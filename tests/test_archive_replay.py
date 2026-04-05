import json
import tempfile
import unittest
from pathlib import Path

from app.backtest.archive_replay import ArchiveReplayRunner, parse_dates
from app.settings import load_config


class ArchiveReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config("config/trident.toml")
        self.runner = ArchiveReplayRunner(self.config)

    def test_parse_dates_single_day_and_range(self) -> None:
        self.assertEqual(parse_dates(date_from="2026-04-01", date_to=None), ["2026-04-01"])
        self.assertEqual(
            parse_dates(date_from="2026-04-01", date_to="2026-04-03"),
            ["2026-04-01", "2026-04-02", "2026-04-03"],
        )

    def test_archive_replay_converts_and_runs_backtest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_dir = Path(tmpdir) / "archive"
            for side in ("l2", "trades"):
                for coin in ("BTC", "ETH"):
                    (archive_dir / side / coin).mkdir(parents=True, exist_ok=True)

            self._write_jsonl(
                archive_dir / "l2" / "BTC" / "2026-04-01.jsonl",
                [
                    {
                        "timestamp": 1775037040544,
                        "coin": "BTC",
                        "best_bid": 68610.0,
                        "best_ask": 68611.0,
                        "bid_depth_10bps": 3043053.19,
                        "ask_depth_10bps": 3502011.79,
                        "spread_bps": 0.1457,
                        "mid": 68610.5,
                    }
                ],
            )
            self._write_jsonl(
                archive_dir / "l2" / "ETH" / "2026-04-01.jsonl",
                [
                    {
                        "timestamp": 1775037040544,
                        "coin": "ETH",
                        "best_bid": 1800.0,
                        "best_ask": 1800.4,
                        "bid_depth_10bps": 903053.19,
                        "ask_depth_10bps": 502011.79,
                        "spread_bps": 2.2,
                        "mid": 1800.2,
                    }
                ],
            )
            self._write_jsonl(
                archive_dir / "trades" / "BTC" / "2026-04-01.jsonl",
                [
                    {
                        "timestamp": 1775037039202,
                        "coin": "BTC",
                        "price": 68600.0,
                        "size": 0.01,
                        "is_buy": True,
                    }
                ],
            )
            self._write_jsonl(
                archive_dir / "trades" / "ETH" / "2026-04-01.jsonl",
                [
                    {
                        "timestamp": 1775037039202,
                        "coin": "ETH",
                        "price": 1800.1,
                        "size": 1.2,
                        "is_buy": True,
                    }
                ],
            )

            snapshot_dir = Path(tmpdir) / "snapshots"
            report_output = Path(tmpdir) / "report.json"
            journal_output = Path(tmpdir) / "journal.jsonl"

            result = self.runner.run(
                data_dir=archive_dir,
                dates=["2026-04-01"],
                coins=["BTC", "ETH"],
                snapshot_dir=snapshot_dir,
                journal_output=journal_output,
                report_output=report_output,
            )

            self.assertEqual(result.snapshot_files_written, 1)
            self.assertEqual(result.snapshot_records_written, 1)
            self.assertTrue((snapshot_dir / "2026-04-01.jsonl").exists())
            self.assertTrue(report_output.exists())
            self.assertTrue(journal_output.exists())
            payload = json.loads(report_output.read_text(encoding="utf-8"))
            self.assertEqual(payload["dates"], ["2026-04-01"])
            self.assertEqual(payload["snapshot_records_written"], 1)
            self.assertIn("backtest", payload)
            self.assertIn("trades_by_symbol", payload["backtest"])

    def _write_jsonl(self, path: Path, rows: list[dict[str, object]]) -> None:
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")


if __name__ == "__main__":
    unittest.main()
