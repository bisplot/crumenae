import base64
import unittest
from decimal import Decimal

from cryptoledger import apitest, sync
from cryptoledger.adapters import get_adapter
from cryptoledger.adapters.stellar import _crc16_xmodem, valid_account_id
from cryptoledger.server import ApiError, App

from helpers import FakeHttp, TempContext, add_wallet

LOG = lambda m: None  # noqa: E731


def strkey(seed: int) -> str:
    """A syntactically valid Stellar account id made from synthetic bytes."""
    payload = bytes([6 << 3]) + bytes([(seed + i) % 256 for i in range(32)])
    return base64.b32encode(payload + _crc16_xmodem(payload).to_bytes(2, "little")).decode()


ME, OTHER = strkey(1), strkey(2)
USDC = "USDC-GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN"


class ProvidersTest(unittest.TestCase):
    def setUp(self):
        self.t = TempContext(FakeHttp().on("https://api.lineascan.example", self.lineascan))
        self.ctx = self.t.ctx
        self.app = App(self.ctx)

    def tearDown(self):
        self.t.close()

    @staticmethod
    def lineascan(method, url, params, payload):
        if params.get("action") == "eth_blockNumber":
            assert params.get("apikey") == "k123" and "chainid" not in params
            return {"jsonrpc": "2.0", "id": 1, "result": "0x10"}
        return None

    def test_add_provider_network_test_remove(self):
        p = self.app.dispatch("POST", "/api/providers", {}, {
            "name": "Lineascan", "type": "etherscan", "base_url": "https://api.lineascan.example/api",
            "chainid_param": False, "key": "k123"})
        self.assertEqual((p["id"], p["secret"], p["secret_source"]), ("lineascan", "lineascan_api_key", "file"))
        net = self.app.dispatch("POST", "/api/networks", {}, {
            "id": "testchain", "name": "Test chain", "provider": "lineascan", "chain_id": "12345",
            "native_symbol": "tst", "native_coingecko": "test-coin"})
        n = self.ctx.networks.get("testchain")
        self.assertEqual((n.adapter, n.explorer_api, n.explorer_key, n.chainid_param),
                         ("evm", "https://api.lineascan.example/api", "lineascan", False))
        self.assertEqual(net["native"]["symbol"], "TST")
        # Its own call limits, found by host.
        self.assertEqual(self.ctx.limiter.service_for("api.lineascan.example"), "lineascan")
        self.app.dispatch("PUT", "/api/settings", {}, {"rate_limits": {"lineascan": {"rate": "2", "unit": "second"}}})
        self.assertEqual(self.ctx.limiter.limits("lineascan")["per_second"], 2)
        # Test button, generic for the type.
        r = apitest.test_provider(self.ctx, "lineascan")
        self.assertTrue(r["ok"], r)
        # Cannot remove while a network uses it.
        with self.assertRaises(ApiError):
            self.app.dispatch("DELETE", "/api/providers/lineascan", {}, None)
        self.app.dispatch("DELETE", "/api/networks/testchain", {}, None)
        self.app.dispatch("DELETE", "/api/providers/lineascan", {}, None)
        self.assertIsNone(self.ctx.secrets.get("lineascan_api_key"))
        self.assertNotIn("lineascan", [x["id"] for x in self.app.dispatch("GET", "/api/providers", {}, None)])

    def test_validation(self):
        bad = [{"name": "X", "type": "solana"}, {"name": "", "type": "etherscan"},
               {"name": "X", "type": "etherscan", "base_url": "ftp://x"},
               {"name": "X", "type": "alchemy", "base_url": "https://eth.example/v2"}]
        for body in bad:
            with self.assertRaises(ApiError, msg=body):
                self.app.dispatch("POST", "/api/providers", {}, body)
        with self.assertRaises(ApiError):   # a Bitcoin provider cannot read an EVM chain
            self.app.dispatch("PUT", "/api/networks/arbitrum", {}, {"provider": "esplora"})
        with self.assertRaises(ApiError):   # built-in providers are not removable
            self.app.dispatch("DELETE", "/api/providers/etherscan", {}, None)

    def test_switch_builtin_network_and_hide(self):
        self.app.dispatch("PUT", "/api/networks/arbitrum", {}, {"provider": "alchemy", "alchemy_network": "arb-mainnet"})
        n = self.ctx.networks.get("arbitrum")
        self.assertEqual((n.adapter, n.alchemy_network, n.explorer_key), ("alchemy", "arb-mainnet", "alchemy"))
        self.ctx.secrets.set("alchemy_api_key", "a" * 32)
        w = add_wallet(self.ctx, "arbitrum", "0x" + "a" * 40)
        self.assertEqual(sync.wallet_service(self.ctx, w), "alchemy")
        self.app.dispatch("PUT", "/api/networks/arbitrum", {}, {"provider": "etherscan"})
        self.assertEqual(self.ctx.networks.get("arbitrum").adapter, "evm")
        self.app.dispatch("PUT", "/api/networks/base", {}, {"hidden": True})
        nets = {x["id"]: x for x in self.app.dispatch("GET", "/api/networks", {}, None)}
        self.assertTrue(nets["base"]["hidden"])
        self.assertEqual(nets["arbitrum"]["wallets"], 1)
        with self.assertRaises(ApiError):
            self.app.dispatch("PUT", "/api/networks/base", {}, {"chain_id": 1})   # not editable on a built-in

    def test_presets(self):
        get = self.ctx.networks.get
        self.assertEqual((get("bsc").adapter, get("bsc").alchemy_network), ("alchemy", "bnb-mainnet"))
        self.assertEqual((get("monad").adapter, get("monad").chain_id), ("alchemy", 143))
        self.assertEqual(get("arc").ignore_tokens, ["0x3600000000000000000000000000000000000000"])
        self.assertEqual((get("linea").adapter, get("linea").chain_id), ("evm", 59144))
        self.assertEqual((get("stellar").adapter, get("stellar").provider), ("stellar", "horizon"))


