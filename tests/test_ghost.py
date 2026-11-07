import unittest
from decimal import Decimal

from cryptoledger import assets as assets_mod
from cryptoledger import ledger, yearend
from cryptoledger.models import Movement, SyncResult
from cryptoledger.prices import GHOST_SOURCE, PriceService

from helpers import TempContext, add_wallet

CUT_2025 = 1767221999  # 2025-12-31 23:59:59 Europe/Rome
DEAD = "ethereum:0x" + "d" * 40


class GhostAssetTest(unittest.TestCase):
    def setUp(self):
        self.t = TempContext(offline=True)
        ctx = self.ctx = self.t.ctx
        ctx.settings.set("first_fiscal_year", 2025)
        net = ctx.networks.get("ethereum")
        assets_mod.upsert(ctx, [assets_mod.token_asset(ctx, net, "0x" + "d" * 40, "DEAD", "Dead token", 18)])
        self.w = add_wallet(ctx, "ethereum", "0x" + "a" * 40)
        ledger.store_sync_result(ctx, self.w, SyncResult(movements=[
            Movement("m1", "0xh", 1735689600 + 86400, DEAD, Decimal("1000"), "in", "0xfrom")]))

    def tearDown(self):
        self.t.close()

    def mark_ghost(self, **extra):
        assets_mod.update_mapping(self.ctx, DEAD, dict(extra, status="ghost"))

    def test_unmapped_token_is_flagged_until_marked_ghost(self):
        yearend.compute_year(self.ctx, PriceService(self.ctx), 2025)
        row = {r["asset_key"]: r for r in yearend.rows_for_year(self.ctx, 2025)}[DEAD]
        self.assertTrue(row["flags"][0].startswith("⛔"))
        self.assertIsNone(row["value_eur"])

        self.mark_ghost()
        yearend.compute_year(self.ctx, PriceService(self.ctx), 2025)
        row = {r["asset_key"]: r for r in yearend.rows_for_year(self.ctx, 2025)}[DEAD]
        self.assertEqual(row["amount"], "1000")              # still in the balances
        self.assertEqual(Decimal(row["value_eur"]), 0)
        self.assertEqual(row["price_source"], GHOST_SOURCE)
        self.assertFalse(any(f.startswith("⛔") for f in row["flags"]))
        self.assertTrue(any("Ghost" in f for f in row["flags"]))

    def test_not_spam_and_kept_by_sync(self):
        self.mark_ghost()
        self.assertNotEqual(self.ctx.db.scalar("SELECT category FROM movements WHERE asset_key=?", (DEAD,)), "spam")
        net = self.ctx.networks.get("ethereum")
        assets_mod.upsert(self.ctx, [assets_mod.token_asset(self.ctx, net, "0x" + "d" * 40, "DEAD")])
        a = assets_mod.get(self.ctx, DEAD)
        self.assertEqual(a["status"], "ghost")
        self.assertTrue(assets_mod.is_priced(a))
        self.assertFalse(assets_mod.is_hidden(a))

    def test_past_price_is_kept_today_is_zero(self):
        self.mark_ghost(coingecko_id="deadcoin")
        a = assets_mod.get(self.ctx, DEAD)
        ps = PriceService(self.ctx)
        # The price it had while it still traded (here from the cache) is used...
        self.ctx.db.execute("INSERT INTO prices(price_key, ts, price_eur, source, price_ts, fetched_at)"
                            " VALUES('cg:deadcoin', 1000, '2.5', 'coingecko', 1000, 1)")
        self.assertEqual(ps.price_at(a, 1000).price, Decimal("2.5"))
        # ...otherwise EUR 0; today too, unless a live market exists (offline here).
        self.assertEqual(ps.price_at(a, 2000).price, 0)
        self.assertEqual(ps.latest(a).price, 0)
        self.assertEqual(ps.refresh_current({DEAD: a}), {})


if __name__ == "__main__":
    unittest.main()
