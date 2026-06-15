import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.trident.pod_a.dynamic_symbol_guard import (
    PodADynamicSymbolGuard,
    falling_knife_score,
    symbol_guard_details,
)
from app.trident.pod_a.order_block_shadow import OrderBlockShadowFeatures
from app.trident.pod_a.regime_shadow import RegimeShadowFeatures
from app.trident.types import SymbolMarketSnapshot


class PodADynamicSymbolGuardTests(unittest.TestCase):
    def test_scores_falling_knife_from_regime_relative_weakness_and_order_block(self) -> None:
        score, reason, subscores = falling_knife_score(
            snapshot=_snapshot(
                "ETH",
                95.0,
                ema_fast=96.0,
                ema_slow=100.0,
                vwap_distance_bps=-45.0,
                spread_bps=8.0,
                bucket_range_bps=90.0,
            ),
            regime_features=_regime_features(
                gate="bearish",
                btc_ret_60=-30.0,
                symbol_ret_60=-120.0,
                symbol_ret_240=-220.0,
            ),
            order_block_features=_order_block_features("ETH", bearish=True),
        )

        self.assertGreaterEqual(score, 75.0)
        self.assertIn("regime", reason)
        self.assertEqual(subscores["order_block"], 15.0)

    def test_state_machine_persists_quarantine_and_exits_after_recovery(self) -> None:
        now = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "guard_state.json"
            guard = PodADynamicSymbolGuard(state_path)
            first = guard._transition(
                timestamp=now,
                symbol="ETH",
                score=80.0,
                reason="regime,relative_weakness",
                subscores={"regime": 20.0},
            )
            guard.save()

            self.assertEqual(first.state, "quarantine")
            self.assertTrue(first.would_block)
            reloaded = PodADynamicSymbolGuard(state_path)
            self.assertEqual(reloaded.states["ETH"].state, "quarantine")

            still_quarantined = reloaded._transition(
                timestamp=now + timedelta(minutes=30),
                symbol="ETH",
                score=30.0,
                reason="normal",
                subscores={},
            )
            exited = reloaded._transition(
                timestamp=now + timedelta(minutes=91),
                symbol="ETH",
                score=30.0,
                reason="normal",
                subscores={},
            )

            self.assertEqual(still_quarantined.state, "quarantine")
            self.assertEqual(exited.state, "normal")
            self.assertEqual(exited.quarantine_exit_reason, "score_recovered_for_60m")

    def test_details_are_observation_only_when_features_missing(self) -> None:
        details = symbol_guard_details(None)

        self.assertEqual(details["symbol_guard_shadow_mode"], "observation_only")
        self.assertEqual(details["symbol_guard_state"], "missing_features")
        self.assertFalse(details["would_block_dynamic_symbol_guard"])
        self.assertTrue(details["symbol_guard_live_action_unchanged"])


def _snapshot(
    symbol: str,
    price: float,
    *,
    ema_fast: float,
    ema_slow: float,
    vwap_distance_bps: float,
    spread_bps: float,
    bucket_range_bps: float,
) -> SymbolMarketSnapshot:
    return SymbolMarketSnapshot(
        symbol=symbol,
        price=price,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        vwap_distance_bps=vwap_distance_bps,
        structure_score=0.10,
        funding_rate=0.0,
        spread_bps=spread_bps,
        btc_aligned=False,
        bucket_range_bps=bucket_range_bps,
    )


def _regime_features(
    *,
    gate: str,
    btc_ret_60: float,
    symbol_ret_60: float,
    symbol_ret_240: float,
) -> RegimeShadowFeatures:
    return RegimeShadowFeatures(
        timestamp="2026-06-15T10:00:00Z",
        symbol="ETH",
        bull_regime_score=0,
        bear_regime_score=4,
        regime_gate_decision=gate,
        btc_ret_60m_bps=btc_ret_60,
        btc_ret_240m_bps=None,
        btc_ret_1440m_bps=None,
        symbol_ret_60m_bps=symbol_ret_60,
        symbol_ret_240m_bps=symbol_ret_240,
        btc_above_ema_slow=False,
        btc_fast_above_slow=False,
        symbol_above_ema_slow=False,
        symbol_fast_above_slow=False,
        structure_score=0.1,
        breadth_pct=0.2,
        alt_participation_pct=0.2,
        leader_trend_score=-0.1,
        coherence_score=None,
        dispersion_pct=None,
    )


def _order_block_features(symbol: str, *, bearish: bool) -> OrderBlockShadowFeatures:
    return OrderBlockShadowFeatures(
        timestamp="2026-06-15T10:00:00Z",
        symbol=symbol,
        bullish_order_blocks_1h4h=[],
        bearish_order_blocks_1h4h=["order_block_bear_retest:1h"] if bearish else [],
    )


if __name__ == "__main__":
    unittest.main()
