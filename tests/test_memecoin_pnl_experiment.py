import unittest

from app.backtest.memecoin_pnl_experiment import ExperimentalMemecoinService, OFFICIAL_SCENARIOS
from app.settings import load_config
from app.trident.pod_b.signals import BreakoutContext


class MemecoinPnlExperimentTests(unittest.TestCase):
    def _config(self):
        return load_config("config/trident.toml")

    def test_flow_following_emits_signal_for_ranked_context(self) -> None:
        config = self._config()
        scenario = next(item for item in OFFICIAL_SCENARIOS if item.name == "memecoin_flow_only")
        service = ExperimentalMemecoinService(
            config,
            trigger_specs=scenario.trigger_specs,
            label=scenario.name,
        )
        context = BreakoutContext(
            symbol="DOGE",
            regime="TrendExpansion",
            price=100.0,
            ema_fast=101.0,
            ema_slow=100.1,
            vwap_distance_bps=6.5,
            structure_score=0.25,
            funding_rate=0.0,
            spread_bps=1.5,
            btc_aligned=True,
            price_move_bps=1.2,
            market_cluster="crypto",
            cluster_leader="BTC",
            book_imbalance=0.18,
            trade_flow_bias=0.24,
            bucket_trade_count=20,
            bucket_notional_usd=1400.0,
            bucket_range_bps=22.0,
            delta_spread_bps=0.9,
            delta_book_imbalance=0.20,
            delta_trade_flow_bias=0.30,
            volume_ratio=3.4,
            trade_count_ratio=3.0,
            realized_vol_short_bps=6.0,
            realized_vol_long_bps=2.2,
            compression_score=0.35,
            best_bid_size=120.0,
            best_ask_size=95.0,
            bid_depth_10bps=180.0,
            ask_depth_10bps=110.0,
            bid_depth_velocity=0.42,
            ask_depth_velocity=-0.35,
            best_bid_size_velocity=0.28,
            best_ask_size_velocity=-0.22,
            microprice_dislocation_bps=1.3,
        )

        signals = service.evaluate_many([context])
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].setup, "memecoin_flow_following_long")
        self.assertGreaterEqual(signals[0].confidence, config.pod_b.bis_min_confidence)

    def test_top_n_filter_keeps_only_highest_ranked_event_candidate(self) -> None:
        config = self._config()
        service = ExperimentalMemecoinService(
            config,
            trigger_specs=(
                OFFICIAL_SCENARIOS[1].trigger_specs[0],
            ),
            label="event_only_test",
        )
        leader = BreakoutContext(
            symbol="DOGE",
            regime="TrendExpansion",
            price=100.0,
            ema_fast=101.2,
            ema_slow=100.0,
            vwap_distance_bps=7.0,
            structure_score=0.30,
            funding_rate=0.0,
            spread_bps=1.8,
            btc_aligned=True,
            price_move_bps=1.5,
            market_cluster="crypto",
            cluster_leader="BTC",
            book_imbalance=0.16,
            trade_flow_bias=0.20,
            bucket_trade_count=24,
            bucket_notional_usd=1600.0,
            bucket_range_bps=28.0,
            delta_spread_bps=1.4,
            delta_book_imbalance=0.26,
            delta_trade_flow_bias=0.40,
            volume_ratio=3.6,
            trade_count_ratio=3.2,
            realized_vol_short_bps=6.5,
            realized_vol_long_bps=3.0,
            compression_score=0.30,
            best_bid_size=120.0,
            best_ask_size=88.0,
            bid_depth_10bps=185.0,
            ask_depth_10bps=100.0,
            bid_depth_velocity=0.78,
            ask_depth_velocity=-0.70,
            best_bid_size_velocity=0.52,
            best_ask_size_velocity=-0.44,
            microprice_dislocation_bps=1.4,
        )
        follower = BreakoutContext(
            symbol="XRP",
            regime="TrendExpansion",
            price=100.0,
            ema_fast=100.8,
            ema_slow=100.0,
            vwap_distance_bps=6.5,
            structure_score=0.25,
            funding_rate=0.0,
            spread_bps=1.8,
            btc_aligned=True,
            price_move_bps=1.1,
            market_cluster="crypto",
            cluster_leader="BTC",
            book_imbalance=0.12,
            trade_flow_bias=0.16,
            bucket_trade_count=13,
            bucket_notional_usd=220.0,
            bucket_range_bps=24.0,
            delta_spread_bps=0.45,
            delta_book_imbalance=0.08,
            delta_trade_flow_bias=0.18,
            volume_ratio=2.6,
            trade_count_ratio=2.2,
            realized_vol_short_bps=4.8,
            realized_vol_long_bps=2.5,
            compression_score=0.28,
            best_bid_size=100.0,
            best_ask_size=90.0,
            bid_depth_10bps=150.0,
            ask_depth_10bps=104.0,
            bid_depth_velocity=0.20,
            ask_depth_velocity=-0.18,
            best_bid_size_velocity=0.12,
            best_ask_size_velocity=-0.10,
            microprice_dislocation_bps=1.0,
        )

        service._trigger_specs = (
            type(service._trigger_specs[0])(
                name="event_top1",
                trigger_kind="event_momentum",
                description="top 1 only",
                top_n=1,
                min_interest_score=0.55,
                max_spread_bps=5.0,
                min_bucket_notional_usd=100.0,
                allowed_regimes=("TrendExpansion", "PanicSqueeze"),
            ),
        )
        signals = service.evaluate_many([leader, follower])
        self.assertEqual([signal.symbol for signal in signals], ["DOGE"])

    def test_hybrid_keeps_base_signal_when_memecoin_filter_does_not_match(self) -> None:
        config = self._config()
        scenario = next(item for item in OFFICIAL_SCENARIOS if item.name == "hybrid_breakout_plus_combo")
        service = ExperimentalMemecoinService(
            config,
            trigger_specs=scenario.trigger_specs,
            include_base=True,
            label=scenario.name,
        )
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
            price_move_bps=0.0,
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

        signal = service.evaluate(context)
        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertIn(signal.setup, {"compression_breakout_long", "vol_expansion_long", "ttm_squeeze_release_long"})


if __name__ == "__main__":
    unittest.main()
