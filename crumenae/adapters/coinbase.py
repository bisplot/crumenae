"""Coinbase account, through the Coinbase App API v2 (api.coinbase.com) with a read-only CDP API key.

Create the key on the Coinbase Developer Platform (portal.cdp.coinbase.com → API keys) with the
"View" permission only. It has two parts: the key name (organizations/…/apiKeys/…, or a key id)
and the private key (an ECDSA PEM block, or an Ed25519 key). Every request carries a fresh JWT
signed on your computer (crumenae/jwt_sign.py): claims sub = key name, iss = "cdp", nbf, exp
(2 minutes) and uri = "GET api.coinbase.com/<path>", header kid = key name and a random nonce.

  * accounts: GET /v2/accounts (one account per currency) → live balances;
  * history: GET /v2/accounts/{id}/transactions for each account, newest first, paged; every
    completed transaction (buy, sell, convert, send, receive, staking reward…) is one movement
    with its signed amount in that currency, so the balance at any date is rebuilt exactly.
"""
from __future__ import annotations

import secrets as _secrets
import time
import urllib.parse
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from .. import assets as assets_mod
from .. import jwt_sign
from ..http import HttpError
from ..timeutil import parse_utc
from ..util import D, ZERO
from . import register
from .base import Adapter, AdapterError, AssetInfo, Logger, Movement, SyncResult

HOST = "api.coinbase.com"
PAGE = 100
MAX_PAGES = 1000
REWARD_TYPES = {"staking_reward", "inflation_reward", "interest", "earn_payout", "reward", "incentives_reward_payout",
                "coinbase_earn_payout", "cardbuyback", "card_buyback"}
TRADE_TYPES = {"buy", "sell", "trade", "advanced_trade_fill", "retail_simple_dust", "convert"}


@register
class CoinbaseAdapter(Adapter):
    id = "coinbase"
    kind = "exchange"
    needs_address = False
    has_history = True       # every balance change is a transaction of its account
    credentials = ["api_key_name", "api_private_key"]

    def validate_address(self, address: str) -> str:
        return address.strip()[:64]   # optional account nickname

    def _base(self) -> str:
        return (self.network.explorer_api or f"https://{HOST}").rstrip("/")

    def _get(self, wallet: Dict[str, Any], path_and_query: str) -> Any:
        name, private = self.wallet_secret(wallet, "api_key_name"), self.wallet_secret(wallet, "api_private_key")
        if not name or not private:
            raise AdapterError("Coinbase API key missing: key name and private key (Wallets → edit this account)")
        path = urllib.parse.urlsplit(path_and_query).path
        host = urllib.parse.urlsplit(self._base()).hostname or HOST
        now = int(time.time())
        try:
            token = jwt_sign.jwt({"sub": name, "iss": "cdp", "nbf": now, "exp": now + 120, "uri": f"GET {host}{path}"},
                                 private, {"kid": name, "nonce": _secrets.token_hex(16)})
        except (ValueError, IndexError) as e:
            raise AdapterError(f"Coinbase private key not readable ({e}): paste it exactly as Coinbase gave it")
        try:
            return self.ctx.http.get_json(self._base() + path_and_query, headers={"Authorization": f"Bearer {token}"})
        except HttpError as e:
            if e.status in (401, 403):
                raise AdapterError("Coinbase refused the API key: check the key name and private key, and that the "
                                   "key has the View permission")
            raise AdapterError(f"Coinbase {path}: {e.body[:200] if e.body else e}")

    def _pages(self, wallet: Dict[str, Any], path: str, stop_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Items of a v2 list, newest first, until stop_id (already read) or the end."""
        out: List[Dict[str, Any]] = []
        url: Optional[str] = f"{path}?limit={PAGE}"
        for _ in range(MAX_PAGES):
            if not url:
                break
            page = self._get(wallet, url) or {}
            for item in page.get("data") or []:
                if stop_id and item.get("id") == stop_id:
                    return out
                out.append(item)
            nxt = (page.get("pagination") or {}).get("next_uri")
            url = nxt if nxt and nxt.startswith("/") else None
        return out

    def _accounts(self, wallet: Dict[str, Any]) -> List[Dict[str, Any]]:
        if getattr(self, "_cached_accounts", None) is None:
            self._cached_accounts = self._pages(wallet, "/v2/accounts")
        return self._cached_accounts

    def _asset(self, ticker: str, infos: Dict[str, AssetInfo]) -> str:
        info = assets_mod.ticker_asset(self.ctx, self.network.id, ticker.upper())
        infos[info.key] = info
        return info.key

    def parse_transaction(self, tx: Dict[str, Any], infos: Dict[str, AssetInfo]) -> Optional[Movement]:
        if str(tx.get("status") or "completed") != "completed":
            return None   # pending or failed: not (yet) part of the balance
        amount = tx.get("amount") or {}
        value = D(amount.get("amount"))
        if not value or not amount.get("currency"):
            return None
        kind = str(tx.get("type") or "")
        network = tx.get("network") or {}
        party = tx.get("to") if value < 0 else tx.get("from")
        counterparty = str((party or {}).get("address") or (party or {}).get("email") or "")
        if kind in REWARD_TYPES:
            info = f"reward: Coinbase {kind.replace('_', ' ')}"
        elif kind in TRADE_TYPES:
            info = "trade"
        else:
            info = kind.replace("_", " ")
        return Movement(uid=f"cb:{tx['id']}", tx_hash=str(network.get("hash") or ""),
                        ts=parse_utc(str(tx.get("created_at"))), asset_key=self._asset(amount["currency"], infos),
                        amount=value, kind="in" if value > 0 else "out", counterparty=counterparty,
                        info=info, to_address=counterparty if value < 0 else "")

    def sync(self, wallet: Dict[str, Any], state: Dict[str, Any], log: Logger) -> SyncResult:
        result = SyncResult(state=dict(state))
        newest: Dict[str, str] = dict(state.get("newest_tx") or {})
        infos: Dict[str, AssetInfo] = {}
        count = 0
        for acct in self._accounts(wallet):
            aid = str(acct.get("id") or "")
            if not aid:
                continue
            txs = self._pages(wallet, f"/v2/accounts/{aid}/transactions", newest.get(aid))
            if txs:
                newest[aid] = txs[0]["id"]
            for tx in txs:
                m = self.parse_transaction(tx, infos)
                if m:
                    result.movements.append(m)
                    count += 1
        log(f"{wallet['label']}: {count} new Coinbase transactions in {len(self._accounts(wallet))} accounts")
        result.assets = list(infos.values())
        result.state["newest_tx"] = newest
        return result

    def current_balances(self, wallet: Dict[str, Any], known_assets: List[str],
                         log: Logger) -> Tuple[Dict[str, Decimal], List[AssetInfo]]:
        self._cached_accounts = None   # live: read again
        infos: Dict[str, AssetInfo] = {}
        totals: Dict[str, Decimal] = {}
        for acct in self._accounts(wallet):
            bal = acct.get("balance") or {}
            amount = D(bal.get("amount"))
            if bal.get("currency") and amount:
                key = self._asset(bal["currency"], infos)
                totals[key] = totals.get(key, ZERO) + amount
        for key in known_assets:
            if key.startswith(self.network.id + ":") and key not in totals:
                totals[key] = ZERO
        return totals, list(infos.values())
