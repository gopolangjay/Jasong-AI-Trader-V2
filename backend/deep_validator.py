from typing import (
    Any,
    Callable,
    Dict,
    List,
)

import math


# ============================================================
# JASONG AI TRADER V4.9
# EXPLAINABLE DEEP VALIDATION
# ============================================================


VERIFIED_MIN_TRADES = 20
VERIFIED_MIN_WIN_RATE = 0.70
VERIFIED_MIN_PROFIT_FACTOR = 1.50
VERIFIED_MAX_DRAWDOWN = 0.05

WATCH_MIN_TRADES = 15
WATCH_MIN_WIN_RATE = 0.62
WATCH_MIN_PROFIT_FACTOR = 1.20
WATCH_MAX_DRAWDOWN = 0.08

NEAR_MIN_TRADES = 10
NEAR_MIN_WIN_RATE = 0.75
NEAR_MIN_PROFIT_FACTOR = 1.50
NEAR_MAX_DRAWDOWN = 0.08


def _safe_float(
    value: Any,
    default: float = 0.0,
) -> float:
    try:
        number = float(
            value
        )

        if (
            math.isnan(
                number
            )
            or math.isinf(
                number
            )
        ):
            return default

        return number

    except (
        TypeError,
        ValueError,
    ):
        return default


# ============================================================
# WILSON LOWER CONFIDENCE BOUND
# ============================================================

def _wilson_lower_bound(
    wins: int,
    trades: int,
    z: float = 1.96,
) -> float:
    """
    Conservative estimate of win-rate reliability.

    Example:
    9 wins from 10 may show a raw 90% win rate,
    but its statistical lower bound is much lower
    because the sample is very small.

    This is not a prediction of the next trade.
    """

    if trades <= 0:
        return 0.0

    p = wins / trades

    denominator = (
        1.0
        + (
            z * z
            / trades
        )
    )

    centre = (
        p
        + (
            z * z
            / (
                2.0 * trades
            )
        )
    )

    adjustment = z * math.sqrt(
        (
            p * (
                1.0 - p
            )
            / trades
        )
        + (
            z * z
            / (
                4.0
                * trades
                * trades
            )
        )
    )

    lower = (
        centre
        - adjustment
    ) / denominator

    return max(
        0.0,
        min(
            lower,
            1.0,
        ),
    )


# ============================================================
# SAMPLE RELIABILITY
# ============================================================

def _sample_reliability(
    trades: int,
) -> float:
    """
    30+ trades = 100% sample weight.

    This is a reliability weighting only,
    not a trading probability.
    """

    if trades <= 0:
        return 0.0

    return min(
        trades / 30.0,
        1.0,
    )


# ============================================================
# RAW DEEP SCORE
# ============================================================

def _score_validation(
    result: Dict[str, Any],
) -> float:
    """
    Raw strategy-quality score.

    This score is NOT a win probability.
    """

    trades = int(
        result.get(
            "trades",
            0,
        )
    )

    win_rate = _safe_float(
        result.get(
            "win_rate",
            0.0,
        )
    )

    profit_factor = _safe_float(
        result.get(
            "profit_factor",
            0.0,
        )
    )

    max_drawdown = abs(
        _safe_float(
            result.get(
                "max_drawdown",
                0.0,
            )
        )
    )

    return_pct = _safe_float(
        result.get(
            "return_pct",
            0.0,
        )
    )

    validated_sample = bool(
        result.get(
            "validated_sample",
            result.get(
                "sample_ok",
                False,
            ),
        )
    )

    score = 0.0

    # Win rate
    score += min(
        win_rate * 50.0,
        50.0,
    )

    # Profit factor
    score += min(
        max(
            profit_factor,
            0.0,
        ) * 10.0,
        25.0,
    )

    # Positive return
    if return_pct > 0:

        score += min(
            return_pct * 100.0,
            10.0,
        )

    # Sample size
    if trades >= 50:

        score += 10.0

    elif trades >= 30:

        score += 8.0

    elif trades >= 20:

        score += 5.0

    elif trades >= 10:

        score += 2.0

    # Valid optimiser sample
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
        max(
            0.0,
            min(
                score,
                100.0,
            ),
        ),
        2,
    )


# ============================================================
# RELIABILITY-ADJUSTED DEEP SCORE
# ============================================================

