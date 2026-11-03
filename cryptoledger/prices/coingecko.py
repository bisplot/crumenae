"""CoinGecko: EUR prices directly. Works without a key (public, slow); a free
Demo key raises the limits but historical data is restricted to the past 365
days; a Pro key gives full history."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from ..http import HttpError
from ..models import AdapterError
from . import PriceResult

PUBLIC = "https://api.coingecko.com/api/v3"
PRO = "https://pro-api.coingecko.com/api/v3"


class CoinGeckoPrices:
    name = "coingecko"

    def __init__(self, ctx):
        self.ctx = ctx

    def _base(self) -> Tuple[str, Dict[str, str]]:
        key = self.ctx.secrets.get("coingecko_api_key")
        if key and self.ctx.settings.get("coingecko_plan") == "pro":
            return PRO, {"x-cg-pro-api-key": key}
        if key:
            return PUBLIC, {"x-cg-demo-api-key": key}
        return PUBLIC, {}

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        base, headers = self._base()
        try:
            return self.ctx.http.get_json(base + path, params, headers)
        except HttpError as e:
            if e.status == 404:
                return None
            raise AdapterError(f"CoinGecko {path}: {e}")

    def can_price(self, asset: Dict) -> bool:
        return bool(asset.get("coingecko_id"))

    def price_at(self, asset: Dict, ts: int) -> Optional[PriceResult]:
        cg = asset["coingecko_id"]
        data = self._get(f"/coins/{cg}/market_chart/range",
                         {"vs_currency": "eur", "from": ts - 2 * 3600, "to": ts + 60})
        points = (data or {}).get("prices") or []
        best = None
        for ms, price in points:
            t = int(ms) // 1000
            if t <= ts and (best is None or t > best[0]):
                best = (t, price)
        if best is None and points:
            ms, price = min(points, key=lambda p: abs(int(p[0]) // 1000 - ts))
            best = (int(ms) // 1000, price)
        if best is None:
            return None
        gap = ts - best[0]
        note = f"nearest CoinGecko point {abs(gap) // 60} min {'before' if gap >= 0 else 'after'}" if abs(gap) > 300 else ""
        return PriceResult(Decimal(str(best[1])), f"coingecko {cg} EUR", best[0], note)

    def current(self, assets: Dict[str, Dict], now_ts: int) -> Dict[str, PriceResult]:
        ids = sorted({a["coingecko_id"] for a in assets.values() if self.can_price(a)})
        out: Dict[str, PriceResult] = {}
        prices: Dict[str, Any] = {}
        for i in range(0, len(ids), 200):
            chunk = ids[i:i + 200]
            data = self._get("/simple/price", {"ids": ",".join(chunk), "vs_currencies": "eur",
                                               "include_last_updated_at": "true"}) or {}
            prices.update(data)
        for key, a in assets.items():
            p = prices.get(a.get("coingecko_id") or "")
            if p and p.get("eur") is not None:
                out[key] = PriceResult(Decimal(str(p["eur"])), f"coingecko {a['coingecko_id']} EUR",
                                       int(p.get("last_updated_at") or now_ts))
        return out

    def lookup_contract(self, platform: str, contract: str) -> Optional[Dict[str, Any]]:
        """Map an unknown token to a CoinGecko coin by its contract address."""
        data = self._get(f"/coins/{platform}/contract/{contract}")
        if not data or not data.get("id"):
            return None
        return {"id": data["id"], "symbol": (data.get("symbol") or "").upper(), "name": data.get("name", "")}
