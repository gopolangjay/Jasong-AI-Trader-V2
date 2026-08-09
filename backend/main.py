from market_scanner import classify_result, calculate_market_score
from market_scanner import scan_markets
from multi_optimizer import optimise_all_timeframes
from strategy_optimizer import optimise_strategy
from optimizer import threshold_sweep
MARKETS = {
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "JPY=X",
    "AUDUSD": "AUDUSD=X",
    "NZDUSD": "NZDUSD=X",
    "USDCAD": "CAD=X",
    "USDCHF": "CHF=X",
    "EURJPY": "EURJPY=X",
    "GBPJPY": "GBPJPY=X",
}
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
import pandas as pd
import yfinance as yf

from indicators import add_indicators
from engine import PROFILES, train_model, enrich, decision
from paper import backtest, stake_for_balance
from database import SessionLocal, Trade, init_db

app = FastAPI(title="Jasong AI Trader V3 API", version="3.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
init_db()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_data(symbol: str, period="1mo", interval="15m"):
    try:
        periods_to_try = [period, "1mo", "5d"]

        for p in periods_to_try:
            d = yf.download(
                symbol,
                period=p,
                interval=interval,
                progress=False,
                auto_adjust=True
            )

            if isinstance(d.columns, pd.MultiIndex):
                d.columns = d.columns.get_level_values(0)

            if all(col in d.columns for col in ["Open", "High", "Low", "Close"]):
                d = d.dropna(subset=["Open", "High", "Low", "Close"])

                if len(d) >= 80:
                    return d

        raise ValueError(
            f"Not enough market data for {symbol} at {interval}"
        )

    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Market data unavailable: {e}"
        )

def build(symbol: str, period: str, interval: str):
    raw = get_data(symbol, period, interval)
    ind = add_indicators(raw)
    model = train_model(ind)
    return enrich(ind, model)

@app.get("/")
def root():
    return {
        "name":"Jasong AI Trader V3",
        "mode":"paper-trading",
        "live_execution":False,
        "message":"AI-assisted research and paper trading only."
    }

@app.get("/health")
def health():
    return {"status":"ok","version":"3.0.0","live_execution":False}

@app.get("/signal")
def signal(symbol: str="EURUSD=X", risk_mode: str="Balanced",
           period: str="1mo", interval: str="15m", balance: float=10000.0):
    if risk_mode not in PROFILES:
        raise HTTPException(status_code=400, detail="Invalid risk mode")
    profile = PROFILES[risk_mode]
    sig = build(symbol, period, interval)
    out = decision(sig, profile)
    out.update({
        "symbol":symbol,
        "risk_mode":risk_mode,
        "suggested_paper_stake": stake_for_balance(balance, profile.risk_per_trade),
        "live_execution":False,
    })
    return out

@app.get("/backtest")
def run_backtest(symbol: str="EURUSD=X", risk_mode: str="Balanced",
                 period: str="1mo", interval: str="15m",
                 starting_balance: float=10000.0, payout: float=0.80):
    if risk_mode not in PROFILES:
        raise HTTPException(status_code=400, detail="Invalid risk mode")
    sig = build(symbol, period, interval)
    result = backtest(sig, PROFILES[risk_mode], starting_balance, payout)
    result.update({"symbol":symbol,"risk_mode":risk_mode,"live_execution":False})
    return result

@app.post("/paper-trades")
def create_paper_trade(symbol: str, direction: str, confidence: float,
                       entry_price: float, stake: float,
                       db: Session = Depends(get_db)):
    if direction not in {"BUY","SELL"}:
        raise HTTPException(status_code=400, detail="Direction must be BUY or SELL")
    t = Trade(symbol=symbol, direction=direction, confidence=confidence,
              entry_price=entry_price, stake=stake, mode="paper")
    db.add(t)
    db.commit()
    db.refresh(t)
    return {"id":t.id,"status":"recorded","live_execution":False}

