import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from trade_excursions import TradeExcursionTracker


class FakeBroker:
    def __init__(self):
        self.close_calls = []
        self.update_calls = []
        self.confirm_result = {
            "dealStatus": "REJECTED",
            "reason": "ATTACHED_ORDER_LEVEL_ERROR",
            "limitLevel": None,
            "stopLevel": None,
        }

    def market_details(self, epic):
        return {
            "snapshot": {
                "marketStatus": "TRADEABLE",
                "bid": 77231.0,
                "offer": 77276.0,
            },
            "instrument": {
                "name": "Bitcoin",
            },
            "dealingRules": {
                "minNormalStopOrLimitDistance": {
                    "unit": "POINTS",
                    "value": 500.0,
                },
            },
        }

    def _request(self, method, path, version=1, payload=None, **kwargs):
        self.update_calls.append((method, path, version, payload))
        return {"dealReference": "REF1"}

    def confirm(self, ref):
        return dict(self.confirm_result)

    def close_position(self, deal_id):
        self.close_calls.append(deal_id)
        return {
            "dealId": deal_id,
            "status": "ACCEPTED",
            "closeVerified": True,
            "level": 77231.0,
        }


class RiskWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.broker = FakeBroker()
        self.tracker = TradeExcursionTracker(
            broker=self.broker,
            state_path=os.path.join(self.tmp.name, "state.json"),
            poll_seconds=60,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _risk_record(self):
        return {
            "deal_id": "D1",
            "deal_reference": "JSCAT_CRY_TEST",
            "direction": "BUY",
            "entry_price": 77499.0,
            "current_price": 77231.0,
            "current_bid": 77231.0,
            "current_offer": 77276.0,
            "epic": "CS.D.BITCOIN.CFBMU.IP",
            "status": "OPEN",
            "jasong_owned": True,
            "risk_policy_version": "v63-risk-exit-v1",
            "planned_risk_price_distance": 464.994,
            "planned_target_r": 1.5,
            "protective_stop_price": 77034.006,
            "take_profit_target_price": 78196.491,
            "highest_price_since_entry": 77499.0,
            "lowest_price_since_entry": 76824.0,
        }

    def test_ig_minimum_moves_stop_outward_but_not_planned_stop(self):
        record = self._risk_record()
        normalised = self.tracker._normalise_native_levels(record)

        self.assertTrue(normalised["normalised"])
        self.assertEqual(record["protective_stop_price"], 77034.006)

        # Current 77231 - minimum 500 - 5% safety margin 25 = 76706.
        self.assertAlmostEqual(normalised["stop_level"], 76706.0, places=6)

        # Planned target is already farther than current + 525.
        self.assertAlmostEqual(normalised["limit_level"], 78196.491, places=6)

    def test_mae_breach_closes_even_after_current_price_recovers(self):
        record = self._risk_record()
        self.tracker._calculate(record)
        self.tracker._update_r_telemetry(record, 1000.0)

        # Current is only about -0.576R, but historical MAE is about -1.452R.
        self.assertGreater(record["current_favourable_r"], -1.0)
        self.assertLess(record["mae_r"], -1.0)

        reason = self.tracker._server_exit_reason(record, 1000.0)
        self.assertEqual(reason, "HARD_STOP_1R")

    def test_rejected_native_order_is_suppressed(self):
        record = self._risk_record()
        self.tracker._state["trades"]["D1"] = record
        self.tracker._attach_native_orders("D1")

        saved = self.tracker._state["trades"]["D1"]
        self.assertEqual(saved["native_take_profit_state"], "REJECTED")
        self.assertEqual(saved["native_protective_stop_state"], "REJECTED")
        self.assertTrue(saved["native_order_suppressed"])
        self.assertEqual(len(self.broker.update_calls), 1)

        # No second broker call for the same rejected trade.
        self.assertFalse(self.tracker._native_order_needed(saved, 9999999999.0))

    def test_server_close_records_hard_stop_reason(self):
        record = self._risk_record()
        self.tracker._state["trades"]["D1"] = record
        self.tracker._execute_server_close("D1", "HARD_STOP_1R")

        saved = self.tracker._state["trades"]["D1"]
        self.assertEqual(saved["close_reason"], "HARD_STOP_1R")
        self.assertEqual(saved["hard_stop_close_state"], "CLOSE_VERIFIED")
        self.assertEqual(self.broker.close_calls, ["D1"])


if __name__ == "__main__":
    unittest.main()
