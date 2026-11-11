"""End to end: three wallets on three chains, one full sync, then the dashboard
and the year-end report, all through the same code paths the UI uses."""
import unittest
from decimal import Decimal

from crumenae import report, views
from crumenae.sync import recompute, run_sync

from helpers import FakeHttp, TempContext, add_wallet, fixture
from test_adapters import ME, BTC_ME, btc_handler, etherscan_handler, solana_handler
from test_engine import market_handler


def coingecko_now(method, url, params, payload):
    if url.endswith("/simple/price"):
        return {i: {"eur": 1.0, "last_updated_at": 1762000000} for i in params["ids"].split(",")}
    return None


class FullSyncTest(unittest.TestCase):
    def test_full_sync(self):
        http = (FakeHttp()
                .on("https://api.etherscan.io/v2/api", etherscan_handler)
                .on("https://api.mainnet-beta.solana.com", solana_handler)
                .on("https://mempool.space/api", btc_handler)
                .on("https://api.coingecko.com", coingecko_now)
                .on("https://", market_handler))
        t = TempContext(http)
        try:
            ctx = t.ctx
            ctx.secrets.set("etherscan_api_key", "KEY")
            ctx.settings.set("first_fiscal_year", 2024)
            for net, addr in (("ethereum", ME), ("solana", fixture("solana.json")["owner"]), ("bitcoin", BTC_ME)):
                add_wallet(ctx, net, addr, label=net, synced=False)
            lines = []
            result = run_sync(ctx, lines.append)
            self.assertEqual(result["errors"], 0, "\n".join(lines))
            # Sync does not compute year-end values: it marks them out of date.
            self.assertEqual(ctx.db.scalar("SELECT COUNT(*) FROM year_end"), 0)
            self.assertTrue(ctx.yearend_stale())
            self.assertEqual(recompute(ctx, lines.append), [2024, 2025])     # the "Recompute" button
            self.assertIsNone(ctx.yearend_stale())

            # Year-end 2025: every wallet has a source and a value.
            detail = views.year_detail(ctx, 2025)
            by_net = {w["wallet"]["network"]: w for w in detail["wallets"]}
            self.assertEqual(by_net["bitcoin"]["source"], "reconstructed")
            # SOL comes from chain-reported post-tx balances, USDC from summed transfers:
            # the wallet-level label is the conservative one.
            self.assertEqual(by_net["solana"]["source"], "reconstructed")
            sol = {a["symbol"]: a for a in by_net["solana"]["assets"]}
            self.assertEqual(Decimal(sol["SOL"]["amount"]), Decimal("1.99999"))
            self.assertEqual(Decimal(sol["SOL"]["price_eur"]), Decimal("118") / Decimal("1.18"))
            eth = {a["symbol"]: a for a in by_net["ethereum"]["assets"]}
            self.assertEqual(Decimal(eth["ETH"]["value_eur"]), Decimal("1.59829") * Decimal("2500.50"))

            # Transactions got an EUR value at their own time.
            valued = ctx.db.scalar("SELECT COUNT(*) FROM movements WHERE value_eur IS NOT NULL")
            self.assertGreater(valued, 5)

            # Dashboard: live prices from CoinGecko (fake: 1 EUR each), spam token excluded.
            dash = views.dashboard(ctx)
            self.assertTrue(dash["holdings"])
            self.assertNotIn("CLAIM", [h["symbol"] for h in dash["holdings"] if h["priced"]])

            rep = report.build(ctx, 2025)
            self.assertEqual({w["network"] for w in rep["wallets"] if w["held"]}, {"ethereum", "solana", "bitcoin"})
            folder = report.write_folder(ctx, 2025)
            self.assertTrue((folder / "rw_crypto.csv").exists() and (folder / "evidence.md").exists())
        finally:
            t.close()


if __name__ == "__main__":
    unittest.main()
