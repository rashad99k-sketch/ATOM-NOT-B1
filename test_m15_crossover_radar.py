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



def test_m15_radar_batches_are_bounded_and_rotate_universe_and_retained_setups(monkeypatch):
    import ggpbot

    monkeypatch.setitem(ggpbot.MEMORY, "m15_radar_active_cursor", 0)
    monkeypatch.setitem(ggpbot.MEMORY, "m15_radar_universe_cursor", 0)
    universe = [f"COIN{i}/USDT" for i in range(80)]
    retained = [f"ACTIVE{i}/USDT" for i in range(20)]

    first = ggpbot._select_m15_radar_batch(universe, retained, batch_size=10)
    second = ggpbot._select_m15_radar_batch(universe, retained, batch_size=10)

    assert len(first) == 10
    assert len(second) == 10
    assert len(set(first)) == 10
    assert len(set(second)) == 10
    assert len(set(first) & set(retained)) == 5
    assert len(set(second) & set(retained)) == 5
    assert set(first) != set(second)
    assert ggpbot.M15_RADAR_SCAN_INTERVAL_SECONDS == 30
    assert ggpbot.M15_RADAR_BATCH_SIZE == 10


def test_same_symbol_scanner_cache_miss_is_single_flight(monkeypatch):
    import time
    from concurrent.futures import ThreadPoolExecutor
    import ggpbot

    frame = frame_from_closes([100.0] * 120, NOW - 120 * 900_000)
    calls = []

    def fake_fetch(symbol, limit):
        calls.append((symbol, limit))
        time.sleep(0.03)
        return frame.copy()

    with ggpbot._SCANNER_FRAME_CACHE_LOCK:
        ggpbot._SCANNER_FRAME_CACHE.clear()
    monkeypatch.setattr(ggpbot, "get_ohlcv_safe", fake_fetch)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: ggpbot._get_scanner_raw_frame("LOCK/USDT", 150), range(8)))

    assert len(calls) == 1
    assert all(result is not None for result in results)
    with ggpbot._SCANNER_FRAME_CACHE_LOCK:
        ggpbot._SCANNER_FRAME_CACHE.clear()


def test_bingx_429_kline_response_enters_cooldown_without_retry_storm(monkeypatch):
    import time
    import ggpbot

    calls = []

    def fetch_ohlcv(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("HTTP 429 Too Many Requests")

    monkeypatch.setattr(ggpbot, "rate_limit", lambda func=None: None)
    monkeypatch.setattr(ggpbot, "_BINGX_KLINE_COOLDOWN_UNTIL", 0.0)
    result = ggpbot.safe_api_call(fetch_ohlcv, "BTC/USDT:USDT", "15m", limit=150)

    assert result is None
    assert len(calls) == 1
    assert ggpbot._BINGX_KLINE_COOLDOWN_UNTIL > time.monotonic()
    assert ggpbot._LAST_API_RATE_LIMIT["method"] == "fetch_ohlcv"


def test_bingx_418_kline_response_enters_longer_cooldown(monkeypatch):
    import time
    import ggpbot

    calls = []

    def fetch_ohlcv(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("HTTP 418 IP banned")

    monkeypatch.setattr(ggpbot, "rate_limit", lambda func=None: None)
    monkeypatch.setattr(ggpbot, "_BINGX_KLINE_COOLDOWN_UNTIL", 0.0)
    before = time.monotonic()
    result = ggpbot.safe_api_call(fetch_ohlcv, "BTC/USDT:USDT", "15m", limit=150)

    assert result is None
    assert len(calls) == 1
    assert ggpbot._BINGX_KLINE_COOLDOWN_UNTIL - before >= 59



def test_m15_radar_market_universe_is_cached_across_30_second_cycles(monkeypatch):
    import ggpbot

    calls = []
    monkeypatch.setattr(ggpbot, "get_usdt_perp_symbols", lambda: calls.append(1) or ["BTC/USDT"])
    monkeypatch.setitem(ggpbot.MEMORY, "m15_radar_universe_cache", {})
    first = ggpbot._get_m15_radar_universe()
    second = ggpbot._get_m15_radar_universe()

    assert first == second == ["BTC/USDT"]
    assert len(calls) == 1



def test_concurrent_kline_workers_are_suppressed_after_first_429(monkeypatch):
    """Workers queued before a 429 must re-check cooldown before calling BingX."""
    import time
    from concurrent.futures import ThreadPoolExecutor
    import ggpbot

    calls = []
    ggpbot._BINGX_KLINE_COOLDOWN_UNTIL = 0.0

    def fetch_ohlcv(*args, **kwargs):
        calls.append(1)
        time.sleep(0.03)
        raise RuntimeError("HTTP 429 Too Many Requests")

    monkeypatch.setattr(ggpbot, "rate_limit", lambda func=None: None)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda _: ggpbot.safe_api_call(fetch_ohlcv, "BTC/USDT:USDT", "15m", limit=150),
            range(8),
        ))

    assert results == [None] * 8
    assert len(calls) == 1, "queued workers must not send requests during the cooldown"
    assert ggpbot._BINGX_KLINE_COOLDOWN_UNTIL > time.monotonic()
    ggpbot._BINGX_KLINE_COOLDOWN_UNTIL = 0.0


def test_scanner_frame_cache_separates_limit_and_timeframe(monkeypatch):
    import ggpbot

    calls = []
    frame = frame_from_closes([100.0] * 120, NOW - 120 * 900_000)

    def fake_fetch(symbol, limit):
        calls.append((symbol, limit))
        return frame.copy()

    with ggpbot._SCANNER_FRAME_CACHE_LOCK:
        ggpbot._SCANNER_FRAME_CACHE.clear()
    monkeypatch.setattr(ggpbot, "get_ohlcv_safe", fake_fetch)
    monkeypatch.setattr(ggpbot, "INTERVAL", "15m")

    ggpbot._get_scanner_raw_frame("CACHE/USDT", 150)
    ggpbot._get_scanner_raw_frame("CACHE/USDT", 100)
    assert calls == [("CACHE/USDT", 150), ("CACHE/USDT", 100)]

    with ggpbot._SCANNER_FRAME_CACHE_LOCK:
        ggpbot._SCANNER_FRAME_CACHE.clear()
