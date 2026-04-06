import unittest

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

    def test_backoff_is_capped(self) -> None:
        collector = HyperliquidLiveCollector(load_config("config/trident.toml"), coins=["BTC"])
        collector.stats.consecutive_failures = 8
        self.assertEqual(
            collector._backoff_delay(),
            collector.config.hyperliquid.max_reconnect_delay_seconds,
        )


if __name__ == "__main__":
    unittest.main()
