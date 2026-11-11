"""The Settings "Test" button: one cheap call per service; free calls are not counted."""
import unittest

from crumenae import apitest
from crumenae.http import HttpError

from helpers import FakeHttp, TempContext


def handler(method, url, params, payload):
    if "api.etherscan.io" in url:
        if params.get("apikey") == "GOOD":
            return {"jsonrpc": "2.0", "id": 1, "result": "0x1700000"}
        return {"status": "0", "message": "NOTOK", "result": "Invalid API Key"}
    if "helius-rpc.com" in url:
        if params.get("api-key") != "GOOD":
            raise HttpError(401, '{"jsonrpc":"2.0","error":{"code":-32401,"message":"invalid api key"}}', url)
        return {"jsonrpc": "2.0", "id": 1, "result": 380000000}
    if url.endswith("/v1/key/info"):
        return {"data": {"plan": {"credit_limit_monthly": 10000, "rate_limit_minute": 30},
                         "usage": {"current_day": {"credits_used": 12}, "current_month": {"credits_left": 9500}}}}
    if url.endswith("/ping") and "coingecko" in url:
        return {"gecko_says": "(V3) To the Moon!"}
    if url.endswith("/api/v2") and "trezor.io" in url:
        return {"blockbook": {"coin": "Dogecoin", "bestHeight": 5600000}, "backend": {"blocks": 5600001}}
    if url.endswith("/api/blocks") and "sxp.mainnet.sh" in url:
        return {"meta": {"count": 1}, "data": [{"id": "ab", "height": 14500000}]}
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

    def test_solana_helius_key(self):
        self.ctx.secrets.set("helius_api_key", "WRONG")
        r = apitest.run(self.ctx, "solana")
        self.assertFalse(r["ok"])
        self.assertIn("Key refused", r["message"])
        self.ctx.secrets.set("helius_api_key", "GOOD")
        r = apitest.run(self.ctx, "solana")
        self.assertEqual((r["ok"], r["cost"]), (True, "1 credit"))
        self.assertIn("380,000,000", r["message"])

    def test_coinmarketcap_is_free_and_reports_plan(self):
        self.ctx.secrets.set("coinmarketcap_api_key", "K")
        r = apitest.run(self.ctx, "coinmarketcap")
        self.assertEqual((r["ok"], r["cost"]), (True, "free (no credits)"))
        self.assertIn("30 calls/min", r["message"])
        self.assertIn("used today: 12", r["message"])
        self.assertTrue(apitest.run(self.ctx, "coingecko")["ok"])

    def test_blockbook_and_ark_providers_have_a_test(self):
        for pid in ("blockbook_doge", "blockbook_bch", "solar"):
            r = apitest.run(self.ctx, pid)
            self.assertEqual((r["ok"], r["cost"]), (True, "1 call"), pid)
            self.assertNotIn("No test available", r["message"])
        self.assertIn("5,600,000", apitest.run(self.ctx, "blockbook_doge")["message"])
        self.assertIn("14,500,000", apitest.run(self.ctx, "solar")["message"])

    def test_403_without_a_key_does_not_blame_the_key(self):
        self.t.http.on("https://doge1.trezor.io", lambda *a: (_ for _ in ()).throw(HttpError(403, "blocked", "u")))
        self.t.http.handlers.insert(0, self.t.http.handlers.pop())
        r = apitest.run(self.ctx, "blockbook_doge")
        self.assertFalse(r["ok"])
        self.assertIn("no key is saved", r["message"])
        self.assertNotIn("copied in full", r["message"])

    def test_ecb_asks_for_csv(self):
        """The real ECB returns JSON unless the Accept header asks for CSV."""
        csv_text = ("KEY,TIME_PERIOD,OBS_VALUE\nEXR.D.USD.EUR.SP00.A,2026-11-06,1.1165\n"
                    "EXR.D.USD.EUR.SP00.A,2026-11-09,1.1190\n")

        class Ecb:
            def get_text(self, url, params=None, headers=None, count=True):
                return csv_text if "csv" in (headers or {}).get("Accept", "") else '{"header": {"id": "x"}}'

        self.ctx.http = Ecb()
        r = apitest.run(self.ctx, "ecb")
        self.assertEqual((r["ok"], r["cost"]), (True, "free"))
        self.assertIn("1.1190", r["message"])
        self.ctx.http.get_text = lambda *a, **k: '{"header": {}}'
        r = apitest.run(self.ctx, "ecb")
        self.assertFalse(r["ok"])
        self.assertIn("not CSV", r["message"])

    def test_free_calls_are_not_counted(self):
        from crumenae.http import HttpClient
        from crumenae.ratelimit import DailyLimitReached, utc_day
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
