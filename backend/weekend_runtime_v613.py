from __future__ import annotations

from typing import Any, Dict

from weekend_market_engine import WeekendMarketEngine

RUNTIME_VERSION = "6.13-weekend-structure-execution-v7"

# Verified from the connected IG DEMO market search. Identity and tradeability
# are deliberately separate: retaining the epic never makes it executable.
VERIFIED_WEEKEND_MARKETS = {
    "BITCOIN": {
        "epic": "CS.D.BITCOIN.CFBMU.IP",
        "name": "Bitcoin ($0.1)",
        "instrument_type": "CURRENCIES",
        "last_status": "UNKNOWN",
        "last_checked": 0.0,
        "identity_verified": True,
        "source": "IG_DEMO_VERIFIED",
    }
}


def install(app: Any, runtime: Dict[str, Any]) -> WeekendMarketEngine:
    broker = runtime["broker"]
    engine = WeekendMarketEngine(broker)

    # Seed only identities already verified against IG DEMO. The engine still
    # calls market_details for this exact epic and assess_market must see
    # TRADEABLE + a usable quote before candle analysis. _execute performs a
    # second fresh broker guard immediately before any DEMO order.
    for symbol, market in VERIFIED_WEEKEND_MARKETS.items():
        engine._availability[symbol] = dict(market)

    # Publicly identify this corrected runtime generation without changing the
    # underlying strategy ID or weakening any V6 execution guard.
    engine._state["version"] = RUNTIME_VERSION
    runtime["weekend_market_engine"] = engine
    engine.start_thread()

    @app.get("/weekend-market/status")
    def weekend_market_status() -> Dict[str, Any]:
        return engine.status()

    @app.get("/weekend-market/scan")
    def weekend_market_scan() -> Dict[str, Any]:
        return engine.tick()

    app.title = "Jasong AI Trader V6.13 Adaptive FX + XAUUSD + Weekend API"
    app.version = "6.13"
    app.description = (
        "IG DEMO only: adaptive FX and XAUUSD weekday/session execution plus "
        "broker-driven fail-closed weekend crypto structure execution."
    )
    app.openapi_schema = None
    return engine
