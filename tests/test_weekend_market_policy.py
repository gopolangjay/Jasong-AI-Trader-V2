import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from weekend_market_policy import assess_market, discover_open_supported, execution_guard


class WeekendMarketPolicyTests(unittest.TestCase):
    def setUp(self):
        self.old = os.environ.get("JASONG_WEEKEND_SUPPORTED_CATEGORIES")
        os.environ["JASONG_WEEKEND_SUPPORTED_CATEGORIES"] = "CRYPTO"

    def tearDown(self):
        if self.old is None:
            os.environ.pop("JASONG_WEEKEND_SUPPORTED_CATEGORIES", None)
        else:
            os.environ["JASONG_WEEKEND_SUPPORTED_CATEGORIES"] = self.old

    def test_open_supported_market_is_eligible(self):
        result = assess_market({"epic": "CS.D.BTCUSD.CFD.IP", "name": "Bitcoin", "category": "CRYPTO", "marketStatus": "TRADEABLE", "bid": 100.0, "offer": 101.0})
        self.assertTrue(result["eligible"])

    def test_closed_market_fails_closed(self):
        result = assess_market({"epic": "CS.D.BTCUSD.CFD.IP", "category": "CRYPTO", "marketStatus": "CLOSED", "bid": 100.0, "offer": 101.0})
        self.assertFalse(result["eligible"])
        self.assertIn("IG_NOT_TRADEABLE", result["reasons"])

    def test_fx_cannot_sneak_into_weekend_strategy(self):
        result = assess_market({"epic": "CS.D.EURUSD.MINI.IP", "category": "CURRENCIES", "marketStatus": "TRADEABLE", "bid": 1.1, "offer": 1.1002})
        self.assertFalse(result["eligible"])
        self.assertIn("UNSUPPORTED_CATEGORY", result["reasons"])

    def test_missing_quote_fails_closed(self):
        result = assess_market({"epic": "X", "category": "CRYPTO", "marketStatus": "TRADEABLE"})
        self.assertFalse(result["eligible"])

    def test_discovery_only_returns_open_supported(self):
        markets = [
            {"epic": "A", "category": "CRYPTO", "marketStatus": "TRADEABLE", "bid": 10, "offer": 11},
            {"epic": "B", "category": "CRYPTO", "marketStatus": "CLOSED", "bid": 10, "offer": 11},
            {"epic": "C", "category": "CURRENCIES", "marketStatus": "TRADEABLE", "bid": 10, "offer": 11},
        ]
        self.assertEqual([m["epic"] for m in discover_open_supported(markets)], ["A"])

    def test_spread_guard(self):
        result = execution_guard({"epic": "A", "category": "CRYPTO", "marketStatus": "TRADEABLE", "bid": 10, "offer": 12}, max_spread=1)
        self.assertFalse(result["eligible"])
        self.assertIn("SPREAD_TOO_WIDE", result["reasons"])


if __name__ == "__main__":
    unittest.main()
