from dataclasses import replace
import math

from paper import backtest


# ============================================================
# JASONG AI TRADER V4.9
# RELIABILITY-AWARE STRATEGY OPTIMIZER
# ============================================================


DEFAULT_THRESHOLDS = [
    0.45,
    0.50,
    0.55,
    0.60,
    0.62,
    0.65,
    0.67,
    0.70,
]

DEFAULT_HOLDING_CANDLES = [
    1,
    2,
    3,
    4,
    6,
    8,
]


def _safe_float(
    value,
    default=0.0,
):
    try:
        number = float(value)

        if math.isnan(number) or math.isinf(number):
            return default

        return number

    except (TypeError, ValueError):
        return default


def _sample_reliability(
    trades: int,
) -> float:
    """
    0.0 to 1.0 sample reliability indicator.

    30+ qualifying trades receives full sample weight.

    This is NOT a probability that the strategy will win.
    """

    if trades <= 0:
        return 0.0

    return min(
        trades / 30.0,
        1.0,
    )


def _adjusted_win_rate(
    win_rate: float,
    trades: int,
) -> float:
    """
    Shrink small-sample win rates toward 50%.

    Examples:

    9/10 = 90% raw win rate
    but only 33% sample reliability.

    The adjusted rate therefore does not allow
    a tiny sample to dominate the optimiser.
    """

    reliability = _sample_reliability(
        trades
    )

    return (
        0.50
        + (
            win_rate - 0.50
        ) * reliability
    )


def optimise_strategy(
    df,
    base_profile,
    starting_balance=10000.0,
    payout=0.80,
    thresholds=None,
    holding_periods=None,
    min_trades=5,
):
    thresholds = (
        thresholds
        or DEFAULT_THRESHOLDS
    )

    holding_periods = (
        holding_periods
        or DEFAULT_HOLDING_CANDLES
    )

    results = []

    for threshold in thresholds:

        for holding_candles in holding_periods:

            profile = replace(
                base_profile,
                min_confidence=float(
                    threshold
                ),
            )

            result = backtest(
                df,
                profile,
                starting_balance=
                    starting_balance,
                payout=
                    payout,
                holding_candles=
                    holding_candles,
            )

            trades = int(
                result.get(
                    "trades",
                    0,
                )
            )

            wins = int(
                result.get(
                    "wins",
                    0,
                )
            )

            losses = int(
                result.get(
                    "losses",
                    0,
                )
            )

            win_rate = _safe_float(
                result.get(
                    "win_rate",
                    0.0,
                )
            )

            return_pct = _safe_float(
                result.get(
                    "return_pct",
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

            profit_factor = _safe_float(
                result.get(
                    "profit_factor",
                    0.0,
                )
            )

            average_trade_pnl = (
                _safe_float(
                    result.get(
                        "average_trade_pnl",
                        0.0,
                    )
                )
            )

            sample_reliability = (
                _sample_reliability(
                    trades
                )
            )

            adjusted_win_rate = (
                _adjusted_win_rate(
                    win_rate,
                    trades,
                )
            )

            # ================================================
            # HARD MINIMUM SAMPLE PENALTY
            # ================================================

            sample_penalty = 0.0

            if trades < min_trades:

                sample_penalty += (
                    min_trades - trades
                ) * 10.0

            # ================================================
            # V4.9 SMALL-SAMPLE PENALTY
            #
            # A 9/10 result remains interesting, but it
            # should not automatically beat a robust
            # 30-100 trade setup.
            # ================================================

            small_sample_penalty = 0.0

            if trades < 20:

                small_sample_penalty = (
                    20 - trades
                ) * 0.75

            # ================================================
            # PROFIT FACTOR CAP
            #
            # PF 7 is excellent, but a single losing trade
            # can make PF extremely large on tiny samples.
            # We cap its ranking contribution at PF 3.
            # ================================================

            pf_for_score = min(
                max(
                    profit_factor,
                    0.0,
                ),
                3.0,
            )

            # ================================================
            # SAMPLE BONUS
            # ================================================

            sample_bonus = min(
                trades,
                50,
            ) * 0.15

            # ================================================
            # V4.9 RELIABILITY-AWARE SCORE
            #
            # This is ONLY an optimiser ranking score.
            # It is NOT a win probability.
            # ================================================

            score = (
                return_pct * 100.0
                + adjusted_win_rate * 30.0
                + pf_for_score * 8.0
                + sample_bonus
                - max_drawdown * 100.0
                - sample_penalty
                - small_sample_penalty
            )

            results.append({
                "threshold":
                    float(
                        threshold
                    ),

                "threshold_pct":
                    round(
                        float(
                            threshold
                        ) * 100,
                        1,
                    ),

                "holding_candles":
                    int(
                        holding_candles
                    ),

                "trades":
                    trades,

                "wins":
                    wins,

                "losses":
                    losses,

                "win_rate":
                    win_rate,

                "adjusted_win_rate":
                    adjusted_win_rate,

                "sample_reliability":
                    sample_reliability,

                "return_pct":
                    return_pct,

                "max_drawdown":
                    max_drawdown,

                "profit_factor":
                    profit_factor,

                "average_trade_pnl":
                    average_trade_pnl,

                "score":
                    float(
                        score
                    ),

                "sample_ok":
                    trades >= min_trades,

                "full_validation_sample":
                    trades >= 20,
            })

    # ========================================================
    # RANK
    # ========================================================

    ranked = sorted(
        results,
        key=lambda item: (
            item[
                "sample_ok"
            ],
            item[
                "score"
            ],
            item[
                "trades"
            ],
        ),
        reverse=True,
    )

    valid = [
        item
        for item in ranked
        if item[
            "sample_ok"
        ]
    ]

    return {
        "optimizer":
            "V4.9_RELIABILITY_AWARE",

        "combinations_tested":
            len(
                results
            ),

        "best":
            (
                valid[0]
                if valid
                else None
            ),

        "best_any_sample":
            (
                ranked[0]
                if ranked
                else None
            ),

        "ranking":
            ranked,
    }
