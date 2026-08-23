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


VERSION = "6.3-clean-core"

# The clean runtime owns only the current execution architecture.
# Legacy paper/watcher/history/automanager modules are intentionally absent.
RUNTIME: Dict[str, Any] = {}


def _state_dir() -> str:
    return "/var/data" if os.path.isdir("/var/data") else "/tmp"


# specialist_market_integration replaces this symbol with its resilient loader.
# Keeping the name preserves the current integration contract without importing
# the legacy main.py module.
def _v673_global_market_data(seed: Dict[str, Any]) -> pd.DataFrame:
    loader = RUNTIME.get("market_data")
    if loader is None:
        loader = ResilientSpecialistMarketData(state_dir=_state_dir())
        RUNTIME["market_data"] = loader
    return loader.load(seed)


def specialist_frame(seed: Dict[str, Any]) -> pd.DataFrame:
    """Current Rule + ML specialist analysis pipeline."""
    raw = _v673_global_market_data(seed)
    indicators = add_indicators(raw)
    model = train_model(indicators)
    enriched = enrich(indicators, model)

    if enriched is None or enriched.empty:
        raise ValueError(f"No Rule+ML enrichment for {seed.get('key')}")

    # Preserve raw OHLCV when an older enrich() omits those fields.
    for column in ("Open", "High", "Low", "Close", "Volume"):
        if column in raw.columns and column not in enriched.columns:
            enriched[column] = raw[column].reindex(enriched.index)

    return enriched


def _compound_candidates(_: float = 0.0) -> List[Dict[str, Any]]:
    """Late-bound PRIME candidate source used to break bootstrap circularity."""
    system = RUNTIME.get("specialist_system")
    if not isinstance(system, dict):
        return []

    forward_prime = system.get("forward_prime")
    if forward_prime is None:
        return []

    try:
        rows = forward_prime.compound_candidates()
        return list(rows or [])
    except Exception:
        return []


def create_app() -> FastAPI:
    app = FastAPI(
        title="Jasong AI Trader V6.3 Clean Core API",
        version=VERSION,
        description=(
            "Clean runtime: specialist category intelligence, broker-settled "
            "forward PRIME, controlled IG DEMO category execution, Elite "
            "compound, MFE/MAE, mobile sync and ChatGPT diagnostics."
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
        state_path=os.getenv(
            "COMPOUND_STATE_PATH",
            f"{_state_dir()}/jasong_elite_compound.json",
        ),
    )
    RUNTIME["compound_engine"] = compound

    # This module already owns the current architecture:
    # category intelligence + broker-settled forward PRIME + JSCAT execution
    # + MFE/MAE + mobile sync + ChatGPT MCP/actions.
    system = install_specialist_market_system(
        app=app,
        broker=broker,
        compound_engine=compound,
        frame_func=specialist_frame,
        ownership_components=[],
    )
    RUNTIME["specialist_system"] = system
    RUNTIME["market_data"] = system.get("market_data")

    # Compound is started only after PRIME/category wiring exists.
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
        return {
            "ok": True,
            "version": VERSION,
            "runtime": "CLEAN_CORE",
            "broker": broker.status(),
            "specialist_installed": bool(RUNTIME.get("specialist_system")),
            "legacy_main_imported": False,
            "legacy_execution_reliability_imported": False,
            "live_money_execution": False,
        }

    return app


app = create_app()

# Compatibility aliases for diagnostics/mobile code that expect named globals.
IG_DEMO_BROKER = RUNTIME["broker"]
COMPOUND_ENGINE = RUNTIME["compound_engine"]
V63_SPECIALIST_SYSTEM = RUNTIME["specialist_system"]
CATEGORY_STRATEGY_ENGINE = V63_SPECIALIST_SYSTEM["intelligence"]
CATEGORY_PORTFOLIO_ENGINE = V63_SPECIALIST_SYSTEM["portfolio"]
