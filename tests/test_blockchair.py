"""Dogecoin and Bitcoin Cash through Blockchair (the default, no key needed): history with paging
and resume, unconfirmed transactions, live balance and the wallet check, the optional key, clear
errors when Blockchair refuses; the optional Blockbook key and the Settings Test."""
import unittest
from decimal import Decimal
from unittest import mock

from crumenae import apitest, sync, views
from crumenae.adapters import blockchair, get_adapter
from crumenae.http import HttpError
from crumenae.models import AdapterError

from helpers import FakeHttp, TempContext, add_wallet
from test_btc_like_coins import BCH, DOGE

LOG = lambda m: None  # noqa: E731
BASE = "https://api.blockchair.com"


def row(h, block, when, change):
    return {"block_id": block, "hash": h, "time": when, "balance_change": change}


DOGE_TXS = [   # newest first, like Blockchair
    row("pending", -1, "2026-11-19 10:00:00", 5_00000000),
    row("d3", 300, "2026-03-01 10:00:00", -201_00000000),    # 200 sent + 1 fee
    row("d2", 200, "2025-12-01 10:00:00", 100_00000000),
    row("d1", 100, "2025-06-01 10:00:00", 400_00000000),
]


class FakeBlockchair:
    def __init__(self, chain, address, txs, balance):
        self.chain, self.address, self.txs, self.balance = chain, address, txs, balance
        self.calls, self.refuse = [], None

    def __call__(self, method, url, params, payload):
        if not url.startswith(f"{BASE}/{self.chain}/dashboards/address/"):
            return None
        self.calls.append((url, dict(params or {})))
        if self.refuse:
            return {"data": None, "context": {"code": self.refuse, "error": "Your IP address is temporary blacklisted"}}
        limit, offset = int(params.get("limit", 100)), int(params.get("offset", 0))
        details = params.get("transaction_details") == "true"
        txs = self.txs[offset:offset + limit]
        return {"data": {self.address: {
                    "address": {"balance": self.balance, "transaction_count": len(self.txs)},
                    "transactions": txs if details else [t["hash"] for t in txs]}},
                "context": {"code": 200}}


class DogecoinBlockchairTest(unittest.TestCase):
    def setUp(self):
        self.fake = FakeBlockchair("dogecoin", DOGE, DOGE_TXS, 299_00000000)
        self.t = TempContext(FakeHttp().on(BASE, self.fake))
        self.ctx = self.t.ctx
        self.ctx.offline = False
        self.w = add_wallet(self.ctx, "dogecoin", DOGE, synced=False)
        self.adapter = get_adapter(self.ctx, self.ctx.networks.get("dogecoin"))
        patcher = mock.patch.object(blockchair, "PAGE", 2)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.t.close()

    def test_default_provider_is_blockchair(self):
        net = self.ctx.networks.get("dogecoin")
        self.assertEqual((net.provider, net.adapter, self.adapter.chain), ("blockchair", "blockchair", "dogecoin"))
        self.assertEqual(self.ctx.networks.get("bitcoin_cash").blockchair_chain, "bitcoin-cash")

    def test_pages_unconfirmed_and_amounts(self):
        res = self.adapter.sync(self.w, {}, LOG)
        self.assertEqual({m.uid: m.amount for m in res.movements},
                         {"d1:coin": Decimal(400), "d2:coin": Decimal(100), "d3:coin": Decimal(-201)})
        self.assertEqual([p["offset"] for _, p in self.fake.calls], [0, 2])
        self.assertEqual(res.state["newest_txid"], "d3")
        self.assertNotIn("key", self.fake.calls[0][1])                       # no key: none sent

    def test_resume_balance_and_wallet_check(self):
        sync.sync_wallet(self.ctx, self.ctx.wallet(self.w["id"]), LOG)
        bal = self.ctx.db.scalar("SELECT amount FROM current_balances WHERE asset_key='dogecoin:native'")
        self.assertEqual(Decimal(bal), Decimal(299))
        self.assertEqual(views.wallets(self.ctx)[0]["check"]["status"], "ok")   # 400 + 100 - 201 = 299
        self.fake.calls.clear()
        self.fake.txs.insert(1, row("d4", 400, "2026-11-01 10:00:00", 1_00000000))
        again = self.adapter.sync(self.w, self.ctx.sync_state(self.w["id"]), LOG)
        self.assertEqual([m.uid for m in again.movements], ["d4:coin"])
        self.assertEqual([p["offset"] for _, p in self.fake.calls], [0, 2])  # stops at the known tip

    def test_optional_key_is_sent(self):
        self.ctx.secrets.set("blockchair_api_key", "K1")
        self.adapter.sync(self.w, {}, LOG)
        self.assertEqual(self.fake.calls[0][1]["key"], "K1")

    def test_refused_is_a_clear_error(self):
        self.fake.refuse = 430
        with self.assertRaises(AdapterError) as e:
            self.adapter.sync(self.w, {}, LOG)
        self.assertIn("Blockchair refused the request (430)", str(e.exception))
        self.assertIn("Blockchair key", str(e.exception))

        def http_402(method, url, params, payload):
            raise HttpError(402, '{"context":{"code":402}}', url)
        self.t.http.handlers.insert(0, (BASE, http_402))
        with self.assertRaises(AdapterError) as e:
            self.adapter.current_balances(self.w, [], LOG)
        self.assertIn("daily limit", str(e.exception))


