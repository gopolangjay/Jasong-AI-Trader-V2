import random
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

from database import (
    SessionLocal,
    Trade,
    init_db,
)


# ============================================================
# JASONG AI TRADER V4.4
# ============================================================
#
# Main improvements:
#
# 1. Fresh market-data cache
# 2. Stale-cache fallback
# 3. Yahoo rate-limit cooldown
# 4. Download throttling
# 5. Safe intraday periods
# 6. Reduced repeated Yahoo requests
# 7. One deep-validation candidate per request
# 8. Zero-balance protection
# 9. Cache diagnostics
# 10. No duplicate /backtest-all route
#
# Paper trading / research only.
# ============================================================


# ============================================================
# MARKETS
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


# ============================================================
# FASTAPI APP
# ============================================================

app = FastAPI(
    title="Jasong AI Trader V4.4 API",
    version="4.4.0",
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
# MARKET-DATA CACHE
# ============================================================

@dataclass
class CacheEntry:
    dataframe: pd.DataFrame
    created_at: float


_DATA_CACHE: Dict[
    Tuple[str, str, str],
    CacheEntry,
] = {}


_CACHE_LOCK = threading.Lock()

_DOWNLOAD_LOCK = threading.Lock()


# Fresh data is preferred for 5 minutes.
CACHE_TTL_SECONDS = 300


# If Yahoo temporarily fails, data up to 6 hours old
# may be used as a defensive fallback.
STALE_CACHE_MAX_AGE_SECONDS = 21600


# Keep Yahoo downloads spaced apart.
MIN_DOWNLOAD_GAP_SECONDS = 2.0


# When Yahoo reports rate limiting, stop immediately
# hammering it for a while.
YAHOO_RATE_LIMIT_COOLDOWN_SECONDS = 180


_last_download_time = 0.0

_yahoo_cooldown_until = 0.0


# ============================================================
# BASIC VALIDATION
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
    value: float,
    field_name: str = "starting_balance",
):

    if value <= 0:

        raise HTTPException(
            status_code=400,
            detail=(
                f"{field_name} must be "
                "greater than 0"
            ),
        )


# ============================================================
# SAFE PERIOD HANDLING
# ============================================================

def safe_period(
    period: str,
    interval: str,
) -> str:

    period = str(
        period or "1mo"
    ).lower().strip()

    interval = str(
        interval or "15m"
    ).lower().strip()


    # Yahoo 1-minute candles have tight restrictions.
    if interval == "1m":

        if period not in {
            "1d",
            "5d",
            "7d",
        }:
            return "5d"

        return period


    # Other intraday intervals.
    if interval in {
        "2m",
        "5m",
        "15m",
        "30m",
        "60m",
        "90m",
        "1h",
    }:

        if period not in {
            "1d",
            "5d",
            "1mo",
        }:
            return "1mo"

        return period


    return period


def fallback_periods(
    period: str,
    interval: str,
) -> list[str]:

    interval = str(
        interval
    ).lower().strip()

    requested = safe_period(
        period,
        interval,
    )


    if interval == "1m":

        candidates = [
            requested,
            "5d",
            "1d",
        ]


    elif interval in {
        "2m",
        "5m",
        "15m",
        "30m",
        "60m",
        "90m",
        "1h",
    }:

        candidates = [
            requested,
            "1mo",
            "5d",
        ]


    else:

        candidates = [
            requested,
            "3mo",
            "1mo",
        ]


    unique = []

    for item in candidates:

        if item not in unique:
            unique.append(item)


    return unique


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


def _get_cache_entry(
    symbol: str,
    period: str,
    interval: str,
) -> Optional[CacheEntry]:

    key = _cache_key(
        symbol,
        period,
        interval,
    )

    with _CACHE_LOCK:

        entry = _DATA_CACHE.get(
            key
        )

        return entry


