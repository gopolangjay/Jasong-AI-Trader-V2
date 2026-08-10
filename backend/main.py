
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import pandas as pd
import yfinance as yf

from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from deep_validator import validate_candidates
from fast_scanner import fast_scan_markets
from sequential_scanner import build_top_markets

from market_scanner import (
    classify_result,
    calculate_market_score,
)

from multi_optimizer import optimise_all_timeframes
from strategy_optimizer import optimise_strategy
from optimizer import threshold_sweep

from indicators import add_indicators

from engine import (
    PROFILES,
    train_model,
    enrich,
    decision,
)

from paper import (
    backtest,
    stake_for_balance,
)

from trade_watcher import TradeWatcherEngine

from database import (
    SessionLocal,
    Trade,
    init_db,
)


# ============================================================
# JASONG AI TRADER V5.4
# SMART RANKING + MULTI-CANDIDATE WATCH PORTFOLIO
# ============================================================

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


app = FastAPI(
    title="Jasong AI Trader V5.4 API",
    version="5.4.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

init_db()


# ============================================================
# DATABASE
# ============================================================

def get_db():
    db = SessionLocal()

    try:
        yield db

    finally:
        db.close()


# ============================================================
# CACHE
# ============================================================

@dataclass
class CacheEntry:
    dataframe: pd.DataFrame
    created_at: float
    source: str


_DATA_CACHE: Dict[
    Tuple[str, str, str],
    CacheEntry,
] = {}

_CACHE_LOCK = threading.Lock()
_DOWNLOAD_LOCK = threading.Lock()


# Fresh data for five minutes.
CACHE_TTL_SECONDS = 300

# Old data may be reused during Yahoo failure.
STALE_CACHE_MAX_AGE_SECONDS = 21600

# Space Yahoo requests apart.
MIN_DOWNLOAD_GAP_SECONDS = 2.5

# Stop hammering Yahoo after a rate-limit incident.
YAHOO_COOLDOWN_SECONDS = 180


_last_download_time = 0.0
_yahoo_cooldown_until = 0.0


# ============================================================
# GENERAL VALIDATION
# ============================================================

def validate_risk_mode(
    risk_mode: str,
):
    if risk_mode not in PROFILES:
        raise HTTPException(
            status_code=400,
            detail="Invalid risk mode",
        )


def validate_balance(
    amount: float,
    field_name: str = "starting_balance",
):
    if amount <= 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{field_name} must be "
                "greater than 0"
            ),
        )


# ============================================================
# CACHE HELPERS
# ============================================================

def _cache_key(
    symbol: str,
    period: str,
    interval: str,
):
    return (
        symbol.upper().strip(),
        period.lower().strip(),
        interval.lower().strip(),
    )


def _cache_put(
    symbol: str,
    period: str,
    interval: str,
    data: pd.DataFrame,
    source: str,
):
    key = _cache_key(
        symbol,
        period,
        interval,
    )

    with _CACHE_LOCK:
        _DATA_CACHE[key] = CacheEntry(
            dataframe=data.copy(),
            created_at=time.time(),
            source=source,
        )


def _cache_get(
    symbol: str,
    period: str,
    interval: str,
    allow_stale: bool = False,
) -> Optional[pd.DataFrame]:

    key = _cache_key(
        symbol,
        period,
        interval,
    )

    with _CACHE_LOCK:
        entry = _DATA_CACHE.get(
            key
        )

        if entry is None:
            return None

        age = (
            time.time()
            - entry.created_at
        )

        max_age = (
            STALE_CACHE_MAX_AGE_SECONDS
            if allow_stale
            else CACHE_TTL_SECONDS
        )

        if age > max_age:
            return None

        return entry.dataframe.copy()


def clear_market_cache():
    with _CACHE_LOCK:
        count = len(
            _DATA_CACHE
        )

        _DATA_CACHE.clear()

    return count


def cache_stats():
    now = time.time()

    with _CACHE_LOCK:
        items = []

        for (
            symbol,
            period,
            interval,
        ), entry in _DATA_CACHE.items():

            age = (
                now
                - entry.created_at
            )

            items.append({
                "symbol": symbol,
                "period": period,
                "interval": interval,
                "rows": len(
                    entry.dataframe
                ),
                "source": entry.source,
                "age_seconds": round(
                    age,
                    1,
                ),
                "fresh": (
                    age
                    <= CACHE_TTL_SECONDS
                ),
            })

    return {
        "entries": len(items),
        "fresh_ttl_seconds":
            CACHE_TTL_SECONDS,
        "stale_max_age_seconds":
            STALE_CACHE_MAX_AGE_SECONDS,
        "yahoo_cooldown_active":
            yahoo_cooldown_active(),
        "yahoo_cooldown_remaining":
            yahoo_cooldown_remaining(),
        "items": items,
    }


# ============================================================
# YAHOO COOLDOWN
# ============================================================

def yahoo_cooldown_active():
    return (
        time.time()
        < _yahoo_cooldown_until
    )


