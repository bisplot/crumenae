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
from typing import Dict, Optional, Tuple

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
            # The ECB answers in the format of the Accept header (JSON by default for this app),
            # whatever "format" says: ask for CSV explicitly.
            text = self.ctx.http.get_text(URL.format(cur=currency), {
                "startPeriod": start.isoformat(), "endPeriod": day.isoformat(), "format": "csvdata"},
                headers={"Accept": "text/csv"})
        except HttpError as e:
            raise AdapterError(f"ECB rate {currency}: {e}")
        if text.lstrip().startswith(("{", "<")):
            raise AdapterError(f"ECB rate {currency}: unexpected answer (not CSV)")
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

    def rates_range(self, currency: str, start: date, end: date) -> Dict[date, Decimal]:
        """Units of `currency` per EUR for every day of [start, end] (weekends and holidays take
        the last published rate), from ONE request; already stored days are not asked again."""
        currency = currency.upper()
        if currency == "EUR":
            return {}
        have = self.ctx.db.query("SELECT day, rate FROM fx_rates WHERE currency=? AND day BETWEEN ? AND ? ORDER BY day",
                                 (currency, (start - timedelta(days=LOOKBACK_DAYS)).isoformat(), end.isoformat()))
        checked = self.ctx.db.get_meta(f"fx_range:{currency}") or ""
        if not self.ctx.offline and (not checked or checked < end.isoformat()):
            first = start - timedelta(days=LOOKBACK_DAYS)
            if checked:
                first = max(first, date.fromisoformat(checked) - timedelta(days=LOOKBACK_DAYS))
            try:
                text = self.ctx.http.get_text(URL.format(cur=currency), {
                    "startPeriod": first.isoformat(), "endPeriod": end.isoformat(), "format": "csvdata"},
                    headers={"Accept": "text/csv"})
            except HttpError as e:
                raise AdapterError(f"ECB rates {currency}: {e}")
            if text.lstrip().startswith(("{", "<")):
                raise AdapterError(f"ECB rates {currency}: unexpected answer (not CSV)")
            with self.ctx.db.tx():
                for d, r in parse_csv(text):
                    self.ctx.db.execute("INSERT OR REPLACE INTO fx_rates(currency, day, rate) VALUES(?,?,?)",
                                        (currency, d, str(r)))
                self.ctx.db.set_meta(f"fx_range:{currency}", max(checked, end.isoformat()))
            have = self.ctx.db.query("SELECT day, rate FROM fx_rates WHERE currency=? AND day BETWEEN ? AND ? ORDER BY day",
                                     (currency, (start - timedelta(days=LOOKBACK_DAYS)).isoformat(), end.isoformat()))
        known = [(date.fromisoformat(r["day"]), Decimal(r["rate"])) for r in have]
        out: Dict[date, Decimal] = {}
        i, last = 0, None
        d = start
        while d <= end:
            while i < len(known) and known[i][0] <= d:
                last = known[i]
                i += 1
            if last and (d - last[0]).days <= LOOKBACK_DAYS:
                out[d] = last[1]
            d += timedelta(days=1)
        return out

    def usd_to_eur(self, usd: Decimal, day: date) -> Tuple[Decimal, str]:
        rate, used = self.rate("USD", day)
        return usd / rate, f"ECB USD/EUR {rate} ({used})"
