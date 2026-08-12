from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd
import yfinance as yf


# ============================================================
# JASONG AI TRADER V6.2
# MULTI-SOURCE FOREX MARKET-DATA ROUTER
# ============================================================

TWELVE_DATA_API_KEY = os.getenv(
    "TWELVE_DATA_API_KEY",
    "",
).strip()

FINNHUB_API_KEY = os.getenv(
    "FINNHUB_API_KEY",
    "",
).strip()

TWELVE_BASE = "https://api.twelvedata.com"
FINNHUB_BASE = "https://finnhub.io/api/v1"

CACHE_TTL_SECONDS = 300
STALE_CACHE_SECONDS = 21600
UNIVERSE_TTL_SECONDS = 86400

# Protect a Twelve Data Basic account. This leaves spare headroom below the
# public Basic allowances instead of spending every available credit.
TWELVE_MAX_CALLS_PER_MINUTE = 7
TWELVE_SOFT_DAILY_CALL_LIMIT = 700


@dataclass
class RouterCacheEntry:
    dataframe: pd.DataFrame
    created_at: float
    source: str


_CACHE: Dict[str, RouterCacheEntry] = {}
_CACHE_LOCK = threading.RLock()

_UNIVERSE_CACHE: List[dict] = []
_UNIVERSE_UPDATED_AT = 0.0
_UNIVERSE_LOCK = threading.RLock()

_TWELVE_CALL_TIMES = deque()
_TWELVE_DAY_KEY = None
_TWELVE_DAY_CALLS = 0
_TWELVE_RATE_LOCK = threading.RLock()

_PROVIDER_STATE = {
    "TWELVE_DATA": {
        "configured": bool(TWELVE_DATA_API_KEY),
        "healthy": None,
        "last_success": None,
        "last_error": None,
        "requests": 0,
        "skipped_for_budget": 0,
    },
    "FINNHUB": {
        "configured": bool(FINNHUB_API_KEY),
        "healthy": None,
        "last_success": None,
        "last_error": None,
        "requests": 0,
    },
    "YAHOO": {
        "configured": True,
        "healthy": None,
        "last_success": None,
        "last_error": None,
        "requests": 0,
    },
}


SPECIAL_YAHOO_TO_FX = {
    "JPY=X": "USD/JPY",
    "CAD=X": "USD/CAD",
    "CHF=X": "USD/CHF",
}

SPECIAL_FX_TO_YAHOO = {
    "USD/JPY": "JPY=X",
    "USD/CAD": "CAD=X",
    "USD/CHF": "CHF=X",
}


# ============================================================
# GENERIC HTTP
# ============================================================

def _json_get(
    url: str,
    params: dict,
    timeout: float = 20.0,
):
    query = urlencode(params)
    request = Request(
        f"{url}?{query}",
        headers={
            "User-Agent": "Jasong-AI-Trader-V6.2",
            "Accept": "application/json",
        },
    )

    with urlopen(
        request,
        timeout=timeout,
    ) as response:
        payload = response.read().decode(
            "utf-8"
        )

    return json.loads(payload)


# ============================================================
# SYMBOL NORMALISATION
# ============================================================

def canonical_fx_symbol(
    symbol: str,
) -> str:
    clean = str(symbol or "").upper().strip()

    if clean in SPECIAL_YAHOO_TO_FX:
        return SPECIAL_YAHOO_TO_FX[clean]

    if "/" in clean:
        left, _, right = clean.partition("/")
        if len(left) == 3 and len(right) == 3:
            return f"{left}/{right}"

    if clean.endswith("=X"):
        clean = clean[:-2]

    clean = (
        clean.replace("/", "")
        .replace("_", "")
        .replace("-", "")
        .replace(" ", "")
    )

    if len(clean) == 6:
        return f"{clean[:3]}/{clean[3:]}"

    return str(symbol or "").strip()


def market_name(symbol: str) -> str:
    return canonical_fx_symbol(symbol).replace(
        "/",
        "",
    )


