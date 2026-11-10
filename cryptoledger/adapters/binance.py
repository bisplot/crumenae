"""Binance exchange account, read-only API key (create it with "Enable Reading" only).

What the API gives:
  * live balances: Spot + Funding + Simple Earn (flexible and locked);
  * daily Spot snapshots taken by Binance at 23:59:59 UTC, for the last 30 days
    only: sync in January and the 31/12 snapshot is stored;
  * deposits and withdrawals (linked to your own on-chain wallets by tx hash);
  * Simple Earn rewards (flexible and locked), imported as movements with the
    category "reward" and valued in EUR on the day received.
Trades and Convert are not imported yet, so past year-end balances come from
snapshots (scheduled, daily or manual), never from a reconstruction.
"""
from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from .. import assets as assets_mod
from ..http import HttpError
from ..timeutil import parse_utc
from ..util import D, ZERO
from . import register
from .base import Adapter, AdapterError, AssetInfo, Logger, Movement, Snapshot, SyncResult

DAY_MS = 86_400_000
WINDOW_MS = 89 * DAY_MS
EARLIEST_MS = 1_498_867_200_000  # 2017-07-01, Binance launch
SIMPLE_EARN_START_MS = 1_669_852_800_000  # 2022-12-01: Simple Earn (ex Savings/Staking) history
EARN_TYPES = ("REALTIME", "BONUS", "REWARDS")   # flexible reward types


