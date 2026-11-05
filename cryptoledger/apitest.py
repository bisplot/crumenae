"""The "Test" button of each API: one call, the cheapest the provider offers, to check
that the service answers and the key is accepted.

Only CoinMarketCap has a test call that costs nothing (/v1/key/info). Elsewhere the
cheapest call still counts at the provider (Etherscan: 1 call; Solscan: 100 compute
units, and /monitor/usage also returns your usage), so it is counted here too.
"""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from typing import Any, Dict, Optional

from .http import HttpError

ETHERSCAN = "https://api.etherscan.io/v2/api"
SOLSCAN = "https://pro-api.solscan.io/v2.0"


def _result(ok: bool, message: str, cost: str) -> Dict[str, Any]:
    return {"ok": ok, "message": message, "cost": cost}


def _first(d: Any, *names: str) -> Optional[Any]:
    if isinstance(d, dict):
        for n in names:
            if d.get(n) is not None:
                return d[n]
    return None


def test_etherscan(ctx) -> Dict[str, Any]:
    key = ctx.secrets.get("etherscan_api_key")
    cost = "1 call"
    if not key:
        return _result(False, "No Etherscan key saved.", "none")
    data = ctx.http.get_json(ETHERSCAN, {"chainid": 1, "module": "proxy", "action": "eth_blockNumber", "apikey": key})
    block = data.get("result") if isinstance(data, dict) else None
    if isinstance(block, str) and block.startswith("0x"):
        return _result(True, f"Key accepted. Latest Ethereum block: {int(block, 16):,}.", cost)
    return _result(False, f"Etherscan refused: {data.get('result') or data.get('message') or data}", cost)


def test_solana(ctx) -> Dict[str, Any]:
    key = ctx.secrets.get("solscan_api_key")
    if not key:
        net = ctx.networks.get("solana")
        resp = ctx.http.post_json(net.rpc_url, {"jsonrpc": "2.0", "id": 1, "method": "getSlot", "params": []})
        if "result" in resp:
            return _result(True, f"No Solscan key: the public Solana endpoint answers (slot {resp['result']:,}). "
                                 "It is slow; a Solscan key is recommended.", "1 call to the public endpoint")
        return _result(False, f"Public Solana endpoint: {resp.get('error')}", "1 call")
    cost = "100 compute units"
    try:
        data = ctx.http.get_json(SOLSCAN + "/monitor/usage", headers={"token": key})
    except HttpError as e:
        if e.status in (401, 403):
            return _result(False, "Solscan refused the key (%d). Use the **V2** key from Solscan → API Management "
                                  "(the V1 key does not work on these endpoints) and check that your Pro API "
                                  "plan is active." % e.status, cost)
        raise
    usage = data.get("data") if isinstance(data, dict) else None
    remaining = _first(usage, "remaining_cus", "remaining_cu", "remaining")
    used = _first(usage, "total_requests_24h", "total_cu_24h", "used_cus", "used")
    extra = []
    if remaining is not None:
        extra.append(f"compute units left: {remaining:,}" if isinstance(remaining, int) else f"left: {remaining}")
    if used is not None:
        extra.append(f"used: {used:,}" if isinstance(used, int) else f"used: {used}")
    return _result(True, "Key accepted." + (" " + ", ".join(extra) + "." if extra else ""), cost)


def test_coingecko(ctx) -> Dict[str, Any]:
    from .prices.coingecko import CoinGeckoPrices
    base, headers = CoinGeckoPrices(ctx)._base()
    data = ctx.http.get_json(base + "/ping", headers=headers)
    if isinstance(data, dict) and data.get("gecko_says"):
        who = "with your key" if headers else "without a key (public access)"
        return _result(True, f"CoinGecko answers {who}.", "1 call")
    return _result(False, f"Unexpected answer: {data}", "1 call")


def test_coinmarketcap(ctx) -> Dict[str, Any]:
    key = ctx.secrets.get("coinmarketcap_api_key")
    if not key:
        return _result(False, "No CoinMarketCap key saved.", "none")
    data = ctx.http.get_json("https://pro-api.coinmarketcap.com/v1/key/info",
                             headers={"X-CMC_PRO_API_KEY": key}, count=False)
    info = (data or {}).get("data") or {}
    plan, usage = info.get("plan") or {}, info.get("usage") or {}
    day = (usage.get("current_day") or {}).get("credits_used")
    month = (usage.get("current_month") or {}).get("credits_left")
    parts = [f"{plan.get('rate_limit_minute')} calls/min" if plan.get("rate_limit_minute") else "",
             f"{plan.get('credit_limit_monthly'):,} credits/month" if plan.get("credit_limit_monthly") else "",
             f"used today: {day}" if day is not None else "", f"left this month: {month:,}" if isinstance(month, int) else ""]
    return _result(True, "Key accepted. " + ", ".join(p for p in parts if p) + ".", "free (no credits)")


def test_binance_data(ctx) -> Dict[str, Any]:
    ctx.http.get_json("https://data-api.binance.vision/api/v3/ping", count=False)
    return _result(True, "Binance public prices answer (no key needed).", "free")


def test_ecb(ctx) -> Dict[str, Any]:
    end = date.today()
    text = ctx.http.get_text("https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A",
                             {"startPeriod": (end - timedelta(days=10)).isoformat(), "endPeriod": end.isoformat(),
                              "format": "csvdata"}, count=False)
    rows = [r for r in csv.DictReader(io.StringIO(text)) if r.get("OBS_VALUE")]
    if rows:
        return _result(True, f"ECB answers: USD/EUR {rows[-1]['OBS_VALUE']} on {rows[-1]['TIME_PERIOD']}.", "free")
    return _result(False, "ECB answered without rates.", "free")


def test_moralis(ctx) -> Dict[str, Any]:
    key = ctx.secrets.get("moralis_api_key")
    if not key:
        return _result(False, "No Moralis key saved.", "none")
    data = ctx.http.get_json("https://deep-index.moralis.io/api/v2.2/web3/version", headers={"X-API-Key": key})
    return _result(True, f"Key accepted (API {data.get('version', '?')}).", "1 call")


def test_trongrid(ctx) -> Dict[str, Any]:
    key = ctx.secrets.get("trongrid_api_key")
    headers = {"TRON-PRO-API-KEY": key} if key else {}
    ctx.http.get_json("https://api.trongrid.io/wallet/getnowblock", headers=headers)
    return _result(True, "TronGrid answers" + (" with your key." if key else " (no key)."), "1 call")


TESTS = {"etherscan": test_etherscan, "solana": test_solana, "coingecko": test_coingecko,
         "coinmarketcap": test_coinmarketcap, "binance_data": test_binance_data, "ecb": test_ecb,
         "moralis": test_moralis, "trongrid": test_trongrid}


def run(ctx, service: str) -> Dict[str, Any]:
    fn = TESTS.get(service)
    if not fn:
        return _result(False, "No test available for this service.", "none")
    if ctx.offline:
        return _result(False, "Demo/offline mode: no API is called.", "none")
    try:
        return fn(ctx)
    except HttpError as e:
        if e.status in (401, 403):
            return _result(False, f"Key refused ({e.status}): check that it is copied in full and active.", "1 call")
        return _result(False, f"No answer: {e}", "?")
