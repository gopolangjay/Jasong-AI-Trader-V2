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
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

VERSION = "6.9.4-forward-compact-actions-tp30"
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



def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except Exception:
        return None


def _pct(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round((float(numerator) / float(denominator)) * 100.0, 2)


def _forward_rows(evidence_source: Any, limit: int = 1000) -> List[Dict[str, Any]]:
    """Read V6.9.4 broker-settled evidence without the obsolete phase API.

    ForwardPrimeArchitecture exposes validator.all_rows(); older releases exposed
    phase_trade_analysis(). We prefer the forward store and retain the legacy path
    only as a compatibility fallback.
    """
    cap = max(1, min(int(limit), 2000))
    if evidence_source is None:
        return []

    validator = getattr(evidence_source, "validator", None)
    getter = getattr(validator, "all_rows", None)
    if callable(getter):
        try:
            rows = getter(limit=cap) or []
            return [dict(row) for row in rows if isinstance(row, dict)]
        except TypeError:
            try:
                rows = getter(cap) or []
                return [dict(row) for row in rows if isinstance(row, dict)]
            except Exception:
                pass
        except Exception:
            pass

    store = getattr(evidence_source, "store", None)
    getter = getattr(store, "rows", None)
    if callable(getter):
        try:
            rows = getter(limit=cap) or []
            return [dict(row) for row in rows if isinstance(row, dict)]
        except Exception:
            pass

    phase = getattr(evidence_source, "phase_trade_analysis", None)
    if callable(phase):
        try:
            payload = phase(None) or {}
            candidates = (
                payload.get("trades")
                or payload.get("settled_trades")
                or payload.get("entries")
                or []
            )
            return [dict(row) for row in candidates[:cap] if isinstance(row, dict)]
        except Exception:
            pass
    return []


def _merge_excursion_rows(broker: Any, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    tracker = getattr(broker, "_trade_excursion_tracker", None)
    merge = getattr(tracker, "merge", None)
    if not callable(merge):
        return [dict(row) for row in rows if isinstance(row, dict)]
    output: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            output.append(dict(merge(dict(row))))
        except Exception:
            output.append(dict(row))
    return output


def _take_profit_status(broker: Any) -> Dict[str, Any]:
    tracker = getattr(broker, "_trade_excursion_tracker", None)
    status = getattr(tracker, "status", None)
    if not callable(status):
        return {
            "available": False,
            "enabled": False,
            "target_pct": 30.0,
            "basis": "ENTRY_PRICE_FAVOURABLE_MOVE_PCT",
        }
    try:
        raw = status() or {}
    except Exception:
        raw = {}
    return _safe_json({
        "available": True,
        "enabled": raw.get("take_profit_execution_enabled"),
        "target_pct": raw.get("take_profit_target_pct"),
        "basis": raw.get("take_profit_basis"),
        "scope": raw.get("take_profit_scope"),
        "reached_trades": raw.get("take_profit_reached_trades"),
        "close_sent_trades": raw.get("take_profit_close_sent_trades"),
        "native_limit_attached_trades": raw.get("native_take_profit_attached_trades"),
        "primary_execution": raw.get("take_profit_primary_execution"),
        "fallback_execution": raw.get("take_profit_fallback_execution"),
        "poll_seconds": raw.get("poll_seconds"),
        "last_sync_at": raw.get("last_sync_at"),
        "last_error": raw.get("last_error"),
    })


def _compact_trade(row: Dict[str, Any]) -> Dict[str, Any]:
    return _safe_json({
        "trade_id": row.get("trade_id") or row.get("deal_id") or row.get("ig_deal_id"),
        "deal_reference": row.get("deal_reference") or row.get("ig_deal_reference"),
        "track": row.get("track") or row.get("evidence_source"),
        "category": row.get("category"),
        "market": row.get("market") or row.get("symbol"),
        "symbol": row.get("symbol") or row.get("market"),
        "strategy_id": row.get("strategy_id") or row.get("selected_strategy"),
        "direction": row.get("direction"),
        "result": row.get("broker_result") or row.get("result"),
        "entry_price": (
            row.get("entry_price")
            or row.get("entry_level")
            or row.get("broker_entry_level")
        ),
        "exit_price": (
            row.get("exit_price")
            or row.get("exit_level")
            or row.get("broker_exit_level")
        ),
        "exit_favourable_pct": (round(_favourable_exit_pct(row), 6) if _favourable_exit_pct(row) is not None else None),
        "broker_pnl": row.get("broker_pnl"),
        "r_multiple": row.get("r_multiple"),
        "r_source": row.get("r_source"),
        "opened_at": row.get("opened_at") or row.get("created_at"),
        "closed_at": row.get("closed_at") or row.get("settled_at"),
        "highest_price_since_entry": row.get("highest_price_since_entry"),
        "lowest_price_since_entry": row.get("lowest_price_since_entry"),
        "mfe": row.get("mfe"),
        "mae": row.get("mae"),
        "mfe_pct": row.get("mfe_pct"),
        "mae_pct": row.get("mae_pct"),
        "mae_abs_pct": row.get("mae_abs_pct"),
        "highest_price_vs_entry_pct": row.get("highest_price_vs_entry_pct"),
        "highest_price_as_pct_of_entry": row.get("highest_price_as_pct_of_entry"),
        "current_favourable_pct": row.get("current_favourable_pct"),
        "take_profit_enabled": row.get("take_profit_enabled"),
        "take_profit_target_pct": row.get("take_profit_target_pct"),
        "take_profit_target_price": row.get("take_profit_target_price"),
        "take_profit_reached": row.get("take_profit_reached"),
        "take_profit_reached_at": row.get("take_profit_reached_at"),
        "take_profit_trigger_price": row.get("take_profit_trigger_price"),
        "take_profit_close_state": row.get("take_profit_close_state"),
        "take_profit_closed_at": row.get("take_profit_closed_at"),
        "native_take_profit_state": row.get("native_take_profit_state"),
        "native_take_profit_level": row.get("native_take_profit_level"),
        "close_reason": row.get("close_reason"),
    })


def _compact_position(row: Dict[str, Any]) -> Dict[str, Any]:
    broker = row.get("broker") if isinstance(row.get("broker"), dict) else {}
    return _safe_json({
        "deal_id": row.get("deal_id") or row.get("ig_deal_id") or broker.get("deal_id"),
        "deal_reference": row.get("deal_reference") or row.get("ig_deal_reference"),
        "track": row.get("track") or row.get("evidence_source"),
        "category": row.get("category"),
        "market": row.get("market") or row.get("symbol") or broker.get("market_name"),
        "symbol": row.get("symbol") or row.get("market"),
        "strategy_id": row.get("strategy_id") or row.get("selected_strategy"),
        "direction": row.get("direction") or broker.get("direction"),
        "size": row.get("size") or row.get("ig_size") or broker.get("size"),
        "status": row.get("status") or row.get("broker_status"),
        "entry_price": (
            row.get("entry_price")
            or row.get("entry_level")
            or row.get("broker_entry_level")
            or broker.get("level")
        ),
        "current_price": row.get("current_price"),
        "highest_price_since_entry": row.get("highest_price_since_entry"),
        "lowest_price_since_entry": row.get("lowest_price_since_entry"),
        "mfe": row.get("mfe"),
        "mae": row.get("mae"),
        "mfe_pct": row.get("mfe_pct"),
        "mae_pct": row.get("mae_pct"),
        "mae_abs_pct": row.get("mae_abs_pct"),
        "highest_price_vs_entry_pct": row.get("highest_price_vs_entry_pct"),
        "highest_price_as_pct_of_entry": row.get("highest_price_as_pct_of_entry"),
        "current_favourable_pct": row.get("current_favourable_pct"),
        "take_profit_enabled": row.get("take_profit_enabled"),
        "take_profit_target_pct": row.get("take_profit_target_pct"),
        "take_profit_target_price": row.get("take_profit_target_price"),
        "take_profit_reached": row.get("take_profit_reached"),
        "take_profit_trigger_price": row.get("take_profit_trigger_price"),
        "take_profit_close_state": row.get("take_profit_close_state"),
        "take_profit_close_attempts": row.get("take_profit_close_attempts"),
        "take_profit_closed_at": row.get("take_profit_closed_at"),
        "native_take_profit_state": row.get("native_take_profit_state"),
        "native_take_profit_level": row.get("native_take_profit_level"),
        "price_basis": row.get("price_basis"),
        "last_observed_at": row.get("last_observed_at"),
        "opened_at": row.get("opened_at") or row.get("created_at"),
    })


def _favourable_exit_pct(row: Dict[str, Any]) -> Optional[float]:
    entry = _finite(row.get("entry_price") or row.get("entry_level") or row.get("broker_entry_level"))
    exit_price = _finite(row.get("exit_price") or row.get("exit_level") or row.get("broker_exit_level"))
    direction = str(row.get("direction") or "").upper().strip()
    if entry is None or entry <= 0 or exit_price is None:
        return None
    if direction == "BUY":
        return ((exit_price - entry) / entry) * 100.0
    if direction == "SELL":
        return ((entry - exit_price) / entry) * 100.0
    return None


def _trade_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    settled = len(rows)
    wins = 0
    losses = 0
    pnls: List[float] = []
    r_values: List[float] = []
    mfe_pcts: List[float] = []
    mae_pcts: List[float] = []
    mae_abs_pcts: List[float] = []
    r_sources: Dict[str, int] = {}
    take_profit_reached = 0
    take_profit_closed = 0
    take_profit_exit_at_or_above_target = 0
    take_profit_targets: List[float] = []
    by_strategy: Dict[str, Dict[str, Any]] = {}

    for row in rows:
        result = str(row.get("broker_result") or row.get("result") or "").upper().strip()
        if result == "WIN":
            wins += 1
        elif result == "LOSS":
            losses += 1

        pnl = _finite(row.get("broker_pnl"))
        if pnl is not None:
            pnls.append(pnl)
        r_value = _finite(row.get("r_multiple"))
        if r_value is not None:
            r_values.append(r_value)
        mfe_pct = _finite(row.get("mfe_pct"))
        mae_pct = _finite(row.get("mae_pct"))
        mae_abs_pct = _finite(row.get("mae_abs_pct"))
        if mfe_pct is not None:
            mfe_pcts.append(mfe_pct)
        if mae_pct is not None:
            mae_pcts.append(mae_pct)
        if mae_abs_pct is not None:
            mae_abs_pcts.append(mae_abs_pct)

        target_pct = _finite(row.get("take_profit_target_pct"))
        if target_pct is None:
            target_pct = 30.0
        take_profit_targets.append(target_pct)
        exit_favourable_pct = _favourable_exit_pct(row)
        reached = bool(row.get("take_profit_reached"))
        if not reached and mfe_pct is not None and mfe_pct >= target_pct:
            reached = True
        if not reached and exit_favourable_pct is not None and exit_favourable_pct >= target_pct:
            reached = True
        if reached:
            take_profit_reached += 1
        if exit_favourable_pct is not None and exit_favourable_pct >= target_pct:
            take_profit_exit_at_or_above_target += 1
        tp_state = str(row.get("take_profit_close_state") or "").upper().strip()
        close_reason = str(row.get("close_reason") or "").upper().strip()
        if tp_state in {"CLOSE_SENT", "CLOSE_VERIFIED"} or close_reason.startswith("TAKE_PROFIT_"):
            take_profit_closed += 1

        r_source = str(row.get("r_source") or "UNKNOWN")
        r_sources[r_source] = r_sources.get(r_source, 0) + 1

        strategy = str(
            row.get("strategy_id")
            or row.get("selected_strategy")
            or "UNKNOWN"
        ).upper().strip()
        bucket = by_strategy.setdefault(
            strategy,
            {"settled_trades": 0, "wins": 0, "losses": 0, "r_total": 0.0, "r_count": 0},
        )
        bucket["settled_trades"] += 1
        if result == "WIN":
            bucket["wins"] += 1
        elif result == "LOSS":
            bucket["losses"] += 1
        if r_value is not None:
            bucket["r_total"] += r_value
            bucket["r_count"] += 1

    strategy_rows: List[Dict[str, Any]] = []
    for strategy, bucket in by_strategy.items():
        count = int(bucket["settled_trades"])
        r_count = int(bucket["r_count"])
        strategy_rows.append({
            "strategy_id": strategy,
            "settled_trades": count,
            "wins": int(bucket["wins"]),
            "losses": int(bucket["losses"]),
            "win_rate_pct": _pct(int(bucket["wins"]), count),
            "avg_r": round(float(bucket["r_total"]) / r_count, 4) if r_count else None,
        })
    strategy_rows.sort(key=lambda row: int(row.get("settled_trades") or 0), reverse=True)

    pnl_count = len(pnls)
    r_count = len(r_values)
    excursion_count = sum(
        1
        for row in rows
        if _finite(row.get("mfe_pct")) is not None
        and _finite(row.get("mae_pct")) is not None
    )
    true_r_count = sum(
        1
        for row in rows
        if str(row.get("r_source") or "").upper() in {
            "BROKER_PNL_OVER_ENTRY_RISK",
            "EXPLICIT_R",
            "TRUE_R",
        }
    )

    return {
        "settled_trades": settled,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": _pct(wins, settled),
        "realized_pnl_total_known": round(sum(pnls), 4) if pnls else None,
        "realized_pnl_known_trades": pnl_count,
        "realized_pnl_missing_trades": max(0, settled - pnl_count),
        "cash_pnl_coverage_pct": _pct(pnl_count, settled),
        "cash_pnl_complete": bool(settled > 0 and pnl_count == settled),
        "total_r": round(sum(r_values), 4) if r_values else None,
        "avg_r": round(sum(r_values) / r_count, 4) if r_count else None,
        "r_coverage_pct": _pct(r_count, settled),
        "true_r_trades": true_r_count,
        "true_r_coverage_pct": _pct(true_r_count, settled),
        "r_sources": r_sources,
        "excursion_covered_trades": excursion_count,
        "excursion_coverage_pct": _pct(excursion_count, settled),
        "avg_mfe_pct": round(sum(mfe_pcts) / len(mfe_pcts), 4) if mfe_pcts else None,
        "avg_mae_pct": round(sum(mae_pcts) / len(mae_pcts), 4) if mae_pcts else None,
        "avg_mae_abs_pct": round(sum(mae_abs_pcts) / len(mae_abs_pcts), 4) if mae_abs_pcts else None,
        "best_mfe_pct": round(max(mfe_pcts), 4) if mfe_pcts else None,
        "worst_mae_pct": round(min(mae_pcts), 4) if mae_pcts else None,
        "take_profit_target_pct": round(max(take_profit_targets), 4) if take_profit_targets else 30.0,
        "take_profit_reached_trades": take_profit_reached,
        "take_profit_reached_pct": _pct(take_profit_reached, settled),
        "take_profit_closed_trades": take_profit_closed,
        "take_profit_closed_pct": _pct(take_profit_closed, settled),
        "exit_at_or_above_take_profit_trades": take_profit_exit_at_or_above_target,
        "exit_at_or_above_take_profit_pct": _pct(take_profit_exit_at_or_above_target, settled),
        "by_strategy": strategy_rows[:20],
    }


def _compact_portfolio(portfolio: Any, limit: int = 20) -> Dict[str, Any]:
    cap = max(1, min(int(limit), 50))
    try:
        rows = portfolio.positions(limit=max(cap, 100)) or []
    except Exception:
        rows = []
    positions = [dict(row) for row in rows if isinstance(row, dict)]
    open_rows = [
        row for row in positions
        if str(row.get("status") or row.get("broker_status") or "").upper() == "OPEN"
    ]
    by_category: Dict[str, int] = {}
    for row in open_rows:
        category = str(row.get("category") or "UNKNOWN").upper()
        by_category[category] = by_category.get(category, 0) + 1
    state = getattr(portfolio, "_state", {})
    return {
        "available": True,
        "execution_mode": "IG_DEMO_ONLY",
        "open_positions": len(open_rows),
        "open_by_category": by_category,
        "opens_lifetime": int((state or {}).get("opens") or 0),
        "closes_lifetime": int((state or {}).get("closes") or 0),
        "max_open_positions": int(getattr(portfolio, "max_open_positions", 0) or 0),
        "global_ig_max_positions": int(getattr(portfolio, "global_ig_max_positions", 0) or 0),
        "last_tick_at": (state or {}).get("last_tick_at"),
        "last_error": (state or {}).get("last_error"),
        "positions": [_compact_position(row) for row in open_rows[:cap]],
        "returned_positions": min(len(open_rows), cap),
        "positions_truncated": len(open_rows) > cap,
        "live_money_execution": False,
    }


def _compact_compound(compound_engine: Any, limit: int = 5) -> Dict[str, Any]:
    payload = _call_first(compound_engine, ("status",)) or {}
    if not isinstance(payload, dict):
        return {"available": False, "status": "unavailable"}
    current = payload.get("current_cycle") if isinstance(payload.get("current_cycle"), dict) else {}
    raw_positions = []
    if isinstance(current, dict) and isinstance(current.get("positions"), list):
        raw_positions = [row for row in current.get("positions") or [] if isinstance(row, dict)]
    elif isinstance(payload.get("compound_broker_positions"), list):
        raw_positions = [row for row in payload.get("compound_broker_positions") or [] if isinstance(row, dict)]
    cap = max(1, min(int(limit), 10))
    cycle = {
        "cycle_id": current.get("cycle_id") or current.get("id"),
        "cycle_number": current.get("cycle_number") or payload.get("cycle_number"),
        "status": current.get("status"),
        "started_at": current.get("started_at"),
        "starting_capital": (
            current.get("starting_capital")
            or current.get("cycle_start_capital")
            or current.get("capital_at_start")
        ),
        "target_multiple": current.get("target_multiple") or current.get("selected_target_multiple"),
        "target_profit_pct": current.get("target_profit_pct"),
        "stop_loss_pct": current.get("stop_loss_pct"),
        "positions": [_compact_position(row) for row in raw_positions[:cap]],
        "open_positions": len(raw_positions),
    } if current else None
    return _safe_json({
        "available": True,
        "version": payload.get("version") or getattr(compound_engine, "VERSION", None),
        "enabled": payload.get("enabled"),
        "status": payload.get("status"),
        "paused_reason": payload.get("paused_reason"),
        "campaign_id": payload.get("campaign_id"),
        "cycle_number": payload.get("cycle_number"),
        "current_capital": payload.get("current_capital"),
        "reserve_balance": payload.get("reserve_balance"),
        "total_harvested": payload.get("total_harvested"),
        "target_mode": payload.get("target_mode") or getattr(compound_engine, "target_mode", None),
        "current_cycle": cycle,
        "pending_elite_count": len(payload.get("pending_elite_candidates") or []),
        "last_tick_at": payload.get("last_tick_at"),
        "last_error": payload.get("last_error"),
        "live_money_execution": False,
    })


def _parse_account_payload(payload: Any, preferred_account_id: Any = None) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {"available": False}
    rows = payload.get("accounts") or []
    if not isinstance(rows, list):
        rows = []
    chosen: Optional[Dict[str, Any]] = None
    wanted = str(preferred_account_id or "").strip()
    if wanted:
        chosen = next(
            (row for row in rows if isinstance(row, dict) and str(row.get("accountId") or "") == wanted),
            None,
        )
    if chosen is None:
        chosen = next(
            (row for row in rows if isinstance(row, dict) and bool(row.get("preferred"))),
            None,
        )
    if chosen is None:
        chosen = next((row for row in rows if isinstance(row, dict)), None)
    if chosen is None:
        return {"available": False}
    balance_obj = chosen.get("balance") if isinstance(chosen.get("balance"), dict) else {}
    balance = _finite(balance_obj.get("balance"))
    open_pnl = _finite(balance_obj.get("profitLoss"))
    equity = (balance + open_pnl) if balance is not None and open_pnl is not None else None
    return _safe_json({
        "available": True,
        "account_id": chosen.get("accountId"),
        "account_name": chosen.get("accountName"),
        "account_type": chosen.get("accountType"),
        "currency": chosen.get("currency"),
        "preferred": chosen.get("preferred"),
        "balance": balance,
        "open_pnl": open_pnl,
        "equity": round(equity, 4) if equity is not None else None,
        "available_funds": balance_obj.get("available"),
        "deposit": balance_obj.get("deposit"),
    })


def _cached_account(compound_engine: Any, evidence_source: Any, broker: Any) -> Dict[str, Any]:
    broker_status = _call_first(broker, ("status",)) or {}
    preferred = broker_status.get("account_id") if isinstance(broker_status, dict) else None
    candidates: List[Any] = []
    state = getattr(compound_engine, "_state", None)
    if isinstance(state, dict):
        candidates.extend([state.get("broker_account"), state.get("broker_accounts")])
    legacy = getattr(evidence_source, "legacy_evidence_source", None)
    legacy_state = getattr(legacy, "_state", None)
    if isinstance(legacy_state, dict):
        candidates.extend([legacy_state.get("broker_account"), legacy_state.get("broker_accounts")])
    for candidate in candidates:
        if isinstance(candidate, dict):
            if isinstance(candidate.get("accounts"), list):
                parsed = _parse_account_payload(candidate, preferred)
                if parsed.get("available"):
                    parsed["source"] = "CACHED_ENGINE_ACCOUNT"
                    return parsed
            # Some engines persist the selected account directly.
            if candidate.get("accountId") or candidate.get("account_id"):
                balance_obj = candidate.get("balance") if isinstance(candidate.get("balance"), dict) else candidate
                balance = _finite(balance_obj.get("balance"))
                open_pnl = _finite(balance_obj.get("profitLoss") or balance_obj.get("profit_loss"))
                equity = (balance + open_pnl) if balance is not None and open_pnl is not None else None
                return _safe_json({
                    "available": True,
                    "account_id": candidate.get("accountId") or candidate.get("account_id"),
                    "currency": candidate.get("currency") or candidate.get("currencyIsoCode"),
                    "balance": balance,
                    "open_pnl": open_pnl,
                    "equity": round(equity, 4) if equity is not None else None,
                    "available_funds": balance_obj.get("available"),
                    "deposit": balance_obj.get("deposit"),
                    "source": "CACHED_ENGINE_ACCOUNT",
                })
    return {
        "available": False,
        "account_id": preferred,
        "source": "BROKER_STATUS_ONLY",
    }


def _open_system_positions(
    portfolio: Any,
    compound_engine: Any,
    evidence_source: Any,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    cap = max(1, min(int(limit), 50))
    merged: Dict[str, Dict[str, Any]] = {}
    anonymous = 0

    def add(row: Any, default_track: str) -> None:
        nonlocal anonymous
        if not isinstance(row, dict):
            return
        status = str(row.get("status") or row.get("broker_status") or "").upper().strip()
        if status and status not in {"OPEN", "TRADEABLE", "ACTIVE"}:
            return
        item = dict(row)
        item.setdefault("track", default_track)
        key = str(
            item.get("deal_id")
            or item.get("ig_deal_id")
            or item.get("trade_id")
            or item.get("deal_reference")
            or ""
        ).strip()
        if not key:
            anonymous += 1
            key = f"ANON_{anonymous}_{item.get('market') or item.get('symbol') or ''}"
        merged[key] = item

    try:
        for row in portfolio.positions(limit=100) or []:
            add(row, "CATEGORY")
    except Exception:
        pass

    compound_payload = _call_first(compound_engine, ("status",)) or {}
    if isinstance(compound_payload, dict):
        current = compound_payload.get("current_cycle")
        if isinstance(current, dict):
            for row in current.get("positions") or []:
                add(row, "COMPOUND")
        for row in compound_payload.get("compound_broker_positions") or []:
            add(row, "COMPOUND")

    legacy = getattr(evidence_source, "legacy_evidence_source", None)
    if legacy is not None:
        try:
            payload = _call_first(legacy, ("status",)) or {}
            mirrors = payload.get("mirrors") if isinstance(payload, dict) else None
            rows = mirrors.values() if isinstance(mirrors, dict) else mirrors if isinstance(mirrors, list) else []
            for row in rows:
                add(row, "LEARNING")
        except Exception:
            pass

    output = [_compact_position(row) for row in merged.values()]
    output.sort(
        key=lambda row: float(row.get("last_observed_at") or row.get("opened_at") or 0.0)
        if _finite(row.get("last_observed_at") or row.get("opened_at")) is not None
        else 0.0,
        reverse=True,
    )
    return output[:cap]


def _compact_runtime_diagnostics(
    app: Any,
    intelligence: Any,
    portfolio: Any,
    compound_engine: Any,
    broker: Any,
    evidence_source: Any,
) -> Dict[str, Any]:
    intelligence_state = getattr(intelligence, "_state", {})
    portfolio_state = getattr(portfolio, "_state", {})
    compound_state = getattr(compound_engine, "_state", {})
    broker_status = _call_first(broker, ("status",)) or {}
    validator = getattr(evidence_source, "validator", None)
    store = getattr(validator, "store", None) or getattr(evidence_source, "store", None)
    return _safe_json({
        "version": VERSION,
        "category": {
            "enabled": bool((intelligence_state or {}).get("enabled", True)),
            "last_scan_at": (intelligence_state or {}).get("last_scan_at"),
            "last_error": (intelligence_state or {}).get("last_error"),
        },
        "portfolio": {
            "enabled": bool(getattr(portfolio, "enabled", True)),
            "last_tick_at": (portfolio_state or {}).get("last_tick_at"),
            "last_error": (portfolio_state or {}).get("last_error"),
        },
        "compound": {
            "enabled": (compound_state or {}).get("enabled"),
            "status": (compound_state or {}).get("status"),
            "last_tick_at": (compound_state or {}).get("last_tick_at"),
            "last_error": (compound_state or {}).get("last_error"),
        },
        "broker": {
            "configured": broker_status.get("configured") if isinstance(broker_status, dict) else None,
            "connected": broker_status.get("connected") if isinstance(broker_status, dict) else None,
            "environment": broker_status.get("environment") if isinstance(broker_status, dict) else None,
            "last_error": broker_status.get("last_error") if isinstance(broker_status, dict) else None,
        },
        "forward_store": {
            "available": store is not None,
            "path": getattr(store, "path", None),
        },
        "mcp_install_error": getattr(app.state, "jasong_mcp_install_error", None),
        "actions_install_error": getattr(app.state, "jasong_actions_install_error", None),
        "live_money_execution": False,
    })

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

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Jasong AI Trader V6.9.4 Forward Actions",
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
            },
            "schemas": {
                "ConfirmRequest": {
                    "type": "object",
                    "required": ["confirm"],
                    "properties": {
                        "confirm": {
                            "type": "boolean",
                            "description": (
                                "Set true only after the user explicitly asked "
                                "to perform this write."
                            ),
                        }
                    },
                    "additionalProperties": False,
                },
                "RunScanRequest": {
                    "type": "object",
                    "required": ["confirm"],
                    "properties": {
                        "confirm": {
                            "type": "boolean",
                            "description": (
                                "Set true only after the user explicitly asked "
                                "to perform this write."
                            ),
                        },
                        "category": {
                            "type": "string",
                            "enum": [
                                "FOREX",
                                "INDICES",
                                "CRYPTO",
                                "METALS",
                                "ENERGY",
                                "SHARES",
                            ],
                            "description": (
                                "Optional market category. Omit to scan all "
                                "categories."
                            ),
                        },
                    },
                    "additionalProperties": False,
                },
                "CategoryAutotradeRequest": {
                    "type": "object",
                    "required": ["confirm", "enabled"],
                    "properties": {
                        "confirm": {
                            "type": "boolean",
                            "description": (
                                "Set true only after the user explicitly asked "
                                "to perform this write."
                            ),
                        },
                        "enabled": {"type": "boolean"},
                    },
                    "additionalProperties": False,
                },
                "SymbolWriteRequest": {
                    "type": "object",
                    "required": ["confirm", "symbol"],
                    "properties": {
                        "confirm": {
                            "type": "boolean",
                            "description": (
                                "Set true only after the user explicitly asked "
                                "to perform this write."
                            ),
                        },
                        "symbol": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 80,
                        },
                    },
                    "additionalProperties": False,
                },
                "ClosePositionRequest": {
                    "type": "object",
                    "required": ["confirm", "deal_id"],
                    "properties": {
                        "confirm": {
                            "type": "boolean",
                            "description": (
                                "Set true only after the user explicitly asked "
                                "to perform this write."
                            ),
                        },
                        "deal_id": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 120,
                        },
                    },
                    "additionalProperties": False,
                },
            },
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
                    "Get compact Jasong system status",
                    "Read a compact system health/status summary. For trading performance, prefer /assistant/performance.",
                )
            },
            "/assistant/performance": {
                "get": get_op(
                    "getTradingPerformance",
                    "Get trustworthy trading performance",
                    "PREFERRED endpoint for performance questions. Returns compact broker-settled wins/losses, win rate, known realized P&L coverage, R metrics, account snapshot, open trades, and MFE/MAE without oversized engine payloads.",
                    [
                        {"name": "limit", "in": "query", "required": False, "schema": {"type": "integer", "minimum": 1, "maximum": 25, "default": 12}}
                    ],
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
                    "Get compact Category portfolio",
                    "Read compact Category IG DEMO positions, capacity and MFE/MAE fields without the full portfolio ledger.",
                    [{"name": "limit", "in": "query", "required": False, "schema": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20}}],
                )
            },
            "/assistant/compound": {
                "get": get_op(
                    "getCompoundStatus",
                    "Get compact Compound status",
                    "Read the current 80/20 Compound cycle, capital, reserve and up to five current basket positions. Large historical cycle/candidate ledgers are omitted.",
                )
            },
            "/assistant/ig-demo": {
                "get": get_op(
                    "getIGDemoStatus",
                    "Get compact IG DEMO broker status",
                    "Read a compact IG DEMO account and open-position snapshot. Credentials/session tokens and oversized raw payloads are omitted.",
                    [{"name": "limit", "in": "query", "required": False, "schema": {"type": "integer", "minimum": 1, "maximum": 30, "default": 15}}],
                )
            },
            "/assistant/trades": {
                "get": get_op(
                    "getTradeHistory",
                    "Get compact broker-settled trade history",
                    "Read V6.9.4 broker-settled forward trades directly from the forward-validation store. phase_id remains accepted only for legacy compatibility.",
                    [
                        {"name": "phase_id", "in": "query", "required": False, "schema": {"type": "integer", "minimum": 0, "default": 0}},
                        {"name": "limit", "in": "query", "required": False, "schema": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20}},
                        {"name": "strategy", "in": "query", "required": False, "schema": {"type": "string"}}
                    ],
                )
            },
            "/assistant/diagnostics": {
                "get": get_op(
                    "getDiagnostics",
                    "Get compact runtime diagnostics",
                    "Read only health/error fields from each engine. Full state objects are deliberately excluded to stay connector-safe.",
                )
            },
            "/assistant/write/run-scan": {
                "post": post_op(
                    "runMarketScan",
                    "Run market scan now",
                    "Runs the specialist intelligence scan. This can change rankings and may subsequently allow normal Category autotrading to act.",
                    {"$ref": "#/components/schemas/RunScanRequest"},
                )
            },
            "/assistant/write/full-refresh": {
                "post": post_op(
                    "forceFullRefresh",
                    "Force full 40-market refresh",
                    "Starts a FORCE_ALL optimization/evidence refresh across all 40 markets.",
                    {"$ref": "#/components/schemas/ConfirmRequest"},
                )
            },
            "/assistant/write/category-autotrade": {
                "post": post_op(
                    "setCategoryAutotrade",
                    "Enable or disable Category autotrading",
                    "Changes Category autotrading for the current running process only. Render environment settings remain restart-time authority.",
                    {"$ref": "#/components/schemas/CategoryAutotradeRequest"},
                )
            },
            "/assistant/write/open-qualified": {
                "post": post_op(
                    "openQualifiedCategoryPosition",
                    "Open a currently qualified Category position",
                    "Opens only a market that is currently standard-eligible and still passes Category portfolio/global exposure controls. The caller cannot provide EPIC, direction, size, or bypass flags.",
                    {"$ref": "#/components/schemas/SymbolWriteRequest"},
                )
            },
            "/assistant/write/close-category-position": {
                "post": post_op(
                    "closeCategoryPosition",
                    "Close a Category IG DEMO position",
                    "Closes only an open JSCAT-owned Category position by deal ID. It cannot close manual, Compound, learning or live-money positions.",
                    {"$ref": "#/components/schemas/ClosePositionRequest"},
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
        ranked = [_compact_market(row) for row in _ranked_rows(intelligence)]
        states: Dict[str, int] = {"PRIME": 0, "STRONG": 0, "WATCH": 0}
        for row in ranked:
            state_name = str(row.get("state") or "WATCH").upper()
            states[state_name] = states.get(state_name, 0) + 1
        forward_rows = _forward_rows(evidence_source, limit=1000)
        forward_rows = _merge_excursion_rows(broker, forward_rows)
        _audit("system")
        return _safe_json({
            "version": VERSION,
            "execution_mode": "IG_DEMO_ONLY",
            "category": {
                "ranked_markets": len(ranked),
                "states": states,
            },
            "portfolio": _compact_portfolio(portfolio, limit=5),
            "compound": _compact_compound(compound_engine, limit=5),
            "broker": _call_first(broker, ("status",)) or {},
            "take_profit": _take_profit_status(broker),
            "forward_performance": _trade_summary(forward_rows),
            "actions": {
                "enabled": status.get("enabled"),
                "read_access": status.get("read_access"),
                "write_access": bool(_write_enabled()),
            },
            "live_money_execution": False,
        })

    async def performance(request: Request, limit: int = 12) -> Dict[str, Any]:
        _require_key(request)
        cap = max(1, min(int(limit), 25))
        rows = _forward_rows(evidence_source, limit=1000)
        rows = _merge_excursion_rows(broker, rows)
        summary = _trade_summary(rows)
        open_positions = _open_system_positions(
            portfolio,
            compound_engine,
            evidence_source,
            limit=cap,
        )
        account = _cached_account(compound_engine, evidence_source, broker)
        # Readiness is explicit: broker-settled W/L and R can be evaluated even
        # while historical cash-P&L coverage is incomplete. Cash totals must not
        # be presented as complete unless cash_pnl_complete is true.
        assessment_ready = bool(summary.get("settled_trades", 0) > 0)
        _audit(
            "performance",
            settled=summary.get("settled_trades"),
            open_positions=len(open_positions),
        )
        return _safe_json({
            "version": VERSION,
            "available": assessment_ready,
            "authority": "BROKER_SETTLED_FORWARD_ONLY",
            "performance_assessment_ready": assessment_ready,
            "performance": summary,
            "account": account,
            "open_positions": open_positions,
            "open_positions_returned": len(open_positions),
            "take_profit": _take_profit_status(broker),
            "recent_settled_trades": [_compact_trade(row) for row in rows[:cap]],
            "recent_settled_trades_returned": min(len(rows), cap),
            "interpretation_guard": {
                "wins_losses_win_rate": "BROKER_SETTLED_FORWARD_STORE",
                "cash_pnl": (
                    "COMPLETE"
                    if summary.get("cash_pnl_complete")
                    else "PARTIAL_DO_NOT_TREAT_AS_TOTAL_LIFETIME_CASH_PNL"
                ),
                "r_metrics": "USE_R_SOURCE_COUNTS_TO_DISTINGUISH_TRUE_R_FROM_BINARY_FALLBACK",
                "mfe_mae": "BROKER_OBSERVED_EXIT_SIDE_QUOTES; REST-observed, not tick-perfect",
                "take_profit": "30% favourable price move from entry; IG DEMO native limitLevel is primary, server-observed close is fallback",
            },
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
                "historical_validation_mode": "INFORMATIONAL_ONLY",
                "historical_execution_veto": False,
                "prime_authority": "BROKER_SETTLED_FORWARD_ONLY",
                "forward_min_settled_trades": getattr(getattr(evidence_source, "validator", None), "config", None).min_settled_trades_for_prime if getattr(getattr(evidence_source, "validator", None), "config", None) is not None else 12,
                "forward_min_profit_factor": getattr(getattr(evidence_source, "validator", None), "config", None).min_profit_factor if getattr(getattr(evidence_source, "validator", None), "config", None) is not None else 1.20,
                "forward_min_expectancy_r": getattr(getattr(evidence_source, "validator", None), "config", None).min_expectancy_r if getattr(getattr(evidence_source, "validator", None), "config", None) is not None else 0.05,
                "forward_min_win_rate_pct": (getattr(getattr(evidence_source, "validator", None), "config", None).min_win_rate * 100.0) if getattr(getattr(evidence_source, "validator", None), "config", None) is not None else 45.0,
                "forward_min_bootstrap_pct": (getattr(getattr(evidence_source, "validator", None), "config", None).min_bootstrap_prob_positive_expectancy * 100.0) if getattr(getattr(evidence_source, "validator", None), "config", None) is not None else 75.0,
                "forward_max_drawdown_r": getattr(getattr(evidence_source, "validator", None), "config", None).max_drawdown_r if getattr(getattr(evidence_source, "validator", None), "config", None) is not None else 6.0,
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
                "historical_validation": {
                    "mode": "INFORMATIONAL_ONLY",
                    "holdout_wr_pct": row.get("historical_win_rate_pct"),
                    "historical_profit_factor": row.get("historical_profit_factor"),
                    "historical_trades": row.get("historical_trades"),
                    "walk_forward_pass": row.get("walk_forward_pass"),
                },
                "forward_validation": row.get("forward_validation"),
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

    async def category_portfolio(request: Request, limit: int = 20) -> Dict[str, Any]:
        _require_key(request)
        payload = _compact_portfolio(portfolio, limit=limit)
        _audit("portfolio", open_positions=payload.get("open_positions"))
        return payload

    async def compound(request: Request) -> Dict[str, Any]:
        _require_key(request)
        payload = _compact_compound(compound_engine, limit=5)
        _audit("compound", available=payload.get("available"))
        return payload

    async def ig_demo(request: Request, limit: int = 15) -> Dict[str, Any]:
        _require_key(request)
        cap = max(1, min(int(limit), 30))
        configured = bool(getattr(broker, "configured", lambda: False)())
        broker_status = _call_first(broker, ("status",)) or {}
        accounts_payload = _call_first(broker, ("accounts",)) if configured else None
        positions_payload = _call_first(broker, ("positions",)) if configured else None
        account = _parse_account_payload(
            accounts_payload,
            broker_status.get("account_id") if isinstance(broker_status, dict) else None,
        )
        raw_positions = []
        if isinstance(positions_payload, dict):
            for item in positions_payload.get("positions", []) or []:
                if not isinstance(item, dict):
                    continue
                position = item.get("position") or {}
                market = item.get("market") or {}
                if not isinstance(position, dict) or not isinstance(market, dict):
                    continue
                raw_positions.append({
                    "deal_id": position.get("dealId"),
                    "deal_reference": position.get("dealReference"),
                    "direction": position.get("direction"),
                    "size": position.get("size") if position.get("size") is not None else position.get("dealSize"),
                    "entry_price": position.get("level"),
                    "epic": market.get("epic") or position.get("epic"),
                    "market": market.get("instrumentName") or market.get("marketName"),
                    "market_status": market.get("marketStatus"),
                    "bid": market.get("bid"),
                    "offer": market.get("offer"),
                })
        _audit("ig_demo", configured=configured, open_positions=len(raw_positions))
        return _safe_json({
            "version": VERSION,
            "status": broker_status,
            "account": account,
            "open_positions_count": len(raw_positions),
            "open_positions": raw_positions[:cap],
            "positions_truncated": len(raw_positions) > cap,
            "live_money_execution": False,
        })

    async def trades(
        request: Request,
        phase_id: int = 0,
        limit: int = 20,
        strategy: str = "",
    ) -> Dict[str, Any]:
        _require_key(request)
        cap = max(1, min(int(limit), 50))
        all_rows = _forward_rows(evidence_source, limit=1000)
        all_rows = _merge_excursion_rows(broker, all_rows)
        wanted_strategy = str(strategy or "").upper().strip()
        rows = all_rows
        if wanted_strategy:
            rows = [
                row for row in rows
                if str(row.get("strategy_id") or row.get("selected_strategy") or "").upper().strip()
                == wanted_strategy
            ]
        if rows or getattr(evidence_source, "validator", None) is not None:
            _audit(
                "trades",
                phase_id=phase_id,
                strategy=wanted_strategy,
                available=True,
                count=len(rows),
            )
            return _safe_json({
                "version": VERSION,
                "available": True,
                "authority": "BROKER_SETTLED_FORWARD_ONLY",
                "source": "FORWARD_VALIDATION_STORE",
                "phase_id_ignored_for_v694": int(phase_id) if int(phase_id) > 0 else None,
                "strategy_filter": wanted_strategy or None,
                "summary": _trade_summary(rows),
                "count": len(rows),
                "returned": min(len(rows), cap),
                "trades": [_compact_trade(row) for row in rows[:cap]],
                "truncated": len(rows) > cap,
                "live_money_execution": False,
            })

        # Legacy compatibility fallback only.
        phase = getattr(evidence_source, "phase_trade_analysis", None)
        if callable(phase):
            try:
                payload = phase(int(phase_id) if int(phase_id) > 0 else None)
                _audit("trades", phase_id=phase_id, available=True, source="LEGACY_PHASE")
                return _safe_json(payload)
            except Exception as exc:
                return {
                    "version": VERSION,
                    "available": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
        _audit("trades", phase_id=phase_id, available=False)
        return {
            "version": VERSION,
            "available": False,
            "error": "No forward-validation store or legacy phase evidence source is available",
        }

    async def diagnostics(request: Request) -> Dict[str, Any]:
        _require_key(request)
        _audit("diagnostics")
        return _compact_runtime_diagnostics(
            app,
            intelligence,
            portfolio,
            compound_engine,
            broker,
            evidence_source,
        )

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
    app.add_api_route("/assistant/performance", performance, methods=["GET"], name="jasong_actions_performance")
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
