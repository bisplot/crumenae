"""The "Test" button of each API: one call, the cheapest the provider offers, to check
that the service answers and the key is accepted.

Only CoinMarketCap has a test call that costs nothing (/v1/key/info). Elsewhere the
cheapest call still counts at the provider (Etherscan: 1 call; Helius: 1 credit),
so it is counted here too.
"""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta
from typing import Any, Dict, Optional

from .http import HttpError

ETHERSCAN = "https://api.etherscan.io/v2/api"


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
    from .adapters import get_adapter
    adapter = get_adapter(ctx, ctx.networks.get("solana"))
    url = adapter._url()
    via_helius = "helius" in url
    resp = ctx.http.post_json(url, {"jsonrpc": "2.0", "id": 1, "method": "getSlot", "params": []})
    if "result" not in resp:
        return _result(False, f"Solana RPC: {(resp.get('error') or {}).get('message') or resp}", "1 call")
    if via_helius:
        return _result(True, f"Helius key accepted. Current Solana slot: {resp['result']:,}.", "1 credit")
    return _result(True, f"No Helius key: the public Solana endpoint answers (slot {resp['result']:,}). "
                         "It is slow and rate-limited; a free Helius key is recommended.", "1 call")


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


def test_nodereal(ctx) -> Dict[str, Any]:
    key = ctx.secrets.get("nodereal_api_key")
    if not key:
        return _result(False, "No NodeReal key saved.", "none")
    data = ctx.http.post_json(f"https://bsc-mainnet.nodereal.io/v1/{key}",
                              {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []})
    if not isinstance(data, dict) or data.get("error") or not data.get("result"):
        return _result(False, f"NodeReal answered with an error: {(data or {}).get('error')}", "1 call")
    return _result(True, f"Key accepted: BSC block {int(data['result'], 16):,}.", "1 call")


def test_trongrid(ctx) -> Dict[str, Any]:
    key = ctx.secrets.get("trongrid_api_key")
    headers = {"TRON-PRO-API-KEY": key} if key else {}
    ctx.http.get_json("https://api.trongrid.io/wallet/getnowblock", headers=headers)
    return _result(True, "TronGrid answers" + (" with your key." if key else " (no key)."), "1 call")


TESTS = {"etherscan": test_etherscan, "solana": test_solana, "coingecko": test_coingecko,
         "coinmarketcap": test_coinmarketcap, "binance_data": test_binance_data, "ecb": test_ecb,
         "moralis": test_moralis, "nodereal": test_nodereal, "trongrid": test_trongrid}


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
