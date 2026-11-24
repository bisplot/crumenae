"""Portfolio value over time: daily values from the history or recorded balances, daily prices
fetched in ranges and cached, USD pairs converted with ECB rates from one request, unpriced and
partial days, portfolio and lost-wallet filters, and a chart failure never failing a sync."""
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

from crumenae import history, ledger, sync
from crumenae.assets import native_asset
from crumenae.http import HttpError
from crumenae.models import AssetInfo, Movement, Snapshot, SyncResult
from crumenae.util import now

from helpers import FakeHttp, TempContext, add_wallet

LOG = lambda m: None  # noqa: E731
DAY = 86400


def utc_day(ms):
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc).date()


class FakeMarkets:
    """Binance daily klines (BTCEUR: 1000 + day of month; XYZ only against USDT: 2 USD) and ECB
    USD rates (1.25 USD per EUR)."""
    def __init__(self):
        self.klines, self.ecb = [], []

    def __call__(self, method, url, params, payload):
        if url.endswith("/api/v3/klines"):
            sym = params["symbol"]
            self.klines.append(sym)
            assert params["interval"] == "1d"
            if sym not in ("BTCEUR", "XYZUSDT"):
                raise HttpError(400, '{"code":-1121,"msg":"Invalid symbol."}', url)
            rows, t = [], int(params["startTime"])
            while t <= int(params["endTime"]) and len(rows) < int(params["limit"]):
                price = 1000 + utc_day(t).day if sym == "BTCEUR" else 2
                rows.append([t, "0", "0", "0", str(price), "0", t + DAY * 1000 - 1])
                t += DAY * 1000
            return rows
        if "data-api.ecb.europa.eu" in url:
            self.ecb.append((params["startPeriod"], params["endPeriod"]))
            start = datetime.fromisoformat(params["startPeriod"]).date()
            end = datetime.fromisoformat(params["endPeriod"]).date()
            lines, d = ["KEY,TIME_PERIOD,OBS_VALUE"], start
            while d <= end:
                if d.weekday() < 5:                     # no rates on weekends: the last one is used
                    lines.append(f"EXR.D.USD.EUR.SP00.A,{d.isoformat()},1.25")
                d += timedelta(days=1)
            return "\n".join(lines) + "\n"
        return None


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.markets = FakeMarkets()
        self.t = TempContext(FakeHttp().on("https://", self.markets))
        self.ctx = self.t.ctx
        self.ctx.offline = False
        self.cal = self.ctx.calendar
        self.today = self.cal.local_date(now())
        self.noon = lambda days_ago: self.cal.end_of_day(self.today - timedelta(days=days_ago)) - 12 * 3600
        # live price of BTC (what a sync stores): today's point
        self.ctx.db.execute("INSERT INTO prices(price_key, ts, price_eur, source, price_ts, note, fetched_at)"
                            " VALUES('cg:bitcoin', ?, '50000', 'test', ?, 'live', ?)", (now(), now(), now()))

    def tearDown(self):
        self.t.close()

    def btc_wallet(self, **extra):
        w = add_wallet(self.ctx, "bitcoin", "1BoatSLRHtKNngkdXEeobR76b53LETtpyT", **extra)
        ledger.store_sync_result(self.ctx, w, SyncResult(assets=[native_asset(self.ctx.networks.get("bitcoin"))], movements=[
            Movement("m1", "h1", self.noon(5), "bitcoin:native", Decimal("1"), "in", "x"),
            Movement("m2", "h2", self.noon(2), "bitcoin:native", Decimal("-0.4"), "out", "y")]))
        return w

    def points(self, **kw):
        return {p["day"]: p for p in history.series(self.ctx, **kw)["points"]}

    def close(self, days_ago):
        return Decimal(1000 + (self.today - timedelta(days=days_ago)).day)

    def test_values_from_the_history_and_cached_prices(self):
        self.btc_wallet()
        history.compute(self.ctx)
        pts = self.points()
        self.assertEqual(len(pts), 6)                                   # 5 days ago … today
        d5, d1 = (self.today - timedelta(days=5)).isoformat(), (self.today - timedelta(days=1)).isoformat()
        self.assertEqual(Decimal(pts[d5]["value"]), self.close(5))      # 1 BTC × that day's close
        self.assertEqual(Decimal(pts[d1]["value"]), Decimal("0.6") * self.close(1))
        self.assertEqual(Decimal(pts[self.today.isoformat()]["value"]), Decimal("0.6") * 50000)   # live price
        self.assertFalse(any(p["partial"] or p["unpriced"] for p in pts.values()))
        self.assertEqual(self.markets.klines, ["BTCEUR"])                 # one request for every day
        history.compute(self.ctx)
        self.assertEqual(self.markets.klines, ["BTCEUR"])                 # next time: from the cache

    def test_usd_pair_with_one_ecb_request(self):
        w = add_wallet(self.ctx, "ethereum", "0x" + "1" * 40)
        ledger.store_sync_result(self.ctx, w, SyncResult(
            assets=[AssetInfo("ethereum:0xabc", "ethereum", "XYZ", "Xyz", 18, contract="0xabc",
                              binance_symbol="XYZ", status="known")],
            movements=[Movement("x1", "h", self.noon(9), "ethereum:0xabc", Decimal("10"), "in", "z")]))
        history.compute(self.ctx)
        pts = self.points()
        d = (self.today - timedelta(days=3)).isoformat()
        self.assertEqual(Decimal(pts[d]["value"]), Decimal("10") * 2 / Decimal("1.25"))   # 2 USD each
        self.assertEqual(len(self.markets.ecb), 1)
        self.assertEqual(self.markets.klines[:3], ["XYZEUR", "EURXYZ", "XYZUSDT"])

    def test_recorded_balances_partial_and_unpriced(self):
        self.btc_wallet()
        man = add_wallet(self.ctx, "manual", "", label="Ledger Live")
        ledger.store_sync_result(self.ctx, man, SyncResult(
            assets=[AssetInfo("manual:FOO", "manual", "FOO", "Foo", 8, status="unmapped")],
            snapshots=[Snapshot(self.noon(3), "manual:FOO", Decimal("7"), "manual")]))
        history.compute(self.ctx)
        pts = self.points()
        before = (self.today - timedelta(days=4)).isoformat()
        after = (self.today - timedelta(days=2)).isoformat()
        self.assertTrue(pts[before]["partial"])        # the manual account has no balance yet that day
        self.assertFalse(pts[after]["partial"])
        self.assertEqual(pts[after]["unpriced"], 1)    # FOO has no price: left out, day flagged
        self.assertEqual(Decimal(pts[after]["value"]), Decimal("0.6") * self.close(2))

    def test_portfolio_and_lost_wallets(self):
        self.btc_wallet(portfolio="Client A")
        history.compute(self.ctx)
        self.assertTrue(self.points(portfolio="Client A"))
        self.assertEqual(self.points(portfolio="Personal"), {})
        self.ctx.db.execute("UPDATE wallets SET lost=1")
        self.assertEqual(self.points(), {})                           # hidden, as on the Dashboard

    def test_a_chart_failure_never_fails_a_sync(self):
        self.btc_wallet()
        self.ctx.settings.set("value_transactions", False)     # this fake serves daily candles only
        self.ctx.offline = True                                 # no wallet reading: only the chart step matters
        lines = []
        with mock.patch.object(history, "compute", side_effect=RuntimeError("boom")):
            res = sync.run_sync(self.ctx, lines.append)
        self.assertEqual(res["errors"], 0)
        self.assertTrue(any("Value over time not updated: boom" in m for m in lines))


if __name__ == "__main__":
    unittest.main()
