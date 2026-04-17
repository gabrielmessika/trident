import unittest
from dataclasses import replace

from app.risk.pod_a_gate import PodARiskGate
from app.settings import PodASymbolModeConfig, load_config
from app.trident.capital_allocator import CapitalAllocator
from app.trident.pod_a import (
    AnchorTrendContext,
    AnchorTrendPlanner,
    AnchorTrendService,
    MarketContextService,
)
from app.trident.types import PodName, Regime, SymbolMarketSnapshot, TradePlan


class AnchorTrendServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config("config/trident.toml")
        self.service = AnchorTrendService()
        self.context_service = MarketContextService()
        self.planner = AnchorTrendPlanner(self.config)

    def test_generates_bos_retest_signal_for_stronger_structure(self) -> None:
        signal = self.service.evaluate(
            AnchorTrendContext(
                symbol="ETH",
                regime="TrendExpansion",
                price=3102.0,
                ema_fast=3095.0,
                ema_slow=3050.0,
                vwap_distance_bps=3.0,
                structure_score=0.78,
                funding_rate=0.0001,
                spread_bps=1.0,
                btc_aligned=True,
            )
        )

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.setup, "bos_retest_long")
        self.assertIsNotNone(signal.invalidation_price)

    def test_generates_vwap_reclaim_signal_when_flow_confirms_reclaim(self) -> None:
        signal = self.service.evaluate(
            AnchorTrendContext(
                symbol="SOL",
                regime="TrendExpansion",
                price=152.0,
                ema_fast=151.6,
                ema_slow=150.8,
                vwap_distance_bps=2.0,
                structure_score=0.58,
                funding_rate=0.0001,
                spread_bps=1.0,
                btc_aligned=True,
                book_imbalance=0.15,
                trade_flow_bias=0.18,
                bucket_range_bps=12.0,
            )
        )

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.setup, "vwap_reclaim_long")

    def test_generates_vwap_reclaim_signal_in_range_auction_when_mtf_is_already_strong(self) -> None:
        signal = self.service.evaluate(
            AnchorTrendContext(
                symbol="ETH",
                regime="RangeAuction",
                price=3120.0,
                ema_fast=3115.0,
                ema_slow=3102.0,
                vwap_distance_bps=-3.0,
                structure_score=0.56,
                funding_rate=0.0,
                spread_bps=1.0,
                btc_aligned=True,
                book_imbalance=0.22,
                trade_flow_bias=0.24,
                bucket_range_bps=14.0,
                trend_1h_bps=32.0,
                trend_4h_bps=48.0,
                mtf_bias_score=40.0,
                candles_ready=True,
            )
        )

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.setup, "vwap_reclaim_long")

    def test_generates_liquidity_sweep_reclaim_signal_when_range_and_flow_expand(self) -> None:
        signal = self.service.evaluate(
            AnchorTrendContext(
                symbol="HYPE",
                regime="TrendExpansion",
                price=20.2,
                ema_fast=20.1,
                ema_slow=19.9,
                vwap_distance_bps=-12.0,
                structure_score=0.67,
                funding_rate=0.0,
                spread_bps=1.1,
                btc_aligned=True,
                book_imbalance=0.28,
                trade_flow_bias=0.22,
                bucket_range_bps=26.0,
            )
        )

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.setup, "liquidity_sweep_reclaim_long")
        self.assertGreater(signal.confidence, 0.75)

    def test_generates_long_signal_in_trend_expansion(self) -> None:
        signal = self.service.evaluate(
            AnchorTrendContext(
                symbol="ETH",
                regime="TrendExpansion",
                price=3100.0,
                ema_fast=3090.0,
                ema_slow=3050.0,
                vwap_distance_bps=-8.0,
                structure_score=0.62,
                funding_rate=0.0001,
                spread_bps=1.2,
                btc_aligned=True,
            )
        )

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, "long")
        self.assertEqual(signal.setup, "trend_pullback_long")

    def test_trend_pullback_respects_indicator_vetoes(self) -> None:
        signal = self.service.evaluate(
            AnchorTrendContext(
                symbol="ETH",
                regime="TrendExpansion",
                price=3100.0,
                ema_fast=3090.0,
                ema_slow=3050.0,
                vwap_distance_bps=-8.0,
                structure_score=0.62,
                funding_rate=0.0001,
                spread_bps=1.2,
                btc_aligned=True,
                ichimoku_bias_score=-0.35,
                supertrend_direction=-1,
                vwap_reclaim_score=-0.25,
            )
        )

        self.assertIsNone(signal)

    def test_indicator_confirmation_boosts_confidence(self) -> None:
        supportive = self.service.evaluate(
            AnchorTrendContext(
                symbol="ETH",
                regime="TrendExpansion",
                price=3100.0,
                ema_fast=3090.0,
                ema_slow=3050.0,
                vwap_distance_bps=-8.0,
                structure_score=0.62,
                funding_rate=0.0001,
                spread_bps=1.2,
                btc_aligned=True,
                ichimoku_bias_score=0.42,
                supertrend_direction=1,
                stoch_rsi_k=0.56,
                cci20=42.0,
                vwap_reclaim_score=0.28,
            )
        )
        cautious = self.service.evaluate(
            AnchorTrendContext(
                symbol="ETH",
                regime="TrendExpansion",
                price=3100.0,
                ema_fast=3090.0,
                ema_slow=3050.0,
                vwap_distance_bps=-8.0,
                structure_score=0.62,
                funding_rate=0.0001,
                spread_bps=1.2,
                btc_aligned=True,
                ichimoku_bias_score=-0.05,
                supertrend_direction=0,
                stoch_rsi_k=0.90,
                cci20=140.0,
                vwap_reclaim_score=0.02,
            )
        )

        self.assertIsNotNone(supportive)
        self.assertIsNotNone(cautious)
        assert supportive is not None
        assert cautious is not None
        self.assertGreater(supportive.confidence, cautious.confidence)
        self.assertIn("confirmation_quality", supportive.confidence_components)

    def test_generates_short_signal_in_trend_expansion(self) -> None:
        signal = self.service.evaluate(
            AnchorTrendContext(
                symbol="SOL",
                regime="TrendExpansion",
                price=140.0,
                ema_fast=141.0,
                ema_slow=145.0,
                vwap_distance_bps=9.0,
                structure_score=-0.58,
                funding_rate=-0.0001,
                spread_bps=1.8,
                btc_aligned=True,
            )
        )

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, "short")
        self.assertEqual(signal.setup, "trend_pullback_short")

    def test_generates_signal_for_index_cluster_without_btc_dependency(self) -> None:
        contexts = self.context_service.build_contexts(
            regime=Regime.TREND_EXPANSION,
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="SPY",
                    price=5000.0,
                    ema_fast=4992.0,
                    ema_slow=4975.0,
                    vwap_distance_bps=-6.0,
                    structure_score=0.58,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=False,
                )
            ],
        )

        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0].market_cluster, "index")
        self.assertEqual(contexts[0].cluster_leader, "SPY")
        self.assertTrue(contexts[0].cluster_aligned)

        signal = self.service.evaluate(contexts[0])

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.market_cluster, "index")
        self.assertEqual(signal.cluster_leader, "SPY")

    def test_rejects_non_trending_context(self) -> None:
        signal = self.service.evaluate(
            AnchorTrendContext(
                symbol="BTC",
                regime="RangeAuction",
                price=68000.0,
                ema_fast=67950.0,
                ema_slow=67800.0,
                vwap_distance_bps=-5.0,
                structure_score=0.55,
                funding_rate=0.0,
                spread_bps=0.8,
                btc_aligned=True,
            )
        )

        self.assertIsNone(signal)

    def test_market_context_service_builds_contexts(self) -> None:
        contexts = self.context_service.build_contexts(
            regime=Regime.TREND_EXPANSION,
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="ETH",
                    price=3100.0,
                    ema_fast=3090.0,
                    ema_slow=3050.0,
                    vwap_distance_bps=-8.0,
                    structure_score=0.62,
                    funding_rate=0.0001,
                    spread_bps=1.2,
                    btc_aligned=True,
                )
            ],
        )

        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0].regime, "TrendExpansion")
        self.assertEqual(contexts[0].symbol, "ETH")

    def test_market_context_service_accumulates_multi_timeframe_bias(self) -> None:
        timestamps = [
            "2026-04-04T00:00:00Z",
            "2026-04-04T01:00:00Z",
            "2026-04-04T02:00:00Z",
            "2026-04-04T03:00:00Z",
            "2026-04-04T04:00:00Z",
        ]
        prices = [100.0, 101.0, 102.0, 103.0, 104.0]

        last_context = None
        for timestamp, price in zip(timestamps, prices, strict=True):
            contexts = self.context_service.build_contexts(
                regime=Regime.TREND_EXPANSION,
                timestamp=timestamp,
                snapshots=[
                    SymbolMarketSnapshot(
                        symbol="ETH",
                        price=price,
                        ema_fast=price - 0.2,
                        ema_slow=price - 0.8,
                        vwap_distance_bps=1.0,
                        structure_score=0.65,
                        funding_rate=0.0,
                        spread_bps=1.0,
                        btc_aligned=True,
                    )
                ],
            )
            last_context = contexts[0]

        assert last_context is not None
        self.assertTrue(last_context.candles_ready)
        self.assertGreater(last_context.trend_15m_bps, 0.0)
        self.assertGreater(last_context.trend_1h_bps, 0.0)
        self.assertGreater(last_context.trend_4h_bps, 0.0)
        self.assertGreater(last_context.mtf_bias_score, 0.0)

    def test_market_context_service_detects_hourly_structure_break(self) -> None:
        timestamps = [
            "2026-04-04T00:00:00Z",
            "2026-04-04T01:00:00Z",
            "2026-04-04T02:00:00Z",
            "2026-04-04T03:00:00Z",
            "2026-04-04T04:00:00Z",
            "2026-04-04T05:00:00Z",
        ]
        prices = [100.0, 102.0, 101.0, 103.0, 102.0, 104.0]

        last_context = None
        for timestamp, price in zip(timestamps, prices, strict=True):
            contexts = self.context_service.build_contexts(
                regime=Regime.TREND_EXPANSION,
                timestamp=timestamp,
                snapshots=[
                    SymbolMarketSnapshot(
                        symbol="ETH",
                        price=price,
                        ema_fast=price - 0.2,
                        ema_slow=price - 0.8,
                        vwap_distance_bps=0.5,
                        structure_score=0.75,
                        funding_rate=0.0,
                        spread_bps=1.0,
                        btc_aligned=True,
                    )
                ],
            )
            last_context = contexts[0]

        assert last_context is not None
        self.assertTrue(last_context.structure_ready)
        self.assertGreater(last_context.swing_high_1h, 0.0)
        self.assertGreater(last_context.range_high_1h, 0.0)
        self.assertTrue(last_context.bos_long_confirmed)

    def test_best_signal_returns_highest_confidence_candidate(self) -> None:
        contexts = [
            AnchorTrendContext(
                symbol="ETH",
                regime="TrendExpansion",
                price=3100.0,
                ema_fast=3090.0,
                ema_slow=3050.0,
                vwap_distance_bps=-8.0,
                structure_score=0.62,
                funding_rate=0.0001,
                spread_bps=1.2,
                btc_aligned=True,
            ),
            AnchorTrendContext(
                symbol="SOL",
                regime="TrendExpansion",
                price=140.0,
                ema_fast=141.0,
                ema_slow=145.0,
                vwap_distance_bps=9.0,
                structure_score=-0.58,
                funding_rate=-0.0001,
                spread_bps=1.8,
                btc_aligned=True,
            ),
        ]

        signal = self.service.best_signal(contexts)

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.symbol, "ETH")

    def test_trade_planner_uses_symbol_allocation(self) -> None:
        allocation = CapitalAllocator(self.config).build_plan(
            Regime.TREND_EXPANSION,
            {
                PodName.POD_A: ["BTC", "ETH", "HYPE", "SOL"],
                PodName.POD_B: [],
                PodName.POD_C: [],
            },
        ).pod_allocations[PodName.POD_A]

        signal = self.service.evaluate(
            AnchorTrendContext(
                symbol="ETH",
                regime="TrendExpansion",
                price=3100.0,
                ema_fast=3090.0,
                ema_slow=3050.0,
                vwap_distance_bps=-8.0,
                structure_score=0.62,
                funding_rate=0.0001,
                spread_bps=1.2,
                btc_aligned=True,
            )
        )

        assert signal is not None
        trade_plan = self.planner.build_trade_plan(signal, allocation)

        self.assertIsNotNone(trade_plan)
        assert trade_plan is not None
        # Leverage and sizing properties
        self.assertAlmostEqual(
            trade_plan.target_notional_usd,
            trade_plan.margin_usd * trade_plan.effective_leverage,
            places=0,
        )
        self.assertGreater(trade_plan.margin_usd, 0.0)
        self.assertGreater(trade_plan.effective_leverage, 0.0)
        self.assertEqual(trade_plan.stop_bps, 160.0)
        self.assertGreater(trade_plan.expected_loss_usd, 0.0)
        self.assertEqual(trade_plan.time_stop_hours, 24)

    def test_trade_planner_clamps_leverage_to_asset_limit(self) -> None:
        config = replace(
            self.config,
            pod_a=replace(
                self.config.pod_a,
                default_leverage=10.0,
                max_leverage=10.0,
                max_leverage_by_symbol={"ETH": 2.0},
            ),
        )
        planner = AnchorTrendPlanner(config)
        allocation = CapitalAllocator(config).build_plan(
            Regime.TREND_EXPANSION,
            {
                PodName.POD_A: ["BTC", "ETH", "HYPE", "SOL"],
                PodName.POD_B: [],
                PodName.POD_C: [],
            },
        ).pod_allocations[PodName.POD_A]
        signal = self.service.evaluate(
            AnchorTrendContext(
                symbol="ETH",
                regime="TrendExpansion",
                price=3100.0,
                ema_fast=3090.0,
                ema_slow=3050.0,
                vwap_distance_bps=-8.0,
                structure_score=0.62,
                funding_rate=0.0001,
                spread_bps=1.2,
                btc_aligned=True,
            )
        )

        assert signal is not None
        trade_plan = planner.build_trade_plan(signal, allocation)

        self.assertIsNotNone(trade_plan)
        assert trade_plan is not None
        self.assertEqual(trade_plan.requested_leverage, 2.0)
        self.assertEqual(trade_plan.effective_leverage, 2.0)
        # Notional = margin * effective_leverage; margin depends on allocation
        self.assertAlmostEqual(
            trade_plan.target_notional_usd,
            trade_plan.margin_usd * trade_plan.effective_leverage,
            places=2,
        )

    def test_trade_planner_applies_tao_symbol_mode_overrides(self) -> None:
        config = replace(
            self.config,
            pod_a=replace(
                self.config.pod_a,
                symbol_modes={
                    "TAO": PodASymbolModeConfig(
                        enabled=True,
                        allowed_setups=["trend_pullback_long"],
                        allowed_regimes=["TrendExpansion"],
                        min_confidence=0.6,
                        risk_per_trade_pct_multiplier=0.5,
                        stop_bps_multiplier=2.0,
                        stop_bps_floor=220.0,
                        time_stop_hours=48,
                        take_profit_multiplier=1.1,
                        break_even_multiplier=1.25,
                        trailing_activation_multiplier=1.3,
                        trailing_distance_multiplier=1.15,
                        max_leverage=4.0,
                    )
                },
            ),
        )
        planner = AnchorTrendPlanner(config)
        allocation = CapitalAllocator(config).build_plan(
            Regime.TREND_EXPANSION,
            {
                PodName.POD_A: ["TAO"],
                PodName.POD_B: [],
                PodName.POD_C: [],
            },
        ).pod_allocations[PodName.POD_A]
        signal = self.service.evaluate(
            AnchorTrendContext(
                symbol="TAO",
                regime="TrendExpansion",
                price=300.0,
                ema_fast=298.0,
                ema_slow=292.0,
                vwap_distance_bps=-8.0,
                structure_score=0.62,
                funding_rate=0.0,
                spread_bps=1.2,
                btc_aligned=True,
            )
        )

        assert signal is not None
        trade_plan = planner.build_trade_plan(signal, allocation)

        self.assertIsNotNone(trade_plan)
        assert trade_plan is not None
        self.assertGreaterEqual(trade_plan.stop_bps, 220.0)
        self.assertEqual(trade_plan.time_stop_hours, 48)
        self.assertLessEqual(trade_plan.effective_leverage, 4.0)
        self.assertEqual(trade_plan.risk_budget_usd, 6.25)
        self.assertTrue(bool(trade_plan.setup_details.get("special_symbol_mode_active")))

    def test_symbol_mode_can_bypass_global_disabled_setup_for_tao(self) -> None:
        config = replace(
            self.config,
            pod_a=replace(
                self.config.pod_a,
                symbol_modes={
                    "TAO": PodASymbolModeConfig(
                        enabled=True,
                        allowed_setups=["trend_pullback_short"],
                        allowed_regimes=["TrendExpansion"],
                        min_confidence=0.65,
                    )
                },
            ),
        )
        gate = PodARiskGate(config)
        accepted_plan = TradePlan(
            symbol="TAO",
            side="short",
            setup="trend_pullback_short",
            confidence=0.7,
            target_notional_usd=200.0,
            stop_bps=180.0,
            time_stop_hours=48,
            margin_usd=50.0,
            requested_leverage=4.0,
            effective_leverage=4.0,
            risk_budget_usd=7.5,
            expected_loss_usd=3.6,
            setup_details={"regime": "TrendExpansion"},
        )
        rejected_plan = replace(accepted_plan, confidence=0.6)

        accepted = gate.evaluate_many([accepted_plan])[0]
        rejected = gate.evaluate_many([rejected_plan])[0]

        self.assertTrue(accepted.accepted)
        self.assertEqual(accepted.reason, "accepted")
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.reason, "symbol_mode_confidence_below_min")


if __name__ == "__main__":
    unittest.main()
