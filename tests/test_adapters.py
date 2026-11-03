import unittest
from decimal import Decimal

from cryptoledger import ledger
from cryptoledger.adapters import AdapterError, get_adapter
from cryptoledger.adapters.tron import hex_to_base58

from helpers import FakeHttp, TempContext, add_wallet, fixture

ME = "0x1111111111111111111111111111111111111111"
LOG = lambda m: None  # noqa: E731


def etherscan_handler(method, url, params, payload):
    data = fixture("etherscan.json")
    if params.get("module") == "account" and params.get("action") in ("txlist", "txlistinternal", "tokentx"):
        assert params.get("chainid") == 1 and params.get("apikey") == "KEY"
        return data[params["action"]] if int(params.get("page", 1)) == 1 else data["empty"]
    if params.get("action") == "balance":
        return {"status": "1", "message": "OK", "result": "1597550000000000000"}
    if params.get("action") == "tokenbalance":
        return {"status": "1", "message": "OK", "result": "1500000000"}
    return None


class EvmAdapterTest(unittest.TestCase):
    def setUp(self):
        self.t = TempContext(FakeHttp().on("https://api.etherscan.io/v2/api", etherscan_handler))
        self.ctx = self.t.ctx
        self.ctx.secrets.set("etherscan_api_key", "KEY")
        self.net = self.ctx.networks.get("ethereum")
        self.adapter = get_adapter(self.ctx, self.net)
        self.wallet = add_wallet(self.ctx, "ethereum", ME)

    def tearDown(self):
        self.t.close()

    def test_address_validation(self):
        self.assertEqual(self.adapter.validate_address("  0xABCDEFabcdef0123456789012345678901234567 "),
                         "0xabcdefabcdef0123456789012345678901234567")
        with self.assertRaises(ValueError):
            self.adapter.validate_address("0x123")

    def test_movements(self):
        res = self.adapter.sync(self.wallet, {}, LOG)
        by_uid = {m.uid: m for m in res.movements}
        self.assertEqual(by_uid["0xaaa1:in"].amount, Decimal("2"))
        self.assertNotIn("0xaaa1:fee", by_uid)                       # fee paid by the sender
        self.assertEqual(by_uid["0xaaa2:out"].amount, Decimal("-0.5"))
        self.assertEqual(by_uid["0xaaa2:fee"].amount, Decimal("-0.0015"))  # 150000 gas * 10 gwei
        self.assertEqual(by_uid["0xaaa2:out"].info, "swapExactETHForTokens")
        self.assertEqual((by_uid["0xaaa2:out"].from_address, by_uid["0xaaa2:out"].to_address),
                         (ME, "0x7a250d5630b4cf539739df2c5dacb4c659f2488d"))
        self.assertEqual(by_uid["0xaaa1:in"].from_address, "0x9999999999999999999999999999999999999999")
        self.assertEqual(by_uid["0xaaa3:fee"].amount, Decimal("-0.00021"))
        self.assertTrue(by_uid["0xaaa3:fee"].failed)
        self.assertNotIn("0xaaa3:out", by_uid)                        # failed: value not sent
        self.assertEqual(by_uid["0xaaa4:int:0_1:in"].amount, Decimal("0.1"))
        usdc = [m for m in res.movements if m.asset_key.endswith("0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48")]
        self.assertEqual(usdc[0].amount, Decimal("1500"))
        spam = [m for m in res.movements if "0x4444" in m.asset_key][0]
        self.assertEqual((spam.amount, spam.info), (Decimal("0"), "zero-value transfer"))
        self.assertEqual(res.state["last_block"], 320)
        statuses = {a.symbol: a.status for a in res.assets}
        self.assertEqual(statuses["USDC"], "known")
        self.assertEqual(statuses["CLAIM"], "unmapped")

    def test_reconstruction_matches_live_balance(self):
        res = self.adapter.sync(self.wallet, {}, LOG)
        ledger.store_sync_result(self.ctx, self.wallet, res)
        balances, _ = ledger.reconstruct(self.ctx, self.wallet["id"], 2_000_000_000)
        # 2 - 0.5 - 0.0015 - 0.00021 + 0.1 = 1.59829 ETH
        self.assertEqual(balances["ethereum:native"], Decimal("1.59829"))
        # Re-sync is idempotent.
        self.assertEqual(ledger.store_sync_result(self.ctx, self.wallet, res), 0)
        live, _ = self.adapter.current_balances(self.wallet, ledger.known_asset_keys(self.ctx, self.wallet["id"]), LOG)
        self.assertEqual(live["ethereum:native"], Decimal("1.59755"))
        self.assertEqual(live["ethereum:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"], Decimal("1500"))

    def test_missing_key_is_a_clear_error(self):
        self.ctx.secrets.delete("etherscan_api_key")
        with self.assertRaises(AdapterError) as cm:
            self.adapter.sync(self.wallet, {}, LOG)
        self.assertIn("API key", str(cm.exception))


