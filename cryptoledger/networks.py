"""Network registry: built-in config/networks.json + networks added from the UI
+ per-network overrides (explorer URL, RPC URL) from Settings."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from .paths import CONFIG_DIR

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
OVERRIDABLE = ("explorer_api", "explorer_key", "rpc_url", "chainid_param")


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
    def kind(self) -> str:
        return {"binance": "exchange", "manual": "manual"}.get(self.adapter, "chain")

    @property
    def native_key(self) -> Optional[str]:
        return f"{self.id}:native" if self.cfg.get("native") else None

    def public(self) -> Dict[str, Any]:
        out = {k: v for k, v in self.cfg.items() if not k.startswith("_")}
        out.update(id=self.id, kind=self.kind, builtin=self.builtin)
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
            nets[nid] = Network(nid, dict(cfg), builtin=True)
        for row in self.db.query("SELECT id, config FROM custom_networks ORDER BY id"):
            nets[row["id"]] = Network(row["id"], json.loads(row["config"]), builtin=False)
        overrides = self.settings.get("network_overrides") or {}
        for nid, ov in overrides.items():
            if nid in nets:
                for k in OVERRIDABLE:
                    if k in ov and ov[k] is not None:
                        nets[nid].cfg[k] = ov[k]
        return list(nets.values())

    def get(self, network_id: str) -> Network:
        for n in self.all():
            if n.id == network_id:
                return n
        raise KeyError(f"unknown network '{network_id}'")

    def add_custom_evm(self, data: Dict[str, Any]) -> Network:
        """Add an EVM chain served by an Etherscan-compatible explorer API (Etherscan V2,
        Blockscout, Routescan...). No code needed."""
        nid = str(data.get("id", "")).strip().lower()
        if not _ID_RE.match(nid):
            raise ValueError("id: 2-32 chars, lower-case letters, digits, '-' or '_'")
        if nid in self._builtin:
            raise ValueError(f"'{nid}' is a built-in network; override its URLs in Settings instead")
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

    def delete_custom(self, network_id: str) -> None:
        used = self.db.scalar("SELECT COUNT(*) FROM wallets WHERE network=?", (network_id,))
        if used:
            raise ValueError("network is used by wallets; delete them first")
        self.db.execute("DELETE FROM custom_networks WHERE id=?", (network_id,))
