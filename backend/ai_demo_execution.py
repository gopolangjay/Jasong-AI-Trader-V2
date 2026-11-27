"""User-requested discretionary execution, confined to the IG DEMO adapter."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import time
import uuid

import pandas as pd

from openai_analyst import close_reason_issue


def position_key(deal_id):
    return hashlib.sha256(("JSCAT:"+str(deal_id)).encode()).hexdigest()[:32]


def demo_only(broker):
    status = broker.status()
    return (status.get("environment") == "DEMO" and status.get("live_money_execution") is False
            and str(status.get("base_url") or getattr(broker, "BASE_URL", ""))
            == "https://demo-api.ig.com/gateway/deal")


def entry_reasons(row, now):
    if not row.get("openai_validated") or row.get("direction") not in {"BUY", "SELL"}:
        return [row.get("openai_wait_reason") or "OPENAI_WAIT"]
    reasons = []
    try:
        stamp = pd.Timestamp(row["signal_timestamp"])
        if stamp.tzinfo is None or not 0 <= pd.Timestamp(now).timestamp()-stamp.timestamp() <= 300:
            reasons.append("OPENAI_DECISION_STALE")
    except (KeyError, ValueError, TypeError):
        reasons.append("OPENAI_DECISION_TIMESTAMP_INVALID")
    if not row.get("current_candle_strategy_complete"):
        reasons.append("VALID_CLOSED_CANDLES_REQUIRED")
    if not row.get("ig_epic") or not row.get("setup_id"):
        reasons.append("INSTRUMENT_OR_DECISION_ID_MISSING")
    return reasons


def _number(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


class DiscretionaryDemoMixin:
    """No application risk sizing, position caps, quote veto or attached orders.

    Input identity, durable once-only submissions, DEMO confinement and ownership
    are protocol correctness. IG remains the authority on acceptance and margin.
    """

    def discretionary_position_context(self):
        rows = self.positions(limit=1000)
        result = []
        for row in rows:
            if row.get("status") != "OPEN" or not str(row.get("deal_reference", "")).startswith("JSCAT_"):
                continue
            broker = row.get("broker") or {}
            direction = row.get("direction")
            bid, offer = _number(broker.get("bid")), _number(broker.get("offer"))
            exit_price = None
            if bid is not None and offer is not None and 0 < bid <= offer:
                exit_price = {"BUY": bid, "SELL": offer}.get(direction)
            result.append({
                "position_key": position_key(row["deal_id"]), "symbol": row.get("symbol"),
                "direction": direction, "size": _number(row.get("size")),
                "entry_price": _number(broker.get("level") or row.get("entry_level")),
                "current_bid": bid, "current_offer": offer, "indicative_exit_price": exit_price,
                "unrealized_profit_loss": _number(broker.get("profit_loss")),
                "quote_observed_at": row.get("last_seen_at"), "opened_at": row.get("opened_at"),
                "advisory_stop": row.get("model_stop_price"), "advisory_target": row.get("model_target_price"),
                "review_horizon_minutes": row.get("model_holding_minutes"),
                "original_rationale": row.get("openai_analysis", {}).get("reason"),
            })
        return result

    def _durable_submission(self, key, action, target, metadata=None):
        submissions = self._state.setdefault("ai_decision_submissions", {})
        if key in submissions:
            return False
        now = time.time()
        # Old decisions cannot be submitted after 300 seconds; retaining a day
        # of keys across restarts is sufficient for once-only submission.
        for old in list(submissions):
            if float(submissions[old].get("at") or 0) < now-86400:
                del submissions[old]
        submissions[key] = {"at": now, "action": action, "target": target, "state": "SUBMITTING", **(metadata or {})}
        try:
            path = Path(self.state_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".ai-tmp")
            temporary.write_text(json.dumps(self._state, default=str))
            os.replace(temporary, path)
        except Exception:
            submissions.pop(key, None)
            raise RuntimeError("AI_SUBMISSION_JOURNAL_NOT_DURABLE") from None
        return True

    def _discretionary_may_open(self, row):
        if not demo_only(self.broker):
            return False, "IG_DEMO_REQUIRED"
        if not self._is_active_strategy(row):
            return False, "UNKNOWN_AI_STRATEGY_OR_MARKET"
        reasons = entry_reasons(row, self._now())
        if reasons:
            return False, ", ".join(reasons)
        key = "OPEN:"+str(row["setup_id"])
        if key in self._state.get("ai_decision_submissions", {}):
            return False, "AI_DECISION_ALREADY_SUBMITTED"
        return True, "AI_DISCRETIONARY_DEMO"

    def _discretionary_open(self, candidate):
        allowed, reason = self._discretionary_may_open(candidate)
        if not allowed:
            return False
        key = "OPEN:"+candidate["setup_id"]
        try:
            size = candidate.get("openai_size")
            if size is None:
                size = getattr(self.broker, "default_size", None)
            size = _number(size)
            if size is None or size <= 0:
                raise RuntimeError("IG_DEMO_ORDER_SIZE_UNAVAILABLE")
            reference = ("JSCAT_AI_"+uuid.uuid4().hex[:20]).upper()
            metadata = {"deal_reference": reference, "symbol": candidate["symbol"],
                        "category": candidate["category"], "epic": candidate["ig_epic"],
                        "direction": candidate["direction"], "setup_id": candidate["setup_id"],
                        "strategy_id": candidate["strategy_id"]}
            if not self._durable_submission(key, "OPEN", candidate["symbol"], metadata):
                return False
            # Intentionally no stop_level, limit_level, max_spread_bps or
            # account-risk valuation. IG applies its native order rules.
            result = self.broker.open_epic_position(
                epic=candidate["ig_epic"], direction=candidate["direction"], size=size,
                deal_reference=reference, allow_size_increment_retry=True,
            ) or {}
            deal_id = result.get("dealId")
            if not deal_id or str(result.get("dealStatus", "")).upper() != "ACCEPTED":
                raise RuntimeError("IG_DEMO_OPEN_NOT_CONFIRMED")
            now = time.time()
            position = {
                "track": "CATEGORY", "category": candidate["category"],
                "symbol": candidate["symbol"], "market": candidate.get("market"),
                "direction": candidate["direction"], "epic": candidate["ig_epic"],
                "deal_id": deal_id, "deal_reference": result.get("dealReference") or reference,
                "size": result.get("size") or size, "entry_level": result.get("level"),
                "opened_at": now, "due_at": None, "status": "OPEN",
                "strategy_id": candidate["strategy_id"], "setup_id": candidate["setup_id"],
                "execution_policy": "AI_DISCRETIONARY_DEMO", "risk_per_trade_pct": None,
                "risk_cash_budget": None, "estimated_stop_risk_cash": None,
                "protective_stop_price": None, "take_profit_target_price": None,
                "model_stop_price": candidate.get("model_stop_price"),
                "model_target_price": candidate.get("model_target_price"),
                "model_holding_minutes": candidate.get("model_holding_minutes"),
                "openai_analysis": candidate.get("openai_analysis") or {},
                "broker_open_result": result, "live_money_execution": False,
            }
            self._state.setdefault("positions", []).append(position)
            self._state["opens"] = int(self._state.get("opens") or 0)+1
            self._state["ai_decision_submissions"][key].update(state="CONFIRMED", deal_id=deal_id)
            self._state["last_order_error"] = None
            self._journal("AI_DEMO_OPEN_CONFIRMED", symbol=candidate["symbol"], deal_id=deal_id,
                          setup_id=candidate["setup_id"])
            self._persist()
            return True
        except Exception as exc:
            if key in self._state.get("ai_decision_submissions", {}):
                self._state["ai_decision_submissions"][key]["state"] = "REJECTED_OR_UNCONFIRMED"
            self._state["last_order_error"] = {"at": time.time(), "symbol": candidate.get("symbol"),
                                               "error": f"{type(exc).__name__}: {exc}"}
            self._journal("AI_DEMO_OPEN_FAILED", **self._state["last_order_error"])
            self._persist()
            return False

    def _recover_discretionary_orders(self, broker_rows):
        """Recover broker-confirmed fills after a response timeout, by our own reference."""
        known = {r.get("deal_id") for r in self._state.get("positions", [])}
        by_reference = {s.get("deal_reference"): s for s in self._state.get("ai_decision_submissions", {}).values()
                        if s.get("action") == "OPEN" and s.get("deal_reference")}
        for row in broker_rows:
            submission = by_reference.get(row.get("deal_reference"))
            if not submission or not row.get("deal_id") or row["deal_id"] in known:
                continue
            tracked = {k:submission.get(k) for k in ["deal_reference", "symbol", "category", "epic",
                                                    "direction", "setup_id", "strategy_id"]}
            tracked.update(track="CATEGORY", deal_id=row["deal_id"], size=row.get("size"),
                           entry_level=row.get("level"), opened_at=submission["at"], status="OPEN", due_at=None,
                           broker=dict(row), last_seen_at=time.time(), execution_policy="AI_DISCRETIONARY_DEMO",
                           risk_per_trade_pct=None, protective_stop_price=None, take_profit_target_price=None,
                           confirmation_source="BROKER_REFERENCE_RECONCILIATION", live_money_execution=False)
            self._state.setdefault("positions", []).append(tracked)
            known.add(row["deal_id"])
            self._state["opens"] = int(self._state.get("opens") or 0)+1
            submission.update(state="CONFIRMED_BY_RECONCILIATION", deal_id=row["deal_id"])
            self._journal("AI_DEMO_OPEN_RECOVERED", deal_id=row["deal_id"], symbol=tracked["symbol"])

    def _discretionary_closes(self):
        engine = getattr(self, "analysis_engine", None)
        if not engine or not demo_only(self.broker):
            return
        decision = engine.decision_snapshot()
        if not decision or not 0 <= self._now().timestamp()-float(decision["created_at"]) <= 300:
            return
        by_key = {position_key(row["deal_id"]): row for row in self._open_positions()
                  if row.get("deal_id") and str(row.get("deal_reference", "")).startswith("JSCAT_")}
        for item in decision.get("position_decisions", []):
            if item.get("action") != "CLOSE" or close_reason_issue(item.get("reason")):
                continue
            row = by_key.get(item["position_key"])
            if row is None:
                continue
            key = "CLOSE:"+decision["context_id"]+":"+item["position_key"]
            try:
                if not self._durable_submission(key, "CLOSE", item["position_key"]):
                    continue
                result = self.broker.close_position(row["deal_id"]) or {}
                confirmed = result.get("closeVerified") is True or str(result.get("status", "")).upper() in {
                    "CLOSED_VERIFIED", "ALREADY_CLOSED_OR_NOT_FOUND"}
                row.update(close_result=result, model_close_action="CLOSE", model_close_reason=item["reason"],
                           model_close_context_id=decision["context_id"],
                           close_state="CLOSED_VERIFIED" if confirmed else "PENDING_OR_DEFERRED")
                if confirmed:
                    row.update(status="CLOSED", closed_at=time.time())
                    self._state["closes"] = int(self._state.get("closes") or 0)+1
                self._state["ai_decision_submissions"][key]["state"] = row["close_state"]
                self._state["last_model_close_error"] = None
                self._journal("AI_DEMO_CLOSE", deal_id=row["deal_id"], confirmed=confirmed,
                              reason=item["reason"])
            except Exception as exc:
                self._state["last_model_close_error"] = {"at": time.time(), "position_key": item["position_key"],
                                                        "error": f"{type(exc).__name__}: {exc}"}
            self._persist()

    def _discretionary_tick(self):
        with self._lock:
            try:
                if not demo_only(self.broker):
                    raise RuntimeError("IG_DEMO_REQUIRED")
                self._reconcile()
                if self.enabled and self.broker.configured():
                    rankings = self.ranking_source() or {}
                    self._discretionary_closes()
                    rows = [dict(row) for bucket in rankings.values() for row in bucket]
                    rows.sort(key=lambda row: row.get("openai_priority") or 999)
                    for row in rows:
                        self._discretionary_open(row)
                self._state["last_error"] = None
            except Exception as exc:
                self._state["last_error"] = f"{type(exc).__name__}: {exc}"
            self._state["last_tick_at"] = time.time()
            self._persist()
            return self.status()
