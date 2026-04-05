import unittest

from app.settings import load_config
from app.trident.capital_allocator import CapitalAllocator
from app.trident.pod_a import (
    AnchorTrendContext,
    AnchorTrendPlanner,
    AnchorTrendService,
    MarketContextService,
)
from app.trident.types import PodName, Regime, SymbolMarketSnapshot


class AnchorTrendServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = AnchorTrendService()
        self.context_service = MarketContextService()
        self.planner = AnchorTrendPlanner()

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
        config = load_config("config/trident.toml")
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
        trade_plan = self.planner.build_trade_plan(signal, allocation)

        self.assertIsNotNone(trade_plan)
        assert trade_plan is not None
        self.assertEqual(trade_plan.target_notional_usd, 150.0)
        self.assertEqual(trade_plan.time_stop_hours, 24)


if __name__ == "__main__":
    unittest.main()
