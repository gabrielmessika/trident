import unittest

from app.settings import load_config
from app.trident.pod_c.signals import TradfiTrendSignal
from app.trident.types import PodAllocation, PodName, SymbolAllocation
from scripts.run_p202_pod_c_cluster_multiplier_replay import (
    ClusterMultiplierTradfiPlanner,
    ScenarioResult,
    _closed_trade_breakdown,
    _profit_factor,
    build_cluster_statuses,
)


class P202PodCClusterMultiplierReplayTests(unittest.TestCase):
    def test_closed_trade_breakdown_keeps_fees_by_cluster(self) -> None:
        breakdown = _closed_trade_breakdown(
            {
                "closed_trade_log": [
                    {
                        "market_cluster": "gold",
                        "pnl_usd": 4.0,
                        "gross_pnl_usd": 5.0,
                        "fees_usd": 1.0,
                    },
                    {
                        "market_cluster": "gold",
                        "pnl_usd": -1.5,
                        "gross_pnl_usd": -1.0,
                        "fees_usd": 0.5,
                    },
                ]
            },
            key="market_cluster",
        )

        self.assertEqual(breakdown["gold"]["trades"], 2)
        self.assertEqual(breakdown["gold"]["pnl_usd"], 2.5)
        self.assertEqual(breakdown["gold"]["fees_usd"], 1.5)

    def test_profit_factor_uses_net_pnl(self) -> None:
        self.assertEqual(
            _profit_factor(
                {
                    "closed_trade_log": [
                        {"pnl_usd": 4.0},
                        {"pnl_usd": 2.0},
                        {"pnl_usd": -3.0},
                    ]
                }
            ),
            2.0,
        )

    def test_cluster_multiplier_planner_restores_global_multiplier(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_c.size_multiplier = 0.55
        planner = ClusterMultiplierTradfiPlanner(
            config,
            cluster_multipliers={"gold": 0.70},
        )
        allocation = PodAllocation(
            pod=PodName.POD_C,
            target_pct=1.0,
            target_usd=100.0,
            symbols=[
                SymbolAllocation(symbol="XYZ:GOLD", target_pct=1.0, target_usd=100.0),
            ],
        )
        signal = TradfiTrendSignal(
            symbol="XYZ:GOLD",
            side="long",
            setup="tradfi_continuation_long",
            confidence=0.8,
            entry_price=3500.0,
            market_cluster="gold",
        )

        plan = planner.build_trade_plan(signal, allocation)

        self.assertIsNotNone(plan)
        self.assertEqual(config.pod_c.size_multiplier, 0.55)
        assert plan is not None
        self.assertGreater(plan.margin_usd, 55.0)

    def test_silver_status_requires_manual_review_even_when_positive(self) -> None:
        baseline = _scenario_result(
            "baseline_055",
            pnl=10.0,
            trades=10,
            fees=2.0,
            clusters={"silver": {"trades": 4, "pnl_usd": 1.0, "gross_pnl_usd": 2.0, "fees_usd": 1.0}},
        )
        silver = _scenario_result(
            "silver_070",
            pnl=14.0,
            trades=10,
            fees=2.2,
            clusters={"silver": {"trades": 4, "pnl_usd": 4.0, "gross_pnl_usd": 5.0, "fees_usd": 1.0}},
        )

        statuses = build_cluster_statuses([baseline, silver])

        self.assertEqual(statuses["silver"]["status"], "watch")
        self.assertIn("manual review", str(statuses["silver"]["reason"]))


def _scenario_result(
    scenario: str,
    *,
    pnl: float,
    trades: int,
    fees: float,
    clusters: dict[str, dict[str, float | int]],
) -> ScenarioResult:
    return ScenarioResult(
        scenario=scenario,
        description=scenario,
        input_path="fixture.jsonl",
        global_multiplier=0.55,
        cluster_multipliers={},
        blocked_symbols=[],
        runtime_seconds=0.0,
        date_start="2026-06-01",
        date_end="2026-06-02",
        date_count=2,
        records_processed=10,
        signal_count=trades,
        accepted_count=trades,
        rejected_count=0,
        opened_count=trades,
        skipped_open_count=0,
        closed_trade_count=trades,
        win_rate=1.0,
        profit_factor=2.0,
        realized_pnl_usd=pnl,
        gross_pnl_usd=pnl + fees,
        fees_usd=fees,
        max_drawdown_usd=0.0,
        delta_vs_extended_baseline_usd=(0.0 if scenario == "baseline_055" else pnl - 10.0),
        trade_ratio_vs_extended_baseline=1.0,
        fees_ratio_vs_extended_baseline=1.0,
        rejections_by_reason={},
        close_reasons={},
        pnl_by_cluster=clusters,
        pnl_by_symbol={},
        pnl_by_date={},
    )


if __name__ == "__main__":
    unittest.main()
