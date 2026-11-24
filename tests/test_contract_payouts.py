"""Coins moved by contracts on chains where Alchemy gives no internal transfers (Ronin, BSC…):
staking rewards and other payouts in the native coin, wrapping and unwrapping (WRON), failed
transactions, a node without past balances, resume, and the wallet check."""
import unittest
from decimal import Decimal

from crumenae import classify, sync, views
from crumenae.adapters import get_adapter

from helpers import FakeHttp, TempContext, add_wallet

LOG = lambda m: None  # noqa: E731
ME = "0x" + "ab" * 20
FRIEND, SHOP, STAKING = "0x" + "11" * 20, "0x" + "33" * 20, "0x" + "55" * 20
WRON = "0xe514d9deb7966c8be0ca922de8a064264ea6bcd4"
E18, GAS = 10 ** 18, 21000 * 10 ** 9          # gas: 21,000 × 1 gwei per transaction


def ext(block, h, frm, to, wei):
    return {"blockNum": hex(block), "uniqueId": f"{h}:external", "hash": h, "from": frm, "to": to,
            "category": "external", "asset": "RON", "rawContract": {"value": hex(wei), "decimal": "0x12"},
            "metadata": {"blockTimestamp": f"2025-01-{block % 28 + 1:02d}T00:00:00.000Z"}}


class FakeRonin:
    """An archive node: RON and WRON balances at every block, transfers as Alchemy reports them
    (no internal transfers, no WRON Deposit/Withdrawal)."""
    def __init__(self, archive=True):
        self.archive, self.calls = archive, []
        self.transfers = [
            ext(0x10, "0xa1", FRIEND, ME, 10 * E18),           # received 10 RON
            ext(0x20, "0xa2", ME, STAKING, 0),                  # claimRewards: 1.5 RON paid by the contract
            ext(0x25, "0xa3", ME, WRON, 2 * E18),               # wrap 2 RON
            ext(0x30, "0xa4", ME, WRON, 0),                     # unwrap 2 WRON: RON paid by the contract
            ext(0x40, "0xa5", ME, SHOP, 5 * E18),               # failed: nothing moved but the gas
        ]
        self.native = [(0x10, 10 * E18), (0x20, 15 * E18 // 10 - GAS), (0x25, -2 * E18 - GAS),
                       (0x30, 2 * E18 - GAS), (0x40, -GAS)]
        self.wron = [(0x25, 2 * E18), (0x30, -2 * E18)]

    @staticmethod
    def at(changes, block):
        return sum(v for b, v in changes if b <= block)

    def __call__(self, method, url, params, payload):
        m, p = payload["method"], payload["params"]
        ok = lambda r: {"jsonrpc": "2.0", "id": 1, "result": r}  # noqa: E731
        self.calls.append((m, p))
        if m == "eth_blockNumber":
            return ok(hex(0x50))
        if m == "alchemy_getAssetTransfers":
            q = p[0]
            assert "internal" not in q["category"]            # Ronin: Alchemy has none
            lo = int(q["fromBlock"], 16)
            field, value = ("fromAddress", q["fromAddress"]) if "fromAddress" in q else ("toAddress", q["toAddress"])
            key = "from" if field == "fromAddress" else "to"
            return ok({"transfers": [t for t in self.transfers if t[key] == value and int(t["blockNum"], 16) >= lo]})
        if m == "eth_getTransactionReceipt":
            return ok({"from": ME, "to": "0x", "gasUsed": hex(21000), "effectiveGasPrice": hex(10 ** 9),
                       "status": "0x0" if p[0] == "0xa5" else "0x1"})
        if m == "alchemy_getTokenBalances":
            return ok({"address": ME, "tokenBalances": [{"contractAddress": WRON, "tokenBalance": "0x0"}]})
        tag = p[-1]
        if tag != "latest" and not self.archive:
            return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "missing trie node"}}
        block = 0x50 if tag == "latest" else int(tag, 16)
        if m == "eth_getBalance":
            return ok(hex(self.at(self.native, block)))
        if m == "eth_call":
            to = p[0]["to"].lower()
            return ok(hex(self.at(self.wron, block) if to == WRON else 0))
        if m == "eth_getBlockByNumber":
            return ok({"timestamp": hex(1735689600 + int(p[0], 16))})
        return None


