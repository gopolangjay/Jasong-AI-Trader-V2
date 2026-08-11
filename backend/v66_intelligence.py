"""
Jasong AI Trader V6.6
Forward Performance + Adaptive Watch Cadence + Portfolio/Correlation Intelligence

PAPER ONLY. This module never places broker orders.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple


BREAK_EVEN_WR = 1.0 / 1.8  # 55.56% at 0.80 payout


class V66Intelligence:
    def __init__(
        self,
        *,
        forward_quarantine_min_trades: int = 8,
        forward_mature_min_trades: int = 20,
        forward_min_win_rate: float = 0.56,
        forward_min_profit_factor: float = 1.00,
        max_currency_exposure: int = 2,
        max_highly_correlated_open: int = 1,
        high_correlation_abs: float = 0.80,
    ) -> None:
        self.forward_quarantine_min_trades = int(
            forward_quarantine_min_trades
        )
        self.forward_mature_min_trades = int(
            forward_mature_min_trades
        )
        self.forward_min_win_rate = float(
            forward_min_win_rate
        )
        self.forward_min_profit_factor = float(
            forward_min_profit_factor
        )
        self.max_currency_exposure = int(
            max_currency_exposure
        )
        self.max_highly_correlated_open = int(
            max_highly_correlated_open
        )
        self.high_correlation_abs = float(
            high_correlation_abs
        )

    # --------------------------------------------------------
    # forward performance
    # --------------------------------------------------------

    @staticmethod
    def _pf(wins: int, losses: int, payout: float = 0.80) -> float:
        gp = wins * float(payout)
        gl = losses * 1.0

        if gl > 0:
            return gp / gl
        if gp > 0:
            return 999.0
        return 0.0

    @staticmethod
    def _bucket(confidence: float) -> str:
        c = float(confidence)

        if 0.35 <= c < 0.40:
            return "35_40"
        if 0.40 <= c < 0.50:
            return "40_50"
        if 0.50 <= c < 0.60:
            return "50_60"
        if 0.60 <= c < 0.67:
            return "60_67"
        if 0.67 <= c < 0.75:
            return "67_75"
        if c >= 0.75:
            return "75_plus"

        return "below_35"

    def forward_key(
        self,
        *,
        market: str,
        direction: str,
        confidence: float,
        entry_path: Optional[str],
    ) -> str:
        return (
            f"{str(market).upper()}|"
            f"{str(direction).upper()}|"
            f"{self._bucket(confidence)}|"
            f"{str(entry_path or 'UNKNOWN').upper()}"
        )

    def forward_performance(
        self,
        watchers: Iterable[Dict[str, Any]],
    ) -> Dict[str, Any]:
        groups: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "pnl": 0.0,
                "market": None,
                "direction": None,
                "bucket": None,
                "entry_path": None,
            }
        )

        path_groups: Dict[str, Dict[str, Any]] = defaultdict(
            lambda: {
                "trades": 0,
                "wins": 0,
                "losses": 0,
                "pnl": 0.0,
            }
        )

        total = 0
        wins_total = 0
        losses_total = 0
        pnl_total = 0.0

        for item in watchers:
            result = str(
                item.get("result")
                or item.get("status")
                or ""
            ).upper()

            if result not in {"WIN", "LOSS"}:
                continue

            confidence = float(
                (
                    item.get("entry_snapshot")
                    or {}
                ).get("live_confidence")
                or (
                    item.get("last_live_signal")
                    or {}
                ).get("confidence")
                or 0.0
            )

            market = str(
                item.get("market")
                or item.get("symbol")
                or ""
            ).upper()

            direction = str(
                item.get("direction")
                or ""
            ).upper()

            entry_path = str(
                item.get("entry_path")
                or "UNKNOWN"
            ).upper()

            key = self.forward_key(
                market=market,
                direction=direction,
                confidence=confidence,
                entry_path=entry_path,
            )

            bucket = self._bucket(
                confidence
            )

            won = (
                result == "WIN"
            )

            pnl = float(
                item.get("pnl")
                or 0.0
            )

            row = groups[key]

            row["market"] = market
            row["direction"] = direction
            row["bucket"] = bucket
            row["entry_path"] = entry_path
            row["trades"] += 1
            row["wins"] += (
                1 if won else 0
            )
            row["losses"] += (
                0 if won else 1
            )
            row["pnl"] += pnl

            path_row = path_groups[
                entry_path
            ]

            path_row["trades"] += 1
            path_row["wins"] += (
                1 if won else 0
            )
            path_row["losses"] += (
                0 if won else 1
            )
            path_row["pnl"] += pnl

            total += 1
            wins_total += (
                1 if won else 0
            )
            losses_total += (
                0 if won else 1
            )
            pnl_total += pnl

        def finalize(
            source: Dict[str, Dict[str, Any]],
        ) -> Dict[str, Dict[str, Any]]:
            result: Dict[str, Dict[str, Any]] = {}

            for key, row in source.items():
                trades = int(
                    row["trades"]
                )
                wins = int(
                    row["wins"]
                )
                losses = int(
                    row["losses"]
                )

                wr = (
                    wins / trades
                    if trades
                    else 0.0
                )

                pf = self._pf(
                    wins,
                    losses,
                )

                if trades < self.forward_quarantine_min_trades:
                    trust = "LEARNING"
                    quarantined = False

                elif (
                    wr < self.forward_min_win_rate
                    or pf < self.forward_min_profit_factor
                ):
                    trust = "QUARANTINED"
                    quarantined = True

                elif trades < self.forward_mature_min_trades:
                    trust = "CONFIRMING"
                    quarantined = False

                else:
                    trust = "MATURE"
                    quarantined = False

                result[key] = {
                    **row,
                    "pnl": round(
                        float(row["pnl"]),
                        2,
                    ),
                    "win_rate": round(
                        wr,
                        6,
                    ),
                    "win_rate_pct": round(
                        wr * 100.0,
                        2,
                    ),
                    "profit_factor": round(
                        pf,
                        4,
                    ),
                    "break_even_win_rate": round(
                        BREAK_EVEN_WR,
                        6,
                    ),
                    "trust": trust,
                    "quarantined": quarantined,
                }

            return result

        groups_final = finalize(
            groups
        )

        paths_final = finalize(
            path_groups
        )

        overall_wr = (
            wins_total / total
            if total
            else 0.0
        )

        overall_pf = self._pf(
            wins_total,
            losses_total,
        )

        return {
            "version":
                "V6.6_FORWARD_PERFORMANCE_INTELLIGENCE",
            "forward_trades":
                total,
            "wins":
                wins_total,
            "losses":
                losses_total,
            "win_rate":
                round(
                    overall_wr,
                    6,
                ),
            "win_rate_pct":
                round(
                    overall_wr
                    * 100.0,
                    2,
                ),
            "profit_factor":
                round(
                    overall_pf,
                    4,
                ),
            "total_pnl":
                round(
                    pnl_total,
                    2,
                ),
            "by_market_direction_bucket_path":
                groups_final,
            "by_entry_path":
                paths_final,
            "live_execution":
                False,
        }

    def forward_gate(
        self,
        *,
        watchers: Iterable[Dict[str, Any]],
        market: str,
        direction: str,
        confidence: float,
        entry_path: str,
    ) -> Dict[str, Any]:
        intelligence = (
            self.forward_performance(
                watchers
            )
        )

        key = self.forward_key(
            market=market,
            direction=direction,
            confidence=confidence,
            entry_path=entry_path,
        )

        evidence = (
            intelligence[
                "by_market_direction_bucket_path"
            ].get(
                key
            )
        )

        # Do not overreact before enough genuine forward evidence exists.
        if evidence is None:
            return {
                "allowed": True,
                "status": "LEARNING",
                "reason": (
                    "No settled forward evidence for this exact setup yet."
                ),
                "key": key,
            }

        if evidence.get(
            "quarantined"
        ):
            return {
                "allowed": False,
                "status": "QUARANTINED",
                "reason": (
                    "Genuine forward performance fell below V6.6 trust limits."
                ),
                "key": key,
                "evidence": evidence,
            }

        return {
            "allowed": True,
            "status": evidence.get(
                "trust",
                "LEARNING",
            ),
            "reason": (
                "Forward evidence has not triggered quarantine."
            ),
            "key": key,
            "evidence": evidence,
        }

    # --------------------------------------------------------
    # adaptive watcher cadence + smarter expiry
    # --------------------------------------------------------

    def watcher_timing(
        self,
        *,
        watcher: Dict[str, Any],
        confidence: float,
        adaptive_gate: Optional[Dict[str, Any]],
        ai_up: float,
        rsi: float,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        now = float(
            now
            if now is not None
            else time.time()
        )

        direction = str(
            watcher.get(
                "direction"
            )
            or ""
        ).upper()

        created_at = float(
            watcher.get(
                "created_at"
            )
            or now
        )

        age_minutes = max(
            0.0,
            (
                now
                - created_at
            )
            / 60.0,
        )

        c = float(
            confidence
            or 0.0
        )

        gate_path = str(
            (
                adaptive_gate
                or {}
            ).get(
                "path"
            )
            or ""
        ).upper()

        # Near an actionable adaptive/normal region: recheck every 15 min.
        near_entry = (
            c >= 0.35
            or gate_path
            == "ADAPTIVE_QUALIFIED"
        )

        interval_minutes = (
            15
            if near_entry
            else 30
        )

        directional_strength = (
            ai_up
            if direction == "BUY"
            else 1.0 - ai_up
        )

        # Faster early expiry when the verified thesis has clearly weakened.
        clearly_weak = (
            age_minutes >= 30.0
            and c < 0.25
            and directional_strength < 0.55
        )

        # Overextended against a clean entry for too long.
        overextended = (
            direction == "BUY"
            and rsi >= 72.0
        ) or (
            direction == "SELL"
            and rsi <= 28.0
        )

        overextended_stale = (
            age_minutes >= 45.0
            and overextended
        )

        # Hard cap: 60 minutes for unentered verified setups.
        hard_expire_at = (
            created_at
            + 60.0 * 60.0
        )

        expire_now = (
            clearly_weak
            or overextended_stale
            or now >= hard_expire_at
        )

        if clearly_weak:
            reason = (
                "Verified thesis weakened: confidence <25% and "
                "directional probability <55% after 30 minutes."
            )

        elif overextended_stale:
            reason = (
                "Setup remained overextended for at least 45 minutes."
            )

        elif now >= hard_expire_at:
            reason = (
                "V6.6 one-hour verified-watcher lifetime reached."
            )

        else:
            reason = (
                "Near-entry setup: 15-minute cadence."
                if near_entry
                else
                "Weak/distant setup: 30-minute cadence."
            )

        return {
            "interval_minutes":
                interval_minutes,
            "next_check_at":
                now
                + interval_minutes
                * 60,
            "hard_expire_at":
                hard_expire_at,
            "expire_now":
                expire_now,
            "reason":
                reason,
            "age_minutes":
                round(
                    age_minutes,
                    2,
                ),
            "near_entry":
                near_entry,
        }

    # --------------------------------------------------------
    # FX exposure/correlation portfolio gate
    # --------------------------------------------------------

    @staticmethod
    def pair(
        symbol: str,
    ) -> Optional[Tuple[str, str]]:
        clean = str(
            symbol
            or ""
        ).upper().replace(
            "=X",
            "",
        ).replace(
            "/",
            "",
        )

        if len(clean) != 6:
            return None

        return (
            clean[:3],
            clean[3:],
        )

    @staticmethod
    def currency_signed_exposure(
        symbol: str,
        direction: str,
    ) -> Dict[str, int]:
        pair = V66Intelligence.pair(
            symbol
        )

        if pair is None:
            return {}

        base, quote = pair
        direction = str(
            direction
        ).upper()

        if direction == "BUY":
            return {
                base: 1,
                quote: -1,
            }

        if direction == "SELL":
            return {
                base: -1,
                quote: 1,
            }

        return {}

    def portfolio_gate(
        self,
        *,
        open_watchers: Iterable[Dict[str, Any]],
        candidate_symbol: str,
        candidate_direction: str,
        correlations: Optional[Dict[str, Dict[str, float]]] = None,
    ) -> Dict[str, Any]:
        candidate_exp = (
            self.currency_signed_exposure(
                candidate_symbol,
                candidate_direction,
            )
        )

        exposure_totals: Dict[str, int] = defaultdict(
            int
        )

        open_items = []

        for item in open_watchers:
            if str(
                item.get(
                    "status"
                )
                or ""
            ).upper() != "OPEN":
                continue

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

            exp = self.currency_signed_exposure(
                symbol,
                direction,
            )

            for currency, signed in exp.items():
                exposure_totals[
                    currency
                ] += signed

            open_items.append(
                {
                    "symbol":
                        symbol,
                    "direction":
                        direction,
                }
            )

        projected = dict(
            exposure_totals
        )

        blocks: List[str] = []

        for currency, signed in candidate_exp.items():
            projected[
                currency
            ] = (
                projected.get(
                    currency,
                    0,
                )
                + signed
            )

            if abs(
                projected[
                    currency
                ]
            ) > self.max_currency_exposure:
                blocks.append(
                    f"{currency} projected directional exposure "
                    f"{projected[currency]} exceeds "
                    f"{self.max_currency_exposure}."
                )

        correlation_hits = []

        correlations = (
            correlations
            or {}
        )

        candidate_clean = str(
            candidate_symbol
        ).upper()

        for item in open_items:
            open_symbol = str(
                item[
                    "symbol"
                ]
            ).upper()

            corr = (
                correlations
                .get(
                    candidate_clean,
                    {}
                )
                .get(
                    open_symbol
                )
            )

            if corr is None:
                corr = (
                    correlations
                    .get(
                        open_symbol,
                        {}
                    )
                    .get(
                        candidate_clean
                    )
                )

            if corr is None:
                continue

            corr = float(
                corr
            )

            if abs(
                corr
            ) >= self.high_correlation_abs:
                correlation_hits.append({
                    "symbol":
                        open_symbol,
                    "correlation":
                        round(
                            corr,
                            4,
                        ),
                })

        if len(
            correlation_hits
        ) >= self.max_highly_correlated_open:
            blocks.append(
                "Candidate is highly correlated with an existing open FX trade."
            )

        return {
            "allowed":
                len(
                    blocks
                ) == 0,
            "blocks":
                blocks,
            "projected_currency_exposure":
                projected,
            "high_correlation_hits":
                correlation_hits,
            "correlation_threshold":
                self.high_correlation_abs,
            "paper_only":
                True,
        }

    def status(self) -> Dict[str, Any]:
        return {
            "version":
                "V6.6",
            "forward_quarantine_min_trades":
                self.forward_quarantine_min_trades,
            "forward_mature_min_trades":
                self.forward_mature_min_trades,
            "forward_min_win_rate":
                self.forward_min_win_rate,
            "forward_min_profit_factor":
                self.forward_min_profit_factor,
            "watcher_near_entry_check_minutes":
                15,
            "watcher_default_check_minutes":
                30,
            "watcher_hard_expiry_minutes":
                60,
            "max_currency_exposure":
                self.max_currency_exposure,
            "high_correlation_abs":
                self.high_correlation_abs,
            "live_execution":
                False,
        }
