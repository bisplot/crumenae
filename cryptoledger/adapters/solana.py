"""Solana. With a Helius API key (Settings → API keys, free plan available) the
app calls Helius; without one, the public Solana endpoint (slow, rate-limited).
Another provider can be used by setting a full "Solana RPC URL" instead.

Every transaction carries the balances before and after it, so SOL and SPL
token changes are exact, and the SOL balance after each transaction is stored:
the year-end SOL balance is the chain's own number, not a sum.
"""
from __future__ import annotations

import urllib.parse
from collections import defaultdict
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .. import assets as assets_mod
from ..b58 import b58decode
from ..http import HttpError
from ..util import ZERO, scaled
from . import register
from .base import Adapter, AdapterError, AssetInfo, Logger, Movement, SyncResult

TOKEN_PROGRAMS = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   # SPL Token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",   # Token-2022
)
SIG_PAGE = 1000
HELIUS_URL = "https://mainnet.helius-rpc.com/?api-key="


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
    def _url(self) -> str:
        """Explicit RPC URL > Helius API key > public endpoint."""
        explicit = self.secret("solana_rpc_url")
        if explicit:
            return explicit
        key = self.secret("helius_api_key")
        if key:
            return HELIUS_URL + urllib.parse.quote(key, safe="")
        return self.network.rpc_url

    def _rpc(self, method: str, params: List[Any]) -> Any:
        for attempt in range(4):
            try:
                resp = self.ctx.http.post_json(self._url(), {"jsonrpc": "2.0", "id": 1,
                                                             "method": method, "params": params})
            except HttpError as e:
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

    # --- sync -------------------------------------------------------------------
    def sync(self, wallet: Dict[str, Any], state: Dict[str, Any], log: Logger) -> SyncResult:
        owner = wallet["address"]
        result = SyncResult(state=dict(state))
        token_accounts = self._token_accounts(owner)
        known = list(dict.fromkeys(list(state.get("token_accounts", [])) + [t["pubkey"] for t in token_accounts]))
        cursors: Dict[str, str] = dict(state.get("until", {}))
        sigs: Dict[str, Dict[str, Any]] = {}
        for account in [owner] + known:
            page = self._signatures(account, cursors.get(account))
            if page:
                cursors[account] = page[0]["signature"]
            for s in page:
                sigs[s["signature"]] = s
        log(f"{wallet['label']}: {len(sigs)} new Solana transactions to read")
        decimals: Dict[str, int] = {t["mint"]: t["decimals"] for t in token_accounts}
        ordered = sorted(sigs.values(), key=lambda s: (s.get("blockTime") or 0, s.get("slot") or 0))
        for n, s in enumerate(ordered, 1):
            tx = self._rpc("getTransaction", [s["signature"], {"encoding": "jsonParsed",
                                                               "maxSupportedTransactionVersion": 0,
                                                               "commitment": "finalized"}])
            if not tx:
                result.warnings.append(f"transaction {s['signature']} not returned by RPC")
                continue
            moves, decs = self.parse_transaction(tx, s["signature"], owner, known)
            result.movements.extend(moves)
            decimals.update(decs)
            if n % 100 == 0:
                log(f"{wallet['label']}: {n}/{len(ordered)} transactions read")
        assets: List[AssetInfo] = [assets_mod.native_asset(self.network)]
        for mint, dec in decimals.items():
            assets.append(assets_mod.token_asset(self.ctx, self.network, mint, decimals=dec))
        result.assets = assets
        result.state["until"] = cursors
        result.state["token_accounts"] = known
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
        for key in known_assets:
            if key.startswith(self.network.id + ":") and key not in out:
                out[key] = ZERO  # token account closed
        return out, infos
