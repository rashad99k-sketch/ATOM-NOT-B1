"""Single-process runtime bootstrap shared by WSGI and direct GGP launches.

The lock is held for the lifetime of the supervisor thread/process. A second
web worker may serve HTTP, but it cannot start a second trading engine.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Dict, Optional

from ggp_instance_lock import acquire_instance_lock


class RuntimeBootstrap:
    def __init__(self, lock_factory: Callable[[str], Any] = acquire_instance_lock):
        self._lock_factory = lock_factory
        self._guard = threading.RLock()
        self._lock_handle: Optional[Any] = None
        self._thread: Optional[threading.Thread] = None

    def start(self, target: Callable[[], Any], state: Dict[str, Any], lock_path: str) -> bool:
        """Start the supervisor once; fail closed if another process owns execution."""
        with self._guard:
            if self._thread is not None and self._thread.is_alive():
                state.update({"status": "RUNNING", "thread_alive": True})
                return True
            if self._lock_handle is not None:
                state.update({
                    "status": "FAILED",
                    "thread_alive": False,
                    "last_error": "SUPERVISOR_EXITED_LOCK_RETAINED_PROCESS_RESTART_REQUIRED",
                })
                return False

            try:
                handle = self._lock_factory(os.path.abspath(lock_path))
            except Exception as exc:
                state.update({
                    "status": "FAILED",
                    "launch_mode": "wsgi",
                    "started_at": time.time(),
                    "heartbeat_ts": None,
                    "thread_alive": False,
                    "last_error": f"RUNTIME_LOCK_ACQUISITION_FAILED:{type(exc).__name__}:{str(exc)[:200]}",
                })
                return False
            if handle is None:
                state.update({
                    "status": "LOCKED_BY_OTHER_PROCESS",
                    "launch_mode": "wsgi",
                    "started_at": time.time(),
                    "heartbeat_ts": None,
                    "thread_alive": False,
                    "last_error": "ANOTHER_PROCESS_OWNS_GGP_RUNTIME_LOCK",
                })
                return False

            self._lock_handle = handle
            state.update({
                "status": "STARTING",
                "launch_mode": "wsgi",
                "started_at": time.time(),
                "heartbeat_ts": None,
                "loop_cycles": 0,
                "thread_alive": True,
                "last_error": None,
                "pid": os.getpid(),
            })
            self._thread = threading.Thread(
                target=self._run,
                args=(target, state),
                name="ggp_trading_supervisor",
                daemon=True,
            )
            try:
                self._thread.start()
            except Exception as exc:
                state.update({
                    "status": "FAILED",
                    "thread_alive": False,
                    "last_error": f"SUPERVISOR_THREAD_START_FAILED:{type(exc).__name__}:{exc}",
                })
                try:
                    self._lock_handle.close()
                except Exception:
                    pass
                self._lock_handle = None
                self._thread = None
                return False
            return True

    @staticmethod
    def _run(target: Callable[[], Any], state: Dict[str, Any]) -> None:
        state.update({"status": "RUNNING", "thread_alive": True, "supervisor_entered_at": time.time()})
        try:
            target()
            state.update({
                "status": "STOPPED_UNEXPECTEDLY",
                "thread_alive": False,
                "last_error": "SUPERVISOR_RETURNED_WITHOUT_SHUTDOWN_REQUEST",
            })
        except BaseException as exc:
            state.update({
                "status": "FAILED",
                "thread_alive": False,
                "last_error": f"{type(exc).__name__}:{str(exc)[:240]}",
            })

    def is_alive(self) -> bool:
        with self._guard:
            return bool(self._thread and self._thread.is_alive())


runtime_bootstrap = RuntimeBootstrap()
