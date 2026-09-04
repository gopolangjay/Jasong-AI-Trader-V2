from __future__ import annotations

import os

# Keep IG historical candles disabled unless explicitly enabled.
if str(
    os.getenv("JASONG_ALLOW_IG_HISTORICAL_CANDLES", "false")
).strip().lower() not in {"1", "true", "yes", "on"}:
    os.environ["IG_DEMO_MARKET_DATA"] = "false"

os.environ.setdefault("IG_DEMO_POSITIONS_CACHE_SECONDS", "3")

# V6.12 installs the adaptive FX signal, PRIME and structural-risk policy before
# clean_core_runtime imports the execution stack.  Gold remains on its existing
# XAUUSD liquidity/structure policy; the relaxed thresholds are FX-specific.
import adaptive_fx_v612  # noqa: F401,E402

import uvicorn
from clean_core_runtime import app


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        proxy_headers=True,
    )
