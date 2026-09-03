from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import pandas as pd

from forex_liquidity_lines_strategy import (
    LIQUID_FOREX_PAIRS,
    STRATEGY_ID as FOREX_STRATEGY_ID,
    STRATEGY_NAME as FOREX_STRATEGY_NAME,
    VERSION as FOREX_STRATEGY_VERSION,
    analyze_forex,
)
from xauusd_liquidity_strategy import (
    STRATEGY_ID as XAUUSD_STRATEGY_ID,
    STRATEGY_NAME as XAUUSD_STRATEGY_NAME,
    VERSION as XAUUSD_STRATEGY_VERSION,
    analyze_xauusd,
)


# ============================================================================
# JASONG AI TRADER V6.3 CLEAN CORE
# ACTIVE FX + XAUUSD LIQUIDITY / MARKET-STRUCTURE STRATEGIES
# ============================================================================
#
# EXECUTION AUTHORITY:
#   H4 STRUCTURE -> PREMIUM/DISCOUNT -> M15 LIQUIDITY SWEEP -> H1/M15
#   BOS/CHoCH -> OB/FVG RETEST -> CLOSED-CANDLE CONFIRMATION -> >=2R ROOM
#   -> PAIR-RELEVANT LONDON/NEW YORK/TOKYO/SYDNEY SESSION
#   -> IG TRADEABILITY/SPREAD -> IG DEMO ENTRY.
#
# The former EMA/ADX/range entry router is retired from autonomous execution.
# The active universe is Gold plus the 28 liquid combinations of the eight major
# currencies. Exotics remain analysis-only because the supplied material marks
# their spreads and volatility as unsuitable for this setup.
#
# A strategy ID is versioned when its live rules materially change. This prevents
# old broker evidence from being silently mixed with a new strategy definition.
# ============================================================================

VERSION = "6.11-fx-xau-liquidity-active-v1"
EVIDENCE_SCHEMA_VERSION = 6

QUANT_MIN_CONFIDENCE = 0.28
MODEL_AI_MIN_CONFIDENCE = 0.40
STANDARD_FAST_SCORE_MIN = 45.0
COMPOUND_FAST_SCORE_MIN = 45.0

# Retained only for backward-compatible diagnostics/API fields.
# They do NOT gate current execution in this module.
HISTORICAL_WIN_RATE_TARGET = 0.60
HISTORICAL_PROFIT_FACTOR_TARGET = 1.20
HISTORICAL_MIN_TRADES = 0

TOP_N_PER_CATEGORY = 5
COMPOUND_SLOTS_PER_CATEGORY = 2

# Exact agreed regime threshold.
TREND_ADX_MIN = 25.0

CATEGORY_ORDER = (
    "FOREX",
    "INDICES",
    "CRYPTO",
    "METALS",
    "ENERGY",
    "SHARES",
)