def solana_handler(method, url, params, payload):
    data = fixture("solana.json")
    m, p = payload["method"], payload["params"]
    if m == "getTokenAccountsByOwner":
        if p[1]["programId"].startswith("Tokenkeg"):
            return {"jsonrpc": "2.0", "id": 1, "result": data["getTokenAccountsByOwner"]}
        return {"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": []}}
    if m == "getSignaturesForAddress":
        key = "signatures_owner" if p[0] == data["owner"] else "signatures_token"
        return {"jsonrpc": "2.0", "id": 1, "result": data[key]}
    if m == "getTransaction":
        return {"jsonrpc": "2.0", "id": 1, "result": data["transactions"][p[0]]}
    if m == "getBalance":
        return {"jsonrpc": "2.0", "id": 1, "result": {"context": {"slot": 1}, "value": 1999990000}}
    return None


class SolanaAdapterTest(unittest.TestCase):
    def setUp(self):
        self.t = TempContext(FakeHttp().on("https://api.mainnet-beta.solana.com", solana_handler))
        self.ctx = self.t.ctx
        self.owner = fixture("solana.json")["owner"]
        self.wallet = add_wallet(self.ctx, "solana", self.owner)
        self.adapter = get_adapter(self.ctx, self.ctx.networks.get("solana"))

    def tearDown(self):
        self.t.close()

    def test_sync_parses_balances_from_pre_post(self):
        res = self.adapter.sync(self.wallet, {}, LOG)
        by_uid = {m.uid: m for m in res.movements}
        self.assertEqual(by_uid["sigA:sol"].amount, Decimal("3"))
        self.assertEqual(by_uid["sigA:sol"].balance_after, Decimal("3"))
        sender = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"   # paid the fee, so its change is -3 SOL - fee
        self.assertEqual((by_uid["sigA:sol"].from_address, by_uid["sigA:sol"].to_address), (sender, self.owner))
        self.assertEqual((by_uid["sigB:sol"].from_address, by_uid["sigB:sol"].to_address), (self.owner, sender))
        self.assertNotIn("sigA:fee", by_uid)                   # fee paid by the sender
        self.assertEqual(by_uid["sigF:fee"].amount, Decimal("-0.000005"))
        self.assertTrue(by_uid["sigF:fee"].failed)
        self.assertNotIn("sigF:sol", by_uid)
        self.assertEqual(by_uid["sigB:sol"].amount, Decimal("-1"))
        usdc = "solana:EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        self.assertEqual(by_uid[f"sigB:spl:{usdc.split(':')[1]}"].amount, Decimal("150"))
        self.assertEqual(by_uid[f"sigC:spl:{usdc.split(':')[1]}"].amount, Decimal("-125"))
        self.assertNotIn("sigC:sol", by_uid)                   # owner not in the account keys
        self.assertEqual(res.state["until"][self.owner], "sigB")
        ledger.store_sync_result(self.ctx, self.wallet, res)
        balances, anchored = ledger.reconstruct(self.ctx, self.wallet["id"], 2_000_000_000)
        self.assertEqual(balances["solana:native"], Decimal("1.99999"))
        self.assertEqual(balances[usdc], Decimal("25"))
        self.assertIn("solana:native", anchored)
        live, _ = self.adapter.current_balances(self.wallet, [], LOG)
        self.assertEqual(live["solana:native"], balances["solana:native"])
        self.assertEqual(live[usdc], balances[usdc])


TRON_ME = hex_to_base58("41" + "11" * 20)
TRON_OTHER = hex_to_base58("41" + "22" * 20)
USDT_TRC20 = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


