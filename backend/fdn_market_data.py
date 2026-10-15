"""Optional FinancialData.Net/fdnpy intraday analysis source.

The SDK's endpoint methods are retained. Its unbounded transport and pagination
are replaced so an outage cannot hang the trading worker or log a keyed URL.
"""
from __future__ import annotations

import importlib.util
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd


class FDNDataError(RuntimeError):
    pass


class FinancialDataProvider:
    SOURCE = "FINANCIALDATA_NET_FDNPY"
    CRYPTO = {"BITCOIN": "BTCUSD", "ETHER": "ETHUSD", "SOLANA": "SOLUSD", "XRP": "XRPUSD", "LITECOIN": "LTCUSD"}

    def __init__(self, *, api_key: str | None = None, client: Any = None) -> None:
        self._api_key = api_key if api_key is not None else os.getenv("FDN_API_KEY", "")
        self._client = client
        self._lock = threading.RLock()
        self._cache: dict[tuple, tuple[float, pd.DataFrame]] = {}
        self._days: dict[tuple, tuple[float, list]] = {}
        self._calls: deque[float] = deque()
        self._day = ""
        self._day_calls = 0
        self._cooldown_until = 0.0
        self._last_error: str | None = None
        self._last_success: float | None = None
        self.lookback_days = max(4, min(14, int(os.getenv("FDN_LOOKBACK_DAYS", "7"))))
        self.ttl = max(60, min(900, int(os.getenv("FDN_CACHE_SECONDS", "300"))))
        self.max_pages = max(1, min(10, int(os.getenv("FDN_MAX_PAGES_PER_DAY", "6"))))
        self.max_daily = max(1, int(os.getenv("FDN_MAX_REQUESTS_PER_DAY", "300")))
        self.max_minute = max(1, min(60, int(os.getenv("FDN_MAX_REQUESTS_PER_MINUTE", "30"))))
        self.timeout = max(1.0, min(20.0, float(os.getenv("FDN_TIMEOUT_SECONDS", "8"))))

    @property
    def configured(self) -> bool:
        return bool(self._api_key or self._client is not None)

    def identifier(self, seed: dict[str, Any]) -> tuple[str, str] | None:
        category = str(seed.get("category") or "").upper()
        key = str(seed.get("key") or "").upper()
        if category == "FOREX" and len(key) == 6 and key.isalpha():
            return "get_forex_minute_prices", key
        if category == "CRYPTO" and key in self.CRYPTO:
            return "get_crypto_minute_prices", self.CRYPTO[key]
        if category == "SHARES" and key.isalnum():
            return "get_minute_prices", key
        # Commodity/index daily prices are never represented as M15 candles.
        return None

    def _reserve_call(self) -> None:
        now = time.time()
        day = datetime.now(timezone.utc).date().isoformat()
        if day != self._day:
            self._day, self._day_calls = day, 0
        while self._calls and now-self._calls[0] >= 60:
            self._calls.popleft()
        if now < self._cooldown_until:
            raise FDNDataError("FDN provider cooling down")
        if self._day_calls >= self.max_daily or len(self._calls) >= self.max_minute:
            raise FDNDataError("FDN request budget reached")
        self._day_calls += 1
        self._calls.append(now)

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from fdnpy import FinancialDataClient
        except ImportError:
            raise FDNDataError("fdnpy is not installed") from None
        owner = self

        class BoundedClient(FinancialDataClient):
            def make_request(self, endpoint, params):
                owner._reserve_call()
                try:
                    response = self.session.get(self.base_url+endpoint, params={**params, "key": self.api_key}, timeout=(3.0, owner.timeout))
                except Exception:
                    owner._cooldown_until = time.time()+60
                    raise FDNDataError("FDN transport unavailable") from None
                if response.status_code != 200:
                    owner._cooldown_until = time.time()+(900 if response.status_code in {401, 403} else 60)
                    raise FDNDataError(f"FDN HTTP {response.status_code}; check subscription/key or retry later") from None
                try:
                    payload = response.json()
                except Exception:
                    raise FDNDataError("FDN returned invalid JSON") from None
                if not isinstance(payload, list) or any(not isinstance(r, dict) for r in payload):
                    raise FDNDataError("FDN returned an unexpected data shape")
                return response.headers.get("X-Record-Limit"), payload

            def get_data(self, endpoint, params=None, limit=300):
                result, offset = [], 0
                for _ in range(owner.max_pages):
                    record_limit, page = self.make_request(endpoint, {**(params or {}), "offset": offset})
                    result.extend(page)
                    try:
                        page_size = int(record_limit or limit)
                    except (TypeError, ValueError):
                        raise FDNDataError("FDN returned an invalid page limit") from None
                    if page_size <= 0:
                        raise FDNDataError("FDN returned an invalid page limit")
                    if len(page) < page_size:
                        return result
                    offset += page_size
                raise FDNDataError("FDN page budget exhausted; incomplete day rejected")

        self._client = BoundedClient(api_key=self._api_key)
        return self._client

    @staticmethod
    def normalize(records: list[dict[str, Any]], identifier: str, now: pd.Timestamp) -> pd.DataFrame:
        rows = []
        for record in records:
            if str(record.get("trading_symbol") or "").upper() != identifier:
                raise FDNDataError("FDN symbol mismatch")
            if not record.get("time"):
                raise FDNDataError("FDN intraday timestamp missing")
            rows.append({"Time": record["time"], **{c: record.get(c.lower()) for c in ("Open", "High", "Low", "Close", "Volume")}})
        frame = pd.DataFrame(rows)
        if frame.empty:
            raise FDNDataError("FDN has no intraday records")
        frame.index = pd.to_datetime(frame.pop("Time"), utc=True, errors="coerce")
        if frame.index.isna().any() or any(frame.index.asi8 % pd.Timedelta(minutes=1).value):
            raise FDNDataError("FDN has invalid minute timestamps")
        for col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
        frame.Volume = frame.Volume.fillna(0.0)
        ohlc = frame[["Open", "High", "Low", "Close"]]
        if (not np.isfinite(ohlc).all().all() or (ohlc <= 0).any().any()
                or (frame.High < frame[["Open", "Close", "Low"]].max(axis=1)).any()
                or (frame.Low > frame[["Open", "Close", "High"]].min(axis=1)).any()
                or not np.isfinite(frame.Volume).all() or (frame.Volume < 0).any()):
            raise FDNDataError("FDN has invalid OHLCV values")
        frame = frame[~frame.index.duplicated(keep="last")].sort_index()
        frame = frame[frame.index+pd.Timedelta(minutes=1) <= now]
        grouped = frame.resample("15min", label="left", closed="left", origin="start_day")
        bars = grouped.agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
        bars = bars[(grouped.Close.count() == 15) & (bars.index+pd.Timedelta(minutes=15) <= now)].dropna()
        if len(bars) < 80:
            raise FDNDataError("FDN has insufficient complete M15 history")
        bars.attrs["analysis_price_source"] = FinancialDataProvider.SOURCE
        bars.attrs["bar_interval_seconds"] = 900
        return bars

    def load(self, seed: dict[str, Any], *, now: Any = None) -> pd.DataFrame:
        mapping = self.identifier(seed)
        if not self.configured or mapping is None:
            raise FDNDataError("FDN is unconfigured or this market has no supported intraday endpoint")
        stamp = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
        stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
        with self._lock:
            cached = self._cache.get(mapping)
            if cached and time.time()-cached[0] < self.ttl:
                return cached[1].copy()
            try:
                client = self._get_client()
                records = []
                # Oldest first: a request budget failure is explicit, never a
                # silently truncated history that happens to look current.
                for offset in reversed(range(self.lookback_days)):
                    day = (stamp-pd.Timedelta(days=offset)).date()
                    if mapping[0] == "get_forex_minute_prices" and day.weekday() >= 5:
                        continue
                    key = (*mapping, day.isoformat())
                    saved = self._days.get(key)
                    if saved and (day < stamp.date() or time.time()-saved[0] < self.ttl):
                        page = saved[1]
                    else:
                        page = getattr(client, mapping[0])(identifier=mapping[1], date=day.isoformat())
                        if not isinstance(page, list):
                            raise FDNDataError("FDN endpoint returned non-list data")
                        self._days[key] = (time.time(), page)
                    records.extend(page)
                frame = self.normalize(records, mapping[1], stamp)
                self._cache[mapping] = (time.time(), frame)
                cutoff = (stamp-pd.Timedelta(days=self.lookback_days+1)).date().isoformat()
                self._days = {k: v for k, v in self._days.items() if k[-1] >= cutoff}
                self._last_success, self._last_error = time.time(), None
                return frame.copy()
            except Exception as exc:
                # Even an injected or future SDK implementation cannot expose
                # the query-string API key through status or outer exceptions.
                message = str(exc) if isinstance(exc, FDNDataError) else "FDN endpoint unavailable"
                if self._api_key:
                    message = message.replace(self._api_key, "[REDACTED]")
                self._last_error = message[:180]
                raise FDNDataError(self._last_error) from None

    def status(self) -> dict[str, Any]:
        return {"provider": self.SOURCE, "configured": self.configured,
                "sdk_installed": importlib.util.find_spec("fdnpy") is not None,
                "role": "ANALYSIS_CANDLES_ONLY", "supported_intraday_categories": ["FOREX", "CRYPTO", "SHARES"],
                "forex_subscription": "PREMIUM", "last_error": self._last_error, "last_success_at": self._last_success,
                "requests_today": self._day_calls, "request_budget_day": self._day or None,
                "max_requests_per_day": self.max_daily, "cache_entries": len(self._cache),
                "cooldown_active": time.time() < self._cooldown_until, "live_money_execution": False}
