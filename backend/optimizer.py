from dataclasses import replace

from paper import backtest


DEFAULT_THRESHOLDS = [
    0.35,
    0.40,
    0.45,
    0.50,
    0.55,
    0.60,
    0.62,
    0.65,
    0.67,
    0.70,
    0.72,
    0.75,
]


def threshold_sweep(
    df,
    base_profile,
    starting_balance=10000.0,
    payout=0.80,
    thresholds=None,
    holding_candles=4,
):
    thresholds = thresholds or DEFAULT_THRESHOLDS

    results = []

    for threshold in thresholds:
        profile = replace(
            base_profile,
            min_confidence=float(threshold),
        )

        result = backtest(
            df,
            profile,
            starting_balance=starting_balance,
            payout=payout,
            holding_candles=holding_candles,
        )

        results.append({
            "threshold": float(threshold),
            "threshold_pct": round(float(threshold) * 100, 1),
            "trades": int(result.get("trades", 0)),
            "wins": int(result.get("wins", 0)),
            "losses": int(result.get("losses", 0)),
            "win_rate": float(result.get("win_rate", 0.0)),
            "return_pct": float(result.get("return_pct", 0.0)),
            "max_drawdown": float(result.get("max_drawdown", 0.0)),
            "profit_factor": float(result.get("profit_factor", 0.0)),
            "average_trade_pnl": float(
                result.get("average_trade_pnl", 0.0)
            ),
        })

    ranked = sorted(
        results,
        key=lambda x: (
            x["return_pct"],
            x["profit_factor"],
            x["win_rate"],
            -abs(x["max_drawdown"]),
            x["trades"],
        ),
        reverse=True,
    )

    return {
        "tested_thresholds": len(results),
        "best": ranked[0] if ranked else None,
        "results": results,
        "ranking": ranked,
    }