def yahoo_symbol(symbol: str) -> str:
    canonical = canonical_fx_symbol(symbol)

    if canonical in SPECIAL_FX_TO_YAHOO:
        return SPECIAL_FX_TO_YAHOO[canonical]

    compact = canonical.replace("/", "")

    if len(compact) == 6:
        return compact + "=X"

    return str(symbol)


# ============================================================
# INTERVAL / HISTORY NORMALISATION
# ============================================================

def twelve_interval(interval: str) -> str:
    lookup = {
        "1m": "1min",
        "2m": "1min",
        "5m": "5min",
        "15m": "15min",
        "30m": "30min",
        "60m": "1h",
        "1h": "1h",
        "2h": "2h",
        "4h": "4h",
        "1d": "1day",
    }
    return lookup.get(
        str(interval).lower(),
        str(interval),
    )


def approximate_outputsize(
    period: str,
    interval: str,
) -> int:
    period = str(period).lower()
    interval = str(interval).lower()

    if period == "1d":
        size = 150
    elif period in {"5d", "7d"}:
        size = 550
    elif period == "1mo":
        size = 2200
    elif period == "3mo":
        size = 3500
    elif period == "6mo":
        size = 4500
    else:
        size = 2500

    if interval in {"1h", "60m", "2h", "4h"}:
        size = min(size, 2500)

    return max(100, min(size, 5000))


# ============================================================
# DATA CLEANING
# ============================================================

def clean_ohlcv(
    frame: pd.DataFrame,
) -> pd.DataFrame:
    if frame is None:
        return pd.DataFrame()

    data = frame.copy()

    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)

    rename_map = {}

    for col in data.columns:
        lower = str(col).lower()
        if lower == "open":
            rename_map[col] = "Open"
        elif lower == "high":
            rename_map[col] = "High"
        elif lower == "low":
            rename_map[col] = "Low"
        elif lower == "close":
            rename_map[col] = "Close"
        elif lower == "volume":
            rename_map[col] = "Volume"

    data = data.rename(columns=rename_map)

    required = ["Open", "High", "Low", "Close"]

    if not all(col in data.columns for col in required):
        return pd.DataFrame()

    if "Volume" not in data.columns:
        data["Volume"] = 0.0

    data = data[
        ["Open", "High", "Low", "Close", "Volume"]
    ].copy()

    for col in data.columns:
        data[col] = pd.to_numeric(
            data[col],
            errors="coerce",
        )

    data = data.dropna(subset=required)

    if not isinstance(data.index, pd.DatetimeIndex):
        data.index = pd.to_datetime(
            data.index,
            errors="coerce",
            utc=True,
        )

    data = data[~data.index.isna()]

    return data.sort_index()


# ============================================================
# CACHE
# ============================================================

def _cache_key(
    symbol: str,
    period: str,
    interval: str,
) -> str:
    return (
        f"{canonical_fx_symbol(symbol)}|"
        f"{period.lower()}|{interval.lower()}"
    )


def _cache_put(
    symbol: str,
    period: str,
    interval: str,
    data: pd.DataFrame,
    source: str,
) -> None:
    key = _cache_key(symbol, period, interval)

    with _CACHE_LOCK:
        _CACHE[key] = RouterCacheEntry(
            dataframe=data.copy(),
            created_at=time.time(),
            source=source,
        )


def _cache_get(
    symbol: str,
    period: str,
    interval: str,
    stale: bool = False,
) -> Optional[pd.DataFrame]:
    key = _cache_key(symbol, period, interval)

    with _CACHE_LOCK:
        entry = _CACHE.get(key)

        if entry is None:
            return None

        age = time.time() - entry.created_at
        max_age = (
            STALE_CACHE_SECONDS
            if stale
            else CACHE_TTL_SECONDS
        )

        if age > max_age:
            return None

        return entry.dataframe.copy()


