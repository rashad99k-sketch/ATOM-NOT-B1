"""Runtime-only M15 crossover candidate lifecycle.

This module retains M15 observations in process memory and optionally persists
them through the existing TradeJournal runtime-state store. It never grants
entry authority and never submits, modifies, or closes an order.
"""
from __future__ import annotations

import copy
import threading
import time
from typing import Any, Dict

_LOCK = threading.RLock()
_ACTIVE: Dict[str, Dict[str, Any]] = {}
_ACTIVE_TTL_SECONDS = 6 * 60 * 60
_TERMINAL_STAGES = {"EXPIRED", "INVALIDATED", "COMPLETED", "ENTRY_WINDOW_MISSED"}
_BREAKOUT_MIN_VOLUME_RATIO = 1.15
_BREAKOUT_MIN_BODY_RATIO = 0.55
_BREAKOUT_MIN_CLOSE_POSITION = 0.72
_BREAKOUT_MIN_DISPLACEMENT_ATR = 0.35
_BREAKOUT_MAX_EXTENSION_ATR = 1.50
_STORE = None
_STORE_READY = False
_PERSIST_OK = False
_STORE_KEY = "m15_persistent_opportunity_registry_v1"


def configure_m15_setup_store(store):
    """Attach the existing SQLite TradeJournal runtime-state store, if available."""
    global _STORE, _STORE_READY, _PERSIST_OK
    with _LOCK:
        _STORE = store
        _STORE_READY = False
        _PERSIST_OK = False
        _ensure_loaded_locked()


def _ensure_loaded_locked():
    global _STORE_READY
    if _STORE_READY:
        return
    _STORE_READY = True
    if _STORE is None:
        return
    try:
        saved = _STORE.get_runtime_state(_STORE_KEY, {})
        if isinstance(saved, dict):
            for key, value in saved.items():
                if isinstance(value, dict) and value.get("symbol"):
                    _ACTIVE[str(key)] = copy.deepcopy(value)
            # A successful read means the configured persistence backend is reachable.
            global _PERSIST_OK
            _PERSIST_OK = True
    except Exception:
        # Keep the in-memory lifecycle available, but never claim it was persisted.
        _STORE_READY = False


def _persist_locked():
    global _PERSIST_OK
    if _STORE is None:
        _PERSIST_OK = False
        return False
    try:
        _STORE.set_runtime_state(_STORE_KEY, copy.deepcopy(_ACTIVE))
        _PERSIST_OK = True
    except Exception:
        _PERSIST_OK = False
    return _PERSIST_OK


def clear_m15_setup_memory():
    with _LOCK:
        _ensure_loaded_locked()
        _ACTIVE.clear()
        _persist_locked()


def _number(value, default=None):
    try:
        result = float(value)
        return result if result == result and abs(result) != float("inf") else default
    except (TypeError, ValueError, OverflowError):
        return default


