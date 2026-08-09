from multi_optimizer import optimise_all_timeframes


def classify_result(
    trades,
    win_rate,
    profit_factor,
    return_pct,
    max_drawdown,
):
    """
    Historical strategy classification.
    This is not a guarantee of future performance.
    """

    if (
        trades >= 40
        and win_rate >= 0.70
        and profit_factor >= 1.50
        and return_pct > 0
        and max_drawdown <= 0.08
    ):
        return "STRONG"

    if (
        trades >= 20
        and win_rate >= 0.60
        and profit_factor >= 1.20
        and return_pct > 0
        and max_drawdown <= 0.10
    ):
        return "QUALIFIED"

    if (
        trades >= 15
        and win_rate >= 0.55
        and profit_factor >= 1.00
        and return_pct > 0
    ):
        return "WATCH"

    return "REJECT"


def calculate_market_score(
    trades,
    win_rate,
    profit_factor,
    return_pct,
    max_drawdown,
):
    """
    Composite ranking score with a sample-size penalty.
    """

    sample_factor = min(trades / 50.0, 1.0)

    score = (
        (win_rate * 40.0)
        + (min(profit_factor, 5.0) * 8.0)
        + (return_pct * 100.0)
        - (max_drawdown * 100.0)
    )

    return round(score * sample_factor, 2)


def scan_markets(
    markets,
    get_data_func,
    add_indicators_func,
    train_model_func,
    enrich_func,
    profiles,
    risk_mode="Balanced",
    starting_balance=10000.0,
    payout=0.80,
    min_win_rate=0.65,
    min_profit_factor=1.20,
    max_drawdown_limit=0.08,
    min_trades=10,
):
    if risk_mode not in profiles:
        raise ValueError("Invalid risk mode")

    profile = profiles[risk_mode]
    candidates = []

    for market_name, symbol in markets.items():
        try:
            optimisation = optimise_all_timeframes(
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
                candidates.append({
                    "market": market_name,
                    "symbol": symbol,
                    "status": "NO_DATA",
                    "market_score": 0.0,
                })
                continue

            trades = int(best.get("trades", 0))
            wins = int(best.get("wins", 0))
            losses = int(best.get("losses", 0))
            win_rate = float(best.get("win_rate", 0.0))
            profit_factor = float(best.get("profit_factor", 0.0))
            return_pct = float(best.get("return_pct", 0.0))
            max_drawdown_raw = float(
                best.get("max_drawdown", 0.0)
            )
            max_drawdown = abs(max_drawdown_raw)

            status = classify_result(
                trades,
                win_rate,
                profit_factor,
                return_pct,
                max_drawdown,
            )

            market_score = calculate_market_score(
                trades,
                win_rate,
                profit_factor,
                return_pct,
                max_drawdown,
            )

            candidates.append({
                "market": market_name,
                "symbol": symbol,
                "status": status,

                "interval": best.get("interval"),
                "period": best.get("period"),

                "threshold": best.get("threshold"),
                "threshold_pct": best.get("threshold_pct"),
                "holding_candles": best.get(
                    "holding_candles"
                ),

                "trades": trades,
                "wins": wins,
                "losses": losses,

                "win_rate": win_rate,
                "return_pct": return_pct,
                "max_drawdown": max_drawdown_raw,
                "profit_factor": profit_factor,

                "average_trade_pnl": float(
                    best.get("average_trade_pnl", 0.0)
                ),

                "optimizer_score": float(
                    best.get("score", 0.0)
                ),

                "market_score": market_score,

                "validated_sample": bool(
                    best.get("validated_sample", False)
                ),
            })

        except Exception as e:
            candidates.append({
                "market": market_name,
                "symbol": symbol,
                "status": "ERROR",
                "market_score": 0.0,
                "error": str(e),
            })

    ranked_all = sorted(
        candidates,
        key=lambda x: x.get("market_score", 0.0),
        reverse=True,
    )

    qualified = [
        c for c in ranked_all
        if c.get("status") in (
            "STRONG",
            "QUALIFIED",
        )
    ]

    watchlist = [
        c for c in ranked_all
        if c.get("status") == "WATCH"
    ]

    return {
        "risk_mode": risk_mode,
        "markets_tested": len(markets),

        "qualified_markets": len(qualified),

        "best_market":
            qualified[0]
            if qualified
            else None,

        "ranking": qualified,

        "watchlist": watchlist,

        "all_results": ranked_all,

        "live_execution": False,
    }
