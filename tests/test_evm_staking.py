"""Contract staking on EVM chains: Ronin's staking contract and StakeWise v3 vaults (MetaMask
Pooled Staking), read with eth_getLogs / eth_call on a fake JSON-RPC chain."""
import unittest
from decimal import Decimal

from cryptoledger import classify, sync, views
from cryptoledger.adapters import get_adapter
from cryptoledger.adapters import evm_staking as es

from helpers import FakeHttp, TempContext, add_wallet

LOG = lambda m: None  # noqa: E731
E18 = 10 ** 18
T0 = 1_735_689_600            # 2025-01-01 00:00 UTC
HEAD = 5000
USER = "0x" + "a" * 40
PROXY = "0x545edb750eb8769c868429be9586f5857a768758"
POOL1 = "0x" + "1" * 40
POOL2 = "0x" + "2" * 40
VAULT = "0x" + "5" * 40
OTHER = "0x" + "6" * 40       # a contract that is not a vault


def word(n):
    return format(int(n), "x").rjust(64, "0")


def topic(addr):
    return "0x" + addr[2:].rjust(64, "0")


def entry(contract, block, index, topics, *data):
    return {"address": contract, "blockNumber": hex(block), "logIndex": hex(index),
            "transactionHash": f"0x{block:064x}", "topics": topics, "data": "0x" + "".join(word(d) for d in data)}


def transfer(frm, to, value, block, block_time, category="external"):
    return {"category": category, "blockNum": hex(block), "from": frm, "to": to, "asset": "ETH",
            "hash": f"0x{block:064x}", "rawContract": {"value": hex(value), "decimal": "0x12"},
            "metadata": {"blockTimestamp": block_time(block)}}


def iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def matches(e, q):
    if e["address"] != q.get("address", "").lower():
        return False
    if not int(q["fromBlock"], 16) <= int(e["blockNumber"], 16) <= int(q["toBlock"], 16):
        return False
    for i, want in enumerate(q["topics"]):
        if want is None:
            continue
        have = e["topics"][i] if i < len(e["topics"]) else None
        if have not in (want if isinstance(want, list) else [want]):
            return False
    return True


class Chain:
    """A fake JSON-RPC node: blocks every `spacing` seconds, logs, transfers and contract reads."""

    def __init__(self, spacing, logs, transfers, call, max_range=2500):
        self.spacing, self.logs, self.transfers, self.call, self.max_range = spacing, logs, transfers, call, max_range

    def handler(self, method, url, params, payload):
        m, p = payload["method"], payload["params"]

        def ok(result):
            return {"jsonrpc": "2.0", "id": 1, "result": result}
        if m == "eth_blockNumber":
            return ok(hex(HEAD))
        if m == "eth_getBlockByNumber":
            return ok({"timestamp": hex(T0 + self.spacing * int(p[0], 16))})
        if m == "eth_getLogs":
            q = p[0]
            if int(q["toBlock"], 16) - int(q["fromBlock"], 16) > self.max_range:
                return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": "block range too large"}}
            return ok([e for e in self.logs if matches(e, q)])
        if m == "eth_call":
            tag = p[1]
            block = HEAD if tag == "latest" else int(tag, 16)
            return ok(self.call(p[0]["to"].lower(), p[0]["data"], block))
        if m == "eth_getBalance":
            return ok(hex(10 * E18))
        if m == "alchemy_getAssetTransfers":
            side = "from" if "fromAddress" in p[0] else "to"
            who = p[0].get("fromAddress") or p[0].get("toAddress")
            return ok({"transfers": [t for t in self.transfers if t[side] == who]})
        if m == "alchemy_getTokenBalances":
            return ok({"address": USER, "tokenBalances": []})
        if m == "eth_getTransactionReceipt":
            return ok({"from": USER, "gasUsed": "0x0", "effectiveGasPrice": "0x0", "status": "0x1"})
        return None


# --- Ronin -----------------------------------------------------------------------------------
RONIN_LOGS = [
    entry(PROXY, 1000, 0, [es.T_DELEGATED, topic(USER), topic(POOL1)], 100 * E18),
    entry(PROXY, 2000, 0, [es.T_REWARD_CLAIMED, topic(POOL1), topic(USER)], 5 * E18),        # claimRewards
    entry(PROXY, 3000, 0, [es.T_REWARD_CLAIMED, topic(POOL1), topic(USER)], 2 * E18),        # delegateRewards:
    entry(PROXY, 3000, 1, [es.T_DELEGATED, topic(USER), topic(POOL2)], 2 * E18),             # ...into POOL2
    entry(PROXY, 4000, 0, [es.T_UNDELEGATED, topic(USER), topic(POOL1)], 40 * E18),
    entry(PROXY, 4100, 0, [es.T_DELEGATED, topic("0x" + "9" * 40), topic(POOL1)], 7 * E18),  # someone else
]


