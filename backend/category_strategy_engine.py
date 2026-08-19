from __future__ import annotations

import json
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

import pandas as pd

# ---------------------------------------------------------------------------
# JASONG AI TRADER V6.9.3 - COMPLETE IG DEMO TESTING / 60% WALK-FORWARD POLICY
# ---------------------------------------------------------------------------
# Design contract
#   * six independent specialist categories;
#   * live confidence floors remain 28% Quant + 40% directional Model-AI;
#   * final holdout target is 60% WR with PF >=1.20; live confidence remains separate;
#   * each category ranks at most five selections;
#   * ranks #1 and #2 are Compound slot candidates only when every gate passes;
#   * finite strategy variants are selected BEFORE the untouched final holdout;
#   * final 60% validation is allowed only when selection-window stability and 3-fold WF also pass;
#   * execution remains IG DEMO only; this module contains no production URL.
# ---------------------------------------------------------------------------

VERSION = "6.9.3"
QUANT_MIN_CONFIDENCE = 0.28
MODEL_AI_MIN_CONFIDENCE = 0.40
HISTORICAL_WIN_RATE_TARGET = 0.60
HISTORICAL_PROFIT_FACTOR_TARGET = 1.20
HISTORICAL_MIN_TRADES = 30
STANDARD_FAST_SCORE_MIN = 60.0
COMPOUND_FAST_SCORE_MIN = 60.0
TOP_N_PER_CATEGORY = 5
COMPOUND_SLOTS_PER_CATEGORY = 2
EVIDENCE_SCHEMA_VERSION = 3
WALK_FORWARD_FOLDS = 3
WALK_FORWARD_MIN_FOLD_TRADES = 5
WALK_FORWARD_MIN_FOLDS = 2
WALK_FORWARD_MIN_FOLD_WIN_RATE = 0.60
WALK_FORWARD_MIN_MEDIAN_WIN_RATE = 0.60
WALK_FORWARD_MIN_PROFITABLE_FOLDS = 2

CATEGORY_ORDER = ("FOREX", "INDICES", "CRYPTO", "METALS", "ENERGY", "SHARES")

CATEGORY_RULES: Dict[str, Dict[str, Any]] = {
    "FOREX": {
        "strategy_id": "FX_REGIME_TREND_PULLBACK_V1",
        "strategy_name": "FX Regime + Trend Pullback",
        "holding_bars": 4,
        "cost_bps": 1.5,
        "spread_gate_bps": 8.0,
    },
    "INDICES": {
        "strategy_id": "INDEX_SESSION_MOMENTUM_V1",
        "strategy_name": "Index Session Momentum",
        "holding_bars": 2,
        "cost_bps": 2.0,
        "spread_gate_bps": 18.0,
    },
    "CRYPTO": {
        "strategy_id": "CRYPTO_REGIME_ROUTER_V1",
        "strategy_name": "Crypto Regime Momentum / Range Router",
        "holding_bars": 4,
        "cost_bps": 12.0,
        "spread_gate_bps": 80.0,
    },
    "METALS": {
        "strategy_id": "METALS_TREND_VOLATILITY_V1",
        "strategy_name": "Metals Trend + Volatility",
        "holding_bars": 8,
        "cost_bps": 4.0,
        "spread_gate_bps": 22.0,
    },
    "ENERGY": {
        "strategy_id": "ENERGY_SESSION_BREAKOUT_V1",
        "strategy_name": "Energy Session Momentum + Breakout",
        "holding_bars": 3,
        "cost_bps": 5.0,
        "spread_gate_bps": 22.0,
    },
    "SHARES": {
        "strategy_id": "SHARE_EVENT_MOMENTUM_V1",
        "strategy_name": "Share Price/Volume Event Momentum",
        "holding_bars": 4,
        "cost_bps": 5.0,
        "spread_gate_bps": 35.0,
    },
}


def _compound_asset_class(category: str) -> str:
    return {
        "FOREX": "FX",
        "INDICES": "INDEX",
        "CRYPTO": "CRYPTO",
        "METALS": "COMMODITY",
        "ENERGY": "COMMODITY",
        "SHARES": "SHARE",
    }.get(str(category or "").upper().strip(), str(category or "").upper().strip())


def _seed(
    key: str,
    name: str,
    category: str,
    analysis_symbol: str,
    *,
    ig_search_terms: Iterable[str],
    expected_types: Iterable[str] = (),
    name_tokens: Iterable[str] = (),
    exposure_tags: Iterable[str] = (),
    ig_symbol: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "key": key,
        "name": name,
        "category": category,
        "asset_class": _compound_asset_class(category),
        "analysis_symbol": analysis_symbol,
        "ig_search_terms": list(ig_search_terms),
        "expected_types": list(expected_types),
        "name_tokens": list(name_tokens),
        "exposure_tags": list(exposure_tags),
        "ig_symbol": ig_symbol,
    }


CATEGORY_MARKET_SEEDS: List[Dict[str, Any]] = [
    # FOREX - separate specialist engine, mature liquid core.
    _seed("EURUSD", "EUR/USD", "FOREX", "EURUSD=X", ig_search_terms=["EUR/USD"], expected_types=["CURRENCIES"], name_tokens=["EUR", "USD"], exposure_tags=["EUR", "USD", "FX_MAJOR"], ig_symbol="EUR/USD"),
    _seed("GBPUSD", "GBP/USD", "FOREX", "GBPUSD=X", ig_search_terms=["GBP/USD"], expected_types=["CURRENCIES"], name_tokens=["GBP", "USD"], exposure_tags=["GBP", "USD", "FX_MAJOR"], ig_symbol="GBP/USD"),
    _seed("USDJPY", "USD/JPY", "FOREX", "USDJPY=X", ig_search_terms=["USD/JPY"], expected_types=["CURRENCIES"], name_tokens=["USD", "JPY"], exposure_tags=["USD", "JPY", "FX_MAJOR"], ig_symbol="USD/JPY"),
    _seed("AUDUSD", "AUD/USD", "FOREX", "AUDUSD=X", ig_search_terms=["AUD/USD"], expected_types=["CURRENCIES"], name_tokens=["AUD", "USD"], exposure_tags=["AUD", "USD", "FX_MAJOR"], ig_symbol="AUD/USD"),
    _seed("NZDUSD", "NZD/USD", "FOREX", "NZDUSD=X", ig_search_terms=["NZD/USD"], expected_types=["CURRENCIES"], name_tokens=["NZD", "USD"], exposure_tags=["NZD", "USD", "FX_MAJOR"], ig_symbol="NZD/USD"),
    _seed("USDCAD", "USD/CAD", "FOREX", "CAD=X", ig_search_terms=["USD/CAD"], expected_types=["CURRENCIES"], name_tokens=["USD", "CAD"], exposure_tags=["USD", "CAD", "FX_MAJOR"], ig_symbol="USD/CAD"),
    _seed("USDCHF", "USD/CHF", "FOREX", "CHF=X", ig_search_terms=["USD/CHF"], expected_types=["CURRENCIES"], name_tokens=["USD", "CHF"], exposure_tags=["USD", "CHF", "FX_MAJOR"], ig_symbol="USD/CHF"),
    _seed("EURJPY", "EUR/JPY", "FOREX", "EURJPY=X", ig_search_terms=["EUR/JPY"], expected_types=["CURRENCIES"], name_tokens=["EUR", "JPY"], exposure_tags=["EUR", "JPY", "FX_CROSS"], ig_symbol="EUR/JPY"),
    _seed("GBPJPY", "GBP/JPY", "FOREX", "GBPJPY=X", ig_search_terms=["GBP/JPY"], expected_types=["CURRENCIES"], name_tokens=["GBP", "JPY"], exposure_tags=["GBP", "JPY", "FX_CROSS"], ig_symbol="GBP/JPY"),

    # INDICES
    _seed("US500", "US 500", "INDICES", "^GSPC", ig_search_terms=["US 500"], expected_types=["INDICES"], name_tokens=["US", "500"], exposure_tags=["US_EQUITY", "GLOBAL_EQUITY"]),
    _seed("USTECH100", "US Tech 100", "INDICES", "^NDX", ig_search_terms=["US Tech 100", "Nasdaq 100"], expected_types=["INDICES"], name_tokens=["100"], exposure_tags=["US_EQUITY", "US_TECH", "GLOBAL_EQUITY"]),
    _seed("WALLSTREET", "Wall Street", "INDICES", "^DJI", ig_search_terms=["Wall Street"], expected_types=["INDICES"], name_tokens=["WALL", "STREET"], exposure_tags=["US_EQUITY", "GLOBAL_EQUITY"]),
    _seed("GERMANY40", "Germany 40", "INDICES", "^GDAXI", ig_search_terms=["Germany 40"], expected_types=["INDICES"], name_tokens=["GERMANY", "40"], exposure_tags=["EU_EQUITY", "GLOBAL_EQUITY"]),
    _seed("FTSE100", "FTSE 100", "INDICES", "^FTSE", ig_search_terms=["FTSE 100"], expected_types=["INDICES"], name_tokens=["FTSE", "100"], exposure_tags=["UK_EQUITY", "GLOBAL_EQUITY"]),
    _seed("FRANCE40", "France 40", "INDICES", "^FCHI", ig_search_terms=["France 40"], expected_types=["INDICES"], name_tokens=["FRANCE", "40"], exposure_tags=["EU_EQUITY", "GLOBAL_EQUITY"]),
    _seed("EURO50", "EU Stocks 50", "INDICES", "^STOXX50E", ig_search_terms=["EU Stocks 50", "Euro Stoxx 50"], expected_types=["INDICES"], name_tokens=["50"], exposure_tags=["EU_EQUITY", "GLOBAL_EQUITY"]),
    _seed("JAPAN225", "Japan 225", "INDICES", "^N225", ig_search_terms=["Japan 225"], expected_types=["INDICES"], name_tokens=["JAPAN", "225"], exposure_tags=["JP_EQUITY", "GLOBAL_EQUITY"]),
    _seed("AUSTRALIA200", "Australia 200", "INDICES", "^AXJO", ig_search_terms=["Australia 200"], expected_types=["INDICES"], name_tokens=["AUSTRALIA", "200"], exposure_tags=["AU_EQUITY", "GLOBAL_EQUITY"]),
    _seed("SA40", "South Africa 40", "INDICES", "^J200.JO", ig_search_terms=["South Africa 40", "SA 40"], expected_types=["INDICES"], name_tokens=["40"], exposure_tags=["ZA_EQUITY", "GLOBAL_EQUITY"]),

    # CRYPTO
    _seed("BITCOIN", "Bitcoin", "CRYPTO", "BTC-USD", ig_search_terms=["Bitcoin"], name_tokens=["BITCOIN"], exposure_tags=["CRYPTO", "CRYPTO_LARGE_CAP"]),
    _seed("ETHER", "Ether", "CRYPTO", "ETH-USD", ig_search_terms=["Ether", "Ethereum"], name_tokens=["ETHER"], exposure_tags=["CRYPTO", "CRYPTO_LARGE_CAP"]),
    _seed("SOLANA", "Solana", "CRYPTO", "SOL-USD", ig_search_terms=["Solana"], name_tokens=["SOLANA"], exposure_tags=["CRYPTO", "CRYPTO_ALT"]),
    _seed("XRP", "XRP", "CRYPTO", "XRP-USD", ig_search_terms=["XRP", "Ripple"], name_tokens=["XRP"], exposure_tags=["CRYPTO", "CRYPTO_ALT"]),
    _seed("LITECOIN", "Litecoin", "CRYPTO", "LTC-USD", ig_search_terms=["Litecoin"], name_tokens=["LITECOIN"], exposure_tags=["CRYPTO", "CRYPTO_ALT"]),

    # METALS
    _seed("GOLD", "Gold", "METALS", "GC=F", ig_search_terms=["Spot Gold", "Gold"], expected_types=["COMMODITIES"], name_tokens=["GOLD"], exposure_tags=["PRECIOUS_METALS", "COMMODITIES"]),
    _seed("SILVER", "Silver", "METALS", "SI=F", ig_search_terms=["Spot Silver", "Silver"], expected_types=["COMMODITIES"], name_tokens=["SILVER"], exposure_tags=["PRECIOUS_METALS", "COMMODITIES"]),
    _seed("COPPER", "Copper", "METALS", "HG=F", ig_search_terms=["Copper"], expected_types=["COMMODITIES"], name_tokens=["COPPER"], exposure_tags=["INDUSTRIAL_METALS", "COMMODITIES"]),

    # ENERGY
    _seed("USCRUDE", "US Crude", "ENERGY", "CL=F", ig_search_terms=["US Crude", "Oil - US Crude"], expected_types=["COMMODITIES"], name_tokens=["CRUDE"], exposure_tags=["ENERGY", "COMMODITIES"]),
    _seed("BRENT", "Brent Crude", "ENERGY", "BZ=F", ig_search_terms=["Brent Crude", "Oil - Brent Crude"], expected_types=["COMMODITIES"], name_tokens=["BRENT"], exposure_tags=["ENERGY", "COMMODITIES"]),
    _seed("NATGAS", "Natural Gas", "ENERGY", "NG=F", ig_search_terms=["Natural Gas"], expected_types=["COMMODITIES"], name_tokens=["NATURAL", "GAS"], exposure_tags=["ENERGY", "COMMODITIES"]),

    # SHARES - liquid starter universe. A price/volume event proxy is used unless
    # a real earnings/news feed is explicitly wired later; the engine never fakes a catalyst.
    _seed("AAPL", "Apple", "SHARES", "AAPL", ig_search_terms=["Apple"], expected_types=["SHARES"], name_tokens=["APPLE"], exposure_tags=["US_EQUITY", "US_TECH", "MEGA_CAP"]),
    _seed("MSFT", "Microsoft", "SHARES", "MSFT", ig_search_terms=["Microsoft"], expected_types=["SHARES"], name_tokens=["MICROSOFT"], exposure_tags=["US_EQUITY", "US_TECH", "MEGA_CAP"]),
    _seed("NVDA", "NVIDIA", "SHARES", "NVDA", ig_search_terms=["NVIDIA"], expected_types=["SHARES"], name_tokens=["NVIDIA"], exposure_tags=["US_EQUITY", "US_TECH", "SEMICONDUCTORS"]),
    _seed("AMZN", "Amazon", "SHARES", "AMZN", ig_search_terms=["Amazon"], expected_types=["SHARES"], name_tokens=["AMAZON"], exposure_tags=["US_EQUITY", "MEGA_CAP", "CONSUMER_TECH"]),
    _seed("GOOGL", "Alphabet", "SHARES", "GOOGL", ig_search_terms=["Alphabet", "Google"], expected_types=["SHARES"], name_tokens=["ALPHABET"], exposure_tags=["US_EQUITY", "US_TECH", "MEGA_CAP"]),
    _seed("META", "Meta Platforms", "SHARES", "META", ig_search_terms=["Meta Platforms", "Meta"], expected_types=["SHARES"], name_tokens=["META"], exposure_tags=["US_EQUITY", "US_TECH", "MEGA_CAP"]),
    _seed("TSLA", "Tesla", "SHARES", "TSLA", ig_search_terms=["Tesla"], expected_types=["SHARES"], name_tokens=["TESLA"], exposure_tags=["US_EQUITY", "EV", "GROWTH"]),
    _seed("JPM", "JPMorgan Chase", "SHARES", "JPM", ig_search_terms=["JPMorgan Chase", "JP Morgan"], expected_types=["SHARES"], name_tokens=["JPMORGAN"], exposure_tags=["US_EQUITY", "US_FINANCIALS"]),
    _seed("XOM", "Exxon Mobil", "SHARES", "XOM", ig_search_terms=["Exxon Mobil"], expected_types=["SHARES"], name_tokens=["EXXON"], exposure_tags=["US_EQUITY", "ENERGY"]),
    _seed("AMD", "AMD", "SHARES", "AMD", ig_search_terms=["Advanced Micro Devices", "AMD"], expected_types=["SHARES"], name_tokens=["MICRO", "DEVICES"], exposure_tags=["US_EQUITY", "US_TECH", "SEMICONDUCTORS"]),
]


