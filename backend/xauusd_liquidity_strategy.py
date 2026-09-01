from __future__ import annotations

import math
from datetime import datetime, time as dt_time, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd


VERSION = "6.10-xau-liquidity-structure-v1"
STRATEGY_ID = "XAUUSD_LIQUIDITY_STRUCTURE_V1"
STRATEGY_NAME = "XAUUSD London-New York Liquidity / Structure"

UTC = ZoneInfo("UTC")
LONDON = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")
JOHANNESBURG = ZoneInfo("Africa/Johannesburg")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except Exception:
        return default


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _normalise_ohlcv(raw: pd.DataFrame) -> pd.DataFrame:
    if raw is None or raw.empty:
        raise ValueError("XAUUSD strategy requires OHLCV data")

    frame = raw.copy()
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = [str(col[0]) for col in frame.columns]

    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError("XAUUSD strategy requires a DatetimeIndex")

    if frame.index.tz is None:
        frame.index = frame.index.tz_localize(UTC)
    else:
        frame.index = frame.index.tz_convert(UTC)

    for column in ("Open", "High", "Low", "Close"):
        if column not in frame.columns:
            raise ValueError(f"Missing required XAUUSD column: {column}")
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    if "Volume" not in frame.columns:
        frame["Volume"] = 1.0
    frame["Volume"] = pd.to_numeric(
        frame["Volume"], errors="coerce"
    ).fillna(1.0)

    frame = (
        frame.dropna(subset=["Open", "High", "Low", "Close"])
        .sort_index()
    )
    frame = frame[~frame.index.duplicated(keep="last")]
    if len(frame) < 240:
        raise ValueError(
            "XAUUSD liquidity/structure analysis requires at least 240 completed 15-minute candles"
        )
    return frame


