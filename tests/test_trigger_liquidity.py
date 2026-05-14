import json
import tempfile
import unittest
from pathlib import Path

from app.live.trigger_liquidity_enricher import (
    TriggerLiquidityEnricherRunner,
    TriggerLiquiditySnapshotEnricher,
)
from app.live.trigger_liquidity_collector import (
    TriggerLiquidityNodeDataCollector,
    TriggerLiquidityQuickNodeCollector,
    normalize_quicknode_hypercore_url,
)
from app.live.trigger_liquidity_sql_backfill import TriggerLiquiditySqlBackfiller
from app.settings import TriggerLiquidityConfig, load_config
from app.trident.trigger_liquidity import TriggerLiquidityBook, TriggerLiquidityOverlay
from app.trident.types import SymbolMarketSnapshot, TradePlan


def _order_status(
    *,
    status: str = "open",
    oid: int = 1,
    coin: str = "SOL",
    side: str = "B",
    trigger_px: float = 101.0,
    sz: float = 20.0,
    order_type: str = "Stop Market",
    time: str = "2026-05-13T10:00:00Z",
) -> dict[str, object]:
    return {
        "time": time,
        "user": "0xabc",
        "status": status,
        "order": {
            "coin": coin,
            "side": side,
            "limitPx": str(trigger_px),
            "sz": str(sz),
            "oid": oid,
            "timestamp": 1778666400000,
            "triggerCondition": "Price above",
            "isTrigger": True,
            "triggerPx": str(trigger_px),
            "isPositionTpsl": True,
            "reduceOnly": True,
            "orderType": order_type,
            "origSz": str(sz),
        },
    }


def _snapshot(**overrides: object) -> SymbolMarketSnapshot:
    data = {
        "symbol": "SOL",
        "price": 100.0,
        "ema_fast": 100.0,
        "ema_slow": 99.0,
        "vwap_distance_bps": 0.0,
        "structure_score": 0.5,
        "funding_rate": 0.0,
        "spread_bps": 1.0,
        "btc_aligned": True,
        "trigger_liquidity_available": True,
        "cascade_risk_up": 0.1,
        "cascade_risk_down": 0.9,
        "trigger_data_age_seconds": 1.0,
    }
    data.update(overrides)
    return SymbolMarketSnapshot(**data)


class TriggerLiquidityStateTests(unittest.TestCase):
    def test_book_builds_stop_and_tp_cluster_features(self) -> None:
        book = TriggerLiquidityBook()
        book.apply_order_status(
            _order_status(oid=1, side="B", trigger_px=101.0, sz=20.0, order_type="Stop Market")
        )
        book.apply_order_status(
            _order_status(oid=2, side="A", trigger_px=99.0, sz=20.0, order_type="Stop Market")
        )
        book.apply_order_status(
            _order_status(oid=3, side="A", trigger_px=101.5, sz=10.0, order_type="Take Profit")
        )

        features = book.features_for_symbol(
            symbol="SOL",
            reference_price=100.0,
            bucket_bps=10.0,
            lookahead_bps=200.0,
            min_cluster_notional_usd=1_000.0,
            now_ms=1778666405000,
        )

        self.assertTrue(features.trigger_liquidity_available)
        self.assertGreater(features.stop_pressure_above, 0.0)
        self.assertGreater(features.stop_pressure_below, 0.0)
        self.assertGreater(features.tp_pressure_above, 0.0)
        self.assertEqual(features.cascade_risk_up, 1.0)
        self.assertEqual(features.cascade_risk_down, 1.0)
        self.assertEqual(features.trigger_data_age_seconds, 5.0)

        book.apply_order_status(_order_status(status="canceled", oid=1))
        features_after_cancel = book.features_for_symbol(
            symbol="SOL",
            reference_price=100.0,
            bucket_bps=10.0,
            lookahead_bps=200.0,
            min_cluster_notional_usd=1_000.0,
            now_ms=1778666405000,
        )
        self.assertEqual(features_after_cancel.cascade_risk_up, 0.0)
        self.assertGreater(features_after_cancel.cascade_risk_down, 0.0)


