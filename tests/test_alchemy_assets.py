import unittest
from decimal import Decimal

from cryptoledger import ledger, sync, views
from cryptoledger.adapters import get_adapter
from cryptoledger.models import Movement, SyncResult

from helpers import FakeHttp, TempContext, add_wallet

LOG = lambda m: None  # noqa: E731
ME = "0x" + "ab" * 20
USDT = "0x55d398326f99059ff775485246999027b3197955"
CAKE = "0x" + "cd" * 20   # not in the built-in list: metadata is read
KEY = "b" * 32


def alchemy_handler(method, url, params, payload):
    assert url == f"https://bnb-mainnet.g.alchemy.com/v2/{KEY}", url
    m, p = payload["method"], payload["params"]
    ok = lambda r: {"jsonrpc": "2.0", "id": 1, "result": r}  # noqa: E731
    if m == "alchemy_getAssetTransfers":
        q = p[0]
        assert q["category"] == ["external", "erc20"] and q["withMetadata"] is True
        if q.get("toAddress") == ME:
            return ok({"transfers": [
                {"blockNum": "0x10", "uniqueId": "0xaaa:external", "hash": "0xAAA", "from": "0x" + "11" * 20,
                 "to": ME, "value": 2, "asset": "BNB", "category": "external",
                 "rawContract": {"value": hex(2 * 10 ** 18), "address": None, "decimal": "0x12"},
                 "metadata": {"blockTimestamp": "2025-01-01T00:00:00.000Z"}},
                {"blockNum": "0x11", "uniqueId": "0xbbb:log:0x3", "hash": "0xBBB", "from": "0x" + "22" * 20,
                 "to": ME, "value": 5, "asset": "USDT", "category": "erc20",
                 "rawContract": {"value": hex(5 * 10 ** 18), "address": USDT, "decimal": "0x12"},
                 "metadata": {"blockTimestamp": "2025-01-02T00:00:00.000Z"}},
                {"blockNum": "0x12", "uniqueId": "0xnft:log:0x1", "hash": "0xNFT", "from": "0x" + "22" * 20,
                 "to": ME, "category": "erc721", "rawContract": {"address": "0x" + "77" * 20},
                 "metadata": {"blockTimestamp": "2025-01-02T00:00:00.000Z"}}]})
        return ok({"transfers": [
            {"blockNum": "0x13", "uniqueId": "0xccc:external", "hash": "0xCCC", "from": ME, "to": "0x" + "33" * 20,
             "value": 0.5, "asset": "BNB", "category": "external",
             "rawContract": {"value": hex(5 * 10 ** 17), "address": None, "decimal": "0x12"},
             "metadata": {"blockTimestamp": "2025-01-03T00:00:00.000Z"}}]})
    if m == "eth_getTransactionReceipt":
        return ok({"from": ME, "to": "0x" + "33" * 20, "gasUsed": hex(21000), "effectiveGasPrice": hex(10 ** 9),
                   "status": "0x1"})
    if m == "alchemy_getTokenBalances":
        assert p[:2] == [ME, "erc20"]
        return ok({"address": ME, "tokenBalances": [
            {"contractAddress": USDT, "tokenBalance": hex(7 * 10 ** 18)},
            {"contractAddress": CAKE, "tokenBalance": hex(3 * 10 ** 18)},
            {"contractAddress": "0x" + "88" * 20, "tokenBalance": "0x0"}]})
    if m == "alchemy_getTokenMetadata":
        assert p == [CAKE]
        return ok({"name": "PancakeSwap Token", "symbol": "Cake", "decimals": 18})
    if m == "eth_getBalance":
        return ok(hex(15 * 10 ** 17))
    return None


