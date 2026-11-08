"""Solana native staking: stake accounts (live and closed ones found in the history), their balance
after every transaction and every epoch's reward, rewards as income, SOL moved in/out not a disposal."""
import unittest
from decimal import Decimal

from cryptoledger import classify, ledger, sync, views
from cryptoledger.adapters.solana import NOT_DEACTIVATED, SLOTS_PER_EPOCH, STAKE_PROGRAM
from cryptoledger.b58 import b58encode

from helpers import FakeHttp, TempContext, add_wallet

LOG = lambda m: None  # noqa: E731
OWNER = b58encode(bytes([1] * 32))
STAKE = b58encode(bytes([2] * 32))
CLOSED = b58encode(bytes([3] * 32))
VOTER = b58encode(bytes([4] * 32))
SYSTEM = "11111111111111111111111111111111"
SOL = 1_000_000_000
FEE = 5000


def slot(epoch, n=5):
    return epoch * SLOTS_PER_EPOCH + n


def when(s):
    return 1_700_000_000 + s // 1000   # a block time for each slot


def tx(sig, epoch, keys, pre, post, ixs):
    s = slot(epoch)
    return {"blockTime": when(s), "slot": s,
            "meta": {"err": None, "fee": FEE, "preBalances": pre, "postBalances": post,
                     "preTokenBalances": [], "postTokenBalances": [], "innerInstructions": []},
            "transaction": {"signatures": [sig], "message": {
                "accountKeys": [{"pubkey": k, "signer": i == 0, "writable": True} for i, k in enumerate(keys)],
                "instructions": ixs}}}


def create(new, lamports):
    return {"program": "system", "parsed": {"type": "createAccount", "info": {
        "source": OWNER, "newAccount": new, "lamports": lamports, "owner": STAKE_PROGRAM}}}


TXS = {
    # Created CLOSED with 1 SOL (epoch 780), withdrew it with a reward (epoch 790): only in the history.
    "c1": tx("c1", 780, [OWNER, CLOSED, SYSTEM, STAKE_PROGRAM], [3 * SOL, 0, 1, 1],
             [2 * SOL - FEE, SOL, 1, 1], [create(CLOSED, SOL)]),
    "c2": tx("c2", 790, [OWNER, CLOSED, STAKE_PROGRAM], [2 * SOL - FEE, SOL + 10_000_000, 1],
             [3 * SOL + 10_000_000 - 2 * FEE, 0, 1],
             [{"program": "stake", "parsed": {"type": "withdraw", "info": {"stakeAccount": CLOSED}}}]),
    # Created and delegated STAKE with 5 SOL (epoch 795).
    "dlg": tx("dlg", 795, [OWNER, STAKE, SYSTEM, STAKE_PROGRAM], [10 * SOL, 0, 1, 1],
              [5 * SOL - FEE, 5 * SOL, 1, 1],
              [create(STAKE, 5 * SOL),
               {"program": "stake", "parsed": {"type": "delegate", "info": {"stakeAccount": STAKE, "voteAccount": VOTER}}}]),
}
SIGS = {OWNER: ["dlg", "c2", "c1"], STAKE: ["dlg"], CLOSED: ["c2", "c1"]}
REWARDS = {(CLOSED, 785): (10_000_000, SOL + 10_000_000),
           (STAKE, 796): (1_000_000, 5 * SOL + 1_000_000), (STAKE, 797): (1_000_000, 5 * SOL + 2_000_000)}


class SolanaRpc:
    def __init__(self):
        self.calls = []

    def __call__(self, method, url, params, payload):
        m, p = payload["method"], payload["params"]
        self.calls.append(m)
        ok = lambda r: {"jsonrpc": "2.0", "id": 1, "result": r}  # noqa: E731
        if m == "getTokenAccountsByOwner":
            return ok({"context": {"slot": 1}, "value": []})
        if m == "getProgramAccounts":
            assert p[0] == STAKE_PROGRAM and p[1]["filters"][0]["memcmp"] == {"offset": 44, "bytes": OWNER}
            return ok([{"pubkey": STAKE, "account": {"lamports": 5 * SOL + 2_000_000, "data": {"program": "stake", "parsed": {
                "type": "delegated", "info": {"stake": {"delegation": {
                    "voter": VOTER, "activationEpoch": "795", "deactivationEpoch": str(NOT_DEACTIVATED)}}}}}}}])
        if m == "getSignaturesForAddress":
            if p[1].get("until"):
                return ok([])
            return ok([{"signature": s, "slot": TXS[s]["slot"], "blockTime": TXS[s]["blockTime"], "err": None}
                       for s in SIGS.get(p[0], [])])
        if m == "getTransaction":
            return ok(TXS[p[0]])
        if m == "getEpochInfo":
            return ok({"epoch": 800, "absoluteSlot": slot(800, 10)})
        if m == "getInflationReward":
            epoch = p[1]["epoch"]
            out = []
            for a in p[0]:
                r = REWARDS.get((a, epoch))
                out.append({"epoch": epoch, "effectiveSlot": slot(epoch + 1, 1), "amount": r[0], "postBalance": r[1],
                            "commission": 5} if r else None)
            return ok(out)
        if m == "getBlockTime":
            return ok(when(p[0]))
        if m == "getBalance":
            return ok({"context": {"slot": 1}, "value": 5 * SOL - FEE + 3 * SOL})
        return None


