"""Application context: everything a module needs, created once per data directory."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .db import Database
from .http import HttpClient
from .networks import NetworkRegistry, load_json
from .paths import default_data_dir
from .secrets import SecretStore
from .settings import Settings
from .timeutil import FiscalCalendar
from .util import now


class Context:
    def __init__(self, data_dir: Optional[Path] = None, http: Any = None,
                 secrets_backend: str = "auto", offline: bool = False):
        self.data_dir = Path(data_dir or default_data_dir())
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = Database(str(self.data_dir / "cryptoledger.db"))
        self.settings = Settings(self.db)
        self.secrets = SecretStore(self.data_dir, backend=secrets_backend)
        self.networks = NetworkRegistry(self.db, self.settings)
        self.http = http or HttpClient()
        # offline: never call external APIs (demo mode, tests).
        self.offline = offline or self.db.get_meta("demo") == "1"
        self.tokens = load_json("tokens.json")
        symbols = load_json("symbols.json")
        self.fiat = set(symbols.get("fiat", []))
        self.symbol_coingecko: Dict[str, str] = symbols.get("coingecko", {})

    @property
    def calendar(self) -> FiscalCalendar:
        return FiscalCalendar(self.settings.get("timezone"), self.settings.get("fiscal_year_end"))

    def wallets(self, active_only: bool = False, portfolio: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM wallets WHERE 1=1"
        params: List[Any] = []
        if active_only:
            sql += " AND active=1"
        if portfolio:
            sql += " AND portfolio=?"
            params.append(portfolio)
        return self.db.query(sql + " ORDER BY portfolio, COALESCE(rw_slot, 9999), label", params)

    def wallet(self, wallet_id: int) -> Dict[str, Any]:
        w = self.db.query_one("SELECT * FROM wallets WHERE id=?", (wallet_id,))
        if not w:
            raise KeyError(f"wallet {wallet_id} not found")
        return w

    def log(self, message: str, level: str = "info", wallet_id: Optional[int] = None) -> None:
        self.db.execute("INSERT INTO sync_log(ts, wallet_id, level, message) VALUES(?,?,?,?)",
                        (now(), wallet_id, level, message))

    def sync_state(self, wallet_id: int) -> Dict[str, Any]:
        raw = self.db.scalar("SELECT state FROM sync_state WHERE wallet_id=?", (wallet_id,))
        return json.loads(raw) if raw else {}

    def save_sync_state(self, wallet_id: int, state: Dict[str, Any]) -> None:
        self.db.execute("INSERT INTO sync_state(wallet_id, state) VALUES(?, ?) "
                        "ON CONFLICT(wallet_id) DO UPDATE SET state=excluded.state",
                        (wallet_id, json.dumps(state)))


Logger = Callable[[str], None]
