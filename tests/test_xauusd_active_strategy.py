from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import pandas as pd


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from category_execution_engine import (  # noqa: E402
    CategoryExecutionEngine,
    RiskSizingError,
)
from risk_exit_policy import build_risk_plan  # noqa: E402
from forex_liquidity_lines_strategy import STRATEGY_ID as FX_STRATEGY_ID  # noqa: E402
from xauusd_liquidity_strategy import (  # noqa: E402
    STRATEGY_ID,
    _direction_setup,
    _resample_completed,
    analyze_xauusd,
    session_context,
)


def base_frame(rows=800, end="2026-01-15 02:30:00+00:00"):
    index = pd.date_range(end=end, periods=rows, freq="15min")
    close = pd.Series(
        [2000.0 + i * 0.04 + (0.45 if i % 17 == 0 else 0.0) for i in range(rows)],
        index=index,
    )
    open_ = close.shift(1).fillna(close.iloc[0] - 0.05)
    return pd.DataFrame(
        {
            "Open": open_,
            "High": pd.concat([open_, close], axis=1).max(axis=1) + 0.30,
            "Low": pd.concat([open_, close], axis=1).min(axis=1) - 0.30,
            "Close": close,
            "Volume": 1000.0,
        },
        index=index,
    )


class SessionAndStructureTests(unittest.TestCase):
    def test_london_and_new_york_windows_follow_dst_and_sast(self):
        winter_london = session_context("2026-01-15 08:30:00+00:00")
        self.assertTrue(winter_london["london_active"])
        self.assertFalse(winter_london["new_york_active"])
        self.assertIn("10:30:00+02:00", winter_london["south_africa_local"])

        summer_london = session_context("2026-07-15 07:30:00+00:00")
        self.assertTrue(summer_london["london_active"])
        self.assertFalse(summer_london["new_york_active"])
        self.assertIn("09:30:00+02:00", summer_london["south_africa_local"])

        winter_overlap = session_context("2026-01-15 13:30:00+00:00")
        summer_overlap = session_context("2026-07-15 12:30:00+00:00")
        self.assertTrue(winter_overlap["overlap"])
        self.assertTrue(summer_overlap["overlap"])
        self.assertIn("15:30:00+02:00", winter_overlap["south_africa_local"])
        self.assertIn("14:30:00+02:00", summer_overlap["south_africa_local"])

    def test_weekend_and_outside_session_are_blocked(self):
        self.assertFalse(session_context("2026-01-17 13:30:00+00:00")["active"])
        self.assertFalse(session_context("2026-01-15 03:00:00+00:00")["active"])

    def test_incomplete_h4_aggregate_is_ignored(self):
        frame = base_frame(rows=18, end="2026-01-01 04:15:00+00:00")
        result = _resample_completed(frame, "4h", 16)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.index[-1].isoformat(), "2026-01-01T04:00:00+00:00")

    def test_full_analyser_waits_outside_execution_session(self):
        result = analyze_xauusd(base_frame())
        self.assertEqual(result["direction"], "WAIT")
        self.assertFalse(result["session"]["active"])
        self.assertIn("SESSION_ACTIVE", result["rejection_reasons"])

    def test_all_eight_checks_are_required_and_setup_id_is_event_stable(self):
        frame = base_frame(rows=30, end="2026-01-15 13:15:00+00:00")
        frame.loc[:, "Close"] = 100.0
        frame.loc[:, "Open"] = 99.8
        frame.loc[:, "High"] = 100.4
        frame.loc[:, "Low"] = 99.6
        atr = pd.Series(1.0, index=frame.index)
        sweep = {
            "direction": "SELL_SIDE_SWEEP",
            "source": "ROLLING_SELL_SIDE_LIQUIDITY",
            "level": 99.0,
            "extreme": 98.5,
            "position": 20,
            "timestamp": frame.index[20],
        }
        structure = {
            "type": "BOS",
            "level": 101.0,
            "position": 23,
            "timestamp": frame.index[23],
        }
        zone = {
            "kind": "ORDER_BLOCK",
            "low": 99.0,
            "high": 100.0,
            "origin_position": 22,
            "origin_timestamp": frame.index[22],
            "retest_position": 29,
            "retest_timestamp": frame.index[29],
        }
        h4 = {
            "trend": "BULLISH",
            "equilibrium": 100.2,
            "recent_swing_high": 105.0,
            "recent_swing_low": 95.0,
        }
        h1 = {
            "trend": "BULLISH",
            "recent_swing_high": 104.0,
            "recent_swing_low": 96.0,
        }
        previous_day = {"high": 104.5, "low": 96.0}
        session = {"active": True, "overlap": True}

        confirmation_one = {"passed": True, "timestamp": frame.index[-1]}
        confirmation_two = {"passed": True, "timestamp": frame.index[-2]}
        with (
            patch("xauusd_liquidity_strategy._find_sweep", return_value=sweep),
            patch("xauusd_liquidity_strategy._find_structure_break", return_value=structure),
            patch("xauusd_liquidity_strategy._find_retest_zone", return_value=zone),
            patch("xauusd_liquidity_strategy._confirmation", return_value=confirmation_one),
        ):
            qualified = _direction_setup(
                frame,
                "BUY",
                h4=h4,
                h1=h1,
                session=session,
                previous_day=previous_day,
                atr=atr,
            )
        self.assertEqual(qualified["direction"], "BUY")
        self.assertTrue(all(qualified["checks"].values()))
        self.assertGreaterEqual(qualified["room_to_opposing_liquidity_r"], 2.0)

        with (
            patch("xauusd_liquidity_strategy._find_sweep", return_value=sweep),
            patch("xauusd_liquidity_strategy._find_structure_break", return_value=structure),
            patch("xauusd_liquidity_strategy._find_retest_zone", return_value=zone),
            patch("xauusd_liquidity_strategy._confirmation", return_value=confirmation_two),
        ):
            later_confirmation = _direction_setup(
                frame,
                "BUY",
                h4=h4,
                h1=h1,
                session=session,
                previous_day=previous_day,
                atr=atr,
            )
        self.assertEqual(qualified["setup_id"], later_confirmation["setup_id"])

        with (
            patch("xauusd_liquidity_strategy._find_sweep", return_value=sweep),
            patch("xauusd_liquidity_strategy._find_structure_break", return_value=structure),
            patch("xauusd_liquidity_strategy._find_retest_zone", return_value=zone),
            patch("xauusd_liquidity_strategy._confirmation", return_value=confirmation_one),
        ):
            outside = _direction_setup(
                frame,
                "BUY",
                h4=h4,
                h1=h1,
                session={"active": False, "overlap": False},
                previous_day=previous_day,
                atr=atr,
            )
        self.assertEqual(outside["direction"], "WAIT")
        self.assertFalse(outside["checks"]["session_active"])


