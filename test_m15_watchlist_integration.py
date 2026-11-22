import pandas as pd
import pytest

import m15_setup_lifecycle as lifecycle
import m15_watchlist_integration as integration


@pytest.fixture(autouse=True)
def isolate_lifecycle_registry():
    lifecycle.configure_m15_setup_store(None)
    lifecycle.clear_m15_setup_memory()
    yield
    lifecycle.clear_m15_setup_memory()


def test_existing_watchlist_frame_advances_lifecycle_once_per_closed_candle(monkeypatch):
    calls = []

    def fake_evaluate(symbol, frame, *, now_ms=None):
        calls.append((symbol, int(frame["timestamp"].iloc[-1])))
        return {
            "symbol": symbol, "crossover_status": "NO_ACTIVE_CROSS",
            "opportunity_state": "WAITING", "direction": "UNKNOWN",
            "signal_timestamp_ms": None, "chart": None, "entry_authorization": False,
        }

    monkeypatch.setattr(integration, "evaluate_m15_radar", fake_evaluate)
    memory = {"watchlist": {}}
    frame = pd.DataFrame({"timestamp": [1_800_000_000_000]})
    first = integration.process_m15_watchlist_frame("BTC/USDT", frame, memory, now_ms=1_800_000_900_000)
    second = integration.process_m15_watchlist_frame("BTC/USDT", frame, memory, now_ms=1_800_001_000_000)

    assert len(calls) == 1
    assert first == second
    assert memory["_m15_lifecycle_last_candle"]["BTC/USDT"] == 1_800_000_000_000
    assert first["entry_authorization"] is False


def test_active_watchlist_feed_is_discovery_only(monkeypatch):
    ts = 1_800_000_000_000

    def fake_evaluate(symbol, frame, *, now_ms=None):
        return {
            "symbol": symbol, "crossover_status": "CROSS_CONFIRMED",
            "opportunity_state": "WAITING_FOR_PULLBACK", "direction": "BUY",
            "signal_timestamp_ms": ts, "ema_distance_atr": 0.2, "volume_ratio": 1.0,
            "session_vwap": 99.0, "closed_price": 101.0,
            "chart": {"timestamps": [ts], "open": [100.0], "close": [101.0],
                      "high": [101.2], "low": [99.9], "ema9": [100.5], "ema15": [100.4]},
            "entry_authorization": False,
        }

    monkeypatch.setattr(integration, "evaluate_m15_radar", fake_evaluate)
    memory = {"watchlist": {}}
    frame = pd.DataFrame({"timestamp": [ts]})
    result = integration.process_m15_watchlist_frame("ETH/USDT", frame, memory, now_ms=ts + 900_000)

    assert result["lifecycle_active"] is True
    assert memory["m15_setup_watchlist"][0]["side_hint"] == "BUY"
    assert memory["m15_setup_watchlist"][0]["source"] == "m15_watchlist_lifecycle_discovery_only"
    assert result["entry_authorization"] is False


def test_stale_duplicate_frame_expires_candidate_without_refetch(monkeypatch):
    lifecycle.clear_m15_setup_memory()
    ts = 1_800_000_000_000
    frame = pd.DataFrame({"timestamp": [ts]})
    calls = []

    def fake_evaluate(symbol, supplied_frame, *, now_ms=None):
        calls.append(symbol)
        return {
            "symbol": symbol, "crossover_status": "CROSS_CONFIRMED",
            "opportunity_state": "WAITING_FOR_PULLBACK", "direction": "BUY",
            "signal_timestamp_ms": ts,
            "chart": {"timestamps": [ts], "open": [100.0], "close": [101.0],
                      "high": [101.2], "low": [99.9], "ema9": [100.5], "ema15": [100.4]},
            "entry_authorization": False,
        }

    monkeypatch.setattr(integration, "evaluate_m15_radar", fake_evaluate)
    memory = {"watchlist": {}}
    first = integration.process_m15_watchlist_frame("XRP/USDT", frame, memory, now_ms=ts + 900_000)
    assert first["lifecycle_active"] is True
    assert memory["m15_setup_watchlist"]

    stale = integration.process_m15_watchlist_frame(
        "XRP/USDT", frame, memory, now_ms=ts + 900_000 + 1_800_001
    )
    assert len(calls) == 1
    assert stale["lifecycle_stage"] == "EXPIRED"
    assert stale["lifecycle_active"] is False
    assert "m15_setup" not in memory["watchlist"]["XRP/USDT"]
    assert memory["m15_setup_watchlist"] == []
