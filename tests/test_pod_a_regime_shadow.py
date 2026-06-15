import unittest
from datetime import datetime, timedelta, timezone

from app.trident.pod_a.regime_shadow import (
    PodARegimeShadowTracker,
    build_regime_shadow_features,
    regime_shadow_details,
    signal_regime_shadow_details,
)
from app.trident.types import SymbolMarketSnapshot


class PodARegimeShadowTests(unittest.TestCase):
    def test_scores_defensive_transition_for_short_shadow(self) -> None:
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        features = build_regime_shadow_features(
            timestamp=now,
            symbol="ETH",
            snapshot=_snapshot("ETH", 96.0, ema_fast=97.0, ema_slow=100.0),
            btc_snapshot=_snapshot("BTC", 98.0, ema_fast=99.0, ema_slow=100.0),
            histories={},
            regime={"structure_score": 0.10},
        )

        self.assertEqual(features.bear_regime_score, 3)
        self.assertEqual(features.regime_gate_decision, "defensive")
        details = signal_regime_shadow_details(
            features,
            side="short",
            setup="trend_pullback_short",
        )

        self.assertTrue(details["would_open_defensive_short_shadow"])
        self.assertFalse(details["would_block_long"])
        self.assertTrue(details["live_action_unchanged"])

    def test_blocks_long_shadow_only_when_bear_score_is_strict(self) -> None:
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        features = build_regime_shadow_features(
            timestamp=now,
            symbol="ETH",
            snapshot=_snapshot("ETH", 96.0, ema_fast=97.0, ema_slow=100.0),
            btc_snapshot=_snapshot("BTC", 98.0, ema_fast=99.0, ema_slow=100.0),
            histories={},
            regime={"structure_score": 0.10, "breadth_pct": 0.30},
        )

        details = signal_regime_shadow_details(
            features,
            side="long",
            setup="trend_pullback_long",
        )

        self.assertGreaterEqual(features.bear_regime_score, 4)
        self.assertEqual(features.regime_gate_decision, "bearish")
        self.assertTrue(details["would_block_long"])
        self.assertFalse(details["would_open_defensive_short_shadow"])
        self.assertTrue(details["live_action_unchanged"])

    def test_tracker_uses_history_available_before_current_record(self) -> None:
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        tracker = PodARegimeShadowTracker()
        previous_snapshots = [
            _snapshot("BTC", 100.0, ema_fast=100.0, ema_slow=100.0),
            _snapshot("ETH", 100.0, ema_fast=100.0, ema_slow=100.0),
        ]
        tracker.observe(timestamp=now - timedelta(minutes=60), snapshots=previous_snapshots)

        features_by_symbol = tracker.evaluate(
            timestamp=now,
            snapshots=[
                _snapshot("BTC", 98.0, ema_fast=99.0, ema_slow=100.0),
                _snapshot("ETH", 96.0, ema_fast=97.0, ema_slow=100.0),
            ],
            regime_snapshot={"structure_score": 0.10},
        )

        self.assertLess(features_by_symbol["BTC"].btc_ret_60m_bps or 0.0, 0.0)
        self.assertLess(features_by_symbol["ETH"].symbol_ret_60m_bps or 0.0, 0.0)

    def test_missing_features_never_changes_live_action(self) -> None:
        details = regime_shadow_details(None, side="long")

        self.assertEqual(details["regime_gate_decision"], "missing_features")
        self.assertFalse(details["would_block_long"])
        self.assertFalse(details["would_open_defensive_short_shadow"])
        self.assertTrue(details["live_action_unchanged"])


def _snapshot(
    symbol: str,
    price: float,
    *,
    ema_fast: float,
    ema_slow: float,
) -> SymbolMarketSnapshot:
    return SymbolMarketSnapshot(
        symbol=symbol,
        price=price,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        vwap_distance_bps=0.0,
        structure_score=0.0,
        funding_rate=0.0,
        spread_bps=1.0,
        btc_aligned=True,
    )


if __name__ == "__main__":
    unittest.main()
