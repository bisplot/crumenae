import json
import threading
import unittest
import urllib.error
import urllib.request
from decimal import Decimal

from cryptoledger import assets as assets_mod
from cryptoledger import classify, ledger, report, yearend
from cryptoledger.http import HttpError
from cryptoledger.models import Movement, Snapshot, SyncResult
from cryptoledger.prices import PriceService
from cryptoledger.prices.ecb import parse_csv
from cryptoledger.scheduler import AutoSnapshot

from helpers import FakeHttp, TempContext, add_wallet

CUT_2025 = 1767221999  # 2025-12-31 23:59:59 Europe/Rome
ECB_CSV = (
    "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE,OBS_STATUS\n"
    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2025-12-29,1.1700,A\n"
    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2025-12-30,1.1750,A\n"
    "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2025-12-31,1.1800,A\n"
)


def kline(close, open_ms):
    return [[open_ms, "1", "1", "1", str(close), "10", open_ms + 59_999, "1", 1, "1", "1", "0"]]


def market_handler(method, url, params, payload):
    if "data-api.binance.vision" in url:
        sym, start = params["symbol"], int(params["startTime"])
        if sym == "ETHEUR":
            return kline("2500.50", start)
        if sym == "SOLUSDT":
            return kline("118", start)
        if sym == "BTCEUR":
            return kline("80000", start)
        raise HttpError(400, '{"code":-1121,"msg":"Invalid symbol."}', url)
    if "data-api.ecb.europa.eu" in url:
        return ECB_CSV
    if "api.coingecko.com" in url and "/market_chart/range" in url:
        return {"prices": [[(CUT_2025 - 3600) * 1000, 0.95], [(CUT_2025 - 120) * 1000, 0.9512]],
                "market_caps": [], "total_volumes": []}
    return None


def asset(ctx, key):
    return assets_mod.get(ctx, key)


