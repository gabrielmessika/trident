import json
import tempfile
import unittest
from pathlib import Path

from app.backtest.full_bot_experiment_sweep import (
    FullBotExperimentScenario,
    FullBotExperimentSweepRunner,
)
from app.settings import load_config


def _record(timestamp: str, *, btc_price: float, eth_price: float, sol_price: float) -> dict[str, object]:
    return {
        "timestamp": timestamp,
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
                "symbol": "BTC",
                "price": btc_price,
                "ema_fast": btc_price * 0.999,
                "ema_slow": btc_price * 0.996,
                "vwap_distance_bps": -8.0,
                "structure_score": 0.62,
                "funding_rate": 0.0,
                "spread_bps": 1.1,
                "btc_aligned": True,
                "book_imbalance": 0.18,
                "trade_flow_bias": 0.15,
                "bucket_volume": 150.0,
                "bucket_trade_count": 50,
                "bucket_range_bps": 24.0,
                "source": "test",
            },
            {
                "symbol": "ETH",
                "price": eth_price,
                "ema_fast": eth_price * 0.999,
                "ema_slow": eth_price * 0.994,
                "vwap_distance_bps": -6.0,
                "structure_score": 0.58,
                "funding_rate": 0.0001,
                "spread_bps": 1.2,
                "btc_aligned": True,
                "book_imbalance": 0.14,
                "trade_flow_bias": 0.12,
                "bucket_volume": 120.0,
                "bucket_trade_count": 40,
                "bucket_range_bps": 28.0,
                "source": "test",
            },
            {
                "symbol": "SOL",
                "price": sol_price,
                "ema_fast": sol_price * 0.998,
                "ema_slow": sol_price * 0.997,
                "vwap_distance_bps": -10.0,
                "structure_score": 0.71,
                "funding_rate": 0.0002,
                "spread_bps": 1.4,
                "btc_aligned": True,
                "book_imbalance": 0.2,
                "trade_flow_bias": 0.18,
                "bucket_volume": 180.0,
                "bucket_trade_count": 70,
                "bucket_range_bps": 34.0,
                "source": "test",
            },
        ],
    }


class FullBotExperimentSweepTests(unittest.TestCase):
    def test_sweep_runs_multiple_radical_scenarios(self) -> None:
        config = load_config("config/trident.toml")
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir = Path(tmpdir)
            input_path = temp_dir / "snapshots.jsonl"
            input_path.write_text(
                "\n".join(
                    json.dumps(item)
                    for item in [
                        _record("2026-04-05T10:00:00Z", btc_price=1000.0, eth_price=100.0, sol_price=200.0),
                        _record("2026-04-05T10:01:00Z", btc_price=1005.0, eth_price=100.4, sol_price=200.8),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            report_output = temp_dir / "sweep.json"
            report_dir = temp_dir / "scenario_reports"
            scenarios = [
                FullBotExperimentScenario(
                    name="current",
                    description="Reference",
                ),
                FullBotExperimentScenario(
                    name="pod_a_only",
                    description="A only",
                    pod_b_enabled=False,
                    pod_c_enabled=False,
                ),
            ]

            result = FullBotExperimentSweepRunner(config).run(
                input_path=input_path,
                scenarios=scenarios,
                report_output=report_output,
                report_dir=report_dir,
            )

            self.assertEqual(len(result.scenarios), 2)
            self.assertIn(result.recommended_scenario, {"current", "pod_a_only"})
            self.assertTrue(report_output.exists())
            self.assertTrue((report_dir / "current.json").exists())
            self.assertTrue((report_dir / "current.md").exists())
            payload = json.loads(report_output.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["scenarios"]), 2)
            self.assertEqual(payload["scenarios"][0]["scenario"]["name"], "current")


if __name__ == "__main__":
    unittest.main()
