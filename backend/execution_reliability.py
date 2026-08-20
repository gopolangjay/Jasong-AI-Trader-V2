from __future__ import annotations

import copy
import os
import threading
import time
from collections import Counter
from typing import Any, Dict, List, Optional

from category_execution_engine import CategoryExecutionEngine
from category_strategy_engine import CategoryStrategyEngine
from ig_demo_broker import IGDemoBroker


VERSION = "6.9.4-execution-reliability"


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


def _patch_ig_demo_marker() -> None:
    # ig_demo_broker.py already hard-codes the DEMO URL, but its adaptive
    # SIZE_INCREMENT branch references self.demo. Older builds never created
    # that attribute, so a normal IG size rejection could abort the retry path.
    # A class-level True is sufficient and cannot enable live execution because
    # IGDemoBroker.BASE_URL itself is the DEMO gateway.
    if not hasattr(IGDemoBroker, "demo"):
        IGDemoBroker.demo = True  # type: ignore[attr-defined]


_ORIGINAL_CATEGORY_TICK = CategoryExecutionEngine.tick
_ORIGINAL_CATEGORY_STATUS = CategoryExecutionEngine.status
_ORIGINAL_STRATEGY_LOOP = CategoryStrategyEngine._loop
_PATCH_LOCK = threading.RLock()
_INSTALLED = False


def _execution_health_state(engine: CategoryExecutionEngine) -> Dict[str, Any]:
    state = engine._state.setdefault("execution_reliability", {})
    state.setdefault("version", VERSION)
    state.setdefault("candidate_error_cooldowns", {})
    state.setdefault("recent_errors", [])
    state.setdefault("recent_blockers", {})
    state.setdefault("total_candidate_errors", 0)
    state.setdefault("total_isolated_candidate_attempts", 0)
    state.setdefault("total_isolated_candidate_opens", 0)
    state.setdefault("tick_count", 0)
    return state


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
            self._reconcile()
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
                external = self._external_positions()
                rankings = self.ranking_source() or {}
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
                            cooldowns[key] = _now() + retry_seconds
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
        return self.status()


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

        self._stop.wait(self.scan_interval_seconds)


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
        CategoryExecutionEngine.tick = _reliable_category_tick
        CategoryExecutionEngine.status = _reliable_category_status
        CategoryStrategyEngine._loop = _reliable_strategy_loop
        _INSTALLED = True

        return {
            "version": VERSION,
            "installed": True,
            "ig_demo_marker_fixed": True,
            "candidate_exception_isolation": True,
            "live_refresh_independent_of_historical_refresh": True,
            "historical_execution_veto": False,
            "live_money_execution": False,
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
    by_category: Dict[str, Dict[str, int]] = {}
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
            ranked += cat_ranked
            strong += cat_strong
            prime += cat_prime
            by_category[category] = {
                "ranked": cat_ranked,
                "strong": cat_strong,
                "prime": cat_prime,
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

    if not configured:
        flow_state = "BROKER_NOT_CONFIGURED"
    elif not enabled:
        flow_state = "CATEGORY_AUTOTRADE_DISABLED"
    elif tick_error:
        flow_state = "EXECUTION_TICK_ERROR"
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
            "last_tick_at": (
                portfolio_state.get("last_tick_at")
                if isinstance(portfolio_state, dict)
                else None
            ),
            "last_tick_age_seconds": _age_seconds(
                portfolio_state.get("last_tick_at")
                if isinstance(portfolio_state, dict)
                else None
            ),
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
            "last_run_at": intelligence_state.get("last_run_at"),
            "last_run_age_seconds": _age_seconds(
                intelligence_state.get("last_run_at")
            ),
            "last_error": intelligence_state.get("last_error"),
            "live_refresh_independent": bool(
                intelligence_state.get("live_refresh_independent")
            ),
            "last_independent_live_refresh_at": intelligence_state.get(
                "last_independent_live_refresh_at"
            ),
            "historical_refresh_execution_veto": False,
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
        "live_money_execution": False,
    }


def install_execution_health_route(
    app: Any,
    *,
    system: Optional[Dict[str, Any]] = None,
    broker: Any = None,
) -> None:
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