def _get_fresh_cache(
    symbol: str,
    period: str,
    interval: str,
) -> Optional[pd.DataFrame]:

    entry = _get_cache_entry(
        symbol,
        period,
        interval,
    )

    if entry is None:
        return None


    age = (
        time.time()
        - entry.created_at
    )


    if age > CACHE_TTL_SECONDS:
        return None


    return entry.dataframe.copy()


def _get_stale_cache(
    symbol: str,
    period: str,
    interval: str,
) -> Optional[pd.DataFrame]:

    entry = _get_cache_entry(
        symbol,
        period,
        interval,
    )

    if entry is None:
        return None


    age = (
        time.time()
        - entry.created_at
    )


    if age > STALE_CACHE_MAX_AGE_SECONDS:
        return None


    return entry.dataframe.copy()


def _store_cache(
    symbol: str,
    period: str,
    interval: str,
    dataframe: pd.DataFrame,
):

    key = _cache_key(
        symbol,
        period,
        interval,
    )

    with _CACHE_LOCK:

        _DATA_CACHE[key] = CacheEntry(
            dataframe=dataframe.copy(),
            created_at=time.time(),
        )


def clear_market_cache():

    with _CACHE_LOCK:

        count = len(
            _DATA_CACHE
        )

        _DATA_CACHE.clear()


    return count


def market_cache_stats():

    now = time.time()

    items = []


    with _CACHE_LOCK:

        for (
            key,
            entry,
        ) in _DATA_CACHE.items():

            symbol, period, interval = key

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
                "age_seconds": round(
                    age,
                    1,
                ),
                "fresh": (
                    age
                    <= CACHE_TTL_SECONDS
                ),
                "stale_usable": (
                    age
                    <= STALE_CACHE_MAX_AGE_SECONDS
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
# YAHOO RATE LIMIT
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
        0,
        round(
            remaining,
            1,
        ),
    )


def activate_yahoo_cooldown():

    global _yahoo_cooldown_until

    _yahoo_cooldown_until = (
        time.time()
        + YAHOO_RATE_LIMIT_COOLDOWN_SECONDS
    )


def _is_rate_limit_error(
    exc: Exception,
):

    message = str(
        exc
    ).lower()

    patterns = [
        "rate limit",
        "too many requests",
        "yfratelimiterror",
        "429",
    ]


    return any(
        pattern in message
        for pattern in patterns
    )


# ============================================================
# DOWNLOAD THROTTLING
# ============================================================

def _wait_for_download_slot():

    global _last_download_time


    with _DOWNLOAD_LOCK:

        now = time.time()

        elapsed = (
            now
            - _last_download_time
        )


        if elapsed < MIN_DOWNLOAD_GAP_SECONDS:

            time.sleep(
                MIN_DOWNLOAD_GAP_SECONDS
                - elapsed
            )


        _last_download_time = (
            time.time()
        )


# ============================================================
# CLEAN YAHOO DATA
# ============================================================

def _clean_download(
    dataframe: pd.DataFrame,
):

    if dataframe is None:
        return pd.DataFrame()


    data = dataframe.copy()


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
        column in data.columns
        for column in required
    ):

        return pd.DataFrame()


    data = data.dropna(
        subset=required
    )


    return data


# ============================================================
# ONE CONTROLLED YAHOO DOWNLOAD
# ============================================================

def _download_market_data(
    symbol: str,
    period: str,
    interval: str,
):

    if yahoo_cooldown_active():

        raise RuntimeError(
            "Yahoo data source is "
            "temporarily cooling down "
            f"for approximately "
            f"{yahoo_cooldown_remaining()} "
            "seconds"
        )


    _wait_for_download_slot()


    try:

        data = yf.download(
            symbol,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=True,
            threads=False,
        )


        cleaned = _clean_download(
            data
        )


        if cleaned.empty:

            raise ValueError(
                "Yahoo returned no usable rows"
            )


        return cleaned


    except Exception as exc:

        if _is_rate_limit_error(
            exc
        ):

            activate_yahoo_cooldown()

            raise RuntimeError(
                "Yahoo rate limit detected"
            ) from exc


        raise