def yahoo_cooldown_remaining():
    remaining = (
        _yahoo_cooldown_until
        - time.time()
    )

    return max(
        0.0,
        round(
            remaining,
            1,
        ),
    )


def activate_yahoo_cooldown():
    global _yahoo_cooldown_until

    _yahoo_cooldown_until = (
        time.time()
        + YAHOO_COOLDOWN_SECONDS
    )


def _looks_like_rate_limit(
    exc: Exception,
):
    text = str(
        exc
    ).lower()

    markers = [
        "too many requests",
        "rate limit",
        "yfratelimiterror",
        "429",
    ]

    return any(
        marker in text
        for marker in markers
    )


# ============================================================
# DOWNLOAD THROTTLE
# ============================================================

def _wait_for_download_slot():
    global _last_download_time

    with _DOWNLOAD_LOCK:
        now = time.time()

        elapsed = (
            now
            - _last_download_time
        )

        if (
            elapsed
            < MIN_DOWNLOAD_GAP_SECONDS
        ):
            time.sleep(
                MIN_DOWNLOAD_GAP_SECONDS
                - elapsed
            )

        _last_download_time = (
            time.time()
        )


# ============================================================
# CLEAN PRICE DATA
# ============================================================

def _clean_price_data(
    frame: pd.DataFrame,
):
    if frame is None:
        return pd.DataFrame()

    data = frame.copy()

    if isinstance(
        data.columns,
        pd.MultiIndex,
    ):
        data.columns = (
            data.columns
            .get_level_values(0)
        )

    required = [
        "Open",
        "High",
        "Low",
        "Close",
    ]

    if not all(
        name in data.columns
        for name in required
    ):
        return pd.DataFrame()

    if "Volume" not in data.columns:
        data["Volume"] = 0.0

    data = data[
        [
            "Open",
            "High",
            "Low",
            "Close",
            "Volume",
        ]
    ].copy()

    data = data.dropna(
        subset=[
            "Open",
            "High",
            "Low",
            "Close",
        ]
    )

    data = data.sort_index()

    return data


# ============================================================
# SAFE YAHOO PERIODS
# ============================================================

def safe_native_period(
    period: str,
    interval: str,
):
    period = str(
        period or "1mo"
    ).lower().strip()

    interval = str(
        interval or "15m"
    ).lower().strip()

    if interval == "1m":
        if period not in {
            "1d",
            "5d",
            "7d",
        }:
            return "5d"

    if interval in {
        "2m",
        "5m",
        "15m",
        "30m",
    }:
        if period not in {
            "1d",
            "5d",
            "1mo",
        }:
            return "1mo"

    return period


# ============================================================
# NATIVE YAHOO DOWNLOAD
# ============================================================

def _download_native(
    symbol: str,
    period: str,
    interval: str,
):
    period = safe_native_period(
        period,
        interval,
    )

    fresh = _cache_get(
        symbol,
        period,
        interval,
    )

    if (
        fresh is not None
        and len(fresh) >= 80
    ):
        return fresh

    if yahoo_cooldown_active():
        stale = _cache_get(
            symbol,
            period,
            interval,
            allow_stale=True,
        )

        if (
            stale is not None
            and len(stale) >= 80
        ):
            return stale

        raise HTTPException(
            status_code=503,
            detail=(
                "Yahoo market data is "
                "temporarily cooling down "
                f"({yahoo_cooldown_remaining()}s)"
            ),
        )

    _wait_for_download_slot()

    try:
        raw = yf.download(
            symbol,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=True,
            threads=False,
        )

        data = _clean_price_data(
            raw
        )

        if data.empty:
            raise ValueError(
                "Yahoo returned no usable data"
            )

        _cache_put(
            symbol,
            period,
            interval,
            data,
            source="YAHOO",
        )

        return data.copy()

    except Exception as exc:
        if _looks_like_rate_limit(
            exc
        ):
            activate_yahoo_cooldown()

        stale = _cache_get(
            symbol,
            period,
            interval,
            allow_stale=True,
        )

        if (
            stale is not None
            and len(stale) >= 80
        ):
            return stale

        raise HTTPException(
            status_code=502,
            detail=(
                "Market data unavailable: "
                f"{symbol} {period} {interval}: "
                f"{exc}"
            ),
        )


# ============================================================
# LOCAL RESAMPLING
# ============================================================

def _resample_ohlcv(
    base: pd.DataFrame,
    rule: str,
):
    data = base.resample(
        rule,
        label="right",
        closed="right",
    ).agg({
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    })

    data = data.dropna(
        subset=[
            "Open",
            "High",
            "Low",
            "Close",
        ]
    )

    return data


