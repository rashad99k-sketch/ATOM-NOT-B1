import numpy as np
import pandas as pd

import roro_confluence as confluence


def _frame(side="BUY", count=80):
    start = 1795132800000
    rows = []
    for i in range(count):
        rows.append({
            "timestamp": start + i * 900_000,
            "open": 100.00,
            "high": 100.20,
            "low": 99.80,
            "close": 100.00,
            "volume": 1000.0,
        })
    frame = pd.DataFrame(rows)
    frame.loc[count - 25:count - 2, "volume"] = 800.0
    frame.loc[count - 1, "volume"] = 1200.0
    if side == "BUY":
        # Pullback, rejection and breakout occupy distinct closed candles.
        frame.loc[count - 3, ["open", "high", "low", "close"]] = [100.05, 100.22, 99.90, 100.05]
        frame.loc[count - 2, ["open", "high", "low", "close"]] = [100.00, 100.30, 99.85, 100.22]
        frame.loc[count - 1, ["open", "high", "low", "close"]] = [100.15, 100.60, 100.05, 100.50]
    else:
        frame.loc[count - 3, ["open", "high", "low", "close"]] = [99.95, 100.10, 99.78, 99.95]
        frame.loc[count - 2, ["open", "high", "low", "close"]] = [100.00, 100.15, 99.70, 99.80]
        frame.loc[count - 1, ["open", "high", "low", "close"]] = [99.85, 100.00, 99.40, 99.50]
    return frame


def _ema_factory(side="BUY"):
    def ema(values, period):
        if period == 9:
            out = np.full(len(values), 99.8 if side == "BUY" else 100.2, dtype=float)
            out[-4:] = 100.2 if side == "BUY" else 100.0
        elif period == 15:
            out = np.full(len(values), 100.0 if side == "BUY" else 100.1, dtype=float)
        else:
            raise AssertionError(f"unexpected EMA period: {period}")
        return pd.Series(out, index=values.index)
    return ema


def _run(monkeypatch, side="BUY", frame=None, vwap_value=None, current_price=None, now_offset=1_000):
    frame = frame if frame is not None else _frame(side)
    vwap_value = vwap_value if vwap_value is not None else (99.5 if side == "BUY" else 100.5)
    monkeypatch.setattr(
        confluence, "compute_session_vwap",
        lambda closed, timeframe_seconds, now_ms=None: pd.Series(vwap_value, index=closed.index, dtype=float),
    )
    now_ms = int(frame.timestamp.iloc[-1] + 900_000 + now_offset)
    price = current_price if current_price is not None else (100.50 if side == "BUY" else 99.50)
    return confluence.validate_ema915_vwap_pullback_entry_gate(
        frame, side, _ema_factory(side), timeframe_seconds=900, now_ms=now_ms,
        cross_lookback_candles=8, pullback_lookback_candles=5, current_price=price,
    )


def test_buy_requires_ema9_ema15_cross_vwap_pullback_rejection_and_break(monkeypatch):
    result = _run(monkeypatch, "BUY")
    assert result["ready"] is True
    assert result["cross_direction"] == "BULLISH"
    assert result["pullback_touch"] is True
    assert result["rejection_confirmed"] is True
    assert result["continuation_break"] is True
    assert "EMA9_EMA15" in result["signal_candle_id"]


def test_sell_is_directional_mirror_of_buy(monkeypatch):
    result = _run(monkeypatch, "SELL")
    assert result["ready"] is True
    assert result["cross_direction"] == "BEARISH"
    assert result["pullback_touch"] is True
    assert result["continuation_break"] is True


