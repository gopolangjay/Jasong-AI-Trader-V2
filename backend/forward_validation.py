from __future__ import annotations

import hashlib
import math
import os
import random
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional

from forward_store import ForwardStore


@dataclass(frozen=True)
class ForwardValidationConfig:
    min_settled_trades_for_prime: int = 12
    rolling_window_trades: int = 40
    min_profit_factor: float = 1.20
    min_expectancy_r: float = 0.05
    min_win_rate: float = 0.45
    min_bootstrap_prob_positive_expectancy: float = 0.75
    max_drawdown_r: float = 6.0
    bootstrap_samples: int = 2000

    @classmethod
    def from_env(cls) -> "ForwardValidationConfig":
        def integer(name: str, default: int, lo: int, hi: int) -> int:
            try:
                value = int(os.getenv(name, str(default)))
            except Exception:
                value = default
            return max(lo, min(hi, value))

        def number(name: str, default: float, lo: float, hi: float) -> float:
            try:
                value = float(os.getenv(name, str(default)))
            except Exception:
                value = default
            return max(lo, min(hi, value))

        return cls(
            min_settled_trades_for_prime=integer("FORWARD_MIN_SETTLED_TRADES_FOR_PRIME", 12, 3, 1000),
            rolling_window_trades=integer("FORWARD_ROLLING_WINDOW_TRADES", 40, 12, 1000),
            min_profit_factor=number("FORWARD_MIN_PROFIT_FACTOR", 1.20, 0.0, 100.0),
            min_expectancy_r=number("FORWARD_MIN_EXPECTANCY_R", 0.05, -10.0, 10.0),
            min_win_rate=number("FORWARD_MIN_WIN_RATE", 0.45, 0.0, 1.0),
            min_bootstrap_prob_positive_expectancy=number("FORWARD_MIN_BOOTSTRAP_POSITIVE_EXPECTANCY", 0.75, 0.0, 1.0),
            max_drawdown_r=number("FORWARD_MAX_DRAWDOWN_R", 6.0, 0.0, 1000.0),
            bootstrap_samples=integer("FORWARD_BOOTSTRAP_SAMPLES", 2000, 200, 20000),
        )