def _derived_data(
    symbol: str,
    period: str,
    interval: str,
):
    """
    Generate common higher timeframes locally.

    30m is derived from native 15m.

    1h for short histories can also be derived from 15m.

    Longer hourly history is downloaded natively because
    Yahoo does not provide enough 15m history to truthfully
    create a multi-month 1h backtest.
    """

    interval = interval.lower()

    existing = _cache_get(
        symbol,
        period,
        interval,
    )

    if (
        existing is not None
        and len(existing) >= 80
    ):
        return existing

    # --------------------------------------------------------
    # 30 MINUTES FROM 15 MINUTES
    # --------------------------------------------------------

    if interval == "30m":
        base_period = safe_native_period(
            period,
            "15m",
        )

        base = _download_native(
            symbol,
            base_period,
            "15m",
        )

        derived = _resample_ohlcv(
            base,
            "30min",
        )

        if len(derived) >= 80:
            _cache_put(
                symbol,
                period,
                "30m",
                derived,
                source="RESAMPLED_15M",
            )

            return derived.copy()

    # --------------------------------------------------------
    # SHORT 1H HISTORY FROM 15M
    # --------------------------------------------------------

    if (
        interval in {
            "60m",
            "1h",
        }
        and period in {
            "1d",
            "5d",
            "1mo",
        }
    ):
        base = _download_native(
            symbol,
            safe_native_period(
                period,
                "15m",
            ),
            "15m",
        )

        derived = _resample_ohlcv(
            base,
            "1h",
        )

        if len(derived) >= 80:
            _cache_put(
                symbol,
                period,
                interval,
                derived,
                source="RESAMPLED_15M",
            )

            return derived.copy()

    # --------------------------------------------------------
    # 2H / 4H FROM NATIVE 1H
    # --------------------------------------------------------

    if interval in {
        "2h",
        "4h",
    }:
        base = _download_native(
            symbol,
            period,
            "1h",
        )

        rule = (
            "2h"
            if interval == "2h"
            else "4h"
        )

        derived = _resample_ohlcv(
            base,
            rule,
        )

        if len(derived) >= 80:
            _cache_put(
                symbol,
                period,
                interval,
                derived,
                source="RESAMPLED_1H",
            )

            return derived.copy()

    return None


# ============================================================
# PUBLIC DATA ACCESS
# ============================================================

def get_data(
    symbol: str,
    period: str = "1mo",
    interval: str = "15m",
):
    symbol = str(
        symbol
    ).strip()

    period = str(
        period or "1mo"
    ).lower().strip()

    interval = str(
        interval or "15m"
    ).lower().strip()

    if not symbol:
        raise HTTPException(
            status_code=400,
            detail="Market symbol required",
        )

    # --------------------------------------------------------
    # EXACT CACHE
    # --------------------------------------------------------

    cached = _cache_get(
        symbol,
        period,
        interval,
    )

    if (
        cached is not None
        and len(cached) >= 80
    ):
        return cached

    # --------------------------------------------------------
    # TRY LOCAL DERIVATION FIRST
    # --------------------------------------------------------

    derived = _derived_data(
        symbol,
        period,
        interval,
    )

    if (
        derived is not None
        and len(derived) >= 80
    ):
        return derived

    # --------------------------------------------------------
    # OTHERWISE USE NATIVE YAHOO
    # --------------------------------------------------------

    return _download_native(
        symbol,
        period,
        interval,
    )


# ============================================================
# AI BUILD PIPELINE
# ============================================================

def build(
    symbol: str,
    period: str,
    interval: str,
):
    raw = get_data(
        symbol,
        period,
        interval,
    )

    indicators = add_indicators(
        raw
    )

    model = train_model(
        indicators
    )

    return enrich(
        indicators,
        model,
    )


# ============================================================
# ROOT
# ============================================================

@app.get("/")
def root():
    return {
        "name":
            "Jasong AI Trader V5.3",

        "version":
            "5.3.0",

        "mode":
            "paper-trading",

        "live_execution":
            False,
    }


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": "5.4.0",
        "cache_entries":
            len(_DATA_CACHE),
        "yahoo_cooldown_active":
            yahoo_cooldown_active(),
        "yahoo_cooldown_remaining":
            yahoo_cooldown_remaining(),
        "live_execution":
            False,
    }


# ============================================================
# DATA CACHE
# ============================================================

@app.get("/data-cache")
def data_cache():
    return {
        "status": "ok",
        **cache_stats(),
    }


@app.post("/data-cache/clear")
def clear_data_cache():
    removed = clear_market_cache()

    return {
        "status": "cleared",
        "entries_removed": removed,
    }


# ============================================================
# SIGNAL
# ============================================================

@app.get("/signal")
def signal(
    symbol: str = "EURUSD=X",
    risk_mode: str = "Balanced",
    period: str = "1mo",
    interval: str = "15m",
    balance: float = 10000.0,
):
    validate_risk_mode(
        risk_mode
    )

    validate_balance(
        balance,
        "balance",
    )

    profile = PROFILES[
        risk_mode
    ]

    signal_data = build(
        symbol,
        period,
        interval,
    )

    result = decision(
        signal_data,
        profile,
    )

    result.update({
        "symbol": symbol,
        "risk_mode": risk_mode,
        "suggested_paper_stake":
            stake_for_balance(
                balance,
                profile.risk_per_trade,
            ),
        "live_execution":
            False,
    })

    return result