def _new_setup(symbol, direction, now_ms, stage, cross_timestamp_ms=None):
    return {
        "symbol": symbol, "direction": direction if direction in {"BUY", "SELL"} else "UNKNOWN",
        "cross_timestamp_ms": cross_timestamp_ms,
        "event_id": f"M15:{symbol}:{direction}:{int(cross_timestamp_ms)}" if cross_timestamp_ms else None,
        "stage": stage,
        "first_seen_ms": now_ms, "last_seen_ms": now_ms,
        "pullback_timestamp_ms": None, "pullback_hold_timestamp_ms": None,
        "rejection_timestamp_ms": None, "trigger_timestamp_ms": None,
        "rejection_high": None, "rejection_low": None, "rejection_confirmed": False,
        "breakout_status": "WAITING_FOR_BREAKOUT", "breakout_level": None,
        "breakout_timestamp_ms": None, "breakout_close": None,
        "breakout_volume_ratio": None, "breakout_displacement_atr": None,
        "breakout_extension_atr": None, "breakout_body_ratio": None,
        "false_breakout_count": 0, "last_breakout_attempt_timestamp_ms": None,
        "failed_breakout_timestamp_ms": None,
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
        "lifecycle_breakout_status": prior.get("breakout_status", "WAITING_FOR_BREAKOUT"),
        "lifecycle_breakout_level": prior.get("breakout_level"),
        "lifecycle_breakout_timestamp_ms": prior.get("breakout_timestamp_ms"),
        "lifecycle_breakout_close": prior.get("breakout_close"),
        "lifecycle_breakout_volume_ratio": prior.get("breakout_volume_ratio"),
        "lifecycle_breakout_displacement_atr": prior.get("breakout_displacement_atr"),
        "lifecycle_breakout_extension_atr": prior.get("breakout_extension_atr"),
        "lifecycle_false_breakout_count": int(prior.get("false_breakout_count", 0)),
        "lifecycle_failed_breakout_timestamp_ms": prior.get("failed_breakout_timestamp_ms"),
        "lifecycle_breakout_confirmed": prior.get("breakout_status") == "SUCCESSFUL_BREAKOUT",
        "strategy_mode": mode, "strategy_mode_reason": reason,
        "lifecycle_event_id": prior.get("event_id"),
        "lifecycle_persisted": bool(_PERSIST_OK), "lifecycle_cycles_seen": int(prior.get("cycles_seen", 0)),
        "lifecycle_minimum_retention_met": int(prior.get("cycles_seen", 0)) >= 2,
        "entry_authorization": False, "entry_readiness": "NOT_AUTHORIZED_BY_RADAR",
    })
    return row


