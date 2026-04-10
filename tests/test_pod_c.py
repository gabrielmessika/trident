import asyncio
import tempfile
import unittest
from pathlib import Path

from app.backtest.pod_c_runner import PodCBacktestRunner
from app.execution.directional_executor import DirectionalExecutor
from app.live.pod_c_live_runner import PodCLiveRunner
from app.risk.pod_c_gate import PodCRiskGate
from app.settings import load_config
from app.trident.pod_c import TradfiTrendContextService, TradfiTrendPlanner, TradfiTrendService
from app.trident.types import (
    PodAllocation,
    PodName,
    Regime,
    RiskDecision,
    SymbolAllocation,
    SymbolMarketSnapshot,
    TradePlan,
)


class _FakeCollector:
    def __init__(self) -> None:
        self.coins = ["SPX", "PAXG"]
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
                    "symbol": "SPX",
                    "price": 5100.0,
                    "ema_fast": 5110.0,
                    "ema_slow": 5088.0,
                    "vwap_distance_bps": -4.0,
                    "structure_score": 0.5,
                    "funding_rate": 0.0,
                    "spread_bps": 1.0,
                    "btc_aligned": True,
                    "book_imbalance": 0.14,
                    "trade_flow_bias": 0.10,
                    "bucket_volume": 2.0,
                    "bucket_trade_count": 8,
                    "bucket_range_bps": 24.0,
                    "market_cluster": "index",
                    "cluster_aligned": True,
                    "cluster_leader": "SPX",
                    "source": "test",
                },
                {
                    "symbol": "PAXG",
                    "price": 3200.0,
                    "ema_fast": 3197.0,
                    "ema_slow": 3205.0,
                    "vwap_distance_bps": 9.0,
                    "structure_score": -0.22,
                    "funding_rate": 0.0,
                    "spread_bps": 1.0,
                    "btc_aligned": True,
                    "book_imbalance": -0.12,
                    "trade_flow_bias": -0.10,
                    "bucket_volume": 1.0,
                    "bucket_trade_count": 6,
                    "bucket_range_bps": 18.0,
                    "market_cluster": "gold",
                    "cluster_aligned": True,
                    "cluster_leader": "PAXG",
                    "source": "test",
                },
            ],
        }


