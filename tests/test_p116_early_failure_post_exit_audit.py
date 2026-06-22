from datetime import datetime, timezone

from app.settings import load_config
from scripts.run_p102_exit_sensitivity import SnapshotPoint, TradeSpec
from scripts.run_p116_early_failure_post_exit_audit import (
    audit_early_failure_trades,
    classify_path,
    post_exit_mfe_mae_bps,
    summarize_rows,
)


def test_classify_path_marks_missed_winner_and_loss_avoided() -> None:
    assert classify_path(-1.0, 1.5) == "missed_winner"
    assert classify_path(-2.0, -0.5) == "missed_loss_reduction"
    assert classify_path(-1.0, -3.0) == "loss_avoided_by_efe"
    assert classify_path(-1.0, -1.0) == "neutral"


def test_post_exit_mfe_mae_tracks_path_after_original_close() -> None:
    trade = _trade(original_closed_at="2026-06-10T10:15:00Z")
    points = [
        _point("2026-06-10T10:05:00Z", 99.0),
        _point("2026-06-10T10:20:00Z", 101.5),
        _point("2026-06-10T10:25:00Z", 98.5),
        _point("2026-06-10T10:40:00Z", 102.0),
    ]

    mfe, mae = post_exit_mfe_mae_bps(
        trade,
        points,
        natural_closed_at="2026-06-10T10:25:00+00:00",
    )

    assert mfe == 150.0
    assert mae == 150.0


def test_audit_early_failure_trades_summarizes_missed_recovery() -> None:
    config = load_config("config/trident.toml")
    trade = _trade()
    rows = audit_early_failure_trades(
        trades=[trade],
        snapshot_index={
            "BTC": [
                _point("2026-06-10T10:05:00Z", 99.2, structure_score=0.1, vwap_distance_bps=-10.0),
                _point("2026-06-10T11:05:00Z", 101.0, structure_score=0.5, vwap_distance_bps=0.0),
            ]
        },
        config=config,
        taker_fee_bps=0.0,
        dry_run_slippage_bps=0.0,
        dry_run_spread_multiplier=0.0,
    )
    summary = summarize_rows(rows, replay_report="fixture.json", snapshot_input="fixture")

    assert rows[0].classification == "missed_winner"
    assert rows[0].natural_close_reason == "end_of_window"
    assert rows[0].natural_pnl_usd > rows[0].original_pnl_usd
    assert summary.missed_winner_count == 1
    assert summary.missed_loss_reduction_count == 0
    assert summary.missed_recovery_usd > 0.0


def _trade(
    *,
    original_closed_at: str = "2026-06-10T10:15:00Z",
) -> TradeSpec:
    return TradeSpec(
        trade_id="BTC|long|efe",
        symbol="BTC",
        side="long",
        setup="trend_pullback_long",
        confidence=0.7,
        entry_price=100.0,
        target_notional_usd=1000.0,
        stop_bps=100.0,
        time_stop_hours=24,
        take_profit_bps=0.0,
        break_even_trigger_bps=0.0,
        trailing_activation_bps=0.0,
        trailing_distance_bps=0.0,
        opened_at=datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc),
        original_closed_at=datetime.fromisoformat(original_closed_at.replace("Z", "+00:00")),
        original_pnl_usd=-1.0,
        original_close_reason="early_failure_exit",
        setup_details={"market_cluster": "crypto"},
    )


def _point(
    timestamp: str,
    price: float,
    *,
    structure_score: float = 0.5,
    vwap_distance_bps: float = 0.0,
) -> SnapshotPoint:
    return SnapshotPoint(
        timestamp=datetime.fromisoformat(timestamp.replace("Z", "+00:00")),
        price=price,
        spread_bps=0.0,
        structure_score=structure_score,
        vwap_distance_bps=vwap_distance_bps,
        btc_aligned=True,
    )
