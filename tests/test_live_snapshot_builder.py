import json
import tempfile
import unittest
from pathlib import Path

from app.live.snapshot_builder import LiveSnapshotBuilder
from app.live.snapshot_writer import LiveSnapshotWriter


class LiveSnapshotBuilderTests(unittest.TestCase):
    def test_builder_emits_snapshot_on_bucket_roll(self) -> None:
        builder = LiveSnapshotBuilder(["BTC", "ETH"], bucket_ms=60_000)

        first = builder.ingest_book(
            {
                "coin": "BTC",
                "time": 1_000,
                "levels": [
                    [{"px": "68000", "sz": "2.0", "n": 1}],
                    [{"px": "68002", "sz": "3.0", "n": 1}],
                ],
            }
        )
        self.assertEqual(first, [])
        builder.ingest_book(
            {
                "coin": "ETH",
                "time": 1_500,
                "levels": [
                    [{"px": "3000", "sz": "5.0", "n": 1}],
                    [{"px": "3001", "sz": "4.0", "n": 1}],
                ],
            }
        )
        builder.ingest_trade(
            {
                "coin": "BTC",
                "side": "B",
                "px": "68001",
                "sz": "0.5",
                "time": 20_000,
            }
        )
        records = builder.ingest_trade(
            {
                "coin": "ETH",
                "side": "A",
                "px": "3000.5",
                "sz": "1.2",
                "time": 61_000,
            }
        )

        self.assertEqual(len(records), 1)
        payload = records[0]
        self.assertEqual(payload["timestamp"], "1970-01-01T00:00:00Z")
        self.assertEqual(payload["regime_snapshot"]["ready"], True)
        self.assertEqual(len(payload["symbols"]), 2)
        btc = next(item for item in payload["symbols"] if item["symbol"] == "BTC")
        self.assertEqual(btc["bucket_trade_count"], 1)
        self.assertEqual(btc["source"], "hyperliquid_live_collector")

    def test_writer_appends_daily_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            writer = LiveSnapshotWriter(tmpdir)
            paths = writer.append_many(
                [
                    {"timestamp": "2026-04-05T08:00:00Z", "symbols": [], "regime_snapshot": {}},
                    {"timestamp": "2026-04-05T08:01:00Z", "symbols": [], "regime_snapshot": {}},
                ]
            )
            self.assertEqual(len(paths), 2)
            day_path = Path(tmpdir) / "2026-04-05.jsonl"
            self.assertTrue(day_path.exists())
            lines = day_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["timestamp"], "2026-04-05T08:00:00Z")


if __name__ == "__main__":
    unittest.main()
