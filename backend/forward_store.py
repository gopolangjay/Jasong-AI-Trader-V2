from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


class ForwardStore:
    """Persistent audit ledger for broker-settled forward trades.

    SQLite is used only as an evidence store; it does not own broker execution.
    Entry-time snapshots and provenance are immutable audit payloads from the
    signal that actually went to IG DEMO.
    """

    def __init__(self, path: str) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS forward_trades (
                    trade_id TEXT PRIMARY KEY,
                    market TEXT,
                    symbol TEXT,
                    strategy_id TEXT,
                    direction TEXT,
                    broker_result TEXT,
                    opened_at REAL,
                    closed_at REAL,
                    entry_level REAL,
                    exit_level REAL,
                    broker_pnl REAL,
                    r_multiple REAL,
                    r_source TEXT,
                    entry_snapshot_json TEXT NOT NULL,
                    provenance_json TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    updated_at REAL NOT NULL DEFAULT (strftime('%s','now'))
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_forward_strategy_closed ON forward_trades(strategy_id, closed_at)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_forward_market_closed ON forward_trades(symbol, closed_at)"
            )
            db.commit()

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value if value is not None else {}, separators=(",", ":"), default=str, sort_keys=True)

    def upsert(self, row: Dict[str, Any]) -> bool:
        trade_id = str(row.get("trade_id") or row.get("ig_deal_id") or row.get("deal_id") or "").strip()
        result = str(row.get("broker_result") or row.get("result") or "").upper().strip()
        if not trade_id or result not in {"WIN", "LOSS"}:
            return False
        values = (
            trade_id,
            row.get("market") or row.get("symbol"),
            row.get("symbol") or row.get("market"),
            row.get("strategy_id") or row.get("selected_strategy") or "UNKNOWN",
            str(row.get("direction") or "").upper(),
            result,
            row.get("opened_at") or row.get("entry_time"),
            row.get("closed_at"),
            row.get("entry_level") or row.get("broker_entry_level"),
            row.get("exit_level") or row.get("broker_exit_level"),
            row.get("broker_pnl"),
            row.get("r_multiple"),
            row.get("r_source"),
            self._json(row.get("entry_snapshot") or row.get("signal_snapshot") or {}),
            self._json(row.get("provenance") or {}),
            self._json(row),
        )
        with self._lock, self._connect() as db:
            db.execute(
                """
                INSERT INTO forward_trades (
                    trade_id, market, symbol, strategy_id, direction, broker_result,
                    opened_at, closed_at, entry_level, exit_level, broker_pnl,
                    r_multiple, r_source, entry_snapshot_json, provenance_json, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_id) DO UPDATE SET
                    market=excluded.market,
                    symbol=excluded.symbol,
                    strategy_id=excluded.strategy_id,
                    direction=excluded.direction,
                    broker_result=excluded.broker_result,
                    opened_at=excluded.opened_at,
                    closed_at=excluded.closed_at,
                    entry_level=excluded.entry_level,
                    exit_level=excluded.exit_level,
                    broker_pnl=excluded.broker_pnl,
                    r_multiple=excluded.r_multiple,
                    r_source=excluded.r_source,
                    provenance_json=excluded.provenance_json,
                    raw_json=excluded.raw_json,
                    updated_at=strftime('%s','now')
                """,
                values,
            )
            db.commit()
        return True

    def sync(self, rows: Iterable[Dict[str, Any]]) -> int:
        count = 0
        for row in rows:
            if isinstance(row, dict) and self.upsert(row):
                count += 1
        return count

    def rows(
        self,
        *,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        where = []
        params: List[Any] = []
        if strategy_id:
            where.append("UPPER(strategy_id)=UPPER(?)")
            params.append(str(strategy_id))
        if symbol:
            where.append("UPPER(REPLACE(REPLACE(REPLACE(symbol,'/',''),'-',''),' ',''))=UPPER(?)")
            params.append("".join(ch for ch in str(symbol).upper() if ch.isalnum()))
        sql = "SELECT * FROM forward_trades"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY COALESCE(closed_at, 0) DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, int(limit)))
        with self._lock, self._connect() as db:
            result = db.execute(sql, params).fetchall()
        output: List[Dict[str, Any]] = []
        for item in result:
            row = dict(item)
            for key in ("entry_snapshot_json", "provenance_json", "raw_json"):
                try:
                    row[key.removesuffix("_json")] = json.loads(row.get(key) or "{}")
                except Exception:
                    row[key.removesuffix("_json")] = {}
            output.append(row)
        return output
