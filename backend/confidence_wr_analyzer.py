from __future__ import annotations

import math
import threading
import time
import uuid
from typing import Any, Callable, Dict, Optional

import pandas as pd


class ConfidenceWinRateAnalyzer:
    """V6.2 confidence-bucket vs realised win-rate analyzer.

    This is a walk-forward diagnostic/adaptive-threshold engine.
    At each historical timestamp, the model only receives candles available
    up to that timestamp. Each qualifying BUY/SELL observation is then resolved
    after a fixed holding horizon using later candles only for OUTCOME scoring.

    The analyzer does NOT place trades.
    """

    BUCKETS = (
        ("35_40", 0.35, 0.40),
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

    @staticmethod
    def _classification(
        *,
        trades: int,
        win_rate: float,
        profit_factor: float,
        target_win_rate: float,
        min_trades_qualified: int,
        min_trades_promising: int,
        min_profit_factor: float,
    ) -> str:
        if (
            trades >= min_trades_qualified
            and win_rate >= target_win_rate
            and profit_factor >= min_profit_factor
        ):
            return "QUALIFIED"

        if (
            trades >= min_trades_promising
            and win_rate >= target_win_rate
            and profit_factor >= min_profit_factor
        ):
            return "PROMISING"

        if (
            trades > 0
            and win_rate >= target_win_rate
        ):
            return "INSUFFICIENT_SAMPLE"

        return "REJECT"

    def create_job(
        self,
        *,
        risk_mode: str = "Balanced",
        days: int = 7,
        interval: str = "15m",
        holding_candles: int = 4,
        stride_candles: int = 1,
        target_win_rate: float = 0.65,
        min_profit_factor: float = 1.50,
        min_trades_qualified: int = 20,
        min_trades_promising: int = 10,
        minimum_trade_confidence: float = 0.35,
        markets: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        if risk_mode not in self.profiles:
            raise ValueError("Invalid risk mode")

        days = max(1, min(int(days), 30))
        holding_candles = max(1, min(int(holding_candles), 32))
        stride_candles = max(1, min(int(stride_candles), 16))
        target_win_rate = max(0.50, min(float(target_win_rate), 0.95))
        min_profit_factor = max(0.0, float(min_profit_factor))
        minimum_trade_confidence = max(
            0.0,
            min(float(minimum_trade_confidence), 1.0),
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
            "holding_candles": holding_candles,
            "stride_candles": stride_candles,
            "target_win_rate": target_win_rate,
            "target_win_rate_pct": round(target_win_rate * 100.0, 1),
            "min_profit_factor": min_profit_factor,
            "min_trades_qualified": min_trades_qualified,
            "min_trades_promising": min_trades_promising,
            "minimum_trade_confidence": minimum_trade_confidence,
            "minimum_trade_confidence_pct": round(
                minimum_trade_confidence * 100.0,
                1,
            ),
            "markets": selected,
            "markets_total": len(selected),
            "markets_completed": 0,
            "current_market": None,
            "progress_percent": 0,
            "message": "Confidence/WR analyzer queued",
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
                "holding_candles": holding_candles,
                "stride_candles": stride_candles,
                "target_win_rate": target_win_rate,
                "min_profit_factor": min_profit_factor,
                "min_trades_qualified": min_trades_qualified,
                "min_trades_promising": min_trades_promising,
                "minimum_trade_confidence": minimum_trade_confidence,
                "markets": selected,
            },
            name=f"confidence-wr-{job_id[:8]}",
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

    def _bucket(self, confidence: float) -> Optional[str]:
        for name, low, high in self.BUCKETS:
            if low <= confidence < high:
                return name
        return None

    @staticmethod
    def _empty_bucket() -> Dict[str, Any]:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "gross_profit_units": 0.0,
            "gross_loss_units": 0.0,
            "win_rate": 0.0,
            "win_rate_pct": 0.0,
            "profit_factor": 0.0,
            "classification": "REJECT",
        }

    def _finalize_bucket(
        self,
        bucket: Dict[str, Any],
        *,
        target_win_rate: float,
        min_profit_factor: float,
        min_trades_qualified: int,
        min_trades_promising: int,
    ) -> Dict[str, Any]:
        trades = int(bucket["trades"])
        wins = int(bucket["wins"])
        losses = int(bucket["losses"])

        win_rate = wins / trades if trades else 0.0

        gross_profit = float(bucket["gross_profit_units"])
        gross_loss = float(bucket["gross_loss_units"])

        if gross_loss > 0:
            pf = gross_profit / gross_loss
        elif gross_profit > 0:
            pf = 999.0
        else:
            pf = 0.0

        classification = self._classification(
            trades=trades,
            win_rate=win_rate,
            profit_factor=pf,
            target_win_rate=target_win_rate,
            min_trades_qualified=min_trades_qualified,
            min_trades_promising=min_trades_promising,
            min_profit_factor=min_profit_factor,
        )

        result = dict(bucket)
        result.update({
            "win_rate": round(win_rate, 6),
            "win_rate_pct": round(win_rate * 100.0, 2),
            "profit_factor": round(pf, 4),
            "classification": classification,
        })
        return result

    def _run(
        self,
        *,
        job_id: str,
        risk_mode: str,
        days: int,
        interval: str,
        holding_candles: int,
        stride_candles: int,
        target_win_rate: float,
        min_profit_factor: float,
        min_trades_qualified: int,
        min_trades_promising: int,
        minimum_trade_confidence: float,
        markets: list[str],
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

            global_buckets = {
                name: self._empty_bucket()
                for name, _low, _high in self.BUCKETS
            }

            best_candidates = []

            for market_index, market in enumerate(markets, start=1):
                symbol = self.markets[market]

                self._update(
                    job_id,
                    current_market=market,
                    message=f"Analyzing {market} ({market_index}/{len(markets)})",
                    progress_percent=int(
                        ((market_index - 1) / max(len(markets), 1)) * 100
                    ),
                )

                raw = self.get_data_func(
                    symbol,
                    "1mo",
                    interval,
                )

                if raw is None or raw.empty or len(raw) < 140:
                    market_results.append({
                        "market": market,
                        "symbol": symbol,
                        "status": "INSUFFICIENT_DATA",
                        "rows": 0 if raw is None else len(raw),
                    })
                    continue

                raw = raw.copy().sort_index()

                latest_ts = pd.Timestamp(raw.index[-1])
                cutoff = latest_ts - pd.Timedelta(days=days)

                positions = [
                    i
                    for i, ts in enumerate(raw.index)
                    if pd.Timestamp(ts) >= cutoff
                    and i >= 100
                    and i + holding_candles < len(raw)
                ]

                positions = positions[::stride_candles]

                market_buckets = {
                    name: self._empty_bucket()
                    for name, _low, _high in self.BUCKETS
                }

                direction_buckets = {
                    "BUY": {
                        name: self._empty_bucket()
                        for name, _low, _high in self.BUCKETS
                    },
                    "SELL": {
                        name: self._empty_bucket()
                        for name, _low, _high in self.BUCKETS
                    },
                }

                failures = 0
                trade_events = []

                for pos_index, end_pos in enumerate(positions, start=1):
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

                    if confidence < minimum_trade_confidence:
                        continue

                    decision = str(
                        live.get("decision")
                        or "WAIT"
                    ).upper()

                    if decision not in {"BUY", "SELL"}:
                        continue

                    bucket_name = self._bucket(confidence)
                    if bucket_name is None:
                        continue

                    entry_price = self._safe_float(
                        historical["Close"].iloc[-1],
                        0.0,
                    )
                    exit_price = self._safe_float(
                        raw["Close"].iloc[end_pos + holding_candles],
                        0.0,
                    )

                    if entry_price <= 0 or exit_price <= 0:
                        continue

                    won = (
                        exit_price > entry_price
                        if decision == "BUY"
                        else exit_price < entry_price
                    )

                    # Payout-normalized PF using 0.8 reward / 1.0 loss.
                    # This mirrors binary-style payout assumptions already used
                    # in the app's paper framework.
                    reward_unit = 0.80 if won else 0.0
                    loss_unit = 0.0 if won else 1.0

                    for bucket in (
                        market_buckets[bucket_name],
                        direction_buckets[decision][bucket_name],
                        global_buckets[bucket_name],
                    ):
                        bucket["trades"] += 1
                        if won:
                            bucket["wins"] += 1
                        else:
                            bucket["losses"] += 1
                        bucket["gross_profit_units"] += reward_unit
                        bucket["gross_loss_units"] += loss_unit

                    if len(trade_events) < 500:
                        trade_events.append({
                            "timestamp": self._iso(raw.index[end_pos]),
                            "direction": decision,
                            "confidence": round(confidence, 6),
                            "confidence_pct": round(confidence * 100.0, 2),
                            "bucket": bucket_name,
                            "entry_price": entry_price,
                            "exit_timestamp": self._iso(
                                raw.index[end_pos + holding_candles]
                            ),
                            "exit_price": exit_price,
                            "result": "WIN" if won else "LOSS",
                            "holding_candles": holding_candles,
                        })

                    if pos_index % 10 == 0:
                        within_market = pos_index / max(len(positions), 1)
                        overall = (
                            (market_index - 1 + within_market)
                            / max(len(markets), 1)
                        )
                        self._update(
                            job_id,
                            progress_percent=min(99, int(overall * 100)),
                            message=(
                                f"{market}: {pos_index}/{len(positions)} "
                                "walk-forward observations"
                            ),
                        )

                finalized_market = {}
                for name, _low, _high in self.BUCKETS:
                    finalized_market[name] = self._finalize_bucket(
                        market_buckets[name],
                        target_win_rate=target_win_rate,
                        min_profit_factor=min_profit_factor,
                        min_trades_qualified=min_trades_qualified,
                        min_trades_promising=min_trades_promising,
                    )

                finalized_direction = {"BUY": {}, "SELL": {}}
                for direction in ("BUY", "SELL"):
                    for name, _low, _high in self.BUCKETS:
                        finalized_direction[direction][name] = (
                            self._finalize_bucket(
                                direction_buckets[direction][name],
                                target_win_rate=target_win_rate,
                                min_profit_factor=min_profit_factor,
                                min_trades_qualified=min_trades_qualified,
                                min_trades_promising=min_trades_promising,
                            )
                        )

                        bucket = finalized_direction[direction][name]
                        if bucket["classification"] in {
                            "QUALIFIED",
                            "PROMISING",
                        }:
                            best_candidates.append({
                                "market": market,
                                "symbol": symbol,
                                "direction": direction,
                                "bucket": name,
                                **bucket,
                            })

                market_results.append({
                    "market": market,
                    "symbol": symbol,
                    "status": "OK",
                    "observations_tested": len(positions) - failures,
                    "failed_observations": failures,
                    "buckets": finalized_market,
                    "direction_buckets": finalized_direction,
                    "events": trade_events,
                })

                self._update(
                    job_id,
                    markets_completed=market_index,
                )

            finalized_global = {}
            for name, _low, _high in self.BUCKETS:
                finalized_global[name] = self._finalize_bucket(
                    global_buckets[name],
                    target_win_rate=target_win_rate,
                    min_profit_factor=min_profit_factor,
                    min_trades_qualified=min_trades_qualified,
                    min_trades_promising=min_trades_promising,
                )

            bucket_order = {
                name: index
                for index, (name, _low, _high) in enumerate(self.BUCKETS)
            }

            best_candidates.sort(
                key=lambda item: (
                    1 if item["classification"] == "QUALIFIED" else 0,
                    item["win_rate"],
                    item["profit_factor"],
                    item["trades"],
                    -bucket_order.get(item["bucket"], 99),
                ),
                reverse=True,
            )

            adaptive_floor_by_market_direction = []

            for market_result in market_results:
                if market_result.get("status") != "OK":
                    continue

                for direction in ("BUY", "SELL"):
                    chosen = None

                    # Lowest confidence bucket that is actually QUALIFIED.
                    for name, low, high in self.BUCKETS:
                        bucket = market_result["direction_buckets"][direction][name]
                        if bucket["classification"] == "QUALIFIED":
                            chosen = {
                                "market": market_result["market"],
                                "symbol": market_result["symbol"],
                                "direction": direction,
                                "bucket": name,
                                "minimum_confidence": low,
                                "minimum_confidence_pct": round(low * 100.0, 1),
                                "trades": bucket["trades"],
                                "wins": bucket["wins"],
                                "losses": bucket["losses"],
                                "win_rate": bucket["win_rate"],
                                "win_rate_pct": bucket["win_rate_pct"],
                                "profit_factor": bucket["profit_factor"],
                                "classification": bucket["classification"],
                            }
                            break

                    if chosen is not None:
                        adaptive_floor_by_market_direction.append(chosen)

            result = {
                "analyzer": "V6.2_CONFIDENCE_VS_WIN_RATE",
                "method": (
                    "Walk-forward signal generation with no future candles; "
                    "future candles are used only to resolve the pre-existing "
                    "BUY/SELL outcome after the configured holding horizon."
                ),
                "risk_mode": risk_mode,
                "days": days,
                "interval": interval,
                "holding_candles": holding_candles,
                "stride_candles": stride_candles,
                "minimum_trade_confidence": minimum_trade_confidence,
                "minimum_trade_confidence_pct": round(
                    minimum_trade_confidence * 100.0,
                    1,
                ),
                "target_win_rate": target_win_rate,
                "target_win_rate_pct": round(target_win_rate * 100.0, 1),
                "min_profit_factor": min_profit_factor,
                "min_trades_qualified": min_trades_qualified,
                "min_trades_promising": min_trades_promising,
                "qualification_rule": (
                    f"QUALIFIED when trades >= {min_trades_qualified}, "
                    f"WR >= {target_win_rate:.0%}, "
                    f"PF >= {min_profit_factor:.2f}"
                ),
                "global_buckets": finalized_global,
                "adaptive_floor_by_market_direction":
                    adaptive_floor_by_market_direction,
                "best_qualified_buckets": best_candidates[:50],
                "markets": market_results,
                "live_execution": False,
            }

            self._update(
                job_id,
                status="COMPLETED",
                completed_at=time.time(),
                current_market=None,
                progress_percent=100,
                message="Confidence-vs-WR analysis completed",
                result=result,
                error=None,
            )

        except Exception as exc:
            self._update(
                job_id,
                status="FAILED",
                completed_at=time.time(),
                current_market=None,
                message="Analyzer failed",
                error=str(exc),
            )
