"""Per-key single-flight coordination for shared market-data cache misses.

This prevents concurrent scanner/watchlist/entry workers from issuing duplicate
OHLCV reads for the same cache key. It does not change request pacing, cache TTL,
data validation, or retry/backoff policy.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Tuple


class KeyedSingleFlight:
    """Coalesce concurrent cache misses using a bounded stripe-lock pool."""

    def __init__(self, stripes: int = 64):
        count = max(1, int(stripes))
        self._locks = tuple(threading.Lock() for _ in range(count))

    def get_or_fetch(
        self,
        key: Any,
        *,
        get_cached: Callable[[], Any],
        fetch: Callable[[], Any],
        is_valid: Callable[[Any], bool],
        set_cached: Callable[[Any], None],
    ) -> Tuple[Any, str]:
        cached = get_cached()
        if is_valid(cached):
            return cached, "CACHE_HIT"

        lock = self._locks[hash(key) % len(self._locks)]
        with lock:
            # Another worker may have populated the cache while this worker waited.
            cached = get_cached()
            if is_valid(cached):
                return cached, "CACHE_HIT"

            value = fetch()
            if not is_valid(value):
                return None, "NONE"
            set_cached(value)
            return value, "FRESH_REQUEST"
