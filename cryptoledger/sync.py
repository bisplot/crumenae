"""Sync orchestration and background jobs."""
from __future__ import annotations

import threading
import traceback
from typing import Any, Callable, Dict, List, Optional

from . import assets as assets_mod
from . import classify, exports, ledger, yearend
from .adapters import get_adapter
from .http import HttpError
from .models import AdapterError
from .prices import PriceService
from .util import D, dstr, now

MAX_VALUATIONS_PER_RUN = 3000
MAX_CONTRACT_LOOKUPS_PER_RUN = 25


def sync_wallet(ctx, wallet: Dict[str, Any], log: Callable[[str], None], full: bool = False) -> int:
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
    ensure_assets(ctx, wallet, network)
    ctx.db.execute("UPDATE wallets SET last_sync_at=?, last_error=NULL WHERE id=?", (now(), wallet["id"]))
    log(f"{wallet['label']}: {added} new movements, {sum(1 for v in balances.values() if v)} assets held now")
    if added or res.snapshots:
        ctx.mark_yearend_stale(f"new data from {wallet['label']}")
    return added


def ensure_assets(ctx, wallet: Dict[str, Any], network) -> int:
    """Re-create the asset entry of anything this wallet holds or has moved but that is missing
    from the asset list (e.g. after "Clear all"): tickers for exchange and manual accounts,
    native coins, contracts for tokens (name filled by the next CoinGecko lookup)."""
    keys = [r["k"] for r in ctx.db.query(
        "SELECT asset_key AS k FROM movements WHERE wallet_id=? UNION SELECT asset_key FROM snapshots WHERE wallet_id=?"
        " UNION SELECT asset_key FROM current_balances WHERE wallet_id=?", (wallet["id"],) * 3)]
    known = {r["key"] for r in ctx.db.query("SELECT key FROM assets")}
    infos = []
    for key in keys:
        if key in known or ":" not in key:
            continue
        prefix, rest = key.split(":", 1)
        if rest == "native" and network.native:
            infos.append(assets_mod.native_asset(network))
        elif network.kind in ("exchange", "manual"):
            infos.append(assets_mod.ticker_asset(ctx, prefix, rest))
        elif not rest.startswith("trc10:"):
            infos.append(assets_mod.token_asset(ctx, network, rest))
    assets_mod.upsert(ctx, infos)
    return len(infos)


def load_assets(ctx, log: Callable[[str], None], wallet_ids: Optional[List[int]] = None) -> Dict[str, Any]:
    """"Load all" on the Assets page: live balances only (tokens held now), no transaction
    history, so it costs a few calls per wallet instead of one per transaction. Assets moved
    in the past are rebuilt from the stored history without any call."""
    prices = PriceService(ctx)
    wallets = [w for w in ctx.wallets(active_only=True) if not wallet_ids or w["id"] in wallet_ids]
    loaded = errors = 0
    for w in wallets:
        try:
            network = ctx.networks.get(w["network"])
        except KeyError:
            continue
        adapter = get_adapter(ctx, network)
        if ctx.offline or adapter.kind == "manual":
            ensure_assets(ctx, w, network)
            continue
        try:
            balances, infos = adapter.current_balances(w, ledger.known_asset_keys(ctx, w["id"]), log)
            assets_mod.upsert(ctx, infos)
            ledger.store_current(ctx, w["id"], balances, now())
            ensure_assets(ctx, w, network)
            held = sum(1 for v in balances.values() if v)
            loaded += 1
            log(f"{w['label']}: {held} assets held now")
        except Exception as e:  # keep going with the other wallets
            errors += 1
            log(f"{w['label']}: ✖ {e}")
    if ctx.offline:
        log("Offline/demo mode: asset list rebuilt from stored data, no API called.")
    else:
        lookup_unknown_tokens(ctx, prices, log)
        refresh_prices(ctx, prices, log)
    n = ctx.db.scalar("SELECT COUNT(*) FROM assets") or 0
    log(f"Done: {n} assets, {loaded} wallet(s) read, {errors} error(s). Transactions were not read: "
        "use Sync now for the history.")
    return {"assets": n, "errors": errors}


def run_sync(ctx, log: Callable[[str], None], wallet_ids: Optional[List[int]] = None,
             full: bool = False) -> Dict[str, Any]:
    prices = PriceService(ctx)
    wallets = [w for w in ctx.wallets(active_only=True) if not wallet_ids or w["id"] in wallet_ids]
    errors = 0
    if ctx.offline:
        log("Offline/demo mode: external APIs are not called.")
        for w in wallets:   # still rebuild missing asset entries from stored data
            try:
                ensure_assets(ctx, w, ctx.networks.get(w["network"]))
            except KeyError:
                pass
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
    refresh_prices(ctx, prices, log)
    if ctx.settings.get("value_transactions"):
        value_movements(ctx, prices, log)
    write_exports(ctx, log)
    for e in prices.errors[:5]:
        log(f"price provider: {e}")
    log(f"Done: {errors} wallet error(s).")
    if ctx.yearend_stale():
        log("Year-end values were not recomputed: press \"Recompute year-end\" when you are ready.")
    return {"errors": errors}


