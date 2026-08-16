from __future__ import annotations

import math
import time
from typing import Any, Dict, Iterable, List, Optional


class EvidenceExecutionIntegrityEngine:
    """Read-only V6.7.2a evidence and execution integrity scorer.

    The score is deliberately separated into four components so a broker/API
    defect is never mislabeled as a weak trading strategy:

      1. operational_readiness
      2. evidence_quality
      3. strategy_performance
      4. compound_readiness

    This module never opens, closes or sizes a trade.
    """

    VERSION = "6.7.2a"

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
    def _clamp(value: float, minimum: float = 0.0, maximum: float = 100.0) -> float:
        return max(minimum, min(maximum, value))

    @classmethod
    def _score(cls, value: float) -> float:
        return round(cls._clamp(value), 1)

    @staticmethod
    def _rows(value: Any) -> List[Dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [dict(row) for row in value if isinstance(row, dict)]

    @staticmethod
    def _status(value: Any) -> str:
        return str(value or "").upper().strip()

    @staticmethod
    def _path_persistent(path: Any) -> bool:
        clean = str(path or "")
        return clean.startswith("/var/data/")

    @classmethod
    def _model_metrics(
        cls,
        model_evidence: Dict[str, Any],
    ) -> Dict[str, Any]:
        rows = cls._rows(model_evidence.get("rows"))
        actual = [
            row
            for row in rows
            if cls._status(row.get("status")) in {"OPEN", "CLOSED"}
        ]
        settled = [
            row
            for row in actual
            if cls._status(row.get("status")) == "CLOSED"
            and cls._status(row.get("result")) in {"WIN", "LOSS"}
        ]
        open_rows = [
            row
            for row in actual
            if cls._status(row.get("status")) == "OPEN"
        ]

        wins = sum(
            1
            for row in settled
            if cls._status(row.get("result")) == "WIN"
        )
        losses = len(settled) - wins
        gross_profit = sum(
            max(cls._safe_float(row.get("pnl"), 0.0), 0.0)
            for row in settled
        )
        gross_loss = abs(
            sum(
                min(cls._safe_float(row.get("pnl"), 0.0), 0.0)
                for row in settled
            )
        )
        profit_factor = (
            gross_profit / gross_loss
            if gross_loss > 0
            else (
                99.0
                if gross_profit > 0
                else 0.0
            )
        )
        total_pnl = sum(
            cls._safe_float(row.get("pnl"), 0.0)
            for row in settled
        )
        win_rate = (
            wins / len(settled)
            if settled
            else 0.0
        )

        metadata_fields = (
            "model_ai_confidence",
            "quant_confidence",
            "entry_class",
            "smart_fast_score",
            "quality_tier",
            "deep_status",
        )
        metadata_total = len(actual) * len(metadata_fields)
        metadata_present = sum(
            1
            for row in actual
            for field in metadata_fields
            if row.get(field) not in (None, "")
        )
        metadata_completeness = (
            metadata_present / metadata_total
            if metadata_total > 0
            else 0.0
        )

        broker_matched = sum(
            1
            for row in actual
            if bool(row.get("broker_linked"))
            or bool(row.get("ig_deal_id"))
        )

        historical_rows = []
        for row in settled:
            hist_wr = row.get("historical_win_rate")
            hist_pf = row.get("historical_profit_factor")
            hist_trades = row.get("historical_trades")
            if hist_wr is None and hist_pf is None:
                continue
            wr = cls._safe_float(hist_wr, 0.0)
            if wr > 1.0:
                wr /= 100.0
            historical_rows.append(
                {
                    "trade_id": row.get("trade_id"),
                    "symbol": row.get("symbol"),
                    "direction": row.get("direction"),
                    "historical_win_rate": wr if hist_wr is not None else None,
                    "historical_profit_factor": (
                        cls._safe_float(hist_pf, 0.0)
                        if hist_pf is not None
                        else None
                    ),
                    "historical_trades": hist_trades,
                    "forward_result": row.get("result"),
                    "forward_pnl": row.get("pnl"),
                }
            )

        if historical_rows:
            hist_wrs = [
                row["historical_win_rate"]
                for row in historical_rows
                if row["historical_win_rate"] is not None
            ]
            hist_pfs = [
                row["historical_profit_factor"]
                for row in historical_rows
                if row["historical_profit_factor"] is not None
            ]
            historical_forward = {
                "available": True,
                "matched_settled_trades": len(historical_rows),
                "historical_avg_win_rate_pct": (
                    round(sum(hist_wrs) / len(hist_wrs) * 100.0, 2)
                    if hist_wrs
                    else None
                ),
                "historical_avg_profit_factor": (
                    round(sum(hist_pfs) / len(hist_pfs), 4)
                    if hist_pfs
                    else None
                ),
                "forward_win_rate_pct": round(win_rate * 100.0, 2),
                "forward_profit_factor": round(profit_factor, 4),
                "rows": historical_rows[:100],
            }
        else:
            historical_forward = {
                "available": False,
                "matched_settled_trades": 0,
                "historical_avg_win_rate_pct": None,
                "historical_avg_profit_factor": None,
                "forward_win_rate_pct": round(win_rate * 100.0, 2),
                "forward_profit_factor": round(profit_factor, 4),
                "rows": [],
                "reason": (
                    "No settled V64 trade currently contains normalized "
                    "historical win-rate/profit-factor metadata. V6.7.2 "
                    "persists those fields on new entries when supplied by "
                    "the watcher/deep-validation evidence."
                ),
            }

        mfe_values = [
            cls._safe_float(row.get("mfe_bps"), 0.0)
            for row in settled
            if row.get("mfe_bps") is not None
        ]
        mae_values = [
            cls._safe_float(row.get("mae_bps"), 0.0)
            for row in settled
            if row.get("mae_bps") is not None
        ]

        return {
            "entries": len(actual),
            "open_entries": len(open_rows),
            "settled_entries": len(settled),
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(win_rate * 100.0, 2),
            "profit_factor": round(profit_factor, 4),
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "total_pnl": round(total_pnl, 2),
            "broker_matched_entries": broker_matched,
            "metadata_completeness_pct": round(metadata_completeness * 100.0, 1),
            "avg_mfe_bps": (
                round(sum(mfe_values) / len(mfe_values), 4)
                if mfe_values
                else None
            ),
            "avg_mae_bps": (
                round(sum(mae_values) / len(mae_values), 4)
                if mae_values
                else None
            ),
            "historical_vs_forward": historical_forward,
        }

    @classmethod
    def _broker_integrity(
        cls,
        ig_demo: Dict[str, Any],
        now: float,
    ) -> Dict[str, Any]:
        mirrors = cls._rows(ig_demo.get("mirrors"))
        broker = (
            dict(ig_demo.get("broker") or {})
            if isinstance(ig_demo.get("broker"), dict)
            else {}
        )

        overdue_open = []
        close_errors = []
        close_pending = []
        close_deferred = []

        for mirror in mirrors:
            status = cls._status(mirror.get("broker_status"))
            scheduled = cls._safe_float(
                mirror.get("scheduled_close_at"),
                0.0,
            )
            if (
                status in {"OPEN", "CLOSE_PENDING"}
                and scheduled > 0
                and now > scheduled
            ):
                overdue_open.append(mirror)

            close_execution_state = cls._status(
                mirror.get("close_execution_state")
            )
            is_deferred = (
                close_execution_state
                == "CLOSE_DEFERRED_MARKET_CLOSED"
            )
            if is_deferred:
                close_deferred.append(
                    {
                        "deal_id": mirror.get("ig_deal_id"),
                        "symbol": mirror.get("symbol"),
                        "market_status": mirror.get("market_status"),
                        "scheduled_close_at": mirror.get(
                            "scheduled_close_at"
                        ),
                        "deferred_at": mirror.get("close_deferred_at"),
                        "reason": mirror.get("close_deferred_reason"),
                    }
                )

            error = str(
                mirror.get("last_close_error")
                or mirror.get("last_error")
                or ""
            )
            if (
                "close" in error.lower()
                and not is_deferred
            ):
                close_errors.append(
                    {
                        "deal_id": mirror.get("ig_deal_id"),
                        "symbol": mirror.get("symbol"),
                        "error": error,
                    }
                )

            if status == "CLOSE_PENDING":
                close_pending.append(
                    {
                        "deal_id": mirror.get("ig_deal_id"),
                        "symbol": mirror.get("symbol"),
                        "requested_at": mirror.get("close_requested_at"),
                        "remaining_size": mirror.get("remaining_size"),
                    }
                )

        return {
            "configured": bool(
                ig_demo.get("configured")
                or broker.get("configured")
            ),
            "connected": bool(
                broker.get("connected")
            ),
            "sync_state": cls._status(
                ig_demo.get("sync_state")
            ),
            "broker_sync_age_seconds": ig_demo.get(
                "broker_sync_age_seconds"
            ),
            "broker_reconciliation_error": ig_demo.get(
                "broker_reconciliation_error"
            ),
            "account_sync_error": ig_demo.get(
                "account_sync_error"
            ),
            "last_error": ig_demo.get("last_error"),
            "open_positions": int(
                ig_demo.get("open_broker_positions")
                or 0
            ),
            "close_pending_count": len(close_pending),
            "close_pending": close_pending[:20],
            "close_deferred_count": len(close_deferred),
            "close_deferred": close_deferred[:20],
            "actionable_overdue_open_count": max(
                0,
                len(overdue_open) - len(close_deferred),
            ),
            "overdue_open_count": len(overdue_open),
            "overdue_open": [
                {
                    "deal_id": row.get("ig_deal_id"),
                    "symbol": row.get("symbol"),
                    "scheduled_close_at": row.get("scheduled_close_at"),
                    "broker_status": row.get("broker_status"),
                    "last_error": row.get("last_error"),
                }
                for row in overdue_open[:20]
            ],
            "close_error_count": len(close_errors),
            "close_errors": close_errors[:20],
            "state_path": ig_demo.get("state_path"),
        }

    @classmethod
    def evaluate(
        cls,
        *,
        model_evidence: Optional[Dict[str, Any]] = None,
        ig_demo: Optional[Dict[str, Any]] = None,
        compound: Optional[Dict[str, Any]] = None,
        market_data: Optional[Dict[str, Any]] = None,
        persistence: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        model_evidence = dict(model_evidence or {})
        ig_demo = dict(ig_demo or {})
        compound = dict(compound or {})
        market_data = dict(market_data or {})
        persistence = dict(persistence or {})
        now = time.time()

        model = cls._model_metrics(
            model_evidence
        )
        broker = cls._broker_integrity(
            ig_demo,
            now,
        )

        compound_perf = (
            dict(compound.get("performance") or {})
            if isinstance(compound.get("performance"), dict)
            else {}
        )
        completed_cycles = int(
            compound_perf.get("completed_cycles")
            or 0
        )
        compound_close = (
            dict(compound.get("close_integrity") or {})
            if isinstance(compound.get("close_integrity"), dict)
            else {}
        )

        jasong_path = persistence.get("jasong_state_path")
        ig_path = (
            persistence.get("ig_state_path")
            or ig_demo.get("state_path")
        )
        compound_path = (
            persistence.get("compound_state_path")
            or compound.get("state_path")
        )
        persistent_count = sum(
            1
            for path in (
                jasong_path,
                ig_path,
                compound_path,
            )
            if cls._path_persistent(path)
        )

        healthy_providers = market_data.get(
            "healthy_providers"
        )
        market_data_ok = (
            bool(healthy_providers)
            if isinstance(healthy_providers, list)
            else not bool(
                market_data.get("error")
            )
        )

        # --------------------------------------------------------------
        # 1) Operational readiness
        # --------------------------------------------------------------
        operational = 0.0
        operational += 10.0 if broker["configured"] else 0.0
        operational += 15.0 if broker["connected"] else 0.0
        operational += 15.0 if broker["sync_state"] == "SYNCED" else (
            7.5 if broker["sync_state"] == "STALE" else 0.0
        )
        operational += 10.0 if not broker["broker_reconciliation_error"] else 0.0
        operational += 5.0 if not broker["account_sync_error"] else 0.0
        operational += 15.0 if broker["close_error_count"] == 0 else max(
            0.0,
            15.0 - 5.0 * broker["close_error_count"],
        )
        operational += (
            10.0
            if broker["actionable_overdue_open_count"] == 0
            else max(
                0.0,
                10.0
                - 2.0
                * broker["actionable_overdue_open_count"],
            )
        )
        operational += 15.0 * (persistent_count / 3.0)
        operational += 5.0 if market_data_ok else 0.0
        operational_score = cls._score(
            operational
        )

        # --------------------------------------------------------------
        # 2) Evidence quality
        # --------------------------------------------------------------
        settled = int(model["settled_entries"])
        entries = int(model["entries"])
        broker_matched = int(
            model["broker_matched_entries"]
        )

        sample_ratio = min(
            settled / 30.0,
            1.0,
        )
        broker_match_ratio = (
            broker_matched / entries
            if entries > 0
            else 0.0
        )
        metadata_ratio = (
            cls._safe_float(
                model["metadata_completeness_pct"],
                0.0,
            )
            / 100.0
        )
        cycles_ratio = min(
            completed_cycles / 5.0,
            1.0,
        )
        unattributed = int(
            model_evidence.get(
                "broker_recovered_unattributed"
            )
            or 0
        )
        recovered_score = max(
            0.0,
            10.0 - min(unattributed, 5) * 2.0,
        )

        evidence_score = cls._score(
            35.0 * sample_ratio
            + 20.0 * broker_match_ratio
            + 20.0 * metadata_ratio
            + 15.0 * cycles_ratio
            + recovered_score
        )

        # --------------------------------------------------------------
        # 3) Strategy performance, reliability-shrunk toward neutral 50.
        # --------------------------------------------------------------
        pf = cls._safe_float(
            model["profit_factor"],
            0.0,
        )
        wr = cls._safe_float(
            model["win_rate_pct"],
            0.0,
        )
        total_pnl = cls._safe_float(
            model["total_pnl"],
            0.0,
        )
        # With payout 0.80, break-even is ~55.6%. This component is used as
        # an operational score only; it is not a prediction of future returns.
        wr_component = cls._clamp(
            ((wr - 35.0) / 40.0) * 100.0
        )
        pf_component = cls._clamp(
            (pf / 2.0) * 100.0
        )
        pnl_component = (
            65.0
            if total_pnl > 0
            else (
                35.0
                if total_pnl < 0
                else 50.0
            )
        )
        raw_strategy = (
            0.40 * pf_component
            + 0.35 * wr_component
            + 0.25 * pnl_component
        )
        reliability = min(
            settled / 30.0,
            1.0,
        )
        strategy_score = cls._score(
            50.0 * (1.0 - reliability)
            + raw_strategy * reliability
        )

        if settled < 10:
            strategy_confidence = "LOW"
        elif settled < 30:
            strategy_confidence = "PRELIMINARY"
        elif settled < 100:
            strategy_confidence = "MEDIUM"
        else:
            strategy_confidence = "HIGH"

        # --------------------------------------------------------------
        # 4) Compound readiness
        # --------------------------------------------------------------
        compound_status = cls._status(
            compound.get("status")
        )
        compound_error = compound.get(
            "last_error"
        )
        compound_active_ok = compound_status not in {
            "ERROR",
            "BROKER_NOT_CONFIGURED",
        }
        close_errors = int(
            compound_close.get("errors")
            or 0
        )

        compound_score = 0.0
        compound_score += 20.0 if compound_active_ok else 0.0
        compound_score += 15.0 if not compound_error else 0.0
        compound_score += 15.0 if close_errors == 0 else max(
            0.0,
            15.0 - 5.0 * close_errors,
        )
        compound_score += 30.0 * min(
            completed_cycles / 10.0,
            1.0,
        )
        compound_score += 10.0 if compound.get("rules") else 0.0
        compound_score += 10.0 if cls._path_persistent(compound_path) else 0.0
        compound_score = cls._score(
            compound_score
        )

        overall = cls._score(
            0.30 * operational_score
            + 0.30 * evidence_score
            + 0.25 * strategy_score
            + 0.15 * compound_score
        )

        blockers: List[str] = []
        if broker["close_error_count"]:
            blockers.append(
                f"{broker['close_error_count']} IG close error(s) remain."
            )
        if broker["close_deferred_count"]:
            blockers.append(
                (
                    f"{broker['close_deferred_count']} overdue broker "
                    "position(s) are safely deferred because their IG "
                    "markets are not currently closable."
                )
            )
        if broker["actionable_overdue_open_count"]:
            blockers.append(
                (
                    f"{broker['actionable_overdue_open_count']} broker "
                    "position(s) are overdue and actionable for closure."
                )
            )
        if broker["sync_state"] != "SYNCED":
            blockers.append(
                f"IG broker reconciliation is {broker['sync_state'] or 'UNKNOWN'}."
            )
        if settled < 30:
            blockers.append(
                f"Only {settled}/30 clean settled model trades are available for the first evidence checkpoint."
            )
        if completed_cycles < 5:
            blockers.append(
                f"Only {completed_cycles}/5 completed Compound cycles are available for the first cycle checkpoint."
            )
        if model["metadata_completeness_pct"] < 90:
            blockers.append(
                f"Model trade metadata completeness is {model['metadata_completeness_pct']:.1f}%."
            )

        recommendations: List[str] = []
        if (
            broker["close_error_count"]
            or broker["actionable_overdue_open_count"]
        ):
            recommendations.append(
                "Resolve broker close/reconciliation integrity before interpreting strategy results."
            )
        elif broker["close_deferred_count"]:
            recommendations.append(
                (
                    "No close-integrity repair is required for the deferred "
                    "positions; Jasong will submit their close automatically "
                    "when IG reports TRADEABLE or CLOSINGS_ONLY."
                )
            )
        if settled < 30:
            recommendations.append(
                "Keep AI/Quant/Fast/Quality thresholds unchanged until at least the first clean 30-trade checkpoint."
            )
        if completed_cycles < 5:
            recommendations.append(
                "Collect completed Compound cycles before changing the +50% target, -15% stop or 80/20 harvest."
            )
        if model["metadata_completeness_pct"] < 100:
            recommendations.append(
                "Use V6.7.2a attribution fields on new entries so AI, Quant, Fast, Quality, Deep, historical context and MFE/MAE remain linked."
            )

        return {
            "version": cls.VERSION,
            "name": "JASONG EVIDENCE & EXECUTION INTEGRITY",
            "system_efficiency_score": overall,
            "score_interpretation": (
                "Operational/evidence readiness indicator only; it is not a "
                "return forecast, win probability or guarantee."
            ),
            "scores": {
                "operational_readiness": operational_score,
                "evidence_quality": evidence_score,
                "strategy_performance": strategy_score,
                "compound_readiness": compound_score,
            },
            "strategy_score_confidence": strategy_confidence,
            "strategy_sample_reliability_pct": round(
                reliability * 100.0,
                1,
            ),
            "model_evidence": model,
            "broker_integrity": broker,
            "compound_evidence": {
                "status": compound_status,
                "completed_cycles": completed_cycles,
                "wins": int(compound_perf.get("wins") or 0),
                "losses": int(compound_perf.get("losses") or 0),
                "win_rate_pct": cls._safe_float(
                    compound_perf.get("win_rate_pct"),
                    0.0,
                ),
                "total_realised_profit": cls._safe_float(
                    compound_perf.get("total_realised_profit"),
                    0.0,
                ),
                "total_harvested": cls._safe_float(
                    compound_perf.get("total_harvested"),
                    0.0,
                ),
                "reserve_balance": cls._safe_float(
                    compound_perf.get("reserve_balance"),
                    0.0,
                ),
                "close_integrity": compound_close,
                "state_path": compound_path,
            },
            "persistence": {
                "jasong_state_path": jasong_path,
                "ig_state_path": ig_path,
                "compound_state_path": compound_path,
                "persistent_paths_ok": persistent_count,
                "persistent_paths_required": 3,
            },
            "historical_vs_forward": model["historical_vs_forward"],
            "blockers": blockers,
            "recommendations": recommendations,
            "next_checkpoints": {
                "clean_settled_model_trades": 30,
                "completed_compound_cycles": 5,
                "second_model_checkpoint": 75,
                "second_compound_checkpoint": 15,
                "serious_threshold_review_model_trades": 100,
                "serious_threshold_review_compound_cycles": 20,
            },
            "generated_at": now,
            "environment": "DEMO",
            "live_money_execution": False,
        }
