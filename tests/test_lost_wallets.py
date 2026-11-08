"""Lost wallets: off the Dashboard, Assets ("Lost" tab) and Transactions; still in year-end values."""
import unittest
from decimal import Decimal

from cryptoledger import assets as assets_mod
from cryptoledger import ledger, sync, views, yearend
from cryptoledger.models import Movement, Snapshot, SyncResult
from cryptoledger.prices import PriceService
from cryptoledger.server import App

from helpers import TempContext, add_wallet


class LostWalletTest(unittest.TestCase):
    def setUp(self):
        self.t = TempContext(offline=True)
        ctx = self.ctx = self.t.ctx
        ctx.settings.set("first_fiscal_year", 2025)
        self.app = App(ctx)
        assets_mod.upsert(ctx, [assets_mod.native_asset(ctx.networks.get("ethereum"))])
        self.mine = add_wallet(ctx, "ethereum", "0x" + "a" * 40, label="Main")
        self.gone = add_wallet(ctx, "ethereum", "0x" + "b" * 40, label="Old phone")
        ts = ctx.calendar.cutoff(2025) - 86400
        for w, amount in ((self.mine, "2"), (self.gone, "5")):
            ledger.store_sync_result(ctx, w, SyncResult(movements=[
                Movement(f"in{w['id']}", f"0xh{w['id']}", ts, "ethereum:native", Decimal(amount), "in", "0xfrom")]))
            ctx.db.execute("INSERT INTO current_balances(wallet_id, asset_key, amount, fetched_at) VALUES(?,?,?,?)",
                           (w["id"], "ethereum:native", amount, 1))
        # A token only the lost wallet ever held.
        ctx.db.execute("INSERT INTO current_balances(wallet_id, asset_key, amount, fetched_at) VALUES(?,?,?,?)",
                       (self.gone["id"], "ethereum:0x" + "c" * 40, "7", 1))
        ctx.db.execute("INSERT OR IGNORE INTO assets(key, network, symbol, name, decimals, contract, status, updated_at)"
                       " VALUES(?,?,?,?,?,?,?,?)", ("ethereum:0x" + "c" * 40, "ethereum", "OLD", "Old token", 18,
                                                    "0x" + "c" * 40, "unmapped", 1))

    def tearDown(self):
        self.t.close()

    def mark(self, lost=True):
        return self.app.dispatch("PUT", f"/api/wallets/{self.gone['id']}", {}, {"lost": lost})

    def test_mark_and_found(self):
        self.assertTrue(self.mark()["lost"])
        w = self.app.dispatch("PUT", f"/api/wallets/{self.gone['id']}", {}, {"label": "Old phone 2018"})
        self.assertTrue(w["lost"])                       # untouched when not sent
        self.assertFalse(self.mark(False)["lost"])

    def test_dashboard_leaves_lost_wallets_out(self):
        self.mark()
        d = views.dashboard(self.ctx)
        self.assertEqual([w["label"] for w in d["wallets"]], ["Main"])
        self.assertEqual(d["lost_hidden"], 1)
        eth = next(h for h in d["holdings"] if h["symbol"] == "ETH")
        self.assertEqual(Decimal(eth["amount"]), 2)
        self.assertNotIn("OLD", [h["symbol"] for h in d["holdings"]])
        self.assertFalse(any("Old phone" in f["text"] for f in d["flags"]))

    def test_assets_lost_tab_data(self):
        self.mark()
        rows = {a["key"]: a for a in views.asset_list(self.ctx)}
        eth = rows["ethereum:native"]
        self.assertEqual((Decimal(eth["held"]), Decimal(eth["held_lost"]), eth["lost_only"]), (2, 5, False))
        old = rows["ethereum:0x" + "c" * 40]
        self.assertEqual((Decimal(old["held"]), Decimal(old["held_lost"]), old["lost_only"]), (0, 7, True))
        self.assertTrue(next(w for w in old["wallets"] if w["label"] == "Old phone")["lost"])

    def test_transactions_hidden_unless_chosen(self):
        self.mark()
        tx = lambda **q: self.app.dispatch("GET", "/api/transactions", q, None)["rows"]  # noqa: E731
        self.assertEqual({r["wallet_label"] for r in tx()}, {"Main"})
        self.assertEqual({r["wallet_label"] for r in tx(lost="1")}, {"Old phone"})
        self.assertEqual({r["wallet_label"] for r in tx(wallet=str(self.gone["id"]))}, {"Old phone"})

    def test_live_prices_skip_assets_held_only_in_lost_wallets(self):
        self.mark()
        seen = {}

        class Spy(PriceService):
            def refresh_current(self, assets):
                seen.update(assets)
                return {}
        sync.refresh_prices(self.ctx, Spy(self.ctx), lambda m: None)
        self.assertIn("ethereum:native", seen)
        self.assertNotIn("ethereum:0x" + "c" * 40, seen)

    def test_year_end_still_includes_lost_wallets(self):
        manual = add_wallet(self.ctx, "manual", "", label="Lost exchange")
        ledger.store_sync_result(self.ctx, manual, SyncResult(snapshots=[
            Snapshot(self.ctx.calendar.cutoff(2025), "manual:EUR", Decimal("100"), "manual")]))
        self.app.dispatch("PUT", f"/api/wallets/{manual['id']}", {}, {"lost": True})
        yearend.compute_year(self.ctx, PriceService(self.ctx), 2025)
        rows = [r for r in yearend.rows_for_year(self.ctx, 2025) if r["wallet_id"] == manual["id"] and r["asset_key"] != "*"]
        self.assertEqual([Decimal(r["amount"]) for r in rows], [100])          # still in the year-end balances
        self.assertIn(manual["id"], yearend.wallet_totals(self.ctx, 2025))
        series_wallets = views.dashboard(self.ctx)["series"]
        self.assertEqual([s["year"] for s in series_wallets], [2025])


if __name__ == "__main__":
    unittest.main()
