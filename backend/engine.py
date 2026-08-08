from dataclasses import dataclass
import numpy as np
import pandas as pd
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression

FEATURES = [
    "RSI_14","MACD","MACD_SIGNAL","BB_WIDTH","ATR_PCT",
    "RET_1","RET_3","RET_5","VOL_20","BODY_PCT",
    "TREND_UP","TREND_STRONG"
]

@dataclass(frozen=True)
class RiskProfile:
    name: str
    risk_per_trade: float
    daily_loss_limit: float
    max_consecutive_losses: int
    min_confidence: float

PROFILES = {
    "Conservative": RiskProfile("Conservative", 0.005, 0.02, 2, 0.72),
    "Balanced": RiskProfile("Balanced", 0.010, 0.04, 3, 0.67),
    "Aggressive": RiskProfile("Aggressive", 0.020, 0.08, 4, 0.62),
}

def rule_score(row) -> float:
    s = 50.0
    if row["EMA_9"] > row["EMA_21"] > row["EMA_50"]:
        s += 14
    elif row["EMA_9"] < row["EMA_21"] < row["EMA_50"]:
        s -= 14
    s += 9 if row["MACD"] > row["MACD_SIGNAL"] else -9

    r = row["RSI_14"]
    if 52 <= r <= 68: s += 8
    elif 32 <= r <= 48: s -= 8
    elif r > 75: s -= 5
    elif r < 25: s += 5

    s += 5 if row["Close"] > row["BB_MID"] else -5
    s += 4 if row["RET_3"] > 0 else -4
    if abs(row["BODY_PCT"]) > 0.45:
        s += 3 * np.sign(row["BODY_PCT"])
    return float(np.clip(s, 0, 100))

def train_model(df: pd.DataFrame):
    x = df.copy()
    x["TARGET"] = (x["Close"].shift(-1) > x["Close"]).astype(int)
    x = x.dropna(subset=FEATURES + ["TARGET"]).copy()
    if len(x) < 150 or x["TARGET"].nunique() < 2:
        return None
    split = int(len(x) * 0.70)
    model = Pipeline([
        ("scale", StandardScaler()),
        ("clf", LogisticRegression(max_iter=1500, class_weight="balanced"))
    ])
    model.fit(x.iloc[:split][FEATURES], x.iloc[:split]["TARGET"])
    return model

def enrich(df: pd.DataFrame, model=None) -> pd.DataFrame:
    x = df.copy()
    x["RULE_SCORE"] = x.apply(rule_score, axis=1)
    x["ML_UP_PROB"] = 0.5
    valid = x[FEATURES].notna().all(axis=1)
    if model is not None and valid.any():
        x.loc[valid, "ML_UP_PROB"] = model.predict_proba(x.loc[valid, FEATURES])[:,1]

    x["UP_PROB"] = (x["RULE_SCORE"]/100.0)*0.55 + x["ML_UP_PROB"]*0.45
    x["CONFIDENCE"] = (abs(x["UP_PROB"]-0.5)*2).clip(0,1)
    x["DIRECTION"] = np.where(x["UP_PROB"] >= 0.5, "BUY", "SELL")

    med = x["ATR_PCT"].rolling(100, min_periods=20).median()
    x["QUALITY_OK"] = (
        x["ATR_PCT"].notna()
        & (x["ATR_PCT"] > med*0.35)
        & (x["ATR_PCT"] < med*3.0)
    )
    return x

def decision(df: pd.DataFrame, profile: RiskProfile):
    row = df.dropna().iloc[-1]
    confidence = float(row["CONFIDENCE"])

    if not bool(row["QUALITY_OK"]):
        d, reason = "WAIT", "Market-quality/volatility filter"
    elif confidence < profile.min_confidence:
        d, reason = "WAIT", f"Confidence below {profile.min_confidence:.0%}"
    else:
        d, reason = str(row["DIRECTION"]), "Rule + ML signal passed risk filter"

    return {
        "decision": d,
        "confidence": confidence,
        "combined_up_probability": float(row["UP_PROB"]),
        "rule_score": float(row["RULE_SCORE"]),
        "ml_probability": float(row["ML_UP_PROB"]),
        "price": float(row["Close"]),
        "rsi": float(row["RSI_14"]),
        "atr_pct": float(row["ATR_PCT"]),
        "reason": reason,
    }