@app.get("/paper-trades")
def list_paper_trades(db: Session = Depends(get_db)):
    rows = db.query(Trade).order_by(Trade.created_at.desc()).limit(200).all()
    return [{
        "id":r.id,
        "created_at":r.created_at.isoformat(),
        "symbol":r.symbol,
        "direction":r.direction,
        "confidence":r.confidence,
        "entry_price":r.entry_price,
        "stake":r.stake,
        "result":r.result,
        "pnl":r.pnl,
        "closed":r.closed,
    } for r in rows]
@app.get("/backtest-all")
def run_backtest_all(
    risk_mode: str = "Balanced",
    period: str = "1mo",
    interval: str = "15m",
    starting_balance: float = 10000.0,
    payout: float = 0.80,
):

    if risk_mode not in PROFILES:
        raise HTTPException(
            status_code=400,
            detail="Invalid risk mode"
        )

    profile = PROFILES[risk_mode]

    results = []

    for name, symbol in MARKETS.items():
        try:
            sig = build(symbol, period, interval)

            result = backtest(
                sig,
                profile,
                starting_balance,
                payout
            )

            results.append({
                "market": name,
                "symbol": symbol,
                "trades": result.get("trades", 0),
                "wins": result.get("wins", 0),
                "losses": result.get("losses", 0),
                "win_rate": result.get("win_rate", 0.0),
                "return_pct": result.get("return_pct", 0.0),
                "max_drawdown": result.get("max_drawdown", 0.0),
                "profit_factor": result.get("profit_factor", 0.0),
                "average_trade_pnl": result.get("average_trade_pnl", 0.0),
            })

        except Exception as e:
            results.append({
                "market": name,
                "symbol": symbol,
                "error": str(e),
            })

    ranked = sorted(
        results,
        key=lambda x: x.get("return_pct", -999),
        reverse=True
    )

    return {
        "risk_mode": risk_mode,
        "period": period,
        "interval": interval,
        "live_execution": False,
        "markets_tested": len(MARKETS),
        "results": ranked,
    }
@app.get("/backtest-all")
def run_backtest_all(
    risk_mode: str = "Balanced",
    period: str = "1mo",
    interval: str = "15m",
    starting_balance: float = 10000.0,
    payout: float = 0.80,
):
    if risk_mode not in PROFILES:
        raise HTTPException(
            status_code=400,
            detail="Invalid risk mode"
        )

    profile = PROFILES[risk_mode]
    results = []

    for name, symbol in MARKETS.items():
        try:
            sig = build(symbol, period, interval)

            result = backtest(
                sig,
                profile,
                starting_balance,
                payout
            )

            results.append({
                "market": name,
                "symbol": symbol,
                "trades": result.get("trades", 0),
                "wins": result.get("wins", 0),
                "losses": result.get("losses", 0),
                "win_rate": result.get("win_rate", 0.0),
                "return_pct": result.get("return_pct", 0.0),
                "max_drawdown": result.get("max_drawdown", 0.0),
                "profit_factor": result.get("profit_factor", 0.0),
                "average_trade_pnl": result.get("average_trade_pnl", 0.0),
            })

        except Exception as e:
            results.append({
                "market": name,
                "symbol": symbol,
                "error": str(e),
            })

    ranked = sorted(
        results,
        key=lambda x: x.get("return_pct", -999),
        reverse=True
    )

    return {
        "risk_mode": risk_mode,
        "period": period,
        "interval": interval,
        "live_execution": False,
        "markets_tested": len(MARKETS),
        "results": ranked,
    }


