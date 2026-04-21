import json
import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch

from app.settings import load_config, override_app_config
from app.trident.supervisor import TridentSupervisor
from app.trident.types import (
    ObservedSymbolStatus,
    PodName,
    Regime,
    RegimeSnapshot,
    SignalPreview,
    SymbolAllocation,
    SymbolMarketSnapshot,
    SymbolRoutingDecision,
)


class SupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config("config/trident.toml")

    def test_supervisor_observed_symbols_do_not_fallback_to_pod_a_symbols(self) -> None:
        self.config.hyperliquid.observation_universe = []
        self.config.hyperliquid.default_coins = []

        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident",
            mode="observation",
        )

        self.assertEqual(supervisor._observed_symbols(), [])

    def test_supervisor_filters_candidates_by_allowed_market_clusters(self) -> None:
        self.config.hyperliquid.observation_universe = ["BTC", "GLD", "SPY"]
        self.config.pod_a.enabled = True
        self.config.pod_b.enabled = True
        self.config.pod_c.enabled = True
        self.config.pod_a.allowed_market_clusters = ["crypto"]
        self.config.pod_b.allowed_market_clusters = ["gold"]
        self.config.pod_c.allowed_market_clusters = ["index"]

        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident",
            mode="observation",
        )
        supervisor.apply_regime_snapshot(
            RegimeSnapshot(
                ready=True,
                adx=24.0,
                atr_ratio=0.9,
                range_width_bps=100.0,
                structure_score=0.3,
            ),
            cluster_regime_snapshots={
                "index": RegimeSnapshot(
                    ready=True,
                    adx=30.0,
                    atr_ratio=1.0,
                    range_width_bps=120.0,
                    structure_score=0.35,
                ),
            },
        )
        supervisor.refresh_symbol_routing(
            [
                SymbolMarketSnapshot(
                    symbol="BTC",
                    price=68000.0,
                    ema_fast=68100.0,
                    ema_slow=67950.0,
                    vwap_distance_bps=-3.0,
                    structure_score=0.4,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                    book_imbalance=0.05,
                    trade_flow_bias=0.04,
                    bucket_volume=120.0,
                    bucket_trade_count=100,
                    bucket_range_bps=35.0,
                    market_cluster="crypto",
                ),
                SymbolMarketSnapshot(
                    symbol="GLD",
                    price=220.0,
                    ema_fast=220.4,
                    ema_slow=219.8,
                    vwap_distance_bps=-1.5,
                    structure_score=0.22,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=False,
                    book_imbalance=0.04,
                    trade_flow_bias=0.03,
                    bucket_volume=300.0,
                    bucket_trade_count=40,
                    bucket_range_bps=18.0,
                    market_cluster="gold",
                    cluster_aligned=True,
                    cluster_leader="GLD",
                ),
                SymbolMarketSnapshot(
                    symbol="SPY",
                    price=510.0,
                    ema_fast=511.2,
                    ema_slow=509.4,
                    vwap_distance_bps=-4.0,
                    structure_score=0.33,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=False,
                    book_imbalance=0.04,
                    trade_flow_bias=0.05,
                    bucket_volume=500.0,
                    bucket_trade_count=60,
                    bucket_range_bps=22.0,
                    market_cluster="index",
                    cluster_aligned=True,
                    cluster_leader="SPY",
                ),
            ]
        )

        snapshot = supervisor.snapshot()
        self.assertEqual(snapshot["pods"]["pod_a"]["candidate_symbols"], ["BTC"])
        self.assertEqual(snapshot["pods"]["pod_b"]["candidate_symbols"], ["GLD"])
        self.assertEqual(snapshot["pods"]["pod_c"]["candidate_symbols"], ["SPY"])

    def test_supervisor_keeps_active_symbol_in_managed_scope_when_unassigned(self) -> None:
        self.config.hyperliquid.observation_universe = ["BTC"]
        self.config.pod_a.enabled = True
        self.config.pod_b.enabled = False
        self.config.pod_c.enabled = False
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident",
            mode="observation",
        )

        self.assertEqual(
            supervisor.managed_symbols_for(PodName.POD_A, {"BTC"}),
            {"BTC"},
        )

    def test_supervisor_compacts_backtest_logs_into_summaries(self) -> None:
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident-full-bot-backtest",
            mode="observation",
        )
        supervisor._compact_tradable_pool_log = supervisor._new_compact_tradable_pool_log()
        supervisor._compact_routing_log = supervisor._new_compact_routing_log()
        supervisor._compact_pod_b_sync_log = supervisor._new_compact_pod_b_sync_log()

        with patch("app.trident.supervisor.logger.info") as mock_info:
            supervisor._log_tradable_pool_changes(
                previous_status_by_symbol={
                    "BTC": ObservedSymbolStatus(
                        symbol="BTC",
                        tradable=False,
                        reasons=["bucket_trade_count_below_min"],
                    )
                },
                current_status_by_symbol={
                    "BTC": ObservedSymbolStatus(symbol="BTC", tradable=True, reasons=[])
                },
            )
            supervisor._log_symbol_routing_changes(
                previous_owners={},
                decisions=[
                    SymbolRoutingDecision(
                        symbol="BTC",
                        owner=PodName.POD_B,
                        mode="dynamic_affinity",
                        reason="best_affinity:pod_b (0.86)",
                    )
                ],
                previous_conflict_count=0,
            )
            supervisor._log_pod_b_sync_changes(
                previous_status={},
                current_status={
                    "managed_symbols": ["BTC"],
                    "target_usd": 200.0,
                    "process_state": "config_rendered",
                    "last_sync_reason": "config_rendered",
                },
            )
            supervisor.flush_compact_logs()

        messages = [call.args[0] for call in mock_info.call_args_list]
        self.assertTrue(
            any("Supervisor tradable pool summary;" in message for message in messages)
        )
        self.assertTrue(
            any("Supervisor routing summary;" in message for message in messages)
        )
        self.assertTrue(
            any("Supervisor Pod B sync summary;" in message for message in messages)
        )
        self.assertFalse(
            any("Supervisor tradable pool changed;" in message for message in messages)
        )
        self.assertFalse(
            any("Supervisor symbol routing changed;" in message for message in messages)
        )
        self.assertFalse(
            any("Supervisor Pod B sync changed;" in message for message in messages)
        )

    def test_supervisor_logs_explicit_reason_when_routing_decision_disappears(self) -> None:
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident",
            mode="observation",
        )

        with patch("app.trident.supervisor.logger.info") as mock_info:
            supervisor._log_symbol_routing_changes(
                previous_owners={"BTC": PodName.POD_B},
                decisions=[],
                previous_conflict_count=0,
            )

        changes = mock_info.call_args.args[2]
        self.assertIn(
            "reason=routing_decision_missing_after_candidate_drop",
            changes[0],
        )
        self.assertNotIn("reason=unknown", changes[0])

    def test_supervisor_claims_symbols_for_enabled_pods(self) -> None:
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident",
            mode="observation",
        )

        snapshot = supervisor.snapshot()

        self.assertEqual(snapshot["pods"]["pod_a"]["owned_symbols"], [])
        self.assertEqual(snapshot["pods"]["pod_a"]["candidate_symbols"], [])
        self.assertEqual(snapshot["ownership_conflicts"], [])

    def test_supervisor_resolves_overlaps_using_priority_fallback(self) -> None:
        self.config.pod_b.enabled = True
        self.config.pod_c.enabled = True

        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident",
            mode="observation",
        )
        supervisor.apply_regime_snapshot(
            RegimeSnapshot(
                ready=True,
                adx=10.0,
                atr_ratio=0.6,
                range_width_bps=40.0,
                structure_score=0.05,
            )
        )
        supervisor.refresh_symbol_routing(
            [
                SymbolMarketSnapshot(
                    symbol="SOL",
                    price=180.0,
                    ema_fast=180.2,
                    ema_slow=180.0,
                    vwap_distance_bps=-1.0,
                    structure_score=0.04,
                    funding_rate=0.0,
                    spread_bps=1.2,
                    btc_aligned=True,
                    book_imbalance=0.02,
                    trade_flow_bias=0.02,
                    bucket_volume=1000.0,
                    bucket_trade_count=25,
                    bucket_range_bps=18.0,
                )
            ]
        )

        snapshot = supervisor.snapshot()

        self.assertEqual(snapshot["pods"]["pod_b"]["owned_symbols"], [])
        self.assertEqual(snapshot["pods"]["pod_a"]["owned_symbols"], ["SOL"])
        self.assertEqual(snapshot["pods"]["pod_c"]["owned_symbols"], [])
        self.assertEqual(snapshot["ownership_conflicts"], [])
        sol_routing = next(item for item in snapshot["symbol_routing"] if item["symbol"] == "SOL")
        self.assertEqual(sol_routing["mode"], "fallback_priority")

    def test_supervisor_exposes_capital_plan_and_regime_snapshot(self) -> None:
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident",
            mode="observation",
        )
        supervisor.apply_regime_snapshot(
            RegimeSnapshot(
                ready=True,
                adx=32.0,
                atr_ratio=1.2,
                range_width_bps=180.0,
                structure_score=0.55,
            )
        )
        supervisor.refresh_symbol_routing(
            [
                SymbolMarketSnapshot(
                    symbol="BTC",
                    price=68000.0,
                    ema_fast=68120.0,
                    ema_slow=67980.0,
                    vwap_distance_bps=-4.0,
                    structure_score=0.58,
                    funding_rate=0.0,
                    spread_bps=0.8,
                    btc_aligned=True,
                    book_imbalance=0.05,
                    trade_flow_bias=0.04,
                    bucket_volume=100.0,
                    bucket_trade_count=200,
                    bucket_range_bps=42.0,
                ),
                SymbolMarketSnapshot(
                    symbol="ETH",
                    price=3100.0,
                    ema_fast=3115.0,
                    ema_slow=3088.0,
                    vwap_distance_bps=-7.0,
                    structure_score=0.61,
                    funding_rate=0.0001,
                    spread_bps=1.0,
                    btc_aligned=True,
                    book_imbalance=0.08,
                    trade_flow_bias=0.06,
                    bucket_volume=120.0,
                    bucket_trade_count=180,
                    bucket_range_bps=46.0,
                ),
                SymbolMarketSnapshot(
                    symbol="SOL",
                    price=180.0,
                    ema_fast=181.5,
                    ema_slow=179.3,
                    vwap_distance_bps=-5.0,
                    structure_score=0.72,
                    funding_rate=0.0002,
                    spread_bps=1.6,
                    btc_aligned=True,
                    book_imbalance=0.07,
                    trade_flow_bias=0.06,
                    bucket_volume=200.0,
                    bucket_trade_count=160,
                    bucket_range_bps=48.0,
                ),
                SymbolMarketSnapshot(
                    symbol="HYPE",
                    price=22.0,
                    ema_fast=22.25,
                    ema_slow=21.85,
                    vwap_distance_bps=-9.0,
                    structure_score=0.66,
                    funding_rate=0.0002,
                    spread_bps=2.2,
                    btc_aligned=True,
                    book_imbalance=0.09,
                    trade_flow_bias=0.08,
                    bucket_volume=180.0,
                    bucket_trade_count=140,
                    bucket_range_bps=52.0,
                ),
            ]
        )

        snapshot = supervisor.snapshot()
        expected_pod_a_pct = self.config.trident.allocations.trend_expansion.pod_a
        expected_cash_pct = round(1.0 - expected_pod_a_pct, 6)
        expected_per_symbol_pct = round(expected_pod_a_pct / 4.0, 6)

        self.assertEqual(snapshot["regime"], "TrendExpansion")
        self.assertEqual(snapshot["capital_plan"]["regime"], "TrendExpansion")
        self.assertEqual(snapshot["capital_plan"]["total_equity_usd"], 1000.0)
        self.assertEqual(snapshot["pods"]["pod_a"]["target_pct"], expected_pod_a_pct)
        self.assertEqual(snapshot["pods"]["pod_b"]["target_pct"], 0.0)
        self.assertEqual(snapshot["capital_plan"]["cash_pct"], expected_cash_pct)
        self.assertEqual(
            snapshot["capital_plan"]["pods"]["pod_a"]["symbols"][0]["target_pct"],
            expected_per_symbol_pct,
        )

    def test_supervisor_previews_pod_a_signals(self) -> None:
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident",
            mode="observation",
        )
        supervisor.apply_regime_snapshot(
            RegimeSnapshot(
                ready=True,
                adx=32.0,
                atr_ratio=1.2,
                range_width_bps=180.0,
                structure_score=0.55,
            )
        )

        previews = supervisor.preview_pod_a_signals(
            [
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
                ),
                SymbolMarketSnapshot(
                    symbol="BTC",
                    price=68000.0,
                    ema_fast=67950.0,
                    ema_slow=67800.0,
                    vwap_distance_bps=-5.0,
                    structure_score=0.30,
                    funding_rate=0.0,
                    spread_bps=0.8,
                    btc_aligned=True,
                ),
            ]
        )

        self.assertEqual(len(previews), 1)
        self.assertEqual(previews[0].symbol, "ETH")
        self.assertEqual(supervisor.snapshot()["pod_a_signal_preview"][0]["symbol"], "ETH")
        self.assertTrue(supervisor.snapshot()["pod_a_signal_preview"][0]["reason_summary"])
        self.assertIn("setup_details", supervisor.snapshot()["pod_a_signal_preview"][0])
        self.assertTrue(supervisor.snapshot()["pod_a_signal_review"])

    def test_supervisor_builds_pod_a_trade_plans(self) -> None:
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident",
            mode="observation",
        )
        supervisor.apply_regime_snapshot(
            RegimeSnapshot(
                ready=True,
                adx=32.0,
                atr_ratio=1.2,
                range_width_bps=180.0,
                structure_score=0.55,
            )
        )

        plans = supervisor.build_pod_a_trade_plans(
            [
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
            ]
        )

        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].symbol, "ETH")
        self.assertEqual(plans[0].target_notional_usd, 781.25)
        self.assertEqual(plans[0].effective_leverage, 3.125)

    def test_supervisor_rebalances_pod_a_allocation_over_active_signals_on_small_wallet(self) -> None:
        config = override_app_config(
            self.config,
            reference_equity_usd=500.0,
            pod_a_default_leverage=2.0,
            pod_a_max_leverage=3.0,
        )
        supervisor = TridentSupervisor(
            config=config,
            profile="trident",
            mode="observation",
        )
        supervisor.state.regime = Regime.RANGE_AUCTION
        supervisor.capital_plan = supervisor._build_capital_plan()
        allocation = supervisor._pod_a_planning_allocation(
            [
                SignalPreview(
                    symbol="ETH",
                    side="long",
                    setup="vwap_reclaim_long",
                    confidence=0.82,
                )
            ]
        )

        self.assertEqual(allocation.target_usd, 50.0)
        self.assertEqual(len(allocation.symbols), 1)
        self.assertEqual(allocation.symbols[0].symbol, "ETH")
        self.assertEqual(allocation.symbols[0].target_usd, 50.0)

    def test_supervisor_filters_observation_only_symbols_out_of_pod_a(self) -> None:
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident",
            mode="observation",
        )
        supervisor.apply_regime_snapshot(
            RegimeSnapshot(
                ready=True,
                adx=32.0,
                atr_ratio=1.2,
                range_width_bps=180.0,
                structure_score=0.55,
            )
        )

        snapshots = [
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
            ),
            SymbolMarketSnapshot(
                symbol="TEST",
                price=0.18,
                ema_fast=0.179,
                ema_slow=0.175,
                vwap_distance_bps=-6.0,
                structure_score=0.70,
                funding_rate=0.0,
                spread_bps=1.5,
                btc_aligned=True,
            ),
        ]

        previews = supervisor.preview_pod_a_signals(snapshots)
        plans = supervisor.build_pod_a_trade_plans(snapshots)

        self.assertEqual([preview.symbol for preview in previews], ["ETH"])
        self.assertEqual([plan.symbol for plan in plans], ["ETH"])

    def test_supervisor_syncs_pod_b_runtime_status(self) -> None:
        self.config.pod_b.enabled = True
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident",
            mode="observation",
        )
        supervisor.apply_regime_snapshot(
            RegimeSnapshot(
                ready=True,
                adx=28.0,
                atr_ratio=1.0,
                range_width_bps=120.0,
                structure_score=0.35,
            )
        )
        supervisor.refresh_symbol_routing(
            [
                SymbolMarketSnapshot(
                    symbol="DOGE",
                    price=0.18,
                    ema_fast=0.1812,
                    ema_slow=0.1798,
                    vwap_distance_bps=10.0,
                    structure_score=0.28,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                    book_imbalance=0.22,
                    trade_flow_bias=0.25,
                    bucket_volume=5000.0,
                    bucket_trade_count=30,
                    bucket_notional_usd=900.0,
                    bucket_range_bps=20.0,
                    volume_ratio=1.6,
                    trade_count_ratio=1.4,
                    realized_vol_short_bps=7.0,
                ),
                SymbolMarketSnapshot(
                    symbol="XRP",
                    price=0.64,
                    ema_fast=0.6401,
                    ema_slow=0.64,
                    vwap_distance_bps=-1.0,
                    structure_score=0.02,
                    funding_rate=0.0,
                    spread_bps=1.1,
                    btc_aligned=True,
                    book_imbalance=0.01,
                    trade_flow_bias=0.01,
                    bucket_volume=4000.0,
                    bucket_trade_count=24,
                    bucket_range_bps=14.0,
                ),
            ]
        )

        snapshot = supervisor.snapshot()

        self.assertEqual(snapshot["pod_b_status"]["managed_symbols"], [])
        self.assertEqual(
            snapshot["pod_b_status"]["last_sync_reason"],
            "supervisor_planned_state",
        )
        self.assertEqual(
            snapshot["pod_b_status"]["status_path"],
            "logs/pod_b_live_status.json",
        )
        self.assertEqual(snapshot["pod_b_status"]["opening_symbols"], [])
        self.assertEqual(snapshot["pod_b_status"]["open_positions"], [])
        self.assertEqual(snapshot["pod_b_status"]["total_position_count"], 0)

    def test_supervisor_snapshot_strips_embedded_pod_b_supervisor(self) -> None:
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident",
            mode="observation",
        )
        runtime_payload = {
            "pod": "pod_b",
            "updated_at": "2999-01-01T00:00:00Z",
            "process_state": "running",
            "managed_symbols": ["BTC"],
            "opening_symbols": ["BTC"],
            "open_positions": [],
            "report": {"closed_trade_count": 1},
            "supervisor": {
                "regime": "TrendExpansion",
            },
        }

        with patch(
            "app.trident.supervisor.load_runtime_status",
            return_value=runtime_payload,
        ):
            snapshot = supervisor.snapshot()

        self.assertEqual(snapshot["pod_b_status"]["managed_symbols"], ["BTC"])
        self.assertNotIn("supervisor", snapshot["pod_b_status"])

    def test_supervisor_tracks_shadow_blocked_pod_b_signals(self) -> None:
        self.config.pod_b.enabled = True
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident",
            mode="observation",
        )
        supervisor.apply_regime_snapshot(
            RegimeSnapshot(
                ready=True,
                adx=28.0,
                atr_ratio=1.0,
                range_width_bps=120.0,
                structure_score=0.35,
                btc_impulse=True,
            )
        )

        snapshot = SymbolMarketSnapshot(
            symbol="AVAX",
            price=100.0,
            ema_fast=100.9,
            ema_slow=100.0,
            vwap_distance_bps=7.0,
            structure_score=0.36,
            funding_rate=0.0,
            spread_bps=1.2,
            btc_aligned=True,
            market_cluster="crypto",
            cluster_aligned=True,
            cluster_leader="BTC",
            book_imbalance=0.20,
            trade_flow_bias=0.22,
            bucket_trade_count=22,
            bucket_notional_usd=780.0,
            bucket_range_bps=34.0,
            delta_book_imbalance=0.15,
            delta_trade_flow_bias=0.24,
            volume_ratio=2.0,
            trade_count_ratio=1.8,
            realized_vol_short_bps=7.2,
            realized_vol_long_bps=4.1,
            compression_score=0.76,
            microprice_dislocation_bps=1.1,
        )

        with (
            patch.object(supervisor, "refresh_symbol_routing", return_value=None),
            patch.object(supervisor, "opening_symbols_for", return_value=set()),
            patch.object(supervisor, "owner_for_symbol", return_value=PodName.POD_A),
            patch.object(
                supervisor,
                "allocation_for_symbol",
                return_value=SymbolAllocation(
                    symbol="AVAX",
                    target_pct=0.05,
                    target_usd=500.0,
                    reason_summary="uniform_allocation",
                ),
            ),
        ):
            previews = supervisor.preview_pod_b_signals([snapshot])

        self.assertEqual(previews, [])
        reviews = supervisor.snapshot()["pod_b_signal_review"]
        self.assertEqual(len(reviews), 1)
        review = reviews[0]
        self.assertEqual(review["symbol"], "AVAX")
        self.assertEqual(review["status"], "shadow_blocked_by_routing")
        self.assertEqual(review["owner"], "pod_a")
        self.assertTrue(review["blocked_by_routing"])
        self.assertEqual(review["owner_allocation_target_usd"], 500.0)
        self.assertIn("shadow blocked by routing", review["reason_summary"])

    def test_supervisor_tracks_regime_history(self) -> None:
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident",
            mode="observation",
        )
        supervisor.apply_regime_snapshot(
            RegimeSnapshot(
                ready=True,
                adx=8.0,
                atr_ratio=0.2,
                range_width_bps=40.0,
                structure_score=0.05,
            )
        )
        supervisor.apply_regime_snapshot(
            RegimeSnapshot(
                ready=True,
                adx=30.0,
                atr_ratio=1.2,
                range_width_bps=180.0,
                structure_score=0.6,
            )
        )
        supervisor.apply_regime_snapshot(
            RegimeSnapshot(
                ready=True,
                adx=30.0,
                atr_ratio=1.2,
                range_width_bps=180.0,
                structure_score=0.6,
            )
        )

        snapshot = supervisor.snapshot()

        self.assertEqual(snapshot["regime_transition_count"], 2)
        self.assertEqual(snapshot["regime_evaluation_count"], 3)
        self.assertEqual(len(snapshot["regime_history"]), 2)
        self.assertEqual(snapshot["regime_history"][0]["previous_regime"], "Cash")
        self.assertEqual(snapshot["regime_history"][0]["new_regime"], "DeadZone")
        self.assertEqual(snapshot["regime_history"][1]["new_regime"], "TrendExpansion")
        self.assertEqual(snapshot["raw_regime"], "TrendExpansion")

    def test_supervisor_routes_symbols_dynamically_by_market_context(self) -> None:
        self.config.pod_b.enabled = True
        self.config.pod_c.enabled = True
        self.config.hyperliquid.observation_universe = [
            "BTC",
            "ETH",
            "SOL",
            "HYPE",
            "SPY",
            "DOGE",
            "XRP",
            "SUI",
        ]
        self.config.trident.allocations.trend_expansion.pod_a = 0.75
        self.config.trident.allocations.trend_expansion.pod_b = 0.15
        self.config.trident.allocations.trend_expansion.pod_c = 0.10
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident",
            mode="observation",
        )
        supervisor.apply_regime_snapshot(
            RegimeSnapshot(
                ready=True,
                adx=28.0,
                atr_ratio=1.1,
                range_width_bps=150.0,
                structure_score=0.45,
            ),
            cluster_regime_snapshots={
                "index": RegimeSnapshot(
                    ready=True,
                    adx=28.0,
                    atr_ratio=1.1,
                    range_width_bps=150.0,
                    structure_score=0.45,
                )
            },
        )

        supervisor.refresh_symbol_routing(
            [
                SymbolMarketSnapshot(
                    symbol="BTC",
                    price=68000.0,
                    ema_fast=68120.0,
                    ema_slow=67980.0,
                    vwap_distance_bps=-4.0,
                    structure_score=0.58,
                    funding_rate=0.0,
                    spread_bps=0.8,
                    btc_aligned=True,
                    book_imbalance=0.05,
                    trade_flow_bias=0.04,
                    bucket_range_bps=42.0,
                ),
                SymbolMarketSnapshot(
                    symbol="ETH",
                    price=3100.0,
                    ema_fast=3115.0,
                    ema_slow=3088.0,
                    vwap_distance_bps=-7.0,
                    structure_score=0.61,
                    funding_rate=0.0001,
                    spread_bps=1.0,
                    btc_aligned=True,
                    book_imbalance=0.08,
                    trade_flow_bias=0.06,
                    bucket_range_bps=46.0,
                ),
                SymbolMarketSnapshot(
                    symbol="SOL",
                    price=180.0,
                    ema_fast=181.5,
                    ema_slow=179.3,
                    vwap_distance_bps=-5.0,
                    structure_score=0.72,
                    funding_rate=0.0002,
                    spread_bps=1.6,
                    btc_aligned=True,
                    book_imbalance=0.07,
                    trade_flow_bias=0.06,
                    bucket_range_bps=48.0,
                ),
                SymbolMarketSnapshot(
                    symbol="HYPE",
                    price=22.0,
                    ema_fast=22.25,
                    ema_slow=21.85,
                    vwap_distance_bps=-9.0,
                    structure_score=0.66,
                    funding_rate=0.0002,
                    spread_bps=2.2,
                    btc_aligned=True,
                    book_imbalance=0.09,
                    trade_flow_bias=0.08,
                    bucket_range_bps=52.0,
                ),
                SymbolMarketSnapshot(
                    symbol="SPY",
                    price=5100.0,
                    ema_fast=5112.0,
                    ema_slow=5087.0,
                    vwap_distance_bps=-3.0,
                    structure_score=0.44,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                    book_imbalance=0.12,
                    trade_flow_bias=0.10,
                    bucket_volume=1.8,
                    bucket_trade_count=7,
                    bucket_range_bps=20.0,
                ),
                SymbolMarketSnapshot(
                    symbol="DOGE",
                    price=0.18,
                    ema_fast=0.1802,
                    ema_slow=0.1799,
                    vwap_distance_bps=-1.0,
                    structure_score=0.04,
                    funding_rate=0.0,
                    spread_bps=1.2,
                    btc_aligned=True,
                    book_imbalance=0.02,
                    trade_flow_bias=0.02,
                    bucket_range_bps=28.0,
                ),
                SymbolMarketSnapshot(
                    symbol="XRP",
                    price=0.64,
                    ema_fast=0.6405,
                    ema_slow=0.6398,
                    vwap_distance_bps=-1.5,
                    structure_score=0.08,
                    funding_rate=0.0,
                    spread_bps=1.4,
                    btc_aligned=True,
                    book_imbalance=0.03,
                    trade_flow_bias=0.02,
                    bucket_range_bps=34.0,
                ),
                SymbolMarketSnapshot(
                    symbol="SUI",
                    price=1.42,
                    ema_fast=1.421,
                    ema_slow=1.419,
                    vwap_distance_bps=-2.0,
                    structure_score=0.05,
                    funding_rate=0.0,
                    spread_bps=2.0,
                    btc_aligned=True,
                    book_imbalance=0.03,
                    trade_flow_bias=0.02,
                    bucket_range_bps=30.0,
                ),
            ]
        )

        snapshot = supervisor.snapshot()

        self.assertEqual(snapshot["ownership_conflicts"], [])
        self.assertEqual(snapshot["pods"]["pod_a"]["owned_symbols"], ["BTC", "DOGE", "ETH", "HYPE", "SOL", "SUI", "XRP"])
        self.assertEqual(snapshot["pods"]["pod_b"]["owned_symbols"], [])
        self.assertEqual(snapshot["pods"]["pod_c"]["owned_symbols"], ["SPY"])
        sui_routing = next(item for item in snapshot["symbol_routing"] if item["symbol"] == "SUI")
        btc_routing = next(item for item in snapshot["symbol_routing"] if item["symbol"] == "BTC")
        spy_routing = next(item for item in snapshot["symbol_routing"] if item["symbol"] == "SPY")
        self.assertEqual(btc_routing["owner"], "pod_a")
        self.assertEqual(sui_routing["owner"], "pod_a")
        self.assertEqual(sui_routing["mode"], "dynamic_affinity")
        self.assertEqual(spy_routing["owner"], "pod_c")
        self.assertEqual(spy_routing["mode"], "dynamic_affinity")

    def test_supervisor_routes_new_observation_symbol_without_static_pod_lists(self) -> None:
        self.config.pod_b.enabled = True
        self.config.pod_c.enabled = True
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident",
            mode="observation",
        )
        supervisor.apply_regime_snapshot(
            RegimeSnapshot(
                ready=True,
                adx=9.0,
                atr_ratio=0.55,
                range_width_bps=28.0,
                structure_score=0.03,
            )
        )
        supervisor.refresh_symbol_routing(
            [
                SymbolMarketSnapshot(
                    symbol="ADA",
                    price=0.45,
                    ema_fast=0.4501,
                    ema_slow=0.45,
                    vwap_distance_bps=-0.8,
                    structure_score=0.02,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                    book_imbalance=0.01,
                    trade_flow_bias=0.01,
                    bucket_volume=3000.0,
                    bucket_trade_count=20,
                    bucket_range_bps=14.0,
                )
            ]
        )

        snapshot = supervisor.snapshot()
        ada_routing = next(item for item in snapshot["symbol_routing"] if item["symbol"] == "ADA")

        self.assertEqual(snapshot["pods"]["pod_b"]["owned_symbols"], [])
        self.assertEqual(snapshot["pods"]["pod_a"]["owned_symbols"], ["ADA"])
        self.assertEqual(ada_routing["owner"], "pod_a")
        self.assertEqual(ada_routing["mode"], "fallback_priority")

    def test_supervisor_routing_uses_hysteresis_before_switching_owner(self) -> None:
        self.config.pod_c.enabled = True
        self.config.trident.routing.min_assign_score = 0.45
        self.config.trident.routing.min_hold_score = 0.35
        self.config.trident.routing.hysteresis_margin = 0.15
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident",
            mode="observation",
        )
        supervisor.apply_regime_snapshot(
            RegimeSnapshot(
                ready=True,
                adx=30.0,
                atr_ratio=1.15,
                range_width_bps=150.0,
                structure_score=0.55,
            )
        )

        supervisor.refresh_symbol_routing(
            [
                SymbolMarketSnapshot(
                    symbol="SOL",
                    price=180.0,
                    ema_fast=181.5,
                    ema_slow=179.3,
                    vwap_distance_bps=-5.0,
                    structure_score=0.72,
                    funding_rate=0.0002,
                    spread_bps=1.4,
                    btc_aligned=True,
                    book_imbalance=0.05,
                    trade_flow_bias=0.04,
                    bucket_range_bps=44.0,
                )
            ]
        )

        supervisor.refresh_symbol_routing(
            [
                SymbolMarketSnapshot(
                    symbol="SOL",
                    price=180.0,
                    ema_fast=180.55,
                    ema_slow=180.45,
                    vwap_distance_bps=-12.0,
                    structure_score=0.55,
                    funding_rate=0.0002,
                    spread_bps=1.4,
                    btc_aligned=True,
                    book_imbalance=0.20,
                    trade_flow_bias=0.18,
                    bucket_range_bps=120.0,
                )
            ]
        )

        snapshot = supervisor.snapshot()
        sol_routing = next(item for item in snapshot["symbol_routing"] if item["symbol"] == "SOL")

        self.assertEqual(sol_routing["owner"], "pod_a")
        self.assertEqual(sol_routing["mode"], "dynamic_hysteresis")
        self.assertIn("hysteresis_hold:pod_a", sol_routing["reason"])

    def test_supervisor_filters_low_quality_live_symbols_out_of_tradable_pool(self) -> None:
        self.config.pod_b.enabled = True
        self.config.hyperliquid.observation_universe = ["DOGE", "XRP", "ADA", "PAXG"]
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident",
            mode="observation",
        )
        supervisor.apply_regime_snapshot(
            RegimeSnapshot(
                ready=True,
                adx=12.0,
                atr_ratio=0.7,
                range_width_bps=60.0,
                structure_score=0.08,
            )
        )
        supervisor.refresh_symbol_routing(
            [
                SymbolMarketSnapshot(
                    symbol="DOGE",
                    price=0.18,
                    ema_fast=0.1801,
                    ema_slow=0.18,
                    vwap_distance_bps=-1.0,
                    structure_score=0.03,
                    funding_rate=0.0,
                    spread_bps=1.1,
                    btc_aligned=True,
                    book_imbalance=0.01,
                    trade_flow_bias=0.01,
                    bucket_volume=1200.0,
                    bucket_trade_count=5,
                    bucket_range_bps=10.0,
                    source="test_live",
                ),
                SymbolMarketSnapshot(
                    symbol="XRP",
                    price=0.64,
                    ema_fast=0.6401,
                    ema_slow=0.64,
                    vwap_distance_bps=-1.0,
                    structure_score=0.02,
                    funding_rate=0.0,
                    spread_bps=12.5,
                    btc_aligned=True,
                    book_imbalance=0.01,
                    trade_flow_bias=0.01,
                    bucket_volume=50.0,
                    bucket_trade_count=8,
                    bucket_range_bps=12.0,
                    source="test_live",
                ),
                SymbolMarketSnapshot(
                    symbol="ADA",
                    price=0.45,
                    ema_fast=0.4501,
                    ema_slow=0.45,
                    vwap_distance_bps=-0.8,
                    structure_score=0.02,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                    book_imbalance=0.01,
                    trade_flow_bias=0.01,
                    bucket_volume=2.0,
                    bucket_trade_count=2,
                    bucket_range_bps=14.0,
                    source="test_live",
                ),
                SymbolMarketSnapshot(
                    symbol="PAXG",
                    price=3200.0,
                    ema_fast=3200.5,
                    ema_slow=3199.5,
                    vwap_distance_bps=-0.4,
                    structure_score=0.02,
                    funding_rate=0.02,
                    spread_bps=1.0,
                    btc_aligned=True,
                    book_imbalance=0.01,
                    trade_flow_bias=0.01,
                    bucket_volume=40.0,
                    bucket_trade_count=10,
                    bucket_range_bps=9.0,
                    source="test_live",
                ),
            ]
        )

        snapshot = supervisor.snapshot()
        quality_by_symbol = {
            item["symbol"]: item for item in snapshot["observed_symbol_status"]
        }

        self.assertEqual(snapshot["tradable_pool"], ["DOGE"])
        self.assertEqual(snapshot["pods"]["pod_b"]["candidate_symbols"], ["DOGE"])
        self.assertEqual(snapshot["pods"]["pod_b"]["owned_symbols"], [])
        self.assertEqual(snapshot["pods"]["pod_a"]["owned_symbols"], ["DOGE"])
        self.assertEqual(quality_by_symbol["DOGE"]["reasons"], [])
        self.assertIn("spread_above_max", quality_by_symbol["XRP"]["reasons"])
        self.assertIn("bucket_notional_below_min", quality_by_symbol["ADA"]["reasons"])
        self.assertIn("bucket_trade_count_below_min", quality_by_symbol["ADA"]["reasons"])
        self.assertIn("funding_outlier", quality_by_symbol["PAXG"]["reasons"])

    def test_supervisor_keeps_globally_blocked_symbol_observed_but_not_tradable(self) -> None:
        self.config.hyperliquid.observation_universe = ["BTC", "TAO"]
        self.config.hyperliquid.tradable_blocked_symbols = ["TAO"]
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident",
            mode="observation",
        )
        supervisor.apply_regime_snapshot(
            RegimeSnapshot(
                ready=True,
                adx=28.0,
                atr_ratio=1.1,
                range_width_bps=160.0,
                structure_score=0.45,
            )
        )
        supervisor.refresh_symbol_routing(
            [
                SymbolMarketSnapshot(
                    symbol="BTC",
                    price=68000.0,
                    ema_fast=68100.0,
                    ema_slow=67950.0,
                    vwap_distance_bps=-3.0,
                    structure_score=0.4,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                    book_imbalance=0.05,
                    trade_flow_bias=0.04,
                    bucket_volume=120.0,
                    bucket_trade_count=100,
                    bucket_range_bps=35.0,
                    source="test_live",
                ),
                SymbolMarketSnapshot(
                    symbol="TAO",
                    price=245.0,
                    ema_fast=246.0,
                    ema_slow=244.0,
                    vwap_distance_bps=-4.0,
                    structure_score=0.52,
                    funding_rate=0.0003,
                    spread_bps=1.5,
                    btc_aligned=True,
                    book_imbalance=0.09,
                    trade_flow_bias=0.08,
                    bucket_volume=250.0,
                    bucket_trade_count=30,
                    bucket_range_bps=90.0,
                    source="test_live",
                ),
            ]
        )

        snapshot = supervisor.snapshot()
        quality_by_symbol = {
            item["symbol"]: item for item in snapshot["observed_symbol_status"]
        }

        self.assertEqual(snapshot["observation_universe"], ["BTC", "TAO"])
        self.assertEqual(snapshot["tradable_pool"], ["BTC"])
        self.assertEqual(snapshot["pods"]["pod_a"]["candidate_symbols"], ["BTC"])
        self.assertFalse(quality_by_symbol["TAO"]["tradable"])
        self.assertEqual(quality_by_symbol["TAO"]["reasons"], ["symbol_blocked"])

    def test_supervisor_exposes_local_regime_by_symbol(self) -> None:
        self.config.pod_b.enabled = True
        self.config.pod_c.enabled = True
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident",
            mode="observation",
        )
        supervisor.apply_regime_snapshot(
            RegimeSnapshot(
                ready=True,
                adx=30.0,
                atr_ratio=1.1,
                range_width_bps=140.0,
                structure_score=0.5,
            )
        )
        supervisor.refresh_symbol_routing(
            [
                SymbolMarketSnapshot(
                    symbol="SOL",
                    price=180.0,
                    ema_fast=181.5,
                    ema_slow=179.3,
                    vwap_distance_bps=-5.0,
                    structure_score=0.72,
                    funding_rate=0.0002,
                    spread_bps=1.4,
                    btc_aligned=True,
                    book_imbalance=0.05,
                    trade_flow_bias=0.04,
                    bucket_range_bps=44.0,
                )
            ]
        )

        snapshot = supervisor.snapshot()
        sol_state = next(
            item for item in snapshot["local_regime_by_symbol"] if item["symbol"] == "SOL"
        )

        self.assertEqual(sol_state["local_regime"], "TrendStructure")
        self.assertEqual(sol_state["global_alignment"], "aligned")
        self.assertEqual(sol_state["owner"], "pod_a")
        self.assertEqual(sol_state["reassignment_count"], 0)
        self.assertIn("pod_a", sol_state["pod_scores"])

    def test_supervisor_tracks_local_regime_transitions(self) -> None:
        self.config.pod_c.enabled = True
        self.config.trident.routing.min_assign_score = 0.45
        self.config.trident.routing.min_hold_score = 0.35
        self.config.trident.routing.hysteresis_margin = 0.15
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident",
            mode="observation",
        )
        supervisor.apply_regime_snapshot(
            RegimeSnapshot(
                ready=True,
                adx=30.0,
                atr_ratio=1.15,
                range_width_bps=150.0,
                structure_score=0.55,
            )
        )
        supervisor.refresh_symbol_routing(
            [
                SymbolMarketSnapshot(
                    symbol="SOL",
                    price=180.0,
                    ema_fast=181.5,
                    ema_slow=179.3,
                    vwap_distance_bps=-5.0,
                    structure_score=0.72,
                    funding_rate=0.0002,
                    spread_bps=1.4,
                    btc_aligned=True,
                    book_imbalance=0.05,
                    trade_flow_bias=0.04,
                    bucket_range_bps=44.0,
                )
            ]
        )
        supervisor.refresh_symbol_routing(
            [
                SymbolMarketSnapshot(
                    symbol="SOL",
                    price=180.0,
                    ema_fast=180.55,
                    ema_slow=180.45,
                    vwap_distance_bps=-12.0,
                    structure_score=0.55,
                    funding_rate=0.0002,
                    spread_bps=1.4,
                    btc_aligned=True,
                    book_imbalance=0.20,
                    trade_flow_bias=0.18,
                    bucket_range_bps=120.0,
                )
            ]
        )

        snapshot = supervisor.snapshot()
        sol_transitions = [
            item
            for item in snapshot["local_regime_transitions"]
            if item["symbol"] == "SOL"
        ]

        self.assertGreaterEqual(len(sol_transitions), 2)
        self.assertEqual(sol_transitions[-1]["previous_local_regime"], "TrendStructure")
        self.assertEqual(sol_transitions[-1]["new_local_regime"], "EventImpulse")
        self.assertEqual(snapshot["symbol_reassignment_count_by_symbol"].get("SOL", 0), 0)

    def test_supervisor_applies_reassignment_cooldown_after_owner_switch(self) -> None:
        self.config.pod_b.enabled = True
        self.config.trident.allocations.trend_expansion.pod_a = 0.75
        self.config.trident.allocations.trend_expansion.pod_b = 0.25
        self.config.trident.allocations.range_auction.pod_a = 0.15
        self.config.trident.allocations.range_auction.pod_b = 0.85
        self.config.trident.routing.reassignment_cooldown_seconds = 300
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident",
            mode="observation",
        )
        supervisor.apply_regime_snapshot(
            RegimeSnapshot(
                ready=True,
                adx=30.0,
                atr_ratio=1.15,
                range_width_bps=150.0,
                structure_score=0.55,
            )
        )
        range_snapshot = SymbolMarketSnapshot(
            symbol="SOL",
            price=180.0,
            ema_fast=180.1,
            ema_slow=180.0,
            vwap_distance_bps=-0.3,
            structure_score=0.02,
            funding_rate=0.0,
            spread_bps=1.0,
            btc_aligned=True,
            book_imbalance=0.01,
            trade_flow_bias=0.01,
            bucket_volume=800.0,
            bucket_trade_count=20,
            bucket_range_bps=10.0,
        )
        trend_snapshot = SymbolMarketSnapshot(
            symbol="SOL",
            price=185.0,
            ema_fast=186.5,
            ema_slow=183.0,
            vwap_distance_bps=-5.0,
            structure_score=0.65,
            funding_rate=0.0001,
            spread_bps=0.8,
            btc_aligned=True,
            book_imbalance=0.15,
            trade_flow_bias=0.12,
            bucket_volume=2500.0,
            bucket_trade_count=80,
            bucket_range_bps=45.0,
        )

        supervisor.refresh_symbol_routing([range_snapshot])
        supervisor.refresh_symbol_routing([trend_snapshot])
        supervisor.refresh_symbol_routing([range_snapshot])

        snapshot = supervisor.snapshot()
        sol_routing = next(item for item in snapshot["symbol_routing"] if item["symbol"] == "SOL")

        self.assertIn(sol_routing["owner"], ("pod_a", "pod_b"))
        self.assertIn(sol_routing["mode"], ("dynamic_cooldown", "dynamic_hysteresis"))
        self.assertGreaterEqual(snapshot["symbol_reassignment_count_by_symbol"].get("SOL", 0), 0)

    def test_supervisor_applies_symbol_reassignment_debounce_when_global_cooldown_is_disabled(self) -> None:
        self.config.pod_b.enabled = True
        self.config.trident.allocations.trend_expansion.pod_a = 0.75
        self.config.trident.allocations.trend_expansion.pod_b = 0.25
        self.config.trident.allocations.range_auction.pod_a = 0.15
        self.config.trident.allocations.range_auction.pod_b = 0.85
        self.config.trident.routing.reassignment_cooldown_seconds = 0
        self.config.trident.routing.reassignment_debounce_min_score = 0.0
        self.config.trident.routing.reassignment_debounce_seconds_by_symbol = {"SOL": 3600}
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident",
            mode="observation",
        )
        supervisor.apply_regime_snapshot(
            RegimeSnapshot(
                ready=True,
                adx=30.0,
                atr_ratio=1.15,
                range_width_bps=150.0,
                structure_score=0.55,
            )
        )
        range_snapshot = SymbolMarketSnapshot(
            symbol="SOL",
            price=180.0,
            ema_fast=180.1,
            ema_slow=180.0,
            vwap_distance_bps=-0.3,
            structure_score=0.02,
            funding_rate=0.0,
            spread_bps=1.0,
            btc_aligned=True,
            book_imbalance=0.01,
            trade_flow_bias=0.01,
            bucket_volume=800.0,
            bucket_trade_count=20,
            bucket_range_bps=10.0,
        )
        trend_snapshot = SymbolMarketSnapshot(
            symbol="SOL",
            price=185.0,
            ema_fast=186.5,
            ema_slow=183.0,
            vwap_distance_bps=-5.0,
            structure_score=0.65,
            funding_rate=0.0001,
            spread_bps=0.8,
            btc_aligned=True,
            book_imbalance=0.15,
            trade_flow_bias=0.12,
            bucket_volume=2500.0,
            bucket_trade_count=80,
            bucket_range_bps=45.0,
        )

        supervisor.refresh_symbol_routing([range_snapshot])
        supervisor.refresh_symbol_routing([trend_snapshot])
        supervisor.refresh_symbol_routing([range_snapshot])

        snapshot = supervisor.snapshot()
        sol_routing = next(item for item in snapshot["symbol_routing"] if item["symbol"] == "SOL")

        self.assertIn(sol_routing["owner"], ("pod_a", "pod_b"))
        self.assertIn(sol_routing["mode"], ("dynamic_debounce", "dynamic_hysteresis"))
        self.assertGreaterEqual(snapshot["symbol_reassignment_count_by_symbol"].get("SOL", 0), 0)

    def test_supervisor_applies_symbol_routing_override_to_pod_c(self) -> None:
        self.config.pod_c.enabled = True
        self.config.trident.allocations.trend_expansion.pod_a = 0.75
        self.config.trident.allocations.trend_expansion.pod_b = 0.15
        self.config.trident.allocations.trend_expansion.pod_c = 0.10
        self.config.trident.routing.symbol_pod_overrides["BTC"] = "pod_c"
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident",
            mode="observation",
        )
        supervisor.apply_regime_snapshot(
            RegimeSnapshot(
                ready=True,
                adx=34.0,
                atr_ratio=1.25,
                range_width_bps=180.0,
                structure_score=0.62,
            )
        )
        supervisor.refresh_symbol_routing(
            [
                SymbolMarketSnapshot(
                    symbol="BTC",
                    price=68000.0,
                    ema_fast=68180.0,
                    ema_slow=67920.0,
                    vwap_distance_bps=-4.0,
                    structure_score=0.66,
                    funding_rate=0.0001,
                    spread_bps=0.8,
                    btc_aligned=True,
                    book_imbalance=0.07,
                    trade_flow_bias=0.05,
                    bucket_volume=120.0,
                    bucket_trade_count=220,
                    bucket_range_bps=55.0,
                )
            ]
        )

        snapshot = supervisor.snapshot()
        btc_routing = next(item for item in snapshot["symbol_routing"] if item["symbol"] == "BTC")
        btc_state = next(
            item for item in snapshot["local_regime_by_symbol"] if item["symbol"] == "BTC"
        )
        btc_ownership = next(
            item for item in snapshot["symbol_ownership"] if item["symbol"] == "BTC"
        )

        self.assertEqual(snapshot["pods"]["pod_c"]["candidate_symbols"], ["BTC"])
        self.assertEqual(snapshot["pods"]["pod_c"]["owned_symbols"], ["BTC"])
        self.assertEqual(btc_routing["owner"], "pod_c")
        self.assertEqual(btc_routing["mode"], "manual_override")
        self.assertTrue(btc_routing["override_active"])
        self.assertEqual(btc_routing["override_owner"], "pod_c")
        self.assertIn("manual_override", btc_routing["reason"])
        self.assertTrue(btc_state["override_active"])
        self.assertEqual(btc_state["override_owner"], "pod_c")
        self.assertTrue(btc_ownership["override_active"])
        self.assertEqual(btc_ownership["override_owner"], "pod_c")

    def test_supervisor_builds_pod_c_allocations_from_cluster_budgets(self) -> None:
        self.config.pod_a.enabled = False
        self.config.pod_b.enabled = False
        self.config.pod_c.enabled = True
        self.config.hyperliquid.observation_universe = ["GLD", "SPY"]
        self.config.pod_c.allowed_market_clusters = ["gold", "index"]
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident",
            mode="observation",
        )

        supervisor.apply_regime_snapshot(
            RegimeSnapshot(
                ready=True,
                adx=35.0,
                atr_ratio=2.1,
                range_width_bps=220.0,
                structure_score=0.7,
                btc_impulse=True,
            ),
            cluster_regime_snapshots={
                "gold": RegimeSnapshot(
                    ready=True,
                    adx=28.0,
                    atr_ratio=1.1,
                    range_width_bps=120.0,
                    structure_score=0.6,
                    btc_impulse=False,
                ),
                "index": RegimeSnapshot(
                    ready=True,
                    adx=8.0,
                    atr_ratio=0.3,
                    range_width_bps=40.0,
                    structure_score=0.0,
                    btc_impulse=False,
                ),
            },
        )
        supervisor.refresh_symbol_routing(
            [
                SymbolMarketSnapshot(
                    symbol="GLD",
                    price=245.0,
                    ema_fast=244.0,
                    ema_slow=241.0,
                    vwap_distance_bps=-5.0,
                    structure_score=0.65,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=False,
                    market_cluster="gold",
                    bucket_volume=1000.0,
                    bucket_trade_count=20,
                    bucket_range_bps=30.0,
                ),
                SymbolMarketSnapshot(
                    symbol="SPY",
                    price=510.0,
                    ema_fast=510.1,
                    ema_slow=510.0,
                    vwap_distance_bps=-0.2,
                    structure_score=0.01,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=False,
                    market_cluster="index",
                    bucket_volume=1000.0,
                    bucket_trade_count=20,
                    bucket_range_bps=8.0,
                ),
            ]
        )

        pod_c = supervisor.capital_plan.pod_allocations[PodName.POD_C]
        self.assertEqual(pod_c.target_pct, 0.15)
        self.assertEqual([item.symbol for item in pod_c.symbols], ["GLD"])
        self.assertEqual(supervisor.allowed_symbols_for(PodName.POD_C), {"GLD"})
        snapshot = supervisor.snapshot()
        self.assertEqual(snapshot["pods"]["pod_c"]["candidate_symbols"], ["GLD"])
        self.assertEqual(snapshot["pods"]["pod_c"]["owned_symbols"], ["GLD"])
        self.assertEqual(
            snapshot["cluster_target_allocations"],
            {"gold": 0.15},
        )

    def test_supervisor_reloads_runtime_symbol_override_without_redeploy(self) -> None:
        self.config.pod_b.enabled = True
        with tempfile.TemporaryDirectory() as temp_dir:
            override_path = Path(temp_dir) / "runtime_routing_overrides.json"
            self.config.trident.routing.runtime_override_path = str(override_path)
            supervisor = TridentSupervisor(
                config=self.config,
                profile="trident",
                mode="observation",
            )
            supervisor.apply_regime_snapshot(
                RegimeSnapshot(
                    ready=True,
                    adx=30.0,
                    atr_ratio=1.1,
                    range_width_bps=140.0,
                    structure_score=0.5,
                )
            )
            trend_snapshot = SymbolMarketSnapshot(
                symbol="SOL",
                price=180.0,
                ema_fast=181.5,
                ema_slow=179.3,
                vwap_distance_bps=-5.0,
                structure_score=0.72,
                funding_rate=0.0002,
                spread_bps=1.4,
                btc_aligned=True,
                book_imbalance=0.05,
                trade_flow_bias=0.04,
                bucket_volume=200.0,
                bucket_trade_count=160,
                bucket_range_bps=48.0,
            )

            supervisor.refresh_symbol_routing([trend_snapshot])
            initial_snapshot = supervisor.snapshot()
            initial_sol = next(
                item for item in initial_snapshot["symbol_routing"] if item["symbol"] == "SOL"
            )
            self.assertEqual(initial_sol["owner"], "pod_a")

            override_path.write_text(
                json.dumps(
                    {
                        "updated_at": "2026-04-07T22:00:00Z",
                        "symbol_pod_overrides": {"SOL": "pod_b"},
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            supervisor.refresh_symbol_routing([trend_snapshot])
            refreshed_snapshot = supervisor.snapshot()
            refreshed_sol = next(
                item for item in refreshed_snapshot["symbol_routing"] if item["symbol"] == "SOL"
            )

            self.assertEqual(refreshed_sol["owner"], "pod_b")
            self.assertEqual(refreshed_sol["mode"], "manual_override")
            self.assertTrue(refreshed_sol["override_active"])
            self.assertEqual(refreshed_sol["override_owner"], "pod_b")
            self.assertEqual(
                refreshed_snapshot["routing_overrides"]["runtime"],
                {"SOL": "pod_b"},
            )
            self.assertEqual(
                refreshed_snapshot["routing_overrides"]["effective"],
                {"SOL": "pod_b"},
            )

    def test_supervisor_limits_pod_b_ownership_to_feasible_dead_zone_capacity(self) -> None:
        self.config.pod_b.enabled = True
        symbols = [f"COIN{i}" for i in range(10)]
        self.config.hyperliquid.observation_universe = list(symbols)
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident",
            mode="observation",
        )
        supervisor.apply_regime_snapshot(
            RegimeSnapshot(
                ready=True,
                adx=8.0,
                atr_ratio=0.4,
                range_width_bps=30.0,
                structure_score=0.03,
            )
        )
        supervisor.refresh_symbol_routing(
            [
                SymbolMarketSnapshot(
                    symbol=symbol,
                    price=1.0 + index,
                    ema_fast=1.0 + index + 0.001,
                    ema_slow=1.0 + index,
                    vwap_distance_bps=-1.0,
                    structure_score=0.02,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                    book_imbalance=0.01,
                    trade_flow_bias=0.01,
                    bucket_volume=1000.0,
                    bucket_trade_count=20,
                    bucket_range_bps=12.0,
                )
                for index, symbol in enumerate(symbols)
            ]
        )

        snapshot = supervisor.snapshot()

        self.assertEqual(snapshot["capital_plan"]["regime"], "DeadZone")
        self.assertEqual(snapshot["capital_plan"]["pods"]["pod_b"]["target_pct"], 0.0)
        self.assertEqual(snapshot["capital_plan"]["pods"]["pod_b"]["target_usd"], 0.0)
        self.assertEqual(snapshot["pods"]["pod_b"]["owned_symbols"], [])
        self.assertEqual(
            [item["symbol"] for item in snapshot["capital_plan"]["pods"]["pod_b"]["symbols"]],
            [],
        )
        self.assertEqual(snapshot["ownership_conflicts"], [])


if __name__ == "__main__":
    unittest.main()
