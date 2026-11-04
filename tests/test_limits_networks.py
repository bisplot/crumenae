import unittest
from decimal import Decimal

from cryptoledger import ledger
from cryptoledger.adapters import AdapterError, get_adapter
from cryptoledger.b58 import b58check_encode
from cryptoledger.http import HttpClient, _redact
from cryptoledger.ratelimit import DailyLimitReached, RateLimiter, utc_day

from helpers import FakeHttp, TempContext, add_wallet

LOG = lambda m: None  # noqa: E731


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.slept.append(round(s, 6))
        self.t += s


class RateLimiterTest(unittest.TestCase):
    def setUp(self):
        self.t = TempContext()
        self.ctx = self.t.ctx

    def tearDown(self):
        self.t.close()

    def test_daily_cap_and_per_second_spacing(self):
        clock = FakeClock()
        lim = RateLimiter(self.ctx.db, self.ctx.settings, clock=clock.clock, sleep=clock.sleep)
        self.ctx.settings.set("rate_limits", {"etherscan": {"per_second": 2, "per_day": 3}})
        for _ in range(3):
            self.assertEqual(lim.acquire("api.etherscan.io"), "etherscan")
        self.assertEqual(clock.slept, [0.5, 0.5])                 # 2 calls/s -> 0.5 s apart
        with self.assertRaises(DailyLimitReached) as cm:
            lim.acquire("api.etherscan.io")
        self.assertIn("daily limit of 3 calls", str(cm.exception))
        self.assertEqual(lim.used_today("etherscan"), 3)
        # Another service is unaffected; yesterday's usage does not count today.
        self.assertEqual(lim.acquire("unknown.example"), "other")
        self.ctx.db.execute("UPDATE api_usage SET day='2000-01-01' WHERE service='etherscan'")
        lim.acquire("api.etherscan.io")

    def test_defaults_and_removing_a_cap(self):
        lim = RateLimiter(self.ctx.db, self.ctx.settings)
        self.assertEqual(lim.limits("etherscan"),
                         {"rate": 3.0, "unit": "second", "per_second": 3.0, "per_day": 100000})
        self.ctx.settings.set("rate_limits", {"etherscan": {"per_day": None}})
        self.assertEqual(lim.limits("etherscan")["per_day"], None)
        # Per-minute plans: CoinGecko 30/min = one call every 2 s.
        self.assertEqual(lim.limits("coingecko"), {"rate": 30.0, "unit": "minute", "per_second": 0.5,
                                                   "per_day": None})
        self.assertEqual(lim.service_for("mainnet.helius-rpc.com"), "helius")

    def test_http_client_stops_before_calling(self):
        client = HttpClient(retries=0)
        client.limiter = self.ctx.limiter
        self.ctx.settings.set("rate_limits", {"coingecko": {"per_day": 1}})
        self.ctx.db.execute("INSERT INTO api_usage(service, day, calls) VALUES('coingecko', ?, 1)", (utc_day(),))
        with self.assertRaises(DailyLimitReached):
            client.get_json("https://api.coingecko.com/api/v3/ping")  # never reaches the network

    def test_settings_api_validation(self):
        from cryptoledger.server import ApiError, App
        app = App(self.ctx)
        with self.assertRaises(ApiError):
            app.dispatch("PUT", "/api/settings", {}, {"rate_limits": {"etherscan": {"per_second": "fast"}}})
        with self.assertRaises(ApiError):
            app.dispatch("PUT", "/api/settings", {}, {"rate_limits": {"nope": {"per_day": 1}}})
        with self.assertRaises(ApiError):
            app.dispatch("PUT", "/api/settings", {}, {"rate_limits": {"etherscan": {"per_day": "1.5"}}})
        app.dispatch("PUT", "/api/settings", {}, {"rate_limits": {"etherscan": {"per_second": "0,5", "per_day": ""}}})
        row = [r for r in app.dispatch("GET", "/api/limits", {}, None) if r["id"] == "etherscan"][0]
        self.assertEqual((row["per_second"], row["per_day"], row["default_per_day"]), (0.5, None, 100000))
        # Per-minute unit.
        app.dispatch("PUT", "/api/settings", {}, {"rate_limits": {"etherscan": {"rate": "120", "unit": "minute"}}})
        row = [r for r in app.dispatch("GET", "/api/limits", {}, None) if r["id"] == "etherscan"][0]
        self.assertEqual((row["rate"], row["unit"], row["per_second"]), (120.0, "minute", 2.0))
        with self.assertRaises(ApiError):
            app.dispatch("PUT", "/api/settings", {}, {"rate_limits": {"etherscan": {"rate": 1, "unit": "hour"}}})

    def test_per_minute_spacing(self):
        clock = FakeClock()
        lim = RateLimiter(self.ctx.db, self.ctx.settings, clock=clock.clock, sleep=clock.sleep)
        self.ctx.settings.set("rate_limits", {"solscan": {"rate": 30, "unit": "minute"}})
        for _ in range(3):
            lim.acquire("pro-api.solscan.io")
        self.assertEqual(clock.slept, [2.0, 2.0])     # 30 per minute -> one every 2 s

    def test_keys_are_redacted_in_errors(self):
        self.assertNotIn("SECRET", _redact("https://mainnet.helius-rpc.com/?api-key=SECRET"))


