import unittest
from dataclasses import replace

from app.backtest.pod_b_pattern_experiment import ExperimentalBreakoutService, summarize_backtest
from app.settings import load_config
from app.trident.pod_b.signals import BreakoutContext


class PodBPatternExperimentTests(unittest.TestCase):
    def _baseline_config(self):
        config = load_config("config/trident.toml")
        return replace(
            config,
            pod_b=replace(config.pod_b, bis_strict_continuation_filter_enabled=False),
        )

    def test_confidence_boost_increases_matching_signal_confidence(self) -> None:
        config = self._baseline_config()
        context = BreakoutContext(
            symbol="BTC",
            regime="TrendExpansion",
            price=100.0,
            ema_fast=100.8,
            ema_slow=99.9,
            vwap_distance_bps=9.0,
            structure_score=0.42,
            funding_rate=0.0,
            spread_bps=1.1,
            btc_aligned=True,
            market_cluster="crypto",
            cluster_leader="BTC",
            book_imbalance=0.32,
            trade_flow_bias=0.28,
            bucket_trade_count=24,
            bucket_notional_usd=800.0,
            bucket_range_bps=42.0,
            delta_book_imbalance=0.22,
            delta_trade_flow_bias=0.30,
            volume_ratio=2.4,
            trade_count_ratio=1.9,
            realized_vol_short_bps=7.0,
            realized_vol_long_bps=4.0,
            compression_score=0.70,
            microprice_dislocation_bps=1.4,
        )

        baseline = ExperimentalBreakoutService(config, scenario="confidence_boost")
        base_only = ExperimentalBreakoutService(config, scenario="baseline")
        original = base_only.evaluate(context)
        boosted = baseline.evaluate(context)

        self.assertIsNotNone(original)
        self.assertIsNotNone(boosted)
        assert original is not None
        assert boosted is not None
        self.assertEqual(boosted.setup, "vol_expansion_long")
        self.assertGreater(boosted.confidence, original.confidence)

    def test_expansion_continuation_can_emit_signal_below_base_vol_threshold(self) -> None:
        config = self._baseline_config()
        context = BreakoutContext(
            symbol="ETH",
            regime="TrendExpansion",
            price=100.0,
            ema_fast=100.8,
            ema_slow=100.0,
            vwap_distance_bps=6.0,
            structure_score=0.35,
            funding_rate=0.0,
            spread_bps=1.8,
            btc_aligned=True,
            market_cluster="crypto",
            cluster_leader="BTC",
            book_imbalance=0.08,
            trade_flow_bias=0.12,
            bucket_trade_count=8,
            bucket_notional_usd=400.0,
            bucket_range_bps=45.0,
            delta_book_imbalance=0.06,
            delta_trade_flow_bias=0.16,
            volume_ratio=2.0,
            trade_count_ratio=1.8,
            realized_vol_short_bps=5.5,
            realized_vol_long_bps=3.6,
            compression_score=0.20,
            microprice_dislocation_bps=0.4,
        )

        baseline_service = ExperimentalBreakoutService(config, scenario="baseline")
        variant_service = ExperimentalBreakoutService(config, scenario="expansion_continuation")

        self.assertIsNone(baseline_service.evaluate(context))
        signal = variant_service.evaluate(context)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.setup, "expansion_continuation_long")
        self.assertGreaterEqual(signal.confidence, config.pod_b.bis_min_confidence)

    def test_continuation_filter_blocks_base_signal_without_pattern(self) -> None:
        config = self._baseline_config()
        context = BreakoutContext(
            symbol="BTC",
            regime="TrendExpansion",
            price=100.0,
            ema_fast=100.8,
            ema_slow=99.9,
            vwap_distance_bps=9.0,
            structure_score=0.42,
            funding_rate=0.0,
            spread_bps=1.1,
            btc_aligned=True,
            market_cluster="crypto",
            cluster_leader="BTC",
            book_imbalance=0.32,
            trade_flow_bias=0.28,
            bucket_trade_count=24,
            bucket_notional_usd=800.0,
            bucket_range_bps=15.0,
            delta_book_imbalance=0.22,
            delta_trade_flow_bias=0.30,
            volume_ratio=2.4,
            trade_count_ratio=1.9,
            realized_vol_short_bps=7.0,
            realized_vol_long_bps=4.0,
            compression_score=0.70,
            microprice_dislocation_bps=1.4,
        )

        baseline_service = ExperimentalBreakoutService(config, scenario="baseline")
        filter_service = ExperimentalBreakoutService(config, scenario="continuation_filter")

        self.assertIsNotNone(baseline_service.evaluate(context))
        self.assertIsNone(filter_service.evaluate(context))

    def test_strict_continuation_filter_requires_higher_quality_pattern(self) -> None:
        config = self._baseline_config()
        soft_match_context = BreakoutContext(
            symbol="BTC",
            regime="TrendExpansion",
            price=100.0,
            ema_fast=100.8,
            ema_slow=99.9,
            vwap_distance_bps=9.0,
            structure_score=0.42,
            funding_rate=0.0,
            spread_bps=1.1,
            btc_aligned=True,
            market_cluster="crypto",
            cluster_leader="BTC",
            book_imbalance=0.32,
            trade_flow_bias=0.28,
            bucket_trade_count=24,
            bucket_notional_usd=800.0,
            bucket_range_bps=28.0,
            delta_book_imbalance=0.22,
            delta_trade_flow_bias=0.30,
            volume_ratio=2.4,
            trade_count_ratio=1.9,
            realized_vol_short_bps=7.0,
            realized_vol_long_bps=4.0,
            compression_score=0.70,
            microprice_dislocation_bps=1.4,
        )
        strong_match_context = BreakoutContext(
            symbol="BTC",
            regime="TrendExpansion",
            price=100.0,
            ema_fast=100.8,
            ema_slow=99.9,
            vwap_distance_bps=9.0,
            structure_score=0.42,
            funding_rate=0.0,
            spread_bps=1.1,
            btc_aligned=True,
            market_cluster="crypto",
            cluster_leader="BTC",
            book_imbalance=0.32,
            trade_flow_bias=0.28,
            bucket_trade_count=24,
            bucket_notional_usd=800.0,
            bucket_range_bps=34.0,
            delta_book_imbalance=0.22,
            delta_trade_flow_bias=0.30,
            volume_ratio=2.4,
            trade_count_ratio=1.9,
            realized_vol_short_bps=7.0,
            realized_vol_long_bps=4.0,
            compression_score=0.70,
            microprice_dislocation_bps=1.4,
        )

        continuation_filter = ExperimentalBreakoutService(config, scenario="continuation_filter")
        strict_filter = ExperimentalBreakoutService(config, scenario="strict_continuation_filter")

        self.assertIsNotNone(continuation_filter.evaluate(soft_match_context))
        self.assertIsNone(strict_filter.evaluate(soft_match_context))
        self.assertIsNotNone(strict_filter.evaluate(strong_match_context))

    def test_summarize_backtest_reports_win_rate(self) -> None:
        summary = summarize_backtest(
            {
                "signal_count": 10,
                "accepted_count": 8,
                "opened_count": 6,
                "closed_trade_count": 5,
                "win_count": 3,
                "loss_count": 2,
                "realized_pnl_usd": 12.3456,
                "gross_pnl_usd": 15.0,
                "fees_usd": 2.6544,
                "max_drawdown_usd": 5.4,
                "average_hold_hours": 0.75,
                "average_confidence": 0.66,
                "signals_by_setup": {"vol_expansion_long": 10},
                "trades_by_setup": {"vol_expansion_long": 5},
                "pnl_by_setup": {"vol_expansion_long": 12.3456},
                "pnl_by_date": {"2026-04-13": 12.3456},
            }
        )

        self.assertEqual(summary["win_rate"], 0.6)
        self.assertEqual(summary["realized_pnl_usd"], 12.3456)
        self.assertEqual(summary["trades_by_setup"]["vol_expansion_long"], 5)


if __name__ == "__main__":
    unittest.main()
