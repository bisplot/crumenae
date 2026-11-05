"""The Settings "Test" button: one cheap call per service; free calls are not counted."""
import unittest

from cryptoledger import apitest
from cryptoledger.http import HttpError

from helpers import FakeHttp, TempContext


def handler(method, url, params, payload):
    if "api.etherscan.io" in url:
        if params.get("apikey") == "GOOD":
            return {"jsonrpc": "2.0", "id": 1, "result": "0x1700000"}
        return {"status": "0", "message": "NOTOK", "result": "Invalid API Key"}
    if url.endswith("/monitor/usage"):
        raise HttpError(401, '{"success":false}', url)
    if url.endswith("/v1/key/info"):
        return {"data": {"plan": {"credit_limit_monthly": 10000, "rate_limit_minute": 30},
                         "usage": {"current_day": {"credits_used": 12}, "current_month": {"credits_left": 9500}}}}
    if url.endswith("/ping") and "coingecko" in url:
        return {"gecko_says": "(V3) To the Moon!"}
    return None


class ApiTestTest(unittest.TestCase):
    def setUp(self):
        self.t = TempContext(FakeHttp().on("https://", handler))
        self.ctx = self.t.ctx

    def tearDown(self):
        self.t.close()

    def test_etherscan_good_and_bad_key(self):
        self.assertFalse(apitest.run(self.ctx, "etherscan")["ok"])            # no key saved
        self.ctx.secrets.set("etherscan_api_key", "BAD")
        r = apitest.run(self.ctx, "etherscan")
        self.assertEqual((r["ok"], r["cost"]), (False, "1 call"))
        self.assertIn("Invalid API Key", r["message"])
        self.ctx.secrets.set("etherscan_api_key", "GOOD")
        r = apitest.run(self.ctx, "etherscan")
        self.assertTrue(r["ok"])
        self.assertIn("24,117,248", r["message"])

    def test_solscan_401_explains_v2_key(self):
        self.ctx.secrets.set("solscan_api_key", "V1KEY")
        r = apitest.run(self.ctx, "solana")
        self.assertFalse(r["ok"])
        self.assertIn("V2", r["message"])

    def test_coinmarketcap_is_free_and_reports_plan(self):
        self.ctx.secrets.set("coinmarketcap_api_key", "K")
        r = apitest.run(self.ctx, "coinmarketcap")
        self.assertEqual((r["ok"], r["cost"]), (True, "free (no credits)"))
        self.assertIn("30 calls/min", r["message"])
        self.assertIn("used today: 12", r["message"])
        self.assertTrue(apitest.run(self.ctx, "coingecko")["ok"])

    def test_free_calls_are_not_counted(self):
        from cryptoledger.http import HttpClient
        from cryptoledger.ratelimit import DailyLimitReached, utc_day
        client = HttpClient(retries=0)
        client.limiter = self.ctx.limiter
        self.ctx.settings.set("rate_limits", {"coinmarketcap": {"per_day": 1}})
        self.ctx.db.execute("INSERT INTO api_usage(service, day, calls) VALUES('coinmarketcap', ?, 1)", (utc_day(),))
        self.ctx.limiter.acquire("pro-api.coinmarketcap.com", count=False)    # free check passes the cap
        self.assertEqual(self.ctx.limiter.used_today("coinmarketcap"), 1)     # and is not counted
        with self.assertRaises(DailyLimitReached):
            self.ctx.limiter.acquire("pro-api.coinmarketcap.com")

    def test_offline(self):
        self.ctx.offline = True
        self.assertIn("offline", apitest.run(self.ctx, "etherscan")["message"])


if __name__ == "__main__":
    unittest.main()
