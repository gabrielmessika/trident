import unittest

from app.trident.symbol_registry import SymbolRegistry
from app.trident.types import PodName


class SymbolRegistryTests(unittest.TestCase):
    def test_claim_release_cycle(self) -> None:
        registry = SymbolRegistry()

        self.assertTrue(registry.claim("BTC", PodName.POD_A))
        self.assertEqual(registry.owner_of("BTC"), PodName.POD_A)
        self.assertFalse(registry.claim("BTC", PodName.POD_B))
        self.assertFalse(registry.release("BTC", PodName.POD_B))
        self.assertTrue(registry.release("BTC", PodName.POD_A))
        self.assertIsNone(registry.owner_of("BTC"))


if __name__ == "__main__":
    unittest.main()
