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
