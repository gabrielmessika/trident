import unittest
from dataclasses import replace

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

    def test_closes_immediately_on_opposite_signal_without_debounce(self) -> None:
        config = load_config("config/trident.toml")
        config = replace(
            config,
            pod_a=replace(config.pod_a, opposite_signal_debounce_minutes=0),
        )
        executor = PodAExecutor(config)
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
                    price=3005.0,
                    ema_fast=3000.0,
                    ema_slow=2955.0,
                    vwap_distance_bps=2.0,
                    structure_score=0.3,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                )
            ],
            risk_decisions=[],
            signal_sides_by_symbol={"ETH": "short"},
            timestamp="2026-04-04T00:10:00Z",
        )

        self.assertEqual(len(batch.closed_trades), 1)
        self.assertEqual(batch.closed_trades[0].close_reason, "opposite_signal")
        self.assertFalse(batch.has_open_position_after["ETH"])

    def test_keeps_position_during_opposite_signal_debounce_window(self) -> None:
        config = load_config("config/trident.toml")
        config = replace(
            config,
            pod_a=replace(config.pod_a, opposite_signal_debounce_minutes=15),
        )
        executor = PodAExecutor(config)
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
                    price=3005.0,
                    ema_fast=3000.0,
                    ema_slow=2955.0,
                    vwap_distance_bps=2.0,
                    structure_score=0.3,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                )
            ],
            risk_decisions=[],
            signal_sides_by_symbol={"ETH": "short"},
            timestamp="2026-04-04T00:10:00Z",
        )

        self.assertEqual(len(batch.closed_trades), 0)
        self.assertTrue(batch.had_open_position_before["ETH"])
        self.assertTrue(batch.has_open_position_after["ETH"])
        self.assertTrue(executor.portfolio.has_open_position("ETH"))

    def test_closes_position_after_opposite_signal_debounce_window(self) -> None:
        config = load_config("config/trident.toml")
        config = replace(
            config,
            pod_a=replace(config.pod_a, opposite_signal_debounce_minutes=15),
        )
        executor = PodAExecutor(config)
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

        executor.process_record(
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="ETH",
                    price=3005.0,
                    ema_fast=3000.0,
                    ema_slow=2955.0,
                    vwap_distance_bps=2.0,
                    structure_score=0.3,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                )
            ],
            risk_decisions=[],
            signal_sides_by_symbol={"ETH": "short"},
            timestamp="2026-04-04T00:10:00Z",
        )

        batch = executor.process_record(
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="ETH",
                    price=3008.0,
                    ema_fast=3002.0,
                    ema_slow=2958.0,
                    vwap_distance_bps=4.0,
                    structure_score=0.25,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                )
            ],
            risk_decisions=[],
            signal_sides_by_symbol={"ETH": "short"},
            timestamp="2026-04-04T00:26:00Z",
        )

        self.assertEqual(len(batch.closed_trades), 1)
        self.assertEqual(batch.closed_trades[0].close_reason, "opposite_signal")
        self.assertFalse(batch.has_open_position_after["ETH"])

    def test_keeps_crypto_trend_pullback_position_during_stop_grace_window(self) -> None:
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
                setup_details={"market_cluster": "crypto"},
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

        within_grace = executor.process_record(
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

        self.assertEqual(len(within_grace.closed_trades), 0)
        self.assertTrue(executor.portfolio.has_open_position("ETH"))

        after_grace = executor.process_record(
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
            timestamp="2026-04-04T03:00:00Z",
        )

        self.assertEqual(len(after_grace.closed_trades), 1)
        self.assertEqual(after_grace.closed_trades[0].close_reason, "stop_hit")

    def test_stop_grace_does_not_apply_outside_crypto_trend_pullback(self) -> None:
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
                setup_details={"market_cluster": "index"},
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

    def test_scales_into_campaign_position_once_when_repeated_signal_confirms(self) -> None:
        executor = PodAExecutor(load_config("config/trident.toml"))
        initial = RiskDecision(
            accepted=True,
            reason="accepted",
            trade_plan=TradePlan(
                symbol="ETH",
                side="long",
                setup="trend_pullback_long",
                confidence=0.74,
                target_notional_usd=280.0,
                stop_bps=160.0,
                time_stop_hours=36,
                take_profit_bps=0.0,
                break_even_trigger_bps=232.0,
                trailing_activation_bps=304.0,
                trailing_distance_bps=184.0,
                margin_usd=80.0,
                effective_leverage=3.5,
                risk_budget_usd=8.75,
                expected_loss_usd=4.48,
                setup_details={
                    "campaign_mode_active": True,
                    "routing_revoke_exempt": True,
                    "campaign_add_on_enabled": True,
                    "campaign_add_on_fraction": 0.3,
                    "campaign_add_on_trigger_bps": 20.0,
                    "campaign_add_on_min_confidence": 0.72,
                    "campaign_max_add_ons": 1,
                    "campaign_add_on_count": 0,
                    "campaign_base_target_notional_usd": 400.0,
                    "campaign_base_margin_usd": 114.285714,
                    "campaign_base_risk_budget_usd": 12.5,
                    "campaign_base_expected_loss_usd": 6.4,
                },
            ),
        )
        repeated = RiskDecision(
            accepted=True,
            reason="accepted",
            trade_plan=TradePlan(
                symbol="ETH",
                side="long",
                setup="trend_pullback_long",
                confidence=0.78,
                target_notional_usd=280.0,
                stop_bps=160.0,
                time_stop_hours=36,
                take_profit_bps=0.0,
                break_even_trigger_bps=232.0,
                trailing_activation_bps=304.0,
                trailing_distance_bps=184.0,
                margin_usd=80.0,
                effective_leverage=3.5,
                risk_budget_usd=8.75,
                expected_loss_usd=4.48,
                setup_details={
                    "campaign_mode_active": True,
                    "routing_revoke_exempt": True,
                    "campaign_add_on_enabled": True,
                    "campaign_add_on_fraction": 0.3,
                    "campaign_add_on_trigger_bps": 20.0,
                    "campaign_add_on_min_confidence": 0.72,
                    "campaign_max_add_ons": 1,
                    "campaign_add_on_count": 0,
                    "campaign_base_target_notional_usd": 400.0,
                    "campaign_base_margin_usd": 114.285714,
                    "campaign_base_risk_budget_usd": 12.5,
                    "campaign_base_expected_loss_usd": 6.4,
                },
            ),
        )

        executor.process_record(
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="ETH",
                    price=100.0,
                    ema_fast=99.0,
                    ema_slow=98.0,
                    vwap_distance_bps=-6.0,
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
                    price=101.0,
                    ema_fast=100.5,
                    ema_slow=99.0,
                    vwap_distance_bps=-3.0,
                    structure_score=0.72,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                )
            ],
            risk_decisions=[repeated],
            signal_sides_by_symbol={"ETH": "long"},
            timestamp="2026-04-04T00:15:00Z",
        )

        self.assertEqual(batch.opened_symbols, ["ETH"])
        self.assertEqual(len(batch.closed_trades), 0)
        position = executor.portfolio.open_positions["ETH"]
        self.assertAlmostEqual(position.target_notional_usd, 400.0, places=4)
        self.assertEqual(int(position.setup_details.get("campaign_add_on_count", 0)), 1)

        second_repeat = executor.process_record(
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="ETH",
                    price=101.5,
                    ema_fast=101.0,
                    ema_slow=99.5,
                    vwap_distance_bps=-2.0,
                    structure_score=0.74,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                )
            ],
            risk_decisions=[repeated],
            signal_sides_by_symbol={"ETH": "long"},
            timestamp="2026-04-04T00:30:00Z",
        )

        self.assertEqual(second_repeat.opened_symbols, [])
        self.assertEqual(second_repeat.skipped_open_symbols, ["ETH"])
        self.assertAlmostEqual(
            executor.portfolio.open_positions["ETH"].target_notional_usd,
            400.0,
            places=4,
        )

    def test_closes_position_when_symbol_is_no_longer_allowed(self) -> None:
        config = load_config("config/trident.toml")
        config = replace(
            config,
            trident=replace(
                config.trident,
                execution=replace(
                    config.trident.execution,
                    routing_revoke_grace_minutes=0,
                    routing_revoke_grace_minutes_by_symbol={},
                ),
            ),
        )
        executor = PodAExecutor(config)
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

    def test_keeps_position_during_routing_revoke_grace_window(self) -> None:
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

        self.assertEqual(len(batch.closed_trades), 0)
        self.assertTrue(batch.had_open_position_before["ETH"])
        self.assertTrue(batch.has_open_position_after["ETH"])

    def test_keeps_campaign_position_when_routing_is_revoked(self) -> None:
        executor = PodAExecutor(load_config("config/trident.toml"))
        decision = RiskDecision(
            accepted=True,
            reason="accepted",
            trade_plan=TradePlan(
                symbol="ETH",
                side="long",
                setup="trend_pullback_long",
                confidence=0.7,
                target_notional_usd=450.0,
                stop_bps=80.0,
                time_stop_hours=36,
                margin_usd=150.0,
                effective_leverage=3.0,
                risk_budget_usd=7.5,
                expected_loss_usd=3.6,
                setup_details={
                    "campaign_mode_active": True,
                    "routing_revoke_exempt": True,
                },
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
                    price=3008.0,
                    ema_fast=2998.0,
                    ema_slow=2958.0,
                    vwap_distance_bps=-1.0,
                    structure_score=0.45,
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

        self.assertEqual(len(batch.closed_trades), 0)
        self.assertTrue(batch.had_open_position_before["ETH"])
        self.assertTrue(batch.has_open_position_after["ETH"])

    def test_keeps_position_when_symbol_is_unassigned_but_still_managed(self) -> None:
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
            entry_allowed_symbols={"ETH"},
            managed_symbols={"ETH"},
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
            entry_allowed_symbols=set(),
            managed_symbols={"ETH"},
        )

        self.assertEqual(len(batch.closed_trades), 0)
        self.assertTrue(batch.has_open_position_after["ETH"])

    def test_blocks_new_open_when_symbol_is_not_entry_allowed_even_if_managed(self) -> None:
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
            entry_allowed_symbols=set(),
            managed_symbols={"ETH"},
        )

        self.assertEqual(batch.opened_symbols, [])
        self.assertEqual(batch.skipped_open_symbols, ["ETH"])
        self.assertFalse(batch.has_open_position_after["ETH"])

    def test_keeps_position_with_symbol_specific_routing_revoke_grace(self) -> None:
        config = load_config("config/trident.toml")
        config = replace(
            config,
            trident=replace(
                config.trident,
                execution=replace(
                    config.trident.execution,
                    routing_revoke_grace_minutes=0,
                    routing_revoke_grace_minutes_by_symbol={"ETH": 60},
                ),
            ),
        )
        executor = PodAExecutor(config)
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
            timestamp="2026-04-04T00:45:00Z",
            allowed_symbols=set(),
        )

        self.assertEqual(len(batch.closed_trades), 0)
        self.assertTrue(batch.has_open_position_after["ETH"])

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

    def test_closed_trade_preserves_trailing_and_break_even_fields(self) -> None:
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
                break_even_trigger_bps=56.0,
                trailing_activation_bps=80.0,
                trailing_distance_bps=44.0,
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

        closed, _ = executor.finalize(
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="ETH",
                    price=3010.0,
                    ema_fast=3005.0,
                    ema_slow=2970.0,
                    vwap_distance_bps=3.0,
                    structure_score=0.4,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                )
            ],
            timestamp="2026-04-04T01:00:00Z",
        )

        self.assertEqual(len(closed), 1)
        trade = closed[0]
        self.assertEqual(trade.trailing_activation_bps, 80.0)
        self.assertEqual(trade.trailing_distance_bps, 44.0)
        self.assertEqual(trade.break_even_trigger_bps, 56.0)
