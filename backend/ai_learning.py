from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional


# ============================================================
# JASONG AI TRADER V6.6
# AUTONOMOUS AI PAPER LEARNING ENGINE
#
# Purpose:
# - Runs on the backend, not on the phone.
# - Continues while the Android app is closed.
# - Uses the existing Jasong endpoints:
#     /fast-scan
#     /signal
#     /paper-trades
#     /auto-dashboard
# - Opens PAPER trades only.
# - Maximum one open PAPER trade by default.
# - Uses AI40 directional eligibility.
# - Records the decision snapshot so outcomes can be analysed later.
# ============================================================


@dataclass
class AILearningConfig:
    base_url: str
    enabled: bool = True
    interval_seconds: int = 120

    starting_balance: float = 10000.0
    risk_mode: str = "Balanced"
    payout: float = 0.80

    scan_period: str = "5d"
    scan_interval: str = "15m"
    scan_top_n: int = 9

    ai_confidence_floor: float = 0.40
    maximum_open_trades: int = 1

    # Fallback only. If /signal returns suggested_paper_stake,
    # the backend-suggested stake is preferred.
    fallback_stake_pct: float = 0.01

    # Persistent Render disk is ideal:
    # /var/data/ai_learning_state.json
    state_path: str = "/tmp/jasong_ai_learning_state.json"

    @classmethod
    def from_env(cls) -> "AILearningConfig":
        base_url = os.getenv(
            "JASONG_API_BASE_URL",
            "https://jasong-ai-trader-v2.onrender.com",
        ).rstrip("/")

        enabled = os.getenv(
            "AI_LEARNING_ENABLED",
            "true",
        ).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        return cls(
            base_url=base_url,
            enabled=enabled,
            interval_seconds=max(
                60,
                int(os.getenv("AI_LEARNING_INTERVAL_SECONDS", "120")),
            ),
            starting_balance=float(
                os.getenv("AI_LEARNING_STARTING_BALANCE", "10000")
            ),
            risk_mode=os.getenv(
                "AI_LEARNING_RISK_MODE",
                "Balanced",
            ),
            payout=float(
                os.getenv("AI_LEARNING_PAYOUT", "0.80")
            ),
            scan_top_n=min(
                9,
                max(
                    1,
                    int(os.getenv("AI_LEARNING_TOP_N", "9")),
                ),
            ),
            ai_confidence_floor=float(
                os.getenv("AI_LEARNING_AI_FLOOR", "0.40")
            ),
            maximum_open_trades=max(
                1,
                int(os.getenv("AI_LEARNING_MAX_OPEN", "1")),
            ),
            fallback_stake_pct=float(
                os.getenv("AI_LEARNING_STAKE_PCT", "0.01")
            ),
            state_path=os.getenv(
                "AI_LEARNING_STATE_PATH",
                "/tmp/jasong_ai_learning_state.json",
            ),
        )


