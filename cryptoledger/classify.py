"""Automatic classification of movements. You can override any category on the
Transactions page; your choice is never overwritten by a later sync.

Each category maps to a tax treatment (Italian rules, see docs/METHODOLOGY.md):
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Set, Tuple

from .util import D

CATEGORIES: Dict[str, str] = {
    "buy": "Bought with fiat: sets the cost basis",
    "sell": "Sold for fiat: taxable",
    "swap": "Crypto-to-crypto swap, same function: neutral, cost carried over",
    "swap_taxable": "Swap to/from stablecoin/EMT or NFT: taxable",
    "internal_transfer": "Between your own wallets/accounts: neutral, cost carried over",
    "reward": "Reward, staking, airdrop, Earn: income at value on receipt, which becomes its cost",
    "payment": "Paid goods/services with crypto: taxable",
    "fee": "Network or exchange fee",
    "deposit": "Received, not yet classified",
    "withdrawal": "Sent, not yet classified",
    "spam": "Spam / dust / worthless token: ignored",
    "other": "Other",
}


def run(ctx) -> int:
    own: Dict[str, Set[str]] = defaultdict(set)
    for w in ctx.wallets():
        if w["address"]:
            own[w["network"]].add(w["address"].lower())

    linked: Set[str] = {r["tx_hash"].lower() for r in ctx.db.query(
        "SELECT tx_hash FROM movements WHERE tx_hash<>'' AND kind<>'fee'"
        " GROUP BY lower(tx_hash) HAVING COUNT(DISTINCT wallet_id) > 1")}

    rows = ctx.db.query(
        "SELECT m.id, m.wallet_id, w.network, m.tx_hash, m.asset_key, m.amount, m.kind, m.counterparty,"
        " m.category, m.info, COALESCE(a.status,'unmapped') AS status"
        " FROM movements m JOIN wallets w ON w.id=m.wallet_id LEFT JOIN assets a ON a.key=m.asset_key"
        " WHERE m.category_source='auto'")

    legs: Dict[Tuple[int, str], Dict[str, Set[str]]] = defaultdict(lambda: {"in": set(), "out": set()})
    for r in rows:
        if r["tx_hash"] and r["kind"] in ("in", "out") and D(r["amount"]) != 0 and r["status"] not in ("spam", "ignored"):
            legs[(r["wallet_id"], r["tx_hash"])][r["kind"]].add(r["asset_key"])

    updates: List[Tuple[str, int]] = []
    for r in rows:
        cat = _category(r, own, linked, legs)
        if cat != r["category"]:
            updates.append((cat, r["id"]))
    if updates:
        with ctx.db.tx():
            ctx.db.executemany("UPDATE movements SET category=? WHERE id=?", updates)
    return len(updates)


def _category(r, own, linked, legs) -> str:
    if r["kind"] == "fee":
        return "fee"
    if r["status"] in ("spam", "ignored") or D(r["amount"]) == 0:
        return "spam"
    cp = (r["counterparty"] or "").lower()
    if cp and cp in own.get(r["network"], ()):
        return "internal_transfer"
    if r["tx_hash"] and r["tx_hash"].lower() in linked:
        return "internal_transfer"
    leg = legs.get((r["wallet_id"], r["tx_hash"]))
    if leg and leg["in"] and leg["out"] and leg["in"] != leg["out"]:
        return "swap"
    return "deposit" if r["kind"] == "in" else "withdrawal"


def set_category(ctx, movement_id: int, category: str, note: str = None) -> None:
    if category not in CATEGORIES and category != "auto":
        raise ValueError(f"unknown category '{category}'")
    if category == "auto":
        ctx.db.execute("UPDATE movements SET category_source='auto' WHERE id=?", (movement_id,))
        run(ctx)
    else:
        ctx.db.execute("UPDATE movements SET category=?, category_source='user' WHERE id=?",
                       (category, movement_id))
    if note is not None:
        ctx.db.execute("UPDATE movements SET note=? WHERE id=?", (note[:500], movement_id))
