import sys
import unittest
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from mobile_sync import MobileSyncCache  # noqa: E402


class _Portfolio:
    def status(self):
        return {
            "version": "6.11-fx-xau-active-execution-v1",
            "enabled": True,
            "execution_mode": "IG_DEMO_ONLY",
            "active_strategy": "FX_LIQUIDITY_LINES_V1 + XAUUSD_LIQUIDITY_STRUCTURE_V1",
            "active_strategies": ["FX_LIQUIDITY_LINES_V1", "XAUUSD_LIQUIDITY_STRUCTURE_V1"],
            "active_symbol": "28_LIQUID_FX_PAIRS + GOLD",
            "old_entry_strategies_retired": True,
            "risk_per_trade_pct": 1.0,
            "max_daily_entries_south_africa": {"FOREX": 4, "METALS": 2},
            "minimum_target_r": 2.0,
            "live_money_execution": False,
        }


class MobileSyncV611Tests(unittest.TestCase):
    def test_mobile_snapshot_exposes_active_fx_xau_risk_policy(self):
        cache = object.__new__(MobileSyncCache)
        cache.portfolio = _Portfolio()

        status = cache._portfolio_status([], [])

        self.assertEqual(MobileSyncCache.VERSION, "6.11-fx-xau-mobile-sync")
        self.assertEqual(MobileSyncCache.CATEGORIES, ("FOREX", "METALS"))
        self.assertEqual(
            status["active_strategy"],
            "FX_LIQUIDITY_LINES_V1 + XAUUSD_LIQUIDITY_STRUCTURE_V1",
        )
        self.assertEqual(status["active_symbol"], "28_LIQUID_FX_PAIRS + GOLD")
        self.assertTrue(status["old_entry_strategies_retired"])
        self.assertEqual(status["risk_per_trade_pct"], 1.0)
        self.assertEqual(status["minimum_target_r"], 2.0)
        self.assertFalse(status["live_money_execution"])
        self.assertEqual(status["status_source"], "CACHED_INTERNAL_STATE")


if __name__ == "__main__":
    unittest.main()