class AILearningEngine:
    def __init__(
        self,
        config: Optional[AILearningConfig] = None,
    ) -> None:
        self.config = config or AILearningConfig.from_env()

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._cycle_lock = threading.Lock()

        self._state_lock = threading.Lock()
        self._state: Dict[str, Any] = {
            "engine": "AI_LEARNING",
            "entry_path": "AI40",
            "enabled": self.config.enabled,
            "running": False,
            "last_cycle_started_at": None,
            "last_cycle_finished_at": None,
            "last_action": "NOT_STARTED",
            "last_reason": None,
            "last_error": None,
            "last_scan_count": 0,
            "last_qualified_count": 0,
            "last_selected": None,
            "learning_trades": [],
        }

        self._load_state()

    # ========================================================
    # STATE
    # ========================================================

    def _state_file(self) -> Path:
        return Path(self.config.state_path)

    def _load_state(self) -> None:
        path = self._state_file()

        if not path.exists():
            return

        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))

            if isinstance(loaded, dict):
                self._state.update(loaded)

            # Process state never survives a Render restart.
            self._state["running"] = False

        except Exception:
            # A damaged diagnostic state file must never stop trading.
            pass

    def _save_state(self) -> None:
        path = self._state_file()

        try:
            path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            temporary = path.with_suffix(
                path.suffix + ".tmp"
            )

            temporary.write_text(
                json.dumps(
                    self._state,
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )

            temporary.replace(path)

        except Exception:
            # PAPER trading must not fail solely because
            # a diagnostic state file cannot be written.
            pass

    def _set_state(self, **values: Any) -> None:
        with self._state_lock:
            self._state.update(values)
            self._save_state()

    # ========================================================
    # HTTP
    # ========================================================

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        timeout_seconds: int = 180,
        maximum_attempts: int = 3,
    ) -> Dict[str, Any]:
        url = f"{self.config.base_url}{path}"

        if params:
            encoded = urllib.parse.urlencode(
                {
                    key: value
                    for key, value in params.items()
                    if value is not None
                }
            )

            url = f"{url}?{encoded}"

        last_error: Optional[BaseException] = None

        for attempt in range(
            1,
            maximum_attempts + 1,
        ):
            try:
                request = urllib.request.Request(
                    url,
                    method=method.upper(),
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "Jasong-AI-Learning-V6.6",
                    },
                )

                with urllib.request.urlopen(
                    request,
                    timeout=timeout_seconds,
                ) as response:
                    raw = response.read().decode("utf-8")

                    decoded = json.loads(raw)

                    if not isinstance(decoded, dict):
                        raise ValueError(
                            f"Unexpected JSON from {path}"
                        )

                    return decoded

            except (
                urllib.error.URLError,
                urllib.error.HTTPError,
                TimeoutError,
                ConnectionError,
                json.JSONDecodeError,
                ValueError,
            ) as exc:
                last_error = exc

                if attempt >= maximum_attempts:
                    raise

                time.sleep(
                    min(
                        10,
                        attempt * 3,
                    )
                )

        raise RuntimeError(
            f"Request failed: {last_error}"
        )

    # ========================================================
    # NORMALISATION
    # ========================================================

    @staticmethod
    def _float(
        value: Any,
        default: float = 0.0,
    ) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @classmethod
    def _probability(
        cls,
        value: Any,
        default: float = 0.0,
    ) -> float:
        number = cls._float(
            value,
            default,
        )

        if number > 1.0:
            number /= 100.0

        return max(
            0.0,
            min(
                1.0,
                number,
            ),
        )

    @staticmethod
    def _direction(
        item: Mapping[str, Any],
    ) -> str:
        return str(
            item.get("direction")
            or item.get("decision")
            or item.get("side")
            or "WAIT"
        ).upper()

    @staticmethod
    def _market(
        item: Mapping[str, Any],
    ) -> str:
        return str(
            item.get("symbol")
            or item.get("market")
            or ""
        ).strip()

    @classmethod
    def _fast_score(
        cls,
        item: Mapping[str, Any],
    ) -> float:
        return cls._float(
            item.get("smart_fast_score")
            or item.get("fast_score")
            or item.get("score")
            or 0.0
        )

    def _candidate_list(
        self,
        scan: Mapping[str, Any],
    ) -> List[Dict[str, Any]]:
        raw = (
            scan.get("ranking")
            or scan.get("top_candidates")
            or scan.get("candidates")
            or []
        )

        if not isinstance(raw, list):
            return []

        candidates: List[Dict[str, Any]] = []

        for item in raw:
            if isinstance(item, dict):
                candidates.append(
                    dict(item)
                )

        candidates.sort(
            key=self._fast_score,
            reverse=True,
        )

        return candidates[
            : self.config.scan_top_n
        ]

    # ========================================================
    # DASHBOARD / EXISTING PAPER TRADES
    # ========================================================

    def _dashboard(self) -> Dict[str, Any]:
        return self._request_json(
            "GET",
            "/auto-dashboard",
            params={
                "starting_balance":
                    self.config.starting_balance,
            },
            timeout_seconds=60,
        )

    def _paper_trades(
        self,
        dashboard: Mapping[str, Any],
    ) -> List[Dict[str, Any]]:
        raw = dashboard.get(
            "paper_trades",
            [],
        )

        if not isinstance(raw, list):
            return []

        return [
            dict(item)
            for item in raw
            if isinstance(item, dict)
        ]

    def _open_trade_count(
        self,
        dashboard: Mapping[str, Any],
    ) -> int:
        statuses = {
            "OPEN",
            "READY",
            "PENDING",
            "ACTIVE",
        }

        return sum(
            1
            for trade in self._paper_trades(
                dashboard
            )
            if str(
                trade.get(
                    "status",
                    "",
                )
            ).upper()
            in statuses
        )

    # ========================================================
    # LEARNING OUTCOME SYNC
    # ========================================================

    @staticmethod
    def _trade_identity(
        item: Mapping[str, Any],
    ) -> Optional[str]:
        value = (
            item.get("trade_id")
            or item.get("id")
            or item.get("paper_trade_id")
        )

        if value is None:
            return None

        return str(value)

    def _same_trade(
        self,
        learning_trade: Mapping[str, Any],
        dashboard_trade: Mapping[str, Any],
    ) -> bool:
        learning_id = self._trade_identity(
            learning_trade
        )

        dashboard_id = self._trade_identity(
            dashboard_trade
        )

        if (
            learning_id
            and dashboard_id
            and learning_id == dashboard_id
        ):
            return True

        if (
            self._market(learning_trade)
            != self._market(dashboard_trade)
        ):
            return False

        if (
            self._direction(learning_trade)
            != self._direction(dashboard_trade)
        ):
            return False

        learning_entry = self._float(
            learning_trade.get(
                "entry_price"
            ),
            -1.0,
        )

        dashboard_entry = self._float(
            dashboard_trade.get(
                "entry_price"
            ),
            -2.0,
        )

        if (
            learning_entry > 0
            and dashboard_entry > 0
        ):
            tolerance = max(
                1e-8,
                abs(learning_entry) * 1e-5,
            )

            return (
                abs(
                    learning_entry
                    - dashboard_entry
                )
                <= tolerance
            )

        return True

    def _sync_learning_outcomes(
        self,
        dashboard: Mapping[str, Any],
    ) -> None:
        dashboard_trades = self._paper_trades(
            dashboard
        )

        with self._state_lock:
            learning_trades = self._state.get(
                "learning_trades",
                [],
            )

            if not isinstance(
                learning_trades,
                list,
            ):
                learning_trades = []

            changed = False

            for record in learning_trades:
                if not isinstance(
                    record,
                    dict,
                ):
                    continue

                current_status = str(
                    record.get(
                        "status",
                        "OPEN",
                    )
                ).upper()

                if current_status in {
                    "WIN",
                    "LOSS",
                    "CLOSED",
                }:
                    continue

                for dashboard_trade in dashboard_trades:
                    if not self._same_trade(
                        record,
                        dashboard_trade,
                    ):
                        continue

                    new_status = str(
                        dashboard_trade.get(
                            "status",
                            current_status,
                        )
                    ).upper()

                    record.update(
                        {
                            "status":
                                new_status,
                            "exit_price":
                                dashboard_trade.get(
                                    "exit_price"
                                ),
                            "pnl":
                                dashboard_trade.get(
                                    "pnl"
                                ),
                            "settled_at":
                                dashboard_trade.get(
                                    "settled_at"
                                )
                                or dashboard_trade.get(
                                    "closed_at"
                                ),
                        }
                    )

                    if (
                        new_status
                        != current_status
                    ):
                        changed = True

                    break

            if changed:
                self._state[
                    "learning_trades"
                ] = learning_trades

            self._state[
                "learning_stats"
            ] = self._learning_stats_locked(
                learning_trades
            )

            self._save_state()

    def _learning_stats_locked(
        self,
        trades: List[Any],
    ) -> Dict[str, Any]:
        valid = [
            item
            for item in trades
            if isinstance(item, dict)
        ]

        settled = [
            item
            for item in valid
            if str(
                item.get(
                    "status",
                    "",
                )
            ).upper()
            in {
                "WIN",
                "LOSS",
                "CLOSED",
            }
        ]

        wins = sum(
            1
            for item in settled
            if str(
                item.get(
                    "status",
                    "",
                )
            ).upper()
            == "WIN"
        )

        losses = sum(
            1
            for item in settled
            if str(
                item.get(
                    "status",
                    "",
                )
            ).upper()
            == "LOSS"
        )

        pnl_values = [
            self._float(
                item.get("pnl"),
                0.0,
            )
            for item in settled
        ]

        total_pnl = sum(
            pnl_values
        )

        win_rate = (
            wins / len(settled) * 100.0
            if settled
            else None
        )

        return {
            "total_ai_learning_trades":
                len(valid),
            "open":
                len(valid) - len(settled),
            "settled":
                len(settled),
            "wins":
                wins,
            "losses":
                losses,
            "win_rate_pct":
                round(
                    win_rate,
                    2,
                )
                if win_rate is not None
                else None,
            "total_pnl":
                round(
                    total_pnl,
                    2,
                ),
        }

    # ========================================================
    # AI40 DECISION
    # ========================================================

    def _signal(
        self,
        symbol: str,
    ) -> Dict[str, Any]:
        return self._request_json(
            "GET",
            "/signal",
            params={
                "symbol":
                    symbol,
                "risk_mode":
                    self.config.risk_mode,
                "balance":
                    self.config.starting_balance,
            },
            timeout_seconds=120,
        )

    def _candidate_signal_agrees(
        self,
        candidate_direction: str,
        signal_direction: str,
    ) -> bool:
        return (
            candidate_direction
            in {"BUY", "SELL"}
            and signal_direction
            == candidate_direction
        )

    def _ai_directional_confidence(
        self,
        direction: str,
        signal: Mapping[str, Any],
    ) -> float:
        up_probability = self._probability(
            signal.get(
                "combined_up_probability",
                0.50,
            ),
            0.50,
        )

        if direction == "BUY":
            return up_probability

        if direction == "SELL":
            return 1.0 - up_probability

        return 0.0

    @staticmethod
    def _explicit_ai_approval(
        signal: Mapping[str, Any],
    ) -> Optional[bool]:
        for key in (
            "ai_approved",
            "approved",
            "ml_approved",
            "model_approved",
        ):
            if key not in signal:
                continue

            value = signal.get(key)

            if isinstance(value, bool):
                return value

            text = str(
                value
            ).strip().lower()

            if text in {
                "true",
                "1",
                "yes",
                "approved",
                "pass",
            }:
                return True

            if text in {
                "false",
                "0",
                "no",
                "rejected",
                "fail",
            }:
                return False

        return None

    def _qualify_candidates(
        self,
        candidates: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        qualified: List[Dict[str, Any]] = []

        for candidate in candidates:
            symbol = self._market(
                candidate
            )

            candidate_direction = self._direction(
                candidate
            )

            if (
                not symbol
                or candidate_direction
                not in {
                    "BUY",
                    "SELL",
                }
            ):
                continue

            try:
                signal = self._signal(
                    symbol
                )
            except Exception as exc:
                qualified.append(
                    {
                        "_rejected":
                            True,
                        "symbol":
                            symbol,
                        "reason":
                            f"SIGNAL_ERROR: {exc}",
                    }
                )
                continue

            signal_direction = self._direction(
                signal
            )

            if not self._candidate_signal_agrees(
                candidate_direction,
                signal_direction,
            ):
                continue

            approval = self._explicit_ai_approval(
                signal
            )

            if approval is False:
                continue

            ai_confidence = (
                self._ai_directional_confidence(
                    signal_direction,
                    signal,
                )
            )

            if (
                ai_confidence
                < self.config.ai_confidence_floor
            ):
                continue

            entry_price = self._float(
                signal.get("price"),
                0.0,
            )

            if entry_price <= 0:
                continue

            quant_confidence = self._probability(
                signal.get(
                    "confidence",
                    0.0,
                )
            )

            stake = self._float(
                signal.get(
                    "suggested_paper_stake"
                ),
                0.0,
            )

            if stake <= 0:
                stake = round(
                    self.config.starting_balance
                    * self.config.fallback_stake_pct,
                    2,
                )

            qualified.append(
                {
                    "symbol":
                        symbol,
                    "market":
                        symbol.replace(
                            "=X",
                            "",
                        ),
                    "direction":
                        signal_direction,
                    "fast_score":
                        self._fast_score(
                            candidate
                        ),
                    "quant_confidence":
                        quant_confidence,
                    "ai_confidence":
                        ai_confidence,
                    "combined_up_probability":
                        self._probability(
                            signal.get(
                                "combined_up_probability",
                                0.50,
                            ),
                            0.50,
                        ),
                    "rsi":
                        self._float(
                            signal.get(
                                "rsi"
                            ),
                            0.0,
                        ),
                    "entry_price":
                        entry_price,
                    "stake":
                        stake,
                    "risk_mode":
                        self.config.risk_mode,
                    "source":
                        "AI_LEARNING",
                    "entry_path":
                        "AI40",
                    "approval_explicit":
                        approval,
                    "signal_reason":
                        signal.get(
                            "reason"
                        ),
                }
            )

        return [
            item
            for item in qualified
            if not item.get(
                "_rejected"
            )
        ]

    # ========================================================
    # OPEN PAPER TRADE
    # ========================================================

    def _open_paper_trade(
        self,
        selected: Mapping[str, Any],
    ) -> Dict[str, Any]:
        return self._request_json(
            "POST",
            "/paper-trades",
            params={
                "symbol":
                    selected["symbol"],
                "direction":
                    selected["direction"],
                "confidence":
                    selected[
                        "quant_confidence"
                    ],
                "entry_price":
                    selected[
                        "entry_price"
                    ],
                "stake":
                    selected["stake"],

                # Existing FastAPI routes safely ignore undeclared
                # extra query parameters. If the current endpoint
                # has been extended to store them, these fields
                # become persistent labels automatically.
                "source":
                    "AI_LEARNING",
                "entry_path":
                    "AI40",
                "ai_confidence":
                    selected[
                        "ai_confidence"
                    ],
                "fast_score":
                    selected[
                        "fast_score"
                    ],
                "rsi":
                    selected["rsi"],
            },
            timeout_seconds=60,
        )

    # ========================================================
    # ONE AUTONOMOUS CYCLE
    # ========================================================

    def run_cycle(self) -> Dict[str, Any]:
        if not self.config.enabled:
            self._set_state(
                last_action="DISABLED",
                last_reason=
                    "AI_LEARNING_ENABLED is false",
            )

            return self.status()

        if not self._cycle_lock.acquire(
            blocking=False,
        ):
            self._set_state(
                last_action="SKIPPED_BUSY",
                last_reason=
                    "Another AI learning cycle is already running",
            )

            return self.status()

        started_at = time.time()

        self._set_state(
            last_cycle_started_at=
                started_at,
            last_action="RUNNING",
            last_reason=None,
            last_error=None,
        )

        try:
            # -----------------------------------------------
            # 1. Sync previously opened AI-learning outcomes.
            # -----------------------------------------------
            dashboard = self._dashboard()

            self._sync_learning_outcomes(
                dashboard
            )

            # -----------------------------------------------
            # 2. Safety: do not create another trade while
            #    an existing paper trade is open.
            # -----------------------------------------------
            open_count = self._open_trade_count(
                dashboard
            )

            if (
                open_count
                >= self.config.maximum_open_trades
            ):
                self._set_state(
                    last_action=
                        "WAIT_EXISTING_TRADE",
                    last_reason=
                        f"{open_count} PAPER trade(s) already open",
                    last_scan_count=0,
                    last_qualified_count=0,
                )

                return self.status()

            # -----------------------------------------------
            # 3. Fast scan existing V6.6 universe.
            # -----------------------------------------------
            scan = self._request_json(
                "GET",
                "/fast-scan",
                params={
                    "period":
                        self.config.scan_period,
                    "interval":
                        self.config.scan_interval,
                    "top_n":
                        self.config.scan_top_n,
                },
                timeout_seconds=180,
            )

            candidates = self._candidate_list(
                scan
            )

            self._set_state(
                last_scan_count=
                    len(candidates),
                last_action=
                    "EVALUATING",
            )

            if not candidates:
                self._set_state(
                    last_action=
                        "NO_CANDIDATES",
                    last_reason=
                        "Fast scan returned no candidates",
                    last_qualified_count=0,
                )

                return self.status()

            # -----------------------------------------------
            # 4. AI40 live confirmation.
            # -----------------------------------------------
            qualified = self._qualify_candidates(
                candidates
            )

            self._set_state(
                last_qualified_count=
                    len(qualified),
            )

            if not qualified:
                self._set_state(
                    last_action=
                        "NO_AI40_SETUP",
                    last_reason=
                        "No market passed live direction agreement + AI40",
                    last_selected=None,
                )

                return self.status()

            # Highest fast score first, then AI confidence,
            # then quantitative confidence.
            qualified.sort(
                key=lambda item: (
                    self._float(
                        item.get(
                            "fast_score"
                        )
                    ),
                    self._float(
                        item.get(
                            "ai_confidence"
                        )
                    ),
                    self._float(
                        item.get(
                            "quant_confidence"
                        )
                    ),
                ),
                reverse=True,
            )

            selected = qualified[0]

            # -----------------------------------------------
            # 5. Real PAPER order through existing backend.
            # -----------------------------------------------
            created = self._open_paper_trade(
                selected
            )

            learning_record = {
                **dict(selected),
                "opened_at":
                    time.time(),
                "status":
                    "OPEN",
                "paper_trade_response":
                    created,
            }

            created_trade = (
                created.get("trade")
                if isinstance(
                    created,
                    dict,
                )
                else None
            )

            if isinstance(
                created_trade,
                dict,
            ):
                learning_record.update(
                    {
                        "trade_id":
                            self._trade_identity(
                                created_trade
                            ),
                        "status":
                            str(
                                created_trade.get(
                                    "status",
                                    "OPEN",
                                )
                            ).upper(),
                    }
                )

            with self._state_lock:
                history = self._state.get(
                    "learning_trades",
                    [],
                )

                if not isinstance(
                    history,
                    list,
                ):
                    history = []

                history.append(
                    learning_record
                )

                # Keep the most recent 500 learning records.
                self._state[
                    "learning_trades"
                ] = history[-500:]

                self._state[
                    "learning_stats"
                ] = self._learning_stats_locked(
                    self._state[
                        "learning_trades"
                    ]
                )

                self._state[
                    "last_selected"
                ] = dict(
                    selected
                )

                self._state[
                    "last_action"
                ] = "PAPER_TRADE_OPENED"

                self._state[
                    "last_reason"
                ] = (
                    f"{selected['symbol']} "
                    f"{selected['direction']} "
                    f"opened by AI40"
                )

                self._state[
                    "last_error"
                ] = None

                self._save_state()

            return self.status()

        except Exception as exc:
            self._set_state(
                last_action="ERROR",
                last_error=
                    f"{type(exc).__name__}: {exc}",
                last_reason=
                    "AI learning cycle failed",
            )

            return self.status()

        finally:
            self._set_state(
                last_cycle_finished_at=
                    time.time(),
            )

            self._cycle_lock.release()

    # ========================================================
    # BACKGROUND LOOP
    # ========================================================

    def _loop(self) -> None:
        self._set_state(
            running=True,
        )

        while not self._stop_event.is_set():
            self.run_cycle()

            self._stop_event.wait(
                self.config.interval_seconds
            )

        self._set_state(
            running=False,
        )

    def start(self) -> Dict[str, Any]:
        if not self.config.enabled:
            return self.status()

        if (
            self._thread is not None
            and self._thread.is_alive()
        ):
            return self.status()

        self._stop_event.clear()

        self._thread = threading.Thread(
            target=self._loop,
            name="jasong-ai-learning",
            daemon=True,
        )

        self._thread.start()

        return self.status()

    def stop(self) -> Dict[str, Any]:
        self._stop_event.set()

        thread = self._thread

        if (
            thread is not None
            and thread.is_alive()
        ):
            thread.join(
                timeout=5,
            )

        self._set_state(
            running=False,
            last_action=
                "STOPPED",
        )

        return self.status()

    def status(self) -> Dict[str, Any]:
        with self._state_lock:
            snapshot = json.loads(
                json.dumps(
                    self._state,
                    default=str,
                )
            )

        snapshot["config"] = {
            **asdict(
                self.config
            ),
            # base_url is not secret, but expose it explicitly
            # so diagnostics show which deployment is being used.
        }

        snapshot["thread_alive"] = bool(
            self._thread
            and self._thread.is_alive()
        )

        return snapshot


ai_learning_engine = AILearningEngine()
