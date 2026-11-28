from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from demo_entry_trial import TRIAL_ID, evaluate_entry, guarded_trial_status, trial_status
from openai_policy import GUARDED_DEMO_POLICY

NOW = datetime(2026, 11, 27, 12, 30, 30, tzinfo=timezone.utc).timestamp()
M15 = 900
OPEN = int(NOW // M15 * M15)


def signal(symbol="USDJPY", side="BUY", action="ENTER_NOW", rule="CLOSE_ABOVE", level=159.20):
    return {
        "symbol": symbol, "direction": side, "ig_epic": f"IG.{symbol}",
        "model_stop_price": 159.12 if side == "BUY" else 159.60,
        "model_target_price": 159.60 if side == "BUY" else 159.12,
        "spread_gate_bps": 10,
        "trial_entry": {"action": action, "confirmation": {"rule": rule, "price": level}},
        "trial_decision_at": NOW - 1,
        "trial_m15_closes": [(OPEN - 2 * M15, 159.21), (OPEN - M15, 159.27)],
        "openai_analysis": {"reason": "Observed continuation supports entry."},
    }


def quote(symbol="USDJPY", **extra):
    return {"source": "IG_DEMO_MARKET_DETAILS", "epic": f"IG.{symbol}", "bid": 159.28,
            "offer": 159.30, "observed_at": NOW, "update_timestamp_utc": NOW - 3, "delay_time": 0, **extra}


def test_wait_future_conditions_and_missing_confirmation_cannot_admit():
    waiting = signal(action="WAIT")
    assert evaluate_entry(waiting, quote(), [])["reason"] == "MODEL_WAIT"
    unknown = {**signal(), "trial_entry": None}
    assert evaluate_entry(unknown, quote(), [])["reason"] == "ENTRY_DECISION_UNAVAILABLE"
    conditional = {**signal(), "openai_analysis": {"reason": "Enter if price cannot reclaim resistance."}}
    assert evaluate_entry(conditional, quote(), [])["reason"] == "CONDITIONAL_ENTRY_RATIONALE"
    for rule, level in (("CLOSE_ABOVE", 159.28), ("HOLD_ABOVE", 159.25)):
        assert evaluate_entry(signal(rule=rule, level=level), quote(), [])["reason"] == "COMPLETED_M15_TRIGGER_REQUIRED"
    for bars in ([(OPEN, 159.27)], [(OPEN - 3 * M15, 159.21), (OPEN - M15, 159.27)]):
        row = {**signal(rule="HOLD_ABOVE"), "trial_m15_closes": bars}
        assert evaluate_entry(row, quote(), [])["reason"] == "COMPLETED_M15_TRIGGER_REQUIRED"
    prior_quote = quote(update_timestamp_utc=OPEN - 1)
    assert evaluate_entry(signal(), prior_quote, [])["reason"] == "COMPLETED_M15_TRIGGER_REQUIRED"
    assert evaluate_entry(signal(rule="HOLD_ABOVE"), quote(), [])["admitted"] is True


def test_quote_must_be_fresh_for_exact_epic_and_leave_room_after_spread():
    for changes in (
        {"update_timestamp_utc": NOW - 181}, {"update_timestamp_utc": NOW + 1},
        {"update_timestamp_utc": None}, {"delay_time": 15},
        {"epic": "IG.DIFFERENT"}, {"bid": 159.32},
    ):
        assert evaluate_entry(signal(), {**quote(), **changes}, [])["reason"] == "FRESH_IG_QUOTE_REQUIRED"
    assert evaluate_entry({**signal(), "trial_decision_at": NOW - 301}, quote(), [])["reason"] == "ENTRY_DECISION_STALE"
    assert evaluate_entry(signal(), quote(), [], now=NOW + 181)["reason"] == "FRESH_IG_QUOTE_REQUIRED"
    outside = {**signal(), "model_stop_price": 190.56, "model_target_price": 191.05}
    assert evaluate_entry(outside, quote(), [])["reason"] == "QUOTE_OUTSIDE_STOP_TARGET"
    almost_at_target = {**signal(), "model_target_price": 159.31}
    assert evaluate_entry(almost_at_target, quote(), [])["reason"] == "TARGET_MOVE_TOO_SMALL_AFTER_SPREAD"
    low_cap = {**signal(), "spread_gate_bps": 0.1}
    assert evaluate_entry(low_cap, quote(), [])["reason"] == "ENTRY_SPREAD_TOO_WIDE"
    assert evaluate_entry(signal(), quote(), [])["executable_quote"] == 159.30
    assert evaluate_entry(signal(side="SELL", rule="CLOSE_BELOW", level=159.31), quote(), [])["executable_quote"] == 159.28
    assert evaluate_entry(signal(), quote(bid=159.19, offer=159.21), [])["reason"] == "QUOTE_REVERSED_CONFIRMATION"


def test_all_open_positions_including_legacy_and_manual_count_toward_exposure():
    position = {"symbol": "USDJPY", "epic": "IG.USDJPY", "direction": "BUY",
                "entry_trial": {"admitted": True, "trial_id": TRIAL_ID}}
    assert evaluate_entry(signal(side="SELL"), quote(), [position])["reason"] == "MARKET_ALREADY_OPEN"
    assert evaluate_entry(signal("GBPJPY"), quote("GBPJPY"), [position])["reason"] == "SAME_DIRECTION_YEN_EXPOSURE"
    assert evaluate_entry(signal("GBPJPY", side="SELL"), quote("GBPJPY"), [position])["admitted"] is True
    assert evaluate_entry(signal("EURUSD"), quote("EURUSD"), [position])["admitted"] is True
    rejected = {**position, "entry_trial": {"admitted": False}}
    assert evaluate_entry(signal(), quote(), [rejected])["reason"] == "MARKET_ALREADY_OPEN"
    manual = {"epic": "CS.D.USDJPY.MINI.IP", "market_name": "Weekend USD/JPY", "direction": "BUY"}
    assert evaluate_entry(signal("GBPJPY"), quote("GBPJPY"), [manual])["reason"] == "SAME_DIRECTION_YEN_EXPOSURE"


def test_trial_metrics_use_only_broker_cash_in_each_currency_and_ignore_old_trades():
    def settled(offset, amount, *, currency="USD", admitted=True):
        return {
            "execution_policy": "AI_DISCRETIONARY_DEMO", "opened_at": NOW + offset,
            "closed_at": NOW + offset + 1, "status": "CLOSED",
            "entry_trial": {"admitted": admitted, "reason": "ENTER_NOW_CONFIRMED" if admitted else "MODEL_WAIT"},
            "close_result": {"status": "CLOSED_VERIFIED", "closeVerified": True, "remainingSize": 0,
                             "dealStatus": "ACCEPTED", "environment": "DEMO", "profit": amount,
                             "profitCurrency": currency, "profitSource": "IG_CLOSE_CONFIRMATION"},
        }

    rows = [settled(4, -2), settled(1, 10), settled(3, -20, admitted=False),
            settled(5, None), settled(6, 4, currency="JPY"), settled(-5, -999)]
    status = trial_status(rows, NOW)
    assert status["broker_orders_submitted"] == 0
    assert status["extra_model_calls"] == 0
    assert status["current_strategy"]["entries"] == 5
    assert status["guarded_trial"]["entries"] == 4
    assert status["guarded_trial"]["cash_pnl_unavailable"] == 1
    assert status["guarded_trial"]["cash_pnl_complete"] is False
    assert status["entry_outcomes"] == {"ENTER_NOW_CONFIRMED": 4, "MODEL_WAIT": 1}
    assert status["current_strategy"]["verified_pnl_by_currency"]["USD"]["net_realized"] == -12
    assert status["guarded_trial"]["verified_pnl_by_currency"]["USD"] == {
        "confirmed_closes": 2, "wins": 1, "losses": 1, "breakeven": 0, "net_realized": 8,
        "win_rate_pct": 50, "avg_per_confirmed_close": 4, "max_realized_drawdown": 2,
    }
    assert status["guarded_trial"]["verified_pnl_by_currency"]["JPY"]["net_realized"] == 4
    live = guarded_trial_status(
        [*rows, {**settled(7, 15), "execution_policy": GUARDED_DEMO_POLICY}],
        {"entry_trial_started_at": NOW, "guarded_trial_started_at": NOW + 7},
    )
    assert live["live_demo_trial"]["entries"] == 1
    assert live["live_demo_trial"]["verified_pnl_by_currency"]["USD"]["net_realized"] == 15
    assert live["shadow_comparison"]["current_strategy"]["entries"] == 5