class SolanaHeliusTest(unittest.TestCase):
    def test_endpoint_priority(self):
        t = TempContext()
        try:
            adapter = get_adapter(t.ctx, t.ctx.networks.get("solana"))
            self.assertEqual(adapter._url(), "https://api.mainnet-beta.solana.com")
            t.ctx.secrets.set("helius_api_key", "abc-123")
            self.assertEqual(adapter._url(), "https://mainnet.helius-rpc.com/?api-key=abc-123")
            t.ctx.secrets.set("solana_rpc_url", "https://my.rpc.example")
            self.assertEqual(adapter._url(), "https://my.rpc.example")
        finally:
            t.close()


ME = "0x" + "1" * 40
OTHER = "0x" + "2" * 40
AXS = "0x97a9107c1793bc407d6f527b77e7fff4d812bece"


def moralis_handler(method, url, params, payload):
    assert params["chain"] == "0x7e4"
    if url.endswith(f"/{ME}"):
        return {"cursor": None, "result": [
            {"hash": "0xA1", "from_address": OTHER, "to_address": ME, "value": "5000000000000000000",
             "receipt_gas_used": "21000", "gas_price": "20000000000", "receipt_status": "1",
             "block_timestamp": "2025-03-01T10:00:00.000Z", "block_number": "100"},
            {"hash": "0xA2", "from_address": ME, "to_address": OTHER, "value": "1000000000000000000",
             "receipt_gas_used": "21000", "gas_price": "20000000000", "receipt_status": "1",
             "block_timestamp": "2025-04-01T10:00:00.000Z", "block_number": "200",
             "internal_transactions": [{"from": OTHER, "to": ME, "value": "200000000000000000"}]},
            {"hash": "0xA3", "from_address": ME, "to_address": OTHER, "value": "3000000000000000000",
             "receipt_gas_used": "30000", "gas_price": "20000000000", "receipt_status": "0",
             "block_timestamp": "2025-05-01T10:00:00.000Z", "block_number": "300"}]}
    if url.endswith(f"/{ME}/erc20/transfers"):
        return {"cursor": None, "result": [
            {"transaction_hash": "0xB1", "address": AXS, "from_address": OTHER, "to_address": ME, "value": "7000000000000000000",
             "token_symbol": "AXS", "token_name": "Axie Infinity Shard", "token_decimals": "18", "log_index": 4,
             "block_timestamp": "2025-06-01T10:00:00.000Z", "block_number": "400", "possible_spam": False},
            {"transaction_hash": "0xB2", "address": "0x" + "9" * 40, "from_address": OTHER, "to_address": ME,
             "value": "1", "token_symbol": "FREE", "token_name": "claim", "token_decimals": "0", "log_index": 1,
             "block_timestamp": "2025-06-02T10:00:00.000Z", "block_number": "410", "possible_spam": True}]}
    if url.endswith("/dateToBlock"):
        return {"block": 501, "timestamp": int(params["date"]) + 3}
    if url.endswith(f"/{ME}/balance"):
        return {"balance": "4198980000000000000" if params.get("to_block") == 500 else "4000000000000000000"}
    if url.endswith(f"/{ME}/erc20"):
        return [{"token_address": AXS, "symbol": "AXS", "name": "Axie Infinity Shard", "decimals": 18,
                 "balance": "7000000000000000000", "possible_spam": False}]
    return None


