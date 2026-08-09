from strategy_optimizer import optimise_strategy


TIMEFRAMES = [
    ("5m", "1mo"),
    ("15m", "1mo"),
    ("30m", "1mo"),
    ("1h", "3mo"),
]


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
    results = []

    for interval, period in TIMEFRAMES:
        try:
            raw = get_data_func(
                symbol,
                period,
                interval,
            )

            ind = add_indicators_func(raw)
            model = train_model_func(ind)
            enriched = enrich_func(ind, model)

            optimisation = optimise_strategy(
                enriched,
                profile,
                starting_balance=starting_balance,
                payout=payout,
                min_trades=10,
            )

            best = optimisation.get("best")
            best_any = optimisation.get("best_any_sample")

            if best is not None:
                candidate = dict(best)
                candidate["interval"] = interval
                candidate["period"] = period
                candidate["symbol"] = symbol
                candidate["validated_sample"] = True

                results.append(candidate)

            elif best_any is not None:
                candidate = dict(best_any)
                candidate["interval"] = interval
                candidate["period"] = period
                candidate["symbol"] = symbol
                candidate["validated_sample"] = False

                results.append(candidate)

        except Exception as e:
            results.append({
                "symbol": symbol,
                "interval": interval,
                "period": period,
                "error": str(e),
                "validated_sample": False,
            })

    successful = [
        r for r in results
        if "error" not in r
    ]

    ranked = sorted(
        successful,
        key=lambda x: (
            x.get("validated_sample", False),
            x.get("score", -999999),
        ),
        reverse=True,
    )

    return {
        "symbol": symbol,
        "timeframes_tested": len(TIMEFRAMES),
        "best": ranked[0] if ranked else None,
        "ranking": ranked,
        "all_results": results,
    }
