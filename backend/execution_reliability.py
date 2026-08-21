from __future__ import annotations

import copy
import hashlib
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
from forward_store import ForwardStore
from forward_validation import ForwardValidationEngine
from trade_excursions import TradeExcursionTracker


VERSION = "6.9.4-adaptive-forward-v6"


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
_ORIGINAL_FORWARD_STORE_SYNC = ForwardStore.sync
_ORIGINAL_FORWARD_VALIDATOR_SYNC = ForwardValidationEngine.sync
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


def _batched_forward_store_sync(
    self: ForwardStore,
    rows: Any,
) -> int:
    """Persist one forward evidence batch in one SQLite transaction."""
    valid: List[Dict[str, Any]] = []
    for raw in rows or []:
        if not isinstance(raw, dict):
            continue
        trade_id = str(
            raw.get("trade_id")
            or raw.get("ig_deal_id")
            or raw.get("deal_id")
            or ""
        ).strip()
        result = str(
            raw.get("broker_result")
            or raw.get("result")
            or ""
        ).upper().strip()
        if trade_id and result in {"WIN", "LOSS"}:
            row = dict(raw)
            row["trade_id"] = trade_id
            row["broker_result"] = result
            valid.append(row)
    if not valid:
        return 0

    sql = """
        INSERT INTO forward_trades (
            trade_id, market, symbol, strategy_id, direction, broker_result,
            opened_at, closed_at, entry_level, exit_level, broker_pnl,
            r_multiple, r_source, entry_snapshot_json, provenance_json, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(trade_id) DO UPDATE SET
            market=excluded.market,
            symbol=excluded.symbol,
            strategy_id=excluded.strategy_id,
            direction=excluded.direction,
            broker_result=excluded.broker_result,
            opened_at=excluded.opened_at,
            closed_at=excluded.closed_at,
            entry_level=excluded.entry_level,
            exit_level=excluded.exit_level,
            broker_pnl=excluded.broker_pnl,
            r_multiple=excluded.r_multiple,
            r_source=excluded.r_source,
            provenance_json=excluded.provenance_json,
            raw_json=excluded.raw_json,
            updated_at=strftime('%s','now')
    """

    values = []
    for row in valid:
        values.append((
            row["trade_id"],
            row.get("market") or row.get("symbol"),
            row.get("symbol") or row.get("market"),
            row.get("strategy_id") or row.get("selected_strategy") or "UNKNOWN",
            str(row.get("direction") or "").upper(),
            row["broker_result"],
            row.get("opened_at") or row.get("entry_time"),
            row.get("closed_at"),
            row.get("entry_level") or row.get("broker_entry_level"),
            row.get("exit_level") or row.get("broker_exit_level"),
            row.get("broker_pnl"),
            row.get("r_multiple"),
            row.get("r_source"),
            self._json(row.get("entry_snapshot") or row.get("signal_snapshot") or {}),
            self._json(row.get("provenance") or {}),
            self._json(row),
        ))

    with self._lock, self._connect() as db:
        db.executemany(sql, values)
        db.commit()
    return len(values)


def _forward_evidence_fingerprint(rows: List[Dict[str, Any]]) -> str:
    compact = []
    for row in rows:
        compact.append((
            str(row.get("trade_id") or ""),
            str(row.get("strategy_id") or "UNKNOWN"),
            str(row.get("broker_result") or ""),
            str(row.get("r_source") or ""),
            round(_to_float(row.get("r_multiple"), 0.0), 8),
            str(row.get("broker_pnl")),
            str(row.get("closed_at")),
            str(row.get("exit_level") or row.get("broker_exit_level")),
        ))
    compact.sort()
    return hashlib.sha256(repr(compact).encode("utf-8", errors="replace")).hexdigest()


def _singleflight_forward_validator_sync(
    self: ForwardValidationEngine,
) -> int:
    """Write settled forward evidence only when the normalized evidence changed."""
    lock = getattr(self, "_jasong_forward_sync_lock", None)
    if lock is None:
        lock = threading.Lock()
        self._jasong_forward_sync_lock = lock

    if not lock.acquire(blocking=False):
        self._jasong_forward_sync_skips = int(
            getattr(self, "_jasong_forward_sync_skips", 0) or 0
        ) + 1
        return 0

    started = _now()
    try:
        normalized: List[Dict[str, Any]] = []
        try:
            source = self.evidence_source() or []
        except Exception:
            source = []
        for raw in source:
            if not isinstance(raw, dict):
                continue
            row = self._normalise(raw)
            if row:
                normalized.append(row)

        fingerprint = _forward_evidence_fingerprint(normalized)
        previous = getattr(self, "_jasong_forward_sync_fingerprint", None)
        self._jasong_forward_sync_last_scanned_at = _now()
        self._jasong_forward_sync_last_rows = len(normalized)
        if previous == fingerprint:
            self._jasong_forward_sync_unchanged = int(
                getattr(self, "_jasong_forward_sync_unchanged", 0) or 0
            ) + 1
            self._jasong_forward_sync_last_duration_seconds = round(
                _now() - started, 3
            )
            return 0

        count = self.store.sync(normalized)
        self._jasong_forward_sync_fingerprint = fingerprint
        self._jasong_forward_sync_last_changed_at = _now()
        self._jasong_forward_sync_last_duration_seconds = round(
            _now() - started, 3
        )
        self._jasong_forward_sync_last_write_count = count
        return count
    finally:
        lock.release()


def _compute_guarded_forward_rankings(
    self: ForwardPrimeArchitecture,
    *args: Any,
    **kwargs: Any,
) -> Dict[str, List[Dict[str, Any]]]:
    """Compute one authoritative V5 validated ranking snapshot."""
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


def _rankings_cache_key(args: Any, kwargs: Dict[str, Any]) -> str:
    category = kwargs.get("category")
    top_n = kwargs.get("top_n")
    if category is None and len(args) >= 1:
        category = args[0]
    if top_n is None and len(args) >= 2:
        top_n = args[1]
    if top_n is None:
        top_n = 5
    return f"{str(category or 'ALL').upper().strip()}|{int(top_n)}"


def _rankings_refresh_seconds() -> float:
    try:
        value = float(os.getenv("JASONG_FORWARD_RANKINGS_REFRESH_SECONDS", "60"))
    except Exception:
        value = 60.0
    return max(20.0, min(120.0, value))


def _rankings_stale_max_seconds() -> float:
    try:
        value = float(os.getenv("JASONG_FORWARD_RANKINGS_STALE_MAX_SECONDS", "150"))
    except Exception:
        value = 150.0
    return max(60.0, min(180.0, value))


def _historical_refresh_running(forward_prime: ForwardPrimeArchitecture) -> bool:
    intelligence = getattr(forward_prime, "intelligence", None)
    state = getattr(intelligence, "_state", {}) if intelligence is not None else {}
    if not isinstance(state, dict):
        return False
    refresh = state.get("full_refresh")
    return bool(
        isinstance(refresh, dict)
        and str(refresh.get("status") or "").upper() == "RUNNING"
    )


def _age_cached_rankings(
    self: ForwardPrimeArchitecture,
    rankings: Dict[str, List[Dict[str, Any]]],
    cache_age: float,
) -> Dict[str, List[Dict[str, Any]]]:
    """Age cached signal/quote freshness before a caller can execute it."""
    output = copy.deepcopy(rankings)
    signal_limit = float(getattr(self, "signal_max_age_seconds", 300.0) or 300.0)
    quote_limit = float(getattr(self, "quote_max_age_seconds", 180.0) or 180.0)
    for rows in output.values():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            row["forward_rankings_cache_age_seconds"] = round(cache_age, 2)
            signal_age = row.get("signal_age_seconds")
            quote_age = row.get("quote_age_seconds")
            current_signal_age = None
            current_quote_age = None
            if signal_age is not None:
                current_signal_age = max(0.0, _to_float(signal_age, 0.0) + cache_age)
                row["signal_age_seconds"] = round(current_signal_age, 3)
            if quote_age is not None:
                current_quote_age = max(0.0, _to_float(quote_age, 0.0) + cache_age)
                row["quote_age_seconds"] = round(current_quote_age, 3)

            stale_reasons: List[str] = []
            if current_signal_age is not None and current_signal_age > signal_limit:
                stale_reasons.append("SIGNAL_STALE")
            if current_quote_age is not None and current_quote_age > quote_limit:
                stale_reasons.append("BROKER_QUOTE_STALE")
            if stale_reasons:
                provenance = row.get("provenance")
                provenance = dict(provenance) if isinstance(provenance, dict) else {}
                issues = [str(x) for x in (provenance.get("issues") or [])]
                for reason in stale_reasons:
                    if reason not in issues:
                        issues.append(reason)
                provenance["issues"] = issues
                provenance["fresh"] = False
                row["provenance"] = provenance
                reasons = [str(x) for x in (row.get("rejection_reasons") or [])]
                for reason in stale_reasons:
                    if reason not in reasons:
                        reasons.append(reason)
                row["rejection_reasons"] = reasons
                for field in (
                    "strong_qualified", "standard_eligible", "trade_eligible",
                    "ig_demo_learning_eligible", "prime_qualified",
                    "execution_eligible", "eligible", "compound_eligible",
                ):
                    row[field] = False
    return output


def _forward_rankings_refresh_worker(
    self: ForwardPrimeArchitecture,
    cache_key: str,
    args: Any,
    kwargs: Dict[str, Any],
) -> None:
    started = _now()
    lock = getattr(self, "_jasong_rankings_cache_lock")
    try:
        output = _compute_guarded_forward_rankings(self, *args, **kwargs)
        with lock:
            cache = getattr(self, "_jasong_rankings_cache", {})
            cache_at = getattr(self, "_jasong_rankings_cache_at", {})
            cache[cache_key] = copy.deepcopy(output)
            cache_at[cache_key] = _now()
            self._jasong_rankings_cache = cache
            self._jasong_rankings_cache_at = cache_at
            self._jasong_rankings_last_error = None
            self._jasong_rankings_last_refresh_duration_seconds = round(_now() - started, 3)
            self._jasong_rankings_last_refresh_at = _now()
    except Exception as exc:
        with lock:
            self._jasong_rankings_last_error = _compact_error(exc)
            self._jasong_rankings_last_refresh_duration_seconds = round(_now() - started, 3)
    finally:
        with lock:
            refreshing = getattr(self, "_jasong_rankings_refreshing", set())
            refreshing.discard(cache_key)
            self._jasong_rankings_refreshing = refreshing