def _reliability_adjusted_score(
    deep_score: float,
    trades: int,
) -> float:
    """
    Prevent tiny samples from being treated like
    fully established historical evidence.

    We do NOT discard a strong small sample.

    Instead, we display both:
    - raw deep score
    - reliability-adjusted score
    """

    reliability = (
        _sample_reliability(
            trades
        )
    )

    # Shrink toward neutral score 50.
    adjusted = (
        50.0
        + (
            deep_score - 50.0
        ) * reliability
    )

    return round(
        max(
            0.0,
            min(
                adjusted,
                100.0,
            ),
        ),
        2,
    )


# ============================================================
# VERIFICATION FAILURES
# ============================================================

def _verification_failures(
    trades: int,
    win_rate: float,
    profit_factor: float,
    max_drawdown: float,
    validated_sample: bool,
) -> List[str]:

    reasons = []

    if trades < VERIFIED_MIN_TRADES:

        reasons.append(
            "INSUFFICIENT_SAMPLE_FOR_FULL_VERIFICATION"
        )

    if win_rate < VERIFIED_MIN_WIN_RATE:

        reasons.append(
            "WIN_RATE_BELOW_VERIFIED_THRESHOLD"
        )

    if profit_factor < VERIFIED_MIN_PROFIT_FACTOR:

        reasons.append(
            "PROFIT_FACTOR_BELOW_VERIFIED_THRESHOLD"
        )

    if max_drawdown > VERIFIED_MAX_DRAWDOWN:

        reasons.append(
            "DRAWDOWN_ABOVE_VERIFIED_LIMIT"
        )

    if not validated_sample:

        reasons.append(
            "OPTIMISER_SAMPLE_NOT_VALIDATED"
        )

    return reasons


# ============================================================
# CLASSIFICATION
# ============================================================

def _validation_status(
    trades: int,
    win_rate: float,
    profit_factor: float,
    max_drawdown: float,
    validated_sample: bool,
) -> str:
    """
    V4.9 classification.

    VERIFIED
        Fully meets strict verification criteria.

    NEAR_VERIFIED
        Very strong historical result but does not yet
        have enough evidence for the VERIFIED badge.

    WATCH
        Potentially useful result requiring caution.

    REJECT
        Does not meet minimum validation standards.
    """

    if (
        trades
        >= VERIFIED_MIN_TRADES
        and win_rate
        >= VERIFIED_MIN_WIN_RATE
        and profit_factor
        >= VERIFIED_MIN_PROFIT_FACTOR
        and max_drawdown
        <= VERIFIED_MAX_DRAWDOWN
        and validated_sample
    ):
        return "VERIFIED"

    # Strong small sample.
    # Example: 9/10, 90% WR and PF 7.
    if (
        trades
        >= NEAR_MIN_TRADES
        and trades
        < VERIFIED_MIN_TRADES
        and win_rate
        >= NEAR_MIN_WIN_RATE
        and profit_factor
        >= NEAR_MIN_PROFIT_FACTOR
        and max_drawdown
        <= NEAR_MAX_DRAWDOWN
        and validated_sample
    ):
        return "NEAR_VERIFIED"

    if (
        trades
        >= WATCH_MIN_TRADES
        and win_rate
        >= WATCH_MIN_WIN_RATE
        and profit_factor
        >= WATCH_MIN_PROFIT_FACTOR
        and max_drawdown
        <= WATCH_MAX_DRAWDOWN
    ):
        return "WATCH"

    return "REJECT"


# ============================================================
# HUMAN-READABLE SUMMARY
# ============================================================

def _build_explanation(
    status: str,
    trades: int,
    wins: int,
    losses: int,
    win_rate: float,
    profit_factor: float,
    max_drawdown: float,
    reasons: List[str],
) -> str:

    if status == "VERIFIED":

        return (
            "Passed full deep-validation criteria "
            f"with {wins}/{trades} historical wins, "
            f"{win_rate:.1%} win rate and "
            f"profit factor {profit_factor:.2f}."
        )

    if status == "NEAR_VERIFIED":

        return (
            "Historical performance is very strong "
            f"({wins}/{trades} wins, "
            f"{win_rate:.1%} win rate, "
            f"PF {profit_factor:.2f}), "
            "but the sample is too small for the "
            "full VERIFIED badge."
        )

    if status == "WATCH":

        return (
            "Historical performance passed WATCH "
            "criteria but did not satisfy every "
            "full verification requirement."
        )

    if reasons:

        return (
            "Rejected because: "
            + ", ".join(
                reasons
            )
        )

    return (
        "Historical validation did not meet "
        "the required quality thresholds."
    )


