"""Tron via TronGrid v1 (key optional: Settings → Blockchain APIs → Tron).

TRX transfers, TRC-10 transfers, TRC-20 transfers and fees are read from the
account's transaction lists. Staked (frozen) TRX still belongs to you, so
freezing/unfreezing is neutral and the live balance includes staked TRX.
Claimed voting rewards (WithdrawBalanceContract) are income: category "reward".
"""
from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal
from typing import Any, Dict, List, Tuple

from .. import assets as assets_mod
from ..b58 import b58check_decode, b58check_encode
from ..http import HttpError
from ..util import ZERO, scaled
from . import register
from .base import Adapter, AdapterError, AssetInfo, Logger, Movement, StakePosition, SyncResult

ADDRESS_RE = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")
PAGE = 200
NEUTRAL_TYPES = {
    "FreezeBalanceContract", "UnfreezeBalanceContract", "FreezeBalanceV2Contract",
    "UnfreezeBalanceV2Contract", "WithdrawExpireUnfreezeContract", "DelegateResourceContract",
    "UnDelegateResourceContract", "VoteWitnessContract", "AccountPermissionUpdateContract",
    "CancelAllUnfreezeV2Contract", "AccountCreateContract", "AccountUpdateContract",
}


def hex_to_base58(value: str) -> str:
    """'41ab…' (21 bytes hex) -> 'T…'. Base58 input is returned unchanged."""
    if not value:
        return ""
    if value.startswith("T"):
        return value
    v = value[2:] if value.startswith("0x") else value
    if len(v) == 40:
        v = "41" + v
    return b58check_encode(bytes.fromhex(v))


def base58_to_hex(address: str) -> str:
    return b58check_decode(address).hex()


def _asset_name(name: str) -> str:
    """TRC-10 ids sometimes arrive hex-encoded ('31303030303031' = '1000001')."""
    if name and re.fullmatch(r"(?:[0-9a-fA-F]{2})+", name):
        try:
            text = bytes.fromhex(name).decode("ascii")
            if text.isprintable():
                return text
        except (ValueError, UnicodeDecodeError):
            pass
    return name


