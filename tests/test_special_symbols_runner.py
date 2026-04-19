import unittest

from app.settings import load_config
from app.special_symbols_runtime import build_special_symbols_runtime_config


class SpecialSymbolsRuntimeTests(unittest.TestCase):
    def test_builds_isolated_runtime_config_from_shadow_profile(self) -> None:
        config = load_config("config/trident_special_symbols_core_shadow.toml")

        runtime_config, selection = build_special_symbols_runtime_config(config)

        self.assertEqual(selection.tradable_symbols, ["TAO", "XPL", "BIO"])
        self.assertEqual(selection.observe_only_symbols, ["ETH", "PENGU"])
        self.assertEqual(
            selection.observation_universe,
            ["ETH", "PENGU", "TAO", "XPL", "BIO"],
        )
        self.assertTrue(runtime_config.pod_a.enabled)
        self.assertFalse(runtime_config.pod_b.enabled)
        self.assertFalse(runtime_config.pod_c.enabled)
        self.assertEqual(runtime_config.pod_a.blocked_symbols, [])
        self.assertEqual(
            runtime_config.hyperliquid.tradable_blocked_symbols,
            ["ETH", "PENGU"],
        )

    def test_runtime_config_can_override_special_symbol_lists(self) -> None:
        config = load_config("config/trident_special_symbols_core_shadow.toml")

        runtime_config, selection = build_special_symbols_runtime_config(
            config,
            tradable_symbols=["TAO", "BIO"],
            observe_only_symbols=["BTC"],
        )

        self.assertEqual(selection.tradable_symbols, ["TAO", "BIO"])
        self.assertEqual(selection.observe_only_symbols, ["BTC"])
        self.assertEqual(selection.observation_universe, ["BTC", "TAO", "BIO"])
        self.assertEqual(runtime_config.hyperliquid.observation_universe, ["BTC", "TAO", "BIO"])
        self.assertEqual(runtime_config.hyperliquid.default_coins, ["BTC", "TAO", "BIO"])
        self.assertEqual(runtime_config.hyperliquid.tradable_blocked_symbols, ["ETH", "PENGU", "BTC"])


if __name__ == "__main__":
    unittest.main()
