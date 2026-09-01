from __future__ import annotations

import copy
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class MobileSyncCache:
    """Build one cheap, coherent mobile snapshot in the background.

    HTTP requests only read the last completed snapshot. Expensive category
    ranking and forward-metric work is performed by this server-side worker,
    never serially on Android resume.
    """

    VERSION = "6.10-xau-mobile-sync"
    CATEGORIES = ("METALS",)

    def __init__(
        self,
        *,
        intelligence: Any,
        portfolio: Any,
        forward_prime: Any,
        compound_engine: Any,
        market_data: Any,
        excursion_tracker: Any,
        legacy_evidence: Optional[Any] = None,
    ) -> None:
        self.intelligence = intelligence
        self.portfolio = portfolio
        self.forward_prime = forward_prime
        self.compound_engine = compound_engine
        self.market_data = market_data
        self.excursion_tracker = excursion_tracker
        self.legacy_evidence = legacy_evidence
        self.refresh_seconds = max(
            5,
            min(60, int(os.getenv("MOBILE_SYNC_BUILD_SECONDS", "10"))),
        )
        self._lock = threading.RLock()
        self._build_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._revision = 0
        base_dir = "/var/data" if Path("/var/data").exists() else "/tmp"
        self.state_path = Path(
            os.getenv(
                "MOBILE_SYNC_STATE_PATH",
                f"{base_dir}/jasong_mobile_sync_snapshot.json",
            )
        )
        self._snapshot: Dict[str, Any] = {
            "version": self.VERSION,
            "ready": False,
            "revision": 0,
            "built_at": None,
            "last_error": None,
            "live_money_execution": False,
        }
        self._load_snapshot()

    @staticmethod
    def _safe_copy(value: Any, fallback: Any) -> Any:
        try:
            return copy.deepcopy(value)
        except Exception:
            return fallback

    def _load_snapshot(self) -> None:
        try:
            if not self.state_path.exists():
                return
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            raw["version"] = self.VERSION
            raw["restored_from_disk"] = True
            self._snapshot = raw
            self._revision = int(raw.get("revision") or 0)
        except Exception as exc:
            self._snapshot["last_error"] = (
                f"snapshot load: {type(exc).__name__}: {exc}"
            )

    def _persist_snapshot(self, snapshot: Dict[str, Any]) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(snapshot, separators=(",", ":"), default=str),
                encoding="utf-8",
            )
            tmp.replace(self.state_path)
        except Exception:
            # Mobile cache persistence must never affect trading/runtime work.
            pass

    def _portfolio_status(
        self,
        positions: List[Dict[str, Any]],
        excursions: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        del positions, excursions
        try:
            status = self._safe_copy(self.portfolio.status(), {})
        except Exception:
            status = {}
        if not isinstance(status, dict):
            status = {}
        status["status_source"] = "CACHED_INTERNAL_STATE"
        status["live_money_execution"] = False
        return status

    def _compound_cached_status(self) -> Dict[str, Any]:
        lock = getattr(self.compound_engine, "_lock", None)
        if lock is not None:
            try:
                lock.acquire()
            except Exception:
                lock = None
        try:
            raw = self._safe_copy(
                getattr(self.compound_engine, "_state", {}) or {},
                {},
            )
        finally:
            if lock is not None:
                try:
                    lock.release()
                except Exception:
                    pass

        current = raw.get("current_cycle")
        positions = []
        if isinstance(current, dict) and isinstance(current.get("positions"), list):
            positions = [
                self.excursion_tracker.merge(dict(row))
                for row in current.get("positions") or []
                if isinstance(row, dict)
            ]
            current["positions"] = positions

        raw["compound_broker_positions"] = positions
        raw["compound_max_positions"] = int(
            getattr(self.compound_engine, "max_positions", 5)
        )
        raw["pending_elite_count"] = len(
            raw.get("pending_elite_candidates") or []
        )
        raw["intelligence_bridge_state"] = raw.get(
            "intelligence_bridge_state", "IDLE"
        )
        raw["snapshot_source"] = "CACHED_COMPOUND_STATE"
        raw["live_money_execution"] = False
        return raw

    def _legacy_cached_status(self) -> Dict[str, Any]:
        if self.legacy_evidence is None:
            return {}
        lock = getattr(self.legacy_evidence, "_lock", None)
        if lock is not None:
            try:
                lock.acquire()
            except Exception:
                lock = None
        try:
            raw = self._safe_copy(
                getattr(self.legacy_evidence, "_state", {}) or {},
                {},
            )
        finally:
            if lock is not None:
                try:
                    lock.release()
                except Exception:
                    pass
        raw["snapshot_source"] = "CACHED_IG_MIRROR_STATE"
        raw["live_money_execution"] = False
        return raw

    def _forward_status(
        self,
        rankings: Dict[str, List[Dict[str, Any]]],
        trades: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        strategies = sorted(
            {
                str(row.get("strategy_id") or row.get("selected_strategy") or "UNKNOWN")
                for bucket in rankings.values()
                for row in bucket
                if row.get("strategy_id") or row.get("selected_strategy")
            }
        )
        metrics: Dict[str, Any] = {}
        for strategy in strategies:
            try:
                metrics[strategy] = self.forward_prime.validator.metrics(
                    strategy_id=strategy,
                    sync=False,
                )
            except Exception:
                continue
        cfg = self.forward_prime.validator.config
        prime_count = sum(
            1
            for bucket in rankings.values()
            for row in bucket
            if row.get("prime_qualified")
        )
        strong_count = sum(
            1
            for bucket in rankings.values()
            for row in bucket
            if row.get("strong_qualified")
        )
        return {
            "version": getattr(self.forward_prime, "VERSION", "6.9.4-forward"),
            "authority": "BROKER_SETTLED_FORWARD_ONLY",
            "mode": "FORWARD_VALIDATED" if prime_count else "FORWARD_BOOTSTRAP",
            "historical_validation": {
                "mode": "INFORMATIONAL_ONLY",
                "execution_veto": False,
            },
            "bootstrap_lane": "STRONG -> controlled IG DEMO -> settled forward evidence -> PRIME",
            "thresholds": {
                "min_settled_trades_for_prime": cfg.min_settled_trades_for_prime,
                "rolling_window_trades": cfg.rolling_window_trades,
                "min_profit_factor": cfg.min_profit_factor,
                "min_expectancy_r": cfg.min_expectancy_r,
                "min_win_rate": cfg.min_win_rate,
                "min_bootstrap_prob_positive_expectancy": cfg.min_bootstrap_prob_positive_expectancy,
                "max_drawdown_r": cfg.max_drawdown_r,
            },
            "strategy_metrics": metrics,
            "stored_settled_trades": len(trades),
            "strong_candidates": strong_count,
            "prime_candidates": prime_count,
            "live_money_execution": False,
        }

    def build(self) -> Dict[str, Any]:
        if not self._build_lock.acquire(blocking=False):
            return self.snapshot()
        try:
            built_at = time.time()
            rankings = self.forward_prime.category_rankings()
            positions = [
                self.excursion_tracker.merge(dict(row))
                for row in (self.portfolio.positions(limit=500) or [])
                if isinstance(row, dict)
            ]
            trades = [
                self.excursion_tracker.merge(dict(row))
                for row in self.forward_prime.validator.all_rows(limit=300)
                if isinstance(row, dict)
            ]
            excursions = self.excursion_tracker.rows(limit=1000)
            data_health = self.market_data.status()
            forward_status = self._forward_status(rankings, trades)
            portfolio_status = self._portfolio_status(positions, excursions)

            compound_status = self._compound_cached_status()
            legacy = self._legacy_cached_status()

            self._revision += 1
            snapshot = {
                "version": self.VERSION,
                "ready": True,
                "revision": self._revision,
                "built_at": built_at,
                "server_time": time.time(),
                "refresh_seconds": self.refresh_seconds,
                "category_rankings": rankings,
                "category_portfolio_status": portfolio_status,
                "category_portfolio_positions": positions,
                "forward_validation_status": forward_status,
                "forward_validation_trades": trades,
                "market_data_health": data_health,
                "trade_excursion_status": self.excursion_tracker.status(),
                "trade_excursions": excursions,
                "compound_status": compound_status,
                "legacy_ig_demo_status": legacy,
                "last_error": None,
                "live_money_execution": False,
            }
            with self._lock:
                self._snapshot = snapshot
            self._persist_snapshot(snapshot)
            return self.snapshot()
        except Exception as exc:
            with self._lock:
                previous = dict(self._snapshot)
                previous["last_error"] = f"{type(exc).__name__}: {exc}"
                previous["last_build_attempt_at"] = time.time()
                self._snapshot = previous
            return self.snapshot()
        finally:
            self._build_lock.release()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            out = self._safe_copy(self._snapshot, {})
        out["served_at"] = time.time()
        built_at = out.get("built_at")
        try:
            out["age_seconds"] = max(0.0, time.time() - float(built_at))
        except Exception:
            out["age_seconds"] = None
        return out

    def start_thread(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="jasong-mobile-sync-cache",
                daemon=True,
            )
            self._thread.start()

    def stop_thread(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.build()
            self._stop.wait(self.refresh_seconds)
