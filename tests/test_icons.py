"""Coin icons: bundled files, chosen by CoinGecko id (never by symbol alone), none for spam."""
import re
import unittest
from pathlib import Path

from crumenae import icons, sync, views

from helpers import FakeHttp, TempContext, add_wallet
from test_adapters import BTC_ME, btc_handler

WEB = Path(__file__).resolve().parent.parent / "web"


class IconFilesTest(unittest.TestCase):
    def test_every_icon_exists_and_every_file_is_used(self):
        used = set(icons.BY_COINGECKO.values()) | set(icons.BY_TICKER.values()) | set(icons.BY_FIAT.values())
        files = {p.name for p in (WEB / "icons").iterdir() if p.suffix in (".svg", ".png")}
        self.assertEqual(used - files, set())          # no broken image
        self.assertEqual(files - used, set())          # no unused file in the repository
        for f in files:
            self.assertLess((WEB / "icons" / f).stat().st_size, 20_000, f)   # small files only
        self.assertTrue((WEB / "icons" / "README.md").is_file())             # sources and licences

    def test_chosen_by_id_never_by_symbol(self):
        self.assertEqual(icons.icon_for({"status": "known", "coingecko_id": "usd-coin"}), "icons/usdc.svg")
        self.assertEqual(icons.icon_for({"status": "known", "binance_symbol": "LUNC"}), "icons/lunc.png")
        self.assertEqual(icons.icon_for({"status": "known", "fiat": "EUR"}), "icons/eur.svg")
        # a token calling itself USDC without the real id, or a real coin marked spam: no icon
        self.assertIsNone(icons.icon_for({"status": "known", "symbol": "USDC"}))
        for status in ("spam", "ignored", "unmapped"):
            self.assertIsNone(icons.icon_for({"status": status, "coingecko_id": "usd-coin"}))
        self.assertIsNone(icons.icon_for(None))

    def test_pages_use_the_icon_or_the_dot(self):
        js = (WEB / "app.js").read_text(encoding="utf-8")
        self.assertIn("function coinMark(icon, color)", js)
        self.assertGreaterEqual(len(re.findall(r"coinMark\(", js)), 6)


class IconInViewsTest(unittest.TestCase):
    def test_dashboard_transactions_and_assets_carry_the_icon(self):
        t = TempContext(FakeHttp().on("https://mempool.space/api", btc_handler))
        try:
            w = add_wallet(t.ctx, "bitcoin", BTC_ME, synced=False)
            sync.sync_wallet(t.ctx, t.ctx.wallet(w["id"]), lambda m: None)
            self.assertEqual({h["icon"] for h in views.dashboard(t.ctx)["holdings"]}, {"icons/btc.svg"})
            rows = views.transactions(t.ctx)["rows"]
            self.assertTrue(rows and all(r["icon"] == "icons/btc.svg" for r in rows))
            self.assertNotIn("asset_cg", rows[0])
            self.assertEqual({a["icon"] for a in views.asset_list(t.ctx)}, {"icons/btc.svg"})
        finally:
            t.close()


if __name__ == "__main__":
    unittest.main()
