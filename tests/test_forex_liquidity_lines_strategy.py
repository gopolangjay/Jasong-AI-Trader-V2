from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

import pandas as pd


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from forex_liquidity_lines_strategy import (  # noqa: E402
    LIQUID_FOREX_PAIRS,
    _direction_setup,
    _trendline_from_points,
    candlestick_confirmation,
    forex_session_context,
    news_blackout_context,
)


def small_frame(rows=40, end="2026-01-15 13:15:00+00:00"):
    index = pd.date_range(end=end, periods=rows, freq="15min")
    close = pd.Series([1.1000 + i * 0.00002 for i in range(rows)], index=index)
    open_ = close.shift(1).fillna(close.iloc[0] - 0.00002)
    return pd.DataFrame(
        {
            "Open": open_,
            "High": pd.concat([open_, close], axis=1).max(axis=1) + 0.0003,
            "Low": pd.concat([open_, close], axis=1).min(axis=1) - 0.0003,
            "Close": close,
            "Volume": 1000.0,
        },
        index=index,
    )


class ForexUniverseAndSessionTests(unittest.TestCase):
    def test_universe_is_all_28_liquid_major_currency_combinations(self):
        self.assertEqual(len(LIQUID_FOREX_PAIRS), 28)
        self.assertEqual(len(set(LIQUID_FOREX_PAIRS)), 28)
        self.assertIn("EURUSD", LIQUID_FOREX_PAIRS)
        self.assertIn("AUDNZD", LIQUID_FOREX_PAIRS)
        self.assertIn("CHFJPY", LIQUID_FOREX_PAIRS)
        self.assertNotIn("USDZAR", LIQUID_FOREX_PAIRS)

    def test_pair_geography_and_dst_select_relevant_sessions(self):
        winter_overlap = forex_session_context(
            "2026-01-15 13:30:00+00:00", "EURUSD"
        )
        summer_overlap = forex_session_context(
            "2026-07-15 12:30:00+00:00", "EURUSD"
        )
        self.assertTrue(winter_overlap["london_new_york_overlap"])
        self.assertTrue(summer_overlap["london_new_york_overlap"])
        self.assertEqual(
            winter_overlap["relevant_sessions"], ["LONDON", "NEW_YORK"]
        )

        tokyo = forex_session_context("2026-01-15 01:00:00+00:00", "USDJPY")
        self.assertTrue(tokyo["tokyo_active"])
        self.assertIn("TOKYO", tokyo["active_sessions"])

        sydney = forex_session_context("2026-01-15 22:30:00+00:00", "AUDNZD")
        self.assertEqual(sydney["relevant_sessions"], ["SYDNEY"])
        self.assertTrue(sydney["sydney_active"])

    def test_weekend_and_irrelevant_hours_are_blocked(self):
        self.assertFalse(
            forex_session_context("2026-01-17 13:30:00+00:00", "EURUSD")["active"]
        )
        self.assertFalse(
            forex_session_context("2026-01-15 22:00:00+00:00", "EURGBP")["active"]
        )

    def test_sydney_monday_uses_local_weekday_not_utc_weekday(self):
        # During Sydney daylight saving, Monday 08:30 is still Sunday in UTC.
        context = forex_session_context(
            "2026-01-04 21:30:00+00:00", "AUDNZD"
        )
        self.assertTrue(context["sydney_active"])
        self.assertTrue(context["weekday_gate_pass"])


