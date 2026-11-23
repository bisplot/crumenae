import unittest
from decimal import Decimal
from unittest import mock

from crumenae import sync
from crumenae.adapters import cosmos, get_adapter
from crumenae.http import HttpError
from crumenae.adapters.cosmos import bech32_decode, bech32_encode, module_address

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


def search(params, rows):
    """Like PublicNode: no height range in the query, newest first, paged."""
    if "tx.height" in params["query"]:
        raise HttpError(400, '{"error" : "Please specify tx.height with strict equality."}', "u")
    assert params["order_by"] == "ORDER_BY_DESC"
    rows = sorted(rows, key=lambda r: -int(r["height"]))
    page, limit = int(params["page"]), int(params["limit"])
    return {"tx_responses": rows[(page - 1) * limit:page * limit], "total": str(len(rows)), "pagination": None}


FCD_PAGE = 2


def to_fcd(r, old=False):
    """An LCD test transaction as the FCD returns it: amino tx, events in logs. `old`: the format
    of older blocks (no msg_index, fee not in the events)."""
    mtype = r["tx"]["body"]["messages"][0]["@type"].rsplit(".", 1)[-1]
    module = "staking" if "Delegate" in mtype else "bank"
    fee = next((a["value"] for e in r["events"] if e["type"] == "tx" for a in e["attributes"] if a["key"] == "fee"), "")
    signer = ME if fee else PEER
    events = r["events"]
    if old:   # one event per type, attributes of all events of that type concatenated, no msg_index
        merged = {}
        for e in events:
            if e["type"] != "tx" and any(a["key"] == "msg_index" for a in e["attributes"]):
                merged.setdefault(e["type"], []).extend(a for a in e["attributes"] if a["key"] != "msg_index")
        events = [{"type": k, "attributes": v} for k, v in merged.items()]
    amount = [{"denom": "uluna", "amount": fee.replace("uluna", "")}] if fee else []
    return {"id": int(r["height"]), "txhash": r["txhash"], "height": r["height"], "timestamp": r["timestamp"],
            "code": r["code"] or None, "chainId": "columbus-5",
            "logs": [] if r["code"] else [{"msg_index": 0, "log": "", "events": events}],
            "tx": {"type": "core/StdTx", "value": {"fee": {"amount": amount, "gas": "1"},
                                                    "msg": [{"type": f"{module}/{mtype}",
                                                             "value": {"from_address": signer,
                                                                       "delegator_address": signer}}]}}}


def fcd_handler(rows, old=False):
    rows = sorted(rows, key=lambda r: -int(r["height"]))

    def handler(method, url, params, payload):
        if not url.endswith("/v1/txs"):
            return None
        assert params["account"] == ME and int(params["limit"]) in (10, 100)
        ids = [int(r["height"]) for r in rows]
        start = ids.index(int(params["offset"])) + 1 if params.get("offset") else 0
        page = rows[start:start + FCD_PAGE]
        more = start + FCD_PAGE < len(rows)
        return {"next": int(page[-1]["height"]) if more else None, "limit": int(params["limit"]),
                "txs": [to_fcd(r, old) for r in page]}
    return handler


FCD = "https://terra-classic-fcd.publicnode.com"
ALL_TXS = [SEND, RECEIVE, DELEGATE, FAILED]


