from __future__ import annotations

import json
import os
from datetime import datetime, time as dt_time, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

from xauusd_liquidity_strategy import (
    _atr,
    _base_delta,
    _confirmed_pivots,
    _iso,
    _normalise_ohlcv,
    _resample_completed,
    _safe_float,
    _structure_state,
)


VERSION = "6.11-fx-liquidity-lines-v1"
STRATEGY_ID = "FX_LIQUIDITY_LINES_V1"
STRATEGY_NAME = "Forex Multi-Session Liquidity / Lines"

UTC = ZoneInfo("UTC")
JOHANNESBURG = ZoneInfo("Africa/Johannesburg")

SESSION_DEFINITIONS: Dict[str, Dict[str, Any]] = {
    "LONDON": {
        "timezone": ZoneInfo("Europe/London"),
        "open": dt_time(8, 0),
        "close": dt_time(17, 0),
        "currencies": {"EUR", "GBP", "CHF"},
    },
    "NEW_YORK": {
        "timezone": ZoneInfo("America/New_York"),
        "open": dt_time(8, 0),
        "close": dt_time(17, 0),
        "currencies": {"USD", "CAD"},
    },
    "TOKYO": {
        "timezone": ZoneInfo("Asia/Tokyo"),
        "open": dt_time(9, 0),
        "close": dt_time(18, 0),
        "currencies": {"JPY"},
    },
    "SYDNEY": {
        "timezone": ZoneInfo("Australia/Sydney"),
        "open": dt_time(8, 0),
        "close": dt_time(17, 0),
        "currencies": {"AUD", "NZD"},
    },
}

CURRENCY_SESSION = {
    currency: session
    for session, definition in SESSION_DEFINITIONS.items()
    for currency in definition["currencies"]
}

LIQUID_FOREX_PAIRS: Tuple[str, ...] = (
    "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDJPY", "USDCAD", "USDCHF",
    "EURGBP", "EURJPY", "EURCHF", "EURCAD", "EURAUD", "EURNZD",
    "GBPJPY", "GBPCHF", "GBPCAD", "GBPAUD", "GBPNZD",
    "AUDJPY", "AUDCHF", "AUDCAD", "AUDNZD",
    "NZDJPY", "NZDCHF", "NZDCAD",
    "CADJPY", "CADCHF", "CHFJPY",
)


def normalise_pair(symbol: Any) -> str:
    clean = "".join(ch for ch in str(symbol or "").upper() if ch.isalpha())
    if clean.endswith("X") and len(clean) == 7:
        clean = clean[:-1]
    if len(clean) != 6:
        raise ValueError(f"Unsupported forex symbol: {symbol}")
    return clean


def pair_currencies(symbol: Any) -> Tuple[str, str]:
    clean = normalise_pair(symbol)
    return clean[:3], clean[3:]


