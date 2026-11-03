"""While the app is running, take live snapshots around each fiscal-year cutoff:
one ~1 minute before 23:59:59 and one ~1 minute after. The app does not need to
be open for this if you install the launchd/cron job (see README)."""
from __future__ import annotations

import threading
import time
import traceback

from . import sync
from .util import now

PRE_WINDOW = (90, 20)     # seconds before the cutoff: start between 90 and 20 s before
POST_WINDOW = (20, 180)   # seconds after the cutoff


class AutoSnapshot(threading.Thread):
    def __init__(self, ctx, jobs, interval: float = 10.0):
        super().__init__(name="auto-snapshot", daemon=True)
        self.ctx = ctx
        self.jobs = jobs
        self.interval = interval

    def run(self) -> None:
        while True:
            try:
                self.tick(now())
            except Exception:
                self.ctx.log(traceback.format_exc(), "error")
            time.sleep(self.interval)

    def due(self, t: int):
        """(fiscal_year, tag) if a snapshot is due at time t, else None."""
        cal = self.ctx.calendar
        current = cal.year_of(t)
        pre_cut = cal.cutoff(current)
        if pre_cut - PRE_WINDOW[0] <= t <= pre_cut - PRE_WINDOW[1]:
            return current, "pre"
        post_cut = cal.cutoff(current - 1)
        if post_cut + POST_WINDOW[0] <= t <= post_cut + POST_WINDOW[1]:
            return current - 1, "post"
        return None

    def tick(self, t: int) -> bool:
        if self.ctx.offline or not self.ctx.settings.get("auto_snapshot"):
            return False
        due = self.due(t)
        if not due:
            return False
        marker = f"auto_snapshot:{due[0]}:{due[1]}"
        if self.ctx.db.get_meta(marker):
            return False
        self.ctx.db.set_meta(marker, str(t))
        self.ctx.log(f"automatic year-end snapshot ({due[1]} cutoff {due[0]})")
        sync.take_snapshot(self.ctx, lambda m: self.ctx.log(m), source="scheduled")
        return True