def _cached_guarded_forward_rankings(
    self: ForwardPrimeArchitecture,
    *args: Any,
    **kwargs: Any,
) -> Dict[str, List[Dict[str, Any]]]:
    """Return a safe shared cache and refresh the expensive rankings in background."""
    lock = getattr(self, "_jasong_rankings_cache_lock", None)
    if lock is None:
        lock = threading.RLock()
        self._jasong_rankings_cache_lock = lock
        self._jasong_rankings_cache = {}
        self._jasong_rankings_cache_at = {}
        self._jasong_rankings_refreshing = set()

    key = _rankings_cache_key(args, kwargs)
    now = _now()
    refresh_after = _rankings_refresh_seconds()
    stale_max = _rankings_stale_max_seconds()
    with lock:
        cache = getattr(self, "_jasong_rankings_cache", {})
        cache_at = getattr(self, "_jasong_rankings_cache_at", {})
        cached = cache.get(key)
        at = _to_float(cache_at.get(key), 0.0)
        age = max(0.0, now - at) if at > 0 else float("inf")
        refreshing = getattr(self, "_jasong_rankings_refreshing", set())
        full_refresh = _historical_refresh_running(self)

        should_refresh = age >= refresh_after and key not in refreshing
        if should_refresh and not full_refresh:
            refreshing.add(key)
            self._jasong_rankings_refreshing = refreshing
            threading.Thread(
                target=_forward_rankings_refresh_worker,
                args=(self, key, tuple(args), dict(kwargs)),
                name=f"jasong-forward-rankings-{key}",
                daemon=True,
            ).start()
            self._jasong_rankings_refresh_started_at = now
            self._jasong_rankings_refresh_deferred = False
        elif should_refresh and full_refresh:
            self._jasong_rankings_refresh_deferred = True

        self._jasong_rankings_cache_state = (
            "READY"
            if isinstance(cached, dict) and age <= stale_max
            else ("STALE" if isinstance(cached, dict) else "WARMING_UP")
        )
        self._jasong_rankings_cache_age_seconds = (
            round(age, 2) if age != float("inf") else None
        )
        if not isinstance(cached, dict) or age > stale_max:
            return {}
        return _age_cached_rankings(self, cached, age)


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

                health["phase"] = "RANKINGS_CACHE_READ"
                health["phase_started_at"] = _now()
                rankings = self.ranking_source() or {}
                health["rankings_ready_at"] = _now()
                ranking_owner = getattr(self.ranking_source, "__self__", None)
                if ranking_owner is not None:
                    health["forward_rankings_cache_state"] = getattr(
                        ranking_owner, "_jasong_rankings_cache_state", None
                    )
                    health["forward_rankings_cache_age_seconds"] = getattr(
                        ranking_owner, "_jasong_rankings_cache_age_seconds", None
                    )
                    health["forward_rankings_refresh_running"] = bool(
                        getattr(ranking_owner, "_jasong_rankings_refreshing", set())
                    )
                    health["forward_rankings_refresh_deferred"] = bool(
                        getattr(ranking_owner, "_jasong_rankings_refresh_deferred", False)
                    )
                    health["forward_rankings_last_refresh_duration_seconds"] = getattr(
                        ranking_owner, "_jasong_rankings_last_refresh_duration_seconds", None
                    )
                    health["forward_rankings_last_error"] = getattr(
                        ranking_owner, "_jasong_rankings_last_error", None
                    )

                # V5.1: execution-health must never recalculate forward rankings.
                # Cache only the already-completed execution ranking snapshot in
                # memory. The health route reads this snapshot without touching
                # validator.sync(), broker evidence, bootstrap metrics or market
                # analysis. This keeps diagnostics instant even with hundreds of
                # settled forward trades.
                try:
                    if rankings:
                        self._jasong_health_rankings_cache = {
                            str(category): [
                                copy.deepcopy(row)
                                for row in (rows or [])[:5]
                                if isinstance(row, dict)
                            ]
                            for category, rows in rankings.items()
                            if isinstance(rows, list)
                        }
                        self._jasong_health_rankings_cache_at = _now()
                        health["health_rankings_cache_at"] = (
                            self._jasong_health_rankings_cache_at
                        )
                except Exception:
                    pass
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
        ForwardStore.sync = _batched_forward_store_sync
        ForwardValidationEngine.sync = _singleflight_forward_validator_sync
        removed_variants = _patch_quarantined_strategy_variants()

        # Entry-policy repair: restore validated standard eligibility and prevent
        # category_rankings() from re-promoting WATCH/STRONG signals.
        ForwardPrimeArchitecture.enrich = _guarded_forward_enrich
        ForwardPrimeArchitecture.category_rankings = _cached_guarded_forward_rankings
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
            "batched_forward_store_sync": True,
            "forward_evidence_change_detection": True,
            "shared_forward_rankings_singleflight_cache": True,
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
    # V5.1: health is a cache-only diagnostic surface. Do NOT call
    # forward_prime.category_rankings() here: that path performs validator.sync(),
    # forward metrics/bootstrap work and evidence merging. With a large forward
    # ledger, a browser health request could therefore take a very long time.
    # The execution thread already computes the authoritative rankings; consume
    # only its last completed snapshot.
    rankings_cache_at = None
    rankings_cache_age = None
    rankings_cache_state = "WARMING_UP"
    try:
        rankings = getattr(portfolio, "_jasong_health_rankings_cache", None)
        rankings_cache_at = getattr(
            portfolio, "_jasong_health_rankings_cache_at", None
        )
        if not isinstance(rankings, dict):
            rankings = {}
        if rankings_cache_at is not None:
            rankings_cache_age = _age_seconds(rankings_cache_at)
        rankings_cache_state = "READY" if rankings else "WARMING_UP"
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
    elif rankings_cache_state == "WARMING_UP":
        flow_state = "HEALTH_CACHE_WARMING_UP"
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
            "health_rankings_cache_state": rankings_cache_state,
            "health_rankings_cache_at": rankings_cache_at,
            "health_rankings_cache_age_seconds": rankings_cache_age,
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
            "execution_health_cache_only": True,
            "execution_health_recomputes_forward_rankings": False,
            "forward_store_sync_mode": "BATCHED_SINGLE_TRANSACTION",
            "forward_evidence_sync_mode": "CHANGE_DETECTED_SINGLEFLIGHT",
            "forward_rankings_mode": "SHARED_BACKGROUND_SINGLEFLIGHT_CACHE",
            "forward_rankings_cache_state": getattr(
                forward_prime, "_jasong_rankings_cache_state", None
            ) if forward_prime is not None else None,
            "forward_rankings_cache_age_seconds": getattr(
                forward_prime, "_jasong_rankings_cache_age_seconds", None
            ) if forward_prime is not None else None,
            "forward_rankings_refresh_running": bool(
                getattr(forward_prime, "_jasong_rankings_refreshing", set())
            ) if forward_prime is not None else False,
            "forward_rankings_refresh_deferred": bool(
                getattr(forward_prime, "_jasong_rankings_refresh_deferred", False)
            ) if forward_prime is not None else False,
            "forward_rankings_last_refresh_duration_seconds": getattr(
                forward_prime, "_jasong_rankings_last_refresh_duration_seconds", None
            ) if forward_prime is not None else None,
            "forward_rankings_last_error": getattr(
                forward_prime, "_jasong_rankings_last_error", None
            ) if forward_prime is not None else None,
            "forward_validator_last_sync_duration_seconds": getattr(
                getattr(forward_prime, "validator", None),
                "_jasong_forward_sync_last_duration_seconds", None,
            ) if forward_prime is not None else None,
            "forward_validator_last_sync_rows": getattr(
                getattr(forward_prime, "validator", None),
                "_jasong_forward_sync_last_rows", None,
            ) if forward_prime is not None else None,
            "forward_validator_unchanged_sync_skips": getattr(
                getattr(forward_prime, "validator", None),
                "_jasong_forward_sync_unchanged", 0,
            ) if forward_prime is not None else 0,
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


# ===========================================================================
# V6 ADAPTIVE FORWARD TRADER
# ===========================================================================
#
# V5.2 fixed execution reliability and restored the strict validated-standard
# gate. V6 keeps those protections, but adds a controlled path for genuinely
# improved strategies to build fresh broker-settled evidence:
#
#   WATCH -> PROBATION -> VALIDATED -> PRIME
#
# Probation is IG DEMO only, minimum-legal-size biased, continuation-confirmed,
# exposure-capped, and cannot enter Compound. Promotion/quarantine is computed
# from NEW V6 broker-settled evidence so legacy poor execution does not make
# recovery mathematically impossible. Seeded failed strategy families remain
# hard-quarantined from new optimizer selection.
# ===========================================================================

V6_POLICY_VERSION = "V6_ADAPTIVE_FORWARD"
V6_POLICY_LABEL = "WATCH_PROBATION_VALIDATED_PRIME"

V6_PROBATION_THRESHOLDS: Dict[str, Dict[str, float]] = {
    "FOREX": {"quant": 0.45, "ai": 0.50, "fast": 72.0},
    "INDICES": {"quant": 0.45, "ai": 0.55, "fast": 75.0},
    "CRYPTO": {"quant": 0.40, "ai": 0.50, "fast": 70.0},
    "METALS": {"quant": 0.50, "ai": 0.55, "fast": 78.0},
    "ENERGY": {"quant": 0.50, "ai": 0.55, "fast": 78.0},
    "SHARES": {"quant": 0.45, "ai": 0.55, "fast": 75.0},
}

V6_SOFT_HIST_WR_MIN = 0.45
V6_SOFT_HIST_PF_MIN = 0.90
V6_PROBATION_SCORE_MIN = 0.58
V6_VALIDATED_SCORE_MIN = 0.62
V6_PROBATION_REVIEW_MIN = 10
V6_PROBATION_PROMOTION_MIN = 15
V6_PROBATION_MAX_SETTLED = 30
V6_PROMOTION_WR_MIN = 0.55
V6_PROMOTION_PF_MIN = 1.20
V6_PROMOTION_EXPECTANCY_MIN = 0.05
V6_PROMOTION_MAX_DD_R = 4.0
V6_PROMOTION_BOOTSTRAP_MIN = 0.65
V6_QUARANTINE_WR_FLOOR = 0.45
V6_QUARANTINE_PF_FLOOR = 0.85
V6_QUARANTINE_EXPECTANCY_FLOOR = -0.10
V6_PRIME_MIN_SETTLED = 20
V6_PRIME_WR_MIN = 0.55
V6_PRIME_PF_MIN = 1.25
V6_PRIME_EXPECTANCY_MIN = 0.08
V6_PRIME_MAX_DD_R = 4.0
V6_PRIME_BOOTSTRAP_MIN = 0.75

_V6_BASE_FORWARD_ENRICH = ForwardPrimeArchitecture.enrich
_V6_BASE_FORWARD_METRICS = ForwardValidationEngine.metrics
_V6_BASE_CATEGORY_MAY_OPEN = CategoryExecutionEngine._may_open
_V6_BASE_TRACKER_UPDATE = TradeExcursionTracker._update_take_profit_fields
_V6_BASE_TRACKER_MERGE = TradeExcursionTracker.merge
_V6_BASE_TRACKER_STATUS = TradeExcursionTracker.status
_V6_BASE_CATEGORY_STATUS = CategoryExecutionEngine.status
_V6_BASE_HEALTH_SNAPSHOT = execution_health_snapshot
_V6_BASE_RUNTIME_OPTIMIZATIONS = _install_runtime_execution_optimizations
_V6_BASE_AGE_CACHED_RANKINGS = _age_cached_rankings
_V6_BASE_IG_ACCOUNTS = IGDemoBroker.accounts
_V6_BASE_IG_OPEN = IGDemoBroker.open_epic_position
_V6_BASE_IG_CLOSE = IGDemoBroker.close_position


def _v6_env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except Exception:
        value = default
    return max(lo, min(hi, value))


def _v6_env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except Exception:
        value = default
    return max(lo, min(hi, value))


