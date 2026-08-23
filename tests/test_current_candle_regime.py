import os
import sys
import tempfile
import unittest

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from category_strategy_engine import (
    CATEGORY_MARKET_SEEDS,
    CATEGORY_ORDER,
    MODEL_AI_MIN_CONFIDENCE,
    QUANT_MIN_CONFIDENCE,
    TREND_ADX_MIN,
    CategoryStrategyEngine,
    _feature_frame,
    _live_router,
)


class FakeBroker:
    def configured(self):
        return True

    @staticmethod
    def _min_deal_size(_details):
        return 0.1

    def resolve_market(self, symbol, require_tradeable=False):
        return {
            "epic": f"FX.{symbol.replace('/', '')}",
            "name": symbol,
            "instrument_type": "CURRENCIES",
            "market_status": "TRADEABLE",
            "expiry": "-",
            "details": {
                "instrument": {
                    "name": symbol,
                    "type": "CURRENCIES",
                    "expiry": "-",
                },
                "snapshot": {
                    "marketStatus": "TRADEABLE",
                    "bid": 1.2000,
                    "offer": 1.20005,
                },
            },
        }

    def resolve_global_market(self, **kwargs):
        key = kwargs.get("cache_key", "GLOBAL")
        return {
            "epic": f"GLOBAL.{key}",
            "name": key,
            "instrument_type": "INDICES",
            "market_status": "TRADEABLE",
            "min_deal_size": 0.1,
            "expiry": "-",
            "bid": 100.0,
            "offer": 100.05,
        }


def trending_frame(rows=400):
    index = pd.date_range(
        "2026-01-01",
        periods=rows,
        freq="15min",
        tz="UTC",
    )
    # Long trend with recurrent shallow pullbacks.
    vals = []
    price = 1.0
    for i in range(rows):
        price += 0.00035
        if i % 9 == 0:
            price -= 0.00065
        vals.append(price)

    close = pd.Series(vals, index=index)
    open_ = close.shift(1).fillna(close.iloc[0] - 0.0001)
    high = pd.concat([open_, close], axis=1).max(axis=1) + 0.00035
    low = pd.concat([open_, close], axis=1).min(axis=1) - 0.00035

    return pd.DataFrame(
        {
            "Open": open_,
            "High": high,
            "Low": low,
            "Close": close,
            "Volume": 1000.0,
            "UP_PROB": 0.90,
        },
        index=index,
    )


class CurrentCandleRegimeTests(unittest.TestCase):
    def test_policy_constants(self):
        self.assertEqual(QUANT_MIN_CONFIDENCE, 0.28)
        self.assertEqual(MODEL_AI_MIN_CONFIDENCE, 0.40)
        self.assertEqual(TREND_ADX_MIN, 25.0)
        self.assertEqual(
            CATEGORY_ORDER,
            ("FOREX", "INDICES", "CRYPTO", "METALS", "ENERGY", "SHARES"),
        )
        self.assertEqual(len(CATEGORY_MARKET_SEEDS), 40)

    def test_forming_candle_is_not_the_execution_candle(self):
        frame = _feature_frame(trending_frame())
        with tempfile.TemporaryDirectory() as tmp:
            engine = CategoryStrategyEngine(
                broker=FakeBroker(),
                frame_func=lambda seed: trending_frame(),
                state_path=os.path.join(tmp, "state.json"),
                scan_interval_seconds=9999,
            )
            seed = next(
                row for row in CATEGORY_MARKET_SEEDS
                if row["key"] == "EURUSD"
            )
            result = engine._evaluate_seed(seed)
            self.assertTrue(result["forming_candle_ignored"])
            self.assertEqual(result["closed_candle_index_offset"], -2)
            self.assertNotEqual(
                result["closed_candle_timestamp"],
                frame.index[-1].isoformat(),
            )

    def test_no_ai_probability_cannot_pass_ai40(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = CategoryStrategyEngine(
                broker=FakeBroker(),
                frame_func=lambda seed: trending_frame().drop(columns=["UP_PROB"]),
                state_path=os.path.join(tmp, "state.json"),
            )
            seed = next(
                row for row in CATEGORY_MARKET_SEEDS
                if row["key"] == "EURUSD"
            )
            result = engine._evaluate_seed(seed)
            self.assertFalse(result["ai40_pass"])
            self.assertFalse(result["standard_eligible"])

    def test_new_strategy_ids_are_versioned(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = CategoryStrategyEngine(
                broker=FakeBroker(),
                frame_func=lambda seed: trending_frame(),
                state_path=os.path.join(tmp, "state.json"),
            )
            seed = next(
                row for row in CATEGORY_MARKET_SEEDS
                if row["key"] == "EURUSD"
            )
            result = engine._evaluate_seed(seed)
            self.assertEqual(
                result["strategy_id"],
                "FX_CURRENT_CANDLE_REGIME_V3",
            )
            self.assertEqual(
                result["strategy_selection_mode"],
                "CURRENT_CLOSED_CANDLE_ONLY",
            )
            self.assertFalse(result["historical_execution_veto"])


if __name__ == "__main__":
    unittest.main()