def test_missing_ema9_ema15_cross_fails_closed(monkeypatch):
    frame = _frame("BUY")
    def no_cross(values, period):
        value = 100.2 if period == 9 else 100.0
        return pd.Series(np.full(len(values), value), index=values.index)
    monkeypatch.setattr(
        confluence, "compute_session_vwap",
        lambda closed, timeframe_seconds, now_ms=None: pd.Series(99.5, index=closed.index),
    )
    result = confluence.validate_ema915_vwap_pullback_entry_gate(
        frame, "BUY", no_cross, now_ms=int(frame.timestamp.iloc[-1] + 901_000)
    )
    assert result["ready"] is False
    assert result["reason"] == "missing_fresh_ema9_ema15_cross"
    # Indicators were successfully calculated; only the fresh crossover is absent.
    assert result["indicator_status"] == "COMPUTED"
    assert result["ema9"] == 100.2
    assert result["ema15"] == 100.0
    assert result["vwap"] == 99.5
    assert result["latest_closed_timestamp"] == int(frame.timestamp.iloc[-1])
    assert result["closed_candle_count"] == len(frame)


def test_three_recent_vwap_crosses_block_choppy_entry(monkeypatch):
    frame = _frame("BUY")
    def whipsaw_ema(values, period):
        if period == 9:
            out = np.full(len(values), 99.8, dtype=float)
            out[-8:] = [99.4, 99.6, 99.4, 99.6, 99.4, 99.6, 100.2, 100.2]
        else:
            out = np.full(len(values), 100.0, dtype=float)
        return pd.Series(out, index=values.index)
    monkeypatch.setattr(
        confluence, "compute_session_vwap",
        lambda closed, timeframe_seconds, now_ms=None: pd.Series(99.5, index=closed.index),
    )
    result = confluence.validate_ema915_vwap_pullback_entry_gate(
        frame, "BUY", whipsaw_ema, timeframe_seconds=900,
        now_ms=int(frame.timestamp.iloc[-1] + 901_000), current_price=100.5,
    )
    assert result["ready"] is False
    assert result["reason"] == "excessive_vwap_whipsaw"
    assert result["ema9_vwap_crosses_last_8"] >= 3


def test_vwap_direction_must_agree_with_ema_cross(monkeypatch):
    result = _run(monkeypatch, "BUY", vwap_value=100.4)
    assert result["ready"] is False
    assert result["reason"] == "ema915_or_vwap_direction_not_aligned"


def test_pullback_is_mandatory_after_cross(monkeypatch):
    frame = _frame("BUY")
    frame.loc[len(frame) - 3, ["open", "high", "low", "close"]] = [100.60, 100.80, 100.55, 100.70]
    frame.loc[len(frame) - 2, ["open", "high", "low", "close"]] = [100.60, 100.80, 100.55, 100.70]
    frame.loc[len(frame) - 1, ["open", "high", "low", "close"]] = [100.60, 100.90, 100.55, 100.80]
    result = _run(monkeypatch, "BUY", frame=frame)
    assert result["ready"] is False
    assert result["reason"] == "waiting_for_ema_band_pullback"


def test_rejection_and_continuation_break_are_separate_requirements(monkeypatch):
    frame = _frame("BUY")
    # A bullish candle that fails to close above the previous candle high.
    frame.loc[len(frame) - 1, ["open", "high", "low", "close"]] = [100.10, 100.30, 100.00, 100.25]
    result = _run(monkeypatch, "BUY", frame=frame)
    assert result["ready"] is False
    assert result["reason"] == "waiting_for_continuation_break"


def test_continuation_without_rejection_wick_is_not_authorized(monkeypatch):
    frame = _frame("BUY")
    frame.loc[len(frame) - 2, ["open", "high", "low", "close"]] = [100.05, 100.30, 100.05, 100.25]
    result = _run(monkeypatch, "BUY", frame=frame)
    assert result["ready"] is False
    assert result["reason"] == "waiting_for_directional_rejection"





def test_quality_breakout_is_blocked_when_volume_does_not_expand(monkeypatch):
    frame = _frame("BUY")
    frame.loc[len(frame) - 1, "volume"] = 800.0
    result = _run(monkeypatch, "BUY", frame=frame)
    assert result["ready"] is False
    assert result["reason"] == "continuation_break_volume_not_confirmed"
    assert result["pullback_timestamp"] is not None
    assert result["rejection_timestamp"] is not None