def _v6_nested_raw(row: Dict[str, Any]) -> Dict[str, Any]:
    raw = row.get("raw")
    out = dict(raw) if isinstance(raw, dict) else {}
    for key, value in row.items():
        if key not in {"raw", "raw_json", "entry_snapshot_json", "provenance_json"}:
            out.setdefault(key, value)
    return out


def _v6_metric_pf(values: List[float]) -> float:
    gains = sum(v for v in values if v > 0)
    losses = abs(sum(v for v in values if v < 0))
    if losses <= 0:
        return 99.0 if gains > 0 else 0.0
    return gains / losses


def _v6_metric_drawdown(values: List[float]) -> float:
    equity = 0.0
    peak = 0.0
    maximum = 0.0
    for value in reversed(values):
        equity += value
        peak = max(peak, equity)
        maximum = max(maximum, peak - equity)
    return maximum


def _v6_summary_for_rows(
    validator: ForwardValidationEngine,
    rows: List[Dict[str, Any]],
    *,
    key: str,
    bootstrap: bool = True,
) -> Dict[str, Any]:
    values = [_to_float(row.get("r_multiple"), 0.0) for row in rows]
    settled = len(rows)
    wins = sum(
        1 for row in rows
        if str(row.get("broker_result") or "").upper() == "WIN"
    )
    wr = wins / settled if settled else 0.0
    pf = _v6_metric_pf(values)
    expectancy = sum(values) / settled if settled else 0.0
    drawdown = _v6_metric_drawdown(values)
    probability = 0.0
    if bootstrap and settled >= 8:
        try:
            probability = validator._bootstrap_positive_probability(values, key)
        except Exception:
            probability = 0.0
    r_sources: Counter[str] = Counter(
        str(row.get("r_source") or "UNKNOWN") for row in rows
    )
    return {
        "settled_trades": settled,
        "wins": wins,
        "losses": settled - wins,
        "win_rate": round(wr, 6),
        "win_rate_pct": round(wr * 100.0, 2),
        "profit_factor": round(pf, 6),
        "expectancy_r": round(expectancy, 6),
        "max_drawdown_r": round(drawdown, 6),
        "bootstrap_probability_positive_expectancy": round(probability, 6),
        "bootstrap_probability_positive_expectancy_pct": round(probability * 100.0, 2),
        "r_sources": dict(r_sources),
    }


