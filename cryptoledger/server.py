"""Local web server: JSON API + the browser UI. Listens on 127.0.0.1 only by default.

Protection against other websites talking to this local server: requests must
carry a localhost Host header (DNS rebinding), and every state-changing request
must send the custom header X-CryptoLedger (forces a CORS preflight, which this
server never approves).
"""
from __future__ import annotations

import json
import mimetypes
import re
import secrets as _random
import shutil
import socketserver
import threading
import sys
import traceback
import urllib.parse
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import __version__
from . import assets as assets_mod
from . import classify, exports, report, sync, views, yearend
from . import providers as providers_mod
from .adapters import ADAPTERS, get_adapter
from .context import Context
from .paths import WEB_DIR
from .prices import PriceService
from .secrets import KNOWN as KNOWN_SECRETS
from .settings import DEFAULTS
from .timeutil import FiscalCalendar
from .util import D, dstr, dumps, now


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


Route = Tuple[str, "re.Pattern[str]", Callable[..., Any]]


class App:
    """All API handlers. Kept separate from HTTP plumbing so tests can call them."""

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.jobs = sync.JobRunner(ctx)
        self.routes: List[Route] = []
        r = self._route
        r("GET", r"/api/status", self.status)
        r("GET", r"/api/dashboard", self.dashboard)
        r("GET", r"/api/networks", self.networks)
        r("POST", r"/api/networks", self.add_network)
        r("PUT", r"/api/networks/(?P<nid>[a-z0-9_-]+)", self.update_network)
        r("DELETE", r"/api/networks/(?P<nid>[a-z0-9_-]+)", self.delete_network)
        r("GET", r"/api/providers", self.providers)
        r("POST", r"/api/providers", self.add_provider)
        r("PUT", r"/api/providers/(?P<pid>[a-z0-9_]+)", self.update_provider)
        r("DELETE", r"/api/providers/(?P<pid>[a-z0-9_]+)", self.delete_provider)
        r("GET", r"/api/wallets", self.wallets)
        r("POST", r"/api/wallets", self.create_wallets)
        r("PUT", r"/api/wallets/(?P<wid>\d+)", self.update_wallet)
        r("DELETE", r"/api/wallets/(?P<wid>\d+)", self.delete_wallet)
        r("GET", r"/api/settings", self.get_settings)
        r("PUT", r"/api/settings", self.put_settings)
        r("PUT", r"/api/secrets", self.put_secrets)
        r("POST", r"/api/jobs/(?P<kind>sync|snapshot|prices|recompute|assets)", self.start_job)
        r("GET", r"/api/jobs/current", self.current_job)
        r("GET", r"/api/years/(?P<year>\d{4})", self.year)
        r("GET", r"/api/report/(?P<year>\d{4})", self.report_json)
        r("GET", r"/api/report/(?P<year>\d{4})/(?P<name>[a-z_]+\.(?:csv|md))", self.report_file)
        r("POST", r"/api/report/(?P<year>\d{4})/write", self.report_write)
        r("GET", r"/api/exports", self.exports_list)
        r("GET", r"/api/exports/(?P<name>[a-z0-9_-]+\.csv)", self.export_file)
        r("GET", r"/api/transactions", self.transactions)
        r("PUT", r"/api/transactions/(?P<mid>\d+)", self.update_transaction)
        r("GET", r"/api/assets", self.assets)
        r("POST", r"/api/assets/clear", self.clear_assets)
        r("PUT", r"/api/assets/(?P<key>.+)", self.update_asset)
        r("GET", r"/api/manual/(?P<wid>\d+)", self.get_manual)
        r("PUT", r"/api/manual/(?P<wid>\d+)", self.put_manual)
        r("GET", r"/api/log", self.log)
        r("GET", r"/api/limits", self.limits)
        r("POST", r"/api/test/(?P<service>[a-z_]+)", self.test_api)
        r("GET", r"/api/reset/phrase", self.reset_phrase)
        r("POST", r"/api/reset", self.reset)
        self._reset_phrase: Optional[Tuple[str, float]] = None
        self._reset_lock = threading.Lock()

    def _route(self, method: str, pattern: str, fn: Callable[..., Any]) -> None:
        self.routes.append((method, re.compile("^" + pattern + "$"), fn))

    def dispatch(self, method: str, path: str, query: Dict[str, str], body: Any) -> Any:
        for m, rx, fn in self.routes:
            match = rx.match(path)
            if match and m == method:
                kwargs = {k: urllib.parse.unquote(v) for k, v in match.groupdict().items()}
                return fn(query=query, body=body, **kwargs)
        raise ApiError(404, f"no route {method} {path}")

    # --- status / settings ---------------------------------------------------------
    def status(self, **_: Any) -> Dict[str, Any]:
        cal = self.ctx.calendar
        last = cal.last_completed_year(now())
        return {
            "version": __version__, "python": sys.version.split()[0], "data_dir": str(self.ctx.data_dir),
            "demo": self.ctx.db.get_meta("demo") == "1", "offline": self.ctx.offline,
            "secrets_backend": self.ctx.secrets.backend, "timezone": cal.tz_name,
            "fiscal_year_end": self.ctx.settings.get("fiscal_year_end"), "last_completed_year": last,
            "next_cutoff": cal.cutoff(last + 1), "next_cutoff_local": cal.iso_local(cal.cutoff(last + 1)),
            "wallets": self.ctx.db.scalar("SELECT COUNT(*) FROM wallets"),
            "exports_dir": str(exports.exports_dir(self.ctx)),
            "yearend_stale": self.ctx.yearend_stale(),
            "yearend_computed_at": int(self.ctx.db.get_meta("yearend_computed_at") or 0) or None,
            "job": self.jobs.current.public() if self.jobs.current else None,
        }

    def get_settings(self, **_: Any) -> Dict[str, Any]:
        secrets = {name: {"label": label, "source": self.ctx.secrets.source(name)}
                   for name, label in KNOWN_SECRETS.items()}
        for prov in providers_mod.custom(self.ctx.settings):
            name = providers_mod.secret_name(prov["id"])
            secrets[name] = {"label": f"{prov['name']} API key", "source": self.ctx.secrets.source(name)}
        for n in self.ctx.networks.all():
            if n.adapter in ("evm", "alchemy"):
                name = f"rpc_url:{n.id}"
                secrets[name] = {"label": f"{n.name} archive RPC URL (optional)", "source": self.ctx.secrets.source(name)}
        return {"settings": self.ctx.settings.all(), "secrets": secrets, "secrets_backend": self.ctx.secrets.backend,
                "providers": ["binance", "coingecko", "coinmarketcap"]}

    def put_settings(self, body: Any, **_: Any) -> Dict[str, Any]:
        if not isinstance(body, dict):
            raise ApiError(400, "expected an object")
        s = self.ctx.settings
        affects_yearend = []
        for key, value in body.items():
            if key not in DEFAULTS:
                raise ApiError(400, f"unknown setting '{key}'")
            if key in ("timezone", "fiscal_year_end"):
                tz = value if key == "timezone" else s.get("timezone")
                fy = value if key == "fiscal_year_end" else s.get("fiscal_year_end")
                try:
                    FiscalCalendar(str(tz), str(fy))
                except ValueError as e:
                    raise ApiError(400, str(e))
            elif key in ("historical_price_providers", "current_price_providers"):
                if not isinstance(value, list) or any(v not in ("binance", "coingecko", "coinmarketcap") for v in value):
                    raise ApiError(400, f"{key}: list of binance / coingecko / coinmarketcap")
            elif key == "usd_to_eur" and value not in ("ecb", "binance"):
                raise ApiError(400, "usd_to_eur must be 'ecb' or 'binance'")
            elif key == "coingecko_plan" and value not in ("demo", "pro"):
                raise ApiError(400, "coingecko_plan must be 'demo' or 'pro'")
            elif key == "first_fiscal_year":
                if value in ("", None):
                    value = None
                elif not str(value).isdigit() or not 2009 <= int(value) <= 2100:
                    raise ApiError(400, "first_fiscal_year must be a year")
                else:
                    value = int(value)
            elif key == "ic_rate":
                try:
                    if not Decimal(str(value)) >= 0:
                        raise InvalidOperation
                except InvalidOperation:
                    raise ApiError(400, "ic_rate must be a number")
                value = str(value)
            elif key == "snapshot_tolerance_seconds":
                if not str(value).isdigit():
                    raise ApiError(400, "tolerance must be seconds")
                value = int(value)
            elif key in ("value_transactions", "auto_snapshot"):
                value = bool(value)
            elif key == "rate_limits":
                value = _clean_rate_limits(value, set(self.ctx.limiter.services()))
            elif key == "platforms":
                if not isinstance(value, list):
                    raise ApiError(400, "platforms must be a list")
                value = _merge_platforms([], value)
            elif key == "network_overrides":
                if not isinstance(value, dict):
                    raise ApiError(400, "network_overrides must be an object")
                for nid, ov in value.items():
                    if not isinstance(ov, dict):
                        raise ApiError(400, "each override must be an object")
                    for k, v in ov.items():
                        if k not in ("explorer_api", "explorer_key", "rpc_url", "chainid_param"):
                            raise ApiError(400, f"cannot override '{k}'")
                        if k in ("explorer_api", "rpc_url") and v and not str(v).startswith(("https://", "http://")):
                            raise ApiError(400, f"{nid}.{k} must be a URL")
            if key in YEAREND_SETTINGS and value != s.get(key):
                affects_yearend.append(key.replace("_", " "))
            s.set(key, value)
        if affects_yearend:
            self.ctx.mark_yearend_stale("settings changed: " + ", ".join(affects_yearend))
        return self.get_settings()

    def put_secrets(self, body: Any, **_: Any) -> Dict[str, Any]:
        if not isinstance(body, dict):
            raise ApiError(400, "expected an object")
        network_ids = {n.id for n in self.ctx.networks.all()}
        for name, value in body.items():
            custom_keys = {providers_mod.secret_name(p["id"]) for p in providers_mod.custom(self.ctx.settings)}
            ok = name in KNOWN_SECRETS or name in custom_keys or \
                (name.startswith("rpc_url:") and name.split(":", 1)[1] in network_ids)
            if not ok:
                raise ApiError(400, f"unknown secret '{name}'")
            value = (value or "").strip()
            if value and name.endswith("url") and not value.startswith(("https://", "http://")):
                raise ApiError(400, f"{name} must be a URL")
            self.ctx.secrets.set(name, value or None)
        return self.get_settings()

    # --- networks ------------------------------------------------------------------
    def networks(self, **_: Any) -> List[Dict[str, Any]]:
        out = []
        provs = {p["id"]: p for p in providers_mod.all_providers(self.ctx.settings)}
        used = {r["network"]: r["n"] for r in self.ctx.db.query("SELECT network, COUNT(*) AS n FROM wallets GROUP BY network")}
        for n in self.ctx.networks.all():
            p = n.public()
            cls = ADAPTERS.get(n.adapter)
            p["needs_address"] = bool(cls and cls.needs_address)
            p["credentials"] = list(cls.credentials) if cls else []
            p["provider_name"] = (provs.get(n.provider) or {}).get("name") or ""
            p["provider_type"] = (provs.get(n.provider) or {}).get("type") or ""
            p["wallets"] = used.get(n.id, 0)
            p["hidden"] = bool(n.hidden)
            out.append(p)
        return out

    def add_network(self, body: Any, **_: Any) -> Dict[str, Any]:
        try:
            return self.ctx.networks.add_custom(body or {}).public()
        except ValueError as e:
            raise ApiError(400, str(e))

    def update_network(self, nid: str, body: Any, **_: Any) -> Dict[str, Any]:
        """Built-in network: provider, Alchemy name, hidden, URLs (stored as overrides).
        Network you added: every field."""
        body = body if isinstance(body, dict) else {}
        try:
            net = self.ctx.networks.get(nid)
        except KeyError:
            raise ApiError(404, "unknown network")
        if body.get("provider"):
            prov = providers_mod.get(self.ctx.settings, str(body["provider"]))
            if not prov:
                raise ApiError(400, "unknown provider")
            if providers_mod.TYPES[prov["type"]]["family"] != net.family:
                raise ApiError(400, f"{prov['name']} cannot read {net.name}")
            if prov["type"] == "alchemy" and not (body.get("alchemy_network") or net.alchemy_network):
                raise ApiError(400, f"give the Alchemy name of {net.name} (e.g. eth-mainnet, arb-mainnet)")
        if not net.builtin:
            data = dict(json.loads(self.ctx.db.scalar("SELECT config FROM custom_networks WHERE id=?", (nid,)) or "{}"))
            flat = {"id": nid, "name": data.get("name"), "provider": data.get("provider"),
                    "chain_id": data.get("chain_id"), "alchemy_network": data.get("alchemy_network"),
                    "bech32_prefix": data.get("bech32_prefix"), "native_denom": data.get("native_denom"),
                    "tx_url": data.get("tx_url"),
                    "address_url": data.get("address_url"), "coingecko_platform": data.get("coingecko_platform"),
                    "rpc_url": data.get("rpc_url"), "l2": data.get("l2")}
            for k, v in (data.get("native") or {}).items():
                flat[f"native_{k}"] = v
            flat["native_coingecko"] = (data.get("native") or {}).get("coingecko")
            flat["native_binance"] = (data.get("native") or {}).get("binance")
            flat.update({k: v for k, v in body.items() if k != "id"})
            if not flat.get("provider"):
                raise ApiError(400, "choose a provider")
            hidden = bool(flat.pop("hidden", data.get("hidden")))
            try:
                self.ctx.networks.add_custom(flat, update=True)
            except ValueError as e:
                raise ApiError(400, str(e))
            if hidden != bool(data.get("hidden")):
                ov = dict(self.ctx.settings.get("network_overrides") or {})
                ov[nid] = dict(ov.get(nid) or {}, hidden=hidden)
                self.ctx.settings.set("network_overrides", ov)
            return self.ctx.networks.get(nid).public()
        from .networks import EDITABLE
        overrides = dict(self.ctx.settings.get("network_overrides") or {})
        ov = dict(overrides.get(nid) or {})
        for k, v in body.items():
            if k not in EDITABLE:
                raise ApiError(400, f"'{k}' cannot be changed on a built-in network")
            if k in ("explorer_api", "rpc_url") and v and not str(v).startswith(("https://", "http://")):
                raise ApiError(400, f"{k} must be a URL")
            if k == "hidden":
                v = bool(v)
            if k == "provider" and v != net.provider:
                ov.pop("explorer_api", None)   # an explorer URL set for the old provider no longer applies
            if v in (None, "") or (k == "hidden" and not v):
                ov.pop(k, None)
            else:
                ov[k] = v
        overrides[nid] = ov
        self.ctx.settings.set("network_overrides", {k: v for k, v in overrides.items() if v})
        return self.ctx.networks.get(nid).public()

    # --- API providers -------------------------------------------------------------------
    def providers(self, **_: Any) -> List[Dict[str, Any]]:
        limits = {r["id"]: r for r in self.limits()}
        nets = self.ctx.networks.all()
        out = []
        for p in providers_mod.all_providers(self.ctx.settings):
            t = providers_mod.TYPES.get(p["type"], {})
            secret = providers_mod.secret_name(p["id"])
            lim = limits.get(p["id"]) or {}
            out.append(dict(
                p, type_label=t.get("label", p["type"]), family=t.get("family"), secret=secret,
                secret_source=self.ctx.secrets.source(secret),
                networks=[{"id": n.id, "name": n.name} for n in nets if n.provider == p["id"] and not n.hidden],
                wallets=lim.get("wallets", 0), limits={k: lim.get(k) for k in (
                    "rate", "unit", "per_day", "default_rate", "default_unit", "default_per_day", "used_today")},
                removable=not p["builtin"] and not any(n.provider == p["id"] for n in nets)))
        return out

    def add_provider(self, body: Any, **_: Any) -> Dict[str, Any]:
        body = body if isinstance(body, dict) else {}
        try:
            prov = providers_mod.clean(body)
        except ValueError as e:
            raise ApiError(400, str(e))
        current = providers_mod.custom(self.ctx.settings)
        prov["id"] = providers_mod.slug(prov["name"], {p["id"] for p in current})
        self.ctx.settings.set("providers", [{k: v for k, v in p.items() if k != "builtin"} for p in current] + [prov])
        if body.get("key"):
            self.ctx.secrets.set(providers_mod.secret_name(prov["id"]), str(body["key"]).strip())
        return next(p for p in self.providers() if p["id"] == prov["id"])

    def update_provider(self, pid: str, body: Any, **_: Any) -> Dict[str, Any]:
        body = body if isinstance(body, dict) else {}
        current = providers_mod.custom(self.ctx.settings)
        existing = next((p for p in current if p["id"] == pid), None)
        if not existing:
            raise ApiError(400, "built-in providers keep their settings; change their key and limits in the table")
        if body.get("type") and body["type"] != existing["type"] and \
                any(n.provider == pid for n in self.ctx.networks.all()):
            raise ApiError(400, "networks use this provider: its type cannot change")
        try:
            prov = providers_mod.clean(body, existing)
        except ValueError as e:
            raise ApiError(400, str(e))
        prov["id"] = pid
        self.ctx.settings.set("providers", [prov if p["id"] == pid else {k: v for k, v in p.items() if k != "builtin"}
                                            for p in current])
        if body.get("key"):
            self.ctx.secrets.set(providers_mod.secret_name(pid), str(body["key"]).strip())
        return next(p for p in self.providers() if p["id"] == pid)

    def delete_provider(self, pid: str, **_: Any) -> Dict[str, Any]:
        current = providers_mod.custom(self.ctx.settings)
        if not any(p["id"] == pid for p in current):
            raise ApiError(400, "built-in providers cannot be removed")
        users = [n.name for n in self.ctx.networks.all() if n.provider == pid]
        if users:
            raise ApiError(400, "used by " + ", ".join(users) + ": point those networks to another provider first")
        self.ctx.settings.set("providers", [{k: v for k, v in p.items() if k != "builtin"} for p in current if p["id"] != pid])
        self.ctx.secrets.delete(providers_mod.secret_name(pid))
        limits = dict(self.ctx.settings.get("rate_limits") or {})
        if limits.pop(pid, None) is not None:
            self.ctx.settings.set("rate_limits", limits)
        return {"ok": True}

    def delete_network(self, nid: str, **_: Any) -> Dict[str, Any]:
        try:
            self.ctx.networks.delete_custom(nid)
        except ValueError as e:
            raise ApiError(400, str(e))
        return {"ok": True}

    # --- wallets -------------------------------------------------------------------
    def wallets(self, **_: Any) -> List[Dict[str, Any]]:
        return views.wallets(self.ctx)

    def _clean_wallet(self, body: Dict[str, Any], network_id: str, existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            network = self.ctx.networks.get(network_id)
        except KeyError:
            raise ApiError(400, f"unknown network '{network_id}'")
        adapter = get_adapter(self.ctx, network)
        address = str(body.get("address", existing["address"] if existing else "") or "")
        if adapter.needs_address or address:
            try:
                address = adapter.validate_address(address)
            except ValueError as e:
                raise ApiError(400, f"{network.name}: {e}")
        label = str(body.get("label") or (existing or {}).get("label") or "").strip()
        if not label:
            short = f" {address[:6]}…{address[-4:]}" if len(address) > 12 else (f" {address}" if address else "")
            label = f"{network.name}{short}"
        slot = body.get("rw_slot", (existing or {}).get("rw_slot"))
        if slot in ("", None):
            slot = None
        elif not str(slot).isdigit():
            raise ApiError(400, "RW slot must be a number")
        else:
            slot = int(slot)
        return {
            "label": label[:80], "network": network.id, "address": address,
            "portfolio": (str(body.get("portfolio") or (existing or {}).get("portfolio") or "Personal").strip()[:60]
                          or "Personal"),
            "platform": str(body.get("platform", (existing or {}).get("platform") or "") or "").strip()[:60],
            "rw_slot": slot,
            "bollo_applied": 1 if body.get("bollo_applied", (existing or {}).get("bollo_applied")) else 0,
            "active": 0 if body.get("active", (existing or {}).get("active", 1)) in (False, 0, "0") else 1,
            "notes": str(body.get("notes", (existing or {}).get("notes") or ""))[:500],
            "lost": 1 if body.get("lost", (existing or {}).get("lost")) not in (None, False, 0, "0", "") else 0,
        }

    def _save_credentials(self, wallet_id: int, network_id: str, body: Dict[str, Any]) -> None:
        cls = ADAPTERS.get(self.ctx.networks.get(network_id).adapter)
        for cred in (cls.credentials if cls else []):
            if body.get(cred):
                self.ctx.secrets.set(f"wallet:{wallet_id}:{cred}", str(body[cred]).strip())

    def create_wallets(self, body: Any, **_: Any) -> List[Dict[str, Any]]:
        if not isinstance(body, dict):
            raise ApiError(400, "expected an object")
        nets = body.get("networks") or [body.get("network")]
        if not nets or not all(nets):
            raise ApiError(400, "choose a network")
        cleaned = [self._clean_wallet(body, n) for n in nets]
        if len(cleaned) > 1:  # same label on several chains: suffix the chain
            for c in cleaned:
                c["label"] = f"{c['label']} · {self.ctx.networks.get(c['network']).name}" if not body.get("label") \
                    else f"{c['label']} ({self.ctx.networks.get(c['network']).name})"
        created = []
        for c in cleaned:
            exists = self.ctx.db.scalar("SELECT id FROM wallets WHERE network=? AND address=? AND address<>''",
                                        (c["network"], c["address"]))
            if exists:
                raise ApiError(409, f"{c['address']} is already tracked on {c['network']}")
            cur = self.ctx.db.execute(
                "INSERT INTO wallets(label, network, address, portfolio, platform, rw_slot, bollo_applied, active,"
                " notes, lost, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (c["label"], c["network"], c["address"], c["portfolio"], c["platform"], c["rw_slot"],
                 c["bollo_applied"], c["active"], c["notes"], c["lost"], now()))
            self._save_credentials(cur.lastrowid, c["network"], body)
            created.append(cur.lastrowid)
        self._remember_platform(cleaned[0]["platform"])
        self._wallets_changed()
        return [w for w in views.wallets(self.ctx) if w["id"] in created]

    def update_wallet(self, wid: str, body: Any, **_: Any) -> Dict[str, Any]:
        w = self._wallet(wid)
        c = self._clean_wallet(body or {}, w["network"], existing=w)
        if c["address"] != w["address"]:
            raise ApiError(400, "the address cannot change: add a new wallet instead")
        self.ctx.db.execute(
            "UPDATE wallets SET label=?, portfolio=?, platform=?, rw_slot=?, bollo_applied=?, active=?, notes=?,"
            " lost=? WHERE id=?",
            (c["label"], c["portfolio"], c["platform"], c["rw_slot"], c["bollo_applied"], c["active"], c["notes"],
             c["lost"], w["id"]))
        self._save_credentials(w["id"], w["network"], body or {})
        self._remember_platform(c["platform"])
        self._wallets_changed()
        return next(x for x in views.wallets(self.ctx) if x["id"] == w["id"])

    def delete_wallet(self, wid: str, **_: Any) -> Dict[str, Any]:
        w = self._wallet(wid)
        cls = ADAPTERS.get(self.ctx.networks.get(w["network"]).adapter) if w["network"] else None
        for cred in (cls.credentials if cls else []):
            self.ctx.secrets.delete(f"wallet:{w['id']}:{cred}")
        self.ctx.db.execute("DELETE FROM wallets WHERE id=?", (w["id"],))
        self._wallets_changed()
        return {"ok": True}

    def _remember_platform(self, platform: str) -> None:
        """A platform typed on a wallet joins the list offered in the wallet form."""
        if platform:
            current = self.ctx.settings.get("platforms") or []
            merged = _merge_platforms(current, [platform])
            if merged != current:
                self.ctx.settings.set("platforms", merged)

    def _wallets_changed(self) -> None:
        """wallets.csv is rewritten whenever the list of wallets changes."""
        try:
            exports.write_wallets(self.ctx)
        except OSError as e:
            self.ctx.log(f"wallets.csv not written: {e}", "error")

    def _year_changed(self, year: int) -> None:
        """Refresh the files of a completed fiscal year after an edit."""
        if year <= self.ctx.calendar.last_completed_year(now()):
            try:
                exports.write_year(self.ctx, year)
            except OSError as e:
                self.ctx.log(f"exports not written: {e}", "error")

    def _wallet(self, wid: Any) -> Dict[str, Any]:
        try:
            return self.ctx.wallet(int(wid))
        except (KeyError, ValueError):
            raise ApiError(404, "wallet not found")

    # --- jobs ----------------------------------------------------------------------
    def start_job(self, kind: str, body: Any, **_: Any) -> Dict[str, Any]:
        body = body if isinstance(body, dict) else {}
        ctx = self.ctx
        if kind == "sync" and body.get("service"):
            # "Sync" on one API row: only the wallets that use that service.
            ids = sync.wallets_by_service(ctx).get(str(body["service"]), [])
            if not ids:
                raise ApiError(400, "no active wallet uses this service")
            body = dict(body, wallet_ids=ids)
        if kind == "sync":
            ids = [int(x) for x in body.get("wallet_ids") or []]
            fn = lambda log: sync.run_sync(ctx, log, ids or None, bool(body.get("full")))  # noqa: E731
        elif kind == "assets":
            ids = [int(x) for x in body.get("wallet_ids") or []]
            fn = lambda log: sync.load_assets(ctx, log, ids or None)  # noqa: E731
        elif kind == "snapshot":
            fn = lambda log: sync.take_snapshot(ctx, log, source="live")  # noqa: E731
        elif kind == "prices":
            fn = lambda log: sync.refresh_prices(ctx, PriceService(ctx), log)  # noqa: E731
        else:
            def fn(log):
                return sync.recompute(ctx, log)
        try:
            job = self.jobs.start(kind, fn)
        except RuntimeError as e:
            raise ApiError(409, str(e))
        return job.public()

    def current_job(self, **_: Any) -> Optional[Dict[str, Any]]:
        return self.jobs.current.public() if self.jobs.current else None

    # --- year-end, reports ---------------------------------------------------------------
    def year(self, year: str, query: Dict[str, str], **_: Any) -> Dict[str, Any]:
        return views.year_detail(self.ctx, int(year), query.get("portfolio") or None)

    def report_json(self, year: str, query: Dict[str, str], **_: Any) -> Dict[str, Any]:
        return report.build(self.ctx, int(year), query.get("portfolio") or None)

    def report_file(self, year: str, name: str, query: Dict[str, str], **_: Any) -> "FileResponse":
        y, pf = int(year), query.get("portfolio") or None
        if name == "year_end_balances.csv":
            text = report.balances_csv(self.ctx, y, pf)
        elif name == "rw_crypto.csv":
            text = report.rw_csv(report.build(self.ctx, y, pf))
        elif name == "transactions.csv":
            text = report.transactions_csv(self.ctx, y, pf)
        elif name == "evidence.md":
            text = report.evidence_md(self.ctx, report.build(self.ctx, y, pf))
        else:
            raise ApiError(404, "unknown report file")
        ctype = "text/csv" if name.endswith(".csv") else "text/markdown"
        return FileResponse(text.encode("utf-8"), f"{ctype}; charset=utf-8", f"TY{y}_{name}")

    def report_write(self, year: str, query: Dict[str, str], **_: Any) -> Dict[str, Any]:
        folder = report.write_folder(self.ctx, int(year), query.get("portfolio") or None)
        return {"folder": str(folder)}

    def exports_list(self, **_: Any) -> Dict[str, Any]:
        cal = self.ctx.calendar
        years = yearend.fiscal_years(self.ctx)
        current = cal.last_completed_year(now()) + 1
        return {"folder": str(exports.exports_dir(self.ctx)), "wallets": "wallets.csv",
                "years": [{"year": y, "balances": exports.balances_name(self.ctx, y),
                           "transactions": exports.transactions_name(y), "complete": True} for y in years]
                + [{"year": current, "balances": None, "transactions": exports.transactions_name(current),
                    "complete": False}]}

    def export_file(self, name: str, query: Dict[str, str], **_: Any) -> "FileResponse":
        text = exports.build_file(self.ctx, name, query.get("portfolio") or None)
        if text is None:
            raise ApiError(404, "unknown export")
        return FileResponse(text.encode("utf-8"), "text/csv; charset=utf-8", name)

    # --- transactions, assets, manual entries -----------------------------------------
    def transactions(self, query: Dict[str, str], **_: Any) -> Dict[str, Any]:
        def num(k: str) -> Optional[int]:
            v = query.get(k)
            return int(v) if v and v.isdigit() else None
        return views.transactions(self.ctx, wallet_id=num("wallet"), year=num("year"),
                                  category=query.get("category") or None, q=query.get("q") or None,
                                  portfolio=query.get("portfolio") or None, limit=num("limit") or 200,
                                  offset=num("offset") or 0, lost=query.get("lost") == "1")

    def update_transaction(self, mid: str, body: Any, **_: Any) -> Dict[str, Any]:
        body = body or {}
        try:
            classify.set_category(self.ctx, int(mid), body.get("category") or "auto", body.get("note"))
        except ValueError as e:
            raise ApiError(400, str(e))
        row = self.ctx.db.query_one("SELECT * FROM movements WHERE id=?", (int(mid),)) or {}
        if row:
            self._year_changed(self.ctx.calendar.year_of(row["ts"]))
        return row

    def assets(self, **_: Any) -> List[Dict[str, Any]]:
        return views.asset_list(self.ctx)

    def clear_assets(self, **_: Any) -> Dict[str, Any]:
        """"Clear all" on the Assets page: forget every asset (names, price mappings, spam marks)
        and the live balances. Transactions and year-end values stay. "Load all" rebuilds the
        list from the wallets with a full sync."""
        n = self.ctx.db.scalar("SELECT COUNT(*) FROM assets") or 0
        with self.ctx.db.tx():
            self.ctx.db.execute("DELETE FROM assets")
            self.ctx.db.execute("DELETE FROM current_balances")
            self.ctx.db.execute("DELETE FROM meta WHERE key LIKE 'cg_lookup:%'")
        self.ctx.mark_yearend_stale("asset list cleared")
        return {"cleared": n}

    def update_asset(self, key: str, body: Any, **_: Any) -> Dict[str, Any]:
        try:
            return assets_mod.update_mapping(self.ctx, key, body or {})
        except KeyError:
            raise ApiError(404, "asset not found")
        except ValueError as e:
            raise ApiError(400, str(e))

    def _manual_ts(self, which: str) -> int:
        if which == "now":
            return now()
        if not which.isdigit():
            raise ApiError(400, "year must be a number or 'now'")
        return self.ctx.calendar.cutoff(int(which))

    def get_manual(self, wid: str, query: Dict[str, str], **_: Any) -> Dict[str, Any]:
        w = self._wallet(wid)
        rows = self.ctx.db.query(
            "SELECT s.taken_at, s.asset_key, s.amount, a.symbol FROM snapshots s LEFT JOIN assets a ON a.key=s.asset_key"
            " WHERE s.wallet_id=? AND s.source='manual' ORDER BY s.taken_at DESC, a.symbol", (w["id"],))
        return {"wallet": w["id"], "entries": rows}

    def put_manual(self, wid: str, body: Any, **_: Any) -> Dict[str, Any]:
        """Replace the manual balances of one wallet at one fiscal-year cutoff (or now)."""
        w = self._wallet(wid)
        body = body or {}
        ts = self._manual_ts(str(body.get("year", "")))
        infos, rows = [], []
        for e in body.get("entries") or []:
            key = e.get("asset_key")
            if not key:
                ticker = str(e.get("symbol", "")).strip().upper()
                if not re.match(r"^[A-Z0-9.\-]{1,15}$", ticker):
                    raise ApiError(400, f"invalid ticker '{ticker}'")
                info = assets_mod.ticker_asset(self.ctx, w["network"], ticker)
                infos.append(info)
                key = info.key
            try:
                amount = D(e.get("amount"))
            except InvalidOperation:
                raise ApiError(400, f"invalid amount for {key}")
            rows.append((w["id"], ts, key, dstr(amount), "manual", str(e.get("note", ""))[:200]))
        assets_mod.upsert(self.ctx, infos)
        with self.ctx.db.tx():
            self.ctx.db.execute("DELETE FROM snapshots WHERE wallet_id=? AND taken_at=? AND source='manual'",
                                (w["id"], ts))
            self.ctx.db.executemany("INSERT INTO snapshots(wallet_id, taken_at, asset_key, amount, source, note)"
                                    " VALUES(?,?,?,?,?,?)", rows)
        if w["network"] == "manual":
            adapter = get_adapter(self.ctx, self.ctx.networks.get("manual"))
            bal, _ = adapter.current_balances(w, [], lambda m: None)
            from .ledger import store_current
            store_current(self.ctx, w["id"], bal, now(), snapshot_source=None)
        if str(body.get("year", "")).isdigit():
            self.ctx.mark_yearend_stale(f"manual balances of {w['label']} for {body['year']}")
        return self.get_manual(wid=str(w["id"]), query={})

    def dashboard(self, query: Dict[str, str], **_: Any) -> Dict[str, Any]:
        return views.dashboard(self.ctx, query.get("portfolio") or None)

    # --- reset -----------------------------------------------------------------------
    def reset_phrase(self, **_: Any) -> Dict[str, Any]:
        """A random sentence the user must type to confirm the reset. Valid for 10 minutes."""
        words = [_random.choice(RESET_WORDS[i]) for i in range(len(RESET_WORDS))]
        phrase = " ".join(words)
        with self._reset_lock:
            self._reset_phrase = (phrase, now() + 600)
        return {"phrase": phrase, "expires_in": 600}

    def reset(self, body: Any, **_: Any) -> Dict[str, Any]:
        """Delete everything: wallets, transactions, assets, prices, year-end values, settings,
        API keys (keychain and data/secrets.json), exports and reports. Only the empty database
        stays, as after the first start."""
        if self.jobs.current and self.jobs.current.finished_at is None:
            raise ApiError(409, "a job is running: wait for it to finish, then reset")
        typed = str((body or {}).get("phrase") or "").strip() if isinstance(body, dict) else ""
        with self._reset_lock:
            expected = self._reset_phrase
            if not expected or now() > expected[1]:
                raise ApiError(400, "the confirmation sentence expired: open the reset window again")
            if typed != expected[0]:
                raise ApiError(400, "the sentence does not match")
            self._reset_phrase = None
        ctx = self.ctx
        # API keys: the global ones, archive RPC URLs and the exchange keys of every wallet.
        names = set(KNOWN_SECRETS) | {f"rpc_url:{n.id}" for n in ctx.networks.all()} | \
            {providers_mod.secret_name(p["id"]) for p in providers_mod.custom(ctx.settings)}
        for w in ctx.db.query("SELECT id, network FROM wallets"):
            try:
                cls = ADAPTERS.get(ctx.networks.get(w["network"]).adapter)
            except KeyError:
                cls = None
            for cred in set(getattr(cls, "credentials", ()) or ()) | {"api_key", "api_secret"}:
                names.add(f"wallet:{w['id']}:{cred}")
        for name in names:
            ctx.secrets.delete(name)
        try:
            ctx.secrets.file.unlink()
        except FileNotFoundError:
            pass
        env_left = sorted(n for n in KNOWN_SECRETS if ctx.secrets.source(n) == "env")
        # Database: every table emptied; meta keeps only the schema version and the demo flag.
        tables = [r["name"] for r in ctx.db.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        with ctx.db.tx() as conn:
            conn.execute("PRAGMA defer_foreign_keys=ON")
            for t in tables:
                if t == "meta":
                    conn.execute("DELETE FROM meta WHERE key NOT IN ('schema_version', 'demo')")
                else:
                    conn.execute(f'DELETE FROM "{t}"')
        try:
            ctx.db.execute("VACUUM")  # deleted rows do not linger in the file
        except Exception:
            pass
        # Files written by the app.
        for folder in (exports.exports_dir(ctx), ctx.data_dir / "reports"):
            shutil.rmtree(folder, ignore_errors=True)
        ctx.log("Reset: all data deleted")
        return {"ok": True, "env_secrets_left": env_left}

    def limits(self, **_: Any) -> List[Dict[str, Any]]:
        """One row per API service: its key, limits, usage today and wallets that use it."""
        from .ratelimit import PRICE_SERVICES, SECRET_OF
        users = sync.wallets_by_service(self.ctx)
        rows = self.ctx.limiter.overview()
        for r in rows:
            secret = SECRET_OF.get(r["id"])
            r["secret"] = secret
            r["secret_source"] = self.ctx.secrets.source(secret) if secret else None
            r["price_service"] = r["id"] in PRICE_SERVICES
            r["wallets"] = len(users.get(r["id"], []))
        return rows

    def test_api(self, service: str, **_: Any) -> Dict[str, Any]:
        from . import apitest
        return apitest.run(self.ctx, service)

    def log(self, **_: Any) -> List[Dict[str, Any]]:
        return self.ctx.db.query("SELECT * FROM sync_log ORDER BY id DESC LIMIT 200")


# Settings whose change alters year-end values (they then need a recompute).
YEAREND_SETTINGS = ("timezone", "fiscal_year_end", "first_fiscal_year", "historical_price_providers",
                    "usd_to_eur", "snapshot_tolerance_seconds")


RESET_WORDS = (
    ("amber", "silver", "quiet", "golden", "frozen", "hidden", "purple", "rapid", "gentle", "crimson", "distant", "hollow"),
    ("falcon", "river", "harbor", "lantern", "meadow", "comet", "anchor", "orchard", "glacier", "canyon", "beacon", "willow"),
    ("carries", "follows", "guards", "paints", "counts", "crosses", "shelters", "measures", "finds", "watches"),
    ("seven", "twelve", "three", "eleven", "nine", "forty", "twenty", "five"),
    ("ledgers", "pebbles", "kettles", "candles", "ribbons", "marbles", "lanterns", "compasses", "feathers", "tickets"),
)


def _merge_platforms(current: List[str], new: List[Any]) -> List[str]:
    """Add platforms to the list, without duplicates (case-insensitive), sorted."""
    out = {str(p).strip().lower(): str(p).strip()[:60] for p in current if str(p).strip()}
    for p in new:
        p = str(p or "").strip()[:60]
        if p and p.lower() not in out:
            out[p.lower()] = p
    return sorted(out.values(), key=str.lower)


def _clean_rate_limits(value: Any, valid: Optional[set] = None) -> Dict[str, Dict[str, Any]]:
    """{service: {rate: number|null, unit: "second"|"minute", per_day: int|null}};
    empty/0 means no limit. The v0.1 form {per_second: n} is still accepted."""
    from .ratelimit import SERVICES
    if not isinstance(value, dict):
        raise ApiError(400, "rate_limits must be an object")
    out: Dict[str, Dict[str, Any]] = {}
    for sid, lim in value.items():
        if sid not in (valid or SERVICES) or not isinstance(lim, dict):
            raise ApiError(400, f"unknown service '{sid}'")
        clean: Dict[str, Any] = {}
        if "unit" in lim:
            if lim["unit"] not in ("second", "minute"):
                raise ApiError(400, f"{sid}.unit must be 'second' or 'minute'")
            clean["unit"] = lim["unit"]
        if "per_second" in lim and "rate" not in lim:   # v0.1 form
            lim = dict(lim, rate=lim["per_second"])
            clean.setdefault("unit", "second")
        for k in ("rate", "per_day"):
            if k not in lim:
                continue
            v = lim[k]
            if v in ("", None, 0, "0"):
                clean[k] = None
                continue
            try:
                num = float(str(v).replace(",", "."))
            except ValueError:
                raise ApiError(400, f"{sid}.{k} must be a number")
            if num <= 0 or (k == "per_day" and num != int(num)):
                raise ApiError(400, f"{sid}.{k} must be a positive {'whole ' if k == 'per_day' else ''}number")
            clean[k] = int(num) if k == "per_day" else num
        out[sid] = clean
    return out


STATIC_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
    ".ico": "image/x-icon", ".json": "application/json; charset=utf-8", ".woff2": "font/woff2",
    ".webmanifest": "application/manifest+json",
}


