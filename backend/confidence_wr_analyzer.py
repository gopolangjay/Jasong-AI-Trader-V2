from __future__ import annotations

import math
import threading
import time
import uuid
from dataclasses import replace
from typing import Any, Callable, Dict, Optional

import pandas as pd


class ConfidenceWinRateAnalyzer:
    """V6.2.2 raw-confidence calibration analyzer.

    RAW-CALIBRATION METHOD
    ----------------------
    1. Load one month of candles for a market.
    2. Split the history into:
       - TRAIN: everything before the requested test window.
       - TEST: the requested last N days.
    3. Train the AI model ONCE on TRAIN.
    4. Freeze that model.
    5. Calculate indicators/predictions over the combined history using the
       frozen model.
    6. Replay the TEST period chronologically.
    7. For calibration only, lower decision()'s confidence gate to zero so the
       original 67% live-entry threshold cannot censor lower-confidence data.
    8. Record the model's raw confidence on every eligible candle >= 35%.
    9. If decision() still returns WAIT because of a non-confidence trading
       filter, infer raw BUY/SELL from combined_up_probability:
           >= 50% -> BUY
           <  50% -> SELL
    10. Future candles are used ONLY to settle that pre-existing direction
        after the configured holding horizon.

    This measures whether confidence itself is calibrated to realised WR.
    It is NOT a simulation of all live V6 risk/RSI/entry filters.

    This analyzer never places a trade and never changes the live threshold.
    """

    BUCKETS = (
        ("35_40", 0.35, 0.40),
        ("40_50", 0.40, 0.50),
        ("50_60", 0.50, 0.60),
        ("60_67", 0.60, 0.67),
        ("67_75", 0.67, 0.75),
        ("75_plus", 0.75, 1.01),
    )

    PAYOUT_UNIT = 0.80

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

    # ------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------

    @staticmethod
    def _safe_float(
        value: Any,
        default: float = 0.0,
    ) -> float:
        try:
            number = float(value)

            if (
                math.isnan(number)
                or math.isinf(number)
            ):
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

    def _bucket(
        self,
        confidence: float,
    ) -> Optional[str]:
        for (
            name,
            low,
            high,
        ) in self.BUCKETS:
            if (
                low
                <= confidence
                < high
            ):
                return name

        return None

    def _finalize_bucket(
        self,
        bucket: Dict[str, Any],
        *,
        target_win_rate: float,
        min_profit_factor: float,
        min_trades_qualified: int,
        min_trades_promising: int,
    ) -> Dict[str, Any]:
        trades = int(
            bucket.get(
                "trades",
                0,
            )
        )

        wins = int(
            bucket.get(
                "wins",
                0,
            )
        )

        losses = int(
            bucket.get(
                "losses",
                0,
            )
        )

        win_rate = (
            wins / trades
            if trades
            else 0.0
        )

        gross_profit = float(
            bucket.get(
                "gross_profit_units",
                0.0,
            )
        )

        gross_loss = float(
            bucket.get(
                "gross_loss_units",
                0.0,
            )
        )

        if gross_loss > 0:
            profit_factor = (
                gross_profit
                / gross_loss
            )

        elif gross_profit > 0:
            profit_factor = 999.0

        else:
            profit_factor = 0.0

        classification = (
            self._classification(
                trades=trades,
                win_rate=win_rate,
                profit_factor=
                    profit_factor,
                target_win_rate=
                    target_win_rate,
                min_trades_qualified=
                    min_trades_qualified,
                min_trades_promising=
                    min_trades_promising,
                min_profit_factor=
                    min_profit_factor,
            )
        )

        result = dict(
            bucket
        )

        result.update({
            "trades":
                trades,
            "wins":
                wins,
            "losses":
                losses,
            "win_rate":
                round(
                    win_rate,
                    6,
                ),
            "win_rate_pct":
                round(
                    win_rate
                    * 100.0,
                    2,
                ),
            "profit_factor":
                round(
                    profit_factor,
                    4,
                ),
            "classification":
                classification,
        })

        return result

    # ------------------------------------------------------------
    # jobs
    # ------------------------------------------------------------

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
        markets: Optional[
            list[str]
        ] = None,
    ) -> Dict[str, Any]:
        if risk_mode not in self.profiles:
            raise ValueError(
                "Invalid risk mode"
            )

        days = max(
            1,
            min(
                int(days),
                14,
            ),
        )

        holding_candles = max(
            1,
            min(
                int(
                    holding_candles
                ),
                32,
            ),
        )

        stride_candles = max(
            1,
            min(
                int(
                    stride_candles
                ),
                16,
            ),
        )

        target_win_rate = max(
            0.50,
            min(
                float(
                    target_win_rate
                ),
                0.95,
            ),
        )

        min_profit_factor = max(
            0.0,
            float(
                min_profit_factor
            ),
        )

        min_trades_qualified = max(
            1,
            int(
                min_trades_qualified
            ),
        )

        min_trades_promising = max(
            1,
            min(
                int(
                    min_trades_promising
                ),
                min_trades_qualified,
            ),
        )

        minimum_trade_confidence = max(
            0.0,
            min(
                float(
                    minimum_trade_confidence
                ),
                1.0,
            ),
        )

        selected = []

        requested = (
            markets
            or list(
                self.markets.keys()
            )
        )

        for name in requested:
            clean = str(
                name
            ).upper().strip()

            if (
                clean in self.markets
                and clean
                not in selected
            ):
                selected.append(
                    clean
                )

        if not selected:
            raise ValueError(
                "No valid markets supplied"
            )

        job_id = str(
            uuid.uuid4()
        )

        now = time.time()

        job = {
            "job_id":
                job_id,
            "analyzer_version":
                "V6.2.2_RAW_CALIBRATION",
            "status":
                "QUEUED",
            "created_at":
                now,
            "started_at":
                None,
            "completed_at":
                None,
            "risk_mode":
                risk_mode,
            "days":
                days,
            "interval":
                interval,
            "holding_candles":
                holding_candles,
            "stride_candles":
                stride_candles,
            "target_win_rate":
                target_win_rate,
            "target_win_rate_pct":
                round(
                    target_win_rate
                    * 100.0,
                    1,
                ),
            "min_profit_factor":
                min_profit_factor,
            "min_trades_qualified":
                min_trades_qualified,
            "min_trades_promising":
                min_trades_promising,
            "minimum_trade_confidence":
                minimum_trade_confidence,
            "minimum_trade_confidence_pct":
                round(
                    minimum_trade_confidence
                    * 100.0,
                    1,
                ),
            "markets":
                selected,
            "markets_total":
                len(
                    selected
                ),
            "markets_completed":
                0,
            "current_market":
                None,
            "progress_percent":
                0,
            "message":
                "Fast out-of-sample analyzer queued",
            "result":
                None,
            "error":
                None,
            "live_execution":
                False,
        }

        with self._lock:
            self._jobs[
                job_id
            ] = job

        worker = threading.Thread(
            target=self._run,
            kwargs={
                "job_id":
                    job_id,
                "risk_mode":
                    risk_mode,
                "days":
                    days,
                "interval":
                    interval,
                "holding_candles":
                    holding_candles,
                "stride_candles":
                    stride_candles,
                "target_win_rate":
                    target_win_rate,
                "min_profit_factor":
                    min_profit_factor,
                "min_trades_qualified":
                    min_trades_qualified,
                "min_trades_promising":
                    min_trades_promising,
                "minimum_trade_confidence":
                    minimum_trade_confidence,
                "markets":
                    selected,
            },
            name=(
                "confidence-wr-fast-"
                f"{job_id[:8]}"
            ),
            daemon=True,
        )

        worker.start()

        return self.get_job(
            job_id
        )

    def get_job(
        self,
        job_id: str,
    ) -> Optional[
        Dict[str, Any]
    ]:
        with self._lock:
            item = self._jobs.get(
                job_id
            )

            return (
                dict(
                    item
                )
                if item
                else None
            )

    def _update(
        self,
        job_id: str,
        **values,
    ) -> None:
        with self._lock:
            if (
                job_id
                in self._jobs
            ):
                self._jobs[
                    job_id
                ].update(
                    values
                )

    # ------------------------------------------------------------
    # analysis
    # ------------------------------------------------------------

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
            started_at=
                time.time(),
            message=(
                "Fast OOS analysis started"
            ),
        )

        try:
            profile = self.profiles[
                risk_mode
            ]

            # Analysis-only profile: remove the 67% live confidence gate.
            # The real V6.1/V6.2 live profile is NOT modified.
            try:
                calibration_profile = replace(
                    profile,
                    min_confidence=0.0,
                )
            except Exception:
                calibration_profile = profile

            global_buckets = {
                name:
                    self._empty_bucket()
                for (
                    name,
                    _low,
                    _high,
                )
                in self.BUCKETS
            }

            market_results = []
            best_candidates = []

            for (
                market_index,
                market,
            ) in enumerate(
                markets,
                start=1,
            ):
                symbol = (
                    self.markets[
                        market
                    ]
                )

                self._update(
                    job_id,
                    current_market=
                        market,
                    message=(
                        f"{market}: loading data "
                        f"({market_index}/"
                        f"{len(markets)})"
                    ),
                    progress_percent=
                        int(
                            (
                                market_index
                                - 1
                            )
                            / max(
                                len(markets),
                                1,
                            )
                            * 100
                        ),
                )

                raw = (
                    self.get_data_func(
                        symbol,
                        "1mo",
                        interval,
                    )
                )

                if (
                    raw is None
                    or raw.empty
                    or len(raw) < 160
                ):
                    market_results.append({
                        "market":
                            market,
                        "symbol":
                            symbol,
                        "status":
                            "INSUFFICIENT_DATA",
                        "rows":
                            (
                                0
                                if raw is None
                                else len(raw)
                            ),
                    })

                    self._update(
                        job_id,
                        markets_completed=
                            market_index,
                    )

                    continue

                raw = (
                    raw.copy()
                    .sort_index()
                )

                latest_ts = (
                    pd.Timestamp(
                        raw.index[-1]
                    )
                )

                cutoff = (
                    latest_ts
                    - pd.Timedelta(
                        days=days
                    )
                )

                train_mask = [
                    pd.Timestamp(ts)
                    < cutoff
                    for ts in raw.index
                ]

                train_raw = raw.loc[
                    train_mask
                ].copy()

                if len(
                    train_raw
                ) < 100:
                    market_results.append({
                        "market":
                            market,
                        "symbol":
                            symbol,
                        "status":
                            "INSUFFICIENT_TRAINING_DATA",
                        "training_rows":
                            len(
                                train_raw
                            ),
                    })

                    self._update(
                        job_id,
                        markets_completed=
                            market_index,
                    )

                    continue

                self._update(
                    job_id,
                    current_market=
                        market,
                    message=(
                        f"{market}: training frozen "
                        "pre-test model"
                    ),
                )

                # --------------------------------------------
                # TRAIN ONCE BEFORE TEST WINDOW
                # --------------------------------------------

                train_indicators = (
                    self.add_indicators_func(
                        train_raw
                    )
                )

                model = (
                    self.train_model_func(
                        train_indicators
                    )
                )

                # --------------------------------------------
                # APPLY FROZEN MODEL TO FULL HISTORY ONCE
                # --------------------------------------------

                full_indicators = (
                    self.add_indicators_func(
                        raw
                    )
                )

                enriched = (
                    self.enrich_func(
                        full_indicators,
                        model,
                    )
                )

                if (
                    enriched is None
                    or len(enriched) < 120
                ):
                    market_results.append({
                        "market":
                            market,
                        "symbol":
                            symbol,
                        "status":
                            "ENRICH_FAILED",
                    })

                    self._update(
                        job_id,
                        markets_completed=
                            market_index,
                    )

                    continue

                # Align the raw close series to the enriched index.
                close_series = (
                    raw["Close"]
                    .reindex(
                        enriched.index
                    )
                )

                test_positions = []

                for i, ts in enumerate(
                    enriched.index
                ):
                    if (
                        pd.Timestamp(ts)
                        >= cutoff
                        and i
                        + holding_candles
                        < len(
                            enriched
                        )
                    ):
                        test_positions.append(
                            i
                        )

                test_positions = (
                    test_positions[
                        ::stride_candles
                    ]
                )

                market_buckets = {
                    name:
                        self._empty_bucket()
                    for (
                        name,
                        _low,
                        _high,
                    )
                    in self.BUCKETS
                }

                direction_buckets = {
                    "BUY": {
                        name:
                            self._empty_bucket()
                        for (
                            name,
                            _low,
                            _high,
                        )
                        in self.BUCKETS
                    },
                    "SELL": {
                        name:
                            self._empty_bucket()
                        for (
                            name,
                            _low,
                            _high,
                        )
                        in self.BUCKETS
                    },
                }

                events = []
                failures = 0
                signals_35_plus = 0

                self._update(
                    job_id,
                    current_market=
                        market,
                    message=(
                        f"{market}: scoring "
                        f"{len(test_positions)} "
                        "out-of-sample candles"
                    ),
                )

                for (
                    point_number,
                    pos,
                ) in enumerate(
                    test_positions,
                    start=1,
                ):
                    # decision() sees no row after this historical point.
                    historical_view = (
                        enriched.iloc[
                            :pos + 1
                        ]
                    )

                    try:
                        live = (
                            self.decision_func(
                                historical_view,
                                calibration_profile,
                            )
                        )

                    except Exception:
                        failures += 1
                        continue

                    confidence = (
                        self._safe_float(
                            live.get(
                                "confidence"
                            ),
                            0.0,
                        )
                    )

                    confidence = max(
                        0.0,
                        min(
                            confidence,
                            1.0,
                        ),
                    )

                    if (
                        confidence
                        < minimum_trade_confidence
                    ):
                        continue

                    live_decision = str(
                        live.get(
                            "decision"
                        )
                        or "WAIT"
                    ).upper()

                    ai_up = self._safe_float(
                        live.get(
                            "combined_up_probability"
                        ),
                        0.50,
                    )

                    # RAW calibration direction:
                    # preserve explicit BUY/SELL when available; otherwise
                    # infer direction from the uncensored model probability.
                    if live_decision in {
                        "BUY",
                        "SELL",
                    }:
                        decision = live_decision
                        direction_source = "DECISION"
                    else:
                        decision = (
                            "BUY"
                            if ai_up >= 0.50
                            else "SELL"
                        )
                        direction_source = "RAW_AI_PROBABILITY"

                    signals_35_plus += 1

                    bucket_name = (
                        self._bucket(
                            confidence
                        )
                    )

                    if (
                        bucket_name
                        is None
                    ):
                        continue

                    entry_price = (
                        self._safe_float(
                            close_series.iloc[
                                pos
                            ],
                            0.0,
                        )
                    )

                    exit_price = (
                        self._safe_float(
                            close_series.iloc[
                                pos
                                + holding_candles
                            ],
                            0.0,
                        )
                    )

                    if (
                        entry_price <= 0
                        or exit_price <= 0
                    ):
                        failures += 1
                        continue

                    won = (
                        exit_price
                        > entry_price
                        if decision
                        == "BUY"
                        else exit_price
                        < entry_price
                    )

                    reward_unit = (
                        self.PAYOUT_UNIT
                        if won
                        else 0.0
                    )

                    loss_unit = (
                        0.0
                        if won
                        else 1.0
                    )

                    for bucket in (
                        market_buckets[
                            bucket_name
                        ],
                        direction_buckets[
                            decision
                        ][
                            bucket_name
                        ],
                        global_buckets[
                            bucket_name
                        ],
                    ):
                        bucket[
                            "trades"
                        ] += 1

                        if won:
                            bucket[
                                "wins"
                            ] += 1
                        else:
                            bucket[
                                "losses"
                            ] += 1

                        bucket[
                            "gross_profit_units"
                        ] += (
                            reward_unit
                        )

                        bucket[
                            "gross_loss_units"
                        ] += (
                            loss_unit
                        )

                    if len(
                        events
                    ) < 500:
                        events.append({
                            "timestamp":
                                self._iso(
                                    enriched.index[
                                        pos
                                    ]
                                ),
                            "direction":
                                decision,
                            "live_decision":
                                live_decision,
                            "direction_source":
                                direction_source,
                            "ai_up":
                                round(
                                    ai_up,
                                    6,
                                ),
                            "ai_up_pct":
                                round(
                                    ai_up * 100.0,
                                    2,
                                ),
                            "confidence":
                                round(
                                    confidence,
                                    6,
                                ),
                            "confidence_pct":
                                round(
                                    confidence
                                    * 100.0,
                                    2,
                                ),
                            "bucket":
                                bucket_name,
                            "entry_price":
                                entry_price,
                            "exit_timestamp":
                                self._iso(
                                    enriched.index[
                                        pos
                                        + holding_candles
                                    ]
                                ),
                            "exit_price":
                                exit_price,
                            "result":
                                (
                                    "WIN"
                                    if won
                                    else "LOSS"
                                ),
                            "holding_candles":
                                holding_candles,
                        })

                    if (
                        point_number
                        % 50
                        == 0
                    ):
                        within = (
                            point_number
                            / max(
                                len(
                                    test_positions
                                ),
                                1,
                            )
                        )

                        overall = (
                            (
                                market_index
                                - 1
                                + within
                            )
                            / max(
                                len(markets),
                                1,
                            )
                        )

                        self._update(
                            job_id,
                            progress_percent=
                                min(
                                    99,
                                    int(
                                        overall
                                        * 100
                                    ),
                                ),
                            message=(
                                f"{market}: "
                                f"{point_number}/"
                                f"{len(test_positions)}"
                            ),
                        )

                finalized_market = {}

                for (
                    name,
                    _low,
                    _high,
                ) in self.BUCKETS:
                    finalized_market[
                        name
                    ] = (
                        self._finalize_bucket(
                            market_buckets[
                                name
                            ],
                            target_win_rate=
                                target_win_rate,
                            min_profit_factor=
                                min_profit_factor,
                            min_trades_qualified=
                                min_trades_qualified,
                            min_trades_promising=
                                min_trades_promising,
                        )
                    )

                finalized_direction = {
                    "BUY": {},
                    "SELL": {},
                }

                for direction in (
                    "BUY",
                    "SELL",
                ):
                    for (
                        name,
                        _low,
                        _high,
                    ) in self.BUCKETS:
                        bucket = (
                            self._finalize_bucket(
                                direction_buckets[
                                    direction
                                ][
                                    name
                                ],
                                target_win_rate=
                                    target_win_rate,
                                min_profit_factor=
                                    min_profit_factor,
                                min_trades_qualified=
                                    min_trades_qualified,
                                min_trades_promising=
                                    min_trades_promising,
                            )
                        )

                        finalized_direction[
                            direction
                        ][
                            name
                        ] = bucket

                        if (
                            bucket[
                                "classification"
                            ]
                            in {
                                "QUALIFIED",
                                "PROMISING",
                            }
                        ):
                            best_candidates.append({
                                "market":
                                    market,
                                "symbol":
                                    symbol,
                                "direction":
                                    direction,
                                "bucket":
                                    name,
                                **bucket,
                            })

                market_results.append({
                    "market":
                        market,
                    "symbol":
                        symbol,
                    "status":
                        "OK",
                    "training_rows":
                        len(
                            train_raw
                        ),
                    "test_rows":
                        len(
                            test_positions
                        ),
                    "signals_35_plus":
                        signals_35_plus,
                    "failed_observations":
                        failures,
                    "buckets":
                        finalized_market,
                    "direction_buckets":
                        finalized_direction,
                    "events":
                        events,
                })

                self._update(
                    job_id,
                    markets_completed=
                        market_index,
                    progress_percent=
                        int(
                            market_index
                            / max(
                                len(markets),
                                1,
                            )
                            * 100
                        ),
                    message=(
                        f"{market} complete "
                        f"({market_index}/"
                        f"{len(markets)})"
                    ),
                )

            # ------------------------------------------------
            # finalize global buckets
            # ------------------------------------------------

            finalized_global = {}

            for (
                name,
                _low,
                _high,
            ) in self.BUCKETS:
                finalized_global[
                    name
                ] = (
                    self._finalize_bucket(
                        global_buckets[
                            name
                        ],
                        target_win_rate=
                            target_win_rate,
                        min_profit_factor=
                            min_profit_factor,
                        min_trades_qualified=
                            min_trades_qualified,
                        min_trades_promising=
                            min_trades_promising,
                    )
                )

            # ------------------------------------------------
            # lowest qualified floor per market + direction
            # ------------------------------------------------

            adaptive_floors = []

            for market_result in (
                market_results
            ):
                if (
                    market_result.get(
                        "status"
                    )
                    != "OK"
                ):
                    continue

                for direction in (
                    "BUY",
                    "SELL",
                ):
                    for (
                        name,
                        low,
                        _high,
                    ) in self.BUCKETS:
                        bucket = (
                            market_result[
                                "direction_buckets"
                            ][
                                direction
                            ][
                                name
                            ]
                        )

                        if (
                            bucket[
                                "classification"
                            ]
                            == "QUALIFIED"
                        ):
                            adaptive_floors.append({
                                "market":
                                    market_result[
                                        "market"
                                    ],
                                "symbol":
                                    market_result[
                                        "symbol"
                                    ],
                                "direction":
                                    direction,
                                "bucket":
                                    name,
                                "minimum_confidence":
                                    low,
                                "minimum_confidence_pct":
                                    round(
                                        low
                                        * 100.0,
                                        1,
                                    ),
                                "trades":
                                    bucket[
                                        "trades"
                                    ],
                                "wins":
                                    bucket[
                                        "wins"
                                    ],
                                "losses":
                                    bucket[
                                        "losses"
                                    ],
                                "win_rate":
                                    bucket[
                                        "win_rate"
                                    ],
                                "win_rate_pct":
                                    bucket[
                                        "win_rate_pct"
                                    ],
                                "profit_factor":
                                    bucket[
                                        "profit_factor"
                                    ],
                                "classification":
                                    "QUALIFIED",
                            })

                            break

            best_candidates.sort(
                key=lambda item: (
                    1
                    if item.get(
                        "classification"
                    )
                    == "QUALIFIED"
                    else 0,
                    float(
                        item.get(
                            "win_rate",
                            0.0,
                        )
                    ),
                    float(
                        item.get(
                            "profit_factor",
                            0.0,
                        )
                    ),
                    int(
                        item.get(
                            "trades",
                            0,
                        )
                    ),
                ),
                reverse=True,
            )

            result = {
                "analyzer":
                    "V6.2.2_RAW_CONFIDENCE_CALIBRATION",
                "method":
                    (
                        "Train once per market before the test window, freeze model, "
                        "remove the confidence gate for analysis only, infer raw "
                        "direction from AI probability when normal decision is WAIT, "
                        "then use future candles only to resolve outcomes."
                    ),
                "risk_mode":
                    risk_mode,
                "days":
                    days,
                "interval":
                    interval,
                "holding_candles":
                    holding_candles,
                "stride_candles":
                    stride_candles,
                "minimum_trade_confidence":
                    minimum_trade_confidence,
                "minimum_trade_confidence_pct":
                    round(
                        minimum_trade_confidence
                        * 100.0,
                        1,
                    ),
                "target_win_rate":
                    target_win_rate,
                "target_win_rate_pct":
                    round(
                        target_win_rate
                        * 100.0,
                        1,
                    ),
                "min_profit_factor":
                    min_profit_factor,
                "min_trades_qualified":
                    min_trades_qualified,
                "min_trades_promising":
                    min_trades_promising,
                "qualification_rule":
                    (
                        f"QUALIFIED: trades >= "
                        f"{min_trades_qualified}, "
                        f"WR >= "
                        f"{target_win_rate:.0%}, "
                        f"PF >= "
                        f"{min_profit_factor:.2f}"
                    ),
                "global_buckets":
                    finalized_global,
                "adaptive_floor_by_market_direction":
                    adaptive_floors,
                "best_qualified_buckets":
                    best_candidates[
                        :50
                    ],
                "markets":
                    market_results,
                "live_execution":
                    False,
            }

            self._update(
                job_id,
                status="COMPLETED",
                completed_at=
                    time.time(),
                current_market=
                    None,
                progress_percent=
                    100,
                message=(
                    "Fast out-of-sample confidence/WR analysis completed"
                ),
                result=
                    result,
                error=
                    None,
            )

        except Exception as exc:
            self._update(
                job_id,
                status="FAILED",
                completed_at=
                    time.time(),
                current_market=
                    None,
                message=(
                    "Fast analyzer failed"
                ),
                error=
                    str(exc),
            )
