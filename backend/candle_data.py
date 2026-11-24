"""Strict candle normalization shared by broker, caches and regime analysis."""
from __future__ import annotations

from numbers import Number
from typing import Any

import numpy as np
import pandas as pd

OHLC = ["Open", "High", "Low", "Close"]
OHLCV = [*OHLC, "Volume"]
IG_INTERVALS = {
    "MINUTE": "1min", "MINUTE_2": "2min", "MINUTE_3": "3min", "MINUTE_5": "5min",
    "MINUTE_10": "10min", "MINUTE_15": "15min", "MINUTE_30": "30min",
    "HOUR": "1h", "HOUR_2": "2h", "HOUR_3": "3h", "HOUR_4": "4h",
}


class CandleValidationError(ValueError):
    """Reject a batch rather than silently repair or discard bad market data."""


def utc_timestamp(value: Any, *, naive_timezone: str | None = "UTC") -> pd.Timestamp:
    if value is None or isinstance(value, Number):
        raise CandleValidationError("Invalid candle timestamp")
    try:
        stamp = pd.Timestamp(value)
        if pd.isna(stamp):
            raise ValueError("missing timestamp")
        if stamp.tzinfo is None:
            if not naive_timezone:
                raise ValueError("timezone is unknown")
            stamp = stamp.tz_localize(naive_timezone, ambiguous="raise", nonexistent="raise")
        return stamp.tz_convert("UTC")
    except Exception as exc:
        raise CandleValidationError("Invalid candle timestamp or unknown timezone") from exc


def normalize_candles(
    raw: pd.DataFrame, *, interval: str | None = None,
    now: Any = None, closed_only: bool = False,
) -> pd.DataFrame:
    """Preserve OHLC; normalize UTC, validate, deduplicate and sort.

    Naive provider-frame indexes have the existing UTC contract. IG's local
    snapshotTime is handled separately and never assumed to be UTC.
    Missing volume means unknown activity (zero), never synthetic liquidity.
    """
    if not isinstance(raw, pd.DataFrame) or not all(c in raw for c in OHLC):
        raise CandleValidationError("Missing required OHLC columns")
    frame = raw.copy()
    try:
        if isinstance(frame.index, pd.DatetimeIndex):
            frame.index = pd.to_datetime(frame.index, utc=True)
        else:
            frame.index = pd.DatetimeIndex([utc_timestamp(t) for t in frame.index])
    except Exception as exc:
        raise CandleValidationError("Invalid candle timestamps") from exc
    if frame.index.isna().any():
        raise CandleValidationError("Invalid candle timestamps; require aligned M15 candle opens")
    duration = pd.Timedelta(interval) if interval is not None else None
    if duration is not None:
        if duration <= pd.Timedelta(0):
            raise CandleValidationError("Candle interval must be positive")
        if (frame.index != frame.index.floor(duration)).any():
            raise CandleValidationError("Invalid candle boundaries; require aligned M15 candle opens" if duration == pd.Timedelta(minutes=15)
                                        else "Invalid candle opening boundaries")
    if "Volume" not in frame:
        frame["Volume"] = 0.0
    for col in OHLCV:
        if frame[col].map(lambda x: isinstance(x, (bool, np.bool_, complex))).any():
            raise CandleValidationError("Invalid OHLCV values")
        try:
            frame[col] = pd.to_numeric(frame[col], errors="raise")
        except (ValueError, TypeError) as exc:
            raise CandleValidationError("Invalid OHLCV values") from exc
    frame["Volume"] = frame.Volume.fillna(0.0)
    prices = frame[OHLC]
    valid = (np.isfinite(prices).all(axis=1) & (prices > 0).all(axis=1)
             & (frame.High >= prices.max(axis=1)) & (frame.Low <= prices.min(axis=1)))
    if not valid.all() or not np.isfinite(frame.Volume).all() or (frame.Volume < 0).any():
        raise CandleValidationError("Invalid OHLCV values")
    duplicates = frame.index.duplicated(keep=False)
    if duplicates.any():
        counts = frame.loc[duplicates, OHLCV].groupby(level=0).nunique(dropna=False)
        if (counts > 1).any().any():
            raise CandleValidationError("Conflicting duplicate candle timestamps")
        frame = frame[~frame.index.duplicated(keep="first")]
    frame = frame.sort_index()
    if closed_only:
        if duration is None:
            raise CandleValidationError("Closed-candle filtering requires an interval")
        stamp = pd.Timestamp.now(tz="UTC") if now is None else utc_timestamp(now)
        frame = frame[frame.index + duration <= stamp]
    return frame


