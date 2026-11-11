"""Bitcoin and Litecoin via an Esplora API (mempool.space / litecoinspace.org by default,
blockstream.info or your own node work too).

One wallet = one address. These coins are UTXO-based, so summing every confirmed
transaction gives the exact balance at any moment.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Tuple

from .. import assets as assets_mod
from ..http import HttpError
from ..util import scaled
from . import register
from . import utxo_address
from .base import Adapter, AdapterError, AssetInfo, Logger, Movement, SyncResult

CHAIN_PAGE = 25


@register
class BitcoinAdapter(Adapter):
    id = "bitcoin"

    def validate_address(self, address: str) -> str:
        return utxo_address.validate(self.network.address_format or "bitcoin", address)

    @property
    def decimals(self) -> int:
        return int((self.network.native or {}).get("decimals") or 8)

    def _get(self, path: str) -> Any:
        try:
            return self.ctx.http.get_json(self.network.explorer_api.rstrip("/") + path)
        except HttpError as e:
            raise AdapterError(f"{self.network.name} explorer: {e}")

    def parse_transaction(self, tx: Dict[str, Any], addr: str) -> List[Movement]:
        status = tx.get("status") or {}
        if not status.get("confirmed"):
            return []
        h = tx["txid"]
        ts = int(status.get("block_time") or 0)
        key = f"{self.network.id}:native"
        received = sum(int(o.get("value") or 0) for o in tx.get("vout") or []
                       if o.get("scriptpubkey_address") == addr)
        inputs = tx.get("vin") or []
        spent = sum(int((i.get("prevout") or {}).get("value") or 0) for i in inputs
                    if (i.get("prevout") or {}).get("scriptpubkey_address") == addr)
        all_inputs_ours = bool(inputs) and all(
            (i.get("prevout") or {}).get("scriptpubkey_address") == addr for i in inputs)
        fee = int(tx.get("fee") or 0) if all_inputs_ours else 0
        delta = received - spent
        moves: List[Movement] = []
        if fee:
            moves.append(Movement(uid=f"{h}:fee", tx_hash=h, ts=ts, asset_key=key,
                                  amount=-scaled(fee, self.decimals), kind="fee", from_address=addr))
        transfer = delta + fee
        if transfer:
            others = [o.get("scriptpubkey_address") for o in tx.get("vout") or []
                      if o.get("scriptpubkey_address") and o.get("scriptpubkey_address") != addr]
            senders = [(i.get("prevout") or {}).get("scriptpubkey_address") for i in inputs]
            counterparty = (others[0] if transfer < 0 and others else
                            next((s for s in senders if s and s != addr), ""))
            cp = counterparty or ""
            moves.append(Movement(uid=f"{h}:btc", tx_hash=h, ts=ts, asset_key=key,
                                  amount=scaled(transfer, self.decimals), kind="in" if transfer > 0 else "out",
                                  counterparty=cp, from_address=cp if transfer > 0 else addr,
                                  to_address=addr if transfer > 0 else cp))
        return moves

    def sync(self, wallet: Dict[str, Any], state: Dict[str, Any], log: Logger) -> SyncResult:
        addr = wallet["address"]
        result = SyncResult(state=dict(state))
        known_tip = state.get("newest_txid")
        txs: List[Dict[str, Any]] = []
        page = self._get(f"/address/{addr}/txs")
        reached = False
        while True:
            confirmed = [t for t in page if (t.get("status") or {}).get("confirmed")]
            for t in confirmed:
                if t["txid"] == known_tip:
                    reached = True
                    break
                txs.append(t)
            if reached or len(confirmed) < CHAIN_PAGE:
                break
            page = self._get(f"/address/{addr}/txs/chain/{confirmed[-1]['txid']}")
        log(f"{wallet['label']}: {len(txs)} new {self.network.name} transactions")
        for t in txs:
            result.movements.extend(self.parse_transaction(t, addr))
        if txs:
            result.state["newest_txid"] = txs[0]["txid"]
        result.assets = [assets_mod.native_asset(self.network)]
        return result

    def current_balances(self, wallet: Dict[str, Any], known_assets: List[str],
                         log: Logger) -> Tuple[Dict[str, Decimal], List[AssetInfo]]:
        info = self._get(f"/address/{wallet['address']}")
        cs = info.get("chain_stats") or {}
        sats = int(cs.get("funded_txo_sum") or 0) - int(cs.get("spent_txo_sum") or 0)
        native = assets_mod.native_asset(self.network)
        return {native.key: scaled(sats, self.decimals)}, [native]
