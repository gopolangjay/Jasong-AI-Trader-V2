from __future__ import annotations

import os
import sys
import unittest

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import adaptive_fx_v612 as v612
import category_execution_engine as execution
import risk_exit_policy


class AdaptiveFxV612PolicyTests(unittest.TestCase):
    def test_agreed_demo_minimums_are_installed(self):
        status = v612.policy_status()
        self.assertEqual(status["version"], "6.12-adaptive-fx-session-momentum-v1")
        self.assertEqual(status["market_count"], 28)
        self.assertEqual(status["optional_required"], 2)
        self.assertEqual(len(status["optional_confluence"]), 5)
        self.assertEqual(status["quant_min"], 0.20)
        self.assertEqual(status["model_ai_min"], 0.30)
        self.assertEqual(status["setup_confidence_min"], 0.30)
        self.assertEqual(status["market_structure_min"], 0.60)
        self.assertEqual(status["min_target_r"], 0.30)
        self.assertEqual(status["a_grade_min_target_r"], 0.50)
        self.assertEqual(status["preferred_target_r"], 2.0)
        self.assertTrue(status["execution_news_required"])
        self.assertTrue(status["execution_ig_safety_required"])
        self.assertFalse(status["live_money_execution"])

    def test_m15_structure_break_does_not_require_liquidity_sweep(self):
        index = pd.date_range("2026-09-04T08:00:00Z", periods=20, freq="15min")
        rows = []
        for _ in range(19):
            rows.append({
                "Open": 1.0000,
                "High": 1.0200,
                "Low": 0.9900,
                "Close": 1.0100,
                "Volume": 100.0,
            })
        rows.append({
            "Open": 1.0100,
            "High": 1.0600,
            "Low": 1.0000,
            "Close": 1.0500,
            "Volume": 120.0,
        })
        frame = pd.DataFrame(rows, index=index)
        atr = pd.Series([0.0100] * len(frame), index=index)

        result = v612._adaptive_structure_shift(
            frame,
            "BUY",
            atr,
            "BULLISH",
            sweep=None,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["source"], "RECENT_M15_STRUCTURE_BREAK")
        self.assertIn(result["type"], {"BOS", "CHOCH_CISD_MSS"})

    def test_adaptive_risk_policy_allows_point_three_r(self):
        candidate = {
            "strategy_id": v612.STRATEGY_ID,
            "category": "FOREX",
            "structural_stop_distance": 0.0020,
            "target_r": 0.30,
        }
        plan = risk_exit_policy.build_risk_plan(
            candidate,
            entry_price=1.2000,
            direction="BUY",
        )
        self.assertEqual(plan.target_r, 0.30)
        self.assertAlmostEqual(plan.target_distance, 0.0006, places=10)
        self.assertEqual(plan.version, "v612-adaptive-fx-structural-risk-v1")

    def test_execution_engine_accepts_new_strategy_id(self):
        self.assertIn(
            v612.STRATEGY_ID,
            execution.CategoryExecutionEngine.ACTIVE_STRATEGY_IDS,
        )
        self.assertEqual(
            execution.CategoryExecutionEngine.VERSION,
            "6.12-fx-adaptive-execution-v1",
        )


if __name__ == "__main__":
    unittest.main()
