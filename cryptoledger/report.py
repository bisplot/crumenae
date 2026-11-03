"""Year-end report: per-wallet values at the start and end of the fiscal year,
days held, and the files handed over to the tax return (folder
crypto_report_TY<YYYY>/, see docs/REPORT_CONTRACT.md)."""
from __future__ import annotations

import csv
import io
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from . import assets as assets_mod
from . import ledger, yearend
from .adapters import get_adapter
from .prices import PriceService
from .timeutil import iso_utc
from .util import D, ZERO, dstr, now, round_eur


def _value_at(ctx, prices: PriceService, balances: Dict[str, Decimal], ts: int,
              all_assets: Dict[str, Dict]) -> Tuple[Optional[Decimal], List[str]]:
    total = ZERO
    missing: List[str] = []
    for key, amount in balances.items():
        if amount <= 0:
            continue
        a = all_assets.get(key)
        pr = prices.price_at(a, ts)
        if pr.price is None:
            missing.append((a or {}).get("symbol") or key)
        else:
            total += amount * pr.price
    return total, missing


def _year_values(ctx, year: int) -> Dict[int, Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    for r in yearend.rows_for_year(ctx, year):
        w = out.setdefault(r["wallet_id"], {"amounts": {}, "value": ZERO, "complete": True,
                                            "source": "", "flags": []})
        if r["asset_key"] == "*":
            w["source"] = r["balance_source"]
            w["flags"] = r["flags"]
            if r["balance_source"] == "missing":
                w["complete"] = False
            continue
        w["amounts"][r["asset_key"]] = D(r["amount"])
        if r["value_eur"] is None:
            w["complete"] = False
        else:
            w["value"] += D(r["value_eur"])
    return out


def build(ctx, year: int, portfolio: Optional[str] = None, prices: Optional[PriceService] = None) -> Dict[str, Any]:
    prices = prices or PriceService(ctx)
    cal = ctx.calendar
    cutoff, opening = cal.cutoff(year), cal.cutoff(year - 1)
    days_in_year = cal.days_in_year(year)
    ic_rate = D(ctx.settings.get("ic_rate") or "0.002")
    all_assets = assets_mod.all_assets(ctx)
    hidden = {k for k, a in all_assets.items() if assets_mod.is_hidden(a)}
    end_vals, start_vals = _year_values(ctx, year), _year_values(ctx, year - 1)
    wallets_out: List[Dict[str, Any]] = []
    for w in ctx.wallets(portfolio=portfolio):
        end = end_vals.get(w["id"], {"amounts": {}, "value": ZERO, "complete": False, "source": "missing", "flags": []})
        start = start_vals.get(w["id"], {"amounts": {}, "value": ZERO, "complete": False, "source": "missing", "flags": []})
        held_start = any(v > 0 for v in start["amounts"].values())
        held_end = any(v > 0 for v in end["amounts"].values())
        notes: List[str] = []
        adapter = get_adapter(ctx, ctx.networks.get(w["network"]))
        days: Optional[int] = None
        col7: Optional[Decimal] = start["value"] if start["complete"] else None
        col8: Optional[Decimal] = end["value"] if end["complete"] else None
        if adapter.has_history and w.get("last_sync_at"):
            held, eod = ledger.daily_timeline(ctx, w["id"], cal.days_of_year(year), start["amounts"],
                                              opening, cal.end_of_day, hidden)
            days = len(held)
            if held and not held_start:
                first = held[0]
                col7, missing = _value_at(ctx, prices, eod[first], cal.end_of_day(first), all_assets)
                notes.append(f"🟡 Opened during the year: initial value at end of {first.isoformat()}")
                if missing:
                    col7 = None
                    notes.append("⛔ No price for " + ", ".join(missing))
            if held and not held_end:
                last_pos = [d for d in held if any(v > 0 for v in eod[d].values())]
                if last_pos:
                    d = last_pos[-1]
                    col8, missing = _value_at(ctx, prices, eod[d], cal.end_of_day(d), all_assets)
                    notes.append(f"🟡 Emptied during the year: final value at end of {d.isoformat()}")
                    if missing:
                        col8 = None
                        notes.append("⛔ No price for " + ", ".join(missing))
                else:
                    col8 = ZERO
                    notes.append("🟡 Emptied on the day it was funded: final value 0, check")
        else:
            if held_start and held_end:
                days = days_in_year
                if adapter.kind != "chain":
                    notes.append("🟡 No transaction history: assumed held all year")
            elif held_start or held_end:
                notes.append("⛔ Days held unknown (no transaction history): enter them in the return")
            else:
                days = 0
        if not start["complete"] and held_start:
            notes.append("⛔ Initial value incomplete (see opening balances)")
        if not end["complete"] and (held_end or end["source"] == "missing"):
            notes.append("⛔ Final value incomplete (see year-end balances)")
        ic = None
        if col8 is not None and days is not None and not w["bollo_applied"]:
            ic = round_eur(col8 * ic_rate * Decimal(days) / Decimal(days_in_year))
        held_any = bool(days) or held_start or held_end
        wallets_out.append({
            "wallet_id": w["id"], "label": w["label"], "network": w["network"], "address": w["address"],
            "portfolio": w["portfolio"], "rw_slot": w["rw_slot"], "bollo_applied": bool(w["bollo_applied"]),
            "held": held_any, "days_held": days,
            "initial_value_eur": dstr(round_eur(col7)), "final_value_eur": dstr(round_eur(col8)),
            "opening_source": start["source"], "closing_source": end["source"],
            "ic_eur": dstr(ic), "notes": notes + [f for f in end["flags"] if f[:1] in ("⛔", "🟡")],
        })
    held = [w for w in wallets_out if w["held"]]
    return {
        "fiscal_year": year, "timezone": ctx.settings.get("timezone"),
        "opening_cutoff": opening, "closing_cutoff": cutoff,
        "opening_local": cal.iso_local(opening), "closing_local": cal.iso_local(cutoff),
        "days_in_year": days_in_year, "ic_rate": dstr(ic_rate),
        "wallets": wallets_out,
        "totals": {
            "initial_value_eur": dstr(sum((D(w["initial_value_eur"]) for w in held if w["initial_value_eur"]), ZERO)),
            "final_value_eur": dstr(sum((D(w["final_value_eur"]) for w in held if w["final_value_eur"]), ZERO)),
            "ic_eur": dstr(sum((D(w["ic_eur"]) for w in held if w["ic_eur"]), ZERO)),
        },
    }


# --- files -------------------------------------------------------------------------

def _csv(header: List[str], rows: List[List[Any]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    for r in rows:
        writer.writerow(["" if v is None else v for v in r])
    return buf.getvalue()


def balances_csv(ctx, year: int, portfolio: Optional[str] = None) -> str:
    wallets = {w["id"]: w for w in ctx.wallets(portfolio=portfolio)}
    rows = []
    for fy, label in ((year - 1, "opening"), (year, "closing")):
        for r in yearend.rows_for_year(ctx, fy):
            w = wallets.get(r["wallet_id"])
            if not w or r["asset_key"] == "*":
                continue
            rows.append([year, label, iso_utc(r["cutoff_ts"]), w["label"], w["network"], w["portfolio"],
                         w["address"], r["symbol"], r["asset_key"], r["amount"], r["price_eur"], r["value_eur"],
                         r["balance_source"], r["price_source"],
                         iso_utc(r["price_ts"]) if r["price_ts"] else "", " | ".join(r["flags"])])
    return _csv(["fiscal_year", "balance", "cutoff_utc", "wallet", "network", "portfolio", "address", "asset",
                 "asset_key", "quantity", "price_eur", "value_eur", "balance_source", "price_source",
                 "price_time_utc", "flags"], rows)


def rw_csv(report: Dict[str, Any]) -> str:
    rows = []
    for w in sorted(report["wallets"], key=lambda x: (x["rw_slot"] is None, x["rw_slot"] or 0, x["label"])):
        if not w["held"]:
            continue
        rows.append([w["rw_slot"], w["label"], w["network"], w["address"], w["initial_value_eur"],
                     w["final_value_eur"], w["days_held"], 100, 1 if w["bollo_applied"] else 0, w["ic_eur"],
                     " | ".join(w["notes"])])
    return _csv(["slot", "wallet", "network", "address", "col7_initial_value_eur", "col8_final_value_eur",
                 "days_held", "quota_pct", "bollo_applied", "ic_eur", "notes"], rows)


def transactions_csv(ctx, year: int, portfolio: Optional[str] = None) -> str:
    cal = ctx.calendar
    wallets = {w["id"]: w for w in ctx.wallets(portfolio=portfolio)}
    rows = []
    for m in ctx.db.query(
            "SELECT m.*, a.symbol FROM movements m LEFT JOIN assets a ON a.key=m.asset_key"
            " WHERE m.ts BETWEEN ? AND ? ORDER BY m.ts, m.id", (cal.start(year), cal.cutoff(year))):
        w = wallets.get(m["wallet_id"])
        if not w:
            continue
        rows.append([cal.iso_local(m["ts"]), w["label"], w["network"], m["tx_hash"], m["symbol"], m["asset_key"],
                     m["amount"], m["kind"], m["category"], m["counterparty"], m["price_eur"], m["value_eur"],
                     m["price_source"], "failed" if m["failed"] else "", m["note"]])
    return _csv(["time_local", "wallet", "network", "tx_hash", "asset", "asset_key", "amount", "kind", "category",
                 "counterparty", "price_eur", "value_eur", "price_source", "status", "note"], rows)


def evidence_md(ctx, report: Dict[str, Any]) -> str:
    year = report["fiscal_year"]
    lines = [
        f"# Crypto year-end evidence — TY{year}",
        "",
        f"Generated {iso_utc(now())} by CryptoLedger {__version__}.",
        f"Opening balances at {report['opening_local']}, closing balances at {report['closing_local']} "
        f"({report['timezone']}). All values in EUR at the time of each balance.",
        "",
        "## Wallets",
        "",
        "| Slot | Wallet | Network | Opening source | Closing source | Days | Initial € | Final € | IC € |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for w in report["wallets"]:
        if not w["held"]:
            continue
        lines.append(f"| {w['rw_slot'] or ''} | {w['label']} | {w['network']} | "
                     f"{yearend.SOURCE_LABELS.get(w['opening_source'], w['opening_source'])} | "
                     f"{yearend.SOURCE_LABELS.get(w['closing_source'], w['closing_source'])} | "
                     f"{w['days_held'] if w['days_held'] is not None else '?'} | {w['initial_value_eur'] or '?'} | "
                     f"{w['final_value_eur'] or '?'} | {w['ic_eur'] or ''} |")
    lines += ["", "## Prices used at the closing cutoff", "",
              "| Asset | Price € | Source | Quote time (UTC) |", "|---|---|---|---|"]
    seen = set()
    for r in yearend.rows_for_year(ctx, year):
        if r["asset_key"] == "*" or r["symbol"] in seen:
            continue
        seen.add(r["symbol"])
        lines.append(f"| {r['symbol']} | {r['price_eur'] or '—'} | {r['price_source'] or '—'} | "
                     f"{iso_utc(r['price_ts']) if r['price_ts'] else '—'} |")
    flags = [(w["label"], n) for w in report["wallets"] if w["held"] for n in w["notes"]]
    lines += ["", "## Open flags", ""]
    lines += [f"- {label}: {n}" for label, n in flags] or ["- none"]
    syncs = ctx.db.query("SELECT label, network, last_sync_at, last_error FROM wallets ORDER BY id")
    lines += ["", "## Data pulls", ""]
    for s in syncs:
        when = iso_utc(s["last_sync_at"]) if s["last_sync_at"] else "never"
        lines.append(f"- {s['label']} ({s['network']}): last sync {when}" +
                     (f" — last error: {s['last_error']}" if s["last_error"] else ""))
    lines += ["", "Values flagged ⛔ are not reliable and must be checked; 🟡 are estimates to review.", ""]
    return "\n".join(lines)


def write_folder(ctx, year: int, portfolio: Optional[str] = None) -> Path:
    report = build(ctx, year, portfolio)
    folder = ctx.data_dir / "reports" / f"crypto_report_TY{year}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "year_end_balances.csv").write_text(balances_csv(ctx, year, portfolio), encoding="utf-8")
    (folder / "rw_crypto.csv").write_text(rw_csv(report), encoding="utf-8")
    (folder / "transactions.csv").write_text(transactions_csv(ctx, year, portfolio), encoding="utf-8")
    (folder / "evidence.md").write_text(evidence_md(ctx, report), encoding="utf-8")
    (folder / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return folder
