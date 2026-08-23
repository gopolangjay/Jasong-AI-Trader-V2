from __future__ import annotations

import math
import os
import statistics
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, Optional


VERSION = "v63-risk-exit-v1"


@dataclass(frozen=True)
class RiskPlan:
    version: str
    category: str
    direction: str
    entry_price: float
    stop_pct: float
    target_r: float
    stop_distance: float
    target_distance: float
    protective_stop_price: float
    take_profit_target_price: float
    source: str

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


# These are volatility guardrails, not fixed stops.
# The actual stop is derived from recent realised bar movement, then clamped
# inside the category-specific range.
CATEGORY_STOP_PCT_BOUNDS = {
    "FOREX": (0.08, 0.40),
    "INDICES": (0.18, 0.90),
    "CRYPTO": (0.60, 2.50),
    "METALS": (0.25, 1.20),
    "ENERGY": (0.35, 1.50),
    "SHARES": (0.45, 2.00),
}


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        out = float(value)
        return out if math.isfinite(out) else default
    except Exception:
        return default


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    value = _safe_float(os.getenv(name), default)
    if value is None:
        value = default
    return max(lo, min(hi, value))


def _recent_absolute_return_pct(values: Iterable[Any]) -> float:
    cleaned = []
    for raw in list(values or [])[-24:]:
        value = _safe_float(raw)
        if value is None:
            continue
        cleaned.append(abs(value) * 100.0)
    if not cleaned:
        return 0.0

    # Median is deliberately used instead of standard deviation so a single
    # gap/candle cannot make the next stop absurdly wide.
    return float(statistics.median(cleaned))


def build_risk_plan(
    candidate: Dict[str, Any],
    *,
    entry_price: float,
    direction: str,
) -> RiskPlan:
    entry = _safe_float(entry_price)
    if entry is None or entry <= 0:
        raise ValueError("entry_price must be a positive finite number")

    clean_direction = str(direction or "").upper().strip()
    if clean_direction not in {"BUY", "SELL"}:
        raise ValueError("direction must be BUY or SELL")

    category = str(candidate.get("category") or "UNKNOWN").upper().strip()
    floor_pct, cap_pct = CATEGORY_STOP_PCT_BOUNDS.get(category, (0.25, 1.50))

    vol_multiplier = _env_float(
        "CATEGORY_RISK_VOL_MULTIPLIER",
        2.5,
        0.5,
        10.0,
    )
    spread_multiplier = _env_float(
        "CATEGORY_RISK_SPREAD_MULTIPLIER",
        1.5,
        0.0,
        10.0,
    )
    target_r = _env_float(
        "CATEGORY_TAKE_PROFIT_R",
        1.5,
        0.25,
        10.0,
    )

    median_abs_return_pct = _recent_absolute_return_pct(
        candidate.get("recent_returns") or []
    )

    # 1 bp = 0.01 percent.
    spread_bps = _safe_float(
        candidate.get("ig_spread_bps")
        if candidate.get("ig_spread_bps") is not None
        else candidate.get("spread_bps"),
        0.0,
    ) or 0.0
    spread_pct = max(0.0, spread_bps * 0.01)

    raw_stop_pct = (
        median_abs_return_pct * vol_multiplier
        + spread_pct * spread_multiplier
    )
    stop_pct = max(floor_pct, min(cap_pct, raw_stop_pct))

    stop_distance = entry * (stop_pct / 100.0)
    target_distance = stop_distance * target_r

    if clean_direction == "BUY":
        protective_stop = entry - stop_distance
        take_profit = entry + target_distance
    else:
        protective_stop = entry + stop_distance
        take_profit = entry - target_distance

    if protective_stop <= 0 or take_profit <= 0:
        raise ValueError("calculated broker risk levels are invalid")

    source = (
        f"MEDIAN_ABS_RETURN_X{vol_multiplier:g}"
        f"+SPREAD_X{spread_multiplier:g}"
        f"_CLAMP_{floor_pct:g}_{cap_pct:g}_PCT"
    )

    return RiskPlan(
        version=VERSION,
        category=category,
        direction=clean_direction,
        entry_price=round(entry, 10),
        stop_pct=round(stop_pct, 6),
        target_r=round(target_r, 6),
        stop_distance=round(stop_distance, 10),
        target_distance=round(target_distance, 10),
        protective_stop_price=round(protective_stop, 10),
        take_profit_target_price=round(take_profit, 10),
        source=source,
    )
