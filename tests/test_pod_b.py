import unittest
import json
import tempfile
from dataclasses import replace
from pathlib import Path

from app.backtest.pod_b_runner import PodBBacktestRunner
from app.settings import PodBPatternRuleConfig, load_config
from app.trident.pod_b import BreakoutContext, BreakoutPlanner, BreakoutService, PodBRiskGate
from app.trident.types import PodAllocation, PodName, SymbolAllocation, TradePlan


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

    def test_service_attaches_microstructure_watch_scores(self) -> None:
        config = load_config("config/trident.toml")
        service = BreakoutService(config)

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
                delta_spread_bps=0.6,
                delta_book_imbalance=0.22,
                delta_trade_flow_bias=0.30,
                volume_ratio=2.4,
                trade_count_ratio=1.9,
                realized_vol_short_bps=7.0,
                realized_vol_long_bps=4.0,
                compression_score=0.70,
                best_bid_size=4.0,
                best_ask_size=1.8,
                bid_depth_10bps=12.0,
                ask_depth_10bps=6.0,
                bid_depth_velocity=0.30,
                ask_depth_velocity=-0.55,
                best_bid_size_velocity=0.25,
                best_ask_size_velocity=-0.45,
                microprice_dislocation_bps=1.4,
            )
        )

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertGreater(float(signal.setup_details.get("liquidity_pull_score", 0.0)), 0.0)
        self.assertGreater(float(signal.setup_details.get("depth_refill_score", 0.0)), 0.0)
        self.assertEqual(signal.setup_details.get("liquidity_pull_direction"), "long")
        self.assertEqual(signal.setup_details.get("depth_refill_direction_depth10"), "long")
        self.assertEqual(signal.setup_details.get("depth_refill_direction_touch"), "long")

    def test_risk_gate_applies_rolling_symbol_setup_guardrail(self) -> None:
        config = load_config("config/trident.toml")
        config = replace(
            config,
            pod_b=replace(
                config.pod_b,
                bis_guardrail_enabled=True,
                bis_guardrail_lookback_trades=2,
                bis_guardrail_min_closed_trades=2,
                bis_guardrail_max_cumulative_loss_usd=-5.0,
            ),
        )
        risk_gate = PodBRiskGate(config)
        plan = TradePlan(
            symbol="BTC",
            side="long",
            setup="vol_expansion_long",
            confidence=0.72,
            target_notional_usd=100.0,
            stop_bps=40.0,
            time_stop_hours=2,
            margin_usd=25.0,
            effective_leverage=2.0,
            risk_budget_usd=7.5,
            expected_loss_usd=3.0,
        )

        risk_gate.record_closed_trade(symbol="BTC", setup="vol_expansion_long", pnl_usd=-3.0)
        first_pass = risk_gate.evaluate_many([plan])
        self.assertTrue(first_pass[0].accepted)

        risk_gate.record_closed_trade(symbol="BTC", setup="vol_expansion_long", pnl_usd=-2.5)
        blocked = risk_gate.evaluate_many([plan])
        self.assertFalse(blocked[0].accepted)
        self.assertEqual(blocked[0].reason, "rolling_guardrail_symbol_setup")

        risk_gate.record_closed_trade(symbol="BTC", setup="vol_expansion_long", pnl_usd=7.0)
        recovered = risk_gate.evaluate_many([plan])
        self.assertTrue(recovered[0].accepted)

    def test_risk_gate_blocks_rule_based_on_pattern_veto(self) -> None:
        config = load_config("config/trident.toml")
        config = replace(
            config,
            pod_b=replace(
                config.pod_b,
                pattern_vetoes=[
                    PodBPatternRuleConfig(
                        name="vol_ratio_low",
                        enabled=True,
                        setups=["vol_expansion_long"],
                        sides=["long"],
                        max_volume_ratio=1.60,
                    )
                ],
            ),
        )
        risk_gate = PodBRiskGate(config)
        plan = TradePlan(
            symbol="BTC",
            side="long",
            setup="vol_expansion_long",
            confidence=0.72,
            target_notional_usd=100.0,
            stop_bps=40.0,
            time_stop_hours=2,
            margin_usd=25.0,
            effective_leverage=2.0,
            risk_budget_usd=7.5,
            expected_loss_usd=3.0,
            setup_details={
                "volume_ratio": 1.52,
                "regime": "TrendExpansion",
            },
        )

        decisions = risk_gate.evaluate_many([plan])
        self.assertEqual(len(decisions), 1)
        self.assertFalse(decisions[0].accepted)
        self.assertEqual(decisions[0].reason, "pattern_veto_vol_ratio_low")

    def test_risk_gate_adds_pattern_watch_hits_on_accept(self) -> None:
        config = load_config("config/trident.toml")
        config = replace(
            config,
            pod_b=replace(
                config.pod_b,
                pattern_vetoes=[],
                pattern_watchers=[
                    PodBPatternRuleConfig(
                        name="watch_mid_vol",
                        enabled=True,
                        setups=["vol_expansion_long"],
                        sides=["long"],
                        max_volume_ratio=1.85,
                    )
                ],
            ),
        )
        risk_gate = PodBRiskGate(config)
        plan = TradePlan(
            symbol="BTC",
            side="long",
            setup="vol_expansion_long",
            confidence=0.72,
            target_notional_usd=100.0,
            stop_bps=40.0,
            time_stop_hours=2,
            margin_usd=25.0,
            effective_leverage=2.0,
            risk_budget_usd=7.5,
            expected_loss_usd=3.0,
            setup_details={
                "volume_ratio": 1.72,
                "regime": "TrendExpansion",
            },
        )

        decisions = risk_gate.evaluate_many([plan])
        self.assertEqual(len(decisions), 1)
        self.assertTrue(decisions[0].accepted)
        self.assertEqual(plan.setup_details.get("pattern_watch_hits"), "watch_mid_vol")
        self.assertEqual(plan.setup_details.get("pattern_watch_count"), 1)

    def test_risk_gate_matches_microstructure_watch_rule(self) -> None:
        config = load_config("config/trident.toml")
        config = replace(
            config,
            pod_b=replace(
                config.pod_b,
                pattern_vetoes=[],
                pattern_watchers=[
                    PodBPatternRuleConfig(
                        name="micro_depth_refill",
                        enabled=True,
                        sides=["long"],
                        regimes=["TrendExpansion"],
                        min_bucket_notional_usd=250.0,
                        max_spread_bps=3.0,
                        min_liquidity_pull_score=0.65,
                        min_depth_refill_score=0.75,
                    )
                ],
            ),
        )
        risk_gate = PodBRiskGate(config)
        plan = TradePlan(
            symbol="BTC",
            side="long",
            setup="vol_expansion_long",
            confidence=0.72,
            target_notional_usd=100.0,
            stop_bps=40.0,
            time_stop_hours=2,
            margin_usd=25.0,
            effective_leverage=2.0,
            risk_budget_usd=7.5,
            expected_loss_usd=3.0,
            setup_details={
                "regime": "TrendExpansion",
                "spread_bps": 1.2,
                "bucket_notional_usd": 800.0,
                "liquidity_pull_score": 0.71,
                "depth_refill_score": 0.82,
            },
        )

        decisions = risk_gate.evaluate_many([plan])
        self.assertEqual(len(decisions), 1)
        self.assertTrue(decisions[0].accepted)
        self.assertEqual(plan.setup_details.get("pattern_watch_hits"), "micro_depth_refill")
        self.assertEqual(plan.setup_details.get("pattern_watch_count"), 1)

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

    def test_service_can_emit_ttm_squeeze_release_setup(self) -> None:
        config = load_config("config/trident.toml")
        squeeze_only_config = replace(
            config,
            pod_b=replace(
                config.pod_b,
                bis_enabled_setups=["ttm_squeeze_release_long"],
                bis_strict_continuation_filter_enabled=False,
            ),
        )
        service = BreakoutService(squeeze_only_config)

        signal = service.evaluate(
            BreakoutContext(
                symbol="ETH",
                regime="TrendExpansion",
                price=100.0,
                ema_fast=100.9,
                ema_slow=100.0,
                vwap_distance_bps=7.0,
                structure_score=0.36,
                funding_rate=0.0,
                spread_bps=1.2,
                btc_aligned=True,
                market_cluster="crypto",
                cluster_leader="BTC",
                book_imbalance=0.20,
                trade_flow_bias=0.22,
                bucket_trade_count=22,
                bucket_notional_usd=780.0,
                bucket_range_bps=34.0,
                delta_book_imbalance=0.15,
                delta_trade_flow_bias=0.24,
                volume_ratio=2.0,
                trade_count_ratio=1.8,
                realized_vol_short_bps=7.2,
                realized_vol_long_bps=4.1,
                compression_score=0.76,
                microprice_dislocation_bps=1.1,
            )
        )

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.setup, "ttm_squeeze_release_long")
        self.assertIn("squeeze_release_quality", signal.confidence_components)

    def test_runner_replays_strategy_on_routed_symbol_universe(self) -> None:
        config = load_config("config/trident.toml")
        config = replace(
            config,
            pod_b=replace(config.pod_b, enabled=True),
        )
        config.hyperliquid.observation_universe = ["BTC"]
        config.trident.allocations.trend_expansion.pod_b = 1.0
        config.trident.allocations.trend_expansion.pod_a = 0.0
        config.trident.allocations.trend_expansion.pod_c = 0.0
        config.trident.allocations.trend_expansion.cash = 0.0

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
