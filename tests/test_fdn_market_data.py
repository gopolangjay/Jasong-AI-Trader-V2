from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "backend"))
from fdn_market_data import FinancialDataProvider, FDNDataError


def minute_records(date="2026-10-15", count=1440):
    return [{"trading_symbol": "EURUSD", "time": t.isoformat(), "open": 1.1,
             "high": 1.102, "low": 1.099, "close": 1.101, "volume": 4}
            for t in pd.date_range(date, periods=count, freq="min", tz="UTC")]


class FDNTests(unittest.TestCase):
    def test_minute_records_become_complete_sorted_utc_m15(self):
        records = minute_records()
        bars = FinancialDataProvider.normalize(list(reversed(records)), "EURUSD", pd.Timestamp("2026-10-15 23:59Z"))
        self.assertEqual(len(bars), 95)
        self.assertTrue(bars.index.is_monotonic_increasing)
        self.assertEqual(str(bars.index.tz), "UTC")
        self.assertEqual(bars.Volume.iloc[0], 60)
        self.assertEqual(bars.index[-1].isoformat(), "2026-10-15T23:30:00+00:00")

    def test_missing_minute_drops_entire_aggregate(self):
        records = minute_records()
        del records[7]
        bars = FinancialDataProvider.normalize(records, "EURUSD", pd.Timestamp("2026-10-16 00:00Z"))
        self.assertEqual(len(bars), 95)
        self.assertNotIn(pd.Timestamp("2026-10-15 00:00Z"), bars.index)

    def test_invalid_prices_or_wrong_symbols_fail_closed(self):
        for key, value in (("close", float("inf")), ("high", 0.1), ("trading_symbol", "GBPUSD")):
            with self.subTest(key=key):
                records = minute_records()
                records[0][key] = value
                with self.assertRaises(FDNDataError):
                    FinancialDataProvider.normalize(records, "EURUSD", pd.Timestamp("2026-10-16 00:00Z"))

    def test_unknown_volume_is_zero_not_synthetic_activity(self):
        records = minute_records()
        for r in records:
            r.pop("volume")
        bars = FinancialDataProvider.normalize(records, "EURUSD", pd.Timestamp("2026-10-16 00:00Z"))
        self.assertEqual(bars.Volume.sum(), 0)

    def test_daily_commodities_are_not_substituted_for_intraday(self):
        provider = FinancialDataProvider(api_key="test-key")
        for seed in ({"category": "METALS", "key": "GOLD"}, {"category": "INDICES", "key": "US500"}):
            self.assertIsNone(provider.identifier(seed))
        self.assertEqual(provider.identifier({"category": "CRYPTO", "key": "BITCOIN"}), ("get_crypto_minute_prices", "BTCUSD"))

    def test_sdk_calls_have_timeouts_and_do_not_log_keyed_urls(self):
        provider = FinancialDataProvider(api_key="sensitive-test-key")
        client = provider._get_client()
        response = Mock(status_code=403)
        client.session.get = Mock(return_value=response)
        with self.assertRaises(FDNDataError) as raised:
            client.get_forex_minute_prices(identifier="EURUSD", date="2026-10-15")
        self.assertNotIn("sensitive-test-key", str(raised.exception))
        self.assertEqual(client.session.get.call_count, 1)
        self.assertEqual(client.session.get.call_args.kwargs["timeout"], (3.0, 8.0))
        self.assertTrue(provider.status()["cooldown_active"])

    def test_pagination_stops_at_bound_and_rejects_partial_day(self):
        provider = FinancialDataProvider(api_key="test-key")
        provider.max_pages = 2
        client = provider._get_client()
        client.make_request = Mock(return_value=("300", minute_records(count=300)))
        with self.assertRaisesRegex(FDNDataError, "incomplete day"):
            client.get_forex_minute_prices(identifier="EURUSD", date="2026-10-15")
        self.assertEqual(client.make_request.call_count, 2)
        self.assertEqual(client.make_request.call_args.args[1]["offset"], 300)

    def test_quota_blocks_before_request(self):
        provider = FinancialDataProvider(api_key="test-key")
        provider.max_daily = 1
        provider._reserve_call()
        with self.assertRaisesRegex(FDNDataError, "budget"):
            provider._reserve_call()
        self.assertEqual(provider.status()["requests_today"], 1)

    def test_endpoint_exception_is_redacted(self):
        client = Mock()
        client.get_forex_minute_prices.side_effect = RuntimeError("https://example?key=secret")
        provider = FinancialDataProvider(api_key="secret", client=client)
        with self.assertRaises(FDNDataError):
            provider.load({"category": "FOREX", "key": "EURUSD"})
        self.assertNotIn("secret", str(provider.status()))

    def test_sdk_endpoint_and_history_cache_are_used(self):
        client = Mock()
        client.get_forex_minute_prices.side_effect = lambda *, identifier, date: minute_records(date)
        provider = FinancialDataProvider(client=client)
        seed = {"category": "FOREX", "key": "EURUSD"}
        bars = provider.load(seed, now="2026-10-15 12:00Z")
        self.assertGreater(len(bars), 300)
        calls = client.get_forex_minute_prices.call_count
        provider.load(seed, now="2026-10-15 12:01Z")
        self.assertEqual(client.get_forex_minute_prices.call_count, calls)
        self.assertEqual(provider.status()["cache_entries"], 1)

    def test_resilient_loader_falls_back_after_fdn_error(self):
        from specialist_market_data import ResilientSpecialistMarketData
        with tempfile.TemporaryDirectory() as directory:
            loader = ResilientSpecialistMarketData(state_dir=directory)
            loader.fdn = FinancialDataProvider(api_key="test-key")
            with patch.object(loader.fdn, "load", side_effect=FDNDataError("budget reached")), patch.object(loader, "_forex", return_value="fallback"):
                self.assertEqual(loader.load({"category": "FOREX", "key": "EURUSD"}), "fallback")


if __name__ == "__main__":
    unittest.main()
