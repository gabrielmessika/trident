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

    def test_symbol_cap_reduces_effective_pod_allocation(self) -> None:
        self.config.pod_c.enabled = True
        plan = self.allocator.build_plan(
            regime=Regime.PANIC_SQUEEZE,
            owned_symbols_by_pod={
                PodName.POD_A: [],
                PodName.POD_B: [],
                PodName.POD_C: ["SOL", "HYPE"],
            },
        )

        pod_c = plan.pod_allocations[PodName.POD_C]
        self.assertEqual(pod_c.target_pct, 0.05)
        self.assertEqual(len(pod_c.symbols), 2)
        self.assertEqual(pod_c.symbols[0].target_pct, 0.025)
        self.assertEqual(plan.cash_pct, 0.95)

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


if __name__ == "__main__":
    unittest.main()
