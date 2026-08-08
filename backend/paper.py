def stake_for_balance(balance: float, risk_per_trade: float) -> float:
    return round(max(balance * risk_per_trade, 0.0), 2)

def backtest(df, profile, starting_balance=10000.0, payout=0.80):
    x = df.dropna().copy()
    balance = float(starting_balance)
    peak = balance
    current_day = None
    day_start = balance
    losses = 0
    rows = []

    for i in range(len(x)-1):
        row, nxt = x.iloc[i], x.iloc[i+1]
        dt = x.index[i]
        day = dt.date() if hasattr(dt, "date") else dt
        if day != current_day:
            current_day = day
            day_start = balance
            losses = 0

        if (balance-day_start)/max(day_start,1e-9) <= -profile.daily_loss_limit:
            continue
        if losses >= profile.max_consecutive_losses:
            continue
        if not bool(row["QUALITY_OK"]):
            continue
        if float(row["CONFIDENCE"]) < profile.min_confidence:
            continue

        direction = str(row["DIRECTION"])
        won = ((direction=="BUY" and nxt["Close"] > row["Close"]) or
               (direction=="SELL" and nxt["Close"] < row["Close"]))

        stake = balance * profile.risk_per_trade
        pnl = stake*payout if won else -stake
        balance += pnl
        peak = max(peak, balance)
        drawdown = (balance-peak)/peak
        losses = 0 if won else losses+1

        rows.append({
            "time": str(dt),
            "direction": direction,
            "confidence": float(row["CONFIDENCE"]),
            "stake": float(stake),
            "won": bool(won),
            "pnl": float(pnl),
            "balance": float(balance),
            "drawdown": float(drawdown),
        })

    wins = sum(1 for r in rows if r["won"])
    return {
        "trades": len(rows),
        "wins": wins,
        "win_rate": wins/len(rows) if rows else 0.0,
        "starting_balance": starting_balance,
        "ending_balance": balance,
        "return_pct": balance/starting_balance - 1 if starting_balance else 0,
        "max_drawdown": min([r["drawdown"] for r in rows], default=0.0),
        "equity_curve": [{"time": r["time"], "balance": r["balance"]} for r in rows],
        "journal": rows[-200:],
    }
