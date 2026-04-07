import asyncio
import tempfile
import unittest
from pathlib import Path

from app.backtest.pod_c_runner import PodCBacktestRunner
from app.execution.directional_executor import DirectionalExecutor
from app.live.pod_c_live_runner import PodCLiveRunner
from app.risk.pod_c_gate import PodCRiskGate
from app.settings import load_config
from app.trident.pod_c import EventContextService, EventRaiderPlanner, EventRaiderService
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
        context_service = EventContextService(config)
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

    def test_service_emits_index_cluster_signal_for_dynamic_follower(self) -> None:
        config = load_config("config/trident.toml")
        config.hyperliquid.market_cluster_overrides["NDX"] = "index"
        context_service = EventContextService(config)
        service = EventRaiderService(config.pod_c)
        planner = EventRaiderPlanner(config.pod_c)
        contexts = context_service.build_contexts(
            Regime.TREND_EXPANSION,
            [
                SymbolMarketSnapshot(
                    symbol="SPX",
                    price=5005.0,
                    ema_fast=4998.0,
                    ema_slow=4980.0,
                    vwap_distance_bps=9.0,
                    structure_score=0.4,
                    funding_rate=0.0,
                    spread_bps=0.8,
                    btc_aligned=False,
                    book_imbalance=0.18,
                    trade_flow_bias=0.16,
                ),
                SymbolMarketSnapshot(
                    symbol="NDX",
                    price=210.0,
                    ema_fast=209.9,
                    ema_slow=209.7,
                    vwap_distance_bps=1.5,
                    structure_score=0.12,
                    funding_rate=0.0,
                    spread_bps=0.9,
                    btc_aligned=False,
                    book_imbalance=0.12,
                    trade_flow_bias=0.1,
                ),
            ],
        )

        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0].symbol, "NDX")
        self.assertEqual(contexts[0].market_cluster, "index")
        self.assertEqual(contexts[0].leader_symbol, "SPX")
        self.assertTrue(contexts[0].cluster_aligned)

        signal = service.evaluate_many(contexts)[0]
        allocation = PodAllocation(
            pod=PodName.POD_C,
            target_pct=0.2,
            target_usd=200.0,
            symbols=[SymbolAllocation(symbol="NDX", target_pct=0.2, target_usd=200.0)],
        )
        plan = planner.build_trade_plan(signal, allocation)

        self.assertEqual(signal.market_cluster, "index")
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertLess(plan.time_stop_hours, config.pod_c.time_stop_hours)

    def test_pod_c_risk_gate_uses_stricter_min_confidence(self) -> None:
        config = load_config("config/trident.toml")
        gate = PodCRiskGate(config)
        decision = gate.evaluate_many(
            [
                TradePlan(
                    symbol="SOL",
                    side="long",
                    setup="lead_lag_long",
                    confidence=config.pod_c.min_confidence - 0.01,
                    target_notional_usd=100.0,
                    stop_bps=45.0,
                    time_stop_hours=config.pod_c.time_stop_hours,
                )
            ]
        )[0]

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "confidence_below_min")

    def test_pod_c_risk_gate_blocks_configured_symbols(self) -> None:
        config = load_config("config/trident.toml")
        gate = PodCRiskGate(config)
        decision = gate.evaluate_many(
            [
                TradePlan(
                    symbol="HYPE",
                    side="long",
                    setup="lead_lag_long",
                    confidence=config.pod_c.min_confidence + 0.05,
                    target_notional_usd=100.0,
                    stop_bps=45.0,
                    time_stop_hours=config.pod_c.time_stop_hours,
                )
            ]
        )[0]

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "symbol_blocked")

    def test_pod_c_reentry_cooldown_blocks_immediate_flip(self) -> None:
        config = load_config("config/trident.toml")
        executor = DirectionalExecutor(config)
        short_plan = TradePlan(
            symbol="SOL",
            side="short",
            setup="lead_lag_short",
            confidence=0.8,
            target_notional_usd=100.0,
            stop_bps=45.0,
            time_stop_hours=config.pod_c.time_stop_hours,
            reentry_cooldown_minutes=config.pod_c.reentry_cooldown_minutes,
        )
        long_plan = TradePlan(
            symbol="SOL",
            side="long",
            setup="lead_lag_long",
            confidence=0.82,
            target_notional_usd=100.0,
            stop_bps=45.0,
            time_stop_hours=config.pod_c.time_stop_hours,
            reentry_cooldown_minutes=config.pod_c.reentry_cooldown_minutes,
        )

        executor.process_record(
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="SOL",
                    price=50.0,
                    ema_fast=49.9,
                    ema_slow=49.7,
                    vwap_distance_bps=1.0,
                    structure_score=-0.2,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                )
            ],
            risk_decisions=[RiskDecision(accepted=True, reason="accepted", trade_plan=short_plan)],
            signal_sides_by_symbol={"SOL": "short"},
            timestamp="2026-04-05T10:00:00Z",
        )

        batch = executor.process_record(
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="SOL",
                    price=50.05,
                    ema_fast=50.0,
                    ema_slow=49.8,
                    vwap_distance_bps=2.0,
                    structure_score=0.15,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                )
            ],
            risk_decisions=[RiskDecision(accepted=True, reason="accepted", trade_plan=long_plan)],
            signal_sides_by_symbol={"SOL": "long"},
            timestamp="2026-04-05T10:10:00Z",
        )

        self.assertEqual(len(batch.closed_trades), 1)
        self.assertEqual(batch.closed_trades[0].close_reason, "opposite_signal")
        self.assertEqual(batch.skipped_open_symbols, ["SOL"])
        self.assertFalse(batch.has_open_position_after["SOL"])

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
        status_path = Path("logs/pod_c_live_status.json")
        original_status = status_path.read_text(encoding="utf-8") if status_path.exists() else None
        with tempfile.TemporaryDirectory() as tmpdir:
            journal_path = Path(tmpdir) / "pod_c_live.jsonl"
            try:
                result = asyncio.run(runner.run(max_runtime_seconds=0.1, journal_path=journal_path))
                self.assertEqual(result["records_processed"], 1)
                self.assertGreaterEqual(result["signal_count"], 1)
                self.assertTrue(journal_path.exists())
            finally:
                if original_status is None:
                    status_path.unlink(missing_ok=True)
                else:
                    status_path.write_text(original_status, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
