"""Litecoin (Esplora), Dogecoin and Bitcoin Cash (Blockbook): address checks, history with the
Bitcoin rules, paging, resume, live balance and the wallet check."""
import unittest
from decimal import Decimal

from cryptoledger import sync, views
from cryptoledger.adapters import get_adapter
from cryptoledger.adapters.utxo_address import cashaddr_valid, validate
from cryptoledger.b58 import b58check_encode

from helpers import FakeHttp, TempContext, add_wallet

LOG = lambda m: None  # noqa: E731
LTC = b58check_encode(bytes([0x30]) + bytes(range(20)))      # L…
DOGE = b58check_encode(bytes([0x1E]) + bytes(range(20)))     # D…
OTHER_DOGE = b58check_encode(bytes([0x1E]) + bytes([9] * 20))
BCH = "bitcoincash:qpm2qsznhks23z7629mms6s4cwef74vcwvy22gdx6a"   # cashaddr spec example
BTC_LEGACY = b58check_encode(bytes([0x00]) + bytes(range(20)))


class AddressTest(unittest.TestCase):
    def test_each_coin_accepts_its_own_addresses_only(self):
        self.assertEqual(validate("litecoin", LTC), LTC)
        self.assertEqual(validate("litecoin", "LTC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KGMN4N9"),
                         "ltc1qw508d6qejxtdg4y5r3zarvary0c5xw7kgmn4n9")
        self.assertEqual(validate("dogecoin", DOGE), DOGE)
        self.assertEqual(validate("bitcoin-cash", BCH), BCH)
        self.assertEqual(validate("bitcoin-cash", BCH.split(":")[1].upper()), BCH)   # prefix optional
        self.assertEqual(validate("bitcoin-cash", BTC_LEGACY), BTC_LEGACY)            # legacy format
        for fmt, wrong in (("litecoin", BTC_LEGACY), ("dogecoin", LTC), ("bitcoin-cash", DOGE),
                           ("dogecoin", DOGE[:-1] + ("A" if DOGE[-1] != "A" else "B"))):
            with self.assertRaises(ValueError):
                validate(fmt, wrong)
        self.assertFalse(cashaddr_valid(BCH[:-1] + ("q" if BCH[-1] != "q" else "p")))   # checksum


def esplora(method, url, params, payload):
    if url.endswith(f"/address/{LTC}/txs"):
        return [{"txid": "l2", "fee": 1000, "status": {"confirmed": True, "block_time": 1736000000},
                 "vin": [{"prevout": {"scriptpubkey_address": LTC, "value": 300_000_000}}],
                 "vout": [{"scriptpubkey_address": "LShop", "value": 100_000_000},
                          {"scriptpubkey_address": LTC, "value": 199_999_000}]},
                {"txid": "l1", "fee": 500, "status": {"confirmed": True, "block_time": 1730000000},
                 "vin": [{"prevout": {"scriptpubkey_address": "LOther", "value": 400_000_000}}],
                 "vout": [{"scriptpubkey_address": LTC, "value": 300_000_000}]}]
    if url.endswith(f"/address/{LTC}"):
        return {"chain_stats": {"funded_txo_sum": 499_999_000, "spent_txo_sum": 300_000_000}}
    return None


def tx(txid, height, t, vin, vout, fees="0"):
    return {"txid": txid, "blockHeight": height, "blockTime": t, "fees": fees,
            "vin": [{"addresses": [a], "value": str(v), "isAddress": True} for a, v in vin],
            "vout": [{"addresses": [a], "value": str(v), "isAddress": True} for a, v in vout]}


DOGE_TXS = [   # newest first, like Blockbook
    tx("pending", -1, 0, [(OTHER_DOGE, 5)], [(DOGE, 5)]),
    tx("d3", 300, 1736000000, [(DOGE, 50_000_000_000)],
       [(OTHER_DOGE, 20_000_000_000), (DOGE, 29_900_000_000)], fees="100000000"),
    tx("d2", 200, 1733000000, [(OTHER_DOGE, 10_000_000_000)], [(DOGE, 10_000_000_000)]),
    tx("d1", 100, 1730000000, [(OTHER_DOGE, 40_000_000_000)], [(DOGE, 40_000_000_000)]),
]


