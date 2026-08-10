from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List


class AutomatedTradeManager:
    """Paper-only automated candidate management for Jasong AI Trader V5.5.

    Responsibilities:
      - periodically scan markets
      - rank candidates
      - deep-validate candidates
      - create watchers for VERIFIED setups
      - replace expired/rejected opportunities automatically
      - leave entry, risk control, opening and settlement to TradeWatcherEngine

    No broker execution is performed.
    """

    ACTIVE_WATCHER_STATUSES = {
        "WATCHING",
        "READY",
        "RISK_BLOCKED",
        "OPEN",
    }

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
        self._stop_event = threading.Event()
        self._thread = None

        self._state = {
            "enabled": False,
            "risk_mode": "Balanced",
            "starting_balance": 10000.0,
            "payout": 0.80,
            "scan_interval_minutes": 15,
            "target_active_watchers": 3,
            "scan_top_n": 5,
            "last_run_at": None,
            "next_run_at": None,
            "last_result": None,
            "runs": 0,
            "verified_created": 0,
            "last_error": None,
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
                name="jasong-v55-auto-manager",
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
        scan_top_n: int = 5,
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
                "next_run_at": time.time(),
                "last_error": None,
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
            return dict(
                self._state
            )

    # --------------------------------------------------------------
    # watcher portfolio
    # --------------------------------------------------------------

    def _active_watchers(self) -> List[dict]:
        return [
            item
            for item in self.watcher_engine.list()
            if item.get(
                "status"
            )
            in self.ACTIVE_WATCHER_STATUSES
        ]

    def _active_symbols(self) -> set[str]:
        return {
            str(
                item.get(
                    "symbol"
                )
                or ""
            )
            for item in self._active_watchers()
            if item.get(
                "symbol"
            )
        }

    # --------------------------------------------------------------
    # one automated cycle
    # --------------------------------------------------------------

    def run_now(self) -> dict:
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
            "candidates":
                [],
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

            active_symbols = (
                self._active_symbols()
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

                # REJECT-tier discovery candidates are not worth a heavy
                # deep-validation request.
                if (
                    candidate.get(
                        "quality_tier"
                    )
                    == "REJECT"
                ):
                    continue

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

                if not bool(
                    validated.get(
                        "verified",
                        False,
                    )
                ):
                    continue

                # Preserve discovery/adaptive context in the watcher snapshot.
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

            return result

    def _finish_run(
        self,
        result: dict,
        error: str | None,
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

    def _loop(self) -> None:
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

                now = time.time()

                if (
                    enabled
                    and (
                        next_run_at is None
                        or now
                        >= float(
                            next_run_at
                        )
                    )
                ):
                    self.run_now()

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