def tron_handler(method, url, params, payload):
    if url.endswith("/transactions"):
        return {"success": True, "meta": {}, "data": [
            {"txID": "t1", "block_timestamp": 1735000000000, "ret": [{"contractRet": "SUCCESS", "fee": 0}],
             "raw_data": {"contract": [{"type": "TransferContract", "parameter": {"value": {
                 "amount": 50_000_000, "owner_address": "41" + "22" * 20, "to_address": "41" + "11" * 20}}}]}},
            {"txID": "t2", "block_timestamp": 1736000000000, "ret": [{"contractRet": "SUCCESS", "fee": 1_100_000}],
             "raw_data": {"contract": [{"type": "TransferContract", "parameter": {"value": {
                 "amount": 10_000_000, "owner_address": "41" + "11" * 20, "to_address": "41" + "22" * 20}}}]}},
            {"txID": "t3", "block_timestamp": 1737000000000, "ret": [{"contractRet": "SUCCESS", "fee": 0}],
             "energy_fee": 0, "net_fee": 345000,
             "raw_data": {"contract": [{"type": "FreezeBalanceV2Contract", "parameter": {"value": {
                 "frozen_balance": 20_000_000, "owner_address": "41" + "11" * 20}}}]}},
        ]}
    if url.endswith("/transactions/trc20"):
        return {"success": True, "meta": {}, "data": [
            {"transaction_id": "t4", "block_timestamp": 1738000000000, "from": TRON_OTHER, "to": TRON_ME,
             "type": "Transfer", "value": "250000000",
             "token_info": {"symbol": "USDT", "address": USDT_TRC20, "decimals": 6, "name": "Tether USD"}}]}
    if url.endswith(f"/v1/accounts/{TRON_ME}"):
        return {"success": True, "data": [{"balance": 18_555_000, "frozenV2": [{"amount": 20_000_000}, {"type": "ENERGY"}],
                                            "trc20": [{USDT_TRC20: "250000000"}]}]}
    return None


class TronAdapterTest(unittest.TestCase):
    def setUp(self):
        self.t = TempContext(FakeHttp().on("https://api.trongrid.io", tron_handler))
        self.ctx = self.t.ctx
        self.wallet = add_wallet(self.ctx, "tron", TRON_ME)
        self.adapter = get_adapter(self.ctx, self.ctx.networks.get("tron"))

    def tearDown(self):
        self.t.close()

    def test_validation(self):
        self.assertEqual(self.adapter.validate_address(USDT_TRC20), USDT_TRC20)
        with self.assertRaises(ValueError):
            self.adapter.validate_address("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6u")

    def test_sync_and_staked_trx_is_neutral(self):
        res = self.adapter.sync(self.wallet, {}, LOG)
        ledger.store_sync_result(self.ctx, self.wallet, res)
        balances, _ = ledger.reconstruct(self.ctx, self.wallet["id"], 2_000_000_000)
        # 50 in - 10 out - 1.1 fee - 0.345 freeze fee = 38.555 TRX (frozen TRX still owned)
        self.assertEqual(balances["tron:native"], Decimal("38.555"))
        self.assertEqual(balances[f"tron:{USDT_TRC20}"], Decimal("250"))
        self.assertEqual(res.state["last_ts_ms"], 1738000000000)
        live, _ = self.adapter.current_balances(self.wallet, [], LOG)
        self.assertEqual(live["tron:native"], Decimal("38.555"))   # 18.555 liquid + 20 staked
        self.assertEqual(live[f"tron:{USDT_TRC20}"], Decimal("250"))


BTC_ME = "bc1qme0000000000000000000000000000000000000"


def btc_handler(method, url, params, payload):
    if url.endswith(f"/address/{BTC_ME}/txs"):
        return [
            {"txid": "spend", "fee": 1000, "status": {"confirmed": True, "block_time": 1736000000},
             "vin": [{"prevout": {"scriptpubkey_address": BTC_ME, "value": 5_000_000}}],
             "vout": [{"scriptpubkey_address": "bc1qshop", "value": 1_000_000},
                      {"scriptpubkey_address": BTC_ME, "value": 3_999_000}]},
            {"txid": "recv", "fee": 500, "status": {"confirmed": True, "block_time": 1730000000},
             "vin": [{"prevout": {"scriptpubkey_address": "bc1qother", "value": 6_000_000}}],
             "vout": [{"scriptpubkey_address": BTC_ME, "value": 5_000_000},
                      {"scriptpubkey_address": "bc1qother", "value": 999_500}]},
            {"txid": "mempool", "fee": 300, "status": {"confirmed": False},
             "vin": [], "vout": [{"scriptpubkey_address": BTC_ME, "value": 1}]},
        ]
    if url.endswith(f"/address/{BTC_ME}"):
        return {"chain_stats": {"funded_txo_sum": 8_999_000, "spent_txo_sum": 5_000_000}}
    return None


