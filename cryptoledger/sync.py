"""Sync orchestration and background jobs."""
from __future__ import annotations

import threading
import traceback
from typing import Any, Callable, Dict, List, Optional

from . import assets as assets_mod
from . import classify, ledger, yearend
from .adapters import get_adapter
from .http import HttpError
from .models import AdapterError
from .prices import PriceService
from .util import D, dstr, now

MAX_VALUATIONS_PER_RUN = 3000
MAX_CONTRACT_LOOKUPS_PER_RUN = 25


def sync_wallet(ctx, wallet: Dict[str, Any], log: Callable[[str], None], full: bool = False) -> None:
    network = ctx.networks.get(wallet["network"])
    adapter = get_adapter(ctx, network)
    state = {} if full else ctx.sync_state(wallet["id"])
    res = adapter.sync(wallet, state, log)
    added = ledger.store_sync_result(ctx, wallet, res)
    for warning in res.warnings:
        log(f"{wallet['label']}: ⚠ {warning}")
        ctx.log(warning, "warn", wallet["id"])
    balances, infos = adapter.current_balances(wallet, ledger.known_asset_keys(ctx, wallet["id"]), log)
    assets_mod.upsert(ctx, infos)
    ledger.store_current(ctx, wallet["id"], balances, now())
    ctx.db.execute("UPDATE wallets SET last_sync_at=?, last_error=NULL WHERE id=?", (now(), wallet["id"]))
    log(f"{wallet['label']}: {added} new movements, {sum(1 for v in balances.values() if v)} assets held now")


def run_sync(ctx, log: Callable[[str], None], wallet_ids: Optional[List[int]] = None,
             full: bool = False) -> Dict[str, Any]:
    prices = PriceService(ctx)
    wallets = [w for w in ctx.wallets(active_only=True) if not wallet_ids or w["id"] in wallet_ids]
    errors = 0
    if ctx.offline:
        log("Offline/demo mode: external APIs are not called.")
    else:
        for w in wallets:
            try:
                sync_wallet(ctx, w, log, full)
            except (AdapterError, HttpError, ValueError, KeyError) as e:
                errors += 1
                ctx.db.execute("UPDATE wallets SET last_error=? WHERE id=?", (str(e)[:500], w["id"]))
                ctx.log(str(e), "error", w["id"])
                log(f"{w['label']}: ✖ {e}")
            except Exception as e:  # keep going with the other wallets
                errors += 1
                ctx.db.execute("UPDATE wallets SET last_error=? WHERE id=?", (f"internal error: {e}"[:500], w["id"]))
                ctx.log(traceback.format_exc(), "error", w["id"])
                log(f"{w['label']}: ✖ internal error: {e}")
        lookup_unknown_tokens(ctx, prices, log)
    changed = classify.run(ctx)
    log(f"Classification: {changed} movements updated")
    years = yearend.compute_all(ctx, prices, log)
    refresh_prices(ctx, prices, log)
    if ctx.settings.get("value_transactions"):
        value_movements(ctx, prices, log)
    for e in prices.errors[:5]:
        log(f"price provider: {e}")
    log(f"Done. Fiscal years {years[0]}–{years[-1]}; {errors} wallet error(s).")
    return {"errors": errors, "years": years}


def refresh_prices(ctx, prices: PriceService, log: Callable[[str], None]) -> int:
    rows = ctx.db.query(
        "SELECT DISTINCT a.* FROM current_balances c JOIN assets a ON a.key=c.asset_key"
        " WHERE CAST(c.amount AS REAL) != 0")
    got = prices.refresh_current({r["key"]: r for r in rows})
    log(f"Live prices: {len(got)}/{len(rows)} assets priced")
    return len(got)


def lookup_unknown_tokens(ctx, prices: PriceService, log: Callable[[str], None]) -> None:
    """Map unknown tokens to CoinGecko by contract address (reliable, unlike tickers)."""
    rows = ctx.db.query(
        "SELECT a.* FROM assets a WHERE a.status='unmapped' AND a.contract IS NOT NULL"
        " AND EXISTS (SELECT 1 FROM current_balances c WHERE c.asset_key=a.key AND CAST(c.amount AS REAL) != 0)")
    done = 0
    for a in rows:
        if done >= MAX_CONTRACT_LOOKUPS_PER_RUN:
            break
        marker = f"cg_lookup:{a['key']}"
        if ctx.db.get_meta(marker):
            continue
        try:
            platform = ctx.networks.get(a["network"]).coingecko_platform
        except KeyError:
            continue
        if not platform:
            continue
        done += 1
        try:
            found = prices.coingecko.lookup_contract(platform, a["contract"])
        except AdapterError as e:
            log(f"CoinGecko lookup {a['symbol']}: {e}")
            break
        ctx.db.set_meta(marker, str(now()))
        if found:
            ctx.db.execute("UPDATE assets SET coingecko_id=?, symbol=?, name=?, status='known', updated_at=?"
                           " WHERE key=? AND status='unmapped'",
                           (found["id"], found["symbol"] or a["symbol"], found["name"] or a["name"], now(), a["key"]))
            log(f"Mapped token {found['symbol']} → CoinGecko '{found['id']}'")


