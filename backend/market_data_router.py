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

from ig_demo_broker import IGDemoBroker


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

# V6.6.5: use the connected IG DEMO account as the primary candle source.
# This can be disabled without code changes if needed.
IG_DEMO_MARKET_DATA_ENABLED = (
    os.getenv(
        "IG_DEMO_MARKET_DATA",
        "true",
    ).strip().lower()
    not in {"0", "false", "no", "off"}
)
IG_DEMO_HISTORY_REFRESH_SECONDS = max(
    300,
    int(
        os.getenv(
            "IG_DEMO_HISTORY_REFRESH_SECONDS",
            "1800",
        )
    ),
)
IG_DEMO_WARMUP_POINTS = max(
    80,
    min(
        500,
        int(
            os.getenv(
                "IG_DEMO_WARMUP_POINTS",
                "500",
            )
        ),
    ),
)
IG_DEMO_MAX_CACHE_ROWS = max(
    160,
    min(
        2000,
        int(
            os.getenv(
                "IG_DEMO_MAX_CACHE_ROWS",
                "600",
            )
        ),
    ),
)

TWELVE_BASE = "https://api.twelvedata.com"
FINNHUB_BASE = "https://finnhub.io/api/v1"

CACHE_TTL_SECONDS = 300
STALE_CACHE_SECONDS = 21600
UNIVERSE_TTL_SECONDS = 86400

# V6.4: rotate a curated liquid/learnable FX universe quickly.
LEARNING_UNIVERSE_SIZE = 80
LEARNING_CURRENCIES = [
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD",
    "NOK", "SEK", "DKK", "SGD", "HKD", "ZAR", "MXN", "PLN",
    "TRY", "CNH", "CZK", "HUF",
]
MAJOR_CURRENCIES = {"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD"}

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

# Rolling IG cache is keyed by canonical symbol + interval, intentionally
# independent of requested period. For live/demo learning we need a stable
# recent window rather than repeatedly downloading the same 1-month history.
_IG_HISTORY_CACHE: Dict[str, RouterCacheEntry] = {}
_IG_HISTORY_LOCK = threading.RLock()

_IG_DEMO_BROKER = IGDemoBroker()

_UNIVERSE_CACHE: List[dict] = []
_UNIVERSE_UPDATED_AT = 0.0
_UNIVERSE_LOCK = threading.RLock()

_TWELVE_CALL_TIMES = deque()
_TWELVE_DAY_KEY = None
_TWELVE_DAY_CALLS = 0
_TWELVE_RATE_LOCK = threading.RLock()

