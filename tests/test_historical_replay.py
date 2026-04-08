import json
import tempfile
import unittest
from pathlib import Path

from app.backtest.candle_converter import CandleToSnapshotConverter
from app.backtest.historical_replay import HistoricalReplayRunner, parse_dates
from app.settings import load_config


class ParseDatesTests(unittest.TestCase):
    def test_single_date(self) -> None:
        dates = parse_dates(date_from="2026-01-01", date_to=None)
        self.assertEqual(dates, ["2026-01-01"])

    def test_date_range(self) -> None:
        dates = parse_dates(date_from="2026-01-01", date_to="2026-01-03")
        self.assertEqual(dates, ["2026-01-01", "2026-01-02", "2026-01-03"])

    def test_invalid_range_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_dates(date_from="2026-01-05", date_to="2026-01-01")


class HistoricalReplaySkipFetchTests(unittest.TestCase):
    """Tests the replay pipeline with skip_fetch=True and pre-built candle data."""

    def test_replay_with_local_candles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            candle_dir = base / "candles"
            snapshot_dir = base / "snapshots"
            report_path = base / "report.json"

            # Create local candle data
            btc_dir = candle_dir / "1h" / "BTC"
            btc_dir.mkdir(parents=True)
            candles = [
                {"t": 1_767_225_600_000, "o": "50000", "h": "50100", "l": "49900", "c": "50050", "v": "10.5", "n": 150},
                {"t": 1_767_229_200_000, "o": "50050", "h": "50200", "l": "49950", "c": "50150", "v": "12.3", "n": 180},
                {"t": 1_767_232_800_000, "o": "50150", "h": "50300", "l": "50050", "c": "50250", "v": "11.0", "n": 160},
            ]
            with (btc_dir / "2026-01-01.jsonl").open("w") as f:
                for c in candles:
                    f.write(json.dumps(c) + "\n")

            config = load_config("config/trident.toml")
            runner = HistoricalReplayRunner(config)
            result = runner.run(
                dates=["2026-01-01"],
                coins=["BTC"],
                interval="1h",
                candle_dir=candle_dir,
                snapshot_dir=snapshot_dir,
                report_output=report_path,
                skip_fetch=True,
                skip_funding=True,
            )

            self.assertEqual(result.snapshot_files_written, 1)
            self.assertEqual(result.snapshot_records_written, 3)
            self.assertEqual(result.fetch_candles_total, 0)
            self.assertEqual(result.backtest.records_processed, 3)
            self.assertTrue(report_path.exists())

            report = json.loads(report_path.read_text())
            self.assertIn("backtest", report)
            self.assertEqual(report["interval"], "1h")


if __name__ == "__main__":
    unittest.main()
