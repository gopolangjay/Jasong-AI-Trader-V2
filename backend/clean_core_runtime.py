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


VERSION = "6.13-adaptive-fx-xau-weekend"
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
        title="Jasong AI Trader V6.13 Adaptive FX + XAUUSD + Weekend API",
        version=VERSION,
        description=(
            "Clean IG DEMO runtime: adaptive FX and XAUUSD execution plus a "
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

    broker = IGDemoBroker()
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
    )
    RUNTIME["specialist_system"] = system
    RUNTIME["market_data"] = system.get("market_data")

    if hasattr(compound, "start_thread"):
        compound.start_thread()

    @app.get("/")
    def root() -> Dict[str, Any]:
        return {
            "service": "Jasong AI Trader",
            "version": VERSION,
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
            "version": VERSION,
            "runtime": "CLEAN_CORE",
            "broker": broker.status(),
            "specialist_installed": bool(RUNTIME.get("specialist_system")),
            "weekend_market_installed": weekend is not None,
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
