"""The EVM chains read through Alchemy with the same key: configuration, endpoint, a sync and
the Celo native-token duplicate."""
import unittest
from decimal import Decimal

from crumenae.adapters import get_adapter

from helpers import FakeHttp, TempContext, add_wallet

LOG = lambda m: None  # noqa: E731
ME = "0x" + "a" * 40
CELO_TOKEN = "0x471ece3750da237f93b8e339c536989b8978a438"
CHAINS = {"zksync": "zksync-mainnet", "scroll": "scroll-mainnet", "blast": "blast-mainnet",
          "mantle": "mantle-mainnet", "gnosis": "gnosis-mainnet", "celo": "celo-mainnet",
          "sonic": "sonic-mainnet", "opbnb": "opbnb-mainnet", "unichain": "unichain-mainnet",
          "worldchain": "worldchain-mainnet", "berachain": "berachain-mainnet", "zora": "zora-mainnet"}


def rpc(method, url, params, payload):
    m = payload["method"]
    ok = lambda r: {"jsonrpc": "2.0", "id": 1, "result": r}  # noqa: E731
    if m == "eth_blockNumber":
        return ok(hex(500))
    if m == "alchemy_getAssetTransfers":
        if payload["params"][0].get("toAddress") == ME:
            return ok({"transfers": [{"blockNum": "0x10", "uniqueId": "0xaa:external", "hash": "0xAA",
                                      "from": "0x" + "1" * 40, "to": ME, "category": "external", "asset": "X",
                                      "rawContract": {"value": hex(3 * 10 ** 18), "decimal": "0x12"},
                                      "metadata": {"blockTimestamp": "2025-01-01T00:00:00.000Z"}}]})
        return ok({"transfers": []})
    if m == "eth_getBalance":
        return ok(hex(3 * 10 ** 18))
    if m == "alchemy_getTokenBalances":
        return ok({"address": ME, "tokenBalances": [{"contractAddress": CELO_TOKEN, "tokenBalance": hex(3 * 10 ** 18)}]})
    return None


class MoreEvmChainsTest(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttp()
        for slug in CHAINS.values():
            self.http.on(f"https://{slug}.g.alchemy.com/v2/", rpc)
        self.t = TempContext(self.http)
        self.ctx = self.t.ctx
        self.ctx.secrets.set("alchemy_api_key", "KEY")

    def tearDown(self):
        self.t.close()

    def test_each_chain_is_an_evm_chain_read_by_alchemy(self):
        for nid, slug in CHAINS.items():
            net = self.ctx.networks.get(nid)
            self.assertEqual((net.family, net.adapter, net.alchemy_network), ("evm", "alchemy", slug), nid)
            self.assertTrue(net.chain_id and net.native.get("symbol") and net.tx_url, nid)
            self.assertEqual(get_adapter(self.ctx, net)._rpc_url(), f"https://{slug}.g.alchemy.com/v2/KEY")

    def test_sync_and_balance(self):
        for nid in ("zksync", "mantle", "berachain"):
            w = add_wallet(self.ctx, nid, ME, label=nid)
            adapter = get_adapter(self.ctx, self.ctx.networks.get(nid))
            res = adapter.sync(w, {}, LOG)
            self.assertEqual([(m.uid, m.amount) for m in res.movements], [("0xaa:in", Decimal(3))], nid)
            self.assertEqual(res.state["last_block"], 500)

    def test_celo_native_token_is_not_counted_twice(self):
        w = add_wallet(self.ctx, "celo", ME)
        bal, _ = get_adapter(self.ctx, self.ctx.networks.get("celo")).current_balances(w, [], LOG)
        self.assertEqual(bal["celo:native"], Decimal(3))
        self.assertNotIn(f"celo:{CELO_TOKEN}", bal)                 # the same CELO, seen as an ERC-20


if __name__ == "__main__":
    unittest.main()