@dataclass(frozen=True)
class StrategySignal:
    direction: str
    quant_confidence: float
    regime: str
    long_score: float
    short_score: float
    max_score: float
    reason: str


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        if not math.isfinite(number):
            return default
        return number
    except Exception:
        return default


def _confidence01(value: Any) -> float:
    number = _safe_float(value, 0.0)
    if number > 1.0 and number <= 100.0:
        number /= 100.0
    return max(0.0, min(1.0, number))


def _profit_factor(values: Iterable[float]) -> float:
    values = list(values)
    gp = sum(v for v in values if v > 0)
    gl = abs(sum(v for v in values if v < 0))
    if gl <= 1e-12:
        return 9.99 if gp > 0 else 0.0
    return gp / gl


def _max_drawdown(values: Iterable[float]) -> float:
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for value in values:
        r = max(-0.50, min(0.50, _safe_float(value)))
        equity *= max(1e-9, 1.0 + r)
        peak = max(peak, equity)
        if peak > 0:
            worst = max(worst, (peak - equity) / peak)
    return worst


def _rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()
    avg_loss = loss.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()
    rs = avg_gain / avg_loss.replace(0.0, float("nan"))
    result = 100.0 - 100.0 / (1.0 + rs)
    return result.fillna(50.0)


def _atr(frame: pd.DataFrame, length: int = 14) -> pd.Series:
    prev_close = frame["Close"].shift(1)
    tr = pd.concat(
        [
            frame["High"] - frame["Low"],
            (frame["High"] - prev_close).abs(),
            (frame["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()


def _adx(frame: pd.DataFrame, length: int = 14) -> pd.Series:
    high = frame["High"]
    low = frame["Low"]
    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0.0), 0.0)
    minus_dm = down.where((down > up) & (down > 0.0), 0.0)
    atr = _atr(frame, length).replace(0.0, float("nan"))
    plus_di = 100.0 * plus_dm.ewm(alpha=1.0 / length, adjust=False).mean() / atr
    minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / length, adjust=False).mean() / atr
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, float("nan"))
    return dx.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean().fillna(0.0)


def _feature_frame(raw: pd.DataFrame) -> pd.DataFrame:
    if raw is None or raw.empty:
        raise ValueError("No market data supplied")
    frame = raw.copy()
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = [str(col[0]) for col in frame.columns]
    for col in ("Open", "High", "Low", "Close"):
        if col not in frame.columns:
            raise ValueError(f"Missing required OHLC column: {col}")
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    if "Volume" not in frame.columns:
        frame["Volume"] = 0.0
    frame["Volume"] = pd.to_numeric(frame["Volume"], errors="coerce").fillna(0.0)
    frame = frame.dropna(subset=["Open", "High", "Low", "Close"]).copy()
    if len(frame) < 220:
        raise ValueError(f"Insufficient history: {len(frame)} rows; need at least 220")

    c = frame["Close"]
    frame["EMA20"] = c.ewm(span=20, adjust=False).mean()
    frame["EMA50"] = c.ewm(span=50, adjust=False).mean()
    frame["EMA200"] = c.ewm(span=200, adjust=False).mean()
    frame["RSI14"] = _rsi(c, 14)
    frame["ATR14"] = _atr(frame, 14)
    frame["ATR_PCT"] = frame["ATR14"] / c.replace(0.0, float("nan"))
    frame["ADX14"] = _adx(frame, 14)
    frame["RET1"] = c.pct_change(1)
    frame["RET2"] = c.pct_change(2)
    frame["RET4"] = c.pct_change(4)
    frame["RET8"] = c.pct_change(8)
    frame["RET20"] = c.pct_change(20)
    frame["VOL20"] = frame["RET1"].rolling(20).std()
    frame["VOL20_MED"] = frame["VOL20"].rolling(80).median()
    frame["ATR_Q95"] = frame["ATR_PCT"].rolling(120).quantile(0.95)
    frame["MA20"] = c.rolling(20).mean()
    frame["STD20"] = c.rolling(20).std()
    frame["BB_Z"] = (c - frame["MA20"]) / frame["STD20"].replace(0.0, float("nan"))
    frame["HIGH20_PREV"] = frame["High"].rolling(20).max().shift(1)
    frame["LOW20_PREV"] = frame["Low"].rolling(20).min().shift(1)
    frame["BREAKOUT_UP"] = c > frame["HIGH20_PREV"]
    frame["BREAKOUT_DOWN"] = c < frame["LOW20_PREV"]
    frame["GAP_PCT"] = frame["Open"] / c.shift(1).replace(0.0, float("nan")) - 1.0
    vol_median = frame["Volume"].rolling(20).median().replace(0.0, float("nan"))
    frame["REL_VOLUME"] = (frame["Volume"] / vol_median).replace([float("inf"), float("-inf")], float("nan")).fillna(1.0)
    frame["EMA20_ATR_DIST"] = (c - frame["EMA20"]) / frame["ATR14"].replace(0.0, float("nan"))

    # Session features. When a timestamp index is unavailable, rolling proxies
    # keep the strategy deterministic instead of inventing exchange session times.
    if isinstance(frame.index, pd.DatetimeIndex):
        session_key = pd.Series(frame.index.date, index=frame.index)
        frame["SESSION_OPEN"] = frame["Open"].groupby(session_key).transform("first")
        typical = (frame["High"] + frame["Low"] + frame["Close"]) / 3.0
        positive_vol = frame["Volume"].where(frame["Volume"] > 0.0, 1.0)
        pv = typical * positive_vol
        frame["SESSION_VWAP"] = pv.groupby(session_key).cumsum() / positive_vol.groupby(session_key).cumsum()
    else:
        frame["SESSION_OPEN"] = frame["Open"].rolling(8, min_periods=1).first() if hasattr(frame["Open"].rolling(8), "first") else frame["Open"].shift(7).fillna(frame["Open"])
        typical = (frame["High"] + frame["Low"] + frame["Close"]) / 3.0
        frame["SESSION_VWAP"] = typical.rolling(8, min_periods=1).mean()
    frame["SESSION_RETURN"] = c / frame["SESSION_OPEN"].replace(0.0, float("nan")) - 1.0
    frame["VWAP_ATR_DIST"] = (c - frame["SESSION_VWAP"]) / frame["ATR14"].replace(0.0, float("nan"))

    # Preserve model probability produced by the existing Jasong Rule+ML stack.
    up_col = None
    for candidate in ("UP_PROB", "combined_up_probability", "AI_UP", "up_probability"):
        if candidate in frame.columns:
            up_col = candidate
            break
    if up_col is not None:
        frame["JASONG_UP_PROB"] = frame[up_col].map(_confidence01)
    else:
        frame["JASONG_UP_PROB"] = 0.0

    return frame.replace([float("inf"), float("-inf")], float("nan"))


def _score_to_signal(long_score: float, short_score: float, max_score: float, min_score: float, regime: str, reason: str) -> StrategySignal:
    strongest = max(long_score, short_score)
    edge = abs(long_score - short_score)
    direction = "WAIT"
    if strongest >= min_score and edge >= 0.70:
        direction = "BUY" if long_score > short_score else "SELL"
    # Deliberately calibrated independently from historical win rate.
    # This is the user's live 28 confidence path.
    base = strongest / max(max_score, 1e-9)
    separation = edge / max(max_score, 1e-9)
    quant = max(0.0, min(0.95, 0.82 * base + 0.18 * separation)) if direction != "WAIT" else max(0.0, min(0.27, 0.20 * base))
    return StrategySignal(direction, quant, regime, long_score, short_score, max_score, reason)


def _fx_signal(row: pd.Series) -> StrategySignal:
    long = short = 0.0
    if row["EMA20"] > row["EMA50"]: long += 1.25
    else: short += 1.25
    if row["EMA50"] > row["EMA200"]: long += 1.00
    else: short += 1.00
    if row["Close"] > row["EMA20"]: long += 0.75
    else: short += 0.75
    if row["ADX14"] >= 20.0:
        (long if row["RET4"] > 0 else short)
        if row["RET4"] > 0: long += 1.00
        elif row["RET4"] < 0: short += 1.00
    if 52.0 <= row["RSI14"] <= 72.0: long += 0.75
    if 28.0 <= row["RSI14"] <= 48.0: short += 0.75
    if abs(_safe_float(row["EMA20_ATR_DIST"])) <= 1.2:
        if row["EMA20"] > row["EMA50"]: long += 0.75
        elif row["EMA20"] < row["EMA50"]: short += 0.75
    regime = "TREND" if row["ADX14"] >= 20.0 else "RANGE"
    return _score_to_signal(long, short, 5.5, 3.25, regime, "EMA20/50/200 + ADX + RSI + pullback alignment")


