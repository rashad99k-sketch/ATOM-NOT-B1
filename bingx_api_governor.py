"""Central, priority-aware request governor for the shared BingX CCXT client.

All network-facing CCXT methods exposed by GovernedExchange are scheduled here.
This module deliberately does not cache exchange responses or retry writes.
"""
from __future__ import annotations

import collections
import functools
import re
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple


class APIGovernorError(RuntimeError):
    """A request was rejected locally because the API is cooling down."""

    def __init__(self, category: str, retry_after: float, reason: str = "cooldown"):
        self.category = str(category)
        self.retry_after = max(0.0, float(retry_after))
        self.code = 429
        super().__init__(
            f"BingX API governor HTTP 429 local cooldown ({reason}): category={self.category} "
            f"retry_after={self.retry_after:.2f}s"
        )


class BingXAPIGovernor:
    """One scheduler and request budget shared by every exchange consumer.

    Priority: order/protection (0), account reconciliation (1), market data (2),
    background/other reads (3). Defaults are intentionally conservative.
    """

    DEFAULT_INTERVALS = {
        "market_kline": 1.05,       # one Kline request per second, with headroom
        "market_data": 0.05,        # <= 20 requests/second across ticker/orderbook reads
        "account_balance": 0.25,    # <= 4 requests/second (< published 5/s endpoint cap)
        "account_positions": 0.25,
        "account_orders": 0.25,
        "account_trades": 0.25,
        "account_config": 0.25,
        "order_write": 0.25,        # CCXT/BingX order endpoint budget; no automatic retry
        "leverage_write": 0.55,
        "generic": 0.35,
    }

    def __init__(
        self,
        *,
        min_intervals: Optional[Dict[str, float]] = None,
        max_concurrent: int = 4,
        cooldown_429: float = 30.0,
        cooldown_418: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.intervals = dict(self.DEFAULT_INTERVALS)
        if min_intervals:
            for key, value in min_intervals.items():
                self.intervals[str(key)] = max(0.0, float(value))
        self.max_concurrent = max(1, int(max_concurrent))
        self.cooldown_429 = max(0.0, float(cooldown_429))
        self.cooldown_418 = max(0.0, float(cooldown_418))
        self._clock = clock
        self._sleep = sleeper
        self._condition = threading.Condition(threading.RLock())
        self._queue = []
        self._ticket = 0
        self._active = 0
        self._next_allowed = collections.defaultdict(float)
        self._cooldown_until = {}
        self._global_cooldown_until = 0.0
        self._metrics_lock = threading.RLock()
        self._metrics = collections.defaultdict(
            lambda: {"attempts": 0, "success": 0, "errors": 0,
                     "rate_limited": 0, "total_latency_ms": 0.0,
                     "max_latency_ms": 0.0, "queue_wait_ms": 0.0}
        )
        self._last_error = None

    @staticmethod
    def classify(method_name: str) -> Tuple[str, int]:
        name = str(method_name or "").lower()
        if name == "fetch_ohlcv" or "klines" in name or "kline" in name:
            return "market_kline", 2
        if name in {"create_order", "create_market_order", "create_limit_order",
                    "cancel_order", "cancel_all_orders", "edit_order"}:
            return "order_write", 0
        if "set_leverage" in name:
            return "leverage_write", 0
        if name in {"fetch_open_orders", "fetch_closed_orders", "fetch_orders",
                    "fetch_order", "fetch_order_book", "fetch_order_books"}:
            if name.startswith("fetch_order_book"):
                return "market_data", 2
            return "account_orders", 0
        if name in {"fetch_positions", "fetch_position", "fetch_open_positions"}:
            return "account_positions", 0
        if name in {"fetch_balance"}:
            return "account_balance", 1
        if name in {"fetch_my_trades", "fetch_trades", "fetch_closed_trades"}:
            return "account_trades", 1
        if name in {"fetch_position_mode", "fetch_leverage", "fetch_margin_mode"}:
            return "account_config", 1
        if name in {"fetch_ticker", "fetch_tickers", "fetch_bids_asks",
                    "fetch_ohlcv", "fetch_funding_rate", "fetch_funding_rates",
                    "fetch_mark_price", "fetch_mark_prices"}:
            return "market_data", 2
        if name == "load_markets":
            return "account_config", 3
        # CCXT exposes generated BingX private endpoint methods dynamically.
        if name.startswith(("swapv2", "contractv1", "spotv1", "private")):
            if any(token in name for token in ("posttradeorder", "delete", "cancel", "posttrade")):
                return "order_write", 0
            if "openorders" in name or "allorders" in name or "order" in name:
                return "account_orders", 0
            if "position" in name:
                return "account_positions", 1
            if "balance" in name:
                return "account_balance", 1
            return "generic", 3
        if name.startswith(("fetch_", "create_", "cancel_", "set_", "edit_")):
            return "generic", 3
        return "generic", 3

    def _cooldown_remaining(self, category: str, now: float) -> float:
        return max(
            self._global_cooldown_until - now,
            self._cooldown_until.get(category, 0.0) - now,
            0.0,
        )

    def _record_start(self, category: str, queue_wait_ms: float) -> None:
        with self._metrics_lock:
            row = self._metrics[category]
            row["attempts"] += 1
            row["queue_wait_ms"] += max(0.0, queue_wait_ms)

    def _record_finish(self, category: str, started: float, error: Optional[BaseException]) -> None:
        elapsed_ms = max(0.0, (self._clock() - started) * 1000.0)
        with self._metrics_lock:
            row = self._metrics[category]
            row["total_latency_ms"] += elapsed_ms
            row["max_latency_ms"] = max(row["max_latency_ms"], elapsed_ms)
            if error is None:
                row["success"] += 1
            else:
                row["errors"] += 1
                text = str(error).lower()
                if any(x in text for x in ("429", "418", "rate limit", "100410")):
                    row["rate_limited"] += 1
                self._last_error = {
                    "category": category, "type": type(error).__name__,
                    "message": str(error)[:240], "at": time.time(),
                }

    @staticmethod
    def _rate_limit_kind(error: BaseException) -> Optional[str]:
        text = str(error).lower()
        code = getattr(error, "code", None) or getattr(error, "status", None)
        if str(code) == "418" or "418" in text or "ip banned" in text:
            return "418"
        if str(code) == "429" or "429" in text or "rate limit" in text or "100410" in text:
            return "429"
        return None

    def _apply_cooldown(self, category: str, kind: str) -> None:
        now = self._clock()
        with self._condition:
            if kind == "418":
                self._global_cooldown_until = max(
                    self._global_cooldown_until, now + self.cooldown_418
                )
            else:
                self._cooldown_until[category] = max(
                    self._cooldown_until.get(category, 0.0), now + self.cooldown_429
                )
            self._condition.notify_all()

    def _enter(self, category: str, priority: int) -> float:
        queued_at = self._clock()
        with self._condition:
            remaining = self._cooldown_remaining(category, self._clock())
            if remaining > 0:
                raise APIGovernorError(category, remaining)
            self._ticket += 1
            token = (int(priority), self._ticket, category)
            self._queue.append(token)
            try:
                while True:
                    now = self._clock()
                    remaining = self._cooldown_remaining(category, now)
                    if remaining > 0:
                        raise APIGovernorError(category, remaining)
                    first = min(self._queue, key=lambda item: (item[0], item[1]))
                    interval_wait = max(0.0, self._next_allowed[category] - now)
                    if first == token and self._active < self.max_concurrent and interval_wait <= 0:
                        self._queue.remove(token)
                        self._active += 1
                        self._next_allowed[category] = now + self.intervals.get(category, self.intervals["generic"])
                        self._record_start(category, (now - queued_at) * 1000.0)
                        return now
                    self._condition.wait(timeout=min(0.25, interval_wait) if interval_wait > 0 else 0.25)
            finally:
                if token in self._queue:
                    self._queue.remove(token)
                    self._condition.notify_all()

    def _leave(self) -> None:
        with self._condition:
            self._active = max(0, self._active - 1)
            self._condition.notify_all()

    def call(self, method_name: str, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        category, priority = self.classify(method_name)
        started = self._enter(category, priority)
        error = None
        try:
            return function(*args, **kwargs)
        except BaseException as exc:
            error = exc
            kind = self._rate_limit_kind(exc)
            if kind:
                self._apply_cooldown(category, kind)
            raise
        finally:
            self._record_finish(category, started, error)
            self._leave()

    def snapshot(self) -> Dict[str, Any]:
        now = self._clock()
        with self._metrics_lock:
            metrics = {key: dict(value) for key, value in self._metrics.items()}
            last_error = dict(self._last_error) if self._last_error else None
        with self._condition:
            cooldowns = {
                key: round(max(0.0, value - now), 3)
                for key, value in self._cooldown_until.items() if value > now
            }
            global_cooldown = round(max(0.0, self._global_cooldown_until - now), 3)
            active, queued = self._active, len(self._queue)
        totals = {key: sum(row[key] for row in metrics.values())
                  for key in ("attempts", "success", "errors", "rate_limited")}
        return {
            "active": active, "queued": queued, "max_concurrent": self.max_concurrent,
            "totals": totals, "categories": metrics, "cooldowns": cooldowns,
            "global_cooldown_seconds": global_cooldown, "last_error": last_error,
            "policy": dict(self.intervals),
        }


class GovernedExchange:
    """Transparent CCXT proxy; all network-facing methods use one governor."""

    _NETWORK_PREFIXES = (
        "fetch_", "create_", "cancel_", "set_", "edit_", "swapv2",
        "contractv1", "spotv1", "private",
    )
    _LOCAL_METHODS = {
        "market", "markets_by_id", "safe_currency_code", "safe_symbol",
        "amount_to_precision", "price_to_precision", "cost_to_precision",
        "currency_to_precision", "parse8601", "iso8601", "milliseconds",
        "nonce", "decimal_to_precision",
    }

    def __init__(self, exchange: Any, governor: BingXAPIGovernor):
        self._exchange = exchange
        self._governor = governor

    @property
    def raw_exchange(self) -> Any:
        """Escape hatch for diagnostics only; application code must use the proxy."""
        return self._exchange

    @property
    def governor(self) -> BingXAPIGovernor:
        return self._governor

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._exchange, name)
        lowered = name.lower()
        if (name in self._LOCAL_METHODS or not callable(value)
                or not (lowered.startswith(self._NETWORK_PREFIXES) or lowered == "load_markets")):
            return value

        @functools.wraps(value)
        def governed(*args: Any, **kwargs: Any) -> Any:
            return self._governor.call(name, value, *args, **kwargs)

        governed.__name__ = name
        return governed

    def __repr__(self) -> str:
        return f"GovernedExchange({self._exchange!r})"