# ============================================================
# BACKTEST
# ============================================================

@app.get("/backtest")
def run_backtest(
    symbol: str = "EURUSD=X",
    risk_mode: str = "Balanced",
    period: str = "1mo",
    interval: str = "15m",
    starting_balance: float = 10000.0,
    payout: float = 0.80,
):
    validate_risk_mode(
        risk_mode
    )

    validate_balance(
        starting_balance
    )

    data = build(
        symbol,
        period,
        interval,
    )

    result = backtest(
        data,
        PROFILES[risk_mode],
        starting_balance,
        payout,
    )

    result.update({
        "symbol": symbol,
        "risk_mode": risk_mode,
        "live_execution": False,
    })

    return result


# ============================================================
# PAPER TRADES
# ============================================================

@app.post("/paper-trades")
def create_paper_trade(
    symbol: str,
    direction: str,
    confidence: float,
    entry_price: float,
    stake: float,
    db: Session = Depends(get_db),
):
    if direction not in {
        "BUY",
        "SELL",
    }:
        raise HTTPException(
            status_code=400,
            detail=(
                "Direction must be "
                "BUY or SELL"
            ),
        )

    if stake <= 0:
        raise HTTPException(
            status_code=400,
            detail=(
                "Stake must be "
                "greater than 0"
            ),
        )

    trade = Trade(
        symbol=symbol,
        direction=direction,
        confidence=confidence,
        entry_price=entry_price,
        stake=stake,
        mode="paper",
    )

    db.add(
        trade
    )

    db.commit()

    db.refresh(
        trade
    )

    return {
        "id": trade.id,
        "status": "recorded",
        "live_execution": False,
    }


@app.get("/paper-trades")
def list_paper_trades(
    db: Session = Depends(get_db),
):
    rows = (
        db.query(Trade)
        .order_by(
            Trade.created_at.desc()
        )
        .limit(200)
        .all()
    )

    return [
        {
            "id": row.id,
            "created_at":
                row.created_at.isoformat(),
            "symbol": row.symbol,
            "direction": row.direction,
            "confidence": row.confidence,
            "entry_price": row.entry_price,
            "stake": row.stake,
            "result": row.result,
            "pnl": row.pnl,
            "closed": row.closed,
        }
        for row in rows
    ]


# ============================================================
# BACKTEST ALL
# ============================================================

@app.get("/backtest-all")
def run_backtest_all(
    risk_mode: str = "Balanced",
    period: str = "1mo",
    interval: str = "15m",
    starting_balance: float = 10000.0,
    payout: float = 0.80,
):
    validate_risk_mode(
        risk_mode
    )

    validate_balance(
        starting_balance
    )

    profile = PROFILES[
        risk_mode
    ]

    results = []

    for (
        market_name,
        symbol,
    ) in MARKETS.items():

        try:
            data = build(
                symbol,
                period,
                interval,
            )

            tested = backtest(
                data,
                profile,
                starting_balance,
                payout,
            )

            results.append({
                "market":
                    market_name,
                "symbol":
                    symbol,
                "trades":
                    tested.get(
                        "trades",
                        0,
                    ),
                "wins":
                    tested.get(
                        "wins",
                        0,
                    ),
                "losses":
                    tested.get(
                        "losses",
                        0,
                    ),
                "win_rate":
                    tested.get(
                        "win_rate",
                        0.0,
                    ),
                "return_pct":
                    tested.get(
                        "return_pct",
                        0.0,
                    ),
                "max_drawdown":
                    tested.get(
                        "max_drawdown",
                        0.0,
                    ),
                "profit_factor":
                    tested.get(
                        "profit_factor",
                        0.0,
                    ),
                "average_trade_pnl":
                    tested.get(
                        "average_trade_pnl",
                        0.0,
                    ),
            })

        except Exception as exc:
            results.append({
                "market":
                    market_name,
                "symbol":
                    symbol,
                "error":
                    str(exc),
            })

    results = sorted(
        results,
        key=lambda item:
            item.get(
                "return_pct",
                -999,
            ),
        reverse=True,
    )

    return {
        "risk_mode":
            risk_mode,
        "period":
            period,
        "interval":
            interval,
        "markets_tested":
            len(MARKETS),
        "results":
            results,
        "live_execution":
            False,
    }


# ============================================================
# THRESHOLD SWEEP
# ============================================================

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
    validate_risk_mode(
        risk_mode
    )

    validate_balance(
        starting_balance
    )

    raw = get_data(
        symbol,
        period,
        interval,
    )

    indicators = add_indicators(
        raw
    )

    model = train_model(
        indicators
    )

    enriched = enrich(
        indicators,
        model,
    )

    result = threshold_sweep(
        enriched,
        PROFILES[risk_mode],
        starting_balance=
            starting_balance,
        payout=payout,
        holding_candles=
            holding_candles,
    )

    result.update({
        "symbol": symbol,
        "risk_mode": risk_mode,
        "period": period,
        "interval": interval,
        "holding_candles":
            holding_candles,
        "live_execution": False,
    })

    return result


