from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ig_demo_broker import IGDemoBroker, IGDemoError


class IGDemoMirror:
    """
    Mirrors Jasong AI learning trades to IG DEMO.

    Phase-1 defaults:
    - 10 accepted IG DEMO entries total.
    - Up to 3 broker positions open at once.
    - Only trades already opened by the Jasong AI learning engine are mirrored.
    - IG market availability is treated as a hard execution requirement.
    """

    def __init__(
        self,
        *,
        broker: IGDemoBroker,
        trade_source: Callable[[], Dict[str, Any]],
    ) -> None:
        self.broker = broker
        self.trade_source = trade_source

        self.enabled = self._bool_env(
            "IG_DEMO_AUTOTRADE",
            False,
        )
        self.phase_target = self._int_env(
            "IG_DEMO_PHASE_TARGET",
            10,
            minimum=1,
            maximum=10000,
        )
        self.max_open_positions = self._int_env(
            "IG_DEMO_MAX_OPEN_POSITIONS",
            3,
            minimum=1,
            maximum=20,
        )
        self.poll_seconds = self._int_env(
            "IG_DEMO_MIRROR_POLL_SECONDS",
            15,
            minimum=5,
            maximum=300,
        )

        default_state = (
            "/var/data/jasong_ig_demo_mirror.json"
            if Path("/var/data").exists()
            else "/tmp/jasong_ig_demo_mirror.json"
        )
        self.state_path = Path(
            os.getenv(
                "IG_DEMO_STATE_PATH",
                default_state,
            )
        )

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state: Dict[str, Any] = {
            "version": "6.6.3-IG-DEMO",
            "mirrors": {},
            "last_sync_at": None,
            "last_error": None,
        }
        self._load()

    @staticmethod
    def _bool_env(name: str, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    @staticmethod
    def _int_env(
        name: str,
        default: int,
        *,
        minimum: int,
        maximum: int,
    ) -> int:
        try:
            value = int(os.getenv(name, str(default)))
        except Exception:
            value = default
        return max(minimum, min(maximum, value))

    def _load(self) -> None:
        try:
            if self.state_path.exists():
                data = json.loads(
                    self.state_path.read_text(
                        encoding="utf-8"
                    )
                )
                if isinstance(data, dict):
                    self._state.update(data)
        except Exception as exc:
            self._state["last_error"] = (
                f"state load: {exc}"
            )

    def _persist(self) -> None:
        try:
            self.state_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            tmp = self.state_path.with_suffix(
                self.state_path.suffix + ".tmp"
            )
            tmp.write_text(
                json.dumps(
                    self._state,
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            tmp.replace(self.state_path)
        except Exception as exc:
            self._state["last_error"] = (
                f"state persist: {exc}"
            )

    def start(self) -> Dict[str, Any]:
        with self._lock:
            if (
                self._thread is not None
                and self._thread.is_alive()
            ):
                return self.status()

            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="jasong-ig-demo-mirror",
                daemon=True,
            )
            self._thread.start()
        return self.status()

    def stop(self) -> Dict[str, Any]:
        self._stop_event.set()
        return self.status()

    def set_enabled(self, enabled: bool) -> Dict[str, Any]:
        with self._lock:
            self.enabled = bool(enabled)
        return self.status()

    def _run(self) -> None:
        while not self._stop_event.wait(
            self.poll_seconds
        ):
            try:
                self.sync_once()
            except Exception as exc:
                with self._lock:
                    self._state["last_error"] = (
                        f"{type(exc).__name__}: {exc}"
                    )
                    self._state["last_sync_at"] = (
                        time.time()
                    )
                    self._persist()

    def _mirrors(self) -> Dict[str, Dict[str, Any]]:
        mirrors = self._state.setdefault(
            "mirrors",
            {},
        )
        return mirrors

    def _accepted_count(self) -> int:
        return sum(
            1
            for item in self._mirrors().values()
            if item.get("ig_deal_id")
        )

    def _open_count(self) -> int:
        return sum(
            1
            for item in self._mirrors().values()
            if item.get("broker_status") == "OPEN"
        )

    def phase_complete(self) -> bool:
        return self._accepted_count() >= self.phase_target

    def execution_required(self) -> bool:
        return bool(
            self.enabled
            and self.broker.configured()
            and not self.phase_complete()
        )

    def status(self) -> Dict[str, Any]:
        with self._lock:
            mirrors = list(
                self._mirrors().values()
            )
            mirrors.sort(
                key=lambda x: float(
                    x.get("created_at") or 0.0
                ),
                reverse=True,
            )
            return {
                "version": "6.6.3-IG-DEMO",
                "broker": self.broker.status(),
                "enabled": self.enabled,
                "configured": self.broker.configured(),
                "phase_target": self.phase_target,
                "phase_accepted_trades": self._accepted_count(),
                "phase_remaining": max(
                    0,
                    self.phase_target
                    - self._accepted_count(),
                ),
                "phase_complete": self.phase_complete(),
                "max_open_positions": self.max_open_positions,
                "open_broker_positions": self._open_count(),
                "poll_seconds": self.poll_seconds,
                "last_sync_at": self._state.get("last_sync_at"),
                "last_error": self._state.get("last_error"),
                "mirrors": mirrors[:50],
                "environment": "DEMO",
                "live_money_execution": False,
            }

    def preflight_symbol(
        self,
        symbol: str,
    ) -> Dict[str, Any]:
        if not self.execution_required():
            return {
                "required": False,
                "ok": True,
                "reason": "IG DEMO autotrade not active",
            }
        try:
            market = self.broker.resolve_market(
                symbol,
                require_tradeable=True,
            )
            return {
                "required": True,
                "ok": True,
                "market": {
                    key: market.get(key)
                    for key in (
                        "symbol",
                        "epic",
                        "expiry",
                        "name",
                        "market_status",
                    )
                },
            }
        except Exception as exc:
            return {
                "required": True,
                "ok": False,
                "reason": str(exc),
            }

    @staticmethod
    def _trade_rows(
        payload: Dict[str, Any],
    ) -> list[Dict[str, Any]]:
        rows = payload.get("trades", [])
        return [
            dict(item)
            for item in rows
            if isinstance(item, dict)
        ]

    def _close_due(
        self,
        trade: Dict[str, Any],
        mirror: Dict[str, Any],
    ) -> bool:
        status = str(
            trade.get("status") or ""
        ).upper()
        if status != "OPEN":
            return True

        try:
            due = float(
                trade.get("scheduled_close_at")
                or 0.0
            )
        except Exception:
            due = 0.0

        return bool(
            due > 0
            and time.time() >= due
        )

    def sync_once(self) -> Dict[str, Any]:
        with self._lock:
            self._state["last_sync_at"] = time.time()

        if not self.enabled:
            return self.status()

        if not self.broker.configured():
            with self._lock:
                self._state["last_error"] = (
                    "IG DEMO credentials not configured"
                )
                self._persist()
            return self.status()

        self.broker.connect()

        trade_payload = self.trade_source()
        trades = self._trade_rows(trade_payload)
        by_id = {
            str(item.get("trade_id")): item
            for item in trades
            if item.get("trade_id")
        }

        # 1. Close broker positions whose AI learning trade has settled
        #    or whose planned holding period is due.
        for trade_id, mirror in list(
            self._mirrors().items()
        ):
            if mirror.get("broker_status") != "OPEN":
                continue

            trade = by_id.get(trade_id)
            if trade is None:
                continue

            if self._close_due(trade, mirror):
                deal_id = str(
                    mirror.get("ig_deal_id")
                    or ""
                )
                if not deal_id:
                    continue
                try:
                    result = self.broker.close_position(
                        deal_id
                    )
                    mirror["broker_status"] = "CLOSED"
                    mirror["closed_at"] = time.time()
                    mirror["close_result"] = result
                    mirror["internal_result"] = trade.get(
                        "result"
                    )
                    mirror["internal_pnl"] = trade.get(
                        "pnl"
                    )
                except Exception as exc:
                    mirror["last_error"] = (
                        f"close: {type(exc).__name__}: {exc}"
                    )

        # 2. Mirror newly opened Jasong AI learning trades.
        if not self.phase_complete():
            for trade in trades:
                if self.phase_complete():
                    break
                if self._open_count() >= self.max_open_positions:
                    break

                trade_id = str(
                    trade.get("trade_id") or ""
                )
                if not trade_id:
                    continue
                if trade_id in self._mirrors():
                    continue
                if str(trade.get("status") or "").upper() != "OPEN":
                    continue

                symbol = str(
                    trade.get("symbol")
                    or trade.get("market")
                    or ""
                )
                direction = str(
                    trade.get("direction")
                    or ""
                ).upper()

                mirror = {
                    "trade_id": trade_id,
                    "symbol": symbol,
                    "direction": direction,
                    "entry_class": trade.get("entry_class"),
                    "model_ai_confidence": trade.get(
                        "model_ai_confidence"
                    ),
                    "internal_entry_price": trade.get(
                        "entry_price"
                    ),
                    "scheduled_close_at": trade.get(
                        "scheduled_close_at"
                    ),
                    "created_at": time.time(),
                    "broker_status": "SUBMITTING",
                    "environment": "DEMO",
                    "live_money_execution": False,
                }
                self._mirrors()[trade_id] = mirror
                self._persist()

                try:
                    result = self.broker.open_market_position(
                        symbol=symbol,
                        direction=direction,
                        deal_reference=(
                            f"JASONG_{trade_id.replace('-', '')[:20]}"
                        ),
                    )
                    mirror["open_result"] = result
                    mirror["ig_deal_id"] = result.get(
                        "dealId"
                    )
                    mirror["ig_deal_reference"] = result.get(
                        "dealReference"
                    )
                    mirror["ig_epic"] = result.get("epic")
                    mirror["ig_size"] = result.get("size")
                    mirror["broker_entry_level"] = result.get(
                        "level"
                    )
                    mirror["broker_status"] = (
                        "OPEN"
                        if result.get("dealStatus") != "REJECTED"
                        else "REJECTED"
                    )
                    mirror["opened_at"] = time.time()
                    mirror["last_error"] = None

                except Exception as exc:
                    mirror["broker_status"] = "ERROR"
                    mirror["last_error"] = (
                        f"open: {type(exc).__name__}: {exc}"
                    )

        with self._lock:
            self._state["last_error"] = None
            self._persist()

        return self.status()
