from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from forward_store import ForwardStore
from forward_validation import ForwardValidationConfig, ForwardValidationEngine
from prime_policy import ForwardPrimeArchitecture
from provenance import ProvenanceRegistry
from strategy_learning import StrategyLearningEngine


class FakeIntelligence:
    def __init__(self, rows):
        self.rows = rows

    def _fresh_rows(self):
        return [dict(row) for row in self.rows]


class ForwardValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ForwardStore(os.path.join(self.tmp.name, "forward.sqlite3"))

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def rows(count=12, wins=9, strategy="TREND_CONTINUATION"):
        now = time.time()
        output = []
        for i in range(count):
            win = i < wins
            output.append({
                "trade_id": f"T{i}",
                "strategy_id": strategy,
                "symbol": "META",
                "direction": "BUY",
                "broker_result": "WIN" if win else "LOSS",
                "opened_at": now - 2000 + i * 60,
                "closed_at": now - 1900 + i * 60,
                "r_multiple": 1.0 if win else -1.0,
                "r_source": "TEST",
            })
        return output

    def test_prime_uses_forward_settled_metrics(self):
        rows = self.rows()
        engine = ForwardValidationEngine(
            store=self.store,
            evidence_source=lambda: rows,
            config=ForwardValidationConfig(
                min_settled_trades_for_prime=12,
                rolling_window_trades=40,
                min_profit_factor=1.20,
                min_expectancy_r=0.05,
                min_win_rate=0.45,
                min_bootstrap_prob_positive_expectancy=0.75,
                max_drawdown_r=6.0,
                bootstrap_samples=1000,
            ),
        )
        metrics = engine.metrics(strategy_id="TREND_CONTINUATION")
        self.assertEqual(metrics["settled_trades"], 12)
        self.assertAlmostEqual(metrics["win_rate"], 0.75)
        self.assertAlmostEqual(metrics["profit_factor"], 3.0)
        self.assertTrue(metrics["prime_eligible"])

    def test_prime_bootstrap_blocks_small_sample(self):
        rows = self.rows(count=6, wins=6)
        engine = ForwardValidationEngine(
            store=self.store,
            evidence_source=lambda: rows,
            config=ForwardValidationConfig(bootstrap_samples=500),
        )
        metrics = engine.metrics(strategy_id="TREND_CONTINUATION")
        self.assertFalse(metrics["checks"]["minimum_settled_trades"])
        self.assertFalse(metrics["prime_eligible"])

    def test_historical_failure_is_informational_only(self):
        now = time.time()
        candidate = {
            "key": "META",
            "symbol": "META",
            "market": "Meta Platforms",
            "category": "SHARES",
            "strategy_id": "TREND_CONTINUATION",
            "strategy_name": "Trend continuation",
            "direction": "BUY",
            "quant_confidence": 0.35,
            "model_ai_confidence": 0.55,
            "smart_fast_score": 60.0,
            "ig_tradeable": True,
            "ig_quote_source": "REFRESHED_EPIC_SNAPSHOT",
            "ig_spread_bps": 5.0,
            "spread_gate_bps": 35.0,
            "spread_pass": True,
            "evaluated_at": now,
            "historical_win_rate": 0.20,
            "historical_profit_factor": 0.50,
            "historical_trades": 100,
            "historical_target_verified": False,
            "walk_forward_pass": False,
            "quality_tier": "C",
            "deep_status": "REJECT",
            "rejection_reasons": ["HOLDOUT_WR_BELOW_60", "WALK_FORWARD_BELOW_40"],
        }
        registry = ProvenanceRegistry("YAHOO_FINANCE")
        registry._frames["META"] = {
            "analysis_price_source": "YAHOO_FINANCE",
            "analysis_price_timestamp": now,
        }
        architecture = ForwardPrimeArchitecture(
            intelligence=FakeIntelligence([candidate]),
            broker=object(),
            provenance_registry=registry,
            legacy_evidence_source=None,
            state_dir=self.tmp.name,
        )
        architecture.validator.evidence_source = lambda: self.rows()
        row = architecture.enrich(candidate)
        self.assertEqual(row["historical_validation_mode"], "INFORMATIONAL_ONLY")
        self.assertTrue(row["strong_qualified"])
        self.assertTrue(row["prime_qualified"])
        self.assertNotIn("HOLDOUT_WR_BELOW_60", row["rejection_reasons"])
        self.assertEqual(row["quality_basis"], "LIVE_STRONG_POLICY")

    def test_stale_broker_quote_blocks_strong(self):
        now = time.time()
        candidate = {
            "key": "META",
            "symbol": "META",
            "market": "Meta Platforms",
            "category": "SHARES",
            "strategy_id": "TREND_CONTINUATION",
            "direction": "BUY",
            "quant_confidence": 0.35,
            "model_ai_confidence": 0.55,
            "smart_fast_score": 60.0,
            "ig_tradeable": True,
            "ig_quote_source": "REFRESHED_EPIC_SNAPSHOT",
            "broker_quote_timestamp": now - 1000,
            "spread_pass": True,
            "evaluated_at": now,
        }
        registry = ProvenanceRegistry("YAHOO_FINANCE")
        registry._frames["META"] = {
            "analysis_price_source": "YAHOO_FINANCE",
            "analysis_price_timestamp": now,
        }
        architecture = ForwardPrimeArchitecture(
            intelligence=FakeIntelligence([candidate]),
            broker=object(),
            provenance_registry=registry,
            legacy_evidence_source=None,
            state_dir=self.tmp.name,
        )
        row = architecture.enrich(candidate)
        self.assertFalse(row["strong_qualified"])
        self.assertIn("BROKER_QUOTE_STALE", row["rejection_reasons"])

    def test_learning_reports_repeated_weak_trend_losses(self):
        rows = []
        for i in range(4):
            rows.append({
                "trade_id": f"L{i}",
                "strategy_id": "TREND_CONTINUATION",
                "symbol": "EURUSD",
                "direction": "BUY",
                "broker_result": "LOSS",
                "entry_snapshot": {"adx": 15.0, "rsi": 50.0},
                "provenance": {},
            })
        report = StrategyLearningEngine().analyze(rows)
        names = {item["mistake"] for item in report["findings"]}
        self.assertIn("WEAK_TREND_ENTRY", names)
        self.assertFalse(report["automatic_strategy_rewrite"])


if __name__ == "__main__":
    unittest.main()