class FakeRiskBroker:
    def __init__(self, balance=10000.0):
        self.balance = balance
        self.open_calls = []
        self.close_calls = []
        self._positions = []

    def configured(self):
        return True

    def status(self):
        return {"account_id": "DEMO-1"}

    def accounts(self):
        return {
            "accounts": [
                {
                    "accountId": "DEMO-1",
                    "currency": "ZAR",
                    "balance": {"balance": self.balance, "available": self.balance},
                }
            ]
        }

    @staticmethod
    def _min_deal_size(_details):
        return 0.1

    @staticmethod
    def _deal_size_increment(_details):
        return 0.1

    def market_details(self, _epic, require_quote=False):
        return {
            "instrument": {
                "currencies": [{"code": "ZAR", "isDefault": True}],
            },
            "snapshot": {"bid": 1999.5, "offer": 2000.0},
            "dealingRules": {
                "minDealSize": {"value": 0.1},
                "sizeIncrement": {"value": 0.1},
            },
        }

    def estimate_closed_position_pnl(self, **kwargs):
        move = abs(float(kwargs["entry_level"]) - float(kwargs["exit_level"]))
        return {"account_pnl": -move * 20.0 * float(kwargs["size"])}

    def positions(self):
        return {"positions": list(self._positions)}

    def open_epic_position(self, **kwargs):
        self.open_calls.append(dict(kwargs))
        deal_id = f"D{len(self.open_calls)}"
        return {
            "dealId": deal_id,
            "dealReference": kwargs.get("deal_reference"),
            "size": kwargs.get("size"),
            "level": 2000.0,
            "dealStatus": "ACCEPTED",
        }

    def close_position(self, deal_id):
        self.close_calls.append(deal_id)
        return {"status": "ACCEPTED", "closeVerified": True}


def active_candidate(setup_id="setup-1"):
    return {
        "category": "METALS",
        "category_rank": 1,
        "strategy_id": STRATEGY_ID,
        "strategy_name": "XAUUSD London-New York Liquidity / Structure",
        "symbol": "GOLD",
        "market": "Gold",
        "direction": "BUY",
        "ig_epic": "CS.D.CFAGOLD.CFD.IP",
        "ig_bid": 1999.5,
        "ig_offer": 2000.0,
        "standard_eligible": True,
        "session_active": True,
        "session_name": "LONDON_NEW_YORK_OVERLAP",
        "session": {"active": True, "overlap": True},
        "session_exit_at": time.time() + 3600,
        "setup_id": setup_id,
        "structural_stop_distance": 10.0,
        "target_r": 2.0,
        "holding_bars": 48,
        "max_hold_seconds": 12 * 3600,
        "exposure_tags": ["GOLD", "USD", "METALS"],
    }


