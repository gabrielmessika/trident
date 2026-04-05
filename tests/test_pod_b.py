import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from app.settings import load_config
from app.trident.pod_b import PassivbotConfig, PassivbotConfigRenderer, PassivbotManager
from app.trident.types import PodAllocation, PodName


class PodBTests(unittest.TestCase):
    def test_renderer_builds_minimal_passivbot_live_config(self) -> None:
        renderer = PassivbotConfigRenderer()
        payload = renderer.render(
            PassivbotConfig(
                config_path="runtime/passivbot/live.json",
                approved_coins=["DOGE", "XRP"],
                target_pct=0.7,
                target_usd=700.0,
            )
        )

        self.assertEqual(payload["live"]["approved_coins"], ["DOGE", "XRP"])
        self.assertEqual(payload["live"]["time_in_force"], "post_only")
        self.assertEqual(payload["trident"]["target_usd"], 700.0)

    def test_manager_sync_writes_runtime_config(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_b.enabled = True
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "runtime" / "passivbot" / "live.json"
            config.pod_b.passivbot_config_path = str(config_path)
            manager = PassivbotManager(config)

            status = manager.sync(
                allocation=PodAllocation(
                    pod=PodName.POD_B,
                    target_pct=0.7,
                    target_usd=700.0,
                ),
                owned_symbols=["DOGE", "XRP"],
            )

            self.assertTrue(config_path.exists())
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["live"]["approved_coins"], ["DOGE", "XRP"])
            self.assertEqual(status.managed_symbols, ["DOGE", "XRP"])
            self.assertEqual(status.last_sync_reason, "config_rendered")
            self.assertEqual(status.total_position_count, 0)
            self.assertEqual(status.total_open_order_count, 0)
            self.assertEqual(status.total_fill_count, 0)
            self.assertEqual(status.realized_pnl_usd, 0.0)
            self.assertEqual(len(status.inventory), 2)
            self.assertEqual(status.inventory[0].target_notional_usd, 350.0)

    def test_manager_reads_status_file_when_present(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_b.enabled = True
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "runtime" / "passivbot" / "live.json"
            config.pod_b.passivbot_config_path = str(config_path)
            manager = PassivbotManager(config)
            status_path = manager.status_path(config_path)
            status_path.parent.mkdir(parents=True, exist_ok=True)
            status_path.write_text(
                json.dumps(
                    {
                        "process_state": "running",
                        "managed_symbols": ["DOGE"],
                        "target_usd": 123.0,
                        "last_sync_reason": "external_wrapper_running",
                        "positions": [
                            {
                                "symbol": "DOGE",
                                "side": "long",
                                "size": 1200.0,
                                "entry_price": 0.18,
                                "mark_price": 0.181,
                                "notional_usd": 217.2,
                                "unrealized_pnl_usd": 1.2,
                            }
                        ],
                        "open_orders": [
                            {
                                "symbol": "DOGE",
                                "side": "buy",
                                "price": 0.179,
                                "size": 500.0,
                            }
                        ],
                        "recent_fills": [
                            {
                                "symbol": "DOGE",
                                "side": "buy",
                                "action": "fill",
                                "price": 0.179,
                                "size": 500.0,
                                "notional_usd": 89.5,
                                "fee_usd": 0.0,
                                "timestamp": "2026-04-05T10:00:00Z",
                            }
                        ],
                        "total_fill_count": 1,
                        "realized_pnl_usd": 2.5,
                    }
                ),
                encoding="utf-8",
            )

            status = manager.sync(
                allocation=PodAllocation(
                    pod=PodName.POD_B,
                    target_pct=0.2,
                    target_usd=200.0,
                ),
                owned_symbols=["DOGE", "XRP"],
            )

            self.assertEqual(status.process_state, "running")
            self.assertEqual(status.managed_symbols, ["DOGE"])
            self.assertEqual(status.target_usd, 123.0)
            self.assertEqual(status.total_position_count, 1)
            self.assertEqual(status.total_open_order_count, 1)
            self.assertEqual(status.total_fill_count, 1)
            self.assertEqual(status.realized_pnl_usd, 2.5)
            self.assertEqual(status.positions[0].symbol, "DOGE")
            self.assertEqual(status.inventory[0].symbol, "DOGE")
            self.assertTrue(status.inventory[0].has_position)
            self.assertEqual(status.recent_fills[0].symbol, "DOGE")

    def test_manager_can_start_and_stop_wrapper_process(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_b.enabled = True
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "runtime" / "passivbot" / "live.json"
            config.pod_b.passivbot_config_path = str(config_path)
            manager = PassivbotManager(config)

            running_status = manager.start(
                allocation=PodAllocation(
                    pod=PodName.POD_B,
                    target_pct=0.2,
                    target_usd=200.0,
                ),
                owned_symbols=["DOGE"],
                command=[
                    sys.executable,
                    "-c",
                    "import time; time.sleep(60)",
                ],
            )
            try:
                self.assertEqual(running_status.process_state, "running")
                self.assertIsNotNone(running_status.pid)
                self.assertEqual(running_status.last_sync_reason, "started_by_trident")
                self.assertTrue(Path(running_status.stdout_path).exists())
                self.assertTrue(Path(running_status.stderr_path).exists())

                stopped_status = manager.stop()
                self.assertEqual(stopped_status.process_state, "stopped")
                self.assertIsNone(stopped_status.pid)
                self.assertEqual(stopped_status.last_sync_reason, "stopped_by_trident")
            finally:
                manager.stop()

    def test_manager_can_launch_real_paper_live_runner_command(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_b.enabled = True
        config.pod_b.symbols = ["DOGE"]
        config.pod_b.launch_workdir = "/workspaces/trident"
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "runtime" / "passivbot" / "live.json"
            snapshot_dir = Path(tmpdir) / "snapshots"
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            (snapshot_dir / "2026-04-05.jsonl").write_text(
                json.dumps(
                    {
                        "timestamp": "2026-04-05T10:00:00Z",
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
                                "symbol": "DOGE",
                                "price": 100.0,
                                "ema_fast": 100.0,
                                "ema_slow": 100.0,
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
                )
                + "\n",
                encoding="utf-8",
            )
            config.pod_b.passivbot_config_path = str(config_path)
            config.pod_b.launch_command = [
                sys.executable,
                "-m",
                "app.trident.pod_b.paper_live_runner",
                "--config-path",
                "{config}",
                "--input",
                str(snapshot_dir),
                "--poll-seconds",
                "0.05",
                "--max-runtime-seconds",
                "60",
            ]
            manager = PassivbotManager(config)

            running_status = manager.start(
                allocation=PodAllocation(
                    pod=PodName.POD_B,
                    target_pct=0.2,
                    target_usd=200.0,
                ),
                owned_symbols=["DOGE"],
            )
            try:
                time.sleep(0.25)
                synced_status = manager.sync(
                    allocation=PodAllocation(
                        pod=PodName.POD_B,
                        target_pct=0.2,
                        target_usd=200.0,
                    ),
                    owned_symbols=["DOGE"],
                )
                self.assertEqual(running_status.process_state, "running")
                self.assertIsNotNone(running_status.pid)
                self.assertEqual(synced_status.process_state, "running")
                self.assertGreaterEqual(synced_status.total_open_order_count, 1)
            finally:
                manager.stop()

    def test_manager_marks_stale_running_status_as_stopped(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_b.enabled = True
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "runtime" / "passivbot" / "live.json"
            config.pod_b.passivbot_config_path = str(config_path)
            manager = PassivbotManager(config)
            status_path = manager.status_path(config_path)
            status_path.parent.mkdir(parents=True, exist_ok=True)
            status_path.write_text(
                json.dumps(
                    {
                        "process_state": "running",
                        "managed_symbols": ["DOGE"],
                        "target_usd": 123.0,
                        "last_sync_reason": "external_wrapper_running",
                        "pid": 999999,
                    }
                ),
                encoding="utf-8",
            )

            status = manager.sync(
                allocation=PodAllocation(
                    pod=PodName.POD_B,
                    target_pct=0.2,
                    target_usd=200.0,
                ),
                owned_symbols=["DOGE", "XRP"],
            )

            self.assertEqual(status.process_state, "stopped")
            self.assertIsNone(status.pid)
            self.assertEqual(status.last_sync_reason, "process_exited")
            self.assertEqual(status.total_position_count, 0)
            self.assertEqual(len(status.inventory), 2)

    def test_manager_builds_default_inventory_from_positions_and_orders(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_b.enabled = True
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "runtime" / "passivbot" / "live.json"
            config.pod_b.passivbot_config_path = str(config_path)
            manager = PassivbotManager(config)
            status_path = manager.status_path(config_path)
            status_path.parent.mkdir(parents=True, exist_ok=True)
            status_path.write_text(
                json.dumps(
                    {
                        "process_state": "running",
                        "managed_symbols": ["DOGE", "XRP"],
                        "target_usd": 300.0,
                        "positions": [
                            {
                                "symbol": "DOGE",
                                "side": "short",
                                "size": 1000.0,
                                "entry_price": 0.2,
                                "mark_price": 0.19,
                                "notional_usd": 190.0,
                            }
                        ],
                        "open_orders": [
                            {"symbol": "DOGE", "side": "buy", "price": 0.189, "size": 200.0},
                            {"symbol": "XRP", "side": "sell", "price": 0.55, "size": 300.0},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            status = manager.sync(
                allocation=PodAllocation(
                    pod=PodName.POD_B,
                    target_pct=0.3,
                    target_usd=300.0,
                ),
                owned_symbols=["DOGE", "XRP"],
            )

            self.assertEqual(status.total_notional_usd, 190.0)
            self.assertEqual(status.total_open_order_count, 2)
            inventory = {item.symbol: item for item in status.inventory}
            self.assertEqual(inventory["DOGE"].target_notional_usd, 150.0)
            self.assertEqual(inventory["DOGE"].current_notional_usd, 190.0)
            self.assertEqual(inventory["DOGE"].open_order_count, 1)
            self.assertEqual(inventory["XRP"].open_order_count, 1)


if __name__ == "__main__":
    unittest.main()