def _index_signal(row: pd.Series) -> StrategySignal:
    long = short = 0.0
    if row["SESSION_RETURN"] > 0: long += 1.25
    elif row["SESSION_RETURN"] < 0: short += 1.25
    if row["Close"] > row["SESSION_VWAP"]: long += 1.25
    else: short += 1.25
    if row["RET2"] > 0: long += 0.75
    elif row["RET2"] < 0: short += 0.75
    if row["RET4"] > 0: long += 0.75
    elif row["RET4"] < 0: short += 0.75
    if row["ADX14"] >= 18.0:
        if row["EMA20"] > row["EMA50"]: long += 0.75
        else: short += 0.75
    if row["REL_VOLUME"] >= 1.10:  # only adds conviction to the existing direction
        if long > short: long += 0.50
        elif short > long: short += 0.50
    regime = "SESSION_MOMENTUM" if abs(_safe_float(row["SESSION_RETURN"])) > 0 else "NEUTRAL"
    return _score_to_signal(long, short, 5.25, 3.15, regime, "session return + VWAP + short-horizon momentum + ADX")


def _crypto_signal(row: pd.Series) -> StrategySignal:
    atr_pct = _safe_float(row["ATR_PCT"])
    panic_threshold = _safe_float(row["ATR_Q95"], 1.0)
    if panic_threshold > 0 and atr_pct >= panic_threshold and abs(_safe_float(row["RET1"])) >= max(0.02, 2.0 * _safe_float(row["VOL20"])):
        return StrategySignal("WAIT", 0.0, "PANIC", 0.0, 0.0, 1.0, "panic-volatility regime: no trade")

    adx = _safe_float(row["ADX14"])
    long = short = 0.0
    if adx >= 22.0:
        regime = "TREND"
        if row["EMA20"] > row["EMA50"]: long += 1.25
        else: short += 1.25
        if row["EMA50"] > row["EMA200"]: long += 0.75
        else: short += 0.75
        if row["RET4"] > 0: long += 1.0
        elif row["RET4"] < 0: short += 1.0
        if row["BREAKOUT_UP"]: long += 1.0
        if row["BREAKOUT_DOWN"]: short += 1.0
        if row["REL_VOLUME"] >= 1.15:
            if long > short: long += 0.75
            elif short > long: short += 0.75
        return _score_to_signal(long, short, 4.75, 2.8, regime, "trend/breakout momentum with liquidity confirmation")

    regime = "RANGE"
    z = _safe_float(row["BB_Z"])
    if z <= -1.5 and row["RSI14"] <= 38.0: long += 2.0
    if z >= 1.5 and row["RSI14"] >= 62.0: short += 2.0
    if row["Close"] < row["SESSION_VWAP"] and long > 0: long += 0.75
    if row["Close"] > row["SESSION_VWAP"] and short > 0: short += 0.75
    if row["REL_VOLUME"] <= 1.35:
        if long > short: long += 0.50
        elif short > long: short += 0.50
    return _score_to_signal(long, short, 3.25, 2.35, regime, "range mean-reversion via Bollinger z-score + RSI")


def _metals_signal(row: pd.Series) -> StrategySignal:
    long = short = 0.0
    if row["EMA20"] > row["EMA50"]: long += 1.0
    else: short += 1.0
    if row["EMA50"] > row["EMA200"]: long += 1.25
    else: short += 1.25
    if row["RET8"] > 0: long += 1.0
    elif row["RET8"] < 0: short += 1.0
    if row["RET20"] > 0: long += 0.75
    elif row["RET20"] < 0: short += 0.75
    if row["ADX14"] >= 18.0:
        if long > short: long += 0.75
        else: short += 0.75
    if 45.0 <= row["RSI14"] <= 72.0 and long > short: long += 0.50
    if 28.0 <= row["RSI14"] <= 55.0 and short > long: short += 0.50
    regime = "MACRO_TREND" if row["ADX14"] >= 18.0 else "RANGE"
    return _score_to_signal(long, short, 5.25, 3.15, regime, "medium-horizon EMA trend + ADX + 8/20-bar momentum")


def _energy_signal(row: pd.Series) -> StrategySignal:
    long = short = 0.0
    if row["Close"] > row["SESSION_VWAP"]: long += 1.1
    else: short += 1.1
    if row["SESSION_RETURN"] > 0: long += 0.9
    elif row["SESSION_RETURN"] < 0: short += 0.9
    if row["EMA20"] > row["EMA50"]: long += 0.8
    else: short += 0.8
    if row["BREAKOUT_UP"]: long += 1.0
    if row["BREAKOUT_DOWN"]: short += 1.0
    if row["RET4"] > 0: long += 0.7
    elif row["RET4"] < 0: short += 0.7
    if row["REL_VOLUME"] >= 1.15:
        if long > short: long += 0.7
        elif short > long: short += 0.7
    regime = "BREAKOUT" if bool(row["BREAKOUT_UP"] or row["BREAKOUT_DOWN"]) else "SESSION_MOMENTUM"
    return _score_to_signal(long, short, 5.2, 3.1, regime, "VWAP/session momentum + EMA + breakout + relative volume")


def _share_signal(row: pd.Series) -> StrategySignal:
    long = short = 0.0
    gap = _safe_float(row["GAP_PCT"])
    relv = _safe_float(row["REL_VOLUME"], 1.0)
    # This is a PRICE/VOLUME event detector, not a fabricated earnings flag.
    event = abs(gap) >= 0.004 or relv >= 1.35
    if gap > 0: long += 0.75
    elif gap < 0: short += 0.75
    if row["Close"] > row["SESSION_VWAP"]: long += 1.0
    else: short += 1.0
    if row["EMA20"] > row["EMA50"]: long += 0.85
    else: short += 0.85
    if row["RET4"] > 0: long += 0.75
    elif row["RET4"] < 0: short += 0.75
    if row["BREAKOUT_UP"]: long += 0.85
    if row["BREAKOUT_DOWN"]: short += 0.85
    if relv >= 1.25:
        if long > short: long += 0.75
        elif short > long: short += 0.75
    if event:
        if long > short: long += 0.50
        elif short > long: short += 0.50
    regime = "PRICE_VOLUME_EVENT" if event else "NORMAL_SESSION"
    return _score_to_signal(long, short, 5.45, 3.2, regime, "gap/relative-volume event + VWAP + trend + breakout")



# ---------------------------------------------------------------------------
# V6.9.1 finite strategy-variant library
# ---------------------------------------------------------------------------
# IMPORTANT: this is deliberately a small, explicit search space. The optimiser
# never tunes against the final holdout. It chooses a variant using only the
# earlier selection window, then freezes that variant before measuring the final
# 30% holdout. This reduces the risk of manufacturing a high historical win rate
# through repeated trial-and-error on the same final sample.

@dataclass(frozen=True)
class StrategyVariant:
    strategy_id: str
    strategy_name: str
    signal_func: Callable[[pd.Series], StrategySignal]
    holding_bars: int


def _fx_breakout_signal(row: pd.Series) -> StrategySignal:
    long = short = 0.0
    adx = _safe_float(row["ADX14"])
    if adx < 21.0:
        return StrategySignal("WAIT", 0.0, "RANGE", 0.0, 0.0, 1.0, "FX breakout requires ADX >=21")
    if row["EMA20"] > row["EMA50"] > row["EMA200"]: long += 1.6
    if row["EMA20"] < row["EMA50"] < row["EMA200"]: short += 1.6
    if row["BREAKOUT_UP"]: long += 1.5
    if row["BREAKOUT_DOWN"]: short += 1.5
    if row["RET4"] > 0 and row["RET8"] > 0: long += 1.1
    if row["RET4"] < 0 and row["RET8"] < 0: short += 1.1
    if row["REL_VOLUME"] >= 1.05:
        if long > short: long += 0.6
        elif short > long: short += 0.6
    return _score_to_signal(long, short, 4.8, 3.0, "BREAKOUT", "FX EMA-stack + ADX + 20-bar breakout momentum")


def _fx_range_signal(row: pd.Series) -> StrategySignal:
    if _safe_float(row["ADX14"]) > 23.0:
        return StrategySignal("WAIT", 0.0, "TREND", 0.0, 0.0, 1.0, "FX range reversion disabled in strong trend")
    long = short = 0.0
    z = _safe_float(row["BB_Z"])
    dist = _safe_float(row["EMA20_ATR_DIST"])
    if z <= -1.35: long += 1.5
    if z >= 1.35: short += 1.5
    if row["RSI14"] <= 37.0: long += 1.0
    if row["RSI14"] >= 63.0: short += 1.0
    if dist <= -1.0: long += 0.8
    if dist >= 1.0: short += 0.8
    if row["Close"] < row["SESSION_VWAP"] and long > short: long += 0.5
    if row["Close"] > row["SESSION_VWAP"] and short > long: short += 0.5
    return _score_to_signal(long, short, 3.8, 2.7, "RANGE", "FX Bollinger/RSI/ATR mean reversion")


def _index_breakout_signal(row: pd.Series) -> StrategySignal:
    long = short = 0.0
    if row["BREAKOUT_UP"]: long += 1.7
    if row["BREAKOUT_DOWN"]: short += 1.7
    if row["SESSION_RETURN"] > 0: long += 0.9
    elif row["SESSION_RETURN"] < 0: short += 0.9
    if row["Close"] > row["SESSION_VWAP"]: long += 0.9
    else: short += 0.9
    if row["ADX14"] >= 20.0:
        if row["EMA20"] > row["EMA50"]: long += 0.8
        else: short += 0.8
    if row["REL_VOLUME"] >= 1.10:
        if long > short: long += 0.6
        elif short > long: short += 0.6
    return _score_to_signal(long, short, 4.9, 3.0, "BREAKOUT", "Index 20-bar breakout + VWAP/session confirmation")


def _index_reversion_signal(row: pd.Series) -> StrategySignal:
    if _safe_float(row["ADX14"]) > 24.0:
        return StrategySignal("WAIT", 0.0, "TREND", 0.0, 0.0, 1.0, "Index reversion disabled in strong trend")
    long = short = 0.0
    vwap_dist = _safe_float(row["VWAP_ATR_DIST"])
    z = _safe_float(row["BB_Z"])
    if vwap_dist <= -1.0: long += 1.3
    if vwap_dist >= 1.0: short += 1.3
    if z <= -1.4: long += 1.1
    if z >= 1.4: short += 1.1
    if row["RSI14"] <= 38.0: long += 0.9
    if row["RSI14"] >= 62.0: short += 0.9
    return _score_to_signal(long, short, 3.3, 2.45, "VWAP_REVERSION", "Index VWAP/ATR + Bollinger + RSI reversion")


def _crypto_trend_signal(row: pd.Series) -> StrategySignal:
    atr_pct = _safe_float(row["ATR_PCT"])
    if atr_pct >= _safe_float(row["ATR_Q95"], 1.0) and abs(_safe_float(row["RET1"])) >= max(0.02, 2.0 * _safe_float(row["VOL20"])):
        return StrategySignal("WAIT", 0.0, "PANIC", 0.0, 0.0, 1.0, "panic volatility: no trend entry")
    if _safe_float(row["ADX14"]) < 24.0:
        return StrategySignal("WAIT", 0.0, "RANGE", 0.0, 0.0, 1.0, "crypto trend requires ADX >=24")
    long = short = 0.0
    if row["EMA20"] > row["EMA50"] > row["EMA200"]: long += 1.5
    if row["EMA20"] < row["EMA50"] < row["EMA200"]: short += 1.5
    if row["RET4"] > 0 and row["RET8"] > 0: long += 1.1
    if row["RET4"] < 0 and row["RET8"] < 0: short += 1.1
    if row["BREAKOUT_UP"]: long += 1.0
    if row["BREAKOUT_DOWN"]: short += 1.0
    if row["REL_VOLUME"] >= 1.20:
        if long > short: long += 0.7
        elif short > long: short += 0.7
    return _score_to_signal(long, short, 4.3, 2.8, "TREND", "Crypto EMA-stack + momentum + breakout + volume")


def _crypto_range_signal(row: pd.Series) -> StrategySignal:
    if _safe_float(row["ADX14"]) >= 22.0:
        return StrategySignal("WAIT", 0.0, "TREND", 0.0, 0.0, 1.0, "crypto range reversion only below ADX22")
    long = short = 0.0
    z = _safe_float(row["BB_Z"])
    if z <= -1.35: long += 1.5
    if z >= 1.35: short += 1.5
    if row["RSI14"] <= 36.0: long += 1.0
    if row["RSI14"] >= 64.0: short += 1.0
    if _safe_float(row["VWAP_ATR_DIST"]) <= -1.0: long += 0.7
    if _safe_float(row["VWAP_ATR_DIST"]) >= 1.0: short += 0.7
    return _score_to_signal(long, short, 3.2, 2.35, "RANGE", "Crypto Bollinger/RSI/VWAP range reversion")


