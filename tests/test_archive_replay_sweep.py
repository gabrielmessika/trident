import json
import tempfile
import unittest
from pathlib import Path

from app.backtest.archive_replay_sweep import (
    ArchiveReplaySweepRunner,
    default_scenarios,
)
from app.settings import load_config


class ArchiveReplaySweepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config("config/trident.toml")
        self.runner = ArchiveReplaySweepRunner(self.config)

    def test_sweep_runs_multiple_scenarios_and_writes_report(self) -> None:
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

            report_output = Path(tmpdir) / "sweep_report.json"
            result = self.runner.run(
                data_dir=archive_dir,
                dates=["2026-04-01"],
                coins=["BTC", "ETH"],
                scenarios=default_scenarios(
                    reference_equity_usd=500.0,
                    leverages=[1.0, 2.0, 3.0, 5.0, 10.0],
                ),
                report_output=report_output,
            )

            self.assertEqual(result.snapshot_files_written, 1)
            self.assertEqual(len(result.scenarios), 5)
            self.assertEqual(
                [scenario.scenario.name for scenario in result.scenarios],
                ["500usd_1x", "500usd_2x", "500usd_3x", "500usd_5x", "500usd_10x"],
            )
            self.assertIn(
                result.recommended_scenario,
                {"500usd_1x", "500usd_2x", "500usd_3x", "500usd_5x", "500usd_10x"},
            )
            self.assertTrue(report_output.exists())
            payload = json.loads(report_output.read_text(encoding="utf-8"))
            self.assertEqual(payload["recommended_scenario"], result.recommended_scenario)
            self.assertEqual(len(payload["scenarios"]), 5)
            self.assertEqual(payload["scenarios"][0]["scenario"]["reference_equity_usd"], 500.0)
            self.assertIn("realized_pnl_pct", payload["scenarios"][0])
            self.assertIn("max_open_expected_loss_pct", payload["scenarios"][0])

    def _write_jsonl(self, path: Path, rows: list[dict[str, object]]) -> None:
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")


if __name__ == "__main__":
    unittest.main()
