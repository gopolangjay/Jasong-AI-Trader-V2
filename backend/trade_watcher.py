from __future__ import annotations

import math
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from forward_guard import (
    DEFAULT_SLIPPAGE_BPS,
    DEFAULT_SPREAD_BPS,
    adverse_execution_price,
    freeze_forward_snapshot,
    verify_forward_snapshot,
)


class TradeWatcherEngine:
    """Server-side paper-trade watcher for Jasong AI Trader V5.6.

    The engine keeps verified candidates under observation, confirms live entry
    conditions, applies paper-risk circuit breakers, opens paper trades, closes
    them after the validated holding window, and exposes forward statistics.

    State is intentionally paper-only. Watcher state is in memory; completed
    paper trades remain in the configured database through the existing Trade
    model.
    """

    MAX_OPEN_TRADES = 2
    LOOP_SECONDS = 20

    def __init__(
        self,
        session_factory,
        trade_model,
        signal_func: Callable[[str, str, float], Dict[str, Any]],
        price_func: Callable[[str], float],
        profiles: Dict[str, Any],
    ):
        self.session_factory = session_factory
        self.Trade = trade_model
        self.signal_func = signal_func
        self.price_func = price_func
        self.profiles = profiles

        self._watchers: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="jasong-v57-forward-watcher",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            number = float(value)
            if math.isnan(number) or math.isinf(number):
                return default
            return number
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _interval_minutes(interval: Any) -> int:
        text = str(interval or "15m").strip().lower()
        try:
            if text.endswith("m"):
                return max(1, int(text[:-1]))
            if text.endswith("h"):
                return max(1, int(text[:-1])) * 60
        except ValueError:
            pass
        return 15

    @staticmethod
    def _utc_iso(ts: Optional[float]) -> Optional[str]:
        if ts is None:
            return None
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

    def _public(self, watcher: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(watcher)
        for key in (
            "created_at",
            "verified_at",
            "expires_at",
            "next_check_at",
            "last_checked_at",
            "entry_time",
            "target_exit_at",
            "closed_at",
        ):
            if key in out:
                out[f"{key}_iso"] = self._utc_iso(out.get(key))
        return out

    # ------------------------------------------------------------------
    # database helpers
    # ------------------------------------------------------------------

    def _db_rows(self, db, closed: Optional[bool] = None):
        query = db.query(self.Trade).filter(self.Trade.mode == "forward")
        if closed is not None:
            query = query.filter(self.Trade.closed == closed)
        return query.order_by(self.Trade.created_at.asc()).all()

    def _current_balance(self, db, starting_balance: float) -> float:
        rows = self._db_rows(db, closed=True)
        pnl = sum(self._safe_float(row.pnl) for row in rows)
        return max(0.0, starting_balance + pnl)

    def _daily_pnl(self, db) -> float:
        today = datetime.now(timezone.utc).date()
        total = 0.0
        for row in self._db_rows(db, closed=True):
            created = getattr(row, "created_at", None)
            if created is None:
                continue
            try:
                created_date = created.date()
            except Exception:
                continue
            if created_date == today:
                total += self._safe_float(row.pnl)
        return total

    def _consecutive_losses(self, db) -> int:
        rows = (
            db.query(self.Trade)
            .filter(self.Trade.mode == "forward")
            .filter(self.Trade.closed == True)  # noqa: E712
            .order_by(self.Trade.created_at.desc())
            .limit(20)
            .all()
        )
        count = 0
        for row in rows:
            result = str(getattr(row, "result", "") or "").upper()
            pnl = self._safe_float(getattr(row, "pnl", 0.0))
            if result == "LOSS" or pnl < 0:
                count += 1
            else:
                break
        return count

    def _open_count(self, db) -> int:
        return (
            db.query(self.Trade)
            .filter(self.Trade.mode == "forward")
            .filter(self.Trade.closed == False)  # noqa: E712
            .count()
        )

    def _duplicate_open(self, db, symbol: str) -> bool:
        return (
            db.query(self.Trade)
            .filter(self.Trade.mode == "forward")
            .filter(self.Trade.closed == False)  # noqa: E712
            .filter(self.Trade.symbol == symbol)
            .first()
            is not None
        )

    @staticmethod
    def _fx_pair(
        symbol: str,
    ) -> tuple[str, str] | None:
        clean = str(
            symbol
            or ""
        ).upper().replace(
            "=X",
            "",
        ).replace(
            "/",
            "",
        )

        if len(clean) != 6:
            return None

        return (
            clean[:3],
            clean[3:],
        )

    def _currency_exposure(
        self,
        symbol: str,
        direction: str,
    ) -> dict[str, int]:
        pair = self._fx_pair(
            symbol
        )

        if pair is None:
            return {}

        base, quote = pair

        if str(
            direction
            or ""
        ).upper() == "BUY":
            return {
                base: 1,
                quote: -1,
            }

        return {
            base: -1,
            quote: 1,
        }

    def _correlated_open_exposure(
        self,
        db,
        symbol: str,
        direction: str,
    ) -> list[str]:
        """Block strongly redundant currency-leg exposure.

        Example: BUY EURUSD + BUY GBPUSD both create short-USD exposure.
        This is intentionally simple/conservative and only acts on currently
        open paper trades.
        """

        proposed = self._currency_exposure(
            symbol,
            direction,
        )

        if not proposed:
            return []

        conflicts = []

        rows = (
            db.query(
                self.Trade
            )
            .filter(
                self.Trade.mode
                == "paper"
            )
            .filter(
                self.Trade.closed
                == False  # noqa: E712
            )
            .all()
        )

        for row in rows:
            existing = (
                self._currency_exposure(
                    str(
                        getattr(
                            row,
                            "symbol",
                            "",
                        )
                        or ""
                    ),
                    str(
                        getattr(
                            row,
                            "direction",
                            "",
                        )
                        or ""
                    ),
                )
            )

            shared_same_side = [
                currency
                for currency, side
                in proposed.items()
                if currency
                in existing
                and existing[
                    currency
                ]
                == side
            ]

            if shared_same_side:
                conflicts.append(
                    (
                        f"{getattr(row, 'symbol', '')}:"
                        f"{','.join(shared_same_side)}"
                    )
                )

        return conflicts

    # ------------------------------------------------------------------
    # risk engine
    # ------------------------------------------------------------------

    def _risk_decision(
        self,
        db,
        watcher: Dict[str, Any],
        live_signal: Dict[str, Any],
    ) -> Dict[str, Any]:
        risk_mode = watcher["risk_mode"]
        profile = self.profiles[risk_mode]
        starting_balance = self._safe_float(
            watcher.get("starting_balance", 10000.0), 10000.0
        )
        current_balance = self._current_balance(db, starting_balance)
        daily_pnl = self._daily_pnl(db)
        consecutive_losses = self._consecutive_losses(db)
        open_count = self._open_count(db)

        blocks = []

        if current_balance <= 0:
            blocks.append("PAPER_BALANCE_DEPLETED")

        if daily_pnl <= -(starting_balance * profile.daily_loss_limit):
            blocks.append("DAILY_LOSS_LIMIT_REACHED")

        if consecutive_losses >= profile.max_consecutive_losses:
            blocks.append("CONSECUTIVE_LOSS_LIMIT_REACHED")

        if open_count >= self.MAX_OPEN_TRADES:
            blocks.append("MAX_OPEN_TRADES_REACHED")

        if self._duplicate_open(db, watcher["symbol"]):
            blocks.append("DUPLICATE_MARKET_EXPOSURE")

        correlation_conflicts = (
            self._correlated_open_exposure(
                db=db,
                symbol=watcher["symbol"],
                direction=str(
                    watcher.get(
                        "direction",
                        "",
                    )
                ),
            )
        )

        if correlation_conflicts:
            blocks.append(
                "CORRELATED_CURRENCY_EXPOSURE"
            )

        strategy_health = str(
            watcher.get(
                "strategy_health",
                "PROBATION",
            )
            or "PROBATION"
        ).upper()

        if strategy_health == "QUARANTINED":
            blocks.append(
                "STRATEGY_QUARANTINED"
            )

        reliability = self._safe_float(
            watcher.get("sample_reliability", 0.0)
        )
        if reliability <= 0:
            reliability = self._safe_float(
                watcher.get("sample_reliability_pct", 0.0)
            ) / 100.0

        wilson = self._safe_float(
            watcher.get("wilson_lower_win_rate", 0.0)
        )
        if wilson <= 0:
            wilson = self._safe_float(
                watcher.get("wilson_lower_win_rate_pct", 0.0)
            ) / 100.0

        profit_factor = self._safe_float(watcher.get("profit_factor", 0.0))
        max_drawdown = abs(self._safe_float(watcher.get("max_drawdown", 0.0)))
        live_confidence = self._safe_float(live_signal.get("confidence", 0.0))

        # Dynamic sizing only REDUCES the profile's base risk. Historical
        # strength never increases stake beyond the user's selected risk mode.
        quality = (
            0.45
            + 0.20 * min(max(reliability, 0.0), 1.0)
            + 0.15 * min(max(live_confidence, 0.0), 1.0)
            + 0.10 * min(max(profit_factor / 3.0, 0.0), 1.0)
            + 0.10 * min(max(wilson / 0.70, 0.0), 1.0)
        )

        if max_drawdown > 0.03:
            quality -= min((max_drawdown - 0.03) * 4.0, 0.20)

        if strategy_health == "DEGRADING":
            quality *= 0.70
        elif strategy_health == "PROBATION":
            quality *= 0.85

        quality = min(max(quality, 0.40), 1.00)
        risk_fraction = profile.risk_per_trade * quality
        stake = round(max(current_balance * risk_fraction, 0.0), 2)

        return {
            "allowed": not blocks and stake > 0,
            "blocks": blocks,
            "starting_balance": starting_balance,
            "current_balance": round(current_balance, 2),
            "daily_pnl": round(daily_pnl, 2),
            "consecutive_losses": consecutive_losses,
            "open_trades": open_count,
            "strategy_health": strategy_health,
            "correlation_conflicts": correlation_conflicts,
            "base_risk_fraction": float(profile.risk_per_trade),
            "quality_multiplier": round(quality, 4),
            "effective_risk_fraction": round(risk_fraction, 6),
            "stake": stake,
        }

    # ------------------------------------------------------------------
    # watcher creation / retrieval
    # ------------------------------------------------------------------

    def create(
        self,
        candidate: Dict[str, Any],
        risk_mode: str,
        starting_balance: float,
        payout: float = 0.80,
    ) -> Dict[str, Any]:
        if risk_mode not in self.profiles:
            raise ValueError("Invalid risk mode")

        if not bool(candidate.get("verified", False)):
            raise ValueError("Only VERIFIED candidates may be watched")

        market = str(candidate.get("market") or "").strip()
        symbol = str(candidate.get("symbol") or "").strip()
        direction = str(candidate.get("direction") or "WAIT").upper()

        if not market or not symbol:
            raise ValueError("Verified candidate requires market and symbol")
        if direction not in {"BUY", "SELL"}:
            raise ValueError("Verified candidate direction must be BUY or SELL")

        interval_minutes = self._interval_minutes(candidate.get("interval"))
        holding_candles = max(1, int(candidate.get("holding_candles") or 1))
        now = time.time()

        # Keep an unentered verified setup alive for four validation candles.
        # If it does not confirm by then, it must be revalidated.
        watch_lifetime_minutes = max(interval_minutes * 4, 30)

        watcher_id = str(uuid.uuid4())
        watcher = {
            "watcher_id": watcher_id,
            "market": market,
            "symbol": symbol,
            "direction": direction,
            "risk_mode": risk_mode,
            "starting_balance": float(starting_balance),
            "payout": float(payout),
            "status": "WATCHING",
            "created_at": now,
            "verified_at": now,
            "expires_at": now + watch_lifetime_minutes * 60,
            "next_check_at": now,
            "last_checked_at": None,
            "trade_id": None,
            "entry_price": None,
            "entry_time": None,
            "target_exit_at": None,
            "exit_price": None,
            "closed_at": None,
            "result": None,
            "pnl": None,
            "last_live_signal": None,
            "risk_decision": None,
            "last_reason": "Verified candidate added to live watch.",
            "interval": candidate.get("interval"),
            "period": candidate.get("period"),
            "interval_minutes": interval_minutes,
            "holding_candles": holding_candles,
            "holding_minutes": interval_minutes * holding_candles,
            "threshold_pct": candidate.get("threshold_pct"),
            "deep_score": candidate.get("deep_score"),
            "reliability_adjusted_score": candidate.get(
                "reliability_adjusted_score"
            ),
            "win_rate": candidate.get("win_rate"),
            "trades": candidate.get("trades"),
            "profit_factor": candidate.get("profit_factor"),
            "max_drawdown": candidate.get("max_drawdown"),
            "sample_reliability": candidate.get("sample_reliability"),
            "sample_reliability_pct": candidate.get("sample_reliability_pct"),
            "wilson_lower_win_rate": candidate.get("wilson_lower_win_rate"),
            "wilson_lower_win_rate_pct": candidate.get(
                "wilson_lower_win_rate_pct"
            ),
            "fast_score": candidate.get("fast_score"),
            "adaptive_rank_score": candidate.get("adaptive_rank_score"),
            "strategy_health": candidate.get("strategy_health", "PROBATION"),
            "strategy_health_reason": candidate.get("strategy_health_reason"),
            "forward_symbol_stats": candidate.get("forward_symbol_stats"),
            "explanation": candidate.get("explanation"),
            # V5.7 genuine forward audit fields. These are populated only
            # when a live-confirmed forward trade actually opens.
            "forward_protocol": "V5.7_GENUINE_FORWARD",
            "entry_snapshot": None,
            "entry_snapshot_hash": None,
            "entry_price_effective": None,
            "exit_price_effective": None,
            "spread_bps": DEFAULT_SPREAD_BPS,
            "slippage_bps": DEFAULT_SLIPPAGE_BPS,
            "settlement_guard_passed": None,
            "settlement_due_at": None,
        }

        with self._lock:
            # Expire any older non-open watcher for the same symbol.
            for existing in self._watchers.values():
                if (
                    existing.get("symbol") == symbol
                    and existing.get("status")
                    in {"WATCHING", "READY", "RISK_BLOCKED"}
                ):
                    existing["status"] = "SUPERSEDED"
                    existing["last_reason"] = "Replaced by newer verified setup."
            self._watchers[watcher_id] = watcher

        # The background loop will evaluate immediately.
        return self._public(watcher)

    def list(self) -> list[Dict[str, Any]]:
        with self._lock:
            watchers = sorted(
                self._watchers.values(),
                key=lambda x: x.get("created_at", 0),
                reverse=True,
            )
            return [self._public(item) for item in watchers]

    def get(self, watcher_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            item = self._watchers.get(watcher_id)
            return self._public(item) if item else None

    # ------------------------------------------------------------------
    # live evaluation
    # ------------------------------------------------------------------

    def _evaluate(self, watcher_id: str, force: bool = False) -> None:
        with self._lock:
            watcher = self._watchers.get(watcher_id)
            if not watcher:
                return
            status = watcher.get("status")
            if status not in {"WATCHING", "READY", "RISK_BLOCKED"}:
                return

            now = time.time()
            if not force and now < self._safe_float(watcher.get("next_check_at")):
                return

            if now >= self._safe_float(watcher.get("expires_at")):
                watcher["status"] = "EXPIRED"
                watcher["last_reason"] = (
                    "Verified setup expired before live entry confirmation; "
                    "run deep validation again."
                )
                return

            symbol = watcher["symbol"]
            risk_mode = watcher["risk_mode"]
            balance = self._safe_float(watcher.get("starting_balance"), 10000.0)

        try:
            live = self.signal_func(symbol, risk_mode, balance)
        except Exception as exc:
            with self._lock:
                watcher = self._watchers.get(watcher_id)
                if watcher:
                    watcher["last_checked_at"] = time.time()
                    watcher["next_check_at"] = time.time() + 300
                    watcher["last_reason"] = f"Live check failed: {exc}"
            return

        direction = str(watcher.get("direction") or "WAIT").upper()
        live_decision = str(live.get("decision") or "WAIT").upper()
        confidence = self._safe_float(live.get("confidence", 0.0))
        ai_up = self._safe_float(live.get("combined_up_probability", 0.50), 0.50)
        rsi = self._safe_float(live.get("rsi", 50.0), 50.0)
        profile = self.profiles[risk_mode]

        opposite = (
            (direction == "BUY" and live_decision == "SELL")
            or (direction == "SELL" and live_decision == "BUY")
        )

        if opposite and confidence >= profile.min_confidence:
            with self._lock:
                watcher = self._watchers.get(watcher_id)
                if watcher:
                    watcher["status"] = "INVALIDATED"
                    watcher["last_live_signal"] = live
                    watcher["last_checked_at"] = time.time()
                    watcher["last_reason"] = (
                        "Strong live signal flipped against the verified direction."
                    )
            return

        overextended = (
            direction == "BUY" and rsi >= 70.0
        ) or (
            direction == "SELL" and rsi <= 30.0
        )

        probability_ok = ai_up >= 0.60 if direction == "BUY" else ai_up <= 0.40
        confirmed = (
            live_decision == direction
            and confidence >= profile.min_confidence
            and probability_ok
            and not overextended
        )

        now = time.time()
        next_check = now + watcher["interval_minutes"] * 60

        if not confirmed:
            reason_bits = []
            if overextended:
                reason_bits.append(f"RSI overextended at {rsi:.1f}")
            if live_decision != direction:
                reason_bits.append(
                    f"live signal {live_decision} does not match {direction}"
                )
            if confidence < profile.min_confidence:
                reason_bits.append(
                    f"confidence {confidence:.1%} below {profile.min_confidence:.0%}"
                )
            if not probability_ok:
                reason_bits.append("AI directional probability not strong enough")

            with self._lock:
                watcher = self._watchers.get(watcher_id)
                if watcher:
                    watcher["status"] = "WATCHING"
                    watcher["last_live_signal"] = live
                    watcher["last_checked_at"] = now
                    watcher["next_check_at"] = next_check
                    watcher["last_reason"] = "; ".join(reason_bits) or "Waiting."
            return

        db = self.session_factory()
        try:
            risk = self._risk_decision(db, watcher, live)
            with self._lock:
                watcher = self._watchers.get(watcher_id)
                if watcher:
                    watcher["last_live_signal"] = live
                    watcher["last_checked_at"] = now
                    watcher["risk_decision"] = risk

            if not risk["allowed"]:
                with self._lock:
                    watcher = self._watchers.get(watcher_id)
                    if watcher:
                        watcher["status"] = "RISK_BLOCKED"
                        watcher["next_check_at"] = next_check
                        watcher["last_reason"] = ", ".join(risk["blocks"])
                return

            entry_price = self._safe_float(live.get("price"), 0.0)
            if entry_price <= 0:
                entry_price = self.price_func(watcher["symbol"])

            frozen = freeze_forward_snapshot(
                watcher=watcher,
                live_signal=live,
                entry_price=entry_price,
                stake=risk["stake"],
                entry_time=now,
                spread_bps=DEFAULT_SPREAD_BPS,
                slippage_bps=DEFAULT_SLIPPAGE_BPS,
            )

            effective_entry = adverse_execution_price(
                raw_price=entry_price,
                direction=direction,
                leg="ENTRY",
                spread_bps=DEFAULT_SPREAD_BPS,
                slippage_bps=DEFAULT_SLIPPAGE_BPS,
            )

            trade = self.Trade(
                symbol=watcher["symbol"],
                direction=direction,
                confidence=confidence,
                entry_price=effective_entry,
                stake=risk["stake"],
                mode="forward",
            )
            db.add(trade)
            db.commit()
            db.refresh(trade)

            due_at = now + watcher["holding_minutes"] * 60

            with self._lock:
                watcher = self._watchers.get(watcher_id)
                if watcher:
                    watcher["status"] = "OPEN"
                    watcher["trade_id"] = trade.id
                    watcher["entry_price"] = entry_price
                    watcher["entry_price_effective"] = effective_entry
                    watcher["entry_time"] = now
                    watcher["target_exit_at"] = due_at
                    watcher["settlement_due_at"] = due_at
                    watcher["entry_snapshot"] = frozen["snapshot"]
                    watcher["entry_snapshot_hash"] = frozen["snapshot_hash"]
                    watcher["settlement_guard_passed"] = None
                    watcher["last_reason"] = (
                        "V5.7 live entry confirmed. Genuine forward paper trade "
                        "opened with frozen parameters and no future data."
                    )
        finally:
            db.close()

    def check_now(self, watcher_id: str) -> Optional[Dict[str, Any]]:
        self._resolve_open(watcher_id, force=False)
        self._evaluate(watcher_id, force=True)
        return self.get(watcher_id)

    # ------------------------------------------------------------------
    # closing / forward outcome
    # ------------------------------------------------------------------

    def _resolve_open(self, watcher_id: str, force: bool = False) -> None:
        with self._lock:
            watcher = self._watchers.get(watcher_id)
            if not watcher or watcher.get("status") != "OPEN":
                return
            target = self._safe_float(watcher.get("settlement_due_at") or watcher.get("target_exit_at"))
            # V5.7 NEVER permits early settlement, even from a forced/manual check.
            # The outcome may only be observed after the pre-committed horizon.
            if time.time() < target:
                return

            if not verify_forward_snapshot(
                watcher.get("entry_snapshot"),
                watcher.get("entry_snapshot_hash"),
            ):
                watcher["status"] = "AUDIT_BLOCKED"
                watcher["settlement_guard_passed"] = False
                watcher["last_reason"] = (
                    "Forward settlement blocked: frozen entry snapshot failed "
                    "its V5.7 integrity check."
                )
                return
            trade_id = watcher.get("trade_id")
            symbol = watcher["symbol"]
            direction = watcher["direction"]
            entry_price = self._safe_float(watcher.get("entry_price"))
            payout = self._safe_float(watcher.get("payout"), 0.80)

        try:
            exit_price = self.price_func(symbol)
        except Exception as exc:
            with self._lock:
                watcher = self._watchers.get(watcher_id)
                if watcher:
                    watcher["last_reason"] = f"Exit price unavailable: {exc}"
            return

        spread_bps = self._safe_float(
            watcher.get("spread_bps"),
            DEFAULT_SPREAD_BPS,
        )
        slippage_bps = self._safe_float(
            watcher.get("slippage_bps"),
            DEFAULT_SLIPPAGE_BPS,
        )
        effective_entry = self._safe_float(
            watcher.get("entry_price_effective"),
            entry_price,
        )
        effective_exit = adverse_execution_price(
            raw_price=exit_price,
            direction=direction,
            leg="EXIT",
            spread_bps=spread_bps,
            slippage_bps=slippage_bps,
        )

        won = (
            effective_exit > effective_entry
            if direction == "BUY"
            else effective_exit < effective_entry
        )

        db = self.session_factory()
        try:
            trade = db.query(self.Trade).filter(self.Trade.id == trade_id).first()
            if trade is None:
                return
            stake = self._safe_float(trade.stake)
            pnl = stake * payout if won else -stake
            trade.result = "WIN" if won else "LOSS"
            trade.pnl = round(pnl, 2)
            trade.closed = True
            db.commit()

            with self._lock:
                watcher = self._watchers.get(watcher_id)
                if watcher:
                    watcher["status"] = "WIN" if won else "LOSS"
                    watcher["exit_price"] = exit_price
                    watcher["exit_price_effective"] = effective_exit
                    watcher["closed_at"] = time.time()
                    watcher["result"] = "WIN" if won else "LOSS"
                    watcher["pnl"] = round(pnl, 2)
                    watcher["settlement_guard_passed"] = True
                    watcher["last_reason"] = (
                        "V5.7 genuine forward trade resolved only after the "
                        "pre-committed holding horizon using adverse execution "
                        "assumptions."
                    )
        finally:
            db.close()

    # ------------------------------------------------------------------
    # statistics
    # ------------------------------------------------------------------

    def forward_stats(self, starting_balance: float = 10000.0) -> Dict[str, Any]:
        db = self.session_factory()
        try:
            rows = self._db_rows(db, closed=True)
            closed = [
                row
                for row in rows
                if str(getattr(row, "result", "") or "").upper()
                in {"WIN", "LOSS"}
            ]

            wins = sum(
                1
                for row in closed
                if str(row.result).upper() == "WIN"
            )
            losses = len(closed) - wins
            gross_profit = sum(
                max(self._safe_float(row.pnl), 0.0) for row in closed
            )
            gross_loss = abs(
                sum(min(self._safe_float(row.pnl), 0.0) for row in closed)
            )
            profit_factor = (
                gross_profit / gross_loss
                if gross_loss > 0
                else (999.0 if gross_profit > 0 else 0.0)
            )

            balance = float(starting_balance)
            peak = balance
            max_dd = 0.0
            total_pnl = 0.0
            for row in closed:
                pnl = self._safe_float(row.pnl)
                balance += pnl
                total_pnl += pnl
                peak = max(peak, balance)
                if peak > 0:
                    dd = (balance - peak) / peak
                    max_dd = min(max_dd, dd)

            open_trades = self._open_count(db)

            return {
                "forward_protocol": "V5.7_GENUINE_FORWARD",
                "forward_trades": len(closed),
                "wins": wins,
                "losses": losses,
                "win_rate": wins / len(closed) if closed else 0.0,
                "profit_factor": round(profit_factor, 4),
                "starting_balance": float(starting_balance),
                "paper_balance": round(balance, 2),
                "total_pnl": round(total_pnl, 2),
                "return_pct": (
                    balance / starting_balance - 1.0
                    if starting_balance > 0
                    else 0.0
                ),
                "max_drawdown": max_dd,
                "open_trades": open_trades,
                "live_execution": False,
            }
        finally:
            db.close()

    def forward_journal(self, limit: int = 100) -> Dict[str, Any]:
        """Audit view of genuine forward trades.

        Persisted trade rows provide the durable outcome record. Rich frozen
        setup snapshots are returned for watchers still present in this server
        process. This endpoint never changes a result.
        """
        db = self.session_factory()
        try:
            rows = (
                db.query(self.Trade)
                .filter(self.Trade.mode == "forward")
                .order_by(self.Trade.created_at.desc())
                .limit(max(1, min(int(limit), 500)))
                .all()
            )

            with self._lock:
                by_trade_id = {
                    item.get("trade_id"): self._public(item)
                    for item in self._watchers.values()
                    if item.get("trade_id") is not None
                }

            entries = []
            for row in rows:
                watcher = by_trade_id.get(row.id)
                entries.append({
                    "trade_id": row.id,
                    "created_at": (
                        row.created_at.isoformat()
                        if getattr(row, "created_at", None) is not None
                        else None
                    ),
                    "symbol": row.symbol,
                    "direction": row.direction,
                    "confidence": row.confidence,
                    "entry_price_effective": row.entry_price,
                    "stake": row.stake,
                    "closed": bool(row.closed),
                    "result": row.result,
                    "pnl": row.pnl,
                    "forward_protocol": "V5.7_GENUINE_FORWARD",
                    "audit": watcher,
                })

            return {
                "version": "5.7.0",
                "forward_protocol": "V5.7_GENUINE_FORWARD",
                "entries": entries,
                "count": len(entries),
                "live_execution": False,
            }
        finally:
            db.close()

    # ------------------------------------------------------------------
    # background loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                with self._lock:
                    ids = list(self._watchers.keys())

                for watcher_id in ids:
                    self._resolve_open(watcher_id)
                    self._evaluate(watcher_id)
            except Exception:
                # One watcher must never kill the daemon loop.
                pass

            self._stop_event.wait(self.LOOP_SECONDS)
