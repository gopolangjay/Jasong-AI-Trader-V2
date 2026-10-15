"""Closed-candle trend/pullback and range/divergence rules (no broker I/O)."""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

VERSION = "6.14-regime-adaptive-v1"
TREND_ID = "REGIME_EMA20_50_PULLBACK_V1"
REVERSAL_ID = "REGIME_RSI14_DIVERGENCE_V1"
STRATEGY_IDS = (TREND_ID, REVERSAL_ID)
OHLCV = ["Open", "High", "Low", "Close", "Volume"]


@dataclass(frozen=True)
class RegimeConfig:
    trend_adx: float = 25.0
    range_adx: float = 20.0
    crossover_lookback: int = 40
    pivot_radius: int = 2
    pivot_lookback: int = 48
    max_pivot_age: int = 8
    trend_target_r: float = 2.0
    reversal_min_r: float = 1.2
    max_spread_atr: float = 0.15
    min_relative_volume: float = 0.5


def completed_frames(raw: pd.DataFrame, now: Any = None) -> dict[str, pd.DataFrame]:
    """Input timestamps are UTC M15 bar opens; require every constituent bar.

    A partial H1/H4 candle, a duplicate, or a missing M15 bar can never be
    promoted to a completed higher-timeframe signal.
    """
    if not isinstance(raw.index, pd.DatetimeIndex):
        raise ValueError("Regime strategy requires timestamped M15 OHLCV")
    frame = raw.copy()
    frame.index = pd.to_datetime(frame.index, utc=True)
    if frame.index.has_duplicates:
        raise ValueError("Duplicate M15 timestamps")
    if any((frame.index.asi8 % pd.Timedelta(minutes=15).value) != 0):
        raise ValueError("Regime input must use aligned M15 candle opens")
    for col in OHLCV[:4]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame["Volume"] = pd.to_numeric(frame.get("Volume", pd.Series(0.0, index=frame.index)), errors="coerce").fillna(0.0)
    values = frame[OHLCV[:4]]
    valid = (np.isfinite(values).all(axis=1) & (values > 0).all(axis=1)
             & (frame.High >= frame[["Open", "Close", "Low"]].max(axis=1))
             & (frame.Low <= frame[["Open", "Close", "High"]].min(axis=1)))
    if not valid.all() or not np.isfinite(frame.Volume).all() or (frame.Volume < 0).any():
        raise ValueError("Invalid OHLCV in regime input")
    stamp = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    frame = frame[OHLCV].sort_index()
    frame = frame[frame.index + pd.Timedelta(minutes=15) <= stamp]
    frames = {"M15": frame}
    for name, rule, count in (("H1", "1h", 4), ("H4", "4h", 16)):
        grouped = frame.resample(rule, label="left", closed="left", origin="start_day")
        result = grouped.agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
        result = result[(grouped.Close.count() == count) & (result.index + pd.Timedelta(rule) <= stamp)]
        frames[name] = result.dropna()
    return frames


def features(frame: pd.DataFrame) -> pd.DataFrame:
    x = frame.copy()
    x["EMA20"] = x.Close.ewm(span=20, adjust=False, min_periods=50).mean()
    x["EMA50"] = x.Close.ewm(span=50, adjust=False, min_periods=50).mean()
    delta = x.Close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    x["RSI14"] = (100 - 100 / (1 + gain / loss.replace(0, np.nan))).where(loss != 0, 100.0).where((gain != 0) | (loss != 0), 50.0)
    tr = pd.concat([x.High-x.Low, (x.High-x.Close.shift()).abs(), (x.Low-x.Close.shift()).abs()], axis=1).max(axis=1)
    x["ATR14"] = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    up, down = x.High.diff(), -x.Low.diff()
    plus = up.where((up > down) & (up > 0), 0.0).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    minus = down.where((down > up) & (down > 0), 0.0).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    dx = (100 * (plus-minus).abs() / (plus+minus).replace(0, np.nan)).fillna(0.0)
    x["ADX14"] = dx.ewm(alpha=1/14, adjust=False, min_periods=28).mean()
    x["BB_MID"] = x.Close.rolling(20).mean()
    std = x.Close.rolling(20).std(ddof=0)
    x["BB_UPPER"], x["BB_LOWER"] = x.BB_MID + 2*std, x.BB_MID - 2*std
    return x


