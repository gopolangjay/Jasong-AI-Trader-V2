"""Identity and configuration for OpenAI-selected IG DEMO entries."""
import os

STRATEGY_ID = "OPENAI_MARKET_ANALYST_V1"
STRATEGY_NAME = "OpenAI Market Analyst"
GUARDED_DEMO_POLICY = "AI_GUARDED_DEMO"


def enabled() -> bool:
    return os.getenv("JASONG_ANALYSIS_MODE", "OPENAI").strip().upper() == "OPENAI"


def discretionary_enabled() -> bool:
    # The existing Render value activates the guarded successor.
    mode = os.getenv("JASONG_DEMO_EXECUTION_POLICY", "GUARDED").strip().upper()
    return enabled() and mode in {"GUARDED", "DISCRETIONARY"}


def policy_status(discretionary: bool | None = None) -> dict:
    from runtime_version import VERSION
    discretionary = discretionary_enabled() if discretionary is None else discretionary
    controls = (["IG_DEMO_ONLY", "VALID_CLOSED_CANDLES", "FRESH_MODEL_DECISION",
                 "EXPLICIT_ENTER_NOW", "COMPLETED_M15_TRIGGER", "FRESH_IG_QUOTE_AND_SPREAD",
                 "STOP_TARGET_GEOMETRY", "ONE_OPEN_PER_MARKET", "ONE_PER_YEN_DIRECTION",
                 "EXACT_INSTRUMENT", "ONE_SUBMISSION_PER_DECISION", "OWNED_POSITION_CLOSES",
                 "EXPLICIT_CLOSE_ACTION", "CONSISTENT_CLOSE_REASON"]
                if discretionary else ["IG_DEMO_ONLY", "VALID_CLOSED_CANDLES", "FRESH_DECISION_AND_QUOTE",
                    "BROKER_TRADEABILITY", "SPREAD_COST", "ACCOUNT_RISK_SIZING", "POSITION_AND_EXPOSURE_CAPS",
                    "DUPLICATE_PROTECTION", "ATTACHED_STOP_AND_TARGET", "CONFIGURED_NEWS_GUARD"])
    return {
        "version": VERSION, "strategy_ids": [STRATEGY_ID], "analysis_mode": "OPENAI",
        "decision_authority": "OPENAI_RESPONSES_STRUCTURED_ANALYSIS",
        "entry_policy": ("ENTER_NOW requires a confirmed M15 trigger, fresh IG quote and available exposure"
                         if discretionary else "AI selects BUY, SELL or WAIT from completed price evidence"),
        "execution_policy": GUARDED_DEMO_POLICY if discretionary else "PROTECTED_DEMO",
        "exit_policy": "EXPLICIT_MODEL_CLOSE_DECISIONS" if discretionary else "ATTACHED_PROTECTION_AND_HORIZON",
        "fixed_strategy_filters_removed": ["EMA_CROSS", "ADX_REGIME", "RSI_DIVERGENCE",
                                           "ML_CONFIDENCE_FLOORS", "FAST_SCORE", "GEOGRAPHIC_SESSION",
                                           "FORWARD_PRIME", "FIXED_MINIMUM_REWARD_RISK"],
        "execution_controls": controls,
        "application_risk_caps_enabled": not discretionary,
        "mandatory_attached_protection": not discretionary,
        "guarded_entry_preflight": discretionary,
        "max_positions_per_market": 1 if discretionary else None,
        "max_positions_per_yen_direction": 1 if discretionary else None,
        "application_quote_spread_dealing_distance_veto": not discretionary,
        "broker_rules": "IG_ENFORCED_MARKET_MARGIN_AND_ORDER_RULES_STILL_APPLY",
        "maximum_proposals_per_analysis": 3, "confidence_basis": "NO_WIN_PROBABILITY_CLAIM",
        "bitcoin_policy": "AI may propose Bitcoin whenever IG is tradeable; no weekday veto",
        "compound_policy": "AI_CATEGORY_EXECUTOR_ONLY",
        "environment": "IG_DEMO_ONLY", "live_money_execution": False,
    }
