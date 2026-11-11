"""Coinbase account (App API v2): signed JWT per request, accounts and balances, every
transaction per account with paging and resume, rewards, the wallet check, clear errors."""
import base64
import json
import unittest
from decimal import Decimal

from crumenae import classify, jwt_sign, sync, views
from crumenae.adapters import get_adapter
from crumenae.http import HttpError
from crumenae.models import AdapterError

from helpers import TempContext, add_wallet
from test_jwt_sign import ED_PKCS8

LOG = lambda m: None  # noqa: E731
NAME = "organizations/test-org/apiKeys/test-key"
BASE = "https://api.coinbase.com"


def pad(s):
    return s + "=" * (-len(s) % 4)


def tx(tid, kind, amount, cur, when, status="completed", **extra):
    return dict({"id": tid, "type": kind, "status": status, "created_at": when,
                 "amount": {"amount": amount, "currency": cur}, "native_amount": {"amount": "0", "currency": "EUR"}},
                **extra)


TXS = {
    "acc-btc": [tx("b4", "send", "-0.01", "BTC", "2026-04-01T10:00:00Z", to={"address": "bc1qdest"},
                   network={"hash": "abc123"}),
                tx("b3", "buy", "0.02", "BTC", "2026-03-01T10:00:00Z"),
                tx("b2", "send", "-0.5", "BTC", "2026-02-15T10:00:00Z", status="failed"),
                tx("b1", "buy", "0.05", "BTC", "2026-02-01T10:00:00Z")],
    "acc-eur": [tx("e2", "buy", "-1500", "EUR", "2026-02-01T10:00:00Z"),
                tx("e1", "fiat_deposit", "2000", "EUR", "2026-01-15T10:00:00Z")],
    "acc-eth": [tx("h1", "staking_reward", "0.001", "ETH", "2026-03-10T10:00:00Z")],
}
ACCOUNTS = [{"id": "acc-btc", "currency": {"code": "BTC"}, "balance": {"amount": "0.06", "currency": "BTC"}},
            {"id": "acc-eur", "currency": {"code": "EUR"}, "balance": {"amount": "500", "currency": "EUR"}},
            {"id": "acc-eth", "currency": {"code": "ETH"}, "balance": {"amount": "0.001", "currency": "ETH"}},
            {"id": "acc-doge", "currency": {"code": "DOGE"}, "balance": {"amount": "0", "currency": "DOGE"}}]


class FakeCoinbase:
    """Checks the JWT on every call (claims, uri, Ed25519 signature) and pages by 2."""
    def __init__(self, refuse=False):
        self.refuse, self.calls, self.txs = refuse, [], {k: list(v) for k, v in TXS.items()}

    def page(self, items, url, path):
        start = 0
        if "starting_after=" in url:
            after = url.split("starting_after=")[1].split("&")[0]
            start = [i["id"] for i in items].index(after) + 1
        chunk = items[start:start + 2]
        more = start + 2 < len(items)
        return {"data": chunk, "pagination": {"next_uri": f"{path}?limit=100&starting_after={chunk[-1]['id']}"
                                              if more else None}}

    def get_json(self, url, params=None, headers=None, count=True):
        self.calls.append(url)
        if self.refuse:
            raise HttpError(401, "Unauthorized", url)
        token = headers["Authorization"].split(" ", 1)[1]
        head, body, sig = token.split(".")
        h, c = json.loads(base64.urlsafe_b64decode(pad(head))), json.loads(base64.urlsafe_b64decode(pad(body)))
        path = url[len(BASE):].split("?")[0]
        assert h["alg"] == "EdDSA" and h["kid"] == NAME and h["nonce"]
        assert c["sub"] == NAME and c["iss"] == "cdp" and c["uri"] == f"GET api.coinbase.com{path}"
        assert c["exp"] - c["nbf"] <= 120
        seed = jwt_sign.load_private_key(ED_PKCS8)[1]
        assert base64.urlsafe_b64decode(pad(sig)) == jwt_sign.ed25519_sign(seed, f"{head}.{body}".encode())
        if path == "/v2/accounts":
            return self.page(ACCOUNTS, url, path)
        aid = path.split("/")[3]
        return self.page(self.txs.get(aid, []), url, path)


class CoinbaseTest(unittest.TestCase):
    def setUp(self):
        self.t = TempContext()
        self.ctx = self.t.ctx
        self.ctx.offline = False
        self.ctx.http = FakeCoinbase()
        self.w = add_wallet(self.ctx, "coinbase", "", label="Coinbase", synced=False)
        self.ctx.secrets.set(f"wallet:{self.w['id']}:api_key_name", NAME)
        self.ctx.secrets.set(f"wallet:{self.w['id']}:api_private_key", ED_PKCS8.replace("\n", ""))
        self.adapter = get_adapter(self.ctx, self.ctx.networks.get("coinbase"))

    def tearDown(self):
        self.t.close()

    def test_history_balances_and_check(self):
        sync.sync_wallet(self.ctx, self.ctx.wallet(self.w["id"]), LOG)
        classify.run(self.ctx)
        rows = {r["uid"]: r for r in self.ctx.db.query("SELECT * FROM movements")}
        self.assertNotIn("cb:b2", rows)                                   # failed send: not in the balance
        self.assertEqual((Decimal(rows["cb:b4"]["amount"]), rows["cb:b4"]["tx_hash"]), (Decimal("-0.01"), "abc123"))
        self.assertEqual(rows["cb:h1"]["category"], "reward")
        bal = {r["asset_key"]: Decimal(r["amount"]) for r in
               self.ctx.db.query("SELECT * FROM current_balances")}
        self.assertEqual((bal["coinbase:BTC"], bal["coinbase:EUR"]), (Decimal("0.06"), Decimal(500)))
        self.assertEqual(views.wallets(self.ctx)[0]["check"]["status"], "ok")   # history = live balance

    def test_resume_reads_only_new_transactions(self):
        first = self.adapter.sync(self.w, {}, LOG)
        self.ctx.http.txs["acc-btc"].insert(0, tx("b5", "receive", "0.003", "BTC", "2026-05-01T10:00:00Z",
                                                  **{"from": {"address": "bc1qsender"}}))
        self.ctx.http.calls.clear()
        again = get_adapter(self.ctx, self.ctx.networks.get("coinbase")).sync(self.w, first.state, LOG)
        self.assertEqual([m.uid for m in again.movements], ["cb:b5"])
        self.assertEqual(again.movements[0].counterparty, "bc1qsender")

    def test_errors(self):
        self.ctx.http.refuse = True
        with self.assertRaises(AdapterError) as e:
            self.adapter.current_balances(self.w, [], LOG)
        self.assertIn("refused the API key", str(e.exception))
        self.ctx.secrets.set(f"wallet:{self.w['id']}:api_private_key", "garbage")
        with self.assertRaises(AdapterError) as e:
            self.adapter.sync(self.w, {}, LOG)
        self.assertIn("private key not readable", str(e.exception))


if __name__ == "__main__":
    unittest.main()
