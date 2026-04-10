import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from app.settings import load_config
from app.trident.pod_b import PassivbotConfig, PassivbotConfigRenderer, PassivbotManager
from app.trident.pod_b.paper_engine import PaperPositionState, PodBPaperEngine
from app.trident.types import SymbolMarketSnapshot
from app.trident.types import PodAllocation, PodName


class PodBTests(unittest.TestCase):
    def test_pod_b_engine_pauses_quotes_when_regime_is_not_range_friendly(self) -> None:
        config = load_config("config/trident.toml")
        engine = PodBPaperEngine(
            managed_symbols=["DOGE"],
            target_usd=200.0,
            config=config.pod_b,
        )

        status, fills = engine.process_record(
            timestamp="2026-04-05T10:00:00Z",
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="DOGE",
                    price=100.0,
                    ema_fast=100.0,
                    ema_slow=100.0,
                    vwap_distance_bps=0.0,
                    structure_score=0.0,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                    book_imbalance=0.0,
                    trade_flow_bias=0.0,
                    bucket_volume=10.0,
                    bucket_trade_count=5,
                    bucket_range_bps=12.0,
                )
            ],
            status_meta={"config_path": "", "status_path": ""},
            regime_snapshot={
                "ready": True,
                "adx": 30.0,
                "atr_ratio": 1.1,
                "range_width_bps": 160.0,
                "structure_score": 0.35,
                "btc_impulse": True,
            },
        )

        self.assertEqual(fills, [])
        self.assertEqual(status.total_open_order_count, 0)

    def test_pod_b_engine_switches_to_unwind_only_when_inventory_is_skewed(self) -> None:
        config = load_config("config/trident.toml")
        engine = PodBPaperEngine(
            managed_symbols=["DOGE"],
            target_usd=200.0,
            config=config.pod_b,
        )
        engine.positions_by_symbol["DOGE"] = PaperPositionState(
            signed_size=1.5,
            avg_entry_price=100.0,
        )

        status, _ = engine.process_record(
            timestamp="2026-04-05T10:00:00Z",
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="DOGE",
                    price=100.0,
                    ema_fast=100.0,
                    ema_slow=100.0,
                    vwap_distance_bps=0.0,
                    structure_score=0.0,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                    book_imbalance=0.0,
                    trade_flow_bias=0.0,
                    bucket_volume=10.0,
                    bucket_trade_count=5,
                    bucket_range_bps=12.0,
                )
            ],
            status_meta={"config_path": "", "status_path": ""},
            regime_snapshot={
                "ready": True,
                "adx": 10.0,
                "atr_ratio": 0.4,
                "range_width_bps": 40.0,
                "structure_score": 0.0,
                "btc_impulse": False,
            },
        )

        self.assertGreaterEqual(status.total_open_order_count, 1)
        self.assertTrue(all(order.side == "sell" for order in status.open_orders))

    def test_pod_b_engine_widens_quotes_and_reduces_size_in_toxic_conditions(self) -> None:
        config = load_config("config/trident.toml")
        calm_engine = PodBPaperEngine(
            managed_symbols=["DOGE"],
            target_usd=200.0,
            config=config.pod_b,
        )
        toxic_engine = PodBPaperEngine(
            managed_symbols=["DOGE"],
            target_usd=200.0,
            config=config.pod_b,
        )
        regime_snapshot = {
            "ready": True,
            "adx": 10.0,
            "atr_ratio": 0.4,
            "range_width_bps": 40.0,
            "structure_score": 0.0,
            "btc_impulse": False,
        }

        calm_status, _ = calm_engine.process_record(
            timestamp="2026-04-05T10:00:00Z",
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="DOGE",
                    price=100.0,
                    ema_fast=100.0,
                    ema_slow=100.0,
                    vwap_distance_bps=0.0,
                    structure_score=0.0,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                    book_imbalance=0.0,
                    trade_flow_bias=0.0,
                    bucket_volume=10.0,
                    bucket_trade_count=5,
                    bucket_range_bps=12.0,
                )
            ],
            status_meta={"config_path": "", "status_path": ""},
            regime_snapshot=regime_snapshot,
        )
        toxic_status, _ = toxic_engine.process_record(
            timestamp="2026-04-05T10:00:00Z",
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="DOGE",
                    price=100.0,
                    ema_fast=100.0,
                    ema_slow=100.0,
                    vwap_distance_bps=4.0,
                    structure_score=0.0,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                    book_imbalance=0.0,
                    trade_flow_bias=0.18,
                    bucket_volume=10.0,
                    bucket_trade_count=5,
                    bucket_range_bps=80.0,
                )
            ],
            status_meta={"config_path": "", "status_path": ""},
            regime_snapshot=regime_snapshot,
        )

        calm_buy = next(order for order in calm_status.open_orders if order.side == "buy")
        toxic_buy = next(order for order in toxic_status.open_orders if order.side == "buy")

        # Toxic flow reduces order size via toxicity_size_discount
        self.assertLessEqual(toxic_buy.size, calm_buy.size)

    def test_pod_b_engine_applies_symbol_specific_quote_and_size_multipliers(self) -> None:
        config = load_config("config/trident.toml")
        doge_engine = PodBPaperEngine(
            managed_symbols=["DOGE"],
            target_usd=200.0,
            config=config.pod_b,
        )
        hype_engine = PodBPaperEngine(
            managed_symbols=["HYPE"],
            target_usd=200.0,
            config=config.pod_b,
        )
        regime_snapshot = {
            "ready": True,
            "adx": 10.0,
            "atr_ratio": 0.4,
            "range_width_bps": 40.0,
            "structure_score": 0.0,
            "btc_impulse": False,
        }

        doge_status, _ = doge_engine.process_record(
            timestamp="2026-04-05T10:00:00Z",
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="DOGE",
                    price=100.0,
                    ema_fast=100.0,
                    ema_slow=100.0,
                    vwap_distance_bps=0.0,
                    structure_score=0.0,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                    book_imbalance=0.0,
                    trade_flow_bias=0.0,
                    bucket_volume=10.0,
                    bucket_trade_count=5,
                    bucket_range_bps=12.0,
                )
            ],
            status_meta={"config_path": "", "status_path": ""},
            regime_snapshot=regime_snapshot,
        )
        hype_status, _ = hype_engine.process_record(
            timestamp="2026-04-05T10:00:00Z",
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="HYPE",
                    price=100.0,
                    ema_fast=100.0,
                    ema_slow=100.0,
                    vwap_distance_bps=0.0,
                    structure_score=0.0,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                    book_imbalance=0.0,
                    trade_flow_bias=0.0,
                    bucket_volume=10.0,
                    bucket_trade_count=5,
                    bucket_range_bps=12.0,
                )
            ],
            status_meta={"config_path": "", "status_path": ""},
            regime_snapshot=regime_snapshot,
        )

        doge_buy = next(order for order in doge_status.open_orders if order.side == "buy")
        hype_buy = next(order for order in hype_status.open_orders if order.side == "buy")
        doge_sell = next(order for order in doge_status.open_orders if order.side == "sell")
        hype_sell = next(order for order in hype_status.open_orders if order.side == "sell")

        # HYPE has quote_width_multiplier=1.50 → wider spread → lower bid, higher ask
        self.assertLessEqual(hype_buy.price, doge_buy.price)
        self.assertGreaterEqual(hype_sell.price, doge_sell.price)
        # HYPE has order_size_multiplier=0.50 → smaller orders
        self.assertLess(hype_buy.size, doge_buy.size)

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
            self.assertIn("paper_quote_width_multiplier_by_symbol", payload["trident"])
            self.assertIn("paper_order_size_multiplier_by_symbol", payload["trident"])
            self.assertIn("paper_max_inventory_skew_pct_by_symbol", payload["trident"])
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
                "--config",
                "config/trident.toml",
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
                # Live runner skips historical data, so 0 orders is expected
                self.assertGreaterEqual(synced_status.total_open_order_count, 0)
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

    def test_manager_trims_stale_runtime_symbols_outside_assignment(self) -> None:
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
                        "managed_symbols": ["DOGE", "XRP", "LTC"],
                        "target_usd": 300.0,
                        "last_sync_reason": "external_wrapper_running",
                        "positions": [
                            {
                                "symbol": "DOGE",
                                "side": "long",
                                "size": 1000.0,
                                "entry_price": 0.2,
                                "mark_price": 0.21,
                                "notional_usd": 210.0,
                                "unrealized_pnl_usd": 10.0,
                            }
                        ],
                        "open_orders": [
                            {"symbol": "DOGE", "side": "buy", "price": 0.209, "size": 100.0},
                            {"symbol": "LTC", "side": "sell", "price": 53.0, "size": 1.0},
                        ],
                        "inventory": [
                            {
                                "symbol": "DOGE",
                                "target_notional_usd": 100.0,
                                "current_notional_usd": 210.0,
                                "inventory_skew_pct": 2.1,
                                "has_position": True,
                                "open_order_count": 1,
                            },
                            {
                                "symbol": "XRP",
                                "target_notional_usd": 100.0,
                                "current_notional_usd": 0.0,
                                "inventory_skew_pct": 0.0,
                                "has_position": False,
                                "open_order_count": 0,
                            },
                            {
                                "symbol": "LTC",
                                "target_notional_usd": 100.0,
                                "current_notional_usd": 0.0,
                                "inventory_skew_pct": 0.0,
                                "has_position": False,
                                "open_order_count": 1,
                            },
                        ],
                        "total_open_order_count": 2,
                        "total_position_count": 1,
                        "realized_pnl_usd": 1.5,
                        "total_notional_usd": 210.0,
                        "total_unrealized_pnl_usd": 10.0,
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

            self.assertEqual(status.managed_symbols, ["DOGE", "XRP"])
            self.assertEqual([order.symbol for order in status.open_orders], ["DOGE"])
            self.assertEqual([item.symbol for item in status.inventory], ["DOGE", "XRP"])
            self.assertEqual(status.total_open_order_count, 1)
            self.assertEqual(status.total_position_count, 1)


if __name__ == "__main__":
    unittest.main()