def _atr(frame: pd.DataFrame, length: int = 14) -> pd.Series:
    previous = frame["Close"].shift(1)
    true_range = pd.concat(
        [
            frame["High"] - frame["Low"],
            (frame["High"] - previous).abs(),
            (frame["Low"] - previous).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(
        alpha=1.0 / length,
        adjust=False,
        min_periods=length,
    ).mean()


def _base_delta(frame: pd.DataFrame) -> pd.Timedelta:
    diffs = frame.index.to_series().diff().dropna()
    if diffs.empty:
        return pd.Timedelta(minutes=15)
    seconds = float(diffs.dt.total_seconds().median())
    if not math.isfinite(seconds) or seconds <= 0:
        seconds = 900.0
    return pd.Timedelta(seconds=max(60.0, min(seconds, 3600.0)))


def _resample_completed(
    frame: pd.DataFrame,
    rule: str,
    expected_base_bars: int,
) -> pd.DataFrame:
    """Build higher timeframes without using an unfinished aggregate candle.

    Yahoo and IG intraday timestamps represent the start of a candle. Shift to
    the candle's inferred close before resampling; then retain only aggregates
    whose labelled end is no later than the latest completed base-candle end.
    """
    delta = _base_delta(frame)
    shifted = frame[["Open", "High", "Low", "Close", "Volume"]].copy()
    shifted.index = shifted.index + delta
    latest_completed_end = shifted.index[-1]

    grouped = shifted.resample(
        rule,
        label="right",
        closed="right",
        origin="start_day",
    )
    out = grouped.agg(
        {
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Volume": "sum",
        }
    )
    counts = grouped["Close"].count()
    minimum_count = max(2, int(math.ceil(expected_base_bars * 0.70)))
    out = out[(counts >= minimum_count) & (out.index <= latest_completed_end)]
    return out.dropna(subset=["Open", "High", "Low", "Close"])


def _confirmed_pivots(
    frame: pd.DataFrame,
    *,
    left: int = 2,
    right: int = 2,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    highs: List[Dict[str, Any]] = []
    lows: List[Dict[str, Any]] = []
    high_values = frame["High"].to_numpy(dtype=float)
    low_values = frame["Low"].to_numpy(dtype=float)

    for i in range(left, len(frame) - right):
        high_window = high_values[i - left:i + right + 1]
        low_window = low_values[i - left:i + right + 1]
        if (
            high_values[i] == float(high_window.max())
            and high_values[i] > float(high_values[i - 1])
            and high_values[i] >= float(high_values[i + 1])
        ):
            highs.append(
                {
                    "position": i,
                    "timestamp": frame.index[i],
                    "price": float(high_values[i]),
                }
            )
        if (
            low_values[i] == float(low_window.min())
            and low_values[i] < float(low_values[i - 1])
            and low_values[i] <= float(low_values[i + 1])
        ):
            lows.append(
                {
                    "position": i,
                    "timestamp": frame.index[i],
                    "price": float(low_values[i]),
                }
            )
    return highs, lows


def _structure_state(frame: pd.DataFrame) -> Dict[str, Any]:
    if len(frame) < 30:
        return {
            "trend": "NEUTRAL",
            "reason": "INSUFFICIENT_HIGHER_TIMEFRAME_BARS",
        }

    highs, lows = _confirmed_pivots(frame)
    close = frame["Close"]
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()

    last_highs = highs[-2:]
    last_lows = lows[-2:]
    hh = len(last_highs) == 2 and last_highs[-1]["price"] > last_highs[-2]["price"]
    lh = len(last_highs) == 2 and last_highs[-1]["price"] < last_highs[-2]["price"]
    hl = len(last_lows) == 2 and last_lows[-1]["price"] > last_lows[-2]["price"]
    ll = len(last_lows) == 2 and last_lows[-1]["price"] < last_lows[-2]["price"]

    ema_bull = bool(ema20.iloc[-1] > ema50.iloc[-1] and close.iloc[-1] > ema20.iloc[-1])
    ema_bear = bool(ema20.iloc[-1] < ema50.iloc[-1] and close.iloc[-1] < ema20.iloc[-1])

    if hh and hl and ema_bull:
        trend = "BULLISH"
    elif lh and ll and ema_bear:
        trend = "BEARISH"
    else:
        trend = "NEUTRAL"

    recent_high = (
        float(last_highs[-1]["price"])
        if last_highs
        else float(frame["High"].tail(20).max())
    )
    recent_low = (
        float(last_lows[-1]["price"])
        if last_lows
        else float(frame["Low"].tail(20).min())
    )
    if recent_high <= recent_low:
        recent_high = float(frame["High"].tail(20).max())
        recent_low = float(frame["Low"].tail(20).min())

    return {
        "trend": trend,
        "higher_high": hh,
        "higher_low": hl,
        "lower_high": lh,
        "lower_low": ll,
        "ema20": round(float(ema20.iloc[-1]), 8),
        "ema50": round(float(ema50.iloc[-1]), 8),
        "recent_swing_high": round(recent_high, 8),
        "recent_swing_low": round(recent_low, 8),
        "equilibrium": round((recent_high + recent_low) / 2.0, 8),
        "last_confirmed_high_at": _iso(last_highs[-1]["timestamp"] if last_highs else None),
        "last_confirmed_low_at": _iso(last_lows[-1]["timestamp"] if last_lows else None),
    }


def session_context(timestamp: Any) -> Dict[str, Any]:
    stamp = pd.Timestamp(timestamp)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize(UTC)
    else:
        stamp = stamp.tz_convert(UTC)

    utc_dt = stamp.to_pydatetime()
    london_dt = utc_dt.astimezone(LONDON)
    new_york_dt = utc_dt.astimezone(NEW_YORK)
    sast_dt = utc_dt.astimezone(JOHANNESBURG)

    trading_day = utc_dt.weekday() < 5
    london_active = bool(
        trading_day
        and dt_time(8, 0)
        <= london_dt.time().replace(tzinfo=None)
        < dt_time(17, 0)
    )
    new_york_active = bool(
        trading_day
        and dt_time(8, 0)
        <= new_york_dt.time().replace(tzinfo=None)
        < dt_time(17, 0)
    )
    overlap = london_active and new_york_active

    if overlap:
        name = "LONDON_NEW_YORK_OVERLAP"
    elif london_active:
        name = "LONDON"
    elif new_york_active:
        name = "NEW_YORK"
    else:
        name = "OUTSIDE_EXECUTION_SESSION"

    ny_close = datetime.combine(
        new_york_dt.date(),
        dt_time(17, 0),
        tzinfo=NEW_YORK,
    )
    if ny_close <= new_york_dt:
        ny_close += timedelta(days=1)

    return {
        "active": bool(london_active or new_york_active),
        "name": name,
        "london_active": london_active,
        "new_york_active": new_york_active,
        "overlap": overlap,
        "weekday_gate_pass": trading_day,
        "utc": utc_dt.isoformat(),
        "london_local": london_dt.isoformat(),
        "new_york_local": new_york_dt.isoformat(),
        "south_africa_local": sast_dt.isoformat(),
        "south_africa_timezone": "Africa/Johannesburg",
        "next_new_york_close_utc": ny_close.astimezone(UTC).isoformat(),
        "session_exit_at": ny_close.timestamp(),
        "policy": (
            "Weekday entries only while either the 08:00-17:00 "
            "Europe/London or 08:00-17:00 America/New_York session is open; "
            "IANA timezones handle UK/US daylight-saving changes automatically."
        ),
    }


def _previous_new_york_day_levels(frame: pd.DataFrame) -> Dict[str, Any]:
    local_dates = pd.Series(
        [stamp.tz_convert(NEW_YORK).date() for stamp in frame.index],
        index=frame.index,
    )
    dates = list(dict.fromkeys(local_dates.tolist()))
    if len(dates) < 2:
        return {}
    previous_date = dates[-2]
    previous = frame[local_dates == previous_date]
    if previous.empty:
        return {}
    return {
        "date": str(previous_date),
        "high": float(previous["High"].max()),
        "low": float(previous["Low"].min()),
    }


def _candle_parts(row: pd.Series) -> Dict[str, float]:
    open_ = _safe_float(row.get("Open"))
    high = _safe_float(row.get("High"))
    low = _safe_float(row.get("Low"))
    close = _safe_float(row.get("Close"))
    range_ = max(1e-12, high - low)
    body = abs(close - open_)
    return {
        "body_ratio": body / range_,
        "upper_wick_ratio": (high - max(open_, close)) / range_,
        "lower_wick_ratio": (min(open_, close) - low) / range_,
    }


def _find_sweep(
    frame: pd.DataFrame,
    direction: str,
    atr: pd.Series,
    previous_day: Dict[str, Any],
    lookback: int = 12,
) -> Optional[Dict[str, Any]]:
    prior_low = frame["Low"].shift(1).rolling(20, min_periods=10).min()
    prior_high = frame["High"].shift(1).rolling(20, min_periods=10).max()
    start = max(21, len(frame) - lookback)

    for i in range(len(frame) - 1, start - 1, -1):
        row = frame.iloc[i]
        atr_value = _safe_float(atr.iloc[i])
        if atr_value <= 0:
            continue
        parts = _candle_parts(row)
        candidates: List[Tuple[str, float]] = []

        if direction == "BUY":
            rolling = _safe_float(prior_low.iloc[i])
            if rolling > 0:
                candidates.append(("ROLLING_SELL_SIDE_LIQUIDITY", rolling))
            if previous_day.get("low"):
                candidates.append(("PREVIOUS_NEW_YORK_DAY_LOW", float(previous_day["low"])))

            for source, level in candidates:
                penetration = level - _safe_float(row.get("Low"))
                if (
                    penetration >= atr_value * 0.03
                    and penetration <= atr_value * 1.25
                    and _safe_float(row.get("Close")) > level
                    and parts["lower_wick_ratio"] >= 0.20
                ):
                    return {
                        "direction": "SELL_SIDE_SWEEP",
                        "source": source,
                        "level": round(level, 8),
                        "extreme": round(_safe_float(row.get("Low")), 8),
                        "position": i,
                        "timestamp": frame.index[i],
                        "penetration_atr": round(penetration / atr_value, 4),
                    }
        else:
            rolling = _safe_float(prior_high.iloc[i])
            if rolling > 0:
                candidates.append(("ROLLING_BUY_SIDE_LIQUIDITY", rolling))
            if previous_day.get("high"):
                candidates.append(("PREVIOUS_NEW_YORK_DAY_HIGH", float(previous_day["high"])))

            for source, level in candidates:
                penetration = _safe_float(row.get("High")) - level
                if (
                    penetration >= atr_value * 0.03
                    and penetration <= atr_value * 1.25
                    and _safe_float(row.get("Close")) < level
                    and parts["upper_wick_ratio"] >= 0.20
                ):
                    return {
                        "direction": "BUY_SIDE_SWEEP",
                        "source": source,
                        "level": round(level, 8),
                        "extreme": round(_safe_float(row.get("High")), 8),
                        "position": i,
                        "timestamp": frame.index[i],
                        "penetration_atr": round(penetration / atr_value, 4),
                    }
    return None


def _find_structure_break(
    frame: pd.DataFrame,
    direction: str,
    sweep: Dict[str, Any],
    atr: pd.Series,
    h1_trend: str,
) -> Optional[Dict[str, Any]]:
    sweep_pos = int(sweep["position"])
    reference_start = max(0, sweep_pos - 8)
    reference = frame.iloc[reference_start:sweep_pos]
    if reference.empty:
        return None

    if direction == "BUY":
        level = float(reference["High"].max())
    else:
        level = float(reference["Low"].min())

    for i in range(sweep_pos + 1, len(frame)):
        row = frame.iloc[i]
        atr_value = _safe_float(atr.iloc[i])
        parts = _candle_parts(row)
        if direction == "BUY":
            passed = _safe_float(row.get("Close")) > level + atr_value * 0.03
            candle_aligned = _safe_float(row.get("Close")) > _safe_float(row.get("Open"))
        else:
            passed = _safe_float(row.get("Close")) < level - atr_value * 0.03
            candle_aligned = _safe_float(row.get("Close")) < _safe_float(row.get("Open"))
        if passed and candle_aligned and parts["body_ratio"] >= 0.45:
            continuation = (
                (direction == "BUY" and h1_trend == "BULLISH")
                or (direction == "SELL" and h1_trend == "BEARISH")
            )
            return {
                "type": "BOS" if continuation else "CHOCH",
                "level": round(level, 8),
                "position": i,
                "timestamp": frame.index[i],
                "body_ratio": round(parts["body_ratio"], 4),
            }
    return None


def _find_retest_zone(
    frame: pd.DataFrame,
    direction: str,
    sweep: Dict[str, Any],
    structure: Dict[str, Any],
    atr: pd.Series,
) -> Optional[Dict[str, Any]]:
    sweep_pos = int(sweep["position"])
    break_pos = int(structure["position"])
    zones: List[Dict[str, Any]] = []

    # Last opposite candle before the displacement that broke structure.
    for i in range(break_pos - 1, max(-1, sweep_pos - 5), -1):
        row = frame.iloc[i]
        opposite = (
            _safe_float(row.get("Close")) < _safe_float(row.get("Open"))
            if direction == "BUY"
            else _safe_float(row.get("Close")) > _safe_float(row.get("Open"))
        )
        if opposite:
            zones.append(
                {
                    "kind": "ORDER_BLOCK",
                    "low": float(row["Low"]),
                    "high": float(row["High"]),
                    "origin_position": i,
                    "origin_timestamp": frame.index[i],
                }
            )
            break

    # Three-candle fair-value gap around the displacement leg.
    gap_start = max(2, sweep_pos)
    gap_end = min(len(frame) - 1, break_pos + 2)
    for i in range(gap_start, gap_end + 1):
        atr_value = _safe_float(atr.iloc[i])
        if direction == "BUY":
            lower = float(frame["High"].iloc[i - 2])
            upper = float(frame["Low"].iloc[i])
        else:
            lower = float(frame["High"].iloc[i])
            upper = float(frame["Low"].iloc[i - 2])
        if upper > lower and upper - lower >= atr_value * 0.05:
            zones.append(
                {
                    "kind": "FAIR_VALUE_GAP",
                    "low": lower,
                    "high": upper,
                    "origin_position": i,
                    "origin_timestamp": frame.index[i],
                }
            )

    if not zones:
        return None

    retest_start = break_pos + 1
    for i in range(max(retest_start, len(frame) - 3), len(frame)):
        row_low = float(frame["Low"].iloc[i])
        row_high = float(frame["High"].iloc[i])
        for zone in reversed(zones):
            touched = row_low <= float(zone["high"]) and row_high >= float(zone["low"])
            if touched:
                return {
                    **zone,
                    "retest_position": i,
                    "retest_timestamp": frame.index[i],
                }
    return None


def _confirmation(frame: pd.DataFrame, direction: str) -> Dict[str, Any]:
    row = frame.iloc[-1]
    previous = frame.iloc[-2]
    parts = _candle_parts(row)
    open_ = float(row["Open"])
    close = float(row["Close"])
    prev_open = float(previous["Open"])
    prev_close = float(previous["Close"])

    if direction == "BUY":
        engulfing = (
            close > open_
            and prev_close < prev_open
            and open_ <= prev_close
            and close >= prev_open
        )
        rejection = close > open_ and parts["lower_wick_ratio"] >= 0.35
        displacement = close > open_ and parts["body_ratio"] >= 0.60
    else:
        engulfing = (
            close < open_
            and prev_close > prev_open
            and open_ >= prev_close
            and close <= prev_open
        )
        rejection = close < open_ and parts["upper_wick_ratio"] >= 0.35
        displacement = close < open_ and parts["body_ratio"] >= 0.60

    passed = bool(engulfing or rejection or displacement)
    return {
        "passed": passed,
        "engulfing": engulfing,
        "rejection": rejection,
        "displacement": displacement,
        "body_ratio": round(parts["body_ratio"], 4),
        "upper_wick_ratio": round(parts["upper_wick_ratio"], 4),
        "lower_wick_ratio": round(parts["lower_wick_ratio"], 4),
        "timestamp": frame.index[-1],
    }


def _direction_setup(
    frame: pd.DataFrame,
    direction: str,
    *,
    h4: Dict[str, Any],
    h1: Dict[str, Any],
    session: Dict[str, Any],
    previous_day: Dict[str, Any],
    atr: pd.Series,
) -> Dict[str, Any]:
    desired_h4 = "BULLISH" if direction == "BUY" else "BEARISH"
    h4_aligned = h4.get("trend") == desired_h4

    current = float(frame["Close"].iloc[-1])
    equilibrium = _safe_float(h4.get("equilibrium"), current)
    atr_now = _safe_float(atr.iloc[-1])
    if direction == "BUY":
        location_pass = current <= equilibrium + atr_now * 0.25
    else:
        location_pass = current >= equilibrium - atr_now * 0.25

    sweep = _find_sweep(frame, direction, atr, previous_day)
    structure = (
        _find_structure_break(
            frame,
            direction,
            sweep,
            atr,
            str(h1.get("trend") or "NEUTRAL"),
        )
        if sweep
        else None
    )
    zone = (
        _find_retest_zone(frame, direction, sweep, structure, atr)
        if sweep and structure
        else None
    )
    candle = _confirmation(frame, direction)

    entry = current
    stop_distance = 0.0
    target_distance = 0.0
    room_r = 0.0
    stop = None
    target = None

    if sweep and zone and atr_now > 0:
        if direction == "BUY":
            invalidation = min(float(sweep["extreme"]), float(zone["low"]))
            stop = invalidation - atr_now * 0.15
            stop_distance = entry - stop
            opposing = max(
                _safe_float(previous_day.get("high"), entry),
                _safe_float(h1.get("recent_swing_high"), entry),
                _safe_float(h4.get("recent_swing_high"), entry),
            )
            room_r = (opposing - entry) / stop_distance if stop_distance > 0 else 0.0
            target = entry + stop_distance * 2.0
        else:
            invalidation = max(float(sweep["extreme"]), float(zone["high"]))
            stop = invalidation + atr_now * 0.15
            stop_distance = stop - entry
            opposing = min(
                value
                for value in (
                    _safe_float(previous_day.get("low"), entry),
                    _safe_float(h1.get("recent_swing_low"), entry),
                    _safe_float(h4.get("recent_swing_low"), entry),
                )
                if value > 0
            )
            room_r = (entry - opposing) / stop_distance if stop_distance > 0 else 0.0
            target = entry - stop_distance * 2.0

        target_distance = stop_distance * 2.0

    structural_risk_pass = bool(
        atr_now > 0
        and stop_distance >= atr_now * 0.35
        and stop_distance <= atr_now * 3.0
        and room_r >= 2.0
        and stop is not None
        and target is not None
        and stop > 0
        and target > 0
    )

    checks = {
        "session_active": bool(session.get("active")),
        "h4_structure_aligned": h4_aligned,
        "premium_discount_location": location_pass,
        "liquidity_sweep": sweep is not None,
        "bos_or_choch": structure is not None,
        "order_block_or_fvg_retest": zone is not None,
        "closed_candle_confirmation": bool(candle.get("passed")),
        "minimum_two_r_room": structural_risk_pass,
    }
    passed_count = sum(1 for value in checks.values() if value)
    all_mandatory = passed_count == len(checks)

    confidence = passed_count / float(len(checks))
    if session.get("overlap") and all_mandatory:
        confidence = min(1.0, confidence + 0.02)

    setup_id = None
    if sweep and structure:
        # One liquidity event is one setup.  Do not make the identifier depend
        # on the latest confirmation candle, otherwise the same sweep/structure
        # could be entered again a few candles later after an early close.
        setup_id = "|".join(
            [
                STRATEGY_ID,
                direction,
                _iso(sweep.get("timestamp")) or "NO_SWEEP_TIME",
                _iso(structure.get("timestamp")) or "NO_STRUCTURE_TIME",
            ]
        )

    return {
        "direction": direction if all_mandatory else "WAIT",
        "candidate_direction": direction,
        "all_mandatory": all_mandatory,
        "checks": checks,
        "passed_checks": passed_count,
        "total_checks": len(checks),
        "confidence": round(confidence, 6),
        "sweep": {
            **sweep,
            "timestamp": _iso(sweep.get("timestamp")),
        } if sweep else None,
        "structure_break": {
            **structure,
            "timestamp": _iso(structure.get("timestamp")),
        } if structure else None,
        "retest_zone": {
            **zone,
            "origin_timestamp": _iso(zone.get("origin_timestamp")),
            "retest_timestamp": _iso(zone.get("retest_timestamp")),
        } if zone else None,
        "confirmation": {
            **candle,
            "timestamp": _iso(candle.get("timestamp")),
        },
        "analysis_entry_price": round(entry, 8),
        "structural_stop_price": round(stop, 8) if stop is not None else None,
        "structural_stop_distance": round(stop_distance, 8),
        "take_profit_target_price": round(target, 8) if target is not None else None,
        "target_distance": round(target_distance, 8),
        "target_r": 2.0,
        "room_to_opposing_liquidity_r": round(room_r, 4),
        "setup_id": setup_id,
    }


def analyze_xauusd(completed_15m: pd.DataFrame) -> Dict[str, Any]:
    """Evaluate the complete video-derived XAUUSD setup on closed candles.

    The caller must exclude the still-forming base candle. The function uses
    only completed M15, H1 and H4 bars and returns WAIT unless every mandatory
    course condition is present.
    """
    frame = _normalise_ohlcv(completed_15m)
    atr = _atr(frame)
    if not math.isfinite(_safe_float(atr.iloc[-1])) or _safe_float(atr.iloc[-1]) <= 0:
        raise ValueError("XAUUSD ATR is unavailable on the latest completed candle")

    h1_frame = _resample_completed(frame, "1h", 4)
    h4_frame = _resample_completed(frame, "4h", 16)
    h1 = _structure_state(h1_frame)
    h4 = _structure_state(h4_frame)

    bar_close = frame.index[-1] + _base_delta(frame)
    session = session_context(bar_close)
    previous_day = _previous_new_york_day_levels(frame)

    buy = _direction_setup(
        frame,
        "BUY",
        h4=h4,
        h1=h1,
        session=session,
        previous_day=previous_day,
        atr=atr,
    )
    sell = _direction_setup(
        frame,
        "SELL",
        h4=h4,
        h1=h1,
        session=session,
        previous_day=previous_day,
        atr=atr,
    )

    qualified = [row for row in (buy, sell) if row.get("all_mandatory")]
    if len(qualified) == 1:
        selected = qualified[0]
        direction = str(selected["candidate_direction"])
    else:
        selected = max(
            (buy, sell),
            key=lambda row: (
                int(row.get("passed_checks") or 0),
                _safe_float(row.get("confidence")),
            ),
        )
        direction = "WAIT"

    rejection_reasons = [
        key.upper()
        for key, passed in (selected.get("checks") or {}).items()
        if not passed
    ]
    if len(qualified) > 1:
        rejection_reasons.append("CONFLICTING_BUY_AND_SELL_SETUPS")

    return {
        "version": VERSION,
        "strategy_id": STRATEGY_ID,
        "strategy_name": STRATEGY_NAME,
        "direction": direction,
        "quant_confidence": (
            _safe_float(selected.get("confidence"))
            if direction in {"BUY", "SELL"}
            else min(0.27, _safe_float(selected.get("confidence")) * 0.27)
        ),
        "directional_confidence": _safe_float(selected.get("confidence")),
        "strategy_branch": "LIQUIDITY_STRUCTURE_CONFLUENCE",
        "strategy_reason": (
            "H4 trend + premium/discount + liquidity sweep + BOS/CHoCH + "
            "OB/FVG retest + closed-candle confirmation + >=2R room during "
            "London/New York execution hours"
        ),
        "rejection_reasons": list(dict.fromkeys(rejection_reasons)),
        "selected_setup": selected,
        "buy_setup": buy,
        "sell_setup": sell,
        "h4_structure": h4,
        "h1_structure": h1,
        "previous_new_york_day": previous_day,
        "session": session,
        "atr_15m": round(_safe_float(atr.iloc[-1]), 8),
        "closed_candle_timestamp": _iso(frame.index[-1]),
        "closed_candle_end_timestamp": _iso(bar_close),
        "analysis_timeframes": {
            "higher_timeframe": "H4",
            "confirmation_timeframe": "H1",
            "entry_timeframe": "M15",
            "m1_and_15s_disabled": True,
        },
        "setup_id": selected.get("setup_id") if direction in {"BUY", "SELL"} else None,
        "structural_stop_price": selected.get("structural_stop_price"),
        "structural_stop_distance": selected.get("structural_stop_distance"),
        "take_profit_target_price": selected.get("take_profit_target_price"),
        "target_distance": selected.get("target_distance"),
        "target_r": selected.get("target_r"),
        "room_to_opposing_liquidity_r": selected.get("room_to_opposing_liquidity_r"),
        "session_exit_at": session.get("session_exit_at"),
        "max_hold_seconds": 12 * 60 * 60,
        "forming_candle_ignored": True,
        "live_money_execution": False,
    }
