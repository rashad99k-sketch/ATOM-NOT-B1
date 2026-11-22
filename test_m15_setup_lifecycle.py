import m15_setup_lifecycle as lifecycle


BASE_TS = 1_800_000_000_000


def _row(symbol="YFI/USDT", status="CROSS_CONFIRMED", direction="SELL", **overrides):
    row = {
        "symbol": symbol, "crossover_status": status, "opportunity_state": "WAITING_FOR_PULLBACK",
        "direction": direction, "signal_timestamp_ms": BASE_TS,
        "ema_distance_atr": 0.18, "volume_ratio": 0.9, "session_vwap": 100.0,
        "closed_price": 99.0 if direction == "SELL" else 101.0,
        "chart": {"timestamps": [BASE_TS], "open": [99.5], "close": [99.0],
                  "high": [100.0], "low": [98.8], "ema9": [100.2], "ema15": [100.4]},
    }
    row.update(overrides)
    return row


def setup_function():
    lifecycle.configure_m15_setup_store(None)
    lifecycle.clear_m15_setup_memory()


def test_cross_candidate_survives_disappearing_cross_marker_in_runtime_memory():
    first = lifecycle.advance_m15_setup_lifecycle(_row(), now_ms=BASE_TS + 10_000, cycle_id=1)
    assert first["lifecycle_active"] is True
    assert first["lifecycle_stage"] == "WAITING_FOR_PULLBACK"
    assert first["lifecycle_cycles_seen"] == 1
    assert first["entry_authorization"] is False

    later = _row(status="NO_ACTIVE_CROSS", signal_timestamp_ms=None)
    later["chart"] = {
        "timestamps": [BASE_TS, BASE_TS + 900_000],
        "open": [99.5, 99.3], "close": [99.0, 98.8],
        "high": [100.0, 99.6], "low": [98.8, 98.5],
        "ema9": [100.2, 100.0], "ema15": [100.4, 100.2],
    }
    result = lifecycle.advance_m15_setup_lifecycle(later, now_ms=BASE_TS + 20_000, cycle_id=2)
    assert result["lifecycle_active"] is True
    assert result["lifecycle_direction"] == "SELL"
    assert result["lifecycle_cycles_seen"] == 2
    assert result["entry_authorization"] is False
    assert any(x["symbol"] == "YFI/USDT" for x in lifecycle.list_active_m15_setups(now_ms=BASE_TS + 20_000))


def test_pre_cross_transitions_to_waiting_for_candle_close_without_authorizing_entry():
    row = _row(status="PRE_CROSS", signal_timestamp_ms=None)
    result = lifecycle.advance_m15_setup_lifecycle(row, now_ms=BASE_TS + 10_000, cycle_id=1)
    assert result["lifecycle_stage"] == "CROSS_APPROACHING"
    detected = _row(status="CROSS_DETECTED", signal_timestamp_ms=None)
    result = lifecycle.advance_m15_setup_lifecycle(detected, now_ms=BASE_TS + 20_000, cycle_id=2)
    assert result["lifecycle_stage"] == "WAITING_FOR_CANDLE_CLOSE"
    assert result["entry_authorization"] is False


def test_pullback_retest_and_trigger_require_distinct_later_closed_candles():
    chart = {
        "timestamps": [BASE_TS, BASE_TS + 900_000, BASE_TS + 1_800_000, BASE_TS + 2_700_000, BASE_TS + 3_600_000],
        "open": [101.0, 100.8, 100.35, 100.4, 100.9],
        "close": [101.1, 100.3, 100.5, 100.8, 101.2],
        "high": [101.2, 101.0, 100.7, 101.0, 101.3],
        "low": [100.9, 100.1, 100.25, 100.3, 100.8],
        "ema9": [100.2] * 5, "ema15": [100.4] * 5,
    }
    result = lifecycle.advance_m15_setup_lifecycle(
        _row(direction="BUY", chart=chart, closed_price=101.2),
        now_ms=BASE_TS + 3_700_000, cycle_id=1,
    )
    assert result["lifecycle_stage"] == "TRIGGER_CONFIRMED"
    assert result["lifecycle_pullback_timestamp_ms"] == BASE_TS + 900_000
    assert result["lifecycle_pullback_hold_timestamp_ms"] == BASE_TS + 1_800_000
    assert result["lifecycle_retest_timestamp_ms"] == BASE_TS + 2_700_000
    assert result["lifecycle_trigger_timestamp_ms"] == BASE_TS + 3_600_000
    assert result["entry_authorization"] is False


def test_same_candle_touch_cannot_also_confirm_rejection():
    chart = {
        "timestamps": [BASE_TS, BASE_TS + 900_000],
        "open": [101.0, 100.8], "close": [101.1, 101.0],
        "high": [101.2, 101.1], "low": [100.9, 100.1],
        "ema9": [100.2, 100.2], "ema15": [100.4, 100.4],
    }
    result = lifecycle.advance_m15_setup_lifecycle(
        _row(direction="BUY", chart=chart, closed_price=101.0),
        now_ms=BASE_TS + 1_000_000, cycle_id=1,
    )
    assert result["lifecycle_stage"] == "PULLBACK_DETECTED"
    assert result["lifecycle_rejection_confirmed"] is False
    assert result["entry_authorization"] is False


