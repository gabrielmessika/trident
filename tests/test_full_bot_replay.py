import json
import tempfile
import unittest
from pathlib import Path

from app.backtest.full_bot_replay import FullBotBacktestRunner
from app.settings import load_config


def _full_bot_record(timestamp: str, *, btc_price: float, eth_price: float, sol_price: float) -> dict[str, object]:
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


class FullBotReplayTests(unittest.TestCase):
    def test_full_bot_replay_writes_report_summary_and_history(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_a.enabled = True
        config.pod_b.enabled = True
        config.pod_c.enabled = True
        config.pod_b.paper_pause_outside_range = False

        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir = Path(tmpdir)
            input_path = temp_dir / "snapshots.jsonl"
            input_path.write_text(
                "\n".join(
                    json.dumps(record)
                    for record in [
                        _full_bot_record(
                            "2026-04-05T10:00:00Z",
                            btc_price=1000.0,
                            eth_price=100.0,
                            sol_price=200.0,
                        ),
                        _full_bot_record(
                            "2026-04-05T10:01:00Z",
                            btc_price=1005.0,
                            eth_price=100.4,
                            sol_price=200.8,
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            report_path = temp_dir / "full_bot_report.json"
            summary_path = temp_dir / "full_bot_summary.md"
            comparison_path = temp_dir / "full_bot_history.jsonl"

            result = FullBotBacktestRunner(config).run_jsonl(
                input_path=input_path,
                report_output=report_path,
                summary_output=summary_path,
                comparison_output=comparison_path,
            )

            self.assertEqual(result.records_processed, 2)
            self.assertEqual(result.duplicate_timestamps_skipped, 0)
            self.assertTrue(report_path.exists())
            self.assertTrue(summary_path.exists())
            self.assertTrue(comparison_path.exists())
            self.assertIn("directional_fees_usd", report_path.read_text(encoding="utf-8"))
            self.assertIn("TRIDENT full-bot backtest", summary_path.read_text(encoding="utf-8"))
            history_lines = comparison_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(history_lines), 1)
            history_entry = json.loads(history_lines[0])
            self.assertEqual(history_entry["records_processed"], 2)
            self.assertIn("pod_a_realized_pnl_usd", history_entry)
            self.assertIn("pod_b_total_fill_count", history_entry)
            self.assertIn("pod_c_realized_pnl_usd", history_entry)

    def test_full_bot_replay_merges_same_timestamp_snapshot_lines(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_a.enabled = True
        config.pod_b.enabled = True
        config.pod_c.enabled = True
        config.pod_b.paper_pause_outside_range = False
        config.hyperliquid.observation_universe = ["BTC", "PAXG"]

        crypto_record = _full_bot_record(
            "2026-04-05T10:00:00Z",
            btc_price=1000.0,
            eth_price=100.0,
            sol_price=200.0,
        )
        crypto_record["cluster_regime_snapshots"] = {
            "crypto": {
                "ready": True,
                "adx": 30.0,
                "atr_ratio": 1.15,
                "range_width_bps": 150.0,
                "structure_score": 0.55,
                "btc_impulse": False,
            }
        }
        tradfi_record = {
            "timestamp": "2026-04-05T10:00:00Z",
            "regime_snapshot": {
                "ready": True,
                "adx": 12.0,
                "atr_ratio": 0.3,
                "range_width_bps": 20.0,
                "structure_score": 0.1,
                "btc_impulse": False,
            },
            "cluster_regime_snapshots": {
                "gold": {
                    "ready": True,
                    "adx": 12.0,
                    "atr_ratio": 0.3,
                    "range_width_bps": 20.0,
                    "structure_score": 0.1,
                    "btc_impulse": False,
                }
            },
            "symbols": [
                {
                    "symbol": "PAXG",
                    "price": 2400.0,
                    "ema_fast": 2401.0,
                    "ema_slow": 2398.0,
                    "vwap_distance_bps": -1.0,
                    "structure_score": 0.12,
                    "funding_rate": 0.0,
                    "spread_bps": 0.3,
                    "btc_aligned": True,
                    "book_imbalance": 0.04,
                    "trade_flow_bias": 0.03,
                    "bucket_volume": 20.0,
                    "bucket_trade_count": 9,
                    "bucket_range_bps": 6.0,
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "snapshots.jsonl"
            input_path.write_text(
                json.dumps(crypto_record) + "\n" + json.dumps(tradfi_record) + "\n",
                encoding="utf-8",
            )

            result = FullBotBacktestRunner(config).run_jsonl(input_path=input_path)

        self.assertEqual(result.records_processed, 1)
        self.assertEqual(result.duplicate_timestamps_skipped, 0)
        self.assertEqual(result.first_timestamp, "2026-04-05T10:00:00Z")
        self.assertEqual(result.last_timestamp, "2026-04-05T10:00:00Z")


if __name__ == "__main__":
    unittest.main()
