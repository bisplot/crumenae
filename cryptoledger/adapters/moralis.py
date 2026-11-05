"""EVM chains through the Moralis Web3 Data API (one API key, free plan available).

Used for Ronin, whose own Skynet API is being retired by Sky Mavis in favour of
Moralis. Works for any chain Moralis supports: set "moralis_chain" (hex chain id)
in the network config.

Reads native transactions (with internal transfers), ERC-20 transfers, live
balances, and balances at a past block (used to cross-check year-end values).
"""
from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from .. import assets as assets_mod
from ..http import HttpError
from ..timeutil import parse_utc
from ..util import scaled
from . import register
from .base import Adapter, AdapterError, AssetInfo, Logger, Movement, SyncResult

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
PAGE = 100


@register
class MoralisAdapter(Adapter):
    id = "moralis"

    def validate_address(self, address: str) -> str:
        address = address.strip()
        if address.lower().startswith("ronin:"):      # Ronin Wallet shows "ronin:<hex>"
            address = "0x" + address.split(":", 1)[1]
        if not ADDRESS_RE.match(address):
            raise ValueError("address must be 0x (or ronin:) followed by 40 hex characters")
        return address.lower()

    # --- API --------------------------------------------------------------------------
    def _get(self, path: str, params: Dict[str, Any]) -> Any:
        key = self.secret(f"{self.network.explorer_key or 'moralis'}_api_key")
        if not key:
            raise AdapterError("Moralis API key missing (Settings → Blockchain APIs)")
        p = dict(params, chain=self.network.moralis_chain or hex(int(self.network.chain_id)))
        try:
            return self.ctx.http.get_json(self.network.explorer_api.rstrip("/") + path, p, {"X-API-Key": key})
        except HttpError as e:
            raise AdapterError(f"{self.network.name} (Moralis): {e}")

    def _paged(self, path: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        cursor = None
        while True:
            p = dict(params, limit=PAGE, order="ASC")
            if cursor:
                p["cursor"] = cursor
            data = self._get(path, p) or {}
            rows.extend(data.get("result") or [])
            cursor = data.get("cursor")
            if not cursor or not data.get("result"):
                return rows

    # --- sync -------------------------------------------------------------------------
    def sync(self, wallet: Dict[str, Any], state: Dict[str, Any], log: Logger) -> SyncResult:
        addr = wallet["address"].lower()
        result = SyncResult(state=dict(state))
        start = int(state.get("last_block", 0))
        native = assets_mod.native_asset(self.network)
        dec = native.decimals or 18
        txs = self._paged(f"/{addr}", {"from_block": start, "include": "internal_transactions"})
        transfers = self._paged(f"/{addr}/erc20/transfers", {"from_block": start})
        log(f"{wallet['label']}: {len(txs)} {self.network.name} transactions, {len(transfers)} token transfers")
        moves: List[Movement] = []
        seen: Dict[str, AssetInfo] = {native.key: native}
        max_block = start

        for tx in txs:
            h = (tx.get("hash") or "").lower()
            ts = parse_utc(tx["block_timestamp"])
            max_block = max(max_block, int(tx.get("block_number") or 0))
            frm = (tx.get("from_address") or "").lower()
            to = (tx.get("to_address") or "").lower()
            failed = str(tx.get("receipt_status", "1")) == "0"
            value = scaled(tx.get("value") or 0, dec)
            if frm == addr:
                fee = scaled(int(tx.get("receipt_gas_used") or 0) * int(tx.get("gas_price") or 0), dec)
                if fee:
                    moves.append(Movement(uid=f"{h}:fee", tx_hash=h, ts=ts, asset_key=native.key, amount=-fee,
                                          kind="fee", counterparty=to, failed=failed, from_address=frm, to_address=to))
                if value and not failed:
                    moves.append(Movement(uid=f"{h}:out", tx_hash=h, ts=ts, asset_key=native.key, amount=-value,
                                          kind="out", counterparty=to, from_address=frm, to_address=to))
            if to == addr and value and not failed:
                moves.append(Movement(uid=f"{h}:in", tx_hash=h, ts=ts, asset_key=native.key, amount=value,
                                      kind="in", counterparty=frm, from_address=frm, to_address=to))
            if failed:
                continue
            for i, itx in enumerate(tx.get("internal_transactions") or []):
                ifrm, ito = (itx.get("from") or "").lower(), (itx.get("to") or "").lower()
                ival = scaled(itx.get("value") or 0, dec)
                if not ival:
                    continue
                if ifrm == addr:
                    moves.append(Movement(uid=f"{h}:int:{i}:out", tx_hash=h, ts=ts, asset_key=native.key,
                                          amount=-ival, kind="out", counterparty=ito, info="internal",
                                          from_address=ifrm, to_address=ito))
                if ito == addr:
                    moves.append(Movement(uid=f"{h}:int:{i}:in", tx_hash=h, ts=ts, asset_key=native.key,
                                          amount=ival, kind="in", counterparty=ifrm, info="internal",
                                          from_address=ifrm, to_address=ito))

        counter: Dict[str, int] = defaultdict(int)
        for t in transfers:
            h = (t.get("transaction_hash") or "").lower()
            ts = parse_utc(t["block_timestamp"])
            max_block = max(max_block, int(t.get("block_number") or 0))
            contract = (t.get("address") or "").lower()
            frm, to = (t.get("from_address") or "").lower(), (t.get("to_address") or "").lower()
            key = f"{self.network.id}:{contract}"
            asset = seen.get(key) or assets_mod.token_asset(
                self.ctx, self.network, contract, t.get("token_symbol") or "", t.get("token_name") or "",
                int(t["token_decimals"]) if str(t.get("token_decimals") or "").isdigit() else None)
            if t.get("possible_spam") and asset.status == "unmapped":
                asset.status = "spam"   # Moralis flags airdropped scam tokens
            seen[key] = asset
            if asset.decimals is None:
                continue
            amount = scaled(t.get("value") or 0, asset.decimals)
            if t.get("log_index") not in (None, ""):
                base = f"{h}:log:{t['log_index']}"
            else:
                sig = f"{h}:tok:{contract}:{frm}:{to}:{t.get('value')}"
                base = f"{sig}:{counter[sig]}"
                counter[sig] += 1
            info = "zero-value transfer" if not amount else ""
            if frm == addr:
                moves.append(Movement(uid=f"{base}:out", tx_hash=h, ts=ts, asset_key=key, amount=-amount,
                                      kind="out", counterparty=to, info=info, from_address=frm, to_address=to))
            if to == addr:
                moves.append(Movement(uid=f"{base}:in", tx_hash=h, ts=ts, asset_key=key, amount=amount,
                                      kind="in", counterparty=frm, info=info, from_address=frm, to_address=to))

        result.movements = moves
        result.assets = list(seen.values())
        result.state["last_block"] = max_block
        return result

    # --- balances ---------------------------------------------------------------------
    def _balances(self, addr: str, block: Optional[int]) -> Tuple[Dict[str, Decimal], List[AssetInfo]]:
        native = assets_mod.native_asset(self.network)
        extra = {"to_block": block} if block is not None else {}
        out = {native.key: scaled((self._get(f"/{addr}/balance", extra) or {}).get("balance") or 0,
                                  native.decimals or 18)}
        infos = [native]
        for tok in self._get(f"/{addr}/erc20", extra) or []:
            contract = (tok.get("token_address") or "").lower()
            dec = tok.get("decimals")
            if not contract or dec in (None, ""):
                continue
            info = assets_mod.token_asset(self.ctx, self.network, contract, tok.get("symbol") or "",
                                          tok.get("name") or "", int(dec))
            if tok.get("possible_spam") and info.status == "unmapped":
                info.status = "spam"
            out[info.key] = scaled(tok.get("balance") or 0, info.decimals if info.decimals is not None else int(dec))
            infos.append(info)
        return out, infos

    def current_balances(self, wallet: Dict[str, Any], known_assets: List[str],
                         log: Logger) -> Tuple[Dict[str, Decimal], List[AssetInfo]]:
        out, infos = self._balances(wallet["address"].lower(), None)
        for key in known_assets:
            if key.startswith(self.network.id + ":") and key not in out:
                out[key] = Decimal(0)
        return out, infos

    def balance_at(self, wallet: Dict[str, Any], ts: int, asset_keys: List[str],
                   log: Logger) -> Optional[Dict[str, Decimal]]:
        try:
            found = self._get("/dateToBlock", {"date": ts}) or {}
            block = int(found.get("block"))
            if int(found.get("timestamp") or 0) > ts:
                block -= 1   # Moralis returns the closest block, which can be just after the cutoff
            out, _ = self._balances(wallet["address"].lower(), block)
        except (AdapterError, TypeError, ValueError) as e:
            log(f"{wallet['label']}: balance at cutoff block not available ({e})")
            return None
        return out
