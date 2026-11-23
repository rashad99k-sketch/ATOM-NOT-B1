from types import SimpleNamespace

import pandas as pd

import ggp_institutional_bridge as bridge
from roro_institutional import InstitutionalDecision, DecisionState


def _frame():
    start = 1795132800000
    return pd.DataFrame([
        {"timestamp": start + i * 900_000, "open": 100.0, "high": 101.0,
         "low": 99.0, "close": 100.5, "volume": 1000.0}
        for i in range(80)
    ])


def _context():
    return {
        "atr": 1.0, "price": 100.5,
        "regime": SimpleNamespace(state="TREND", direction="BULLISH"),
        "structure": {"confirmed": False, "direction": "TRANSITION", "bos": None},
        "liquidity": SimpleNamespace(event="NONE", direction="NEUTRAL", level=None, confidence=0.0, reasons=[]),
        "order_block": None, "fvg": None,
        "support_resistance": {"status": "UNKNOWN", "supports": [], "resistances": []},
        "support_resistance_aligned": False,
        "volume_ratio": 1.0, "volume_confirmed": False,
        "golden_zone": {"state": "UNKNOWN", "score": 0.0},
    }


def _patch_common(monkeypatch, gate):
    ctx = _context()
    monkeypatch.setattr(bridge, "_frame", lambda frame: frame.copy())
    monkeypatch.setattr(bridge, "validate_closed_frame", lambda frame, now_ms=None, timeframe_s=900: frame.copy())
    monkeypatch.setattr(bridge, "_build_context", lambda df, side: ctx)
    monkeypatch.setattr(bridge, "_adx", lambda df: 18.0)
    monkeypatch.setattr(bridge, "build_intelligence_decision", lambda **kwargs: SimpleNamespace(to_dict=lambda: {
        "mode": "TREND", "confidence": 50.0, "reference_low": None, "reference_high": None,
    }))
    monkeypatch.setattr(bridge, "classify_price_pattern", lambda *a, **k: "NONE")
    monkeypatch.setattr(bridge.AUTHORITY, "decide", lambda *a, **k: InstitutionalDecision(
        state=DecisionState.WAITING_FOR_RETEST.value, setup_type="TREND_CONTINUATION_RETEST",
        side="BUY", reason="legacy_structure_pending", score=12.0,
    ))
    monkeypatch.setattr(bridge, "validate_ema915_vwap_location_first_entry_gate", lambda *a, **k: dict(gate))


def _ready_gate():
    return {
        "ready": True, "decision_state": "ENTRY_READY",
        "entry_mode": "PULLBACK_RETEST_REJECTION_BREAK",
        "reason": "ema915_cross_pullback_rejection_break_vwap_aligned_location_clear",
        "cross_direction": "BULLISH", "cross_timestamp": 1795132800000,
        "pullback_timestamp": 1795190000000, "signal_candle_timestamp": 1795200000000,
        "signal_candle_id": "cross:pullback:rejection:confirmation",
        "pullback_low": 98.8, "pullback_high": 100.8,
        "entry_zone_low": 99.0, "entry_zone_high": 101.0,
        "location_status": "UNKNOWN_NON_BLOCKING",
    }


def test_ema_cross_lane_can_authorize_without_ob_sweep_or_mss(monkeypatch):
    _patch_common(monkeypatch, _ready_gate())
    result = bridge.analyze_symbol("BTC/USDT:USDT", "BUY", _frame(), now_ms=1795204801000)
    assert result["decision_state"] == "ENTRY_READY"
    assert result["decision_setup_type"] == "EMA9_VWAP_ADAPTIVE"
    assert result["structural_stop"] < result["price"]
    assert "closed_ema9_ema15_cross" in result["evidence"]
    assert "separate_directional_rejection_candle" in result["evidence"]
    assert any(item.startswith("location_filter:") for item in result["evidence"])


def test_missing_institutional_zone_is_non_blocking_location_context(monkeypatch):
    _patch_common(monkeypatch, _ready_gate())
    result = bridge.analyze_symbol("BTC/USDT:USDT", "BUY", _frame(), now_ms=1795204801000)
    assert result["decision_state"] == "ENTRY_READY"
    assert result["adaptive_entry_gate"]["location_status"] == "UNKNOWN_NON_BLOCKING"


def test_location_block_keeps_entry_out_of_ready_state(monkeypatch):
    gate = _ready_gate()
    gate.update({
        "ready": False, "decision_state": "BLOCKED_BY_LOCATION",
        "reason": "opposing_institutional_zone_blocks_available_room",
        "location_status": "OPPOSING_ZONE_TOO_CLOSE",
    })
    _patch_common(monkeypatch, gate)
    result = bridge.analyze_symbol("BTC/USDT:USDT", "BUY", _frame(), now_ms=1795204801000)
    assert result["decision_state"] == "NO_CHASE"
    assert result["structural_stop"] is None


def test_incomplete_pullback_waits_without_institutional_pattern_veto(monkeypatch):
    gate = _ready_gate()
    gate.update({
        "ready": False, "decision_state": "WAITING_FOR_REJECTION",
        "reason": "waiting_for_separate_directional_rejection",
    })
    _patch_common(monkeypatch, gate)
    result = bridge.analyze_symbol("BTC/USDT:USDT", "BUY", _frame(), now_ms=1795204801000)
    assert result["decision_state"] == "WAITING_FOR_CONFIRMATION"
    assert result["decision_reason"] == "waiting_for_separate_directional_rejection"


def test_scanner_snapshot_uses_only_defined_adaptive_side_map_and_progress_map():
    from pathlib import Path
    source = Path(__file__).with_name("ggpbot.py").read_text(encoding="utf-8")
    assert "_progress_by_stage" not in source
    assert "_ema915_checks" not in source
    assert '"ema9_vwap_adaptive_strategy": _strategy_snapshot' in source
    assert '"MACRO_PULLBACK_READY"' in source
    assert '"WAITING_FOR_CONTINUATION_BREAK"' in source
    assert '"EMA9_EMA15_SESSION_VWAP_PULLBACK"' in source
