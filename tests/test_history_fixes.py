"""History fixes: nothing is skipped for good. A transaction the provider does not return, a token
without decimals or a reward amount that cannot be read is retried at the next sync; every list
of one sync ends at the same point; paged results are read to the end."""
import copy
import unittest
from decimal import Decimal

from crumenae.adapters import get_adapter
from crumenae.http import HttpError

from helpers import FakeHttp, TempContext, add_wallet, fixture
from test_adapters import ME, TRON_ME, binance_handler, etherscan_handler, solana_handler, tron_handler
from test_providers_stellar import ME as XLM_ME, horizon_handler

LOG = lambda m: None  # noqa: E731


class SolanaTest(unittest.TestCase):
    def setUp(self):
        self.missing = {"sigB"}
        self.calls = []

        def handler(method, url, params, payload):
            if payload["method"] == "getTransaction":
                self.calls.append(payload["params"][0])
                if payload["params"][0] in self.missing:
                    return {"jsonrpc": "2.0", "id": 1, "result": None}
            return solana_handler(method, url, params, payload)
        self.t = TempContext(FakeHttp().on("https://api.mainnet-beta.solana.com", handler))
        self.ctx = self.t.ctx
        self.wallet = add_wallet(self.ctx, "solana", fixture("solana.json")["owner"])
        self.adapter = get_adapter(self.ctx, self.ctx.networks.get("solana"))

    def tearDown(self):
        self.t.close()

    def test_missing_transaction_is_asked_again(self):
        first = self.adapter.sync(self.wallet, {}, LOG)
        self.assertNotIn("sigB:sol", {m.uid for m in first.movements})
        self.assertEqual([r["signature"] for r in first.state["retry"]], ["sigB"])
        self.assertTrue(any("asked again" in w for w in first.warnings))
        self.missing.clear()
        self.calls.clear()
        again = self.adapter.sync(self.wallet, first.state, LOG)
        self.assertIn("sigB:sol", {m.uid for m in again.movements})
        self.assertEqual(again.state["retry"], [])

    def test_same_slot_older_transaction_first(self):
        self.missing.clear()
        data = fixture("solana.json")
        # Two transactions in the same slot: the list gives the newer one first.
        sigs = [dict(s, slot=200, blockTime=1735000000) for s in data["signatures_owner"][:2]]
        order = [s["signature"] for s in sigs]
        self.adapter._signatures = lambda account, until: sigs if account == data["owner"] else []
        self.adapter.sync(self.wallet, {}, LOG)
        self.assertEqual(self.calls, list(reversed(order)))


class EtherscanTest(unittest.TestCase):
    def setUp(self):
        self.queries = []
        self.no_decimals = True

        def handler(method, url, params, payload):
            if params.get("action") == "eth_blockNumber":
                return {"jsonrpc": "2.0", "id": 1, "result": hex(400)}
            if params.get("action") == "eth_call":
                return {"jsonrpc": "2.0", "id": 1, "result": "0x" if self.no_decimals else hex(6)}
            if params.get("action") in ("txlist", "txlistinternal", "tokentx"):
                self.queries.append(params)
                if params["action"] == "tokentx" and int(params.get("page", 1)) == 1:
                    data = copy.deepcopy(fixture("etherscan.json")["tokentx"])
                    data["result"][0]["tokenDecimal"] = ""          # listed without decimals (USDC-like)
                    data["result"][0]["contractAddress"] = "0x" + "5" * 40
                    return data
            return etherscan_handler(method, url, params, payload)
        self.t = TempContext(FakeHttp().on("https://api.etherscan.io/v2/api", handler))
        self.ctx = self.t.ctx
        self.ctx.secrets.set("etherscan_api_key", "KEY")
        self.adapter = get_adapter(self.ctx, self.ctx.networks.get("ethereum"))
        self.wallet = add_wallet(self.ctx, "ethereum", ME)

    def tearDown(self):
        self.t.close()

    def test_all_lists_end_at_the_same_block(self):
        self.no_decimals = False
        res = self.adapter.sync(self.wallet, {}, LOG)
        self.assertEqual({q["endblock"] for q in self.queries}, {400})
        self.assertEqual(res.state["last_block"], 400)

    def test_token_without_decimals_is_retried_not_guessed(self):
        res = self.adapter.sync(self.wallet, {}, LOG)
        token = "ethereum:0x" + "5" * 40
        self.assertNotIn(token, {m.asset_key for m in res.movements})   # not stored as raw units
        self.assertEqual(res.state["last_block"], 200)                    # read again from that block
        self.assertTrue(any("decimals unknown" in w for w in res.warnings))
        self.no_decimals = False
        again = self.adapter.sync(self.wallet, res.state, LOG)
        amounts = [m.amount for m in again.movements if m.asset_key == token]
        self.assertEqual(amounts, [Decimal("1500")])                      # decimals() of the contract: 6


