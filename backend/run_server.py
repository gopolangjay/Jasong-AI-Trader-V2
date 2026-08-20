from __future__ import annotations

import os

# Protect the IG DEMO account's historical-data allowance.
#
# IG remains the broker source of truth for:
# - executable quotes / spread preflight
# - open positions
# - MFE / MAE observations
# - order entry / close / native take-profit
#
# Historical OHLCV analysis is routed through Twelve Data / Yahoo / persisted
# specialist cache instead of consuming IG's historical-candle allowance.
if str(
    os.getenv("JASONG_ALLOW_IG_HISTORICAL_CANDLES", "false")
).strip().lower() not in {"1", "true", "yes", "on"}:
    os.environ["IG_DEMO_MARKET_DATA"] = "false"

# Coalesce only short bursts of identical position reads. Any broker write
# invalidates the cache immediately in execution_reliability.py.
os.environ.setdefault("IG_DEMO_POSITIONS_CACHE_SECONDS", "3")

import uvicorn

import execution_reliability
import main as jasong_main


def _install_runtime_health() -> None:
    system = getattr(jasong_main, "V693_SPECIALIST_SYSTEM", None)
    if not isinstance(system, dict):
        system = getattr(jasong_main, "V694_SPECIALIST_SYSTEM", None)
    if not isinstance(system, dict):
        system = {}

    broker = getattr(jasong_main, "IG_DEMO_BROKER", None)
    execution_reliability.install_execution_health_route(
        jasong_main.app,
        system=system,
        broker=broker,
    )


_install_runtime_health()


if __name__ == "__main__":
    uvicorn.run(
        jasong_main.app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        proxy_headers=True,
    )
