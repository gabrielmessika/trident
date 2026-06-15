import unittest
from datetime import datetime, timedelta, timezone

from scripts.run_p106_regime_gate_replay import (
    FeatureHistory,
    GateSpec,
    _gate_allows,
    build_features,
)
from app.trident.types import SymbolMarketSnapshot, TradePlan


class P106RegimeGateReplayTests(unittest.TestCase):
    def test_build_features_scores_bearish_pre_entry_regime(self) -> None:
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        histories = {
            "BTC": _history(now, [(1440, 105.0), (240, 101.5), (60, 100.5), (0, 98.0)]),
            "ETH": _history(now, [(240, 102.0), (60, 100.0), (0, 96.0)]),
        }
        btc = _snapshot("BTC", 98.0, ema_fast=98.5, ema_slow=100.0)
        eth = _snapshot("ETH", 96.0, ema_fast=96.5, ema_slow=100.0)

        features = build_features(
            timestamp=now,
            symbol="ETH",
            snapshot=eth,
            btc_snapshot=btc,
            histories=histories,
            regime={
                "structure_score": 0.10,
                "breadth_pct": 0.30,
                "alt_participation_pct": 0.25,
                "leader_trend_score": -0.08,
                "coherence_score": 0.20,
                "dispersion_pct": 0.70,
            },
        )

        self.assertGreaterEqual(features.bear_score, 6)
        self.assertLessEqual(features.bull_score, 1)
        self.assertEqual(features.regime_gate, "bearish")

    def test_gate_allows_only_matching_side(self) -> None:
        now = datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc)
        histories = {
            "BTC": _history(now, [(240, 101.5), (60, 100.5), (0, 98.0)]),
            "ETH": _history(now, [(240, 102.0), (60, 100.0), (0, 96.0)]),
        }
        features = build_features(
            timestamp=now,
            symbol="ETH",
            snapshot=_snapshot("ETH", 96.0, ema_fast=96.5, ema_slow=100.0),
            btc_snapshot=_snapshot("BTC", 98.0, ema_fast=98.5, ema_slow=100.0),
            histories=histories,
            regime={"structure_score": 0.10, "breadth_pct": 0.30, "leader_trend_score": -0.08},
        )
        spec = GateSpec(
            name="unit",
            description="unit",
            allow_longs=True,
            allow_shorts=True,
            long_min_bull=3,
            long_max_bear=2,
            short_min_bear=3,
            short_max_bull=2,
        )

        self.assertTrue(_gate_allows(spec, _plan("short"), features))
        self.assertFalse(_gate_allows(spec, _plan("long"), features))


def _history(now: datetime, points: list[tuple[int, float]]) -> FeatureHistory:
    history = FeatureHistory()
    for minutes_ago, price in sorted(points, reverse=True):
        history.append(now - timedelta(minutes=minutes_ago), price)
    return history


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


def _plan(side: str) -> TradePlan:
    return TradePlan(
        symbol="ETH",
        side=side,
        setup=f"trend_pullback_{side}",
        confidence=0.7,
        target_notional_usd=200.0,
        stop_bps=100.0,
        time_stop_hours=3,
    )


if __name__ == "__main__":
    unittest.main()
