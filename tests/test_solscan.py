"""Solscan as Solana source: same scenario as the RPC fixture (tests/fixtures/solana.json),
same movements out, so switching provider never changes or duplicates the ledger."""
import unittest
from decimal import Decimal

from cryptoledger import ledger
from cryptoledger.adapters import AdapterError, get_adapter
from cryptoledger.http import HttpError

from helpers import FakeHttp, TempContext, add_wallet, fixture
from test_adapters import solana_handler

OWNER = fixture("solana.json")["owner"]
SENDER = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
ATA = "7UX2i7SucgLMQcfZ75s3VXmZZY4YRUyJN9X1RgfMoDUi"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL = "So11111111111111111111111111111111111111111"


def change(sig, ts, mint, account, pre, post, dec):
    return {"block_id": 1, "block_time": ts, "trans_id": sig, "address": OWNER, "token_address": mint,
            "token_account": account, "token_decimals": dec, "amount": abs(post - pre),
            "pre_balance": pre, "post_balance": post, "change_type": "inc" if post > pre else "dec"}


BALANCE_CHANGES = [
    change("sigA", 1730000000, SOL, OWNER, 0, 3000000000, 9),
    change("sigF", 1732000000, SOL, OWNER, 3000000000, 2999995000, 9),
    change("sigB", 1735000000, SOL, OWNER, 2999995000, 1999990000, 9),
    change("sigB", 1735000000, USDC, ATA, 0, 150000000, 6),
    change("sigC", 1736000000, USDC, ATA, 150000000, 25000000, 6),
]
TRANSACTIONS = [  # newest first
    {"tx_hash": "sigC", "block_time": 1736000000, "fee": 5000, "signer": [SENDER], "status": "Success"},
    {"tx_hash": "sigB", "block_time": 1735000000, "fee": 5000, "signer": [OWNER], "status": "Success"},
    {"tx_hash": "sigF", "block_time": 1732000000, "fee": 5000, "signer": [OWNER], "status": "Fail"},
    {"tx_hash": "sigA", "block_time": 1730000000, "fee": 5000, "signer": [SENDER], "status": "Success"},
]
TRANSFERS = [
    {"trans_id": "sigA", "block_time": 1730000000, "from_address": SENDER, "to_address": OWNER,
     "token_address": SOL, "amount": 3000000000, "token_decimals": 9, "flow": "in"},
    {"trans_id": "sigB", "block_time": 1735000000, "from_address": OWNER, "to_address": SENDER,
     "token_address": SOL, "amount": 1000000000, "token_decimals": 9, "flow": "out"},
]


def solscan_handler(calls):
    def handle(method, url, params, payload):
        calls.append((url.rsplit("/v2.0", 1)[1], params))
        path = url.rsplit("/v2.0", 1)[1]
        if path == "/account/balance_change":
            rows = [r for r in BALANCE_CHANGES if r["block_time"] >= int(params.get("from_time") or 0)]
            return {"success": True, "data": rows if int(params["page"]) == 1 else []}
        if path == "/account/transactions":
            return {"success": True, "data": TRANSACTIONS}
        if path == "/account/transfer":
            return {"success": True, "data": TRANSFERS if int(params["page"]) == 1 else []}
        if path == "/account/detail":
            return {"success": True, "data": {"account": OWNER, "lamports": 1999990000}}
        if path == "/account/token-accounts":
            return {"success": True, "data": [{"token_account": ATA, "token_address": USDC, "amount": 25000000,
                                               "token_decimals": 6, "owner": OWNER}]}
        return None
    return handle


class SolscanTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        http = (FakeHttp().on("https://pro-api.solscan.io/v2.0", solscan_handler(self.calls))
                .on("https://api.mainnet-beta.solana.com", solana_handler))
        self.t = TempContext(http)
        self.ctx = self.t.ctx
        self.adapter = get_adapter(self.ctx, self.ctx.networks.get("solana"))

    def tearDown(self):
        self.t.close()

    def summary(self, moves):
        return {m.uid: (m.amount, m.kind, m.balance_after) for m in moves}

    def test_same_movements_as_rpc(self):
        rpc = self.adapter.sync(add_wallet(self.ctx, "solana", OWNER, label="rpc"), {}, lambda m: None)
        self.ctx.secrets.set("solscan_api_key", "KEY")
        wallet = add_wallet(self.ctx, "solana", OWNER + "", label="solscan")
        res = self.adapter.sync(wallet, {}, lambda m: None)
        self.assertEqual(self.summary(res.movements), self.summary(rpc.movements))
        self.assertTrue(all(c[0].startswith("/account/") for c in self.calls))
        by = {m.uid: m for m in res.movements}
        self.assertEqual((by["sigA:sol"].from_address, by["sigA:sol"].to_address), (SENDER, OWNER))
        self.assertEqual((by["sigB:sol"].from_address, by["sigB:sol"].to_address), (OWNER, SENDER))
        self.assertEqual(res.state["solscan_from_time"], 1736000000)
        ledger.store_sync_result(self.ctx, wallet, res)
        bal, _ = ledger.reconstruct(self.ctx, wallet["id"], 2_000_000_000)
        live, _ = self.adapter.current_balances(wallet, [], lambda m: None)
        self.assertEqual(bal["solana:native"], live["solana:native"])
        self.assertEqual(bal[f"solana:{USDC}"], live[f"solana:{USDC}"])
        self.assertEqual(live[f"solana:{USDC}"], Decimal("25"))

    def test_incremental_and_refused_key(self):
        self.ctx.secrets.set("solscan_api_key", "KEY")
        wallet = add_wallet(self.ctx, "solana", OWNER)
        res = self.adapter.sync(wallet, {"solscan_from_time": 1736000000}, lambda m: None)
        self.assertEqual([m.uid for m in res.movements], ["sigC:spl:" + USDC])

        def refuse(method, url, params, payload):
            raise HttpError(401, '{"success":false}', url)
        self.t.http.handlers.insert(0, ("https://pro-api.solscan.io", refuse))
        with self.assertRaises(AdapterError) as cm:
            self.adapter.sync(wallet, {}, lambda m: None)
        self.assertIn("not in your plan", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
