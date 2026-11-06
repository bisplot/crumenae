"""Network registry: built-in config/networks.json + networks added from the UI
+ per-network overrides from Settings (provider, Alchemy name, URLs, hidden).

Each network names the API provider that reads it ("provider"); the provider's type
decides the adapter and its URL/key (see providers.py)."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from . import providers as providers_mod
from .paths import CONFIG_DIR

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
OVERRIDABLE = ("explorer_api", "explorer_key", "rpc_url", "chainid_param")
# Settings a built-in network accepts from the Networks table.
EDITABLE = ("provider", "alchemy_network", "hidden") + OVERRIDABLE


def apply_provider(cfg: Dict[str, Any], provider: Optional[Dict[str, Any]]) -> None:
    """Point a network config at its provider: adapter, base URL, key name."""
    if not provider:
        return
    t = providers_mod.TYPES.get(provider["type"]) or {}
    family = providers_mod.ADAPTER_FAMILY.get(cfg.get("adapter") or "", cfg.get("family"))
    if t.get("family") != family:
        return   # incompatible (e.g. a Bitcoin provider on an EVM chain): keep the network as it is
    if not t.get("custom"):
        return   # Solana, Tron, Solar: their adapters know their endpoints
    cfg["adapter"] = t["adapter"]
    cfg["explorer_api"] = provider["base_url"]
    if provider["type"] in ("etherscan", "alchemy"):
        cfg["explorer_key"] = provider["id"]
    if provider["type"] == "etherscan":
        cfg["chainid_param"] = bool(provider.get("chainid_param"))


class Network:
    def __init__(self, network_id: str, cfg: Dict[str, Any], builtin: bool = True):
        self.id = network_id
        self.cfg = cfg
        self.builtin = builtin

    def __getattr__(self, item: str) -> Any:
        if item.startswith("__"):
            raise AttributeError(item)
        return self.cfg.get(item)

    @property
    def family(self) -> str:
        return providers_mod.ADAPTER_FAMILY.get(self.adapter or "", self.cfg.get("family") or "")

    @property
    def kind(self) -> str:
        return {"binance": "exchange", "manual": "manual"}.get(self.adapter, "chain")

    @property
    def native_key(self) -> Optional[str]:
        return f"{self.id}:native" if self.cfg.get("native") else None

    def public(self) -> Dict[str, Any]:
        out = {k: v for k, v in self.cfg.items() if not k.startswith("_")}
        out.update(id=self.id, kind=self.kind, builtin=self.builtin, family=self.family)
        return out


def load_json(name: str) -> Dict[str, Any]:
    with open(CONFIG_DIR / name, encoding="utf-8") as fh:
        data = json.load(fh)
    return {k: v for k, v in data.items() if not k.startswith("_")}


class NetworkRegistry:
    def __init__(self, db, settings):
        self.db = db
        self.settings = settings
        self._builtin = load_json("networks.json")

    def all(self) -> List[Network]:
        nets: Dict[str, Network] = {}
        for nid, cfg in self._builtin.items():
            nets[nid] = Network(nid, json.loads(json.dumps(cfg)), builtin=True)
        for row in self.db.query("SELECT id, config FROM custom_networks ORDER BY id"):
            nets[row["id"]] = Network(row["id"], json.loads(row["config"]), builtin=False)
        overrides = self.settings.get("network_overrides") or {}
        provs = {p["id"]: p for p in providers_mod.all_providers(self.settings)}
        for nid, net in nets.items():
            ov = overrides.get(nid) or {}
            for k in ("provider", "alchemy_network", "hidden"):
                if k in ov and ov[k] not in (None, ""):
                    net.cfg[k] = ov[k]
            apply_provider(net.cfg, provs.get(net.cfg.get("provider")))
            for k in OVERRIDABLE:
                if k in ov and ov[k] not in (None, ""):
                    net.cfg[k] = ov[k]
        return list(nets.values())

    def get(self, network_id: str) -> Network:
        for n in self.all():
            if n.id == network_id:
                return n
        raise KeyError(f"unknown network '{network_id}'")

    def add_custom(self, data: Dict[str, Any], update: bool = False) -> Network:
        """Add (or edit) a network served by any provider: an EVM chain (Etherscan- or
        Alchemy-compatible provider), a Cosmos SDK chain (LCD), a Bitcoin-like chain (Esplora)
        or Stellar (Horizon). No code needed."""
        nid = str(data.get("id", "")).strip().lower()
        if not _ID_RE.match(nid):
            raise ValueError("id: 2-32 chars, lower-case letters, digits, '-' or '_'")
        if nid in self._builtin:
            raise ValueError(f"'{nid}' is a built-in network: change it in the Networks table instead")
        exists = self.db.scalar("SELECT 1 FROM custom_networks WHERE id=?", (nid,))
        if exists and not update:
            raise ValueError(f"a network '{nid}' already exists")
        provider_id = str(data.get("provider") or "").strip()
        if provider_id:
            return self._save_custom(nid, data, provider_id)
        try:
            chain_id = int(data.get("chain_id"))
        except (TypeError, ValueError):
            raise ValueError("chain_id must be an integer")
        symbol = str(data.get("native_symbol", "")).strip().upper()
        if not symbol:
            raise ValueError("native_symbol is required")
        explorer_api = str(data.get("explorer_api", "")).strip()
        rpc_url = str(data.get("rpc_url", "")).strip()
        if not explorer_api and not rpc_url:
            raise ValueError("give at least an explorer API URL or an RPC URL")
        for url in (explorer_api, rpc_url):
            if url and not url.startswith(("https://", "http://")):
                raise ValueError("URLs must start with https://")
        cfg = {
            "name": str(data.get("name") or nid),
            "adapter": "evm",
            "chain_id": chain_id,
            "explorer_api": explorer_api,
            "explorer_key": str(data.get("explorer_key", "")).strip(),
            "chainid_param": bool(data.get("chainid_param", "etherscan.io/v2" in explorer_api)),
            "tx_url": str(data.get("tx_url", "")).strip(),
            "address_url": str(data.get("address_url", "")).strip(),
            "rpc_url": rpc_url,
            "coingecko_platform": str(data.get("coingecko_platform", "")).strip(),
            "native": {
                "symbol": symbol,
                "name": str(data.get("native_name") or symbol),
                "decimals": int(data.get("native_decimals") or 18),
                "coingecko": str(data.get("native_coingecko", "")).strip() or None,
                "binance": str(data.get("native_binance", "")).strip().upper() or None,
            },
        }
        self.db.execute(
            "INSERT INTO custom_networks(id, config) VALUES(?, ?) "
            "ON CONFLICT(id) DO UPDATE SET config=excluded.config",
            (nid, json.dumps(cfg)),
        )
        return Network(nid, cfg, builtin=False)

    add_custom_evm = add_custom   # name used by version 0.1

    def _save_custom(self, nid: str, data: Dict[str, Any], provider_id: str) -> Network:
        provider = providers_mod.get(self.settings, provider_id)
        if not provider:
            raise ValueError(f"unknown provider '{provider_id}'")
        ptype = providers_mod.TYPES[provider["type"]]
        if not ptype["custom"]:
            raise ValueError(f"{provider['name']} serves only its own network")
        family = ptype["family"]
        symbol = str(data.get("native_symbol", "")).strip().upper()
        if not symbol:
            raise ValueError("native coin symbol is required")
        cfg: Dict[str, Any] = {
            "name": str(data.get("name") or nid).strip()[:60],
            "adapter": ptype["adapter"],
            "family": family,
            "provider": provider_id,
            "tx_url": str(data.get("tx_url", "")).strip(),
            "address_url": str(data.get("address_url", "")).strip(),
            "coingecko_platform": str(data.get("coingecko_platform", "")).strip(),
            "native": {
                "symbol": symbol,
                "name": str(data.get("native_name") or symbol).strip(),
                "decimals": int(data.get("native_decimals") or {"evm": 18, "bitcoin": 8, "stellar": 7, "cosmos": 6}[family]),
                "coingecko": str(data.get("native_coingecko", "")).strip() or None,
                "binance": str(data.get("native_binance", "")).strip().upper() or None,
            },
        }
        if family == "evm":
            try:
                cfg["chain_id"] = int(data.get("chain_id"))
            except (TypeError, ValueError):
                raise ValueError("chain id must be an integer (see chainlist.org)")
            if provider["type"] == "alchemy":
                cfg["alchemy_network"] = str(data.get("alchemy_network") or "").strip()
                if not re.match(r"^[a-z0-9-]{3,40}$", cfg["alchemy_network"]):
                    raise ValueError("Alchemy network name, e.g. monad-mainnet (shown in your Alchemy dashboard)")
            rpc = str(data.get("rpc_url", "")).strip()
            if rpc and not rpc.startswith(("https://", "http://")):
                raise ValueError("the archive RPC URL must start with https://")
            cfg["rpc_url"] = rpc
            cfg["l2"] = bool(data.get("l2"))
        if family == "cosmos":
            cfg["bech32_prefix"] = str(data.get("bech32_prefix") or "").strip().lower()
            cfg["native_denom"] = str(data.get("native_denom") or "").strip()
            if not re.match(r"^[a-z]{1,20}$", cfg["bech32_prefix"]):
                raise ValueError("address prefix, e.g. terra, cosmos, osmo")
            if not re.match(r"^[a-zA-Z][a-zA-Z0-9/:._-]{1,127}$", cfg["native_denom"]):
                raise ValueError("native denom, e.g. uluna, uatom, uosmo")
        for url in (cfg["tx_url"], cfg["address_url"]):
            if url and not url.startswith(("https://", "http://")):
                raise ValueError("explorer links must start with https://")
        self.db.execute(
            "INSERT INTO custom_networks(id, config) VALUES(?, ?) "
            "ON CONFLICT(id) DO UPDATE SET config=excluded.config",
            (nid, json.dumps(cfg)),
        )
        return self.get(nid)

    def delete_custom(self, network_id: str) -> None:
        used = self.db.scalar("SELECT COUNT(*) FROM wallets WHERE network=?", (network_id,))
        if used:
            raise ValueError("network is used by wallets; delete them first")
        self.db.execute("DELETE FROM custom_networks WHERE id=?", (network_id,))
