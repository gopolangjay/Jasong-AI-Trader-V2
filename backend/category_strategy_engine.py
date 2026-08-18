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
# JASONG AI TRADER V6.9.0 - SPECIALIST MARKET CATEGORY INTELLIGENCE
# ---------------------------------------------------------------------------
# Design contract
#   * six independent specialist categories;
#   * live confidence floors remain 28% Quant + 40% directional Model-AI;
#   * historical 70% is an evidence/validation target, NOT a live confidence;
#   * each category ranks at most five selections;
#   * ranks #1 and #2 are Compound slot candidates only when every gate passes;
#   * execution remains IG DEMO only; this module contains no production URL.
# ---------------------------------------------------------------------------

VERSION = "6.9.0"
QUANT_MIN_CONFIDENCE = 0.28
MODEL_AI_MIN_CONFIDENCE = 0.40
HISTORICAL_WIN_RATE_TARGET = 0.70
STANDARD_FAST_SCORE_MIN = 80.0
COMPOUND_FAST_SCORE_MIN = 90.0
TOP_N_PER_CATEGORY = 5
COMPOUND_SLOTS_PER_CATEGORY = 2

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


_SIGNAL_FUNC: Dict[str, Callable[[pd.Series], StrategySignal]] = {
    "FOREX": _fx_signal,
    "INDICES": _index_signal,
    "CRYPTO": _crypto_signal,
    "METALS": _metals_signal,
    "ENERGY": _energy_signal,
    "SHARES": _share_signal,
}


