from typing import Any, Callable, Dict, List

import math
import pandas as pd


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        value = float(value)

        if math.isnan(value) or math.isinf(value):
            return default

        return value

    except (TypeError, ValueError):
        return default


def _safe_bool(value: Any) -> bool:
    try:
        return bool(value)
    except Exception:
        return False


def _latest_valid_row(
    df: pd.DataFrame,
) -> pd.Series:
    """
    Return the latest usable indicator row.
    """

    if df is None or df.empty:
        raise ValueError("No market data available")

    cleaned = df.copy()

    # We only require Close to exist.
    if "Close" not in cleaned.columns:
        raise ValueError("Close column missing")

    cleaned = cleaned.dropna(
        subset=["Close"],
    )

    if cleaned.empty:
        raise ValueError(
            "No valid closing prices available"
        )

    return cleaned.iloc[-1]


def _market_fast_score(
    row: pd.Series,
) -> Dict[str, Any]:
    """
    Lightweight market scoring.

    This does NOT:
    - train an ML model
    - run threshold sweeps
    - run multi-timeframe backtests

    It scores the latest technical state only.
    """

    close = _safe_float(
        row.get("Close"),
    )

    ema9 = _safe_float(
        row.get("EMA_9"),
        close,
    )

    ema21 = _safe_float(
        row.get("EMA_21"),
        close,
    )

    ema50 = _safe_float(
        row.get("EMA_50"),
        close,
    )

    rsi = _safe_float(
        row.get("RSI_14"),
        50.0,
    )

    macd = _safe_float(
        row.get("MACD"),
    )

    macd_signal = _safe_float(
        row.get("MACD_SIGNAL"),
    )

    atr_pct = abs(
        _safe_float(
            row.get("ATR_PCT"),
        )
    )

    ret1 = _safe_float(
        row.get("RET_1"),
    )

    ret3 = _safe_float(
        row.get("RET_3"),
    )

    ret5 = _safe_float(
        row.get("RET_5"),
    )

    body_pct = _safe_float(
        row.get("BODY_PCT"),
    )

    trend_up = _safe_bool(
        row.get("TREND_UP"),
    )

    trend_strong = _safe_bool(
        row.get("TREND_STRONG"),
    )

    bullish = 0.0
    bearish = 0.0

    reasons: List[str] = []

    # =====================================================
    # 1. EMA STRUCTURE
    # =====================================================

    if ema9 > ema21:
        bullish += 12
    elif ema9 < ema21:
        bearish += 12

    if ema21 > ema50:
        bullish += 12
    elif ema21 < ema50:
        bearish += 12

    if ema9 > ema21 > ema50:
        bullish += 8
        reasons.append(
            "Bullish EMA alignment"
        )

    if ema9 < ema21 < ema50:
        bearish += 8
        reasons.append(
            "Bearish EMA alignment"
        )

    # =====================================================
    # 2. EXISTING TREND FLAGS
    # =====================================================

    if trend_up:
        bullish += 6

    if trend_strong:
        # Strong trend benefits whichever
        # side currently leads.
        if bullish >= bearish:
            bullish += 6
        else:
            bearish += 6

    # =====================================================
    # 3. MACD
    # =====================================================

    if macd > macd_signal:
        bullish += 12
        reasons.append(
            "MACD bullish"
        )

    elif macd < macd_signal:
        bearish += 12
        reasons.append(
            "MACD bearish"
        )

    # =====================================================
    # 4. RSI
    # =====================================================

    if 52 <= rsi <= 68:
        bullish += 10

    elif 32 <= rsi <= 48:
        bearish += 10

    elif rsi > 75:
        # Very extended bullish market.
        bullish -= 5
        reasons.append(
            "RSI overextended"
        )

    elif rsi < 25:
        bearish -= 5
        reasons.append(
            "RSI overextended"
        )

    # =====================================================
    # 5. SHORT-TERM RETURNS
    # =====================================================

    positive_returns = sum(
        [
            ret1 > 0,
            ret3 > 0,
            ret5 > 0,
        ]
    )

    negative_returns = sum(
        [
            ret1 < 0,
            ret3 < 0,
            ret5 < 0,
        ]
    )

    bullish += positive_returns * 5
    bearish += negative_returns * 5

    if positive_returns == 3:
        reasons.append(
            "Positive momentum"
        )

    if negative_returns == 3:
        reasons.append(
            "Negative momentum"
        )

    # =====================================================
    # 6. CURRENT CANDLE BODY
    # =====================================================

    if body_pct > 0:
        bullish += 4

    elif body_pct < 0:
        bearish += 4

    # =====================================================
    # 7. DIRECTION + CONVICTION
    # =====================================================

    if bullish > bearish:
        direction = "BUY"
        directional_score = bullish

    elif bearish > bullish:
        direction = "SELL"
        directional_score = bearish

    else:
        direction = "WAIT"
        directional_score = bullish

    difference = abs(
        bullish - bearish
    )

    # Direction certainty.
    directional_score += min(
        difference * 0.35,
        12,
    )

    # =====================================================
    # 8. MARKET QUALITY
    # =====================================================

    quality = 20.0

    # Extremely quiet markets get a small penalty.
    if atr_pct <= 0:
        quality -= 5

    # Excessive volatility also gets penalised.
    elif atr_pct > 0.03:
        quality -= 8

    elif atr_pct > 0.02:
        quality -= 4

    # Neutral RSI can indicate weak direction.
    if 48 < rsi < 52:
        quality -= 5

    # =====================================================
    # FINAL SCORE
    # =====================================================

    final_score = (
        directional_score
        + quality
    )

    final_score = max(
        0.0,
        min(
            100.0,
            final_score,
        ),
    )

    # =====================================================
    # STATUS
    # =====================================================

    if (
        final_score >= 75
        and direction != "WAIT"
    ):
        status = "STRONG"

    elif (
        final_score >= 60
        and direction != "WAIT"
    ):
        status = "QUALIFIED"

    elif final_score >= 45:
        status = "WATCH"

    else:
        status = "REJECT"

    return {
        "price": close,
        "rsi": rsi,
        "ema_9": ema9,
        "ema_21": ema21,
        "ema_50": ema50,
        "macd": macd,
        "macd_signal": macd_signal,
        "atr_pct": atr_pct,
        "ret_1": ret1,
        "ret_3": ret3,
        "ret_5": ret5,
        "direction": direction,
        "bullish_score": round(
            bullish,
            2,
        ),
        "bearish_score": round(
            bearish,
            2,
        ),
        "fast_score": round(
            final_score,
            2,
        ),
        "status": status,
        "reasons": reasons[:5],
    }


