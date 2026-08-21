from __future__ import annotations

import copy
import os
import statistics
import threading
import time
import uuid
from collections import Counter
from typing import Any, Dict, List, Optional

from category_execution_engine import CategoryExecutionEngine
import category_strategy_engine as category_strategy_module
from category_strategy_engine import CategoryStrategyEngine
from ig_demo_broker import IGDemoBroker
from prime_policy import ForwardPrimeArchitecture
from trade_excursions import TradeExcursionTracker


VERSION = "6.9.4-wr-guard-v5"


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
_ORIGINAL_FORWARD_ENRICH = ForwardPrimeArchitecture.enrich
_ORIGINAL_FORWARD_RANKINGS = ForwardPrimeArchitecture.category_rankings
_ORIGINAL_FORWARD_CATEGORY_ROWS = ForwardPrimeArchitecture._category_rows
_ORIGINAL_TRACKER_UPDATE_TP = TradeExcursionTracker._update_take_profit_fields
_ORIGINAL_TRACKER_NATIVE_NEEDED = TradeExcursionTracker._native_take_profit_needed
_ORIGINAL_TRACKER_ATTACH_TP = TradeExcursionTracker._attach_native_take_profit
_ORIGINAL_TRACKER_EXECUTE_TP_CLOSE = TradeExcursionTracker._execute_take_profit_close
_ORIGINAL_TRACKER_MERGE = TradeExcursionTracker.merge
_ORIGINAL_TRACKER_STATUS = TradeExcursionTracker.status
_PATCH_LOCK = threading.RLock()
_INSTALLED = False



# ---------------------------------------------------------------------------
# V5 WIN-RATE PROTECTION / VALIDATED EXECUTION GUARD
# ---------------------------------------------------------------------------
#
# The overnight/live evidence showed that the V6.9.4 ForwardPrime layer had
# accidentally turned `strong_qualified` into `standard_eligible`, overriding
# the CategoryStrategyEngine's original 60% holdout + PF>=1.20 + walk-forward
# requirement. V5 restores validation as an execution prerequisite.
#
# STRONG remains a useful live timing/watch state. It is no longer sufficient
# to authorize a Category order.
# ---------------------------------------------------------------------------

WR_GUARD_HIST_WR_MIN = 0.60
WR_GUARD_HIST_PF_MIN = 1.20
WR_GUARD_FORWARD_WR_MIN = 0.50
WR_GUARD_FORWARD_MIN_SAMPLE = 20
WR_GUARD_CALIBRATED_MIN = 0.60
WR_GUARD_QUARANTINE_RECOVERY_WR = 0.55

# Seed quarantines are based on the user's broker-settled forward report.
# A quarantined family can recover automatically only after both the strict
# historical gate passes and >=20 forward trades recover to >=55% WR.
WR_GUARD_SEEDED_QUARANTINES = (
    "INDEX_SESSION_MOMENTUM",
    "METALS_BREAKOUT",
    "ENERGY_TREND",
)



def _patch_quarantined_strategy_variants() -> Dict[str, List[str]]:
    """Remove proven failing variants from new optimizer selections.

    The optimizer still uses its original selection window and untouched
    holdout/walk-forward procedure. We only remove three families whose broker
    forward records are currently unacceptable. Alternative variants must still
    earn the unchanged 60% WR / 1.20 PF / WF validation before execution.
    """
    removed: Dict[str, List[str]] = {}
    mapping = {
        "INDICES": {"INDEX_SESSION_MOMENTUM_V1"},
        "METALS": {"METALS_BREAKOUT_V2"},
        "ENERGY": {"ENERGY_TREND_V2"},
    }
    variants_map = getattr(category_strategy_module, "STRATEGY_VARIANTS", None)
    if not isinstance(variants_map, dict):
        return removed

    for category, banned in mapping.items():
        variants = list(variants_map.get(category) or [])
        kept = [
            variant
            for variant in variants
            if str(getattr(variant, "strategy_id", "")).upper() not in banned
        ]
        dropped = [
            str(getattr(variant, "strategy_id", ""))
            for variant in variants
            if str(getattr(variant, "strategy_id", "")).upper() in banned
        ]
        if kept and dropped:
            variants_map[category] = kept
            removed[category] = dropped
            signal_map = getattr(category_strategy_module, "_SIGNAL_FUNC", None)
            if isinstance(signal_map, dict):
                signal_map[category] = kept[0].signal_func
    return removed


def _strategy_id(row: Dict[str, Any]) -> str:
    return str(
        row.get("strategy_id")
        or row.get("selected_strategy")
        or row.get("strategy_name")
        or "UNKNOWN"
    ).upper().strip()


def _historical_execution_gate(row: Dict[str, Any]) -> tuple[bool, List[str]]:
    reasons: List[str] = []
    wr = _to_float(row.get("historical_win_rate"), 0.0)
    pf = _to_float(row.get("historical_profit_factor"), 0.0)
    sample = int(_to_float(row.get("historical_trades"), 0.0))
    selection_stable = bool(row.get("optimizer_selection_stable"))
    wf = bool(row.get("walk_forward_pass"))
    target = bool(
        row.get("historical_target_verified")
        or row.get("historical_60_verified")
    )

    if sample < 10:
        reasons.append("HISTORICAL_SAMPLE_BELOW_10")
    if wr < WR_GUARD_HIST_WR_MIN:
        reasons.append("HISTORICAL_WR_BELOW_60")
    if pf < WR_GUARD_HIST_PF_MIN:
        reasons.append("HISTORICAL_PF_BELOW_1_20")
    if not selection_stable:
        reasons.append("SELECTION_NOT_STABLE")
    if not wf:
        reasons.append("WALK_FORWARD_NOT_PASSED")
    if not target:
        reasons.append("HISTORICAL_60_TARGET_NOT_VERIFIED")
    return len(reasons) == 0, reasons


def _forward_safety_gate(
    row: Dict[str, Any],
) -> tuple[bool, List[str], bool]:
    forward = row.get("forward_validation")
    if not isinstance(forward, dict):
        return True, [], False

    settled = int(_to_float(forward.get("settled_trades"), 0.0))
    wr = _to_float(forward.get("win_rate"), 0.0)
    reasons: List[str] = []
    applicable = settled >= WR_GUARD_FORWARD_MIN_SAMPLE
    if applicable and wr < WR_GUARD_FORWARD_WR_MIN:
        reasons.append("FORWARD_WR_BELOW_50")
    return len(reasons) == 0, reasons, applicable


