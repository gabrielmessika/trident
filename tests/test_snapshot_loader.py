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


if __name__ == "__main__":
    unittest.main()