# ============================================================
# STRATEGY OPTIMIZER
# ============================================================

@app.get("/strategy-optimize")
def run_strategy_optimize(
    symbol: str = "EURUSD=X",
    risk_mode: str = "Balanced",
    period: str = "1mo",
    interval: str = "15m",
    starting_balance: float = 10000.0,
    payout: float = 0.80,
):
    validate_risk_mode(
        risk_mode
    )

    validate_balance(
        starting_balance
    )

    raw = get_data(
        symbol,
        period,
        interval,
    )

    indicators = add_indicators(
        raw
    )

    model = train_model(
        indicators
    )

    enriched = enrich(
        indicators,
        model,
    )

    result = optimise_strategy(
        enriched,
        PROFILES[risk_mode],
        starting_balance=
            starting_balance,
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


# ============================================================
# OPTIMIZE ALL TIMEFRAMES
# ============================================================

@app.get("/optimize-timeframes")
def run_optimize_timeframes(
    symbol: str = "EURUSD=X",
    risk_mode: str = "Balanced",
    starting_balance: float = 10000.0,
    payout: float = 0.80,
):
    validate_risk_mode(
        risk_mode
    )

    validate_balance(
        starting_balance
    )

    result = optimise_all_timeframes(
        symbol=symbol,
        get_data_func=get_data,
        add_indicators_func=
            add_indicators,
        train_model_func=
            train_model,
        enrich_func=enrich,
        profile=
            PROFILES[risk_mode],
        starting_balance=
            starting_balance,
        payout=payout,
    )

    result.update({
        "risk_mode": risk_mode,
        "live_execution": False,
    })

    return result


# ============================================================
# SCAN ONE MARKET
# ============================================================

@app.get("/scan-market")
def run_scan_market(
    market: str = "EURUSD",
    risk_mode: str = "Balanced",
    starting_balance: float = 10000.0,
    payout: float = 0.80,
):
    validate_risk_mode(
        risk_mode
    )

    validate_balance(
        starting_balance
    )

    market = market.upper()

    if market not in MARKETS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown market: "
                f"{market}"
            ),
        )

    symbol = MARKETS[
        market
    ]

    result = optimise_all_timeframes(
        symbol=symbol,
        get_data_func=get_data,
        add_indicators_func=
            add_indicators,
        train_model_func=
            train_model,
        enrich_func=enrich,
        profile=
            PROFILES[risk_mode],
        starting_balance=
            starting_balance,
        payout=payout,
    )

    best = result.get(
        "best"
    )

    if best:
        trades = int(
            best.get(
                "trades",
                0,
            )
        )

        win_rate = float(
            best.get(
                "win_rate",
                0.0,
            )
        )

        profit_factor = float(
            best.get(
                "profit_factor",
                0.0,
            )
        )

        return_pct = float(
            best.get(
                "return_pct",
                0.0,
            )
        )

        max_drawdown = abs(
            float(
                best.get(
                    "max_drawdown",
                    0.0,
                )
            )
        )

        best["status"] = (
            classify_result(
                trades,
                win_rate,
                profit_factor,
                return_pct,
                max_drawdown,
            )
        )

        best["market_score"] = (
            calculate_market_score(
                trades,
                win_rate,
                profit_factor,
                return_pct,
                max_drawdown,
            )
        )

    result.update({
        "market": market,
        "symbol": symbol,
        "risk_mode": risk_mode,
        "status": (
            best.get(
                "status"
            )
            if best
            else "NO_DATA"
        ),
        "market_score": (
            best.get(
                "market_score",
                0.0,
            )
            if best
            else 0.0
        ),
        "live_execution": False,
    })

    return result


# ============================================================
# RANK MARKETS
# ============================================================

@app.post("/rank-markets")
def rank_markets(
    results: list = Body(...),
    top_n: int = 3,
):
    if not isinstance(
        results,
        list,
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "results must be a list"
            ),
        )

    if (
        top_n < 1
        or top_n > 9
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "top_n must be between "
                "1 and 9"
            ),
        )

    return build_top_markets(
        results=results,
        top_n=top_n,
    )


# ============================================================
# V5.4 SMART FAST-SCORE LAYER
# ============================================================

def _v54_quality_tier(score: float) -> str:
    if score >= 90.0:
        return "A+"
    if score >= 82.0:
        return "A"
    if score >= 72.0:
        return "B"
    if score >= 62.0:
        return "C"
    return "REJECT"


