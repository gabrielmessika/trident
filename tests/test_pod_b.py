import unittest
import json
import tempfile
from dataclasses import replace
from pathlib import Path

from app.backtest.pod_b_runner import PodBBacktestRunner
from app.settings import load_config
from app.trident.pod_b import BreakoutContext, BreakoutPlanner, BreakoutService, PodBRiskGate
from app.trident.types import PodAllocation, PodName, SymbolAllocation


class PodBTests(unittest.TestCase):
    def test_service_planner_and_risk_gate_produce_directional_plan(self) -> None:
        config = load_config("config/trident.toml")
        service = BreakoutService(config)
        planner = BreakoutPlanner(config)
        risk_gate = PodBRiskGate(config)

        signal = service.evaluate(
            BreakoutContext(
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
        )

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, "long")
        allocation = PodAllocation(
            pod=PodName.POD_B,
            target_pct=0.10,
            target_usd=100.0,
            symbols=[SymbolAllocation(symbol="BTC", target_pct=0.10, target_usd=100.0)],
        )
        plan = planner.build_trade_plan(signal, allocation)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertGreater(plan.target_notional_usd, 0.0)
        self.assertGreater(plan.effective_leverage, 1.0)
        self.assertEqual(plan.risk_budget_usd, 7.5)

        decisions = risk_gate.evaluate_many([plan])
        self.assertEqual(len(decisions), 1)
        self.assertTrue(decisions[0].accepted)

    def test_service_rejects_dead_zone_even_with_good_microstructure(self) -> None:
        config = load_config("config/trident.toml")
        service = BreakoutService(config)

        signal = service.evaluate(
            BreakoutContext(
                symbol="BTC",
                regime="DeadZone",
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
        )

        self.assertIsNone(signal)

    def test_service_blocks_signal_when_strict_continuation_filter_fails(self) -> None:
        config = load_config("config/trident.toml")
        disabled_filter_config = replace(
            config,
            pod_b=replace(config.pod_b, bis_strict_continuation_filter_enabled=False),
        )
        baseline_service = BreakoutService(disabled_filter_config)
        filtered_service = BreakoutService(config)

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

        self.assertIsNotNone(baseline_service.evaluate(context))
        self.assertIsNone(filtered_service.evaluate(context))

    def test_runner_replays_strategy_on_routed_symbol_universe(self) -> None:
        config = load_config("config/trident.toml")
        config.hyperliquid.observation_universe = ["BTC"]

        records = [
            {
                "timestamp": "2026-04-05T10:00:00Z",
                "regime_snapshot": {
                    "ready": True,
                    "adx": 28.0,
                    "atr_ratio": 1.0,
                    "range_width_bps": 120.0,
                    "structure_score": 0.50,
                    "btc_impulse": True,
                },
                "symbols": [
                    {
                        "symbol": "BTC",
                        "price": 100.0,
                        "ema_fast": 100.8,
                        "ema_slow": 99.9,
                        "vwap_distance_bps": 9.0,
                        "structure_score": 0.42,
                        "funding_rate": 0.0,
                        "spread_bps": 1.1,
                        "btc_aligned": True,
                        "market_cluster": "crypto",
                        "cluster_aligned": True,
                        "cluster_leader": "BTC",
                        "book_imbalance": 0.32,
                        "trade_flow_bias": 0.28,
                        "bucket_volume": 8.0,
                        "bucket_notional_usd": 800.0,
                        "bucket_trade_count": 24,
                        "bucket_range_bps": 34.0,
                        "delta_book_imbalance": 0.22,
                        "delta_trade_flow_bias": 0.30,
                        "volume_ratio": 2.4,
                        "trade_count_ratio": 1.9,
                        "realized_vol_short_bps": 7.0,
                        "realized_vol_long_bps": 4.0,
                        "compression_score": 0.70,
                        "microprice_dislocation_bps": 1.4,
                        "source": "test",
                    }
                ],
            },
            {
                "timestamp": "2026-04-05T10:01:00Z",
                "regime_snapshot": {
                    "ready": True,
                    "adx": 30.0,
                    "atr_ratio": 1.1,
                    "range_width_bps": 140.0,
                    "structure_score": 0.54,
                    "btc_impulse": True,
                },
                "symbols": [
                    {
                        "symbol": "BTC",
                        "price": 101.0,
                        "ema_fast": 101.2,
                        "ema_slow": 100.3,
                        "vwap_distance_bps": 11.0,
                        "structure_score": 0.48,
                        "funding_rate": 0.0,
                        "spread_bps": 1.2,
                        "btc_aligned": True,
                        "market_cluster": "crypto",
                        "cluster_aligned": True,
                        "cluster_leader": "BTC",
                        "book_imbalance": 0.30,
                        "trade_flow_bias": 0.22,
                        "bucket_volume": 9.0,
                        "bucket_notional_usd": 909.0,
                        "bucket_trade_count": 26,
                        "bucket_range_bps": 36.0,
                        "delta_book_imbalance": 0.10,
                        "delta_trade_flow_bias": 0.12,
                        "volume_ratio": 1.8,
                        "trade_count_ratio": 1.4,
                        "realized_vol_short_bps": 8.0,
                        "realized_vol_long_bps": 4.5,
                        "compression_score": 0.62,
                        "microprice_dislocation_bps": 1.0,
                        "source": "test",
                    }
                ],
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "snapshots.jsonl"
            input_path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )
            result = PodBBacktestRunner(config).run_jsonl(input_path)

        self.assertGreaterEqual(result.backtest.get("signal_count", 0), 1)
        self.assertGreaterEqual(result.backtest.get("closed_trade_count", 0), 1)
        self.assertGreater(result.backtest.get("realized_pnl_usd", 0.0), 0.0)


if __name__ == "__main__":
    unittest.main()