class AlchemyBscTest(unittest.TestCase):
    def setUp(self):
        self.t = TempContext(FakeHttp().on("https://bnb-mainnet.g.alchemy.com", alchemy_handler))
        self.ctx = self.t.ctx
        self.ctx.secrets.set("alchemy_api_key", KEY)
        self.wallet = add_wallet(self.ctx, "bsc", ME)
        self.adapter = get_adapter(self.ctx, self.ctx.networks.get("bsc"))

    def tearDown(self):
        self.t.close()

    def test_sync(self):
        self.assertEqual(self.adapter.provider(), "alchemy")
        res = self.adapter.sync(self.wallet, {}, LOG)
        got = sorted((m.uid, m.asset_key, m.amount, m.ts) for m in res.movements)
        self.assertEqual(got, sorted([
            ("0xaaa:in", "bsc:native", Decimal(2), 1735689600),
            ("0xbbb:log:3:in", f"bsc:{USDT}", Decimal(5), 1735776000),
            ("0xccc:out", "bsc:native", Decimal("-0.5"), 1735862400),
            ("0xccc:fee", "bsc:native", Decimal("-0.000021"), 1735862400),
        ]))
        self.assertEqual(res.state["last_block"], 0x13)
        from cryptoledger import sync as sync_mod
        self.assertEqual(sync_mod.wallet_service(self.ctx, self.wallet), "alchemy")

    def test_key_redacted_in_errors(self):
        from cryptoledger.http import _redact
        self.assertEqual(_redact(f"https://bnb-mainnet.g.alchemy.com/v2/{KEY}"), "https://bnb-mainnet.g.alchemy.com/v2/***")

    def test_live_balances_find_every_token(self):
        bal, infos = self.adapter.current_balances(self.wallet, [], LOG)
        self.assertEqual(bal, {"bsc:native": Decimal("1.5"), f"bsc:{USDT}": Decimal(7), f"bsc:{CAKE}": Decimal(3)})
        self.assertEqual({i.key: i.symbol for i in infos}[f"bsc:{CAKE}"], "Cake")


class LoadAssetsTest(unittest.TestCase):
    def test_reads_balances_not_history(self):
        t = TempContext(FakeHttp().on("https://bnb-mainnet.g.alchemy.com", alchemy_handler))
        try:
            ctx = t.ctx
            ctx.secrets.set("alchemy_api_key", KEY)
            add_wallet(ctx, "bsc", ME)
            add_wallet(ctx, "manual", "", label="Cold")
            out = sync.load_assets(ctx, LOG)
            methods = [c[3]["method"] for c in t.http.calls if c[3]]
            self.assertNotIn("alchemy_getAssetTransfers", methods)
            self.assertIn("alchemy_getTokenBalances", methods)
            keys = {r["key"] for r in ctx.db.query("SELECT key FROM assets")}
            self.assertTrue({"bsc:native", f"bsc:{USDT}", f"bsc:{CAKE}"} <= keys)
            self.assertEqual(out["errors"], 0)
            self.assertEqual(ctx.db.scalar("SELECT COUNT(*) FROM movements"), 0)
        finally:
            t.close()


class SpamHiddenTest(unittest.TestCase):
    def test_default_hides_spam(self):
        t = TempContext(offline=True)
        try:
            ctx = t.ctx
            w = add_wallet(ctx, "ethereum", "0x" + "a" * 40)
            ledger.store_sync_result(ctx, w, SyncResult(movements=[
                Movement("m1", "0xh1", 1, "ethereum:native", Decimal("1"), "in"),
                Movement("m2", "0xh2", 2, "ethereum:native", Decimal("0"), "in")]))   # zero value: spam
            from cryptoledger import classify
            classify.run(ctx)
            self.assertEqual(views.transactions(ctx)["total"], 1)
            self.assertEqual(views.transactions(ctx, category="all")["total"], 2)
            self.assertEqual(views.transactions(ctx, category="spam")["total"], 1)
        finally:
            t.close()



class BinanceEarnPositionsTest(unittest.TestCase):
    def test_earn_positions_on_staking_page(self):
        from test_adapters import binance_handler
        t = TempContext(FakeHttp().on("https://api.binance.com", binance_handler))
        try:
            ctx = t.ctx
            w = add_wallet(ctx, "binance", "", label="Binance")
            ctx.secrets.set(f"wallet:{w['id']}:api_key", "k")
            ctx.secrets.set(f"wallet:{w['id']}:api_secret", "s")
            get_adapter(ctx, ctx.networks.get("binance")).current_balances(w, [], LOG)
            pos = views.staking(ctx, 2025)["positions"]
            self.assertEqual(len(pos), 1)
            self.assertEqual(pos[0]["auto"], "Binance Simple Earn")
            self.assertEqual(pos[0]["current"], [{"asset_key": "binance:USDC", "symbol": "USDC", "amount": "100"}])
        finally:
            t.close()


if __name__ == "__main__":
    unittest.main()
