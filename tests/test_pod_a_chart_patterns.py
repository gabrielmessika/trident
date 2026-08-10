from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from app.settings import load_config
from app.trident.pod_a.candles import Candle
from app.trident.pod_a.chart_patterns import (
    detect_double_bottoms,
    detect_triangle_breakouts,
)
from app.trident.pod_a.planner import AnchorTrendPlanner
from app.trident.pod_a.service import AnchorTrendService
from app.trident.pod_a.signals import AnchorTrendContext, AnchorTrendSignal
from app.trident.types import PodAllocation, PodName, SymbolAllocation


def _candle(index: int, open_: float, high: float, low: float, close: float) -> Candle:
    return Candle(
        opened_at=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(hours=4 * index),
        open=open_,
        high=high,
        low=low,
        close=close,
    )


class PodAChartPatternTests(unittest.TestCase):
    def test_double_bottom_detector_matches_valid_breakout(self) -> None:
        candles: list[Candle] = []
        for index in range(24):
            low = 95.0
            high = 98.0
            close = 96.0
            if index == 4:
                low = 90.0
                high = 96.0
                close = 92.0
            if index == 9:
                high = 100.0
                close = 98.0
            if index == 14:
                low = 91.0
                high = 97.0
                close = 92.0
            if index == 18:
                high = 100.0
                close = 98.0
            if index == 23:
                high = 99.0
                close = 99.0
            candles.append(_candle(index, close, high, low, close))
        candles.append(_candle(24, 99.0, 102.0, 98.0, 101.0))

        rows = detect_double_bottoms(candles)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].pattern, "double_bottom")
        self.assertGreater(rows[0].score, 2.0)

    def test_triangle_detector_matches_valid_breakout(self) -> None:
        candles: list[Candle] = []
        for index in range(24):
            high = 110.0 - index * 0.35
            low = 90.0 + index * 0.30
            close = (high + low) / 2.0
            candles.append(_candle(index, close, high, low, close))
        candles[-1].close = 100.0
        candles.append(_candle(24, 100.0, 105.0, 99.0, 103.0))

        rows = detect_triangle_breakouts(candles)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].pattern, "triangle_breakout")
        self.assertGreater(rows[0].compression_pct or 0.0, 18.0)

    def test_service_emits_chart_signal_only_on_first_4h_snapshot(self) -> None:
        config = load_config("config/trident.toml")
        config = replace(
            config,
            pod_a=replace(
                config.pod_a,
                allowed_setups=["chart_double_bottom_long", "chart_triangle_breakout_long"],
            ),
        )
        service = AnchorTrendService(config)
        context = self._chart_context(current_4h_sample_count=1)

        signal = service.evaluate(context)

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.setup, "chart_double_bottom_long")
        self.assertEqual(signal.setup_details["chart_pattern"], "double_bottom")
        self.assertEqual(signal.setup_details["chart_stop_bps"], 600.0)

        repeated_context = self._chart_context(current_4h_sample_count=2)
        repeated_signal = service.evaluate(repeated_context)
        self.assertFalse(
            repeated_signal is not None and repeated_signal.setup.startswith("chart_")
        )

    def test_planner_uses_chart_target_stop_and_time_stop(self) -> None:
        config = load_config("config/trident.toml")
        planner = AnchorTrendPlanner(config)
        signal = AnchorTrendSignal(
            symbol="ETH",
            side="long",
            setup="chart_double_bottom_long",
            confidence=0.60,
            entry_price=100.0,
            invalidation_price=94.0,
            setup_details={
                "chart_stop_bps": 600.0,
                "chart_take_profit_bps": 350.0,
                "chart_time_stop_hours": 336.0,
            },
        )
        allocation = PodAllocation(
            pod=PodName.POD_A,
            target_pct=0.2,
            target_usd=250.0,
            symbols=[SymbolAllocation(symbol="ETH", target_pct=0.2, target_usd=250.0)],
        )

        plan = planner.build_trade_plan(signal, allocation)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.stop_bps, 600.0)
        self.assertEqual(plan.take_profit_bps, 350.0)
        self.assertEqual(plan.break_even_trigger_bps, 0.0)
        self.assertEqual(plan.trailing_activation_bps, 0.0)
        self.assertEqual(plan.time_stop_hours, 336)

    def test_config_promotes_two_chart_profiles(self) -> None:
        config = load_config("config/trident.toml")

        self.assertTrue(config.pod_a.chart_patterns.enabled)
        self.assertEqual(config.pod_a.chart_patterns.max_open_positions, 1)
        self.assertIn("chart_double_bottom_long", config.pod_a.allowed_setups)
        self.assertIn("chart_triangle_breakout_long", config.pod_a.allowed_setups)
        self.assertEqual(len(config.pod_a.chart_patterns.profiles), 2)

    def _chart_context(self, *, current_4h_sample_count: int) -> AnchorTrendContext:
        return AnchorTrendContext(
            symbol="ETH",
            regime="TrendExpansion",
            price=100.0,
            ema_fast=99.0,
            ema_slow=98.0,
            vwap_distance_bps=2.0,
            structure_score=0.55,
            funding_rate=0.0,
            spread_bps=1.0,
            btc_aligned=True,
            volume_ratio=1.5,
            current_4h_sample_count=current_4h_sample_count,
            chart_pattern_name="double_bottom",
            chart_pattern_validation_time="2026-01-01T00:00:00Z",
            chart_pattern_theoretical_target_bps=400.0,
            chart_pattern_structure_height_pct=4.0,
            chart_pattern_structure_depth_pct=4.0,
            chart_pattern_breakout_margin_pct=0.25,
            chart_pattern_low_mismatch_pct=1.0,
            chart_pattern_bars=24,
            chart_pattern_score=2.30,
        )


if __name__ == "__main__":
    unittest.main()