def _historical_grade(trades: int, win_rate: float, profit_factor: float, max_dd: float) -> tuple[str, str, bool]:
    validated_70 = trades >= 30 and win_rate >= HISTORICAL_WIN_RATE_TARGET and profit_factor >= 1.30 and max_dd <= 0.12
    if trades >= 40 and win_rate >= 0.72 and profit_factor >= 1.50 and max_dd <= 0.10:
        return "A+", "CATEGORY_VERIFIED_70", True
    if validated_70:
        return "A", "CATEGORY_VERIFIED_70", True
    if trades >= 20 and win_rate >= 0.62 and profit_factor >= 1.10 and max_dd <= 0.16:
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
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state = self._load_state()

    def _default_state(self) -> Dict[str, Any]:
        return {
            "version": self.VERSION,
            "enabled": True,
            "offset_by_category": {category: 0 for category in CATEGORY_ORDER},
            "runs": 0,
            "last_run_at": None,
            "last_error": None,
            "evaluations": {},
            "last_batch_keys": [],
        }

    def _load_state(self) -> Dict[str, Any]:
        state = self._default_state()
        try:
            if os.path.exists(self.state_path):
                with open(self.state_path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                if isinstance(raw, dict):
                    state.update(raw)
        except Exception:
            pass
        state["version"] = self.VERSION
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

    @staticmethod
    def _directional_ai(row: pd.Series, direction: str) -> float:
        up = _confidence01(row.get("JASONG_UP_PROB"))
        if direction == "BUY":
            return up
        if direction == "SELL":
            return 1.0 - up
        return 0.0

    def _evaluate_seed(self, seed: Dict[str, Any]) -> Dict[str, Any]:
        category = str(seed.get("category") or "").upper()
        if category not in _SIGNAL_FUNC:
            raise ValueError(f"Unsupported category: {category}")
        raw = self.frame_func(seed)
        frame = _feature_frame(raw)
        signal_func = _SIGNAL_FUNC[category]
        rule = CATEGORY_RULES[category]
        holding = int(rule["holding_bars"])
        cost = float(rule["cost_bps"]) / 10000.0

        # Genuine held-out assessment on the last 30%. No result is called 70%
        # verified unless the specialist strategy itself reaches that threshold.
        cut = max(200, int(len(frame) * 0.70))
        strategy_returns: List[float] = []
        for i in range(cut, max(cut, len(frame) - holding)):
            row = frame.iloc[i]
            sig = signal_func(row)
            if sig.direction not in {"BUY", "SELL"} or sig.quant_confidence < QUANT_MIN_CONFIDENCE:
                continue
            ai = self._directional_ai(row, sig.direction)
            if ai < MODEL_AI_MIN_CONFIDENCE:
                continue
            entry = _safe_float(row["Close"])
            exit_price = _safe_float(frame.iloc[i + holding]["Close"])
            if entry <= 0 or exit_price <= 0:
                continue
            gross = exit_price / entry - 1.0
            if sig.direction == "SELL":
                gross = -gross
            strategy_returns.append(gross - cost)

        trades = len(strategy_returns)
        wins = sum(1 for value in strategy_returns if value > 0.0)
        losses = trades - wins
        win_rate = wins / trades if trades else 0.0
        profit_factor = _profit_factor(strategy_returns)
        max_dd = _max_drawdown(strategy_returns)
        quality, category_validation_status, validated_70 = _historical_grade(trades, win_rate, profit_factor, max_dd)
        # Existing V6.8.x Compound recognises VERIFIED/WATCH/REJECT. Keep the
        # category-specific evidence label separately so no compatibility gate is
        # weakened or confused by a new unrecognised deep-status string.
        deep_status = "VERIFIED" if validated_70 else ("WATCH" if quality == "B" else "REJECT")
        fast_score = _fast_score(trades, win_rate, profit_factor, max_dd)

        latest = frame.dropna(subset=["Close"]).iloc[-1]
        live = signal_func(latest)
        model_ai = self._directional_ai(latest, live.direction)
        quant_pass = live.quant_confidence >= QUANT_MIN_CONFIDENCE
        ai_pass = model_ai >= MODEL_AI_MIN_CONFIDENCE

        row: Dict[str, Any] = {
            **seed,
            "version": self.VERSION,
            "market": seed.get("name"),
            "symbol": seed.get("key"),
            "strategy_id": rule["strategy_id"],
            "strategy_name": rule["strategy_name"],
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
            "historical_wins": wins,
            "historical_losses": losses,
            "historical_max_drawdown_pct": round(max_dd * 100.0, 2),
            "historical_70_verified": validated_70,
            "quality_tier": quality,
            "historical_grade": quality,
            "deep_status": deep_status,
            "category_validation_status": category_validation_status,
            "verified": validated_70,
            "experimental": not validated_70,
            "strategy_quarantined": False,
            "smart_fast_score": round(fast_score, 2),
            "strategy_reason": live.reason,
            "signal_reason": live.reason,
            "live_price": _safe_float(latest.get("Close")),
            "rsi": round(_safe_float(latest.get("RSI14"), 50.0), 2),
            "adx": round(_safe_float(latest.get("ADX14")), 2),
            "holding_bars": holding,
            "validation_target_pct": 70.0,
            "analysis_source": "CATEGORY_SPECIALIST_PLUS_JASONG_MODEL_HELD_OUT",
            # Persist a bounded recent-return vector so the cross-category risk
            # engine measures actual co-movement instead of assuming zero.
            "recent_returns": [
                round(_safe_float(value), 10)
                for value in frame["RET1"].dropna().tail(160).tolist()
            ],
            "evaluated_at": time.time(),
            "ig_tradeable": False,
            "ig_epic": None,
            "ig_market_status": None,
            "ig_spread_bps": None,
            "standard_eligible": False,
            "compound_slot_candidate": False,
            "compound_eligible": False,
            "live_money_execution": False,
        }

        # Resolve IG only after the specialist signal passes the two live confidence
        # paths and has at least probation-level historical evidence.
        promising = live.direction in {"BUY", "SELL"} and quant_pass and ai_pass and fast_score >= 65.0
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
                details = market.get("details") or {}
                snapshot = details.get("snapshot") or {}
                bid = _safe_float(market.get("bid") if market.get("bid") is not None else snapshot.get("bid"))
                offer = _safe_float(market.get("offer") if market.get("offer") is not None else snapshot.get("offer"))
                if bid > 0 and offer >= bid:
                    mid = (bid + offer) / 2.0
                    row["ig_spread_bps"] = round((offer - bid) / mid * 10000.0, 4) if mid > 0 else None
            except Exception as exc:
                row["ig_preflight_error"] = f"{type(exc).__name__}: {exc}"

        spread = row.get("ig_spread_bps")
        # Fail closed: a tradeable candidate without a usable executable quote
        # is not allowed to pass the spread gate.
        spread_pass = spread is not None and _safe_float(spread, 1e9) <= float(rule["spread_gate_bps"])
        row["spread_gate_bps"] = float(rule["spread_gate_bps"])
        row["spread_pass"] = spread_pass
        row["standard_eligible"] = bool(
            live.direction in {"BUY", "SELL"}
            and quant_pass
            and ai_pass
            and validated_70
            and fast_score >= STANDARD_FAST_SCORE_MIN
            and row["ig_tradeable"]
            and spread_pass
        )
        # V6.8.x Compound candidate-schema compatibility. The Compound engine
        # still performs its own adaptive-regime, PRIME-forward and correlation
        # gates; these fields simply preserve the established contract.
        row["trade_eligible"] = row["standard_eligible"]
        row["direction_match"] = live.direction in {"BUY", "SELL"}
        row["confidence_qualified"] = bool(quant_pass and ai_pass)
        row["intelligence_source"] = "CATEGORY_SPECIALIST_V690"
        row["fast_threshold_source"] = "CATEGORY_SPECIALIST"
        row["spread_bps"] = row.get("ig_spread_bps")
        row["spread_limit_bps"] = float(rule["spread_gate_bps"])
        row["required_fast_score"] = COMPOUND_FAST_SCORE_MIN

        # Ranking score is category-local. It does not alter the 28/40 gates.
        pf_score = min(100.0, max(0.0, profit_factor / 2.0 * 100.0))
        row["category_rank_score"] = round(
            0.30 * fast_score
            + 0.25 * model_ai * 100.0
            + 0.20 * live.quant_confidence * 100.0
            + 0.15 * win_rate * 100.0
            + 0.10 * pf_score,
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
                    "direction": "WAIT",
                    "deep_status": "CATEGORY_REJECT",
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
        with self._lock:
            rows = [dict(v) for v in (self._state.get("evaluations") or {}).values() if isinstance(v, dict)]
        return [row for row in rows if now - _safe_float(row.get("evaluated_at")) <= self.candidate_ttl_seconds]

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
                row["execution_basis"] = "CATEGORY_TOP2_VALIDATED_70" if row["compound_eligible"] else "NOT_QUALIFIED"
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
                "strategy": CATEGORY_RULES[category]["strategy_name"],
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
                "name": "JASONG SPECIALIST MARKET CATEGORY INTELLIGENCE",
                "enabled": bool(self._state.get("enabled", True)),
                "confidence_policy": {
                    "quant_min": QUANT_MIN_CONFIDENCE,
                    "quant_min_pct": 28.0,
                    "model_ai_min": MODEL_AI_MIN_CONFIDENCE,
                    "model_ai_min_pct": 40.0,
                    "historical_validation_target_pct": 70.0,
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
                    self.run_now()
            except Exception as exc:
                with self._lock:
                    self._state["last_error"] = f"{type(exc).__name__}: {exc}"
                    self._persist()
            self._stop.wait(self.scan_interval_seconds)

    def stop_thread(self) -> None:
        self._stop.set()