def list_active_m15_setups(*, now_ms=None):
    now = float(now_ms if now_ms is not None else time.time() * 1000) / 1000.0
    active = []
    with _LOCK:
        _ensure_loaded_locked()
        for symbol, item in list(_ACTIVE.items()):
            first_seen = float(item.get("first_seen_ms", 0) or 0) / 1000.0
            stage = str(item.get("stage") or "")
            if first_seen <= 0 or now - first_seen > _ACTIVE_TTL_SECONDS:
                _ACTIVE.pop(symbol, None)
                _persist_locked()
                continue
            # Retain terminal tombstones until TTL so an old cross cannot revive.
            if stage not in _TERMINAL_STAGES:
                active.append({
                    "symbol": symbol, "stage": stage,
                    "direction": item.get("direction"),
                    "event_id": item.get("event_id"),
                    "cross_timestamp_ms": item.get("cross_timestamp_ms"),
                })
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
        _ensure_loaded_locked()
        old = _ACTIVE.get(symbol)
        prior = dict(old) if isinstance(old, dict) else None
        first_seen = float(prior.get("first_seen_ms", 0) or 0) / 1000.0 if prior else 0.0
        within_ttl = bool(prior and first_seen > 0 and 0 <= now - first_seen <= _ACTIVE_TTL_SECONDS)
        if prior and not within_ttl and prior.get("stage") not in _TERMINAL_STAGES:
            prior["stage"] = "EXPIRED"
            prior["terminal_reason"] = "SETUP_LIFETIME_EXPIRED"
            _ACTIVE[symbol] = dict(prior)
            _persist_locked()

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
            _persist_locked()
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
            _persist_locked()
            return _public_row(row, prior, "NO_TRADE_OR_WAIT", "SETUP_LIFETIME_EXPIRED")

        if prior.get("last_cycle_id") != cycle_id:
            prior["cycles_seen"] = int(prior.get("cycles_seen", 0)) + 1
            prior["last_cycle_id"] = cycle_id

        # Missing data does not invalidate a setup, but cannot advance its lifecycle.
        if status != "DATA_UNAVAILABLE":
            chart = row.get("chart") if isinstance(row.get("chart"), dict) else {}
            names = ("timestamps", "high", "low", "open", "close", "ema9", "ema15", "volume", "session_vwap")
            arrays = [chart.get(name) or [] for name in names]
            size = min((len(values) for values in arrays[:7]), default=0)
            candles = []
            candle_context = {}
            if size:
                ts, highs, lows, opens, closes, e9s, e15s = [values[-size:] for values in arrays[:7]]
                volumes = list(arrays[7][-size:]) if len(arrays[7]) >= size else [None] * size
                vwaps = list(arrays[8][-size:]) if len(arrays[8]) >= size else [None] * size
                start_ts = int(prior.get("cross_timestamp_ms") or prior.get("first_seen_ms") or 0)
                tr_values = []
                previous_close = None
                for i in range(size):
                    try:
                        h, low, c = float(highs[i]), float(lows[i]), float(closes[i])
                        tr = max(h - low, abs(h - previous_close), abs(low - previous_close)) if previous_close is not None else h - low
                        tr_values.append(max(0.0, tr))
                        previous_close = c
                    except (TypeError, ValueError, OverflowError):
                        tr_values.append(0.0)
                atr_values = []
                atr_value = None
                for tr in tr_values:
                    atr_value = tr if atr_value is None else ((13.0 * atr_value + tr) / 14.0)
                    atr_values.append(atr_value)
                for i in range(size):
                    try:
                        stamp = int(ts[i])
                        o, c, h, low, e9, e15 = map(float, (opens[i], closes[i], highs[i], lows[i], e9s[i], e15s[i]))
                        if stamp <= start_ts or min(o, c, h, low, e9, e15) <= 0 or h < low:
                            continue
                        candles.append((stamp, o, c, h, low, e9, e15))
                        span = max(0.0, h - low)
                        body_ratio = abs(c - o) / span if span > 0 else 0.0
                        close_position = (c - low) / span if span > 0 else 0.5
                        atr_i = _number(atr_values[i])
                        volume_i = _number(volumes[i])
                        prior_volumes = [_number(v) for v in volumes[max(0, i - 20):i]]
                        prior_volumes = [v for v in prior_volumes if v is not None and v >= 0]
                        baseline = sum(prior_volumes) / len(prior_volumes) if len(prior_volumes) >= 10 else None
                        volume_ratio_i = volume_i / baseline if volume_i is not None and baseline and baseline > 0 else None
                        vwap_i = _number(vwaps[i])
                        candle_context[stamp] = {
                            "atr": atr_i,
                            "volume_ratio": volume_ratio_i,
                            "body_ratio": body_ratio,
                            "close_position": close_position,
                            "displacement_atr": abs(c - o) / atr_i if atr_i and atr_i > 0 else None,
                            "extension_atr": abs(c - e9) / atr_i if atr_i and atr_i > 0 else None,
                            "vwap_aligned": bool(vwap_i is not None and ((prior.get("direction") == "BUY" and c > vwap_i) or (prior.get("direction") == "SELL" and c < vwap_i))),
                        }
                    except (TypeError, ValueError, OverflowError, IndexError):
                        continue
                candles.sort(key=lambda item: item[0])

            if prior.get("stage") in {"WAITING_FOR_PULLBACK", "WAITING_FOR_CANDLE_CLOSE", "MOMENTUM_EXPANSION_CANDIDATE"}:
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
            direction = str(prior.get("direction") or "UNKNOWN").upper()
            breakout_level = _number(prior.get("breakout_level"))
            if breakout_level is None:
                breakout_level = _number(prior.get("rejection_high" if direction == "BUY" else "rejection_low"))
                if breakout_level is not None:
                    prior["breakout_level"] = breakout_level

            # A subsequent closed candle through the level invalidates a previously
            # confirmed breakout. Intrabar wicks alone never change lifecycle authority.
            if prior.get("breakout_status") == "SUCCESSFUL_BREAKOUT":
                success_ts = int(prior.get("breakout_timestamp_ms") or 0)
                for stamp, o, c, h, low, e9, e15 in candles:
                    if stamp <= success_ts:
                        continue
                    failed = (direction == "BUY" and c <= breakout_level) or (direction == "SELL" and c >= breakout_level)
                    if failed:
                        prior["stage"] = "INVALIDATED"
                        prior["breakout_status"] = "FAILED_BREAKOUT"
                        prior["failed_breakout_timestamp_ms"] = stamp
                        prior["terminal_reason"] = "CLOSED_CANDLE_RETURNED_THROUGH_CONFIRMED_BREAKOUT_LEVEL"
                        break

            # Breakout attempts are evaluated only from candles after the closed
            # rejection candle. A wick through the level without a close beyond it
            # is a false breakout, not a valid continuation trigger.
            if (prior.get("stage") in {"REJECTION_CONFIRMED", "FALSE_BREAKOUT", "BREAKOUT_UNCONFIRMED"}
                    and rejection_ts and breakout_level is not None):
                last_attempt = int(prior.get("last_breakout_attempt_timestamp_ms") or rejection_ts)
                for stamp, o, c, h, low, e9, e15 in candles:
                    if stamp <= last_attempt:
                        continue
                    wick_broke = (direction == "BUY" and h > breakout_level) or (direction == "SELL" and low < breakout_level)
                    close_broke = (direction == "BUY" and c > breakout_level) or (direction == "SELL" and c < breakout_level)
                    if not wick_broke:
                        continue
                    ctx = candle_context.get(stamp, {})
                    if not close_broke:
                        prior["stage"] = "FALSE_BREAKOUT"
                        prior["breakout_status"] = "FALSE_BREAKOUT"
                        prior["false_breakout_count"] = int(prior.get("false_breakout_count", 0)) + 1
                        prior["last_breakout_attempt_timestamp_ms"] = stamp
                        prior["false_breakout_timestamp_ms"] = stamp
                        last_attempt = stamp
                        continue

                    volume_ratio_i = _number(ctx.get("volume_ratio"))
                    body_ratio_i = _number(ctx.get("body_ratio"))
                    close_position_i = _number(ctx.get("close_position"))
                    displacement_i = _number(ctx.get("displacement_atr"))
                    extension_i = _number(ctx.get("extension_atr"))
                    directional_close = (close_position_i is not None and
                        ((direction == "BUY" and close_position_i >= _BREAKOUT_MIN_CLOSE_POSITION)
                         or (direction == "SELL" and close_position_i <= 1.0 - _BREAKOUT_MIN_CLOSE_POSITION)))
                    quality_ok = bool(
                        volume_ratio_i is not None and volume_ratio_i >= _BREAKOUT_MIN_VOLUME_RATIO
                        and body_ratio_i is not None and body_ratio_i >= _BREAKOUT_MIN_BODY_RATIO
                        and directional_close
                        and displacement_i is not None and displacement_i >= _BREAKOUT_MIN_DISPLACEMENT_ATR
                        and extension_i is not None and extension_i <= _BREAKOUT_MAX_EXTENSION_ATR
                        and ctx.get("vwap_aligned") is True
                    )
                    prior["last_breakout_attempt_timestamp_ms"] = stamp
                    prior["breakout_close"] = c
                    prior["breakout_volume_ratio"] = volume_ratio_i
                    prior["breakout_displacement_atr"] = displacement_i
                    prior["breakout_extension_atr"] = extension_i
                    prior["breakout_body_ratio"] = body_ratio_i
                    if quality_ok:
                        prior["stage"] = "TRIGGER_CONFIRMED"
                        prior["breakout_status"] = "SUCCESSFUL_BREAKOUT"
                        prior["breakout_timestamp_ms"] = stamp
                        prior["trigger_timestamp_ms"] = stamp
                        prior["breakout_quality"] = "CLOSED_BREAKOUT_VOLUME_DISPLACEMENT_VWAP_NO_CHASE"
                        break
                    prior["stage"] = "BREAKOUT_UNCONFIRMED"
                    prior["breakout_status"] = "BREAKOUT_UNCONFIRMED"
                    prior["breakout_unconfirmed_timestamp_ms"] = stamp
                    last_attempt = stamp

        gap = _number(row.get("ema_distance_atr"))
        volume = _number(row.get("volume_ratio"))
        vwap = _number(row.get("session_vwap"))
        price = _number(row.get("closed_price", row.get("current_price")))
        aligned = bool(vwap is not None and price is not None and (
            (prior.get("direction") == "BUY" and price > vwap)
            or (prior.get("direction") == "SELL" and price < vwap)
        ))
        evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
        body_ratio = _number(evidence.get("body_to_range"))
        close_position = _number(evidence.get("close_position"))
        displacement_atr = _number(evidence.get("displacement_atr"))
        price_extension_atr = _number(evidence.get("price_extension_atr"))
        expansion_candle = bool(
            volume is not None and volume >= 1.5
            and body_ratio is not None and body_ratio >= 0.65
            and displacement_atr is not None and displacement_atr >= 0.80
            and close_position is not None
            and ((prior.get("direction") == "BUY" and close_position >= 0.80)
                 or (prior.get("direction") == "SELL" and close_position <= 0.20))
            and aligned
            and price_extension_atr is not None and price_extension_atr <= 1.5
        )
        if expansion_candle and prior.get("stage") in {"WAITING_FOR_PULLBACK", "WAITING_FOR_CANDLE_CLOSE"}:
            prior["stage"] = "MOMENTUM_EXPANSION_CANDIDATE"
            prior["momentum_timestamp_ms"] = int(row.get("signal_timestamp_ms") or now_ms)
        if prior.get("breakout_status") == "FAILED_BREAKOUT":
            mode, reason = "NO_TRADE_OR_WAIT", "FAILED_BREAKOUT_CLOSED_BACK_THROUGH_LEVEL"
        elif prior.get("breakout_status") == "SUCCESSFUL_BREAKOUT":
            mode, reason = "BREAKOUT_CONFIRMED_DISCOVERY_ONLY", "CLOSED_BREAKOUT_VOLUME_DISPLACEMENT_VWAP_NO_CHASE"
        elif prior.get("breakout_status") == "FALSE_BREAKOUT":
            mode, reason = "WAIT_FOR_EVIDENCE", "WICK_BREAKOUT_REJECTED_BY_CLOSED_CANDLE"
        elif prior.get("breakout_status") == "BREAKOUT_UNCONFIRMED":
            mode, reason = "WAIT_FOR_EVIDENCE", "BREAKOUT_CLOSE_LACKS_REQUIRED_QUALITY"
        elif gap is None or volume is None:
            mode, reason = "WAIT_FOR_EVIDENCE", "REGIME_OR_VOLUME_UNAVAILABLE"
        elif expansion_candle:
            mode, reason = "MOMENTUM_CANDIDATE", "DISPLACEMENT_VOLUME_CANDLE_CLOSE_AND_VWAP_CONFIRMED"
        elif gap >= 0.35 and volume >= 1.0 and aligned:
            mode, reason = "TREND_CANDIDATE", "EMA_SEPARATION_VOLUME_AND_VWAP_ALIGNED"
        elif gap <= 0.20 and volume >= 0.70 and aligned:
            mode, reason = "SCALP_CANDIDATE", "WEAK_SEPARATION_BUT_DIRECTIONAL_LIQUIDITY"
        else:
            mode, reason = "NO_TRADE_OR_WAIT", "INSUFFICIENT_TREND_OR_SCALP_QUALITY"

        prior.update({"last_seen_ms": now_ms, "strategy_mode": mode, "strategy_mode_reason": reason})
        _ACTIVE[symbol] = dict(prior)
        _persist_locked()

    return _public_row(row, prior, mode, reason)
