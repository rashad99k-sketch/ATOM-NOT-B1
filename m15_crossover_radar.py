"""M15-only crossover radar; discovery evidence only, never an execution authority."""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from ggp_market_data import closed_ohlcv_frame, require_m15_timeframe


def _finite(value: Any) -> Optional[float]:
    try:
        result = float(value)
        return result if np.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _empty(symbol: str, reason: str, now_ms: int) -> Dict[str, Any]:
    return {
        "symbol": symbol, "timeframe": "15m", "direction": "UNKNOWN",
        "current_price": None, "ema9": None, "ema15": None, "session_vwap": None,
        "ema_distance": None, "atr": None, "volume_ratio": None,
        "crossover_status": "DATA_UNAVAILABLE", "opportunity_state": "WAITING",
        "entry_readiness": "NOT_AUTHORIZED_BY_RADAR", "rejection_reason": reason,
        "last_update_ms": now_ms, "signal_timestamp_ms": None, "chart": None,
        "evidence": {}, "entry_authorization": False,
    }


def evaluate_m15_radar(symbol: str, raw_frame: Any, *, now_ms: Optional[int] = None,
                       max_closed_age_ms: int = 1_800_000) -> Dict[str, Any]:
    """Evaluate an M15 crossover candidate using closed bars plus a provisional bar.

    A provisional cross is reported as ``CROSS_DETECTED`` and cannot be
    ``CROSS_CONFIRMED``. This function only describes evidence and never grants
    entry authorization.
    """
    now_ms = int(now_ms if now_ms is not None else pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    symbol = str(symbol)
    require_m15_timeframe("15m")
    if raw_frame is None or not isinstance(raw_frame, pd.DataFrame) or raw_frame.empty:
        return _empty(symbol, "OHLCV_UNAVAILABLE", now_ms)
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    if not required.issubset(raw_frame.columns):
        return _empty(symbol, "OHLCV_COLUMNS_MISSING", now_ms)
    try:
        raw = raw_frame.copy()
        for column in ("timestamp", "open", "high", "low", "close", "volume"):
            raw[column] = pd.to_numeric(raw[column], errors="coerce")
        raw = raw.dropna(subset=list(required)).sort_values("timestamp").drop_duplicates("timestamp", keep="last")
        if raw.empty or (raw[list(required - {"timestamp"})] <= 0).any().any():
            # Low prices can be zero only for invalid market data; volume may be
            # zero for a valid candle, so validate OHLC separately below.
            price_cols = ["open", "high", "low", "close"]
            if raw.empty or (raw[price_cols] <= 0).any().any() or (raw["volume"] < 0).any():
                return _empty(symbol, "OHLCV_VALUES_INVALID", now_ms)
        if (raw[["open", "high", "low", "close"]] <= 0).any().any() or (raw["volume"] < 0).any():
            return _empty(symbol, "OHLCV_VALUES_INVALID", now_ms)
        raw["timestamp"] = raw["timestamp"].astype("int64")
    except Exception:
        return _empty(symbol, "OHLCV_VALUES_INVALID", now_ms)

    closed = closed_ohlcv_frame(raw, "15m", now_ms=now_ms)
    if closed is None or len(closed) < 30:
        return _empty(symbol, "INSUFFICIENT_CLOSED_M15_CANDLES", now_ms)
    closed = closed.tail(120).reset_index(drop=True)
    close = closed["close"].astype(float)
    ema9 = close.ewm(span=9, adjust=False).mean()
    ema15 = close.ewm(span=15, adjust=False).mean()
    delta = close.diff()
    true_range = pd.concat([
        closed["high"] - closed["low"],
        (closed["high"] - close.shift()).abs(),
        (closed["low"] - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr_series = true_range.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    atr = _finite(atr_series.iloc[-1])
    price = _finite(close.iloc[-1])
    e9 = _finite(ema9.iloc[-1])
    e15 = _finite(ema15.iloc[-1])
    if not all(x is not None and x > 0 for x in (atr, price, e9, e15)):
        return _empty(symbol, "INDICATORS_UNAVAILABLE", now_ms)

    try:
        from roro_confluence import compute_session_vwap
        vwap_series = compute_session_vwap(closed, 900, now_ms=now_ms)
        vwap = _finite(vwap_series.iloc[-1]) if vwap_series is not None and len(vwap_series) else None
    except Exception:
        vwap = None
    gap = float(ema9.iloc[-1] - ema15.iloc[-1])
    gap_prev = float(ema9.iloc[-2] - ema15.iloc[-2])
    gap_before = float(ema9.iloc[-3] - ema15.iloc[-3])
    gap_atr = abs(gap) / atr
    contracted = abs(gap) < abs(gap_prev) and abs(gap_prev) <= abs(gap_before) + 0.02 * atr
    confirmed_side = "BUY" if gap > 0 and gap_prev <= 0 else "SELL" if gap < 0 and gap_prev >= 0 else None
    signal_timestamp = int(closed["timestamp"].iloc[-1])
    closed_age = max(0, now_ms - (signal_timestamp + 900_000))
    if closed_age > int(max_closed_age_ms):
        expired = _empty(symbol, "STALE_CLOSED_CANDLE_DATA", now_ms)
        expired.update({"crossover_status": "EXPIRED", "opportunity_state": "EXPIRED"})
        return expired

    last_raw = raw.iloc[-1]
    latest_price = _finite(last_raw["close"])
    raw_is_forming = int(last_raw["timestamp"]) + 900_000 > now_ms
    intrabar_side = None
    if raw_is_forming and int(last_raw["timestamp"]) > signal_timestamp:
        live_close = pd.concat([close, pd.Series([float(last_raw["close"])])], ignore_index=True)
        live_ema9 = live_close.ewm(span=9, adjust=False).mean()
        live_ema15 = live_close.ewm(span=15, adjust=False).mean()
        live_gap = float(live_ema9.iloc[-1] - live_ema15.iloc[-1])
        if gap <= 0 < live_gap:
            intrabar_side = "BUY"
        elif gap >= 0 > live_gap:
            intrabar_side = "SELL"

    direction = confirmed_side or intrabar_side
    if direction is None and contracted and gap_atr <= 0.25:
        direction = "BUY" if gap < 0 and float(ema9.iloc[-1] - ema9.iloc[-2]) > 0 else \
                    "SELL" if gap > 0 and float(ema9.iloc[-1] - ema9.iloc[-2]) < 0 else "UNKNOWN"
    if confirmed_side:
        status = "CROSS_CONFIRMED"
        state = "WAITING_FOR_PULLBACK"
        reason = "CLOSED_M15_EMA9_EMA15_CROSS"
    elif intrabar_side:
        status = "CROSS_DETECTED"
        state = "WAITING_FOR_CANDLE_CLOSE"
        reason = "INTRABAR_CROSS_NOT_CONFIRMED"
    elif contracted and gap_atr <= 0.25:
        status = "PRE_CROSS"
        state = "CROSS_APPROACHING"
        reason = "EMA_DISTANCE_CONTRACTING_WITHIN_0.25_ATR"
    else:
        status = "NO_ACTIVE_CROSS"
        state = "WAITING"
        reason = "NO_CONFIRMED_OR_EARLY_CROSS"

    vol = closed["volume"].astype(float)
    vol_baseline = float(vol.iloc[-21:-1].mean()) if len(vol) >= 21 else 0.0
    volume_ratio = float(vol.iloc[-1] / vol_baseline) if vol_baseline > 0 else None
    candle = closed.iloc[-1]
    candle_range = float(candle["high"] - candle["low"])
    body_ratio = abs(float(candle["close"] - candle["open"])) / candle_range if candle_range > 0 else None
    close_position = (float(candle["close"] - candle["low"]) / candle_range) if candle_range > 0 else None
    displacement_atr = abs(float(candle["close"] - candle["open"])) / atr if atr > 0 else None
    price_extension_atr = abs(float(price) - float(e9)) / atr if atr > 0 else None
    if status == "CROSS_CONFIRMED" and direction:
        if direction == "BUY" and price <= e9:
            state, reason = "INVALIDATED", "CLOSED_PRICE_LOST_EMA9_AFTER_BULLISH_CROSS"
        elif direction == "SELL" and price >= e9:
            state, reason = "INVALIDATED", "CLOSED_PRICE_RECLAIMED_EMA9_AFTER_BEARISH_CROSS"
        else:
            after_cross = closed.iloc[-5:-1]
            touched = bool(((after_cross["low"] <= ema9.iloc[-5:-1] + 0.35 * atr) &
                            (after_cross["high"] >= ema9.iloc[-5:-1] - 0.35 * atr)).any()) if len(after_cross) else False
            if touched:
                state = "WAITING_FOR_CONFIRMATION"
                reason = "PULLBACK_NEAR_EMA9; ENTRY_GATE_CONFIRMATION_REQUIRED"
    # A crossover alone can never set readiness or authorization.
    chart_start = max(0, len(closed) - 60)
    chart = {
        "timestamps": [int(x) for x in closed["timestamp"].iloc[chart_start:]],
        "open": [float(x) for x in closed["open"].iloc[chart_start:]],
        "close": [float(x) for x in close.iloc[chart_start:]],
        "high": [float(x) for x in closed["high"].iloc[chart_start:]],
        "low": [float(x) for x in closed["low"].iloc[chart_start:]],
        "ema9": [float(x) for x in ema9.iloc[chart_start:]],
        "ema15": [float(x) for x in ema15.iloc[chart_start:]],
        "session_vwap": [(_finite(x)) for x in (vwap_series.iloc[chart_start:] if vwap_series is not None else [None] * (len(closed) - chart_start))],
        "cross_marker": {"timestamp": signal_timestamp, "direction": confirmed_side, "status": "CONFIRMED"} if confirmed_side else None,
        "intrabar_marker": {"timestamp": int(last_raw["timestamp"]), "direction": intrabar_side, "status": "PROVISIONAL"} if intrabar_side else None,
    }
    return {
        "symbol": symbol, "timeframe": "15m", "direction": direction or "UNKNOWN",
        "current_price": latest_price, "closed_price": price,
        "ema9": e9, "ema15": e15, "session_vwap": vwap,
        "ema_distance": gap, "ema_distance_atr": round(gap_atr, 5), "atr": atr,
        "volume_ratio": round(volume_ratio, 5) if volume_ratio is not None else None,
        "crossover_status": status, "opportunity_state": state,
        "entry_readiness": "NOT_AUTHORIZED_BY_RADAR",
        "rejection_reason": reason, "last_update_ms": now_ms,
        "signal_timestamp_ms": signal_timestamp if confirmed_side else int(last_raw["timestamp"]) if intrabar_side else None,
        "closed_candle_age_ms": closed_age,
        "liquidity_status": "ANALYZE_WITH_EXISTING_INSTITUTIONAL_WATCHLIST",
        "market_regime": "UNCLASSIFIED_BY_RADAR",
        "setup_quality": None, "opportunity_priority": None,
        "evidence": {
            "closed_m15_confirmed": bool(confirmed_side),
            "intrabar_only": bool(intrabar_side and not confirmed_side),
            "ema_gap_contracting": bool(contracted),
            "body_to_range": round(body_ratio, 5) if body_ratio is not None else None,
            "close_position": round(close_position, 5) if close_position is not None else None,
            "displacement_atr": round(displacement_atr, 5) if displacement_atr is not None else None,
            "price_extension_atr": round(price_extension_atr, 5) if price_extension_atr is not None else None,
            "volume_ratio": round(volume_ratio, 5) if volume_ratio is not None else None,
            "vwap_available": vwap is not None,
            "source": "CLOSED_M15_PLUS_PROVISIONAL_CURRENT_CANDLE",
        },
        "chart": chart,
        "entry_authorization": False,
    }
