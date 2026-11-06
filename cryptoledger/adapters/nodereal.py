"""BNB Smart Chain through Alchemy or NodeReal MegaNode (both have a free plan; the
BscScan API closed in December 2025 and Etherscan's free plan does not cover BSC).

Provider: an Alchemy key if saved (sign-up with e-mail or Google), else a NodeReal
key. Alchemy uses alchemy_getAssetTransfers (native and BEP-20; Alchemy gives no
internal transfers on BSC, so BNB received from contracts is caught by the year-end
cross-check against chain state) and alchemy_getTokenBalances to list every token
held now. NodeReal is described below.

History comes from the MegaNode enhanced API `nr_getAssetTransfers` (native,
internal and BEP-20 transfers, JSON-RPC 2.0). Gas fees come from the receipts of
the transactions you sent. Live and past balances use standard JSON-RPC on the
same endpoint (eth_getBalance / balanceOf), so the year-end reconstruction is
cross-checked against chain state where the plan allows archive reads.

The field names follow NodeReal's reference; parsing is tolerant (hex or decimal
numbers, optional decimals) because the documentation examples vary.
"""
from __future__ import annotations

from datetime import datetime
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

    ALCHEMY_URL = "https://bnb-mainnet.g.alchemy.com/v2/"

    def _key(self) -> Optional[str]:
        return self.secret(f"{self.network.explorer_key or 'nodereal'}_api_key")

    def provider(self) -> Optional[str]:
        """'alchemy', 'nodereal' or None (no key)."""
        custom = self.secret(f"rpc_url:{self.network.id}")
        if custom:
            return "alchemy" if "alchemy.com" in custom else "nodereal"
        if self.secret("alchemy_api_key"):
            return "alchemy"
        if self._key():
            return "nodereal"
        return None

    def _rpc_url(self) -> Optional[str]:
        custom = self.secret(f"rpc_url:{self.network.id}")
        if custom:
            return custom
        if self.secret("alchemy_api_key"):
            return self.ALCHEMY_URL + self.secret("alchemy_api_key")
        key = self._key()
        if not key:
            return None
        return (self.network.explorer_api or "https://bsc-mainnet.nodereal.io/v1").rstrip("/") + "/" + key

    def _call(self, method: str, params: List[Any]) -> Any:
        url = self._rpc_url()
        if not url:
            raise AdapterError("BSC API key missing (Settings → Blockchain APIs → BNB Smart Chain: Alchemy or NodeReal)")
        name = "Alchemy" if self.provider() == "alchemy" else "NodeReal"
        try:
            resp = self.ctx.http.post_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        except HttpError as e:
            if e.status in (401, 403):
                raise AdapterError(f"{name} refused the API key: copy it again from your {name} dashboard "
                                   "(for Alchemy, enable BNB Smart Chain on the app)")
            raise AdapterError(f"{self.network.name} ({name}) {method}: {e}")
        if not isinstance(resp, dict):
            raise AdapterError(f"{self.network.name} ({name}) {method}: unexpected response")
        if resp.get("error"):
            err = resp["error"]
            raise AdapterError(f"{self.network.name} ({name}) {method}: {err.get('message') if isinstance(err, dict) else err}")
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
        if self.provider() == "alchemy":
            return self._alchemy_transfers(address_field, address, from_block)
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

    def _alchemy_transfers(self, address_field: str, address: str, from_block: int) -> List[Dict[str, Any]]:
        """alchemy_getAssetTransfers, converted to the NodeReal shape used by sync()."""
        out: List[Dict[str, Any]] = []
        page_key = None
        while True:
            q: Dict[str, Any] = {"fromBlock": hex(from_block), "toBlock": "latest", address_field: address,
                                 "category": ["external", "erc20"], "order": "asc", "withMetadata": True,
                                 "excludeZeroValue": False, "maxCount": "0x3e8"}
            if page_key:
                q["pageKey"] = page_key
            res = self._call("alchemy_getAssetTransfers", [q]) or {}
            batch = res.get("transfers") or []
            for t in batch:
                n = _from_alchemy(t)
                if n:
                    out.append(n)
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
            raise AdapterError("BSC API key missing (Settings → Blockchain APIs → BNB Smart Chain: Alchemy or NodeReal)")
        infos: List[AssetInfo] = [native]
        found: Dict[str, Tuple[int, AssetInfo]] = {}
        if self.provider() == "alchemy":
            # Every token held now, without reading the history.
            found = self._alchemy_token_balances(wallet["address"].lower(), log)
            infos += [info for _, info in found.values()]
        found_keys = {info.key for _, info in found.values()}
        bal = self.balance_at(wallet, None, [native.key] + [k for k in tokens if k not in found_keys], log)
        if bal is None:
            raise AdapterError(f"{self.network.name}: live balances could not be read")
        for raw, info in found.values():
            if info.decimals is not None:
                bal[info.key] = scaled(raw, info.decimals)
        return bal, infos

    def _alchemy_token_balances(self, address: str, log: Logger) -> Dict[str, Tuple[int, AssetInfo]]:
        out: Dict[str, Tuple[int, AssetInfo]] = {}
        page_key = None
        while True:
            params: List[Any] = [address, "erc20"] + ([{"pageKey": page_key}] if page_key else [])
            res = self._call("alchemy_getTokenBalances", params) or {}
            for tb in res.get("tokenBalances") or []:
                contract = str(tb.get("contractAddress") or "").lower()
                raw = _int(tb.get("tokenBalance"))
                if not contract or tb.get("error") or not raw:
                    continue
                known = assets_mod.get(self.ctx, f"{self.network.id}:{contract}")
                builtin = assets_mod.token_asset(self.ctx, self.network, contract)
                if builtin.decimals is not None:          # built-in token list
                    info = builtin
                elif known and known.get("decimals") is not None:
                    info = assets_mod.token_asset(self.ctx, self.network, contract, known["symbol"], known.get("name") or "",
                                                  int(known["decimals"]))
                else:
                    try:
                        meta = self._call("alchemy_getTokenMetadata", [contract]) or {}
                    except AdapterError as e:
                        log(f"token {contract}: metadata not read ({e})")
                        meta = {}
                    dec = meta.get("decimals")
                    info = assets_mod.token_asset(self.ctx, self.network, contract, str(meta.get("symbol") or ""),
                                                  str(meta.get("name") or ""), int(dec) if dec is not None else None)
                out[contract] = (raw, info)
            page_key = res.get("pageKey")
            if not page_key:
                return out


def _from_alchemy(t: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    cat = {"external": "external", "internal": "internal", "erc20": "20"}.get(str(t.get("category") or ""))
    if not cat:
        return None   # NFTs are not valued
    raw_c = t.get("rawContract") or {}
    raw = raw_c.get("value")
    dec = raw_c.get("decimal")
    if raw in (None, ""):
        # Some transfers carry only the scaled value: rebuild the raw integer.
        v, d = t.get("value"), _int(dec, -1)
        if v is None or d < 0:
            return None
        raw = hex(int(Decimal(str(v)) * (Decimal(10) ** d)))
    ts = 0
    stamp = (t.get("metadata") or {}).get("blockTimestamp")
    if stamp:
        try:
            ts = int(datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp())
        except ValueError:
            ts = 0
    uid = str(t.get("uniqueId") or "")
    log_index = uid.split(":log:", 1)[1] if ":log:" in uid else None
    return {"category": cat, "blockNum": t.get("blockNum"), "from": t.get("from"), "to": t.get("to"),
            "value": raw, "asset": t.get("asset"), "hash": t.get("hash"), "blockTimeStamp": ts,
            "contractAddress": raw_c.get("address") if cat == "20" else None,
            "decimal": dec if cat == "20" else None, "logIndex": log_index}
