from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "backend"))
from ai_demo_execution import position_key
from category_strategy_engine import CATEGORY_MARKET_SEEDS
from demo_entry_trial import LIVE_TRIAL_ID
from ig_demo_broker import IGDemoBroker, IGDemoError, IGDemoOrderRejected
from openai_analyst import OpenAIAnalyst, AnalystUnavailable
from openai_integration import OpenAIStrategyEngine, OpenAIExecutionEngine, OpenAIPrimeArchitecture
from openai_policy import GUARDED_DEMO_POLICY
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
        raise AssertionError("Guarded entries must not use account-percent risk sizing")

    def entry_quote(self, epic):
        return {"source": "IG_DEMO_MARKET_DETAILS", "epic": epic, "bid": self.quote_price,
                "offer": self.quote_price + .0001, "observed_at": NOW.timestamp(),
                "update_timestamp_utc": NOW.timestamp()}

    def open_epic_position(self, **kwargs):
        quote = self.entry_quote(kwargs["epic"])
        kwargs["entry_guard"](quote)
        self.opened.append(kwargs)
        return {"dealId": "D"+str(len(self.opened)), "dealReference": kwargs["deal_reference"],
                "dealStatus": "ACCEPTED", "level": 1.2, "size": kwargs["size"], "entry_quote": quote}

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
        self.position_decisions = []
        self.trial_entry = {"action": "ENTER_NOW", "confirmation": {"rule": "CLOSE_ABOVE", "price": .9}}
        self.proposal_symbol = "EURUSD"
        def transport(payload):
            context = json.loads(payload["input"])
            ref = next(market["reference_price"] for market in context["markets"] if market["symbol"] == "EURUSD")
            decision = {"context_id": context["context_id"], "proposals": [{
                "symbol": self.proposal_symbol, "direction": "BUY", "size": None,
                "stop_price": ref - .01 if self.trial_entry else None,
                "target_price": ref + .01 if self.trial_entry else None,
                "holding_minutes": None, "trial_entry": self.trial_entry,
                "reason": "Observed continuation supports entry."}],
                "position_decisions": self.position_decisions, "wait_reason": "Other markets WAIT."}
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
        self.broker.quote_price = row["live_price"]
        return row

    @staticmethod
    def position_decision(action="CLOSE", reason="Close because the price failed to hold support.", deal="D1"):
        return {"position_key": position_key(deal), "action": action, "reason": reason}

    def test_guarded_entry_uses_model_levels_without_attaching_broker_orders(self):
        row = self.selected()
        self.execution.max_open_positions = self.execution.max_per_category = 0
        self.execution.global_ig_max_positions = self.execution.max_daily_fx_entries = 0
        self.execution.max_theme_exposure = self.execution.max_daily_fx_entries_per_pair = 0
        row.update(ig_quote_timestamp=0, spread_pass=False, liquidity_pass=False, ig_spread_bps=100000,
                   news_guard={"required": True, "clear": False}, session_active=False)
        self.execution._open_candidate(row, [])
        self.assertEqual(len(self.broker.opened), 1)
        order = self.broker.opened[0]
        self.assertFalse({"stop_level", "limit_level", "max_spread_bps"} & order.keys())
        self.assertEqual(order["size"], .1)
        tracked = self.execution.positions()[0]
        self.assertIsNone(tracked["due_at"])
        self.assertIsNone(tracked["protective_stop_price"])
        self.assertIsNone(self.execution.status()["risk_per_trade_pct"])
        self.assertEqual(tracked["execution_policy"], GUARDED_DEMO_POLICY)
        self.assertTrue(tracked["entry_trial"]["admitted"])

    def test_durable_decision_submission_prevents_repeat_after_restart(self):
        row = self.selected()
        self.execution._open_candidate(row, [])
        self.execution._open_candidate(row, [])
        restarted = OpenAIExecutionEngine(broker=self.broker, ranking_source=lambda: {},
            state_path=self.directory.name+"/positions.json", now_func=lambda: NOW, discretionary=True)
        restarted._open_candidate(row, [])
        self.assertEqual(len(self.broker.opened), 1)

    def test_guarded_trial_submits_only_one_entry_and_serves_metrics_without_execution_lock(self):
        row = self.selected()
        self.execution._open_candidate(row, [])
        self.execution._open_candidate({**row, "setup_id": "second-setup"}, [])
        self.assertEqual(self.transport.call_count, 1)
        self.assertEqual(len(self.broker.opened), 1)
        status = self.execution.entry_trial_status()
        self.assertEqual(status["trial_id"], LIVE_TRIAL_ID)
        self.assertEqual(status["recent_entry_checks"], {"ENTER_NOW_CONFIRMED": 1, "MARKET_ALREADY_OPEN": 1})
        self.assertEqual(status["live_demo_trial"]["entries"], 1)
        self.assertEqual(status["shadow_comparison"]["current_strategy"]["entries"], 0)
        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.execution._lock:
                report = executor.submit(self.execution.entry_trial_status)
                self.assertEqual(report.result(timeout=1), status)

    def test_live_environment_and_stale_model_decisions_still_cannot_submit(self):
        row = self.selected()
        with patch.object(self.broker, "status", return_value={"environment": "LIVE", "live_money_execution": True}):
            self.execution._open_candidate(row, [])
        self.execution._open_candidate({**row, "signal_timestamp": (NOW-pd.Timedelta(minutes=6)).isoformat()}, [])
        self.assertEqual(self.broker.opened, [])

    def test_wait_bad_candles_stale_quote_and_unavailable_exposure_cannot_submit(self):
        row = self.selected()
        for candidate, reason in (
            ({**row, "trial_entry": {"action": "WAIT"}}, "MODEL_WAIT"),
            ({**row, "trial_m15_closes": []}, "COMPLETED_M15_TRIGGER_REQUIRED"),
            ({**row, "version": "6.16-ai-discretionary-demo-v4"}, "ENTRY_VERSION_MISMATCH"),
        ):
            self.assertFalse(self.execution._open_candidate(candidate, []))
            self.assertEqual(self.execution._state["last_guarded_entry_check"]["reason"], reason)
        stale = {**self.broker.entry_quote(row["ig_epic"]), "update_timestamp_utc": NOW.timestamp() - 181}
        with patch.object(self.broker, "entry_quote", return_value=stale):
            self.assertFalse(self.execution._open_candidate(row, []))
            self.assertEqual(self.execution._state["last_guarded_entry_check"]["reason"], "FRESH_IG_QUOTE_REQUIRED")
        with patch.object(self.broker, "positions", return_value={}):
            self.assertFalse(self.execution._open_candidate(row, []))
            self.assertEqual(self.execution._state["last_guarded_entry_check"]["reason"], "BROKER_EXPOSURE_UNAVAILABLE")
        self.assertEqual(self.broker.opened, [])
        self.assertEqual(self.execution._state.get("ai_decision_submissions", {}), {})

    def test_broker_exposure_from_before_cutover_blocks_new_entries(self):
        row = self.selected()
        existing = {"positions": [{"position": {"dealId": "LEGACY", "dealReference": "EXTERNAL",
                                                "direction": "SELL"},
                                   "market": {"epic": row["ig_epic"], "instrumentName": "EUR/USD"}}]}
        with patch.object(self.broker, "positions", return_value=existing):
            self.assertFalse(self.execution._open_candidate(row, []))
        self.assertEqual(self.execution.entry_trial_status()["recent_entry_checks"], {"MARKET_ALREADY_OPEN": 1})
        self.assertEqual(self.broker.opened, [])

    def test_invalid_entry_symbol_leaves_valid_position_management_available(self):
        self.proposal_symbol = "UNKNOWN"
        self.position_decisions = [self.position_decision()]
        analysis = self.analyst.analyse([{"symbol": "EURUSD", "reference_price": 1.2}], as_of=NOW.isoformat(),
                                        positions=[{"position_key": position_key("D1")}])
        self.assertEqual(analysis["proposals"], [])
        self.assertEqual(analysis["position_decisions"], self.position_decisions)
        self.assertEqual(self.analyst.status()["last_entry_validation_error"], "OPENAI_UNKNOWN_OR_DUPLICATE_SYMBOL")

    def test_model_receives_only_position_aliases_and_can_close_an_owned_position(self):
        self.execution._open_candidate(self.selected(), [])
        self.execution._reconcile()
        context = self.execution.discretionary_position_context()
        self.assertEqual(context[0]["position_key"], position_key("D1"))
        self.assertEqual(context[0]["indicative_exit_price"], context[0]["current_bid"])
        self.position_decisions = [self.position_decision()]
        self.engine._analysis_period = None
        self.selected()
        payload = self.transport.call_args.args[0]
        self.assertNotIn('"deal_id"', payload["input"])
        self.assertNotIn('"account_id"', payload["input"])
        self.execution._discretionary_closes()
        self.execution._discretionary_closes()
        self.assertEqual(self.broker.closed, ["D1"])
        self.assertEqual(self.execution.positions()[0]["status"], "CLOSED")
        self.assertEqual(self.execution.positions()[0]["model_close_action"], "CLOSE")
        self.assertEqual(self.execution.status()["closes"], 1)

    def test_unknown_or_duplicate_alias_rejects_whole_model_batch(self):
        markets = [{"symbol": "EURUSD", "reference_price": 1.2}]
        positions = [{"position_key": position_key("D1")}]
        duplicate = [self.position_decision("HOLD"), self.position_decision("CLOSE")]
        for decisions in ([self.position_decision(deal="unknown")], duplicate):
            with self.subTest(decisions=decisions):
                self.position_decisions = decisions
                with self.assertRaisesRegex(AnalystUnavailable, "UNKNOWN_OR_DUPLICATE_POSITION"):
                    self.analyst.analyse(markets, as_of=NOW.isoformat(), positions=positions)
        self.assertEqual(self.analyst.status()["latest_close_decisions"], [])

    def test_usdjpy_short_hold_reason_cannot_authorize_close(self):
        self.execution._open_candidate(self.selected(), [])
        position = self.execution._state["positions"][0]
        position.update(symbol="USDJPY", direction="SELL", entry_level=150.25,
                        broker={"bid": 150.16, "offer": 150.31})
        hold_reason = "Hold the USDJPY short. H1 and H4 still support the short thesis."
        self.position_decisions = [self.position_decision(reason=hold_reason)]
        self.engine._analysis_period = None
        self.selected()
        context = json.loads(self.transport.call_args.args[0]["input"])["open_positions"][0]
        self.assertEqual(context["indicative_exit_price"], 150.31)
        decisions = self.analyst.status()
        self.assertEqual(decisions["latest_close_decisions"], [])
        self.assertEqual(decisions["latest_position_decisions"], [{
            **self.position_decision("HOLD", hold_reason), "rejected_action": "CLOSE",
            "validation_error": "OPENAI_CLOSE_REASON_CONFLICT",
        }])
        self.execution._discretionary_closes()
        self.assertEqual(self.broker.closed, [])
        self.assertEqual(self.execution.positions()[0]["status"], "OPEN")

    def test_only_consistent_explicit_close_reaches_broker_even_if_snapshot_is_invalid(self):
        self.execution._open_candidate(self.selected(), [])
        for payload in (
            {"closes": [{"position_key": position_key("D1"), "reason": "Keep the short open."}]},
            {"position_decisions": [self.position_decision("HOLD", "Higher timeframe supports holding.")]},
            {"position_decisions": [self.position_decision(reason="Close because price is choppy. Keep it open.")]},
            {"position_decisions": [self.position_decision(reason="Price is near a target.")]},
        ):
            with self.subTest(payload=payload):
                snapshot = {**payload, "context_id": "context", "created_at": NOW.timestamp()}
                with patch.object(self.engine, "decision_snapshot", return_value=snapshot):
                    self.execution._discretionary_closes()
                self.assertEqual(self.broker.closed, [])
                self.assertEqual(self.execution.status()["closes"], 0)
                self.assertFalse(any(key.startswith("CLOSE:") for key in self.execution._state["ai_decision_submissions"]))

    def test_pending_close_acknowledgement_is_not_reported_as_closed(self):
        self.execution._open_candidate(self.selected(), [])
        self.position_decisions = [self.position_decision()]
        self.engine._analysis_period = None
        self.selected()
        with patch.object(self.broker, "close_position", return_value={"status": "ACCEPTED", "closeVerified": False}):
            self.execution._discretionary_closes()
        self.assertEqual(self.execution.positions()[0]["status"], "OPEN")
        self.assertEqual(self.execution.status()["closes"], 0)

    def test_order_rejection_is_recorded_without_fabricating_a_position(self):
        row = self.selected()
        def reject(**kwargs):
            kwargs["entry_guard"](self.broker.entry_quote(kwargs["epic"]))
            raise IGDemoOrderRejected("IG_REJECTED_MARGIN")
        with patch.object(self.broker, "open_epic_position", side_effect=reject) as send:
            self.execution._open_candidate(row, [])
            self.execution._open_candidate(row, [])
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.execution.positions(), [])
        self.assertIn("IG_REJECTED_MARGIN", self.execution.status()["last_order_error"]["error"])

    def test_timed_out_open_is_recovered_by_broker_reference_without_resubmission(self):
        row = self.selected()
        open_position = self.broker.open_epic_position
        def uncertain(**kwargs):
            open_position(**kwargs)
            raise TimeoutError("confirmation timed out")
        with patch.object(self.broker, "open_epic_position", side_effect=uncertain):
            self.execution._open_candidate(row, [])
        self.assertEqual(self.execution.positions(), [])
        with patch.object(self.broker, "positions", return_value={"positions": []}):
            self.assertFalse(self.execution._open_candidate({**row, "setup_id": "while-unconfirmed"}, []))
        self.execution._reconcile()
        self.execution._reconcile()
        self.execution._open_candidate(row, [])
        self.assertEqual(len(self.broker.opened), 1)
        self.assertEqual(self.execution.status()["opens"], 1)
        self.assertEqual(self.execution.positions()[0]["deal_id"], "D1")
        self.assertEqual(self.execution.positions()[0]["execution_policy"], GUARDED_DEMO_POLICY)
        self.assertTrue(self.execution.positions()[0]["entry_trial"]["admitted"])

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

    def test_guarded_broker_requires_preflight_at_submission_and_preserves_close_path(self):
        broker = IGDemoBroker(guarded_entries_only=True)
        broker.market_details = Mock(return_value={"instrument": {"type": "CURRENCIES",
            "currencies": [{"code": "USD", "isDefault": True}]}, "snapshot": {
                "marketStatus": "TRADEABLE", "updateTimestampUTC": (NOW.timestamp() - 2) * 1000,
                "priceLadder": [{"bid": "1.2000", "ask": "1.2001"}], "delayTime": 0,
            }})
        broker._min_deal_size = Mock(return_value=.1)
        broker._deal_size_increment = Mock(return_value=.1)
        broker._session = SimpleNamespace(last_used_at=None)
        broker._raw_request = Mock(return_value=({"dealReference": "REF"}, {}))
        broker.confirm = Mock(return_value={"dealId": "D1", "dealStatus": "ACCEPTED", "level": 1.2})
        with self.assertRaisesRegex(IGDemoError, "GUARDED_ENTRY_REQUIRED"):
            broker.open_epic_position(epic="EURUSD.DEMO", direction="BUY", size=.1)
        with self.assertRaisesRegex(IGDemoError, "GUARDED_ENTRY_REQUIRED"):
            broker._request("POST", "/positions/otc", payload={})
        broker._raw_request.assert_not_called()
        rejected = Mock(side_effect=IGDemoError("STALE_QUOTE"))
        with self.assertRaisesRegex(IGDemoError, "STALE_QUOTE"):
            broker.open_epic_position(epic="EURUSD.DEMO", direction="BUY", size=.1, entry_guard=rejected)
        broker._raw_request.assert_not_called()
        guard = Mock(side_effect=lambda _: broker._raw_request.assert_not_called())
        result = broker.open_epic_position(epic="EURUSD.DEMO", direction="BUY", size=.1, entry_guard=guard)
        guard.assert_called_once()
        payload = broker._raw_request.call_args.kwargs["payload"]
        self.assertNotIn("stopLevel", payload)
        self.assertNotIn("limitLevel", payload)
        self.assertEqual(payload["orderType"], "MARKET")
        self.assertEqual(result["entry_quote"]["bid"], 1.2)
        self.assertEqual(result["entry_quote"]["offer"], 1.2001)
        self.assertEqual(result["entry_quote"]["update_timestamp_utc"], NOW.timestamp() - 2)
        broker._raw_request.reset_mock()
        broker.confirm.return_value = {"dealStatus": "REJECTED", "reason": "SIZE_INCREMENT"}
        with self.assertRaises(IGDemoOrderRejected):
            broker.open_epic_position(epic="EURUSD.DEMO", direction="BUY", size=.1, entry_guard=Mock())
        broker._raw_request.assert_called_once()
        broker._request("POST", "/positions/otc", extra_headers={"_method": "DELETE"})
        self.assertEqual(broker._raw_request.call_args.kwargs["extra_headers"], {"_method": "DELETE"})

    def test_real_broker_close_records_only_matching_cash_confirmation(self):
        broker = IGDemoBroker()
        position = {"position": {"direction": "SELL", "size": .6}, "market": {"marketStatus": "TRADEABLE"}}
        broker._request = Mock(return_value={"dealReference": "CLOSE-REF"})
        for fields, expected in (
            ({"profit": -2.34, "profitCurrency": "USD"}, -2.34),
            ({"profit": 0, "profitCurrency": "JPY"}, 0),
            ({"profit": -2.34, "profitCurrency": "USD", "dealReference": "UNRELATED"}, None),
            ({"profit": -2.34}, None),
            ({"profit": float("nan"), "profitCurrency": "USD"}, None),
        ):
            with self.subTest(fields=fields):
                broker._find_open_position = Mock(side_effect=[position, None])
                broker.confirm = Mock(return_value={"dealReference": "CLOSE-REF", "dealStatus": "ACCEPTED", **fields})
                with patch("ig_demo_broker.time.sleep"):
                    close = broker.close_position("D1")
                self.assertTrue(close["closeVerified"])
                self.assertEqual(close.get("profit"), expected)
                self.assertEqual(close.get("profitCurrency"), fields["profitCurrency"] if expected is not None else None)


if __name__ == "__main__":
    unittest.main()
