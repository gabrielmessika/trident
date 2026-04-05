import json
import tempfile
import unittest
from pathlib import Path

from app.backtest.gbot_converter import GbotL2ToTridentConverter


class GbotConverterTests(unittest.TestCase):
    def test_converter_builds_trident_snapshots_from_l2(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir) / "data"
            (data_dir / "l2" / "BTC").mkdir(parents=True)
            (data_dir / "l2" / "ETH").mkdir(parents=True)

            btc_rows = [
                {
                    "timestamp": 1_700_000_000_000,
                    "coin": "BTC",
                    "best_bid": 50000.0,
                    "best_ask": 50001.0,
                    "bid_depth_10bps": 1.0,
                    "ask_depth_10bps": 1.0,
                    "spread_bps": 0.2,
                    "mid": 50000.5,
                },
                {
                    "timestamp": 1_700_000_060_000,
                    "coin": "BTC",
                    "best_bid": 50020.0,
                    "best_ask": 50021.0,
                    "bid_depth_10bps": 1.0,
                    "ask_depth_10bps": 1.0,
                    "spread_bps": 0.2,
                    "mid": 50020.5,
                },
            ]
            eth_rows = [
                {
                    "timestamp": 1_700_000_000_000,
                    "coin": "ETH",
                    "best_bid": 2500.0,
                    "best_ask": 2500.2,
                    "bid_depth_10bps": 1.0,
                    "ask_depth_10bps": 1.0,
                    "spread_bps": 0.8,
                    "mid": 2500.1,
                }
            ]

            for path, rows in (
                (data_dir / "l2" / "BTC" / "2026-04-01.jsonl", btc_rows),
                (data_dir / "l2" / "ETH" / "2026-04-01.jsonl", eth_rows),
            ):
                with path.open("w", encoding="utf-8") as handle:
                    for row in rows:
                        handle.write(json.dumps(row) + "\n")

            output_path = Path(tmpdir) / "trident_snapshots.jsonl"
            count = GbotL2ToTridentConverter(bucket_ms=60_000).convert(
                data_dir=data_dir,
                date="2026-04-01",
                coins=["BTC", "ETH"],
                output_path=output_path,
            )

            self.assertEqual(count, 2)
            lines = output_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)
            first = json.loads(lines[0])
            self.assertIn("regime_snapshot", first)
            self.assertEqual(first["symbols"][0]["symbol"], "BTC")
            self.assertIn("book_imbalance", first["symbols"][0])
            self.assertIn("trade_flow_bias", first["symbols"][0])


if __name__ == "__main__":
    unittest.main()