def _seeded_quarantine_state(
    row: Dict[str, Any],
    *,
    historical_ok: bool,
) -> tuple[bool, Optional[str]]:
    sid = _strategy_id(row)
    matched = next(
        (prefix for prefix in WR_GUARD_SEEDED_QUARANTINES if sid.startswith(prefix)),
        None,
    )
    if not matched:
        return False, None

    forward = row.get("forward_validation")
    forward = forward if isinstance(forward, dict) else {}
    settled = int(_to_float(forward.get("settled_trades"), 0.0))
    wr = _to_float(forward.get("win_rate"), 0.0)

    recovered = bool(
        historical_ok
        and settled >= WR_GUARD_FORWARD_MIN_SAMPLE
        and wr >= WR_GUARD_QUARANTINE_RECOVERY_WR
    )
    return (not recovered), matched


def _calibrated_execution_confidence(row: Dict[str, Any]) -> float:
    """Calibration score where validation/forward evidence dominates live timing.

    This is deliberately NOT advertised as a probability. It is an execution
    quality score used only after the hard historical/forward safety gates.
    """
    hist_wr = max(0.0, min(1.0, _to_float(row.get("historical_win_rate"), 0.0)))
    hist_pf = max(0.0, _to_float(row.get("historical_profit_factor"), 0.0))
    hist_pf_score = max(0.0, min(1.0, hist_pf / 1.50))

    forward = row.get("forward_validation")
    forward = forward if isinstance(forward, dict) else {}
    settled = int(_to_float(forward.get("settled_trades"), 0.0))
    forward_wr = max(0.0, min(1.0, _to_float(forward.get("win_rate"), hist_wr)))
    if settled < 12:
        # Before a minimally useful forward sample exists, do not fabricate a
        # forward probability. Historical validation temporarily carries it.
        forward_wr = hist_wr

    ai = max(0.0, min(1.0, _to_float(row.get("model_ai_confidence"), 0.0)))
    quant = max(0.0, min(1.0, _to_float(row.get("quant_confidence"), 0.0)))
    fast = max(
        0.0,
        min(
            1.0,
            _to_float(
                row.get("live_fast_score")
                if row.get("live_fast_score") is not None
                else row.get("smart_fast_score"),
                0.0,
            )
            / 100.0,
        ),
    )

    score = (
        0.35 * hist_wr
        + 0.20 * hist_pf_score
        + 0.20 * forward_wr
        + 0.10 * ai
        + 0.05 * quant
        + 0.10 * fast
    )
    return round(max(0.0, min(1.0, score)), 6)


