import os
import sys
import tempfile
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from category_strategy_engine import (  # noqa: E402
    ACTIVE_EXECUTION_KEYS,
    CATEGORY_MARKET_SEEDS,
    CATEGORY_ORDER,
    EVIDENCE_SCHEMA_VERSION,
    HISTORICAL_WIN_RATE_TARGET,
    MODEL_AI_MIN_CONFIDENCE,
    QUANT_MIN_CONFIDENCE,
    CategoryStrategyEngine,
)
from specialist_market_integration import (  # noqa: E402
    _enable_compound_category_coexistence,
    _extend_owned_prefix,
)


class NoCallBroker:
    def configured(self):
        return True


class OldCompound:
    VERSION = "6.7.3"

    @staticmethod
    def _foreign_positions(positions):
        return [row for row in positions if not row.get("is_compound")]


class OwnershipAwareComponent:
    VERSION = "6.10"

    def __init__(self):
        self.jasong_owned_reference_prefixes = [
            "JSCMP_",
            "JASONG_",
            "JSBND_",
            "JSLRN_",
            "JSELT_",
        ]


class SpecialistIntegrationTests(unittest.TestCase):
    def _engine(self, frame_func=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return CategoryStrategyEngine(
            broker=NoCallBroker(),
            frame_func=frame_func or (lambda seed: (_ for _ in ()).throw(AssertionError())),
            state_path=os.path.join(tmp.name, "strategies.json"),
            scan_interval_seconds=9999,
        )

    def test_policy_and_active_market_scope(self):
        self.assertEqual(QUANT_MIN_CONFIDENCE, 0.28)
        self.assertEqual(MODEL_AI_MIN_CONFIDENCE, 0.40)
        self.assertEqual(HISTORICAL_WIN_RATE_TARGET, 0.60)
        self.assertEqual(
            CATEGORY_ORDER,
            ("FOREX", "INDICES", "CRYPTO", "METALS", "ENERGY", "SHARES"),
        )
        self.assertEqual(ACTIVE_EXECUTION_KEYS, ("GOLD",))

    def test_catalogue_is_preserved_but_only_gold_is_execution_active(self):
        engine = self._engine()
        universe = engine.universe()
        self.assertEqual(len(universe), 40)
        active = [row for row in universe if row["execution_active"]]
        self.assertEqual([row["key"] for row in active], ["GOLD"])
        self.assertTrue(
            all(
                row["execution_policy"] == "ANALYSIS_ONLY_RETIRED_ENTRY_STRATEGY"
                for row in universe
                if row["key"] != "GOLD"
            )
        )

    def test_retired_market_does_not_request_market_data(self):
        engine = self._engine()
        eurusd = next(row for row in CATEGORY_MARKET_SEEDS if row["key"] == "EURUSD")
        result = engine._evaluate_seed(eurusd)
        self.assertEqual(result["strategy_id"], "RETIRED_ENTRY_STRATEGY")
        self.assertFalse(result["standard_eligible"])

    def test_only_current_schema_rows_reach_rankings(self):
        engine = self._engine()
        now = time.time()
        engine._state["evaluations"] = {
            "GOLD": {
                "key": "GOLD",
                "symbol": "GOLD",
                "market": "Gold",
                "category": "METALS",
                "version": engine.VERSION,
                "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
                "current_candle_strategy_complete": True,
                "direction": "WAIT",
                "standard_eligible": False,
                "category_rank_score": 10.0,
                "evaluated_at": now,
            },
            "OLD": {
                "key": "OLD",
                "symbol": "OLD",
                "category": "FOREX",
                "version": "6.9-old",
                "evidence_schema_version": 4,
                "current_candle_strategy_complete": True,
                "evaluated_at": now,
            },
        }
        rankings = engine.category_rankings()
        self.assertEqual([row["symbol"] for row in rankings["METALS"]], ["GOLD"])
        self.assertEqual(rankings["FOREX"], [])

    def test_old_compound_foreign_blocker_allows_jscat_but_not_manual(self):
        compound = OldCompound()
        _enable_compound_category_coexistence(compound)
        rows = compound._foreign_positions(
            [
                {"deal_reference": "JSCAT_MET_123", "is_compound": False},
                {"deal_reference": "MANUAL_123", "is_compound": False},
                {"deal_reference": "JSCMP_123", "is_compound": True},
            ]
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["deal_reference"], "MANUAL_123")

    def test_jscat_prefix_is_added_without_losing_existing_prefixes(self):
        component = OwnershipAwareComponent()
        _extend_owned_prefix(component)
        self.assertIn("JSCAT_", component.jasong_owned_reference_prefixes)
        self.assertIn("JSCMP_", component.jasong_owned_reference_prefixes)
        self.assertIn("JSELT_", component.jasong_owned_reference_prefixes)


if __name__ == "__main__":
    unittest.main()
