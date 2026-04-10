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
                        "symbol": "SPX",
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
                        "symbol": "SPX",
                        "price": 5110.0,
                        "ema_fast": 5124.0,
                        "ema_slow": 5092.0,
                        "vwap_distance_bps": -2.5,
                        "structure_score": 0.42,
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
        self.assertEqual(result.symbol_reassignment_count_by_symbol.get("SPX"), 1)

    def test_routing_replay_applies_symbol_debounce_to_limit_second_reassignment(self) -> None:
        self.config.trident.routing.reassignment_cooldown_seconds = 0
        self.config.trident.routing.reassignment_debounce_min_score = 0.0
        self.config.trident.routing.reassignment_debounce_seconds_by_symbol = {"SPX": 3600}
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
                        "symbol": "SPX",
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
                        "symbol": "SPX",
                        "price": 5110.0,
                        "ema_fast": 5124.0,
                        "ema_slow": 5092.0,
                        "vwap_distance_bps": -2.5,
                        "structure_score": 0.42,
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
                        "symbol": "SPX",
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
        self.assertEqual(result.symbol_reassignment_count_by_symbol.get("SPX"), 1)
        self.assertIn("dynamic_debounce", result.mode_counts)


if __name__ == "__main__":
    unittest.main()
