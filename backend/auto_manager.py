
from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional


class AutomatedTradeManager:
    """Paper-only non-blocking candidate manager for Jasong AI Trader V6.6.

    Stability changes:
      - only ONE heavy auto-manager cycle can run at a time
      - enabling Auto Mode schedules the first automatic cycle for LATER
      - manual run-now is queued and returns immediately
      - job status is polled separately
      - scheduler queues jobs instead of blocking itself in a heavy cycle

    No live broker execution is performed.
    """

    ACTIVE_WATCHER_STATUSES = {
        "WATCHING",
        "READY",
        "RISK_BLOCKED",
        "OPEN",
    }

    TERMINAL_JOB_STATUSES = {
        "COMPLETED",
        "FAILED",
        "SKIPPED",
    }

    MAX_JOB_HISTORY = 30

    def __init__(
        self,
        scan_candidates_func: Callable[..., List[Dict[str, Any]]],
        validate_candidate_func: Callable[..., Dict[str, Any]],
        watcher_engine: Any,
        state_store=None,
        learning_engine=None,
    ):
        self.scan_candidates_func = scan_candidates_func
        self.validate_candidate_func = validate_candidate_func
        self.watcher_engine = watcher_engine
        self.state_store = state_store
        self.learning_engine = learning_engine

        # V6.6: deep validation is decoupled from the scan cycle.
        # The manager can keep scanning/scheduling while expensive historical
        # validation runs in a bounded worker pool.
        self.validation_workers = 3
        self._validation_executor = ThreadPoolExecutor(
            max_workers=self.validation_workers,
            thread_name_prefix="jasong-v66-deep",
        )
        self._validation_inflight: Dict[str, Dict[str, Any]] = {}
        self._validation_history: List[Dict[str, Any]] = []

        self._lock = threading.RLock()
        self._run_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._job_order: List[str] = []

        # V5.6 rotation memory. A rejected/WATCH candidate receives a
        # temporary cooldown so the next cycle can move down the ranking
        # instead of repeatedly deep-validating the same market.
        self._candidate_cooldowns: Dict[str, float] = {}
        self._candidate_last_status: Dict[str, str] = {}

        self._state = {
            "enabled": False,
            "risk_mode": "Balanced",
            "starting_balance": 10000.0,
            "payout": 0.80,
            "scan_interval_minutes": 3,
            "target_active_watchers": 6,
            "scan_top_n": 20,
            "last_run_at": None,
            "next_run_at": None,
            "last_result": None,
            "runs": 0,
            "verified_created": 0,
            "last_error": None,
            "run_in_progress": False,
            "active_job_id": None,
            "last_job_id": None,
            "progress_stage": "IDLE",
            "progress_message": "Waiting for next cycle",
            "progress_percent": 0,
            "progress_candidate": None,
            "max_deep_validations_per_cycle": 3,
            "background_validation_workers": 3,
            "background_validations": 0,
            "background_validation_last": None,
            "rotation_mode": "V6.6_NON_BLOCKING_VALIDATION",
            "candidate_cooldowns": {},
            "last_rotated_symbol": None,
        }

        self._restore_state()

    def _restore_state(self) -> None:
        if self.state_store is None:
            return
        saved = self.state_store.load("auto_manager", {})
        if not isinstance(saved, dict):
            return
        state = saved.get("state")
        if isinstance(state, dict):
            self._state.update(state)
            # A process cannot restart with a worker genuinely still running.
            self._state["run_in_progress"] = False
            self._state["active_job_id"] = None
            if self._state.get("enabled"):
                next_run = self._state.get("next_run_at")
                if next_run is None or float(next_run) < time.time():
                    self._state["next_run_at"] = time.time() + 5.0
        cooldowns = saved.get("candidate_cooldowns")
        if isinstance(cooldowns, dict):
            self._candidate_cooldowns = {str(k): float(v) for k, v in cooldowns.items()}
        statuses = saved.get("candidate_last_status")
        if isinstance(statuses, dict):
            self._candidate_last_status = {str(k): str(v) for k, v in statuses.items()}

    def _persist_state(self) -> None:
        if self.state_store is None:
            return
        with self._lock:
            payload = {
                "state": dict(self._state),
                "candidate_cooldowns": dict(self._candidate_cooldowns),
                "candidate_last_status": dict(self._candidate_last_status),
            }
        self.state_store.save("auto_manager", payload)

    # --------------------------------------------------------------
    # lifecycle
    # --------------------------------------------------------------

    def start_thread(self) -> None:
        with self._lock:
            if (
                self._thread is not None
                and self._thread.is_alive()
            ):
                return

            self._stop_event.clear()

            self._thread = threading.Thread(
                target=self._loop,
                name="jasong-v56-auto-manager-scheduler",
                daemon=True,
            )

            self._thread.start()

    def enable(
        self,
        risk_mode: str,
        starting_balance: float,
        payout: float,
        scan_interval_minutes: int = 3,
        target_active_watchers: int = 6,
        scan_top_n: int = 20,
    ) -> dict:
        if scan_interval_minutes < 2:
            raise ValueError(
                "scan_interval_minutes must be at least 2"
            )

        if (
            target_active_watchers < 1
            or target_active_watchers > 8
        ):
            raise ValueError(
                "target_active_watchers must be between 1 and 8"
            )

        if (
            scan_top_n < target_active_watchers
            or scan_top_n > 30
        ):
            raise ValueError(
                "scan_top_n must be >= target_active_watchers and <= 30"
            )

        now = time.time()

        with self._lock:
            self._state.update({
                "enabled": True,
                "risk_mode": risk_mode,
                "starting_balance": float(
                    starting_balance
                ),
                "payout": float(
                    payout
                ),
                "scan_interval_minutes": int(
                    scan_interval_minutes
                ),
                "target_active_watchers": int(
                    target_active_watchers
                ),
                "scan_top_n": int(
                    scan_top_n
                ),
                # V5.5.1: DO NOT launch a heavy scan immediately.
                "next_run_at": (
                    now
                    + int(
                        scan_interval_minutes
                    )
                    * 60
                ),
                "last_error": None,
                "progress_stage": "IDLE",
                "progress_message": "Waiting for next cycle",
                "progress_percent": 0,
                "progress_candidate": None,
                "max_deep_validations_per_cycle": 3,
            })

        self._persist_state()
        return self.status()

    def disable(self) -> dict:
        with self._lock:
            self._state[
                "enabled"
            ] = False

            self._state[
                "next_run_at"
            ] = None

        self._persist_state()
        return self.status()

    def status(self) -> dict:
        with self._lock:
            state = dict(
                self._state
            )

            state[
                "queued_or_running_jobs"
            ] = sum(
                1
                for job in self._jobs.values()
                if job.get(
                    "status"
                )
                in {
                    "QUEUED",
                    "RUNNING",
                }
            )

            state["background_validations"] = len(self._validation_inflight)
            state["background_validation_workers"] = self.validation_workers
            state["validation_inflight"] = [
                {k: v for k, v in item.items() if k != "future"}
                for item in self._validation_inflight.values()
            ]
            state["validation_history"] = list(self._validation_history[-20:])
            return state

    def _set_progress(
        self,
        stage: str,
        message: str,
        percent: int,
        candidate: Optional[str] = None,
    ) -> None:
        with self._lock:
            pct = max(0, min(100, int(percent)))

            self._state["progress_stage"] = stage
            self._state["progress_message"] = message
            self._state["progress_percent"] = pct
            self._state["progress_candidate"] = candidate

            active_job_id = self._state.get("active_job_id")

            if active_job_id:
                job = self._jobs.get(active_job_id)

                if job is not None:
                    job["progress"] = {
                        "stage": stage,
                        "message": message,
                        "percent": pct,
                        "candidate": candidate,
                    }

    # --------------------------------------------------------------
    # jobs
    # --------------------------------------------------------------

    def _prune_jobs(self) -> None:
        with self._lock:
            while (
                len(
                    self._job_order
                )
                > self.MAX_JOB_HISTORY
            ):
                oldest = (
                    self._job_order.pop(
                        0
                    )
                )

                if (
                    oldest
                    == self._state.get(
                        "active_job_id"
                    )
                ):
                    self._job_order.append(
                        oldest
                    )
                    break

                self._jobs.pop(
                    oldest,
                    None,
                )

    def get_job(
        self,
        job_id: str,
    ) -> Optional[dict]:
        with self._lock:
            job = self._jobs.get(
                job_id
            )

            return (
                dict(job)
                if job is not None
                else None
            )

    def list_jobs(self) -> List[dict]:
        with self._lock:
            return [
                dict(
                    self._jobs[
                        job_id
                    ]
                )
                for job_id in reversed(
                    self._job_order
                )
                if job_id
                in self._jobs
            ]

    def queue_run(
        self,
        source: str = "manual",
    ) -> dict:
        """Queue one heavy cycle and return immediately."""

        with self._lock:
            active_job_id = (
                self._state.get(
                    "active_job_id"
                )
            )

            if active_job_id:
                active_job = (
                    self._jobs.get(
                        active_job_id
                    )
                )

                if (
                    active_job
                    and active_job.get(
                        "status"
                    )
                    in {
                        "QUEUED",
                        "RUNNING",
                    }
                ):
                    return {
                        "accepted": False,
                        "status": "ALREADY_RUNNING",
                        "job_id":
                            active_job_id,
                        "job":
                            dict(
                                active_job
                            ),
                    }

            job_id = str(
                uuid.uuid4()
            )

            now = time.time()

            job = {
                "job_id": job_id,
                "status": "QUEUED",
                "source": source,
                "created_at": now,
                "started_at": None,
                "completed_at": None,
                "result": None,
                "error": None,
                "progress": {
                    "stage": "QUEUED",
                    "message": "Waiting for worker",
                    "percent": 0,
                    "candidate": None,
                },
            }

            self._jobs[
                job_id
            ] = job

            self._job_order.append(
                job_id
            )

            self._state[
                "active_job_id"
            ] = job_id

            self._state[
                "last_job_id"
            ] = job_id

        self._prune_jobs()

        worker = threading.Thread(
            target=self._job_worker,
            args=(
                job_id,
            ),
            name=(
                f"jasong-v551-auto-job-"
                f"{job_id[:8]}"
            ),
            daemon=True,
        )

        worker.start()

        return {
            "accepted": True,
            "status": "QUEUED",
            "job_id": job_id,
            "job": self.get_job(
                job_id
            ),
        }

    def _job_worker(
        self,
        job_id: str,
    ) -> None:
        # Absolute single-cycle protection.
        if not self._run_lock.acquire(
            blocking=False
        ):
            with self._lock:
                job = self._jobs.get(
                    job_id
                )

                if job is not None:
                    job[
                        "status"
                    ] = "SKIPPED"
                    job[
                        "completed_at"
                    ] = time.time()
                    job[
                        "error"
                    ] = (
                        "Another auto-manager cycle is already running."
                    )

                if (
                    self._state.get(
                        "active_job_id"
                    )
                    == job_id
                ):
                    self._state[
                        "active_job_id"
                    ] = None

            return

        try:
            with self._lock:
                job = self._jobs.get(
                    job_id
                )

                if job is None:
                    return

                job[
                    "status"
                ] = "RUNNING"
                job[
                    "started_at"
                ] = time.time()

                self._state[
                    "run_in_progress"
                ] = True

            self._set_progress(
                stage="STARTING",
                message="Preparing automated cycle",
                percent=5,
            )

            result = (
                self._run_cycle()
            )

            with self._lock:
                job = self._jobs.get(
                    job_id
                )

                if job is not None:
                    job[
                        "status"
                    ] = "COMPLETED"
                    job[
                        "result"
                    ] = result
                    job[
                        "completed_at"
                    ] = time.time()

            if self._validation_capacity() < self.validation_workers:
                self._set_progress(
                    stage="BACKGROUND_VALIDATION",
                    message=f"{len(self._validation_inflight)} deep validation job(s) running in background",
                    percent=65,
                )
            else:
                self._set_progress(
                    stage="COMPLETED",
                    message="Automated discovery cycle completed",
                    percent=100,
                )

        except Exception as exc:
            with self._lock:
                job = self._jobs.get(
                    job_id
                )

                if job is not None:
                    job[
                        "status"
                    ] = "FAILED"
                    job[
                        "error"
                    ] = str(
                        exc
                    )
                    job[
                        "completed_at"
                    ] = time.time()

                self._state[
                    "last_error"
                ] = str(
                    exc
                )

        finally:
            with self._lock:
                self._state[
                    "run_in_progress"
                ] = False

                if (
                    self._state.get(
                        "active_job_id"
                    )
                    == job_id
                ):
                    self._state[
                        "active_job_id"
                    ] = None

            self._run_lock.release()

    # --------------------------------------------------------------
    # V5.6 progressive candidate rotation
    # --------------------------------------------------------------

    @staticmethod
    def _rotation_key(
        candidate: Dict[str, Any],
    ) -> str:
        return (
            f"{candidate.get('symbol') or ''}:"
            f"{str(candidate.get('direction') or '').upper()}"
        )

    def _cooldown_active(
        self,
        candidate: Dict[str, Any],
    ) -> bool:
        key = self._rotation_key(
            candidate
        )

        until = float(
            self._candidate_cooldowns.get(
                key,
                0.0,
            )
            or 0.0
        )

        return time.time() < until

    def _set_candidate_cooldown(
        self,
        candidate: Dict[str, Any],
        status: str,
    ) -> None:
        key = self._rotation_key(
            candidate
        )

        clean_status = str(
            status
            or ""
        ).upper()

        # Avoid hammering the same expensive deep-validation candidate.
        # WATCH gets a shorter retry than REJECT/NOT_VERIFIED.
        if clean_status in {
            "WATCH",
            "NEAR_VERIFIED",
        }:
            seconds = 12 * 60
        elif clean_status in {
            "REJECT",
            "NOT_VERIFIED",
            "NO_DATA",
            "ERROR",
        }:
            seconds = 20 * 60
        else:
            seconds = 15 * 60

        self._candidate_cooldowns[
            key
        ] = (
            time.time()
            + seconds
        )

        self._candidate_last_status[
            key
        ] = clean_status

        with self._lock:
            self._state[
                "candidate_cooldowns"
            ] = {
                item_key: round(
                    max(
                        0.0,
                        until
                        - time.time(),
                    )
                    / 60.0,
                    1,
                )
                for item_key, until
                in self._candidate_cooldowns.items()
                if until
                > time.time()
            }

    # --------------------------------------------------------------
    # watcher portfolio
    # --------------------------------------------------------------

    def _active_watchers(
        self,
    ) -> List[dict]:
        return [
            item
            for item in self.watcher_engine.list()
            if item.get(
                "status"
            )
            in self.ACTIVE_WATCHER_STATUSES
        ]

    def _active_symbols(
        self,
    ) -> set[str]:
        return {
            str(
                item.get(
                    "symbol"
                )
                or ""
            )
            for item
            in self._active_watchers()
            if item.get(
                "symbol"
            )
        }

    # --------------------------------------------------------------
    # V6.6 non-blocking deep-validation pool
    # --------------------------------------------------------------

    def _validation_key(self, candidate: Dict[str, Any]) -> str:
        return self._rotation_key(candidate)

    def _validation_capacity(self) -> int:
        with self._lock:
            return max(0, self.validation_workers - len(self._validation_inflight))

    def _queue_background_validation(self, candidate: Dict[str, Any], settings: Dict[str, Any]) -> bool:
        key = self._validation_key(candidate)
        market_name = str(candidate.get("market") or candidate.get("symbol") or key)

        with self._lock:
            if key in self._validation_inflight:
                return False
            if len(self._validation_inflight) >= self.validation_workers:
                return False
            meta = {
                "key": key,
                "market": market_name,
                "symbol": str(candidate.get("symbol") or ""),
                "direction": str(candidate.get("direction") or "").upper(),
                "queued_at": time.time(),
                "status": "RUNNING",
            }
            self._validation_inflight[key] = meta
            self._state["background_validations"] = len(self._validation_inflight)

        future = self._validation_executor.submit(
            self.validate_candidate_func,
            candidate=candidate,
            risk_mode=str(settings.get("risk_mode", "Balanced")),
            starting_balance=float(settings.get("starting_balance", 10000.0)),
            payout=float(settings.get("payout", 0.80)),
        )

        with self._lock:
            if key in self._validation_inflight:
                self._validation_inflight[key]["future"] = future

        future.add_done_callback(
            lambda f, c=dict(candidate), s=dict(settings), k=key: self._complete_background_validation(k, c, s, f)
        )
        return True

    def _complete_background_validation(
        self,
        key: str,
        candidate: Dict[str, Any],
        settings: Dict[str, Any],
        future: Future,
    ) -> None:
        market_name = str(candidate.get("market") or candidate.get("symbol") or key)
        symbol = str(candidate.get("symbol") or "")
        completed_at = time.time()

        try:
            validated = dict(future.result() or {})
            error = None
        except Exception as exc:
            validated = {
                "market": candidate.get("market"),
                "symbol": symbol,
                "direction": candidate.get("direction"),
                "status": "ERROR",
                "verified": False,
                "explanation": str(exc),
            }
            error = str(exc)

        # Preserve ranking context for watcher/learning analytics.
        for field in (
            "adaptive_rank_score", "smart_fast_score", "quality_tier",
            "forward_symbol_stats", "strategy_health",
            "strategy_health_reason", "strategy_quarantined",
        ):
            if validated.get(field) is None and candidate.get(field) is not None:
                validated[field] = candidate.get(field)

        learning_submit_error = None
        if self.learning_engine is not None:
            try:
                self.learning_engine.submit_candidate(
                    candidate=candidate,
                    validated=validated,
                    risk_mode=str(settings.get("risk_mode", "Balanced")),
                    starting_balance=float(settings.get("starting_balance", 10000.0)),
                    payout=float(settings.get("payout", 0.80)),
                )
            except Exception as exc:
                learning_submit_error = str(exc)

        watcher_id = None
        watcher_status = None
        if bool(validated.get("verified", False)) and symbol:
            try:
                active = self._active_watchers()
                target = int(settings.get("target_active_watchers", 6))
                active_symbols = {str(w.get("symbol") or "") for w in active}
                if symbol not in active_symbols and len(active) < target:
                    watcher = self.watcher_engine.create(
                        candidate=validated,
                        risk_mode=str(settings.get("risk_mode", "Balanced")),
                        starting_balance=float(settings.get("starting_balance", 10000.0)),
                        payout=float(settings.get("payout", 0.80)),
                    )
                    watcher_id = watcher.get("watcher_id")
                    watcher_status = watcher.get("status")
                    with self._lock:
                        self._state["verified_created"] = int(self._state.get("verified_created", 0)) + 1
            except Exception as exc:
                watcher_status = f"WATCHER_ERROR: {exc}"

        if not bool(validated.get("verified", False)):
            self._set_candidate_cooldown(candidate, str(validated.get("status", "NOT_VERIFIED")))

        summary = {
            "market": market_name,
            "symbol": symbol,
            "direction": str(candidate.get("direction") or "").upper(),
            "status": str(validated.get("status") or "NOT_VERIFIED"),
            "verified": bool(validated.get("verified", False)),
            "queued_at": None,
            "completed_at": completed_at,
            "elapsed_seconds": None,
            "watcher_id": watcher_id,
            "watcher_status": watcher_status,
            "learning_submit_error": learning_submit_error,
            "error": error,
        }

        with self._lock:
            meta = self._validation_inflight.pop(key, None) or {}
            summary["queued_at"] = meta.get("queued_at")
            if summary["queued_at"]:
                summary["elapsed_seconds"] = round(completed_at - float(summary["queued_at"]), 1)
            self._validation_history.append(summary)
            self._validation_history = self._validation_history[-100:]
            self._state["background_validations"] = len(self._validation_inflight)
            self._state["background_validation_last"] = summary
            self._state["last_rotated_symbol"] = symbol
            if self._validation_inflight:
                self._state["progress_stage"] = "BACKGROUND_VALIDATION"
                self._state["progress_message"] = f"{len(self._validation_inflight)} deep validation job(s) running in background"
                self._state["progress_percent"] = 65
                self._state["progress_candidate"] = None
            else:
                self._state["progress_stage"] = "IDLE"
                self._state["progress_message"] = "Background validation complete; waiting for next scan"
                self._state["progress_percent"] = 100
                self._state["progress_candidate"] = None

        self._persist_state()

    # --------------------------------------------------------------
    # one automated cycle
    # --------------------------------------------------------------

    def _run_cycle(
        self,
    ) -> dict:
        """Run a fast discovery cycle and QUEUE deep validation.

        V6.6 deliberately does not wait for expensive optimiser/backtest work.
        The scan cycle returns quickly while a bounded background pool validates
        the best eligible candidates. This keeps Auto Manager responsive and
        allows the next discovery cycle to occur on schedule.
        """
        with self._lock:
            settings = dict(self._state)

        started_at = time.time()
        active_before = self._active_watchers()
        target = int(settings.get("target_active_watchers", 6))
        available_slots = max(0, target - len(active_before))
        max_queue = int(settings.get("max_deep_validations_per_cycle", 3))
        capacity = self._validation_capacity()
        queue_limit = min(max_queue, capacity)

        result = {
            "version": "6.6.0",
            "started_at": started_at,
            "active_before": len(active_before),
            "target_active_watchers": target,
            "available_slots": available_slots,
            "scanned": 0,
            "deep_queued": 0,
            "deep_validated": 0,
            "verified_created": 0,
            "skipped_existing": 0,
            "skipped_cooldown": 0,
            "skipped_quarantined": 0,
            "skipped_inflight": 0,
            "candidates": [],
            "background_validation_capacity": capacity,
            "max_deep_validations_per_cycle": max_queue,
            "validation_mode": "V6.6_NON_BLOCKING_POOL",
        }

        try:
            self._set_progress(
                stage="FAST_SCAN",
                message="Scanning and ranking markets",
                percent=15,
            )
            candidates = self.scan_candidates_func(
                top_n=int(settings.get("scan_top_n", 20)),
                payout=float(settings.get("payout", 0.80)),
            )
            result["scanned"] = len(candidates)

            self._set_progress(
                stage="RANKING",
                message=f"Ranked {len(candidates)} candidates; queueing background validation",
                percent=35,
            )

            active_symbols = self._active_symbols()
            queued = 0
            for candidate in candidates:
                if queued >= queue_limit:
                    break

                symbol = str(candidate.get("symbol") or "")
                key = self._validation_key(candidate)
                if not symbol or symbol in active_symbols:
                    result["skipped_existing"] += 1
                    continue
                if bool(candidate.get("strategy_quarantined", False)):
                    result["skipped_quarantined"] += 1
                    continue
                if self._cooldown_active(candidate):
                    result["skipped_cooldown"] += 1
                    continue
                if candidate.get("quality_tier") == "REJECT":
                    continue
                with self._lock:
                    if key in self._validation_inflight:
                        result["skipped_inflight"] += 1
                        continue

                ok = self._queue_background_validation(candidate, settings)
                if not ok:
                    continue
                queued += 1
                result["deep_queued"] += 1
                result["candidates"].append({
                    "market": candidate.get("market"),
                    "symbol": symbol,
                    "direction": candidate.get("direction"),
                    "smart_fast_score": candidate.get("smart_fast_score", candidate.get("fast_score")),
                    "quality_tier": candidate.get("quality_tier"),
                    "validation_status": "RUNNING_IN_BACKGROUND",
                })

            inflight = len(self._validation_inflight)
            if inflight:
                self._set_progress(
                    stage="BACKGROUND_VALIDATION",
                    message=f"{inflight} deep validation job(s) running; scanner remains responsive",
                    percent=65,
                )
            else:
                self._set_progress(
                    stage="IDLE",
                    message="No eligible deep-validation work; waiting for next cycle",
                    percent=100,
                )

            result["background_validations"] = inflight
            result["elapsed_seconds"] = round(time.time() - started_at, 3)
            return result

        except Exception as exc:
            result["error"] = str(exc)
            result["elapsed_seconds"] = round(time.time() - started_at, 3)
            return result

    def _finish_run(
        self,
        result: dict,
        error: Optional[str],
    ) -> None:
        now = time.time()

        with self._lock:
            interval_minutes = int(
                self._state.get(
                    "scan_interval_minutes",
                    15,
                )
            )

            self._state[
                "last_run_at"
            ] = now

            self._state[
                "last_result"
            ] = result

            self._state[
                "last_error"
            ] = error

            self._state[
                "runs"
            ] = int(
                self._state.get(
                    "runs",
                    0,
                )
            ) + 1

            self._state[
                "verified_created"
            ] = int(
                self._state.get(
                    "verified_created",
                    0,
                )
            ) + int(
                result.get(
                    "verified_created",
                    0,
                )
            )

            if self._state.get(
                "enabled"
            ):
                self._state[
                    "next_run_at"
                ] = (
                    now
                    + interval_minutes
                    * 60
                )
            else:
                self._state[
                    "next_run_at"
                ] = None

        self._persist_state()

    # --------------------------------------------------------------
    # background scheduler
    # --------------------------------------------------------------

    def _loop(
        self,
    ) -> None:
        while not self._stop_event.is_set():
            try:
                with self._lock:
                    enabled = bool(
                        self._state.get(
                            "enabled",
                            False,
                        )
                    )

                    next_run_at = (
                        self._state.get(
                            "next_run_at"
                        )
                    )

                    run_in_progress = bool(
                        self._state.get(
                            "run_in_progress",
                            False,
                        )
                    )

                now = time.time()

                if (
                    enabled
                    and not run_in_progress
                    and next_run_at is not None
                    and now
                    >= float(
                        next_run_at
                    )
                ):
                    queued = self.queue_run(
                        source="scheduler"
                    )

                    # Prevent scheduler from queueing repeatedly while the
                    # worker is still being created.
                    if queued.get(
                        "accepted"
                    ):
                        with self._lock:
                            interval_minutes = int(
                                self._state.get(
                                    "scan_interval_minutes",
                                    15,
                                )
                            )

                            self._state[
                                "next_run_at"
                            ] = (
                                now
                                + interval_minutes
                                * 60
                            )

            except Exception as exc:
                with self._lock:
                    self._state[
                        "last_error"
                    ] = str(
                        exc
                    )

            self._persist_state()
            self._stop_event.wait(
                10.0
            )
