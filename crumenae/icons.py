"""Coin icons for the web pages, bundled in web/icons/ (no request to any icon server).

Icons are chosen by the asset's CoinGecko id (or, for an exchange coin, its Binance ticker), never
by its symbol alone: a spam token calling itself "USDC" gets no icon. Spam and ignored assets get
none either. Sources and licences: web/icons/README.md."""
from __future__ import annotations

from typing import Any, Dict, Optional

# CoinGecko id -> file in web/icons/
BY_COINGECKO: Dict[str, str] = {
    "arbitrum": "arb.png",
    "avalanche-2": "avax.svg",
    "binance-bitcoin": "btc.svg",
    "binance-peg-cardano": "ada.svg",
    "binance-peg-dogecoin": "doge.svg",
    "binance-peg-polkadot": "dot.svg",
    "binance-peg-xrp": "xrp.svg",
    "binance-usd": "busd.png",
    "binancecoin": "bnb.svg",
    "bitcoin": "btc.svg",
    "bitcoin-cash": "bch.svg",
    "bittorrent": "btt.svg",
    "bonk": "bonk.png",
    "celo": "celo.png",
    "chainlink": "link.svg",
    "dai": "dai.svg",
    "dogecoin": "doge.svg",
    "ethereum": "eth.svg",
    "euro-coin": "eurc.png",
    "first-digital-usd": "fdusd.png",
    "jupiter-exchange-solana": "jup.png",
    "litecoin": "ltc.svg",
    "mantle": "mnt.png",
    "monad": "mon.png",
    "optimism": "op.png",
    "pancakeswap-token": "cake.png",
    "pepe": "pepe.png",
    "polygon-ecosystem-token": "matic.svg",
    "ronin": "ron.png",
    "shiba-inu": "shib.png",
    "solana": "sol.svg",
    "sonic-3": "s.png",
    "stellar": "xlm.svg",
    "terra-luna": "lunc.png",
    "terra-luna-2": "luna.png",
    "terrausd": "ustc.png",
    "tether": "usdt.svg",
    "tron": "trx.svg",
    "uniswap": "uni.svg",
    "usd-coin": "usdc.svg",
    "wbnb": "bnb.svg",
    "weth": "eth.svg",
    "wrapped-bitcoin": "wbtc.svg",
    "wrapped-solana": "sol.svg",
    "wrapped-tron": "trx.svg",
    "xdai": "dai.svg",
}

# Binance ticker -> file (exchange accounts: Binance, Coinbase, Young Platform)
BY_TICKER: Dict[str, str] = {
    "ADA": "ada.svg",
    "ARB": "arb.png",
    "AVAX": "avax.svg",
    "BCH": "bch.svg",
    "BNB": "bnb.svg",
    "BONK": "bonk.png",
    "BTC": "btc.svg",
    "BTCB": "btc.svg",
    "BTT": "btt.svg",
    "BUSD": "busd.png",
    "CAKE": "cake.png",
    "CELO": "celo.png",
    "DAI": "dai.svg",
    "DOGE": "doge.svg",
    "DOT": "dot.svg",
    "ETH": "eth.svg",
    "EURC": "eurc.png",
    "FDUSD": "fdusd.png",
    "JUP": "jup.png",
    "LINK": "link.svg",
    "LTC": "ltc.svg",
    "LUNA": "luna.png",
    "LUNC": "lunc.png",
    "MATIC": "matic.svg",
    "OP": "op.png",
    "PEPE": "pepe.png",
    "POL": "matic.svg",
    "RON": "ron.png",
    "SHIB": "shib.png",
    "SOL": "sol.svg",
    "TRX": "trx.svg",
    "UNI": "uni.svg",
    "USDC": "usdc.svg",
    "USDT": "usdt.svg",
    "USTC": "ustc.png",
    "WBNB": "bnb.svg",
    "WBTC": "wbtc.svg",
    "WETH": "eth.svg",
    "XDAI": "dai.svg",
    "XLM": "xlm.svg",
    "XRP": "xrp.svg",
}


# Fiat balances on exchanges
BY_FIAT: Dict[str, str] = {"EUR": "eur.svg", "USD": "usd.svg"}


def icon_for(asset: Optional[Dict[str, Any]]) -> Optional[str]:
    """URL of the asset's icon, or None (the page shows a coloured dot)."""
    if not asset or asset.get("status") in ("spam", "ignored", "unmapped"):
        return None
    f = (BY_FIAT.get(str(asset.get("fiat") or "").upper()) or BY_COINGECKO.get(str(asset.get("coingecko_id") or ""))
         or BY_TICKER.get(str(asset.get("binance_symbol") or "").upper()))
    return f"icons/{f}" if f else None
