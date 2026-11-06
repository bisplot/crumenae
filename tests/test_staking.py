import unittest
from decimal import Decimal

from cryptoledger import ledger, yearend
from cryptoledger.models import Snapshot, SyncResult
from cryptoledger.prices import PriceService
from cryptoledger.server import ApiError, App

from helpers import TempContext, add_wallet


class StakingTest(unittest.TestCase):
    def setUp(self):
        self.t = TempContext(offline=True)
        self.ctx = self.t.ctx
        self.app = App(self.ctx)
        self.pos = self.app.dispatch("POST", "/api/wallets", {}, {
            "network": "manual", "label": "DOT staking", "platform": "Ledger Live", "staking": True})[0]

    def tearDown(self):
        self.t.close()

    def test_flag_is_saved_and_editable(self):
        self.assertTrue(self.pos["staking"])
        w = self.app.dispatch("PUT", f"/api/wallets/{self.pos['id']}", {}, {"staking": False})
        self.assertFalse(w["staking"])
        w = self.app.dispatch("PUT", f"/api/wallets/{self.pos['id']}", {}, {"label": "Renamed"})
        self.assertFalse(w["staking"])   # untouched when not sent

    def test_position_is_valued_at_year_end(self):
        ledger.store_sync_result(self.ctx, self.pos, SyncResult(snapshots=[
            Snapshot(self.ctx.calendar.cutoff(2025), "manual:DOT", Decimal("108"), "manual")]))
        self.ctx.settings.set("first_fiscal_year", 2025)
        yearend.compute_year(self.ctx, PriceService(self.ctx), 2025)
        s = self.app.dispatch("GET", "/api/staking", {"year": "2025"}, None)
        self.assertEqual(len(s["positions"]), 1)
        p = s["positions"][0]
        self.assertTrue(p["manual"])
        self.assertEqual(p["source"], "manual")
        self.assertEqual(p["assets"][0]["amount"], "108")

    def test_rewards_typed_by_hand(self):
        r = self.app.dispatch("POST", "/api/staking/rewards", {}, {
            "wallet_id": self.pos["id"], "symbol": "dot", "amount": "1,25", "time": "2025-06-30 14:00"})
        self.assertEqual((r["category"], r["category_source"], r["amount"]), ("reward", "user", "1.25"))
        self.app.dispatch("POST", "/api/staking/rewards", {}, {
            "wallet_id": self.pos["id"], "symbol": "DOT", "amount": "0.75", "time": "2025-12-31"})
        self.app.dispatch("POST", "/api/staking/rewards", {}, {
            "wallet_id": self.pos["id"], "symbol": "DOT", "amount": "9", "time": "2024-12-31"})   # TY2024
        s = self.app.dispatch("GET", "/api/staking", {"year": "2025"}, None)
        self.assertEqual(len(s["rewards"]), 2)
        self.assertEqual(s["reward_totals"][0]["symbol"], "DOT")
        self.assertEqual(Decimal(s["reward_totals"][0]["amount"]), Decimal("2"))
        # Typed rewards never change a manual balance (that comes from the year-end entries).
        self.assertEqual(ledger.snapshot_groups(self.ctx, self.pos["id"], ["manual"], 0, 2 ** 40), [])
        # Delete: only rewards typed by hand.
        self.app.dispatch("DELETE", f"/api/staking/rewards/{r['id']}", {}, None)
        self.assertEqual(len(self.app.dispatch("GET", "/api/staking", {"year": "2025"}, None)["rewards"]), 1)

    def test_rewards_rejected_on_synced_wallets_and_bad_input(self):
        eth = add_wallet(self.ctx, "ethereum", "0x" + "a" * 40)
        bad = [
            {"wallet_id": eth["id"], "symbol": "ETH", "amount": "1", "time": "2025-01-01"},
            {"wallet_id": self.pos["id"], "symbol": "DOT", "amount": "-1", "time": "2025-01-01"},
            {"wallet_id": self.pos["id"], "symbol": "DOT", "amount": "1", "time": "31/12/2025"},
            {"wallet_id": self.pos["id"], "symbol": "DOT", "amount": "1", "time": "2999-01-01"},
            {"wallet_id": self.pos["id"], "symbol": "<b>", "amount": "1", "time": "2025-01-01"},
        ]
        for body in bad:
            with self.assertRaises(ApiError, msg=body):
                self.app.dispatch("POST", "/api/staking/rewards", {}, body)
        mid = self.ctx.db.execute(
            "INSERT INTO movements(wallet_id, uid, ts, asset_key, amount, kind, category) VALUES(?,?,?,?,?,?,?)",
            (eth["id"], "x", 1, "ethereum:native", "1", "in", "reward")).lastrowid
        with self.assertRaises(ApiError):   # synced rewards cannot be deleted here
            self.app.dispatch("DELETE", f"/api/staking/rewards/{mid}", {}, None)


class NeverSyncedAndBscTest(unittest.TestCase):
    def setUp(self):
        self.t = TempContext(offline=True)
        self.ctx = self.t.ctx

    def tearDown(self):
        self.t.close()

    def test_never_synced_flag_explains_why(self):
        w = add_wallet(self.ctx, "solana", "So11111111111111111111111111111111111111112", synced=False)
        r = yearend.balance_at_cutoff(self.ctx, self.ctx.wallet(w["id"]), self.ctx.calendar.cutoff(2025))
        self.assertIn("press Sync now", r.flags[0])
        self.ctx.db.execute("UPDATE wallets SET last_error='Helius refused the API key (401)' WHERE id=?", (w["id"],))
        r = yearend.balance_at_cutoff(self.ctx, self.ctx.wallet(w["id"]), self.ctx.calendar.cutoff(2025))
        self.assertIn("401", r.flags[0])

    def test_bsc_uses_alchemy(self):
        from cryptoledger.adapters import get_adapter
        net = self.ctx.networks.get("bsc")
        self.assertEqual((net.adapter, net.provider, net.alchemy_network), ("alchemy", "alchemy", "bnb-mainnet"))
        a = get_adapter(self.ctx, net)
        self.assertEqual(a.validate_address("0x" + "AB" * 20), "0x" + "ab" * 20)


if __name__ == "__main__":
    unittest.main()
