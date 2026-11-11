"""Fiscal-year arithmetic.

A fiscal year is labelled by the calendar year in which it ends. Its cutoff is
23:59:59 local time on the fiscal-year-end day (default 31 December, Europe/Rome),
so for Italy the TY2025 cutoff is 2025-12-31 23:59:59 CET = 22:59:59 UTC.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import List

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python < 3.9
    raise SystemExit("Crumenae needs Python 3.9 or newer.")


class FiscalCalendar:
    def __init__(self, tz_name: str = "Europe/Rome", fy_end: str = "12-31"):
        try:
            self.tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            raise ValueError(
                f"Unknown time zone '{tz_name}'. On Windows run: pip install tzdata"
            )
        self.tz_name = tz_name
        try:
            month, day = (int(x) for x in fy_end.split("-"))
            date(2001, month, day)  # validates, and rejects 02-29
        except Exception:
            raise ValueError(f"Fiscal year end must be MM-DD (got '{fy_end}')")
        self.month, self.day = month, day

    # --- cutoffs -----------------------------------------------------------------
    def end_date(self, year: int) -> date:
        return date(year, self.month, self.day)

    def cutoff(self, year: int) -> int:
        """Unix timestamp of 23:59:59 local time on the last day of fiscal year `year`."""
        return self.end_of_day(self.end_date(year))

    def start(self, year: int) -> int:
        """First second of fiscal year `year` (= previous cutoff + 1)."""
        return self.cutoff(year - 1) + 1

    def end_of_day(self, d: date) -> int:
        return int(datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=self.tz).timestamp())

    def year_of(self, ts: int) -> int:
        y = self.local(ts).year
        return y if ts <= self.cutoff(y) else y + 1

    def days_in_year(self, year: int) -> int:
        return (self.end_date(year) - self.end_date(year - 1)).days

    def days_of_year(self, year: int) -> List[date]:
        first = self.end_date(year - 1) + timedelta(days=1)
        return [first + timedelta(days=i) for i in range(self.days_in_year(year))]

    def last_completed_year(self, now_ts: int) -> int:
        y = self.year_of(now_ts)
        return y - 1

    # --- formatting --------------------------------------------------------------
    def local(self, ts: int) -> datetime:
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(self.tz)

    def local_date(self, ts: int) -> date:
        return self.local(ts).date()

    def iso_local(self, ts: int) -> str:
        return self.local(ts).isoformat()

    def parse_local(self, text: str) -> int:
        """'YYYY-MM-DD' (noon) or 'YYYY-MM-DD HH:MM[:SS]' / 'YYYY-MM-DDTHH:MM' in local time."""
        text = text.strip().replace("T", " ")
        for fmt, noon in (("%Y-%m-%d %H:%M:%S", False), ("%Y-%m-%d %H:%M", False), ("%Y-%m-%d", True)):
            try:
                d = datetime.strptime(text, fmt)
            except ValueError:
                continue
            if noon:
                d = d.replace(hour=12)
            return int(d.replace(tzinfo=self.tz).timestamp())
        raise ValueError(f"invalid local date/time '{text}'")


def iso_utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(text: str) -> int:
    """Parse 'YYYY-MM-DD HH:MM:SS' (UTC, as Binance returns it) or ISO-8601."""
    text = text.strip().replace("T", " ").replace("Z", "")
    if "+" in text[10:]:
        text = text[: 10 + text[10:].index("+")]
    fmt = "%Y-%m-%d %H:%M:%S" if len(text) > 10 else "%Y-%m-%d"
    if "." in text:
        text = text.split(".")[0]
    return int(datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp())
