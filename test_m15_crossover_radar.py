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


def test_recent_closed_cross_remains_visible_for_eight_closed_bars():
    closes = [110 - i * 0.25 for i in range(59)] + [110.0, 111.0, 112.0, 113.0]
    frame = frame_from_closes(closes, NOW - len(closes) * 900_000)

    result = evaluate_m15_radar("BTC/USDT", frame, now_ms=NOW)

    assert result["crossover_status"] == "CROSS_CONFIRMED"
    assert result["direction"] == "BUY"
    assert result["cross_age_bars"] == 3
    assert result["signal_timestamp_ms"] == int(frame["timestamp"].iloc[-4])
    assert result["chart"]["cross_marker"]["timestamp"] == int(frame["timestamp"].iloc[-4])
    assert result["entry_authorization"] is False


def test_recent_closed_cross_at_exact_eight_bar_limit_is_still_visible():
    closes = [110 - i * 0.25 for i in range(59)] + [110.0] + [111.0] * 8
    frame = frame_from_closes(closes, NOW - len(closes) * 900_000)

    result = evaluate_m15_radar("ETH/USDT", frame, now_ms=NOW)

    assert result["crossover_status"] == "CROSS_CONFIRMED"
    assert result["cross_age_bars"] == 8


def test_recent_closed_cross_older_than_eight_bars_is_not_fresh():
    closes = [110 - i * 0.25 for i in range(59)] + [110.0] + [111.0] * 9
    frame = frame_from_closes(closes, NOW - len(closes) * 900_000)

    result = evaluate_m15_radar("ETH/USDT", frame, now_ms=NOW)

    assert result["crossover_status"] != "CROSS_CONFIRMED"
    assert result["entry_authorization"] is False


def test_watchlist_shared_frame_publishes_recent_cross_without_extra_fetch():
    from m15_watchlist_integration import process_m15_watchlist_frame

    closes = [110 - i * 0.25 for i in range(59)] + [110.0, 111.0, 112.0, 113.0]
    frame = frame_from_closes(closes, NOW - len(closes) * 900_000)
    memory = {"watchlist": {}, "m15_crossover_radar": []}

    result = process_m15_watchlist_frame("BTC/USDT", frame, memory, now_ms=NOW)

    assert result["crossover_status"] == "CROSS_CONFIRMED"
    assert result["cross_age_bars"] == 3
    assert memory["m15_crossover_radar"][0]["symbol"] == "BTC/USDT"
    assert memory["m15_crossover_radar_meta"]["source"] == "SCANNER_2_SHARED_CLOSED_FRAME"
    assert memory["m15_crossover_radar_meta"]["entry_authorization"] is False


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



def test_legacy_radar_hook_does_not_launch_duplicate_exchange_scan(monkeypatch):
    import inspect
    import ggpbot

    calls = []
    monkeypatch.setattr(ggpbot, "get_ohlcv_safe", lambda *args, **kwargs: calls.append(args))
    assert ggpbot.run_m15_crossover_radar() is False
    assert calls == []
    assert not hasattr(ggpbot, "M15_RADAR_SCAN_INTERVAL_SECONDS")
    assert not hasattr(ggpbot, "M15_RADAR_BATCH_SIZE")
    main_loop_source = inspect.getsource(ggpbot._original_main_loop_sniper)
    assert "run_m15_crossover_radar" not in main_loop_source


def test_institutional_flow_unavailable_never_publishes_synthetic_defaults(monkeypatch):
    import ggpbot

    monkeypatch.setattr(ggpbot, "_discovery_paused", lambda: False)
    monkeypatch.setattr(ggpbot, "get_ohlcv_safe", lambda *args, **kwargs: None)
    ggpbot.DASHBOARD_STATE["institutional_flow"] = {}

    assert ggpbot.update_institutional_flow_scanner() is False
    flow = ggpbot.DASHBOARD_STATE["institutional_flow"]
    assert flow["status"] == "DATA_UNAVAILABLE"
    assert flow["banker_pressure"] is None
    assert flow["institutional_bias"] is None


def test_institutional_flow_failure_preserves_last_valid_snapshot_as_stale(monkeypatch):
    import ggpbot

    monkeypatch.setattr(ggpbot, "_discovery_paused", lambda: False)
    monkeypatch.setattr(ggpbot, "get_ohlcv_safe", lambda *args, **kwargs: None)
    ggpbot.DASHBOARD_STATE["institutional_flow"] = {
        "status": "VALID", "banker_pressure": 71.2,
        "last_valid_update_ms": NOW - 1000,
    }

    assert ggpbot.update_institutional_flow_scanner() is False
    flow = ggpbot.DASHBOARD_STATE["institutional_flow"]
    assert flow["status"] == "STALE"
    assert flow["banker_pressure"] == 71.2
    assert flow["last_valid_update_ms"] == NOW - 1000


def test_legacy_two_stage_scanner_cadence_is_preserved():
    import ggpbot

    assert ggpbot.GLOBAL_SCAN_INTERVAL == 1200
    assert ggpbot.INSTITUTIONAL_WATCHLIST_REFRESH_SECONDS == 20
    assert ggpbot.INSTITUTIONAL_WATCHLIST_BATCH_SIZE == 12
    assert ggpbot.INSTITUTIONAL_WATCHLIST_MAX_SYMBOLS == 120


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



