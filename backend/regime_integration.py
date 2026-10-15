"""V6.14 strategy and execution classes, leaving legacy engines available."""
from __future__ import annotations

import time
from typing import Any

import pandas as pd

from category_strategy_engine import CategoryStrategyEngine, CATEGORY_RULES, ACTIVE_EXECUTION_KEYS, EVIDENCE_SCHEMA_VERSION
from category_execution_engine import CategoryExecutionEngine
from forex_liquidity_lines_strategy import LIQUID_FOREX_PAIRS, forex_session_context, news_blackout_context
from prime_policy import ForwardPrimeArchitecture
from regime_strategy import VERSION, STRATEGY_IDS, TREND_ID, REVERSAL_ID, analyze_regime, execution_reasons
from xauusd_liquidity_strategy import session_context


def policy_status() -> dict[str, Any]:
    return {"version": VERSION, "strategy_ids": list(STRATEGY_IDS),
            "router": "H1_ADX_TREND_RANGE_TRANSITION", "trend": "H1 EMA20/50 cross -> pullback -> directional close; H4 conflict veto when available",
            "reversal": "M15 or H1 confirmed price-pivot RSI14 divergence at Bollinger/range edge -> strong close",
            "trend_adx_above": 25, "range_adx_below": 20, "transition": "WAIT",
            "trend_target_r": 2.0, "reversal_minimum_target_r": 1.2,
            "risk": "ATR/structure stop, spread buffer, account risk sizing, broker-attached stop/limit",
            "confidence_basis": "RULE_COMPLETION_IS_NOT_WIN_PROBABILITY",
            "active_symbols": [*LIQUID_FOREX_PAIRS, "GOLD"], "weekend_crypto_policy": "V6.13_UNCHANGED",
            "compound_policy": "NEW_REGIME_ENTRIES_USE_RISK_SIZED_CATEGORY_PORTFOLIO",
            "environment": "IG_DEMO_ONLY", "live_money_execution": False}


def current_session(symbol: str, now: Any) -> dict[str, Any]:
    return session_context(now) if symbol == "GOLD" else forex_session_context(now, symbol)


