# Adaptive Impulse Entry Implementation

## Scope
Add a narrowly-qualified no-pullback continuation entry to the existing M15 EMA9/EMA15 + Session VWAP path. The established pullback, micro-pullback, institutional retest, macro-pullback, and reversal paths remain in place.

## Impulse authorization
A fresh closed-candle EMA9/EMA15 cross must remain within the existing cross freshness window. Directional EMA/VWAP alignment is mandatory. The latest closed candle must close beyond the preceding 10-candle range (BOS). A valid causal zone must already exist and overlap the impulse close or live price. The impulse must satisfy all three supporting confirmations:
- relative volume >= 1.30 versus the preceding 20 completed candles;
- candle body >= 0.80 ATR and full range >= 1.20 ATR;
- body/range >= 0.60 and directional close location in the final 22% of the range (BUY upper 22%, SELL lower 22%).

Live extension from EMA9 must remain between -0.10 ATR and the lesser of 1.0 ATR or the configured max_extension_atr. A qualified but overextended BOS is classified NO_CHASE, not executable.

## Execution integrity
The existing execute_entry and open_position gates re-run the same EMA915 validator. The same signal_candle_id and entry_mode must still match at the actual ticker price. The LIVE institutional authorization explicitly permits IMPULSE_ENTRY only with EMA915_VWAP_IMPULSE_VALID, the cross evidence, a causal-zone evidence item, and directional structure/liquidity evidence. No alternate order path is added.

## Protected systems
No changes are intended to sizing, leverage, risk caps, SL/TP geometry, execution service, native SL verification, position synchronization, profit management, scanner cadence, Dashboard/API/Telegram, or rate-limit handling.

## Validation
CI is required. This code change is not a claim of profitability or a substitute for PAPER/BingX Demo validation. Live trading must remain disabled for acceptance until CI and exchange demo checks pass.
