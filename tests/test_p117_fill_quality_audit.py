from datetime import datetime, timezone

from scripts.run_p117_fill_quality_audit import (
    FillQualityRow,
    SnapshotPoint,
    expected_entry_cost_bps,
    forward_metrics,
    liquidity_notional,
    summarize_buckets,
    timestamp_key,
)


def test_forward_metrics_uses_directional_returns_and_short_horizons() -> None:
    start = datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc)
    points = [
        SnapshotPoint(start, price=100.0, spread_bps=1.0),
        SnapshotPoint(start.replace(minute=5), price=102.0, spread_bps=1.0),
        SnapshotPoint(start.replace(minute=15), price=98.0, spread_bps=1.0),
    ]

    metrics = forward_metrics(points, start, side="long", entry_price=100.0)

    assert metrics["future_return_1m_bps"] == 200.0
    assert metrics["future_return_5m_bps"] == 200.0
    assert metrics["future_return_15m_bps"] == -200.0
    assert metrics["adverse_return_15m_bps"] == 200.0
    assert metrics["mfe_15m_bps"] == 200.0
    assert metrics["mae_15m_bps"] == -200.0


def test_liquidity_notional_uses_touched_side() -> None:
    snapshot = {
        "price": 100.0,
        "best_bid": 99.9,
        "best_ask": 100.1,
        "best_bid_size": 3.0,
        "best_ask_size": 2.0,
        "bid_depth_10bps": 30.0,
        "ask_depth_10bps": 20.0,
    }

    long_touch, long_depth = liquidity_notional(snapshot, side="long")
    short_touch, short_depth = liquidity_notional(snapshot, side="short")

    assert long_touch == 200.2
    assert long_depth == 2000.0
    assert short_touch == 299.70000000000005
    assert short_depth == 3000.0


def test_expected_entry_cost_uses_half_spread_model() -> None:
    assert expected_entry_cost_bps(
        spread_bps=4.0,
        spread_multiplier=0.5,
        slippage_bps=0.5,
    ) == 2.5


def test_timestamp_key_normalizes_z_and_offset_formats() -> None:
    assert timestamp_key("2026-06-22T10:00:00Z") == timestamp_key(
        "2026-06-22T10:00:00+00:00"
    )


def test_summarize_buckets_counts_status_depth_and_pnl() -> None:
    rows = [
        _row("opened", pnl=1.5, depth_ratio=12.0, future_15m=20.0),
        _row("opened", pnl=-2.0, depth_ratio=0.7, future_15m=-15.0),
        _row("accepted_skipped", pnl=None, depth_ratio=0.7, future_15m=12.0),
        _row("risk_rejected", pnl=None, depth_ratio=None, future_15m=-5.0),
    ]

    buckets = summarize_buckets(rows)
    by_key = {(row.bucket_type, row.bucket): row for row in buckets}

    opened = by_key[("status", "opened")]
    assert opened.decisions == 2
    assert opened.opened == 2
    assert opened.closed_trades == 2
    assert opened.closed_pnl_usd == -0.5
    assert opened.win_rate == 0.5
    assert opened.avg_future_return_15m_bps == 2.5

    shallow = by_key[("depth_ratio", "lt_1x")]
    assert shallow.decisions == 2
    assert shallow.opened == 1
    assert shallow.accepted_skipped == 1


def _row(
    status: str,
    *,
    pnl: float | None,
    depth_ratio: float | None,
    future_15m: float,
) -> FillQualityRow:
    return FillQualityRow(
        timestamp="2026-06-22T10:00:00Z",
        symbol="ETH",
        side="long",
        setup="trend_pullback_long",
        status=status,
        reason=status,
        risk_accepted=status != "risk_rejected",
        opened=status == "opened",
        skipped_open=status == "accepted_skipped",
        target_notional_usd=200.0,
        entry_mid_price=100.0,
        fill_price=100.02 if status == "opened" else None,
        spread_bps=2.0,
        expected_entry_cost_bps=1.5,
        expected_round_trip_cost_bps=10.0,
        expected_entry_cost_usd=0.03,
        bucket_notional_usd=10000.0,
        touch_notional_usd=500.0,
        depth_10bps_usd=None if depth_ratio is None else depth_ratio * 200.0,
        depth_to_order_ratio=depth_ratio,
        touch_to_order_ratio=2.5,
        book_imbalance=0.1,
        trade_flow_bias=0.2,
        microprice_dislocation_bps=0.3,
        asset_ctx_observation_age_seconds=None,
        external_reference_age_seconds=None,
        future_return_1m_bps=future_15m / 2.0,
        future_return_5m_bps=future_15m / 3.0,
        future_return_15m_bps=future_15m,
        adverse_return_1m_bps=max(-future_15m / 2.0, 0.0),
        adverse_return_5m_bps=max(-future_15m / 3.0, 0.0),
        adverse_return_15m_bps=max(-future_15m, 0.0),
        mfe_15m_bps=max(future_15m, 0.0),
        mae_15m_bps=min(future_15m, 0.0),
        closed_trade_pnl_usd=pnl,
        close_reason="time_stop" if pnl is not None else None,
        hold_hours=1.0 if pnl is not None else None,
    )
