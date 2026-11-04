"""Solar Network (SXP), an ARK Core fork, through its public REST API (no key).

The API URL is editable in Settings → Networks: Solar is winding down and its
public endpoints may move. Amounts are in base units, 1 SXP = 100,000,000.

Transfers (single or multi-recipient), burns and fees are read; other
transaction types (votes, registrations) only cost a fee and are recorded so.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Dict, List, Tuple

from .. import assets as assets_mod
from ..b58 import b58check_decode
from ..http import HttpError
from ..util import scaled
from . import register
from .base import Adapter, AdapterError, AssetInfo, Logger, Movement, SyncResult

ADDRESS_RE = re.compile(r"^S[1-9A-HJ-NP-Za-km-z]{33}$")
PAGE = 100
DECIMALS = 8


def _ts(tx: Dict[str, Any]) -> int:
    t = tx.get("timestamp")
    if isinstance(t, dict):
        return int(t.get("unix") or 0)
    return int(t or 0)


def _recipients(tx: Dict[str, Any]) -> List[Tuple[str, int]]:
    """(recipient, amount in base units) for every payment in the transaction."""
    asset = tx.get("asset") or {}
    legs = asset.get("transfers") or asset.get("payments")
    if legs:
        return [(p.get("recipientId") or p.get("recipient") or "", int(p.get("amount") or 0)) for p in legs]
    if tx.get("recipient") or tx.get("recipientId"):
        return [(tx.get("recipient") or tx.get("recipientId"), int(tx.get("amount") or 0))]
    return []


@register
class SolarAdapter(Adapter):
    id = "solar"

    def validate_address(self, address: str) -> str:
        address = address.strip()
        if not ADDRESS_RE.match(address):
            raise ValueError("Solar addresses start with S and are 34 characters long")
        try:
            if len(b58check_decode(address)) != 21:
                raise ValueError
        except ValueError:
            raise ValueError("Solar address checksum is not valid")
        return address

    def _get(self, path: str, params: Dict[str, Any] = None) -> Any:
        try:
            return self.ctx.http.get_json(self.network.explorer_api.rstrip("/") + path, params)
        except HttpError as e:
            raise AdapterError(f"Solar API: {e} (check the URL in Settings → Networks)")

    def parse_transaction(self, tx: Dict[str, Any], addr: str) -> Tuple[List[Movement], str]:
        h, ts = tx.get("id", ""), _ts(tx)
        native = f"{self.network.id}:native"
        sender = tx.get("sender") or tx.get("senderId") or ""
        moves: List[Movement] = []
        warning = ""
        recipients = _recipients(tx)
        is_transfer = bool(recipients)
        # A non-transfer that carries an amount (e.g. a burn) takes that amount out of the wallet.
        is_burn = not is_transfer and int(tx.get("amount") or 0) > 0
        if sender == addr:
            fee = int(tx.get("fee") or 0)
            first_to = recipients[0][0] if recipients else ""
            if fee:
                moves.append(Movement(uid=f"{h}:fee", tx_hash=h, ts=ts, asset_key=native, amount=-scaled(fee, DECIMALS),
                                      kind="fee", counterparty=first_to, from_address=addr, to_address=first_to))
            sent = sum(a for r, a in recipients if r != addr)
            if sent:
                moves.append(Movement(uid=f"{h}:out", tx_hash=h, ts=ts, asset_key=native,
                                      amount=-scaled(sent, DECIMALS), kind="out", counterparty=first_to,
                                      from_address=addr, to_address=first_to,
                                      info=f"{len(recipients)} recipients" if len(recipients) > 1 else ""))
            if is_burn and int(tx.get("amount") or 0):
                moves.append(Movement(uid=f"{h}:burn", tx_hash=h, ts=ts, asset_key=native,
                                      amount=-scaled(int(tx["amount"]), DECIMALS), kind="out", info="burn",
                                      from_address=addr))
            elif not is_transfer:
                warning = f"{h}: transaction type {tx.get('typeGroup')}/{tx.get('type')} recorded as fee only"
        received = sum(a for r, a in recipients if r == addr and sender != addr)
        if received:
            moves.append(Movement(uid=f"{h}:in", tx_hash=h, ts=ts, asset_key=native,
                                  amount=scaled(received, DECIMALS), kind="in", counterparty=sender,
                                  from_address=sender, to_address=addr))
        return moves, warning

    def sync(self, wallet: Dict[str, Any], state: Dict[str, Any], log: Logger) -> SyncResult:
        addr = wallet["address"]
        result = SyncResult(state=dict(state))
        known = state.get("newest_id")
        txs: List[Dict[str, Any]] = []
        page = 1
        while True:                                   # newest first; stop at the last one already read
            data = self._get(f"/wallets/{addr}/transactions", {"page": page, "limit": PAGE}) or {}
            rows = data.get("data") or []
            stop = False
            for tx in rows:
                if tx.get("id") == known:
                    stop = True
                    break
                txs.append(tx)
            if stop or len(rows) < PAGE or not (data.get("meta") or {}).get("next"):
                break
            page += 1
        log(f"{wallet['label']}: {len(txs)} new Solar transactions")
        for tx in reversed(txs):
            moves, warning = self.parse_transaction(tx, addr)
            result.movements.extend(moves)
            if warning:
                result.warnings.append(warning)
        if txs:
            result.state["newest_id"] = txs[0].get("id")
        result.assets = [assets_mod.native_asset(self.network)]
        return result

    def current_balances(self, wallet: Dict[str, Any], known_assets: List[str],
                         log: Logger) -> Tuple[Dict[str, Decimal], List[AssetInfo]]:
        data = (self._get(f"/wallets/{wallet['address']}") or {}).get("data") or {}
        native = assets_mod.native_asset(self.network)
        return {native.key: scaled(int(data.get("balance") or 0), DECIMALS)}, [native]
