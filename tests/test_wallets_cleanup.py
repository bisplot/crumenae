"""Wallets page and API: adding on several chains is all or nothing, clear errors instead of
crashes, paused wallets can be synced by themselves, edits and deletes mark the year-end."""
import unittest

from crumenae import sync
from crumenae.server import ApiError, App

from helpers import TempContext, add_wallet

A = "0x" + "a" * 40
LOG = lambda m: None  # noqa: E731


class WalletsApiTest(unittest.TestCase):
    def setUp(self):
        self.t = TempContext(offline=True)
        self.ctx = self.t.ctx
        self.app = App(self.ctx)

    def tearDown(self):
        self.t.close()

    def post(self, body):
        return self.app.dispatch("POST", "/api/wallets", {}, body)

    def count(self):
        return self.ctx.db.scalar("SELECT COUNT(*) FROM wallets")

    def test_several_chains_all_or_nothing(self):
        self.post({"network": "polygon", "address": A})
        with self.assertRaises(ApiError) as e:
            self.post({"networks": ["ethereum", "polygon"], "address": A})   # polygon already tracked
        self.assertEqual(e.exception.status, 409)
        self.assertEqual(self.count(), 1)                                       # ethereum not half-created

    def test_same_label_on_an_exchange_is_a_clear_error(self):
        body = {"network": "manual", "label": "Ledger Live"}
        self.post(body)
        with self.assertRaises(ApiError) as e:
            self.post(body)
        self.assertEqual(e.exception.status, 409)
        self.assertIn("already exists", str(e.exception))

    def test_exchange_needs_its_keys(self):
        with self.assertRaises(ApiError) as e:
            self.post({"network": "binance", "label": "Binance"})
        self.assertEqual(e.exception.status, 400)
        self.assertEqual(self.count(), 0)
        w = self.post({"network": "binance", "label": "Binance", "api_key": "k", "api_secret": "s"})[0]
        self.assertTrue(all(w["credentials"].values()))

    def test_edit_and_delete_mark_the_year_end(self):
        w = self.post({"network": "ethereum", "address": A})[0]
        self.ctx.db.execute("DELETE FROM meta WHERE key='yearend_stale'")
        self.app.dispatch("PUT", f"/api/wallets/{w['id']}", {}, {"notes": "just a note"})
        self.assertFalse(self.ctx.yearend_stale())                              # a note changes nothing
        self.app.dispatch("PUT", f"/api/wallets/{w['id']}", {}, {"rw_slot": "3"})
        self.assertTrue(self.ctx.yearend_stale())
        self.ctx.db.execute("DELETE FROM meta WHERE key='yearend_stale'")
        self.app.dispatch("DELETE", f"/api/wallets/{w['id']}", {}, None)
        self.assertTrue(self.ctx.yearend_stale())

    def test_old_address_stays_editable(self):
        w = add_wallet(self.ctx, "ethereum", "0xNOT-VALID-ANY-MORE")                # saved by an older version
        out = self.app.dispatch("PUT", f"/api/wallets/{w['id']}", {}, {"label": "Renamed"})
        self.assertEqual(out["label"], "Renamed")


class PausedSyncTest(unittest.TestCase):
    def test_a_paused_wallet_chosen_by_itself_is_synced(self):
        t = TempContext(offline=False)
        try:
            ctx = t.ctx
            w = add_wallet(ctx, "manual", "", label="Paused", synced=False)
            ctx.db.execute("UPDATE wallets SET active=0 WHERE id=?", (w["id"],))
            sync.run_sync(ctx, LOG)                                             # Sync all: skipped
            self.assertIsNone(ctx.wallet(w["id"])["last_sync_at"])
            sync.run_sync(ctx, LOG, wallet_ids=[w["id"]])                       # its own Sync button
            self.assertIsNotNone(ctx.wallet(w["id"])["last_sync_at"])
        finally:
            t.close()


if __name__ == "__main__":
    unittest.main()
