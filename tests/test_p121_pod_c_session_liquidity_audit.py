from __future__ import annotations

from datetime import datetime, timezone

from scripts.run_p121_pod_c_session_liquidity_audit import (
    TradeRow,
    evaluate_policy,
    session_bucket,
    summarize_bucket,
)


def test_session_bucket_uses_us_cash_half_hour_boundary() -> None:
    assert session_bucket(datetime(2026, 6, 1, 6, 59, tzinfo=timezone.utc)) == "asia_overnight"
    assert session_bucket(datetime(2026, 6, 1, 7, 0, tzinfo=timezone.utc)) == "europe_morning"
    assert session_bucket(datetime(2026, 6, 1, 13, 29, tzinfo=timezone.utc)) == "us_premarket"
    assert session_bucket(datetime(2026, 6, 1, 13, 30, tzinfo=timezone.utc)) == "us_cash"
    assert session_bucket(datetime(2026, 6, 1, 20, 0, tzinfo=timezone.utc)) == "us_late"
    assert session_bucket(None) == "unknown"


def test_policy_cap_scales_touched_session_without_removing_trade() -> None:
    rows = [
        trade("t1", "us_late", -8.0),
        trade("t2", "us_cash", 4.0),
        trade("t3", "us_late", 2.0),
    ]

    outcome = evaluate_policy("fixture", rows, "cap_session_us_late", 0.5)

    assert outcome.base_pnl_usd == -2.0
    assert outcome.touched_trades == 2
    assert outcome.touched_pnl_usd == -6.0
    assert outcome.adjusted_pnl_usd == 1.0
    assert outcome.delta_usd == 3.0
    assert outcome.touched_winners == 1
    assert outcome.touched_losers == 1


def test_bucket_summary_profit_factor_and_symbols() -> None:
    rows = [
        trade("t1", "us_cash", -2.0, symbol="XYZ:GOLD"),
        trade("t2", "us_cash", 6.0, symbol="XYZ:CL"),
    ]

    summary = summarize_bucket("fixture", "session", "us_cash", rows)

    assert summary.trades == 2
    assert summary.pnl_usd == 4.0
    assert summary.win_rate == 0.5
    assert summary.profit_factor == 3.0
    assert summary.symbols == {"XYZ:CL": 1, "XYZ:GOLD": 1}


def trade(
    trade_id: str,
    session: str,
    pnl: float,
    *,
    symbol: str = "XYZ:GOLD",
    activity_bucket: str = "high",
    trade_count_bucket: str = "high",
) -> TradeRow:
    return TradeRow(
        window="fixture",
        trade_id=trade_id,
        symbol=symbol,
        side="long",
        opened_at="2026-06-01T20:00:00Z",
        hour_utc=20.0,
        session=session,
        pnl_usd=pnl,
        confidence=0.7,
        market_cluster="gold",
        activity_bucket=activity_bucket,
        trade_count_bucket=trade_count_bucket,
        close_reason="time_stop",
    )
