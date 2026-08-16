
from adaptive_confidence import AdaptiveConfidenceGate
from v66_intelligence import V66Intelligence
from v68_copilot import COPILOT
import os
import threading
import time
from dataclasses import dataclass, replace
from typing import Dict, Optional, Tuple

import pandas as pd
import yfinance as yf

from market_data_router import (
    canonical_fx_symbol,
    get_forex_universe,
    get_market_data,
    get_discovery_market_data,
    get_learning_universe,
    market_data_health,
    get_ig_demo_broker,
)

from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
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
from auto_manager import AutomatedTradeManager
from risk_gateway import RiskGateway
from execution_gateway import ExecutionGateway
from autonomous_controller import AutonomousController
from confidence_replay import ConfidenceReplayEngine
from confidence_wr_analyzer import ConfidenceWinRateAnalyzer
from persistent_state import PersistentStateStore
from v64_learning_engine import V64LearningTradeEngine
from ig_demo_broker import IGDemoError
from ig_demo_bridge import IGDemoMirror
from compound_engine import EliteCompoundEngine

from database import (
    SessionLocal,
    Trade,
    init_db,
)


# ============================================================
# V6.6 UNIFIED ADAPTIVE THRESHOLD POLICY
# ============================================================
PAPER_NORMAL_MIN_CONFIDENCE = 0.30
PAPER_AI_MIN_CONFIDENCE = 0.40
PAPER_ABSOLUTE_MIN_CONFIDENCE = 0.30

# Make the threshold uniform across ALL risk modes. Risk mode now changes
# position sizing / drawdown controls only, not entry-confidence requirements.
PROFILES = {
    name: replace(profile, min_confidence=PAPER_NORMAL_MIN_CONFIDENCE)
    for name, profile in PROFILES.items()
}


# ============================================================
# V6.6 AI40-ONLY AUTONOMOUS PAPER LEARNING POLICY
# ============================================================
class AI40OnlyLearningTradeEngine(V64LearningTradeEngine):
    """PAPER-only learning engine that requires the AI40 path to enter.

    The inherited V6.6 learning engine already handles candidate watchers,
    paper stake sizing, timed settlement, P&L, persistence, shadow evidence
    and learning statistics. This override changes only entry eligibility:

      * BUY/SELL live direction must match the candidate direction.
      * Directional model-AI confidence must be at least 40%.
      * Normal N30 confidence alone can no longer open an AI-learning trade.

    No broker execution is enabled.
    """

    def _entry_class(
        self,
        watcher: Dict[str, object],
        live: Dict[str, object],
    ) -> Dict[str, object]:
        decision = super()._entry_class(
            watcher,
            live,
        )

        verified = bool(
            watcher.get("verified")
        )
        experimental = bool(
            watcher.get("experimental")
        )
        trade_eligible = bool(
            watcher.get(
                "trade_eligible",
                verified or experimental,
            )
        )

        direction_match = bool(
            decision.get("direction_match")
        )
        ai_pass = bool(
            decision.get("ai_pass")
        )

        if (
            trade_eligible
            and direction_match
            and ai_pass
        ):
            return {
                **decision,
                "class": (
                    "EM"
                    if experimental
                    else "M"
                ),
                "enter": True,
                "reason": (
                    "AI40 autonomous PAPER path: "
                    "live direction agrees and directional "
                    "model-AI confidence is >=40%"
                ),
            }

        reason = str(
            decision.get("reason")
            or "AI40 entry requirements not met"
        )

        if (
            trade_eligible
            and direction_match
            and not ai_pass
        ):
            reason = (
                "AI40 autonomous PAPER path rejected: "
                "direction agrees but directional model-AI "
                "confidence is below 40%"
            )

        return {
            **decision,
            "class": "S",
            "enter": False,
            "reason": reason,
        }

# ============================================================
# JASONG AI TRADER V6.5
# HIGH-THROUGHPUT AUTONOMOUS PAPER LEARNING ENGINE
# ============================================================

CORE_MARKETS = {
    "EURUSD": "EUR/USD",
    "GBPUSD": "GBP/USD",
    "USDJPY": "USD/JPY",
    "AUDUSD": "AUD/USD",
    "NZDUSD": "NZD/USD",
    "USDCAD": "USD/CAD",
    "USDCHF": "USD/CHF",
    "EURJPY": "EUR/JPY",
    "GBPJPY": "GBP/JPY",
}

# Backward compatibility for manual endpoints.
MARKETS = dict(CORE_MARKETS)

# Twelve Data Basic is intentionally protected from credit exhaustion.
# The full provider universe is available, but Auto Manager rotates a small
# discovery batch through it rather than downloading 1,300+ histories at once.
FX_DISCOVERY_BATCH_SIZE = len(CORE_MARKETS)
_FX_DISCOVERY_OFFSET = 0
_FX_DISCOVERY_LOCK = threading.RLock()


app = FastAPI(
    title="Jasong AI Trader V6.7.1 API",
    version="6.7.1",
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
    """Unified V6.2 market-data entry point.

    Provider routing is handled by market_data_router.py. Downstream strategy,
    backtest, validator and watcher code continue using this same function.
    """

    symbol = str(symbol or "").strip()
    period = str(period or "1mo").lower().strip()
    interval = str(interval or "15m").lower().strip()

    if not symbol:
        raise HTTPException(
            status_code=400,
            detail="Market symbol required",
        )

    try:
        data = get_market_data(
            symbol=symbol,
            period=period,
            interval=interval,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "V6.2 market data unavailable: "
                f"{symbol} {period} {interval}: {exc}"
            ),
        )

    if data is None or data.empty or len(data) < 80:
        raise HTTPException(
            status_code=502,
            detail=(
                "V6.2 insufficient market data: "
                f"{symbol} {period} {interval}"
            ),
        )

    return data


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
            "Jasong AI Trader V6.6",

        "version":
            "6.6.0",

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
    provider = market_data_health()

    manager = (
        V55_AUTO_MANAGER.status()
        if "V55_AUTO_MANAGER" in globals()
        else {}
    )

    return {
        "status": "ok",
        "version": "6.6.0",
        "engine": "JASONG_AI_V6.6",
        "auto_manager_enabled": bool(
            manager.get("enabled", False)
        ),
        "auto_manager_runs": int(
            manager.get("runs", 0) or 0
        ),
        "market_data_status": provider.get("status"),
        "configured_providers": provider.get(
            "configured_providers", []
        ),
        "healthy_providers": provider.get(
            "healthy_providers", []
        ),
        "fx_universe_size": provider.get(
            "universe_size", 0
        ),
        "v65_learning": (
            V64_LEARNING_ENGINE.status()
            if "V64_LEARNING_ENGINE" in globals()
            else None
        ),
        "threshold_policy": {
            "normal_min_confidence": PAPER_NORMAL_MIN_CONFIDENCE,
            "normal_min_confidence_pct": PAPER_NORMAL_MIN_CONFIDENCE * 100.0,
            "ai_min_confidence": PAPER_AI_MIN_CONFIDENCE,
            "ai_min_confidence_pct": PAPER_AI_MIN_CONFIDENCE * 100.0,
                        "paper_only": True,
        },
        "live_execution": False,
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
# V6.2 FOREX UNIVERSE / DATA PROVIDERS
# ============================================================

@app.get("/fx-universe")
def fx_universe(
    limit: int = 100,
    offset: int = 0,
    refresh: bool = False,
):
    limit = max(1, min(int(limit), 5000))
    offset = max(0, int(offset))

    universe = get_forex_universe(
        force_refresh=refresh
    )
    selected = universe[offset: offset + limit]

    return {
        "version": "6.6.0",
        "total_fx_instruments": len(universe),
        "offset": offset,
        "returned": len(selected),
        "markets": selected,
        "live_execution": False,
    }


@app.get("/market-data-health")
def get_market_data_health():
    return {
        "version": "6.6.0",
        **market_data_health(),
        "live_execution": False,
    }


@app.get("/market-data-probe")
def market_data_probe(
    symbol: str = "EUR/USD",
    period: str = "5d",
    interval: str = "15m",
):
    data = get_data(
        symbol=symbol,
        period=period,
        interval=interval,
    )

    return {
        "status": "ok",
        "symbol": canonical_fx_symbol(symbol),
        "period": period,
        "interval": interval,
        "rows": len(data),
        "latest_price": float(
            data["Close"].iloc[-1]
        ),
        "market_data": market_data_health(),
        "live_execution": False,
    }


# ============================================================
# V6.6 CONTINUOUS SIGNAL -> AUTONOMOUS PAPER BRIDGE
# ============================================================

def _v651_confidence01(value) -> float:
    """Normalize 0..1 or 0..100 confidence values to 0..1."""
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if number != number:
        return 0.0
    if number > 1.0:
        number /= 100.0
    return max(0.0, min(1.0, number))


def _v651_directional_model_ai(result: dict, direction: str) -> float:
    """Convert displayed AI-up probability into confidence for BUY/SELL."""
    raw_up = None
    for key in (
        "combined_up_probability",
        "ai_up_probability",
        "ai_up",
        "up_probability",
        "prob_up",
        "probability_up",
    ):
        if result.get(key) is not None:
            raw_up = result.get(key)
            break
    if raw_up is None:
        return 0.0
    up = _v651_confidence01(raw_up)
    return up if direction == "BUY" else (1.0 - up)


def _v651_norm_symbol(value: str) -> str:
    """Make EUR/USD, EURUSD=X and EURUSD comparable for watcher matching."""
    text = str(value or "").upper()
    for token in ("/", "=", "-", "_", " ", ":"):
        text = text.replace(token, "")
    if text.endswith("X") and len(text) > 6:
        text = text[:-1]
    return text



# ============================================================
# V6.7.1 UNIFIED LIVE INTELLIGENCE BUS
# ============================================================

_V671_INTELLIGENCE_LOCK = threading.RLock()
_V671_LIVE_INTELLIGENCE: dict[str, dict] = {}
try:
    _V671_INTELLIGENCE_TTL_SECONDS = max(
        30,
        min(
            900,
            int(
                os.getenv(
                    "COMPOUND_INTELLIGENCE_TTL_SECONDS",
                    "180",
                )
            ),
        ),
    )
except Exception:
    _V671_INTELLIGENCE_TTL_SECONDS = 180


def _v671_ai_up_probability(
    result: dict,
) -> float:
    for key in (
        "combined_up_probability",
        "ai_up_probability",
        "ai_up",
        "up_probability",
        "prob_up",
        "probability_up",
    ):
        if result.get(key) is not None:
            return _v651_confidence01(
                result.get(key)
            )
    return 0.0


def _v671_intelligence_key(
    symbol: str,
    direction: str,
) -> str:
    return (
        f"{_v651_norm_symbol(symbol)}:"
        f"{str(direction or '').upper().strip()}"
    )


def _v671_prune_intelligence() -> None:
    cutoff = (
        time.time()
        - _V671_INTELLIGENCE_TTL_SECONDS
    )
    with _V671_INTELLIGENCE_LOCK:
        stale = [
            key
            for key, row
            in _V671_LIVE_INTELLIGENCE.items()
            if float(
                row.get("observed_at")
                or 0.0
            )
            < cutoff
        ]
        for key in stale:
            _V671_LIVE_INTELLIGENCE.pop(
                key,
                None,
            )


def _v671_store_live_intelligence(
    *,
    symbol: str,
    risk_mode: str,
    signal_result: dict,
) -> dict:
    """Store the exact live decision used by the Home Live Intelligence card.

    This is an intelligence observation, not an order. Compound still applies
    every Elite gate before IG DEMO execution.
    """
    direction = str(
        signal_result.get("direction")
        or signal_result.get("signal")
        or signal_result.get("decision")
        or "WAIT"
    ).upper().strip()

    quant = _v651_confidence01(
        signal_result.get("confidence")
    )
    ai_up = _v671_ai_up_probability(
        signal_result
    )
    directional_ai = (
        _v651_directional_model_ai(
            signal_result,
            direction,
        )
        if direction in {
            "BUY",
            "SELL",
        }
        else 0.0
    )

    now = time.time()
    snapshot = {
        "version": "6.7.1",
        "source":
            "HOME_LIVE_INTELLIGENCE",
        "symbol": symbol,
        "normalized_symbol":
            _v651_norm_symbol(symbol),
        "direction": direction,
        "risk_mode": risk_mode,
        "quant_confidence": quant,
        "quant_confidence_pct":
            round(
                quant * 100.0,
                2,
            ),
        "ai_up_probability":
            ai_up,
        "ai_up_probability_pct":
            round(
                ai_up * 100.0,
                2,
            ),
        "model_ai_confidence":
            directional_ai,
        "model_ai_directional_confidence_pct":
            round(
                directional_ai * 100.0,
                2,
            ),
        "price":
            signal_result.get("price"),
        "rsi":
            signal_result.get("rsi"),
        "reason":
            signal_result.get("reason"),
        "observed_at":
            now,
        "paper_learning":
            True,
        "ig_demo_compound_eligible_for_evaluation":
            direction
            in {
                "BUY",
                "SELL",
            },
        "live_money_execution":
            False,
    }

    if direction in {
        "BUY",
        "SELL",
    }:
        key = _v671_intelligence_key(
            symbol,
            direction,
        )
        with _V671_INTELLIGENCE_LOCK:
            _V671_LIVE_INTELLIGENCE[
                key
            ] = snapshot

    _v671_prune_intelligence()
    return snapshot


def _v671_recent_intelligence() -> list[dict]:
    _v671_prune_intelligence()
    now = time.time()
    with _V671_INTELLIGENCE_LOCK:
        rows = [
            dict(row)
            for row
            in _V671_LIVE_INTELLIGENCE.values()
        ]

    for row in rows:
        row[
            "age_seconds"
        ] = round(
            max(
                0.0,
                now
                - float(
                    row.get("observed_at")
                    or now
                ),
            ),
            2,
        )

    rows.sort(
        key=lambda row:
            float(
                row.get("observed_at")
                or 0.0
            ),
        reverse=True,
    )
    return rows


def _v671_intelligence_for(
    symbol: str,
    direction: str,
) -> Optional[dict]:
    _v671_prune_intelligence()
    key = _v671_intelligence_key(
        symbol,
        direction,
    )
    with _V671_INTELLIGENCE_LOCK:
        row = _V671_LIVE_INTELLIGENCE.get(
            key
        )
        return (
            dict(row)
            if isinstance(
                row,
                dict,
            )
            else None
        )


def _v651_signal_bridge(
    *,
    symbol: str,
    risk_mode: str,
    balance: float,
    signal_result: dict,
) -> dict:
    """
    Connect Refresh AI Signal to the autonomous PAPER workflow.

    Safety rule: this bridge NEVER creates fake VERIFIED status. If a matching
    VERIFIED learning watcher exists, it forces an immediate learning tick.
    Otherwise it queues Auto Manager so normal discovery + deep validation
    decides whether the setup deserves a verified watcher.
    """
    direction = str(
        signal_result.get("direction")
        or signal_result.get("signal")
        or signal_result.get("decision")
        or "WAIT"
    ).upper()

    quant = _v651_confidence01(signal_result.get("confidence"))
    model_ai = _v651_directional_model_ai(signal_result, direction)
    normal_pass = quant >= V64LearningTradeEngine.NORMAL_MIN_CONFIDENCE
    ai_pass = model_ai >= V64LearningTradeEngine.AI_MIN_CONFIDENCE
    directional = direction in {"BUY", "SELL"}
    eligible = directional and (normal_pass or ai_pass)

    diagnostic = {
        "version": "6.6.0",
        "paper_only": True,
        "live_execution": False,
        "direction": direction,
        "normal_confidence_pct": round(quant * 100.0, 2),
        "normal_threshold_pct": round(
            V64LearningTradeEngine.NORMAL_MIN_CONFIDENCE * 100.0, 2
        ),
        "model_ai_directional_confidence_pct": round(model_ai * 100.0, 2),
        "model_ai_threshold_pct": round(
            V64LearningTradeEngine.AI_MIN_CONFIDENCE * 100.0, 2
        ),
        "normal_pass": normal_pass,
        "ai_pass": ai_pass,
        "eligible_for_verified_watcher_check": eligible,
        "verified_watcher_found": False,
        "action": "NO_ACTION",
        "reason": None,
    }

    if not eligible:
        diagnostic["reason"] = "Signal is WAIT or below both PAPER-learning thresholds."
        return diagnostic

    engine = globals().get("V64_LEARNING_ENGINE")
    manager = globals().get("V55_AUTO_MANAGER")
    if engine is None:
        diagnostic["reason"] = "Learning engine is not initialized."
        return diagnostic

    wanted_symbol = _v651_norm_symbol(symbol)

    try:
        watchers = engine.watchers().get("watchers", [])
        matching = [
            w for w in watchers
            if bool(w.get("verified"))
            and _v651_norm_symbol(w.get("symbol")) == wanted_symbol
            and str(w.get("direction") or "").upper() == direction
        ]

        if matching:
            diagnostic["verified_watcher_found"] = True
            engine.tick()
            diagnostic["action"] = "VERIFIED_WATCHER_RECHECKED"
            diagnostic["reason"] = (
                "Matching VERIFIED watcher found; learning engine rechecked immediately."
            )
            return diagnostic

        if manager is not None:
            queued = manager.queue_run(source="signal_bridge")
            diagnostic["action"] = (
                "AUTO_MANAGER_QUEUED"
                if queued.get("accepted")
                else "AUTO_MANAGER_ALREADY_RUNNING"
            )
            diagnostic["auto_manager_job_id"] = queued.get("job_id")
            diagnostic["reason"] = (
                "Thresholds passed but no matching VERIFIED watcher exists. "
                "Auto Manager was queued for discovery/deep validation."
            )
        else:
            diagnostic["reason"] = "Thresholds passed but Auto Manager is not initialized."
    except Exception as exc:
        diagnostic["action"] = "BRIDGE_ERROR"
        diagnostic["reason"] = str(exc)

    return diagnostic


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
    """V6.6 app signal plus autonomous PAPER bridge diagnostics."""
    validate_risk_mode(risk_mode)
    validate_balance(balance, "balance")

    base_profile = PROFILES[risk_mode]
    profile = replace(
        base_profile,
        min_confidence=V64LearningTradeEngine.NORMAL_MIN_CONFIDENCE,
    )

    signal_data = build(symbol, period, interval)
    result = decision(signal_data, profile)

    result.update({
        "symbol": symbol,
        "risk_mode": risk_mode,
        "suggested_paper_stake": stake_for_balance(
            balance,
            base_profile.risk_per_trade,
        ),
        "threshold_policy": {
            "normal_min_confidence": V64LearningTradeEngine.NORMAL_MIN_CONFIDENCE,
            "normal_min_confidence_pct":
                V64LearningTradeEngine.NORMAL_MIN_CONFIDENCE * 100.0,
            "ai_min_confidence": V64LearningTradeEngine.AI_MIN_CONFIDENCE,
            "ai_min_confidence_pct":
                V64LearningTradeEngine.AI_MIN_CONFIDENCE * 100.0,
                        "paper_only": True,
        },
        "live_execution": False,
    })

    # A manual refresh now participates in the autonomous PAPER workflow.
    # It can trigger verification/recheck, but never bypass deep validation.
    result["autonomous_bridge"] = _v651_signal_bridge(
        symbol=symbol,
        risk_mode=risk_mode,
        balance=balance,
        signal_result=result,
    )

    # V6.7.1: the exact same observation displayed under Live Intelligence is
    # now placed on the shared server-side intelligence bus. If Compound is
    # active, this wakes it immediately instead of waiting for the normal poll.
    shared_intelligence = _v671_store_live_intelligence(
        symbol=symbol,
        risk_mode=risk_mode,
        signal_result=result,
    )
    result[
        "unified_intelligence"
    ] = shared_intelligence

    compound = globals().get(
        "COMPOUND_ENGINE"
    )
    if compound is not None:
        try:
            result[
                "compound_bridge"
            ] = compound.notify_intelligence(
                shared_intelligence
            )
        except Exception as exc:
            result[
                "compound_bridge"
            ] = {
                "accepted": False,
                "bridge_state":
                    "BRIDGE_ERROR",
                "reason":
                    f"{type(exc).__name__}: {exc}",
                "live_money_execution":
                    False,
            }
    else:
        result[
            "compound_bridge"
        ] = {
            "accepted": False,
            "bridge_state":
                "COMPOUND_NOT_INITIALISED",
            "live_money_execution":
                False,
        }

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

    base_profile = PROFILES[
        risk_mode
    ]

    profile = replace(
        base_profile,
        min_confidence=(
            V64LearningTradeEngine.NORMAL_MIN_CONFIDENCE
        ),
    )

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
                base_profile.risk_per_trade,
            ),
        "observed_at": time.time(),
        "live_execution": False,
    })

    return result



