"""Per-service API call limits: calls per second and calls per day.

Free plans usually cap both. The defaults below are conservative; set your own
plan's numbers in Settings → API call limits. When a daily cap is reached the
app stops calling that service until the next UTC day (when most providers
reset) and says so, instead of being blocked by the provider.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .http import HttpError

# id -> label, hosts, default calls/second, default calls/day (None = no cap), hint about free plans.
SERVICES: Dict[str, Dict[str, Any]] = {
    "etherscan": {"label": "Etherscan (EVM chains)", "hosts": ["api.etherscan.io"],
                  "per_second": 3, "per_day": 100000, "hint": "Free: 3 calls/s, 100,000 calls/day"},
    "moralis": {"label": "Moralis (Ronin)", "hosts": ["deep-index.moralis.io"],
                "per_second": 5, "per_day": None,
                "hint": "Free plan is metered in compute units per day: see your Moralis dashboard"},
    "solscan": {"label": "Solscan (Solana)", "hosts": ["pro-api.solscan.io"],
                "per_second": 15, "per_day": None,
                "hint": "Lite: 1,000 calls/min, 20M compute units/month (100 per call ≈ 6,600/day)"},
    "helius": {"label": "Helius (Solana)", "hosts": ["mainnet.helius-rpc.com", "api.helius.xyz"],
               "per_second": 10, "per_day": None, "hint": "Free: about 10 RPC calls/s; monthly credits"},
    "solana_public": {"label": "Solana public RPC", "hosts": ["api.mainnet-beta.solana.com"],
                      "per_second": 4, "per_day": None, "hint": "Public endpoint, heavily rate-limited"},
    "trongrid": {"label": "TronGrid (Tron)", "hosts": ["api.trongrid.io"],
                 "per_second": 10, "per_day": 100000, "hint": "Free with key: ~15 calls/s, 100,000/day"},
    "esplora": {"label": "mempool.space / Blockstream (Bitcoin)", "hosts": ["mempool.space", "blockstream.info"],
                "per_second": 3, "per_day": None, "hint": "Public, no key"},
    "solar": {"label": "Solar API (SXP)", "hosts": ["sxp.mainnet.sh"],
              "per_second": 2, "per_day": None, "hint": "Public, no key"},
    "binance_api": {"label": "Binance account API", "hosts": ["api.binance.com"],
                    "per_second": 5, "per_day": None, "hint": "Weight-based: 6,000/min per IP"},
    "binance_data": {"label": "Binance public prices", "hosts": ["data-api.binance.vision"],
                     "per_second": 10, "per_day": None, "hint": "Weight-based: 6,000/min per IP"},
    "coingecko": {"label": "CoinGecko", "hosts": ["api.coingecko.com", "pro-api.coingecko.com"],
                  "per_second": 0.5, "per_day": None,
                  "hint": "Demo: 30-100 calls/min, 10,000/month (≈330/day)"},
    "coinmarketcap": {"label": "CoinMarketCap", "hosts": ["pro-api.coinmarketcap.com"],
                      "per_second": 0.5, "per_day": None,
                      "hint": "Basic: 30 calls/min, 10,000 credits/month (≈330/day)"},
    "ecb": {"label": "ECB exchange rates", "hosts": ["data-api.ecb.europa.eu"],
            "per_second": 2, "per_day": None, "hint": "Public, no key"},
    "other": {"label": "Other (custom networks, RPC URLs)", "hosts": [],
              "per_second": 5, "per_day": None, "hint": "Any host not listed above"},
}


class DailyLimitReached(HttpError):
    """Raised before calling a service whose daily cap is used up. Not retried."""

    def __init__(self, service: str, limit: int, url: str):
        label = SERVICES.get(service, {}).get("label", service)
        super().__init__(429, f"daily limit of {limit:,} calls reached for {label} "
                              "(Settings → API call limits); it resets at 00:00 UTC", url)
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
        """Effective limits: user settings over defaults. per_second/per_day None = no limit."""
        base = SERVICES.get(service, SERVICES["other"])
        user = (self.settings.get("rate_limits") or {}).get(service) or {}
        per_second = user.get("per_second", base["per_second"])
        per_day = user.get("per_day", base["per_day"])
        return {"per_second": float(per_second) if per_second else None,
                "per_day": int(per_day) if per_day else None}

    def used_today(self, service: str) -> int:
        return int(self.db.scalar("SELECT calls FROM api_usage WHERE service=? AND day=?",
                                  (service, utc_day())) or 0)

    def acquire(self, host: str, url: str = "") -> str:
        """Wait for a free slot for this host's service and count the call."""
        service = self.service_for(host)
        lim = self.limits(service)
        if lim["per_day"] is not None and self.used_today(service) >= lim["per_day"]:
            raise DailyLimitReached(service, lim["per_day"], url)
        if lim["per_second"]:
            interval = 1.0 / lim["per_second"]
            with self._lock:
                now = self._clock()
                start = max(now, self._next.get(service, 0.0))
                self._next[service] = start + interval
            if start > now:
                self._sleep(start - now)
        self.db.execute(
            "INSERT INTO api_usage(service, day, calls) VALUES(?,?,1) "
            "ON CONFLICT(service, day) DO UPDATE SET calls=calls+1", (service, utc_day()))
        return service

    def overview(self) -> List[Dict[str, Any]]:
        out = []
        for sid, s in SERVICES.items():
            lim = self.limits(sid)
            out.append({"id": sid, "label": s["label"], "hint": s["hint"], "hosts": s["hosts"],
                        "per_second": lim["per_second"], "per_day": lim["per_day"],
                        "default_per_second": s["per_second"], "default_per_day": s["per_day"],
                        "used_today": self.used_today(sid)})
        return out
