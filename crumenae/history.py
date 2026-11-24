"""Portfolio value over time: the EUR value of each wallet at the end of every day, for the
Dashboard chart.

Quantities
  * wallets with a full history (most chains, Coinbase): rebuilt from the transactions, day by
    day, as the year-end does (ledger.daily_timeline);
  * the others (Terra, Young Platform, manual accounts…): the balance recorded at each sync or
    entered by hand, held until the next one. Days before the first one are not covered: the
    chart marks them "partial".
Prices
  * one daily close per coin, fetched in ranges (Binance: 1,000 days per request; CoinGecko: one
    range request, last 365 days without a Pro key; ECB rates for USD and other fiat in one
    request) and kept in the price cache under "d:<identity>", so later runs only fetch new days;
  * today: the latest live price, so the chart ends on the Dashboard total;
  * ghost coins count as EUR 0; spam and ignored assets are left out; a held coin without a price
    that day is left out of the total and the day is marked "unpriced".
Daily closes are UTC (the exchanges' daily candles); the year-end page keeps the exact cutoff.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from . import assets as assets_mod
from . import ledger, yearend
from .adapters import get_adapter
from .models import AdapterError
from .prices import PriceService, identity
from .util import ZERO, D, dstr, now

Logger = Callable[[str], None]


def _span(start: date, end: date) -> List[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _utc_ts(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


# --- daily prices ----------------------------------------------------------------------------
def daily_prices(ctx, prices: PriceService, asset: Dict[str, Any], days: List[date],
                 today: date) -> Dict[date, Decimal]:
    """EUR price of an asset for each of `days` that has one (today: the latest live price)."""
    if not days:
        return {}
    if asset.get("status") == "ghost":
        return {d: ZERO for d in days}
    ident = identity(asset)
    if not ident:
        return {}
    if ident == "fiat:EUR":
        return {d: Decimal(1) for d in days}
    out: Dict[date, Decimal] = {}
    past = [d for d in days if d < today]
    if past:
        key = "d:" + ident
        start, end = min(past), max(past)
        rows = ctx.db.query("SELECT ts, price_eur FROM prices WHERE price_key=? AND ts BETWEEN ? AND ?",
                            (key, _utc_ts(start), _utc_ts(end)))
        cached = {datetime.fromtimestamp(r["ts"], tz=timezone.utc).date(): D(r["price_eur"]) for r in rows}
        checked = ctx.db.get_meta(f"daily_checked:{ident}") or ""
        todo = [d for d in past if d not in cached and d.isoformat() > checked]
        if todo:
            found, source = _fetch_daily(ctx, prices, asset, min(todo), max(todo))
            if found:
                with ctx.db.tx():
                    for d, p in found.items():
                        ctx.db.execute("INSERT OR REPLACE INTO prices(price_key, ts, price_eur, source, price_ts, note,"
                                       " fetched_at) VALUES(?,?,?,?,?,?,?)",
                                       (key, _utc_ts(d), dstr(p), source, _utc_ts(d) + 86399, "daily close", now()))
                cached.update(found)
            if not ctx.offline and found is not None:
                ctx.db.set_meta(f"daily_checked:{ident}", max(checked, max(todo).isoformat()))
        out.update({d: cached[d] for d in past if d in cached})
    if today in days:
        latest = prices.latest(asset)
        if latest and latest.price is not None:
            out[today] = latest.price
    return out


def _fetch_daily(ctx, prices: PriceService, asset: Dict[str, Any], start: date,
                 end: date) -> Tuple[Optional[Dict[date, Decimal]], str]:
    """(prices found, source) for [start, end]; None when nothing could be asked (offline, error)."""
    if asset.get("fiat"):
        if ctx.offline:
            return None, ""
        try:
            rates = prices.fx.rates_range(asset["fiat"], start, end)
        except AdapterError as e:
            prices.errors.append(str(e))
            return None, ""
        return {d: Decimal(1) / r for d, r in rates.items() if r}, f"ECB {asset['fiat']}/EUR"
    if prices.demo is not None:
        got = {}
        for d in _span(start, end):
            r = prices.demo.price_at(asset, _utc_ts(d) + 86399)
            if r and r.price is not None:
                got[d] = r.price
        return got, "DEMO synthetic price"
    if ctx.offline:
        return None, ""
    found: Dict[date, Decimal] = {}
    sources: List[str] = []
    asked = False
    for name in ctx.settings.get("historical_price_providers") or []:
        provider = prices.providers.get(name)
        if not provider or not hasattr(provider, "daily_closes") or not provider.can_price(asset):
            continue
        missing = [d for d in _span(start, end) if d not in found]
        if not missing:
            break
        try:
            got, source = provider.daily_closes(asset, min(missing), max(missing))
            asked = True
        except AdapterError as e:
            prices.errors.append(str(e))
            continue
        new = {d: p for d, p in got.items() if d not in found}
        if new:
            found.update(new)
            sources.append(source)
    return (found if asked or found else None), " + ".join(sources)


# --- quantities ------------------------------------------------------------------------------
def _from_history(ctx, wallet: Dict[str, Any], today: date, hidden: Set[str]) -> Dict[date, Dict[str, Decimal]]:
    first = ctx.db.scalar("SELECT MIN(ts) FROM movements WHERE wallet_id=?", (wallet["id"],))
    if first is None:
        return {}
    cal = ctx.calendar
    days = _span(cal.local_date(first), today)
    _, eod = ledger.daily_timeline(ctx, wallet["id"], days, {}, first - 1, cal.end_of_day, hidden)
    return eod


SNAPSHOT_GRACE = 3600   # an exchange's daily snapshot at 23:59:59 UTC is 00:59:59 in Rome: still that day


def _from_snapshots(ctx, wallet: Dict[str, Any], today: date, hidden: Set[str]) -> Dict[date, Dict[str, Decimal]]:
    """Balances recorded at syncs or entered by hand, each held until the next one (a snapshot up
    to an hour after midnight counts for the day before, as the year-end allows)."""
    rows = ctx.db.query("SELECT taken_at, asset_key, amount FROM snapshots WHERE wallet_id=? ORDER BY taken_at",
                        (wallet["id"],))
    groups: Dict[int, Dict[str, Decimal]] = {}
    for r in rows:
        if r["asset_key"] not in hidden:
            groups.setdefault(r["taken_at"], {})[r["asset_key"]] = D(r["amount"])
    current = ctx.db.query("SELECT asset_key, amount, fetched_at FROM current_balances WHERE wallet_id=?",
                           (wallet["id"],))
    if current:
        t = max(r["fetched_at"] or 0 for r in current)
        groups[t] = {r["asset_key"]: D(r["amount"]) for r in current if r["asset_key"] not in hidden}
    if not groups:
        return {}
    cal = ctx.calendar
    marks = sorted(groups)
    out: Dict[date, Dict[str, Decimal]] = {}
    i, held = 0, None
    for d in _span(cal.local_date(marks[0] - SNAPSHOT_GRACE), today):
        limit = cal.end_of_day(d) + SNAPSHOT_GRACE
        while i < len(marks) and marks[i] <= limit:
            held = groups[marks[i]]
            i += 1
        if held is not None:
            out[d] = dict(held)
    return out


def _full_history(ctx, wallet: Dict[str, Any]) -> bool:
    try:
        adapter = get_adapter(ctx, ctx.networks.get(wallet["network"]))
    except (KeyError, AdapterError):
        return False
    return adapter.kind != "manual" and adapter.has_history and not yearend._history_missing(ctx, wallet["id"])


# --- compute and read ------------------------------------------------------------------------
def compute(ctx, prices: Optional[PriceService] = None, log: Logger = lambda m: None) -> int:
    """Rebuild the daily value of every wallet; returns the number of days covered."""
    prices = prices or PriceService(ctx)
    all_assets = assets_mod.all_assets(ctx)
    hidden = {k for k, a in all_assets.items() if assets_mod.is_hidden(a)}
    today = ctx.calendar.local_date(now())
    balances: Dict[int, Dict[date, Dict[str, Decimal]]] = {}
    recorded: Dict[str, Optional[str]] = {}    # wallets without a full history -> first day covered
    for w in ctx.wallets():
        if _full_history(ctx, w):
            eod = _from_history(ctx, w, today, hidden)
        else:
            eod = _from_snapshots(ctx, w, today, hidden)
            recorded[str(w["id"])] = min(eod).isoformat() if eod else None
        if eod:
            balances[w["id"]] = eod
    held: Dict[str, Set[date]] = {}
    for eod in balances.values():
        for d, bal in eod.items():
            for k, v in bal.items():
                if v and k in all_assets:
                    held.setdefault(k, set()).add(d)
    # One price series per price identity (the same coin on several chains), over all its days.
    groups: Dict[str, List[str]] = {}
    for k in held:
        a = all_assets[k]
        groups.setdefault("ghost:" + k if a.get("status") == "ghost" else identity(a) or k, []).append(k)
    price_of: Dict[str, Dict[date, Decimal]] = {}
    for keys in groups.values():
        days = sorted(set().union(*(held[k] for k in keys)))
        series_ = daily_prices(ctx, prices, all_assets[keys[0]], days, today)
        for k in keys:
            price_of[k] = series_
    rows: List[Tuple[int, str, str, int]] = []
    for wid, eod in balances.items():
        for d, bal in eod.items():
            value, unpriced = ZERO, 0
            for k, v in bal.items():
                if not v or k not in all_assets:
                    continue
                p = price_of.get(k, {}).get(d)
                if p is None:
                    unpriced += 1
                else:
                    value += v * p
            rows.append((wid, d.isoformat(), dstr(value), unpriced))
    with ctx.db.tx():
        ctx.db.execute("DELETE FROM value_history")
        ctx.db.conn.executemany("INSERT INTO value_history(wallet_id, day, value_eur, unpriced) VALUES(?,?,?,?)", rows)
    ctx.db.set_meta("value_history_at", str(now()))
    ctx.db.set_meta("value_history_recorded", json.dumps(recorded))
    days = len({r[1] for r in rows})
    log(f"Value over time: {days} days, {len(balances)} wallet(s)")
    return days


def series(ctx, portfolio: Optional[str] = None) -> Dict[str, Any]:
    """Daily totals for the chart: [{day, value, unpriced, partial}], lost wallets left out (as on
    the Dashboard). "partial": a wallet without a full history has no recorded balance yet that
    day (its value is missing from the total); "uncovered": such wallets with none at all."""
    wallets = [w for w in ctx.wallets(portfolio=portfolio) if not w["lost"]]
    ids = [w["id"] for w in wallets]
    at = ctx.db.get_meta("value_history_at")
    if not ids:
        return {"points": [], "computed_at": int(at) if at else None, "uncovered": []}
    try:
        recorded = json.loads(ctx.db.get_meta("value_history_recorded") or "{}")
    except ValueError:
        recorded = {}
    starts = [recorded[str(i)] for i in ids if recorded.get(str(i))]
    covered_from = max(starts) if starts else None
    marks = ",".join("?" * len(ids))
    rows = ctx.db.query(f"SELECT day, value_eur, unpriced FROM value_history WHERE wallet_id IN ({marks})"
                        " ORDER BY day", ids)
    days: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        p = days.setdefault(r["day"], {"value": ZERO, "unpriced": 0})
        p["value"] += D(r["value_eur"])
        p["unpriced"] += r["unpriced"]
    points = [{"day": d, "value": dstr(p["value"]), "unpriced": p["unpriced"],
               "partial": bool(covered_from and d < covered_from)} for d, p in sorted(days.items())]
    return {"points": points, "computed_at": int(at) if at else None,
            "uncovered": [w["label"] for w in wallets if str(w["id"]) in recorded and not recorded[str(w["id"])]]}