class BitcoinAdapterTest(unittest.TestCase):
    def test_sync(self):
        t = TempContext(FakeHttp().on("https://mempool.space/api", btc_handler))
        try:
            ctx = t.ctx
            wallet = add_wallet(ctx, "bitcoin", BTC_ME)
            adapter = get_adapter(ctx, ctx.networks.get("bitcoin"))
            res = adapter.sync(wallet, {}, LOG)
            by_uid = {m.uid: m for m in res.movements}
            self.assertEqual(by_uid["recv:btc"].amount, Decimal("0.05"))
            self.assertEqual(by_uid["spend:fee"].amount, Decimal("-0.00001"))
            self.assertEqual(by_uid["spend:btc"].amount, Decimal("-0.01"))
            self.assertEqual(by_uid["spend:btc"].counterparty, "bc1qshop")
            self.assertNotIn("mempool:btc", by_uid)
            self.assertEqual(res.state["newest_txid"], "spend")
            ledger.store_sync_result(ctx, wallet, res)
            bal, _ = ledger.reconstruct(ctx, wallet["id"], 2_000_000_000)
            live, _ = adapter.current_balances(wallet, [], LOG)
            self.assertEqual(bal["bitcoin:native"], live["bitcoin:native"])
            # Incremental: stops at the newest known tx.
            again = adapter.sync(wallet, res.state, LOG)
            self.assertEqual(again.movements, [])
        finally:
            t.close()


def binance_handler(method, url, params, payload):
    assert "signature" in params or url.endswith("/api/v3/time")
    if url.endswith("/api/v3/time"):
        return {"serverTime": 1_700_000_000_000}
    if url.endswith("/sapi/v1/accountSnapshot"):
        return {"code": 200, "msg": "", "snapshotVos": [
            {"type": "spot", "updateTime": 1767225599000,
             "data": {"totalAssetOfBtc": "0.1", "balances": [{"asset": "BTC", "free": "0.09", "locked": "0.01"},
                                                              {"asset": "EUR", "free": "12.5", "locked": "0"}]}}]}
    if url.endswith("/sapi/v1/capital/deposit/hisrec"):
        if int(params["startTime"]) < 1_700_000_000_000:
            return []
        return [{"id": "d1", "amount": "0.5", "coin": "ETH", "network": "ETH", "status": 1, "address": "0xdep",
                 "txId": "0xABC", "insertTime": 1_760_000_000_000}]
    if url.endswith("/sapi/v1/capital/withdraw/history"):
        return []
    if url.endswith("/sapi/v1/simple-earn/flexible/position"):
        return {"rows": [{"asset": "USDC", "totalAmount": "100"}], "total": 1}
    if url.endswith("/sapi/v1/simple-earn/locked/position"):
        return {"rows": [], "total": 0}
    if url.endswith("/api/v3/account"):
        return {"balances": [{"asset": "BTC", "free": "0.1", "locked": "0"}, {"asset": "LDUSDC", "free": "100", "locked": "0"}]}
    if url.endswith("/sapi/v1/asset/get-funding-asset"):
        return [{"asset": "EUR", "free": "20", "locked": "0", "freeze": "0", "withdrawing": "0"}]
    return None


class BinanceAdapterTest(unittest.TestCase):
    def test_sync_and_balances(self):
        t = TempContext(FakeHttp().on("https://api.binance.com", binance_handler))
        try:
            ctx = t.ctx
            ctx.settings.set("first_fiscal_year", 2025)
            wallet = add_wallet(ctx, "binance", "")
            ctx.secrets.set(f"wallet:{wallet['id']}:api_key", "k")
            ctx.secrets.set(f"wallet:{wallet['id']}:api_secret", "s")
            adapter = get_adapter(ctx, ctx.networks.get("binance"))
            res = adapter.sync(wallet, {}, LOG)
            self.assertEqual(len(res.snapshots), 2)
            self.assertEqual(res.snapshots[0].taken_at, 1767225599)  # 2025-12-31 23:59:59 UTC
            dep = res.movements[0]
            self.assertEqual((dep.asset_key, dep.amount, dep.tx_hash), ("binance:ETH", Decimal("0.5"), "0xabc"))
            self.assertEqual((dep.from_address, dep.to_address, dep.counterparty), ("", "0xdep", ""))
            live, infos = adapter.current_balances(wallet, [], LOG)
            self.assertEqual(live, {"binance:USDC": Decimal("100"), "binance:BTC": Decimal("0.1"),
                                    "binance:EUR": Decimal("20")})  # LDUSDC not double-counted
            self.assertEqual({i.key: i.fiat for i in infos}["binance:EUR"], "EUR")
        finally:
            t.close()

    def test_missing_credentials(self):
        t = TempContext()
        try:
            wallet = add_wallet(t.ctx, "binance", "")
            with self.assertRaises(AdapterError):
                get_adapter(t.ctx, t.ctx.networks.get("binance")).current_balances(wallet, [], LOG)
        finally:
            t.close()


if __name__ == "__main__":
    unittest.main()
