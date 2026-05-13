import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from app.risk.pod_a_gate import PodARiskGate
from app.settings import (
    PodACampaignConfig,
    PodAReversalFadeConfig,
    PodASetupRunnerConfig,
    PodAStructuralTargetConfig,
    PodASymbolModeConfig,
    load_config,
)
from app.trident.capital_allocator import CapitalAllocator
from app.trident.pod_a import (
    AnchorTrendContext,
    AnchorTrendSignal,
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

    def test_trend_pullback_details_include_btc_overextension_features(self) -> None:
        signal = self.service.evaluate(
            AnchorTrendContext(
                symbol="BTC",
                regime="TrendExpansion",
                price=70000.0,
                ema_fast=69800.0,
                ema_slow=67200.0,
                vwap_distance_bps=-8.0,
                structure_score=0.62,
                funding_rate=0.0001,
                spread_bps=1.2,
                btc_aligned=True,
                rsi21_4h=70.0,
                ema50_distance_4h_pct=4.6,
                ema50_distance_4h_atr=2.2,
                macd_hist_4h=180.0,
                macd_hist_delta_4h=-12.5,
                upper_wick_ratio_4h=0.22,
                bb_position_4h=0.96,
                btc_overextension_score=0.73,
            )
        )

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.setup, "trend_pullback_long")
        self.assertEqual(signal.setup_details["rsi21_4h"], 70.0)
        self.assertEqual(signal.setup_details["btc_overextension_score"], 0.73)

    def test_trend_pullback_details_include_completed_mtf_candidate_features(self) -> None:
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
                prev_ema50_ready_1h=True,
                prev_rsi14_1h=46.2,
                prev_ema20_distance_ema50_1h_pct=-0.42,
                entry_vs_open_1h_bps=58.4,
                prev_ema50_ready_4h=True,
                prev_rsi14_4h=38.6,
                prev_ema50_distance_4h_pct=-1.15,
            )
        )

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.setup, "trend_pullback_long")
        self.assertTrue(signal.setup_details["prev_ema50_ready_1h"])
        self.assertEqual(signal.setup_details["prev_rsi14_1h"], 46.2)
        self.assertEqual(signal.setup_details["entry_vs_open_1h_bps"], 58.4)
        self.assertEqual(signal.setup_details["prev_rsi14_4h"], 38.6)
        self.assertEqual(signal.setup_details["prev_ema50_distance_4h_pct"], -1.15)

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

    def test_generates_ichimoku_continuation_long_when_mtf_trend_is_clean(self) -> None:
        config = replace(
            self.config,
            pod_a=replace(
                self.config.pod_a,
                symbol_modes={
                    "BIO": PodASymbolModeConfig(
                        enabled=True,
                        allowed_setups=["ichimoku_continuation_long"],
                        allowed_regimes=["TrendExpansion"],
                        min_confidence=0.65,
                    )
                },
            ),
        )
        service = AnchorTrendService(config)
        signal = service.evaluate(
            AnchorTrendContext(
                symbol="BIO",
                regime="TrendExpansion",
                price=1.024,
                ema_fast=1.018,
                ema_slow=0.998,
                vwap_distance_bps=3.0,
                structure_score=0.34,
                funding_rate=0.0,
                spread_bps=1.0,
                btc_aligned=True,
                trend_1h_bps=22.0,
                trend_4h_bps=48.0,
                mtf_bias_score=31.0,
                candles_ready=True,
                ichimoku_bias_score=0.42,
                supertrend_direction=1,
                stoch_rsi_k=0.58,
                vwap_reclaim_score=0.06,
            )
        )

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.setup, "ichimoku_continuation_long")

    def test_generates_ichimoku_continuation_short_when_bearish_trend_is_clean(self) -> None:
        config = replace(
            self.config,
            pod_a=replace(
                self.config.pod_a,
                a_grade_enabled=False,
                symbol_modes={
                    "TAO": PodASymbolModeConfig(
                        enabled=True,
                        allowed_setups=["ichimoku_continuation_short"],
                        allowed_regimes=["TrendExpansion"],
                        min_confidence=0.65,
                    )
                },
            ),
        )
        service = AnchorTrendService(config)
        signal = service.evaluate(
            AnchorTrendContext(
                symbol="TAO",
                regime="TrendExpansion",
                price=244.0,
                ema_fast=245.0,
                ema_slow=248.5,
                vwap_distance_bps=-2.0,
                structure_score=-0.36,
                funding_rate=0.0,
                spread_bps=1.1,
                btc_aligned=True,
                trend_1h_bps=-18.0,
                trend_4h_bps=-42.0,
                mtf_bias_score=-27.0,
                candles_ready=True,
                ichimoku_bias_score=-0.38,
                supertrend_direction=-1,
                stoch_rsi_k=0.42,
                vwap_reclaim_score=-0.03,
            )
        )

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.setup, "ichimoku_continuation_short")

    def test_default_prod_config_keeps_trend_pullback_when_ichimoku_special_mode_is_not_enabled(self) -> None:
        signal = self.service.evaluate(
            AnchorTrendContext(
                symbol="BIO",
                regime="TrendExpansion",
                price=1.024,
                ema_fast=1.018,
                ema_slow=0.998,
                vwap_distance_bps=3.0,
                structure_score=0.46,
                funding_rate=0.0,
                spread_bps=1.0,
                btc_aligned=True,
                trend_1h_bps=22.0,
                trend_4h_bps=48.0,
                mtf_bias_score=31.0,
                candles_ready=True,
                ichimoku_bias_score=0.42,
                supertrend_direction=1,
                stoch_rsi_k=0.58,
                vwap_reclaim_score=0.06,
            )
        )

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.setup, "trend_pullback_long")

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

    def test_generates_reversal_fade_short_when_rejection_is_confirmed(self) -> None:
        service = AnchorTrendService(
            replace(
                self.config,
                pod_a=replace(
                    self.config.pod_a,
                    reversal_fade=PodAReversalFadeConfig(
                        enabled=True,
                        allowed_regimes=["TrendExpansion", "PanicSqueeze"],
                        max_distance_from_resistance_bps=18.0,
                        min_target_to_support_bps=35.0,
                        min_trend_1h_bps=8.0,
                        min_trend_4h_bps=12.0,
                        min_rejection_flow=0.10,
                        min_stoch_rsi_k=0.72,
                        min_cci20=90.0,
                        max_vwap_reclaim_score=-0.05,
                    ),
                ),
            )
        )
        signal = service.evaluate(
            AnchorTrendContext(
                symbol="ETH",
                regime="TrendExpansion",
                price=101.4,
                ema_fast=101.6,
                ema_slow=100.8,
                vwap_distance_bps=4.0,
                structure_score=0.18,
                funding_rate=0.0,
                spread_bps=1.2,
                btc_aligned=True,
                book_imbalance=-0.22,
                trade_flow_bias=-0.18,
                bucket_range_bps=24.0,
                trend_1h_bps=18.0,
                trend_4h_bps=34.0,
                mtf_bias_score=24.0,
                candles_ready=True,
                structure_ready=True,
                range_high_1h=101.6,
                range_low_1h=100.5,
                swing_high_1h=101.5,
                swing_low_1h=100.8,
                ichimoku_bias_score=0.12,
                supertrend_direction=-1,
                stoch_rsi_k=0.84,
                cci20=122.0,
                vwap_reclaim_score=-0.11,
            )
        )

        self.assertIsNotNone(signal)
        assert signal is not None
        self.assertEqual(signal.side, "short")
        self.assertEqual(signal.setup, "reversal_fade_short")
        self.assertEqual(signal.setup_details.get("family"), "reversal_fade")

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

    def test_market_context_service_exposes_completed_hourly_candidate_features(self) -> None:
        base = datetime(2026, 4, 4, tzinfo=UTC)
        last_context = None
        for index in range(55):
            price = 100.0 + index
            contexts = self.context_service.build_contexts(
                regime=Regime.TREND_EXPANSION,
                timestamp=(base + timedelta(hours=index)).isoformat().replace("+00:00", "Z"),
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
        contexts = self.context_service.build_contexts(
            regime=Regime.TREND_EXPANSION,
            timestamp=(base + timedelta(hours=54, minutes=30)).isoformat().replace("+00:00", "Z"),
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="ETH",
                    price=156.0,
                    ema_fast=155.8,
                    ema_slow=155.2,
                    vwap_distance_bps=1.0,
                    structure_score=0.65,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                )
            ],
        )
        last_context = contexts[0]

        self.assertTrue(last_context.prev_ema50_ready_1h)
        self.assertGreater(last_context.prev_rsi14_1h, 50.0)
        self.assertGreater(last_context.prev_ema20_distance_ema50_1h_pct, 0.0)
        self.assertGreater(last_context.entry_vs_open_1h_bps, 0.0)

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

    def test_trade_planner_applies_campaign_mode_for_crypto_trend_pullback(self) -> None:
        config = replace(
            self.config,
            pod_a=replace(
                self.config.pod_a,
                campaign=PodACampaignConfig(
                    enabled=True,
                    setups=["trend_pullback_long"],
                    allowed_regimes=["TrendExpansion", "PanicSqueeze"],
                    require_candles_ready=True,
                    min_confidence=0.6,
                    min_structure_score=0.5,
                    min_ichimoku_bias_score=0.2,
                    max_stoch_rsi_k=0.82,
                    max_cci20=120.0,
                    stop_bps_multiplier=1.5,
                    stop_bps_floor=220.0,
                    time_stop_hours=36,
                    take_profit_multiplier=0.0,
                    break_even_multiplier=1.4,
                    trailing_activation_multiplier=1.8,
                    trailing_distance_multiplier=1.1,
                    reentry_cooldown_minutes=45,
                ),
            ),
        )
        planner = AnchorTrendPlanner(config)
        allocation = CapitalAllocator(config).build_plan(
            Regime.TREND_EXPANSION,
            {
                PodName.POD_A: ["ETH"],
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
                candles_ready=True,
                trend_1h_bps=22.0,
                trend_4h_bps=48.0,
                ichimoku_bias_score=0.35,
                stoch_rsi_k=0.58,
                cci20=48.0,
            )
        )

        assert signal is not None
        trade_plan = planner.build_trade_plan(signal, allocation)

        self.assertIsNotNone(trade_plan)
        assert trade_plan is not None
        self.assertEqual(trade_plan.time_stop_hours, 36)
        self.assertEqual(trade_plan.take_profit_bps, 0.0)
        self.assertEqual(trade_plan.reentry_cooldown_minutes, 45)
        self.assertGreaterEqual(trade_plan.stop_bps, 220.0)
        self.assertTrue(bool(trade_plan.setup_details.get("campaign_mode_active")))
        self.assertTrue(bool(trade_plan.setup_details.get("routing_revoke_exempt")))
        self.assertGreater(trade_plan.trailing_activation_bps, trade_plan.stop_bps)

    def test_trade_planner_applies_setup_runner_for_crypto_trend_pullback(self) -> None:
        config = replace(
            self.config,
            pod_a=replace(
                self.config.pod_a,
                a_grade_enabled=False,
                setup_runner=PodASetupRunnerConfig(
                    enabled=True,
                    setups=["trend_pullback_long"],
                    allowed_market_clusters=["crypto"],
                    min_confidence=0.6,
                    take_profit_multiplier=0.0,
                    break_even_multiplier=1.0,
                    trailing_activation_multiplier=1.4,
                    trailing_distance_multiplier=0.8,
                ),
            ),
        )
        planner = AnchorTrendPlanner(config)
        allocation = CapitalAllocator(config).build_plan(
            Regime.TREND_EXPANSION,
            {
                PodName.POD_A: ["ETH"],
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
        self.assertEqual(trade_plan.take_profit_bps, 0.0)
        self.assertEqual(trade_plan.break_even_trigger_bps, 160.0)
        self.assertEqual(trade_plan.trailing_activation_bps, 224.0)
        self.assertEqual(trade_plan.trailing_distance_bps, 128.0)
        self.assertTrue(bool(trade_plan.setup_details.get("setup_runner_active")))
        self.assertFalse(bool(trade_plan.setup_details.get("campaign_mode_active")))

    def test_trade_planner_applies_a_grade_boost_and_wider_exits(self) -> None:
        base_config = replace(
            self.config,
            pod_a=replace(self.config.pod_a, a_grade_enabled=False),
        )
        boosted_config = replace(
            self.config,
            pod_a=replace(self.config.pod_a, a_grade_enabled=True),
        )
        signal = AnchorTrendSignal(
            symbol="ETH",
            side="long",
            setup="trend_pullback_long",
            confidence=0.70,
            entry_price=3100.0,
            invalidation_price=3050.0,
            market_cluster="crypto",
            cluster_leader="BTC",
            setup_details={
                "regime": "TrendExpansion",
                "structure_score": 0.62,
                "candles_ready": True,
                "trend_1h_bps": 22.0,
                "trend_4h_bps": 48.0,
                "stoch_rsi_k": 0.58,
                "cci20": 48.0,
                "vwap_reclaim_score": 0.55,
                "btc_overextension_score": 0.20,
            },
        )
        base_allocation = CapitalAllocator(base_config).build_plan(
            Regime.TREND_EXPANSION,
            {
                PodName.POD_A: ["ETH"],
                PodName.POD_B: [],
                PodName.POD_C: [],
            },
        ).pod_allocations[PodName.POD_A]
        boosted_allocation = CapitalAllocator(boosted_config).build_plan(
            Regime.TREND_EXPANSION,
            {
                PodName.POD_A: ["ETH"],
                PodName.POD_B: [],
                PodName.POD_C: [],
            },
        ).pod_allocations[PodName.POD_A]

        base_plan = AnchorTrendPlanner(base_config).build_trade_plan(signal, base_allocation)
        boosted_plan = AnchorTrendPlanner(boosted_config).build_trade_plan(
            signal,
            boosted_allocation,
        )

        self.assertIsNotNone(base_plan)
        self.assertIsNotNone(boosted_plan)
        assert base_plan is not None
        assert boosted_plan is not None
        self.assertTrue(bool(boosted_plan.setup_details.get("a_grade_active")))
        self.assertEqual(boosted_plan.setup_details.get("a_grade_level"), "strong")
        self.assertGreaterEqual(int(boosted_plan.setup_details.get("a_grade_score", 0)), 8)
        self.assertAlmostEqual(
            boosted_plan.target_notional_usd,
            base_plan.target_notional_usd * 1.4,
            places=4,
        )
        self.assertAlmostEqual(boosted_plan.margin_usd, base_plan.margin_usd * 1.4, places=4)
        self.assertAlmostEqual(
            boosted_plan.risk_budget_usd,
            base_plan.risk_budget_usd * 1.4,
            places=4,
        )
        self.assertAlmostEqual(
            boosted_plan.expected_loss_usd,
            base_plan.expected_loss_usd * 1.4,
            places=4,
        )
        self.assertAlmostEqual(
            boosted_plan.break_even_trigger_bps,
            base_plan.break_even_trigger_bps * 1.2,
            places=4,
        )
        self.assertAlmostEqual(
            boosted_plan.trailing_activation_bps,
            base_plan.trailing_activation_bps * 1.15,
            places=4,
        )
        self.assertAlmostEqual(
            boosted_plan.trailing_distance_bps,
            base_plan.trailing_distance_bps * 1.35,
            places=4,
        )

    def test_trade_planner_reserves_capacity_for_campaign_add_on(self) -> None:
        config = replace(
            self.config,
            pod_a=replace(
                self.config.pod_a,
                a_grade_enabled=False,
                campaign=PodACampaignConfig(
                    enabled=True,
                    setups=["trend_pullback_long"],
                    allowed_regimes=["TrendExpansion", "PanicSqueeze"],
                    require_candles_ready=True,
                    min_confidence=0.6,
                    min_structure_score=0.5,
                    min_ichimoku_bias_score=0.2,
                    max_stoch_rsi_k=0.82,
                    max_cci20=120.0,
                    stop_bps_multiplier=1.5,
                    stop_bps_floor=220.0,
                    time_stop_hours=36,
                    take_profit_multiplier=0.0,
                    break_even_multiplier=1.4,
                    trailing_activation_multiplier=1.8,
                    trailing_distance_multiplier=1.1,
                    reentry_cooldown_minutes=45,
                    initial_entry_fraction=0.7,
                    add_on_enabled=True,
                    add_on_fraction=0.3,
                    add_on_trigger_bps=35.0,
                    add_on_min_confidence=0.72,
                    max_add_ons_per_position=1,
                ),
            ),
        )
        planner = AnchorTrendPlanner(config)
        allocation = CapitalAllocator(config).build_plan(
            Regime.TREND_EXPANSION,
            {
                PodName.POD_A: ["ETH"],
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
                candles_ready=True,
                trend_1h_bps=22.0,
                trend_4h_bps=48.0,
                ichimoku_bias_score=0.35,
                stoch_rsi_k=0.58,
                cci20=48.0,
            )
        )

        assert signal is not None
        trade_plan = planner.build_trade_plan(signal, allocation)

        self.assertIsNotNone(trade_plan)
        assert trade_plan is not None
        self.assertAlmostEqual(
            trade_plan.target_notional_usd,
            float(trade_plan.setup_details["campaign_base_target_notional_usd"]) * 0.7,
            places=4,
        )
        self.assertTrue(bool(trade_plan.setup_details.get("campaign_add_on_enabled")))
        self.assertAlmostEqual(
            float(trade_plan.setup_details.get("campaign_add_on_fraction", 0.0)),
            0.3,
            places=4,
        )
        self.assertEqual(int(trade_plan.setup_details.get("campaign_max_add_ons", 0)), 1)

    def test_trade_planner_skips_campaign_mode_when_mtf_confirmation_is_not_ready(self) -> None:
        config = replace(
            self.config,
            pod_a=replace(
                self.config.pod_a,
                campaign=PodACampaignConfig(
                    enabled=True,
                    setups=["trend_pullback_long"],
                    allowed_regimes=["TrendExpansion", "PanicSqueeze"],
                    require_candles_ready=True,
                    min_confidence=0.6,
                    min_structure_score=0.5,
                    min_ichimoku_bias_score=0.2,
                    max_stoch_rsi_k=0.82,
                    max_cci20=120.0,
                    stop_bps_multiplier=1.5,
                    stop_bps_floor=220.0,
                    time_stop_hours=36,
                    take_profit_multiplier=0.0,
                    break_even_multiplier=1.4,
                    trailing_activation_multiplier=1.8,
                    trailing_distance_multiplier=1.1,
                    reentry_cooldown_minutes=45,
                ),
            ),
        )
        planner = AnchorTrendPlanner(config)
        allocation = CapitalAllocator(config).build_plan(
            Regime.TREND_EXPANSION,
            {
                PodName.POD_A: ["ETH"],
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
        self.assertFalse(bool(trade_plan.setup_details.get("campaign_mode_active")))
        self.assertEqual(trade_plan.time_stop_hours, 24)

    def test_trade_planner_applies_structural_take_profit_from_nearest_resistance(self) -> None:
        config = replace(
            self.config,
            pod_a=replace(
                self.config.pod_a,
                campaign=PodACampaignConfig(
                    enabled=True,
                    setups=["trend_pullback_long"],
                    allowed_regimes=["TrendExpansion"],
                    require_candles_ready=True,
                    min_confidence=0.6,
                    min_structure_score=0.5,
                    min_ichimoku_bias_score=0.2,
                    max_stoch_rsi_k=0.82,
                    max_cci20=120.0,
                    stop_bps_multiplier=1.5,
                    stop_bps_floor=220.0,
                    time_stop_hours=36,
                    take_profit_multiplier=0.0,
                    break_even_multiplier=1.4,
                    trailing_activation_multiplier=1.8,
                    trailing_distance_multiplier=1.1,
                    reentry_cooldown_minutes=45,
                ),
                structural_targets=PodAStructuralTargetConfig(
                    enabled=True,
                    setups=["trend_pullback_long"],
                    require_structure_ready=True,
                    target_buffer_bps=6.0,
                    min_target_bps=25.0,
                    max_target_bps=220.0,
                ),
            ),
        )
        planner = AnchorTrendPlanner(config)
        allocation = CapitalAllocator(config).build_plan(
            Regime.TREND_EXPANSION,
            {
                PodName.POD_A: ["ETH"],
                PodName.POD_B: [],
                PodName.POD_C: [],
            },
        ).pod_allocations[PodName.POD_A]
        signal = self.service.evaluate(
            AnchorTrendContext(
                symbol="ETH",
                regime="TrendExpansion",
                price=100.0,
                ema_fast=99.4,
                ema_slow=98.0,
                vwap_distance_bps=-8.0,
                structure_score=0.62,
                funding_rate=0.0001,
                spread_bps=1.2,
                btc_aligned=True,
                candles_ready=True,
                structure_ready=True,
                swing_high_1h=101.50,
                range_high_1h=102.40,
                swing_low_1h=98.50,
                range_low_1h=97.80,
                trend_1h_bps=22.0,
                trend_4h_bps=48.0,
                ichimoku_bias_score=0.35,
                stoch_rsi_k=0.58,
                cci20=48.0,
            )
        )

        assert signal is not None
        trade_plan = planner.build_trade_plan(signal, allocation)

        self.assertIsNotNone(trade_plan)
        assert trade_plan is not None
        self.assertAlmostEqual(trade_plan.take_profit_bps, 144.0, places=4)
        self.assertTrue(bool(trade_plan.setup_details.get("structural_target_active")))
        self.assertEqual(
            trade_plan.setup_details.get("structural_target_source"),
            "swing_high_1h",
        )
        self.assertAlmostEqual(
            float(trade_plan.setup_details.get("structural_target_level", 0.0)),
            101.5,
            places=8,
        )

    def test_trade_planner_skips_structural_take_profit_without_structure(self) -> None:
        config = replace(
            self.config,
            pod_a=replace(
                self.config.pod_a,
                structural_targets=PodAStructuralTargetConfig(
                    enabled=True,
                    setups=["trend_pullback_long"],
                    require_structure_ready=True,
                    target_buffer_bps=6.0,
                    min_target_bps=25.0,
                    max_target_bps=220.0,
                ),
            ),
        )
        planner = AnchorTrendPlanner(config)
        allocation = CapitalAllocator(config).build_plan(
            Regime.TREND_EXPANSION,
            {
                PodName.POD_A: ["ETH"],
                PodName.POD_B: [],
                PodName.POD_C: [],
            },
        ).pod_allocations[PodName.POD_A]
        signal = self.service.evaluate(
            AnchorTrendContext(
                symbol="ETH",
                regime="TrendExpansion",
                price=100.0,
                ema_fast=99.4,
                ema_slow=98.0,
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
        self.assertFalse(bool(trade_plan.setup_details.get("structural_target_active")))

    def test_trade_planner_applies_structural_target_to_reversal_fade_short(self) -> None:
        config = replace(
            self.config,
            pod_a=replace(
                self.config.pod_a,
                reversal_fade=PodAReversalFadeConfig(
                    enabled=True,
                    allowed_regimes=["TrendExpansion"],
                    max_distance_from_resistance_bps=18.0,
                    min_target_to_support_bps=25.0,
                    min_trend_1h_bps=8.0,
                    min_trend_4h_bps=12.0,
                    min_rejection_flow=0.10,
                    min_stoch_rsi_k=0.72,
                    min_cci20=90.0,
                    max_vwap_reclaim_score=-0.05,
                ),
                structural_targets=PodAStructuralTargetConfig(
                    enabled=True,
                    setups=["reversal_fade_short"],
                    require_structure_ready=True,
                    target_buffer_bps=6.0,
                    min_target_bps=25.0,
                    max_target_bps=220.0,
                ),
            ),
        )
        service = AnchorTrendService(config)
        planner = AnchorTrendPlanner(config)
        allocation = CapitalAllocator(config).build_plan(
            Regime.TREND_EXPANSION,
            {
                PodName.POD_A: ["ETH"],
                PodName.POD_B: [],
                PodName.POD_C: [],
            },
        ).pod_allocations[PodName.POD_A]
        signal = service.evaluate(
            AnchorTrendContext(
                symbol="ETH",
                regime="TrendExpansion",
                price=101.4,
                ema_fast=101.6,
                ema_slow=100.8,
                vwap_distance_bps=4.0,
                structure_score=0.18,
                funding_rate=0.0,
                spread_bps=1.2,
                btc_aligned=True,
                book_imbalance=-0.22,
                trade_flow_bias=-0.18,
                bucket_range_bps=24.0,
                trend_1h_bps=18.0,
                trend_4h_bps=34.0,
                mtf_bias_score=24.0,
                candles_ready=True,
                structure_ready=True,
                range_high_1h=101.6,
                range_low_1h=100.3,
                swing_high_1h=101.5,
                swing_low_1h=101.06,
                ichimoku_bias_score=0.12,
                supertrend_direction=-1,
                stoch_rsi_k=0.84,
                cci20=122.0,
                vwap_reclaim_score=-0.11,
            )
        )

        assert signal is not None
        trade_plan = planner.build_trade_plan(signal, allocation)

        self.assertIsNotNone(trade_plan)
        assert trade_plan is not None
        self.assertEqual(trade_plan.setup, "reversal_fade_short")
        self.assertTrue(bool(trade_plan.setup_details.get("structural_target_active")))
        self.assertEqual(
            trade_plan.setup_details.get("structural_target_source"),
            "swing_low_1h",
        )
        self.assertAlmostEqual(trade_plan.take_profit_bps, 27.5306, places=3)

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

    def test_symbol_mode_can_allow_ichimoku_setup_outside_global_allowlist(self) -> None:
        config = replace(
            self.config,
            pod_a=replace(
                self.config.pod_a,
                symbol_modes={
                    "BIO": PodASymbolModeConfig(
                        enabled=True,
                        allowed_setups=["ichimoku_continuation_long"],
                        allowed_regimes=["TrendExpansion"],
                        min_confidence=0.65,
                    )
                },
            ),
        )
        gate = PodARiskGate(config)
        accepted_plan = TradePlan(
            symbol="BIO",
            side="long",
            setup="ichimoku_continuation_long",
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

        accepted = gate.evaluate_many([accepted_plan])[0]

        self.assertTrue(accepted.accepted)
        self.assertEqual(accepted.reason, "accepted")

    def test_pod_a_can_block_reserved_symbols_for_future_special_pod(self) -> None:
        config = replace(
            self.config,
            pod_a=replace(
                self.config.pod_a,
                blocked_symbols=["TAO", "XPL", "BIO"],
            ),
        )
        gate = PodARiskGate(config)
        blocked_plan = TradePlan(
            symbol="TAO",
            side="long",
            setup="trend_pullback_long",
            confidence=0.8,
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

        decision = gate.evaluate_many([blocked_plan])[0]

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "symbol_blocked")


if __name__ == "__main__":
    unittest.main()
