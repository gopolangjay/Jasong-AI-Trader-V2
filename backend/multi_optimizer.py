from strategy_optimizer import optimise_strategy


# ============================================================
# V4.6 FAST MULTI-TIMEFRAME OPTIMIZER
# ============================================================
#
# Designed for Render/free-instance stability.
#
# Instead of running every expensive timeframe every time,
# we test the most useful combinations first and stop early
# when a strong validated result is found.
# ============================================================


TIMEFRAMES = [
    ("15m", "1mo"),
    ("30m", "1mo"),
    ("1h", "3mo"),
]


def _candidate_score(
    candidate,
):
    """
    Safe numeric score helper.
    """

    try:
        return float(
            candidate.get(
                "score",
                -999999,
            )
        )

    except Exception:
        return -999999.0


def _is_strong_enough(
    candidate,
):
    """
    Early-stop rule.

    This does NOT mean guaranteed profit.
    It simply means the optimiser already found a
    sufficiently strong validated historical setup,
    so there is little benefit in continuing to run
    more expensive timeframe tests.
    """

    if not candidate:
        return False

    if not bool(
        candidate.get(
            "validated_sample",
            False,
        )
    ):
        return False

    trades = int(
        candidate.get(
            "trades",
            0,
        )
    )

    win_rate = float(
        candidate.get(
            "win_rate",
            0.0,
        )
    )

    profit_factor = float(
        candidate.get(
            "profit_factor",
            0.0,
        )
    )

    max_drawdown = abs(
        float(
            candidate.get(
                "max_drawdown",
                0.0,
            )
        )
    )

    return (
        trades >= 20
        and win_rate >= 0.70
        and profit_factor >= 1.50
        and max_drawdown <= 0.05
    )


def optimise_all_timeframes(
    symbol,
    get_data_func,
    add_indicators_func,
    train_model_func,
    enrich_func,
    profile,
    starting_balance=10000.0,
    payout=0.80,
):
    """
    Optimise one market across a reduced set of useful
    timeframes.

    V4.6 improvements:
    - removes redundant 5m heavy optimisation
    - tests 15m first
    - tests 30m second using locally resampled data
    - tests 1h / 3mo only if still needed
    - supports early stop on a strong validated candidate
    """

    results = []

    tested = 0

    early_stop = False

    for interval, period in TIMEFRAMES:

        tested += 1

        try:
            # =================================================
            # GET DATA
            # =================================================

            raw = get_data_func(
                symbol,
                period,
                interval,
            )

            if raw is None or len(raw) < 80:

                results.append({
                    "symbol":
                        symbol,

                    "interval":
                        interval,

                    "period":
                        period,

                    "error":
                        "Not enough market data",

                    "validated_sample":
                        False,
                })

                continue

            # =================================================
            # INDICATORS
            # =================================================

            ind = add_indicators_func(
                raw
            )

            if ind is None or len(ind) < 80:

                results.append({
                    "symbol":
                        symbol,

                    "interval":
                        interval,

                    "period":
                        period,

                    "error":
                        "Not enough indicator data",

                    "validated_sample":
                        False,
                })

                continue

            # =================================================
            # MODEL
            # =================================================

            model = train_model_func(
                ind
            )

            # =================================================
            # ENRICH
            # =================================================

            enriched = enrich_func(
                ind,
                model,
            )

            if (
                enriched is None
                or len(enriched) < 80
            ):

                results.append({
                    "symbol":
                        symbol,

                    "interval":
                        interval,

                    "period":
                        period,

                    "error":
                        "Not enough enriched data",

                    "validated_sample":
                        False,
                })

                continue

            # =================================================
            # STRATEGY OPTIMISATION
            # =================================================

            optimisation = optimise_strategy(
                enriched,
                profile,
                starting_balance=
                    starting_balance,
                payout=
                    payout,
                min_trades=
                    10,
            )

            best = optimisation.get(
                "best"
            )

            best_any = optimisation.get(
                "best_any_sample"
            )

            candidate = None

            # =================================================
            # VALIDATED SAMPLE
            # =================================================

            if best is not None:

                candidate = dict(
                    best
                )

                candidate[
                    "validated_sample"
                ] = True

            # =================================================
            # FALLBACK SAMPLE
            # =================================================

            elif best_any is not None:

                candidate = dict(
                    best_any
                )

                candidate[
                    "validated_sample"
                ] = False

            # =================================================
            # STORE CANDIDATE
            # =================================================

            if candidate is not None:

                candidate[
                    "interval"
                ] = interval

                candidate[
                    "period"
                ] = period

                candidate[
                    "symbol"
                ] = symbol

                results.append(
                    candidate
                )

                # =============================================
                # EARLY STOP
                # =============================================

                if _is_strong_enough(
                    candidate
                ):

                    early_stop = True

                    break

        except Exception as e:

            results.append({
                "symbol":
                    symbol,

                "interval":
                    interval,

                "period":
                    period,

                "error":
                    str(e),

                "validated_sample":
                    False,
            })

    # =========================================================
    # SUCCESSFUL RESULTS ONLY
    # =========================================================

    successful = [
        result
        for result in results
        if "error" not in result
    ]

    # =========================================================
    # RANK
    # =========================================================

    ranked = sorted(
        successful,
        key=lambda item: (
            bool(
                item.get(
                    "validated_sample",
                    False,
                )
            ),
            _candidate_score(
                item
            ),
        ),
        reverse=True,
    )

    # =========================================================
    # RETURN
    # =========================================================

    return {
        "symbol":
            symbol,

        "timeframes_configured":
            len(TIMEFRAMES),

        "timeframes_tested":
            tested,

        "early_stop":
            early_stop,

        "best":
            (
                ranked[0]
                if ranked
                else None
            ),

        "ranking":
            ranked,

        "all_results":
            results,
    }
