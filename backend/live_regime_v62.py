from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


VERSION = "6.9.4-live-regime-v6.2.1-clean"

CATEGORY_SPREAD_LIMIT_BPS = {
    "FOREX": 8.0,
    "INDICES": 18.0,
    "CRYPTO": 80.0,
    "METALS": 22.0,
    "ENERGY": 22.0,
    "SHARES": 35.0,
}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except Exception:
        return default


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def _clean_ohlcv(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        raise ValueError("market frame is empty")
    data = frame.copy()
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = [str(col[0]) for col in data.columns]
    aliases = {
        "open": "Open", "high": "High", "low": "Low", "close": "Close",
        "volume": "Volume", "OPEN": "Open", "HIGH": "High", "LOW": "Low",
        "CLOSE": "Close", "VOLUME": "Volume",
    }
    data = data.rename(columns={k: v for k, v in aliases.items() if k in data.columns})
    required = ["Open", "High", "Low", "Close"]
    missing = [c for c in required if c not in data.columns]
    if missing:
        raise ValueError(f"missing OHLC columns: {missing}")
    if "Volume" not in data.columns:
        data["Volume"] = 0.0
    data = data[["Open", "High", "Low", "Close", "Volume"]].copy()
    for col in data.columns:
        data[col] = pd.to_numeric(data[col], errors="coerce")
    data = data.dropna(subset=required).sort_index()
    data = data[~data.index.duplicated(keep="last")]
    if len(data) < 80:
        raise ValueError(f"insufficient current-candle buffer: {len(data)} rows")
    return data


def _completed(frame: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Use only closed candles when the DatetimeIndex identifies an open bar."""
    if len(frame) < 3 or not isinstance(frame.index, pd.DatetimeIndex):
        return frame
    last = frame.index[-1]
    try:
        if last.tzinfo is None:
            last = last.tz_localize("UTC")
        else:
            last = last.tz_convert("UTC")
        now = pd.Timestamp.now(tz="UTC")
        if now < last + pd.Timedelta(minutes=minutes):
            return frame.iloc[:-1]
    except Exception:
        pass
    return frame


def _resample(frame: pd.DataFrame, rule: str, minutes: int) -> pd.DataFrame:
    if not isinstance(frame.index, pd.DatetimeIndex):
        return frame.copy()
    out = frame.resample(rule).agg({
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    }).dropna(subset=["Open", "High", "Low", "Close"])
    return _completed(out, minutes)


def _indicators(frame: pd.DataFrame) -> pd.DataFrame:
    df = frame.copy()
    close, high, low = df["Close"], df["High"], df["Low"]

    df["EMA20"] = close.ewm(span=20, adjust=False).mean()
    df["EMA50"] = close.ewm(span=50, adjust=False).mean()

    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    df["ATR14"] = atr
    df["ATR_PCT"] = (atr / close.replace(0, np.nan)) * 100.0
    df["ATR_PCT_MED50"] = df["ATR_PCT"].rolling(50, min_periods=20).median()

    up = high.diff()
    down = -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    atr_w = tr.ewm(alpha=1 / 14, adjust=False).mean().replace(0, np.nan)
    plus_di = 100.0 * plus_dm.ewm(alpha=1 / 14, adjust=False).mean() / atr_w
    minus_di = 100.0 * minus_dm.ewm(alpha=1 / 14, adjust=False).mean() / atr_w
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    df["ADX14"] = dx.ewm(alpha=1 / 14, adjust=False).mean().fillna(0.0)
    df["PLUS_DI14"] = plus_di.fillna(0.0)
    df["MINUS_DI14"] = minus_di.fillna(0.0)

    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    df["RSI14"] = (100.0 - (100.0 / (1.0 + rs))).fillna(50.0)

    ma20 = close.rolling(20, min_periods=20).mean()
    sd20 = close.rolling(20, min_periods=20).std(ddof=0)
    df["BB_MID"] = ma20
    df["BB_UPPER"] = ma20 + 2.0 * sd20
    df["BB_LOWER"] = ma20 - 2.0 * sd20
    df["BB_WIDTH_PCT"] = ((df["BB_UPPER"] - df["BB_LOWER"]) / ma20.replace(0, np.nan)) * 100.0
    df["BB_WIDTH_MED50"] = df["BB_WIDTH_PCT"].rolling(50, min_periods=20).median()

    ll14 = low.rolling(14, min_periods=14).min()
    hh14 = high.rolling(14, min_periods=14).max()
    df["STOCH_K"] = 100.0 * (close - ll14) / (hh14 - ll14).replace(0, np.nan)
    df["STOCH_D"] = df["STOCH_K"].rolling(3, min_periods=1).mean()

    body = (df["Close"] - df["Open"]).abs()
    rng = (df["High"] - df["Low"]).replace(0, np.nan)
    df["BODY_RATIO"] = (body / rng).fillna(0.0)
    df["BULL_CANDLE"] = df["Close"] > df["Open"]
    df["BEAR_CANDLE"] = df["Close"] < df["Open"]

    vol = df["Volume"].fillna(0.0)
    vol_ma = vol.rolling(20, min_periods=5).mean()
    df["REL_VOLUME"] = np.where(vol_ma > 0, vol / vol_ma, np.nan)
    return df


def _pivot_indices(series: pd.Series, mode: str, window: int = 2) -> List[int]:
    values = series.to_numpy(dtype=float)
    found: List[int] = []
    for i in range(window, len(values) - window):
        if not math.isfinite(values[i]):
            continue
        chunk = values[i - window:i + window + 1]
        if mode == "LOW" and values[i] == np.nanmin(chunk) and np.sum(chunk == values[i]) == 1:
            found.append(i)
        elif mode == "HIGH" and values[i] == np.nanmax(chunk) and np.sum(chunk == values[i]) == 1:
            found.append(i)
    return found


def _rsi_divergence(df: pd.DataFrame, direction: str) -> Dict[str, Any]:
    look = df.tail(50).copy()
    if len(look) < 20:
        return {"confirmed": False, "reason": "insufficient divergence window"}
    price_col = "Low" if direction == "BUY" else "High"
    mode = "LOW" if direction == "BUY" else "HIGH"
    pivots = _pivot_indices(look[price_col], mode)
    if len(pivots) < 2:
        return {"confirmed": False, "reason": "two price pivots not found"}
    i1, i2 = pivots[-2], pivots[-1]
    if i2 < len(look) - 12:
        return {"confirmed": False, "reason": "latest divergence pivot too old"}
    p1, p2 = _f(look.iloc[i1][price_col]), _f(look.iloc[i2][price_col])
    r1, r2 = _f(look.iloc[i1]["RSI14"], 50.0), _f(look.iloc[i2]["RSI14"], 50.0)
    if direction == "BUY":
        confirmed = p2 < p1 and r2 > r1 and min(r1, r2) <= 30.0
    else:
        confirmed = p2 > p1 and r2 < r1 and max(r1, r2) >= 70.0
    return {
        "confirmed": bool(confirmed),
        "price_pivot_1": p1,
        "price_pivot_2": p2,
        "rsi_pivot_1": round(r1, 3),
        "rsi_pivot_2": round(r2, 3),
        "pivot_age_bars": len(look) - 1 - i2,
        "reason": (
            "bullish price lower-low / RSI higher-low divergence"
            if confirmed and direction == "BUY"
            else "bearish price higher-high / RSI lower-high divergence"
            if confirmed
            else "strict RSI divergence conditions not met"
        ),
    }


def _volatility_state(row: pd.Series) -> Tuple[str, float, float]:
    atr_pct = _f(row.get("ATR_PCT"))
    med = _f(row.get("ATR_PCT_MED50"), atr_pct)
    ratio = atr_pct / med if med > 0 else 1.0
    if ratio >= 2.25:
        return "EXTREME", ratio, 0.40
    if ratio >= 1.65:
        return "HIGH", ratio, 0.55
    if ratio <= 0.45:
        return "TOO_LOW", ratio, 0.60
    if ratio <= 0.70:
        return "LOW", ratio, 0.75
    return "NORMAL", ratio, 1.00


def _volume_state(row: pd.Series) -> Tuple[str, Optional[float]]:
    rel = row.get("REL_VOLUME")
    if rel is None or (isinstance(rel, float) and math.isnan(rel)):
        return "NO_RELIABLE_VOLUME_SERIES", None
    val = _f(rel)
    if val >= 1.20:
        return "HIGH", val
    if val >= 0.70:
        return "NORMAL", val
    if val >= 0.35:
        return "THIN", val
    return "VERY_THIN", val


def _recent_cross(df: pd.DataFrame, direction: str, bars: int = 60) -> Optional[int]:
    tail = df.tail(max(3, bars)).copy()
    spread = tail["EMA20"] - tail["EMA50"]
    sign = spread > 0 if direction == "BUY" else spread < 0
    flips = sign & (~sign.shift(1, fill_value=False))
    indices = np.flatnonzero(flips.to_numpy())
    if len(indices) == 0:
        return None
    return len(tail) - 1 - int(indices[-1])


def _trend_setup(df: pd.DataFrame, timeframe: str) -> Dict[str, Any]:
    if len(df) < 60:
        return {"ready": False, "regime": "UNKNOWN", "direction": "WAIT", "score": 0.0}
    row, prev = df.iloc[-1], df.iloc[-2]
    close = _f(row["Close"])
    atr = max(_f(row["ATR14"]), close * 1e-6)
    adx = _f(row["ADX14"])
    ema20, ema50 = _f(row["EMA20"]), _f(row["EMA50"])
    slope20 = ema20 - _f(df.iloc[-4]["EMA20"], ema20)
    plus_di, minus_di = _f(row["PLUS_DI14"]), _f(row["MINUS_DI14"])

    if adx < 25.0:
        return {
            "ready": False,
            "regime": "RANGING_OR_TRANSITION" if adx < 20 else "TRANSITION",
            "direction": "WAIT",
            "score": 0.0,
            "adx": round(adx, 2),
            "reason": "Trend follower requires ADX >= 25",
        }

    if ema20 > ema50 and slope20 > 0 and plus_di >= minus_di:
        direction = "BUY"
    elif ema20 < ema50 and slope20 < 0 and minus_di >= plus_di:
        direction = "SELL"
    else:
        return {
            "ready": False, "regime": "TRENDING_BUT_CONFLICTED", "direction": "WAIT",
            "score": 20.0, "adx": round(adx, 2),
            "reason": "ADX is strong but EMA/DI direction is conflicted",
        }

    bullish = bool(row["BULL_CANDLE"])
    bearish = bool(row["BEAR_CANDLE"])
    candle_ok = bullish if direction == "BUY" else bearish
    body_ratio = _f(row["BODY_RATIO"])

    if direction == "BUY":
        touch = _f(row["Low"]) <= ema20 + 0.25 * atr and _f(row["Low"]) >= ema20 - 1.00 * atr
        reclaim = close >= ema20
        prior_extreme = _f(df["High"].iloc[-21:-1].max())
        breakout = close > prior_extreme + 0.05 * atr and _f(prev["Close"]) <= prior_extreme
    else:
        touch = _f(row["High"]) >= ema20 - 0.25 * atr and _f(row["High"]) <= ema20 + 1.00 * atr
        reclaim = close <= ema20
        prior_extreme = _f(df["Low"].iloc[-21:-1].min())
        breakout = close < prior_extreme - 0.05 * atr and _f(prev["Close"]) >= prior_extreme

    pullback = bool(touch and reclaim and candle_ok and body_ratio >= 0.35)
    breakout_ok = bool(breakout and candle_ok and body_ratio >= 0.55)
    cross_age = _recent_cross(df, direction, bars=70)
    established = abs(ema20 - ema50) / atr >= 0.35
    alignment_ok = cross_age is not None or established

    vol_state, rel_volume = _volume_state(row)
    volume_penalty = 0.0 if rel_volume is None or rel_volume >= 0.55 else 8.0
    false_breakout = breakout_ok and body_ratio < 0.65 and rel_volume is not None and rel_volume < 0.85

    score = 42.0
    score += _clip((adx - 25.0) * 1.2, 0, 18)
    score += 12.0 if alignment_ok else 0.0
    score += 18.0 if pullback else (15.0 if breakout_ok else 0.0)
    score += _clip((body_ratio - 0.35) * 25.0, 0, 8)
    if rel_volume is not None:
        score += _clip((rel_volume - 0.7) * 8.0, 0, 5)
    score -= volume_penalty
    if false_breakout:
        score -= 15.0
    score = _clip(score, 0, 100)

    trigger = "EMA20_PULLBACK" if pullback else ("20_BAR_BREAKOUT" if breakout_ok else "WAIT_FOR_PULLBACK_OR_BREAKOUT")
    ready = bool(alignment_ok and (pullback or breakout_ok) and not false_breakout and score >= 70.0)
    return {
        "ready": ready,
        "strategy_id": "LIVE_TREND_FOLLOWER_EMA20_50_ADX_V62",
        "strategy_name": "Trend Follower — EMA20/EMA50 + ADX",
        "regime": "TRENDING_UP" if direction == "BUY" else "TRENDING_DOWN",
        "direction": direction if ready else "WAIT",
        "proposed_direction": direction,
        "timeframe": timeframe,
        "trigger": trigger,
        "score": round(score, 2),
        "adx": round(adx, 2),
        "ema20": round(ema20, 8),
        "ema50": round(ema50, 8),
        "cross_age_bars": cross_age,
        "established_alignment": established,
        "pullback_confirmed": pullback,
        "breakout_confirmed": breakout_ok,
        "false_breakout_risk": false_breakout,
        "signal_candle_body_ratio": round(body_ratio, 4),
        "volume_state": vol_state,
        "relative_volume": None if rel_volume is None else round(rel_volume, 4),
        "atr": atr,
        "atr_pct": round(_f(row["ATR_PCT"]), 6),
        "live_price": close,
        "signal_high": _f(row["High"]),
        "signal_low": _f(row["Low"]),
        "reason": (
            "EMA20/EMA50 trend + ADX>=25 + confirmed 20EMA pullback candle"
            if pullback else
            "EMA20/EMA50 trend + ADX>=25 + confirmed breakout candle"
            if breakout_ok else
            "Trend present; waiting for pullback to EMA20 or confirmed breakout"
        ),
    }


def _range_setup(df: pd.DataFrame, timeframe: str) -> Dict[str, Any]:
    if len(df) < 60:
        return {"ready": False, "regime": "UNKNOWN", "direction": "WAIT", "score": 0.0}
    row = df.iloc[-1]
    close = _f(row["Close"])
    atr = max(_f(row["ATR14"]), close * 1e-6)
    adx = _f(row["ADX14"])
    ema_gap_atr = abs(_f(row["EMA20"]) - _f(row["EMA50"])) / atr
    width = _f(row["BB_WIDTH_PCT"])
    width_med = _f(row["BB_WIDTH_MED50"], width)
    width_ratio = width / width_med if width_med > 0 else 1.0

    if adx >= 22.0 or ema_gap_atr > 1.10 or width_ratio > 1.80:
        return {
            "ready": False,
            "regime": "TRENDING_OR_BREAKING_OUT" if adx >= 25 else "TRANSITION",
            "direction": "WAIT", "score": 0.0, "adx": round(adx, 2),
            "reason": "RSI divergence reversal disabled while market is trending/expanding",
        }

    support = _f(df["Low"].iloc[-41:-1].min())
    resistance = _f(df["High"].iloc[-41:-1].max())
    lower, upper = _f(row["BB_LOWER"]), _f(row["BB_UPPER"])
    near_support = _f(row["Low"]) <= support + 0.35 * atr or close <= lower + 0.25 * atr
    near_resistance = _f(row["High"]) >= resistance - 0.35 * atr or close >= upper - 0.25 * atr

    bull_div = _rsi_divergence(df, "BUY")
    bear_div = _rsi_divergence(df, "SELL")
    body_ratio = _f(row["BODY_RATIO"])
    strong_green = bool(row["BULL_CANDLE"]) and body_ratio >= 0.50
    strong_red = bool(row["BEAR_CANDLE"]) and body_ratio >= 0.50

    if bull_div.get("confirmed") and near_support and strong_green:
        direction = "BUY"
        divergence = bull_div
        boundary = "SUPPORT/LOWER_BOLLINGER"
    elif bear_div.get("confirmed") and near_resistance and strong_red:
        direction = "SELL"
        divergence = bear_div
        boundary = "RESISTANCE/UPPER_BOLLINGER"
    else:
        direction = "WAIT"
        divergence = bull_div if near_support else bear_div if near_resistance else {"confirmed": False}
        boundary = "NONE"

    vol_state, rel_volume = _volume_state(row)
    score = 38.0
    score += 28.0 if divergence.get("confirmed") else 0.0
    score += 16.0 if boundary != "NONE" else 0.0
    score += _clip((body_ratio - 0.5) * 30.0, 0, 8) if direction != "WAIT" else 0.0
    score += _clip((22.0 - adx) * 0.8, 0, 8)
    if rel_volume is not None and rel_volume < 0.30:
        score -= 8.0
    score = _clip(score, 0, 100)
    ready = bool(direction in {"BUY", "SELL"} and score >= 75.0)

    return {
        "ready": ready,
        "strategy_id": "LIVE_RANGE_RSI_DIVERGENCE_V62",
        "strategy_name": "Reversal Timer — RSI14 Divergence + Bollinger Range",
        "regime": "RANGING",
        "direction": direction if ready else "WAIT",
        "proposed_direction": direction,
        "timeframe": timeframe,
        "trigger": "RSI_DIVERGENCE_REVERSAL" if direction != "WAIT" else "WAIT_FOR_STRICT_DIVERGENCE",
        "score": round(score, 2),
        "adx": round(adx, 2),
        "rsi": round(_f(row["RSI14"], 50.0), 2),
        "stoch_k": round(_f(row["STOCH_K"], 50.0), 2),
        "bb_lower": lower,
        "bb_upper": upper,
        "support": support,
        "resistance": resistance,
        "boundary": boundary,
        "divergence": divergence,
        "signal_candle_body_ratio": round(body_ratio, 4),
        "volume_state": vol_state,
        "relative_volume": None if rel_volume is None else round(rel_volume, 4),
        "atr": atr,
        "atr_pct": round(_f(row["ATR_PCT"]), 6),
        "live_price": close,
        "signal_high": _f(row["High"]),
        "signal_low": _f(row["Low"]),
        "reason": (
            "Range + strict RSI divergence + Bollinger/support-resistance + strong reversal candle"
            if ready else
            "Ranging market; waiting for strict RSI divergence at support/resistance and a strong reversal candle"
        ),
    }


def _protective_stop(setup: Dict[str, Any], volatility_state: str) -> Dict[str, Any]:
    price = _f(setup.get("live_price"))
    atr = _f(setup.get("atr"))
    direction = str(setup.get("proposed_direction") or setup.get("direction") or "").upper()
    if price <= 0 or atr <= 0 or direction not in {"BUY", "SELL"}:
        return {"stop_level": None, "stop_price_pct": None}

    if str(setup.get("strategy_id") or "").startswith("LIVE_TREND"):
        mult = 2.20 if volatility_state in {"HIGH", "EXTREME"} else 1.80
        if direction == "BUY":
            structural = _f(setup.get("signal_low"), price) - 0.25 * atr
            atr_stop = price - mult * atr
            stop = min(structural, atr_stop)
        else:
            structural = _f(setup.get("signal_high"), price) + 0.25 * atr
            atr_stop = price + mult * atr
            stop = max(structural, atr_stop)
    else:
        mult = 1.15 if volatility_state in {"HIGH", "EXTREME"} else 0.90
        if direction == "BUY":
            support = _f(setup.get("support"), _f(setup.get("signal_low"), price))
            stop = min(_f(setup.get("signal_low"), price), support) - 0.15 * atr
            stop = min(stop, price - mult * atr)
        else:
            resistance = _f(setup.get("resistance"), _f(setup.get("signal_high"), price))
            stop = max(_f(setup.get("signal_high"), price), resistance) + 0.15 * atr
            stop = max(stop, price + mult * atr)

    pct = abs(price - stop) / price * 100.0
    return {"stop_level": round(stop, 10), "stop_price_pct": round(pct, 6)}


def analyze_live_market(frame: pd.DataFrame, *, category: str, seed: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Evaluate current market regime only; no historical performance/backtest/news."""
    raw = _clean_ohlcv(frame)
    raw = _completed(raw, 15)
    raw = raw.tail(1600)

    f15 = _indicators(raw)
    f1h = _indicators(_resample(raw, "1h", 60)) if isinstance(raw.index, pd.DatetimeIndex) else _indicators(raw)
    f4h = _indicators(_resample(raw, "4h", 240)) if isinstance(raw.index, pd.DatetimeIndex) else f1h

    candidates: List[Dict[str, Any]] = []
    for tf, df in (("1H", f1h), ("4H", f4h)):
        if len(df) >= 60:
            candidates.append(_trend_setup(df, tf))
    for tf, df in (("15M", f15), ("1H", f1h)):
        if len(df) >= 60:
            candidates.append(_range_setup(df, tf))

    ready = [x for x in candidates if x.get("ready")]
    if ready:
        chosen = max(ready, key=lambda x: _f(x.get("score")))
    else:
        # Keep the strongest current regime diagnosis for WATCH telemetry.
        chosen = max(candidates, key=lambda x: _f(x.get("score")), default={
            "ready": False, "regime": "UNKNOWN", "direction": "WAIT", "score": 0.0,
            "reason": "No current strategy context available",
        })

    # Volatility comes from the trigger timeframe itself where possible.
    tf = str(chosen.get("timeframe") or "15M")
    source = f15 if tf == "15M" else f1h if tf == "1H" else f4h
    latest = source.iloc[-1]
    vol_state, vol_ratio, size_mult = _volatility_state(latest)
    volume_state, rel_volume = _volume_state(latest)
    liquidity_ok = not (rel_volume is not None and rel_volume < 0.30)
    volatility_ok = vol_state != "EXTREME"

    stop = _protective_stop(chosen, vol_state)
    score = _f(chosen.get("score"))
    strategy_id = str(chosen.get("strategy_id") or "")
    if strategy_id.startswith("LIVE_TREND"):
        stop_win_target = 25.0 + _clip((score - 70.0) / 30.0 * 5.0, 0.0, 5.0)
        max_hold_hours = 48.0 if tf == "4H" else 12.0
    else:
        stop_win_target = 20.0 + _clip((score - 75.0) / 25.0 * 5.0, 0.0, 5.0)
        max_hold_hours = 16.0 if tf == "1H" else 4.0

    chosen = dict(chosen)
    chosen.update({
        "version": VERSION,
        "category": str(category or "").upper().strip(),
        "market": (seed or {}).get("name"),
        "symbol": (seed or {}).get("key"),
        "current_candle_only_policy": True,
        "historical_performance_used": False,
        "news_used": False,
        "market_regime": chosen.get("regime"),
        "live_setup_score": round(score, 2),
        "volatility_state": vol_state,
        "volatility_ratio_to_recent_median": round(vol_ratio, 4),
        "volatility_ok": volatility_ok,
        "liquidity_state": volume_state,
        "liquidity_ok_before_spread": liquidity_ok,
        "relative_volume": None if rel_volume is None else round(rel_volume, 4),
        "size_multiplier": round(size_mult, 4),
        "protective_stop_level": stop.get("stop_level"),
        "protective_stop_price_pct": stop.get("stop_price_pct"),
        "stop_win_trigger_pct": 20.0,
        "stop_win_target_pct": round(_clip(stop_win_target, 20.0, 30.0), 2),
        "stop_win_lock_floor_pct": 10.0,
        "max_hold_hours": max_hold_hours,
        "evaluated_at": time.time(),
        "candidate_setups": [
            {
                "strategy_id": x.get("strategy_id"),
                "regime": x.get("regime"),
                "timeframe": x.get("timeframe"),
                "direction": x.get("direction"),
                "proposed_direction": x.get("proposed_direction"),
                "ready": bool(x.get("ready")),
                "score": x.get("score"),
                "reason": x.get("reason"),
            }
            for x in candidates
        ],
    })
    chosen["setup_ready"] = bool(
        chosen.get("ready")
        and chosen.get("direction") in {"BUY", "SELL"}
        and volatility_ok
        and liquidity_ok
    )
    return chosen
