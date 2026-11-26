"""Identity and configuration for OpenAI-selected, protected DEMO entries."""
import os

STRATEGY_ID = "OPENAI_MARKET_ANALYST_V1"
STRATEGY_NAME = "OpenAI Market Analyst"


def enabled() -> bool:
    return os.getenv("JASONG_ANALYSIS_MODE", "OPENAI").strip().upper() == "OPENAI"


def discretionary_enabled() -> bool:
    return enabled() and os.getenv("JASONG_DEMO_EXECUTION_POLICY", "DISCRETIONARY").strip().upper() == "DISCRETIONARY"


def policy_status(discretionary: bool | None = None) -> dict:
    from runtime_version import VERSION
    discretionary = discretionary_enabled() if discretionary is None else discretionary
    controls = (["IG_DEMO_ONLY", "VALID_CLOSED_CANDLES", "FRESH_MODEL_DECISION",
                 "EXACT_INSTRUMENT", "ONE_SUBMISSION_PER_DECISION", "OWNED_POSITION_CLOSES"]
                if discretionary else ["IG_DEMO_ONLY", "VALID_CLOSED_CANDLES", "FRESH_DECISION_AND_QUOTE",
                    "BROKER_TRADEABILITY", "SPREAD_COST", "ACCOUNT_RISK_SIZING", "POSITION_AND_EXPOSURE_CAPS",
                    "DUPLICATE_PROTECTION", "ATTACHED_STOP_AND_TARGET", "CONFIGURED_NEWS_GUARD"])
    return {
        "version": VERSION, "strategy_ids": [STRATEGY_ID], "analysis_mode": "OPENAI",
        "decision_authority": "OPENAI_RESPONSES_STRUCTURED_ANALYSIS",
        "entry_policy": "AI selects BUY, SELL or WAIT from completed price evidence",
        "execution_policy": "AI_DISCRETIONARY_DEMO" if discretionary else "PROTECTED_DEMO",
        "exit_policy": "MODEL_CLOSE_DECISIONS" if discretionary else "ATTACHED_PROTECTION_AND_HORIZON",
        "fixed_strategy_filters_removed": ["EMA_CROSS", "ADX_REGIME", "RSI_DIVERGENCE",
                                           "ML_CONFIDENCE_FLOORS", "FAST_SCORE", "GEOGRAPHIC_SESSION",
                                           "FORWARD_PRIME", "FIXED_MINIMUM_REWARD_RISK"],
        "execution_controls": controls,
        "application_risk_caps_enabled": not discretionary,
        "mandatory_attached_protection": not discretionary,
        "application_quote_spread_dealing_distance_veto": not discretionary,
        "broker_rules": "IG_ENFORCED_MARKET_MARGIN_AND_ORDER_RULES_STILL_APPLY",
        "maximum_proposals_per_analysis": 3, "confidence_basis": "NO_WIN_PROBABILITY_CLAIM",
        "bitcoin_policy": "AI may propose Bitcoin whenever IG is tradeable; no weekday veto",
        "compound_policy": "AI_CATEGORY_EXECUTOR_ONLY",
        "environment": "IG_DEMO_ONLY", "live_money_execution": False,
    }
