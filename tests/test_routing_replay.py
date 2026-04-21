import json
import tempfile
import unittest
from pathlib import Path

from app.backtest.routing_replay import RoutingReplayRunner
from app.settings import load_config


class RoutingReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config("config/trident.toml")
        self.config.pod_b.enabled = True
        self.config.pod_c.enabled = True

    def test_routing_replay_dedupes_duplicate_timestamps(self) -> None:
        records = [
            {
                "timestamp": "2026-04-07T00:00:00Z",
                "regime_snapshot": {
                    "ready": True,
                    "adx": 28.0,
                    "atr_ratio": 1.1,
                    "range_width_bps": 140.0,
                    "structure_score": 0.5,
                    "btc_impulse": False,
                },
                "symbols": [
                    {
                        "symbol": "SPX",
                        "price": 5100.0,
                        "ema_fast": 5112.0,
                        "ema_slow": 5087.0,
                        "vwap_distance_bps": -3.0,
                        "structure_score": 0.44,
                        "funding_rate": 0.0,
                        "spread_bps": 1.0,
                        "btc_aligned": True,
                        "book_imbalance": 0.12,
                        "trade_flow_bias": 0.10,
                        "bucket_volume": 1.8,
                        "bucket_trade_count": 7,
                        "bucket_range_bps": 20.0,
                    }
                ],
            },
            {
                "timestamp": "2026-04-07T00:00:00Z",
                "regime_snapshot": {
                    "ready": True,
                    "adx": 28.0,
                    "atr_ratio": 1.1,
                    "range_width_bps": 140.0,
                    "structure_score": 0.5,
                    "btc_impulse": False,
                },
                "symbols": [
                    {
                        "symbol": "SPX",
                        "price": 5100.0,
                        "ema_fast": 5112.0,
                        "ema_slow": 5087.0,
                        "vwap_distance_bps": -3.0,
                        "structure_score": 0.44,
                        "funding_rate": 0.0,
                        "spread_bps": 1.0,
                        "btc_aligned": True,
                        "book_imbalance": 0.12,
                        "trade_flow_bias": 0.10,
                        "bucket_volume": 1.8,
                        "bucket_trade_count": 7,
                        "bucket_range_bps": 20.0,
                    }
                ],
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "routing_replay.jsonl"
            input_path.write_text(
                "".join(json.dumps(item) + "\n" for item in records),
                encoding="utf-8",
            )
            result = RoutingReplayRunner(self.config).run_jsonl(input_path)

        self.assertEqual(result.records_processed, 1)
        self.assertEqual(result.unique_timestamps_processed, 1)
        self.assertEqual(result.duplicate_timestamps_skipped, 0)
        self.assertEqual(result.initial_assignment_count, 1)

    def test_routing_replay_merges_split_snapshot_lines_before_routing(self) -> None:
        self.config.hyperliquid.observation_universe = ["BTC", "PAXG"]
        self.config.pod_a.enabled = True
        self.config.pod_b.enabled = False
        self.config.pod_c.enabled = True
        self.config.pod_c.allowed_market_clusters = ["gold"]
        records = [
            {
                "timestamp": "2026-04-07T00:00:00Z",
                "regime_snapshot": {
                    "ready": True,
                    "adx": 28.0,
                    "atr_ratio": 1.1,
                    "range_width_bps": 140.0,
                    "structure_score": 0.5,
                    "btc_impulse": False,
                },
                "cluster_regime_snapshots": {
                    "crypto": {
                        "ready": True,
                        "adx": 28.0,
                        "atr_ratio": 1.1,
                        "range_width_bps": 140.0,
                        "structure_score": 0.5,
                        "btc_impulse": False,
                    },
                },
                "symbols": [
                    {
                        "symbol": "BTC",
                        "price": 5100.0,
                        "ema_fast": 5112.0,
                        "ema_slow": 5087.0,
                        "vwap_distance_bps": -3.0,
                        "structure_score": 0.44,
                        "funding_rate": 0.0,
                        "spread_bps": 1.0,
                        "btc_aligned": True,
                        "book_imbalance": 0.12,
                        "trade_flow_bias": 0.10,
                        "bucket_volume": 1.8,
                        "bucket_trade_count": 7,
                        "bucket_range_bps": 20.0,
                    }
                ],
            },
            {
                "timestamp": "2026-04-07T00:00:00Z",
                "regime_snapshot": {
                    "ready": True,
                    "adx": 12.0,
                    "atr_ratio": 0.2,
                    "range_width_bps": 25.0,
                    "structure_score": 0.1,
                    "btc_impulse": False,
                },
                "cluster_regime_snapshots": {
                    "gold": {
                        "ready": True,
                        "adx": 12.0,
                        "atr_ratio": 0.2,
                        "range_width_bps": 25.0,
                        "structure_score": 0.1,
                        "btc_impulse": False,
                    },
                },
                "symbols": [
                    {
                        "symbol": "PAXG",
                        "price": 2400.0,
                        "ema_fast": 2401.5,
                        "ema_slow": 2397.0,
                        "vwap_distance_bps": -1.0,
                        "structure_score": 0.2,
                        "funding_rate": 0.0,
                        "spread_bps": 0.3,
                        "btc_aligned": True,
                        "book_imbalance": 0.05,
                        "trade_flow_bias": 0.04,
                        "bucket_volume": 10.0,
                        "bucket_trade_count": 5,
                        "bucket_range_bps": 8.0,
                    }
                ],
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "routing_replay_merged.jsonl"
            input_path.write_text(
                "".join(json.dumps(item) + "\n" for item in records),
                encoding="utf-8",
            )
            result = RoutingReplayRunner(self.config).run_jsonl(input_path)

        self.assertEqual(result.records_processed, 1)
        self.assertEqual(result.unique_timestamps_processed, 1)
        self.assertEqual(result.duplicate_timestamps_skipped, 0)
        self.assertEqual(result.initial_assignment_count, 2)

    def test_routing_replay_tracks_reassignments(self) -> None:
        self.config.trident.routing.reassignment_cooldown_seconds = 0
        self.config.trident.allocations.trend_expansion.pod_a = 0.45
        self.config.trident.allocations.trend_expansion.pod_b = 0.35
        self.config.trident.allocations.trend_expansion.pod_c = 0.0
        self.config.trident.allocations.trend_expansion.cash = 0.20
        records = [
            {
                "timestamp": "2026-04-07T00:00:00Z",
                "regime_snapshot": {
                    "ready": True,
                    "adx": 30.0,
                    "atr_ratio": 1.15,
                    "range_width_bps": 150.0,
                    "structure_score": 0.55,
                    "btc_impulse": False,
                },
                "symbols": [
                    {
                        "symbol": "BTC",
                        "price": 5100.0,
                        "ema_fast": 5100.5,
                        "ema_slow": 5100.0,
                        "vwap_distance_bps": -0.6,
                        "structure_score": 0.02,
                        "funding_rate": 0.0,
                        "spread_bps": 1.0,
                        "btc_aligned": True,
                        "book_imbalance": 0.01,
                        "trade_flow_bias": 0.01,
                        "bucket_volume": 0.5,
                        "bucket_trade_count": 5,
                        "bucket_range_bps": 12.0,
                        "realized_vol_short_bps": 6.1,
                        "realized_vol_long_bps": 5.0,
                    }
                ],
            },
            {
                "timestamp": "2026-04-07T00:01:00Z",
                "regime_snapshot": {
                    "ready": True,
                    "adx": 30.0,
                    "atr_ratio": 1.15,
                    "range_width_bps": 150.0,
                    "structure_score": 0.55,
                    "btc_impulse": True,
                },
                "symbols": [
                    {
                        "symbol": "BTC",
                        "price": 5110.0,
                        "ema_fast": 5110.3,
                        "ema_slow": 5110.0,
                        "vwap_distance_bps": 6.0,
                        "structure_score": 0.2,
                        "funding_rate": 0.0,
                        "spread_bps": 1.0,
                        "btc_aligned": True,
                        "book_imbalance": 0.35,
                        "trade_flow_bias": 0.35,
                        "bucket_volume": 2.0,
                        "bucket_trade_count": 12,
                        "bucket_range_bps": 35.0,
                        "delta_book_imbalance": 0.3,
                        "delta_trade_flow_bias": 0.35,
                        "volume_ratio": 2.8,
                        "trade_count_ratio": 2.4,
                        "realized_vol_short_bps": 8.0,
                        "realized_vol_long_bps": 4.0,
                        "compression_score": 0.4,
                        "microprice_dislocation_bps": 1.5,
                    }
                ],
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "routing_reassign.jsonl"
            input_path.write_text(
                "".join(json.dumps(item) + "\n" for item in records),
                encoding="utf-8",
            )
            result = RoutingReplayRunner(self.config).run_jsonl(input_path)

        self.assertEqual(result.records_processed, 2)
        self.assertGreaterEqual(result.reassignment_event_count, 1)
        self.assertGreaterEqual(result.local_regime_transition_count, 2)
        self.assertEqual(result.max_ownership_conflict_count, 0)
        self.assertEqual(result.symbol_reassignment_count_by_symbol.get("BTC"), 1)

    def test_routing_replay_applies_symbol_debounce_to_limit_second_reassignment(self) -> None:
        self.config.trident.routing.reassignment_cooldown_seconds = 0
        self.config.trident.routing.reassignment_debounce_min_score = 0.0
        self.config.trident.routing.reassignment_debounce_seconds_by_symbol = {"BTC": 3600}
        self.config.trident.allocations.trend_expansion.pod_a = 0.45
        self.config.trident.allocations.trend_expansion.pod_b = 0.35
        self.config.trident.allocations.trend_expansion.pod_c = 0.0
        self.config.trident.allocations.trend_expansion.cash = 0.20
        records = [
            {
                "timestamp": "2026-04-07T00:00:00Z",
                "regime_snapshot": {
                    "ready": True,
                    "adx": 30.0,
                    "atr_ratio": 1.15,
                    "range_width_bps": 150.0,
                    "structure_score": 0.55,
                    "btc_impulse": False,
                },
                "symbols": [
                    {
                        "symbol": "BTC",
                        "price": 5100.0,
                        "ema_fast": 5100.5,
                        "ema_slow": 5100.0,
                        "vwap_distance_bps": -0.6,
                        "structure_score": 0.02,
                        "funding_rate": 0.0,
                        "spread_bps": 1.0,
                        "btc_aligned": True,
                        "book_imbalance": 0.01,
                        "trade_flow_bias": 0.01,
                        "bucket_volume": 0.5,
                        "bucket_trade_count": 5,
                        "bucket_range_bps": 12.0,
                        "realized_vol_short_bps": 6.1,
                        "realized_vol_long_bps": 5.0,
                    }
                ],
            },
            {
                "timestamp": "2026-04-07T00:01:00Z",
                "regime_snapshot": {
                    "ready": True,
                    "adx": 30.0,
                    "atr_ratio": 1.15,
                    "range_width_bps": 150.0,
                    "structure_score": 0.55,
                    "btc_impulse": True,
                },
                "symbols": [
                    {
                        "symbol": "BTC",
                        "price": 5110.0,
                        "ema_fast": 5110.3,
                        "ema_slow": 5110.0,
                        "vwap_distance_bps": 6.0,
                        "structure_score": 0.2,
                        "funding_rate": 0.0,
                        "spread_bps": 1.0,
                        "btc_aligned": True,
                        "book_imbalance": 0.35,
                        "trade_flow_bias": 0.35,
                        "bucket_volume": 2.0,
                        "bucket_trade_count": 12,
                        "bucket_range_bps": 35.0,
                        "delta_book_imbalance": 0.3,
                        "delta_trade_flow_bias": 0.35,
                        "volume_ratio": 2.8,
                        "trade_count_ratio": 2.4,
                        "realized_vol_short_bps": 8.0,
                        "realized_vol_long_bps": 4.0,
                        "compression_score": 0.4,
                        "microprice_dislocation_bps": 1.5,
                    }
                ],
            },
            {
                "timestamp": "2026-04-07T00:02:00Z",
                "regime_snapshot": {
                    "ready": True,
                    "adx": 30.0,
                    "atr_ratio": 1.15,
                    "range_width_bps": 150.0,
                    "structure_score": 0.55,
                    "btc_impulse": False,
                },
                "symbols": [
                    {
                        "symbol": "BTC",
                        "price": 5100.0,
                        "ema_fast": 5100.5,
                        "ema_slow": 5100.0,
                        "vwap_distance_bps": -0.6,
                        "structure_score": 0.02,
                        "funding_rate": 0.0,
                        "spread_bps": 1.0,
                        "btc_aligned": True,
                        "book_imbalance": 0.01,
                        "trade_flow_bias": 0.01,
                        "bucket_volume": 0.5,
                        "bucket_trade_count": 5,
                        "bucket_range_bps": 12.0,
                        "realized_vol_short_bps": 6.1,
                        "realized_vol_long_bps": 5.0,
                    }
                ],
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "routing_debounce.jsonl"
            input_path.write_text(
                "".join(json.dumps(item) + "\n" for item in records),
                encoding="utf-8",
            )
            result = RoutingReplayRunner(self.config).run_jsonl(input_path)

        self.assertEqual(result.records_processed, 3)
        self.assertEqual(result.reassignment_event_count, 1)
        self.assertEqual(result.symbol_reassignment_count_by_symbol.get("BTC"), 1)
        self.assertIn("dynamic_debounce", result.mode_counts)


if __name__ == "__main__":
    unittest.main()