class ForexNewsAndCandleTests(unittest.TestCase):
    def test_high_impact_event_blocks_either_pair_currency(self):
        payload = (
            '[{"currency":"USD","impact":"HIGH","name":"CPI",'
            '"timestamp":"2026-01-15T13:30:00Z","before_minutes":30,'
            '"after_minutes":30}]'
        )
        with patch.dict(
            os.environ,
            {"JASONG_HIGH_IMPACT_NEWS_WINDOWS_JSON": payload},
            clear=False,
        ):
            blocked = news_blackout_context(
                "2026-01-15 13:15:00+00:00", "EURUSD"
            )
            clear = news_blackout_context(
                "2026-01-15 15:00:00+00:00", "EURUSD"
            )
        self.assertFalse(blocked["clear"])
        self.assertEqual(blocked["active_events"][0]["name"], "CPI")
        self.assertTrue(clear["clear"])

    def test_invalid_configured_news_data_fails_closed(self):
        with patch.dict(
            os.environ,
            {"JASONG_HIGH_IMPACT_NEWS_WINDOWS_JSON": "not-json"},
            clear=False,
        ):
            result = news_blackout_context(
                "2026-01-15 13:15:00+00:00", "EURUSD"
            )
        self.assertFalse(result["clear"])
        self.assertIsNotNone(result["error"])

        missing_time = '[{"currency":"EUR","impact":"HIGH"}]'
        with patch.dict(
            os.environ,
            {"JASONG_HIGH_IMPACT_NEWS_WINDOWS_JSON": missing_time},
            clear=False,
        ):
            result = news_blackout_context(
                "2026-01-15 13:15:00+00:00", "EURUSD"
            )
        self.assertFalse(result["clear"])
        self.assertIn("invalid EUR news window", result["error"])

    def test_bullish_and_bearish_engulfing_are_analyzed(self):
        frame = small_frame(rows=4)
        frame.iloc[-2, frame.columns.get_loc("Open")] = 1.1020
        frame.iloc[-2, frame.columns.get_loc("Close")] = 1.1000
        frame.iloc[-2, frame.columns.get_loc("High")] = 1.1022
        frame.iloc[-2, frame.columns.get_loc("Low")] = 1.0998
        frame.iloc[-1, frame.columns.get_loc("Open")] = 1.0995
        frame.iloc[-1, frame.columns.get_loc("Close")] = 1.1025
        frame.iloc[-1, frame.columns.get_loc("High")] = 1.1027
        frame.iloc[-1, frame.columns.get_loc("Low")] = 1.0993
        bullish = candlestick_confirmation(frame, "BUY")
        self.assertTrue(bullish["passed"])
        self.assertIn("BULLISH_ENGULFING", bullish["patterns"])

        frame.iloc[-2, frame.columns.get_loc("Open")] = 1.1000
        frame.iloc[-2, frame.columns.get_loc("Close")] = 1.1020
        frame.iloc[-1, frame.columns.get_loc("Open")] = 1.1025
        frame.iloc[-1, frame.columns.get_loc("Close")] = 1.0995
        bearish = candlestick_confirmation(frame, "SELL")
        self.assertTrue(bearish["passed"])
        self.assertIn("BEARISH_ENGULFING", bearish["patterns"])


class ForexLineAndSetupTests(unittest.TestCase):
    def test_external_line_requires_two_touches_and_correct_slope(self):
        missing = _trendline_from_points([], 20, 1.10, 0.001, "BUY")
        self.assertFalse(missing["available"])

        rising = _trendline_from_points(
            [
                {"position": 5, "price": 1.0900, "timestamp": "a"},
                {"position": 15, "price": 1.0950, "timestamp": "b"},
            ],
            20,
            1.0980,
            0.001,
            "BUY",
        )
        self.assertTrue(rising["available"])
        self.assertTrue(rising["aligned"])
        self.assertTrue(rising["intact"])

    def test_setup_id_is_stable_across_confirmation_candles(self):
        frame = small_frame()
        atr = pd.Series(0.001, index=frame.index)
        sweep = {
            "source": "PREVIOUS_DAY_LOW",
            "extreme": 1.0988,
            "position": 30,
            "timestamp": frame.index[30],
        }
        structure = {
            "type": "BOS",
            "level": 1.1030,
            "position": 33,
            "timestamp": frame.index[33],
        }
        zone = {
            "kind": "ORDER_BLOCK",
            "low": 1.0990,
            "high": 1.1000,
            "origin_timestamp": frame.index[32],
            "retest_timestamp": frame.index[-1],
        }
        line = {
            "available": True,
            "aligned": True,
            "intact": True,
            "touched": True,
        }
        h4 = {
            "trend": "BULLISH",
            "equilibrium": 1.1020,
        }
        h1 = {"trend": "BULLISH"}
        session = {"active": True, "pair": "EURUSD", "overlap": True}
        news = {"clear": True}
        levels = {
            "previous_day": {"high": 1.1200, "low": 1.0900},
            "previous_week": {},
            "previous_month": {},
        }
        confirmations = [
            {"passed": True, "patterns": ["BULLISH_ENGULFING"], "timestamp": frame.index[-1]},
            {"passed": True, "patterns": ["HAMMER_REJECTION"], "timestamp": frame.index[-2]},
        ]
        results = []
        for confirmation in confirmations:
            with (
                patch("forex_liquidity_lines_strategy.trendline_context", return_value=line),
                patch("forex_liquidity_lines_strategy._find_liquidity_sweep", return_value=sweep),
                patch("forex_liquidity_lines_strategy._find_structure_shift", return_value=structure),
                patch("forex_liquidity_lines_strategy._find_retest_zone", return_value=zone),
                patch("forex_liquidity_lines_strategy.candlestick_confirmation", return_value=confirmation),
            ):
                results.append(
                    _direction_setup(
                        frame,
                        "BUY",
                        h4_frame=frame,
                        h4=h4,
                        h1=h1,
                        session=session,
                        news=news,
                        levels=levels,
                        atr=atr,
                    )
                )
        self.assertEqual(results[0]["direction"], "BUY")
        self.assertTrue(all(results[0]["checks"].values()))
        self.assertEqual(results[0]["setup_id"], results[1]["setup_id"])
        self.assertTrue(results[0]["internal_line_entry_confluence"])


if __name__ == "__main__":
    unittest.main()
