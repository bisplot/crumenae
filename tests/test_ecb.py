"""ECB exchange rates: the ECB answers in the format of the Accept header, so CSV must be asked
for explicitly (the app's default is JSON, which made every ECB rate fail)."""
import unittest
from datetime import date
from decimal import Decimal

from cryptoledger.models import AdapterError
from cryptoledger.prices.ecb import EcbRates

from helpers import TempContext

CSV = ("KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE,OBS_STATUS\n"
       "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-11-06,1.1165,A\n"
       "EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,2026-11-09,1.1190,A\n")


class Ecb:
    """Like the real service: JSON unless the request asks for CSV."""
    def __init__(self):
        self.accepts = []

    def get_text(self, url, params=None, headers=None, count=True):
        accept = (headers or {}).get("Accept", "application/json")
        self.accepts.append(accept)
        return CSV if "csv" in accept else '{"header": {"id": "x"}, "dataSets": []}'


class EcbTest(unittest.TestCase):
    def setUp(self):
        self.t = TempContext()
        self.ctx = self.t.ctx
        self.ctx.offline = False
        self.ctx.http = Ecb()

    def tearDown(self):
        self.t.close()

    def test_asks_for_csv_and_uses_the_last_rate_before_a_weekend(self):
        rates = EcbRates(self.ctx)
        self.assertEqual(rates.rate("USD", date(2026, 11, 8)), (Decimal("1.1165"), "2026-11-06"))
        self.assertEqual(self.ctx.http.accepts, ["text/csv"])
        self.assertEqual(rates.rate("USD", date(2026, 11, 9)), (Decimal("1.1190"), "2026-11-09"))
        self.assertEqual(len(self.ctx.http.accepts), 1)            # the second day came from the cache

    def test_a_non_csv_answer_is_a_clear_error(self):
        self.ctx.http.get_text = lambda *a, **k: '{"header": {}}'
        with self.assertRaises(AdapterError) as e:
            EcbRates(self.ctx).rate("USD", date(2026, 11, 9))
        self.assertIn("not CSV", str(e.exception))


if __name__ == "__main__":
    unittest.main()
