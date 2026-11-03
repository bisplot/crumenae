import csv
import io
import sqlite3
import unittest
from decimal import Decimal

from cryptoledger import assets as assets_mod
from cryptoledger import classify, exports, ledger, yearend
from cryptoledger.db import SCHEMA_VERSION, Database
from cryptoledger.models import AssetInfo, Movement, SyncResult
from cryptoledger.prices import PriceService

from helpers import FakeHttp, TempContext, add_wallet
from test_engine import market_handler

A = "0x" + "a" * 40
B = "0x" + "b" * 40
ROUTER = "0x" + "7" * 40
T0 = 1740000000  # 2025-02-19
BTC_ADDR = "bc1q" + "q" * 38  # well-formed, not a real wallet


def read(text):
    return list(csv.DictReader(io.StringIO(text)))


def mv(uid, h, ts, asset, amount, kind, frm="", to="", cp=""):
    return Movement(uid, h, ts, asset, Decimal(amount), kind, cp, from_address=frm, to_address=to)


class TransactionsExportTest(unittest.TestCase):
    def setUp(self):
        self.t = TempContext()
        ctx = self.ctx = self.t.ctx
        nets = {n.id: n for n in ctx.networks.all()}
        assets_mod.upsert(ctx, [
            assets_mod.native_asset(nets["ethereum"]),
            assets_mod.token_asset(ctx, nets["ethereum"], "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"),
            AssetInfo("ethereum:0xtoka", "ethereum", "TOKA", status="mapped", binance_symbol="TOKA"),
            AssetInfo("ethereum:0xtokb", "ethereum", "TOKB", status="mapped", binance_symbol="TOKB"),
            AssetInfo("ethereum:0xlp", "ethereum", "LP", status="mapped", binance_symbol="LP"),
            AssetInfo("ethereum:0xspam", "ethereum", "CLAIM", status="unmapped"),
            assets_mod.ticker_asset(ctx, "binance", "BTC"),
        ])
        self.a = add_wallet(ctx, "ethereum", A, label="Wallet A", platform="MetaMask")
        self.b = add_wallet(ctx, "ethereum", B, label="Wallet B")
        self.ex = add_wallet(ctx, "binance", "", label="Binance")
        eth, usdc = "ethereum:native", "ethereum:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
        ledger.store_sync_result(ctx, self.a, SyncResult(movements=[
            # swap: ETH -> USDC through a router, gas paid in ETH
            mv("s:fee", "0xs1", T0, eth, "-0.0015", "fee", A, ROUTER, ROUTER),
            mv("s:out", "0xs1", T0, eth, "-0.5", "out", A, ROUTER, ROUTER),
            mv("s:in", "0xs1", T0, usdc, "1500", "in", "0xpool", A, "0xpool"),
            # received from a stranger
            mv("r:in", "0xr1", T0 + 100, eth, "1", "in", "0xstranger", A, "0xstranger"),
            # failed transaction: only the fee
            mv("f:fee", "0xf1", T0 + 200, eth, "-0.0002", "fee", A, ROUTER, ROUTER),
            # to my other wallet
            mv("i:out", "0xi1", T0 + 300, eth, "-0.3", "out", A, B, B),
            # reward and airdrop (set by the user below)
            mv("w:in", "0xw1", T0 + 400, eth, "0.01", "in", "0xstaking", A, "0xstaking"),
            mv("d:in", "0xd1", T0 + 500, usdc, "50", "in", "0xdrop", A, "0xdrop"),
            # spam: never exported
            mv("x:in", "0xx1", T0 + 600, "ethereum:0xspam", "0", "in", "0xspammer", A, "0xspammer"),
            # liquidity deposit: two tokens out, one LP token in
            mv("l:a", "0xl1", T0 + 700, "ethereum:0xtoka", "-10", "out", A, ROUTER, ROUTER),
            mv("l:b", "0xl1", T0 + 700, "ethereum:0xtokb", "-5", "out", A, ROUTER, ROUTER),
            mv("l:lp", "0xl1", T0 + 700, "ethereum:0xlp", "1", "in", ROUTER, A, ROUTER),
            # partial refund of the same asset nets out
            mv("p:out", "0xp1", T0 + 800, eth, "-1", "out", A, ROUTER, ROUTER),
            mv("p:back", "0xp1", T0 + 800, eth, "0.2", "in", ROUTER, A, ROUTER),
            # previous fiscal year: not in transactions_2025.csv
            mv("old:in", "0xold", 1700000000, eth, "5", "in", "0xstranger", A, "0xstranger"),
        ]))
        ledger.store_sync_result(ctx, self.b, SyncResult(movements=[
            mv("i:in", "0xi1", T0 + 300, eth, "0.3", "in", A, B, A)]))
        ledger.store_sync_result(ctx, self.ex, SyncResult(movements=[
            # exchange withdrawal without on-chain hash: amount and fee share the uid stem
            mv("wd:9", "", T0 + 900, "binance:BTC", "-0.1", "out", "", "bc1qdest", "bc1qdest"),
            mv("wd:9:fee", "", T0 + 900, "binance:BTC", "-0.0002", "fee", "", "bc1qdest"),
        ]))
        classify.run(ctx)
        for uid, cat in (("w:in", "reward"), ("d:in", "airdrop")):
            mid = ctx.db.scalar("SELECT id FROM movements WHERE uid=?", (uid,))
            classify.set_category(ctx, mid, cat)
        # EUR values at the time (normally filled by the sync)
        prices = {eth: "2500", usdc: "0.85", "ethereum:0xtoka": "2", "ethereum:0xtokb": "4",
                  "ethereum:0xlp": "40", "binance:BTC": "80000"}
        for key, price in prices.items():
            ctx.db.execute("UPDATE movements SET price_eur=?, value_eur=CAST(amount AS REAL)*? WHERE asset_key=?",
                           (price, float(price), key))
        self.rows = read(exports.transactions_csv(ctx, 2025))

    def tearDown(self):
        self.t.close()

    def by_hash(self, h):
        return [r for r in self.rows if r["tx_hash"] == h]

    def test_header_is_exact(self):
        header = exports.transactions_csv(self.ctx, 2025).splitlines()[0]
        self.assertEqual(header, "datetime,wallet,network,tx_hash,type,asset_in,qty_in,asset_out,qty_out,"
                                 "fee_asset,fee_qty,value_eur,from_address,to_address")

    def test_swap_is_one_row(self):
        (r,) = self.by_hash("0xs1")
        self.assertEqual((r["type"], r["asset_in"], r["qty_in"], r["asset_out"], r["qty_out"], r["fee_asset"],
                          r["fee_qty"]), ("swap", "USDC", "1500", "ETH", "0.5", "ETH", "0.0015"))
        self.assertEqual(r["value_eur"], "1275.00")          # value received: 1500 USDC x 0.85
        self.assertEqual((r["from_address"], r["to_address"]), (A, ROUTER))
        self.assertEqual(r["datetime"], "2025-02-19T22:20:00+01:00")
        self.assertEqual((r["wallet"], r["network"]), ("Wallet A", "ethereum"))

    def test_transfers_fee_rewards(self):
        (r,) = self.by_hash("0xr1")
        self.assertEqual((r["type"], r["asset_in"], r["qty_in"], r["value_eur"], r["from_address"], r["to_address"]),
                         ("transfer_in", "ETH", "1", "2500.00", "0xstranger", A))
        (f,) = self.by_hash("0xf1")
        self.assertEqual((f["type"], f["fee_asset"], f["fee_qty"], f["asset_in"], f["asset_out"]),
                         ("fee", "ETH", "0.0002", "", ""))
        own = {r["wallet"]: r["type"] for r in self.by_hash("0xi1")}
        self.assertEqual(own, {"Wallet A": "transfer_out", "Wallet B": "transfer_in"})
        self.assertEqual(self.by_hash("0xw1")[0]["type"], "reward")
        self.assertEqual(self.by_hash("0xd1")[0]["type"], "airdrop")
        self.assertEqual(self.by_hash("0xx1"), [])                    # spam left out
        self.assertEqual(self.by_hash("0xold"), [])                   # other fiscal year

    def test_multi_leg_and_refund(self):
        lp = self.by_hash("0xl1")
        self.assertEqual([(r["type"], r["asset_in"], r["asset_out"], r["qty_out"]) for r in lp],
                         [("swap", "LP", "TOKA", "10"), ("swap", "", "TOKB", "5")])
        (p,) = self.by_hash("0xp1")
        self.assertEqual((p["type"], p["asset_out"], p["qty_out"], p["value_eur"]), ("transfer_out", "ETH", "0.8", "2000.00"))

    def test_exchange_withdrawal_with_fee(self):
        (w,) = [r for r in self.rows if r["wallet"] == "Binance"]
        self.assertEqual((w["type"], w["asset_out"], w["qty_out"], w["fee_asset"], w["fee_qty"], w["to_address"]),
                         ("transfer_out", "BTC", "0.1", "BTC", "0.0002", "bc1qdest"))

    def test_only_allowed_types(self):
        self.assertTrue({r["type"] for r in self.rows} <= set(exports.TX_TYPES))


