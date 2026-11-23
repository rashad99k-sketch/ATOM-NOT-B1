"""Thread-safe, read-only account snapshot for dashboard requests."""
from __future__ import annotations

import threading
import time


class DashboardAccountSnapshot:
    def __init__(self):
        self._lock = threading.RLock()
        self._data = {
            "balance": None,
            "free_balance": None,
            "source": "BINGX_FUTURES",
            "status": "NOT_STARTED",
            "updated_at": None,
            "last_attempt_at": None,
            "last_error": None,
        }

    def update(self, balance, free_balance, *, source="BINGX_FUTURES", now=None):
        timestamp = time.time() if now is None else float(now)
        with self._lock:
            self._data.update({
                "balance": balance,
                "free_balance": free_balance,
                "source": str(source),
                "status": "VALID" if balance is not None and free_balance is not None else "PARTIAL",
                "updated_at": timestamp,
                "last_attempt_at": timestamp,
                "last_error": None,
            })
            return dict(self._data)

    def mark_error(self, error, *, now=None):
        timestamp = time.time() if now is None else float(now)
        with self._lock:
            has_snapshot = self._data.get("updated_at") is not None
            self._data["status"] = "STALE" if has_snapshot else "ERROR"
            self._data["last_attempt_at"] = timestamp
            self._data["last_error"] = str(error)[:240]
            return dict(self._data)

    def snapshot(self, *, now=None, stale_after=45.0):
        timestamp = time.time() if now is None else float(now)
        with self._lock:
            result = dict(self._data)
        updated = result.get("updated_at")
        age = max(0.0, timestamp - float(updated)) if updated is not None else None
        if age is not None and age > max(0.0, float(stale_after)) and result.get("status") in {"VALID", "PARTIAL"}:
            result["status"] = "STALE"
        result["age_seconds"] = round(age, 2) if age is not None else None
        result["fresh"] = age is not None and age <= max(0.0, float(stale_after))
        return result
