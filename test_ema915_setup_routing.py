import pandas as pd

from roro_confluence import validate_ema915_vwap_pullback_entry_gate


def _ema(series, length):
    return series.ewm(span=length, adjust=False).mean()


def _cross_frame():
    # Stable pre-cross market followed by a modest, closed-candle bullish move.
    # The move is intentionally not a full EMA-band pullback + breakout sequence.
    closes = [100.0] * 32 + [
        100.02, 100.04, 100.06, 100.08, 100.10, 100.12,
    ]
    start = (1_800_000_000_000 // 86_400_000) * 86_400_000  # exact UTC session midnight
    rows = []
    for index, close in enumerate(closes):
        rows.append({
            "timestamp": start + index * 900_000,
            "open": close - 0.01,
            "high": close + 0.04,
            "low": close - 0.04,
            "close": close,
            "volume": 100.0,
        })
    frame = pd.DataFrame(rows)
    now_ms = int(frame["timestamp"].iloc[-1]) + 900_000 + 1_000
    return frame, now_ms


REVERSAL_EVIDENCE = [
    "prior_liquidity_pool:SELL_SIDE",
    "liquidity_sweep:SELL_SIDE_TAKEN",
    "reclaim:LEVEL_RECLAIMED",
    "closed_mss:BULLISH",
    "directional_trigger:BULLISH_DISPLACEMENT",
    "post_mss_retest:CONFIRMED",
]


def test_reversal_uses_crossover_as_trigger_then_requires_institutional_sweep_mss_retest():
    frame, now_ms = _cross_frame()
    result = validate_ema915_vwap_pullback_entry_gate(
        frame, "BUY", _ema, timeframe_seconds=900, now_ms=now_ms,
        current_price=float(frame["close"].iloc[-1]),
        setup_mode="LIQUIDITY_SWEEP_REVERSAL",
        institutional_evidence=REVERSAL_EVIDENCE,
        institutional_event_id="roro-event-123",
        institutional_origin_timestamp=int(frame["timestamp"].iloc[-4]),
    )

    assert result["ready"] is True
    assert result["authorization_state"] == "EMA915_INSTITUTIONAL_REVERSAL_VALID"
    assert result["liquidity_sweep_confirmed"] is True
    assert result["reclaim_confirmed"] is True
    assert result["mss_confirmed"] is True
    assert result["post_mss_retest_confirmed"] is True
    assert "EMA915_REVERSAL" in result["signal_candle_id"]
    assert "roro-event-123" in result["signal_candle_id"]


def test_reversal_cannot_enter_when_sweep_or_post_mss_retest_evidence_is_missing():
    frame, now_ms = _cross_frame()
    incomplete = [item for item in REVERSAL_EVIDENCE if "sweep" not in item and "post_mss_retest" not in item]
    result = validate_ema915_vwap_pullback_entry_gate(
        frame, "BUY", _ema, timeframe_seconds=900, now_ms=now_ms,
        current_price=float(frame["close"].iloc[-1]),
        setup_mode="LIQUIDITY_SWEEP_REVERSAL",
        institutional_evidence=incomplete,
        institutional_event_id="roro-event-incomplete",
        institutional_origin_timestamp=int(frame["timestamp"].iloc[-4]),
    )

    assert result["ready"] is False
    assert result["reason"] == "institutional_reversal_evidence_incomplete"


def test_continuation_keeps_pullback_rejection_and_breakout_requirements():
    frame, now_ms = _cross_frame()
    result = validate_ema915_vwap_pullback_entry_gate(
        frame, "BUY", _ema, timeframe_seconds=900, now_ms=now_ms,
        current_price=float(frame["close"].iloc[-1]),
        setup_mode="TREND_CONTINUATION_ENGINE",
    )

    assert result["ready"] is False
    assert result["reason"] in {
        "waiting_for_ema_band_pullback",
        "waiting_for_directional_rejection",
        "waiting_for_continuation_break",
        "false_breakout_close_back_inside",
    }


def test_reversal_cross_must_match_requested_direction():
    frame, now_ms = _cross_frame()
    result = validate_ema915_vwap_pullback_entry_gate(
        frame, "SELL", _ema, timeframe_seconds=900, now_ms=now_ms,
        current_price=float(frame["close"].iloc[-1]),
        setup_mode="LIQUIDITY_SWEEP_REVERSAL",
        institutional_evidence=REVERSAL_EVIDENCE,
        institutional_event_id="roro-event-wrong-side",
        institutional_origin_timestamp=int(frame["timestamp"].iloc[-4]),
    )

    assert result["ready"] is False
    assert result["reason"] == "ema9_ema15_cross_direction_mismatch"


def test_reversal_cannot_use_a_sweep_that_predates_the_ema915_cross():
    frame, now_ms = _cross_frame()
    result = validate_ema915_vwap_pullback_entry_gate(
        frame, "BUY", _ema, timeframe_seconds=900, now_ms=now_ms,
        current_price=float(frame["close"].iloc[-1]),
        setup_mode="LIQUIDITY_SWEEP_REVERSAL",
        institutional_evidence=REVERSAL_EVIDENCE,
        institutional_event_id="roro-event-before-cross",
        institutional_origin_timestamp=int(frame["timestamp"].iloc[20]),
    )

    assert result["ready"] is False
    assert result["reason"] == "institutional_sweep_not_after_ema915_cross"


def test_failed_auction_reversal_requires_its_own_evidence_set():
    frame, now_ms = _cross_frame()
    evidence = [
        "failed_auction:ABOVE_VALUE",
        "outside_acceptance_failed:CONFIRMED",
        "reclaim:VWAP_RECLAIMED",
        "closed_mss:BULLISH",
        "post_mss_retest:ORDER_BLOCK",
        "directional_trigger:BULLISH",
    ]
    result = validate_ema915_vwap_pullback_entry_gate(
        frame, "BUY", _ema, timeframe_seconds=900, now_ms=now_ms,
        current_price=float(frame["close"].iloc[-1]),
        setup_mode="FAILED_AUCTION_REVERSAL",
        institutional_evidence=evidence,
        institutional_event_id="failed-auction-event",
        institutional_origin_timestamp=int(frame["timestamp"].iloc[-4]),
    )
    assert result["ready"] is True
    assert result["setup_mode"] == "FAILED_AUCTION_REVERSAL"


def test_reversal_fails_closed_without_a_known_sweep_timestamp():
    frame, now_ms = _cross_frame()
    result = validate_ema915_vwap_pullback_entry_gate(
        frame, "BUY", _ema, timeframe_seconds=900, now_ms=now_ms,
        current_price=float(frame["close"].iloc[-1]),
        setup_mode="LIQUIDITY_SWEEP_REVERSAL",
        institutional_evidence=REVERSAL_EVIDENCE,
        institutional_event_id="missing-origin-event",
    )
    assert result["ready"] is False
    assert result["reason"] == "institutional_sweep_timestamp_unavailable"


def _impulse_frame(side="BUY"):
    frame, now_ms = _cross_frame()
    direction = 1.0 if side == "BUY" else -1.0
    # Match the production EMA crossover detector: EMA9 rises above/falls below
    # EMA15 at -3, then the following -2 candle is the confirmed impulse BOS.
    cross_index = len(frame) - 3
    base = 100.0
    for i in range(len(frame) - 12, len(frame)):
        if i < cross_index:
            frame.loc[i, ["open", "high", "low", "close", "volume"]] = [
                base, base + 0.04, base - 0.04, base, 100.0
            ]
    frame.loc[cross_index, ["open", "high", "low", "close", "volume"]] = [
        base, base + 0.16 * direction, base - 0.16 * direction,
        base + 0.10 * direction, 100.0
    ]
    level = 100.10 if side == "BUY" else 99.90
    open_price = level - 1.00 * direction
    close_price = level + 1.00 * direction
    low = min(open_price, close_price) - 0.05
    high = max(open_price, close_price) + 0.05
    frame.loc[len(frame) - 2, ["open", "high", "low", "close", "volume"]] = [
        open_price, high, low, close_price, 300.0
    ]
    # The last candle is a small continuation after the impulse; the gate must
    # still evaluate the impulse candle's own BOS and quality metrics.
    frame.loc[len(frame) - 1, ["open", "high", "low", "close", "volume"]] = [
        close_price, close_price + 0.08 * direction,
        close_price - 0.02 * direction, close_price + 0.05 * direction, 100.0
    ]
    live = close_price + 0.06 * direction
    return frame, int(frame.timestamp.iloc[-1] + 901_000), live


def test_continuation_allows_qualified_bullish_impulse_without_pullback():
    frame, now_ms, live = _impulse_frame("BUY")
    result = validate_ema915_vwap_pullback_entry_gate(
        frame, "BUY", _ema, timeframe_seconds=900, now_ms=now_ms,
        current_price=live, setup_mode="TREND_CONTINUATION_ENGINE",
        entry_zone_low=99.8, entry_zone_high=101.0,
        entry_zone_available_timestamp=int(frame.timestamp.iloc[-12]),
    )
    assert result["ready"] is True
    assert result["entry_mode"] == "IMPULSE_ENTRY"
    assert result["authorization_state"] == "EMA915_VWAP_IMPULSE_VALID"
    assert result["bos_confirmed"] is True
    assert result["causal_zone_confirmed"] is True


def test_continuation_allows_qualified_bearish_impulse_without_pullback():
    frame, now_ms, live = _impulse_frame("SELL")
    result = validate_ema915_vwap_pullback_entry_gate(
        frame, "SELL", _ema, timeframe_seconds=900, now_ms=now_ms,
        current_price=live, setup_mode="TREND_CONTINUATION_ENGINE",
        entry_zone_low=99.0, entry_zone_high=100.2,
        entry_zone_available_timestamp=int(frame.timestamp.iloc[-12]),
    )
    assert result["ready"] is True
    assert result["entry_mode"] == "IMPULSE_ENTRY"
    assert result["authorization_state"] == "EMA915_VWAP_IMPULSE_VALID"
    assert result["bos_confirmed"] is True


def test_impulse_requires_all_three_quality_confirmations_and_bos():
    frame, now_ms, live = _impulse_frame("BUY")
    frame.loc[len(frame)-1, "volume"] = 100.0
    result = validate_ema915_vwap_pullback_entry_gate(
        frame, "BUY", _ema, timeframe_seconds=900, now_ms=now_ms,
        current_price=live, setup_mode="TREND_CONTINUATION_ENGINE",
        entry_zone_low=99.8, entry_zone_high=101.0,
        entry_zone_available_timestamp=int(frame.timestamp.iloc[-12]),
    )
    assert result["ready"] is False


def test_impulse_no_chase_never_authorizes_late_price():
    frame, now_ms, live = _impulse_frame("BUY")
    result = validate_ema915_vwap_pullback_entry_gate(
        frame, "BUY", _ema, timeframe_seconds=900, now_ms=now_ms,
        current_price=live + 3.0, setup_mode="TREND_CONTINUATION_ENGINE",
        entry_zone_low=99.8, entry_zone_high=101.0,
        entry_zone_available_timestamp=int(frame.timestamp.iloc[-12]),
    )
    assert result["ready"] is False
    assert result["reason"] == "entry_window_missed_no_chase"
    assert result["entry_mode"] == "NO_CHASE"
