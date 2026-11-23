from types import SimpleNamespace
import pandas as pd
import numpy as np

import ggp_institutional_bridge as bridge
from roro_institutional import InstitutionalDecision, DecisionState


def _frame():
    start = 1795132800000
    return pd.DataFrame([
        {"timestamp": start + i * 900_000, "open": 100.0, "high": 101.0,
         "low": 99.0, "close": 100.5, "volume": 1000.0}
        for i in range(80)
    ])


def _context(with_zone=True, structure=True, liquidity=False):
    ob = ({
        "low": 98.5, "high": 99.2, "strength": 7.0, "distance_atr": 0.8,
        "valid": True, "status": "ACTIVE", "validation": {"valid": True, "status": "ACTIVE", "fill_pct": 0.0},
    } if with_zone else None)
    return {
        "atr": 1.0, "price": 100.5,
        "regime": SimpleNamespace(state="TREND", direction="BULLISH"),
        "structure": {"confirmed": bool(structure), "direction": "BULLISH" if structure else "TRANSITION", "bos": "BULLISH_BOS" if structure else None},
        "liquidity": SimpleNamespace(event="SWEEP" if liquidity else "NONE", direction="BULLISH" if liquidity else "NEUTRAL", level=99.0, confidence=60.0, reasons=[]),
        "order_block": ob, "fvg": None,
        "support_resistance": {"status": "UNKNOWN", "supports": [], "resistances": []},
        "support_resistance_aligned": False,
        "volume_ratio": 1.2, "volume_confirmed": True,
        "golden_zone": {"state": "SINGLE_INSTITUTIONAL_ZONE", "score": 25.0},
    }


def _patch_common(monkeypatch, ctx, gate_ready=True):
    monkeypatch.setattr(bridge, "_frame", lambda frame: frame.copy())
    monkeypatch.setattr(bridge, "validate_closed_frame", lambda frame, now_ms=None, timeframe_s=900: frame.copy())
    monkeypatch.setattr(bridge, "_build_context", lambda df, side: ctx)
    monkeypatch.setattr(bridge, "_adx", lambda df: 22.0)
    monkeypatch.setattr(bridge, "build_intelligence_decision", lambda **kwargs: SimpleNamespace(to_dict=lambda: {
        "mode": "TREND", "confidence": 70.0,
        "reference_low": (ctx["order_block"]["low"] if ctx.get("order_block") else None),
        "reference_high": (ctx["order_block"]["high"] if ctx.get("order_block") else None),
    }))
    monkeypatch.setattr(bridge, "classify_price_pattern", lambda *a, **k: "NONE")
    monkeypatch.setattr(bridge.AUTHORITY, "decide", lambda *a, **k: InstitutionalDecision(
        state=DecisionState.WAITING_FOR_RETEST.value, setup_type="TREND_CONTINUATION_RETEST",
        side="BUY", reason="waiting_for_legacy_retest", score=0.0,
    ))
    monkeypatch.setattr(bridge, "validate_ema915_vwap_pullback_entry_gate", lambda *a, **k: {
        "ready": gate_ready, "entry_mode": "MICRO_PULLBACK" if gate_ready else "WAIT",
        "authorization_state": "EMA915_VWAP_PULLBACK_VALID" if gate_ready else None,
        "trigger": "EMA9_EMA15_VWAP_PULLBACK",
        "cross_direction": "BULLISH",
        "reason": "ema915_cross_vwap_pullback_rejection_then_quality_breakout_confirmed" if gate_ready else "waiting_for_ema_band_or_macro_pullback",
        "cross_timestamp": int(ctx.get("cross_timestamp", 1795132800000)),
        "ema9_cross": "BULLISH", "ema50_alignment": "OPPOSED",
        "signal_candle_id": "BUY:EMA9_EMA15:1795132800000:PULLBACK:1:REJECTION:2:BREAK:3",
    })


def test_adaptive_cross_can_authorize_only_with_zone_and_structure_or_liquidity(monkeypatch):
    ctx = _context(with_zone=True, structure=True)
    ctx["cross_timestamp"] = 1795132800000
    _patch_common(monkeypatch, ctx)
    result = bridge.analyze_symbol("BTC/USDT:USDT", "BUY", _frame(), now_ms=1795204801000)
    assert result["decision_state"] == "ENTRY_READY"
    assert result["decision_setup_type"] == "EMA9_VWAP_ADAPTIVE"
    assert result["setup_type"] == "EMA9_VWAP_ADAPTIVE"
    assert result["structural_stop"] < result["price"]
    assert "ema9_ema15_cross_confirmed" in result["evidence"]
    assert any(item.startswith("ema915_adaptive_confirmation:") for item in result["evidence"])
    assert any(item.startswith("causal_zone:ORDER_BLOCK") for item in result["evidence"])


def test_adaptive_cross_without_causal_zone_cannot_authorize(monkeypatch):
    ctx = _context(with_zone=False, structure=True)
    _patch_common(monkeypatch, ctx)
    result = bridge.analyze_symbol("BTC/USDT:USDT", "BUY", _frame(), now_ms=1795204801000)
    assert result["decision_setup_type"] != "EMA9_VWAP_ADAPTIVE"
    assert result["structural_stop"] is None


def test_adaptive_cross_without_directional_structure_or_sweep_cannot_authorize(monkeypatch):
    ctx = _context(with_zone=True, structure=False, liquidity=False)
    _patch_common(monkeypatch, ctx)
    result = bridge.analyze_symbol("BTC/USDT:USDT", "BUY", _frame(), now_ms=1795204801000)
    assert result["decision_setup_type"] != "EMA9_VWAP_ADAPTIVE"
    assert result["decision_state"] != "ENTRY_READY"


def test_adaptive_cross_can_use_matching_liquidity_confirmation_when_structure_is_transition(monkeypatch):
    ctx = _context(with_zone=True, structure=False, liquidity=True)
    _patch_common(monkeypatch, ctx)
    result = bridge.analyze_symbol("BTC/USDT:USDT", "BUY", _frame(), now_ms=1795204801000)
    assert result["decision_setup_type"] == "EMA9_VWAP_ADAPTIVE"
    assert any(item.startswith("liquidity_confirmation:") for item in result["evidence"])


def test_scanner_snapshot_uses_only_defined_adaptive_side_map_and_progress_map():
    from pathlib import Path
    source = Path(__file__).with_name("ggpbot.py").read_text(encoding="utf-8")
    # The scanner block is module-level; verify the former stale variable names
    # cannot silently break the watchlist update at runtime.
    assert "_progress_by_stage" not in source
    assert "_ema915_checks" not in source
    assert '"ema9_vwap_adaptive_strategy": _strategy_snapshot' in source
    assert '"MACRO_PULLBACK_READY"' in source
    assert '"WAITING_FOR_CONTINUATION_BREAK"' in source
    assert '"EMA9_EMA15_SESSION_VWAP_PULLBACK"' in source


def test_live_adaptive_gate_explicitly_allows_only_confirmed_impulse_mode():
    from pathlib import Path

    source = Path(__file__).with_name("ggpbot.py").read_text(encoding="utf-8")
    authorization = source.split('elif setup == "EMA9_VWAP_ADAPTIVE":', 1)[1].split("\n            else:", 1)[0]
    assert '"EMA915_VWAP_IMPULSE_VALID"' in authorization
    assert '"IMPULSE_ENTRY"' in authorization
    open_position = source.split("def open_position(", 1)[1]
    assert "_validate_ema915_vwap_pullback_entry_gate(" in open_position
    assert "entry_gate_snapshot" in open_position
