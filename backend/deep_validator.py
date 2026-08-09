from typing import Any, Callable, Dict, List

import math


def _safe_float(
    value: Any,
    default: float = 0.0,
) -> float:
    try:
        number = float(value)

        if math.isnan(number) or math.isinf(number):
            return default

        return number

    except (TypeError, ValueError):
        return default


def _score_validation(
    result: Dict[str, Any],
) -> float:
    """
    Score a fully optimised/backtested strategy.

    This score is NOT a win probability.
    It is used only to rank deeply validated candidates.
    """

    trades = int(
        result.get("trades", 0)
    )

    win_rate = _safe_float(
        result.get("win_rate", 0.0)
    )

    profit_factor = _safe_float(
        result.get("profit_factor", 0.0)
    )

    max_drawdown = abs(
        _safe_float(
            result.get("max_drawdown", 0.0)
        )
    )

    return_pct = _safe_float(
        result.get("return_pct", 0.0)
    )

    validated_sample = bool(
        result.get(
            "validated_sample",
            result.get("sample_ok", False),
        )
    )

    score = 0.0

    # Win rate contribution
    score += min(
        win_rate * 50.0,
        50.0,
    )

    # Profit factor contribution
    score += min(
        max(profit_factor, 0.0) * 10.0,
        25.0,
    )

    # Positive return contribution
    if return_pct > 0:
        score += min(
            return_pct * 100.0,
            10.0,
        )

    # Reward useful sample size
    if trades >= 50:
        score += 10.0

    elif trades >= 30:
        score += 8.0

    elif trades >= 20:
        score += 5.0

    elif trades >= 10:
        score += 2.0

    # Reward validated sample
    if validated_sample:
        score += 10.0

    # Drawdown penalties
    if max_drawdown > 0.10:
        score -= 20.0

    elif max_drawdown > 0.08:
        score -= 15.0

    elif max_drawdown > 0.05:
        score -= 8.0

    elif max_drawdown > 0.03:
        score -= 3.0

    return round(
        max(0.0, min(score, 100.0)),
        2,
    )


def _validation_status(
    trades: int,
    win_rate: float,
    profit_factor: float,
    max_drawdown: float,
    validated_sample: bool,
) -> str:
    """
    Conservative validation classification.
    """

    if (
        trades >= 20
        and win_rate >= 0.70
        and profit_factor >= 1.50
        and max_drawdown <= 0.05
        and validated_sample
    ):
        return "VERIFIED"

    if (
        trades >= 15
        and win_rate >= 0.62
        and profit_factor >= 1.20
        and max_drawdown <= 0.08
    ):
        return "WATCH"

    return "REJECT"


def validate_one_market(
    candidate: Dict[str, Any],
    optimise_all_timeframes_func: Callable,
    get_data_func: Callable,
    add_indicators_func: Callable,
    train_model_func: Callable,
    enrich_func: Callable,
    profile: Any,
    starting_balance: float = 10000.0,
    payout: float = 0.80,
) -> Dict[str, Any]:
    """
    Run the heavy optimiser for one shortlisted market.
    """

    market = candidate.get("market")

    symbol = candidate.get("symbol")

    if not symbol:
        raise ValueError(
            "Candidate symbol missing"
        )

    optimisation = optimise_all_timeframes_func(
        symbol=symbol,
        get_data_func=get_data_func,
        add_indicators_func=add_indicators_func,
        train_model_func=train_model_func,
        enrich_func=enrich_func,
        profile=profile,
        starting_balance=starting_balance,
        payout=payout,
    )

    best = optimisation.get("best")

    if not best:
        return {
            "market": market,
            "symbol": symbol,
            "fast_direction":
                candidate.get("direction"),
            "fast_score":
                candidate.get("fast_score"),
            "status": "NO_DATA",
            "verified": False,
        }

    trades = int(
        best.get("trades", 0)
    )

    wins = int(
        best.get("wins", 0)
    )

    losses = int(
        best.get("losses", 0)
    )

    win_rate = _safe_float(
        best.get("win_rate", 0.0)
    )

    return_pct = _safe_float(
        best.get("return_pct", 0.0)
    )

    max_drawdown = abs(
        _safe_float(
            best.get("max_drawdown", 0.0)
        )
    )

    profit_factor = _safe_float(
        best.get("profit_factor", 0.0)
    )

    average_trade_pnl = _safe_float(
        best.get("average_trade_pnl", 0.0)
    )

    validated_sample = bool(
        best.get(
            "validated_sample",
            best.get("sample_ok", False),
        )
    )

    deep_score = _score_validation(
        best
    )

    status = _validation_status(
        trades=trades,
        win_rate=win_rate,
        profit_factor=profit_factor,
        max_drawdown=max_drawdown,
        validated_sample=validated_sample,
    )

    direction = (
        best.get("direction")
        or candidate.get("direction")
        or "WAIT"
    )

    return {
        "market": market,
        "symbol": symbol,

        "fast_direction":
            candidate.get("direction"),

        "fast_score":
            candidate.get("fast_score"),

        "direction": direction,

        "period":
            best.get("period"),

        "interval":
            best.get("interval"),

        "threshold":
            best.get("threshold"),

        "threshold_pct":
            best.get("threshold_pct"),

        "holding_candles":
            best.get("holding_candles"),

        "trades": trades,

        "wins": wins,

        "losses": losses,

        "win_rate": win_rate,

        "return_pct": return_pct,

        "max_drawdown":
            max_drawdown,

        "profit_factor":
            profit_factor,

        "average_trade_pnl":
            average_trade_pnl,

        "validated_sample":
            validated_sample,

        "deep_score":
            deep_score,

        "status":
            status,

        "verified":
            status == "VERIFIED",
    }