class ContractPayoutsTest(unittest.TestCase):
    def setUp(self, archive=True):
        self.fake = FakeRonin(archive)
        self.t = TempContext(FakeHttp().on("https://ronin-mainnet.g.alchemy.com", self.fake))
        self.ctx = self.t.ctx
        self.ctx.offline = False
        self.ctx.secrets.set("alchemy_api_key", "K")
        self.w = add_wallet(self.ctx, "ronin", ME, synced=False)

    def tearDown(self):
        self.t.close()

    def test_payouts_wrapping_and_failed_tx(self):
        res = get_adapter(self.ctx, self.ctx.networks.get("ronin")).sync(self.w, {}, LOG)
        got = {m.uid: (m.asset_key.split(":")[1], m.amount, m.info) for m in res.movements}
        fee = Decimal(GAS) / E18
        self.assertEqual(got["blk32:native"], ("native", Decimal("1.5"), "paid by a contract"))   # reward claim
        self.assertEqual(got["blk37:wrap"], (WRON, Decimal(2), "wrapped"))
        self.assertEqual(got["blk48:wrap"], (WRON, Decimal(-2), "unwrapped"))
        self.assertEqual(got["blk48:native"], ("native", Decimal(2), "paid by a contract"))
        self.assertNotIn("0xa5:out", got)                                          # failed: value not sent
        self.assertEqual(got["0xa5:fee"][1], -fee)                                 # … its gas was paid
        total = sum(m.amount for m in res.movements if m.asset_key == "ronin:native")
        self.assertEqual(total, Decimal("11.5") - 4 * fee)                         # = the chain's balance
        self.assertEqual(res.state["balance_check"], 1)

    def test_wallet_check_and_swap_classification(self):
        sync.sync_wallet(self.ctx, self.ctx.wallet(self.w["id"]), LOG)
        self.assertEqual(views.wallets(self.ctx)[0]["check"]["status"], "ok")
        classify.run(self.ctx)
        cats = {r["uid"]: r["category"] for r in self.ctx.db.query("SELECT uid, category FROM movements")}
        self.assertEqual((cats["0xa3:out"], cats["blk37:wrap"]), ("swap", "swap"))   # RON → WRON

    def test_resume_and_first_sync_after_the_update(self):
        a = get_adapter(self.ctx, self.ctx.networks.get("ronin"))
        old_state = {"last_block": 0x40, "reader": "alchemy"}             # synced before this version
        a.sync(self.w, old_state, LOG)
        froms = {p[0]["fromBlock"] for m, p in self.fake.calls if m == "alchemy_getAssetTransfers"}
        self.assertEqual(froms, {"0x0"})                                   # every past block checked once
        res = a.sync(self.w, {"last_block": 0x40, "reader": "alchemy", "balance_check": 1}, LOG)
        self.assertEqual({m.uid for m in res.movements}, {"0xa5:fee"})     # then only new blocks

    def test_node_without_past_balances(self):
        self.tearDown()
        self.setUp(archive=False)
        res = get_adapter(self.ctx, self.ctx.networks.get("ronin")).sync(self.w, {}, LOG)
        self.assertFalse([m for m in res.movements if m.uid.startswith("blk")])
        self.assertTrue(any("no balances of past blocks" in w for w in res.warnings))


class StakingPlumbingTest(unittest.TestCase):
    def test_staked_asset_and_category(self):
        from crumenae import assets as assets_mod
        t = TempContext()
        try:
            a = assets_mod.staked_asset(t.ctx.networks.get("ronin"))
            self.assertEqual((a.key, a.symbol, a.coingecko_id), ("ronin:staked", "RON (staked)", "ronin"))
            self.assertEqual(assets_mod.group_key(vars(a)), assets_mod.group_key(
                vars(assets_mod.native_asset(t.ctx.networks.get("ronin")))))       # grouped with RON
            self.assertIn("staking", classify.CATEGORIES)
            row = {"kind": "out", "status": "known", "amount": "-5", "info": "staking: delegated", "counterparty": "",
                   "tx_hash": "0x1", "network": "ronin", "wallet_id": 1}
            self.assertEqual(classify._category(row, {}, set(), {}), "staking")
        finally:
            t.close()


if __name__ == "__main__":
    unittest.main()
