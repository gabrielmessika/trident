import unittest

from app.settings import load_config
from app.trident.symbol_router import SymbolRouter
from app.trident.types import PodName, Regime, SymbolRoutingDecision


class RoutingStabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config("config/trident.toml")

    def test_symbol_router_prefers_existing_owner_during_capacity_trim_within_margin(self) -> None:
        self.config.trident.capital.reference_equity_usd = 100.0
        self.config.trident.capital.min_symbol_allocation_usd = 25.0
        self.config.trident.allocations.dead_zone.pod_a = 0.0
        self.config.trident.allocations.dead_zone.pod_b = 0.30
        self.config.trident.allocations.dead_zone.pod_c = 0.0
        self.config.trident.allocations.dead_zone.cash = 0.70
        router = SymbolRouter(self.config)
        decisions = [
            SymbolRoutingDecision(
                symbol="BTC",
                owner=PodName.POD_B,
                mode="dynamic_affinity",
                reason="best_affinity:pod_b (0.50)",
                previous_owner=PodName.POD_B,
                candidate_pods=[PodName.POD_B],
                pod_scores={PodName.POD_B: 0.50},
            ),
            SymbolRoutingDecision(
                symbol="ETH",
                owner=PodName.POD_B,
                mode="dynamic_affinity",
                reason="best_affinity:pod_b (0.55)",
                previous_owner=None,
                candidate_pods=[PodName.POD_B],
                pod_scores={PodName.POD_B: 0.55},
            ),
        ]

        resolved = {
            item.symbol: item
            for item in router._enforce_capacity_limits(
                decisions,
                regime=Regime.DEAD_ZONE,
            )
        }

        self.assertEqual(resolved["BTC"].owner, PodName.POD_B)
        self.assertIsNone(resolved["ETH"].owner)
        self.assertEqual(resolved["ETH"].mode, "allocation_capacity")
        self.assertEqual(resolved["ETH"].reason, "capacity_trim:pod_b")


if __name__ == "__main__":
    unittest.main()
