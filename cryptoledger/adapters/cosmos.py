"""Cosmos SDK chains (Terra, Terra Classic, and any chain added from Settings) through a
public LCD/REST endpoint, no key.

History: /cosmos/tx/v1beta1/txs searched by "message.sender" and "coin_received.receiver"
(SDK 0.47+ query syntax), parsed from the events of each transaction:
  * coin_spent / coin_received of the account give every balance change;
  * the fee comes from the "tx" event (fee, fee_payer);
  * coins sent to the staking pools (delegations) are NOT counted as leaving: staked coins
    are still yours, so the year-end balance = liquid + staked + unbonding, like the live one;
  * coins paid by the distribution module are staking rewards (category "reward").
Live balances: bank balances + delegations + unbonding delegations.

Public LCD nodes keep only recent transactions, so the history is usually partial: unless the
network sets "history_complete": true (an archive node), year-end balances come from snapshots
(the live balance read at each sync, or near the cutoff) or manual entries, never from summing a
partial history. Transactions are still imported (rewards, fees, transfers).

Network config: bech32_prefix ("terra"), native_denom ("uluna"), optional "denoms" with
symbol, decimals, price ids and status of other native denoms (e.g. Terra Classic "uusd" = USTC;
its other old fiat stablecoins are "ghost": no market).
Unknown denoms (ibc/…, factory/…) are listed on the Assets page to map or mark as spam.
CW20 tokens (smart contracts) are not read.
"""
from __future__ import annotations

import hashlib
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from .. import assets as assets_mod
from ..http import HttpError
from ..util import ZERO, scaled
from . import register
from .base import Adapter, AdapterError, AssetInfo, Logger, Movement, SyncResult

PAGE = 100
_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_COIN = re.compile(r"(\d+)([a-zA-Z][a-zA-Z0-9/:._-]*)")
STAKING_MSGS = ("MsgDelegate", "MsgUndelegate", "MsgBeginRedelegate", "MsgCancelUnbondingDelegation")


# --- bech32 (BIP-173) ------------------------------------------------------------------
def _polymod(values: List[int]) -> int:
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (b >> i) & 1 else 0
    return chk