class RegimeStrategyEngine(CategoryStrategyEngine):
    VERSION = VERSION

    def __init__(self, *, now_func=None, **kwargs):
        self._now = now_func or (lambda: pd.Timestamp.now(tz="UTC"))
        super().__init__(**kwargs)

    def _evaluate_seed(self, seed: dict[str, Any]) -> dict[str, Any]:
        key, category = str(seed.get("key") or "").upper(), str(seed.get("category") or "").upper()
        if key not in ACTIVE_EXECUTION_KEYS:
            return super()._evaluate_seed(seed)
        now = self._now()
        raw = self.frame_func(seed)
        analysis = analyze_regime(raw, key, now=now)
        session = current_session(key, now)
        news = news_blackout_context(now, "XAUUSD" if key == "GOLD" else key)
        direction = analysis["direction"]
        rule_score = 1.0 if analysis["eligible"] else 0.0
        # Legacy ML remains diagnostic; the supplied strategies are rule based.
        closed = raw[raw.index+pd.Timedelta(minutes=15) <= now]
        probability = float(closed.UP_PROB.iloc[-1]) if "UP_PROB" in closed and len(closed) else 0.5
        ai = probability if direction == "BUY" else 1-probability if direction == "SELL" else 0.0
        row = {**seed, **analysis, "symbol": key, "market": seed.get("name"), "version": self.VERSION,
               "evidence_schema_version": EVIDENCE_SCHEMA_VERSION, "current_candle_strategy_complete": True,
               "optimizer_complete": True, "walk_forward_complete": True,
               "strategy_definition_version": self.VERSION, "strategy_module_version": self.VERSION,
               "strategy_selection_mode": "REGIME_ADAPTIVE_TREND_OR_DIVERGENCE", "regime_strategy": analysis,
               "analysis_source": "CLOSED_M15_H1_H4_REGIME_RULES", "live_direction": direction,
               "signal_qualified": bool(analysis["eligible"]), "rule_completion_score": rule_score,
               "quant_confidence": rule_score, "quant_confidence_pct": rule_score*100,
               "model_ai_confidence": ai, "model_ai_confidence_source": "LEGACY_ML_DIAGNOSTIC_ONLY",
               "smart_fast_score": rule_score*100, "confidence_basis": "RULE_COMPLETION_NOT_WIN_PROBABILITY",
               "session": session, "session_active": session["active"], "session_name": session["name"],
               "session_exit_at": session.get("session_exit_at"), "south_africa_time": session.get("south_africa_local"),
               "news_guard": news, "evaluated_at": time.time(), "ig_tradeable": False, "ig_epic": None,
               "spread_pass": False, "liquidity_pass": False, "ig_quote_source": "NOT_REQUESTED",
               "volatility_pass": analysis.get("panic_volatility_pass", False),
               "spread_gate_bps": CATEGORY_RULES[category]["spread_gate_bps"],
               "compound_eligible": False, "historical_validation_mode": "INFORMATIONAL_ONLY", "historical_execution_veto": False,
               "recent_returns": raw.Close.pct_change().dropna().tail(160).tolist()}
        if analysis["eligible"]:
            try:
                market = self._resolve_execution_market(seed)
                bid, offer, source = self._resolve_bid_offer(seed, market)
                row.update({"ig_epic": market.get("epic"), "ig_market_name": market.get("name"),
                            "ig_market_status": market.get("market_status"), "ig_instrument_type": market.get("instrument_type"),
                            "ig_tradeable": str(market.get("market_status") or "").upper() == "TRADEABLE",
                            "ig_bid": bid, "ig_offer": offer, "ig_quote_source": source, "ig_quote_timestamp": time.time(),
                            "ig_min_deal_size": market.get("min_deal_size"), "ig_expiry": market.get("expiry")})
                if bid is not None and offer is not None and 0 < bid < offer:
                    mid = (bid+offer)/2
                    bps = (offer-bid)/mid*10000
                    relative_atr = analysis["atr"]/analysis["live_price"]
                    row.update({"ig_spread_bps": bps, "spread_bps": bps, "spread_atr_ratio": bps/10000/relative_atr,
                                "spread_pass": bps <= row["spread_gate_bps"],
                                "liquidity_pass": bool(analysis["volume_pass"] and bps/10000/relative_atr <= analysis["max_spread_atr"])})
            except Exception as exc:
                row["ig_preflight_error"] = f"{type(exc).__name__}: {exc}"
        reasons = execution_reasons(row)
        row.update({"rejection_reasons": reasons, "standard_eligible": not reasons, "trade_eligible": not reasons,
                    "confidence_qualified": analysis["eligible"], "direction_match": direction in {"BUY", "SELL"},
                    "execution_safety_pass": not reasons, "category_rank_score": rule_score*80+(20 if not reasons else 0),
                    "quality_tier": "A" if not reasons else None, "deep_status": "VERIFIED" if not reasons else "OBSERVE"})
        return row

    def universe(self, category=None):
        rows = super().universe(category)
        for row in rows:
            if row["execution_active"]:
                row["execution_policy"] = "REGIME_ADAPTIVE_TREND_OR_DIVERGENCE"
        return rows

    def status(self):
        out = super().status()
        out.update({"version": self.VERSION, "name": "JASONG V6.14 REGIME ADAPTIVE", "regime_policy": policy_status(),
                    "strategy_sequence": {"setup": policy_status()["trend"]+" OR "+policy_status()["reversal"],
                                          "then": "Session/news/freshness -> IG liquidity -> account-risk sizing -> attached stop/limit"},
                    "confidence_policy": {"basis": "RULE_COMPLETION_NOT_WIN_PROBABILITY", "ml": "DIAGNOSTIC_ONLY"}})
        for category in ("FOREX", "METALS"):
            info = out.get("categories", {}).get(category, {})
            info.update({"strategy": "Regime Adaptive: EMA Pullback / RSI Divergence",
                         "strategy_id": "REGIME_ADAPTIVE_ROUTER_V1", "selection_mode": "REGIME_ADAPTIVE_TREND_OR_DIVERGENCE"})
        return out

    def optimizer_summary(self):
        out = super().optimizer_summary()
        out.update({"version": self.VERSION, "method": "CLOSED_CANDLE_REGIME_ROUTER", "regime_policy": policy_status(),
                    "setup_rule": "H1 regime selects H1 trend pullback or M15/H1 divergence; transition waits",
                    "risk_reward_rule": "Trend 2R; reversal >=1.2R to existing range mean", "quant_min_pct": None,
                    "model_ai_min_pct": None, "overall_setup_confidence_min_pct": None})
        return out


