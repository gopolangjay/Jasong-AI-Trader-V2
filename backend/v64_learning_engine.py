from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None


class V64LearningTradeEngine:
    """High-throughput PAPER learning engine.

    This engine is deliberately separate from the strict genuine-forward
    watcher engine so exploratory PAPER trades do not contaminate the strict
    baseline. It supports:

    N30  - normal PAPER path: quantitative confidence >= 30% with direction agreement
    AI40 - AI PAPER path: OpenAI directional confidence >= 40%, approval, and
           agreement with the historically validated direction
    DUAL - both N30 and AI40 pass at the same observation
    S    - shadow observation: rejected/watch-only setup recorded without balance impact

    No broker credentials are accepted and no live order is sent.
    """

    VERSION = "6.5.0"
    NAMESPACE = "v64_learning_engine"
    NORMAL_MIN_CONFIDENCE = 0.30
    AI_MIN_CONFIDENCE = 0.40
    # The old 67% gate is intentionally removed from V6.5 PAPER learning.

    def __init__(
        self,
        *,
        signal_func: Callable[[str, str, float], Dict[str, Any]],
        price_func: Callable[[str], float],
        state_store=None,
        max_watchers: int = 6,
        max_open_trades: int = 3,
        watcher_refresh_seconds: int = 60,
        starting_balance: float = 10000.0,
        payout: float = 0.80,
        default_stake_pct: float = 0.01,
    ):
        self.signal_func = signal_func
        self.price_func = price_func
        self.state_store = state_store
        self.max_watchers = int(max_watchers)
        self.max_open_trades = int(max_open_trades)
        self.watcher_refresh_seconds = max(30, int(watcher_refresh_seconds))
        self.default_stake_pct = float(default_stake_pct)

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._client = None
        self._ai_cache: Dict[str, Dict[str, Any]] = {}

        self._state: Dict[str, Any] = {
            "version": self.VERSION,
            "enabled": True,
            "starting_balance": float(starting_balance),
            "paper_balance": float(starting_balance),
            "payout": float(payout),
            "watchers": [],
            "open_trades": [],
            "settled_trades": [],
            "shadow_open": [],
            "shadow_settled": [],
            "journal": [],
            "last_tick_at": None,
            "ticks": 0,
            "last_error": None,
        }
        self._restore()

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            value = float(value)
            if value != value or value in (float("inf"), float("-inf")):
                return default
            return value
        except (TypeError, ValueError):
            return default

    @classmethod
    def _confidence01(cls, value: Any) -> float:
        number = cls._safe_float(value, 0.0)
        if number > 1.0:
            number /= 100.0
        return max(0.0, min(1.0, number))

    def _restore(self) -> None:
        if self.state_store is None:
            return
        saved = self.state_store.load(self.NAMESPACE, {})
        if isinstance(saved, dict) and saved:
            self._state.update(saved)
        # A process restart cannot leave a tick genuinely in progress.
        self._state["enabled"] = True
        self._state["version"] = self.VERSION

    def _persist(self) -> None:
        if self.state_store is None:
            return
        with self._lock:
            payload = dict(self._state)
        self.state_store.save(self.NAMESPACE, payload)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="jasong-v64-learning-engine",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def enable(self) -> dict:
        with self._lock:
            self._state["enabled"] = True
        self._persist()
        return self.status()

    def pause(self) -> dict:
        with self._lock:
            self._state["enabled"] = False
        self._persist()
        return self.status()

    def submit_candidate(
        self,
        candidate: Dict[str, Any],
        validated: Optional[Dict[str, Any]] = None,
        *,
        risk_mode: str = "Balanced",
        starting_balance: float = 10000.0,
        payout: float = 0.80,
    ) -> Dict[str, Any]:
        """Add/update a learning watcher from the Auto Manager funnel."""
        item = dict(candidate or {})
        validated = dict(validated or {})

        symbol = str(validated.get("symbol") or item.get("symbol") or "").strip()
        direction = str(validated.get("direction") or item.get("direction") or "").upper().strip()
        if not symbol or direction not in {"BUY", "SELL"}:
            return {"accepted": False, "reason": "missing symbol/direction"}

        verified = bool(validated.get("verified", False))
        deep_status = str(validated.get("status") or "NOT_VALIDATED").upper()
        holding_candles = int(validated.get("holding_candles") or item.get("holding_candles") or 4)
        holding_candles = max(1, min(holding_candles, 24))

        now = time.time()
        key = f"{symbol}:{direction}"

        watcher = {
            "watcher_id": str(uuid.uuid4()),
            "key": key,
            "symbol": symbol,
            "market": validated.get("market") or item.get("market") or symbol,
            "direction": direction,
            "risk_mode": risk_mode,
            "verified": verified,
            "deep_status": deep_status,
            "holding_candles": holding_candles,
            "payout": float(payout),
            "candidate": item,
            "validated": validated,
            "created_at": now,
            "last_checked_at": None,
            "last_quant_confidence": None,
            "last_signal": None,
            "last_ai": None,
            "status": "WATCHING" if verified else "SHADOW_WATCH",
            "expires_at": now + 12 * 3600,
        }

        with self._lock:
            watchers = list(self._state.get("watchers", []))
            existing_index = next(
                (i for i, w in enumerate(watchers) if w.get("key") == key),
                None,
            )
            if existing_index is not None:
                old = watchers[existing_index]
                watcher["watcher_id"] = old.get("watcher_id", watcher["watcher_id"])
                watcher["created_at"] = old.get("created_at", now)
                watchers[existing_index] = watcher
            else:
                watchers.append(watcher)

            # Keep best/most recent six; VERIFIED setups get priority.
            watchers.sort(
                key=lambda w: (
                    1 if w.get("verified") else 0,
                    self._safe_float(
                        (w.get("candidate") or {}).get("adaptive_rank_score")
                        or (w.get("candidate") or {}).get("fast_score"),
                        0.0,
                    ),
                    self._safe_float(w.get("created_at"), 0.0),
                ),
                reverse=True,
            )
            self._state["watchers"] = watchers[: self.max_watchers]
            self._state["starting_balance"] = float(starting_balance)
            self._state["payout"] = float(payout)

        self._journal("CANDIDATE_SUBMITTED", {
            "symbol": symbol,
            "direction": direction,
            "verified": verified,
            "deep_status": deep_status,
        })
        self._persist()
        return {"accepted": True, "verified": verified, "symbol": symbol, "direction": direction}

    def _journal(self, event: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            rows = list(self._state.get("journal", []))
            rows.append({"event": event, "timestamp": time.time(), **dict(payload)})
            self._state["journal"] = rows[-2000:]

    def _open_count(self) -> int:
        return len([t for t in self._state.get("open_trades", []) if t.get("status") == "OPEN"])

    def _duplicate_open(self, symbol: str, direction: str) -> bool:
        return any(
            t.get("status") == "OPEN"
            and t.get("symbol") == symbol
            and t.get("direction") == direction
            for t in self._state.get("open_trades", [])
        )

    def _stake(self) -> float:
        balance = max(self._safe_float(self._state.get("paper_balance"), 0.0), 0.0)
        return round(max(1.0, balance * self.default_stake_pct), 2)

    def _ai_assess(
        self,
        watcher: Dict[str, Any],
        live: Dict[str, Any],
        quant_conf: float,
    ) -> Dict[str, Any]:
        key = watcher["key"]
        cached = self._ai_cache.get(key)
        if cached and time.time() - cached.get("timestamp", 0) < 300:
            return dict(cached["result"])

        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key or OpenAI is None:
            return {
                "available": False,
                "direction": None,
                "confidence": 0.0,
                "approve": False,
                "reason": "OpenAI unavailable",
            }

        try:
            if self._client is None:
                self._client = OpenAI(api_key=api_key)

            candidate = watcher.get("candidate") or {}
            validated = watcher.get("validated") or {}
            prompt = {
                "task": "Independent PAPER-trade directional assessment. Do not claim certainty.",
                "symbol": watcher.get("symbol"),
                "historical_direction": watcher.get("direction"),
                "quant_confidence_pct": round(quant_conf * 100.0, 2),
                "live_signal": live.get("direction") or live.get("signal"),
                "price": live.get("price"),
                "rsi": live.get("rsi") or candidate.get("rsi"),
                "fast_score": candidate.get("fast_score"),
                "quality_tier": candidate.get("quality_tier"),
                "deep_status": watcher.get("deep_status"),
                "historical_win_rate": validated.get("win_rate"),
                "historical_profit_factor": validated.get("profit_factor"),
                "historical_drawdown": validated.get("max_drawdown"),
                "instruction": (
                    "Return JSON only with direction BUY/SELL/WAIT, confidence 0-100, "
                    "approve boolean, and a short reason. Confidence is your directional "
                    "assessment, not a guarantee."
                ),
            }
            response = self._client.responses.create(
                model=os.getenv("JASONG_LEARNING_AI_MODEL", "gpt-5-mini"),
                input=json.dumps(prompt, default=str),
            )
            text = (response.output_text or "").strip()
            if text.startswith("```"):
                text = text.strip("`")
                if text.lower().startswith("json"):
                    text = text[4:].strip()
            parsed = json.loads(text)
            result = {
                "available": True,
                "direction": str(parsed.get("direction") or "WAIT").upper(),
                "confidence": self._confidence01(parsed.get("confidence")),
                "approve": bool(parsed.get("approve", False)),
                "reason": str(parsed.get("reason") or "")[:500],
            }
        except Exception as exc:
            result = {
                "available": False,
                "direction": None,
                "confidence": 0.0,
                "approve": False,
                "reason": f"AI assessment failed: {exc}",
            }

        self._ai_cache[key] = {"timestamp": time.time(), "result": dict(result)}
        return result

    def _entry_class(
        self,
        watcher: Dict[str, Any],
        live: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Classify one V6.5 PAPER learning opportunity.

        The old 67% confidence gate is not used.

        Actual PAPER eligibility requires a VERIFIED historical setup and can
        pass through either:
          - NORMAL_30: live quantitative direction matches and confidence >=30%
          - AI_40: AI direction agrees, confidence >=40%, and approve is true

        Both paths passing at once are labelled DUAL. Everything else becomes a
        shadow observation so we can later measure rejected risk and opportunity
        cost without changing the PAPER balance.
        """

        wanted = str(
            watcher.get("direction")
            or ""
        ).upper()

        live_direction = str(
            live.get("direction")
            or live.get("signal")
            or "WAIT"
        ).upper()

        quant = self._confidence01(
            live.get("confidence")
        )

        verified = bool(
            watcher.get("verified")
        )

        normal_pass = (
            verified
            and live_direction == wanted
            and quant >= self.NORMAL_MIN_CONFIDENCE
        )

        ai = (
            self._ai_assess(
                watcher,
                live,
                quant,
            )
            if verified
            else {
                "available": False,
                "direction": None,
                "confidence": 0.0,
                "approve": False,
                "reason": "Historical setup not VERIFIED",
            }
        )

        ai_confidence = self._confidence01(
            ai.get("confidence")
        )

        ai_pass = (
            verified
            and bool(ai.get("available"))
            and str(
                ai.get("direction")
                or ""
            ).upper()
            == wanted
            and ai_confidence
            >= self.AI_MIN_CONFIDENCE
            and bool(ai.get("approve"))
        )

        if normal_pass and ai_pass:
            return {
                "class": "DUAL",
                "entry_path": "NORMAL_30_PLUS_AI_40",
                "enter": True,
                "quant": quant,
                "ai": ai,
                "normal_pass": True,
                "ai_pass": True,
                "reason": (
                    "Both V6.5 PAPER gates passed: normal >=30% with "
                    "direction agreement and AI >=40% with approval."
                ),
            }

        if normal_pass:
            return {
                "class": "N30",
                "entry_path": "NORMAL_30",
                "enter": True,
                "quant": quant,
                "ai": ai,
                "normal_pass": True,
                "ai_pass": False,
                "reason": (
                    "V6.5 normal PAPER gate passed: confidence >=30% "
                    "and live direction matches the verified setup."
                ),
            }

        if ai_pass:
            return {
                "class": "AI40",
                "entry_path": "AI_40",
                "enter": True,
                "quant": quant,
                "ai": ai,
                "normal_pass": False,
                "ai_pass": True,
                "reason": (
                    "V6.5 AI PAPER gate passed: AI confidence >=40%, "
                    "AI approved, and AI direction matches the verified setup."
                ),
            }

        reasons = []

        if not verified:
            reasons.append(
                "historical setup not VERIFIED"
            )

        if live_direction != wanted:
            reasons.append(
                f"live direction {live_direction} does not match {wanted}"
            )

        if quant < self.NORMAL_MIN_CONFIDENCE:
            reasons.append(
                f"quant confidence {quant * 100:.1f}% below 30%"
            )

        if not bool(ai.get("available")):
            reasons.append(
                "AI assessment unavailable"
            )
        else:
            if str(
                ai.get("direction")
                or ""
            ).upper() != wanted:
                reasons.append(
                    "AI direction does not match verified setup"
                )

            if ai_confidence < self.AI_MIN_CONFIDENCE:
                reasons.append(
                    f"AI confidence {ai_confidence * 100:.1f}% below 40%"
                )

            if not bool(ai.get("approve")):
                reasons.append(
                    "AI did not approve"
                )

        return {
            "class": "S",
            "entry_path": "SHADOW_REJECT",
            "enter": False,
            "quant": quant,
            "ai": ai,
            "normal_pass": False,
            "ai_pass": False,
            "reason": (
                "; ".join(reasons)
                if reasons
                else "Shadow-only risk observation"
            ),
        }

    def _risk_snapshot(
        self,
        watcher: Dict[str, Any],
        live: Dict[str, Any],
        decision: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Freeze observable risk factors at the entry/rejection timestamp."""

        candidate = watcher.get("candidate") or {}
        validated = watcher.get("validated") or {}
        ai = decision.get("ai") or {}

        return {
            "captured_at": time.time(),
            "verified": bool(watcher.get("verified")),
            "deep_status": watcher.get("deep_status"),
            "wanted_direction": watcher.get("direction"),
            "live_direction": (
                live.get("direction")
                or live.get("signal")
                or "WAIT"
            ),
            "quant_confidence": decision.get("quant"),
            "quant_confidence_pct": round(
                self._confidence01(
                    decision.get("quant")
                ) * 100.0,
                2,
            ),
            "normal_threshold": self.NORMAL_MIN_CONFIDENCE,
            "normal_pass": bool(
                decision.get("normal_pass", False)
            ),
            "ai_available": bool(ai.get("available")),
            "ai_direction": ai.get("direction"),
            "ai_confidence": self._confidence01(
                ai.get("confidence")
            ),
            "ai_confidence_pct": round(
                self._confidence01(
                    ai.get("confidence")
                ) * 100.0,
                2,
            ),
            "ai_threshold": self.AI_MIN_CONFIDENCE,
            "ai_approve": bool(ai.get("approve")),
            "ai_pass": bool(
                decision.get("ai_pass", False)
            ),
            "ai_reason": ai.get("reason"),
            "entry_path": decision.get("entry_path"),
            "rsi": (
                live.get("rsi")
                or candidate.get("rsi")
            ),
            "price": live.get("price"),
            "fast_score": candidate.get("fast_score"),
            "smart_fast_score": candidate.get(
                "smart_fast_score"
            ),
            "adaptive_rank_score": candidate.get(
                "adaptive_rank_score"
            ),
            "quality_tier": candidate.get("quality_tier"),
            "historical_win_rate": validated.get("win_rate"),
            "historical_profit_factor": validated.get(
                "profit_factor"
            ),
            "historical_drawdown": validated.get(
                "max_drawdown"
            ),
            "historical_trades": validated.get("trades"),
            "historical_interval": validated.get("interval"),
            "historical_holding_candles": validated.get(
                "holding_candles"
            ),
        }

    def _open_trade(
        self,
        watcher: Dict[str, Any],
        live: Dict[str, Any],
        decision: Dict[str, Any],
    ) -> None:
        if self._open_count() >= self.max_open_trades:
            return
        symbol = watcher["symbol"]
        direction = watcher["direction"]
        if self._duplicate_open(symbol, direction):
            return

        price = self._safe_float(live.get("price"), 0.0)
        if price <= 0:
            try:
                price = float(self.price_func(symbol))
            except Exception:
                return

        now = time.time()
        holding = max(1, int(watcher.get("holding_candles") or 4))
        trade = {
            "trade_id": str(uuid.uuid4()),
            "entry_class": decision["class"],
            "entry_path": decision.get("entry_path"),
            "symbol": symbol,
            "market": watcher.get("market"),
            "direction": direction,
            "quant_confidence": decision.get("quant"),
            "ai": decision.get("ai"),
            "entry_price": price,
            "stake": self._stake(),
            "opened_at": now,
            "scheduled_close_at": now + holding * 15 * 60,
            "holding_candles": holding,
            "status": "OPEN",
            "result": None,
            "pnl": None,
            "exit_price": None,
            "closed_at": None,
            "reason": decision.get("reason"),
            "risk_snapshot": self._risk_snapshot(
                watcher,
                live,
                decision,
            ),
        }
        with self._lock:
            self._state["open_trades"].append(trade)
        self._journal("LEARNING_TRADE_OPENED", trade)

    def _open_shadow(
        self,
        watcher: Dict[str, Any],
        live: Dict[str, Any],
        decision: Dict[str, Any],
    ) -> None:
        symbol = watcher["symbol"]
        direction = watcher["direction"]
        if any(
            t.get("status") == "OPEN"
            and t.get("symbol") == symbol
            and t.get("direction") == direction
            for t in self._state.get("shadow_open", [])
        ):
            return
        price = self._safe_float(live.get("price"), 0.0)
        if price <= 0:
            try:
                price = float(self.price_func(symbol))
            except Exception:
                return
        now = time.time()
        holding = max(1, int(watcher.get("holding_candles") or 4))
        shadow = {
            "trade_id": str(uuid.uuid4()),
            "entry_class": "S",
            "entry_path": decision.get(
                "entry_path",
                "SHADOW_REJECT",
            ),
            "symbol": symbol,
            "market": watcher.get("market"),
            "direction": direction,
            "quant_confidence": decision.get("quant"),
            "ai": decision.get("ai"),
            "entry_price": price,
            "stake": 0.0,
            "opened_at": now,
            "scheduled_close_at": now + holding * 15 * 60,
            "holding_candles": holding,
            "status": "OPEN",
            "result": None,
            "pnl": 0.0,
            "reason": decision.get("reason"),
            "risk_snapshot": self._risk_snapshot(
                watcher,
                live,
                decision,
            ),
        }
        with self._lock:
            self._state["shadow_open"].append(shadow)
            self._state["shadow_open"] = self._state["shadow_open"][-30:]
        self._journal("SHADOW_TRADE_OPENED", shadow)

    def _settle_list(self, key_open: str, key_settled: str, affect_balance: bool) -> None:
        now = time.time()
        with self._lock:
            rows = list(self._state.get(key_open, []))
        remaining = []
        settled_now = []
        for trade in rows:
            if trade.get("status") != "OPEN" or now < self._safe_float(trade.get("scheduled_close_at"), now + 1):
                remaining.append(trade)
                continue
            try:
                exit_price = float(self.price_func(trade["symbol"]))
            except Exception:
                remaining.append(trade)
                continue
            entry = self._safe_float(trade.get("entry_price"), 0.0)
            direction = str(trade.get("direction") or "").upper()
            won = exit_price > entry if direction == "BUY" else exit_price < entry
            stake = self._safe_float(trade.get("stake"), 0.0)
            payout = self._safe_float(self._state.get("payout"), 0.80)
            raw_pnl = (
                stake * payout
                if won
                else -stake
            )

            if affect_balance:
                pnl = raw_pnl
                hypothetical_pnl = raw_pnl
            else:
                # Shadow observations do not change the PAPER balance, but we
                # retain a one-unit counterfactual payoff for risk analysis.
                hypothetical_pnl = (
                    payout
                    if won
                    else -1.0
                )
                pnl = 0.0

            trade.update({
                "status": "CLOSED",
                "result": "WIN" if won else "LOSS",
                "exit_price": exit_price,
                "closed_at": now,
                "pnl": round(pnl, 2),
                "hypothetical_pnl": round(
                    hypothetical_pnl,
                    4,
                ),
            })
            settled_now.append(trade)
            self._journal("LEARNING_TRADE_SETTLED" if affect_balance else "SHADOW_TRADE_SETTLED", trade)

        with self._lock:
            self._state[key_open] = remaining
            settled = list(self._state.get(key_settled, [])) + settled_now
            self._state[key_settled] = settled[-2000:]
            if affect_balance:
                self._state["paper_balance"] = round(
                    self._safe_float(self._state.get("paper_balance"), 0.0)
                    + sum(self._safe_float(t.get("pnl"), 0.0) for t in settled_now),
                    2,
                )

    def tick(self) -> Dict[str, Any]:
        with self._lock:
            if not self._state.get("enabled", True):
                return self.status()
            watchers = list(self._state.get("watchers", []))

        now = time.time()
        fresh_watchers = []
        for watcher in watchers:
            if now >= self._safe_float(watcher.get("expires_at"), now + 1):
                continue
            try:
                live = self.signal_func(
                    watcher["symbol"],
                    watcher.get("risk_mode", "Balanced"),
                    self._safe_float(self._state.get("paper_balance"), 10000.0),
                )
                if "price" not in live or self._safe_float(live.get("price"), 0.0) <= 0:
                    live["price"] = float(self.price_func(watcher["symbol"]))
                decision = self._entry_class(watcher, live)
                watcher["last_checked_at"] = now
                watcher["last_quant_confidence"] = decision.get("quant")
                watcher["last_signal"] = live.get("direction") or live.get("signal")
                watcher["last_ai"] = decision.get("ai")
                if decision.get("enter"):
                    self._open_trade(watcher, live, decision)
                else:
                    # Create a counterfactual outcome so rejected gates teach us too.
                    self._open_shadow(watcher, live, decision)
                fresh_watchers.append(watcher)
            except Exception as exc:
                watcher["last_error"] = str(exc)
                watcher["last_checked_at"] = now
                fresh_watchers.append(watcher)

        with self._lock:
            self._state["watchers"] = fresh_watchers[: self.max_watchers]

        self._settle_list("open_trades", "settled_trades", True)
        self._settle_list("shadow_open", "shadow_settled", False)

        with self._lock:
            self._state["last_tick_at"] = now
            self._state["ticks"] = int(self._state.get("ticks", 0)) + 1
            self._state["last_error"] = None
        self._persist()
        return self.status()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self._state.get("enabled", True):
                    self.tick()
            except Exception as exc:
                with self._lock:
                    self._state["last_error"] = str(exc)
                self._persist()
            self._stop_event.wait(self.watcher_refresh_seconds)

    def _stats_for(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        closed = [r for r in rows if r.get("status") == "CLOSED" and r.get("result") in {"WIN", "LOSS"}]
        wins = sum(1 for r in closed if r.get("result") == "WIN")
        losses = len(closed) - wins
        gross_profit = sum(max(self._safe_float(r.get("pnl"), 0.0), 0.0) for r in closed)
        gross_loss = abs(sum(min(self._safe_float(r.get("pnl"), 0.0), 0.0) for r in closed))
        return {
            "trades": len(closed),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / len(closed), 6) if closed else 0.0,
            "win_rate_pct": round((wins / len(closed)) * 100.0, 2) if closed else 0.0,
            "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else (99.0 if gross_profit > 0 else 0.0),
            "total_pnl": round(sum(self._safe_float(r.get("pnl"), 0.0) for r in closed), 2),
        }

    def status(self) -> Dict[str, Any]:
        with self._lock:
            state = dict(self._state)
            actual = list(state.get("settled_trades", []))
            shadow = list(state.get("shadow_settled", []))
            open_trades = list(state.get("open_trades", []))
            watchers = list(state.get("watchers", []))

        classes = {}
        for cls in ["N30", "AI40", "DUAL"]:
            classes[cls] = self._stats_for([t for t in actual if t.get("entry_class") == cls])

        # Shadow P&L is deliberately zero, so calculate only outcome frequency.
        shadow_wins = sum(1 for t in shadow if t.get("result") == "WIN")
        shadow_stats = {
            "trades": len(shadow),
            "wins": shadow_wins,
            "losses": len(shadow) - shadow_wins,
            "win_rate": round(shadow_wins / len(shadow), 6) if shadow else 0.0,
            "win_rate_pct": round((shadow_wins / len(shadow)) * 100.0, 2) if shadow else 0.0,
        }

        buckets = {}
        bucket_defs = [
            ("0-29", 0.0, 0.30),
            ("30-34", 0.30, 0.35),
            ("35-39", 0.35, 0.40),
            ("40-49", 0.40, 0.50),
            ("50-59", 0.50, 0.60),
            ("60+", 0.60, 1.01),
        ]
        all_outcomes = actual + shadow
        for name, low, high in bucket_defs:
            rows = [
                t
                for t in all_outcomes
                if (
                    low
                    <= self._confidence01(
                        t.get("quant_confidence")
                    )
                    < high
                    and t.get("result")
                    in {"WIN", "LOSS"}
                )
            ]

            wins = sum(
                1
                for t in rows
                if t.get("result") == "WIN"
            )

            hypothetical_values = [
                self._safe_float(
                    t.get(
                        "hypothetical_pnl",
                        t.get("pnl"),
                    ),
                    0.0,
                )
                for t in rows
            ]

            gross_hyp_profit = sum(
                max(v, 0.0)
                for v in hypothetical_values
            )

            gross_hyp_loss = abs(
                sum(
                    min(v, 0.0)
                    for v in hypothetical_values
                )
            )

            hypothetical_pf = (
                gross_hyp_profit / gross_hyp_loss
                if gross_hyp_loss > 0
                else (
                    99.0
                    if gross_hyp_profit > 0
                    else 0.0
                )
            )

            buckets[name] = {
                "trades": len(rows),
                "actual_trades": sum(
                    1
                    for t in rows
                    if t in actual
                ),
                "shadow_observations": sum(
                    1
                    for t in rows
                    if t in shadow
                ),
                "wins": wins,
                "losses": len(rows) - wins,
                "win_rate_pct": (
                    round(
                        wins / len(rows) * 100.0,
                        2,
                    )
                    if rows
                    else 0.0
                ),
                "hypothetical_profit_factor": round(
                    hypothetical_pf,
                    4,
                ),
            }

        ai_bucket_defs = [
            ("0-39", 0.0, 0.40),
            ("40-49", 0.40, 0.50),
            ("50-59", 0.50, 0.60),
            ("60-69", 0.60, 0.70),
            ("70+", 0.70, 1.01),
        ]

        ai_buckets = {}

        for name, low, high in ai_bucket_defs:
            rows = []

            for trade in all_outcomes:
                ai = trade.get("ai") or {}
                ai_conf = self._confidence01(
                    ai.get("confidence")
                )

                if (
                    low <= ai_conf < high
                    and trade.get("result")
                    in {"WIN", "LOSS"}
                ):
                    rows.append(trade)

            wins = sum(
                1
                for trade in rows
                if trade.get("result")
                == "WIN"
            )

            ai_buckets[name] = {
                "observations": len(rows),
                "wins": wins,
                "losses": len(rows) - wins,
                "win_rate_pct": (
                    round(
                        wins / len(rows) * 100.0,
                        2,
                    )
                    if rows
                    else 0.0
                ),
            }

        return {
            "version": self.VERSION,
            "enabled": bool(state.get("enabled", True)),
            "paper_only": True,
            "live_execution": False,
            "threshold_policy": {
                "normal_min_confidence": self.NORMAL_MIN_CONFIDENCE,
                "normal_min_confidence_pct": 30.0,
                "ai_min_confidence": self.AI_MIN_CONFIDENCE,
                "ai_min_confidence_pct": 40.0,
                "legacy_67_gate_active": False,
                "entry_rule": (
                    "VERIFIED setup: NORMAL>=30% with direction match "
                    "OR AI>=40% with approval and direction agreement"
                ),
            },
            "max_watchers": self.max_watchers,
            "active_watchers": len(watchers),
            "watcher_refresh_seconds": self.watcher_refresh_seconds,
            "max_open_trades": self.max_open_trades,
            "open_trades": len(open_trades),
            "paper_balance": state.get("paper_balance"),
            "starting_balance": state.get("starting_balance"),
            "ticks": state.get("ticks"),
            "last_tick_at": state.get("last_tick_at"),
            "last_error": state.get("last_error"),
            "actual": self._stats_for(actual),
            "by_entry_class": classes,
            "shadow": shadow_stats,
            "confidence_buckets": buckets,
            "ai_confidence_buckets": ai_buckets,
        }

    def journal(self, limit: int = 200) -> Dict[str, Any]:
        with self._lock:
            rows = list(self._state.get("journal", []))
        return {
            "version": self.VERSION,
            "entries": rows[-max(1, min(int(limit), 2000)):],
            "count": len(rows),
            "live_execution": False,
        }

    def watchers(self) -> Dict[str, Any]:
        with self._lock:
            rows = list(self._state.get("watchers", []))
        return {"version": self.VERSION, "watchers": rows, "count": len(rows), "live_execution": False}