class TronTest(unittest.TestCase):
    def setUp(self):
        self.params = []
        self.info_fails = True

        def handler(method, url, params, payload):
            if url.endswith("/wallet/gettransactioninfobyid"):
                if self.info_fails:
                    raise HttpError(503, "busy", url)
                return {"id": payload["value"], "withdraw_amount": 3_000_000}
            if url.endswith("/transactions") and not url.endswith("/trc20/transactions"):
                self.params.append(params)
                data = tron_handler(method, url, params, payload)
                claim = {"txID": "c2", "block_timestamp": 1740000000000, "ret": [{"contractRet": "SUCCESS", "fee": 0}],
                         "raw_data": {"contract": [{"type": "WithdrawBalanceContract", "parameter": {
                             "value": {"owner_address": "41" + "11" * 20}}}]}}
                return dict(data, data=data["data"] + [claim])
            if url.endswith("/trc20/transactions"):
                self.params.append(params)
            return tron_handler(method, url, params, payload)
        self.t = TempContext(FakeHttp().on("https://api.trongrid.io", handler))
        self.ctx = self.t.ctx
        self.adapter = get_adapter(self.ctx, self.ctx.networks.get("tron"))
        self.wallet = add_wallet(self.ctx, "tron", TRON_ME)

    def tearDown(self):
        self.t.close()

    def test_both_lists_share_the_end_and_unread_claims_are_retried(self):
        res = self.adapter.sync(self.wallet, {}, LOG)
        self.assertEqual(len({p["max_timestamp"] for p in self.params}), 1)
        self.assertNotIn("c2:reward", {m.uid for m in res.movements})
        self.assertEqual(res.state["last_ts_ms"], 1740000000000)          # read again from the claim
        self.info_fails = False
        again = self.adapter.sync(self.wallet, res.state, LOG)
        rewards = [m.amount for m in again.movements if m.uid == "c2:reward"]
        self.assertEqual(rewards, [Decimal(3)])


class StellarTest(unittest.TestCase):
    def test_failed_transaction_fee_is_counted(self):
        def handler(method, url, params, payload):
            if url.endswith(f"/accounts/{XLM_ME}/operations"):
                assert params.get("include_failed") == "true"
                data = horizon_handler(method, url, params, payload)
                failed = {"id": "0000000009", "paging_token": "9", "type": "payment", "from": XLM_ME, "to": "X",
                          "transaction_successful": False, "transaction_hash": "dd" * 32,
                          "transaction": {"hash": "dd" * 32, "source_account": XLM_ME, "fee_charged": "100",
                                          "successful": False, "created_at": "2025-05-01T00:00:00Z"}}
                return {"_embedded": {"records": data["_embedded"]["records"] + [failed]}}
            return horizon_handler(method, url, params, payload)
        t = TempContext(FakeHttp().on("https://horizon.stellar.org", handler))
        try:
            w = add_wallet(t.ctx, "stellar", XLM_ME)
            res = get_adapter(t.ctx, t.ctx.networks.get("stellar")).sync(w, {}, LOG)
            fee = next(m for m in res.movements if m.uid == "dd" * 32 + ":fee")
            self.assertEqual((fee.amount, fee.failed), (Decimal("-0.00001"), True))
        finally:
            t.close()


class BinanceTest(unittest.TestCase):
    def test_more_than_1000_deposits_in_a_window(self):
        offsets = []

        def handler(method, url, params, payload):
            if url.endswith("/sapi/v1/capital/deposit/hisrec"):
                offset = int(params.get("offset") or 0)
                offsets.append(offset)
                if not int(params["startTime"]) <= 1_690_000_000_000 < int(params["endTime"]) or offset > 1000:
                    return []
                n = 1000 if offset == 0 else 5
                return [{"id": f"d{offset + i}", "coin": "BTC", "amount": "0.001", "status": 1,
                         "insertTime": 1_690_000_000_000 + i, "txId": f"t{offset + i}"} for i in range(n)]
            return binance_handler(method, url, params, payload)
        t = TempContext(FakeHttp().on("https://api.binance.com", handler))
        try:
            w = add_wallet(t.ctx, "binance", "", label="Binance")
            t.ctx.secrets.set(f"wallet:{w['id']}:api_key", "k")
            t.ctx.secrets.set(f"wallet:{w['id']}:api_secret", "s")
            res = get_adapter(t.ctx, t.ctx.networks.get("binance")).sync(t.ctx.wallet(w["id"]), {}, LOG)
            deposits = [m for m in res.movements if m.uid.startswith("dep:")]
            self.assertEqual(len(deposits), 1005)
            self.assertIn(1000, offsets)
        finally:
            t.close()


if __name__ == "__main__":
    unittest.main()
