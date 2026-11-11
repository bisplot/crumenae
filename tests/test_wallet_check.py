"""Wallet check after each sync (live balance vs the history), honest wallet status, and the
year-end cases that used to give a wrong value without a flag."""
import unittest
from decimal import Decimal

from crumenae import ledger, sync, views, yearend
from crumenae.models import Movement, SyncResult
from crumenae.prices import PriceService
from crumenae.util import now

from helpers import FakeHttp, TempContext, add_wallet
from test_adapters import BTC_ME, binance_handler, btc_handler

LOG = lambda m: None  # noqa: E731


def btc(funded):
    """The Bitcoin fake, with the live balance changed (funded total) to simulate a missing tx."""
    def handler(method, url, params, payload):
        if url.endswith(f"/address/{BTC_ME}"):
            return {"chain_stats": {"funded_txo_sum": funded, "spent_txo_sum": 5_000_000}}
        return btc_handler(method, url, params, payload)
    return handler


class WalletCheckTest(unittest.TestCase):
    def sync_btc(self, funded):
        self.t = TempContext(FakeHttp().on("https://mempool.space/api", btc(funded)))
        self.ctx = self.t.ctx
        self.w = add_wallet(self.ctx, "bitcoin", BTC_ME, synced=False)
        sync.sync_wallet(self.ctx, self.ctx.wallet(self.w["id"]), LOG)
        return views.wallets(self.ctx)[0]

    def tearDown(self):
        self.t.close()

    def test_history_matching_the_live_balance_is_ok(self):
        w = self.sync_btc(8_999_000)
        self.assertEqual(w["check"]["status"], "ok")
        self.assertEqual(w["check"]["assets"], [])

    def test_difference_is_reported_per_asset(self):
        w = self.sync_btc(9_999_000)   # 0.01 BTC more on the chain than in the transactions read
        self.assertEqual(w["check"]["status"], "diff")
        a = w["check"]["assets"][0]
        self.assertEqual((a["asset_key"], Decimal(a["live"]), Decimal(a["history"]), Decimal(a["difference"])),
                         ("bitcoin:native", Decimal("0.04999"), Decimal("0.03999"), Decimal("0.01")))
        flags = [f["text"] for f in views.dashboard(self.ctx)["flags"]]
        self.assertTrue(any("differ from the transactions" in f for f in flags))


class PartialAndManualTest(unittest.TestCase):
    def setUp(self):
        self.t = TempContext(FakeHttp())
        self.ctx = self.t.ctx

    def tearDown(self):
        self.t.close()

    def snapshots(self, wid):
        return self.ctx.db.scalar("SELECT COUNT(*) FROM snapshots WHERE wallet_id=? AND source='live'", (wid,))

    def test_incomplete_live_balance_is_not_a_snapshot(self):
        def no_earn(method, url, params, payload):
            if "/simple-earn/" in url and "position" in url:
                from crumenae.http import HttpError
                raise HttpError(500, "down", url)
            return binance_handler(method, url, params, payload)
        self.t.http.on("https://api.binance.com", no_earn)
        w = add_wallet(self.ctx, "binance", "", label="Binance", synced=False)
        self.ctx.secrets.set(f"wallet:{w['id']}:api_key", "k")
        self.ctx.secrets.set(f"wallet:{w['id']}:api_secret", "s")
        sync.sync_wallet(self.ctx, self.ctx.wallet(w["id"]), LOG)
        row = views.wallets(self.ctx)[0]
        self.assertEqual(row["check"]["status"], "partial")
        self.assertIn("Simple Earn", row["check"]["not_read"][0])
        self.assertTrue(row["last_warning"])
        self.assertTrue(self.ctx.db.scalar("SELECT COUNT(*) FROM sync_log WHERE level='warn'"
                                           " AND message LIKE 'live balance incomplete%'"))
        self.assertEqual(self.snapshots(w["id"]), 0)

    def test_manual_account_sync_writes_no_snapshot(self):
        w = add_wallet(self.ctx, "manual", "", label="Ledger Live")
        ledger.store_sync_result(self.ctx, w, SyncResult(snapshots=[]))
        sync.sync_wallet(self.ctx, self.ctx.wallet(w["id"]), LOG)
        self.assertEqual(self.snapshots(w["id"]), 0)
        self.assertEqual(views.wallets(self.ctx)[0]["check"]["status"], "n/a")


class YearEndHonestyTest(unittest.TestCase):
    CUT = 1767221999   # 2025-12-31 23:59:59 Europe/Rome

    def setUp(self):
        self.t = TempContext(offline=True)
        self.ctx = self.t.ctx
        self.ctx.settings.set("first_fiscal_year", 2025)

    def tearDown(self):
        self.t.close()

    def reading(self, w):
        return yearend.balance_at_cutoff(self.ctx, self.ctx.wallet(w["id"]), self.CUT)

    def test_no_history_but_coins_held_is_not_zero(self):
        w = add_wallet(self.ctx, "bitcoin", BTC_ME)
        ledger.store_current(self.ctx, w["id"], {"bitcoin:native": Decimal("0.5")}, now(), snapshot_source=None)
        self.assertEqual(self.reading(w).source, "missing")

    def test_empty_wallet_is_still_zero(self):
        w = add_wallet(self.ctx, "bitcoin", BTC_ME)
        self.assertEqual(self.reading(w).source, "reconstructed")

    def test_mismatch_makes_the_wallet_incomplete(self):
        w = add_wallet(self.ctx, "bitcoin", BTC_ME)
        ledger.store_sync_result(self.ctx, w, SyncResult(movements=[
            Movement("m1", "h1", self.CUT - 86400 * 30, "bitcoin:native", Decimal("1"), "in", "x")]))
        self.ctx.db.execute("INSERT INTO snapshots(wallet_id, taken_at, asset_key, amount, source, note)"
                            " VALUES(?,?,?,?,?,?)", (w["id"], self.CUT, "bitcoin:native", "2", "live", ""))
        yearend.compute_year(self.ctx, PriceService(self.ctx), 2025)
        self.assertFalse(yearend.wallet_totals(self.ctx, 2025)[w["id"]]["complete"])
        self.assertFalse(views.year_detail(self.ctx, 2025)["wallets"][0]["complete"])


if __name__ == "__main__":
    unittest.main()
