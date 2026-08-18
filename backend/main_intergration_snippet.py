"""Copy these lines into backend/main.py after COMPOUND_ENGINE is created.

This file is intentionally a small integration shim, not a replacement main.py,
so current V6.8.x reconciliation/dual-track changes remain intact.
"""

from specialist_market_integration import install_specialist_market_system


def _v690_specialist_frame(seed: dict):
    # Prefer the multi-asset loader already present in V6.7.3+; keep a narrow
    # fallback for installations where the function was renamed.
    loader = globals().get("_v673_global_market_data")
    if callable(loader):
        raw = loader(seed)
    else:
        public_symbol = seed.get("analysis_symbol") or seed.get("symbol") or seed.get("key")
        data_loader = globals().get("get_data")
        if not callable(data_loader):
            raise RuntimeError("No compatible Jasong market-data loader found")
        try:
            raw = data_loader(public_symbol, period="1mo", interval="15m")
        except TypeError:
            raw = data_loader(public_symbol)
    indicators = add_indicators(raw)
    model = train_model(indicators)
    enriched = enrich(indicators, model)
    if enriched is None or enriched.empty:
        raise ValueError(f"No Jasong Rule+ML enrichment for {seed.get('key')}")

    # Preserve raw OHLCV in case an older enrich() implementation drops fields.
    for column in ("Open", "High", "Low", "Close", "Volume"):
        if column in raw.columns and column not in enriched.columns:
            enriched[column] = raw[column].reindex(enriched.index)
    return enriched


try:
    GLOBAL_MARKET_ENGINE.stop_thread()
except Exception:
    pass

V690_SPECIALIST_SYSTEM = install_specialist_market_system(
    app=app,
    broker=IG_DEMO_BROKER,
    compound_engine=COMPOUND_ENGINE,
    frame_func=_v690_specialist_frame,
    ownership_components=[globals().get("IG_DEMO_MIRROR")],
)
CATEGORY_STRATEGY_ENGINE = V690_SPECIALIST_SYSTEM["intelligence"]
CATEGORY_PORTFOLIO_ENGINE = V690_SPECIALIST_SYSTEM["portfolio"]

# Backward-compatible alias: existing /global-markets/* handlers now read the
# specialist category engine instead of continuing the old generic scanner.
GLOBAL_MARKET_ENGINE = CATEGORY_STRATEGY_ENGINE