def _guarded_forward_enrich(
    self: ForwardPrimeArchitecture,
    raw: Dict[str, Any],
    *,
    forward_metrics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    row = _ORIGINAL_FORWARD_ENRICH(
        self,
        raw,
        forward_metrics=forward_metrics,
    )

    live_strong = bool(row.get("strong_qualified"))
    historical_ok, historical_reasons = _historical_execution_gate(row)
    forward_ok, forward_reasons, forward_gate_applicable = _forward_safety_gate(row)
    seeded_quarantine, quarantine_family = _seeded_quarantine_state(
        row,
        historical_ok=historical_ok,
    )
    unknown_strategy = _strategy_id(row) == "UNKNOWN"
    calibrated = _calibrated_execution_confidence(row)
    calibrated_ok = calibrated >= WR_GUARD_CALIBRATED_MIN

    validated_standard = bool(
        live_strong
        and historical_ok
        and forward_ok
        and not seeded_quarantine
        and not unknown_strategy
        and calibrated_ok
    )

    forward = row.get("forward_validation")
    forward = forward if isinstance(forward, dict) else {}
    prime = bool(validated_standard and forward.get("prime_eligible"))

    guard_reasons: List[str] = []
    guard_reasons.extend(historical_reasons)
    guard_reasons.extend(forward_reasons)
    if seeded_quarantine:
        guard_reasons.append(
            f"STRATEGY_QUARANTINED_{quarantine_family}"
        )
    if unknown_strategy:
        guard_reasons.append("UNKNOWN_STRATEGY_ATTRIBUTION")
    if not calibrated_ok:
        guard_reasons.append("CALIBRATED_EXECUTION_SCORE_BELOW_60")

    existing = [
        str(x)
        for x in (row.get("rejection_reasons") or [])
        if str(x or "").strip()
    ]
    rejection = list(dict.fromkeys(existing + guard_reasons))
    if live_strong and not validated_standard:
        rejection.append("STRONG_IS_WATCH_ONLY_UNTIL_VALIDATED")
    if validated_standard and not prime:
        rejection.append("FORWARD_VALIDATION_NOT_YET_PRIME")

    historical = row.get("historical_validation")
    if isinstance(historical, dict):
        historical = dict(historical)
        historical["mode"] = "STANDARD_EXECUTION_GATE"
        historical["execution_veto"] = True
        row["historical_validation"] = historical

    row.update(
        {
            "historical_validation_mode": "STANDARD_EXECUTION_GATE",
            "historical_execution_veto": True,
            "validated_execution_gate": validated_standard,
            "historical_execution_gate_pass": historical_ok,
            "historical_execution_gate_reasons": historical_reasons,
            "forward_safety_gate_pass": forward_ok,
            "forward_safety_gate_applicable": forward_gate_applicable,
            "forward_safety_gate_reasons": forward_reasons,
            "strategy_quarantined": bool(seeded_quarantine),
            "strategy_quarantine_family": quarantine_family,
            "unknown_strategy_blocked": unknown_strategy,
            "calibrated_execution_confidence": calibrated,
            "calibrated_execution_confidence_pct": round(calibrated * 100.0, 2),
            "calibrated_execution_threshold_pct": WR_GUARD_CALIBRATED_MIN * 100.0,
            "standard_eligible": validated_standard,
            "trade_eligible": validated_standard,
            "learning_eligible": False,
            "ig_demo_learning_eligible": validated_standard,
            "prime_qualified": prime,
            "execution_eligible": prime,
            "eligible": prime,
            "trade_class": (
                "PRIME"
                if prime
                else (
                    "VALIDATED_STRONG"
                    if validated_standard
                    else ("WATCH_STRONG" if live_strong else "OBSERVE")
                )
            ),
            "execution_basis": (
                "FORWARD_PRIME_PLUS_VALIDATED_STANDARD_GATE"
                if prime
                else (
                    "VALIDATED_STANDARD_CATEGORY"
                    if validated_standard
                    else "WATCH_ONLY_NOT_VALIDATED"
                )
            ),
            "rejection_reasons": list(dict.fromkeys(rejection)),
        }
    )
    return row


def _guarded_forward_rankings(
    self: ForwardPrimeArchitecture,
    *args: Any,
    **kwargs: Any,
) -> Dict[str, List[Dict[str, Any]]]:
    """Prevent category_rankings() from re-promoting STRONG into standard."""
    output = _ORIGINAL_FORWARD_RANKINGS(self, *args, **kwargs) or {}
    for rows in output.values():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            standard = bool(row.get("validated_execution_gate"))
            row["standard_eligible"] = standard
            row["trade_eligible"] = standard
            row["ig_demo_learning_eligible"] = standard
            row["learning_eligible"] = False
            forward = row.get("forward_validation")
            forward = forward if isinstance(forward, dict) else {}
            prime = bool(standard and forward.get("prime_eligible"))
            row["prime_qualified"] = prime
            row["execution_eligible"] = prime
            row["eligible"] = prime
            row["compound_eligible"] = bool(
                row.get("compound_slot_candidate") and prime
            )
    return output


def _enhanced_category_rows(
    self: ForwardPrimeArchitecture,
) -> List[Dict[str, Any]]:
    """Improve new forward R/attribution telemetry without rewriting old rows."""
    rows = _ORIGINAL_FORWARD_CATEGORY_ROWS(self) or []
    out: List[Dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        row["strategy_id"] = _strategy_id(row)

        close_result = (
            row.get("close_result")
            if isinstance(row.get("close_result"), dict)
            else {}
        )
        if row.get("broker_pnl") is None:
            for key in ("profitLoss", "pnl", "profit"):
                if close_result.get(key) is not None:
                    row["broker_pnl"] = close_result.get(key)
                    break

        planned_stop_pct = _to_float(row.get("planned_stop_pct"), 0.0)
        if planned_stop_pct > 0:
            entry = _to_float(row.get("entry_level") or row.get("entry_price"), 0.0)
            exit_level = _to_float(
                close_result.get("level")
                or row.get("exit_level")
                or row.get("current_price"),
                0.0,
            )
            direction = str(row.get("direction") or "").upper()
            if entry > 0 and exit_level > 0 and direction in {"BUY", "SELL"}:
                favourable_pct = (
                    ((exit_level - entry) / entry) * 100.0
                    if direction == "BUY"
                    else ((entry - exit_level) / entry) * 100.0
                )
                row["r_multiple"] = round(
                    favourable_pct / planned_stop_pct,
                    6,
                )
                row["r_source"] = "PRICE_MOVE_OVER_PLANNED_STOP_PCT"
                row["planned_risk_pct"] = planned_stop_pct
        out.append(row)
    return out


def _recent_volatility_pct(candidate: Dict[str, Any]) -> float:
    values: List[float] = []
    for raw in (candidate.get("recent_returns") or [])[-20:]:
        try:
            value = abs(float(raw)) * 100.0
            if value > 0:
                values.append(value)
        except Exception:
            continue
    if not values:
        return 0.0
    try:
        return float(statistics.median(values))
    except Exception:
        return sum(values) / len(values)


_EXIT_PROFILES: Dict[str, Dict[str, float]] = {
    "FOREX": {"min_stop": 0.05, "max_stop": 0.12, "rr": 1.25},
    "INDICES": {"min_stop": 0.10, "max_stop": 0.25, "rr": 1.25},
    "CRYPTO": {"min_stop": 0.30, "max_stop": 0.80, "rr": 1.30},
    "METALS": {"min_stop": 0.10, "max_stop": 0.25, "rr": 1.25},
    "ENERGY": {"min_stop": 0.15, "max_stop": 0.35, "rr": 1.25},
    "SHARES": {"min_stop": 0.15, "max_stop": 0.40, "rr": 1.30},
}


def _candidate_exit_plan(candidate: Dict[str, Any]) -> Dict[str, Any]:
    category = str(candidate.get("category") or "UNKNOWN").upper().strip()
    profile = _EXIT_PROFILES.get(
        category,
        {"min_stop": 0.10, "max_stop": 0.30, "rr": 1.25},
    )
    volatility_pct = _recent_volatility_pct(candidate)
    raw_stop = volatility_pct * 2.5 if volatility_pct > 0 else profile["min_stop"]
    stop_pct = max(
        profile["min_stop"],
        min(profile["max_stop"], raw_stop),
    )
    target_pct = stop_pct * profile["rr"]
    return {
        "exit_policy_version": "V5_VOLATILITY_R",
        "entry_volatility_pct": round(volatility_pct, 6),
        "planned_stop_pct": round(stop_pct, 6),
        "planned_take_profit_pct": round(target_pct, 6),
        "planned_reward_r": round(profile["rr"], 3),
        "trailing_activate_r": 0.75,
        "trailing_lock_r": 0.10,
        "exit_plan_basis": "MEDIAN_ABS_LAST20_RETURNS_X2_5_WITH_CATEGORY_CLAMPS",
    }


def _attach_initial_native_risk(
    self: CategoryExecutionEngine,
    position: Dict[str, Any],
) -> None:
    deal_id = str(position.get("deal_id") or "").strip()
    entry = _to_float(position.get("entry_level"), 0.0)
    direction = str(position.get("direction") or "").upper().strip()
    stop_pct = _to_float(position.get("planned_stop_pct"), 0.0)
    tp_pct = _to_float(position.get("planned_take_profit_pct"), 0.0)
    if (
        not deal_id
        or entry <= 0
        or direction not in {"BUY", "SELL"}
        or stop_pct <= 0
        or tp_pct <= 0
    ):
        position["native_risk_state"] = "NOT_READY"
        return

    if direction == "BUY":
        stop_level = entry * (1.0 - stop_pct / 100.0)
        limit_level = entry * (1.0 + tp_pct / 100.0)
    else:
        stop_level = entry * (1.0 + stop_pct / 100.0)
        limit_level = entry * (1.0 - tp_pct / 100.0)

    position["planned_stop_level"] = round(stop_level, 10)
    position["planned_take_profit_level"] = round(limit_level, 10)
    request_fn = getattr(self.broker, "_request", None)
    if not callable(request_fn):
        position["native_risk_state"] = "TRACKER_FALLBACK"
        return

    try:
        ack = request_fn(
            "PUT",
            f"/positions/otc/{deal_id}",
            version=2,
            payload={
                "stopLevel": float(stop_level),
                "limitLevel": float(limit_level),
            },
        ) or {}
        ref = str(ack.get("dealReference") or "").strip()
        confirmation: Dict[str, Any] = {}
        confirm_fn = getattr(self.broker, "confirm", None)
        if ref and callable(confirm_fn):
            confirmation = confirm_fn(ref) or {}
        rejected = (
            str(confirmation.get("dealStatus") or "").upper().strip()
            == "REJECTED"
        )
        position["native_risk_state"] = (
            "REJECTED" if rejected else ("CONFIRMED" if confirmation else "ATTACHED")
        )
        position["native_risk_deal_reference"] = ref or None
        position["native_risk_reason"] = confirmation.get("reason")
    except Exception as exc:
        # The TradeExcursionTracker is patched below to retry both stop+limit
        # and to enforce server-observed exits as a fallback.
        position["native_risk_state"] = "TRACKER_FALLBACK"
        position["native_risk_error"] = _compact_error(exc)


def _wr_guarded_category_open_candidate(
    self: CategoryExecutionEngine,
    candidate: Dict[str, Any],
    external: List[Dict[str, Any]],
) -> None:
    if not bool(candidate.get("standard_eligible")):
        return
    if not bool(candidate.get("validated_execution_gate")):
        return
    before = {
        str(row.get("deal_id") or "")
        for row in self._open_positions()
        if row.get("deal_id")
    }
    _adaptive_category_open_candidate(self, candidate, external)

    opened = next(
        (
            row
            for row in reversed(self._open_positions())
            if str(row.get("deal_id") or "") not in before
        ),
        None,
    )
    if not isinstance(opened, dict):
        return

    plan = _candidate_exit_plan(candidate)
    opened.update(plan)
    opened["market_regime"] = candidate.get("market_regime") or candidate.get("regime")
    opened["entry_signal_timestamp"] = (
        candidate.get("signal_timestamp")
        or candidate.get("evaluated_at")
    )
    opened["entry_live_price"] = candidate.get("live_price")
    opened["calibrated_execution_confidence_pct"] = candidate.get(
        "calibrated_execution_confidence_pct"
    )
    opened["historical_win_rate"] = candidate.get("historical_win_rate")
    opened["historical_profit_factor"] = candidate.get("historical_profit_factor")
    opened["walk_forward_pass"] = candidate.get("walk_forward_pass")
    _attach_initial_native_risk(self, opened)


def _continuation_confirmation(
    engine: CategoryExecutionEngine,
    candidate: Dict[str, Any],
) -> tuple[bool, str]:
    """Require a fresh second observation that confirms the predicted direction."""
    health = _execution_health_state(engine)
    states = health.setdefault("signal_confirmations", {})
    key = _candidate_key(candidate)
    now = _now()
    evaluated_at = _to_float(
        candidate.get("evaluated_at")
        or candidate.get("signal_timestamp"),
        now,
    )
    regime = str(
        candidate.get("market_regime")
        or candidate.get("regime")
        or "UNKNOWN"
    ).upper()
    direction = str(candidate.get("direction") or "").upper()
    live_price = _to_float(candidate.get("live_price"), 0.0)
    fast = _to_float(
        candidate.get("live_fast_score")
        if candidate.get("live_fast_score") is not None
        else candidate.get("smart_fast_score"),
        0.0,
    )

    prior = states.get(key)
    if not isinstance(prior, dict):
        states[key] = {
            "first_seen_at": now,
            "evaluated_at": evaluated_at,
            "regime": regime,
            "direction": direction,
            "live_price": live_price,
            "fast": fast,
        }
        return False, "AWAITING_CONTINUATION_CONFIRMATION"

    # It must be a genuinely newer specialist evaluation, not repeated polling
    # of exactly the same snapshot.
    prior_eval = _to_float(prior.get("evaluated_at"), 0.0)
    if evaluated_at <= prior_eval + 1e-6:
        return False, "AWAITING_NEW_SIGNAL_SNAPSHOT"

    if direction != str(prior.get("direction") or "").upper():
        states[key] = {
            "first_seen_at": now,
            "evaluated_at": evaluated_at,
            "regime": regime,
            "direction": direction,
            "live_price": live_price,
            "fast": fast,
        }
        return False, "DIRECTION_CHANGED_RESET_CONFIRMATION"

    if regime != str(prior.get("regime") or "").upper():
        states[key] = {
            "first_seen_at": now,
            "evaluated_at": evaluated_at,
            "regime": regime,
            "direction": direction,
            "live_price": live_price,
            "fast": fast,
        }
        return False, "REGIME_CHANGED_RESET_CONFIRMATION"

    recent: List[float] = []
    for raw in (candidate.get("recent_returns") or [])[-3:]:
        try:
            recent.append(float(raw))
        except Exception:
            continue
    momentum_confirmed = False
    if recent:
        signed = sum(recent)
        momentum_confirmed = (
            signed > 0 if direction == "BUY" else signed < 0
        )

    prior_price = _to_float(prior.get("live_price"), 0.0)
    price_confirmed = False
    if live_price > 0 and prior_price > 0:
        price_confirmed = (
            live_price > prior_price if direction == "BUY" else live_price < prior_price
        )

    fast_stable = fast >= max(45.0, _to_float(prior.get("fast"), fast) - 12.0)
    confirmed = bool((momentum_confirmed or price_confirmed) and fast_stable)
    if confirmed:
        states.pop(key, None)
        return True, "CONTINUATION_CONFIRMED"

    states[key] = {
        "first_seen_at": prior.get("first_seen_at") or now,
        "evaluated_at": evaluated_at,
        "regime": regime,
        "direction": direction,
        "live_price": live_price,
        "fast": fast,
    }
    return False, "CONTINUATION_NOT_CONFIRMED"


def _reentry_reset_gate(
    engine: CategoryExecutionEngine,
    candidate: Dict[str, Any],
) -> tuple[bool, str]:
    symbol = str(candidate.get("symbol") or "").upper().strip()
    direction = str(candidate.get("direction") or "").upper().strip()
    if not symbol or direction not in {"BUY", "SELL"}:
        return False, "REENTRY_IDENTITY_MISSING"

    latest: Optional[Dict[str, Any]] = None
    for row in engine._state.setdefault("positions", []):
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol") or "").upper().strip() != symbol:
            continue
        if str(row.get("direction") or "").upper().strip() != direction:
            continue
        if str(row.get("status") or "").upper() == "OPEN":
            continue
        closed_at = _to_float(row.get("closed_at"), 0.0)
        if closed_at <= 0:
            continue
        if latest is None or closed_at > _to_float(latest.get("closed_at"), 0.0):
            latest = row

    if latest is None:
        return True, "NO_RECENT_SAME_DIRECTION_CLOSE"

    age = max(0.0, _now() - _to_float(latest.get("closed_at"), 0.0))
    try:
        minimum_reset = int(
            os.getenv("WR_GUARD_REENTRY_MIN_RESET_SECONDS", "1800")
        )
    except Exception:
        minimum_reset = 1800
    minimum_reset = max(600, min(7200, minimum_reset))

    previous_regime = str(latest.get("market_regime") or "").upper().strip()
    new_regime = str(
        candidate.get("market_regime")
        or candidate.get("regime")
        or ""
    ).upper().strip()
    regime_changed = bool(
        previous_regime
        and new_regime
        and previous_regime != new_regime
    )

    if age < minimum_reset and not regime_changed:
        return False, "POST_TRADE_RESET_NOT_ESTABLISHED"
    return True, (
        "REGIME_RESET_CONFIRMED"
        if regime_changed
        else "POST_TRADE_RESET_TIME_AND_NEW_CONFIRMATION"
    )


def _tracker_context(self: TradeExcursionTracker, record: Dict[str, Any]) -> None:
    portfolio = getattr(self, "_jasong_category_portfolio", None)
    deal_id = str(record.get("deal_id") or "").strip()
    if portfolio is not None and deal_id:
        try:
            with portfolio._lock:
                rows = list(portfolio._state.get("positions") or [])
            match = next(
                (
                    row
                    for row in rows
                    if str(row.get("deal_id") or "").strip() == deal_id
                ),
                None,
            )
            if isinstance(match, dict):
                for field in (
                    "category",
                    "strategy_id",
                    "planned_stop_pct",
                    "planned_take_profit_pct",
                    "planned_stop_level",
                    "planned_take_profit_level",
                    "planned_reward_r",
                    "entry_volatility_pct",
                    "exit_policy_version",
                    "market_regime",
                ):
                    if match.get(field) is not None:
                        record[field] = match.get(field)
        except Exception:
            pass

    if not record.get("category"):
        ref = str(record.get("deal_reference") or "").upper()
        prefix_map = {
            "JSCAT_FOR_": "FOREX",
            "JSCAT_IND_": "INDICES",
            "JSCAT_CRY_": "CRYPTO",
            "JSCAT_MET_": "METALS",
            "JSCAT_ENE_": "ENERGY",
            "JSCAT_SHA_": "SHARES",
        }
        for prefix, category in prefix_map.items():
            if ref.startswith(prefix):
                record["category"] = category
                break


def _record_target_pct(self: TradeExcursionTracker, record: Dict[str, Any]) -> float:
    _tracker_context(self, record)
    planned = _to_float(record.get("planned_take_profit_pct"), 0.0)
    if str(record.get("deal_reference") or "").upper().startswith("JSCAT_") and planned > 0:
        return planned
    return float(self.take_profit_pct)


def _record_stop_pct(self: TradeExcursionTracker, record: Dict[str, Any]) -> float:
    _tracker_context(self, record)
    if not str(record.get("deal_reference") or "").upper().startswith("JSCAT_"):
        return 0.0
    return max(0.0, _to_float(record.get("planned_stop_pct"), 0.0))


def _risk_aware_update_exit_fields(
    self: TradeExcursionTracker,
    record: Dict[str, Any],
    now: float,
) -> bool:
    """Category-aware TP/SL/trailing trigger; 30% remains legacy non-Category."""
    entry = self._safe_float(record.get("entry_price"))
    direction = str(record.get("direction") or "").upper().strip()
    current = self._safe_float(record.get("current_price"))
    target_pct = _record_target_pct(self, record)
    stop_pct = _record_stop_pct(self, record)

    record["take_profit_enabled"] = bool(self.take_profit_enabled)
    record["take_profit_target_pct"] = self._round(target_pct, 6)
    record["take_profit_basis"] = (
        "VOLATILITY_R_TARGET_FROM_ENTRY"
        if stop_pct > 0
        else "ENTRY_PRICE_FAVOURABLE_MOVE_PCT"
    )
    record["protective_stop_pct"] = self._round(stop_pct, 6) if stop_pct > 0 else None
    record["exit_policy_version"] = (
        record.get("exit_policy_version")
        or ("V5_VOLATILITY_R" if stop_pct > 0 else "LEGACY_TP")
    )

    target_price = None
    stop_price = None
    if entry is not None and entry > 0:
        if direction == "BUY":
            target_price = entry * (1.0 + target_pct / 100.0)
            stop_price = (
                entry * (1.0 - stop_pct / 100.0)
                if stop_pct > 0
                else None
            )
        elif direction == "SELL":
            target_price = entry * (1.0 - target_pct / 100.0)
            stop_price = (
                entry * (1.0 + stop_pct / 100.0)
                if stop_pct > 0
                else None
            )
        record["take_profit_target_price"] = self._round(target_price)
        record["protective_stop_price"] = self._round(stop_price)

    favourable = self._current_favourable_pct(record)
    record["current_favourable_pct"] = self._round(favourable, 6)
    mfe_pct = self._safe_float(record.get("mfe_pct"))
    reached_ever = bool(mfe_pct is not None and mfe_pct >= target_pct)
    record["take_profit_reached"] = reached_ever
    if reached_ever and record.get("take_profit_reached_at") is None:
        record["take_profit_reached_at"] = now
        record["take_profit_first_reached_price"] = self._round(current)

    # Dynamic profit protection after the trade proves itself.
    desired_stop = stop_price
    if stop_pct > 0 and entry is not None and entry > 0 and mfe_pct is not None:
        if mfe_pct >= stop_pct:
            lock_pct = stop_pct * 0.35
            record["trailing_state"] = "LOCK_0_35R"
        elif mfe_pct >= stop_pct * 0.75:
            lock_pct = stop_pct * 0.10
            record["trailing_state"] = "LOCK_0_10R"
        else:
            lock_pct = None
            record["trailing_state"] = "INITIAL_RISK"
        if lock_pct is not None:
            if direction == "BUY":
                desired_stop = entry * (1.0 + lock_pct / 100.0)
            elif direction == "SELL":
                desired_stop = entry * (1.0 - lock_pct / 100.0)
    record["desired_native_stop_price"] = self._round(desired_stop)

    if not self.take_profit_enabled:
        return False
    if not bool(record.get("jasong_owned")):
        return False
    if str(record.get("status") or "").upper() != "OPEN":
        return False
    if favourable is None:
        return False

    trigger_reason: Optional[str] = None
    if stop_pct > 0 and favourable <= -stop_pct:
        trigger_reason = "PROTECTIVE_STOP"
    elif favourable >= target_pct:
        trigger_reason = "VOLATILITY_R_TAKE_PROFIT" if stop_pct > 0 else "TAKE_PROFIT"

    if not trigger_reason:
        return False

    state = str(record.get("take_profit_close_state") or "").upper()
    last_attempt = self._safe_float(record.get("take_profit_last_attempt_at")) or 0.0
    if state in {"CLOSED", "CLOSE_VERIFIED"}:
        return False
    if state in {"TRIGGERED", "CLOSE_SENT", "CLOSE_PENDING", "ERROR", "DEFERRED_MARKET_CLOSED"}:
        if (now - last_attempt) < self.take_profit_retry_seconds:
            return False

    record["exit_trigger_reason"] = trigger_reason
    record["take_profit_close_state"] = "TRIGGERED"
    record["take_profit_triggered_at"] = record.get("take_profit_triggered_at") or now
    record["take_profit_trigger_price"] = self._round(current)
    record["take_profit_trigger_favourable_pct"] = self._round(favourable, 6)
    return True


def _risk_aware_native_needed(
    self: TradeExcursionTracker,
    record: Dict[str, Any],
    now: float,
) -> bool:
    if not self.take_profit_enabled or not bool(record.get("jasong_owned")):
        return False
    if str(record.get("status") or "").upper() != "OPEN":
        return False

    target = self._safe_float(record.get("take_profit_target_price"))
    desired_stop = self._safe_float(record.get("desired_native_stop_price"))
    if target is None or target <= 0:
        return False

    target_attached = self._safe_float(record.get("native_take_profit_level"))
    stop_attached = self._safe_float(record.get("native_stop_level"))
    tolerance = max(1e-8, abs(target) * 1e-9)
    target_ok = bool(
        target_attached is not None
        and abs(target_attached - target) <= tolerance
    )
    if desired_stop is not None and desired_stop > 0:
        stop_tolerance = max(1e-8, abs(desired_stop) * 1e-9)
        stop_ok = bool(
            stop_attached is not None
            and abs(stop_attached - desired_stop) <= stop_tolerance
        )
    else:
        stop_ok = True

    if target_ok and stop_ok:
        return False

    last_attempt = self._safe_float(record.get("native_take_profit_last_attempt_at")) or 0.0
    # Initial protection should be attempted promptly; subsequent changes obey
    # a shorter 60s minimum to make trailing protection meaningful.
    retry = min(self.take_profit_native_retry_seconds, 60)
    return (now - last_attempt) >= retry


def _risk_aware_attach_native(self: TradeExcursionTracker, deal_id: str) -> None:
    now = time.time()
    with self._lock:
        record = self._state.setdefault("trades", {}).get(str(deal_id))
        if not isinstance(record, dict):
            return
        _tracker_context(self, record)
        target = self._safe_float(record.get("take_profit_target_price"))
        desired_stop = self._safe_float(record.get("desired_native_stop_price"))
        if target is None or target <= 0:
            return
        payload: Dict[str, Any] = {"limitLevel": float(target)}
        if desired_stop is not None and desired_stop > 0:
            payload["stopLevel"] = float(desired_stop)
        record["native_take_profit_last_attempt_at"] = now
        record["native_take_profit_attempts"] = int(
            record.get("native_take_profit_attempts") or 0
        ) + 1
        record["native_take_profit_state"] = "ATTACHING"
        self._persist()

    try:
        request_fn = getattr(self.broker, "_request", None)
        if not callable(request_fn):
            raise RuntimeError("IG broker update-position request method unavailable")
        acknowledgement = request_fn(
            "PUT",
            f"/positions/otc/{deal_id}",
            version=2,
            payload=payload,
        ) or {}
        ref = str(acknowledgement.get("dealReference") or "").strip()
        confirmation: Dict[str, Any] = {}
        confirm_fn = getattr(self.broker, "confirm", None)
        if ref and callable(confirm_fn):
            confirmation = confirm_fn(ref) or {}
        rejected = (
            str(confirmation.get("dealStatus") or "").upper().strip()
            == "REJECTED"
        )
        with self._lock:
            record = self._state.setdefault("trades", {}).get(str(deal_id))
            if not isinstance(record, dict):
                return
            record["native_take_profit_deal_reference"] = ref or None
            record["native_take_profit_level"] = self._round(target)
            if desired_stop is not None and desired_stop > 0:
                record["native_stop_level"] = self._round(desired_stop)
            record["native_risk_payload"] = {
                "limitLevel": self._round(target),
                "stopLevel": self._round(desired_stop),
            }
            if rejected:
                record["native_take_profit_state"] = "REJECTED"
                record["native_take_profit_error"] = str(
                    confirmation.get("reason") or confirmation
                )
            else:
                record["native_take_profit_state"] = (
                    "CONFIRMED" if confirmation else "ATTACHED"
                )
                record["native_take_profit_attached_at"] = time.time()
            self._persist()
    except Exception as exc:
        with self._lock:
            record = self._state.setdefault("trades", {}).get(str(deal_id))
            if isinstance(record, dict):
                record["native_take_profit_state"] = "ERROR"
                record["native_take_profit_error"] = _compact_error(exc)
                record["native_take_profit_last_attempt_at"] = time.time()
                self._persist()


def _risk_aware_execute_close(
    self: TradeExcursionTracker,
    deal_id: str,
) -> None:
    now = time.time()
    with self._lock:
        record = self._state.setdefault("trades", {}).get(str(deal_id))
        if not isinstance(record, dict):
            return
        reason = str(record.get("exit_trigger_reason") or "EXIT_INTELLIGENCE")
        record["take_profit_last_attempt_at"] = now
        record["take_profit_close_attempts"] = int(
            record.get("take_profit_close_attempts") or 0
        ) + 1
        record["take_profit_close_state"] = "CLOSE_PENDING"
        self._persist()

    try:
        result = self.broker.close_position(str(deal_id)) or {}
        status = str(result.get("status") or result.get("dealStatus") or "").upper().strip()
        verified = bool(result.get("closeVerified"))
        success = verified or status in {"ACCEPTED", "ALREADY_CLOSED_OR_NOT_FOUND"}
        deferred = status == "CLOSE_DEFERRED_MARKET_CLOSED"
        compact_result = {
            "status": status or None,
            "dealStatus": result.get("dealStatus"),
            "reason": result.get("reason"),
            "closeVerified": verified,
            "level": result.get("level"),
            "profit": result.get("profit"),
            "profitLoss": result.get("profitLoss"),
            "pnl": result.get("pnl"),
            "profitCurrency": result.get("profitCurrency"),
        }
        with self._lock:
            record = self._state.setdefault("trades", {}).get(str(deal_id))
            if not isinstance(record, dict):
                return
            record["take_profit_close_result"] = compact_result
            record["exit_close_result"] = compact_result
            if success:
                record["take_profit_close_state"] = (
                    "CLOSE_VERIFIED" if verified else "CLOSE_SENT"
                )
                record["take_profit_closed_at"] = time.time()
                record["close_reason"] = reason
                pnl = (
                    result.get("profitLoss")
                    if result.get("profitLoss") is not None
                    else result.get("pnl")
                )
                if pnl is None:
                    pnl = result.get("profit")
                if pnl is not None:
                    record["broker_pnl"] = pnl
            elif deferred:
                record["take_profit_close_state"] = "DEFERRED_MARKET_CLOSED"
            else:
                record["take_profit_close_state"] = status or "CLOSE_PENDING"
            self._persist()
    except Exception as exc:
        with self._lock:
            record = self._state.setdefault("trades", {}).get(str(deal_id))
            if isinstance(record, dict):
                record["take_profit_close_state"] = "ERROR"
                record["take_profit_close_error"] = _compact_error(exc)
                record["take_profit_last_attempt_at"] = time.time()
                self._persist()


def _risk_aware_merge(
    self: TradeExcursionTracker,
    row: Dict[str, Any],
) -> Dict[str, Any]:
    out = _ORIGINAL_TRACKER_MERGE(self, row)
    key = str(
        out.get("deal_id")
        or out.get("ig_deal_id")
        or out.get("trade_id")
        or ""
    ).strip()
    excursion = self._lookup(key) if key else None
    if isinstance(excursion, dict):
        for field in (
            "category",
            "strategy_id",
            "planned_stop_pct",
            "planned_take_profit_pct",
            "planned_stop_level",
            "planned_take_profit_level",
            "planned_reward_r",
            "entry_volatility_pct",
            "protective_stop_pct",
            "protective_stop_price",
            "desired_native_stop_price",
            "native_stop_level",
            "trailing_state",
            "exit_trigger_reason",
            "exit_policy_version",
            "broker_pnl",
        ):
            if excursion.get(field) is not None:
                out[field] = excursion.get(field)
    return out


def _risk_aware_tracker_status(
    self: TradeExcursionTracker,
) -> Dict[str, Any]:
    out = _ORIGINAL_TRACKER_STATUS(self)
    out.update(
        {
            "category_exit_intelligence_enabled": True,
            "category_exit_policy": "VOLATILITY_R_NATIVE_STOP_LIMIT_PLUS_TRAILING",
            "legacy_30pct_scope": "NON_CATEGORY_JASONG_POSITIONS_ONLY",
            "category_stop_basis": "MEDIAN_ABS_LAST20_RETURNS_X2_5_WITH_CATEGORY_CLAMPS",
            "category_target_basis": "PLANNED_STOP_PCT_X_CATEGORY_REWARD_R",
            "protective_stop_execution": "IG_DEMO_NATIVE_STOP_LEVEL_PLUS_SERVER_FALLBACK",
        }
    )
    return out


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

    if bool(row.get("strong_qualified")) and not bool(row.get("standard_eligible")):
        if not bool(row.get("historical_execution_gate_pass")):
            reasons.append("HISTORICAL_VALIDATION_GATE_FAIL")
        if not bool(row.get("forward_safety_gate_pass", True)):
            reasons.append("FORWARD_WR_SAFETY_GATE_FAIL")
        if bool(row.get("strategy_quarantined")):
            reasons.append("STRATEGY_QUARANTINED")
        if bool(row.get("unknown_strategy_blocked")):
            reasons.append("UNKNOWN_STRATEGY_ATTRIBUTION")
        if _to_float(row.get("calibrated_execution_confidence"), 0.0) < WR_GUARD_CALIBRATED_MIN:
            reasons.append("CALIBRATED_EXECUTION_SCORE_BELOW_60")

    return list(dict.fromkeys(reasons))


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

                        # V5: standard_eligible is an absolute validated
                        # execution prerequisite. Live STRONG alone is WATCH.
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

                        reentry_ok, reentry_reason = _reentry_reset_gate(
                            self,
                            candidate,
                        )
                        if not reentry_ok:
                            blockers[reentry_reason] += 1
                            health["blocked_eligible_this_tick"] += 1
                            health["last_blocked_candidate"] = {
                                "at": now,
                                "candidate": key,
                                "reason": reentry_reason,
                            }
                            continue

                        confirmation_ok, confirmation_reason = _continuation_confirmation(
                            self,
                            candidate,
                        )
                        if not confirmation_ok:
                            blockers[confirmation_reason] += 1
                            health["blocked_eligible_this_tick"] += 1
                            health["last_blocked_candidate"] = {
                                "at": now,
                                "candidate": key,
                                "reason": confirmation_reason,
                            }
                            continue

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
        "historical_validation_mode": "STANDARD_EXECUTION_GATE",
        "historical_execution_veto": True,
        "historical_min_win_rate_pct": 60.0,
        "historical_min_profit_factor": 1.20,
        "walk_forward_required": True,
        "forward_wr_safety_floor_pct_after_20_trades": 50.0,
        "strong_is_watch_only": True,
        "prime_authority": "BROKER_SETTLED_FORWARD_PLUS_VALIDATED_STANDARD_GATE",
        "execution_mode": "IG_DEMO_ONLY",
    }
    return out


def _reliable_strategy_loop(self: CategoryStrategyEngine) -> None:
    """Keep rolling live candidate refresh active while optimisation runs.

    V5 uses validated holdout/WF evidence as a standard-entry gate, but a running
    refresh is not allowed to freeze live scanning. The last completed validation
    evidence remains the gate until the refreshed evaluation replaces it.
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
        removed_variants = _patch_quarantined_strategy_variants()

        # Entry-policy repair: restore validated standard eligibility and prevent
        # category_rankings() from re-promoting WATCH/STRONG signals.
        ForwardPrimeArchitecture.enrich = _guarded_forward_enrich
        ForwardPrimeArchitecture.category_rankings = _guarded_forward_rankings
        ForwardPrimeArchitecture._category_rows = _enhanced_category_rows

        # Exit/risk intelligence for new Category positions.
        TradeExcursionTracker._update_take_profit_fields = _risk_aware_update_exit_fields
        TradeExcursionTracker._native_take_profit_needed = _risk_aware_native_needed
        TradeExcursionTracker._attach_native_take_profit = _risk_aware_attach_native
        TradeExcursionTracker._execute_take_profit_close = _risk_aware_execute_close
        TradeExcursionTracker.merge = _risk_aware_merge
        TradeExcursionTracker.status = _risk_aware_tracker_status

        CategoryExecutionEngine._reconcile = _optimized_category_reconcile
        CategoryExecutionEngine._open_candidate = _wr_guarded_category_open_candidate
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
            "validated_standard_execution_gate": True,
            "failing_variants_removed_from_optimizer": {
                "INDICES": ["INDEX_SESSION_MOMENTUM_V1"],
                "METALS": ["METALS_BREAKOUT_V2"],
                "ENERGY": ["ENERGY_TREND_V2"],
            },
            "strong_is_watch_only": True,
            "seeded_strategy_quarantine": True,
            "optimizer_removed_failing_variants": removed_variants,
            "forward_wr_safety_gate": True,
            "continuation_confirmation_required": True,
            "post_trade_reentry_reset_required": True,
            "category_volatility_r_exit_intelligence": True,
            "adaptive_category_size_downshift": True,
            "minimum_deal_size_floor_enforced": True,
            "live_refresh_independent_of_historical_refresh": True,
            "historical_validation_execution_gate": True,
            "historical_refresh_blocks_scanner": False,
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

        excursion_tracker = system.get("excursion_tracker")
        if excursion_tracker is not None:
            try:
                excursion_tracker._jasong_category_portfolio = portfolio
            except Exception:
                pass

        # Force one background revalidation pass so markets previously optimized
        # onto a removed family can be re-selected from the surviving variants.
        try:
            state = getattr(intelligence, "_state", None)
            if isinstance(state, dict) and not bool(state.get("wr_guard_v5_refresh_started")):
                state["wr_guard_v5_refresh_started"] = True
                starter = getattr(intelligence, "start_full_refresh", None)
                if callable(starter):
                    starter(force=True)
        except Exception:
            pass

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
            "validated_standard_execution_gate": True,
            "strong_is_watch_only": True,
            "continuation_confirmation_required": True,
            "post_trade_reentry_reset_required": True,
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
    live_strong = 0
    standard = 0
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
            cat_live_strong = sum(
                1 for row in rows if bool(row.get("strong_qualified"))
            )
            cat_standard = sum(
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
            live_strong += cat_live_strong
            standard += cat_standard
            prime += cat_prime
            by_category[category] = {
                "ranked": cat_ranked,
                "strong": cat_live_strong,
                "standard_eligible": cat_standard,
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
    elif standard <= 0 and live_strong > 0:
        flow_state = "WAITING_FOR_VALIDATED_STANDARD"
    elif live_strong <= 0:
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
        "strategy_unchanged": False,
        "entry_policy": {
            "quant_min_pct": 28.0,
            "model_ai_min_pct": 40.0,
            "live_fast_min": 45.0,
            "historical_validation_mode": "STANDARD_EXECUTION_GATE",
            "historical_execution_veto": True,
            "historical_min_win_rate_pct": 60.0,
            "historical_min_profit_factor": 1.20,
            "walk_forward_required": True,
            "forward_wr_safety_floor_pct_after_20_trades": 50.0,
            "seeded_quarantines": list(WR_GUARD_SEEDED_QUARANTINES),
            "calibrated_execution_score_min_pct": 60.0,
            "continuation_confirmation_required": True,
            "post_trade_reentry_reset_required": True,
            "prime_authority": "BROKER_SETTLED_FORWARD_PLUS_VALIDATED_STANDARD_GATE",
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
            "live_strong_candidates": live_strong,
            "standard_eligible_candidates": standard,
            "strong_candidates": live_strong,
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
            "note": "refresh concurrency does not block scanner; completed validation evidence gates entries",
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
            "historical_only_reasons_excluded": False,
            "historical_validation_is_execution_gate": True,
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
            "validated_standard_execution_gate": True,
            "strong_is_watch_only": True,
            "continuation_confirmation_required": True,
            "post_trade_reentry_reset_required": True,
            "category_exit_policy": "VOLATILITY_R",
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
            "category_native_stop_and_limit": True,
            "legacy_30pct_tp_scope": "NON_CATEGORY_JASONG_POSITIONS_ONLY",
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

