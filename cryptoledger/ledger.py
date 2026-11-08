"""Writing sync results and reading balances back from the ledger."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from . import assets as assets_mod
from .models import StakePosition, SyncResult
from .util import D, ZERO, dstr


def store_sync_result(ctx, wallet: Dict[str, Any], res: SyncResult) -> int:
    assets_mod.upsert(ctx, res.assets)
    inserted = 0
    with ctx.db.tx():
        for m in res.movements:
            cur = ctx.db.execute(
                "INSERT OR IGNORE INTO movements(wallet_id, uid, tx_hash, ts, asset_key, amount, kind,"
                " counterparty, from_address, to_address, failed, balance_after, info)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (wallet["id"], m.uid, m.tx_hash or "", int(m.ts), m.asset_key, dstr(m.amount), m.kind,
                 m.counterparty or "", m.from_address or "", m.to_address or "", 1 if m.failed else 0,
                 dstr(m.balance_after), m.info or ""))
            inserted += cur.rowcount
            if not cur.rowcount and (m.from_address or m.to_address):
                ctx.db.execute(
                    "UPDATE movements SET from_address=?, to_address=? WHERE wallet_id=? AND uid=?"
                    " AND from_address='' AND to_address=''",
                    (m.from_address or "", m.to_address or "", wallet["id"], m.uid))
        for s in res.snapshots:
            ctx.db.execute(
                "INSERT OR REPLACE INTO snapshots(wallet_id, taken_at, asset_key, amount, source, note)"
                " VALUES(?,?,?,?,?,?)",
                (wallet["id"], int(s.taken_at), s.asset_key, dstr(s.amount), s.source, s.note))
        ctx.save_sync_state(wallet["id"], res.state)
    return inserted


def store_current(ctx, wallet_id: int, balances: Dict[str, Decimal], ts: int,
                  snapshot_source: Optional[str] = "live") -> None:
    with ctx.db.tx():
        ctx.db.execute("DELETE FROM current_balances WHERE wallet_id=?", (wallet_id,))
        for key, amount in balances.items():
            ctx.db.execute("INSERT INTO current_balances(wallet_id, asset_key, amount, fetched_at)"
                           " VALUES(?,?,?,?)", (wallet_id, key, dstr(amount), ts))
            if snapshot_source:
                ctx.db.execute(
                    "INSERT OR REPLACE INTO snapshots(wallet_id, taken_at, asset_key, amount, source, note)"
                    " VALUES(?,?,?,?,?,?)", (wallet_id, ts, key, dstr(amount), snapshot_source, ""))


def store_staking(ctx, wallet_id: int, positions: List[StakePosition], ts: int) -> None:
    """The wallet's staking positions as read now (replaces the previous reading)."""
    with ctx.db.tx():
        ctx.db.execute("DELETE FROM staking_positions WHERE wallet_id=?", (wallet_id,))
        for p in positions:
            if not (p.staked or p.unbonding or p.pending_rewards):
                continue
            ctx.db.execute(
                "INSERT INTO staking_positions(wallet_id, asset_key, protocol, validator, staked, unbonding,"
                " pending_rewards, fetched_at) VALUES(?,?,?,?,?,?,?,?)",
                (wallet_id, p.asset_key, p.protocol[:80], p.validator[:120], dstr(p.staked), dstr(p.unbonding),
                 dstr(p.pending_rewards), ts))


def known_asset_keys(ctx, wallet_id: int) -> List[str]:
    rows = ctx.db.query(
        "SELECT DISTINCT m.asset_key AS k FROM movements m LEFT JOIN assets a ON a.key=m.asset_key"
        " WHERE m.wallet_id=? AND COALESCE(a.status,'') NOT IN ('spam','ignored')"
        " UNION SELECT asset_key FROM current_balances WHERE wallet_id=?", (wallet_id, wallet_id))
    return [r["k"] for r in rows]


def movements(ctx, wallet_id: int, until_ts: Optional[int] = None,
              since_ts: Optional[int] = None) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM movements WHERE wallet_id=?"
    params: List[Any] = [wallet_id]
    if since_ts is not None:
        sql += " AND ts>?"
        params.append(since_ts)
    if until_ts is not None:
        sql += " AND ts<=?"
        params.append(until_ts)
    return ctx.db.query(sql + " ORDER BY ts, id", params)


def apply(balances: Dict[str, Decimal], m: Dict[str, Any]) -> None:
    """Apply one movement row; an absolute post-tx balance wins over the sum."""
    if m.get("balance_after") is not None:
        balances[m["asset_key"]] = D(m["balance_after"])
    else:
        balances[m["asset_key"]] = balances.get(m["asset_key"], ZERO) + D(m["amount"])


def reconstruct(ctx, wallet_id: int, ts: int) -> Tuple[Dict[str, Decimal], Set[str]]:
    """Balances at `ts` from the ledger; also returns assets anchored to chain-reported post balances."""
    balances: Dict[str, Decimal] = {}
    anchored: Set[str] = set()
    for m in movements(ctx, wallet_id, until_ts=ts):
        apply(balances, m)
        if m.get("balance_after") is not None:
            anchored.add(m["asset_key"])
    return balances, anchored


def snapshot_groups(ctx, wallet_id: int, sources: Iterable[str], lo: int, hi: int) -> List[Tuple[int, str, Dict[str, Decimal]]]:
    """Snapshots in [lo, hi], grouped by (taken_at, source)."""
    src = list(sources)
    marks = ",".join("?" * len(src))
    rows = ctx.db.query(
        f"SELECT taken_at, source, asset_key, amount FROM snapshots WHERE wallet_id=? AND source IN ({marks})"
        " AND taken_at BETWEEN ? AND ? ORDER BY taken_at", [wallet_id] + src + [lo, hi])
    groups: Dict[Tuple[int, str], Dict[str, Decimal]] = {}
    for r in rows:
        groups.setdefault((r["taken_at"], r["source"]), {})[r["asset_key"]] = D(r["amount"])
    return [(t, s, g) for (t, s), g in groups.items()]


def daily_timeline(ctx, wallet_id: int, days: List[date], start_balances: Dict[str, Decimal],
                   start_ts: int, end_of_day, hidden: Set[str]):
    """Walk the fiscal year day by day.

    Returns (held_days, end_of_day_balances) where held_days are the local dates on
    which the wallet held a positive balance at any moment, and end_of_day_balances
    maps each date to the balances at 23:59:59 of that date.
    """
    bal = {k: v for k, v in start_balances.items() if k not in hidden}
    moves = [m for m in movements(ctx, wallet_id, since_ts=start_ts, until_ts=end_of_day(days[-1]))
             if m["asset_key"] not in hidden]
    i = 0
    held: List[date] = []
    eod: Dict[date, Dict[str, Decimal]] = {}

    def positive() -> bool:
        return any(v > 0 for v in bal.values())

    for d in days:
        limit = end_of_day(d)
        any_pos = positive()
        while i < len(moves) and moves[i]["ts"] <= limit:
            apply(bal, moves[i])
            i += 1
            any_pos = any_pos or positive()
        if any_pos:
            held.append(d)
        eod[d] = dict(bal)
    return held, eod
