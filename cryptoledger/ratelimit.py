"""Per-service API call limits: calls per second (or per minute) and calls per day.

Free plans usually cap both. The defaults below are conservative; set your own
plan's numbers in Settings → Blockchain APIs or Price sources. When a daily cap is reached the
app stops calling that service until the next UTC day (when most providers
reset) and says so, instead of being blocked by the provider.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .http import HttpError

# id -> label, hosts, default rate + unit ("second" or "minute"), default calls/day (None = no cap),
# hint about free plans.
SERVICES: Dict[str, Dict[str, Any]] = {
    "etherscan": {"label": "Etherscan (EVM chains)", "hosts": ["api.etherscan.io"],
                  "rate": 3, "unit": "second", "per_day": 100000, "hint": "Free: 3 calls/s, 100,000 calls/day"},
    "moralis": {"label": "Moralis (Ronin)", "hosts": ["deep-index.moralis.io"],
                "rate": 5, "unit": "second", "per_day": None,
                "hint": "Free plan is metered in compute units per day: see your Moralis dashboard"},
    "alchemy": {"label": "Alchemy (BNB Smart Chain)", "hosts": ["bnb-mainnet.g.alchemy.com"],
                "rate": 10, "unit": "second", "per_day": None,
                "hint": "Free plan: monthly compute units (about 120 per transfers call); see your Alchemy dashboard"},
    "nodereal": {"label": "NodeReal MegaNode (BNB Smart Chain)", "hosts": ["bsc-mainnet.nodereal.io"],
                 "rate": 5, "unit": "second", "per_day": None,
                 "hint": "Free key: 10M compute units/month, 150 CU/s; see your NodeReal dashboard"},
    "helius": {"label": "Helius (Solana)", "hosts": ["mainnet.helius-rpc.com", "api.helius.xyz"],
               "rate": 10, "unit": "second", "per_day": None,
               "hint": "Free: 10 RPC calls/s, monthly credits (1 credit per standard call)"},
    "solana_public": {"label": "Solana public RPC", "hosts": ["api.mainnet-beta.solana.com"],
                      "rate": 4, "unit": "second", "per_day": None, "hint": "Public endpoint, heavily rate-limited"},
    "trongrid": {"label": "TronGrid (Tron)", "hosts": ["api.trongrid.io"],
                 "rate": 10, "unit": "second", "per_day": 100000, "hint": "Free key at trongrid.io: ~15 calls/s, 100,000/day; without a key calls may be refused"},
    "esplora": {"label": "mempool.space / Blockstream (Bitcoin)", "hosts": ["mempool.space", "blockstream.info"],
                "rate": 3, "unit": "second", "per_day": None, "hint": "Public, no key"},
    "solar": {"label": "Solar API (SXP)", "hosts": ["sxp.mainnet.sh"],
              "rate": 2, "unit": "second", "per_day": None, "hint": "Public, no key"},
    "binance_api": {"label": "Binance account API", "hosts": ["api.binance.com"],
                    "rate": 5, "unit": "second", "per_day": None, "hint": "Weight-based: 6,000/min per IP"},
    "binance_data": {"label": "Binance public prices", "hosts": ["data-api.binance.vision"],
                     "rate": 10, "unit": "second", "per_day": None, "hint": "Weight-based: 6,000/min per IP"},
    "coingecko": {"label": "CoinGecko", "hosts": ["api.coingecko.com", "pro-api.coingecko.com"],
                  "rate": 30, "unit": "minute", "per_day": None,
                  "hint": "Demo: 30-100 calls/min, 10,000/month (≈330/day)"},
    "coinmarketcap": {"label": "CoinMarketCap", "hosts": ["pro-api.coinmarketcap.com"],
                      "rate": 30, "unit": "minute", "per_day": None,
                      "hint": "Basic: 30 calls/min, 10,000 credits/month (≈330/day)"},
    "ecb": {"label": "ECB exchange rates", "hosts": ["data-api.ecb.europa.eu"],
            "rate": 2, "unit": "second", "per_day": None, "hint": "Public, no key"},
    "other": {"label": "Other (custom networks, RPC URLs)", "hosts": [],
              "rate": 5, "unit": "second", "per_day": None, "hint": "Any host not listed above"},
}


UNITS = {"second": 1, "minute": 60}   # seconds per unit


# API key (secret name) of each service, and services that provide prices rather than wallet data.
SECRET_OF = {"etherscan": "etherscan_api_key", "moralis": "moralis_api_key", "nodereal": "nodereal_api_key", "alchemy": "alchemy_api_key", "helius": "helius_api_key", "trongrid": "trongrid_api_key", "coingecko": "coingecko_api_key",
             "coinmarketcap": "coinmarketcap_api_key"}
PRICE_SERVICES = ("binance_data", "coingecko", "coinmarketcap", "ecb")


class DailyLimitReached(HttpError):
    """Raised before calling a service whose daily cap is used up. Not retried."""

    def __init__(self, service: str, limit: int, url: str):
        label = SERVICES.get(service, {}).get("label", service)
        super().__init__(429, f"daily limit of {limit:,} calls reached for {label} "
                              "(Settings → Blockchain APIs or Price sources); it resets at 00:00 UTC", url)
        self.service = service


def utc_day(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc).date().isoformat()


class RateLimiter:
    def __init__(self, db, settings, clock=time.monotonic, sleep=time.sleep):
        self.db = db
        self.settings = settings
        self._clock = clock
        self._sleep = sleep
        self._next: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._host_map = {h: sid for sid, s in SERVICES.items() for h in s["hosts"]}

    def service_for(self, host: str) -> str:
        return self._host_map.get((host or "").lower(), "other")

    def limits(self, service: str) -> Dict[str, Any]:
        """Effective limits: user settings over defaults. rate/per_day None = no limit.
        per_second is the rate converted to calls per second."""
        base = SERVICES.get(service, SERVICES["other"])
        user = (self.settings.get("rate_limits") or {}).get(service) or {}
        if "rate" in user or "unit" in user:
            rate, unit = user.get("rate", base["rate"]), user.get("unit", base["unit"])
        elif "per_second" in user:                      # settings saved by v0.1
            rate, unit = user["per_second"], "second"
        else:
            rate, unit = base["rate"], base["unit"]
        unit = unit if unit in UNITS else "second"
        per_day = user.get("per_day", base["per_day"])
        rate = float(rate) if rate else None
        return {"rate": rate, "unit": unit, "per_second": rate / UNITS[unit] if rate else None,
                "per_day": int(per_day) if per_day else None}

    def used_today(self, service: str) -> int:
        return int(self.db.scalar("SELECT calls FROM api_usage WHERE service=? AND day=?",
                                  (service, utc_day())) or 0)

    def acquire(self, host: str, url: str = "", count: bool = True) -> str:
        """Wait for a free slot for this host's service and count the call (unless count=False:
        a call the provider does not charge, such as a free key check)."""
        service = self.service_for(host)
        lim = self.limits(service)
        if count and lim["per_day"] is not None and self.used_today(service) >= lim["per_day"]:
            raise DailyLimitReached(service, lim["per_day"], url)
        if lim["per_second"]:
            interval = 1.0 / lim["per_second"]
            with self._lock:
                now = self._clock()
                start = max(now, self._next.get(service, 0.0))
                self._next[service] = start + interval
            if start > now:
                self._sleep(start - now)
        if not count:
            return service
        self.db.execute(
            "INSERT INTO api_usage(service, day, calls) VALUES(?,?,1) "
            "ON CONFLICT(service, day) DO UPDATE SET calls=calls+1", (service, utc_day()))
        return service

    def overview(self) -> List[Dict[str, Any]]:
        out = []
        for sid, s in SERVICES.items():
            lim = self.limits(sid)
            out.append({"id": sid, "label": s["label"], "hint": s["hint"], "hosts": s["hosts"],
                        "rate": lim["rate"], "unit": lim["unit"], "per_second": lim["per_second"],
                        "per_day": lim["per_day"], "default_rate": s["rate"], "default_unit": s["unit"],
                        "default_per_day": s["per_day"], "used_today": self.used_today(sid)})
        return out
