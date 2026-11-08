"""Staking found automatically at each sync: Terra delegations, Tron frozen TRX and voting rewards,
liquid-staking tokens. Nothing needs the "Staking position" tick."""
import unittest
from decimal import Decimal

from cryptoledger import classify, sync, views

from helpers import FakeHttp, TempContext, add_wallet
from test_adapters import TRON_ME, tron_handler
from test_cosmos import ME, VALOPER, lcd_handler

LOG = lambda m: None  # noqa: E731


def terra_handler(method, url, params, payload):
    if url.endswith(f"/cosmos/distribution/v1beta1/delegators/{ME}/rewards"):
        return {"rewards": [{"validator_address": VALOPER, "reward": [{"denom": "uluna", "amount": "250000.123"}]}],
                "total": []}
    if url.endswith(f"/cosmos/staking/v1beta1/validators/{VALOPER}"):
        return {"validator": {"description": {"moniker": "Good Validator"}}}
    if url.endswith(f"/cosmos/staking/v1beta1/delegators/{ME}/unbonding_delegations"):
        return {"unbonding_responses": [{"validator_address": VALOPER, "entries": [{"balance": "1000000"}]}]}
    return lcd_handler(method, url, params, payload)


def tron_staking_handler(method, url, params, payload):
    if url.endswith("/wallet/getReward"):
        return {"reward": 1_500_000}
    if url.endswith("/wallet/gettransactioninfobyid"):
        return {"id": payload["value"], "withdraw_amount": 3_000_000}
    if url.endswith(f"/v1/accounts/{TRON_ME}"):
        return {"success": True, "data": [{"balance": 18_555_000, "frozenV2": [{"amount": 20_000_000}, {"type": "ENERGY"}],
                                            "unfrozenV2": [{"unfreeze_amount": 5_000_000}],
                                            "votes": [{"vote_address": "41" + "33" * 20, "vote_count": 20}]}]}
    if url.endswith("/transactions") and not url.endswith("/trc20/transactions"):
        data = tron_handler(method, url, params, payload)
        claims = [
            {"txID": "c1", "block_timestamp": 1739000000000, "ret": [{"contractRet": "SUCCESS", "fee": 0}],
             "withdraw_amount": 2_000_000,
             "raw_data": {"contract": [{"type": "WithdrawBalanceContract", "parameter": {"value": {
                 "owner_address": "41" + "11" * 20}}}]}},
            {"txID": "c2", "block_timestamp": 1740000000000, "ret": [{"contractRet": "SUCCESS", "fee": 0}],
             "raw_data": {"contract": [{"type": "WithdrawBalanceContract", "parameter": {"value": {
                 "owner_address": "41" + "11" * 20}}}]}}]
        return dict(data, data=data["data"] + claims)
    return tron_handler(method, url, params, payload)


class TerraStakingTest(unittest.TestCase):
    def test_delegations_listed_without_tick(self):
        t = TempContext(FakeHttp().on("https://terra-classic-lcd.publicnode.com", terra_handler))
        try:
            w = add_wallet(t.ctx, "terra_classic", ME)
            sync.sync_wallet(t.ctx, t.ctx.wallet(w["id"]), LOG)
            staked = views.staking(t.ctx, 2025)["staked"]
            self.assertEqual(len(staked), 1)
            p = staked[0]
            self.assertEqual((p["protocol"], p["validator"], p["symbol"]), ("Terra Classic (LUNC) staking", "Good Validator", "LUNC"))
            self.assertEqual((Decimal(p["staked"]), Decimal(p["unbonding"]), Decimal(p["pending_rewards"])),
                             (Decimal(4), Decimal(1), Decimal("0.25")))
            self.assertFalse(p["wallet"]["staking"])                     # no tick needed
            # Unchanged: staked coins are in the balance.
            held = {r["asset_key"]: Decimal(r["amount"]) for r in t.ctx.db.query("SELECT * FROM current_balances")}
            self.assertEqual(held["terra_classic:native"], Decimal("8.138"))
        finally:
            t.close()


class TronStakingTest(unittest.TestCase):
    def setUp(self):
        self.t = TempContext(FakeHttp().on("https://api.trongrid.io", tron_staking_handler))
        self.ctx = self.t.ctx
        self.w = add_wallet(self.ctx, "tron", TRON_ME)

    def tearDown(self):
        self.t.close()

    def test_frozen_trx_votes_and_pending_rewards(self):
        sync.sync_wallet(self.ctx, self.ctx.wallet(self.w["id"]), LOG)
        p = views.staking(self.ctx, 2025)["staked"][0]
        self.assertEqual((p["protocol"], Decimal(p["staked"]), Decimal(p["unbonding"]), Decimal(p["pending_rewards"])),
                         ("Tron Stake 2.0", Decimal(20), Decimal(5), Decimal("1.5")))
        self.assertTrue(p["validator"].startswith("T"))                # the Super Representative voted for

    def test_claimed_voting_rewards_are_income(self):
        sync.sync_wallet(self.ctx, self.ctx.wallet(self.w["id"]), LOG)
        classify.run(self.ctx)
        rows = {r["uid"]: r for r in self.ctx.db.query("SELECT * FROM movements WHERE uid LIKE 'c%'")}
        self.assertEqual((Decimal(rows["c1:reward"]["amount"]), rows["c1:reward"]["category"]), (Decimal(2), "reward"))
        self.assertEqual(Decimal(rows["c2:reward"]["amount"]), Decimal(3))   # amount read from the tx info
        self.assertFalse(self.ctx.db.scalar("SELECT COUNT(*) FROM sync_log WHERE message LIKE '%WithdrawBalance%'"))


class LiquidStakingTokenTest(unittest.TestCase):
    def test_steth_listed(self):
        t = TempContext(offline=True)
        try:
            from cryptoledger import assets as assets_mod
            ctx = t.ctx
            w = add_wallet(ctx, "ethereum", "0x" + "a" * 40)
            steth = "0xae7ab96520de3a18e5e111b5eaab095312d7fe84"
            assets_mod.upsert(ctx, [assets_mod.token_asset(ctx, ctx.networks.get("ethereum"), steth)])
            ctx.db.execute("INSERT INTO current_balances(wallet_id, asset_key, amount, fetched_at) VALUES(?,?,?,?)",
                           (w["id"], f"ethereum:{steth}", "1.5", 1))
            p = views.staking(ctx, 2025)["staked"]
            self.assertEqual([(x["protocol"], x["symbol"], x["staked"]) for x in p], [("Lido", "stETH", "1.5")])
        finally:
            t.close()


if __name__ == "__main__":
    unittest.main()
