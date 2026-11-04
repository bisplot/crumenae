"""API keys and secrets.

Lookup order for a secret named e.g. "etherscan_api_key":
  1. environment variable CRYPTOLEDGER_ETHERSCAN_API_KEY
  2. the OS keychain (macOS Keychain, Windows Credential Locker, Secret Service),
     if the optional `keyring` package is installed
  3. data/secrets.json (file permissions 600), only when no keychain is available

Secrets are never sent to the browser: the UI only learns whether one is set.
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Dict, Optional

SERVICE = "cryptoledger"

# Known global secrets, shown on the Settings page.
KNOWN = {
    "etherscan_api_key": "Etherscan API key (one key covers all Etherscan V2 chains)",
    "coingecko_api_key": "CoinGecko API key (Demo or Pro)",
    "coinmarketcap_api_key": "CoinMarketCap API key",
    "trongrid_api_key": "TronGrid API key",
    "helius_api_key": "Helius API key (Solana)",
    "moralis_api_key": "Moralis API key (Ronin)",
    "solana_rpc_url": "Solana RPC URL (advanced: only if you use another provider than Helius)",
}


def env_name(name: str) -> str:
    return "CRYPTOLEDGER_" + re.sub(r"[^A-Z0-9]", "_", name.upper())


class SecretStore:
    def __init__(self, data_dir: Path, backend: str = "auto"):
        self.file = Path(data_dir) / "secrets.json"
        self._lock = threading.Lock()
        self._keyring = None
        if backend in ("auto", "keyring"):
            self._keyring = _load_keyring()
        if backend == "keyring" and self._keyring is None:
            raise RuntimeError("keyring backend requested but not available (pip install keyring)")

    @property
    def backend(self) -> str:
        return "keychain" if self._keyring is not None else "file"

    def get(self, name: str) -> Optional[str]:
        env = os.environ.get(env_name(name))
        if env:
            return env
        if self._keyring is not None:
            try:
                value = self._keyring.get_password(SERVICE, name)
                if value:
                    return value
            except Exception:
                pass
        return self._read_file().get(name) or None

    def source(self, name: str) -> Optional[str]:
        if os.environ.get(env_name(name)):
            return "env"
        if self._keyring is not None:
            try:
                if self._keyring.get_password(SERVICE, name):
                    return "keychain"
            except Exception:
                pass
        if self._read_file().get(name):
            return "file"
        return None

    def set(self, name: str, value: Optional[str]) -> None:
        if not value:
            self.delete(name)
            return
        if self._keyring is not None:
            self._keyring.set_password(SERVICE, name, value)
            return
        with self._lock:
            data = self._read_file()
            data[name] = value
            self._write_file(data)

    def delete(self, name: str) -> None:
        if self._keyring is not None:
            try:
                self._keyring.delete_password(SERVICE, name)
            except Exception:
                pass
        with self._lock:
            data = self._read_file()
            if name in data:
                del data[name]
                self._write_file(data)

    def _read_file(self) -> Dict[str, str]:
        try:
            return json.loads(self.file.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def _write_file(self, data: Dict[str, str]) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(str(tmp), str(self.file))
        try:
            os.chmod(str(self.file), 0o600)
        except OSError:
            pass


def _load_keyring():
    try:
        import keyring  # type: ignore
        from keyring.backends import fail  # type: ignore
    except Exception:
        return None
    try:
        kr = keyring.get_keyring()
        if isinstance(kr, fail.Keyring) or getattr(kr, "priority", 1) <= 0:
            return None
        return keyring
    except Exception:
        return None
