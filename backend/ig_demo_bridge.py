from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ig_demo_broker import IGDemoBroker, IGDemoError


class IGDemoMirror:
    CLOSE_ALLOWED_MARKET_STATUSES = {
        "TRADEABLE",
        "CLOSINGS_ONLY",
    }

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
        self.retry_seconds = self._int_env(
            "IG_DEMO_RETRY_SECONDS",
            65,
            minimum=15,
            maximum=300,
        )
        self.max_open_retries = self._int_env(
            "IG_DEMO_MAX_OPEN_RETRIES",
            8,
            minimum=1,
            maximum=20,
        )
        self.default_hold_seconds = self._int_env(
            "IG_DEMO_DEFAULT_HOLD_SECONDS",
            3600,
            minimum=60,
            maximum=86400,
        )
        self.reconcile_max_age_seconds = self._int_env(
            "IG_DEMO_RECONCILE_MAX_AGE_SECONDS",
            25,
            minimum=10,
            maximum=300,
        )
        self.account_refresh_seconds = self._int_env(
            "IG_DEMO_ACCOUNT_REFRESH_SECONDS",
            30,
            minimum=15,
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
        self._sync_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state: Dict[str, Any] = {
            "version": "6.7.2a-IG-DEMO-MARKET-AWARE-CLOSE",
            "mirrors": {},
            "broker_positions": [],
            "broker_account": {},
            "last_sync_at": None,
            "last_broker_sync_at": None,
            "last_account_sync_at": None,
            "last_error": None,
            "broker_reconciliation_error": None,
            "account_sync_error": None,
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
        # Reconcile immediately on process start. This lets a restarted
        # backend rediscover JASONG_* IG DEMO positions before the phone opens.
        while not self._stop_event.is_set():
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

            if self._stop_event.wait(
                self.poll_seconds
            ):
                break

    def _mirrors(self) -> Dict[str, Dict[str, Any]]:
        mirrors = self._state.setdefault(
            "mirrors",
            {},
        )
        return mirrors

    @staticmethod
    def _timestamp_from_ig(value: Any) -> Optional[float]:
        text = str(value or "").strip()
        if not text:
            return None

        candidates = [
            text,
            text.replace("Z", "+00:00"),
        ]

        for candidate in candidates:
            try:
                parsed = datetime.fromisoformat(candidate)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.timestamp()
            except Exception:
                continue

        # IG sometimes returns fractional seconds with a space separator.
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y/%m/%d %H:%M:%S",
        ):
            try:
                parsed = datetime.strptime(
                    text.split(".")[0],
                    fmt,
                ).replace(tzinfo=timezone.utc)
                return parsed.timestamp()
            except Exception:
                continue
        return None

    @staticmethod
    def _symbol_from_market(
        market: Dict[str, Any],
        epic: str,
    ) -> str:
        name = str(
            market.get("instrumentName")
            or market.get("name")
            or ""
        ).upper()

        match = re.search(
            r"\b([A-Z]{3})\s*[/ -]\s*([A-Z]{3})\b",
            name,
        )
        if match:
            return f"{match.group(1)}/{match.group(2)}"

        # Some IG names contain the pair without a separator.
        compact = re.sub(r"[^A-Z]", "", name)
        core_codes = {
            "EURUSD",
            "GBPUSD",
            "USDJPY",
            "AUDUSD",
            "NZDUSD",
            "USDCAD",
            "USDCHF",
            "EURJPY",
            "GBPJPY",
        }
        for pair in core_codes:
            if pair in compact:
                return f"{pair[:3]}/{pair[3:]}"

        return str(epic or name or "IG DEMO")

    def _normalise_ig_positions(
        self,
        payload: Dict[str, Any],
    ) -> list[Dict[str, Any]]:
        rows: list[Dict[str, Any]] = []

        for item in payload.get("positions", []) or []:
            if not isinstance(item, dict):
                continue

            position = item.get("position") or {}
            market = item.get("market") or {}
            if not isinstance(position, dict):
                continue
            if not isinstance(market, dict):
                market = {}

            deal_reference = str(
                position.get("dealReference")
                or ""
            ).strip()

            # Critical safety boundary: only adopt positions created by Jasong.
            # Manual IG DEMO positions remain untouched.
            if not deal_reference.startswith("JASONG_"):
                continue

            deal_id = str(
                position.get("dealId")
                or ""
            ).strip()
            if not deal_id:
                continue

            epic = str(
                market.get("epic")
                or position.get("epic")
                or ""
            ).strip()
            opened_at = self._timestamp_from_ig(
                position.get("createdDateUTC")
                or position.get("createdDate")
            )
            if opened_at is None:
                opened_at = time.time()

            try:
                size = float(
                    position.get("dealSize")
                    or position.get("size")
                    or 0.0
                )
            except Exception:
                size = 0.0

            try:
                entry_level = float(
                    position.get("level")
                    or 0.0
                )
            except Exception:
                entry_level = 0.0

            rows.append({
                "deal_id": deal_id,
                "deal_reference": deal_reference,
                "symbol": self._symbol_from_market(
                    market,
                    epic,
                ),
                "market": (
                    market.get("instrumentName")
                    or market.get("name")
                    or self._symbol_from_market(
                        market,
                        epic,
                    )
                ),
                "epic": epic,
                "direction": str(
                    position.get("direction")
                    or ""
                ).upper(),
                "size": size,
                "entry_level": entry_level,
                "opened_at": opened_at,
                "scheduled_close_at": (
                    opened_at
                    + self.default_hold_seconds
                ),
                "bid": market.get("bid"),
                "offer": market.get("offer"),
                "market_status": market.get("marketStatus"),
                "status": "OPEN",
                "source": "IG_DEMO_BROKER",
                "environment": "DEMO",
                "live_money_execution": False,
            })

        rows.sort(
            key=lambda row: float(
                row.get("opened_at") or 0.0
            ),
            reverse=True,
        )
        return rows

    @staticmethod
    def _signed_move_bps(
        *,
        entry_level: Any,
        direction: Any,
        bid: Any,
        offer: Any,
    ) -> Optional[float]:
        """Executable price excursion in basis points from broker entry."""
        try:
            entry = float(entry_level or 0.0)
            bid_value = float(bid or 0.0)
            offer_value = float(offer or 0.0)
        except Exception:
            return None
        if entry <= 0:
            return None

        side = str(direction or "").upper()
        if side == "BUY":
            exit_now = bid_value
            if exit_now <= 0:
                return None
            return ((exit_now - entry) / entry) * 10000.0
        if side == "SELL":
            exit_now = offer_value
            if exit_now <= 0:
                return None
            return ((entry - exit_now) / entry) * 10000.0
        return None

    def _update_mirror_excursion(
        self,
        mirror: Dict[str, Any],
        position: Dict[str, Any],
    ) -> None:
        move = self._signed_move_bps(
            entry_level=(
                mirror.get("broker_entry_level")
                or position.get("entry_level")
            ),
            direction=(
                mirror.get("direction")
                or position.get("direction")
            ),
            bid=position.get("bid"),
            offer=position.get("offer"),
        )
        if move is None:
            return

        previous_mfe = self._safe_number(
            mirror.get("mfe_bps")
        )
        previous_mae = self._safe_number(
            mirror.get("mae_bps")
        )

        mirror["current_move_bps"] = round(move, 4)
        mirror["mfe_bps"] = round(
            max(
                0.0,
                previous_mfe
                if previous_mfe is not None
                else 0.0,
                move,
            ),
            4,
        )
        mirror["mae_bps"] = round(
            min(
                0.0,
                previous_mae
                if previous_mae is not None
                else 0.0,
                move,
            ),
            4,
        )
        mirror["last_excursion_at"] = time.time()

    def _mirror_by_deal_id(
        self,
        deal_id: str,
    ) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
        for key, mirror in self._mirrors().items():
            if str(
                mirror.get("ig_deal_id")
                or ""
            ) == str(deal_id):
                return key, mirror
        return None, None

    def _mirror_by_reference(
        self,
        deal_reference: str,
    ) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
        for key, mirror in self._mirrors().items():
            if str(
                mirror.get("ig_deal_reference")
                or ""
            ) == str(deal_reference):
                return key, mirror
        return None, None

    @staticmethod
    def _safe_number(
        value: Any,
    ) -> Optional[float]:
        try:
            if value is None:
                return None
            return float(value)
        except Exception:
            return None

    def _normalise_account_snapshot(
        self,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Extract the active IG DEMO account balance/P&L without exposing tokens."""
        broker_status = self.broker.status()
        active_account_id = str(
            broker_status.get("account_id")
            or ""
        )

        rows = [
            dict(item)
            for item in payload.get("accounts", []) or []
            if isinstance(item, dict)
        ]

        selected: Dict[str, Any] = {}
        for item in rows:
            if (
                active_account_id
                and str(item.get("accountId") or "")
                == active_account_id
            ):
                selected = item
                break

        if not selected:
            for item in rows:
                if item.get("preferred") is True:
                    selected = item
                    break

        if not selected and rows:
            selected = rows[0]

        balance_block = selected.get("balance")
        if not isinstance(balance_block, dict):
            balance_block = {}

        account_balance = self._safe_number(
            balance_block.get("balance")
        )
        account_available = self._safe_number(
            balance_block.get("available")
        )
        account_deposit = self._safe_number(
            balance_block.get("deposit")
        )
        account_profit_loss = self._safe_number(
            balance_block.get("profitLoss")
        )

        return {
            "account_id": (
                selected.get("accountId")
                or active_account_id
                or None
            ),
            "account_name": selected.get("accountName"),
            "account_type": selected.get("accountType"),
            "currency": (
                selected.get("currency")
                or selected.get("currencyIsoCode")
            ),
            "preferred": selected.get("preferred"),
            "balance": account_balance,
            "available": account_available,
            "margin": account_deposit,
            "profit_loss": account_profit_loss,
            "source": "IG_DEMO_ACCOUNT",
            "environment": "DEMO",
            "live_money_execution": False,
        }

    def refresh_account_snapshot(
        self,
        *,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Refresh account balance/floating P&L at a quota-safe cadence."""
        if not self.broker.configured():
            return dict(
                self._state.get("broker_account")
                or {}
            )

        now = time.time()
        last = float(
            self._state.get("last_account_sync_at")
            or 0.0
        )

        if (
            not force
            and last > 0
            and now - last < self.account_refresh_seconds
        ):
            return dict(
                self._state.get("broker_account")
                or {}
            )

        try:
            payload = self.broker.accounts()
            snapshot = self._normalise_account_snapshot(
                payload
            )
            with self._lock:
                self._state["broker_account"] = snapshot
                self._state["last_account_sync_at"] = now
                self._state["account_sync_error"] = None
                self._persist()
            return dict(snapshot)
        except Exception as exc:
            with self._lock:
                self._state["account_sync_error"] = (
                    f"{type(exc).__name__}: {exc}"
                )
                self._persist()
            return dict(
                self._state.get("broker_account")
                or {}
            )

    def reconcile_broker_positions(
        self,
    ) -> Dict[str, Any]:
        """Rebuild local mirror state from authoritative IG DEMO positions.

        This is intentionally independent of the phone/app and allows a
        restarted backend to recover bot-created positions using JASONG_ deal
        references.
        """
        if not self.broker.configured():
            return self.status()

        self.broker.connect()
        payload = self.broker.positions()
        positions = self._normalise_ig_positions(
            payload
        )
        now = time.time()
        current_deal_ids = {
            str(row.get("deal_id") or "")
            for row in positions
        }

        with self._lock:
            mirrors = self._mirrors()

            for position in positions:
                deal_id = str(
                    position.get("deal_id")
                    or ""
                )
                deal_reference = str(
                    position.get("deal_reference")
                    or ""
                )

                key, mirror = self._mirror_by_deal_id(
                    deal_id
                )
                if mirror is None:
                    key, mirror = self._mirror_by_reference(
                        deal_reference
                    )

                if mirror is None:
                    key = f"IG_RECOVERED_{deal_id}"
                    mirror = {
                        "trade_id": key,
                        "created_at": position.get(
                            "opened_at"
                        ),
                        "recovered_from_ig": True,
                        "entry_class": "IG_RECOVERED",
                        "environment": "DEMO",
                        "live_money_execution": False,
                        "open_attempts": 0,
                        "close_attempts": 0,
                        "close_requested_at": None,
                        "close_verified": False,
                        "mfe_bps": 0.0,
                        "mae_bps": 0.0,
                    }
                    mirrors[key] = mirror

                mirror.update({
                    "symbol": position.get("symbol"),
                    "market": position.get("market"),
                    "direction": position.get("direction"),
                    "ig_deal_id": deal_id,
                    "ig_deal_reference": deal_reference,
                    "ig_epic": position.get("epic"),
                    "ig_size": position.get("size"),
                    "market_status": position.get("market_status"),
                    "broker_entry_level": position.get(
                        "entry_level"
                    ),
                    "opened_at": (
                        mirror.get("opened_at")
                        or position.get("opened_at")
                    ),
                    "scheduled_close_at": (
                        mirror.get("scheduled_close_at")
                        or position.get(
                            "scheduled_close_at"
                        )
                    ),
                    "broker_status": "OPEN",
                    "last_seen_on_ig_at": now,
                    "last_error": None,
                })
                self._update_mirror_excursion(
                    mirror,
                    position,
                )

            # If a position used to be OPEN in our ledger but IG no longer
            # reports it, treat IG as authoritative. Do not delete the record:
            # keeping it is what preserves the Phase-1 sample count.
            for mirror in mirrors.values():
                deal_id = str(
                    mirror.get("ig_deal_id")
                    or ""
                )
                previous_status = str(
                    mirror.get("broker_status")
                    or ""
                ).upper()
                if (
                    deal_id
                    and previous_status
                    in {
                        "OPEN",
                        "CLOSE_PENDING",
                    }
                    and deal_id not in current_deal_ids
                ):
                    initiated_close = bool(
                        mirror.get("close_requested_at")
                    )
                    mirror["broker_status"] = (
                        "CLOSED"
                        if initiated_close
                        else "CLOSED_EXTERNALLY"
                    )
                    mirror["closed_at"] = (
                        mirror.get("closed_at")
                        or now
                    )
                    mirror["close_verified"] = True
                    mirror["remaining_size"] = 0.0
                    mirror["externally_closed"] = (
                        not initiated_close
                    )

            self._state["broker_positions"] = positions
            self._state["last_broker_sync_at"] = now
            self._state[
                "broker_reconciliation_error"
            ] = None
            self._persist()

        # Account balance/floating P&L is a separate IG endpoint. Refresh it
        # at a slower cadence so performance is broker-grounded without
        # burning the non-trading REST allowance.
        self.refresh_account_snapshot()

        return self.status()

    def ensure_broker_fresh(
        self,
        max_age_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Refresh IG position truth only when the cached snapshot is stale."""
        if not self.broker.configured():
            return self.status()

        maximum_age = int(
            max_age_seconds
            if max_age_seconds is not None
            else self.reconcile_max_age_seconds
        )
        last = float(
            self._state.get("last_broker_sync_at")
            or 0.0
        )

        if (
            last > 0
            and time.time() - last < maximum_age
        ):
            return self.status()

        # Avoid duplicate GET /positions calls when app polling and background
        # sync overlap.
        acquired = self._sync_lock.acquire(
            blocking=False
        )
        if not acquired:
            return self.status()

        try:
            return self.reconcile_broker_positions()
        except Exception as exc:
            with self._lock:
                self._state[
                    "broker_reconciliation_error"
                ] = (
                    f"{type(exc).__name__}: {exc}"
                )
                self._persist()
            return self.status()
        finally:
            self._sync_lock.release()

    def _accepted_count(self) -> int:
        return len({
            str(item.get("ig_deal_id"))
            for item in self._mirrors().values()
            if item.get("ig_deal_id")
        })

    def _open_count(self) -> int:
        # Combine the last authoritative IG snapshot with positions opened
        # since that snapshot during the current cycle.
        positions = self._state.get(
            "broker_positions"
        )
        broker_count = (
            len(positions)
            if isinstance(positions, list)
            else 0
        )
        ledger_count = sum(
            1
            for item in self._mirrors().values()
            if str(
                item.get("broker_status")
                or ""
            ).upper() == "OPEN"
        )
        return max(
            broker_count,
            ledger_count,
        )

    def _broker_stats(self) -> Dict[str, Any]:
        mirrors = list(
            self._mirrors().values()
        )

        graded = [
            item
            for item in mirrors
            if str(
                item.get("broker_result")
                or ""
            ).upper() in {"WIN", "LOSS"}
        ]

        closed = [
            item
            for item in mirrors
            if str(
                item.get("broker_status")
                or ""
            ).upper() in {
                "CLOSED",
                "CLOSED_EXTERNALLY",
            }
        ]

        wins = sum(
            1
            for item in graded
            if str(
                item.get("broker_result")
                or ""
            ).upper() == "WIN"
        )
        losses = len(graded) - wins

        account = dict(
            self._state.get("broker_account")
            or {}
        )

        return {
            "accepted_trades": self._accepted_count(),
            "open_positions": self._open_count(),
            "closed_positions": len(closed),
            "graded_trades": len(graded),
            # Backward compatibility.
            "trades": len(graded),
            "wins": wins,
            "losses": losses,
            "win_rate_pct": (
                round(
                    wins / len(graded) * 100.0,
                    2,
                )
                if graded
                else 0.0
            ),
            "account_balance": account.get("balance"),
            "account_available": account.get("available"),
            "account_margin": account.get("margin"),
            "account_profit_loss": account.get("profit_loss"),
            "account_currency": account.get("currency"),
        }

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

            broker_positions = list(
                self._state.get(
                    "broker_positions"
                )
                or []
            )
            broker_account = dict(
                self._state.get(
                    "broker_account"
                )
                or {}
            )
            last_broker_sync = float(
                self._state.get(
                    "last_broker_sync_at"
                )
                or 0.0
            )
            broker_sync_age = (
                max(
                    0.0,
                    time.time()
                    - last_broker_sync,
                )
                if last_broker_sync > 0
                else None
            )
            reconciliation_error = (
                self._state.get(
                    "broker_reconciliation_error"
                )
            )
            last_account_sync = float(
                self._state.get(
                    "last_account_sync_at"
                )
                or 0.0
            )
            account_sync_age = (
                max(
                    0.0,
                    time.time()
                    - last_account_sync,
                )
                if last_account_sync > 0
                else None
            )
            account_sync_error = self._state.get(
                "account_sync_error"
            )
            sync_state = (
                "ERROR"
                if reconciliation_error
                else (
                    "SYNCED"
                    if broker_sync_age is not None
                    and broker_sync_age
                    <= max(
                        60,
                        self.poll_seconds * 3,
                    )
                    else "STALE"
                )
            )

            return {
                "version": "6.7.2a-IG-DEMO-MARKET-AWARE-CLOSE",
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
                "broker_positions": broker_positions,
                "broker_account": broker_account,
                "broker_stats": self._broker_stats(),
                "broker_performance": self._broker_stats(),
                "sync_state": sync_state,
                "last_broker_sync_at": (
                    last_broker_sync
                    if last_broker_sync > 0
                    else None
                ),
                "broker_sync_age_seconds": (
                    round(
                        broker_sync_age,
                        2,
                    )
                    if broker_sync_age is not None
                    else None
                ),
                "broker_reconciliation_error":
                    reconciliation_error,
                "last_account_sync_at": (
                    last_account_sync
                    if last_account_sync > 0
                    else None
                ),
                "account_sync_age_seconds": (
                    round(
                        account_sync_age,
                        2,
                    )
                    if account_sync_age is not None
                    else None
                ),
                "account_sync_error":
                    account_sync_error,
                "account_refresh_seconds":
                    self.account_refresh_seconds,
                "poll_seconds": self.poll_seconds,
                "retry_seconds": self.retry_seconds,
                "max_open_retries": self.max_open_retries,
                "default_hold_seconds":
                    self.default_hold_seconds,
                "last_sync_at": self._state.get("last_sync_at"),
                "last_error": self._state.get("last_error"),
                "state_path": str(self.state_path),
                "mirrors": mirrors[:100],
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

    @staticmethod
    def _retryable_open_error(message: str) -> bool:
        clean = str(message or "").lower()
        return any(
            token in clean
            for token in (
                "exceeded-account-allowance",
                "exceeded-api-key-allowance",
                "http 429",
                "http 500",
                "http 502",
                "http 503",
                "http 504",
                "network error",
                "timeout",
            )
        )

    def _submit_open(
        self,
        trade: Dict[str, Any],
        mirror: Dict[str, Any],
    ) -> None:
        trade_id = str(
            trade.get("trade_id")
            or mirror.get("trade_id")
            or ""
        )
        symbol = str(
            trade.get("symbol")
            or trade.get("market")
            or mirror.get("symbol")
            or ""
        )
        direction = str(
            trade.get("direction")
            or mirror.get("direction")
            or ""
        ).upper()

        mirror["broker_status"] = "SUBMITTING"
        mirror["last_attempt_at"] = time.time()
        mirror["open_attempts"] = int(
            mirror.get("open_attempts")
            or 0
        ) + 1
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
            mirror["next_retry_at"] = None

        except Exception as exc:
            message = (
                f"open: {type(exc).__name__}: {exc}"
            )
            mirror["last_error"] = message

            attempts = int(
                mirror.get("open_attempts")
                or 0
            )
            retryable = self._retryable_open_error(
                message
            )

            if (
                retryable
                and attempts < self.max_open_retries
                and str(
                    trade.get("status")
                    or ""
                ).upper() == "OPEN"
            ):
                mirror["broker_status"] = "RETRY_WAIT"
                mirror["next_retry_at"] = (
                    time.time()
                    + self.retry_seconds
                )
            else:
                mirror["broker_status"] = "ERROR"
                mirror["next_retry_at"] = None

        self._persist()

    @staticmethod
    def _record_broker_outcome(
        mirror: Dict[str, Any],
        close_result: Dict[str, Any],
    ) -> None:
        try:
            entry = float(
                mirror.get("broker_entry_level")
                or 0.0
            )
            exit_level = float(
                close_result.get("level")
                or 0.0
            )
        except Exception:
            entry = 0.0
            exit_level = 0.0

        direction = str(
            mirror.get("direction")
            or ""
        ).upper()

        mirror["broker_exit_level"] = (
            exit_level
            if exit_level > 0
            else None
        )

        if entry <= 0 or exit_level <= 0:
            return

        if direction == "BUY":
            result = (
                "WIN"
                if exit_level > entry
                else "LOSS"
            )
            movement = exit_level - entry
        elif direction == "SELL":
            result = (
                "WIN"
                if exit_level < entry
                else "LOSS"
            )
            movement = entry - exit_level
        else:
            return

        mirror["broker_result"] = result
        mirror["broker_price_movement"] = movement

    def sync_once(self) -> Dict[str, Any]:
        # Serialise full mirror cycles. App polling may separately ask for
        # ensure_broker_fresh(), but only one mutating sync runs at a time.
        acquired = self._sync_lock.acquire(
            blocking=False
        )
        if not acquired:
            return self.status()

        try:
            with self._lock:
                self._state["last_sync_at"] = time.time()

            if not self.broker.configured():
                with self._lock:
                    self._state["last_error"] = (
                        "IG DEMO credentials not configured"
                    )
                    self._persist()
                return self.status()

            self.broker.connect()
            self.refresh_account_snapshot()

            # Always reconcile IG first, even if autotrade is temporarily
            # disabled. IG is the source of truth for open broker positions.
            try:
                payload = self.broker.positions()
                positions = self._normalise_ig_positions(
                    payload
                )
                now = time.time()
                current_ids = {
                    str(row.get("deal_id") or "")
                    for row in positions
                }

                with self._lock:
                    for position in positions:
                        deal_id = str(
                            position.get("deal_id")
                            or ""
                        )
                        deal_reference = str(
                            position.get("deal_reference")
                            or ""
                        )

                        key, mirror = self._mirror_by_deal_id(
                            deal_id
                        )
                        if mirror is None:
                            key, mirror = self._mirror_by_reference(
                                deal_reference
                            )

                        if mirror is None:
                            key = (
                                f"IG_RECOVERED_{deal_id}"
                            )
                            mirror = {
                                "trade_id": key,
                                "created_at":
                                    position.get(
                                        "opened_at"
                                    ),
                                "recovered_from_ig":
                                    True,
                                "entry_class":
                                    "IG_RECOVERED",
                                "environment": "DEMO",
                                "live_money_execution":
                                    False,
                                "open_attempts": 0,
                            }
                            self._mirrors()[
                                key
                            ] = mirror

                        mirror.update({
                            "symbol":
                                position.get(
                                    "symbol"
                                ),
                            "market":
                                position.get(
                                    "market"
                                ),
                            "direction":
                                position.get(
                                    "direction"
                                ),
                            "ig_deal_id":
                                deal_id,
                            "ig_deal_reference":
                                deal_reference,
                            "ig_epic":
                                position.get(
                                    "epic"
                                ),
                            "ig_size":
                                position.get(
                                    "size"
                                ),
                            "market_status":
                                position.get(
                                    "market_status"
                                ),
                            "broker_entry_level":
                                position.get(
                                    "entry_level"
                                ),
                            "opened_at":
                                mirror.get(
                                    "opened_at"
                                )
                                or position.get(
                                    "opened_at"
                                ),
                            "scheduled_close_at":
                                mirror.get(
                                    "scheduled_close_at"
                                )
                                or position.get(
                                    "scheduled_close_at"
                                ),
                            "broker_status":
                                "OPEN",
                            "last_seen_on_ig_at":
                                now,
                            "last_error":
                                None,
                        })
                        self._update_mirror_excursion(
                            mirror,
                            position,
                        )

                    for mirror in (
                        self._mirrors().values()
                    ):
                        deal_id = str(
                            mirror.get(
                                "ig_deal_id"
                            )
                            or ""
                        )
                        previous_status = str(
                            mirror.get(
                                "broker_status"
                            )
                            or ""
                        ).upper()
                        if (
                            deal_id
                            and previous_status
                            in {
                                "OPEN",
                                "CLOSE_PENDING",
                            }
                            and deal_id
                            not in current_ids
                        ):
                            initiated_close = bool(
                                mirror.get(
                                    "close_requested_at"
                                )
                            )
                            mirror[
                                "broker_status"
                            ] = (
                                "CLOSED"
                                if initiated_close
                                else "CLOSED_EXTERNALLY"
                            )
                            mirror[
                                "closed_at"
                            ] = (
                                mirror.get(
                                    "closed_at"
                                )
                                or now
                            )
                            mirror[
                                "close_verified"
                            ] = True
                            mirror[
                                "remaining_size"
                            ] = 0.0
                            mirror[
                                "externally_closed"
                            ] = (
                                not initiated_close
                            )

                    self._state[
                        "broker_positions"
                    ] = positions
                    self._state[
                        "last_broker_sync_at"
                    ] = now
                    self._state[
                        "broker_reconciliation_error"
                    ] = None
            except Exception as exc:
                with self._lock:
                    self._state[
                        "broker_reconciliation_error"
                    ] = (
                        f"{type(exc).__name__}: {exc}"
                    )

            trade_payload = self.trade_source()
            trades = self._trade_rows(
                trade_payload
            )
            by_id = {
                str(item.get("trade_id")): item
                for item in trades
                if item.get("trade_id")
            }

            # Close due JASONG positions. If internal learning state survived,
            # its due time wins. If it was lost during a restart, the recovered
            # broker position's own scheduled_close_at is used.
            for trade_id, mirror in list(
                self._mirrors().items()
            ):
                if str(
                    mirror.get("broker_status")
                    or ""
                ).upper() != "OPEN":
                    continue

                trade = by_id.get(trade_id)
                due = False

                if trade is not None:
                    due = self._close_due(
                        trade,
                        mirror,
                    )
                else:
                    try:
                        scheduled = float(
                            mirror.get(
                                "scheduled_close_at"
                            )
                            or 0.0
                        )
                    except Exception:
                        scheduled = 0.0
                    due = bool(
                        scheduled > 0
                        and time.time()
                        >= scheduled
                    )

                if not due:
                    continue

                deal_id = str(
                    mirror.get("ig_deal_id")
                    or ""
                )
                if not deal_id:
                    continue

                market_status = str(
                    mirror.get("market_status")
                    or ""
                ).upper().strip()

                if (
                    market_status
                    and market_status
                    not in self.CLOSE_ALLOWED_MARKET_STATUSES
                ):
                    # This is a scheduling deferral, not a broker execution
                    # failure. Keep the position OPEN and let the normal
                    # /positions reconciliation tell us when the market
                    # becomes closable again.
                    mirror["close_execution_state"] = (
                        "CLOSE_DEFERRED_MARKET_CLOSED"
                    )
                    mirror["close_deferred_reason"] = (
                        f"IG market status is {market_status}"
                    )
                    mirror["close_deferred_at"] = time.time()
                    mirror["last_market_status_at_close_check"] = (
                        market_status
                    )
                    # Clear the old weekend rejection once the current IG
                    # snapshot itself confirms the market is unavailable.
                    old_error = str(
                        mirror.get("last_close_error")
                        or ""
                    )
                    if (
                        "MARKET_CLOSED_WITH_EDITS"
                        in old_error.upper()
                    ):
                        mirror["last_close_error"] = None
                    old_last_error = str(
                        mirror.get("last_error")
                        or ""
                    )
                    if (
                        "MARKET_CLOSED_WITH_EDITS"
                        in old_last_error.upper()
                    ):
                        mirror["last_error"] = None
                    continue

                mirror["close_execution_state"] = (
                    "CLOSE_READY"
                )
                mirror["close_deferred_reason"] = None

                try:
                    result = (
                        self.broker.close_position(
                            deal_id
                        )
                    )

                    if bool(
                        result.get("closeDeferred")
                    ):
                        mirror["close_execution_state"] = (
                            "CLOSE_DEFERRED_MARKET_CLOSED"
                        )
                        mirror["market_status"] = (
                            result.get("marketStatus")
                            or mirror.get("market_status")
                        )
                        mirror["close_deferred_reason"] = (
                            result.get("deferredReason")
                            or (
                                "IG market is not currently open "
                                "for position closing"
                            )
                        )
                        mirror["close_deferred_at"] = (
                            time.time()
                        )
                        mirror["last_close_error"] = None
                        mirror["last_error"] = None
                        continue

                    mirror["close_attempts"] = int(
                        mirror.get("close_attempts")
                        or 0
                    ) + 1
                    mirror["close_requested_at"] = (
                        time.time()
                    )

                    mirror[
                        "close_result"
                    ] = result
                    mirror[
                        "remaining_size"
                    ] = result.get(
                        "remainingSize"
                    )
                    mirror[
                        "close_verified"
                    ] = bool(
                        result.get(
                            "closeVerified"
                        )
                    )

                    if mirror["close_verified"]:
                        mirror[
                            "broker_status"
                        ] = "CLOSED"
                        mirror["closed_at"] = (
                            time.time()
                        )
                    else:
                        # Accepted/submitted is not the same as broker-verified
                        # disappearance. The next normal reconcile is authoritative.
                        mirror[
                            "broker_status"
                        ] = "CLOSE_PENDING"

                    mirror["last_error"] = None

                    if trade is not None:
                        mirror[
                            "internal_result"
                        ] = trade.get(
                            "result"
                        )
                        mirror[
                            "internal_pnl"
                        ] = trade.get(
                            "pnl"
                        )

                    self._record_broker_outcome(
                        mirror,
                        result,
                    )
                except Exception as exc:
                    mirror["last_close_error"] = (
                        f"{type(exc).__name__}: "
                        f"{exc}"
                    )
                    mirror["last_error"] = (
                        f"close: "
                        f"{type(exc).__name__}: "
                        f"{exc}"
                    )

            # Autotrade-disabled mode still reconciles and closes bot-owned
            # positions, but does not create new ones.
            if self.enabled and not self.phase_complete():
                for trade in trades:
                    if self.phase_complete():
                        break
                    if (
                        self._open_count()
                        >= self.max_open_positions
                    ):
                        break

                    trade_id = str(
                        trade.get("trade_id")
                        or ""
                    )
                    if not trade_id:
                        continue

                    trade_status = str(
                        trade.get("status")
                        or ""
                    ).upper()
                    if trade_status != "OPEN":
                        continue

                    existing = (
                        self._mirrors().get(
                            trade_id
                        )
                    )

                    if existing is not None:
                        broker_status = str(
                            existing.get(
                                "broker_status"
                            )
                            or ""
                        ).upper()

                        if broker_status in {
                            "OPEN",
                            "CLOSED",
                            "CLOSED_EXTERNALLY",
                            "REJECTED",
                            "SUBMITTING",
                        }:
                            continue

                        last_error = str(
                            existing.get(
                                "last_error"
                            )
                            or ""
                        )
                        attempts = int(
                            existing.get(
                                "open_attempts"
                            )
                            or 0
                        )

                        if (
                            broker_status
                            == "ERROR"
                            and self._retryable_open_error(
                                last_error
                            )
                            and attempts
                            < self.max_open_retries
                        ):
                            existing[
                                "broker_status"
                            ] = "RETRY_WAIT"
                            existing[
                                "next_retry_at"
                            ] = time.time()

                        if str(
                            existing.get(
                                "broker_status"
                            )
                            or ""
                        ).upper() != "RETRY_WAIT":
                            continue

                        try:
                            next_retry = float(
                                existing.get(
                                    "next_retry_at"
                                )
                                or 0.0
                            )
                        except Exception:
                            next_retry = 0.0

                        if (
                            time.time()
                            < next_retry
                        ):
                            continue

                        self._submit_open(
                            trade,
                            existing,
                        )
                        continue

                    mirror = {
                        "trade_id": trade_id,
                        "symbol":
                            trade.get(
                                "symbol"
                            )
                            or trade.get(
                                "market"
                            ),
                        "direction":
                            str(
                                trade.get(
                                    "direction"
                                )
                                or ""
                            ).upper(),
                        "entry_class":
                            trade.get(
                                "entry_class"
                            ),
                        "historical_grade":
                            trade.get(
                                "historical_grade"
                            ),
                        "model_ai_confidence":
                            trade.get(
                                "model_ai_confidence"
                            ),
                        "quant_confidence":
                            trade.get(
                                "quant_confidence"
                            ),
                        "entry_normal_pass":
                            trade.get(
                                "entry_normal_pass"
                            ),
                        "entry_ai_pass":
                            trade.get(
                                "entry_ai_pass"
                            ),
                        "model_reason":
                            trade.get(
                                "reason"
                            ),
                        "smart_fast_score":
                            trade.get(
                                "smart_fast_score"
                            ),
                        "quality_tier":
                            trade.get(
                                "quality_tier"
                            ),
                        "deep_status":
                            trade.get(
                                "deep_status"
                            ),
                        "historical_win_rate":
                            trade.get(
                                "historical_win_rate"
                            ),
                        "historical_profit_factor":
                            trade.get(
                                "historical_profit_factor"
                            ),
                        "historical_trades":
                            trade.get(
                                "historical_trades"
                            ),
                        "entry_rsi":
                            trade.get(
                                "entry_rsi"
                            ),
                        "entry_ai_up":
                            trade.get(
                                "entry_ai_up"
                            ),
                        "internal_entry_price":
                            trade.get(
                                "entry_price"
                            ),
                        "scheduled_close_at":
                            trade.get(
                                "scheduled_close_at"
                            ),
                        "created_at":
                            time.time(),
                        "broker_status":
                            "NEW",
                        "environment":
                            "DEMO",
                        "live_money_execution":
                            False,
                        "open_attempts":
                            0,
                        "next_retry_at":
                            None,
                        "close_attempts":
                            0,
                        "close_requested_at":
                            None,
                        "close_verified":
                            False,
                        "remaining_size":
                            None,
                        "mfe_bps":
                            0.0,
                        "mae_bps":
                            0.0,
                    }
                    self._mirrors()[
                        trade_id
                    ] = mirror
                    self._persist()
                    self._submit_open(
                        trade,
                        mirror,
                    )

            # Re-read positions after a trade/close cycle only when the broker
            # snapshot is older than one poll. This keeps the app close to IG
            # without exceeding IG's non-trading REST quota.
            with self._lock:
                self._state["last_error"] = None
                self._persist()

            return self.status()
        finally:
            self._sync_lock.release()
