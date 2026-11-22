"""Runtime-only M15 crossover candidate lifecycle.

This module retains M15 observations in process memory only. It never grants
entry authority and never submits, modifies, or closes an order.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict

_LOCK = threading.RLock()
_ACTIVE: Dict[str, Dict[str, Any]] = {}
_ACTIVE_TTL_SECONDS = 6 * 60 * 60
_TERMINAL_STAGES = {"EXPIRED", "INVALIDATED", "COMPLETED"}


def clear_m15_setup_memory():
    with _LOCK:
        _ACTIVE.clear()


def _number(value, default=None):
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else default
    except (TypeError, ValueError, OverflowError):
        return default


def _new_setup(symbol, direction, now_ms, stage, cross_timestamp_ms=None):
    return {
        "symbol": symbol, "direction": direction if direction in {"BUY", "SELL"} else "UNKNOWN",
        "cross_timestamp_ms": cross_timestamp_ms, "stage": stage,
        "first_seen_ms": now_ms, "last_seen_ms": now_ms,
        "pullback_timestamp_ms": None, "pullback_hold_timestamp_ms": None,
        "rejection_timestamp_ms": None, "trigger_timestamp_ms": None,
        "rejection_high": None, "rejection_low": None, "rejection_confirmed": False,
        "cycles_seen": 0, "last_cycle_id": None,
    }


def _public_row(row, prior, mode, reason):
    row.update({
        "lifecycle_active": prior.get("stage") not in _TERMINAL_STAGES,
        "lifecycle_stage": prior.get("stage"),
        "lifecycle_direction": prior.get("direction"),
        "lifecycle_cross_timestamp_ms": prior.get("cross_timestamp_ms"),
        "lifecycle_pullback_timestamp_ms": prior.get("pullback_timestamp_ms"),
        "lifecycle_pullback_hold_timestamp_ms": prior.get("pullback_hold_timestamp_ms"),
        "lifecycle_retest_timestamp_ms": prior.get("rejection_timestamp_ms"),
        "lifecycle_trigger_timestamp_ms": prior.get("trigger_timestamp_ms"),
        "lifecycle_rejection_confirmed": bool(prior.get("rejection_confirmed")),
        "strategy_mode": mode, "strategy_mode_reason": reason,
        "lifecycle_persisted": False, "lifecycle_cycles_seen": int(prior.get("cycles_seen", 0)),
        "lifecycle_minimum_retention_met": int(prior.get("cycles_seen", 0)) >= 2,
        "entry_authorization": False, "entry_readiness": "NOT_AUTHORIZED_BY_RADAR",
    })
    return row


def list_active_m15_setups(*, now_ms=None):
    now = float(now_ms if now_ms is not None else time.time() * 1000) / 1000.0
    active = []
    with _LOCK:
        for symbol, item in list(_ACTIVE.items()):
            first_seen = float(item.get("first_seen_ms", 0) or 0) / 1000.0
            stage = str(item.get("stage") or "")
            if first_seen <= 0 or now - first_seen > _ACTIVE_TTL_SECONDS:
                _ACTIVE.pop(symbol, None)
                continue
            # Retain terminal tombstones until TTL so an old cross cannot revive.
            if stage not in _TERMINAL_STAGES:
                active.append({"symbol": symbol, "stage": stage})
    return active


def advance_m15_setup_lifecycle(row: Dict[str, Any], *, now_ms=None, cycle_id=None):
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    now = now_ms / 1000.0
    symbol = str(row.get("symbol") or "").strip().upper()
    if not symbol:
        return row

    status = str(row.get("crossover_status") or "").upper()
    opportunity = str(row.get("opportunity_state") or "").upper()
    direction = str(row.get("direction") or "UNKNOWN").upper()
    cycle_id = int(cycle_id if cycle_id is not None else now_ms)
    signal_ts = _number(row.get("signal_timestamp_ms"), 0)
    signal_ts = int(signal_ts) if signal_ts and signal_ts > 0 else None

    with _LOCK:
        old = _ACTIVE.get(symbol)
        prior = dict(old) if isinstance(old, dict) else None
        first_seen = float(prior.get("first_seen_ms", 0) or 0) / 1000.0 if prior else 0.0
        within_ttl = bool(prior and first_seen > 0 and 0 <= now - first_seen <= _ACTIVE_TTL_SECONDS)
        if prior and not within_ttl and prior.get("stage") not in _TERMINAL_STAGES:
            prior["stage"] = "EXPIRED"
            prior["terminal_reason"] = "SETUP_LIFETIME_EXPIRED"
            _ACTIVE[symbol] = dict(prior)

        terminal_input = status in {"EXPIRED", "INVALIDATED"} or opportunity in {"EXPIRED", "INVALIDATED"}
        if terminal_input:
            if prior is None or not within_ttl:
                terminal_stage = "INVALIDATED" if "INVALIDATED" in {status, opportunity} else "EXPIRED"
                if status == "CROSS_CONFIRMED" and signal_ts:
                    prior = _new_setup(symbol, direction, now_ms, terminal_stage, signal_ts)
                else:
                    row.update({
                        "lifecycle_active": False, "lifecycle_stage": terminal_stage,
                        "strategy_mode": "NO_TRADE_OR_WAIT", "strategy_mode_reason": "RADAR_SETUP_TERMINAL",
                        "entry_authorization": False, "entry_readiness": "NOT_AUTHORIZED_BY_RADAR",
                    })
                    return row
            elif status == "CROSS_CONFIRMED" and signal_ts and signal_ts != int(prior.get("cross_timestamp_ms") or 0):
                prior = _new_setup(symbol, direction, now_ms, "WAITING_FOR_PULLBACK", signal_ts)
            else:
                prior["stage"] = "INVALIDATED" if "INVALIDATED" in {status, opportunity} else "EXPIRED"
                prior["terminal_reason"] = str(row.get("rejection_reason") or "RADAR_SETUP_TERMINAL")
            if prior.get("stage") not in _TERMINAL_STAGES:
                prior["stage"] = "INVALIDATED" if "INVALIDATED" in {status, opportunity} else "EXPIRED"
            prior["last_seen_ms"] = now_ms
            _ACTIVE[symbol] = dict(prior)
            return _public_row(row, prior, "NO_TRADE_OR_WAIT", prior.get("terminal_reason", "RADAR_SETUP_TERMINAL"))

        active = bool(prior and within_ttl and prior.get("stage") not in _TERMINAL_STAGES)
        if status == "CROSS_CONFIRMED" and signal_ts:
            if prior and int(prior.get("cross_timestamp_ms") or 0) == signal_ts:
                # Same cross event: preserve its current lifecycle or terminal tombstone.
                pass
            else:
                prior = _new_setup(symbol, direction, now_ms, "WAITING_FOR_PULLBACK", signal_ts)
                active = True
        elif status == "PRE_CROSS":
            if prior is None:
                prior = _new_setup(symbol, direction, now_ms, "CROSS_APPROACHING")
                active = True
            elif prior.get("stage") == "CROSS_APPROACHING" and direction in {"BUY", "SELL"}:
                prior["direction"] = direction
        elif status == "CROSS_DETECTED":
            if prior is None:
                prior = _new_setup(symbol, direction, now_ms, "WAITING_FOR_CANDLE_CLOSE")
                active = True
            elif prior.get("stage") == "CROSS_APPROACHING":
                prior["stage"] = "WAITING_FOR_CANDLE_CLOSE"
                if direction in {"BUY", "SELL"}:
                    prior["direction"] = direction
        elif not active:
            row.update({
                "lifecycle_active": False, "lifecycle_stage": prior.get("stage") if prior else None,
                "strategy_mode": "UNCLASSIFIED", "strategy_mode_reason": "NO_ACTIVE_M15_SETUP",
                "entry_authorization": False, "entry_readiness": "NOT_AUTHORIZED_BY_RADAR",
            })
            return row

        if prior is None:
            row.update({
                "lifecycle_active": False, "strategy_mode": "UNCLASSIFIED",
                "entry_authorization": False, "entry_readiness": "NOT_AUTHORIZED_BY_RADAR",
            })
            return row
        if prior.get("stage") in _TERMINAL_STAGES:
            return _public_row(row, prior, "NO_TRADE_OR_WAIT", prior.get("terminal_reason", "SETUP_TERMINAL"))
        if now - float(prior.get("first_seen_ms", now_ms)) / 1000.0 > _ACTIVE_TTL_SECONDS:
            prior["stage"] = "EXPIRED"
            prior["terminal_reason"] = "SETUP_LIFETIME_EXPIRED"
            prior["last_seen_ms"] = now_ms
            _ACTIVE[symbol] = dict(prior)
            return _public_row(row, prior, "NO_TRADE_OR_WAIT", "SETUP_LIFETIME_EXPIRED")

        if prior.get("last_cycle_id") != cycle_id:
            prior["cycles_seen"] = int(prior.get("cycles_seen", 0)) + 1
            prior["last_cycle_id"] = cycle_id

        # Missing data does not invalidate a setup, but cannot advance its lifecycle.
        if status != "DATA_UNAVAILABLE":
            chart = row.get("chart") if isinstance(row.get("chart"), dict) else {}
            names = ("timestamps", "high", "low", "open", "close", "ema9", "ema15")
            arrays = [chart.get(name) or [] for name in names]
            size = min((len(values) for values in arrays), default=0)
            candles = []
            if size:
                ts, highs, lows, opens, closes, e9s, e15s = [values[-size:] for values in arrays]
                start_ts = int(prior.get("cross_timestamp_ms") or prior.get("first_seen_ms") or 0)
                for i in range(size):
                    try:
                        stamp = int(ts[i])
                        o, c, h, low, e9, e15 = map(float, (opens[i], closes[i], highs[i], lows[i], e9s[i], e15s[i]))
                        if stamp <= start_ts or min(o, c, h, low, e9, e15) <= 0 or h < low:
                            continue
                        candles.append((stamp, o, c, h, low, e9, e15))
                    except (TypeError, ValueError, OverflowError):
                        continue
                candles.sort(key=lambda item: item[0])

            if prior.get("stage") in {"WAITING_FOR_PULLBACK", "WAITING_FOR_CANDLE_CLOSE"}:
                for stamp, o, c, h, low, e9, e15 in candles:
                    band_low, band_high = sorted((e9, e15))
                    if low <= band_high and h >= band_low:
                        prior["stage"] = "PULLBACK_DETECTED"
                        prior["pullback_timestamp_ms"] = stamp
                        prior["pullback_observation"] = "EMA9_EMA15_BAND_TOUCH_ONLY"
                        break

            pullback_ts = int(prior.get("pullback_timestamp_ms") or 0)
            if prior.get("stage") == "PULLBACK_DETECTED" and pullback_ts:
                for stamp, o, c, h, low, e9, e15 in candles:
                    if stamp <= pullback_ts:
                        continue
                    band_low, band_high = sorted((e9, e15))
                    held = c >= band_low if prior.get("direction") == "BUY" else c <= band_high
                    if held:
                        prior["stage"] = "PULLBACK_HOLD_CONFIRMED"
                        prior["pullback_hold_timestamp_ms"] = stamp
                        break

            hold_ts = int(prior.get("pullback_hold_timestamp_ms") or 0)
            if prior.get("stage") == "PULLBACK_HOLD_CONFIRMED" and hold_ts:
                for index, candle in enumerate(candles):
                    stamp, o, c, h, low, e9, e15 = candle
                    if stamp <= hold_ts:
                        continue
                    previous = next((x for x in reversed(candles[:index]) if x[0] < stamp), None)
                    if previous is None:
                        continue
                    previous_close = previous[2]
                    span = max(0.0, h - low)
                    lower_wick = (min(o, c) - low) / span if span else 0.0
                    upper_wick = (h - max(o, c)) / span if span else 0.0
                    bullish = prior.get("direction") == "BUY" and c > o and c > previous_close and lower_wick >= 0.10
                    bearish = prior.get("direction") == "SELL" and c < o and c < previous_close and upper_wick >= 0.10
                    if bullish or bearish:
                        prior["stage"] = "REJECTION_CONFIRMED"
                        prior["rejection_timestamp_ms"] = stamp
                        prior["rejection_high"] = h
                        prior["rejection_low"] = low
                        prior["rejection_confirmed"] = True
                        break

            rejection_ts = int(prior.get("rejection_timestamp_ms") or 0)
            if prior.get("stage") == "REJECTION_CONFIRMED" and rejection_ts:
                rejection_high = _number(prior.get("rejection_high"))
                rejection_low = _number(prior.get("rejection_low"))
                for stamp, o, c, h, low, e9, e15 in candles:
                    if stamp <= rejection_ts:
                        continue
                    triggered = (
                        prior.get("direction") == "BUY" and c > o and rejection_high is not None and c > rejection_high
                    ) or (
                        prior.get("direction") == "SELL" and c < o and rejection_low is not None and c < rejection_low
                    )
                    if triggered:
                        prior["stage"] = "TRIGGER_CONFIRMED"
                        prior["trigger_timestamp_ms"] = stamp
                        break

        gap = _number(row.get("ema_distance_atr"))
        volume = _number(row.get("volume_ratio"))
        vwap = _number(row.get("session_vwap"))
        price = _number(row.get("closed_price", row.get("current_price")))
        aligned = bool(vwap is not None and price is not None and (
            (prior.get("direction") == "BUY" and price > vwap)
            or (prior.get("direction") == "SELL" and price < vwap)
        ))
        if gap is None or volume is None:
            mode, reason = "WAIT_FOR_EVIDENCE", "REGIME_OR_VOLUME_UNAVAILABLE"
        elif gap >= 0.35 and volume >= 1.0 and aligned:
            mode, reason = "TREND_CANDIDATE", "EMA_SEPARATION_VOLUME_AND_VWAP_ALIGNED"
        elif gap <= 0.20 and volume >= 0.70 and aligned:
            mode, reason = "SCALP_CANDIDATE", "WEAK_SEPARATION_BUT_DIRECTIONAL_LIQUIDITY"
        else:
            mode, reason = "NO_TRADE_OR_WAIT", "INSUFFICIENT_TREND_OR_SCALP_QUALITY"

        prior.update({"last_seen_ms": now_ms, "strategy_mode": mode, "strategy_mode_reason": reason})
        _ACTIVE[symbol] = dict(prior)

    return _public_row(row, prior, mode, reason)
