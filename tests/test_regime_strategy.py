from __future__ import annotations

import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "backend"))
from regime_strategy import (RegimeConfig, TREND_ID, REVERSAL_ID, analyze_regime, completed_frames,
                             features, trend_setup, divergence_setup, classify)


def trend_candles(bearish=False):
    close = np.r_[np.linspace(110, 90, 100), np.linspace(90, 105, 20), 98, 98.8]/100
    hours = pd.date_range(end="2026-10-15 10:00Z", periods=len(close), freq="h")
    rows, index = [], []
    previous = close[0]+0.001
    for stamp, end in zip(hours, close):
        for i in range(4):
            o, c = previous+(end-previous)*i/4, previous+(end-previous)*(i+1)/4
            rows.append([o, max(o, c)+0.0015, min(o, c)-0.0015, c, 100])
            index.append(stamp+pd.Timedelta(minutes=15*i))
        previous = end
    frame = pd.DataFrame(rows, index=pd.DatetimeIndex(index), columns=["Open", "High", "Low", "Close", "Volume"])
    if bearish:
        frame[["Open", "High", "Low", "Close"]] = np.c_[2-frame.Open, 2-frame.Low, 2-frame.High, 2-frame.Close]
    return frame


def divergence_frame(bearish=False):
    index = pd.date_range("2026-10-14 00:00Z", periods=80, freq="15min")
    frame = pd.DataFrame({"Open": 98.0, "High": 100.0, "Low": 97.0, "Close": 98.0,
                          "Volume": 100.0, "ATR14": 3.0, "RSI14": 50.0,
                          "BB_LOWER": 92.0, "BB_MID": 100.0, "BB_UPPER": 108.0}, index=index)
    frame.loc[index[64], ["Low", "RSI14"]] = [92, 20]
    frame.loc[index[77], ["Low", "RSI14"]] = [91, 28]
    frame.loc[index[79], ["Open", "High", "Low", "Close"]] = [92.5, 94.2, 92.3, 94]
    if bearish:
        frame[["Open", "High", "Low", "Close"]] = np.c_[200-frame.Open, 200-frame.Low, 200-frame.High, 200-frame.Close]
        frame.RSI14 = 100-frame.RSI14
        frame[["BB_LOWER", "BB_UPPER"]] = np.c_[200-frame.BB_UPPER, 200-frame.BB_LOWER]
        frame.BB_MID = 200-frame.BB_MID
    return frame


def range_candles(bearish=False):
    """A noisy range with a weaker second downswing and a lower wick low."""
    rng = np.random.default_rng(2)
    close = 100+rng.normal(0, .35, 800)
    close[-15:] = [99, 97, 95.8, 97.5, 99.5, 100, 99.7, 99, 98, 97.2, 96.7, 96.3, 96.1, 96.2, 96.6]
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close)+rng.uniform(.1, 2, 800)
    low = np.minimum(open_, close)-rng.uniform(.1, 2, 800)
    for i in range(785, 800):
        low[i], high[i] = min(open_[i], close[i])-.05, max(open_[i], close[i])+.05
    low[-13], low[-3] = 95.7, 95.6
    frame = pd.DataFrame(np.c_[open_/100, high/100, low/100, close/100, np.full(800, 100)],
                         columns=["Open", "High", "Low", "Close", "Volume"],
                         index=pd.date_range(end="2026-10-15 10:45Z", periods=800, freq="15min"))
    if bearish:
        frame[["Open", "High", "Low", "Close"]] = np.c_[2-frame.Open, 2-frame.Low, 2-frame.High, 2-frame.Close]
    return frame


