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
# APP
# ============================================================

app = FastAPI(
    title="Jasong AI Trader V4.3 API",
    version="4.3.0",
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
# V4.3 MARKET DATA CACHE
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

CACHE_TTL_SECONDS = 300

MIN_DOWNLOAD_GAP_SECONDS = 1.25

_last_download_time = 0.0


# ============================================================
# PERIOD SAFETY
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

    # Yahoo 1-minute data is especially restricted.
    if interval == "1m":

        allowed = {
            "1d",
            "5d",
            "7d",
        }

        if period not in allowed:
            return "5d"

        return period

    # Intraday data has tighter history limits.
    if interval in {
        "2m",
        "5m",
        "15m",
        "30m",
        "60m",
        "90m",
        "1h",
    }:

        allowed = {
            "1d",
            "5d",
            "1mo",
        }

        if period not in allowed:
            return "1mo"

        return period

    return period


def fallback_periods(
    period: str,
    interval: str,
) -> list[str]:

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

    result = []

    for item in candidates:

        if item not in result:
            result.append(item)

    return result


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


def _get_cached(
    symbol: str,
    period: str,
    interval: str,
) -> Optional[pd.DataFrame]:

    key = _cache_key(
        symbol,
        period,
        interval,
    )

    now = time.time()

    with _CACHE_LOCK:

        entry = _DATA_CACHE.get(
            key
        )

        if entry is None:
            return None

        age = (
            now
            - entry.created_at
        )

        if age > CACHE_TTL_SECONDS:

            _DATA_CACHE.pop(
                key,
                None,
            )

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

        _DATA_CACHE[key] = (
            CacheEntry(
                dataframe=
                    dataframe.copy(),

                created_at=
                    time.time(),
            )
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

    with _CACHE_LOCK:

        items = []

        for (
            key,
            entry,
        ) in _DATA_CACHE.items():

            symbol, period, interval = key

            items.append({
                "symbol":
                    symbol,

                "period":
                    period,

                "interval":
                    interval,

                "age_seconds":
                    round(
                        now
                        - entry.created_at,
                        1,
                    ),

                "rows":
                    len(
                        entry.dataframe
                    ),
            })

    return {
        "entries":
            len(items),

        "ttl_seconds":
            CACHE_TTL_SECONDS,

        "items":
            items,
    }


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
# DATA CLEANING
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


def _is_rate_limit_error(
    exc: Exception,
):

    message = str(
        exc
    ).lower()

    indicators = [
        "rate limit",
        "too many requests",
        "yfratelimiterror",
        "429",
    ]

    return any(
        marker in message
        for marker in indicators
    )


# ============================================================
# YAHOO DOWNLOAD
# ============================================================

def _download_market_data(
    symbol: str,
    period: str,
    interval: str,
    retries: int = 3,
):

    last_exception = None

    for attempt in range(
        retries
    ):

        try:

            _wait_for_download_slot()

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

            if not cleaned.empty:

                return cleaned

            last_exception = (
                ValueError(
                    "Yahoo returned "
                    "no usable rows"
                )
            )

        except Exception as exc:

            last_exception = exc

            if not _is_rate_limit_error(
                exc
            ):

                break

        if attempt < retries - 1:

            delay = (
                2 ** (
                    attempt + 1
                )
            )

            delay += random.uniform(
                0.1,
                0.8,
            )

            time.sleep(
                delay
            )

    if last_exception is not None:

        raise last_exception

    raise ValueError(
        "Market download failed"
    )


# ============================================================
# PUBLIC MARKET DATA FUNCTION
# ============================================================

def get_data(
    symbol: str,
    period: str = "1mo",
    interval: str = "15m",
):

    try:

        symbol = str(
            symbol
        ).strip()

        interval = str(
            interval
        ).lower().strip()

        if not symbol:

            raise ValueError(
                "Market symbol "
                "is required"
            )

        periods = fallback_periods(
            period,
            interval,
        )

        errors = []

        for safe_p in periods:

            # --------------------------------------------
            # CACHE FIRST
            # --------------------------------------------

            cached = _get_cached(
                symbol,
                safe_p,
                interval,
            )

            if cached is not None:

                if len(cached) >= 80:

                    return cached

            # --------------------------------------------
            # DOWNLOAD ONLY WHEN NECESSARY
            # --------------------------------------------

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
                    f"{safe_p}: "
                    f"only "
                    f"{len(data)} rows"
                )

            except Exception as exc:

                errors.append(
                    f"{safe_p}: "
                    f"{exc}"
                )

        raise ValueError(
            "Market data unavailable "
            f"for {symbol} "
            f"at {interval}. "
            + " | ".join(errors)
        )

    except HTTPException:
        raise

    except Exception as e:

        raise HTTPException(
            status_code=502,
            detail=(
                "Market data unavailable: "
                f"{e}"
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

    ind = add_indicators(
        raw
    )

    model = train_model(
        ind
    )

    return enrich(
        ind,
        model,
    )


# ============================================================
# VALIDATION HELPERS
# ============================================================

def validate_risk_mode(
    risk_mode: str,
):

    if risk_mode not in PROFILES:

        raise HTTPException(
            status_code=400,
            detail=
                "Invalid risk mode",
        )


def validate_balance(
    balance: float,
    field_name:
        str = "starting_balance",
):

    if balance <= 0:

        raise HTTPException(
            status_code=400,
            detail=(
                f"{field_name} "
                "must be greater than 0"
            ),
        )


# ============================================================
# ROOT
# ============================================================

@app.get("/")
def root():

    return {
        "name":
            "Jasong AI Trader V4.3",

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
        "status":
            "ok",

        "version":
            "4.3.0",

        "live_execution":
            False,
    }


# ============================================================
# CACHE DIAGNOSTICS
# ============================================================

@app.get("/data-cache")
def data_cache():

    return {
        "status":
            "ok",

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

    sig = build(
        symbol,
        period,
        interval,
    )

    out = decision(
        sig,
        profile,
    )

    out.update({
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

    return out


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

    sig = build(
        symbol,
        period,
        interval,
    )

    result = backtest(
        sig,
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
                "stake must be "
                "greater than 0"
            ),
        )

    t = Trade(
        symbol=symbol,
        direction=direction,
        confidence=confidence,
        entry_price=entry_price,
        stake=stake,
        mode="paper",
    )

    db.add(t)
    db.commit()
    db.refresh(t)

    return {
        "id":
            t.id,

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
                r.id,

            "created_at":
                r.created_at.isoformat(),

            "symbol":
                r.symbol,

            "direction":
                r.direction,

            "confidence":
                r.confidence,

            "entry_price":
                r.entry_price,

            "stake":
                r.stake,

            "result":
                r.result,

            "pnl":
                r.pnl,

            "closed":
                r.closed,
        }

        for r in rows
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

    for name, symbol in (
        MARKETS.items()
    ):

        try:

            sig = build(
                symbol,
                period,
                interval,
            )

            result = backtest(
                sig,
                profile,
                starting_balance,
                payout,
            )

            results.append({
                "market":
                    name,

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

        except Exception as e:

            results.append({
                "market":
                    name,

                "symbol":
                    symbol,

                "error":
                    str(e),
            })

    ranked = sorted(
        results,
        key=lambda x:
            x.get(
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

        "live_execution":
            False,

        "markets_tested":
            len(MARKETS),

        "results":
            ranked,
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

    ind = add_indicators(
        raw
    )

    model = train_model(
        ind
    )

    enriched = enrich(
        ind,
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

    ind = add_indicators(
        raw
    )

    model = train_model(
        ind
    )

    enriched = enrich(
        ind,
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

    best = (
        result.get(
            "best"
        )
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
# RANK COMPLETED MARKETS
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
                "results must "
                "be a list"
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
                "top_n must "
                "be between "
                "1 and 9"
            ),
        )

    ranked = build_top_markets(
        results=results,
        top_n=top_n,
    )

    return ranked


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
                "top_n must be "
                f"between 1 and "
                f"{len(MARKETS)}"
            ),
        )

    result = fast_scan_markets(
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

    return result


# ============================================================
# V4.3 DEEP VALIDATION
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
        3,
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

    if (
        max_candidates < 1
        or
        max_candidates > 3
    ):

        raise HTTPException(
            status_code=400,
            detail=(
                "max_candidates "
                "must be between "
                "1 and 3"
            ),
        )

    result = validate_candidates(
        candidates=
            candidates,

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
            max_candidates,
    )

    return result
