"""Stellar via a Horizon server (public horizon.stellar.org by default, no key).

History comes from the account's *effects* (every balance change: payments, path
payments, account creation, DEX trades) joined with its *operations* (transaction
hash, counterparty) and the fee of each transaction the account paid. Live balances
come from /accounts/{id}. Assets are XLM ("stellar:native") and issued assets
("stellar:<CODE>-<ISSUER>"): an issued asset is identified by its issuer, never by
its code alone, so a fake "USDC" from another issuer is not priced as Circle's USDC.

Not covered yet (flagged as warnings): liquidity-pool deposits and withdrawals,
claimable balances and account merges. Year-end balances come from the history; a
live snapshot near the cutoff cross-checks it.
"""
from __future__ import annotations

import base64
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from .. import assets as assets_mod
from ..http import HttpError
from ..util import D, ZERO
from . import register
from .base import Adapter, AdapterError, AssetInfo, Logger, Movement, SyncResult

PAGE = 200
STROOP = Decimal("0.0000001")
HANDLED = {"account_created", "account_credited", "account_debited", "trade"}
UNHANDLED_WARN = {"liquidity_pool_deposited", "liquidity_pool_withdrew", "claimable_balance_created",
                  "claimable_balance_claimed", "account_removed"}


def _crc16_xmodem(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def valid_account_id(address: str) -> bool:
    """Stellar public key (StrKey "G…", 56 characters, with checksum)."""
    if len(address) != 56 or not address.startswith("G"):
        return False
    try:
        raw = base64.b32decode(address)
    except (ValueError, TypeError):
        return False
    if len(raw) != 35 or raw[0] != 6 << 3:
        return False
    return _crc16_xmodem(raw[:-2]).to_bytes(2, "little") == raw[-2:]


@register
class StellarAdapter(Adapter):
    id = "stellar"

    def validate_address(self, address: str) -> str:
        address = address.strip().upper()
        if address.startswith("M"):
            raise ValueError("muxed addresses (M…) are not supported: use the G… account address")
        if not valid_account_id(address):
            raise ValueError("not a Stellar account address (G…, 56 characters)")
        return address

    # --- API --------------------------------------------------------------------
    def _base(self) -> str:
        return (self.network.explorer_api or "https://horizon.stellar.org").rstrip("/")

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        try:
            return self.ctx.http.get_json(self._base() + path, params or {})
        except HttpError as e:
            raise AdapterError(f"Stellar Horizon: {e}")

    def _records(self, path: str, cursor: str, extra: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        while True:
            params = dict(extra or {}, order="asc", limit=PAGE)
            if cursor:
                params["cursor"] = cursor
            try:
                data = self._get(path, params)
            except AdapterError as e:
                if "HTTP 404" in str(e):
                    return out   # account not funded yet
                raise
            batch = ((data or {}).get("_embedded") or {}).get("records") or []
            out.extend(batch)
            if len(batch) < PAGE:
                return out
            cursor = batch[-1].get("paging_token") or ""

    def asset_key(self, rec: Dict[str, Any], prefix: str = "") -> Tuple[Optional[str], Optional[AssetInfo]]:
        atype = rec.get(f"{prefix}asset_type")
        if atype == "native":
            native = assets_mod.native_asset(self.network)
            return native.key, native
        code, issuer = rec.get(f"{prefix}asset_code"), rec.get(f"{prefix}asset_issuer")
        if atype in ("credit_alphanum4", "credit_alphanum12") and code and issuer:
            info = assets_mod.token_asset(self.ctx, self.network, f"{code}-{issuer}", code,
                                          f"{code} (issuer {issuer[:4]}…{issuer[-4:]})", 7)
            return info.key, info
        return None, None   # liquidity-pool shares and unknown types are not tracked

    # --- sync -------------------------------------------------------------------
    def sync(self, wallet: Dict[str, Any], state: Dict[str, Any], log: Logger) -> SyncResult:
        result = SyncResult(state=dict(state))
        me = wallet["address"]
        log(f"{wallet['label']}: reading Stellar effects and operations")
        effects = self._records(f"/accounts/{me}/effects", str(state.get("effects_cursor") or ""))
        ops = self._records(f"/accounts/{me}/operations", str(state.get("ops_cursor") or ""),
                            {"join": "transactions", "include_failed": "true"})
        log(f"{wallet['label']}: {len(effects)} effects, {len(ops)} operations")
        by_op = {str(o.get("id")): o for o in ops}
        # An effect whose operation was read in an earlier sync (the two lists have their own
        # cursors): read that operation, so the movement keeps its hash and counterparty.
        for op_id in list(dict.fromkeys(str(e.get("id") or e.get("paging_token")).split("-")[0] for e in effects
                                         if e.get("type") in HANDLED))[:100]:
            if op_id and op_id not in by_op:
                try:
                    by_op[op_id] = self._get(f"/operations/{op_id}", {"join": "transactions"}) or {}
                except AdapterError:
                    pass
        native = assets_mod.native_asset(self.network)
        assets: Dict[str, AssetInfo] = {native.key: native}
        moves: List[Movement] = []
        unhandled: Dict[str, int] = {}

        for e in effects:
            etype = e.get("type")
            if etype not in HANDLED:
                if etype in UNHANDLED_WARN:
                    unhandled[etype] = unhandled.get(etype, 0) + 1
                continue
            eid = str(e.get("id") or e.get("paging_token"))
            op = by_op.get(eid.split("-")[0]) or {}
            tx = op.get("transaction") or {}
            h = str(op.get("transaction_hash") or tx.get("hash") or "")
            ts = _ts(e.get("created_at"))
            info = op.get("type") or ""
            frm, to = str(op.get("from") or op.get("funder") or ""), str(op.get("to") or op.get("account") or "")
            other = to if frm == me else frm
            if etype == "account_created":
                moves.append(Movement(uid=f"{eid}:in", tx_hash=h, ts=ts, asset_key=native.key,
                                      amount=D(e.get("starting_balance")), kind="in", counterparty=other,
                                      info="create_account", from_address=frm, to_address=me))
            elif etype in ("account_credited", "account_debited"):
                key, ainfo = self.asset_key(e)
                if not key:
                    continue
                assets[key] = ainfo
                amount = D(e.get("amount"))
                incoming = etype == "account_credited"
                moves.append(Movement(uid=f"{eid}:{'in' if incoming else 'out'}", tx_hash=h, ts=ts, asset_key=key,
                                      amount=amount if incoming else -amount, kind="in" if incoming else "out",
                                      counterparty=other, info=info,
                                      from_address=frm if incoming else me, to_address=me if incoming else to))
            elif etype == "trade":
                # A path payment you sent is already counted by its credited/debited effects.
                if str(op.get("type") or "").startswith("path_payment") and op.get("source_account") == me:
                    continue
                sold, s_info = self.asset_key(e, "sold_")
                bought, b_info = self.asset_key(e, "bought_")
                if not sold or not bought:
                    continue
                assets[sold], assets[bought] = s_info, b_info
                seller = str(e.get("seller") or "")
                moves.append(Movement(uid=f"{eid}:out", tx_hash=h, ts=ts, asset_key=sold,
                                      amount=-D(e.get("sold_amount")), kind="out", counterparty=seller, info="trade"))
                moves.append(Movement(uid=f"{eid}:in", tx_hash=h, ts=ts, asset_key=bought,
                                      amount=D(e.get("bought_amount")), kind="in", counterparty=seller, info="trade"))

        # Network fees of the transactions this account paid (once per transaction), failed ones
        # included: a failed transaction moves nothing but its fee is still charged.
        seen_tx = set()
        for o in ops:
            tx = o.get("transaction") or {}
            h = str(o.get("transaction_hash") or tx.get("hash") or "")
            payer = tx.get("fee_account") or tx.get("source_account")
            if not h or h in seen_tx or payer != me or not tx.get("fee_charged"):
                continue
            seen_tx.add(h)
            moves.append(Movement(uid=f"{h}:fee", tx_hash=h, ts=_ts(tx.get("created_at") or o.get("created_at")),
                                  asset_key=native.key, amount=-(D(tx.get("fee_charged")) * STROOP), kind="fee",
                                  failed=o.get("transaction_successful") is False or tx.get("successful") is False,
                                  from_address=me))

        for etype, n in sorted(unhandled.items()):
            result.warnings.append(f"Stellar: {n} '{etype}' effect(s) not imported (not supported yet): "
                                   "check the balance with a snapshot or a manual entry")
        result.movements = moves
        result.assets = list(assets.values())
        if effects:
            result.state["effects_cursor"] = effects[-1].get("paging_token")
        if ops:
            result.state["ops_cursor"] = ops[-1].get("paging_token")
        return result

    # --- live balances ----------------------------------------------------------
    def current_balances(self, wallet: Dict[str, Any], known_assets: List[str],
                         log: Logger) -> Tuple[Dict[str, Decimal], List[AssetInfo]]:
        native = assets_mod.native_asset(self.network)
        out: Dict[str, Decimal] = {native.key: ZERO}
        infos: List[AssetInfo] = [native]
        try:
            acct = self._get(f"/accounts/{wallet['address']}")
        except AdapterError as e:
            if "HTTP 404" in str(e):
                acct = {}   # not funded yet
            else:
                raise
        for b in (acct or {}).get("balances") or []:
            key, info = self.asset_key(b)
            if not key:
                continue
            out[key] = out.get(key, ZERO) + D(b.get("balance"))
            if info.key != native.key:
                infos.append(info)
        for key in known_assets:
            if key.startswith(self.network.id + ":") and key not in out:
                out[key] = ZERO
        return out, infos


def _ts(text: Any) -> int:
    from datetime import datetime
    if not text:
        return 0
    return int(datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp())
