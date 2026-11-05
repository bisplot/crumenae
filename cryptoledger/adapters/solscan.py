"""Solana history and balances from the Solscan Pro API (v2.0, paid plans from "Lite").

Used by the Solana network when a Solscan API key is set (Settings → Blockchain APIs):

  /account/balance_change   every balance change of the wallet: token, raw amount,
                            inc/dec, balance before and after
  /account/transactions     fee and signer of each transaction (who paid the fee)
  /account/transfer         sender and recipient (optional: fills from/to addresses)
  /account/detail           live SOL balance
  /account/token-accounts   live token balances

Movements get the same ids as with the RPC source (<signature>:sol, :fee, :spl:<mint>),
so switching between Solscan and Helius never duplicates anything.
"""
from __future__ import annotations

from collections import OrderedDict
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from ..http import HttpError
from ..models import AdapterError, Movement
from ..util import scaled

BASE = "https://pro-api.solscan.io/v2.0"
PAGE = 100
TX_PAGE = 40
# Solscan's id for native SOL, and the wrapped-SOL mint (native when held by the wallet itself).
SOL_IDS = {"So11111111111111111111111111111111111111111", "So11111111111111111111111111111111111111112"}


def _first(row: Dict[str, Any], *names: str) -> Any:
    for n in names:
        if row.get(n) not in (None, ""):
            return row[n]
    return None


class SolscanClient:
    def __init__(self, ctx, key: str):
        self.ctx = ctx
        self.key = key

    def get(self, path: str, params: Dict[str, Any]) -> Any:
        try:
            data = self.ctx.http.get_json(BASE + path, params, {"token": self.key})
        except HttpError as e:
            if e.status in (401, 403):
                raise AdapterError(f"Solscan {path}: key refused ({e.status}). Use the V2 key from Solscan → "
                                   "API Management (not the V1 key) and check that the Pro API plan is active; "
                                   "the Test button in Settings checks it")
            raise AdapterError(f"Solscan {path}: {e}")
        if isinstance(data, dict) and data.get("success") is False:
            err = data.get("errors") or data.get("error") or data
            raise AdapterError(f"Solscan {path}: {err}")
        return data.get("data") if isinstance(data, dict) else data

    def paged(self, path: str, params: Dict[str, Any], page_size: int = PAGE) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        page = 1
        while True:
            batch = self.get(path, dict(params, page=page, page_size=page_size)) or []
            if isinstance(batch, dict):          # some endpoints wrap lists: {"items": [...]}
                batch = batch.get("items") or batch.get("data") or []
            rows.extend(batch)
            if len(batch) < page_size:
                return rows
            page += 1


def _is_native(row: Dict[str, Any], owner: str) -> bool:
    mint = _first(row, "token_address", "token") or ""
    account = _first(row, "token_account", "address") or owner
    return mint in SOL_IDS and account == owner


def fees_paid(client: SolscanClient, owner: str, since: int) -> Dict[str, int]:
    """{signature: fee in lamports} for transactions where the wallet paid the fee."""
    out: Dict[str, int] = {}
    before: Optional[str] = None
    while True:
        params: Dict[str, Any] = {"address": owner, "limit": TX_PAGE}
        if before:
            params["before"] = before
        rows = client.get("/account/transactions", params) or []
        for tx in rows:
            sig = _first(tx, "tx_hash", "trans_id", "signature")
            signers = tx.get("signer") or tx.get("signers") or []
            payer = signers[0] if isinstance(signers, list) and signers else signers
            if sig and payer == owner:
                out[sig] = int(tx.get("fee") or 0)
        if len(rows) < TX_PAGE or int(_first(rows[-1], "block_time") or 0) < since:
            return out
        before = _first(rows[-1], "tx_hash", "trans_id", "signature")


def parties(client: SolscanClient, owner: str, since: int) -> Dict[Tuple[str, str], Tuple[str, str]]:
    """{(signature, mint or 'native'): (from, to)} from the transfer list."""
    out: Dict[Tuple[str, str], Tuple[str, str]] = {}
    for t in client.paged("/account/transfer", {"address": owner, "sort_by": "block_time",
                                                 "sort_order": "asc", "from_time": since}):
        sig = _first(t, "trans_id", "tx_hash", "signature")
        mint = _first(t, "token_address") or ""
        key = "native" if mint in SOL_IDS else mint
        frm = _first(t, "from_address") or ""
        to = _first(t, "to_address") or ""
        if sig and (sig, key) not in out:
            out[(sig, key)] = (frm, to)
    return out


