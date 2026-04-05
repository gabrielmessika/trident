import unittest

from app.backtest.pod_a_executor import PodAExecutor
from app.settings import load_config
from app.trident.types import RiskDecision, SymbolMarketSnapshot, TradePlan


class PodAExecutorTests(unittest.TestCase):
    def test_opens_and_closes_position_on_end_of_backtest(self) -> None:
        executor = PodAExecutor(load_config("config/trident.toml"))
        decision = RiskDecision(
            accepted=True,
            reason="accepted",
            trade_plan=TradePlan(
                symbol="ETH",
                side="long",
                setup="trend_pullback_long",
                confidence=0.62,
                target_notional_usd=150.0,
                stop_bps=80.0,
                time_stop_hours=24,
            ),
        )

        batch = executor.process_record(
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="ETH",
                    price=3000.0,
                    ema_fast=2990.0,
                    ema_slow=2950.0,
                    vwap_distance_bps=-5.0,
                    structure_score=0.6,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                )
            ],
            risk_decisions=[decision],
            signal_sides_by_symbol={"ETH": "long"},
            timestamp="2026-04-04T00:00:00Z",
        )

        self.assertEqual(batch.opened_symbols, ["ETH"])
        self.assertEqual(batch.skipped_open_symbols, [])
        self.assertEqual(batch.had_open_position_before["ETH"], False)
        self.assertEqual(batch.has_open_position_after["ETH"], True)
        self.assertEqual(len(batch.fills), 1)
        closed, fills = executor.finalize(
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="ETH",
                    price=3030.0,
                    ema_fast=3010.0,
                    ema_slow=2960.0,
                    vwap_distance_bps=3.0,
                    structure_score=0.4,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                )
            ],
            timestamp="2026-04-04T01:00:00Z",
        )

        self.assertEqual(len(fills), 1)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].close_reason, "end_of_backtest")
        self.assertGreater(closed[0].pnl_usd, 0)
        self.assertGreater(closed[0].fees_usd, 0)

    def test_closes_on_stop_hit(self) -> None:
        executor = PodAExecutor(load_config("config/trident.toml"))
        decision = RiskDecision(
            accepted=True,
            reason="accepted",
            trade_plan=TradePlan(
                symbol="ETH",
                side="long",
                setup="trend_pullback_long",
                confidence=0.62,
                target_notional_usd=150.0,
                stop_bps=80.0,
                time_stop_hours=24,
            ),
        )

        executor.process_record(
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="ETH",
                    price=3000.0,
                    ema_fast=2990.0,
                    ema_slow=2950.0,
                    vwap_distance_bps=-5.0,
                    structure_score=0.6,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                )
            ],
            risk_decisions=[decision],
            signal_sides_by_symbol={"ETH": "long"},
            timestamp="2026-04-04T00:00:00Z",
        )

        batch = executor.process_record(
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="ETH",
                    price=2970.0,
                    ema_fast=2980.0,
                    ema_slow=2940.0,
                    vwap_distance_bps=-20.0,
                    structure_score=0.1,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                )
            ],
            risk_decisions=[],
            signal_sides_by_symbol={},
            timestamp="2026-04-04T01:00:00Z",
        )

        self.assertEqual(len(batch.closed_trades), 1)
        self.assertEqual(batch.closed_trades[0].close_reason, "stop_hit")
        self.assertLess(batch.closed_trades[0].pnl_usd, 0)
        self.assertEqual(batch.skipped_open_symbols, [])
        self.assertEqual(batch.had_open_position_before["ETH"], True)
        self.assertEqual(batch.has_open_position_after["ETH"], False)
        self.assertEqual(batch.close_reasons_by_symbol["ETH"], "stop_hit")
        self.assertEqual(len(batch.fills), 1)
