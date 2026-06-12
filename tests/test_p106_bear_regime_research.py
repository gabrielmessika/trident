import unittest
from datetime import datetime, timedelta, timezone

from scripts.run_p106_bear_regime_research import (
    MarketPoint,
    bear_score,
    discover_patterns,
    simulate_pattern_trade,
    side_return_bps,
)


class P106BearRegimeResearchTests(unittest.TestCase):
    def test_bear_score_uses_pre_entry_features(self) -> None:
        btc = _point(
            "BTC",
            98.0,
            ema_slow=100.0,
            returns={60: -50.0, 240: -150.0},
            regime={
                "structure_score": 0.10,
                "breadth_pct": 0.30,
                "leader_trend_score": -0.08,
            },
        )
        symbol = _point(
            "ETH",
            95.0,
            ema_slow=100.0,
            returns={60: -25.0, 240: -130.0},
            regime=btc.regime,
        )

        self.assertGreaterEqual(bear_score(symbol, btc), 6)

    def test_short_simulation_profits_when_price_falls(self) -> None:
        entry = _point(
            "ETH",
            100.0,
            ema_slow=101.0,
            returns={60: -30.0, 240: -140.0},
        )
        exit_point = _point("ETH", 97.0, ts=entry.timestamp + timedelta(minutes=60))

        trade = simulate_pattern_trade(
            window_label="unit",
            pattern="short_downtrend_continuation",
            side="short",
            point=entry,
            future_points=[exit_point],
            btc_point=entry,
            horizon_minutes=180,
            notional_usd=200.0,
            stop_bps=120.0,
            take_profit_bps=240.0,
            round_trip_cost_bps=16.0,
        )

        self.assertIsNotNone(trade)
        assert trade is not None
        self.assertEqual(trade.close_reason, "take_profit")
        self.assertGreater(trade.pnl_usd, 0)
        self.assertGreater(side_return_bps(100.0, 97.0, "short"), 0)

    def test_discover_patterns_flags_bear_short_and_control_long(self) -> None:
        btc = _point(
            "BTC",
            98.0,
            ema_slow=100.0,
            returns={60: -50.0, 240: -150.0},
            regime={
                "structure_score": 0.10,
                "breadth_pct": 0.30,
                "leader_trend_score": -0.08,
            },
        )
        symbol = _point(
            "ETH",
            95.0,
            ema_fast=94.0,
            ema_slow=96.0,
            returns={60: -30.0, 240: -140.0},
            regime=btc.regime,
            vwap_distance_bps=-4.0,
            trade_flow_bias=-0.4,
            book_imbalance=-0.2,
        )

        patterns = discover_patterns(
            symbol,
            btc_point=btc,
            min_bucket_notional_usd=100.0,
            min_bucket_trade_count=3,
            max_spread_bps=10.0,
        )

        self.assertIn(("short_downtrend_continuation", "short"), patterns)
        self.assertIn(("short_flow_book_aligned", "short"), patterns)


def _point(
    symbol: str,
    price: float,
    *,
    ts: datetime | None = None,
    ema_fast: float | None = None,
    ema_slow: float | None = None,
    returns: dict[int, float] | None = None,
    regime: dict[str, float] | None = None,
    vwap_distance_bps: float = 0.0,
    trade_flow_bias: float = 0.0,
    book_imbalance: float = 0.0,
) -> MarketPoint:
    return MarketPoint(
        timestamp=ts or datetime(2026, 6, 10, 12, 0, tzinfo=timezone.utc),
        symbol=symbol,
        price=price,
        ema_fast=price if ema_fast is None else ema_fast,
        ema_slow=price if ema_slow is None else ema_slow,
        vwap_distance_bps=vwap_distance_bps,
        structure_score=0.0,
        spread_bps=1.0,
        book_imbalance=book_imbalance,
        trade_flow_bias=trade_flow_bias,
        bucket_notional_usd=1000.0,
        bucket_trade_count=20,
        regime=regime or {"structure_score": 0.1, "breadth_pct": 0.3, "leader_trend_score": -0.1},
        returns_bps=returns or {},
    )


if __name__ == "__main__":
    unittest.main()
