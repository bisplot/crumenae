"""Binance public market data (no key, full history, 1-minute candles).

Uses data-api.binance.vision, Binance's market-data-only endpoint. The price at
time T is the close of the 1-minute candle containing T: for a 23:59:59 cutoff
that is the last trade of 23:59:59.999.

Order of attempts: <SYM>EUR, then EUR<SYM> (inverted), then <SYM>USDT/USDC/FDUSD/BUSD
converted to EUR with the ECB rate of that day (or EUR/USDT, per settings).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, Optional, Set, Tuple

from ..http import HttpError
from ..models import AdapterError
from . import PriceResult

BASE = "https://data-api.binance.vision"
USD_QUOTES = ("USDT", "USDC", "FDUSD", "BUSD")
MAX_GAP_MS = 5 * 60_000


class BinancePrices:
    name = "binance"

    def __init__(self, ctx, fx):
        self.ctx = ctx
        self.fx = fx
        self._invalid: Set[str] = set()

    def can_price(self, asset: Dict) -> bool:
        return bool(asset.get("binance_symbol"))

    def kline_close(self, symbol: str, ts: int) -> Optional[Tuple[Decimal, int]]:
        if symbol in self._invalid:
            return None
        start_ms = (ts // 60) * 60_000
        try:
            rows = self.ctx.http.get_json(BASE + "/api/v3/klines", {
                "symbol": symbol, "interval": "1m", "startTime": start_ms, "limit": 1})
        except HttpError as e:
            if e.status == 400 and "-1121" in e.body:  # invalid symbol
                self._invalid.add(symbol)
                return None
            raise AdapterError(f"Binance klines {symbol}: {e}")
        if not rows:
            return None
        open_ms, close, close_ms = int(rows[0][0]), Decimal(str(rows[0][4])), int(rows[0][6])
        if open_ms - start_ms > MAX_GAP_MS:
            return None  # pair was not trading at that time
        return close, close_ms // 1000

    def price_at(self, asset: Dict, ts: int) -> Optional[PriceResult]:
        sym = asset["binance_symbol"].upper()
        if sym == "EUR":
            return PriceResult(Decimal(1), "fixed", ts, "EUR")
        r = self.kline_close(sym + "EUR", ts)
        if r:
            return PriceResult(r[0], f"binance {sym}EUR 1m close", r[1])
        r = self.kline_close("EUR" + sym, ts)
        if r and r[0]:
            return PriceResult(Decimal(1) / r[0], f"binance 1/EUR{sym} 1m close", r[1])
        for quote in USD_QUOTES:
            if quote == sym:
                continue
            r = self.kline_close(sym + quote, ts)
            if r:
                eur, note = self.usd_to_eur(r[0], ts, quote)
                return PriceResult(eur, f"binance {sym}{quote} 1m close × {note}", r[1],
                                   f"{quote} treated as 1 USD" if self.ctx.settings.get("usd_to_eur") == "ecb" else "")
        return None

    def usd_to_eur(self, usd: Decimal, ts: int, quote: str) -> Tuple[Decimal, str]:
        if self.ctx.settings.get("usd_to_eur") == "binance" and quote in ("USDT", "USDC", "FDUSD"):
            r = self.kline_close("EUR" + quote, ts)
            if r and r[0]:
                return usd / r[0], f"binance EUR{quote}"
        eur, note = self.fx.usd_to_eur(usd, self.ctx.calendar.local_date(ts))
        return eur, note

    def current(self, assets: Dict[str, Dict], now_ts: int) -> Dict[str, PriceResult]:
        """Live prices of every asset from ONE call (/api/v3/ticker/price, all pairs), instead of
        up to six candle requests per asset."""
        wanted = {k: a for k, a in assets.items() if self.can_price(a)}
        if not wanted:
            return {}
        try:
            rows = self.ctx.http.get_json(BASE + "/api/v3/ticker/price")
        except HttpError as e:
            raise AdapterError(f"Binance ticker prices: {e}")
        book = {r["symbol"]: Decimal(str(r["price"])) for r in rows or [] if r.get("symbol") and r.get("price")}
        out: Dict[str, PriceResult] = {}
        usd_rate = None
        for key, a in wanted.items():
            sym = a["binance_symbol"].upper()
            if sym == "EUR":
                out[key] = PriceResult(Decimal(1), "fixed", now_ts, "EUR")
            elif book.get(sym + "EUR"):
                out[key] = PriceResult(book[sym + "EUR"], f"binance {sym}EUR live", now_ts)
            elif book.get("EUR" + sym):
                out[key] = PriceResult(Decimal(1) / book["EUR" + sym], f"binance 1/EUR{sym} live", now_ts)
            else:
                quote = next((q for q in USD_QUOTES if q != sym and book.get(sym + q)), None)
                if not quote:
                    continue
                if usd_rate is None:
                    try:
                        usd_rate = self.usd_to_eur(Decimal(1), now_ts, quote)
                    except AdapterError:
                        usd_rate = False
                if not usd_rate:
                    continue
                out[key] = PriceResult(book[sym + quote] * usd_rate[0], f"binance {sym}{quote} live × {usd_rate[1]}",
                                       now_ts, f"{quote} treated as 1 USD"
                                       if self.ctx.settings.get("usd_to_eur") == "ecb" else "")
        return out
