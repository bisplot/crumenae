"""Minimal JSON HTTP client on urllib: per-host throttling, retries, proxies from env."""
from __future__ import annotations

import json
import random
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from . import __version__

USER_AGENT = f"Crumenae/{__version__} (+https://github.com/bisplot/crumenae)"

# Minimum seconds between two calls to the same host (free-tier friendly).
HOST_INTERVALS = {
    "api.etherscan.io": 0.25,          # free: ~3-5 calls/s
    "api.coingecko.com": 2.2,          # demo/public: ~30 calls/min
    "pro-api.coingecko.com": 0.15,
    "pro-api.coinmarketcap.com": 2.1,  # basic: 30 calls/min
    "api.trongrid.io": 0.35,
    "api.mainnet-beta.solana.com": 0.25,
    "mempool.space": 0.3,
    "blockstream.info": 0.3,
    "data-api.binance.vision": 0.05,
    "api.binance.com": 0.1,
    "bnb-mainnet.g.alchemy.com": 0.1,
    "horizon.stellar.org": 0.3,
    "data-api.ecb.europa.eu": 0.2,
}
DEFAULT_INTERVAL = 0.2
RETRY_STATUS = {408, 418, 425, 429, 500, 502, 503, 504}


class HttpError(Exception):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} from {_redact(url)}: {body[:300]}")
        self.status = status
        self.body = body
        self.url = url


def _redact(url: str) -> str:
    """Hide API keys in error messages and logs."""
    parts = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    q = [(k, "***" if k.lower() in ("apikey", "api_key", "api-key", "key", "signature", "x_cg_demo_api_key") else v)
         for k, v in q]
    # Keys carried in the path (e.g. https://bnb-mainnet.g.alchemy.com/v2/<key>): long opaque segments.
    path = "/".join("***" if re.fullmatch(r"[A-Za-z0-9_-]{24,}", seg) else seg for seg in parts.path.split("/"))
    return urllib.parse.urlunsplit(parts._replace(path=path, query=urllib.parse.urlencode(q)))


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


class HttpClient:
    def __init__(self, timeout: float = 30.0, retries: int = 4):
        self.timeout = timeout
        self.retries = retries
        self._ctx = _ssl_context()
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler(),  # honours HTTPS_PROXY / NO_PROXY
            urllib.request.HTTPSHandler(context=self._ctx),
        )
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()
        # Set by the Context: per-service calls/second and calls/day (ratelimit.RateLimiter).
        self.limiter = None

    def _throttle(self, host: str) -> None:
        interval = HOST_INTERVALS.get(host, DEFAULT_INTERVAL)
        with self._lock:
            wait = self._last.get(host, 0.0) + interval - time.monotonic()
            self._last[host] = time.monotonic() + max(wait, 0.0)
        if wait > 0:
            time.sleep(wait)

    def request(self, method: str, url: str, params: Optional[Dict[str, Any]] = None,
                headers: Optional[Dict[str, str]] = None, body: Optional[bytes] = None,
                count: bool = True) -> bytes:
        """count=False: a call the provider does not charge (not counted against daily caps)."""
        if params:
            sep = "&" if "?" in url else "?"
            url = url + sep + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        hdrs.update(headers or {})
        host = urllib.parse.urlsplit(url).hostname or ""
        last_error: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            if self.limiter is not None:
                self.limiter.acquire(host, url, count=count)  # may raise DailyLimitReached: never retried
            else:
                self._throttle(host)
            req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
            try:
                with self._opener.open(req, timeout=self.timeout) as resp:
                    return resp.read()
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")
                err = HttpError(e.code, text, url)
                if e.code not in RETRY_STATUS or attempt == self.retries:
                    raise err
                last_error = err
                delay = _retry_after(e.headers.get("Retry-After")) or (2 ** attempt + random.random())
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last_error = e
                if attempt == self.retries:
                    break
                delay = 2 ** attempt + random.random()
            time.sleep(min(delay, 60))
        if isinstance(last_error, HttpError):
            raise last_error
        raise HttpError(0, f"network error: {last_error}", url)

    def get_json(self, url: str, params: Optional[Dict[str, Any]] = None,
                 headers: Optional[Dict[str, str]] = None, count: bool = True) -> Any:
        return json.loads(self.request("GET", url, params, headers, count=count).decode("utf-8"))

    def post_json(self, url: str, payload: Any, headers: Optional[Dict[str, str]] = None,
                  params: Optional[Dict[str, Any]] = None) -> Any:
        hdrs = {"Content-Type": "application/json"}
        hdrs.update(headers or {})
        raw = self.request("POST", url, params, hdrs, json.dumps(payload).encode("utf-8"))
        return json.loads(raw.decode("utf-8"))

    def get_text(self, url: str, params: Optional[Dict[str, Any]] = None,
                 headers: Optional[Dict[str, str]] = None, count: bool = True) -> str:
        return self.request("GET", url, params, headers, count=count).decode("utf-8")


def _retry_after(value: Optional[str]) -> Optional[float]:
    try:
        return float(value) if value else None
    except ValueError:
        return None