def _v54_rescore_candidate(
    candidate: dict,
) -> dict:
    """Make Fast Score more discriminating.

    The original scanner remains the discovery engine. V5.4 applies
    transparent penalties for obvious entry-quality contradictions so a
    saturated 100/100 does not look like a 100% win probability.

    This score is still a RANKING score only.
    """

    item = dict(candidate)

    raw_score = float(
        item.get(
            "raw_fast_score",
            item.get("fast_score", 0.0),
        )
        or 0.0
    )

    direction = str(
        item.get("direction", "WAIT")
        or "WAIT"
    ).upper()

    try:
        rsi = float(
            item.get("rsi", 50.0)
            or 50.0
        )
    except (TypeError, ValueError):
        rsi = 50.0

    reasons = [
        str(reason)
        for reason in (
            item.get("reasons")
            or []
        )
    ]

    reason_text = " | ".join(
        reasons
    ).lower()

    penalties = []
    bonuses = []

    # --------------------------------------------------------
    # RSI extension penalties
    # --------------------------------------------------------

    if direction == "BUY":
        if rsi >= 80.0:
            penalties.append(
                ("RSI_EXTREME", 24.0)
            )
        elif rsi >= 75.0:
            penalties.append(
                ("RSI_OVEREXTENDED", 18.0)
            )
        elif rsi >= 70.0:
            penalties.append(
                ("RSI_ELEVATED", 10.0)
            )
        elif 52.0 <= rsi <= 66.0:
            bonuses.append(
                ("RSI_HEALTHY_BUY_ZONE", 2.0)
            )

    elif direction == "SELL":
        if rsi <= 20.0:
            penalties.append(
                ("RSI_EXTREME", 24.0)
            )
        elif rsi <= 25.0:
            penalties.append(
                ("RSI_OVEREXTENDED", 18.0)
            )
        elif rsi <= 30.0:
            penalties.append(
                ("RSI_DEPRESSED", 10.0)
            )
        elif 34.0 <= rsi <= 48.0:
            bonuses.append(
                ("RSI_HEALTHY_SELL_ZONE", 2.0)
            )

    # --------------------------------------------------------
    # Indicator contradiction penalties
    # --------------------------------------------------------

    if (
        direction == "BUY"
        and "macd bearish"
        in reason_text
    ):
        penalties.append(
            ("MACD_CONTRADICTS_BUY", 12.0)
        )

    if (
        direction == "SELL"
        and "macd bullish"
        in reason_text
    ):
        penalties.append(
            ("MACD_CONTRADICTS_SELL", 12.0)
        )

    if (
        direction == "BUY"
        and "bearish ema"
        in reason_text
    ):
        penalties.append(
            ("EMA_CONTRADICTS_BUY", 18.0)
        )

    if (
        direction == "SELL"
        and "bullish ema"
        in reason_text
    ):
        penalties.append(
            ("EMA_CONTRADICTS_SELL", 18.0)
        )

    if "positive momentum" in reason_text:
        if direction == "BUY":
            bonuses.append(
                ("POSITIVE_MOMENTUM", 2.0)
            )

    if "negative momentum" in reason_text:
        if direction == "SELL":
            bonuses.append(
                ("NEGATIVE_MOMENTUM", 2.0)
            )

    penalty_total = sum(
        amount
        for _name, amount
        in penalties
    )

    bonus_total = sum(
        amount
        for _name, amount
        in bonuses
    )

    smart_score = max(
        0.0,
        min(
            100.0,
            raw_score
            - penalty_total
            + bonus_total,
        ),
    )

    item["raw_fast_score"] = round(
        raw_score,
        2,
    )
    item["fast_score"] = round(
        smart_score,
        2,
    )
    item["smart_fast_score"] = round(
        smart_score,
        2,
    )
    item["quality_tier"] = (
        _v54_quality_tier(
            smart_score
        )
    )
    item["score_penalties"] = [
        {
            "code": name,
            "points": amount,
        }
        for name, amount
        in penalties
    ]
    item["score_bonuses"] = [
        {
            "code": name,
            "points": amount,
        }
        for name, amount
        in bonuses
    ]
    item["ranking_note"] = (
        "V5.4 Fast Score is a discovery ranking score, "
        "not a probability of winning."
    )

    return item


def _v54_rescore_fast_scan(
    result: dict,
    top_n: int,
) -> dict:
    """Re-rank scanner output without changing the discovery engine."""

    output = dict(result)

    raw_ranking = (
        result.get("ranking")
        or []
    )

    rescored = []

    for raw in raw_ranking:
        if isinstance(raw, dict):
            rescored.append(
                _v54_rescore_candidate(
                    raw
                )
            )

    rescored.sort(
        key=lambda item: (
            float(
                item.get(
                    "fast_score",
                    0.0,
                )
                or 0.0
            ),
            float(
                item.get(
                    "raw_fast_score",
                    0.0,
                )
                or 0.0
            ),
        ),
        reverse=True,
    )

    if rescored:
        output["ranking"] = rescored
        output["top_candidates"] = (
            rescored[:top_n]
        )
        output["best_candidate"] = (
            rescored[0]
        )
        output["candidates_found"] = len(
            [
                item
                for item in rescored
                if item.get(
                    "quality_tier"
                )
                != "REJECT"
            ]
        )

    output["scanner"] = (
        "V5.4_SMART_FAST_SCAN"
    )
    output["ranking_version"] = (
        "V5.4"
    )
    output["fast_score_is_probability"] = (
        False
    )

    return output


