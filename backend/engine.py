from dataclasses import dataclass
import numpy as np
import pandas as pd

from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression


FEATURES = [
    "RSI_14",
    "MACD",
    "MACD_SIGNAL",
    "BB_WIDTH",
    "ATR_PCT",
    "RET_1",
    "RET_3",
    "RET_5",
    "VOL_20",
    "BODY_PCT",
    "TREND_UP",
    "TREND_STRONG",
]


@dataclass(frozen=True)
class RiskProfile:
    name: str
    risk_per_trade: float
    daily_loss_limit: float
    max_consecutive_losses: int
    min_confidence: float


PROFILES = {
    "Conservative": RiskProfile(
        "Conservative", 0.005, 0.02, 2, 0.72
    ),
    "Balanced": RiskProfile(
        "Balanced", 0.010, 0.04, 3, 0.67
    ),
    "Aggressive": RiskProfile(
        "Aggressive", 0.020, 0.08, 4, 0.62
    ),
}


# Number of candles used to judge whether a prediction was correct.
PREDICTION_HORIZON = 4

# Ignore tiny future moves. The threshold is based on volatility.
ATR_MOVE_MULTIPLIER = 0.35


def rule_score(row) -> float:
    """
    Technical-analysis score from 0 to 100.
    50 = neutral.
    Above 50 = bullish bias.
    Below 50 = bearish bias.
    """

    score = 50.0

    # EMA trend structure
    if row["EMA_9"] > row["EMA_21"] > row["EMA_50"]:
        score += 14
    elif row["EMA_9"] < row["EMA_21"] < row["EMA_50"]:
        score -= 14

    # MACD confirmation
    if row["MACD"] > row["MACD_SIGNAL"]:
        score += 9
    else:
        score -= 9

    # RSI
    rsi = row["RSI_14"]

    if 52 <= rsi <= 68:
        score += 8
    elif 32 <= rsi <= 48:
        score -= 8
    elif rsi > 75:
        score -= 5
    elif rsi < 25:
        score += 5

    # Bollinger Band position
    if row["Close"] > row["BB_MID"]:
        score += 5
    else:
        score -= 5

    # Short-term momentum
    if row["RET_3"] > 0:
        score += 4
    else:
        score -= 4

    # Candle strength
    if abs(row["BODY_PCT"]) > 0.45:
        score += 3 * np.sign(row["BODY_PCT"])

    return float(np.clip(score, 0, 100))


def train_model(df: pd.DataFrame):
    """
    Train the ML model using a multi-candle, volatility-aware target.

    Instead of asking:
        "Will the very next candle go up?"

    V4 asks:
        "Will price make a meaningful move over the next several candles?"
    """

    x = df.copy()

    future_close = x["Close"].shift(-PREDICTION_HORIZON)

    future_return = (
        future_close - x["Close"]
    ) / x["Close"]

    move_threshold = (
        x["ATR_PCT"] * ATR_MOVE_MULTIPLIER
    )

    # 1 = meaningful upward move
    # 0 = meaningful downward move
    # NaN = movement too small / noise
    x["TARGET"] = np.where(
        future_return > move_threshold,
        1,
        np.where(
            future_return < -move_threshold,
            0,
            np.nan,
        ),
    )

    x = x.dropna(
        subset=FEATURES + ["TARGET"]
    ).copy()

    if len(x) < 150:
        return None

    if x["TARGET"].nunique() < 2:
        return None

    split = int(len(x) * 0.70)

    training = x.iloc[:split].copy()

    if training["TARGET"].nunique() < 2:
        return None

    model = Pipeline(
        [
            ("scale", StandardScaler()),
            (
                "clf",
                LogisticRegression(
                    max_iter=2000,
                    class_weight="balanced",
                    random_state=42,
                ),
            ),
        ]
    )

    model.fit(
        training[FEATURES],
        training["TARGET"].astype(int),
    )

    return model


def enrich(df: pd.DataFrame, model=None) -> pd.DataFrame:
    """
    Add rule score, ML probability, combined probability,
    confidence and market-quality filters.
    """

    x = df.copy()

    x["RULE_SCORE"] = x.apply(
        rule_score,
        axis=1,
    )

    # Neutral probability when ML model unavailable
    x["ML_UP_PROB"] = 0.50

    valid = x[FEATURES].notna().all(axis=1)

    if model is not None and valid.any():
        x.loc[
            valid,
            "ML_UP_PROB",
        ] = model.predict_proba(
            x.loc[valid, FEATURES]
        )[:, 1]

    # Hybrid system:
    # 55% technical rules
    # 45% machine learning
    rule_probability = x["RULE_SCORE"] / 100.0

    x["UP_PROB"] = (
        rule_probability * 0.55
        + x["ML_UP_PROB"] * 0.45
    )

    # Confidence represents distance from 50/50.
    x["CONFIDENCE"] = (
        np.abs(x["UP_PROB"] - 0.50) * 2.0
    ).clip(0, 1)

    x["DIRECTION"] = np.where(
        x["UP_PROB"] >= 0.50,
        "BUY",
        "SELL",
    )

    # Volatility quality filter
    atr_median = x["ATR_PCT"].rolling(
        100,
        min_periods=20,
    ).median()

    x["QUALITY_OK"] = (
        x["ATR_PCT"].notna()
        & atr_median.notna()
        & (x["ATR_PCT"] > atr_median * 0.35)
        & (x["ATR_PCT"] < atr_median * 3.0)
    )

    return x


def decision(
    df: pd.DataFrame,
    profile: RiskProfile,
):
    """
    Produce the latest trading decision.
    """

    clean = df.dropna().copy()

    if clean.empty:
        return {
            "decision": "WAIT",
            "confidence": 0.0,
            "reason": "Not enough valid market data",
        }

    row = clean.iloc[-1]

    confidence = float(
        row["CONFIDENCE"]
    )

    if not bool(row["QUALITY_OK"]):
        direction = "WAIT"
        reason = "Market-quality/volatility filter"

    elif confidence < profile.min_confidence:
        direction = "WAIT"
        reason = (
            f"Confidence below "
            f"{profile.min_confidence:.0%}"
        )

    else:
        direction = str(
            row["DIRECTION"]
        )

        reason = (
            "Rule + ML signal passed "
            "risk filter"
        )

    return {
        "decision": direction,
        "confidence": confidence,

        "combined_up_probability":
            float(row["UP_PROB"]),

        "rule_score":
            float(row["RULE_SCORE"]),

        "ml_probability":
            float(row["ML_UP_PROB"]),

        "price":
            float(row["Close"]),

        "rsi":
            float(row["RSI_14"]),

        "atr_pct":
            float(row["ATR_PCT"]),

        "prediction_horizon":
            PREDICTION_HORIZON,

        "reason":
            reason,
    }
