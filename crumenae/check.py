"""Wallet check: after each sync, the balance rebuilt from the synced transactions is compared
with the live balance, asset by asset. A difference means the history is incomplete or wrong
(a transaction the provider did not return, a fee not recorded, a token without decimals...),
which would also make a year-end value built from that history wrong: it is shown on the Wallets
page and the Dashboard instead of waiting for a cutoff.

Statuses: "ok" (every asset matches), "diff" (some assets differ), "partial" (part of the live
balance could not be read, so it is not a reliable reference), "n/a" (no transaction history to
compare: exchanges, manual accounts, chains read from live balances only).
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Dict, List

from . import assets as assets_mod
from . import ledger
from .util import ZERO, dstr, now

TOLERANCE = Decimal("1e-6")   # relative, like the year-end cross-check
MAX_ITEMS = 50


def differs(a: Decimal, b: Decimal) -> bool:
    tol = max(Decimal("1e-8"), TOLERANCE * max(abs(a), abs(b)))
    return abs(a - b) > tol


def compare(ctx, wallet: Dict[str, Any], live: Dict[str, Decimal]) -> List[Dict[str, Any]]:
    """Assets whose live balance differs from the history rebuilt now. Spam and ignored assets
    are left out; an asset with history but no live reading is reported as not read."""
    recon, _ = ledger.reconstruct(ctx, wallet["id"], now())
    all_assets = assets_mod.all_assets(ctx)
    out: List[Dict[str, Any]] = []
    for key in sorted(set(live) | set(recon)):
        a = all_assets.get(key)
        if a and assets_mod.is_hidden(a):
            continue
        history = recon.get(key, ZERO)
        now_amount = live.get(key)
        if now_amount is None:
            if not history:
                continue
        elif not differs(now_amount, history):
            continue
        out.append({"asset_key": key, "symbol": (a or {}).get("symbol") or key.split(":", 1)[-1],
                    "live": None if now_amount is None else dstr(now_amount), "history": dstr(history),
                    "difference": None if now_amount is None else dstr(now_amount - history)})
    return out


def run(ctx, wallet: Dict[str, Any], adapter, live: Dict[str, Decimal]) -> Dict[str, Any]:
    """Compare and store the result on the wallet."""
    detail: Dict[str, Any] = {}
    if adapter.kind != "chain" or not adapter.has_history:
        status = "n/a"
    else:
        diffs = compare(ctx, wallet, live)
        detail["assets"] = diffs[:MAX_ITEMS]
        detail["count"] = len(diffs)
        status = "diff" if diffs else "ok"
    if adapter.partial:
        detail["not_read"] = list(adapter.partial)
        status = "partial"
    ctx.db.execute("UPDATE wallets SET check_status=?, check_detail=?, check_at=? WHERE id=?",
                   (status, json.dumps(detail), now(), wallet["id"]))
    return {"status": status, **detail}


def public(w: Dict[str, Any]) -> Dict[str, Any]:
    """The stored check of a wallet row, for the API."""
    try:
        detail = json.loads(w.get("check_detail") or "{}")
    except ValueError:
        detail = {}
    return {"status": w.get("check_status") or None, "at": w.get("check_at"), **detail}
