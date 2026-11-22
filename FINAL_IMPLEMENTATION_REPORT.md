# NAISO-LIVE M15 Crossover Upgrade — Implementation Report

## Delivery basis and safety

Implementation is based on public repository `main` at `4cc2bdbcf3dd1559b7c81a458922df2a23506a2e`, not the older uploaded ZIP snapshot. The branch is `feature/m15-radar-hardening`. No live account, exchange connection, order placement, secrets, leverage, risk allocation, Render deployment, or merge was used. All new strategy calculations are observational or planning-only unless they pass through the pre-existing Entry Gate and execution authority.

## Added files

- `m15_crossover_radar.py` implements a deterministic M15 radar over a closed-candle history plus an optional forming candle. It reports `PRE_CROSS`, provisional `CROSS_DETECTED`, closed `CROSS_CONFIRMED`, `INVALIDATED`, `EXPIRED`, or no active cross; it supplies EMA9/EMA15/session-VWAP values, ATR, relative volume, candle evidence, timestamps, and chart series. Its result always has `entry_authorization: false`.
- `test_m15_discovery_frames.py` covers M15-only timeframe enforcement and strict closed-candle timestamp handling.
- `test_m15_crossover_radar.py` covers bullish and bearish closed crosses, intrabar-only crosses, pre-cross, stale data, incomplete data, and integration with the shared scanner cache.
- `test_profit_metrics.py` covers long/short movement versus ROE, partial/full quantity scaling, contract multiplier, fee/slippage/funding assumptions, and target ladder levels.

## Modified files and behavior

- `ggp_market_data.py` now provides `require_m15_timeframe()` and `closed_ohlcv_frame()`. Discovery data must have valid Unix-millisecond timestamps; the helper sorts/deduplicates them and excludes unfinished bars.
- `ggpbot.py` enforces M15 at the runtime configuration boundary. RF, V2, professional, and legacy radar scanners receive closed M15 frames from their existing shared cache. The same existing global scanner cycle also computes crossover observations for the scanned universe. The existing dashboard `/data` payload includes these observations, and the dashboard shows a per-symbol EMA9/EMA15/session-VWAP SVG chart with confirmed or provisional markers, signal time, state, reason, and available institutional watchlist OB/FVG/liquidity context.
- `ggp_profit_brain.py` now exposes `calculate_profit_metrics()` for scenario analysis and `price_move_target_ladder()` for 0.50%, 1%, 2%, and 3% underlying price-move reference levels. These helpers are explicitly non-realized and do not alter the existing live PnL booking, management mode, TP1/TP2, close, or stop-loss paths.

## Existing systems preserved

The existing institutional watchlist, liquidity/volume/candle/structure engines, session VWAP implementation used by the entry gate, M15 pullback/rejection/continuation Entry Gate, no-chase/risk validation, event deduplication, TradeExecutionService, Native SL, exchange synchronization, verified close handling, Unified Profit Brain, scalp single-target full-close behavior, TP1/TP2 and dashboard/Telegram interfaces were not bypassed or replaced. The crossover radar has no order capability. The existing entry path remains the final authority.

The existing profit brain already distinguishes scalp from trend management and chooses conservative range targets from available evidence. The new price target ladder is informational only; it does not retune the existing TP policy or promise any target will be reached. `calculate_profit_metrics()` is a scenario estimator with explicit fee, slippage, funding, leverage, and contract-multiplier inputs; the exchange's verified realized-PnL evidence remains authoritative for actual closes.

## Verification performed

- Final full regression suite on the current implementation: **620 passed**, with **1 existing pandas deprecation warning** (`Timestamp.utcnow` in `test_professional_discovery_scanner.py`).
- Latest targeted M15 discovery/radar and profit-metric suite: **19 passed**.
- Python compile check passed for the changed Python modules.
- Dashboard rendered locally through Flask test client with HTTP 200 and the new radar panel present.
- Extracted dashboard JavaScript passed `node --check` after fixing a discovered escaping defect.
- `git diff --check` passed after the implementation changes.

## Known remaining gaps and constraints

The radar evaluates the exchange's M15 OHLCV snapshot, not every trade tick. An intrabar signal is explicitly provisional and never confirmed until the M15 candle closes. The chart is a compact inline SVG rather than an interactive candlestick/charting package. Radar scanning shares the existing bounded scanner universe and only enriches order blocks, FVG, liquidity, and regime values when a recent institutional watchlist result is available; otherwise it marks those values unavailable instead of manufacturing evidence.

The project still has a VWAP naming discrepancy outside the M15 entry authority: `ggpbot.compute_vwap()` is rolling-window cumulative VWAP, while `roro_confluence.compute_session_vwap()` is session-reset VWAP. They were not silently unified because changing legacy consumers could change trading behavior. The separately referenced Early Crossover spec was not included in the attachments.

A Pull Request could not be opened because GitHub integration is not enabled in this session. No PR or merge is claimed. The completed changes will be committed locally on the feature branch; the ZIP will be generated from that committed tree.

## Run instructions

Use Python 3.10+ (the code was tested under Python 3.12), then in the extracted project directory:

```bash
python -m venv .venv
# Linux/macOS
. .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Copy `.env.example` to `.env` if local overrides are needed. Keep `PAPER_MODE=true`; `INTERVAL` must be `15m` or startup fails. Do not add live credentials to the project archive. On Windows, use the included `RUN_WINDOWS.bat` or `RUN.bat` per the existing project instructions. To run tests, use `python -m pytest -q`. The archive contains no `.env`, exchange credentials, virtual environment, or runtime logs.
