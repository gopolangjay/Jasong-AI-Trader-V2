import os
import sys
import tempfile
import unittest

import pandas as pd


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from category_strategy_engine import (  # noqa: E402
    ACTIVE_EXECUTION_KEYS,
    CATEGORY_MARKET_SEEDS,
    CATEGORY_ORDER,
    MODEL_AI_MIN_CONFIDENCE,
    QUANT_MIN_CONFIDENCE,
    TREND_ADX_MIN,
    CategoryStrategyEngine,
)
from xauusd_liquidity_strategy import STRATEGY_ID  # noqa: E402


class FakeBroker:
    def configured(self):
        return True

    @staticmethod
    def _min_deal_size(_details):
        return 0.1

    def resolve_global_market(self, **kwargs):
        key = kwargs.get("cache_key", "GLOBAL")
        return {
            "epic": f"GLOBAL.{key}",
            "name": "Spot Gold" if key == "GOLD" else key,
            "instrument_type": "COMMODITIES",
            "market_status": "TRADEABLE",
            "min_deal_size": 0.1,
            "expiry": "-",
            "bid": 1999.5,
            "offer": 2000.0,
        }


def xau_frame(rows=800, up_prob=0.90):
    index = pd.date_range(
        end="2026-01-15 02:45:00+00:00",
        periods=rows,
        freq="15min",
    )
    values = []
    price = 2000.0
    for i in range(rows):
        price += 0.12
        if i % 19 == 0:
            price -= 0.75
        values.append(price)
    close = pd.Series(values, index=index)
    open_ = close.shift(1).fillna(close.iloc[0] - 0.10)
    high = pd.concat([open_, close], axis=1).max(axis=1) + 0.40
    low = pd.concat([open_, close], axis=1).min(axis=1) - 0.40
    return pd.DataFrame(
        {
            "Open": open_,
            "High": high,
            "Low": low,
            "Close": close,
            "Volume": 1000.0,
            "UP_PROB": up_prob,
        },
        index=index,
    )


class ActiveXauRouterTests(unittest.TestCase):
    def _engine(self, frame):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return CategoryStrategyEngine(
            broker=FakeBroker(),
            frame_func=lambda seed: frame.copy(),
            state_path=os.path.join(tmp.name, "state.json"),
            scan_interval_seconds=9999,
        )

    @staticmethod
    def _seed(key):
        return next(row for row in CATEGORY_MARKET_SEEDS if row["key"] == key)

    def test_policy_constants_and_catalogue(self):
        self.assertEqual(QUANT_MIN_CONFIDENCE, 0.28)
        self.assertEqual(MODEL_AI_MIN_CONFIDENCE, 0.40)
        self.assertEqual(TREND_ADX_MIN, 25.0)
        self.assertEqual(
            CATEGORY_ORDER,
            ("FOREX", "INDICES", "CRYPTO", "METALS", "ENERGY", "SHARES"),
        )
        self.assertEqual(len(CATEGORY_MARKET_SEEDS), 40)
        self.assertEqual(ACTIVE_EXECUTION_KEYS, ("GOLD",))

    def test_forming_candle_is_not_the_execution_candle(self):
        frame = xau_frame()
        result = self._engine(frame)._evaluate_seed(self._seed("GOLD"))
        self.assertTrue(result["forming_candle_ignored"])
        self.assertEqual(result["closed_candle_index_offset"], -2)
        self.assertEqual(
            result["closed_candle_timestamp"],
            frame.index[-2].isoformat(),
        )
        self.assertNotEqual(
            result["closed_candle_timestamp"],
            frame.index[-1].isoformat(),
        )

    def test_non_gold_autonomous_entries_are_retired(self):
        result = self._engine(xau_frame())._evaluate_seed(self._seed("EURUSD"))
        self.assertEqual(result["strategy_id"], "RETIRED_ENTRY_STRATEGY")
        self.assertEqual(result["direction"], "WAIT")
        self.assertFalse(result["standard_eligible"])
        self.assertIn("OLD_ENTRY_STRATEGY_RETIRED", result["rejection_reasons"])

    def test_gold_uses_new_versioned_strategy(self):
        result = self._engine(xau_frame())._evaluate_seed(self._seed("GOLD"))
        self.assertEqual(result["strategy_id"], STRATEGY_ID)
        self.assertEqual(
            result["strategy_selection_mode"],
            "XAUUSD_MULTI_TIMEFRAME_LIQUIDITY_STRUCTURE",
        )
        self.assertEqual(
            result["model_ai_confidence_source"],
            "XAUUSD_RULE_CONFLUENCE_NOT_LEGACY_ML",
        )
        self.assertFalse(result["historical_execution_veto"])

    def test_legacy_ml_probability_does_not_change_the_xau_setup(self):
        low_ml = self._engine(xau_frame(up_prob=0.05))._evaluate_seed(
            self._seed("GOLD")
        )
        high_ml = self._engine(xau_frame(up_prob=0.95))._evaluate_seed(
            self._seed("GOLD")
        )
        self.assertEqual(low_ml["direction"], high_ml["direction"])
        self.assertEqual(
            low_ml["xauusd_strategy"]["selected_setup"]["checks"],
            high_ml["xauusd_strategy"]["selected_setup"]["checks"],
        )


if __name__ == "__main__":
    unittest.main()
