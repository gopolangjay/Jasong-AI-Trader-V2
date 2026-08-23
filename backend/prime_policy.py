from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from types import MethodType
from typing import Any, Callable, Dict, Iterable, List, Optional

from forward_store import ForwardStore
from forward_validation import ForwardValidationConfig, ForwardValidationEngine
from provenance import ProvenanceRegistry
from strategy_learning import StrategyLearningEngine


QUANT_MIN = 0.28
DIRECTIONAL_AI_MIN = 0.40
FAST_MIN = 45.0
TOP_N_PER_CATEGORY = 5
COMPOUND_SLOTS_PER_CATEGORY = 2


class ForwardPrimeArchitecture:
    """Runtime integration layer that makes broker-settled forward evidence PRIME authority."""

    VERSION = "6.3-clean-core-forward-r-v1"

    def __init__(
        self,
        *,
        intelligence: Any,
        broker: Any,
        provenance_registry: ProvenanceRegistry,
        legacy_evidence_source: Optional[Any],
        state_dir: str,
    ) -> None:
        self.intelligence = intelligence
        self.broker = broker
        self.provenance_registry = provenance_registry
        self.legacy_evidence_source = legacy_evidence_source
        self.category_portfolio = None
        self.signal_max_age_seconds = max(30.0, float(os.getenv("FORWARD_SIGNAL_MAX_AGE_SECONDS", "300")))
        self.quote_max_age_seconds = max(15.0, float(os.getenv("FORWARD_QUOTE_MAX_AGE_SECONDS", "180")))
        self._lock = threading.RLock()
        store_path = os.getenv("FORWARD_VALIDATION_DB_PATH", str(Path(state_dir) / "jasong_forward_validation.sqlite3"))
        self.store = ForwardStore(store_path)
        self.validator = ForwardValidationEngine(
            store=self.store,
            evidence_source=self._settled_evidence_rows,
            config=ForwardValidationConfig.from_env(),
        )
        self.learner = StrategyLearningEngine()

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return default

    @staticmethod
    def _key(value: Any) -> str:
        return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())

    @staticmethod
    def _historical_information(row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "mode": "INFORMATIONAL_ONLY",
            "historical_win_rate": row.get("historical_win_rate"),
            "historical_win_rate_pct": row.get("historical_win_rate_pct"),
            "historical_profit_factor": row.get("historical_profit_factor"),
            "historical_trades": row.get("historical_trades"),
            "historical_max_drawdown_pct": row.get("historical_max_drawdown_pct"),
            "holdout_target_verified": row.get("historical_target_verified"),
            "walk_forward_pass": row.get("walk_forward_pass"),
            "walk_forward_min_win_rate_pct": row.get("walk_forward_min_win_rate_pct"),
            "walk_forward_median_win_rate_pct": row.get("walk_forward_median_win_rate_pct"),
            "walk_forward_profitable_folds": row.get("walk_forward_profitable_folds"),
            "original_quality_tier": row.get("quality_tier"),
            "original_deep_status": row.get("deep_status"),
            "historical_smart_fast_score": row.get("smart_fast_score"),
            "original_rejection_reasons": list(row.get("rejection_reasons") or []),
            "execution_veto": False,
        }

    def _legacy_rows(self) -> List[Dict[str, Any]]:
        source = self.legacy_evidence_source
        if source is None:
            return []
        rows: Iterable[Dict[str, Any]] = []
        try:
            getter = getattr(source, "_settled_broker_rows", None)
            if callable(getter):
                rows = getter() or []
            else:
                status = source.status() if hasattr(source, "status") else {}
                rows = status.get("mirrors", []) if isinstance(status, dict) else []
        except Exception:
            return []
        return [dict(row) for row in rows if isinstance(row, dict)]

    @staticmethod
    def _category_result(row: Dict[str, Any]) -> Optional[str]:
        explicit = str(row.get("broker_result") or row.get("result") or "").upper().strip()
        if explicit in {"WIN", "LOSS"}:
            return explicit
        if str(row.get("status") or "").upper() not in {"CLOSED", "CLOSED_RECONCILED"}:
            return None
        close_result = row.get("close_result") if isinstance(row.get("close_result"), dict) else {}
        pnl_value = close_result.get("profitLoss")
        if pnl_value is None:
            pnl_value = close_result.get("profit")
        if pnl_value is None:
            pnl_value = close_result.get("pnl")
        if pnl_value is not None:
            try:
                pnl = float(pnl_value)
                if pnl > 0:
                    return "WIN"
                if pnl < 0:
                    return "LOSS"
            except Exception:
                pass
        try:
            entry = float(row.get("entry_level") or 0.0)
            exit_level = float(close_result.get("level") or row.get("exit_level") or 0.0)
        except Exception:
            return None
        direction = str(row.get("direction") or "").upper()
        if entry <= 0 or exit_level <= 0 or direction not in {"BUY", "SELL"}:
            return None
        move = exit_level - entry if direction == "BUY" else entry - exit_level
        return "WIN" if move > 0 else "LOSS" if move < 0 else None

    def _category_rows(self) -> List[Dict[str, Any]]:
        portfolio = self.category_portfolio
        if portfolio is None:
            return []
        try:
            source = portfolio.positions() if hasattr(portfolio, "positions") else []
        except Exception:
            return []
        output: List[Dict[str, Any]] = []
        for raw in source or []:
            if not isinstance(raw, dict):
                continue
            result = self._category_result(raw)
            if result not in {"WIN", "LOSS"}:
                continue
            row = dict(raw)
            row["trade_id"] = str(row.get("deal_id") or row.get("trade_id") or "")
            row["broker_result"] = result
            close_result = row.get("close_result") if isinstance(row.get("close_result"), dict) else {}
            row["exit_level"] = close_result.get("level") or row.get("exit_level")
            if close_result.get("profitLoss") is not None:
                row["broker_pnl"] = close_result.get("profitLoss")
            elif close_result.get("profit") is not None:
                row["broker_pnl"] = close_result.get("profit")
            elif close_result.get("pnl") is not None:
                row["broker_pnl"] = close_result.get("pnl")

            try:
                entry = float(row.get("entry_level") or 0.0)
                exit_level = float(row.get("exit_level") or 0.0)
                risk_distance = float(row.get("planned_risk_price_distance") or 0.0)
            except Exception:
                entry = exit_level = risk_distance = 0.0
            direction = str(row.get("direction") or "").upper().strip()
            if entry > 0 and exit_level > 0 and risk_distance > 0 and direction in {"BUY", "SELL"}:
                realised_move = exit_level - entry if direction == "BUY" else entry - exit_level
                row["r_multiple"] = realised_move / risk_distance
                row["realized_r"] = row["r_multiple"]
                row["r_source"] = "BROKER_EXIT_OVER_PLANNED_PRICE_RISK"
            output.append(row)
        return output

    def _settled_evidence_rows(self) -> List[Dict[str, Any]]:
        rows = self._legacy_rows() + self._category_rows()
        by_id: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            key = str(row.get("trade_id") or row.get("ig_deal_id") or row.get("deal_id") or "").strip()
            if key:
                by_id[key] = row
        return list(by_id.values())

    def _live_fast_score(self, row: Dict[str, Any], provenance: Dict[str, Any]) -> float:
        quant = max(0.0, min(1.0, self._safe_float(row.get("quant_confidence"), 0.0)))
        ai = max(0.0, min(1.0, self._safe_float(row.get("model_ai_confidence"), 0.0)))
        direction = str(row.get("direction") or row.get("live_direction") or "").upper()
        recent = []
        for value in (row.get("recent_returns") or [])[-20:]:
            try:
                recent.append(float(value))
            except Exception:
                continue
        if recent:
            signed = sum(recent[-5:]) + 0.5 * sum(recent)
            aligned = signed > 0 if direction == "BUY" else signed < 0 if direction == "SELL" else False
            momentum = 1.0 if aligned else 0.25
        else:
            momentum = 0.50
        spread = self._safe_float(row.get("ig_spread_bps") or row.get("spread_bps"), 0.0)
        limit = self._safe_float(row.get("spread_gate_bps") or row.get("spread_limit_bps"), 0.0)
        spread_efficiency = max(0.0, min(1.0, 1.0 - spread / limit)) if limit > 0 and spread >= 0 else 0.0
        freshness = 1.0 if provenance.get("fresh") else 0.0
        score = 100.0 * (
            0.25 * min(1.0, quant / 0.60)
            + 0.30 * min(1.0, ai / 0.70)
            + 0.20 * momentum
            + 0.15 * spread_efficiency
            + 0.10 * freshness
        )
        return round(max(0.0, min(100.0, score)), 2)

    def _strong_gate(self, row: Dict[str, Any], provenance: Dict[str, Any]) -> tuple[bool, List[str]]:
        reasons: List[str] = []
        direction = str(row.get("direction") or row.get("live_direction") or "").upper().strip()
        if direction not in {"BUY", "SELL"}:
            reasons.append("NO_DIRECTION")
        if self._safe_float(row.get("quant_confidence"), 0.0) < QUANT_MIN:
            reasons.append("QUANT_BELOW_28")
        if self._safe_float(row.get("model_ai_confidence"), 0.0) < DIRECTIONAL_AI_MIN:
            reasons.append("MODEL_AI_BELOW_40")
        if self._safe_float(row.get("smart_fast_score"), 0.0) < FAST_MIN:
            reasons.append("FAST_BELOW_45")
        if not bool(row.get("ig_tradeable")):
            reasons.append("IG_NOT_TRADEABLE")
        if row.get("spread_pass") is not True:
            reasons.append("SPREAD_GATE_FAIL")
        for issue in provenance.get("issues") or []:
            if issue not in reasons:
                reasons.append(str(issue))
        return len(reasons) == 0, reasons

    def enrich(self, raw: Dict[str, Any], *, forward_metrics: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        row = dict(raw)
        historical = self._historical_information(row)
        provenance = self.provenance_registry.build(
            row,
            signal_max_age_seconds=self.signal_max_age_seconds,
            quote_max_age_seconds=self.quote_max_age_seconds,
        )
        historical_fast = row.get("smart_fast_score")
        live_fast = self._live_fast_score(row, provenance)
        row["historical_smart_fast_score"] = historical_fast
        row["smart_fast_score"] = live_fast
        row["live_fast_score"] = live_fast
        row["fast_score_source"] = "LIVE_SIGNAL_SPREAD_FRESHNESS"
        strategy_id = str(
            row.get("strategy_id") or row.get("selected_strategy") or row.get("strategy_name") or "UNKNOWN"
        ).upper().strip()
        strong, strong_reasons = self._strong_gate(row, provenance)
        forward = forward_metrics or (
            self.validator.metrics(strategy_id=strategy_id)
            if strategy_id != "UNKNOWN"
            else self.validator.metrics(symbol=row.get("symbol"))
        )
        prime = bool(strong and forward.get("prime_eligible"))

        historical_only = {
            "SELECTION_UNSTABLE", "HOLDOUT_SAMPLE_BELOW_MIN", "HOLDOUT_WR_BELOW_60",
            "PROFIT_FACTOR_BELOW_1_20", "WALK_FORWARD_BELOW_40",
            "WF_HOLDOUT_SAMPLE_BELOW_MIN", "WF_HOLDOUT_WR_BELOW_60",
            "WF_HOLDOUT_PF_BELOW_1_20", "WF_HOLDOUT_DRAWDOWN_FAIL",
            "WF_MIN_FOLD_WR_BELOW_40", "WF_MEDIAN_WR_BELOW_40",
            "WF_PROFITABLE_FOLDS_BELOW_2", "WF_FOLD_DRAWDOWN_FAIL",
        }
        operational_original = [
            str(reason) for reason in (row.get("rejection_reasons") or [])
            if str(reason) not in historical_only
            and not str(reason).startswith("HOLDOUT_")
            and not str(reason).startswith("WF_")
        ]
        rejection_reasons = list(dict.fromkeys(strong_reasons + operational_original))
        if strong and not prime:
            rejection_reasons.append("FORWARD_VALIDATION_NOT_YET_PRIME")

        live_quality = None
        live_deep = None
        if strong:
            live_quality = "A+" if (
                self._safe_float(row.get("model_ai_confidence")) >= 0.60
                and self._safe_float(row.get("quant_confidence")) >= 0.40
                and self._safe_float(row.get("smart_fast_score")) >= 70.0
            ) else "A"
            live_deep = "VERIFIED"

        row.update({
            "historical_validation": historical,
            "historical_validation_mode": "INFORMATIONAL_ONLY",
            "provenance": provenance,
            "analysis_price_source": provenance.get("analysis_price_source"),
            "analysis_price_timestamp": provenance.get("analysis_price_timestamp"),
            "broker_quote_source": provenance.get("broker_quote_source"),
            "broker_quote_timestamp": provenance.get("broker_quote_timestamp"),
            "news_sources": provenance.get("news_sources"),
            "news_timestamp": provenance.get("news_timestamp"),
            "fallback_source": provenance.get("fallback_source"),
            "signal_age_seconds": provenance.get("signal_age_seconds"),
            "quote_age_seconds": provenance.get("quote_age_seconds"),
            "forward_validation": forward,
            "forward_policy_managed": True,
            "selected_strategy": strategy_id,
            "market_regime": row.get("regime") or row.get("market_regime") or "SPECIALIST",
            "strong_qualified": strong,
            "confidence_qualified": strong,
            "standard_eligible": strong,
            "trade_eligible": strong,
            "prime_qualified": prime,
            "execution_eligible": prime,
            "eligible": prime,
            "learning_eligible": bool(strong and not prime),
            "ig_demo_learning_eligible": strong,
            "trade_class": "PRIME" if prime else ("STRONG" if strong else "OBSERVE"),
            "execution_basis": "FORWARD_VALIDATED_PRIME" if prime else ("BROKER_FORWARD_LEARNING_STRONG" if strong else "NOT_QUALIFIED"),
            "quality_tier": live_quality or row.get("quality_tier"),
            "deep_status": live_deep or row.get("deep_status"),
            "quality_basis": "LIVE_STRONG_POLICY" if strong else "OBSERVATION_ONLY",
            "deep_status_basis": "LIVE_BROKER_PREFLIGHT" if strong else "OBSERVATION_ONLY",
            "rejection_reasons": list(dict.fromkeys(rejection_reasons)),
            "prime_reasons": [] if prime else list(dict.fromkeys(rejection_reasons)),
            "portfolio_gates": "ENFORCED_AT_EXECUTION: capacity + exposure + duplicate + EPIC limits",
        })
        return row

    def _fresh_rows(self) -> List[Dict[str, Any]]:
        getter = getattr(self.intelligence, "_fresh_rows", None)
        if callable(getter):
            try:
                return [dict(row) for row in (getter() or []) if isinstance(row, dict)]
            except Exception:
                pass
        try:
            rankings = self.intelligence.category_rankings() or {}
        except Exception:
            return []
        return [dict(row) for bucket in rankings.values() for row in bucket if isinstance(row, dict)]

    def category_rankings(self, category: Optional[str] = None, top_n: int = TOP_N_PER_CATEGORY) -> Dict[str, List[Dict[str, Any]]]:
        rows = self._fresh_rows()
        categories = [str(category).upper().strip()] if category else ["FOREX", "INDICES", "CRYPTO", "METALS", "ENERGY", "SHARES"]
        output: Dict[str, List[Dict[str, Any]]] = {}
        self.validator.sync()
        metric_cache: Dict[str, Dict[str, Any]] = {}

        def forward_for(row: Dict[str, Any]) -> Dict[str, Any]:
            strategy = str(row.get("strategy_id") or row.get("selected_strategy") or row.get("strategy_name") or "UNKNOWN").upper().strip()
            cache_key = strategy if strategy != "UNKNOWN" else "SYMBOL::" + str(row.get("symbol") or row.get("market") or "")
            if cache_key not in metric_cache:
                metric_cache[cache_key] = (
                    self.validator.metrics(strategy_id=strategy, sync=False)
                    if strategy != "UNKNOWN"
                    else self.validator.metrics(symbol=row.get("symbol"), sync=False)
                )
            return metric_cache[cache_key]

        for cat in categories:
            pool = [
                self.enrich(row, forward_metrics=forward_for(row))
                for row in rows if str(row.get("category") or "").upper() == cat
            ]
            pool.sort(
                key=lambda row: (
                    bool(row.get("strong_qualified")),
                    bool(row.get("prime_qualified")),
                    self._safe_float(row.get("smart_fast_score")),
                    self._safe_float(row.get("model_ai_confidence")),
                    self._safe_float(row.get("quant_confidence")),
                ),
                reverse=True,
            )
            ranked = []
            for idx, row in enumerate(pool[:max(1, min(int(top_n), TOP_N_PER_CATEGORY))], start=1):
                row["category_rank"] = idx
                row["rank"] = idx
                row["source_rank"] = idx
                row["compound_slot_candidate"] = idx <= COMPOUND_SLOTS_PER_CATEGORY
                row["compound_eligible"] = bool(idx <= COMPOUND_SLOTS_PER_CATEGORY and row.get("prime_qualified"))
                row["standard_eligible"] = bool(row.get("strong_qualified"))
                row["trade_eligible"] = bool(row.get("strong_qualified"))
                ranked.append(row)
            output[cat] = ranked
        return output

    def compound_candidates(self) -> List[Dict[str, Any]]:
        rows = []
        for category, ranked in self.category_rankings().items():
            for row in ranked:
                if row.get("compound_eligible"):
                    item = dict(row)
                    item["compound_source_category"] = category
                    item["compound_source_rank"] = item.get("category_rank")
                    rows.append(item)
        rows.sort(
            key=lambda row: (
                self._safe_float((row.get("forward_validation") or {}).get("expectancy_r"), -999.0),
                self._safe_float((row.get("forward_validation") or {}).get("profit_factor"), 0.0),
                self._safe_float(row.get("smart_fast_score"), 0.0),
            ),
            reverse=True,
        )
        return rows

    def execution_guard_snapshot(self) -> Dict[str, Any]:
        by_market: Dict[str, Dict[str, Any]] = {}
        prime_count = 0
        strong_count = 0
        rankings = self.category_rankings()
        for bucket in rankings.values():
            for enriched in bucket:
                key = self._key(enriched.get("symbol") or enriched.get("market"))
                if not key:
                    continue
                forward = dict(enriched.get("forward_validation") or {})
                strong_count += int(bool(enriched.get("strong_qualified")))
                prime_count += int(bool(enriched.get("prime_qualified")))
                by_market[key] = {
                    **forward,
                    "quarantined": not bool(enriched.get("prime_qualified")),
                    "hard_quarantined": False,
                    "quarantine_reason": None if enriched.get("prime_qualified") else "FORWARD_VALIDATION_NOT_YET_PRIME",
                }
        return {
            "version": self.VERSION,
            "mode": "FORWARD_VALIDATED" if prime_count else "FORWARD_BOOTSTRAP",
            "authority": "BROKER_SETTLED_FORWARD_ONLY",
            "historical_validation_mode": "INFORMATIONAL_ONLY",
            "recent": {"strong_candidates": strong_count, "prime_candidates": prime_count},
            "by_market": by_market,
            "by_class": {"PRIME": {"hard_quarantined": False}},
            "execution_policy": "STRONG_DEMO_LEARNING_THEN_FORWARD_VALIDATED_PRIME",
        }

    def _patch_compound(self, compound_engine: Any) -> None:
        for attr, value in {
            "quant_min_confidence": QUANT_MIN,
            "ai_min_confidence": DIRECTIONAL_AI_MIN,
            "fast_score_min": FAST_MIN,
            "global_fast_score_min": FAST_MIN,
            "prime_min_historical_wr": 0.0,
            "prime_min_historical_pf": 0.0,
            "prime_min_historical_trades": 0,
            "strategy_confidence_min": 0.0,
            "strategy_ev_min": -999.0,
        }.items():
            if hasattr(compound_engine, attr):
                setattr(compound_engine, attr, value)
        setter = getattr(compound_engine, "set_forward_evidence_source", None)
        if callable(setter):
            setter(self.execution_guard_snapshot)
        elif hasattr(compound_engine, "forward_evidence_source"):
            compound_engine.forward_evidence_source = self.execution_guard_snapshot

        original_assessment = getattr(compound_engine, "_adaptive_strategy_assessment", None)
        if callable(original_assessment) and not getattr(compound_engine, "_forward_prime_strategy_patch", False):
            def assessment(engine_self: Any, row: Dict[str, Any]) -> Dict[str, Any]:
                if row.get("forward_policy_managed"):
                    strategy = str(row.get("strategy_id") or row.get("selected_strategy") or "CATEGORY_SPECIALIST")
                    direction = str(row.get("direction") or "").upper()
                    forward = row.get("forward_validation") if isinstance(row.get("forward_validation"), dict) else {}
                    confidence = max(
                        self._safe_float(row.get("model_ai_confidence"), 0.0),
                        self._safe_float(row.get("quant_confidence"), 0.0),
                    )
                    return {
                        "market_regime": row.get("regime") or "SPECIALIST",
                        "selected_strategy": strategy,
                        "strategy_direction": direction,
                        "strategy_confidence": confidence,
                        "strategy_confidence_pct": round(confidence * 100.0, 2),
                        "strategy_expected_value": self._safe_float(forward.get("expectancy_r"), 0.0),
                        "strategy_direction_match": direction in {"BUY", "SELL"},
                        "strategy_reason": "Specialist strategy selected before broker-forward validation",
                        "strategy_metrics": {"source": "CATEGORY_SPECIALIST", "forward": forward},
                    }
                return original_assessment(row)
            compound_engine._adaptive_strategy_assessment = MethodType(assessment, compound_engine)
            compound_engine._forward_prime_strategy_patch = True
        compound_engine.candidate_source = lambda _cycle_capital: self.compound_candidates()

    def _patch_legacy_learning_lane(self, evidence_source: Any) -> None:
        if evidence_source is None:
            return
        for attr, value in {
            "prime_min_historical_wr": 0.0,
            "prime_min_historical_pf": 0.0,
            "prime_min_historical_trades": 0,
            "prime_fx_fast_min": FAST_MIN,
        }.items():
            if hasattr(evidence_source, attr):
                setattr(evidence_source, attr, value)

    def attach_category_portfolio(self, portfolio: Any) -> None:
        self.category_portfolio = portfolio
        original_open = getattr(portfolio, "_open_candidate", None)
        if not callable(original_open) or getattr(portfolio, "_forward_prime_open_patch", False):
            return

        def open_candidate(self_portfolio: Any, candidate: Dict[str, Any], external: List[Dict[str, Any]]) -> Any:
            before = {str(row.get("deal_id")) for row in self_portfolio._state.get("positions", [])}
            result = original_open(candidate, external)
            for position in self_portfolio._state.get("positions", []):
                deal_id = str(position.get("deal_id") or "")
                if not deal_id or deal_id in before:
                    continue
                position["trade_class"] = candidate.get("trade_class") or "STRONG"
                position["provenance"] = dict(candidate.get("provenance") or {})
                position["entry_snapshot"] = {
                    "strategy_id": candidate.get("strategy_id"),
                    "strategy_name": candidate.get("strategy_name"),
                    "direction": candidate.get("direction"),
                    "quant_confidence": candidate.get("quant_confidence"),
                    "model_ai_confidence": candidate.get("model_ai_confidence"),
                    "smart_fast_score": candidate.get("smart_fast_score"),
                    "rsi": candidate.get("rsi"),
                    "adx": candidate.get("adx"),
                    "spread_bps": candidate.get("ig_spread_bps") or candidate.get("spread_bps"),
                    "spread_limit_bps": candidate.get("spread_gate_bps") or candidate.get("spread_limit_bps"),
                    "captured_at": time.time(),
                }
                position["historical_validation"] = dict(candidate.get("historical_validation") or {})
            try:
                self_portfolio._persist()
            except Exception:
                pass
            return result

        portfolio._open_candidate = MethodType(open_candidate, portfolio)
        portfolio._forward_prime_open_patch = True

    def install(self, *, compound_engine: Any) -> None:
        self._patch_compound(compound_engine)
        self._patch_legacy_learning_lane(self.legacy_evidence_source)

    def status(self) -> Dict[str, Any]:
        rankings = self.category_rankings()
        strategies = sorted({
            str(row.get("strategy_id") or "UNKNOWN")
            for bucket in rankings.values()
            for row in bucket if row.get("strategy_id")
        })
        metrics = {strategy: self.validator.metrics(strategy_id=strategy) for strategy in strategies}
        rows = self.validator.all_rows(limit=200)
        return {
            "version": self.VERSION,
            "authority": "BROKER_SETTLED_FORWARD_ONLY",
            "historical_validation": {"mode": "INFORMATIONAL_ONLY", "execution_veto": False},
            "bootstrap_lane": "STRONG -> controlled IG DEMO category trades -> settled forward evidence -> PRIME",
            "thresholds": metrics[next(iter(metrics))]["thresholds"] if metrics else self.validator.metrics(symbol="__NONE__")["thresholds"],
            "strategy_metrics": metrics,
            "strategy_learning": self.learner.analyze(rows),
            "stored_settled_trades": len(rows),
            "store_path": self.store.path,
            "live_money_execution": False,
        }

    def learning_report(self) -> Dict[str, Any]:
        return self.learner.analyze(self.validator.all_rows(limit=500))

    def routes(self, app: Any) -> None:
        app.add_api_route("/forward-validation/status", self.status, methods=["GET"], name="forward_validation_status_clean_core_r")
        app.add_api_route("/forward-validation/learning", self.learning_report, methods=["GET"], name="forward_validation_learning_clean_core_r")
        app.add_api_route(
            "/forward-validation/trades",
            lambda: {
                "version": self.VERSION,
                "authority": "BROKER_SETTLED_FORWARD_ONLY",
                "trades": self.validator.all_rows(limit=200),
                "live_money_execution": False,
            },
            methods=["GET"],
            name="forward_validation_trades_clean_core_r",
        )


def make_provenance_frame_func(
    frame_func: Callable[[Dict[str, Any]], Any],
    registry: ProvenanceRegistry,
    *,
    analysis_source: str,
) -> Callable[[Dict[str, Any]], Any]:
    def wrapped(seed: Dict[str, Any]):
        frame = frame_func(seed)
        return registry.record_frame(seed, frame, source=analysis_source)
    return wrapped
