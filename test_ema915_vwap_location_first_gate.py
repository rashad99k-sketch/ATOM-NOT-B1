import numpy as np
import pandas as pd

import roro_confluence as confluence


def _market(side="BUY"):
    n = 60
    start = 1795132800000
    if side == "BUY":
        rows = [{
            "timestamp": start + i * 900_000,
            "open": 101.1, "high": 101.4, "low": 101.0, "close": 101.2,
            "volume": 1000.0,
        } for i in range(n)]
        rows[55].update({"open": 100.8, "high": 101.3, "low": 100.55, "close": 101.0})
        rows[56].update({"open": 100.8, "high": 101.3, "low": 100.7, "close": 101.2})
        rows[59].update({"open": 101.2, "high": 101.6, "low": 101.1, "close": 101.5})
    else:
        rows = [{
            "timestamp": start + i * 900_000,
            "open": 98.9, "high": 99.0, "low": 98.6, "close": 98.8,
            "volume": 1000.0,
        } for i in range(n)]
        rows[55].update({"open": 99.2, "high": 99.45, "low": 98.6, "close": 99.0})
        rows[56].update({"open": 99.2, "high": 99.3, "low": 98.6, "close": 98.8})
        rows[59].update({"open": 98.8, "high": 98.9, "low": 98.3, "close": 98.4})
    return pd.DataFrame(rows)


def _ema(side):
    def calculate(values, period):
        if side == "BUY":
            fast_before, slow_before, fast_after, slow_after = 100.2, 100.4, 100.8, 100.5
        else:
            fast_before, slow_before, fast_after, slow_after = 100.4, 100.2, 99.2, 99.5
        arr = np.full(len(values), fast_before if period == 9 else slow_before, dtype=float)
        arr[52:] = fast_after if period == 9 else slow_after
        return pd.Series(arr, index=values.index)
    return calculate


def _run(monkeypatch, side="BUY", location=None, price=None, target=None):
    frame = _market(side)
    monkeypatch.setattr(
        confluence, "compute_session_vwap",
        lambda closed, timeframe_seconds, now_ms=None: pd.Series(100.0, index=closed.index, dtype=float),
    )
    now_ms = int(frame.timestamp.iloc[-1] + 901_000)
    live = price if price is not None else (101.3 if side == "BUY" else 98.7)
    return confluence.validate_ema915_vwap_location_first_entry_gate(
        frame, side, _ema(side), timeframe_seconds=900, now_ms=now_ms,
        current_price=live, max_cross_age_bars=8, pullback_lookback_bars=5,
        max_extension_atr=1.5, location_context=location or {}, target_price=target,
    )


def test_buy_entry_requires_cross_pullback_rejection_and_later_break(monkeypatch):
    result = _run(monkeypatch, "BUY")
    assert result["ready"] is True
    assert result["cross_direction"] == "BULLISH"
    assert result["pullback_touch"] is True
    assert result["rejection_confirmed"] is True
    assert result["continuation_break"] is True
    assert result["signal_candle_timestamp"] == result["latest_closed_timestamp"]
    assert result["signal_candle_id"].count(":") == 3


def test_sell_is_directional_mirror(monkeypatch):
    result = _run(monkeypatch, "SELL")
    assert result["ready"] is True
    assert result["cross_direction"] == "BEARISH"
    assert result["pullback_touch"] is True
    assert result["rejection_confirmed"] is True
    assert result["continuation_break"] is True


def test_missing_institutional_zone_is_non_blocking(monkeypatch):
    result = _run(monkeypatch, "BUY", location={})
    assert result["ready"] is True
    assert result["location_status"] == "UNKNOWN_NON_BLOCKING"


def test_nearby_resistance_blocks_buy_only_when_it_consumes_tp1_room(monkeypatch):
    location = {"support_resistance": {"resistances": [{
        "low": 101.32, "high": 101.55, "status": "CONFIRMED",
        "confirmed_at_timestamp": 1795190000000,
    }]}}
    result = _run(monkeypatch, "BUY", location=location, target=102.0)
    assert result["ready"] is False
    assert result["decision_state"] == "BLOCKED_BY_LOCATION"
    assert result["location_status"] == "OPPOSING_ZONE_TOO_CLOSE"


def test_resistance_beyond_tp1_does_not_block_buy(monkeypatch):
    location = {"support_resistance": {"resistances": [{
        "low": 102.2, "high": 102.5, "status": "CONFIRMED",
        "confirmed_at_timestamp": 1795190000000,
    }]}}
    result = _run(monkeypatch, "BUY", location=location, target=102.0)
    assert result["ready"] is True


def test_nearby_support_blocks_sell_only_when_it_consumes_tp1_room(monkeypatch):
    location = {"support_resistance": {"supports": [{
        "low": 98.45, "high": 98.72, "status": "CONFIRMED",
        "confirmed_at_timestamp": 1795190000000,
    }]}}
    result = _run(monkeypatch, "SELL", location=location, target=97.8)
    assert result["ready"] is False
    assert result["decision_state"] == "BLOCKED_BY_LOCATION"


def test_no_chase_uses_live_price_extension_in_atr(monkeypatch):
    result = _run(monkeypatch, "BUY", price=102.0)
    assert result["ready"] is False
    assert result["decision_state"] == "NO_CHASE"
    assert result["reason"] == "entry_window_missed_no_chase"


def test_wrong_side_cross_cannot_authorize(monkeypatch):
    result = _run(monkeypatch, "SELL", price=101.0)
    assert result["ready"] is False
    assert result["reason"] == "live_price_vwap_alignment_lost"
    assert result["decision_state"] == "INVALIDATED"


def test_closed_candle_freshness_is_required(monkeypatch):
    frame = _market("BUY")
    monkeypatch.setattr(
        confluence, "compute_session_vwap",
        lambda closed, timeframe_seconds, now_ms=None: pd.Series(100.0, index=closed.index, dtype=float),
    )
    result = confluence.validate_ema915_vwap_location_first_entry_gate(
        frame, "BUY", _ema("BUY"), timeframe_seconds=900,
        now_ms=int(frame.timestamp.iloc[-1] + 4 * 900_000), current_price=101.3,
    )
    assert result["ready"] is False
    assert result["reason"] == "stale_closed_candle_data"
