"""
Jasong AI Trader V6.3
Adaptive Confidence Qualification Gate

Purpose
-------
Allow a PAPER trade below the normal live confidence floor only when the exact
market + direction + confidence bucket has demonstrated:
    win rate >= 65%
    profit factor >= 1.50
    resolved observations >= 20

The gate is fail-closed:
- no calibration -> no adaptive override
- stale calibration -> no adaptive override
- missing bucket -> no adaptive override
- failed historical criteria -> no adaptive override

It never enables broker/live execution.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional


BUCKETS = (
    ("35_40", 0.35, 0.40),
    ("40_50", 0.40, 0.50),
    ("50_60", 0.50, 0.60),
    ("60_67", 0.60, 0.67),
    ("67_75", 0.67, 0.75),
    ("75_plus", 0.75, 1.01),
)


class AdaptiveConfidenceGate:
    def __init__(
        self,
        state_path: str = "/tmp/adaptive_confidence_state.json",
        target_win_rate: float = 0.65,
        min_profit_factor: float = 1.50,
        min_trades: int = 20,
        max_age_hours: float = 24.0,
        absolute_min_confidence: float = 0.35,
    ) -> None:
        self.state_path = Path(
            os.getenv(
                "ADAPTIVE_CONFIDENCE_STATE_PATH",
                "/tmp/adaptive_confidence_state.json",
            )
        )

        self.target_win_rate = float(
            target_win_rate
        )

        self.min_profit_factor = float(
            min_profit_factor
        )

        self.min_trades = int(
            min_trades
        )

        self.max_age_hours = float(
            max_age_hours
        )

        self.absolute_min_confidence = float(
            absolute_min_confidence
        )

        self._lock = threading.RLock()

        self._state = {
            "updated_at": None,
            "source_job_id": None,
            "source_analyzer": None,
            "qualified": {},
        }

        self.load()

    @staticmethod
    def bucket_for(confidence: float) -> Optional[str]:
        c = float(confidence)
        for name, low, high in BUCKETS:
            if low <= c < high:
                return name
        return None

    @staticmethod
    def _key(market: str, direction: str, bucket: str) -> str:
        return f"{market.upper()}|{direction.upper()}|{bucket}"

    def load(self) -> None:
        with self._lock:
            if not self.state_path.exists():
                return
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._state.update(data)
            except Exception:
                # Fail closed if persisted state is damaged.
                self._state = {
                    "updated_at": None,
                    "source_job_id": None,
                    "source_analyzer": None,
                    "qualified": {},
                }

    def save(self) -> None:
        with self._lock:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._state, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            tmp.replace(self.state_path)

    def update_from_calibration_job(self, job: Dict[str, Any]) -> Dict[str, Any]:
        if str(job.get("status", "")).upper() != "COMPLETED":
            raise ValueError("Calibration job must be COMPLETED.")

        result = job.get("result") or {}
        markets = result.get("markets") or []
        qualified: Dict[str, Any] = {}

        for market_result in markets:
            market = str(market_result.get("market") or "").upper()
            direction_buckets = market_result.get("direction_buckets") or {}
            for direction in ("BUY", "SELL"):
                buckets = direction_buckets.get(direction) or {}
                for bucket, stats in buckets.items():
                    trades = int(stats.get("trades") or 0)
                    wr = float(stats.get("win_rate") or 0.0)
                    pf = float(stats.get("profit_factor") or 0.0)

                    passes = (
                        trades >= self.min_trades
                        and wr >= self.target_win_rate
                        and pf >= self.min_profit_factor
                    )
                    if passes:
                        key = self._key(market, direction, bucket)
                        qualified[key] = {
                            "market": market,
                            "direction": direction,
                            "bucket": bucket,
                            "trades": trades,
                            "wins": int(stats.get("wins") or 0),
                            "losses": int(stats.get("losses") or 0),
                            "win_rate": wr,
                            "win_rate_pct": round(wr * 100.0, 2),
                            "profit_factor": pf,
                            "classification": "QUALIFIED",
                        }

        with self._lock:
            self._state = {
                "updated_at": time.time(),
                "source_job_id": job.get("job_id"),
                "source_analyzer": result.get("analyzer") or job.get("analyzer_version"),
                "criteria": {
                    "target_win_rate": self.target_win_rate,
                    "min_profit_factor": self.min_profit_factor,
                    "min_trades": self.min_trades,
                    "absolute_min_confidence": self.absolute_min_confidence,
                    "max_age_hours": self.max_age_hours,
                },
                "qualified": qualified,
            }
            self.save()

        return self.snapshot()

    def is_stale(self) -> bool:
        updated = self._state.get("updated_at")
        if not updated:
            return True
        return (time.time() - float(updated)) > self.max_age_hours * 3600.0

    def evaluate(
        self,
        market: str,
        direction: str,
        confidence: float,
        normal_min_confidence: float,
    ) -> Dict[str, Any]:
        c = float(confidence)
        normal_floor = float(normal_min_confidence)

        # Normal V6 confidence path requires no adaptive exception.
        if c >= normal_floor:
            return {
                "allowed_by_confidence": True,
                "path": "NORMAL_CONFIDENCE",
                "reason": "Confidence meets the normal V6 threshold.",
                "confidence": c,
                "normal_min_confidence": normal_floor,
            }

        if c < self.absolute_min_confidence:
            return {
                "allowed_by_confidence": False,
                "path": "REJECT",
                "reason": "Confidence is below the absolute 35% adaptive floor.",
                "confidence": c,
            }

        if self.is_stale():
            return {
                "allowed_by_confidence": False,
                "path": "REJECT",
                "reason": "Adaptive calibration is missing or stale.",
                "confidence": c,
            }

        bucket = self.bucket_for(c)
        if not bucket:
            return {
                "allowed_by_confidence": False,
                "path": "REJECT",
                "reason": "No confidence bucket matched.",
                "confidence": c,
            }

        key = self._key(market, direction, bucket)
        evidence = (self._state.get("qualified") or {}).get(key)

        if not evidence:
            return {
                "allowed_by_confidence": False,
                "path": "REJECT",
                "reason": "This market/direction/confidence bucket is not historically qualified.",
                "market": market.upper(),
                "direction": direction.upper(),
                "bucket": bucket,
                "confidence": c,
            }

        return {
            "allowed_by_confidence": True,
            "path": "ADAPTIVE_QUALIFIED",
            "reason": "Historical WR/PF/sample gate passed.",
            "market": market.upper(),
            "direction": direction.upper(),
            "bucket": bucket,
            "confidence": c,
            "evidence": evidence,
        }

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            state = json.loads(json.dumps(self._state))
        state["stale"] = self.is_stale()
        state["qualified_count"] = len(state.get("qualified") or {})
        state["paper_only"] = True
        state["broker_execution_enabled"] = False
        return state
