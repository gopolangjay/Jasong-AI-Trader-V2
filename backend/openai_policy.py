"""Identity and configuration for OpenAI-selected, protected DEMO entries."""
import os

STRATEGY_ID = "OPENAI_MARKET_ANALYST_V1"
STRATEGY_NAME = "OpenAI Market Analyst"


def enabled() -> bool:
    return os.getenv("JASONG_ANALYSIS_MODE", "OPENAI").strip().upper() == "OPENAI"


def policy_status() -> dict:
    from runtime_version import VERSION
    return {
        "version": VERSION, "strategy_ids": [STRATEGY_ID], "analysis_mode": "OPENAI",
        "decision_authority": "OPENAI_RESPONSES_STRUCTURED_ANALYSIS",
        "entry_policy": "AI selects BUY, SELL or WAIT from completed price evidence",
        "fixed_strategy_filters_removed": ["EMA_CROSS", "ADX_REGIME", "RSI_DIVERGENCE",
                                           "ML_CONFIDENCE_FLOORS", "FAST_SCORE", "GEOGRAPHIC_SESSION",
                                           "FORWARD_PRIME", "FIXED_MINIMUM_REWARD_RISK"],
        "execution_controls": ["IG_DEMO_ONLY", "VALID_CLOSED_CANDLES", "FRESH_DECISION_AND_QUOTE",
                               "BROKER_TRADEABILITY", "SPREAD_COST", "ACCOUNT_RISK_SIZING",
                               "POSITION_AND_EXPOSURE_CAPS", "DUPLICATE_PROTECTION",
                               "ATTACHED_STOP_AND_TARGET", "CONFIGURED_NEWS_GUARD"],
        "maximum_proposals_per_analysis": 3, "confidence_basis": "NO_WIN_PROBABILITY_CLAIM",
        "bitcoin_policy": "AI may propose Bitcoin whenever IG is tradeable; no weekday veto",
        "compound_policy": "AI_ENTRIES_USE_PROTECTED_ACCOUNT_RISK_EXECUTOR_ONLY",
        "environment": "IG_DEMO_ONLY", "live_money_execution": False,
    }
