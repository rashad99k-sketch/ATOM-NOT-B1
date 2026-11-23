import numpy as np
import pandas as pd

import roro_confluence as confluence
from roro_confluence import validate_adaptive_ema9_vwap_entry_gate


def _frame(mode="plain"):
    start = 1795132800000
    rows = []
    for i in range(40):
        close = 100.0
        open_price = 99.8
        high, low = 100.6, 99.4
        if mode == "plain" and i == 39:
            open_price, high, low, close = 100.1, 100.6, 99.8, 100.4
        elif mode == "micro" and i == 39:
            open_price, high, low, close = 100.1, 100.65, 100.05, 100.55
        elif mode == "micro_wait" and i == 39:
            open_price, high, low, close = 100.50, 100.85, 100.42, 100.70
        elif mode == "retest":
            if i == 37:  # breakout candle, on the cross
                open_price, high, low, close = 100.4, 102.0, 100.3, 101.5
            elif i == 39:  # later retest and bullish reclaim
                open_price, high, low, close = 100.55, 101.0, 100.4, 100.75
        rows.append({
            "timestamp": start + i * 900_000,
            "open": open_price, "high": high, "low": low,
            "close": close, "volume": 1000.0,
        })
    frame = pd.DataFrame(rows)
    now_ms = int(frame.timestamp.iloc[-1] + 901_000)
    return frame, now_ms


def _run(monkeypatch, mode, side="BUY", live=100.1, zone=(99.8, 100.2)):
    frame, now_ms = _frame(mode)
    cross_ts = int(frame.timestamp.iloc[-3])
    monkeypatch.setattr(
        confluence, "validate_dual_ema_vwap_entry_gate",
        lambda *a, **k: {
            "ready": True, "side": side, "reason": "fresh_closed_ema9_vwap_cross_ema50_soft_confirmation",
            "ema9_cross": "BULLISH" if side == "BUY" else "BEARISH",
            "ema9_cross_timestamp": cross_ts,
            "ema50": None, "ema50_alignment": "UNAVAILABLE",
            "signal_candle_id": f"{side}:{cross_ts}",
            "cross_quality": "UNRATED_PENDING_CALIBRATION",
        },
    )
    monkeypatch.setattr(
        confluence, "compute_session_vwap",
        lambda closed, timeframe_seconds, now_ms=None: pd.Series(100.0, index=closed.index),
    )
    ema_fn = lambda values, period: pd.Series(100.0, index=values.index)
    return validate_adaptive_ema9_vwap_entry_gate(
        frame, side, ema_fn, timeframe_seconds=900, now_ms=now_ms,
        current_price=live, lookback_candles=5,
        entry_zone_low=zone[0] if zone else None,
        entry_zone_high=zone[1] if zone else None,
    )


def test_direct_entry_when_cross_and_live_location_are_confirmed(monkeypatch):
    result = _run(monkeypatch, "plain", live=100.1)
    assert result["ready"] is True
    assert result["entry_mode"] == "DIRECT_ENTRY"
    assert result["location_source"] == "INSTITUTIONAL_ZONE"
    assert result["signal_candle_id"].startswith("BUY:")


def test_micro_pullback_requires_zone_touch_and_directional_reclaim(monkeypatch):
    result = _run(monkeypatch, "micro", live=100.55)
    assert result["ready"] is True
    assert result["entry_mode"] == "MICRO_PULLBACK"
    assert result["micro_pullback_touched"] is True
    assert result["micro_pullback_reclaimed"] is True


def test_micro_pullback_waits_when_price_has_not_retested_zone(monkeypatch):
    result = _run(monkeypatch, "micro_wait", live=100.65)
    assert result["ready"] is False
    assert result["entry_mode"] == "MICRO_PULLBACK"
    assert result["reason"] == "waiting_for_micro_pullback"


def test_structural_break_uses_retest_and_reclaim_not_breakout_chase(monkeypatch):
    result = _run(monkeypatch, "retest", live=100.7, zone=None)
    assert result["ready"] is True
    assert result["entry_mode"] == "RETEST_ENTRY"
    assert result["structure_break"] is True
    assert result["retest_confirmed"] is True
    assert result["location_source"] == "EMA9_VWAP_CONFLUENCE"


def test_no_chase_invalidates_stale_or_overextended_price(monkeypatch):
    result = _run(monkeypatch, "plain", live=103.0)
    assert result["ready"] is False
    assert result["entry_mode"] == "INVALIDATED"
    assert result["reason"] == "entry_window_missed_no_chase"


def test_ema50_unavailable_is_not_a_veto(monkeypatch):
    result = _run(monkeypatch, "plain", live=100.1)
    assert result["ready"] is True
    assert result["ema50_alignment"] == "UNAVAILABLE"
    assert result["cross_quality"] == "UNRATED_PENDING_CALIBRATION"


def test_micro_pullback_is_retained_when_scanner_misses_touch_candle(monkeypatch):
    frame, now_ms = _frame("plain")
    # The touch/reclaim happens one candle before the latest scanner snapshot.
    frame.loc[37, ["open", "high", "low", "close"]] = [100.0, 100.5, 99.9, 100.35]
    frame.loc[38, ["open", "high", "low", "close"]] = [100.2, 100.45, 100.0, 100.3]
    frame.loc[39, ["open", "high", "low", "close"]] = [100.6, 100.75, 100.55, 100.7]
    cross_ts = int(frame.timestamp.iloc[-3])
    monkeypatch.setattr(confluence, "validate_dual_ema_vwap_entry_gate", lambda *a, **k: {
        "ready": True, "side": "BUY", "reason": "fresh_closed_ema9_vwap_cross",
        "ema9_cross": "BULLISH", "ema9_cross_timestamp": cross_ts,
        "ema50": None, "ema50_alignment": "UNAVAILABLE",
        "signal_candle_id": f"BUY:{cross_ts}",
        "cross_quality": "UNRATED_PENDING_CALIBRATION",
    })
    monkeypatch.setattr(confluence, "compute_session_vwap", lambda closed, timeframe_seconds, now_ms=None: pd.Series(100.0, index=closed.index))
    result = validate_adaptive_ema9_vwap_entry_gate(
        frame, "BUY", lambda values, period: pd.Series(100.0, index=values.index),
        timeframe_seconds=900, now_ms=now_ms, current_price=100.5,
        lookback_candles=5, entry_zone_low=99.8, entry_zone_high=100.2,
    )
    assert result["ready"] is True
    assert result["entry_mode"] == "MICRO_PULLBACK"
    assert result["micro_pullback_candle_timestamp"] == int(frame.timestamp.iloc[-2])


