"""Asset identity and price mapping."""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Optional

from .models import AssetInfo
from .util import now

# Statuses set by you, never overwritten by a sync. "ghost": a real token that can no longer
# be traded (delisted, dead project): kept in balances, valued at EUR 0 when it has no market.
USER_STATUSES = ("mapped", "spam", "ignored", "ghost")
STATUSES = ("known", "mapped", "unmapped", "spam", "ignored", "ghost")


def native_asset(network) -> AssetInfo:
    n = network.native or {}
    return AssetInfo(
        key=f"{network.id}:native", network=network.id, symbol=n.get("symbol", "?"),
        name=n.get("name", ""), decimals=n.get("decimals"), coingecko_id=n.get("coingecko"),
        binance_symbol=n.get("binance"), status="known",
    )


def staked_asset(network, account: str, label: str = "") -> AssetInfo:
    """Coins in a stake account (a Solana stake account, a staking contract or vault on an EVM
    chain): its own asset, so its balance comes from the chain; priced like the native coin,
    grouped with it on the Dashboard."""
    n = network.native or {}
    sym = n.get("symbol", "?")
    short = f"{account[:4]}…{account[-4:]}" if not account.startswith("0x") else f"{account[:6]}…{account[-4:]}"
    return AssetInfo(
        key=f"{network.id}:stake:{account}", network=network.id, symbol=f"{sym} (staked)"[:24],
        name=f"{label} {short}" if label else f"Stake account {short}", decimals=n.get("decimals"),
        coingecko_id=n.get("coingecko"), binance_symbol=n.get("binance"), status="known",
    )


def token_asset(ctx, network, contract: str, symbol: str = "", name: str = "",
                decimals: Optional[int] = None) -> AssetInfo:
    known = (ctx.tokens.get(network.id) or {}).get(contract)
    if known:
        return AssetInfo(
            key=f"{network.id}:{contract}", network=network.id, symbol=known["symbol"],
            name=known.get("name", name), decimals=known.get("decimals", decimals), contract=contract,
            coingecko_id=known.get("coingecko"), binance_symbol=known.get("binance"), status="known",
        )
    short = contract[:6] + "…" if len(contract) > 10 else contract
    return AssetInfo(key=f"{network.id}:{contract}", network=network.id,
                     symbol=(symbol or short)[:24], name=(name or "")[:80],
                     decimals=decimals, contract=contract, status="unmapped")


def ticker_asset(ctx, network_id: str, ticker: str) -> AssetInfo:
    """Exchange or manual balance known only by ticker (BTC, EUR...)."""
    t = ticker.strip().upper()
    if t in ctx.fiat:
        return AssetInfo(key=f"{network_id}:{t}", network=network_id, symbol=t, name=t,
                         fiat=t, status="known")
    cg = ctx.symbol_coingecko.get(t)
    binance = t if network_id == "binance" else (t if cg else None)
    return AssetInfo(key=f"{network_id}:{t}", network=network_id, symbol=t, name=t,
                     coingecko_id=cg, binance_symbol=binance,
                     status="known" if (cg or binance) else "unmapped")


def upsert(ctx, assets: Iterable[AssetInfo]) -> None:
    """Insert new assets; refresh metadata of existing ones without touching user choices."""
    ts = now()
    with ctx.db.tx():
        for a in assets:
            row = ctx.db.query_one("SELECT status FROM assets WHERE key=?", (a.key,))
            if row is None:
                ctx.db.execute(
                    "INSERT INTO assets(key, network, symbol, name, decimals, contract, coingecko_id,"
                    " binance_symbol, fiat, status, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (a.key, a.network, a.symbol, a.name, a.decimals, a.contract, a.coingecko_id,
                     a.binance_symbol, a.fiat, a.status, ts))
            elif row["status"] in USER_STATUSES:
                ctx.db.execute("UPDATE assets SET decimals=COALESCE(decimals, ?), updated_at=? WHERE key=?",
                               (a.decimals, ts, a.key))
            elif a.status == "known" or row["status"] == "unmapped":
                ctx.db.execute(
                    "UPDATE assets SET symbol=?, name=?, decimals=COALESCE(?, decimals),"
                    " coingecko_id=COALESCE(?, coingecko_id), binance_symbol=COALESCE(?, binance_symbol),"
                    " fiat=COALESCE(?, fiat), status=CASE WHEN ?='known' THEN 'known' ELSE status END,"
                    " updated_at=? WHERE key=?",
                    (a.symbol, a.name, a.decimals, a.coingecko_id, a.binance_symbol, a.fiat,
                     a.status, ts, a.key))


