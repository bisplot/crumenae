"""Test helpers: a fake HTTP client and a throw-away context.

Fixtures reproduce the documented response shapes of each public API
(Etherscan, Solana JSON-RPC, TronGrid, Esplora, Binance, CoinGecko, ECB).
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from cryptoledger.context import Context
from cryptoledger.http import HttpError

FIXTURES = Path(__file__).parent / "fixtures"

Handler = Callable[[str, str, Dict[str, Any], Any], Any]


def fixture(name: str) -> Any:
    with open(FIXTURES / name, encoding="utf-8") as fh:
        return json.load(fh)


class FakeHttp:
    """Routes requests to handlers: handler(method, url, params, payload) -> JSON object.
    A handler returns None to pass the request on to the next one."""

    def __init__(self) -> None:
        self.handlers: List[Tuple[str, Handler]] = []
        self.calls: List[Tuple[str, str, Dict[str, Any], Any]] = []

    def on(self, url_prefix: str, handler: Handler) -> "FakeHttp":
        self.handlers.append((url_prefix, handler))
        return self

    def _dispatch(self, method: str, url: str, params: Optional[Dict[str, Any]], payload: Any) -> Any:
        parts = urllib.parse.urlsplit(url)
        merged = {k: v[-1] for k, v in urllib.parse.parse_qs(parts.query).items()}
        merged.update({k: v for k, v in (params or {}).items()})
        base = urllib.parse.urlunsplit(parts._replace(query=""))
        self.calls.append((method, base, merged, payload))
        for prefix, handler in self.handlers:
            if base.startswith(prefix):
                result = handler(method, base, merged, payload)
                if result is not None:
                    return result
        raise HttpError(404, f"no fake for {method} {base} {merged}", url)

    def get_json(self, url, params=None, headers=None, count=True):
        return self._dispatch("GET", url, params, None)

    def post_json(self, url, payload, headers=None, params=None):
        return self._dispatch("POST", url, params, payload)

    def get_text(self, url, params=None, headers=None, count=True):
        return self._dispatch("GET", url, params, None)

    def request(self, method, url, params=None, headers=None, body=None, count=True):
        return json.dumps(self._dispatch(method, url, params, body)).encode("utf-8")


class TempContext:
    """Context in a temporary folder, secrets in a file (never the real keychain)."""

    def __init__(self, http: Optional[FakeHttp] = None, offline: bool = False):
        self.dir = tempfile.mkdtemp(prefix="cryptoledger-test-")
        self.http = http or FakeHttp()
        self.ctx = Context(Path(self.dir), http=self.http, secrets_backend="file", offline=offline)

    def close(self) -> None:
        self.ctx.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)


def add_wallet(ctx: Context, network: str, address: str = "", label: str = "W", synced: bool = True,
               **extra: Any) -> Dict[str, Any]:
    cur = ctx.db.execute(
        "INSERT INTO wallets(label, network, address, portfolio, platform, rw_slot, bollo_applied, created_at,"
        " last_sync_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (label, network, address, extra.get("portfolio", "Personal"), extra.get("platform", ""),
         extra.get("rw_slot"), 1 if extra.get("bollo") else 0, 0, 1 if synced else None))
    return ctx.wallet(cur.lastrowid)


os.environ.setdefault("CRYPTOLEDGER_TEST", "1")
