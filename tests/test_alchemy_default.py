"""One free Alchemy key reads every EVM chain; Etherscan is used automatically where its free
plan works once its key is saved; a change of provider never counts a movement twice."""
import unittest

from crumenae.adapters import get_adapter
from crumenae.adapters.evm import resume_block
from crumenae.server import App

from helpers import FakeHttp, TempContext, add_wallet

LOG = lambda m: None  # noqa: E731
ADDR = "0x" + "a" * 40
EVM = ("ethereum", "polygon", "arbitrum", "optimism", "base", "avalanche", "linea", "bsc", "monad", "arc", "ronin")


class AutomaticProviderTest(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def alchemy(method, url, params, payload):
            self.calls.append((url, payload))
            if payload["method"] == "alchemy_getAssetTransfers":
                return {"jsonrpc": "2.0", "id": 1, "result": {"transfers": []}}
            return None
        self.t = TempContext(FakeHttp().on("https://eth-mainnet.g.alchemy.com/v2/", alchemy))
        self.ctx = self.t.ctx
        self.app = App(self.ctx)

    def tearDown(self):
        self.t.close()

    def provider(self, nid):
        return self.ctx.networks.get(nid).provider

    def test_alchemy_alone_reads_every_evm_chain(self):
        self.ctx.secrets.set("alchemy_api_key", "A")
        for nid in EVM:
            n = self.ctx.networks.get(nid)
            self.assertEqual((n.provider, n.adapter), ("alchemy", "alchemy"), nid)
            self.assertTrue(n.alchemy_network, nid)
        self.assertEqual(self.ctx.networks.get("ethereum").alchemy_network, "eth-mainnet")
        self.assertTrue(self.ctx.networks.get("ethereum").alchemy_internal)

    def test_no_key_yet_points_to_alchemy(self):
        self.assertEqual({self.provider(n) for n in EVM}, {"alchemy"})

    def test_etherscan_key_used_where_its_free_plan_works(self):
        self.ctx.secrets.set("alchemy_api_key", "A")
        self.ctx.secrets.set("etherscan_api_key", "E")   # cached answers are refreshed at once
        self.assertEqual([self.provider(n) for n in ("ethereum", "polygon", "arbitrum", "linea")], ["etherscan"] * 4)
        self.assertEqual(self.ctx.networks.get("ethereum").adapter, "evm")
        self.assertEqual([self.provider(n) for n in ("optimism", "base", "avalanche", "bsc")], ["alchemy"] * 4)

    def test_existing_etherscan_only_install_is_unchanged(self):
        self.ctx.secrets.set("etherscan_api_key", "E")
        self.assertEqual([self.provider(n) for n in ("ethereum", "base", "optimism", "avalanche")], ["etherscan"] * 4)

    def test_choice_in_settings_wins_and_automatic_can_come_back(self):
        self.ctx.secrets.set("etherscan_api_key", "E")
        nets = {n["id"]: n for n in self.app.dispatch("GET", "/api/networks", {}, None)}
        self.assertTrue(nets["ethereum"]["provider_auto"])
        self.assertEqual(nets["ethereum"]["auto_providers"], ["etherscan", "alchemy"])
        n = self.app.dispatch("PUT", "/api/networks/ethereum", {}, {"provider": "alchemy"})
        self.assertEqual((n["provider"], n.get("provider_auto")), ("alchemy", None))
        n = self.app.dispatch("PUT", "/api/networks/ethereum", {}, {"provider": ""})
        self.assertEqual((n["provider"], n["provider_auto"]), ("etherscan", True))

    def test_switch_of_provider_resumes_after_the_last_block(self):
        self.assertEqual(resume_block({}, "alchemy"), 0)
        self.assertEqual(resume_block({"last_block": 100}, "alchemy"), 100)                         # same reader
        self.assertEqual(resume_block({"last_block": 100, "reader": "alchemy"}, "alchemy"), 100)
        self.assertEqual(resume_block({"last_block": 100, "reader": "etherscan"}, "alchemy"), 101)
        self.ctx.secrets.set("alchemy_api_key", "A")
        w = add_wallet(self.ctx, "ethereum", ADDR)
        a = get_adapter(self.ctx, self.ctx.networks.get("ethereum"))
        res = a.sync(w, {"last_block": 100, "reader": "etherscan"}, LOG)
        q = [p["params"][0] for _, p in self.calls if p["method"] == "alchemy_getAssetTransfers"]
        self.assertEqual({x["fromBlock"] for x in q}, {hex(101)})
        self.assertIn("internal", q[0]["category"])                                        # Ethereum: internal too
        self.assertEqual((res.state["reader"], res.state["last_block"]), ("alchemy", 101))

    def test_solana_uses_the_alchemy_key(self):
        a = lambda: get_adapter(self.ctx, self.ctx.networks.get("solana"))  # noqa: E731
        self.assertIn("solana.com", a()._url())                       # no key: public endpoint
        self.ctx.secrets.set("alchemy_api_key", "A")
        self.assertEqual(a()._url(), "https://solana-mainnet.g.alchemy.com/v2/A")
        self.ctx.secrets.set("helius_api_key", "H")                   # a Helius key wins
        self.assertIn("helius-rpc.com", a()._url())


if __name__ == "__main__":
    unittest.main()
