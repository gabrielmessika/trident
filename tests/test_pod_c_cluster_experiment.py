import unittest

from app.backtest.pod_c_cluster_experiment import ClusterAwareTradfiService, summarize_backtest
from app.settings import load_config
from app.trident.pod_c.signals import TradfiTrendContext


class PodCClusterExperimentTests(unittest.TestCase):
    def _context(self, **overrides):
        base = dict(
            symbol="XYZ:CL",
            regime="TrendExpansion",
            price=70.0,
            ema_fast=70.35,
            ema_slow=70.10,
            vwap_distance_bps=-2.0,
            spread_bps=1.5,
            funding_rate=0.0,
            structure_score=0.45,
            book_imbalance=0.38,
            trade_flow_bias=0.42,
            bucket_range_bps=22.0,
            bucket_trade_count=8,
            bucket_volume=100.0,
            bucket_notional_usd=7000.0,
            activity_ratio=1.4,
            trade_count_ratio=1.2,
            trend_bps=24.0,
            btc_aligned=True,
            market_cluster="oil",
            cluster_aligned=True,
            cluster_leader="XYZ:CL",
        )
        base.update(overrides)
        return TradfiTrendContext(**base)

    def test_oil_only_accepts_oil_pullback_long(self) -> None:
        config = load_config("config/trident.toml")
        service = ClusterAwareTradfiService(config.pod_c, scenario="oil_only_v1")

        signal = service.evaluate(self._context())

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.setup, "tradfi_continuation_long")

    def test_oil_only_rejects_non_oil_clusters(self) -> None:
        config = load_config("config/trident.toml")
        service = ClusterAwareTradfiService(config.pod_c, scenario="oil_only_v1")

        signal = service.evaluate(
            self._context(
                symbol="XYZ:SILVER",
                market_cluster="silver",
                cluster_leader="XYZ:SILVER",
                vwap_distance_bps=2.0,
            )
        )

        self.assertIsNone(signal)

    def test_oil_silver_index_accepts_silver_breakout_long(self) -> None:
        config = load_config("config/trident.toml")
        service = ClusterAwareTradfiService(config.pod_c, scenario="oil_silver_index_v1")

        signal = service.evaluate(
            self._context(
                symbol="XYZ:SILVER",
                market_cluster="silver",
                cluster_leader="XYZ:SILVER",
                price=32.0,
                ema_fast=32.16,
                ema_slow=32.03,
                trend_bps=25.0,
                structure_score=0.45,
                vwap_distance_bps=2.0,
                spread_bps=1.2,
                book_imbalance=0.34,
                trade_flow_bias=0.40,
                bucket_range_bps=21.0,
            )
        )

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.setup, "tradfi_continuation_long")

    def test_summarize_backtest_keeps_symbol_breakdown(self) -> None:
        summary = summarize_backtest(
            {
                "signal_count": 12,
                "accepted_count": 7,
                "closed_trade_count": 5,
                "win_count": 3,
                "realized_pnl_usd": 4.25,
                "gross_pnl_usd": 6.0,
                "fees_usd": 1.75,
                "max_drawdown_usd": 2.1,
                "pnl_by_symbol": {"XYZ:CL": 3.0},
                "trades_by_symbol": {"XYZ:CL": 2},
                "pnl_by_setup": {"tradfi_continuation_long": 4.25},
            }
        )

        self.assertEqual(summary["win_rate"], 0.6)
        self.assertEqual(summary["pnl_by_symbol"]["XYZ:CL"], 3.0)


if __name__ == "__main__":
    unittest.main()