class RegimeTests(unittest.TestCase):
    def test_full_range_buy_and_sell_use_real_rsi_and_observed_mean(self):
        for bearish, direction in ((False, "BUY"), (True, "SELL")):
            with self.subTest(direction=direction):
                result = analyze_regime(range_candles(bearish), "EURUSD", now="2026-10-15 11:00Z")
                self.assertTrue(result["eligible"], result["rejection_reasons"])
                self.assertEqual(result["strategy_id"], REVERSAL_ID)
                self.assertEqual(result["direction"], direction)
                self.assertEqual(result["regime"], "RANGING")
                self.assertGreaterEqual(result["target_r"], 1.2)
                self.assertAlmostEqual(result["take_profit_target_price"], result["selected_setup"]["range_target"])

    def test_full_trend_buy_and_sell_from_real_indicators(self):
        for bearish, direction in ((False, "BUY"), (True, "SELL")):
            with self.subTest(direction=direction):
                result = analyze_regime(trend_candles(bearish), "EURUSD", now="2026-10-15 11:00Z")
                self.assertTrue(result["eligible"], result)
                self.assertEqual(result["direction"], direction)
                self.assertEqual(result["strategy_id"], TREND_ID)
                self.assertGreater(result["h1_adx"], 25)
                self.assertEqual(result["timeframe"], "H1")
                self.assertGreater(result["structural_stop_distance"], 0)
                self.assertEqual(result["target_r"], 2)

    def test_forming_and_future_candles_do_not_change_signal(self):
        frame = trend_candles()
        expected = analyze_regime(frame, "EURUSD", now="2026-10-15 11:00Z")
        for stamp in ("2026-10-15 11:00Z", "2026-10-16 11:00Z"):
            frame.loc[pd.Timestamp(stamp)] = [500, 600, 1, 550, 999999]
        actual = analyze_regime(frame, "EURUSD", now="2026-10-15 11:00Z")
        for key in ("direction", "setup_id", "structural_stop_distance", "ema20", "rsi"):
            self.assertEqual(expected[key], actual[key])

    def test_missing_m15_cannot_complete_h1_or_h4(self):
        frame = trend_candles()
        frame = frame.drop(frame.index[-2])
        result = completed_frames(frame, "2026-10-15 11:00Z")
        self.assertNotIn(pd.Timestamp("2026-10-15 10:00Z"), result["H1"].index)
        self.assertTrue(all(t+pd.Timedelta(hours=4) <= pd.Timestamp("2026-10-15 11:00Z") for t in result["H4"].index))

    def test_stale_confirmation_and_stale_data_are_rejected(self):
        frame = trend_candles()
        stale = analyze_regime(frame, "EURUSD", now="2026-10-15 11:06Z")
        self.assertEqual(stale["direction"], "WAIT")
        self.assertIn("CONFIRMATION_SIGNAL_STALE", stale["rejection_reasons"])
        old = analyze_regime(frame, "EURUSD", now="2026-10-16 11:00Z")
        self.assertIn("ANALYSIS_CANDLES_STALE", old["rejection_reasons"])

    def test_rsi_at_unidirectional_and_flat_limits_is_defined(self):
        frame = trend_candles().tail(100).copy()
        for rising, expected in ((True, 100), (False, 50)):
            close = np.linspace(1, 2, 100) if rising else np.ones(100)
            frame[["Open", "High", "Low", "Close"]] = np.c_[close, close+.01, close-.01, close]
            self.assertAlmostEqual(features(frame).RSI14.iloc[-1], expected)

    def test_transition_does_not_select_either_strategy(self):
        frame = features(trend_candles()).tail(10).copy()
        frame["ADX14"] = 23.0
        self.assertEqual(classify(frame, RegimeConfig()), "TRANSITION")

    def test_cross_itself_and_old_cross_do_not_trigger_entry(self):
        frame = features(completed_frames(trend_candles(), "2026-10-15 11:00Z")["H1"])
        crosses = ((frame.EMA20 > frame.EMA50) & (frame.EMA20.shift() <= frame.EMA50.shift()))
        crossing = int(np.flatnonzero(crosses.to_numpy())[-1])
        self.assertFalse(trend_setup(frame.iloc[:crossing+1], "BUY", RegimeConfig())["eligible"])
        self.assertFalse(trend_setup(frame, "BUY", RegimeConfig(crossover_lookback=1))["eligible"])

    def test_divergence_requires_matching_price_pivots_and_strong_close(self):
        for bearish, direction in ((False, "BUY"), (True, "SELL")):
            with self.subTest(direction=direction):
                frame = divergence_frame(bearish)
                result = divergence_setup(frame, direction, RegimeConfig())
                self.assertTrue(result["eligible"], result)
                self.assertEqual(result["pivot_rsi"], [80, 72] if bearish else [20, 28])
                frame.loc[frame.index[77], "RSI14"] = 90 if bearish else 10
                self.assertFalse(divergence_setup(frame, direction, RegimeConfig())["eligible"])

    def test_unconfirmed_pivot_and_breakout_cannot_be_faded(self):
        frame = divergence_frame()
        self.assertFalse(divergence_setup(frame.iloc[:-1], "BUY", RegimeConfig())["eligible"])
        frame.loc[frame.index[-1], ["Open", "High", "Low", "Close"]] = [90, 91, 88, 89]
        self.assertFalse(divergence_setup(frame, "BUY", RegimeConfig())["eligible"])

    def test_divergence_without_reversal_candle_waits(self):
        frame = divergence_frame()
        frame.loc[frame.index[-1], "Open"] = frame.Close.iloc[-1]
        self.assertFalse(divergence_setup(frame, "BUY", RegimeConfig())["eligible"])

    def test_duplicate_and_invalid_prices_fail_closed(self):
        frame = trend_candles()
        with self.assertRaises(ValueError):
            completed_frames(pd.concat([frame, frame.tail(1)]))
        frame.iloc[-1, frame.columns.get_loc("Close")] = np.inf
        with self.assertRaises(ValueError):
            completed_frames(frame)


if __name__ == "__main__":
    unittest.main()
