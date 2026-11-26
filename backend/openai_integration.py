"""OpenAI entry selection with deterministic, protected IG DEMO execution."""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import threading
import time

import pandas as pd

import category_strategy_engine as strategy_base
from category_strategy_engine import CategoryStrategyEngine, CATEGORY_RULES, ACTIVE_EXECUTION_KEYS
from category_execution_engine import CategoryExecutionEngine
from prime_policy import ForwardPrimeArchitecture
from regime_strategy import completed_frames, features, STRATEGY_IDS as REGIME_IDS
from regime_integration import current_session
from forex_liquidity_lines_strategy import news_blackout_context
from openai_analyst import OpenAIAnalyst, AnalystUnavailable
from openai_policy import STRATEGY_ID, STRATEGY_NAME, policy_status as base_policy
from runtime_version import VERSION
from ai_demo_execution import DiscretionaryDemoMixin, entry_reasons as discretionary_reasons

AI_ACTIVE_KEYS = (*ACTIVE_EXECUTION_KEYS, "BITCOIN")


def policy_status(discretionary=None):
    return {**base_policy(discretionary), "active_symbols": list(AI_ACTIVE_KEYS)}


def _seconds(value):
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        return float(value)
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError("timestamp must be explicit UTC")
    return stamp.timestamp()


def execution_reasons(row, *, now=None):
    now = time.time() if now is None else _seconds(now)
    if row.get("openai_validated") is not True or row.get("direction") not in {"BUY", "SELL"}:
        return [str(row.get("openai_wait_reason") or "OPENAI_WAIT")]
    reasons = []
    try:
        if not 0 <= now - _seconds(row.get("signal_timestamp")) <= 300:
            reasons.append("OPENAI_DECISION_STALE")
        if not 0 <= now - _seconds(row.get("analysis_price_timestamp")) <= 1200:
            reasons.append("ANALYSIS_CANDLES_STALE")
    except (ValueError, TypeError, OverflowError):
        reasons.append("ANALYSIS_TIMESTAMP_INVALID")
    try:
        if not 0 <= now - _seconds(row.get("ig_quote_timestamp")) <= 180:
            reasons.append("BROKER_QUOTE_STALE")
        bid, offer = float(row["ig_bid"]), float(row["ig_offer"])
        if not (math.isfinite(bid) and math.isfinite(offer) and 0 < bid < offer):
            reasons.append("BROKER_QUOTE_INVALID")
        else:
            bps = (offer-bid)/((offer+bid)/2)*10000
            if bps > float(row["spread_gate_bps"]):
                reasons.append("BROKER_SPREAD_COST_GUARD")
    except (ValueError, TypeError, KeyError, OverflowError):
        reasons.append("BROKER_QUOTE_UNAVAILABLE")
    if row.get("ig_tradeable") is not True or row.get("ig_market_status") != "TRADEABLE":
        reasons.append("IG_NOT_TRADEABLE")
    if not row.get("ig_epic") or not row.get("setup_id"):
        reasons.append("BROKER_IDENTITY_OR_DECISION_ID_MISSING")
    news = row.get("news_guard") or {}
    if news.get("required") and not news.get("clear"):
        reasons.append("CONFIGURED_HIGH_IMPACT_NEWS_GUARD")
    try:
        if not all(math.isfinite(float(row[k])) and float(row[k]) > 0
                   for k in ("live_price", "structural_stop_distance", "target_r")):
            reasons.append("PROTECTIVE_LEVELS_INVALID")
    except (KeyError, TypeError, ValueError, OverflowError):
        reasons.append("PROTECTIVE_LEVELS_INVALID")
    return list(dict.fromkeys(reasons))


def demo_only(broker):
    try:
        status = broker.status()
        return (status.get("environment") == "DEMO" and status.get("live_money_execution") is False
                and str(status.get("base_url") or getattr(broker, "BASE_URL", "")) == "https://demo-api.ig.com/gateway/deal")
    except Exception:
        return False