def _metals_breakout_signal(row: pd.Series) -> StrategySignal:
    long = short = 0.0
    if row["BREAKOUT_UP"]: long += 1.5
    if row["BREAKOUT_DOWN"]: short += 1.5
    if row["EMA20"] > row["EMA50"] > row["EMA200"]: long += 1.3
    if row["EMA20"] < row["EMA50"] < row["EMA200"]: short += 1.3
    if row["RET8"] > 0 and row["RET20"] > 0: long += 1.0
    if row["RET8"] < 0 and row["RET20"] < 0: short += 1.0
    if row["ADX14"] >= 22.0:
        if long > short: long += 0.7
        elif short > long: short += 0.7
    return _score_to_signal(long, short, 4.5, 2.9, "BREAKOUT", "Metals breakout + medium trend confirmation")


def _metals_reversion_signal(row: pd.Series) -> StrategySignal:
    if _safe_float(row["ADX14"]) > 21.0:
        return StrategySignal("WAIT", 0.0, "TREND", 0.0, 0.0, 1.0, "metals reversion disabled above ADX21")
    long = short = 0.0
    z = _safe_float(row["BB_Z"])
    if z <= -1.45: long += 1.5
    if z >= 1.45: short += 1.5
    if row["RSI14"] <= 38.0: long += 0.9
    if row["RSI14"] >= 62.0: short += 0.9
    if _safe_float(row["EMA20_ATR_DIST"]) <= -1.1: long += 0.7
    if _safe_float(row["EMA20_ATR_DIST"]) >= 1.1: short += 0.7
    return _score_to_signal(long, short, 3.1, 2.35, "RANGE", "Metals Bollinger/RSI/ATR reversion")


def _energy_trend_signal(row: pd.Series) -> StrategySignal:
    long = short = 0.0
    if row["EMA20"] > row["EMA50"] > row["EMA200"]: long += 1.4
    if row["EMA20"] < row["EMA50"] < row["EMA200"]: short += 1.4
    if row["RET4"] > 0 and row["RET8"] > 0: long += 1.0
    if row["RET4"] < 0 and row["RET8"] < 0: short += 1.0
    if row["SESSION_RETURN"] > 0: long += 0.7
    elif row["SESSION_RETURN"] < 0: short += 0.7
    if row["Close"] > row["SESSION_VWAP"]: long += 0.7
    else: short += 0.7
    if row["ADX14"] >= 22.0:
        if long > short: long += 0.7
        elif short > long: short += 0.7
    return _score_to_signal(long, short, 4.5, 2.9, "TREND", "Energy EMA-stack + session/VWAP trend continuation")


def _energy_reversion_signal(row: pd.Series) -> StrategySignal:
    if _safe_float(row["ADX14"]) > 22.0:
        return StrategySignal("WAIT", 0.0, "TREND", 0.0, 0.0, 1.0, "energy reversion disabled above ADX22")
    long = short = 0.0
    if _safe_float(row["VWAP_ATR_DIST"]) <= -1.15: long += 1.4
    if _safe_float(row["VWAP_ATR_DIST"]) >= 1.15: short += 1.4
    if _safe_float(row["BB_Z"]) <= -1.4: long += 1.0
    if _safe_float(row["BB_Z"]) >= 1.4: short += 1.0
    if row["RSI14"] <= 38.0: long += 0.8
    if row["RSI14"] >= 62.0: short += 0.8
    return _score_to_signal(long, short, 3.2, 2.4, "RANGE", "Energy VWAP/ATR + Bollinger reversion")


def _share_trend_signal(row: pd.Series) -> StrategySignal:
    long = short = 0.0
    if row["EMA20"] > row["EMA50"] > row["EMA200"]: long += 1.4
    if row["EMA20"] < row["EMA50"] < row["EMA200"]: short += 1.4
    if row["RET4"] > 0 and row["RET8"] > 0: long += 1.0
    if row["RET4"] < 0 and row["RET8"] < 0: short += 1.0
    if row["Close"] > row["SESSION_VWAP"]: long += 0.8
    else: short += 0.8
    if row["BREAKOUT_UP"]: long += 0.8
    if row["BREAKOUT_DOWN"]: short += 0.8
    if row["REL_VOLUME"] >= 1.15:
        if long > short: long += 0.6
        elif short > long: short += 0.6
    return _score_to_signal(long, short, 4.6, 2.95, "TREND_CONTINUATION", "Share trend continuation + VWAP + breakout + volume")


def _share_reversion_signal(row: pd.Series) -> StrategySignal:
    if _safe_float(row["ADX14"]) > 23.0:
        return StrategySignal("WAIT", 0.0, "TREND", 0.0, 0.0, 1.0, "share reversion disabled above ADX23")
    long = short = 0.0
    if _safe_float(row["VWAP_ATR_DIST"]) <= -1.0: long += 1.3
    if _safe_float(row["VWAP_ATR_DIST"]) >= 1.0: short += 1.3
    if _safe_float(row["BB_Z"]) <= -1.35: long += 1.1
    if _safe_float(row["BB_Z"]) >= 1.35: short += 1.1
    if row["RSI14"] <= 37.0: long += 0.9
    if row["RSI14"] >= 63.0: short += 0.9
    return _score_to_signal(long, short, 3.3, 2.45, "VWAP_REVERSION", "Share VWAP/Bollinger/RSI reversion")


STRATEGY_VARIANTS: Dict[str, List[StrategyVariant]] = {
    "FOREX": [
        StrategyVariant("FX_REGIME_TREND_PULLBACK_V1", "FX Regime + Trend Pullback", _fx_signal, 4),
        StrategyVariant("FX_BREAKOUT_MOMENTUM_V2", "FX Breakout Momentum", _fx_breakout_signal, 4),
        StrategyVariant("FX_RANGE_REVERSION_V2", "FX Range Reversion", _fx_range_signal, 3),
    ],
    "INDICES": [
        StrategyVariant("INDEX_SESSION_MOMENTUM_V1", "Index Session Momentum", _index_signal, 2),
        StrategyVariant("INDEX_BREAKOUT_V2", "Index Breakout Momentum", _index_breakout_signal, 2),
        StrategyVariant("INDEX_VWAP_REVERSION_V2", "Index VWAP Reversion", _index_reversion_signal, 2),
    ],
    "CRYPTO": [
        StrategyVariant("CRYPTO_REGIME_ROUTER_V1", "Crypto Regime Momentum / Range Router", _crypto_signal, 4),
        StrategyVariant("CRYPTO_TREND_V2", "Crypto Trend Momentum", _crypto_trend_signal, 4),
        StrategyVariant("CRYPTO_RANGE_V2", "Crypto Range Reversion", _crypto_range_signal, 3),
    ],
    "METALS": [
        StrategyVariant("METALS_TREND_VOLATILITY_V1", "Metals Trend + Volatility", _metals_signal, 8),
        StrategyVariant("METALS_BREAKOUT_V2", "Metals Breakout Momentum", _metals_breakout_signal, 6),
        StrategyVariant("METALS_REVERSION_V2", "Metals Range Reversion", _metals_reversion_signal, 4),
    ],
    "ENERGY": [
        StrategyVariant("ENERGY_SESSION_BREAKOUT_V1", "Energy Session Momentum + Breakout", _energy_signal, 3),
        StrategyVariant("ENERGY_TREND_V2", "Energy Trend Continuation", _energy_trend_signal, 4),
        StrategyVariant("ENERGY_REVERSION_V2", "Energy Range Reversion", _energy_reversion_signal, 3),
    ],
    "SHARES": [
        StrategyVariant("SHARE_EVENT_MOMENTUM_V1", "Share Price/Volume Event Momentum", _share_signal, 4),
        StrategyVariant("SHARE_TREND_CONTINUATION_V2", "Share Trend Continuation", _share_trend_signal, 4),
        StrategyVariant("SHARE_VWAP_REVERSION_V2", "Share VWAP Reversion", _share_reversion_signal, 3),
    ],
}

# Baseline mapping retained for compatibility with external imports/tests.
_SIGNAL_FUNC: Dict[str, Callable[[pd.Series], StrategySignal]] = {
    category: variants[0].signal_func
    for category, variants in STRATEGY_VARIANTS.items()
}


def _historical_grade(trades: int, win_rate: float, profit_factor: float, max_dd: float) -> tuple[str, str, bool]:
    # V6.9.3 hotfix: keep the existing 30-trade evidence floor explicit.
    # The user-selected execution targets remain 60% WR and PF >= 1.20.
    validated_target = (
        trades >= HISTORICAL_MIN_TRADES
        and win_rate >= HISTORICAL_WIN_RATE_TARGET
        and profit_factor >= HISTORICAL_PROFIT_FACTOR_TARGET
        and max_dd <= 0.12
    )
    if trades >= 40 and win_rate >= 0.70 and profit_factor >= 1.50 and max_dd <= 0.10:
        return "A+", "CATEGORY_ELITE_70", True
    if validated_target:
        return "A", "CATEGORY_VERIFIED_60", True
    if trades >= 20 and win_rate >= 0.55 and profit_factor >= 1.05 and max_dd <= 0.16:
        return "B", "CATEGORY_PROBATION", False
    return "C", "CATEGORY_REJECT", False


def _fast_score(trades: int, win_rate: float, profit_factor: float, max_dd: float) -> float:
    wr = max(0.0, min(1.0, (win_rate - 0.50) / 0.25))
    pf = max(0.0, min(1.0, (profit_factor - 1.0) / 1.5))
    sample = max(0.0, min(1.0, trades / 50.0))
    dd = max(0.0, min(1.0, (0.18 - max_dd) / 0.18))
    return max(0.0, min(100.0, 30.0 + 30.0 * wr + 20.0 * pf + 10.0 * sample + 10.0 * dd))