class PodCTests(unittest.TestCase):
    def test_tradfi_service_builds_activity_history(self) -> None:
        config = load_config("config/trident.toml")
        service = TradfiTrendService(config.pod_c)
        for _ in range(10):
            service.update_history("SPX", 1000.0, 4)
        self.assertAlmostEqual(service.activity_ratio("SPX", 1000.0), 1.0, places=2)
        self.assertAlmostEqual(service.activity_ratio("SPX", 500.0), 0.5, places=2)
        self.assertAlmostEqual(service.activity_ratio("SPX", 2000.0), 2.0, places=2)

    def test_tradfi_service_detects_continuation_signal(self) -> None:
        config = load_config("config/trident.toml")
        service = TradfiTrendService(config.pod_c)
        context_service = TradfiTrendContextService(config.pod_c, service)
        for _ in range(10):
            context_service.build_contexts(
                Regime.TREND_EXPANSION,
                [
                    SymbolMarketSnapshot(
                        symbol="SPX",
                        price=5000.0,
                        ema_fast=5008.0,
                        ema_slow=4998.0,
                        vwap_distance_bps=0.0,
                        structure_score=0.22,
                        funding_rate=0.0,
                        spread_bps=1.0,
                        btc_aligned=True,
                        book_imbalance=0.08,
                        trade_flow_bias=0.07,
                        bucket_volume=0.6,
                        bucket_trade_count=5,
                        bucket_range_bps=12.0,
                        market_cluster="index",
                        cluster_aligned=True,
                        cluster_leader="SPX",
                    ),
                ],
                owned_symbols={"SPX"},
            )
        contexts = context_service.build_contexts(
            Regime.TREND_EXPANSION,
            [
                SymbolMarketSnapshot(
                    symbol="SPX",
                    price=5050.0,
                    ema_fast=5066.0,
                    ema_slow=5030.0,
                    vwap_distance_bps=-3.0,
                    structure_score=0.48,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                    book_imbalance=0.16,
                    trade_flow_bias=0.13,
                    bucket_volume=1.5,
                    bucket_trade_count=8,
                    bucket_range_bps=22.0,
                    market_cluster="index",
                    cluster_aligned=True,
                    cluster_leader="SPX",
                ),
            ],
            owned_symbols={"SPX"},
        )
        self.assertGreaterEqual(len(contexts), 1)
        signals = service.evaluate_many(contexts)
        self.assertGreaterEqual(len(signals), 1)
        self.assertEqual(signals[0].symbol, "SPX")
        self.assertEqual(signals[0].side, "long")
        self.assertEqual(signals[0].setup, "tradfi_continuation_long")

    def test_tradfi_planner_builds_trade_plan(self) -> None:
        config = load_config("config/trident.toml")
        service = TradfiTrendService(config.pod_c)
        context_service = TradfiTrendContextService(config.pod_c, service)
        planner = TradfiTrendPlanner(config.pod_c)
        for _ in range(10):
            context_service.build_contexts(
                Regime.TREND_EXPANSION,
                [
                    SymbolMarketSnapshot(
                        symbol="SPX",
                        price=5000.0,
                        ema_fast=5008.0,
                        ema_slow=4998.0,
                        vwap_distance_bps=0.0,
                        structure_score=0.22,
                        funding_rate=0.0,
                        spread_bps=1.0,
                        btc_aligned=True,
                        book_imbalance=0.08,
                        trade_flow_bias=0.07,
                        bucket_volume=0.6,
                        bucket_trade_count=5,
                        bucket_range_bps=12.0,
                        market_cluster="index",
                        cluster_aligned=True,
                        cluster_leader="SPX",
                    ),
                ],
                owned_symbols={"SPX"},
            )
        contexts = context_service.build_contexts(
            Regime.TREND_EXPANSION,
            [
                SymbolMarketSnapshot(
                    symbol="SPX",
                    price=5050.0,
                    ema_fast=5066.0,
                    ema_slow=5030.0,
                    vwap_distance_bps=-3.0,
                    structure_score=0.48,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                    book_imbalance=0.16,
                    trade_flow_bias=0.13,
                    bucket_volume=1.5,
                    bucket_trade_count=8,
                    bucket_range_bps=22.0,
                    market_cluster="index",
                    cluster_aligned=True,
                    cluster_leader="SPX",
                ),
            ],
            owned_symbols={"SPX"},
        )
        signals = service.evaluate_many(contexts)
        self.assertGreater(len(signals), 0)
        allocation = PodAllocation(
            pod=PodName.POD_C,
            target_pct=0.3,
            target_usd=300.0,
            symbols=[SymbolAllocation(symbol="SPX", target_pct=0.3, target_usd=300.0)],
        )
        plan = planner.build_trade_plan(signals[0], allocation)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.symbol, "SPX")
        self.assertGreater(plan.confidence, 0.5)

    def test_pod_c_risk_gate_blocks_low_confidence(self) -> None:
        config = load_config("config/trident.toml")
        gate = PodCRiskGate(config)
        decision = gate.evaluate_many(
            [
                TradePlan(
                    symbol="SPX",
                    side="long",
                    setup="tradfi_continuation_long",
                    confidence=config.pod_c.min_confidence - 0.01,
                    target_notional_usd=100.0,
                    stop_bps=45.0,
                    time_stop_hours=config.pod_c.time_stop_hours,
                )
            ]
        )[0]
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "confidence_below_min")

    def test_pod_c_reentry_cooldown_blocks_immediate_flip(self) -> None:
        config = load_config("config/trident.toml")
        executor = DirectionalExecutor(config)
        short_plan = TradePlan(
            symbol="SPX",
            side="short",
            setup="tradfi_reclaim_short",
            confidence=0.8,
            target_notional_usd=100.0,
            stop_bps=45.0,
            time_stop_hours=config.pod_c.time_stop_hours,
            reentry_cooldown_minutes=config.pod_c.reentry_cooldown_minutes,
        )
        long_plan = TradePlan(
            symbol="SPX",
            side="long",
            setup="tradfi_continuation_long",
            confidence=0.82,
            target_notional_usd=100.0,
            stop_bps=45.0,
            time_stop_hours=config.pod_c.time_stop_hours,
            reentry_cooldown_minutes=config.pod_c.reentry_cooldown_minutes,
        )

        executor.process_record(
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="SPX",
                    price=5000.0,
                    ema_fast=4995.0,
                    ema_slow=5005.0,
                    vwap_distance_bps=1.0,
                    structure_score=-0.2,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                    market_cluster="index",
                    cluster_aligned=True,
                    cluster_leader="SPX",
                )
            ],
            risk_decisions=[RiskDecision(accepted=True, reason="accepted", trade_plan=short_plan)],
            signal_sides_by_symbol={"SPX": "short"},
            timestamp="2026-04-05T10:00:00Z",
        )

        batch = executor.process_record(
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="SPX",
                    price=5005.0,
                    ema_fast=5012.0,
                    ema_slow=4998.0,
                    vwap_distance_bps=2.0,
                    structure_score=0.15,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                    market_cluster="index",
                    cluster_aligned=True,
                    cluster_leader="SPX",
                )
            ],
            risk_decisions=[RiskDecision(accepted=True, reason="accepted", trade_plan=long_plan)],
            signal_sides_by_symbol={"SPX": "long"},
            timestamp="2026-04-05T10:10:00Z",
        )

        self.assertEqual(len(batch.closed_trades), 1)
        self.assertEqual(batch.closed_trades[0].close_reason, "opposite_signal")
        self.assertEqual(batch.skipped_open_symbols, ["SPX"])
        self.assertFalse(batch.has_open_position_after["SPX"])

    def test_pod_c_live_runner_processes_records(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_c.enabled = True
        runner = PodCLiveRunner(config, coins=["SPX", "PAXG"])
        runner.collector = _FakeCollector()  # type: ignore[assignment]
        status_path = Path("logs/pod_c_live_status.json")
        original_status = status_path.read_text(encoding="utf-8") if status_path.exists() else None
        with tempfile.TemporaryDirectory() as tmpdir:
            journal_path = Path(tmpdir) / "pod_c_live.jsonl"
            try:
                result = asyncio.run(runner.run(max_runtime_seconds=0.1, journal_path=journal_path))
                self.assertEqual(result["records_processed"], 1)
            finally:
                if original_status is None:
                    status_path.unlink(missing_ok=True)
                else:
                    status_path.write_text(original_status, encoding="utf-8")

    def test_pod_c_live_runner_defaults_to_pod_c_scope(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_c.enabled = True
        config.pod_c.symbols = ["SPX", "PAXG", "XYZ100", "WTIOIL", "GOLD", "SILVER"]
        config.hyperliquid.observation_universe = ["BTC", "ETH"]

        runner = PodCLiveRunner(config)

        self.assertEqual(
            runner.coins,
            ["SPX", "PAXG", "XYZ100", "WTIOIL", "GOLD", "SILVER"],
        )
        self.assertEqual(
            runner.config.hyperliquid.observation_universe,
            ["SPX", "PAXG", "XYZ100", "WTIOIL", "GOLD", "SILVER"],
        )


if __name__ == "__main__":
    unittest.main()
