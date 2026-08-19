from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List


RECOMMENDATIONS = {
    "STALE_SIGNAL": "Tighten the maximum signal age before broker submission.",
    "STALE_BROKER_QUOTE": "Refresh the exact IG quote immediately before entry.",
    "EXCESSIVE_SPREAD_SLIPPAGE": "Reduce the spread/slippage ceiling or skip that liquidity window.",
    "WEAK_TREND_ENTRY": "Raise the ADX/trend-strength requirement for this strategy family.",
    "OVEREXTENDED_SHORT": "Require a bounce/retest before shorting an already exhausted move.",
    "OVEREXTENDED_LONG": "Require a pullback/retest before buying an already extended move.",
    "POOR_ENTRY_TIMING": "Delay entry until price confirms rather than entering on the first impulse.",
    "PROFIT_GIVEBACK": "Review exit/trailing logic so meaningful favourable excursion is protected.",
}


class StrategyLearningEngine:
    """Diagnoses recurring forward mistakes; never rewrites strategy code itself."""

    @staticmethod
    def _number(value: Any, default: float = 0.0) -> float:
        try:
            number = float(value)
            return number if math.isfinite(number) else default
        except Exception:
            return default

    def _flags(self, row: Dict[str, Any]) -> List[str]:
        result = str(row.get("broker_result") or row.get("result") or "").upper()
        if result != "LOSS":
            return []
        provenance = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
        snapshot = row.get("entry_snapshot") if isinstance(row.get("entry_snapshot"), dict) else {}
        flags: List[str] = []
        signal_age = self._number(provenance.get("signal_age_seconds"), 0.0)
        quote_age = self._number(provenance.get("quote_age_seconds"), 0.0)
        if signal_age > self._number(provenance.get("signal_max_age_seconds"), 300.0):
            flags.append("STALE_SIGNAL")
        if quote_age > self._number(provenance.get("quote_max_age_seconds"), 180.0):
            flags.append("STALE_BROKER_QUOTE")

        spread = self._number(snapshot.get("spread_bps", row.get("spread_bps")), 0.0)
        spread_limit = self._number(snapshot.get("spread_limit_bps", row.get("spread_limit_bps")), 0.0)
        slippage = abs(self._number(row.get("slippage_bps"), 0.0))
        if (spread_limit > 0 and spread > spread_limit) or (spread > 0 and slippage > max(2.0, spread * 0.5)):
            flags.append("EXCESSIVE_SPREAD_SLIPPAGE")

        strategy = str(row.get("strategy_id") or row.get("selected_strategy") or "").upper()
        adx = self._number(snapshot.get("adx", row.get("adx")), 100.0)
        rsi = self._number(snapshot.get("rsi", row.get("rsi", row.get("entry_rsi"))), 50.0)
        direction = str(row.get("direction") or "").upper()
        if "TREND" in strategy and adx < 20.0:
            flags.append("WEAK_TREND_ENTRY")
        if direction == "SELL" and rsi <= 28.0:
            flags.append("OVEREXTENDED_SHORT")
        if direction == "BUY" and rsi >= 72.0:
            flags.append("OVEREXTENDED_LONG")

        mfe = self._number(row.get("mfe_bps"), 0.0)
        mae = abs(self._number(row.get("mae_bps"), 0.0))
        if mae >= max(8.0, mfe * 1.5) and mfe < 5.0:
            flags.append("POOR_ENTRY_TIMING")
        if mfe >= max(5.0, mae * 0.5):
            flags.append("PROFIT_GIVEBACK")
        return list(dict.fromkeys(flags))

    def analyze(self, rows: Iterable[Dict[str, Any]], minimum_occurrences: int = 3) -> Dict[str, Any]:
        settled = [dict(row) for row in rows if isinstance(row, dict) and str(row.get("broker_result") or row.get("result") or "").upper() in {"WIN", "LOSS"}]
        losses = [row for row in settled if str(row.get("broker_result") or row.get("result") or "").upper() == "LOSS"]
        counts: Dict[str, Dict[str, Any]] = {}
        for row in losses:
            for flag in self._flags(row):
                item = counts.setdefault(flag, {"occurrences": 0, "strategies": {}, "markets": {}})
                item["occurrences"] += 1
                strategy = str(row.get("strategy_id") or row.get("selected_strategy") or "UNKNOWN")
                market = str(row.get("symbol") or row.get("market") or "UNKNOWN")
                item["strategies"][strategy] = item["strategies"].get(strategy, 0) + 1
                item["markets"][market] = item["markets"].get(market, 0) + 1
        findings = []
        for flag, data in counts.items():
            if data["occurrences"] < minimum_occurrences:
                continue
            findings.append({
                "mistake": flag,
                "occurrences": data["occurrences"],
                "loss_rate_pct": round(data["occurrences"] / max(1, len(losses)) * 100.0, 2),
                "strategies": data["strategies"],
                "markets": data["markets"],
                "recommendation": RECOMMENDATIONS[flag],
                "automatic_strategy_rewrite": False,
            })
        findings.sort(key=lambda item: item["occurrences"], reverse=True)
        return {
            "authority": "BROKER_SETTLED_FORWARD_ONLY",
            "settled_trades": len(settled),
            "losses_analyzed": len(losses),
            "minimum_occurrences": minimum_occurrences,
            "findings": findings,
            "automatic_strategy_rewrite": False,
        }