class TriggerLiquidityEnricherTests(unittest.TestCase):
    def test_enricher_adds_trigger_liquidity_fields_to_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            snapshots = tmp / "snapshots.jsonl"
            triggers = tmp / "triggers.jsonl"
            output = tmp / "enriched.jsonl"
            snapshots.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-05-13T10:00:05Z",
                        "regime_snapshot": {
                            "ready": True,
                            "adx": 20.0,
                            "atr_ratio": 1.0,
                            "range_width_bps": 30.0,
                            "structure_score": 0.2,
                            "btc_impulse": False,
                        },
                        "symbols": [
                            {
                                "symbol": "SOL",
                                "price": 100.0,
                                "ema_fast": 100.0,
                                "ema_slow": 100.0,
                                "vwap_distance_bps": 0.0,
                                "structure_score": 0.0,
                                "funding_rate": 0.0,
                                "spread_bps": 1.0,
                                "btc_aligned": True,
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            triggers.write_text(json.dumps(_order_status()) + "\n", encoding="utf-8")

            result = TriggerLiquiditySnapshotEnricher(
                TriggerLiquidityConfig(
                    bucket_bps=10.0,
                    lookahead_bps=200.0,
                    min_cluster_notional_usd=1_000.0,
                )
            ).enrich(
                input_path=snapshots,
                trigger_source_path=triggers,
                output_path=output,
            )

            enriched = json.loads(output.read_text(encoding="utf-8").strip())
            symbol = enriched["symbols"][0]
            self.assertEqual(result["symbols_enriched"], 1)
            self.assertTrue(symbol["trigger_liquidity_available"])
            self.assertGreater(symbol["stop_pressure_above"], 0.0)
            self.assertEqual(symbol["cascade_risk_up"], 1.0)

    def test_runner_enriches_latest_snapshot_and_writes_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            snapshot_dir = tmp / "snapshots"
            trigger_dir = tmp / "trigger_source"
            output_dir = tmp / "enriched"
            status_path = tmp / "runtime" / "trigger_status.json"
            snapshot_dir.mkdir()
            trigger_dir.mkdir()
            (snapshot_dir / "2026-05-13.jsonl").write_text(
                json.dumps(
                    {
                        "timestamp": "2026-05-13T10:00:05Z",
                        "symbols": [{"symbol": "SOL", "price": 100.0}],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (trigger_dir / "node_order_statuses.jsonl").write_text(
                json.dumps(_order_status()) + "\n",
                encoding="utf-8",
            )
            config = load_config("config/trident.toml")
            config.hyperliquid.snapshot_output_dir = str(snapshot_dir)
            config.trigger_liquidity.source_path = str(trigger_dir)
            config.trigger_liquidity.snapshot_output_dir = str(output_dir)
            config.trigger_liquidity.bucket_bps = 10.0
            config.trigger_liquidity.lookahead_bps = 200.0
            config.trigger_liquidity.min_cluster_notional_usd = 1_000.0

            status = TriggerLiquidityEnricherRunner(
                config,
                poll_seconds=1.0,
                status_path=status_path,
            ).run_once()

            enriched = json.loads(
                (output_dir / "2026-05-13.jsonl").read_text(encoding="utf-8").strip()
            )
            status_payload = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertTrue(status["healthy"])
            self.assertEqual(status_payload["events_loaded"], 1)
            self.assertEqual(status_payload["records_processed"], 1)
            self.assertEqual(status_payload["symbols_enriched"], 1)
            self.assertTrue(enriched["symbols"][0]["trigger_liquidity_available"])


class TriggerLiquidityCollectorTests(unittest.TestCase):
    def test_collector_filters_node_order_statuses_and_tracks_offsets(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source_dir = tmp / "node_order_statuses" / "hourly" / "20260513"
            source_dir.mkdir(parents=True)
            source_file = source_dir / "10"
            output_dir = tmp / "trigger_liquidity"
            state_path = tmp / "runtime" / "collector_state.json"
            status_path = tmp / "runtime" / "collector_status.json"
            source_file.write_text(
                "\n".join(
                    [
                        json.dumps(_order_status()),
                        json.dumps(
                            _order_status(
                                oid=2,
                                order_type="Limit",
                                trigger_px=0.0,
                            )
                            | {"order": {"isTrigger": False}}
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            collector = TriggerLiquidityNodeDataCollector(
                source_paths=[source_dir.parent],
                output_dir=output_dir,
                state_path=state_path,
                status_path=status_path,
                lookback_hours=24.0,
            )

            first = collector.collect_once()
            second = collector.collect_once()

            output = output_dir / "2026-05-13.jsonl"
            lines = output.read_text(encoding="utf-8").strip().splitlines()
            status = json.loads(status_path.read_text(encoding="utf-8"))
            self.assertEqual(first.trigger_records_written, 1)
            self.assertEqual(second.trigger_records_written, 0)
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["source"], "hyperliquid_node_order_statuses")
            self.assertEqual(status["process_state"], "running")
            self.assertEqual(status["source_file_count"], 1)

    def test_quicknode_collector_fetches_order_blocks_and_tracks_last_block(self) -> None:
        testcase = self

        class FakeQuickNodeClient:
            def __init__(self) -> None:
                self.url = normalize_quicknode_hypercore_url(
                    "https://example.hype-mainnet.quiknode.pro/token"
                )
                self.batch_requests: list[tuple[int, int]] = []

            def latest_block_number(self, stream: str) -> int:
                testcase.assertEqual(stream, "orders")
                return 105

            def batch_blocks(
                self,
                *,
                stream: str,
                from_block: int,
                to_block: int,
            ) -> list[dict[str, object]]:
                testcase.assertEqual(stream, "orders")
                self.batch_requests.append((from_block, to_block))
                if not (from_block <= 104 <= to_block):
                    return []
                return [
                    {
                        "block_number": 104,
                        "events": [
                            _order_status(time="2026-05-14T17:07:59.995818204"),
                            _order_status(trigger_px=0.0)
                            | {"order": {"isTrigger": False}},
                        ],
                    }
                ]

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            output_dir = tmp / "trigger_liquidity"
            state_path = tmp / "runtime" / "collector_state.json"
            status_path = tmp / "runtime" / "collector_status.json"
            fake_client = FakeQuickNodeClient()
            collector = TriggerLiquidityQuickNodeCollector(
                quicknode_url="https://ignored",
                output_dir=output_dir,
                state_path=state_path,
                status_path=status_path,
                batch_size=2,
                initial_lookback_blocks=3,
                max_blocks_per_poll=3,
                client=fake_client,  # type: ignore[arg-type]
            )

            first = collector.collect_once()
            second = collector.collect_once()

            output = output_dir / "2026-05-14.jsonl"
            lines = output.read_text(encoding="utf-8").strip().splitlines()
            status = json.loads(status_path.read_text(encoding="utf-8"))
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(first.trigger_records_written, 1)
            self.assertEqual(second.trigger_records_written, 0)
            self.assertEqual(fake_client.batch_requests, [(103, 104), (105, 105)])
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["source"], "quicknode_orders")
            self.assertEqual(json.loads(lines[0])["block_number"], 104)
            self.assertEqual(status["provider"], "quicknode_orders")
            self.assertEqual(status["process_state"], "running")
            self.assertEqual(state["quicknode"]["last_block_number"], 105)

    def test_sql_backfill_writes_trigger_orders_and_resumes_with_keyset(self) -> None:
        testcase = self

        class FakeSqlClient:
            def __init__(self) -> None:
                self.queries: list[str] = []
                self.pages = [
                    [
                        {
                            "block_number": 10,
                            "status_time_text": "2026-04-01 00:00:01.000000",
                            "block_time_text": "2026-04-01 00:00:01.000000",
                            "user": "0xabc",
                            "hash": "0xhash",
                            "status": "open",
                            "coin": "BTC",
                            "side": "A",
                            "limit_price": "70000",
                            "size": "0.5",
                            "oid": 42,
                            "order_timestamp_text": "2026-04-01 00:00:00.500000",
                            "trigger_condition": "Price below 69000",
                            "trigger_price_text": "69000",
                            "is_position_tpsl": 1,
                            "reduce_only": 1,
                            "order_type": "Stop Market",
                            "orig_size": "0.5",
                            "tif": None,
                            "cloid": None,
                            "unique_id": "u1",
                        }
                    ],
                    [],
                ]

            def query(self, sql: str) -> tuple[dict[str, object], float]:
                self.queries.append(sql)
                page = self.pages.pop(0)
                return {"data": page, "rows_before_limit_at_least": len(page)}, 1.5

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            output_dir = tmp / "trigger_liquidity"
            state_path = tmp / "runtime" / "sql_state.json"
            status_path = tmp / "runtime" / "sql_status.json"
            fake_client = FakeSqlClient()
            backfiller = TriggerLiquiditySqlBackfiller(
                api_key="ignored",
                output_dir=output_dir,
                state_path=state_path,
                status_path=status_path,
                start_time="2026-04-01",
                end_time="2026-04-02",
                page_size=1,
                sleep_seconds=0.0,
                client=fake_client,  # type: ignore[arg-type]
            )

            stats = backfiller.run()

            output = output_dir / "2026-04-01.jsonl"
            lines = output.read_text(encoding="utf-8").strip().splitlines()
            payload = json.loads(lines[0])
            status = json.loads(status_path.read_text(encoding="utf-8"))
            state = json.loads(state_path.read_text(encoding="utf-8"))
            testcase.assertEqual(stats.trigger_records_written, 1)
            testcase.assertEqual(len(fake_client.queries), 2)
            testcase.assertIn("unique_id > 'u1'", fake_client.queries[1])
            testcase.assertEqual(payload["source"], "quicknode_sql_hyperliquid_orders")
            testcase.assertEqual(payload["time"], "2026-04-01T00:00:01Z")
            testcase.assertEqual(payload["order"]["triggerPx"], "69000")
            testcase.assertEqual(status["process_state"], "completed")
            testcase.assertTrue(state["sql_backfill"]["completed"])


class TriggerLiquidityOverlayTests(unittest.TestCase):
    def test_shadow_overlay_marks_watch_without_vetoing(self) -> None:
        overlay = TriggerLiquidityOverlay(
            TriggerLiquidityConfig(
                enabled=True,
                shadow_only=True,
                veto_enabled=True,
                veto_min_cascade_risk=0.75,
            )
        )

        decision = overlay.evaluate(_snapshot(), side="long")

        self.assertEqual(decision.action, "watch")
        self.assertEqual(decision.proposed_action, "veto_entry")
        self.assertFalse(decision.veto)

    def test_non_shadow_overlay_can_veto_plan(self) -> None:
        overlay = TriggerLiquidityOverlay(
            TriggerLiquidityConfig(
                enabled=True,
                shadow_only=False,
                veto_enabled=True,
                veto_min_cascade_risk=0.75,
            )
        )
        plan = TradePlan(
            symbol="SOL",
            side="long",
            setup="trend_pullback_long",
            confidence=0.7,
            target_notional_usd=100.0,
            stop_bps=50.0,
            time_stop_hours=12,
        )

        self.assertIsNone(overlay.apply_to_plan(plan, _snapshot()))


if __name__ == "__main__":
    unittest.main()
