"""Solana via JSON-RPC. With a Helius API key (Settings → Blockchain APIs, free plan
available) the app calls Helius; without one, the public Solana endpoint (slow,
rate-limited). An explicit "solana_rpc_url" secret (environment variable
CRYPTOLEDGER_SOLANA_RPC_URL) overrides both, for any other provider.

Every transaction carries the balances before and after it, so SOL and SPL
token changes are exact, and the SOL balance after each transaction is stored:
the year-end SOL balance is the chain's own number, not a sum.

Native staking: each stake account whose withdraw authority is the wallet is its own
asset ("SOL (staked)"), with its balance after every transaction and after every
epoch's inflation reward (getInflationReward). Moving SOL in or out of it is not a
disposal; each reward is income (category "reward") at the time it was credited.
"""
from __future__ import annotations

import re
import urllib.parse
from collections import defaultdict
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .. import assets as assets_mod
from ..b58 import b58decode
from ..http import HttpError
from ..util import ZERO, now, scaled
from . import register
from .base import Adapter, AdapterError, AssetInfo, Logger, Movement, StakePosition, SyncResult

TOKEN_PROGRAMS = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   # SPL Token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",   # Token-2022
)
SIG_PAGE = 1000
STAKE_PROGRAM = "Stake11111111111111111111111111111111111111"
SLOTS_PER_EPOCH = 432_000
NOT_DEACTIVATED = 18446744073709551615   # deactivationEpoch of an active delegation (u64 max)
HELIUS_URL = "https://mainnet.helius-rpc.com/?api-key="
ALCHEMY_URL = "https://solana-mainnet.g.alchemy.com/v2/"


