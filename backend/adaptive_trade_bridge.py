"""
V6.3 paper-trade bridge.

Call adaptive_trade_permission(...) immediately before your existing PAPER
execution function. It does NOT place orders itself.
"""

from typing import Any, Dict
from adaptive_confidence import AdaptiveConfidenceGate


def adaptive_trade_permission(
    gate: AdaptiveConfidenceGate,
    market: str,
    direction: str,
    confidence: float,
    normal_min_confidence: float,
    existing_filters_pass: bool,
    risk_controls_pass: bool,
) -> Dict[str, Any]:
    confidence_gate = gate.evaluate(
        market=market,
        direction=direction,
        confidence=confidence,
        normal_min_confidence=normal_min_confidence,
    )

    allowed = bool(
        confidence_gate.get("allowed_by_confidence")
        and existing_filters_pass
        and risk_controls_pass
    )

    return {
        "paper_trade_allowed": allowed,
        "confidence_gate": confidence_gate,
        "existing_filters_pass": bool(existing_filters_pass),
        "risk_controls_pass": bool(risk_controls_pass),
        "broker_execution_enabled": False,
    }
