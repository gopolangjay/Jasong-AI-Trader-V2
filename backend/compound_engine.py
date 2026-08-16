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
    """Jasong V6.7.2 Elite 80/20 Compound Engine — evidence-integrity release.

    Design goals:
      * preserve the existing Jasong AI / PAPER / SHADOW learning engines;
      * reuse their current validated watcher intelligence as the candidate feed;
      * execute a separate IG DEMO strategy using JSCMP_* deal references;
      * keep compound state in its own persistent file;
      * use a configurable starting capital, +50% basket target, -15% basket stop,
        20% profit harvest and 80% profit compounding;
      * select up to five elite, diversified FX markets;
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

    VERSION = "6.7.2"
    DEAL_PREFIX = "JSCMP_"

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
        self.max_positions = self._int_env(
            "COMPOUND_MAX_POSITIONS", 5, 1, 10
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
        self.max_spread_bps = self._float_env(
            "COMPOUND_MAX_SPREAD_BPS", 8.0, 0.1, 100.0
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
            "COMPOUND_SELECTION_REFRESH_SECONDS", 120, 30, 3600
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
                name="jasong-v672-elite-compound",
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
                    "is_compound": ref.startswith(self.DEAL_PREFIX),
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
    def _foreign_positions(positions: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [dict(row) for row in positions if not bool(row.get("is_compound"))]

    @staticmethod
    def _compound_positions(positions: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [dict(row) for row in positions if bool(row.get("is_compound"))]

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
        q_score = {"A+": 100.0, "A": 92.0}.get(q, 0.0)
        d_score = {
            "VERIFIED": 100.0,
            "NEAR_VERIFIED": 92.0,
            "WATCH": 82.0,
            "AI_LEARNING_SHADOW_PROMOTION": 86.0,
        }.get(d, 0.0)
        return 0.60 * q_score + 0.40 * d_score

    @staticmethod
    def _normalise_pair(value: Any) -> str:
        text = str(value or "").upper().strip()
        letters = "".join(ch for ch in text if ch.isalpha())
        if len(letters) >= 6:
            letters = letters[:6]
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

    def _spread_metrics(self, symbol: str) -> Dict[str, Any]:
        market = self.broker.resolve_market(symbol, require_tradeable=True)
        details = market.get("details") or {}
        snapshot = details.get("snapshot") or {}
        bid = self._safe_float(snapshot.get("bid"), 0.0)
        offer = self._safe_float(
            snapshot.get("offer")
            if snapshot.get("offer") is not None
            else snapshot.get("ask"),
            0.0,
        )
        if bid <= 0 or offer <= 0 or offer < bid:
            return {
                "ok": False,
                "reason": "IG DEMO spread unavailable",
                "spread_bps": None,
                "spread_score": 0.0,
                "market": market,
            }
        mid = (bid + offer) / 2.0
        spread_bps = ((offer - bid) / mid) * 10000.0 if mid > 0 else 999.0
        spread_score = max(0.0, 100.0 * (1.0 - spread_bps / self.max_spread_bps))
        return {
            "ok": spread_bps <= self.max_spread_bps,
            "reason": None if spread_bps <= self.max_spread_bps else "Spread above elite limit",
            "spread_bps": spread_bps,
            "spread_score": spread_score,
            "market": market,
        }

    def _rank_candidates(self, capital: float) -> List[Dict[str, Any]]:
        source_rows = self.candidate_source(capital) or []
        screened: List[Dict[str, Any]] = []

        for source_rank, raw in enumerate(source_rows, start=1):
            row = dict(raw or {})
            row["source_rank"] = source_rank
            row.setdefault("eligible", False)
            row.setdefault("rejection_reasons", [])

            symbol = self._normalise_pair(row.get("symbol") or row.get("market"))
            direction = str(row.get("direction") or "").upper().strip()
            ai = self._safe_float(row.get("model_ai_confidence"), 0.0)
            quant = self._safe_float(row.get("quant_confidence"), 0.0)
            fast = self._safe_float(row.get("smart_fast_score"), 0.0)
            quality = str(row.get("quality_tier") or "").upper().strip()
            deep = str(row.get("deep_status") or "").upper().strip()
            direction_match = bool(row.get("direction_match"))

            reasons: List[str] = []
            if not symbol or direction not in {"BUY", "SELL"}:
                reasons.append("Invalid symbol/direction")
            if ai < self.ai_min_confidence:
                reasons.append(f"AI {ai*100:.1f}% < {self.ai_min_confidence*100:.0f}%")
            if quant < self.quant_min_confidence:
                reasons.append(f"Quant {quant*100:.1f}% < {self.quant_min_confidence*100:.0f}%")
            if fast < self.fast_score_min:
                reasons.append(f"Fast score {fast:.1f} < {self.fast_score_min:.0f}")
            if quality not in {"A+", "A"}:
                reasons.append(f"Quality {quality or '-'} is not A/A+")
            if deep not in {"VERIFIED", "NEAR_VERIFIED", "WATCH", "AI_LEARNING_SHADOW_PROMOTION"}:
                reasons.append(f"Deep status {deep or '-'} not elite-eligible")
            if not direction_match:
                reasons.append("Live direction does not agree")

            row.update(
                {
                    "symbol": symbol,
                    "direction": direction,
                    "model_ai_confidence": ai,
                    "quant_confidence": quant,
                    "smart_fast_score": fast,
                    "quality_tier": quality,
                    "deep_status": deep,
                    "direction_match": direction_match,
                }
            )

            if reasons:
                row["rejection_reasons"] = reasons
                screened.append(row)
                continue

            try:
                spread = self._spread_metrics(symbol)
            except Exception as exc:
                row["rejection_reasons"] = [f"IG DEMO preflight: {type(exc).__name__}: {exc}"]
                screened.append(row)
                continue

            row["spread_bps"] = spread.get("spread_bps")
            row["spread_score"] = spread.get("spread_score")
            row["ig_epic"] = (spread.get("market") or {}).get("epic")
            if not spread.get("ok"):
                row["rejection_reasons"] = [str(spread.get("reason") or "Spread gate failed")]
                screened.append(row)
                continue

            deep_quality = self._quality_score(quality, deep)
            base_score = (
                25.0 * ai
                + 25.0 * quant
                + 0.20 * deep_quality
                + 0.15 * fast
                + 0.10 * self._safe_float(spread.get("spread_score"), 0.0)
            )
            row["deep_quality_score"] = round(deep_quality, 2)
            row["elite_base_score"] = round(base_score, 2)
            row["eligible"] = True
            screened.append(row)

        eligible = [row for row in screened if row.get("eligible")]
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
            if len(selected) >= self.max_positions:
                break
            symbol = str(row.get("symbol") or "")
            if any(str(x.get("symbol")) == symbol for x in selected):
                row["eligible"] = False
                row["rejection_reasons"] = ["Duplicate market"]
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
                row["rejection_reasons"] = ["Currency exposure limit"]
                continue

            correlations = [
                abs(self._correlation(matrix, symbol, str(existing.get("symbol") or "")))
                for existing in selected
            ]
            max_corr = max(correlations) if correlations else 0.0
            if max_corr >= self.high_correlation_abs:
                row["eligible"] = False
                row["rejection_reasons"] = [f"Correlation {max_corr:.2f} >= {self.high_correlation_abs:.2f}"]
                continue

            diversification_score = max(0.0, min(100.0, 100.0 * (1.0 - max_corr)))
            elite_score = self._safe_float(row.get("elite_base_score"), 0.0) + 0.05 * diversification_score
            row["max_abs_correlation"] = round(max_corr, 4)
            row["diversification_score"] = round(diversification_score, 2)
            row["elite_score"] = round(elite_score, 2)
            row["selected"] = True
            selected.append(row)
            exposures = prospective

        selected_ids = {id(row) for row in selected}
        for row in screened:
            row.setdefault("selected", id(row) in selected_ids)
            row.setdefault("elite_score", row.get("elite_base_score"))

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
            self._state["last_candidate_ranking"] = snapshot[:50]
            self._state["last_selection_at"] = now
        self._journal(
            "ELITE_RANKING",
            {
                "evaluated": len(snapshot),
                "selected": len(selected),
                "selected_symbols": [row.get("symbol") for row in selected],
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
        selected = self._rank_candidates(capital)

        foreign = self._foreign_positions(positions)
        if foreign:
            blocker_names = [
                str(
                    row.get("symbol")
                    or row.get("epic")
                    or row.get("deal_reference")
                    or "IG position"
                )
                for row in foreign
            ]
            with self._lock:
                self._state["status"] = "WAITING_FOR_CLEAN_BROKER"
                self._state["last_foreign_blockers"] = [
                    dict(row)
                    for row in foreign
                ]
                self._state["pending_elite_candidates"] = [
                    dict(row)
                    for row in selected
                ]
                self._state["intelligence_bridge_state"] = (
                    "ELITE_READY_BROKER_BLOCKED"
                    if selected
                    else "BROKER_BLOCKED_NO_ELITE"
                )
                self._state["paused_reason"] = (
                    f"{len(foreign)} legacy/manual IG DEMO position(s) block "
                    "Compound execution because IG account P&L is account-wide. "
                    + (
                        f"{len(selected)} Elite setup(s) are ready and will be "
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
                self._state["status"] = "WAITING_FOR_ELITE_MARKETS"
                self._state["paused_reason"] = (
                    "No market currently passes every Elite gate. "
                    "Unified Live Intelligence remains connected and will wake "
                    "the Compound engine when a new directional setup arrives."
                )
                self._state["pending_elite_candidates"] = []
                self._state["intelligence_bridge_state"] = "LISTENING_FOR_ELITE"
                self._persist()
            return self.status()

        with self._lock:
            self._state["pending_elite_candidates"] = [
                dict(row)
                for row in selected
            ]
            self._state["intelligence_bridge_state"] = "ELITE_READY"

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
                "target_pct": self.profit_target_pct,
                "target_profit": round(capital * self.profit_target_pct, 8),
                "stop_pct": self.stop_loss_pct,
                "stop_loss_amount": round(capital * self.stop_loss_pct, 8),
                "harvest_pct": self.harvest_pct,
                "broker_balance_before": account_balance,
                "broker_currency": account.get("currency"),
                "positions": [],
                "selected_candidates": [dict(row) for row in selected],
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

        weights = self._weights(len(selected))
        opened: List[Dict[str, Any]] = []
        errors: List[str] = []

        for index, (candidate, weight) in enumerate(zip(selected, weights), start=1):
            allocation = capital * weight
            requested_size = self._deal_size(allocation)
            ref = self._new_deal_reference(cycle_number, index)
            try:
                result = self.broker.open_market_position(
                    symbol=str(candidate.get("symbol") or candidate.get("market") or ""),
                    direction=str(candidate.get("direction") or "").upper(),
                    size=requested_size,
                    deal_reference=ref,
                )
                row = {
                    "slot": index,
                    "symbol": candidate.get("symbol"),
                    "market": candidate.get("market"),
                    "direction": candidate.get("direction"),
                    "elite_score": candidate.get("elite_score"),
                    "model_ai_confidence": candidate.get("model_ai_confidence"),
                    "quant_confidence": candidate.get("quant_confidence"),
                    "smart_fast_score": candidate.get("smart_fast_score"),
                    "quality_tier": candidate.get("quality_tier"),
                    "deep_status": candidate.get("deep_status"),
                    "spread_bps": candidate.get("spread_bps"),
                    "intelligence_source": candidate.get("intelligence_source"),
                    "intelligence_age_seconds": candidate.get("intelligence_age_seconds"),
                    "source_rank": candidate.get("source_rank"),
                    "live_price_at_selection": candidate.get("live_price"),
                    "entry_rsi": candidate.get("rsi"),
                    "signal_reason": candidate.get("signal_reason") or candidate.get("reason"),
                    "historical_win_rate": candidate.get("historical_win_rate"),
                    "historical_profit_factor": candidate.get("historical_profit_factor"),
                    "historical_trades": candidate.get("historical_trades"),
                    "max_abs_correlation": candidate.get("max_abs_correlation"),
                    "diversification_score": candidate.get("diversification_score"),
                    "weight_pct": round(weight * 100.0, 4),
                    "allocation_amount": round(allocation, 8),
                    "requested_size": requested_size,
                    "ig_size": result.get("size"),
                    "ig_deal_id": result.get("dealId"),
                    "ig_deal_reference": result.get("dealReference") or ref,
                    "ig_epic": result.get("epic"),
                    "entry_level": result.get("level"),
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
                with self._lock:
                    current = self._state.get("current_cycle") or {}
                    current_positions = list(current.get("positions") or [])
                    current_positions.append(row)
                    current["positions"] = current_positions
                    self._state["current_cycle"] = current
                    self._persist()
            except Exception as exc:
                message = f"{candidate.get('symbol')}: {type(exc).__name__}: {exc}"
                errors.append(message)
                self._journal("COMPOUND_OPEN_ERROR", {"message": message})

        if not opened:
            with self._lock:
                cycle = dict(self._state.get("current_cycle") or {})
                cycle["status"] = "OPEN_FAILED"
                cycle["completed_at"] = self._now()
                cycle["open_errors"] = errors
                cycles = list(self._state.get("cycles") or [])
                cycles.append(cycle)
                self._state["cycles"] = cycles[-1000:]
                self._state["current_cycle"] = None
                self._state["status"] = "WAITING_FOR_ELITE_MARKETS"
                self._state["last_error"] = "; ".join(errors) if errors else "No compound position opened"
                self._state["next_cycle_at"] = self._now() + self.restart_cooldown_seconds
                self._state["intelligence_bridge_state"] = "OPEN_FAILED"
                self._persist()
            return self.status()

        with self._lock:
            cycle = dict(self._state.get("current_cycle") or {})
            cycle["status"] = "ACTIVE"
            cycle["positions"] = opened
            cycle["open_errors"] = errors
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
                "symbols": [row.get("symbol") for row in opened],
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

        if invalid:
            harvested = 0.0
            compounded_profit = 0.0
            next_capital = capital
            result_label = "INVALID"
        else:
            positive_profit = max(0.0, realised)
            harvested = positive_profit * self.harvest_pct
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

        foreign = self._foreign_positions(positions)
        compound = self._compound_positions(positions)

        if foreign:
            # Preserve evidence integrity. Account-wide P&L is contaminated by
            # another broker position, so exit only our positions and do not
            # compound or harvest this cycle.
            self._close_compound_positions(compound)
            return self._finalise_cycle(
                "BROKER_CONTAMINATION",
                self._safe_float(account.get("profit_loss"), 0.0),
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

        running_pnl = self._safe_float(account.get("profit_loss"), 0.0)
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
            "profit_target_pct": self.profit_target_pct,
            "stop_loss_pct": self.stop_loss_pct,
            "profit_harvest_pct": self.harvest_pct,
            "profit_compound_pct": 1.0 - self.harvest_pct,
            "max_positions": self.max_positions,
            "model_ai_min_confidence": self.ai_min_confidence,
            "quant_min_confidence": self.quant_min_confidence,
            "fast_score_min": self.fast_score_min,
            "quality_tiers": ["A+", "A"],
            "direction_agreement_required": True,
            "ig_tradeable_required": True,
            "max_spread_bps": self.max_spread_bps,
            "high_correlation_abs": self.high_correlation_abs,
            "max_currency_exposure": self.max_currency_exposure,
            "forced_filler_trades": False,
            "allocation_weights": [25, 22, 20, 18, 15],
            "reference_capital": self.reference_capital,
            "reference_deal_size": self.reference_deal_size,
            "max_deal_size": self.max_deal_size,
            "reserve_reused": False,
            "auto_restart": True,
            "broker_execution": "IG_DEMO_ONLY",
            "legacy_tracking_preserved": True,
            "legacy_ig_new_entries_must_be_paused_while_compound_runs": True,
            "unified_intelligence_bridge": True,
            "broker_clean_required": True,
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
            "broker_account": account,
            "compound_broker_positions": self._compound_positions(positions),
            "foreign_broker_positions": self._foreign_positions(positions),
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
                dict(state.get("last_intelligence_signal"))
                if isinstance(state.get("last_intelligence_signal"), dict)
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
