"""V6.14 offline clean-core import/startup smoke test.

Run from backend/:
    python clean_core_smoke.py

This test deliberately does not place an order.
"""
from __future__ import annotations

import importlib
import sys
from unittest.mock import patch


FORBIDDEN = {
    "main",
    "execution_reliability",
    "v64_learning_engine",
    "auto_manager",
    "trade_watcher",
    "deep_validator",
    "sequential_scanner",
    "global_market_engine",
    "ig_demo_bridge",
}


with patch("threading.Thread.start", return_value=None):
    # Exercise the production entrypoint, including its legacy rollback imports.
    importlib.import_module("run_server")
    runtime = importlib.import_module("clean_core_runtime")

assert getattr(runtime, "app", None) is not None
assert getattr(runtime, "IG_DEMO_BROKER", None) is not None
assert getattr(runtime, "COMPOUND_ENGINE", None) is not None
assert getattr(runtime, "V63_SPECIALIST_SYSTEM", None) is not None
from runtime_version import VERSION
assert runtime.app.version == VERSION
from openai_policy import enabled as openai_enabled
expected_strategy = "OpenAIStrategyEngine" if openai_enabled() else "RegimeStrategyEngine"
expected_execution = "OpenAIExecutionEngine" if openai_enabled() else "RegimeExecutionEngine"
assert type(runtime.CATEGORY_STRATEGY_ENGINE).__name__ == expected_strategy
assert type(runtime.CATEGORY_PORTFOLIO_ENGINE).__name__ == expected_execution
if openai_enabled():
    from openai_policy import discretionary_enabled
    assert runtime.CATEGORY_PORTFOLIO_ENGINE.discretionary == discretionary_enabled()
    assert runtime.V63_SPECIALIST_SYSTEM["excursion_tracker"].discretionary == discretionary_enabled()

loaded_forbidden = sorted(name for name in FORBIDDEN if name in sys.modules)
if loaded_forbidden:
    raise AssertionError(
        "Legacy modules entered clean runtime: " + ", ".join(loaded_forbidden)
    )

paths = {getattr(route, "path", "") for route in runtime.app.routes}
required = {
    "/health",
    "/market-categories/status",
    "/market-categories/compound-candidates",
    "/category-portfolio/status",
    "/mobile/sync",
    "/trade-excursions",
    "/strategy/regime-policy",
}
missing = sorted(required - paths)
if missing:
    raise AssertionError("Missing clean-core routes: " + ", ".join(missing))

print("IG DEMO CLEAN CORE SMOKE PASS", expected_strategy)
print("routes:", len(paths))
print("broker:", runtime.IG_DEMO_BROKER.status().get("environment"))
print("live_money_execution: False")
