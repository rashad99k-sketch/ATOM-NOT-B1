"""Closed-candle EMA9/EMA15 + Session VWAP confluence diagnostics.

VWAP events are typed separately: price crossing VWAP and EMA9 crossing
VWAP are distinct observations. This module never authorizes an entry.
"""
from __future__ import annotations
from typing import Any, Dict, Optional, Sequence
import math

DEFAULT_WINDOW_CANDLES = 4


def _num(value: Any) -> Optional[float]:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _cross_direction(previous: Any, current: Any) -> Optional[str]:
    a, b = _num(previous), _num(current)
    if a is None or b is None:
        return None
    if a <= 0 < b:
        return "BUY"
    if a >= 0 > b:
        return "SELL"
    return None


def detect_triple_confluence(
    timestamps: Sequence[Any], close: Sequence[Any], ema9: Sequence[Any],
    ema15: Sequence[Any], vwap: Sequence[Any], *, window_candles: int = DEFAULT_WINDOW_CANDLES,
    direction: Optional[str] = None, atr: Optional[float] = None,
    adx: Optional[float] = None, di_plus: Optional[float] = None,
    di_minus: Optional[float] = None, minimum_adx: float = 18.0,
) -> Dict[str, Any]:
    """Return the nearest auditable triple event using only supplied closed bars.

    A valid event requires a confirmed EMA9/EMA15 cross and at least one same-
    direction VWAP event (PRICE_VWAP_CROSS or EMA9_VWAP_CROSS) within the
    configurable candle window. Trend filtering may downgrade it to WEAK.
    """
    sequences = (timestamps, close, ema9, ema15, vwap)
    size = min(map(len, sequences)) if all(hasattr(x, "__len__") for x in sequences) else 0
    window = max(1, min(12, int(window_candles)))
    result: Dict[str, Any] = {
        "status": "INVALID", "direction": "UNKNOWN", "event_id": None,
        "ema_cross_timestamp": None, "ema_cross_price": None,
        "vwap_event_timestamp": None, "vwap_event_price": None,
        "vwap_event_type": None, "bars_between_events": None,
        "window_candles": window, "ema9": _num(ema9[-1]) if size else None,
        "ema15": _num(ema15[-1]) if size else None,
        "vwap": _num(vwap[-1]) if size else None,
        "distance_at_cross_atr": None, "confluence_strength": 0.0,
        "reasons": ["insufficient_or_invalid_closed_candles"],
        "entry_authorization": False,
    }
    if size < 3:
        return result
    arrays = [[_num(x) for x in seq[-size:]] for seq in sequences]
    if any(any(item is None for item in arr) for arr in arrays):
        result["reasons"] = ["non_finite_indicator_or_timestamp"]
        return result
    ts, c, e9, e15, vw = arrays
    expected = str(direction or "").upper()
    cross_idx = None
    cross_side = None
    for i in range(size - 1, max(0, size - window - 1), -1):
        cross_side = _cross_direction(e9[i - 1] - e15[i - 1], e9[i] - e15[i])
        if cross_side:
            cross_idx = i
            break
    if cross_idx is None:
        result["reasons"] = ["no_recent_closed_ema9_ema15_cross"]
        return result
    if expected in {"BUY", "SELL"} and expected != cross_side:
        result.update({"direction": cross_side, "ema_cross_timestamp": int(ts[cross_idx]),
                       "ema_cross_price": c[cross_idx], "reasons": ["direction_mismatch"]})
        return result
    vwap_events = []
    for i in range(1, size):
        price_side = _cross_direction(c[i - 1] - vw[i - 1], c[i] - vw[i])
        ema_side = _cross_direction(e9[i - 1] - vw[i - 1], e9[i] - vw[i])
        if price_side == cross_side:
            vwap_events.append((abs(i - cross_idx), i, "PRICE_VWAP_CROSS", price_side))
        if ema_side == cross_side:
            vwap_events.append((abs(i - cross_idx), i, "EMA9_VWAP_CROSS", ema_side))
    matched = [event for event in vwap_events if event[0] <= window]
    result.update({"direction": cross_side, "ema_cross_timestamp": int(ts[cross_idx]),
                   "ema_cross_price": c[cross_idx], "reasons": []})
    if not matched:
        result["status"] = "WEAK"
        result["reasons"] = ["no_directional_vwap_cross_within_window"]
        return result
    matched.sort(key=lambda item: (item[0], item[2] != "PRICE_VWAP_CROSS", item[1]))
    bars, vwap_idx, event_type, _ = matched[0]
    unit = _num(atr)
    distance = abs(e9[cross_idx] - e15[cross_idx]) / unit if unit and unit > 0 else None
    aligned = (e9[-1] > e15[-1] and c[-1] > vw[-1]) if cross_side == "BUY" else (
        e9[-1] < e15[-1] and c[-1] < vw[-1]
    )
    adx_value, dip, dim = _num(adx), _num(di_plus), _num(di_minus)
    directional_di = (dip is not None and dim is not None and dip > dim) if cross_side == "BUY" else (
        dip is not None and dim is not None and dim > dip
    )
    strong = aligned and (adx_value is None or adx_value >= float(minimum_adx)) and (
        directional_di or dip is None or dim is None
    )
    result.update({
        "status": "VALID" if strong else "WEAK", "vwap_event_timestamp": int(ts[vwap_idx]),
        "vwap_event_price": c[vwap_idx], "vwap_event_type": event_type,
        "bars_between_events": bars, "distance_at_cross_atr": round(distance, 6) if distance is not None else None,
        "confluence_strength": round(max(0.0, 1.0 - bars / (window + 1.0)) * (1.0 if aligned else 0.5), 4),
        "reasons": (["ema_cross_and_directional_vwap_cross_with_trend_alignment"] if strong else
                    ["triple_cross_matched_but_trend_or_alignment_is_weak"]),
        "event_id": f"M15:{int(ts[cross_idx])}:{cross_side}:{event_type}:{int(ts[vwap_idx])}",
        "entry_authorization": False,
    })
    return result
