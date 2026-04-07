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
                        "symbol": "SOL",
                        "price": 180.0,
                        "ema_fast": 181.5,
                        "ema_slow": 179.3,
                        "vwap_distance_bps": -5.0,
                        "structure_score": 0.72,
                        "funding_rate": 0.0002,
                        "spread_bps": 1.4,
                        "btc_aligned": True,
                        "book_imbalance": 0.05,
                        "trade_flow_bias": 0.04,
                        "bucket_volume": 200.0,
                        "bucket_trade_count": 120,
                        "bucket_range_bps": 48.0,
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
                        "symbol": "SOL",
                        "price": 180.0,
                        "ema_fast": 181.5,
                        "ema_slow": 179.3,
                        "vwap_distance_bps": -5.0,
                        "structure_score": 0.72,
                        "funding_rate": 0.0002,
                        "spread_bps": 1.4,
                        "btc_aligned": True,
                        "book_imbalance": 0.05,
                        "trade_flow_bias": 0.04,
                        "bucket_volume": 200.0,
                        "bucket_trade_count": 120,
                        "bucket_range_bps": 48.0,
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
        self.assertEqual(result.duplicate_timestamps_skipped, 1)
        self.assertEqual(result.initial_assignment_count, 1)

    def test_routing_replay_tracks_reassignments(self) -> None:
        self.config.trident.routing.reassignment_cooldown_seconds = 0
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
                        "symbol": "SOL",
                        "price": 180.0,
                        "ema_fast": 181.5,
                        "ema_slow": 179.3,
                        "vwap_distance_bps": -5.0,
                        "structure_score": 0.72,
                        "funding_rate": 0.0002,
                        "spread_bps": 1.4,
                        "btc_aligned": True,
                        "book_imbalance": 0.05,
                        "trade_flow_bias": 0.04,
                        "bucket_volume": 200.0,
                        "bucket_trade_count": 120,
                        "bucket_range_bps": 44.0,
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
                        "symbol": "SOL",
                        "price": 180.0,
                        "ema_fast": 180.2,
                        "ema_slow": 180.1,
                        "vwap_distance_bps": -15.0,
                        "structure_score": 0.9,
                        "funding_rate": 0.0002,
                        "spread_bps": 1.4,
                        "btc_aligned": True,
                        "book_imbalance": 0.55,
                        "trade_flow_bias": 0.52,
                        "bucket_volume": 220.0,
                        "bucket_trade_count": 130,
                        "bucket_range_bps": 180.0,
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
        self.assertEqual(result.symbol_reassignment_count_by_symbol.get("SOL"), 1)

    def test_routing_replay_applies_symbol_debounce_to_limit_second_reassignment(self) -> None:
        self.config.trident.routing.reassignment_cooldown_seconds = 0
        self.config.trident.routing.reassignment_debounce_min_score = 0.0
        self.config.trident.routing.reassignment_debounce_seconds_by_symbol = {"SOL": 3600}
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
                        "symbol": "SOL",
                        "price": 180.0,
                        "ema_fast": 181.5,
                        "ema_slow": 179.3,
                        "vwap_distance_bps": -5.0,
                        "structure_score": 0.72,
                        "funding_rate": 0.0002,
                        "spread_bps": 1.4,
                        "btc_aligned": True,
                        "book_imbalance": 0.05,
                        "trade_flow_bias": 0.04,
                        "bucket_volume": 200.0,
                        "bucket_trade_count": 120,
                        "bucket_range_bps": 44.0,
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
                        "symbol": "SOL",
                        "price": 180.0,
                        "ema_fast": 180.2,
                        "ema_slow": 180.1,
                        "vwap_distance_bps": -15.0,
                        "structure_score": 0.9,
                        "funding_rate": 0.0002,
                        "spread_bps": 1.4,
                        "btc_aligned": True,
                        "book_imbalance": 0.55,
                        "trade_flow_bias": 0.52,
                        "bucket_volume": 220.0,
                        "bucket_trade_count": 130,
                        "bucket_range_bps": 180.0,
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
                        "symbol": "SOL",
                        "price": 180.0,
                        "ema_fast": 181.5,
                        "ema_slow": 179.3,
                        "vwap_distance_bps": -5.0,
                        "structure_score": 0.72,
                        "funding_rate": 0.0002,
                        "spread_bps": 1.4,
                        "btc_aligned": True,
                        "book_imbalance": 0.05,
                        "trade_flow_bias": 0.04,
                        "bucket_volume": 200.0,
                        "bucket_trade_count": 120,
                        "bucket_range_bps": 44.0,
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
        self.assertEqual(result.symbol_reassignment_count_by_symbol.get("SOL"), 1)
        self.assertIn("dynamic_debounce", result.mode_counts)


if __name__ == "__main__":
    unittest.main()
