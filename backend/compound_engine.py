from __future__ import annotations

import json
import math
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from ig_demo_broker import IGDemoBroker, IGDemoError


class EliteCompoundEngine:
    """Jasong V6.7.3 Elite 80/20 Compound Engine — global multi-market release.

    Design goals:
      * preserve the existing Jasong AI / PAPER / SHADOW learning engines;
      * reuse their current validated watcher intelligence as the candidate feed;
      * execute a separate IG DEMO strategy using JSCMP_* deal references;
      * keep compound state in its own persistent file;
      * compare 1.2x / 1.3x / 1.5x basket targets using genuine IG DEMO cycles,
        while retaining the fixed -15% basket stop and 20% profit-harvest model;
      * select up to five elite, diversified markets across FX, indices, commodities, crypto, shares, ETFs and rates;
      * never touch manual IG positions or legacy JASONG_* positions;
      * never target IG live-money endpoints.

    IMPORTANT ACCOUNTING BOUNDARY
    -----------------------------
    IG's account P&L is account-wide. To keep cycle P&L attributable to this
    strategy, the engine will not open a new compound basket while any foreign
    broker position is open. "Foreign" means any open position whose deal
    reference does not start with JSCMP_.

    V6.7.1 changes the behaviour while the broker is not yet clean:
      * Elite ranking continues to run;
      * Live Intelligence can wake the engine immediately;
      * qualifying setups are retained as PENDING_ELITE candidates;
      * exact broker blockers are exposed to the mobile app;
      * the first eligible basket is opened automatically as soon as the broker
        becomes clean and the live setup still passes every Elite gate.

    Existing Jasong AI can therefore keep scanning, learning and collecting
    PAPER/SHADOW evidence while legacy IG entries drain, without losing the
    opportunity trail that produced the Compound decision.
    """

    VERSION = "6.8.17"
    DEAL_PREFIX = "JSCMP_"
    LEARNING_PREFIXES = (
        "JASONG_",
        "JSBND_",
        "JSLRN_",
        "JSELT_",
    )
    CLOSE_ALLOWED_MARKET_STATUSES = {"TRADEABLE", "CLOSINGS_ONLY"}

    def __init__(
        self,
        *,
        broker: IGDemoBroker,
        candidate_source: Callable[[float], List[Dict[str, Any]]],
        correlation_source: Optional[Callable[[], Dict[str, Dict[str, float]]]] = None,
        state_path: Optional[str] = None,
    ) -> None:
        self.broker = broker
        self.candidate_source = candidate_source
        self.correlation_source = correlation_source

        self.profit_target_pct = self._float_env(
            "COMPOUND_PROFIT_TARGET_PCT", 0.50, 0.01, 5.0
        )
        self.stop_loss_pct = self._float_env(
            "COMPOUND_STOP_LOSS_PCT", 0.15, 0.01, 1.0
        )
        self.harvest_pct = self._float_env(
            "COMPOUND_PROFIT_HARVEST_PCT", 0.20, 0.0, 1.0
        )
        # V6.8.16 target optimiser.
        #
        # 1.2x = +20% basket target
        # 1.3x = +30% basket target
        # 1.5x = +50% basket target
        #
        # With the existing 20%-of-profit harvest:
        #   1.2x -> +16% active, +4% reserve on a winning cycle
        #   1.3x -> +24% active, +6% reserve on a winning cycle
        #   1.5x -> +40% active, +10% reserve on a winning cycle
        #
        # This exactly preserves the existing 1.5x economics while allowing
        # 1.2x and 1.3x to be judged on probability + duration + recovery +
        # reserve rather than headline return alone.
        self.target_mode = str(
            os.getenv("COMPOUND_TARGET_MODE", "ADAPTIVE")
        ).upper().strip()
        if self.target_mode not in {"ADAPTIVE", "FIXED"}:
            self.target_mode = "ADAPTIVE"

        self.target_multiples = self._parse_target_multiples(
            os.getenv(
                "COMPOUND_TARGET_MULTIPLIERS",
                "1.2,1.3,1.5",
            )
        )
        self.default_target_multiple = self._nearest_target_multiple(
            self._float_env(
                "COMPOUND_TARGET_DEFAULT_MULTIPLE",
                1.30,
                1.01,
                6.0,
            )
        )
        self.target_min_samples_per_target = self._int_env(
            "COMPOUND_TARGET_MIN_SAMPLES_PER_TARGET",
            4,
            1,
            100,
        )
        self.target_evidence_window = self._int_env(
            "COMPOUND_TARGET_EVIDENCE_WINDOW",
            60,
            6,
            1000,
        )

        self.target_weight_probability = self._float_env(
            "COMPOUND_TARGET_WEIGHT_PROBABILITY",
            0.40,
            0.0,
            1.0,
        )
        self.target_weight_duration = self._float_env(
            "COMPOUND_TARGET_WEIGHT_DURATION",
            0.25,
            0.0,
            1.0,
        )
        self.target_weight_recovery = self._float_env(
            "COMPOUND_TARGET_WEIGHT_RECOVERY",
            0.20,
            0.0,
            1.0,
        )
        self.target_weight_reserve = self._float_env(
            "COMPOUND_TARGET_WEIGHT_RESERVE",
            0.15,
            0.0,
            1.0,
        )
        self.max_positions = self._int_env(
            "COMPOUND_MAX_POSITIONS", 5, 1, 10
        )
        self.global_broker_max_positions = self._int_env(
            "IG_DEMO_MAX_OPEN_POSITIONS", 15, 1, 50
        )
        self.required_basket_positions = self._int_env(
            "COMPOUND_REQUIRED_BASKET_POSITIONS",
            5,
            5,
            5,
        )
        self.candidate_pool_size = self._int_env(
            "COMPOUND_CANDIDATE_POOL_SIZE",
            12,
            5,
            30,
        )
        self.ai_min_confidence = self._float_env(
            "COMPOUND_AI_MIN_CONFIDENCE", 0.40, 0.0, 1.0
        )
        self.quant_min_confidence = self._float_env(
            "COMPOUND_QUANT_MIN_CONFIDENCE", 0.30, 0.0, 1.0
        )
        self.fast_score_min = self._float_env(
            "COMPOUND_FAST_SCORE_MIN", 90.0, 0.0, 100.0
        )
        # GLOBAL_MULTI_MARKET uses a different held-out evidence score
        # distribution from SERVER_FRESH_SIGNAL. Keep mature FX/server
        # fast scoring at 90, but calibrate global learning at 60.
        self.global_fast_score_min = self._float_env(
            "COMPOUND_GLOBAL_FAST_SCORE_MIN", 60.0, 0.0, 100.0
        )
        # V6.8.0 minimum-to-maximum learning floor. These values do NOT
        # weaken Elite Compound execution; they only classify useful near-miss
        # evidence for the IG DEMO learning path.
        self.learning_ai_min_confidence = self._float_env(
            "LEARNING_AI_MIN_CONFIDENCE", 0.25, 0.0, 1.0
        )
        self.learning_quant_min_confidence = self._float_env(
            "LEARNING_QUANT_MIN_CONFIDENCE", 0.15, 0.0, 1.0
        )
        self.learning_fast_score_min = self._float_env(
            "LEARNING_FAST_SCORE_MIN", 50.0, 0.0, 100.0
        )
        self.max_spread_bps = self._float_env(
            "COMPOUND_MAX_SPREAD_BPS", 8.0, 0.1, 100.0
        )
        # V6.7.3 uses asset-class-aware spread ceilings.  These are raw
        # spread-in-basis-points gates, not forecasts of total trading cost.
        self.asset_spread_bps = {
            "FX": self._float_env("COMPOUND_FX_MAX_SPREAD_BPS", 8.0, 0.1, 100.0),
            "INDEX": self._float_env("COMPOUND_INDEX_MAX_SPREAD_BPS", 18.0, 0.1, 200.0),
            "COMMODITY": self._float_env("COMPOUND_COMMODITY_MAX_SPREAD_BPS", 22.0, 0.1, 300.0),
            "CRYPTO": self._float_env("COMPOUND_CRYPTO_MAX_SPREAD_BPS", 80.0, 0.1, 500.0),
            "SHARE": self._float_env("COMPOUND_SHARE_MAX_SPREAD_BPS", 35.0, 0.1, 300.0),
            "ETF": self._float_env("COMPOUND_ETF_MAX_SPREAD_BPS", 35.0, 0.1, 300.0),
            "RATE": self._float_env("COMPOUND_RATE_MAX_SPREAD_BPS", 25.0, 0.1, 300.0),
        }
        self.max_theme_exposure = self._int_env(
            "COMPOUND_MAX_THEME_EXPOSURE", 2, 1, 5
        )
        self.high_correlation_abs = self._float_env(
            "COMPOUND_HIGH_CORRELATION_ABS", 0.80, 0.0, 1.0
        )
        self.max_currency_exposure = self._int_env(
            "COMPOUND_MAX_CURRENCY_EXPOSURE", 2, 1, 5
        )
        self.poll_seconds = self._int_env(
            "COMPOUND_POLL_SECONDS", 15, 5, 300
        )
        self.selection_refresh_seconds = self._int_env(
            "COMPOUND_SELECTION_REFRESH_SECONDS",
            15,
            5,
            300,
        )
        self.restart_cooldown_seconds = self._int_env(
            "COMPOUND_RESTART_COOLDOWN_SECONDS", 60, 10, 3600
        )
        self.reference_capital = self._float_env(
            "COMPOUND_REFERENCE_CAPITAL", 1000.0, 1.0, 1000000000.0
        )
        self.reference_deal_size = self._float_env(
            "COMPOUND_REFERENCE_DEAL_SIZE", 0.50, 0.000001, 1000000.0
        )
        self.max_deal_size = self._float_env(
            "COMPOUND_MAX_DEAL_SIZE", 10.0, 0.000001, 1000000.0
        )
        self.min_cycle_capital = self._float_env(
            "COMPOUND_MIN_CYCLE_CAPITAL", 1.0, 0.01, 1000000000.0
        )

        default_state = (
            "/var/data/jasong_compound_state.json"
            if Path("/var/data").exists()
            else "/tmp/jasong_compound_state.json"
        )
        self.state_path = Path(
            state_path
            or os.getenv("COMPOUND_STATE_PATH", default_state)
        )

        self._lock = threading.RLock()
        self._tick_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._correlation_cache: Dict[str, Dict[str, float]] = {}
        self._correlation_cache_at = 0.0

        self._state: Dict[str, Any] = {
            "version": self.VERSION,
            "enabled": False,
            "auto_restart": True,
            "campaign_id": None,
            "campaign_started_at": None,
            "campaign_initial_capital": None,
            "current_capital": None,
            "reserve_balance": 0.0,
            "total_harvested": 0.0,
            "cycle_number": 0,
            "current_cycle": None,
            "cycles": [],
            "candidate_journal": [],
            "last_candidate_ranking": [],
            "last_selection_at": None,
            "next_cycle_at": None,
            "status": "STOPPED",
            "paused_reason": None,
            "last_tick_at": None,
            "last_error": None,
            "broker_account": {},
            "broker_positions": [],
            "legacy_execution_paused": False,
            "pending_elite_candidates": [],
            "last_intelligence_signal": None,
            "last_intelligence_at": None,
            "intelligence_bridge_state": "IDLE",
            "intelligence_wake_count": 0,
            "last_foreign_blockers": [],
            "close_integrity": {
                "attempts": 0,
                "verified": 0,
                "pending": 0,
                "errors": 0,
                "last_error": None,
            },
        }
        self._load()

    # ------------------------------------------------------------------
    # Configuration / persistence
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_target_multiples(raw: str) -> List[float]:
        values: List[float] = []
        for token in str(raw or "").split(","):
            try:
                value = round(float(token.strip()), 4)
            except Exception:
                continue
            if value <= 1.0:
                continue
            if value not in values:
                values.append(value)

        if not values:
            values = [1.2, 1.3, 1.5]

        # The strategy comparison is intentionally constrained to sensible
        # positive basket multipliers and sorted for stable reporting.
        return sorted(values)

    def _nearest_target_multiple(
        self,
        value: float,
    ) -> float:
        candidates = (
            self.target_multiples
            if hasattr(self, "target_multiples")
            and self.target_multiples
            else [1.2, 1.3, 1.5]
        )
        return min(
            candidates,
            key=lambda item: abs(float(item) - float(value)),
        )

    @staticmethod
    def _float_env(name: str, default: float, minimum: float, maximum: float) -> float:
        try:
            value = float(os.getenv(name, str(default)))
        except Exception:
            value = default
        if not math.isfinite(value):
            value = default
        return max(minimum, min(maximum, value))

    @staticmethod
    def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(os.getenv(name, str(default)))
        except Exception:
            value = default
        return max(minimum, min(maximum, value))

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            number = float(value)
            if not math.isfinite(number):
                return default
            return number
        except Exception:
            return default

    @staticmethod
    def _now() -> float:
        return time.time()

    def _load(self) -> None:
        try:
            if self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._state.update(data)
            self._state["version"] = self.VERSION
        except Exception as exc:
            self._state["last_error"] = f"state load: {type(exc).__name__}: {exc}"

    def _persist(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._state, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            tmp.replace(self.state_path)
        except Exception as exc:
            self._state["last_error"] = f"state persist: {type(exc).__name__}: {exc}"

    def _journal(self, event: str, payload: Optional[Dict[str, Any]] = None) -> None:
        row = {"event": event, "timestamp": self._now(), **dict(payload or {})}
        with self._lock:
            rows = list(self._state.get("candidate_journal") or [])
            rows.append(row)
            self._state["candidate_journal"] = rows[-3000:]
        self._persist()

    # ------------------------------------------------------------------
    # Thread lifecycle
    # ------------------------------------------------------------------

    def start_thread(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="jasong-v680-elite-compound",
                daemon=True,
            )
            self._thread.start()

    def stop_thread(self) -> None:
        self._stop_event.set()
        self._wake_event.set()

    def wake(self) -> None:
        """Wake the server-side Compound loop without waiting for the next poll."""
        self._wake_event.set()

    def notify_intelligence(
        self,
        snapshot: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Receive the same Live Intelligence snapshot shown in the mobile app.

        The signal itself is not an order. It only wakes Compound immediately.
        The candidate must still pass Fast >= configured minimum, A/A+ quality,
        deep-validation eligibility, AI/Quant floors, IG spread/tradeability,
        correlation and currency-exposure gates.

        This method is deliberately lightweight so the /signal request does not
        block on broker/deep-validation work.
        """
        clean = dict(snapshot or {})
        now = self._now()

        with self._lock:
            self._state["last_intelligence_signal"] = clean or None
            self._state["last_intelligence_at"] = now
            self._state["intelligence_bridge_state"] = "LIVE_SIGNAL_RECEIVED"
            self._state["intelligence_wake_count"] = int(
                self._state.get("intelligence_wake_count") or 0
            ) + 1
            self._persist()

        self.wake()
        return {
            "accepted": True,
            "bridge_state": "LIVE_SIGNAL_RECEIVED",
            "compound_enabled": bool(self._state.get("enabled")),
            "signal": clean,
            "environment": "DEMO",
            "live_money_execution": False,
        }

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception as exc:
                with self._lock:
                    self._state["last_error"] = f"{type(exc).__name__}: {exc}"
                    self._state["last_tick_at"] = self._now()
                    self._persist()

            # V6.7.1: sleep normally, but allow shared Live Intelligence to wake
            # the loop immediately when a fresh BUY/SELL signal arrives.
            self._wake_event.wait(self.poll_seconds)
            self._wake_event.clear()

    # ------------------------------------------------------------------
    # Broker state
    # ------------------------------------------------------------------

    def _account_snapshot(self) -> Dict[str, Any]:
        payload = self.broker.accounts()
        status = self.broker.status()
        active_id = str(status.get("account_id") or "")
        rows = [
            dict(item)
            for item in (payload.get("accounts") or [])
            if isinstance(item, dict)
        ]
        selected: Dict[str, Any] = {}
        for item in rows:
            if active_id and str(item.get("accountId") or "") == active_id:
                selected = item
                break
        if not selected:
            selected = next((item for item in rows if item.get("preferred") is True), {})
        if not selected and rows:
            selected = rows[0]

        balance_block = selected.get("balance")
        if not isinstance(balance_block, dict):
            balance_block = {}

        return {
            "account_id": selected.get("accountId") or active_id or None,
            "account_name": selected.get("accountName"),
            "currency": selected.get("currency") or selected.get("currencyIsoCode"),
            "balance": self._safe_float(balance_block.get("balance"), 0.0),
            "available": self._safe_float(balance_block.get("available"), 0.0),
            "margin": self._safe_float(balance_block.get("deposit"), 0.0),
            "profit_loss": self._safe_float(balance_block.get("profitLoss"), 0.0),
            "environment": "DEMO",
            "live_money_execution": False,
        }

    def _normalise_positions(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for item in payload.get("positions", []) or []:
            if not isinstance(item, dict):
                continue
            position = item.get("position") or {}
            market = item.get("market") or {}
            if not isinstance(position, dict):
                continue
            if not isinstance(market, dict):
                market = {}

            deal_id = str(position.get("dealId") or "").strip()
            if not deal_id:
                continue
            ref = str(position.get("dealReference") or "").strip()
            epic = str(market.get("epic") or position.get("epic") or "").strip()
            symbol = str(
                market.get("instrumentName")
                or market.get("name")
                or epic
                or "IG DEMO"
            )
            size = self._safe_float(
                position.get("size")
                if position.get("size") is not None
                else position.get("dealSize"),
                0.0,
            )
            level = self._safe_float(
                position.get("level")
                if position.get("level") is not None
                else position.get("openLevel"),
                0.0,
            )
            rows.append(
                {
                    "deal_id": deal_id,
                    "deal_reference": ref,
                    "epic": epic,
                    "symbol": symbol,
                    "direction": str(position.get("direction") or "").upper(),
                    "size": size,
                    "entry_level": level,
                    "bid": market.get("bid"),
                    "offer": market.get("offer"),
                    "market_status": market.get("marketStatus"),
                    "currency": position.get("currency"),
                    "is_compound": ref.startswith(self.DEAL_PREFIX),
                    "is_learning": ref.startswith(self.LEARNING_PREFIXES),
                    "is_jasong_owned": (
                        ref.startswith(self.DEAL_PREFIX)
                        or ref.startswith(self.LEARNING_PREFIXES)
                    ),
                    "is_external_manual": not (
                        ref.startswith(self.DEAL_PREFIX)
                        or ref.startswith(self.LEARNING_PREFIXES)
                    ),
                    "is_legacy_jasong": ref.startswith("JASONG_"),
                }
            )
        return rows

    def _broker_snapshot(self) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
        self.broker.connect()
        account = self._account_snapshot()
        positions = self._normalise_positions(self.broker.positions())
        with self._lock:
            self._state["broker_account"] = account
            self._state["broker_positions"] = positions
        return account, positions

    @staticmethod
    def _external_positions(
        positions: Iterable[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        return [
            dict(row)
            for row in positions
            if bool(row.get("is_external_manual"))
        ]

    @staticmethod
    def _learning_positions(
        positions: Iterable[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        return [
            dict(row)
            for row in positions
            if bool(row.get("is_learning"))
        ]

    @staticmethod
    def _compound_positions(
        positions: Iterable[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        return [
            dict(row)
            for row in positions
            if bool(row.get("is_compound"))
        ]

    # Backward-compatible name. In V6.8.13 "foreign" means genuinely external,
    # not another Jasong-owned Learning position.
    @classmethod
    def _foreign_positions(
        cls,
        positions: Iterable[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        return cls._external_positions(positions)

    def _compound_open_capacity(
        self,
        positions: List[Dict[str, Any]],
    ) -> Dict[str, int]:
        """Account-wide IG DEMO capacity with a separate Compound basket cap."""
        total_open = len(positions)
        compound_open = len(
            self._compound_positions(positions)
        )
        global_remaining = max(
            0,
            self.global_broker_max_positions - total_open,
        )
        compound_remaining = max(
            0,
            self.max_positions - compound_open,
        )
        return {
            "global_max": self.global_broker_max_positions,
            "compound_max": self.max_positions,
            "total_open": total_open,
            "compound_open": compound_open,
            "global_remaining": global_remaining,
            "compound_remaining": compound_remaining,
            "compound_slots_available": min(
                global_remaining,
                compound_remaining,
            ),
        }

    @staticmethod
    def _same_broker_market(
        candidate: Dict[str, Any],
        position: Dict[str, Any],
    ) -> bool:
        candidate_epic = str(
            candidate.get("ig_epic") or ""
        ).upper().strip()
        position_epic = str(
            position.get("epic") or ""
        ).upper().strip()
        if candidate_epic and position_epic:
            return candidate_epic == position_epic

        candidate_symbol = "".join(
            ch
            for ch in str(
                candidate.get("symbol")
                or candidate.get("market")
                or ""
            ).upper()
            if ch.isalnum()
        )
        position_symbol = "".join(
            ch
            for ch in str(
                position.get("symbol")
                or ""
            ).upper()
            if ch.isalnum()
        )
        return bool(
            candidate_symbol
            and position_symbol
            and (
                candidate_symbol in position_symbol
                or position_symbol in candidate_symbol
            )
        )

    def _remove_learning_duplicate_exposure(
        self,
        selected: List[Dict[str, Any]],
        learning_positions: List[Dict[str, Any]],
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Do not let Compound duplicate a market already owned by Learning."""
        clean: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []

        for candidate in selected:
            blocker = next(
                (
                    position
                    for position in learning_positions
                    if self._same_broker_market(
                        candidate,
                        position,
                    )
                ),
                None,
            )
            if blocker is None:
                clean.append(candidate)
                continue

            row = dict(candidate)
            row["selected"] = False
            row["execution_eligible"] = False
            reasons = list(
                row.get("rejection_reasons") or []
            )
            reasons.append(
                "Existing Jasong Learning IG DEMO exposure on the same market"
            )
            row["rejection_reasons"] = list(
                dict.fromkeys(reasons)
            )
            row["duplicate_broker_deal_id"] = blocker.get(
                "deal_id"
            )
            row["duplicate_broker_reference"] = blocker.get(
                "deal_reference"
            )
            row["duplicate_exposure_source"] = "LEARNING_MIRROR"
            skipped.append(row)

        return clean, skipped

    def _compound_running_pnl(
        self,
        account: Dict[str, Any],
        compound_positions: List[Dict[str, Any]],
        learning_positions: List[Dict[str, Any]],
    ) -> tuple[float, Dict[str, Any]]:
        """Return JSCMP-only running P&L.

        If no Learning position is open, the IG account-wide P&L is clean and
        remains the authoritative value. If Learning shares the account, use
        isolated JSCMP mark-to-market from broker metadata.
        """
        if not learning_positions:
            value = self._safe_float(
                account.get("profit_loss"),
                0.0,
            )
            return value, {
                "mode": "IG_ACCOUNT_PNL_CLEAN",
                "learning_positions": 0,
                "total_pnl": value,
            }

        isolated = self.broker.estimate_positions_pnl(
            compound_positions,
            account_currency=str(
                account.get("currency") or ""
            ),
        )
        return self._safe_float(
            isolated.get("total_pnl"),
            0.0,
        ), isolated

    @classmethod
    def _signed_move_bps(
        cls,
        position: Dict[str, Any],
    ) -> Optional[float]:
        entry = cls._safe_float(
            position.get("entry_level"),
            0.0,
        )
        if entry <= 0:
            return None

        direction = str(
            position.get("direction")
            or ""
        ).upper()
        bid = cls._safe_float(
            position.get("bid"),
            0.0,
        )
        offer = cls._safe_float(
            position.get("offer"),
            0.0,
        )

        if direction == "BUY" and bid > 0:
            return ((bid - entry) / entry) * 10000.0
        if direction == "SELL" and offer > 0:
            return ((entry - offer) / entry) * 10000.0
        return None

    def _update_cycle_position_excursions(
        self,
        cycle: Dict[str, Any],
        broker_positions: List[Dict[str, Any]],
    ) -> None:
        by_deal = {
            str(row.get("deal_id") or ""): row
            for row in broker_positions
            if row.get("deal_id")
        }
        stored = [
            dict(row)
            for row in (cycle.get("positions") or [])
            if isinstance(row, dict)
        ]
        now = self._now()

        for row in stored:
            deal_id = str(
                row.get("deal_id")
                or row.get("ig_deal_id")
                or ""
            )
            live = by_deal.get(
                deal_id
            )
            if live is None:
                continue

            move = self._signed_move_bps(
                live
            )
            if move is None:
                continue

            row["current_move_bps"] = round(
                move,
                4,
            )
            row["mfe_bps"] = round(
                max(
                    0.0,
                    self._safe_float(
                        row.get("mfe_bps"),
                        0.0,
                    ),
                    move,
                ),
                4,
            )
            row["mae_bps"] = round(
                min(
                    0.0,
                    self._safe_float(
                        row.get("mae_bps"),
                        0.0,
                    ),
                    move,
                ),
                4,
            )
            row["current_close_level"] = (
                live.get("bid")
                if str(
                    live.get("direction")
                    or ""
                ).upper() == "BUY"
                else live.get("offer")
            )
            row["last_excursion_at"] = now
            row["broker_size_now"] = live.get(
                "size"
            )
            row["broker_market_status"] = live.get(
                "market_status"
            )

        cycle["positions"] = stored

    # ------------------------------------------------------------------
    # Candidate scoring / diversification
    # ------------------------------------------------------------------

    @staticmethod
    def _quality_score(quality: str, deep_status: str) -> float:
        q = str(quality or "").upper().strip()
        d = str(deep_status or "").upper().strip()
        q_score = {
            "A+": 100.0,
            "A": 92.0,
            "B+": 78.0,
            "B": 68.0,
            "C+": 52.0,
            "C": 42.0,
        }.get(q, 0.0)
        d_score = {
            "VERIFIED": 100.0,
            "NEAR_VERIFIED": 92.0,
            "WATCH": 82.0,
            "AI_LEARNING_SHADOW_PROMOTION": 86.0,
            "GLOBAL_VERIFIED": 100.0,
            "GLOBAL_NEAR_VERIFIED": 92.0,
            # A rejection is still useful forward evidence; it is not Elite.
            "REJECT": 40.0,
            "GLOBAL_REJECT": 40.0,
        }.get(d, 0.0)
        return 0.60 * q_score + 0.40 * d_score

    def _required_fast_score(self, row: Dict[str, Any]) -> float:
        """Return the source-calibrated fast threshold."""
        source = str(row.get("intelligence_source") or "").upper().strip()
        if source == "GLOBAL_MULTI_MARKET":
            return float(self.global_fast_score_min)
        return float(self.fast_score_min)

    def _learning_floor_passes(
        self, ai: float, quant: float, fast: float, quality: str
    ) -> bool:
        return bool(
            ai >= self.learning_ai_min_confidence
            and quant >= self.learning_quant_min_confidence
            and fast >= self.learning_fast_score_min
            and str(quality or "").upper().strip() in {"B", "B+", "A", "A+"}
        )

    def _elite_state(
        self,
        *,
        ai: float,
        quant: float,
        fast: float,
        quality: str,
        deep: str,
        direction_match: bool,
        spread_ok: Optional[bool],
        elite_score: float,
        strict_reasons: List[str],
        technical_invalid: bool = False,
    ) -> str:
        if technical_invalid:
            return "INVALID"
        if not strict_reasons:
            if str(quality).upper() == "A+" and elite_score >= 85.0:
                return "ELITE_A_PLUS"
            return "ELITE_A"
        if not self._learning_floor_passes(ai, quant, fast, quality):
            return "OBSERVE"
        ai_gap = abs(ai - self.ai_min_confidence)
        quant_gap = abs(quant - self.quant_min_confidence)
        fast_gap = abs(fast - self.fast_score_min)
        near_boundary = ai_gap <= 0.05 or quant_gap <= 0.05 or fast_gap <= 10.0
        return "LEARNING_PLUS" if near_boundary or len(strict_reasons) == 1 else "LEARNING"

    @staticmethod
    def _normalise_pair(value: Any) -> str:
        """Normalise true FX pairs without mangling non-FX market keys."""
        text = str(value or "").upper().strip()
        compact = text.replace("=X", "").replace(" ", "")
        if "/" in compact:
            parts = compact.split("/", 1)
            if len(parts) == 2 and len(parts[0]) == 3 and len(parts[1]) == 3 and parts[0].isalpha() and parts[1].isalpha():
                return f"{parts[0]}/{parts[1]}"
        letters = "".join(ch for ch in compact if ch.isalpha())
        # Only six-letter alphabetic values are treated as compact FX pairs.
        if len(compact) == 6 and len(letters) == 6:
            return f"{letters[:3]}/{letters[3:]}"
        return text

    @classmethod
    def _currency_effect(cls, symbol: str, direction: str) -> Dict[str, int]:
        pair = cls._normalise_pair(symbol)
        if "/" not in pair:
            return {}
        base, quote = pair.split("/", 1)
        if len(base) != 3 or len(quote) != 3:
            return {}
        if str(direction).upper() == "BUY":
            return {base: 1, quote: -1}
        if str(direction).upper() == "SELL":
            return {base: -1, quote: 1}
        return {}

    def _correlation_matrix(self) -> Dict[str, Dict[str, float]]:
        if self.correlation_source is None:
            return {}
        now = self._now()
        if self._correlation_cache and now - self._correlation_cache_at < 300:
            return self._correlation_cache
        try:
            matrix = self.correlation_source() or {}
            if isinstance(matrix, dict):
                self._correlation_cache = matrix
                self._correlation_cache_at = now
        except Exception:
            pass
        return self._correlation_cache

    def _correlation(self, matrix: Dict[str, Dict[str, float]], left: str, right: str) -> float:
        l = self._normalise_pair(left).upper()
        r = self._normalise_pair(right).upper()
        candidates_left = [left.upper(), l, l.replace("/", "")]
        candidates_right = [right.upper(), r, r.replace("/", "")]
        for a in candidates_left:
            row = matrix.get(a)
            if not isinstance(row, dict):
                continue
            for b in candidates_right:
                try:
                    if b in row:
                        return float(row[b])
                except Exception:
                    continue
        return 0.0

    def _spread_metrics(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        symbol = str(candidate.get("symbol") or candidate.get("market") or "")
        epic = str(candidate.get("ig_epic") or "").strip()
        asset_class = str(candidate.get("asset_class") or "FX").upper().strip()

        if epic:
            details = self.broker.market_details(epic, require_quote=True)
            instrument = details.get("instrument") or {}
            snapshot = details.get("snapshot") or {}
            status = str(snapshot.get("marketStatus") or candidate.get("ig_market_status") or "").upper()
            if status != "TRADEABLE":
                return {
                    "ok": False,
                    "reason": f"IG DEMO market status {status or 'UNKNOWN'}",
                    "spread_bps": None,
                    "spread_score": 0.0,
                    "market": {
                        "epic": epic,
                        "name": instrument.get("name") or candidate.get("market"),
                        "instrument_type": instrument.get("type"),
                        "market_status": status,
                        "details": details,
                    },
                }
            market = {
                "symbol": symbol,
                "epic": epic,
                "name": instrument.get("name") or candidate.get("market"),
                "instrument_type": instrument.get("type"),
                "market_status": status,
                "details": details,
            }
        else:
            if asset_class == "FX":
                market = self.broker.resolve_market(
                    symbol,
                    require_tradeable=True,
                )
            else:
                # V6.8.5: non-FX candidates must NEVER be routed through the
                # six-letter FX parser. Reuse the seed metadata carried by the
                # global intelligence engine and resolve the actual IG market.
                search_terms = list(
                    candidate.get("ig_search_terms")
                    or [candidate.get("market"), candidate.get("symbol")]
                )
                expected_types = list(
                    candidate.get("expected_types")
                    or [asset_class]
                )
                name_tokens = list(
                    candidate.get("name_tokens")
                    or [candidate.get("market"), candidate.get("symbol")]
                )
                market = self.broker.resolve_global_market(
                    search_terms=search_terms,
                    expected_types=expected_types,
                    name_tokens=name_tokens,
                    require_tradeable=True,
                    cache_key=str(
                        candidate.get("key")
                        or candidate.get("symbol")
                        or candidate.get("market")
                        or ""
                    ),
                )

            details = market.get("details") or {}
            snapshot = details.get("snapshot") or {}

        snapshot = details.get("snapshot") or {}
        quote = (
            self.broker.extract_snapshot_quote(details)
            if hasattr(self.broker, "extract_snapshot_quote")
            else {}
        )
        fallback_quote = details.get("_quote_snapshot") or {}
        bid = self._safe_float(
            quote.get("bid")
            if quote.get("bid") is not None
            else fallback_quote.get("bid"),
            0.0,
        )
        offer = self._safe_float(
            quote.get("offer")
            if quote.get("offer") is not None
            else fallback_quote.get("offer"),
            0.0,
        )
        if bid <= 0 or offer <= 0 or offer < bid:
            return {
                "ok": False,
                "reason": "IG DEMO spread unavailable after v4 ladder/direct quote and v3 snapshot fallback",
                "spread_bps": None,
                "spread_score": 0.0,
                "market": market,
            }
        mid = (bid + offer) / 2.0
        spread_bps = ((offer - bid) / mid) * 10000.0 if mid > 0 else 999.0
        limit = self.asset_spread_bps.get(asset_class, self.max_spread_bps)
        spread_score = max(0.0, 100.0 * (1.0 - spread_bps / max(limit, 0.0001)))
        return {
            "ok": spread_bps <= limit,
            "reason": None if spread_bps <= limit else f"{asset_class} spread {spread_bps:.1f} bps > {limit:.1f} bps",
            "spread_bps": spread_bps,
            "spread_limit_bps": limit,
            "spread_score": spread_score,
            "market": market,
        }

    def _rank_candidates(
        self,
        capital: float,
        selection_limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Rank all signals for IG DEMO confidence-first execution.

        V6.8.2 learning rule:
        - AI, Quant and Fast are the REQUIRED confidence gates.
        - A/A+ Quality and Deep Validation still contribute evidence and the
          Elite label, but they no longer block an otherwise confidence-qualified
          IG DEMO trade.
        - Direction agreement, exact IG mapping/tradeability and spread remain
          hard execution-safety requirements.
        - Correlation/exposure limits remain portfolio safety controls.

        This deliberately increases genuine broker-forward observations while
        keeping live-money execution disabled.
        """
        source_rows = self.candidate_source(capital) or []
        screened: List[Dict[str, Any]] = []
        selection_limit = max(
            self.required_basket_positions,
            int(
                selection_limit
                or self.max_positions
            ),
        )

        allowed_deep = {
            "VERIFIED", "NEAR_VERIFIED", "WATCH",
            "AI_LEARNING_SHADOW_PROMOTION",
            "GLOBAL_VERIFIED", "GLOBAL_NEAR_VERIFIED",
        }

        for source_rank, raw in enumerate(source_rows, start=1):
            row = dict(raw or {})
            row["source_rank"] = source_rank
            row["eligible"] = False
            row["elite_eligible"] = False
            row["learning_eligible"] = False
            row["selected"] = False
            row["rejection_reasons"] = []

            symbol = self._normalise_pair(row.get("symbol") or row.get("market"))
            direction = str(row.get("direction") or "").upper().strip()
            ai = self._safe_float(row.get("model_ai_confidence"), 0.0)
            quant = self._safe_float(row.get("quant_confidence"), 0.0)
            fast = self._safe_float(row.get("smart_fast_score"), 0.0)
            required_fast = self._required_fast_score(row)
            quality = str(row.get("quality_tier") or "").upper().strip()
            deep = str(row.get("deep_status") or "").upper().strip()
            direction_match = bool(row.get("direction_match"))

            row.update({
                "symbol": symbol,
                "direction": direction,
                "model_ai_confidence": ai,
                "quant_confidence": quant,
                "smart_fast_score": fast,
                "quality_tier": quality,
                "deep_status": deep,
                "direction_match": direction_match,
                "threshold_distance": {
                    "ai_pct_points": round((ai - self.ai_min_confidence) * 100.0, 3),
                    "quant_pct_points": round((quant - self.quant_min_confidence) * 100.0, 3),
                    "fast_points": round(fast - required_fast, 3),
                    "required_fast_score": round(required_fast, 2),
                },
            })

            technical_invalid = (not symbol or direction not in {"BUY", "SELL"})

            confidence_reasons: List[str] = []
            if technical_invalid:
                confidence_reasons.append("Invalid symbol/direction")
            if ai < self.ai_min_confidence:
                confidence_reasons.append(
                    f"AI {ai*100:.1f}% < {self.ai_min_confidence*100:.0f}%"
                )
            if quant < self.quant_min_confidence:
                confidence_reasons.append(
                    f"Quant {quant*100:.1f}% < {self.quant_min_confidence*100:.0f}%"
                )
            if fast < required_fast:
                confidence_reasons.append(
                    f"Fast score {fast:.1f} < {required_fast:.0f} "
                    f"for {str(row.get('intelligence_source') or 'DEFAULT')}"
                )
            if not direction_match:
                confidence_reasons.append("Live direction does not agree")

            # Elite evidence is still measured, but no longer blocks an IG DEMO
            # trade once the required confidence gates have passed.
            elite_only_reasons: List[str] = []
            if quality not in {"A+", "A"}:
                elite_only_reasons.append(
                    f"Quality {quality or '-'} below Elite A/A+ evidence standard"
                )
            if deep not in allowed_deep:
                elite_only_reasons.append(
                    f"Deep status {deep or '-'} below Elite evidence standard"
                )

            confidence_pass = (
                not technical_invalid
                and ai >= self.ai_min_confidence
                and quant >= self.quant_min_confidence
                and fast >= required_fast
                and direction_match
            )

            # A confidence-qualified signal MUST get an IG execution preflight.
            # We still refuse malformed/untradeable broker data even in DEMO.
            spread = {
                "ok": None,
                "spread_score": 0.0,
                "spread_bps": None,
                "spread_limit_bps": None,
                "market": {},
            }
            if confidence_pass:
                try:
                    spread = self._spread_metrics(row)
                except Exception as exc:
                    spread = {
                        "ok": False,
                        "reason": (
                            f"IG DEMO preflight: {type(exc).__name__}: {exc}"
                        ),
                        "spread_bps": None,
                        "spread_score": 0.0,
                        "spread_limit_bps": None,
                        "market": {},
                    }

                row["spread_bps"] = spread.get("spread_bps")
                row["spread_score"] = spread.get("spread_score")
                row["spread_limit_bps"] = spread.get("spread_limit_bps")
                resolved_market = spread.get("market") or {}
                row["ig_epic"] = (
                    resolved_market.get("epic")
                    or row.get("ig_epic")
                )
                row["ig_market_name"] = (
                    resolved_market.get("name")
                    or row.get("ig_market_name")
                )
                row["ig_instrument_type"] = (
                    resolved_market.get("instrument_type")
                    or row.get("ig_instrument_type")
                )
                row["ig_instrument_family"] = (
                    resolved_market.get("instrument_family")
                    or row.get("ig_instrument_family")
                )
                row["ig_market_status"] = (
                    resolved_market.get("market_status")
                    or row.get("ig_market_status")
                )

                if spread.get("ok") is not True:
                    confidence_reasons.append(
                        str(spread.get("reason") or "IG spread/tradeability failed")
                    )
                    confidence_pass = False

            deep_quality = self._quality_score(quality, deep)
            # Always produce a quality score. A failed gate must never collapse
            # the information to Elite 0.0.
            base_score = (
                25.0 * ai
                + 25.0 * quant
                + 0.20 * deep_quality
                + 0.15 * fast
                + 0.10 * self._safe_float(spread.get("spread_score"), 0.0)
            )
            row["deep_quality_score"] = round(deep_quality, 2)
            row["elite_base_score"] = round(base_score, 2)
            row["elite_score"] = round(base_score, 2)

            strict_pass = bool(
                confidence_pass
                and quality in {"A+", "A"}
                and deep in allowed_deep
            )

            # Confidence execution is the V6.8.2 IG DEMO rule.
            execution_reasons = list(dict.fromkeys(confidence_reasons))
            row["rejection_reasons"] = execution_reasons
            row["elite_reasons"] = list(dict.fromkeys(elite_only_reasons))
            row["elite_evidence_notes"] = [
                note
                .replace("not elite-eligible", "below Elite evidence standard")
                .replace("is not A/A+", "below Elite A/A+ evidence standard")
                for note in row["elite_reasons"]
            ]
            row["confidence_qualified"] = bool(confidence_pass)
            row["execution_eligible"] = bool(confidence_pass)
            row["required_fast_score"] = round(required_fast, 2)
            row["fast_threshold_source"] = (
                "GLOBAL_CALIBRATED"
                if str(row.get("intelligence_source") or "").upper() == "GLOBAL_MULTI_MARKET"
                else "SERVER_FRESH"
            )
            row["elite_eligible"] = strict_pass
            row["eligible"] = bool(confidence_pass)
            row["learning_eligible"] = bool(confidence_pass and not strict_pass)

            row["elite_state"] = (
                self._elite_state(
                    ai=ai,
                    quant=quant,
                    fast=fast,
                    quality=quality,
                    deep=deep,
                    direction_match=direction_match,
                    spread_ok=spread.get("ok"),
                    elite_score=row["elite_score"],
                    strict_reasons=elite_only_reasons,
                    technical_invalid=False,
                )
                if confidence_pass
                else "OBSERVE"
            )

            row["trade_class"] = (
                "ELITE"
                if strict_pass
                else "CONFIDENCE"
                if confidence_pass
                else "OBSERVE"
            )
            row["execution_basis"] = (
                "REQUIRED_CONFIDENCE"
                if confidence_pass
                else "NOT_QUALIFIED"
            )
            row["ig_demo_learning_eligible"] = bool(confidence_pass)
            screened.append(row)

        # V6.8.2: select all signals that pass the required confidence +
        # broker-safety gates. Elite is now an evidence label, not a trade gate.
        eligible = [row for row in screened if row.get("execution_eligible")]
        eligible.sort(
            key=lambda r: (
                self._safe_float(r.get("elite_base_score"), 0.0),
                self._safe_float(r.get("model_ai_confidence"), 0.0),
                self._safe_float(r.get("quant_confidence"), 0.0),
            ),
            reverse=True,
        )

        matrix = self._correlation_matrix()
        selected: List[Dict[str, Any]] = []
        exposures: Dict[str, int] = {}
        for row in eligible:
            if len(selected) >= selection_limit:
                break
            symbol = str(row.get("symbol") or "")
            if any(str(x.get("symbol")) == symbol for x in selected):
                row["eligible"] = False
                row["execution_eligible"] = False
                row["elite_eligible"] = False
                row["rejection_reasons"].append("Duplicate market")
                continue

            effects = self._currency_effect(symbol, str(row.get("direction") or ""))
            prospective = dict(exposures)
            exposure_block = False
            for currency, delta in effects.items():
                prospective[currency] = prospective.get(currency, 0) + delta
                if abs(prospective[currency]) > self.max_currency_exposure:
                    exposure_block = True
            if exposure_block:
                row["eligible"] = False
                row["execution_eligible"] = False
                row["elite_eligible"] = False
                row["rejection_reasons"].append("Currency exposure limit")
                continue

            theme_counts: Dict[str, int] = {}
            for existing in selected:
                for tag in existing.get("exposure_tags") or []:
                    key = str(tag or "").upper().strip()
                    if key:
                        theme_counts[key] = theme_counts.get(key, 0) + 1
            theme_block = None
            for tag in row.get("exposure_tags") or []:
                key = str(tag or "").upper().strip()
                if key and theme_counts.get(key, 0) >= self.max_theme_exposure:
                    theme_block = key
                    break
            if theme_block:
                row["eligible"] = False
                row["execution_eligible"] = False
                row["elite_eligible"] = False
                row["rejection_reasons"].append(f"Theme exposure limit: {theme_block}")
                continue

            correlations = [
                abs(self._correlation(
                    matrix,
                    str(row.get("analysis_symbol") or symbol),
                    str(existing.get("analysis_symbol") or existing.get("symbol") or ""),
                ))
                for existing in selected
            ]
            max_corr = max(correlations) if correlations else 0.0
            if max_corr >= self.high_correlation_abs:
                row["eligible"] = False
                row["elite_eligible"] = False
                row["rejection_reasons"].append(
                    f"Correlation {max_corr:.2f} >= {self.high_correlation_abs:.2f}"
                )
                continue

            diversification_score = max(0.0, min(100.0, 100.0 * (1.0 - max_corr)))
            elite_score = self._safe_float(row.get("elite_base_score"), 0.0) + 0.05 * diversification_score
            row["max_abs_correlation"] = round(max_corr, 4)
            row["diversification_score"] = round(diversification_score, 2)
            row["elite_score"] = round(elite_score, 2)

            # V6.8.11:
            # Selection by REQUIRED_CONFIDENCE must never mutate the evidence
            # classification into ELITE. Elite remains a separate evidence
            # label and is true only when the strict Elite gates passed.
            if bool(row.get("elite_eligible")):
                row["elite_state"] = (
                    "ELITE_A_PLUS"
                    if str(row.get("quality_tier")) == "A+"
                    and elite_score >= 85.0
                    else "ELITE_A"
                )
                row["trade_class"] = "ELITE"
            else:
                row["elite_state"] = "CONFIDENCE_QUALIFIED"
                row["trade_class"] = "CONFIDENCE"

            row["selected"] = True
            selected.append(row)
            exposures = prospective

        screened.sort(
            key=lambda r: (
                bool(r.get("selected")),
                self._safe_float(r.get("elite_score"), 0.0),
                self._safe_float(r.get("elite_base_score"), 0.0),
            ),
            reverse=True,
        )

        now = self._now()
        snapshot = []
        for row in screened:
            clean = dict(row)
            clean["evaluated_at"] = now
            snapshot.append(clean)

        with self._lock:
            self._state["last_candidate_ranking"] = snapshot[:100]
            self._state["last_selection_at"] = now
        self._journal(
            "ELITE_RANKING",
            {
                "evaluated": len(snapshot),
                "selected": len(selected),
                "selection_limit": selection_limit,
                "required_basket_positions":
                    self.required_basket_positions,
                "learning_eligible": sum(
                    1
                    for r in snapshot
                    if r.get("learning_eligible")
                ),
                "selected_symbols": [
                    row.get("symbol")
                    for row in selected
                ],
            },
        )
        return selected

    # ------------------------------------------------------------------
    # Cycle sizing / execution
    # ------------------------------------------------------------------

    @staticmethod
    def _weights(count: int) -> List[float]:
        base = [25.0, 22.0, 20.0, 18.0, 15.0][: max(0, count)]
        total = sum(base)
        if total <= 0:
            return []
        return [value / total for value in base]

    def _deal_size(self, allocation_amount: float) -> float:
        raw = self.reference_deal_size * (allocation_amount / self.reference_capital)
        return max(0.000001, min(self.max_deal_size, raw))

    @staticmethod
    def _median(values: List[float]) -> Optional[float]:
        clean = sorted(
            float(value)
            for value in values
            if value is not None
            and math.isfinite(float(value))
            and float(value) >= 0
        )
        if not clean:
            return None
        middle = len(clean) // 2
        if len(clean) % 2:
            return clean[middle]
        return (clean[middle - 1] + clean[middle]) / 2.0

    def _target_profit_pct(
        self,
        multiple: float,
    ) -> float:
        return max(0.0, float(multiple) - 1.0)

    def _active_win_multiplier(
        self,
        multiple: float,
    ) -> float:
        profit_pct = self._target_profit_pct(multiple)
        return 1.0 + (
            profit_pct * (1.0 - self.harvest_pct)
        )

    def _reserve_fraction_per_win(
        self,
        multiple: float,
    ) -> float:
        return (
            self._target_profit_pct(multiple)
            * self.harvest_pct
        )

    def _recovery_wins_after_losses(
        self,
        multiple: float,
        loss_count: int,
    ) -> Optional[int]:
        loss_count = max(1, int(loss_count))
        loss_multiplier = 1.0 - self.stop_loss_pct
        win_multiplier = self._active_win_multiplier(
            multiple
        )
        if (
            loss_multiplier <= 0
            or loss_multiplier >= 1
            or win_multiplier <= 1
        ):
            return None

        remaining = loss_multiplier ** loss_count
        if remaining <= 0:
            return None

        required_growth = 1.0 / remaining
        return int(
            math.ceil(
                math.log(required_growth)
                / math.log(win_multiplier)
            )
        )

    def _target_break_even_win_probability(
        self,
        multiple: float,
    ) -> Optional[float]:
        """Log-growth break-even probability for active capital.

        Reserve is tracked separately and is not reused by Compound, therefore
        the active-capital break-even correctly uses the post-harvest win
        multiplier against the -15% loss multiplier.
        """
        win_multiplier = self._active_win_multiplier(
            multiple
        )
        loss_multiplier = 1.0 - self.stop_loss_pct
        if win_multiplier <= 1 or not (0 < loss_multiplier < 1):
            return None

        denominator = (
            math.log(win_multiplier)
            - math.log(loss_multiplier)
        )
        if denominator <= 0:
            return None

        value = (
            -math.log(loss_multiplier)
            / denominator
        )
        return max(0.0, min(1.0, value))

    def _cycle_target_multiple(
        self,
        cycle: Dict[str, Any],
    ) -> Optional[float]:
        explicit = cycle.get("target_multiple")
        if explicit is not None:
            try:
                return self._nearest_target_multiple(
                    float(explicit)
                )
            except Exception:
                pass

        # Genuine legacy Compound cycles used target_pct directly. Map them
        # only when the target matches one of the configured alternatives.
        target_pct = cycle.get("target_pct")
        if target_pct is None:
            return None
        try:
            inferred = 1.0 + float(target_pct)
        except Exception:
            return None

        nearest = self._nearest_target_multiple(
            inferred
        )
        if abs(nearest - inferred) <= 0.015:
            return nearest
        return None

    def target_analysis(self) -> Dict[str, Any]:
        """Compare 1.2x / 1.3x / 1.5x on the four requested variables.

        Evidence is genuine completed Compound IG DEMO cycles. Each target's
        probability and duration are learned from cycles that actually used
        that target. Counterfactual MFE is NOT treated as a win because it
        cannot prove ordering versus the stop.
        """
        with self._lock:
            cycles = [
                dict(row)
                for row in (
                    self._state.get("cycles")
                    or []
                )
                if isinstance(row, dict)
            ]

        completed = [
            row
            for row in cycles
            if str(
                row.get("status") or ""
            ).upper()
            == "COMPLETED"
            and str(
                row.get("result") or ""
            ).upper()
            in {"WIN", "LOSS"}
        ]
        completed.sort(
            key=lambda row: self._safe_float(
                row.get("completed_at"),
                0.0,
            ),
            reverse=True,
        )
        completed = completed[
            :self.target_evidence_window
        ]

        grouped: Dict[float, List[Dict[str, Any]]] = {
            float(multiple): []
            for multiple in self.target_multiples
        }

        for cycle in completed:
            multiple = self._cycle_target_multiple(
                cycle
            )
            if multiple is None:
                continue
            grouped.setdefault(
                float(multiple),
                [],
            ).append(cycle)

        rows: List[Dict[str, Any]] = []

        for multiple in self.target_multiples:
            bucket = grouped.get(
                float(multiple),
                [],
            )
            wins = [
                row
                for row in bucket
                if str(
                    row.get("result") or ""
                ).upper()
                == "WIN"
            ]
            losses = [
                row
                for row in bucket
                if str(
                    row.get("result") or ""
                ).upper()
                == "LOSS"
            ]
            settled = len(wins) + len(losses)

            # Beta(1,1) smoothing prevents a tiny 1/1 sample from being treated
            # as a 100% probability.
            smoothed_probability = (
                (len(wins) + 1.0)
                / (settled + 2.0)
            )

            durations = []
            win_durations = []
            loss_durations = []
            for cycle in bucket:
                started = self._safe_float(
                    cycle.get("started_at"),
                    0.0,
                )
                completed_at = self._safe_float(
                    cycle.get("completed_at"),
                    0.0,
                )
                if (
                    started <= 0
                    or completed_at <= started
                ):
                    continue
                hours = (
                    completed_at - started
                ) / 3600.0
                durations.append(hours)
                if str(
                    cycle.get("result") or ""
                ).upper() == "WIN":
                    win_durations.append(hours)
                else:
                    loss_durations.append(hours)

            median_duration = self._median(
                durations
            )
            median_win_duration = self._median(
                win_durations
            )
            median_loss_duration = self._median(
                loss_durations
            )

            active_win_multiplier = (
                self._active_win_multiplier(
                    multiple
                )
            )
            reserve_fraction = (
                self._reserve_fraction_per_win(
                    multiple
                )
            )
            break_even = (
                self._target_break_even_win_probability(
                    multiple
                )
            )

            recovery = {
                str(streak): (
                    self._recovery_wins_after_losses(
                        multiple,
                        streak,
                    )
                )
                for streak in range(1, 6)
            }
            recovery_values = [
                value
                for value in recovery.values()
                if value is not None
            ]
            average_recovery = (
                sum(recovery_values)
                / len(recovery_values)
                if recovery_values
                else None
            )

            expected_reserve_fraction_per_cycle = (
                smoothed_probability
                * reserve_fraction
            )
            reserve_rate_per_hour = (
                expected_reserve_fraction_per_cycle
                / median_duration
                if (
                    median_duration is not None
                    and median_duration > 0
                )
                else None
            )

            expected_active_multiplier = (
                smoothed_probability
                * active_win_multiplier
                + (1.0 - smoothed_probability)
                * (1.0 - self.stop_loss_pct)
            )

            expected_log_growth = (
                smoothed_probability
                * math.log(active_win_multiplier)
                + (1.0 - smoothed_probability)
                * math.log(
                    1.0 - self.stop_loss_pct
                )
            )

            rows.append({
                "target_multiple":
                    round(float(multiple), 4),
                "profit_target_pct":
                    round(
                        self._target_profit_pct(
                            multiple
                        ) * 100.0,
                        2,
                    ),
                "samples": settled,
                "wins": len(wins),
                "losses": len(losses),
                "observed_win_rate_pct": (
                    round(
                        len(wins)
                        / settled
                        * 100.0,
                        2,
                    )
                    if settled
                    else None
                ),
                "smoothed_win_probability_pct":
                    round(
                        smoothed_probability
                        * 100.0,
                        2,
                    ),
                "median_cycle_hours": (
                    round(
                        median_duration,
                        3,
                    )
                    if median_duration is not None
                    else None
                ),
                "median_win_hours": (
                    round(
                        median_win_duration,
                        3,
                    )
                    if median_win_duration is not None
                    else None
                ),
                "median_loss_hours": (
                    round(
                        median_loss_duration,
                        3,
                    )
                    if median_loss_duration is not None
                    else None
                ),
                "active_win_multiplier":
                    round(
                        active_win_multiplier,
                        6,
                    ),
                "active_growth_on_win_pct":
                    round(
                        (
                            active_win_multiplier
                            - 1.0
                        )
                        * 100.0,
                        2,
                    ),
                "reserve_fraction_on_win":
                    round(
                        reserve_fraction,
                        6,
                    ),
                "reserve_on_win_pct_of_start":
                    round(
                        reserve_fraction
                        * 100.0,
                        2,
                    ),
                "expected_reserve_pct_of_start_per_cycle":
                    round(
                        expected_reserve_fraction_per_cycle
                        * 100.0,
                        3,
                    ),
                "expected_reserve_pct_of_start_per_hour":
                    (
                        round(
                            reserve_rate_per_hour
                            * 100.0,
                            4,
                        )
                        if reserve_rate_per_hour
                        is not None
                        else None
                    ),
                "loss_multiplier":
                    round(
                        1.0
                        - self.stop_loss_pct,
                        6,
                    ),
                "recovery_wins_after_losses":
                    recovery,
                "average_recovery_wins_1_to_5_losses":
                    (
                        round(
                            average_recovery,
                            3,
                        )
                        if average_recovery is not None
                        else None
                    ),
                "break_even_win_probability_pct":
                    (
                        round(
                            break_even
                            * 100.0,
                            2,
                        )
                        if break_even is not None
                        else None
                    ),
                "expected_active_multiplier_per_cycle":
                    round(
                        expected_active_multiplier,
                        6,
                    ),
                "expected_log_growth_per_cycle":
                    round(
                        expected_log_growth,
                        6,
                    ),
                "eligible_for_adaptive_selection":
                    settled
                    >= self.target_min_samples_per_target,
            })

        # Score only when every target has enough genuine selected-target
        # cycles. Until then we deliberately explore the least-sampled target.
        enough_all = all(
            int(row.get("samples") or 0)
            >= self.target_min_samples_per_target
            for row in rows
        )

        durations = [
            float(row["median_cycle_hours"])
            for row in rows
            if row.get("median_cycle_hours")
            is not None
            and float(
                row["median_cycle_hours"]
            ) > 0
        ]
        fastest = (
            min(durations)
            if durations
            else None
        )

        recoveries = [
            float(
                row[
                    "average_recovery_wins_1_to_5_losses"
                ]
            )
            for row in rows
            if row.get(
                "average_recovery_wins_1_to_5_losses"
            )
            is not None
            and float(
                row[
                    "average_recovery_wins_1_to_5_losses"
                ]
            )
            > 0
        ]
        best_recovery = (
            min(recoveries)
            if recoveries
            else None
        )

        reserve_rates = [
            float(
                row[
                    "expected_reserve_pct_of_start_per_hour"
                ]
            )
            for row in rows
            if row.get(
                "expected_reserve_pct_of_start_per_hour"
            )
            is not None
            and float(
                row[
                    "expected_reserve_pct_of_start_per_hour"
                ]
            )
            >= 0
        ]
        best_reserve_rate = (
            max(reserve_rates)
            if reserve_rates
            else None
        )

        weight_total = (
            self.target_weight_probability
            + self.target_weight_duration
            + self.target_weight_recovery
            + self.target_weight_reserve
        )
        if weight_total <= 0:
            weight_total = 1.0

        for row in rows:
            probability_score = (
                float(
                    row[
                        "smoothed_win_probability_pct"
                    ]
                )
                / 100.0
            )

            duration = row.get(
                "median_cycle_hours"
            )
            duration_score = (
                min(
                    1.0,
                    fastest / float(duration),
                )
                if (
                    fastest is not None
                    and duration is not None
                    and float(duration) > 0
                )
                else 0.0
            )

            recovery_value = row.get(
                "average_recovery_wins_1_to_5_losses"
            )
            recovery_score = (
                min(
                    1.0,
                    best_recovery
                    / float(recovery_value),
                )
                if (
                    best_recovery is not None
                    and recovery_value is not None
                    and float(recovery_value) > 0
                )
                else 0.0
            )

            reserve_rate = row.get(
                "expected_reserve_pct_of_start_per_hour"
            )
            reserve_score = (
                min(
                    1.0,
                    float(reserve_rate)
                    / best_reserve_rate,
                )
                if (
                    best_reserve_rate is not None
                    and best_reserve_rate > 0
                    and reserve_rate is not None
                )
                else 0.0
            )

            composite = (
                probability_score
                * self.target_weight_probability
                + duration_score
                * self.target_weight_duration
                + recovery_score
                * self.target_weight_recovery
                + reserve_score
                * self.target_weight_reserve
            ) / weight_total

            row["score_components"] = {
                "win_probability":
                    round(
                        probability_score
                        * 100.0,
                        2,
                    ),
                "cycle_duration":
                    round(
                        duration_score
                        * 100.0,
                        2,
                    ),
                "loss_recovery":
                    round(
                        recovery_score
                        * 100.0,
                        2,
                    ),
                "harvested_reserve":
                    round(
                        reserve_score
                        * 100.0,
                        2,
                    ),
            }
            row["composite_score"] = (
                round(
                    composite * 100.0,
                    2,
                )
            )

        if self.target_mode == "FIXED":
            selected = self.default_target_multiple
            selection_state = "FIXED"
            selection_reason = (
                "COMPOUND_TARGET_MODE=FIXED; using configured "
                f"{selected:.2f}x target."
            )
        elif enough_all:
            best = max(
                rows,
                key=lambda row: (
                    float(
                        row.get(
                            "composite_score"
                        )
                        or 0.0
                    ),
                    # Tie preference goes to the middle target because it
                    # preserves faster recovery without requiring 1.5x.
                    -abs(
                        float(
                            row[
                                "target_multiple"
                            ]
                        )
                        - 1.3
                    ),
                ),
            )
            selected = float(
                best["target_multiple"]
            )
            selection_state = "ADAPTIVE"
            selection_reason = (
                "All target candidates have the minimum genuine IG DEMO "
                "sample; selected the highest four-variable composite score."
            )
        else:
            # Genuine exploration is required to learn probability and duration
            # for each target. Prefer 1.3x on ties, then the other least-sampled
            # target. Current open cycles are never changed mid-cycle.
            preference = {
                self.default_target_multiple: 0,
                1.3: 1,
                1.2: 2,
                1.5: 3,
            }
            least = min(
                int(row.get("samples") or 0)
                for row in rows
            )
            candidates = [
                row
                for row in rows
                if int(
                    row.get("samples") or 0
                ) == least
            ]
            chosen = min(
                candidates,
                key=lambda row: preference.get(
                    float(
                        row[
                            "target_multiple"
                        ]
                    ),
                    99,
                ),
            )
            selected = float(
                chosen["target_multiple"]
            )
            selection_state = "EXPLORATION"
            selection_reason = (
                "Not all target candidates have enough genuine selected-target "
                "cycles. The next cycle uses the least-sampled target so the "
                "system can learn real win probability and duration rather "
                "than infer them from counterfactual MFE."
            )

        return {
            "version": self.VERSION,
            "mode": self.target_mode,
            "selection_state":
                selection_state,
            "selected_target_multiple":
                round(selected, 4),
            "selected_profit_target_pct":
                round(
                    (
                        selected
                        - 1.0
                    )
                    * 100.0,
                    2,
                ),
            "selection_reason":
                selection_reason,
            "current_cycle_unchanged":
                True,
            "candidates":
                rows,
            "weights": {
                "win_probability":
                    self.target_weight_probability,
                "cycle_duration":
                    self.target_weight_duration,
                "loss_recovery":
                    self.target_weight_recovery,
                "harvested_reserve":
                    self.target_weight_reserve,
            },
            "minimum_samples_per_target":
                self.target_min_samples_per_target,
            "evidence_window":
                self.target_evidence_window,
            "all_targets_minimum_evidence":
                enough_all,
            "stop_loss_pct":
                round(
                    self.stop_loss_pct
                    * 100.0,
                    2,
                ),
            "harvest_model":
                "20_PERCENT_OF_POSITIVE_PROFIT_BY_DEFAULT",
            "harvest_pct_of_profit":
                round(
                    self.harvest_pct
                    * 100.0,
                    2,
                ),
            "reserve_reused":
                False,
            "probability_source":
                "GENUINE_COMPLETED_IG_DEMO_COMPOUND_CYCLES",
            "duration_source":
                "ACTUAL_SELECTED_TARGET_CYCLE_DURATION",
            "counterfactual_mfe_used_as_win":
                False,
        }

    def _select_target_plan(
        self,
    ) -> Dict[str, Any]:
        analysis = self.target_analysis()
        multiple = float(
            analysis.get(
                "selected_target_multiple"
            )
            or self.default_target_multiple
        )
        return {
            "target_multiple":
                multiple,
            "profit_target_pct":
                self._target_profit_pct(
                    multiple
                ),
            "selection_state":
                analysis.get(
                    "selection_state"
                ),
            "selection_reason":
                analysis.get(
                    "selection_reason"
                ),
            "analysis_snapshot":
                analysis,
        }

    def _new_deal_reference(self, cycle_number: int, slot: int) -> str:
        token = uuid.uuid4().hex[:12].upper()
        return f"{self.DEAL_PREFIX}{cycle_number:03d}{slot:02d}_{token}"[:30]

    def _open_cycle(self, account: Dict[str, Any], positions: List[Dict[str, Any]]) -> Dict[str, Any]:
        with self._lock:
            capital = self._safe_float(self._state.get("current_capital"), 0.0)
            enabled = bool(self._state.get("enabled"))
            next_cycle_at = self._safe_float(self._state.get("next_cycle_at"), 0.0)

        if not enabled:
            return self.status()
        if capital < self.min_cycle_capital:
            with self._lock:
                self._state["status"] = "PAUSED_CAPITAL_TOO_LOW"
                self._state["paused_reason"] = "Current compound capital is below the configured minimum."
                self._persist()
            return self.status()
        if next_cycle_at and self._now() < next_cycle_at:
            with self._lock:
                self._state["status"] = "COOLDOWN"
            return self.status()

        account_balance = self._safe_float(account.get("balance"), 0.0)
        reserve = self._safe_float(self._state.get("reserve_balance"), 0.0)
        deployable = max(0.0, account_balance - reserve)
        if capital > deployable + 1e-9:
            with self._lock:
                self._state["status"] = "PAUSED_INSUFFICIENT_DEPLOYABLE_FUNDS"
                self._state["paused_reason"] = (
                    f"Cycle capital {capital:.2f} exceeds broker balance minus protected reserve ({deployable:.2f})."
                )
                self._state["pending_elite_candidates"] = []
                self._state["intelligence_bridge_state"] = "PAUSED_FUNDS"
                self._persist()
            return self.status()

        # V6.7.1: ALWAYS evaluate intelligence before the clean-broker veto.
        # This is the missing link from V6.7.1: Live Intelligence and the
        # Compound screen now share the same decision trail even when existing
        # legacy/manual broker positions temporarily block execution.
        selected = self._rank_candidates(
            capital,
            selection_limit=self.candidate_pool_size,
        )

        external = self._external_positions(positions)
        learning_positions = self._learning_positions(positions)

        # Learning positions are Jasong-owned and are allowed to coexist with
        # Compound. Only genuinely external/manual broker positions block.
        selected, duplicate_skips = (
            self._remove_learning_duplicate_exposure(
                selected,
                learning_positions,
            )
        )

        capacity = self._compound_open_capacity(
            positions
        )

        if (
            capacity["compound_slots_available"]
            < self.required_basket_positions
        ):
            with self._lock:
                self._state["status"] = (
                    "WAITING_FOR_BROKER_CAPACITY"
                )
                self._state["paused_reason"] = (
                    "Compound requires five free IG DEMO basket slots "
                    "before a new cycle can begin. "
                    f"Compound {capacity['compound_open']}/"
                    f"{capacity['compound_max']}; "
                    f"all broker positions {capacity['total_open']}/"
                    f"{capacity['global_max']}; "
                    f"available Compound slots "
                    f"{capacity['compound_slots_available']}/"
                    f"{self.required_basket_positions}."
                )
                self._state[
                    "broker_capacity"
                ] = capacity
                self._persist()
            return self.status()

        if duplicate_skips:
            with self._lock:
                ranking = [
                    dict(row)
                    for row in (
                        self._state.get(
                            "last_candidate_ranking"
                        )
                        or []
                    )
                ]
                by_key = {
                    str(
                        row.get("key")
                        or row.get("symbol")
                        or row.get("market")
                        or ""
                    ): row
                    for row in duplicate_skips
                }
                for index, row in enumerate(ranking):
                    key = str(
                        row.get("key")
                        or row.get("symbol")
                        or row.get("market")
                        or ""
                    )
                    if key in by_key:
                        ranking[index] = dict(
                            by_key[key]
                        )
                self._state[
                    "last_candidate_ranking"
                ] = ranking
                self._state[
                    "last_duplicate_learning_exposure"
                ] = duplicate_skips

        if (
            len(selected)
            < self.required_basket_positions
        ):
            with self._lock:
                self._state["status"] = (
                    "WAITING_FOR_5_ELIGIBLE_MARKETS"
                )
                self._state["paused_reason"] = (
                    "Compound is continuously reassessing markets every "
                    f"{self.poll_seconds}s and will only start the next "
                    "target cycle when five eligible, diversified IG DEMO "
                    "markets are simultaneously available. "
                    f"Current qualified queue: {len(selected)}/"
                    f"{self.required_basket_positions}."
                )
                self._state[
                    "pending_elite_candidates"
                ] = [
                    dict(row)
                    for row in selected
                ]
                self._state[
                    "intelligence_bridge_state"
                ] = "WAITING_FOR_5_ELIGIBLE_MARKETS"
                self._state[
                    "basket_assembly"
                ] = {
                    "required":
                        self.required_basket_positions,
                    "eligible_now":
                        len(selected),
                    "candidate_pool_size":
                        self.candidate_pool_size,
                    "last_checked_at":
                        self._now(),
                    "refresh_seconds":
                        self.poll_seconds,
                    "top_candidates": [
                        {
                            "symbol":
                                row.get("symbol"),
                            "market":
                                row.get("market"),
                            "direction":
                                row.get("direction"),
                            "trade_class":
                                row.get("trade_class"),
                            "ai_pct": round(
                                self._safe_float(
                                    row.get(
                                        "model_ai_confidence"
                                    ),
                                    0.0,
                                )
                                * 100.0,
                                2,
                            ),
                            "quant_pct": round(
                                self._safe_float(
                                    row.get(
                                        "quant_confidence"
                                    ),
                                    0.0,
                                )
                                * 100.0,
                                2,
                            ),
                            "fast_score":
                                row.get(
                                    "smart_fast_score"
                                ),
                            "opportunity_score":
                                row.get(
                                    "opportunity_score"
                                ),
                        }
                        for row in selected[
                            :self.required_basket_positions
                        ]
                    ],
                }
                self._persist()
            return self.status()

        if external:
            blocker_names = [
                str(
                    row.get("symbol")
                    or row.get("epic")
                    or row.get("deal_reference")
                    or "IG position"
                )
                for row in external
            ]
            with self._lock:
                self._state["status"] = "WAITING_FOR_CLEAN_BROKER"
                self._state["last_foreign_blockers"] = [
                    dict(row)
                    for row in external
                ]
                self._state["pending_elite_candidates"] = [
                    dict(row)
                    for row in selected
                ]
                self._state["intelligence_bridge_state"] = (
                    "CONFIDENCE_READY_BROKER_BLOCKED"
                    if selected
                    else "BROKER_BLOCKED_NO_CONFIDENCE"
                )
                self._state["paused_reason"] = (
                    f"{len(external)} external/manual IG DEMO position(s) block "
                    "Compound execution because IG account P&L is account-wide. "
                    + (
                        f"{len(selected)} confidence-qualified setup(s) are ready and will be "
                        "revalidated automatically when the broker becomes clean. "
                        if selected
                        else
                        "Live Intelligence is still being evaluated while the broker drains. "
                    )
                    + "Blockers: "
                    + ", ".join(blocker_names[:6])
                )
                self._persist()
            return self.status()

        with self._lock:
            self._state["last_foreign_blockers"] = []

        if not selected:
            with self._lock:
                self._state["status"] = (
                    "WAITING_FOR_NON_DUPLICATE_CONFIDENCE"
                    if duplicate_skips
                    else "WAITING_FOR_REQUIRED_CONFIDENCE"
                )
                self._state["paused_reason"] = (
                    (
                        "Confidence-qualified setup(s) exist, but each currently "
                        "duplicates a Jasong Learning IG DEMO market exposure. "
                        "Compound will revalidate automatically when a distinct "
                        "market becomes available. "
                    )
                    if duplicate_skips
                    else
                    "No market currently passes all required confidence and broker-safety gates. "
                ) + (
                    ""
                    if duplicate_skips
                    else
                    "Unified Live Intelligence remains connected and will wake "
                    "the Compound engine when a new directional setup arrives."
                )
                self._state["pending_elite_candidates"] = []
                self._state["intelligence_bridge_state"] = "LISTENING_FOR_REQUIRED_CONFIDENCE"
                self._persist()
            return self.status()

        with self._lock:
            self._state["pending_elite_candidates"] = [
                dict(row)
                for row in selected
            ]
            self._state["basket_assembly"] = {
                "required":
                    self.required_basket_positions,
                "eligible_now":
                    len(selected),
                "candidate_pool_size":
                    self.candidate_pool_size,
                "primary_symbols": [
                    row.get("symbol")
                    for row in selected[
                        :self.required_basket_positions
                    ]
                ],
                "alternate_symbols": [
                    row.get("symbol")
                    for row in selected[
                        self.required_basket_positions:
                    ]
                ],
                "last_checked_at":
                    self._now(),
                "refresh_seconds":
                    self.poll_seconds,
            }
            self._state["intelligence_bridge_state"] = (
                "FIVE_MARKETS_READY"
            )

        target_plan = self._select_target_plan()
        selected_target_multiple = self._safe_float(
            target_plan.get("target_multiple"),
            self.default_target_multiple,
        )
        selected_target_pct = self._safe_float(
            target_plan.get("profit_target_pct"),
            self.profit_target_pct,
        )

        with self._lock:
            cycle_number = int(self._state.get("cycle_number") or 0) + 1
            cycle = {
                "cycle_id": str(uuid.uuid4()),
                "campaign_id": self._state.get("campaign_id"),
                "cycle_number": cycle_number,
                "status": "OPENING",
                "started_at": self._now(),
                "completed_at": None,
                "starting_capital": round(capital, 8),
                "target_multiple": round(
                    selected_target_multiple,
                    4,
                ),
                "target_pct": round(
                    selected_target_pct,
                    8,
                ),
                "target_profit": round(
                    capital
                    * selected_target_pct,
                    8,
                ),
                "target_selection_state":
                    target_plan.get(
                        "selection_state"
                    ),
                "target_selection_reason":
                    target_plan.get(
                        "selection_reason"
                    ),
                "target_analysis_at_open":
                    target_plan.get(
                        "analysis_snapshot"
                    ),
                "stop_pct": self.stop_loss_pct,
                "stop_loss_amount": round(capital * self.stop_loss_pct, 8),
                "harvest_pct": self.harvest_pct,
                "broker_balance_before": account_balance,
                "broker_currency": account.get("currency"),
                "learning_positions_at_start": [
                    dict(row)
                    for row in learning_positions
                ],
                "learning_positions_at_start_count": len(
                    learning_positions
                ),
                "pnl_isolation_mode": (
                    "ISOLATED_JSCMP_MARK_TO_MARKET"
                    if learning_positions
                    else "IG_ACCOUNT_PNL_CLEAN"
                ),
                "broker_capacity_at_start": capacity,
                "positions": [],
                "required_basket_positions":
                    self.required_basket_positions,
                "candidate_queue": [
                    dict(row)
                    for row in selected
                ],
                "selected_candidates": [
                    dict(row)
                    for row in selected[
                        :self.required_basket_positions
                    ]
                ],
                "running_pnl": 0.0,
                "peak_pnl": 0.0,
                "trough_pnl": 0.0,
                "basket_mfe_pnl": 0.0,
                "basket_mae_pnl": 0.0,
                "basket_mfe_pct": 0.0,
                "basket_mae_pct": 0.0,
                "exit_reason": None,
                "realised_profit": None,
                "harvested_profit": 0.0,
                "compounded_profit": 0.0,
                "next_cycle_capital": None,
            }
            self._state["cycle_number"] = cycle_number
            self._state["current_cycle"] = cycle
            self._state["status"] = "OPENING_BASKET"
            self._state["paused_reason"] = None
            self._persist()

        weights = self._weights(
            self.required_basket_positions
        )
        opened: List[Dict[str, Any]] = []
        opened_candidates: List[Dict[str, Any]] = []
        errors: List[str] = []
        attempted_symbols: List[str] = []

        for candidate in selected:
            if (
                len(opened)
                >= self.required_basket_positions
            ):
                break

            slot = len(opened) + 1
            weight = weights[slot - 1]
            allocation = capital * weight
            requested_size = self._deal_size(
                allocation
            )
            ref = self._new_deal_reference(
                cycle_number,
                slot,
            )
            attempted_symbols.append(
                str(
                    candidate.get("symbol")
                    or candidate.get("market")
                    or ""
                )
            )

            try:
                if candidate.get("ig_epic"):
                    result = (
                        self.broker.open_epic_position(
                            epic=str(
                                candidate.get(
                                    "ig_epic"
                                )
                                or ""
                            ),
                            direction=str(
                                candidate.get(
                                    "direction"
                                )
                                or ""
                            ).upper(),
                            size=requested_size,
                            deal_reference=ref,
                        )
                    )
                else:
                    result = (
                        self.broker.open_market_position(
                            symbol=str(
                                candidate.get(
                                    "symbol"
                                )
                                or candidate.get(
                                    "market"
                                )
                                or ""
                            ),
                            direction=str(
                                candidate.get(
                                    "direction"
                                )
                                or ""
                            ).upper(),
                            size=requested_size,
                            deal_reference=ref,
                        )
                    )

                # A Compound slot exists only after IG supplies a real accepted
                # deal ID. Anything else is treated as a failed candidate and
                # the next ranked alternate is tried.
                deal_id = str(
                    result.get("dealId")
                    or ""
                ).strip()
                deal_status = str(
                    result.get("dealStatus")
                    or result.get("status")
                    or ""
                ).upper()

                if (
                    not deal_id
                    or deal_status
                    not in {
                        "ACCEPTED",
                        "OPEN",
                    }
                ):
                    raise RuntimeError(
                        "IG did not return an accepted deal ID"
                    )

                row = {
                    "slot": slot,
                    "symbol":
                        candidate.get("symbol"),
                    "market":
                        candidate.get("market"),
                    "asset_class":
                        candidate.get(
                            "asset_class"
                        )
                        or "FX",
                    "analysis_symbol":
                        candidate.get(
                            "analysis_symbol"
                        ),
                    "exposure_tags": list(
                        candidate.get(
                            "exposure_tags"
                        )
                        or []
                    ),
                    "direction":
                        candidate.get("direction"),
                    "elite_score":
                        candidate.get(
                            "elite_score"
                        ),
                    "elite_state":
                        candidate.get(
                            "elite_state"
                        ),
                    "trade_class":
                        candidate.get(
                            "trade_class"
                        ),
                    "execution_basis":
                        candidate.get(
                            "execution_basis"
                        ),
                    "confidence_qualified":
                        candidate.get(
                            "confidence_qualified"
                        ),
                    "model_ai_confidence":
                        candidate.get(
                            "model_ai_confidence"
                        ),
                    "quant_confidence":
                        candidate.get(
                            "quant_confidence"
                        ),
                    "smart_fast_score":
                        candidate.get(
                            "smart_fast_score"
                        ),
                    "quality_tier":
                        candidate.get(
                            "quality_tier"
                        ),
                    "deep_status":
                        candidate.get(
                            "deep_status"
                        ),
                    "spread_bps":
                        candidate.get(
                            "spread_bps"
                        ),
                    "opportunity_score":
                        candidate.get(
                            "opportunity_score"
                        ),
                    "opportunity_age_seconds":
                        candidate.get(
                            "opportunity_age_seconds"
                        ),
                    "intelligence_source":
                        candidate.get(
                            "intelligence_source"
                        ),
                    "intelligence_age_seconds":
                        candidate.get(
                            "intelligence_age_seconds"
                        ),
                    "source_rank":
                        candidate.get(
                            "source_rank"
                        ),
                    "live_price_at_selection":
                        candidate.get(
                            "live_price"
                        ),
                    "entry_rsi":
                        candidate.get("rsi"),
                    "signal_reason":
                        candidate.get(
                            "signal_reason"
                        )
                        or candidate.get(
                            "reason"
                        ),
                    "historical_win_rate":
                        candidate.get(
                            "historical_win_rate"
                        ),
                    "historical_profit_factor":
                        candidate.get(
                            "historical_profit_factor"
                        ),
                    "historical_trades":
                        candidate.get(
                            "historical_trades"
                        ),
                    "max_abs_correlation":
                        candidate.get(
                            "max_abs_correlation"
                        ),
                    "diversification_score":
                        candidate.get(
                            "diversification_score"
                        ),
                    "weight_pct":
                        round(
                            weight * 100.0,
                            4,
                        ),
                    "allocation_amount":
                        round(
                            allocation,
                            8,
                        ),
                    "requested_size":
                        requested_size,
                    "ig_size":
                        result.get("size"),
                    "ig_deal_id":
                        deal_id,
                    "ig_deal_reference":
                        result.get(
                            "dealReference"
                        )
                        or ref,
                    "ig_epic":
                        result.get("epic"),
                    "entry_level":
                        result.get("level"),
                    "broker_status": "OPEN",
                    "opened_at": self._now(),
                    "open_result": result,
                    "current_move_bps": 0.0,
                    "mfe_bps": 0.0,
                    "mae_bps": 0.0,
                    "close_attempts": 0,
                    "close_verified": False,
                    "last_close_error": None,
                }

                opened.append(row)
                opened_candidates.append(
                    dict(candidate)
                )

                with self._lock:
                    current = dict(
                        self._state.get(
                            "current_cycle"
                        )
                        or {}
                    )
                    current[
                        "positions"
                    ] = [
                        dict(item)
                        for item in opened
                    ]
                    current[
                        "selected_candidates"
                    ] = [
                        dict(item)
                        for item in opened_candidates
                    ]
                    current[
                        "opening_progress"
                    ] = {
                        "accepted":
                            len(opened),
                        "required":
                            self.required_basket_positions,
                        "attempted_symbols":
                            list(
                                attempted_symbols
                            ),
                        "errors":
                            list(errors),
                    }
                    self._state[
                        "current_cycle"
                    ] = current
                    self._persist()

            except Exception as exc:
                message = (
                    f"{candidate.get('symbol')}: "
                    f"{type(exc).__name__}: {exc}"
                )
                errors.append(message)
                self._journal(
                    "COMPOUND_OPEN_ERROR",
                    {
                        "message": message,
                        "slot": slot,
                        "attempted_symbol":
                            candidate.get("symbol"),
                        "fallback_available":
                            len(selected)
                            > len(
                                attempted_symbols
                            ),
                    },
                )

        if (
            len(opened)
            < self.required_basket_positions
        ):
            rollback_results: List[
                Dict[str, Any]
            ] = []

            if opened:
                try:
                    rollback_results = (
                        self._close_compound_positions(
                            opened
                        )
                    )
                except Exception as exc:
                    errors.append(
                        "rollback: "
                        f"{type(exc).__name__}: {exc}"
                    )

            with self._lock:
                cycle = dict(
                    self._state.get(
                        "current_cycle"
                    )
                    or {}
                )
                cycle["status"] = (
                    "OPEN_INCOMPLETE_ROLLBACK"
                )
                cycle["completed_at"] = self._now()
                cycle["open_errors"] = list(errors)
                cycle["opening_progress"] = {
                    "accepted":
                        len(opened),
                    "required":
                        self.required_basket_positions,
                    "attempted_symbols":
                        list(attempted_symbols),
                    "rollback_results":
                        rollback_results,
                }

                cycles = list(
                    self._state.get("cycles")
                    or []
                )
                cycles.append(cycle)
                self._state["cycles"] = (
                    cycles[-1000:]
                )
                self._state["current_cycle"] = (
                    None
                )
                self._state["status"] = (
                    "WAITING_FOR_5_ELIGIBLE_MARKETS"
                )
                self._state["paused_reason"] = (
                    "IG accepted fewer than five "
                    "Compound positions after trying "
                    "the ranked alternates. Any partial "
                    "basket was rolled back; the engine "
                    "will reassess again on the next "
                    f"{self.poll_seconds}-second cycle."
                )
                self._state[
                    "last_error"
                ] = (
                    "; ".join(errors)
                    if errors
                    else (
                        "Could not assemble five "
                        "accepted IG DEMO positions"
                    )
                )
                self._state[
                    "next_cycle_at"
                ] = (
                    self._now()
                    + self.poll_seconds
                )
                self._state[
                    "intelligence_bridge_state"
                ] = "FIVE_MARKET_ASSEMBLY_FAILED"
                self._persist()

            return self.status()

        with self._lock:
            cycle = dict(self._state.get("current_cycle") or {})
            cycle["status"] = "ACTIVE"
            cycle["positions"] = opened
            cycle["selected_candidates"] = [
                dict(row)
                for row in opened_candidates
            ]
            cycle["open_errors"] = errors
            cycle["basket_opened_full"] = (
                len(opened)
                == self.required_basket_positions
            )
            cycle["basket_opened_count"] = len(
                opened
            )
            self._state["current_cycle"] = cycle
            self._state["status"] = "ACTIVE"
            self._state["paused_reason"] = None
            self._state["pending_elite_candidates"] = []
            self._state["intelligence_bridge_state"] = "BASKET_ACTIVE"
            self._persist()
        self._journal(
            "COMPOUND_CYCLE_OPENED",
            {
                "cycle_number": cycle_number,
                "capital": capital,
                "positions": len(opened),
                "required_positions":
                    self.required_basket_positions,
                "basket_opened_full":
                    len(opened)
                    == self.required_basket_positions,
                "symbols": [
                    row.get("symbol")
                    for row in opened
                ],
                "execution_basis": "REQUIRED_CONFIDENCE",
                "elite_required": False,
            },
        )

        # Refresh broker truth once after opening so the mobile app does not
        # temporarily show 0/5 until the next 15-second monitoring tick.
        try:
            self._broker_snapshot()
        except Exception:
            pass

        return self.status()

    def _close_compound_positions(
        self,
        positions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []

        for position in positions:
            deal_id = str(
                position.get("deal_id")
                or position.get("ig_deal_id")
                or ""
            ).strip()
            if not deal_id:
                continue

            market_status = str(
                position.get("market_status")
                or position.get("broker_market_status")
                or ""
            ).upper().strip()

            if (
                market_status
                and market_status
                not in self.CLOSE_ALLOWED_MARKET_STATUSES
            ):
                results.append(
                    {
                        "deal_id": deal_id,
                        "ok": True,
                        "verified": False,
                        "pending": False,
                        "deferred": True,
                        "market_status": market_status,
                        "status": "CLOSE_DEFERRED_MARKET_CLOSED",
                    }
                )
                with self._lock:
                    cycle = dict(
                        self._state.get("current_cycle")
                        or {}
                    )
                    stored_positions = [
                        dict(item)
                        for item in (
                            cycle.get("positions")
                            or []
                        )
                        if isinstance(item, dict)
                    ]
                    for stored in stored_positions:
                        stored_id = str(
                            stored.get("deal_id")
                            or stored.get("ig_deal_id")
                            or ""
                        )
                        if stored_id != deal_id:
                            continue
                        stored["close_execution_state"] = (
                            "CLOSE_DEFERRED_MARKET_CLOSED"
                        )
                        stored["broker_market_status"] = (
                            market_status
                        )
                        stored["close_deferred_at"] = self._now()
                        stored["close_deferred_reason"] = (
                            f"IG market status is {market_status}"
                        )
                        stored["last_close_error"] = None
                    cycle["positions"] = stored_positions
                    self._state["current_cycle"] = cycle
                    self._persist()
                continue

            with self._lock:
                close_integrity = dict(
                    self._state.get("close_integrity")
                    or {}
                )
                close_integrity["attempts"] = int(
                    close_integrity.get("attempts")
                    or 0
                ) + 1
                self._state["close_integrity"] = close_integrity
                self._persist()

            try:
                result = self.broker.close_position(
                    deal_id
                )
                if bool(
                    result.get("closeDeferred")
                ):
                    market_status = str(
                        result.get("marketStatus")
                        or market_status
                        or ""
                    ).upper().strip()
                    results.append(
                        {
                            "deal_id": deal_id,
                            "ok": True,
                            "verified": False,
                            "pending": False,
                            "deferred": True,
                            "market_status": market_status,
                            "status": "CLOSE_DEFERRED_MARKET_CLOSED",
                            "result": result,
                        }
                    )
                    with self._lock:
                        cycle = dict(
                            self._state.get("current_cycle")
                            or {}
                        )
                        stored_positions = [
                            dict(item)
                            for item in (
                                cycle.get("positions")
                                or []
                            )
                            if isinstance(item, dict)
                        ]
                        for stored in stored_positions:
                            stored_id = str(
                                stored.get("deal_id")
                                or stored.get("ig_deal_id")
                                or ""
                            )
                            if stored_id != deal_id:
                                continue
                            stored["close_execution_state"] = (
                                "CLOSE_DEFERRED_MARKET_CLOSED"
                            )
                            stored["broker_market_status"] = (
                                market_status
                            )
                            stored["close_deferred_at"] = self._now()
                            stored["close_deferred_reason"] = (
                                result.get("deferredReason")
                                or (
                                    "IG market is not currently open "
                                    "for position closing"
                                )
                            )
                            stored["last_close_error"] = None
                        cycle["positions"] = stored_positions
                        self._state["current_cycle"] = cycle
                        self._persist()
                    continue

                verified = bool(
                    result.get("closeVerified")
                )
                pending = (
                    not verified
                    and str(
                        result.get("status")
                        or ""
                    ).upper()
                    == "CLOSE_PENDING"
                )

                results.append(
                    {
                        "deal_id": deal_id,
                        "ok": True,
                        "verified": verified,
                        "pending": pending,
                        "requested_close_size": result.get(
                            "requestedCloseSize"
                        ),
                        "remaining_size": result.get(
                            "remainingSize"
                        ),
                        "result": result,
                    }
                )

                with self._lock:
                    close_integrity = dict(
                        self._state.get("close_integrity")
                        or {}
                    )
                    if verified:
                        close_integrity["verified"] = int(
                            close_integrity.get("verified")
                            or 0
                        ) + 1
                    elif pending:
                        close_integrity["pending"] = int(
                            close_integrity.get("pending")
                            or 0
                        ) + 1
                    close_integrity["last_error"] = None
                    self._state["close_integrity"] = close_integrity

                    cycle = dict(
                        self._state.get("current_cycle")
                        or {}
                    )
                    stored_positions = [
                        dict(item)
                        for item in (
                            cycle.get("positions")
                            or []
                        )
                        if isinstance(item, dict)
                    ]
                    for stored in stored_positions:
                        stored_id = str(
                            stored.get("deal_id")
                            or stored.get("ig_deal_id")
                            or ""
                        )
                        if stored_id != deal_id:
                            continue
                        stored["close_attempts"] = int(
                            stored.get("close_attempts")
                            or 0
                        ) + 1
                        stored["close_verified"] = verified
                        stored["broker_status"] = (
                            "CLOSED"
                            if verified
                            else "CLOSE_PENDING"
                        )
                        stored["remaining_size"] = result.get(
                            "remainingSize"
                        )
                        stored["close_result"] = result
                        stored["last_close_error"] = None
                    cycle["positions"] = stored_positions
                    self._state["current_cycle"] = cycle
                    self._persist()

            except Exception as exc:
                error_text = (
                    f"{type(exc).__name__}: {exc}"
                )
                results.append(
                    {
                        "deal_id": deal_id,
                        "ok": False,
                        "verified": False,
                        "pending": False,
                        "error": error_text,
                    }
                )

                with self._lock:
                    close_integrity = dict(
                        self._state.get("close_integrity")
                        or {}
                    )
                    close_integrity["errors"] = int(
                        close_integrity.get("errors")
                        or 0
                    ) + 1
                    close_integrity["last_error"] = error_text
                    self._state["close_integrity"] = close_integrity

                    cycle = dict(
                        self._state.get("current_cycle")
                        or {}
                    )
                    stored_positions = [
                        dict(item)
                        for item in (
                            cycle.get("positions")
                            or []
                        )
                        if isinstance(item, dict)
                    ]
                    for stored in stored_positions:
                        stored_id = str(
                            stored.get("deal_id")
                            or stored.get("ig_deal_id")
                            or ""
                        )
                        if stored_id != deal_id:
                            continue
                        stored["close_attempts"] = int(
                            stored.get("close_attempts")
                            or 0
                        ) + 1
                        stored["close_verified"] = False
                        stored["last_close_error"] = error_text
                    cycle["positions"] = stored_positions
                    self._state["current_cycle"] = cycle
                    self._persist()

                self._journal(
                    "COMPOUND_CLOSE_ERROR",
                    {
                        "deal_id": deal_id,
                        "error": error_text,
                    },
                )

        return results

    def _finalise_cycle(self, reason: str, trigger_pnl: float, *, invalid: bool = False) -> Dict[str, Any]:
        with self._lock:
            cycle = dict(self._state.get("current_cycle") or {})
        if not cycle:
            return self.status()

        try:
            account_after, positions_after = self._broker_snapshot()
        except Exception:
            account_after = dict(self._state.get("broker_account") or {})
            positions_after = list(self._state.get("broker_positions") or [])

        remaining = self._compound_positions(positions_after)
        if remaining:
            # Let the next tick retry any failed closes. Do not account a cycle
            # until all of its broker positions are gone.
            with self._lock:
                cycle["status"] = "CLOSING"
                cycle["exit_reason"] = reason
                cycle["trigger_pnl"] = trigger_pnl
                self._state["current_cycle"] = cycle
                self._state["status"] = "CLOSING_BASKET"
                self._persist()
            return self.status()

        balance_before = self._safe_float(cycle.get("broker_balance_before"), 0.0)
        balance_after = self._safe_float(account_after.get("balance"), balance_before)
        realised = balance_after - balance_before
        if not math.isfinite(realised):
            realised = trigger_pnl

        capital = self._safe_float(cycle.get("starting_capital"), 0.0)
        reserve_before = self._safe_float(self._state.get("reserve_balance"), 0.0)
        cycle_harvest_pct = self._safe_float(
            cycle.get("harvest_pct"),
            self.harvest_pct,
        )

        if invalid:
            harvested = 0.0
            compounded_profit = 0.0
            next_capital = capital
            result_label = "INVALID"
        else:
            positive_profit = max(0.0, realised)
            harvested = positive_profit * cycle_harvest_pct
            compounded_profit = positive_profit - harvested
            if realised >= 0:
                next_capital = capital + compounded_profit
            else:
                next_capital = max(0.0, capital + realised)
            result_label = "WIN" if realised > 0 else ("LOSS" if realised < 0 else "FLAT")

        cycle.update(
            {
                "status": "COMPLETED" if not invalid else "INVALID",
                "completed_at": self._now(),
                "exit_reason": reason,
                "trigger_pnl": round(trigger_pnl, 8),
                "broker_balance_after": balance_after,
                "realised_profit": round(realised, 8),
                "harvested_profit": round(harvested, 8),
                "compounded_profit": round(compounded_profit, 8),
                "next_cycle_capital": round(next_capital, 8),
                "result": result_label,
                "basket_mfe_pnl": round(
                    self._safe_float(
                        cycle.get("peak_pnl"),
                        0.0,
                    ),
                    8,
                ),
                "basket_mae_pnl": round(
                    self._safe_float(
                        cycle.get("trough_pnl"),
                        0.0,
                    ),
                    8,
                ),
                "basket_mfe_pct": (
                    round(
                        self._safe_float(
                            cycle.get("peak_pnl"),
                            0.0,
                        )
                        / capital
                        * 100.0,
                        4,
                    )
                    if capital > 0
                    else 0.0
                ),
                "basket_mae_pct": (
                    round(
                        self._safe_float(
                            cycle.get("trough_pnl"),
                            0.0,
                        )
                        / capital
                        * 100.0,
                        4,
                    )
                    if capital > 0
                    else 0.0
                ),
                "evidence_complete": all(
                    bool(
                        str(
                            row.get("ig_deal_id")
                            or row.get("deal_id")
                            or ""
                        )
                    )
                    and row.get(
                        "model_ai_confidence"
                    ) is not None
                    and row.get(
                        "quant_confidence"
                    ) is not None
                    and row.get(
                        "smart_fast_score"
                    ) is not None
                    and bool(
                        row.get(
                            "quality_tier"
                        )
                    )
                    and bool(
                        row.get(
                            "deep_status"
                        )
                    )
                    for row in (
                        cycle.get("positions")
                        or []
                    )
                    if isinstance(row, dict)
                ),
            }
        )

        with self._lock:
            cycles = list(self._state.get("cycles") or [])
            cycles.append(cycle)
            self._state["cycles"] = cycles[-1000:]
            self._state["current_cycle"] = None
            self._state["current_capital"] = round(next_capital, 8)
            if not invalid:
                self._state["reserve_balance"] = round(reserve_before + harvested, 8)
                self._state["total_harvested"] = round(
                    self._safe_float(self._state.get("total_harvested"), 0.0) + harvested,
                    8,
                )
            self._state["next_cycle_at"] = (
                self._now() + self.restart_cooldown_seconds
                if self._state.get("enabled") and self._state.get("auto_restart")
                else None
            )
            self._state["status"] = (
                "COOLDOWN"
                if self._state.get("enabled") and self._state.get("auto_restart")
                else "STOPPED"
            )
            self._state["paused_reason"] = None
            self._state["pending_elite_candidates"] = []
            self._state["intelligence_bridge_state"] = (
                "COOLDOWN"
                if self._state.get("enabled") and self._state.get("auto_restart")
                else "IDLE"
            )
            self._persist()

        self._journal(
            "COMPOUND_CYCLE_CLOSED",
            {
                "cycle_number": cycle.get("cycle_number"),
                "reason": reason,
                "result": result_label,
                "realised_profit": realised,
                "harvested_profit": harvested,
                "next_cycle_capital": next_capital,
            },
        )
        return self.status()

    def _monitor_cycle(self, account: Dict[str, Any], positions: List[Dict[str, Any]]) -> Dict[str, Any]:
        with self._lock:
            cycle = dict(self._state.get("current_cycle") or {})
        if not cycle:
            return self.status()

        external = self._external_positions(positions)
        learning_positions = self._learning_positions(positions)
        compound = self._compound_positions(positions)

        if external:
            # Only genuinely external/manual positions are contamination.
            # Jasong Learning positions are an intentional V6.8.13 dual-track
            # execution source and must not invalidate Compound.
            self._close_compound_positions(compound)
            return self._finalise_cycle(
                "EXTERNAL_BROKER_CONTAMINATION",
                self._safe_float(
                    account.get("profit_loss"),
                    0.0,
                ),
                invalid=True,
            )

        if not compound:
            # All compound positions disappeared (manual close, broker close,
            # restart recovery). Finalise from clean account balance delta.
            reason = str(cycle.get("exit_reason") or "BROKER_POSITIONS_CLOSED")
            return self._finalise_cycle(
                reason,
                self._safe_float(account.get("profit_loss"), 0.0),
            )

        try:
            running_pnl, pnl_detail = self._compound_running_pnl(
                account,
                compound,
                learning_positions,
            )
        except Exception as exc:
            # Do not fabricate a Compound P&L. Keep the basket open, retain
            # broker truth, and expose the valuation fault for immediate
            # reconciliation instead of triggering a false +50%/-15% exit.
            cycle["last_pnl_isolation_error"] = (
                f"{type(exc).__name__}: {exc}"
            )
            cycle["pnl_isolation_state"] = "ERROR"
            cycle["learning_broker_positions"] = [
                dict(row)
                for row in learning_positions
            ]
            cycle["broker_open_positions"] = compound
            cycle["last_broker_check_at"] = self._now()
            with self._lock:
                self._state["current_cycle"] = cycle
                self._state["status"] = "ACTIVE_PNL_ISOLATION_ERROR"
                self._state["paused_reason"] = (
                    "Compound remains open on IG DEMO, but isolated JSCMP P&L "
                    "could not be valued while Learning shares the account. "
                    "No synthetic/account-wide close trigger was used."
                )
                self._persist()
            return self.status()

        cycle["pnl_isolation_state"] = "OK"
        cycle["pnl_isolation_detail"] = pnl_detail
        cycle["last_pnl_isolation_error"] = None

        with self._lock:
            paused = str(
                self._state.get("paused_reason") or ""
            )
            if "isolated JSCMP P&L could not be valued" in paused:
                self._state["paused_reason"] = None
        cycle["learning_broker_positions"] = [
            dict(row)
            for row in learning_positions
        ]
        cycle["learning_broker_positions_count"] = len(
            learning_positions
        )

        target = self._safe_float(cycle.get("target_profit"), 0.0)
        stop = self._safe_float(cycle.get("stop_loss_amount"), 0.0)

        cycle["running_pnl"] = round(running_pnl, 8)
        cycle["peak_pnl"] = round(
            max(
                self._safe_float(
                    cycle.get("peak_pnl"),
                    running_pnl,
                ),
                running_pnl,
            ),
            8,
        )
        cycle["trough_pnl"] = round(
            min(
                self._safe_float(
                    cycle.get("trough_pnl"),
                    running_pnl,
                ),
                running_pnl,
            ),
            8,
        )
        capital = self._safe_float(
            cycle.get("starting_capital"),
            0.0,
        )
        cycle["basket_mfe_pnl"] = cycle["peak_pnl"]
        cycle["basket_mae_pnl"] = cycle["trough_pnl"]
        cycle["basket_mfe_pct"] = (
            round(
                cycle["peak_pnl"]
                / capital
                * 100.0,
                4,
            )
            if capital > 0
            else 0.0
        )
        cycle["basket_mae_pct"] = (
            round(
                cycle["trough_pnl"]
                / capital
                * 100.0,
                4,
            )
            if capital > 0
            else 0.0
        )

        self._update_cycle_position_excursions(
            cycle,
            compound,
        )
        cycle["last_broker_check_at"] = self._now()
        cycle["broker_open_positions"] = compound

        with self._lock:
            self._state["current_cycle"] = cycle
            self._state["status"] = "ACTIVE" if cycle.get("status") != "CLOSING" else "CLOSING_BASKET"
            self._state["intelligence_bridge_state"] = (
                "BASKET_ACTIVE"
                if cycle.get("status") != "CLOSING"
                else "BASKET_CLOSING"
            )
            self._persist()

        close_reason = None
        if target > 0 and running_pnl >= target:
            close_reason = "TAKE_PROFIT_50"
        elif stop > 0 and running_pnl <= -stop:
            close_reason = "STOP_LOSS_15"
        elif str(cycle.get("status") or "").upper() == "CLOSING":
            close_reason = str(cycle.get("exit_reason") or "CLOSING")

        if close_reason:
            close_results = self._close_compound_positions(compound)
            with self._lock:
                cycle = dict(self._state.get("current_cycle") or {})
                cycle["status"] = "CLOSING"
                cycle["exit_reason"] = close_reason
                cycle["trigger_pnl"] = running_pnl
                cycle["close_results"] = close_results
                self._state["current_cycle"] = cycle
                self._state["status"] = "CLOSING_BASKET"
                self._persist()
            return self._finalise_cycle(close_reason, running_pnl)

        return self.status()

    # ------------------------------------------------------------------
    # Public controls
    # ------------------------------------------------------------------

    def start_campaign(self, starting_capital: float, *, new_campaign: bool = True) -> Dict[str, Any]:
        capital = self._safe_float(starting_capital, 0.0)
        if capital < self.min_cycle_capital:
            raise ValueError(
                f"starting_capital must be at least {self.min_cycle_capital:g}"
            )
        if not self.broker.configured():
            raise IGDemoError("IG DEMO credentials are not configured")

        account, _ = self._broker_snapshot()
        account_balance = self._safe_float(account.get("balance"), 0.0)
        if capital > account_balance + 1e-9:
            raise ValueError(
                f"starting_capital {capital:.2f} exceeds IG DEMO account balance {account_balance:.2f}"
            )

        with self._lock:
            current = self._state.get("current_cycle")
            if current:
                self._state["enabled"] = True
                self._state["auto_restart"] = True
                self._state["status"] = "ACTIVE"
                self._persist()
                return self.status()

            if new_campaign or not self._state.get("campaign_id"):
                self._state["campaign_id"] = str(uuid.uuid4())
                self._state["campaign_started_at"] = self._now()
                self._state["campaign_initial_capital"] = round(capital, 8)
                self._state["current_capital"] = round(capital, 8)
                self._state["reserve_balance"] = 0.0
                self._state["total_harvested"] = 0.0
                self._state["cycle_number"] = 0
            elif self._state.get("current_capital") is None:
                self._state["current_capital"] = round(capital, 8)

            self._state["enabled"] = True
            self._state["auto_restart"] = True
            self._state["next_cycle_at"] = self._now()
            self._state["status"] = "STARTING"
            self._state["paused_reason"] = None
            self._state["last_error"] = None
            self._state["intelligence_bridge_state"] = "STARTING"
            self._persist()

        self.start_thread()
        self._journal(
            "COMPOUND_CAMPAIGN_STARTED",
            {"starting_capital": capital, "new_campaign": new_campaign},
        )
        return self.tick()

    def resume(self) -> Dict[str, Any]:
        with self._lock:
            if not self._state.get("campaign_id") or self._state.get("current_capital") is None:
                raise ValueError("No compound campaign exists to resume")
            self._state["enabled"] = True
            self._state["auto_restart"] = True
            self._state["next_cycle_at"] = self._now()
            self._state["status"] = "RESUMING"
            self._state["paused_reason"] = None
            self._state["intelligence_bridge_state"] = "RESUMING"
            self._persist()
        self.start_thread()
        return self.tick()

    def stop(self, *, close_now: bool = False) -> Dict[str, Any]:
        with self._lock:
            self._state["enabled"] = False
            self._state["auto_restart"] = False
            self._state["next_cycle_at"] = None
            cycle = dict(self._state.get("current_cycle") or {})
            if cycle and not close_now:
                self._state["status"] = "DRAINING"
                self._state["paused_reason"] = "No new cycles will start; current basket still obeys +50%/-15% exits."
            elif not cycle:
                self._state["status"] = "STOPPED"
                self._state["paused_reason"] = None
                self._state["intelligence_bridge_state"] = "IDLE"
            self._persist()

        if close_now and cycle:
            try:
                account, positions = self._broker_snapshot()
                compound = self._compound_positions(positions)
                running = self._safe_float(account.get("profit_loss"), 0.0)
                self._close_compound_positions(compound)
                with self._lock:
                    current = dict(self._state.get("current_cycle") or {})
                    current["status"] = "CLOSING"
                    current["exit_reason"] = "MANUAL_CLOSE"
                    current["trigger_pnl"] = running
                    self._state["current_cycle"] = current
                    self._persist()
                return self._finalise_cycle("MANUAL_CLOSE", running)
            except Exception as exc:
                with self._lock:
                    self._state["last_error"] = f"manual close: {type(exc).__name__}: {exc}"
                    self._persist()
        return self.status()

    def mark_legacy_execution_paused(self, paused: bool) -> None:
        with self._lock:
            self._state["legacy_execution_paused"] = bool(paused)
            self._persist()

    def tick(self) -> Dict[str, Any]:
        if not self._tick_lock.acquire(blocking=False):
            return self.status()
        try:
            with self._lock:
                self._state["last_tick_at"] = self._now()

            if not self.broker.configured():
                with self._lock:
                    self._state["status"] = "BROKER_NOT_CONFIGURED"
                    self._state["last_error"] = "IG DEMO credentials not configured"
                    self._persist()
                return self.status()

            account, positions = self._broker_snapshot()
            with self._lock:
                current_cycle = self._state.get("current_cycle")
                enabled = bool(self._state.get("enabled"))

            if current_cycle:
                result = self._monitor_cycle(account, positions)
            elif enabled:
                result = self._open_cycle(account, positions)
            else:
                with self._lock:
                    if self._state.get("status") not in {"STOPPED", "DRAINING"}:
                        self._state["status"] = "STOPPED"
                    self._persist()
                result = self.status()

            with self._lock:
                self._state["last_error"] = None
                self._persist()
            return result
        except Exception as exc:
            with self._lock:
                self._state["last_error"] = f"{type(exc).__name__}: {exc}"
                self._state["status"] = "ERROR"
                self._persist()
            return self.status()
        finally:
            self._tick_lock.release()

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def history(self, limit: int = 100) -> Dict[str, Any]:
        with self._lock:
            cycles = list(self._state.get("cycles") or [])
        cycles.sort(key=lambda row: self._safe_float(row.get("started_at"), 0.0), reverse=True)
        return {
            "version": self.VERSION,
            "cycles": cycles[: max(1, min(int(limit), 1000))],
            "environment": "DEMO",
            "live_money_execution": False,
        }

    def candidates(self) -> Dict[str, Any]:
        with self._lock:
            rows = list(self._state.get("last_candidate_ranking") or [])
        return {
            "version": self.VERSION,
            "candidates": rows,
            "environment": "DEMO",
            "live_money_execution": False,
        }

    def positions(self) -> Dict[str, Any]:
        with self._lock:
            rows = [
                dict(row)
                for row in (self._state.get("broker_positions") or [])
                if bool(row.get("is_compound"))
            ]
        return {
            "version": self.VERSION,
            "positions": rows,
            "environment": "DEMO",
            "live_money_execution": False,
        }

    def rules(self) -> Dict[str, Any]:
        return {
            "name": "JASONG ELITE 80/20 COMPOUND",
            "version": self.VERSION,
            "legacy_fallback_profit_target_pct":
                self.profit_target_pct,
            "adaptive_target_mode":
                self.target_mode,
            "target_multiples":
                list(self.target_multiples),
            "default_target_multiple":
                self.default_target_multiple,
            "target_min_samples_per_target":
                self.target_min_samples_per_target,
            "target_evidence_window":
                self.target_evidence_window,
            "target_optimizer_weights": {
                "win_probability":
                    self.target_weight_probability,
                "cycle_duration":
                    self.target_weight_duration,
                "loss_recovery":
                    self.target_weight_recovery,
                "harvested_reserve":
                    self.target_weight_reserve,
            },
            "stop_loss_pct": self.stop_loss_pct,
            "profit_harvest_pct": self.harvest_pct,
            "profit_compound_pct": 1.0 - self.harvest_pct,
            "max_positions": self.max_positions,
            "required_basket_positions":
                self.required_basket_positions,
            "candidate_pool_size":
                self.candidate_pool_size,
            "eligibility_refresh_seconds":
                self.poll_seconds,
            "new_cycle_requires_full_five":
                True,
            "partial_basket_becomes_active":
                False,
            "model_ai_min_confidence": self.ai_min_confidence,
            "quant_min_confidence": self.quant_min_confidence,
            "fast_score_min": self.fast_score_min,
            "global_fast_score_min": self.global_fast_score_min,
            "fast_threshold_policy": {
                "SERVER_FRESH_SIGNAL": self.fast_score_min,
                "GLOBAL_MULTI_MARKET": self.global_fast_score_min,
            },
            "execution_policy": "CONFIDENCE_FIRST",
            "elite_required_for_execution": False,
            "quality_required_for_execution": False,
            "deep_required_for_execution": False,
            "quality_tiers": ["A+", "A"],
            "learning_floor": {
                "model_ai_min_confidence": self.learning_ai_min_confidence,
                "quant_min_confidence": self.learning_quant_min_confidence,
                "fast_score_min": self.learning_fast_score_min,
                "quality_tiers": ["B", "B+", "A", "A+"],
                "execution_environment": "IG_DEMO_ONLY",
            },
            "elite_states": [
                "ELITE_A_PLUS", "ELITE_A", "LEARNING_PLUS",
                "LEARNING", "OBSERVE", "INVALID",
            ],
            "direction_agreement_required": True,
            "ig_tradeable_required": True,
            "max_spread_bps": self.max_spread_bps,
            "high_correlation_abs": self.high_correlation_abs,
            "max_currency_exposure": self.max_currency_exposure,
            "max_theme_exposure": self.max_theme_exposure,
            "asset_spread_bps": dict(self.asset_spread_bps),
            "forced_filler_trades": False,
            "allocation_weights": [25, 22, 20, 18, 15],
            "reference_capital": self.reference_capital,
            "reference_deal_size": self.reference_deal_size,
            "max_deal_size": self.max_deal_size,
            "reserve_reused": False,
            "auto_restart": True,
            "broker_execution": "IG_DEMO_ONLY",
            "legacy_tracking_preserved": True,
            "learning_can_coexist_with_compound": True,
            "unified_intelligence_bridge": True,
            "external_manual_positions_block_compound": True,
            "signals_evaluated_while_broker_blocked": True,
            "evidence_integrity_tracking": True,
            "position_mfe_mae_tracking": True,
            "basket_mfe_mae_tracking": True,
            "verified_close_required_for_accounting": True,
            "live_money_execution": False,
        }

    def status(self) -> Dict[str, Any]:
        with self._lock:
            state = dict(self._state)
            cycles = list(state.get("cycles") or [])
            current = dict(state.get("current_cycle") or {}) if state.get("current_cycle") else None
            account = dict(state.get("broker_account") or {})
            positions = list(state.get("broker_positions") or [])
            ranking = list(state.get("last_candidate_ranking") or [])

        completed = [row for row in cycles if str(row.get("status") or "").upper() == "COMPLETED"]
        wins = sum(1 for row in completed if str(row.get("result") or "").upper() == "WIN")
        losses = sum(1 for row in completed if str(row.get("result") or "").upper() == "LOSS")
        total_realised = sum(self._safe_float(row.get("realised_profit"), 0.0) for row in completed)

        target_progress = 0.0
        stop_progress = 0.0
        if current:
            pnl = self._safe_float(current.get("running_pnl"), 0.0)
            target = self._safe_float(current.get("target_profit"), 0.0)
            stop = self._safe_float(current.get("stop_loss_amount"), 0.0)
            if pnl >= 0 and target > 0:
                target_progress = max(0.0, min(1.0, pnl / target))
            elif pnl < 0 and stop > 0:
                stop_progress = max(0.0, min(1.0, abs(pnl) / stop))

        return {
            "version": self.VERSION,
            "name": "JASONG ELITE 80/20 COMPOUND",
            "enabled": bool(state.get("enabled")),
            "execution_mode": "IG_DEMO_ONLY",
            "direct_ig_demo_execution": True,
            "paper_execution_enabled": False,
            "dual_track_execution": True,
            "learning_coexistence_allowed": True,
            "global_ig_demo_max_positions":
                self.global_broker_max_positions,
            "compound_max_positions":
                self.max_positions,
            "required_basket_positions":
                self.required_basket_positions,
            "candidate_pool_size":
                self.candidate_pool_size,
            "eligibility_refresh_seconds":
                self.poll_seconds,
            "basket_assembly":
                dict(
                    state.get(
                        "basket_assembly"
                    )
                    or {}
                ),
            "broker_capacity":
                self._compound_open_capacity(positions),
            "auto_restart": bool(state.get("auto_restart")),
            "status": state.get("status") or "STOPPED",
            "paused_reason": state.get("paused_reason"),
            "campaign_id": state.get("campaign_id"),
            "campaign_started_at": state.get("campaign_started_at"),
            "campaign_initial_capital": state.get("campaign_initial_capital"),
            "current_capital": state.get("current_capital"),
            "reserve_balance": self._safe_float(state.get("reserve_balance"), 0.0),
            "total_harvested": self._safe_float(state.get("total_harvested"), 0.0),
            "cycle_number": int(state.get("cycle_number") or 0),
            "current_cycle": current,
            "current_target_progress_pct": round(target_progress * 100.0, 2),
            "current_stop_progress_pct": round(stop_progress * 100.0, 2),
            "target_optimizer": self.target_analysis(),
            "broker_account": account,
            "compound_broker_positions": self._compound_positions(positions),
            "learning_broker_positions": self._learning_positions(positions),
            "external_broker_positions": self._external_positions(positions),
            # Backward-compatible alias; now external/manual only.
            "foreign_broker_positions": self._external_positions(positions),
            "legacy_execution_paused": bool(state.get("legacy_execution_paused")),
            "pending_elite_candidates": [
                dict(row)
                for row in (state.get("pending_elite_candidates") or [])
                if isinstance(row, dict)
            ][: self.max_positions],
            "pending_elite_count": len(
                [
                    row
                    for row in (state.get("pending_elite_candidates") or [])
                    if isinstance(row, dict)
                ]
            ),
            "last_intelligence_signal": (
                {
                    key: value
                    for key, value in dict(
                        state.get("last_intelligence_signal")
                    ).items()
                    if key not in {
                        "paper_learning",
                        "paper_only",
                    }
                }
                if isinstance(
                    state.get("last_intelligence_signal"),
                    dict,
                )
                else None
            ),
            "last_intelligence_at": state.get("last_intelligence_at"),
            "intelligence_bridge_state": state.get("intelligence_bridge_state") or "IDLE",
            "intelligence_wake_count": int(state.get("intelligence_wake_count") or 0),
            "last_foreign_blockers": [
                dict(row)
                for row in (state.get("last_foreign_blockers") or [])
                if isinstance(row, dict)
            ],
            "last_candidate_ranking": ranking[:30],
            "recent_cycles": sorted(
                cycles,
                key=lambda row: self._safe_float(row.get("started_at"), 0.0),
                reverse=True,
            )[:20],
            "performance": {
                "completed_cycles": len(completed),
                "wins": wins,
                "losses": losses,
                "win_rate_pct": round(wins / len(completed) * 100.0, 2) if completed else 0.0,
                "total_realised_profit": round(total_realised, 8),
                "total_harvested": self._safe_float(state.get("total_harvested"), 0.0),
                "reserve_balance": self._safe_float(state.get("reserve_balance"), 0.0),
            },
            "next_cycle_at": state.get("next_cycle_at"),
            "last_selection_at": state.get("last_selection_at"),
            "last_tick_at": state.get("last_tick_at"),
            "last_error": state.get("last_error"),
            "close_integrity": dict(
                state.get("close_integrity")
                or {}
            ),
            "rules": self.rules(),
            "state_path": str(self.state_path),
            "environment": "DEMO",
            "live_money_execution": False,
        }