def _ig_timestamp(row: dict) -> pd.Timestamp:
    if row.get("snapshotTimeUTC") is not None:
        return utc_timestamp(row["snapshotTimeUTC"])
    # An explicitly offset legacy timestamp is usable; an unspecified local
    # timezone is not. Both broker methods request IG v3's UTC field.
    return utc_timestamp(row.get("snapshotTime"), naive_timezone=None)


def _ig_number(value: Any) -> float:
    try:
        number = float(value)
    except (ValueError, TypeError) as exc:
        raise CandleValidationError("Invalid IG OHLC quote") from exc
    if isinstance(value, (bool, np.bool_)) or not np.isfinite(number) or number <= 0:
        raise CandleValidationError("Invalid IG OHLC quote")
    return number


def _ig_mid(value: Any) -> float:
    if not isinstance(value, dict):
        return _ig_number(value)
    quotes = {key: _ig_number(value[key]) for key in ("bid", "ask", "lastTraded") if value.get(key) is not None}
    if "bid" in quotes and "ask" in quotes:
        if quotes["bid"] > quotes["ask"]:
            raise CandleValidationError("Crossed IG OHLC quote")
        return quotes["bid"] / 2 + quotes["ask"] / 2
    for key in ("lastTraded", "bid", "ask"):
        if key in quotes:
            return quotes[key]
    raise CandleValidationError("Missing IG OHLC quote")


def ig_prices_to_frame(prices: list, *, interval: str | None = None, now: Any = None,
                       closed_only: bool = False) -> pd.DataFrame:
    rows, timestamps = [], []
    for item in prices:
        if not isinstance(item, dict):
            raise CandleValidationError("Invalid IG candle record")
        timestamps.append(_ig_timestamp(item))
        fields = [item.get(c.lower() + "Price") for c in OHLC]
        # Validate each complete side as well as the analysis midpoint. A
        # malformed bid/ask high must not be hidden by averaging the sides.
        for side in ("bid", "ask", "lastTraded"):
            if all(isinstance(p, dict) and p.get(side) is not None for p in fields):
                o, h, l, c = [_ig_number(p[side]) for p in fields]
                if h < max(o, c, l) or l > min(o, c, h):
                    raise CandleValidationError("Invalid IG OHLC quote geometry")
        rows.append({**dict(zip(OHLC, [_ig_mid(p) for p in fields])),
                     "Volume": item.get("lastTradedVolume")})
    frame = pd.DataFrame(rows, columns=OHLCV, index=pd.DatetimeIndex(timestamps, tz="UTC", name="datetime"))
    return normalize_candles(frame, interval=interval, now=now, closed_only=closed_only)


def normalized_ig_prices(prices: list, *, interval: str | None = None) -> list[dict]:
    """Return validated, unique, chronological broker rows with UTC strings.

    Retain forming rows in this transport response: regime analysis filters by
    actual close time; the independent weekend engine retains its final-bar veto.
    """
    frame = ig_prices_to_frame(prices, interval=interval)
    records, signatures = {}, {}
    for item in prices:
        stamp = _ig_timestamp(item)
        signature = []
        for col in OHLC:
            quote = item.get(col.lower() + "Price")
            signature.append(tuple((key, _ig_number(quote[key])) for key in ("bid", "ask", "lastTraded")
                                   if quote.get(key) is not None) if isinstance(quote, dict) else _ig_number(quote))
        if stamp in signatures and signatures[stamp] != signature:
            raise CandleValidationError("Conflicting duplicate IG quote timestamps")
        signatures[stamp] = signature
        record = {**item, "snapshotTimeUTC": stamp.isoformat()}
        if "snapshotTime" in record:
            record["snapshotTime"] = stamp.isoformat()
        records.setdefault(stamp, record)
    return [records[t] for t in frame.index]
