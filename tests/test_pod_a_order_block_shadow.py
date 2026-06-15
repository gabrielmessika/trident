import unittest

from app.trident.pod_a.order_block_shadow import (
    PodAOrderBlockShadowTracker,
    order_block_shadow_details,
    signal_order_block_shadow_details,
)
from app.trident.pod_a.regime_shadow import RegimeShadowFeatures
from app.trident.types import SymbolMarketSnapshot


class PodAOrderBlockShadowTests(unittest.TestCase):
    def test_detects_bullish_order_block_retest_on_1h(self) -> None:
        tracker = PodAOrderBlockShadowTracker()
        for timestamp, price in [
            ("2026-06-10T00:00:00Z", 100.0),
            ("2026-06-10T00:30:00Z", 98.0),
            ("2026-06-10T01:00:00Z", 99.0),
            ("2026-06-10T01:30:00Z", 104.0),
        ]:
            tracker.observe(timestamp=timestamp, snapshots=[_snapshot("ETH", price)])

        features = tracker.observe(
            timestamp="2026-06-10T02:00:00Z",
            snapshots=[_snapshot("ETH", 103.0)],
        )

        self.assertIn("order_block_bull_retest:1h", features["ETH"].bullish_order_blocks_1h4h)
        self.assertFalse(features["ETH"].bearish_order_blocks_1h4h)

    def test_detects_bearish_order_block_retest_on_1h(self) -> None:
        tracker = PodAOrderBlockShadowTracker()
        for timestamp, price in [
            ("2026-06-10T00:00:00Z", 100.0),
            ("2026-06-10T00:30:00Z", 102.0),
            ("2026-06-10T01:00:00Z", 101.0),
            ("2026-06-10T01:30:00Z", 96.0),
        ]:
            tracker.observe(timestamp=timestamp, snapshots=[_snapshot("ETH", price)])

        features = tracker.observe(
            timestamp="2026-06-10T02:00:00Z",
            snapshots=[_snapshot("ETH", 97.0)],
        )

        self.assertIn("order_block_bear_retest:1h", features["ETH"].bearish_order_blocks_1h4h)
        details = signal_order_block_shadow_details(
            features["ETH"],
            _regime_features("defensive"),
            side="short",
            setup="trend_pullback_short",
        )

        self.assertTrue(details["would_open_defensive_short_order_block_shadow"])
        self.assertFalse(details["would_block_long_order_block_shadow"])
        self.assertTrue(details["live_action_unchanged"])

    def test_missing_features_never_changes_live_action(self) -> None:
        details = order_block_shadow_details(
            None,
            None,
            side="long",
        )

        self.assertEqual(details["order_block_shadow_mode"], "observation_only")
        self.assertFalse(details["would_block_long_order_block_shadow"])
        self.assertFalse(details["would_open_defensive_short_order_block_shadow"])
        self.assertTrue(details["live_action_unchanged"])


def _snapshot(symbol: str, price: float) -> SymbolMarketSnapshot:
    return SymbolMarketSnapshot(
        symbol=symbol,
        price=price,
        ema_fast=price,
        ema_slow=price,
        vwap_distance_bps=0.0,
        structure_score=0.0,
        funding_rate=0.0,
        spread_bps=1.0,
        btc_aligned=True,
    )


def _regime_features(gate: str) -> RegimeShadowFeatures:
    return RegimeShadowFeatures(
        timestamp="2026-06-10T02:00:00Z",
        symbol="ETH",
        bull_regime_score=1,
        bear_regime_score=3,
        regime_gate_decision=gate,
        btc_ret_60m_bps=None,
        btc_ret_240m_bps=None,
        btc_ret_1440m_bps=None,
        symbol_ret_60m_bps=None,
        symbol_ret_240m_bps=None,
        btc_above_ema_slow=False,
        btc_fast_above_slow=False,
        symbol_above_ema_slow=False,
        symbol_fast_above_slow=False,
        structure_score=0.0,
        breadth_pct=None,
        alt_participation_pct=None,
        leader_trend_score=None,
        coherence_score=None,
        dispersion_pct=None,
    )


if __name__ == "__main__":
    unittest.main()
