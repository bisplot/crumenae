"""Read-only data for the UI. No network calls here: pages load from the database."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

from . import assets as assets_mod
from . import yearend
from .adapters import ADAPTERS
from .classify import CATEGORIES
from .prices import PriceService
from .util import D, ZERO, dstr


def _wallet_public(ctx, w: Dict[str, Any]) -> Dict[str, Any]:
    from .exports import default_platform
    out = dict(w)
    out["platform_display"] = default_platform(ctx, w)
    out["bollo_applied"] = bool(w["bollo_applied"])
    out["active"] = bool(w["active"])
    try:
        net = ctx.networks.get(w["network"])
        out["network_name"] = net.name
        out["address_url"] = (net.address_url or "").format(address=w["address"]) if w["address"] else ""
        cls = ADAPTERS.get(net.adapter)
        out["credentials"] = {c: bool(ctx.secrets.get(f"wallet:{w['id']}:{c}")) for c in (cls.credentials if cls else [])}
    except KeyError:
        out["network_name"] = w["network"] + " (unknown)"
    return out


def dashboard(ctx, portfolio: Optional[str] = None) -> Dict[str, Any]:
    prices = PriceService(ctx)
    all_assets = assets_mod.all_assets(ctx)
    wallets = ctx.wallets(portfolio=portfolio)
    ids = {w["id"] for w in wallets}
    rows = [r for r in ctx.db.query("SELECT * FROM current_balances") if r["wallet_id"] in ids]

    holdings: Dict[str, Dict[str, Any]] = {}
    per_wallet: Dict[int, Dict[str, Any]] = defaultdict(lambda: {"value": ZERO, "unpriced": 0})
    unpriced: List[Dict[str, Any]] = []
    price_time = None
    for r in rows:
        amount = D(r["amount"])
        if amount == 0:
            continue
        a = all_assets.get(r["asset_key"]) or {"key": r["asset_key"], "symbol": r["asset_key"], "status": "unmapped"}
        if assets_mod.is_hidden(a):
            continue
        pr = prices.latest(a) if assets_mod.is_priced(a) else None
        g = assets_mod.group_key(a)
        h = holdings.setdefault(g, {"symbol": a["symbol"], "name": a.get("name") or a["symbol"], "amount": ZERO,
                                    "value": ZERO, "price": None, "price_source": None, "price_ts": None,
                                    "wallets": [], "priced": True})
        h["amount"] += amount
        h["wallets"].append({"wallet_id": r["wallet_id"], "asset_key": r["asset_key"], "amount": dstr(amount)})
        if pr and pr.price is not None:
            value = amount * pr.price
            h["value"] += value
            h["price"], h["price_source"], h["price_ts"] = dstr(pr.price), pr.source, pr.price_ts
            per_wallet[r["wallet_id"]]["value"] += value
            price_time = max(price_time or 0, pr.price_ts or 0)
        else:
            h["priced"] = False
            per_wallet[r["wallet_id"]]["unpriced"] += 1
            unpriced.append({"asset_key": r["asset_key"], "symbol": a["symbol"], "amount": dstr(amount),
                             "status": a.get("status")})
    total = sum((h["value"] for h in holdings.values()), ZERO)
    holding_list = sorted(holdings.values(), key=lambda h: h["value"], reverse=True)
    for h in holding_list:
        h["share"] = float(h["value"] / total) if total else 0.0
        h["amount"], h["value"] = dstr(h["amount"]), dstr(h["value"])

    years = sorted({r["fiscal_year"] for r in ctx.db.query("SELECT DISTINCT fiscal_year FROM year_end")})
    series = []
    year_values: Dict[int, Dict[int, Dict[str, Any]]] = {}
    for y in years:
        totals = yearend.wallet_totals(ctx, y)
        year_values[y] = totals
        vals = [t for wid, t in totals.items() if wid in ids]
        series.append({"year": y, "value": dstr(sum((t["value"] for t in vals), ZERO)),
                       "complete": all(t["complete"] for t in vals)})
    last_year = years[-1] if years else None

    wallet_list = []
    for w in wallets:
        pw = per_wallet.get(w["id"], {"value": ZERO, "unpriced": 0})
        ye = year_values.get(last_year, {}).get(w["id"]) if last_year else None
        item = _wallet_public(ctx, w)
        item.update(value_now=dstr(pw["value"]), unpriced=pw["unpriced"],
                    value_last_year_end=dstr(ye["value"]) if ye else None,
                    last_year_complete=ye["complete"] if ye else None,
                    last_year_flags=ye["flags"] if ye else [])
        wallet_list.append(item)

    flags = []
    if wallets and not years:
        flags.append({"level": "warn", "text": "Year-end values not computed yet: press \"Recompute year-end\""})
    elif ctx.yearend_stale():
        flags.append({"level": "warn", "text": f"Year-end values out of date ({ctx.yearend_stale()}): "
                                               "press \"Recompute year-end\""})
    for w in wallets:
        if w["last_error"]:
            flags.append({"level": "error", "text": f"{w['label']}: {w['last_error']}"})
        elif w["active"] and not w["last_sync_at"] and w["network"] != "manual":
            flags.append({"level": "warn", "text": f"{w['label']}: not synced yet"})
    if unpriced:
        flags.append({"level": "warn", "text": f"{len(unpriced)} held asset(s) without a price: map them on the Assets page"})
    for item in wallet_list:
        for f in item["last_year_flags"]:
            flags.append({"level": "error" if "⛔" in f else "warn",
                          "text": f"{item['label']} {last_year}: {f}"})
    return {
        "total_now": dstr(total), "price_time": price_time, "holdings": holding_list,
        "wallets": wallet_list, "series": series, "last_year": last_year, "flags": flags[:50],
        "unpriced": unpriced, "portfolios": portfolios(ctx),
        "last_sync": ctx.db.scalar("SELECT MAX(last_sync_at) FROM wallets"),
        "demo": ctx.db.get_meta("demo") == "1",
    }


def portfolios(ctx) -> List[str]:
    return [r["portfolio"] for r in ctx.db.query("SELECT DISTINCT portfolio FROM wallets ORDER BY portfolio")]


def wallets(ctx) -> List[Dict[str, Any]]:
    return [_wallet_public(ctx, w) for w in ctx.wallets()]


def year_detail(ctx, year: int, portfolio: Optional[str] = None) -> Dict[str, Any]:
    ids = {w["id"]: w for w in ctx.wallets(portfolio=portfolio)}
    by_wallet: Dict[int, Dict[str, Any]] = {}
    for r in yearend.rows_for_year(ctx, year):
        if r["wallet_id"] not in ids:
            continue
        w = by_wallet.setdefault(r["wallet_id"], {"wallet": _wallet_public(ctx, ids[r["wallet_id"]]),
                                                  "assets": [], "source": "", "flags": []})
        if r["asset_key"] == "*":
            w["source"] = r["balance_source"]
            w["source_label"] = yearend.SOURCE_LABELS.get(r["balance_source"], r["balance_source"])
            w["flags"] = r["flags"]
            continue
        w["assets"].append({k: r[k] for k in ("asset_key", "symbol", "name", "amount", "price_eur", "price_source",
                                               "price_ts", "value_eur", "flags", "asset_status")})
    for w in by_wallet.values():
        w["value"] = dstr(sum((D(a["value_eur"]) for a in w["assets"] if a["value_eur"]), ZERO))
        w["complete"] = w["source"] != "missing" and all(a["value_eur"] is not None for a in w["assets"])
    cal = ctx.calendar
    return {"year": year, "cutoff": cal.cutoff(year), "cutoff_local": cal.iso_local(cal.cutoff(year)),
            "timezone": cal.tz_name, "wallets": list(by_wallet.values()),
            "years": [r["fiscal_year"] for r in ctx.db.query(
                "SELECT DISTINCT fiscal_year FROM year_end ORDER BY fiscal_year")]}


def transactions(ctx, wallet_id: Optional[int] = None, year: Optional[int] = None,
                 category: Optional[str] = None, q: Optional[str] = None, portfolio: Optional[str] = None,
                 limit: int = 200, offset: int = 0) -> Dict[str, Any]:
    sql = (" FROM movements m JOIN wallets w ON w.id=m.wallet_id LEFT JOIN assets a ON a.key=m.asset_key"
           " WHERE 1=1")
    params: List[Any] = []
    if wallet_id:
        sql += " AND m.wallet_id=?"
        params.append(wallet_id)
    if portfolio:
        sql += " AND w.portfolio=?"
        params.append(portfolio)
    if year:
        cal = ctx.calendar
        sql += " AND m.ts BETWEEN ? AND ?"
        params += [cal.start(year), cal.cutoff(year)]
    if category == "all":
        pass                                   # everything, spam included
    elif category:
        sql += " AND m.category=?"
        params.append(category)
    else:
        sql += " AND COALESCE(m.category,'') <> 'spam'"   # spam hidden by default
    if q:
        sql += " AND (m.tx_hash LIKE ? OR m.counterparty LIKE ? OR a.symbol LIKE ? OR m.note LIKE ?)"
        params += [f"%{q}%"] * 4
    total = ctx.db.scalar("SELECT COUNT(*)" + sql, params)
    rows = ctx.db.query(
        "SELECT m.*, w.label AS wallet_label, w.network, a.symbol, a.status AS asset_status" + sql +
        " ORDER BY m.ts DESC, m.id DESC LIMIT ? OFFSET ?", params + [min(limit, 1000), offset])
    cal = ctx.calendar
    for r in rows:
        r["time_local"] = cal.iso_local(r["ts"])
        try:
            r["tx_url"] = (ctx.networks.get(r["network"]).tx_url or "").format(tx=r["tx_hash"]) if r["tx_hash"] else ""
        except KeyError:
            r["tx_url"] = ""
    return {"total": total, "rows": rows, "categories": CATEGORIES}


def asset_list(ctx) -> List[Dict[str, Any]]:
    rows = ctx.db.query(
        "SELECT a.*, (SELECT COUNT(*) FROM movements m WHERE m.asset_key=a.key) AS movements,"
        " (SELECT GROUP_CONCAT(c.amount) FROM current_balances c WHERE c.asset_key=a.key) AS held"
        " FROM assets a ORDER BY (a.status='unmapped') DESC, a.network, a.symbol")
    for r in rows:
        r["held"] = dstr(sum((D(x) for x in (r["held"] or "").split(",") if x), ZERO))
    return rows


def staking(ctx, year: int, portfolio: Optional[str] = None) -> Dict[str, Any]:
    """Staking page: positions (wallets marked as staking, valued at the year-end like any
    wallet, so they are part of the RW) and rewards received during the fiscal year."""
    from .adapters import get_adapter
    detail = year_detail(ctx, year, portfolio)
    by_id = {w["wallet"]["id"]: w for w in detail["wallets"]}
    positions = []
    manual_wallets = []
    for w in ctx.wallets(portfolio=portfolio):
        try:
            kind = get_adapter(ctx, ctx.networks.get(w["network"])).kind
        except KeyError:
            kind = ""
        if kind == "manual":
            manual_wallets.append({"id": w["id"], "label": w["label"], "staking": bool(w["staking"])})
        earn_raw = ctx.db.get_meta(f"earn_positions:{w['id']}") if w["network"] == "binance" else None
        if earn_raw:
            import json
            earn = json.loads(earn_raw)
            if earn.get("positions"):
                positions.append({
                    "wallet": _wallet_public(ctx, w), "assets": [], "source": "", "flags": [], "value": None,
                    "complete": False, "manual": False, "auto": "Binance Simple Earn",
                    "current": [{"asset_key": f"binance:{t}", "symbol": t, "amount": a}
                                for t, a in earn["positions"].items()],
                    "current_at": earn.get("at")})
        if not w["staking"]:
            continue
        item = by_id.get(w["id"]) or {"wallet": _wallet_public(ctx, w), "assets": [], "source": "",
                                      "flags": [], "value": None, "complete": False}
        item["manual"] = kind == "manual"
        item["current"] = [{"asset_key": r["asset_key"], "symbol": r["symbol"] or r["asset_key"], "amount": r["amount"]}
                           for r in ctx.db.query(
                               "SELECT c.asset_key, c.amount, a.symbol FROM current_balances c"
                               " LEFT JOIN assets a ON a.key=c.asset_key WHERE c.wallet_id=? AND c.amount<>'0'",
                               (w["id"],))]
        positions.append(item)

    cal = ctx.calendar
    sql = ("SELECT m.id, m.uid, m.ts, m.amount, m.asset_key, m.value_eur, m.price_source, m.note, m.tx_hash,"
           " w.id AS wallet_id, w.label AS wallet_label, w.network, a.symbol"
           " FROM movements m JOIN wallets w ON w.id=m.wallet_id LEFT JOIN assets a ON a.key=m.asset_key"
           " WHERE m.category='reward' AND m.failed=0 AND m.ts BETWEEN ? AND ?")
    params: List[Any] = [cal.start(year), cal.cutoff(year)]
    if portfolio:
        sql += " AND w.portfolio=?"
        params.append(portfolio)
    rewards = ctx.db.query(sql + " ORDER BY m.ts DESC, m.id DESC", params)
    totals: Dict[str, Dict[str, Any]] = {}
    total_eur = ZERO
    unvalued = 0
    for r in rewards:
        r["time_local"] = cal.iso_local(r["ts"])
        r["manual"] = r["uid"].startswith("manual-reward:")
        t = totals.setdefault(r["asset_key"], {"asset_key": r["asset_key"], "symbol": r["symbol"] or r["asset_key"],
                                               "amount": ZERO, "value_eur": ZERO, "count": 0})
        t["amount"] += D(r["amount"])
        t["count"] += 1
        if r["value_eur"] is not None:
            t["value_eur"] += D(r["value_eur"])
            total_eur += D(r["value_eur"])
        else:
            unvalued += 1
    staked_eur = sum((D(p["value"]) for p in positions if p.get("value")), ZERO)
    return {
        "year": year, "cutoff_local": detail["cutoff_local"], "years": detail["years"],
        "positions": positions, "staked_value_eur": dstr(staked_eur),
        "rewards": rewards, "reward_totals": [dict(t, amount=dstr(t["amount"]), value_eur=dstr(t["value_eur"]))
                                              for t in sorted(totals.values(), key=lambda t: -t["value_eur"])],
        "rewards_value_eur": dstr(total_eur), "rewards_unvalued": unvalued,
        "manual_wallets": manual_wallets,
    }
