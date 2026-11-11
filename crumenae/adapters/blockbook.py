"""Dogecoin and Bitcoin Cash (any Bitcoin-like coin) via a Blockbook API v2 server
(Trezor's public servers by default: doge1.trezor.io, bch1.trezor.io; any other Blockbook
instance can be set in Settings → Networks).

One wallet = one address. GET /api/v2/address/{address}?details=txs gives every transaction
with its inputs and outputs (values in base units, as strings), newest first, in pages. The
movement rules are the Bitcoin ones: one net change per transaction, the fee recorded apart only
when every input is this address, unconfirmed transactions left for the next sync.
"""
from __future__ import annotations

import urllib.parse
from decimal import Decimal
from typing import Any, Dict, List, Tuple

from .. import assets as assets_mod
from ..http import HttpError
from ..util import scaled
from . import register, utxo_address
from .base import Adapter, AdapterError, AssetInfo, Logger, Movement, SyncResult

PAGE_SIZE = 1000


def _int(v: Any) -> int:
    try:
        return int(str(v or "0"))
    except ValueError:
        return 0


@register
class BlockbookAdapter(Adapter):
    id = "blockbook"

    def validate_address(self, address: str) -> str:
        return utxo_address.validate(self.network.address_format or "bitcoin", address)

    @property
    def decimals(self) -> int:
        return int((self.network.native or {}).get("decimals") or 8)

    def _get(self, path: str, params: Dict[str, Any]) -> Any:
        base = (self.network.explorer_api or "").rstrip("/")
        if not base:
            raise AdapterError(f"{self.network.name}: no Blockbook server configured (Settings → Networks)")
        try:
            data = self.ctx.http.get_json(base + path, params)
        except HttpError as e:
            if e.status in (403, 429, 503):
                raise AdapterError(f"{self.network.name} explorer refused the request ({e.status}): try again "
                                   "later, or set another Blockbook server in Settings → Networks")
            raise AdapterError(f"{self.network.name} explorer: {e}")
        if not isinstance(data, dict):
            raise AdapterError(f"{self.network.name} explorer: unexpected answer (not JSON)")
        if data.get("error"):
            raise AdapterError(f"{self.network.name} explorer: {data['error']}")
        return data

    def _address_path(self, address: str) -> str:
        return "/api/v2/address/" + urllib.parse.quote(address, safe=":")

    def parse_transaction(self, tx: Dict[str, Any], addr: str) -> List[Movement]:
        if _int(tx.get("blockHeight")) <= 0:
            return []    # not confirmed yet
        h = str(tx["txid"])
        ts = _int(tx.get("blockTime"))
        key = f"{self.network.id}:native"

        def mine(row: Dict[str, Any]) -> bool:
            return any(utxo_address.same(a, addr) for a in row.get("addresses") or [])
        inputs = tx.get("vin") or []
        outputs = tx.get("vout") or []
        received = sum(_int(o.get("value")) for o in outputs if mine(o))
        spent = sum(_int(i.get("value")) for i in inputs if mine(i))
        all_inputs_ours = bool(inputs) and all(mine(i) for i in inputs)
        fee = _int(tx.get("fees")) if all_inputs_ours else 0
        moves: List[Movement] = []
        if fee:
            moves.append(Movement(uid=f"{h}:fee", tx_hash=h, ts=ts, asset_key=key,
                                  amount=-scaled(fee, self.decimals), kind="fee", from_address=addr))
        transfer = received - spent + fee
        if transfer:
            others = [a for o in outputs if not mine(o) for a in (o.get("addresses") or [])]
            senders = [a for i in inputs if not mine(i) for a in (i.get("addresses") or [])]
            cp = (others[0] if transfer < 0 and others else senders[0] if senders else "")
            moves.append(Movement(uid=f"{h}:coin", tx_hash=h, ts=ts, asset_key=key,
                                  amount=scaled(transfer, self.decimals), kind="in" if transfer > 0 else "out",
                                  counterparty=cp, from_address=cp if transfer > 0 else addr,
                                  to_address=addr if transfer > 0 else cp))
        return moves

    def sync(self, wallet: Dict[str, Any], state: Dict[str, Any], log: Logger) -> SyncResult:
        addr = wallet["address"]
        result = SyncResult(state=dict(state))
        known_tip = state.get("newest_txid")
        txs: List[Dict[str, Any]] = []
        page, reached = 1, False
        while not reached:
            data = self._get(self._address_path(addr), {"details": "txs", "page": page, "pageSize": PAGE_SIZE})
            batch = data.get("txs") or []
            for t in batch:
                if _int(t.get("blockHeight")) <= 0:
                    continue   # unconfirmed: read once confirmed
                if t.get("txid") == known_tip:
                    reached = True
                    break
                txs.append(t)
            if page >= _int(data.get("totalPages")) or not batch:
                break
            page += 1
        log(f"{wallet['label']}: {len(txs)} new {self.network.name} transactions")
        for t in txs:
            result.movements.extend(self.parse_transaction(t, addr))
        if txs:
            result.state["newest_txid"] = txs[0]["txid"]
        result.assets = [assets_mod.native_asset(self.network)]
        return result

    def current_balances(self, wallet: Dict[str, Any], known_assets: List[str],
                         log: Logger) -> Tuple[Dict[str, Decimal], List[AssetInfo]]:
        data = self._get(self._address_path(wallet["address"]), {"details": "basic"})
        native = assets_mod.native_asset(self.network)
        return {native.key: scaled(_int(data.get("balance")), self.decimals)}, [native]