def test_wick_only_breakout_is_classified_false_and_blocked(monkeypatch):
    frame = _frame("BUY")
    frame.loc[len(frame) - 1, ["open", "high", "low", "close"]] = [100.35, 100.62, 100.05, 100.25]
    result = _run(monkeypatch, "BUY", frame=frame)
    assert result["ready"] is False
    assert result["reason"] == "false_breakout_close_back_inside"
    assert result["false_breakout"] is True
    assert result["continuation_break"] is False


def test_same_candle_cannot_be_rejection_and_breakout(monkeypatch):
    frame = _frame("BUY")
    frame.loc[len(frame) - 2, ["open", "high", "low", "close"]] = [100.20, 100.35, 100.10, 100.18]
    result = _run(monkeypatch, "BUY", frame=frame)
    assert result["ready"] is False
    assert result["reason"] == "waiting_for_directional_rejection"


def test_no_chase_guard_uses_actual_ticker_price(monkeypatch):
    result = _run(monkeypatch, "BUY", current_price=102.0)
    assert result["ready"] is False
    assert result["reason"] == "entry_window_missed_no_chase"


def test_stale_closed_candles_cannot_authorize_entry(monkeypatch):
    frame = _frame("BUY")
    monkeypatch.setattr(
        confluence, "compute_session_vwap",
        lambda closed, timeframe_seconds, now_ms=None: pd.Series(99.5, index=closed.index),
    )
    now_ms = int(frame.timestamp.iloc[-1] + 4 * 900_000)
    result = confluence.validate_ema915_vwap_pullback_entry_gate(
        frame, "BUY", _ema_factory("BUY"), timeframe_seconds=900, now_ms=now_ms,
        current_price=100.5,
    )
    assert result["ready"] is False
    assert result["reason"] == "stale_closed_candle_data"


def test_all_execution_paths_use_primary_gate_and_live_last_mile_revalidates():
    from pathlib import Path
    source = Path("ggpbot.py").read_text(encoding="utf-8")
    execute_start = source.index("def execute_entry(")
    execute_end = source.find("\ndef ", execute_start + 1)
    execute = source[execute_start:execute_end]
    assert "_validate_ema915_vwap_location_first_entry_gate(" in execute
    assert "_validate_adaptive_ema9_vwap_entry_gate(" not in execute
    assert "_validate_dual_ema_vwap_entry_gate(" not in execute
    open_position = source[source.index("def open_position("):]
    assert "[EMA9_VWAP_FINAL_GATE]" in open_position
    assert "_validate_ema915_vwap_location_first_entry_gate(" in open_position
    assert "_validate_adaptive_ema9_vwap_entry_gate(" not in open_position
    assert 'final_gate["gate_kind"] = "EMA915_VWAP_LOCATION_FIRST"' in open_position
    assert "entry_gate_snapshot" in open_position
    assert "submit_market_entry(" in open_position
    assert "PARTIAL_FILL_RECOVERY_HOLD" in open_position
    scaling = source[source.index("def scaling_logic("):source.index("def council_exit(")]
    assert "_validate_ema915_vwap_location_first_entry_gate(" in scaling
    assert "EMA915_SCALE_IN_BLOCK" in scaling
    assert scaling.index('STATE["qty"] += actual_scale_qty') < scaling.index("journal.consume_event(event_id, scale_trade_id)")


def _macro_frame():
    frame = _frame("BUY", count=80)
    # Confirmed pre-cross swing low. The macro retest must not damage it.
    frame.loc[70, ["open", "high", "low", "close"]] = [99.5, 100.0, 98.0, 99.4]
    # Cross candle, followed by a deep touch at EMA50 + validated OB zone,
    # then a distinct rejection candle, then a later quality breakout close.
    frame.loc[76, ["open", "high", "low", "close"]] = [100.0, 101.2, 99.9, 100.6]
    frame.loc[77, ["open", "high", "low", "close"]] = [99.9, 100.4, 98.9, 99.8]
    frame.loc[78, ["open", "high", "low", "close"]] = [99.75, 100.35, 99.65, 100.25]
    frame.loc[79, ["open", "high", "low", "close"]] = [100.1, 100.8, 100.0, 100.7]
    frame.loc[79, "volume"] = 1400.0
    return frame


