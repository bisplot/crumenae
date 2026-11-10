import unittest
from decimal import Decimal

from cryptoledger import sync
from cryptoledger.adapters import get_adapter
from cryptoledger.adapters.cosmos import bech32_decode, bech32_encode, module_address

from helpers import FakeHttp, TempContext, add_wallet

LOG = lambda m: None  # noqa: E731
ME = bech32_encode("terra", bytes(range(20)))
PEER = bech32_encode("terra", bytes(range(1, 21)))
VALOPER = bech32_encode("terravaloper", bytes(range(2, 22)))
BONDED = module_address("terra", "bonded_tokens_pool")
DISTR = module_address("terra", "distribution")
FEE_COLLECTOR = module_address("terra", "fee_collector")


def ev(type_, **attrs):
    return {"type": type_, "attributes": [{"key": k, "value": v, "index": True} for k, v in attrs.items()]}


def fee_events(amount):
    return [ev("coin_spent", spender=ME, amount=amount), ev("coin_received", receiver=FEE_COLLECTOR, amount=amount),
            ev("tx", fee=amount, fee_payer=ME)]


def tx(h, height, ts, msgs, events, code=0):
    return {"txhash": h, "height": str(height), "timestamp": ts, "code": code,
            "tx": {"body": {"messages": msgs}, "auth_info": {"fee": {"amount": []}}}, "events": events}


SEND = tx("AA", 10, "2025-01-10T10:00:00Z", [{"@type": "/cosmos.bank.v1beta1.MsgSend"}],
          fee_events("5000uluna") + [
              ev("coin_spent", spender=ME, amount="2000000uluna", msg_index="0"),
              ev("coin_received", receiver=PEER, amount="2000000uluna", msg_index="0"),
              ev("transfer", recipient=PEER, sender=ME, amount="2000000uluna", msg_index="0")])
RECEIVE = tx("BB", 5, "2025-01-05T10:00:00Z", [{"@type": "/cosmos.bank.v1beta1.MsgSend"}], [
    ev("coin_spent", spender=PEER, amount="10000000uluna,3000000uusd", msg_index="0"),
    ev("coin_received", receiver=ME, amount="10000000uluna,3000000uusd", msg_index="0"),
    ev("transfer", recipient=ME, sender=PEER, amount="10000000uluna,3000000uusd", msg_index="0")])
DELEGATE = tx("CC", 20, "2025-02-01T10:00:00Z", [{"@type": "/cosmos.staking.v1beta1.MsgDelegate"}],
              fee_events("4000uluna") + [
                  # rewards paid out automatically when the delegation changes
                  ev("coin_spent", spender=DISTR, amount="150000uluna", msg_index="0"),
                  ev("coin_received", receiver=ME, amount="150000uluna", msg_index="0"),
                  ev("transfer", recipient=ME, sender=DISTR, amount="150000uluna", msg_index="0"),
                  ev("coin_spent", spender=ME, amount="5000000uluna", msg_index="0"),
                  ev("coin_received", receiver=BONDED, amount="5000000uluna", msg_index="0")])
FAILED = tx("DD", 30, "2025-03-01T10:00:00Z", [{"@type": "/cosmos.bank.v1beta1.MsgSend"}],
            fee_events("3000uluna"), code=5)


def lcd_handler(method, url, params, payload):
    if url.endswith("/cosmos/tx/v1beta1/txs"):
        q, _, since = params["query"].partition(" AND tx.height>=")
        assert params["order_by"] == "ORDER_BY_ASC"
        rows = [SEND, DELEGATE, FAILED] if q == f"message.sender='{ME}'" else [RECEIVE, DELEGATE]
        rows = [r for r in rows if int(r["height"]) >= int(since or 0)]
        return {"tx_responses": rows, "total": str(len(rows)), "pagination": None}
    if url.endswith(f"/cosmos/bank/v1beta1/balances/{ME}"):
        return {"balances": [{"denom": "uluna", "amount": "3138000"}, {"denom": "uusd", "amount": "3000000"},
                             {"denom": "ukrw", "amount": "55000"}, {"denom": "ibc/ABCDEF0123456789", "amount": "7"}],
                "pagination": {"next_key": None, "total": "4"}}
    if url.endswith(f"/cosmos/staking/v1beta1/delegations/{ME}"):
        return {"delegation_responses": [{"delegation": {"validator_address": VALOPER},
                                          "balance": {"denom": "uluna", "amount": "4000000"}}]}
    if url.endswith(f"/cosmos/staking/v1beta1/delegators/{ME}/unbonding_delegations"):
        return {"unbonding_responses": [{"entries": [{"balance": "1000000"}]}]}
    return None


