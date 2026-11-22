import pytest

from ggp_profit_brain import calculate_profit_metrics, price_move_target_ladder


def test_long_price_move_roe_and_net_pnl_are_separate():
    result = calculate_profit_metrics(
        side="BUY", entry_price=100, exit_price=100.25, quantity=1,
        leverage=20, fee_rate_per_side=0.0004,
        entry_slippage_bps=1, exit_slippage_bps=1, funding_cost=0.01,
    )

    assert result["available"] is True
    assert result["underlying_price_move_pct"] == pytest.approx(0.25)
    assert result["position_roe_pct"] == pytest.approx(5.0)
    assert result["gross_pnl"] == pytest.approx(0.25)
    assert result["fees_estimated"] == pytest.approx((100 + 100.25) * 0.0004)
    assert result["slippage_estimated"] == pytest.approx((100 + 100.25) / 10_000)
    assert result["net_pnl_estimated"] == pytest.approx(0.25 - 0.0801 - 0.020025 - 0.01)
    assert result["realized"] is False


def test_short_uses_directional_sign_without_conflating_price_move():
    result = calculate_profit_metrics(
        side="SELL", entry_price=100, exit_price=99.5, quantity=2, leverage=20,
    )

    assert result["underlying_price_move_pct"] == pytest.approx(-0.5)
    assert result["directional_price_move_pct"] == pytest.approx(0.5)
    assert result["gross_pnl"] == pytest.approx(1.0)
    assert result["position_roe_pct"] == pytest.approx(10.0)


def test_partial_close_and_contract_multiplier_scale_pnl_and_margin():
    partial = calculate_profit_metrics(
        side="BUY", entry_price=100, exit_price=101, quantity=0.4,
        leverage=20, contract_multiplier=10,
    )
    full = calculate_profit_metrics(
        side="BUY", entry_price=100, exit_price=101, quantity=1,
        leverage=20, contract_multiplier=10,
    )

    assert partial["gross_pnl"] == pytest.approx(4.0)
    assert partial["entry_margin"] == pytest.approx(20.0)
    assert partial["position_roe_pct"] == pytest.approx(20.0)
    assert full["gross_pnl"] == pytest.approx(10.0)


def test_invalid_or_missing_inputs_do_not_emit_false_zero_profit():
    result = calculate_profit_metrics(
        side="BUY", entry_price=100, exit_price=0, quantity=1, leverage=20,
    )

    assert result == {"available": False, "reason": "invalid_or_missing_inputs"}


def test_price_move_target_ladder_separates_price_targets_from_roe():
    long = price_move_target_ladder("BUY", 100.0)
    short = price_move_target_ladder("SELL", 100.0)

    assert long["basis"] == "UNDERLYING_PRICE_MOVEMENT_NOT_LEVERAGED_ROE"
    assert long["targets"] == {"0.50%": 100.5, "1.00%": 101.0, "2.00%": 102.0, "3.00%": 103.0}
    assert short["targets"] == {"0.50%": 99.5, "1.00%": 99.0, "2.00%": 98.0, "3.00%": 97.0}
    assert long["orders_created"] is False


def test_price_move_target_ladder_rejects_invalid_inputs():
    assert price_move_target_ladder("BUY", 0)["available"] is False
    assert price_move_target_ladder("HOLD", 100)["available"] is False
