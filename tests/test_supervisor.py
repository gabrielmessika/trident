import unittest
from pathlib import Path
import tempfile

from app.settings import load_config, override_app_config
from app.trident.supervisor import TridentSupervisor
from app.trident.types import Regime, RegimeSnapshot, SignalPreview, SymbolMarketSnapshot


class SupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config("config/trident.toml")

    def test_supervisor_claims_symbols_for_enabled_pods(self) -> None:
        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident",
            mode="observation",
        )

        snapshot = supervisor.snapshot()

        self.assertEqual(snapshot["pods"]["pod_a"]["owned_symbols"], ["BTC", "ETH", "HYPE", "SOL"])
        self.assertEqual(snapshot["ownership_conflicts"], [])

    def test_supervisor_resolves_overlaps_using_priority_fallback(self) -> None:
        self.config.pod_b.enabled = True
        self.config.pod_b.symbols = ["DOGE", "SOL", "XRP"]
        self.config.pod_c.enabled = True
        self.config.pod_c.follower_symbols = ["HYPE", "SOL", "SUI"]

        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident",
            mode="observation",
        )

        snapshot = supervisor.snapshot()

        self.assertEqual(snapshot["pods"]["pod_c"]["owned_symbols"], ["HYPE", "SOL", "SUI"])
        self.assertEqual(snapshot["pods"]["pod_a"]["owned_symbols"], ["BTC", "ETH"])
        self.assertEqual(snapshot["pods"]["pod_b"]["owned_symbols"], ["DOGE", "XRP"])
        self.assertEqual(snapshot["ownership_conflicts"], [])
        self.assertEqual(
            snapshot["symbol_routing"][0]["mode"],
            "fallback_priority",
        )

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

        snapshot = supervisor.snapshot()

        self.assertEqual(snapshot["regime"], "TrendExpansion")
        self.assertEqual(snapshot["capital_plan"]["regime"], "TrendExpansion")
        self.assertEqual(snapshot["capital_plan"]["total_equity_usd"], 1000.0)
        self.assertEqual(snapshot["pods"]["pod_a"]["target_pct"], 0.6)
        self.assertEqual(snapshot["capital_plan"]["pods"]["pod_a"]["symbols"][0]["target_pct"], 0.15)

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
        self.assertEqual(plans[0].target_notional_usd, 450.0)
        self.assertEqual(plans[0].effective_leverage, 3.0)

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

        self.assertEqual(allocation.target_usd, 100.0)
        self.assertEqual(len(allocation.symbols), 1)
        self.assertEqual(allocation.symbols[0].symbol, "ETH")
        self.assertEqual(allocation.symbols[0].target_usd, 100.0)

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
                symbol="DOGE",
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
        self.config.pod_b.symbols = ["DOGE", "XRP"]
        with tempfile.TemporaryDirectory() as tmpdir:
            self.config.pod_b.passivbot_config_path = str(
                Path(tmpdir) / "runtime" / "passivbot" / "live.json"
            )
            supervisor = TridentSupervisor(
                config=self.config,
                profile="trident",
                mode="observation",
            )

            snapshot = supervisor.snapshot()

            self.assertEqual(snapshot["pod_b_status"]["managed_symbols"], ["DOGE", "XRP"])
            self.assertEqual(snapshot["pod_b_status"]["last_sync_reason"], "config_rendered")
            self.assertTrue(Path(self.config.pod_b.passivbot_config_path).exists())
            self.assertIn("inventory", snapshot["pod_b_status"])
            self.assertEqual(snapshot["pod_b_status"]["total_position_count"], 0)

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
        self.assertEqual(snapshot["pods"]["pod_a"]["owned_symbols"], ["BTC", "ETH", "HYPE", "SOL"])
        self.assertEqual(snapshot["pods"]["pod_b"]["owned_symbols"], ["DOGE", "SUI", "XRP"])
        self.assertEqual(snapshot["pods"]["pod_c"]["owned_symbols"], [])
        sui_routing = next(item for item in snapshot["symbol_routing"] if item["symbol"] == "SUI")
        self.assertEqual(sui_routing["owner"], "pod_b")
        self.assertEqual(sui_routing["mode"], "dynamic_affinity")

    def test_supervisor_routing_uses_hysteresis_before_switching_owner(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
