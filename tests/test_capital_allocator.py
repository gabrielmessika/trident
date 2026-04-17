import unittest

from app.settings import load_config
from app.trident.capital_allocator import CapitalAllocator
from app.trident.types import PodName, Regime


class CapitalAllocatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config("config/trident.toml")
        self.allocator = CapitalAllocator(self.config)

    def test_disabled_pods_flow_back_to_cash(self) -> None:
        plan = self.allocator.build_plan(
            regime=Regime.RANGE_AUCTION,
            owned_symbols_by_pod={
                PodName.POD_A: ["BTC", "ETH", "HYPE", "SOL"],
                PodName.POD_B: [],
                PodName.POD_C: [],
            },
        )

        self.assertEqual(plan.pod_allocations[PodName.POD_A].target_pct, 0.1)
        self.assertEqual(plan.pod_allocations[PodName.POD_B].target_pct, 0.0)
        self.assertEqual(plan.pod_allocations[PodName.POD_C].target_pct, 0.0)
        self.assertEqual(plan.cash_pct, 0.9)

    def test_pod_c_defaults_to_zero_without_cluster_regimes(self) -> None:
        self.config.pod_c.enabled = True
        plan = self.allocator.build_plan(
            regime=Regime.PANIC_SQUEEZE,
            owned_symbols_by_pod={
                PodName.POD_A: [],
                PodName.POD_B: [],
                PodName.POD_C: ["GLD", "SPY"],
            },
        )

        pod_c = plan.pod_allocations[PodName.POD_C]
        self.assertEqual(pod_c.target_pct, 0.0)
        self.assertEqual(pod_c.symbols, [])
        self.assertEqual(plan.cash_pct, 1.0)

    def test_symbols_below_min_allocation_flow_back_once_to_cash(self) -> None:
        plan = self.allocator.build_plan(
            regime=Regime.DEAD_ZONE,
            owned_symbols_by_pod={
                PodName.POD_A: [],
                PodName.POD_B: [f"COIN{i}" for i in range(10)],
                PodName.POD_C: [],
            },
        )

        pod_b = plan.pod_allocations[PodName.POD_B]
        self.assertEqual(pod_b.target_pct, 0.0)
        self.assertEqual(pod_b.target_usd, 0.0)
        self.assertEqual(pod_b.symbols, [])
        self.assertEqual(plan.cash_pct, 1.0)
        self.assertEqual(plan.cash_usd, 1000.0)

    def test_cluster_budgets_allocate_only_active_tradfi_clusters(self) -> None:
        self.config.pod_c.enabled = True
        plan = self.allocator.build_plan(
            regime=Regime.PANIC_SQUEEZE,
            owned_symbols_by_pod={
                PodName.POD_A: ["BTC"],
                PodName.POD_B: [],
                PodName.POD_C: ["GLD", "SPY"],
            },
            cluster_regimes={
                "gold": Regime.TREND_EXPANSION,
                "index": Regime.DEAD_ZONE,
            },
            symbol_clusters_by_pod={
                PodName.POD_C: {
                    "GLD": "gold",
                    "SPY": "index",
                }
            },
        )

        pod_c = plan.pod_allocations[PodName.POD_C]
        self.assertEqual(pod_c.target_pct, 0.15)
        self.assertEqual([(item.symbol, item.target_pct) for item in pod_c.symbols], [("GLD", 0.15)])
        self.assertEqual(plan.cash_pct, 0.75)
        self.assertEqual(
            round(plan.cash_pct + sum(item.target_pct for item in plan.pod_allocations.values()), 6),
            1.0,
        )

    def test_cluster_budgets_scale_to_available_tradfi_sleeve(self) -> None:
        self.config.pod_c.enabled = True
        self.config.pod_b.enabled = True
        self.config.pod_b.max_allocation_pct = 0.85
        plan = self.allocator.build_plan(
            regime=Regime.RANGE_AUCTION,
            owned_symbols_by_pod={
                PodName.POD_A: ["BTC"],
                PodName.POD_B: ["DOGE", "XRP", "SUI"],
                PodName.POD_C: ["GLD", "SPY", "SLV"],
            },
            cluster_regimes={
                "gold": Regime.TREND_EXPANSION,
                "index": Regime.TREND_EXPANSION,
                "silver": Regime.TREND_EXPANSION,
            },
            symbol_clusters_by_pod={
                PodName.POD_C: {
                    "GLD": "gold",
                    "SPY": "index",
                    "SLV": "silver",
                }
            },
        )

        pod_c = plan.pod_allocations[PodName.POD_C]
        self.assertEqual(pod_c.target_pct, 0.15)
        self.assertEqual(
            [(item.symbol, item.target_pct) for item in pod_c.symbols],
            [("GLD", 0.075), ("SPY", 0.05), ("SLV", 0.025)],
        )
        self.assertEqual(plan.cash_pct, 0.0)
        self.assertEqual(
            round(plan.cash_pct + sum(item.target_pct for item in plan.pod_allocations.values()), 6),
            1.0,
        )

    def test_correlated_crypto_groups_scale_uniform_allocations_and_return_cash(self) -> None:
        plan = self.allocator.build_plan(
            regime=Regime.RANGE_AUCTION,
            owned_symbols_by_pod={
                PodName.POD_A: ["BTC"],
                PodName.POD_B: ["ETH", "LINK", "AVAX", "ADA"],
                PodName.POD_C: [],
            },
        )

        pod_b = plan.pod_allocations[PodName.POD_B]
        self.assertEqual(round(pod_b.target_pct, 6), 0.228572)
        self.assertEqual(
            [(item.symbol, round(item.target_pct, 6)) for item in pod_b.symbols],
            [
                ("ETH", 0.057143),
                ("LINK", 0.057143),
                ("AVAX", 0.057143),
                ("ADA", 0.057143),
            ],
        )
        self.assertTrue(all(item.capped_by_correlation for item in pod_b.symbols))
        self.assertEqual({item.correlation_group for item in pod_b.symbols}, {"core_beta"})
        self.assertEqual(round(plan.cash_pct, 6), 0.671428)


if __name__ == "__main__":
    unittest.main()
