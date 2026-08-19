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
from chatgpt_actions import install_chatgpt_actions
from chatgpt_mcp import install_chatgpt_mcp
from prime_policy import ForwardPrimeArchitecture, make_provenance_frame_func
from provenance import ProvenanceRegistry


def _position_identity(row: Dict[str, Any]) -> tuple[str, str]:
    return (
        str(row.get("epic") or row.get("ig_epic") or "").upper().strip(),
        str(row.get("symbol") or row.get("market") or row.get("market_name") or "").upper().strip(),
    )


def _non_category_broker_positions(
    broker: Any,
    intelligence: CategoryStrategyEngine,
) -> List[Dict[str, Any]]:
    """Return every open broker position not owned by the JSCAT track."""
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
    """Teach ownership-aware components that JSCAT_* is an internal DEMO track."""
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
    """Allow JSCAT_* positions to coexist without contaminating Compound P&L."""
    version = str(getattr(compound_engine, "VERSION", "") or "")
    _extend_owned_prefix(compound_engine)
    if not version.startswith("6.7.") or not hasattr(compound_engine, "_foreign_positions"):
        return

    original = compound_engine._foreign_positions

    def _foreign_positions_allow_category(positions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        rows = original(positions)
        return [
            row
            for row in rows
            if not str(row.get("deal_reference") or row.get("dealReference") or "").upper().startswith("JSCAT_")
        ]

    compound_engine._foreign_positions = _foreign_positions_allow_category


def install_specialist_market_system(
    *,
    app: Any,
    broker: Any,
    compound_engine: Any,
    frame_func: Callable[[Dict[str, Any]], pd.DataFrame],
    ownership_components: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """Install V6.9 specialist markets with broker-settled forward PRIME authority."""
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

    # Current V6.9.3 _v673_global_market_data uses yfinance for specialist
    # analysis. Keep the provider explicit/configurable instead of labelling the
    # strategy pipeline itself as the data source.
    analysis_price_source = os.getenv(
        "SPECIALIST_ANALYSIS_PRICE_SOURCE",
        "YAHOO_FINANCE",
    ).upper().strip()
    provenance_registry = ProvenanceRegistry(
        default_analysis_source=analysis_price_source,
    )
    provenance_frame_func = make_provenance_frame_func(
        frame_func,
        provenance_registry,
        analysis_source=analysis_price_source,
    )

    intelligence = CategoryStrategyEngine(
        broker=broker,
        frame_func=provenance_frame_func,
        state_path=category_state_path,
    )
    intelligence.start_thread()

    legacy_forward_evidence = next(
        (
            component
            for component in (ownership_components or [])
            if component is not None
            and hasattr(component, "status")
            and hasattr(component, "execution_guard_snapshot")
        ),
        None,
    )

    forward_prime = ForwardPrimeArchitecture(
        intelligence=intelligence,
        broker=broker,
        provenance_registry=provenance_registry,
        legacy_evidence_source=legacy_forward_evidence,
        state_dir=base_dir,
    )
    forward_prime.install(compound_engine=compound_engine)

    # Make every existing consumer (including GPT Actions/MCP helpers) read the
    # same forward-policy rankings instead of the legacy historical eligibility
    # aliases. Private optimiser/evaluation methods remain untouched.
    intelligence.category_rankings = forward_prime.category_rankings
    intelligence.compound_candidates = forward_prime.compound_candidates

    # Preserve the requested live confidence floors. Historical holdout/WF
    # values are no longer written into Compound PRIME thresholds.
    if hasattr(compound_engine, "quant_min_confidence"):
        compound_engine.quant_min_confidence = QUANT_MIN_CONFIDENCE
    if hasattr(compound_engine, "ai_min_confidence"):
        compound_engine.ai_min_confidence = MODEL_AI_MIN_CONFIDENCE
    for attr in (
        "fast_score_min",
        "prime_fast_score_min",
        "global_fast_score_min",
        "compound_fast_score_min",
    ):
        if hasattr(compound_engine, attr):
            setattr(compound_engine, attr, 45.0)

    if hasattr(compound_engine, "correlation_source"):
        compound_engine.correlation_source = intelligence.correlation_matrix

    # The standard Category IG DEMO track is now the controlled STRONG learning
    # lane. Compound receives only rank #1/#2 candidates that have already met
    # the broker-settled forward PRIME criteria.
    portfolio = CategoryExecutionEngine(
        broker=broker,
        ranking_source=forward_prime.category_rankings,
        external_positions_source=lambda: _non_category_broker_positions(broker, intelligence),
        state_path=portfolio_state_path,
    )
    forward_prime.attach_category_portfolio(portfolio)
    portfolio.start_thread()

    app.title = "Jasong AI Trader V6.9.4 Forward API"
    app.version = "6.9.4-forward"
    app.description = (
        "Jasong AI Trader — specialist market intelligence, broker-settled "
        "forward PRIME validation, controlled IG DEMO learning, adaptive "
        "80/20 compound, and authenticated ChatGPT diagnostics."
    )
    app.openapi_schema = None

    # Assistant diagnostics should see the new PRIME authority, not the legacy
    # historical threshold labels.
    try:
        install_chatgpt_mcp(
            app,
            intelligence=intelligence,
            portfolio=portfolio,
            compound_engine=compound_engine,
            broker=broker,
            evidence_source=forward_prime,
        )
    except Exception as exc:
        mcp_error = f"{type(exc).__name__}: {exc}"
        app.state.jasong_mcp_install_error = mcp_error
        if not any(getattr(route, "path", "") == "/chatgpt-mcp/status" for route in app.routes):
            app.add_api_route(
                "/chatgpt-mcp/status",
                lambda: {
                    "version": "6.9.4-forward",
                    "enabled": True,
                    "installed": False,
                    "runtime_ready": False,
                    "error": mcp_error,
                    "read_only": True,
                    "trade_write_tools_exposed": False,
                    "live_money_execution": False,
                },
                methods=["GET"],
                name="jasong_mcp_status_install_error",
            )

    try:
        install_chatgpt_actions(
            app,
            intelligence=intelligence,
            portfolio=portfolio,
            compound_engine=compound_engine,
            broker=broker,
            evidence_source=forward_prime,
        )
    except Exception as exc:
        app.state.jasong_actions_install_error = f"{type(exc).__name__}: {exc}"

    app.add_api_route(
        "/market-categories/status",
        lambda: {
            **intelligence.status(),
            "prime_authority": "BROKER_SETTLED_FORWARD_ONLY",
            "historical_validation_mode": "INFORMATIONAL_ONLY",
        },
        methods=["GET"],
        name="market_categories_status_v694",
    )

    def category_universe() -> Dict[str, Any]:
        return {
            "version": "6.9.4-forward",
            "categories": {
                category: intelligence.universe(category)
                for category in CATEGORY_ORDER
            },
            "confidence_policy": {
                "quant_min_pct": 28.0,
                "model_ai_min_pct": 40.0,
                "fast_score_min": 45.0,
                "historical_validation_mode": "INFORMATIONAL_ONLY",
                "historical_execution_veto": False,
                "prime_authority": "BROKER_SETTLED_FORWARD_ONLY",
                "forward_min_settled_trades": forward_prime.validator.config.min_settled_trades_for_prime,
                "forward_rolling_window_trades": forward_prime.validator.config.rolling_window_trades,
                "forward_min_profit_factor": forward_prime.validator.config.min_profit_factor,
                "forward_min_expectancy_r": forward_prime.validator.config.min_expectancy_r,
                "forward_min_win_rate": forward_prime.validator.config.min_win_rate,
                "forward_min_bootstrap_positive_expectancy": forward_prime.validator.config.min_bootstrap_prob_positive_expectancy,
                "forward_max_drawdown_r": forward_prime.validator.config.max_drawdown_r,
            },
            "analysis_price_source": analysis_price_source,
            "broker_quote_source": "IG_DEMO",
            "live_money_execution": False,
        }

    app.add_api_route(
        "/market-categories",
        category_universe,
        methods=["GET"],
        name="market_categories_universe_v694",
    )

    def category_rankings(category: str) -> Dict[str, Any]:
        clean = str(category or "").upper().strip()
        if clean not in CATEGORY_ORDER:
            return {
                "version": "6.9.4-forward",
                "category": clean,
                "count": 0,
                "selections": [],
                "error": f"Unknown category. Use: {', '.join(CATEGORY_ORDER)}",
                "live_money_execution": False,
            }
        rows = forward_prime.category_rankings(clean).get(clean, [])
        return {
            "version": "6.9.4-forward",
            "category": clean,
            "count": len(rows),
            "selections": rows,
            "live_money_execution": False,
        }

    app.add_api_route(
        "/market-categories/optimizer",
        lambda: intelligence.optimizer_summary(),
        methods=["GET"],
        name="market_category_optimizer_v694",
    )
    app.add_api_route(
        "/market-categories/evidence-health",
        lambda: intelligence.evidence_coverage(),
        methods=["GET"],
        name="market_category_evidence_health_v694",
    )
    app.add_api_route(
        "/market-categories/full-refresh",
        lambda: intelligence.full_refresh_status(),
        methods=["GET"],
        name="market_category_full_refresh_status_v694",
    )
    app.add_api_route(
        "/market-categories/full-refresh",
        lambda: intelligence.start_full_refresh(force=True),
        methods=["POST"],
        name="market_category_full_refresh_start_v694",
    )

    def compound_candidates() -> Dict[str, Any]:
        rows = forward_prime.compound_candidates()
        return {
            "version": "6.9.4-forward",
            "count": len(rows),
            "candidates": rows,
            "rule": (
                "Category rank #1/#2 + Quant >=28 + directional AI >=40 + "
                "Fast >=45 + fresh sourced analysis + fresh IG quote + "
                "tradeable/spread gates + broker-settled forward PF/expectancy/"
                "WR/drawdown/bootstrap PRIME criteria. Historical holdout and "
                "walk-forward metrics are informational only."
            ),
            "live_money_execution": False,
        }

    app.add_api_route(
        "/market-categories/compound-candidates",
        compound_candidates,
        methods=["GET"],
        name="market_category_compound_candidates_v694",
    )
    app.add_api_route(
        "/market-categories/{category}",
        category_rankings,
        methods=["GET"],
        name="market_category_rankings_v694",
    )
    app.add_api_route(
        "/market-categories/run-now",
        lambda: intelligence.run_now(),
        methods=["POST"],
        name="market_categories_run_now_v694",
    )

    def run_category(category: str) -> Dict[str, Any]:
        clean = str(category or "").upper().strip()
        if clean not in CATEGORY_ORDER:
            return {"version": "6.9.4-forward", "error": "Unknown category", "category": clean}
        return intelligence.run_now(clean)

    app.add_api_route(
        "/market-categories/{category}/run-now",
        run_category,
        methods=["POST"],
        name="market_category_run_now_v694",
    )
    app.add_api_route(
        "/category-portfolio/status",
        lambda: portfolio.status(),
        methods=["GET"],
        name="category_portfolio_status_v694",
    )
    app.add_api_route(
        "/category-portfolio/positions",
        lambda: {
            "version": "6.9.4-forward",
            "positions": portfolio.positions(),
            "live_money_execution": False,
        },
        methods=["GET"],
        name="category_portfolio_positions_v694",
    )
    app.add_api_route(
        "/category-portfolio/run-now",
        lambda: portfolio.tick(),
        methods=["POST"],
        name="category_portfolio_run_now_v694",
    )

    forward_prime.routes(app)

    return {
        "intelligence": intelligence,
        "portfolio": portfolio,
        "forward_prime": forward_prime,
        "version": "6.9.4-forward",
    }
