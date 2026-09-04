from __future__ import annotations

"""Jasong V6.12 adaptive FX session-momentum policy.

This module deliberately overlays V6.11 at process start instead of deleting the
previous strategy.  That keeps rollback simple while changing the active IG DEMO
forex path to the agreed mandatory-core + optional-confluence model.

Signal eligibility:
- H4 and H1 directional bias aligned.
- M15 BOS/CHoCH/CISD/MSS displacement.
- Pair-relevant trading session active.
- At least 2 of 5 optional confirmations:
  external H4 line, premium/discount, liquidity sweep, OB/FVG/break-zone retest,
  closed-candlestick confirmation.
- Overall setup confidence >= 30%.
- Minimum available target >= 0.3R.

Grade A: >=3/5 optional confirmations and >=0.5R.
Grade B: >=2/5 optional confirmations and >=0.3R.

News and IG dealing safety are not allowed to veto the technical *signal*, but
remain hard pre-execution protections.  This distinction is intentional: the
system can surface/learn a valid setup while still refusing to submit a DEMO
order during a high-impact blackout, on a non-tradeable market, with an unsafe
spread/quote, or when the broker minimum size cannot be used safely.
"""

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

import forex_liquidity_lines_strategy as legacy_fx


VERSION = "6.12-adaptive-fx-session-momentum-v1"
STRATEGY_ID = "FX_LIQUIDITY_LINES_V2_ADAPTIVE"
STRATEGY_NAME = "V6.12 Adaptive FX Session Momentum"
ENGINE_VERSION = "6.12-fx-adaptive-xau-active-v1"
EVIDENCE_SCHEMA_VERSION = 7

QUANT_MIN_CONFIDENCE = 0.20
MODEL_AI_MIN_CONFIDENCE = 0.30
SETUP_CONFIDENCE_MIN = 0.30
MARKET_STRUCTURE_MIN = 0.60
OPTIONAL_MIN = 2
A_GRADE_OPTIONAL_MIN = 3
MIN_TARGET_R = 0.30
A_GRADE_MIN_R = 0.50
PREFERRED_TARGET_R = 2.00

OPTIONAL_KEYS: Tuple[str, ...] = (
    "external_trendline_aligned_and_intact",
    "premium_discount_location",
    "liquidity_sweep",
    "order_block_fvg_or_break_retest",
    "closed_candlestick_confirmation",
)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except Exception:
        return default