class RegimePrimeArchitecture(ForwardPrimeArchitecture):
    VERSION = VERSION

    def _strong_gate(self, row, provenance):
        if row.get("strategy_id") not in STRATEGY_IDS:
            return super()._strong_gate(row, provenance)
        reasons = execution_reasons(row) + list(provenance.get("issues") or [])
        return not reasons, list(dict.fromkeys(reasons))

    def category_rankings(self, category=None, top_n=5):
        result = super().category_rankings(category, top_n)
        # The older Compound lane uses allocation sizing without attached
        # stops. Do not let it bypass this generation's account-risk executor.
        for rows in result.values():
            for row in rows:
                if row.get("strategy_id") in STRATEGY_IDS:
                    row["compound_eligible"] = False
                    row["compound_blocker"] = "REGIME_REQUIRES_ACCOUNT_RISK_SIZED_ATTACHED_STOP_EXECUTOR"
        return result


class RegimeExecutionEngine(CategoryExecutionEngine):
    VERSION = VERSION
    # Count prior-generation entries toward today's limits, and continue
    # managing their open positions after a restart/upgrade.
    ACTIVE_STRATEGY_IDS = (*CategoryExecutionEngine.ACTIVE_STRATEGY_IDS, *STRATEGY_IDS)

    def __init__(self, *, now_func=None, **kwargs):
        self._now = now_func or (lambda: pd.Timestamp.now(tz="UTC"))
        super().__init__(**kwargs)

    def _is_active_strategy(self, candidate):
        symbol, category = candidate.get("symbol"), candidate.get("category")
        return candidate.get("strategy_id") in STRATEGY_IDS and (
            (symbol in LIQUID_FOREX_PAIRS and category == "FOREX") or (symbol == "GOLD" and category == "METALS"))

    def _may_open(self, candidate, external):
        if candidate.get("strategy_id") in STRATEGY_IDS:
            reasons = execution_reasons(candidate)
            now = self._now()
            try:
                signal = pd.Timestamp(candidate.get("signal_timestamp"))
                fresh = signal.tzinfo is not None and 0 <= (now-signal).total_seconds() <= 300
            except (ValueError, TypeError):
                fresh = False
            if not fresh:
                reasons.append("CONFIRMATION_SIGNAL_STALE")
            symbol = str(candidate.get("symbol") or "")
            if not current_session(symbol, now)["active"]:
                reasons.append("SESSION_INACTIVE")
            if not news_blackout_context(now, "XAUUSD" if symbol == "GOLD" else symbol)["clear"]:
                reasons.append("HIGH_IMPACT_NEWS_EXECUTION_GUARD")
            if reasons:
                return False, ", ".join(dict.fromkeys(reasons))
        return super()._may_open(candidate, external)

    def status(self):
        out = super().status()
        out.pop("forex_demo_policy", None)
        out.update({"version": self.VERSION, "active_strategy": " + ".join(STRATEGY_IDS), "active_strategies": list(STRATEGY_IDS),
                    "regime_policy": policy_status(), "minimum_target_r": 1.2,
                    "minimum_target_r_by_market": {"FOREX": 1.2, "METALS_GOLD": 1.2},
                    "preferred_target_r_by_market": {"FOREX": 2.0, "METALS_GOLD": 2.0},
                    "risk_policy": "ATR_STRUCTURE_AND_SPREAD_ACCOUNT_RISK_SIZING_WITH_ATTACHED_STOP_LIMIT"})
        return out
