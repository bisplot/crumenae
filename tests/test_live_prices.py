import unittest
from decimal import Decimal

from crumenae import assets as assets_mod
from crumenae import sync, views
from crumenae.prices import GHOST_SOURCE, PriceService, identity

from helpers import FakeHttp, TempContext, add_wallet

LOG = lambda m: None  # noqa: E731
DEAD = "ethereum:0x" + "d" * 40
SPAM = "ethereum:0x" + "e" * 40
NOPE = "ethereum:0x" + "f" * 40
ECB_CSV = ("KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE,OBS_STATUS\n"
           "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,{day},1.2500,A\n")


def market(method, url, params, payload):
    if url.endswith("/api/v3/ticker/price"):
        return [{"symbol": "ETHEUR", "price": "2500"}, {"symbol": "EURSOL", "price": "0.01"},
                {"symbol": "DEADUSDT", "price": "0.5"}, {"symbol": "BTCUSDT", "price": "90000"}]
    if "data-api.ecb.europa.eu" in url:
        return ECB_CSV.format(day=params["endPeriod"])
    if "/coins/ethereum/contract/" in url:
        return {"id": "deadcoin", "symbol": "dead", "name": "Dead token"}
    return None


class LivePricesTest(unittest.TestCase):
    def setUp(self):
        self.http = FakeHttp().on("https://", market)
        self.t = TempContext(self.http)
        ctx = self.ctx = self.t.ctx
        ctx.settings.set("current_price_providers", ["binance"])
        net = ctx.networks.get("ethereum")
        assets_mod.upsert(ctx, [assets_mod.native_asset(net),
                                assets_mod.native_asset(ctx.networks.get("solana")),
                                assets_mod.token_asset(ctx, net, "0x" + "d" * 40, "DEAD", "Dead token", 18),
                                assets_mod.token_asset(ctx, net, "0x" + "e" * 40, "FREE", "Claim reward", 18),
                                assets_mod.token_asset(ctx, net, "0x" + "f" * 40, "NOPE", "Nope", 18)])
        assets_mod.update_mapping(ctx, DEAD, {"status": "ghost", "binance_symbol": "DEAD"})
        assets_mod.update_mapping(ctx, SPAM, {"status": "spam"})
        assets_mod.update_mapping(ctx, NOPE, {"binance_symbol": "NOPE"})

    def tearDown(self):
        self.t.close()

    def ticker_calls(self):
        return [c for c in self.http.calls if c[1].endswith("/ticker/price")]

    def test_one_ticker_call_for_all_assets(self):
        ps = PriceService(self.ctx)
        keys = ["ethereum:native", "solana:native", DEAD, SPAM, NOPE]
        got = ps.refresh_current({k: assets_mod.get(self.ctx, k) for k in keys})
        self.assertEqual(len(self.ticker_calls()), 1)
        self.assertEqual(got["ethereum:native"].price, Decimal("2500"))           # ETHEUR
        self.assertEqual(got["solana:native"].price, Decimal("100"))              # 1 / EURSOL
        self.assertEqual(got[DEAD].price, Decimal("0.4"))                         # DEADUSDT at the ECB rate
        self.assertNotIn(SPAM, got)                                               # spam is never priced
        self.assertNotIn(NOPE, got)

    def test_misses_are_remembered_for_a_day(self):
        nope = assets_mod.get(self.ctx, NOPE)
        PriceService(self.ctx).refresh_current({NOPE: nope})
        PriceService(self.ctx).refresh_current({NOPE: nope})
        self.assertEqual(len(self.ticker_calls()), 1)                            # second run: not asked again
        self.ctx.db.set_meta(f"live_miss:{identity(nope)}", "0")       # a day later
        PriceService(self.ctx).refresh_current({NOPE: nope})
        self.assertEqual(len(self.ticker_calls()), 2)

    def test_ghost_uses_live_price_when_a_market_exists(self):
        ps = PriceService(self.ctx)
        dead = assets_mod.get(self.ctx, DEAD)
        self.assertEqual((ps.latest(dead).price, ps.latest(dead).source), (0, GHOST_SOURCE))
        ps.refresh_current({DEAD: dead})
        self.assertEqual(PriceService(self.ctx).latest(dead).price, Decimal("0.4"))

    def test_ghost_token_is_looked_up_by_contract_and_stays_ghost(self):
        assets_mod.update_mapping(self.ctx, DEAD, {"status": "ghost", "binance_symbol": ""})
        w = add_wallet(self.ctx, "ethereum", "0x" + "a" * 40)
        self.ctx.db.execute("INSERT INTO current_balances(wallet_id, asset_key, amount, fetched_at) VALUES(?,?,?,?)",
                            (w["id"], DEAD, "5", 1))
        sync.lookup_unknown_tokens(self.ctx, PriceService(self.ctx), LOG)
        a = assets_mod.get(self.ctx, DEAD)
        self.assertEqual((a["coingecko_id"], a["status"]), ("deadcoin", "ghost"))

    def test_dashboard_hides_ghost_and_spam(self):
        w = add_wallet(self.ctx, "ethereum", "0x" + "a" * 40)
        net = self.ctx.networks.get("ethereum")
        assets_mod.upsert(self.ctx, [assets_mod.token_asset(self.ctx, net, "0x" + "c" * 40, "AIRDROP", "Visit x.io", 18)])
        for key, amount in (("ethereum:native", "2"), (DEAD, "1000"), (SPAM, "99"), ("ethereum:0x" + "c" * 40, "7")):
            self.ctx.db.execute("INSERT INTO current_balances(wallet_id, asset_key, amount, fetched_at)"
                                " VALUES(?,?,?,?)", (w["id"], key, amount, 1))
        PriceService(self.ctx).refresh_current({k: assets_mod.get(self.ctx, k) for k in ("ethereum:native", DEAD)})
        d = views.dashboard(self.ctx)
        self.assertEqual([h["symbol"] for h in d["holdings"]], ["ETH"])
        self.assertEqual((d["ghost_hidden"], d["spam_hidden"]), (1, 1))
        self.assertEqual(Decimal(d["total_now"]), Decimal("5000"))               # ghost value not in the total


if __name__ == "__main__":
    unittest.main()