def active_fx_candidate(setup_id="fx-setup-1"):
    return {
        **active_candidate(setup_id),
        "category": "FOREX",
        "strategy_id": FX_STRATEGY_ID,
        "strategy_name": "Forex Multi-Session Liquidity / Lines",
        "symbol": "EURUSD",
        "market": "EUR/USD",
        "ig_epic": "CS.D.EURUSD.CFD.IP",
        "session_name": "LONDON_NEW_YORK_OVERLAP",
        "exposure_tags": ["EUR", "USD", "FX_MAJOR"],
    }


class RiskSizedExecutionTests(unittest.TestCase):
    def _engine(self, broker):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return CategoryExecutionEngine(
            broker=broker,
            ranking_source=lambda: {"METALS": [active_candidate()]},
            external_positions_source=lambda: [],
            state_path=os.path.join(tmp.name, "portfolio.json"),
            poll_seconds=999,
        )

    def test_structural_plan_is_exactly_two_r_or_better(self):
        plan = build_risk_plan(
            active_candidate(),
            entry_price=2000.0,
            direction="BUY",
        )
        self.assertEqual(plan.stop_distance, 10.0)
        self.assertEqual(plan.protective_stop_price, 1990.0)
        self.assertEqual(plan.target_r, 2.0)
        self.assertEqual(plan.take_profit_target_price, 2020.0)

    def test_order_size_is_floored_to_one_percent_risk(self):
        engine = self._engine(FakeRiskBroker(balance=10000.0))
        sizing = engine._risk_sized_order(active_candidate())
        self.assertEqual(sizing["risk_cash"], 100.0)
        self.assertEqual(sizing["size"], 0.5)
        self.assertLessEqual(sizing["estimated_stop_risk_cash"], 100.0)

    def test_minimum_size_that_exceeds_risk_budget_is_rejected(self):
        engine = self._engine(FakeRiskBroker(balance=1000.0))
        with self.assertRaises(RiskSizingError):
            engine._risk_sized_order(active_candidate())

    def test_only_active_fx_and_gold_strategies_can_open(self):
        broker = FakeRiskBroker()
        engine = self._engine(broker)
        retired = {
            **active_candidate(),
            "strategy_id": "FX_CURRENT_CANDLE_REGIME_V3",
            "symbol": "EURUSD",
        }
        allowed, reason = engine._may_open(retired, [])
        self.assertFalse(allowed)
        self.assertIn("retired", reason)

        lookalike = {
            **active_fx_candidate(),
            "strategy_id": "FX_LIQUIDITY_LINES_LEGACY",
        }
        allowed, reason = engine._may_open(lookalike, [])
        self.assertFalse(allowed)
        self.assertIn("retired", reason)

        allowed, reason = engine._may_open(active_candidate(), [])
        self.assertTrue(allowed, reason)

        allowed, reason = engine._may_open(active_fx_candidate(), [])
        self.assertTrue(allowed, reason)

    def test_forex_uses_same_structural_two_r_policy(self):
        plan = build_risk_plan(
            active_fx_candidate(),
            entry_price=2000.0,
            direction="BUY",
        )
        self.assertEqual(plan.stop_distance, 10.0)
        self.assertEqual(plan.target_r, 2.0)
        self.assertEqual(plan.take_profit_target_price, 2020.0)

    def test_account_wide_gold_position_and_sast_daily_cap_block_entry(self):
        broker = FakeRiskBroker()
        engine = self._engine(broker)
        broker._positions = [
            {
                "position": {
                    "dealId": "EXISTING-GOLD",
                    "direction": "BUY",
                    "size": 0.1,
                    "level": 2000.0,
                },
                "market": {
                    "epic": "CS.D.CFAGOLD.CFD.IP",
                    "instrumentName": "Spot Gold",
                },
            }
        ]
        allowed, reason = engine._may_open(active_candidate(), [])
        self.assertFalse(allowed)
        self.assertIn("account-wide GOLD", reason)

        broker._positions = []
        engine._state["positions"] = [
            {
                "strategy_id": STRATEGY_ID,
                "category": "METALS",
                "setup_id": f"old-{index}",
                "opened_at": time.time(),
                "status": "CLOSED",
            }
            for index in range(2)
        ]
        allowed, reason = engine._may_open(active_candidate("setup-new"), [])
        self.assertFalse(allowed)
        self.assertIn("daily METALS entry cap", reason)

    def test_risk_sized_open_disables_broker_upward_retry(self):
        broker = FakeRiskBroker()
        engine = self._engine(broker)
        engine._open_candidate(active_candidate(), [])
        self.assertEqual(len(broker.open_calls), 1)
        self.assertEqual(broker.open_calls[0]["size"], 0.5)
        self.assertFalse(broker.open_calls[0]["allow_size_increment_retry"])
        position = engine.positions()[0]
        self.assertEqual(position["strategy_id"], STRATEGY_ID)
        self.assertEqual(position["planned_target_r"], 2.0)
        self.assertFalse(position["live_money_execution"])


if __name__ == "__main__":
    unittest.main()