# ============================================================
# PROVIDER TELEMETRY
# ============================================================

def _provider_request(provider: str) -> None:
    _PROVIDER_STATE[provider]["requests"] += 1


def _provider_success(provider: str) -> None:
    state = _PROVIDER_STATE[provider]
    state["healthy"] = True
    state["last_success"] = time.time()
    state["last_error"] = None


def _provider_failure(
    provider: str,
    exc: Exception,
) -> None:
    state = _PROVIDER_STATE[provider]
    state["healthy"] = False
    state["last_error"] = str(exc)


# ============================================================
# TWELVE DATA CREDIT GUARD
# ============================================================

def _allow_twelve_call() -> bool:
    global _TWELVE_DAY_KEY
    global _TWELVE_DAY_CALLS

    now = time.time()
    day_key = time.strftime(
        "%Y-%m-%d",
        time.gmtime(now),
    )

    with _TWELVE_RATE_LOCK:
        if _TWELVE_DAY_KEY != day_key:
            _TWELVE_DAY_KEY = day_key
            _TWELVE_DAY_CALLS = 0
            _TWELVE_CALL_TIMES.clear()

        while (
            _TWELVE_CALL_TIMES
            and now - _TWELVE_CALL_TIMES[0] >= 60.0
        ):
            _TWELVE_CALL_TIMES.popleft()

        if (
            len(_TWELVE_CALL_TIMES)
            >= TWELVE_MAX_CALLS_PER_MINUTE
        ):
            _PROVIDER_STATE["TWELVE_DATA"][
                "skipped_for_budget"
            ] += 1
            return False

        if (
            _TWELVE_DAY_CALLS
            >= TWELVE_SOFT_DAILY_CALL_LIMIT
        ):
            _PROVIDER_STATE["TWELVE_DATA"][
                "skipped_for_budget"
            ] += 1
            return False

        _TWELVE_CALL_TIMES.append(now)
        _TWELVE_DAY_CALLS += 1
        return True


# ============================================================
# TWELVE DATA
# ============================================================

def download_twelve_data(
    symbol: str,
    period: str,
    interval: str,
) -> pd.DataFrame:
    if not TWELVE_DATA_API_KEY:
        raise RuntimeError(
            "TWELVE_DATA_API_KEY not configured"
        )

    if not _allow_twelve_call():
        raise RuntimeError(
            "Twelve Data local credit guard active"
        )

    canonical = canonical_fx_symbol(symbol)
    _provider_request("TWELVE_DATA")

    payload = _json_get(
        f"{TWELVE_BASE}/time_series",
        {
            "symbol": canonical,
            "interval": twelve_interval(interval),
            "outputsize": approximate_outputsize(
                period,
                interval,
            ),
            "timezone": "UTC",
            "format": "JSON",
            "apikey": TWELVE_DATA_API_KEY,
        },
        timeout=20.0,
    )

    if payload.get("status") == "error":
        raise RuntimeError(
            payload.get(
                "message",
                "Twelve Data error",
            )
        )

    values = payload.get("values") or []

    if not values:
        raise RuntimeError(
            f"Twelve Data returned no values for {canonical}"
        )

    frame = pd.DataFrame(values)
    frame["datetime"] = pd.to_datetime(
        frame["datetime"],
        utc=True,
        errors="coerce",
    )
    frame = frame.set_index("datetime")

    data = clean_ohlcv(frame)

    if len(data) < 80:
        raise RuntimeError(
            f"Twelve Data returned only {len(data)} usable rows"
        )

    _provider_success("TWELVE_DATA")
    return data


# ============================================================
# FINNHUB METADATA / DISCOVERY
# ============================================================

