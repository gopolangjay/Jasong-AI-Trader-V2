from __future__ import annotations

import copy
import os
import threading
import time
import uuid
from collections import Counter
from typing import Any, Dict, List, Optional

from category_execution_engine import CategoryExecutionEngine
from category_strategy_engine import CategoryStrategyEngine
from ig_demo_broker import IGDemoBroker


VERSION = "6.9.4-execution-reliability-v4"


def _now() -> float:
    return time.time()


def _candidate_key(candidate: Dict[str, Any]) -> str:
    category = str(candidate.get("category") or "UNKNOWN").upper().strip()
    symbol = str(
        candidate.get("symbol")
        or candidate.get("key")
        or candidate.get("market")
        or "UNKNOWN"
    ).upper().strip()
    direction = str(candidate.get("direction") or "NONE").upper().strip()
    return f"{category}|{symbol}|{direction}"


def _classify_broker_error(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}".upper()
    markers = (
        ("SIZE_INCREMENT", "SIZE_INCREMENT"),
        ("MARKET_CLOSED", "MARKET_CLOSED"),
        ("MARKETNOTOPEN", "MARKET_CLOSED"),
        ("MARKET NOT OPEN", "MARKET_CLOSED"),
        ("ALLOWANCE", "IG_ALLOWANCE"),
        ("RATE LIMIT", "RATE_LIMIT"),
        ("TOO MANY REQUEST", "RATE_LIMIT"),
        ("INSUFFICIENT", "INSUFFICIENT_FUNDS"),
        ("NOT_ENOUGH", "INSUFFICIENT_FUNDS"),
        ("MARGIN", "MARGIN"),
        ("INVALID_LEVEL", "INVALID_LEVEL"),
        ("LEVEL", "PRICE_LEVEL"),
        ("SESSION", "SESSION"),
        ("TOKEN", "SESSION"),
        ("401", "SESSION"),
        ("403", "IG_REJECTED"),
        ("REJECT", "IG_REJECTED"),
        ("TIMEOUT", "TIMEOUT"),
        ("NETWORK", "NETWORK"),
    )
    for marker, label in markers:
        if marker in text:
            return label
    return "BROKER_ERROR"


def _compact_error(exc: Exception, limit: int = 360) -> str:
    text = f"{type(exc).__name__}: {exc}".replace("\n", " ").strip()
    return text[:limit]


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _patch_ig_demo_marker() -> None:
    # ig_demo_broker.py already hard-codes the DEMO URL, but its adaptive
    # SIZE_INCREMENT branch references self.demo. Older builds never created
    # that attribute, so a normal IG size rejection could abort the retry path.
    # A class-level True is sufficient and cannot enable live execution because
    # IGDemoBroker.BASE_URL itself is the DEMO gateway.
    if not hasattr(IGDemoBroker, "demo"):
        IGDemoBroker.demo = True  # type: ignore[attr-defined]



_ORIGINAL_IG_POSITIONS = IGDemoBroker.positions
_ORIGINAL_IG_OPEN_EPIC_POSITION = IGDemoBroker.open_epic_position
_ORIGINAL_IG_CLOSE_POSITION = IGDemoBroker.close_position


def _positions_cache_seconds() -> float:
    try:
        value = float(os.getenv("IG_DEMO_POSITIONS_CACHE_SECONDS", "5"))
    except Exception:
        value = 5.0
    return max(0.5, min(15.0, value))


def _invalidate_ig_positions_cache(broker: Any) -> None:
    try:
        with getattr(broker, "_jasong_positions_cache_lock"):
            broker._jasong_positions_cache_payload = None
            broker._jasong_positions_cache_at = 0.0
    except Exception:
        try:
            broker._jasong_positions_cache_payload = None
            broker._jasong_positions_cache_at = 0.0
        except Exception:
            pass



def _positions_stale_fallback_seconds() -> float:
    try:
        value = float(
            os.getenv("IG_DEMO_POSITIONS_STALE_FALLBACK_SECONDS", "30")
        )
    except Exception:
        value = 30.0
    return max(5.0, min(60.0, value))


def _ig_nontrading_pressure(self: IGDemoBroker) -> tuple[int, int]:
    """Return current locally-observed non-trading usage and configured ceiling."""
    limit = int(getattr(self, "nontrading_rpm", 20) or 20)
    count = 0
    lock = getattr(self, "_rate_lock", None)
    queue = getattr(self, "_nontrading_times", None)
    try:
        if lock is not None:
            with lock:
                now = _now()
                if queue is not None:
                    while queue and now - queue[0] >= 60.0:
                        queue.popleft()
                    count = len(queue)
        elif queue is not None:
            count = len(queue)
    except Exception:
        count = 0
    return count, max(1, limit)


def _cached_ig_positions(self: IGDemoBroker) -> Dict[str, Any]:
    """Coalesce short bursts of identical IG position reads.

    Category reconciliation, Category status, MFE/MAE observation and mobile
    reporting can all ask for positions within the same few seconds. Reusing one
    successful snapshot avoids spending broker REST allowance on duplicate reads.
    Order writes invalidate this cache immediately.
    """
    lock = getattr(self, "_jasong_positions_cache_lock", None)
    if lock is None:
        lock = threading.RLock()
        self._jasong_positions_cache_lock = lock
    ttl = _positions_cache_seconds()
    now = _now()
    with lock:
        payload = getattr(self, "_jasong_positions_cache_payload", None)
        cached_at = float(getattr(self, "_jasong_positions_cache_at", 0.0) or 0.0)
        age = max(0.0, now - cached_at) if cached_at > 0 else float("inf")
        if isinstance(payload, dict) and age <= ttl:
            self._jasong_positions_cache_hits = int(
                getattr(self, "_jasong_positions_cache_hits", 0) or 0
            ) + 1
            self._jasong_positions_cache_last_mode = "FRESH"
            return copy.deepcopy(payload)

        # The broker wrapper deliberately rate-limits authenticated GET traffic.
        # If that local queue is already near its ceiling, a recent positions
        # snapshot is safer for reconciliation than blocking the execution loop
        # for repeated 60-second allowance waits. Order writes invalidate this
        # cache immediately; broker-side TP closure can therefore be delayed only
        # by this bounded stale-fallback window.
        pressure_count, pressure_limit = _ig_nontrading_pressure(self)
        stale_limit = _positions_stale_fallback_seconds()
        near_limit = pressure_count >= max(1, pressure_limit - 2)
        if (
            isinstance(payload, dict)
            and age <= stale_limit
            and near_limit
        ):
            self._jasong_positions_cache_stale_hits = int(
                getattr(self, "_jasong_positions_cache_stale_hits", 0) or 0
            ) + 1
            self._jasong_positions_cache_last_mode = "STALE_RATE_PRESSURE"
            return copy.deepcopy(payload)

    payload = _ORIGINAL_IG_POSITIONS(self)
    with lock:
        self._jasong_positions_cache_payload = copy.deepcopy(payload)
        self._jasong_positions_cache_at = _now()
        self._jasong_positions_cache_misses = int(
            getattr(self, "_jasong_positions_cache_misses", 0) or 0
        ) + 1
        self._jasong_positions_cache_last_mode = "LIVE_IG"
    return payload


