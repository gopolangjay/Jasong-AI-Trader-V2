from __future__ import annotations

"""Plus-compatible GPT Actions gateway for Jasong AI Trader V6.9.3.

Design goals:
- API-key authenticated read + controlled write access for a private Custom GPT.
- All broker-changing operations remain IG DEMO-only.
- No action exposes IG credentials, session tokens, or Render secrets.
- Write actions require explicit confirmation and are rate-limited.
- Opening a position cannot bypass Jasong qualification/risk gates: it routes
  through CategoryExecutionEngine.open_qualified_symbol().
"""

import hashlib
import hmac
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

VERSION = "6.9.3"
HEADER_NAME = "X-Jasong-Action-Key"
OWNED_CATEGORY_PREFIX = "JSCAT_"

_WRITE_LOCK = threading.RLock()
_WRITE_EVENTS: List[float] = []


class ConfirmBody(BaseModel):
    confirm: bool = Field(
        ...,
        description="Must be true only after the user explicitly requested this write action.",
    )


class CategoryAutotradeBody(ConfirmBody):
    enabled: bool


class SymbolWriteBody(ConfirmBody):
    symbol: str = Field(..., min_length=1, max_length=80)


class ClosePositionBody(ConfirmBody):
    deal_id: str = Field(..., min_length=1, max_length=120)


def _public_base_url() -> str:
    return os.getenv(
        "JASONG_ACTIONS_PUBLIC_BASE_URL",
        os.getenv(
            "JASONG_MCP_PUBLIC_BASE_URL",
            "https://jasong-ai-trader-v2.onrender.com",
        ),
    ).strip().rstrip("/")


def _audit_path() -> Path:
    explicit = os.getenv("JASONG_ACTIONS_AUDIT_PATH", "").strip()
    if explicit:
        return Path(explicit)
    return Path(
        "/var/data/jasong_actions_audit.jsonl"
        if os.path.isdir("/var/data")
        else "/tmp/jasong_actions_audit.jsonl"
    )


def _safe_json(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "<max-depth>"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        clean: Dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            lower = key.lower()
            if any(
                marker in lower
                for marker in (
                    "password",
                    "api_key",
                    "apikey",
                    "secret",
                    "access_token",
                    "refresh_token",
                    "authorization",
                    "security_token",
                    "x-security-token",
                    "cst",
                )
            ):
                continue
            if lower in {"recent_returns", "raw_prices", "candles", "bars"}:
                continue
            clean[key] = _safe_json(raw_value, depth + 1)
        return clean
    if isinstance(value, (list, tuple, set)):
        return [_safe_json(item, depth + 1) for item in list(value)[:250]]
    try:
        return _safe_json(dict(value), depth + 1)
    except Exception:
        return str(value)


def _call_first(obj: Any, names: Iterable[str], *args: Any, **kwargs: Any) -> Any:
    for name in names:
        fn = getattr(obj, name, None)
        if callable(fn):
            try:
                return fn(*args, **kwargs)
            except TypeError:
                try:
                    return fn()
                except Exception:
                    continue
            except Exception:
                continue
    return None


def _audit(action: str, *, write: bool = False, **details: Any) -> None:
    try:
        target = _audit_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "at": time.time(),
            "version": VERSION,
            "action": action,
            "write": bool(write),
            "details": _safe_json(details),
        }
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        try:
            os.chmod(target, 0o600)
        except Exception:
            pass
    except Exception:
        pass


def _action_key_configured() -> bool:
    return len(os.getenv("JASONG_ACTIONS_API_KEY", "").strip()) >= 32


def _require_key(request: Request) -> None:
    expected = os.getenv("JASONG_ACTIONS_API_KEY", "").strip()
    if len(expected) < 32:
        raise HTTPException(
            status_code=503,
            detail="JASONG_ACTIONS_API_KEY is not securely configured",
        )
    supplied = str(request.headers.get(HEADER_NAME) or "").strip()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid Jasong Action API key")