def _as_utc(timestamp: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(timestamp)
    if stamp.tzinfo is None:
        return stamp.tz_localize(UTC)
    return stamp.tz_convert(UTC)


def _session_close(local_dt: datetime, definition: Dict[str, Any]) -> datetime:
    close = datetime.combine(
        local_dt.date(),
        definition["close"],
        tzinfo=definition["timezone"],
    )
    if close <= local_dt:
        close += timedelta(days=1)
    return close


def forex_session_context(timestamp: Any, symbol: Any) -> Dict[str, Any]:
    """Return only the trading sessions relevant to the pair's geography.

    Session clocks use IANA timezones rather than fixed UTC offsets, so London,
    New York and Sydney daylight-saving changes are handled automatically.
    """
    pair = normalise_pair(symbol)
    currencies = set(pair_currencies(pair))
    stamp = _as_utc(timestamp)
    utc_dt = stamp.to_pydatetime()
    relevant: List[str] = []
    active: List[str] = []
    local_times: Dict[str, str] = {}
    active_closes: List[float] = []
    session_states: Dict[str, bool] = {}
    local_weekday_states: Dict[str, bool] = {}

    for name, definition in SESSION_DEFINITIONS.items():
        if not currencies.intersection(definition["currencies"]):
            continue
        relevant.append(name)
        local_dt = utc_dt.astimezone(definition["timezone"])
        local_times[name.lower()] = local_dt.isoformat()
        local_weekday = local_dt.weekday() < 5
        local_weekday_states[f"{name.lower()}_weekday"] = local_weekday
        is_active = bool(
            local_weekday
            and definition["open"]
            <= local_dt.time().replace(tzinfo=None)
            < definition["close"]
        )
        session_states[f"{name.lower()}_active"] = is_active
        if is_active:
            active.append(name)
            active_closes.append(_session_close(local_dt, definition).timestamp())

    overlap = len(active) >= 2
    if overlap:
        name = "_".join(active) + "_OVERLAP"
    elif active:
        name = active[0]
    else:
        name = "OUTSIDE_PAIR_RELEVANT_SESSION"

    return {
        "active": bool(active),
        "name": name,
        "pair": pair,
        "pair_currencies": sorted(currencies),
        "relevant_sessions": relevant,
        "active_sessions": active,
        "overlap": overlap,
        "london_new_york_overlap": (
            "LONDON" in active and "NEW_YORK" in active
        ),
        "weekday_gate_pass": any(local_weekday_states.values()),
        "utc": utc_dt.isoformat(),
        "south_africa_local": utc_dt.astimezone(JOHANNESBURG).isoformat(),
        "south_africa_timezone": "Africa/Johannesburg",
        "session_exit_at": max(active_closes) if active_closes else None,
        "local_times": local_times,
        "local_weekdays": local_weekday_states,
        **session_states,
        "policy": (
            "Weekday entries require an open session associated with either "
            "currency in the pair: EUR/GBP/CHF=London, USD/CAD=New York, "
            "JPY=Tokyo, AUD/NZD=Sydney. IANA timezones are DST-aware."
        ),
    }


def _parse_news_events() -> Tuple[List[Dict[str, Any]], Optional[str]]:
    raw = str(os.getenv("JASONG_HIGH_IMPACT_NEWS_WINDOWS_JSON", "")).strip()
    if not raw:
        return [], None
    try:
        data = json.loads(raw)
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"
    if not isinstance(data, list):
        return [], "news window configuration must be a JSON list"
    if any(not isinstance(item, dict) for item in data):
        return [], "each news window must be a JSON object"
    return data, None


def news_blackout_context(timestamp: Any, symbol: Any) -> Dict[str, Any]:
    """Check optional high-impact event windows for either pair currency.

    Events accept ``currency`` plus ``start``/``end`` ISO timestamps, or a
    single ``timestamp`` with optional ``before_minutes``/``after_minutes``.
    Invalid configured data fails closed. With no feed configured the status is
    explicit; operators can make a feed mandatory with FOREX_NEWS_GUARD_REQUIRED.
    """
    stamp = _as_utc(timestamp)
    currencies = set(pair_currencies(symbol))
    events, error = _parse_news_events()
    required = str(os.getenv("FOREX_NEWS_GUARD_REQUIRED", "false")).lower() in {
        "1", "true", "yes", "on",
    }
    active_events: List[Dict[str, Any]] = []

    if error:
        return {
            "clear": False,
            "configured": True,
            "required": required,
            "error": error,
            "active_events": [],
            "policy": "Invalid news configuration fails closed.",
        }

    for event in events:
        currency = str(event.get("currency") or "").upper().strip()
        impact = str(event.get("impact") or "HIGH").upper().strip()
        if currency not in currencies or impact not in {"HIGH", "RED", "3"}:
            continue
        try:
            if event.get("start") and event.get("end"):
                start = _as_utc(event["start"])
                end = _as_utc(event["end"])
            else:
                centre = _as_utc(event["timestamp"])
                before = max(0, int(event.get("before_minutes", 30)))
                after = max(0, int(event.get("after_minutes", 30)))
                start = centre - pd.Timedelta(minutes=before)
                end = centre + pd.Timedelta(minutes=after)
        except Exception as exc:
            return {
                "clear": False,
                "configured": True,
                "required": required,
                "error": f"invalid {currency} news window: {type(exc).__name__}: {exc}",
                "active_events": [],
                "policy": "Invalid relevant news configuration fails closed.",
            }
        if end < start:
            return {
                "clear": False,
                "configured": True,
                "required": required,
                "error": f"invalid {currency} news window: end precedes start",
                "active_events": [],
                "policy": "Invalid relevant news configuration fails closed.",
            }
        if start <= stamp <= end:
            active_events.append(
                {
                    "currency": currency,
                    "name": event.get("name") or "HIGH_IMPACT_EVENT",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                }
            )

    configured = bool(events)
    clear = not active_events and (configured or not required)
    return {
        "clear": clear,
        "configured": configured,
        "required": required,
        "error": None,
        "active_events": active_events,
        "policy": (
            "No new FX entry during a configured high-impact window for either "
            "currency; entries use the post-news price reaction, not prediction."
        ),
    }


def _candle_parts(row: pd.Series) -> Dict[str, float]:
    open_ = _safe_float(row.get("Open"))
    high = _safe_float(row.get("High"))
    low = _safe_float(row.get("Low"))
    close = _safe_float(row.get("Close"))
    span = max(1e-12, high - low)
    return {
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "body": abs(close - open_),
        "body_ratio": abs(close - open_) / span,
        "upper_wick_ratio": (high - max(open_, close)) / span,
        "lower_wick_ratio": (min(open_, close) - low) / span,
    }


def candlestick_confirmation(frame: pd.DataFrame, direction: str) -> Dict[str, Any]:
    """Classify the closed-candle confirmations shown in the supplied course."""
    if len(frame) < 4:
        return {"passed": False, "patterns": [], "timestamp": None}
    current = _candle_parts(frame.iloc[-1])
    previous = _candle_parts(frame.iloc[-2])
    third = _candle_parts(frame.iloc[-3])
    names: List[str] = []
    bullish = direction == "BUY"

    if bullish:
        if (
            current["close"] > current["open"]
            and previous["close"] < previous["open"]
            and current["open"] <= previous["close"]
            and current["close"] >= previous["open"]
        ):
            names.append("BULLISH_ENGULFING")
        if current["close"] > current["open"] and current["lower_wick_ratio"] >= 0.40:
            names.append("HAMMER_REJECTION")
        if (
            previous["close"] < previous["open"]
            and current["close"] > current["open"]
            and current["close"] >= (previous["open"] + previous["close"]) / 2.0
        ):
            names.append("PIERCING_LINE")
        if (
            third["close"] < third["open"]
            and previous["body_ratio"] <= 0.35
            and current["close"] > current["open"]
            and current["close"] > (third["open"] + third["close"]) / 2.0
        ):
            names.append("MORNING_STAR")
    else:
        if (
            current["close"] < current["open"]
            and previous["close"] > previous["open"]
            and current["open"] >= previous["close"]
            and current["close"] <= previous["open"]
        ):
            names.append("BEARISH_ENGULFING")
        if current["close"] < current["open"] and current["upper_wick_ratio"] >= 0.40:
            names.append("SHOOTING_STAR_REJECTION")
        if (
            previous["close"] > previous["open"]
            and current["close"] < current["open"]
            and current["close"] <= (previous["open"] + previous["close"]) / 2.0
        ):
            names.append("DARK_CLOUD_COVER")
        if (
            third["close"] > third["open"]
            and previous["body_ratio"] <= 0.35
            and current["close"] < current["open"]
            and current["close"] < (third["open"] + third["close"]) / 2.0
        ):
            names.append("EVENING_STAR")

    aligned = (
        current["close"] > current["open"] if bullish
        else current["close"] < current["open"]
    )
    if aligned and current["body_ratio"] >= 0.65:
        names.append("BULLISH_MARUBOZU_DISPLACEMENT" if bullish else "BEARISH_MARUBOZU_DISPLACEMENT")

    inside = (
        previous["high"] < third["high"]
        and previous["low"] > third["low"]
    )
    inside_break = (
        inside
        and (
            current["close"] > previous["high"] if bullish
            else current["close"] < previous["low"]
        )
    )
    if inside_break:
        names.append("INSIDE_BAR_BREAKOUT")

    return {
        "passed": bool(names),
        "patterns": list(dict.fromkeys(names)),
        "timestamp": frame.index[-1],
        "body_ratio": round(current["body_ratio"], 4),
        "upper_wick_ratio": round(current["upper_wick_ratio"], 4),
        "lower_wick_ratio": round(current["lower_wick_ratio"], 4),
    }


def _trendline_from_points(
    points: Sequence[Dict[str, Any]],
    latest_position: int,
    latest_close: float,
    atr_value: float,
    direction: str,
) -> Dict[str, Any]:
    if len(points) < 2:
        return {
            "available": False,
            "aligned": False,
            "intact": False,
            "touched": False,
            "reason": "TWO_CONFIRMED_TOUCHES_REQUIRED",
        }
    first, second = points[-2], points[-1]
    x1, x2 = int(first["position"]), int(second["position"])
    if x2 <= x1:
        return {"available": False, "aligned": False, "intact": False, "touched": False}
    slope = (float(second["price"]) - float(first["price"])) / float(x2 - x1)
    projected = float(second["price"]) + slope * float(latest_position - x2)
    tolerance = max(atr_value * 0.25, abs(latest_close) * 0.00005)
    aligned = slope >= 0.0 if direction == "BUY" else slope <= 0.0
    intact = (
        latest_close >= projected - tolerance
        if direction == "BUY"
        else latest_close <= projected + tolerance
    )
    touched = abs(latest_close - projected) <= tolerance * 1.5
    return {
        "available": True,
        "aligned": bool(aligned),
        "intact": bool(intact),
        "touched": bool(touched),
        "slope_per_bar": round(slope, 10),
        "projected_level": round(projected, 10),
        "tolerance": round(tolerance, 10),
        "first_touch": {"timestamp": _iso(first.get("timestamp")), "price": round(float(first["price"]), 10)},
        "second_touch": {"timestamp": _iso(second.get("timestamp")), "price": round(float(second["price"]), 10)},
    }


def trendline_context(frame: pd.DataFrame, direction: str) -> Dict[str, Any]:
    highs, lows = _confirmed_pivots(frame)
    points = lows if direction == "BUY" else highs
    atr_value = _safe_float(_atr(frame).iloc[-1])
    return _trendline_from_points(
        points,
        len(frame) - 1,
        float(frame["Close"].iloc[-1]),
        atr_value,
        direction,
    )


def _period_levels(frame: pd.DataFrame, timezone: ZoneInfo, frequency: str) -> Dict[str, Any]:
    local = frame.copy()
    local.index = local.index.tz_convert(timezone)
    if frequency == "day":
        keys = [stamp.date() for stamp in local.index]
    elif frequency == "week":
        keys = [(stamp.isocalendar().year, stamp.isocalendar().week) for stamp in local.index]
    else:
        keys = [(stamp.year, stamp.month) for stamp in local.index]
    distinct = list(dict.fromkeys(keys))
    if len(distinct) < 2:
        return {}
    previous_key = distinct[-2]
    mask = pd.Series([key == previous_key for key in keys], index=local.index)
    previous = local[mask.to_numpy()]
    if previous.empty:
        return {}
    return {
        "period": str(previous_key),
        "high": float(previous["High"].max()),
        "low": float(previous["Low"].min()),
    }


def liquidity_levels(frame: pd.DataFrame, symbol: Any) -> Dict[str, Any]:
    base, quote = pair_currencies(symbol)
    # Anchor calendar levels to the base currency's trading geography. Every
    # supported currency has a mapping; the quote fallback keeps this safe if
    # the universe is extended later.
    primary_name = CURRENCY_SESSION.get(base, CURRENCY_SESSION.get(quote, "LONDON"))
    timezone = SESSION_DEFINITIONS[primary_name]["timezone"]
    highs, lows = _confirmed_pivots(frame)
    equal_tolerance = max(_safe_float(_atr(frame).iloc[-1]) * 0.15, 1e-10)

    equal_high = None
    equal_low = None
    for points, target in ((highs[-8:], "high"), (lows[-8:], "low")):
        found = None
        for left, right in zip(points, points[1:]):
            if abs(float(left["price"]) - float(right["price"])) <= equal_tolerance:
                found = (float(left["price"]) + float(right["price"])) / 2.0
        if target == "high":
            equal_high = found
        else:
            equal_low = found

    return {
        "primary_session": primary_name,
        "previous_day": _period_levels(frame, timezone, "day"),
        "previous_week": _period_levels(frame, timezone, "week"),
        "previous_month": _period_levels(frame, timezone, "month"),
        "equal_high": equal_high,
        "equal_low": equal_low,
        "old_high": float(highs[-1]["price"]) if highs else None,
        "old_low": float(lows[-1]["price"]) if lows else None,
    }


def _sweep_candidates(levels: Dict[str, Any], direction: str) -> List[Tuple[str, float]]:
    wanted = "low" if direction == "BUY" else "high"
    rows: List[Tuple[str, float]] = []
    for period in ("previous_day", "previous_week", "previous_month"):
        value = _safe_float((levels.get(period) or {}).get(wanted))
        if value > 0:
            rows.append((f"{period.upper()}_{wanted.upper()}", value))
    for name in (
        "equal_low" if direction == "BUY" else "equal_high",
        "old_low" if direction == "BUY" else "old_high",
    ):
        value = _safe_float(levels.get(name))
        if value > 0:
            rows.append((name.upper(), value))
    return rows


def _find_liquidity_sweep(
    frame: pd.DataFrame,
    direction: str,
    atr: pd.Series,
    levels: Dict[str, Any],
    lookback: int = 16,
) -> Optional[Dict[str, Any]]:
    rolling_low = frame["Low"].shift(1).rolling(20, min_periods=10).min()
    rolling_high = frame["High"].shift(1).rolling(20, min_periods=10).max()
    start = max(21, len(frame) - lookback)
    static = _sweep_candidates(levels, direction)

    for i in range(len(frame) - 1, start - 1, -1):
        row = _candle_parts(frame.iloc[i])
        atr_value = _safe_float(atr.iloc[i])
        if atr_value <= 0:
            continue
        rolling = _safe_float(rolling_low.iloc[i] if direction == "BUY" else rolling_high.iloc[i])
        candidates = list(static)
        if rolling > 0:
            candidates.insert(0, (
                "ROLLING_SELL_SIDE_LIQUIDITY" if direction == "BUY" else "ROLLING_BUY_SIDE_LIQUIDITY",
                rolling,
            ))
        for source, level in candidates:
            if direction == "BUY":
                penetration = level - row["low"]
                reclaimed = row["close"] > level and row["lower_wick_ratio"] >= 0.20
                extreme = row["low"]
                sweep_type = "SELL_SIDE_SWEEP"
            else:
                penetration = row["high"] - level
                reclaimed = row["close"] < level and row["upper_wick_ratio"] >= 0.20
                extreme = row["high"]
                sweep_type = "BUY_SIDE_SWEEP"
            if atr_value * 0.03 <= penetration <= atr_value * 1.50 and reclaimed:
                return {
                    "direction": sweep_type,
                    "source": source,
                    "level": round(level, 10),
                    "extreme": round(extreme, 10),
                    "position": i,
                    "timestamp": frame.index[i],
                    "penetration_atr": round(penetration / atr_value, 4),
                }
    return None


def _find_structure_shift(
    frame: pd.DataFrame,
    direction: str,
    sweep: Dict[str, Any],
    atr: pd.Series,
    h1_trend: str,
) -> Optional[Dict[str, Any]]:
    sweep_pos = int(sweep["position"])
    reference = frame.iloc[max(0, sweep_pos - 8):sweep_pos]
    if reference.empty:
        return None
    level = float(reference["High"].max() if direction == "BUY" else reference["Low"].min())
    for i in range(sweep_pos + 1, len(frame)):
        row = _candle_parts(frame.iloc[i])
        atr_value = _safe_float(atr.iloc[i])
        broken = (
            row["close"] > level + atr_value * 0.03
            if direction == "BUY"
            else row["close"] < level - atr_value * 0.03
        )
        aligned = row["close"] > row["open"] if direction == "BUY" else row["close"] < row["open"]
        if broken and aligned and row["body_ratio"] >= 0.45:
            continuation = (
                (direction == "BUY" and h1_trend == "BULLISH")
                or (direction == "SELL" and h1_trend == "BEARISH")
            )
            return {
                "type": "BOS" if continuation else "CHOCH_CISD_MSS",
                "level": round(level, 10),
                "position": i,
                "timestamp": frame.index[i],
                "body_ratio": round(row["body_ratio"], 4),
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
    for i in range(break_pos - 1, max(-1, sweep_pos - 5), -1):
        row = frame.iloc[i]
        opposite = (
            float(row["Close"]) < float(row["Open"])
            if direction == "BUY" else float(row["Close"]) > float(row["Open"])
        )
        if opposite:
            zones.append({
                "kind": "ORDER_BLOCK",
                "low": float(row["Low"]),
                "high": float(row["High"]),
                "origin_position": i,
                "origin_timestamp": frame.index[i],
            })
            break
    for i in range(max(2, sweep_pos), min(len(frame) - 1, break_pos + 2) + 1):
        if direction == "BUY":
            lower, upper = float(frame["High"].iloc[i - 2]), float(frame["Low"].iloc[i])
        else:
            lower, upper = float(frame["High"].iloc[i]), float(frame["Low"].iloc[i - 2])
        if upper > lower and upper - lower >= _safe_float(atr.iloc[i]) * 0.05:
            zones.append({
                "kind": "FAIR_VALUE_GAP",
                "low": lower,
                "high": upper,
                "origin_position": i,
                "origin_timestamp": frame.index[i],
            })
    if not zones:
        return None
    for i in range(max(break_pos + 1, len(frame) - 4), len(frame)):
        low, high = float(frame["Low"].iloc[i]), float(frame["High"].iloc[i])
        for zone in reversed(zones):
            if low <= float(zone["high"]) and high >= float(zone["low"]):
                return {
                    **zone,
                    "retest_position": i,
                    "retest_timestamp": frame.index[i],
                }
    return None


def _opposing_liquidity(levels: Dict[str, Any], direction: str, fallback: float) -> float:
    values: List[float] = []
    wanted = "high" if direction == "BUY" else "low"
    for period in ("previous_day", "previous_week", "previous_month"):
        value = _safe_float((levels.get(period) or {}).get(wanted))
        if value > 0:
            values.append(value)
    for name in (
        "equal_high" if direction == "BUY" else "equal_low",
        "old_high" if direction == "BUY" else "old_low",
    ):
        value = _safe_float(levels.get(name))
        if value > 0:
            values.append(value)
    viable = [value for value in values if value > fallback] if direction == "BUY" else [value for value in values if value < fallback]
    if not viable:
        return fallback
    return min(viable) if direction == "BUY" else max(viable)


def _direction_setup(
    frame: pd.DataFrame,
    direction: str,
    *,
    h4_frame: pd.DataFrame,
    h4: Dict[str, Any],
    h1: Dict[str, Any],
    session: Dict[str, Any],
    news: Dict[str, Any],
    levels: Dict[str, Any],
    atr: pd.Series,
) -> Dict[str, Any]:
    desired = "BULLISH" if direction == "BUY" else "BEARISH"
    current = float(frame["Close"].iloc[-1])
    atr_now = _safe_float(atr.iloc[-1])
    equilibrium = _safe_float(h4.get("equilibrium"), current)
    external_line = trendline_context(h4_frame, direction)
    internal_line = trendline_context(frame.tail(120), direction)
    location_pass = (
        current <= equilibrium + atr_now * 0.25
        if direction == "BUY" else current >= equilibrium - atr_now * 0.25
    )
    sweep = _find_liquidity_sweep(frame, direction, atr, levels)
    structure = _find_structure_shift(
        frame, direction, sweep, atr, str(h1.get("trend") or "NEUTRAL")
    ) if sweep else None
    zone = _find_retest_zone(frame, direction, sweep, structure, atr) if sweep and structure else None
    candle = candlestick_confirmation(frame, direction)

    entry = current
    stop = None
    stop_distance = 0.0
    room_r = 0.0
    target = None
    if sweep and zone and atr_now > 0:
        if direction == "BUY":
            stop = min(float(sweep["extreme"]), float(zone["low"])) - atr_now * 0.15
            stop_distance = entry - stop
        else:
            stop = max(float(sweep["extreme"]), float(zone["high"])) + atr_now * 0.15
            stop_distance = stop - entry
        opposing = _opposing_liquidity(levels, direction, entry)
        if stop_distance > 0:
            room_r = (
                (opposing - entry) / stop_distance
                if direction == "BUY" else (entry - opposing) / stop_distance
            )
            target = entry + stop_distance * 2.0 if direction == "BUY" else entry - stop_distance * 2.0

    structural_risk_pass = bool(
        atr_now > 0
        and atr_now * 0.25 <= stop_distance <= atr_now * 3.0
        and room_r >= 2.0
        and stop is not None and target is not None
        and stop > 0 and target > 0
    )
    external_pass = bool(
        external_line.get("available")
        and external_line.get("aligned")
        and external_line.get("intact")
    )
    checks = {
        "pair_relevant_session_active": bool(session.get("active")),
        "high_impact_news_clear": bool(news.get("clear")),
        "h4_structure_aligned": h4.get("trend") == desired,
        "external_trendline_aligned_and_intact": external_pass,
        "premium_discount_location": bool(location_pass),
        "liquidity_sweep": sweep is not None,
        "bos_choch_cisd_or_mss": structure is not None,
        "order_block_fvg_or_break_retest": zone is not None,
        "closed_candlestick_confirmation": bool(candle.get("passed")),
        "minimum_two_r_room": structural_risk_pass,
    }
    passed = sum(bool(value) for value in checks.values())
    all_mandatory = passed == len(checks)
    confluence_bonus = 0.02 if internal_line.get("touched") else 0.0
    overlap_bonus = 0.02 if session.get("overlap") else 0.0
    confidence = min(1.0, passed / len(checks) + confluence_bonus + overlap_bonus)
    setup_id = None
    if sweep and structure:
        setup_id = "|".join([
            STRATEGY_ID,
            normalise_pair(session.get("pair")),
            direction,
            _iso(sweep.get("timestamp")) or "NO_SWEEP_TIME",
            _iso(structure.get("timestamp")) or "NO_STRUCTURE_TIME",
        ])

    def serialise(value: Optional[Dict[str, Any]], *timestamp_fields: str) -> Optional[Dict[str, Any]]:
        if not value:
            return None
        out = dict(value)
        for field in timestamp_fields:
            if field in out:
                out[field] = _iso(out[field])
        return out

    return {
        "direction": direction if all_mandatory else "WAIT",
        "candidate_direction": direction,
        "all_mandatory": all_mandatory,
        "checks": checks,
        "passed_checks": passed,
        "total_checks": len(checks),
        "confidence": round(confidence, 6),
        "external_trendline": external_line,
        "internal_trendline": internal_line,
        "internal_line_entry_confluence": bool(internal_line.get("touched")),
        "sweep": serialise(sweep, "timestamp"),
        "structure_break": serialise(structure, "timestamp"),
        "retest_zone": serialise(zone, "origin_timestamp", "retest_timestamp"),
        "confirmation": serialise(candle, "timestamp"),
        "analysis_entry_price": round(entry, 10),
        "structural_stop_price": round(stop, 10) if stop is not None else None,
        "structural_stop_distance": round(stop_distance, 10),
        "take_profit_target_price": round(target, 10) if target is not None else None,
        "target_distance": round(stop_distance * 2.0, 10),
        "target_r": 2.0,
        "room_to_opposing_liquidity_r": round(room_r, 4),
        "setup_id": setup_id,
    }


def analyze_forex(completed_15m: pd.DataFrame, symbol: Any) -> Dict[str, Any]:
    """Evaluate a closed-candle H4/H1/M15 forex liquidity/lines setup."""
    pair = normalise_pair(symbol)
    if pair not in LIQUID_FOREX_PAIRS:
        raise ValueError(f"Forex pair is not in the supported liquid universe: {pair}")
    frame = _normalise_ohlcv(completed_15m)
    atr = _atr(frame)
    if _safe_float(atr.iloc[-1]) <= 0:
        raise ValueError("Forex ATR is unavailable on the latest completed candle")
    h1_frame = _resample_completed(frame, "1h", 4)
    h4_frame = _resample_completed(frame, "4h", 16)
    h1 = _structure_state(h1_frame)
    h4 = _structure_state(h4_frame)
    bar_close = frame.index[-1] + _base_delta(frame)
    session = forex_session_context(bar_close, pair)
    news = news_blackout_context(bar_close, pair)
    levels = liquidity_levels(frame, pair)

    buy = _direction_setup(
        frame, "BUY", h4_frame=h4_frame, h4=h4, h1=h1,
        session=session, news=news, levels=levels, atr=atr,
    )
    sell = _direction_setup(
        frame, "SELL", h4_frame=h4_frame, h4=h4, h1=h1,
        session=session, news=news, levels=levels, atr=atr,
    )
    qualified = [row for row in (buy, sell) if row.get("all_mandatory")]
    if len(qualified) == 1:
        selected = qualified[0]
        direction = str(selected["candidate_direction"])
    else:
        selected = max(
            (buy, sell),
            key=lambda row: (int(row.get("passed_checks") or 0), _safe_float(row.get("confidence"))),
        )
        direction = "WAIT"
    rejection_reasons = [
        key.upper() for key, value in (selected.get("checks") or {}).items() if not value
    ]
    if len(qualified) > 1:
        rejection_reasons.append("CONFLICTING_BUY_AND_SELL_SETUPS")

    return {
        "version": VERSION,
        "strategy_id": STRATEGY_ID,
        "strategy_name": STRATEGY_NAME,
        "symbol": pair,
        "direction": direction,
        "quant_confidence": (
            _safe_float(selected.get("confidence"))
            if direction in {"BUY", "SELL"}
            else min(0.27, _safe_float(selected.get("confidence")) * 0.27)
        ),
        "directional_confidence": _safe_float(selected.get("confidence")),
        "strategy_branch": "FX_LIQUIDITY_LINES_CONFLUENCE",
        "strategy_reason": (
            "H4 market structure + external trendline + premium/discount + "
            "liquidity sweep + BOS/CHoCH/CISD/MSS + OB/FVG retest + closed "
            "candlestick confirmation + >=2R room in a pair-relevant session"
        ),
        "rejection_reasons": list(dict.fromkeys(rejection_reasons)),
        "selected_setup": selected,
        "buy_setup": buy,
        "sell_setup": sell,
        "h4_structure": h4,
        "h1_structure": h1,
        "liquidity_levels": levels,
        "session": session,
        "news_guard": news,
        "atr_15m": round(_safe_float(atr.iloc[-1]), 10),
        "closed_candle_timestamp": _iso(frame.index[-1]),
        "closed_candle_end_timestamp": _iso(bar_close),
        "analysis_timeframes": {
            "higher_timeframe": "H4",
            "confirmation_timeframe": "H1",
            "entry_timeframe": "M15",
            "forming_candle_ignored": True,
        },
        "candlestick_analyzed": True,
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
