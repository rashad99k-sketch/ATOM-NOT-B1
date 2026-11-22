# NAISO-LIVE Forensic Audit — M15 Strategy Upgrade

**Baseline:** public repository `main`, commit `4cc2bdbcf3dd1559b7c81a458922df2a23506a2e` (2026-11-22). The uploaded ZIP has a distinct older snapshot (`5d7cf3f38ee4d319d0fc2ab9239907bb92b044c1`) and was not used as the implementation baseline. The separate Early Crossover Radar specification referenced by the prompt was not present in the supplied attachments or current repository.

**Safety boundary:** audit only; no exchange connection, order placement, credential edits, leverage/sizing/risk changes, LIVE enablement, deployment, or merge.

## Existing execution path and architecture

The live market-data entry path in `ggpbot.py` fetches OHLCV using the environment variable `INTERVAL` (default `15m`) and shares per-symbol scanner snapshots through `_get_scanner_frame`. Discovery output is a hint. The institutional bridge merges candidate sources and independently evaluates BUY/SELL; `run_unified_institutional_entry_cycle` obtains closed candles and recomputes readiness. Actual authorization then crosses `execute_entry`, the EMA9/EMA15/VWAP closed-candle pullback gate, risk checks, and a final ticker/frame revalidation before `open_position` submits through `TradeExecutionService`. Durable gate-event deduplication and an execution lock protect the order boundary.

For management, `LiveTradeManager._apply_management` asks its board/Unified Profit Brain for a decision, then dispatches verified partial/full closes through strict close helpers. Exchange order/position deltas are checked before quantities and close state are updated. The code retains TP1/TP2 behavior for non-scalp positions and supports a single-target full close for scalp mode. No evidence from this read-only audit justified changes to protected execution, sizing, leverage, native stop, synchronization, dashboard, Telegram, or management-state components.

## Findings

1. **M15 is the default, not an enforced production invariant.** `ggpbot.py` reads arbitrary `INTERVAL` at line 787. `roro_config.py` rejects non-15m configuration, but the main executable does not import that guard. Therefore scanner and strategy paths can run with a non-M15 timeframe if the environment is changed.
2. **Several discovery scanners use the still-forming bar.** RF (`scan_market_rf`), V2 (`smart_scanner_v2`), and radar consume `_get_scanner_frame` directly; that helper returns the raw fetched frame. The professional scan separately filters closed bars, and entry authorization independently validates closed candles. This creates inconsistent, unstable discovery evidence, although it does not by itself bypass the entry gate.
3. **VWAP has conflicting definitions.** `ggpbot.compute_vwap` cumulatively weights typical price over the supplied rolling frame without a session reset. `roro_confluence.compute_session_vwap` resets on UTC-day boundaries, uses OHLC4, and filters unfinished candles. The generic label can therefore refer to different values in separate parts of the system.
4. **Indicator methodology differs by engine.** Main EMA uses pandas `ewm(span, adjust=False)`; main ATR uses exponential smoothing. The professional scanner uses rolling ATR. This is an architectural difference needing explicit validation before attempting broad unification.
5. **Separate OHLCV cache behaviors exist.** Main cache keys include symbol/timeframe/limit; scanner cache keys are symbol-only and short-lived. HTF OHLCV is fixed at 1h and has a less strict normalization/cache minimum than primary OHLCV. These are secondary improvement candidates.
6. **Profit and execution protections are materially present.** The close flow checks reduce-only order outcomes and actual position deltas, treats ambiguous close submissions conservatively, and avoids estimating live realized futures PnL from simple amount × price. A helper named `get_realized_pnl_for_symbol` does calculate cashflow-like values, but repo search found no callers; this is a latent hazard, not an observed active path.
7. **OpportunityPipeline is not the main live entry path.** The runtime uses the institutional bridge; the standalone opportunity pipeline is not instantiated in the production scanner. Pipeline tests alone do not prove runtime authorization behavior.
8. **Crossover and dashboard coverage is incomplete for the requested scope.** Tests cover closed-candle EMA/VWAP pullback gates, event journaling, scanner behavior, and profit decisions, but do not cover raw RF/V2/radar forming-bar effects, non-M15 startup configuration, or a full visual per-symbol crossover radar.

## Proposed low-risk implementation scope

- Enforce the existing M15-only design at the actual `ggpbot.py` configuration boundary.
- Ensure shared discovery snapshots exclude unfinished candles, and fail closed when timestamps/timeframe are insufficient to classify candle closure.
- Add deterministic tests for timeframe rejection, closed-candle filtering, crossover/watchlist evidence states as implemented, and regression of protected entry/close paths.
- Document the VWAP inconsistency and defer semantic consolidation until all consumers are mapped; avoid silently changing strategy values.
- Preserve all existing execution, risk, sizing, leverage, native-stop, exchange-sync, and profit-close authority.

## Test baseline

No tests were run during audit: the environment lacks `pytest` (`python3 -m pytest` reports `No module named pytest`). Repository CI installs `requirements.txt` and runs `python -m pytest -q`. Test results will be reported only after dependencies are available and commands actually run.


## Implemented on `feature/m15-radar-hardening`

- Added `require_m15_timeframe()` to `ggp_market_data.py` and applied it when `ggpbot.py` initializes `INTERVAL`. Non-15m configuration now fails fast; values are not silently coerced to another strategy timeframe.
- Added `closed_ohlcv_frame()` for discovery snapshots. It requires Unix-millisecond timestamps, sorts rows, deduplicates by timestamp, and returns only candles whose 15-minute close time has passed. Missing/malformed timestamps and data with no closed candles return unavailable rather than a guessed signal.
- Updated `_get_scanner_frame()` so RF, V2, professional, and radar consumers share cached raw OHLCV but receive only closed M15 data. Closure is reevaluated on cache reads, so a previously forming candle can enter the closed view after its interval ends.
- Added `test_m15_discovery_frames.py` with six tests for M15 enforcement, unfinished/closed candle boundaries, duplicate/out-of-order timestamps, and fail-closed cases.

No changes were made to execution, position sizing, risk allocation, leverage, native stops, exchange synchronization, management authority, dashboards, Telegram, credentials, or LIVE/paper settings. No real BingX calls were made.

## Verification

- `.venv/bin/python -m pytest -q test_m15_discovery_frames.py`: **6 passed**.
- `.venv/bin/python -m py_compile ggp_market_data.py ggpbot.py test_m15_discovery_frames.py`: passed.
- `.venv/bin/python -m pytest -q`: **606 passed**, one pandas deprecation warning in `test_professional_discovery_scanner.py` (`Timestamp.utcnow`); exit code 0.
- `git diff --check`: passed.

## Remaining acceptance work

This is a safety-hardening slice, not completion of the full master prompt. A full per-symbol visual crossover watchlist/chart, early PRE_CROSS state machine, deeper liquidity/volume/candle/regime evidence, and expansion of adaptive management tests were not implemented. VWAP definitions remain intentionally unchanged pending a complete consumer-by-consumer migration plan. No PR was opened: the GitHub connector is not enabled and `gh` is unauthenticated. Nothing has been merged or deployed.
