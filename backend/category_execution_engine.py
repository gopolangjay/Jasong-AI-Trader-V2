from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from risk_exit_policy import build_risk_plan


class CategoryExecutionEngine:
    """Independent IG DEMO standard-category portfolio executor."""

    VERSION = "6.3-clean-core-risk-exit-v1"
    DEAL_PREFIX = "JSCAT_"

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

    def _may_open(self, candidate: Dict[str, Any], external: List[Dict[str, Any]]) -> tuple[bool, str]:
        if not candidate.get("standard_eligible"):
            return False, "not standard eligible"
        epic = str(candidate.get("ig_epic") or "").strip()
        if not epic:
            return False, "no IG EPIC"
        direction = str(candidate.get("direction") or "").upper()
        category = str(candidate.get("category") or "").upper()
        symbol = str(candidate.get("symbol") or "").upper()
        open_rows = self._open_positions()
        if len(open_rows) >= self.max_open_positions:
            return False, "category portfolio position cap reached"
        if len(open_rows) + len(external) >= self.global_ig_max_positions:
            return False, "global IG DEMO position cap reached"
        if sum(1 for row in open_rows if row.get("category") == category) >= self.max_per_category:
            return False, "category position cap reached"
        if any(
            row.get("category") == category
            and str(row.get("symbol") or "").upper() == symbol
            and row.get("direction") == direction
            for row in open_rows
        ):
            return False, "duplicate category signal already open"
        if self._epic_track_count(epic, external) >= self.max_tracks_per_epic:
            return False, "combined category/compound EPIC exposure cap reached"
        theme_counts = self._theme_counts(external)
        for tag in candidate.get("exposure_tags") or []:
            if theme_counts.get(str(tag), 0) >= self.max_theme_exposure:
                return False, f"theme exposure cap reached: {tag}"
        return True, "approved"

    def _open_candidate(self, candidate: Dict[str, Any], external: List[Dict[str, Any]]) -> None:
        allowed, _ = self._may_open(candidate, external)
        if not allowed:
            return

        category = str(candidate.get("category") or "UNK").upper()
        ref = f"JSCAT_{category[:3]}_{uuid.uuid4().hex[:16].upper()}"[:30]
        result = self.broker.open_epic_position(
            epic=str(candidate["ig_epic"]),
            direction=str(candidate["direction"]),
            size=max(self.default_size, float(candidate.get("ig_min_deal_size") or 0.0)),
            deal_reference=ref,
        ) or {}
        deal_id = result.get("dealId")
        if not deal_id:
            raise RuntimeError(f"IG DEMO did not return dealId: {result}")

        entry_level = result.get("level")
        risk_plan = None
        try:
            if entry_level is not None:
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

        hold_seconds = max(900, int(candidate.get("holding_bars") or 4) * 15 * 60)
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
            "due_at": time.time() + hold_seconds,
            "status": "OPEN",
            "exposure_tags": list(candidate.get("exposure_tags") or []),
            "quant_confidence": candidate.get("quant_confidence"),
            "model_ai_confidence": candidate.get("model_ai_confidence"),
            "historical_win_rate": candidate.get("historical_win_rate"),
            "historical_profit_factor": candidate.get("historical_profit_factor"),
            "smart_fast_score": candidate.get("smart_fast_score"),
            "dual_track": self._is_dual_track(candidate.get("ig_epic"), external),
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
        self._journal("OPEN", category=category, symbol=position["symbol"], deal_id=deal_id, dual_track=position["dual_track"])

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
            "risk_policy": "VOLATILITY_PLUS_SPREAD_R_BASED",
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
