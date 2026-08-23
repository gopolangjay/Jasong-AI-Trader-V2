from __future__ import annotations

import copy
import json
import math
import os
import threading
import time
from pathlib import Path
from types import MethodType
from typing import Any, Dict, List, Optional, Tuple


class TradeExcursionTracker:
    """Broker-observed MFE/MAE + R exits + IG-native protection.

    Safety hierarchy for NEW risk-managed Jasong-owned IG DEMO positions:

    1. Planned economics never change:
         hard stop  = -1.0R by default
         take profit = +planned_target_r (1.5R by default)

    2. IG-native stop/limit orders are attached when possible.
       IG dealing rules are read from market_details(epic). If IG requires an
       attached order farther from the CURRENT market than the planned level,
       the native level is moved OUTWARD only. It is never moved inward.

    3. The server watchdog still closes at the ORIGINAL planned R threshold.
       Therefore an outward-normalised native stop is only catastrophe/back-up
       protection; it does not redefine the intended 1R loss.

    4. If IG rejects an attached-order amendment, an identical rejected native
       amendment is not retried continuously. The server watchdog remains the
       active protection.

    Legacy trades with no risk_policy_version retain the old TP-percent telemetry
    until they close. They are not retroactively rewritten.
    """

    VERSION = "6.3-clean-core-ig-risk-watchdog-v2"

    OWNED_PREFIXES = (
        "JSCAT_",
        "JSCMP_",
        "JASONG_",
        "JSBND_",
        "JSLRN_",
        "JSELT_",
    )

    def __init__(
        self,
        *,
        broker: Any,
        state_path: Optional[str] = None,
        poll_seconds: Optional[int] = None,
    ) -> None:
        self.broker = broker

        base_dir = "/var/data" if Path("/var/data").exists() else "/tmp"
        self.state_path = Path(
            state_path
            or os.getenv(
                "TRADE_EXCURSION_STATE_PATH",
                f"{base_dir}/jasong_trade_excursions.json",
            )
        )

        self.poll_seconds = max(
            10,
            min(
                120,
                int(
                    poll_seconds
                    or os.getenv("TRADE_EXCURSION_POLL_SECONDS", "60")
                ),
            ),
        )

        self.close_confirm_misses = max(
            1,
            min(
                5,
                int(
                    os.getenv(
                        "TRADE_EXCURSION_CLOSE_CONFIRM_MISSES",
                        "2",
                    )
                ),
            ),
        )

        self.take_profit_enabled = self._env_bool(
            "TRADE_TAKE_PROFIT_ENABLED",
            True,
        )
        self.hard_stop_enabled = self._env_bool(
            "TRADE_HARD_STOP_ENABLED",
            True,
        )

        self.take_profit_pct = self._env_float(
            "TRADE_TAKE_PROFIT_PCT",
            30.0,
            0.01,
            500.0,
        )
        self.default_target_r = self._env_float(
            "CATEGORY_TAKE_PROFIT_R",
            1.5,
            0.25,
            10.0,
        )
        self.hard_stop_r = self._env_float(
            "CATEGORY_HARD_STOP_R",
            1.0,
            0.25,
            5.0,
        )

        self.take_profit_retry_seconds = int(
            self._env_float(
                "TRADE_TAKE_PROFIT_RETRY_SECONDS",
                60.0,
                10.0,
                900.0,
            )
        )
        self.native_retry_seconds = int(
            self._env_float(
                "TRADE_TAKE_PROFIT_NATIVE_RETRY_SECONDS",
                300.0,
                30.0,
                3600.0,
            )
        )

        # Rejected ATTACHED_ORDER_LEVEL_ERROR amendments are suppressed by
        # default. The server R-watchdog remains active, so repeated identical
        # broker amendments add no safety and can waste IG API allowance.
        self.retry_rejected_native = self._env_bool(
            "TRADE_RETRY_REJECTED_NATIVE_ORDERS",
            False,
        )

        self._lock = threading.RLock()
        self._sync_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._broker_positions_original = None

        self._market_rule_cache: Dict[str, Dict[str, Any]] = {}
        self._market_rule_cache_at: Dict[str, float] = {}
        self._market_rule_cache_ttl = 900.0

        self._state: Dict[str, Any] = {
            "version": self.VERSION,
            "trades": {},
            "last_sync_at": None,
            "last_error": None,
            "sync_count": 0,
        }
        self._load()

    # ------------------------------------------------------------------
    # Configuration / primitive helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _env_bool(name: str, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return bool(default)
        return str(raw).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    @staticmethod
    def _env_float(
        name: str,
        default: float,
        lo: float,
        hi: float,
    ) -> float:
        try:
            value = float(os.getenv(name, str(default)))
        except Exception:
            value = default
        return max(lo, min(hi, value))

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            out = float(value)
            return out if math.isfinite(out) else None
        except Exception:
            return None

    @staticmethod
    def _round(
        value: Optional[float],
        digits: int = 10,
    ) -> Optional[float]:
        if value is None or not math.isfinite(value):
            return None
        return round(value, digits)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        try:
            if self.state_path.exists():
                raw = json.loads(
                    self.state_path.read_text(encoding="utf-8")
                )
                if isinstance(raw, dict):
                    self._state.update(raw)
        except Exception as exc:
            self._state["last_error"] = (
                f"load: {type(exc).__name__}: {exc}"
            )

        self._state["version"] = self.VERSION
        self._state.setdefault("trades", {})

    def _persist(self) -> None:
        try:
            self.state_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            tmp = self.state_path.with_suffix(
                self.state_path.suffix + ".tmp"
            )
            tmp.write_text(
                json.dumps(
                    self._state,
                    indent=2,
                    sort_keys=True,
                    default=str,
                ),
                encoding="utf-8",
            )
            tmp.replace(self.state_path)
        except Exception as exc:
            self._state["last_error"] = (
                f"persist: {type(exc).__name__}: {exc}"
            )

    # ------------------------------------------------------------------
    # Immutable risk-plan registration
    # ------------------------------------------------------------------

    def register_trade_plan(
        self,
        deal_id: str,
        plan: Dict[str, Any],
    ) -> None:
        clean_id = str(deal_id or "").strip()
        if not clean_id or not isinstance(plan, dict):
            return

        with self._lock:
            record = self._state.setdefault(
                "trades",
                {},
            ).get(clean_id)

            if not isinstance(record, dict):
                record = {
                    "deal_id": clean_id,
                    "status": "OPEN",
                    "miss_count": 0,
                    "first_observed_at": time.time(),
                }
                self._state["trades"][clean_id] = record

            # Entry risk is immutable. Never silently move the strategy's
            # planned 1R just because a broker attached order must be farther.
            if record.get("risk_policy_version"):
                return

            mapping = {
                "version": "risk_policy_version",
                "category": "category",
                "strategy_id": "strategy_id",
                "symbol": "symbol",
                "deal_reference": "deal_reference",
                "entry_price": "entry_price",
                "stop_pct": "planned_stop_pct",
                "target_r": "planned_target_r",
                "stop_distance": "planned_risk_price_distance",
                "target_distance": "planned_target_price_distance",
                "protective_stop_price": "protective_stop_price",
                "take_profit_target_price": "take_profit_target_price",
                "source": "risk_plan_source",
            }

            for source, target in mapping.items():
                value = plan.get(source)
                if value is not None:
                    record[target] = value

            record["take_profit_basis"] = (
                "PLANNED_RISK_R_MULTIPLE"
            )
            record["protective_stop_basis"] = (
                "PLANNED_ENTRY_RISK"
            )
            record["server_hard_stop_r"] = self.hard_stop_r
            record["server_hard_stop_enabled"] = (
                self.hard_stop_enabled
            )
            record["risk_plan_registered_at"] = time.time()
            self._persist()

    # ------------------------------------------------------------------
    # Broker-position observation
    # ------------------------------------------------------------------

    @staticmethod
    def _broker_rows(
        payload: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []

        for item in payload.get("positions", []) or []:
            if not isinstance(item, dict):
                continue

            position = item.get("position") or {}
            market = item.get("market") or {}
            if not isinstance(position, dict) or not isinstance(
                market,
                dict,
            ):
                continue

            deal_id = str(
                position.get("dealId") or ""
            ).strip()
            if not deal_id:
                continue

            direction = str(
                position.get("direction") or ""
            ).upper().strip()
            entry = TradeExcursionTracker._safe_float(
                position.get("level")
            )
            bid = TradeExcursionTracker._safe_float(
                market.get("bid")
            )
            offer = TradeExcursionTracker._safe_float(
                market.get("offer")
            )

            if direction == "BUY":
                observed = bid
                basis = "IG_DEMO_BID_EXIT_SIDE"
            elif direction == "SELL":
                observed = offer
                basis = "IG_DEMO_OFFER_EXIT_SIDE"
            else:
                observed = None
                basis = "UNAVAILABLE"

            if (
                observed is None
                and bid is not None
                and offer is not None
            ):
                observed = (bid + offer) / 2.0
                basis = "IG_DEMO_MID_FALLBACK"
            elif observed is None:
                observed = bid if bid is not None else offer
                if observed is not None:
                    basis = "IG_DEMO_SINGLE_SIDE_FALLBACK"

            rows.append(
                {
                    "deal_id": deal_id,
                    "deal_reference":
                        position.get("dealReference"),
                    "direction": direction,
                    "entry_price": entry,
                    "size": (
                        position.get("size")
                        if position.get("size") is not None
                        else position.get("dealSize")
                    ),
                    "epic": (
                        market.get("epic")
                        or position.get("epic")
                    ),
                    "market": (
                        market.get("instrumentName")
                        or market.get("marketName")
                        or market.get("epic")
                    ),
                    "bid": bid,
                    "offer": offer,
                    "observed_price": observed,
                    "price_basis": basis,
                    "market_status":
                        market.get("marketStatus"),
                    "opened_at_broker": (
                        position.get("createdDateUTC")
                        or position.get("createdDate")
                    ),
                }
            )

        return rows

    # ------------------------------------------------------------------
    # MFE / MAE / R
    # ------------------------------------------------------------------

    @staticmethod
    def _calculate(record: Dict[str, Any]) -> None:
        entry = TradeExcursionTracker._safe_float(
            record.get("entry_price")
        )
        high = TradeExcursionTracker._safe_float(
            record.get("highest_price_since_entry")
        )
        low = TradeExcursionTracker._safe_float(
            record.get("lowest_price_since_entry")
        )
        direction = str(
            record.get("direction") or ""
        ).upper().strip()

        if (
            entry is None
            or entry <= 0
            or high is None
            or low is None
        ):
            return

        if direction == "BUY":
            mfe = high - entry
            mae = low - entry
        elif direction == "SELL":
            mfe = entry - low
            mae = entry - high
        else:
            return

        record["mfe"] = TradeExcursionTracker._round(mfe)
        record["mae"] = TradeExcursionTracker._round(mae)
        record["mfe_pct"] = TradeExcursionTracker._round(
            mfe / entry * 100.0,
            6,
        )
        record["mae_pct"] = TradeExcursionTracker._round(
            mae / entry * 100.0,
            6,
        )
        record["mae_abs"] = TradeExcursionTracker._round(
            abs(mae)
        )
        record["mae_abs_pct"] = (
            TradeExcursionTracker._round(
                abs(mae) / entry * 100.0,
                6,
            )
        )
        record["highest_price_vs_entry_pct"] = (
            TradeExcursionTracker._round(
                (high - entry) / entry * 100.0,
                6,
            )
        )
        record["highest_price_as_pct_of_entry"] = (
            TradeExcursionTracker._round(
                high / entry * 100.0,
                6,
            )
        )
        record["lowest_price_vs_entry_pct"] = (
            TradeExcursionTracker._round(
                (low - entry) / entry * 100.0,
                6,
            )
        )

        risk_distance = TradeExcursionTracker._safe_float(
            record.get("planned_risk_price_distance")
        )
        if (
            risk_distance is not None
            and risk_distance > 0
        ):
            record["mfe_r"] = (
                TradeExcursionTracker._round(
                    mfe / risk_distance,
                    6,
                )
            )
            record["mae_r"] = (
                TradeExcursionTracker._round(
                    mae / risk_distance,
                    6,
                )
            )

    @staticmethod
    def _current_favourable_pct(
        record: Dict[str, Any],
    ) -> Optional[float]:
        entry = TradeExcursionTracker._safe_float(
            record.get("entry_price")
        )
        current = TradeExcursionTracker._safe_float(
            record.get("current_price")
        )
        direction = str(
            record.get("direction") or ""
        ).upper().strip()

        if (
            entry is None
            or entry <= 0
            or current is None
        ):
            return None

        if direction == "BUY":
            return (
                (current - entry)
                / entry
                * 100.0
            )
        if direction == "SELL":
            return (
                (entry - current)
                / entry
                * 100.0
            )
        return None

    def _update_r_telemetry(
        self,
        record: Dict[str, Any],
        now: float,
    ) -> None:
        entry = self._safe_float(
            record.get("entry_price")
        )
        current = self._safe_float(
            record.get("current_price")
        )
        risk_distance = self._safe_float(
            record.get("planned_risk_price_distance")
        )
        target_r = self._safe_float(
            record.get("planned_target_r")
        )
        direction = str(
            record.get("direction") or ""
        ).upper().strip()

        record["take_profit_enabled"] = (
            bool(self.take_profit_enabled)
        )
        record["server_hard_stop_enabled"] = (
            bool(self.hard_stop_enabled)
        )
        record["server_hard_stop_r"] = self.hard_stop_r

        favourable_pct = self._current_favourable_pct(
            record
        )
        record["current_favourable_pct"] = self._round(
            favourable_pct,
            6,
        )

        risk_managed = bool(
            record.get("risk_policy_version")
            and risk_distance is not None
            and risk_distance > 0
            and target_r is not None
            and target_r > 0
        )

        if not risk_managed:
            # Legacy compatibility only.
            target_pct = float(self.take_profit_pct)
            record["take_profit_target_pct"] = (
                self._round(target_pct, 6)
            )
            record["take_profit_basis"] = (
                "ENTRY_PRICE_FAVOURABLE_MOVE_PCT"
            )

            if (
                entry is not None
                and entry > 0
                and direction in {"BUY", "SELL"}
            ):
                if direction == "BUY":
                    target_price = entry * (
                        1.0 + target_pct / 100.0
                    )
                else:
                    target_price = entry * (
                        1.0 - target_pct / 100.0
                    )
                record["take_profit_target_price"] = (
                    self._round(target_price)
                )

            mfe_pct = self._safe_float(
                record.get("mfe_pct")
            )
            reached = bool(
                mfe_pct is not None
                and mfe_pct >= target_pct
            )
            record["take_profit_reached"] = reached

            if (
                reached
                and record.get(
                    "take_profit_reached_at"
                )
                is None
            ):
                record["take_profit_reached_at"] = now
                record[
                    "take_profit_first_reached_price"
                ] = self._round(current)
            return

        record["take_profit_basis"] = (
            "PLANNED_RISK_R_MULTIPLE"
        )
        record["take_profit_target_r"] = self._round(
            target_r,
            6,
        )

        if (
            entry is not None
            and entry > 0
            and direction in {"BUY", "SELL"}
        ):
            target_distance = (
                risk_distance * target_r
            )
            if direction == "BUY":
                target_price = entry + target_distance
            else:
                target_price = entry - target_distance

            if target_price > 0:
                # Planned target remains immutable.
                record["take_profit_target_price"] = (
                    self._round(target_price)
                )

            if current is not None:
                if direction == "BUY":
                    current_move = current - entry
                else:
                    current_move = entry - current

                current_r = (
                    current_move / risk_distance
                )
                record["current_favourable_r"] = (
                    self._round(current_r, 6)
                )

        mfe_r = self._safe_float(
            record.get("mfe_r")
        )
        reached = bool(
            mfe_r is not None
            and mfe_r >= target_r
        )
        record["take_profit_reached"] = reached

        if (
            reached
            and record.get("take_profit_reached_at")
            is None
        ):
            record["take_profit_reached_at"] = now
            record[
                "take_profit_first_reached_price"
            ] = self._round(current)

        mae_r = self._safe_float(
            record.get("mae_r")
        )
        stop_reached = bool(
            mae_r is not None
            and mae_r <= -self.hard_stop_r
        )
        record["hard_stop_reached"] = stop_reached

        if (
            stop_reached
            and record.get("hard_stop_reached_at")
            is None
        ):
            record["hard_stop_reached_at"] = now
            record["hard_stop_first_reached_price"] = (
                self._round(current)
            )
            record["hard_stop_first_reached_mae_r"] = (
                self._round(mae_r, 6)
            )

    # ------------------------------------------------------------------
    # Server risk watchdog
    # ------------------------------------------------------------------

    def _server_exit_reason(
        self,
        record: Dict[str, Any],
        now: float,
    ) -> Optional[str]:
        if not bool(record.get("jasong_owned")):
            return None
        if str(
            record.get("status") or ""
        ).upper() != "OPEN":
            return None

        risk_distance = self._safe_float(
            record.get("planned_risk_price_distance")
        )
        target_r = self._safe_float(
            record.get("planned_target_r")
        )
        risk_managed = bool(
            record.get("risk_policy_version")
            and risk_distance is not None
            and risk_distance > 0
            and target_r is not None
            and target_r > 0
        )

        if risk_managed:
            # IMPORTANT: use both current R and MAE/MFE ever-reached R.
            # With a 60-second REST poll a market can breach -1R and recover
            # before the next quote. If MAE shows the breach happened, close
            # immediately at the next observation instead of pretending it did
            # not happen.
            current_r = self._safe_float(
                record.get("current_favourable_r")
            )
            mae_r = self._safe_float(
                record.get("mae_r")
            )
            mfe_r = self._safe_float(
                record.get("mfe_r")
            )

            hard_stop_hit = bool(
                self.hard_stop_enabled
                and (
                    (
                        current_r is not None
                        and current_r <= -self.hard_stop_r
                    )
                    or (
                        mae_r is not None
                        and mae_r <= -self.hard_stop_r
                    )
                )
            )
            if hard_stop_hit:
                return (
                    f"HARD_STOP_{self.hard_stop_r:g}R"
                )

            tp_hit = bool(
                self.take_profit_enabled
                and (
                    (
                        current_r is not None
                        and current_r >= target_r
                    )
                    or (
                        mfe_r is not None
                        and mfe_r >= target_r
                    )
                )
            )
            if tp_hit:
                return (
                    f"TAKE_PROFIT_{target_r:g}R"
                )

            return None

        # Legacy server TP fallback.
        if not self.take_profit_enabled:
            return None

        favourable = self._safe_float(
            record.get("current_favourable_pct")
        )
        mfe_pct = self._safe_float(
            record.get("mfe_pct")
        )
        if (
            (
                favourable is not None
                and favourable >= self.take_profit_pct
            )
            or (
                mfe_pct is not None
                and mfe_pct >= self.take_profit_pct
            )
        ):
            return (
                f"TAKE_PROFIT_{self.take_profit_pct:g}_PCT"
            )

        return None

    def _server_close_needed(
        self,
        record: Dict[str, Any],
        now: float,
    ) -> Optional[str]:
        reason = self._server_exit_reason(
            record,
            now,
        )
        if not reason:
            return None

        state = str(
            record.get("server_exit_state") or ""
        ).upper()
        last_attempt = (
            self._safe_float(
                record.get("server_exit_last_attempt_at")
            )
            or 0.0
        )

        if state in {
            "CLOSED",
            "CLOSE_VERIFIED",
        }:
            return None

        if (
            state
            in {
                "TRIGGERED",
                "CLOSE_SENT",
                "CLOSE_PENDING",
            }
            and (
                now - last_attempt
                < self.take_profit_retry_seconds
            )
        ):
            return None

        if (
            state
            in {
                "ERROR",
                "DEFERRED_MARKET_CLOSED",
            }
            and (
                now - last_attempt
                < self.take_profit_retry_seconds
            )
        ):
            return None

        record["server_exit_state"] = "TRIGGERED"
        record["server_exit_reason"] = reason
        record["server_exit_triggered_at"] = (
            record.get("server_exit_triggered_at")
            or now
        )
        record["server_exit_trigger_price"] = (
            self._round(
                self._safe_float(
                    record.get("current_price")
                )
            )
        )
        record["server_exit_trigger_r"] = (
            record.get("current_favourable_r")
        )

        if reason.startswith("HARD_STOP_"):
            record["hard_stop_close_state"] = (
                "TRIGGERED"
            )
        elif reason.startswith("TAKE_PROFIT_"):
            record["take_profit_close_state"] = (
                "TRIGGERED"
            )

        return reason

    def _execute_server_close(
        self,
        deal_id: str,
        reason: str,
    ) -> None:
        now = time.time()

        with self._lock:
            record = self._state.setdefault(
                "trades",
                {},
            ).get(str(deal_id))
            if not isinstance(record, dict):
                return

            record["server_exit_last_attempt_at"] = now
            record["server_exit_attempts"] = int(
                record.get("server_exit_attempts")
                or 0
            ) + 1
            record["server_exit_state"] = (
                "CLOSE_PENDING"
            )
            if reason.startswith("HARD_STOP_"):
                record["hard_stop_close_state"] = (
                    "CLOSE_PENDING"
                )
                record["hard_stop_close_attempts"] = (
                    int(
                        record.get(
                            "hard_stop_close_attempts"
                        )
                        or 0
                    )
                    + 1
                )
            else:
                record["take_profit_close_state"] = (
                    "CLOSE_PENDING"
                )
                record["take_profit_close_attempts"] = (
                    int(
                        record.get(
                            "take_profit_close_attempts"
                        )
                        or 0
                    )
                    + 1
                )
            self._persist()

        try:
            result = (
                self.broker.close_position(
                    str(deal_id)
                )
                or {}
            )
            status = str(
                result.get("status")
                or result.get("dealStatus")
                or ""
            ).upper().strip()
            verified = bool(
                result.get("closeVerified")
            )
            success = (
                verified
                or status
                in {
                    "ACCEPTED",
                    "ALREADY_CLOSED_OR_NOT_FOUND",
                    "CLOSED_VERIFIED",
                }
            )
            deferred = (
                status
                == "CLOSE_DEFERRED_MARKET_CLOSED"
            )

            compact = {
                "status": status or None,
                "dealStatus":
                    result.get("dealStatus"),
                "reason": result.get("reason"),
                "closeVerified": verified,
                "level": result.get("level"),
                "profit": result.get("profit"),
                "profitLoss":
                    result.get("profitLoss"),
                "pnl": result.get("pnl"),
                "profitCurrency":
                    result.get("profitCurrency"),
            }

            with self._lock:
                record = self._state.setdefault(
                    "trades",
                    {},
                ).get(str(deal_id))
                if not isinstance(record, dict):
                    return

                record["server_exit_result"] = compact
                record["close_reason"] = reason

                # Preserve the old field consumed by downstream merge/PRIME.
                record["take_profit_close_result"] = (
                    compact
                )

                if success:
                    record["server_exit_state"] = (
                        "CLOSE_VERIFIED"
                        if verified
                        else "CLOSE_SENT"
                    )
                    record["server_exit_closed_at"] = (
                        time.time()
                    )

                    if reason.startswith(
                        "HARD_STOP_"
                    ):
                        record["hard_stop_close_state"] = (
                            "CLOSE_VERIFIED"
                            if verified
                            else "CLOSE_SENT"
                        )
                        record[
                            "hard_stop_closed_at"
                        ] = time.time()
                    else:
                        record[
                            "take_profit_close_state"
                        ] = (
                            "CLOSE_VERIFIED"
                            if verified
                            else "CLOSE_SENT"
                        )
                        record[
                            "take_profit_closed_at"
                        ] = time.time()

                elif deferred:
                    record["server_exit_state"] = (
                        "DEFERRED_MARKET_CLOSED"
                    )
                    if reason.startswith(
                        "HARD_STOP_"
                    ):
                        record[
                            "hard_stop_close_state"
                        ] = "DEFERRED_MARKET_CLOSED"
                    else:
                        record[
                            "take_profit_close_state"
                        ] = "DEFERRED_MARKET_CLOSED"
                else:
                    record["server_exit_state"] = (
                        status or "CLOSE_PENDING"
                    )

                self._persist()

        except Exception as exc:
            with self._lock:
                record = self._state.setdefault(
                    "trades",
                    {},
                ).get(str(deal_id))
                if isinstance(record, dict):
                    error = (
                        f"{type(exc).__name__}: {exc}"
                    )
                    record["server_exit_state"] = (
                        "ERROR"
                    )
                    record["server_exit_error"] = error
                    record[
                        "server_exit_last_attempt_at"
                    ] = time.time()

                    if reason.startswith(
                        "HARD_STOP_"
                    ):
                        record[
                            "hard_stop_close_state"
                        ] = "ERROR"
                        record[
                            "hard_stop_close_error"
                        ] = error
                    else:
                        record[
                            "take_profit_close_state"
                        ] = "ERROR"
                        record[
                            "take_profit_close_error"
                        ] = error

                    self._persist()

    # ------------------------------------------------------------------
    # IG dealing rules / native level normalisation
    # ------------------------------------------------------------------

    @staticmethod
    def _distance_from_rule(
        rule: Any,
        reference_price: float,
    ) -> Optional[float]:
        if not isinstance(rule, dict):
            return None

        value = TradeExcursionTracker._safe_float(
            rule.get("value")
        )
        if value is None or value <= 0:
            return None

        unit = str(
            rule.get("unit") or "POINTS"
        ).upper().strip()

        if unit == "PERCENTAGE":
            return (
                reference_price
                * value
                / 100.0
            )

        # IG POINTS are expressed in the market's displayed price units.
        return value

    def _market_rules(
        self,
        epic: str,
    ) -> Dict[str, Any]:
        clean = str(epic or "").strip()
        if not clean:
            return {}

        now = time.time()
        cached = self._market_rule_cache.get(clean)
        cached_at = self._market_rule_cache_at.get(
            clean,
            0.0,
        )
        if (
            cached is not None
            and now - cached_at
            < self._market_rule_cache_ttl
        ):
            return copy.deepcopy(cached)

        details_fn = getattr(
            self.broker,
            "market_details",
            None,
        )
        if not callable(details_fn):
            return {}

        try:
            details = (
                details_fn(clean) or {}
            )
            rules = (
                details.get("dealingRules")
                if isinstance(details, dict)
                else {}
            ) or {}
            snapshot = (
                details.get("snapshot")
                if isinstance(details, dict)
                else {}
            ) or {}
            instrument = (
                details.get("instrument")
                if isinstance(details, dict)
                else {}
            ) or {}

            result = {
                "dealingRules": rules,
                "snapshot": snapshot,
                "instrument": instrument,
                "fetched_at": now,
            }
            self._market_rule_cache[clean] = result
            self._market_rule_cache_at[clean] = now
            return copy.deepcopy(result)

        except Exception as exc:
            return {
                "error": (
                    f"{type(exc).__name__}: {exc}"
                ),
                "fetched_at": now,
            }

    def _normalise_native_levels(
        self,
        record: Dict[str, Any],
    ) -> Dict[str, Any]:
        direction = str(
            record.get("direction") or ""
        ).upper().strip()
        current = self._safe_float(
            record.get("current_price")
        )
        target = self._safe_float(
            record.get("take_profit_target_price")
        )
        planned_stop = self._safe_float(
            record.get("protective_stop_price")
        )
        epic = str(
            record.get("epic") or ""
        ).strip()

        result = {
            "limit_level": target,
            "stop_level": planned_stop,
            "normalised": False,
            "normalisation_reason": None,
            "minimum_distance": None,
            "dealing_rules_available": False,
        }

        if (
            direction not in {"BUY", "SELL"}
            or current is None
            or current <= 0
            or not epic
        ):
            return result

        details = self._market_rules(epic)
        rules = details.get("dealingRules") or {}
        if not isinstance(rules, dict):
            return result

        result["dealing_rules_available"] = bool(
            rules
        )

        minimum = self._distance_from_rule(
            rules.get(
                "minNormalStopOrLimitDistance"
            ),
            current,
        )
        if minimum is None:
            return result

        # Small broker-side safety margin so floating point / a moving quote does
        # not leave an order exactly on the minimum boundary by the time IG
        # receives the amendment.
        safety_margin = max(
            minimum * 0.05,
            abs(current) * 0.000001,
        )
        required = minimum + safety_margin

        result["minimum_distance"] = (
            self._round(minimum)
        )
        result["minimum_distance_with_margin"] = (
            self._round(required)
        )

        limit_level = target
        stop_level = planned_stop
        reasons: List[str] = []

        if direction == "BUY":
            if (
                target is not None
                and target > 0
            ):
                broker_min_limit = (
                    current + required
                )
                if target < broker_min_limit:
                    limit_level = broker_min_limit
                    reasons.append(
                        "BUY_LIMIT_MOVED_OUTWARD_TO_IG_MIN"
                    )

            if (
                planned_stop is not None
                and planned_stop > 0
            ):
                broker_max_stop = (
                    current - required
                )
                if planned_stop > broker_max_stop:
                    stop_level = broker_max_stop
                    reasons.append(
                        "BUY_STOP_MOVED_OUTWARD_TO_IG_MIN"
                    )

        else:
            if (
                target is not None
                and target > 0
            ):
                broker_max_limit = (
                    current - required
                )
                if target > broker_max_limit:
                    limit_level = broker_max_limit
                    reasons.append(
                        "SELL_LIMIT_MOVED_OUTWARD_TO_IG_MIN"
                    )

            if (
                planned_stop is not None
                and planned_stop > 0
            ):
                broker_min_stop = (
                    current + required
                )
                if planned_stop < broker_min_stop:
                    stop_level = broker_min_stop
                    reasons.append(
                        "SELL_STOP_MOVED_OUTWARD_TO_IG_MIN"
                    )

        if (
            limit_level is not None
            and limit_level <= 0
        ):
            limit_level = None
        if (
            stop_level is not None
            and stop_level <= 0
        ):
            stop_level = None

        result["limit_level"] = (
            self._round(limit_level)
        )
        result["stop_level"] = (
            self._round(stop_level)
        )
        result["normalised"] = bool(reasons)
        result["normalisation_reason"] = (
            reasons or None
        )
        result["dealing_rules"] = {
            "minNormalStopOrLimitDistance":
                rules.get(
                    "minNormalStopOrLimitDistance"
                ),
            "minControlledRiskStopDistance":
                rules.get(
                    "minControlledRiskStopDistance"
                ),
            "maxStopOrLimitDistance":
                rules.get(
                    "maxStopOrLimitDistance"
                ),
            "marketOrderPreference":
                rules.get(
                    "marketOrderPreference"
                ),
            "trailingStopsPreference":
                rules.get(
                    "trailingStopsPreference"
                ),
        }
        return result

    @staticmethod
    def _native_signature(
        limit_level: Optional[float],
        stop_level: Optional[float],
    ) -> str:
        limit_text = (
            "NONE"
            if limit_level is None
            else f"{limit_level:.10f}"
        )
        stop_text = (
            "NONE"
            if stop_level is None
            else f"{stop_level:.10f}"
        )
        return f"L={limit_text}|S={stop_text}"

    def _native_order_needed(
        self,
        record: Dict[str, Any],
        now: float,
    ) -> bool:
        if not bool(record.get("jasong_owned")):
            return False
        if str(
            record.get("status") or ""
        ).upper() != "OPEN":
            return False

        risk_managed = bool(
            record.get("risk_policy_version")
        )
        target = self._safe_float(
            record.get("take_profit_target_price")
        )
        stop = (
            self._safe_float(
                record.get("protective_stop_price")
            )
            if risk_managed
            else None
        )

        if target is None and stop is None:
            return False

        if bool(
            record.get("native_order_suppressed")
        ) and not self.retry_rejected_native:
            return False

        state = str(
            record.get("native_take_profit_state") or ""
        ).upper()

        if state in {"CONFIRMED", "ATTACHED"}:
            # If both requested components are present, no retry.
            limit_ok = (
                target is None
                or record.get(
                    "native_take_profit_level"
                )
                is not None
            )
            stop_ok = (
                stop is None
                or record.get(
                    "native_protective_stop_level"
                )
                is not None
            )
            if limit_ok and stop_ok:
                return False

        last_attempt = (
            self._safe_float(
                record.get(
                    "native_take_profit_last_attempt_at"
                )
            )
            or 0.0
        )
        return (
            now - last_attempt
            >= self.native_retry_seconds
        )

    def _attach_native_orders(
        self,
        deal_id: str,
    ) -> None:
        now = time.time()

        with self._lock:
            record = self._state.setdefault(
                "trades",
                {},
            ).get(str(deal_id))
            if not isinstance(record, dict):
                return

            normalised = self._normalise_native_levels(
                record
            )
            target = self._safe_float(
                normalised.get("limit_level")
            )
            stop = self._safe_float(
                normalised.get("stop_level")
            )
            risk_managed = bool(
                record.get("risk_policy_version")
            )

            if target is None and stop is None:
                return

            signature = self._native_signature(
                target,
                stop,
            )

            # Never spam an identical rejected amendment.
            if (
                not self.retry_rejected_native
                and record.get(
                    "native_last_rejected_signature"
                )
                == signature
            ):
                record["native_order_suppressed"] = True
                record[
                    "native_order_suppression_reason"
                ] = (
                    "IDENTICAL_REJECTED_ATTACHED_ORDER"
                )
                self._persist()
                return

            record["native_level_policy"] = (
                "IG_DEALING_RULE_NORMALISED_OUTWARD_ONLY"
            )
            record["native_level_normalisation"] = (
                normalised
            )
            record["native_order_signature"] = signature
            record[
                "native_take_profit_last_attempt_at"
            ] = now
            record["native_take_profit_attempts"] = int(
                record.get("native_take_profit_attempts")
                or 0
            ) + 1
            record["native_take_profit_state"] = (
                "ATTACHING"
            )

            if risk_managed and stop is not None:
                record[
                    "native_protective_stop_state"
                ] = "ATTACHING"

            self._persist()

        try:
            request_fn = getattr(
                self.broker,
                "_request",
                None,
            )
            if not callable(request_fn):
                raise RuntimeError(
                    "IG broker update-position request method unavailable"
                )

            payload: Dict[str, Any] = {}
            if target is not None:
                payload["limitLevel"] = float(target)
            if (
                risk_managed
                and stop is not None
            ):
                payload["stopLevel"] = float(stop)

            acknowledgement = (
                request_fn(
                    "PUT",
                    f"/positions/otc/{deal_id}",
                    version=2,
                    payload=payload,
                )
                or {}
            )

            ref = str(
                acknowledgement.get("dealReference")
                or ""
            ).strip()

            confirmation: Dict[str, Any] = {}
            confirm_fn = getattr(
                self.broker,
                "confirm",
                None,
            )
            if ref and callable(confirm_fn):
                confirmation = (
                    confirm_fn(ref) or {}
                )

            deal_status = str(
                confirmation.get("dealStatus")
                or ""
            ).upper().strip()
            rejected = (
                deal_status == "REJECTED"
            )

            with self._lock:
                record = self._state.setdefault(
                    "trades",
                    {},
                ).get(str(deal_id))
                if not isinstance(record, dict):
                    return

                record[
                    "native_take_profit_deal_reference"
                ] = ref or None
                record[
                    "native_take_profit_confirmation"
                ] = {
                    "dealStatus":
                        confirmation.get("dealStatus"),
                    "reason":
                        confirmation.get("reason"),
                    "limitLevel":
                        confirmation.get("limitLevel"),
                    "stopLevel":
                        confirmation.get("stopLevel"),
                    "status":
                        confirmation.get("status"),
                }

                if target is not None:
                    confirmed_limit = self._safe_float(
                        confirmation.get("limitLevel")
                    )
                    record["native_take_profit_level"] = (
                        self._round(
                            confirmed_limit
                            if confirmed_limit is not None
                            else target
                        )
                    )

                if (
                    risk_managed
                    and stop is not None
                ):
                    confirmed_stop = self._safe_float(
                        confirmation.get("stopLevel")
                    )
                    record[
                        "native_protective_stop_level"
                    ] = self._round(
                        confirmed_stop
                        if confirmed_stop is not None
                        else stop
                    )

                if rejected:
                    reason = str(
                        confirmation.get("reason")
                        or confirmation
                    )
                    record[
                        "native_take_profit_state"
                    ] = "REJECTED"
                    record[
                        "native_take_profit_error"
                    ] = reason
                    record[
                        "native_last_rejected_signature"
                    ] = signature

                    if risk_managed:
                        record[
                            "native_protective_stop_state"
                        ] = "REJECTED"
                        record[
                            "native_protective_stop_error"
                        ] = reason

                    if not self.retry_rejected_native:
                        record[
                            "native_order_suppressed"
                        ] = True
                        record[
                            "native_order_suppression_reason"
                        ] = (
                            "BROKER_REJECTED_ATTACHED_ORDER;"
                            "SERVER_R_WATCHDOG_ACTIVE"
                        )
                else:
                    state = (
                        "CONFIRMED"
                        if confirmation
                        else "ATTACHED"
                    )
                    record[
                        "native_take_profit_state"
                    ] = state
                    record[
                        "native_take_profit_attached_at"
                    ] = time.time()

                    if (
                        risk_managed
                        and stop is not None
                    ):
                        record[
                            "native_protective_stop_state"
                        ] = state
                        record[
                            "native_protective_stop_attached_at"
                        ] = time.time()

                    record["native_order_suppressed"] = (
                        False
                    )
                    record[
                        "native_order_suppression_reason"
                    ] = None

                self._persist()

        except Exception as exc:
            with self._lock:
                record = self._state.setdefault(
                    "trades",
                    {},
                ).get(str(deal_id))
                if isinstance(record, dict):
                    error = (
                        f"{type(exc).__name__}: {exc}"
                    )
                    record[
                        "native_take_profit_state"
                    ] = "ERROR"
                    record[
                        "native_take_profit_error"
                    ] = error
                    record[
                        "native_take_profit_last_attempt_at"
                    ] = time.time()

                    if record.get(
                        "risk_policy_version"
                    ):
                        record[
                            "native_protective_stop_state"
                        ] = "ERROR"
                        record[
                            "native_protective_stop_error"
                        ] = error

                    self._persist()

    # ------------------------------------------------------------------
    # Main observe loop
    # ------------------------------------------------------------------

    def observe_payload(
        self,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not self._sync_lock.acquire(
            blocking=False
        ):
            return self.status()

        try:
            now = time.time()
            broker_rows = self._broker_rows(
                payload or {}
            )
            seen = {
                row["deal_id"]
                for row in broker_rows
            }

            server_close_candidates: List[
                Tuple[str, str]
            ] = []
            native_candidates: List[str] = []

            with self._lock:
                trades = self._state.setdefault(
                    "trades",
                    {},
                )

                for row in broker_rows:
                    deal_id = row["deal_id"]
                    record = trades.get(deal_id)

                    if not isinstance(record, dict):
                        record = {
                            "deal_id": deal_id,
                            "first_observed_at": now,
                            "highest_price_since_entry":
                                row.get("entry_price"),
                            "lowest_price_since_entry":
                                row.get("entry_price"),
                            "status": "OPEN",
                            "miss_count": 0,
                        }
                        trades[deal_id] = record

                    record.update(
                        {
                            "deal_reference": (
                                row.get("deal_reference")
                                or record.get(
                                    "deal_reference"
                                )
                            ),
                            "direction":
                                row.get("direction"),
                            "entry_price": (
                                row.get("entry_price")
                                if row.get("entry_price")
                                is not None
                                else record.get(
                                    "entry_price"
                                )
                            ),
                            "size": row.get("size"),
                            "epic": row.get("epic"),
                            "market": row.get("market"),
                            "current_bid":
                                row.get("bid"),
                            "current_offer":
                                row.get("offer"),
                            "current_price":
                                row.get(
                                    "observed_price"
                                ),
                            "price_basis":
                                row.get("price_basis"),
                            "market_status":
                                row.get(
                                    "market_status"
                                ),
                            "opened_at_broker":
                                row.get(
                                    "opened_at_broker"
                                ),
                            "last_observed_at": now,
                            "status": "OPEN",
                            "miss_count": 0,
                            "jasong_owned": str(
                                row.get(
                                    "deal_reference"
                                )
                                or record.get(
                                    "deal_reference"
                                )
                                or ""
                            ).upper().startswith(
                                self.OWNED_PREFIXES
                            ),
                        }
                    )

                    current = self._safe_float(
                        row.get("observed_price")
                    )
                    entry = self._safe_float(
                        record.get("entry_price")
                    )

                    if current is not None:
                        high = self._safe_float(
                            record.get(
                                "highest_price_since_entry"
                            )
                        )
                        low = self._safe_float(
                            record.get(
                                "lowest_price_since_entry"
                            )
                        )

                        if high is None:
                            high = (
                                entry
                                if entry is not None
                                else current
                            )
                        if low is None:
                            low = (
                                entry
                                if entry is not None
                                else current
                            )

                        record[
                            "highest_price_since_entry"
                        ] = max(high, current)
                        record[
                            "lowest_price_since_entry"
                        ] = min(low, current)

                    self._calculate(record)
                    self._update_r_telemetry(
                        record,
                        now,
                    )

                    server_reason = (
                        self._server_close_needed(
                            record,
                            now,
                        )
                    )
                    if server_reason:
                        server_close_candidates.append(
                            (
                                deal_id,
                                server_reason,
                            )
                        )
                    elif self._native_order_needed(
                        record,
                        now,
                    ):
                        native_candidates.append(
                            deal_id
                        )

                # Reconcile disappeared positions.
                for deal_id, record in list(
                    trades.items()
                ):
                    if (
                        not isinstance(record, dict)
                        or str(
                            record.get("status") or ""
                        ).upper()
                        != "OPEN"
                        or deal_id in seen
                    ):
                        continue

                    misses = int(
                        record.get("miss_count") or 0
                    ) + 1
                    record["miss_count"] = misses
                    record["last_missing_at"] = now

                    if (
                        misses
                        >= self.close_confirm_misses
                    ):
                        record["status"] = (
                            "CLOSED_OBSERVED"
                        )
                        record[
                            "closed_observed_at"
                        ] = now
                        self._calculate(record)

                # Bound persistent file size.
                if len(trades) > 2500:
                    ordered = sorted(
                        trades.values(),
                        key=lambda r: float(
                            (r or {}).get(
                                "last_observed_at"
                            )
                            or (r or {}).get(
                                "closed_observed_at"
                            )
                            or 0.0
                        ),
                        reverse=True,
                    )[:2000]
                    self._state["trades"] = {
                        str(row.get("deal_id")): row
                        for row in ordered
                        if (
                            isinstance(row, dict)
                            and row.get("deal_id")
                        )
                    }

                self._state["last_sync_at"] = now
                self._state["last_error"] = None
                self._state["sync_count"] = int(
                    self._state.get("sync_count")
                    or 0
                ) + 1
                self._persist()

            # Risk closes have priority over native-order maintenance.
            for deal_id, reason in (
                server_close_candidates
            ):
                self._execute_server_close(
                    deal_id,
                    reason,
                )

            closed_ids = {
                deal_id
                for deal_id, _ in server_close_candidates
            }

            for deal_id in native_candidates:
                if deal_id in closed_ids:
                    continue
                self._attach_native_orders(
                    deal_id
                )

            return self.status()

        except Exception as exc:
            with self._lock:
                self._state["last_error"] = (
                    f"{type(exc).__name__}: {exc}"
                )
                self._state[
                    "last_sync_attempt_at"
                ] = time.time()
                self._persist()
            return self.status()

        finally:
            self._sync_lock.release()

    # ------------------------------------------------------------------
    # Broker observer / periodic sync
    # ------------------------------------------------------------------

    def install_broker_observer(self) -> None:
        if getattr(
            self.broker,
            "_trade_excursion_observer_installed",
            False,
        ):
            return

        original = getattr(
            self.broker,
            "positions",
            None,
        )
        if not callable(original):
            return

        self._broker_positions_original = original

        def observed_positions(
            broker_self: Any,
            *args: Any,
            **kwargs: Any,
        ):
            payload = original(
                *args,
                **kwargs,
            )
            if isinstance(payload, dict):
                try:
                    self.observe_payload(payload)
                except Exception:
                    pass
            return payload

        self.broker.positions = MethodType(
            observed_positions,
            self.broker,
        )
        self.broker._trade_excursion_observer_installed = (
            True
        )
        self.broker._trade_excursion_tracker = self

    def sync_once(self) -> Dict[str, Any]:
        try:
            getter = self._broker_positions_original
            if callable(getter):
                payload = getter() or {}
            else:
                payload = (
                    self.broker.positions() or {}
                )
                if getattr(
                    self.broker,
                    "_trade_excursion_observer_installed",
                    False,
                ):
                    return self.status()

            return self.observe_payload(
                payload
            )

        except Exception as exc:
            with self._lock:
                self._state["last_error"] = (
                    f"{type(exc).__name__}: {exc}"
                )
                self._state[
                    "last_sync_attempt_at"
                ] = time.time()
                self._persist()
            return self.status()

    # ------------------------------------------------------------------
    # Public rows / merge
    # ------------------------------------------------------------------

    def rows(
        self,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            rows = [
                copy.deepcopy(row)
                for row in self._state.setdefault(
                    "trades",
                    {},
                ).values()
                if isinstance(row, dict)
            ]

        rows.sort(
            key=lambda row: (
                str(
                    row.get("status") or ""
                ).upper()
                == "OPEN",
                float(
                    row.get("last_observed_at")
                    or row.get(
                        "closed_observed_at"
                    )
                    or 0.0
                ),
            ),
            reverse=True,
        )
        return rows[
            :max(
                1,
                min(int(limit), 2000),
            )
        ]

    def by_deal_id(
        self,
    ) -> Dict[str, Dict[str, Any]]:
        return {
            str(row.get("deal_id")): row
            for row in self.rows(limit=2000)
            if row.get("deal_id")
        }

    def _lookup(
        self,
        deal_id: str,
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._state.setdefault(
                "trades",
                {},
            ).get(str(deal_id))
            return (
                copy.deepcopy(row)
                if isinstance(row, dict)
                else None
            )

    def merge(
        self,
        row: Dict[str, Any],
    ) -> Dict[str, Any]:
        out = dict(row)
        key = str(
            out.get("deal_id")
            or out.get("ig_deal_id")
            or out.get("trade_id")
            or out.get("id")
            or ""
        ).strip()

        if not key:
            return out

        excursion = self._lookup(key)
        if not excursion:
            return out

        fields = (
            "highest_price_since_entry",
            "lowest_price_since_entry",
            "current_price",
            "price_basis",
            "mfe",
            "mae",
            "mfe_pct",
            "mae_pct",
            "mae_abs",
            "mae_abs_pct",
            "mfe_r",
            "mae_r",
            "highest_price_vs_entry_pct",
            "highest_price_as_pct_of_entry",
            "lowest_price_vs_entry_pct",
            "current_favourable_pct",
            "current_favourable_r",
            "risk_policy_version",
            "planned_stop_pct",
            "planned_risk_price_distance",
            "planned_target_r",
            "protective_stop_price",
            "risk_plan_source",
            "server_hard_stop_enabled",
            "server_hard_stop_r",
            "hard_stop_reached",
            "hard_stop_reached_at",
            "hard_stop_close_state",
            "hard_stop_closed_at",
            "server_exit_state",
            "server_exit_reason",
            "server_exit_trigger_r",
            "take_profit_enabled",
            "take_profit_target_pct",
            "take_profit_target_r",
            "take_profit_basis",
            "take_profit_target_price",
            "take_profit_reached",
            "take_profit_reached_at",
            "take_profit_first_reached_price",
            "take_profit_close_state",
            "take_profit_closed_at",
            "native_take_profit_state",
            "native_take_profit_level",
            "native_take_profit_attached_at",
            "native_take_profit_attempts",
            "native_take_profit_error",
            "native_protective_stop_level",
            "native_protective_stop_state",
            "native_protective_stop_error",
            "native_order_suppressed",
            "native_order_suppression_reason",
            "native_level_normalisation",
            "close_reason",
            "last_observed_at",
        )

        for field in fields:
            if excursion.get(field) is not None:
                out[field] = excursion.get(field)

        if (
            not isinstance(
                out.get("close_result"),
                dict,
            )
            and isinstance(
                excursion.get("server_exit_result"),
                dict,
            )
        ):
            out["close_result"] = copy.deepcopy(
                excursion["server_exit_result"]
            )
        elif (
            not isinstance(
                out.get("close_result"),
                dict,
            )
            and isinstance(
                excursion.get(
                    "take_profit_close_result"
                ),
                dict,
            )
        ):
            out["close_result"] = copy.deepcopy(
                excursion[
                    "take_profit_close_result"
                ]
            )

        out["excursion_tracking"] = {
            "source":
                "IG_DEMO_PERIODIC_REST",
            "poll_seconds":
                self.poll_seconds,
            "price_basis":
                excursion.get("price_basis"),
            "last_observed_at":
                excursion.get(
                    "last_observed_at"
                ),
            "risk_policy_version":
                excursion.get(
                    "risk_policy_version"
                ),
            "server_hard_stop_r":
                excursion.get(
                    "server_hard_stop_r"
                ),
            "server_exit_state":
                excursion.get(
                    "server_exit_state"
                ),
            "server_exit_reason":
                excursion.get(
                    "server_exit_reason"
                ),
            "take_profit_basis":
                excursion.get(
                    "take_profit_basis"
                ),
            "native_take_profit_state":
                excursion.get(
                    "native_take_profit_state"
                ),
            "native_take_profit_level":
                excursion.get(
                    "native_take_profit_level"
                ),
            "native_protective_stop_level":
                excursion.get(
                    "native_protective_stop_level"
                ),
            "native_order_suppressed":
                excursion.get(
                    "native_order_suppressed"
                ),
        }
        return out

    def _merge_compound_payload(
        self,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        out = copy.deepcopy(payload)

        rows = out.get(
            "compound_broker_positions"
        )
        if isinstance(rows, list):
            out[
                "compound_broker_positions"
            ] = [
                self.merge(row)
                if isinstance(row, dict)
                else row
                for row in rows
            ]

        current = out.get("current_cycle")
        if (
            isinstance(current, dict)
            and isinstance(
                current.get("positions"),
                list,
            )
        ):
            current["positions"] = [
                self.merge(row)
                if isinstance(row, dict)
                else row
                for row in (
                    current.get("positions")
                    or []
                )
            ]

        cycles = out.get("recent_cycles")
        if isinstance(cycles, list):
            for cycle in cycles:
                if (
                    isinstance(cycle, dict)
                    and isinstance(
                        cycle.get("positions"),
                        list,
                    )
                ):
                    cycle["positions"] = [
                        self.merge(row)
                        if isinstance(row, dict)
                        else row
                        for row in (
                            cycle.get("positions")
                            or []
                        )
                    ]

        return out

    def install_runtime_merges(
        self,
        *,
        portfolio: Optional[Any] = None,
        compound_engine: Optional[Any] = None,
        legacy_evidence: Optional[Any] = None,
    ) -> None:
        if portfolio is not None:
            portfolio._trade_excursion_tracker = (
                self
            )

        if (
            portfolio is not None
            and not getattr(
                portfolio,
                "_excursion_positions_patch",
                False,
            )
        ):
            original_positions = getattr(
                portfolio,
                "positions",
                None,
            )
            if callable(original_positions):

                def positions(
                    component_self: Any,
                    *args: Any,
                    **kwargs: Any,
                ):
                    rows = (
                        original_positions(
                            *args,
                            **kwargs,
                        )
                        or []
                    )
                    return [
                        self.merge(dict(row))
                        if isinstance(row, dict)
                        else row
                        for row in rows
                    ]

                portfolio.positions = MethodType(
                    positions,
                    portfolio,
                )
                portfolio._excursion_positions_patch = (
                    True
                )

        if (
            compound_engine is not None
            and not getattr(
                compound_engine,
                "_excursion_status_patch",
                False,
            )
        ):
            original_status = getattr(
                compound_engine,
                "status",
                None,
            )
            if callable(original_status):

                def compound_status(
                    component_self: Any,
                    *args: Any,
                    **kwargs: Any,
                ):
                    payload = (
                        original_status(
                            *args,
                            **kwargs,
                        )
                        or {}
                    )
                    return (
                        self._merge_compound_payload(
                            payload
                        )
                        if isinstance(
                            payload,
                            dict,
                        )
                        else payload
                    )

                compound_engine.status = MethodType(
                    compound_status,
                    compound_engine,
                )
                compound_engine._excursion_status_patch = (
                    True
                )

        if legacy_evidence is not None:
            if not getattr(
                legacy_evidence,
                "_excursion_status_patch",
                False,
            ):
                original_status = getattr(
                    legacy_evidence,
                    "status",
                    None,
                )
                if callable(original_status):

                    def legacy_status(
                        component_self: Any,
                        *args: Any,
                        **kwargs: Any,
                    ):
                        payload = (
                            original_status(
                                *args,
                                **kwargs,
                            )
                            or {}
                        )
                        if not isinstance(
                            payload,
                            dict,
                        ):
                            return payload

                        out = copy.deepcopy(
                            payload
                        )
                        mirrors = out.get(
                            "mirrors"
                        )

                        if isinstance(
                            mirrors,
                            dict,
                        ):
                            out["mirrors"] = {
                                k: (
                                    self.merge(v)
                                    if isinstance(
                                        v,
                                        dict,
                                    )
                                    else v
                                )
                                for k, v
                                in mirrors.items()
                            }
                        elif isinstance(
                            mirrors,
                            list,
                        ):
                            out["mirrors"] = [
                                self.merge(v)
                                if isinstance(v, dict)
                                else v
                                for v in mirrors
                            ]
                        return out

                    legacy_evidence.status = MethodType(
                        legacy_status,
                        legacy_evidence,
                    )
                    legacy_evidence._excursion_status_patch = (
                        True
                    )

            if not getattr(
                legacy_evidence,
                "_excursion_settled_patch",
                False,
            ):
                original_rows = getattr(
                    legacy_evidence,
                    "_settled_broker_rows",
                    None,
                )
                if callable(original_rows):

                    def settled_rows(
                        component_self: Any,
                        *args: Any,
                        **kwargs: Any,
                    ):
                        rows = (
                            original_rows(
                                *args,
                                **kwargs,
                            )
                            or []
                        )
                        return [
                            self.merge(dict(row))
                            if isinstance(
                                row,
                                dict,
                            )
                            else row
                            for row in rows
                        ]

                    legacy_evidence._settled_broker_rows = (
                        MethodType(
                            settled_rows,
                            legacy_evidence,
                        )
                    )
                    legacy_evidence._excursion_settled_patch = (
                        True
                    )

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        rows = self.rows(limit=2000)

        return {
            "version": self.VERSION,
            "enabled": True,
            "mfe_mae_observation_only": True,
            "server_r_watchdog_enabled": True,
            "server_hard_stop_enabled":
                bool(self.hard_stop_enabled),
            "server_hard_stop_r":
                self.hard_stop_r,
            "take_profit_execution_enabled":
                bool(self.take_profit_enabled),
            "take_profit_target_r_default":
                self.default_target_r,
            "take_profit_basis_new_trades":
                "PLANNED_RISK_R_MULTIPLE",
            "legacy_open_trade_take_profit_basis":
                "ENTRY_PRICE_FAVOURABLE_MOVE_PCT",
            "protective_stop_new_trades": True,
            "risk_policy_version":
                "v63-risk-exit-v1",
            "native_order_policy":
                "IG_DEALING_RULE_NORMALISED_OUTWARD_ONLY",
            "native_rejection_policy": (
                "SUPPRESS_REJECTED_REPEAT_AND_USE_SERVER_WATCHDOG"
                if not self.retry_rejected_native
                else "RETRY_AFTER_COOLDOWN"
            ),
            "take_profit_scope":
                "JASONG_OWNED_IG_DEMO_POSITIONS_ONLY",
            "take_profit_primary_execution":
                "IG_DEMO_NATIVE_LIMIT_LEVEL",
            "risk_primary_execution":
                "IG_DEMO_NATIVE_STOP_WHEN_ACCEPTED",
            "risk_fallback_execution":
                "SERVER_OBSERVED_HARD_CLOSE_AT_PLANNED_R",
            "take_profit_fallback_execution":
                "SERVER_OBSERVED_CLOSE_AT_PLANNED_R",
            "poll_seconds": self.poll_seconds,
            "price_extreme_basis":
                "BROKER_OBSERVED_EXIT_SIDE_QUOTES",
            "formulas": {
                "BUY_MFE":
                    "highest_price_since_entry - entry_price",
                "BUY_MAE":
                    "lowest_price_since_entry - entry_price",
                "SELL_MFE":
                    "entry_price - lowest_price_since_entry",
                "SELL_MAE":
                    "entry_price - highest_price_since_entry",
                "MFE_R":
                    "MFE / planned_risk_price_distance",
                "MAE_R":
                    "MAE / planned_risk_price_distance",
                "SERVER_HARD_STOP":
                    "current_R <= -hard_stop_R OR MAE_R <= -hard_stop_R",
                "SERVER_TP":
                    "current_R >= target_R OR MFE_R >= target_R",
            },
            "tracked_trades": len(rows),
            "open_trades": sum(
                1
                for row in rows
                if str(
                    row.get("status") or ""
                ).upper()
                == "OPEN"
            ),
            "risk_managed_trades": sum(
                1
                for row in rows
                if bool(
                    row.get("risk_policy_version")
                )
            ),
            "hard_stop_reached_trades": sum(
                1
                for row in rows
                if bool(
                    row.get("hard_stop_reached")
                )
            ),
            "hard_stop_close_sent_trades": sum(
                1
                for row in rows
                if str(
                    row.get(
                        "hard_stop_close_state"
                    )
                    or ""
                ).upper()
                in {
                    "CLOSE_SENT",
                    "CLOSE_VERIFIED",
                }
            ),
            "take_profit_reached_trades": sum(
                1
                for row in rows
                if bool(
                    row.get("take_profit_reached")
                )
            ),
            "take_profit_close_sent_trades": sum(
                1
                for row in rows
                if str(
                    row.get(
                        "take_profit_close_state"
                    )
                    or ""
                ).upper()
                in {
                    "CLOSE_SENT",
                    "CLOSE_VERIFIED",
                }
            ),
            "native_take_profit_attached_trades": sum(
                1
                for row in rows
                if str(
                    row.get(
                        "native_take_profit_state"
                    )
                    or ""
                ).upper()
                in {
                    "ATTACHED",
                    "CONFIRMED",
                }
            ),
            "native_protective_stop_trades": sum(
                1
                for row in rows
                if str(
                    row.get(
                        "native_protective_stop_state"
                    )
                    or ""
                ).upper()
                in {
                    "ATTACHED",
                    "CONFIRMED",
                }
            ),
            "native_rejected_suppressed_trades": sum(
                1
                for row in rows
                if bool(
                    row.get(
                        "native_order_suppressed"
                    )
                )
            ),
            "last_sync_at":
                self._state.get("last_sync_at"),
            "last_error":
                self._state.get("last_error"),
            "sync_count": int(
                self._state.get("sync_count")
                or 0
            ),
            "state_path": str(self.state_path),
            "live_money_execution": False,
        }

    def snapshot(
        self,
        limit: int = 500,
    ) -> Dict[str, Any]:
        return {
            **self.status(),
            "trades": self.rows(limit=limit),
        }

    # ------------------------------------------------------------------
    # Thread lifecycle
    # ------------------------------------------------------------------

    def start_thread(self) -> None:
        with self._lock:
            if (
                self._thread
                and self._thread.is_alive()
            ):
                return

            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="jasong-trade-excursions",
                daemon=True,
            )
            self._thread.start()

    def stop_thread(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.sync_once()
            self._stop.wait(
                self.poll_seconds
            )
