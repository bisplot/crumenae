"""EUR prices at any moment, from a chain of providers, with a persistent cache.

Every price carries its source and the exact time of the quote, so each value
in a report can be traced back.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

from ..models import AdapterError
from ..util import D, dstr, now


@dataclass
class PriceResult:
    price: Optional[Decimal]
    source: str
    price_ts: Optional[int]
    note: str = ""


GHOST_SOURCE = "ghost (no market)"


def ghost_price(ts: int) -> PriceResult:
    return PriceResult(Decimal(0), GHOST_SOURCE, ts, "")


def identity(asset: Optional[Dict[str, Any]]) -> Optional[str]:
    """Cache key: same coin -> same key, whichever chain it sits on."""
    if not asset:
        return None
    if asset.get("fiat"):
        return "fiat:" + asset["fiat"]
    if asset.get("coingecko_id"):
        return "cg:" + asset["coingecko_id"]
    if asset.get("binance_symbol"):
        return "bn:" + asset["binance_symbol"].upper()
    return None


class PriceService:
    def __init__(self, ctx):
        from .binance import BinancePrices
        from .coingecko import CoinGeckoPrices
        from .coinmarketcap import CoinMarketCapPrices
        from .ecb import EcbRates

        self.ctx = ctx
        self.fx = EcbRates(ctx)
        self.coingecko = CoinGeckoPrices(ctx)
        self.providers = {
            "binance": BinancePrices(ctx, self.fx),
            "coingecko": self.coingecko,
            "coinmarketcap": CoinMarketCapPrices(ctx),
        }
        self._missed: Set[Tuple[str, int]] = set()
        self.errors: List[str] = []
        self.demo = None
        if ctx.db.get_meta("demo") == "1":
            from .demo import DemoPrices
            self.demo = DemoPrices()

    # --- cache --------------------------------------------------------------------
    def _cached(self, ident: str, ts: int) -> Optional[PriceResult]:
        row = self.ctx.db.query_one("SELECT * FROM prices WHERE price_key=? AND ts=?", (ident, ts))
        if row:
            return PriceResult(D(row["price_eur"]), row["source"], row["price_ts"], row["note"])
        return None

    def _store(self, ident: str, ts: int, r: PriceResult, live: bool = False) -> None:
        note = ("live " + r.note).strip() if live else r.note
        self.ctx.db.execute(
            "INSERT OR REPLACE INTO prices(price_key, ts, price_eur, source, price_ts, note, fetched_at)"
            " VALUES(?,?,?,?,?,?,?)", (ident, ts, dstr(r.price), r.source, r.price_ts, note, now()))

    # --- historical -----------------------------------------------------------------
    def price_at(self, asset: Optional[Dict[str, Any]], ts: int) -> PriceResult:
        r = self._price_at(asset, ts)
        if r.price is None and asset and asset.get("status") == "ghost":
            # Ghost token: the price it had while it traded, otherwise EUR 0.
            return ghost_price(ts)
        return r

    def _price_at(self, asset: Optional[Dict[str, Any]], ts: int) -> PriceResult:
        if not asset:
            return PriceResult(None, "unmapped", None, "unknown asset")
        if asset.get("status") in ("spam", "ignored"):
            return PriceResult(None, "ignored", None, f"asset marked {asset['status']}")
        ident = identity(asset)
        if not ident:
            return PriceResult(None, "unmapped", None, "no price source: map it on the Assets page")
        cached = self._cached(ident, ts)
        if cached:
            return cached
        if asset.get("fiat"):
            return self._fiat(ident, asset["fiat"], ts)
        if self.demo is not None:
            r = self.demo.price_at(asset, ts)
            if r:
                self._store(ident, ts, r)
                return r
            return PriceResult(None, "unavailable", None, "no demo price")
        if self.ctx.offline:
            return PriceResult(None, "offline", None, "price not in cache")
        if (ident, ts) in self._missed:
            return PriceResult(None, "unavailable", None, "no provider returned a price")
        notes: List[str] = []
        for name in self.ctx.settings.get("historical_price_providers") or []:
            provider = self.providers.get(name)
            if not provider or not provider.can_price(asset):
                continue
            try:
                r = provider.price_at(asset, ts)
            except AdapterError as e:
                notes.append(str(e))
                continue
            if r and r.price is not None:
                self._store(ident, ts, r)
                return r
            notes.append(f"{name}: no price at that time")
        self._missed.add((ident, ts))
        return PriceResult(None, "unavailable", None, "; ".join(notes) or "no provider can price this asset")

    def _fiat(self, ident: str, currency: str, ts: int) -> PriceResult:
        if currency == "EUR":
            return PriceResult(Decimal(1), "EUR", ts)
        try:
            rate, day = self.fx.rate(currency, self.ctx.calendar.local_date(ts))
        except AdapterError as e:
            return PriceResult(None, "unavailable", None, str(e))
        r = PriceResult(Decimal(1) / rate, f"ECB {currency}/EUR {rate} ({day})", ts)
        self._store(ident, ts, r)
        return r

    # --- current --------------------------------------------------------------------
    def refresh_current(self, assets: Dict[str, Dict[str, Any]]) -> Dict[str, PriceResult]:
        """Fetch live prices for {asset_key: asset_row}; cached in 5-minute buckets."""
        now_ts = now()
        bucket = now_ts - now_ts % 300
        results: Dict[str, PriceResult] = {}
        remaining: Dict[str, Dict[str, Any]] = {}
        for key, a in assets.items():
            ident = identity(a)
            if not ident or a.get("status") in ("spam", "ignored"):
                continue   # ghost coins are looked up too: if a market exists, its price is used
            if self._recent_miss(ident, now_ts):
                continue   # no provider had a price in the last 24 h: not asked again yet
            cached = self._cached(ident, bucket)
            if cached:
                results[key] = cached
            elif a.get("fiat"):
                results[key] = self._fiat(ident, a["fiat"], now_ts)
            else:
                remaining[key] = a
        if self.demo is not None:
            for key, a in remaining.items():
                r = self.demo.price_at(a, bucket)
                if r:
                    results[key] = r
                    self._store(identity(a), bucket, r, live=True)
            return results
        if self.ctx.offline:
            return results
        for name in self.ctx.settings.get("current_price_providers") or []:
            provider = self.providers.get(name)
            if not provider or not remaining:
                continue
            try:
                got = provider.current(remaining, now_ts)
            except AdapterError as e:
                self.errors.append(str(e))
                continue
            for key, r in got.items():
                results[key] = r
                self._store(identity(remaining[key]), bucket, r, live=True)
                remaining.pop(key, None)
        for a in remaining.values():   # no provider prices these: remember it for a day
            self.ctx.db.set_meta(f"live_miss:{identity(a)}", str(now_ts))
        return results

    MISS_TTL = 24 * 3600

    def _recent_miss(self, ident: str, now_ts: int) -> bool:
        raw = self.ctx.db.get_meta(f"live_miss:{ident}")
        return bool(raw) and now_ts - int(raw) < self.MISS_TTL

    def latest(self, asset: Optional[Dict[str, Any]]) -> Optional[PriceResult]:
        """Most recent cached price (live first), without network calls."""
        ident = identity(asset)
        if (asset or {}).get("status") == "ghost":
            # A ghost coin uses a live price of the last 2 days if one exists, otherwise EUR 0.
            row = self.ctx.db.query_one(
                "SELECT * FROM prices WHERE price_key=? AND note LIKE 'live%' AND fetched_at>=? "
                "ORDER BY COALESCE(price_ts, ts) DESC LIMIT 1", (ident or "", now() - 2 * 86400)) if ident else None
            if row:
                return PriceResult(D(row["price_eur"]), row["source"], row["price_ts"] or row["ts"], row["note"])
            return ghost_price(now())
        if not ident or (asset or {}).get("status") in ("spam", "ignored"):
            return None
        if ident == "fiat:EUR":
            return PriceResult(Decimal(1), "EUR", now())
        row = self.ctx.db.query_one(
            "SELECT * FROM prices WHERE price_key=? ORDER BY COALESCE(price_ts, ts) DESC LIMIT 1", (ident,))
        if not row:
            return None
        return PriceResult(D(row["price_eur"]), row["source"], row["price_ts"] or row["ts"], row["note"])