def ronin_staked(pool, block):
    total = 0
    for e in RONIN_LOGS:
        if int(e["blockNumber"], 16) > block or e["topics"][1] != topic(USER):
            continue
        if e["topics"][2] == topic(pool) and e["topics"][0] in (es.T_DELEGATED, es.T_UNDELEGATED):
            amount = int(e["data"][2:66], 16)
            total += amount if e["topics"][0] == es.T_DELEGATED else -amount
    return total


def ronin_call(to, data, block):
    assert to == PROXY
    words = es._words("0x" + data[10:])
    if data.startswith(es.GET_MANY_STAKING):
        n = words[2]
        pools = ["0x" + format(w, "040x") for w in words[3:3 + n]]
        values = [ronin_staked(p, block) for p in pools]
    elif data.startswith(es.GET_REWARDS):
        n = words[2]
        pools = ["0x" + format(w, "040x") for w in words[3:3 + n]]
        values = [E18 if p == POOL1 else 0 for p in pools]
    else:
        raise AssertionError(data)
    return "0x" + word(32) + word(len(values)) + "".join(word(v) for v in values)


class RoninStakingTest(unittest.TestCase):
    def setUp(self):
        stamp = lambda b: iso(T0 + 3 * b)  # noqa: E731
        self.chain = Chain(3, RONIN_LOGS, [transfer(USER, PROXY, 100 * E18, 1000, stamp)], ronin_call)
        self.http = FakeHttp().on("https://ronin-mainnet.g.alchemy.com/v2/", self.chain.handler)
        self.t = TempContext(self.http)
        self.ctx = self.t.ctx
        self.ctx.secrets.set("alchemy_api_key", "A")
        self.w = add_wallet(self.ctx, "ronin", USER, label="Ronin")
        self.key = f"ronin:stake:{PROXY}"

    def tearDown(self):
        self.t.close()

    def moves(self):
        return self.ctx.db.query("SELECT * FROM movements WHERE wallet_id=? ORDER BY ts, id", (self.w["id"],))

    def get_logs(self):
        return [c[3]["params"][0] for c in self.http.calls if c[3] and c[3].get("method") == "eth_getLogs"]

    def test_history_rewards_and_positions(self):
        sync.sync_wallet(self.ctx, self.w, LOG)
        classify.run(self.ctx)
        stake = [m for m in self.moves() if m["asset_key"] == self.key]
        self.assertEqual(sum(Decimal(m["amount"]) for m in stake), Decimal(62))
        self.assertEqual(sum(Decimal(m["amount"]) for m in stake if m["category"] == "reward"), Decimal(7))
        self.assertEqual({m["category"] for m in stake if "reward:" not in m["info"]}, {"internal_transfer"})
        out = [m for m in self.moves() if m["asset_key"] == "ronin:native" and m["kind"] == "out"]
        self.assertEqual([m["category"] for m in out], ["internal_transfer"])   # delegating is not a disposal
        # Live: 60 RON in POOL1 (1 RON to claim) and 2 in POOL2.
        cur = {r["asset_key"]: Decimal(r["amount"]) for r in self.ctx.db.query(
            "SELECT * FROM current_balances WHERE wallet_id=?", (self.w["id"],))}
        self.assertEqual(cur[self.key], Decimal(62))
        pos = {r["validator"]: r for r in views.staking(self.ctx, 2025)["staked"]}
        self.assertEqual(Decimal(pos[f"validator {POOL1[:6]}…{POOL1[-4:]}"]["staked"]), Decimal(60))
        self.assertEqual(Decimal(pos[f"validator {POOL1[:6]}…{POOL1[-4:]}"]["pending_rewards"]), Decimal(1))
        self.assertEqual(Decimal(pos[f"validator {POOL2[:6]}…{POOL2[-4:]}"]["staked"]), Decimal(2))
        a = self.ctx.db.query("SELECT symbol, name FROM assets WHERE key=?", (self.key,))[0]
        self.assertEqual(a["symbol"], "RON (staked)")
        self.assertTrue(a["name"].startswith("Ronin staking"))
        # The node refused the long range: smaller ranges were read instead.
        spans = [int(q["toBlock"], 16) - int(q["fromBlock"], 16) for q in self.get_logs()]
        self.assertTrue(any(s > 2500 for s in spans) and any(s < 2000 for s in spans))

    def test_second_sync_reads_only_new_blocks(self):
        sync.sync_wallet(self.ctx, self.w, LOG)
        before = len(self.get_logs())
        sync.sync_wallet(self.ctx, self.ctx.wallet(self.w["id"]), LOG)
        self.assertEqual(len(self.get_logs()), before)            # nothing new since HEAD
        self.assertEqual(sum(1 for m in self.moves() if m["asset_key"] == self.key), 7)

    def test_year_end_reads_the_contract_at_the_cutoff_block(self):
        sync.sync_wallet(self.ctx, self.w, LOG)
        adapter = get_adapter(self.ctx, self.ctx.networks.get("ronin"))
        at = adapter.balance_at(self.ctx.wallet(self.w["id"]), T0 + 3 * 2500, [self.key], LOG)
        self.assertEqual(at[self.key], Decimal(100))              # before the redelegation and the undelegation