def _write_enabled() -> bool:
    return os.getenv("JASONG_ACTIONS_WRITE_ENABLED", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _require_confirm(confirm: bool) -> None:
    if confirm is not True:
        raise HTTPException(
            status_code=400,
            detail="Explicit confirm=true is required for this write action",
        )


def _write_slot() -> None:
    if not _write_enabled():
        raise HTTPException(
            status_code=403,
            detail="GPT Actions write access is disabled. Set JASONG_ACTIONS_WRITE_ENABLED=true.",
        )
    limit = max(
        1,
        min(
            60,
            int(os.getenv("JASONG_ACTIONS_MAX_WRITES_PER_MINUTE", "10")),
        ),
    )
    now = time.time()
    with _WRITE_LOCK:
        _WRITE_EVENTS[:] = [stamp for stamp in _WRITE_EVENTS if now - stamp < 60.0]
        if len(_WRITE_EVENTS) >= limit:
            raise HTTPException(
                status_code=429,
                detail="GPT Actions write rate limit reached; retry later.",
            )
        _WRITE_EVENTS.append(now)


def _assert_demo_only(broker: Any) -> None:
    status = _call_first(broker, ("status",)) or {}
    environment = str(status.get("environment") or "").upper().strip()
    base_url = str(status.get("base_url") or getattr(broker, "BASE_URL", "")).lower()
    if environment != "DEMO":
        raise HTTPException(
            status_code=403,
            detail=f"Refusing write: broker environment is {environment or 'UNKNOWN'}, not DEMO.",
        )
    if "demo-api.ig.com" not in base_url:
        raise HTTPException(
            status_code=403,
            detail="Refusing write: broker base URL is not the IG DEMO endpoint.",
        )
    if bool(status.get("live_money_execution")):
        raise HTTPException(
            status_code=403,
            detail="Refusing write: live-money execution flag is true.",
        )


def _ranked_rows(intelligence: Any) -> List[Dict[str, Any]]:
    rankings = intelligence.category_rankings() or {}
    return [
        dict(row)
        for category in ("FOREX", "INDICES", "CRYPTO", "METALS", "ENERGY", "SHARES")
        for row in rankings.get(category, [])[:5]
        if isinstance(row, dict)
    ]


def _compact_market(row: Dict[str, Any]) -> Dict[str, Any]:
    return _safe_json({
        "key": row.get("key") or row.get("symbol"),
        "market": row.get("market") or row.get("name"),
        "category": row.get("category"),
        "asset_class": row.get("asset_class"),
        "strategy": row.get("strategy_name"),
        "regime": row.get("regime"),
        "direction": row.get("direction"),
        "quant_confidence_pct": row.get("quant_confidence_pct"),
        "model_ai_directional_confidence_pct": row.get("model_ai_directional_confidence_pct"),
        "fast_score": row.get("smart_fast_score"),
        "holdout_win_rate_pct": row.get("historical_win_rate_pct"),
        "profit_factor": row.get("historical_profit_factor"),
        "holdout_trades": row.get("historical_trades"),
        "sample_pass": row.get("historical_sample_pass"),
        "walk_forward_pass": row.get("walk_forward_pass"),
        "walk_forward_min_win_rate_pct": row.get("walk_forward_min_win_rate_pct"),
        "walk_forward_median_win_rate_pct": row.get("walk_forward_median_win_rate_pct"),
        "walk_forward_profitable_folds": row.get("walk_forward_profitable_folds"),
        "selection_stable": row.get("optimizer_selection_stable"),
        "standard_eligible": row.get("standard_eligible"),
        "compound_eligible": row.get("compound_eligible"),
        "ig_tradeable": row.get("ig_tradeable"),
        "ig_epic": row.get("ig_epic"),
        "ig_spread_bps": row.get("ig_spread_bps"),
        "spread_pass": row.get("spread_pass"),
        "rejection_reasons": row.get("rejection_reasons") or [],
        "state": "PRIME" if row.get("compound_eligible") else (
            "STRONG" if (
                float(row.get("smart_fast_score") or 0) >= 45
                and float(row.get("quant_confidence_pct") or 0) >= 28
                and float(row.get("model_ai_directional_confidence_pct") or 0) >= 40
            ) else "WATCH"
        ),
        "evaluated_at": row.get("evaluated_at"),
    })


def _market_key(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _find_market(intelligence: Any, symbol: str) -> Optional[Dict[str, Any]]:
    wanted = _market_key(symbol)
    for row in _ranked_rows(intelligence):
        variants = {
            _market_key(row.get("key")),
            _market_key(row.get("symbol")),
            _market_key(row.get("market")),
            _market_key(row.get("name")),
        }
        if wanted and wanted in variants:
            return row
    return None


def build_actions_openapi() -> Dict[str, Any]:
    base = _public_base_url()
    common_401 = {
        "401": {"description": "Invalid or missing Jasong Action API key"},
        "503": {"description": "Action API key is not configured"},
    }

    def get_op(operation_id: str, summary: str, description: str, parameters=None):
        return {
            "operationId": operation_id,
            "summary": summary,
            "description": description,
            "parameters": parameters or [],
            "responses": {"200": {"description": "Successful response"}, **common_401},
            "security": [{"JasongActionKey": []}],
            "x-openai-isConsequential": False,
        }

    def post_op(operation_id: str, summary: str, description: str, schema: Dict[str, Any]):
        return {
            "operationId": operation_id,
            "summary": summary,
            "description": (
                "CONSEQUENTIAL WRITE. Only call after the user explicitly requests "
                "this exact change. " + description
            ),
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": schema}},
            },
            "responses": {
                "200": {"description": "Write processed"},
                "400": {"description": "Confirmation or request validation failed"},
                "401": {"description": "Invalid or missing Jasong Action API key"},
                "403": {"description": "Write access disabled or DEMO-only safety check failed"},
                "429": {"description": "Write rate limit reached"},
            },
            "security": [{"JasongActionKey": []}],
            "x-openai-isConsequential": True,
        }

    confirm_prop = {
        "type": "boolean",
        "description": "Set true only after the user explicitly asked to perform this write.",
    }

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Jasong AI Trader V6.9.3 Actions",
            "version": VERSION,
            "description": (
                "Private read/write GPT Actions API for Jasong AI Trader. "
                "All broker-changing operations are hard-limited to IG DEMO. "
                "No broker credentials are exposed."
            ),
        },
        "servers": [{"url": base}],
        "components": {
            "securitySchemes": {
                "JasongActionKey": {
                    "type": "apiKey",
                    "in": "header",
                    "name": HEADER_NAME,
                }
            }
        },
        "paths": {
            "/assistant/status": {
                "get": get_op(
                    "getAssistantStatus",
                    "Get GPT Actions status",
                    "Read current Actions gateway configuration and DEMO safety state.",
                )
            },
            "/assistant/system": {
                "get": get_op(
                    "getJasongSystem",
                    "Get Jasong system status",
                    "Read category, portfolio, Compound, broker and forward-evidence status.",
                )
            },
            "/assistant/opportunities": {
                "get": get_op(
                    "getMarketOpportunities",
                    "Get live market opportunities",
                    "Read current top-five-per-category opportunities; optionally filter by category or PRIME/STRONG/WATCH state.",
                    [
                        {"name": "category", "in": "query", "required": False, "schema": {"type": "string"}},
                        {"name": "state", "in": "query", "required": False, "schema": {"type": "string", "enum": ["PRIME", "STRONG", "WATCH"]}},
                        {"name": "limit", "in": "query", "required": False, "schema": {"type": "integer", "minimum": 1, "maximum": 30, "default": 20}},
                    ],
                )
            },
            "/assistant/market/{symbol}": {
                "get": get_op(
                    "getMarketDetails",
                    "Get market details",
                    "Read full current evidence and execution fields for one ranked market.",
                    [{"name": "symbol", "in": "path", "required": True, "schema": {"type": "string"}}],
                )
            },
            "/assistant/blockers/{symbol}": {
                "get": get_op(
                    "getExecutionBlockers",
                    "Explain execution blockers",
                    "Read the exact current reasons a market is not standard-eligible or PRIME.",
                    [{"name": "symbol", "in": "path", "required": True, "schema": {"type": "string"}}],
                )
            },
            "/assistant/prime": {
                "get": get_op(
                    "getPrimeMarkets",
                    "Get PRIME markets",
                    "Read current markets that pass the Compound candidate gates.",
                )
            },
            "/assistant/validation": {
                "get": get_op(
                    "getValidationStatus",
                    "Get validation status",
                    "Read optimizer, holdout, PF, sample and walk-forward validation evidence.",
                )
            },
            "/assistant/evidence-health": {
                "get": get_op(
                    "getEvidenceHealth",
                    "Get evidence health",
                    "Read 40-market optimization coverage and schema health.",
                )
            },
            "/assistant/portfolio": {
                "get": get_op(
                    "getCategoryPortfolio",
                    "Get Category portfolio",
                    "Read Category IG DEMO positions and portfolio capacity.",
                )
            },
            "/assistant/compound": {
                "get": get_op(
                    "getCompoundStatus",
                    "Get Compound status",
                    "Read current 80/20 Compound cycle, basket, capital, reserve and candidates.",
                )
            },
            "/assistant/ig-demo": {
                "get": get_op(
                    "getIGDemoStatus",
                    "Get IG DEMO broker status",
                    "Read safe IG DEMO account and open-position information. Credentials and session tokens are scrubbed.",
                )
            },
            "/assistant/trades": {
                "get": get_op(
                    "getTradeHistory",
                    "Get broker-settled trade history",
                    "Read current or requested forward-evidence phase.",
                    [{"name": "phase_id", "in": "query", "required": False, "schema": {"type": "integer", "minimum": 0, "default": 0}}],
                )
            },
            "/assistant/diagnostics": {
                "get": get_op(
                    "getDiagnostics",
                    "Get runtime diagnostics",
                    "Read current engine errors and health surfaces.",
                )
            },
            "/assistant/write/run-scan": {
                "post": post_op(
                    "runMarketScan",
                    "Run market scan now",
                    "Runs the specialist intelligence scan. This can change rankings and may subsequently allow normal Category autotrading to act.",
                    {
                        "type": "object",
                        "required": ["confirm"],
                        "properties": {
                            "confirm": confirm_prop,
                            "category": {
                                "type": "string",
                                "description": "Optional category: FOREX, INDICES, CRYPTO, METALS, ENERGY, SHARES. Omit for all categories.",
                            },
                        },
                        "additionalProperties": False,
                    },
                )
            },
            "/assistant/write/full-refresh": {
                "post": post_op(
                    "forceFullRefresh",
                    "Force full 40-market refresh",
                    "Starts a FORCE_ALL optimization/evidence refresh across all 40 markets.",
                    {
                        "type": "object",
                        "required": ["confirm"],
                        "properties": {"confirm": confirm_prop},
                        "additionalProperties": False,
                    },
                )
            },
            "/assistant/write/category-autotrade": {
                "post": post_op(
                    "setCategoryAutotrade",
                    "Enable or disable Category autotrading",
                    "Changes Category autotrading for the current running process only. Render environment settings remain restart-time authority.",
                    {
                        "type": "object",
                        "required": ["confirm", "enabled"],
                        "properties": {
                            "confirm": confirm_prop,
                            "enabled": {"type": "boolean"},
                        },
                        "additionalProperties": False,
                    },
                )
            },
            "/assistant/write/open-qualified": {
                "post": post_op(
                    "openQualifiedCategoryPosition",
                    "Open a currently qualified Category position",
                    "Opens only a market that is currently standard-eligible and still passes Category portfolio/global exposure controls. The caller cannot provide EPIC, direction, size, or bypass flags.",
                    {
                        "type": "object",
                        "required": ["confirm", "symbol"],
                        "properties": {
                            "confirm": confirm_prop,
                            "symbol": {"type": "string", "minLength": 1, "maxLength": 80},
                        },
                        "additionalProperties": False,
                    },
                )
            },
            "/assistant/write/close-category-position": {
                "post": post_op(
                    "closeCategoryPosition",
                    "Close a Category IG DEMO position",
                    "Closes only an open JSCAT-owned Category position by deal ID. It cannot close manual, Compound, learning or live-money positions.",
                    {
                        "type": "object",
                        "required": ["confirm", "deal_id"],
                        "properties": {
                            "confirm": confirm_prop,
                            "deal_id": {"type": "string", "minLength": 1, "maxLength": 120},
                        },
                        "additionalProperties": False,
                    },
                )
            },
        },
    }