def classify(frame: pd.DataFrame, config: RegimeConfig) -> str:
    row = frame.iloc[-1]
    if row.ADX14 > config.trend_adx:
        if row.EMA20 > row.EMA50 and row.EMA20 > frame.EMA20.iloc[-4]:
            return "TREND_UP"
        if row.EMA20 < row.EMA50 and row.EMA20 < frame.EMA20.iloc[-4]:
            return "TREND_DOWN"
        return "TRANSITION"
    if row.ADX14 < config.range_adx:
        return "RANGING"
    if (frame.ADX14.tail(4).diff().dropna() < 0).all() and abs(row.EMA20-row.EMA50) < row.ATR14*0.5:
        return "EXHAUSTED"
    return "TRANSITION"


def _strong(row: pd.Series, direction: str, minimum_body: float = 0.55) -> bool:
    span = float(row.High-row.Low)
    if span <= 0:
        return False
    bullish = direction == "BUY"
    body = float(row.Close-row.Open) * (1 if bullish else -1)
    edge = float(row.High-row.Close if bullish else row.Close-row.Low) / span
    return body/span >= minimum_body and edge <= 0.3


def trend_setup(frame: pd.DataFrame, direction: str, config: RegimeConfig) -> dict[str, Any]:
    sign = 1 if direction == "BUY" else -1
    difference = (frame.EMA20-frame.EMA50) * sign
    crosses = np.flatnonzero(((difference > 0) & (difference.shift() <= 0)).to_numpy())
    if not len(crosses) or len(frame)-1-int(crosses[-1]) > config.crossover_lookback:
        return {"eligible": False, "reason": "NO_RECENT_CONFIRMED_EMA_CROSS"}
    cross = int(crosses[-1])
    if cross >= len(frame)-1:
        return {"eligible": False, "reason": "WAIT_FOR_POST_CROSS_PULLBACK"}
    # Only the first confirmed pullback belongs to this crossover event.
    for i in range(cross+1, len(frame)):
        row = frame.iloc[i]
        tol = row.ATR14*0.25
        touch = row.Low <= row.EMA20+tol and row.High >= row.EMA20-tol
        resumed = sign*(row.Close-row.EMA20) >= 0 and abs(row.Close-row.EMA20) <= row.ATR14*0.6
        if touch and resumed and _strong(row, direction, 0.4):
            if i != len(frame)-1:
                return {"eligible": False, "reason": "PULLBACK_CONFIRMATION_ALREADY_PASSED"}
            swing = frame.iloc[max(cross, i-4):i+1]
            extreme = float(swing.Low.min() if sign == 1 else swing.High.max())
            return {"eligible": True, "direction": direction, "event": frame.index[cross].isoformat(),
                    "confirmation": frame.index[i].isoformat(), "extreme": extreme,
                    "reason": "EMA_CROSS_THEN_CONFIRMED_EMA20_PULLBACK"}
    return {"eligible": False, "reason": "WAIT_FOR_EMA20_PULLBACK_AND_CANDLE"}


