import unittest
import tempfile
from pathlib import Path

from app.live.collector import HyperliquidLiveCollector
from app.live.errors import HyperliquidRateLimitError
from app.settings import load_config


class LiveCollectorTests(unittest.TestCase):
    def test_collector_shards_observation_universe_by_connection_limit(self) -> None:
        config = load_config("config/trident.toml")
        config.hyperliquid.max_coins_per_connection = 3
        collector = HyperliquidLiveCollector(
            config,
            coins=["BTC", "ETH", "SOL", "HYPE", "DOGE", "XRP", "BTC"],
        )
        self.assertEqual(collector.coins, ["BTC", "ETH", "SOL", "HYPE", "DOGE", "XRP"])
        self.assertEqual(
            collector.coin_shards,
            [["BTC", "ETH", "SOL"], ["HYPE", "DOGE", "XRP"]],
        )

    def test_parse_message_counts_invalid_json(self) -> None:
        collector = HyperliquidLiveCollector(load_config("config/trident.toml"), coins=["BTC"])
        payload = collector._parse_message("{bad json")
        self.assertIsNone(payload)
        self.assertEqual(collector.stats.invalid_message_count, 1)

    def test_handle_payload_ignores_pong(self) -> None:
        collector = HyperliquidLiveCollector(load_config("config/trident.toml"), coins=["BTC"])
        records = collector._handle_payload({"channel": "pong"})
        self.assertEqual(records, [])
        self.assertEqual(collector.stats.pong_count, 1)
        self.assertEqual(collector.stats.messages_processed, 0)

    def test_handle_payload_raises_on_rate_limit(self) -> None:
        collector = HyperliquidLiveCollector(load_config("config/trident.toml"), coins=["BTC"])
        with self.assertRaises(HyperliquidRateLimitError):
            collector._handle_payload({"channel": "error", "data": "rate limit exceeded"})

    def test_collector_does_not_fallback_to_pod_a_symbols(self) -> None:
        config = load_config("config/trident.toml")
        config.hyperliquid.observation_universe = []
        config.hyperliquid.default_coins = []

        collector = HyperliquidLiveCollector(config)

        self.assertEqual(collector.coins, [])

    def test_collector_uses_lowercase_dex_prefix_for_builder_dex_ws_symbols(self) -> None:
        collector = HyperliquidLiveCollector(
            load_config("config/trident.toml"),
            coins=["XYZ:SP500", "XYZ:GOLD"],
        )

        self.assertEqual(collector.coins, ["XYZ:SP500", "XYZ:GOLD"])
        self.assertEqual(collector.coin_shards, [["xyz:SP500", "xyz:GOLD"]])

    def test_backoff_is_capped(self) -> None:
        collector = HyperliquidLiveCollector(load_config("config/trident.toml"), coins=["BTC"])
        collector.stats.consecutive_failures = 8
        self.assertEqual(
            collector._backoff_delay(),
            collector.config.hyperliquid.max_reconnect_delay_seconds,
        )

    def test_collector_writes_pod_b_sidecar_rows_on_intraminute_roll(self) -> None:
        config = load_config("config/trident.toml")
        with tempfile.TemporaryDirectory() as tmpdir:
            config.hyperliquid.snapshot_output_dir = str(Path(tmpdir) / "snapshots")
            config.hyperliquid.pod_b_feature_output_dir = str(Path(tmpdir) / "features")
            collector = HyperliquidLiveCollector(config, coins=["BTC"])

            collector._handle_payload(
                {
                    "channel": "l2Book",
                    "data": {
                        "coin": "BTC",
                        "time": 1_000,
                        "levels": [
                            [{"px": "68000", "sz": "2.0", "n": 1}],
                            [{"px": "68002", "sz": "3.0", "n": 1}],
                        ],
                    },
                }
            )
            collector._handle_payload(
                {
                    "channel": "l2Book",
                    "data": {
                        "coin": "BTC",
                        "time": 11_000,
                        "levels": [
                            [{"px": "68010", "sz": "4.0", "n": 1}],
                            [{"px": "68013", "sz": "1.0", "n": 1}],
                        ],
                    },
                }
            )

            feature_path = Path(config.hyperliquid.pod_b_feature_output_dir) / "1970-01-01.jsonl"
            self.assertTrue(feature_path.exists())
            lines = feature_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertIn("\"symbol\": \"BTC\"", lines[0])
            self.assertEqual(collector.stats.pod_b_feature_rows_written, 1)


if __name__ == "__main__":
    unittest.main()