class SolanaStakingTest(unittest.TestCase):
    def setUp(self):
        self.rpc = SolanaRpc()
        self.t = TempContext(FakeHttp().on("https://api.mainnet-beta.solana.com", self.rpc))
        self.ctx = self.t.ctx
        self.w = add_wallet(self.ctx, "solana", OWNER)

    def tearDown(self):
        self.t.close()

    def sync(self):
        sync.sync_wallet(self.ctx, self.ctx.wallet(self.w["id"]), LOG)
        classify.run(self.ctx)

    def moves(self):
        return {r["uid"]: r for r in self.ctx.db.query("SELECT * FROM movements")}

    def test_history_rewards_and_categories(self):
        self.sync()
        m = self.moves()
        self.assertEqual((Decimal(m[f"dlg:stake:{STAKE}"]["amount"]), Decimal(m[f"dlg:stake:{STAKE}"]["balance_after"])),
                         (Decimal(5), Decimal(5)))
        self.assertEqual(m["dlg:sol"]["category"], "internal_transfer")          # moving SOL to stake: no disposal
        self.assertEqual(m[f"dlg:stake:{STAKE}"]["category"], "internal_transfer")
        r = m[f"stake:{STAKE}:reward:797"]
        self.assertEqual((Decimal(r["amount"]), Decimal(r["balance_after"]), r["category"]),
                         (Decimal("0.001"), Decimal("5.002"), "reward"))
        self.assertIn(f"stake:{CLOSED}:reward:785", m)                            # closed account: from history
        self.assertEqual(Decimal(m[f"c2:stake:{CLOSED}"]["balance_after"]), 0)
        self.assertNotIn(f"stake:{CLOSED}:reward:791", m)
        year = self.ctx.calendar.year_of(when(slot(797)))
        rewards = views.staking(self.ctx, year)["rewards"]
        self.assertEqual(sorted(Decimal(x["amount"]) for x in rewards), [Decimal("0.001"), Decimal("0.001"), Decimal("0.01")])

    def test_balance_at_any_time_from_the_chain(self):
        self.sync()
        mid, _ = ledger.reconstruct(self.ctx, self.w["id"], when(slot(797, 1)))   # after the 796 reward
        self.assertEqual(mid[f"solana:stake:{STAKE}"], Decimal("5.001"))
        end, anchored = ledger.reconstruct(self.ctx, self.w["id"], 2_000_000_000)
        self.assertEqual((end[f"solana:stake:{STAKE}"], end[f"solana:stake:{CLOSED}"]), (Decimal("5.002"), 0))
        self.assertIn(f"solana:stake:{STAKE}", anchored)
        held = {r["asset_key"]: Decimal(r["amount"]) for r in self.ctx.db.query("SELECT * FROM current_balances")}
        self.assertEqual(held[f"solana:stake:{STAKE}"], end[f"solana:stake:{STAKE}"])   # live = history

    def test_staking_page_and_asset(self):
        self.sync()
        p = views.staking(self.ctx, 2025)["staked"]
        self.assertEqual([(x["protocol"], x["validator"], Decimal(x["staked"]), x["symbol"]) for x in p],
                         [("Solana native staking", VOTER, Decimal("5.002"), "SOL (staked)")])
        a = self.ctx.db.query_one("SELECT * FROM assets WHERE key=?", (f"solana:stake:{STAKE}",))
        self.assertEqual((a["coingecko_id"], a["status"]), ("solana", "known"))

    def test_partitioned_rewards_one_block_time_per_epoch(self):
        from cryptoledger.adapters import get_adapter
        other = b58encode(bytes([5] * 32))

        def rewards(method, url, params, payload):
            m = payload["method"]
            if m == "getInflationReward":   # same epoch, paid 250 slots apart
                return {"jsonrpc": "2.0", "id": 1, "result": [
                    {"epoch": 799, "effectiveSlot": slot(800, 1), "amount": 7, "postBalance": 107, "commission": 5},
                    {"epoch": 799, "effectiveSlot": slot(800, 251), "amount": 9, "postBalance": 209, "commission": 5}]}
            return self.rpc(method, url, params, payload)
        t = TempContext(FakeHttp().on("https://api.mainnet-beta.solana.com", rewards))
        try:
            a = get_adapter(t.ctx, t.ctx.networks.get("solana"))
            self.rpc.calls.clear()
            moves = a._rewards(OWNER, {STAKE: (799, None), other: (799, None)}, {}, LOG)
            self.assertEqual(self.rpc.calls.count("getBlockTime"), 1)
            self.assertEqual([m.ts - moves[0].ts for m in moves], [0, 100])             # 250 slots × 0.4 s
            self.assertEqual([m.balance_after for m in moves], [Decimal("0.000000107"), Decimal("0.000000209")])
        finally:
            t.close()

    def test_newer_transaction_version_is_asked_again(self):
        from cryptoledger.adapters import get_adapter
        asked = []

        def v2(method, url, params, payload):
            if payload["method"] == "getTransaction":
                version = payload["params"][1]["maxSupportedTransactionVersion"]
                asked.append(version)
                if version < 2:
                    return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32015, "message":
                            "Transaction version (2) is not supported by the requesting client."}}
            return self.rpc(method, url, params, payload)
        t = TempContext(FakeHttp().on("https://api.mainnet-beta.solana.com", v2))
        try:
            tx = get_adapter(t.ctx, t.ctx.networks.get("solana")).get_transaction("dlg")
            self.assertEqual((asked, tx["slot"]), ([1, 2], slot(795)))
        finally:
            t.close()

    def test_next_sync_reads_only_new_epochs(self):
        self.sync()
        self.rpc.calls.clear()
        self.sync()
        self.assertNotIn("getInflationReward", self.rpc.calls)
        self.assertNotIn("getTransaction", self.rpc.calls)


if __name__ == "__main__":
    unittest.main()
