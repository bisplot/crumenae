"""API providers: the services that read blockchains (Etherscan, Alchemy, Helius...).

Built-in providers come with the app; you can add your own from Settings → API
providers (any service that speaks one of the supported *types*), give them a key,
limits and a Test, and point networks at them. No code is needed unless a provider
speaks a protocol none of the types below understands (see docs/ADDING_AN_ADAPTER.md).

A provider's API key is the secret "<provider id>_api_key" (OS keychain or
data/secrets.json). Its call limits are the rate-limit entry with the same id.
"""
from __future__ import annotations

import re
import urllib.parse
from typing import Any, Dict, List, Optional

# type -> how networks served by it are read. "family" decides which networks can use it;
# "adapter" is the code that talks to it; "custom" = can be added from the UI.
TYPES: Dict[str, Dict[str, Any]] = {
    "etherscan": {"label": "Etherscan-compatible explorer API", "family": "evm", "adapter": "evm", "custom": True,
                  "default_url": "https://api.etherscan.io/v2/api",
                  "help": "Etherscan V2 (one key, chain chosen by chain id), Blockscout, Routescan, Lineascan, "
                          "Monadscan… Full history: transactions, internal transfers, tokens."},
    "alchemy": {"label": "Alchemy-compatible (JSON-RPC + Transfers API)", "family": "evm", "adapter": "alchemy",
                "custom": True, "default_url": "https://{network}.g.alchemy.com/v2",
                "help": "{network} is replaced by each network's Alchemy name (e.g. bnb-mainnet). History of "
                        "native and token transfers, every token held now."},
    "moralis": {"label": "Moralis Web3 Data API", "family": "evm", "adapter": "moralis", "custom": True,
                "default_url": "https://deep-index.moralis.io/api/v2.2",
                "help": "Each network gives its Moralis chain (hex chain id)."},
    "esplora": {"label": "Esplora (Bitcoin)", "family": "bitcoin", "adapter": "bitcoin", "custom": True,
                "default_url": "https://mempool.space/api", "help": "mempool.space, Blockstream or your own node."},
    "horizon": {"label": "Stellar Horizon", "family": "stellar", "adapter": "stellar", "custom": True,
                "default_url": "https://horizon.stellar.org", "help": "The public Horizon server or any other."},
    "solana": {"label": "Solana RPC", "family": "solana", "adapter": "solana", "custom": False, "default_url": ""},
    "trongrid": {"label": "TronGrid", "family": "tron", "adapter": "tron", "custom": False, "default_url": ""},
    "ark": {"label": "ARK/Solar API", "family": "solar", "adapter": "solar", "custom": False, "default_url": ""},
}

ADAPTER_FAMILY = {"evm": "evm", "alchemy": "evm", "moralis": "evm", "bitcoin": "bitcoin", "stellar": "stellar",
                  "solana": "solana", "tron": "tron", "solar": "solar"}