def divergence_setup(frame: pd.DataFrame, direction: str, config: RegimeConfig) -> dict[str, Any]:
    """Match RSI at two strict price pivots, using only confirmed right bars."""
    radius = config.pivot_radius
    field = "Low" if direction == "BUY" else "High"
    values = frame[field].to_numpy()
    pivots = []
    for i in range(max(radius, len(frame)-config.pivot_lookback), len(frame)-radius):
        neighbours = np.r_[values[i-radius:i], values[i+1:i+radius+1]]
        if bool((values[i] < neighbours).all() if direction == "BUY" else (values[i] > neighbours).all()):
            pivots.append(i)
    if len(pivots) < 2:
        return {"eligible": False, "reason": "TWO_CONFIRMED_PRICE_PIVOTS_REQUIRED"}
    first, second = pivots[-2:]
    atr = float(frame.ATR14.iloc[-1])
    a, b = frame.iloc[first], frame.iloc[second]
    rsi_a, rsi_b = float(a.RSI14), float(b.RSI14)
    bullish = direction == "BUY"
    price_div = b[field] < a[field]-atr*0.02 if bullish else b[field] > a[field]+atr*0.02
    rsi_div = (rsi_b >= rsi_a+3 and rsi_a <= 30 and rsi_b < 45) if bullish else (rsi_b <= rsi_a-3 and rsi_a >= 70 and rsi_b > 55)
    if second-first < 3 or len(frame)-1-second > config.max_pivot_age or not price_div or not rsi_div:
        return {"eligible": False, "reason": "NO_FRESH_RSI_DIVERGENCE_AT_PRICE_PIVOTS"}
    bounds = frame.iloc[max(0, first-20):second]
    support, resistance = float(bounds.Low.min()), float(bounds.High.max())
    near_edge = b.Low <= support+atr*0.35 and b.Low <= b.BB_LOWER+atr*0.35 if bullish else b.High >= resistance-atr*0.35 and b.High >= b.BB_UPPER-atr*0.35
    latest = frame.iloc[-1]
    # A close outside the established range is a breakout, never a fade signal.
    inside = support-atr*0.15 <= latest.Close <= resistance+atr*0.15
    if not near_edge or not inside:
        return {"eligible": False, "reason": "NOT_AT_RANGE_EDGE_OR_RANGE_BROKEN"}
    confirmations = [i for i in range(second+radius, len(frame)) if _strong(frame.iloc[i], direction)]
    if not confirmations or confirmations[0] != len(frame)-1:
        return {"eligible": False, "reason": "WAIT_FOR_FRESH_STRONG_REVERSAL_CANDLE"}
    target = min(float(latest.BB_MID), (support+resistance)/2) if bullish else max(float(latest.BB_MID), (support+resistance)/2)
    return {"eligible": True, "direction": direction, "event": frame.index[first].isoformat()+"/"+frame.index[second].isoformat(),
            "confirmation": frame.index[-1].isoformat(), "extreme": float(b[field]), "range_target": target,
            "pivot_rsi": [rsi_a, rsi_b], "support": support, "resistance": resistance,
            "reason": "CONFIRMED_RSI_DIVERGENCE_AT_RANGE_BOUNDARY"}