@register
class TronAdapter(Adapter):
    id = "tron"

    def validate_address(self, address: str) -> str:
        address = address.strip()
        if not ADDRESS_RE.match(address):
            raise ValueError("Tron address must start with T and be 34 characters")
        try:
            raw = b58check_decode(address)
        except ValueError:
            raise ValueError("Tron address checksum is not valid")
        if len(raw) != 21 or raw[0] != 0x41:
            raise ValueError("not a Tron mainnet address")
        return address

    def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        headers = {}
        key = self.secret("trongrid_api_key")
        if key:
            headers["TRON-PRO-API-KEY"] = key
        try:
            data = self.ctx.http.get_json(self.network.explorer_api.rstrip("/") + path, params, headers)
        except HttpError as e:
            raise AdapterError(f"TronGrid: {e}")
        if data.get("success") is False:
            raise AdapterError(f"TronGrid: {data.get('error') or data}")
        return data

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        headers = {}
        key = self.secret("trongrid_api_key")
        if key:
            headers["TRON-PRO-API-KEY"] = key
        try:
            data = self.ctx.http.post_json(self.network.explorer_api.rstrip("/") + path, payload, headers)
        except HttpError as e:
            raise AdapterError(f"TronGrid: {e}")
        return data if isinstance(data, dict) else {}

    def _paged(self, path: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        fingerprint = None
        while True:
            p = dict(params, limit=PAGE)
            if fingerprint:
                p["fingerprint"] = fingerprint
            data = self._get(path, p)
            batch = data.get("data") or []
            rows.extend(batch)
            fingerprint = (data.get("meta") or {}).get("fingerprint")
            if not fingerprint or not batch:
                return rows

    # --- parsing ----------------------------------------------------------------
    def parse_transactions(self, txs: List[Dict[str, Any]], addr: str) -> Tuple[List[Movement], List[str], Dict[str, str]]:
        native = f"{self.network.id}:native"
        moves: List[Movement] = []
        warnings: List[str] = []
        trc10: Dict[str, str] = {}
        for tx in txs:
            h = tx.get("txID") or tx.get("txid") or ""
            ts = int(tx.get("block_timestamp") or 0) // 1000
            ret = (tx.get("ret") or [{}])[0]
            failed = ret.get("contractRet") not in (None, "SUCCESS")
            contracts = ((tx.get("raw_data") or {}).get("contract") or [])
            if not contracts:
                continue
            c = contracts[0]
            typ = c.get("type", "")
            v = (c.get("parameter") or {}).get("value") or {}
            owner = hex_to_base58(v.get("owner_address", ""))
            fee_sun = int(ret.get("fee") or 0) or (int(tx.get("energy_fee") or 0) + int(tx.get("net_fee") or 0))
            if owner == addr and fee_sun:
                moves.append(Movement(uid=f"{h}:fee", tx_hash=h, ts=ts, asset_key=native,
                                      amount=-scaled(fee_sun, 6), kind="fee", failed=failed, info=typ,
                                      from_address=owner,
                                      to_address=hex_to_base58(v.get("to_address") or v.get("contract_address") or "")))
            if failed:
                continue
            if typ == "TransferContract":
                to = hex_to_base58(v.get("to_address", ""))
                amount = scaled(v.get("amount") or 0, 6)
                if owner == addr and amount:
                    moves.append(Movement(uid=f"{h}:out", tx_hash=h, ts=ts, asset_key=native,
                                          amount=-amount, kind="out", counterparty=to,
                                          from_address=owner, to_address=to))
                if to == addr and amount:
                    moves.append(Movement(uid=f"{h}:in", tx_hash=h, ts=ts, asset_key=native,
                                          amount=amount, kind="in", counterparty=owner,
                                          from_address=owner, to_address=to))
            elif typ == "TransferAssetContract":
                to = hex_to_base58(v.get("to_address", ""))
                token = _asset_name(str(v.get("asset_name", "")))
                key = f"{self.network.id}:trc10:{token}"
                trc10[key] = token
                amount = Decimal(int(v.get("amount") or 0))  # decimals unknown: raw units
                if owner == addr and amount:
                    moves.append(Movement(uid=f"{h}:trc10:out", tx_hash=h, ts=ts, asset_key=key,
                                          amount=-amount, kind="out", counterparty=to, info="TRC-10 (raw units)",
                                          from_address=owner, to_address=to))
                if to == addr and amount:
                    moves.append(Movement(uid=f"{h}:trc10:in", tx_hash=h, ts=ts, asset_key=key,
                                          amount=amount, kind="in", counterparty=owner, info="TRC-10 (raw units)",
                                          from_address=owner, to_address=to))
            elif typ == "TriggerSmartContract":
                call_value = int(v.get("call_value") or 0)
                if owner == addr and call_value:
                    contract = hex_to_base58(v.get("contract_address", ""))
                    moves.append(Movement(uid=f"{h}:call", tx_hash=h, ts=ts, asset_key=native,
                                          amount=-scaled(call_value, 6), kind="out", counterparty=contract,
                                          from_address=owner, to_address=contract))
            elif typ == "WithdrawBalanceContract":
                # Claimed voting rewards: income. The amount is in the transaction info; when the
                # list does not carry it, sync() reads it (one call per claim).
                if owner == addr:
                    amount = tx.get("withdraw_amount")
                    moves.append(Movement(uid=f"{h}:reward", tx_hash=h, ts=ts, asset_key=native,
                                          amount=scaled(int(amount), 6) if amount is not None else ZERO,
                                          kind="in", info="reward: Tron voting" if amount is not None
                                          else "reward: Tron voting (amount pending)",
                                          from_address="", to_address=owner))
            elif typ in NEUTRAL_TYPES:
                continue
            elif owner == addr:
                warnings.append(f"{h}: {typ} not interpreted (fee recorded only); check the year-end snapshot")
        return moves, warnings, trc10

    def parse_trc20(self, rows: List[Dict[str, Any]], addr: str) -> Tuple[List[Movement], Dict[str, AssetInfo]]:
        moves: List[Movement] = []
        infos: Dict[str, AssetInfo] = {}
        counter: Dict[str, int] = defaultdict(int)
        for t in rows:
            if t.get("type") not in (None, "Transfer"):
                continue
            ti = t.get("token_info") or {}
            contract = ti.get("address", "")
            asset = infos.get(f"{self.network.id}:{contract}") or assets_mod.token_asset(
                self.ctx, self.network, contract, ti.get("symbol", ""), ti.get("name", ""),
                int(ti.get("decimals") or 0))
            infos[asset.key] = asset
            dec = asset.decimals if asset.decimals is not None else int(ti.get("decimals") or 0)
            amount = scaled(t.get("value") or 0, dec)
            h = t.get("transaction_id", "")
            ts = int(t.get("block_timestamp") or 0) // 1000
            frm, to = t.get("from", ""), t.get("to", "")
            sig = f"{h}:trc20:{contract}:{frm}:{to}:{t.get('value')}"
            uid = f"{sig}:{counter[sig]}"
            counter[sig] += 1
            info = "zero-value transfer" if not amount else ""
            if frm == addr:
                moves.append(Movement(uid=f"{uid}:out", tx_hash=h, ts=ts, asset_key=asset.key,
                                      amount=-amount, kind="out", counterparty=to, info=info,
                                      from_address=frm, to_address=to))
            if to == addr:
                moves.append(Movement(uid=f"{uid}:in", tx_hash=h, ts=ts, asset_key=asset.key,
                                      amount=amount, kind="in", counterparty=frm, info=info,
                                      from_address=frm, to_address=to))
        return moves, infos

    # --- sync -------------------------------------------------------------------
    def sync(self, wallet: Dict[str, Any], state: Dict[str, Any], log: Logger) -> SyncResult:
        addr = wallet["address"]
        result = SyncResult(state=dict(state))
        since = int(state.get("last_ts_ms", 0))
        common = {"only_confirmed": "true", "order_by": "block_timestamp,asc", "min_timestamp": since}
        txs = self._paged(f"/v1/accounts/{addr}/transactions", common)
        trc20 = self._paged(f"/v1/accounts/{addr}/transactions/trc20", common)
        log(f"{wallet['label']}: {len(txs)} Tron transactions, {len(trc20)} TRC-20 transfers")
        moves, warnings, trc10 = self.parse_transactions(txs, addr)
        for m in moves:   # voting-reward claims whose amount the list did not carry
            if m.info.endswith("(amount pending)"):
                info = self._post("/wallet/gettransactioninfobyid", {"value": m.tx_hash})
                m.amount = scaled(int(info.get("withdraw_amount") or 0), 6)
                m.info = "reward: Tron voting"
        moves = [m for m in moves if not (m.uid.endswith(":reward") and not m.amount)]
        tmoves, infos = self.parse_trc20(trc20, addr)
        result.movements = moves + tmoves
        result.warnings = warnings
        result.assets = [assets_mod.native_asset(self.network)] + list(infos.values()) + [
            AssetInfo(key=k, network=self.network.id, symbol=f"TRC10-{name}"[:24], name=f"TRC-10 {name}",
                      decimals=0, contract=name) for k, name in trc10.items()]
        stamps = [int(t.get("block_timestamp") or 0) for t in txs + trc20]
        if stamps:
            result.state["last_ts_ms"] = max(stamps)
        return result

    def staking_positions(self, wallet: Dict[str, Any], log: Logger) -> List[StakePosition]:
        """Frozen TRX read by current_balances, plus voting rewards not claimed yet."""
        positions = list(self.staking_found)
        try:
            sun = int(self._post("/wallet/getReward", {"address": wallet["address"], "visible": True}).get("reward") or 0)
        except (AdapterError, ValueError) as e:
            log(f"{wallet['label']}: unclaimed voting rewards not read ({e})")
            sun = 0
        if sun:
            if not positions:
                positions = [StakePosition(asset_key=f"{self.network.id}:native", protocol="Tron Stake 2.0")]
            positions[0].pending_rewards = scaled(sun, 6)
        return positions

    def current_balances(self, wallet: Dict[str, Any], known_assets: List[str],
                         log: Logger) -> Tuple[Dict[str, Decimal], List[AssetInfo]]:
        addr = wallet["address"]
        data = (self._get(f"/v1/accounts/{addr}", {}).get("data") or [{}])
        acct = data[0] if data else {}
        sun = int(acct.get("balance") or 0)
        sun += sum(int(f.get("amount") or 0) for f in acct.get("frozenV2") or [])
        sun += sum(int(f.get("unfreeze_amount") or 0) for f in acct.get("unfrozenV2") or [])
        sun += sum(int(f.get("frozen_balance") or 0) for f in acct.get("frozen") or [])
        sun += int(acct.get("delegated_frozenV2_balance_for_bandwidth") or 0)
        sun += int((acct.get("account_resource") or {}).get("delegated_frozenV2_balance_for_energy") or 0)
        native = assets_mod.native_asset(self.network)
        out: Dict[str, Decimal] = {native.key: scaled(sun, 6)}
        # Staked TRX (Stake 2.0 and legacy frozen, delegated included) for the Staking page.
        staked = sum(int(f.get("amount") or 0) for f in acct.get("frozenV2") or []) + \
            sum(int(f.get("frozen_balance") or 0) for f in acct.get("frozen") or []) + \
            int(acct.get("delegated_frozenV2_balance_for_bandwidth") or 0) + \
            int((acct.get("account_resource") or {}).get("delegated_frozenV2_balance_for_energy") or 0)
        unfreezing = sum(int(f.get("unfreeze_amount") or 0) for f in acct.get("unfrozenV2") or [])
        votes = [v for v in acct.get("votes") or [] if v.get("vote_address")]
        self.staking_found = [StakePosition(
            asset_key=native.key, staked=scaled(staked, 6), unbonding=scaled(unfreezing, 6),
            protocol="Tron Stake 2.0",
            validator=(hex_to_base58(votes[0]["vote_address"]) if len(votes) == 1
                       else f"{len(votes)} Super Representatives" if votes else "no votes"))] \
            if staked or unfreezing else []
        decimals = {r["key"]: r["decimals"] for r in self.ctx.db.query(
            "SELECT key, decimals FROM assets WHERE network=?", (self.network.id,))}
        infos = [native]
        for entry in acct.get("trc20") or []:
            for contract, raw in entry.items():
                key = f"{self.network.id}:{contract}"
                info = assets_mod.token_asset(self.ctx, self.network, contract)
                dec = info.decimals if info.decimals is not None else decimals.get(key)
                if dec is None:
                    log(f"{wallet['label']}: TRC-20 {contract} decimals unknown, skipped until a transfer is seen")
                    continue
                out[key] = scaled(raw, dec)
                infos.append(info)
        for entry in acct.get("assetV2") or []:
            key = f"{self.network.id}:trc10:{entry.get('key')}"
            out[key] = Decimal(int(entry.get("value") or 0))
        for key in known_assets:
            if key.startswith(self.network.id + ":") and key not in out:
                out[key] = ZERO
        return out, infos
