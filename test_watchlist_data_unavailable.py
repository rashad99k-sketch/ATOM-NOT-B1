"""Regression tests for preserving last-good Watchlist analysis on fetch failures."""

from ggp_institutional_bridge import update_unified_watchlist


def _candidate_memory(previous=None):
    watchlist = {"BTC": dict(previous)} if previous is not None else {}
    return {
        "professional_scan_candidates": [
            {"symbol": "BTC/USDT:USDT", "side": "BUY", "score": 8.0}
        ],
        "watchlist": watchlist,
    }


def test_unavailable_refresh_preserves_last_valid_watchlist_snapshot():
    previous = {
        "symbol": "BTC", "exchange_symbol": "BTC/USDT:USDT", "side": "BUY",
        "decision_state": "ENTRY_READY", "decision_reason": "validated_prior_setup",
        "score": 82.0, "last_update": 1_700_000_000.0,
        "data_quality": "VALID_CLOSED_CANDLES",
        "order_block": {"low": 99.0, "high": 100.0},
        "icons": {"status": "✅"},
    }
    memory = _candidate_memory(previous)

    result = update_unified_watchlist(
        memory, fetch_frame=lambda _symbol: None, max_items=10, batch_size=1
    )
    row = result["BTC"]

    assert row["decision_state"] == "ENTRY_READY"
    assert row["decision_reason"] == "validated_prior_setup"
    assert row["score"] == 82.0
    assert row["order_block"] == {"low": 99.0, "high": 100.0}
    assert row["last_update"] == previous["last_update"]
    assert row["data_quality"] == "VALID_CLOSED_CANDLES"
    assert row["data_status"] == "UNAVAILABLE"
    assert row["data_stale"] is True
    assert row["last_fetch_error"] == "missing_or_invalid_market_data"


def test_first_unavailable_watchlist_observation_is_not_marked_fresh():
    memory = _candidate_memory()

    result = update_unified_watchlist(
        memory, fetch_frame=lambda _symbol: None, max_items=10, batch_size=1
    )
    row = result["BTC"]

    assert row["decision_state"] == "UNKNOWN"
    assert row["data_quality"] == "DATA_UNAVAILABLE"
    assert row["data_status"] == "UNAVAILABLE"
    assert row["last_update"] == 0.0
    assert row["data_stale"] is True
