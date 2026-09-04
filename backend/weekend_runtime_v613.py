from __future__ import annotations

from typing import Any, Dict

from weekend_market_engine import VERSION, WeekendMarketEngine


def install(app: Any, runtime: Dict[str, Any]) -> WeekendMarketEngine:
    broker = runtime["broker"]
    engine = WeekendMarketEngine(broker)
    runtime["weekend_market_engine"] = engine
    engine.start_thread()

    @app.get("/weekend-market/status")
    def weekend_market_status() -> Dict[str, Any]:
        return engine.status()

    @app.get("/weekend-market/scan")
    def weekend_market_scan() -> Dict[str, Any]:
        return engine.tick()

    # Unify the public application metadata with the deployed execution generation.
    app.title = "Jasong AI Trader V6.13 Adaptive FX + XAUUSD + Weekend API"
    app.version = "6.13"
    app.description = (
        "IG DEMO only: adaptive FX and XAUUSD weekday/session execution plus "
        "broker-driven fail-closed weekend crypto structure execution."
    )
    app.openapi_schema = None
    return engine
