"""EVM chains (Ethereum, BNB Chain, Polygon, Arbitrum, Base, ...).

History comes from an Etherscan-compatible explorer API (Etherscan V2 multichain
by default; Blockscout and Routescan expose the same interface): normal txs,
internal txs and ERC-20 transfers. If an RPC URL is configured for the network,
balances at a past moment are also read from chain state (needs an archive node)
and used to check the reconstruction.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from .. import assets as assets_mod
from ..http import HttpError
from ..util import scaled
from . import register
from .base import Adapter, AdapterError, AssetInfo, Logger, Movement, SyncResult

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
PAGE = 1000
WINDOW = 10000            # Etherscan returns at most page*offset <= 10000 rows per query
BALANCE_OF = "0x70a08231"  # balanceOf(address)


@register
class EvmAdapter(Adapter):
    id = "evm"

    @property
    def has_history(self) -> bool:  # type: ignore[override]
        return bool(self.network.explorer_api)

    def validate_address(self, address: str) -> str:
        address = address.strip()
        if not ADDRESS_RE.match(address):
            raise ValueError("EVM address must be 0x followed by 40 hex characters")
        return address.lower()

    # --- explorer API -----------------------------------------------------------
    def _api(self, params: Dict[str, Any]) -> Any:
        base = self.network.explorer_api
        if not base:
            raise AdapterError(f"{self.network.name}: no explorer API configured (Settings → Networks)")
        p = dict(params)
        if self.network.chainid_param:
            p["chainid"] = self.network.chain_id
        key_name = self.network.explorer_key
        if key_name:
            key = self.secret(f"{key_name}_api_key")
            if not key and "etherscan.io" in base:
                raise AdapterError("Etherscan API key missing (Settings → API keys)")
            if key:
                p["apikey"] = key
        for attempt in range(6):
            try:
                data = self.ctx.http.get_json(base, p)
            except HttpError as e:
                raise AdapterError(f"{self.network.name} explorer: {e}")
            status = str(data.get("status", ""))
            message = str(data.get("message", ""))
            result = data.get("result")
            if status == "1" or (status == "" and "result" in data):
                return result
            text = f"{message} {result}".lower()
            if "no transactions found" in text or "no records found" in text or result == []:
                return []
            if "rate limit" in text or "max calls" in text:
                time.sleep(1.0 + attempt)
                continue
            raise AdapterError(f"{self.network.name} explorer: {message}: {result}")
        raise AdapterError(f"{self.network.name} explorer: rate limit, try again later")

    def _list(self, action: str, address: str, start_block: int) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        page = 1
        while True:
            batch = self._api({"module": "account", "action": action, "address": address,
                               "startblock": start_block, "endblock": 999999999,
                               "page": page, "offset": PAGE, "sort": "asc"})
            if not isinstance(batch, list):
                raise AdapterError(f"{self.network.name} explorer: unexpected {action} response")
            rows.extend(batch)
            if len(batch) < PAGE:
                return rows
            page += 1
            if page * PAGE > WINDOW:
                # Restart the window from the last block; duplicates are dropped by uid.
                start_block = int(batch[-1]["blockNumber"])
                page = 1

    # --- sync -------------------------------------------------------------------
    def sync(self, wallet: Dict[str, Any], state: Dict[str, Any], log: Logger) -> SyncResult:
        result = SyncResult(state=dict(state))
        if not self.has_history:
            result.warnings.append(
                f"{self.network.name}: no explorer API, transaction history is not available. "
                "Year-end balances come from snapshots or the archive RPC.")
            return result
        addr = wallet["address"].lower()
        start = int(state.get("last_block", 0))
        native = assets_mod.native_asset(self.network)
        decimals = native.decimals or 18
        log(f"{wallet['label']}: reading {self.network.name} transactions from block {start}")
        normal = self._list("txlist", addr, start)
        internal = self._list("txlistinternal", addr, start)
        tokens = self._list("tokentx", addr, start)
        log(f"{wallet['label']}: {len(normal)} txs, {len(internal)} internal, {len(tokens)} token transfers")

        moves: List[Movement] = []
        seen_assets: Dict[str, AssetInfo] = {native.key: native}
        max_block = start

        for tx in normal:
            h = tx["hash"].lower()
            ts = int(tx["timeStamp"])
            max_block = max(max_block, int(tx["blockNumber"]))
            frm = (tx.get("from") or "").lower()
            to = (tx.get("to") or "").lower() or (tx.get("contractAddress") or "").lower()
            failed = tx.get("isError") == "1" or tx.get("txreceipt_status") == "0"
            value = scaled(tx.get("value") or 0, decimals)
            info = (tx.get("functionName") or "").split("(")[0]
            if frm == addr:
                gas_used = int(tx.get("gasUsed") or 0)
                gas_price = int(tx.get("gasPrice") or 0)
                fee = scaled(gas_used * gas_price, decimals)
                if fee:
                    moves.append(Movement(uid=f"{h}:fee", tx_hash=h, ts=ts, asset_key=native.key,
                                          amount=-fee, kind="fee", counterparty=to, failed=failed, info=info))
                if value and not failed:
                    moves.append(Movement(uid=f"{h}:out", tx_hash=h, ts=ts, asset_key=native.key,
                                          amount=-value, kind="out", counterparty=to, info=info))
            if to == addr and value and not failed:
                moves.append(Movement(uid=f"{h}:in", tx_hash=h, ts=ts, asset_key=native.key,
                                      amount=value, kind="in", counterparty=frm, info=info))

        counter: Dict[str, int] = defaultdict(int)
        for itx in internal:
            h = itx["hash"].lower()
            ts = int(itx["timeStamp"])
            max_block = max(max_block, int(itx["blockNumber"]))
            idx = itx.get("traceId") or str(counter[h])
            counter[h] += 1
            if itx.get("isError") == "1":
                continue
            frm = (itx.get("from") or "").lower()
            to = (itx.get("to") or "").lower()
            value = scaled(itx.get("value") or 0, decimals)
            if not value:
                continue
            if frm == addr:
                moves.append(Movement(uid=f"{h}:int:{idx}:out", tx_hash=h, ts=ts, asset_key=native.key,
                                      amount=-value, kind="out", counterparty=to, info="internal"))
            if to == addr:
                moves.append(Movement(uid=f"{h}:int:{idx}:in", tx_hash=h, ts=ts, asset_key=native.key,
                                      amount=value, kind="in", counterparty=frm, info="internal"))

        counter.clear()
        for t in tokens:
            h = t["hash"].lower()
            ts = int(t["timeStamp"])
            max_block = max(max_block, int(t["blockNumber"]))
            contract = (t.get("contractAddress") or "").lower()
            frm = (t.get("from") or "").lower()
            to = (t.get("to") or "").lower()
            try:
                dec = int(t.get("tokenDecimal") or 0)
            except ValueError:
                dec = 0
            asset = seen_assets.get(f"{self.network.id}:{contract}") or assets_mod.token_asset(
                self.ctx, self.network, contract, t.get("tokenSymbol", ""), t.get("tokenName", ""), dec)
            seen_assets[asset.key] = asset
            dec = asset.decimals if asset.decimals is not None else dec
            amount = scaled(t.get("value") or 0, dec)
            if t.get("logIndex") not in (None, ""):
                base_uid = f"{h}:log:{t['logIndex']}"
            else:
                sig = f"{h}:tok:{contract}:{frm}:{to}:{t.get('value')}"
                base_uid = f"{sig}:{counter[sig]}"
                counter[sig] += 1
            info = "zero-value transfer" if not amount else ""
            if frm == addr:
                moves.append(Movement(uid=f"{base_uid}:out", tx_hash=h, ts=ts, asset_key=asset.key,
                                      amount=-amount, kind="out", counterparty=to, info=info))
            if to == addr:
                moves.append(Movement(uid=f"{base_uid}:in", tx_hash=h, ts=ts, asset_key=asset.key,
                                      amount=amount, kind="in", counterparty=frm, info=info))

        if self.network.l2:
            result.warnings.append(
                f"{self.network.name}: explorer fees exclude L1 data fees on some rollups; "
                "set an archive RPC URL to verify year-end balances against chain state.")
        result.movements = moves
        result.assets = list(seen_assets.values())
        result.state["last_block"] = max_block
        return result

    # --- live balances ----------------------------------------------------------
    def current_balances(self, wallet: Dict[str, Any], known_assets: List[str],
                         log: Logger) -> Tuple[Dict[str, Decimal], List[AssetInfo]]:
        addr = wallet["address"].lower()
        native = assets_mod.native_asset(self.network)
        tokens = [k for k in known_assets if k != native.key and k.startswith(self.network.id + ":")]
        out: Dict[str, Decimal] = {}
        decimals = {r["key"]: r["decimals"] for r in self.ctx.db.query(
            "SELECT key, decimals FROM assets WHERE network=?", (self.network.id,))}
        if self.network.explorer_api:
            out[native.key] = scaled(self._api({"module": "account", "action": "balance",
                                                "address": addr, "tag": "latest"}), native.decimals or 18)
            for key in tokens:
                contract = key.split(":", 1)[1]
                raw = self._api({"module": "account", "action": "tokenbalance",
                                 "contractaddress": contract, "address": addr, "tag": "latest"})
                out[key] = scaled(raw or 0, decimals.get(key) or 0)
        elif self._rpc_url():
            bal = self.balance_at(wallet, None, [native.key] + tokens, log)
            out.update(bal or {})
        else:
            raise AdapterError(f"{self.network.name}: configure an explorer API or RPC URL")
        return out, [native]

    # --- chain state at a past moment (archive RPC) -----------------------------
    def _rpc_url(self) -> Optional[str]:
        return self.secret(f"rpc_url:{self.network.id}") or self.network.rpc_url or None

    def _rpc(self, method: str, params: List[Any]) -> Any:
        url = self._rpc_url()
        resp = self.ctx.http.post_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if resp.get("error"):
            raise AdapterError(f"{self.network.name} RPC {method}: {resp['error'].get('message')}")
        return resp.get("result")

    def block_at(self, ts: int) -> int:
        """Last block mined at or before `ts`."""
        if self.network.explorer_api:
            try:
                r = self._api({"module": "block", "action": "getblocknobytime",
                               "timestamp": ts, "closest": "before"})
                return int(r)
            except (AdapterError, ValueError, TypeError):
                pass
        # Binary search over RPC block timestamps.
        hi = int(self._rpc("eth_blockNumber", []), 16)
        lo = 0
        while lo < hi:
            mid = (lo + hi + 1) // 2
            blk = self._rpc("eth_getBlockByNumber", [hex(mid), False])
            if int(blk["timestamp"], 16) <= ts:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def balance_at(self, wallet: Dict[str, Any], ts: Optional[int], asset_keys: List[str],
                   log: Logger) -> Optional[Dict[str, Decimal]]:
        if not self._rpc_url():
            return None
        addr = wallet["address"].lower()
        tag = "latest" if ts is None else hex(self.block_at(ts))
        native = assets_mod.native_asset(self.network)
        decimals = {r["key"]: r["decimals"] for r in self.ctx.db.query(
            "SELECT key, decimals FROM assets WHERE network=?", (self.network.id,))}
        out: Dict[str, Decimal] = {}
        try:
            out[native.key] = scaled(int(self._rpc("eth_getBalance", [addr, tag]), 16), native.decimals or 18)
            for key in asset_keys:
                if key == native.key or not key.startswith(self.network.id + ":"):
                    continue
                if decimals.get(key) is None:
                    continue  # unknown decimals: cannot scale, leave to reconstruction
                contract = key.split(":", 1)[1]
                data = BALANCE_OF + addr[2:].rjust(64, "0")
                raw = self._rpc("eth_call", [{"to": contract, "data": data}, tag])
                value = int(raw, 16) if raw and raw != "0x" else 0
                out[key] = scaled(value, decimals[key])
        except (AdapterError, HttpError, ValueError, TypeError) as e:
            log(f"{wallet['label']}: chain-state read failed ({e}); the RPC may not be an archive node")
            return None
        return out
