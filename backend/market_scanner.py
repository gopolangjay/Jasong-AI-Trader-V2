from multi_optimizer import optimise_all_timeframes


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
                })
                continue

            trades = int(best.get("trades", 0))
            win_rate = float(best.get("win_rate", 0.0))
            profit_factor = float(best.get("profit_factor", 0.0))
            max_drawdown = abs(float(best.get("max_drawdown", 0.0)))
            score = float(best.get("score", -999999))

            quality_pass = (
                trades >= min_trades
                and win_rate >= min_win_rate
                and profit_factor >= min_profit_factor
                and max_drawdown <= max_drawdown_limit
                and bool(best.get("validated_sample", False))
            )

            candidates.append({
                "market": market_name,
                "symbol": symbol,
                "status": "PASS" if quality_pass else "REJECT",
                "interval": best.get("interval"),
                "period": best.get("period"),
                "threshold": best.get("threshold"),
                "threshold_pct": best.get("threshold_pct"),
                "holding_candles": best.get("holding_candles"),
                "trades": trades,
                "wins": int(best.get("wins", 0)),
                "losses": int(best.get("losses", 0)),
                "win_rate": win_rate,
                "return_pct": float(best.get("return_pct", 0.0)),
                "max_drawdown": float(best.get("max_drawdown", 0.0)),
                "profit_factor": profit_factor,
                "average_trade_pnl": float(
                    best.get("average_trade_pnl", 0.0)
                ),
                "score": score,
                "validated_sample": bool(
                    best.get("validated_sample", False)
                ),
            })

        except Exception as e:
            candidates.append({
                "market": market_name,
                "symbol": symbol,
                "status": "ERROR",
                "error": str(e),
            })

    passed = [
        c for c in candidates
        if c.get("status") == "PASS"
    ]

    ranked = sorted(
        passed,
        key=lambda x: x.get("score", -999999),
        reverse=True,
    )

    return {
        "risk_mode": risk_mode,
        "markets_tested": len(markets),
        "qualified_markets": len(ranked),
        "best_market": ranked[0] if ranked else None,
        "ranking": ranked,
        "all_results": candidates,
        "live_execution": False,
    }
