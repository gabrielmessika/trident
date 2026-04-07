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
                target_notional_usd=450.0,
                stop_bps=80.0,
                time_stop_hours=24,
                margin_usd=150.0,
                effective_leverage=3.0,
                risk_budget_usd=7.5,
                expected_loss_usd=3.6,
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
                target_notional_usd=450.0,
                stop_bps=80.0,
                time_stop_hours=24,
                margin_usd=150.0,
                effective_leverage=3.0,
                risk_budget_usd=7.5,
                expected_loss_usd=3.6,
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

    def test_upgrades_open_position_when_stronger_same_side_setup_arrives(self) -> None:
        executor = PodAExecutor(load_config("config/trident.toml"))
        initial = RiskDecision(
            accepted=True,
            reason="accepted",
            trade_plan=TradePlan(
                symbol="ETH",
                side="long",
                setup="trend_pullback_long",
                confidence=0.64,
                target_notional_usd=150.0,
                stop_bps=90.0,
                time_stop_hours=24,
                margin_usd=75.0,
                effective_leverage=2.0,
                risk_budget_usd=1.5,
                expected_loss_usd=1.35,
            ),
        )
        stronger = RiskDecision(
            accepted=True,
            reason="accepted",
            trade_plan=TradePlan(
                symbol="ETH",
                side="long",
                setup="liquidity_sweep_reclaim_long",
                confidence=0.91,
                target_notional_usd=180.0,
                stop_bps=80.0,
                time_stop_hours=24,
                margin_usd=60.0,
                effective_leverage=3.0,
                risk_budget_usd=1.5,
                expected_loss_usd=1.44,
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
            risk_decisions=[initial],
            signal_sides_by_symbol={"ETH": "long"},
            timestamp="2026-04-04T00:00:00Z",
        )

        batch = executor.process_record(
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="ETH",
                    price=3010.0,
                    ema_fast=3005.0,
                    ema_slow=2970.0,
                    vwap_distance_bps=-4.0,
                    structure_score=0.72,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                )
            ],
            risk_decisions=[stronger],
            signal_sides_by_symbol={"ETH": "long"},
            timestamp="2026-04-04T00:15:00Z",
        )

        self.assertEqual(batch.opened_symbols, ["ETH"])
        self.assertEqual(batch.skipped_open_symbols, [])
        self.assertEqual(len(batch.closed_trades), 1)
        self.assertEqual(batch.closed_trades[0].close_reason, "upgrade_setup")
        self.assertEqual(batch.close_reasons_by_symbol["ETH"], "upgrade_setup")
        self.assertEqual(len(batch.fills), 2)
        self.assertEqual(executor.portfolio.open_positions["ETH"].setup, "liquidity_sweep_reclaim_long")

    def test_closes_position_when_symbol_is_no_longer_allowed(self) -> None:
        executor = PodAExecutor(load_config("config/trident.toml"))
        decision = RiskDecision(
            accepted=True,
            reason="accepted",
            trade_plan=TradePlan(
                symbol="ETH",
                side="long",
                setup="trend_pullback_long",
                confidence=0.62,
                target_notional_usd=450.0,
                stop_bps=80.0,
                time_stop_hours=24,
                margin_usd=150.0,
                effective_leverage=3.0,
                risk_budget_usd=7.5,
                expected_loss_usd=3.6,
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
            allowed_symbols={"ETH"},
        )

        batch = executor.process_record(
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="ETH",
                    price=3005.0,
                    ema_fast=2998.0,
                    ema_slow=2958.0,
                    vwap_distance_bps=-2.0,
                    structure_score=0.4,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                )
            ],
            risk_decisions=[],
            signal_sides_by_symbol={},
            timestamp="2026-04-04T00:15:00Z",
            allowed_symbols=set(),
        )

        self.assertEqual(len(batch.closed_trades), 1)
        self.assertEqual(batch.closed_trades[0].close_reason, "routing_revoked")
        self.assertEqual(batch.close_reasons_by_symbol["ETH"], "routing_revoked")
        self.assertFalse(batch.has_open_position_after["ETH"])

    def test_closes_on_take_profit_hit(self) -> None:
        executor = PodAExecutor(load_config("config/trident.toml"))
        decision = RiskDecision(
            accepted=True,
            reason="accepted",
            trade_plan=TradePlan(
                symbol="ETH",
                side="long",
                setup="trend_pullback_long",
                confidence=0.7,
                target_notional_usd=300.0,
                stop_bps=80.0,
                time_stop_hours=24,
                take_profit_bps=100.0,
                break_even_trigger_bps=50.0,
                trailing_activation_bps=80.0,
                trailing_distance_bps=30.0,
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
                    price=3032.0,
                    ema_fast=3015.0,
                    ema_slow=2985.0,
                    vwap_distance_bps=8.0,
                    structure_score=0.7,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                )
            ],
            risk_decisions=[],
            signal_sides_by_symbol={},
            timestamp="2026-04-04T00:30:00Z",
        )

        self.assertEqual(len(batch.closed_trades), 1)
        self.assertEqual(batch.closed_trades[0].close_reason, "take_profit_hit")
        self.assertGreater(batch.closed_trades[0].pnl_usd, 0)

    def test_closes_on_trailing_stop_after_favorable_move(self) -> None:
        executor = PodAExecutor(load_config("config/trident.toml"))
        decision = RiskDecision(
            accepted=True,
            reason="accepted",
            trade_plan=TradePlan(
                symbol="ETH",
                side="long",
                setup="trend_pullback_long",
                confidence=0.7,
                target_notional_usd=300.0,
                stop_bps=80.0,
                time_stop_hours=24,
                take_profit_bps=200.0,
                break_even_trigger_bps=50.0,
                trailing_activation_bps=80.0,
                trailing_distance_bps=30.0,
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

        warmup = executor.process_record(
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="ETH",
                    price=3027.0,
                    ema_fast=3010.0,
                    ema_slow=2980.0,
                    vwap_distance_bps=6.0,
                    structure_score=0.65,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                )
            ],
            risk_decisions=[],
            signal_sides_by_symbol={},
            timestamp="2026-04-04T00:20:00Z",
        )
        self.assertEqual(len(warmup.closed_trades), 0)

        batch = executor.process_record(
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="ETH",
                    price=3012.0,
                    ema_fast=3004.0,
                    ema_slow=2988.0,
                    vwap_distance_bps=2.0,
                    structure_score=0.45,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                )
            ],
            risk_decisions=[],
            signal_sides_by_symbol={},
            timestamp="2026-04-04T00:35:00Z",
        )

        self.assertEqual(len(batch.closed_trades), 1)
        self.assertEqual(batch.closed_trades[0].close_reason, "trailing_stop")
        self.assertGreater(batch.closed_trades[0].pnl_usd, 0)
