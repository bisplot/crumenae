"""EVM chains through Alchemy (free plan, sign-up with e-mail or Google): BNB Smart
Chain, Monad, Arc, and any chain Alchemy serves. One key covers them all.

The network config names the Alchemy subdomain ("alchemy_network", e.g. "bnb-mainnet"
→ https://bnb-mainnet.g.alchemy.com/v2/<key>), or the provider's base URL contains
"{network}". History: alchemy_getAssetTransfers (native and ERC-20; internal
transfers only where Alchemy offers them: "alchemy_internal": true). Gas: receipts of
the transactions you sent. Tokens held now: alchemy_getTokenBalances. Balances at a
past block (year-end cross-check): standard JSON-RPC on the same endpoint.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from .. import assets as assets_mod
from ..http import HttpError
from ..util import scaled
from . import evm_staking, register
from .base import AdapterError, AssetInfo, Logger, Movement, SyncResult
from .evm import BALANCE_OF, EvmAdapter, resume_block

DECIMALS = "0x313ce567"    # decimals()
looks_like_spam = assets_mod.looks_like_spam
ALCHEMY_TEMPLATE = "https://{network}.g.alchemy.com/v2"


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
class AlchemyAdapter(EvmAdapter):
    id = "alchemy"

    @property
    def has_history(self) -> bool:  # type: ignore[override]
        return True

    def validate_address(self, address: str) -> str:
        address = address.strip()
        if address.lower().startswith("ronin:"):      # Ronin Wallet shows "ronin:<hex>"
            address = "0x" + address.split(":", 1)[1]
        return super().validate_address(address)

    def _key(self) -> Optional[str]:
        return self.secret(f"{self.network.explorer_key or 'alchemy'}_api_key")

    def provider(self) -> Optional[str]:
        return "alchemy" if self._rpc_url() else None

    def _rpc_url(self) -> Optional[str]:
        custom = self.secret(f"rpc_url:{self.network.id}")
        if custom:
            return custom
        key = self._key()
        if not key:
            return None
        base = self.network.explorer_api or ALCHEMY_TEMPLATE
        sub = self.network.alchemy_network or ""
        return base.replace("{network}", sub).rstrip("/") + "/" + key

    def _call(self, method: str, params: List[Any]) -> Any:
        url = self._rpc_url()
        if not url:
            raise AdapterError(f"Alchemy API key missing (Settings → API providers) for {self.network.name}")
        try:
            resp = self.ctx.http.post_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        except HttpError as e:
            if e.status in (401, 403):
                raise AdapterError(f"Alchemy refused the API key for {self.network.name}: copy it again from your "
                                   "Alchemy dashboard and enable this chain on the app")
            raise AdapterError(f"{self.network.name} (Alchemy) {method}: {e}")
        if not isinstance(resp, dict):
            raise AdapterError(f"{self.network.name} (Alchemy) {method}: unexpected response")
        if resp.get("error"):
            err = resp["error"]
            raise AdapterError(f"{self.network.name} (Alchemy) {method}: {err.get('message') if isinstance(err, dict) else err}")
        return resp.get("result")

    def _ignored(self) -> set:
        """Token contracts that only mirror the native coin (e.g. Arc's USDC ERC-20 interface):
        counting them too would double the balance."""
        return {str(c).lower() for c in (self.network.ignore_tokens or [])}

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
        return self._alchemy_transfers(address_field, address, from_block)

    def _alchemy_transfers(self, address_field: str, address: str, from_block: int) -> List[Dict[str, Any]]:
        """alchemy_getAssetTransfers, converted to the transfer shape used by sync()."""
        out: List[Dict[str, Any]] = []
        page_key = None
        while True:
            q: Dict[str, Any] = {"fromBlock": hex(from_block), "toBlock": "latest", address_field: address,
                                 "category": ["external", "erc20"] + (["internal"] if self.network.alchemy_internal else []),
                                 "order": "asc", "withMetadata": True,
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
        start = resume_block(state, "alchemy")
        native = assets_mod.native_asset(self.network)
        nat_dec = native.decimals or 18
        log(f"{wallet['label']}: reading {self.network.name} transfers from block {start} (Alchemy)")
        rows: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
        try:
            for field in ("fromAddress", "toAddress"):
                for t in self._transfers(field, addr, start):
                    k = (str(t.get("hash", "")).lower(), t.get("category"), str(t.get("from", "")).lower(),
                         str(t.get("to", "")).lower(), str(t.get("contractAddress", "")).lower(), str(t.get("value")),
                         t.get("logIndex"), t.get("traceIndex") or t.get("traceAddress"))
                    rows[k] = t   # a transfer to yourself comes back in both queries
        except AdapterError as e:
            if _key_problem(e):
                raise
            # The provider answers but has no transfers API for this chain: keep going with the
            # live balances (read next by the sync), and say why the history is missing.
            result.warnings.append(f"{self.network.name}: transaction history not available from this provider "
                                   f"({e}). Live balances are still read; year-end values need a snapshot near "
                                   "the cutoff or a manual entry.")
            log(f"{wallet['label']}: ⚠ no transaction history ({e}); reading live balances only")
            return result
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
                if not contract or contract in self._ignored():
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
                if asset.status == "unmapped" and looks_like_spam(asset.symbol, asset.name):
                    asset.status = "spam"
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
        result.state["reader"] = "alchemy"
        evm_staking.ContractStaking(self).sync(wallet, result, moves, log)
        return result

    # --- live balances ------------------------------------------------------------
    def current_balances(self, wallet: Dict[str, Any], known_assets: List[str],
                         log: Logger) -> Tuple[Dict[str, Decimal], List[AssetInfo]]:
        native = assets_mod.native_asset(self.network)
        tokens = [k for k in known_assets if k != native.key and k.startswith(self.network.id + ":")]
        if not self._rpc_url():
            raise AdapterError(f"Alchemy API key missing (Settings → API providers) for {self.network.name}")
        infos: List[AssetInfo] = [native]
        found: Dict[str, Tuple[int, AssetInfo]] = {}
        # Every token held now, without reading the history.
        try:
            found = self._alchemy_token_balances(wallet["address"].lower(), log)
        except AdapterError as e:
            if _key_problem(e):
                raise
            log(f"{wallet['label']}: token list not available from this provider ({e}); "
                "checking the known tokens of this chain one by one")
            found = self._builtin_token_balances(wallet["address"].lower(), tokens)
        infos += [info for _, info in found.values()]
        found_keys = {info.key for _, info in found.values()}
        bal = self.balance_at(wallet, None, [native.key] + [k for k in tokens if k not in found_keys], log)
        if bal is None:
            raise AdapterError(f"{self.network.name}: live balance of the native coin could not be read")
        for raw, info in found.values():
            if info.decimals is not None:
                bal[info.key] = scaled(raw, info.decimals)
        evm_staking.add_live(self, wallet, bal, infos, log)
        return bal, infos

    def balance_at(self, wallet: Dict[str, Any], ts: Optional[int], asset_keys: List[str],
                   log: Logger) -> Optional[Dict[str, Decimal]]:
        """Native coin and token balances at a block (ts) or now (ts None). A token whose contract
        cannot answer balanceOf (broken or self-destructed spam) is skipped, never fatal."""
        if not self._rpc_url():
            return None
        addr = wallet["address"].lower()
        native = assets_mod.native_asset(self.network)
        try:
            block = None if ts is None else self.block_at(ts)
            tag = "latest" if block is None else hex(block)
            out: Dict[str, Decimal] = {native.key: scaled(_int(self._call("eth_getBalance", [addr, tag])),
                                                          native.decimals or 18)}
            if ts is not None:   # coins in staking contracts (live: read by current_balances)
                evm_staking.add_at(self, wallet, block, asset_keys, out)
        except (AdapterError, HttpError, ValueError, TypeError) as e:
            log(f"{wallet['label']}: chain-state read failed ({e}); the node may not keep old blocks")
            return None
        decimals = {r["key"]: r["decimals"] for r in self.ctx.db.query(
            "SELECT key, decimals FROM assets WHERE network=?", (self.network.id,))}
        skipped = 0
        for key in asset_keys:
            if (key == native.key or not key.startswith(self.network.id + ":") or decimals.get(key) is None
                    or key.startswith(self.network.id + ":stake:")):
                continue
            contract = key.split(":", 1)[1]
            if contract in self._ignored():
                continue
            try:
                raw = self._call("eth_call", [{"to": contract, "data": BALANCE_OF + addr[2:].rjust(64, "0")}, tag])
            except AdapterError:
                skipped += 1
                continue
            out[key] = scaled(_int(raw) if raw and raw != "0x" else 0, decimals[key])
        if skipped:
            log(f"{wallet['label']}: {skipped} token contract(s) did not answer balanceOf and were skipped")
        return out

    def _builtin_token_balances(self, address: str, skip: List[str]) -> Dict[str, Tuple[int, AssetInfo]]:
        """balanceOf for each token of the built-in list (config/tokens.json) of this chain:
        standard JSON-RPC, available on every node."""
        out: Dict[str, Tuple[int, AssetInfo]] = {}
        for contract in (self.ctx.tokens.get(self.network.id) or {}):
            info = assets_mod.token_asset(self.ctx, self.network, contract)
            if info.key in skip or contract in self._ignored() or info.decimals is None:
                continue
            raw = self._call("eth_call", [{"to": contract, "data": BALANCE_OF + address[2:].rjust(64, "0")}, "latest"])
            value = _int(raw) if raw and raw != "0x" else 0
            if value:
                out[contract] = (value, info)
        return out

    def _alchemy_token_balances(self, address: str, log: Logger) -> Dict[str, Tuple[int, AssetInfo]]:
        out: Dict[str, Tuple[int, AssetInfo]] = {}
        page_key = None
        while True:
            params: List[Any] = [address, "erc20"] + ([{"pageKey": page_key}] if page_key else [])
            res = self._call("alchemy_getTokenBalances", params) or {}
            for tb in res.get("tokenBalances") or []:
                contract = str(tb.get("contractAddress") or "").lower()
                raw = _int(tb.get("tokenBalance"))
                if not contract or tb.get("error") or not raw or contract in self._ignored():
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
                    if info.status == "unmapped" and looks_like_spam(info.symbol, info.name):
                        info.status = "spam"
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


def _key_problem(e: Exception) -> bool:
    """Missing or refused key: the user must fix it, never hide it behind a fallback."""
    text = str(e).lower()
    return "key missing" in text or "refused the api key" in text