def scan_market_fast(
    market_name: str,
    symbol: str,
    get_data_func: Callable,
    add_indicators_func: Callable,
    period: str = "5d",
    interval: str = "15m",
) -> Dict[str, Any]:
    """
    Perform a lightweight scan of one market.
    """

    raw = get_data_func(
        symbol,
        period,
        interval,
    )

    if raw is None or raw.empty:
        raise ValueError(
            "No price data returned"
        )

    if len(raw) < 20:
        raise ValueError(
            "Insufficient market data"
        )

    enriched = add_indicators_func(
        raw,
    )

    row = _latest_valid_row(
        enriched,
    )

    scored = _market_fast_score(
        row,
    )

    return {
        "market": market_name,
        "symbol": symbol,
        "period": period,
        "interval": interval,
        **scored,
    }


def fast_scan_markets(
    markets: Dict[str, str],
    get_data_func: Callable,
    add_indicators_func: Callable,
    period: str = "5d",
    interval: str = "15m",
    top_n: int = 3,
) -> Dict[str, Any]:
    """
    Fast Stage-1 scan of all configured markets.

    No ML training and no backtesting are performed here.
    """

    results: List[
        Dict[str, Any]
    ] = []

    for market_name, symbol in markets.items():
        try:
            result = scan_market_fast(
                market_name=market_name,
                symbol=symbol,
                get_data_func=get_data_func,
                add_indicators_func=(
                    add_indicators_func
                ),
                period=period,
                interval=interval,
            )

            results.append(
                result
            )

        except Exception as exc:
            results.append(
                {
                    "market": market_name,
                    "symbol": symbol,
                    "status": "ERROR",
                    "fast_score": 0.0,
                    "direction": "WAIT",
                    "error": str(exc),
                }
            )

    successful = [
        row
        for row in results
        if row.get("status")
        != "ERROR"
    ]

    ranked = sorted(
        successful,
        key=lambda row: float(
            row.get(
                "fast_score",
                0.0,
            )
        ),
        reverse=True,
    )

    candidates = [
        row
        for row in ranked
        if row.get("status")
        in (
            "STRONG",
            "QUALIFIED",
            "WATCH",
        )
    ]

    top_candidates = (
        candidates[:top_n]
    )

    return {
        "scanner": "V4.2_FAST_SCAN",
        "period": period,
        "interval": interval,
        "markets_tested": len(
            markets
        ),
        "markets_successful": len(
            successful
        ),
        "markets_failed": (
            len(results)
            - len(successful)
        ),
        "candidates_found": len(
            candidates
        ),
        "best_candidate": (
            top_candidates[0]
            if top_candidates
            else None
        ),
        "top_candidates":
            top_candidates,
        "ranking": ranked,
        "all_results": results,
        "heavy_optimisation":
            False,
        "live_execution":
            False,
    }
