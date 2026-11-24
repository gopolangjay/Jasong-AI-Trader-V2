from __future__ import annotations

import copy
import os
import sys
import unittest
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "backend"))
from candle_data import CandleValidationError, normalize_candles, ig_prices_to_frame, normalized_ig_prices
from ig_demo_broker import IGDemoBroker
from market_data_router import clean_ohlcv
import market_data_router as router
from regime_strategy import completed_frames
from specialist_market_data import ResilientSpecialistMarketData


def bars(count=96, end="2026-10-15 10:45Z"):
    return pd.DataFrame({"Open": 1.0, "High": 1.2, "Low": .9, "Close": 1.1, "Volume": 100.0},
                        index=pd.date_range(end=end, periods=count, freq="15min"))


def ig_row(stamp="2026-10-15T10:45:00Z"):
    return {"snapshotTimeUTC": stamp, "snapshotTime": "2026/10/15 12:45:00",
            "openPrice": {"bid": .99, "ask": 1.01}, "highPrice": {"bid": 1.19, "ask": 1.21},
            "lowPrice": {"bid": .89, "ask": .91}, "closePrice": {"bid": 1.09, "ask": 1.11},
            "lastTradedVolume": 100.0}


class CandleDataTests(unittest.TestCase):
    def test_timezone_conversion_sorting_and_identical_duplicates_preserve_prices(self):
        expected = bars()
        for timezone in ("Africa/Johannesburg", "America/New_York", "Europe/London"):
            with self.subTest(timezone=timezone):
                raw = pd.concat([expected, expected.tail(2)]).iloc[::-1]
                raw.index = raw.index.tz_convert(timezone)
                original = raw.copy(deep=True)
                actual = normalize_candles(raw, interval="15min")
                pd.testing.assert_frame_equal(actual, expected, check_freq=False)
                pd.testing.assert_frame_equal(raw, original)
                self.assertEqual(str(actual.index.tz), "UTC")
                self.assertTrue(all(t.minute in (0, 15, 30, 45) and t.second == t.microsecond == t.nanosecond == 0
                                    for t in actual.index))

    def test_conflicting_duplicate_for_every_ohlcv_field_is_rejected(self):
        for col in bars().columns:
            with self.subTest(col=col):
                original = bars()
                duplicate = original.tail(1).copy()
                duplicate[col] += .001
                with self.assertRaisesRegex(CandleValidationError, "Conflicting duplicate"):
                    normalize_candles(pd.concat([original, duplicate]), interval="15min")

    def test_nanosecond_microsecond_second_and_minute_offsets_are_not_rounded(self):
        for delta in (pd.Timedelta(1, "ns"), pd.Timedelta(1, "us"), pd.Timedelta(seconds=1), pd.Timedelta(minutes=1)):
            with self.subTest(delta=delta):
                raw = bars()
                raw.index += delta
                with self.assertRaisesRegex(CandleValidationError, "aligned M15"):
                    completed_frames(raw)

    def test_invalid_ohlc_rejected_before_provider_cleaners_can_discard_it(self):
        for col, value in (("Close", np.nan), ("Close", np.inf), ("Open", 0), ("Low", -1),
                           ("High", .99), ("Low", 1.11), ("Close", "bad"), ("Close", True),
                           ("Volume", -1), ("Volume", np.inf), ("Volume", "bad")):
            for cleaner in (clean_ohlcv, lambda x: ResilientSpecialistMarketData._clean(x, min_rows=80, label="EURUSD")):
                with self.subTest(col=col, value=value, cleaner=cleaner):
                    raw = bars().astype(object)
                    raw.iloc[0, raw.columns.get_loc(col)] = value
                    original = raw.copy(deep=True)
                    with self.assertRaises(CandleValidationError):
                        cleaner(raw)
                    pd.testing.assert_frame_equal(raw, original)

    def test_missing_columns_and_invalid_or_numeric_timestamps_fail_closed(self):
        for raw in (bars().drop(columns="Open"), bars().reset_index(drop=True), bars().set_axis([pd.NaT]*96),
                    bars().set_axis(["not a timestamp"]*96)):
            with self.subTest(index_type=type(raw.index)):
                with self.assertRaises(CandleValidationError):
                    normalize_candles(raw)

    def test_missing_volume_stays_zero_and_naive_provider_indexes_are_utc(self):
        raw = bars().drop(columns="Volume")
        raw.index = raw.index.tz_localize(None)
        actual = normalize_candles(raw, interval="15min")
        self.assertEqual(actual.Volume.sum(), 0)
        self.assertEqual(str(actual.index.tz), "UTC")

    def test_close_boundary_and_partial_higher_frames(self):
        raw = bars(4)
        before = completed_frames(raw, "2026-10-15 10:59:59.999999999Z")
        self.assertEqual(len(before["M15"]), 3)
        self.assertTrue(before["H1"].empty)
        at_close = completed_frames(raw, "2026-10-15 13:00+02:00")
        self.assertEqual(len(at_close["M15"]), 4)
        self.assertEqual(len(at_close["H1"]), 1)
        missing = pd.concat([raw.drop(raw.index[-2]), raw.tail(1)])
        self.assertTrue(completed_frames(missing, "2026-10-15 11:00Z")["H1"].empty)

    def test_ig_prefers_utc_and_accepts_explicit_offsets(self):
        rows = [ig_row("2026-10-15 12:45+02:00"), ig_row("2026-10-15T10:30:00"), ig_row()]
        original = copy.deepcopy(rows)
        actual = ig_prices_to_frame(rows, interval="15min", now="2026-10-15 10:59Z", closed_only=True)
        self.assertEqual(actual.index.tolist(), [pd.Timestamp("2026-10-15 10:30Z")])
        self.assertAlmostEqual(actual.Close.iloc[0], 1.1)
        normalized = normalized_ig_prices(rows, interval="15min")
        self.assertEqual([r["snapshotTimeUTC"] for r in normalized],
                         ["2026-10-15T10:30:00+00:00", "2026-10-15T10:45:00+00:00"])
        self.assertEqual(rows, original)

    def test_ig_never_guesses_a_local_timezone_or_ignores_invalid_utc(self):
        for utc in (None, "", "bad", 1792061100):
            with self.subTest(utc=utc):
                row = ig_row()
                if utc is None:
                    row.pop("snapshotTimeUTC")
                else:
                    row["snapshotTimeUTC"] = utc
                with self.assertRaises(CandleValidationError):
                    ig_prices_to_frame([row])
        row = ig_row()
        row.pop("snapshotTimeUTC")
        row["snapshotTime"] = "2026-10-15T12:45:00+02:00"
        self.assertEqual(ig_prices_to_frame([row]).index[0], pd.Timestamp("2026-10-15 10:45Z"))

    def test_bad_broker_rows_quotes_and_conflicts_are_not_silently_dropped(self):
        for key, value in (("closePrice", None), ("closePrice", {"bid": 2, "ask": 1}),
                           ("highPrice", {"bid": .5, "ask": 1.9}), ("lowPrice", np.inf),
                           ("openPrice", {"bid": 0, "ask": 1}), ("lastTradedVolume", "bad"),
                           ("snapshotTimeUTC", "2026-10-15T10:45:00.000001Z")):
            with self.subTest(key=key):
                row = ig_row()
                row[key] = value
                with self.assertRaises(CandleValidationError):
                    normalized_ig_prices([ig_row(), row], interval="15min")
        with self.assertRaises(CandleValidationError):
            ig_prices_to_frame([ig_row(), None])
        shifted_quotes = ig_row()
        shifted_quotes["closePrice"] = {"bid": 1.08, "ask": 1.12}
        with self.assertRaisesRegex(CandleValidationError, "Conflicting duplicate"):
            normalized_ig_prices([ig_row(), shifted_quotes])
        changed = ig_row()
        changed["lastTradedVolume"] = 101
        with self.assertRaisesRegex(CandleValidationError, "Conflicting duplicate"):
            normalized_ig_prices([ig_row(), changed])

    def test_both_broker_history_methods_request_v3_utc_without_pagination(self):
        broker = IGDemoBroker()
        broker.resolve_market = Mock(return_value={"epic": "EURUSD.DEMO", "symbol": "EURUSD"})
        broker._request = Mock(return_value={"prices": [ig_row()]})
        for fetch in (lambda: broker.historical_prices("EURUSD", num_points=120),
                      lambda: broker.historical_prices_epic("EURUSD.DEMO", num_points=120)):
            result = fetch()
            broker._request.assert_called_with("GET", "/prices/EURUSD.DEMO", version=3,
                                              query={"resolution": "MINUTE_15", "max": 120, "pageSize": 0})
            self.assertEqual(result["prices"][0]["snapshotTimeUTC"], "2026-10-15T10:45:00+00:00")
            self.assertEqual(broker.BASE_URL, "https://demo-api.ig.com/gateway/deal")
            self.assertFalse(result["live_money_execution"])
        self.assertEqual(broker.resolve_market.call_count, 1)
        broker.resolve_market.assert_called_once_with("EURUSD", require_tradeable=False)


