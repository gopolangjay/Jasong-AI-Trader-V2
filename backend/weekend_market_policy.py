from __future__ import annotations

"""Fail-closed weekend/holiday market eligibility for IG DEMO.

This module never assumes a symbol is open because of its asset class. A market
must be explicitly allow-listed as a supported strategy category AND IG must
report it tradeable with a usable quote at scan time. Ordinary FX and spot Gold
remain governed by their weekday/session strategies.
"""

import os
from typing import Any, Dict, Iterable, List, Mapping, Sequence

VERSION = "6.13-weekend-market-policy-v1"
STRATEGY_ID = "WEEKEND_24_7_STRUCTURE_V1"

# Categories for which the service may run the weekend structure strategy.
# Keep this deliberately narrow until a category has tests + risk rules.
DEFAULT_SUPPORTED_CATEGORIES = ("CRYPTO",)

CLOSED_STATUSES = {"CLOSED", "OFFLINE", "SUSPENDED", "EDITS_ONLY", "ON_AUCTION"}
OPEN_STATUSES = {"TRADEABLE"}


def supported_categories() -> set[str]:
    raw = os.getenv("JASONG_WEEKEND_SUPPORTED_CATEGORIES", "CRYPTO")
    return {x.strip().upper() for x in raw.split(",") if x.strip()}


def _number(value: Any) -> float | None:
    try:
        out = float(value)
        return out if out > 0 else None
    except (TypeError, ValueError):
        return None


def assess_market(snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    epic = str(snapshot.get("epic") or "").strip()
    name = str(snapshot.get("name") or snapshot.get("instrumentName") or epic).strip()
    category = str(snapshot.get("category") or snapshot.get("instrumentType") or "").upper().strip()
    status = str(snapshot.get("marketStatus") or snapshot.get("status") or "").upper().strip()
    bid = _number(snapshot.get("bid"))
    offer = _number(snapshot.get("offer") or snapshot.get("ask"))
    spread = (offer - bid) if bid is not None and offer is not None else None
    allowed_category = category in supported_categories()
    tradeable = status in OPEN_STATUSES
    quote_ok = bid is not None and offer is not None and offer >= bid
    eligible = bool(epic and allowed_category and tradeable and quote_ok)
    reasons: List[str] = []
    if not epic:
        reasons.append("MISSING_EPIC")
    if not allowed_category:
        reasons.append("UNSUPPORTED_CATEGORY")
    if not tradeable:
        reasons.append("IG_NOT_TRADEABLE")
    if not quote_ok:
        reasons.append("INVALID_OR_MISSING_QUOTE")
    return {
        "epic": epic,
        "name": name,
        "category": category,
        "market_status": status,
        "bid": bid,
        "offer": offer,
        "spread": spread,
        "eligible": eligible,
        "reasons": reasons,
        "strategy_id": STRATEGY_ID if eligible else None,
        "policy_version": VERSION,
    }


def discover_open_supported(markets: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Return only markets that are genuinely tradeable *now* on IG.

    Discovery is intentionally broker-driven: callers supply live IG market
    snapshots from search/watchlist endpoints. No static weekend-open assumption
    can make a market eligible.
    """
    assessed = [assess_market(m) for m in markets]
    eligible = [m for m in assessed if m["eligible"]]
    return sorted(eligible, key=lambda m: (m["spread"] is None, m["spread"] or 0.0, m["epic"]))


def execution_guard(snapshot: Mapping[str, Any], *, max_spread: float | None = None) -> Dict[str, Any]:
    result = assess_market(snapshot)
    reasons = list(result["reasons"])
    if max_spread is not None and result["spread"] is not None and result["spread"] > max_spread:
        reasons.append("SPREAD_TOO_WIDE")
    return {**result, "eligible": not reasons, "reasons": reasons}
