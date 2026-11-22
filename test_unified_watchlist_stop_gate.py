import ggp_institutional_bridge as bridge


def _candidate_map(_memory):
    return {
        "BTC/USDT": {
            "symbol": "BTC/USDT",
            "exchange_symbol": "BTC/USDT",
            "source": "test",
            "sources": ["test"],
            "sides": [],
            "score": 1.0,
        }
    }


def test_unified_watchlist_stops_before_fetch_when_discovery_is_paused(monkeypatch):
    monkeypatch.setattr(bridge, "merge_candidates", _candidate_map)
    fetch_calls = []
    memory = {"watchlist": {"OLD/USDT": {"decision_state": "ENTRY_READY"}}}

    result = bridge.update_unified_watchlist(
        memory,
        lambda symbol: fetch_calls.append(symbol),
        should_stop=lambda: True,
    )

    assert fetch_calls == []
    assert result["OLD/USDT"]["decision_state"] == "ENTRY_READY"
    assert memory["unified_watchlist_stop_reason"] == "DISCOVERY_PAUSED"
    assert memory["unified_watchlist_last_batch_count"] == 0


def test_unified_watchlist_yields_after_inflight_fetch_before_analysis(monkeypatch):
    monkeypatch.setattr(bridge, "merge_candidates", _candidate_map)
    stop_checks = iter([False, False, True])
    fetch_calls = []
    analyze_calls = []
    memory = {"watchlist": {}}

    def should_stop():
        return next(stop_checks)

    def fetch_frame(symbol):
        fetch_calls.append(symbol)
        return object()

    monkeypatch.setattr(
        bridge, "analyze_symbol",
        lambda *args, **kwargs: analyze_calls.append(args) or {},
    )

    bridge.update_unified_watchlist(
        memory, fetch_frame, should_stop=should_stop,
    )

    assert fetch_calls == ["BTC/USDT"]
    assert analyze_calls == []
    assert memory["watchlist"] == {}
    assert memory["unified_watchlist_stop_reason"] == "DISCOVERY_PAUSED"
    assert memory["unified_watchlist_last_batch_count"] == 0


def test_unified_watchlist_stop_callback_errors_fail_closed(monkeypatch):
    monkeypatch.setattr(bridge, "merge_candidates", _candidate_map)
    fetch_calls = []

    def broken_gate():
        raise RuntimeError("state read failed")

    bridge.update_unified_watchlist(
        {"watchlist": {}},
        lambda symbol: fetch_calls.append(symbol),
        should_stop=broken_gate,
    )

    assert fetch_calls == []