class CategoryStrategyEngine:
    """Runs six independent market-category strategies and ranks 1-5 per category."""

    VERSION = VERSION

    def __init__(
        self,
        *,
        broker: Any,
        frame_func: Callable[[Dict[str, Any]], pd.DataFrame],
        state_path: str,
        scan_interval_seconds: Optional[int] = None,
        batch_size: Optional[int] = None,
    ) -> None:
        self.broker = broker
        self.frame_func = frame_func
        self.state_path = state_path
        self.scan_interval_seconds = max(90, int(scan_interval_seconds or os.getenv("CATEGORY_SCAN_INTERVAL_SECONDS", "180")))
        # Compatibility with the existing V6.8.x /global-markets/opportunity-board
        # endpoint. Specialist rankings are refreshed from already-evaluated
        # evidence without triggering another heavy market-data scan.
        self.eligibility_refresh_seconds = max(
            15,
            int(os.getenv("CATEGORY_ELIGIBILITY_REFRESH_SECONDS", "15")),
        )
        self.batch_size = max(3, min(18, int(batch_size or os.getenv("CATEGORY_SCAN_BATCH_SIZE", "6"))))
        self.candidate_ttl_seconds = max(300, int(os.getenv("CATEGORY_CANDIDATE_TTL_SECONDS", "1800")))
        self.auto_full_refresh = str(os.getenv("CATEGORY_AUTO_FULL_REFRESH", "true")).lower().strip() in {"1", "true", "yes", "on"}
        self._lock = threading.RLock()
        self._refresh_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._full_refresh_thread: Optional[threading.Thread] = None
        self._state = self._load_state()

    def _default_state(self) -> Dict[str, Any]:
        return {
            "version": self.VERSION,
            "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
            "enabled": True,
            "offset_by_category": {category: 0 for category in CATEGORY_ORDER},
            "runs": 0,
            "last_run_at": None,
            "last_error": None,
            "evaluations": {},
            "last_batch_keys": [],
            "legacy_rows_excluded": 0,
            "migration_source_version": None,
            "migration_at": None,
            "full_refresh": {
                "status": "PENDING",
                "started_at": None,
                "completed_at": None,
                "processed": 0,
                "total": len(CATEGORY_MARKET_SEEDS),
                "current_key": None,
                "errors": 0,
                "last_error": None,
            },
        }

    def _is_current_evaluation(self, row: Any) -> bool:
        if not isinstance(row, dict):
            return False
        return bool(
            str(row.get("version") or "") == self.VERSION
            and int(row.get("evidence_schema_version") or 0) == EVIDENCE_SCHEMA_VERSION
            and row.get("optimizer_complete") is True
            and row.get("walk_forward_complete") is True
        )

    def _load_state(self) -> Dict[str, Any]:
        state = self._default_state()
        try:
            if os.path.exists(self.state_path):
                with open(self.state_path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                if isinstance(raw, dict):
                    source_version = str(raw.get("version") or "UNKNOWN")
                    state["migration_source_version"] = source_version
                    # Preserve harmless scheduling/counter metadata, but never let
                    # an older category-analysis row compete with V6.9.3 evidence.
                    for key in ("enabled", "runs", "last_run_at"):
                        if key in raw:
                            state[key] = raw[key]
                    raw_evaluations = raw.get("evaluations") or {}
                    current: Dict[str, Any] = {}
                    excluded = 0
                    if isinstance(raw_evaluations, dict):
                        for key, row in raw_evaluations.items():
                            if self._is_current_evaluation(row):
                                current[str(key)] = row
                            else:
                                excluded += 1
                    state["evaluations"] = current
                    state["legacy_rows_excluded"] = int(raw.get("legacy_rows_excluded") or 0) + excluded
                    state["migration_at"] = time.time() if excluded or source_version != self.VERSION else raw.get("migration_at")
                    # A version/schema change requires one clean 40-market bootstrap.
                    if excluded or source_version != self.VERSION or len(current) < len(CATEGORY_MARKET_SEEDS):
                        state["offset_by_category"] = {category: 0 for category in CATEGORY_ORDER}
                        state["full_refresh"] = {
                            "status": "PENDING",
                            "started_at": None,
                            "completed_at": None,
                            "processed": len(current),
                            "total": len(CATEGORY_MARKET_SEEDS),
                            "current_key": None,
                            "errors": 0,
                            "last_error": None,
                        }
                    else:
                        state["full_refresh"] = dict(raw.get("full_refresh") or state["full_refresh"])
        except Exception as exc:
            state["last_error"] = f"state_load: {type(exc).__name__}: {exc}"
        state["version"] = self.VERSION
        state["evidence_schema_version"] = EVIDENCE_SCHEMA_VERSION
        state.setdefault("offset_by_category", {category: 0 for category in CATEGORY_ORDER})
        return state

    def _persist(self) -> None:
        try:
            path = Path(self.state_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = str(path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._state, fh, separators=(",", ":"), default=str)
            os.replace(tmp, self.state_path)
        except Exception as exc:
            self._state["last_error"] = f"persist: {type(exc).__name__}: {exc}"

    def _current_evaluations(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            rows = self._state.get("evaluations") or {}
            return {
                str(key): dict(row)
                for key, row in rows.items()
                if self._is_current_evaluation(row)
            }

    def evidence_coverage(self) -> Dict[str, Any]:
        current = self._current_evaluations()
        universe_keys = [str(seed["key"]) for seed in CATEGORY_MARKET_SEEDS]
        completed = [key for key in universe_keys if key in current]
        pending = [key for key in universe_keys if key not in current]
        return {
            "version": self.VERSION,
            "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
            "markets_total": len(universe_keys),
            "markets_optimised": len(completed),
            "markets_pending_optimisation": len(pending),
            "optimiser_complete": len(pending) == 0,
            "completed_keys": completed,
            "pending_keys": pending,
            "legacy_rows_excluded": int(self._state.get("legacy_rows_excluded") or 0),
            "live_money_execution": False,
        }

    def universe(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = CATEGORY_MARKET_SEEDS
        if category:
            clean = str(category).upper().strip()
            rows = [row for row in rows if row["category"] == clean]
        return [dict(row) for row in rows]

    def _next_batch(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            categories = [str(category).upper().strip()] if category else list(CATEGORY_ORDER)
            categories = [c for c in categories if c in CATEGORY_ORDER]
            if not categories:
                return []
            each = max(1, self.batch_size // len(categories)) if not category else self.batch_size
            batch: List[Dict[str, Any]] = []
            offsets = self._state.setdefault("offset_by_category", {})
            for cat in categories:
                pool = [row for row in CATEGORY_MARKET_SEEDS if row["category"] == cat]
                if not pool:
                    continue
                offset = int(offsets.get(cat) or 0) % len(pool)
                take = min(each, len(pool))
                for i in range(take):
                    batch.append(dict(pool[(offset + i) % len(pool)]))
                offsets[cat] = (offset + take) % len(pool)
            self._state["last_batch_keys"] = [row["key"] for row in batch]
            return batch

    def _resolve_execution_market(self, seed: Dict[str, Any]) -> Dict[str, Any]:
        if seed.get("category") == "FOREX" and seed.get("ig_symbol"):
            market = self.broker.resolve_market(str(seed["ig_symbol"]), require_tradeable=False)
            details = market.get("details") or {}
            snapshot = details.get("snapshot") or {}
            instrument = details.get("instrument") or {}
            return {
                "epic": market.get("epic"),
                "name": market.get("name") or instrument.get("name"),
                "instrument_type": market.get("instrument_type") or instrument.get("type"),
                "market_status": market.get("market_status") or snapshot.get("marketStatus"),
                "min_deal_size": getattr(self.broker, "_min_deal_size", lambda _: 0.0)(details),
                "expiry": market.get("expiry") or instrument.get("expiry"),
                "bid": snapshot.get("bid"),
                "offer": snapshot.get("offer"),
            }
        return self.broker.resolve_global_market(
            search_terms=list(seed.get("ig_search_terms") or [seed.get("name")]),
            expected_types=list(seed.get("expected_types") or []),
            name_tokens=list(seed.get("name_tokens") or []),
            require_tradeable=False,
            cache_key=str(seed.get("key") or seed.get("name") or ""),
        )

    def _resolve_bid_offer(
        self,
        seed: Dict[str, Any],
        market: Dict[str, Any],
    ) -> tuple[Optional[float], Optional[float], str]:
        """Resolve a usable IG DEMO bid/offer without inventing a spread.

        IG can return a TRADEABLE market while a particular resolver payload has
        no top-level quote.  We first use the resolver/details snapshot, then
        refresh the exact epic, and finally query IG market search and match the
        same epic.  If none supplies both sides, the spread remains unavailable
        and execution still fails closed.
        """
        def _pair(raw_bid: Any, raw_offer: Any, source: str):
            bid = _safe_float(raw_bid)
            offer = _safe_float(raw_offer)
            if bid > 0.0 and offer >= bid:
                return bid, offer, source
            return None

        details = market.get("details") or {}
        snapshot = details.get("snapshot") or {}
        for candidate in (
            _pair(market.get("bid"), market.get("offer"), "RESOLVER_TOP_LEVEL"),
            _pair(snapshot.get("bid"), snapshot.get("offer"), "RESOLVER_DETAILS_SNAPSHOT"),
        ):
            if candidate:
                return candidate

        epic = str(market.get("epic") or "").strip()
        if epic and hasattr(self.broker, "market_details"):
            try:
                refreshed = self.broker.market_details(epic) or {}
                snap = refreshed.get("snapshot") or {}
                candidate = _pair(snap.get("bid"), snap.get("offer"), "REFRESHED_EPIC_SNAPSHOT")
                if candidate:
                    return candidate
            except Exception:
                pass

        # Some IG market-search rows contain bid/offer even when the v4 details
        # snapshot omitted them. Match only the already-resolved epic so fuzzy
        # search can never substitute another instrument.
        if epic and hasattr(self.broker, "search_markets"):
            terms = list(seed.get("ig_search_terms") or [seed.get("name")])
            for term in terms[:2]:
                if not str(term or "").strip():
                    continue
                try:
                    response = self.broker.search_markets(str(term)) or {}
                    for raw in response.get("markets", []) or []:
                        if not isinstance(raw, dict):
                            continue
                        if str(raw.get("epic") or "").strip() != epic:
                            continue
                        candidate = _pair(raw.get("bid"), raw.get("offer"), "EXACT_EPIC_MARKET_SEARCH")
                        if candidate:
                            return candidate
                except Exception:
                    continue

        return None, None, "UNAVAILABLE"

    @staticmethod
    def _directional_ai(row: pd.Series, direction: str) -> float:
        up = _confidence01(row.get("JASONG_UP_PROB"))
        if direction == "BUY":
            return up
        if direction == "SELL":
            return 1.0 - up
        return 0.0

    @staticmethod
    def _metrics_from_returns(values: List[float]) -> Dict[str, Any]:
        trades = len(values)
        wins = sum(1 for value in values if value > 0.0)
        losses = trades - wins
        win_rate = wins / trades if trades else 0.0
        return {
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "profit_factor": _profit_factor(values),
            "max_drawdown": _max_drawdown(values),
            "mean_return": (sum(values) / trades) if trades else 0.0,
        }

    def _simulate_variant(
        self,
        frame: pd.DataFrame,
        variant: StrategyVariant,
        *,
        start: int,
        end: int,
        cost: float,
    ) -> Dict[str, Any]:
        """Evaluate one frozen variant over one chronological window.

        Entries are non-overlapping: after an accepted trade the simulator moves
        forward by the strategy holding horizon. This avoids inflating the sample
        with several simultaneous copies of the same market position.
        """
        returns: List[float] = []
        i = max(0, int(start))
        stop = min(int(end), len(frame) - int(variant.holding_bars))
        while i < stop:
            row = frame.iloc[i]
            sig = variant.signal_func(row)
            if sig.direction not in {"BUY", "SELL"} or sig.quant_confidence < QUANT_MIN_CONFIDENCE:
                i += 1
                continue
            ai = self._directional_ai(row, sig.direction)
            if ai < MODEL_AI_MIN_CONFIDENCE:
                i += 1
                continue
            entry = _safe_float(row["Close"])
            exit_price = _safe_float(frame.iloc[i + variant.holding_bars]["Close"])
            if entry <= 0 or exit_price <= 0:
                i += 1
                continue
            gross = exit_price / entry - 1.0
            if sig.direction == "SELL":
                gross = -gross
            returns.append(gross - cost)
            i += max(1, int(variant.holding_bars))
        metrics = self._metrics_from_returns(returns)
        metrics["returns"] = returns
        return metrics

    @staticmethod
    def _window_score(metrics: Dict[str, Any]) -> float:
        trades = int(metrics.get("trades") or 0)
        if trades < 6:
            return -1000.0 + trades
        wr = _safe_float(metrics.get("win_rate"))
        pf = _safe_float(metrics.get("profit_factor"))
        dd = _safe_float(metrics.get("max_drawdown"))
        mean = _safe_float(metrics.get("mean_return"))
        sample = min(1.0, trades / 30.0)
        # Bounded components; no final-holdout information enters this score.
        score = (
            45.0 * wr
            + 20.0 * min(1.0, max(0.0, pf / 2.0))
            + 15.0 * max(0.0, 1.0 - min(dd, 0.25) / 0.25)
            + 10.0 * sample
            + 10.0 * min(1.0, max(0.0, mean * 500.0))
        )
        if pf < 1.0:
            score -= (1.0 - pf) * 20.0
        if wr < 0.50:
            score -= (0.50 - wr) * 30.0
        return round(score, 4)

    def _select_variant(self, frame: pd.DataFrame, category: str, cost: float) -> Dict[str, Any]:
        n = len(frame)
        # Features need a long warm-up (EMA200). Variant selection uses 40-70%;
        # the final 30% is untouched until one variant has been frozen.
        selection_start = max(200, int(n * 0.40))
        selection_end = max(selection_start + 20, int(n * 0.70))
        selection_end = min(selection_end, n - 1)
        mid = selection_start + max(1, (selection_end - selection_start) // 2)

        leaderboard: List[Dict[str, Any]] = []
        for variant in STRATEGY_VARIANTS[category]:
            full = self._simulate_variant(frame, variant, start=selection_start, end=selection_end, cost=cost)
            first = self._simulate_variant(frame, variant, start=selection_start, end=mid, cost=cost)
            second = self._simulate_variant(frame, variant, start=mid, end=selection_end, cost=cost)
            score_full = self._window_score(full)
            score_first = self._window_score(first)
            score_second = self._window_score(second)
            stable = bool(
                int(full["trades"]) >= 16
                and int(first["trades"]) >= 5
                and int(second["trades"]) >= 5
                and _safe_float(full["win_rate"]) >= 0.50
                and _safe_float(full["profit_factor"]) >= 1.0
                and _safe_float(full["max_drawdown"]) <= 0.20
                and min(_safe_float(first["win_rate"]), _safe_float(second["win_rate"])) >= 0.40
            )
            # Reward performance that survives both chronological halves.
            stability_floor = min(score_first, score_second)
            optimiser_score = score_full + 0.35 * stability_floor + (8.0 if stable else 0.0)
            leaderboard.append({
                "variant": variant,
                "optimizer_score": round(optimiser_score, 4),
                "selection_stable": stable,
                "selection": full,
                "selection_first_half": first,
                "selection_second_half": second,
            })

        leaderboard.sort(
            key=lambda item: (
                bool(item["selection_stable"]),
                _safe_float(item["optimizer_score"]),
                _safe_float(item["selection"].get("profit_factor")),
                _safe_float(item["selection"].get("win_rate")),
            ),
            reverse=True,
        )
        selected = leaderboard[0]
        selected["leaderboard"] = leaderboard
        selected["holdout_start"] = selection_end
        return selected

    def _walk_forward_folds(
        self,
        frame: pd.DataFrame,
        variant: StrategyVariant,
        *,
        holdout_start: int,
        cost: float,
    ) -> List[Dict[str, Any]]:
        start = max(0, int(holdout_start))
        end = len(frame)
        span = max(0, end - start)
        if span <= WALK_FORWARD_FOLDS:
            return []
        boundaries = [start]
        for fold in range(1, WALK_FORWARD_FOLDS):
            boundaries.append(start + int(span * fold / WALK_FORWARD_FOLDS))
        boundaries.append(end)
        folds: List[Dict[str, Any]] = []
        for i in range(WALK_FORWARD_FOLDS):
            left, right = boundaries[i], boundaries[i + 1]
            metrics = self._simulate_variant(frame, variant, start=left, end=right, cost=cost)
            metrics.pop("returns", None)
            folds.append({
                "fold": i + 1,
                "start_index": left,
                "end_index": right,
                "trades": int(metrics.get("trades") or 0),
                "wins": int(metrics.get("wins") or 0),
                "losses": int(metrics.get("losses") or 0),
                "win_rate": _safe_float(metrics.get("win_rate")),
                "profit_factor": _safe_float(metrics.get("profit_factor")),
                "max_drawdown": _safe_float(metrics.get("max_drawdown")),
                "mean_return": _safe_float(metrics.get("mean_return")),
            })
        return folds

    @staticmethod
    def _walk_forward_gate(folds: List[Dict[str, Any]], aggregate: Dict[str, Any]) -> Dict[str, Any]:
        qualifying = [fold for fold in folds if int(fold.get("trades") or 0) >= WALK_FORWARD_MIN_FOLD_TRADES]
        win_rates = sorted(_safe_float(fold.get("win_rate")) for fold in qualifying)
        median_wr = 0.0
        if win_rates:
            m = len(win_rates) // 2
            median_wr = win_rates[m] if len(win_rates) % 2 else (win_rates[m - 1] + win_rates[m]) / 2.0
        min_wr = min(win_rates) if win_rates else 0.0
        profitable = sum(1 for fold in qualifying if _safe_float(fold.get("profit_factor")) >= 1.0)
        fold_dd_ok = all(_safe_float(fold.get("max_drawdown")) <= 0.15 for fold in qualifying) if qualifying else False
        checks = {
            "enough_qualifying_folds": len(qualifying) >= WALK_FORWARD_MIN_FOLDS,
            "holdout_sample_ok": int(aggregate.get("trades") or 0) >= HISTORICAL_MIN_TRADES,
            "holdout_wr_ok": _safe_float(aggregate.get("win_rate")) >= HISTORICAL_WIN_RATE_TARGET,
            "holdout_pf_ok": _safe_float(aggregate.get("profit_factor")) >= HISTORICAL_PROFIT_FACTOR_TARGET,
            "holdout_dd_ok": _safe_float(aggregate.get("max_drawdown")) <= 0.12,
            "min_fold_wr_ok": min_wr >= WALK_FORWARD_MIN_FOLD_WIN_RATE,
            "median_fold_wr_ok": median_wr >= WALK_FORWARD_MIN_MEDIAN_WIN_RATE,
            "profitable_folds_ok": profitable >= WALK_FORWARD_MIN_PROFITABLE_FOLDS,
            "fold_drawdown_ok": fold_dd_ok,
        }
        passed = bool(all(checks.values()))
        rejection_reasons: List[str] = []
        if not checks["enough_qualifying_folds"]:
            rejection_reasons.append("WF_QUALIFYING_FOLDS_BELOW_MIN")
        if not checks["holdout_sample_ok"]:
            rejection_reasons.append("WF_HOLDOUT_SAMPLE_BELOW_MIN")
        if not checks["holdout_wr_ok"]:
            rejection_reasons.append("WF_HOLDOUT_WR_BELOW_60")
        if not checks["holdout_pf_ok"]:
            rejection_reasons.append("WF_HOLDOUT_PF_BELOW_1_20")
        if not checks["holdout_dd_ok"]:
            rejection_reasons.append("WF_HOLDOUT_DRAWDOWN_FAIL")
        if not checks["min_fold_wr_ok"]:
            rejection_reasons.append("WF_MIN_FOLD_WR_BELOW_60")
        if not checks["median_fold_wr_ok"]:
            rejection_reasons.append("WF_MEDIAN_WR_BELOW_60")
        if not checks["profitable_folds_ok"]:
            rejection_reasons.append("WF_PROFITABLE_FOLDS_BELOW_2")
        if not checks["fold_drawdown_ok"]:
            rejection_reasons.append("WF_FOLD_DRAWDOWN_FAIL")
        return {
            "passed": passed,
            "qualifying_folds": len(qualifying),
            "min_fold_win_rate": min_wr,
            "median_fold_win_rate": median_wr,
            "profitable_folds": profitable,
            "fold_drawdown_ok": fold_dd_ok,
            "checks": checks,
            "rejection_reasons": rejection_reasons,
        }

    def _evaluate_seed(self, seed: Dict[str, Any]) -> Dict[str, Any]:
        category = str(seed.get("category") or "").upper()
        if category not in STRATEGY_VARIANTS:
            raise ValueError(f"Unsupported category: {category}")
        raw = self.frame_func(seed)
        frame = _feature_frame(raw)
        rule = CATEGORY_RULES[category]
        cost = float(rule["cost_bps"]) / 10000.0

        optimiser = self._select_variant(frame, category, cost)
        variant: StrategyVariant = optimiser["variant"]
        holding = int(variant.holding_bars)
        signal_func = variant.signal_func
        selection = optimiser["selection"]
        selection_stable = bool(optimiser["selection_stable"])
        cut = int(optimiser["holdout_start"])

        # FINAL untouched 30% holdout. The chosen strategy variant is frozen
        # before this function sees these outcomes.
        holdout = self._simulate_variant(
            frame,
            variant,
            start=cut,
            end=len(frame),
            cost=cost,
        )
        strategy_returns = list(holdout.pop("returns", []))
        trades = int(holdout["trades"])
        wins = int(holdout["wins"])
        losses = int(holdout["losses"])
        win_rate = _safe_float(holdout["win_rate"])
        profit_factor = _safe_float(holdout["profit_factor"])
        max_dd = _safe_float(holdout["max_drawdown"])

        raw_quality, _, raw_verified_target = _historical_grade(trades, win_rate, profit_factor, max_dd)
        walk_forward_folds = self._walk_forward_folds(
            frame,
            variant,
            holdout_start=cut,
            cost=cost,
        )
        walk_forward = self._walk_forward_gate(walk_forward_folds, holdout)
        # V6.9.3 promotes the user-selected 60% evidence policy only when variant selection is stable AND
        # the untouched final holdout survives multiple chronological folds.
        validated_target = bool(raw_verified_target and selection_stable and walk_forward["passed"])
        if validated_target:
            quality = raw_quality
            category_validation_status = "CATEGORY_VERIFIED_60_WALK_FORWARD"
        elif (
            selection_stable
            and trades >= 20
            and win_rate >= 0.55
            and profit_factor >= 1.05
            and max_dd <= 0.16
        ):
            quality = "B"
            category_validation_status = "CATEGORY_PROBATION"
        else:
            quality = "C"
            category_validation_status = "CATEGORY_REJECT"

        deep_status = "VERIFIED" if validated_target else ("WATCH" if quality == "B" else "REJECT")
        fast_score = _fast_score(trades, win_rate, profit_factor, max_dd)

        latest = frame.dropna(subset=["Close"]).iloc[-1]
        live = signal_func(latest)
        model_ai = self._directional_ai(latest, live.direction)
        quant_pass = live.quant_confidence >= QUANT_MIN_CONFIDENCE
        ai_pass = model_ai >= MODEL_AI_MIN_CONFIDENCE

        leaderboard_public = []
        for item in optimiser.get("leaderboard") or []:
            v: StrategyVariant = item["variant"]
            m = item["selection"]
            leaderboard_public.append({
                "strategy_id": v.strategy_id,
                "strategy_name": v.strategy_name,
                "holding_bars": v.holding_bars,
                "optimizer_score": round(_safe_float(item.get("optimizer_score")), 3),
                "selection_stable": bool(item.get("selection_stable")),
                "selection_trades": int(m.get("trades") or 0),
                "selection_win_rate_pct": round(_safe_float(m.get("win_rate")) * 100.0, 2),
                "selection_profit_factor": round(_safe_float(m.get("profit_factor")), 4),
                "selection_max_drawdown_pct": round(_safe_float(m.get("max_drawdown")) * 100.0, 2),
            })

        row: Dict[str, Any] = {
            **seed,
            "version": self.VERSION,
            "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
            "optimizer_complete": True,
            "walk_forward_complete": True,
            "market": seed.get("name"),
            "symbol": seed.get("key"),
            "strategy_id": variant.strategy_id,
            "strategy_name": variant.strategy_name,
            "optimizer_enabled": True,
            "optimizer_method": "FINITE_VARIANT_SELECTION_40_70_THEN_3_FOLD_UNTOUCHED_70_100",
            "strategy_variants_tested": len(STRATEGY_VARIANTS[category]),
            "optimizer_selection_stable": selection_stable,
            "optimizer_score": round(_safe_float(optimiser.get("optimizer_score")), 3),
            "optimizer_leaderboard": leaderboard_public,
            "selection_trades": int(selection.get("trades") or 0),
            "selection_win_rate_pct": round(_safe_float(selection.get("win_rate")) * 100.0, 2),
            "selection_profit_factor": round(_safe_float(selection.get("profit_factor")), 4),
            "selection_max_drawdown_pct": round(_safe_float(selection.get("max_drawdown")) * 100.0, 2),
            "regime": live.regime,
            "direction": live.direction,
            "live_direction": live.direction,
            "quant_confidence": round(live.quant_confidence, 6),
            "quant_confidence_pct": round(live.quant_confidence * 100.0, 2),
            "model_ai_confidence": round(model_ai, 6),
            "model_ai_directional_confidence_pct": round(model_ai * 100.0, 2),
            "ai28_pass": quant_pass,
            "ai40_pass": ai_pass,
            "historical_win_rate": round(win_rate, 6),
            "historical_win_rate_pct": round(win_rate * 100.0, 2),
            "historical_profit_factor": round(profit_factor, 4),
            "historical_trades": trades,
            "historical_min_trades": HISTORICAL_MIN_TRADES,
            "historical_sample_pass": trades >= HISTORICAL_MIN_TRADES,
            "historical_wins": wins,
            "historical_losses": losses,
            "historical_max_drawdown_pct": round(max_dd * 100.0, 2),
            "historical_target_verified": validated_target,
            "historical_60_verified": validated_target,
            "historical_70_verified": bool(trades >= 30 and win_rate >= 0.70 and profit_factor >= 1.30 and max_dd <= 0.12 and selection_stable and walk_forward["passed"]),
            "raw_holdout_target_pass": bool(raw_verified_target),
            "raw_holdout_70_pass": bool(trades >= 30 and win_rate >= 0.70 and profit_factor >= 1.30 and max_dd <= 0.12),
            "walk_forward_pass": bool(walk_forward["passed"]),
            "walk_forward_fold_count": len(walk_forward_folds),
            "walk_forward_qualifying_folds": int(walk_forward["qualifying_folds"]),
            "walk_forward_min_win_rate_pct": round(_safe_float(walk_forward["min_fold_win_rate"]) * 100.0, 2),
            "walk_forward_median_win_rate_pct": round(_safe_float(walk_forward["median_fold_win_rate"]) * 100.0, 2),
            "walk_forward_profitable_folds": int(walk_forward["profitable_folds"]),
            "walk_forward_fold_drawdown_ok": bool(walk_forward["fold_drawdown_ok"]),
            "walk_forward_rejection_reasons": list(walk_forward.get("rejection_reasons") or []),
            "walk_forward_policy": {
                "all_qualifying_folds_must_meet_min_wr": True,
                "minimum_qualifying_fold_wr_pct": WALK_FORWARD_MIN_FOLD_WIN_RATE * 100.0,
                "median_fold_wr_pct": WALK_FORWARD_MIN_MEDIAN_WIN_RATE * 100.0,
                "minimum_profitable_folds": WALK_FORWARD_MIN_PROFITABLE_FOLDS,
                "minimum_trades_per_fold": WALK_FORWARD_MIN_FOLD_TRADES,
                "minimum_qualifying_folds": WALK_FORWARD_MIN_FOLDS,
                "holdout_min_trades": HISTORICAL_MIN_TRADES,
            },
            "walk_forward_folds": [
                {
                    **fold,
                    "win_rate_pct": round(_safe_float(fold.get("win_rate")) * 100.0, 2),
                    "profit_factor": round(_safe_float(fold.get("profit_factor")), 4),
                    "max_drawdown_pct": round(_safe_float(fold.get("max_drawdown")) * 100.0, 2),
                }
                for fold in walk_forward_folds
            ],
            "quality_tier": quality,
            "historical_grade": quality,
            "deep_status": deep_status,
            "category_validation_status": category_validation_status,
            "verified": validated_target,
            "experimental": not validated_target,
            "strategy_quarantined": False,
            "smart_fast_score": round(fast_score, 2),
            "strategy_reason": live.reason,
            "signal_reason": live.reason,
            "live_price": _safe_float(latest.get("Close")),
            "rsi": round(_safe_float(latest.get("RSI14"), 50.0), 2),
            "adx": round(_safe_float(latest.get("ADX14")), 2),
            "holding_bars": holding,
            "validation_target_pct": HISTORICAL_WIN_RATE_TARGET * 100.0,
            "holdout_fraction_pct": 30.0,
            "analysis_source": "CATEGORY_SPECIALIST_V693_60WF_PLUS_JASONG_MODEL_3_FOLD_HOLDOUT",
            "recent_returns": [
                round(_safe_float(value), 10)
                for value in frame["RET1"].dropna().tail(160).tolist()
            ],
            "evaluated_at": time.time(),
            "ig_tradeable": False,
            "ig_epic": None,
            "ig_market_status": None,
            "ig_spread_bps": None,
            "ig_bid": None,
            "ig_offer": None,
            "ig_quote_source": "NOT_REQUESTED",
            "standard_eligible": False,
            "compound_slot_candidate": False,
            "compound_eligible": False,
            "live_money_execution": False,
        }

        # V6.9.3 testing preflight begins at the same Fast 60 execution floor.
        promising = live.direction in {"BUY", "SELL"} and quant_pass and ai_pass and fast_score >= STANDARD_FAST_SCORE_MIN
        if promising:
            try:
                market = self._resolve_execution_market(seed)
                row["ig_epic"] = market.get("epic")
                row["ig_market_name"] = market.get("name")
                row["ig_instrument_type"] = market.get("instrument_type")
                row["ig_market_status"] = market.get("market_status")
                row["ig_tradeable"] = str(market.get("market_status") or "").upper() == "TRADEABLE"
                row["ig_min_deal_size"] = market.get("min_deal_size")
                row["ig_expiry"] = market.get("expiry")
                bid, offer, quote_source = self._resolve_bid_offer(seed, market)
                row["ig_bid"] = bid
                row["ig_offer"] = offer
                row["ig_quote_source"] = quote_source
                if bid is not None and offer is not None:
                    mid_price = (bid + offer) / 2.0
                    row["ig_spread_bps"] = round((offer - bid) / mid_price * 10000.0, 4) if mid_price > 0 else None
            except Exception as exc:
                row["ig_preflight_error"] = f"{type(exc).__name__}: {exc}"

        spread = row.get("ig_spread_bps")
        spread_pass = spread is not None and _safe_float(spread, 1e9) <= float(rule["spread_gate_bps"])
        row["spread_gate_bps"] = float(rule["spread_gate_bps"])
        row["spread_pass"] = spread_pass
        rejection_reasons: List[str] = []
        if live.direction not in {"BUY", "SELL"}:
            rejection_reasons.append("NO_DIRECTION")
        if not quant_pass:
            rejection_reasons.append("QUANT_BELOW_28")
        if not ai_pass:
            rejection_reasons.append("MODEL_AI_BELOW_40")
        if fast_score < STANDARD_FAST_SCORE_MIN:
            rejection_reasons.append("FAST_BELOW_60")
        if not selection_stable:
            rejection_reasons.append("SELECTION_UNSTABLE")
        if trades < HISTORICAL_MIN_TRADES:
            rejection_reasons.append("HOLDOUT_SAMPLE_BELOW_MIN")
        if win_rate < HISTORICAL_WIN_RATE_TARGET:
            rejection_reasons.append("HOLDOUT_WR_BELOW_60")
        if profit_factor < HISTORICAL_PROFIT_FACTOR_TARGET:
            rejection_reasons.append("PROFIT_FACTOR_BELOW_1_20")
        if not walk_forward["passed"]:
            rejection_reasons.append("WALK_FORWARD_BELOW_60")
            rejection_reasons.extend(
                reason for reason in (walk_forward.get("rejection_reasons") or [])
                if reason not in rejection_reasons
            )
        if promising and not row["ig_tradeable"]:
            rejection_reasons.append("IG_NOT_TRADEABLE")
        if promising and not spread_pass:
            rejection_reasons.append("SPREAD_GATE_FAIL")
            if spread is None:
                rejection_reasons.append("SPREAD_QUOTE_UNAVAILABLE")
            elif _safe_float(spread, 1e9) > float(rule["spread_gate_bps"]):
                rejection_reasons.append("SPREAD_TOO_WIDE")
        if not promising:
            rejection_reasons.append("IG_PREFLIGHT_NOT_REACHED")
        row["rejection_reasons"] = rejection_reasons
        row["standard_eligible"] = bool(
            live.direction in {"BUY", "SELL"}
            and quant_pass
            and ai_pass
            and validated_target
            and fast_score >= STANDARD_FAST_SCORE_MIN
            and row["ig_tradeable"]
            and spread_pass
        )
        row["trade_eligible"] = row["standard_eligible"]
        row["direction_match"] = live.direction in {"BUY", "SELL"}
        row["confidence_qualified"] = bool(quant_pass and ai_pass)
        row["intelligence_source"] = "CATEGORY_SPECIALIST_V693"
        row["fast_threshold_source"] = "CATEGORY_SPECIALIST_OPTIMISED"
        row["spread_bps"] = row.get("ig_spread_bps")
        row["spread_limit_bps"] = float(rule["spread_gate_bps"])
        row["required_fast_score"] = COMPOUND_FAST_SCORE_MIN

        pf_score = min(100.0, max(0.0, profit_factor / 2.0 * 100.0))
        stability_bonus = 5.0 if selection_stable else 0.0
        row["category_rank_score"] = round(
            0.28 * fast_score
            + 0.24 * model_ai * 100.0
            + 0.18 * live.quant_confidence * 100.0
            + 0.15 * win_rate * 100.0
            + 0.10 * pf_score
            + stability_bonus,
            3,
        )
        return row

    def run_now(self, category: Optional[str] = None) -> Dict[str, Any]:
        batch = self._next_batch(category)
        evaluations: Dict[str, Any] = {}
        last_error: Optional[str] = None
        for seed in batch:
            key = str(seed.get("key") or seed.get("name"))
            try:
                evaluations[key] = self._evaluate_seed(seed)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                evaluations[key] = {
                    **seed,
                    "market": seed.get("name"),
                    "symbol": key,
                    "category": seed.get("category"),
                    "version": self.VERSION,
                    "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
                    "optimizer_complete": False,
                    "walk_forward_complete": False,
                    "direction": "WAIT",
                    "deep_status": "CATEGORY_REJECT",
                    "historical_target_verified": False,
                    "historical_60_verified": False,
                    "historical_70_verified": False,
                    "standard_eligible": False,
                    "compound_eligible": False,
                    "evaluated_at": time.time(),
                    "reason": last_error,
                    "live_money_execution": False,
                }
        with self._lock:
            self._state.setdefault("evaluations", {}).update(evaluations)
            self._state["runs"] = int(self._state.get("runs") or 0) + 1
            self._state["last_run_at"] = time.time()
            self._state["last_error"] = last_error
            self._persist()
        return self.status()

    def _fresh_rows(self) -> List[Dict[str, Any]]:
        now = time.time()
        rows = list(self._current_evaluations().values())
        return [
            row for row in rows
            if now - _safe_float(row.get("evaluated_at")) <= self.candidate_ttl_seconds
        ]

    def _full_refresh_worker(self, force: bool = False) -> None:
        refresh = self._state.setdefault("full_refresh", {})
        pending = set(self.evidence_coverage()["pending_keys"])
        targets = [
            seed for seed in CATEGORY_MARKET_SEEDS
            if force or str(seed.get("key")) in pending
        ]
        with self._lock:
            refresh.update({
                "status": "RUNNING",
                "mode": "FORCE_ALL" if force else "PENDING_ONLY",
                "started_at": time.time(),
                "completed_at": None,
                "processed": 0,
                "total": len(targets),
                "current_key": None,
                "errors": 0,
                "last_error": None,
            })
            self._persist()
        for seed in targets:
            if self._stop.is_set():
                break
            key = str(seed.get("key") or seed.get("name"))
            with self._lock:
                refresh["current_key"] = key
                self._persist()
            try:
                row = self._evaluate_seed(dict(seed))
                with self._lock:
                    self._state.setdefault("evaluations", {})[key] = row
            except Exception as exc:
                with self._lock:
                    refresh["errors"] = int(refresh.get("errors") or 0) + 1
                    refresh["last_error"] = f"{key}: {type(exc).__name__}: {exc}"
            finally:
                with self._lock:
                    refresh["processed"] = int(refresh.get("processed") or 0) + 1
                    self._state["last_run_at"] = time.time()
                    self._persist()
        with self._lock:
            if self._stop.is_set():
                refresh["status"] = "STOPPED"
            elif int(refresh.get("errors") or 0) > 0:
                refresh["status"] = "COMPLETED_WITH_ERRORS"
            else:
                refresh["status"] = "COMPLETED"
            refresh["completed_at"] = time.time()
            refresh["current_key"] = None
            self._state["runs"] = int(self._state.get("runs") or 0) + 1
            self._persist()

    def start_full_refresh(self, force: bool = False) -> Dict[str, Any]:
        with self._refresh_lock:
            if self._full_refresh_thread and self._full_refresh_thread.is_alive():
                return self.full_refresh_status()
            self._full_refresh_thread = threading.Thread(
                target=self._full_refresh_worker,
                kwargs={"force": bool(force)},
                daemon=True,
                name="jasong-v692-full-market-optimiser",
            )
            self._full_refresh_thread.start()
        return self.full_refresh_status()

    def full_refresh_status(self) -> Dict[str, Any]:
        with self._lock:
            refresh = dict(self._state.get("full_refresh") or {})
        coverage = self.evidence_coverage()
        return {
            "version": self.VERSION,
            **refresh,
            "markets_optimised": coverage["markets_optimised"],
            "markets_pending_optimisation": coverage["markets_pending_optimisation"],
            "pending_keys": coverage["pending_keys"],
            "legacy_rows_excluded": coverage["legacy_rows_excluded"],
            "live_money_execution": False,
        }

    def category_rankings(self, category: Optional[str] = None, top_n: int = TOP_N_PER_CATEGORY) -> Dict[str, List[Dict[str, Any]]]:
        top_n = max(1, min(TOP_N_PER_CATEGORY, int(top_n)))
        rows = self._fresh_rows()
        wanted = [str(category).upper().strip()] if category else list(CATEGORY_ORDER)
        output: Dict[str, List[Dict[str, Any]]] = {}
        for cat in wanted:
            if cat not in CATEGORY_ORDER:
                continue
            pool = [dict(row) for row in rows if str(row.get("category") or "").upper() == cat]
            pool.sort(
                key=lambda row: (
                    bool(row.get("standard_eligible")),
                    _safe_float(row.get("category_rank_score")),
                    _safe_float(row.get("smart_fast_score")),
                    _safe_float(row.get("model_ai_confidence")),
                    _safe_float(row.get("quant_confidence")),
                ),
                reverse=True,
            )
            ranked: List[Dict[str, Any]] = []
            for idx, row in enumerate(pool[:top_n], start=1):
                row["category_rank"] = idx
                row["compound_slot_candidate"] = idx <= COMPOUND_SLOTS_PER_CATEGORY
                row["compound_eligible"] = bool(
                    idx <= COMPOUND_SLOTS_PER_CATEGORY
                    and row.get("standard_eligible")
                    and _safe_float(row.get("smart_fast_score")) >= COMPOUND_FAST_SCORE_MIN
                )
                row["rank"] = idx
                row["source_rank"] = idx
                row["eligible"] = row["compound_eligible"]
                row["elite_eligible"] = row["compound_eligible"]
                row["execution_eligible"] = row["compound_eligible"]
                row["selected"] = row["compound_eligible"]
                row["prime_qualified"] = row["compound_eligible"]
                row["trade_class"] = "ELITE" if row["compound_eligible"] else "OBSERVE"
                if row["compound_eligible"] and row.get("quality_tier") == "A+":
                    row["elite_state"] = "ELITE_A_PLUS"
                elif row["compound_eligible"]:
                    row["elite_state"] = "ELITE_A"
                else:
                    row["elite_state"] = "OBSERVE"
                row["execution_basis"] = "CATEGORY_TOP2_VALIDATED_60_WF" if row["compound_eligible"] else "NOT_QUALIFIED"
                row["rejection_reasons"] = [] if row["compound_eligible"] else list(row.get("rejection_reasons") or [])
                ranked.append(row)
            output[cat] = ranked
        return output

    def candidates(self) -> List[Dict[str, Any]]:
        rankings = self.category_rankings()
        return [row for category in CATEGORY_ORDER for row in rankings.get(category, [])]

    def compound_candidates(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for category, ranked in self.category_rankings().items():
            for row in ranked:
                if row.get("compound_eligible"):
                    item = dict(row)
                    item["compound_source_category"] = category
                    item["compound_source_rank"] = item.get("category_rank")
                    rows.append(item)
        rows.sort(
            key=lambda row: (
                _safe_float(row.get("category_rank_score")),
                _safe_float(row.get("smart_fast_score")),
                _safe_float(row.get("model_ai_confidence")),
            ),
            reverse=True,
        )
        return rows

    # ------------------------------------------------------------------
    # V6.8.x global-market compatibility surface
    # ------------------------------------------------------------------
    # backend/main.py already exposes /global-markets/* routes. Once the
    # specialist engine becomes GLOBAL_MARKET_ENGINE those routes must remain
    # callable instead of failing on missing methods.
    def opportunity_board(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        rows = self.candidates()
        rows.sort(
            key=lambda row: (
                bool(row.get("standard_eligible")),
                bool(row.get("compound_eligible")),
                _safe_float(row.get("category_rank_score")),
                _safe_float(row.get("smart_fast_score")),
            ),
            reverse=True,
        )
        return rows[:limit]

    def refresh_opportunity_board(self) -> Dict[str, Any]:
        rows = self.opportunity_board(limit=100)
        return {
            "version": self.VERSION,
            "count": len(rows),
            "eligibility_refresh_seconds": self.eligibility_refresh_seconds,
            "opportunities": rows,
            "live_money_execution": False,
        }

    def correlation_matrix(self) -> Dict[str, Dict[str, float]]:
        rows = self.candidates()
        series: Dict[str, List[float]] = {}
        for row in rows:
            values = row.get("recent_returns") or []
            key = str(row.get("symbol") or row.get("key") or "").upper().strip()
            if key and isinstance(values, list) and len(values) >= 20:
                series[key] = [_safe_float(value) for value in values]

        keys = list(series)
        matrix: Dict[str, Dict[str, float]] = {key: {} for key in keys}
        for left in keys:
            for right in keys:
                if left == right:
                    matrix[left][right] = 1.0
                    continue
                a = series[left]
                b = series[right]
                n = min(len(a), len(b))
                if n < 20:
                    matrix[left][right] = 0.0
                    continue
                a = a[-n:]
                b = b[-n:]
                ma = sum(a) / n
                mb = sum(b) / n
                cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
                va = sum((x - ma) ** 2 for x in a)
                vb = sum((y - mb) ** 2 for y in b)
                denom = math.sqrt(va * vb)
                matrix[left][right] = float(cov / denom) if denom > 0 else 0.0
        return matrix

    def optimizer_summary(self) -> Dict[str, Any]:
        current = list(self._current_evaluations().values())
        categories: Dict[str, Any] = {}
        for category in CATEGORY_ORDER:
            rows = [row for row in current if str(row.get("category") or "").upper() == category]
            rows.sort(
                key=lambda row: (
                    bool(row.get("historical_target_verified")),
                    bool(row.get("walk_forward_pass")),
                    bool(row.get("optimizer_selection_stable")),
                    _safe_float(row.get("historical_win_rate")),
                    _safe_float(row.get("historical_profit_factor")),
                    _safe_float(row.get("optimizer_score")),
                    int(row.get("historical_trades") or 0),
                ),
                reverse=True,
            )
            best = rows[0] if rows else None
            categories[category] = {
                "market": (best or {}).get("market"),
                "symbol": (best or {}).get("symbol"),
                "selected_strategy_id": (best or {}).get("strategy_id"),
                "selected_strategy_name": (best or {}).get("strategy_name"),
                "optimizer_complete": bool((best or {}).get("optimizer_complete", False)),
                "optimizer_selection_stable": bool((best or {}).get("optimizer_selection_stable", False)),
                "selection_win_rate_pct": (best or {}).get("selection_win_rate_pct"),
                "selection_profit_factor": (best or {}).get("selection_profit_factor"),
                "final_holdout_win_rate_pct": (best or {}).get("historical_win_rate_pct"),
                "final_holdout_profit_factor": (best or {}).get("historical_profit_factor"),
                "walk_forward_pass": bool((best or {}).get("walk_forward_pass", False)),
                "walk_forward_min_win_rate_pct": (best or {}).get("walk_forward_min_win_rate_pct"),
                "walk_forward_median_win_rate_pct": (best or {}).get("walk_forward_median_win_rate_pct"),
                "walk_forward_profitable_folds": (best or {}).get("walk_forward_profitable_folds"),
                "historical_target_verified": bool((best or {}).get("historical_target_verified", False)),
                "historical_60_verified": bool((best or {}).get("historical_60_verified", False)),
                "historical_70_verified": bool((best or {}).get("historical_70_verified", False)),
                "leaderboard": (best or {}).get("optimizer_leaderboard", []),
            }
        return {
            "version": self.VERSION,
            "method": "finite variant selection 40%-70%; untouched final 30% split into 3 chronological folds; execution target 60% WR / PF 1.20 / each qualifying WF fold >=60%",
            "final_holdout_used_for_selection": False,
            "walk_forward_folds": WALK_FORWARD_FOLDS,
            "quant_min_pct": 28.0,
            "model_ai_min_pct": 40.0,
            "historical_validation_target_pct": HISTORICAL_WIN_RATE_TARGET * 100.0,
            "profit_factor_target": HISTORICAL_PROFIT_FACTOR_TARGET,
            "historical_min_trades": HISTORICAL_MIN_TRADES,
            "fast_score_min": STANDARD_FAST_SCORE_MIN,
            "walk_forward_min_pct": WALK_FORWARD_MIN_FOLD_WIN_RATE * 100.0,
            "walk_forward_policy": {
                "all_qualifying_folds_must_meet_min_wr": True,
                "minimum_qualifying_fold_wr_pct": WALK_FORWARD_MIN_FOLD_WIN_RATE * 100.0,
                "median_fold_wr_pct": WALK_FORWARD_MIN_MEDIAN_WIN_RATE * 100.0,
                "minimum_profitable_folds": WALK_FORWARD_MIN_PROFITABLE_FOLDS,
                "minimum_trades_per_fold": WALK_FORWARD_MIN_FOLD_TRADES,
                "minimum_qualifying_folds": WALK_FORWARD_MIN_FOLDS,
                "holdout_min_trades": HISTORICAL_MIN_TRADES,
            },
            "coverage": self.evidence_coverage(),
            "categories": categories,
            "live_money_execution": False,
        }

    def status(self) -> Dict[str, Any]:
        rankings = self.category_rankings()
        by_category: Dict[str, Any] = {}
        standard_ready = compound_ready = 0
        for category in CATEGORY_ORDER:
            rows = rankings.get(category, [])
            standard = sum(1 for row in rows if row.get("standard_eligible"))
            compound = sum(1 for row in rows if row.get("compound_eligible"))
            standard_ready += standard
            compound_ready += compound
            by_category[category] = {
                "strategy": "Adaptive specialist optimiser (3 finite variants)",
                "strategy_variants": len(STRATEGY_VARIANTS[category]),
                "ranked": len(rows),
                "standard_ready": standard,
                "compound_ready": compound,
                "top": rows[0] if rows else None,
            }

        evaluated_by_asset_class: Dict[str, int] = {}
        for row in self._fresh_rows():
            asset_class = str(
                row.get("asset_class")
                or row.get("category")
                or "UNKNOWN"
            ).upper().strip()
            evaluated_by_asset_class[asset_class] = (
                evaluated_by_asset_class.get(asset_class, 0) + 1
            )

        with self._lock:
            return {
                "version": self.VERSION,
                "name": "JASONG V6.9.3 COMPLETE IG DEMO TEST INTELLIGENCE",
                "enabled": bool(self._state.get("enabled", True)),
                "confidence_policy": {
                    "quant_min": QUANT_MIN_CONFIDENCE,
                    "quant_min_pct": 28.0,
                    "model_ai_min": MODEL_AI_MIN_CONFIDENCE,
                    "model_ai_min_pct": 40.0,
                    "historical_validation_target_pct": HISTORICAL_WIN_RATE_TARGET * 100.0,
                    "profit_factor_target": HISTORICAL_PROFIT_FACTOR_TARGET,
                    "historical_min_trades": HISTORICAL_MIN_TRADES,
                    "fast_score_min": STANDARD_FAST_SCORE_MIN,
                    "walk_forward_min_pct": WALK_FORWARD_MIN_FOLD_WIN_RATE * 100.0,
                    "walk_forward_all_qualifying_folds_must_meet_60": True,
                    "walk_forward_median_min_pct": WALK_FORWARD_MIN_MEDIAN_WIN_RATE * 100.0,
                    "walk_forward_min_profitable_folds": WALK_FORWARD_MIN_PROFITABLE_FOLDS,
                    "walk_forward_min_trades_per_fold": WALK_FORWARD_MIN_FOLD_TRADES,
                    "optimizer_final_holdout_pct": 30.0,
                    "optimizer_selection_window_pct": 30.0,
                    "walk_forward_folds": WALK_FORWARD_FOLDS,
                },
                "categories": by_category,
                "category_count": len(CATEGORY_ORDER),
                "universe_size": len(CATEGORY_MARKET_SEEDS),
                "fresh_evaluations": len(self._fresh_rows()),
                "standard_ready": standard_ready,
                "compound_ready": compound_ready,
                # Backward-compatible names consumed by the existing
                # V6.8.x /global-markets status/dashboard surface.
                "elite_ready": compound_ready,
                "evaluated_by_asset_class": evaluated_by_asset_class,
                "eligibility_refresh_seconds": self.eligibility_refresh_seconds,
                "heavy_scan_seconds": self.scan_interval_seconds,
                "top_n_per_category": TOP_N_PER_CATEGORY,
                "strategy_optimizer": {
                    "enabled": True,
                    "variants_per_category": 3,
                    "selection_window": "40%-70%",
                    "final_holdout": "70%-100% split into 3 chronological folds",
                    "walk_forward_folds": WALK_FORWARD_FOLDS,
                    "final_holdout_used_for_selection": False,
                    "non_overlapping_backtest_positions": True,
                },
                "evidence_hygiene": self.evidence_coverage(),
                "full_refresh": self.full_refresh_status(),
                "compound_slots_per_category": COMPOUND_SLOTS_PER_CATEGORY,
                "runs": int(self._state.get("runs") or 0),
                "last_run_at": self._state.get("last_run_at"),
                "last_batch_keys": list(self._state.get("last_batch_keys") or []),
                "last_error": self._state.get("last_error"),
                "state_path": self.state_path,
                "execution_mode": "IG_DEMO_ONLY",
                "live_money_execution": False,
            }

    def start_thread(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="jasong-category-strategies")
            self._thread.start()

    def _loop(self) -> None:
        if self._stop.wait(12.0):
            return
        while not self._stop.is_set():
            try:
                if self._state.get("enabled", True):
                    coverage = self.evidence_coverage()
                    refresh_running = bool(self._full_refresh_thread and self._full_refresh_thread.is_alive())
                    if self.auto_full_refresh and coverage["markets_pending_optimisation"] > 0:
                        if not refresh_running:
                            self.start_full_refresh()
                    elif not refresh_running:
                        self.run_now()
            except Exception as exc:
                with self._lock:
                    self._state["last_error"] = f"{type(exc).__name__}: {exc}"
                    self._persist()
            self._stop.wait(self.scan_interval_seconds)

    def stop_thread(self) -> None:
        self._stop.set()
