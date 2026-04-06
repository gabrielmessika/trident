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

    def test_supervisor_detects_ownership_conflicts_using_priority(self) -> None:
        self.config.pod_b.enabled = True
        self.config.pod_b.symbols = ["SOL", "XRP"]
        self.config.pod_c.enabled = True
        self.config.pod_c.follower_symbols = ["HYPE", "SOL"]

        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident",
            mode="observation",
        )

        snapshot = supervisor.snapshot()

        self.assertEqual(snapshot["pods"]["pod_c"]["owned_symbols"], ["HYPE", "SOL"])
        self.assertEqual(snapshot["pods"]["pod_a"]["owned_symbols"], ["BTC", "ETH"])
        self.assertEqual(snapshot["pods"]["pod_b"]["owned_symbols"], ["XRP"])
        self.assertEqual(
            snapshot["ownership_conflicts"],
            [
                {"symbol": "SOL", "requested_by": "pod_a", "owner": "pod_c"},
                {"symbol": "HYPE", "requested_by": "pod_a", "owner": "pod_c"},
                {"symbol": "SOL", "requested_by": "pod_b", "owner": "pod_c"},
            ],
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


if __name__ == "__main__":
    unittest.main()