class BalancesAndWalletsExportTest(unittest.TestCase):
    def setUp(self):
        self.t = TempContext(FakeHttp().on("https://", market_handler))
        ctx = self.ctx = self.t.ctx
        ctx.settings.set("first_fiscal_year", 2025)
        nets = {n.id: n for n in ctx.networks.all()}
        assets_mod.upsert(ctx, [assets_mod.native_asset(nets["bitcoin"]),
                                AssetInfo("bitcoin:mystery", "bitcoin", "MYST", status="unmapped")])
        self.btc = add_wallet(ctx, "bitcoin", BTC_ADDR, label="Cold storage", platform="Ledger")
        add_wallet(ctx, "binance", "", label="Exchange")
        add_wallet(ctx, "manual", "", label="Other")
        ledger.store_sync_result(ctx, self.btc, SyncResult(movements=[
            Movement("a", "h1", T0, "bitcoin:native", Decimal("0.4"), "in", "x"),
            Movement("b", "h2", T0, "bitcoin:mystery", Decimal("3"), "in", "x")]))
        yearend.compute_all(ctx, PriceService(ctx))

    def tearDown(self):
        self.t.close()

    def test_balances_file(self):
        self.assertEqual(exports.balances_name(self.ctx, 2025), "balances_2025-12-31.csv")
        text = exports.balances_csv(self.ctx, 2025)
        self.assertEqual(text.splitlines()[0], "wallet,network,asset,quantity,price_eur,value_eur,price_source")
        rows = {r["asset"]: r for r in read(text)}
        self.assertEqual((rows["BTC"]["quantity"], rows["BTC"]["price_eur"], rows["BTC"]["value_eur"]),
                         ("0.4", "80000", "32000.00"))
        self.assertIn("BTCEUR 1m close", rows["BTC"]["price_source"])
        self.assertEqual((rows["MYST"]["price_eur"], rows["MYST"]["value_eur"]), ("", ""))
        self.assertTrue(rows["MYST"]["price_source"].startswith("none: "))

    def test_wallets_file_and_rewrites(self):
        rows = read(exports.wallets_csv(self.ctx))
        self.assertEqual([(r["wallet"], r["network"], r["address"], r["platform"]) for r in rows],
                         [("Cold storage", "bitcoin", BTC_ADDR, "Ledger"), ("Exchange", "binance", "", "Binance"),
                          ("Other", "manual", "", "manual")])
        changed = exports.write_all(self.ctx)
        self.assertEqual(sorted(changed), ["balances_2025-12-31.csv", "transactions_2025.csv", "wallets.csv"])
        self.assertEqual(exports.write_all(self.ctx), [])            # nothing changed: files untouched
        folder = exports.exports_dir(self.ctx)
        self.assertTrue((folder / "balances_2025-12-31.csv").is_file())
        # Editing a wallet through the API rewrites wallets.csv.
        from cryptoledger.server import App
        App(self.ctx).dispatch("PUT", f"/api/wallets/{self.btc['id']}", {}, {"platform": "Trezor"})
        self.assertIn("Trezor", (folder / "wallets.csv").read_text())

    def test_download_endpoint(self):
        from cryptoledger.server import App, ApiError, FileResponse
        app = App(self.ctx)
        f = app.dispatch("GET", "/api/exports/balances_2025-12-31.csv", {}, None)
        self.assertIsInstance(f, FileResponse)
        self.assertEqual(f.filename, "balances_2025-12-31.csv")
        with self.assertRaises(ApiError):
            app.dispatch("GET", "/api/exports/balances_2025-06-30.csv", {}, None)   # not this fiscal-year end
        listing = app.dispatch("GET", "/api/exports", {}, None)
        self.assertEqual(listing["years"][-1]["complete"], False)               # current year: transactions only