# ============================================================
# PUBLIC MARKET-DATA FUNCTION
# ============================================================

def get_data(
    symbol: str,
    period: str = "1mo",
    interval: str = "15m",
):

    symbol = str(
        symbol
    ).strip()

    interval = str(
        interval
    ).lower().strip()


    if not symbol:

        raise HTTPException(
            status_code=400,
            detail="Market symbol required",
        )


    periods = fallback_periods(
        period,
        interval,
    )


    errors = []


    # ========================================================
    # FIRST PASS: FRESH CACHE
    # ========================================================

    for safe_p in periods:

        cached = _get_fresh_cache(
            symbol,
            safe_p,
            interval,
        )


        if (
            cached is not None
            and len(cached) >= 80
        ):

            return cached


    # ========================================================
    # IF YAHOO IS COOLING DOWN, USE STALE CACHE
    # ========================================================

    if yahoo_cooldown_active():

        for safe_p in periods:

            stale = _get_stale_cache(
                symbol,
                safe_p,
                interval,
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
                "temporarily rate limited "
                "and no cached dataset "
                "is available for "
                f"{symbol} {interval}"
            ),
        )


    # ========================================================
    # DOWNLOAD
    # ========================================================

    for safe_p in periods:

        try:

            data = _download_market_data(
                symbol=symbol,
                period=safe_p,
                interval=interval,
            )


            if not data.empty:

                _store_cache(
                    symbol=symbol,
                    period=safe_p,
                    interval=interval,
                    dataframe=data,
                )


            if len(data) >= 80:

                return data.copy()


            errors.append(
                f"{safe_p}: only "
                f"{len(data)} rows"
            )


        except Exception as exc:

            errors.append(
                f"{safe_p}: {exc}"
            )


            # If this failure activated the
            # rate-limit cooldown, stop making
            # further Yahoo requests immediately.
            if yahoo_cooldown_active():
                break


    # ========================================================
    # FINAL STALE-CACHE FALLBACK
    # ========================================================

    for safe_p in periods:

        stale = _get_stale_cache(
            symbol,
            safe_p,
            interval,
        )


        if (
            stale is not None
            and len(stale) >= 80
        ):

            return stale


    raise HTTPException(
        status_code=502,
        detail=(
            "Market data unavailable for "
            f"{symbol} at {interval}. "
            + " | ".join(errors)
        ),
    )


# ============================================================
# BUILD AI DATASET
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

    enriched = enrich(
        indicators,
        model,
    )


    return enriched


# ============================================================
# ROOT
# ============================================================

@app.get("/")
def root():

    return {
        "name":
            "Jasong AI Trader V4.4",

        "version":
            "4.4.0",

        "mode":
            "paper-trading",

        "live_execution":
            False,

        "message":
            "AI-assisted research "
            "and paper trading only.",
    }


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
def health():

    return {
        "status": "ok",
        "version": "4.4.0",
        "live_execution": False,
        "cache_entries":
            len(_DATA_CACHE),
        "yahoo_cooldown_active":
            yahoo_cooldown_active(),
        "yahoo_cooldown_remaining":
            yahoo_cooldown_remaining(),
    }


# ============================================================
# CACHE DIAGNOSTICS
# ============================================================

@app.get("/data-cache")
def data_cache():

    return {
        "status": "ok",
        **market_cache_stats(),
    }


@app.post("/data-cache/clear")
def clear_data_cache():

    removed = (
        clear_market_cache()
    )


    return {
        "status":
            "cleared",

        "entries_removed":
            removed,
    }


# ============================================================
# SIGNAL
# ============================================================

