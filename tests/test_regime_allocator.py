import unittest

from app.settings import load_config
from app.trident.regime_allocator import RegimeAllocator
from app.trident.types import Regime, RegimeSnapshot


class RegimeAllocatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config("config/trident.toml")
        self.allocator = RegimeAllocator(self.config)

    def test_returns_cash_when_snapshot_not_ready(self) -> None:
        regime = self.allocator.classify(RegimeSnapshot(ready=False))
        self.assertEqual(regime, Regime.CASH)

    def test_classifies_dead_zone(self) -> None:
        regime = self.allocator.classify(
            RegimeSnapshot(
                ready=True,
                adx=10.0,
                atr_ratio=0.4,
                range_width_bps=50.0,
                structure_score=0.1,
            )
        )
        self.assertEqual(regime, Regime.DEAD_ZONE)

    def test_classifies_range_auction(self) -> None:
        regime = self.allocator.classify(
            RegimeSnapshot(
                ready=True,
                adx=16.0,
                atr_ratio=1.1,
                range_width_bps=130.0,
                structure_score=0.15,
            )
        )
        self.assertEqual(regime, Regime.RANGE_AUCTION)

    def test_classifies_trend_expansion(self) -> None:
        regime = self.allocator.classify(
            RegimeSnapshot(
                ready=True,
                adx=32.0,
                atr_ratio=1.2,
                range_width_bps=180.0,
                structure_score=0.55,
            )
        )
        self.assertEqual(regime, Regime.TREND_EXPANSION)

    def test_rejects_trend_expansion_when_breadth_is_weak(self) -> None:
        self.config.trident.regime.crypto_v2_enabled = True
        regime = self.allocator.classify(
            RegimeSnapshot(
                ready=True,
                adx=34.0,
                atr_ratio=1.3,
                range_width_bps=190.0,
                structure_score=0.62,
                symbol_count=6,
                active_symbol_count=6,
                aligned_symbol_count=2,
                breadth_pct=0.3333,
                alt_participation_pct=0.2,
                dispersion_pct=0.6667,
                leader_trend_score=0.74,
                coherence_score=0.42,
            )
        )
        self.assertEqual(regime, Regime.RANGE_AUCTION)

    def test_hybrid_mode_preserves_legacy_trend_when_v2_is_weaker(self) -> None:
        self.config.trident.regime.crypto_v2_enabled = True
        self.config.trident.regime.crypto_v2_mode = "hybrid_upgrade_only"
        regime = self.allocator.classify(
            RegimeSnapshot(
                ready=True,
                adx=34.0,
                atr_ratio=1.3,
                range_width_bps=190.0,
                structure_score=0.62,
                symbol_count=6,
                active_symbol_count=6,
                aligned_symbol_count=2,
                breadth_pct=0.3333,
                alt_participation_pct=0.2,
                dispersion_pct=0.6667,
                leader_trend_score=0.74,
                coherence_score=0.42,
            )
        )
        self.assertEqual(regime, Regime.TREND_EXPANSION)

    def test_hybrid_mode_can_upgrade_legacy_range_with_v2_specific_thresholds(self) -> None:
        self.config.trident.regime.crypto_v2_enabled = True
        self.config.trident.regime.crypto_v2_mode = "hybrid_upgrade_only"
        self.config.trident.regime.crypto_v2_adx_trend_threshold = 18.0
        self.config.trident.regime.crypto_v2_trend_structure_threshold = 0.24
        regime = self.allocator.classify(
            RegimeSnapshot(
                ready=True,
                adx=20.0,
                atr_ratio=1.05,
                range_width_bps=165.0,
                structure_score=0.26,
                symbol_count=5,
                active_symbol_count=5,
                aligned_symbol_count=5,
                breadth_pct=1.0,
                alt_participation_pct=1.0,
                dispersion_pct=0.0,
                leader_trend_score=0.82,
                coherence_score=0.86,
            )
        )
        self.assertEqual(regime, Regime.TREND_EXPANSION)

    def test_hybrid_mode_can_block_dead_zone_to_trend_upgrade(self) -> None:
        self.config.trident.regime.crypto_v2_enabled = True
        self.config.trident.regime.crypto_v2_mode = "hybrid_upgrade_only"
        self.config.trident.regime.crypto_v2_allow_dead_zone_to_trend_upgrade = False
        self.config.trident.regime.crypto_v2_adx_trend_threshold = 18.0
        self.config.trident.regime.crypto_v2_trend_structure_threshold = 0.24
        regime = self.allocator.classify(
            RegimeSnapshot(
                ready=True,
                adx=20.0,
                atr_ratio=0.40,
                range_width_bps=50.0,
                structure_score=0.26,
                symbol_count=5,
                active_symbol_count=5,
                aligned_symbol_count=5,
                breadth_pct=1.0,
                alt_participation_pct=1.0,
                dispersion_pct=0.0,
                leader_trend_score=0.82,
                coherence_score=0.86,
            )
        )
        self.assertEqual(regime, Regime.DEAD_ZONE)

    def test_classifies_panic_squeeze(self) -> None:
        regime = self.allocator.classify(
            RegimeSnapshot(
                ready=True,
                adx=22.0,
                atr_ratio=2.1,
                range_width_bps=240.0,
                structure_score=0.2,
                btc_impulse=True,
            )
        )
        self.assertEqual(regime, Regime.PANIC_SQUEEZE)

    def test_resolve_applies_hysteresis_for_dead_to_range_transition(self) -> None:
        dead_snapshot = RegimeSnapshot(
            ready=True,
            adx=10.0,
            atr_ratio=0.4,
            range_width_bps=50.0,
            structure_score=0.1,
        )
        range_snapshot = RegimeSnapshot(
            ready=True,
            adx=16.0,
            atr_ratio=1.1,
            range_width_bps=130.0,
            structure_score=0.15,
        )

        first = self.allocator.resolve(
            snapshot=dead_snapshot,
            current_regime=Regime.CASH,
            pending_regime=None,
            pending_count=0,
        )
        self.assertEqual(first.effective_regime, Regime.DEAD_ZONE)
        self.assertTrue(first.switched)

        second = self.allocator.resolve(
            snapshot=range_snapshot,
            current_regime=first.effective_regime,
            pending_regime=first.pending_regime,
            pending_count=first.pending_count,
        )
        self.assertEqual(second.effective_regime, Regime.DEAD_ZONE)
        self.assertEqual(second.pending_regime, Regime.RANGE_AUCTION)
        self.assertEqual(second.pending_count, 1)

        third = self.allocator.resolve(
            snapshot=range_snapshot,
            current_regime=second.effective_regime,
            pending_regime=second.pending_regime,
            pending_count=second.pending_count,
        )
        self.assertEqual(third.effective_regime, Regime.DEAD_ZONE)
        self.assertEqual(third.pending_count, 2)

        fourth = self.allocator.resolve(
            snapshot=range_snapshot,
            current_regime=third.effective_regime,
            pending_regime=third.pending_regime,
            pending_count=third.pending_count,
        )
        self.assertEqual(fourth.effective_regime, Regime.RANGE_AUCTION)
        self.assertTrue(fourth.switched)

    def test_resolve_enters_panic_immediately(self) -> None:
        panic_snapshot = RegimeSnapshot(
            ready=True,
            adx=22.0,
            atr_ratio=2.1,
            range_width_bps=240.0,
            structure_score=0.2,
            btc_impulse=True,
        )

        decision = self.allocator.resolve(
            snapshot=panic_snapshot,
            current_regime=Regime.DEAD_ZONE,
            pending_regime=None,
            pending_count=0,
        )

        self.assertEqual(decision.raw_regime, Regime.PANIC_SQUEEZE)
        self.assertEqual(decision.effective_regime, Regime.PANIC_SQUEEZE)
        self.assertTrue(decision.switched)


if __name__ == "__main__":
    unittest.main()
