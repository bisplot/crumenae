import os
import unittest
from decimal import Decimal

from crumenae import exports, ledger
from crumenae.models import Movement, SyncResult
from crumenae.server import ApiError, App

from helpers import TempContext, add_wallet


class PlatformListTest(unittest.TestCase):
    def setUp(self):
        self.t = TempContext()
        self.app = App(self.t.ctx)

    def tearDown(self):
        self.t.close()

    def test_starts_empty_and_learns_typed_platforms(self):
        ctx = self.t.ctx
        self.assertEqual(ctx.settings.get("platforms"), [])
        w = self.app.dispatch("POST", "/api/wallets", {}, {
            "network": "ethereum", "address": "0x" + "a" * 40, "platform": " Rabby "})[0]
        self.assertEqual(ctx.settings.get("platforms"), ["Rabby"])
        # Same name in another case: no duplicate. A new name on edit: added, sorted.
        self.app.dispatch("POST", "/api/wallets", {}, {"network": "manual", "platform": "rabby"})
        self.app.dispatch("PUT", f"/api/wallets/{w['id']}", {}, {"platform": "Keystone"})
        self.assertEqual(ctx.settings.get("platforms"), ["Keystone", "Rabby"])
        # Empty platform: nothing added.
        self.app.dispatch("POST", "/api/wallets", {}, {"network": "manual", "label": "X"})
        self.assertEqual(ctx.settings.get("platforms"), ["Keystone", "Rabby"])

    def test_settings_validation(self):
        self.app.dispatch("PUT", "/api/settings", {}, {"platforms": ["b", "A", "a", " "]})
        self.assertEqual(self.t.ctx.settings.get("platforms"), ["A", "b"])
        with self.assertRaises(ApiError):
            self.app.dispatch("PUT", "/api/settings", {}, {"platforms": "Ledger"})


class ResetTest(unittest.TestCase):
    def setUp(self):
        self.t = TempContext()
        self.app = App(self.t.ctx)

    def tearDown(self):
        self.t.close()

    def _fill(self):
        ctx = self.t.ctx
        eth = add_wallet(ctx, "ethereum", "0x" + "b" * 40)
        ledger.store_sync_result(ctx, eth, SyncResult(movements=[
            Movement("m1", "0xh", 1, "ethereum:native", Decimal("1"), "in")]))
        bn = self.app.dispatch("POST", "/api/wallets", {}, {
            "network": "binance", "api_key": "k", "api_secret": "s", "platform": "Binance"})[0]
        ctx.secrets.set("etherscan_api_key", "E")
        ctx.secrets.set("rpc_url:ethereum", "https://rpc.example")
        ctx.settings.set("timezone", "Europe/Berlin")
        ctx.db.execute("INSERT INTO prices(price_key, ts, price_eur, source, fetched_at) VALUES('x', 1, '1', 's', 1)")
        exports.write_wallets(ctx)
        (ctx.data_dir / "reports" / "r").mkdir(parents=True)
        return bn

    def test_requires_the_random_sentence(self):
        self._fill()
        with self.assertRaises(ApiError):   # no sentence asked yet
            self.app.dispatch("POST", "/api/reset", {}, {"phrase": "anything"})
        p1 = self.app.dispatch("GET", "/api/reset/phrase", {}, None)["phrase"]
        self.assertEqual(len(p1.split()), 5)
        with self.assertRaises(ApiError):
            self.app.dispatch("POST", "/api/reset", {}, {"phrase": p1.upper()})
        self.assertEqual(self.t.ctx.db.scalar("SELECT COUNT(*) FROM wallets"), 2)  # nothing deleted

    def test_deletes_everything(self):
        ctx = self.t.ctx
        bn = self._fill()
        self.assertTrue(ctx.secrets.file.exists())
        phrase = self.app.dispatch("GET", "/api/reset/phrase", {}, None)["phrase"]
        r = self.app.dispatch("POST", "/api/reset", {}, {"phrase": phrase})
        self.assertTrue(r["ok"])
        tables = [x["name"] for x in ctx.db.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('meta', 'sync_log') AND name NOT LIKE 'sqlite_%'")]
        for t in tables:
            self.assertEqual(ctx.db.scalar(f'SELECT COUNT(*) FROM "{t}"'), 0, t)
        self.assertEqual([m["key"] for m in ctx.db.query("SELECT key FROM meta")], ["schema_version"])
        self.assertEqual(ctx.settings.get("timezone"), "Europe/Rome")
        for name in ("etherscan_api_key", "rpc_url:ethereum", f"wallet:{bn['id']}:api_key", f"wallet:{bn['id']}:api_secret"):
            self.assertIsNone(ctx.secrets.get(name), name)
        self.assertFalse(ctx.secrets.file.exists())
        self.assertFalse((ctx.data_dir / "exports").exists())
        self.assertFalse((ctx.data_dir / "reports").exists())
        # The sentence works once.
        with self.assertRaises(ApiError):
            self.app.dispatch("POST", "/api/reset", {}, {"phrase": phrase})
        # The app still works afterwards.
        self.app.dispatch("POST", "/api/wallets", {}, {"network": "manual", "label": "New"})
        self.assertEqual(ctx.db.scalar("SELECT COUNT(*) FROM wallets"), 1)

    def test_env_secrets_are_reported(self):
        os.environ["CRUMENAE_HELIUS_API_KEY"] = "x"
        try:
            phrase = self.app.dispatch("GET", "/api/reset/phrase", {}, None)["phrase"]
            r = self.app.dispatch("POST", "/api/reset", {}, {"phrase": phrase})
            self.assertEqual(r["env_secrets_left"], ["helius_api_key"])
        finally:
            del os.environ["CRUMENAE_HELIUS_API_KEY"]


if __name__ == "__main__":
    unittest.main()
