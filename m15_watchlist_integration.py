"""Run the M15 setup lifecycle on the existing institutional Watchlist frame.

This module contains no exchange client and no network code. It consumes the
already-fetched, closed OHLCV frame from Scanner 2 and is idempotent per
symbol/candle so BUY and SELL hypotheses cannot advance it twice.
"""
from __future__ import annotations
import copy
import threading
import time
from typing import Any, Callable, Dict, Optional

from m15_crossover_radar import evaluate_m15_radar
from m15_setup_lifecycle import advance_m15_setup_lifecycle, list_active_m15_setups

_LOCK = threading.RLock()
_MAX_TRACKED_SYMBOLS = 200
_MAX_RADAR_ROWS = 60


def _canonical(symbol: Any) -> str:
    return str(symbol or "").strip().upper()


def _finite_int(value: Any) -> Optional[int]:
    try:
        number = int(value)
        return number if number > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _sync_active_feed(memory: Dict[str, Any]) -> list:
    active = list_active_m15_setups()
    memory["m15_setup_watchlist"] = [
        {
            "symbol": item.get("symbol"),
            "side_hint": item.get("direction"),
            "score": 75.0,
            "m15_stage": item.get("stage"),
            "m15_event_id": item.get("event_id"),
            "m15_cross_timestamp_ms": item.get("cross_timestamp_ms"),
            "source": "m15_watchlist_lifecycle_discovery_only",
        }
        for item in active if item.get("direction") in {"BUY", "SELL"}
    ]
    return active


def _update_radar_meta(memory: Dict[str, Any], *, now_ms: int, symbol: str,
                       data_unavailable: bool = False, reason: Optional[str] = None) -> None:
    rows = [item for item in memory.get("m15_crossover_radar", []) if isinstance(item, dict)]
    meta = memory.setdefault("m15_crossover_radar_meta", {})
    meta.update({
        "state": "PARTIAL_DATA" if data_unavailable else "ACTIVE" if rows else "NO_CANDIDATES",
        "timeframe": "15m",
        "source": "SCANNER_2_SHARED_CLOSED_FRAME",
        "closed_candles_only": True,
        "entry_authorization": False,
        "last_update_ms": int(now_ms),
        "last_symbol": str(symbol),
        "frames_processed_total": int(meta.get("frames_processed_total", 0) or 0) + 1,
        "candidate_count": len(rows),
        "confirmed_count": sum(1 for item in rows if item.get("crossover_status") == "CROSS_CONFIRMED"),
        "provisional_count": sum(1 for item in rows if item.get("crossover_status") == "CROSS_DETECTED"),
        "early_count": sum(1 for item in rows if item.get("crossover_status") == "PRE_CROSS"),
        "last_data_error": str(reason) if data_unavailable and reason else None,
    })
    if data_unavailable:
        meta["data_unavailable_count"] = int(meta.get("data_unavailable_count", 0) or 0) + 1


