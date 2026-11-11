"""Staking was removed: the one-time cleanup of what a staking version stored, the API without
staking, and the two history fixes kept from that work (Solana transaction versions, Tron claims)."""
import json
import unittest
from decimal import Decimal

from crumenae import classify, sync
from crumenae.adapters import get_adapter
from crumenae.db import Database
from crumenae.server import ApiError, App

from helpers import FakeHttp, TempContext, add_wallet
from test_adapters import TRON_ME, solana_handler, tron_handler

LOG = lambda m: None  # noqa: E731
STAKE = "solana:stake:Stake1111"


class CleanupTest(unittest.TestCase):
    def setUp(self):
        self.t = TempContext(offline=True)
        db = self.t.ctx.db
        self.w = add_wallet(self.t.ctx, "solana", "Owner1111", staking=True)
        wid = self.w["id"]
        db.execute("UPDATE wallets SET staking=1 WHERE id=?", (wid,))
        db.execute("INSERT INTO assets(key, network, symbol, status) VALUES(?, 'solana', 'SOL (staked)', 'known')", (STAKE,))
        db.execute("INSERT INTO assets(key, network, symbol, status) VALUES('solana:native', 'solana', 'SOL', 'known')")
        for uid, key in (("a", STAKE), ("b", "solana:native")):
            db.execute("INSERT INTO movements(wallet_id, uid, tx_hash, ts, asset_key, amount, kind)"
                       " VALUES(?,?,?,?,?,?,?)", (wid, uid, "", 1, key, "1", "in"))
        for table in ("current_balances",):
            db.execute(f"INSERT INTO {table}(wallet_id, asset_key, amount, fetched_at) VALUES(?,?,?,?)", (wid, STAKE, "1", 1))
            db.execute(f"INSERT INTO {table}(wallet_id, asset_key, amount, fetched_at) VALUES(?,?,?,?)",
                       (wid, "solana:native", "2", 1))
        db.execute("CREATE TABLE staking_positions(wallet_id INTEGER, asset_key TEXT)")
        db.set_meta("earn_positions:1", "{}")
        db.set_meta("validator:terra:x", "Good")
        self.t.ctx.save_sync_state(wid, {"until": {"Owner1111": "s1", "Stake1111": "s2"}, "token_accounts": [],
                                         "stake_accounts": ["Stake1111"], "stake_epochs": {}, "reward_epoch": {}})
        db.execute("DELETE FROM meta WHERE key='cleanup_staking_v1'")   # as in a database from that version
        self.db = Database(db.path)                                      # opening it runs the migration

    def tearDown(self):
        self.db.close()
        self.t.close()

    def test_staking_data_removed_everything_else_kept(self):
        q = lambda sql: [tuple(r.values()) for r in self.db.query(sql)]  # noqa: E731
        self.assertEqual(q("SELECT asset_key FROM movements"), [("solana:native",)])
        self.assertEqual(q("SELECT asset_key FROM current_balances"), [("solana:native",)])
        self.assertEqual(q("SELECT key FROM assets"), [("solana:native",)])
        self.assertFalse(q("SELECT name FROM sqlite_master WHERE name='staking_positions'"))
        self.assertFalse(q("SELECT key FROM meta WHERE key LIKE 'earn_positions:%' OR key LIKE 'validator:%'"))
        self.assertEqual(q("SELECT staking FROM wallets"), [(0,)])
        state = json.loads(self.db.scalar("SELECT state FROM sync_state"))
        self.assertEqual(state, {"until": {"Owner1111": "s1"}, "token_accounts": []})
        self.assertTrue(self.db.get_meta("yearend_stale"))

    def test_runs_once(self):
        self.db.execute("INSERT INTO assets(key, network, symbol, status) VALUES(?, 'solana', 'X', 'known')", (STAKE,))
        self.db.close()
        self.db = Database(self.db.path)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM assets WHERE key=?", (STAKE,)), 1)


class ApiWithoutStakingTest(unittest.TestCase):
    def setUp(self):
        self.t = TempContext(offline=True)
        self.app = App(self.t.ctx)

    def tearDown(self):
        self.t.close()

    def test_no_staking_routes(self):
        for method, path in (("GET", "/api/staking"), ("POST", "/api/staking/rewards")):
            with self.assertRaises(ApiError) as e:
                self.app.dispatch(method, path, {}, {})
            self.assertEqual(e.exception.status, 404)

    def test_wallet_staking_flag_ignored(self):
        w = self.app.dispatch("POST", "/api/wallets", {}, {"network": "manual", "label": "Ledger", "staking": True})[0]
        self.assertFalse(w["staking"])


class KeptHistoryFixesTest(unittest.TestCase):
    def test_newer_solana_transaction_version_is_asked_again(self):
        asked = []

        def v2(method, url, params, payload):
            if payload["method"] == "getTransaction":
                version = payload["params"][1]["maxSupportedTransactionVersion"]
                asked.append(version)
                if version < 2:
                    return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32015, "message":
                            "Transaction version (2) is not supported by the requesting client."}}
            return solana_handler(method, url, params, payload)
        t = TempContext(FakeHttp().on("https://api.mainnet-beta.solana.com", v2))
        try:
            from helpers import fixture
            sig = next(iter(fixture("solana.json")["transactions"]))
            tx = get_adapter(t.ctx, t.ctx.networks.get("solana")).get_transaction(sig)
            self.assertEqual(asked, [1, 2])
            self.assertTrue(tx)
        finally:
            t.close()

    def test_claimed_tron_voting_rewards_are_in_the_history(self):
        def claims(method, url, params, payload):
            if url.endswith("/wallet/gettransactioninfobyid"):
                return {"id": payload["value"], "withdraw_amount": 3_000_000}
            if url.endswith("/transactions") and not url.endswith("/trc20/transactions"):
                data = tron_handler(method, url, params, payload)
                owner = {"owner_address": "41" + "11" * 20}
                extra = [
                    {"txID": "c1", "block_timestamp": 1739000000000, "ret": [{"contractRet": "SUCCESS", "fee": 0}],
                     "withdraw_amount": 2_000_000,
                     "raw_data": {"contract": [{"type": "WithdrawBalanceContract", "parameter": {"value": owner}}]}},
                    {"txID": "c2", "block_timestamp": 1740000000000, "ret": [{"contractRet": "SUCCESS", "fee": 0}],
                     "raw_data": {"contract": [{"type": "WithdrawBalanceContract", "parameter": {"value": owner}}]}}]
                return dict(data, data=data["data"] + extra)
            return tron_handler(method, url, params, payload)
        t = TempContext(FakeHttp().on("https://api.trongrid.io", claims))
        try:
            w = add_wallet(t.ctx, "tron", TRON_ME)
            sync.sync_wallet(t.ctx, t.ctx.wallet(w["id"]), LOG)
            classify.run(t.ctx)
            rows = {r["uid"]: r for r in t.ctx.db.query("SELECT * FROM movements WHERE uid LIKE 'c%'")}
            self.assertEqual((Decimal(rows["c1:reward"]["amount"]), rows["c1:reward"]["category"]), (Decimal(2), "reward"))
            self.assertEqual(Decimal(rows["c2:reward"]["amount"]), Decimal(3))   # amount read from the tx info
        finally:
            t.close()


if __name__ == "__main__":
    unittest.main()
