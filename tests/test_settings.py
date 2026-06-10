import unittest

from app.hyperliquid.private_state import sdk_base_url_from_info_url
from app.settings import load_config


class SettingsConfigTests(unittest.TestCase):
    def test_mainnet_config_keeps_live_safety_defaults(self) -> None:
        config = load_config("config/trident.toml")

        self.assertEqual(config.hyperliquid.info_url, "https://api.hyperliquid.xyz/info")
        self.assertEqual(config.trident.risk.min_confidence, 0.50)
        self.assertEqual(config.trident.execution.live_max_order_notional_usd, 200.0)
        self.assertTrue(config.trident.execution.live_require_protective_orders)
        self.assertFalse(config.trident.execution.live_block_stop_grace_setups)
        self.assertEqual(config.trident.execution.live_stop_grace_catastrophic_sl_bps, 300.0)
        self.assertEqual(config.trident.execution.live_stop_grace_catastrophic_sl_multiplier, 2.0)
        self.assertEqual(config.trident.execution.live_stop_grace_catastrophic_sl_buffer_bps, 35.0)
        self.assertEqual(config.trident.execution.live_stop_grace_catastrophic_sl_max_bps, 160.0)
        self.assertFalse(config.trident.execution.live_post_only_retry_on_upgrade)
        self.assertEqual(config.trident.execution.live_post_only_buffer_bps, 1.0)
        self.assertEqual(config.pod_a.setup_allowed_regimes, ["TrendExpansion"])
        self.assertEqual(config.pod_a.min_setup_structure_score, 0.40)
        self.assertEqual(config.pod_a.setup_ema_tolerance_bps, 0.0)
        self.assertTrue(config.pod_a.intraday_setup_guardrail_enabled)
        self.assertEqual(config.pod_a.stop_grace_minutes, 60)
        self.assertEqual(config.pod_a.stop_grace_strong_minutes, 120)
        self.assertTrue(config.pod_a.early_failure_exit_enabled)
        self.assertTrue(config.pod_a.live_quality_sizing_enabled)
        self.assertTrue(config.pod_a.live_loss_tax_enabled)
        self.assertEqual(config.pod_a.live_correlation_full_size_slots, 3)
        self.assertTrue(config.pod_c.cluster_aware_v2_enabled)
        self.assertEqual(config.pod_c.min_confidence, 0.66)
        self.assertEqual(config.pod_c.size_multiplier, 0.70)
        self.assertEqual(config.pod_c.blocked_symbols, ["XYZ:SILVER"])
        self.assertEqual(config.pod_c.cluster_modes["silver"].break_even_multiplier, 0.90)
        self.assertEqual(config.pod_c.cluster_modes["silver"].trailing_activation_multiplier, 0.75)
        self.assertEqual(config.pod_c.cluster_modes["silver"].trailing_distance_multiplier, 0.75)

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
        self.assertEqual(config.hyperliquid.tradable_max_spread_bps, 25.0)
        self.assertEqual(config.hyperliquid.tradable_min_bucket_notional_usd, 25.0)
        self.assertEqual(config.hyperliquid.tradable_min_bucket_trade_count, 1)
        self.assertEqual(config.hyperliquid.market_cluster_overrides["BTC"], "index")
        self.assertEqual(config.hyperliquid.market_cluster_overrides["SOL"], "gold")
        self.assertEqual(config.trident.routing.symbol_pod_overrides["BTC"], "pod_c")
        self.assertEqual(config.trident.routing.symbol_pod_overrides["ETH"], "pod_a")
        self.assertTrue(config.pod_a.enabled)
        self.assertTrue(config.pod_c.enabled)
        self.assertEqual(config.trident.risk.min_confidence, 0.30)
        self.assertEqual(config.trident.execution.live_max_order_notional_usd, 100.0)
        self.assertFalse(config.trident.execution.live_require_protective_orders)
        self.assertFalse(config.trident.execution.live_block_stop_grace_setups)
        self.assertEqual(config.trident.execution.live_stop_grace_catastrophic_sl_bps, 300.0)
        self.assertTrue(config.trident.execution.live_post_only_retry_on_upgrade)
        self.assertEqual(config.trident.execution.live_post_only_buffer_bps, 2.0)
        self.assertEqual(config.trident.allocations.dead_zone.pod_a, 0.05)
        self.assertEqual(config.trident.allocations.dead_zone.pod_c, 0.05)
        self.assertEqual(
            config.trident.allocations_cluster.clusters["index"].dead_zone.target_pct,
            0.05,
        )
        self.assertEqual(
            config.trident.allocations_cluster.clusters["gold"].dead_zone.target_pct,
            0.05,
        )
        self.assertEqual(config.pod_a.blocked_regimes, [])
        self.assertEqual(config.pod_a.min_setup_structure_score, 0.05)
        self.assertEqual(config.pod_a.setup_ema_tolerance_bps, 8.0)
        self.assertEqual(config.pod_a.risk_per_trade_pct, 0.00045)
        self.assertIn("DeadZone", config.pod_a.setup_allowed_regimes)
        self.assertIn("trend_pullback_short", config.pod_a.allowed_setups)
        self.assertNotIn("trend_pullback_short", config.pod_a.disabled_setups)
        self.assertFalse(config.pod_c.cluster_aware_v2_enabled)
        self.assertEqual(config.pod_c.max_spread_bps, 18.0)
        self.assertEqual(config.pod_c.min_bucket_notional_usd, 25.0)
        self.assertEqual(config.pod_c.min_bucket_trade_count, 1)
        self.assertEqual(config.pod_c.min_confidence, 0.30)
        self.assertEqual(config.pod_c.min_trend_bps, 2.0)
        self.assertEqual(config.pod_c.min_structure_score, 0.05)
        self.assertEqual(config.pod_c.risk_per_trade_pct, 0.00060)
        self.assertEqual(
            config.pod_c.cluster_modes["gold"].allowed_setups,
            [
                "tradfi_continuation_long",
                "tradfi_continuation_short",
                "tradfi_reclaim_long",
                "tradfi_reclaim_short",
            ],
        )


if __name__ == "__main__":
    unittest.main()