# ============================================================
# VALIDATE ONE MARKET
# ============================================================

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

    market = candidate.get(
        "market"
    )

    symbol = candidate.get(
        "symbol"
    )

    if not symbol:

        raise ValueError(
            "Candidate symbol missing"
        )

    optimisation = (
        optimise_all_timeframes_func(
            symbol=
                symbol,

            get_data_func=
                get_data_func,

            add_indicators_func=
                add_indicators_func,

            train_model_func=
                train_model_func,

            enrich_func=
                enrich_func,

            profile=
                profile,

            starting_balance=
                starting_balance,

            payout=
                payout,
        )
    )

    best = optimisation.get(
        "best"
    )

    if not best:

        return {
            "market":
                market,

            "symbol":
                symbol,

            "fast_direction":
                candidate.get(
                    "direction"
                ),

            "fast_score":
                candidate.get(
                    "fast_score"
                ),

            "status":
                "NO_DATA",

            "verified":
                False,

            "rejection_reasons": [
                "NO_VALID_BACKTEST_RESULT"
            ],
        }

    trades = int(
        best.get(
            "trades",
            0,
        )
    )

    wins = int(
        best.get(
            "wins",
            0,
        )
    )

    losses = int(
        best.get(
            "losses",
            0,
        )
    )

    win_rate = _safe_float(
        best.get(
            "win_rate",
            0.0,
        )
    )

    return_pct = _safe_float(
        best.get(
            "return_pct",
            0.0,
        )
    )

    max_drawdown = abs(
        _safe_float(
            best.get(
                "max_drawdown",
                0.0,
            )
        )
    )

    profit_factor = _safe_float(
        best.get(
            "profit_factor",
            0.0,
        )
    )

    average_trade_pnl = (
        _safe_float(
            best.get(
                "average_trade_pnl",
                0.0,
            )
        )
    )

    validated_sample = bool(
        best.get(
            "validated_sample",
            best.get(
                "sample_ok",
                False,
            ),
        )
    )

    deep_score = (
        _score_validation(
            best
        )
    )

    reliability_adjusted_score = (
        _reliability_adjusted_score(
            deep_score,
            trades,
        )
    )

    sample_reliability = (
        _sample_reliability(
            trades
        )
    )

    wilson_lower_bound = (
        _wilson_lower_bound(
            wins,
            trades,
        )
    )

    status = (
        _validation_status(
            trades=
                trades,

            win_rate=
                win_rate,

            profit_factor=
                profit_factor,

            max_drawdown=
                max_drawdown,

            validated_sample=
                validated_sample,
        )
    )

    reasons = (
        _verification_failures(
            trades=
                trades,

            win_rate=
                win_rate,

            profit_factor=
                profit_factor,

            max_drawdown=
                max_drawdown,

            validated_sample=
                validated_sample,
        )
    )

    direction = (
        best.get(
            "direction"
        )
        or candidate.get(
            "direction"
        )
        or "WAIT"
    )

    explanation = (
        _build_explanation(
            status=
                status,

            trades=
                trades,

            wins=
                wins,

            losses=
                losses,

            win_rate=
                win_rate,

            profit_factor=
                profit_factor,

            max_drawdown=
                max_drawdown,

            reasons=
                reasons,
        )
    )

    return {
        "market":
            market,

        "symbol":
            symbol,

        "fast_direction":
            candidate.get(
                "direction"
            ),

        "fast_score":
            candidate.get(
                "fast_score"
            ),

        "direction":
            direction,

        "period":
            best.get(
                "period"
            ),

        "interval":
            best.get(
                "interval"
            ),

        "threshold":
            best.get(
                "threshold"
            ),

        "threshold_pct":
            best.get(
                "threshold_pct"
            ),

        "holding_candles":
            best.get(
                "holding_candles"
            ),

        "trades":
            trades,

        "wins":
            wins,

        "losses":
            losses,

        "win_rate":
            win_rate,

        "return_pct":
            return_pct,

        "max_drawdown":
            max_drawdown,

        "profit_factor":
            profit_factor,

        "average_trade_pnl":
            average_trade_pnl,

        "validated_sample":
            validated_sample,

        # -----------------------------------------------
        # V4.9 RELIABILITY METRICS
        # -----------------------------------------------

        "sample_reliability":
            round(
                sample_reliability,
                4,
            ),

        "sample_reliability_pct":
            round(
                sample_reliability
                * 100.0,
                1,
            ),

        "wilson_lower_win_rate":
            round(
                wilson_lower_bound,
                4,
            ),

        "wilson_lower_win_rate_pct":
            round(
                wilson_lower_bound
                * 100.0,
                1,
            ),

        "deep_score":
            deep_score,

        "reliability_adjusted_score":
            reliability_adjusted_score,

        # -----------------------------------------------
        # EXPLAINABLE VALIDATION
        # -----------------------------------------------

        "status":
            status,

        "verified":
            status
            == "VERIFIED",

        "near_verified":
            status
            == "NEAR_VERIFIED",

        "rejection_reasons":
            reasons,

        "primary_reason":
            (
                reasons[0]
                if reasons
                else None
            ),

        "explanation":
            explanation,

        "verification_requirements": {
            "minimum_trades":
                VERIFIED_MIN_TRADES,

            "minimum_win_rate":
                VERIFIED_MIN_WIN_RATE,

            "minimum_profit_factor":
                VERIFIED_MIN_PROFIT_FACTOR,

            "maximum_drawdown":
                VERIFIED_MAX_DRAWDOWN,

            "validated_sample_required":
                True,
        },

        "timeframes_tested":
            optimisation.get(
                "timeframes_tested"
            ),

        "early_stop":
            optimisation.get(
                "early_stop",
                False,
            ),
    }


