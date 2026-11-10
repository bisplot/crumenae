"""Contract staking on EVM chains, read with standard JSON-RPC (eth_getLogs, eth_call).

The contracts are listed per network in config/staking.json:

- "ronin_dpos": Ronin's staking contract. The wallet's Delegated / Undelegated / RewardClaimed
  events give the history; getManyStakingAmountsById / getRewardsById give the amount staked and
  the unclaimed rewards per validator pool.
- "stakewise_v3": StakeWise v3 vaults (MetaMask Pooled Staking is one). Vaults are found among the
  contracts the wallet sent the native coin to (one getShares call per contract, once). History:
  Deposited / ExitQueueEntered / ExitedAssetsClaimed; value: convertToAssets(getShares) plus the
  exits not claimed yet.

The coins in each contract are their own asset "<network>:stake:<contract>" ("RON (staked)"), so
they count in the live balances and at year-end (read at the cutoff block: archive node), and
moving coins in or out is an internal transfer (classify treats the contract as yours). Claimed
Ronin rewards are income when claimed. A vault's rewards raise its share price with no
transaction: they show in its value and as "rewards so far", never as income movements.

Reads go to the chain's JSON-RPC: a custom RPC URL for the network, else Alchemy (with your key),
else the network's public RPC; the same whichever provider reads the transaction history.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from .. import assets as assets_mod
from ..http import HttpError
from ..models import AdapterError, AssetInfo, Movement, StakePosition, SyncResult
from ..util import scaled

RONIN = "ronin_dpos"
STAKEWISE = "stakewise_v3"

# keccak-256 of the event signatures.
T_DELEGATED = "0xe5541a6b6103d4fa7e021ed54fad39c66f27a76bd13d374cf6240ae6bd0bb72b"     # Delegated(address,address,uint256)
T_UNDELEGATED = "0x4d10bd049775c77bd7f255195afba5088028ecb3c7c277d393ccff7934f2f92c"   # Undelegated(address,address,uint256)
T_REWARD_CLAIMED = "0x0aa4d283470c904c551d18bb894d37e17674920f3261a7f854be501e25f421b7"  # RewardClaimed(address,address,uint256)
T_DEPOSITED = "0x861a4138e41fb21c121a7dbb1053df465c837fc77380cc7226189a662281be2c"     # Deposited(address,address,uint256,uint256,address)
T_EXIT_ENTERED = "0x211091c5bf013c1230f996c3bb2bc327e3de429a3d3c356dcea9a0c858bc407f"  # ExitQueueEntered(address,address,uint256,uint256)
T_EXIT_CLAIMED = "0xeb3b05c070c24f667611fdb3ff75fe007d42401c573aed8d8faca95fd00ccb56"  # ExitedAssetsClaimed(address,uint256,uint256,uint256)
# First 4 bytes of keccak-256 of the function signatures.
GET_MANY_STAKING = "0x435628ba"    # getManyStakingAmountsById(address[],address[])
GET_REWARDS = "0x94a8c31c"         # getRewardsById(address,address[])
GET_SHARES = "0xf04da65b"          # getShares(address)
CONVERT_TO_ASSETS = "0x07a2d13a"   # convertToAssets(uint256)

MIN_SPAN = 2000          # smallest block range tried when a node refuses a long eth_getLogs range
BACK = 5                 # blocks before the first transfer to a contract, for safety
ZERO = Decimal(0)
ONE_COIN = 10 ** 18


def _int(v: Any) -> int:
    if v is None or v == "" or v == "0x":
        return 0
    if isinstance(v, int):
        return v
    s = str(v)
    return int(s, 16) if s.lower().startswith("0x") else int(s)


def _w_addr(a: str) -> str:
    return a.lower()[2:].rjust(64, "0")


def _w_int(n: int) -> str:
    return format(int(n), "x").rjust(64, "0")


def _addr_array(addrs: List[str]) -> str:
    return _w_int(len(addrs)) + "".join(_w_addr(a) for a in addrs)


def _words(data: Optional[str]) -> List[int]:
    s = (data or "0x")[2:]
    return [int(s[i:i + 64], 16) for i in range(0, len(s) - len(s) % 64, 64)]


def _uint_array(data: Optional[str]) -> List[int]:
    w = _words(data)
    if len(w) < 2:
        return []
    off = w[0] // 32
    n = w[off] if off < len(w) else 0
    return w[off + 1:off + 1 + n]


def _topic(addr: str) -> str:
    return "0x" + _w_addr(addr)


def _topic_addr(t: str) -> str:
    return "0x" + str(t)[-40:].lower()


def _short(a: str) -> str:
    return f"{a[:6]}…{a[-4:]}"


def _key_problem(e: Exception) -> bool:
    text = str(e).lower()
    return "key missing" in text or "refused the api key" in text


def many_staking_call(pools: List[str], user: str) -> str:
    head = _w_int(64) + _w_int(64 + 32 * (1 + len(pools)))
    return GET_MANY_STAKING + head + _addr_array(pools) + _addr_array([user] * len(pools))


def rewards_call(user: str, pools: List[str]) -> str:
    return GET_REWARDS + _w_addr(user) + _w_int(64) + _addr_array(pools)


class ContractStaking:
    """Staking contracts of one EVM network, for one adapter (Etherscan or Alchemy)."""

    def __init__(self, adapter):
        self.a = adapter
        self.ctx = adapter.ctx
        self.network = adapter.network
        conf = (getattr(self.ctx, "staking", None) or {}).get(self.network.id) or []
        self.configs: List[Dict[str, Any]] = conf if isinstance(conf, list) else [conf]
        self._times: Dict[int, int] = {}
        self.decimals = int((self.network.native or {}).get("decimals") or 18)

    # --- JSON-RPC ---------------------------------------------------------------
    def endpoint(self) -> Optional[str]:
        custom = self.a.secret(f"rpc_url:{self.network.id}")
        if custom:
            return custom
        key = self.a.secret("alchemy_api_key")
        if key and self.network.alchemy_network:
            return f"https://{self.network.alchemy_network}.g.alchemy.com/v2/{key}"
        return self.network.rpc_url or None

    def rpc(self, method: str, params: List[Any]) -> Any:
        url = self.endpoint()
        if not url:
            raise AdapterError(f"Alchemy API key missing (Settings → API providers): {self.network.name} "
                               "staking contracts are read through it")
        try:
            resp = self.ctx.http.post_json(url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        except HttpError as e:
            if e.status in (401, 403):
                raise AdapterError(f"Alchemy refused the API key for {self.network.name}: copy it again from your "
                                   "Alchemy dashboard and enable this chain on the app")
            raise AdapterError(f"{self.network.name} {method}: {e}")
        if not isinstance(resp, dict):
            raise AdapterError(f"{self.network.name} {method}: unexpected response")
        if resp.get("error"):
            err = resp["error"]
            raise AdapterError(f"{self.network.name} {method}: {err.get('message') if isinstance(err, dict) else err}")
        return resp.get("result")

    def call(self, to: str, data: str, tag: str = "latest") -> str:
        return self.rpc("eth_call", [{"to": to, "data": data}, tag]) or "0x"

    def block_at(self, ts: int) -> int:
        hi = _int(self.rpc("eth_blockNumber", []))
        lo = 0
        while lo < hi:
            mid = (lo + hi + 1) // 2
            blk = self.rpc("eth_getBlockByNumber", [hex(mid), False]) or {}
            if _int(blk.get("timestamp")) <= ts:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def logs(self, address: str, topics: List[Any], start: int, end: int, log) -> List[Dict[str, Any]]:
        """eth_getLogs over [start, end]; when the node refuses the range, smaller ranges."""
        out: List[Dict[str, Any]] = []
        span = max(1, end - start + 1)
        frm = start
        calls = 0
        while frm <= end:
            to = min(end, frm + span - 1)
            try:
                res = self.rpc("eth_getLogs", [{"fromBlock": hex(frm), "toBlock": hex(to),
                                                "address": address, "topics": topics}]) or []
            except AdapterError as e:
                if _key_problem(e) or span <= MIN_SPAN:
                    raise
                span = max(MIN_SPAN, span // 10)
                continue
            out += res
            frm = to + 1
            calls += 1
            if calls % 200 == 0:
                log(f"{self.network.name} staking events: block {frm} of {end}")
        return out

    def _ts(self, entry: Dict[str, Any]) -> int:
        if entry.get("blockTimestamp"):
            return _int(entry["blockTimestamp"])
        b = _int(entry.get("blockNumber"))
        if b not in self._times:
            blk = self.rpc("eth_getBlockByNumber", [hex(b), False]) or {}
            self._times[b] = _int(blk.get("timestamp"))
        return self._times[b]

    def asset(self, contract: str, info: Dict[str, Any]) -> AssetInfo:
        return assets_mod.staked_asset(self.network, contract, info.get("label") or "Staking")

    # --- discovery --------------------------------------------------------------
    def _candidates(self, wallet: Dict[str, Any], new_moves: List[Movement]) -> Dict[str, int]:
        """Contracts the wallet sent the native coin to, with the time of the first transfer."""
        native = f"{self.network.id}:native"
        out: Dict[str, int] = {}
        for r in self.ctx.db.query(
                "SELECT lower(counterparty) AS c, MIN(ts) AS t FROM movements WHERE wallet_id=? AND asset_key=?"
                " AND kind='out' AND counterparty<>'' GROUP BY lower(counterparty)", (wallet["id"], native)):
            out[r["c"]] = int(r["t"])
        for m in new_moves:
            if m.asset_key == native and m.kind == "out" and m.counterparty:
                c = m.counterparty.lower()
                out[c] = min(out.get(c, m.ts), m.ts)
        out.pop(wallet["address"].lower(), None)
        return out

    def _is_vault(self, contract: str, user: str) -> bool:
        try:
            shares = self.call(contract, GET_SHARES + _w_addr(user))
            rate = self.call(contract, CONVERT_TO_ASSETS + _w_int(ONE_COIN))
        except AdapterError as e:
            if _key_problem(e):
                raise
            return False
        return len(shares) == 66 and len(rate) == 66 and _int(rate) > 0

    # --- history ----------------------------------------------------------------
    def sync(self, wallet: Dict[str, Any], result: SyncResult, moves: List[Movement], log) -> None:
        """Adds the staking movements and assets to `result` (and `moves`); state in result.state."""
        if not self.configs or not self.endpoint():
            return
        user = wallet["address"].lower()
        st = json.loads(json.dumps(result.state.get("staking") or {}))
        contracts: Dict[str, Dict[str, Any]] = st.setdefault("contracts", {})
        checked = set(st.get("checked", []))
        try:
            cands = self._candidates(wallet, moves)
            for conf in self.configs:
                if conf.get("type") == RONIN:
                    c = str(conf.get("contract") or "").lower()
                    if c in cands and c not in contracts:
                        contracts[c] = {"type": RONIN, "label": conf.get("label") or "Staking",
                                        "from": max(0, self.block_at(cands[c]) - BACK)}
                elif conf.get("type") == STAKEWISE:
                    for c, t in sorted(cands.items()):
                        if c in checked or c in contracts:
                            continue
                        if self._is_vault(c, user):
                            contracts[c] = {"type": STAKEWISE, "label": conf.get("label") or "StakeWise vault",
                                            "from": max(0, self.block_at(t) - BACK)}
                            log(f"{wallet['label']}: staking vault found at {c}")
                        checked.add(c)
            head = _int(self.rpc("eth_blockNumber", [])) if contracts else 0
        except AdapterError as e:   # never fatal: the history and balances are read anyway
            result.warnings.append(f"staking contracts not read ({e})")
            st["checked"] = sorted(checked)
            result.state["staking"] = st
            return
        st["checked"] = sorted(checked)
        for c, info in contracts.items():
            start = int(info.get("next", info["from"]))
            asset = self.asset(c, info)
            result.assets.append(asset)
            if start > head:
                continue
            try:
                if info["type"] == RONIN:
                    found = self._ronin_moves(c, info, user, start, head, asset.key, log)
                else:
                    found = self._vault_moves(c, info, user, start, head, asset.key, log)
            except AdapterError as e:
                result.warnings.append(f"{info.get('label')} {_short(c)}: staking events not read ({e}); "
                                       "the next sync tries again")
                continue
            moves += found
            info["next"] = head + 1
            if found:
                log(f"{wallet['label']}: {len(found)} staking movement(s) in {info.get('label')}")
        result.state["staking"] = st

    def _ronin_moves(self, c: str, info: Dict[str, Any], user: str, start: int, end: int, key: str,
                     log) -> List[Movement]:
        entries = (self.logs(c, [[T_DELEGATED, T_UNDELEGATED], _topic(user)], start, end, log)
                   + self.logs(c, [T_REWARD_CLAIMED, None, _topic(user)], start, end, log))
        pools = set(info.get("pools", []))
        moves: List[Movement] = []
        for e in sorted(entries, key=lambda x: (_int(x.get("blockNumber")), _int(x.get("logIndex")))):
            topics = [str(t).lower() for t in e.get("topics") or []]
            words = _words(e.get("data"))
            if len(topics) < 3 or not words:
                continue
            h = str(e.get("transactionHash") or "").lower()
            uid = f"{h}:log:{_int(e.get('logIndex'))}:stake"
            ts = self._ts(e)
            amount = scaled(words[0], self.decimals)
            if topics[0] in (T_DELEGATED, T_UNDELEGATED):
                pool = _topic_addr(topics[2])
                pools.add(pool)
                if topics[0] == T_DELEGATED:
                    moves.append(Movement(uid=uid, tx_hash=h, ts=ts, asset_key=key, amount=amount, kind="in",
                                          counterparty=user, info=f"stake: delegated to {_short(pool)}",
                                          from_address=user, to_address=c))
                else:
                    moves.append(Movement(uid=uid, tx_hash=h, ts=ts, asset_key=key, amount=-amount, kind="out",
                                          counterparty=user, info=f"stake: undelegated from {_short(pool)}",
                                          from_address=c, to_address=user))
            elif topics[0] == T_REWARD_CLAIMED:
                pool = _topic_addr(topics[1])
                # Income once, when claimed; the claimed coins then leave the pool (to the wallet,
                # or straight back into a pool: delegateRewards also emits Delegated).
                moves.append(Movement(uid=f"{uid}:reward", tx_hash=h, ts=ts, asset_key=key, amount=amount,
                                      kind="in", counterparty=pool,
                                      info=f"reward: {info.get('label') or 'staking'} ({_short(pool)})",
                                      from_address=pool, to_address=c))
                moves.append(Movement(uid=f"{uid}:claim", tx_hash=h, ts=ts, asset_key=key, amount=-amount,
                                      kind="out", counterparty=user, info="stake: rewards claimed",
                                      from_address=c, to_address=user))
        info["pools"] = sorted(pools)
        return moves

    def _vault_value(self, c: str, user: str, info: Dict[str, Any], tag: str, block: Optional[int]) -> Tuple[int, int]:
        """(value of the shares, exits not claimed yet) in base units at a block (None = now)."""
        shares = _int(self.call(c, GET_SHARES + _w_addr(user), tag))
        value = _int(self.call(c, CONVERT_TO_ASSETS + _w_int(shares), tag)) if shares else 0
        pending = 0
        for amount, b_in, b_out in (info.get("tickets") or {}).values():
            if (block is None or int(b_in) <= block) and (b_out is None or (block is not None and int(b_out) > block)):
                pending += int(amount)
        return value, pending

    def _vault_moves(self, c: str, info: Dict[str, Any], user: str, start: int, end: int, key: str,
                     log) -> List[Movement]:
        entries = (self.logs(c, [T_DEPOSITED, None, _topic(user)], start, end, log)
                   + self.logs(c, [[T_EXIT_ENTERED, T_EXIT_CLAIMED], _topic(user)], start, end, log))
        tickets: Dict[str, List[Any]] = info.setdefault("tickets", {})
        deposited, withdrawn = int(info.get("deposited", 0)), int(info.get("withdrawn", 0))
        moves: List[Movement] = []
        for e in sorted(entries, key=lambda x: (_int(x.get("blockNumber")), _int(x.get("logIndex")))):
            topics = [str(t).lower() for t in e.get("topics") or []]
            words = _words(e.get("data"))
            if not topics or not words:
                continue
            blk = _int(e.get("blockNumber"))
            h = str(e.get("transactionHash") or "").lower()
            uid = f"{h}:log:{_int(e.get('logIndex'))}:stake"
            if topics[0] == T_DEPOSITED:
                deposited += words[0]
                amount, kind, frm, to, note = words[0], "in", user, c, "stake: deposit"
            elif topics[0] == T_EXIT_ENTERED and len(words) >= 2:
                rate = _int(self.call(c, CONVERT_TO_ASSETS + _w_int(words[1]), hex(blk)))
                tickets[str(words[0])] = [str(rate), blk, None]
                continue   # shares become an exit ticket: same value, no movement
            elif topics[0] == T_EXIT_CLAIMED and len(words) >= 3:
                prev, new, paid = words[0], words[1], words[2]
                withdrawn += paid
                ticket = tickets.get(str(prev))
                if ticket:
                    ticket[2] = blk
                    if new:
                        tickets[str(new)] = [str(max(0, int(ticket[0]) - paid)), blk, None]
                amount, kind, frm, to, note = paid, "out", c, user, "stake: exit claimed"
            else:
                continue
            try:
                value, pending = self._vault_value(c, user, info, hex(blk), blk)
                after: Optional[Decimal] = scaled(value + pending, self.decimals)
            except AdapterError:
                after = None   # not an archive node: the history alone gives the balance
            moves.append(Movement(uid=uid, tx_hash=h, ts=self._ts(e), asset_key=key,
                                  amount=scaled(amount, self.decimals) * (1 if kind == "in" else -1), kind=kind,
                                  counterparty=user, info=note, from_address=frm, to_address=to,
                                  balance_after=after))
        info["deposited"], info["withdrawn"] = str(deposited), str(withdrawn)
        return moves

    # --- balances ---------------------------------------------------------------
    def contracts(self, wallet: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        return ((self.ctx.sync_state(wallet["id"]).get("staking") or {}).get("contracts") or {})

    def read(self, wallet: Dict[str, Any], block: Optional[int] = None,
             with_positions: bool = True) -> Tuple[Dict[str, Decimal], List[StakePosition], List[AssetInfo]]:
        """Coins in each staking contract now (block None) or at a block."""
        user = wallet["address"].lower()
        tag = "latest" if block is None else hex(block)
        amounts: Dict[str, Decimal] = {}
        positions: List[StakePosition] = []
        infos: List[AssetInfo] = []
        for c, info in self.contracts(wallet).items():
            asset = self.asset(c, info)
            infos.append(asset)
            label = info.get("label") or "Staking"
            if info["type"] == RONIN:
                pools = list(info.get("pools") or [])
                if not pools:
                    amounts[asset.key] = ZERO
                    continue
                staked = _uint_array(self.call(c, many_staking_call(pools, user), tag))
                rewards = _uint_array(self.call(c, rewards_call(user, pools), tag)) if with_positions else []
                amounts[asset.key] = scaled(sum(staked), self.decimals)
                for i, pool in enumerate(pools):
                    s = staked[i] if i < len(staked) else 0
                    r = rewards[i] if i < len(rewards) else 0
                    if s or r:
                        positions.append(StakePosition(asset.key, scaled(s, self.decimals),
                                                       pending_rewards=scaled(r, self.decimals),
                                                       protocol=label, validator=f"validator {_short(pool)}"))
            else:
                value, pending = self._vault_value(c, user, info, tag, block)
                amounts[asset.key] = scaled(value + pending, self.decimals)
                if value or pending:
                    gained = value + pending - (int(info.get("deposited", 0)) - int(info.get("withdrawn", 0)))
                    note = f"vault {_short(c)}"
                    if gained > 0:
                        note += f" · rewards so far {scaled(gained, self.decimals):.6f} (already in the value)"
                    positions.append(StakePosition(asset.key, scaled(value, self.decimals),
                                                   unbonding=scaled(pending, self.decimals),
                                                   protocol=label, validator=note))
        return amounts, positions, infos


def add_live(adapter, wallet: Dict[str, Any], balances: Dict[str, Decimal], infos: List[AssetInfo], log) -> None:
    """current_balances: coins staked now, and the Staking-page positions."""
    adapter.staking_found = []
    staking = ContractStaking(adapter)
    if not staking.configs or not staking.contracts(wallet):
        return
    try:
        amounts, positions, assets = staking.read(wallet)
    except (AdapterError, ValueError) as e:   # never fatal: the other balances are still stored
        log(f"{wallet['label']}: staking contracts not read ({e})")
        return
    balances.update(amounts)
    infos.extend(assets)
    adapter.staking_found = positions


def add_at(adapter, wallet: Dict[str, Any], block: Optional[int], asset_keys: List[str],
           out: Dict[str, Decimal]) -> None:
    """balance_at: coins in the staking contracts at a block. Raises if they cannot be read, so
    the caller does not report a balance without them."""
    prefix = f"{adapter.network.id}:stake:"
    if not any(k.startswith(prefix) for k in asset_keys):
        return
    amounts, _, _ = ContractStaking(adapter).read(wallet, block, with_positions=False)
    out.update({k: v for k, v in amounts.items() if k in asset_keys})
