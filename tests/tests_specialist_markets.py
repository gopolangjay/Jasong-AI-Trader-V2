import os
import sys
import tempfile
import time
import unittest

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from category_strategy_engine import (  # noqa: E402
    CATEGORY_MARKET_SEEDS,
    CATEGORY_ORDER,
    COMPOUND_SLOTS_PER_CATEGORY,
    HISTORICAL_WIN_RATE_TARGET,
    MODEL_AI_MIN_CONFIDENCE,
    QUANT_MIN_CONFIDENCE,
    TOP_N_PER_CATEGORY,
    CategoryStrategyEngine,
)
from category_execution_engine import CategoryExecutionEngine  # noqa: E402
from specialist_market_integration import (  # noqa: E402
    _enable_compound_category_coexistence,
    _extend_owned_prefix,
)


class FakeBroker:
    def __init__(self):
        self._positions = []
        self.open_calls = []
        self.close_calls = []

    def configured(self):
        return True

    @staticmethod
    def _min_deal_size(_details):
        return 0.1

    def resolve_market(self, symbol, require_tradeable=False):
        details = {
            "instrument": {"name": symbol, "type": "CURRENCIES", "expiry": "-"},
            "snapshot": {"marketStatus": "TRADEABLE", "bid": 1.20000, "offer": 1.20005},
        }
        return {
            "symbol": symbol,
            "epic": f"FX.{symbol.replace('/', '')}",
            "name": symbol,
            "instrument_type": "CURRENCIES",
            "market_status": "TRADEABLE",
            "expiry": "-",
            "details": details,
        }

    def resolve_global_market(self, **kwargs):
        key = kwargs.get("cache_key", "GLOBAL")
        details = {
            "instrument": {"name": key, "type": "INDICES", "expiry": "-"},
            "snapshot": {"marketStatus": "TRADEABLE", "bid": 100.00, "offer": 100.05},
        }
        return {
            "symbol": key,
            "epic": f"GLOBAL.{key}",
            "name": key,
            "instrument_type": "INDICES",
            "market_status": "TRADEABLE",
            "min_deal_size": 0.1,
            "expiry": "-",
            "details": details,
        }

    def positions(self):
        return {"positions": list(self._positions)}

    def open_epic_position(self, *, epic, direction, size=None, deal_reference=None):
        deal_id = f"D{len(self._positions) + 1}"
        item = {
            "position": {
                "dealId": deal_id,
                "dealReference": deal_reference,
                "direction": direction,
                "size": size,
                "level": 100.0,
            },
            "market": {
                "epic": epic,
                "instrumentName": epic,
                "marketStatus": "TRADEABLE",
                "bid": 100.0,
                "offer": 100.05,
            },
        }
        self._positions.append(item)
        self.open_calls.append((epic, direction, size, deal_reference))
        return {
            "dealId": deal_id,
            "dealReference": deal_reference,
            "size": size,
            "level": 100.0,
            "dealStatus": "ACCEPTED",
        }

    def close_position(self, deal_id):
        self.close_calls.append(deal_id)
        self._positions = [
            row for row in self._positions
            if str((row.get("position") or {}).get("dealId")) != str(deal_id)
        ]
        return {"dealId": deal_id, "status": "ACCEPTED", "closeVerified": True}


def trending_frame(rows=720, up_prob=0.90):
    index = pd.date_range("2026-01-01", periods=rows, freq="15min", tz="UTC")
    close = pd.Series([1.0 + i * 0.00020 for i in range(rows)], index=index)
    open_ = close.shift(1).fillna(close.iloc[0] - 0.0001)
    frame = pd.DataFrame({
        "Open": open_,
        "High": close + 0.00040,
        "Low": close - 0.00040,
        "Close": close,
        "Volume": 1000.0,
        "UP_PROB": up_prob,
    }, index=index)
    return frame


class OldCompound:
    VERSION = "6.7.3"

    @staticmethod
    def _foreign_positions(positions):
        return [row for row in positions if not row.get("is_compound")]


class NewOwnershipAwareComponent:
    VERSION = "6.8.19"

    def __init__(self):
        self.jasong_owned_reference_prefixes = [
            "JSCMP_", "JASONG_", "JSBND_", "JSLRN_", "JSELT_"
        ]


class SpecialistStrategyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.broker = FakeBroker()
        self.engine = CategoryStrategyEngine(
            broker=self.broker,
            frame_func=lambda seed: trending_frame(),
            state_path=os.path.join(self.tmp.name, "strategies.json"),
            scan_interval_seconds=9999,
            batch_size=6,
        )

    def tearDown(self):
        self.engine.stop_thread()
        self.tmp.cleanup()

    def test_policy_is_28_40_and_70_validation(self):
        self.assertEqual(QUANT_MIN_CONFIDENCE, 0.28)
        self.assertEqual(MODEL_AI_MIN_CONFIDENCE, 0.40)
        self.assertEqual(HISTORICAL_WIN_RATE_TARGET, 0.70)
        self.assertEqual(CATEGORY_ORDER, ("FOREX", "INDICES", "CRYPTO", "METALS", "ENERGY", "SHARES"))

    def test_forex_specialist_can_earn_real_historical_verification(self):
        eurusd = next(seed for seed in CATEGORY_MARKET_SEEDS if seed["key"] == "EURUSD")
        row = self.engine._evaluate_seed(eurusd)
        self.assertEqual(row["category"], "FOREX")
        self.assertEqual(row["strategy_id"], "FX_REGIME_TREND_PULLBACK_V1")
        self.assertTrue(row["ai28_pass"])
        self.assertTrue(row["ai40_pass"])
        self.assertGreaterEqual(row["historical_win_rate"], 0.70)
        self.assertTrue(row["historical_70_verified"])
        self.assertGreaterEqual(row["historical_trades"], 30)
        self.assertTrue(row["spread_pass"])
        self.assertTrue(row["standard_eligible"])
        self.assertGreaterEqual(len(row["recent_returns"]), 20)

    def test_no_model_probability_cannot_fake_ai40(self):
        eurusd = next(seed for seed in CATEGORY_MARKET_SEEDS if seed["key"] == "EURUSD")
        row = self.engine._evaluate_seed({**eurusd}) if False else None
        # New engine with no UP_PROB in the frame: BUY directional AI is zero.
        engine = CategoryStrategyEngine(
            broker=self.broker,
            frame_func=lambda seed: trending_frame().drop(columns=["UP_PROB"]),
            state_path=os.path.join(self.tmp.name, "no_ai.json"),
        )
        result = engine._evaluate_seed(eurusd)
        self.assertFalse(result["ai40_pass"])
        self.assertFalse(result["standard_eligible"])

    def test_rankings_are_capped_at_five_and_only_top_two_can_compound(self):
        now = time.time()
        evaluations = {}
        for idx in range(7):
            evaluations[f"F{idx}"] = {
                "key": f"F{idx}", "symbol": f"F{idx}", "market": f"Forex {idx}",
                "category": "FOREX", "direction": "BUY", "standard_eligible": True,
                "smart_fast_score": 99 - idx, "model_ai_confidence": 0.80,
                "quant_confidence": 0.75, "category_rank_score": 99 - idx,
                "evaluated_at": now,
            }
        self.engine._state["evaluations"] = evaluations
        ranked = self.engine.category_rankings("FOREX")["FOREX"]
        self.assertEqual(len(ranked), TOP_N_PER_CATEGORY)
        compound = [row for row in ranked if row["compound_eligible"]]
        self.assertEqual(len(compound), COMPOUND_SLOTS_PER_CATEGORY)
        self.assertEqual([row["category_rank"] for row in compound], [1, 2])

    def test_correlation_uses_real_recent_returns(self):
        now = time.time()
        seq = [i / 10000 for i in range(1, 50)]
        self.engine._state["evaluations"] = {
            "A": {"symbol": "A", "category": "FOREX", "evaluated_at": now, "recent_returns": seq, "category_rank_score": 90},
            "B": {"symbol": "B", "category": "FOREX", "evaluated_at": now, "recent_returns": [x * 2 for x in seq], "category_rank_score": 89},
        }
        corr = self.engine.correlation_matrix()
        self.assertAlmostEqual(corr["A"]["B"], 1.0, places=6)

    def test_old_compound_foreign_blocker_allows_jscat_but_not_manual(self):
        compound = OldCompound()
        _enable_compound_category_coexistence(compound)
        rows = compound._foreign_positions([
            {"deal_reference": "JSCAT_FOR_123", "is_compound": False},
            {"deal_reference": "MANUAL_123", "is_compound": False},
            {"deal_reference": "JSCMP_123", "is_compound": True},
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["deal_reference"], "MANUAL_123")


    def test_jscat_prefix_is_added_without_losing_existing_owned_prefixes(self):
        component = NewOwnershipAwareComponent()
        _extend_owned_prefix(component)
        self.assertIn("JSCAT_", component.jasong_owned_reference_prefixes)
        self.assertIn("JSCMP_", component.jasong_owned_reference_prefixes)
        self.assertIn("JSELT_", component.jasong_owned_reference_prefixes)


class CategoryExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.broker = FakeBroker()

    def tearDown(self):
        self.tmp.cleanup()

    def test_category_and_compound_same_epic_are_allowed_as_two_tracks(self):
        candidate = {
            "category": "FOREX", "category_rank": 1, "strategy_id": "FX_REGIME_TREND_PULLBACK_V1",
            "strategy_name": "FX Regime + Trend Pullback", "symbol": "EURUSD", "market": "EUR/USD",
            "direction": "BUY", "ig_epic": "FX.EURUSD", "ig_min_deal_size": 0.1,
            "standard_eligible": True, "holding_bars": 4, "exposure_tags": ["EUR", "USD"],
            "quant_confidence": 0.65, "model_ai_confidence": 0.75, "historical_win_rate": 0.74,
            "historical_profit_factor": 1.6, "smart_fast_score": 95.0,
        }
        external = [{
            "track": "COMPOUND", "epic": "FX.EURUSD", "direction": "BUY",
            "exposure_tags": ["EUR", "USD"],
        }]
        engine = CategoryExecutionEngine(
            broker=self.broker,
            ranking_source=lambda: {"FOREX": [candidate]},
            external_positions_source=lambda: external,
            state_path=os.path.join(self.tmp.name, "portfolio.json"),
            poll_seconds=999,
        )
        allowed, _ = engine._may_open(candidate, external)
        self.assertTrue(allowed)
        engine._open_candidate(candidate, external)
        self.assertEqual(len(self.broker.open_calls), 1)
        self.assertTrue(self.broker.open_calls[0][3].startswith("JSCAT_"))
        self.assertTrue(engine.positions()[0]["dual_track"])
        # A third track on the same EPIC must be blocked.
        allowed_again, reason = engine._may_open(candidate, external)
        self.assertFalse(allowed_again)
        self.assertTrue("duplicate" in reason or "EPIC" in reason)


    def test_combined_account_capacity_blocks_category_entry_at_fifteen(self):
        candidate = {
            "category": "FOREX", "category_rank": 1, "strategy_id": "FX_REGIME_TREND_PULLBACK_V1",
            "strategy_name": "FX Regime + Trend Pullback", "symbol": "EURUSD", "market": "EUR/USD",
            "direction": "BUY", "ig_epic": "FX.EURUSD", "ig_min_deal_size": 0.1,
            "standard_eligible": True, "holding_bars": 4, "exposure_tags": ["EUR", "USD"],
            "quant_confidence": 0.65, "model_ai_confidence": 0.75, "historical_win_rate": 0.74,
            "historical_profit_factor": 1.6, "smart_fast_score": 95.0,
        }
        external = [
            {"track": "COMPOUND" if i < 5 else "JASONG_LEARNING", "epic": f"X.{i}", "exposure_tags": []}
            for i in range(15)
        ]
        engine = CategoryExecutionEngine(
            broker=self.broker,
            ranking_source=lambda: {"FOREX": [candidate]},
            external_positions_source=lambda: external,
            state_path=os.path.join(self.tmp.name, "cap.json"),
            poll_seconds=999,
        )
        allowed, reason = engine._may_open(candidate, external)
        self.assertFalse(allowed)
        self.assertIn("global IG DEMO position cap", reason)


if __name__ == "__main__":
    unittest.main()
