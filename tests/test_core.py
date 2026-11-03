import unittest
from datetime import date

from cryptoledger.adapters.binance import sign
from cryptoledger.adapters.tron import base58_to_hex, hex_to_base58
from cryptoledger.b58 import b58check_decode, b58decode, b58encode
from cryptoledger.timeutil import FiscalCalendar, parse_utc
from cryptoledger.util import D, dstr


class FiscalCalendarTest(unittest.TestCase):
    def test_rome_cutoff_is_2259_utc_in_winter(self):
        cal = FiscalCalendar("Europe/Rome", "12-31")
        self.assertEqual(cal.cutoff(2025), 1767221999)  # 2025-12-31T22:59:59Z
        self.assertEqual(cal.iso_local(cal.cutoff(2025)), "2025-12-31T23:59:59+01:00")
        self.assertEqual(cal.start(2026), 1767222000)

    def test_year_of_around_cutoff(self):
        cal = FiscalCalendar("Europe/Rome")
        self.assertEqual(cal.year_of(1767221999), 2025)
        self.assertEqual(cal.year_of(1767222000), 2026)   # 00:00:00 local on 1 January
        self.assertEqual(cal.year_of(1767225000), 2026)   # still 31/12 in UTC, already 2026 in Rome

    def test_days(self):
        cal = FiscalCalendar("Europe/Rome")
        self.assertEqual(cal.days_in_year(2024), 366)
        self.assertEqual(cal.days_in_year(2025), 365)
        days = cal.days_of_year(2025)
        self.assertEqual((days[0], days[-1]), (date(2025, 1, 1), date(2025, 12, 31)))

    def test_non_calendar_fiscal_year(self):
        cal = FiscalCalendar("Europe/London", "04-05")
        self.assertEqual(cal.iso_local(cal.cutoff(2025)), "2025-04-05T23:59:59+01:00")
        self.assertEqual(cal.days_in_year(2025), 365)

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            FiscalCalendar("Mars/Olympus")
        with self.assertRaises(ValueError):
            FiscalCalendar("Europe/Rome", "02-29")

    def test_parse_utc(self):
        self.assertEqual(parse_utc("2021-04-29 16:08:00"), 1619712480)
        self.assertEqual(parse_utc("2021-04-29T16:08:00.000Z"), 1619712480)


class EncodingTest(unittest.TestCase):
    def test_base58_roundtrip(self):
        raw = bytes(range(32))
        self.assertEqual(b58decode(b58encode(raw)), raw)
        self.assertEqual(b58encode(b"\x00\x00\x01"), "112")

    def test_tron_addresses(self):
        usdt = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
        self.assertEqual(base58_to_hex(usdt), "41a614f803b6fd780986a42c78ec9c7f77e6ded13c")
        self.assertEqual(hex_to_base58("41a614f803b6fd780986a42c78ec9c7f77e6ded13c"), usdt)
        self.assertEqual(hex_to_base58("0xa614f803b6fd780986a42c78ec9c7f77e6ded13c"), usdt)
        with self.assertRaises(ValueError):
            b58check_decode("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6u")

    def test_binance_signature_matches_documentation_example(self):
        secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
        query = ("symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1"
                 "&recvWindow=5000&timestamp=1499827319559")
        self.assertEqual(sign(secret, query), "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71")

    def test_decimal_strings(self):
        self.assertEqual(dstr(D("1.2300")), "1.23")
        self.assertEqual(dstr(D("-0.0")), "0")
        self.assertEqual(dstr(D("1E-18")), "0.000000000000000001")
        self.assertEqual(dstr(D(48800000) + D("1e-18")), "48800000.000000000000000001")


if __name__ == "__main__":
    unittest.main()