def _adaptive_structure_shift(
    frame: pd.DataFrame,
    direction: str,
    atr: pd.Series,
    h1_trend: str,
    sweep: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Find a displacement break without making a liquidity sweep mandatory."""
    if sweep:
        legacy = legacy_fx._find_structure_shift(
            frame,
            direction,
            sweep,
            atr,
            h1_trend,
        )
        if legacy:
            out = dict(legacy)
            out["source"] = "SWEEP_ANCHORED_STRUCTURE_BREAK"
            return out

    start = max(10, len(frame) - 10)
    for i in range(start, len(frame)):
        reference = frame.iloc[max(0, i - 10):i]
        if len(reference) < 4:
            continue
        level = float(
            reference["High"].max()
            if direction == "BUY"
            else reference["Low"].min()
        )
        row = legacy_fx._candle_parts(frame.iloc[i])
        atr_value = _safe_float(atr.iloc[i])
        if atr_value <= 0:
            continue
        broken = (
            row["close"] > level + atr_value * 0.02
            if direction == "BUY"
            else row["close"] < level - atr_value * 0.02
        )
        aligned = (
            row["close"] > row["open"]
            if direction == "BUY"
            else row["close"] < row["open"]
        )
        if broken and aligned and row["body_ratio"] >= 0.40:
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
                "source": "RECENT_M15_STRUCTURE_BREAK",
            }
    return None


def _adaptive_retest_zone(
    frame: pd.DataFrame,
    direction: str,
    structure: Optional[Dict[str, Any]],
    atr: pd.Series,
    sweep: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    if not structure:
        return None

    if sweep:
        legacy = legacy_fx._find_retest_zone(
            frame,
            direction,
            sweep,
            structure,
            atr,
        )
        if legacy:
            out = dict(legacy)
            out["source"] = "SWEEP_ANCHORED_RETEST"
            return out

    break_pos = int(structure["position"])
    atr_break = max(_safe_float(atr.iloc[break_pos]), 1e-12)
    zones: List[Dict[str, Any]] = []

    # Last opposite candle before the displacement.
    for i in range(break_pos - 1, max(-1, break_pos - 9), -1):
        row = frame.iloc[i]
        opposite = (
            float(row["Close"]) < float(row["Open"])
            if direction == "BUY"
            else float(row["Close"]) > float(row["Open"])
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

    # Always retain the actual broken structure level as a valid break-zone retest.
    structure_level = float(structure["level"])
    zones.append({
        "kind": "BREAK_ZONE",
        "low": structure_level - atr_break * 0.10,
        "high": structure_level + atr_break * 0.10,
        "origin_position": break_pos,
        "origin_timestamp": frame.index[break_pos],
    })

    # Local three-candle imbalance around the displacement.
    for i in range(max(2, break_pos - 1), min(len(frame) - 1, break_pos + 2) + 1):
        if direction == "BUY":
            lower = float(frame["High"].iloc[i - 2])
            upper = float(frame["Low"].iloc[i])
        else:
            lower = float(frame["High"].iloc[i])
            upper = float(frame["Low"].iloc[i - 2])
        if upper > lower and upper - lower >= _safe_float(atr.iloc[i]) * 0.05:
            zones.append({
                "kind": "FAIR_VALUE_GAP",
                "low": lower,
                "high": upper,
                "origin_position": i,
                "origin_timestamp": frame.index[i],
            })

    for i in range(break_pos + 1, len(frame)):
        low = float(frame["Low"].iloc[i])
        high = float(frame["High"].iloc[i])
        for zone in reversed(zones):
            if low <= float(zone["high"]) and high >= float(zone["low"]):
                return {
                    **zone,
                    "retest_position": i,
                    "retest_timestamp": frame.index[i],
                    "source": "ADAPTIVE_STRUCTURE_RETEST",
                }
    return None


def _fallback_structural_anchor(
    frame: pd.DataFrame,
    direction: str,
    entry: float,
) -> Optional[float]:
    highs, lows = legacy_fx._confirmed_pivots(frame)
    points = lows if direction == "BUY" else highs
    viable = [
        float(point["price"])
        for point in points[-8:]
        if (
            float(point["price"]) < entry
            if direction == "BUY"
            else float(point["price"]) > entry
        )
    ]
    if viable:
        return max(viable) if direction == "BUY" else min(viable)

    recent = frame.iloc[max(0, len(frame) - 8):]
    if recent.empty:
        return None
    value = float(
        recent["Low"].min()
        if direction == "BUY"
        else recent["High"].max()
    )
    if direction == "BUY" and value < entry:
        return value
    if direction == "SELL" and value > entry:
        return value
    return None


def _structural_risk(
    frame: pd.DataFrame,
    direction: str,
    entry: float,
    atr_now: float,
    levels: Dict[str, Any],
    sweep: Optional[Dict[str, Any]],
    zone: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    anchors: List[float] = []
    if sweep:
        anchors.append(float(sweep["extreme"]))
    if zone:
        anchors.append(float(zone["low"] if direction == "BUY" else zone["high"]))

    if anchors:
        anchor = min(anchors) if direction == "BUY" else max(anchors)
        source = "SWEEP_OR_RETEST_INVALIDATION"
    else:
        anchor = _fallback_structural_anchor(frame, direction, entry)
        source = "RECENT_CONFIRMED_SWING_INVALIDATION"

    if anchor is None or atr_now <= 0:
        return {
            "valid": False,
            "stop": None,
            "stop_distance": 0.0,
            "target": None,
            "target_distance": 0.0,
            "target_r": 0.0,
            "room_r": 0.0,
            "room_source": "NO_VALID_STRUCTURE",
            "stop_source": source,
        }

    stop = (
        float(anchor) - atr_now * 0.15
        if direction == "BUY"
        else float(anchor) + atr_now * 0.15
    )
    stop_distance = entry - stop if direction == "BUY" else stop - entry

    # If a distant old sweep makes the stop unusably wide, fall back to the most
    # recent confirmed swing.  This does not tighten below a structural level.
    if stop_distance > atr_now * 4.0:
        recent_anchor = _fallback_structural_anchor(frame, direction, entry)
        if recent_anchor is not None:
            stop = (
                recent_anchor - atr_now * 0.15
                if direction == "BUY"
                else recent_anchor + atr_now * 0.15
            )
            stop_distance = entry - stop if direction == "BUY" else stop - entry
            source = "RECENT_SWING_AFTER_DISTANT_INVALIDATION"

    if stop_distance <= 0 or stop <= 0:
        return {
            "valid": False,
            "stop": None,
            "stop_distance": 0.0,
            "target": None,
            "target_distance": 0.0,
            "target_r": 0.0,
            "room_r": 0.0,
            "room_source": "INVALID_STOP",
            "stop_source": source,
        }

    opposing = legacy_fx._opposing_liquidity(levels, direction, entry)
    known_opposing = bool(
        opposing != entry
        and (
            opposing > entry
            if direction == "BUY"
            else opposing < entry
        )
    )
    if known_opposing:
        room_r = (
            (opposing - entry) / stop_distance
            if direction == "BUY"
            else (entry - opposing) / stop_distance
        )
        room_source = "KNOWN_OPPOSING_LIQUIDITY"
    else:
        # No mapped opposing level is not treated as a zero-room veto.  Use the
        # preferred 2R objective and record that no closer mapped liquidity was found.
        room_r = PREFERRED_TARGET_R
        room_source = "NO_CLOSER_MAPPED_OPPOSING_LIQUIDITY"

    room_r = max(0.0, room_r)
    target_r = min(PREFERRED_TARGET_R, room_r)
    target_distance = stop_distance * target_r
    target = (
        entry + target_distance
        if direction == "BUY"
        else entry - target_distance
    )
    valid = bool(stop_distance > 0 and target > 0 and target_r >= MIN_TARGET_R)
    return {
        "valid": valid,
        "stop": stop,
        "stop_distance": stop_distance,
        "target": target,
        "target_distance": target_distance,
        "target_r": target_r,
        "room_r": room_r,
        "room_source": room_source,
        "stop_source": source,
        "opposing_liquidity": opposing if known_opposing else None,
    }


def _serialise(value: Optional[Dict[str, Any]], *timestamp_fields: str) -> Optional[Dict[str, Any]]:
    if not value:
        return None
    out = dict(value)
    for field in timestamp_fields:
        if field in out:
            out[field] = legacy_fx._iso(out[field])
    return out


def _direction_setup_v612(
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

    external_line = legacy_fx.trendline_context(h4_frame, direction)
    internal_line = legacy_fx.trendline_context(frame.tail(120), direction)
    external_pass = bool(
        external_line.get("available")
        and external_line.get("aligned")
        and external_line.get("intact")
    )
    location_pass = bool(
        current <= equilibrium + atr_now * 0.25
        if direction == "BUY"
        else current >= equilibrium - atr_now * 0.25
    )

    sweep = legacy_fx._find_liquidity_sweep(frame, direction, atr, levels)
    structure = _adaptive_structure_shift(
        frame,
        direction,
        atr,
        str(h1.get("trend") or "NEUTRAL"),
        sweep,
    )
    zone = _adaptive_retest_zone(frame, direction, structure, atr, sweep)
    candle = legacy_fx.candlestick_confirmation(frame, direction)
    risk = _structural_risk(
        frame,
        direction,
        current,
        atr_now,
        levels,
        sweep,
        zone,
    )

    h4_pass = h4.get("trend") == desired
    h1_pass = h1.get("trend") == desired
    structure_pass = structure is not None
    session_pass = bool(session.get("active"))

    optional_checks = {
        "external_trendline_aligned_and_intact": external_pass,
        "premium_discount_location": location_pass,
        "liquidity_sweep": sweep is not None,
        "order_block_fvg_or_break_retest": zone is not None,
        "closed_candlestick_confirmation": bool(candle.get("passed")),
    }
    optional_count = sum(bool(value) for value in optional_checks.values())
    market_structure_confluence = (
        sum(bool(value) for value in (h4_pass, h1_pass, structure_pass)) / 3.0
    )
    core_pass = bool(h4_pass and h1_pass and structure_pass and session_pass)

    # Minimum B-grade setup lands exactly at 30%: core=10% + two optional=20%.
    # Additional evidence lifts confidence without becoming another mandatory veto.
    confidence = (0.10 if core_pass else 0.0) + 0.10 * optional_count
    if internal_line.get("touched"):
        confidence += 0.05
    if session.get("overlap"):
        confidence += 0.05
    if news.get("clear"):
        confidence += 0.05
    if _safe_float(risk.get("target_r")) >= PREFERRED_TARGET_R:
        confidence += 0.05
    confidence = min(1.0, confidence)

    target_r = _safe_float(risk.get("target_r"))
    grade: Optional[str] = None
    if (
        core_pass
        and optional_count >= A_GRADE_OPTIONAL_MIN
        and confidence >= SETUP_CONFIDENCE_MIN
        and target_r >= A_GRADE_MIN_R
    ):
        grade = "A"
    elif (
        core_pass
        and optional_count >= OPTIONAL_MIN
        and confidence >= SETUP_CONFIDENCE_MIN
        and target_r >= MIN_TARGET_R
    ):
        grade = "B"

    signal_eligible = bool(
        grade in {"A", "B"}
        and market_structure_confluence >= MARKET_STRUCTURE_MIN
    )

    checks = {
        "pair_relevant_session_active": session_pass,
        "high_impact_news_clear": bool(news.get("clear")),
        "h4_structure_aligned": h4_pass,
        "h1_structure_aligned": h1_pass,
        "directional_bias_aligned": bool(h4_pass and h1_pass),
        "bos_choch_cisd_or_mss": structure_pass,
        **optional_checks,
        "minimum_point_3_r_room": target_r >= MIN_TARGET_R,
        "preferred_two_r_room": target_r >= PREFERRED_TARGET_R,
    }

    rejection_reasons: List[str] = []
    if not h4_pass:
        rejection_reasons.append("H4_DIRECTIONAL_BIAS_NOT_ALIGNED")
    if not h1_pass:
        rejection_reasons.append("H1_DIRECTIONAL_BIAS_NOT_ALIGNED")
    if not structure_pass:
        rejection_reasons.append("M15_STRUCTURE_BREAK_NOT_CONFIRMED")
    if not session_pass:
        rejection_reasons.append("PAIR_RELEVANT_SESSION_INACTIVE")
    if optional_count < OPTIONAL_MIN:
        rejection_reasons.append("OPTIONAL_CONFLUENCE_BELOW_2_OF_5")
    if confidence < SETUP_CONFIDENCE_MIN:
        rejection_reasons.append("SETUP_CONFIDENCE_BELOW_30")
    if market_structure_confluence < MARKET_STRUCTURE_MIN:
        rejection_reasons.append("MARKET_STRUCTURE_CONFLUENCE_BELOW_60")
    if target_r < MIN_TARGET_R:
        rejection_reasons.append("TARGET_ROOM_BELOW_0_3R")

    setup_id = None
    if structure:
        setup_id = "|".join([
            STRATEGY_ID,
            legacy_fx.normalise_pair(session.get("pair")),
            direction,
            legacy_fx._iso(structure.get("timestamp")) or "NO_STRUCTURE_TIME",
            grade or "OBSERVE",
        ])

    return {
        "direction": direction if signal_eligible else "WAIT",
        "candidate_direction": direction,
        "signal_eligible": signal_eligible,
        "all_mandatory": signal_eligible,  # backward-compatible field
        "core_mandatory_pass": core_pass,
        "core_rule": "H4/H1 directional bias + M15 structure break + pair session",
        "checks": checks,
        "optional_checks": optional_checks,
        "optional_confluence_count": optional_count,
        "optional_confluence_required": OPTIONAL_MIN,
        "optional_confluence_total": len(OPTIONAL_KEYS),
        "market_structure_confluence": round(market_structure_confluence, 6),
        "market_structure_minimum": MARKET_STRUCTURE_MIN,
        "confidence": round(confidence, 6),
        "overall_setup_confidence": round(confidence, 6),
        "overall_setup_confidence_minimum": SETUP_CONFIDENCE_MIN,
        "trade_grade": grade,
        "external_trendline": external_line,
        "internal_trendline": internal_line,
        "internal_line_entry_confluence": bool(internal_line.get("touched")),
        "sweep": _serialise(sweep, "timestamp"),
        "structure_break": _serialise(structure, "timestamp"),
        "retest_zone": _serialise(zone, "origin_timestamp", "retest_timestamp"),
        "confirmation": _serialise(candle, "timestamp"),
        "analysis_entry_price": round(current, 10),
        "structural_stop_price": round(_safe_float(risk.get("stop")), 10) if risk.get("stop") is not None else None,
        "structural_stop_distance": round(_safe_float(risk.get("stop_distance")), 10),
        "take_profit_target_price": round(_safe_float(risk.get("target")), 10) if risk.get("target") is not None else None,
        "target_distance": round(_safe_float(risk.get("target_distance")), 10),
        "target_r": round(target_r, 6),
        "preferred_target_r": PREFERRED_TARGET_R,
        "minimum_target_r": MIN_TARGET_R,
        "room_to_opposing_liquidity_r": round(_safe_float(risk.get("room_r")), 4),
        "room_source": risk.get("room_source"),
        "stop_source": risk.get("stop_source"),
        "setup_id": setup_id,
        "rejection_reasons": rejection_reasons,
        "news_clear_for_execution": bool(news.get("clear")),
    }


def analyze_forex_v612(completed_15m: pd.DataFrame, symbol: Any) -> Dict[str, Any]:
    pair = legacy_fx.normalise_pair(symbol)
    if pair not in legacy_fx.LIQUID_FOREX_PAIRS:
        raise ValueError(f"Forex pair is not in the supported liquid universe: {pair}")

    frame = legacy_fx._normalise_ohlcv(completed_15m)
    atr = legacy_fx._atr(frame)
    if _safe_float(atr.iloc[-1]) <= 0:
        raise ValueError("Forex ATR is unavailable on the latest completed candle")

    h1_frame = legacy_fx._resample_completed(frame, "1h", 4)
    h4_frame = legacy_fx._resample_completed(frame, "4h", 16)
    h1 = legacy_fx._structure_state(h1_frame)
    h4 = legacy_fx._structure_state(h4_frame)
    bar_close = frame.index[-1] + legacy_fx._base_delta(frame)
    session = legacy_fx.forex_session_context(bar_close, pair)
    news = legacy_fx.news_blackout_context(bar_close, pair)
    levels = legacy_fx.liquidity_levels(frame, pair)

    buy = _direction_setup_v612(
        frame,
        "BUY",
        h4_frame=h4_frame,
        h4=h4,
        h1=h1,
        session=session,
        news=news,
        levels=levels,
        atr=atr,
    )
    sell = _direction_setup_v612(
        frame,
        "SELL",
        h4_frame=h4_frame,
        h4=h4,
        h1=h1,
        session=session,
        news=news,
        levels=levels,
        atr=atr,
    )

    qualified = [row for row in (buy, sell) if row.get("signal_eligible")]
    if len(qualified) == 1:
        selected = qualified[0]
        direction = str(selected["candidate_direction"])
    elif len(qualified) > 1:
        selected = max(
            qualified,
            key=lambda row: (
                1 if row.get("trade_grade") == "A" else 0,
                int(row.get("optional_confluence_count") or 0),
                _safe_float(row.get("confidence")),
                _safe_float(row.get("target_r")),
            ),
        )
        direction = "WAIT"
    else:
        selected = max(
            (buy, sell),
            key=lambda row: (
                int(row.get("core_mandatory_pass") is True),
                int(row.get("optional_confluence_count") or 0),
                _safe_float(row.get("confidence")),
            ),
        )
        direction = "WAIT"

    rejection_reasons = list(selected.get("rejection_reasons") or [])
    if len(qualified) > 1:
        rejection_reasons.append("CONFLICTING_BUY_AND_SELL_SETUPS")

    confidence = _safe_float(selected.get("confidence"))
    quant = confidence if direction in {"BUY", "SELL"} else min(0.19, confidence * 0.25)

    return {
        "version": VERSION,
        "strategy_id": STRATEGY_ID,
        "strategy_name": STRATEGY_NAME,
        "symbol": pair,
        "direction": direction,
        "quant_confidence": round(quant, 6),
        "directional_confidence": confidence if direction in {"BUY", "SELL"} else 0.0,
        "overall_setup_confidence": confidence,
        "setup_confidence_minimum": SETUP_CONFIDENCE_MIN,
        "quant_minimum": QUANT_MIN_CONFIDENCE,
        "model_ai_minimum": MODEL_AI_MIN_CONFIDENCE,
        "strategy_branch": "FX_ADAPTIVE_SESSION_MOMENTUM",
        "strategy_reason": (
            "Mandatory H4/H1 bias + M15 structure break + pair-relevant session; "
            "minimum 2/5 optional confirmations; >=30% setup confidence; >=0.3R. "
            "News and IG dealing checks are execution-safety guards, not technical-signal vetoes."
        ),
        "rejection_reasons": list(dict.fromkeys(rejection_reasons)),
        "selected_setup": selected,
        "buy_setup": buy,
        "sell_setup": sell,
        "trade_grade": selected.get("trade_grade") if direction in {"BUY", "SELL"} else None,
        "h4_structure": h4,
        "h1_structure": h1,
        "liquidity_levels": levels,
        "session": session,
        "news_guard": news,
        "atr_15m": round(_safe_float(atr.iloc[-1]), 10),
        "closed_candle_timestamp": legacy_fx._iso(frame.index[-1]),
        "closed_candle_end_timestamp": legacy_fx._iso(bar_close),
        "analysis_timeframes": {
            "higher_timeframe": "H4",
            "confirmation_timeframe": "H1",
            "entry_timeframe": "M15",
            "forming_candle_ignored": True,
        },
        "candlestick_analyzed": True,
        "candlestick_mandatory": False,
        "setup_id": selected.get("setup_id") if direction in {"BUY", "SELL"} else None,
        "structural_stop_price": selected.get("structural_stop_price"),
        "structural_stop_distance": selected.get("structural_stop_distance"),
        "take_profit_target_price": selected.get("take_profit_target_price"),
        "target_distance": selected.get("target_distance"),
        "target_r": selected.get("target_r"),
        "minimum_target_r": MIN_TARGET_R,
        "preferred_target_r": PREFERRED_TARGET_R,
        "room_to_opposing_liquidity_r": selected.get("room_to_opposing_liquidity_r"),
        "session_exit_at": session.get("session_exit_at"),
        "max_hold_seconds": 12 * 60 * 60,
        "forming_candle_ignored": True,
        "signal_news_optional": True,
        "execution_news_required": True,
        "execution_ig_safety_required": True,
        "live_money_execution": False,
    }


def _install_category_strategy_patch() -> None:
    import category_strategy_engine as engine

    if getattr(engine, "_V612_ADAPTIVE_FX_INSTALLED", False):
        return

    engine.analyze_forex = analyze_forex_v612
    engine.FOREX_STRATEGY_ID = STRATEGY_ID
    engine.FOREX_STRATEGY_NAME = STRATEGY_NAME
    engine.FOREX_STRATEGY_VERSION = VERSION
    engine.VERSION = ENGINE_VERSION
    engine.EVIDENCE_SCHEMA_VERSION = EVIDENCE_SCHEMA_VERSION
    engine.CategoryStrategyEngine.VERSION = ENGINE_VERSION
    engine.CATEGORY_RULES["FOREX"]["strategy_id"] = STRATEGY_ID
    engine.CATEGORY_RULES["FOREX"]["strategy_name"] = STRATEGY_NAME

    original_evaluate = engine.CategoryStrategyEngine._evaluate_seed
    original_status = engine.CategoryStrategyEngine.status
    original_optimizer_summary = engine.CategoryStrategyEngine.optimizer_summary

    def evaluate_seed_v612(self: Any, seed: Dict[str, Any]) -> Dict[str, Any]:
        row = original_evaluate(self, seed)
        if str(seed.get("category") or "").upper() != "FOREX":
            return row

        analysis = dict(row.get("forex_strategy") or {})
        if analysis.get("strategy_id") != STRATEGY_ID:
            return row

        direction = str(row.get("direction") or "WAIT").upper()
        quant = _safe_float(row.get("quant_confidence"))
        ai = _safe_float(row.get("model_ai_confidence"))
        setup_conf = _safe_float(analysis.get("overall_setup_confidence"))
        grade = analysis.get("trade_grade")

        quant_pass = bool(direction in {"BUY", "SELL"} and quant >= QUANT_MIN_CONFIDENCE)
        ai_pass = bool(direction in {"BUY", "SELL"} and ai >= MODEL_AI_MIN_CONFIDENCE)
        setup_conf_pass = bool(direction in {"BUY", "SELL"} and setup_conf >= SETUP_CONFIDENCE_MIN)
        grade_pass = grade in {"A", "B"}
        signal_qualified = bool(quant_pass and ai_pass and setup_conf_pass and grade_pass)

        # V6.11 skipped IG preflight whenever sweep/volume gates failed.  Under
        # V6.12 those are optional technical evidence, so preflight the newly
        # qualified signal here when the old path did not.
        if signal_qualified and not row.get("ig_epic"):
            try:
                market = self._resolve_execution_market(seed)
                row["ig_epic"] = market.get("epic")
                row["ig_market_name"] = market.get("name")
                row["ig_instrument_type"] = market.get("instrument_type")
                row["ig_market_status"] = market.get("market_status")
                row["ig_tradeable"] = (
                    str(market.get("market_status") or "").upper() == "TRADEABLE"
                )
                row["ig_min_deal_size"] = market.get("min_deal_size")
                row["ig_expiry"] = market.get("expiry")
                bid, offer, source = self._resolve_bid_offer(seed, market)
                row["ig_bid"] = bid
                row["ig_offer"] = offer
                row["ig_quote_source"] = source
                if bid is not None and offer is not None:
                    mid = (bid + offer) / 2.0
                    row["ig_spread_bps"] = (
                        round((offer - bid) / mid * 10000.0, 4)
                        if mid > 0
                        else None
                    )
            except Exception as exc:
                row["ig_preflight_error"] = f"{type(exc).__name__}: {exc}"

        spread_limit = float(engine.CATEGORY_RULES["FOREX"]["spread_gate_bps"])
        spread = row.get("ig_spread_bps")
        spread_pass = bool(
            spread is not None and _safe_float(spread, 1e9) <= spread_limit
        )
        news_clear = bool((analysis.get("news_guard") or {}).get("clear"))
        panic_clear = bool(row.get("panic_volatility_pass", True))
        ig_tradeable = bool(row.get("ig_tradeable"))

        # Hard execution safety remains even though these checks no longer veto
        # the technical setup itself.
        execution_safe = bool(
            signal_qualified
            and news_clear
            and panic_clear
            and ig_tradeable
            and spread_pass
        )

        old_reasons = {
            "FX_FULL_CONFLUENCE_NOT_CONFIRMED",
            "LIQUIDITY_GATE_FAIL",
            "VOLATILITY_GATE_FAIL",
            "QUANT_BELOW_28",
            "MODEL_AI_BELOW_40",
            "FAST_BELOW_45",
            "IG_NOT_TRADEABLE",
            "SPREAD_GATE_FAIL",
            "SPREAD_QUOTE_UNAVAILABLE",
            "SPREAD_TOO_WIDE",
        }
        reasons = [
            str(reason)
            for reason in (analysis.get("rejection_reasons") or [])
            if str(reason) not in old_reasons
        ]
        if direction not in {"BUY", "SELL"}:
            reasons.append("FX_V612_SIGNAL_NOT_QUALIFIED")
        if not quant_pass:
            reasons.append("QUANT_BELOW_20")
        if not ai_pass:
            reasons.append("MODEL_AI_BELOW_30")
        if not setup_conf_pass:
            reasons.append("SETUP_CONFIDENCE_BELOW_30")
        if signal_qualified and not news_clear:
            reasons.append("HIGH_IMPACT_NEWS_EXECUTION_GUARD")
        if signal_qualified and not panic_clear:
            reasons.append("PANIC_VOLATILITY_EXECUTION_GUARD")
        if signal_qualified and not ig_tradeable:
            reasons.append("IG_NOT_TRADEABLE")
        if signal_qualified and not spread_pass:
            reasons.append("SPREAD_GATE_FAIL")
            if spread is None:
                reasons.append("SPREAD_QUOTE_UNAVAILABLE")
            elif _safe_float(spread, 1e9) > spread_limit:
                reasons.append("SPREAD_TOO_WIDE")

        row.update({
            "strategy_id": STRATEGY_ID,
            "strategy_name": STRATEGY_NAME,
            "strategy_definition_version": ENGINE_VERSION,
            "strategy_selection_mode": "FX_ADAPTIVE_SESSION_MOMENTUM",
            "analysis_source": "FOREX_V612_M15_H1_H4_ADAPTIVE_SESSION_MOMENTUM",
            "strategy_module_version": VERSION,
            "forex_strategy_version": VERSION,
            "required_quant_confidence": QUANT_MIN_CONFIDENCE,
            "required_model_ai_confidence": MODEL_AI_MIN_CONFIDENCE,
            "required_setup_confidence": SETUP_CONFIDENCE_MIN,
            "quant20_pass": quant_pass,
            "ai30_pass": ai_pass,
            "setup30_pass": setup_conf_pass,
            # Legacy labels remain diagnostic only and no longer govern FX V6.12.
            "ai28_pass": quant >= 0.28,
            "ai40_pass": ai >= 0.40,
            "trade_grade": grade,
            "quality_tier": grade or row.get("quality_tier"),
            "signal_qualified": signal_qualified,
            "execution_safety_pass": execution_safe,
            "news_clear_for_execution": news_clear,
            "ig_size_safety_deferred_to_order_sizing": True,
            "spread_gate_bps": spread_limit,
            "spread_limit_bps": spread_limit,
            "spread_bps": spread,
            "spread_pass": spread_pass,
            "standard_eligible": execution_safe,
            "trade_eligible": execution_safe,
            "confidence_qualified": bool(quant_pass and ai_pass and setup_conf_pass),
            "direction_match": direction in {"BUY", "SELL"},
            "rejection_reasons": list(dict.fromkeys(reasons)),
            "live_money_execution": False,
        })
        return row

    def status_v612(self: Any) -> Dict[str, Any]:
        out = original_status(self)
        out["version"] = ENGINE_VERSION
        out["name"] = "JASONG V6.12 ADAPTIVE FX SESSION MOMENTUM + XAUUSD"
        out.setdefault("strategy_sequence", {})["setup"] = (
            "FX: H4/H1 bias + M15 structure break + pair session + >=2/5 optional "
            "confirmations; A=3/5 and >=0.5R, B=2/5 and >=0.3R"
        )
        out["strategy_sequence"]["then"] = (
            "FX signal -> news/panic guard -> IG tradeability/quote/spread/size safety -> IG DEMO"
        )
        out["confidence_policy"] = {
            "forex_quant_min": QUANT_MIN_CONFIDENCE,
            "forex_quant_min_pct": 20.0,
            "forex_model_ai_min": MODEL_AI_MIN_CONFIDENCE,
            "forex_model_ai_min_pct": 30.0,
            "forex_setup_confidence_min": SETUP_CONFIDENCE_MIN,
            "forex_setup_confidence_min_pct": 30.0,
            "forex_market_structure_min": MARKET_STRUCTURE_MIN,
            "forex_optional_confluence_required": OPTIONAL_MIN,
            "forex_optional_confluence_total": 5,
            "forex_min_target_r": MIN_TARGET_R,
            "forex_preferred_target_r": PREFERRED_TARGET_R,
            "fast_score": "DIAGNOSTIC_FOR_FX_V612_NOT_A_SIGNAL_VETO",
            "historical_validation_mode": "INFORMATIONAL_ONLY",
            "prime_authority": "BROKER_SETTLED_FORWARD_ONLY",
        }
        return out

    def optimizer_summary_v612(self: Any) -> Dict[str, Any]:
        out = original_optimizer_summary(self)
        out["version"] = ENGINE_VERSION
        out["method"] = "FX_V612_ADAPTIVE_SESSION_MOMENTUM_PLUS_XAUUSD"
        out["setup_rule"] = (
            "FX mandatory H4/H1 bias + M15 break + session; minimum 2/5 optional confirmations"
        )
        out["risk_reward_rule"] = "FX minimum 0.3R, A-grade >=0.5R, prefer 2R+"
        out["quant_min_pct"] = 20.0
        out["model_ai_min_pct"] = 30.0
        out["overall_setup_confidence_min_pct"] = 30.0
        return out

    engine.CategoryStrategyEngine._evaluate_seed = evaluate_seed_v612
    engine.CategoryStrategyEngine.status = status_v612
    engine.CategoryStrategyEngine.optimizer_summary = optimizer_summary_v612
    engine._V612_ADAPTIVE_FX_INSTALLED = True


def _install_risk_patch() -> None:
    import risk_exit_policy as risk_policy

    if getattr(risk_policy, "_V612_ADAPTIVE_FX_INSTALLED", False):
        return

    original_build = risk_policy.build_risk_plan

    def build_risk_plan_v612(
        candidate: Dict[str, Any],
        *,
        entry_price: float,
        direction: str,
    ) -> Any:
        if str(candidate.get("strategy_id") or "").upper().strip() != STRATEGY_ID:
            return original_build(candidate, entry_price=entry_price, direction=direction)

        entry = _safe_float(entry_price)
        clean_direction = str(direction or "").upper().strip()
        structural_distance = _safe_float(candidate.get("structural_stop_distance"))
        target_r = _safe_float(candidate.get("target_r"))
        if entry <= 0 or clean_direction not in {"BUY", "SELL"}:
            raise ValueError("adaptive FX risk plan requires valid entry and direction")
        if structural_distance <= 0:
            raise ValueError("adaptive FX entry has no valid structural stop distance")
        if target_r < MIN_TARGET_R:
            raise ValueError("adaptive FX entry has less than 0.3R target room")

        target_r = max(MIN_TARGET_R, min(10.0, target_r))
        stop_pct = structural_distance / entry * 100.0
        target_distance = structural_distance * target_r
        if clean_direction == "BUY":
            stop_price = entry - structural_distance
            target_price = entry + target_distance
        else:
            stop_price = entry + structural_distance
            target_price = entry - target_distance
        if stop_price <= 0 or target_price <= 0:
            raise ValueError("adaptive FX calculated risk levels are invalid")

        return risk_policy.RiskPlan(
            version="v612-adaptive-fx-structural-risk-v1",
            category="FOREX",
            direction=clean_direction,
            entry_price=round(entry, 10),
            stop_pct=round(stop_pct, 6),
            target_r=round(target_r, 6),
            stop_distance=round(structural_distance, 10),
            target_distance=round(target_distance, 10),
            protective_stop_price=round(stop_price, 10),
            take_profit_target_price=round(target_price, 10),
            source=(
                "V612_ADAPTIVE_STRUCTURE_INVALIDATION_"
                f"TARGET_{target_r:g}R_PREFER_2R"
            ),
        )

    risk_policy.build_risk_plan = build_risk_plan_v612
    risk_policy._V612_ADAPTIVE_FX_INSTALLED = True


def _install_prime_patch() -> None:
    import prime_policy as prime

    if getattr(prime, "_V612_ADAPTIVE_FX_INSTALLED", False):
        return

    original_gate = prime.ForwardPrimeArchitecture._strong_gate

    def strong_gate_v612(self: Any, row: Dict[str, Any], provenance: Dict[str, Any]):
        if str(row.get("strategy_id") or "").upper().strip() != STRATEGY_ID:
            return original_gate(self, row, provenance)

        reasons: List[str] = []
        direction = str(row.get("direction") or row.get("live_direction") or "").upper().strip()
        if direction not in {"BUY", "SELL"}:
            reasons.append("NO_DIRECTION")
        if _safe_float(row.get("quant_confidence")) < QUANT_MIN_CONFIDENCE:
            reasons.append("QUANT_BELOW_20")
        if _safe_float(row.get("model_ai_confidence")) < MODEL_AI_MIN_CONFIDENCE:
            reasons.append("MODEL_AI_BELOW_30")
        if _safe_float((row.get("forex_strategy") or {}).get("overall_setup_confidence")) < SETUP_CONFIDENCE_MIN:
            reasons.append("SETUP_CONFIDENCE_BELOW_30")
        if str(row.get("trade_grade") or "").upper() not in {"A", "B"}:
            reasons.append("FX_GRADE_NOT_A_OR_B")
        if not bool((row.get("news_guard") or {}).get("clear")):
            reasons.append("HIGH_IMPACT_NEWS_EXECUTION_GUARD")
        if not bool(row.get("ig_tradeable")):
            reasons.append("IG_NOT_TRADEABLE")
        if row.get("spread_pass") is not True:
            reasons.append("SPREAD_GATE_FAIL")
        if row.get("panic_volatility_pass") is False:
            reasons.append("PANIC_VOLATILITY_EXECUTION_GUARD")
        for issue in provenance.get("issues") or []:
            if issue not in reasons:
                reasons.append(str(issue))
        return len(reasons) == 0, reasons

    prime.ForwardPrimeArchitecture._strong_gate = strong_gate_v612
    prime._V612_ADAPTIVE_FX_INSTALLED = True


def _install_execution_patch() -> None:
    # Risk must be patched first so any from-import in this module receives the
    # adaptive builder.  Explicit assignments also make re-import order harmless.
    import category_execution_engine as execution
    import risk_exit_policy as risk_policy

    execution.FX_STRATEGY_ID = STRATEGY_ID
    execution.build_risk_plan = risk_policy.build_risk_plan
    execution.CategoryExecutionEngine.VERSION = "6.12-fx-adaptive-execution-v1"
    execution.CategoryExecutionEngine.ACTIVE_STRATEGY_IDS = (
        execution.XAU_STRATEGY_ID,
        STRATEGY_ID,
    )


def install() -> None:
    _install_risk_patch()
    _install_category_strategy_patch()
    _install_prime_patch()
    _install_execution_patch()


install()


def policy_status() -> Dict[str, Any]:
    return {
        "version": VERSION,
        "strategy_id": STRATEGY_ID,
        "strategy_name": STRATEGY_NAME,
        "markets": list(legacy_fx.LIQUID_FOREX_PAIRS),
        "market_count": len(legacy_fx.LIQUID_FOREX_PAIRS),
        "mandatory_core": [
            "H4/H1_DIRECTIONAL_BIAS",
            "M15_BOS_CHOCH_CISD_MSS",
            "PAIR_RELEVANT_SESSION",
        ],
        "optional_confluence": list(OPTIONAL_KEYS),
        "optional_required": OPTIONAL_MIN,
        "market_structure_min": MARKET_STRUCTURE_MIN,
        "quant_min": QUANT_MIN_CONFIDENCE,
        "model_ai_min": MODEL_AI_MIN_CONFIDENCE,
        "setup_confidence_min": SETUP_CONFIDENCE_MIN,
        "min_target_r": MIN_TARGET_R,
        "a_grade_min_target_r": A_GRADE_MIN_R,
        "preferred_target_r": PREFERRED_TARGET_R,
        "signal_news_optional": True,
        "execution_news_required": True,
        "execution_ig_safety_required": True,
        "environment": "IG_DEMO_ONLY",
        "live_money_execution": False,
    }
