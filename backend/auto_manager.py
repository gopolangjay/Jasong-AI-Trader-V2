from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional


class AutomatedTradeManager:
    """Paper-only automated candidate manager for Jasong AI Trader V5.6.

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
    ):
        self.scan_candidates_func = scan_candidates_func
        self.validate_candidate_func = validate_candidate_func
        self.watcher_engine = watcher_engine

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
            "scan_interval_minutes": 15,
            "target_active_watchers": 3,
            "scan_top_n": 9,
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
            "max_deep_validations_per_cycle": 1,
            "rotation_mode": "V5.6_PROGRESSIVE",
            "candidate_cooldowns": {},
            "last_rotated_symbol": None,
        }

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
        scan_interval_minutes: int = 15,
        target_active_watchers: int = 3,
        scan_top_n: int = 9,
    ) -> dict:
        if scan_interval_minutes < 5:
            raise ValueError(
                "scan_interval_minutes must be at least 5"
            )

        if (
            target_active_watchers < 1
            or target_active_watchers > 5
        ):
            raise ValueError(
                "target_active_watchers must be between 1 and 5"
            )

        if (
            scan_top_n < target_active_watchers
            or scan_top_n > 9
        ):
            raise ValueError(
                "scan_top_n must be >= target_active_watchers and <= 9"
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
                "max_deep_validations_per_cycle": 1,
            })

        return self.status()

    def disable(self) -> dict:
        with self._lock:
            self._state[
                "enabled"
            ] = False

            self._state[
                "next_run_at"
            ] = None

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

            self._set_progress(
                stage="COMPLETED",
                message="Automated cycle completed",
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
            seconds = 30 * 60
        elif clean_status in {
            "REJECT",
            "NOT_VERIFIED",
            "NO_DATA",
            "ERROR",
        }:
            seconds = 60 * 60
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
    # one automated cycle
    # --------------------------------------------------------------

    def _run_cycle(
        self,
    ) -> dict:
        with self._lock:
            settings = dict(
                self._state
            )

        started_at = time.time()

        active_before = (
            self._active_watchers()
        )

        target = int(
            settings.get(
                "target_active_watchers",
                3,
            )
        )

        available_slots = max(
            0,
            target
            - len(
                active_before
            ),
        )

        result = {
            "started_at":
                started_at,
            "active_before":
                len(
                    active_before
                ),
            "target_active_watchers":
                target,
            "available_slots":
                available_slots,
            "scanned":
                0,
            "deep_validated":
                0,
            "verified_created":
                0,
            "skipped_existing":
                0,
            "skipped_cooldown":
                0,
            "skipped_quarantined":
                0,
            "candidates":
                [],
            "max_deep_validations_per_cycle":
                1,
        }

        if available_slots <= 0:
            result[
                "reason"
            ] = (
                "Watcher portfolio already at target capacity."
            )

            self._finish_run(
                result=result,
                error=None,
            )

            return result

        try:
            self._set_progress(
                stage="FAST_SCAN",
                message="Scanning and ranking markets",
                percent=15,
            )

            candidates = (
                self.scan_candidates_func(
                    top_n=int(
                        settings.get(
                            "scan_top_n",
                            5,
                        )
                    ),
                    payout=float(
                        settings.get(
                            "payout",
                            0.80,
                        )
                    ),
                )
            )

            result[
                "scanned"
            ] = len(
                candidates
            )

            self._set_progress(
                stage="RANKING",
                message=(
                    f"Ranked {len(candidates)} candidates; "
                    "selecting the best eligible market"
                ),
                percent=30,
            )

            active_symbols = (
                self._active_symbols()
            )

            deep_attempts = 0
            max_deep_attempts = int(
                settings.get(
                    "max_deep_validations_per_cycle",
                    1,
                )
            )

            for candidate in candidates:
                if (
                    result[
                        "verified_created"
                    ]
                    >= available_slots
                ):
                    break

                symbol = str(
                    candidate.get(
                        "symbol"
                    )
                    or ""
                )

                if (
                    not symbol
                    or symbol
                    in active_symbols
                ):
                    result[
                        "skipped_existing"
                    ] += 1
                    continue

                if bool(
                    candidate.get(
                        "strategy_quarantined",
                        False,
                    )
                ):
                    result[
                        "skipped_quarantined"
                    ] += 1
                    continue

                if self._cooldown_active(
                    candidate
                ):
                    result[
                        "skipped_cooldown"
                    ] += 1
                    continue

                if (
                    candidate.get(
                        "quality_tier"
                    )
                    == "REJECT"
                ):
                    continue

                if deep_attempts >= max_deep_attempts:
                    result["reason"] = (
                        "V5.5.2 cycle limit reached: "
                        "one heavy deep validation per cycle."
                    )
                    break

                deep_attempts += 1

                market_name = str(
                    candidate.get("market")
                    or symbol
                )

                self._set_progress(
                    stage="DEEP_VALIDATION",
                    message=(
                        f"Deep validating {market_name} "
                        f"({deep_attempts}/{max_deep_attempts})"
                    ),
                    percent=55,
                    candidate=market_name,
                )

                validated = (
                    self.validate_candidate_func(
                        candidate=
                            candidate,
                        risk_mode=str(
                            settings.get(
                                "risk_mode",
                                "Balanced",
                            )
                        ),
                        starting_balance=float(
                            settings.get(
                                "starting_balance",
                                10000.0,
                            )
                        ),
                        payout=float(
                            settings.get(
                                "payout",
                                0.80,
                            )
                        ),
                    )
                )

                result[
                    "deep_validated"
                ] += 1

                summary = {
                    "market":
                        candidate.get(
                            "market"
                        ),
                    "symbol":
                        symbol,
                    "adaptive_rank_score":
                        candidate.get(
                            "adaptive_rank_score"
                        ),
                    "smart_fast_score":
                        candidate.get(
                            "smart_fast_score",
                            candidate.get(
                                "fast_score"
                            ),
                        ),
                    "quality_tier":
                        candidate.get(
                            "quality_tier"
                        ),
                    "strategy_health":
                        candidate.get(
                            "strategy_health"
                        ),
                    "forward_evidence_active":
                        candidate.get(
                            "forward_evidence_active"
                        ),
                    "deep_status":
                        validated.get(
                            "status"
                        ),
                    "verified":
                        bool(
                            validated.get(
                                "verified",
                                False,
                            )
                        ),
                }

                result[
                    "candidates"
                ].append(
                    summary
                )

                with self._lock:
                    self._state[
                        "last_rotated_symbol"
                    ] = symbol

                if not bool(
                    validated.get(
                        "verified",
                        False,
                    )
                ):
                    self._set_candidate_cooldown(
                        candidate,
                        str(
                            validated.get(
                                "status",
                                "NOT_VERIFIED",
                            )
                        ),
                    )
                    continue

                validated[
                    "adaptive_rank_score"
                ] = candidate.get(
                    "adaptive_rank_score"
                )

                validated[
                    "smart_fast_score"
                ] = candidate.get(
                    "smart_fast_score",
                    candidate.get(
                        "fast_score"
                    ),
                )

                validated[
                    "quality_tier"
                ] = candidate.get(
                    "quality_tier"
                )

                validated[
                    "forward_symbol_stats"
                ] = candidate.get(
                    "forward_symbol_stats"
                )

                validated[
                    "strategy_health"
                ] = candidate.get(
                    "strategy_health"
                )

                validated[
                    "strategy_health_reason"
                ] = candidate.get(
                    "strategy_health_reason"
                )

                validated[
                    "strategy_quarantined"
                ] = candidate.get(
                    "strategy_quarantined",
                    False,
                )

                self._set_progress(
                    stage="WATCHER_CREATION",
                    message=(
                        f"{market_name} VERIFIED; creating server watcher"
                    ),
                    percent=85,
                    candidate=market_name,
                )

                watcher = (
                    self.watcher_engine.create(
                        candidate=
                            validated,
                        risk_mode=str(
                            settings.get(
                                "risk_mode",
                                "Balanced",
                            )
                        ),
                        starting_balance=float(
                            settings.get(
                                "starting_balance",
                                10000.0,
                            )
                        ),
                        payout=float(
                            settings.get(
                                "payout",
                                0.80,
                            )
                        ),
                    )
                )

                active_symbols.add(
                    symbol
                )

                result[
                    "verified_created"
                ] += 1

                summary[
                    "watcher_id"
                ] = watcher.get(
                    "watcher_id"
                )

                summary[
                    "watcher_status"
                ] = watcher.get(
                    "status"
                )

            self._set_progress(
                stage="FINALISING",
                message=(
                    "Cycle finished. V5.6 rotation will continue "
                    "through the next eligible ranked candidate."
                ),
                percent=95,
            )

            self._finish_run(
                result=result,
                error=None,
            )

            return result

        except Exception as exc:
            result[
                "error"
            ] = str(
                exc
            )

            self._finish_run(
                result=result,
                error=str(
                    exc
                ),
            )

            # Keep error inside the completed job result instead of
            # crashing the HTTP request/process.
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

            self._stop_event.wait(
                10.0
            )