class BlockbookFake:
    def __init__(self, address, txs, balance, page_size=2):
        self.address, self.txs, self.balance, self.page_size = address, txs, balance, page_size
        self.pages = []

    def __call__(self, method, url, params, payload):
        if not url.endswith("/api/v2/address/" + self.address):
            return None
        if params.get("details") == "basic":
            return {"address": self.address, "balance": str(self.balance)}
        page = int(params.get("page", 1))
        self.pages.append(page)
        n = self.page_size
        total = (len(self.txs) + n - 1) // n
        return {"page": page, "totalPages": total, "txs": self.txs[(page - 1) * n:page * n],
                "balance": str(self.balance)}


class LitecoinTest(unittest.TestCase):
    def test_sync_balance_and_check(self):
        t = TempContext(FakeHttp().on("https://litecoinspace.org/api", esplora))
        try:
            w = add_wallet(t.ctx, "litecoin", LTC, synced=False)
            sync.sync_wallet(t.ctx, t.ctx.wallet(w["id"]), LOG)
            moves = {r["uid"]: Decimal(r["amount"]) for r in t.ctx.db.query("SELECT uid, amount FROM movements")}
            self.assertEqual(moves, {"l1:btc": Decimal(3), "l2:fee": Decimal("-0.00001"), "l2:btc": Decimal(-1)})
            self.assertEqual(views.wallets(t.ctx)[0]["check"]["status"], "ok")
        finally:
            t.close()


class DogecoinTest(unittest.TestCase):
    def setUp(self):
        self.fake = BlockbookFake(DOGE, DOGE_TXS, 29_900_000_000)
        self.t = TempContext(FakeHttp().on("https://doge1.trezor.io", self.fake))
        self.ctx = self.t.ctx
        self.w = add_wallet(self.ctx, "dogecoin", DOGE, synced=False)
        self.adapter = get_adapter(self.ctx, self.ctx.networks.get("dogecoin"))

    def tearDown(self):
        self.t.close()

    def test_pages_fees_and_unconfirmed(self):
        res = self.adapter.sync(self.w, {}, LOG)
        got = {m.uid: m.amount for m in res.movements}
        self.assertEqual(got, {"d1:coin": Decimal(400), "d2:coin": Decimal(100),
                               "d3:fee": Decimal(-1), "d3:coin": Decimal(-200)})   # 500 in - 299 back - 1 fee
        self.assertEqual(self.fake.pages, [1, 2])
        self.assertEqual(res.state["newest_txid"], "d3")
        self.assertEqual(next(m for m in res.movements if m.uid == "d3:coin").counterparty, OTHER_DOGE)

    def test_resume_and_wallet_check(self):
        sync.sync_wallet(self.ctx, self.ctx.wallet(self.w["id"]), LOG)
        self.assertEqual(views.wallets(self.ctx)[0]["check"]["status"], "ok")      # 299 DOGE both ways
        self.fake.pages.clear()
        again = self.adapter.sync(self.w, self.ctx.sync_state(self.w["id"]), LOG)
        self.assertEqual((again.movements, self.fake.pages), ([], [1]))           # stops at the known tip


class BitcoinCashTest(unittest.TestCase):
    def test_cashaddr_with_or_without_prefix_in_the_answer(self):
        bare = BCH.split(":")[1]
        txs = [tx("b1", 10, 1730000000, [("bitcoincash:qother", 3_000_000)], [(bare, 2_000_000)])]
        fake = BlockbookFake(BCH, txs, 2_000_000)
        t = TempContext(FakeHttp().on("https://bch1.trezor.io", fake))
        try:
            w = add_wallet(t.ctx, "bitcoin_cash", BCH, synced=False)
            sync.sync_wallet(t.ctx, t.ctx.wallet(w["id"]), LOG)
            self.assertEqual(t.ctx.db.scalar("SELECT amount FROM movements WHERE uid='b1:coin'"), "0.02")
            self.assertEqual(views.wallets(t.ctx)[0]["check"]["status"], "ok")
        finally:
            t.close()

    def test_refused_by_the_server_is_a_clear_error(self):
        from cryptoledger.http import HttpError
        from cryptoledger.models import AdapterError

        def blocked(method, url, params, payload):
            raise HttpError(403, "Forbidden", url)
        t = TempContext(FakeHttp().on("https://bch1.trezor.io", blocked))
        try:
            w = add_wallet(t.ctx, "bitcoin_cash", BCH)
            with self.assertRaises(AdapterError) as e:
                get_adapter(t.ctx, t.ctx.networks.get("bitcoin_cash")).sync(w, {}, LOG)
            self.assertIn("another Blockbook server", str(e.exception))
        finally:
            t.close()


if __name__ == "__main__":
    unittest.main()