def validate_candidates(
    candidates: List[Dict[str, Any]],
    optimise_all_timeframes_func: Callable,
    get_data_func: Callable,
    add_indicators_func: Callable,
    train_model_func: Callable,
    enrich_func: Callable,
    profile: Any,
    starting_balance: float = 10000.0,
    payout: float = 0.80,
    max_candidates: int = 3,
) -> Dict[str, Any]:
    """
    Deeply validate only the shortlisted markets.

    Expected input:
    top_candidates from /fast-scan.
    """

    shortlist = candidates[
        :max_candidates
    ]

    results: List[
        Dict[str, Any]
    ] = []

    for candidate in shortlist:
        try:
            result = validate_one_market(
                candidate=candidate,
                optimise_all_timeframes_func=(
                    optimise_all_timeframes_func
                ),
                get_data_func=get_data_func,
                add_indicators_func=(
                    add_indicators_func
                ),
                train_model_func=(
                    train_model_func
                ),
                enrich_func=enrich_func,
                profile=profile,
                starting_balance=(
                    starting_balance
                ),
                payout=payout,
            )

            results.append(result)

        except Exception as exc:
            results.append({
                "market":
                    candidate.get(
                        "market"
                    ),

                "symbol":
                    candidate.get(
                        "symbol"
                    ),

                "fast_direction":
                    candidate.get(
                        "direction"
                    ),

                "fast_score":
                    candidate.get(
                        "fast_score"
                    ),

                "status": "ERROR",

                "verified": False,

                "error": str(exc),
            })

    successful = [
        item
        for item in results
        if item.get("status")
        not in (
            "ERROR",
            "NO_DATA",
        )
    ]

    ranked = sorted(
        successful,
        key=lambda item:
            float(
                item.get(
                    "deep_score",
                    0.0,
                )
            ),
        reverse=True,
    )

    verified = [
        item
        for item in ranked
        if item.get("verified")
    ]

    final_market = (
        verified[0]
        if verified
        else (
            ranked[0]
            if ranked
            else None
        )
    )

    if final_market is None:
        final_status = "NO_TRADE"

    elif final_market.get(
        "verified"
    ):
        final_status = (
            "VERIFIED_TRADE"
        )

    else:
        final_status = (
            "NOT_VERIFIED"
        )

    return {
        "validator":
            "V4.3_DEEP_VALIDATION",

        "candidates_received":
            len(candidates),

        "candidates_tested":
            len(shortlist),

        "successful_validations":
            len(successful),

        "verified_markets":
            len(verified),

        "final_status":
            final_status,

        "final_market":
            final_market,

        "ranking":
            ranked,

        "all_results":
            results,

        "live_execution":
            False,
    }