def _v63_adaptive_live_signal(
    symbol: str,
    risk_mode: str,
    balance: float,
):
    """V6.3 analysis-only live callback for adaptive PAPER entries.

    It uses the same engine/indicators/model as the normal live signal.
    Only the profile confidence floor is temporarily set to zero so
    lower-confidence BUY/SELL decisions can be evaluated by the rolling
    historical qualification gate. The real PROFILES object is untouched.
    """

    base_profile = PROFILES[
        risk_mode
    ]

    adaptive_profile = replace(
        base_profile,
        min_confidence=0.0,
    )

    signal_data = build(
        symbol,
        "1mo",
        "15m",
    )

    result = decision(
        signal_data,
        adaptive_profile,
    )

    result.update({
        "symbol":
            symbol,
        "risk_mode":
            risk_mode,
        "suggested_paper_stake":
            stake_for_balance(
                balance,
                base_profile.risk_per_trade,
            ),
        "observed_at":
            time.time(),
        "adaptive_analysis_only":
            True,
        "live_execution":
            False,
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



# ============================================================
# V5.6 FORWARD INTELLIGENCE / STRATEGY HEALTH
# ============================================================

V56_FORWARD_MIN_TRADES = 10
V56_QUARANTINE_MIN_TRADES = 15


def _v56_forward_health(
    symbol: str,
    direction: str | None = None,
    payout: float = 0.80,
) -> dict:
    """Forward paper evidence for a symbol/direction.

    Historical validation remains independent. Forward evidence only starts
    influencing ranking after enough unseen paper trades have settled.
    """

    db = SessionLocal()

    try:
        query = (
            db.query(Trade)
            .filter(Trade.mode == "forward")
            .filter(Trade.closed == True)  # noqa: E712
            .filter(Trade.symbol == symbol)
        )

        clean_direction = str(
            direction or ""
        ).upper()

        if clean_direction in {
            "BUY",
            "SELL",
        }:
            query = query.filter(
                Trade.direction
                == clean_direction
            )

        rows = (
            query
            .order_by(
                Trade.created_at.asc()
            )
            .all()
        )

        closed = [
            row
            for row in rows
            if str(
                getattr(
                    row,
                    "result",
                    "",
                )
                or ""
            ).upper()
            in {
                "WIN",
                "LOSS",
            }
        ]

        wins = sum(
            1
            for row in closed
            if str(
                getattr(
                    row,
                    "result",
                    "",
                )
                or ""
            ).upper()
            == "WIN"
        )

        losses = (
            len(closed)
            - wins
        )

        gross_profit = sum(
            max(
                float(
                    getattr(
                        row,
                        "pnl",
                        0.0,
                    )
                    or 0.0
                ),
                0.0,
            )
            for row in closed
        )

        gross_loss = abs(
            sum(
                min(
                    float(
                        getattr(
                            row,
                            "pnl",
                            0.0,
                        )
                        or 0.0
                    ),
                    0.0,
                )
                for row in closed
            )
        )

        trades = len(
            closed
        )

        win_rate = (
            wins / trades
            if trades
            else 0.0
        )

        profit_factor = (
            gross_profit
            / gross_loss
            if gross_loss > 0
            else (
                99.0
                if gross_profit > 0
                else 0.0
            )
        )

        break_even = (
            1.0
            / (
                1.0
                + float(
                    payout
                )
            )
        )

        # ----------------------------------------------------
        # Strategy health classification
        # ----------------------------------------------------

        if trades < V56_FORWARD_MIN_TRADES:
            health = "PROBATION"
            health_reason = (
                "Not enough settled forward trades yet."
            )

        elif (
            trades
            >= V56_QUARANTINE_MIN_TRADES
            and (
                win_rate
                < break_even
                - 0.03
                or profit_factor
                < 0.85
            )
        ):
            health = "QUARANTINED"
            health_reason = (
                "Forward evidence has fallen materially below "
                "the configured profitability floor."
            )

        elif (
            win_rate
            >= break_even
            + 0.08
            and profit_factor
            >= 1.30
        ):
            health = "HEALTHY"
            health_reason = (
                "Forward win rate and profit factor support the "
                "historical edge."
            )

        else:
            health = "DEGRADING"
            health_reason = (
                "Forward edge exists or remains inconclusive, but "
                "does not currently satisfy the HEALTHY threshold."
            )

        health_adjustment = {
            "PROBATION": 0.0,
            "HEALTHY": 6.0,
            "DEGRADING": -6.0,
            "QUARANTINED": -30.0,
        }[
            health
        ]

        return {
            "symbol":
                symbol,
            "direction":
                clean_direction
                or None,
            "trades":
                trades,
            "wins":
                wins,
            "losses":
                losses,
            "win_rate":
                round(
                    win_rate,
                    4,
                ),
            "win_rate_pct":
                round(
                    win_rate
                    * 100.0,
                    1,
                ),
            "profit_factor":
                round(
                    profit_factor,
                    4,
                ),
            "break_even_win_rate":
                round(
                    break_even,
                    4,
                ),
            "break_even_win_rate_pct":
                round(
                    break_even
                    * 100.0,
                    1,
                ),
            "health":
                health,
            "health_reason":
                health_reason,
            "ranking_adjustment":
                health_adjustment,
            "evidence_active":
                trades
                >= V56_FORWARD_MIN_TRADES,
            "quarantined":
                health
                == "QUARANTINED",
        }

    finally:
        db.close()


def _v55_symbol_forward_stats(
    symbol: str,
    payout: float = 0.80,
) -> dict:
    """Backward-compatible V5.5 symbol-level forward stats."""

    return _v56_forward_health(
        symbol=symbol,
        direction=None,
        payout=payout,
    )


def _v56_adaptive_candidate_score(
    candidate: dict,
    payout: float = 0.80,
) -> dict:
    """V5.6 ranking = Smart Fast Score + direction-specific forward health."""

    item = dict(
        candidate
    )

    base_score = float(
        item.get(
            "smart_fast_score",
            item.get(
                "fast_score",
                0.0,
            ),
        )
        or 0.0
    )

    symbol = str(
        item.get(
            "symbol"
        )
        or ""
    )

    direction = str(
        item.get(
            "direction"
        )
        or ""
    ).upper()

    health = _v56_forward_health(
        symbol=symbol,
        direction=direction,
        payout=payout,
    )

    adjustment = float(
        health.get(
            "ranking_adjustment",
            0.0,
        )
        or 0.0
    )

    adaptive_score = max(
        0.0,
        min(
            100.0,
            base_score
            + adjustment,
        ),
    )

    item[
        "adaptive_rank_score"
    ] = round(
        adaptive_score,
        2,
    )

    item[
        "forward_evidence_adjustment"
    ] = round(
        adjustment,
        2,
    )

    item[
        "forward_evidence_active"
    ] = bool(
        health.get(
            "evidence_active",
            False,
        )
    )

    # Keep the old key so V5.5.x UI/code remains compatible.
    item[
        "forward_symbol_stats"
    ] = health

    item[
        "strategy_health"
    ] = health.get(
        "health"
    )

    item[
        "strategy_health_reason"
    ] = health.get(
        "health_reason"
    )

    item[
        "strategy_quarantined"
    ] = bool(
        health.get(
            "quarantined",
            False,
        )
    )

    return item


def _v55_adaptive_candidate_score(
    candidate: dict,
    payout: float = 0.80,
) -> dict:
    """Backward-compatible name, now powered by V5.6 health."""

    return _v56_adaptive_candidate_score(
        candidate=candidate,
        payout=payout,
    )


def _v55_rank_candidates(
    candidates: list[dict],
    payout: float = 0.80,
) -> list[dict]:
    ranked = [
        _v56_adaptive_candidate_score(
            candidate=item,
            payout=payout,
        )
        for item
        in candidates
        if isinstance(
            item,
            dict,
        )
    ]

    ranked.sort(
        key=lambda item: (
            bool(
                not item.get(
                    "strategy_quarantined",
                    False,
                )
            ),
            float(
                item.get(
                    "adaptive_rank_score",
                    0.0,
                )
                or 0.0
            ),
            float(
                item.get(
                    "smart_fast_score",
                    item.get(
                        "fast_score",
                        0.0,
                    ),
                )
                or 0.0
            ),
        ),
        reverse=True,
    )

    return ranked


V61_CONFIDENCE_REPLAY = ConfidenceReplayEngine(
    markets=MARKETS,
    get_data_func=get_data,
    add_indicators_func=add_indicators,
    train_model_func=train_model,
    enrich_func=enrich,
    decision_func=decision,
    profiles=PROFILES,
)

V62_CONFIDENCE_WR_ANALYZER = ConfidenceWinRateAnalyzer(
    markets=MARKETS,
    get_data_func=get_data,
    add_indicators_func=add_indicators,
    train_model_func=train_model,
    enrich_func=enrich,
    decision_func=decision,
    profiles=PROFILES,
)

# -----------------------------------------------------------------------------
# V6.3.1 ADAPTIVE CONFIDENCE GATE -- PAPER ONLY
# -----------------------------------------------------------------------------
adaptive_confidence_gate = AdaptiveConfidenceGate(
    state_path="/tmp/adaptive_confidence_state.json",
    target_win_rate=0.65,
    min_profit_factor=1.50,
    min_trades=20,
    max_age_hours=24.0,
    absolute_min_confidence=0.30,
)


def _v66_fx_correlation_matrix():
    """Recent 15-minute FX return correlations for portfolio gating.

    Uses only past/current market data. If a symbol cannot be loaded it is
    skipped and the portfolio gate falls back to currency-exposure controls.
    """

    returns = {}

    for market, symbol in MARKETS.items():
        try:
            df = get_data(
                symbol,
                "5d",
                "15m",
            )

            if (
                df is None
                or df.empty
                or "Close" not in df
            ):
                continue

            series = (
                df["Close"]
                .astype(float)
                .pct_change()
                .dropna()
                .tail(300)
            )

            if len(series) >= 30:
                returns[
                    symbol.upper()
                ] = series

        except Exception:
            continue

    symbols = list(
        returns.keys()
    )

    matrix = {
        symbol: {}
        for symbol in symbols
    }

    for i, left in enumerate(symbols):
        for right in symbols[
            i + 1:
        ]:
            aligned = (
                returns[left]
                .to_frame("left")
                .join(
                    returns[right]
                    .to_frame("right"),
                    how="inner",
                )
                .dropna()
            )

            if len(aligned) < 30:
                continue

            corr = float(
                aligned["left"]
                .corr(
                    aligned["right"]
                )
            )

            if corr != corr:
                continue

            matrix[left][right] = corr
            matrix[right][left] = corr

    return matrix


V66_INTELLIGENCE = V66Intelligence(
    forward_quarantine_min_trades=8,
    forward_mature_min_trades=20,
    forward_min_win_rate=0.56,
    forward_min_profit_factor=1.00,
    max_currency_exposure=2,
    max_highly_correlated_open=1,
    high_correlation_abs=0.80,
)

V60_RISK_GATEWAY = RiskGateway(
    max_open_trades=2, max_daily_loss_pct=0.04, max_drawdown_pct=0.10, max_consecutive_losses=3,
    max_assumed_spread_bps=3.0, max_price_age_seconds=180,
)
V60_EXECUTION_GATEWAY = ExecutionGateway(mode="PAPER", allow_live_execution=False)
JASONG_STATE_PATH = os.getenv(
    "JASONG_STATE_PATH",
    (
        "/var/data/jasong_v61_state.json"
        if os.path.isdir("/var/data")
        else "data/jasong_v61_state.json"
    ),
)
V61_STATE_STORE = PersistentStateStore(
    JASONG_STATE_PATH
)

V53_WATCHER_ENGINE = TradeWatcherEngine(
    session_factory=SessionLocal, trade_model=Trade, signal_func=_v53_live_signal, price_func=_v53_latest_price, profiles=PROFILES,
    risk_gateway=V60_RISK_GATEWAY,
    execution_gateway=V60_EXECUTION_GATEWAY,
    state_store=V61_STATE_STORE,
    adaptive_signal_func=_v63_adaptive_live_signal,
    adaptive_confidence_gate=adaptive_confidence_gate,
    v66_intelligence=V66_INTELLIGENCE,
    correlation_func=_v66_fx_correlation_matrix,
)

V53_WATCHER_ENGINE.start()


# ============================================================
# V5.5 AUTOMATED TRADE MANAGER
# ============================================================

def _v62_next_fx_batch(
    batch_size: int = FX_DISCOVERY_BATCH_SIZE,
) -> dict[str, str]:
    """V6.6.5 Phase-1 IG DEMO discovery universe.

    Keep the first 10 broker-demo trades on nine liquid core FX pairs.
    This avoids wasting IG historical-data allowance across an 80-pair
    rotating universe while Yahoo/Twelve Data are degraded.
    """
    return dict(CORE_MARKETS)


def _v55_scan_candidates(
    top_n: int = 9,
    payout: float = 0.80,
) -> list[dict]:
    """V6.2 rotating multi-source FX discovery scan."""

    scan_markets = _v62_next_fx_batch(
        batch_size=FX_DISCOVERY_BATCH_SIZE
    )

    if not scan_markets:
        scan_markets = dict(CORE_MARKETS)

    effective_top_n = max(
        1,
        min(
            int(top_n),
            len(scan_markets),
        ),
    )

    raw_result = fast_scan_markets(
        markets=scan_markets,
        get_data_func=get_discovery_market_data,
        add_indicators_func=add_indicators,
        period="5d",
        interval="15m",
        top_n=effective_top_n,
    )

    raw = _v54_rescore_fast_scan(
        result=raw_result,
        top_n=effective_top_n,
    )

    candidates = (
        raw.get("ranking")
        or raw.get("top_candidates")
        or []
    )

    ranked = _v55_rank_candidates(
        candidates=[
            dict(item)
            for item in candidates
            if isinstance(item, dict)
        ],
        payout=payout,
    )

    universe_size = len(
        CORE_MARKETS
    )

    for candidate in ranked:
        candidate["discovery_version"] = (
            "V6.6.5_IG_DEMO_CORE9"
        )
        candidate["discovery_universe_size"] = (
            universe_size
        )
        candidate["discovery_batch_size"] = len(
            scan_markets
        )

    return ranked


def _v55_validate_candidate(
    candidate: dict,
    risk_mode: str,
    starting_balance: float,
    payout: float,
) -> dict:
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
            PROFILES[
                risk_mode
            ],
        starting_balance=
            starting_balance,
        payout=payout,
        max_candidates=1,
    )

    final_market = (
        result.get(
            "final_market"
        )
    )

    if isinstance(
        final_market,
        dict,
    ):
        return dict(
            final_market
        )

    return {
        "market":
            candidate.get(
                "market"
            ),
        "symbol":
            candidate.get(
                "symbol"
            ),
        "status":
            result.get(
                "final_status",
                "NOT_VERIFIED",
            ),
        "verified":
            False,
        "explanation":
            "No verified market returned by deep validation.",
    }


V64_LEARNING_ENGINE = AI40OnlyLearningTradeEngine(
    signal_func=_v63_adaptive_live_signal,
    price_func=_v53_latest_price,
    state_store=V61_STATE_STORE,
    max_watchers=6,
    max_open_trades=1,
    watcher_refresh_seconds=30,
    starting_balance=10000.0,
    payout=0.80,
    default_stake_pct=0.01,
)
V64_LEARNING_ENGINE.start()

# AI-learning experiment: disable the N30 entry path inside V64.
# Only the AI40 model path may open an autonomous learning PAPER trade.
V64_LEARNING_ENGINE.NORMAL_MIN_CONFIDENCE = 1.01
V64_LEARNING_ENGINE.AI_MIN_CONFIDENCE = PAPER_AI_MIN_CONFIDENCE

# ============================================================
# IG DEMO BROKER EXECUTION BRIDGE
# ============================================================
# Strictly demo-only. The broker class hard-codes demo-api.ig.com
# and has no production IG URL.
IG_DEMO_BROKER = get_ig_demo_broker()
IG_DEMO_MIRROR = IGDemoMirror(
    broker=IG_DEMO_BROKER,
    trade_source=lambda: V64_LEARNING_ENGINE.trades(
        limit=200,
    ),
)
IG_DEMO_MIRROR.start()



V55_AUTO_MANAGER = AutomatedTradeManager(
    scan_candidates_func=
        _v55_scan_candidates,
    validate_candidate_func=
        _v55_validate_candidate,
    watcher_engine=
        V53_WATCHER_ENGINE,
    state_store=V61_STATE_STORE,
    learning_engine=V64_LEARNING_ENGINE,
)

V55_AUTO_MANAGER.start_thread()


# ============================================================
# V6.2 ALWAYS-ON AUTO MANAGER
# ============================================================

def _v62_ensure_auto_manager() -> None:
    """Migrate persisted Auto Manager state to the V6.6 learning cadence.

    Risk mode, balance and payout are preserved where available, while the
    sourcing cadence/watch capacity is upgraded to the V6.6 defaults.
    """

    try:
        state = V55_AUTO_MANAGER.status()
        V55_AUTO_MANAGER.enable(
            risk_mode=str(state.get("risk_mode", "Balanced")),
            starting_balance=float(state.get("starting_balance", 10000.0) or 10000.0),
            payout=float(state.get("payout", 0.80) or 0.80),
            scan_interval_minutes=2,
            target_active_watchers=6,
            scan_top_n=9,
        )
        V55_AUTO_MANAGER.start_thread()

    except Exception as exc:
        print(
            "[V6.6 AUTO START ERROR]",
            exc,
        )


_v62_ensure_auto_manager()


V60_CONTROLLER = AutonomousController(
    auto_manager=V55_AUTO_MANAGER, watcher_engine=V53_WATCHER_ENGINE, risk_gateway=V60_RISK_GATEWAY,
    execution_gateway=V60_EXECUTION_GATEWAY, starting_balance=10000.0,
)
V60_CONTROLLER.start_thread()



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



# ============================================================
# V5.5 AUTO MANAGER API
# ============================================================

@app.get("/auto-manager")
def get_auto_manager_status():
    return {
        "manager":
            V55_AUTO_MANAGER.status(),
        "live_execution":
            False,
    }


@app.post("/auto-manager/start")
def start_auto_manager(
    risk_mode: str = "Balanced",
    starting_balance: float = 10000.0,
    payout: float = 0.80,
    scan_interval_minutes: int = 3,
    target_active_watchers: int = 6,
    scan_top_n: int = 9,
):
    validate_risk_mode(
        risk_mode
    )

    validate_balance(
        starting_balance
    )

    try:
        state = V55_AUTO_MANAGER.enable(
            risk_mode=
                risk_mode,
            starting_balance=
                starting_balance,
            payout=
                payout,
            scan_interval_minutes=
                scan_interval_minutes,
            target_active_watchers=
                target_active_watchers,
            scan_top_n=
                scan_top_n,
        )

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    return {
        "status":
            "AUTO_MANAGER_ENABLED",
        "manager":
            state,
        "live_execution":
            False,
    }


@app.post("/auto-manager/stop")
def stop_auto_manager():
    state = (
        V55_AUTO_MANAGER.disable()
    )

    return {
        "status":
            "AUTO_MANAGER_DISABLED",
        "manager":
            state,
        "live_execution":
            False,
    }


@app.post("/auto-manager/run-now")
def run_auto_manager_now():
    queued = (
        V55_AUTO_MANAGER.queue_run(
            source="manual",
        )
    )

    return {
        "status": (
            "AUTO_RUN_QUEUED"
            if queued.get(
                "accepted"
            )
            else "AUTO_RUN_ALREADY_RUNNING"
        ),
        "accepted":
            bool(
                queued.get(
                    "accepted",
                    False,
                )
            ),
        "job_id":
            queued.get(
                "job_id"
            ),
        "job":
            queued.get(
                "job"
            ),
        "manager":
            V55_AUTO_MANAGER.status(),
        "live_execution":
            False,
    }


@app.get("/auto-manager/job/{job_id}")
def get_auto_manager_job(
    job_id: str,
):
    job = (
        V55_AUTO_MANAGER.get_job(
            job_id
        )
    )

    if job is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "Auto Manager job not found"
            ),
        )

    return {
        "job":
            job,
        "manager":
            V55_AUTO_MANAGER.status(),
        "live_execution":
            False,
    }


