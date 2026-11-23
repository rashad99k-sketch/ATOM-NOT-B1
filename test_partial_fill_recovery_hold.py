from types import SimpleNamespace

import pandas as pd

import bingx_execution
import ggpbot


class _Exchange:
    def market(self, symbol):
        return {"contractSize": 1.0}

    def amount_to_precision(self, symbol, amount):
        return str(amount)

    def fetch_balance(self):
        return {"free": {"USDT": 1000.0}}

    def fetch_ticker(self, symbol):
        return {"last": 100.0}


class _PartialService:
    last_stop_request = None

    def __init__(self, exchange, journal=None, logger=None):
        self.exchange = exchange

    def submit_market_entry(self, **kwargs):
        return SimpleNamespace(
            state=bingx_execution.ExecutionState.PARTIAL_FILL,
            filled_qty=1.0, remaining_qty=1.0,
            average_fill_price=100.0, client_order_id="partial-client",
            exchange_order_id="partial-exchange",
            position_mode=bingx_execution.PositionMode.ONE_WAY.value,
            position_side="BOTH", trade_id="partial-trade",
            raw_order={"id": "partial-exchange", "filled": 1.0, "average": 100.0},
            reason="order_status=PARTIALLY_FILLED",
        )

    def place_and_verify_native_stop(self, **kwargs):
        type(self).last_stop_request = dict(kwargs)
        return "native-partial-stop"


def test_open_position_protects_partial_fill_and_keeps_recovery_lock(monkeypatch):
    previous_state = dict(ggpbot.STATE)
    previous_active = ggpbot._ACTIVE_TRADE
    previous_block = ggpbot.RECOVERY_BLOCK_ENTRIES
    try:
        ggpbot.STATE.clear()
        ggpbot.STATE.update({"open": False, "execution_unresolved": False, "recovery_hold": False})
        ggpbot._ACTIVE_TRADE = False
        ggpbot.RECOVERY_BLOCK_ENTRIES = False
        monkeypatch.setattr(ggpbot, "PAPER_MODE", False)
        monkeypatch.setattr(ggpbot, "ex", _Exchange())
        monkeypatch.setattr(ggpbot, "safe_api_call", lambda fn, *a, **k: fn(*a, **k))
        monkeypatch.setattr(ggpbot, "dynamic_spread_tolerance", lambda _symbol: 5.0)
        monkeypatch.setattr(ggpbot, "get_spread_bps", lambda _symbol: 1.0)
        monkeypatch.setattr(ggpbot, "log_execution", lambda *a, **k: None)
        monkeypatch.setattr(ggpbot, "TradeExecutionService", _PartialService)
        frame = pd.DataFrame({
            "timestamp": [1795132800000 + i * 900_000 for i in range(80)],
            "open": [100.0] * 80, "high": [101.0] * 80,
            "low": [99.0] * 80, "close": [100.5] * 80,
            "volume": [1000.0] * 80,
        })
        monkeypatch.setattr(ggpbot, "_integration_fetch_frame", lambda _symbol: frame.copy())
        monkeypatch.setattr(ggpbot, "_validate_ema915_vwap_location_first_entry_gate", lambda *a, **k: {
            "ready": True, "gate_kind": "EMA915_VWAP_LOCATION_FIRST",
            "signal_candle_id": "cross:pullback:rejection:confirmation",
            "entry_mode": "PULLBACK_RETEST_REJECTION_BREAK",
        })
        management = {
            "entry_gate_snapshot": {
                "ready": True, "gate_kind": "EMA915_VWAP_LOCATION_FIRST",
                "signal_candle_id": "cross:pullback:rejection:confirmation",
                "entry_mode": "PULLBACK_RETEST_REJECTION_BREAK",
                "trigger": "EMA9_EMA15_SESSION_VWAP_LOCATION_FIRST",
            },
            "entry_gate_kind": "EMA915_VWAP_LOCATION_FIRST",
            "entry_gate_timeframe": "15m", "tp1_price": 105.0,
            "location_context": {},
        }
        result = ggpbot.open_position(
            "BUY", 2.0, "BTC/USDT:USDT", stop_loss=95.0,
            trade_id="partial-trade", management_state=management,
        )
        assert result is None
        assert _PartialService.last_stop_request["quantity"] == 1.0
        assert _PartialService.last_stop_request["trigger_price"] == 95.0
        assert ggpbot.STATE["open"] is True
        assert ggpbot.STATE["qty"] == 1.0
        assert ggpbot.STATE["recovery_hold"] is True
        assert ggpbot.STATE["execution_unresolved"] is True
        assert ggpbot.STATE["native_sl_verified"] is True
        assert ggpbot.RECOVERY_BLOCK_ENTRIES is True
        assert ggpbot._ACTIVE_TRADE is True
    finally:
        ggpbot.STATE.clear()
        ggpbot.STATE.update(previous_state)
        ggpbot._ACTIVE_TRADE = previous_active
        ggpbot.RECOVERY_BLOCK_ENTRIES = previous_block
