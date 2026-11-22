"""Persistent lifecycle memory for M15 crossover opportunities.

This module tracks candidates only. It never authorizes or submits orders.
SQLite persistence keeps PRE_CROSS/CROSS/RETEST progress across scanner cycles
and process restarts when the runtime data directory is on persistent storage.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict

_LOCK = threading.RLock()
_DB_PATH = os.getenv("M15_SETUP_DB_PATH", os.path.join("data", "m15_setup_lifecycle.sqlite3"))
_ACTIVE_TTL_SECONDS = 6 * 60 * 60


def _connect():
    directory = os.path.dirname(os.path.abspath(_DB_PATH))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=5.0)
    conn.execute("""CREATE TABLE IF NOT EXISTS m15_setup_lifecycle (
        symbol TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL
    )""")
    return conn


def _load(symbol: str):
    conn = _connect()
    try:
        row = conn.execute("SELECT payload, updated_at FROM m15_setup_lifecycle WHERE symbol=?", (symbol,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    try:
        payload = json.loads(row[0])
        payload["_stored_at"] = float(row[1])
        return payload
    except (TypeError, ValueError):
        return None


def _save(symbol: str, payload: Dict[str, Any], now: float):
    clean = {k: v for k, v in payload.items() if not k.startswith("_")}
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO m15_setup_lifecycle(symbol,payload,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
            (symbol, json.dumps(clean, separators=(",", ":"), allow_nan=False), now),
        )
        conn.commit()
    finally:
        conn.close()


def list_active_m15_setups(*, now_ms=None):
    now = float(now_ms if now_ms is not None else time.time() * 1000) / 1000.0
    conn = _connect()
    try:
        rows = conn.execute('SELECT symbol, payload FROM m15_setup_lifecycle').fetchall()
    finally:
        conn.close()
    active = []
    for symbol, raw in rows:
        try:
            payload = json.loads(raw)
            first_seen = float(payload.get('first_seen_ms', 0)) / 1000.0
            stage = str(payload.get('stage') or '')
            if stage not in ('EXPIRED', 'INVALIDATED', 'COMPLETED') and first_seen > 0 and now - first_seen <= _ACTIVE_TTL_SECONDS:
                active.append({'symbol': symbol, 'stage': stage})
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return active


def advance_m15_setup_lifecycle(row: Dict[str, Any], *, now_ms: int | None = None, cycle_id: int | None = None) -> Dict[str, Any]:
    """Attach persistent stage + mode diagnostics to one radar row."""
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    now = now_ms / 1000.0
    symbol = str(row.get("symbol") or "").strip().upper()
    if not symbol:
        return row
    with _LOCK:
        prior = _load(symbol)
        status = str(row.get("crossover_status") or "")
        state = str(row.get("opportunity_state") or "WAITING")
        cross_ts = row.get("signal_timestamp_ms") if status == "CROSS_CONFIRMED" else None
        direction = str(row.get("direction") or "UNKNOWN")
        first_seen_seconds = (float(prior.get("first_seen_ms", 0)) / 1000.0) if prior else 0.0
        active = bool(prior and first_seen_seconds > 0 and now - first_seen_seconds <= _ACTIVE_TTL_SECONDS)
        cycle_id = int(cycle_id if cycle_id is not None else now_ms)

        if status == "CROSS_CONFIRMED" and cross_ts:
            if not active or int(prior.get("cross_timestamp_ms") or 0) != int(cross_ts):
                prior = {
                    "symbol": symbol, "direction": direction, "cross_timestamp_ms": int(cross_ts),
                    "stage": "WAITING_FOR_PULLBACK", "first_seen_ms": now_ms,
                    "last_seen_ms": now_ms, "pullback_timestamp_ms": None,
                    "retest_timestamp_ms": None, "rejection_confirmed": False,
                }
            else:
                prior["last_seen_ms"] = now_ms
        elif status == "PRE_CROSS":
            if not active or prior.get("stage") in {"INVALIDATED", "EXPIRED"}:
                prior = {
                    "symbol": symbol, "direction": direction, "cross_timestamp_ms": None,
                    "stage": "CROSS_APPROACHING", "first_seen_ms": now_ms,
                    "last_seen_ms": now_ms, "pullback_timestamp_ms": None,
                    "retest_timestamp_ms": None, "rejection_confirmed": False,
                }
            else:
                prior["last_seen_ms"] = now_ms
                if prior.get("stage") == "CROSS_APPROACHING":
                    prior["direction"] = direction
        elif status == "CROSS_DETECTED":
            if not active or prior.get("stage") in {"INVALIDATED", "EXPIRED"}:
                prior = {
                    "symbol": symbol, "direction": direction, "cross_timestamp_ms": None,
                    "stage": "WAITING_FOR_CANDLE_CLOSE", "first_seen_ms": now_ms,
                    "last_seen_ms": now_ms, "pullback_timestamp_ms": None,
                    "retest_timestamp_ms": None, "rejection_confirmed": False,
                }
            else:
                prior["last_seen_ms"] = now_ms
                prior["stage"] = "WAITING_FOR_CANDLE_CLOSE"
        elif active:
            prior["last_seen_ms"] = now_ms

        if not prior:
            row["lifecycle_active"] = False
            row["strategy_mode"] = "UNCLASSIFIED"
            return row

        if now - float(prior.get("_stored_at", now)) > _ACTIVE_TTL_SECONDS:
            prior["stage"] = "EXPIRED"

        direction = str(prior.get("direction") or direction)
        chart = row.get("chart") or {}
        ts = chart.get("timestamps") or []
        highs, lows = chart.get("high") or [], chart.get("low") or []
        opens, closes = chart.get("open") or [], chart.get("close") or []
        e9s, e15s = chart.get("ema9") or [], chart.get("ema15") or []
        start_ts = int(prior.get("cross_timestamp_ms") or prior.get("first_seen_ms") or 0)
        touched_idx = None
        for i in range(min(len(ts), len(highs), len(lows), len(e9s), len(e15s))):
            if int(ts[i]) <= start_ts:
                continue
            band_low, band_high = min(float(e9s[i]), float(e15s[i])), max(float(e9s[i]), float(e15s[i]))
            if float(lows[i]) <= band_high and float(highs[i]) >= band_low:
                touched_idx = i
        if touched_idx is not None and prior.get("stage") in {"WAITING_FOR_PULLBACK", "WAITING_FOR_CANDLE_CLOSE"}:
            prior["stage"] = "PULLBACK_DETECTED"
            prior["pullback_timestamp_ms"] = int(ts[touched_idx])
        # Retest confirmation must be a later closed candle with directional rejection;
        # a touch by itself is never promoted to a confirmed retest.
        if (touched_idx is not None and len(closes) >= 2 and len(ts) >= 2
                and len(opens) == len(closes) == len(highs) == len(lows)
                and len(ts) == len(closes) and touched_idx < len(closes) - 1):
            o, c, h, low = map(float, (opens[-1], closes[-1], highs[-1], lows[-1]))
            candle_range = max(0.0, h - low)
            lower_wick = (min(o, c) - low) / candle_range if candle_range else 0.0
            upper_wick = (h - max(o, c)) / candle_range if candle_range else 0.0
            prior["rejection_confirmed"] = (
                c > o and c > float(closes[-2]) and lower_wick >= 0.10
                if prior.get("direction") == "BUY" else
                c < o and c < float(closes[-2]) and upper_wick >= 0.10
                if prior.get("direction") == "SELL" else False
            )
            if prior["rejection_confirmed"] and prior.get("stage") == "PULLBACK_DETECTED":
                prior["stage"] = "RETEST_CONFIRMED"
                prior["retest_timestamp_ms"] = int(ts[-1])

        gap_atr = row.get("ema_distance_atr")
        volume_ratio = row.get("volume_ratio")
        vwap = row.get("session_vwap")
        price = row.get("closed_price", row.get("current_price"))
        direction_ok = (
            vwap is not None and price is not None and
            ((direction == "BUY" and float(price) > float(vwap)) or
             (direction == "SELL" and float(price) < float(vwap)))
        )
        if gap_atr is None or volume_ratio is None:
            mode, mode_reason = "WAIT_FOR_EVIDENCE", "REGIME_OR_VOLUME_UNAVAILABLE"
        elif float(gap_atr) >= 0.35 and float(volume_ratio) >= 1.0 and direction_ok:
            mode, mode_reason = "TREND_CANDIDATE", "EMA_SEPARATION_VOLUME_AND_VWAP_ALIGNED"
        elif float(gap_atr) <= 0.20 and float(volume_ratio) >= 0.70 and direction_ok:
            mode, mode_reason = "SCALP_CANDIDATE", "WEAK_SEPARATION_BUT_DIRECTIONAL_LIQUIDITY"
        else:
            mode, mode_reason = "NO_TRADE_OR_WAIT", "INSUFFICIENT_TREND_OR_SCALP_QUALITY"

        if prior.get("stage") == "EXPIRED":
            mode = "NO_TRADE_OR_WAIT"
            mode_reason = "SETUP_LIFETIME_EXPIRED"
        prior["last_seen_ms"] = now_ms
        prior["strategy_mode"] = mode
        prior["strategy_mode_reason"] = mode_reason
        _save(symbol, prior, now)
        row.update({
            "lifecycle_active": prior.get("stage") not in {"EXPIRED", "INVALIDATED"},
            "lifecycle_stage": prior.get("stage"),
            "lifecycle_direction": prior.get("direction"),
            "lifecycle_cross_timestamp_ms": prior.get("cross_timestamp_ms"),
            "lifecycle_pullback_timestamp_ms": prior.get("pullback_timestamp_ms"),
            "lifecycle_retest_timestamp_ms": prior.get("retest_timestamp_ms"),
            "lifecycle_rejection_confirmed": bool(prior.get("rejection_confirmed")),
            "strategy_mode": mode,
            "strategy_mode_reason": mode_reason,
            "lifecycle_persisted": True,
            "entry_authorization": False,
            "entry_readiness": "NOT_AUTHORIZED_BY_RADAR",
        })
        return row
