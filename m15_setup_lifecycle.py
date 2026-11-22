"""Runtime-only M15 crossover candidate lifecycle.

The cache is intentionally process-local: no SQLite, disk writes, or restart
recovery. This module tracks candidates only and never authorizes an order.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict

_LOCK = threading.RLock()
_ACTIVE: Dict[str, Dict[str, Any]] = {}
_ACTIVE_TTL_SECONDS = 6 * 60 * 60


def clear_m15_setup_memory():
    with _LOCK:
        _ACTIVE.clear()


def list_active_m15_setups(*, now_ms=None):
    now = float(now_ms if now_ms is not None else time.time() * 1000) / 1000.0
    active = []
    with _LOCK:
        for symbol, item in list(_ACTIVE.items()):
            first_seen = float(item.get("first_seen_ms", 0) or 0) / 1000.0
            stage = str(item.get("stage") or "")
            if stage in {"EXPIRED", "INVALIDATED", "COMPLETED"} or first_seen <= 0 or now - first_seen > _ACTIVE_TTL_SECONDS:
                _ACTIVE.pop(symbol, None)
                continue
            active.append({"symbol": symbol, "stage": stage})
    return active


def advance_m15_setup_lifecycle(row: Dict[str, Any], *, now_ms=None, cycle_id=None):
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    now = now_ms / 1000.0
    symbol = str(row.get("symbol") or "").strip().upper()
    if not symbol:
        return row
    status = str(row.get("crossover_status") or "")
    direction = str(row.get("direction") or "UNKNOWN").upper()
    cycle_id = int(cycle_id if cycle_id is not None else now_ms)

    with _LOCK:
        old = _ACTIVE.get(symbol)
        prior = dict(old) if isinstance(old, dict) else None
        first_seen = float(prior.get("first_seen_ms", 0) or 0) / 1000.0 if prior else 0.0
        active = bool(prior and first_seen > 0 and now - first_seen <= _ACTIVE_TTL_SECONDS
                      and prior.get("stage") not in {"EXPIRED", "INVALIDATED", "COMPLETED"})

        if status == "CROSS_CONFIRMED" and row.get("signal_timestamp_ms"):
            cross_ts = int(row["signal_timestamp_ms"])
            if not active or int(prior.get("cross_timestamp_ms") or 0) != cross_ts:
                prior = {"symbol": symbol, "direction": direction, "cross_timestamp_ms": cross_ts,
                         "stage": "WAITING_FOR_PULLBACK", "first_seen_ms": now_ms,
                         "last_seen_ms": now_ms, "pullback_timestamp_ms": None,
                         "retest_timestamp_ms": None, "rejection_confirmed": False,
                         "cycles_seen": 0, "last_cycle_id": None}
        elif status == "PRE_CROSS":
            if not active:
                prior = {"symbol": symbol, "direction": direction, "cross_timestamp_ms": None,
                         "stage": "CROSS_APPROACHING", "first_seen_ms": now_ms,
                         "last_seen_ms": now_ms, "pullback_timestamp_ms": None,
                         "retest_timestamp_ms": None, "rejection_confirmed": False,
                         "cycles_seen": 0, "last_cycle_id": None}
            elif prior.get("stage") == "CROSS_APPROACHING":
                prior["direction"] = direction
        elif status == "CROSS_DETECTED":
            if not active:
                prior = {"symbol": symbol, "direction": direction, "cross_timestamp_ms": None,
                         "stage": "WAITING_FOR_CANDLE_CLOSE", "first_seen_ms": now_ms,
                         "last_seen_ms": now_ms, "pullback_timestamp_ms": None,
                         "retest_timestamp_ms": None, "rejection_confirmed": False,
                         "cycles_seen": 0, "last_cycle_id": None}
        elif not active:
            row.update({"lifecycle_active": False, "strategy_mode": "UNCLASSIFIED",
                        "entry_authorization": False, "entry_readiness": "NOT_AUTHORIZED_BY_RADAR"})
            return row

        if prior is None:
            row.update({"lifecycle_active": False, "strategy_mode": "UNCLASSIFIED",
                        "entry_authorization": False, "entry_readiness": "NOT_AUTHORIZED_BY_RADAR"})
            return row

        if now - float(prior.get("first_seen_ms", now_ms)) / 1000.0 > _ACTIVE_TTL_SECONDS:
            prior["stage"] = "EXPIRED"
        if prior.get("last_cycle_id") != cycle_id:
            prior["cycles_seen"] = int(prior.get("cycles_seen", 0)) + 1
            prior["last_cycle_id"] = cycle_id

        chart = row.get("chart") if isinstance(row.get("chart"), dict) else {}
        ts = chart.get("timestamps") or []
        highs, lows = chart.get("high") or [], chart.get("low") or []
        opens, closes = chart.get("open") or [], chart.get("close") or []
        e9s, e15s = chart.get("ema9") or [], chart.get("ema15") or []
        start_ts = int(prior.get("cross_timestamp_ms") or prior.get("first_seen_ms") or 0)
        touched_idx = None
        for i in range(min(len(ts), len(highs), len(lows), len(e9s), len(e15s))):
            if int(ts[i]) <= start_ts:
                continue
            try:
                low_band, high_band = sorted((float(e9s[i]), float(e15s[i])))
                if float(lows[i]) <= high_band and float(highs[i]) >= low_band:
                    touched_idx = i
            except (TypeError, ValueError):
                continue
        if touched_idx is not None and prior.get("stage") in {"WAITING_FOR_PULLBACK", "WAITING_FOR_CANDLE_CLOSE"}:
            prior["stage"] = "PULLBACK_DETECTED"
            prior["pullback_timestamp_ms"] = int(ts[touched_idx])

        if (touched_idx is not None and touched_idx < len(closes) - 1
                and len(ts) == len(opens) == len(closes) == len(highs) == len(lows)):
            try:
                o, c, h, low = map(float, (opens[-1], closes[-1], highs[-1], lows[-1]))
                previous_close = float(closes[-2])
                span = max(0.0, h - low)
                lower_wick = (min(o, c) - low) / span if span else 0.0
                upper_wick = (h - max(o, c)) / span if span else 0.0
                prior["rejection_confirmed"] = (
                    c > o and c > previous_close and lower_wick >= 0.10
                    if prior.get("direction") == "BUY" else
                    c < o and c < previous_close and upper_wick >= 0.10
                    if prior.get("direction") == "SELL" else False)
                if prior["rejection_confirmed"] and prior.get("stage") == "PULLBACK_DETECTED":
                    prior["stage"] = "RETEST_CONFIRMED"
                    prior["retest_timestamp_ms"] = int(ts[-1])
            except (TypeError, ValueError):
                prior["rejection_confirmed"] = False

        gap, volume = row.get("ema_distance_atr"), row.get("volume_ratio")
        vwap, price = row.get("session_vwap"), row.get("closed_price", row.get("current_price"))
        try:
            aligned = vwap is not None and price is not None and (
                (prior.get("direction") == "BUY" and float(price) > float(vwap)) or
                (prior.get("direction") == "SELL" and float(price) < float(vwap)))
        except (TypeError, ValueError):
            aligned = False
        if gap is None or volume is None:
            mode, reason = "WAIT_FOR_EVIDENCE", "REGIME_OR_VOLUME_UNAVAILABLE"
        elif float(gap) >= 0.35 and float(volume) >= 1.0 and aligned:
            mode, reason = "TREND_CANDIDATE", "EMA_SEPARATION_VOLUME_AND_VWAP_ALIGNED"
        elif float(gap) <= 0.20 and float(volume) >= 0.70 and aligned:
            mode, reason = "SCALP_CANDIDATE", "WEAK_SEPARATION_BUT_DIRECTIONAL_LIQUIDITY"
        else:
            mode, reason = "NO_TRADE_OR_WAIT", "INSUFFICIENT_TREND_OR_SCALP_QUALITY"
        if prior.get("stage") == "EXPIRED":
            mode, reason = "NO_TRADE_OR_WAIT", "SETUP_LIFETIME_EXPIRED"

        prior.update({"last_seen_ms": now_ms, "strategy_mode": mode, "strategy_mode_reason": reason})
        _ACTIVE[symbol] = dict(prior)

    row.update({
        "lifecycle_active": prior.get("stage") not in {"EXPIRED", "INVALIDATED", "COMPLETED"},
        "lifecycle_stage": prior.get("stage"), "lifecycle_direction": prior.get("direction"),
        "lifecycle_cross_timestamp_ms": prior.get("cross_timestamp_ms"),
        "lifecycle_pullback_timestamp_ms": prior.get("pullback_timestamp_ms"),
        "lifecycle_retest_timestamp_ms": prior.get("retest_timestamp_ms"),
        "lifecycle_rejection_confirmed": bool(prior.get("rejection_confirmed")),
        "strategy_mode": mode, "strategy_mode_reason": reason,
        "lifecycle_persisted": False, "lifecycle_cycles_seen": int(prior.get("cycles_seen", 0)),
        "lifecycle_minimum_retention_met": int(prior.get("cycles_seen", 0)) >= 2,
        "entry_authorization": False, "entry_readiness": "NOT_AUTHORIZED_BY_RADAR"})
    return row