@app.get("/auto-manager/jobs")
def list_auto_manager_jobs():
    return {
        "jobs":
            V55_AUTO_MANAGER.list_jobs(),
        "manager":
            V55_AUTO_MANAGER.status(),
        "live_execution":
            False,
    }


@app.get("/adaptive-ranking")
def get_adaptive_ranking(
    top_n: int = 9,
    payout: float = 0.80,
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

    candidates = (
        _v55_scan_candidates(
            top_n=top_n,
            payout=payout,
        )
    )

    return {
        "ranking":
            candidates[
                :top_n
            ],
        "forward_evidence_min_trades":
            V56_FORWARD_MIN_TRADES,
        "note":
            (
                "V5.7 adaptive ranking uses direction-specific forward paper evidence "
                "after the configured minimum settled trades. "
                "Deep validation remains mandatory before watching."
            ),
        "live_execution":
            False,
    }





# ============================================================
# V6.2.2 RAW CONFIDENCE CALIBRATION ANALYZER
# ============================================================

@app.post("/confidence-wr/start")
def start_confidence_wr_analysis(
    risk_mode: str = "Balanced",
    days: int = 7,
    interval: str = "15m",
    holding_candles: int = 4,
    stride_candles: int = 1,
    target_win_rate: float = 0.65,
    min_profit_factor: float = 1.50,
    min_trades_qualified: int = 20,
    min_trades_promising: int = 10,
    minimum_trade_confidence: float = 0.30,
):
    """Raw confidence calibration from 30% upward against realised win rate.\n\n    One frozen pre-test model is used per market; the legacy high-confidence gate is not used by V6.5 PAPER learning.\n    This endpoint never opens a trade.\n    """
    validate_risk_mode(risk_mode)

    try:
        return V62_CONFIDENCE_WR_ANALYZER.create_job(
            risk_mode=risk_mode,
            days=days,
            interval=interval,
            holding_candles=holding_candles,
            stride_candles=stride_candles,
            target_win_rate=target_win_rate,
            min_profit_factor=min_profit_factor,
            min_trades_qualified=min_trades_qualified,
            min_trades_promising=min_trades_promising,
            minimum_trade_confidence=minimum_trade_confidence,
            markets=list(MARKETS.keys()),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )


@app.get("/confidence-wr/{job_id}")
def get_confidence_wr_analysis(
    job_id: str,
):
    job = V62_CONFIDENCE_WR_ANALYZER.get_job(
        job_id
    )

    if job is None:
        raise HTTPException(
            status_code=404,
            detail="Confidence/WR job not found",
        )

    return job


# ============================================================
# V6.1 SEVEN-DAY LIVE CONFIDENCE REPLAY
# ============================================================

@app.post("/confidence-replay/start")
def start_confidence_replay(
    risk_mode: str = "Balanced",
    days: int = 7,
    interval: str = "15m",
    threshold: float | None = None,
    stride_candles: int = 1,
):
    """Start an asynchronous no-future-data confidence replay.

    stride_candles=1 checks every 15m candle and is the thorough test.
    It is CPU intensive because the model is rebuilt at every replay point.
    """
    validate_risk_mode(risk_mode)

    try:
        return V61_CONFIDENCE_REPLAY.create_job(
            risk_mode=risk_mode,
            days=days,
            interval=interval,
            threshold=threshold,
            stride_candles=stride_candles,
            markets=list(MARKETS.keys()),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )


@app.get("/confidence-replay/{job_id}")
def get_confidence_replay(
    job_id: str,
):
    job = V61_CONFIDENCE_REPLAY.get_job(
        job_id
    )

    if job is None:
        raise HTTPException(
            status_code=404,
            detail="Confidence replay job not found",
        )

    return job


# ============================================================
# V6.0 AUTONOMOUS / RISK / EXECUTION CONTROL
# ============================================================

@app.get("/v6/status")
def v6_status():
    return {"version":"6.1.0","controller":V60_CONTROLLER.status(),"live_execution":False}

@app.post("/v6/start")
def v6_start(starting_balance:float=10000.0, risk_mode:str="Balanced", payout:float=0.80,
             scan_interval_minutes:int=15, target_active_watchers:int=3, scan_top_n:int=9, overnight_mode:bool=True):
    validate_balance(starting_balance); validate_risk_mode(risk_mode)
    V60_EXECUTION_GATEWAY.set_mode("PAPER")
    controller=V60_CONTROLLER.enable(starting_balance=starting_balance,overnight_mode=overnight_mode)
    manager=V55_AUTO_MANAGER.enable(risk_mode=risk_mode,starting_balance=starting_balance,payout=payout,
        scan_interval_minutes=scan_interval_minutes,target_active_watchers=target_active_watchers,scan_top_n=scan_top_n)
    return {"status":"V6_AUTONOMOUS_PAPER_ENABLED","controller":controller,"auto_manager":manager,"execution_mode":"PAPER","live_execution":False}

@app.post("/v6/stop")
def v6_stop():
    return {"status":"V6_STOPPED","controller":V60_CONTROLLER.disable(reason="MANUAL_STOP"),"live_execution":False}

@app.post("/v6/emergency-stop")
def v6_emergency_stop(reason:str="MANUAL_EMERGENCY_STOP"):
    return {"status":"V6_EMERGENCY_STOPPED","controller":V60_CONTROLLER.emergency_stop(reason=reason),"live_execution":False}

@app.post("/v6/clear-kill-switch")
def v6_clear_kill_switch():
    return {"status":"KILL_SWITCH_CLEARED","risk":V60_RISK_GATEWAY.clear_kill_switch(),"live_execution":False}

@app.get("/risk-status")
def v6_risk_status():
    return {"version":"6.1.0","risk":V60_RISK_GATEWAY.status(),"live_execution":False}

@app.get("/execution-status")
def v6_execution_status():
    return {"version":"6.1.0","execution":V60_EXECUTION_GATEWAY.status(),"live_execution":False}

@app.get("/execution-orders")
def v6_execution_orders(limit:int=200):
    return {"version":"6.1.0","orders":V60_EXECUTION_GATEWAY.list_orders(limit=limit),"live_execution":False}

@app.get("/overnight-report")
def v6_overnight_report():
    return V60_CONTROLLER.report()


# ============================================================
# V5.5.3 AUTO MODE DASHBOARD / TRADE LIFECYCLE
# ============================================================


@app.get("/strategy-health")
def get_strategy_health(
    payout: float = 0.80,
):
    """V5.7 forward health table for all configured market directions."""

    output = []

    for market, symbol in MARKETS.items():
        for direction in (
            "BUY",
            "SELL",
        ):
            output.append(
                {
                    "market":
                        market,
                    **_v56_forward_health(
                        symbol=symbol,
                        direction=direction,
                        payout=payout,
                    ),
                }
            )

    health_order = {
        "HEALTHY": 0,
        "PROBATION": 1,
        "DEGRADING": 2,
        "QUARANTINED": 3,
    }

    output.sort(
        key=lambda item: (
            health_order.get(
                item.get(
                    "health"
                ),
                9,
            ),
            -int(
                item.get(
                    "trades",
                    0,
                )
                or 0
            ),
        )
    )

    return {
        "version":
            "6.6.0",
        "minimum_forward_trades":
            V56_FORWARD_MIN_TRADES,
        "quarantine_minimum_trades":
            V56_QUARANTINE_MIN_TRADES,
        "strategies":
            output,
        "live_execution":
            False,
    }


@app.get("/auto-dashboard")
def get_auto_dashboard(
    starting_balance: float = 10000.0,
):
    """One lightweight snapshot for the mobile Auto Mode dashboard."""

    validate_balance(
        starting_balance
    )

    manager = (
        V55_AUTO_MANAGER.status()
    )

    watchers = (
        V53_WATCHER_ENGINE.list()
    )

    forward = (
        V53_WATCHER_ENGINE.forward_stats(
            starting_balance=
                starting_balance
        )
    )

    learning_status = V64_LEARNING_ENGINE.status()
    learning_watchers_payload = V64_LEARNING_ENGINE.watchers()
    learning_watchers = learning_watchers_payload.get("watchers", [])
    learning_trades_payload = V64_LEARNING_ENGINE.trades(limit=100)
    learning_trades = learning_trades_payload.get("trades", [])

    model_forward_evidence = (
        _v669_model_forward_evidence(
            starting_balance=
                starting_balance
        )
    )

    # V6.6.7: cached broker truth is maintained by the always-on mirror.
    # Do not make an extra IG REST call on every mobile dashboard poll.
    ig_demo_status = IG_DEMO_MIRROR.status()
    ig_demo_mirrors = [
        dict(item)
        for item in ig_demo_status.get("mirrors", [])
        if isinstance(item, dict)
    ]
    ig_demo_open = int(
        ig_demo_status.get("open_broker_positions")
        or 0
    )

    active_statuses = {
        "WATCHING",
        "READY",
        "RISK_BLOCKED",
        "OPEN",
    }

    terminal_statuses = {
        "WIN",
        "LOSS",
        "EXPIRED",
        "INVALIDATED",
        "SUPERSEDED",
    }

    active = [
        item
        for item in watchers
        if item.get(
            "status"
        )
        in active_statuses
    ]

    open_watchers = [
        item
        for item in watchers
        if item.get(
            "status"
        )
        == "OPEN"
    ]

    watching = [
        item
        for item in watchers
        if item.get(
            "status"
        )
        in {
            "WATCHING",
            "READY",
            "RISK_BLOCKED",
        }
    ]

    completed = [
        item
        for item in watchers
        if item.get(
            "status"
        )
        in terminal_statuses
    ]

    def lifecycle_rank(
        item: dict,
    ) -> tuple:
        status = str(
            item.get(
                "status",
                "",
            )
        )

        priority = {
            "OPEN": 100,
            "READY": 90,
            "WATCHING": 80,
            "RISK_BLOCKED": 70,
            "WIN": 60,
            "LOSS": 55,
            "EXPIRED": 40,
            "INVALIDATED": 30,
            "SUPERSEDED": 20,
        }.get(
            status,
            0,
        )

        timestamp = float(
            item.get(
                "created_at",
                0.0,
            )
            or 0.0
        )

        return (
            priority,
            timestamp,
        )

    lifecycle = sorted(
        [
            dict(item)
            for item in watchers
        ],
        key=lifecycle_rank,
        reverse=True,
    )

    # --------------------------------------------------------
    # V6.7 MOBILE PAPER TRADE JOURNAL
    # --------------------------------------------------------
    # Only actual opened/settled PAPER trades are included here.
    # WATCHING/EXPIRED candidates are intentionally excluded so
    # the phone clearly separates "candidate" from "trade".
    now_ts = time.time()

    paper_trade_rows = []

    for item in watchers:
        status = str(
            item.get("status")
            or ""
        ).upper()

        trade_id = item.get(
            "trade_id"
        )

        if (
            not trade_id
            and status
            not in {
                "OPEN",
                "WIN",
                "LOSS",
            }
        ):
            continue

        snapshot = (
            item.get(
                "entry_snapshot"
            )
            or {}
        )

        entry_confidence = (
            snapshot.get(
                "live_confidence"
            )
        )

        if entry_confidence is None:
            entry_confidence = (
                (
                    item.get(
                        "last_live_signal"
                    )
                    or {}
                ).get(
                    "confidence"
                )
            )

        due_at = (
            item.get(
                "settlement_due_at"
            )
            or item.get(
                "target_exit_at"
            )
        )

        remaining_minutes = None

        if (
            status == "OPEN"
            and due_at is not None
        ):
            try:
                remaining_minutes = max(
                    0.0,
                    (
                        float(
                            due_at
                        )
                        - now_ts
                    )
                    / 60.0,
                )
            except Exception:
                remaining_minutes = None

        paper_trade_rows.append({
            "trade_id":
                trade_id,
            "watcher_id":
                item.get(
                    "watcher_id"
                ),
            "market":
                item.get(
                    "market"
                ),
            "symbol":
                item.get(
                    "symbol"
                ),
            "direction":
                item.get(
                    "direction"
                ),
            "status":
                status,
            "entry_path":
                item.get(
                    "entry_path"
                ),
            "entry_confidence":
                entry_confidence,
            "entry_price":
                (
                    item.get(
                        "entry_price_effective"
                    )
                    or item.get(
                        "entry_price"
                    )
                ),
            "entry_time":
                item.get(
                    "entry_time"
                ),
            "entry_time_iso":
                item.get(
                    "entry_time_iso"
                ),
            "settlement_due_at":
                due_at,
            "target_exit_at_iso":
                item.get(
                    "target_exit_at_iso"
                ),
            "remaining_minutes":
                (
                    round(
                        remaining_minutes,
                        1,
                    )
                    if remaining_minutes
                    is not None
                    else None
                ),
            "exit_price":
                (
                    item.get(
                        "exit_price_effective"
                    )
                    or item.get(
                        "exit_price"
                    )
                ),
            "closed_at":
                item.get(
                    "closed_at"
                ),
            "closed_at_iso":
                item.get(
                    "closed_at_iso"
                ),
            "result":
                item.get(
                    "result"
                ),
            "pnl":
                item.get(
                    "pnl"
                ),
            "stake":
                snapshot.get(
                    "stake"
                ),
            "payout":
                item.get(
                    "payout"
                ),
            "historical_win_rate":
                snapshot.get(
                    "historical_win_rate"
                ),
            "historical_trades":
                snapshot.get(
                    "historical_trades"
                ),
            "historical_profit_factor":
                snapshot.get(
                    "historical_profit_factor"
                ),
            "live_ai_up":
                snapshot.get(
                    "live_ai_up"
                ),
            "live_rsi":
                snapshot.get(
                    "live_rsi"
                ),
            "adaptive_gate":
                item.get(
                    "adaptive_gate"
                ),
            "v66_forward_gate":
                item.get(
                    "v66_forward_gate"
                ),
            "v66_portfolio_gate":
                item.get(
                    "v66_portfolio_gate"
                ),
            "forward_protocol":
                item.get(
                    "forward_protocol"
                ),
        })

    # V6.6 learning-engine PAPER trades are genuine forward PAPER entries too.
    for trade in learning_trades:
        status = str(trade.get("status") or "").upper()
        if status not in {"OPEN", "CLOSED"}:
            continue
        paper_trade_rows.append({
            "source": "V66_LEARNING_ENGINE",
            "trade_id": trade.get("trade_id"),
            "watcher_id": None,
            "market": trade.get("market"),
            "symbol": trade.get("symbol"),
            "direction": trade.get("direction"),
            "status": "OPEN" if status == "OPEN" else str(trade.get("result") or "CLOSED"),
            "entry_path": trade.get("entry_class"),
            "historical_grade": trade.get("historical_grade"),
            "entry_confidence": trade.get("quant_confidence"),
            "model_ai_confidence": trade.get("model_ai_confidence"),
            "entry_price": trade.get("entry_price"),
            "entry_time": trade.get("opened_at"),
            "settlement_due_at": trade.get("scheduled_close_at"),
            "exit_price": trade.get("exit_price"),
            "closed_at": trade.get("closed_at"),
            "result": trade.get("result"),
            "pnl": trade.get("pnl"),
            "stake": trade.get("stake"),
            "paper_only": True,
        })

    # Broker-reconciled IG DEMO rows survive app restarts and backend
    # process restarts (when state is stored on /var/data). They are appended
    # only when the internal learning row is absent, preventing duplicates.
    existing_trade_ids = {
        str(row.get("trade_id"))
        for row in paper_trade_rows
        if row.get("trade_id") is not None
    }

    for mirror in ig_demo_mirrors:
        deal_id = mirror.get("ig_deal_id")
        if not deal_id:
            continue

        trade_id = str(
            mirror.get("trade_id")
            or f"IG_{deal_id}"
        )
        if trade_id in existing_trade_ids:
            # Enrich the internal row with the actual broker deal.
            for row in paper_trade_rows:
                if str(row.get("trade_id")) == trade_id:
                    row["broker"] = "IG"
                    row["broker_environment"] = "DEMO"
                    row["ig_deal_id"] = deal_id
                    row["ig_deal_reference"] = mirror.get(
                        "ig_deal_reference"
                    )
                    row["ig_size"] = mirror.get("ig_size")
                    row["broker_status"] = mirror.get(
                        "broker_status"
                    )
                    row["broker_entry_level"] = mirror.get(
                        "broker_entry_level"
                    )
                    row["broker_exit_level"] = mirror.get(
                        "broker_exit_level"
                    )
                    break
            continue

        broker_status = str(
            mirror.get("broker_status")
            or ""
        ).upper()
        if broker_status not in {
            "OPEN",
            "CLOSED",
            "CLOSED_EXTERNALLY",
        }:
            continue

        result = str(
            mirror.get("broker_result")
            or (
                "OPEN"
                if broker_status == "OPEN"
                else "CLOSED"
            )
        ).upper()

        paper_trade_rows.append({
            "source": "IG_DEMO_BROKER",
            "trade_id": trade_id,
            "watcher_id": None,
            "market": (
                mirror.get("market")
                or mirror.get("symbol")
            ),
            "symbol": mirror.get("symbol"),
            "direction": mirror.get("direction"),
            "status": (
                "OPEN"
                if broker_status == "OPEN"
                else result
            ),
            "entry_path": (
                mirror.get("entry_class")
                or "IG_RECOVERED"
            ),
            "historical_grade": "IG_DEMO",
            "entry_confidence": None,
            "model_ai_confidence": mirror.get(
                "model_ai_confidence"
            ),
            "entry_price": (
                mirror.get("broker_entry_level")
                or mirror.get(
                    "internal_entry_price"
                )
            ),
            "entry_time": (
                mirror.get("opened_at")
                or mirror.get("created_at")
            ),
            "settlement_due_at": mirror.get(
                "scheduled_close_at"
            ),
            "exit_price": mirror.get(
                "broker_exit_level"
            ),
            "closed_at": mirror.get("closed_at"),
            "result": mirror.get("broker_result"),
            "pnl": mirror.get("internal_pnl"),
            "stake": mirror.get("ig_size"),
            "paper_only": False,
            "demo_only": True,
            "broker": "IG",
            "broker_environment": "DEMO",
            "ig_deal_id": deal_id,
            "ig_deal_reference": mirror.get(
                "ig_deal_reference"
            ),
            "ig_size": mirror.get("ig_size"),
            "broker_status": broker_status,
            "recovered_from_ig": bool(
                mirror.get("recovered_from_ig")
            ),
            "live_money_execution": False,
        })
        existing_trade_ids.add(trade_id)

    paper_trade_rows.sort(
        key=lambda row: float(
            row.get(
                "entry_time"
            )
            or 0.0
        ),
        reverse=True,
    )

    v66_forward = (
        V66_INTELLIGENCE
        .forward_performance(
            watchers
        )
    )

    return {
        "version":
            "6.1.0",
        "auto_mode":
            bool(
                manager.get(
                    "enabled",
                    False,
                )
            ),
        "manager":
            manager,
        "summary": {
            "active_watchers":
                max(len(active), int(learning_status.get("active_watchers", 0) or 0)),
            "watching":
                len(watching),
            "open_trades":
                max(
                    len(open_watchers)
                    + int(
                        learning_status.get(
                            "open_trades",
                            0,
                        )
                        or 0
                    ),
                    ig_demo_open,
                ),
            "ig_demo_open_positions":
                ig_demo_open,
            "ig_demo_sync_state":
                ig_demo_status.get("sync_state"),
            "completed_watchers":
                len(completed),
            "target_active_watchers":
                int(
                    manager.get(
                        "target_active_watchers",
                        3,
                    )
                    or 3
                ),
            "next_scan_at":
                manager.get(
                    "next_run_at"
                ),
            "last_scan_at":
                manager.get(
                    "last_run_at"
                ),
            "current_stage":
                manager.get(
                    "progress_stage",
                    "IDLE",
                ),
            "current_message":
                manager.get(
                    "progress_message",
                    "Waiting",
                ),
            "current_candidate":
                manager.get(
                    "progress_candidate"
                ),
            "progress_percent":
                int(
                    manager.get(
                        "progress_percent",
                        0,
                    )
                    or 0
                ),
        },
        "forward":
            forward,
        "model_forward_evidence":
            model_forward_evidence,
        "learning": learning_status,
        "learning_watchers": learning_watchers[:20],
        "ig_demo": ig_demo_status,
        "ig_demo_performance":
            dict(
                ig_demo_status.get(
                    "broker_performance"
                )
                or {}
            ),
        "compound": (
            COMPOUND_ENGINE.status()
            if "COMPOUND_ENGINE" in globals()
            else {
                "status": "STARTING",
                "environment": "DEMO",
                "live_money_execution": False,
            }
        ),
        "paper_trades":
            paper_trade_rows[:50],
        "v66_forward_intelligence":
            v66_forward,
        "strategy_health": [
            {
                "market": item.get("market"),
                "symbol": item.get("symbol"),
                "direction": item.get("direction"),
                **_v56_forward_health(
                    symbol=str(item.get("symbol") or ""),
                    direction=str(item.get("direction") or ""),
                    payout=float(item.get("payout") or 0.80),
                ),
            }
            for item in active
        ],
        "open_positions":
            open_watchers,
        "watching_positions":
            watching,
        "lifecycle":
            lifecycle[:20],
        "forward_protocol":
            "V6_GENUINE_FORWARD",
        "live_execution":
            False,
    }


@app.get("/forward-journal")
def get_forward_journal(
    limit: int = 100,
):
    """Immutable-observation view of V5.7 genuine forward entries/outcomes."""
    return V53_WATCHER_ENGINE.forward_journal(
        limit=limit
    )


def _v669_model_forward_evidence(
    starting_balance: float = 10000.0,
) -> dict:
    """Forward evidence for the model that is actually creating IG DEMO trades.

    The legacy V53 forward engine is retained separately. V6.6.9 counts
    V64/AI-learning entries as soon as they are opened, while win-rate/P&L
    remain based only on settled outcomes.

    Broker-recovered positions with no surviving model metadata are *not*
    silently attributed to the model. They remain broker evidence until
    a model-linked trade_id/metadata exists.
    """
    learning_status = V64_LEARNING_ENGINE.status()
    learning_payload = V64_LEARNING_ENGINE.trades(
        limit=2000
    )
    learning_rows = [
        dict(item)
        for item in learning_payload.get(
            "trades",
            [],
        )
        if isinstance(item, dict)
    ]

    mirrors_payload = IG_DEMO_MIRROR.status()
    mirrors = [
        dict(item)
        for item in mirrors_payload.get(
            "mirrors",
            [],
        )
        if isinstance(item, dict)
    ]

    mirror_by_trade_id = {
        str(item.get("trade_id")): item
        for item in mirrors
        if item.get("trade_id") is not None
    }

    # Actual model-generated entries: OPEN + CLOSED learning trades.
    actual_rows = []
    for trade in learning_rows:
        status = str(
            trade.get("status")
            or ""
        ).upper()

        if status not in {
            "OPEN",
            "CLOSED",
        }:
            continue

        row = dict(trade)
        trade_id = str(
            row.get("trade_id")
            or ""
        )
        mirror = mirror_by_trade_id.get(
            trade_id
        )

        if mirror:
            row["broker_linked"] = bool(
                mirror.get("ig_deal_id")
            )
            row["ig_deal_id"] = mirror.get(
                "ig_deal_id"
            )
            row["broker_status"] = mirror.get(
                "broker_status"
            )
            row["broker_result"] = mirror.get(
                "broker_result"
            )

            # If the broker has a definitive result, retain it as a second
            # evidence field without overwriting the internal PAPER result.
            row["broker_outcome_available"] = (
                str(
                    mirror.get(
                        "broker_result"
                    )
                    or ""
                ).upper()
                in {
                    "WIN",
                    "LOSS",
                }
            )
        else:
            row["broker_linked"] = False
            row["broker_outcome_available"] = False

        actual_rows.append(row)

    open_rows = [
        row
        for row in actual_rows
        if str(
            row.get("status")
            or ""
        ).upper() == "OPEN"
    ]
    settled_rows = [
        row
        for row in actual_rows
        if str(
            row.get("status")
            or ""
        ).upper() == "CLOSED"
        and str(
            row.get("result")
            or ""
        ).upper()
        in {
            "WIN",
            "LOSS",
        }
    ]

    wins = sum(
        1
        for row in settled_rows
        if str(
            row.get("result")
            or ""
        ).upper() == "WIN"
    )
    losses = len(
        settled_rows
    ) - wins

    total_pnl = round(
        sum(
            float(
                row.get("pnl")
                or 0.0
            )
            for row in settled_rows
        ),
        2,
    )

    broker_matched = sum(
        1
        for row in actual_rows
        if row.get(
            "broker_linked"
        )
    )

    recovered_unattributed = sum(
        1
        for mirror in mirrors
        if mirror.get(
            "ig_deal_id"
        )
        and bool(
            mirror.get(
                "recovered_from_ig"
            )
        )
        and str(
            mirror.get(
                "entry_class"
            )
            or ""
        ).upper()
        == "IG_RECOVERED"
    )

    by_entry_class = {}
    for row in actual_rows:
        cls = str(
            row.get(
                "entry_class"
            )
            or "UNKNOWN"
        ).upper()
        bucket = by_entry_class.setdefault(
            cls,
            {
                "entries": 0,
                "open": 0,
                "settled": 0,
                "wins": 0,
                "losses": 0,
            },
        )
        bucket["entries"] += 1

        if str(
            row.get("status")
            or ""
        ).upper() == "OPEN":
            bucket["open"] += 1
        elif str(
            row.get("result")
            or ""
        ).upper() in {
            "WIN",
            "LOSS",
        }:
            bucket["settled"] += 1
            if str(
                row.get(
                    "result"
                )
                or ""
            ).upper() == "WIN":
                bucket["wins"] += 1
            else:
                bucket["losses"] += 1

    for bucket in by_entry_class.values():
        bucket["win_rate_pct"] = (
            round(
                bucket["wins"]
                / bucket["settled"]
                * 100.0,
                2,
            )
            if bucket["settled"]
            else 0.0
        )

    legacy = V53_WATCHER_ENGINE.forward_stats(
        starting_balance=
            starting_balance
    )

    return {
        "version":
            "6.6.9",
        "source":
            "V64_AI_LEARNING_FORWARD_EVIDENCE",
        "entries":
            len(actual_rows),
        # Compatibility aliases used by existing mobile code.
        "forward_trades":
            len(actual_rows),
        "trades":
            len(actual_rows),
        "open_entries":
            len(open_rows),
        "settled_entries":
            len(settled_rows),
        "wins":
            wins,
        "losses":
            losses,
        "win_rate_pct":
            (
                round(
                    wins
                    / len(settled_rows)
                    * 100.0,
                    2,
                )
                if settled_rows
                else 0.0
            ),
        "total_pnl":
            total_pnl,
        "paper_balance":
            learning_status.get(
                "paper_balance"
            ),
        "starting_balance":
            learning_status.get(
                "starting_balance"
            ),
        "broker_matched_entries":
            broker_matched,
        "broker_recovered_unattributed":
            recovered_unattributed,
        "by_entry_class":
            by_entry_class,
        "confidence_buckets":
            learning_status.get(
                "confidence_buckets"
            )
            or {},
        "rows":
            actual_rows[:100],
        "legacy_v53_forward":
            legacy,
        "note":
            (
                "Entries count immediately when the V64/AI-learning model "
                "opens them. W/L, win rate and Model P&L populate only after "
                "those model entries settle. IG_RECOVERED positions without "
                "surviving model metadata remain broker evidence only."
            ),
        "live_money_execution":
            False,
        "demo_only":
            True,
    }


@app.get("/model-forward-evidence")
def get_model_forward_evidence(
    starting_balance: float = 10000.0,
):
    validate_balance(
        starting_balance
    )

    return _v669_model_forward_evidence(
        starting_balance=
            starting_balance
    )


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



# ============================================================
# V6.5 PAPER LEARNING THRESHOLD POLICY
# ============================================================

@app.get("/v65/threshold-policy")
def v65_threshold_policy():
    return {
        "version": "6.6.0",
        "normal_min_confidence": PAPER_NORMAL_MIN_CONFIDENCE,
        "normal_min_confidence_pct": PAPER_NORMAL_MIN_CONFIDENCE * 100.0,
        "ai_min_confidence": PAPER_AI_MIN_CONFIDENCE,
        "ai_min_confidence_pct": PAPER_AI_MIN_CONFIDENCE * 100.0,
                "entry_rule": (
            "VERIFIED setup may enter PAPER through normal confidence >=30% "
            "with live direction agreement OR AI confidence >=40% with "
            "AI approval and direction agreement."
        ),
        "shadow_learning": True,
        "paper_only": True,
        "broker_execution_enabled": False,
        "live_execution": False,
    }


# ============================================================
# V6.4 HIGH-THROUGHPUT PAPER LEARNING API
# ============================================================

@app.get("/v64/learning-status")
def v64_learning_status():
    return V64_LEARNING_ENGINE.status()


@app.get("/v64/learning-watchers")
def v64_learning_watchers():
    return V64_LEARNING_ENGINE.watchers()


@app.get("/v64/learning-journal")
def v64_learning_journal(limit: int = 200):
    return V64_LEARNING_ENGINE.journal(limit=limit)


@app.post("/v64/learning/run-now")
def v64_learning_run_now():
    return V64_LEARNING_ENGINE.tick()


@app.post("/v64/learning/enable")
def v64_learning_enable():
    return V64_LEARNING_ENGINE.enable()


@app.post("/v64/learning/pause")
def v64_learning_pause():
    return V64_LEARNING_ENGINE.pause()


# ============================================================
# V6.6 DIRECT AUTONOMOUS AI40 PAPER LEARNING API
# ============================================================

_AI_LEARNING_RUN_LOCK = threading.Lock()


def _ai_learning_candidate_direction(candidate: dict) -> str:
    return str(
        candidate.get("direction")
        or candidate.get("signal")
        or candidate.get("decision")
        or candidate.get("side")
        or "WAIT"
    ).upper().strip()


def _ai_learning_live_direction(live: dict) -> str:
    return str(
        live.get("direction")
        or live.get("signal")
        or live.get("decision")
        or "WAIT"
    ).upper().strip()


def _ai_learning_candidate_score(candidate: dict) -> float:
    for key in (
        "adaptive_rank_score",
        "smart_fast_score",
        "fast_score",
        "score",
        "market_score",
    ):
        try:
            value = candidate.get(key)
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            pass
    return 0.0


def _ai_learning_call_with_timeout(
    func,
    *args,
    timeout_seconds: float = 25.0,
):
    """Run a read/analysis function with a hard request-side timeout.

    The worker is daemonised. A timeout does not open or mutate a trade;
    it only abandons that market evaluation so /ai-learning/run-now can
    always release its lock and return a diagnostic response.
    """
    box = {
        "done": False,
        "result": None,
        "error": None,
    }

    def worker():
        try:
            box["result"] = func(*args)
        except Exception as exc:
            box["error"] = exc
        finally:
            box["done"] = True

    thread = threading.Thread(
        target=worker,
        name="jasong-ai-learning-eval",
        daemon=True,
    )
    thread.start()
    thread.join(timeout=max(1.0, float(timeout_seconds)))

    if thread.is_alive():
        return {
            "ok": False,
            "timeout": True,
            "result": None,
            "error": (
                f"Timed out after {timeout_seconds:.0f}s"
            ),
        }

    if box["error"] is not None:
        exc = box["error"]
        return {
            "ok": False,
            "timeout": False,
            "result": None,
            "error": (
                f"{type(exc).__name__}: {exc}"
            ),
        }

    return {
        "ok": True,
        "timeout": False,
        "result": box["result"],
        "error": None,
    }


def _v66_ai_learning_cycle() -> dict:
    """
    Immediate autonomous PAPER-only AI40 evaluation of the learning watchers.

    V6.6.1 FIX:
    - Uses the existing V64 learning watchers already being fed by Auto Manager.
    - Does NOT run another full FX discovery scan inside this request.
    - Does NOT call V64.tick() synchronously.
    - Each live-signal evaluation has a hard timeout.
    - Opens at most one PAPER trade.
    - Normal/N30 path remains disabled for this AI-learning experiment.
    - Broker/live execution remains disabled.
    """

    cycle_started_at = time.time()

    if not _AI_LEARNING_RUN_LOCK.acquire(blocking=False):
        return {
            "status": "AI_LEARNING_BUSY",
            "paper_only": True,
            "live_execution": False,
            "message": (
                "Another bounded AI-learning evaluation is still running. "
                "This version will release automatically after its watcher "
                "evaluations finish or time out."
            ),
        }

    try:
        V64_LEARNING_ENGINE.enable()

        current = V64_LEARNING_ENGINE.status()
        open_count = int(
            current.get("open_trades", 0)
            or 0
        )

        if open_count >= 1:
            return {
                "status": "WAIT_EXISTING_TRADE",
                "paper_only": True,
                "live_execution": False,
                "open_trades": open_count,
                "learning": current,
            }

        balance = float(
            current.get("paper_balance", 10000.0)
            or 10000.0
        )

        watcher_payload = V64_LEARNING_ENGINE.watchers()
        watchers = (
            watcher_payload.get("watchers", [])
            if isinstance(watcher_payload, dict)
            else []
        )

        watchers = [
            dict(item)
            for item in watchers
            if isinstance(item, dict)
        ][:6]

        if not watchers:
            return {
                "status": "NO_LEARNING_WATCHERS",
                "paper_only": True,
                "live_execution": False,
                "watchers": 0,
                "message": (
                    "Auto Manager has not supplied a candidate to the "
                    "AI-learning engine yet."
                ),
                "learning": current,
            }

        evaluated = []
        qualified = []

        for rank, watcher in enumerate(watchers, start=1):
            symbol = str(
                watcher.get("symbol")
                or ""
            ).strip()

            market = str(
                watcher.get("market")
                or symbol
            ).strip()

            wanted = str(
                watcher.get("direction")
                or ""
            ).upper().strip()

            row = {
                "rank": rank,
                "watcher_id": watcher.get("watcher_id"),
                "market": market,
                "symbol": symbol,
                "candidate_direction": wanted,
                "deep_status": watcher.get("deep_status"),
                "verified": bool(watcher.get("verified")),
                "experimental": bool(watcher.get("experimental")),
                "live_direction": None,
                "quant_confidence_pct": None,
                "model_ai_directional_confidence_pct": None,
                "direction_match": False,
                "ai40_pass": False,
                "entry_class": None,
                "passed": False,
                "timed_out": False,
                "reason": None,
            }

            if not symbol or wanted not in {"BUY", "SELL"}:
                row["reason"] = "Watcher has no valid BUY/SELL symbol."
                evaluated.append(row)
                continue

            # When IG DEMO autotrade is enabled, broker availability becomes
            # a hard execution preflight. This prevents the AI from opening
            # an internal learning trade that cannot be mirrored to IG DEMO.
            ig_preflight = IG_DEMO_MIRROR.preflight_symbol(
                symbol
            )
            row["ig_demo_preflight"] = ig_preflight
            if (
                ig_preflight.get("required")
                and not ig_preflight.get("ok")
            ):
                row["reason"] = (
                    "IG DEMO market unavailable: "
                    f"{ig_preflight.get('reason')}"
                )
                evaluated.append(row)
                continue

            live_result = _ai_learning_call_with_timeout(
                _v63_adaptive_live_signal,
                symbol,
                watcher.get("risk_mode", "Balanced"),
                balance,
                timeout_seconds=25.0,
            )

            if not live_result["ok"]:
                row["timed_out"] = bool(
                    live_result["timeout"]
                )
                row["reason"] = (
                    "Live signal timeout"
                    if live_result["timeout"]
                    else f"Live signal error: {live_result['error']}"
                )
                evaluated.append(row)
                continue

            live = dict(
                live_result["result"]
                or {}
            )

            live_direction = _ai_learning_live_direction(
                live
            )

            quant = V64_LEARNING_ENGINE._confidence01(
                live.get("confidence")
            )

            model_ai = (
                V64_LEARNING_ENGINE
                ._directional_model_ai_confidence(
                    live,
                    wanted,
                )
            )

            # ---------------------------------------------------------
            # V6.6.2 AI-LEARNING SHADOW PROMOTION
            #
            # Normal production logic intentionally keeps NO_TRADE
            # candidates as SHADOW_WATCH and trade_eligible=False.
            #
            # For the PAPER-ONLY AI-learning experiment we allow a
            # high-quality Auto Manager shadow candidate to be evaluated
            # as an EXPERIMENTAL watcher, but only inside this request.
            #
            # The stored production watcher is NOT mutated and the normal
            # verified/watch pipeline is NOT weakened.
            # ---------------------------------------------------------
            candidate_meta = (
                watcher.get("candidate")
                if isinstance(
                    watcher.get("candidate"),
                    dict,
                )
                else {}
            )

            smart_score = _ai_learning_candidate_score(
                candidate_meta
            )

            quality_tier = str(
                candidate_meta.get("quality_tier")
                or ""
            ).upper().strip()

            quarantined = bool(
                candidate_meta.get(
                    "strategy_quarantined",
                    False,
                )
            )

            source_trade_eligible = bool(
                watcher.get(
                    "trade_eligible",
                    False,
                )
            )

            source_deep_status = str(
                watcher.get(
                    "deep_status"
                )
                or ""
            ).upper().strip()

            shadow_promotable = bool(
                (not source_trade_eligible)
                and (not quarantined)
                and source_deep_status
                    in {
                        "NO_TRADE",
                        "NOT_VERIFIED",
                        "WATCH",
                        "NEAR_VERIFIED",
                    }
                and (
                    quality_tier
                    in {
                        "A+",
                        "A",
                    }
                    or smart_score
                    >= 90.0
                )
            )

            evaluation_watcher = dict(
                watcher
            )

            if shadow_promotable:
                evaluation_watcher[
                    "experimental"
                ] = True
                evaluation_watcher[
                    "trade_eligible"
                ] = True
                evaluation_watcher[
                    "deep_status"
                ] = (
                    "AI_LEARNING_SHADOW_PROMOTION"
                )

            # Use V64's own classifier. NORMAL_MIN_CONFIDENCE is
            # deliberately disabled for this AI-learning engine, so a
            # promoted shadow can open only through model-AI >= 40%
            # with matching live direction. That produces EM.
            decision = V64_LEARNING_ENGINE._entry_class(
                evaluation_watcher,
                live,
            )

            direction_match = (
                live_direction == wanted
            )
            ai40_pass = (
                model_ai
                >= PAPER_AI_MIN_CONFIDENCE
            )

            entry_class = str(
                decision.get("class")
                or "S"
            ).upper()

            # AI-learning experiment accepts only M/EM.
            passed = bool(
                decision.get("enter")
                and entry_class in {"M", "EM"}
                and direction_match
                and ai40_pass
            )

            row.update({
                "live_direction": live_direction,
                "quant_confidence_pct": round(
                    quant * 100.0,
                    2,
                ),
                "model_ai_directional_confidence_pct": round(
                    model_ai * 100.0,
                    2,
                ),
                "direction_match": direction_match,
                "ai40_pass": ai40_pass,
                "entry_class": entry_class,
                "source_trade_eligible":
                    source_trade_eligible,
                "source_deep_status":
                    source_deep_status,
                "quality_tier":
                    quality_tier,
                "smart_fast_score":
                    smart_score,
                "shadow_promotable":
                    shadow_promotable,
                "shadow_promoted_for_ai_learning":
                    bool(
                        shadow_promotable
                        and entry_class
                        in {
                            "EM",
                            "M",
                        }
                    ),
                "passed": passed,
                "reason": decision.get("reason"),
            })

            evaluated.append(row)

            if passed:
                qualified.append({
                    "watcher":
                        evaluation_watcher,
                    "source_watcher":
                        watcher,
                    "live":
                        live,
                    "decision":
                        decision,
                    "row":
                        row,
                    "model_ai":
                        model_ai,
                    "quant":
                        quant,
                })

        if not qualified:
            return {
                "status": "NO_AI40_SETUP",
                "paper_only": True,
                "live_execution": False,
                "watchers_evaluated": len(evaluated),
                "qualified": 0,
                "elapsed_seconds": round(
                    time.time() - cycle_started_at,
                    2,
                ),
                "evaluated": evaluated,
                "learning": V64_LEARNING_ENGINE.status(),
            }

        qualified.sort(
            key=lambda item: (
                float(item["model_ai"]),
                float(item["quant"]),
            ),
            reverse=True,
        )

        selected = qualified[0]
        watcher = selected["watcher"]
        live = selected["live"]
        decision = selected["decision"]

        before = V64_LEARNING_ENGINE.trades(
            limit=200
        )
        before_rows = (
            before.get("trades", [])
            if isinstance(before, dict)
            else []
        )
        before_ids = {
            str(item.get("trade_id"))
            for item in before_rows
            if isinstance(item, dict)
            and item.get("trade_id") is not None
        }

        # Open directly through the existing PAPER engine. This preserves
        # its stake sizing, duplicate protection, max-open limit, journal
        # and settlement format without invoking another market-data tick.
        V64_LEARNING_ENGINE._open_trade(
            watcher,
            live,
            decision,
        )
        V64_LEARNING_ENGINE._persist()

        after = V64_LEARNING_ENGINE.trades(
            limit=200
        )
        after_rows = (
            after.get("trades", [])
            if isinstance(after, dict)
            else []
        )

        new_trades = [
            item
            for item in after_rows
            if isinstance(item, dict)
            and str(item.get("trade_id")) not in before_ids
        ]

        ai_trades = [
            item
            for item in new_trades
            if str(
                item.get("entry_class")
                or ""
            ).upper()
            in {"M", "EM"}
        ]

        if ai_trades:
            return {
                "status": "PAPER_TRADE_OPENED",
                "paper_only": True,
                "live_execution": False,
                "selected": selected["row"],
                "trade": ai_trades[0],
                "elapsed_seconds": round(
                    time.time() - cycle_started_at,
                    2,
                ),
                "evaluated": evaluated,
                "learning": V64_LEARNING_ENGINE.status(),
            }

        return {
            "status": "ENTRY_NOT_CREATED",
            "paper_only": True,
            "live_execution": False,
            "selected": selected["row"],
            "elapsed_seconds": round(
                time.time() - cycle_started_at,
                2,
            ),
            "evaluated": evaluated,
            "message": (
                "The watcher passed AI40 but the V64 PAPER engine did not "
                "create a new trade, usually because of duplicate/open-limit "
                "protection."
            ),
            "learning": V64_LEARNING_ENGINE.status(),
        }

    except Exception as exc:
        return {
            "status": "ERROR",
            "paper_only": True,
            "live_execution": False,
            "elapsed_seconds": round(
                time.time() - cycle_started_at,
                2,
            ),
            "error": (
                f"{type(exc).__name__}: {exc}"
            ),
        }

    finally:
        _AI_LEARNING_RUN_LOCK.release()


# ============================================================
# V6.7.1 UNIFIED INTELLIGENCE + ELITE COMPOUND ENGINE
# ============================================================

def _v671_compound_candidate_source(
    cycle_capital: float,
) -> list[dict]:
    """Unified candidate feed for Elite Compound.

    V6.7.1 deliberately joins two information paths:

      1. the persistent V64/deep-validated watcher pool; and
      2. the exact recent Live Intelligence observations shown on Home.

    A Home signal does NOT bypass deep validation. If it has no matching
    deep-validated watcher yet, the row is still exposed to Compound with
    PENDING_DEEP_VALIDATION so the mobile app can show the exact reason it is
    not yet tradeable. The existing signal bridge queues Auto Manager to build
    that missing evidence.

    When a recent Home observation matches a watcher, Compound uses that same
    direction/AI/Quant observation instead of immediately recomputing a
    different signal. If no recent Home observation exists, Compound remains
    autonomous and obtains a fresh server-side signal.
    """
    watcher_payload = (
        V64_LEARNING_ENGINE.watchers()
    )
    watchers = (
        watcher_payload.get(
            "watchers",
            [],
        )
        if isinstance(
            watcher_payload,
            dict,
        )
        else []
    )
    watchers = [
        dict(item)
        for item in watchers
        if isinstance(
            item,
            dict,
        )
    ]

    recent = (
        _v671_recent_intelligence()
    )
    recent_by_key = {
        _v671_intelligence_key(
            str(row.get("symbol") or ""),
            str(row.get("direction") or ""),
        ): row
        for row in recent
        if str(
            row.get("direction")
            or ""
        ).upper()
        in {
            "BUY",
            "SELL",
        }
    }

    evaluated: list[dict] = []
    seen_keys: set[str] = set()

    balance = max(
        1.0,
        float(
            cycle_capital
            or 1.0
        ),
    )

    for rank, watcher in enumerate(
        watchers,
        start=1,
    ):
        symbol = str(
            watcher.get("symbol")
            or ""
        ).strip()
        wanted = str(
            watcher.get("direction")
            or ""
        ).upper().strip()
        market = str(
            watcher.get("market")
            or symbol
        ).strip()

        candidate_meta = (
            watcher.get("candidate")
            if isinstance(
                watcher.get(
                    "candidate"
                ),
                dict,
            )
            else {}
        )
        validated_meta = (
            watcher.get("validated")
            if isinstance(
                watcher.get(
                    "validated"
                ),
                dict,
            )
            else {}
        )

        smart_score = (
            _ai_learning_candidate_score(
                candidate_meta
            )
        )
        quality_tier = str(
            candidate_meta.get(
                "quality_tier"
            )
            or validated_meta.get(
                "quality_tier"
            )
            or ""
        ).upper().strip()
        deep_status = str(
            watcher.get("deep_status")
            or validated_meta.get(
                "status"
            )
            or ""
        ).upper().strip()

        key = (
            _v671_intelligence_key(
                symbol,
                wanted,
            )
        )
        seen_keys.add(
            key
        )

        row = {
            "rank": rank,
            "watcher_id":
                watcher.get(
                    "watcher_id"
                ),
            "market": market,
            "symbol": symbol,
            "direction": wanted,
            "quality_tier":
                quality_tier,
            "deep_status":
                deep_status,
            "smart_fast_score":
                smart_score,
            "verified":
                bool(
                    watcher.get(
                        "verified"
                    )
                ),
            "experimental":
                bool(
                    watcher.get(
                        "experimental"
                    )
                ),
            "trade_eligible":
                bool(
                    watcher.get(
                        "trade_eligible"
                    )
                ),
            "strategy_quarantined":
                bool(
                    candidate_meta.get(
                        "strategy_quarantined",
                        False,
                    )
                ),
            "live_direction":
                None,
            "direction_match":
                False,
            "quant_confidence":
                0.0,
            "model_ai_confidence":
                0.0,
            "intelligence_source":
                None,
            "intelligence_age_seconds":
                None,
            "reason":
                None,
        }

        if (
            not symbol
            or wanted
            not in {
                "BUY",
                "SELL",
            }
        ):
            row[
                "reason"
            ] = (
                "Watcher has no valid "
                "BUY/SELL symbol."
            )
            evaluated.append(
                row
            )
            continue

        if row[
            "strategy_quarantined"
        ]:
            row[
                "reason"
            ] = (
                "Existing strategy-health "
                "gate quarantined this "
                "candidate."
            )
            evaluated.append(
                row
            )
            continue

        shared = (
            recent_by_key.get(
                key
            )
        )

        if shared is not None:
            live_direction = str(
                shared.get(
                    "direction"
                )
                or ""
            ).upper()
            quant = (
                V64_LEARNING_ENGINE
                ._confidence01(
                    shared.get(
                        "quant_confidence"
                    )
                )
            )
            model_ai = (
                V64_LEARNING_ENGINE
                ._confidence01(
                    shared.get(
                        "model_ai_confidence"
                    )
                )
            )

            row.update({
                "live_direction":
                    live_direction,
                "direction_match":
                    (
                        live_direction
                        == wanted
                    ),
                "quant_confidence":
                    quant,
                "quant_confidence_pct":
                    round(
                        quant
                        * 100.0,
                        2,
                    ),
                "model_ai_confidence":
                    model_ai,
                "model_ai_directional_confidence_pct":
                    round(
                        model_ai
                        * 100.0,
                        2,
                    ),
                "live_price":
                    shared.get(
                        "price"
                    ),
                "rsi":
                    shared.get(
                        "rsi"
                    ),
                "signal_reason":
                    shared.get(
                        "reason"
                    ),
                "intelligence_source":
                    "UNIFIED_HOME_LIVE_INTELLIGENCE",
                "intelligence_age_seconds":
                    shared.get(
                        "age_seconds"
                    ),
                "reason":
                    shared.get(
                        "reason"
                    ),
            })
            evaluated.append(
                row
            )
            continue

        # No recent mobile/Home observation for this exact symbol+direction:
        # keep the system autonomous by evaluating it server-side.
        live_result = (
            _ai_learning_call_with_timeout(
                _v63_adaptive_live_signal,
                symbol,
                watcher.get(
                    "risk_mode",
                    "Balanced",
                ),
                balance,
                timeout_seconds=25.0,
            )
        )

        if not live_result.get(
            "ok"
        ):
            row[
                "intelligence_source"
            ] = (
                "SERVER_FRESH_SIGNAL"
            )
            row[
                "reason"
            ] = (
                "Live signal timeout"
                if live_result.get(
                    "timeout"
                )
                else (
                    "Live signal error: "
                    f"{live_result.get('error')}"
                )
            )
            evaluated.append(
                row
            )
            continue

        live = dict(
            live_result.get(
                "result"
            )
            or {}
        )
        live_direction = (
            _ai_learning_live_direction(
                live
            )
        )
        quant = (
            V64_LEARNING_ENGINE
            ._confidence01(
                live.get(
                    "confidence"
                )
            )
        )
        model_ai = (
            V64_LEARNING_ENGINE
            ._directional_model_ai_confidence(
                live,
                wanted,
            )
        )

        row.update({
            "live_direction":
                live_direction,
            "direction_match":
                (
                    live_direction
                    == wanted
                ),
            "quant_confidence":
                quant,
            "quant_confidence_pct":
                round(
                    quant
                    * 100.0,
                    2,
                ),
            "model_ai_confidence":
                model_ai,
            "model_ai_directional_confidence_pct":
                round(
                    model_ai
                    * 100.0,
                    2,
                ),
            "live_price":
                live.get(
                    "price"
                ),
            "rsi":
                live.get(
                    "rsi"
                ),
            "signal_reason":
                live.get(
                    "reason"
                ),
            "intelligence_source":
                "SERVER_FRESH_SIGNAL",
            "reason":
                live.get(
                    "reason"
                ),
        })
        evaluated.append(
            row
        )

    # Surface fresh Home signals that have not yet acquired a matching
    # deep-validated watcher. They cannot trade yet, but they are now visible
    # inside the Compound ranking instead of silently living on a separate UI.
    for shared in recent:
        direction = str(
            shared.get(
                "direction"
            )
            or ""
        ).upper()
        symbol = str(
            shared.get(
                "symbol"
            )
            or ""
        )
        key = (
            _v671_intelligence_key(
                symbol,
                direction,
            )
        )
        if (
            key in seen_keys
            or direction
            not in {
                "BUY",
                "SELL",
            }
        ):
            continue

        quant = (
            V64_LEARNING_ENGINE
            ._confidence01(
                shared.get(
                    "quant_confidence"
                )
            )
        )
        model_ai = (
            V64_LEARNING_ENGINE
            ._confidence01(
                shared.get(
                    "model_ai_confidence"
                )
            )
        )

        evaluated.append({
            "rank":
                len(
                    evaluated
                )
                + 1,
            "watcher_id":
                None,
            "market":
                symbol,
            "symbol":
                symbol,
            "direction":
                direction,
            "quality_tier":
                "",
            "deep_status":
                "PENDING_DEEP_VALIDATION",
            "smart_fast_score":
                0.0,
            "verified":
                False,
            "experimental":
                False,
            "trade_eligible":
                False,
            "strategy_quarantined":
                False,
            "live_direction":
                direction,
            "direction_match":
                True,
            "quant_confidence":
                quant,
            "quant_confidence_pct":
                round(
                    quant
                    * 100.0,
                    2,
                ),
            "model_ai_confidence":
                model_ai,
            "model_ai_directional_confidence_pct":
                round(
                    model_ai
                    * 100.0,
                    2,
                ),
            "live_price":
                shared.get(
                    "price"
                ),
            "rsi":
                shared.get(
                    "rsi"
                ),
            "signal_reason":
                shared.get(
                    "reason"
                ),
            "intelligence_source":
                "UNIFIED_HOME_LIVE_INTELLIGENCE",
            "intelligence_age_seconds":
                shared.get(
                    "age_seconds"
                ),
            "reason": (
                "Live Intelligence is connected, but this exact "
                "symbol/direction does not yet have a matching deep-validated "
                "V64 watcher. Auto Manager must complete Fast/quality/deep "
                "validation before Compound execution."
            ),
        })

    evaluated.sort(
        key=lambda row: (
            float(
                row.get(
                    "model_ai_confidence"
                )
                or 0.0
            ),
            float(
                row.get(
                    "quant_confidence"
                )
                or 0.0
            ),
            float(
                row.get(
                    "smart_fast_score"
                )
                or 0.0
            ),
        ),
        reverse=True,
    )
    return evaluated


COMPOUND_STATE_PATH = os.getenv(
    "COMPOUND_STATE_PATH",
    (
        "/var/data/jasong_compound_state.json"
        if os.path.isdir("/var/data")
        else "/tmp/jasong_compound_state.json"
    ),
)

COMPOUND_ENGINE = EliteCompoundEngine(
    broker=IG_DEMO_BROKER,
    candidate_source=
        _v671_compound_candidate_source,
    correlation_source=
        _v66_fx_correlation_matrix,
    state_path=COMPOUND_STATE_PATH,
)
COMPOUND_ENGINE.start_thread()


@app.get("/compound/status")
def compound_status():
    return COMPOUND_ENGINE.status()


@app.get("/compound/intelligence")
def compound_intelligence():
    """Inspect the shared Home ↔ Compound Live Intelligence bridge."""
    return {
        "version": "6.7.1",
        "recent_live_intelligence":
            _v671_recent_intelligence(),
        "compound":
            COMPOUND_ENGINE.status(),
        "environment": "DEMO",
        "live_money_execution": False,
    }


@app.get("/compound/rules")
def compound_rules():
    return COMPOUND_ENGINE.rules()


@app.get("/compound/history")
def compound_history(
    limit: int = 100,
):
    return COMPOUND_ENGINE.history(
        limit=limit
    )


@app.get("/compound/candidates")
def compound_candidates():
    return COMPOUND_ENGINE.candidates()


@app.get("/compound/positions")
def compound_positions():
    return COMPOUND_ENGINE.positions()


@app.post("/compound/start")
def compound_start(
    starting_capital: float,
    new_campaign: bool = True,
):
    """Start the Elite Compound strategy on IG DEMO only.

    Legacy JASONG_* broker entries are paused so that IG account P&L can be
    attributed cleanly to the compound basket. The existing Auto Manager,
    PAPER learning, model-forward evidence and SHADOW evidence keep running.
    """
    validate_balance(
        starting_capital
    )

    # Stop only NEW legacy broker entries. IGDemoMirror continues its thread,
    # reconciles existing JASONG_* positions and closes them when due.
    IG_DEMO_MIRROR.set_enabled(False)
    IG_DEMO_MIRROR.start()
    COMPOUND_ENGINE.mark_legacy_execution_paused(
        True
    )

    try:
        result = COMPOUND_ENGINE.start_campaign(
            starting_capital,
            new_campaign=new_campaign,
        )
    except (ValueError, IGDemoError) as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        )

    return {
        "status": "COMPOUND_START_REQUESTED",
        "compound": result,
        "legacy_ai_tracking_preserved": True,
        "legacy_ig_new_entries_paused": True,
        "ig_environment": "DEMO",
        "live_money_execution": False,
    }