# ============================================================
# FAST SCAN
# ============================================================

@app.get("/fast-scan")
def run_fast_scan(
    period: str = "5d",
    interval: str = "15m",
    top_n: int = 3,
):
    if (
        top_n < 1
        or top_n > len(MARKETS)
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"top_n must be between "
                f"1 and {len(MARKETS)}"
            ),
        )

    raw_result = fast_scan_markets(
        markets=MARKETS,
        get_data_func=get_data,
        add_indicators_func=
            add_indicators,
        period=period,
        interval=interval,
        top_n=top_n,
    )

    return _v54_rescore_fast_scan(
        result=raw_result,
        top_n=top_n,
    )


# ============================================================
# DEEP VALIDATE
# ONE CANDIDATE PER REQUEST
# ============================================================

@app.post("/deep-validate")
def run_deep_validate(
    candidates: list[dict],
    risk_mode: str = "Balanced",
    starting_balance: float = 10000.0,
    payout: float = 0.80,
    max_candidates: int = 1,
):
    validate_risk_mode(
        risk_mode
    )

    validate_balance(
        starting_balance
    )

    if not candidates:
        raise HTTPException(
            status_code=400,
            detail=(
                "No candidates supplied"
            ),
        )

    candidate = candidates[
        0
    ]

    result = validate_candidates(
        candidates=[
            candidate
        ],
        optimise_all_timeframes_func=
            optimise_all_timeframes,
        get_data_func=get_data,
        add_indicators_func=
            add_indicators,
        train_model_func=
            train_model,
        enrich_func=enrich,
        profile=
            PROFILES[risk_mode],
        starting_balance=
            starting_balance,
        payout=payout,
        max_candidates=1,
    )

    result.update({
        "requested_candidates":
            len(candidates),
        "candidates_processed_this_request":
            1,
        "validation_mode":
            "V4.6_SINGLE_CANDIDATE",
        "live_execution":
            False,
    })

    return result
# ============================================================
# V4.8 ASYNC DEEP VALIDATION JOBS
# ============================================================

import uuid

from fastapi import BackgroundTasks


_DEEP_VALIDATION_JOBS = {}

_DEEP_JOB_LOCK = threading.Lock()

DEEP_JOB_MAX_AGE_SECONDS = 3600


def _clean_old_deep_jobs():
    now = time.time()

    with _DEEP_JOB_LOCK:
        old_ids = []

        for job_id, job in _DEEP_VALIDATION_JOBS.items():
            created_at = job.get(
                "created_at",
                now,
            )

            if (
                now - created_at
                > DEEP_JOB_MAX_AGE_SECONDS
            ):
                old_ids.append(
                    job_id
                )

        for job_id in old_ids:
            _DEEP_VALIDATION_JOBS.pop(
                job_id,
                None,
            )


def _update_deep_job(
    job_id,
    **values,
):
    with _DEEP_JOB_LOCK:
        job = _DEEP_VALIDATION_JOBS.get(
            job_id
        )

        if job is None:
            return

        job.update(
            values
        )


def _run_deep_validation_job(
    job_id,
    candidate,
    risk_mode,
    starting_balance,
    payout,
):
    """
    Heavy deep validation runs here AFTER
    the mobile request has already received
    the job_id.
    """

    _update_deep_job(
        job_id,
        status="RUNNING",
        started_at=time.time(),
    )

    try:
        result = validate_candidates(
            candidates=[
                candidate
            ],

            optimise_all_timeframes_func=
                optimise_all_timeframes,

            get_data_func=
                get_data,

            add_indicators_func=
                add_indicators,

            train_model_func=
                train_model,

            enrich_func=
                enrich,

            profile=
                PROFILES[
                    risk_mode
                ],

            starting_balance=
                starting_balance,

            payout=
                payout,

            max_candidates=
                1,
        )

        _update_deep_job(
            job_id,
            status="COMPLETED",
            completed_at=time.time(),
            result=result,
            error=None,
        )

    except Exception as exc:
        _update_deep_job(
            job_id,
            status="FAILED",
            completed_at=time.time(),
            result=None,
            error=str(exc),
        )


