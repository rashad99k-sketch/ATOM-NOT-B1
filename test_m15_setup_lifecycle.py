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


def setup_function():
    lifecycle.clear_m15_setup_memory()


def test_cross_candidate_survives_disappearing_cross_marker_in_runtime_memory():
    first = lifecycle.advance_m15_setup_lifecycle(_row(), now_ms=1_800_000_010_000, cycle_id=1)
    assert first["lifecycle_active"] is True
    assert first["lifecycle_stage"] == "WAITING_FOR_PULLBACK"
    assert first["lifecycle_cycles_seen"] == 1
    assert first["entry_authorization"] is False

    later = _row(status="NO_ACTIVE_CROSS")
    later["signal_timestamp_ms"] = None
    later["chart"] = {"timestamps": [1_800_000_000_000, 1_800_000_900_000],
                      "open": [99.5, 99.3], "close": [99.0, 98.8],
                      "high": [100.0, 99.6], "low": [98.8, 98.5],
                      "ema9": [100.2, 100.0], "ema15": [100.4, 100.2]}
    result = lifecycle.advance_m15_setup_lifecycle(later, now_ms=1_800_000_020_000, cycle_id=2)
    assert result["lifecycle_active"] is True
    assert result["lifecycle_direction"] == "SELL"
    assert result["lifecycle_cycles_seen"] == 2
    assert result["entry_authorization"] is False
    assert any(x["symbol"] == "YFI/USDT" for x in lifecycle.list_active_m15_setups(now_ms=1_800_000_020_000))


def test_pre_cross_is_tracked_without_authorizing_entry():
    row = _row(status="PRE_CROSS")
    row["signal_timestamp_ms"] = None
    result = lifecycle.advance_m15_setup_lifecycle(row, now_ms=1_800_000_010_000, cycle_id=1)
    assert result["lifecycle_stage"] == "CROSS_APPROACHING"
    assert result["entry_authorization"] is False


def test_runtime_candidate_expires_without_disk_storage():
    lifecycle.advance_m15_setup_lifecycle(_row(), now_ms=1_800_000_010_000, cycle_id=1)
    assert lifecycle.list_active_m15_setups(now_ms=1_800_000_010_000 + 7 * 60 * 60 * 1000) == []