@app.post("/compound/resume")
def compound_resume():
    IG_DEMO_MIRROR.set_enabled(False)
    IG_DEMO_MIRROR.start()
    COMPOUND_ENGINE.mark_legacy_execution_paused(
        True
    )
    try:
        result = COMPOUND_ENGINE.resume()
    except (ValueError, IGDemoError) as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        )
    return {
        "status": "COMPOUND_RESUMED",
        "compound": result,
        "legacy_ai_tracking_preserved": True,
        "legacy_ig_new_entries_paused": True,
        "ig_environment": "DEMO",
        "live_money_execution": False,
    }


@app.post("/compound/stop")
def compound_stop(
    close_now: bool = False,
    resume_legacy: bool = False,
):
    result = COMPOUND_ENGINE.stop(
        close_now=close_now
    )

    if resume_legacy:
        IG_DEMO_MIRROR.set_enabled(True)
        IG_DEMO_MIRROR.start()
        COMPOUND_ENGINE.mark_legacy_execution_paused(
            False
        )

    return {
        "status": (
            "COMPOUND_STOPPED"
            if close_now
            else "COMPOUND_NEW_CYCLES_STOPPED"
        ),
        "compound": result,
        "legacy_ig_new_entries_resumed": bool(
            resume_legacy
        ),
        "ig_environment": "DEMO",
        "live_money_execution": False,
    }


