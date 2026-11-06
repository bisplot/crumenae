"""User preferences stored in the database, with defaults."""
from __future__ import annotations

import json
from typing import Any, Dict

from .db import Database

DEFAULTS: Dict[str, Any] = {
    # Fiscal calendar: cutoff is 23:59:59 local time on fiscal_year_end.
    "timezone": "Europe/Rome",
    "fiscal_year_end": "12-31",
    # First fiscal year to compute (None = year of the oldest transaction).
    "first_fiscal_year": None,
    # Price providers, tried in order. Historical: price at a moment in the past.
    "historical_price_providers": ["binance", "coingecko", "coinmarketcap"],
    "current_price_providers": ["coingecko", "binance", "coinmarketcap"],
    # How USD(T)-quoted prices become EUR: "ecb" (official ECB reference rate
    # of that day) or "binance" (EUR/USDT market price at the same minute).
    "usd_to_eur": "ecb",
    # CoinGecko plan for the key you set: "demo" or "pro".
    "coingecko_plan": "demo",
    # Value every transaction in EUR at its own time (more API calls).
    "value_transactions": True,
    # While the app runs, take a live snapshot around each fiscal-year cutoff.
    "auto_snapshot": True,
    # A snapshot counts as "at the cutoff" if taken within this many seconds of it.
    "snapshot_tolerance_seconds": 300,
    # Italian IC (imposta sul valore delle cripto-attività) rate, used for the RW export estimate.
    "ic_rate": "0.002",
    # Per-network overrides: {"bsc": {"explorer_api": "https://...", "explorer_key": ""}}
    "network_overrides": {},
    # API call limits per service: {"etherscan": {"per_second": 3, "per_day": 100000}}.
    # Missing values use the defaults in ratelimit.SERVICES; null or 0 = no limit.
    "rate_limits": {},
    # Platforms offered in the wallet form (wallet app, device or exchange). Starts empty:
    # every new platform typed on a wallet is added here.
    "platforms": [],
    # API providers you added (Settings → API providers): [{id, name, type, base_url, ...}].
    "providers": [],
}


class Settings:
    def __init__(self, db: Database):
        self.db = db

    def get(self, key: str) -> Any:
        raw = self.db.scalar("SELECT value FROM settings WHERE key=?", (key,))
        if raw is None:
            return DEFAULTS.get(key)
        return json.loads(raw)

    def set(self, key: str, value: Any) -> None:
        if key not in DEFAULTS:
            raise KeyError(f"unknown setting '{key}'")
        self.db.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def all(self) -> Dict[str, Any]:
        out = dict(DEFAULTS)
        for row in self.db.query("SELECT key, value FROM settings"):
            if row["key"] in DEFAULTS:
                out[row["key"]] = json.loads(row["value"])
        return out