class FileResponse:
    def __init__(self, data: bytes, content_type: str, filename: str):
        self.data, self.content_type, self.filename = data, content_type, filename


def make_handler(app: App, allowed_hosts: List[str]):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"CryptoLedger/{__version__}"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet
            pass

        def _send(self, status: int, data: bytes, ctype: str, extra: Optional[Dict[str, str]] = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                             "connect-src 'self'; frame-ancestors 'none'")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _json(self, status: int, obj: Any) -> None:
            self._send(status, dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            return (self.headers.get("Host") or "").lower() in allowed_hosts

        def _handle(self) -> None:
            if not self._host_ok():
                self._json(403, {"error": "forbidden host"})
                return
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            if not path.startswith("/api/"):
                if self.command not in ("GET", "HEAD"):
                    self._json(405, {"error": "method not allowed"})
                    return
                self._static(path)
                return
            if self.command != "GET":
                if self.headers.get("X-CryptoLedger") != "1":
                    self._json(403, {"error": "missing X-CryptoLedger header"})
                    return
            query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            body: Any = None
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                if length > 2_000_000:
                    self._json(413, {"error": "body too large"})
                    return
                if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                    self._json(415, {"error": "JSON body expected"})
                    return
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except ValueError:
                    self._json(400, {"error": "invalid JSON"})
                    return
            try:
                result = app.dispatch(self.command, path, query, body)
            except ApiError as e:
                self._json(e.status, {"error": str(e)})
                return
            except Exception as e:
                app.ctx.log(traceback.format_exc(), "error")
                self._json(500, {"error": f"internal error: {e}"})
                return
            if isinstance(result, FileResponse):
                self._send(200, result.data, result.content_type,
                           {"Content-Disposition": f'attachment; filename="{result.filename}"'})
            else:
                self._json(200, result)

        def _static(self, path: str) -> None:
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            target = (WEB_DIR / rel).resolve()
            if WEB_DIR.resolve() not in target.parents or not target.is_file():
                target = WEB_DIR / "index.html"  # single-page app
            # Explicit types: some Windows registries map .js to text/plain, which nosniff would block.
            ctype = STATIC_TYPES.get(target.suffix.lower()) or mimetypes.guess_type(str(target))[0] \
                or "application/octet-stream"
            self._send(200, target.read_bytes(), ctype)

        do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _handle  # type: ignore

    return Handler


class LocalHTTPServer(ThreadingHTTPServer):
    """HTTPServer.server_bind() resolves the host with socket.getfqdn(), a reverse DNS
    lookup that can stall for seconds on macOS. The name is only cosmetic: skip it."""

    daemon_threads = True

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = port


def serve(ctx: Context, host: str = "127.0.0.1", port: int = 8765,
          ready: Optional[Callable[[ThreadingHTTPServer], None]] = None) -> None:
    from .scheduler import AutoSnapshot

    app = App(ctx)
    httpd = LocalHTTPServer((host, port), None)  # type: ignore[arg-type]
    real_port = httpd.server_address[1]
    allowed = [f"localhost:{real_port}", f"127.0.0.1:{real_port}", f"[::1]:{real_port}"]
    if host not in ("127.0.0.1", "localhost", "::1"):
        allowed.append(f"{host}:{real_port}")
    httpd.RequestHandlerClass = make_handler(app, allowed)
    httpd.daemon_threads = True
    AutoSnapshot(ctx, app.jobs).start()
    if ready:
        ready(httpd)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