class IGHistoryCacheTests(unittest.TestCase):
    def setUp(self):
        self.broker = Mock()
        self.broker.configured.return_value = True
        self.now = pd.Timestamp("2026-10-15 11:30Z")
        self.initial = bars(end="2026-10-15 10:45Z")
        self.patchers = [patch.object(router, "_IG_DEMO_BROKER", self.broker),
                         patch.object(router, "_IG_HISTORY_CACHE", {}), patch.object(router, "_CACHE", {}),
                         patch.object(router.time, "time", return_value=self.now.timestamp())]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)
        router._IG_HISTORY_CACHE[router._ig_history_key("EURUSD", "15m")] = router.RouterCacheEntry(
            self.initial.copy(), self.now.timestamp()-router.IG_DEMO_HISTORY_REFRESH_SECONDS-1, "IG_DEMO")

    def test_refresh_covers_all_missed_bars_and_never_caches_forming_bar(self):
        self.broker.historical_prices.return_value = {"prices": [ig_row(str(t)) for t in
            pd.date_range("2026-10-15 11:00Z", periods=3, freq="15min")]}
        result = router.download_ig_demo("EURUSD", "5d", "15m")
        self.assertEqual(result.index[-1], pd.Timestamp("2026-10-15 11:15Z"))
        self.assertEqual(len(result), 98)
        self.assertTrue(result.index.is_unique)
        self.broker.historical_prices.assert_called_once_with("EUR/USD", resolution="MINUTE_15", num_points=4)
        cached = router.download_ig_demo("EURUSD", "5d", "15m")
        pd.testing.assert_frame_equal(result, cached)
        self.assertEqual(self.broker.historical_prices.call_count, 1)

    def test_cache_refresh_rejects_revised_completed_bar_and_preserves_cache(self):
        row = ig_row()
        row["lastTradedVolume"] = 101
        self.broker.historical_prices.return_value = {"prices": [row]}
        with self.assertRaisesRegex(CandleValidationError, "Conflicting duplicate"):
            router.download_ig_demo("EURUSD", "5d", "15m")
        actual = router._IG_HISTORY_CACHE[router._ig_history_key("EURUSD", "15m")].dataframe
        pd.testing.assert_frame_equal(actual, self.initial)


if __name__ == "__main__":
    unittest.main()
