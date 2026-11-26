from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "backend"))
from ai_demo_execution import position_key
from category_strategy_engine import CATEGORY_MARKET_SEEDS
from ig_demo_broker import IGDemoBroker
from openai_analyst import OpenAIAnalyst, AnalystUnavailable
from openai_integration import OpenAIStrategyEngine, OpenAIExecutionEngine, OpenAIPrimeArchitecture
from provenance import ProvenanceRegistry
from trade_excursions import TradeExcursionTracker
from test_regime_execution import DemoBroker
from test_regime_strategy import range_candles

NOW = pd.Timestamp("2026-10-15T11:00:00Z")


class DiscretionaryBroker(DemoBroker):
    BASE_URL = IGDemoBroker.BASE_URL
    default_size = .1

    def status(self):
        return {"environment": "DEMO", "live_money_execution": False, "base_url": self.BASE_URL}

    def configured(self):
        return True

    def accounts(self):
        raise AssertionError("Discretionary entries must not use account-percent risk sizing")

    def open_epic_position(self, **kwargs):
        self.opened.append(kwargs)
        return {"dealId": "D"+str(len(self.opened)), "dealReference": kwargs["deal_reference"],
                "dealStatus": "ACCEPTED", "level": 1.2, "size": kwargs["size"]}

    def positions(self):
        return {"positions": [
            {"position": {"dealId": "D"+str(i), "dealReference": order["deal_reference"],
                          "direction": order["direction"], "size": order["size"], "level": 1.2},
             "market": {"epic": order["epic"], "bid": 1.21, "offer": 1.211, "marketStatus": "TRADEABLE"}}
            for i, order in enumerate(self.opened, 1) if "D"+str(i) not in self.closed]}


class DiscretionaryDemoTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-discretionary-test"})
        self.env.start(); self.addCleanup(self.env.stop)
        self.clock = patch("openai_integration.time.time", return_value=NOW.timestamp())
        self.clock.start(); self.addCleanup(self.clock.stop)
        self.broker = DiscretionaryBroker()
        self.close_keys = []
        def transport(payload):
            context = json.loads(payload["input"])
            decision = {"context_id": context["context_id"], "proposals": [{
                "symbol": "EURUSD", "direction": "BUY", "size": None, "stop_price": None,
                "target_price": None, "holding_minutes": None, "reason": "Observed continuation supports entry."}],
                "closes": [{"position_key": key, "reason": "Momentum faded; take the available profit."}
                           for key in self.close_keys], "wait_reason": "Other markets WAIT."}
            return {"id": "response-discretionary", "status": "completed", "output": [{"content": [
                {"type": "output_text", "text": json.dumps(decision)}]}]}
        self.transport = Mock(side_effect=transport)
        self.analyst = OpenAIAnalyst(state_path=self.directory.name+"/budget.json", transport=self.transport,
                                     clock=lambda: NOW.timestamp(), discretionary=True)
        self.engine = OpenAIStrategyEngine(broker=self.broker, frame_func=lambda _: range_candles(),
            state_path=self.directory.name+"/signals.json", analyst=self.analyst,
            now_func=lambda: NOW, discretionary=True)
        self.execution = OpenAIExecutionEngine(broker=self.broker, ranking_source=lambda: {},
            state_path=self.directory.name+"/positions.json", now_func=lambda: NOW, discretionary=True)
        self.execution.analysis_engine = self.engine
        self.engine.position_source = self.execution.discretionary_position_context
        self.seed = next(s for s in CATEGORY_MARKET_SEEDS if s["key"] == "EURUSD")

    def selected(self):
        row = self.engine._evaluate_seed(self.seed)
        self.assertTrue(row["standard_eligible"], row["rejection_reasons"])
        return row

    def test_entries_have_no_application_risk_caps_or_attached_order_preflight(self):
        row = self.selected()
        self.execution.max_open_positions = self.execution.max_per_category = 0
        self.execution.global_ig_max_positions = self.execution.max_daily_fx_entries = 0
        self.execution.max_theme_exposure = self.execution.max_daily_fx_entries_per_pair = 0
        row.update(ig_quote_timestamp=0, spread_pass=False, liquidity_pass=False, ig_spread_bps=100000,
                   news_guard={"required": True, "clear": False}, session_active=False)
        self.execution._open_candidate(row, [{"epic": row["ig_epic"]}])
        self.assertEqual(len(self.broker.opened), 1)
        order = self.broker.opened[0]
        self.assertFalse({"stop_level", "limit_level", "max_spread_bps"} & order.keys())
        self.assertEqual(order["size"], .1)
        tracked = self.execution.positions()[0]
        self.assertIsNone(tracked["due_at"])
        self.assertIsNone(tracked["protective_stop_price"])
        self.assertIsNone(self.execution.status()["risk_per_trade_pct"])

    def test_durable_decision_submission_prevents_repeat_after_restart(self):
        row = self.selected()
        self.execution._open_candidate(row, [])
        self.execution._open_candidate(row, [])
        restarted = OpenAIExecutionEngine(broker=self.broker, ranking_source=lambda: {},
            state_path=self.directory.name+"/positions.json", now_func=lambda: NOW, discretionary=True)
        restarted._open_candidate(row, [])
        self.assertEqual(len(self.broker.opened), 1)

    def test_distinct_model_decisions_can_add_same_symbol_without_position_or_daily_caps(self):
        row = self.selected()
        for i in range(5):
            self.execution._open_candidate({**row, "setup_id": "distinct-model-decision-"+str(i)}, [])
        self.assertEqual(len(self.broker.opened), 5)

    def test_live_environment_and_stale_model_decisions_still_cannot_submit(self):
        row = self.selected()
        with patch.object(self.broker, "status", return_value={"environment": "LIVE", "live_money_execution": True}):
            self.execution._open_candidate(row, [])
        self.execution._open_candidate({**row, "signal_timestamp": (NOW-pd.Timedelta(minutes=6)).isoformat()}, [])
        self.assertEqual(self.broker.opened, [])

    def test_model_receives_only_position_aliases_and_can_close_an_owned_position(self):
        self.execution._open_candidate(self.selected(), [])
        self.execution._reconcile()
        context = self.execution.discretionary_position_context()
        self.assertEqual(context[0]["position_key"], position_key("D1"))
        self.close_keys = [context[0]["position_key"]]
        self.engine._analysis_period = None
        self.selected()
        payload = self.transport.call_args.args[0]
        self.assertNotIn('"deal_id"', payload["input"])
        self.assertNotIn('"account_id"', payload["input"])
        self.execution._discretionary_closes()
        self.execution._discretionary_closes()
        self.assertEqual(self.broker.closed, ["D1"])
        self.assertEqual(self.execution.positions()[0]["status"], "CLOSED")
        self.assertEqual(self.execution.status()["closes"], 1)

    def test_unknown_close_alias_rejects_whole_model_batch(self):
        self.close_keys = ["invented-position"]
        with self.assertRaisesRegex(AnalystUnavailable, "UNKNOWN_OR_DUPLICATE_POSITION"):
            self.analyst.analyse([{"symbol": "EURUSD", "reference_price": 1.2}], as_of=NOW.isoformat(), positions=[])
        self.assertEqual(self.analyst.status()["latest_close_decisions"], [])

    def test_pending_close_acknowledgement_is_not_reported_as_closed(self):
        self.execution._open_candidate(self.selected(), [])
        self.close_keys = [position_key("D1")]
        self.engine._analysis_period = None
        self.selected()
        with patch.object(self.broker, "close_position", return_value={"status": "ACCEPTED", "closeVerified": False}):
            self.execution._discretionary_closes()
        self.assertEqual(self.execution.positions()[0]["status"], "OPEN")
        self.assertEqual(self.execution.status()["closes"], 0)

    def test_order_rejection_is_recorded_without_fabricating_a_position(self):
        row = self.selected()
        with patch.object(self.broker, "open_epic_position", side_effect=RuntimeError("IG_REJECTED_MARGIN")) as send:
            self.execution._open_candidate(row, [])
            self.execution._open_candidate(row, [])
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.execution.positions(), [])
        self.assertIn("IG_REJECTED_MARGIN", self.execution.status()["last_order_error"]["error"])

    def test_timed_out_open_is_recovered_by_broker_reference_without_resubmission(self):
        row = self.selected()
        def uncertain(**kwargs):
            self.broker.opened.append(kwargs)
            raise TimeoutError("confirmation timed out")
        with patch.object(self.broker, "open_epic_position", side_effect=uncertain):
            self.execution._open_candidate(row, [])
        self.assertEqual(self.execution.positions(), [])
        self.execution._reconcile()
        self.execution._reconcile()
        self.execution._open_candidate(row, [])
        self.assertEqual(len(self.broker.opened), 1)
        self.assertEqual(self.execution.status()["opens"], 1)
        self.assertEqual(self.execution.positions()[0]["deal_id"], "D1")

    def test_quote_provenance_is_diagnostic_and_not_a_discretionary_veto(self):
        row = self.selected()
        self.engine._state["evaluations"] = {row["symbol"]: row}
        prime = OpenAIPrimeArchitecture(intelligence=self.engine, broker=self.broker,
            provenance_registry=ProvenanceRegistry(default_analysis_source="SYNTHETIC_TEST"),
            legacy_evidence_source=None, state_dir=self.directory.name, discretionary=True)
        ranked = prime.category_rankings("FOREX")["FOREX"][0]
        self.assertTrue(ranked["execution_eligible"], ranked["rejection_reasons"])
        self.assertTrue(ranked["provenance"]["issues"])
        self.assertEqual(ranked["rejection_reasons"], [])

    def test_tracker_does_not_attach_orders_or_force_risk_or_profit_closes(self):
        tracker = TradeExcursionTracker(broker=self.broker, state_path=self.directory.name+"/tracker.json",
                                       discretionary=True)
        row = {"jasong_owned": True, "status": "OPEN", "risk_policy_version": "OLD", "current_r": -20,
               "mae_r": -20, "planned_risk_price_distance": .01, "planned_target_r": 2,
               "take_profit_target_price": 1.5, "protective_stop_price": 1.1}
        self.assertIsNone(tracker._server_exit_reason(row, NOW.timestamp()))
        self.assertFalse(tracker._native_order_needed(row, NOW.timestamp()))
        self.assertFalse(tracker.status()["server_r_watchdog_enabled"])

    def test_real_broker_order_path_accepts_no_quotes_and_no_attached_distances(self):
        broker = IGDemoBroker()
        broker.market_details = Mock(return_value={"instrument": {"type": "CURRENCIES",
            "currencies": [{"code": "USD", "isDefault": True}]}, "snapshot": {"marketStatus": "TRADEABLE"}})
        broker._min_deal_size = Mock(return_value=.1)
        broker._deal_size_increment = Mock(return_value=.1)
        broker._request = Mock(return_value={"dealReference": "REF"})
        broker.confirm = Mock(return_value={"dealId": "D1", "dealStatus": "ACCEPTED", "level": 1.2})
        broker.open_epic_position(epic="EURUSD.DEMO", direction="BUY", size=.1)
        payload = broker._request.call_args.kwargs["payload"]
        self.assertNotIn("stopLevel", payload)
        self.assertNotIn("limitLevel", payload)
        self.assertEqual(payload["orderType"], "MARKET")


if __name__ == "__main__":
    unittest.main()