class OpenAIStrategyEngine(CategoryStrategyEngine):
    VERSION = VERSION
    ACTIVE_EXECUTION_KEYS = AI_ACTIVE_KEYS

    def __init__(self, *, analyst=None, now_func=None, discretionary=False, **kwargs):
        self.discretionary = bool(discretionary)
        self.position_source = lambda: []
        self._now = now_func or (lambda: pd.Timestamp.now(tz="UTC"))
        self.analyst = analyst or OpenAIAnalyst(state_path=os.getenv(
            "OPENAI_ANALYST_BUDGET_PATH", str(Path(kwargs["state_path"]).with_name("jasong_openai_budget.json"))),
            discretionary=self.discretionary)
        self._analysis_lock = threading.Lock()
        self._analysis_period = None
        self._frames = {}
        self._decision = None
        self._data_errors = {}
        self._analysis_running = False
        super().__init__(**kwargs)

    @staticmethod
    def _snapshot(seed, frames):
        m15 = frames["M15"]
        if len(m15) < 32 or len(frames["H1"]) < 4:
            raise ValueError("INSUFFICIENT_CLOSED_PRICE_EVIDENCE")
        snapshot = {"symbol": seed["key"], "category": seed["category"], "reference_price": float(m15.Close.iloc[-1]),
                    "last_completed_at": (m15.index[-1]+pd.Timedelta(minutes=15)).isoformat(),
                    "volume_unknown": bool(m15.Volume.tail(24).sum() == 0),
                    "candle_fields": ["open_time_utc_epoch", "open", "high", "low", "close", "volume"],
                    "candles": {}}
        for timeframe, count in (("M15", 24), ("H1", 12), ("H4", 8)):
            snapshot["candles"][timeframe] = [[int(t.timestamp()), *[round(float(r[c]), 8) for c in ("Open", "High", "Low", "Close", "Volume")]]
                                                for t, r in frames[timeframe].tail(count).iterrows()]
        return snapshot

    def _ensure_analysis(self, now):
        period = now.floor("15min").isoformat()
        if self._analysis_period == period or not self._analysis_lock.acquire(blocking=False):
            return
        try:
            if self._analysis_period == period:
                return
            self._analysis_period = period
            self._analysis_running = True
            self._decision = None
            self._frames = {}
            self._data_errors = {}
            if not demo_only(self.broker):
                self.analyst.failure("IG_DEMO_REQUIRED")
                return
            if not self.analyst.configured():
                self.analyst.failure("OPENAI_API_KEY_NOT_CONFIGURED")
                return
            snapshots, collected = [], {}
            for seed in self._active_market_seeds():
                symbol = seed["key"]
                try:
                    raw = self.frame_func(seed)
                    frames = completed_frames(raw, now)
                    snapshot = self._snapshot(seed, frames)
                    age = (now-pd.Timestamp(snapshot["last_completed_at"])).total_seconds()
                    if not 0 <= age <= 1200:
                        raise ValueError("STALE_ANALYSIS")
                    snapshots.append(snapshot)
                    collected[symbol] = frames
                except Exception as exc:
                    # Source errors may contain keyed URLs; expose only the error type.
                    self._data_errors[symbol] = f"MARKET_EVIDENCE_UNAVAILABLE:{type(exc).__name__}"
            self._frames = collected
            self._decision = self.analyst.analyse(snapshots, as_of=now.isoformat(),
                **({"positions": self.position_source()} if self.discretionary else {}))
        except AnalystUnavailable:
            self._decision = None
        except Exception:
            self.analyst.failure("OPENAI_REFRESH_FAILED")
            self._decision = None
        finally:
            self._analysis_running = False
            self._analysis_lock.release()

    def decision_snapshot(self):
        if not self._analysis_lock.acquire(blocking=False):
            return None
        try:
            return dict(self._decision) if self._decision else None
        finally:
            self._analysis_lock.release()

    def _resolve_execution_market(self, seed):
        if seed["key"] == "BITCOIN":
            # Retain the identity already verified with the connected IG DEMO
            # account; the fresh instrument name and tradeability are checked below.
            return {"epic": "CS.D.BITCOIN.CFBMU.IP", "name": "Bitcoin"}
        return super()._resolve_execution_market(seed)

    def _evaluate_seed(self, seed):
        symbol, category = seed["key"], seed["category"]
        if symbol not in self.ACTIVE_EXECUTION_KEYS:
            return super()._evaluate_seed(seed)
        now = self._now()
        self._ensure_analysis(now)
        # Snapshot the decision and its exact evidence together; another scanner
        # must not combine an old model decision with a new candle batch.
        available = self._analysis_lock.acquire(blocking=False)
        try:
            decision = (self._decision or {}) if available else {}
            frames = self._frames.get(symbol) if available else None
        finally:
            if available:
                self._analysis_lock.release()
        proposals = decision.get("proposals") or []
        selected = next((p for p in proposals if p["symbol"] == symbol), None)
        status = self.analyst.status()
        row = {**seed, "version": self.VERSION, "symbol": symbol, "market": seed["name"], "direction": "WAIT",
               "strategy_id": STRATEGY_ID, "strategy_name": STRATEGY_NAME, "strategy_definition_version": self.VERSION,
               "strategy_module_version": self.VERSION, "strategy_selection_mode": "OPENAI_ANALYST",
               "evidence_schema_version": strategy_base.EVIDENCE_SCHEMA_VERSION, "current_candle_strategy_complete": bool(frames),
               "openai_analysis_complete": bool(decision),
               "optimizer_complete": True, "walk_forward_complete": True, "regime": "OPENAI_ANALYSIS",
               "analysis_source": "OPENAI_ON_CLOSED_M15_H1_H4", "openai_validated": False,
               "openai_wait_reason": self._data_errors.get(symbol) or status.get("last_error") or "OPENAI_WAIT",
               "openai_analysis": {"model": status.get("model"), "response_id": decision.get("response_id"),
                                   "context_id": decision.get("context_id"), "reason": (selected or {}).get("reason") or status.get("wait_reason")},
               "signal_qualified": False, "evaluated_at": time.time(), "ig_tradeable": False, "ig_epic": None,
               "ig_quote_source": "NOT_REQUESTED", "spread_pass": False, "liquidity_pass": False,
               "spread_gate_bps": CATEGORY_RULES[category]["spread_gate_bps"], "compound_eligible": False,
               "historical_validation_mode": "INFORMATIONAL_ONLY", "historical_execution_veto": False,
               "model_ai_confidence": None, "quant_confidence": None, "quant_confidence_pct": None,
               "confidence_basis": "OPENAI_DECISION_IS_NOT_WIN_PROBABILITY", "smart_fast_score": 0,
               "model_ai_confidence_source": "OPENAI_DECISION_NO_PROBABILITY", "live_money_execution": False}
        session = ({"active": True, "name": "BROKER_MARKET_HOURS"} if category == "CRYPTO" else current_session(symbol, now))
        row.update({"session": session, "session_active": session["active"], "session_name": session["name"],
                    "geographic_session_is_entry_gate": False,
                    "news_guard": news_blackout_context(now, "BTCUSD" if symbol == "BITCOIN" else "XAUUSD" if symbol == "GOLD" else symbol)})
        if frames:
            m15 = frames["M15"]
            row.update({"analysis_price_timestamp": (m15.index[-1]+pd.Timedelta(minutes=15)).isoformat(),
                        "live_price": float(m15.Close.iloc[-1]), "recent_returns": m15.Close.pct_change().dropna().tail(160).tolist()})
        if selected and frames:
            reference = row["live_price"]
            sign = 1 if selected["direction"] == "BUY" else -1
            stop, target = selected.get("stop_price"), selected.get("target_price")
            distance = sign*(reference-stop) if stop is not None else None
            reward = sign*(target-reference) if target is not None else None
            row.update({"direction": selected["direction"], "live_direction": selected["direction"],
                        "openai_validated": True, "signal_qualified": True,
                        "signal_timestamp": pd.Timestamp(decision["created_at"], unit="s", tz="UTC").isoformat(),
                        "setup_id": hashlib.sha256((STRATEGY_ID+symbol+decision["context_id"]).encode()).hexdigest(),
                        "structural_stop_distance": distance,
                        "target_r": reward/distance if distance and reward is not None else None,
                        "max_hold_seconds": selected["holding_minutes"]*60 if selected.get("holding_minutes") else None,
                        "openai_size": selected.get("size"), "model_stop_price": stop,
                        "model_target_price": target, "model_holding_minutes": selected.get("holding_minutes"),
                        "openai_priority": proposals.index(selected)+1,
                        "smart_fast_score": 100-proposals.index(selected), "category_rank_score": 100-proposals.index(selected)})
            try:
                market = self._resolve_execution_market(seed)
                epic = market.get("epic")
                if self.discretionary:
                    row.update(ig_epic=epic, ig_market_name=market.get("name"),
                               application_quote_preflight=False, execution_policy="AI_DISCRETIONARY_DEMO")
                else:
                    details = self.broker.market_details(epic, require_quote=True)
                    if symbol == "BITCOIN" and "BITCOIN" not in str((details.get("instrument") or {}).get("name") or "").upper():
                        raise ValueError("BITCOIN_INSTRUMENT_IDENTITY_MISMATCH")
                    quote = (self.broker.extract_snapshot_quote(details) if hasattr(self.broker, "extract_snapshot_quote")
                             else {**(details.get("snapshot") or {}), "source": "FRESH_EPIC_SNAPSHOT"})
                    snapshot = details.get("snapshot") or {}
                    bid, offer = float(quote["bid"]), float(quote["offer"])
                    bps = (offer-bid)/((offer+bid)/2)*10000
                    row.update({"ig_epic": epic, "ig_market_name": market.get("name"),
                                "ig_market_status": str(snapshot.get("marketStatus") or "").upper(),
                                "ig_tradeable": str(snapshot.get("marketStatus") or "").upper() == "TRADEABLE",
                                "ig_bid": bid, "ig_offer": offer, "ig_quote_source": quote.get("source") or "FRESH_EPIC_SNAPSHOT",
                                "ig_quote_timestamp": time.time(), "ig_spread_bps": bps, "spread_bps": bps,
                                "spread_pass": math.isfinite(bps) and 0 < bps <= row["spread_gate_bps"],
                                "liquidity_pass": math.isfinite(bid) and math.isfinite(offer) and 0 < bid < offer})
            except Exception as exc:
                row["ig_preflight_error"] = f"BROKER_PREFLIGHT_FAILED:{type(exc).__name__}"
        # Analysis and broker requests can advance the clock. Validate against
        # completion time so a freshly received quote is not compared with the
        # timestamp captured before I/O; genuine future/stale values still fail.
        reasons = (discretionary_reasons(row, self._now()) if self.discretionary
                   else execution_reasons(row, now=self._now()))
        row.update({"rejection_reasons": reasons, "standard_eligible": not reasons, "trade_eligible": not reasons,
                    "execution_safety_pass": not reasons, "direction_match": row["direction"] in {"BUY", "SELL"},
                    "quality_tier": "AI_SELECTED" if not reasons else None, "deep_status": "VERIFIED" if not reasons else "OBSERVE"})
        return row

    def universe(self, category=None):
        return [{**r, "execution_policy": "OPENAI_ANALYST" if r["execution_active"] else r["execution_policy"]}
                for r in super().universe(category)]

    def analyst_status(self):
        return {**self.analyst.status(), "analysis_running": self._analysis_running,
                "current_period": self._analysis_period, "fresh_input_markets": len(self._frames),
                "data_errors_by_market": dict(self._data_errors), "policy": policy_status(self.discretionary)}

    def status(self):
        out = super().status()
        out.update({"version": VERSION, "name": "JASONG OPENAI DEMO ANALYST", "analysis_mode": "OPENAI",
                    "openai_analyst": self.analyst_status(), "regime_policy": policy_status(self.discretionary),
                    "strategy_sequence": {"setup": "OpenAI BUY/SELL/WAIT from closed-candle evidence",
                                          "then": "AI-selected DEMO market order and model-directed closes" if self.discretionary
                                          else "Fresh broker quote -> costs -> account risk -> attached stop/target"},
                    "confidence_policy": {"basis": "OPENAI_DECISION_NOT_WIN_PROBABILITY", "fixed_thresholds": False}})
        for category in ("FOREX", "METALS", "CRYPTO"):
            info = out.get("categories", {}).get(category, {})
            info.update({"strategy": STRATEGY_NAME, "strategy_id": STRATEGY_ID, "selection_mode": "OPENAI_ANALYST"})
        return out

    def optimizer_summary(self):
        return {"version": VERSION, "method": "OPENAI_STRUCTURED_PRICE_ANALYSIS", "policy": policy_status(),
                "coverage": self.evidence_coverage(), "historical_execution_veto": False, "live_money_execution": False}