@app.post("/compound/run-now")
def compound_run_now():
    return COMPOUND_ENGINE.tick()



# ============================================================
# V6.6.4 OVERNIGHT IG DEMO CONTROL
# ============================================================

def _v664_overnight_demo_snapshot() -> dict:
    """Unified mobile status with IG DEMO as broker source of truth."""
    manager = V55_AUTO_MANAGER.status()
    learning = V64_LEARNING_ENGINE.status()

    # The app can be resumed after minutes or hours. Refresh actual IG broker
    # positions when our cached reconciliation is stale so the phone never has
    # to trust old in-memory counters.
    try:
        IG_DEMO_MIRROR.ensure_broker_fresh(
            max_age_seconds=25
        )
    except Exception:
        pass

    ig_demo = IG_DEMO_MIRROR.status()
    broker = dict(
        ig_demo.get("broker")
        or {}
    )

    environment = str(
        broker.get("environment")
        or ""
    ).upper()
    demo_only = bool(
        environment == "DEMO"
        and broker.get("demo_execution") is True
        and broker.get("live_money_execution") is not True
        and ig_demo.get("live_money_execution") is not True
        and learning.get("live_execution") is not True
    )

    manager_enabled = bool(
        manager.get("enabled")
    )
    learning_enabled = bool(
        learning.get("enabled")
    )
    mirror_enabled = bool(
        ig_demo.get("enabled")
    )
    phase_complete = bool(
        ig_demo.get("phase_complete")
    )
    internal_open = int(
        learning.get("open_trades")
        or 0
    )
    broker_open = int(
        ig_demo.get(
            "open_broker_positions"
        )
        or 0
    )

    if phase_complete:
        run_state = "PHASE_COMPLETE"
    elif (
        demo_only
        and manager_enabled
        and learning_enabled
        and mirror_enabled
    ):
        run_state = "ACTIVE"
    elif (
        (not manager_enabled)
        and (
            internal_open > 0
            or broker_open > 0
        )
    ):
        run_state = "DRAINING"
    else:
        run_state = "PAUSED"

    trade_payload = V64_LEARNING_ENGINE.trades(
        limit=50
    )
    trade_rows = [
        dict(item)
        for item in trade_payload.get(
            "trades",
            [],
        )
        if isinstance(item, dict)
    ]

    current_trade = next(
        (
            item
            for item in trade_rows
            if str(
                item.get("status")
                or ""
            ).upper() == "OPEN"
        ),
        None,
    )

    latest_settled = next(
        (
            item
            for item in trade_rows
            if str(
                item.get("status")
                or ""
            ).upper() == "CLOSED"
        ),
        None,
    )

    broker_positions = [
        dict(item)
        for item in ig_demo.get(
            "broker_positions",
            [],
        )
        if isinstance(item, dict)
    ]

    # If Render restarted and the internal V64 state was temporarily empty,
    # use the recovered IG position so the mobile app still shows the actual
    # broker trade and close countdown.
    if current_trade is None and broker_positions:
        broker_trade = broker_positions[0]
        current_trade = {
            "source": "IG_DEMO_BROKER",
            "trade_id": (
                f"IG_"
                f"{broker_trade.get('deal_id')}"
            ),
            "symbol": broker_trade.get(
                "symbol"
            ),
            "market": broker_trade.get(
                "market"
            ),
            "direction": broker_trade.get(
                "direction"
            ),
            "status": "OPEN",
            "entry_class": "IG_RECOVERED",
            "entry_price": broker_trade.get(
                "entry_level"
            ),
            "stake": broker_trade.get("size"),
            "opened_at": broker_trade.get(
                "opened_at"
            ),
            "scheduled_close_at":
                broker_trade.get(
                    "scheduled_close_at"
                ),
            "ig_deal_id": broker_trade.get(
                "deal_id"
            ),
            "ig_deal_reference":
                broker_trade.get(
                    "deal_reference"
                ),
            "demo_only": True,
            "live_money_execution": False,
        }

    broker_stats = dict(
        ig_demo.get("broker_stats")
        or {}
    )
    broker_performance = dict(
        ig_demo.get("broker_performance")
        or broker_stats
        or {}
    )
    learning_actual = dict(
        learning.get("actual")
        or {}
    )

    # Phase-1 statistics use the broker ledger when available. The internal
    # learning stats remain included below for model diagnostics.
    wins = int(
        broker_stats.get("wins")
        or learning_actual.get("wins")
        or 0
    )
    losses = int(
        broker_stats.get("losses")
        or learning_actual.get("losses")
        or 0
    )
    broker_stat_trades = int(
        broker_stats.get("trades")
        or 0
    )
    win_rate_pct = (
        float(
            broker_stats.get(
                "win_rate_pct"
            )
            or 0.0
        )
        if broker_stat_trades > 0
        else float(
            learning_actual.get(
                "win_rate_pct"
            )
            or 0.0
        )
    )

    return {
        "version":
            "6.7.1-UNIFIED-INTELLIGENCE-COMPOUND",
        "status": run_state,
        "demo_only": demo_only,
        "safe_to_run": bool(
            demo_only
            and broker.get("configured")
            is True
            and broker.get("connected")
            is True
        ),
        "scanner_universe":
            "IG_DEMO_CORE9",
        "risk_mode": str(
            manager.get("risk_mode")
            or "Balanced"
        ),
        "ai_min_confidence":
            PAPER_AI_MIN_CONFIDENCE,
        "ai_min_confidence_pct":
            PAPER_AI_MIN_CONFIDENCE
            * 100.0,
        "summary": {
            "phase_target":
                int(
                    ig_demo.get(
                        "phase_target"
                    )
                    or 10
                ),
            "phase_accepted_trades":
                int(
                    ig_demo.get(
                        "phase_accepted_trades"
                    )
                    or 0
                ),
            "phase_remaining":
                int(
                    ig_demo.get(
                        "phase_remaining"
                    )
                    or 0
                ),
            "phase_complete":
                phase_complete,
            "active_watchers":
                int(
                    learning.get(
                        "active_watchers"
                    )
                    or 0
                ),
            "max_watchers":
                int(
                    learning.get(
                        "max_watchers"
                    )
                    or 0
                ),
            "internal_open_trades":
                internal_open,
            "max_internal_open_trades":
                int(
                    learning.get(
                        "max_open_trades"
                    )
                    or 0
                ),
            "open_broker_positions":
                broker_open,
            "max_broker_positions":
                int(
                    ig_demo.get(
                        "max_open_positions"
                    )
                    or 0
                ),
            "paper_balance":
                float(
                    learning.get(
                        "paper_balance"
                    )
                    or 0.0
                ),
            "wins": wins,
            "losses": losses,
            "win_rate_pct":
                win_rate_pct,
            "total_pnl":
                float(
                    learning_actual.get(
                        "total_pnl"
                    )
                    or 0.0
                ),
            "broker_closed_positions":
                int(
                    broker_performance.get(
                        "closed_positions"
                    )
                    or 0
                ),
            "broker_graded_trades":
                int(
                    broker_performance.get(
                        "graded_trades"
                    )
                    or 0
                ),
            "broker_account_balance":
                broker_performance.get(
                    "account_balance"
                ),
            "broker_account_available":
                broker_performance.get(
                    "account_available"
                ),
            "broker_account_margin":
                broker_performance.get(
                    "account_margin"
                ),
            "broker_running_pnl":
                broker_performance.get(
                    "account_profit_loss"
                ),
            "broker_account_currency":
                broker_performance.get(
                    "account_currency"
                ),
            "broker_sync_state":
                ig_demo.get(
                    "sync_state"
                ),
            "broker_sync_age_seconds":
                ig_demo.get(
                    "broker_sync_age_seconds"
                ),
        },
        "current_trade": current_trade,
        "latest_settled_trade":
            latest_settled,
        "broker_positions":
            broker_positions,
        "broker_performance":
            broker_performance,
        "manager": manager,
        "learning": learning,
        "ig_demo": ig_demo,
        "state": {
            "learning_state_path":
                JASONG_STATE_PATH,
            "ig_mirror_state_path":
                ig_demo.get(
                    "state_path"
                ),
            "persistent_storage_expected":
                str(
                    JASONG_STATE_PATH
                ).startswith(
                    "/var/data/"
                ),
        },
        "environment": "DEMO",
        "live_money_execution": False,
    }


