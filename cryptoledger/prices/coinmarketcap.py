"""CoinMarketCap. The free Basic plan has latest quotes only; historical quotes
need a paid plan: they are tried, and switched off for the session if refused."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

from ..http import HttpError
from ..models import AdapterError
from ..timeutil import parse_utc
from . import PriceResult

BASE = "https://pro-api.coinmarketcap.com"


class CoinMarketCapPrices:
    name = "coinmarketcap"

    def __init__(self, ctx):
        self.ctx = ctx
        self._historical_ok = True

    def _key(self) -> Optional[str]:
        return self.ctx.secrets.get("coinmarketcap_api_key")

    def _get(self, path: str, params: Dict[str, Any]) -> Any:
        try:
            return self.ctx.http.get_json(BASE + path, params, {"X-CMC_PRO_API_KEY": self._key() or ""})
        except HttpError as e:
            if e.status in (401, 402, 403):
                raise AdapterError(f"CoinMarketCap {path}: not allowed by your plan ({e.status})")
            raise AdapterError(f"CoinMarketCap {path}: {e}")

    @staticmethod
    def _symbol(asset: Dict) -> Optional[str]:
        return (asset.get("binance_symbol") or "").upper() or None

    def can_price(self, asset: Dict) -> bool:
        return bool(self._key()) and bool(self._symbol(asset))

    @staticmethod
    def _best(entries: Any) -> Optional[Dict[str, Any]]:
        if isinstance(entries, dict):
            return entries
        ranked = [e for e in entries or [] if e.get("cmc_rank")]
        return min(ranked, key=lambda e: e["cmc_rank"]) if ranked else (entries or [None])[0]

    def price_at(self, asset: Dict, ts: int) -> Optional[PriceResult]:
        if not self._historical_ok:
            return None
        sym = self._symbol(asset)
        start = datetime.fromtimestamp(ts - 900, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            data = self._get("/v2/cryptocurrency/quotes/historical",
                             {"symbol": sym, "time_start": start, "time_end": end,
                              "interval": "5m", "convert": "EUR"})
        except AdapterError:
            self._historical_ok = False
            return None
        entry = self._best((data.get("data") or {}).get(sym))
        quotes = (entry or {}).get("quotes") or []
        if not quotes:
            return None
        q = quotes[-1]
        eur = ((q.get("quote") or {}).get("EUR") or {})
        if eur.get("price") is None:
            return None
        return PriceResult(Decimal(str(eur["price"])), f"coinmarketcap {sym} EUR",
                           parse_utc(q.get("timestamp") or end))

    def current(self, assets: Dict[str, Dict], now_ts: int) -> Dict[str, PriceResult]:
        syms = sorted({self._symbol(a) for a in assets.values() if self.can_price(a)})
        if not syms:
            return {}
        data = self._get("/v2/cryptocurrency/quotes/latest", {"symbol": ",".join(syms), "convert": "EUR"})
        out: Dict[str, PriceResult] = {}
        for key, a in assets.items():
            sym = self._symbol(a)
            entry = self._best((data.get("data") or {}).get(sym or ""))
            eur = (((entry or {}).get("quote") or {}).get("EUR") or {})
            if eur.get("price") is not None:
                out[key] = PriceResult(Decimal(str(eur["price"])), f"coinmarketcap {sym} EUR", now_ts)
        return out