# ============================================================
# VALIDATE CANDIDATES
# ============================================================

def validate_candidates(
    candidates: List[
        Dict[str, Any]
    ],
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

    shortlist = candidates[
        :max_candidates
    ]

    results: List[
        Dict[str, Any]
    ] = []

    for candidate in shortlist:

        try:

            result = (
                validate_one_market(
                    candidate=
                        candidate,

                    optimise_all_timeframes_func=
                        optimise_all_timeframes_func,

                    get_data_func=
                        get_data_func,

                    add_indicators_func=
                        add_indicators_func,

                    train_model_func=
                        train_model_func,

                    enrich_func=
                        enrich_func,

                    profile=
                        profile,

                    starting_balance=
                        starting_balance,

                    payout=
                        payout,
                )
            )

            results.append(
                result
            )

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

                "status":
                    "ERROR",

                "verified":
                    False,

                "near_verified":
                    False,

                "rejection_reasons": [
                    "VALIDATION_EXCEPTION"
                ],

                "error":
                    str(
                        exc
                    ),
            })

    successful = [
        item
        for item in results
        if item.get(
            "status"
        )
        not in (
            "ERROR",
            "NO_DATA",
        )
    ]

    # ========================================================
    # V4.9 RANKING
    #
    # VERIFIED always first.
    # Then NEAR_VERIFIED.
    # Then WATCH.
    # Then remaining candidates.
    #
    # Within each class, reliability-adjusted score wins.
    # ========================================================

    status_priority = {
        "VERIFIED":
            4,

        "NEAR_VERIFIED":
            3,

        "WATCH":
            2,

        "REJECT":
            1,
    }

    ranked = sorted(
        successful,
        key=lambda item: (
            status_priority.get(
                item.get(
                    "status"
                ),
                0,
            ),
            _safe_float(
                item.get(
                    "reliability_adjusted_score",
                    0.0,
                )
            ),
            int(
                item.get(
                    "trades",
                    0,
                )
            ),
        ),
        reverse=True,
    )

    verified = [
        item
        for item in ranked
        if item.get(
            "status"
        )
        == "VERIFIED"
    ]

    near_verified = [
        item
        for item in ranked
        if item.get(
            "status"
        )
        == "NEAR_VERIFIED"
    ]

    watch = [
        item
        for item in ranked
        if item.get(
            "status"
        )
        == "WATCH"
    ]

    if verified:

        final_market = (
            verified[0]
        )

        final_status = (
            "VERIFIED_TRADE"
        )

    elif near_verified:

        final_market = (
            near_verified[0]
        )

        final_status = (
            "NEAR_VERIFIED"
        )

    elif watch:

        final_market = (
            watch[0]
        )

        final_status = (
            "WATCH_ONLY"
        )

    elif ranked:

        final_market = (
            ranked[0]
        )

        final_status = (
            "NOT_VERIFIED"
        )

    else:

        final_market = None

        final_status = (
            "NO_TRADE"
        )

    return {
        "validator":
            "V4.9_EXPLAINABLE_DEEP_VALIDATION",

        "candidates_received":
            len(
                candidates
            ),

        "candidates_tested":
            len(
                shortlist
            ),

        "successful_validations":
            len(
                successful
            ),

        "verified_markets":
            len(
                verified
            ),

        "near_verified_markets":
            len(
                near_verified
            ),

        "watch_markets":
            len(
                watch
            ),

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
