"""Balances and EUR values at 23:59:59 of each fiscal year.

For each wallet the balance at the cutoff comes from the best source available:
  1. manual entry for that cutoff                     (you typed it)
  2. live/scheduled snapshot within the tolerance      (taken by the app at the cutoff)
  3. chain state at the cutoff block                   (EVM archive RPC)
  4. reconstruction from the full transaction history  (sum of movements; exact on Bitcoin
                                                         and for SOL, which carries post-tx balances)
  5. exchange daily snapshot (Binance, 23:59:59 UTC)    (within 1 hour of the cutoff)
When two sources exist they are compared and any difference is flagged.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Dict, List, Optional

from . import assets as assets_mod
from . import ledger
from .adapters import get_adapter
from .models import AdapterError
from .prices import PriceService
from .util import D, ZERO, dstr, now

SOURCE_LABELS = {
    "manual": "manual entry",
    "snapshot": "live snapshot at cutoff",
    "chain_state": "chain state (archive node)",
    "reconstructed": "reconstructed from transactions",
    "post_state": "chain-reported post-tx balance",
    "exchange_daily": "exchange daily snapshot",
    "missing": "no source",
}


class Reading:
    def __init__(self, amounts: Dict[str, Decimal], source: str, flags: Optional[List[str]] = None,
                 asset_flags: Optional[Dict[str, List[str]]] = None, detail: str = ""):
        self.amounts = amounts
        self.source = source
        self.flags = flags or []
        self.asset_flags = asset_flags or {}
        self.detail = detail


def fiscal_years(ctx) -> List[int]:
    """Completed fiscal years to compute, plus the opening year (balances at its start)."""
    cal = ctx.calendar
    last = cal.last_completed_year(now())
    first = ctx.settings.get("first_fiscal_year")
    if not first:
        oldest = ctx.db.scalar(
            "SELECT MIN(t) FROM (SELECT MIN(ts) AS t FROM movements UNION ALL"
            " SELECT MIN(taken_at) FROM snapshots WHERE source IN ('manual','exchange_daily'))")
        first = cal.year_of(int(oldest)) if oldest else last
    first = min(int(first), last)
    return list(range(first, last + 1))


def _differs(a: Decimal, b: Decimal) -> bool:
    tol = max(Decimal("1e-8"), Decimal("1e-6") * max(abs(a), abs(b)))
    return abs(a - b) > tol


def balance_at_cutoff(ctx, wallet: Dict[str, Any], cutoff: int, log=lambda m: None,
                      hidden: Optional[set] = None) -> Reading:
    hidden = hidden or set()
    tol = int(ctx.settings.get("snapshot_tolerance_seconds") or 300)
    wid = wallet["id"]
    network = ctx.networks.get(wallet["network"])
    adapter = get_adapter(ctx, network)

    def clean(amounts: Dict[str, Decimal]) -> Dict[str, Decimal]:
        return {k: v for k, v in amounts.items() if k not in hidden}

    manual = ledger.snapshot_groups(ctx, wid, ["manual"], cutoff, cutoff)
    if manual:
        return Reading(clean(manual[0][2]), "manual")

    near = ledger.snapshot_groups(ctx, wid, ["live", "scheduled"], cutoff - tol, cutoff + tol)
    snap = min(near, key=lambda g: abs(g[0] - cutoff)) if near else None

    chain = None
    cached = ledger.snapshot_groups(ctx, wid, ["chain_state"], cutoff, cutoff)
    if cached:
        chain = cached[0][2]
    elif not ctx.offline and adapter.kind == "chain" and wallet["address"]:
        try:
            keys = ledger.known_asset_keys(ctx, wid)
            chain = adapter.balance_at(wallet, cutoff, keys, log)
        except (AdapterError, Exception) as e:  # never let a check break the computation
            log(f"{wallet['label']}: chain-state read failed: {e}")
            chain = None
        if chain is not None:
            with ctx.db.tx():
                for key, amount in chain.items():
                    ctx.db.execute(
                        "INSERT OR REPLACE INTO snapshots(wallet_id, taken_at, asset_key, amount, source, note)"
                        " VALUES(?,?,?,?,?,?)", (wid, cutoff, key, dstr(amount), "chain_state", "archive RPC"))

    recon = None
    anchored: set = set()
    if adapter.has_history and wallet.get("last_sync_at"):
        recon, anchored = ledger.reconstruct(ctx, wid, cutoff)

    daily = ledger.snapshot_groups(ctx, wid, ["exchange_daily"], cutoff - 3600 - tol, cutoff + 3600 + tol)
    daily_pick = min(daily, key=lambda g: abs(g[0] - cutoff)) if daily else None

    flags: List[str] = []
    asset_flags: Dict[str, List[str]] = {}
    if snap:
        primary, source = snap[2], "snapshot"
        offset = snap[0] - cutoff
        detail = f"snapshot {'+' if offset >= 0 else ''}{offset}s from cutoff"
    elif chain is not None:
        primary, source, detail = chain, "chain_state", "archive RPC at cutoff block"
    elif recon is not None:
        primary = recon
        source = "post_state" if anchored and set(k for k, v in recon.items() if v) <= anchored else "reconstructed"
        detail = ("balance reported by the chain after the last transaction before the cutoff"
                  if source == "post_state" else "sum of all synced movements up to the cutoff")
    elif daily_pick:
        primary, source = daily_pick[2], "exchange_daily"
        offset = daily_pick[0] - cutoff
        detail = f"exchange snapshot {'+' if offset >= 0 else ''}{offset // 60} min from cutoff"
        flags.append(f"🟡 Exchange daily snapshot ({detail}); Spot only: Earn/Funding balances not included")
        detail = ""
    else:
        if adapter.kind == "chain" and not wallet.get("last_sync_at"):
            flags.append("⛔ Wallet never synced")
        elif adapter.kind == "exchange":
            flags.append("⛔ No snapshot at this cutoff: sync within 30 days of it, or add a manual entry")
        else:
            flags.append("⛔ No balance source: add a manual entry")
        return Reading({}, "missing", flags)

    # Cross-checks.
    if source in ("snapshot", "chain_state") and recon is not None:
        for key in sorted(set(primary) | set(recon)):
            if key in hidden:
                continue
            a, b = primary.get(key, ZERO), recon.get(key, ZERO)
            if _differs(a, b):
                asset_flags.setdefault(key, []).append(
                    f"⛔ Mismatch: {SOURCE_LABELS[source]} {dstr(a)} vs transactions {dstr(b)}")
    if source in ("reconstructed", "post_state"):
        for key, v in primary.items():
            if v < 0 and key not in hidden:
                asset_flags.setdefault(key, []).append("⛔ Negative balance: history incomplete")
        if network.l2:
            flags.append("🟡 Rollup fees may be understated by the explorer: verify with a snapshot or archive RPC")
    return Reading(clean(primary), source, flags, asset_flags, detail)


def compute_year(ctx, prices: PriceService, year: int, wallets: Optional[List[Dict[str, Any]]] = None,
                 log=lambda m: None) -> int:
    cal = ctx.calendar
    cutoff = cal.cutoff(year)
    all_assets = assets_mod.all_assets(ctx)
    hidden = {k for k, a in all_assets.items() if assets_mod.is_hidden(a)}
    count = 0
    for w in wallets if wallets is not None else ctx.wallets():
        reading = balance_at_cutoff(ctx, w, cutoff, log, hidden)
        rows = []
        for key, amount in sorted(reading.amounts.items()):
            if amount == 0:
                continue
            asset = all_assets.get(key)
            flags = list(reading.asset_flags.get(key, []))
            pr = prices.price_at(asset, cutoff)
            value = amount * pr.price if pr.price is not None else None
            if pr.price is None:
                flags.append(f"⛔ No EUR price ({pr.source}: {pr.note})" if pr.source != "unmapped"
                             else "⛔ Asset not mapped to a price source (Assets page)")
            elif pr.note:
                flags.append("🟡 " + pr.note)
            rows.append((year, w["id"], key, cutoff, dstr(amount), reading.source, dstr(pr.price),
                         pr.source if pr.price is not None else None, pr.price_ts, dstr(value),
                         json.dumps(flags, ensure_ascii=False), now()))
        meta_flags = list(reading.flags) + ([reading.detail] if reading.detail else [])
        with ctx.db.tx():
            ctx.db.execute("DELETE FROM year_end WHERE fiscal_year=? AND wallet_id=?", (year, w["id"]))
            ctx.db.execute(
                "INSERT INTO year_end(fiscal_year, wallet_id, asset_key, cutoff_ts, amount, balance_source,"
                " flags, computed_at) VALUES(?,?,?,?,?,?,?,?)",
                (year, w["id"], "*", cutoff, "0", reading.source, json.dumps(meta_flags, ensure_ascii=False), now()))
            ctx.db.executemany(
                "INSERT INTO year_end(fiscal_year, wallet_id, asset_key, cutoff_ts, amount, balance_source,"
                " price_eur, price_source, price_ts, value_eur, flags, computed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
        count += len(rows)
    return count


def compute_all(ctx, prices: PriceService, log=lambda m: None) -> List[int]:
    years = fiscal_years(ctx)
    opening = years[0] - 1
    for year in [opening] + years:
        log(f"Year-end {year}: computing balances at {ctx.calendar.iso_local(ctx.calendar.cutoff(year))}")
        compute_year(ctx, prices, year, log=log)
    ctx.db.set_meta("yearend_stale", "")
    ctx.db.set_meta("yearend_computed_at", str(now()))
    return years


def rows_for_year(ctx, year: int) -> List[Dict[str, Any]]:
    rows = ctx.db.query(
        "SELECT y.*, a.symbol, a.name, a.status AS asset_status FROM year_end y"
        " LEFT JOIN assets a ON a.key=y.asset_key WHERE y.fiscal_year=? ORDER BY y.wallet_id, a.symbol", (year,))
    for r in rows:
        r["flags"] = json.loads(r["flags"] or "[]")
    return rows


def wallet_totals(ctx, year: int) -> Dict[int, Dict[str, Any]]:
    """{wallet_id: {"value": Decimal|None, "complete": bool, "source": str, "flags": [...]}}"""
    out: Dict[int, Dict[str, Any]] = {}
    for r in rows_for_year(ctx, year):
        w = out.setdefault(r["wallet_id"], {"value": ZERO, "complete": True, "source": "", "flags": []})
        if r["asset_key"] == "*":
            w["source"] = r["balance_source"]
            w["flags"] = [f for f in r["flags"] if f[:1] in ("⛔", "🟡")]
            if r["balance_source"] == "missing":
                w["complete"] = False
            continue
        if any(f.startswith("⛔") for f in r["flags"]):
            w["flags"].append(f"{r['symbol'] or r['asset_key']}: " + r["flags"][0])
        if r["value_eur"] is None:
            w["complete"] = False
        else:
            w["value"] += D(r["value_eur"])
    return out