def value_movements(ctx, prices: PriceService, log: Callable[[str], None]) -> None:
    """EUR value of each movement at its own time (minute precision)."""
    pending = ctx.db.query(
        "SELECT m.id, m.ts, m.amount, m.asset_key FROM movements m JOIN assets a ON a.key=m.asset_key"
        " WHERE m.value_eur IS NULL AND COALESCE(m.category,'') <> 'spam' AND m.failed=0"
        " AND a.status NOT IN ('spam','ignored','unmapped') ORDER BY m.ts DESC LIMIT ?",
        (MAX_VALUATIONS_PER_RUN,))
    if not pending:
        return
    all_assets = assets_mod.all_assets(ctx)
    done = 0
    updates: List[tuple] = []

    def flush() -> None:
        if updates:
            with ctx.db.tx():  # short transactions: never hold the DB during API calls
                ctx.db.executemany("UPDATE movements SET price_eur=?, value_eur=?, price_source=? WHERE id=?",
                                   updates)
            updates.clear()

    for m in pending:
        pr = prices.price_at(all_assets.get(m["asset_key"]), m["ts"] - m["ts"] % 60)
        if pr.price is None:
            continue
        updates.append((dstr(pr.price), dstr(D(m["amount"]) * pr.price), pr.source, m["id"]))
        done += 1
        if len(updates) >= 100:
            flush()
    flush()
    left = ctx.db.scalar("SELECT COUNT(*) FROM movements WHERE value_eur IS NULL AND failed=0") or 0
    log(f"Transactions valued in EUR: {done}" + (f" ({left} still without a price)" if left else ""))


def take_snapshot(ctx, log: Callable[[str], None], source: str = "scheduled") -> int:
    """Live balances of every active wallet, stored as a point-in-time snapshot."""
    taken = 0
    for w in ctx.wallets(active_only=True):
        try:
            network = ctx.networks.get(w["network"])
            adapter = get_adapter(ctx, network)
            if adapter.kind == "manual":
                continue
            balances, infos = adapter.current_balances(w, ledger.known_asset_keys(ctx, w["id"]), log)
            assets_mod.upsert(ctx, infos)
            ledger.store_current(ctx, w["id"], balances, now(), snapshot_source=source)
            taken += 1
            log(f"{w['label']}: snapshot stored ({len(balances)} assets)")
        except (AdapterError, HttpError, KeyError, ValueError) as e:
            ctx.log(f"snapshot failed: {e}", "error", w["id"])
            log(f"{w['label']}: snapshot failed: {e}")
    return taken


class Job:
    def __init__(self, kind: str):
        self.kind = kind
        self.started_at = now()
        self.finished_at: Optional[int] = None
        self.lines: List[str] = []
        self.error: Optional[str] = None
        self.result: Any = None

    def log(self, line: str) -> None:
        self.lines.append(line)
        del self.lines[:-500]

    def public(self) -> Dict[str, Any]:
        return {"kind": self.kind, "started_at": self.started_at, "finished_at": self.finished_at,
                "running": self.finished_at is None, "error": self.error, "log": self.lines[-200:]}


class JobRunner:
    """One background job at a time (sync, snapshot, recompute)."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.current: Optional[Job] = None
        self._lock = threading.Lock()

    def start(self, kind: str, fn: Callable[[Callable[[str], None]], Any]) -> Job:
        with self._lock:
            if self.current and self.current.finished_at is None:
                raise RuntimeError("another job is running")
            job = Job(kind)
            self.current = job

        def target() -> None:
            try:
                job.result = fn(job.log)
            except Exception as e:
                job.error = str(e)
                job.log(f"✖ {e}")
                self.ctx.log(traceback.format_exc(), "error")
            finally:
                job.finished_at = now()

        threading.Thread(target=target, name=f"job-{kind}", daemon=True).start()
        return job