def process_m15_watchlist_frame(
    symbol: str,
    closed_frame: Any,
    memory: Dict[str, Any],
    *,
    now_ms: Optional[int] = None,
    enqueue_snapshot_fn: Optional[Callable[[Dict[str, Any]], bool]] = None,
    snapshot_path_fn: Optional[Callable[[Dict[str, Any]], str]] = None,
) -> Optional[Dict[str, Any]]:
    """Advance lifecycle state from one existing closed frame; never fetch."""
    if closed_frame is None or getattr(closed_frame, "empty", True):
        return None
    if "timestamp" not in getattr(closed_frame, "columns", []):
        return None
    try:
        candle_ts = _finite_int(closed_frame["timestamp"].iloc[-1])
    except Exception:
        return None
    if candle_ts is None:
        return None
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    key = _canonical(symbol)
    if not key:
        return None

    with _LOCK:
        last_seen = memory.setdefault("_m15_lifecycle_last_candle", {})
        rows = memory.setdefault("_m15_lifecycle_rows", {})
        prior_row = rows.get(key)
        if last_seen.get(key) == candle_ts and isinstance(prior_row, dict):
            candle_age_ms = now_ms - (candle_ts + 900_000)
            if candle_age_ms <= 1_800_000 or not prior_row.get("lifecycle_active"):
                _update_radar_meta(memory, now_ms=now_ms, symbol=key)
                return copy.deepcopy(prior_row)
            stale_row = dict(prior_row)
            stale_row.update({
                "crossover_status": "EXPIRED", "opportunity_state": "EXPIRED",
                "rejection_reason": "STALE_CLOSED_CANDLE_DATA", "last_update_ms": now_ms,
            })
            stale_row = advance_m15_setup_lifecycle(stale_row, now_ms=now_ms, cycle_id=candle_ts)
            rows[key] = copy.deepcopy(stale_row)
            watch_item = memory.setdefault("watchlist", {}).setdefault(key, {"symbol": key})
            watch_item.pop("m15_setup", None)
            _sync_active_feed(memory)
            memory["m15_crossover_radar"] = [
                item for item in memory.get("m15_crossover_radar", [])
                if not isinstance(item, dict) or _canonical(item.get("symbol")) != key
            ][:_MAX_RADAR_ROWS]
            _update_radar_meta(memory, now_ms=now_ms, symbol=key)
            return copy.deepcopy(stale_row)

        row = evaluate_m15_radar(key, closed_frame, now_ms=now_ms)
        if row.get("crossover_status") == "DATA_UNAVAILABLE":
            _update_radar_meta(memory, now_ms=now_ms, symbol=key, data_unavailable=True,
                               reason=row.get("rejection_reason") or "DATA_UNAVAILABLE")
            return row
        row = advance_m15_setup_lifecycle(row, now_ms=now_ms, cycle_id=candle_ts)
        last_seen[key] = candle_ts
        if len(last_seen) > _MAX_TRACKED_SYMBOLS:
            stale_keys = sorted(last_seen, key=lambda item: int(last_seen.get(item, 0) or 0))
            for old_key in stale_keys[:len(last_seen) - _MAX_TRACKED_SYMBOLS]:
                last_seen.pop(old_key, None)
                rows.pop(old_key, None)

        watchlist = memory.setdefault("watchlist", {})
        watch_item = watchlist.setdefault(key, {"symbol": key})
        prior_setup = watch_item.get("m15_setup") if isinstance(watch_item.get("m15_setup"), dict) else {}
        row["snapshot_ref"] = prior_setup.get("snapshot_ref")
        row["snapshot_history"] = list(prior_setup.get("snapshot_history") or [])[-12:]
        triple = row.get("triple_confluence") if isinstance(row.get("triple_confluence"), dict) else {}
        lifecycle_event_id = row.get("lifecycle_event_id") or triple.get("event_id")
        snapshot_changed = (
            row.get("lifecycle_active") and triple.get("status") == "VALID"
            and triple.get("event_id")
            and (prior_setup.get("triple_event_id") != triple.get("event_id")
                 or prior_setup.get("snapshot_stage") != row.get("lifecycle_stage"))
        )
        if snapshot_changed and enqueue_snapshot_fn and snapshot_path_fn:
            stage = "crossover" if prior_setup.get("triple_event_id") != triple.get("event_id") else str(row.get("lifecycle_stage") or "lifecycle").lower()
            payload = {**row, "snapshot_stage": stage, "lifecycle_event_id": triple.get("event_id")}
            try:
                accepted = bool(enqueue_snapshot_fn(payload))
                snapshot_relative = snapshot_path_fn(payload) if accepted else None
            except Exception:
                accepted = False
                snapshot_relative = None
            row["snapshot_ref"] = "/crossover-snapshots/" + snapshot_relative if accepted and snapshot_relative else None
            row["snapshot_queue_status"] = "QUEUED" if row["snapshot_ref"] else "QUEUE_FULL_OR_INVALID"

        if row.get("lifecycle_active"):
            history = list(prior_setup.get("snapshot_history") or [])
            if row.get("snapshot_ref") and row["snapshot_ref"] not in history:
                history.append(row["snapshot_ref"])
            watch_item["m15_setup"] = {
                "stage": row.get("lifecycle_stage"), "direction": row.get("lifecycle_direction"),
                "strategy_mode": row.get("strategy_mode"), "reason": row.get("strategy_mode_reason"),
                "event_id": lifecycle_event_id, "cross_timestamp_ms": row.get("lifecycle_cross_timestamp_ms"),
                "pullback_timestamp_ms": row.get("lifecycle_pullback_timestamp_ms"),
                "retest_timestamp_ms": row.get("lifecycle_retest_timestamp_ms"),
                "breakout_status": row.get("lifecycle_breakout_status"),
                "breakout_timestamp_ms": row.get("lifecycle_breakout_timestamp_ms"),
                "failed_breakout_timestamp_ms": row.get("lifecycle_failed_breakout_timestamp_ms"),
                "false_breakout_count": row.get("lifecycle_false_breakout_count", 0),
                "cycles_seen": row.get("lifecycle_cycles_seen", 1),
                "minimum_retention_met": row.get("lifecycle_minimum_retention_met", False),
                "triple_event_id": triple.get("event_id"), "triple_status": triple.get("status"),
                "triple_confluence": triple, "snapshot_ref": row.get("snapshot_ref"),
                "snapshot_history": history[-12:], "snapshot_stage": row.get("lifecycle_stage"),
                "updated_at_ms": now_ms, "source": "M15_INSTITUTIONAL_WATCHLIST_CLOSED_FRAME",
            }
        elif row.get("lifecycle_stage") in {"INVALIDATED", "EXPIRED", "COMPLETED"}:
            watch_item.pop("m15_setup", None)
            row["snapshot_ref"] = None

        rows[key] = copy.deepcopy(row)
        current = [
            item for item in memory.get("m15_crossover_radar", [])
            if isinstance(item, dict) and _canonical(item.get("symbol")) != key
        ]
        if row.get("crossover_status") in {"CROSS_CONFIRMED", "CROSS_DETECTED", "PRE_CROSS"} or row.get("lifecycle_active"):
            current.append(copy.deepcopy(row))
        current.sort(key=lambda item: (
            0 if item.get("crossover_status") == "CROSS_CONFIRMED" else
            1 if item.get("crossover_status") == "CROSS_DETECTED" else
            2 if item.get("crossover_status") == "PRE_CROSS" else 3,
            int(item.get("cross_age_bars") if item.get("cross_age_bars") is not None else 999),
            -int(item.get("last_update_ms") or 0), _canonical(item.get("symbol")),
        ))
        memory["m15_crossover_radar"] = current[:_MAX_RADAR_ROWS]
        _sync_active_feed(memory)
        _update_radar_meta(memory, now_ms=now_ms, symbol=key)
        row["entry_authorization"] = False
        row["entry_readiness"] = "NOT_AUTHORIZED_BY_RADAR"
        return copy.deepcopy(row)
