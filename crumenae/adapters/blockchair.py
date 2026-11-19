"""Dogecoin and Bitcoin Cash (also Litecoin, Bitcoin, Dash…) via the Blockchair API.

Public, no key needed: about 1,440 request units a day per IP address. A history page with
transaction details costs 2 units, the balance 1. A Blockchair key (optional, saved as the key
of the "blockchair" provider) raises the limit; it is sent as ?key=.

One wallet = one address. GET /{chain}/dashboards/address/{address}?transaction_details=true
gives the balance and the transactions, newest first, each with its net balance change in base
units (the fee of a payment is inside the amount sent). Pages of 100 with offset; the sync
stops at the newest transaction already read. Unconfirmed transactions (block_id -1) are left
for the next sync.
"""
from __future__ import annotations

import urllib.parse
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from .. import assets as assets_mod
from ..http import HttpError
from ..timeutil import parse_utc
from ..util import scaled
from . import register, utxo_address
from .base import Adapter, AdapterError, AssetInfo, Logger, Movement, SyncResult

DEFAULT_BASE = "https://api.blockchair.com"
PAGE = 100
MAX_PAGES = 2000
REFUSED = {402, 429, 430, 434, 435, 503}


def _int(v: Any) -> int:
    try:
        return int(str(v if v is not None else "0").split(".")[0])
    except ValueError:
        return 0


@register
class BlockchairAdapter(Adapter):
    id = "blockchair"

    def validate_address(self, address: str) -> str:
        return utxo_address.validate(self.network.address_format or "bitcoin", address)

    @property
    def decimals(self) -> int:
        return int((self.network.native or {}).get("decimals") or 8)

    @property
    def chain(self) -> str:
        return str(self.network.blockchair_chain or self.network.id.replace("_", "-"))

    def _key(self) -> Optional[str]:
        return self.secret(f"{self.network.explorer_key or 'blockchair'}_api_key")

    def _refused(self, code: int) -> AdapterError:
        how = ("the daily limit without a key is used up, or this IP address is blocked for a while"
               if code in (402, 429, 430, 434, 435) else "the service is busy")
        return AdapterError(f"Blockchair refused the request ({code}): {how}. Try again later (tomorrow for the "
                            "daily limit), or save a Blockchair key in Settings → API providers")

    def _dashboard(self, address: str, params: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        base = (self.network.explorer_api or DEFAULT_BASE).rstrip("/")
        bare = address.split(":", 1)[1] if address.lower().startswith("bitcoincash:") else address
        url = f"{base}/{self.chain}/dashboards/address/{urllib.parse.quote(bare, safe='')}"
        params = dict(params)
        key = self._key()
        if key:
            params["key"] = key
        try:
            data = self.ctx.http.get_json(url, params)
        except HttpError as e:
            if e.status in REFUSED:
                raise self._refused(e.status)
            if e.status in (401, 403) and key:
                raise AdapterError(f"Blockchair refused the key ({e.status}): check it in Settings → API providers")
            raise AdapterError(f"{self.network.name} (Blockchair): {e}")
        if not isinstance(data, dict):
            raise AdapterError(f"{self.network.name} (Blockchair): unexpected answer (not JSON)")
        context = data.get("context") or {}
        code = _int(context.get("code"))
        if code in REFUSED:
            raise self._refused(code)
        if code >= 400 or context.get("error"):
            raise AdapterError(f"{self.network.name} (Blockchair): {context.get('error') or code}")
        rows = data.get("data") or {}
        entry = next(iter(rows.values()), None) if isinstance(rows, dict) else None
        if not isinstance(entry, dict) or not isinstance(entry.get("address"), dict):
            raise AdapterError(f"{self.network.name} (Blockchair): no data for this address")
        return entry["address"], [t for t in entry.get("transactions") or [] if isinstance(t, dict)]

    def parse_transaction(self, tx: Dict[str, Any], addr: str) -> Optional[Movement]:
        if _int(tx.get("block_id")) <= 0:
            return None    # not confirmed yet
        change = _int(tx.get("balance_change"))
        if not change:
            return None
        h = str(tx["hash"])
        amount = scaled(change, self.decimals)
        return Movement(uid=f"{h}:coin", tx_hash=h, ts=parse_utc(str(tx.get("time"))),
                        asset_key=f"{self.network.id}:native", amount=amount, kind="in" if change > 0 else "out",
                        from_address="" if change > 0 else addr, to_address=addr if change > 0 else "",
                        info="" if change > 0 else "amount includes the network fee")

    def sync(self, wallet: Dict[str, Any], state: Dict[str, Any], log: Logger) -> SyncResult:
        addr = wallet["address"]
        result = SyncResult(state=dict(state))
        known_tip = state.get("newest_txid")
        txs: List[Dict[str, Any]] = []
        offset, reached = 0, False
        for _ in range(MAX_PAGES):
            info, batch = self._dashboard(addr, {"transaction_details": "true", "limit": PAGE, "offset": offset})
            for t in batch:
                if _int(t.get("block_id")) <= 0:
                    continue   # unconfirmed: read once confirmed
                if t.get("hash") == known_tip:
                    reached = True
                    break
                txs.append(t)
            offset += PAGE
            if reached or len(batch) < PAGE or offset >= _int(info.get("transaction_count")):
                break
        log(f"{wallet['label']}: {len(txs)} new {self.network.name} transactions (Blockchair)")
        seen = set()
        for t in txs:
            m = self.parse_transaction(t, addr)
            if m and m.uid not in seen:    # a transaction arriving while paging shifts the pages: read twice
                seen.add(m.uid)
                result.movements.append(m)
        if txs:
            result.state["newest_txid"] = txs[0]["hash"]
        result.assets = [assets_mod.native_asset(self.network)]
        return result

    def current_balances(self, wallet: Dict[str, Any], known_assets: List[str],
                         log: Logger) -> Tuple[Dict[str, Decimal], List[AssetInfo]]:
        info, _ = self._dashboard(wallet["address"], {"limit": 0})
        native = assets_mod.native_asset(self.network)
        return {native.key: scaled(_int(info.get("balance")), self.decimals)}, [native]