def analyze_regime(raw: pd.DataFrame, symbol: str, *, now: Any = None, config: RegimeConfig | None = None) -> dict[str, Any]:
    config = config or RegimeConfig()
    out: dict[str, Any] = {"version": VERSION, "direction": "WAIT", "eligible": False, "regime": "UNKNOWN",
                           "strategy_id": TREND_ID, "strategy_name": "EMA20/50 Trend Pullback", "rejection_reasons": [],
                           "forming_candle_ignored": True, "live_money_execution": False}
    frames = completed_frames(raw, now)
    if len(frames["M15"]) < 60 or len(frames["H1"]) < 60:
        out["rejection_reasons"] = ["INSUFFICIENT_COMPLETE_M15_H1_HISTORY"]
        return out
    m15, h1 = features(frames["M15"]), features(frames["H1"])
    stamp = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    base_end = m15.index[-1] + pd.Timedelta(minutes=15)
    out.update({"regime": classify(h1, config), "analysis_price_timestamp": base_end.isoformat(), "live_price": float(m15.Close.iloc[-1]),
                "h1_adx": float(h1.ADX14.iloc[-1]), "data_fresh": 0 <= (stamp-base_end).total_seconds() <= 990})
    regime = out["regime"]
    if regime.startswith("TREND_"):
        frame, timeframe = h1, "H1"
        direction = "BUY" if regime == "TREND_UP" else "SELL"
        selected = trend_setup(frame, direction, config)
        if len(frames["H4"]) >= 60:
            h4 = features(frames["H4"])
            if (direction == "BUY" and h4.EMA20.iloc[-1] < h4.EMA50.iloc[-1]) or (direction == "SELL" and h4.EMA20.iloc[-1] > h4.EMA50.iloc[-1]):
                selected = {"eligible": False, "reason": "H4_TREND_CONFLICT"}
    elif regime in {"RANGING", "EXHAUSTED"}:
        out.update({"strategy_id": REVERSAL_ID, "strategy_name": "RSI14 Divergence Reversal"})
        # Prefer M15; H1 is an alternative when it is also stalling.
        choices = [(features(frames[tf]), tf) for tf in ("M15", "H1")]
        frame, timeframe, selected = choices[0][0], "M15", {"eligible": False, "reason": "NO_CONFIRMED_RANGE_DIVERGENCE"}
        for candidate, tf in choices:
            if candidate.ADX14.iloc[-1] >= config.trend_adx:
                continue
            setups = [divergence_setup(candidate, side, config) for side in ("BUY", "SELL")]
            qualified = [s for s in setups if s["eligible"]]
            if len(qualified) == 1:
                frame, timeframe, selected = candidate, tf, qualified[0]
                break
        direction = selected.get("direction", "WAIT")
    else:
        out["rejection_reasons"] = ["TRANSITION_REGIME_NO_ENTRY"]
        return out
    row = frame.iloc[-1]
    atr = float(row.ATR14)
    median_atr = float(frame.ATR14.iloc[-61:-1].median())
    volatility_ratio = atr/median_atr if median_atr > 0 else math.inf
    vol = frame.Volume.iloc[-21:-1]
    median_volume = float(vol.median())
    relative_volume = float(row.Volume)/median_volume if median_volume > 0 else None
    out.update({"timeframe": timeframe, "selected_setup": selected, "strategy_reason": selected["reason"],
                "adx": float(row.ADX14), "rsi": float(row.RSI14), "ema20": float(row.EMA20), "ema50": float(row.EMA50),
                "atr": atr, "atr_pct": atr/float(row.Close)*100, "volatility_ratio": volatility_ratio,
                "panic_volatility_pass": volatility_ratio <= 3, "relative_volume": relative_volume,
                "volume_available": median_volume > 0, "volume_pass": relative_volume is None or relative_volume >= config.min_relative_volume,
                "max_spread_atr": config.max_spread_atr, "live_price": float(row.Close)})
    reasons = [] if selected["eligible"] else [selected["reason"]]
    if not out["data_fresh"]:
        reasons.append("ANALYSIS_CANDLES_STALE")
    if not math.isfinite(atr) or atr <= 0 or volatility_ratio > 3:
        reasons.append("PANIC_OR_INVALID_VOLATILITY")
    if not out["volume_pass"]:
        reasons.append("LOW_OBSERVED_RELATIVE_VOLUME")
    if selected["eligible"]:
        entry = float(row.Close)
        sign = 1 if direction == "BUY" else -1
        widen = 1.25 if volatility_ratio > 1.5 else 1.0
        distance = max(sign*(entry-selected["extreme"])+atr*0.2*widen, atr*(1.5 if out["strategy_id"] == TREND_ID else 0.5)*widen)
        if out["strategy_id"] == TREND_ID:
            target_r = config.trend_target_r
        else:
            target_r = sign*(selected["range_target"]-entry)/distance
            if distance > atr*1.5*widen:
                reasons.append("REVERSAL_INVALIDATION_TOO_WIDE")
            if target_r < config.reversal_min_r:
                reasons.append("INSUFFICIENT_ROOM_TO_RANGE_MEAN")
        close_time = pd.Timestamp(selected["confirmation"]) + pd.Timedelta(hours=1 if timeframe == "H1" else 0.25)
        if (stamp-close_time).total_seconds() > 300:
            reasons.append("CONFIRMATION_SIGNAL_STALE")
        event = "|".join([out["strategy_id"], symbol, timeframe, direction, selected["event"]])
        out.update({"setup_id": hashlib.sha256(event.encode()).hexdigest()[:24], "signal_timestamp": close_time.isoformat(),
                    "structural_stop_distance": distance, "structural_stop_price": entry-sign*distance,
                    "take_profit_target_price": entry+sign*distance*target_r, "target_distance": distance*target_r,
                    "target_r": target_r, "closed_candle_timestamp": selected["confirmation"],
                    "max_hold_seconds": 12*3600 if out["strategy_id"] == TREND_ID else 4*3600})
    out["rejection_reasons"] = reasons
    out["eligible"] = not reasons
    out["direction"] = direction if out["eligible"] else "WAIT"
    return out


def execution_reasons(row: dict[str, Any]) -> list[str]:
    """The PRIME and order paths must never promote an unsafe raw signal."""
    reasons = list((row.get("regime_strategy") or {}).get("rejection_reasons") or [])
    for passed, reason in (
        (row.get("signal_qualified") is True, "REGIME_SIGNAL_NOT_QUALIFIED"),
        (row.get("session_active") is True, "SESSION_INACTIVE"),
        ((row.get("news_guard") or {}).get("clear") is True, "HIGH_IMPACT_NEWS_EXECUTION_GUARD"),
        (row.get("panic_volatility_pass") is True, "PANIC_VOLATILITY_EXECUTION_GUARD"),
        (row.get("liquidity_pass") is True, "LIQUIDITY_EXECUTION_GUARD"),
        (row.get("spread_pass") is True, "SPREAD_GATE_FAIL"),
        (row.get("ig_tradeable") is True, "IG_NOT_TRADEABLE"),
    ):
        if not passed:
            reasons.append(reason)
    return list(dict.fromkeys(reasons))
