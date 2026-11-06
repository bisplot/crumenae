import unittest
from decimal import Decimal

from cryptoledger import classify, ledger
from cryptoledger.adapters import get_adapter

from helpers import FakeHttp, TempContext, add_wallet
from test_adapters import binance_handler

LOG = lambda m: None  # noqa: E731
T_FLEX = 1_760_000_000_000
T_LOCK = 1_760_086_400_000


def earn_handler(method, url, params, payload):
    window = int(params.get("startTime", 0)) <= T_FLEX <= int(params.get("endTime", 0))
    if url.endswith("/sapi/v1/simple-earn/flexible/history/rewardsRecord"):
        rows = []
        if window and params["type"] == "REALTIME":
            rows = [{"asset": "USDC", "rewards": "0.0123", "projectId": "USDC001", "type": "REALTIME", "time": T_FLEX}]
        if window and params["type"] == "BONUS":
            rows = [{"asset": "USDC", "rewards": "0.001", "projectId": "USDC001", "type": "BONUS", "time": T_FLEX}]
        return {"rows": rows, "total": len(rows)}
    if url.endswith("/sapi/v1/simple-earn/locked/history/rewardsRecord"):
        rows = [{"positionId": "123", "time": T_LOCK, "asset": "DOT", "lockPeriod": "60", "amount": "0.25"}] \
            if int(params["startTime"]) <= T_LOCK <= int(params["endTime"]) else []
        return {"rows": rows, "total": len(rows)}
    return binance_handler(method, url, params, payload)


class BinanceEarnTest(unittest.TestCase):
    def test_rewards_imported_as_reward(self):
        t = TempContext(FakeHttp().on("https://api.binance.com", earn_handler))
        try:
            ctx = t.ctx
            ctx.settings.set("first_fiscal_year", 2025)
            wallet = add_wallet(ctx, "binance", "")
            ctx.secrets.set(f"wallet:{wallet['id']}:api_key", "k")
            ctx.secrets.set(f"wallet:{wallet['id']}:api_secret", "s")
            adapter = get_adapter(ctx, ctx.networks.get("binance"))
            adapter.earn_pause = 0
            res = adapter.sync(wallet, {}, LOG)
            rewards = [m for m in res.movements if m.info.startswith("reward:")]
            self.assertEqual(sorted((m.asset_key, m.amount) for m in rewards),
                             [("binance:DOT", Decimal("0.25")), ("binance:USDC", Decimal("0.001")),
                              ("binance:USDC", Decimal("0.0123"))])
            self.assertTrue(res.state["earn_since_ms"] > T_LOCK)
            ledger.store_sync_result(ctx, wallet, res)
            classify.run(ctx)
            cats = {r["category"] for r in ctx.db.query("SELECT category FROM movements WHERE info LIKE 'reward:%'")}
            self.assertEqual(cats, {"reward"})
            # Re-sync: same uids, no duplicates.
            ledger.store_sync_result(ctx, wallet, adapter.sync(wallet, {}, LOG))
            self.assertEqual(ctx.db.scalar("SELECT COUNT(*) FROM movements WHERE info LIKE 'reward:%'"), 3)
        finally:
            t.close()


if __name__ == "__main__":
    unittest.main()