def _macro_ema(values, period):
    n = len(values)
    if period == 9:
        out = np.full(n, 99.8, dtype=float)
        out[-4:] = [100.2, 100.2, 100.2, 100.2]
    elif period == 15:
        out = np.full(n, 100.0, dtype=float)
    elif period == 20:
        out = np.full(n, 100.4, dtype=float)
        out[-4:] = [100.55, 100.65, 100.75, 100.85]
    elif period == 50:
        out = np.full(n, 99.0, dtype=float)
        out[-4:] = [99.0, 99.05, 99.10, 99.15]
    else:
        raise AssertionError(f"unexpected EMA period: {period}")
    return pd.Series(out, index=values.index)


def test_macro_pullback_requires_ema50_causal_zone_protected_swing_rejection_and_break(monkeypatch):
    frame = _macro_frame()
    monkeypatch.setattr(
        confluence, "compute_session_vwap",
        lambda closed, timeframe_seconds, now_ms=None: pd.Series(99.5, index=closed.index, dtype=float),
    )
    result = confluence.validate_ema915_vwap_pullback_entry_gate(
        frame, "BUY", _macro_ema, timeframe_seconds=900,
        now_ms=int(frame.timestamp.iloc[-1] + 901_000), current_price=100.7,
        cross_lookback_candles=8, pullback_lookback_candles=5,
        entry_zone_low=98.8, entry_zone_high=99.3,
        entry_zone_available_timestamp=int(frame.timestamp.iloc[73]),
    )
    assert result["ready"] is True
    assert result["entry_mode"] == "MACRO_PULLBACK"
    assert result["macro_pullback"] is True
    assert result["protected_swing_level"] == 98.0
    assert result["pullback_touch"] is True
    assert result["rejection_confirmed"] is True
    assert result["continuation_break"] is True
    assert result["authorization_state"] == "EMA915_VWAP_MACRO_PULLBACK_VALID"


def test_macro_pullback_cannot_use_zone_before_zone_creation_timestamp(monkeypatch):
    frame = _macro_frame()
    monkeypatch.setattr(
        confluence, "compute_session_vwap",
        lambda closed, timeframe_seconds, now_ms=None: pd.Series(99.5, index=closed.index, dtype=float),
    )
    result = confluence.validate_ema915_vwap_pullback_entry_gate(
        frame, "BUY", _macro_ema, timeframe_seconds=900,
        now_ms=int(frame.timestamp.iloc[-1] + 901_000), current_price=100.7,
        cross_lookback_candles=8, pullback_lookback_candles=5,
        entry_zone_low=98.8, entry_zone_high=99.3,
        entry_zone_available_timestamp=int(frame.timestamp.iloc[78]),
    )
    assert result["ready"] is False
    assert result["entry_mode"] == "WAITING_FOR_PULLBACK"
    assert result["macro_pullback"] is False


def test_macro_pullback_requires_a_confirmed_pre_cross_swing(monkeypatch):
    frame = _macro_frame()
    frame.loc[70, ["open", "high", "low", "close"]] = [99.9, 100.0, 99.8, 99.9]
    monkeypatch.setattr(
        confluence, "compute_session_vwap",
        lambda closed, timeframe_seconds, now_ms=None: pd.Series(99.5, index=closed.index, dtype=float),
    )
    result = confluence.validate_ema915_vwap_pullback_entry_gate(
        frame, "BUY", _macro_ema, timeframe_seconds=900,
        now_ms=int(frame.timestamp.iloc[-1] + 901_000), current_price=100.7,
        cross_lookback_candles=8, pullback_lookback_candles=5,
        entry_zone_low=98.8, entry_zone_high=99.3,
        entry_zone_available_timestamp=int(frame.timestamp.iloc[73]),
    )
    assert result["ready"] is False
    assert result.get("macro_pullback") is False
    assert result["entry_mode"] == "WAITING_FOR_PULLBACK"


