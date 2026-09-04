from __future__ import annotations

"""Runtime/API presentation overrides for the V6.12 FX policy.

The category portfolio serves both Gold and FX, so its legacy scalar minimum R
was misleading once FX moved to a 0.3R DEMO floor while Gold retained 2R.
"""

from typing import Any, Dict

import category_execution_engine as execution


if not getattr(execution, "_V612_STATUS_INSTALLED", False):
    _original_status = execution.CategoryExecutionEngine.status

    def _status_v612(self: Any) -> Dict[str, Any]:
        out = dict(_original_status(self))
        out["version"] = execution.CategoryExecutionEngine.VERSION
        out["active_strategy"] = (
            f"{execution.FX_STRATEGY_ID} + {execution.XAU_STRATEGY_ID}"
        )
        out["active_strategies"] = [
            execution.FX_STRATEGY_ID,
            execution.XAU_STRATEGY_ID,
        ]
        # Backward-compatible scalar consumed by the current mobile UI.
        out["minimum_target_r"] = 0.30
        out["minimum_target_r_by_market"] = {
            "FOREX": 0.30,
            "METALS_GOLD": 2.00,
        }
        out["preferred_target_r_by_market"] = {
            "FOREX": 2.00,
            "METALS_GOLD": 2.00,
        }
        out["forex_demo_policy"] = {
            "version": "6.12-adaptive-fx-session-momentum-v1",
            "quant_min_pct": 20.0,
            "model_ai_min_pct": 30.0,
            "overall_setup_confidence_min_pct": 30.0,
            "market_structure_min_pct": 60.0,
            "optional_confluence_required": 2,
            "optional_confluence_total": 5,
            "a_grade": "3/5 optional, >=30% confidence, >=0.5R",
            "b_grade": "2/5 optional, >=30% confidence, >=0.3R",
            "news_signal_veto": False,
            "news_execution_guard": True,
            "ig_tradeability_spread_size_execution_guard": True,
            "environment": "IG_DEMO_ONLY",
        }
        return out

    execution.CategoryExecutionEngine.status = _status_v612
    execution._V612_STATUS_INSTALLED = True
