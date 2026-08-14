import gzip
import json
import tempfile
import unittest
from pathlib import Path

from app.backtest.snapshot_loader import SnapshotFormatError, SnapshotLoader


class SnapshotLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.loader = SnapshotLoader()

    def test_loader_reads_valid_jsonl_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "input.jsonl"
            record = {
                "timestamp": "2026-04-04T00:00:00Z",
                "regime_snapshot": {
                    "ready": True,
                    "adx": 32.0,
                    "atr_ratio": 1.2,
                    "range_width_bps": 180.0,
                    "structure_score": 0.55,
                    "btc_impulse": False,
                },
                "symbols": [
                    {
                        "symbol": "ETH",
                        "price": 3100.0,
                        "ema_fast": 3090.0,
                        "ema_slow": 3050.0,
                        "vwap_distance_bps": -8.0,
                        "structure_score": 0.62,
                        "funding_rate": 0.0001,
                        "spread_bps": 1.2,
                        "btc_aligned": True,
                    }
                ],
            }
            input_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

            records = list(self.loader.iter_jsonl(input_path))

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].source_file, "input.jsonl")
            self.assertEqual(records[0].symbols[0]["symbol"], "ETH")
            self.assertIn("breadth_pct", records[0].regime_snapshot)
            self.assertIn("leader_trend_score", records[0].regime_snapshot)

    def test_loader_reads_gzip_through_legacy_jsonl_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "input.jsonl"
            archived_path = Path(f"{input_path}.gz")
            record = {
                "timestamp": "2026-04-04T00:00:00Z",
                "regime_snapshot": {
                    "ready": True,
                    "adx": 32.0,
                    "atr_ratio": 1.2,
                    "range_width_bps": 180.0,
                    "structure_score": 0.55,
                    "btc_impulse": False,
                },
                "symbols": [
                    {
                        "symbol": "ETH",
                        "price": 3100.0,
                        "ema_fast": 3090.0,
                        "ema_slow": 3050.0,
                        "vwap_distance_bps": -8.0,
                        "structure_score": 0.62,
                        "funding_rate": 0.0001,
                        "spread_bps": 1.2,
                        "btc_aligned": True,
                    }
                ],
            }
            with gzip.open(archived_path, "wt", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")

            records = list(self.loader.iter_jsonl(input_path))

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].source_file, "input.jsonl")
            self.assertEqual(records[0].symbols[0]["symbol"], "ETH")

    def test_loader_rejects_missing_symbol_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "bad.jsonl"
            record = {
                "regime_snapshot": {
                    "ready": True,
                    "adx": 32.0,
                    "atr_ratio": 1.2,
                    "range_width_bps": 180.0,
                    "structure_score": 0.55,
                    "btc_impulse": False,
                },
                "symbols": [
                    {
                        "symbol": "ETH",
                        "price": 3100.0,
                    }
                ],
            }
            input_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

            with self.assertRaises(SnapshotFormatError):
                list(self.loader.iter_jsonl(input_path))

    def test_loader_merges_records_with_same_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "merged.jsonl"
            crypto_record = {
                "timestamp": "2026-04-04T00:00:00Z",
                "regime_snapshot": {
                    "ready": True,
                    "adx": 28.0,
                    "atr_ratio": 0.8,
                    "range_width_bps": 120.0,
                    "structure_score": 0.4,
                    "btc_impulse": False,
                },
                "cluster_regime_snapshots": {
                    "crypto": {
                        "ready": True,
                        "adx": 28.0,
                        "atr_ratio": 0.8,
                        "range_width_bps": 120.0,
                        "structure_score": 0.4,
                        "btc_impulse": False,
                    }
                },
                "symbols": [
                    {
                        "symbol": "BTC",
                        "price": 70000.0,
                        "ema_fast": 69950.0,
                        "ema_slow": 69800.0,
                        "vwap_distance_bps": 4.0,
                        "structure_score": 0.4,
                        "funding_rate": 0.0001,
                        "spread_bps": 0.8,
                        "btc_aligned": True,
                    }
                ],
            }
            tradfi_record = {
                "timestamp": "2026-04-04T00:00:00Z",
                "regime_snapshot": {
                    "ready": True,
                    "adx": 4.0,
                    "atr_ratio": 0.1,
                    "range_width_bps": 10.0,
                    "structure_score": 0.05,
                    "btc_impulse": False,
                },
                "cluster_regime_snapshots": {
                    "gold": {
                        "ready": True,
                        "adx": 4.0,
                        "atr_ratio": 0.1,
                        "range_width_bps": 10.0,
                        "structure_score": 0.05,
                        "btc_impulse": False,
                    }
                },
                "symbols": [
                    {
                        "symbol": "PAXG",
                        "price": 3200.0,
                        "ema_fast": 3199.0,
                        "ema_slow": 3198.0,
                        "vwap_distance_bps": 1.0,
                        "structure_score": 0.05,
                        "funding_rate": 0.0,
                        "spread_bps": 1.2,
                        "btc_aligned": True,
                    }
                ],
            }
            input_path.write_text(
                json.dumps(crypto_record) + "\n" + json.dumps(tradfi_record) + "\n",
                encoding="utf-8",
            )

            records = list(self.loader.iter_merged_jsonl(input_path))

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].timestamp, "2026-04-04T00:00:00Z")
            self.assertEqual(
                sorted(item["symbol"] for item in records[0].symbols),
                ["BTC", "PAXG"],
            )
            self.assertEqual(records[0].regime_snapshot["adx"], 28.0)
            self.assertIn("breadth_pct", records[0].regime_snapshot)
            self.assertIn("crypto", records[0].cluster_regime_snapshots)
            self.assertIn("gold", records[0].cluster_regime_snapshots)


if __name__ == "__main__":
    unittest.main()
