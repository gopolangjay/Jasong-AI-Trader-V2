"""Entry validation and broker-confirmed results for the IG DEMO trial."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
import math
import re
from zoneinfo import ZoneInfo

from openai_policy import GUARDED_DEMO_POLICY
from runtime_version import VERSION

TRIAL_ID = "IG_DEMO_ENTRY_TRIAL_V1"
LIVE_TRIAL_ID = "IG_DEMO_GUARDED_TRIAL_V1"
QUOTE_MAX_AGE = 180
M15_SECONDS = 900
CANDLE_MAX_AGE = 1200
SAST = ZoneInfo("Africa/Johannesburg")
_DEFER_ENTRY = re.compile(
    r"\b(?:wait (?:for|until)|only after)\b"
    r"|\bif (?:price|it|the pair|the market) (?:cannot|can(?:not|'t)? hold|fails? to|breaks? (?:above|below))\b",
    re.I,
)
_QUOTED_FX_PAIR = re.compile(r"\b([A-Z]{3})\s*/\s*([A-Z]{3})\b")


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _yen_direction(symbol, side):
    if len(symbol) == 6 and symbol.isalpha() and side in {"BUY", "SELL"}:
        if symbol.endswith("JPY"):
            return "SHORT_JPY" if side == "BUY" else "LONG_JPY"
        if symbol.startswith("JPY"):
            return "LONG_JPY" if side == "BUY" else "SHORT_JPY"
    return None


def _market_symbol(position):
    symbol = str(position.get("symbol") or "").upper()
    name = str(position.get("market_name") or "").upper()
    epic = str(position.get("epic") or "").upper()
    if symbol:
        return re.sub(r"[^A-Z0-9]", "", symbol)
    if pair := _QUOTED_FX_PAIR.search(name):
        return "".join(pair.groups())
    if "BITCOIN" in epic.split("."):
        return "BITCOIN"
    if "GOLD" in epic.split(".") or name.startswith("SPOT GOLD"):
        return "GOLD"
    return next((part for part in epic.split(".") if len(part) == 6 and part.isalpha()), "")


def entry_exposure_issue(candidate, open_positions):
    symbol = candidate["symbol"]
    epic = candidate["ig_epic"].upper()
    yen = _yen_direction(symbol, candidate["direction"])
    for row in open_positions:
        existing_symbol = _market_symbol(row)
        if existing_symbol == symbol or str(row.get("epic") or "").upper() == epic:
            return "MARKET_ALREADY_OPEN"
        if yen and _yen_direction(existing_symbol, row.get("direction")) == yen:
            return "SAME_DIRECTION_YEN_EXPOSURE"
    return None


def entry_action_issue(candidate):
    action = (candidate.get("trial_entry") or {}).get("action")
    if action != "ENTER_NOW":
        return "MODEL_WAIT" if action == "WAIT" else "ENTRY_DECISION_UNAVAILABLE"
    if _DEFER_ENTRY.search((candidate.get("openai_analysis") or {}).get("reason") or ""):
        return "CONDITIONAL_ENTRY_RATIONALE"
    return None


def _confirmation_met(condition, candles, checked_at, published_at):
    rule = condition.get("rule")
    level = _number(condition.get("price"))
    if rule not in {"CLOSE_ABOVE", "CLOSE_BELOW", "HOLD_ABOVE", "HOLD_BELOW"} or level is None or level <= 0:
        return False
    required = 2 if rule.startswith("HOLD") else 1
    if len(candles) < required:
        return False
    recent = candles[-required:]
    last_open = recent[-1][0]
    closed_at = last_open + M15_SECONDS
    if published_at < closed_at or not 0 <= checked_at - closed_at <= CANDLE_MAX_AGE:
        return False
    # A gap cannot count as two consecutive closes.
    sign = 1 if rule.endswith("_ABOVE") else -1
    return all(
        opened_at == last_open - (required - 1 - index) * M15_SECONDS
        and opened_at % M15_SECONDS == 0 and 0 < close and sign * (close - level) > 0
        for index, (opened_at, close) in enumerate(recent)
    )


def evaluate_entry(candidate, quote, open_positions, *, now=None):
    """Validate the decision against closed candles, the IG quote and account exposure."""
    trial_decision = candidate.get("trial_entry") or {}
    result = {"trial_id": LIVE_TRIAL_ID, "action": trial_decision.get("action"), "admitted": False}
    if issue := entry_action_issue(candidate):
        return {**result, "reason": issue}

    observed_at = _number(quote.get("observed_at"))
    checked_at = observed_at if now is None else now
    published_at = _number(quote.get("update_timestamp_utc"))
    bid, offer = _number(quote.get("bid")), _number(quote.get("offer"))
    delay = _number(quote.get("delay_time"))
    epic = candidate.get("ig_epic")
    if (
        quote.get("source") != "IG_DEMO_MARKET_DETAILS"
        or not epic or quote.get("epic") != epic
        or checked_at is None or observed_at is None or published_at is None
        or not published_at <= observed_at <= checked_at <= published_at + QUOTE_MAX_AGE
        or delay not in (None, 0)
        or bid is None or offer is None or not 0 < bid <= offer
    ):
        return {**result, "reason": "FRESH_IG_QUOTE_REQUIRED"}
    decision_at = _number(candidate.get("trial_decision_at"))
    if decision_at is None or not 0 <= checked_at - decision_at <= 300:
        return {**result, "reason": "ENTRY_DECISION_STALE"}
    condition = trial_decision.get("confirmation") or {}
    if not _confirmation_met(condition, candidate.get("trial_m15_closes") or [], checked_at, published_at):
        return {**result, "reason": "COMPLETED_M15_TRIGGER_REQUIRED"}

    side = candidate.get("direction")
    stop = _number(candidate.get("model_stop_price"))
    target = _number(candidate.get("model_target_price"))
    spread_bps = (offer - bid) / ((offer + bid) / 2) * 10000
    cap = _number(candidate.get("spread_gate_bps"))
    entry = offer if side == "BUY" else bid
    result.update(spread_bps=round(spread_bps, 6), executable_quote=entry,
                  quote_timestamp=published_at)
    if cap is None or not 0 <= spread_bps <= cap:
        return {**result, "reason": "ENTRY_SPREAD_TOO_WIDE"}
    above = condition["rule"].endswith("_ABOVE")
    if (above and bid <= condition["price"]) or (not above and offer >= condition["price"]):
        return {**result, "reason": "QUOTE_REVERSED_CONFIRMATION"}
    if stop is None or target is None or not (
        (side == "BUY" and 0 < stop < bid <= offer < target)
        or (side == "SELL" and 0 < target < bid <= offer < stop)
    ):
        return {**result, "reason": "QUOTE_OUTSIDE_STOP_TARGET"}
    reward = target - entry if side == "BUY" else entry - target
    if reward <= offer - bid:
        return {**result, "reason": "TARGET_MOVE_TOO_SMALL_AFTER_SPREAD"}

    if issue := entry_exposure_issue(candidate, open_positions):
        return {**result, "reason": issue}
    return {**result, "admitted": True, "reason": "ENTER_NOW_CONFIRMED"}


def _cash_from_close(row):
    receipt = row.get("close_result") or {}
    cash = _number(receipt.get("profit"))
    currency = receipt.get("profitCurrency")
    if (
        receipt.get("environment") == "DEMO"
        and receipt.get("closeVerified") is True
        and receipt.get("status") == "CLOSED_VERIFIED"
        and receipt.get("dealStatus") == "ACCEPTED"
        and receipt.get("remainingSize") == 0
        and receipt.get("profitSource") == "IG_CLOSE_CONFIRMATION"
        and cash is not None and isinstance(currency, str) and re.fullmatch("[A-Z]{3}", currency)
    ):
        return currency, Decimal(str(cash))
    return None


def _metrics(positions):
    settled = sorted((r for r in positions if r.get("closed_at")), key=lambda r: r["closed_at"])
    amounts = defaultdict(list)
    for row in settled:
        confirmed = _cash_from_close(row)
        if confirmed:
            currency, cash = confirmed
            amounts[currency].append(cash)
    by_currency = {}
    for currency, values in amounts.items():
        pnl = peak = drawdown = Decimal(0)
        for value in values:
            pnl += value
            peak = max(peak, pnl)
            drawdown = max(drawdown, peak - pnl)
        wins = sum(value > 0 for value in values)
        by_currency[currency] = {
            "confirmed_closes": len(values),
            "wins": wins,
            "losses": sum(value < 0 for value in values),
            "breakeven": sum(value == 0 for value in values),
            "win_rate_pct": round(wins / len(values) * 100, 2),
            "net_realized": float(pnl),
            "avg_per_confirmed_close": round(float(pnl / len(values)), 4),
            "max_realized_drawdown": float(drawdown),
        }
    missing = len(settled) - sum(len(values) for values in amounts.values())
    return {
        "entries": len(positions), "open_positions": sum(r.get("status") == "OPEN" for r in positions),
        "settled_positions": len(settled), "cash_pnl_unavailable": missing,
        "cash_pnl_complete": bool(settled) and missing == 0,
        "settled_sast_dates": sorted({datetime.fromtimestamp(r["closed_at"], SAST).date().isoformat() for r in settled}),
        "verified_pnl_by_currency": by_currency,
    }


def trial_status(positions, started_at):
    observed = [
        row for row in positions
        if row.get("execution_policy") == "AI_DISCRETIONARY_DEMO" and (row.get("opened_at") or 0) >= started_at
    ]
    admitted = [row for row in observed if (row.get("entry_trial") or {}).get("admitted") is True]
    return {
        "version": VERSION, "trial_id": TRIAL_ID, "started_at": started_at,
        "started_at_sast": datetime.fromtimestamp(started_at, SAST).isoformat(timespec="seconds"),
        "mode": "SHADOW_MATCHED_IG_DEMO_FILLS", "broker_orders_submitted": 0, "extra_model_calls": 0,
        "live_money_execution": False, "automatic_promotion": False,
        "pnl_basis": "IG_SETTLED_CLOSE_AMOUNTS_AFTER_SPREAD_BEFORE_API_HOSTING_AND_SEPARATE_FEES",
        "entry_outcomes": dict(Counter(
            (row.get("entry_trial") or {}).get("reason") or "ENTRY_DECISION_UNAVAILABLE" for row in observed
        )),
        "current_strategy": _metrics(observed), "guarded_trial": _metrics(admitted),
    }


def guarded_trial_status(positions, state):
    started_at = state["guarded_trial_started_at"]
    guarded = [
        row for row in positions
        if row.get("execution_policy") == GUARDED_DEMO_POLICY and (row.get("opened_at") or 0) >= started_at
    ]
    checked = {
        event["setup_id"]: event for event in state.get("journal", [])
        if event.get("event") == "GUARDED_ENTRY_CHECK"
    }
    return {
        "version": VERSION, "trial_id": LIVE_TRIAL_ID, "mode": "IG_DEMO_GUARDED_EXECUTION",
        "execution_policy": GUARDED_DEMO_POLICY, "started_at": started_at,
        "started_at_sast": datetime.fromtimestamp(started_at, SAST).isoformat(timespec="seconds"),
        "live_money_execution": False, "extra_model_calls": 0,
        "pnl_basis": "IG_SETTLED_CLOSE_AMOUNTS_AFTER_SPREAD_BEFORE_API_HOSTING_AND_SEPARATE_FEES",
        "live_demo_trial": _metrics(guarded),
        "open_positions_from_previous_strategy": sum(
            row.get("status") == "OPEN" and row.get("execution_policy") != GUARDED_DEMO_POLICY
            for row in positions
        ),
        "recent_entry_checks": dict(Counter(event["reason"] for event in checked.values())),
        "last_entry_check": state.get("last_guarded_entry_check"),
        "shadow_comparison": trial_status(positions, state["entry_trial_started_at"]),
    }