def movements(ctx, network_id: str, client: SolscanClient, owner: str, since: int,
              log) -> Tuple[List[Movement], Dict[str, int], int]:
    """Movements of `owner` since `since`, {mint: decimals}, newest block time seen."""
    changes = client.paged("/account/balance_change", {"address": owner, "sort_by": "block_time",
                                                       "sort_order": "asc", "from_time": since})
    fees = fees_paid(client, owner, since)
    try:
        who = parties(client, owner, since)
    except AdapterError as e:  # addresses are a nice-to-have: never fail the sync for them
        log(f"Solscan transfers unavailable, from/to left empty ({e})")
        who = {}
    log(f"Solscan: {len(changes)} balance changes, {len(fees)} fee payments")

    # Aggregate per (signature, asset): a wallet can own several token accounts of one mint.
    agg: "OrderedDict[Tuple[str, str], Dict[str, Any]]" = OrderedDict()
    decimals: Dict[str, int] = {}
    newest = since
    for row in changes:
        sig = _first(row, "trans_id", "tx_hash", "signature")
        if not sig:
            continue
        ts = int(_first(row, "block_time") or 0)
        newest = max(newest, ts)
        dec = int(_first(row, "token_decimals", "decimals") or 0)
        pre, post = _first(row, "pre_balance"), _first(row, "post_balance")
        if pre is not None and post is not None:
            delta = int(post) - int(pre)
        else:
            delta = int(_first(row, "amount") or 0)
            if (_first(row, "change_type", "flow") or "").lower() in ("dec", "out"):
                delta = -abs(delta)
        native = _is_native(row, owner)
        asset = "native" if native else (_first(row, "token_address", "token") or "")
        if not asset:
            continue
        a = agg.setdefault((sig, asset), {"ts": ts, "delta": 0, "post": None, "dec": 9 if native else dec})
        a["delta"] += delta
        if native and post is not None:
            a["post"] = int(post)
        if not native:
            decimals[asset] = dec

    out: List[Movement] = []
    native_key = f"{network_id}:native"
    for (sig, asset), a in agg.items():
        frm, to = who.get((sig, asset), ("", ""))
        if asset == "native":
            fee = fees.get(sig, 0)
            transfer = a["delta"] + fee
            after = scaled(a["post"], 9) if a["post"] is not None else None
            if fee:
                out.append(Movement(uid=f"{sig}:fee", tx_hash=sig, ts=a["ts"], asset_key=native_key,
                                    amount=-scaled(fee, 9), kind="fee", balance_after=after, from_address=owner))
            if transfer:
                cp = to if transfer < 0 else frm
                out.append(Movement(uid=f"{sig}:sol", tx_hash=sig, ts=a["ts"], asset_key=native_key,
                                    amount=scaled(transfer, 9), kind="in" if transfer > 0 else "out",
                                    balance_after=after, counterparty=cp,
                                    from_address=frm or (owner if transfer < 0 else ""),
                                    to_address=to or (owner if transfer > 0 else "")))
        elif a["delta"]:
            cp = to if a["delta"] < 0 else frm
            out.append(Movement(uid=f"{sig}:spl:{asset}", tx_hash=sig, ts=a["ts"],
                                asset_key=f"{network_id}:{asset}", amount=scaled(a["delta"], a["dec"]),
                                kind="in" if a["delta"] > 0 else "out", counterparty=cp,
                                from_address=frm or (owner if a["delta"] < 0 else ""),
                                to_address=to or (owner if a["delta"] > 0 else "")))
    return out, decimals, newest


def balances(client: SolscanClient, network_id: str, owner: str) -> Tuple[Dict[str, Decimal], Dict[str, int]]:
    detail = client.get("/account/detail", {"address": owner}) or {}
    out: Dict[str, Decimal] = {f"{network_id}:native": scaled(int(detail.get("lamports") or 0), 9)}
    decimals: Dict[str, int] = {}
    for ta in client.paged("/account/token-accounts", {"address": owner, "type": "token"}, page_size=40):
        mint = _first(ta, "token_address") or ""
        if not mint:
            continue
        dec = int(_first(ta, "token_decimals", "decimals") or 0)
        key = f"{network_id}:{mint}"
        out[key] = out.get(key, Decimal(0)) + scaled(int(_first(ta, "amount") or 0), dec)
        decimals[mint] = dec
    return out, decimals