# Built-in providers. Their key names are the historical secret names.
BUILTIN: List[Dict[str, Any]] = [
    {"id": "etherscan", "name": "Etherscan", "type": "etherscan", "base_url": "https://api.etherscan.io/v2/api",
     "needs_key": True, "chainid_param": True, "link": "https://etherscan.io/myapikey",
     "hint": "One key for Ethereum, Polygon, Arbitrum, Linea… Free: 3 calls/s, 100,000/day. BNB Smart Chain, "
             "Base, OP, Avalanche and Arc need a paid plan: use Alchemy for those."},
    {"id": "alchemy", "name": "Alchemy", "type": "alchemy", "base_url": "https://{network}.g.alchemy.com/v2",
     "needs_key": True, "link": "https://dashboard.alchemy.com/signup",
     "hint": "Free plan, sign-up with e-mail or Google. One key for BNB Smart Chain, Monad, Arc… (enable each "
             "chain on your Alchemy app)."},
    {"id": "helius", "name": "Solana (Helius)", "type": "solana", "base_url": "https://mainnet.helius-rpc.com",
     "needs_key": False, "link": "https://dashboard.helius.dev/",
     "hint": "Free plan: 10 calls/s, monthly credits. Without a key the slow public Solana endpoint is used."},
    {"id": "trongrid", "name": "Tron (TronGrid)", "type": "trongrid", "base_url": "https://api.trongrid.io",
     "needs_key": False, "link": "https://www.trongrid.io/",
     "hint": "Free key: about 15 calls/s, 100,000/day. Without a key TronGrid may slow down or refuse calls."},
    {"id": "esplora", "name": "mempool.space (Bitcoin)", "type": "esplora", "base_url": "https://mempool.space/api",
     "needs_key": False, "link": "", "hint": "Public, no key."},
    {"id": "horizon", "name": "Stellar Horizon (public)", "type": "horizon", "base_url": "https://horizon.stellar.org",
     "needs_key": False, "link": "", "hint": "Public Stellar API run by the Stellar Development Foundation, no key."},
    {"id": "moralis", "name": "Moralis", "type": "moralis", "base_url": "https://deep-index.moralis.io/api/v2.2",
     "needs_key": True, "link": "https://admin.moralis.com/", "hint": "Paid plans. Used for Ronin."},
    {"id": "solar", "name": "Solar API (SXP)", "type": "ark", "base_url": "https://sxp.mainnet.sh/api",
     "needs_key": False, "link": "", "hint": "Public, no key."},
]
BUILTIN_IDS = {p["id"] for p in BUILTIN}

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
RESERVED_IDS = BUILTIN_IDS | {"solana_public", "binance_api", "binance_data", "coingecko", "coinmarketcap", "ecb",
                              "other", "solana", "rpc_url", "wallet"}


def secret_name(provider_id: str) -> str:
    return f"{provider_id}_api_key"


def custom(settings) -> List[Dict[str, Any]]:
    return [dict(p, builtin=False) for p in (settings.get("providers") or []) if isinstance(p, dict)]


def all_providers(settings) -> List[Dict[str, Any]]:
    return [dict(p, builtin=True) for p in BUILTIN] + custom(settings)


def get(settings, provider_id: str) -> Optional[Dict[str, Any]]:
    for p in all_providers(settings):
        if p["id"] == provider_id:
            return p
    return None


def hosts(provider: Dict[str, Any]) -> List[str]:
    """Hosts a provider's calls go to (for its call limits). "{network}" in the host is a wildcard."""
    url = str(provider.get("base_url") or "").replace("{network}", "network-placeholder")
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if "network-placeholder" in host:
        return ["*." + host.split(".", 1)[1]] if "." in host else []
    return [host] if host else []


def slug(name: str, taken: set) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:24] or "provider"
    if not base[0].isalpha():
        base = "p_" + base
    pid, n = base, 2
    while pid in taken or pid in RESERVED_IDS:
        pid = f"{base}_{n}"
        n += 1
    return pid


def clean(data: Dict[str, Any], existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Validate a custom provider from the UI."""
    ptype = str(data.get("type") or (existing or {}).get("type") or "")
    if ptype not in TYPES or not TYPES[ptype]["custom"]:
        raise ValueError("choose a provider type: " + ", ".join(k for k, t in TYPES.items() if t["custom"]))
    name = str(data.get("name") or (existing or {}).get("name") or "").strip()[:60]
    if not name:
        raise ValueError("give the provider a name")
    url = str(data.get("base_url") or (existing or {}).get("base_url") or TYPES[ptype]["default_url"]).strip()
    if not url.startswith(("https://", "http://")):
        raise ValueError("the base URL must start with https://")
    if ptype == "alchemy" and "{network}" not in url:
        raise ValueError("an Alchemy-compatible URL contains {network}, e.g. https://{network}.g.alchemy.com/v2")
    out = {"name": name, "type": ptype, "base_url": url.rstrip("/"),
           "needs_key": bool(data.get("needs_key", (existing or {}).get("needs_key", ptype != "esplora"))),
           "hint": str(data.get("hint", (existing or {}).get("hint") or ""))[:200],
           "link": str(data.get("link", (existing or {}).get("link") or ""))[:200]}
    if ptype == "etherscan":
        out["chainid_param"] = bool(data.get("chainid_param", (existing or {}).get("chainid_param",
                                                                                   "/v2/" in url)))
    return out
