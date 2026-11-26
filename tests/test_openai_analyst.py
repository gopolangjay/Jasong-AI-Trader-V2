from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from io import BytesIO
from urllib.error import HTTPError
from unittest.mock import Mock, patch

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "backend"))
from openai_analyst import OpenAIAnalyst, AnalystUnavailable
from openai_integration import OpenAIStrategyEngine, OpenAIExecutionEngine, OpenAIPrimeArchitecture, execution_reasons
from openai_policy import STRATEGY_ID
from category_strategy_engine import CATEGORY_MARKET_SEEDS
from regime_strategy import analyze_regime
from test_regime_strategy import range_candles
from test_regime_execution import DemoBroker

NOW = pd.Timestamp("2026-10-15 11:00Z")


def proposal(symbol="EURUSD", direction="BUY"):
    return {"symbol": symbol, "direction": direction, "stop_price": .98 if direction == "BUY" else 1.02,
            "target_price": 1.01 if direction == "BUY" else .99, "holding_minutes": 30,
            "reason": "Observed price action supports this DEMO entry; the stop invalidates it."}


def response_for(payload, proposals=None):
    context = json.loads(payload["input"])
    decision = {"context_id": context["context_id"], "proposals": proposals or [], "wait_reason": "Other symbols WAIT."}
    return {"id": "resp_test", "status": "completed", "usage": {"input_tokens": 100, "output_tokens": 50},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(decision)}]}]}


class AnalystTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-unit-test-value", "JASONG_OPENAI_ANALYST_MODEL": "gpt-5.4-mini"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.now = NOW.timestamp()
        self.transport = Mock(side_effect=lambda p: response_for(p, [proposal()]))
        self.analyst = OpenAIAnalyst(state_path=self.directory.name+"/budget.json", transport=self.transport, clock=lambda: self.now)
        self.market = {"symbol": "EURUSD", "reference_price": 1.0, "candles": {"M15": []}}

    def test_structured_bounded_request_uses_no_broker_or_account_data(self):
        result = self.analyst.analyse([self.market], as_of=NOW.isoformat())
        payload = self.transport.call_args.args[0]
        self.assertFalse(payload["store"])
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertEqual(payload["max_output_tokens"], 4000)
        self.assertNotIn("tools", payload)
        self.assertNotIn("synthetic-unit-test-value", json.dumps(payload))
        self.assertNotIn("account_id", payload["input"])
        self.assertEqual(result["proposals"][0]["direction"], "BUY")
        self.assertEqual(self.analyst.status()["request_budget"]["requests"], 1)

    def test_wait_is_a_valid_decision_and_never_forces_an_entry(self):
        self.transport.side_effect = lambda p: response_for(p)
        result = self.analyst.analyse([self.market], as_of=NOW.isoformat())
        self.assertEqual(result["proposals"], [])
        self.assertIsNone(self.analyst.status()["last_error"])

    def test_invalid_context_symbols_duplicates_geometry_and_schema_fail_closed(self):
        def mutate(payload, kind):
            response = response_for(payload, [proposal()])
            decision = json.loads(response["output"][0]["content"][0]["text"])
            if kind == "context": decision["context_id"] = "wrong"
            elif kind == "symbol": decision["proposals"][0]["symbol"] = "UNKNOWN"
            elif kind == "duplicate": decision["proposals"] *= 2
            elif kind == "stop": decision["proposals"][0]["stop_price"] = 1.02
            elif kind == "target": decision["proposals"][0]["target_price"] = .99
            elif kind == "size": decision["proposals"][0]["size"] = 100
            elif kind == "nan": decision["proposals"][0]["target_price"] = float("nan")
            elif kind == "hold": decision["proposals"][0]["holding_minutes"] = 99999
            response["output"][0]["content"][0]["text"] = json.dumps(decision)
            return response
        for kind in ("context", "symbol", "duplicate", "stop", "target", "size", "nan", "hold"):
            with self.subTest(kind=kind):
                self.transport.side_effect = lambda p: mutate(p, kind)
                with self.assertRaises(AnalystUnavailable):
                    self.analyst.analyse([self.market], as_of=NOW.isoformat())
                self.assertEqual(self.analyst.status()["latest_proposals"], [])

    def test_refusal_incomplete_and_transport_failure_are_not_reused_as_signals(self):
        for response in ({"status": "incomplete"}, {"status": "completed", "output": []},
                         {"status": "completed", "output": [{"content": [{"type": "refusal", "refusal": "no"}]}]}):
            self.transport.side_effect = None
            self.transport.return_value = response
            with self.assertRaises(AnalystUnavailable):
                self.analyst.analyse([self.market], as_of=NOW.isoformat())
        self.transport.side_effect = RuntimeError("sensitive-provider-value")
        with self.assertRaises(AnalystUnavailable) as error:
            self.analyst.analyse([self.market], as_of=NOW.isoformat())
        self.assertNotIn("sensitive-provider-value", str(error.exception))
        self.assertNotIn("sensitive-provider-value", json.dumps(self.analyst.status()))

    def test_budget_is_durable_across_restarts_and_resets_on_next_utc_day(self):
        self.analyst.max_requests = 1
        self.analyst.analyse([self.market], as_of=NOW.isoformat())
        restarted = OpenAIAnalyst(state_path=self.directory.name+"/budget.json", transport=self.transport, clock=lambda: self.now)
        restarted.max_requests = 1
        with self.assertRaisesRegex(AnalystUnavailable, "BUDGET_EXHAUSTED"):
            restarted.analyse([self.market], as_of=NOW.isoformat())
        self.assertEqual(self.transport.call_count, 1)
        self.now += 86400
        restarted.analyse([self.market], as_of=NOW.isoformat())
        self.assertEqual(self.transport.call_count, 2)

    def test_missing_key_and_bad_input_make_no_paid_request(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            with self.assertRaisesRegex(AnalystUnavailable, "NOT_CONFIGURED"):
                self.analyst.analyse([self.market], as_of=NOW.isoformat())
        with self.assertRaisesRegex(AnalystUnavailable, "NO_FRESH"):
            self.analyst.analyse([], as_of=NOW.isoformat())
        self.transport.assert_not_called()

    def test_http_error_body_and_authorization_are_never_exposed(self):
        self.analyst._transport = None
        error = HTTPError("https://api.openai.com/v1/responses", 401, "unauthorized", {},
                          BytesIO(b"private-provider-response synthetic-unit-test-value"))
        with patch("openai_analyst.urlrequest.urlopen", side_effect=error):
            with self.assertRaisesRegex(AnalystUnavailable, "OPENAI_HTTP_401"):
                self.analyst.analyse([self.market], as_of=NOW.isoformat())
        status = json.dumps(self.analyst.status())
        self.assertNotIn("synthetic-unit-test-value", status)
        self.assertNotIn("private-provider-response", status)


class ProtectedBroker(DemoBroker):
    BASE_URL = "https://demo-api.ig.com/gateway/deal"

    def status(self):
        return {"account_id": "DEMO1", "environment": "DEMO", "base_url": self.BASE_URL, "live_money_execution": False}

    def resolve_global_market(self, **kwargs):
        return self.resolve_market("BITCOIN")

    def market_details(self, epic, require_quote=False):
        details = super().market_details(epic, require_quote=require_quote)
        if "BITCOIN" in epic:
            details["instrument"]["name"] = "Bitcoin ($0.1)"
        return details


class OpenAIIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-unit-test-value"})
        self.env.start(); self.addCleanup(self.env.stop)
        self.time_patch = patch("openai_integration.time.time", return_value=NOW.timestamp())
        self.time_patch.start(); self.addCleanup(self.time_patch.stop)
        self.broker = ProtectedBroker()
        self.raw = range_candles()
        # Derive protective levels from the actual evidence, including for a SELL.
        def transport(payload):
            context = json.loads(payload["input"])
            entry = next(m["reference_price"] for m in context["markets"] if m["symbol"] == "EURUSD")
            p = proposal(direction="SELL")
            p.update(stop_price=entry+.005, target_price=entry-.003)
            return response_for(payload, [p])
        self.transport = Mock(side_effect=transport)
        self.analyst = OpenAIAnalyst(state_path=self.directory.name+"/budget.json", transport=self.transport, clock=lambda: NOW.timestamp())
        self.engine = OpenAIStrategyEngine(broker=self.broker, frame_func=lambda _: self.raw.copy(), analyst=self.analyst,
                                          state_path=self.directory.name+"/signals.json", now_func=lambda: NOW)
        self.execution = OpenAIExecutionEngine(broker=self.broker, ranking_source=lambda: {},
            state_path=self.directory.name+"/positions.json", now_func=lambda: NOW)
        self.seed = next(s for s in CATEGORY_MARKET_SEEDS if s["key"] == "EURUSD")

    def selected(self):
        row = self.engine._evaluate_seed(self.seed)
        self.assertTrue(row["standard_eligible"], row["rejection_reasons"])
        return row

    def test_one_analysis_per_bar_not_one_paid_request_per_market(self):
        row = self.selected()
        for seed in self.engine._active_market_seeds():
            self.engine._evaluate_seed(seed)
        self.assertEqual(self.transport.call_count, 1)
        self.assertEqual(row["direction"], "SELL")
        self.assertIsNone(row["model_ai_confidence"])
        self.assertEqual(self.engine.evidence_coverage()["markets_total"], 30)
        crypto = next(r for r in self.engine.universe("CRYPTO") if r["key"] == "BITCOIN")
        self.assertTrue(crypto["execution_active"])

    def test_ai_can_choose_a_setup_without_legacy_direction_or_geographic_gate(self):
        row = self.selected()
        row["session_active"] = False
        self.assertNotEqual(analyze_regime(self.raw, "EURUSD", now=NOW)["direction"], row["direction"])
        allowed, reason = self.execution._may_open(row, [])
        self.assertTrue(allowed, reason)
        self.assertLess(row["target_r"], 1.2)
        self.execution._open_candidate(row, [])
        self.assertEqual(len(self.broker.opened), 1)
        order = self.broker.opened[0]
        self.assertIn("stop_level", order)
        self.assertIn("limit_level", order)
        self.assertFalse(order["allow_size_increment_retry"])
        position = self.execution._state["positions"][0]
        self.assertLessEqual(position["estimated_stop_risk_cash"], position["risk_cash_budget"]*1.001)
        self.assertEqual(position["risk_per_trade_pct"], 1.0)

    def test_stale_decisions_quotes_bad_market_and_non_demo_brokers_are_rejected(self):
        row = self.selected()
        for changes, reason in (({"signal_timestamp": (NOW-pd.Timedelta(minutes=6)).isoformat()}, "DECISION_STALE"),
                                ({"signal_timestamp": (NOW+pd.Timedelta(seconds=1)).isoformat()}, "DECISION_STALE"),
                                ({"ig_quote_timestamp": NOW.timestamp()-181}, "QUOTE_STALE"),
                                ({"ig_market_status": "CLOSED"}, "NOT_TRADEABLE"),
                                ({"ig_offer": float("nan")}, "QUOTE_INVALID")):
            with self.subTest(changes=changes):
                allowed, message = self.execution._may_open({**row, **changes}, [])
                self.assertFalse(allowed)
                self.assertIn(reason, message)
        with patch.object(self.broker, "status", return_value={"environment": "LIVE", "live_money_execution": True}):
            self.assertEqual(self.execution._may_open(row, []), (False, "IG_DEMO_REQUIRED"))

    def test_duplicate_capacity_and_fresh_quote_guards_remain_active(self):
        row = self.selected()
        self.execution.max_open_positions = 1
        self.execution._state["positions"] = [{"status": "OPEN", "symbol": "OTHER", "category": "FOREX"}]
        self.assertFalse(self.execution._may_open(row, [])[0])
        self.execution._state["positions"] = [{"status": "CLOSED", "setup_id": row["setup_id"]}]
        self.assertIn("already traded", self.execution._may_open(row, [])[1])
        self.execution._state["positions"] = []
        self.broker.spread = .1
        self.execution._open_candidate(row, [])
        self.assertEqual(self.broker.opened, [])
        self.assertIn("Fresh IG", self.execution._state["last_risk_rejection"]["reason"])

    def test_missing_attached_protection_closes_the_demo_order(self):
        row = self.selected()
        self.broker.strip_protection = True
        with self.assertRaises(RuntimeError):
            self.execution._open_candidate(row, [])
        self.assertEqual(self.broker.closed, ["D1"])
        self.assertEqual(self.execution._state["positions"], [])

    def test_bad_or_stale_market_evidence_never_reaches_openai(self):
        for mode in ("invalid", "stale"):
            with self.subTest(mode=mode):
                self.engine._analysis_period = None
                bad = self.raw.copy()
                if mode == "invalid":bad.iloc[-1, bad.columns.get_loc("High")] = .01
                else:bad.index -= pd.Timedelta(days=1)
                self.engine.frame_func = lambda _: bad
                row = self.engine._evaluate_seed(self.seed)
                self.assertFalse(row["standard_eligible"])
                self.assertEqual(row["direction"], "WAIT")
        self.transport.assert_not_called()

    def test_forward_and_probability_floors_do_not_veto_a_valid_ai_decision(self):
        row = self.selected()
        prime = object.__new__(OpenAIPrimeArchitecture)
        allowed, reasons = prime._strong_gate({**row, "quant_confidence": 0, "model_ai_confidence": 0, "smart_fast_score": 0}, {"issues": []})
        self.assertTrue(allowed, reasons)
        self.assertFalse(prime._strong_gate(row, {"issues": ["BROKER_QUOTE_STALE"]})[0])

    def test_bitcoin_is_selected_on_a_weekday_but_must_have_the_verified_identity(self):
        def bitcoin_response(payload):
            context = json.loads(payload["input"])
            entry = next(m["reference_price"] for m in context["markets"] if m["symbol"] == "BITCOIN")
            p = proposal("BITCOIN")
            p.update(stop_price=entry-.005, target_price=entry+.003)
            return response_for(payload, [p])
        self.transport.side_effect = bitcoin_response
        seed = next(s for s in CATEGORY_MARKET_SEEDS if s["key"] == "BITCOIN")
        row = self.engine._evaluate_seed(seed)
        self.assertLess(NOW.weekday(), 5)
        self.assertTrue(row["standard_eligible"], row["rejection_reasons"])
        self.assertEqual(row["ig_epic"], "CS.D.BITCOIN.CFBMU.IP")
        self.assertTrue(self.execution._may_open(row, [])[0])
        self.engine._analysis_period = None
        wrong = self.broker.market_details(row["ig_epic"])
        wrong["instrument"]["name"] = "Ether"
        with patch.object(self.broker, "market_details", return_value=wrong):
            rejected = self.engine._evaluate_seed(seed)
        self.assertFalse(rejected["standard_eligible"])

    def test_daily_caps_and_no_legacy_entry_lane_are_preserved(self):
        row = self.selected()
        with patch.object(self.execution, "_opened_today_sast", return_value=self.execution.max_daily_fx_entries):
            allowed, reason = self.execution._may_open(row, [])
            self.assertFalse(allowed)
            self.assertIn("daily FOREX entry cap", reason)
        from fastapi import FastAPI
        from weekend_runtime_v613 import install
        with patch.dict(os.environ, {"JASONG_ANALYSIS_MODE": "OPENAI", "WEEKEND_AUTOTRADE": "true"}), patch("threading.Thread.start"):
            worker = install(FastAPI(), {"broker": self.broker})
        self.assertFalse(worker.enabled)
        self.assertEqual(worker.status()["disabled_reason"], "OPENAI_IS_ENTRY_AUTHORITY")

    def test_inflight_analysis_cannot_expose_an_old_decision_or_trigger_a_second_request(self):
        self.selected()
        self.engine._analysis_lock.acquire()
        try:
            row = self.engine._evaluate_seed(self.seed)
            self.assertFalse(row["standard_eligible"])
            self.assertFalse(row["openai_validated"])
        finally:
            self.engine._analysis_lock.release()
        self.assertEqual(self.transport.call_count, 1)

    def test_ranked_api_market_stays_eligible_without_forward_history_or_fake_probability(self):
        from provenance import ProvenanceRegistry
        import chatgpt_actions
        import chatgpt_mcp
        row = self.selected()
        self.engine._state["evaluations"] = {row["symbol"]: row}
        prime = OpenAIPrimeArchitecture(intelligence=self.engine, broker=self.broker,
            provenance_registry=ProvenanceRegistry(default_analysis_source="SYNTHETIC_UNIT_TEST"),
            legacy_evidence_source=None, state_dir=self.directory.name)
        ranked = prime.category_rankings("FOREX")["FOREX"][0]
        self.assertTrue(ranked["standard_eligible"], ranked["rejection_reasons"])
        self.assertTrue(ranked["execution_eligible"])
        self.assertFalse(ranked["prime_qualified"])
        self.assertFalse(ranked["compound_eligible"])
        self.assertIsNone(ranked["model_ai_confidence"])
        for compact in (chatgpt_actions._compact_market(ranked), chatgpt_mcp._compact_market(ranked)):
            self.assertTrue(compact["openai_validated"])
            self.assertTrue(compact["standard_eligible"])
            self.assertEqual(compact["openai_analysis"]["model"], self.analyst.model)


if __name__ == "__main__":
    unittest.main()
