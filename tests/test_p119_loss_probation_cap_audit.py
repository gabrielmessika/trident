from scripts.run_p119_loss_probation_cap_audit import (
    TradeRow,
    apply_loss_probation,
    probation_multiplier,
    summarize_periods,
)


def test_probation_multiplier_caps_after_negative_history_and_rehabilitates() -> None:
    assert (
        probation_multiplier(
            {"trades": 1, "pnl_usd": -10.0, "expectancy_usd": -10.0, "profit_factor": 0.0},
            min_closed_trades=2,
            max_rolling_pnl_usd=-2.0,
            max_profit_factor=0.8,
            rehab_min_profit_factor=1.05,
            rehab_min_expectancy_usd=0.0,
            cap_multiplier=0.5,
        )[0]
        == 1.0
    )
    assert probation_multiplier(
        {"trades": 2, "pnl_usd": -2.1, "expectancy_usd": -1.05, "profit_factor": 0.0},
        min_closed_trades=2,
        max_rolling_pnl_usd=-2.0,
        max_profit_factor=0.8,
        rehab_min_profit_factor=1.05,
        rehab_min_expectancy_usd=0.0,
        cap_multiplier=0.5,
    ) == (0.5, "loss_probation")
    assert probation_multiplier(
        {"trades": 3, "pnl_usd": 3.0, "expectancy_usd": 1.0, "profit_factor": 2.0},
        min_closed_trades=2,
        max_rolling_pnl_usd=-2.0,
        max_profit_factor=0.8,
        rehab_min_profit_factor=1.05,
        rehab_min_expectancy_usd=0.0,
        cap_multiplier=0.5,
    ) == (1.0, "rehabilitated")


def test_loss_probation_applies_cap_from_prior_trades_only() -> None:
    trades = [
        trade("trade_1", "2026-06-01T00:00:00Z", -2.0),
        trade("trade_2", "2026-06-01T01:00:00Z", -1.0),
        trade("trade_3", "2026-06-01T02:00:00Z", -4.0),
        trade("trade_4", "2026-06-04T00:00:00Z", 6.0),
    ]

    adjusted = apply_loss_probation(
        trades,
        rolling_lookback=4,
        min_closed_trades=2,
        max_rolling_pnl_usd=-2.0,
        max_profit_factor=0.8,
        rehab_min_profit_factor=1.05,
        rehab_min_expectancy_usd=0.0,
        cap_multiplier=0.5,
    )
    summaries = summarize_periods(adjusted, split_date="2026-06-03")

    assert [row.cap_multiplier for row in adjusted] == [1.0, 1.0, 0.5, 0.5]
    assert adjusted[2].adjusted_pnl_usd == -2.0
    assert adjusted[3].adjusted_pnl_usd == 3.0
    assert summaries[0].original_pnl_usd == -1.0
    assert summaries[0].adjusted_pnl_usd == -2.0
    assert summaries[1].capped_trades == 1
    assert summaries[2].capped_trades == 1


def trade(trade_id: str, closed_at: str, pnl: float) -> TradeRow:
    return TradeRow(
        trade_id=trade_id,
        opened_at=closed_at,
        closed_at=closed_at,
        symbol="ETH",
        setup="trend_pullback_long",
        close_reason="stop_hit",
        target_notional_usd=200.0,
        original_pnl_usd=pnl,
    )
