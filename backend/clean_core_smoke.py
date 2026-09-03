"""V6.11 FX + XAUUSD Active Clean Core import/startup smoke test.

Run from backend/:
    python clean_core_smoke.py

This test deliberately does not place an order.
"""
from __future__ import annotations

import importlib
import sys


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


runtime = importlib.import_module("clean_core_runtime")

assert getattr(runtime, "app", None) is not None
assert getattr(runtime, "IG_DEMO_BROKER", None) is not None
assert getattr(runtime, "COMPOUND_ENGINE", None) is not None
assert getattr(runtime, "V63_SPECIALIST_SYSTEM", None) is not None

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
}
missing = sorted(required - paths)
if missing:
    raise AssertionError("Missing clean-core routes: " + ", ".join(missing))

print("V6.11 FX + XAUUSD ACTIVE CLEAN CORE SMOKE PASS")
print("routes:", len(paths))
print("broker:", runtime.IG_DEMO_BROKER.status().get("environment"))
print("live_money_execution: False")
