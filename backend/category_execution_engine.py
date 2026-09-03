from __future__ import annotations

import json
import math
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from forex_liquidity_lines_strategy import (
    LIQUID_FOREX_PAIRS,
    STRATEGY_ID as FX_STRATEGY_ID,
)
from risk_exit_policy import build_risk_plan
from xauusd_liquidity_strategy import STRATEGY_ID as XAU_STRATEGY_ID


class RiskSizingError(RuntimeError):
    pass


class CategoryExecutionEngine:
    """Independent IG DEMO standard-category portfolio executor."""

    VERSION = "6.11-fx-xau-active-execution-v1"
    DEAL_PREFIX = "JSCAT_"
    ACTIVE_STRATEGY_IDS = (XAU_STRATEGY_ID, FX_STRATEGY_ID)
    ACTIVE_SYMBOLS = ("GOLD", *LIQUID_FOREX_PAIRS)

    def __init__(
        self,
        *,
        broker: Any,
        ranking_source: Callable[[], Dict[str, List[Dict[str, Any]]]],
        state_path: str,
        external_positions_source: Optional[Callable[[], List[Dict[str, Any]]]] = None,
        poll_seconds: Optional[int] = None,
    ) -> None:
        self.broker = broker
        self.ranking_source = ranking_source
        self.external_positions_source = external_positions_source
        self.state_path = state_path
        self.enabled = str(os.getenv("CATEGORY_AUTOTRADE", "true")).lower() in {"1", "true", "yes", "on"}
        self.poll_seconds = max(15, int(poll_seconds or os.getenv("CATEGORY_EXECUTION_POLL_SECONDS", "30")))
        self.max_open_positions = max(1, min(30, int(os.getenv("CATEGORY_MAX_OPEN_POSITIONS", "12"))))
        self.global_ig_max_positions = max(1, min(50, int(os.getenv("CATEGORY_GLOBAL_IG_MAX_POSITIONS", "15"))))
        self.max_per_category = max(1, min(5, int(os.getenv("CATEGORY_MAX_OPEN_PER_CATEGORY", "5"))))
        self.max_theme_exposure = max(1, min(10, int(os.getenv("CATEGORY_MAX_THEME_EXPOSURE", "3"))))
        self.max_tracks_per_epic = max(1, min(2, int(os.getenv("CATEGORY_MAX_TRACKS_PER_EPIC", "2"))))
        self.default_size = max(0.0001, float(os.getenv("CATEGORY_DEFAULT_SIZE", "0.5")))
        self.risk_per_trade_pct = max(
            0.10,
            min(
                1.00,
                float(
                    os.getenv(
                        "CATEGORY_RISK_PER_TRADE_PCT",
                        os.getenv("XAU_RISK_PER_TRADE_PCT", "1.0"),
                    )
                ),
            ),
        )
        self.max_daily_entries = max(
            1,
            min(2, int(os.getenv("XAU_MAX_DAILY_ENTRIES", "2"))),
        )
        self.max_daily_fx_entries = max(
            1,
            min(12, int(os.getenv("FOREX_MAX_DAILY_ENTRIES", "4"))),
        )
        self.max_daily_fx_entries_per_pair = max(
            1,
            min(3, int(os.getenv("FOREX_MAX_DAILY_ENTRIES_PER_PAIR", "1"))),
        )
        self._sast = ZoneInfo("Africa/Johannesburg")
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state = self._load_state()

    def _default_state(self) -> Dict[str, Any]:
        return {
            "version": self.VERSION,
            "enabled": self.enabled,
            "positions": [],
            "journal": [],
            "last_tick_at": None,
            "last_error": None,
            "opens": 0,
            "closes": 0,
        }

    def _load_state(self) -> Dict[str, Any]:
        state = self._default_state()
        try:
            if os.path.exists(self.state_path):
                with open(self.state_path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                if isinstance(raw, dict):
                    state.update(raw)
        except Exception:
            pass
        state["version"] = self.VERSION
        state["enabled"] = self.enabled
        return state

    def _persist(self) -> None:
        try:
            path = Path(self.state_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = str(path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._state, fh, separators=(",", ":"), default=str)
            os.replace(tmp, self.state_path)
        except Exception as exc:
            self._state["last_error"] = f"persist: {type(exc).__name__}: {exc}"

    @staticmethod
    def _broker_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        rows = []
        for item in payload.get("positions", []) or []:
            if not isinstance(item, dict):
                continue
            position = item.get("position") or {}
            market = item.get("market") or {}
            if not isinstance(position, dict) or not isinstance(market, dict):
                continue
            rows.append({
                "deal_id": position.get("dealId"),
                "deal_reference": position.get("dealReference"),
                "direction": str(position.get("direction") or "").upper(),
                "size": position.get("size") if position.get("size") is not None else position.get("dealSize"),
                "level": position.get("level"),
                "epic": market.get("epic") or position.get("epic"),
                "market_name": market.get("instrumentName") or market.get("marketName"),
                "market_status": market.get("marketStatus"),
                "bid": market.get("bid"),
                "offer": market.get("offer"),
            })
        return rows

    def _external_positions(self) -> List[Dict[str, Any]]:
        if not self.external_positions_source:
            return []
        try:
            return [dict(row) for row in (self.external_positions_source() or []) if isinstance(row, dict)]
        except Exception:
            return []

    def _journal(self, event: str, **data: Any) -> None:
        self._state.setdefault("journal", []).append({"at": time.time(), "event": event, **data})
        self._state["journal"] = self._state["journal"][-500:]

    def _reconcile(self) -> List[Dict[str, Any]]:
        broker_rows = self._broker_rows(self.broker.positions())
        by_deal = {str(row.get("deal_id") or ""): row for row in broker_rows}
        for item in self._state.setdefault("positions", []):
            if item.get("status") != "OPEN":
                continue
            deal_id = str(item.get("deal_id") or "")
            broker_row = by_deal.get(deal_id)
            if broker_row:
                item["broker"] = broker_row
                item["last_seen_at"] = time.time()
                item["dual_track"] = self._is_dual_track(item.get("epic"), self._external_positions())
            else:
                item["status"] = "CLOSED_RECONCILED"
                item["closed_at"] = time.time()
                self._state["closes"] = int(self._state.get("closes") or 0) + 1
                self._journal("CLOSE_RECONCILED", deal_id=deal_id, symbol=item.get("symbol"), category=item.get("category"))
        return broker_rows

    @staticmethod
    def _is_dual_track(epic: Any, external: List[Dict[str, Any]]) -> bool:
        clean = str(epic or "").upper().strip()
        return bool(clean) and any(
            str(row.get("epic") or row.get("ig_epic") or "").upper().strip() == clean
            for row in external
        )

    def _due_closes(self) -> None:
        now = time.time()
        for item in self._state.setdefault("positions", []):
            if item.get("status") != "OPEN" or now < float(item.get("due_at") or 0.0):
                continue
            deal_id = str(item.get("deal_id") or "")
            if not deal_id:
                continue
            try:
                result = self.broker.close_position(deal_id) or {}
                status = str(result.get("status") or result.get("dealStatus") or "").upper()
                if status == "CLOSE_DEFERRED_MARKET_CLOSED":
                    item["close_state"] = status
                    item["last_close_check_at"] = now
                    continue
                if result.get("closeVerified") or status in {"ALREADY_CLOSED_OR_NOT_FOUND", "ACCEPTED", "CLOSED_VERIFIED"}:
                    item["status"] = "CLOSED"
                    item["closed_at"] = now
                    item["close_result"] = result
                    self._state["closes"] = int(self._state.get("closes") or 0) + 1
                    self._journal("CLOSE", deal_id=deal_id, symbol=item.get("symbol"), category=item.get("category"), result=status)
                else:
                    item["close_state"] = status or "CLOSE_PENDING"
            except Exception as exc:
                item["close_error"] = f"{type(exc).__name__}: {exc}"
                item["last_close_check_at"] = now

    def _open_positions(self) -> List[Dict[str, Any]]:
        return [row for row in self._state.setdefault("positions", []) if row.get("status") == "OPEN"]

    def _theme_counts(self, external: List[Dict[str, Any]]) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for row in self._open_positions() + list(external):
            for tag in row.get("exposure_tags") or []:
                counts[str(tag)] = counts.get(str(tag), 0) + 1
        return counts

    def _epic_track_count(self, epic: str, external: List[Dict[str, Any]]) -> int:
        clean = str(epic or "").upper().strip()
        return sum(1 for row in self._open_positions() if str(row.get("epic") or "").upper().strip() == clean) + sum(
            1 for row in external if str(row.get("epic") or row.get("ig_epic") or "").upper().strip() == clean
        )

    def _broker_has_open_epic(self, epic: str) -> bool:
        clean = str(epic or "").upper().strip()
        if not clean:
            return False
        try:
            rows = self._broker_rows(self.broker.positions() or {})
        except Exception:
            # Fail closed when account-wide exposure cannot be verified.
            return True
        return any(
            str(row.get("epic") or "").upper().strip() == clean
            for row in rows
        )

    def _is_active_strategy(self, candidate: Dict[str, Any]) -> bool:
        strategy = str(candidate.get("strategy_id") or "").upper().strip()
        symbol = str(candidate.get("symbol") or candidate.get("key") or "").upper().strip()
        category = str(candidate.get("category") or "").upper().strip()
        return bool(
            (
                strategy == XAU_STRATEGY_ID
                and symbol == "GOLD"
                and category == "METALS"
            )
            or (
                strategy == FX_STRATEGY_ID
                and symbol in LIQUID_FOREX_PAIRS
                and category == "FOREX"
            )
        )

    def _opened_today_sast(self, category: Optional[str] = None) -> int:
        today = datetime.now(tz=self._sast).date()
        wanted_category = str(category or "").upper().strip()
        count = 0
        for row in self._state.setdefault("positions", []):
            strategy = str(row.get("strategy_id") or "").upper()
            if strategy not in self.ACTIVE_STRATEGY_IDS:
                continue
            if wanted_category and str(row.get("category") or "").upper() != wanted_category:
                continue
            opened_at = float(row.get("opened_at") or 0.0)
            if opened_at <= 0:
                continue
            if datetime.fromtimestamp(opened_at, tz=self._sast).date() == today:
                count += 1
        return count

    def _opened_symbol_today_sast(self, symbol: str) -> int:
        today = datetime.now(tz=self._sast).date()
        wanted = str(symbol or "").upper().strip()
        return sum(
            1
            for row in self._state.setdefault("positions", [])
            if str(row.get("symbol") or "").upper().strip() == wanted
            and float(row.get("opened_at") or 0.0) > 0
            and datetime.fromtimestamp(
                float(row.get("opened_at")), tz=self._sast
            ).date() == today
        )

    def _account_for_risk(self) -> Dict[str, Any]:
        payload = self.broker.accounts() or {}
        rows = [
            row for row in payload.get("accounts", []) or []
            if isinstance(row, dict)
        ]
        if not rows:
            raise RiskSizingError("IG DEMO account balance is unavailable")

        preferred = str(
            (getattr(self.broker, "status", lambda: {})() or {}).get("account_id")
            or ""
        ).strip()
        row = next(
            (
                item for item in rows
                if str(item.get("accountId") or "").strip() == preferred
            ),
            rows[0],
        )
        balance_block = row.get("balance") or {}
        balance = float(balance_block.get("balance") or 0.0)
        currency = str(row.get("currency") or "").upper().strip()
        if balance <= 0 or not currency:
            raise RiskSizingError("IG DEMO account balance/currency is invalid")
        return {
            "account_id": row.get("accountId"),
            "balance": balance,
            "available": float(balance_block.get("available") or 0.0),
            "currency": currency,
        }

    @staticmethod
    def _default_settlement_currency(details: Dict[str, Any]) -> Optional[str]:
        instrument = details.get("instrument") or {}
        currencies = instrument.get("currencies") or []
        selected = next(
            (
                item for item in currencies
                if isinstance(item, dict) and item.get("isDefault") is True
            ),
            None,
        )
        if selected is None:
            selected = next(
                (item for item in currencies if isinstance(item, dict)),
                None,
            )
        code = str((selected or {}).get("code") or "").upper().strip()
        return code or None

    @staticmethod
    def _floor_to_step(value: float, step: float) -> float:
        if step <= 0:
            return float(value)
        return float(f"{math.floor((value + 1e-12) / step) * step:.10f}")

    def _risk_sized_order(
        self,
        candidate: Dict[str, Any],
    ) -> Dict[str, Any]:
        direction = str(candidate.get("direction") or "").upper().strip()
        if direction not in {"BUY", "SELL"}:
            raise RiskSizingError("Risk sizing requires BUY or SELL")

        account = self._account_for_risk()
        epic = str(candidate.get("ig_epic") or "").strip()
        try:
            details = self.broker.market_details(
                epic,
                require_quote=True,
            ) or {}
        except TypeError:
            details = self.broker.market_details(epic) or {}

        quote_snapshot = dict(details.get("_quote_snapshot") or {})
        snapshot = dict(details.get("snapshot") or {})
        bid = quote_snapshot.get("bid")
        offer = quote_snapshot.get("offer")
        if bid is None:
            bid = snapshot.get("bid")
        if offer is None:
            offer = snapshot.get("offer")
        if bid is None:
            bid = candidate.get("ig_bid")
        if offer is None:
            offer = candidate.get("ig_offer")
        raw_entry_quote = offer if direction == "BUY" else bid
        entry_quote = float(raw_entry_quote or 0.0)
        if entry_quote <= 0:
            raise RiskSizingError("Fresh IG entry-side quote is unavailable")

        risk_plan = build_risk_plan(
            candidate,
            entry_price=entry_quote,
            direction=direction,
        )
        settlement_currency = self._default_settlement_currency(details)

        valuation = self.broker.estimate_closed_position_pnl(
            epic=epic,
            direction=direction,
            entry_level=entry_quote,
            exit_level=risk_plan.protective_stop_price,
            size=1.0,
            settlement_currency=settlement_currency,
            account_currency=account["currency"],
        )
        risk_per_size = abs(float(valuation.get("account_pnl") or 0.0))
        if risk_per_size <= 0:
            raise RiskSizingError("IG valuation produced no usable risk per size")

        risk_cash = account["balance"] * self.risk_per_trade_pct / 100.0
        raw_size = risk_cash / risk_per_size
        min_size = float(
            getattr(self.broker, "_min_deal_size", lambda _: 0.0)(details)
            or candidate.get("ig_min_deal_size")
            or 0.0
        )
        increment = float(
            getattr(self.broker, "_deal_size_increment", lambda _: min_size)(details)
            or min_size
            or 0.0
        )
        if min_size <= 0 or increment <= 0:
            raise RiskSizingError(
                "IG minimum size or size increment is unavailable; refusing unsafe sizing"
            )
        size = self._floor_to_step(raw_size, increment)

        if size + 1e-12 < min_size or size <= 0:
            minimum_risk = risk_per_size * min_size
            raise RiskSizingError(
                "IG minimum size would exceed the configured per-trade risk budget "
                f"(budget {risk_cash:.2f} {account['currency']}; "
                f"minimum-size risk {minimum_risk:.2f} {account['currency']})"
            )

        estimated_risk = risk_per_size * size
        if estimated_risk > risk_cash * 1.001:
            raise RiskSizingError(
                "Rounded IG size exceeds the configured per-trade risk budget"
            )

        return {
            "size": size,
            "entry_quote": entry_quote,
            "risk_cash": round(risk_cash, 8),
            "estimated_stop_risk_cash": round(estimated_risk, 8),
            "risk_per_trade_pct": self.risk_per_trade_pct,
            "account_balance": round(account["balance"], 8),
            "account_currency": account["currency"],
            "minimum_size": min_size,
            "size_increment": increment,
            "risk_plan_at_quote": risk_plan.as_dict(),
            "valuation": valuation,
        }

    def _may_open(self, candidate: Dict[str, Any], external: List[Dict[str, Any]]) -> tuple[bool, str]:
        if not self._is_active_strategy(candidate):
            return False, "old autonomous entry strategy is retired; only FX liquidity-lines and XAUUSD liquidity-structure are active"
        if not candidate.get("standard_eligible"):
            return False, "not standard eligible"
        if candidate.get("session_active") is not True:
            return False, "outside the market's approved geographic execution session"
        setup_id = str(candidate.get("setup_id") or "").strip()
        if not setup_id:
            return False, "liquidity/structure setup has no deterministic setup ID"
        category = str(candidate.get("category") or "").upper()
        symbol = str(candidate.get("symbol") or "").upper()
        daily_cap = self.max_daily_fx_entries if category == "FOREX" else self.max_daily_entries
        if self._opened_today_sast(category) >= daily_cap:
            return False, f"South Africa daily {category} entry cap reached"
        if (
            category == "FOREX"
            and self._opened_symbol_today_sast(symbol) >= self.max_daily_fx_entries_per_pair
        ):
            return False, f"South Africa daily {symbol} entry cap reached"
        epic = str(candidate.get("ig_epic") or "").strip()
        if not epic:
            return False, "no IG EPIC"
        open_rows = self._open_positions()
        if len(open_rows) >= self.max_open_positions:
            return False, "category portfolio position cap reached"
        if len(open_rows) + len(external) >= self.global_ig_max_positions:
            return False, "global IG DEMO position cap reached"
        if sum(1 for row in open_rows if row.get("category") == category) >= self.max_per_category:
            return False, "category position cap reached"
        if any(
            str(row.get("symbol") or "").upper() == symbol
            for row in open_rows
        ):
            return False, f"a {symbol} position is already open"
        if any(
            str(row.get("setup_id") or "") == setup_id
            for row in self._state.setdefault("positions", [])
        ):
            return False, "this liquidity/structure setup was already traded"
        if self._broker_has_open_epic(epic):
            return False, f"an account-wide {symbol} position is already open"
        if self._epic_track_count(epic, external) >= self.max_tracks_per_epic:
            return False, "combined category/compound EPIC exposure cap reached"
        theme_counts = self._theme_counts(external)
        for tag in candidate.get("exposure_tags") or []:
            if theme_counts.get(str(tag), 0) >= self.max_theme_exposure:
                return False, f"theme exposure cap reached: {tag}"
        return True, "approved"

    def _open_candidate(self, candidate: Dict[str, Any], external: List[Dict[str, Any]]) -> None:
        allowed, reason = self._may_open(candidate, external)
        if not allowed:
            return

        category = str(candidate.get("category") or "UNK").upper()
        ref = f"JSCAT_{category[:3]}_{uuid.uuid4().hex[:16].upper()}"[:30]
        try:
            sizing = self._risk_sized_order(candidate)
        except RiskSizingError as exc:
            self._journal(
                "CATEGORY_RISK_SIZE_REJECTED",
                symbol=candidate.get("symbol"),
                setup_id=candidate.get("setup_id"),
                reason=str(exc),
            )
            self._state["last_risk_rejection"] = {
                "at": time.time(),
                "symbol": candidate.get("symbol"),
                "setup_id": candidate.get("setup_id"),
                "reason": str(exc),
            }
            return

        result = self.broker.open_epic_position(
            epic=str(candidate["ig_epic"]),
            direction=str(candidate["direction"]),
            size=float(sizing["size"]),
            deal_reference=ref,
            allow_size_increment_retry=False,
        ) or {}
        deal_id = result.get("dealId")
        if not deal_id:
            raise RuntimeError(f"IG DEMO did not return dealId: {result}")

        entry_level = result.get("level")
        if entry_level is None:
            try:
                self.broker.close_position(str(deal_id))
            finally:
                raise RuntimeError(
                    "IG DEMO opened a category position without an entry level; position was closed immediately"
                )

        actual_size = float(result.get("size") or 0.0)
        if actual_size <= 0 or actual_size > float(sizing["size"]) + 1e-12:
            try:
                self.broker.close_position(str(deal_id))
            finally:
                raise RuntimeError(
                    "IG DEMO changed the risk-sized category order upward; "
                    "the position was closed immediately"
                )

        try:
            risk_plan = build_risk_plan(
                candidate,
                entry_price=float(entry_level),
                direction=str(candidate["direction"]),
            )
        except Exception as exc:
            self._journal(
                "RISK_PLAN_ERROR",
                deal_id=deal_id,
                symbol=candidate.get("symbol"),
                error=f"{type(exc).__name__}: {exc}",
            )
            try:
                self.broker.close_position(str(deal_id))
            finally:
                raise RuntimeError(
                    "Structural risk plan failed after entry; "
                    "the IG DEMO position was closed immediately"
                ) from exc

        max_hold_seconds = max(
            900,
            int(
                candidate.get("max_hold_seconds")
                or int(candidate.get("holding_bars") or 48) * 15 * 60
            ),
        )
        due_at = time.time() + max_hold_seconds
        session_exit_at = float(candidate.get("session_exit_at") or 0.0)
        if session_exit_at > time.time():
            due_at = min(due_at, session_exit_at)
        position = {
            "track": "CATEGORY",
            "category": category,
            "category_rank": candidate.get("category_rank"),
            "strategy_id": candidate.get("strategy_id"),
            "strategy_name": candidate.get("strategy_name"),
            "symbol": candidate.get("symbol"),
            "market": candidate.get("market"),
            "direction": candidate.get("direction"),
            "epic": candidate.get("ig_epic"),
            "deal_id": deal_id,
            "deal_reference": result.get("dealReference") or ref,
            "size": result.get("size"),
            "entry_level": entry_level,
            "opened_at": time.time(),
            "due_at": due_at,
            "status": "OPEN",
            "exposure_tags": list(candidate.get("exposure_tags") or []),
            "quant_confidence": candidate.get("quant_confidence"),
            "model_ai_confidence": candidate.get("model_ai_confidence"),
            "historical_win_rate": candidate.get("historical_win_rate"),
            "historical_profit_factor": candidate.get("historical_profit_factor"),
            "smart_fast_score": candidate.get("smart_fast_score"),
            "dual_track": self._is_dual_track(candidate.get("ig_epic"), external),
            "setup_id": candidate.get("setup_id"),
            "session_name": candidate.get("session_name"),
            "session_snapshot": dict(candidate.get("session") or {}),
            "south_africa_time_at_signal": candidate.get("south_africa_time"),
            "london_new_york_overlap": bool(candidate.get("london_new_york_overlap")),
            "risk_per_trade_pct": sizing.get("risk_per_trade_pct"),
            "risk_cash_budget": sizing.get("risk_cash"),
            "estimated_stop_risk_cash": sizing.get("estimated_stop_risk_cash"),
            "risk_account_balance": sizing.get("account_balance"),
            "risk_account_currency": sizing.get("account_currency"),
            "risk_sized_order": sizing,
            "risk_policy_version": risk_plan.version if risk_plan else None,
            "planned_stop_pct": risk_plan.stop_pct if risk_plan else None,
            "planned_risk_price_distance": risk_plan.stop_distance if risk_plan else None,
            "planned_target_r": risk_plan.target_r if risk_plan else None,
            "protective_stop_price": risk_plan.protective_stop_price if risk_plan else None,
            "take_profit_target_price": risk_plan.take_profit_target_price if risk_plan else None,
            "risk_plan_source": risk_plan.source if risk_plan else None,
            "live_money_execution": False,
        }

        self._state.setdefault("positions", []).append(position)
        self._state["opens"] = int(self._state.get("opens") or 0) + 1
        self._journal(
            "OPEN",
            category=category,
            symbol=position["symbol"],
            deal_id=deal_id,
            dual_track=position["dual_track"],
            setup_id=position.get("setup_id"),
            session=position.get("session_name"),
            risk_pct=position.get("risk_per_trade_pct"),
            estimated_stop_risk_cash=position.get("estimated_stop_risk_cash"),
        )

        tracker = getattr(self, "_trade_excursion_tracker", None)
        register = getattr(tracker, "register_trade_plan", None)
        if callable(register) and risk_plan is not None:
            try:
                register(
                    deal_id,
                    {
                        **risk_plan.as_dict(),
                        "strategy_id": position.get("strategy_id"),
                        "symbol": position.get("symbol"),
                        "category": position.get("category"),
                        "deal_reference": position.get("deal_reference"),
                    },
                )
            except Exception as exc:
                self._journal(
                    "RISK_PLAN_REGISTER_ERROR",
                    deal_id=deal_id,
                    symbol=position.get("symbol"),
                    error=f"{type(exc).__name__}: {exc}",
                )

    @staticmethod
    def _symbol_key(value: Any) -> str:
        return "".join(ch for ch in str(value or "").upper() if ch.isalnum())

    def set_enabled(self, enabled: bool) -> Dict[str, Any]:
        with self._lock:
            self.enabled = bool(enabled)
            self._state["enabled"] = self.enabled
            self._journal("AUTOTRADE_SET", enabled=self.enabled, source="GPT_ACTION")
            self._persist()
            return {
                "version": self.VERSION,
                "enabled": self.enabled,
                "scope": "RUNTIME_ONLY",
                "restart_authority": "CATEGORY_AUTOTRADE environment variable",
                "live_money_execution": False,
            }

    def open_qualified_symbol(self, symbol: str) -> Dict[str, Any]:
        wanted = self._symbol_key(symbol)
        if not wanted:
            return {"version": self.VERSION, "opened": False, "error": "symbol is required", "live_money_execution": False}
        with self._lock:
            try:
                self._reconcile()
                self._due_closes()
                rankings = self.ranking_source() or {}
                candidate = None
                for category in ("FOREX", "INDICES", "CRYPTO", "METALS", "ENERGY", "SHARES"):
                    for raw in rankings.get(category, [])[:5]:
                        if not isinstance(raw, dict):
                            continue
                        variants = {
                            self._symbol_key(raw.get("key")),
                            self._symbol_key(raw.get("symbol")),
                            self._symbol_key(raw.get("market")),
                            self._symbol_key(raw.get("name")),
                        }
                        if wanted in variants:
                            candidate = dict(raw)
                            break
                    if candidate is not None:
                        break
                if candidate is None:
                    return {
                        "version": self.VERSION,
                        "opened": False,
                        "symbol": symbol,
                        "reason": "Market is not in the current top-five-per-category ranking surface.",
                        "live_money_execution": False,
                    }
                external = self._external_positions()
                allowed, reason = self._may_open(candidate, external)
                if not allowed:
                    return {
                        "version": self.VERSION,
                        "opened": False,
                        "symbol": candidate.get("symbol") or candidate.get("key"),
                        "market": candidate.get("market") or candidate.get("name"),
                        "category": candidate.get("category"),
                        "reason": reason,
                        "rejection_reasons": candidate.get("rejection_reasons") or [],
                        "standard_eligible": bool(candidate.get("standard_eligible")),
                        "live_money_execution": False,
                    }
                before = {str(row.get("deal_id") or "") for row in self._open_positions() if row.get("deal_id")}
                self._open_candidate(candidate, external)
                self._persist()
                opened = next(
                    (dict(row) for row in reversed(self._open_positions()) if str(row.get("deal_id") or "") not in before),
                    None,
                )
                return {
                    "version": self.VERSION,
                    "opened": opened is not None,
                    "position": opened,
                    "execution_basis": "CURRENT_STANDARD_ELIGIBILITY_PLUS_CATEGORY_RISK_GATES",
                    "live_money_execution": False,
                }
            except Exception as exc:
                self._state["last_error"] = f"GPT open: {type(exc).__name__}: {exc}"
                self._persist()
                return {"version": self.VERSION, "opened": False, "symbol": symbol, "error": f"{type(exc).__name__}: {exc}", "live_money_execution": False}

    def close_category_position(self, deal_id: str) -> Dict[str, Any]:
        wanted = str(deal_id or "").strip()
        if not wanted:
            return {"version": self.VERSION, "closed": False, "error": "deal_id is required", "live_money_execution": False}
        with self._lock:
            try:
                self._reconcile()
                tracked = next(
                    (
                        row for row in self._state.setdefault("positions", [])
                        if row.get("status") == "OPEN"
                        and str(row.get("deal_id") or "") == wanted
                        and str(row.get("deal_reference") or "").upper().startswith(self.DEAL_PREFIX)
                    ),
                    None,
                )
                if tracked is None:
                    return {
                        "version": self.VERSION,
                        "closed": False,
                        "deal_id": wanted,
                        "reason": "Only an open JSCAT-owned Category position can be closed by this action.",
                        "live_money_execution": False,
                    }
                result = self.broker.close_position(wanted) or {}
                status = str(result.get("status") or result.get("dealStatus") or "").upper()
                if result.get("closeVerified") or status in {"ALREADY_CLOSED_OR_NOT_FOUND", "ACCEPTED", "CLOSED_VERIFIED"}:
                    tracked["status"] = "CLOSED"
                    tracked["closed_at"] = time.time()
                    tracked["close_result"] = result
                    self._state["closes"] = int(self._state.get("closes") or 0) + 1
                self._journal("GPT_CLOSE_REQUEST", deal_id=wanted, result=status)
                self._persist()
                return {
                    "version": self.VERSION,
                    "closed": bool(result.get("closeVerified")) or status in {"ALREADY_CLOSED_OR_NOT_FOUND", "ACCEPTED", "CLOSED_VERIFIED"},
                    "result": result,
                    "live_money_execution": False,
                }
            except Exception as exc:
                self._state["last_error"] = f"GPT close: {type(exc).__name__}: {exc}"
                self._persist()
                return {"version": self.VERSION, "closed": False, "deal_id": wanted, "error": f"{type(exc).__name__}: {exc}", "live_money_execution": False}

    def tick(self) -> Dict[str, Any]:
        with self._lock:
            try:
                self._reconcile()
                self._due_closes()
                if self.enabled and getattr(self.broker, "configured", lambda: False)():
                    external = self._external_positions()
                    rankings = self.ranking_source() or {}
                    for category in ("FOREX", "INDICES", "CRYPTO", "METALS", "ENERGY", "SHARES"):
                        for candidate in rankings.get(category, [])[:5]:
                            self._open_candidate(dict(candidate), external)
                self._state["last_error"] = None
            except Exception as exc:
                self._state["last_error"] = f"{type(exc).__name__}: {exc}"
            self._state["last_tick_at"] = time.time()
            self._persist()
            return self.status()

    def positions(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            rows = [dict(row) for row in self._state.setdefault("positions", [])]
        rows.sort(key=lambda row: float(row.get("opened_at") or 0.0), reverse=True)
        return rows[:max(1, min(int(limit), 1000))]

    def status(self) -> Dict[str, Any]:
        open_rows = self._open_positions()
        external = self._external_positions()
        by_category: Dict[str, int] = {}
        dual = 0
        for row in open_rows:
            cat = str(row.get("category") or "UNKNOWN")
            by_category[cat] = by_category.get(cat, 0) + 1
            if self._is_dual_track(row.get("epic"), external):
                dual += 1
        return {
            "version": self.VERSION,
            "name": "JASONG CATEGORY PORTFOLIO",
            "enabled": self.enabled,
            "execution_mode": "IG_DEMO_ONLY",
            "deal_prefix": self.DEAL_PREFIX,
            "active_strategy": f"{FX_STRATEGY_ID} + {XAU_STRATEGY_ID}",
            "active_strategies": [
                FX_STRATEGY_ID,
                XAU_STRATEGY_ID,
            ],
            "active_symbol": "28_LIQUID_FX_PAIRS + GOLD",
            "active_symbols": list(self.ACTIVE_SYMBOLS),
            "active_categories": ["FOREX", "METALS"],
            "old_entry_strategies_retired": True,
            "risk_policy": "STRUCTURAL_STOP_PLUS_ACCOUNT_PERCENT_RISK",
            "risk_per_trade_pct": self.risk_per_trade_pct,
            "max_daily_entries_south_africa": {
                "METALS": self.max_daily_entries,
                "FOREX": self.max_daily_fx_entries,
                "FOREX_PER_PAIR": self.max_daily_fx_entries_per_pair,
            },
            "entries_today_south_africa": {
                "METALS": self._opened_today_sast("METALS"),
                "FOREX": self._opened_today_sast("FOREX"),
            },
            "session_policy": (
                "FX pair geography uses London/New York/Tokyo/Sydney; Gold uses London/New York; DST-aware"
            ),
            "minimum_target_r": 2.0,
            "open_positions": len(open_rows),
            "open_by_category": by_category,
            "dual_track_positions": dual,
            "max_open_positions": self.max_open_positions,
            "global_ig_max_positions": self.global_ig_max_positions,
            "external_open_positions": len(external),
            "combined_open_positions": len(open_rows) + len(external),
            "global_remaining_positions": max(0, self.global_ig_max_positions - len(open_rows) - len(external)),
            "max_per_category": self.max_per_category,
            "max_tracks_per_epic": self.max_tracks_per_epic,
            "opens": int(self._state.get("opens") or 0),
            "closes": int(self._state.get("closes") or 0),
            "last_tick_at": self._state.get("last_tick_at"),
            "last_error": self._state.get("last_error"),
            "last_risk_rejection": self._state.get("last_risk_rejection"),
            "state_path": self.state_path,
            "live_money_execution": False,
        }

    def start_thread(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, daemon=True, name="jasong-category-execution")
            self._thread.start()

    def _loop(self) -> None:
        if self._stop.wait(15.0):
            return
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.poll_seconds)

    def stop_thread(self) -> None:
        self._stop.set()
