import json
import tempfile
import unittest
from pathlib import Path

from app.backtest.snapshot_universe_compare import (
    SnapshotUniverseCompareRunner,
    parse_universe_scenarios,
)
from app.settings import load_config


class SnapshotUniverseCompareTests(unittest.TestCase):
    def test_runner_compares_multiple_universes_on_same_snapshot_stream(self) -> None:
        config = load_config("config/trident.toml")
        runner = SnapshotUniverseCompareRunner(config)

        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "snapshots.jsonl"
            input_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
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
                                        "symbol": "BTC",
                                        "price": 68000.0,
                                        "ema_fast": 67900.0,
                                        "ema_slow": 67500.0,
                                        "vwap_distance_bps": -8.0,
                                        "structure_score": 0.62,
                                        "funding_rate": 0.0,
                                        "spread_bps": 0.8,
                                        "btc_aligned": True,
                                    },
                                    {
                                        "symbol": "SPX",
                                        "price": 5000.0,
                                        "ema_fast": 4992.0,
                                        "ema_slow": 4970.0,
                                        "vwap_distance_bps": -6.0,
                                        "structure_score": 0.58,
                                        "funding_rate": 0.0,
                                        "spread_bps": 1.0,
                                        "btc_aligned": False,
                                    },
                                ],
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-04-05T00:30:00Z",
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
                                        "symbol": "BTC",
                                        "price": 67600.0,
                                        "ema_fast": 67700.0,
                                        "ema_slow": 67400.0,
                                        "vwap_distance_bps": -6.0,
                                        "structure_score": 0.58,
                                        "funding_rate": 0.0,
                                        "spread_bps": 0.9,
                                        "btc_aligned": True,
                                    },
                                    {
                                        "symbol": "SPX",
                                        "price": 5040.0,
                                        "ema_fast": 5032.0,
                                        "ema_slow": 5005.0,
                                        "vwap_distance_bps": -2.0,
                                        "structure_score": 0.62,
                                        "funding_rate": 0.0,
                                        "spread_bps": 1.0,
                                        "btc_aligned": False,
                                    },
                                ],
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            report_output = Path(tmpdir) / "compare.json"

            result = runner.run(
                input_path=input_path,
                scenarios=parse_universe_scenarios("crypto_only=BTC;mixed=BTC,SPX"),
                report_output=report_output,
            )

            self.assertEqual(len(result.scenarios), 2)
            self.assertTrue(report_output.exists())
            mixed = next(item for item in result.scenarios if item.scenario.name == "mixed")
            self.assertIn("index", mixed.comparative_summary["by_cluster"])
            self.assertGreaterEqual(
                mixed.comparative_summary["by_cluster"]["index"]["closed_trade_count"],
                1,
            )
            self.assertGreater(
                mixed.comparative_summary["by_cluster"]["index"]["expectancy_usd"],
                0.0,
            )
            self.assertGreaterEqual(mixed.backtest.signal_count, 2)


if __name__ == "__main__":
    unittest.main()