CATEGORY_RULES: Dict[str, Dict[str, Any]] = {
    "FOREX": {
        "strategy_id": FOREX_STRATEGY_ID,
        "strategy_name": FOREX_STRATEGY_NAME,
        "holding_bars": 48,
        "spread_gate_bps": 8.0,
        "min_atr_pct": 0.010,
        "max_atr_q95_multiplier": 1.40,
        "min_rel_volume": 0.50,
        "trend_requires_vwap": False,
    },
    "INDICES": {
        "strategy_id": "INDEX_CURRENT_CANDLE_REGIME_V3",
        "strategy_name": "Index Current-Candle Trend / Range Router",
        "holding_bars": 2,
        "spread_gate_bps": 18.0,
        "min_atr_pct": 0.015,
        "max_atr_q95_multiplier": 1.35,
        "min_rel_volume": 0.55,
        "trend_requires_vwap": True,
    },
    "CRYPTO": {
        "strategy_id": "CRYPTO_CURRENT_CANDLE_REGIME_V3",
        "strategy_name": "Crypto Current-Candle Trend / Range Router",
        "holding_bars": 4,
        "spread_gate_bps": 80.0,
        "min_atr_pct": 0.040,
        "max_atr_q95_multiplier": 1.30,
        "min_rel_volume": 0.55,
        "trend_requires_vwap": False,
    },
    "METALS": {
        "strategy_id": XAUUSD_STRATEGY_ID,
        "strategy_name": XAUUSD_STRATEGY_NAME,
        "holding_bars": 48,
        "spread_gate_bps": 22.0,
        "min_atr_pct": 0.015,
        "max_atr_q95_multiplier": 1.35,
        "min_rel_volume": 0.50,
        "trend_requires_vwap": False,
    },
    "ENERGY": {
        "strategy_id": "ENERGY_CURRENT_CANDLE_REGIME_V3",
        "strategy_name": "Energy Current-Candle Trend / Range Router",
        "holding_bars": 3,
        "spread_gate_bps": 22.0,
        "min_atr_pct": 0.025,
        "max_atr_q95_multiplier": 1.30,
        "min_rel_volume": 0.55,
        "trend_requires_vwap": True,
    },
    "SHARES": {
        "strategy_id": "SHARE_CURRENT_CANDLE_REGIME_V3",
        "strategy_name": "Share Current-Candle Trend / Range Router",
        "holding_bars": 4,
        "spread_gate_bps": 35.0,
        "min_atr_pct": 0.025,
        "max_atr_q95_multiplier": 1.35,
        "min_rel_volume": 0.65,
        "trend_requires_vwap": True,
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
    }.get(
        str(category or "").upper().strip(),
        str(category or "").upper().strip(),
    )


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


# Preserve the original catalogue and add every liquid major-currency pair.
CATEGORY_MARKET_SEEDS: List[Dict[str, Any]] = [
    # FOREX (28)
    _seed("EURUSD", "EUR/USD", "FOREX", "EURUSD=X", ig_search_terms=["EUR/USD"], expected_types=["CURRENCIES"], name_tokens=["EUR", "USD"], exposure_tags=["EUR", "USD", "FX_MAJOR"], ig_symbol="EUR/USD"),
    _seed("GBPUSD", "GBP/USD", "FOREX", "GBPUSD=X", ig_search_terms=["GBP/USD"], expected_types=["CURRENCIES"], name_tokens=["GBP", "USD"], exposure_tags=["GBP", "USD", "FX_MAJOR"], ig_symbol="GBP/USD"),
    _seed("USDJPY", "USD/JPY", "FOREX", "USDJPY=X", ig_search_terms=["USD/JPY"], expected_types=["CURRENCIES"], name_tokens=["USD", "JPY"], exposure_tags=["USD", "JPY", "FX_MAJOR"], ig_symbol="USD/JPY"),
    _seed("AUDUSD", "AUD/USD", "FOREX", "AUDUSD=X", ig_search_terms=["AUD/USD"], expected_types=["CURRENCIES"], name_tokens=["AUD", "USD"], exposure_tags=["AUD", "USD", "FX_MAJOR"], ig_symbol="AUD/USD"),
    _seed("NZDUSD", "NZD/USD", "FOREX", "NZDUSD=X", ig_search_terms=["NZD/USD"], expected_types=["CURRENCIES"], name_tokens=["NZD", "USD"], exposure_tags=["NZD", "USD", "FX_MAJOR"], ig_symbol="NZD/USD"),
    _seed("USDCAD", "USD/CAD", "FOREX", "CAD=X", ig_search_terms=["USD/CAD"], expected_types=["CURRENCIES"], name_tokens=["USD", "CAD"], exposure_tags=["USD", "CAD", "FX_MAJOR"], ig_symbol="USD/CAD"),
    _seed("USDCHF", "USD/CHF", "FOREX", "CHF=X", ig_search_terms=["USD/CHF"], expected_types=["CURRENCIES"], name_tokens=["USD", "CHF"], exposure_tags=["USD", "CHF", "FX_MAJOR"], ig_symbol="USD/CHF"),
    _seed("EURJPY", "EUR/JPY", "FOREX", "EURJPY=X", ig_search_terms=["EUR/JPY"], expected_types=["CURRENCIES"], name_tokens=["EUR", "JPY"], exposure_tags=["EUR", "JPY", "FX_CROSS"], ig_symbol="EUR/JPY"),
    _seed("GBPJPY", "GBP/JPY", "FOREX", "GBPJPY=X", ig_search_terms=["GBP/JPY"], expected_types=["CURRENCIES"], name_tokens=["GBP", "JPY"], exposure_tags=["GBP", "JPY", "FX_CROSS"], ig_symbol="GBP/JPY"),
    _seed("EURGBP", "EUR/GBP", "FOREX", "EURGBP=X", ig_search_terms=["EUR/GBP"], expected_types=["CURRENCIES"], name_tokens=["EUR", "GBP"], exposure_tags=["EUR", "GBP", "FX_CROSS"], ig_symbol="EUR/GBP"),
    _seed("EURCHF", "EUR/CHF", "FOREX", "EURCHF=X", ig_search_terms=["EUR/CHF"], expected_types=["CURRENCIES"], name_tokens=["EUR", "CHF"], exposure_tags=["EUR", "CHF", "FX_CROSS"], ig_symbol="EUR/CHF"),
    _seed("EURCAD", "EUR/CAD", "FOREX", "EURCAD=X", ig_search_terms=["EUR/CAD"], expected_types=["CURRENCIES"], name_tokens=["EUR", "CAD"], exposure_tags=["EUR", "CAD", "FX_CROSS"], ig_symbol="EUR/CAD"),
    _seed("EURAUD", "EUR/AUD", "FOREX", "EURAUD=X", ig_search_terms=["EUR/AUD"], expected_types=["CURRENCIES"], name_tokens=["EUR", "AUD"], exposure_tags=["EUR", "AUD", "FX_CROSS"], ig_symbol="EUR/AUD"),
    _seed("EURNZD", "EUR/NZD", "FOREX", "EURNZD=X", ig_search_terms=["EUR/NZD"], expected_types=["CURRENCIES"], name_tokens=["EUR", "NZD"], exposure_tags=["EUR", "NZD", "FX_CROSS"], ig_symbol="EUR/NZD"),
    _seed("GBPCHF", "GBP/CHF", "FOREX", "GBPCHF=X", ig_search_terms=["GBP/CHF"], expected_types=["CURRENCIES"], name_tokens=["GBP", "CHF"], exposure_tags=["GBP", "CHF", "FX_CROSS"], ig_symbol="GBP/CHF"),
    _seed("GBPCAD", "GBP/CAD", "FOREX", "GBPCAD=X", ig_search_terms=["GBP/CAD"], expected_types=["CURRENCIES"], name_tokens=["GBP", "CAD"], exposure_tags=["GBP", "CAD", "FX_CROSS"], ig_symbol="GBP/CAD"),
    _seed("GBPAUD", "GBP/AUD", "FOREX", "GBPAUD=X", ig_search_terms=["GBP/AUD"], expected_types=["CURRENCIES"], name_tokens=["GBP", "AUD"], exposure_tags=["GBP", "AUD", "FX_CROSS"], ig_symbol="GBP/AUD"),
    _seed("GBPNZD", "GBP/NZD", "FOREX", "GBPNZD=X", ig_search_terms=["GBP/NZD"], expected_types=["CURRENCIES"], name_tokens=["GBP", "NZD"], exposure_tags=["GBP", "NZD", "FX_CROSS"], ig_symbol="GBP/NZD"),
    _seed("AUDJPY", "AUD/JPY", "FOREX", "AUDJPY=X", ig_search_terms=["AUD/JPY"], expected_types=["CURRENCIES"], name_tokens=["AUD", "JPY"], exposure_tags=["AUD", "JPY", "FX_CROSS"], ig_symbol="AUD/JPY"),
    _seed("AUDCHF", "AUD/CHF", "FOREX", "AUDCHF=X", ig_search_terms=["AUD/CHF"], expected_types=["CURRENCIES"], name_tokens=["AUD", "CHF"], exposure_tags=["AUD", "CHF", "FX_CROSS"], ig_symbol="AUD/CHF"),
    _seed("AUDCAD", "AUD/CAD", "FOREX", "AUDCAD=X", ig_search_terms=["AUD/CAD"], expected_types=["CURRENCIES"], name_tokens=["AUD", "CAD"], exposure_tags=["AUD", "CAD", "FX_CROSS"], ig_symbol="AUD/CAD"),
    _seed("AUDNZD", "AUD/NZD", "FOREX", "AUDNZD=X", ig_search_terms=["AUD/NZD"], expected_types=["CURRENCIES"], name_tokens=["AUD", "NZD"], exposure_tags=["AUD", "NZD", "FX_CROSS"], ig_symbol="AUD/NZD"),
    _seed("NZDJPY", "NZD/JPY", "FOREX", "NZDJPY=X", ig_search_terms=["NZD/JPY"], expected_types=["CURRENCIES"], name_tokens=["NZD", "JPY"], exposure_tags=["NZD", "JPY", "FX_CROSS"], ig_symbol="NZD/JPY"),
    _seed("NZDCHF", "NZD/CHF", "FOREX", "NZDCHF=X", ig_search_terms=["NZD/CHF"], expected_types=["CURRENCIES"], name_tokens=["NZD", "CHF"], exposure_tags=["NZD", "CHF", "FX_CROSS"], ig_symbol="NZD/CHF"),
    _seed("NZDCAD", "NZD/CAD", "FOREX", "NZDCAD=X", ig_search_terms=["NZD/CAD"], expected_types=["CURRENCIES"], name_tokens=["NZD", "CAD"], exposure_tags=["NZD", "CAD", "FX_CROSS"], ig_symbol="NZD/CAD"),
    _seed("CADJPY", "CAD/JPY", "FOREX", "CADJPY=X", ig_search_terms=["CAD/JPY"], expected_types=["CURRENCIES"], name_tokens=["CAD", "JPY"], exposure_tags=["CAD", "JPY", "FX_CROSS"], ig_symbol="CAD/JPY"),
    _seed("CADCHF", "CAD/CHF", "FOREX", "CADCHF=X", ig_search_terms=["CAD/CHF"], expected_types=["CURRENCIES"], name_tokens=["CAD", "CHF"], exposure_tags=["CAD", "CHF", "FX_CROSS"], ig_symbol="CAD/CHF"),
    _seed("CHFJPY", "CHF/JPY", "FOREX", "CHFJPY=X", ig_search_terms=["CHF/JPY"], expected_types=["CURRENCIES"], name_tokens=["CHF", "JPY"], exposure_tags=["CHF", "JPY", "FX_CROSS"], ig_symbol="CHF/JPY"),

    # INDICES (10)
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

    # CRYPTO (5)
    _seed("BITCOIN", "Bitcoin", "CRYPTO", "BTC-USD", ig_search_terms=["Bitcoin"], name_tokens=["BITCOIN"], exposure_tags=["CRYPTO", "CRYPTO_LARGE_CAP"]),
    _seed("ETHER", "Ether", "CRYPTO", "ETH-USD", ig_search_terms=["Ether", "Ethereum"], name_tokens=["ETHER"], exposure_tags=["CRYPTO", "CRYPTO_LARGE_CAP"]),
    _seed("SOLANA", "Solana", "CRYPTO", "SOL-USD", ig_search_terms=["Solana"], name_tokens=["SOLANA"], exposure_tags=["CRYPTO", "CRYPTO_ALT"]),
    _seed("XRP", "XRP", "CRYPTO", "XRP-USD", ig_search_terms=["XRP", "Ripple"], name_tokens=["XRP"], exposure_tags=["CRYPTO", "CRYPTO_ALT"]),
    _seed("LITECOIN", "Litecoin", "CRYPTO", "LTC-USD", ig_search_terms=["Litecoin"], name_tokens=["LITECOIN"], exposure_tags=["CRYPTO", "CRYPTO_ALT"]),

    # METALS (3)
    _seed("GOLD", "Gold", "METALS", "GC=F", ig_search_terms=["Spot Gold", "Gold"], expected_types=["COMMODITIES"], name_tokens=["GOLD"], exposure_tags=["PRECIOUS_METALS", "COMMODITIES"]),
    _seed("SILVER", "Silver", "METALS", "SI=F", ig_search_terms=["Spot Silver", "Silver"], expected_types=["COMMODITIES"], name_tokens=["SILVER"], exposure_tags=["PRECIOUS_METALS", "COMMODITIES"]),
    _seed("COPPER", "Copper", "METALS", "HG=F", ig_search_terms=["Copper"], expected_types=["COMMODITIES"], name_tokens=["COPPER"], exposure_tags=["INDUSTRIAL_METALS", "COMMODITIES"]),

    # ENERGY (3)
    _seed("USCRUDE", "US Crude", "ENERGY", "CL=F", ig_search_terms=["US Crude", "Oil - US Crude"], expected_types=["COMMODITIES"], name_tokens=["CRUDE"], exposure_tags=["ENERGY", "COMMODITIES"]),
    _seed("BRENT", "Brent Crude", "ENERGY", "BZ=F", ig_search_terms=["Brent Crude", "Oil - Brent Crude"], expected_types=["COMMODITIES"], name_tokens=["BRENT"], exposure_tags=["ENERGY", "COMMODITIES"]),
    _seed("NATGAS", "Natural Gas", "ENERGY", "NG=F", ig_search_terms=["Natural Gas"], expected_types=["COMMODITIES"], name_tokens=["NATURAL", "GAS"], exposure_tags=["ENERGY", "COMMODITIES"]),

    # SHARES (10)
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


def _active_execution_keys() -> Tuple[str, ...]:
    """Resolve Gold and/or the 28-pair liquid FX execution universe."""
    configured = str(
        os.getenv("JASONG_ACTIVE_EXECUTION_MARKETS", "GOLD,FOREX_ALL")
    )
    requested = {
        "".join(ch for ch in item.upper().strip() if ch.isalnum())
        for item in configured.split(",")
        if item.strip()
    }
    forex = set(LIQUID_FOREX_PAIRS)
    if "FOREXALL" in requested or "ALLSUPPORTED" in requested:
        requested.update(forex)
    if "ALLSUPPORTED" in requested:
        requested.add("GOLD")
    supported = {"GOLD", *forex}
    active = requested & supported
    if not active:
        # A malformed explicit override must narrow safely, never broaden.
        active = {"GOLD"}
    ordered = [
        str(seed["key"]) for seed in CATEGORY_MARKET_SEEDS
        if str(seed["key"]) in active
    ]
    return tuple(ordered)


ACTIVE_EXECUTION_KEYS = _active_execution_keys()


def _active_market_seeds() -> List[Dict[str, Any]]:
    active = set(ACTIVE_EXECUTION_KEYS)
    return [
        row for row in CATEGORY_MARKET_SEEDS
        if str(row.get("key") or "").upper() in active
    ]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except Exception:
        return default


def _confidence01(value: Any) -> float:
    number = _safe_float(value, 0.0)
    if 1.0 < number <= 100.0:
        number /= 100.0
    return max(0.0, min(1.0, number))


def _rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()
    avg_loss = loss.ewm(alpha=1.0 / length, adjust=False, min_periods=length).mean()
    rs = avg_gain / avg_loss.replace(0.0, float("nan"))
    return (100.0 - 100.0 / (1.0 + rs)).fillna(50.0)


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


def _detect_rsi_divergence(frame: pd.DataFrame) -> Tuple[pd.Series, pd.Series]:
    """Detect price/RSI divergence using only information available at each bar.

    Bullish:
      recent six-bar price low < previous six-bar price low, while RSI at the
      recent low is at least 2 points higher than RSI at the previous low.

    Bearish is the exact inverse using highs.

    The calculation is deliberately backward-looking and never uses future bars.
    """
    bullish = pd.Series(False, index=frame.index, dtype=bool)
    bearish = pd.Series(False, index=frame.index, dtype=bool)

    lows = frame["Low"]
    highs = frame["High"]
    rsi = frame["RSI14"]

    for i in range(14, len(frame)):
        previous = frame.iloc[i - 12:i - 6]
        recent = frame.iloc[i - 6:i + 1]
        if previous.empty or recent.empty:
            continue

        prev_low_idx = previous["Low"].idxmin()
        recent_low_idx = recent["Low"].idxmin()
        prev_high_idx = previous["High"].idxmax()
        recent_high_idx = recent["High"].idxmax()

        prev_low = _safe_float(lows.loc[prev_low_idx])
        recent_low = _safe_float(lows.loc[recent_low_idx])
        prev_low_rsi = _safe_float(rsi.loc[prev_low_idx], 50.0)
        recent_low_rsi = _safe_float(rsi.loc[recent_low_idx], 50.0)

        prev_high = _safe_float(highs.loc[prev_high_idx])
        recent_high = _safe_float(highs.loc[recent_high_idx])
        prev_high_rsi = _safe_float(rsi.loc[prev_high_idx], 50.0)
        recent_high_rsi = _safe_float(rsi.loc[recent_high_idx], 50.0)

        # Small epsilon avoids declaring identical prints a divergence.
        if prev_low > 0:
            bullish.iloc[i] = bool(
                recent_low < prev_low * 0.9998
                and recent_low_rsi >= prev_low_rsi + 2.0
            )
        if prev_high > 0:
            bearish.iloc[i] = bool(
                recent_high > prev_high * 1.0002
                and recent_high_rsi <= prev_high_rsi - 2.0
            )

    return bullish, bearish


def _feature_frame(raw: pd.DataFrame) -> pd.DataFrame:
    if raw is None or raw.empty:
        raise ValueError("No market data supplied")

    frame = raw.copy()
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = [str(col[0]) for col in frame.columns]

    for column in ("Open", "High", "Low", "Close"):
        if column not in frame.columns:
            raise ValueError(f"Missing required OHLC column: {column}")
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    if "Volume" not in frame.columns:
        frame["Volume"] = 1.0
    frame["Volume"] = pd.to_numeric(frame["Volume"], errors="coerce").fillna(1.0)

    frame = frame.dropna(subset=["Open", "High", "Low", "Close"]).copy()
    if len(frame) < 220:
        raise ValueError("At least 220 candles are required for closed-candle regime analysis")

    c = frame["Close"]
    frame["EMA20"] = c.ewm(span=20, adjust=False).mean()
    frame["EMA50"] = c.ewm(span=50, adjust=False).mean()
    frame["EMA200"] = c.ewm(span=200, adjust=False).mean()
    frame["RSI14"] = _rsi(c, 14)
    frame["ATR14"] = _atr(frame, 14)
    frame["ATR_PCT"] = frame["ATR14"] / c.replace(0.0, float("nan")) * 100.0
    frame["ADX14"] = _adx(frame, 14)

    frame["RET1"] = c.pct_change(1)
    frame["RET2"] = c.pct_change(2)
    frame["RET4"] = c.pct_change(4)
    frame["RET8"] = c.pct_change(8)
    frame["RET20"] = c.pct_change(20)
    frame["VOL20"] = frame["RET1"].rolling(20).std()
    frame["ATR_Q95"] = frame["ATR_PCT"].rolling(120, min_periods=40).quantile(0.95)

    frame["BB_MID"] = c.rolling(20).mean()
    bb_std = c.rolling(20).std()
    frame["BB_UPPER"] = frame["BB_MID"] + 2.0 * bb_std
    frame["BB_LOWER"] = frame["BB_MID"] - 2.0 * bb_std
    frame["BB_Z"] = (c - frame["BB_MID"]) / bb_std.replace(0.0, float("nan"))

    # Previous support/resistance only. shift(1) prevents the current candle from
    # defining the level that it is being tested against.
    frame["SUPPORT20"] = frame["Low"].rolling(20).min().shift(1)
    frame["RESISTANCE20"] = frame["High"].rolling(20).max().shift(1)
    frame["BREAKOUT_UP"] = c > frame["RESISTANCE20"]
    frame["BREAKOUT_DOWN"] = c < frame["SUPPORT20"]

    volume_median = frame["Volume"].rolling(20).median().replace(0.0, float("nan"))
    frame["REL_VOLUME"] = (
        frame["Volume"] / volume_median
    ).replace([float("inf"), float("-inf")], float("nan")).fillna(1.0)

    # Session VWAP. If timestamp sessions cannot be determined, a rolling VWAP
    # proxy is used; it does not invent market open/close times.
    typical = (frame["High"] + frame["Low"] + frame["Close"]) / 3.0
    positive_volume = frame["Volume"].where(frame["Volume"] > 0.0, 1.0)
    if isinstance(frame.index, pd.DatetimeIndex):
        session_key = pd.Series(frame.index.date, index=frame.index)
        pv = typical * positive_volume
        frame["SESSION_VWAP"] = (
            pv.groupby(session_key).cumsum()
            / positive_volume.groupby(session_key).cumsum()
        )
    else:
        frame["SESSION_VWAP"] = typical.rolling(8, min_periods=1).mean()

    frame["VWAP_ATR_DIST"] = (
        (c - frame["SESSION_VWAP"])
        / frame["ATR14"].replace(0.0, float("nan"))
    )
    frame["EMA20_ATR_DIST"] = (
        (c - frame["EMA20"])
        / frame["ATR14"].replace(0.0, float("nan"))
    )

    # Candle anatomy / closed-candle confirmation.
    body = (frame["Close"] - frame["Open"]).abs()
    candle_range = (frame["High"] - frame["Low"]).replace(0.0, float("nan"))
    upper_wick = frame["High"] - frame[["Open", "Close"]].max(axis=1)
    lower_wick = frame[["Open", "Close"]].min(axis=1) - frame["Low"]

    frame["BULL_CLOSE"] = frame["Close"] > frame["Open"]
    frame["BEAR_CLOSE"] = frame["Close"] < frame["Open"]
    frame["BODY_TO_RANGE"] = (body / candle_range).fillna(0.0)
    frame["UPPER_WICK_TO_RANGE"] = (upper_wick / candle_range).fillna(0.0)
    frame["LOWER_WICK_TO_RANGE"] = (lower_wick / candle_range).fillna(0.0)

    prev_open = frame["Open"].shift(1)
    prev_close = frame["Close"].shift(1)
    frame["BULL_ENGULFING"] = (
        (frame["Close"] > frame["Open"])
        & (prev_close < prev_open)
        & (frame["Open"] <= prev_close)
        & (frame["Close"] >= prev_open)
    )
    frame["BEAR_ENGULFING"] = (
        (frame["Close"] < frame["Open"])
        & (prev_close > prev_open)
        & (frame["Open"] >= prev_close)
        & (frame["Close"] <= prev_open)
    )
    frame["BULL_REJECTION"] = (
        (frame["LOWER_WICK_TO_RANGE"] >= 0.45)
        & (frame["Close"] >= frame["Open"])
        & (frame["BODY_TO_RANGE"] <= 0.50)
    )
    frame["BEAR_REJECTION"] = (
        (frame["UPPER_WICK_TO_RANGE"] >= 0.45)
        & (frame["Close"] <= frame["Open"])
        & (frame["BODY_TO_RANGE"] <= 0.50)
    )
    frame["BULL_REVERSAL_CANDLE"] = frame["BULL_ENGULFING"] | frame["BULL_REJECTION"]
    frame["BEAR_REVERSAL_CANDLE"] = frame["BEAR_ENGULFING"] | frame["BEAR_REJECTION"]

    # Pullback to EMA20: candle trades into/near EMA20 and closes back in trend
    # direction. The 0.35 ATR tolerance makes the concept scale across markets.
    tol = frame["ATR14"] * 0.35
    frame["BULL_TREND_PULLBACK"] = (
        (frame["Low"] <= frame["EMA20"] + tol)
        & (frame["Close"] > frame["EMA20"])
    )
    frame["BEAR_TREND_PULLBACK"] = (
        (frame["High"] >= frame["EMA20"] - tol)
        & (frame["Close"] < frame["EMA20"])
    )

    # Range location / exhaustion.
    sr_tol = frame["ATR14"] * 0.45
    frame["NEAR_SUPPORT"] = frame["Low"] <= frame["SUPPORT20"] + sr_tol
    frame["NEAR_RESISTANCE"] = frame["High"] >= frame["RESISTANCE20"] - sr_tol
    frame["BOLLINGER_LOW_EXTREME"] = (
        (frame["Low"] <= frame["BB_LOWER"])
        | (frame["BB_Z"] <= -1.35)
    )
    frame["BOLLINGER_HIGH_EXTREME"] = (
        (frame["High"] >= frame["BB_UPPER"])
        | (frame["BB_Z"] >= 1.35)
    )
    frame["BULL_EXHAUSTION"] = (
        (frame["RSI14"] <= 38.0)
        | frame["BOLLINGER_LOW_EXTREME"]
    )
    frame["BEAR_EXHAUSTION"] = (
        (frame["RSI14"] >= 62.0)
        | frame["BOLLINGER_HIGH_EXTREME"]
    )

    bull_div, bear_div = _detect_rsi_divergence(frame)
    frame["RSI_BULL_DIVERGENCE"] = bull_div
    frame["RSI_BEAR_DIVERGENCE"] = bear_div

    # Existing Jasong Rule+ML probability is preserved. We do not fabricate AI.
    up_col = None
    for candidate in (
        "UP_PROB",
        "combined_up_probability",
        "AI_UP",
        "up_probability",
        "JASONG_UP_PROB",
    ):
        if candidate in frame.columns:
            up_col = candidate
            break
    frame["JASONG_UP_PROB"] = (
        frame[up_col].map(_confidence01)
        if up_col is not None
        else 0.0
    )

    return frame.replace(
        [float("inf"), float("-inf")],
        float("nan"),
    )


def _directional_ai(row: pd.Series, direction: str) -> float:
    up = _confidence01(row.get("JASONG_UP_PROB"))
    if direction == "BUY":
        return up
    if direction == "SELL":
        return 1.0 - up
    return 0.0


def _market_quality(
    row: pd.Series,
    category: str,
) -> Dict[str, Any]:
    rule = CATEGORY_RULES[category]
    atr_pct = _safe_float(row.get("ATR_PCT"))
    q95 = _safe_float(row.get("ATR_Q95"), atr_pct)
    rel_volume = _safe_float(row.get("REL_VOLUME"), 1.0)

    volatility_floor = atr_pct >= float(rule["min_atr_pct"])
    panic_ceiling = True
    if q95 > 0:
        panic_ceiling = (
            atr_pct
            <= q95 * float(rule["max_atr_q95_multiplier"])
        )

    liquidity_pass = rel_volume >= float(rule["min_rel_volume"])

    return {
        "volatility_pass": bool(volatility_floor and panic_ceiling),
        "volatility_floor_pass": bool(volatility_floor),
        "panic_volatility_pass": bool(panic_ceiling),
        "liquidity_pass": bool(liquidity_pass),
        "atr_pct": round(atr_pct, 6),
        "atr_q95_pct": round(q95, 6),
        "relative_volume": round(rel_volume, 6),
    }


def _regime(row: pd.Series) -> str:
    adx = _safe_float(row.get("ADX14"))
    atr_pct = _safe_float(row.get("ATR_PCT"))
    q95 = _safe_float(row.get("ATR_Q95"), atr_pct)

    # Panic comes before trend/range so extreme volatility cannot be mistaken for
    # a high-quality trend simply because ADX is elevated.
    if q95 > 0 and atr_pct > q95 * 1.45:
        return "PANIC_VOLATILITY"
    if adx > TREND_ADX_MIN:
        return "TRENDING"
    return "RANGING"


def _trend_direction(
    row: pd.Series,
    category: str,
) -> Tuple[str, float, Dict[str, Any]]:
    """Exact TRENDING branch:
    EMA20/EMA50 + ADX > 25 + pullback + CLOSED candle confirmation.
    Category-specific VWAP/liquidity confirmation can strengthen the setup.
    """
    adx_pass = _safe_float(row.get("ADX14")) > TREND_ADX_MIN

    bull_ema = bool(row.get("EMA20") > row.get("EMA50"))
    bear_ema = bool(row.get("EMA20") < row.get("EMA50"))

    bull_pullback = bool(row.get("BULL_TREND_PULLBACK"))
    bear_pullback = bool(row.get("BEAR_TREND_PULLBACK"))

    # A trend confirmation candle must CLOSE in the same direction and close
    # beyond EMA20. This is not an intrabar trigger.
    bull_close = bool(
        row.get("BULL_CLOSE")
        and row.get("Close") > row.get("EMA20")
    )
    bear_close = bool(
        row.get("BEAR_CLOSE")
        and row.get("Close") < row.get("EMA20")
    )

    requires_vwap = bool(
        CATEGORY_RULES[category].get("trend_requires_vwap")
    )
    bull_vwap = bool(row.get("Close") >= row.get("SESSION_VWAP"))
    bear_vwap = bool(row.get("Close") <= row.get("SESSION_VWAP"))

    bull_momentum = _safe_float(row.get("RET4")) > 0
    bear_momentum = _safe_float(row.get("RET4")) < 0

    bull_mandatory = (
        adx_pass
        and bull_ema
        and bull_pullback
        and bull_close
        and (bull_vwap if requires_vwap else True)
    )
    bear_mandatory = (
        adx_pass
        and bear_ema
        and bear_pullback
        and bear_close
        and (bear_vwap if requires_vwap else True)
    )

    details = {
        "adx_gt_25": adx_pass,
        "ema20_gt_ema50": bull_ema,
        "ema20_lt_ema50": bear_ema,
        "bull_pullback": bull_pullback,
        "bear_pullback": bear_pullback,
        "bull_closed_candle_confirmation": bull_close,
        "bear_closed_candle_confirmation": bear_close,
        "bull_vwap_alignment": bull_vwap,
        "bear_vwap_alignment": bear_vwap,
        "vwap_mandatory": requires_vwap,
        "bull_momentum": bull_momentum,
        "bear_momentum": bear_momentum,
    }

    if bull_mandatory and not bear_mandatory:
        # Quant is live technical completeness, not historical WR.
        quality = 0.66
        quality += 0.08 if bull_momentum else 0.0
        quality += 0.06 if row.get("EMA50") > row.get("EMA200") else 0.0
        quality += 0.05 if bool(row.get("BULL_ENGULFING")) else 0.0
        return "BUY", min(0.95, quality), details

    if bear_mandatory and not bull_mandatory:
        quality = 0.66
        quality += 0.08 if bear_momentum else 0.0
        quality += 0.06 if row.get("EMA50") < row.get("EMA200") else 0.0
        quality += 0.05 if bool(row.get("BEAR_ENGULFING")) else 0.0
        return "SELL", min(0.95, quality), details

    return "WAIT", 0.0, details


def _range_direction(
    row: pd.Series,
    category: str,
) -> Tuple[str, float, Dict[str, Any]]:
    """Exact RANGING branch:
    RSI divergence + Bollinger/S&R + exhaustion + reversal CLOSED candle.
    """
    adx_range = _safe_float(row.get("ADX14")) <= TREND_ADX_MIN

    bull_div = bool(row.get("RSI_BULL_DIVERGENCE"))
    bear_div = bool(row.get("RSI_BEAR_DIVERGENCE"))

    bull_location = bool(
        row.get("BOLLINGER_LOW_EXTREME")
        or row.get("NEAR_SUPPORT")
    )
    bear_location = bool(
        row.get("BOLLINGER_HIGH_EXTREME")
        or row.get("NEAR_RESISTANCE")
    )

    bull_exhaustion = bool(row.get("BULL_EXHAUSTION"))
    bear_exhaustion = bool(row.get("BEAR_EXHAUSTION"))

    bull_reversal = bool(row.get("BULL_REVERSAL_CANDLE"))
    bear_reversal = bool(row.get("BEAR_REVERSAL_CANDLE"))

    bull_mandatory = (
        adx_range
        and bull_div
        and bull_location
        and bull_exhaustion
        and bull_reversal
    )
    bear_mandatory = (
        adx_range
        and bear_div
        and bear_location
        and bear_exhaustion
        and bear_reversal
    )

    # Category-specific context is additive, not a substitute for the agreed
    # mandatory range conditions.
    bull_vwap_reclaim = bool(row.get("Close") >= row.get("SESSION_VWAP"))
    bear_vwap_reject = bool(row.get("Close") <= row.get("SESSION_VWAP"))

    details = {
        "adx_range_le_25": adx_range,
        "rsi_bull_divergence": bull_div,
        "rsi_bear_divergence": bear_div,
        "bull_bollinger_or_support": bull_location,
        "bear_bollinger_or_resistance": bear_location,
        "bull_exhaustion": bull_exhaustion,
        "bear_exhaustion": bear_exhaustion,
        "bull_reversal_candle": bull_reversal,
        "bear_reversal_candle": bear_reversal,
        "bull_vwap_reclaim": bull_vwap_reclaim,
        "bear_vwap_reject": bear_vwap_reject,
    }

    if bull_mandatory and not bear_mandatory:
        quality = 0.72
        quality += 0.05 if bull_vwap_reclaim else 0.0
        quality += 0.05 if bool(row.get("BULL_ENGULFING")) else 0.0
        return "BUY", min(0.95, quality), details

    if bear_mandatory and not bull_mandatory:
        quality = 0.72
        quality += 0.05 if bear_vwap_reject else 0.0
        quality += 0.05 if bool(row.get("BEAR_ENGULFING")) else 0.0
        return "SELL", min(0.95, quality), details

    return "WAIT", 0.0, details


def _live_router(
    row: pd.Series,
    category: str,
) -> Dict[str, Any]:
    regime = _regime(row)
    quality = _market_quality(row, category)

    if regime == "PANIC_VOLATILITY":
        return {
            "direction": "WAIT",
            "quant_confidence": 0.0,
            "regime": regime,
            "strategy_branch": "NO_TRADE",
            "strategy_reason": "Panic volatility blocks new entries.",
            "branch_checks": {},
            **quality,
        }

    if regime == "TRENDING":
        direction, quant, checks = _trend_direction(row, category)
        branch = "TRENDING"
        reason = (
            "EMA20/EMA50 + ADX>25 + pullback + closed-candle confirmation"
        )
    else:
        direction, quant, checks = _range_direction(row, category)
        branch = "RANGING"
        reason = (
            "RSI divergence + Bollinger/S&R + exhaustion + reversal closed candle"
        )

    # VOLATILITY + LIQUIDITY is a mandatory layer after the regime strategy.
    if direction in {"BUY", "SELL"}:
        if not quality["volatility_pass"]:
            direction = "WAIT"
            quant = min(0.27, quant * 0.35)
            reason += " | blocked: volatility quality"
        elif not quality["liquidity_pass"]:
            direction = "WAIT"
            quant = min(0.27, quant * 0.35)
            reason += " | blocked: liquidity quality"

    return {
        "direction": direction,
        "quant_confidence": round(quant, 6),
        "regime": regime,
        "strategy_branch": branch,
        "strategy_reason": reason,
        "branch_checks": checks,
        **quality,
    }


def _closed_candle_position(frame: pd.DataFrame) -> int:
    """Always analyse a completed candle, never the still-forming last row."""
    if len(frame) < 2:
        raise ValueError("At least two candles required")
    return len(frame) - 2


def _live_fast_score(
    quant: float,
    ai: float,
    row: pd.Series,
    quality: Dict[str, Any],
) -> float:
    """Technical preflight score only; prime_policy recalculates authoritative FAST."""
    momentum = min(
        1.0,
        abs(_safe_float(row.get("RET4")))
        / max(1e-9, 2.0 * abs(_safe_float(row.get("VOL20"), 0.001))),
    )
    score = 100.0 * (
        0.40 * max(0.0, min(1.0, quant / 0.75))
        + 0.35 * max(0.0, min(1.0, ai / 0.75))
        + 0.10 * momentum
        + 0.075 * (1.0 if quality.get("volatility_pass") else 0.0)
        + 0.075 * (1.0 if quality.get("liquidity_pass") else 0.0)
    )
    return round(max(0.0, min(100.0, score)), 2)


class CategoryStrategyEngine:
    """FX and XAUUSD execution engine with the six-category API preserved.

    Gold and liquid currency pairs use closed-candle, multi-timeframe
    liquidity/structure rules. Other catalogue markets remain analysis-only.
    """

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
        self.scan_interval_seconds = max(
            90,
            int(
                scan_interval_seconds
                or os.getenv("CATEGORY_SCAN_INTERVAL_SECONDS", "180")
            ),
        )
        self.eligibility_refresh_seconds = max(
            15,
            int(os.getenv("CATEGORY_ELIGIBILITY_REFRESH_SECONDS", "15")),
        )
        self.batch_size = max(
            3,
            min(
                18,
                int(
                    batch_size
                    or os.getenv("CATEGORY_SCAN_BATCH_SIZE", "6")
                ),
            ),
        )
        self.candidate_ttl_seconds = max(
            300,
            int(os.getenv("CATEGORY_CANDIDATE_TTL_SECONDS", "1800")),
        )
        self.auto_full_refresh = str(
            os.getenv("CATEGORY_AUTO_FULL_REFRESH", "true")
        ).lower().strip() in {"1", "true", "yes", "on"}

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
            "offset_by_category": {
                category: 0 for category in CATEGORY_ORDER
            },
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
                "total": len(_active_market_seeds()),
                "current_key": None,
                "errors": 0,
                "last_error": None,
            },
        }

    def _is_current_evaluation(self, row: Any) -> bool:
        return bool(
            isinstance(row, dict)
            and str(row.get("version") or "") == self.VERSION
            and int(row.get("evidence_schema_version") or 0)
            == EVIDENCE_SCHEMA_VERSION
            and row.get("current_candle_strategy_complete") is True
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
                    state["legacy_rows_excluded"] = (
                        int(raw.get("legacy_rows_excluded") or 0)
                        + excluded
                    )
                    if (
                        excluded
                        or source_version != self.VERSION
                        or len(current) < len(_active_market_seeds())
                    ):
                        state["migration_at"] = time.time()
                        state["full_refresh"]["processed"] = len(current)
                    else:
                        state["full_refresh"] = dict(
                            raw.get("full_refresh")
                            or state["full_refresh"]
                        )
        except Exception as exc:
            state["last_error"] = (
                f"state_load: {type(exc).__name__}: {exc}"
            )

        state["version"] = self.VERSION
        state["evidence_schema_version"] = EVIDENCE_SCHEMA_VERSION
        return state

    def _persist(self) -> None:
        try:
            path = Path(self.state_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = str(path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(
                    self._state,
                    fh,
                    separators=(",", ":"),
                    default=str,
                )
            os.replace(tmp, self.state_path)
        except Exception as exc:
            self._state["last_error"] = (
                f"persist: {type(exc).__name__}: {exc}"
            )

    def universe(
        self,
        category: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        rows = CATEGORY_MARKET_SEEDS
        if category:
            clean = str(category).upper().strip()
            rows = [
                row for row in rows
                if row["category"] == clean
            ]
        active = set(ACTIVE_EXECUTION_KEYS)
        return [
            {
                **dict(row),
                "execution_active": str(row.get("key") or "").upper() in active,
                "execution_policy": (
                    CATEGORY_RULES[str(row.get("category"))]["strategy_id"]
                    if str(row.get("key") or "").upper() in active
                    else "ANALYSIS_ONLY_RETIRED_ENTRY_STRATEGY"
                ),
            }
            for row in rows
        ]

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
        universe_keys = [
            str(seed["key"]) for seed in _active_market_seeds()
        ]
        completed = [
            key for key in universe_keys if key in current
        ]
        pending = [
            key for key in universe_keys if key not in current
        ]
        return {
            "version": self.VERSION,
            "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
            "markets_total": len(universe_keys),
            "markets_optimised": len(completed),
            "markets_pending_optimisation": len(pending),
            "optimiser_complete": len(pending) == 0,
            "current_candle_strategy_complete": len(pending) == 0,
            "completed_keys": completed,
            "pending_keys": pending,
            "active_execution_keys": list(ACTIVE_EXECUTION_KEYS),
            "retired_market_count": max(
                0,
                len(CATEGORY_MARKET_SEEDS) - len(universe_keys),
            ),
            "legacy_rows_excluded": int(
                self._state.get("legacy_rows_excluded") or 0
            ),
            "historical_execution_veto": False,
            "live_money_execution": False,
        }

    def _next_batch(
        self,
        category: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            requested_categories = (
                [str(category).upper().strip()]
                if category
                else list(CATEGORY_ORDER)
            )
            active_seeds = _active_market_seeds()
            pools = {
                cat: [row for row in active_seeds if row["category"] == cat]
                for cat in requested_categories
                if cat in CATEGORY_ORDER
            }
            pools = {cat: rows for cat, rows in pools.items() if rows}
            categories = list(pools)
            if not categories:
                return []

            batch: List[Dict[str, Any]] = []
            offsets = self._state.setdefault(
                "offset_by_category",
                {},
            )
            taken = {cat: 0 for cat in categories}
            limit = self.batch_size
            while len(batch) < limit:
                progressed = False
                for cat in categories:
                    pool = pools[cat]
                    if taken[cat] >= len(pool) or len(batch) >= limit:
                        continue
                    offset = int(offsets.get(cat) or 0) % len(pool)
                    batch.append(dict(pool[(offset + taken[cat]) % len(pool)]))
                    taken[cat] += 1
                    progressed = True
                if not progressed:
                    break
            for cat, count in taken.items():
                offsets[cat] = (int(offsets.get(cat) or 0) + count) % len(pools[cat])

            self._state["last_batch_keys"] = [
                row["key"] for row in batch
            ]
            return batch

    def _resolve_execution_market(
        self,
        seed: Dict[str, Any],
    ) -> Dict[str, Any]:
        if (
            seed.get("category") == "FOREX"
            and seed.get("ig_symbol")
        ):
            market = self.broker.resolve_market(
                str(seed["ig_symbol"]),
                require_tradeable=False,
            )
            details = market.get("details") or {}
            snapshot = details.get("snapshot") or {}
            instrument = details.get("instrument") or {}
            return {
                **market,
                "epic": market.get("epic"),
                "name": market.get("name")
                or instrument.get("name"),
                "instrument_type":
                    market.get("instrument_type")
                    or instrument.get("type"),
                "market_status":
                    market.get("market_status")
                    or snapshot.get("marketStatus"),
                "min_deal_size": getattr(
                    self.broker,
                    "_min_deal_size",
                    lambda _: 0.0,
                )(details),
                "expiry": market.get("expiry")
                or instrument.get("expiry"),
                "bid": snapshot.get("bid"),
                "offer": snapshot.get("offer"),
                "details": details,
            }

        return self.broker.resolve_global_market(
            search_terms=list(
                seed.get("ig_search_terms")
                or [seed.get("name")]
            ),
            expected_types=list(
                seed.get("expected_types") or []
            ),
            name_tokens=list(seed.get("name_tokens") or []),
            require_tradeable=False,
            cache_key=str(
                seed.get("key")
                or seed.get("name")
                or ""
            ),
        )

    def _resolve_bid_offer(
        self,
        seed: Dict[str, Any],
        market: Dict[str, Any],
    ) -> Tuple[Optional[float], Optional[float], str]:
        def pair(
            raw_bid: Any,
            raw_offer: Any,
            source: str,
        ):
            bid = _safe_float(raw_bid)
            offer = _safe_float(raw_offer)
            if bid > 0.0 and offer >= bid:
                return bid, offer, source
            return None

        details = market.get("details") or {}
        snapshot = details.get("snapshot") or {}

        for candidate in (
            pair(
                market.get("bid"),
                market.get("offer"),
                "RESOLVER_TOP_LEVEL",
            ),
            pair(
                snapshot.get("bid"),
                snapshot.get("offer"),
                "RESOLVER_DETAILS_SNAPSHOT",
            ),
        ):
            if candidate:
                return candidate

        epic = str(market.get("epic") or "").strip()

        if epic and hasattr(self.broker, "market_details"):
            try:
                refreshed = (
                    self.broker.market_details(epic) or {}
                )
                snap = refreshed.get("snapshot") or {}
                candidate = pair(
                    snap.get("bid"),
                    snap.get("offer"),
                    "REFRESHED_EPIC_SNAPSHOT",
                )
                if candidate:
                    return candidate
            except Exception:
                pass

        if epic and hasattr(self.broker, "search_markets"):
            terms = list(
                seed.get("ig_search_terms")
                or [seed.get("name")]
            )
            for term in terms[:2]:
                if not str(term or "").strip():
                    continue
                try:
                    response = (
                        self.broker.search_markets(str(term))
                        or {}
                    )
                    for raw in response.get("markets", []) or []:
                        if not isinstance(raw, dict):
                            continue
                        if str(raw.get("epic") or "").strip() != epic:
                            continue
                        candidate = pair(
                            raw.get("bid"),
                            raw.get("offer"),
                            "EXACT_EPIC_MARKET_SEARCH",
                        )
                        if candidate:
                            return candidate
                except Exception:
                    continue

        return None, None, "UNAVAILABLE"

    def _evaluate_seed(
        self,
        seed: Dict[str, Any],
    ) -> Dict[str, Any]:
        category = str(seed.get("category") or "").upper()
        if category not in CATEGORY_RULES:
            raise ValueError(f"Unsupported category: {category}")

        key = str(seed.get("key") or "").upper().strip()
        if key not in set(ACTIVE_EXECUTION_KEYS):
            return {
                **seed,
                "version": self.VERSION,
                "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
                "current_candle_strategy_complete": True,
                "optimizer_complete": True,
                "walk_forward_complete": True,
                "market": seed.get("name"),
                "symbol": seed.get("key"),
                "strategy_id": "RETIRED_ENTRY_STRATEGY",
                "strategy_name": "Analysis Only — Autonomous Entry Retired",
                "strategy_definition_version": self.VERSION,
                "strategy_selection_mode": "ANALYSIS_ONLY",
                "direction": "WAIT",
                "live_direction": "WAIT",
                "quant_confidence": 0.0,
                "model_ai_confidence": 0.0,
                "smart_fast_score": 0.0,
                "standard_eligible": False,
                "trade_eligible": False,
                "compound_eligible": False,
                "rejection_reasons": ["OLD_ENTRY_STRATEGY_RETIRED"],
                "evaluated_at": time.time(),
                "live_money_execution": False,
            }

        raw = self.frame_func(seed)
        frame = _feature_frame(raw)
        closed_pos = _closed_candle_position(frame)
        latest = frame.iloc[closed_pos]
        latest_timestamp = frame.index[closed_pos]

        rule = CATEGORY_RULES[category]
        completed = frame.iloc[:closed_pos + 1].copy()
        is_forex = category == "FOREX"
        analysis = (
            analyze_forex(completed, key)
            if is_forex
            else analyze_xauusd(completed)
        )
        quality = _market_quality(latest, category)
        selected_checks = dict(
            (analysis.get("selected_setup") or {}).get("checks") or {}
        )
        live = {
            "direction": analysis.get("direction") or "WAIT",
            "quant_confidence": analysis.get("quant_confidence") or 0.0,
            "regime": (
                (analysis.get("h4_structure") or {}).get("trend")
                or "NEUTRAL"
            ),
            "strategy_branch": analysis.get("strategy_branch"),
            "strategy_reason": analysis.get("strategy_reason"),
            "branch_checks": selected_checks,
            "volatility_pass": quality["volatility_pass"],
            "volatility_floor_pass": quality["volatility_floor_pass"],
            "panic_volatility_pass": quality["panic_volatility_pass"],
            "liquidity_pass": bool(selected_checks.get("liquidity_sweep")),
            "atr_pct": quality["atr_pct"],
            "atr_q95_pct": quality["atr_q95_pct"],
            "relative_volume": quality["relative_volume"],
        }
        direction = str(live["direction"])
        quant = _confidence01(live["quant_confidence"])
        legacy_rule_ml_ai = _directional_ai(latest, direction)
        ai = (
            _confidence01(analysis.get("directional_confidence"))
            if direction in {"BUY", "SELL"}
            else 0.0
        )

        quant_pass = (
            direction in {"BUY", "SELL"}
            and quant >= QUANT_MIN_CONFIDENCE
        )
        ai_pass = (
            direction in {"BUY", "SELL"}
            and ai >= MODEL_AI_MIN_CONFIDENCE
        )

        technical_fast = _live_fast_score(
            quant,
            ai,
            latest,
            live,
        )
        fast_pass = (
            direction in {"BUY", "SELL"}
            and technical_fast >= STANDARD_FAST_SCORE_MIN
        )

        row: Dict[str, Any] = {
            **seed,
            "version": self.VERSION,
            "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
            "current_candle_strategy_complete": True,
            # Backward-compatible completion flags.
            "optimizer_complete": True,
            "walk_forward_complete": True,
            "market": seed.get("name"),
            "symbol": seed.get("key"),
            "strategy_id": rule["strategy_id"],
            "strategy_name": rule["strategy_name"],
            "strategy_definition_version": self.VERSION,
            "strategy_selection_mode":
                (
                    "FX_MULTI_SESSION_LIQUIDITY_LINES"
                    if is_forex
                    else "XAUUSD_MULTI_TIMEFRAME_LIQUIDITY_STRUCTURE"
                ),
            "historical_validation_mode": "INFORMATIONAL_ONLY",
            "historical_execution_veto": False,
            "regime": live["regime"],
            "strategy_branch": live["strategy_branch"],
            "direction": direction,
            "live_direction": direction,
            "quant_confidence": round(quant, 6),
            "quant_confidence_pct": round(quant * 100.0, 2),
            "model_ai_confidence": round(ai, 6),
            "model_ai_directional_confidence_pct": round(
                ai * 100.0,
                2,
            ),
            "model_ai_confidence_source":
                (
                    "FX_RULE_CONFLUENCE_NOT_LEGACY_ML"
                    if is_forex
                    else "XAUUSD_RULE_CONFLUENCE_NOT_LEGACY_ML"
                ),
            "legacy_rule_ml_directional_confidence": round(
                legacy_rule_ml_ai,
                6,
            ),
            "ai28_pass": quant_pass,
            "ai40_pass": ai_pass,
            "smart_fast_score": technical_fast,
            "required_fast_score": STANDARD_FAST_SCORE_MIN,
            "fast_pass": fast_pass,
            "strategy_reason": live["strategy_reason"],
            "signal_reason": live["strategy_reason"],
            "regime_checks": live["branch_checks"],
            "volatility_pass": live["volatility_pass"],
            "volatility_floor_pass":
                live["volatility_floor_pass"],
            "panic_volatility_pass":
                live["panic_volatility_pass"],
            "liquidity_pass": live["liquidity_pass"],
            "atr_pct": live["atr_pct"],
            "atr_q95_pct": live["atr_q95_pct"],
            "relative_volume": live["relative_volume"],
            "live_price": round(
                _safe_float(latest.get("Close")),
                10,
            ),
            "rsi": round(
                _safe_float(latest.get("RSI14"), 50.0),
                2,
            ),
            "adx": round(
                _safe_float(latest.get("ADX14")),
                2,
            ),
            "ema20": round(
                _safe_float(latest.get("EMA20")),
                10,
            ),
            "ema50": round(
                _safe_float(latest.get("EMA50")),
                10,
            ),
            "ema200": round(
                _safe_float(latest.get("EMA200")),
                10,
            ),
            "support20": round(
                _safe_float(latest.get("SUPPORT20")),
                10,
            ),
            "resistance20": round(
                _safe_float(latest.get("RESISTANCE20")),
                10,
            ),
            "rsi_bull_divergence": bool(
                latest.get("RSI_BULL_DIVERGENCE")
            ),
            "rsi_bear_divergence": bool(
                latest.get("RSI_BEAR_DIVERGENCE")
            ),
            "bull_reversal_candle": bool(
                latest.get("BULL_REVERSAL_CANDLE")
            ),
            "bear_reversal_candle": bool(
                latest.get("BEAR_REVERSAL_CANDLE")
            ),
            "closed_candle_timestamp": (
                analysis.get("closed_candle_timestamp")
                or (
                    latest_timestamp.isoformat()
                    if hasattr(latest_timestamp, "isoformat")
                    else str(latest_timestamp)
                )
            ),
            "closed_candle_index_offset": -2,
            "forming_candle_ignored": True,
            "holding_bars": int(rule["holding_bars"]),
            "analysis_source":
                (
                    "FOREX_M15_H1_H4_LIQUIDITY_LINES"
                    if is_forex
                    else "XAUUSD_M15_H1_H4_LIQUIDITY_STRUCTURE"
                ),
            "analysis_execution_price_basis": (
                "ROUTED_CLOSED_CANDLE_STRUCTURE_DISTANCE_TRANSFERRED_TO_"
                "FRESH_IG_DEMO_ENTRY_QUOTE"
            ),
            "strategy_module_version": (
                FOREX_STRATEGY_VERSION if is_forex else XAUUSD_STRATEGY_VERSION
            ),
            "forex_strategy_version": FOREX_STRATEGY_VERSION if is_forex else None,
            "forex_strategy": analysis if is_forex else None,
            "xauusd_strategy_version": XAUUSD_STRATEGY_VERSION if not is_forex else None,
            "xauusd_strategy": analysis if not is_forex else None,
            "candlestick_analyzed": bool(
                analysis.get("candlestick_analyzed", True)
            ),
            "news_guard": dict(analysis.get("news_guard") or {}),
            "setup_id": analysis.get("setup_id"),
            "structural_stop_price": analysis.get("structural_stop_price"),
            "structural_stop_distance": analysis.get("structural_stop_distance"),
            "take_profit_target_price": analysis.get("take_profit_target_price"),
            "target_distance": analysis.get("target_distance"),
            "target_r": analysis.get("target_r"),
            "room_to_opposing_liquidity_r":
                analysis.get("room_to_opposing_liquidity_r"),
            "session": dict(analysis.get("session") or {}),
            "session_name":
                (analysis.get("session") or {}).get("name"),
            "session_active": bool(
                (analysis.get("session") or {}).get("active")
            ),
            "london_new_york_overlap": bool(
                (analysis.get("session") or {}).get(
                    "london_new_york_overlap",
                    (analysis.get("session") or {}).get("overlap"),
                )
            ),
            "south_africa_time":
                (analysis.get("session") or {}).get("south_africa_local"),
            "session_exit_at": analysis.get("session_exit_at"),
            "max_hold_seconds": analysis.get("max_hold_seconds"),
            "recent_returns": [
                round(_safe_float(value), 10)
                for value in frame["RET1"]
                .dropna()
                .iloc[:closed_pos + 1]
                .tail(160)
                .tolist()
            ],
            "evaluated_at": time.time(),
            # Historical fields stay present for API compatibility, but they are
            # explicitly not used to choose or veto this current trade.
            "historical_win_rate": None,
            "historical_win_rate_pct": None,
            "historical_profit_factor": None,
            "historical_trades": None,
            "historical_max_drawdown_pct": None,
            "historical_target_verified": None,
            "walk_forward_pass": None,
            "quality_tier": (
                "A+" if quant >= 0.75 and ai >= 0.60
                else "A" if direction in {"BUY", "SELL"}
                else None
            ),
            "deep_status": (
                "VERIFIED"
                if direction in {"BUY", "SELL"}
                else "OBSERVE"
            ),
            "ig_tradeable": False,
            "ig_epic": None,
            "ig_market_status": None,
            "ig_spread_bps": None,
            "ig_bid": None,
            "ig_offer": None,
            "ig_quote_source": "NOT_REQUESTED",
            "standard_eligible": False,
            "trade_eligible": False,
            "compound_slot_candidate": False,
            "compound_eligible": False,
            "live_money_execution": False,
        }

        # IG preflight occurs only after the full current-candle technical path
        # qualifies. This preserves the intended sequence.
        technical_candidate = bool(
            direction in {"BUY", "SELL"}
            and quant_pass
            and ai_pass
            and fast_pass
            and live["volatility_pass"]
            and live["liquidity_pass"]
        )

        if technical_candidate:
            try:
                market = self._resolve_execution_market(seed)
                row["ig_epic"] = market.get("epic")
                row["ig_market_name"] = market.get("name")
                row["ig_instrument_type"] = (
                    market.get("instrument_type")
                )
                row["ig_market_status"] = (
                    market.get("market_status")
                )
                row["ig_tradeable"] = (
                    str(
                        market.get("market_status")
                        or ""
                    ).upper()
                    == "TRADEABLE"
                )
                row["ig_min_deal_size"] = (
                    market.get("min_deal_size")
                )
                row["ig_expiry"] = market.get("expiry")

                bid, offer, quote_source = (
                    self._resolve_bid_offer(seed, market)
                )
                row["ig_bid"] = bid
                row["ig_offer"] = offer
                row["ig_quote_source"] = quote_source

                if (
                    bid is not None
                    and offer is not None
                ):
                    mid = (bid + offer) / 2.0
                    row["ig_spread_bps"] = (
                        round(
                            (offer - bid)
                            / mid
                            * 10000.0,
                            4,
                        )
                        if mid > 0
                        else None
                    )
            except Exception as exc:
                row["ig_preflight_error"] = (
                    f"{type(exc).__name__}: {exc}"
                )

        spread = row.get("ig_spread_bps")
        spread_limit = float(rule["spread_gate_bps"])
        spread_pass = bool(
            spread is not None
            and _safe_float(spread, 1e9) <= spread_limit
        )
        row["spread_gate_bps"] = spread_limit
        row["spread_limit_bps"] = spread_limit
        row["spread_bps"] = spread
        row["spread_pass"] = spread_pass

        rejection_reasons: List[str] = list(
            analysis.get("rejection_reasons") or []
        )
        if not live["panic_volatility_pass"]:
            rejection_reasons.append("PANIC_VOLATILITY")
        if direction not in {"BUY", "SELL"}:
            rejection_reasons.append(
                (
                    "FX_FULL_CONFLUENCE_NOT_CONFIRMED"
                    if is_forex
                    else "XAUUSD_FULL_CONFLUENCE_NOT_CONFIRMED"
                )
            )
        if not live["volatility_pass"]:
            rejection_reasons.append(
                "VOLATILITY_GATE_FAIL"
            )
        if not live["liquidity_pass"]:
            rejection_reasons.append(
                "LIQUIDITY_GATE_FAIL"
            )
        if not quant_pass:
            rejection_reasons.append("QUANT_BELOW_28")
        if not ai_pass:
            rejection_reasons.append(
                "MODEL_AI_BELOW_40"
            )
        if not fast_pass:
            rejection_reasons.append("FAST_BELOW_45")
        if technical_candidate and not row["ig_tradeable"]:
            rejection_reasons.append("IG_NOT_TRADEABLE")
        if technical_candidate and not spread_pass:
            rejection_reasons.append("SPREAD_GATE_FAIL")
            if spread is None:
                rejection_reasons.append(
                    "SPREAD_QUOTE_UNAVAILABLE"
                )
            elif _safe_float(spread, 1e9) > spread_limit:
                rejection_reasons.append("SPREAD_TOO_WIDE")

        row["rejection_reasons"] = list(
            dict.fromkeys(rejection_reasons)
        )

        # This standard eligibility is later re-evaluated by ForwardPrimeArchitecture
        # with live FAST/freshness/provenance. Historical statistics are absent.
        row["standard_eligible"] = bool(
            technical_candidate
            and row["ig_tradeable"]
            and spread_pass
        )
        row["trade_eligible"] = row["standard_eligible"]
        row["confidence_qualified"] = bool(
            quant_pass and ai_pass
        )
        row["direction_match"] = (
            direction in {"BUY", "SELL"}
        )
        row["intelligence_source"] = (
            rule["strategy_id"]
        )

        # Rank only on current evidence. No historical WR/PF component.
        row["category_rank_score"] = round(
            0.35 * technical_fast
            + 0.30 * ai * 100.0
            + 0.25 * quant * 100.0
            + 5.0 * (
                1.0 if live["volatility_pass"] else 0.0
            )
            + 5.0 * (
                1.0 if live["liquidity_pass"] else 0.0
            ),
            3,
        )
        return row

    def run_now(
        self,
        category: Optional[str] = None,
    ) -> Dict[str, Any]:
        batch = self._next_batch(category)
        evaluations: Dict[str, Any] = {}
        last_error: Optional[str] = None

        for seed in batch:
            key = str(seed.get("key") or seed.get("name"))
            try:
                evaluations[key] = self._evaluate_seed(seed)
            except Exception as exc:
                last_error = (
                    f"{type(exc).__name__}: {exc}"
                )
                evaluations[key] = {
                    **seed,
                    "market": seed.get("name"),
                    "symbol": key,
                    "category": seed.get("category"),
                    "version": self.VERSION,
                    "evidence_schema_version":
                        EVIDENCE_SCHEMA_VERSION,
                    "current_candle_strategy_complete": False,
                    "optimizer_complete": False,
                    "walk_forward_complete": False,
                    "direction": "WAIT",
                    "standard_eligible": False,
                    "compound_eligible": False,
                    "evaluated_at": time.time(),
                    "reason": last_error,
                    "live_money_execution": False,
                }

        with self._lock:
            self._state.setdefault(
                "evaluations",
                {},
            ).update(evaluations)
            self._state["runs"] = int(
                self._state.get("runs") or 0
            ) + 1
            self._state["last_run_at"] = time.time()
            self._state["last_error"] = last_error
            self._persist()

        return self.status()

    def _fresh_rows(self) -> List[Dict[str, Any]]:
        now = time.time()
        return [
            row
            for row in self._current_evaluations().values()
            if (
                now
                - _safe_float(
                    row.get("evaluated_at"),
                    0.0,
                )
                <= self.candidate_ttl_seconds
            )
        ]

    def category_rankings(
        self,
        category: Optional[str] = None,
        top_n: int = TOP_N_PER_CATEGORY,
    ) -> Dict[str, List[Dict[str, Any]]]:
        categories = (
            [str(category).upper().strip()]
            if category
            else list(CATEGORY_ORDER)
        )
        result: Dict[str, List[Dict[str, Any]]] = {}

        fresh = self._fresh_rows()
        for cat in categories:
            if cat not in CATEGORY_ORDER:
                result[cat] = []
                continue

            rows = [
                dict(row)
                for row in fresh
                if str(
                    row.get("category") or ""
                ).upper()
                == cat
            ]
            rows.sort(
                key=lambda row: (
                    bool(row.get("standard_eligible")),
                    _safe_float(
                        row.get("category_rank_score")
                    ),
                    _safe_float(
                        row.get("smart_fast_score")
                    ),
                    _safe_float(
                        row.get("model_ai_confidence")
                    ),
                    _safe_float(
                        row.get("quant_confidence")
                    ),
                ),
                reverse=True,
            )

            ranked: List[Dict[str, Any]] = []
            for idx, row in enumerate(
                rows[:max(1, min(int(top_n), TOP_N_PER_CATEGORY))],
                start=1,
            ):
                row["category_rank"] = idx
                row["rank"] = idx
                row["source_rank"] = idx
                row["compound_slot_candidate"] = (
                    idx <= COMPOUND_SLOTS_PER_CATEGORY
                )
                # PRIME later overwrites the actual Compound eligibility.
                row["compound_eligible"] = False
                ranked.append(row)

            result[cat] = ranked

        return result

    def candidates(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for bucket in self.category_rankings().values():
            rows.extend(dict(row) for row in bucket)
        return rows

    def compound_candidates(self) -> List[Dict[str, Any]]:
        # ForwardPrimeArchitecture replaces this runtime method after startup.
        return [
            row
            for row in self.candidates()
            if row.get("compound_eligible")
        ]

    def opportunity_board(
        self,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        rows = self.candidates()
        rows.sort(
            key=lambda row: (
                bool(row.get("standard_eligible")),
                _safe_float(
                    row.get("category_rank_score")
                ),
            ),
            reverse=True,
        )
        return rows[:max(1, min(int(limit), 500))]

    def refresh_opportunity_board(self) -> Dict[str, Any]:
        rows = self.opportunity_board(limit=100)
        return {
            "version": self.VERSION,
            "count": len(rows),
            "eligibility_refresh_seconds":
                self.eligibility_refresh_seconds,
            "opportunities": rows,
            "live_money_execution": False,
        }

    def correlation_matrix(
        self,
    ) -> Dict[str, Dict[str, float]]:
        rows = self.candidates()
        series: Dict[str, List[float]] = {}

        for row in rows:
            values = row.get("recent_returns") or []
            key = str(
                row.get("symbol")
                or row.get("key")
                or ""
            ).upper().strip()
            if (
                key
                and isinstance(values, list)
                and len(values) >= 20
            ):
                series[key] = [
                    _safe_float(value)
                    for value in values
                ]

        keys = list(series)
        matrix: Dict[str, Dict[str, float]] = {
            key: {} for key in keys
        }

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
                cov = sum(
                    (x - ma) * (y - mb)
                    for x, y in zip(a, b)
                )
                va = sum((x - ma) ** 2 for x in a)
                vb = sum((y - mb) ** 2 for y in b)
                denom = math.sqrt(va * vb)
                matrix[left][right] = (
                    float(cov / denom)
                    if denom > 0
                    else 0.0
                )

        return matrix

    def optimizer_summary(self) -> Dict[str, Any]:
        """Backward-compatible endpoint: historical optimiser is no longer authority."""
        current = list(
            self._current_evaluations().values()
        )
        categories: Dict[str, Any] = {}

        for category in CATEGORY_ORDER:
            rows = [
                row for row in current
                if str(
                    row.get("category") or ""
                ).upper()
                == category
            ]
            rows.sort(
                key=lambda row: _safe_float(
                    row.get("category_rank_score")
                ),
                reverse=True,
            )
            best = rows[0] if rows else {}
            active_category = any(
                row.get("category") == category for row in _active_market_seeds()
            )
            selection_mode = (
                "FX_MULTI_SESSION_LIQUIDITY_LINES"
                if category == "FOREX"
                else "XAUUSD_MULTI_TIMEFRAME_LIQUIDITY_STRUCTURE"
            )
            categories[category] = {
                "market": best.get("market"),
                "symbol": best.get("symbol"),
                "selected_strategy_id":
                    (
                        CATEGORY_RULES[category]["strategy_id"]
                        if active_category
                        else "RETIRED_ENTRY_STRATEGY"
                    ),
                "selected_strategy_name":
                    (
                        CATEGORY_RULES[category]["strategy_name"]
                        if active_category
                        else "Analysis Only — Autonomous Entry Retired"
                    ),
                "selection_mode":
                    (
                        selection_mode
                        if active_category
                        else "ANALYSIS_ONLY_RETIRED_ENTRY_STRATEGY"
                    ),
                "regime": best.get("regime"),
                "strategy_branch":
                    best.get("strategy_branch"),
                "historical_optimizer_authority": False,
                "historical_execution_veto": False,
            }

        return {
            "version": self.VERSION,
            "method":
                "FX_AND_XAUUSD_MULTI_TIMEFRAME_LIQUIDITY_STRUCTURE",
            "current_candle_only": True,
            "forming_candle_ignored": True,
            "active_execution_markets": list(ACTIVE_EXECUTION_KEYS),
            "higher_timeframe_rule": "H4 HH/HL or LH/LL structure",
            "setup_rule": (
                "external/internal lines + premium/discount + liquidity sweep + "
                "BOS/CHoCH/CISD/MSS + OB/FVG retest + closed M15 candlestick"
            ),
            "session_rule": (
                "Pair geography: London/New York/Tokyo/Sydney local sessions, DST-aware"
            ),
            "risk_reward_rule": "minimum 1:2 structural R:R",
            "quant_min_pct": 28.0,
            "model_ai_min_pct": 40.0,
            "fast_score_min": 45.0,
            "historical_optimizer_authority": False,
            "historical_validation_mode":
                "INFORMATIONAL_ONLY",
            "prime_authority":
                "BROKER_SETTLED_FORWARD_ONLY",
            "coverage": self.evidence_coverage(),
            "categories": categories,
            "live_money_execution": False,
        }

    def full_refresh_status(self) -> Dict[str, Any]:
        with self._lock:
            out = dict(
                self._state.get("full_refresh")
                or {}
            )
        out["running"] = bool(
            self._full_refresh_thread
            and self._full_refresh_thread.is_alive()
        )
        out["coverage"] = self.evidence_coverage()
        out["live_money_execution"] = False
        return out

    def _run_full_refresh(self) -> None:
        errors = 0
        with self._refresh_lock:
            with self._lock:
                self._state["full_refresh"] = {
                    "status": "RUNNING",
                    "started_at": time.time(),
                    "completed_at": None,
                    "processed": 0,
                    "total": len(_active_market_seeds()),
                    "current_key": None,
                    "errors": 0,
                    "last_error": None,
                }
                self._persist()

            for idx, seed in enumerate(
                _active_market_seeds(),
                start=1,
            ):
                if self._stop.is_set():
                    break
                key = str(seed["key"])
                last_error = None
                try:
                    evaluation = self._evaluate_seed(
                        dict(seed)
                    )
                except Exception as exc:
                    errors += 1
                    last_error = (
                        f"{type(exc).__name__}: {exc}"
                    )
                    evaluation = {
                        **seed,
                        "market": seed.get("name"),
                        "symbol": key,
                        "version": self.VERSION,
                        "evidence_schema_version":
                            EVIDENCE_SCHEMA_VERSION,
                        "current_candle_strategy_complete":
                            False,
                        "direction": "WAIT",
                        "standard_eligible": False,
                        "compound_eligible": False,
                        "evaluated_at": time.time(),
                        "reason": last_error,
                        "live_money_execution": False,
                    }

                with self._lock:
                    self._state.setdefault(
                        "evaluations",
                        {},
                    )[key] = evaluation
                    refresh = self._state[
                        "full_refresh"
                    ]
                    refresh["processed"] = idx
                    refresh["current_key"] = key
                    refresh["errors"] = errors
                    refresh["last_error"] = last_error
                    self._persist()

            with self._lock:
                refresh = self._state[
                    "full_refresh"
                ]
                refresh["status"] = (
                    "COMPLETE"
                    if not self._stop.is_set()
                    else "STOPPED"
                )
                refresh["completed_at"] = time.time()
                refresh["current_key"] = None
                self._state["runs"] = int(
                    self._state.get("runs") or 0
                ) + 1
                self._state["last_run_at"] = time.time()
                self._state["last_error"] = (
                    refresh.get("last_error")
                )
                self._persist()

    def start_full_refresh(
        self,
        force: bool = False,
    ) -> Dict[str, Any]:
        with self._lock:
            running = bool(
                self._full_refresh_thread
                and self._full_refresh_thread.is_alive()
            )
            if running:
                return self.full_refresh_status()

            coverage = self.evidence_coverage()
            if (
                not force
                and coverage[
                    "markets_pending_optimisation"
                ] == 0
            ):
                return self.full_refresh_status()

            self._full_refresh_thread = threading.Thread(
                target=self._run_full_refresh,
                daemon=True,
                name="jasong-category-full-refresh",
            )
            self._full_refresh_thread.start()
            return self.full_refresh_status()

    def status(self) -> Dict[str, Any]:
        rankings = self.category_rankings()
        by_category: Dict[str, Any] = {}
        standard_ready = 0
        compound_ready = 0

        for category in CATEGORY_ORDER:
            rows = rankings.get(category, [])
            standard = sum(
                1 for row in rows
                if row.get("standard_eligible")
            )
            compound = sum(
                1 for row in rows
                if row.get("compound_eligible")
            )
            standard_ready += standard
            compound_ready += compound

            active_category = any(
                row.get("category") == category for row in _active_market_seeds()
            )
            selection_mode = (
                "FX_MULTI_SESSION_LIQUIDITY_LINES"
                if category == "FOREX"
                else "XAUUSD_MULTI_TIMEFRAME_LIQUIDITY_STRUCTURE"
            )
            by_category[category] = {
                "strategy":
                    (
                        CATEGORY_RULES[category]["strategy_name"]
                        if active_category
                        else "Analysis Only — Autonomous Entry Retired"
                    ),
                "strategy_id":
                    (
                        CATEGORY_RULES[category]["strategy_id"]
                        if active_category
                        else "RETIRED_ENTRY_STRATEGY"
                    ),
                "execution_active": active_category,
                "selection_mode":
                    (
                        selection_mode
                        if active_category
                        else "ANALYSIS_ONLY_RETIRED_ENTRY_STRATEGY"
                    ),
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
                evaluated_by_asset_class.get(
                    asset_class,
                    0,
                )
                + 1
            )

        with self._lock:
            return {
                "version": self.VERSION,
                "name":
                    "JASONG V6.11 FX + XAUUSD LIQUIDITY-STRUCTURE EXECUTION",
                "enabled": bool(
                    self._state.get("enabled", True)
                ),
                "strategy_sequence": {
                    "price_basis":
                        "CURRENT_CLOSED_CANDLE_ONLY",
                    "forming_candle_ignored": True,
                    "active_market": "28 LIQUID FOREX PAIRS + XAUUSD / GOLD",
                    "higher_timeframe": "H4 market structure",
                    "confirmation_timeframe": "H1 structure",
                    "entry_timeframe": "M15 closed candle",
                    "session_policy": (
                        "Pair-relevant London/New York/Tokyo/Sydney local "
                        "sessions; DST-aware"
                    ),
                    "setup": (
                        "external line + premium/discount -> liquidity sweep -> "
                        "BOS/CHoCH/CISD/MSS -> OB/FVG retest -> closed "
                        "candlestick confirmation -> >=2R"
                    ),
                    "then":
                        "volatility + liquidity -> Quant -> AI -> FAST -> IG tradeability/spread",
                },
                "confidence_policy": {
                    "quant_min":
                        QUANT_MIN_CONFIDENCE,
                    "quant_min_pct": 28.0,
                    "model_ai_min":
                        MODEL_AI_MIN_CONFIDENCE,
                    "model_ai_min_pct": 40.0,
                    "fast_score_min":
                        STANDARD_FAST_SCORE_MIN,
                    "historical_validation_mode":
                        "INFORMATIONAL_ONLY",
                    "historical_execution_veto":
                        False,
                    "prime_authority":
                        "BROKER_SETTLED_FORWARD_ONLY",
                },
                "categories": by_category,
                "category_count": len(CATEGORY_ORDER),
                "universe_size": len(_active_market_seeds()),
                "catalogue_size": len(CATEGORY_MARKET_SEEDS),
                "active_execution_keys": list(ACTIVE_EXECUTION_KEYS),
                "retired_entry_markets": [
                    str(row.get("key"))
                    for row in CATEGORY_MARKET_SEEDS
                    if str(row.get("key") or "").upper()
                    not in set(ACTIVE_EXECUTION_KEYS)
                ],
                "fresh_evaluations":
                    len(self._fresh_rows()),
                "standard_ready": standard_ready,
                "compound_ready": compound_ready,
                "elite_ready": compound_ready,
                "evaluated_by_asset_class":
                    evaluated_by_asset_class,
                "eligibility_refresh_seconds":
                    self.eligibility_refresh_seconds,
                "heavy_scan_seconds":
                    self.scan_interval_seconds,
                "top_n_per_category":
                    TOP_N_PER_CATEGORY,
                "strategy_optimizer": {
                    "historical_authority": False,
                    "selection_mode":
                        "CURRENT_CLOSED_CANDLE_ONLY",
                },
                "evidence_hygiene":
                    self.evidence_coverage(),
                "full_refresh":
                    self.full_refresh_status(),
                "compound_slots_per_category":
                    COMPOUND_SLOTS_PER_CATEGORY,
                "runs": int(
                    self._state.get("runs") or 0
                ),
                "last_run_at":
                    self._state.get("last_run_at"),
                "last_batch_keys": list(
                    self._state.get(
                        "last_batch_keys"
                    )
                    or []
                ),
                "last_error":
                    self._state.get("last_error"),
                "state_path": self.state_path,
                "execution_mode": "IG_DEMO_ONLY",
                "live_money_execution": False,
            }

    def start_thread(self) -> None:
        with self._lock:
            if (
                self._thread
                and self._thread.is_alive()
            ):
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                daemon=True,
                name="jasong-category-strategies",
            )
            self._thread.start()

    def _loop(self) -> None:
        if self._stop.wait(12.0):
            return

        while not self._stop.is_set():
            try:
                if self._state.get("enabled", True):
                    coverage = self.evidence_coverage()
                    refresh_running = bool(
                        self._full_refresh_thread
                        and self._full_refresh_thread.is_alive()
                    )
                    if (
                        self.auto_full_refresh
                        and coverage[
                            "markets_pending_optimisation"
                        ] > 0
                    ):
                        if not refresh_running:
                            self.start_full_refresh()
                    elif not refresh_running:
                        self.run_now()
            except Exception as exc:
                with self._lock:
                    self._state["last_error"] = (
                        f"{type(exc).__name__}: {exc}"
                    )
                    self._persist()

            self._stop.wait(
                self.scan_interval_seconds
            )

    def stop_thread(self) -> None:
        self._stop.set()
