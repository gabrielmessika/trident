import asyncio
import tempfile
import unittest
from pathlib import Path

from app.backtest.pod_c_runner import PodCBacktestRunner
from app.live.pod_c_live_runner import PodCLiveRunner
from app.settings import load_config
from app.trident.pod_c import EventContextService, EventRaiderPlanner, EventRaiderService
from app.trident.types import PodAllocation, PodName, Regime, SymbolAllocation, SymbolMarketSnapshot


class _FakeCollector:
    def __init__(self) -> None:
        self.coins = ["BTC", "SOL"]
        self.stats = type(
            "Stats",
            (),
            {
                "messages_processed": 4,
                "snapshots_written": 0,
                "reconnect_count": 0,
                "heartbeat_count": 0,
                "pong_count": 0,
                "timeout_count": 0,
                "api_error_count": 0,
                "rate_limit_error_count": 0,
                "last_error": None,
            },
        )()
        self.writer = type("Writer", (), {"append_many": lambda self, records: list(records)})()
        self.builder = type("Builder", (), {"finalize": lambda self: []})()

    async def iter_records(self, **_: object):
        yield {
            "timestamp": "2026-04-05T10:00:00Z",
            "regime_snapshot": {
                "ready": True,
                "adx": 35.0,
                "atr_ratio": 1.4,
                "range_width_bps": 220.0,
                "structure_score": 0.65,
                "btc_impulse": True,
            },
            "symbols": [
                {
                    "symbol": "BTC",
                    "price": 100.6,
                    "ema_fast": 100.4,
                    "ema_slow": 100.0,
                    "vwap_distance_bps": 15.0,
                    "structure_score": 0.7,
                    "funding_rate": 0.0,
                    "spread_bps": 1.0,
                    "btc_aligned": True,
                    "book_imbalance": 0.3,
                    "trade_flow_bias": 0.2,
                    "bucket_volume": 20.0,
                    "bucket_trade_count": 5,
                    "bucket_range_bps": 30.0,
                    "source": "test",
                },
                {
                    "symbol": "SOL",
                    "price": 50.1,
                    "ema_fast": 50.05,
                    "ema_slow": 50.0,
                    "vwap_distance_bps": 2.0,
                    "structure_score": 0.15,
                    "funding_rate": 0.0,
                    "spread_bps": 1.0,
                    "btc_aligned": True,
                    "book_imbalance": 0.2,
                    "trade_flow_bias": 0.2,
                    "bucket_volume": 10.0,
                    "bucket_trade_count": 3,
                    "bucket_range_bps": 12.0,
                    "source": "test",
                },
            ],
        }


class PodCTests(unittest.TestCase):
    def test_service_emits_lead_lag_signal(self) -> None:
        config = load_config("config/trident.toml")
        context_service = EventContextService(config.pod_c)
        service = EventRaiderService(config.pod_c)
        planner = EventRaiderPlanner(config.pod_c)
        contexts = context_service.build_contexts(
            Regime.TREND_EXPANSION,
            [
                SymbolMarketSnapshot(
                    symbol="BTC",
                    price=100.6,
                    ema_fast=100.4,
                    ema_slow=100.0,
                    vwap_distance_bps=15.0,
                    structure_score=0.7,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                    book_imbalance=0.3,
                    trade_flow_bias=0.2,
                ),
                SymbolMarketSnapshot(
                    symbol="SOL",
                    price=50.1,
                    ema_fast=50.05,
                    ema_slow=50.0,
                    vwap_distance_bps=2.0,
                    structure_score=0.15,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                    book_imbalance=0.2,
                    trade_flow_bias=0.2,
                ),
            ],
        )

        signal = service.evaluate_many(contexts)[0]
        allocation = PodAllocation(
            pod=PodName.POD_C,
            target_pct=0.3,
            target_usd=300.0,
            symbols=[SymbolAllocation(symbol="SOL", target_pct=0.3, target_usd=300.0)],
        )
        plan = planner.build_trade_plan(signal, allocation)

        self.assertEqual(signal.symbol, "SOL")
        self.assertEqual(signal.side, "long")
        self.assertIsNotNone(plan)
        self.assertEqual(plan.symbol, "SOL")
        self.assertGreater(plan.confidence, 0.5)

    def test_pod_c_backtest_runner_replays_snapshots(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_c.enabled = True
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "snapshots.jsonl"
            input_path.write_text(
                "\n".join(
                    [
                        '{"timestamp":"2026-04-05T10:00:00Z","regime_snapshot":{"ready":true,"adx":35.0,"atr_ratio":1.4,"range_width_bps":220.0,"structure_score":0.65,"btc_impulse":true},"symbols":[{"symbol":"BTC","price":100.6,"ema_fast":100.4,"ema_slow":100.0,"vwap_distance_bps":15.0,"structure_score":0.7,"funding_rate":0.0,"spread_bps":1.0,"btc_aligned":true,"book_imbalance":0.3,"trade_flow_bias":0.2,"bucket_volume":20.0,"bucket_trade_count":5,"bucket_range_bps":30.0,"source":"test"},{"symbol":"SOL","price":50.1,"ema_fast":50.05,"ema_slow":50.0,"vwap_distance_bps":2.0,"structure_score":0.15,"funding_rate":0.0,"spread_bps":1.0,"btc_aligned":true,"book_imbalance":0.2,"trade_flow_bias":0.2,"bucket_volume":10.0,"bucket_trade_count":3,"bucket_range_bps":12.0,"source":"test"}]}',
                        '{"timestamp":"2026-04-05T11:00:00Z","regime_snapshot":{"ready":true,"adx":28.0,"atr_ratio":1.1,"range_width_bps":120.0,"structure_score":0.35,"btc_impulse":false},"symbols":[{"symbol":"BTC","price":100.7,"ema_fast":100.5,"ema_slow":100.2,"vwap_distance_bps":10.0,"structure_score":0.5,"funding_rate":0.0,"spread_bps":1.0,"btc_aligned":true,"book_imbalance":0.1,"trade_flow_bias":0.1,"bucket_volume":12.0,"bucket_trade_count":4,"bucket_range_bps":18.0,"source":"test"},{"symbol":"SOL","price":50.5,"ema_fast":50.3,"ema_slow":50.1,"vwap_distance_bps":6.0,"structure_score":0.3,"funding_rate":0.0,"spread_bps":1.0,"btc_aligned":true,"book_imbalance":0.1,"trade_flow_bias":0.1,"bucket_volume":11.0,"bucket_trade_count":4,"bucket_range_bps":18.0,"source":"test"}]}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            result = PodCBacktestRunner(config).run_jsonl(input_path)
            self.assertGreaterEqual(result.backtest["signal_count"], 1)
            self.assertGreaterEqual(result.backtest["accepted_count"], 1)

    def test_pod_c_live_runner_processes_records(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_c.enabled = True
        runner = PodCLiveRunner(config, coins=["BTC", "SOL"])
        runner.collector = _FakeCollector()  # type: ignore[assignment]
        with tempfile.TemporaryDirectory() as tmpdir:
            journal_path = Path(tmpdir) / "pod_c_live.jsonl"
            result = asyncio.run(runner.run(max_runtime_seconds=0.1, journal_path=journal_path))
            self.assertEqual(result["records_processed"], 1)
            self.assertGreaterEqual(result["signal_count"], 1)
            self.assertTrue(journal_path.exists())


if __name__ == "__main__":
    unittest.main()