# --- StakeWise v3 vault (MetaMask Pooled Staking) -------------------------------------------
VAULT_LOGS = [
    entry(VAULT, 1000, 0, [es.T_DEPOSITED, topic(USER), topic(USER)], 10 * E18, 10 * E18, 0),
    entry(VAULT, 4000, 0, [es.T_EXIT_ENTERED, topic(USER), topic(USER)], 7, 4 * E18),
    entry(VAULT, 4500, 0, [es.T_EXIT_CLAIMED, topic(USER)], 7, 0, 44 * E18 // 10),
]


def vault_call(to, data, block):
    if to != VAULT:
        return "0x"                                   # not a vault: no answer
    if data.startswith(es.GET_SHARES):
        return "0x" + word(0 if block < 1000 else 10 * E18 if block < 4000 else 6 * E18)
    if data.startswith(es.CONVERT_TO_ASSETS):
        shares = int(data[10:], 16)
        return "0x" + word(shares if block < 3000 else shares * 11 // 10)   # rewards: +10% from block 3000
    raise AssertionError(data)


class VaultStakingTest(unittest.TestCase):
    def setUp(self):
        stamp = lambda b: iso(T0 + 12 * b)  # noqa: E731
        transfers = [transfer(USER, OTHER, E18, 500, stamp), transfer(USER, VAULT, 10 * E18, 1000, stamp),
                     transfer(VAULT, USER, 44 * E18 // 10, 4500, stamp, "internal")]
        self.chain = Chain(12, VAULT_LOGS, transfers, vault_call, max_range=10 ** 9)
        self.http = FakeHttp().on("https://eth-mainnet.g.alchemy.com/v2/", self.chain.handler)
        self.t = TempContext(self.http)
        self.ctx = self.t.ctx
        self.ctx.secrets.set("alchemy_api_key", "A")
        self.w = add_wallet(self.ctx, "ethereum", USER, label="MetaMask")
        self.key = f"ethereum:stake:{VAULT}"

    def tearDown(self):
        self.t.close()

    def test_vault_found_history_and_value(self):
        sync.sync_wallet(self.ctx, self.w, LOG)
        classify.run(self.ctx)
        moves = self.ctx.db.query("SELECT * FROM movements WHERE wallet_id=? ORDER BY ts, id", (self.w["id"],))
        stake = [m for m in moves if m["asset_key"] == self.key]
        self.assertEqual([Decimal(m["amount"]) for m in stake], [Decimal(10), Decimal("-4.4")])
        self.assertEqual([Decimal(m["balance_after"]) for m in stake], [Decimal(10), Decimal("6.6")])
        cats = {m["counterparty"]: m["category"] for m in moves if m["asset_key"] == "ethereum:native"}
        self.assertEqual(cats[VAULT], "internal_transfer")       # deposit and exit: not disposals
        self.assertEqual(cats[OTHER], "withdrawal")
        self.assertFalse(any(m["category"] == "reward" for m in moves))   # rewards are in the value
        state = self.ctx.sync_state(self.w["id"])["staking"]
        self.assertEqual(list(state["contracts"]), [VAULT])
        self.assertIn(OTHER, state["checked"])
        cur = {r["asset_key"]: Decimal(r["amount"]) for r in self.ctx.db.query(
            "SELECT * FROM current_balances WHERE wallet_id=?", (self.w["id"],))}
        self.assertEqual(cur[self.key], Decimal("6.6"))
        pos = views.staking(self.ctx, 2025)["staked"][0]
        self.assertEqual(Decimal(pos["staked"]), Decimal("6.6"))
        self.assertIn("rewards so far 1.000000", pos["validator"])
        self.assertEqual(self.ctx.db.scalar("SELECT symbol FROM assets WHERE key=?", (self.key,)), "ETH (staked)")

    def test_exit_queue_counts_at_year_end(self):
        sync.sync_wallet(self.ctx, self.w, LOG)
        adapter = get_adapter(self.ctx, self.ctx.networks.get("ethereum"))
        at = adapter.balance_at(self.ctx.wallet(self.w["id"]), T0 + 12 * 4200, [self.key], LOG)
        self.assertEqual(at[self.key], Decimal("11"))            # 6.6 in shares + 4.4 waiting to exit

    def test_no_key_no_staking_and_no_failure(self):
        self.ctx.secrets.delete("alchemy_api_key")
        self.ctx.secrets.set("etherscan_api_key", "E")
        self.http.on("https://api.etherscan.io/", lambda m, u, p, b: {"status": "1", "message": "OK", "result": []
                                                                      if p.get("action") != "balance" else "0"})
        sync.sync_wallet(self.ctx, self.w, LOG)
        self.assertNotIn("staking", self.ctx.sync_state(self.w["id"]))


if __name__ == "__main__":
    unittest.main()
