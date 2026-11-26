from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "backend"))
from runtime_version import VERSION
from regime_integration import RegimeStrategyEngine, RegimeExecutionEngine, RegimePrimeArchitecture
import chatgpt_actions
import chatgpt_mcp


class RuntimeVersionTests(unittest.TestCase):
    def test_runtime_and_gateways_share_release_and_deploy_commit_without_starting_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"RENDER_GIT_COMMIT": "a"*40, "JASONG_MCP_AUTH_STATE_PATH": directory+"/oauth.json",
                   "JASONG_ACTIONS_AUDIT_PATH": directory+"/actions.jsonl",
                   "JASONG_ACTIONS_API_KEY": "version-test-key-"*3,
                   "IG_DEMO_API_KEY": "", "IG_DEMO_IDENTIFIER": "", "IG_DEMO_PASSWORD": "", "OPENAI_API_KEY": ""}
            with patch.dict(os.environ, env):
                with patch("threading.Thread.start", return_value=None):
                    runtime = importlib.import_module("clean_core_runtime")
                    # Capture gateway build metadata without launching trading workers.
                    app = runtime.create_app()
                with TestClient(app) as client:
                    for path in ("/health", "/", "/assistant/status", "/chatgpt-mcp/status"):
                        response = client.get(path, headers={"X-Jasong-Action-Key": env["JASONG_ACTIONS_API_KEY"]})
                        self.assertEqual(response.status_code, 200, path)
                        data = response.json()
                        self.assertEqual(data["version"], VERSION, path)
                        self.assertEqual(data["commit_sha"], "a"*40, path)
                        self.assertFalse(data.get("live_money_execution", False))
                    health = client.get("/health").json()
                    self.assertEqual(health["broker"]["version"], VERSION)
                    self.assertEqual(health["broker"]["commit_sha"], "a"*40)
                    self.assertEqual(health["broker"]["environment"], "DEMO")
                    self.assertFalse(health["broker"]["live_money_execution"])
                    self.assertEqual(health["analysis_mode"], "OPENAI")
                    analysis = client.get("/strategy/openai-analysis").json()
                    self.assertFalse(analysis["configured"])
                    self.assertFalse(analysis["can_call_broker"])
                    self.assertEqual(type(runtime.CATEGORY_STRATEGY_ENGINE).__name__, "OpenAIStrategyEngine")
                    self.assertEqual(type(runtime.CATEGORY_PORTFOLIO_ENGINE).__name__, "OpenAIExecutionEngine")
                    self.assertEqual(client.get("/openapi.json").json()["info"]["version"], VERSION)
                    # No API key and no confirmation must still prevent writes.
                    self.assertEqual(client.post("/assistant/write/open-qualified", json={"symbol": "EURUSD", "confirm": True}).status_code, 401)
                    self.assertEqual(client.post("/assistant/write/open-qualified", json={"symbol": "EURUSD"},
                                                headers={"X-Jasong-Action-Key": env["JASONG_ACTIONS_API_KEY"]}).status_code, 422)
                actions = chatgpt_actions.build_actions_openapi()
                self.assertEqual(actions["info"]["version"], VERSION)
                self.assertFalse(app.state.jasong_mcp_status["trade_write_tools_exposed"])
                self.assertFalse(app.state.jasong_actions_status["write_bypasses_validation"])
                self.assertEqual({runtime.VERSION, chatgpt_actions.VERSION, chatgpt_mcp.VERSION,
                                  RegimeStrategyEngine.VERSION, RegimeExecutionEngine.VERSION,
                                  RegimePrimeArchitecture.VERSION}, {VERSION})

    def test_malformed_regime_data_never_reaches_broker_preflight(self):
        from test_regime_strategy import trend_candles
        from category_strategy_engine import CATEGORY_MARKET_SEEDS
        with tempfile.TemporaryDirectory() as directory:
            raw = trend_candles()
            raw.iloc[-1, raw.columns.get_loc("High")] = .01
            broker = Mock()
            engine = RegimeStrategyEngine(broker=broker, frame_func=lambda _: raw,
                                          state_path=directory+"/signals.json")
            seed = next(s for s in CATEGORY_MARKET_SEEDS if s["key"] == "EURUSD")
            with self.assertRaises(ValueError):
                engine._evaluate_seed(seed)
            broker.resolve_market.assert_not_called()
            broker.open_epic_position.assert_not_called()


if __name__ == "__main__":
    unittest.main()
