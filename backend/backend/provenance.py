from __future__ import annotations

import math
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional


MISSING_SOURCES = {"", "UNKNOWN", "UNAVAILABLE", "NOT_REQUESTED", "NONE", "NULL"}


def _timestamp(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            number = float(value)
            return number if math.isfinite(number) and number > 0 else None
        except Exception:
            return None
    if hasattr(value, "timestamp"):
        try:
            return float(value.timestamp())
        except Exception:
            pass
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _age(now: float, timestamp: Optional[float]) -> Optional[float]:
    if timestamp is None:
        return None
    return round(max(0.0, now - timestamp), 3)


def source_missing(value: Any) -> bool:
    return str(value or "").upper().strip() in MISSING_SOURCES


class ProvenanceRegistry:
    """Keeps the source/timestamp that produced each specialist analysis frame.

    The registry deliberately distinguishes the *analysis data provider* from the
    strategy pipeline name.  This prevents labels such as CATEGORY_SPECIALIST
    from being mistaken for a price-data source.
    """

    def __init__(self, default_analysis_source: str = "UNAVAILABLE") -> None:
        self.default_analysis_source = str(default_analysis_source or "UNAVAILABLE").upper().strip()
        self._lock = threading.RLock()
        self._frames: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _key(value: Any) -> str:
        return "".join(ch for ch in str(value or "").upper() if ch.isalnum())

    def record_frame(self, seed: Dict[str, Any], frame: Any, source: Optional[str] = None) -> Any:
        key = self._key(seed.get("key") or seed.get("symbol") or seed.get("name"))
        provider = str(source or self.default_analysis_source or "UNAVAILABLE").upper().strip()
        price_timestamp = None
        rows = 0
        try:
            rows = len(frame)
        except Exception:
            rows = 0
        try:
            if rows:
                price_timestamp = _timestamp(frame.index[-1])
        except Exception:
            price_timestamp = None
        if price_timestamp is None:
            price_timestamp = time.time()
        record = {
            "analysis_price_source": provider,
            "analysis_price_timestamp": price_timestamp,
            "analysis_symbol": seed.get("analysis_symbol"),
            "analysis_rows": rows,
            "recorded_at": time.time(),
        }
        if key:
            with self._lock:
                self._frames[key] = record
        return frame

    def frame_record(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        key = self._key(
            candidate.get("key")
            or candidate.get("symbol")
            or candidate.get("market")
        )
        with self._lock:
            return dict(self._frames.get(key) or {})

    def build(
        self,
        candidate: Dict[str, Any],
        *,
        now: Optional[float] = None,
        signal_max_age_seconds: float = 300.0,
        quote_max_age_seconds: float = 180.0,
    ) -> Dict[str, Any]:
        now = float(now or time.time())
        frame = self.frame_record(candidate)
        analysis_source = (
            candidate.get("analysis_price_source")
            or frame.get("analysis_price_source")
            or self.default_analysis_source
            or "UNAVAILABLE"
        )
        analysis_timestamp = _timestamp(
            candidate.get("analysis_price_timestamp")
            or frame.get("analysis_price_timestamp")
        )
        signal_timestamp = _timestamp(
            candidate.get("signal_timestamp")
            or candidate.get("evaluated_at")
            or candidate.get("created_at")
        )
        quote_source = str(
            candidate.get("broker_quote_source")
            or candidate.get("ig_quote_source")
            or "UNAVAILABLE"
        ).upper().strip()
        quote_timestamp = _timestamp(
            candidate.get("broker_quote_timestamp")
            or candidate.get("ig_quote_timestamp")
            or (
                candidate.get("evaluated_at")
                if not source_missing(quote_source)
                else None
            )
        )
        news_sources = candidate.get("news_sources") or []
        if isinstance(news_sources, str):
            news_sources = [news_sources]
        news_timestamp = _timestamp(candidate.get("news_timestamp"))
        signal_age = _age(now, signal_timestamp)
        quote_age = _age(now, quote_timestamp)

        issues = []
        if source_missing(analysis_source):
            issues.append("ANALYSIS_PRICE_SOURCE_MISSING")
        if signal_timestamp is None:
            issues.append("SIGNAL_TIMESTAMP_MISSING")
        elif signal_age is not None and signal_age > signal_max_age_seconds:
            issues.append("SIGNAL_STALE")
        if source_missing(quote_source):
            issues.append("BROKER_QUOTE_SOURCE_MISSING")
        if quote_timestamp is None:
            issues.append("BROKER_QUOTE_TIMESTAMP_MISSING")
        elif quote_age is not None and quote_age > quote_max_age_seconds:
            issues.append("BROKER_QUOTE_STALE")

        return {
            "analysis_price_source": str(analysis_source).upper().strip(),
            "analysis_price_timestamp": analysis_timestamp,
            "broker_quote_source": quote_source,
            "broker_quote_timestamp": quote_timestamp,
            "news_sources": [str(item) for item in news_sources if str(item or "").strip()],
            "news_timestamp": news_timestamp,
            "fallback_source": candidate.get("fallback_source"),
            "signal_timestamp": signal_timestamp,
            "signal_age_seconds": signal_age,
            "quote_age_seconds": quote_age,
            "signal_max_age_seconds": signal_max_age_seconds,
            "quote_max_age_seconds": quote_max_age_seconds,
            "fresh": not any(issue in {"SIGNAL_STALE", "BROKER_QUOTE_STALE"} for issue in issues),
            "complete_for_execution": not issues,
            "issues": issues,
        }
