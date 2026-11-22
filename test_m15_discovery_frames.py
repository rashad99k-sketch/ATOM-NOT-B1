import pandas as pd

from ggp_market_data import closed_ohlcv_frame, require_m15_timeframe


def test_strategy_timeframe_is_fixed_to_m15():
    assert require_m15_timeframe("15m") == "15m"
    assert require_m15_timeframe(" 15M ") == "15m"
    for unsupported in ("1m", "3m", "5m", "1h", "", None):
        try:
            require_m15_timeframe(unsupported)
        except ValueError as exc:
            assert "must be 15m" in str(exc)
        else:
            raise AssertionError(f"unsupported timeframe was accepted: {unsupported!r}")


def make_frame(timestamps):
    return pd.DataFrame({
        "timestamp": timestamps,
        "open": [1.0] * len(timestamps),
        "high": [2.0] * len(timestamps),
        "low": [0.5] * len(timestamps),
        "close": [1.5] * len(timestamps),
        "volume": [10.0] * len(timestamps),
    })


def test_closed_ohlcv_frame_excludes_forming_m15_candle():
    now_ms = 1_700_000_000_000
    closed_open = now_ms - 15 * 60 * 1000
    forming_open = now_ms - 2 * 60 * 1000
    frame = make_frame([closed_open, forming_open])

    result = closed_ohlcv_frame(frame, "15m", now_ms=now_ms)

    assert result is not None
    assert result["timestamp"].tolist() == [closed_open]


def test_closed_ohlcv_frame_accepts_bar_exactly_at_close_boundary():
    now_ms = 1_700_000_000_000
    candle_open = now_ms - 15 * 60 * 1000

    result = closed_ohlcv_frame(make_frame([candle_open]), "15m", now_ms=now_ms)

    assert result is not None
    assert result["timestamp"].tolist() == [candle_open]


def test_closed_ohlcv_frame_sorts_and_deduplicates_by_timestamp():
    now_ms = 1_700_000_000_000
    old_open = now_ms - 30 * 60 * 1000
    latest_open = now_ms - 15 * 60 * 1000
    frame = make_frame([latest_open, old_open, old_open])
    frame.loc[0, "close"] = 3.0
    frame.loc[2, "close"] = 4.0

    result = closed_ohlcv_frame(frame, "15m", now_ms=now_ms)

    assert result is not None
    assert result["timestamp"].tolist() == [old_open, latest_open]
    assert result["close"].tolist() == [4.0, 3.0]


def test_closed_ohlcv_frame_fails_closed_without_valid_millisecond_timestamps():
    assert closed_ohlcv_frame(pd.DataFrame({"close": [1.0]}), "15m") is None
    assert closed_ohlcv_frame(make_frame([1_700_000_000]), "15m") is None
    assert closed_ohlcv_frame(make_frame([None]), "15m") is None


def test_closed_ohlcv_frame_returns_none_when_no_candle_is_closed():
    now_ms = 1_700_000_000_000
    assert closed_ohlcv_frame(make_frame([now_ms - 60_000]), "15m", now_ms=now_ms) is None