@app.get("/overnight-demo/status")
def overnight_demo_status():
    return _v664_overnight_demo_snapshot()


@app.post("/overnight-demo/start")
def overnight_demo_start(
    risk_mode: str = "Balanced",
    starting_balance: float = 10000.0,
    payout: float = 0.80,
):
    """Start the autonomous Phase-1 workflow, hard locked to IG DEMO."""
    validate_risk_mode(risk_mode)
    validate_balance(starting_balance)

    try:
        broker = IG_DEMO_BROKER.connect()
    except IGDemoError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    if (
        str(broker.get("environment") or "").upper() != "DEMO"
        or broker.get("demo_execution") is not True
        or broker.get("live_money_execution") is True
    ):
        raise HTTPException(
            status_code=409,
            detail="Overnight mode refused: broker is not hard-locked to IG DEMO.",
        )

    V64_LEARNING_ENGINE.enable()
    V64_LEARNING_ENGINE.start()

    V55_AUTO_MANAGER.enable(
        risk_mode=risk_mode,
        starting_balance=starting_balance,
        payout=payout,
        scan_interval_minutes=2,
        target_active_watchers=6,
        scan_top_n=9,
    )
    V55_AUTO_MANAGER.start_thread()

    IG_DEMO_MIRROR.set_enabled(True)
    IG_DEMO_MIRROR.start()

    queued = V55_AUTO_MANAGER.queue_run(
        source="overnight-demo",
    )

    result = _v664_overnight_demo_snapshot()
    result["launch_job"] = queued
    result["message"] = (
        "Overnight IG DEMO mode started. The backend will keep scanning, "
        "validating, learning and mirroring qualifying trades while the app is closed."
    )
    return result