def test_invalidated_cross_is_terminal_and_same_event_cannot_be_resurrected():
    invalid = _row(opportunity_state="INVALIDATED", rejection_reason="CLOSED_PRICE_LOST_EMA9")
    result = lifecycle.advance_m15_setup_lifecycle(invalid, now_ms=BASE_TS + 10_000, cycle_id=1)
    assert result["lifecycle_active"] is False
    assert result["lifecycle_stage"] == "INVALIDATED"
    result = lifecycle.advance_m15_setup_lifecycle(_row(), now_ms=BASE_TS + 20_000, cycle_id=2)
    assert result["lifecycle_active"] is False
    assert result["lifecycle_stage"] == "INVALIDATED"

    new_event = _row(signal_timestamp_ms=BASE_TS + 900_000)
    result = lifecycle.advance_m15_setup_lifecycle(new_event, now_ms=BASE_TS + 30_000, cycle_id=3)
    assert result["lifecycle_active"] is True
    assert result["lifecycle_stage"] == "WAITING_FOR_PULLBACK"


def test_expired_radar_event_expires_retained_candidate():
    lifecycle.advance_m15_setup_lifecycle(_row(), now_ms=BASE_TS + 10_000, cycle_id=1)
    expired = _row(status="EXPIRED", opportunity_state="EXPIRED", signal_timestamp_ms=None)
    result = lifecycle.advance_m15_setup_lifecycle(expired, now_ms=BASE_TS + 20_000, cycle_id=2)
    assert result["lifecycle_active"] is False
    assert result["lifecycle_stage"] == "EXPIRED"
    assert lifecycle.list_active_m15_setups(now_ms=BASE_TS + 20_000) == []


def test_runtime_candidate_expires_without_disk_storage():
    lifecycle.advance_m15_setup_lifecycle(_row(), now_ms=BASE_TS + 10_000, cycle_id=1)
    assert lifecycle.list_active_m15_setups(now_ms=BASE_TS + 7 * 60 * 60 * 1000) == []



def test_m15_registry_restores_active_opportunity_from_runtime_store():
    class FakeStore:
        def __init__(self):
            self.state = {}

        def get_runtime_state(self, key, default=None):
            return self.state.get(key, default)

        def set_runtime_state(self, key, value):
            self.state[key] = value

    store = FakeStore()
    lifecycle.configure_m15_setup_store(store)
    first = lifecycle.advance_m15_setup_lifecycle(
        _row(symbol="SOL/USDT"), now_ms=BASE_TS + 10_000, cycle_id=1
    )
    assert first["lifecycle_persisted"] is True
    assert first["lifecycle_event_id"] == f"M15:SOL/USDT:SELL:{BASE_TS}"

    # Simulate a process restart: module memory is empty, SQLite state remains.
    lifecycle._ACTIVE.clear()
    lifecycle._STORE_READY = False
    lifecycle.configure_m15_setup_store(store)
    active = lifecycle.list_active_m15_setups(now_ms=BASE_TS + 20_000)
    assert any(item["symbol"] == "SOL/USDT" for item in active)
    lifecycle.configure_m15_setup_store(None)
    lifecycle.clear_m15_setup_memory()


def test_momentum_expansion_is_classified_for_priority_analysis_not_entry_authority():
    row = _row(
        symbol="AVAX/USDT",
        direction="BUY",
        closed_price=101.0,
        session_vwap=100.0,
        volume_ratio=1.8,
        evidence={
            "body_to_range": 0.78,
            "close_position": 0.92,
            "displacement_atr": 0.95,
            "price_extension_atr": 0.70,
        },
    )
    result = lifecycle.advance_m15_setup_lifecycle(
        row, now_ms=BASE_TS + 10_000, cycle_id=1
    )
    assert result["strategy_mode"] == "MOMENTUM_CANDIDATE"
    assert result["lifecycle_stage"] == "MOMENTUM_EXPANSION_CANDIDATE"
    assert result["entry_authorization"] is False


def test_momentum_expansion_does_not_promote_an_extended_no_chase_move():
    row = _row(
        symbol="AVAX/USDT",
        direction="BUY",
        closed_price=101.0,
        session_vwap=100.0,
        volume_ratio=2.0,
        evidence={
            "body_to_range": 0.80,
            "close_position": 0.95,
            "displacement_atr": 1.10,
            "price_extension_atr": 1.9,
        },
    )
    result = lifecycle.advance_m15_setup_lifecycle(
        row, now_ms=BASE_TS + 10_000, cycle_id=1
    )
    assert result["strategy_mode"] != "MOMENTUM_CANDIDATE"
    assert result["entry_authorization"] is False