def lcd_handler(method, url, params, payload):
    if url.endswith("/cosmos/tx/v1beta1/txs"):
        sent = params["query"] == f"message.sender='{ME}'"
        return search(params, [SEND, DELEGATE, FAILED] if sent else [RECEIVE, DELEGATE])
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
        self.t = TempContext(FakeHttp().on("https://terra-classic-lcd.publicnode.com", lcd_handler)
                             .on("https://terra-lcd.publicnode.com", lcd_handler).on(FCD, fcd_handler(ALL_TXS)))
        self.ctx = self.t.ctx
        patcher = mock.patch.object(cosmos, "PAGE", 2)    # pages of 2: paging is exercised
        patcher.start()
        self.addCleanup(patcher.stop)
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
        self.assertEqual(set(res.state["heights"]), {"fcd"})          # Terra Classic: one FCD cursor
        ustc = next(a for a in res.assets if a.key == "terra_classic:uusd")
        self.assertEqual((ustc.symbol, ustc.binance_symbol), ("USTC", "USTC"))

    def test_pruned_node_does_not_hide_new_transactions(self):
        """The node drops old transactions between two syncs: the next sync still reads the new one."""
        luna = get_adapter(self.ctx, self.ctx.networks.get("terra"))
        first = luna.sync(self.wallet, {}, LOG)
        late = tx("EE", 40, "2025-04-01T10:00:00Z", [{"@type": "/cosmos.bank.v1beta1.MsgSend"}],
                  fee_events("2000uluna"))
        calls = []

        def pruned(method, url, params, payload):
            if url.endswith("/cosmos/tx/v1beta1/txs"):
                sent = params["query"] == f"message.sender='{ME}'"
                calls.extend([params["page"]] if sent else [])
                return search(params, [SEND, DELEGATE, FAILED, late] if sent else [])
            return lcd_handler(method, url, params, payload)
        t = TempContext(FakeHttp().on("https://terra-lcd.publicnode.com", pruned))
        try:
            again = get_adapter(t.ctx, t.ctx.networks.get("terra")).sync(self.wallet, first.state, LOG)
            self.assertIn("EE:fee:uluna", {m.uid for m in again.movements})
            self.assertEqual(again.state["heights"]["sent"], 40)
            self.assertEqual(calls, [1, 2])          # page 2 reaches block 20 < 30 (read before): stops there
        finally:
            t.close()

    def test_live_balances_include_staking(self):
        bal, infos = self.adapter.current_balances(self.wallet, [], LOG)
        self.assertEqual(bal["terra_classic:native"], Decimal("8.138"))   # 3.138 liquid + 4 staked + 1 unbonding
        self.assertEqual(bal["terra_classic:uusd"], Decimal(3))
        status = {i.key: i.status for i in infos}
        self.assertEqual(status["terra_classic:ukrw"], "ghost")
        self.assertEqual(status["terra_classic:ibc/ABCDEF0123456789"], "unmapped")
        self.assertTrue(self.adapter.has_history)    # Terra Classic: full history from the FCD
        self.assertFalse(get_adapter(self.ctx, self.ctx.networks.get("terra")).has_history)   # Terra: pruned node

    def test_classic_full_history_from_fcd_and_wallet_check(self):
        """Terra Classic reads every transaction from the FCD, like Terra Finder: the history
        matches the live balance (8.138 LUNC, delegated and unbonding included)."""
        from crumenae import views
        sync.sync_wallet(self.ctx, self.ctx.wallet(self.wallet["id"]), LOG)
        check = views.wallets(self.ctx)[0]["check"]
        self.assertEqual(check["status"], "diff")      # the fake balance holds 2 tokens never received:
        self.assertEqual({a["asset_key"] for a in check["assets"]},     # LUNC and USTC match their history
                         {"terra_classic:ukrw", "terra_classic:ibc/ABCDEF0123456789"})
        fcd_calls = [c for c in self.t.http.calls if c[1].startswith(FCD)]
        self.assertEqual(len(fcd_calls), 2)                                  # 4 transactions, pages of 2
        self.assertFalse([c for c in self.t.http.calls if c[1].endswith("/cosmos/tx/v1beta1/txs")])
        state = self.ctx.sync_state(self.wallet["id"])
        self.t.http.calls.clear()
        again = self.adapter.sync(self.wallet, state, LOG)
        self.assertEqual(len(self.t.http.calls), 1)                          # stops at the block already read
        self.assertEqual({m.uid for m in again.movements}, {"DD:fee:uluna"})   # that block, deduped on store

    def test_classic_old_blocks_format(self):
        """Older blocks: events of a type merged into one (keys repeated), no msg_index, no fee
        events (the fee comes from the tx): same movements as the recent format."""
        expected = sorted((m.uid, m.amount) for m in self.adapter.sync(self.wallet, {}, LOG).movements)
        t = TempContext(FakeHttp().on(FCD, fcd_handler(ALL_TXS, old=True)))
        try:
            old = get_adapter(t.ctx, t.ctx.networks.get("terra_classic")).sync(self.wallet, {}, LOG)
            self.assertEqual(sorted((m.uid, m.amount) for m in old.movements), expected)
        finally:
            t.close()

    def test_wallet_check_says_why_it_is_not_checked(self):
        from crumenae import views
        luna = add_wallet(self.ctx, "terra", ME, label="Luna")
        sync.sync_wallet(self.ctx, self.ctx.wallet(luna["id"]), LOG)
        self.ctx.db.execute("DELETE FROM wallets WHERE id=?", (self.wallet["id"],))
        check = views.wallets(self.ctx)[0]["check"]
        self.assertEqual(check["status"], "n/a")
        self.assertIn("keeps only recent transactions", check["reason"])

    def test_history_unavailable_keeps_balances(self):
        def broken(method, url, params, payload):
            if url.endswith("/v1/txs"):
                return {"type": "INVALID_REQUEST_ERROR", "message": "node error", "code": 400}
            return None
        t = TempContext(FakeHttp().on(FCD, broken).on("https://terra-classic-lcd.publicnode.com", lcd_handler))
        try:
            w = add_wallet(t.ctx, "terra_classic", ME)
            sync.sync_wallet(t.ctx, w, LOG)
            held = {r["asset_key"]: Decimal(r["amount"]) for r in t.ctx.db.query("SELECT * FROM current_balances")}
            self.assertEqual(held["terra_classic:native"], Decimal("8.138"))
            self.assertIn("history not available", t.ctx.db.scalar("SELECT message FROM sync_log WHERE level='warn'"))
        finally:
            t.close()

    def test_terra_and_custom_cosmos_network(self):
        from crumenae.server import App
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