class MoralisRoninTest(unittest.TestCase):
    def setUp(self):
        self.t = TempContext(FakeHttp().on("https://deep-index.moralis.io/api/v2.2", moralis_handler))
        self.ctx = self.t.ctx
        self.ctx.secrets.set("moralis_api_key", "K")
        self.adapter = get_adapter(self.ctx, self.ctx.networks.get("ronin"))
        self.wallet = add_wallet(self.ctx, "ronin", ME)

    def tearDown(self):
        self.t.close()

    def test_ronin_prefix_address(self):
        self.assertEqual(self.adapter.validate_address("ronin:" + "A" * 40), "0x" + "a" * 40)

    def test_sync_balances_and_cutoff_block(self):
        res = self.adapter.sync(self.wallet, {}, LOG)
        by = {m.uid: m for m in res.movements}
        self.assertEqual(by["0xa1:in"].amount, Decimal("5"))
        self.assertEqual(by["0xa2:out"].amount, Decimal("-1"))
        self.assertEqual(by["0xa2:fee"].amount, Decimal("-0.00042"))
        self.assertEqual(by["0xa2:int:0:in"].amount, Decimal("0.2"))
        self.assertNotIn("0xa3:out", by)                      # failed: only the fee
        self.assertEqual(by["0xa3:fee"].amount, Decimal("-0.0006"))
        self.assertEqual(by["0xb1:log:4:in"].amount, Decimal("7"))
        statuses = {a.symbol: a.status for a in res.assets}
        self.assertEqual(statuses["FREE"], "spam")             # Moralis possible_spam
        self.assertEqual(res.state["last_block"], 410)
        ledger.store_sync_result(self.ctx, self.wallet, res)
        bal, _ = ledger.reconstruct(self.ctx, self.wallet["id"], 2_000_000_000)
        self.assertEqual(bal["ronin:native"], Decimal("4.19898"))  # 5 - 1 + 0.2 - fees (incl. failed tx)
        at = self.adapter.balance_at(self.wallet, 1767221999, [], LOG)
        self.assertEqual(at["ronin:native"], Decimal("4.19898"))   # closest block was after: used block 500
        self.assertEqual(at[f"ronin:{AXS}"], Decimal("7"))

    def test_missing_key(self):
        self.ctx.secrets.delete("moralis_api_key")
        with self.assertRaises(AdapterError):
            self.adapter.sync(self.wallet, {}, LOG)


SOLAR_ME = b58check_encode(bytes([63]) + bytes(range(20)))
SOLAR_OTHER = b58check_encode(bytes([63]) + bytes(range(1, 21)))
SOLAR_THIRD = b58check_encode(bytes([63]) + bytes(range(2, 22)))


def solar_tx(id_, ts, sender, **kw):
    tx = {"id": id_, "timestamp": {"epoch": 1, "unix": ts, "human": ""}, "sender": sender, "fee": "5000000",
          "amount": "0", "type": 6, "typeGroup": 1}
    tx.update(kw)
    return tx


def solar_handler(method, url, params, payload):
    if url.endswith(f"/wallets/{SOLAR_ME}/transactions"):
        return {"meta": {"next": None}, "data": [   # newest first
            solar_tx("t4", 1740400000, SOLAR_ME, type=2, typeGroup=2, asset={"votes": {"x": 100}}),
            solar_tx("t3", 1740300000, SOLAR_ME, type=0, typeGroup=2, amount="100000000"),
            solar_tx("t2", 1740200000, SOLAR_ME, asset={"transfers": [
                {"recipientId": SOLAR_OTHER, "amount": "200000000"}, {"recipientId": SOLAR_THIRD, "amount": "50000000"}]}),
            solar_tx("t1", 1740100000, SOLAR_OTHER, asset={"transfers": [{"recipientId": SOLAR_ME, "amount": "1000000000"}]}),
        ]}
    if url.endswith(f"/wallets/{SOLAR_ME}"):
        return {"data": {"address": SOLAR_ME, "balance": "635000000"}}
    return None


