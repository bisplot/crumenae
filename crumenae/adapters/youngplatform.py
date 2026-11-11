"""Young Platform exchange account, through its read-only API (api.youngplatform.com, trader API v1).

Create a read-only API key at pro.youngplatform.com (no trading, no withdrawals). It gives three
values: the key id, the public key and the private key. Every request carries a short-lived
HS256 JWT signed with the private key (claims sub = public key, aud = "trader", iat, exp, and
hash_payload = SHA-256 of the request body, empty for GET) and the key id in X-Api-Key-Id.

  * live balances: GET /private/balance (every vault, summed per currency);
  * trades: GET /private/sor/orders/matches/history (fills of your orders: the two legs and the
    fee), newest first, paged by cursor.

The API lists no deposits or withdrawals, so the history is not complete: past year-end balances
come from the balance read at each sync (a snapshot near the cutoff) or a manual entry, like
Binance's.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from .. import assets as assets_mod
from ..http import HttpError
from ..timeutil import parse_utc
from ..util import D, ZERO
from . import register
from .base import Adapter, AdapterError, AssetInfo, Logger, Movement, SyncResult

BASE = "https://api.youngplatform.com/api/v1/trader"
PAGE = 100
MAX_PAGES = 500


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_jwt(public_key: str, private_key: str, body: bytes = b"", now: Optional[int] = None) -> str:
    """HS256 JWT for one request, as Young Platform's auth guide describes it."""
    iat = int(now if now is not None else time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {"sub": public_key, "aud": "trader", "iat": iat, "exp": iat + 30,
              "hash_payload": hashlib.sha256(body).hexdigest()}
    signing_input = (_b64(json.dumps(header, separators=(",", ":")).encode())
                     + "." + _b64(json.dumps(claims, separators=(",", ":")).encode()))
    sig = hmac.new(private_key.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return signing_input + "." + _b64(sig)


@register
class YoungPlatformAdapter(Adapter):
    id = "youngplatform"
    kind = "exchange"
    needs_address = False
    has_history = False
    credentials = ["key_id", "public_key", "private_key"]

    def validate_address(self, address: str) -> str:
        return address.strip()[:64]   # optional account nickname

    def _base(self) -> str:
        return (self.network.explorer_api or BASE).rstrip("/")

    def _get(self, wallet: Dict[str, Any], path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        key_id, public, private = (self.wallet_secret(wallet, c) for c in self.credentials)
        if not key_id or not public or not private:
            raise AdapterError("Young Platform API key missing: key id, public key and private key "
                               "(Wallets → edit this account)")
        headers = {"Authorization": "Bearer " + make_jwt(public, private), "X-Api-Key-Id": key_id}
        try:
            return self.ctx.http.get_json(self._base() + path, {k: v for k, v in (params or {}).items()
                                                               if v is not None}, headers=headers)
        except HttpError as e:
            if e.status in (401, 403):
                raise AdapterError("Young Platform refused the API key: check the key id, public key and "
                                   "private key (and that the key is enabled for reading)")
            raise AdapterError(f"Young Platform {path}: {e.body[:200] if e.body else e}")

    def _asset(self, ticker: str, infos: Dict[str, AssetInfo]) -> str:
        info = assets_mod.ticker_asset(self.ctx, self.network.id, ticker.upper())
        infos[info.key] = info
        return info.key

    # --- trades -----------------------------------------------------------------
    def parse_match(self, m: Dict[str, Any], infos: Dict[str, AssetInfo]) -> List[Movement]:
        base, _, quote = str(m.get("market") or "").partition("-")
        if not base or not quote:
            return []
        ts = parse_utc(str(m.get("created_at")))
        qty, amount, fee = D(m.get("quantity")), D(m.get("amount")), D(m.get("fee"))
        buy = str(m.get("side")) in ("1", "Buy", "buy", "BUY")
        uid = f"yp:match:{m['id']}"
        venue = str(m.get("venue") or "")
        moves = [
            Movement(uid=f"{uid}:base", tx_hash="", ts=ts, asset_key=self._asset(base, infos),
                     amount=qty if buy else -qty, kind="in" if buy else "out", counterparty=venue,
                     info=f"trade {m.get('market')}"),
            Movement(uid=f"{uid}:quote", tx_hash="", ts=ts, asset_key=self._asset(quote, infos),
                     amount=-amount if buy else amount, kind="out" if buy else "in", counterparty=venue,
                     info=f"trade {m.get('market')}"),
        ]
        if fee and m.get("fee_currency"):
            moves.append(Movement(uid=f"{uid}:fee", tx_hash="", ts=ts,
                                  asset_key=self._asset(str(m["fee_currency"]), infos), amount=-fee,
                                  kind="fee", counterparty=venue, info="trading fee"))
        return moves

    def sync(self, wallet: Dict[str, Any], state: Dict[str, Any], log: Logger) -> SyncResult:
        result = SyncResult(state=dict(state))
        infos: Dict[str, AssetInfo] = {}
        known = int(state.get("last_match_id") or 0)
        params: Dict[str, Any] = {"limit": PAGE}
        newest = known
        matches: List[Dict[str, Any]] = []
        for _ in range(MAX_PAGES):
            page = self._get(wallet, "/private/sor/orders/matches/history", params) or {}
            reached = False
            for m in page.get("data") or []:
                mid = int(m.get("id") or 0)
                if known and mid <= known:
                    reached = True
                    break
                matches.append(m)
                newest = max(newest, mid)
            cursor = page.get("next_cursor") or {}
            if reached or not page.get("has_more") or not cursor:
                break
            params = {"limit": PAGE, "before": cursor.get("timestamp"), "before_id": cursor.get("id")}
        for m in matches:
            result.movements.extend(self.parse_match(m, infos))
        log(f"{wallet['label']}: {len(matches)} new Young Platform trades (deposits and withdrawals are not "
            "listed by the API)")
        result.assets = list(infos.values())
        if newest:
            result.state["last_match_id"] = newest
        return result

    # --- live balances ----------------------------------------------------------
    def current_balances(self, wallet: Dict[str, Any], known_assets: List[str],
                         log: Logger) -> Tuple[Dict[str, Decimal], List[AssetInfo]]:
        infos: Dict[str, AssetInfo] = {}
        totals: Dict[str, Decimal] = {}
        for b in (self._get(wallet, "/private/balance") or {}).get("balances") or []:
            amount = D(b.get("amount"))
            if b.get("currency") and amount:
                key = self._asset(str(b["currency"]), infos)
                totals[key] = totals.get(key, ZERO) + amount
        for key in known_assets:
            if key.startswith(self.network.id + ":") and key not in totals:
                totals[key] = ZERO
        return totals, list(infos.values())