def test_later_close_through_zone_invalidates_an_earlier_micro_reclaim(monkeypatch):
    frame, now_ms = _frame("plain")
    frame.loc[37, ["open", "high", "low", "close"]] = [100.0, 100.5, 99.9, 100.35]
    frame.loc[38, ["open", "high", "low", "close"]] = [100.2, 100.45, 100.0, 100.3]
    frame.loc[39, ["open", "high", "low", "close"]] = [100.0, 100.1, 99.0, 99.3]
    cross_ts = int(frame.timestamp.iloc[-3])
    monkeypatch.setattr(confluence, "validate_dual_ema_vwap_entry_gate", lambda *a, **k: {
        "ready": True, "side": "BUY", "reason": "fresh_closed_ema9_vwap_cross",
        "ema9_cross": "BULLISH", "ema9_cross_timestamp": cross_ts,
        "ema50": None, "ema50_alignment": "UNAVAILABLE",
        "signal_candle_id": f"BUY:{cross_ts}",
        "cross_quality": "UNRATED_PENDING_CALIBRATION",
    })
    monkeypatch.setattr(confluence, "compute_session_vwap", lambda closed, timeframe_seconds, now_ms=None: pd.Series(100.0, index=closed.index))
    result = validate_adaptive_ema9_vwap_entry_gate(
        frame, "BUY", lambda values, period: pd.Series(100.0, index=values.index),
        timeframe_seconds=900, now_ms=now_ms, current_price=100.1,
        lookback_candles=5, entry_zone_low=99.8, entry_zone_high=100.2,
    )
    assert result["ready"] is False
    assert result["entry_mode"] == "MICRO_PULLBACK"
    assert result["micro_pullback_reclaimed"] is False


def test_sell_cross_uses_mirrored_direct_entry_rules(monkeypatch):
    frame, now_ms = _frame("plain")
    frame.loc[39, ["open", "high", "low", "close"]] = [100.1, 100.4, 99.4, 99.6]
    cross_ts = int(frame.timestamp.iloc[-3])
    monkeypatch.setattr(confluence, "validate_dual_ema_vwap_entry_gate", lambda *a, **k: {
        "ready": True, "side": "SELL", "reason": "fresh_closed_ema9_vwap_cross",
        "ema9_cross": "BEARISH", "ema9_cross_timestamp": cross_ts,
        "ema50": None, "ema50_alignment": "UNAVAILABLE",
        "signal_candle_id": f"SELL:{cross_ts}",
        "cross_quality": "UNRATED_PENDING_CALIBRATION",
    })
    monkeypatch.setattr(confluence, "compute_session_vwap", lambda closed, timeframe_seconds, now_ms=None: pd.Series(100.0, index=closed.index))
    result = validate_adaptive_ema9_vwap_entry_gate(
        frame, "SELL", lambda values, period: pd.Series(100.0, index=values.index),
        timeframe_seconds=900, now_ms=now_ms, current_price=99.95,
        lookback_candles=5, entry_zone_low=99.8, entry_zone_high=100.2,
    )
    assert result["ready"] is True
    assert result["entry_mode"] == "DIRECT_ENTRY"
    assert result["side"] == "SELL"


def test_micro_reclaim_is_not_executable_after_live_price_loses_zone(monkeypatch):
    frame, now_ms = _frame("plain")
    frame.loc[37, ["open", "high", "low", "close"]] = [100.0, 100.5, 99.9, 100.35]
    frame.loc[38, ["open", "high", "low", "close"]] = [100.35, 100.75, 100.1, 100.65]
    frame.loc[39, ["open", "high", "low", "close"]] = [100.4, 100.8, 100.3, 100.7]
    cross_ts = int(frame.timestamp.iloc[-3])
    monkeypatch.setattr(confluence, "validate_dual_ema_vwap_entry_gate", lambda *a, **k: {
        "ready": True, "side": "BUY", "reason": "fresh_closed_ema9_vwap_cross",
        "ema9_cross": "BULLISH", "ema9_cross_timestamp": cross_ts,
        "ema50": None, "ema50_alignment": "UNAVAILABLE",
        "signal_candle_id": f"BUY:{cross_ts}",
        "cross_quality": "UNRATED_PENDING_CALIBRATION",
    })
    monkeypatch.setattr(confluence, "compute_session_vwap", lambda closed, timeframe_seconds, now_ms=None: pd.Series(99.5, index=closed.index))
    result = validate_adaptive_ema9_vwap_entry_gate(
        frame, "BUY", lambda values, period: pd.Series(99.5, index=values.index),
        timeframe_seconds=900, now_ms=now_ms, current_price=100.1,
        lookback_candles=5, entry_zone_low=100.2, entry_zone_high=100.5,
    )
    assert result["ready"] is False
    assert result["micro_pullback_reclaimed"] is True
    assert result["micro_pullback_current_zone_hold"] is False
