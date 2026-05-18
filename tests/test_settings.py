import unittest

from app.hyperliquid.private_state import sdk_base_url_from_info_url
from app.settings import load_config


class SettingsConfigTests(unittest.TestCase):
    def test_testnet_config_extends_main_config_and_switches_hyperliquid_network(self) -> None:
        config = load_config("config/trident_testnet.toml")

        self.assertEqual(config.hyperliquid.info_url, "https://api.hyperliquid-testnet.xyz/info")
        self.assertEqual(config.hyperliquid.ws_url, "wss://api.hyperliquid-testnet.xyz/ws")
        self.assertEqual(
            sdk_base_url_from_info_url(config.hyperliquid.info_url),
            "https://api.hyperliquid-testnet.xyz",
        )
        self.assertEqual(config.hyperliquid.snapshot_output_dir, "./data/live_snapshots_testnet")
        self.assertEqual(
            config.hyperliquid.rate_limit_state_path,
            "./runtime/hyperliquid_testnet_rate_limits.json",
        )
        self.assertEqual(config.hyperliquid.observation_universe, ["BTC", "ETH", "SOL"])
        self.assertEqual(config.hyperliquid.market_cluster_overrides["BTC"], "index")
        self.assertEqual(config.hyperliquid.market_cluster_overrides["SOL"], "gold")
        self.assertEqual(config.trident.routing.symbol_pod_overrides["BTC"], "pod_c")
        self.assertEqual(config.trident.routing.symbol_pod_overrides["ETH"], "pod_a")
        self.assertTrue(config.pod_a.enabled)
        self.assertTrue(config.pod_c.enabled)


if __name__ == "__main__":
    unittest.main()
