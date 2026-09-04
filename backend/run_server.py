from __future__ import annotations

import os

# Keep IG historical candles disabled for the legacy runtime unless explicitly enabled.
# V6.13 weekend execution uses IG candles directly through its dedicated broker path.
if str(
    os.getenv("JASONG_ALLOW_IG_HISTORICAL_CANDLES", "false")
).strip().lower() not in {"1", "true", "yes", "on"}:
    os.environ["IG_DEMO_MARKET_DATA"] = "false"

os.environ.setdefault("IG_DEMO_POSITIONS_CACHE_SECONDS", "3")

# V6.12 adaptive FX policy remains the weekday/session FX authority.
import adaptive_fx_v612  # noqa: F401,E402
import adaptive_fx_v612_status  # noqa: F401,E402

import uvicorn
import clean_core_runtime
from clean_core_runtime import app
from weekend_runtime_v613 import install as install_weekend_v613

# V6.13 is a separate fail-closed path. It cannot route ordinary FX into weekend
# execution and only submits an order after a fresh IG TRADEABLE + quote check.
WEEKEND_MARKET_ENGINE = install_weekend_v613(app, clean_core_runtime.RUNTIME)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        proxy_headers=True,
    )
