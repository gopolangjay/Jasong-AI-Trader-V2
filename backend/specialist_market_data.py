from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd
import yfinance as yf

import market_data_router as market_router


class ResilientSpecialistMarketData:
    """V6.9.4 resilient analysis-data layer.

    This class changes DATA ACQUISITION ONLY.

    It does not change:
      * category strategy selection;
      * Quant / Model-AI / FAST thresholds;
      * STRONG qualification;
      * JSCAT execution;
      * forward-validation thresholds;
      * PRIME qualification;
      * Compound rules;
      * IG DEMO execution ownership.

    Data policy:
      FOREX:
        normal Jasong router
        IG DEMO -> Twelve Data -> Yahoo -> router stale cache
        plus persistent specialist cache as final continuity fallback.

      NON-FOREX:
        persistent cache -> serialized Yahoo request -> stale persistent cache.

    Yahoo is never hammered concurrently. One rate-limit incident activates
    a shared cooldown for the specialist public-data path.
    """

    VERSION = "6.9.4-forward-data-resilience"

    def __init__(self, state_dir: Optional[str] = None) -> None:
        base_dir = (
            state_dir
            or ("/var/data" if os.path.isdir("/var/data") else "/tmp")
        )
        self.cache_dir = Path(
            os.getenv(
                "SPECIALIST_MARKET_CACHE_DIR",
                str(Path(base_dir) / "jasong_specialist_market_cache"),
            )
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.fresh_ttl_seconds = int(
            max(
                300,
                min(
                    3600,
                    float(
                        os.getenv(
                            "GLOBAL_DATA_TTL_SECONDS",
                            "900",
                        )
                    ),
                ),
            )
        )
        self.stale_max_age_seconds = int(
            max(
                900,
                min(
                    86400,
                    float(
                        os.getenv(
                            "GLOBAL_DATA_STALE_MAX_SECONDS",
                            "21600",
                        )
                    ),
                ),
            )
        )
        self.yahoo_cooldown_seconds = int(
            max(
                300,
                min(
                    3600,
                    float(
                        os.getenv(
                            "YAHOO_RATE_LIMIT_COOLDOWN_SECONDS",
                            "900",
                        )
                    ),
                ),
            )
        )
        self.yahoo_general_failure_cooldown_seconds = int(
            max(
                30,
                min(
                    600,
                    float(
                        os.getenv(
                            "YAHOO_GENERAL_FAILURE_COOLDOWN_SECONDS",
                            "120",
                        )
                    ),
                ),
            )
        )
        self.yahoo_min_gap_seconds = float(
            max(
                2.5,
                min(
                    30.0,
                    float(
                        os.getenv(
                            "YAHOO_MIN_REQUEST_GAP_SECONDS",
                            "5",
                        )
                    ),
                ),
            )
        )
        self.forex_period = os.getenv(
            "SPECIALIST_FOREX_PERIOD",
            "1mo",
        ).strip()
        self.forex_interval = os.getenv(
            "SPECIALIST_FOREX_INTERVAL",
            "15m",
        ).strip()
        self.global_period = os.getenv(
            "SPECIALIST_GLOBAL_PERIOD",
            "1mo",
        ).strip()
        self.global_interval = os.getenv(
            "SPECIALIST_GLOBAL_INTERVAL",
            "15m",
        ).strip()

        self._cache_lock = threading.RLock()
        self._yahoo_lock = threading.Lock()
        self._source_lock = threading.RLock()

        self._memory: Dict[str, tuple[float, pd.DataFrame, str]] = {}
        self._last_source: Dict[str, str] = {}
        self._last_success: Dict[str, float] = {}
        self._last_error: Dict[str, str] = {}

        self._last_yahoo_request_at = 0.0
        self._yahoo_cooldown_until = 0.0
        self._original_loader = None
        self._installed = False

    # ------------------------------------------------------------------
    # Keys / source telemetry
    # ------------------------------------------------------------------

    @staticmethod
    def _seed_key(seed: Dict[str, Any]) -> str:
        value = (
            seed.get("key")
            or seed.get("symbol")
            or seed.get("name")
            or seed.get("analysis_symbol")
            or "UNKNOWN"
        )
        return "".join(
            ch for ch in str(value).upper()
            if ch.isalnum()
        ) or "UNKNOWN"

    @staticmethod
    def _category(seed: Dict[str, Any]) -> str:
        return str(seed.get("category") or "").upper().strip()

    def _analysis_symbol(self, seed: Dict[str, Any]) -> str:
        if self._category(seed) == "FOREX":
            return str(
                seed.get("ig_symbol")
                or seed.get("key")
                or seed.get("analysis_symbol")
                or ""
            ).strip()
        return str(
            seed.get("analysis_symbol")
            or seed.get("symbol")
            or seed.get("key")
            or ""
        ).strip()

    def _record_success(
        self,
        seed: Dict[str, Any],
        source: str,
    ) -> None:
        key = self._seed_key(seed)
        with self._source_lock:
            self._last_source[key] = str(source or "UNAVAILABLE").upper().strip()
            self._last_success[key] = time.time()
            self._last_error.pop(key, None)

    def _record_error(
        self,
        seed: Dict[str, Any],
        exc: Exception,
    ) -> None:
        key = self._seed_key(seed)
        with self._source_lock:
            self._last_error[key] = f"{type(exc).__name__}: {exc}"

    def source_for(self, seed: Dict[str, Any]) -> str:
        key = self._seed_key(seed)
        with self._source_lock:
            return self._last_source.get(
                key,
                "DYNAMIC_ROUTED_PER_MARKET",
            )

    # ------------------------------------------------------------------
    # Persistent cache
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_name(value: str) -> str:
        clean = str(value or "UNKNOWN").upper()
        return "".join(
            ch if ch.isalnum() else "_"
            for ch in clean
        )

    def _cache_id(self, seed: Dict[str, Any]) -> str:
        return (
            f"{self._category(seed) or 'UNKNOWN'}__"
            f"{self._safe_name(self._analysis_symbol(seed))}"
        )

    def _data_path(self, seed: Dict[str, Any]) -> Path:
        return self.cache_dir / f"{self._cache_id(seed)}.pkl"

    def _meta_path(self, seed: Dict[str, Any]) -> Path:
        return self.cache_dir / f"{self._cache_id(seed)}.json"

    @staticmethod
    def _clean(
        frame: pd.DataFrame,
        *,
        min_rows: int,
        label: str,
    ) -> pd.DataFrame:
        if frame is None or frame.empty:
            raise ValueError(f"No usable market data for {label}")

        data = frame.copy()

        if isinstance(data.columns, pd.MultiIndex):
            data.columns = [
                str(col[0])
                for col in data.columns
            ]

        data = data.rename(
            columns={"Adj Close": "Adj_Close"}
        )

        required = ["Open", "High", "Low", "Close"]
        missing = [
            column
            for column in required
            if column not in data.columns
        ]
        if missing:
            raise ValueError(
                f"Missing OHLC columns for {label}: {missing}"
            )

        if "Volume" not in data.columns:
            data["Volume"] = 0.0

        keep = ["Open", "High", "Low", "Close", "Volume"]
        data = data[keep].copy()

        for column in keep:
            data[column] = pd.to_numeric(
                data[column],
                errors="coerce",
            )

        data = data.dropna(
            subset=required
        ).sort_index()

        if len(data) < int(min_rows):
            raise ValueError(
                f"Insufficient data for {label}: "
                f"{len(data)} rows < {min_rows}"
            )

        return data

    def _memory_get(
        self,
        seed: Dict[str, Any],
        *,
        allow_stale: bool,
    ) -> Optional[tuple[pd.DataFrame, str]]:
        key = self._cache_id(seed)
        now = time.time()
        with self._cache_lock:
            item = self._memory.get(key)
            if item is None:
                return None
            created_at, frame, source = item
            max_age = (
                self.stale_max_age_seconds
                if allow_stale
                else self.fresh_ttl_seconds
            )
            if now - float(created_at) > max_age:
                return None
            return frame.copy(), source

    def _memory_put(
        self,
        seed: Dict[str, Any],
        frame: pd.DataFrame,
        source: str,
    ) -> None:
        key = self._cache_id(seed)
        with self._cache_lock:
            self._memory[key] = (
                time.time(),
                frame.copy(),
                str(source),
            )

    def _disk_get(
        self,
        seed: Dict[str, Any],
        *,
        allow_stale: bool,
        min_rows: int,
    ) -> Optional[tuple[pd.DataFrame, str]]:
        data_path = self._data_path(seed)
        meta_path = self._meta_path(seed)

        if not data_path.is_file():
            return None

        try:
            age = time.time() - data_path.stat().st_mtime
            max_age = (
                self.stale_max_age_seconds
                if allow_stale
                else self.fresh_ttl_seconds
            )
            if age > max_age:
                return None

            frame = pd.read_pickle(data_path)
            frame = self._clean(
                frame,
                min_rows=min_rows,
                label=self._analysis_symbol(seed),
            )

            source = "PERSISTENT_CACHE"
            if meta_path.is_file():
                try:
                    payload = json.loads(
                        meta_path.read_text(
                            encoding="utf-8"
                        )
                    )
                    underlying = str(
                        payload.get("source")
                        or ""
                    ).upper().strip()
                    if underlying:
                        source = (
                            "PERSISTENT_CACHE:"
                            + underlying
                        )
                except Exception:
                    pass

            return frame, source

        except Exception:
            return None

    def _disk_put(
        self,
        seed: Dict[str, Any],
        frame: pd.DataFrame,
        source: str,
    ) -> None:
        data_path = self._data_path(seed)
        meta_path = self._meta_path(seed)

        try:
            frame.to_pickle(data_path)
            meta_path.write_text(
                json.dumps(
                    {
                        "version": self.VERSION,
                        "saved_at": time.time(),
                        "source": str(source),
                        "category": self._category(seed),
                        "analysis_symbol": self._analysis_symbol(seed),
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
        except Exception:
            # Persistence failure must not stop intelligence.
            pass

    def _store(
        self,
        seed: Dict[str, Any],
        frame: pd.DataFrame,
        source: str,
    ) -> pd.DataFrame:
        self._memory_put(
            seed,
            frame,
            source,
        )
        self._disk_put(
            seed,
            frame,
            source,
        )
        self._record_success(
            seed,
            source,
        )
        return frame.copy()

    # ------------------------------------------------------------------
    # Yahoo controls
    # ------------------------------------------------------------------

    @staticmethod
    def _looks_rate_limited(exc: Exception) -> bool:
        text = str(exc).lower()
        return any(
            marker in text
            for marker in (
                "too many requests",
                "rate limit",
                "yfratelimiterror",
                "429",
            )
        )

    def _activate_cooldown(
        self,
        seconds: int,
    ) -> None:
        self._yahoo_cooldown_until = max(
            self._yahoo_cooldown_until,
            time.time() + int(seconds),
        )

    def _wait_yahoo_slot(self) -> None:
        now = time.time()
        elapsed = now - self._last_yahoo_request_at
        if elapsed < self.yahoo_min_gap_seconds:
            time.sleep(
                self.yahoo_min_gap_seconds - elapsed
            )
        self._last_yahoo_request_at = time.time()

    def _stale_fallback(
        self,
        seed: Dict[str, Any],
        *,
        min_rows: int,
    ) -> Optional[pd.DataFrame]:
        memory = self._memory_get(
            seed,
            allow_stale=True,
        )
        if memory is not None:
            frame, source = memory
            cache_source = f"STALE_MEMORY:{source}"
            self._record_success(
                seed,
                cache_source,
            )
            return frame

        disk = self._disk_get(
            seed,
            allow_stale=True,
            min_rows=min_rows,
        )
        if disk is not None:
            frame, source = disk
            self._memory_put(
                seed,
                frame,
                source,
            )
            self._record_success(
                seed,
                source,
            )
            return frame

        return None

    def _yahoo_global(
        self,
        seed: Dict[str, Any],
    ) -> pd.DataFrame:
        ticker = self._analysis_symbol(seed)
        if not ticker:
            raise ValueError(
                "Global market has no analysis symbol"
            )

        min_rows = 180

        memory = self._memory_get(
            seed,
            allow_stale=False,
        )
        if memory is not None:
            frame, source = memory
            self._record_success(seed, source)
            return frame

        disk = self._disk_get(
            seed,
            allow_stale=False,
            min_rows=min_rows,
        )
        if disk is not None:
            frame, source = disk
            self._memory_put(seed, frame, source)
            self._record_success(seed, source)
            return frame

        now = time.time()
        if now < self._yahoo_cooldown_until:
            stale = self._stale_fallback(
                seed,
                min_rows=min_rows,
            )
            if stale is not None:
                return stale

            remaining = max(
                0,
                int(
                    self._yahoo_cooldown_until
                    - now
                ),
            )
            raise RuntimeError(
                "Yahoo specialist data cooling down "
                f"({remaining}s remaining) for {ticker}"
            )

        with self._yahoo_lock:
            # Another thread may have filled cache while this caller waited.
            memory = self._memory_get(
                seed,
                allow_stale=False,
            )
            if memory is not None:
                frame, source = memory
                self._record_success(seed, source)
                return frame

            now = time.time()
            if now < self._yahoo_cooldown_until:
                stale = self._stale_fallback(
                    seed,
                    min_rows=min_rows,
                )
                if stale is not None:
                    return stale
                raise RuntimeError(
                    "Yahoo specialist rate-limit cooldown active"
                )

            self._wait_yahoo_slot()

            try:
                raw = yf.download(
                    ticker,
                    period=self.global_period,
                    interval=self.global_interval,
                    auto_adjust=False,
                    progress=False,
                    threads=False,
                )

                # yfinance can swallow YFRateLimitError and return empty data.
                # Empty data on this 1mo/15m specialist request is therefore
                # treated as a provider failure and starts a cooldown.
                if raw is None or raw.empty:
                    self._activate_cooldown(
                        self.yahoo_cooldown_seconds
                    )
                    raise RuntimeError(
                        "Yahoo returned empty data; "
                        "rate-limit/provider cooldown activated"
                    )

                frame = self._clean(
                    raw,
                    min_rows=min_rows,
                    label=ticker,
                )

                return self._store(
                    seed,
                    frame,
                    "YAHOO_FINANCE",
                )

            except Exception as exc:
                if self._looks_rate_limited(exc):
                    self._activate_cooldown(
                        self.yahoo_cooldown_seconds
                    )
                elif (
                    self._yahoo_cooldown_until
                    <= time.time()
                ):
                    self._activate_cooldown(
                        self.yahoo_general_failure_cooldown_seconds
                    )

                stale = self._stale_fallback(
                    seed,
                    min_rows=min_rows,
                )
                if stale is not None:
                    return stale

                self._record_error(
                    seed,
                    exc,
                )
                raise RuntimeError(
                    "Public analysis data temporarily unavailable "
                    f"for {ticker}: {type(exc).__name__}: {exc}"
                ) from exc

    # ------------------------------------------------------------------
    # FOREX through the existing Jasong router
    # ------------------------------------------------------------------

    def _router_source(
        self,
        symbol: str,
    ) -> str:
        """Best-effort exact provider label from Jasong router cache.

        Falls back to MARKET_DATA_ROUTER if router internals change.
        """
        try:
            key = market_router._cache_key(
                symbol,
                self.forex_period,
                self.forex_interval,
            )
            with market_router._CACHE_LOCK:
                entry = market_router._CACHE.get(key)
                if entry is not None:
                    provider = str(
                        entry.source or ""
                    ).upper().strip()
                    if provider:
                        return (
                            "MARKET_ROUTER:"
                            + provider
                        )
        except Exception:
            pass
        return "MARKET_DATA_ROUTER"

    def _forex(
        self,
        seed: Dict[str, Any],
    ) -> pd.DataFrame:
        symbol = self._analysis_symbol(seed)
        if not symbol:
            raise ValueError(
                "FOREX specialist seed has no usable symbol"
            )

        min_rows = 80

        memory = self._memory_get(
            seed,
            allow_stale=False,
        )
        if memory is not None:
            frame, source = memory
            self._record_success(seed, source)
            return frame

        disk = self._disk_get(
            seed,
            allow_stale=False,
            min_rows=min_rows,
        )
        if disk is not None:
            frame, source = disk
            self._memory_put(seed, frame, source)
            self._record_success(seed, source)
            return frame

        try:
            raw = market_router.get_market_data(
                symbol,
                period=self.forex_period,
                interval=self.forex_interval,
            )
            frame = self._clean(
                raw,
                min_rows=min_rows,
                label=symbol,
            )
            source = self._router_source(
                symbol
            )
            return self._store(
                seed,
                frame,
                source,
            )

        except Exception as exc:
            stale = self._stale_fallback(
                seed,
                min_rows=min_rows,
            )
            if stale is not None:
                return stale

            self._record_error(
                seed,
                exc,
            )
            raise RuntimeError(
                "Routed FOREX analysis data temporarily unavailable "
                f"for {symbol}: {type(exc).__name__}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Public callable installed beneath the existing strategy pipeline
    # ------------------------------------------------------------------

    def load(
        self,
        seed: Dict[str, Any],
    ) -> pd.DataFrame:
        """Return OHLCV only; strategy logic is intentionally untouched."""
        category = self._category(seed)

        if category == "FOREX":
            return self._forex(seed)

        return self._yahoo_global(seed)

    def install_into_frame_func(
        self,
        frame_func: Any,
    ) -> None:
        """Replace only the loader used by main's existing specialist frame.

        The passed frame function still performs the exact same:
          add_indicators -> train_model -> enrich

        No strategy/evaluation function is replaced.
        """
        namespace = getattr(
            frame_func,
            "__globals__",
            None,
        )
        if not isinstance(namespace, dict):
            raise RuntimeError(
                "Specialist frame function has no mutable module namespace"
            )

        current = namespace.get(
            "_v673_global_market_data"
        )

        if (
            callable(current)
            and current is not self.load
        ):
            self._original_loader = current

        namespace[
            "_v673_global_market_data"
        ] = self.load

        self._installed = True

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        now = time.time()
        with self._source_lock:
            sources = dict(self._last_source)
            success = dict(self._last_success)
            errors = dict(self._last_error)

        with self._cache_lock:
            memory_entries = len(self._memory)

        return {
            "version": self.VERSION,
            "installed": self._installed,
            "strategy_logic_changed": False,
            "execution_logic_changed": False,
            "forex_policy": (
                "JASONG_ROUTER: IG_DEMO -> TWELVE_DATA -> "
                "YAHOO -> ROUTER_STALE -> SPECIALIST_PERSISTENT_CACHE"
            ),
            "non_forex_policy": (
                "FRESH_CACHE -> SERIALIZED_YAHOO -> "
                "STALE_PERSISTENT_CACHE"
            ),
            "cache_dir": str(self.cache_dir),
            "memory_entries": memory_entries,
            "fresh_ttl_seconds": self.fresh_ttl_seconds,
            "stale_max_age_seconds": self.stale_max_age_seconds,
            "yahoo_min_gap_seconds": self.yahoo_min_gap_seconds,
            "yahoo_cooldown_seconds": self.yahoo_cooldown_seconds,
            "yahoo_cooldown_active": now < self._yahoo_cooldown_until,
            "yahoo_cooldown_remaining_seconds": max(
                0.0,
                round(
                    self._yahoo_cooldown_until - now,
                    1,
                ),
            ),
            "last_source_by_market": sources,
            "last_success_by_market": success,
            "last_error_by_market": errors,
            "live_money_execution": False,
        }