def horizon_handler(method, url, params, payload):
    if url.endswith(f"/accounts/{ME}"):
        return {"balances": [{"balance": "95.5000000", "asset_type": "native"},
                             {"balance": "20.0000000", "asset_type": "credit_alphanum4", "asset_code": "USDC",
                              "asset_issuer": USDC.split("-")[1]}]}
    if url.endswith(f"/accounts/{ME}/effects"):
        if params.get("cursor"):
            return {"_embedded": {"records": []}}
        return {"_embedded": {"records": [
            {"id": "0000000001-0000000001", "paging_token": "1-1", "type": "account_created", "created_at": "2025-01-01T00:00:00Z",
             "starting_balance": "100.0000000"},
            {"id": "0000000002-0000000001", "paging_token": "2-1", "type": "account_debited", "created_at": "2025-02-01T00:00:00Z",
             "amount": "4.0000000", "asset_type": "native"},
            {"id": "0000000003-0000000001", "paging_token": "3-1", "type": "account_credited", "created_at": "2025-03-01T00:00:00Z",
             "amount": "20.0000000", "asset_type": "credit_alphanum4", "asset_code": "USDC", "asset_issuer": USDC.split("-")[1]},
            {"id": "0000000004-0000000001", "paging_token": "4-1", "type": "liquidity_pool_deposited",
             "created_at": "2025-04-01T00:00:00Z"}]}}
    if url.endswith(f"/accounts/{ME}/operations"):
        assert params.get("join") == "transactions"
        return {"_embedded": {"records": [
            {"id": "0000000001", "paging_token": "1", "type": "create_account", "funder": OTHER, "account": ME,
             "transaction_hash": "aa" * 32, "transaction": {"hash": "aa" * 32, "source_account": OTHER, "fee_charged": "100"}},
            {"id": "0000000002", "paging_token": "2", "type": "payment", "from": ME, "to": OTHER,
             "transaction_hash": "bb" * 32,
             "transaction": {"hash": "bb" * 32, "source_account": ME, "fee_charged": "100", "created_at": "2025-02-01T00:00:00Z"}},
            {"id": "0000000003", "paging_token": "3", "type": "payment", "from": OTHER, "to": ME,
             "transaction_hash": "cc" * 32, "transaction": {"hash": "cc" * 32, "source_account": OTHER, "fee_charged": "100"}}]}}
    if url.rstrip("/") == "https://horizon.stellar.org":
        return {"history_latest_ledger": 55000000}
    return None


class StellarTest(unittest.TestCase):
    def setUp(self):
        self.t = TempContext(FakeHttp().on("https://horizon.stellar.org", horizon_handler))
        self.ctx = self.t.ctx
        self.adapter = get_adapter(self.ctx, self.ctx.networks.get("stellar"))

    def tearDown(self):
        self.t.close()

    def test_address(self):
        self.assertTrue(valid_account_id(ME))
        self.assertEqual(self.adapter.validate_address(ME.lower()), ME)
        broken = ME[:-1] + ("A" if ME[-1] != "A" else "B")
        for bad in (broken, "G123", "0x" + "a" * 40, "M" + ME[1:]):
            with self.assertRaises(ValueError):
                self.adapter.validate_address(bad)

    def test_sync_and_balances(self):
        w = add_wallet(self.ctx, "stellar", ME)
        res = self.adapter.sync(w, {}, LOG)
        got = sorted((m.uid, m.asset_key, m.amount, m.tx_hash[:2]) for m in res.movements)
        self.assertEqual(got, sorted([
            ("0000000001-0000000001:in", "stellar:native", Decimal("100"), "aa"),
            ("0000000002-0000000001:out", "stellar:native", Decimal("-4"), "bb"),
            ("0000000003-0000000001:in", f"stellar:{USDC}", Decimal("20"), "cc"),
            ("bb" * 32 + ":fee", "stellar:native", Decimal("-0.00001"), "bb"),   # 100 stroops, paid by me only
        ]))
        self.assertTrue(any("liquidity_pool_deposited" in x for x in res.warnings))
        self.assertEqual(res.state["effects_cursor"], "4-1")
        usdc = next(a for a in res.assets if a.key == f"stellar:{USDC}")
        self.assertEqual((usdc.symbol, usdc.coingecko_id, usdc.status), ("USDC", "usd-coin", "known"))
        bal, _ = self.adapter.current_balances(w, [], LOG)
        self.assertEqual(bal, {"stellar:native": Decimal("95.5"), f"stellar:{USDC}": Decimal("20")})
        self.assertTrue(apitest.test_provider(self.ctx, "horizon")["ok"])


if __name__ == "__main__":
    unittest.main()
