import time

import pandas as pd

from m15_crossover_radar import evaluate_m15_radar


NOW = 1_800_000_000_000


def frame_from_closes(closes, start_ms=None):
    start_ms = start_ms if start_ms is not None else NOW - len(closes) * 900_000
    timestamps = [start_ms + i * 900_000 for i in range(len(closes))]
    return pd.DataFrame({
        "timestamp": timestamps,
        "open": closes,
        "high": [price * 1.001 for price in closes],
        "low": [price * 0.999 for price in closes],
        "close": closes,
        "volume": [100.0] * len(closes),
    })


def bearish_sequence_with_small_gap():
    closes = [110 - i * 0.25 for i in range(59)] + [103.0]
    start_ms = NOW - len(closes) * 900_000
    return frame_from_closes(closes, start_ms)


def test_closed_bullish_m15_cross_is_confirmed_but_never_entry_authorized():
    closes = [110 - i * 0.25 for i in range(59)] + [110.0]
    frame = frame_from_closes(closes, NOW - len(closes) * 900_000)

    result = evaluate_m15_radar("BTC/USDT", frame, now_ms=NOW)

    assert result["timeframe"] == "15m"
    assert result["crossover_status"] == "CROSS_CONFIRMED"
    assert result["direction"] == "BUY"
    assert result["evidence"]["closed_m15_confirmed"] is True
    assert result["entry_authorization"] is False
    assert result["entry_readiness"] == "NOT_AUTHORIZED_BY_RADAR"
    assert result["chart"]["cross_marker"]["status"] == "CONFIRMED"


def test_closed_bearish_m15_cross_is_confirmed_as_sell():
    closes = [90 + i * 0.25 for i in range(59)] + [90.0]
    frame = frame_from_closes(closes, NOW - len(closes) * 900_000)

    result = evaluate_m15_radar("BTC/USDT", frame, now_ms=NOW)

    assert result["crossover_status"] == "CROSS_CONFIRMED"
    assert result["direction"] == "SELL"
    assert result["chart"]["cross_marker"]["direction"] == "SELL"
    assert result["entry_authorization"] is False


def test_intrabar_cross_is_provisional_not_confirmed():
    closed = bearish_sequence_with_small_gap()
    forming = pd.DataFrame([{
        "timestamp": NOW - 300_000,
        "open": 103.0,
        "high": 120.0,
        "low": 102.0,
        "close": 120.0,
        "volume": 150.0,
    }])
    raw = pd.concat([closed, forming], ignore_index=True)

    result = evaluate_m15_radar("ETH/USDT", raw, now_ms=NOW)

    assert result["crossover_status"] == "CROSS_DETECTED"
    assert result["opportunity_state"] == "WAITING_FOR_CANDLE_CLOSE"
    assert result["evidence"]["intrabar_only"] is True
    assert result["evidence"]["closed_m15_confirmed"] is False
    assert result["chart"]["intrabar_marker"]["status"] == "PROVISIONAL"
    assert result["entry_authorization"] is False


def test_approaching_cross_has_distinct_pre_cross_state():
    closes = [110 - i * 0.25 for i in range(59)] + [103.0]
    frame = frame_from_closes(closes, NOW - len(closes) * 900_000)

    result = evaluate_m15_radar("XRP/USDT", frame, now_ms=NOW)

    assert result["crossover_status"] == "PRE_CROSS"
    assert result["opportunity_state"] == "CROSS_APPROACHING"
    assert result["entry_authorization"] is False


def test_stale_m15_snapshot_is_rejected():
    old_now = NOW - 5_000_000
    frame = frame_from_closes([100.0] * 60, old_now - 60 * 900_000)

    result = evaluate_m15_radar("ADA/USDT", frame, now_ms=NOW)

    assert result["crossover_status"] == "EXPIRED"
    assert result["opportunity_state"] == "EXPIRED"
    assert result["rejection_reason"] == "STALE_CLOSED_CANDLE_DATA"
    assert result["chart"] is None


def test_incomplete_market_data_is_not_fabricated_as_zero():
    result = evaluate_m15_radar("SOL/USDT", pd.DataFrame({"close": [0.0]}), now_ms=NOW)

    assert result["crossover_status"] == "DATA_UNAVAILABLE"
    assert result["current_price"] is None
    assert result["ema9"] is None
    assert result["session_vwap"] is None
    assert result["entry_authorization"] is False


def test_shared_scanner_cache_reuses_raw_snapshot_and_returns_closed_bars_only(monkeypatch):
    import ggpbot

    now_ms = int(time.time() * 1000)
    raw = frame_from_closes([100.0, 101.0], now_ms - 900_000)
    raw.loc[0, "timestamp"] = now_ms - 900_000
    raw.loc[1, "timestamp"] = now_ms - 60_000
    fetches = []

    def fake_fetch(symbol, limit):
        fetches.append((symbol, limit))
        return raw.copy()

    with ggpbot._SCANNER_FRAME_CACHE_LOCK:
        ggpbot._SCANNER_FRAME_CACHE.clear()
    monkeypatch.setattr(ggpbot, "get_ohlcv_safe", fake_fetch)
    monkeypatch.setattr(ggpbot, "INTERVAL", "15m")

    first = ggpbot._get_scanner_frame("TEST/USDT", 150)
    second = ggpbot._get_scanner_frame("TEST/USDT", 150)

    assert first is not None and second is not None
    assert len(first) == len(second) == 1
    assert int(first["timestamp"].iloc[-1]) == now_ms - 900_000
    assert fetches == [("TEST/USDT", 150)]
    with ggpbot._SCANNER_FRAME_CACHE_LOCK:
        ggpbot._SCANNER_FRAME_CACHE.clear()