@app.post("/deep-validation-job")
def create_deep_validation_job(
    candidate: dict,
    background_tasks: BackgroundTasks,

    risk_mode: str =
        "Balanced",

    starting_balance: float =
        10000.0,

    payout: float =
        0.80,
):
    validate_risk_mode(
        risk_mode
    )

    validate_balance(
        starting_balance
    )

    if not candidate:
        raise HTTPException(
            status_code=400,
            detail=(
                "Candidate is required"
            ),
        )

    market = candidate.get(
        "market"
    )

    symbol = candidate.get(
        "symbol"
    )

    if not market or not symbol:
        raise HTTPException(
            status_code=400,
            detail=(
                "Candidate must contain "
                "market and symbol"
            ),
        )

    _clean_old_deep_jobs()

    job_id = str(
        uuid.uuid4()
    )

    with _DEEP_JOB_LOCK:
        _DEEP_VALIDATION_JOBS[
            job_id
        ] = {
            "job_id":
                job_id,

            "status":
                "QUEUED",

            "market":
                market,

            "symbol":
                symbol,

            "candidate":
                candidate,

            "risk_mode":
                risk_mode,

            "created_at":
                time.time(),

            "started_at":
                None,

            "completed_at":
                None,

            "result":
                None,

            "error":
                None,
        }

    background_tasks.add_task(
        _run_deep_validation_job,
        job_id,
        candidate,
        risk_mode,
        starting_balance,
        payout,
    )

    return {
        "job_id":
            job_id,

        "status":
            "QUEUED",

        "market":
            market,

        "symbol":
            symbol,

        "message":
            "Deep validation job created",

        "live_execution":
            False,
    }


@app.get("/deep-validation-job/{job_id}")
def get_deep_validation_job(
    job_id: str,
):
    with _DEEP_JOB_LOCK:
        job = _DEEP_VALIDATION_JOBS.get(
            job_id
        )

        if job is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    "Deep validation job "
                    "not found"
                ),
            )

        response = dict(
            job
        )

    # Do not unnecessarily return the
    # original input object every poll.
    response.pop(
        "candidate",
        None,
    )

    response[
        "live_execution"
    ] = False

    return response



# ============================================================
# V5.3 VERIFIED WATCHER + FORWARD PAPER ENGINE
# ============================================================


def _v53_live_signal(
    symbol: str,
    risk_mode: str,
    balance: float,
):
    """Internal live signal callback used by the watcher engine."""

    profile = PROFILES[
        risk_mode
    ]

    signal_data = build(
        symbol,
        "1mo",
        "15m",
    )

    result = decision(
        signal_data,
        profile,
    )

    result.update({
        "symbol": symbol,
        "risk_mode": risk_mode,
        "suggested_paper_stake":
            stake_for_balance(
                balance,
                profile.risk_per_trade,
            ),
        "live_execution": False,
    })

    return result


def _v53_latest_price(
    symbol: str,
) -> float:
    data = get_data(
        symbol,
        "5d",
        "15m",
    )

    if data is None or data.empty:
        raise ValueError(
            f"No latest price available for {symbol}"
        )

    return float(
        data["Close"].iloc[-1]
    )


V53_WATCHER_ENGINE = TradeWatcherEngine(
    session_factory=SessionLocal,
    trade_model=Trade,
    signal_func=_v53_live_signal,
    price_func=_v53_latest_price,
    profiles=PROFILES,
)

V53_WATCHER_ENGINE.start()


@app.post("/watchers")
def create_verified_watcher(
    candidate: dict = Body(...),
    risk_mode: str = "Balanced",
    starting_balance: float = 10000.0,
    payout: float = 0.80,
):
    """Create a server-side watcher for one VERIFIED historical setup."""

    validate_risk_mode(
        risk_mode
    )

    validate_balance(
        starting_balance
    )

    try:
        watcher = V53_WATCHER_ENGINE.create(
            candidate=candidate,
            risk_mode=risk_mode,
            starting_balance=starting_balance,
            payout=payout,
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    return {
        "status": "WATCHER_CREATED",
        "watcher": watcher,
        "live_execution": False,
    }


@app.get("/watchers")
def list_verified_watchers():
    watchers = (
        V53_WATCHER_ENGINE.list()
    )

    active_statuses = {
        "WATCHING",
        "READY",
        "RISK_BLOCKED",
        "OPEN",
    }

    active = [
        item
        for item in watchers
        if item.get("status")
        in active_statuses
    ]

    return {
        "watchers": watchers,
        "active_watchers": len(active),
        "portfolio_mode":
            "V5.4_MULTI_CANDIDATE",
        "max_open_trades":
            V53_WATCHER_ENGINE.MAX_OPEN_TRADES,
        "live_execution": False,
    }


@app.get("/watchers/{watcher_id}")
def get_verified_watcher(
    watcher_id: str,
):
    watcher = V53_WATCHER_ENGINE.get(
        watcher_id
    )

    if watcher is None:
        raise HTTPException(
            status_code=404,
            detail="Watcher not found",
        )

    return {
        "watcher": watcher,
        "live_execution": False,
    }


@app.post("/watchers/{watcher_id}/check")
def check_verified_watcher_now(
    watcher_id: str,
):
    watcher = V53_WATCHER_ENGINE.check_now(
        watcher_id
    )

    if watcher is None:
        raise HTTPException(
            status_code=404,
            detail="Watcher not found",
        )

    return {
        "watcher": watcher,
        "live_execution": False,
    }


@app.get("/forward-stats")
def get_forward_stats(
    starting_balance: float = 10000.0,
):
    validate_balance(
        starting_balance
    )

    return V53_WATCHER_ENGINE.forward_stats(
        starting_balance=starting_balance
    )