@app.get("/signal")
def signal(
    symbol: str =
        "EURUSD=X",

    risk_mode: str =
        "Balanced",

    period: str =
        "1mo",

    interval: str =
        "15m",

    balance: float =
        10000.0,
):

    validate_risk_mode(
        risk_mode
    )

    validate_balance(
        balance,
        "balance",
    )


    profile = (
        PROFILES[
            risk_mode
        ]
    )


    data = build(
        symbol,
        period,
        interval,
    )


    result = decision(
        data,
        profile,
    )


    result.update({
        "symbol":
            symbol,

        "risk_mode":
            risk_mode,

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
    symbol: str =
        "EURUSD=X",

    risk_mode: str =
        "Balanced",

    period: str =
        "1mo",

    interval: str =
        "15m",

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


    data = build(
        symbol,
        period,
        interval,
    )


    result = backtest(
        data,
        PROFILES[
            risk_mode
        ],
        starting_balance,
        payout,
    )


    result.update({
        "symbol":
            symbol,

        "risk_mode":
            risk_mode,

        "live_execution":
            False,
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
    db: Session =
        Depends(get_db),
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
        "id":
            trade.id,

        "status":
            "recorded",

        "live_execution":
            False,
    }


@app.get("/paper-trades")
def list_paper_trades(
    db: Session =
        Depends(get_db),
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
            "id":
                row.id,

            "created_at":
                row.created_at.isoformat(),

            "symbol":
                row.symbol,

            "direction":
                row.direction,

            "confidence":
                row.confidence,

            "entry_price":
                row.entry_price,

            "stake":
                row.stake,

            "result":
                row.result,

            "pnl":
                row.pnl,

            "closed":
                row.closed,
        }

        for row in rows
    ]


# ============================================================
# BACKTEST ALL
# ============================================================

@app.get("/backtest-all")
def run_backtest_all(
    risk_mode: str =
        "Balanced",

    period: str =
        "1mo",

    interval: str =
        "15m",

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


    profile = (
        PROFILES[
            risk_mode
        ]
    )


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


            result = backtest(
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
                    result.get(
                        "trades",
                        0,
                    ),

                "wins":
                    result.get(
                        "wins",
                        0,
                    ),

                "losses":
                    result.get(
                        "losses",
                        0,
                    ),

                "win_rate":
                    result.get(
                        "win_rate",
                        0.0,
                    ),

                "return_pct":
                    result.get(
                        "return_pct",
                        0.0,
                    ),

                "max_drawdown":
                    result.get(
                        "max_drawdown",
                        0.0,
                    ),

                "profit_factor":
                    result.get(
                        "profit_factor",
                        0.0,
                    ),

                "average_trade_pnl":
                    result.get(
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


    ranked = sorted(
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
            ranked,

        "live_execution":
            False,
    }


# ============================================================
# THRESHOLD SWEEP
# ============================================================

@app.get("/threshold-sweep")
def run_threshold_sweep(
    symbol: str =
        "EURUSD=X",

    risk_mode: str =
        "Balanced",

    period: str =
        "1mo",

    interval: str =
        "15m",

    starting_balance: float =
        10000.0,

    payout: float =
        0.80,

    holding_candles: int =
        4,
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
        PROFILES[
            risk_mode
        ],
        starting_balance=
            starting_balance,
        payout=
            payout,
        holding_candles=
            holding_candles,
    )


    result.update({
        "symbol":
            symbol,

        "risk_mode":
            risk_mode,

        "period":
            period,

        "interval":
            interval,

        "holding_candles":
            holding_candles,

        "live_execution":
            False,
    })


    return result


# ============================================================
# STRATEGY OPTIMISE
# ============================================================

@app.get("/strategy-optimize")
def run_strategy_optimize(
    symbol: str =
        "EURUSD=X",

    risk_mode: str =
        "Balanced",

    period: str =
        "1mo",

    interval: str =
        "15m",

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
        PROFILES[
            risk_mode
        ],
        starting_balance=
            starting_balance,
        payout=
            payout,
    )


    result.update({
        "symbol":
            symbol,

        "risk_mode":
            risk_mode,

        "period":
            period,

        "interval":
            interval,

        "live_execution":
            False,
    })


    return result


# ============================================================
# OPTIMISE TIMEFRAMES
# ============================================================

@app.get("/optimize-timeframes")
def run_optimize_timeframes(
    symbol: str =
        "EURUSD=X",

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


    result = optimise_all_timeframes(
        symbol=
            symbol,

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
    )


    result.update({
        "risk_mode":
            risk_mode,

        "live_execution":
            False,
    })


    return result


# ============================================================
# SCAN ONE MARKET
# ============================================================

@app.get("/scan-market")
def run_scan_market(
    market: str =
        "EURUSD",

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


    market = (
        market.upper()
    )


    if market not in MARKETS:

        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown market: "
                f"{market}"
            ),
        )


    symbol = (
        MARKETS[
            market
        ]
    )


    result = optimise_all_timeframes(
        symbol=
            symbol,

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


        status = classify_result(
            trades,
            win_rate,
            profit_factor,
            return_pct,
            max_drawdown,
        )


        market_score = (
            calculate_market_score(
                trades,
                win_rate,
                profit_factor,
                return_pct,
                max_drawdown,
            )
        )


        best[
            "status"
        ] = status

        best[
            "market_score"
        ] = market_score


    result.update({
        "market":
            market,

        "symbol":
            symbol,

        "risk_mode":
            risk_mode,

        "status":
            (
                best.get(
                    "status"
                )
                if best
                else "NO_DATA"
            ),

        "market_score":
            (
                best.get(
                    "market_score",
                    0.0,
                )
                if best
                else 0.0
            ),

        "live_execution":
            False,
    })


    return result


# ============================================================
# RANK MARKETS
# ============================================================

@app.post("/rank-markets")
def rank_markets(
    results: list =
        Body(...),

    top_n: int =
        3,
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
        or
        top_n > 9
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
# V4.2 FAST SCAN
# ============================================================

@app.get("/fast-scan")
def run_fast_scan(
    period: str =
        "5d",

    interval: str =
        "15m",

    top_n: int =
        3,
):

    if (
        top_n < 1
        or
        top_n > len(MARKETS)
    ):

        raise HTTPException(
            status_code=400,
            detail=(
                f"top_n must be between "
                f"1 and {len(MARKETS)}"
            ),
        )


    return fast_scan_markets(
        markets=
            MARKETS,

        get_data_func=
            get_data,

        add_indicators_func=
            add_indicators,

        period=
            period,

        interval=
            interval,

        top_n=
            top_n,
    )


# ============================================================
# V4.4 DEEP VALIDATION
# ============================================================
#
# IMPORTANT:
#
# Deep validation is intentionally limited to ONE candidate
# per HTTP request.
#
# Why?
#
# The heavy multi-timeframe optimiser can request several
# market datasets. Doing this for 3 markets inside one Render
# request was causing long-running requests and 502 failures.
#
# We will validate #1 first.
# If it fails, the application can validate #2 next.
# ============================================================

@app.post("/deep-validate")
def run_deep_validate(
    candidates: list[dict],

    risk_mode: str =
        "Balanced",

    starting_balance: float =
        10000.0,

    payout: float =
        0.80,

    max_candidates: int =
        1,
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


    # V4.4 deliberately validates one market per
    # request to protect the Render worker.
    candidate_to_test = [
        candidates[0]
    ]


    result = validate_candidates(
        candidates=
            candidate_to_test,

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


    result[
        "requested_candidates"
    ] = len(
        candidates
    )


    result[
        "candidates_processed_this_request"
    ] = 1


    result[
        "validation_mode"
    ] = (
        "SEQUENTIAL_SINGLE_CANDIDATE"
    )


    result[
        "next_candidate_available"
    ] = (
        len(candidates) > 1
    )


    return result
# ============================================================
# V4.5 AUTO VERIFIED TRADE FINDER
# ============================================================

@app.get("/find-verified-trade")
def find_verified_trade(
    risk_mode: str = "Balanced",
    starting_balance: float = 10000.0,
    payout: float = 0.80,
    period: str = "5d",
    interval: str = "15m",
    top_n: int = 3,
):
    """
    V4.5 automatic trade finder.

    Workflow:

    1. Fast-scan all supported markets.
    2. Take the strongest candidates.
    3. Deep-validate them sequentially.
    4. Stop immediately when one candidate becomes VERIFIED.
    5. Return NO_VERIFIED_TRADE if none pass.

    This endpoint finds historically validated setups.
    It does NOT execute live trades.
    """

    # --------------------------------------------------------
    # INPUT VALIDATION
    # --------------------------------------------------------

    validate_risk_mode(
        risk_mode
    )

    validate_balance(
        starting_balance
    )

    if top_n < 1 or top_n > 3:

        raise HTTPException(
            status_code=400,
            detail=(
                "top_n must be between "
                "1 and 3"
            ),
        )

    # --------------------------------------------------------
    # STEP 1: FAST SCAN
    # --------------------------------------------------------

    try:

        fast_result = fast_scan_markets(
            markets=MARKETS,
            get_data_func=get_data,
            add_indicators_func=add_indicators,
            period=period,
            interval=interval,
            top_n=top_n,
        )

    except Exception as exc:

        raise HTTPException(
            status_code=502,
            detail=(
                "Fast scan failed: "
                f"{exc}"
            ),
        )

    # --------------------------------------------------------
    # FIND TOP CANDIDATES
    # --------------------------------------------------------

    candidates = (
        fast_result.get("top_candidates")
        or fast_result.get("ranking")
        or fast_result.get("candidates")
        or []
    )

    if not candidates:

        return {
            "version":
                "V4.5",

            "status":
                "NO_CANDIDATES",

            "message":
                "Fast scan found no "
                "trade candidates.",

            "markets_tested":
                fast_result.get(
                    "markets_tested",
                    len(MARKETS),
                ),

            "candidates_tested":
                0,

            "verified_trade":
                None,

            "validation_history":
                [],

            "live_execution":
                False,
        }

    # Only use the requested shortlist.
    candidates = candidates[:top_n]

    validation_history = []

    # --------------------------------------------------------
    # STEP 2:
    # DEEP VALIDATE SEQUENTIALLY
    # --------------------------------------------------------

    for index, candidate in enumerate(
        candidates,
        start=1,
    ):

        market = candidate.get(
            "market"
        )

        symbol = candidate.get(
            "symbol"
        )

        fast_score = candidate.get(
            "fast_score",
            candidate.get(
                "score",
                0.0,
            ),
        )

        fast_direction = candidate.get(
            "direction",
            "WAIT",
        )

        # ----------------------------------------------------
        # DEEP VALIDATION
        # ----------------------------------------------------

        try:

            deep_result = validate_candidates(
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

        except Exception as exc:

            validation_history.append({
                "position":
                    index,

                "market":
                    market,

                "symbol":
                    symbol,

                "fast_score":
                    fast_score,

                "fast_direction":
                    fast_direction,

                "deep_status":
                    "ERROR",

                "verified":
                    False,

                "error":
                    str(exc),
            })

            continue

        # ----------------------------------------------------
        # EXTRACT RESULT
        # ----------------------------------------------------

        final_market = (
            deep_result.get(
                "final_market"
            )
        )

        final_status = (
            deep_result.get(
                "final_status",
                "NOT_VERIFIED",
            )
        )

        if final_market:

            deep_status = (
                final_market.get(
                    "status",
                    final_status,
                )
            )

            verified = bool(
                final_market.get(
                    "verified",
                    False,
                )
            )

            history_item = {
                "position":
                    index,

                "market":
                    market,

                "symbol":
                    symbol,

                "fast_score":
                    fast_score,

                "fast_direction":
                    fast_direction,

                "deep_status":
                    deep_status,

                "verified":
                    verified,

                "deep_score":
                    final_market.get(
                        "deep_score"
                    ),

                "trades":
                    final_market.get(
                        "trades"
                    ),

                "wins":
                    final_market.get(
                        "wins"
                    ),

                "losses":
                    final_market.get(
                        "losses"
                    ),

                "win_rate":
                    final_market.get(
                        "win_rate"
                    ),

                "profit_factor":
                    final_market.get(
                        "profit_factor"
                    ),

                "return_pct":
                    final_market.get(
                        "return_pct"
                    ),

                "max_drawdown":
                    final_market.get(
                        "max_drawdown"
                    ),

                "period":
                    final_market.get(
                        "period"
                    ),

                "interval":
                    final_market.get(
                        "interval"
                    ),

                "threshold":
                    final_market.get(
                        "threshold"
                    ),

                "holding_candles":
                    final_market.get(
                        "holding_candles"
                    ),

                "validated_sample":
                    final_market.get(
                        "validated_sample"
                    ),
            }

        else:

            verified = False

            history_item = {
                "position":
                    index,

                "market":
                    market,

                "symbol":
                    symbol,

                "fast_score":
                    fast_score,

                "fast_direction":
                    fast_direction,

                "deep_status":
                    final_status,

                "verified":
                    False,
            }

        validation_history.append(
            history_item
        )

        # ----------------------------------------------------
        # STEP 3:
        # STOP ON FIRST VERIFIED MARKET
        # ----------------------------------------------------

        if verified:

            # -----------------------------------------------
            # CHECK DIRECTION AGREEMENT
            # -----------------------------------------------

            deep_direction = (
                final_market.get(
                    "direction",
                    "WAIT",
                )
            )

            direction_agreement = (
                fast_direction
                == deep_direction
            )

            # If fast and deep validation disagree on
            # direction, do NOT return it as final.
            if not direction_agreement:

                validation_history[
                    -1
                ][
                    "deep_status"
                ] = (
                    "DIRECTION_MISMATCH"
                )

                validation_history[
                    -1
                ][
                    "verified"
                ] = False

                continue

            # -----------------------------------------------
            # VERIFIED TRADE FOUND
            # -----------------------------------------------

            verified_trade = {
                **final_market,

                "fast_rank":
                    index,

                "fast_score":
                    fast_score,

                "fast_direction":
                    fast_direction,

                "direction_agreement":
                    True,
            }

            return {
                "version":
                    "V4.5",

                "status":
                    "VERIFIED_TRADE_FOUND",

                "message":
                    (
                        f"{market} passed "
                        "fast scan and deep "
                        "validation."
                    ),

                "risk_mode":
                    risk_mode,

                "markets_tested":
                    fast_result.get(
                        "markets_tested",
                        len(MARKETS),
                    ),

                "fast_candidates":
                    len(candidates),

                "candidates_tested":
                    index,

                "verified_trade":
                    verified_trade,

                "validation_history":
                    validation_history,

                "live_execution":
                    False,

                "warning":
                    (
                        "Historical validation "
                        "does not guarantee the "
                        "next trade will win."
                    ),
            }

    # --------------------------------------------------------
    # NO VERIFIED TRADE
    # --------------------------------------------------------

    return {
        "version":
            "V4.5",

        "status":
            "NO_VERIFIED_TRADE",

        "message":
            (
                "None of the shortlisted "
                "markets passed deep "
                "validation."
            ),

        "risk_mode":
            risk_mode,

        "markets_tested":
            fast_result.get(
                "markets_tested",
                len(MARKETS),
            ),

        "fast_candidates":
            len(candidates),

        "candidates_tested":
            len(
                validation_history
            ),

        "verified_trade":
            None,

        "validation_history":
            validation_history,

        "live_execution":
            False,

        "warning":
            (
                "No trade should be forced "
                "when validation criteria "
                "are not satisfied."
            ),
    }
