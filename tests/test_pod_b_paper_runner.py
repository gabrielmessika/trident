import json
import tempfile
import unittest
from pathlib import Path

from app.settings import load_config
from app.trident.pod_b import PassivbotManager
from app.trident.pod_b.paper_live_runner import PodBPaperLiveRunner
from app.trident.pod_b.paper_runner import PodBPaperRunner
from app.trident.types import PodAllocation, PodName


def _snapshot_record(timestamp: str, symbol: str, price: float) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "regime_snapshot": {
            "ready": True,
            "adx": 12.0,
            "atr_ratio": 0.4,
            "range_width_bps": 30.0,
            "structure_score": 0.0,
            "btc_impulse": False,
        },
        "symbols": [
            {
                "symbol": symbol,
                "price": price,
                "ema_fast": price,
                "ema_slow": price,
                "vwap_distance_bps": 0.0,
                "structure_score": 0.0,
                "funding_rate": 0.0,
                "spread_bps": 1.0,
                "btc_aligned": True,
                "book_imbalance": 0.0,
                "trade_flow_bias": 0.0,
                "bucket_volume": 10.0,
                "bucket_trade_count": 5,
                "bucket_range_bps": 10.0,
                "source": "test",
            }
        ],
    }


class PodBPaperRunnerTests(unittest.TestCase):
    def test_paper_runner_reloads_runtime_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "runtime.json"
            config_path.write_text(
                json.dumps(
                    {
                        "live": {"leverage": 3},
                        "trident": {
                            "managed_symbols": ["DOGE"],
                            "target_usd": 200.0,
                            "paper_quote_width_bps": 6.0,
                            "paper_order_size_pct": 0.25,
                            "paper_max_inventory_skew_pct": 1.0,
                            "paper_maker_fee_bps": 0.0,
                            "paper_recent_fills_limit": 20,
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            runner = PodBPaperRunner(config_path)
            self.assertEqual(runner.managed_symbols, ["DOGE"])
            self.assertEqual(runner.target_usd, 200.0)

            config_path.write_text(
                json.dumps(
                    {
                        "live": {"leverage": 3},
                        "trident": {
                            "managed_symbols": ["DOGE", "XRP"],
                            "target_usd": 120.0,
                            "paper_quote_width_bps": 9.0,
                            "paper_order_size_pct": 0.1,
                            "paper_max_inventory_skew_pct": 0.5,
                            "paper_maker_fee_bps": 0.0,
                            "paper_recent_fills_limit": 5,
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            runner.reload_runtime_config()

            self.assertEqual(runner.managed_symbols, ["DOGE", "XRP"])
            self.assertEqual(runner.target_usd, 120.0)
            self.assertEqual(runner.engine.managed_symbols, ["DOGE", "XRP"])
            self.assertEqual(runner.engine.target_usd, 120.0)
            self.assertEqual(runner.engine.config.paper_quote_width_bps, 9.0)

    def test_paper_runner_writes_status_with_positions_orders_and_fills(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_b.enabled = True
        config.pod_b.symbols = ["DOGE"]
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "runtime" / "passivbot" / "live.json"
            config.pod_b.passivbot_config_path = str(config_path)
            manager = PassivbotManager(config)
            manager.sync(
                allocation=PodAllocation(
                    pod=PodName.POD_B,
                    target_pct=0.2,
                    target_usd=200.0,
                ),
                owned_symbols=["DOGE"],
            )

            input_path = Path(tmpdir) / "snapshots.jsonl"
            report_path = Path(tmpdir) / "paper_report.json"
            journal_path = Path(tmpdir) / "paper_fills.jsonl"
            input_path.write_text(
                "\n".join(
                    json.dumps(record)
                    for record in [
                        _snapshot_record("2026-04-05T10:00:00Z", "DOGE", 100.0),
                        _snapshot_record("2026-04-05T10:01:00Z", "DOGE", 99.9),
                        _snapshot_record("2026-04-05T10:02:00Z", "DOGE", 100.1),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            result = PodBPaperRunner(config_path).run(
                input_path=input_path,
                report_output=report_path,
                journal_output=journal_path,
            )

            self.assertEqual(result.records_processed, 3)
            self.assertEqual(result.fills_emitted, 2)
            self.assertEqual(result.total_fill_count, 2)
            self.assertGreater(result.realized_pnl_usd, 0.0)
            self.assertTrue(Path(result.status_path).exists())
            self.assertTrue(report_path.exists())
            self.assertTrue(journal_path.exists())

            status_payload = json.loads(Path(result.status_path).read_text(encoding="utf-8"))
            self.assertEqual(status_payload["process_state"], "stopped")
            self.assertEqual(status_payload["last_sync_reason"], "paper_runner_completed")
            self.assertEqual(status_payload["managed_symbols"], ["DOGE"])
            self.assertEqual(status_payload["total_fill_count"], 2)
            self.assertEqual(len(status_payload["recent_fills"]), 2)
            self.assertEqual(status_payload["total_open_order_count"], 2)

            journal_lines = journal_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(journal_lines), 2)
            first_event = json.loads(journal_lines[0])
            self.assertEqual(first_event["event_type"], "pod_b_fill")

            parsed_status = manager.sync(
                allocation=PodAllocation(
                    pod=PodName.POD_B,
                    target_pct=0.2,
                    target_usd=200.0,
                ),
                owned_symbols=["DOGE"],
            )
            self.assertEqual(parsed_status.total_fill_count, 2)
            self.assertEqual(len(parsed_status.recent_fills), 2)
            self.assertEqual(parsed_status.process_state, "stopped")
            self.assertIsNotNone(result.report)
            self.assertEqual(result.report["fills_by_symbol"]["DOGE"], 2)
            self.assertEqual(result.report["fills_by_date"]["2026-04-05"], 2)
            self.assertIn("inventory_skew_by_symbol", result.report)

    def test_paper_live_runner_writes_report_output(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_b.enabled = True
        config.pod_b.symbols = ["DOGE"]
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "runtime" / "passivbot" / "live.json"
            config.pod_b.passivbot_config_path = str(config_path)
            manager = PassivbotManager(config)
            manager.sync(
                allocation=PodAllocation(
                    pod=PodName.POD_B,
                    target_pct=0.2,
                    target_usd=200.0,
                ),
                owned_symbols=["DOGE"],
            )
            input_dir = Path(tmpdir) / "snapshots"
            input_dir.mkdir(parents=True, exist_ok=True)
            (input_dir / "2026-04-05.jsonl").write_text(
                "\n".join(
                    json.dumps(record)
                    for record in [
                        _snapshot_record("2026-04-05T10:00:00Z", "DOGE", 100.0),
                        _snapshot_record("2026-04-05T10:01:00Z", "DOGE", 99.9),
                        _snapshot_record("2026-04-05T10:02:00Z", "DOGE", 100.1),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            report_path = Path(tmpdir) / "live_report.json"

            stats = PodBPaperLiveRunner(config_path).run_live(
                input_path=input_dir,
                poll_seconds=0.01,
                max_idle_loops=1,
                report_output=report_path,
            )

            self.assertEqual(stats.records_processed, 3)
            self.assertEqual(stats.fills_emitted, 2)
            self.assertEqual(stats.report_path, str(report_path))
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["fills_by_symbol"]["DOGE"], 2)
            self.assertEqual(payload["fills_by_date"]["2026-04-05"], 2)


if __name__ == "__main__":
    unittest.main()
