from __future__ import annotations

from scripts.run_p122_pod_c_execution_cost_audit import (
    ExecutionTrade,
    entry_cost_bucket,
    evaluate_policy,
    spread_bucket,
    summarize_bucket,
)


def test_spread_and_cost_buckets() -> None:
    assert spread_bucket(None) == "missing"
    assert spread_bucket(0.49) == "lt_0p5"
    assert spread_bucket(0.5) == "0p5_to_1"
    assert spread_bucket(1.0) == "1_to_2"
    assert spread_bucket(2.0) == "2_to_4"
    assert spread_bucket(4.0) == "gte_4"

    assert entry_cost_bucket(None) == "missing"
    assert entry_cost_bucket(7.49) == "lt_7p5"
    assert entry_cost_bucket(7.5) == "7p5_to_8"
    assert entry_cost_bucket(8.0) == "8_to_9"
    assert entry_cost_bucket(9.0) == "gte_9"


def test_policy_cap_spread_scales_only_expensive_touched_trades() -> None:
    rows = [
        trade("t1", -6.0, spread_bps=1.2, symbol="XYZ:GOLD"),
        trade("t2", 4.0, spread_bps=0.2, symbol="XYZ:CL"),
        trade("t3", 2.0, spread_bps=2.5, symbol="XYZ:SP500"),
    ]

    outcome = evaluate_policy("fixture", rows, "cap_spread_gte_1", 0.5)

    assert outcome.base_pnl_usd == 0.0
    assert outcome.touched_trades == 2
    assert outcome.touched_pnl_usd == -4.0
    assert outcome.adjusted_pnl_usd == 2.0
    assert outcome.delta_usd == 2.0
    assert outcome.touched_symbols == {"XYZ:GOLD": 1, "XYZ:SP500": 1}


def test_summary_tracks_fees_gross_and_costs() -> None:
    rows = [
        trade("t1", -2.0, gross_pnl_usd=-1.3, fees_usd=0.7, entry_cost_bps=8.2),
        trade("t2", 6.0, gross_pnl_usd=6.7, fees_usd=0.7, entry_cost_bps=7.4),
    ]

    summary = summarize_bucket("fixture", "spread_bps", "mixed", rows)

    assert summary.trades == 2
    assert summary.pnl_usd == 4.0
    assert summary.gross_pnl_usd == 5.4
    assert summary.fees_usd == 1.4
    assert summary.profit_factor == 3.0
    assert summary.avg_entry_cost_bps == 7.8


def trade(
    trade_id: str,
    pnl_usd: float,
    *,
    spread_bps: float = 0.5,
    entry_cost_bps: float = 7.5,
    gross_pnl_usd: float | None = None,
    fees_usd: float = 0.7,
    symbol: str = "XYZ:GOLD",
) -> ExecutionTrade:
    return ExecutionTrade(
        window="fixture",
        trade_id=trade_id,
        symbol=symbol,
        opened_at="2026-06-01T00:00:00Z",
        pnl_usd=pnl_usd,
        gross_pnl_usd=pnl_usd if gross_pnl_usd is None else gross_pnl_usd,
        fees_usd=fees_usd,
        target_notional_usd=100.0,
        fee_bps=7.0,
        spread_bps=spread_bps,
        entry_cost_bps=entry_cost_bps,
        activity_bucket="normal",
        trade_count_bucket="soft",
        bucket_notional_usd=50_000.0,
        bucket_trade_count=10.0,
    )
