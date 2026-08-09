from typing import Dict, Any, List


MARKETS = [
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "AUDUSD",
    "NZDUSD",
    "USDCAD",
    "USDCHF",
    "EURJPY",
    "GBPJPY",
]


def build_top_markets(
    results: List[Dict[str, Any]],
    top_n: int = 3,
):
    """
    Combine already-completed single-market scan results
    and rank them by market_score.

    This function does NOT run the heavy optimisation itself.
    """

    cleaned = []

    for result in results:
        if not result:
            continue

        status = result.get("status", "REJECT")
        score = float(result.get("market_score", 0.0))

        best = result.get("best") or {}

        cleaned.append({
            "market": result.get("market"),
            "symbol": result.get("symbol"),
            "status": status,
            "market_score": score,

            "interval": best.get("interval"),
            "period": best.get("period"),

            "threshold": best.get("threshold"),
            "threshold_pct": best.get("threshold_pct"),

            "holding_candles": best.get("holding_candles"),

            "trades": best.get("trades", 0),
            "wins": best.get("wins", 0),
            "losses": best.get("losses", 0),

            "win_rate": best.get("win_rate", 0.0),
            "return_pct": best.get("return_pct", 0.0),
            "max_drawdown": best.get("max_drawdown", 0.0),
            "profit_factor": best.get("profit_factor", 0.0),

            "average_trade_pnl":
                best.get("average_trade_pnl", 0.0),

            "validated_sample":
                best.get("validated_sample", False),
        })

    ranked = sorted(
        cleaned,
        key=lambda x: x.get("market_score", 0.0),
        reverse=True,
    )

    qualified = [
        row for row in ranked
        if row.get("status")
        in ("STRONG", "QUALIFIED")
    ]

    watchlist = [
        row for row in ranked
        if row.get("status") == "WATCH"
    ]

    top_markets = qualified[:top_n]

    return {
        "markets_received": len(cleaned),
        "qualified_markets": len(qualified),
        "top_markets": top_markets,
        "best_market":
            top_markets[0]
            if top_markets
            else None,
        "watchlist": watchlist,
        "all_ranked": ranked,
        "live_execution": False,
    }
