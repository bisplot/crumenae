"""ECB euro foreign exchange reference rates (free, no key).

Rates are units of currency per 1 EUR. The ECB does not publish on TARGET
holidays and weekends; the last rate published on or before the date is used,
as is usual practice.
"""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional, Tuple

from ..http import HttpError
from ..models import AdapterError

URL = "https://data-api.ecb.europa.eu/service/data/EXR/D.{cur}.EUR.SP00.A"
LOOKBACK_DAYS = 10


def parse_csv(text: str):
    """Yield (day, rate) from an ECB SDMX csvdata response."""
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        day, value = row.get("TIME_PERIOD"), row.get("OBS_VALUE")
        if day and value not in (None, "", "NaN"):
            yield day, Decimal(value)


class EcbRates:
    def __init__(self, ctx):
        self.ctx = ctx

    def rate(self, currency: str, day: date) -> Tuple[Decimal, str]:
        """(units of `currency` per EUR, ISO date of the rate used)."""
        currency = currency.upper()
        if currency == "EUR":
            return Decimal(1), day.isoformat()
        found = self._cached(currency, day)
        if found:
            return found
        if self.ctx.offline:
            raise AdapterError(f"ECB {currency} rate for {day} not cached (offline)")
        start = day - timedelta(days=LOOKBACK_DAYS)
        try:
            text = self.ctx.http.get_text(URL.format(cur=currency), {
                "startPeriod": start.isoformat(), "endPeriod": day.isoformat(), "format": "csvdata"})
        except HttpError as e:
            raise AdapterError(f"ECB rate {currency}: {e}")
        rows = list(parse_csv(text))
        with self.ctx.db.tx():
            for d, r in rows:
                self.ctx.db.execute("INSERT OR REPLACE INTO fx_rates(currency, day, rate) VALUES(?,?,?)",
                                    (currency, d, str(r)))
            # Remember that this window was fetched, even if empty (holidays).
            self.ctx.db.set_meta(f"fx_checked:{currency}:{day.isoformat()}", "1")
        found = self._cached(currency, day)
        if not found:
            raise AdapterError(f"no ECB {currency} rate in the {LOOKBACK_DAYS} days up to {day}")
        return found

    def _cached(self, currency: str, day: date) -> Optional[Tuple[Decimal, str]]:
        row = self.ctx.db.query_one(
            "SELECT day, rate FROM fx_rates WHERE currency=? AND day<=? ORDER BY day DESC LIMIT 1",
            (currency, day.isoformat()))
        if not row:
            return None
        exact = row["day"] == day.isoformat()
        checked = self.ctx.db.get_meta(f"fx_checked:{currency}:{day.isoformat()}") == "1"
        recent = (day - date.fromisoformat(row["day"])).days <= LOOKBACK_DAYS
        if exact or (checked and recent):
            return Decimal(row["rate"]), row["day"]
        return None

    def usd_to_eur(self, usd: Decimal, day: date) -> Tuple[Decimal, str]:
        rate, used = self.rate("USD", day)
        return usd / rate, f"ECB USD/EUR {rate} ({used})"
