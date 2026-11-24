"""Bitcoin and Litecoin via an Esplora API (mempool.space / litecoinspace.org by default,
blockstream.info or your own node work too).

A wallet is one address, or (Bitcoin) a whole account given by its extended public key
(xpub / ypub / zpub, see hdwallet.py): its receive and change addresses are derived on your
computer and scanned until GAP unused addresses in a row, the standard wallet "gap limit".
These coins are UTXO-based, so summing every confirmed transaction gives the exact balance at
any moment; with an account, each transaction is netted over all its addresses, so coins sent
to your own change address are not a payment.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Iterable, List, Set, Tuple, Union

from .. import assets as assets_mod
from .. import hdwallet
from ..http import HttpError
from ..util import scaled
from . import register
from . import utxo_address
from .base import Adapter, AdapterError, AssetInfo, Logger, Movement, SyncResult

CHAIN_PAGE = 25
GAP = 20          # unused addresses in a row before a chain is considered finished


@register
class BitcoinAdapter(Adapter):
    id = "bitcoin"

    def validate_address(self, address: str) -> str:
        if hdwallet.looks_extended(address):
            if (self.network.address_format or "bitcoin") != "bitcoin":
                raise ValueError(f"extended public keys are supported for Bitcoin only: add {self.network.name} "
                                 "addresses one by one")
            return hdwallet.validate(address)
        return utxo_address.validate(self.network.address_format or "bitcoin", address)

    @staticmethod
    def is_account(wallet: Dict[str, Any]) -> bool:
        return hdwallet.looks_extended(wallet.get("address") or "")

    @property
    def decimals(self) -> int:
        return int((self.network.native or {}).get("decimals") or 8)

    def _get(self, path: str) -> Any:
        try:
            return self.ctx.http.get_json(self.network.explorer_api.rstrip("/") + path)
        except HttpError as e:
            raise AdapterError(f"{self.network.name} explorer: {e}")

    def parse_transaction(self, tx: Dict[str, Any], addr: Union[str, Iterable[str]]) -> List[Movement]:
        """Movements of one transaction for an address, or for a set of addresses (an account)."""
        status = tx.get("status") or {}
        if not status.get("confirmed"):
            return []
        own: Set[str] = {addr} if isinstance(addr, str) else set(addr)
        h = tx["txid"]
        ts = int(status.get("block_time") or 0)
        key = f"{self.network.id}:native"
        received = sum(int(o.get("value") or 0) for o in tx.get("vout") or []
                       if o.get("scriptpubkey_address") in own)
        inputs = tx.get("vin") or []
        spent = sum(int((i.get("prevout") or {}).get("value") or 0) for i in inputs
                    if (i.get("prevout") or {}).get("scriptpubkey_address") in own)
        all_inputs_ours = bool(inputs) and all(
            (i.get("prevout") or {}).get("scriptpubkey_address") in own for i in inputs)
        mine_in = next(((i.get("prevout") or {}).get("scriptpubkey_address") for i in inputs
                        if (i.get("prevout") or {}).get("scriptpubkey_address") in own), "")
        mine_out = next((o.get("scriptpubkey_address") for o in tx.get("vout") or []
                         if o.get("scriptpubkey_address") in own), "")
        addr = mine_in or mine_out or next(iter(own))
        fee = int(tx.get("fee") or 0) if all_inputs_ours else 0
        delta = received - spent
        moves: List[Movement] = []
        if fee:
            moves.append(Movement(uid=f"{h}:fee", tx_hash=h, ts=ts, asset_key=key,
                                  amount=-scaled(fee, self.decimals), kind="fee", from_address=addr))
        transfer = delta + fee
        if transfer:
            others = [o.get("scriptpubkey_address") for o in tx.get("vout") or []
                      if o.get("scriptpubkey_address") and o.get("scriptpubkey_address") not in own]
            senders = [(i.get("prevout") or {}).get("scriptpubkey_address") for i in inputs]
            counterparty = (others[0] if transfer < 0 and others else
                            next((s for s in senders if s and s not in own), ""))
            cp = counterparty or ""
            moves.append(Movement(uid=f"{h}:btc", tx_hash=h, ts=ts, asset_key=key,
                                  amount=scaled(transfer, self.decimals), kind="in" if transfer > 0 else "out",
                                  counterparty=cp, from_address=cp if transfer > 0 else addr,
                                  to_address=(mine_out or addr) if transfer > 0 else cp))
        return moves

    # --- accounts (extended public keys) ---------------------------------------------------
    def _address_info(self, a: str) -> Dict[str, int]:
        info = self._get(f"/address/{a}") or {}
        cs, ms = info.get("chain_stats") or {}, info.get("mempool_stats") or {}
        return {"txs": int(cs.get("tx_count") or 0), "funded": int(cs.get("funded_txo_sum") or 0),
                "spent": int(cs.get("spent_txo_sum") or 0), "pending": int(ms.get("tx_count") or 0)}

    def _discover(self, wallet: Dict[str, Any], state: Dict[str, Any],
                  log: Logger) -> Tuple[Dict[str, Dict[str, int]], List[str]]:
        """Used addresses of the account ({address: stats}) and its address types. Each chain
        (receive 0, change 1) is read until GAP unused addresses in a row; a type whose first
        GAP receive addresses are unused is not used (an "xpub" can stand for four types)."""
        cached = getattr(self, "_account", None)
        if cached and cached[0] == wallet["address"]:
            return cached[1], cached[2]
        key = hdwallet.ExtendedKey(wallet["address"])
        types = [t for t in (state.get("hd") or {}).get("types") or [] if t in key.types] or list(key.types)
        used: Dict[str, Dict[str, int]] = {}
        found: List[str] = []
        for t in types:
            in_use = False
            for chain in (0, 1):
                misses, i = 0, 0
                while misses < GAP:
                    a = key.address(t, chain, i)
                    info = self._address_info(a)
                    if info["txs"] or info["pending"]:
                        used[a], misses, in_use = info, 0, True
                    else:
                        misses += 1
                    i += 1
                if chain == 0 and not in_use:
                    break        # never received: no change addresses either
            if in_use:
                found.append(t)
        if not found:
            found = types if len(types) == 1 else []
        log(f"{wallet['label']}: {len(used)} addresses in use"
            + (f" ({', '.join(hdwallet.LABELS[t] for t in found)})" if found else " (none yet)"))
        self._account = (wallet["address"], used, found)
        return used, found

    def _confirmed_txs(self, a: str, wanted: int) -> List[Dict[str, Any]]:
        """The newest `wanted` confirmed transactions of an address (all of them if wanted < 0)."""
        out: List[Dict[str, Any]] = []
        page = self._get(f"/address/{a}/txs")
        while True:
            confirmed = [t for t in page if (t.get("status") or {}).get("confirmed")]
            out.extend(confirmed)
            if (0 <= wanted <= len(out)) or len(confirmed) < CHAIN_PAGE:
                break
            page = self._get(f"/address/{a}/txs/chain/{confirmed[-1]['txid']}")
        return out if wanted < 0 else out[:wanted]

    def _sync_account(self, wallet: Dict[str, Any], state: Dict[str, Any], log: Logger) -> SyncResult:
        result = SyncResult(state=dict(state))
        used, types = self._discover(wallet, state, log)
        seen: Dict[str, int] = dict((state.get("hd") or {}).get("seen") or {})
        txs: Dict[str, Dict[str, Any]] = {}
        for a, info in used.items():
            new = info["txs"] - int(seen.get(a) or 0)
            if new > 0:
                for t in self._confirmed_txs(a, new if a in seen else -1):
                    txs[t["txid"]] = t
            seen[a] = info["txs"]
        log(f"{wallet['label']}: {len(txs)} new {self.network.name} transactions")
        for t in txs.values():
            result.movements.extend(self.parse_transaction(t, used.keys()))
        result.state["hd"] = {"types": types, "seen": seen}
        result.assets = [assets_mod.native_asset(self.network)]
        return result

    def sync(self, wallet: Dict[str, Any], state: Dict[str, Any], log: Logger) -> SyncResult:
        if self.is_account(wallet):
            return self._sync_account(wallet, state, log)
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
        native = assets_mod.native_asset(self.network)
        if self.is_account(wallet):
            used, _ = self._discover(wallet, self.ctx.sync_state(wallet["id"]) or {}, log)
            sats = sum(i["funded"] - i["spent"] for i in used.values())
            return {native.key: scaled(sats, self.decimals)}, [native]
        info = self._get(f"/address/{wallet['address']}")
        cs = info.get("chain_stats") or {}
        sats = int(cs.get("funded_txo_sum") or 0) - int(cs.get("spent_txo_sum") or 0)
        return {native.key: scaled(sats, self.decimals)}, [native]