_PROVIDER_STATE = {
    "IG_DEMO": {
        "configured": bool(
            IG_DEMO_MARKET_DATA_ENABLED
            and _IG_DEMO_BROKER.configured()
        ),
        "healthy": None,
        "last_success": None,
        "last_error": None,
        "requests": 0,
        "historical_points": 0,
    },
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
# IG DEMO HISTORICAL MARKET DATA
# ============================================================

def get_ig_demo_broker() -> IGDemoBroker:
    """Return the shared strict DEMO broker used by data + execution."""
    return _IG_DEMO_BROKER


def _ig_resolution(
    interval: str,
) -> str:
    lookup = {
        "1m": "MINUTE",
        "2m": "MINUTE_2",
        "3m": "MINUTE_3",
        "5m": "MINUTE_5",
        "10m": "MINUTE_10",
        "15m": "MINUTE_15",
        "30m": "MINUTE_30",
        "60m": "HOUR",
        "1h": "HOUR",
        "2h": "HOUR_2",
        "3h": "HOUR_3",
        "4h": "HOUR_4",
        "1d": "DAY",
    }
    clean = str(interval or "").lower().strip()
    if clean not in lookup:
        raise RuntimeError(
            f"IG DEMO interval not supported: {interval}"
        )
    return lookup[clean]


def _ig_mid(
    value,
) -> Optional[float]:
    if not isinstance(value, dict):
        try:
            return float(value)
        except Exception:
            return None

    def number(key: str) -> Optional[float]:
        raw = value.get(key)
        if raw is None:
            return None
        try:
            return float(raw)
        except Exception:
            return None

    bid = number("bid")
    ask = number("ask")
    last = number("lastTraded")

    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    if last is not None:
        return last
    if bid is not None:
        return bid
    if ask is not None:
        return ask
    return None


def _ig_prices_to_frame(
    prices: list,
) -> pd.DataFrame:
    rows = []
    index = []

    for item in prices or []:
        if not isinstance(item, dict):
            continue

        timestamp = (
            item.get("snapshotTimeUTC")
            or item.get("snapshotTime")
        )
        parsed_time = pd.to_datetime(
            timestamp,
            utc=True,
            errors="coerce",
        )
        if pd.isna(parsed_time):
            continue

        open_price = _ig_mid(
            item.get("openPrice")
        )
        high_price = _ig_mid(
            item.get("highPrice")
        )
        low_price = _ig_mid(
            item.get("lowPrice")
        )
        close_price = _ig_mid(
            item.get("closePrice")
        )

        if any(
            value is None
            for value in (
                open_price,
                high_price,
                low_price,
                close_price,
            )
        ):
            continue

        try:
            volume = float(
                item.get("lastTradedVolume")
                or 0.0
            )
        except Exception:
            volume = 0.0

        index.append(parsed_time)
        rows.append({
            "Open": open_price,
            "High": high_price,
            "Low": low_price,
            "Close": close_price,
            "Volume": volume,
        })

    if not rows:
        return pd.DataFrame()

    frame = pd.DataFrame(
        rows,
        index=pd.DatetimeIndex(index),
    )
    frame.index.name = "datetime"

    return clean_ohlcv(frame)


def _ig_history_key(
    symbol: str,
    interval: str,
) -> str:
    return (
        f"{canonical_fx_symbol(symbol)}|"
        f"{str(interval).lower().strip()}"
    )


def download_ig_demo(
    symbol: str,
    period: str,
    interval: str,
) -> pd.DataFrame:
    """Fetch/refresh a small rolling IG DEMO candle window.

    First use warms up to 500 points, enough to build 30 completed H4 bars from
    M15 data. Subsequent refreshes request only one newest
    point every 30 minutes by default, conserving IG's historical-data quota.
    """
    if not IG_DEMO_MARKET_DATA_ENABLED:
        raise RuntimeError(
            "IG DEMO market-data provider disabled"
        )

    if not _IG_DEMO_BROKER.configured():
        raise RuntimeError(
            "IG DEMO credentials not configured"
        )

    key = _ig_history_key(
        symbol,
        interval,
    )
    now = time.time()

    with _IG_HISTORY_LOCK:
        existing_entry = (
            _IG_HISTORY_CACHE.get(key)
        )
        existing = (
            existing_entry.dataframe.copy()
            if existing_entry is not None
            else pd.DataFrame()
        )
        age = (
            now - existing_entry.created_at
            if existing_entry is not None
            else None
        )

    if (
        existing_entry is not None
        and len(existing) >= 80
        and age is not None
        and age < IG_DEMO_HISTORY_REFRESH_SECONDS
    ):
        return existing

    points = (
        1
        if len(existing) >= 80
        else IG_DEMO_WARMUP_POINTS
    )

    _provider_request("IG_DEMO")

    payload = _IG_DEMO_BROKER.historical_prices(
        canonical_fx_symbol(symbol),
        resolution=_ig_resolution(interval),
        num_points=points,
    )

    prices = payload.get("prices") or []
    fresh = _ig_prices_to_frame(prices)

    if fresh.empty:
        raise RuntimeError(
            f"IG DEMO returned no usable candles for "
            f"{canonical_fx_symbol(symbol)} {interval}"
        )

    if existing.empty:
        merged = fresh
    else:
        merged = pd.concat(
            [existing, fresh],
        )
        merged = merged[
            ~merged.index.duplicated(
                keep="last",
            )
        ].sort_index()

    merged = merged.tail(
        IG_DEMO_MAX_CACHE_ROWS
    )

    if len(merged) < 80:
        raise RuntimeError(
            f"IG DEMO returned only {len(merged)} usable rows "
            f"for {canonical_fx_symbol(symbol)} {interval}"
        )

    with _IG_HISTORY_LOCK:
        _IG_HISTORY_CACHE[key] = RouterCacheEntry(
            dataframe=merged.copy(),
            created_at=time.time(),
            source="IG_DEMO",
        )

    _PROVIDER_STATE["IG_DEMO"][
        "historical_points"
    ] += len(prices)

    _provider_success("IG_DEMO")

    # Also seed the router's normal period-specific cache.
    _cache_put(
        symbol,
        period,
        interval,
        merged,
        "IG_DEMO",
    )

    return merged.copy()


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
    cached = _cache_get(
        symbol,
        period,
        interval,
    )

    if cached is not None and len(cached) >= 80:
        return cached

    errors = []

    # V6.6.5: IG DEMO is primary because this is the actual broker/demo
    # environment the AI is executing against.
    if (
        IG_DEMO_MARKET_DATA_ENABLED
        and _IG_DEMO_BROKER.configured()
    ):
        try:
            return download_ig_demo(
                symbol,
                period,
                interval,
            )
        except Exception as exc:
            _provider_failure(
                "IG_DEMO",
                exc,
            )
            errors.append(
                f"IG DEMO: {exc}"
            )

    # Historical fallbacks remain available.
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
            _provider_failure(
                "TWELVE_DATA",
                exc,
            )
            errors.append(
                f"Twelve Data: {exc}"
            )

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
        _provider_failure(
            "YAHOO",
            exc,
        )
        errors.append(
            f"Yahoo: {exc}"
        )

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


def get_learning_universe(
    limit: int = LEARNING_UNIVERSE_SIZE,
) -> List[dict]:
    """Return a curated fiat-FX universe for fast PAPER learning.

    The full provider universe remains available via get_forex_universe().
    This shortlist intentionally excludes metals/commodities such as XAU/XAG
    and prioritises pairs that contain major currencies.
    """
    universe = get_forex_universe()
    allowed = set(LEARNING_CURRENCIES)
    rows = []
    for item in universe:
        symbol = str(item.get("symbol") or "").upper()
        if "/" not in symbol:
            continue
        base, quote = symbol.split("/", 1)
        if base not in allowed or quote not in allowed or base == quote:
            continue
        major_count = int(base in MAJOR_CURRENCIES) + int(quote in MAJOR_CURRENCIES)
        usd_bonus = 1 if "USD" in {base, quote} else 0
        item = dict(item)
        item["learning_priority"] = major_count * 10 + usd_bonus * 3
        rows.append(item)

    # Dedupe by displayed pair and prefer high-priority, provider-return order.
    unique = {}
    for item in rows:
        unique.setdefault(item["symbol"], item)
    rows = list(unique.values())
    rows.sort(key=lambda x: (x.get("learning_priority", 0), x.get("symbol", "")), reverse=True)
    return rows[: max(1, min(int(limit), LEARNING_UNIVERSE_SIZE))]


def get_discovery_market_data(
    symbol: str,
    period: str = "5d",
    interval: str = "15m",
) -> pd.DataFrame:
    """V6.6.5 discovery: cache -> IG DEMO -> Yahoo -> Twelve -> stale.

    Auto Manager is restricted to the nine core IG FX pairs during Phase 1,
    so using IG here stays within the historical data budget.
    """
    cached = _cache_get(
        symbol,
        period,
        interval,
    )
    if cached is not None and len(cached) >= 80:
        return cached

    errors = []

    if (
        IG_DEMO_MARKET_DATA_ENABLED
        and _IG_DEMO_BROKER.configured()
    ):
        try:
            return download_ig_demo(
                symbol,
                period,
                interval,
            )
        except Exception as exc:
            _provider_failure(
                "IG_DEMO",
                exc,
            )
            errors.append(
                f"IG DEMO: {exc}"
            )

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
            "YAHOO_DISCOVERY",
        )
        return data
    except Exception as exc:
        _provider_failure(
            "YAHOO",
            exc,
        )
        errors.append(
            f"Yahoo: {exc}"
        )

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
                "TWELVE_DATA_DISCOVERY_FALLBACK",
            )
            return data
        except Exception as exc:
            _provider_failure(
                "TWELVE_DATA",
                exc,
            )
            errors.append(
                f"Twelve Data: {exc}"
            )

    stale = _cache_get(
        symbol,
        period,
        interval,
        stale=True,
    )
    if stale is not None and len(stale) >= 80:
        return stale

    raise RuntimeError(
        "All V6.6.5 discovery routes failed for "
        f"{symbol} {period} {interval}. "
        + " | ".join(errors)
    )


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
            "IG_DEMO",
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
        "ig_demo_market_data": {
            "enabled": IG_DEMO_MARKET_DATA_ENABLED,
            "configured": _IG_DEMO_BROKER.configured(),
            "rolling_cache_entries": len(
                _IG_HISTORY_CACHE
            ),
            "refresh_seconds":
                IG_DEMO_HISTORY_REFRESH_SECONDS,
            "warmup_points":
                IG_DEMO_WARMUP_POINTS,
        },
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
