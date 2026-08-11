from __future__ import annotations

import math
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

import pandas as pd


class ConfidenceReplayEngine:
    """Walk-forward replay of the app's live confidence decision.

    For every replay timestamp we slice the price dataframe at that timestamp
    BEFORE indicators/model/decision are calculated. Future candles are never
    included in that replay observation.

    This is diagnostic research only. It does not change V6.1 trading rules.
    """

    BUCKETS = (
        ("lt_40", 0.00, 0.40),
        ("40_50", 0.40, 0.50),
        ("50_60", 0.50, 0.60),
        ("60_67", 0.60, 0.67),
        ("67_75", 0.67, 0.75),
        ("75_plus", 0.75, 1.01),
    )

    def __init__(
        self,
        *,
        markets: Dict[str, str],
        get_data_func: Callable,
        add_indicators_func: Callable,
        train_model_func: Callable,
        enrich_func: Callable,
        decision_func: Callable,
        profiles: Dict[str, Any],
    ):
        self.markets = dict(markets)
        self.get_data_func = get_data_func
        self.add_indicators_func = add_indicators_func
        self.train_model_func = train_model_func
        self.enrich_func = enrich_func
        self.decision_func = decision_func
        self.profiles = profiles

        self._lock = threading.RLock()
        self._jobs: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            number = float(value)
            if math.isnan(number) or math.isinf(number):
                return default
            return number
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _iso(value) -> Optional[str]:
        try:
            ts = pd.Timestamp(value)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            else:
                ts = ts.tz_convert("UTC")
            return ts.isoformat()
        except Exception:
            return None

    def create_job(
        self,
        *,
        risk_mode: str = "Balanced",
        days: int = 7,
        interval: str = "15m",
        threshold: Optional[float] = None,
        stride_candles: int = 1,
        markets: Optional[list[str]] = None,
        max_events_per_market: int = 100,
    ) -> Dict[str, Any]:
        if risk_mode not in self.profiles:
            raise ValueError("Invalid risk mode")

        days = max(1, min(int(days), 14))
        stride_candles = max(1, min(int(stride_candles), 16))

        profile = self.profiles[risk_mode]
        effective_threshold = (
            float(threshold)
            if threshold is not None
            else float(profile.min_confidence)
        )

        selected = []
        requested = markets or list(self.markets.keys())

        for name in requested:
            clean = str(name).upper().strip()
            if clean in self.markets and clean not in selected:
                selected.append(clean)

        if not selected:
            raise ValueError("No valid markets supplied")

        job_id = str(uuid.uuid4())
        now = time.time()

        job = {
            "job_id": job_id,
            "status": "QUEUED",
            "created_at": now,
            "started_at": None,
            "completed_at": None,
            "risk_mode": risk_mode,
            "days": days,
            "interval": interval,
            "threshold": effective_threshold,
            "threshold_pct": round(effective_threshold * 100.0, 1),
            "stride_candles": stride_candles,
            "markets": selected,
            "markets_total": len(selected),
            "markets_completed": 0,
            "current_market": None,
            "progress_percent": 0,
            "message": "Replay queued",
            "result": None,
            "error": None,
            "live_execution": False,
        }

        with self._lock:
            self._jobs[job_id] = job

        thread = threading.Thread(
            target=self._run,
            kwargs={
                "job_id": job_id,
                "risk_mode": risk_mode,
                "days": days,
                "interval": interval,
                "threshold": effective_threshold,
                "stride_candles": stride_candles,
                "markets": selected,
                "max_events_per_market": max(
                    10,
                    min(int(max_events_per_market), 1000),
                ),
            },
            name=f"confidence-replay-{job_id[:8]}",
            daemon=True,
        )
        thread.start()

        return self.get_job(job_id)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            item = self._jobs.get(job_id)
            return dict(item) if item else None

    def _update(self, job_id: str, **values) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id].update(values)

    def _bucket(self, confidence: float) -> str:
        for name, low, high in self.BUCKETS:
            if low <= confidence < high:
                return name
        return "75_plus"

    def _run(
        self,
        *,
        job_id: str,
        risk_mode: str,
        days: int,
        interval: str,
        threshold: float,
        stride_candles: int,
        markets: list[str],
        max_events_per_market: int,
    ) -> None:
        self._update(
            job_id,
            status="RUNNING",
            started_at=time.time(),
            message="Loading historical candles",
        )

        try:
            profile = self.profiles[risk_mode]
            market_results = []

            total_observations = 0
            total_above = 0
            global_max = 0.0
            global_max_market = None
            global_max_time = None

            for market_index, market in enumerate(markets, start=1):
                symbol = self.markets[market]

                self._update(
                    job_id,
                    current_market=market,
                    message=f"Walk-forward replay {market} ({market_index}/{len(markets)})",
                    progress_percent=int(
                        ((market_index - 1) / max(len(markets), 1)) * 100
                    ),
                )

                # One month supplies training history before the 7-day test window.
                raw = self.get_data_func(
                    symbol,
                    "1mo",
                    interval,
                )

                if raw is None or raw.empty:
                    market_results.append({
                        "market": market,
                        "symbol": symbol,
                        "status": "NO_DATA",
                    })
                    continue

                raw = raw.copy().sort_index()

                if len(raw) < 120:
                    market_results.append({
                        "market": market,
                        "symbol": symbol,
                        "status": "INSUFFICIENT_DATA",
                        "rows": len(raw),
                    })
                    continue

                latest_ts = pd.Timestamp(raw.index[-1])
                cutoff = latest_ts - pd.Timedelta(days=days)

                candidate_positions = [
                    i
                    for i, ts in enumerate(raw.index)
                    if pd.Timestamp(ts) >= cutoff
                    and i >= 100
                ]

                candidate_positions = candidate_positions[::stride_candles]

                observations = []
                bucket_counts = {
                    name: 0
                    for name, _low, _high in self.BUCKETS
                }

                above_count = 0
                direction_confirmed_count = 0
                max_conf = 0.0
                max_obs = None
                failures = 0

                for position_index, end_pos in enumerate(
                    candidate_positions,
                    start=1,
                ):
                    # Strict walk-forward slice. No row after end_pos exists
                    # from the model's point of view.
                    historical = raw.iloc[: end_pos + 1].copy()

                    try:
                        indicators = self.add_indicators_func(historical)
                        model = self.train_model_func(indicators)
                        enriched = self.enrich_func(indicators, model)
                        live = self.decision_func(enriched, profile)
                    except Exception:
                        failures += 1
                        continue

                    confidence = self._safe_float(
                        live.get("confidence"),
                        0.0,
                    )
                    confidence = max(0.0, min(confidence, 1.0))

                    decision = str(
                        live.get("decision")
                        or "WAIT"
                    ).upper()

                    ai_up = self._safe_float(
                        live.get("combined_up_probability"),
                        0.50,
                    )
                    rsi = self._safe_float(
                        live.get("rsi"),
                        50.0,
                    )
                    price = self._safe_float(
                        live.get("price"),
                        self._safe_float(
                            historical["Close"].iloc[-1],
                            0.0,
                        ),
                    )

                    event = {
                        "timestamp": self._iso(raw.index[end_pos]),
                        "confidence": round(confidence, 6),
                        "confidence_pct": round(confidence * 100.0, 2),
                        "decision": decision,
                        "ai_up": round(ai_up, 6),
                        "ai_up_pct": round(ai_up * 100.0, 2),
                        "rsi": round(rsi, 2),
                        "price": price,
                        "above_threshold": confidence >= threshold,
                    }

                    total_observations += 1
                    bucket_counts[self._bucket(confidence)] += 1

                    if confidence >= threshold:
                        above_count += 1
                        total_above += 1

                        if decision in {"BUY", "SELL"}:
                            direction_confirmed_count += 1

                        if len(observations) < max_events_per_market:
                            observations.append(event)

                    if confidence > max_conf:
                        max_conf = confidence
                        max_obs = event

                    if confidence > global_max:
                        global_max = confidence
                        global_max_market = market
                        global_max_time = event["timestamp"]

                    if position_index % 10 == 0:
                        within_market = (
                            position_index
                            / max(len(candidate_positions), 1)
                        )
                        overall = (
                            (market_index - 1 + within_market)
                            / max(len(markets), 1)
                        )
                        self._update(
                            job_id,
                            progress_percent=min(
                                99,
                                int(overall * 100),
                            ),
                            message=(
                                f"{market}: {position_index}/"
                                f"{len(candidate_positions)} replay points"
                            ),
                        )

                market_results.append({
                    "market": market,
                    "symbol": symbol,
                    "status": "OK",
                    "observations": (
                        len(candidate_positions) - failures
                    ),
                    "failed_observations": failures,
                    "above_threshold_count": above_count,
                    "above_threshold_pct": (
                        round(
                            above_count
                            / max(
                                len(candidate_positions) - failures,
                                1,
                            )
                            * 100.0,
                            2,
                        )
                    ),
                    "directional_above_threshold_count":
                        direction_confirmed_count,
                    "max_confidence": round(max_conf, 6),
                    "max_confidence_pct": round(max_conf * 100.0, 2),
                    "max_observation": max_obs,
                    "confidence_buckets": bucket_counts,
                    "above_threshold_events": observations,
                })

                self._update(
                    job_id,
                    markets_completed=market_index,
                )

            result = {
                "replay_engine": "V6.1_WALK_FORWARD_CONFIDENCE_REPLAY",
                "method": (
                    "Each historical observation rebuilds indicators/model/"
                    "decision using only candles available up to that timestamp."
                ),
                "risk_mode": risk_mode,
                "days": days,
                "interval": interval,
                "threshold": threshold,
                "threshold_pct": round(threshold * 100.0, 1),
                "stride_candles": stride_candles,
                "markets_tested": len(markets),
                "total_observations": total_observations,
                "above_threshold_count": total_above,
                "above_threshold_pct": (
                    round(
                        total_above / max(total_observations, 1) * 100.0,
                        2,
                    )
                ),
                "ever_above_threshold": total_above > 0,
                "global_max_confidence": round(global_max, 6),
                "global_max_confidence_pct": round(global_max * 100.0, 2),
                "global_max_market": global_max_market,
                "global_max_timestamp": global_max_time,
                "markets": market_results,
                "live_execution": False,
            }

            self._update(
                job_id,
                status="COMPLETED",
                completed_at=time.time(),
                current_market=None,
                progress_percent=100,
                message="Seven-day confidence replay completed",
                result=result,
                error=None,
            )

        except Exception as exc:
            self._update(
                job_id,
                status="FAILED",
                completed_at=time.time(),
                current_market=None,
                message="Replay failed",
                error=str(exc),
            )