def _v6_behavior_profile(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    mfe_r_values: List[float] = []
    mae_r_values: List[float] = []
    winner_capture: List[float] = []
    loss_count = 0
    bad_entry_losses = 0
    observed = 0

    for row in rows:
        raw = _v6_nested_raw(row)
        stop_pct = abs(_to_float(
            raw.get("planned_stop_pct")
            or raw.get("protective_stop_pct"),
            0.0,
        ))
        mfe_pct = _to_float(raw.get("mfe_pct"), float("nan"))
        mae_pct = abs(_to_float(
            raw.get("mae_abs_pct")
            if raw.get("mae_abs_pct") is not None
            else raw.get("mae_pct"),
            float("nan"),
        ))
        if stop_pct <= 0:
            continue
        if mfe_pct != mfe_pct or mae_pct != mae_pct:
            continue

        observed += 1
        mfe_r = max(0.0, mfe_pct) / stop_pct
        mae_r = max(0.0, mae_pct) / stop_pct
        mfe_r_values.append(mfe_r)
        mae_r_values.append(mae_r)
        result = str(row.get("broker_result") or "").upper()
        if result == "LOSS":
            loss_count += 1
            if mfe_r < 0.15 and mae_r >= 0.50:
                bad_entry_losses += 1
        elif result == "WIN" and mfe_r > 0:
            source = str(row.get("r_source") or "").upper()
            if "BINARY_OUTCOME" not in source:
                realised_r = max(0.0, _to_float(row.get("r_multiple"), 0.0))
                winner_capture.append(max(0.0, min(1.5, realised_r / mfe_r)))

    bad_rate = bad_entry_losses / loss_count if loss_count else 0.0
    capture = (
        sum(winner_capture) / len(winner_capture)
        if winner_capture else None
    )
    entry_quality = 1.0 - bad_rate if loss_count >= 3 else 0.50
    exit_quality = max(0.0, min(1.0, capture)) if capture is not None else 0.50
    score = 0.65 * entry_quality + 0.35 * exit_quality
    return {
        "observed_trades": observed,
        "losses_with_excursion": loss_count,
        "bad_entry_losses": bad_entry_losses,
        "bad_entry_loss_rate": round(bad_rate, 6),
        "bad_entry_loss_rate_pct": round(bad_rate * 100.0, 2),
        "avg_mfe_r": round(sum(mfe_r_values) / len(mfe_r_values), 6) if mfe_r_values else None,
        "avg_mae_r": round(sum(mae_r_values) / len(mae_r_values), 6) if mae_r_values else None,
        "winner_capture_efficiency": round(capture, 6) if capture is not None else None,
        "winner_capture_efficiency_pct": round(capture * 100.0, 2) if capture is not None else None,
        "entry_quality_score": round(entry_quality, 6),
        "exit_quality_score": round(exit_quality, 6),
        "behavior_score": round(score, 6),
    }


def _v6_regime_profiles(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        raw = _v6_nested_raw(row)
        regime = str(
            raw.get("market_regime")
            or raw.get("regime")
            or "UNKNOWN"
        ).upper().strip()
        buckets.setdefault(regime, []).append(row)
    output: Dict[str, Dict[str, Any]] = {}
    for regime, bucket in buckets.items():
        values = [_to_float(row.get("r_multiple"), 0.0) for row in bucket]
        wins = sum(
            1 for row in bucket
            if str(row.get("broker_result") or "").upper() == "WIN"
        )
        settled = len(bucket)
        output[regime] = {
            "settled_trades": settled,
            "win_rate": round(wins / settled, 6) if settled else 0.0,
            "win_rate_pct": round((wins / settled) * 100.0, 2) if settled else 0.0,
            "profit_factor": round(_v6_metric_pf(values), 6),
            "expectancy_r": round(sum(values) / settled, 6) if settled else 0.0,
        }
    return output


def _v6_forward_metrics(
    self: ForwardValidationEngine,
    *,
    strategy_id: Optional[str] = None,
    symbol: Optional[str] = None,
    sync: bool = True,
) -> Dict[str, Any]:
    base = _V6_BASE_FORWARD_METRICS(
        self,
        strategy_id=strategy_id,
        symbol=symbol,
        sync=sync,
    )
    try:
        rows = self.store.rows(
            strategy_id=strategy_id,
            symbol=symbol,
            limit=max(80, int(self.config.rolling_window_trades)),
        )
    except Exception:
        rows = []

    v6_rows: List[Dict[str, Any]] = []
    probation_rows: List[Dict[str, Any]] = []
    validated_rows: List[Dict[str, Any]] = []
    for row in rows:
        raw = _v6_nested_raw(row)
        policy = str(
            raw.get("policy_version")
            or raw.get("execution_policy_version")
            or ""
        ).upper()
        lane = str(
            raw.get("execution_lane")
            or raw.get("trade_class")
            or ""
        ).upper()
        if policy.startswith(V6_POLICY_VERSION):
            v6_rows.append(row)
            if lane == "PROBATION":
                probation_rows.append(row)
            elif lane in {"VALIDATED", "PRIME", "VALIDATED_STRONG"}:
                validated_rows.append(row)

    adaptive = _v6_summary_for_rows(
        self,
        v6_rows[:40],
        key=f"{strategy_id or symbol or 'ALL'}|V6_ADAPTIVE",
        bootstrap=True,
    )
    probation = _v6_summary_for_rows(
        self,
        probation_rows[:40],
        key=f"{strategy_id or symbol or 'ALL'}|V6_PROBATION",
        bootstrap=True,
    )
    validated = _v6_summary_for_rows(
        self,
        validated_rows[:40],
        key=f"{strategy_id or symbol or 'ALL'}|V6_VALIDATED",
        bootstrap=True,
    )
    behavior_source = v6_rows[:40] if v6_rows else rows[:40]
    behavior = _v6_behavior_profile(behavior_source)
    regimes = _v6_regime_profiles(v6_rows[:40] if v6_rows else rows[:40])

    adaptive_prime_checks = {
        "minimum_settled_trades": adaptive["settled_trades"] >= V6_PRIME_MIN_SETTLED,
        "win_rate": adaptive["win_rate"] >= V6_PRIME_WR_MIN,
        "profit_factor": adaptive["profit_factor"] >= V6_PRIME_PF_MIN,
        "expectancy_r": adaptive["expectancy_r"] >= V6_PRIME_EXPECTANCY_MIN,
        "bootstrap_positive_expectancy": (
            adaptive["bootstrap_probability_positive_expectancy"]
            >= V6_PRIME_BOOTSTRAP_MIN
        ),
        "max_drawdown_r": adaptive["max_drawdown_r"] <= V6_PRIME_MAX_DD_R,
    }
    adaptive["prime_checks"] = adaptive_prime_checks
    adaptive["prime_eligible"] = all(adaptive_prime_checks.values())

    base = dict(base)
    base["adaptive_v6"] = adaptive
    base["probation_v6"] = probation
    base["validated_v6"] = validated
    base["behavior"] = behavior
    base["regime_profiles"] = regimes
    base["adaptive_policy_version"] = V6_POLICY_VERSION
    return base


def _v6_soft_historical_gate(row: Dict[str, Any]) -> tuple[bool, List[str]]:
    reasons: List[str] = []
    sample = int(_to_float(row.get("historical_trades"), 0.0))
    wr = _to_float(row.get("historical_win_rate"), 0.0)
    pf = _to_float(row.get("historical_profit_factor"), 0.0)
    stable = bool(row.get("optimizer_selection_stable"))
    if sample < 10:
        reasons.append("PROBATION_HIST_SAMPLE_BELOW_10")
    if wr < V6_SOFT_HIST_WR_MIN:
        reasons.append("PROBATION_HIST_WR_BELOW_45")
    if pf < V6_SOFT_HIST_PF_MIN:
        reasons.append("PROBATION_HIST_PF_BELOW_0_90")
    if not stable:
        reasons.append("PROBATION_SELECTION_NOT_STABLE")
    return len(reasons) == 0, reasons


def _v6_probation_live_gate(row: Dict[str, Any]) -> tuple[bool, List[str]]:
    category = str(row.get("category") or "UNKNOWN").upper().strip()
    thresholds = V6_PROBATION_THRESHOLDS.get(
        category,
        {"quant": 0.45, "ai": 0.55, "fast": 75.0},
    )
    reasons: List[str] = []
    direction = str(row.get("direction") or "").upper().strip()
    quant = _to_float(row.get("quant_confidence"), 0.0)
    ai = _to_float(row.get("model_ai_confidence"), 0.0)
    fast = _to_float(
        row.get("live_fast_score")
        if row.get("live_fast_score") is not None
        else row.get("smart_fast_score"),
        0.0,
    )
    if direction not in {"BUY", "SELL"}:
        reasons.append("PROBATION_NO_DIRECTION")
    if quant < thresholds["quant"]:
        reasons.append("PROBATION_QUANT_BELOW_CATEGORY_FLOOR")
    if ai < thresholds["ai"]:
        reasons.append("PROBATION_AI_BELOW_CATEGORY_FLOOR")
    if fast < thresholds["fast"]:
        reasons.append("PROBATION_FAST_BELOW_CATEGORY_FLOOR")
    if not bool(row.get("ig_tradeable")):
        reasons.append("PROBATION_IG_NOT_TRADEABLE")
    if row.get("spread_pass") is not True:
        reasons.append("PROBATION_SPREAD_GATE_FAIL")
    provenance = row.get("provenance")
    if isinstance(provenance, dict) and not bool(provenance.get("fresh")):
        reasons.append("PROBATION_PROVENANCE_NOT_FRESH")
    return len(reasons) == 0, reasons


def _v6_promotion_state(metrics: Dict[str, Any]) -> tuple[bool, List[str]]:
    probation = metrics.get("probation_v6")
    probation = probation if isinstance(probation, dict) else {}
    reasons: List[str] = []
    if int(probation.get("settled_trades") or 0) < V6_PROBATION_PROMOTION_MIN:
        reasons.append("PROBATION_SAMPLE_BELOW_PROMOTION_MIN")
    if _to_float(probation.get("win_rate"), 0.0) < V6_PROMOTION_WR_MIN:
        reasons.append("PROBATION_WR_BELOW_55")
    if _to_float(probation.get("profit_factor"), 0.0) < V6_PROMOTION_PF_MIN:
        reasons.append("PROBATION_PF_BELOW_1_20")
    if _to_float(probation.get("expectancy_r"), 0.0) < V6_PROMOTION_EXPECTANCY_MIN:
        reasons.append("PROBATION_EXPECTANCY_BELOW_0_05R")
    if _to_float(probation.get("max_drawdown_r"), 999.0) > V6_PROMOTION_MAX_DD_R:
        reasons.append("PROBATION_DRAWDOWN_ABOVE_4R")
    if (
        _to_float(probation.get("bootstrap_probability_positive_expectancy"), 0.0)
        < V6_PROMOTION_BOOTSTRAP_MIN
    ):
        reasons.append("PROBATION_BOOTSTRAP_BELOW_65")
    return len(reasons) == 0, reasons


def _v6_dynamic_quarantine_state(metrics: Dict[str, Any]) -> tuple[bool, List[str]]:
    adaptive = metrics.get("adaptive_v6")
    adaptive = adaptive if isinstance(adaptive, dict) else {}
    behavior = metrics.get("behavior")
    behavior = behavior if isinstance(behavior, dict) else {}
    settled = int(adaptive.get("settled_trades") or 0)
    reasons: List[str] = []
    if settled >= V6_PROBATION_REVIEW_MIN:
        if _to_float(adaptive.get("win_rate"), 0.0) < V6_QUARANTINE_WR_FLOOR:
            reasons.append("ADAPTIVE_WR_BELOW_45")
        if _to_float(adaptive.get("profit_factor"), 0.0) < V6_QUARANTINE_PF_FLOOR:
            reasons.append("ADAPTIVE_PF_BELOW_0_85")
        if _to_float(adaptive.get("expectancy_r"), 0.0) < V6_QUARANTINE_EXPECTANCY_FLOOR:
            reasons.append("ADAPTIVE_EXPECTANCY_BELOW_MINUS_0_10R")
    if settled >= V6_PROBATION_MAX_SETTLED:
        promoted, _ = _v6_promotion_state(metrics)
        if not promoted:
            reasons.append("ADAPTIVE_MAX_PROBATION_SAMPLE_WITHOUT_PROMOTION")
    if (
        int(behavior.get("losses_with_excursion") or 0) >= 5
        and _to_float(behavior.get("bad_entry_loss_rate"), 0.0) >= 0.65
    ):
        reasons.append("ADAPTIVE_BAD_ENTRY_RATE_ABOVE_65")
    return len(reasons) > 0, reasons


def _v6_regime_score(row: Dict[str, Any], metrics: Dict[str, Any]) -> tuple[float, Dict[str, Any]]:
    regime = str(
        row.get("market_regime")
        or row.get("regime")
        or "UNKNOWN"
    ).upper().strip()
    profiles = metrics.get("regime_profiles")
    profiles = profiles if isinstance(profiles, dict) else {}
    profile = profiles.get(regime)
    if not isinstance(profile, dict) or int(profile.get("settled_trades") or 0) < 3:
        return 0.50, {}
    wr = max(0.0, min(1.0, _to_float(profile.get("win_rate"), 0.0)))
    pf = max(0.0, min(1.0, _to_float(profile.get("profit_factor"), 0.0) / 1.50))
    return round(0.70 * wr + 0.30 * pf, 6), dict(profile)


def _v6_forward_calibrated_score(row: Dict[str, Any], metrics: Dict[str, Any]) -> float:
    hist_wr = max(0.0, min(1.0, _to_float(row.get("historical_win_rate"), 0.0)))
    hist_pf = max(0.0, min(1.0, _to_float(row.get("historical_profit_factor"), 0.0) / 1.50))
    hist_score = 0.65 * hist_wr + 0.35 * hist_pf

    quant = max(0.0, min(1.0, _to_float(row.get("quant_confidence"), 0.0)))
    ai = max(0.0, min(1.0, _to_float(row.get("model_ai_confidence"), 0.0)))
    fast = max(0.0, min(1.0, _to_float(
        row.get("live_fast_score")
        if row.get("live_fast_score") is not None
        else row.get("smart_fast_score"),
        0.0,
    ) / 100.0))
    live_score = 0.25 * quant + 0.45 * ai + 0.30 * fast

    adaptive = metrics.get("adaptive_v6")
    adaptive = adaptive if isinstance(adaptive, dict) else {}
    settled = int(adaptive.get("settled_trades") or 0)
    source = adaptive if settled >= 5 else metrics
    forward_wr = max(0.0, min(1.0, _to_float(source.get("win_rate"), hist_wr)))
    forward_pf = max(0.0, min(1.0, _to_float(source.get("profit_factor"), 0.0) / 1.50))
    expectancy = _to_float(source.get("expectancy_r"), 0.0)
    expectancy_score = max(0.0, min(1.0, (expectancy + 0.20) / 0.50))
    forward_score = 0.45 * forward_wr + 0.35 * forward_pf + 0.20 * expectancy_score

    behavior = metrics.get("behavior")
    behavior = behavior if isinstance(behavior, dict) else {}
    behavior_score = max(0.0, min(1.0, _to_float(behavior.get("behavior_score"), 0.50)))
    regime_score, _ = _v6_regime_score(row, metrics)

    sample_weight = max(0.0, min(1.0, settled / 20.0))
    early = 0.45 * hist_score + 0.45 * live_score + 0.10 * behavior_score
    mature = (
        0.20 * hist_score
        + 0.25 * live_score
        + 0.35 * forward_score
        + 0.10 * behavior_score
        + 0.10 * regime_score
    )
    score = (1.0 - sample_weight) * early + sample_weight * mature
    return round(max(0.0, min(1.0, score)), 6)


def _v6_adaptive_forward_enrich(
    self: ForwardPrimeArchitecture,
    raw: Dict[str, Any],
    *,
    forward_metrics: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    row = _V6_BASE_FORWARD_ENRICH(
        self,
        raw,
        forward_metrics=forward_metrics,
    )
    metrics = row.get("forward_validation")
    metrics = metrics if isinstance(metrics, dict) else (forward_metrics or {})

    live_strong = bool(row.get("strong_qualified"))
    strategy_unknown = _strategy_id(row) == "UNKNOWN"
    seeded_quarantine = bool(row.get("strategy_quarantined"))
    strict_validated = bool(row.get("validated_execution_gate"))
    soft_ok, soft_reasons = _v6_soft_historical_gate(row)
    probation_live_ok, probation_live_reasons = _v6_probation_live_gate(row)
    promoted, promotion_reasons = _v6_promotion_state(metrics)
    dynamic_quarantine, dynamic_reasons = _v6_dynamic_quarantine_state(metrics)
    adaptive = metrics.get("adaptive_v6")
    adaptive = adaptive if isinstance(adaptive, dict) else {}
    legacy_settled = int(metrics.get("settled_trades") or 0)
    legacy_wr = _to_float(metrics.get("win_rate"), 0.0)
    legacy_hard_fail = bool(legacy_settled >= 30 and legacy_wr < 0.40 and not adaptive.get("settled_trades"))
    if legacy_hard_fail:
        dynamic_quarantine = True
        dynamic_reasons.append("LEGACY_FORWARD_WR_BELOW_40_WITH_30_PLUS_TRADES")

    score = _v6_forward_calibrated_score(row, metrics)
    regime_score, regime_profile = _v6_regime_score(row, metrics)
    weak_regime = bool(
        int(regime_profile.get("settled_trades") or 0) >= 5
        and _to_float(regime_profile.get("win_rate"), 0.0) < 0.40
    )
    if weak_regime:
        dynamic_quarantine = True
        dynamic_reasons.append("CURRENT_REGIME_FORWARD_WR_BELOW_40")

    quarantine = bool(
        seeded_quarantine
        or dynamic_quarantine
        or strategy_unknown
    )

    validated = bool(
        live_strong
        and not quarantine
        and score >= V6_VALIDATED_SCORE_MIN
        and (strict_validated or promoted)
    )

    probation = bool(
        live_strong
        and not validated
        and not quarantine
        and soft_ok
        and probation_live_ok
        and score >= V6_PROBATION_SCORE_MIN
        and int((metrics.get("probation_v6") or {}).get("settled_trades") or 0)
        < V6_PROBATION_MAX_SETTLED
    )

    base_prime = bool(row.get("prime_qualified"))
    adaptive_prime = bool(adaptive.get("prime_eligible"))
    prime = bool(validated and (base_prime or adaptive_prime))

    if prime:
        lane = "PRIME"
    elif validated:
        lane = "VALIDATED"
    elif probation:
        lane = "PROBATION"
    elif live_strong:
        lane = "WATCH"
    else:
        lane = "OBSERVE"

    v5_reasons = list(row.get("rejection_reasons") or [])
    adaptive_reasons: List[str] = []
    adaptive_reasons.extend(soft_reasons)
    adaptive_reasons.extend(probation_live_reasons)
    if dynamic_quarantine:
        adaptive_reasons.extend(dynamic_reasons)
    if strategy_unknown:
        adaptive_reasons.append("UNKNOWN_STRATEGY_ATTRIBUTION")
    if score < V6_PROBATION_SCORE_MIN:
        adaptive_reasons.append("ADAPTIVE_SCORE_BELOW_PROBATION_58")
    if live_strong and not validated and not probation and not quarantine:
        adaptive_reasons.append("WATCH_ONLY_NO_EXECUTION_LANE")

    if lane in {"PROBATION", "VALIDATED", "PRIME"}:
        rejection_reasons: List[str] = []
    else:
        rejection_reasons = list(dict.fromkeys(v5_reasons + adaptive_reasons))

    row.update({
        "policy_version": V6_POLICY_VERSION,
        "adaptive_forward_policy": V6_POLICY_LABEL,
        "execution_lane": lane,
        "trade_class": lane,
        "probation_eligible": probation,
        "adaptive_validated": validated,
        "validated_execution_gate": validated,
        "standard_eligible": validated,
        "trade_eligible": bool(validated or probation),
        "learning_eligible": probation,
        "ig_demo_learning_eligible": bool(validated or probation),
        "prime_qualified": prime,
        "execution_eligible": prime,
        "eligible": prime,
        "compound_eligible": bool(row.get("compound_slot_candidate") and prime),
        "forward_calibrated_score": score,
        "forward_calibrated_score_pct": round(score * 100.0, 2),
        "forward_calibrated_score_is_probability": False,
        "probation_score_threshold_pct": V6_PROBATION_SCORE_MIN * 100.0,
        "validated_score_threshold_pct": V6_VALIDATED_SCORE_MIN * 100.0,
        "soft_historical_admission_pass": soft_ok,
        "soft_historical_admission_reasons": soft_reasons,
        "probation_live_gate_pass": probation_live_ok,
        "probation_live_gate_reasons": probation_live_reasons,
        "probation_promotion_earned": promoted,
        "probation_promotion_reasons": promotion_reasons,
        "dynamic_quarantine": dynamic_quarantine,
        "dynamic_quarantine_reasons": list(dict.fromkeys(dynamic_reasons)),
        "adaptive_quarantine": quarantine,
        "current_regime_forward_score": regime_score,
        "current_regime_forward_profile": regime_profile,
        "adaptive_v6_forward": adaptive,
        "probation_v6_forward": metrics.get("probation_v6") or {},
        "trade_behavior": metrics.get("behavior") or {},
        "historical_validation_mode": "STANDARD_GATE_PLUS_CONTROLLED_FORWARD_PROBATION",
        "historical_execution_veto": True,
        "execution_basis": (
            "ADAPTIVE_FORWARD_PRIME"
            if prime else
            "ADAPTIVE_FORWARD_VALIDATED"
            if validated else
            "CONTROLLED_IG_DEMO_PROBATION"
            if probation else
            "WATCH_ONLY"
            if live_strong else
            "OBSERVATION_ONLY"
        ),
        "validation_diagnostics": list(dict.fromkeys(v5_reasons + adaptive_reasons)),
        "rejection_reasons": rejection_reasons,
    })
    return row


def _v6_compute_guarded_forward_rankings(
    self: ForwardPrimeArchitecture,
    *args: Any,
    **kwargs: Any,
) -> Dict[str, List[Dict[str, Any]]]:
    output = _ORIGINAL_FORWARD_RANKINGS(self, *args, **kwargs) or {}
    for category, rows in list(output.items()):
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            validated = bool(row.get("adaptive_validated"))
            probation = bool(row.get("probation_eligible"))
            prime = bool(row.get("prime_qualified") and validated)
            row["standard_eligible"] = validated
            row["trade_eligible"] = bool(validated or probation)
            row["learning_eligible"] = probation
            row["ig_demo_learning_eligible"] = bool(validated or probation)
            row["prime_qualified"] = prime
            row["execution_eligible"] = prime
            row["eligible"] = prime
            row["compound_eligible"] = bool(
                row.get("compound_slot_candidate") and prime
            )
        lane_priority = {"PRIME": 4, "VALIDATED": 3, "PROBATION": 2, "WATCH": 1, "OBSERVE": 0}
        rows.sort(
            key=lambda row: (
                lane_priority.get(str(row.get("execution_lane") or "OBSERVE").upper(), 0),
                _to_float(row.get("forward_calibrated_score"), 0.0),
                _to_float(row.get("live_fast_score") or row.get("smart_fast_score"), 0.0),
                _to_float(row.get("model_ai_confidence"), 0.0),
                _to_float(row.get("quant_confidence"), 0.0),
            ),
            reverse=True,
        )
        for idx, row in enumerate(rows, start=1):
            row["category_rank"] = idx
            row["rank"] = idx
            row["source_rank"] = idx
            row["compound_slot_candidate"] = idx <= 2
            row["compound_eligible"] = bool(idx <= 2 and row.get("prime_qualified"))
    return output


def _v6_age_cached_rankings(
    self: ForwardPrimeArchitecture,
    rankings: Dict[str, List[Dict[str, Any]]],
    cache_age: float,
) -> Dict[str, List[Dict[str, Any]]]:
    output = _V6_BASE_AGE_CACHED_RANKINGS(self, rankings, cache_age)
    for rows in output.values():
        if not isinstance(rows, list):
            continue
        for row in rows:
            reasons = {str(x) for x in (row.get("rejection_reasons") or [])}
            if {"SIGNAL_STALE", "BROKER_QUOTE_STALE"} & reasons:
                row["probation_eligible"] = False
                row["trade_eligible"] = False
                row["learning_eligible"] = False
                row["ig_demo_learning_eligible"] = False
                if str(row.get("execution_lane") or "").upper() == "PROBATION":
                    row["execution_lane"] = "WATCH"
                    row["trade_class"] = "WATCH"
    return output


def _v6_category_may_open(
    self: CategoryExecutionEngine,
    candidate: Dict[str, Any],
    external: List[Dict[str, Any]],
) -> tuple[bool, str]:
    if bool(candidate.get("standard_eligible")):
        return _V6_BASE_CATEGORY_MAY_OPEN(self, candidate, external)
    if bool(candidate.get("probation_eligible")):
        shadow = dict(candidate)
        shadow["standard_eligible"] = True
        return _V6_BASE_CATEGORY_MAY_OPEN(self, shadow, external)
    return False, "not adaptive execution eligible"


def _v6_probation_capacity_gate(
    engine: CategoryExecutionEngine,
    candidate: Dict[str, Any],
) -> tuple[bool, str]:
    if str(candidate.get("execution_lane") or "").upper() != "PROBATION":
        return True, "NOT_PROBATION"
    open_rows = engine._open_positions()
    probation_rows = [
        row for row in open_rows
        if str(row.get("execution_lane") or row.get("trade_class") or "").upper() == "PROBATION"
    ]
    global_cap = _v6_env_int("ADAPTIVE_MAX_OPEN_PROBATION", 2, 1, 5)
    category_cap = _v6_env_int("ADAPTIVE_MAX_OPEN_PROBATION_PER_CATEGORY", 1, 1, 3)
    strategy_cap = _v6_env_int("ADAPTIVE_MAX_OPEN_PROBATION_PER_STRATEGY", 1, 1, 2)
    if len(probation_rows) >= global_cap:
        return False, "PROBATION_GLOBAL_POSITION_CAP_REACHED"
    category = str(candidate.get("category") or "").upper()
    if sum(1 for row in probation_rows if str(row.get("category") or "").upper() == category) >= category_cap:
        return False, "PROBATION_CATEGORY_POSITION_CAP_REACHED"
    strategy = _strategy_id(candidate)
    if sum(1 for row in probation_rows if _strategy_id(row) == strategy) >= strategy_cap:
        return False, "PROBATION_STRATEGY_POSITION_CAP_REACHED"
    return True, "PROBATION_CAPACITY_AVAILABLE"


def _v6_account_cache_lock(broker: Any) -> threading.RLock:
    lock = getattr(broker, "_jasong_v6_account_lock", None)
    if lock is None:
        lock = threading.RLock()
        broker._jasong_v6_account_lock = lock
    return lock


def _v6_parse_account_payload(broker: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    accounts = payload.get("accounts") if isinstance(payload, dict) else None
    accounts = accounts if isinstance(accounts, list) else []
    status = broker.status() if hasattr(broker, "status") else {}
    wanted = str((status or {}).get("account_id") or "").strip()
    selected: Dict[str, Any] = {}
    for account in accounts:
        if not isinstance(account, dict):
            continue
        account_id = str(account.get("accountId") or "").strip()
        if wanted and account_id == wanted:
            selected = account
            break
    if not selected and accounts:
        selected = next((a for a in accounts if isinstance(a, dict)), {})
    balance_obj = selected.get("balance") if isinstance(selected, dict) else {}
    balance_obj = balance_obj if isinstance(balance_obj, dict) else {}
    balance = _to_float(balance_obj.get("balance"), float("nan"))
    deposit = _to_float(balance_obj.get("deposit"), float("nan"))
    profit_loss = _to_float(balance_obj.get("profitLoss"), float("nan"))
    available = _to_float(balance_obj.get("available"), float("nan"))
    equity = balance + profit_loss if balance == balance and profit_loss == profit_loss else float("nan")
    ratio = available / equity if available == available and equity == equity and equity > 0 else float("nan")
    return {
        "state": "READY" if selected else "NO_ACCOUNT_ROW",
        "account_id": selected.get("accountId") if isinstance(selected, dict) else wanted or None,
        "account_name": selected.get("accountName") if isinstance(selected, dict) else None,
        "account_type": selected.get("accountType") if isinstance(selected, dict) else None,
        "currency": selected.get("currency") if isinstance(selected, dict) else None,
        "preferred": selected.get("preferred") if isinstance(selected, dict) else None,
        "balance": None if balance != balance else round(balance, 6),
        "deposit_margin_used": None if deposit != deposit else round(deposit, 6),
        "profit_loss": None if profit_loss != profit_loss else round(profit_loss, 6),
        "equity": None if equity != equity else round(equity, 6),
        "available_to_trade": None if available != available else round(available, 6),
        "free_margin_ratio": None if ratio != ratio else round(ratio, 6),
        "free_margin_ratio_pct": None if ratio != ratio else round(ratio * 100.0, 2),
        "source": "IG_DEMO_ACCOUNTS",
        "live_money_execution": False,
    }


def _v6_account_refresh_worker(broker: Any) -> None:
    started = _now()
    lock = _v6_account_cache_lock(broker)
    try:
        payload = _V6_BASE_IG_ACCOUNTS(broker) or {}
        snapshot = _v6_parse_account_payload(broker, payload)
        with lock:
            broker._jasong_v6_account_snapshot = snapshot
            broker._jasong_v6_account_snapshot_at = _now()
            broker._jasong_v6_account_last_error = None
            broker._jasong_v6_account_last_duration_seconds = round(_now() - started, 3)
    except Exception as exc:
        with lock:
            broker._jasong_v6_account_last_error = _compact_error(exc)
            broker._jasong_v6_account_last_duration_seconds = round(_now() - started, 3)
    finally:
        with lock:
            broker._jasong_v6_account_refreshing = False


def _v6_account_funding_snapshot(broker: Any, *, trigger_refresh: bool = True) -> Dict[str, Any]:
    if broker is None:
        return {"state": "BROKER_UNAVAILABLE"}
    lock = _v6_account_cache_lock(broker)
    now = _now()
    ttl = _v6_env_float("IG_DEMO_ACCOUNT_CACHE_SECONDS", 30.0, 10.0, 120.0)
    stale = _v6_env_float("IG_DEMO_ACCOUNT_STALE_SECONDS", 180.0, 30.0, 600.0)
    with lock:
        snapshot = getattr(broker, "_jasong_v6_account_snapshot", None)
        at = _to_float(getattr(broker, "_jasong_v6_account_snapshot_at", 0.0), 0.0)
        age = max(0.0, now - at) if at > 0 else float("inf")
        refreshing = bool(getattr(broker, "_jasong_v6_account_refreshing", False))
        if trigger_refresh and age >= ttl and not refreshing:
            broker._jasong_v6_account_refreshing = True
            threading.Thread(
                target=_v6_account_refresh_worker,
                args=(broker,),
                name="jasong-v6-account-funding",
                daemon=True,
            ).start()
            refreshing = True
        if isinstance(snapshot, dict) and age <= stale:
            out = copy.deepcopy(snapshot)
            out["age_seconds"] = round(age, 2)
            out["refreshing"] = refreshing
            out["last_error"] = getattr(broker, "_jasong_v6_account_last_error", None)
            return out
        return {
            "state": "WARMING_UP" if refreshing else "UNKNOWN",
            "age_seconds": None if age == float("inf") else round(age, 2),
            "refreshing": refreshing,
            "last_error": getattr(broker, "_jasong_v6_account_last_error", None),
            "source": "IG_DEMO_ACCOUNTS",
        }


def _v6_invalidate_account_snapshot(broker: Any) -> None:
    try:
        with _v6_account_cache_lock(broker):
            broker._jasong_v6_account_snapshot_at = 0.0
    except Exception:
        pass


def _v6_open_with_account_invalidate(self: IGDemoBroker, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    try:
        return _V6_BASE_IG_OPEN(self, *args, **kwargs)
    finally:
        _v6_invalidate_account_snapshot(self)


def _v6_close_with_account_invalidate(self: IGDemoBroker, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    try:
        return _V6_BASE_IG_CLOSE(self, *args, **kwargs)
    finally:
        _v6_invalidate_account_snapshot(self)


def _v6_account_funding_gate(
    broker: Any,
    candidate: Dict[str, Any],
) -> tuple[bool, str, Dict[str, Any]]:
    snapshot = _v6_account_funding_snapshot(broker, trigger_refresh=True)
    if str(snapshot.get("state") or "").upper() != "READY":
        return True, "ACCOUNT_FUNDING_WARMING_ALLOW_BROKER_AUTHORITY", snapshot
    available = snapshot.get("available_to_trade")
    equity = snapshot.get("equity")
    if available is not None and _to_float(available, 0.0) <= 0:
        return False, "ACCOUNT_AVAILABLE_FUNDS_NONPOSITIVE", snapshot
    reserve = _v6_env_float("ADAPTIVE_MIN_FREE_MARGIN_RATIO", 0.10, 0.0, 0.50)
    if equity is not None and _to_float(equity, 0.0) > 0 and available is not None:
        ratio = _to_float(available, 0.0) / _to_float(equity, 1.0)
        if ratio < reserve:
            return False, "ACCOUNT_FREE_MARGIN_RESERVE_BELOW_POLICY", snapshot
    return True, "ACCOUNT_FUNDING_OK", snapshot


def _v6_candidate_exit_plan(candidate: Dict[str, Any]) -> Dict[str, Any]:
    category = str(candidate.get("category") or "UNKNOWN").upper().strip()
    profile = _EXIT_PROFILES.get(
        category,
        {"min_stop": 0.10, "max_stop": 0.30, "rr": 1.25},
    )
    volatility_pct = _recent_volatility_pct(candidate)
    raw_stop = volatility_pct * 2.5 if volatility_pct > 0 else profile["min_stop"]
    stop_pct = max(profile["min_stop"], min(profile["max_stop"], raw_stop))
    lane = str(candidate.get("execution_lane") or "VALIDATED").upper()
    behavior = candidate.get("trade_behavior")
    behavior = behavior if isinstance(behavior, dict) else {}
    bad_rate = _to_float(behavior.get("bad_entry_loss_rate"), 0.0)
    capture = behavior.get("winner_capture_efficiency")
    capture_value = _to_float(capture, 0.50) if capture is not None else 0.50

    rr = float(profile["rr"])
    trail_activate_r = {
        "FOREX": 0.55,
        "INDICES": 0.55,
        "CRYPTO": 0.75,
        "METALS": 0.60,
        "ENERGY": 0.55,
        "SHARES": 0.60,
    }.get(category, 0.60)
    trail_lock_r = 0.15

    if lane == "PROBATION":
        rr = min(rr, 1.20)
        trail_activate_r = min(trail_activate_r, 0.60)
        trail_lock_r = 0.12
    if bad_rate >= 0.50:
        stop_pct = max(profile["min_stop"], stop_pct * 0.85)
        rr = max(1.10, rr - 0.05)
        trail_activate_r = min(trail_activate_r, 0.50)
    if capture_value < 0.40:
        rr = max(1.10, rr - 0.10)
        trail_activate_r = min(trail_activate_r, 0.50)
        trail_lock_r = max(trail_lock_r, 0.18)
    adaptive = candidate.get("adaptive_v6_forward")
    adaptive = adaptive if isinstance(adaptive, dict) else {}
    if (
        int(adaptive.get("settled_trades") or 0) >= 15
        and _to_float(adaptive.get("win_rate"), 0.0) >= 0.60
        and _to_float(adaptive.get("profit_factor"), 0.0) >= 1.50
    ):
        rr = min(1.45, rr + 0.10)

    target_pct = stop_pct * rr
    return {
        "exit_policy_version": "V6_ADAPTIVE_VOLATILITY_R",
        "entry_volatility_pct": round(volatility_pct, 6),
        "planned_stop_pct": round(stop_pct, 6),
        "planned_take_profit_pct": round(target_pct, 6),
        "planned_reward_r": round(rr, 3),
        "trailing_activate_r": round(trail_activate_r, 3),
        "trailing_lock_r": round(trail_lock_r, 3),
        "exit_plan_basis": "CATEGORY_VOLATILITY_PLUS_FORWARD_MFE_MAE_BEHAVIOR",
    }


def _v6_category_open_candidate(
    self: CategoryExecutionEngine,
    candidate: Dict[str, Any],
    external: List[Dict[str, Any]],
) -> None:
    lane = str(candidate.get("execution_lane") or "").upper()
    if lane in {"PRIME", "VALIDATED"}:
        if not bool(candidate.get("standard_eligible")):
            return
    elif lane == "PROBATION":
        if not bool(candidate.get("probation_eligible")):
            return
    else:
        return

    before = {
        str(row.get("deal_id") or "")
        for row in self._open_positions()
        if row.get("deal_id")
    }
    original_default = float(self.default_size)
    try:
        if lane == "PROBATION":
            probation_size = _v6_env_float("ADAPTIVE_PROBATION_DEFAULT_SIZE", 0.10, 0.0001, 1000.0)
            self.default_size = min(original_default, probation_size)
        _adaptive_category_open_candidate(self, candidate, external)
    finally:
        self.default_size = original_default

    opened = next(
        (
            row for row in reversed(self._open_positions())
            if str(row.get("deal_id") or "") not in before
        ),
        None,
    )
    if not isinstance(opened, dict):
        return

    plan = _v6_candidate_exit_plan(candidate)
    opened.update(plan)
    opened.update({
        "policy_version": V6_POLICY_VERSION,
        "execution_lane": lane,
        "trade_class": lane,
        "market_regime": candidate.get("market_regime") or candidate.get("regime"),
        "entry_signal_timestamp": candidate.get("signal_timestamp") or candidate.get("evaluated_at"),
        "entry_live_price": candidate.get("live_price"),
        "forward_calibrated_score": candidate.get("forward_calibrated_score"),
        "forward_calibrated_score_pct": candidate.get("forward_calibrated_score_pct"),
        "adaptive_v6_forward_at_entry": copy.deepcopy(candidate.get("adaptive_v6_forward") or {}),
        "probation_v6_forward_at_entry": copy.deepcopy(candidate.get("probation_v6_forward") or {}),
        "trade_behavior_at_entry": copy.deepcopy(candidate.get("trade_behavior") or {}),
        "historical_win_rate": candidate.get("historical_win_rate"),
        "historical_profit_factor": candidate.get("historical_profit_factor"),
        "walk_forward_pass": candidate.get("walk_forward_pass"),
    })
    opened["adaptive_entry_snapshot"] = {
        "policy_version": V6_POLICY_VERSION,
        "execution_lane": lane,
        "strategy_id": candidate.get("strategy_id") or candidate.get("selected_strategy"),
        "direction": candidate.get("direction"),
        "quant_confidence": candidate.get("quant_confidence"),
        "model_ai_confidence": candidate.get("model_ai_confidence"),
        "live_fast_score": candidate.get("live_fast_score") or candidate.get("smart_fast_score"),
        "forward_calibrated_score": candidate.get("forward_calibrated_score"),
        "market_regime": opened.get("market_regime"),
        "planned_stop_pct": opened.get("planned_stop_pct"),
        "planned_take_profit_pct": opened.get("planned_take_profit_pct"),
        "planned_reward_r": opened.get("planned_reward_r"),
        "captured_at": _now(),
    }
    _attach_initial_native_risk(self, opened)


def _v6_tracker_context(self: TradeExcursionTracker, record: Dict[str, Any]) -> None:
    # Preserve V5 context population first.
    original = globals().get("_V6_V5_TRACKER_CONTEXT")
    if callable(original):
        original(self, record)
    portfolio = getattr(self, "_jasong_category_portfolio", None)
    deal_id = str(record.get("deal_id") or "").strip()
    if portfolio is None or not deal_id:
        return
    try:
        with portfolio._lock:
            match = next(
                (
                    row for row in (portfolio._state.get("positions") or [])
                    if str(row.get("deal_id") or "").strip() == deal_id
                ),
                None,
            )
        if isinstance(match, dict):
            for field in (
                "policy_version", "execution_lane", "trade_class",
                "forward_calibrated_score", "forward_calibrated_score_pct",
                "trailing_activate_r", "trailing_lock_r",
                "adaptive_entry_snapshot",
            ):
                if match.get(field) is not None:
                    record[field] = copy.deepcopy(match.get(field))
    except Exception:
        pass


def _v6_tracker_update(
    self: TradeExcursionTracker,
    record: Dict[str, Any],
    now: float,
) -> bool:
    triggered = _V6_BASE_TRACKER_UPDATE(self, record, now)
    stop_pct = _record_stop_pct(self, record)
    entry = self._safe_float(record.get("entry_price"))
    mfe_pct = self._safe_float(record.get("mfe_pct"))
    direction = str(record.get("direction") or "").upper().strip()
    if stop_pct <= 0 or entry is None or entry <= 0 or mfe_pct is None:
        return triggered

    activate_r = max(0.25, min(1.50, _to_float(record.get("trailing_activate_r"), 0.75)))
    lock_r = max(0.0, min(0.75, _to_float(record.get("trailing_lock_r"), 0.10)))
    mfe_r = mfe_pct / stop_pct if stop_pct > 0 else 0.0
    desired = self._safe_float(record.get("desired_native_stop_price"))
    if mfe_r >= 1.0:
        lock = max(0.35, lock_r)
        record["trailing_state"] = f"V6_LOCK_{lock:.2f}R"
    elif mfe_r >= activate_r:
        lock = lock_r
        record["trailing_state"] = f"V6_TRAIL_ACTIVE_{lock:.2f}R"
    else:
        lock = None

    if lock is not None:
        lock_pct = stop_pct * lock
        if direction == "BUY":
            v6_desired = entry * (1.0 + lock_pct / 100.0)
            if desired is None or v6_desired > desired:
                desired = v6_desired
        elif direction == "SELL":
            v6_desired = entry * (1.0 - lock_pct / 100.0)
            if desired is None or v6_desired < desired:
                desired = v6_desired
        record["desired_native_stop_price"] = self._round(desired)
    record["mfe_r"] = self._round(mfe_r, 6)
    return triggered


def _v6_tracker_merge(self: TradeExcursionTracker, row: Dict[str, Any]) -> Dict[str, Any]:
    out = _V6_BASE_TRACKER_MERGE(self, row)
    key = str(
        out.get("deal_id") or out.get("ig_deal_id") or out.get("trade_id") or ""
    ).strip()
    excursion = self._lookup(key) if key else None
    if isinstance(excursion, dict):
        for field in (
            "policy_version", "execution_lane", "trade_class",
            "forward_calibrated_score", "forward_calibrated_score_pct",
            "trailing_activate_r", "trailing_lock_r", "mfe_r",
            "adaptive_entry_snapshot",
        ):
            if excursion.get(field) is not None:
                out[field] = copy.deepcopy(excursion.get(field))
    return out


def _v6_tracker_status(self: TradeExcursionTracker) -> Dict[str, Any]:
    out = _V6_BASE_TRACKER_STATUS(self)
    out.update({
        "adaptive_forward_policy": V6_POLICY_VERSION,
        "category_exit_policy": "CATEGORY_VOLATILITY_R_PLUS_MFE_MAE_LEARNING",
        "adaptive_trailing_enabled": True,
        "probation_uses_same_native_stop_protection": True,
    })
    return out


def _v6_reliable_category_tick(self: CategoryExecutionEngine) -> Dict[str, Any]:
    with self._lock:
        health = _execution_health_state(self)
        now = _now()
        health.update({
            "last_tick_started_at": now,
            "tick_count": int(health.get("tick_count") or 0) + 1,
            "candidate_attempts_this_tick": 0,
            "candidate_opens_this_tick": 0,
            "candidate_errors_this_tick": 0,
            "standard_eligible_this_tick": 0,
            "probation_eligible_this_tick": 0,
            "prime_eligible_this_tick": 0,
            "ranked_candidates_this_tick": 0,
            "blocked_eligible_this_tick": 0,
            "skipped_error_cooldown_this_tick": 0,
            "tick_error": None,
            "adaptive_policy_version": V6_POLICY_VERSION,
        })
        blockers: Counter[str] = Counter()

        try:
            health["phase"] = "RECONCILE"
            health["phase_started_at"] = _now()
            self._reconcile()

            health["phase"] = "DUE_CLOSES"
            health["phase_started_at"] = _now()
            self._due_closes()

            configured = bool(getattr(self.broker, "configured", lambda: False)())
            health["broker_configured"] = configured
            try:
                broker_status = self.broker.status() or {}
            except Exception:
                broker_status = {}
            health["broker_connected"] = bool(broker_status.get("connected"))
            health["broker_last_error"] = broker_status.get("last_error")
            health["category_autotrade_enabled"] = bool(self.enabled)
            health["account_funding"] = _v6_account_funding_snapshot(
                self.broker, trigger_refresh=True
            )

            if not self.enabled:
                health["last_open_result"] = "CATEGORY_AUTOTRADE_DISABLED"
            elif not configured:
                health["last_open_result"] = "IG_DEMO_NOT_CONFIGURED"
            else:
                health["phase"] = "EXTERNAL_SNAPSHOT"
                health["phase_started_at"] = _now()
                external = _cached_external_for_tick(self)

                health["phase"] = "RANKINGS_CACHE_READ"
                health["phase_started_at"] = _now()
                rankings = self.ranking_source() or {}
                health["rankings_ready_at"] = _now()
                ranking_owner = getattr(self.ranking_source, "__self__", None)
                if ranking_owner is not None:
                    for target, source in (
                        ("forward_rankings_cache_state", "_jasong_rankings_cache_state"),
                        ("forward_rankings_cache_age_seconds", "_jasong_rankings_cache_age_seconds"),
                        ("forward_rankings_last_refresh_duration_seconds", "_jasong_rankings_last_refresh_duration_seconds"),
                        ("forward_rankings_last_error", "_jasong_rankings_last_error"),
                    ):
                        health[target] = getattr(ranking_owner, source, None)
                    health["forward_rankings_refresh_running"] = bool(
                        getattr(ranking_owner, "_jasong_rankings_refreshing", set())
                    )
                    health["forward_rankings_refresh_deferred"] = bool(
                        getattr(ranking_owner, "_jasong_rankings_refresh_deferred", False)
                    )

                try:
                    if rankings:
                        self._jasong_health_rankings_cache = {
                            str(category): [
                                copy.deepcopy(row)
                                for row in (rows or [])[:5]
                                if isinstance(row, dict)
                            ]
                            for category, rows in rankings.items()
                            if isinstance(rows, list)
                        }
                        self._jasong_health_rankings_cache_at = _now()
                        health["health_rankings_cache_at"] = self._jasong_health_rankings_cache_at
                except Exception:
                    pass

                cooldowns = health.setdefault("candidate_error_cooldowns", {})
                retry_seconds = max(
                    15,
                    min(900, int(os.getenv("CATEGORY_EXECUTION_ERROR_COOLDOWN_SECONDS", "60"))),
                )
                health["error_cooldown_seconds"] = retry_seconds

                for category in ("FOREX", "INDICES", "CRYPTO", "METALS", "ENERGY", "SHARES"):
                    for raw in rankings.get(category, [])[:5]:
                        if not isinstance(raw, dict):
                            continue
                        candidate = dict(raw)
                        health["ranked_candidates_this_tick"] += 1
                        lane = str(candidate.get("execution_lane") or "OBSERVE").upper()
                        if lane == "PRIME":
                            health["prime_eligible_this_tick"] += 1
                            health["standard_eligible_this_tick"] += 1
                        elif lane == "VALIDATED":
                            health["standard_eligible_this_tick"] += 1
                        elif lane == "PROBATION":
                            health["probation_eligible_this_tick"] += 1
                        else:
                            blockers[f"LANE_{lane}_NOT_EXECUTABLE"] += 1
                            continue

                        key = _candidate_key(candidate)
                        until = float(cooldowns.get(key) or 0.0)
                        if until > now:
                            blockers["ERROR_COOLDOWN"] += 1
                            health["skipped_error_cooldown_this_tick"] += 1
                            continue
                        cooldowns.pop(key, None)

                        if lane == "PROBATION":
                            capacity_ok, capacity_reason = _v6_probation_capacity_gate(self, candidate)
                            if not capacity_ok:
                                blockers[capacity_reason] += 1
                                health["blocked_eligible_this_tick"] += 1
                                continue

                        funding_ok, funding_reason, funding = _v6_account_funding_gate(
                            self.broker, candidate
                        )
                        health["account_funding"] = funding
                        if not funding_ok:
                            blockers[funding_reason] += 1
                            health["blocked_eligible_this_tick"] += 1
                            health["last_blocked_candidate"] = {
                                "at": now, "candidate": key, "reason": funding_reason,
                            }
                            continue

                        reentry_ok, reentry_reason = _reentry_reset_gate(self, candidate)
                        if not reentry_ok:
                            blockers[reentry_reason] += 1
                            health["blocked_eligible_this_tick"] += 1
                            health["last_blocked_candidate"] = {
                                "at": now, "candidate": key, "reason": reentry_reason,
                            }
                            continue

                        confirmation_ok, confirmation_reason = _continuation_confirmation(self, candidate)
                        if not confirmation_ok:
                            blockers[confirmation_reason] += 1
                            health["blocked_eligible_this_tick"] += 1
                            health["last_blocked_candidate"] = {
                                "at": now, "candidate": key, "reason": confirmation_reason,
                            }
                            continue

                        allowed, reason = self._may_open(candidate, external)
                        if not allowed:
                            label = str(reason or "BLOCKED").upper().replace(" ", "_")
                            blockers[label] += 1
                            health["blocked_eligible_this_tick"] += 1
                            health["last_blocked_candidate"] = {
                                "at": now, "candidate": key, "reason": reason,
                            }
                            continue

                        health["candidate_attempts_this_tick"] += 1
                        health["total_isolated_candidate_attempts"] = int(
                            health.get("total_isolated_candidate_attempts") or 0
                        ) + 1
                        health["last_open_attempt"] = {
                            "at": _now(),
                            "candidate": key,
                            "execution_lane": lane,
                            "category": candidate.get("category"),
                            "symbol": candidate.get("symbol"),
                            "direction": candidate.get("direction"),
                            "strategy_id": candidate.get("strategy_id") or candidate.get("selected_strategy"),
                            "fast_score": candidate.get("live_fast_score") or candidate.get("smart_fast_score"),
                            "quant_confidence": candidate.get("quant_confidence"),
                            "model_ai_confidence": candidate.get("model_ai_confidence"),
                            "forward_calibrated_score_pct": candidate.get("forward_calibrated_score_pct"),
                        }
                        before_opens = int(self._state.get("opens") or 0)
                        try:
                            self._open_candidate(candidate, external)
                            after_opens = int(self._state.get("opens") or 0)
                            if after_opens > before_opens:
                                health["candidate_opens_this_tick"] += 1
                                health["total_isolated_candidate_opens"] = int(
                                    health.get("total_isolated_candidate_opens") or 0
                                ) + 1
                                health["last_open_result"] = f"OPENED_{lane}"
                                health["last_open_success_at"] = _now()
                                health["last_open_success"] = dict(health["last_open_attempt"])
                                cooldowns.pop(key, None)
                            else:
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
                                "at": _now(), "candidate": key,
                                "execution_lane": lane,
                                "classification": label, "error": message,
                            }
                            recent = health.setdefault("recent_errors", [])
                            recent.append(dict(health["last_candidate_error"]))
                            health["recent_errors"] = recent[-20:]
                            size_plan = health.get("last_size_plan")
                            if (
                                label == "INSUFFICIENT_FUNDS"
                                and isinstance(size_plan, dict)
                                and size_plan.get("candidate") == key
                                and size_plan.get("final_result") == "MINIMUM_DEAL_UNAFFORDABLE"
                            ):
                                label = "MINIMUM_DEAL_UNAFFORDABLE"
                                health["last_open_result"] = label
                                health["last_candidate_error"]["classification"] = label
                                recent[-1]["classification"] = label
                                cooldown_for = max(
                                    60,
                                    min(1800, int(os.getenv("CATEGORY_EXECUTION_FUNDS_COOLDOWN_SECONDS", "300"))),
                                )
                            else:
                                cooldown_for = retry_seconds
                            cooldowns[key] = _now() + cooldown_for
                            blockers[label] += 1
                            try:
                                self._journal(
                                    "OPEN_ERROR_ISOLATED",
                                    candidate=key,
                                    execution_lane=lane,
                                    category=candidate.get("category"),
                                    symbol=candidate.get("symbol"),
                                    classification=label,
                                    error=message,
                                )
                            except Exception:
                                pass
                            continue

                health["candidate_error_cooldowns"] = {
                    key: until for key, until in cooldowns.items()
                    if float(until or 0.0) > now
                }

            health["recent_blockers"] = dict(blockers)
            health["phase"] = "COMPLETE"
            health["phase_started_at"] = _now()
            health["last_tick_completed_at"] = _now()
            health["last_tick_duration_seconds"] = round(
                health["last_tick_completed_at"] - now, 3
            )
            self._state["last_error"] = None
        except Exception as exc:
            message = _compact_error(exc)
            self._state["last_error"] = message
            health["tick_error"] = message
            health["last_open_result"] = "TICK_ERROR"
            health["last_tick_completed_at"] = _now()

        self._state["last_tick_at"] = _now()
        self._persist()
        return {
            "version": VERSION,
            "enabled": bool(self.enabled),
            "open_positions": len(self._open_positions()),
            "last_tick_at": self._state.get("last_tick_at"),
            "last_error": self._state.get("last_error"),
            "execution_health": copy.deepcopy(health),
            "live_money_execution": False,
        }


def _v6_category_status(self: CategoryExecutionEngine) -> Dict[str, Any]:
    out = _V6_BASE_CATEGORY_STATUS(self)
    out["entry_policy"] = {
        "policy_version": V6_POLICY_VERSION,
        "lanes": ["WATCH", "PROBATION", "VALIDATED", "PRIME"],
        "base_live_floors": {"quant_pct": 28.0, "ai_pct": 40.0, "fast": 45.0},
        "probation_category_floors": copy.deepcopy(V6_PROBATION_THRESHOLDS),
        "probation_min_settled_for_promotion": V6_PROBATION_PROMOTION_MIN,
        "probation_promotion_wr_pct": V6_PROMOTION_WR_MIN * 100.0,
        "probation_promotion_pf": V6_PROMOTION_PF_MIN,
        "probation_promotion_expectancy_r": V6_PROMOTION_EXPECTANCY_MIN,
        "validated_historical_route": "60% WR + PF>=1.20 + walk-forward",
        "compound_requires_prime": True,
        "execution_mode": "IG_DEMO_ONLY",
    }
    return out


def _v6_execution_health_snapshot(
    *,
    system: Optional[Dict[str, Any]] = None,
    broker: Any = None,
) -> Dict[str, Any]:
    out = _V6_BASE_HEALTH_SNAPSHOT(system=system, broker=broker)
    system = system or {}
    portfolio = system.get("portfolio")
    rankings = getattr(portfolio, "_jasong_health_rankings_cache", {}) if portfolio is not None else {}
    rankings = rankings if isinstance(rankings, dict) else {}

    counts = Counter()
    by_category: Dict[str, Counter[str]] = {}
    quarantines: List[Dict[str, Any]] = []
    for category, rows in rankings.items():
        cat = Counter()
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            lane = str(row.get("execution_lane") or "OBSERVE").upper()
            counts[lane] += 1
            cat[lane] += 1
            if bool(row.get("adaptive_quarantine")):
                counts["QUARANTINED"] += 1
                cat["QUARANTINED"] += 1
                quarantines.append({
                    "category": category,
                    "symbol": row.get("symbol"),
                    "strategy_id": row.get("strategy_id") or row.get("selected_strategy"),
                    "reasons": row.get("dynamic_quarantine_reasons") or row.get("validation_diagnostics") or [],
                })
        by_category[str(category)] = cat

    category_execution = out.setdefault("category_execution", {})
    category_execution.update({
        "watch_candidates": int(counts.get("WATCH", 0)),
        "probation_eligible_candidates": int(counts.get("PROBATION", 0)),
        "validated_candidates": int(counts.get("VALIDATED", 0)),
        "prime_candidates": int(counts.get("PRIME", 0)),
        "adaptive_quarantined_candidates": int(counts.get("QUARANTINED", 0)),
    })
    existing_by_category = category_execution.get("by_category")
    if isinstance(existing_by_category, dict):
        for category, lane_counts in by_category.items():
            target = existing_by_category.setdefault(category, {})
            target.update({
                "watch": int(lane_counts.get("WATCH", 0)),
                "probation": int(lane_counts.get("PROBATION", 0)),
                "validated": int(lane_counts.get("VALIDATED", 0)),
                "prime": int(lane_counts.get("PRIME", 0)),
                "adaptive_quarantined": int(lane_counts.get("QUARANTINED", 0)),
            })

    funding = _v6_account_funding_snapshot(broker, trigger_refresh=True)
    out["adaptive_forward_trader"] = {
        "version": VERSION,
        "policy_version": V6_POLICY_VERSION,
        "architecture": V6_POLICY_LABEL,
        "lane_counts": {
            "watch": int(counts.get("WATCH", 0)),
            "probation": int(counts.get("PROBATION", 0)),
            "validated": int(counts.get("VALIDATED", 0)),
            "prime": int(counts.get("PRIME", 0)),
            "quarantined": int(counts.get("QUARANTINED", 0)),
        },
        "probation_policy": {
            "category_live_thresholds": copy.deepcopy(V6_PROBATION_THRESHOLDS),
            "minimum_legal_demo_size_bias": True,
            "max_open_global": _v6_env_int("ADAPTIVE_MAX_OPEN_PROBATION", 2, 1, 5),
            "max_open_per_category": _v6_env_int("ADAPTIVE_MAX_OPEN_PROBATION_PER_CATEGORY", 1, 1, 3),
            "continuation_confirmation_required": True,
            "post_trade_reset_required": True,
            "compound_eligible": False,
        },
        "promotion_policy": {
            "min_v6_probation_settled": V6_PROBATION_PROMOTION_MIN,
            "min_win_rate_pct": V6_PROMOTION_WR_MIN * 100.0,
            "min_profit_factor": V6_PROMOTION_PF_MIN,
            "min_expectancy_r": V6_PROMOTION_EXPECTANCY_MIN,
            "max_drawdown_r": V6_PROMOTION_MAX_DD_R,
            "min_bootstrap_positive_pct": V6_PROMOTION_BOOTSTRAP_MIN * 100.0,
        },
        "automatic_quarantine": {
            "review_after_trades": V6_PROBATION_REVIEW_MIN,
            "wr_floor_pct": V6_QUARANTINE_WR_FLOOR * 100.0,
            "pf_floor": V6_QUARANTINE_PF_FLOOR,
            "max_probation_sample": V6_PROBATION_MAX_SETTLED,
            "seeded_failing_families": list(WR_GUARD_SEEDED_QUARANTINES),
            "current": quarantines[:12],
        },
        "score": {
            "name": "FORWARD_CALIBRATED_EXECUTION_SCORE",
            "is_probability": False,
            "uses": [
                "live Quant/AI/FAST",
                "historical WR/PF admission",
                "new V6 forward WR/PF/expectancy",
                "MFE/MAE entry and exit behavior",
                "same-regime forward behavior",
            ],
        },
        "account_funding": funding,
        "live_money_execution": False,
    }

    runtime = out.setdefault("runtime_architecture", {})
    runtime.update({
        "adaptive_forward_trader": True,
        "adaptive_forward_policy_version": V6_POLICY_VERSION,
        "execution_lanes": ["WATCH", "PROBATION", "VALIDATED", "PRIME"],
        "mfe_mae_forward_learning": True,
        "account_funding_snapshot_nonblocking": True,
        "probation_minimum_size_bias": True,
        "compound_prime_only": True,
    })
    broker_policy = out.setdefault("broker_data_policy", {})
    broker_policy.update({
        "account_funding_source": "IG_DEMO_ACCOUNTS_BACKGROUND_CACHE",
        "probation_size_policy": "MINIMUM_LEGAL_SIZE_BIASED_THEN_EXISTING_REJECTION_DOWNSHIFT",
    })

    current_flow = str(out.get("trade_flow_state") or "")
    stale_or_error = current_flow in {
        "EXECUTION_LOOP_STALE", "EXECUTION_TICK_ERROR", "LIVE_SCANNER_STALE",
        "BROKER_NOT_CONFIGURED", "CATEGORY_AUTOTRADE_DISABLED",
    }
    if not stale_or_error:
        if counts.get("PRIME", 0) or counts.get("VALIDATED", 0):
            out["trade_flow_state"] = "READY_TO_EXECUTE_VALIDATED"
        elif counts.get("PROBATION", 0):
            out["trade_flow_state"] = "READY_FOR_CONTROLLED_PROBATION"
        elif counts.get("WATCH", 0):
            out["trade_flow_state"] = "WATCH_ONLY_NO_EXECUTION_LANE"
        else:
            out["trade_flow_state"] = "WAITING_FOR_STRONG_SIGNAL"
    out["version"] = VERSION
    return out


def _v6_runtime_optimizations(
    *,
    system: Optional[Dict[str, Any]],
    broker: Any,
) -> Dict[str, Any]:
    result = _V6_BASE_RUNTIME_OPTIMIZATIONS(system=system, broker=broker)
    _v6_account_funding_snapshot(broker, trigger_refresh=True)
    system = system or {}
    tracker = system.get("excursion_tracker")
    portfolio = system.get("portfolio")
    if tracker is not None and portfolio is not None:
        try:
            tracker._jasong_category_portfolio = portfolio
        except Exception:
            pass
    if isinstance(result, dict):
        result = dict(result)
        result.update({
            "adaptive_forward_trader": True,
            "adaptive_policy_version": V6_POLICY_VERSION,
            "account_funding_background_cache": True,
        })
    return result


def _install_v6_adaptive_forward() -> Dict[str, Any]:
    global _compute_guarded_forward_rankings
    global _age_cached_rankings
    global _tracker_context
    global execution_health_snapshot
    global _install_runtime_execution_optimizations

    if getattr(ForwardPrimeArchitecture, "_jasong_adaptive_v6_patch", False):
        return {"installed": True, "already_installed": True, "version": VERSION}

    # Preserve the V5 tracker context function before replacing the global name.
    globals()["_V6_V5_TRACKER_CONTEXT"] = _tracker_context

    ForwardValidationEngine.metrics = _v6_forward_metrics
    ForwardPrimeArchitecture.enrich = _v6_adaptive_forward_enrich
    _compute_guarded_forward_rankings = _v6_compute_guarded_forward_rankings
    _age_cached_rankings = _v6_age_cached_rankings

    CategoryExecutionEngine._may_open = _v6_category_may_open
    CategoryExecutionEngine._open_candidate = _v6_category_open_candidate
    CategoryExecutionEngine.tick = _v6_reliable_category_tick
    CategoryExecutionEngine.status = _v6_category_status

    _tracker_context = _v6_tracker_context
    TradeExcursionTracker._update_take_profit_fields = _v6_tracker_update
    TradeExcursionTracker.merge = _v6_tracker_merge
    TradeExcursionTracker.status = _v6_tracker_status

    IGDemoBroker.open_epic_position = _v6_open_with_account_invalidate
    IGDemoBroker.close_position = _v6_close_with_account_invalidate

    execution_health_snapshot = _v6_execution_health_snapshot
    _install_runtime_execution_optimizations = _v6_runtime_optimizations

    ForwardPrimeArchitecture._jasong_adaptive_v6_patch = True
    return {
        "installed": True,
        "version": VERSION,
        "policy_version": V6_POLICY_VERSION,
        "watch_probation_validated_prime": True,
        "v6_only_probation_evidence": True,
        "forward_calibrated_execution_score": True,
        "mfe_mae_behavior_learning": True,
        "category_specific_adaptive_exits": True,
        "automatic_quarantine_and_promotion": True,
        "account_funding_background_snapshot": True,
        "probation_minimum_legal_size_bias": True,
        "compound_prime_only": True,
        "ig_demo_only": True,
    }


ADAPTIVE_V6_STATUS = _install_v6_adaptive_forward()