class OpenAIPrimeArchitecture(ForwardPrimeArchitecture):
    VERSION = VERSION

    def __init__(self, *, discretionary=False, **kwargs):
        self.discretionary = bool(discretionary)
        super().__init__(**kwargs)

    def _strong_gate(self, row, provenance):
        if row.get("strategy_id") != STRATEGY_ID:
            return False, ["RETIRED_ENTRY_STRATEGY"]
        reasons = (discretionary_reasons(row, pd.Timestamp(time.time(), unit="s", tz="UTC"))
                   if getattr(self, "discretionary", False)
                   else execution_reasons(row) + list(provenance.get("issues") or []))
        return not reasons, list(dict.fromkeys(reasons))

    def _live_fast_score(self, row, provenance):
        if row.get("strategy_id") == STRATEGY_ID:
            return float(101-int(row.get("openai_priority") or 101))
        return super()._live_fast_score(row, provenance)

    def enrich(self, raw, *, forward_metrics=None):
        row = super().enrich(raw, forward_metrics=forward_metrics)
        if row.get("strategy_id") == STRATEGY_ID:
            reasons = [r for r in row["rejection_reasons"] if r != "FORWARD_VALIDATION_NOT_YET_PRIME"]
            eligible = bool(row["standard_eligible"] and not reasons)
            row.update({"eligible": eligible, "execution_eligible": eligible,
                        "standard_eligible": eligible, "trade_eligible": eligible,
                        "strong_qualified": eligible, "confidence_qualified": eligible,
                        "prime_qualified": bool(eligible and row.get("prime_qualified")),
                        "ig_demo_learning_eligible": eligible,
                        "learning_eligible": bool(eligible and not row.get("prime_qualified")),
                        "execution_basis": ("AI_DISCRETIONARY_DEMO" if getattr(self, "discretionary", False)
                                            else "OPENAI_ANALYST_WITH_PROTECTED_EXECUTOR") if eligible else "OPENAI_WAIT_OR_EXECUTION_GUARD",
                        "fast_score_source": "OPENAI_PRIORITY_NOT_WIN_PROBABILITY",
                        "rejection_reasons": reasons,
                        "compound_eligible": False})
        return row

    def category_rankings(self, category=None, top_n=5):
        result = super().category_rankings(category, top_n)
        for rows in result.values():
            for row in rows:
                row["compound_eligible"] = False
                row["compound_blocker"] = "OPENAI_REQUIRES_PROTECTED_ACCOUNT_RISK_EXECUTOR"
        return result