class PriceTest(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttp().on("https://", market_handler)
        self.t = TempContext(self.http)
        self.ctx = self.t.ctx
        nets = {n.id: n for n in self.ctx.networks.all()}
        assets_mod.upsert(self.ctx, [assets_mod.native_asset(nets["ethereum"]), assets_mod.native_asset(nets["solana"]),
                                     assets_mod.token_asset(self.ctx, nets["ethereum"], "0x1abaea1f7c830bd89acc67ec4af516284b1bc33c"),
                                     assets_mod.ticker_asset(self.ctx, "binance", "USD")])

    def tearDown(self):
        self.t.close()

    def test_ecb_csv(self):
        self.assertEqual(list(parse_csv(ECB_CSV))[-1], ("2025-12-31", Decimal("1.1800")))

    def test_binance_eur_pair_and_cache(self):
        ps = PriceService(self.ctx)
        r = ps.price_at(asset(self.ctx, "ethereum:native"), CUT_2025)
        self.assertEqual(r.price, Decimal("2500.50"))
        self.assertIn("ETHEUR 1m close", r.source)
        self.assertEqual(r.price_ts, CUT_2025)  # candle 22:59:00-22:59:59.999 UTC
        calls = len(self.http.calls)
        r2 = PriceService(self.ctx).price_at(asset(self.ctx, "ethereum:native"), CUT_2025)
        self.assertEqual((r2.price, len(self.http.calls)), (Decimal("2500.50"), calls))  # from cache

    def test_usdt_pair_converted_with_ecb(self):
        r = PriceService(self.ctx).price_at(asset(self.ctx, "solana:native"), CUT_2025)
        self.assertEqual(r.price, Decimal("118") / Decimal("1.1800"))
        self.assertIn("ECB USD/EUR 1.1800 (2025-12-31)", r.source)

    def test_coingecko_fallback_takes_last_point_before_cutoff(self):
        r = PriceService(self.ctx).price_at(asset(self.ctx, "ethereum:0x1abaea1f7c830bd89acc67ec4af516284b1bc33c"), CUT_2025)
        self.assertEqual(r.price, Decimal("0.9512"))
        self.assertEqual(r.price_ts, CUT_2025 - 120)

    def test_fiat_usd_uses_ecb(self):
        r = PriceService(self.ctx).price_at(asset(self.ctx, "binance:USD"), CUT_2025)
        self.assertEqual(r.price, Decimal(1) / Decimal("1.1800"))

    def test_unmapped_and_offline(self):
        self.assertEqual(PriceService(self.ctx).price_at(None, CUT_2025).source, "unmapped")
        self.ctx.offline = True
        r = PriceService(self.ctx).price_at(asset(self.ctx, "solana:native"), CUT_2025 - 86400)
        self.assertIsNone(r.price)


class YearEndTest(unittest.TestCase):
    def setUp(self):
        self.t = TempContext(FakeHttp().on("https://", market_handler), offline=False)
        self.ctx = self.t.ctx
        self.ctx.settings.set("first_fiscal_year", 2025)
        net = self.ctx.networks.get("bitcoin")
        assets_mod.upsert(self.ctx, [assets_mod.native_asset(net)])
        self.w = add_wallet(self.ctx, "bitcoin", "bc1qtest", label="BTC", rw_slot=4)
        moves = [
            Movement("a", "h1", 1735689600 + 86400 * 9, "bitcoin:native", Decimal("0.5"), "in", "x"),    # 10 Jan 2025
            Movement("b", "h2", 1735689600 + 86400 * 100, "bitcoin:native", Decimal("-0.1"), "out", "y"),
            Movement("c", "h3", CUT_2025 + 10, "bitcoin:native", Decimal("1"), "in", "z"),               # after cutoff
        ]
        ledger.store_sync_result(self.ctx, self.w, SyncResult(movements=moves))
        self.prices = PriceService(self.ctx)

    def tearDown(self):
        self.t.close()

    def rows(self, year):
        return {r["asset_key"]: r for r in yearend.rows_for_year(self.ctx, year)}

    def test_reconstructed_balance_and_value(self):
        yearend.compute_year(self.ctx, self.prices, 2025)
        r = self.rows(2025)
        self.assertEqual(r["*"]["balance_source"], "reconstructed")
        self.assertEqual(r["bitcoin:native"]["amount"], "0.4")
        self.assertEqual(Decimal(r["bitcoin:native"]["value_eur"]), Decimal("32000"))
        self.assertEqual(r["bitcoin:native"]["cutoff_ts"], CUT_2025)

    def test_snapshot_wins_and_mismatch_is_flagged(self):
        ledger.store_sync_result(self.ctx, self.w, SyncResult(snapshots=[
            Snapshot(CUT_2025 - 40, "bitcoin:native", Decimal("0.41"), "scheduled")]))
        yearend.compute_year(self.ctx, self.prices, 2025)
        r = self.rows(2025)
        self.assertEqual(r["*"]["balance_source"], "snapshot")
        self.assertEqual(r["bitcoin:native"]["amount"], "0.41")
        self.assertTrue(any("Mismatch" in f for f in r["bitcoin:native"]["flags"]))

    def test_manual_entry_has_priority(self):
        ledger.store_sync_result(self.ctx, self.w, SyncResult(snapshots=[
            Snapshot(CUT_2025, "bitcoin:native", Decimal("0.3"), "manual")]))
        yearend.compute_year(self.ctx, self.prices, 2025)
        self.assertEqual(self.rows(2025)["*"]["balance_source"], "manual")

    def test_exchange_without_snapshot_is_missing(self):
        ex = add_wallet(self.ctx, "binance", "", label="Exchange")
        yearend.compute_year(self.ctx, self.prices, 2025, [ex])
        flags = [r for r in yearend.rows_for_year(self.ctx, 2025) if r["wallet_id"] == ex["id"]][0]["flags"]
        self.assertTrue(flags[0].startswith("⛔"))

    def test_report_days_held_and_ic(self):
        yearend.compute_all(self.ctx, self.prices)
        rep = report.build(self.ctx, 2025, prices=self.prices)
        w = [x for x in rep["wallets"] if x["wallet_id"] == self.w["id"]][0]
        self.assertEqual(w["days_held"], 356)                     # 10 Jan → 31 Dec
        self.assertEqual(w["final_value_eur"], "32000")
        self.assertEqual(w["initial_value_eur"], "40000")         # 0.5 BTC at end of 10 Jan
        self.assertEqual(w["ic_eur"], str((Decimal("32000") * Decimal("0.002") * 356 / 365).quantize(Decimal("0.01"))))
        csv_text = report.rw_csv(rep)
        self.assertIn("col7_initial_value_eur", csv_text.splitlines()[0])
        self.assertIn("4,BTC,bitcoin", csv_text)
        self.assertIn("TY2025", report.evidence_md(self.ctx, rep))


class ClassifyTest(unittest.TestCase):
    def test_categories(self):
        t = TempContext()
        try:
            ctx = t.ctx
            a = add_wallet(ctx, "ethereum", "0x" + "a" * 40, label="A")
            b = add_wallet(ctx, "ethereum", "0x" + "b" * 40, label="B")
            ex = add_wallet(ctx, "binance", "", label="Ex")
            ledger.store_sync_result(ctx, a, SyncResult(movements=[
                Movement("1", "0xt1", 1, "ethereum:native", Decimal("-1"), "out", "0x" + "b" * 40),
                Movement("2", "0xt2", 2, "ethereum:native", Decimal("-1"), "out", "0xrouter"),
                Movement("3", "0xt2", 2, "ethereum:0xusdc", Decimal("2000"), "in", "0xrouter"),
                Movement("4", "0xt3", 3, "ethereum:0xspam", Decimal("0"), "in", "0xspammer"),
                Movement("5", "0xt4", 4, "ethereum:native", Decimal("0.5"), "in", "0xbinancehot"),
                Movement("6", "0xt5", 5, "ethereum:native", Decimal("0.2"), "in", "0xstranger"),
                Movement("7", "0xt2", 2, "ethereum:native", Decimal("-0.001"), "fee", "0xrouter"),
            ]))
            ledger.store_sync_result(ctx, ex, SyncResult(movements=[
                Movement("w1", "0xt4", 4, "binance:ETH", Decimal("-0.5"), "out", "0x" + "a" * 40)]))
            classify.run(ctx)
            cats = {r["uid"]: r["category"] for r in ctx.db.query("SELECT uid, category FROM movements")}
            self.assertEqual(cats, {"1": "internal_transfer", "2": "swap", "3": "swap", "4": "spam",
                                    "5": "internal_transfer", "6": "deposit", "7": "fee", "w1": "internal_transfer"})
            mid = ctx.db.scalar("SELECT id FROM movements WHERE uid='6'")
            classify.set_category(ctx, mid, "reward")
            classify.run(ctx)
            self.assertEqual(ctx.db.scalar("SELECT category FROM movements WHERE id=?", (mid,)), "reward")
            self.assertIsNotNone(b)
        finally:
            t.close()


class SchedulerTest(unittest.TestCase):
    def test_windows(self):
        t = TempContext()
        try:
            s = AutoSnapshot(t.ctx, None)
            self.assertEqual(s.due(CUT_2025 - 60), (2025, "pre"))
            self.assertEqual(s.due(CUT_2025 + 60), (2025, "post"))
            self.assertIsNone(s.due(CUT_2025 - 3600))
            self.assertIsNone(s.due(CUT_2025 + 1))
        finally:
            t.close()


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from cryptoledger.server import serve
        cls.t = TempContext(offline=True)
        ready = threading.Event()
        holder = {}

        def on_ready(httpd):
            holder["httpd"] = httpd
            ready.set()

        def run():
            try:
                serve(cls.t.ctx, "127.0.0.1", 0, on_ready)
            except BaseException as e:  # surface startup errors instead of a bare timeout
                holder["error"] = e
                ready.set()

        threading.Thread(target=run, daemon=True).start()
        if not ready.wait(30) or "error" in holder:
            raise RuntimeError(f"server did not start: {holder.get('error', 'timeout after 30 s')}")
        cls.httpd = holder["httpd"]
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.t.close()

    def call(self, method, path, body=None, headers=None):
        hdrs = {"X-CryptoLedger": "1"} if method != "GET" else {}
        hdrs.update(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        try:
            with self.opener.open(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_ui_and_status(self):
        status, body = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"CryptoLedger", body)
        status, body = self.call("GET", "/api/status")
        self.assertEqual(json.loads(body)["timezone"], "Europe/Rome")

    def test_wallet_lifecycle_and_protections(self):
        evm = "0x" + "c" * 40
        status, _ = self.call("POST", "/api/wallets", {"network": "ethereum", "address": evm}, headers={"X-CryptoLedger": ""})
        self.assertEqual(status, 403)                                        # no custom header
        status, body = self.call("POST", "/api/wallets", {"network": "ethereum", "address": "0x12"})
        self.assertEqual(status, 400)
        self.assertIn(b"40 hex", body)
        status, body = self.call("POST", "/api/wallets", {"networks": ["ethereum", "base"], "address": evm, "label": "MM"})
        self.assertEqual(status, 200)
        created = json.loads(body)
        self.assertEqual(sorted(w["network"] for w in created), ["base", "ethereum"])
        status, _ = self.call("POST", "/api/wallets", {"network": "ethereum", "address": evm})
        self.assertEqual(status, 409)                                        # duplicate
        status, body = self.call("POST", "/api/wallets", {"network": "binance", "label": "Bn", "api_key": "K", "api_secret": "S"})
        bn = json.loads(body)[0]
        self.assertEqual(bn["credentials"], {"api_key": True, "api_secret": True})
        self.assertNotIn(b'"S"', body)                                       # secrets never returned
        status, _ = self.call("DELETE", f"/api/wallets/{bn['id']}")
        self.assertEqual(status, 200)
        self.assertIsNone(self.t.ctx.secrets.get(f"wallet:{bn['id']}:api_secret"))

    def test_host_header_and_settings(self):
        status, _ = self.call("GET", "/api/status", headers={"Host": "attacker.example"})
        self.assertEqual(status, 403)
        status, body = self.call("PUT", "/api/settings", {"timezone": "Nowhere/Land"})
        self.assertEqual(status, 400)
        status, body = self.call("PUT", "/api/secrets", {"etherscan_api_key": "abc"})
        self.assertEqual(status, 200)
        self.assertNotIn(b"abc", body)
        self.assertIn(b'"source": "file"', body)

    def test_manual_entries(self):
        status, body = self.call("POST", "/api/wallets", {"network": "manual", "label": "Cold storage"})
        wid = json.loads(body)[0]["id"]
        status, body = self.call("PUT", f"/api/manual/{wid}", {"year": "2025", "entries": [{"symbol": "dot", "amount": "12,5".replace(",", ".")}]})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["entries"][0]["asset_key"], "manual:DOT")
        rows = yearend.rows_for_year(self.t.ctx, 2025)
        self.assertEqual([r["amount"] for r in rows if r["wallet_id"] == wid and r["asset_key"] == "manual:DOT"], ["12.5"])


if __name__ == "__main__":
    unittest.main()
