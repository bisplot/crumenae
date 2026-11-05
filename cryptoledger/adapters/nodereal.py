"""BNB Smart Chain through NodeReal MegaNode (free key; BNB Chain's replacement for
the BscScan API, which closed in December 2025 — Etherscan's free plan does not
cover BSC).

History comes from the MegaNode enhanced API `nr_getAssetTransfers` (native,
internal and BEP-20 transfers, JSON-RPC 2.0). Gas fees come from the receipts of
the transactions you sent. Live and past balances use standard JSON-RPC on the
same endpoint (eth_getBalance / balanceOf), so the year-end reconstruction is
cross-checked against chain state where the plan allows archive reads.

The field names follow NodeReal's reference; parsing is tolerant (hex or decimal
numbers, optional decimals) because the documentation examples vary.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from .. import assets as assets_mod
from ..http import HttpError
from ..util import scaled
from . import register
from .base import AdapterError, AssetInfo, Logger, Movement, SyncResult
from .evm import EvmAdapter

PAGE = "0x64"              # 100 transfers per page (the documented safe maximum)
CATEGORIES = ["external", "internal", "20"]
DECIMALS = "0x313ce567"    # decimals()


def _int(v: Any, default: int = 0) -> int:
    """Hex ("0x1a") or decimal ("26", 26) integer."""
    if v is None or v == "":
        return default
    if isinstance(v, int):
        return v
    s = str(v).strip()
    try:
        return int(s, 16) if s.lower().startswith("0x") else int(Decimal(s))
    except (ValueError, ArithmeticError):
        return default


@register
class NodeRealAdapter(EvmAdapter):
    id = "nodereal"

    @property
    def has_history(self) -> bool:  # type: ignore[override]
        return True

    def _key(self) -> Optional[str]:
        return self.secret(f"{self.network.explorer_key or 'nodereal'}_api_key")

    def _rpc_url(self) -> Optional[str]:
        custom = self.secret(f"rpc_url:{self.network.id}")
        if custom:
            return custom
        key = self._key()
        if not key:
            return None
        return (self.network.explorer_api or "https://bsc-mainnet.nodereal.io/v1").rstrip("/") + "/" + key

    def _call(self, method: str, params: List[Any]) -> Any:
        url = self._rpc_url()
        if not url:
            raise AdapterError("NodeReal API key missing (Settings → Blockchain APIs → BNB Smart Chain)")
        try:
            resp = self.ctx.http.post_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        except HttpError as e:
            if e.status in (401, 403):
                raise AdapterError("NodeReal refused the API key: copy it again from dashboard.nodereal.io")
            raise AdapterError(f"{self.network.name} (NodeReal) {method}: {e}")
        if not isinstance(resp, dict):
            raise AdapterError(f"{self.network.name} (NodeReal) {method}: unexpected response")
        if resp.get("error"):
            err = resp["error"]
            raise AdapterError(f"{self.network.name} (NodeReal) {method}: {err.get('message') if isinstance(err, dict) else err}")
        return resp.get("result")

    _rpc = _call  # balance_at / block_at of the EVM adapter use the same endpoint

    def block_at(self, ts: int) -> int:
        hi = _int(self._call("eth_blockNumber", []))
        lo = 0
        while lo < hi:
            mid = (lo + hi + 1) // 2
            blk = self._call("eth_getBlockByNumber", [hex(mid), False])
            if _int(blk["timestamp"]) <= ts:
                lo = mid
            else:
                hi = mid - 1
        return lo

    # --- history ------------------------------------------------------------------
    def _transfers(self, address_field: str, address: str, from_block: int) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        page_key = None
        while True:
            q: Dict[str, Any] = {"category": CATEGORIES, "fromBlock": hex(from_block), "toBlock": "latest",
                                 address_field: address, "order": "asc", "excludeZeroValue": False,
                                 "maxCount": PAGE}
            if page_key:
                q["pageKey"] = page_key
            res = self._call("nr_getAssetTransfers", [q]) or {}
            batch = res.get("transfers") or []
            out.extend(batch)
            page_key = res.get("pageKey")
            if not page_key or not batch:
                return out

    def _decimals(self, contract: str, cache: Dict[str, Optional[int]]) -> Optional[int]:
        if contract not in cache:
            known = assets_mod.get(self.ctx, f"{self.network.id}:{contract}")
            if known and known.get("decimals") is not None:
                cache[contract] = int(known["decimals"])
            else:
                try:
                    raw = self._call("eth_call", [{"to": contract, "data": DECIMALS}, "latest"])
                    cache[contract] = _int(raw) if raw and raw != "0x" else None
                except AdapterError:
                    cache[contract] = None
        return cache[contract]

    def sync(self, wallet: Dict[str, Any], state: Dict[str, Any], log: Logger) -> SyncResult:
        result = SyncResult(state=dict(state))
        addr = wallet["address"].lower()
        start = int(state.get("last_block", 0))
        native = assets_mod.native_asset(self.network)
        nat_dec = native.decimals or 18
        log(f"{wallet['label']}: reading {self.network.name} transfers from block {start} (NodeReal)")
        rows: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
        for field in ("fromAddress", "toAddress"):
            for t in self._transfers(field, addr, start):
                k = (str(t.get("hash", "")).lower(), t.get("category"), str(t.get("from", "")).lower(),
                     str(t.get("to", "")).lower(), str(t.get("contractAddress", "")).lower(), str(t.get("value")),
                     t.get("logIndex"), t.get("traceIndex") or t.get("traceAddress"))
                rows[k] = t   # a transfer to yourself comes back in both queries
        transfers = sorted(rows.values(), key=lambda t: (_int(t.get("blockNum")), str(t.get("hash"))))
        log(f"{wallet['label']}: {len(transfers)} transfers")

        moves: List[Movement] = []
        seen_assets: Dict[str, AssetInfo] = {native.key: native}
        dec_cache: Dict[str, Optional[int]] = {}
        counter: Dict[str, int] = {}
        sent: Dict[str, int] = {}   # tx hash -> timestamp, for the gas fee
        max_block = start
        for t in transfers:
            h = str(t.get("hash", "")).lower()
            ts = _int(t.get("blockTimeStamp") or t.get("blockTimestamp"))
            max_block = max(max_block, _int(t.get("blockNum")))
            frm = str(t.get("from") or "").lower()
            to = str(t.get("to") or "").lower()
            cat = str(t.get("category") or "")
            raw = _int(t.get("value"))
            if cat == "external" and frm == addr:
                sent[h] = ts
            if cat in ("external", "internal"):
                key, amount = native.key, scaled(raw, nat_dec)
                if cat == "external":
                    uid = h
                else:
                    sig = f"{h}:int:{frm}:{to}:{raw}"
                    counter[sig] = counter.get(sig, -1) + 1
                    uid = f"{sig}:{counter[sig]}"
                info = "internal" if cat == "internal" else ""
            elif cat == "20":
                contract = str(t.get("contractAddress") or "").lower()
                if not contract:
                    continue
                dec = t.get("decimal", t.get("decimals"))
                dec = _int(dec, -1) if dec not in (None, "") else -1
                if dec < 0:
                    dec = self._decimals(contract, dec_cache)
                if dec is None:
                    result.warnings.append(f"token {contract}: decimals unknown, transfer skipped")
                    continue
                asset = seen_assets.get(f"{self.network.id}:{contract}") or assets_mod.token_asset(
                    self.ctx, self.network, contract, str(t.get("asset") or ""), "", dec)
                seen_assets[asset.key] = asset
                key = asset.key
                amount = scaled(raw, asset.decimals if asset.decimals is not None else dec)
                log_index = t.get("logIndex")
                if log_index not in (None, ""):
                    uid = f"{h}:log:{_int(log_index)}"
                else:
                    sig = f"{h}:tok:{contract}:{frm}:{to}:{raw}"
                    counter[sig] = counter.get(sig, -1) + 1
                    uid = f"{sig}:{counter[sig]}"
                info = "zero-value transfer" if not amount else ""
                if frm == addr:
                    sent.setdefault(h, ts)
            else:
                continue   # NFTs are not valued
            if frm == addr and (amount or cat == "20"):
                moves.append(Movement(uid=f"{uid}:out", tx_hash=h, ts=ts, asset_key=key, amount=-amount,
                                      kind="out", counterparty=to, info=info, from_address=frm, to_address=to))
            if to == addr and (amount or cat == "20"):
                moves.append(Movement(uid=f"{uid}:in", tx_hash=h, ts=ts, asset_key=key, amount=amount,
                                      kind="in", counterparty=frm, info=info, from_address=frm, to_address=to))

        # Gas fees of the transactions you sent (one receipt each).
        for h, ts in sent.items():
            try:
                rc = self._call("eth_getTransactionReceipt", [h]) or {}
            except AdapterError as e:
                result.warnings.append(f"fee of {h[:10]}… not read: {e}")
                continue
            if str(rc.get("from") or "").lower() != addr:
                continue   # a contract moved your tokens: the gas was paid by someone else
            fee = scaled(_int(rc.get("gasUsed")) * _int(rc.get("effectiveGasPrice") or rc.get("gasPrice")), nat_dec)
            if fee:
                moves.append(Movement(uid=f"{h}:fee", tx_hash=h, ts=ts, asset_key=native.key, amount=-fee,
                                      kind="fee", counterparty=str(rc.get("to") or "").lower(),
                                      failed=_int(rc.get("status"), 1) == 0, from_address=addr,
                                      to_address=str(rc.get("to") or "").lower()))

        result.movements = moves
        result.assets = list(seen_assets.values())
        result.state["last_block"] = max_block
        return result

    # --- live balances ------------------------------------------------------------
    def current_balances(self, wallet: Dict[str, Any], known_assets: List[str],
                         log: Logger) -> Tuple[Dict[str, Decimal], List[AssetInfo]]:
        native = assets_mod.native_asset(self.network)
        tokens = [k for k in known_assets if k != native.key and k.startswith(self.network.id + ":")]
        if not self._rpc_url():
            raise AdapterError("NodeReal API key missing (Settings → Blockchain APIs → BNB Smart Chain)")
        bal = self.balance_at(wallet, None, [native.key] + tokens, log)
        if bal is None:
            raise AdapterError(f"{self.network.name}: live balances could not be read from NodeReal")
        return bal, [native]