def get(ctx, key: str) -> Optional[Dict[str, Any]]:
    return ctx.db.query_one("SELECT * FROM assets WHERE key=?", (key,))


def all_assets(ctx) -> Dict[str, Dict[str, Any]]:
    return {r["key"]: r for r in ctx.db.query("SELECT * FROM assets")}


def is_priced(asset: Optional[Dict[str, Any]]) -> bool:
    if not asset or asset["status"] in ("spam", "ignored"):
        return False
    return asset["status"] == "ghost" or bool(asset.get("fiat") or asset.get("coingecko_id") or asset.get("binance_symbol"))


# Airdropped scam tokens advertise a website in their name or symbol ("GasDAO.cc", "Visit x.io").
SPAM_NAME = re.compile(r"(\.(com|io|org|net|cc|xyz|app|finance|site|top|vip|pro|live|fi|gg|me)\b)|https?://|www\.|\bvisit\b|\bclaim\b|\breward",
                       re.IGNORECASE)


def looks_like_spam(*texts: str) -> bool:
    return any(t and SPAM_NAME.search(t) for t in texts)


def is_hidden(asset: Optional[Dict[str, Any]]) -> bool:
    return bool(asset) and asset["status"] in ("spam", "ignored")


def group_key(asset: Dict[str, Any]) -> str:
    """Same coin on different chains/platforms is grouped on the dashboard."""
    if asset.get("fiat"):
        return "fiat:" + asset["fiat"]
    if asset.get("coingecko_id"):
        return "cg:" + asset["coingecko_id"]
    if asset.get("binance_symbol"):
        return "bn:" + asset["binance_symbol"]
    return "asset:" + asset["key"]


def update_mapping(ctx, key: str, data: Dict[str, Any]) -> Dict[str, Any]:
    row = get(ctx, key)
    if not row:
        raise KeyError(f"asset {key} not found")
    status = data.get("status", row["status"])
    if status not in STATUSES:
        raise ValueError("invalid status")
    cg = (data.get("coingecko_id", row["coingecko_id"]) or "").strip() or None
    bn = (data.get("binance_symbol", row["binance_symbol"]) or "").strip().upper() or None
    symbol = (data.get("symbol", row["symbol"]) or row["symbol"]).strip()[:24]
    if status in ("unmapped", "known") and (cg != row["coingecko_id"] or bn != row["binance_symbol"]):
        status = "mapped" if (cg or bn) else "unmapped"
    ctx.db.execute(
        "UPDATE assets SET coingecko_id=?, binance_symbol=?, symbol=?, status=?, updated_at=? WHERE key=?",
        (cg, bn, symbol, status, now(), key))
    if status in ("spam", "ignored"):
        ctx.db.execute("UPDATE movements SET category='spam' WHERE asset_key=? AND category_source='auto'",
                       (key,))
    # Mapping changed: year-end values must be recomputed (on request) and movement
    # values are refreshed at the next sync.
    ctx.mark_yearend_stale(f"price mapping of {symbol} changed")
    ctx.db.execute("UPDATE movements SET price_eur=NULL, value_eur=NULL, price_source=NULL WHERE asset_key=?",
                   (key,))
    return get(ctx, key)
