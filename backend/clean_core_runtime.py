from __future__ import annotations

import os
from typing import Any, Dict, List

import pandas as pd
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from compound_engine import EliteCompoundEngine
from engine import enrich, train_model
from ig_demo_broker import IGDemoBroker
from indicators import add_indicators
from specialist_market_data import ResilientSpecialistMarketData
from specialist_market_integration import install_specialist_market_system


from runtime_version import VERSION, build_info
from candle_data import normalize_candles
from openai_policy import discretionary_enabled, enabled as openai_enabled
RUNTIME: Dict[str, Any] = {}


def _state_dir() -> str:
    return "/var/data" if os.path.isdir("/var/data") else "/tmp"


def _v673_global_market_data(seed: Dict[str, Any]) -> pd.DataFrame:
    loader = RUNTIME.get("market_data")
    if loader is None:
        loader = ResilientSpecialistMarketData(state_dir=_state_dir())
        RUNTIME["market_data"] = loader
    return loader.load(seed)


def specialist_frame(seed: Dict[str, Any]) -> pd.DataFrame:
    raw = _v673_global_market_data(seed)
    raw = normalize_candles(raw, interval="15min", closed_only=True)
    if openai_enabled():
        # Model analysis consumes observed closed OHLCV. Legacy ML is not a gate.
        return raw
    indicators = add_indicators(raw)
    model = train_model(indicators)
    enriched = enrich(indicators, model)
    if enriched is None or enriched.empty:
        raise ValueError(f"No Rule+ML enrichment for {seed.get('key')}")
    for column in ("Open", "High", "Low", "Close", "Volume"):
        if column in raw.columns and column not in enriched.columns:
            enriched[column] = raw[column].reindex(enriched.index)
    return enriched


def _compound_candidates(_: float = 0.0) -> List[Dict[str, Any]]:
    system = RUNTIME.get("specialist_system")
    if not isinstance(system, dict):
        return []
    forward_prime = system.get("forward_prime")
    if forward_prime is None:
        return []
    try:
        return list(forward_prime.compound_candidates() or [])
    except Exception:
        return []


def create_app() -> FastAPI:
    app = FastAPI(
        title="Jasong AI Trader OpenAI DEMO Analyst API",
        version=VERSION,
        description=(
            "Clean IG DEMO runtime: regime-aware FX and XAUUSD execution plus a "
            "broker-driven fail-closed weekend market path."
        ),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    broker = IGDemoBroker(guarded_entries_only=discretionary_enabled())
    RUNTIME["broker"] = broker
    compound = EliteCompoundEngine(
        broker=broker,
        candidate_source=_compound_candidates,
        state_path=os.getenv("COMPOUND_STATE_PATH", f"{_state_dir()}/jasong_elite_compound.json"),
    )
    RUNTIME["compound_engine"] = compound

    system = install_specialist_market_system(
        app=app,
        broker=broker,
        compound_engine=compound,
        frame_func=specialist_frame,
        ownership_components=[],
        regime_mode=not openai_enabled(),
        openai_mode=openai_enabled(),
    )
    RUNTIME["specialist_system"] = system
    RUNTIME["market_data"] = system.get("market_data")

    if openai_enabled():
        from openai_integration import policy_status
    else:
        from regime_integration import policy_status

    @app.get("/strategy/regime-policy")
    def regime_policy() -> Dict[str, Any]:
        return policy_status()

    if hasattr(compound, "start_thread"):
        compound.start_thread()

    @app.get("/")
    def root() -> Dict[str, Any]:
        return {
            "service": "Jasong AI Trader",
            **build_info(),
            "strategy_ids": policy_status()["strategy_ids"],
            "runtime": "CLEAN_CORE",
            "broker_environment": "IG_DEMO",
            "prime_authority": "BROKER_SETTLED_FORWARD_ONLY",
            "historical_validation_mode": "INFORMATIONAL_ONLY",
            "historical_execution_veto": False,
            "live_money_execution": False,
        }

    @app.get("/health")
    def health() -> Dict[str, Any]:
        weekend = RUNTIME.get("weekend_market_engine")
        return {
            "ok": True,
            **build_info(),
            "runtime": "CLEAN_CORE",
            "broker": broker.status(),
            "specialist_installed": bool(RUNTIME.get("specialist_system")),
            "weekend_market_installed": weekend is not None,
            "regime_strategy_ids": policy_status()["strategy_ids"],
            "analysis_mode": "OPENAI" if openai_enabled() else "REGIME",
            "openai_analyst": (system["intelligence"].analyst_status() if openai_enabled() else None),
            "financialdata_net": RUNTIME["market_data"].fdn.status(),
            "weekend_strategy_id": getattr(weekend, "_state", {}).get("strategy_id") if weekend is not None else None,
            "legacy_main_imported": False,
            "legacy_execution_reliability_imported": False,
            "live_money_execution": False,
        }

    return app


app = create_app()
IG_DEMO_BROKER = RUNTIME["broker"]
COMPOUND_ENGINE = RUNTIME["compound_engine"]
V63_SPECIALIST_SYSTEM = RUNTIME["specialist_system"]
CATEGORY_STRATEGY_ENGINE = V63_SPECIALIST_SYSTEM["intelligence"]
CATEGORY_PORTFOLIO_ENGINE = V63_SPECIALIST_SYSTEM["portfolio"]
