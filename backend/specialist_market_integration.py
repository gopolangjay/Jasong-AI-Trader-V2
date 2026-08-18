from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from category_execution_engine import CategoryExecutionEngine
from category_strategy_engine import (
    CATEGORY_ORDER,
    CategoryStrategyEngine,
    MODEL_AI_MIN_CONFIDENCE,
    QUANT_MIN_CONFIDENCE,
)


def _position_identity(row: Dict[str, Any]) -> tuple[str, str]:
    return (
        str(row.get("epic") or row.get("ig_epic") or "").upper().strip(),
        str(row.get("symbol") or row.get("market") or row.get("market_name") or "").upper().strip(),
    )


def _non_category_broker_positions(
    broker: Any,
    intelligence: CategoryStrategyEngine,
) -> List[Dict[str, Any]]:
    """Return every open broker position not owned by the JSCAT track.

    Category execution must share the existing account-wide capacity and exposure
    budget with Compound, learning/boundary tracks and manual positions.  Rows are
    enriched with specialist exposure tags when their EPIC/symbol is known.
    """
    try:
        payload = broker.positions() or {}
    except Exception:
        return []

    tag_index: Dict[str, List[str]] = {}
    try:
        for candidate in intelligence.candidates():
            tags = list(candidate.get("exposure_tags") or [])
            epic, symbol = _position_identity(candidate)
            if epic:
                tag_index["E:" + epic] = tags
            if symbol:
                tag_index["S:" + symbol] = tags
    except Exception:
        pass

    rows: List[Dict[str, Any]] = []
    for item in payload.get("positions", []) or []:
        if not isinstance(item, dict):
            continue
        position = item.get("position") or {}
        market = item.get("market") or {}
        if not isinstance(position, dict) or not isinstance(market, dict):
            continue
        ref = str(position.get("dealReference") or "").upper().strip()
        if ref.startswith("JSCAT_"):
            continue
        epic = str(market.get("epic") or position.get("epic") or "").upper().strip()
        market_name = str(market.get("instrumentName") or market.get("marketName") or "").upper().strip()
        tags = tag_index.get("E:" + epic, tag_index.get("S:" + market_name, []))
        if ref.startswith("JSCMP_"):
            track = "COMPOUND"
        elif ref.startswith(("JASONG_", "JSBND_", "JSLRN_", "JSELT_")):
            track = "JASONG_LEARNING"
        else:
            track = "EXTERNAL_MANUAL"
        rows.append({
            "track": track,
            "deal_id": position.get("dealId"),
            "deal_reference": position.get("dealReference"),
            "epic": epic,
            "market_name": market_name,
            "direction": str(position.get("direction") or "").upper(),
            "size": position.get("size") if position.get("size") is not None else position.get("dealSize"),
            "market_status": market.get("marketStatus"),
            "exposure_tags": list(tags),
        })
    return rows


def _extend_owned_prefix(component: Any) -> None:
    """Teach a V6.8.x ownership-aware component that JSCAT_* is internal."""
    if component is None:
        return
    for attr in (
        "jasong_owned_reference_prefixes",
        "owned_reference_prefixes",
        "owned_prefixes",
    ):
        value = getattr(component, attr, None)
        if isinstance(value, list) and "JSCAT_" not in value:
            value.append("JSCAT_")
        elif isinstance(value, set):
            value.add("JSCAT_")
        elif isinstance(value, tuple) and "JSCAT_" not in value:
            try:
                setattr(component, attr, value + ("JSCAT_",))
            except Exception:
                pass
    for method_name in (
        "register_owned_reference_prefix",
        "add_owned_reference_prefix",
        "allow_reference_prefix",
    ):
        method = getattr(component, method_name, None)
        if callable(method):
            try:
                method("JSCAT_")
            except Exception:
                pass