def sign(secret: str, query: str) -> str:
    return hmac.new(secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()


@register
class BinanceAdapter(Adapter):
    id = "binance"
    earn_pause = 0.8   # Earn history calls weigh 150 of Binance's 12,000/min: about 1 call/s
    kind = "exchange"
    needs_address = False
    has_history = False
    credentials = ["api_key", "api_secret"]

    def __init__(self, ctx, network):
        super().__init__(ctx, network)
        self._offset_ms: Optional[int] = None

    def validate_address(self, address: str) -> str:
        return address.strip()[:64]  # optional account nickname / sub-account email

    # --- signed requests --------------------------------------------------------
    def _creds(self, wallet: Dict[str, Any]) -> Tuple[str, str]:
        key, secret = self.wallet_secret(wallet, "api_key"), self.wallet_secret(wallet, "api_secret")
        if not key or not secret:
            raise AdapterError("Binance API key/secret missing (Wallets → edit this account)")
        return key, secret

    def _base(self) -> str:
        return (self.network.explorer_api or "https://api.binance.com").rstrip("/")

    def _server_offset(self) -> int:
        if self._offset_ms is None:
            try:
                server = int(self.ctx.http.get_json(self._base() + "/api/v3/time")["serverTime"])
                self._offset_ms = server - int(time.time() * 1000)
            except (HttpError, KeyError, ValueError):
                self._offset_ms = 0
        return self._offset_ms

    def _signed(self, wallet: Dict[str, Any], method: str, path: str,
                params: Optional[Dict[str, Any]] = None) -> Any:
        key, secret = self._creds(wallet)
        p = {k: v for k, v in (params or {}).items() if v is not None}
        p["recvWindow"] = 10000
        p["timestamp"] = int(time.time() * 1000) + self._server_offset()
        query = urllib.parse.urlencode(p)
        url = f"{self._base()}{path}?{query}&signature={sign(secret, query)}"
        try:
            raw = self.ctx.http.request(method, url, headers={"X-MBX-APIKEY": key})
        except HttpError as e:
            raise AdapterError(f"Binance {path}: {e.body[:200] or e}")
        import json
        return json.loads(raw.decode("utf-8"))

    # --- sync -------------------------------------------------------------------
    def sync(self, wallet: Dict[str, Any], state: Dict[str, Any], log: Logger) -> SyncResult:
        result = SyncResult(state=dict(state))
        assets: Dict[str, AssetInfo] = {}
        net = self.network.id

        def asset(ticker: str) -> str:
            info = assets_mod.ticker_asset(self.ctx, net, ticker)
            assets[info.key] = info
            return info.key

        # Daily snapshots (Spot), last 30 days.
        end_ms = int(time.time() * 1000)
        try:
            snap = self._signed(wallet, "GET", "/sapi/v1/accountSnapshot",
                                {"type": "SPOT", "startTime": end_ms - 30 * DAY_MS, "endTime": end_ms, "limit": 30})
            for vo in snap.get("snapshotVos") or []:
                taken = int(vo.get("updateTime") or 0) // 1000
                for b in (vo.get("data") or {}).get("balances") or []:
                    amount = D(b.get("free")) + D(b.get("locked"))
                    if amount:
                        result.snapshots.append(Snapshot(taken_at=taken, asset_key=asset(b["asset"]),
                                                         amount=amount, source="exchange_daily",
                                                         note="Binance daily Spot snapshot 23:59:59 UTC"))
            log(f"{wallet['label']}: {len(snap.get('snapshotVos') or [])} daily snapshots")
        except AdapterError as e:
            result.warnings.append(f"daily snapshots unavailable: {e}")

        # Deposits and withdrawals, in 90-day windows.
        start_ms = int(state.get("dw_since_ms") or self._history_start_ms())
        cursor = start_ms
        deposits = withdrawals = 0
        while cursor < end_ms:
            window_end = min(cursor + WINDOW_MS, end_ms)
            for d in self._signed(wallet, "GET", "/sapi/v1/capital/deposit/hisrec",
                                  {"startTime": cursor, "endTime": window_end, "limit": 1000}) or []:
                if int(d.get("status", -1)) not in (1, 6):  # 1 success, 6 credited but cannot withdraw
                    continue
                deposits += 1
                ident = d.get("id") or d.get("txId")
                result.movements.append(Movement(
                    uid=f"dep:{ident}", tx_hash=_clean_hash(d.get("txId")), ts=int(d["insertTime"]) // 1000,
                    asset_key=asset(d["coin"]), amount=D(d["amount"]), kind="in",
                    to_address=d.get("address", ""), info=f"deposit via {d.get('network', '')}"))
            for w in self._signed(wallet, "GET", "/sapi/v1/capital/withdraw/history",
                                  {"startTime": cursor, "endTime": window_end, "limit": 1000}) or []:
                if int(w.get("status", -1)) != 6:  # 6 = completed
                    continue
                withdrawals += 1
                ts = parse_utc(w.get("completeTime") or w["applyTime"])
                key = asset(w["coin"])
                result.movements.append(Movement(
                    uid=f"wd:{w['id']}", tx_hash=_clean_hash(w.get("txId")), ts=ts, asset_key=key,
                    amount=-D(w["amount"]), kind="out", counterparty=w.get("address", ""),
                    to_address=w.get("address", ""), info=f"withdrawal via {w.get('network', '')}"))
                fee = D(w.get("transactionFee"))
                if fee:
                    result.movements.append(Movement(uid=f"wd:{w['id']}:fee", tx_hash=_clean_hash(w.get("txId")),
                                                     ts=ts, asset_key=key, amount=-fee, kind="fee",
                                                     to_address=w.get("address", "")))
            cursor = window_end
        log(f"{wallet['label']}: {deposits} deposits, {withdrawals} withdrawals")
        result.state["dw_since_ms"] = max(start_ms, end_ms - 2 * DAY_MS)  # re-read the last 2 days

        # Simple Earn rewards (flexible and locked), in windows of at most 3 months.
        try:
            n = self._earn_rewards(wallet, state, result, asset, end_ms, log)
            log(f"{wallet['label']}: {n} Simple Earn rewards")
        except AdapterError as e:
            result.warnings.append(f"Simple Earn rewards not read: {e}")
        result.assets = list(assets.values())
        return result

    def _earn_rewards(self, wallet: Dict[str, Any], state: Dict[str, Any], result: SyncResult,
                      asset, end_ms: int, log: Logger) -> int:
        start_ms = int(state.get("earn_since_ms") or max(self._history_start_ms(), SIMPLE_EARN_START_MS))
        count = 0
        calls = 0

        def pages(path: str, params: Dict[str, Any]):
            nonlocal calls
            current = 1
            while True:
                if calls and self.earn_pause:
                    time.sleep(self.earn_pause)
                calls += 1
                data = self._signed(wallet, "GET", path, dict(params, current=current, size=100)) or {}
                rows = data.get("rows") or []
                yield from rows
                if len(rows) < 100 or current * 100 >= int(data.get("total") or 0):
                    return
                current += 1

        cursor = start_ms
        while cursor < end_ms:
            window_end = min(cursor + WINDOW_MS, end_ms)
            for kind in EARN_TYPES:
                for r in pages("/sapi/v1/simple-earn/flexible/history/rewardsRecord",
                               {"type": kind, "startTime": cursor, "endTime": window_end}):
                    amount = D(r.get("rewards"))
                    if not amount or not r.get("asset"):
                        continue
                    t = int(r["time"])
                    result.movements.append(Movement(
                        uid=f"earn:flex:{kind}:{r['asset']}:{t}:{r.get('projectId', '')}", tx_hash="",
                        ts=t // 1000, asset_key=asset(r["asset"]), amount=amount, kind="in",
                        info=f"reward: Simple Earn flexible ({kind.lower()})"))
                    count += 1
            for r in pages("/sapi/v1/simple-earn/locked/history/rewardsRecord",
                           {"startTime": cursor, "endTime": window_end}):
                amount = D(r.get("amount"))
                if not amount or not r.get("asset"):
                    continue
                t = int(r["time"])
                result.movements.append(Movement(
                    uid=f"earn:locked:{r.get('positionId', '')}:{r['asset']}:{t}", tx_hash="",
                    ts=t // 1000, asset_key=asset(r["asset"]), amount=amount, kind="in",
                    info=f"reward: Simple Earn locked{' ' + str(r['lockPeriod']) + ' days' if r.get('lockPeriod') else ''}"))
                count += 1
            cursor = window_end
        result.state["earn_since_ms"] = max(start_ms, end_ms - 2 * DAY_MS)
        return count

    def _history_start_ms(self) -> int:
        first = self.ctx.settings.get("first_fiscal_year")
        if first:
            return max(EARLIEST_MS, (self.ctx.calendar.start(int(first)) - 86400) * 1000)
        return EARLIEST_MS

    # --- live balances ----------------------------------------------------------
    def current_balances(self, wallet: Dict[str, Any], known_assets: List[str],
                         log: Logger) -> Tuple[Dict[str, Decimal], List[AssetInfo]]:
        net = self.network.id
        totals: Dict[str, Decimal] = {}
        infos: Dict[str, AssetInfo] = {}

        def add(ticker: str, amount: Decimal) -> None:
            if not amount:
                return
            info = assets_mod.ticker_asset(self.ctx, net, ticker)
            infos[info.key] = info
            totals[info.key] = totals.get(info.key, ZERO) + amount

        earn_ok = True
        earn: Dict[str, Decimal] = {}   # Simple Earn positions, listed on the Staking page

        def add_earn(ticker: str, amount: Decimal) -> None:
            add(ticker, amount)
            if amount:
                earn[ticker] = earn.get(ticker, ZERO) + amount
        try:
            page, rows = 1, []
            while True:
                data = self._signed(wallet, "GET", "/sapi/v1/simple-earn/flexible/position",
                                    {"current": page, "size": 100})
                rows += data.get("rows") or []
                if len(rows) >= int(data.get("total") or 0) or not data.get("rows"):
                    break
                page += 1
            for r in rows:
                add_earn(r["asset"], D(r.get("totalAmount")))
            data = self._signed(wallet, "GET", "/sapi/v1/simple-earn/locked/position", {"size": 100})
            for r in data.get("rows") or []:
                add_earn(r["asset"], D(r.get("amount")))
            import json
            self.ctx.db.set_meta(f"earn_positions:{wallet['id']}", json.dumps(
                {"at": int(time.time()), "positions": {k: str(v) for k, v in sorted(earn.items())}}))
        except AdapterError as e:
            earn_ok = False
            log(f"{wallet['label']}: Simple Earn positions unavailable ({e})")

        account = self._signed(wallet, "GET", "/api/v3/account", {"omitZeroBalances": "true"})
        for b in account.get("balances") or []:
            ticker = b["asset"]
            if ticker.startswith("LD") and len(ticker) > 2:
                if earn_ok:
                    continue  # legacy "LD" Earn tokens: already counted via Simple Earn
                ticker = ticker[2:]
            add(ticker, D(b.get("free")) + D(b.get("locked")))
        try:
            for b in self._signed(wallet, "POST", "/sapi/v1/asset/get-funding-asset", {}) or []:
                add(b["asset"], D(b.get("free")) + D(b.get("locked")) + D(b.get("freeze"))
                    + D(b.get("withdrawing")))
        except AdapterError as e:
            log(f"{wallet['label']}: Funding wallet unavailable ({e})")
        for key in known_assets:
            if key.startswith(net + ":") and key not in totals:
                totals[key] = ZERO
        return totals, list(infos.values())


def _clean_hash(tx_id: Any) -> str:
    """Binance prefixes internal transfers with 'Internal transfer'; keep on-chain hashes only."""
    text = str(tx_id or "")
    if not text or " " in text:
        return ""
    return text.lower() if text.startswith("0x") else text