def finnhub_forex_symbols(
    exchange: str = "oanda",
) -> List[dict]:
    if not FINNHUB_API_KEY:
        return []

    _provider_request("FINNHUB")

    try:
        payload = _json_get(
            f"{FINNHUB_BASE}/forex/symbol",
            {
                "exchange": exchange,
                "token": FINNHUB_API_KEY,
            },
            timeout=15.0,
        )

        if not isinstance(payload, list):
            raise RuntimeError(
                "Unexpected Finnhub forex-symbol response"
            )

        _provider_success("FINNHUB")
        return payload

    except Exception as exc:
        _provider_failure("FINNHUB", exc)
        return []


# ============================================================
# YAHOO FALLBACK
# ============================================================

def download_yahoo(
    symbol: str,
    period: str,
    interval: str,
) -> pd.DataFrame:
    y_symbol = yahoo_symbol(symbol)
    _provider_request("YAHOO")

    raw = yf.download(
        y_symbol,
        period=period,
        interval=interval,
        progress=False,
        auto_adjust=True,
        threads=False,
    )

    data = clean_ohlcv(raw)

    if len(data) < 80:
        raise RuntimeError(
            f"Yahoo returned only {len(data)} usable rows "
            f"for {y_symbol}"
        )

    _provider_success("YAHOO")
    return data


# ============================================================
# PUBLIC MARKET-DATA ROUTER
# ============================================================

def get_market_data(
    symbol: str,
    period: str = "1mo",
    interval: str = "15m",
) -> pd.DataFrame:
    cached = _cache_get(symbol, period, interval)

    if cached is not None and len(cached) >= 80:
        return cached

    errors = []

    if TWELVE_DATA_API_KEY:
        try:
            data = download_twelve_data(
                symbol,
                period,
                interval,
            )
            _cache_put(
                symbol,
                period,
                interval,
                data,
                "TWELVE_DATA",
            )
            return data
        except Exception as exc:
            _provider_failure("TWELVE_DATA", exc)
            errors.append(f"Twelve Data: {exc}")

    try:
        data = download_yahoo(
            symbol,
            period,
            interval,
        )
        _cache_put(
            symbol,
            period,
            interval,
            data,
            "YAHOO",
        )
        return data
    except Exception as exc:
        _provider_failure("YAHOO", exc)
        errors.append(f"Yahoo: {exc}")

    stale = _cache_get(
        symbol,
        period,
        interval,
        stale=True,
    )

    if stale is not None and len(stale) >= 80:
        return stale

    raise RuntimeError(
        "All market-data routes failed for "
        f"{symbol} {period} {interval}. "
        + " | ".join(errors)
    )


# ============================================================
# FULL FOREX UNIVERSE DISCOVERY
# ============================================================

def _normalise_twelve_universe(
    rows: List[dict],
) -> List[dict]:
    output = []

    for row in rows:
        symbol = str(
            row.get("symbol", "")
        ).upper()

        if "/" not in symbol:
            continue

        compact = symbol.replace("/", "")
        if len(compact) != 6:
            continue

        output.append({
            "market": compact,
            "symbol": symbol,
            "currency_group": row.get(
                "currency_group"
            ),
            "currency_base": row.get(
                "currency_base"
            ) or symbol[:3],
            "currency_quote": row.get(
                "currency_quote"
            ) or symbol[-3:],
            "source": "TWELVE_DATA",
        })

    return output


def _normalise_finnhub_universe(
    rows: List[dict],
) -> List[dict]:
    output = []

    for row in rows:
        display = str(
            row.get("displaySymbol", "")
        ).upper()

        if "/" not in display:
            continue

        compact = display.replace("/", "")
        if len(compact) != 6:
            continue

        output.append({
            "market": compact,
            "symbol": display,
            "currency_group": None,
            "currency_base": display[:3],
            "currency_quote": display[-3:],
            "source": "FINNHUB",
        })

    return output


