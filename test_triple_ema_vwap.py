from pathlib import Path
import queue

from triple_ema_vwap import detect_triple_confluence
import crossover_snapshots
from crossover_snapshots import render_snapshot


def test_price_vwap_cross_and_ema_cross_are_separate_typed_events():
    result = detect_triple_confluence(
        [1, 2, 3, 4], [9, 9, 11, 12], [8, 8, 11, 12], [9, 9, 10, 11],
        [10, 10, 10, 10], direction="BUY", atr=1, adx=25, di_plus=30, di_minus=10,
    )
    assert result["status"] == "VALID"
    assert result["vwap_event_type"] == "PRICE_VWAP_CROSS"
    assert result["bars_between_events"] == 0
    assert result["entry_authorization"] is False
    assert result["event_id"]


def test_ema9_vwap_cross_is_reported_when_price_did_not_cross():
    result = detect_triple_confluence(
        [1, 2, 3, 4], [11, 11, 11, 12], [8, 8, 11, 12], [9, 9, 10, 11],
        [10, 10, 10, 10], direction="BUY", atr=1,
    )
    assert result["vwap_event_type"] == "EMA9_VWAP_CROSS"
    assert result["entry_authorization"] is False


def test_vwap_event_outside_window_is_weak_not_double_counted():
    result = detect_triple_confluence(
        list(range(6)), [9, 11, 11, 11, 12, 13], [11, 11, 11, 11, 12, 13],
        [12, 12, 12, 12, 11, 12], [10, 10, 10, 10, 10, 10],
        window_candles=2, direction="BUY", atr=1,
    )
    assert result["status"] == "WEAK"
    assert result["vwap_event_type"] is None


def test_counter_direction_di_downgrades_confluence():
    result = detect_triple_confluence(
        [1, 2, 3, 4], [9, 9, 11, 12], [8, 8, 11, 12], [9, 9, 10, 11],
        [10, 10, 10, 10], direction="BUY", atr=1, adx=25, di_plus=10, di_minus=30,
    )
    assert result["status"] == "WEAK"
    assert result["entry_authorization"] is False


def test_snapshot_queue_is_bounded_and_reference_path_is_deterministic(monkeypatch):
    class RunningWorker:
        @staticmethod
        def is_alive():
            return True

    monkeypatch.setattr(crossover_snapshots, "_QUEUE", queue.Queue(maxsize=1))
    monkeypatch.setattr(crossover_snapshots, "_WORKER", RunningWorker())
    monkeypatch.setattr(crossover_snapshots, "_STATUS", {"queued": 0, "generated": 0, "dropped": 0, "last_error": None})
    payload = {
        "symbol": "BTC/USDT", "last_update_ms": 42, "snapshot_stage": "pullback",
        "triple_confluence": {"event_id": "evt:1"}, "chart": {"timestamps": [1, 2, 3]},
    }
    assert crossover_snapshots.snapshot_relative_path(payload) == "BTC_USDT/evt_1/pullback_42.png"
    crossover_snapshots._QUEUE.put_nowait((payload, Path(".")))
    assert crossover_snapshots.enqueue_snapshot(payload) is False
    assert crossover_snapshots.snapshot_status()["dropped"] == 1


def test_local_snapshot_uses_supplied_chart_only_and_creates_png(tmp_path, monkeypatch):
    monkeypatch.setattr(
        crossover_snapshots, "_STATUS",
        {"queued": 0, "generated": 0, "dropped": 0, "last_error": None},
    )
    payload = {
        "symbol": "BTC/USDT", "timeframe": "15m", "direction": "BUY",
        "last_update_ms": 1000, "snapshot_stage": "crossover",
        "triple_confluence": {
            "status": "VALID", "event_id": "evt1", "ema_cross_timestamp": 2000,
            "vwap_event_timestamp": 2000, "vwap_event_type": "PRICE_VWAP_CROSS",
        },
        "chart": {
            "timestamps": [1000, 2000, 3000], "open": [1, 2, 3],
            "high": [2, 3, 4], "low": [0.5, 1.5, 2.5], "close": [1.5, 2.5, 3.5],
            "ema9": [1, 2, 3], "ema15": [1.1, 1.9, 2.9],
            "session_vwap": [1.2, 2.0, 2.8], "volume": [12, 14],
        },
    }
    path = render_snapshot(payload, str(tmp_path))
    assert path and Path(path).is_file()
    assert Path(path).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert crossover_snapshots.snapshot_status()["last_error"] is None