@register
class SolanaAdapter(Adapter):
    id = "solana"

    def validate_address(self, address: str) -> str:
        address = address.strip()
        try:
            if len(b58decode(address)) != 32:
                raise ValueError
        except ValueError:
            raise ValueError("Solana address must be a base58 public key (32 bytes)")
        return address

    # --- RPC --------------------------------------------------------------------
    def _endpoint(self) -> Tuple[str, str]:
        """(URL, who answers): explicit RPC URL > Helius key > Alchemy key (the same key as the EVM
        chains; Solana must be enabled on your Alchemy app) > slow public endpoint."""
        explicit = self.secret("solana_rpc_url")
        if explicit:
            return explicit, "your Solana RPC"
        key = self.secret("helius_api_key")
        if key:
            return HELIUS_URL + urllib.parse.quote(key, safe=""), "Helius"
        key = self.secret("alchemy_api_key")
        if key:
            return ALCHEMY_URL + urllib.parse.quote(key, safe=""), "Alchemy"
        return self.network.rpc_url, "the public Solana RPC"

    def _url(self) -> str:
        return self._endpoint()[0]

    def _rpc(self, method: str, params: List[Any]) -> Any:
        for attempt in range(4):
            try:
                resp = self.ctx.http.post_json(self._url(), {"jsonrpc": "2.0", "id": 1,
                                                             "method": method, "params": params})
            except HttpError as e:
                who = self._endpoint()[1]
                if e.status in (401, 403) and who in ("Helius", "Alchemy"):
                    hint = " and that Solana is enabled on your Alchemy app" if who == "Alchemy" else ""
                    raise AdapterError(f"{who} refused the API key ({e.status}): check it in Settings → "
                                       f"API providers{hint}, then press Test")
                raise AdapterError(f"Solana RPC {method}: {e}")
            err = resp.get("error")
            if not err:
                return resp.get("result")
            if err.get("code") in (-32429, 429, -32005) and attempt < 3:
                import time
                time.sleep(2 * (attempt + 1))
                continue
            raise AdapterError(f"Solana RPC {method}: {err.get('message')}")
        raise AdapterError(f"Solana RPC {method}: rate limited")

    TX_VERSION = 1   # newest transaction version this parser reads (legacy, v0 and v1)

    def get_transaction(self, signature: str) -> Optional[Dict[str, Any]]:
        """One transaction, jsonParsed. A node holding a newer transaction version says which one:
        ask again with it rather than failing the whole sync."""
        version = self.TX_VERSION
        for _ in range(3):
            try:
                return self._rpc("getTransaction", [signature, {"encoding": "jsonParsed", "commitment": "finalized",
                                                                "maxSupportedTransactionVersion": version}])
            except AdapterError as e:
                m = re.search(r"Transaction version \((\d+)\) is not supported", str(e))
                if not m or int(m.group(1)) <= version:
                    raise
                version = int(m.group(1))
        return None

    def _signatures(self, account: str, until: Optional[str]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        before = None
        while True:
            opts: Dict[str, Any] = {"limit": SIG_PAGE, "commitment": "finalized"}
            if before:
                opts["before"] = before
            if until:
                opts["until"] = until
            page = self._rpc("getSignaturesForAddress", [account, opts]) or []
            out.extend(page)
            if len(page) < SIG_PAGE:
                return out
            before = page[-1]["signature"]

    def _token_accounts(self, owner: str) -> List[Dict[str, Any]]:
        accounts = []
        for program in TOKEN_PROGRAMS:
            res = self._rpc("getTokenAccountsByOwner",
                            [owner, {"programId": program}, {"encoding": "jsonParsed", "commitment": "finalized"}])
            for item in (res or {}).get("value", []):
                info = item["account"]["data"]["parsed"]["info"]
                ta = info["tokenAmount"]
                accounts.append({"pubkey": item["pubkey"], "mint": info["mint"],
                                 "amount": int(ta["amount"]), "decimals": int(ta["decimals"])})
        return accounts

    # --- parsing ----------------------------------------------------------------
    @staticmethod
    def account_keys(tx: Dict[str, Any]) -> List[str]:
        msg = tx["transaction"]["message"]
        keys = [k["pubkey"] if isinstance(k, dict) else k for k in msg["accountKeys"]]
        loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
        extra = list(loaded.get("writable") or []) + list(loaded.get("readonly") or [])
        # jsonParsed already lists loaded addresses in accountKeys; json encoding does not.
        if extra and not set(extra) <= set(keys):
            keys += extra
        return keys

    @staticmethod
    def stake_accounts_in(tx: Dict[str, Any]) -> List[str]:
        """Stake accounts a transaction creates or acts on (jsonParsed instructions, inner too)."""
        msg = (tx.get("transaction") or {}).get("message") or {}
        ixs = list(msg.get("instructions") or [])
        for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
            ixs += inner.get("instructions") or []
        found = []
        for ix in ixs:
            parsed = ix.get("parsed") if isinstance(ix.get("parsed"), dict) else {}
            info = parsed.get("info") or {}
            if ix.get("program") == "stake" and info.get("stakeAccount"):
                found.append(info["stakeAccount"])
            elif ix.get("program") == "system" and info.get("owner") == STAKE_PROGRAM and info.get("newAccount"):
                found.append(info["newAccount"])
        return list(dict.fromkeys(found))

    def parse_stake(self, tx: Dict[str, Any], signature: str, owner: str,
                    stake_accounts: Iterable[str]) -> List[Movement]:
        """Balance changes of the wallet's stake accounts in one transaction."""
        meta = tx.get("meta") or {}
        if meta.get("err") is not None:
            return []
        keys = self.account_keys(tx)
        ts = int(tx.get("blockTime") or 0)
        moves = []
        for acct in stake_accounts:
            if acct not in keys:
                continue
            i = keys.index(acct)
            if i >= len(meta.get("postBalances") or []):
                continue
            pre, post = int(meta["preBalances"][i]), int(meta["postBalances"][i])
            if pre == post:
                continue
            delta = post - pre
            moves.append(Movement(uid=f"{signature}:stake:{acct}", tx_hash=signature, ts=ts,
                                  asset_key=f"{self.network.id}:stake:{acct}", amount=scaled(delta, 9),
                                  kind="in" if delta > 0 else "out", balance_after=scaled(post, 9),
                                  counterparty=owner, info="stake",
                                  from_address=owner if delta > 0 else acct, to_address=acct if delta > 0 else owner))
        return moves

    def parse_transaction(self, tx: Dict[str, Any], signature: str, owner: str,
                          own_token_accounts: Iterable[str]) -> Tuple[List[Movement], Dict[str, int]]:
        """Movements of `owner` in one transaction, and {mint: decimals} seen."""
        meta = tx.get("meta") or {}
        ts = int(tx.get("blockTime") or 0)
        keys = self.account_keys(tx)
        failed = meta.get("err") is not None
        native = f"{self.network.id}:native"
        moves: List[Movement] = []
        if owner in keys:
            i = keys.index(owner)
            pre, post = int(meta["preBalances"][i]), int(meta["postBalances"][i])
            tx_fee = int(meta.get("fee") or 0)
            fee = tx_fee if i == 0 else 0
            transfer = post - pre + fee
            after = scaled(post, 9)
            if fee:
                moves.append(Movement(uid=f"{signature}:fee", tx_hash=signature, ts=ts, asset_key=native,
                                      amount=-scaled(fee, 9), kind="fee", failed=failed, balance_after=after,
                                      from_address=owner))
            if transfer and not failed:
                # The RPC does not name the other party: it is the account whose SOL
                # changed by exactly the opposite amount (net of the fee if it paid it).
                cp = next((k for j, k in enumerate(keys) if j != i and j < len(meta["postBalances"])
                           and int(meta["postBalances"][j]) - int(meta["preBalances"][j])
                           + (tx_fee if j == 0 else 0) == -transfer), "")
                moves.append(Movement(uid=f"{signature}:sol", tx_hash=signature, ts=ts, asset_key=native,
                                      amount=scaled(transfer, 9), kind="in" if transfer > 0 else "out",
                                      balance_after=after, counterparty=cp,
                                      from_address=cp if transfer > 0 else owner,
                                      to_address=owner if transfer > 0 else cp))
        if failed:
            return moves, {}
        own = set(own_token_accounts)
        decs: Dict[str, int] = {}
        # Net token change per (holder, mint); the holder is the owner wallet when known.
        deltas: Dict[Tuple[str, str], int] = defaultdict(int)
        for bucket, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
            for tb in meta.get(bucket) or []:
                idx = tb.get("accountIndex")
                acct = keys[idx] if idx is not None and idx < len(keys) else ""
                holder = owner if (tb.get("owner") == owner or acct in own) else (tb.get("owner") or acct)
                ui = tb.get("uiTokenAmount") or {}
                deltas[(holder, tb["mint"])] += sign * int(ui.get("amount") or 0)
                if holder == owner:
                    decs[tb["mint"]] = int(ui.get("decimals") or 0)
        for mint in sorted(decs):
            delta = deltas[(owner, mint)]
            if delta:
                cp = next((h for (h, m), d in deltas.items() if m == mint and h != owner and d == -delta), "")
                moves.append(Movement(uid=f"{signature}:spl:{mint}", tx_hash=signature, ts=ts,
                                      asset_key=f"{self.network.id}:{mint}", amount=scaled(delta, decs[mint]),
                                      kind="in" if delta > 0 else "out", counterparty=cp,
                                      from_address=cp if delta > 0 else owner,
                                      to_address=owner if delta > 0 else cp))
        return moves, decs

    # --- staking ----------------------------------------------------------------
    def _stake_accounts(self, owner: str) -> Dict[str, Dict[str, Any]]:
        """Stake accounts whose withdraw authority is the wallet (cached for this sync)."""
        if getattr(self, "_stakes", None) is None:
            res = self._rpc("getProgramAccounts", [STAKE_PROGRAM, {
                "encoding": "jsonParsed", "commitment": "finalized",
                "filters": [{"memcmp": {"offset": 44, "bytes": owner}}]}]) or []
            found: Dict[str, Dict[str, Any]] = {}
            for item in res:
                acct = item.get("account") or {}
                parsed = ((acct.get("data") or {}).get("parsed") or {}) if isinstance(acct.get("data"), dict) else {}
                d = ((parsed.get("info") or {}).get("stake") or {}).get("delegation") or {}
                found[item["pubkey"]] = {
                    "lamports": int(acct.get("lamports") or 0), "voter": d.get("voter") or "",
                    "activation": int(d["activationEpoch"]) if d.get("activationEpoch") is not None else None,
                    "deactivation": int(d["deactivationEpoch"]) if d.get("deactivationEpoch") is not None else None}
            self._stakes = found
        return self._stakes

    def _epoch_info(self) -> Dict[str, Any]:
        if getattr(self, "_epoch", None) is None:
            self._epoch = self._rpc("getEpochInfo", [{"commitment": "finalized"}]) or {}
        return self._epoch

    def _floor_epoch(self) -> int:
        """First epoch whose rewards matter: just before the opening cutoff of the first fiscal
        year (0 = from the start of each stake account when no first fiscal year is set). Slots
        last at least 0.4 s, so the estimate errs on the early side."""
        first = self.ctx.settings.get("first_fiscal_year")
        if not first:
            return 0
        info = self._epoch_info()
        floor_ts = self.ctx.calendar.cutoff(int(first) - 1) - 3 * 86400
        slots_back = max(0, now() - floor_ts) / 0.4
        return max(0, int(int(info.get("absoluteSlot") or 0) - slots_back) // SLOTS_PER_EPOCH)

    MAX_REWARD_EPOCHS = 300   # per sync (about 1.7 years); the next sync continues

    def _rewards(self, owner: str, ranges: Dict[str, Tuple[int, Optional[int]]],
                 state: Dict[str, Any], log: Logger) -> List[Movement]:
        """Inflation rewards credited to the stake accounts: one call per epoch for all of them,
        plus the time of the epoch's reward block."""
        current = int(self._epoch_info().get("epoch") or 0)
        done: Dict[str, int] = dict(state.get("reward_epoch") or {})
        floor = self._floor_epoch()
        first = {a: max(lo, floor) if a not in done else done[a] + 1 for a, (lo, hi) in ranges.items()}
        if not first:
            return []
        start = min(first.values())
        last = min(current - 1, start + self.MAX_REWARD_EPOCHS - 1)
        moves: List[Movement] = []
        for epoch in range(start, last + 1):
            accts = [a for a, (lo, hi) in ranges.items() if first[a] <= epoch and (hi is None or epoch <= hi)]
            if not accts:
                continue
            res = self._rpc("getInflationReward", [accts, {"epoch": epoch, "commitment": "finalized"}]) or []
            paid = [(acct, r) for acct, r in zip(accts, res) if r and int(r.get("amount") or 0)]
            for acct in accts:
                done[acct] = epoch
            if not paid:
                continue
            # Rewards are paid over a few blocks at the start of the next epoch: one block time per
            # epoch, the others dated from their slot (0.4 s each, a few minutes apart at most).
            slot0 = min(int(r["effectiveSlot"]) for _, r in paid)
            t0 = int(self._rpc("getBlockTime", [slot0]) or 0)
            for acct, r in paid:
                ts = t0 + int((int(r["effectiveSlot"]) - slot0) * 0.4)
                moves.append(Movement(uid=f"stake:{acct}:reward:{epoch}", tx_hash="", ts=ts,
                                      asset_key=f"{self.network.id}:stake:{acct}",
                                      amount=scaled(int(r["amount"]), 9), kind="in",
                                      balance_after=scaled(int(r["postBalance"]), 9),
                                      info=f"reward: Solana staking (epoch {epoch})", to_address=acct))
        if last < current - 1:
            log(f"Solana staking rewards read up to epoch {last}; the next sync continues")
        state["reward_epoch"] = done
        return moves

    # --- sync -------------------------------------------------------------------
    def sync(self, wallet: Dict[str, Any], state: Dict[str, Any], log: Logger) -> SyncResult:
        owner = wallet["address"]
        result = SyncResult(state=dict(state))
        token_accounts = self._token_accounts(owner)
        known = list(dict.fromkeys(list(state.get("token_accounts", [])) + [t["pubkey"] for t in token_accounts]))
        try:
            live_stakes = self._stake_accounts(owner)
        except AdapterError as e:
            live_stakes = {}
            result.warnings.append(f"stake accounts not read ({e})")
        stakes: List[str] = list(dict.fromkeys(list(state.get("stake_accounts", [])) + list(live_stakes)))
        spans: Dict[str, List[int]] = {a: list(v) for a, v in (state.get("stake_epochs") or {}).items()}
        cursors: Dict[str, str] = dict(state.get("until", {}))
        sigs: Dict[str, Dict[str, Any]] = {}

        def collect(accounts: Iterable[str], stake: bool) -> None:
            for account in accounts:
                page = self._signatures(account, cursors.get(account))
                if page:
                    cursors[account] = page[0]["signature"]
                    if stake:   # epochs this stake account was used in (for its rewards)
                        ep = [int(x.get("slot") or 0) // SLOTS_PER_EPOCH for x in page]
                        old = spans.get(account)
                        spans[account] = [min(ep + ([old[0]] if old else [])), max(ep + ([old[1]] if old else []))]
                for x in page:
                    sigs.setdefault(x["signature"], x)

        collect([owner] + known, False)
        collect(stakes, True)
        log(f"{wallet['label']}: {len(sigs)} new Solana transactions to read")
        decimals: Dict[str, int] = {t["mint"]: t["decimals"] for t in token_accounts}
        txs: Dict[str, Dict[str, Any]] = {}

        def read(batch: List[Dict[str, Any]], wallet_side: bool) -> List[str]:
            found: List[str] = []
            for n, x in enumerate(batch, 1):
                tx = self.get_transaction(x["signature"])
                if not tx:
                    result.warnings.append(f"transaction {x['signature']} not returned by RPC")
                    continue
                txs[x["signature"]] = tx
                if wallet_side:
                    moves, decs = self.parse_transaction(tx, x["signature"], owner, known)
                    result.movements.extend(moves)
                    decimals.update(decs)
                found += [a for a in self.stake_accounts_in(tx) if a not in stakes and a not in found]
                if n % 100 == 0:
                    log(f"{wallet['label']}: {n}/{len(batch)} transactions read")
            return found

        ordered = sorted(sigs.values(), key=lambda x: (x.get("blockTime") or 0, x.get("slot") or 0))
        new_stakes = read(ordered, True)
        if new_stakes:   # stake accounts seen in the history (closed ones included)
            stakes += new_stakes
            before = set(sigs)
            collect(new_stakes, True)
            read([sigs[k] for k in sigs if k not in before], False)
        for sig, tx in txs.items():
            result.movements.extend(self.parse_stake(tx, sig, owner, stakes))
        if stakes:
            ranges: Dict[str, Tuple[int, Optional[int]]] = {}
            for a in stakes:
                info = live_stakes.get(a)
                lo = spans.get(a, [0, 0])[0]
                if info and info.get("activation") is not None:
                    lo = min(lo, info["activation"]) if a in spans else info["activation"]
                hi: Optional[int] = None
                if not info:
                    hi = spans.get(a, [0, 0])[1]                     # closed: no rewards after its last use
                elif info.get("deactivation") not in (None, NOT_DEACTIVATED):
                    hi = info["deactivation"]
                ranges[a] = (lo, hi)
            try:
                rewards = self._rewards(owner, ranges, result.state, log)
                result.movements.extend(rewards)
                if rewards:
                    log(f"{wallet['label']}: {len(rewards)} staking reward(s)")
            except AdapterError as e:
                result.warnings.append(f"staking rewards not read ({e}): the staked balance after the last "
                                       "transaction is used until the next sync")
        assets: List[AssetInfo] = [assets_mod.native_asset(self.network)]
        assets += [assets_mod.staked_asset(self.network, a) for a in stakes]
        for mint, dec in decimals.items():
            assets.append(assets_mod.token_asset(self.ctx, self.network, mint, decimals=dec))
        result.assets = assets
        result.state["until"] = cursors
        result.state["token_accounts"] = known
        result.state["stake_accounts"] = stakes
        result.state["stake_epochs"] = spans
        return result

    def current_balances(self, wallet: Dict[str, Any], known_assets: List[str],
                         log: Logger) -> Tuple[Dict[str, Decimal], List[AssetInfo]]:
        owner = wallet["address"]
        lamports = self._rpc("getBalance", [owner, {"commitment": "finalized"}])["value"]
        out: Dict[str, Decimal] = {f"{self.network.id}:native": scaled(lamports, 9)}
        infos: List[AssetInfo] = [assets_mod.native_asset(self.network)]
        for ta in self._token_accounts(owner):
            key = f"{self.network.id}:{ta['mint']}"
            out[key] = out.get(key, ZERO) + scaled(ta["amount"], ta["decimals"])
            infos.append(assets_mod.token_asset(self.ctx, self.network, ta["mint"], decimals=ta["decimals"]))
        self.staking_found = []
        try:
            stakes = self._stake_accounts(owner)
            current = int(self._epoch_info().get("epoch") or 0) if stakes else 0
        except AdapterError as e:
            log(f"{wallet['label']}: stake accounts not read ({e})")
            stakes = {}
        for acct, info in stakes.items():
            asset = assets_mod.staked_asset(self.network, acct)
            out[asset.key] = scaled(info["lamports"], 9)
            infos.append(asset)
            amount = scaled(info["lamports"], 9)
            deact = info.get("deactivation")
            active = info.get("voter") and deact in (None, NOT_DEACTIVATED)
            self.staking_found.append(StakePosition(
                asset_key=asset.key, staked=amount if active else ZERO, unbonding=ZERO if active else amount,
                protocol="Solana native staking",
                validator=info["voter"] if active else ("deactivating" if deact is not None and deact >= current
                                                         else "inactive: withdrawable")))
        for key in known_assets:
            if key.startswith(self.network.id + ":") and key not in out:
                out[key] = ZERO  # token account closed
        return out, infos
