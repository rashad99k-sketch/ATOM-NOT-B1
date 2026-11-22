"""Bounded, non-blocking local snapshots for crossover lifecycle events."""
from __future__ import annotations
from pathlib import Path
import queue
import re
import threading
from typing import Any, Dict, Optional

DEFAULT_ROOT = Path(__file__).resolve().parent / "data" / "crossover_snapshots"
_MAX_PENDING = 64
_QUEUE: "queue.Queue[tuple[dict, Path]]" = queue.Queue(maxsize=_MAX_PENDING)
_LOCK = threading.Lock()
_WORKER: Optional[threading.Thread] = None
_STATUS: Dict[str, Any] = {"queued": 0, "generated": 0, "dropped": 0, "last_error": None}


def _safe(value: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "unknown"))[:90] or "unknown"


def snapshot_relative_path(payload: Dict[str, Any]) -> str:
    event = payload.get("triple_confluence", {}) if isinstance(payload.get("triple_confluence"), dict) else {}
    folder = Path(_safe(payload.get("symbol"))) / _safe(event.get("event_id") or payload.get("lifecycle_event_id"))
    filename = f"{_safe(payload.get('snapshot_stage') or 'crossover')}_{int(payload.get('last_update_ms') or 0)}.png"
    return (folder / filename).as_posix()


def render_snapshot(payload: Dict[str, Any], root: Optional[str] = None) -> Optional[str]:
    """Render OHLC, EMA9/15, Session VWAP and volume from the provided cached chart."""
    plt = None
    fig = None
    try:
        import matplotlib
        matplotlib.use("Agg", force=True)
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        from datetime import datetime, timezone
        chart = payload.get("chart") or {}
        names = ("timestamps", "open", "high", "low", "close", "ema9", "ema15", "session_vwap")
        size = min((len(chart.get(key) or []) for key in names), default=0)
        if size < 3:
            return None
        ts = chart["timestamps"][-size:]
        op, hi, lo, cl = [chart[k][-size:] for k in ("open", "high", "low", "close")]
        e9, e15, vwap = [chart[k][-size:] for k in ("ema9", "ema15", "session_vwap")]
        raw_volume = list(chart.get("volume") or [])
        # Keep the volume series aligned with price even when an upstream cached
        # chart contains a shorter optional volume array.
        volume = raw_volume[-size:] if len(raw_volume) >= size else ([0.0] * (size - len(raw_volume)) + raw_volume)
        volume = [float(value) if value is not None else 0.0 for value in volume]
        dates = [datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc) for value in ts]
        fig, (ax, av) = plt.subplots(2, 1, figsize=(11, 6), sharex=True, gridspec_kw={"height_ratios": [4, 1]})
        fig.patch.set_facecolor("#0b1220")
        for axis in (ax, av):
            axis.set_facecolor("#0b1220")
            axis.tick_params(colors="#dbeafe", labelsize=8)
            for spine in axis.spines.values():
                spine.set_color("#334155")
            axis.grid(True, alpha=.15, color="#94a3b8")
        width = 7 / (24 * 60)
        for x, o, h, l, c in zip(dates, op, hi, lo, cl):
            color = "#00c087" if c >= o else "#f05252"
            ax.vlines(x, l, h, color=color, linewidth=.8)
            ax.bar(x, abs(c-o), bottom=min(o,c), width=width, color=color, edgecolor=color, linewidth=.4)
        ax.plot(dates, e9, color="#00d084", linewidth=1.4, label="EMA9")
        ax.plot(dates, e15, color="#ffb020", linewidth=1.4, label="EMA15")
        ax.plot(dates, vwap, color="#48a9ff", linewidth=1.4, label="Session VWAP")
        cross_ts = payload.get("triple_confluence", {}).get("ema_cross_timestamp")
        if cross_ts:
            cross_dt = datetime.fromtimestamp(int(cross_ts)/1000, tz=timezone.utc)
            ax.axvline(cross_dt, color="#e2e8f0", linestyle="--", linewidth=1, label="EMA9/15 cross")
        event = payload.get("triple_confluence", {})
        if event.get("vwap_event_timestamp"):
            vdt = datetime.fromtimestamp(int(event["vwap_event_timestamp"])/1000, tz=timezone.utc)
            ax.axvline(vdt, color="#a78bfa", linestyle=":", linewidth=1, label=str(event.get("vwap_event_type")))
        ax.set_title(f"{payload.get('symbol','?')} {payload.get('timeframe','15m')} · {payload.get('direction','?')} · {event.get('status','?')}", color="white")
        ax.legend(loc="upper left", fontsize=8, facecolor="#162033", labelcolor="white")
        av.bar(dates, volume, width=width, color="#64748b")
        av.set_ylabel("Volume", color="#dbeafe", fontsize=8)
        av.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M", tz=timezone.utc))
        fig.autofmt_xdate()
        fig.tight_layout()
        base = Path(root) if root else DEFAULT_ROOT
        relative = snapshot_relative_path(payload)
        folder = base / Path(relative).parent
        folder.mkdir(parents=True, exist_ok=True)
        target = base / relative
        fig.savefig(target, dpi=110, facecolor=fig.get_facecolor())
        return str(target.resolve())
    except Exception as exc:
        with _LOCK:
            _STATUS["last_error"] = f"{type(exc).__name__}:{str(exc)[:180]}"
        return None
    finally:
        if plt is not None and fig is not None:
            try:
                plt.close(fig)
            except Exception:
                pass


def _run_worker() -> None:
    while True:
        payload, root = _QUEUE.get()
        try:
            result = render_snapshot(payload, str(root))
            with _LOCK:
                if result:
                    _STATUS["generated"] += 1
        finally:
            _QUEUE.task_done()


def enqueue_snapshot(payload: Dict[str, Any], root: Optional[str] = None) -> bool:
    """Copy a small payload and enqueue in O(1); never blocks scanner on rendering."""
    global _WORKER
    if not isinstance(payload, dict) or not isinstance(payload.get("chart"), dict):
        return False
    with _LOCK:
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(target=_run_worker, name="crossover-snapshots", daemon=True)
            _WORKER.start()
    try:
        _QUEUE.put_nowait((dict(payload), Path(root) if root else DEFAULT_ROOT))
        with _LOCK:
            _STATUS["queued"] += 1
        return True
    except queue.Full:
        with _LOCK:
            _STATUS["dropped"] += 1
        return False


def snapshot_status() -> Dict[str, Any]:
    with _LOCK:
        return {**_STATUS, "pending": _QUEUE.qsize(), "capacity": _MAX_PENDING}
