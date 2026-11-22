import json

import m15_setup_lifecycle as lifecycle


def _row(symbol="YFI/USDT", status="CROSS_CONFIRMED", direction="SELL"):
    return {
        "symbol": symbol, "crossover_status": status, "opportunity_state": "WAITING_FOR_PULLBACK",
        "direction": direction, "signal_timestamp_ms": 1_800_000_000_000,
        "ema_distance_atr": 0.18, "volume_ratio": 0.9,
        "session_vwap": 100.0, "closed_price": 99.0,
        "chart": {"timestamps": [1_800_000_000_000], "open": [99.5], "close": [99.0],
                  "high": [100.0], "low": [98.8], "ema9": [100.2], "ema15": [100.4]},
    }


def test_cross_candidate_is_persisted_and_retained_after_cross_marker_disappears(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "_DB_PATH", str(tmp_path / "setups.sqlite3"))
    first = lifecycle.advance_m15_setup_lifecycle(_row(), now_ms=1_800_000_010_000)
    assert first["lifecycle_active"] is True
    assert first["lifecycle_stage"] == "WAITING_FOR_PULLBACK"
    assert first["entry_authorization"] is False

    # New scan no longer has a fresh cross, but the tracked setup must remain visible.
    later = _row(status="NO_ACTIVE_CROSS")
    later["signal_timestamp_ms"] = None
    later["chart"] = {"timestamps": [1_800_000_000_000, 1_800_000_900_000],
                      "open": [99.5, 99.3], "close": [99.0, 98.8],
                      "high": [100.0, 99.6], "low": [98.8, 98.5],
                      "ema9": [100.2, 100.0], "ema15": [100.4, 100.2]}
    result = lifecycle.advance_m15_setup_lifecycle(later, now_ms=1_800_000_020_000)
    assert result["lifecycle_active"] is True
    assert result["lifecycle_direction"] == "SELL"
    assert result["lifecycle_stage"] in {"WAITING_FOR_PULLBACK", "PULLBACK_DETECTED"}
    assert result["entry_authorization"] is False


def test_pre_cross_is_tracked_without_authorizing_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "_DB_PATH", str(tmp_path / "setups.sqlite3"))
    row = _row(status="PRE_CROSS", direction="SELL")
    row["signal_timestamp_ms"] = None
    result = lifecycle.advance_m15_setup_lifecycle(row, now_ms=1_800_000_010_000)
    assert result["lifecycle_stage"] == "CROSS_APPROACHING"
    assert result["strategy_mode"] in {"SCALP_CANDIDATE", "TREND_CANDIDATE", "WAIT_FOR_EVIDENCE", "NO_TRADE_OR_WAIT"}
    assert result["entry_authorization"] is False
