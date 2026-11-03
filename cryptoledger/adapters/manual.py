"""Manual accounts: any platform without an API (or not supported yet).

You type the quantities held at each fiscal-year end (and, if you like, today)
on the Year-end page; they are stored as snapshots with source "manual" and
valued in EUR like everything else.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Tuple

from ..util import D
from . import register
from .base import Adapter, AssetInfo, Logger, SyncResult


@register
class ManualAdapter(Adapter):
    id = "manual"
    kind = "manual"
    needs_address = False
    has_history = False

    def validate_address(self, address: str) -> str:
        return address.strip()[:64]

    def sync(self, wallet: Dict[str, Any], state: Dict[str, Any], log: Logger) -> SyncResult:
        return SyncResult(state=dict(state))

    def current_balances(self, wallet: Dict[str, Any], known_assets: List[str],
                         log: Logger) -> Tuple[Dict[str, Decimal], List[AssetInfo]]:
        """Latest manual entry per asset."""
        rows = self.ctx.db.query(
            "SELECT s.asset_key, s.amount FROM snapshots s JOIN ("
            "  SELECT asset_key, MAX(taken_at) AS t FROM snapshots"
            "  WHERE wallet_id=? AND source='manual' GROUP BY asset_key) m"
            " ON s.asset_key=m.asset_key AND s.taken_at=m.t"
            " WHERE s.wallet_id=? AND s.source='manual'", (wallet["id"], wallet["id"]))
        return {r["asset_key"]: D(r["amount"]) for r in rows}, []