def _prioritise_universe(
    pairs: List[dict],
) -> List[dict]:
    preferred = {
        "USD": 0,
        "EUR": 1,
        "GBP": 2,
        "JPY": 3,
        "AUD": 4,
        "CAD": 5,
        "CHF": 6,
        "NZD": 7,
        "ZAR": 8,
    }

    def score(item: dict):
        base = str(item.get("currency_base") or "")
        quote = str(item.get("currency_quote") or "")
        base_score = preferred.get(base, 50)
        quote_score = preferred.get(quote, 50)
        return (
            base_score + quote_score,
            item.get("symbol", ""),
        )

    return sorted(pairs, key=score)


def get_forex_universe(
    force_refresh: bool = False,
) -> List[dict]:
    global _UNIVERSE_CACHE
    global _UNIVERSE_UPDATED_AT

    now = time.time()

    with _UNIVERSE_LOCK:
        if (
            not force_refresh
            and _UNIVERSE_CACHE
            and now - _UNIVERSE_UPDATED_AT
            < UNIVERSE_TTL_SECONDS
        ):
            return list(_UNIVERSE_CACHE)

    pairs: List[dict] = []

    if TWELVE_DATA_API_KEY and _allow_twelve_call():
        try:
            _provider_request("TWELVE_DATA")
            payload = _json_get(
                f"{TWELVE_BASE}/forex_pairs",
                {
                    "apikey": TWELVE_DATA_API_KEY,
                    "format": "JSON",
                },
                timeout=30.0,
            )

            rows = payload.get("data") or []
            pairs = _normalise_twelve_universe(rows)

            if pairs:
                _provider_success("TWELVE_DATA")

        except Exception as exc:
            _provider_failure("TWELVE_DATA", exc)

    # Finnhub supplements or replaces discovery when Twelve Data discovery
    # is unavailable. It is not assumed to provide free historical candles.
    finn_rows = finnhub_forex_symbols()
    finn_pairs = _normalise_finnhub_universe(finn_rows)

    combined = {}
    for item in pairs + finn_pairs:
        combined.setdefault(item["symbol"], item)

    pairs = list(combined.values())

    if not pairs:
        majors = [
            "EUR/USD",
            "GBP/USD",
            "USD/JPY",
            "AUD/USD",
            "NZD/USD",
            "USD/CAD",
            "USD/CHF",
            "EUR/JPY",
            "GBP/JPY",
        ]
        pairs = [
            {
                "market": item.replace("/", ""),
                "symbol": item,
                "currency_base": item[:3],
                "currency_quote": item[-3:],
                "currency_group": "Fallback",
                "source": "BUILT_IN",
            }
            for item in majors
        ]

    pairs = _prioritise_universe(pairs)

    with _UNIVERSE_LOCK:
        _UNIVERSE_CACHE = pairs
        _UNIVERSE_UPDATED_AT = now

    return list(pairs)


# ============================================================
# HEALTH / TELEMETRY
# ============================================================

def market_data_health() -> dict:
    healthy = [
        name
        for name, state in _PROVIDER_STATE.items()
        if state.get("healthy") is True
    ]

    configured = [
        name
        for name, state in _PROVIDER_STATE.items()
        if state.get("configured")
    ]

    return {
        "status": "HEALTHY" if healthy else "READY",
        "priority": [
            "TWELVE_DATA",
            "YAHOO",
            "STALE_CACHE",
        ],
        "finnhub_role": (
            "FOREX_DISCOVERY_METADATA"
        ),
        "configured_providers": configured,
        "healthy_providers": healthy,
        "providers": {
            key: dict(value)
            for key, value in _PROVIDER_STATE.items()
        },
        "cache_entries": len(_CACHE),
        "universe_size": len(_UNIVERSE_CACHE),
        "universe_updated_at": (
            _UNIVERSE_UPDATED_AT or None
        ),
        "twelve_data_credit_guard": {
            "max_calls_per_minute": (
                TWELVE_MAX_CALLS_PER_MINUTE
            ),
            "soft_daily_call_limit": (
                TWELVE_SOFT_DAILY_CALL_LIMIT
            ),
            "calls_today": _TWELVE_DAY_CALLS,
        },
    }