def install_chatgpt_actions(
    app: Any,
    *,
    intelligence: Any,
    portfolio: Any,
    compound_engine: Any,
    broker: Any,
    evidence_source: Any = None,
) -> Dict[str, Any]:
    """Install the authenticated Plus-compatible GPT Actions API."""

    if getattr(app.state, "jasong_actions_installed", False):
        return dict(getattr(app.state, "jasong_actions_status", {}) or {})

    enabled = os.getenv("JASONG_ACTIONS_ENABLED", "true").strip().lower() in {
        "1", "true", "yes", "on"
    }

    status: Dict[str, Any] = {
        "version": VERSION,
        "enabled": enabled,
        "installed": bool(enabled),
        "read_access": bool(enabled),
        "write_access": bool(enabled and _write_enabled()),
        "api_key_configured": _action_key_configured(),
        "authentication": f"API key via {HEADER_NAME}",
        "execution_mode": "IG_DEMO_ONLY",
        "live_money_execution": False,
        "write_controls": [
            "run_market_scan",
            "force_full_refresh",
            "set_category_autotrade_runtime",
            "open_currently_qualified_category_position",
            "close_JSCAT_category_position",
        ],
        "write_bypasses_validation": False,
    }

    if not enabled:
        app.state.jasong_actions_status = status
        app.add_api_route(
            "/assistant/status",
            lambda: status,
            methods=["GET"],
            name="jasong_actions_status_disabled",
        )
        return status

    def privacy() -> HTMLResponse:
        return HTMLResponse(
            """<!doctype html><html><body style="font-family:system-ui;max-width:760px;margin:40px auto;padding:0 16px">
            <h2>Jasong AI Trader GPT Actions Privacy</h2>
            <p>This private integration exposes Jasong AI Trader runtime data to an authenticated Custom GPT.</p>
            <p>IG passwords, API keys, session tokens and Render secrets are not returned by the API. Read and write calls are audit logged on the backend. Broker-changing actions are restricted to IG DEMO.</p>
            </body></html>"""
        )

    async def openapi_doc() -> JSONResponse:
        return JSONResponse(build_actions_openapi())

    async def actions_status(request: Request) -> Dict[str, Any]:
        _require_key(request)
        current = dict(status)
        current["write_access"] = bool(_write_enabled())
        current["broker"] = _safe_json(_call_first(broker, ("status",)))
        _audit("status")
        return current

    async def system_status(request: Request) -> Dict[str, Any]:
        _require_key(request)
        _audit("system")
        return _safe_json({
            "version": VERSION,
            "category_system": intelligence.status(),
            "portfolio": portfolio.status(),
            "compound": _call_first(compound_engine, ("status",)),
            "broker": _call_first(broker, ("status",)),
            "forward_evidence": _call_first(evidence_source, ("status",)) if evidence_source is not None else None,
            "actions": status,
            "live_money_execution": False,
        })

    async def opportunities(
        request: Request,
        category: str = "",
        state: str = "",
        limit: int = 20,
    ) -> Dict[str, Any]:
        _require_key(request)
        wanted_category = str(category or "").upper().strip()
        wanted_state = str(state or "").upper().strip()
        rows = [_compact_market(row) for row in _ranked_rows(intelligence)]
        if wanted_category:
            rows = [r for r in rows if str(r.get("category") or "").upper() == wanted_category]
        if wanted_state:
            rows = [r for r in rows if str(r.get("state") or "").upper() == wanted_state]
        rows = rows[:max(1, min(int(limit), 30))]
        _audit("opportunities", category=wanted_category, state=wanted_state, count=len(rows))
        return {"version": VERSION, "count": len(rows), "opportunities": rows}

    async def market_details(symbol: str, request: Request) -> Dict[str, Any]:
        _require_key(request)
        row = _find_market(intelligence, symbol)
        _audit("market_details", symbol=symbol)
        if row is None:
            return {"version": VERSION, "found": False, "symbol": symbol}
        return _safe_json(row)

    async def blockers(symbol: str, request: Request) -> Dict[str, Any]:
        _require_key(request)
        row = _find_market(intelligence, symbol)
        _audit("blockers", symbol=symbol)
        if row is None:
            return {"version": VERSION, "found": False, "symbol": symbol}
        return _safe_json({
            "version": VERSION,
            "symbol": row.get("symbol") or row.get("key"),
            "market": row.get("market") or row.get("name"),
            "direction": row.get("direction"),
            "standard_eligible": row.get("standard_eligible"),
            "compound_eligible": row.get("compound_eligible"),
            "rejection_reasons": row.get("rejection_reasons") or [],
            "policy": {
                "quant_min_pct": 28.0,
                "ai_min_pct": 40.0,
                "fast_min": 45.0,
                "holdout_wr_min_pct": 60.0,
                "profit_factor_min": 1.20,
                "holdout_trades_min": 10,
                "walk_forward_min_pct": 40.0,
            },
            "current": {
                "quant_pct": row.get("quant_confidence_pct"),
                "ai_pct": row.get("model_ai_directional_confidence_pct"),
                "fast": row.get("smart_fast_score"),
                "holdout_wr_pct": row.get("historical_win_rate_pct"),
                "profit_factor": row.get("historical_profit_factor"),
                "holdout_trades": row.get("historical_trades"),
                "wf_min_pct": row.get("walk_forward_min_win_rate_pct"),
                "wf_median_pct": row.get("walk_forward_median_win_rate_pct"),
                "wf_profitable_folds": row.get("walk_forward_profitable_folds"),
                "selection_stable": row.get("optimizer_selection_stable"),
                "ig_tradeable": row.get("ig_tradeable"),
                "spread_bps": row.get("ig_spread_bps"),
                "spread_pass": row.get("spread_pass"),
            },
        })

    async def prime(request: Request) -> Dict[str, Any]:
        _require_key(request)
        rows = [_compact_market(row) for row in intelligence.compound_candidates()]
        _audit("prime", count=len(rows))
        return {"version": VERSION, "count": len(rows), "prime_markets": rows}

    async def validation(request: Request) -> Dict[str, Any]:
        _require_key(request)
        _audit("validation")
        return _safe_json(intelligence.optimizer_summary())

    async def evidence_health(request: Request) -> Dict[str, Any]:
        _require_key(request)
        _audit("evidence_health")
        return _safe_json(intelligence.evidence_coverage())

    async def category_portfolio(request: Request) -> Dict[str, Any]:
        _require_key(request)
        _audit("portfolio")
        return _safe_json({"status": portfolio.status(), "positions": portfolio.positions(limit=200)})

    async def compound(request: Request) -> Dict[str, Any]:
        _require_key(request)
        _audit("compound")
        return _safe_json(_call_first(compound_engine, ("status",)) or {"status": "unavailable"})

    async def ig_demo(request: Request) -> Dict[str, Any]:
        _require_key(request)
        configured = bool(getattr(broker, "configured", lambda: False)())
        _audit("ig_demo", configured=configured)
        return _safe_json({
            "version": VERSION,
            "status": _call_first(broker, ("status",)),
            "accounts": _call_first(broker, ("accounts",)) if configured else None,
            "positions": _call_first(broker, ("positions",)) if configured else None,
            "live_money_execution": False,
        })

    async def trades(request: Request, phase_id: int = 0) -> Dict[str, Any]:
        _require_key(request)
        _audit("trades", phase_id=phase_id)
        if evidence_source is None or not hasattr(evidence_source, "phase_trade_analysis"):
            return {"version": VERSION, "available": False}
        try:
            return _safe_json(
                evidence_source.phase_trade_analysis(
                    int(phase_id) if int(phase_id) > 0 else None
                )
            )
        except Exception as exc:
            return {"version": VERSION, "available": False, "error": f"{type(exc).__name__}: {exc}"}

    async def diagnostics(request: Request) -> Dict[str, Any]:
        _require_key(request)
        _audit("diagnostics")
        return _safe_json({
            "version": VERSION,
            "category": intelligence.status(),
            "portfolio": portfolio.status(),
            "compound": _call_first(compound_engine, ("status",)),
            "broker": _call_first(broker, ("status",)),
            "forward_evidence": _call_first(evidence_source, ("status",)) if evidence_source is not None else None,
            "mcp_install_error": getattr(app.state, "jasong_mcp_install_error", None),
            "actions_install_error": getattr(app.state, "jasong_actions_install_error", None),
            "live_money_execution": False,
        })

    async def run_scan(request: Request) -> Dict[str, Any]:
        _require_key(request)
        payload = await request.json()
        confirm = bool((payload or {}).get("confirm"))
        category = str((payload or {}).get("category") or "").upper().strip()
        _require_confirm(confirm)
        _write_slot()
        _assert_demo_only(broker)
        if category and category not in {"FOREX", "INDICES", "CRYPTO", "METALS", "ENERGY", "SHARES"}:
            raise HTTPException(status_code=400, detail="Unknown category")
        result = intelligence.run_now(category if category else None)
        _audit("run_scan", write=True, category=category or "ALL")
        return _safe_json(result)

    async def full_refresh(request: Request, body: ConfirmBody) -> Dict[str, Any]:
        _require_key(request)
        _require_confirm(body.confirm)
        _write_slot()
        _assert_demo_only(broker)
        result = intelligence.start_full_refresh(force=True)
        _audit("full_refresh", write=True)
        return _safe_json(result)

    async def category_autotrade(request: Request, body: CategoryAutotradeBody) -> Dict[str, Any]:
        _require_key(request)
        _require_confirm(body.confirm)
        _write_slot()
        _assert_demo_only(broker)
        result = portfolio.set_enabled(body.enabled)
        _audit("category_autotrade", write=True, enabled=body.enabled)
        return _safe_json(result)

    async def open_qualified(request: Request, body: SymbolWriteBody) -> Dict[str, Any]:
        _require_key(request)
        _require_confirm(body.confirm)
        _write_slot()
        _assert_demo_only(broker)
        result = portfolio.open_qualified_symbol(body.symbol)
        _audit("open_qualified", write=True, symbol=body.symbol, opened=result.get("opened"))
        return _safe_json(result)

    async def close_category(request: Request, body: ClosePositionBody) -> Dict[str, Any]:
        _require_key(request)
        _require_confirm(body.confirm)
        _write_slot()
        _assert_demo_only(broker)
        result = portfolio.close_category_position(body.deal_id)
        _audit("close_category", write=True, deal_id=body.deal_id, closed=result.get("closed"))
        return _safe_json(result)

    app.add_api_route("/assistant/privacy", privacy, methods=["GET"], name="jasong_actions_privacy")
    app.add_api_route("/assistant/openapi.json", openapi_doc, methods=["GET"], name="jasong_actions_openapi")
    app.add_api_route("/assistant/status", actions_status, methods=["GET"], name="jasong_actions_status")
    app.add_api_route("/assistant/system", system_status, methods=["GET"], name="jasong_actions_system")
    app.add_api_route("/assistant/opportunities", opportunities, methods=["GET"], name="jasong_actions_opportunities")
    app.add_api_route("/assistant/market/{symbol}", market_details, methods=["GET"], name="jasong_actions_market")
    app.add_api_route("/assistant/blockers/{symbol}", blockers, methods=["GET"], name="jasong_actions_blockers")
    app.add_api_route("/assistant/prime", prime, methods=["GET"], name="jasong_actions_prime")
    app.add_api_route("/assistant/validation", validation, methods=["GET"], name="jasong_actions_validation")
    app.add_api_route("/assistant/evidence-health", evidence_health, methods=["GET"], name="jasong_actions_evidence_health")
    app.add_api_route("/assistant/portfolio", category_portfolio, methods=["GET"], name="jasong_actions_portfolio")
    app.add_api_route("/assistant/compound", compound, methods=["GET"], name="jasong_actions_compound")
    app.add_api_route("/assistant/ig-demo", ig_demo, methods=["GET"], name="jasong_actions_ig_demo")
    app.add_api_route("/assistant/trades", trades, methods=["GET"], name="jasong_actions_trades")
    app.add_api_route("/assistant/diagnostics", diagnostics, methods=["GET"], name="jasong_actions_diagnostics")

    app.add_api_route("/assistant/write/run-scan", run_scan, methods=["POST"], name="jasong_actions_run_scan")
    app.add_api_route("/assistant/write/full-refresh", full_refresh, methods=["POST"], name="jasong_actions_full_refresh")
    app.add_api_route("/assistant/write/category-autotrade", category_autotrade, methods=["POST"], name="jasong_actions_category_autotrade")
    app.add_api_route("/assistant/write/open-qualified", open_qualified, methods=["POST"], name="jasong_actions_open_qualified")
    app.add_api_route("/assistant/write/close-category-position", close_category, methods=["POST"], name="jasong_actions_close_category")

    app.state.jasong_actions_installed = True
    app.state.jasong_actions_status = status
    return dict(status)
