from __future__ import annotations

import copy
import json
import math
import os
import threading
import time
from pathlib import Path
from types import MethodType
from typing import Any, Dict, List, Optional


class TradeExcursionTracker:
    """Persist broker-observed MFE/MAE and enforce a Jasong-owned profit target.

    Price extremes are based on the executable IG quote seen by periodic REST
    reconciliation. The MFE/MAE recorder itself never changes entries, stops,
    sizes, qualification gates or strategy selection. A separate close-only
    rule inside this tracker can close Jasong-owned IG DEMO positions when the
    configured favourable price move reaches the requested take-profit target.

    Price basis:
      * BUY uses bid as the observable exit-side price.
      * SELL uses offer as the observable exit-side price.

    Formulas intentionally follow the requested sign convention:
      BUY  MFE = highest - entry; MAE = lowest - entry
      SELL MFE = entry - lowest;  MAE = entry - highest

    Therefore MFE is normally >= 0 and MAE is normally <= 0.
    """

    VERSION = "6.9.4-sync-excursions-tp30"
    OWNED_PREFIXES = (
        "JSCAT_",
        "JSCMP_",
        "JASONG_",
        "JSBND_",
        "JSLRN_",
        "JSELT_",
    )

    def __init__(
        self,
        *,
        broker: Any,
        state_path: Optional[str] = None,
        poll_seconds: Optional[int] = None,
    ) -> None:
        self.broker = broker
        base_dir = "/var/data" if Path("/var/data").exists() else "/tmp"
        self.state_path = Path(
            state_path
            or os.getenv(
                "TRADE_EXCURSION_STATE_PATH",
                f"{base_dir}/jasong_trade_excursions.json",
            )
        )
        self.poll_seconds = max(
            10,
            min(
                120,
                int(
                    poll_seconds
                    or os.getenv("TRADE_EXCURSION_POLL_SECONDS", "60")
                ),
            ),
        )
        self.close_confirm_misses = max(
            1,
            min(
                5,
                int(os.getenv("TRADE_EXCURSION_CLOSE_CONFIRM_MISSES", "2")),
            ),
        )
        self.take_profit_enabled = str(
            os.getenv("TRADE_TAKE_PROFIT_ENABLED", "true")
        ).strip().lower() in {"1", "true", "yes", "on"}
        try:
            raw_tp = float(os.getenv("TRADE_TAKE_PROFIT_PCT", "30"))
        except Exception:
            raw_tp = 30.0
        self.take_profit_pct = max(0.01, min(500.0, raw_tp))
        try:
            raw_retry = int(os.getenv("TRADE_TAKE_PROFIT_RETRY_SECONDS", "60"))
        except Exception:
            raw_retry = 60
        self.take_profit_retry_seconds = max(10, min(900, raw_retry))
        try:
            raw_native_retry = int(os.getenv("TRADE_TAKE_PROFIT_NATIVE_RETRY_SECONDS", "300"))
        except Exception:
            raw_native_retry = 300
        self.take_profit_native_retry_seconds = max(30, min(3600, raw_native_retry))
        self._lock = threading.RLock()
        self._sync_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._broker_positions_original = None
        self._state: Dict[str, Any] = {
            "version": self.VERSION,
            "trades": {},
            "last_sync_at": None,
            "last_error": None,
            "sync_count": 0,
        }
        self._load()

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            out = float(value)
            return out if math.isfinite(out) else None
        except Exception:
            return None

    @staticmethod
    def _round(value: Optional[float], digits: int = 10) -> Optional[float]:
        if value is None or not math.isfinite(value):
            return None
        return round(value, digits)

    def _load(self) -> None:
        try:
            if self.state_path.exists():
                raw = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._state.update(raw)
        except Exception as exc:
            self._state["last_error"] = f"load: {type(exc).__name__}: {exc}"
        self._state["version"] = self.VERSION
        self._state.setdefault("trades", {})

    def _persist(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._state, indent=2, sort_keys=True, default=str),
                encoding="utf-8",
            )
            tmp.replace(self.state_path)
        except Exception as exc:
            self._state["last_error"] = f"persist: {type(exc).__name__}: {exc}"

    @staticmethod
    def _broker_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for item in payload.get("positions", []) or []:
            if not isinstance(item, dict):
                continue
            position = item.get("position") or {}
            market = item.get("market") or {}
            if not isinstance(position, dict) or not isinstance(market, dict):
                continue
            deal_id = str(position.get("dealId") or "").strip()
            if not deal_id:
                continue
            direction = str(position.get("direction") or "").upper().strip()
            entry = TradeExcursionTracker._safe_float(position.get("level"))
            bid = TradeExcursionTracker._safe_float(market.get("bid"))
            offer = TradeExcursionTracker._safe_float(market.get("offer"))
            if direction == "BUY":
                observed = bid
                basis = "IG_DEMO_BID_EXIT_SIDE"
            elif direction == "SELL":
                observed = offer
                basis = "IG_DEMO_OFFER_EXIT_SIDE"
            else:
                observed = None
                basis = "UNAVAILABLE"
            if observed is None and bid is not None and offer is not None:
                observed = (bid + offer) / 2.0
                basis = "IG_DEMO_MID_FALLBACK"
            elif observed is None:
                observed = bid if bid is not None else offer
                if observed is not None:
                    basis = "IG_DEMO_SINGLE_SIDE_FALLBACK"
            rows.append(
                {
                    "deal_id": deal_id,
                    "deal_reference": position.get("dealReference"),
                    "direction": direction,
                    "entry_price": entry,
                    "size": position.get("size")
                    if position.get("size") is not None
                    else position.get("dealSize"),
                    "epic": market.get("epic") or position.get("epic"),
                    "market": market.get("instrumentName")
                    or market.get("marketName")
                    or market.get("epic"),
                    "bid": bid,
                    "offer": offer,
                    "observed_price": observed,
                    "price_basis": basis,
                    "market_status": market.get("marketStatus"),
                    "opened_at_broker": position.get("createdDateUTC")
                    or position.get("createdDate"),
                }
            )
        return rows

    @staticmethod
    def _calculate(record: Dict[str, Any]) -> None:
        entry = TradeExcursionTracker._safe_float(record.get("entry_price"))
        high = TradeExcursionTracker._safe_float(
            record.get("highest_price_since_entry")
        )
        low = TradeExcursionTracker._safe_float(
            record.get("lowest_price_since_entry")
        )
        direction = str(record.get("direction") or "").upper().strip()
        if entry is None or entry <= 0 or high is None or low is None:
            return

        if direction == "BUY":
            mfe = high - entry
            mae = low - entry
        elif direction == "SELL":
            mfe = entry - low
            mae = entry - high
        else:
            return

        record["mfe"] = TradeExcursionTracker._round(mfe)
        record["mae"] = TradeExcursionTracker._round(mae)
        record["mfe_pct"] = TradeExcursionTracker._round((mfe / entry) * 100.0, 6)
        record["mae_pct"] = TradeExcursionTracker._round((mae / entry) * 100.0, 6)
        record["mae_abs"] = TradeExcursionTracker._round(abs(mae))
        record["mae_abs_pct"] = TradeExcursionTracker._round(
            (abs(mae) / entry) * 100.0,
            6,
        )

        # User-requested entry-vs-high percentage. Keep both common meanings:
        # change from entry, and the high expressed as a percentage of entry.
        record["highest_price_vs_entry_pct"] = TradeExcursionTracker._round(
            ((high - entry) / entry) * 100.0,
            6,
        )
        record["highest_price_as_pct_of_entry"] = TradeExcursionTracker._round(
            (high / entry) * 100.0,
            6,
        )
        record["lowest_price_vs_entry_pct"] = TradeExcursionTracker._round(
            ((low - entry) / entry) * 100.0,
            6,
        )

    @staticmethod
    def _current_favourable_pct(record: Dict[str, Any]) -> Optional[float]:
        entry = TradeExcursionTracker._safe_float(record.get("entry_price"))
        current = TradeExcursionTracker._safe_float(record.get("current_price"))
        direction = str(record.get("direction") or "").upper().strip()
        if entry is None or entry <= 0 or current is None:
            return None
        if direction == "BUY":
            return ((current - entry) / entry) * 100.0
        if direction == "SELL":
            return ((entry - current) / entry) * 100.0
        return None

    def _update_take_profit_fields(self, record: Dict[str, Any], now: float) -> bool:
        """Update TP telemetry and return True when a close should be attempted."""
        entry = self._safe_float(record.get("entry_price"))
        direction = str(record.get("direction") or "").upper().strip()
        current = self._safe_float(record.get("current_price"))
        target_pct = float(self.take_profit_pct)
        record["take_profit_enabled"] = bool(self.take_profit_enabled)
        record["take_profit_target_pct"] = self._round(target_pct, 6)
        record["take_profit_basis"] = "ENTRY_PRICE_FAVOURABLE_MOVE_PCT"

        if entry is not None and entry > 0:
            if direction == "BUY":
                target_price = entry * (1.0 + target_pct / 100.0)
            elif direction == "SELL":
                target_price = entry * (1.0 - target_pct / 100.0)
            else:
                target_price = None
            record["take_profit_target_price"] = self._round(target_price)

        favourable = self._current_favourable_pct(record)
        record["current_favourable_pct"] = self._round(favourable, 6)

        mfe_pct = self._safe_float(record.get("mfe_pct"))
        reached_ever = bool(mfe_pct is not None and mfe_pct >= target_pct)
        record["take_profit_reached"] = reached_ever
        if reached_ever and record.get("take_profit_reached_at") is None:
            record["take_profit_reached_at"] = now
            record["take_profit_first_reached_price"] = self._round(current)

        if not self.take_profit_enabled:
            return False
        if not bool(record.get("jasong_owned")):
            return False
        if str(record.get("status") or "").upper() != "OPEN":
            return False
        if favourable is None or favourable < target_pct:
            return False

        state = str(record.get("take_profit_close_state") or "").upper()
        last_attempt = self._safe_float(record.get("take_profit_last_attempt_at")) or 0.0
        if state in {"CLOSED", "CLOSE_VERIFIED"}:
            return False
        if state in {"TRIGGERED", "CLOSE_SENT", "CLOSE_PENDING"} and (now - last_attempt) < self.take_profit_retry_seconds:
            return False
        if state in {"ERROR", "DEFERRED_MARKET_CLOSED"} and (now - last_attempt) < self.take_profit_retry_seconds:
            return False

        record["take_profit_close_state"] = "TRIGGERED"
        record["take_profit_triggered_at"] = record.get("take_profit_triggered_at") or now
        record["take_profit_trigger_price"] = self._round(current)
        record["take_profit_trigger_favourable_pct"] = self._round(favourable, 6)
        return True

    def _native_take_profit_needed(self, record: Dict[str, Any], now: float) -> bool:
        if not self.take_profit_enabled or not bool(record.get("jasong_owned")):
            return False
        if str(record.get("status") or "").upper() != "OPEN":
            return False
        target = self._safe_float(record.get("take_profit_target_price"))
        if target is None or target <= 0:
            return False
        favourable = self._safe_float(record.get("current_favourable_pct"))
        if favourable is not None and favourable >= self.take_profit_pct:
            # Already at/through the target: close immediately rather than trying
            # to install a limit behind the executable market.
            return False
        state = str(record.get("native_take_profit_state") or "").upper()
        if state in {"CONFIRMED", "ATTACHED"}:
            attached = self._safe_float(record.get("native_take_profit_level"))
            if attached is not None and abs(attached - target) <= max(1e-8, abs(target) * 1e-9):
                return False
        last_attempt = self._safe_float(record.get("native_take_profit_last_attempt_at")) or 0.0
        return (now - last_attempt) >= self.take_profit_native_retry_seconds

    def _attach_native_take_profit(self, deal_id: str) -> None:
        """Attach an IG DEMO native limitLevel so TP survives app/server gaps."""
        now = time.time()
        with self._lock:
            record = self._state.setdefault("trades", {}).get(str(deal_id))
            if not isinstance(record, dict):
                return
            target = self._safe_float(record.get("take_profit_target_price"))
            if target is None or target <= 0:
                return
            record["native_take_profit_last_attempt_at"] = now
            record["native_take_profit_attempts"] = int(record.get("native_take_profit_attempts") or 0) + 1
            record["native_take_profit_state"] = "ATTACHING"
            self._persist()

        try:
            request_fn = getattr(self.broker, "_request", None)
            if not callable(request_fn):
                raise RuntimeError("IG broker update-position request method unavailable")
            acknowledgement = request_fn(
                "PUT",
                f"/positions/otc/{deal_id}",
                version=2,
                payload={"limitLevel": float(target)},
            ) or {}
            ref = str(acknowledgement.get("dealReference") or "").strip()
            confirmation = {}
            confirm_fn = getattr(self.broker, "confirm", None)
            if ref and callable(confirm_fn):
                confirmation = confirm_fn(ref) or {}
            deal_status = str(confirmation.get("dealStatus") or "").upper().strip()
            rejected = deal_status == "REJECTED"
            with self._lock:
                record = self._state.setdefault("trades", {}).get(str(deal_id))
                if not isinstance(record, dict):
                    return
                record["native_take_profit_deal_reference"] = ref or None
                record["native_take_profit_level"] = self._round(target)
                record["native_take_profit_confirmation"] = {
                    "dealStatus": confirmation.get("dealStatus"),
                    "reason": confirmation.get("reason"),
                    "limitLevel": confirmation.get("limitLevel"),
                    "status": confirmation.get("status"),
                }
                if rejected:
                    record["native_take_profit_state"] = "REJECTED"
                    record["native_take_profit_error"] = str(confirmation.get("reason") or confirmation)
                else:
                    record["native_take_profit_state"] = "CONFIRMED" if confirmation else "ATTACHED"
                    record["native_take_profit_attached_at"] = time.time()
                self._persist()
        except Exception as exc:
            with self._lock:
                record = self._state.setdefault("trades", {}).get(str(deal_id))
                if isinstance(record, dict):
                    record["native_take_profit_state"] = "ERROR"
                    record["native_take_profit_error"] = f"{type(exc).__name__}: {exc}"
                    record["native_take_profit_last_attempt_at"] = time.time()
                    self._persist()

    def _execute_take_profit_close(self, deal_id: str) -> None:
        now = time.time()
        with self._lock:
            record = self._state.setdefault("trades", {}).get(str(deal_id))
            if not isinstance(record, dict):
                return
            record["take_profit_last_attempt_at"] = now
            record["take_profit_close_attempts"] = int(record.get("take_profit_close_attempts") or 0) + 1
            record["take_profit_close_state"] = "CLOSE_PENDING"
            self._persist()

        try:
            result = self.broker.close_position(str(deal_id)) or {}
            status = str(result.get("status") or result.get("dealStatus") or "").upper().strip()
            verified = bool(result.get("closeVerified"))
            success = verified or status in {"ACCEPTED", "ALREADY_CLOSED_OR_NOT_FOUND"}
            deferred = status == "CLOSE_DEFERRED_MARKET_CLOSED"
            compact_result = {
                "status": status or None,
                "dealStatus": result.get("dealStatus"),
                "reason": result.get("reason"),
                "closeVerified": verified,
                "level": result.get("level"),
                "profit": result.get("profit"),
                "profitCurrency": result.get("profitCurrency"),
            }
            with self._lock:
                record = self._state.setdefault("trades", {}).get(str(deal_id))
                if not isinstance(record, dict):
                    return
                record["take_profit_close_result"] = compact_result
                if success:
                    record["take_profit_close_state"] = "CLOSE_VERIFIED" if verified else "CLOSE_SENT"
                    record["take_profit_closed_at"] = time.time()
                    record["close_reason"] = f"TAKE_PROFIT_{self.take_profit_pct:g}_PCT"
                elif deferred:
                    record["take_profit_close_state"] = "DEFERRED_MARKET_CLOSED"
                else:
                    record["take_profit_close_state"] = status or "CLOSE_PENDING"
                self._persist()
        except Exception as exc:
            with self._lock:
                record = self._state.setdefault("trades", {}).get(str(deal_id))
                if isinstance(record, dict):
                    record["take_profit_close_state"] = "ERROR"
                    record["take_profit_close_error"] = f"{type(exc).__name__}: {exc}"
                    record["take_profit_last_attempt_at"] = time.time()
                    self._persist()

    def observe_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Update excursion state from an already-fetched IG positions payload.

        This is the preferred path because it piggybacks on broker reconciliation
        already performed by Category / Compound / Learning engines and therefore
        does not consume another IG REST request.
        """
        if not self._sync_lock.acquire(blocking=False):
            return self.status()
        try:
            now = time.time()
            broker_rows = self._broker_rows(payload or {})
            seen = {row["deal_id"] for row in broker_rows}
            take_profit_candidates: List[str] = []
            native_take_profit_candidates: List[str] = []

            with self._lock:
                trades = self._state.setdefault("trades", {})
                for row in broker_rows:
                    deal_id = row["deal_id"]
                    record = trades.get(deal_id)
                    if not isinstance(record, dict):
                        record = {
                            "deal_id": deal_id,
                            "first_observed_at": now,
                            "highest_price_since_entry": row.get("entry_price"),
                            "lowest_price_since_entry": row.get("entry_price"),
                            "status": "OPEN",
                            "miss_count": 0,
                        }
                        trades[deal_id] = record

                    record.update(
                        {
                            "deal_reference": row.get("deal_reference"),
                            "direction": row.get("direction"),
                            "entry_price": row.get("entry_price")
                            if row.get("entry_price") is not None
                            else record.get("entry_price"),
                            "size": row.get("size"),
                            "epic": row.get("epic"),
                            "market": row.get("market"),
                            "current_bid": row.get("bid"),
                            "current_offer": row.get("offer"),
                            "current_price": row.get("observed_price"),
                            "price_basis": row.get("price_basis"),
                            "market_status": row.get("market_status"),
                            "opened_at_broker": row.get("opened_at_broker"),
                            "last_observed_at": now,
                            "status": "OPEN",
                            "miss_count": 0,
                            "jasong_owned": str(row.get("deal_reference") or "")
                            .upper()
                            .startswith(self.OWNED_PREFIXES),
                        }
                    )

                    current = self._safe_float(row.get("observed_price"))
                    entry = self._safe_float(record.get("entry_price"))
                    if current is not None:
                        high = self._safe_float(
                            record.get("highest_price_since_entry")
                        )
                        low = self._safe_float(
                            record.get("lowest_price_since_entry")
                        )
                        if high is None:
                            high = entry if entry is not None else current
                        if low is None:
                            low = entry if entry is not None else current
                        record["highest_price_since_entry"] = max(high, current)
                        record["lowest_price_since_entry"] = min(low, current)
                    self._calculate(record)
                    if self._update_take_profit_fields(record, now):
                        take_profit_candidates.append(deal_id)
                    elif self._native_take_profit_needed(record, now):
                        native_take_profit_candidates.append(deal_id)

                for deal_id, record in list(trades.items()):
                    if not isinstance(record, dict):
                        continue
                    if str(record.get("status") or "").upper() != "OPEN":
                        continue
                    if deal_id in seen:
                        continue
                    misses = int(record.get("miss_count") or 0) + 1
                    record["miss_count"] = misses
                    record["last_missing_at"] = now
                    if misses >= self.close_confirm_misses:
                        record["status"] = "CLOSED_OBSERVED"
                        record["closed_observed_at"] = now
                        self._calculate(record)

                # Bound state size while retaining closed excursion evidence.
                if len(trades) > 2500:
                    ordered = sorted(
                        trades.values(),
                        key=lambda r: float(
                            (r or {}).get("last_observed_at")
                            or (r or {}).get("closed_observed_at")
                            or 0.0
                        ),
                        reverse=True,
                    )[:2000]
                    self._state["trades"] = {
                        str(row.get("deal_id")): row
                        for row in ordered
                        if isinstance(row, dict) and row.get("deal_id")
                    }

                self._state["last_sync_at"] = now
                self._state["last_error"] = None
                self._state["sync_count"] = int(
                    self._state.get("sync_count") or 0
                ) + 1
                self._persist()

            # Execute closes outside the state lock. The enclosing sync lock stays
            # held so a close-triggered broker reconciliation cannot recursively
            # start another TP pass.
            for deal_id in native_take_profit_candidates:
                self._attach_native_take_profit(deal_id)
            for deal_id in take_profit_candidates:
                self._execute_take_profit_close(deal_id)
            return self.status()
        except Exception as exc:
            with self._lock:
                self._state["last_error"] = f"{type(exc).__name__}: {exc}"
                self._state["last_sync_attempt_at"] = time.time()
                self._persist()
            return self.status()
        finally:
            self._sync_lock.release()

    def install_broker_observer(self) -> None:
        """Observe every existing broker.positions() reconciliation in place.

        The wrapped method returns the exact original payload and never adds a
        broker call. This improves excursion sampling while reducing extra REST
        traffic compared with an independent high-frequency polling loop.
        """
        if getattr(self.broker, "_trade_excursion_observer_installed", False):
            return
        original = getattr(self.broker, "positions", None)
        if not callable(original):
            return
        self._broker_positions_original = original

        def observed_positions(broker_self: Any, *args: Any, **kwargs: Any):
            payload = original(*args, **kwargs)
            if isinstance(payload, dict):
                try:
                    self.observe_payload(payload)
                except Exception:
                    pass
            return payload

        self.broker.positions = MethodType(observed_positions, self.broker)
        self.broker._trade_excursion_observer_installed = True
        self.broker._trade_excursion_tracker = self

    def sync_once(self) -> Dict[str, Any]:
        """Safety refresh when no other engine has reconciled broker positions."""
        try:
            getter = self._broker_positions_original
            if callable(getter):
                payload = getter() or {}
            else:
                payload = self.broker.positions() or {}
                # A broker wrapper may already have observed the payload.
                if getattr(self.broker, "_trade_excursion_observer_installed", False):
                    return self.status()
            return self.observe_payload(payload)
        except Exception as exc:
            with self._lock:
                self._state["last_error"] = f"{type(exc).__name__}: {exc}"
                self._state["last_sync_attempt_at"] = time.time()
                self._persist()
            return self.status()

    def rows(self, limit: int = 500) -> List[Dict[str, Any]]:
        with self._lock:
            rows = [
                copy.deepcopy(row)
                for row in self._state.setdefault("trades", {}).values()
                if isinstance(row, dict)
            ]
        rows.sort(
            key=lambda row: (
                str(row.get("status") or "").upper() == "OPEN",
                float(row.get("last_observed_at") or row.get("closed_observed_at") or 0.0),
            ),
            reverse=True,
        )
        return rows[: max(1, min(int(limit), 2000))]

    def by_deal_id(self) -> Dict[str, Dict[str, Any]]:
        return {
            str(row.get("deal_id")): row
            for row in self.rows(limit=2000)
            if row.get("deal_id")
        }

    def _lookup(self, deal_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._state.setdefault("trades", {}).get(str(deal_id))
            return copy.deepcopy(row) if isinstance(row, dict) else None

    def merge(self, row: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(row)
        key = str(
            out.get("deal_id")
            or out.get("ig_deal_id")
            or out.get("trade_id")
            or out.get("id")
            or ""
        ).strip()
        if not key:
            return out
        excursion = self._lookup(key)
        if not excursion:
            return out
        for field in (
            "highest_price_since_entry",
            "lowest_price_since_entry",
            "current_price",
            "price_basis",
            "mfe",
            "mae",
            "mfe_pct",
            "mae_pct",
            "mae_abs",
            "mae_abs_pct",
            "highest_price_vs_entry_pct",
            "highest_price_as_pct_of_entry",
            "lowest_price_vs_entry_pct",
            "current_favourable_pct",
            "take_profit_enabled",
            "take_profit_target_pct",
            "take_profit_basis",
            "take_profit_target_price",
            "take_profit_reached",
            "take_profit_reached_at",
            "take_profit_first_reached_price",
            "take_profit_triggered_at",
            "take_profit_trigger_price",
            "take_profit_trigger_favourable_pct",
            "take_profit_close_state",
            "take_profit_close_attempts",
            "take_profit_closed_at",
            "take_profit_close_error",
            "native_take_profit_state",
            "native_take_profit_level",
            "native_take_profit_attached_at",
            "native_take_profit_attempts",
            "native_take_profit_error",
            "close_reason",
            "last_observed_at",
        ):
            if excursion.get(field) is not None:
                out[field] = excursion.get(field)
        out["excursion_tracking"] = {
            "source": "IG_DEMO_PERIODIC_REST",
            "poll_seconds": self.poll_seconds,
            "price_basis": excursion.get("price_basis"),
            "last_observed_at": excursion.get("last_observed_at"),
            "take_profit_enabled": excursion.get("take_profit_enabled"),
            "take_profit_target_pct": excursion.get("take_profit_target_pct"),
            "take_profit_close_state": excursion.get("take_profit_close_state"),
            "native_take_profit_state": excursion.get("native_take_profit_state"),
            "native_take_profit_level": excursion.get("native_take_profit_level"),
        }
        return out

    def _merge_compound_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        out = copy.deepcopy(payload)
        rows = out.get("compound_broker_positions")
        if isinstance(rows, list):
            out["compound_broker_positions"] = [
                self.merge(row) if isinstance(row, dict) else row for row in rows
            ]
        current = out.get("current_cycle")
        if isinstance(current, dict) and isinstance(current.get("positions"), list):
            current["positions"] = [
                self.merge(row) if isinstance(row, dict) else row
                for row in current.get("positions") or []
            ]
        cycles = out.get("recent_cycles")
        if isinstance(cycles, list):
            for cycle in cycles:
                if isinstance(cycle, dict) and isinstance(cycle.get("positions"), list):
                    cycle["positions"] = [
                        self.merge(row) if isinstance(row, dict) else row
                        for row in cycle.get("positions") or []
                    ]
        return out

    def install_runtime_merges(
        self,
        *,
        portfolio: Optional[Any] = None,
        compound_engine: Optional[Any] = None,
        legacy_evidence: Optional[Any] = None,
    ) -> None:
        """Expose excursion and take-profit telemetry through existing read surfaces."""
        if portfolio is not None and not getattr(
            portfolio, "_excursion_positions_patch", False
        ):
            original_positions = getattr(portfolio, "positions", None)
            if callable(original_positions):
                def positions(component_self: Any, *args: Any, **kwargs: Any):
                    rows = original_positions(*args, **kwargs) or []
                    return [
                        self.merge(dict(row)) if isinstance(row, dict) else row
                        for row in rows
                    ]
                portfolio.positions = MethodType(positions, portfolio)
                portfolio._excursion_positions_patch = True

        if compound_engine is not None and not getattr(
            compound_engine, "_excursion_status_patch", False
        ):
            original_status = getattr(compound_engine, "status", None)
            if callable(original_status):
                def compound_status(component_self: Any, *args: Any, **kwargs: Any):
                    payload = original_status(*args, **kwargs) or {}
                    return (
                        self._merge_compound_payload(payload)
                        if isinstance(payload, dict)
                        else payload
                    )
                compound_engine.status = MethodType(compound_status, compound_engine)
                compound_engine._excursion_status_patch = True

        if legacy_evidence is not None:
            if not getattr(legacy_evidence, "_excursion_status_patch", False):
                original_status = getattr(legacy_evidence, "status", None)
                if callable(original_status):
                    def legacy_status(component_self: Any, *args: Any, **kwargs: Any):
                        payload = original_status(*args, **kwargs) or {}
                        if not isinstance(payload, dict):
                            return payload
                        out = copy.deepcopy(payload)
                        mirrors = out.get("mirrors")
                        if isinstance(mirrors, dict):
                            out["mirrors"] = {
                                key: self.merge(value) if isinstance(value, dict) else value
                                for key, value in mirrors.items()
                            }
                        elif isinstance(mirrors, list):
                            out["mirrors"] = [
                                self.merge(value) if isinstance(value, dict) else value
                                for value in mirrors
                            ]
                        return out
                    legacy_evidence.status = MethodType(legacy_status, legacy_evidence)
                    legacy_evidence._excursion_status_patch = True

            if not getattr(legacy_evidence, "_excursion_settled_patch", False):
                original_rows = getattr(legacy_evidence, "_settled_broker_rows", None)
                if callable(original_rows):
                    def settled_rows(component_self: Any, *args: Any, **kwargs: Any):
                        rows = original_rows(*args, **kwargs) or []
                        return [
                            self.merge(dict(row)) if isinstance(row, dict) else row
                            for row in rows
                        ]
                    legacy_evidence._settled_broker_rows = MethodType(
                        settled_rows, legacy_evidence
                    )
                    legacy_evidence._excursion_settled_patch = True

    def status(self) -> Dict[str, Any]:
        rows = self.rows(limit=2000)
        return {
            "version": self.VERSION,
            "enabled": True,
            "mfe_mae_observation_only": True,
            "take_profit_execution_enabled": bool(self.take_profit_enabled),
            "take_profit_target_pct": self.take_profit_pct,
            "take_profit_basis": "ENTRY_PRICE_FAVOURABLE_MOVE_PCT",
            "take_profit_scope": "JASONG_OWNED_IG_DEMO_POSITIONS_ONLY",
            "take_profit_primary_execution": "IG_DEMO_NATIVE_LIMIT_LEVEL",
            "take_profit_fallback_execution": "SERVER_OBSERVED_CLOSE_POSITION",
            "poll_seconds": self.poll_seconds,
            "price_extreme_basis": "BROKER_OBSERVED_EXIT_SIDE_QUOTES",
            "formulas": {
                "BUY_MFE": "highest_price_since_entry - entry_price",
                "BUY_MAE": "lowest_price_since_entry - entry_price",
                "SELL_MFE": "entry_price - lowest_price_since_entry",
                "SELL_MAE": "entry_price - highest_price_since_entry",
                "MFE_PCT": "MFE / entry_price * 100",
                "MAE_PCT": "MAE / entry_price * 100",
                "HIGHEST_VS_ENTRY_PCT": "(highest_price_since_entry - entry_price) / entry_price * 100",
                "HIGHEST_AS_PCT_OF_ENTRY": "highest_price_since_entry / entry_price * 100",
            },
            "tracked_trades": len(rows),
            "open_trades": sum(
                1 for row in rows if str(row.get("status") or "").upper() == "OPEN"
            ),
            "take_profit_reached_trades": sum(
                1 for row in rows if bool(row.get("take_profit_reached"))
            ),
            "take_profit_close_sent_trades": sum(
                1 for row in rows
                if str(row.get("take_profit_close_state") or "").upper()
                in {"CLOSE_SENT", "CLOSE_VERIFIED"}
            ),
            "native_take_profit_attached_trades": sum(
                1 for row in rows
                if str(row.get("native_take_profit_state") or "").upper()
                in {"ATTACHED", "CONFIRMED"}
            ),
            "last_sync_at": self._state.get("last_sync_at"),
            "last_error": self._state.get("last_error"),
            "sync_count": int(self._state.get("sync_count") or 0),
            "state_path": str(self.state_path),
            "live_money_execution": False,
        }

    def snapshot(self, limit: int = 500) -> Dict[str, Any]:
        return {
            **self.status(),
            "trades": self.rows(limit=limit),
        }

    def start_thread(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="jasong-trade-excursions",
                daemon=True,
            )
            self._thread.start()

    def stop_thread(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.sync_once()
            self._stop.wait(self.poll_seconds)