class OpenAIExecutionEngine(DiscretionaryDemoMixin, CategoryExecutionEngine):
    VERSION = VERSION
    ACTIVE_SYMBOLS = AI_ACTIVE_KEYS
    ACTIVE_STRATEGY_IDS = (*CategoryExecutionEngine.ACTIVE_STRATEGY_IDS, *REGIME_IDS, STRATEGY_ID)

    def __init__(self, *, now_func=None, discretionary=False, **kwargs):
        self.discretionary = bool(discretionary)
        self._now = now_func or (lambda: pd.Timestamp.now(tz="UTC"))
        super().__init__(**kwargs)

    def _is_active_strategy(self, candidate):
        symbol, category = candidate.get("symbol"), candidate.get("category")
        return candidate.get("strategy_id") == STRATEGY_ID and symbol in AI_ACTIVE_KEYS and (
            (category == "FOREX" and symbol not in {"GOLD", "BITCOIN"})
            or (category == "METALS" and symbol == "GOLD") or (category == "CRYPTO" and symbol == "BITCOIN"))

    def _entry_session_allowed(self, candidate):
        # Explicit AI policy: market hours are determined by IG, not a geographic schedule.
        return candidate.get("strategy_id") == STRATEGY_ID

    def _may_open(self, candidate, external):
        if self.discretionary:
            return self._discretionary_may_open(candidate)
        if not demo_only(self.broker):
            return False, "IG_DEMO_REQUIRED"
        reasons = execution_reasons(candidate, now=self._now())
        if reasons:
            return False, ", ".join(reasons)
        return super()._may_open(candidate, external)

    def _open_candidate(self, candidate, external):
        if self.discretionary:
            return self._discretionary_open(candidate)
        return super()._open_candidate(candidate, external)

    def _due_closes(self):
        if self.discretionary:
            return self._discretionary_closes()
        return super()._due_closes()

    def _reconcile(self):
        rows = super()._reconcile()
        if self.discretionary:
            self._recover_discretionary_orders(rows)
        return rows

    def tick(self):
        if self.discretionary:
            return self._discretionary_tick()
        return super().tick()

    def status(self):
        out = super().status()
        out.update({"version": VERSION, "active_strategy": STRATEGY_ID, "active_strategies": [STRATEGY_ID],
                    "active_symbol": "OPENAI_ACTIVE_MARKETS", "active_categories": ["FOREX", "METALS", "CRYPTO"],
                    "analysis_mode": "OPENAI", "regime_policy": policy_status(self.discretionary), "minimum_target_r": None,
                    "session_policy": "IG broker market hours; no geographic-session entry veto",
                    "risk_policy": "AI_INVALIDATION_TARGET_ACCOUNT_RISK_WITH_ATTACHED_PROTECTION"})
        out["max_daily_entries_south_africa"]["CRYPTO"] = self.max_daily_entries
        out["entries_today_south_africa"]["CRYPTO"] = self._opened_today_sast("CRYPTO")
        for legacy_field in ("minimum_target_r_by_market", "preferred_target_r_by_market", "forex_demo_policy"):
            out.pop(legacy_field, None)
        if self.discretionary:
            out.update(execution_policy="AI_DISCRETIONARY_DEMO", risk_policy="MODEL_DISCRETION_NO_APPLICATION_RISK_CAPS",
                       risk_per_trade_pct=None, max_daily_entries_south_africa=None, max_open_positions=None,
                       global_ig_max_positions=None, global_remaining_positions=None, max_per_category=None,
                       max_tracks_per_epic=None, mandatory_attached_protection=False,
                       quote_spread_dealing_distance_preflight=False, exit_policy="MODEL_CLOSE_DECISIONS",
                       last_order_error=self._state.get("last_order_error"),
                       last_model_close_error=self._state.get("last_model_close_error"))
        return out