@app.get("/threshold-sweep")
def run_threshold_sweep(
    symbol: str = "EURUSD=X",
    risk_mode: str = "Balanced",
    period: str = "1mo",
    interval: str = "15m",
    starting_balance: float = 10000.0,
    payout: float = 0.80,
    holding_candles: int = 4,
):
    if risk_mode not in PROFILES:
        raise HTTPException(
            status_code=400,
            detail="Invalid risk mode"
        )

    raw = get_data(symbol, period, interval)

    ind = add_indicators(raw)
    model = train_model(ind)
    enriched = enrich(ind, model)

    result = threshold_sweep(
        enriched,
        PROFILES[risk_mode],
        starting_balance=starting_balance,
        payout=payout,
        holding_candles=holding_candles,
    )

    result.update({
        "symbol": symbol,
        "risk_mode": risk_mode,
        "period": period,
        "interval": interval,
        "holding_candles": holding_candles,
        "live_execution": False,
    })

    return result
@app.get("/strategy-optimize")
def run_strategy_optimize(
    symbol: str = "EURUSD=X",
    risk_mode: str = "Balanced",
    period: str = "1mo",
    interval: str = "15m",
    starting_balance: float = 10000.0,
    payout: float = 0.80,
):
    if risk_mode not in PROFILES:
        raise HTTPException(
            status_code=400,
            detail="Invalid risk mode"
        )

    raw = get_data(symbol, period, interval)

    ind = add_indicators(raw)
    model = train_model(ind)
    enriched = enrich(ind, model)

    result = optimise_strategy(
        enriched,
        PROFILES[risk_mode],
        starting_balance=starting_balance,
        payout=payout,
    )

    result.update({
        "symbol": symbol,
        "risk_mode": risk_mode,
        "period": period,
        "interval": interval,
        "live_execution": False,
    })

    return result
@app.get("/optimize-timeframes")
def run_optimize_timeframes(
    symbol: str = "EURUSD=X",
    risk_mode: str = "Balanced",
    starting_balance: float = 10000.0,
    payout: float = 0.80,
):
    if risk_mode not in PROFILES:
        raise HTTPException(
            status_code=400,
            detail="Invalid risk mode"
        )

    result = optimise_all_timeframes(
        symbol=symbol,
        get_data_func=get_data,
        add_indicators_func=add_indicators,
        train_model_func=train_model,
        enrich_func=enrich,
        profile=PROFILES[risk_mode],
        starting_balance=starting_balance,
        payout=payout,
    )

    result.update({
        "risk_mode": risk_mode,
        "live_execution": False,
    })

    return result
@app.get("/scan-market")
def run_scan_market(
    market: str = "EURUSD",
    risk_mode: str = "Balanced",
    starting_balance: float = 10000.0,
    payout: float = 0.80,
):
    if risk_mode not in PROFILES:
        raise HTTPException(
            status_code=400,
            detail="Invalid risk mode"
        )

    markets = {
        "EURUSD": "EURUSD=X",
        "GBPUSD": "GBPUSD=X",
        "USDJPY": "JPY=X",
        "AUDUSD": "AUDUSD=X",
        "NZDUSD": "NZDUSD=X",
        "USDCAD": "CAD=X",
        "USDCHF": "CHF=X",
        "EURJPY": "EURJPY=X",
        "GBPJPY": "GBPJPY=X",
    }

    market = market.upper()

    if market not in markets:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown market: {market}"
        )

    symbol = markets[market]

    result = optimise_all_timeframes(
        symbol=symbol,
        get_data_func=get_data,
        add_indicators_func=add_indicators,
        train_model_func=train_model,
        enrich_func=enrich,
        profile=PROFILES[risk_mode],
        starting_balance=starting_balance,
        payout=payout,
    )

    best = result.get("best")

    if best:
        trades = int(best.get("trades", 0))
        win_rate = float(best.get("win_rate", 0.0))
        profit_factor = float(best.get("profit_factor", 0.0))
        return_pct = float(best.get("return_pct", 0.0))
        max_drawdown = abs(
            float(best.get("max_drawdown", 0.0))
        )

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

        best["status"] = status
        best["market_score"] = market_score

    result.update({
        "market": market,
        "symbol": symbol,
        "risk_mode": risk_mode,
        "status": best.get("status") if best else "NO_DATA",
        "market_score": best.get("market_score", 0.0) if best else 0.0,
        "live_execution": False,
    })

    return result