def _open_epic_position_invalidate_cache(self: IGDemoBroker, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    try:
        return _ORIGINAL_IG_OPEN_EPIC_POSITION(self, *args, **kwargs)
    finally:
        _invalidate_ig_positions_cache(self)


def _close_position_invalidate_cache(self: IGDemoBroker, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    try:
        return _ORIGINAL_IG_CLOSE_POSITION(self, *args, **kwargs)
    finally:
        _invalidate_ig_positions_cache(self)


def _patch_ig_positions_cache() -> None:
    if getattr(IGDemoBroker, "_jasong_positions_cache_patch", False):
        return
    IGDemoBroker.positions = _cached_ig_positions
    IGDemoBroker.open_epic_position = _open_epic_position_invalidate_cache
    IGDemoBroker.close_position = _close_position_invalidate_cache
    IGDemoBroker._jasong_positions_cache_patch = True

_ORIGINAL_CATEGORY_RECONCILE = CategoryExecutionEngine._reconcile
_ORIGINAL_CATEGORY_OPEN_CANDIDATE = CategoryExecutionEngine._open_candidate
_ORIGINAL_CATEGORY_TICK = CategoryExecutionEngine.tick
_ORIGINAL_CATEGORY_STATUS = CategoryExecutionEngine.status
_ORIGINAL_STRATEGY_LOOP = CategoryStrategyEngine._loop
_PATCH_LOCK = threading.RLock()
_INSTALLED = False


def _execution_health_state(engine: CategoryExecutionEngine) -> Dict[str, Any]:
    state = engine._state.setdefault("execution_reliability", {})
    state["version"] = VERSION
    state.setdefault("candidate_error_cooldowns", {})
    state.setdefault("recent_errors", [])
    state.setdefault("recent_blockers", {})
    state.setdefault("total_candidate_errors", 0)
    state.setdefault("total_isolated_candidate_attempts", 0)
    state.setdefault("total_isolated_candidate_opens", 0)
    state.setdefault("tick_count", 0)
    return state




def _norm_key(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _cached_evaluation_tag_index(intelligence: Any) -> Dict[str, List[str]]:
    """Build exposure-tag lookups directly from cached strategy evaluations.

    This deliberately does NOT call intelligence.candidates() or forward
    validator metrics. Exposure tagging must be cheap enough to use inside the
    30-second execution loop.
    """
    state = getattr(intelligence, "_state", {}) or {}
    evaluations = state.get("evaluations") if isinstance(state, dict) else {}
    if not isinstance(evaluations, dict):
        evaluations = {}

    index: Dict[str, List[str]] = {}
    for raw in evaluations.values():
        if not isinstance(raw, dict):
            continue
        tags = [str(x) for x in (raw.get("exposure_tags") or []) if str(x or "")]
        if not tags:
            continue
        epic = str(raw.get("ig_epic") or "").upper().strip()
        symbol = _norm_key(raw.get("symbol") or raw.get("key"))
        market = _norm_key(raw.get("market") or raw.get("name"))
        if epic:
            index["E:" + epic] = list(tags)
        if symbol:
            index["S:" + symbol] = list(tags)
        if market:
            index["S:" + market] = list(tags)
    return index


def _make_light_external_positions_source(
    *,
    broker: Any,
    intelligence: Any,
) -> Any:
    """Return a broker-position source with no forward-ranking dependency."""
    def _source() -> List[Dict[str, Any]]:
        try:
            payload = broker.positions() or {}
        except Exception:
            return []

        tag_index = _cached_evaluation_tag_index(intelligence)
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

            epic = str(
                market.get("epic")
                or position.get("epic")
                or ""
            ).upper().strip()
            market_name = str(
                market.get("instrumentName")
                or market.get("marketName")
                or ""
            )
            symbol_key = _norm_key(market_name)
            tags = tag_index.get(
                "E:" + epic,
                tag_index.get("S:" + symbol_key, []),
            )

            if ref.startswith("JSCMP_"):
                track = "COMPOUND"
            elif ref.startswith(
                ("JASONG_", "JSBND_", "JSLRN_", "JSELT_")
            ):
                track = "JASONG_LEARNING"
            else:
                track = "EXTERNAL_MANUAL"

            rows.append(
                {
                    "track": track,
                    "deal_id": position.get("dealId"),
                    "deal_reference": position.get("dealReference"),
                    "epic": epic,
                    "market_name": market_name,
                    "direction": str(position.get("direction") or "").upper(),
                    "size": (
                        position.get("size")
                        if position.get("size") is not None
                        else position.get("dealSize")
                    ),
                    "market_status": market.get("marketStatus"),
                    "exposure_tags": list(tags),
                }
            )
        return rows

    return _source


def _optimized_category_reconcile(
    self: CategoryExecutionEngine,
) -> List[Dict[str, Any]]:
    """Reconcile with one broker snapshot + one external snapshot per tick.

    The original implementation recalculated external positions once for every
    tracked open Category position. That external source itself performed full
    forward rankings, so a simple reconciliation could expand into several
    expensive validator/ranking passes and repeated broker GETs.
    """
    health = _execution_health_state(self)
    health["phase"] = "RECONCILE_BROKER_POSITIONS"
    health["phase_started_at"] = _now()

    broker_payload = self.broker.positions()
    broker_rows = self._broker_rows(broker_payload)
    by_deal = {
        str(row.get("deal_id") or ""): row
        for row in broker_rows
    }

    health["phase"] = "RECONCILE_EXTERNAL_POSITIONS"
    external = self._external_positions()
    self._jasong_last_external_positions = [
        dict(row) for row in external if isinstance(row, dict)
    ]
    self._jasong_last_external_positions_at = _now()

    tracked = self._state.setdefault("positions", [])
    now = _now()
    for item in tracked:
        if item.get("status") != "OPEN":
            continue
        deal_id = str(item.get("deal_id") or "")
        broker_row = by_deal.get(deal_id)
        if broker_row:
            item["broker"] = broker_row
            item["last_seen_at"] = now
            item["dual_track"] = self._is_dual_track(
                item.get("epic"),
                self._jasong_last_external_positions,
            )
        else:
            item["status"] = "CLOSED_RECONCILED"
            item["closed_at"] = now
            self._state["closes"] = int(self._state.get("closes") or 0) + 1
            try:
                self._journal(
                    "CLOSE_RECONCILED",
                    deal_id=deal_id,
                    symbol=item.get("symbol"),
                    category=item.get("category"),
                )
            except Exception:
                pass

    self._jasong_last_broker_rows = [dict(row) for row in broker_rows]
    self._jasong_last_broker_rows_at = now
    health["last_reconcile_completed_at"] = _now()
    health["last_reconcile_broker_positions"] = len(broker_rows)
    health["last_reconcile_external_positions"] = len(
        self._jasong_last_external_positions
    )
    return broker_rows


def _cached_external_for_tick(
    self: CategoryExecutionEngine,
    *,
    max_age: float = 15.0,
) -> List[Dict[str, Any]]:
    rows = getattr(self, "_jasong_last_external_positions", None)
    at = _to_float(
        getattr(self, "_jasong_last_external_positions_at", 0.0),
        0.0,
    )
    if isinstance(rows, list) and (_now() - at) <= max_age:
        return [dict(row) for row in rows if isinstance(row, dict)]
    return self._external_positions()


def _category_downsize_candidates(
    broker: Any,
    *,
    requested_size: float,
    minimum_size: float,
    increment: float,
) -> List[float]:
    """Return smaller valid DEMO sizes after an explicit INSUFFICIENT_FUNDS reject.

    This never increases exposure and never retries below IG's published
    minimum. It is deliberately rejection-driven rather than guessing margin.
    """
    requested = max(0.0, float(requested_size or 0.0))
    minimum = max(0.0, float(minimum_size or 0.0))
    step = max(0.0, float(increment or 0.0))

    if requested <= minimum + 1e-12:
        return []

    normalise = getattr(broker, "_normalise_deal_size", None)

    def valid(value: float) -> float:
        raw = max(minimum, float(value))
        if callable(normalise):
            try:
                return float(
                    normalise(
                        raw,
                        minimum_size=minimum,
                        increment=step,
                    )
                )
            except Exception:
                pass
        return raw

    # Prefer a meaningful reduction instead of many near-identical rejects.
    raw_candidates = [
        requested * 0.50,
        requested * 0.25,
        minimum,
    ]
    values: List[float] = []
    for raw in raw_candidates:
        value = valid(raw)
        if value >= requested - 1e-12:
            continue
        if value < minimum - 1e-12:
            continue
        if value not in values:
            values.append(value)

    # Larger reduced size first, minimum last.
    values.sort(reverse=True)
    if minimum > 0 and minimum < requested - 1e-12:
        min_valid = valid(minimum)
        if min_valid not in values and min_valid < requested - 1e-12:
            values.append(min_valid)
    return values[:3]


def _adaptive_category_open_candidate(
    self: CategoryExecutionEngine,
    candidate: Dict[str, Any],
    external: List[Dict[str, Any]],
) -> None:
    """Open one Category trade with safe DEMO-only affordability downshift.

    The normal Category risk/cap/duplicate gates are preserved. The first order
    uses the existing requested Category size. Only after IG explicitly returns
    INSUFFICIENT_FUNDS do we try smaller valid sizes, never below IG minimum.
    """
    allowed, reason = self._may_open(candidate, external)
    if not allowed:
        return

    category = str(candidate.get("category") or "UNK").upper()
    epic = str(candidate.get("ig_epic") or "").strip()
    direction = str(candidate.get("direction") or "").upper().strip()
    minimum = max(0.0, _to_float(candidate.get("ig_min_deal_size"), 0.0))
    requested = max(float(self.default_size), minimum)

    base_ref = f"JSCAT_{category[:3]}_{uuid.uuid4().hex[:16].upper()}"[:30]
    attempts: List[Dict[str, Any]] = []

    def attempt(size: float, index: int) -> Dict[str, Any]:
        ref = (
            base_ref
            if index == 0
            else f"JSCAT_{category[:3]}_R{index}_{uuid.uuid4().hex[:13].upper()}"[:30]
        )
        started = _now()
        try:
            result = self.broker.open_epic_position(
                epic=epic,
                direction=direction,
                size=float(size),
                deal_reference=ref,
            )
            attempts.append(
                {
                    "size": float(size),
                    "result": "OPENED",
                    "at": started,
                    "deal_id": result.get("dealId"),
                }
            )
            return result
        except Exception as exc:
            classification = _classify_broker_error(exc)
            attempts.append(
                {
                    "size": float(size),
                    "result": classification,
                    "at": started,
                    "error": _compact_error(exc, 220),
                }
            )
            if classification != "INSUFFICIENT_FUNDS":
                raise
            raise

    health = _execution_health_state(self)
    health["last_size_plan"] = {
        "candidate": _candidate_key(candidate),
        "category": category,
        "symbol": candidate.get("symbol"),
        "requested_size": requested,
        "minimum_size": minimum,
        "attempts": attempts,
        "adaptive_downsize_enabled": True,
    }

    result: Optional[Dict[str, Any]] = None
    first_error: Optional[Exception] = None

    try:
        result = attempt(requested, 0)
    except Exception as exc:
        if _classify_broker_error(exc) != "INSUFFICIENT_FUNDS":
            health["last_size_plan"]["attempts"] = list(attempts)
            raise
        first_error = exc

        # Resolve current dealing rules only after a definitive funds reject.
        details: Dict[str, Any] = {}
        try:
            details = self.broker.market_details(epic) or {}
        except Exception:
            details = {}

        min_fn = getattr(self.broker, "_min_deal_size", None)
        inc_fn = getattr(self.broker, "_deal_size_increment", None)
        if callable(min_fn):
            try:
                minimum = max(minimum, float(min_fn(details) or 0.0))
            except Exception:
                pass
        increment = minimum
        if callable(inc_fn):
            try:
                increment = max(0.0, float(inc_fn(details) or 0.0))
            except Exception:
                increment = minimum

        retry_sizes = _category_downsize_candidates(
            self.broker,
            requested_size=requested,
            minimum_size=minimum,
            increment=increment,
        )
        health["last_size_plan"].update(
            {
                "minimum_size": minimum,
                "size_increment": increment,
                "retry_sizes": list(retry_sizes),
            }
        )

        if not retry_sizes:
            health["last_size_plan"]["final_result"] = (
                "MINIMUM_DEAL_UNAFFORDABLE"
                if requested <= minimum + 1e-12
                else "NO_VALID_SMALLER_SIZE"
            )
            health["last_size_plan"]["attempts"] = list(attempts)
            raise first_error

        last_error: Exception = first_error
        for idx, retry_size in enumerate(retry_sizes, start=1):
            try:
                result = attempt(retry_size, idx)
                health["last_size_plan"]["final_result"] = "OPENED_AFTER_DOWNSIZE"
                health["last_size_plan"]["final_size"] = result.get("size") or retry_size
                health["last_size_plan"]["attempts"] = list(attempts)
                health["adaptive_size_successes"] = int(
                    health.get("adaptive_size_successes") or 0
                ) + 1
                break
            except Exception as exc:
                last_error = exc
                if _classify_broker_error(exc) != "INSUFFICIENT_FUNDS":
                    health["last_size_plan"]["final_result"] = _classify_broker_error(exc)
                    health["last_size_plan"]["attempts"] = list(attempts)
                    raise
                continue

        if result is None:
            health["last_size_plan"]["final_result"] = "MINIMUM_DEAL_UNAFFORDABLE"
            health["last_size_plan"]["attempts"] = list(attempts)
            health["minimum_deal_unaffordable_count"] = int(
                health.get("minimum_deal_unaffordable_count") or 0
            ) + 1
            raise last_error

    if not isinstance(result, dict):
        raise RuntimeError("IG DEMO did not return an order result")

    deal_id = result.get("dealId")
    if not deal_id:
        raise RuntimeError(f"IG DEMO did not return dealId: {result}")

    hold_seconds = max(
        900,
        int(candidate.get("holding_bars") or 4) * 15 * 60,
    )
    actual_size = result.get("size")
    if actual_size is None:
        actual_size = requested

    position = {
        "track": "CATEGORY",
        "category": category,
        "category_rank": candidate.get("category_rank"),
        "strategy_id": candidate.get("strategy_id"),
        "strategy_name": candidate.get("strategy_name"),
        "symbol": candidate.get("symbol"),
        "market": candidate.get("market"),
        "direction": candidate.get("direction"),
        "epic": candidate.get("ig_epic"),
        "deal_id": deal_id,
        "deal_reference": result.get("dealReference") or base_ref,
        "size": actual_size,
        "requested_size": requested,
        "entry_level": result.get("level"),
        "opened_at": _now(),
        "due_at": _now() + hold_seconds,
        "status": "OPEN",
        "exposure_tags": list(candidate.get("exposure_tags") or []),
        "quant_confidence": candidate.get("quant_confidence"),
        "model_ai_confidence": candidate.get("model_ai_confidence"),
        "historical_win_rate": candidate.get("historical_win_rate"),
        "historical_profit_factor": candidate.get("historical_profit_factor"),
        "smart_fast_score": candidate.get("smart_fast_score"),
        "live_fast_score": candidate.get("live_fast_score"),
        "adaptive_size_used": bool(
            health.get("last_size_plan", {}).get("final_result")
            == "OPENED_AFTER_DOWNSIZE"
        ),
        "size_attempts": list(attempts),
        "dual_track": self._is_dual_track(candidate.get("ig_epic"), external),
        "live_money_execution": False,
    }
    self._state.setdefault("positions", []).append(position)
    self._state["opens"] = int(self._state.get("opens") or 0) + 1
    self._journal(
        "OPEN",
        category=category,
        symbol=position["symbol"],
        deal_id=deal_id,
        size=actual_size,
        requested_size=requested,
        adaptive_size_used=position["adaptive_size_used"],
        dual_track=position["dual_track"],
    )


def _recomputed_operational_reasons(row: Dict[str, Any]) -> List[str]:
    """Recompute the current live STRONG blockers for diagnostics only.

    This intentionally avoids legacy rejection strings generated before
    ForwardPrimeArchitecture replaced historical smart FAST with live FAST.
    It does not change execution eligibility.
    """
    reasons: List[str] = []
    direction = str(row.get("direction") or row.get("live_direction") or "").upper()
    quant = _to_float(row.get("quant_confidence"), 0.0)
    ai = _to_float(row.get("model_ai_confidence"), 0.0)
    fast = _to_float(
        row.get("live_fast_score")
        if row.get("live_fast_score") is not None
        else row.get("smart_fast_score"),
        0.0,
    )

    if direction not in {"BUY", "SELL"}:
        reasons.append("NO_DIRECTION")
    if quant < 0.28:
        reasons.append("QUANT_BELOW_28")
    if ai < 0.40:
        reasons.append("MODEL_AI_BELOW_40")
    if fast < 45.0:
        reasons.append("FAST_BELOW_45")
    if not bool(row.get("ig_tradeable")):
        reasons.append("IG_NOT_TRADEABLE")
    if row.get("spread_pass") is not True:
        reasons.append("SPREAD_GATE_FAIL")

    provenance = row.get("provenance")
    if isinstance(provenance, dict):
        for issue in provenance.get("issues") or []:
            issue_text = str(issue or "").upper().strip()
            if issue_text and issue_text not in reasons:
                reasons.append(issue_text)

    return reasons


def _reliable_category_tick(self: CategoryExecutionEngine) -> Dict[str, Any]:
    """Run one Category execution tick without letting one market abort the rest."""
    with self._lock:
        health = _execution_health_state(self)
        now = _now()
        health["last_tick_started_at"] = now
        health["tick_count"] = int(health.get("tick_count") or 0) + 1
        health["candidate_attempts_this_tick"] = 0
        health["candidate_opens_this_tick"] = 0
        health["candidate_errors_this_tick"] = 0
        health["standard_eligible_this_tick"] = 0
        health["ranked_candidates_this_tick"] = 0
        health["blocked_eligible_this_tick"] = 0
        health["skipped_error_cooldown_this_tick"] = 0
        health["tick_error"] = None
        blockers: Counter[str] = Counter()

        try:
            health["phase"] = "RECONCILE"
            health["phase_started_at"] = _now()
            self._reconcile()

            health["phase"] = "DUE_CLOSES"
            health["phase_started_at"] = _now()
            self._due_closes()

            configured = bool(
                getattr(self.broker, "configured", lambda: False)()
            )
            health["broker_configured"] = configured
            try:
                broker_status = self.broker.status() or {}
            except Exception:
                broker_status = {}
            health["broker_connected"] = bool(broker_status.get("connected"))
            health["broker_last_error"] = broker_status.get("last_error")
            health["category_autotrade_enabled"] = bool(self.enabled)

            if not self.enabled:
                health["last_open_result"] = "CATEGORY_AUTOTRADE_DISABLED"
            elif not configured:
                health["last_open_result"] = "IG_DEMO_NOT_CONFIGURED"
            else:
                health["phase"] = "EXTERNAL_SNAPSHOT"
                health["phase_started_at"] = _now()
                external = _cached_external_for_tick(self)

                health["phase"] = "RANKINGS"
                health["phase_started_at"] = _now()
                rankings = self.ranking_source() or {}
                health["rankings_ready_at"] = _now()
                cooldowns = health.setdefault("candidate_error_cooldowns", {})
                retry_seconds = max(
                    15,
                    min(
                        900,
                        int(
                            os.getenv(
                                "CATEGORY_EXECUTION_ERROR_COOLDOWN_SECONDS",
                                "60",
                            )
                        ),
                    ),
                )
                health["error_cooldown_seconds"] = retry_seconds

                for category in (
                    "FOREX",
                    "INDICES",
                    "CRYPTO",
                    "METALS",
                    "ENERGY",
                    "SHARES",
                ):
                    for raw in rankings.get(category, [])[:5]:
                        if not isinstance(raw, dict):
                            continue
                        candidate = dict(raw)
                        health["ranked_candidates_this_tick"] += 1

                        # ForwardPrimeArchitecture defines STRONG learning
                        # eligibility through standard_eligible. Historical
                        # validation remains informational only upstream.
                        if not bool(candidate.get("standard_eligible")):
                            blockers["NOT_STANDARD_ELIGIBLE"] += 1
                            continue

                        health["standard_eligible_this_tick"] += 1
                        key = _candidate_key(candidate)

                        until = float(cooldowns.get(key) or 0.0)
                        if until > now:
                            blockers["ERROR_COOLDOWN"] += 1
                            health["skipped_error_cooldown_this_tick"] += 1
                            continue
                        if key in cooldowns:
                            cooldowns.pop(key, None)

                        allowed, reason = self._may_open(candidate, external)
                        if not allowed:
                            label = str(reason or "BLOCKED").upper().replace(" ", "_")
                            blockers[label] += 1
                            health["blocked_eligible_this_tick"] += 1
                            health["last_blocked_candidate"] = {
                                "at": now,
                                "candidate": key,
                                "reason": reason,
                            }
                            continue

                        health["candidate_attempts_this_tick"] += 1
                        health["total_isolated_candidate_attempts"] = int(
                            health.get("total_isolated_candidate_attempts") or 0
                        ) + 1
                        health["last_open_attempt"] = {
                            "at": _now(),
                            "candidate": key,
                            "category": candidate.get("category"),
                            "symbol": candidate.get("symbol"),
                            "direction": candidate.get("direction"),
                            "strategy_id": candidate.get("strategy_id")
                            or candidate.get("selected_strategy"),
                            "fast_score": candidate.get("smart_fast_score")
                            or candidate.get("live_fast_score"),
                            "quant_confidence": candidate.get("quant_confidence"),
                            "model_ai_confidence": candidate.get(
                                "model_ai_confidence"
                            ),
                        }
                        before_opens = int(self._state.get("opens") or 0)

                        try:
                            self._open_candidate(candidate, external)
                            after_opens = int(self._state.get("opens") or 0)
                            if after_opens > before_opens:
                                health["candidate_opens_this_tick"] += 1
                                health["total_isolated_candidate_opens"] = int(
                                    health.get("total_isolated_candidate_opens")
                                    or 0
                                ) + 1
                                health["last_open_result"] = "OPENED"
                                health["last_open_success_at"] = _now()
                                health["last_open_success"] = dict(
                                    health["last_open_attempt"]
                                )
                                cooldowns.pop(key, None)
                            else:
                                # _may_open passed immediately before this call.
                                # This is retained as a diagnostic rather than
                                # aborting the remaining markets.
                                health["last_open_result"] = "NO_OPEN_RECORDED"
                                blockers["NO_OPEN_RECORDED"] += 1
                        except Exception as exc:
                            label = _classify_broker_error(exc)
                            message = _compact_error(exc)
                            health["candidate_errors_this_tick"] += 1
                            health["total_candidate_errors"] = int(
                                health.get("total_candidate_errors") or 0
                            ) + 1
                            health["last_open_result"] = label
                            health["last_candidate_error"] = {
                                "at": _now(),
                                "candidate": key,
                                "classification": label,
                                "error": message,
                            }
                            recent = health.setdefault("recent_errors", [])
                            recent.append(dict(health["last_candidate_error"]))
                            health["recent_errors"] = recent[-20:]
                            size_plan = health.get("last_size_plan")
                            if (
                                label == "INSUFFICIENT_FUNDS"
                                and isinstance(size_plan, dict)
                                and size_plan.get("candidate") == key
                                and size_plan.get("final_result")
                                == "MINIMUM_DEAL_UNAFFORDABLE"
                            ):
                                label = "MINIMUM_DEAL_UNAFFORDABLE"
                                health["last_open_result"] = label
                                health["last_candidate_error"]["classification"] = label
                                recent[-1]["classification"] = label
                                try:
                                    funds_cooldown = int(
                                        os.getenv(
                                            "CATEGORY_EXECUTION_FUNDS_COOLDOWN_SECONDS",
                                            "300",
                                        )
                                    )
                                except Exception:
                                    funds_cooldown = 300
                                cooldown_for = max(60, min(1800, funds_cooldown))
                            else:
                                cooldown_for = retry_seconds

                            cooldowns[key] = _now() + cooldown_for
                            blockers[label] += 1
                            try:
                                self._journal(
                                    "OPEN_ERROR_ISOLATED",
                                    candidate=key,
                                    category=candidate.get("category"),
                                    symbol=candidate.get("symbol"),
                                    classification=label,
                                    error=message,
                                )
                            except Exception:
                                pass
                            # Critical behavior: continue to the next candidate.
                            continue

                # Trim expired/stale cooldown records.
                health["candidate_error_cooldowns"] = {
                    key: until
                    for key, until in cooldowns.items()
                    if float(until or 0.0) > now
                }

            health["recent_blockers"] = dict(blockers)
            health["phase"] = "COMPLETE"
            health["phase_started_at"] = _now()
            health["last_tick_completed_at"] = _now()
            health["last_tick_duration_seconds"] = round(
                health["last_tick_completed_at"] - now, 3
            )
            # Candidate-level errors are recorded under execution_reliability;
            # they no longer poison the entire engine tick.
            self._state["last_error"] = None
        except Exception as exc:
            message = _compact_error(exc)
            self._state["last_error"] = message
            health["tick_error"] = message
            health["last_open_result"] = "TICK_ERROR"
            health["last_tick_completed_at"] = _now()

        self._state["last_tick_at"] = _now()
        self._persist()

        # The background loop ignores tick()'s return value. Calling the full
        # status() here would trigger another external-position/broker read after
        # every tick, so return a cache-only summary instead.
        return {
            "version": getattr(self, "VERSION", "6.9.4"),
            "enabled": bool(self.enabled),
            "open_positions": len(self._open_positions()),
            "last_tick_at": self._state.get("last_tick_at"),
            "last_error": self._state.get("last_error"),
            "execution_health": copy.deepcopy(health),
            "live_money_execution": False,
        }


def _reliable_category_status(self: CategoryExecutionEngine) -> Dict[str, Any]:
    out = _ORIGINAL_CATEGORY_STATUS(self)
    try:
        raw = copy.deepcopy(
            (self._state or {}).get("execution_reliability") or {}
        )
    except Exception:
        raw = {}
    raw.pop("candidate_error_cooldowns", None)
    out["execution_health"] = raw
    out["entry_policy"] = {
        "quant_min_pct": 28.0,
        "model_ai_min_pct": 40.0,
        "live_fast_min": 45.0,
        "historical_validation_mode": "INFORMATIONAL_ONLY",
        "prime_authority": "BROKER_SETTLED_FORWARD_ONLY",
        "execution_mode": "IG_DEMO_ONLY",
    }
    return out


def _reliable_strategy_loop(self: CategoryStrategyEngine) -> None:
    """Keep rolling live candidate refresh active while optimisation runs.

    Historical/optimizer work remains available for diagnostics, but a running
    full refresh no longer prevents the normal rotating live batch from being
    refreshed. ResilientSpecialistMarketData owns provider throttling/cache.
    """
    if self._stop.wait(12.0):
        return

    while not self._stop.is_set():
        cycle_started = _now()
        try:
            if self._state.get("enabled", True):
                coverage = self.evidence_coverage()
                refresh_running = bool(
                    self._full_refresh_thread
                    and self._full_refresh_thread.is_alive()
                )

                if (
                    self.auto_full_refresh
                    and coverage["markets_pending_optimisation"] > 0
                    and not refresh_running
                ):
                    self.start_full_refresh()
                    refresh_running = True

                # Keep live candidate coverage available without doubling the
                # full optimiser workload. While a historical refresh is
                # running, only rescue a category that has no fresh candidate
                # rows. When no historical refresh is running, retain the
                # normal rotating live batch.
                live_refresh_ran = False
                rescued_category = None
                if refresh_running:
                    try:
                        fresh_rows = self._fresh_rows() or []
                    except Exception:
                        fresh_rows = []
                    fresh_categories = {
                        str(row.get("category") or "").upper().strip()
                        for row in fresh_rows
                        if isinstance(row, dict)
                    }
                    missing = [
                        category
                        for category in (
                            "FOREX",
                            "INDICES",
                            "CRYPTO",
                            "METALS",
                            "ENERGY",
                            "SHARES",
                        )
                        if category not in fresh_categories
                    ]
                    if missing:
                        rescued_category = missing[0]
                        self.run_now(category=rescued_category)
                        live_refresh_ran = True
                else:
                    self.run_now()
                    live_refresh_ran = True

                with self._lock:
                    self._state["live_refresh_independent"] = True
                    if live_refresh_ran:
                        self._state["last_independent_live_refresh_at"] = _now()
                    self._state["last_live_rescue_category"] = rescued_category
                    self._state["historical_refresh_running_during_live"] = bool(
                        refresh_running
                    )
                    self._state["historical_refresh_execution_veto"] = False
                    self._state["last_loop_duration_seconds"] = round(
                        _now() - cycle_started, 3
                    )
                    self._persist()
        except Exception as exc:
            with self._lock:
                self._state["last_error"] = (
                    f"live refresh: {_compact_error(exc)}"
                )
                self._state["live_refresh_independent"] = True
                self._state["historical_refresh_execution_veto"] = False
                self._persist()

        elapsed = max(0.0, _now() - cycle_started)
        wait_for = max(
            5.0,
            float(self.scan_interval_seconds) - elapsed,
        )
        self._stop.wait(wait_for)


def install_execution_reliability() -> Dict[str, Any]:
    global _INSTALLED
    with _PATCH_LOCK:
        if _INSTALLED:
            return {
                "version": VERSION,
                "installed": True,
                "already_installed": True,
            }

        _patch_ig_demo_marker()
        _patch_ig_positions_cache()
        CategoryExecutionEngine._reconcile = _optimized_category_reconcile
        CategoryExecutionEngine._open_candidate = _adaptive_category_open_candidate
        CategoryExecutionEngine.tick = _reliable_category_tick
        CategoryExecutionEngine.status = _reliable_category_status
        CategoryStrategyEngine._loop = _reliable_strategy_loop
        _INSTALLED = True

        return {
            "version": VERSION,
            "installed": True,
            "ig_demo_marker_fixed": True,
            "ig_positions_short_cache": True,
            "single_snapshot_reconciliation": True,
            "candidate_exception_isolation": True,
            "adaptive_category_size_downshift": True,
            "minimum_deal_size_floor_enforced": True,
            "live_refresh_independent_of_historical_refresh": True,
            "historical_execution_veto": False,
            "live_money_execution": False,
        }




_RUNTIME_OPT_LOCK = threading.RLock()
_RUNTIME_OPTIMIZED_IDS: set[int] = set()


def _install_runtime_execution_optimizations(
    *,
    system: Optional[Dict[str, Any]],
    broker: Any,
) -> Dict[str, Any]:
    """Wire lightweight exposure reads and freshness-compatible scan cadence."""
    system = system or {}
    portfolio = system.get("portfolio")
    intelligence = system.get("intelligence")
    if portfolio is None or intelligence is None or broker is None:
        return {
            "installed": False,
            "reason": "specialist runtime not ready",
        }

    marker = id(portfolio)
    with _RUNTIME_OPT_LOCK:
        if marker in _RUNTIME_OPTIMIZED_IDS:
            return {
                "installed": True,
                "already_installed": True,
            }

        portfolio.external_positions_source = _make_light_external_positions_source(
            broker=broker,
            intelligence=intelligence,
        )

        # Forward execution rejects signals older than 300s. The legacy scanner
        # default is one market/category every 180s, which can leave a 9-10
        # market category unrefreshed for ~27-30 minutes. Tighten the rolling
        # cadence while leaving the strategy itself unchanged.
        try:
            scan_seconds = int(
                os.getenv("JASONG_LIVE_SCAN_INTERVAL_SECONDS", "90")
            )
        except Exception:
            scan_seconds = 90
        scan_seconds = max(60, min(180, scan_seconds))
        intelligence.scan_interval_seconds = scan_seconds

        try:
            candidate_ttl = int(
                os.getenv("JASONG_EXECUTION_CANDIDATE_TTL_SECONDS", "300")
            )
        except Exception:
            candidate_ttl = 300
        candidate_ttl = max(180, min(300, candidate_ttl))
        intelligence.candidate_ttl_seconds = candidate_ttl

        _RUNTIME_OPTIMIZED_IDS.add(marker)

        state = getattr(portfolio, "_state", None)
        if isinstance(state, dict):
            health = state.setdefault("execution_reliability", {})
            health["lightweight_external_positions_source"] = True
            health["single_snapshot_reconciliation"] = True
            health["network_free_tick_return"] = True
            health["scanner_interval_seconds"] = scan_seconds
            health["execution_candidate_ttl_seconds"] = candidate_ttl
            health["funds_rejection_cooldown_seconds"] = max(
                60,
                min(
                    1800,
                    int(
                        os.getenv(
                            "CATEGORY_EXECUTION_FUNDS_COOLDOWN_SECONDS",
                            "300",
                        )
                    ),
                ),
            )

        return {
            "installed": True,
            "lightweight_external_positions_source": True,
            "single_snapshot_reconciliation": True,
            "network_free_tick_return": True,
            "scanner_interval_seconds": scan_seconds,
            "execution_candidate_ttl_seconds": candidate_ttl,
        }


def _age_seconds(timestamp: Any) -> Optional[float]:
    try:
        value = float(timestamp)
        if value <= 0:
            return None
        return round(max(0.0, _now() - value), 2)
    except Exception:
        return None


def execution_health_snapshot(
    *,
    system: Optional[Dict[str, Any]] = None,
    broker: Any = None,
) -> Dict[str, Any]:
    system = system or {}
    portfolio = system.get("portfolio")
    intelligence = system.get("intelligence")
    forward_prime = system.get("forward_prime")

    broker_status: Dict[str, Any] = {}
    try:
        broker_status = broker.status() if broker is not None else {}
        if not isinstance(broker_status, dict):
            broker_status = {}
    except Exception as exc:
        broker_status = {"last_error": _compact_error(exc)}

    portfolio_state = getattr(portfolio, "_state", {}) if portfolio is not None else {}
    reliability = {}
    if isinstance(portfolio_state, dict):
        try:
            reliability = copy.deepcopy(
                portfolio_state.get("execution_reliability") or {}
            )
        except Exception:
            reliability = {}
    reliability.pop("candidate_error_cooldowns", None)
    tick_started_at = reliability.get("last_tick_started_at")
    tick_completed_at = reliability.get("last_tick_completed_at")
    active_tick_age = None
    try:
        started = float(tick_started_at or 0.0)
        completed = float(tick_completed_at or 0.0)
        if started > 0 and started > completed:
            active_tick_age = round(max(0.0, _now() - started), 2)
    except Exception:
        active_tick_age = None

    intelligence_state = (
        getattr(intelligence, "_state", {}) if intelligence is not None else {}
    )
    if not isinstance(intelligence_state, dict):
        intelligence_state = {}
    full_refresh = intelligence_state.get("full_refresh")
    if not isinstance(full_refresh, dict):
        full_refresh = {}

    ranked = 0
    strong = 0
    prime = 0
    by_category: Dict[str, Dict[str, Any]] = {}
    signal_blockers: Counter[str] = Counter()
    blocked_candidates: List[Dict[str, Any]] = []
    historical_only_prefixes = (
        "HOLDOUT_",
        "WF_",
        "WALK_FORWARD_",
        "PROFIT_FACTOR_",
        "SELECTION_UNSTABLE",
        "FORWARD_VALIDATION_NOT_YET_PRIME",
    )
    try:
        rankings = (
            forward_prime.category_rankings()
            if forward_prime is not None
            else (
                portfolio.ranking_source()
                if portfolio is not None
                else {}
            )
        ) or {}
        for category in (
            "FOREX",
            "INDICES",
            "CRYPTO",
            "METALS",
            "ENERGY",
            "SHARES",
        ):
            rows = [
                row
                for row in rankings.get(category, [])[:5]
                if isinstance(row, dict)
            ]
            cat_ranked = len(rows)
            cat_strong = sum(
                1 for row in rows if bool(row.get("standard_eligible"))
            )
            cat_prime = sum(
                1 for row in rows if bool(row.get("prime_qualified"))
            )
            cat_blockers: Counter[str] = Counter()
            for row in rows:
                if bool(row.get("standard_eligible")):
                    continue
                operational_reasons = _recomputed_operational_reasons(row)
                for reason in operational_reasons:
                    signal_blockers[reason] += 1
                    cat_blockers[reason] += 1
                blocked_candidates.append(
                    {
                        "category": category,
                        "symbol": row.get("symbol") or row.get("market"),
                        "direction": row.get("direction"),
                        "quant_pct": (
                            round(_to_float(row.get("quant_confidence")) * 100.0, 2)
                        ),
                        "model_ai_pct": (
                            round(_to_float(row.get("model_ai_confidence")) * 100.0, 2)
                        ),
                        "fast": row.get("live_fast_score")
                        if row.get("live_fast_score") is not None
                        else row.get("smart_fast_score"),
                        "signal_age_seconds": row.get("signal_age_seconds"),
                        "quote_age_seconds": row.get("quote_age_seconds"),
                        "ig_tradeable": row.get("ig_tradeable"),
                        "spread_pass": row.get("spread_pass"),
                        "reasons": operational_reasons[:8],
                    }
                )
            ranked += cat_ranked
            strong += cat_strong
            prime += cat_prime
            by_category[category] = {
                "ranked": cat_ranked,
                "strong": cat_strong,
                "prime": cat_prime,
                "top_signal_blockers": dict(cat_blockers.most_common(5)),
            }
    except Exception as exc:
        reliability["ranking_health_error"] = _compact_error(exc)

    open_positions = 0
    try:
        if portfolio is not None:
            open_positions = len(portfolio._open_positions())
    except Exception:
        pass

    enabled = bool(getattr(portfolio, "enabled", False)) if portfolio is not None else False
    configured = bool(broker_status.get("configured"))
    tick_error = reliability.get("tick_error")
    last_result = str(reliability.get("last_open_result") or "")

    tick_at = (
        portfolio_state.get("last_tick_at")
        if isinstance(portfolio_state, dict)
        else None
    )
    tick_age = _age_seconds(tick_at)
    scan_at = intelligence_state.get("last_run_at")
    scan_age = _age_seconds(scan_at)
    execution_poll = float(getattr(portfolio, "poll_seconds", 30) or 30)
    scan_interval = float(getattr(intelligence, "scan_interval_seconds", 180) or 180)
    execution_stale_after = max(120.0, execution_poll * 3.0 + 30.0)
    scan_stale_after = max(360.0, scan_interval * 2.0 + 60.0)
    broker_last_error = str(broker_status.get("last_error") or "")
    historical_allowance_exhausted = (
        "historical-data-allowance" in broker_last_error.lower()
        or "historical data allowance" in broker_last_error.lower()
    )

    if not configured:
        flow_state = "BROKER_NOT_CONFIGURED"
    elif not enabled:
        flow_state = "CATEGORY_AUTOTRADE_DISABLED"
    elif tick_error:
        flow_state = "EXECUTION_TICK_ERROR"
    elif historical_allowance_exhausted and (
        (tick_age is not None and tick_age > execution_stale_after)
        or (scan_age is not None and scan_age > scan_stale_after)
    ):
        flow_state = "IG_HISTORICAL_ALLOWANCE_EXHAUSTED"
    elif tick_age is not None and tick_age > execution_stale_after:
        flow_state = "EXECUTION_LOOP_STALE"
    elif scan_age is not None and scan_age > scan_stale_after:
        flow_state = "LIVE_SCANNER_STALE"
    elif strong <= 0:
        flow_state = "WAITING_FOR_STRONG_SIGNAL"
    elif last_result in {
        "CATEGORY_PORTFOLIO_POSITION_CAP_REACHED",
        "GLOBAL_IG_DEMO_POSITION_CAP_REACHED",
    }:
        flow_state = "CAPACITY_BLOCKED"
    elif int(reliability.get("candidate_errors_this_tick") or 0) > 0:
        flow_state = "BROKER_REJECTIONS_ISOLATED"
    else:
        flow_state = "READY_TO_EXECUTE"

    return {
        "version": VERSION,
        "trade_flow_state": flow_state,
        "strategy_unchanged": True,
        "entry_policy": {
            "quant_min_pct": 28.0,
            "model_ai_min_pct": 40.0,
            "live_fast_min": 45.0,
            "historical_validation_mode": "INFORMATIONAL_ONLY",
            "historical_execution_veto": False,
            "prime_authority": "BROKER_SETTLED_FORWARD_ONLY",
            "execution_mode": "IG_DEMO_ONLY",
        },
        "broker": {
            "configured": configured,
            "connected": bool(broker_status.get("connected")),
            "environment": broker_status.get("environment") or "DEMO",
            "last_error": broker_status.get("last_error"),
            "demo_marker": bool(getattr(broker, "demo", True)) if broker is not None else True,
        },
        "category_execution": {
            "enabled": enabled,
            "open_positions": open_positions,
            "max_open_positions": getattr(portfolio, "max_open_positions", None),
            "global_ig_max_positions": getattr(
                portfolio, "global_ig_max_positions", None
            ),
            "last_tick_at": tick_at,
            "last_tick_age_seconds": tick_age,
            "active_tick_age_seconds": active_tick_age,
            "active_tick_phase": reliability.get("phase"),
            "active_tick_phase_started_at": reliability.get("phase_started_at"),
            "stale_after_seconds": execution_stale_after,
            "last_error": (
                portfolio_state.get("last_error")
                if isinstance(portfolio_state, dict)
                else None
            ),
            "ranked_candidates": ranked,
            "strong_candidates": strong,
            "prime_candidates": prime,
            "by_category": by_category,
            "execution_reliability": reliability,
        },
        "live_scanner": {
            "last_run_at": scan_at,
            "last_run_age_seconds": scan_age,
            "stale_after_seconds": scan_stale_after,
            "last_error": intelligence_state.get("last_error"),
            "live_refresh_independent": bool(
                intelligence_state.get("live_refresh_independent")
            ),
            "last_independent_live_refresh_at": intelligence_state.get(
                "last_independent_live_refresh_at"
            ),
            "historical_refresh_execution_veto": False,
            "scan_interval_seconds": getattr(intelligence, "scan_interval_seconds", None),
            "candidate_ttl_seconds": getattr(intelligence, "candidate_ttl_seconds", None),
            "last_loop_duration_seconds": intelligence_state.get("last_loop_duration_seconds"),
        },
        "historical_refresh": {
            "status": full_refresh.get("status"),
            "mode": full_refresh.get("mode"),
            "processed": full_refresh.get("processed"),
            "total": full_refresh.get("total"),
            "current_key": full_refresh.get("current_key"),
            "errors": full_refresh.get("errors"),
            "last_error": full_refresh.get("last_error"),
            "running_during_live_refresh": bool(
                intelligence_state.get(
                    "historical_refresh_running_during_live"
                )
            ),
            "execution_veto": False,
        },
        "signal_gate_diagnostics": {
            "operational_blockers": dict(signal_blockers.most_common(12)),
            "blocked_candidates": sorted(
                blocked_candidates,
                key=lambda row: (
                    len(row.get("reasons") or []),
                    -(_to_float(row.get("fast"))),
                ),
            )[:12],
            "historical_only_reasons_excluded": True,
        },
        "runtime_architecture": {
            "lightweight_external_positions_source": bool(
                reliability.get("lightweight_external_positions_source")
            ),
            "single_snapshot_reconciliation": bool(
                reliability.get("single_snapshot_reconciliation")
            ),
            "network_free_tick_return": bool(
                reliability.get("network_free_tick_return")
            ),
            "scanner_interval_seconds": getattr(
                intelligence, "scan_interval_seconds", None
            ),
            "execution_candidate_ttl_seconds": getattr(
                intelligence, "candidate_ttl_seconds", None
            ),
            "funds_rejection_cooldown_seconds": reliability.get(
                "funds_rejection_cooldown_seconds"
            ),
        },
        "broker_data_policy": {
            "ig_historical_candles_enabled": str(
                os.getenv("IG_DEMO_MARKET_DATA", "true")
            ).strip().lower() not in {"0", "false", "no", "off"},
            "ig_historical_candles_role": "DISABLED_FOR_ANALYSIS" if str(
                os.getenv("IG_DEMO_MARKET_DATA", "true")
            ).strip().lower() in {"0", "false", "no", "off"} else "ENABLED",
            "ig_quotes_and_execution": "IG_DEMO_PRIMARY",
            "historical_allowance_exhausted": historical_allowance_exhausted,
            "positions_cache_seconds": _positions_cache_seconds(),
            "positions_stale_fallback_seconds": _positions_stale_fallback_seconds(),
            "positions_cache_hits": int(
                getattr(broker, "_jasong_positions_cache_hits", 0) or 0
            ) if broker is not None else 0,
            "positions_cache_stale_hits": int(
                getattr(broker, "_jasong_positions_cache_stale_hits", 0) or 0
            ) if broker is not None else 0,
            "positions_cache_misses": int(
                getattr(broker, "_jasong_positions_cache_misses", 0) or 0
            ) if broker is not None else 0,
            "positions_cache_last_mode": (
                getattr(broker, "_jasong_positions_cache_last_mode", None)
                if broker is not None else None
            ),
            "category_size_policy":
                "DEFAULT_THEN_DEMO_ONLY_DOWNSHIFT_ON_EXPLICIT_INSUFFICIENT_FUNDS",
            "category_default_size": getattr(portfolio, "default_size", None),
            "never_below_ig_minimum": True,
            "never_increases_size_on_funds_rejection": True,
        },
        "live_money_execution": False,
    }


def install_execution_health_route(
    app: Any,
    *,
    system: Optional[Dict[str, Any]] = None,
    broker: Any = None,
) -> None:
    _install_runtime_execution_optimizations(
        system=system,
        broker=broker,
    )

    if any(
        getattr(route, "path", "") == "/execution-health"
        for route in getattr(app, "routes", [])
    ):
        return

    def _endpoint() -> Dict[str, Any]:
        return execution_health_snapshot(system=system, broker=broker)

    app.add_api_route(
        "/execution-health",
        _endpoint,
        methods=["GET"],
        name="jasong_execution_health_v694",
    )


INSTALL_STATUS = install_execution_reliability()
