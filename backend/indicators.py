import numpy as np
import pandas as pd

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100/(1+rs))

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev = df["Close"].shift(1)
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev).abs(),
        (df["Low"] - prev).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    x["EMA_9"] = x["Close"].ewm(span=9, adjust=False).mean()
    x["EMA_21"] = x["Close"].ewm(span=21, adjust=False).mean()
    x["EMA_50"] = x["Close"].ewm(span=50, adjust=False).mean()
    x["RSI_14"] = rsi(x["Close"], 14)

    ema12 = x["Close"].ewm(span=12, adjust=False).mean()
    ema26 = x["Close"].ewm(span=26, adjust=False).mean()
    x["MACD"] = ema12 - ema26
    x["MACD_SIGNAL"] = x["MACD"].ewm(span=9, adjust=False).mean()

    mid = x["Close"].rolling(20).mean()
    std = x["Close"].rolling(20).std()
    x["BB_MID"] = mid
    x["BB_UPPER"] = mid + 2*std
    x["BB_LOWER"] = mid - 2*std
    x["BB_WIDTH"] = (x["BB_UPPER"] - x["BB_LOWER"]) / mid.replace(0, np.nan)

    x["ATR_14"] = atr(x)
    x["ATR_PCT"] = x["ATR_14"] / x["Close"]
    x["RET_1"] = x["Close"].pct_change()
    x["RET_3"] = x["Close"].pct_change(3)
    x["RET_5"] = x["Close"].pct_change(5)
    x["VOL_20"] = x["RET_1"].rolling(20).std()
    x["BODY"] = x["Close"] - x["Open"]
    x["RANGE"] = (x["High"] - x["Low"]).replace(0, np.nan)
    x["BODY_PCT"] = x["BODY"] / x["RANGE"]
    x["TREND_UP"] = (x["EMA_9"] > x["EMA_21"]).astype(int)
    x["TREND_STRONG"] = (x["EMA_21"] > x["EMA_50"]).astype(int)
    return x
