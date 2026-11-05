import unittest
from decimal import Decimal

from cryptoledger import classify, ledger
from cryptoledger.adapters import get_adapter
from cryptoledger.http import _redact

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


ME = "0x" + "ab" * 20
USDT = "0x55d398326f99059ff775485246999027b3197955"
KEY = "a" * 32


def nodereal_handler(method, url, params, payload):
    assert url == f"https://bsc-mainnet.nodereal.io/v1/{KEY}", url
    m, p = payload["method"], payload["params"]
    if m == "nr_getAssetTransfers":
        q = p[0]
        assert q["category"] == ["external", "internal", "20"]
        if q.get("toAddress") == ME:
            if not q.get("pageKey"):
                return {"jsonrpc": "2.0", "id": 1, "result": {"pageKey": "page-2", "transfers": [
                    {"category": "external", "blockNum": "0x10", "from": "0x" + "11" * 20, "to": ME,
                     "value": hex(2 * 10 ** 18), "asset": "BNB", "hash": "0xAAA", "blockTimeStamp": 1_735_700_000}]}}
            return {"jsonrpc": "2.0", "id": 1, "result": {"transfers": [
                {"category": "20", "blockNum": "0x11", "from": "0x" + "22" * 20, "to": ME,
                 "value": "0x" + "0" * 40 + hex(5 * 10 ** 18)[2:].rjust(24, "0"), "asset": "USDT",
                 "hash": "0xBBB", "blockTimeStamp": 1_735_800_000, "contractAddress": USDT, "decimal": "18"}]}}
        if q.get("fromAddress") == ME:
            return {"jsonrpc": "2.0", "id": 1, "result": {"transfers": [
                {"category": "external", "blockNum": "0x12", "from": ME, "to": "0x" + "33" * 20,
                 "value": hex(5 * 10 ** 17), "asset": "BNB", "hash": "0xCCC", "blockTimeStamp": 1_735_900_000},
                {"category": "20", "blockNum": "0x13", "from": ME, "to": "0x" + "44" * 20,
                 "value": hex(10 ** 18), "asset": "XYZ", "hash": "0xDDD", "blockTimeStamp": 1_736_000_000,
                 "contractAddress": "0x" + "55" * 20}]}}   # no "decimal": read from the contract
    if m == "eth_call" and p[0]["data"] == "0x313ce567":
        return {"jsonrpc": "2.0", "id": 1, "result": hex(9)}
    if m == "eth_getTransactionReceipt":
        frm = ME if p[0] in ("0xccc", "0xddd") else "0x" + "99" * 20
        return {"jsonrpc": "2.0", "id": 1, "result": {"from": frm, "to": "0x" + "33" * 20, "gasUsed": hex(21000),
                                                      "effectiveGasPrice": hex(3 * 10 ** 9), "status": "0x1"}}
    if m == "eth_getBalance":
        return {"jsonrpc": "2.0", "id": 1, "result": hex(15 * 10 ** 17)}
    if m == "eth_call":
        return {"jsonrpc": "2.0", "id": 1, "result": hex(5 * 10 ** 18)}
    return None


class NodeRealTest(unittest.TestCase):
    def setUp(self):
        self.t = TempContext(FakeHttp().on("https://bsc-mainnet.nodereal.io", nodereal_handler))
        self.ctx = self.t.ctx
        self.ctx.secrets.set("nodereal_api_key", KEY)
        self.wallet = add_wallet(self.ctx, "bsc", ME)
        self.adapter = get_adapter(self.ctx, self.ctx.networks.get("bsc"))

    def tearDown(self):
        self.t.close()

    def test_sync(self):
        res = self.adapter.sync(self.wallet, {}, LOG)
        got = sorted((m.uid, m.asset_key, m.amount) for m in res.movements)
        fee = Decimal(21000 * 3 * 10 ** 9) / Decimal(10 ** 18)
        self.assertEqual(got, sorted([
            ("0xaaa:in", "bsc:native", Decimal(2)),
            (f"0xbbb:tok:{USDT}:{'0x' + '22' * 20}:{ME}:{5 * 10 ** 18}:0:in", f"bsc:{USDT}", Decimal(5)),
            ("0xccc:out", "bsc:native", Decimal("-0.5")),
            ("0xccc:fee", "bsc:native", -fee),
            (f"0xddd:tok:{'0x' + '55' * 20}:{ME}:{'0x' + '44' * 20}:{10 ** 18}:0:out", "bsc:0x" + "55" * 20,
             Decimal("-1000000000")),   # 1e18 raw / 1e9 (decimals read from the contract)
            ("0xddd:fee", "bsc:native", -fee),
        ]))
        self.assertEqual(res.state["last_block"], 0x13)
        bal, _ = self.adapter.current_balances(self.wallet, [f"bsc:{USDT}"], LOG)
        self.assertEqual(bal["bsc:native"], Decimal("1.5"))

    def test_missing_key_and_redaction(self):
        self.ctx.secrets.delete("nodereal_api_key")
        from cryptoledger.adapters.base import AdapterError
        with self.assertRaises(AdapterError):
            self.adapter.sync(self.wallet, {}, LOG)
        self.assertEqual(_redact(f"https://bsc-mainnet.nodereal.io/v1/{KEY}"), "https://bsc-mainnet.nodereal.io/v1/***")

    def test_service_mapping(self):
        from cryptoledger import sync
        self.assertEqual(sync.wallet_service(self.ctx, self.wallet), "nodereal")


if __name__ == "__main__":
    unittest.main()