class ForwardValidationEngine:
    """PRIME authority based only on post-deployment broker-settled evidence."""

    VERSION = "6.3-clean-core-forward-r-v1"

    def __init__(
        self,
        *,
        store: ForwardStore,
        evidence_source: Callable[[], Iterable[Dict[str, Any]]],
        config: Optional[ForwardValidationConfig] = None,
    ) -> None:
        self.store = store
        self.evidence_source = evidence_source
        self.config = config or ForwardValidationConfig.from_env()

    @staticmethod
    def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
        try:
            number = float(value)
            return number if math.isfinite(number) else default
        except Exception:
            return default

    @classmethod
    def _normalise(cls, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        result = str(row.get("broker_result") or row.get("result") or "").upper().strip()
        if result not in {"WIN", "LOSS"}:
            return None
        output = dict(row)
        output["broker_result"] = result
        output["trade_id"] = str(row.get("trade_id") or row.get("ig_deal_id") or row.get("deal_id") or "").strip()
        if not output["trade_id"]:
            return None
        output["strategy_id"] = str(row.get("strategy_id") or row.get("selected_strategy") or "UNKNOWN").upper().strip()
        output["symbol"] = row.get("symbol") or row.get("market")

        explicit_candidate = None
        for key in ("r_multiple", "realized_r", "realised_r", "result_r"):
            if row.get(key) is not None:
                explicit_candidate = row.get(key)
                break
        explicit_r = cls._safe_float(explicit_candidate)

        if explicit_r is not None:
            output["r_multiple"] = explicit_r
            output["r_source"] = row.get("r_source") or "EXPLICIT_R"
        else:
            pnl = cls._safe_float(row.get("broker_pnl"))
            risk = cls._safe_float(
                row.get("planned_risk_cash")
                or row.get("risk_cash")
                or row.get("initial_risk_cash")
            )
            if pnl is not None and risk is not None and risk > 0:
                output["r_multiple"] = pnl / risk
                output["r_source"] = "BROKER_PNL_OVER_ENTRY_RISK"
            else:
                output["r_multiple"] = 1.0 if result == "WIN" else -1.0
                output["r_source"] = "BINARY_OUTCOME_R_FALLBACK"
        return output

    def sync(self) -> int:
        rows = []
        try:
            source = self.evidence_source() or []
        except Exception:
            source = []
        for raw in source:
            if isinstance(raw, dict):
                row = self._normalise(raw)
                if row:
                    rows.append(row)
        return self.store.sync(rows)

    @staticmethod
    def _drawdown_r(values: List[float]) -> float:
        equity = 0.0
        peak = 0.0
        maximum = 0.0
        for value in reversed(values):
            equity += value
            peak = max(peak, equity)
            maximum = max(maximum, peak - equity)
        return maximum

    @staticmethod
    def _profit_factor(values: List[float]) -> float:
        gains = sum(value for value in values if value > 0)
        losses = abs(sum(value for value in values if value < 0))
        if losses <= 0:
            return 99.0 if gains > 0 else 0.0
        return gains / losses

    @staticmethod
    def _seed(values: List[float], key: str) -> int:
        payload = key + "|" + ",".join(f"{value:.8f}" for value in values)
        return int(hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16], 16)

    def _bootstrap_positive_probability(self, values: List[float], key: str) -> float:
        if not values:
            return 0.0
        rng = random.Random(self._seed(values, key))
        positive = 0
        n = len(values)
        for _ in range(self.config.bootstrap_samples):
            mean = sum(values[rng.randrange(n)] for _ in range(n)) / n
            if mean > 0:
                positive += 1
        return positive / self.config.bootstrap_samples

    def metrics(
        self,
        *,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        sync: bool = True,
    ) -> Dict[str, Any]:
        if sync:
            self.sync()
        rows = self.store.rows(strategy_id=strategy_id, symbol=symbol, limit=self.config.rolling_window_trades)
        values = [float(row.get("r_multiple") or 0.0) for row in rows]
        wins = sum(1 for row in rows if str(row.get("broker_result") or "").upper() == "WIN")
        settled = len(rows)
        win_rate = wins / settled if settled else 0.0
        profit_factor = self._profit_factor(values)
        expectancy = sum(values) / settled if settled else 0.0
        drawdown = self._drawdown_r(values)
        key = str(strategy_id or symbol or "ALL")
        bootstrap = self._bootstrap_positive_probability(values, key)
        checks = {
            "minimum_settled_trades": settled >= self.config.min_settled_trades_for_prime,
            "profit_factor": profit_factor >= self.config.min_profit_factor,
            "expectancy_r": expectancy >= self.config.min_expectancy_r,
            "win_rate": win_rate >= self.config.min_win_rate,
            "bootstrap_positive_expectancy": bootstrap >= self.config.min_bootstrap_prob_positive_expectancy,
            "max_drawdown_r": drawdown <= self.config.max_drawdown_r,
        }
        prime = all(checks.values())
        r_sources: Dict[str, int] = {}
        for row in rows:
            source = str(row.get("r_source") or "UNKNOWN")
            r_sources[source] = r_sources.get(source, 0) + 1
        return {
            "authority": "BROKER_SETTLED_FORWARD_ONLY",
            "strategy_id": strategy_id,
            "symbol": symbol,
            "settled_trades": settled,
            "rolling_window_trades": self.config.rolling_window_trades,
            "wins": wins,
            "losses": settled - wins,
            "win_rate": round(win_rate, 6),
            "win_rate_pct": round(win_rate * 100.0, 2),
            "profit_factor": round(profit_factor, 6),
            "expectancy_r": round(expectancy, 6),
            "max_drawdown_r": round(drawdown, 6),
            "bootstrap_probability_positive_expectancy": round(bootstrap, 6),
            "bootstrap_probability_positive_expectancy_pct": round(bootstrap * 100.0, 2),
            "r_sources": r_sources,
            "checks": checks,
            "prime_eligible": prime,
            "state": "PRIME" if prime else ("FORWARD_LEARNING" if settled else "BOOTSTRAP"),
            "thresholds": {
                "min_settled_trades_for_prime": self.config.min_settled_trades_for_prime,
                "min_profit_factor": self.config.min_profit_factor,
                "min_expectancy_r": self.config.min_expectancy_r,
                "min_win_rate": self.config.min_win_rate,
                "min_bootstrap_prob_positive_expectancy": self.config.min_bootstrap_prob_positive_expectancy,
                "max_drawdown_r": self.config.max_drawdown_r,
            },
        }

    def all_rows(self, limit: int = 200) -> List[Dict[str, Any]]:
        self.sync()
        return self.store.rows(limit=max(1, min(int(limit), 1000)))