class SolarTest(unittest.TestCase):
    def test_sync(self):
        t = TempContext(FakeHttp().on("https://sxp.mainnet.sh/api", solar_handler))
        try:
            ctx = t.ctx
            adapter = get_adapter(ctx, ctx.networks.get("solar"))
            self.assertTrue(SOLAR_ME.startswith("S"))
            self.assertEqual(adapter.validate_address(SOLAR_ME), SOLAR_ME)
            with self.assertRaises(ValueError):
                adapter.validate_address(SOLAR_ME[:-1] + ("a" if SOLAR_ME[-1] != "a" else "b"))
            wallet = add_wallet(ctx, "solar", SOLAR_ME)
            res = adapter.sync(wallet, {}, LOG)
            by = {m.uid: m for m in res.movements}
            self.assertEqual(by["t1:in"].amount, Decimal("10"))
            self.assertEqual(by["t2:out"].amount, Decimal("-2.5"))       # two recipients
            self.assertEqual(by["t2:fee"].amount, Decimal("-0.05"))
            self.assertEqual(by["t3:burn"].amount, Decimal("-1"))
            self.assertEqual([m.kind for m in res.movements if m.tx_hash == "t4"], ["fee"])
            self.assertEqual(len(res.warnings), 1)                       # vote: fee only
            self.assertEqual(res.state["newest_id"], "t4")
            ledger.store_sync_result(ctx, wallet, res)
            bal, _ = ledger.reconstruct(ctx, wallet["id"], 2_000_000_000)
            live, _ = adapter.current_balances(wallet, [], LOG)
            self.assertEqual(bal["solar:native"], live["solar:native"])  # 10 - 2.5 - 1 - 3 x 0.05 = 6.35
            self.assertEqual(adapter.sync(wallet, res.state, LOG).movements, [])   # incremental
        finally:
            t.close()


if __name__ == "__main__":
    unittest.main()


class ServiceSyncTest(unittest.TestCase):
    def test_wallets_by_service_and_sync_one_service(self):
        from cryptoledger import sync
        from cryptoledger.server import ApiError, App
        t = TempContext(offline=True)
        try:
            ctx = t.ctx
            eth = add_wallet(ctx, "ethereum", "0x" + "a" * 40)
            base = add_wallet(ctx, "base", "0x" + "a" * 40)
            sol = add_wallet(ctx, "solana", "4Nd1mBQtrMJVYVfKf2PJy9NZUZdTAsp7D4xWLs4gDB4T")
            add_wallet(ctx, "manual", "", label="Cold")
            by = sync.wallets_by_service(ctx)
            self.assertEqual(sorted(by["etherscan"]), sorted([eth["id"], base["id"]]))
            self.assertEqual(by["solana_public"], [sol["id"]])
            ctx.secrets.set("solscan_api_key", "K")          # Solana now goes through Solscan
            self.assertEqual(sync.wallets_by_service(ctx)["solscan"], [sol["id"]])
            app = App(ctx)
            rows = {r["id"]: r for r in app.dispatch("GET", "/api/limits", {}, None)}
            self.assertEqual((rows["etherscan"]["wallets"], rows["etherscan"]["secret"]), (2, "etherscan_api_key"))
            self.assertEqual(rows["solscan"]["secret_source"], "file")
            self.assertTrue(rows["coingecko"]["price_service"])
            job = app.dispatch("POST", "/api/jobs/sync", {}, {"service": "etherscan"})
            self.assertEqual(job["kind"], "sync")
            for _ in range(100):
                if not app.jobs.current.public()["running"]:
                    break
                import time
                time.sleep(0.02)
            with self.assertRaises(ApiError):
                app.dispatch("POST", "/api/jobs/sync", {}, {"service": "trongrid"})   # no Tron wallet
        finally:
            t.close()