def _enable_compound_category_coexistence(compound_engine: Any) -> None:
    """Allow JSCAT_* positions to coexist without contaminating Compound P&L.

    V6.8.x already supports multi-track Jasong ownership.  Older V6.7.x engines
    considered every non-JSCMP position "foreign" and would block a new basket.
    For those older engines only, make JSCAT positions non-blocking while leaving
    _compound_positions untouched so Compound P&L still contains JSCMP only.
    """
    version = str(getattr(compound_engine, "VERSION", "") or "")

    # Extend known ownership-prefix containers/methods without dropping any
    # existing V6.8.x Jasong prefixes.
    _extend_owned_prefix(compound_engine)

    # Compatibility only for recovered V6.7.x behavior.  Do not replace newer
    # V6.8.x dual-track foreign-position logic.
    if not version.startswith("6.7.") or not hasattr(compound_engine, "_foreign_positions"):
        return

    original = compound_engine._foreign_positions

    def _foreign_positions_allow_category(positions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        rows = original(positions)
        allowed: List[Dict[str, Any]] = []
        for row in rows:
            ref = str(
                row.get("deal_reference")
                or row.get("dealReference")
                or ""
            ).upper()
            if ref.startswith("JSCAT_"):
                continue
            allowed.append(row)
        return allowed

    compound_engine._foreign_positions = _foreign_positions_allow_category


def install_specialist_market_system(
    *,
    app: Any,
    broker: Any,
    compound_engine: Any,
    frame_func: Callable[[Dict[str, Any]], pd.DataFrame],
    ownership_components: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """Install V6.9 specialist-market engines into an existing Jasong FastAPI app.

    This is deliberately additive so the current V6.8.x execution-integrity,
    reconciliation and Compound accounting code does not need to be replaced.
    """
    base_dir = "/var/data" if os.path.isdir("/var/data") else "/tmp"
    category_state_path = os.getenv(
        "CATEGORY_STRATEGY_STATE_PATH",
        f"{base_dir}/jasong_category_strategies.json",
    )
    portfolio_state_path = os.getenv(
        "CATEGORY_PORTFOLIO_STATE_PATH",
        f"{base_dir}/jasong_category_portfolio.json",
    )

    _enable_compound_category_coexistence(compound_engine)
    for component in ownership_components or []:
        _extend_owned_prefix(component)

    intelligence = CategoryStrategyEngine(
        broker=broker,
        frame_func=frame_func,
        state_path=category_state_path,
    )
    intelligence.start_thread()

    # Preserve the user's 28 / 40 policy inside Compound too. This alters only
    # the confidence floors; all newer reconciliation/integrity behavior remains
    # owned by the existing Compound engine.
    if hasattr(compound_engine, "quant_min_confidence"):
        compound_engine.quant_min_confidence = QUANT_MIN_CONFIDENCE
    if hasattr(compound_engine, "ai_min_confidence"):
        compound_engine.ai_min_confidence = MODEL_AI_MIN_CONFIDENCE

    # Compound now receives only rank #1/#2 candidates from each specialist
    # category that pass all evidence/live/IG gates. No filler candidates.
    compound_engine.candidate_source = lambda _cycle_capital: intelligence.compound_candidates()
    if hasattr(compound_engine, "correlation_source"):
        compound_engine.correlation_source = intelligence.correlation_matrix

    portfolio = CategoryExecutionEngine(
        broker=broker,
        ranking_source=intelligence.category_rankings,
        external_positions_source=lambda: _non_category_broker_positions(broker, intelligence),
        state_path=portfolio_state_path,
    )
    portfolio.start_thread()

    # FastAPI route registration is done programmatically to avoid a large,
    # fragile replacement of the current backend/main.py.
    app.add_api_route(
        "/market-categories/status",
        lambda: intelligence.status(),
        methods=["GET"],
        name="market_categories_status_v691",
    )

    def category_universe() -> Dict[str, Any]:
        return {
            "version": "6.9.1",
            "categories": {
                category: intelligence.universe(category)
                for category in CATEGORY_ORDER
            },
            "confidence_policy": {
                "quant_min_pct": 28.0,
                "model_ai_min_pct": 40.0,
                "historical_validation_target_pct": 70.0,
            },
            "live_money_execution": False,
        }

    app.add_api_route(
        "/market-categories",
        category_universe,
        methods=["GET"],
        name="market_categories_universe_v691",
    )

    def category_rankings(category: str) -> Dict[str, Any]:
        clean = str(category or "").upper().strip()
        if clean not in CATEGORY_ORDER:
            return {
                "version": "6.9.1",
                "category": clean,
                "count": 0,
                "selections": [],
                "error": f"Unknown category. Use: {', '.join(CATEGORY_ORDER)}",
                "live_money_execution": False,
            }
        rows = intelligence.category_rankings(clean).get(clean, [])
        return {
            "version": "6.9.1",
            "category": clean,
            "count": len(rows),
            "selections": rows,
            "live_money_execution": False,
        }

    def optimizer_status() -> Dict[str, Any]:
        rankings = intelligence.category_rankings()
        categories = {}
        for category in CATEGORY_ORDER:
            rows = rankings.get(category, [])
            top = rows[0] if rows else None
            categories[category] = {
                "selected_strategy_id": (top or {}).get("strategy_id"),
                "selected_strategy_name": (top or {}).get("strategy_name"),
                "optimizer_selection_stable": bool((top or {}).get("optimizer_selection_stable", False)),
                "selection_win_rate_pct": (top or {}).get("selection_win_rate_pct"),
                "selection_profit_factor": (top or {}).get("selection_profit_factor"),
                "final_holdout_win_rate_pct": (top or {}).get("historical_win_rate_pct"),
                "final_holdout_profit_factor": (top or {}).get("historical_profit_factor"),
                "historical_70_verified": bool((top or {}).get("historical_70_verified", False)),
                "leaderboard": (top or {}).get("optimizer_leaderboard", []),
            }
        return {
            "version": "6.9.1",
            "method": "finite variant selection on 40%-70%; untouched final holdout 70%-100%",
            "final_holdout_used_for_selection": False,
            "quant_min_pct": 28.0,
            "model_ai_min_pct": 40.0,
            "historical_validation_target_pct": 70.0,
            "categories": categories,
            "live_money_execution": False,
        }

    app.add_api_route(
        "/market-categories/optimizer",
        optimizer_status,
        methods=["GET"],
        name="market_category_optimizer_v691",
    )

    def compound_candidates() -> Dict[str, Any]:
        rows = intelligence.compound_candidates()
        return {
            "version": "6.9.1",
            "count": len(rows),
            "candidates": rows,
            "rule": "Only category ranks #1/#2 that pass 28/40 + real 70% historical validation + IG gates",
            "live_money_execution": False,
        }

    # Register this static path before /{category}; Starlette resolves routes in
    # declaration order.
    app.add_api_route(
        "/market-categories/compound-candidates",
        compound_candidates,
        methods=["GET"],
        name="market_category_compound_candidates_v691",
    )

    app.add_api_route(
        "/market-categories/{category}",
        category_rankings,
        methods=["GET"],
        name="market_category_rankings_v691",
    )

    app.add_api_route(
        "/market-categories/run-now",
        lambda: intelligence.run_now(),
        methods=["POST"],
        name="market_categories_run_now_v691",
    )

    def run_category(category: str) -> Dict[str, Any]:
        clean = str(category or "").upper().strip()
        if clean not in CATEGORY_ORDER:
            return {"version": "6.9.1", "error": "Unknown category", "category": clean}
        return intelligence.run_now(clean)

    app.add_api_route(
        "/market-categories/{category}/run-now",
        run_category,
        methods=["POST"],
        name="market_category_run_now_v691",
    )

    app.add_api_route(
        "/category-portfolio/status",
        lambda: portfolio.status(),
        methods=["GET"],
        name="category_portfolio_status_v691",
    )
    app.add_api_route(
        "/category-portfolio/positions",
        lambda: {
            "version": "6.9.1",
            "positions": portfolio.positions(),
            "live_money_execution": False,
        },
        methods=["GET"],
        name="category_portfolio_positions_v691",
    )
    app.add_api_route(
        "/category-portfolio/run-now",
        lambda: portfolio.tick(),
        methods=["POST"],
        name="category_portfolio_run_now_v691",
    )

    return {
        "intelligence": intelligence,
        "portfolio": portfolio,
        "version": "6.9.1",
    }
