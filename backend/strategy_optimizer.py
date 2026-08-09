from dataclasses import replace

from paper import backtest


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

DEFAULT_HOLDING_CANDLES = [1, 2, 3, 4, 6, 8]


def optimise_strategy(
    df,
    base_profile,
    starting_balance=10000.0,
    payout=0.80,
    thresholds=None,
    holding_periods=None,
    min_trades=5,
):
    thresholds = thresholds or DEFAULT_THRESHOLDS
    holding_periods = holding_periods or DEFAULT_HOLDING_CANDLES

    results = []

    for threshold in thresholds:
        for holding_candles in holding_periods:

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

            trades = int(result.get("trades", 0))
            win_rate = float(result.get("win_rate", 0.0))
            return_pct = float(result.get("return_pct", 0.0))
            max_drawdown = float(result.get("max_drawdown", 0.0))
            profit_factor = float(result.get("profit_factor", 0.0))

            # Basic V4 quality score.
            # Rewards return, win rate and PF.
            # Penalises drawdown and tiny samples.
            sample_penalty = 0.0

            if trades < min_trades:
                sample_penalty = (min_trades - trades) * 10.0

            score = (
                return_pct * 100.0
                + win_rate * 25.0
                + min(profit_factor, 3.0) * 10.0
                - abs(max_drawdown) * 100.0
                - sample_penalty
            )

            results.append({
                "threshold": float(threshold),
                "threshold_pct": round(float(threshold) * 100, 1),
                "holding_candles": int(holding_candles),
                "trades": trades,
                "wins": int(result.get("wins", 0)),
                "losses": int(result.get("losses", 0)),
                "win_rate": win_rate,
                "return_pct": return_pct,
                "max_drawdown": max_drawdown,
                "profit_factor": profit_factor,
                "average_trade_pnl": float(
                    result.get("average_trade_pnl", 0.0)
                ),
                "score": float(score),
                "sample_ok": trades >= min_trades,
            })

    ranked = sorted(
        results,
        key=lambda x: x["score"],
        reverse=True,
    )

    valid = [
        r for r in ranked
        if r["sample_ok"]
    ]

    return {
        "combinations_tested": len(results),
        "best": valid[0] if valid else None,
        "best_any_sample": ranked[0] if ranked else None,
        "ranking": ranked,
    }
