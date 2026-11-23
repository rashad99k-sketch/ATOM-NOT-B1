"""Gunicorn/WSGI entrypoint that also starts the GGP trading supervisor.

Use one Gunicorn worker. HTTP request threads are separate from the single
trading supervisor thread; the OS lock prevents duplicate execution processes.
"""
from __future__ import annotations

import os

import ggpbot
from ggp_runtime_bootstrap import runtime_bootstrap


_LOCK_PATH = os.getenv(
    "GGP_INSTANCE_LOCK_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ggpbot.instance.lock"),
)

runtime_bootstrap.start(
    ggpbot.safe_main_loop,
    ggpbot.MEMORY.setdefault("runtime_supervisor", {}),
    _LOCK_PATH,
)

app = ggpbot.app