class BitcoinCashBlockchairTest(unittest.TestCase):
    def test_cashaddr_prefix_is_dropped_in_the_url(self):
        bare = BCH.split(":")[1]
        fake = FakeBlockchair("bitcoin-cash", bare, [row("b1", 10, "2025-01-01 00:00:00", 2_000_000)], 2_000_000)
        t = TempContext(FakeHttp().on(BASE, fake))
        try:
            w = add_wallet(t.ctx, "bitcoin_cash", BCH, synced=False)
            sync.sync_wallet(t.ctx, t.ctx.wallet(w["id"]), LOG)
            self.assertTrue(fake.calls[0][0].endswith("/bitcoin-cash/dashboards/address/" + bare))
            self.assertEqual(t.ctx.db.scalar("SELECT amount FROM movements WHERE uid='b1:coin'"), "0.02")
            self.assertEqual(views.wallets(t.ctx)[0]["check"]["status"], "ok")
        finally:
            t.close()


class Recorder:
    """get_json that records headers (FakeHttp ignores them)."""
    def __init__(self, answer):
        self.answer, self.seen = answer, []

    def get_json(self, url, params=None, headers=None, count=True):
        self.seen.append((url, dict(params or {}), dict(headers or {})))
        return self.answer


class KeysAndTestButtonTest(unittest.TestCase):
    def setUp(self):
        self.t = TempContext()
        self.ctx = self.t.ctx
        self.ctx.offline = False

    def tearDown(self):
        self.t.close()

    def test_blockbook_key_goes_in_the_api_key_header(self):
        self.ctx.settings.set("network_overrides", {"dogecoin": {"provider": "blockbook_doge"}})
        self.ctx.http = Recorder({"address": DOGE, "balance": "100000000"})
        adapter = get_adapter(self.ctx, self.ctx.networks.get("dogecoin"))
        w = add_wallet(self.ctx, "dogecoin", DOGE)
        adapter.current_balances(w, [], LOG)
        self.assertEqual(self.ctx.http.seen[-1][2], {})                      # no key: no header
        self.ctx.secrets.set("blockbook_doge_api_key", "NK")
        adapter.current_balances(w, [], LOG)
        self.assertTrue(self.ctx.http.seen[-1][0].startswith("https://doge1.trezor.io/api/v2/address/"))
        self.assertEqual(self.ctx.http.seen[-1][2], {"api-key": "NK"})

    def test_blockchair_test_button(self):
        self.ctx.http = Recorder({"data": {"blocks": 5_900_000}, "context": {"code": 200}})
        r = apitest.run(self.ctx, "blockchair")
        self.assertEqual((r["ok"], r["cost"]), (True, "1 call"))
        self.assertIn("5,900,000", r["message"])
        self.assertTrue(self.ctx.http.seen[0][0].endswith("/dogecoin/stats"))
        self.ctx.http = Recorder({"data": None, "context": {"code": 430, "error": "blacklisted"}})
        r = apitest.run(self.ctx, "blockchair")
        self.assertFalse(r["ok"])
        self.assertIn("430", r["message"])

    def test_networks_pinned_to_trezor_go_back_to_blockchair(self):
        self.ctx.settings.set("network_overrides", {
            "bitcoin_cash": {"provider": "blockbook_bch", "hidden": False},
            "dogecoin": {"explorer_api": "https://doge1.trezor.io"},
            "litecoin": {"hidden": True}})
        self.ctx.db.execute("DELETE FROM meta WHERE key='blockchair_default_v1'")
        self.ctx.db._migrate()
        self.assertEqual(self.ctx.settings.get("network_overrides"),
                         {"bitcoin_cash": {"hidden": False}, "litecoin": {"hidden": True}})
        for nid in ("dogecoin", "bitcoin_cash"):
            net = self.ctx.networks.get(nid)
            self.assertEqual((net.provider, net.explorer_api), ("blockchair", "https://api.blockchair.com"))
        self.ctx.settings.set("network_overrides", {"dogecoin": {"provider": "blockbook_doge"}})
        self.ctx.db._migrate()                                               # once: a later choice is kept
        self.assertEqual(self.ctx.networks.get("dogecoin").provider, "blockbook_doge")

    def test_optional_keys_can_be_saved(self):
        from crumenae.secrets import KNOWN
        for name in ("blockchair_api_key", "blockbook_doge_api_key", "blockbook_bch_api_key"):
            self.assertIn(name, KNOWN)


if __name__ == "__main__":
    unittest.main()
