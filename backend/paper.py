from typing import Dict, List
import math


def stake_for_balance(balance: float, risk_per_trade: float) -> float:
    return round(max(balance * risk_per_trade, 0.0), 2)


def _profit_factor(rows: List[dict]) -> float:
    gross_profit = sum(r["pnl"] for r in rows if r["pnl"] > 0)
    gross_loss = abs(sum(r["pnl"] for r in rows if r["pnl"] < 0))

    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0

    return gross_profit / gross_loss


def backtest(
    df,
    profile,
    starting_balance: float = 10000.0,
    payout: float = 0.80,
    holding_candles: int = 4,
):
    """
    V4 backtest.

    A signal is evaluated over multiple future candles instead of only the
    immediately following candle.

    Risk controls:
    - risk percentage per trade
    - daily loss limit
    - consecutive-loss limit
    - confidence threshold
    - volatility-quality filter
    """

    x = df.dropna().copy()

    balance = float(starting_balance)
    peak = balance

    current_day = None
    day_start_balance = balance
    consecutive_losses = 0

    trades = []
diagnostics = {
    "candles_tested": 0,
    "quality_rejected": 0,
    "confidence_rejected": 0,
    "signals_accepted": 0,
    "max_confidence_seen": 0.0,
    "confidence_sum": 0.0,
    "confidence_count": 0,
}
    if len(x) <= holding_candles:
        return _empty_result(starting_balance)

    for i in range(len(x) - holding_candles):

        row = x.iloc[i]
        future = x.iloc[i + holding_candles]
diagnostics["candles_tested"] += 1

confidence = float(row["CONFIDENCE"])

diagnostics["max_confidence_seen"] = max(
    diagnostics["max_confidence_seen"],
    confidence,
)

diagnostics["confidence_sum"] += confidence
diagnostics["confidence_count"] += 1
        dt = x.index[i]

        day = (
            dt.date()
            if hasattr(dt, "date")
            else dt
        )

        if day != current_day:
            current_day = day
            day_start_balance = balance
            consecutive_losses = 0

        # Daily circuit breaker
        daily_return = (
            balance - day_start_balance
        ) / max(day_start_balance, 1e-9)

        if daily_return <= -profile.daily_loss_limit:
            continue

        # Consecutive-loss circuit breaker
        if (
            consecutive_losses
            >= profile.max_consecutive_losses
        ):
            continue

        # Market-quality gate
        if not bool(row["QUALITY_OK"]):
    diagnostics["quality_rejected"] += 1
    continue

if confidence < profile.min_confidence:
    diagnostics["confidence_rejected"] += 1
    continue

diagnostics["signals_accepted"] += 1

        direction = str(
            row["DIRECTION"]
        )

        entry_price = float(
            row["Close"]
        )

        exit_price = float(
            future["Close"]
        )

        if direction == "BUY":
            won = exit_price > entry_price

        elif direction == "SELL":
            won = exit_price < entry_price

        else:
            continue

        stake = (
            balance
            * profile.risk_per_trade
        )

        pnl = (
            stake * payout
            if won
            else -stake
        )

        balance += pnl

        peak = max(
            peak,
            balance,
        )

        drawdown = (
            balance - peak
        ) / peak

        if won:
            consecutive_losses = 0
        else:
            consecutive_losses += 1

        trades.append(
            {
                "time": str(dt),
                "direction": direction,
                "confidence": confidence,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "holding_candles": holding_candles,
                "stake": float(stake),
                "won": bool(won),
                "pnl": float(pnl),
                "balance": float(balance),
                "drawdown": float(drawdown),
            }
        )

    if not trades:
    result = _empty_result(starting_balance)

    if diagnostics["confidence_count"] > 0:
        diagnostics["average_confidence"] = (
            diagnostics["confidence_sum"]
            / diagnostics["confidence_count"]
        )
    else:
        diagnostics["average_confidence"] = 0.0

    diagnostics["required_confidence"] = float(
        profile.min_confidence
    )

    result["diagnostics"] = diagnostics

    return result

    wins = sum(
        1
        for trade in trades
        if trade["won"]
    )

    losses = (
        len(trades) - wins
    )

    total_pnl = sum(
        trade["pnl"]
        for trade in trades
    )

    avg_trade = (
        total_pnl
        / len(trades)
    )

    max_drawdown = min(
        trade["drawdown"]
        for trade in trades
    )

    profit_factor = _profit_factor(
        trades
    )

    result = {
        "trades":
            len(trades),

        "wins":
            wins,

        "losses":
            losses,

        "win_rate":
            wins / len(trades),

        "starting_balance":
            float(starting_balance),

        "ending_balance":
            float(balance),

        "return_pct":
            (
                balance
                / starting_balance
                - 1
            )
            if starting_balance
            else 0.0,

        "max_drawdown":
            float(max_drawdown),

        "profit_factor":
            (
                float(profit_factor)
                if math.isfinite(profit_factor)
                else 999.0
            ),

        "average_trade_pnl":
            float(avg_trade),

        "holding_candles":
            holding_candles,

        "equity_curve": [
            {
                "time": trade["time"],
                "balance": trade["balance"],
            }
            for trade in trades
        ],

        "journal":
            trades[-200:],
    }
if diagnostics["confidence_count"] > 0:
    diagnostics["average_confidence"] = (
        diagnostics["confidence_sum"]
        / diagnostics["confidence_count"]
    )
else:
    diagnostics["average_confidence"] = 0.0

diagnostics["required_confidence"] = float(
    profile.min_confidence
)

result["diagnostics"] = diagnostics
    return result


def _empty_result(
    starting_balance: float,
) -> Dict:
    return {
        "trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "starting_balance":
            float(starting_balance),
        "ending_balance":
            float(starting_balance),
        "return_pct": 0.0,
        "max_drawdown": 0.0,
        "profit_factor": 0.0,
        "average_trade_pnl": 0.0,
        "holding_candles": 4,
        "equity_curve": [],
        "journal": [],
    }
