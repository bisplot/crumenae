"""Young Platform exchange account: HS256 JWT per request, balances, trades with paging and
resume, clear errors."""
import base64
import hashlib
import hmac
import json
import unittest
from decimal import Decimal

from crumenae import sync
from crumenae.adapters import get_adapter
from crumenae.adapters.youngplatform import make_jwt
from crumenae.http import HttpError
from crumenae.models import AdapterError
from crumenae.server import ApiError, App

from helpers import TempContext, add_wallet

LOG = lambda m: None  # noqa: E731
BASE = "https://api.youngplatform.com/api/v1/trader"


def b64decode(part):
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def match(mid, ts, market, side, qty, amount, fee="0", fee_cur="EUR"):
    return {"id": mid, "created_at": ts, "market": market, "side": side, "quantity": qty, "amount": amount,
            "price": "1", "fee": fee, "fee_currency": fee_cur, "venue": "venueA", "client_order_id": "c"}


MATCHES = [match(3, "2026-03-01T10:00:00.123456789Z", "BTC-EUR", 2, "0.01", "600", "0.6"),
           match(2, "2026-02-01T10:00:00Z", "ETH-EUR", 1, "1", "2500", "2.5"),
           match(1, "2026-01-01T10:00:00Z", "BTC-EUR", 1, "0.05", "3000", "3")]


class FakeYP:
    """The API: checks the two headers and the JWT on every private call."""
    def __init__(self, matches, page=2, refuse=False):
        self.matches, self.page, self.refuse = matches, page, refuse
        self.calls = []

    def get_json(self, url, params=None, headers=None, count=True):
        params = params or {}
        self.calls.append((url, dict(params), dict(headers or {})))
        if self.refuse:
            raise HttpError(401, '{"message":"unauthorized"}', url)
        token = (headers or {})["Authorization"].split(" ", 1)[1]
        head, body, sig = token.split(".")
        good = hmac.new(b"PRIV", f"{head}.{body}".encode(), hashlib.sha256).digest()
        assert b64decode(sig) == good and headers["X-Api-Key-Id"] == "KID"
        claims = json.loads(b64decode(body))
        assert claims["sub"] == "PUB" and claims["aud"] == "trader"
        assert claims["hash_payload"] == hashlib.sha256(b"").hexdigest() and claims["exp"] - claims["iat"] <= 60
        if url == BASE + "/private/balance":
            return {"balances": [{"currency": "EUR", "amount": "100.5", "vault_id": 1, "vault_type": "spot"},
                                 {"currency": "BTC", "amount": "0.03", "vault_id": 1, "vault_type": "spot"},
                                 {"currency": "BTC", "amount": "0.01", "vault_id": 2, "vault_type": "trading"}]}
        if url == BASE + "/private/sor/orders/matches/history":
            rows = self.matches
            if params.get("before_id"):
                rows = [m for m in rows if m["id"] < int(params["before_id"])]
            out = rows[:self.page]
            more = len(rows) > self.page
            return {"data": out, "has_more": more,
                    "next_cursor": {"id": out[-1]["id"], "timestamp": out[-1]["created_at"]} if more else None}
        raise AssertionError(url)


class YoungPlatformTest(unittest.TestCase):
    def setUp(self):
        self.t = TempContext()
        self.ctx = self.t.ctx
        self.ctx.offline = False
        self.ctx.http = FakeYP(MATCHES)
        self.w = add_wallet(self.ctx, "youngplatform", "", label="Young")
        for name, value in (("key_id", "KID"), ("public_key", "PUB"), ("private_key", "PRIV")):
            self.ctx.secrets.set(f"wallet:{self.w['id']}:{name}", value)
        self.adapter = get_adapter(self.ctx, self.ctx.networks.get("youngplatform"))

    def tearDown(self):
        self.t.close()

    def test_jwt_claims(self):
        head, body, _ = make_jwt("PUB", "PRIV", b'{"a":1}', now=1000).split(".")
        self.assertEqual(json.loads(b64decode(head)), {"alg": "HS256", "typ": "JWT"})
        self.assertEqual(json.loads(b64decode(body)), {"sub": "PUB", "aud": "trader", "iat": 1000, "exp": 1030,
                                                        "hash_payload": hashlib.sha256(b'{"a":1}').hexdigest()})

    def test_balances_summed_over_vaults(self):
        bal, _ = self.adapter.current_balances(self.w, [], LOG)
        self.assertEqual(bal, {"youngplatform:EUR": Decimal("100.5"), "youngplatform:BTC": Decimal("0.04")})

    def test_trades_paged_and_resumed(self):
        res = self.adapter.sync(self.w, {}, LOG)
        got = {m.uid: (m.asset_key, m.amount, m.kind) for m in res.movements}
        self.assertEqual(got["yp:match:1:base"], ("youngplatform:BTC", Decimal("0.05"), "in"))       # buy
        self.assertEqual(got["yp:match:1:quote"], ("youngplatform:EUR", Decimal(-3000), "out"))
        self.assertEqual(got["yp:match:3:base"], ("youngplatform:BTC", Decimal("-0.01"), "out"))     # sell
        self.assertEqual(got["yp:match:3:quote"], ("youngplatform:EUR", Decimal(600), "in"))
        self.assertEqual(got["yp:match:2:fee"], ("youngplatform:EUR", Decimal("-2.5"), "fee"))
        self.assertEqual(res.state["last_match_id"], 3)
        pages = [c[1] for c in self.ctx.http.calls if c[0].endswith("/matches/history")]
        self.assertEqual(pages[1]["before_id"], 2)                         # second page from the cursor
        self.ctx.http.matches = [match(4, "2026-04-01T10:00:00Z", "BTC-EUR", 1, "0.02", "1300")] + MATCHES
        self.ctx.http.calls.clear()
        again = self.adapter.sync(self.w, res.state, LOG)
        self.assertEqual({m.uid.split(":")[2] for m in again.movements}, {"4"})   # only the new trade
        self.assertEqual(len(self.ctx.http.calls), 1)

    def test_full_sync_and_wallet_status(self):
        sync.sync_wallet(self.ctx, self.ctx.wallet(self.w["id"]), LOG)
        self.assertEqual(self.ctx.db.scalar("SELECT COUNT(*) FROM movements"), 9)
        self.assertEqual(self.ctx.db.scalar("SELECT COUNT(*) FROM snapshots WHERE source='live'"), 3)   # EUR, BTC, ETH (0)

    def test_refused_key_and_missing_key(self):
        self.ctx.http.refuse = True
        with self.assertRaises(AdapterError) as e:
            self.adapter.current_balances(self.w, [], LOG)
        self.assertIn("refused the API key", str(e.exception))
        self.ctx.secrets.delete(f"wallet:{self.w['id']}:private_key")
        with self.assertRaises(AdapterError) as e:
            self.adapter.sync(self.w, {}, LOG)
        self.assertIn("private key", str(e.exception))

    def test_account_needs_its_three_values(self):
        app = App(self.ctx)
        with self.assertRaises(ApiError):
            app.dispatch("POST", "/api/wallets", {}, {"network": "youngplatform", "label": "YP2", "key_id": "k"})
        w = app.dispatch("POST", "/api/wallets", {}, {"network": "youngplatform", "label": "YP2", "key_id": "k",
                                                      "public_key": "p", "private_key": "s"})[0]
        self.assertEqual(w["credentials"], {"key_id": True, "public_key": True, "private_key": True})


if __name__ == "__main__":
    unittest.main()
