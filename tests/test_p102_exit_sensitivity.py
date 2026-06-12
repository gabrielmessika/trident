import unittest
from datetime import datetime, timezone

from scripts.run_p102_exit_sensitivity import (
    SnapshotPoint,
    TradeSpec,
    VariantSpec,
    run_sensitivity,
    simulate_trade,
)


class P102ExitSensitivityTests(unittest.TestCase):
    def test_cat_stop_max_changes_grace_exit(self) -> None:
        trade = _trade()
        points = [
            _point("2026-06-10T10:05:00Z", 98.2, structure_score=0.5, vwap_distance_bps=0.0),
            _point("2026-06-10T11:05:00Z", 101.0, structure_score=0.5, vwap_distance_bps=0.0),
        ]

        tight = simulate_trade(
            trade,
            points,
            VariantSpec(grace_minutes=60, cat_stop_max_bps=160, early_failure_enabled=False),
            taker_fee_bps=0.0,
            dry_run_slippage_bps=0.0,
            dry_run_spread_multiplier=0.0,
        )
        loose = simulate_trade(
            trade,
            points,
            VariantSpec(grace_minutes=60, cat_stop_max_bps=300, early_failure_enabled=False),
            taker_fee_bps=0.0,
            dry_run_slippage_bps=0.0,
            dry_run_spread_multiplier=0.0,
        )

        self.assertEqual(tight.close_reason, "catastrophic_stop")
        self.assertLess(tight.pnl_usd, 0)
        self.assertEqual(loose.close_reason, "end_of_window")
        self.assertGreater(loose.pnl_usd, 0)

    def test_early_failure_can_close_before_recovery(self) -> None:
        trade = _trade()
        points = [
            _point("2026-06-10T10:15:00Z", 99.2, structure_score=0.1, vwap_distance_bps=-10.0),
            _point("2026-06-10T11:05:00Z", 101.0, structure_score=0.5, vwap_distance_bps=0.0),
        ]

        efe_on = simulate_trade(
            trade,
            points,
            VariantSpec(grace_minutes=60, cat_stop_max_bps=300, early_failure_enabled=True),
            taker_fee_bps=0.0,
            dry_run_slippage_bps=0.0,
            dry_run_spread_multiplier=0.0,
        )
        efe_off = simulate_trade(
            trade,
            points,
            VariantSpec(grace_minutes=60, cat_stop_max_bps=300, early_failure_enabled=False),
            taker_fee_bps=0.0,
            dry_run_slippage_bps=0.0,
            dry_run_spread_multiplier=0.0,
        )

        self.assertEqual(efe_on.close_reason, "early_failure_exit")
        self.assertLess(efe_on.pnl_usd, 0)
        self.assertEqual(efe_off.close_reason, "end_of_window")
        self.assertGreater(efe_off.pnl_usd, 0)

    def test_run_sensitivity_summarizes_variants(self) -> None:
        trade = _trade()
        variants = [
            VariantSpec(grace_minutes=60, cat_stop_max_bps=160, early_failure_enabled=False),
            VariantSpec(grace_minutes=60, cat_stop_max_bps=300, early_failure_enabled=False),
        ]

        summaries, rows = run_sensitivity(
            trades=[trade],
            snapshot_index={
                "BTC": [
                    _point("2026-06-10T10:05:00Z", 98.2),
                    _point("2026-06-10T11:05:00Z", 101.0),
                ]
            },
            variants=variants,
            taker_fee_bps=0.0,
            dry_run_slippage_bps=0.0,
            dry_run_spread_multiplier=0.0,
        )

        self.assertEqual(len(rows), 2)
        self.assertEqual(len(summaries), 2)
        self.assertGreater(summaries[0].pnl_usd, summaries[1].pnl_usd)


def _trade() -> TradeSpec:
    return TradeSpec(
        trade_id="BTC|long|test",
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
        original_closed_at=datetime(2026, 6, 10, 11, 0, tzinfo=timezone.utc),
        original_pnl_usd=0.0,
        original_close_reason="test",
        setup_details={"market_cluster": "crypto"},
    )


def _point(
    timestamp: str,
    price: float,
    *,
    structure_score: float = 0.5,
    vwap_distance_bps: float = 0.0,
) -> SnapshotPoint:
    normalized = timestamp.replace("Z", "+00:00")
    return SnapshotPoint(
        timestamp=datetime.fromisoformat(normalized),
        price=price,
        spread_bps=0.0,
        structure_score=structure_score,
        vwap_distance_bps=vwap_distance_bps,
        btc_aligned=True,
    )


if __name__ == "__main__":
    unittest.main()
