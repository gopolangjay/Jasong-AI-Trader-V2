from __future__ import annotations

import hashlib
import json
from typing import Any, Dict


V57_ENGINE_VERSION = "6.0.0"
DEFAULT_SPREAD_BPS = 1.0
DEFAULT_SLIPPAGE_BPS = 0.5


def _normalise(value: Any) -> Any:
    """Convert a snapshot into deterministic JSON-safe primitives."""
    if isinstance(value, dict):
        return {
            str(key): _normalise(value[key])
            for key in sorted(value)
        }
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    if isinstance(value, float):
        return round(value, 10)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def snapshot_hash(snapshot: Dict[str, Any]) -> str:
    payload = json.dumps(
        _normalise(snapshot),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def freeze_forward_snapshot(
    watcher: Dict[str, Any],
    live_signal: Dict[str, Any],
    entry_price: float,
    stake: float,
    entry_time: float,
    spread_bps: float = DEFAULT_SPREAD_BPS,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
) -> Dict[str, Any]:
    """Freeze exactly what V6.0 knew when the forward trade was opened.

    The hash makes accidental strategy mutation detectable before settlement.
    It does not claim cryptographic non-repudiation; it is an integrity guard.
    """
    snapshot = {
        "engine_version": V57_ENGINE_VERSION,
        "watcher_id": watcher.get("watcher_id"),
        "market": watcher.get("market"),
        "symbol": watcher.get("symbol"),
        "direction": watcher.get("direction"),
        "risk_mode": watcher.get("risk_mode"),
        "entry_time": float(entry_time),
        "entry_price_raw": float(entry_price),
        "stake": float(stake),
        "payout": float(watcher.get("payout", 0.80) or 0.80),
        "period": watcher.get("period"),
        "interval": watcher.get("interval"),
        "interval_minutes": int(watcher.get("interval_minutes", 15) or 15),
        "holding_candles": int(watcher.get("holding_candles", 1) or 1),
        "holding_minutes": int(watcher.get("holding_minutes", 15) or 15),
        "threshold_pct": watcher.get("threshold_pct"),
        "deep_score": watcher.get("deep_score"),
        "adaptive_rank_score": watcher.get("adaptive_rank_score"),
        "historical_win_rate": watcher.get("win_rate"),
        "historical_trades": watcher.get("trades"),
        "historical_profit_factor": watcher.get("profit_factor"),
        "historical_max_drawdown": watcher.get("max_drawdown"),
        "sample_reliability": watcher.get("sample_reliability"),
        "wilson_lower_win_rate": watcher.get("wilson_lower_win_rate"),
        "strategy_health": watcher.get("strategy_health", "PROBATION"),
        "live_decision": live_signal.get("decision"),
        "live_confidence": live_signal.get("confidence"),
        "live_price": live_signal.get("price"),
        "live_rsi": live_signal.get("rsi"),
        "live_ai_up": live_signal.get("combined_up_probability"),
        "spread_bps": float(spread_bps),
        "slippage_bps": float(slippage_bps),
    }
    return {
        "snapshot": snapshot,
        "snapshot_hash": snapshot_hash(snapshot),
    }


def verify_forward_snapshot(
    snapshot: Dict[str, Any] | None,
    expected_hash: str | None,
) -> bool:
    if not snapshot or not expected_hash:
        return False
    return snapshot_hash(snapshot) == str(expected_hash)


def adverse_execution_price(
    raw_price: float,
    direction: str,
    leg: str,
    spread_bps: float = DEFAULT_SPREAD_BPS,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
) -> float:
    """Apply a small adverse spread/slippage assumption to paper execution.

    BUY entry / SELL exit pay upward; SELL entry / BUY exit pay downward.
    This is deliberately conservative and is only a paper-test assumption.
    """
    raw = float(raw_price)
    bps = max(0.0, float(spread_bps)) + max(0.0, float(slippage_bps))
    fraction = bps / 10000.0
    side = str(direction or "").upper()
    leg = str(leg or "").upper()

    upward = (
        (side == "BUY" and leg == "ENTRY")
        or (side == "SELL" and leg == "EXIT")
    )
    return raw * (1.0 + fraction if upward else 1.0 - fraction)