class CosmosTest(unittest.TestCase):
    def setUp(self):
        self.t = TempContext(FakeHttp().on("https://terra-classic-lcd.publicnode.com", lcd_handler))
        self.ctx = self.t.ctx
        self.adapter = get_adapter(self.ctx, self.ctx.networks.get("terra_classic"))
        self.wallet = add_wallet(self.ctx, "terra_classic", ME)

    def tearDown(self):
        self.t.close()

    def test_addresses(self):
        self.assertEqual(BONDED, "terra1fl48vsnmsdzcv85q5d2q4z5ajdha8yu3nln0mh")   # well-known Terra staking pool
        self.assertEqual(self.adapter.validate_address(ME.upper()), ME)
        self.assertEqual(bech32_decode(ME)[0], "terra")
        for bad in (ME[:-1] + ("q" if ME[-1] != "q" else "p"), "cosmos1" + ME[6:], "0x" + "a" * 40):
            with self.assertRaises(ValueError):
                self.adapter.validate_address(bad)

    def test_sync(self):
        res = self.adapter.sync(self.wallet, {}, LOG)
        got = sorted((m.uid, m.asset_key, m.amount, m.kind) for m in res.movements)
        self.assertEqual(got, sorted([
            ("BB:0:uluna:in", "terra_classic:native", Decimal(10), "in"),
            ("BB:0:uusd:in", "terra_classic:uusd", Decimal(3), "in"),
            ("AA:fee:uluna", "terra_classic:native", Decimal("-0.005"), "fee"),
            ("AA:0:uluna:out", "terra_classic:native", Decimal(-2), "out"),
            ("CC:fee:uluna", "terra_classic:native", Decimal("-0.004"), "fee"),
            ("CC:0:uluna:reward", "terra_classic:native", Decimal("0.15"), "in"),   # delegated 5: not counted as out
            ("DD:fee:uluna", "terra_classic:native", Decimal("-0.003"), "fee"),     # failed: fee only
        ]))
        reward = next(m for m in res.movements if m.uid.endswith(":reward"))
        self.assertTrue(reward.info.startswith("reward:"))
        self.assertEqual(set(res.state["heights"]), {"sent", "received"})
        ustc = next(a for a in res.assets if a.key == "terra_classic:uusd")
        self.assertEqual((ustc.symbol, ustc.binance_symbol), ("USTC", "USTC"))

    def test_pruned_node_does_not_hide_new_transactions(self):
        """The node drops old transactions between two syncs: the next sync still reads the new one."""
        first = self.adapter.sync(self.wallet, {}, LOG)
        late = tx("EE", 40, "2025-04-01T10:00:00Z", [{"@type": "/cosmos.bank.v1beta1.MsgSend"}],
                  fee_events("2000uluna"))

        def pruned(method, url, params, payload):
            if url.endswith("/cosmos/tx/v1beta1/txs"):
                q, _, since = params["query"].partition(" AND tx.height>=")
                rows = [FAILED, late] if q == f"message.sender='{ME}'" else []
                rows = [r for r in rows if int(r["height"]) >= int(since or 0)]
                return {"tx_responses": rows, "total": str(len(rows)), "pagination": None}
            return lcd_handler(method, url, params, payload)
        t = TempContext(FakeHttp().on("https://terra-classic-lcd.publicnode.com", pruned))
        try:
            again = get_adapter(t.ctx, t.ctx.networks.get("terra_classic")).sync(self.wallet, first.state, LOG)
            self.assertIn("EE:fee:uluna", {m.uid for m in again.movements})
            self.assertEqual(again.state["heights"]["sent"], 40)
        finally:
            t.close()

    def test_live_balances_include_staking(self):
        bal, infos = self.adapter.current_balances(self.wallet, [], LOG)
        self.assertEqual(bal["terra_classic:native"], Decimal("8.138"))   # 3.138 liquid + 4 staked + 1 unbonding
        self.assertEqual(bal["terra_classic:uusd"], Decimal(3))
        status = {i.key: i.status for i in infos}
        self.assertEqual(status["terra_classic:ukrw"], "ghost")
        self.assertEqual(status["terra_classic:ibc/ABCDEF0123456789"], "unmapped")
        self.assertFalse(self.adapter.has_history)   # public node: year-end from snapshots, not a partial history

    def test_history_unavailable_keeps_balances(self):
        def broken(method, url, params, payload):
            if url.endswith("/cosmos/tx/v1beta1/txs"):
                return {"error": "node error"}
            return lcd_handler(method, url, params, payload)
        t = TempContext(FakeHttp().on("https://terra-classic-lcd.publicnode.com", broken))
        try:
            w = add_wallet(t.ctx, "terra_classic", ME)
            sync.sync_wallet(t.ctx, w, LOG)
            held = {r["asset_key"]: Decimal(r["amount"]) for r in t.ctx.db.query("SELECT * FROM current_balances")}
            self.assertEqual(held["terra_classic:native"], Decimal("8.138"))
            self.assertIn("history not available", t.ctx.db.scalar("SELECT message FROM sync_log WHERE level='warn'"))
        finally:
            t.close()

    def test_terra_and_custom_cosmos_network(self):
        from cryptoledger.server import App
        self.assertEqual(self.ctx.networks.get("terra").native["symbol"], "LUNA")
        app = App(self.ctx)
        app.dispatch("POST", "/api/providers", {}, {"name": "Cosmos Hub LCD", "type": "cosmos",
                                                   "base_url": "https://cosmos-lcd.example", "needs_key": False})
        app.dispatch("POST", "/api/networks", {}, {"id": "cosmoshub", "name": "Cosmos Hub", "provider": "cosmos_hub_lcd",
                                                  "bech32_prefix": "cosmos", "native_denom": "uatom",
                                                  "native_symbol": "ATOM", "native_coingecko": "cosmos"})
        n = self.ctx.networks.get("cosmoshub")
        self.assertEqual((n.adapter, n.explorer_api, n.bech32_prefix, n.native["decimals"]),
                         ("cosmos", "https://cosmos-lcd.example", "cosmos", 6))
        a = get_adapter(self.ctx, n)
        self.assertEqual(a.validate_address(bech32_encode("cosmos", bytes(20))), bech32_encode("cosmos", bytes(20)))


if __name__ == "__main__":
    unittest.main()