def _hrp_expand(hrp: str) -> List[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _convertbits(data: List[int], frombits: int, tobits: int, pad: bool = True) -> Optional[List[int]]:
    acc = bits = 0
    ret: List[int] = []
    maxv = (1 << tobits) - 1
    for v in data:
        acc = (acc << frombits) | v
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad and bits:
        ret.append((acc << (tobits - bits)) & maxv)
    elif not pad and (bits >= frombits or ((acc << (tobits - bits)) & maxv)):
        return None
    return ret


def bech32_decode(address: str) -> Tuple[Optional[str], Optional[bytes]]:
    a = address.strip()
    if a.lower() != a and a.upper() != a:
        return None, None
    a = a.lower()
    pos = a.rfind("1")
    if pos < 1 or pos + 7 > len(a) or len(a) > 90:
        return None, None
    hrp, data = a[:pos], a[pos + 1:]
    if any(c not in _CHARSET for c in data):
        return None, None
    values = [_CHARSET.find(c) for c in data]
    if _polymod(_hrp_expand(hrp) + values) != 1:
        return None, None
    raw = _convertbits(values[:-6], 5, 8, False)
    return (hrp, bytes(raw)) if raw is not None else (None, None)


def bech32_encode(hrp: str, data: bytes) -> str:
    d = _convertbits(list(data), 8, 5) or []
    pm = _polymod(_hrp_expand(hrp) + d + [0] * 6) ^ 1
    return hrp + "1" + "".join(_CHARSET[x] for x in d + [(pm >> 5 * (5 - i)) & 31 for i in range(6)])


def module_address(prefix: str, name: str) -> str:
    """Address of a Cosmos SDK module account (sha256 of its name, first 20 bytes)."""
    return bech32_encode(prefix, hashlib.sha256(name.encode()).digest()[:20])


def parse_coins(text: str) -> List[Tuple[str, int]]:
    return [(m.group(2), int(m.group(1))) for m in _COIN.finditer(text or "")]


@register
class CosmosAdapter(Adapter):
    id = "cosmos"

    @property
    def has_history(self) -> bool:  # type: ignore[override]
        return bool(self.network.history_complete)

    @property
    def prefix(self) -> str:
        return (self.network.bech32_prefix or "cosmos").lower()

    def validate_address(self, address: str) -> str:
        hrp, raw = bech32_decode(address)
        if hrp != self.prefix or raw is None or len(raw) not in (20, 32):
            raise ValueError(f"not a {self.network.name} address ({self.prefix}1…)")
        return address.strip().lower()

    # --- API --------------------------------------------------------------------
    def _base(self) -> str:
        return (self.network.explorer_api or "").rstrip("/")

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if not self._base():
            raise AdapterError(f"{self.network.name}: no API URL (Settings → Networks)")
        try:
            return self.ctx.http.get_json(self._base() + path, params or {})
        except HttpError as e:
            raise AdapterError(f"{self.network.name} API: {e}")

    def asset(self, denom: str) -> AssetInfo:
        if denom == self.network.native_denom:
            return assets_mod.native_asset(self.network)
        cfg = (self.network.denoms or {}).get(denom) or {}
        key = f"{self.network.id}:{denom}"
        if cfg:
            return AssetInfo(key=key, network=self.network.id, symbol=cfg.get("symbol") or denom,
                             name=cfg.get("name") or "", decimals=int(cfg.get("decimals", 6)), contract=denom,
                             coingecko_id=cfg.get("coingecko"), binance_symbol=cfg.get("binance"),
                             status=cfg.get("status") or "known")
        short = denom if len(denom) <= 16 else denom[:10] + "…" + denom[-4:]
        return AssetInfo(key=key, network=self.network.id, symbol=short[:24], name=denom[:80], decimals=6,
                         contract=denom, status="unmapped")

    def _search(self, query: str, done: int) -> List[Dict[str, Any]]:
        """All transactions matching `query`, oldest first, skipping the first `done`."""
        out: List[Dict[str, Any]] = []
        page = done // PAGE + 1
        skip = done % PAGE
        while True:
            data = self._get("/cosmos/tx/v1beta1/txs", {"query": query, "page": page, "limit": PAGE,
                                                       "order_by": "ORDER_BY_ASC"}) or {}
            if data.get("error") or data.get("message") and not data.get("tx_responses"):
                raise AdapterError(f"{self.network.name} API: {data.get('error') or data.get('message')}")
            batch = data.get("tx_responses") or []
            out.extend(batch[skip:])
            skip = 0
            total = int(data.get("total") or 0)
            if len(batch) < PAGE or page * PAGE >= total:
                return out
            page += 1

    # --- sync -------------------------------------------------------------------
    def sync(self, wallet: Dict[str, Any], state: Dict[str, Any], log: Logger) -> SyncResult:
        result = SyncResult(state=dict(state))
        me = wallet["address"]
        log(f"{wallet['label']}: reading {self.network.name} transactions")
        txs: Dict[str, Dict[str, Any]] = {}
        counts = dict(state.get("counts") or {})
        try:
            for name, query in (("sent", f"message.sender='{me}'"), ("received", f"coin_received.receiver='{me}'")):
                found = self._search(query, int(counts.get(name) or 0))
                counts[name] = int(counts.get(name) or 0) + len(found)
                for r in found:
                    txs[r["txhash"]] = r
        except AdapterError as e:
            result.warnings.append(f"{self.network.name}: transaction history not available from this API ({e}). "
                                   "Live balances are still read; year-end values need a snapshot near the "
                                   "cutoff or a manual entry.")
            log(f"{wallet['label']}: ⚠ no transaction history ({e}); reading live balances only")
            return result
        log(f"{wallet['label']}: {len(txs)} transactions"
            + ("" if self.has_history else " (public node: history may be partial; year-end uses the live "
                                           "balance, snapshots or manual entries)"))
        assets: Dict[str, AssetInfo] = {}
        moves: List[Movement] = []
        for r in sorted(txs.values(), key=lambda r: (int(r.get("height") or 0), r["txhash"])):
            for m in self.parse_tx(r, me):
                moves.append(m)
                a = self.asset(m.asset_key.split(":", 1)[1]) if not m.asset_key.endswith(":native") \
                    else assets_mod.native_asset(self.network)
                assets[a.key] = a
        result.movements = moves
        result.assets = list(assets.values())
        result.state["counts"] = counts
        return result

    def parse_tx(self, r: Dict[str, Any], me: str) -> List[Movement]:
        from ..timeutil import parse_utc
        h = r["txhash"]
        ts = parse_utc(r["timestamp"])
        failed = int(r.get("code") or 0) != 0
        bonded = {module_address(self.prefix, "bonded_tokens_pool"), module_address(self.prefix, "not_bonded_tokens_pool")}
        distribution = module_address(self.prefix, "distribution")
        msgs = (((r.get("tx") or {}).get("body") or {}).get("messages") or [])
        msg_type = {str(i): str(m.get("@type", "")).rsplit(".", 1)[-1] for i, m in enumerate(msgs)}

        fee: Dict[str, int] = {}
        groups: Dict[str, Dict[str, Any]] = {}   # msg_index -> spent/received/pool/rewards/peers
        for e in r.get("events") or []:
            attrs: Dict[str, str] = {}
            for a in e.get("attributes") or []:
                attrs[str(a.get("key"))] = str(a.get("value"))
            et = e.get("type")
            if et == "tx" and attrs.get("fee_payer") == me:
                for denom, amt in parse_coins(attrs.get("fee", "")):
                    fee[denom] = fee.get(denom, 0) + amt
                continue
            idx = attrs.get("msg_index", "")
            g = groups.setdefault(idx, {"spent": {}, "received": {}, "pool_in": {}, "dist_out": {}, "peer": ""})
            if et == "coin_spent" and attrs.get("spender") == me:
                for denom, amt in parse_coins(attrs.get("amount", "")):
                    g["spent"][denom] = g["spent"].get(denom, 0) + amt
            elif et == "coin_received" and attrs.get("receiver") == me:
                for denom, amt in parse_coins(attrs.get("amount", "")):
                    g["received"][denom] = g["received"].get(denom, 0) + amt
            elif et == "coin_received" and attrs.get("receiver") in bonded:
                for denom, amt in parse_coins(attrs.get("amount", "")):
                    g["pool_in"][denom] = g["pool_in"].get(denom, 0) + amt
            elif et == "coin_spent" and attrs.get("spender") == distribution:
                for denom, amt in parse_coins(attrs.get("amount", "")):
                    g["dist_out"][denom] = g["dist_out"].get(denom, 0) + amt
            elif et == "transfer":
                if attrs.get("sender") == me and attrs.get("recipient"):
                    g["peer"] = attrs["recipient"]
                elif attrs.get("recipient") == me and attrs.get("sender"):
                    g["peer"] = g["peer"] or attrs["sender"]

        moves: List[Movement] = []

        def akey(denom: str) -> Tuple[str, int]:
            a = self.asset(denom)
            return a.key, a.decimals if a.decimals is not None else 6

        for denom, amt in fee.items():
            k, dec = akey(denom)
            moves.append(Movement(uid=f"{h}:fee:{denom}", tx_hash=h, ts=ts, asset_key=k, amount=-scaled(amt, dec),
                                  kind="fee", failed=failed, from_address=me, info="fee"))
        fee_left = dict(fee)
        if failed:
            return moves   # a failed transaction only costs its fee
        for idx, g in sorted(groups.items()):
            mtype = msg_type.get(idx, "")
            for denom, amt in g["spent"].items():
                if idx == "":                      # fee deduction (and Terra Classic tax), outside messages
                    paid = min(amt, fee_left.get(denom, 0))
                    fee_left[denom] = fee_left.get(denom, 0) - paid
                    amt -= paid
                    if amt <= 0:
                        continue
                    k, dec = akey(denom)
                    moves.append(Movement(uid=f"{h}:tax:{denom}", tx_hash=h, ts=ts, asset_key=k,
                                          amount=-scaled(amt, dec), kind="fee", from_address=me, info="tax"))
                    continue
                if mtype in STAKING_MSGS or g["pool_in"].get(denom):
                    amt -= min(amt, g["pool_in"].get(denom, 0))   # delegated: still yours
                if amt <= 0:
                    continue
                k, dec = akey(denom)
                moves.append(Movement(uid=f"{h}:{idx}:{denom}:out", tx_hash=h, ts=ts, asset_key=k,
                                      amount=-scaled(amt, dec), kind="out", counterparty=g["peer"], info=mtype,
                                      from_address=me, to_address=g["peer"]))
            for denom, amt in g["received"].items():
                reward = min(amt, g["dist_out"].get(denom, 0))
                k, dec = akey(denom)
                if reward:
                    moves.append(Movement(uid=f"{h}:{idx}:{denom}:reward", tx_hash=h, ts=ts, asset_key=k,
                                          amount=scaled(reward, dec), kind="in", info="reward: staking",
                                          counterparty=module_address(self.prefix, "distribution"), to_address=me))
                if amt - reward > 0:
                    moves.append(Movement(uid=f"{h}:{idx}:{denom}:in", tx_hash=h, ts=ts, asset_key=k,
                                          amount=scaled(amt - reward, dec), kind="in", counterparty=g["peer"],
                                          info=mtype, from_address=g["peer"], to_address=me))
        return moves

    # --- live balances ----------------------------------------------------------
    def current_balances(self, wallet: Dict[str, Any], known_assets: List[str],
                         log: Logger) -> Tuple[Dict[str, Decimal], List[AssetInfo]]:
        me = wallet["address"]
        raw: Dict[str, int] = {}

        def add(denom: str, amount: Any) -> None:
            try:
                v = int(Decimal(str(amount)))
            except (ArithmeticError, ValueError):
                return
            raw[denom] = raw.get(denom, 0) + v

        next_key = None
        while True:
            params = {"pagination.limit": 200}
            if next_key:
                params["pagination.key"] = next_key
            data = self._get(f"/cosmos/bank/v1beta1/balances/{me}", params) or {}
            for c in data.get("balances") or []:
                add(c.get("denom"), c.get("amount"))
            next_key = (data.get("pagination") or {}).get("next_key")
            if not next_key:
                break
        try:   # staked and unbonding coins are still yours
            for d in (self._get(f"/cosmos/staking/v1beta1/delegations/{me}") or {}).get("delegation_responses") or []:
                bal = d.get("balance") or {}
                add(bal.get("denom"), bal.get("amount"))
            ub = self._get(f"/cosmos/staking/v1beta1/delegators/{me}/unbonding_delegations") or {}
            for u in ub.get("unbonding_responses") or []:
                for entry in u.get("entries") or []:
                    add(self.network.native_denom, entry.get("balance"))
        except AdapterError as e:
            log(f"{wallet['label']}: staked balance not read ({e})")
        out: Dict[str, Decimal] = {assets_mod.native_asset(self.network).key: ZERO}
        infos: List[AssetInfo] = [assets_mod.native_asset(self.network)]
        for denom, amount in raw.items():
            a = self.asset(denom)
            out[a.key] = out.get(a.key, ZERO) + scaled(amount, a.decimals if a.decimals is not None else 6)
            if a.key != infos[0].key:
                infos.append(a)
        for key in known_assets:
            if key.startswith(self.network.id + ":") and key not in out:
                out[key] = ZERO
        return out, infos
