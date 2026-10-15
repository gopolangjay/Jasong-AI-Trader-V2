from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "backend"))
from regime_integration import RegimeStrategyEngine, RegimeExecutionEngine, RegimePrimeArchitecture
from regime_strategy import TREND_ID, REVERSAL_ID
from category_strategy_engine import CATEGORY_MARKET_SEEDS
from category_execution_engine import RiskSizingError
from risk_exit_policy import build_risk_plan
from ig_demo_broker import IGDemoBroker, IGDemoError
from test_regime_strategy import trend_candles, range_candles

NOW = pd.Timestamp("2026-10-15 11:00Z")


class DemoBroker:
    def __init__(self):
        self.opened, self.closed = [], []
        self.spread = 0.0001
        self.strip_protection = False
        self.fill_slippage = 0

    def status(self):
        return {"account_id": "DEMO1"}

    def positions(self):
        return {"positions": []}

    def accounts(self):
        return {"accounts": [{"accountId": "DEMO1", "currency": "USD", "balance": {"balance": 10000, "available": 10000}}]}

    def market_details(self, epic, require_quote=False):
        return {"instrument": {"currencies": [{"code": "USD", "isDefault": True}]},
                "snapshot": {"bid": 1.2-self.spread, "offer": 1.2, "marketStatus": "TRADEABLE"},
                "dealingRules": {"minDealSize": {"value": 0.1}, "sizeIncrement": {"value": 0.1}}}

    def resolve_market(self, symbol, require_tradeable=False):
        return {"epic": "EURUSD.DEMO", "name": "EUR/USD", "market_status": "TRADEABLE", "details": self.market_details("EURUSD.DEMO")}

    @staticmethod
    def _min_deal_size(_):
        return 0.1

    @staticmethod
    def _deal_size_increment(_):
        return 0.1

    def estimate_closed_position_pnl(self, **kwargs):
        return {"account_pnl": -abs(kwargs["entry_level"]-kwargs["exit_level"])*10000*kwargs["size"], "native_currency": "USD"}

    def open_epic_position(self, **kwargs):
        self.opened.append(kwargs)
        result = {"dealId": "D1", "dealReference": kwargs["deal_reference"], "level": 1.2+self.fill_slippage,
                  "size": kwargs["size"], "dealStatus": "ACCEPTED"}
        if not self.strip_protection:
            result.update({"attached_stop_level": kwargs["stop_level"], "attached_limit_level": kwargs["limit_level"]})
        return result

    def close_position(self, deal_id):
        self.closed.append(deal_id)
        return {"status": "ACCEPTED", "closeVerified": True}


def candidate():
    return {"symbol": "EURUSD", "category": "FOREX", "direction": "BUY", "strategy_id": TREND_ID,
            "signal_qualified": True, "standard_eligible": True, "session_active": True,
            "signal_timestamp": NOW.isoformat(), "setup_id": "stable-event", "news_guard": {"clear": True},
            "ig_tradeable": True, "spread_pass": True, "liquidity_pass": True, "panic_volatility_pass": True,
            "regime_strategy": {"rejection_reasons": []}, "ig_epic": "EURUSD.DEMO",
            "ig_bid": 1.1999, "ig_offer": 1.2, "ig_spread_bps": 1,
            "live_price": 1.0, "atr": .005, "structural_stop_distance": .005,
            "target_r": 2, "spread_gate_bps": 8, "max_spread_atr": .15, "max_hold_seconds": 3600}


class RegimeExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.broker = DemoBroker()
        self.engine = RegimeExecutionEngine(broker=self.broker, ranking_source=lambda: {},
                                            state_path=os.path.join(self.tmp.name, "positions.json"), now_func=lambda: NOW)

    def test_real_trend_pipeline_reaches_broker_preflight(self):
        engine = RegimeStrategyEngine(broker=self.broker, frame_func=lambda _: trend_candles(),
                                     state_path=os.path.join(self.tmp.name, "signals.json"), now_func=lambda: NOW)
        seed = next(s for s in CATEGORY_MARKET_SEEDS if s["key"] == "EURUSD")
        row = engine._evaluate_seed(seed)
        self.assertEqual(row["strategy_id"], TREND_ID)
        self.assertTrue(row["standard_eligible"], row["rejection_reasons"])
        self.assertTrue(row["liquidity_pass"])
        self.assertEqual(row["version"], "6.14-regime-adaptive-v1")

    def test_real_reversal_reaches_protected_risk_sizing(self):
        engine = RegimeStrategyEngine(broker=self.broker, frame_func=lambda _: range_candles(),
                                     state_path=os.path.join(self.tmp.name, "range.json"), now_func=lambda: NOW)
        seed = next(s for s in CATEGORY_MARKET_SEEDS if s["key"] == "EURUSD")
        row = engine._evaluate_seed(seed)
        self.assertTrue(row["standard_eligible"], row["rejection_reasons"])
        self.assertEqual(row["strategy_id"], REVERSAL_ID)
        self.engine._open_candidate(row, [])
        self.assertEqual(len(self.broker.opened), 1)
        self.assertIn("stop_level", self.broker.opened[0])

    def test_forward_prime_does_not_bypass_protected_executor_via_compound(self):
        from prime_policy import ForwardPrimeArchitecture
        prime = object.__new__(RegimePrimeArchitecture)
        with patch.object(ForwardPrimeArchitecture, "category_rankings", return_value={"FOREX": [
            {**candidate(), "compound_eligible": True, "prime_qualified": True}]}):
            result = prime.category_rankings()
        self.assertFalse(result["FOREX"][0]["compound_eligible"])
        self.assertIn("ATTACHED_STOP", result["FOREX"][0]["compound_blocker"])

    def test_prime_cannot_promote_news_or_liquidity_failure(self):
        prime = object.__new__(RegimePrimeArchitecture)
        row = candidate()
        self.assertTrue(prime._strong_gate(row, {"issues": []})[0])
        for key in ("signal_qualified", "liquidity_pass", "session_active", "panic_volatility_pass", "spread_pass"):
            with self.subTest(key=key):
                self.assertFalse(prime._strong_gate({**row, key: False}, {"issues": []})[0])
        self.assertFalse(prime._strong_gate({**row, "news_guard": {"clear": False}}, {"issues": []})[0])
        self.assertFalse(prime._strong_gate(row, {"issues": ["BROKER_QUOTE_STALE"]})[0])

    def test_higher_volatility_reduces_size_at_same_cash_budget(self):
        low = self.engine._risk_sized_order(candidate())
        high = self.engine._risk_sized_order({**candidate(), "structural_stop_distance": .01, "atr": .01})
        self.assertLess(high["size"], low["size"])
        self.assertEqual(high["risk_cash"], low["risk_cash"])
        self.assertLessEqual(high["estimated_stop_risk_cash"], high["risk_cash"])

    def test_broker_quote_unit_scaling_preserves_relative_risk(self):
        row = candidate()
        normal = build_risk_plan(row, entry_price=1.2, direction="BUY")
        points = build_risk_plan(row, entry_price=12000, direction="BUY")
        self.assertAlmostEqual(normal.stop_pct, points.stop_pct)
        self.assertAlmostEqual(points.stop_distance/normal.stop_distance, 10000)

    def test_widened_fresh_spread_blocks_before_submission(self):
        self.broker.spread = .01
        with self.assertRaises(RiskSizingError):
            self.engine._risk_sized_order(candidate())
        self.assertEqual(self.broker.opened, [])

    def test_new_order_has_attached_protection_and_stable_tracker_levels(self):
        self.engine._open_candidate(candidate(), [])
        order = self.broker.opened[0]
        self.assertLess(order["stop_level"], 1.2)
        self.assertGreater(order["limit_level"], 1.2)
        self.assertFalse(order["allow_size_increment_retry"])
        position = self.engine.positions()[0]
        self.assertEqual(position["protective_stop_price"], order["stop_level"])
        self.assertEqual(position["take_profit_target_price"], order["limit_level"])
        self.assertLessEqual(position["estimated_stop_risk_cash"], 100)

    def test_missing_protection_or_excessive_fill_risk_closes_immediately(self):
        for missing, slippage in ((True, 0), (False, .01)):
            self.broker.strip_protection, self.broker.fill_slippage = missing, slippage
            with self.assertRaises(RuntimeError):
                self.engine._open_candidate(candidate(), [])
        self.assertEqual(self.broker.closed, ["D1", "D1"])
        self.assertEqual(self.engine.positions(), [])

    def test_duplicate_setup_and_stale_signal_are_blocked(self):
        self.engine._state["positions"] = [{"status": "CLOSED", "setup_id": "stable-event"}]
        allowed, reason = self.engine._may_open(candidate(), [])
        self.assertFalse(allowed)
        self.assertIn("already traded", reason)
        for timestamp in (None, "garbage", "2026-10-15T10:00:00Z", "2026-10-15T12:00:00Z"):
            allowed, _ = self.engine._may_open({**candidate(), "signal_timestamp": timestamp}, [])
            self.assertFalse(allowed)

    def test_new_ids_do_not_enable_other_markets(self):
        self.assertFalse(self.engine._is_active_strategy({**candidate(), "symbol": "AAPL", "category": "SHARES"}))
        self.assertTrue(self.engine._is_active_strategy({**candidate(), "strategy_id": REVERSAL_ID}))


class AttachedOrderBrokerTests(unittest.TestCase):
    def setUp(self):
        self.broker = IGDemoBroker()
        self.details = {"instrument": {"type": "CURRENCIES", "currencies": [{"code": "USD", "isDefault": True}]},
                        "snapshot": {"marketStatus": "TRADEABLE", "bid": 1.1999, "offer": 1.2},
                        "dealingRules": {"minNormalStopOrLimitDistance": {"unit": "POINTS", "value": .001}}}
        self.broker.market_details = Mock(side_effect=lambda *_, **__: self.details)
        self.broker._min_deal_size = Mock(return_value=.1)
        self.broker._deal_size_increment = Mock(return_value=.1)
        self.broker._request = Mock(return_value={"dealReference": "REF"})
        self.broker.confirm = Mock(return_value={"dealId": "D1", "dealStatus": "ACCEPTED", "level": 1.2})

    def test_broker_submits_stop_and_limit_in_same_order(self):
        result = self.broker.open_epic_position(epic="EURUSD.DEMO", direction="BUY", size=.1,
                                               allow_size_increment_retry=False, stop_level=1.19, limit_level=1.22, max_spread_bps=8)
        payload = self.broker._request.call_args.kwargs["payload"]
        self.assertEqual(payload["stopLevel"], 1.19)
        self.assertEqual(payload["limitLevel"], 1.22)
        self.assertEqual(result["attached_stop_level"], 1.19)

    def test_invalid_protection_and_minimum_distance_never_send(self):
        for stop, target in ((1.201, 1.22), (1.19999, 1.22), (None, 1.22), (float("nan"), 1.22)):
            with self.subTest(stop=stop):
                with self.assertRaises(IGDemoError):
                    self.broker.open_epic_position(epic="EURUSD.DEMO", direction="BUY", size=.1,
                                                  stop_level=stop, limit_level=target, max_spread_bps=8)
        self.broker._request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