@app.post("/overnight-demo/stop")
def overnight_demo_stop():
    """Stop NEW overnight entries while allowing any current DEMO trade to settle."""
    V55_AUTO_MANAGER.disable()

    # Remove learning watchers so no new internal entry can be opened while
    # an already-open learning/IG DEMO position is allowed to settle safely.
    try:
        with V64_LEARNING_ENGINE._lock:
            V64_LEARNING_ENGINE._state["watchers"] = []
        V64_LEARNING_ENGINE._persist()
    except Exception:
        pass

    learning = V64_LEARNING_ENGINE.status()
    ig_demo = IG_DEMO_MIRROR.status()
    internal_open = int(learning.get("open_trades") or 0)
    broker_open = int(ig_demo.get("open_broker_positions") or 0)

    if internal_open == 0 and broker_open == 0:
        V64_LEARNING_ENGINE.pause()
        IG_DEMO_MIRROR.set_enabled(False)
        message = "Overnight IG DEMO mode stopped. No positions are open."
    else:
        # Keep learning + mirror alive only to settle/close current DEMO trades.
        V64_LEARNING_ENGINE.enable()
        IG_DEMO_MIRROR.set_enabled(True)
        IG_DEMO_MIRROR.start()
        message = (
            "New overnight entries stopped. Existing DEMO positions will be "
            "allowed to settle and close automatically."
        )

    result = _v664_overnight_demo_snapshot()
    result["message"] = message
    return result


# ============================================================
# IG DEMO BROKER API
# ============================================================

@app.get("/ig-demo/status")
def ig_demo_status():
    return IG_DEMO_MIRROR.status()


@app.post("/ig-demo/connect")
def ig_demo_connect():
    try:
        broker = IG_DEMO_BROKER.connect()
        mirror = IG_DEMO_MIRROR.sync_once()
        return {
            "status": "IG_DEMO_CONNECTED",
            "broker": broker,
            "mirror": mirror,
            "live_money_execution": False,
        }
    except IGDemoError as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        )


@app.post("/ig-demo/autotrade")
def ig_demo_set_autotrade(
    enabled: bool = True,
):
    state = IG_DEMO_MIRROR.set_enabled(
        enabled
    )
    if enabled:
        IG_DEMO_MIRROR.start()
    return {
        "status": (
            "IG_DEMO_AUTOTRADE_ENABLED"
            if enabled
            else "IG_DEMO_AUTOTRADE_DISABLED"
        ),
        "mirror": state,
        "live_money_execution": False,
    }


@app.post("/ig-demo/sync-now")
def ig_demo_sync_now():
    try:
        return IG_DEMO_MIRROR.sync_once()
    except IGDemoError as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        )


@app.get("/ig-demo/accounts")
def ig_demo_accounts():
    try:
        return {
            "broker": "IG",
            "environment": "DEMO",
            "accounts": IG_DEMO_BROKER.accounts(),
            "live_money_execution": False,
        }
    except IGDemoError as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        )


@app.get("/ig-demo/market")
def ig_demo_market(
    symbol: str,
):
    try:
        return {
            "broker": "IG",
            "environment": "DEMO",
            "market":
                IG_DEMO_BROKER.resolve_market(
                    symbol,
                    require_tradeable=False,
                ),
            "live_money_execution": False,
        }
    except IGDemoError as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        )


@app.get("/ig-demo/positions")
def ig_demo_positions():
    try:
        return {
            "broker": "IG",
            "environment": "DEMO",
            "positions":
                IG_DEMO_BROKER.positions(),
            "live_money_execution": False,
        }
    except IGDemoError as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        )


@app.post("/ig-demo/open-test")
def ig_demo_open_test(
    symbol: str,
    direction: str,
    size: float | None = None,
):
    """Manual IG DEMO smoke-test order only; never targets IG live."""
    try:
        return IG_DEMO_BROKER.open_market_position(
            symbol=symbol,
            direction=direction,
            size=size,
        )
    except IGDemoError as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        )


@app.post("/ig-demo/close/{deal_id}")
def ig_demo_close(
    deal_id: str,
):
    try:
        return IG_DEMO_BROKER.close_position(
            deal_id
        )
    except IGDemoError as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        )


@app.get("/ai-learning/status")
def ai_learning_status():
    return {
        "mode": "DIRECT_AI40_SHADOW_PROMOTION_V662",
        "paper_only": True,
        "broker_execution_enabled": False,
        "ig_demo_broker_execution_enabled": bool(
            IG_DEMO_MIRROR.status().get("enabled")
            or COMPOUND_ENGINE.status().get("enabled")
        ),
        "live_money_execution": False,
        "normal_path_disabled_for_ai_learning": True,
        "shadow_promotion_for_ai_learning": True,
        "ig_demo": IG_DEMO_MIRROR.status(),
        "ai_min_confidence": PAPER_AI_MIN_CONFIDENCE,
        "ai_min_confidence_pct":
            PAPER_AI_MIN_CONFIDENCE * 100.0,
        "learning": V64_LEARNING_ENGINE.status(),
    }


@app.get("/ai-learning/trades")
def ai_learning_trades(limit: int = 100):
    return V64_LEARNING_ENGINE.trades(limit=limit)


@app.get("/ai-learning/watchers")
def ai_learning_watchers():
    return V64_LEARNING_ENGINE.watchers()


@app.get("/ai-learning/journal")
def ai_learning_journal(limit: int = 100):
    return V64_LEARNING_ENGINE.journal(limit=limit)


@app.post("/ai-learning/start")
def ai_learning_start():
    V64_LEARNING_ENGINE.enable()
    V64_LEARNING_ENGINE.start()
    return {
        "status": "AI_LEARNING_ENABLED",
        "paper_only": True,
        "live_execution": False,
        "learning": V64_LEARNING_ENGINE.status(),
    }


@app.post("/ai-learning/run-now")
def ai_learning_run_now():
    return _v66_ai_learning_cycle()


@app.post("/ai-learning/stop")
def ai_learning_stop():
    return {
        "status": "AI_LEARNING_PAUSED",
        "paper_only": True,
        "live_execution": False,
        "learning": V64_LEARNING_ENGINE.pause(),
    }


@app.get("/v64/learning-universe")
def v64_learning_universe(limit: int = 80):
    rows = get_learning_universe(limit=limit)
    return {
        "version": "6.6.0",
        "target_full_rotation_minutes": 12,
        "batch_size": FX_DISCOVERY_BATCH_SIZE,
        "scan_interval_minutes": 3,
        "returned": len(rows),
        "markets": rows,
        "live_execution": False,
    }


@app.get("/persistent-state")
def persistent_state_status():
    snapshot = V61_STATE_STORE.snapshot()
    return {
        "status": "ok",
        "version": "6.6.0",
        "path": str(V61_STATE_STORE.path),
        "namespaces": sorted(k for k in snapshot.keys() if k != "_meta"),
        "meta": snapshot.get("_meta", {}),
        "live_execution": False,
    }



# -----------------------------------------------------------------------------
# V6.3 ADAPTIVE CONFIDENCE API -- PAPER ONLY
# -----------------------------------------------------------------------------

@app.get("/adaptive-confidence/status")
def adaptive_confidence_status():
    """Show currently qualified market/direction/confidence buckets."""
    adaptive_confidence_gate.load()
    return adaptive_confidence_gate.snapshot()


@app.post("/adaptive-confidence/load")
def adaptive_confidence_load(payload: dict):
    """
    Load a COMPLETED V6.2.2 calibration result into the V6.3 adaptive gate.

    Send the full JSON returned by GET /confidence-wr/{job_id}.
    This changes PAPER eligibility only. It never enables broker execution.
    """
    snapshot = adaptive_confidence_gate.update_from_calibration_job(payload)
    adaptive_confidence_gate.load()
    return {
        "status": "LOADED",
        "paper_only": True,
        "broker_execution_enabled": False,
        "adaptive": snapshot,
    }


@app.post("/adaptive-confidence/check")
def adaptive_confidence_check(payload: dict):
    adaptive_confidence_gate.load()
    """
    Diagnostic check for one prospective signal.

    Example:
      {
        "market": "GBPJPY",
        "direction": "BUY",
        "confidence": 0.37,
        "normal_min_confidence": 0.30
      }
    """
    market = str(payload.get("market") or "").upper()
    direction = str(payload.get("direction") or "").upper()
    confidence = float(payload.get("confidence") or 0.0)
    normal_floor = float(payload.get("normal_min_confidence") or 0.30)

    if direction not in {"BUY", "SELL"}:
        return {
            "allowed_by_confidence": False,
            "path": "REJECT",
            "reason": "Direction must be BUY or SELL.",
            "paper_only": True,
        }

    result = adaptive_confidence_gate.evaluate(
        market=market,
        direction=direction,
        confidence=confidence,
        normal_min_confidence=normal_floor,
    )
    result["paper_only"] = True
    result["broker_execution_enabled"] = False
    return result



# ============================================================
# V6.9 SYSTEM HEALTH & OPERATIONS DASHBOARD
# ============================================================