def recompute(ctx, log: Callable[[str], None]) -> List[int]:
    """The "Recompute year-end" button: classification, balances and EUR values at every
    fiscal-year cutoff, then the exports."""
    classify.run(ctx)
    years = yearend.compute_all(ctx, PriceService(ctx), log)
    write_exports(ctx, log)
    log(f"Year-end values recomputed for {years[0] - 1}–{years[-1]}.")
    return years


def wallet_service(ctx, wallet: Dict[str, Any]) -> Optional[str]:
    """The API service (ratelimit.SERVICES id) a wallet's sync calls, or None (manual)."""
    import urllib.parse
    try:
        net = ctx.networks.get(wallet["network"])
    except KeyError:
        return None
    if net.adapter == "manual":
        return None
    if net.adapter == "solana":
        url = get_adapter(ctx, net)._url()
    elif net.adapter == "alchemy":
        url = get_adapter(ctx, net)._rpc_url() or net.explorer_api or ""
    elif net.adapter == "binance":
        url = net.explorer_api or "https://api.binance.com"
    else:
        url = net.explorer_api or ctx.secrets.get(f"rpc_url:{net.id}") or net.rpc_url or ""
    return ctx.limiter.service_for(urllib.parse.urlsplit(url).hostname or "")


def wallets_by_service(ctx) -> Dict[str, List[int]]:
    out: Dict[str, List[int]] = {}
    for w in ctx.wallets(active_only=True):
        sid = wallet_service(ctx, w)
        if sid:
            out.setdefault(sid, []).append(w["id"])
    return out


def write_exports(ctx, log: Callable[[str], None]) -> None:
    """Rewrite data/exports/ (wallets, balances per year-end, transactions per year)."""
    try:
        exports.write_all(ctx, log)
    except OSError as e:  # a read-only or full disk must not break the sync
        log(f"Exports not written: {e}")
        ctx.log(f"exports not written: {e}", "error")


def refresh_prices(ctx, prices: PriceService, log: Callable[[str], None]) -> int:
    # Live prices are for the Dashboard: assets held only in lost wallets are not shown there.
    rows = ctx.db.query(
        "SELECT DISTINCT a.* FROM current_balances c JOIN assets a ON a.key=c.asset_key"
        " JOIN wallets w ON w.id=c.wallet_id"
        " WHERE CAST(c.amount AS REAL) != 0 AND a.status NOT IN ('spam','ignored') AND w.lost=0")
    if rows and not ctx.offline:
        log(f"Live prices for {len(rows)} assets…")
    got = prices.refresh_current({r["key"]: r for r in rows})
    log(f"Live prices: {len(got)}/{len(rows)} assets priced")
    return len(got)


def lookup_unknown_tokens(ctx, prices: PriceService, log: Callable[[str], None]) -> None:
    """Map unknown tokens to CoinGecko by contract address (reliable, unlike tickers)."""
    rows = ctx.db.query(
        "SELECT a.* FROM assets a WHERE (a.status='unmapped' OR (a.status='ghost' AND a.coingecko_id IS NULL"
        " AND a.binance_symbol IS NULL)) AND a.contract IS NOT NULL"
        " AND EXISTS (SELECT 1 FROM current_balances c WHERE c.asset_key=a.key AND CAST(c.amount AS REAL) != 0)")
    todo = [a for a in rows if not ctx.db.get_meta(f"cg_lookup:{a['key']}")][:MAX_CONTRACT_LOOKUPS_PER_RUN]
    if todo:
        tip = "" if ctx.secrets.get("coingecko_api_key") else \
            " (without a CoinGecko key this is slow: a free Demo key speeds it up, Settings → Price sources)"
        log(f"Looking up {len(todo)} unknown token(s) on CoinGecko by contract{tip}…")
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
            ctx.db.execute("UPDATE assets SET coingecko_id=?, symbol=?, name=?, updated_at=?,"
                           " status=CASE WHEN status='unmapped' THEN 'known' ELSE status END"
                           " WHERE key=? AND status IN ('unmapped','ghost')",
                           (found["id"], found["symbol"] or a["symbol"], found["name"] or a["name"], now(), a["key"]))
            ctx.mark_yearend_stale(f"token {found['symbol'] or a['symbol']} mapped to a price")
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