class MigrationTest(unittest.TestCase):
    def test_v1_database_gets_new_columns(self):
        t = TempContext()
        path = t.ctx.db.path.replace("cryptoledger.db", "old.db")
        old = sqlite3.connect(path)
        old.executescript("""
            CREATE TABLE wallets (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, network TEXT NOT NULL,
                address TEXT NOT NULL DEFAULT '', portfolio TEXT NOT NULL DEFAULT 'Personal', rw_slot INTEGER,
                bollo_applied INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
                notes TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, last_sync_at INTEGER, last_error TEXT);
            CREATE TABLE movements (id INTEGER PRIMARY KEY AUTOINCREMENT, wallet_id INTEGER NOT NULL, uid TEXT NOT NULL,
                tx_hash TEXT NOT NULL DEFAULT '', ts INTEGER NOT NULL, asset_key TEXT NOT NULL, amount TEXT NOT NULL,
                kind TEXT NOT NULL, counterparty TEXT NOT NULL DEFAULT '', failed INTEGER NOT NULL DEFAULT 0,
                balance_after TEXT, info TEXT NOT NULL DEFAULT '', category TEXT,
                category_source TEXT NOT NULL DEFAULT 'auto', note TEXT NOT NULL DEFAULT '', price_eur TEXT,
                value_eur TEXT, price_source TEXT, UNIQUE (wallet_id, uid));
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO meta VALUES ('schema_version', '1');
            INSERT INTO wallets(label, network, created_at) VALUES ('Old', 'manual', 0);
        """)
        old.commit()
        old.close()
        try:
            db = Database(path)
            cols = {r["name"] for r in db.query("PRAGMA table_info(movements)")}
            self.assertTrue({"from_address", "to_address"} <= cols)
            self.assertEqual(db.query("SELECT label, platform FROM wallets"), [{"label": "Old", "platform": ""}])
            self.assertEqual(db.get_meta("schema_version"), str(SCHEMA_VERSION))
            db.close()
        finally:
            t.close()


if __name__ == "__main__":
    unittest.main()
