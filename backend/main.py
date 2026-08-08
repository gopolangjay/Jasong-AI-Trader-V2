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

def get_data(symbol: str, period="3mo", interval="15m"):
    try:
        d = yf.download(symbol, period=period, interval=interval,
                        progress=False, auto_adjust=True)
        if isinstance(d.columns, pd.MultiIndex):
            d.columns = d.columns.get_level_values(0)
        d = d.dropna(subset=["Open","High","Low","Close"])
        if len(d) < 100:
            raise ValueError("Not enough market data")
        return d
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Market data unavailable: {e}")

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
           period: str="3mo", interval: str="15m", balance: float=10000.0):
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
                 period: str="3mo", interval: str="15m",
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