def test_institutional_zone_retest_is_separate_from_ema50_macro_pullback(monkeypatch):
    frame = _macro_frame()
    def retest_ema(values, period):
        if period == 9:
            out = np.full(len(values), 99.8, dtype=float)
            out[-4:] = 100.2
        elif period == 15:
            out = np.full(len(values), 100.0, dtype=float)
        elif period == 20:
            out = np.full(len(values), 100.4, dtype=float)
            out[-4:] = [100.55, 100.65, 100.75, 100.85]
        elif period == 50:
            out = np.full(len(values), 95.0, dtype=float)
            out[-4:] = [95.0, 95.05, 95.10, 95.15]
        else:
            raise AssertionError(f"unexpected EMA period: {period}")
        return pd.Series(out, index=values.index)
    monkeypatch.setattr(
        confluence, "compute_session_vwap",
        lambda closed, timeframe_seconds, now_ms=None: pd.Series(99.5, index=closed.index, dtype=float),
    )
    result = confluence.validate_ema915_vwap_pullback_entry_gate(
        frame, "BUY", retest_ema, timeframe_seconds=900,
        now_ms=int(frame.timestamp.iloc[-1] + 901_000), current_price=100.7,
        cross_lookback_candles=8, pullback_lookback_candles=5,
        entry_zone_low=98.8, entry_zone_high=99.3,
        entry_zone_available_timestamp=int(frame.timestamp.iloc[73]),
    )
    assert result["ready"] is True
    assert result["entry_mode"] == "RETEST"
    assert result["macro_pullback"] is False
    assert result["authorization_state"] == "EMA915_VWAP_RETEST_VALID"


def test_macro_alignment_exception_never_weakens_reversal_vwap_gate(monkeypatch):
    frame = _frame("BUY")
    def ema_with_macro_trend(values, period):
        if period == 9:
            out = np.full(len(values), 99.8, dtype=float)
            out[-4:] = 100.2
        elif period == 15:
            out = np.full(len(values), 100.0, dtype=float)
        elif period == 20:
            out = np.full(len(values), 100.3, dtype=float)
            out[-4:] = [100.3, 100.4, 100.5, 100.6]
        elif period == 50:
            out = np.full(len(values), 99.8, dtype=float)
            out[-4:] = [99.8, 99.9, 99.95, 100.0]
        else:
            raise AssertionError(f"unexpected EMA period: {period}")
        return pd.Series(out, index=values.index)
    monkeypatch.setattr(
        confluence, "compute_session_vwap",
        lambda closed, timeframe_seconds, now_ms=None: pd.Series(100.4, index=closed.index, dtype=float),
    )
    result = confluence.validate_ema915_vwap_pullback_entry_gate(
        frame, "BUY", ema_with_macro_trend, timeframe_seconds=900,
        now_ms=int(frame.timestamp.iloc[-1] + 901_000), current_price=100.5,
        setup_mode="REVERSAL",
    )
    assert result["macro_trend_aligned"] is True
    assert result["ready"] is False
    assert result["reason"] == "ema915_or_vwap_direction_not_aligned"


def test_macro_retest_is_invalidated_if_protected_swing_breaks_before_rejection(monkeypatch):
    frame = _macro_frame()
    # The retest touches the causal zone on candle 77, but candle 78 closes
    # decisively through the pre-cross swing low before any rejection can form.
    frame.loc[78, ["open", "high", "low", "close"]] = [99.8, 100.0, 97.5, 97.8]
    monkeypatch.setattr(
        confluence, "compute_session_vwap",
        lambda closed, timeframe_seconds, now_ms=None: pd.Series(99.5, index=closed.index, dtype=float),
    )
    result = confluence.validate_ema915_vwap_pullback_entry_gate(
        frame, "BUY", _macro_ema, timeframe_seconds=900,
        now_ms=int(frame.timestamp.iloc[-1] + 901_000), current_price=100.7,
        cross_lookback_candles=8, pullback_lookback_candles=5,
        entry_zone_low=98.8, entry_zone_high=99.3,
        entry_zone_available_timestamp=int(frame.timestamp.iloc[73]),
    )
    assert result["ready"] is False
    assert result["entry_mode"] == "INVALIDATED"
    assert result["reason"] == "retest_swing_invalidated"
    assert result["retest_invalidated"] is True