def _v69_system_overview():
    """
    Read-only operational health snapshot.

    This endpoint does not modify trading decisions or execution state.
    It is designed to answer:
      - Is Jasong AI alive?
      - Is Auto Manager running?
      - How far is the current search?
      - Is calibration fresh?
      - Are market data and OpenAI available?
      - Are there current watchers/trades/errors?
    """

    now = time.time()

    manager = V55_AUTO_MANAGER.status()
    watchers = V53_WATCHER_ENGINE.list()

    adaptive_confidence_gate.load()
    adaptive = adaptive_confidence_gate.snapshot()

    cache = cache_stats()

    active_statuses = {
        "WATCHING",
        "READY",
        "RISK_BLOCKED",
        "OPEN",
    }

    active_watchers = [
        item
        for item in watchers
        if str(
            item.get("status")
            or ""
        ).upper()
        in active_statuses
    ]

    watching = [
        item
        for item in watchers
        if str(
            item.get("status")
            or ""
        ).upper()
        in {
            "WATCHING",
            "READY",
            "RISK_BLOCKED",
        }
    ]

    open_trades = [
        item
        for item in watchers
        if str(
            item.get("status")
            or ""
        ).upper()
        == "OPEN"
    ]

    settled = [
        item
        for item in watchers
        if str(
            item.get("status")
            or ""
        ).upper()
        in {
            "WIN",
            "LOSS",
        }
    ]

    wins = sum(
        1
        for item in settled
        if str(
            item.get("status")
            or item.get("result")
            or ""
        ).upper()
        == "WIN"
    )

    losses = len(settled) - wins

    settled_wr = (
        wins / len(settled)
        if settled
        else 0.0
    )

    total_pnl = sum(
        float(
            item.get("pnl")
            or 0.0
        )
        for item in settled
    )

    manager_enabled = bool(
        manager.get(
            "enabled",
            False,
        )
    )

    run_in_progress = bool(
        manager.get(
            "run_in_progress",
            False,
        )
    )

    scan_interval_minutes = int(
        manager.get(
            "scan_interval_minutes",
            15,
        )
        or 15
    )

    next_run_at = manager.get(
        "next_run_at"
    )

    last_run_at = manager.get(
        "last_run_at"
    )

    next_scan_seconds = None

    if next_run_at is not None:
        try:
            next_scan_seconds = round(
                float(next_run_at)
                - now,
                1,
            )
        except Exception:
            next_scan_seconds = None

    last_scan_age_seconds = None

    if last_run_at is not None:
        try:
            last_scan_age_seconds = max(
                0.0,
                round(
                    now
                    - float(last_run_at),
                    1,
                ),
            )
        except Exception:
            last_scan_age_seconds = None

    scheduler_overdue = False

    if (
        manager_enabled
        and not run_in_progress
        and next_scan_seconds is not None
        and next_scan_seconds
        < -300.0
    ):
        scheduler_overdue = True

    progress_stage = str(
        manager.get(
            "progress_stage",
            "IDLE",
        )
        or "IDLE"
    ).upper()

    progress_percent = int(
        manager.get(
            "progress_percent",
            0,
        )
        or 0
    )

    progress_candidate = manager.get(
        "progress_candidate"
    )

    last_error = (
        manager.get(
            "last_error"
        )
    )

    yahoo_cooldown = bool(
        cache.get(
            "yahoo_cooldown_active",
            False,
        )
    )

    calibration_fresh = not bool(
        adaptive.get(
            "stale",
            True,
        )
    )

    copilot_connected = bool(
        COPILOT.configured()
    )

    issues = []

    if last_error:
        issues.append({
            "severity": "RED",
            "component": "AUTO_MANAGER",
            "message": str(last_error),
        })

    if scheduler_overdue:
        issues.append({
            "severity": "RED",
            "component": "SCHEDULER",
            "message": (
                "Auto Manager is enabled but the scheduled cycle is overdue."
            ),
        })

    if not calibration_fresh:
        issues.append({
            "severity": "AMBER",
            "component": "CALIBRATION",
            "message": (
                "Adaptive confidence calibration is stale or missing."
            ),
        })

    if yahoo_cooldown:
        issues.append({
            "severity": "AMBER",
            "component": "MARKET_DATA",
            "message": (
                "Yahoo market-data cooldown is active."
            ),
        })

    if not copilot_connected:
        issues.append({
            "severity": "AMBER",
            "component": "OPENAI_COPILOT",
            "message": (
                "OpenAI copilot is not configured."
            ),
        })

    if any(
        issue["severity"] == "RED"
        for issue in issues
    ):
        overall = "RED"
        overall_label = "SYSTEM ERROR"

    elif issues:
        overall = "AMBER"
        overall_label = "RUNNING - ATTENTION"

    elif manager_enabled:
        overall = "GREEN"
        overall_label = "SYSTEM HEALTHY"

    else:
        overall = "GREY"
        overall_label = "SYSTEM ONLINE - AUTO MODE IDLE"

    if run_in_progress:
        activity = (
            f"{progress_stage}"
            + (
                f" • {progress_candidate}"
                if progress_candidate
                else ""
            )
            + f" • {progress_percent}%"
        )

    elif manager_enabled:
        activity = "Waiting for next automatic cycle"

    else:
        activity = "Auto Manager is disabled"

    return {
        "version": "V6.9",
        "generated_at": now,
        "overall": {
            "status": overall,
            "label": overall_label,
            "issues_count": len(issues),
        },
        "backend": {
            "status": "ONLINE",
            "healthy": True,
            "live_execution": False,
        },
        "auto_manager": {
            "enabled": manager_enabled,
            "status": (
                "RUNNING"
                if run_in_progress
                else (
                    "ENABLED"
                    if manager_enabled
                    else "IDLE"
                )
            ),
            "runs": int(
                manager.get(
                    "runs",
                    0,
                )
                or 0
            ),
            "scan_interval_minutes": scan_interval_minutes,
            "last_run_at": last_run_at,
            "last_scan_age_seconds": last_scan_age_seconds,
            "next_run_at": next_run_at,
            "next_scan_seconds": next_scan_seconds,
            "scheduler_overdue": scheduler_overdue,
            "last_error": last_error,
        },
        "search": {
            "run_in_progress": run_in_progress,
            "stage": progress_stage,
            "message": manager.get(
                "progress_message",
                "Waiting for next cycle",
            ),
            "percent": progress_percent,
            "candidate": progress_candidate,
            "active_job_id": manager.get(
                "active_job_id"
            ),
            "queued_or_running_jobs": int(
                manager.get(
                    "queued_or_running_jobs",
                    0,
                )
                or 0
            ),
            "activity": activity,
        },
        "watchers": {
            "active": len(active_watchers),
            "watching": len(watching),
            "target": int(
                manager.get(
                    "target_active_watchers",
                    3,
                )
                or 3
            ),
            "open_trades": len(open_trades),
            "total_records": len(watchers),
        },
        "forward_performance": {
            "settled_trades": len(settled),
            "wins": wins,
            "losses": losses,
            "win_rate": round(
                settled_wr,
                6,
            ),
            "win_rate_pct": round(
                settled_wr * 100.0,
                2,
            ),
            "total_pnl": round(
                total_pnl,
                2,
            ),
        },
        "calibration": {
            "status": (
                "FRESH"
                if calibration_fresh
                else "STALE"
            ),
            "fresh": calibration_fresh,
            "qualified_count": int(
                adaptive.get(
                    "qualified_count",
                    0,
                )
                or 0
            ),
            "source_job_id": adaptive.get(
                "source_job_id"
            ),
            "updated_at": adaptive.get(
                "updated_at"
            ),
        },
        "market_data": {
            "status": (
                "DEGRADED"
                if yahoo_cooldown
                else "HEALTHY"
            ),
            "yahoo_cooldown_active": yahoo_cooldown,
            "yahoo_cooldown_remaining": cache.get(
                "yahoo_cooldown_remaining",
                0.0,
            ),
            "cache_entries": cache.get(
                "entries",
                0,
            ),
        },
        "copilot": {
            "status": (
                "CONNECTED"
                if copilot_connected
                else "DISCONNECTED"
            ),
            "configured": copilot_connected,
            "model": COPILOT.model,
            "advisory_only": True,
        },
        "issues": issues,
        "paper_only": True,
        "broker_execution_enabled": False,
    }


@app.get("/v69/system-overview")
def v69_system_overview():
    return _v69_system_overview()


@app.get("/v69/diagnostic")
def v69_system_diagnostic():
    """
    Performs a read-only diagnostic including one lightweight market-data probe.
    No trade is created or modified.
    """

    checks = []

    overview = _v69_system_overview()

    checks.append({
        "component": "BACKEND",
        "passed": True,
        "message": "Backend endpoint is responding.",
    })

    checks.append({
        "component": "AUTO_MANAGER",
        "passed": not bool(
            overview[
                "auto_manager"
            ][
                "scheduler_overdue"
            ]
        )
        and not bool(
            overview[
                "auto_manager"
            ][
                "last_error"
            ]
        ),
        "message": (
            "Auto Manager scheduler is responsive."
            if (
                not overview[
                    "auto_manager"
                ][
                    "scheduler_overdue"
                ]
                and not overview[
                    "auto_manager"
                ][
                    "last_error"
                ]
            )
            else "Auto Manager needs attention."
        ),
    })

    checks.append({
        "component": "CALIBRATION",
        "passed": bool(
            overview[
                "calibration"
            ][
                "fresh"
            ]
        ),
        "message": (
            "Adaptive calibration is fresh."
            if overview[
                "calibration"
            ][
                "fresh"
            ]
            else "Adaptive calibration is stale or missing."
        ),
    })

    checks.append({
        "component": "OPENAI_COPILOT",
        "passed": bool(
            COPILOT.configured()
        ),
        "message": (
            "OpenAI copilot is configured."
            if COPILOT.configured()
            else "OpenAI copilot is not configured."
        ),
    })

    market_probe = {
        "passed": False,
        "rows": 0,
        "latest_price": None,
        "error": None,
    }

    try:
        probe = get_data(
            "EURUSD=X",
            "5d",
            "15m",
        )

        market_probe[
            "rows"
        ] = int(
            len(probe)
        )

        market_probe[
            "latest_price"
        ] = float(
            probe[
                "Close"
            ].iloc[
                -1
            ]
        )

        market_probe[
            "passed"
        ] = bool(
            len(probe) >= 80
        )

    except Exception as exc:
        market_probe[
            "error"
        ] = str(exc)

    checks.append({
        "component": "MARKET_DATA",
        "passed": bool(
            market_probe[
                "passed"
            ]
        ),
        "message": (
            "EURUSD 15m market-data probe passed."
            if market_probe[
                "passed"
            ]
            else (
                "Market-data probe failed: "
                + str(
                    market_probe[
                        "error"
                    ]
                    or "insufficient rows"
                )
            )
        ),
        "details": market_probe,
    })

    failed = [
        item
        for item in checks
        if not bool(
            item.get(
                "passed"
            )
        )
    ]

    red_components = {
        "BACKEND",
        "AUTO_MANAGER",
        "MARKET_DATA",
    }

    critical_failed = any(
        item[
            "component"
        ]
        in red_components
        for item in failed
    )

    if critical_failed:
        status = "RED"
        label = "SYSTEM DIAGNOSTIC FAILED"

    elif failed:
        status = "AMBER"
        label = "SYSTEM RUNNING WITH WARNINGS"

    else:
        status = "GREEN"
        label = "SYSTEM CHECK PASSED"

    return {
        "version": "V6.9",
        "status": status,
        "label": label,
        "passed_checks": len(
            checks
        )
        - len(
            failed
        ),
        "total_checks": len(
            checks
        ),
        "checks": checks,
        "paper_only": True,
        "broker_execution_enabled": False,
    }


# ============================================================
# V6.8 INTELLIGENCE COPILOT
# ============================================================

def _v68_copilot_context():
    """
    Read-only evidence bundle for the OpenAI copilot.
    Uses the existing V6.6 watcher engine and adaptive gate.
    The copilot receives no broker credentials and has no execution tools.
    """

    # Existing V5.3/V6.x watcher engine is the source of truth.
    watchers = V53_WATCHER_ENGINE.list()

    # Existing V6.6 forward intelligence summarizes genuine settled trades.
    v66_forward = (
        V66_INTELLIGENCE
        .forward_performance(
            watchers
        )
    )

    # Existing V6.3 adaptive gate persists the currently-qualified buckets.
    adaptive_confidence_gate.load()

    paper_trades = []

    for item in watchers:
        status = str(
            item.get("status")
            or ""
        ).upper()

        if (
            item.get("trade_id")
            or status in {
                "OPEN",
                "WIN",
                "LOSS",
            }
        ):
            snapshot = (
                item.get(
                    "entry_snapshot"
                )
                or {}
            )

            paper_trades.append({
                "trade_id":
                    item.get("trade_id"),
                "market":
                    item.get("market"),
                "symbol":
                    item.get("symbol"),
                "direction":
                    item.get("direction"),
                "status":
                    status,
                "entry_path":
                    item.get("entry_path"),
                "entry_confidence":
                    snapshot.get(
                        "live_confidence"
                    )
                    or (
                        item.get(
                            "last_live_signal"
                        )
                        or {}
                    ).get(
                        "confidence"
                    ),
                "entry_price":
                    item.get(
                        "entry_price_effective"
                    )
                    or item.get(
                        "entry_price"
                    ),
                "exit_price":
                    item.get(
                        "exit_price_effective"
                    )
                    or item.get(
                        "exit_price"
                    ),
                "result":
                    item.get("result"),
                "pnl":
                    item.get("pnl"),
                "entry_time_iso":
                    item.get(
                        "entry_time_iso"
                    ),
                "closed_at_iso":
                    item.get(
                        "closed_at_iso"
                    ),
                "historical_win_rate":
                    item.get(
                        "win_rate"
                    ),
                "historical_profit_factor":
                    item.get(
                        "profit_factor"
                    ),
                "historical_trades":
                    item.get(
                        "trades"
                    ),
                "adaptive_gate":
                    item.get(
                        "adaptive_gate"
                    ),
                "v66_forward_gate":
                    item.get(
                        "v66_forward_gate"
                    ),
                "v66_portfolio_gate":
                    item.get(
                        "v66_portfolio_gate"
                    ),
            })

    paper_trades.sort(
        key=lambda row: str(
            row.get(
                "entry_time_iso"
            )
            or ""
        ),
        reverse=True,
    )

    active_watchers = []

    for item in watchers:
        status = str(
            item.get("status")
            or ""
        ).upper()

        if status in {
            "WATCHING",
            "OPEN",
            "RISK_BLOCKED",
        }:
            active_watchers.append({
                "market":
                    item.get("market"),
                "symbol":
                    item.get("symbol"),
                "direction":
                    item.get("direction"),
                "status":
                    status,
                "entry_path":
                    item.get("entry_path"),
                "last_reason":
                    item.get(
                        "last_reason"
                    ),
                "last_live_signal":
                    item.get(
                        "last_live_signal"
                    ),
                "adaptive_gate":
                    item.get(
                        "adaptive_gate"
                    ),
                "v66_timing":
                    item.get(
                        "v66_timing"
                    ),
            })

    ig_demo_state = IG_DEMO_MIRROR.status()
    compound_state = COMPOUND_ENGINE.status()
    ig_demo_execution_enabled = bool(
        ig_demo_state.get("enabled")
        or compound_state.get("enabled")
        or compound_state.get("current_cycle")
    )

    return {
        "engine": {
            "version":
                "V6.7.1",
            "paper_learning_enabled":
                True,
            "ig_demo_broker_execution_enabled":
                ig_demo_execution_enabled,
            "live_money_execution":
                False,
            "copilot_advisory_only":
                True,
        },
        "ig_demo": ig_demo_state,
        "elite_compound": compound_state,
        "adaptive_confidence":
            adaptive_confidence_gate.snapshot(),
        "v66_forward_intelligence":
            v66_forward,
        "paper_trades":
            paper_trades[:50],
        "active_watchers":
            active_watchers,
    }


class V68CopilotRequest(BaseModel):
    question: str
    mode: str = "GENERAL"


@app.get("/v68/status")
def v68_status():
    return {
        "version": "V6.8.1",
        "openai_configured": COPILOT.configured(),
        "model": COPILOT.model,
        "copilot_advisory_only": True,
        "can_execute_trades": False,
        "paper_only": True,
        "broker_execution_enabled": False,
        "features": [
            "ASK_JASONG_AI",
            "OVERNIGHT_TRADE_REVIEW",
            "LOSS_PATTERN_ANALYSIS",
            "FORWARD_VS_HISTORICAL_REVIEW",
            "TRADE_EXPLANATION",
        ],
    }


@app.post("/v68/ask")
def v68_ask(request: V68CopilotRequest):
    question = (request.question or "").strip()

    if not question:
        raise HTTPException(
            status_code=400,
            detail="question is required",
        )

    if not COPILOT.configured():
        raise HTTPException(
            status_code=503,
            detail="OpenAI is not configured on the backend.",
        )

    try:
        return COPILOT.analyze(
            question=question,
            context=_v68_copilot_context(),
            mode=request.mode,
        )

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        )


@app.get("/v68/overnight-review")
def v68_overnight_review():
    if not COPILOT.configured():
        raise HTTPException(
            status_code=503,
            detail="OpenAI is not configured on the backend.",
        )

    question = (
        "Review the most recent Jasong session using BOTH internal PAPER/model evidence "
        "and actual IG DEMO broker evidence. If Elite Compound was active, review its cycle evidence separately. "
        "Separate settled trades "
        "from open/watch-only setups. Include wins, losses, WR, PF and "
        "P&L where supported. Identify repeated loss patterns and say "
        "whether the sample is large enough to justify changing the strategy."
    )

    try:
        return COPILOT.analyze(
            question=question,
            context=_v68_copilot_context(),
            mode="OVERNIGHT_REVIEW",
        )

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        )


# ============================================================
# V6.6 INTELLIGENT FORWARD ENGINE
# ============================================================

@app.get("/v66/status")
def v66_status():
    return {
        "engine": V66_INTELLIGENCE.status(),
        "adaptive_confidence":
            adaptive_confidence_gate.snapshot(),
        "live_execution": False,
    }


@app.get("/v66/forward-intelligence")
def v66_forward_intelligence():
    watchers = V53_WATCHER_ENGINE.list()

    return V66_INTELLIGENCE.forward_performance(
        watchers
    )


@app.get("/v66/correlation")
def v66_correlation():
    matrix = _v66_fx_correlation_matrix()

    return {
        "version":
            "V6.6_FX_CORRELATION",
        "threshold_abs":
            V66_INTELLIGENCE.high_correlation_abs,
        "matrix":
            matrix,
        "live_execution":
            False,
    }


@app.get("/v66/portfolio")
def v66_portfolio():
    watchers = V53_WATCHER_ENGINE.list()

    open_watchers = [
        item
        for item in watchers
        if str(
            item.get("status")
            or ""
        ).upper()
        == "OPEN"
    ]

    exposure = {}

    for item in open_watchers:
        signed = (
            V66_INTELLIGENCE
            .currency_signed_exposure(
                item.get("symbol"),
                item.get("direction"),
            )
        )

        for currency, value in signed.items():
            exposure[
                currency
            ] = (
                exposure.get(
                    currency,
                    0,
                )
                + value
            )

    return {
        "version":
            "V6.6_PORTFOLIO_INTELLIGENCE",
        "open_trades":
            len(open_watchers),
        "currency_exposure":
            exposure,
        "max_currency_exposure":
            V66_INTELLIGENCE.max_currency_exposure,
        "high_correlation_abs":
            V66_INTELLIGENCE.high_correlation_abs,
        "live_execution":
            False,
    }
