from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict


class PersistentStateStore:
    """Small atomic JSON state store for V6.1 runtime recovery."""

    def __init__(self, path: str = "data/jasong_v61_state.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _read_all(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def load(self, namespace: str, default: Any = None) -> Any:
        with self._lock:
            return self._read_all().get(namespace, default)

    def save(self, namespace: str, value: Any) -> None:
        with self._lock:
            data = self._read_all()
            data[namespace] = value
            data["_meta"] = {
                "version": "6.1.0",
                "updated_at": time.time(),
            }
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=True, sort_keys=True, indent=2, default=str),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return self._read_all()
