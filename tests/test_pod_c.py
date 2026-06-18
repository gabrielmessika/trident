import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from app.backtest.pod_c_runner import PodCBacktestRunner
from app.execution.directional_executor import DirectionalExecutor
from app.live.pod_c_live_runner import PodCLiveRunner
from app.research.pod_c_day_by_day_patterns import _recommendations
from app.risk.pod_c_gate import PodCRiskGate
from app.settings import PodAPatternVetoConfig, PodCClusterModeConfig, load_config
from app.trident.pod_c import (
    TradfiTrendContext,
    TradfiTrendContextService,
    TradfiTrendPlanner,
    TradfiTrendService,
    TradfiTrendSignal,
)
from app.trident.types import (
    PodAllocation,
    PodName,
    Regime,
    RegimeSnapshot,
    RiskDecision,
    SymbolAllocation,
    SymbolMarketSnapshot,
    TradePlan,
)


class _FakeCollector:
    def __init__(self) -> None:
        self.coins = ["SPY", "PAXG"]
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
                    "symbol": "SPY",
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
                    "cluster_leader": "SPY",
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


class _FakeInfoClient:
    def __init__(self, mids: dict[str, float]) -> None:
        self._mids = mids

    def fetch_all_mids(self, *, symbols: list[str] | None = None) -> dict[str, float]:
        if not symbols:
            return dict(self._mids)
        requested = {str(symbol).strip().upper() for symbol in symbols}
        return {
            symbol: price
            for symbol, price in self._mids.items()
            if str(symbol).strip().upper() in requested
        }


class _FakeExternalReferenceEnricher:
    def __init__(self) -> None:
        self.calls = 0
        self.symbols_enriched = 0

    def enrich_record(self, record: dict[str, object]) -> dict[str, object]:
        self.calls += 1
        payload = dict(record)
        symbols: list[object] = []
        for item in record.get("symbols", []):
            if not isinstance(item, dict):
                symbols.append(item)
                continue
            enriched = dict(item)
            enriched.update(
                {
                    "external_reference_price": 5099.5,
                    "external_reference_source_count": 1,
                    "external_reference_sources": "fake",
                    "external_reference_symbol": "fake:SPY",
                    "external_reference_time": str(record.get("timestamp")),
                    "external_reference_age_seconds": 0.0,
                    "external_reference_max_deviation_bps": 0.0,
                    "external_premium_bps": 0.9805,
                    "external_momentum_60s_bps": 1.0,
                    "external_momentum_300s_bps": 2.0,
                    "external_alignment_score": 0.1,
                }
            )
            self.symbols_enriched += 1
            symbols.append(enriched)
        payload["symbols"] = symbols
        return payload

    def stats_payload(self) -> dict[str, object]:
        return {
            "enabled": True,
            "records_seen": self.calls,
            "symbols_enriched": self.symbols_enriched,
        }


class PodCTests(unittest.TestCase):
    def test_tradfi_service_builds_activity_history(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_c = replace(config.pod_c, cluster_aware_v2_enabled=False)
        service = TradfiTrendService(config.pod_c)
        for _ in range(10):
            service.update_history("SPX", 1000.0, 4)
        self.assertAlmostEqual(service.activity_ratio("SPX", 1000.0), 1.0, places=2)
        self.assertAlmostEqual(service.activity_ratio("SPX", 500.0), 0.5, places=2)
        self.assertAlmostEqual(service.activity_ratio("SPX", 2000.0), 2.0, places=2)

    def test_tradfi_service_detects_continuation_signal(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_c = replace(config.pod_c, cluster_aware_v2_enabled=False)
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
        self.assertEqual(signals[0].setup_details["market_cluster"], "index")
        self.assertEqual(signals[0].setup_details["cluster_strategy"], "index_breakout_long")
        self.assertEqual(signals[0].setup_details["flow_alignment"], "supportive")
        self.assertEqual(signals[0].setup_details["trend_bucket"], "strong")

    def test_tradfi_service_cluster_v2_accepts_oil_pullback_long(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_c = replace(config.pod_c, cluster_aware_v2_enabled=True)
        service = TradfiTrendService(config.pod_c)
        context_service = TradfiTrendContextService(config.pod_c, service)
        for _ in range(10):
            context_service.build_contexts(
                Regime.TREND_EXPANSION,
                [
                    SymbolMarketSnapshot(
                        symbol="XYZ:CL",
                        price=82.0,
                        ema_fast=82.1,
                        ema_slow=81.8,
                        vwap_distance_bps=-1.4,
                        structure_score=0.22,
                        funding_rate=0.0,
                        spread_bps=1.0,
                        btc_aligned=True,
                        book_imbalance=0.34,
                        trade_flow_bias=0.42,
                        bucket_volume=1.5,
                        bucket_trade_count=8,
                        bucket_range_bps=18.0,
                        market_cluster="oil",
                        cluster_aligned=True,
                        cluster_leader="XYZ:CL",
                    ),
                ],
                owned_symbols={"XYZ:CL"},
            )
        contexts = context_service.build_contexts(
            Regime.TREND_EXPANSION,
            [
                SymbolMarketSnapshot(
                    symbol="XYZ:CL",
                    price=82.5,
                    ema_fast=83.2,
                    ema_slow=82.0,
                    vwap_distance_bps=-2.1,
                    structure_score=0.30,
                    funding_rate=0.0,
                    spread_bps=1.2,
                    btc_aligned=True,
                    book_imbalance=0.36,
                    trade_flow_bias=0.44,
                    bucket_volume=3.2,
                    bucket_trade_count=10,
                    bucket_range_bps=24.0,
                    market_cluster="oil",
                    cluster_aligned=True,
                    cluster_leader="XYZ:CL",
                ),
            ],
            owned_symbols={"XYZ:CL"},
        )

        signals = service.evaluate_many(contexts)

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].symbol, "XYZ:CL")
        self.assertEqual(signals[0].side, "long")

    def test_tradfi_service_cluster_v2_rejects_weak_oil_pullback_long(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_c = replace(config.pod_c, cluster_aware_v2_enabled=True)
        service = TradfiTrendService(config.pod_c)
        contexts = [
            TradfiTrendContext(
                symbol="XYZ:CL",
                regime=Regime.TREND_EXPANSION.value,
                price=82.5,
                ema_fast=83.2,
                ema_slow=82.0,
                vwap_distance_bps=-3.1,
                spread_bps=1.2,
                funding_rate=0.0,
                structure_score=0.30,
                book_imbalance=0.52,
                trade_flow_bias=0.36,
                bucket_range_bps=24.0,
                bucket_trade_count=10,
                bucket_volume=3.0,
                bucket_notional_usd=247.5,
                activity_ratio=1.9,
                trade_count_ratio=1.0,
                trend_bps=12.0,
                btc_aligned=True,
                market_cluster="oil",
                cluster_aligned=True,
                cluster_leader="XYZ:CL",
                global_regime=Regime.TREND_EXPANSION.value,
                cluster_regime=Regime.TREND_EXPANSION.value,
            ),
        ]

        signals = service.evaluate_many(contexts)

        self.assertEqual(signals, [])

    def test_tradfi_service_cluster_v2_rejects_oil_short_breakdown(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_c = replace(config.pod_c, cluster_aware_v2_enabled=True)
        service = TradfiTrendService(config.pod_c)
        context_service = TradfiTrendContextService(config.pod_c, service)
        for _ in range(10):
            context_service.build_contexts(
                Regime.TREND_EXPANSION,
                [
                    SymbolMarketSnapshot(
                        symbol="XYZ:CL",
                        price=82.0,
                        ema_fast=81.9,
                        ema_slow=82.2,
                        vwap_distance_bps=-2.0,
                        structure_score=-0.20,
                        funding_rate=0.0,
                        spread_bps=1.0,
                        btc_aligned=True,
                        book_imbalance=-0.10,
                        trade_flow_bias=-0.08,
                        bucket_volume=2.0,
                        bucket_trade_count=8,
                        bucket_range_bps=20.0,
                        market_cluster="oil",
                        cluster_aligned=True,
                        cluster_leader="XYZ:CL",
                    ),
                ],
                owned_symbols={"XYZ:CL"},
            )
        contexts = context_service.build_contexts(
            Regime.TREND_EXPANSION,
            [
                SymbolMarketSnapshot(
                    symbol="XYZ:CL",
                    price=81.2,
                    ema_fast=80.8,
                    ema_slow=81.9,
                    vwap_distance_bps=-3.0,
                    structure_score=-0.34,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                    book_imbalance=-0.18,
                    trade_flow_bias=-0.14,
                    bucket_volume=3.5,
                    bucket_trade_count=10,
                    bucket_range_bps=28.0,
                    market_cluster="oil",
                    cluster_aligned=True,
                    cluster_leader="XYZ:CL",
                ),
            ],
            owned_symbols={"XYZ:CL"},
        )

        signals = service.evaluate_many(contexts)

        self.assertEqual(signals, [])

    def test_tradfi_service_cluster_v2_accepts_silver_and_index_breakouts_only(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_c = replace(
            config.pod_c,
            cluster_aware_v2_enabled=True,
            min_confidence=0.55,
        )
        service = TradfiTrendService(config.pod_c)
        contexts = [
            TradfiTrendContext(
                symbol="XYZ:SILVER",
                regime=Regime.TREND_EXPANSION.value,
                price=30.0,
                ema_fast=30.6,
                ema_slow=29.8,
                vwap_distance_bps=3.0,
                spread_bps=1.5,
                funding_rate=0.0,
                structure_score=0.28,
                book_imbalance=0.04,
                trade_flow_bias=0.05,
                bucket_range_bps=22.0,
                bucket_trade_count=8,
                bucket_volume=2.0,
                bucket_notional_usd=300.0,
                activity_ratio=1.2,
                trade_count_ratio=1.1,
                trend_bps=12.0,
                btc_aligned=True,
                market_cluster="silver",
                cluster_aligned=True,
                cluster_leader="XYZ:SILVER",
            ),
            TradfiTrendContext(
                symbol="XYZ:SP500",
                regime=Regime.TREND_EXPANSION.value,
                price=5100.0,
                ema_fast=5120.0,
                ema_slow=5088.0,
                vwap_distance_bps=2.0,
                spread_bps=1.0,
                funding_rate=0.0,
                structure_score=0.26,
                book_imbalance=0.05,
                trade_flow_bias=0.04,
                bucket_range_bps=18.0,
                bucket_trade_count=9,
                bucket_volume=1.0,
                bucket_notional_usd=150.0,
                activity_ratio=1.2,
                trade_count_ratio=1.0,
                trend_bps=9.0,
                btc_aligned=True,
                market_cluster="index",
                cluster_aligned=True,
                cluster_leader="XYZ:SP500",
            ),
            TradfiTrendContext(
                symbol="XYZ:GOLD",
                regime=Regime.TREND_EXPANSION.value,
                price=3200.0,
                ema_fast=3215.0,
                ema_slow=3190.0,
                vwap_distance_bps=2.0,
                spread_bps=1.0,
                funding_rate=0.0,
                structure_score=0.26,
                book_imbalance=0.05,
                trade_flow_bias=0.04,
                bucket_range_bps=18.0,
                bucket_trade_count=9,
                bucket_volume=1.0,
                bucket_notional_usd=150.0,
                activity_ratio=1.2,
                trade_count_ratio=1.0,
                trend_bps=9.0,
                btc_aligned=True,
                market_cluster="gold",
                cluster_aligned=True,
                cluster_leader="XYZ:GOLD",
                global_regime=Regime.TREND_EXPANSION.value,
                cluster_regime=Regime.TREND_EXPANSION.value,
            ),
        ]

        signals = service.evaluate_many(contexts)

        self.assertEqual(
            [signal.symbol for signal in signals],
            ["XYZ:SILVER", "XYZ:SP500", "XYZ:GOLD"],
        )
        self.assertEqual(signals[2].setup_details["cluster_strategy"], "gold_breakout_long")

    def test_tradfi_service_cluster_v2_rejects_gold_outside_expansion(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_c = replace(
            config.pod_c,
            cluster_aware_v2_enabled=True,
            min_confidence=0.55,
        )
        service = TradfiTrendService(config.pod_c)
        contexts = [
            TradfiTrendContext(
                symbol="XYZ:GOLD",
                regime=Regime.RANGE_AUCTION.value,
                price=3200.0,
                ema_fast=3215.0,
                ema_slow=3190.0,
                vwap_distance_bps=2.0,
                spread_bps=1.0,
                funding_rate=0.0,
                structure_score=0.26,
                book_imbalance=0.05,
                trade_flow_bias=0.04,
                bucket_range_bps=18.0,
                bucket_trade_count=9,
                bucket_volume=1.0,
                bucket_notional_usd=150.0,
                activity_ratio=1.2,
                trade_count_ratio=1.0,
                trend_bps=9.0,
                btc_aligned=True,
                market_cluster="gold",
                cluster_aligned=True,
                cluster_leader="XYZ:GOLD",
                global_regime=Regime.RANGE_AUCTION.value,
                cluster_regime=Regime.RANGE_AUCTION.value,
            ),
        ]

        signals = service.evaluate_many(contexts)

        self.assertEqual(signals, [])

    def test_tradfi_planner_builds_trade_plan(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_c = replace(config.pod_c, cluster_aware_v2_enabled=False)
        service = TradfiTrendService(config.pod_c)
        context_service = TradfiTrendContextService(config.pod_c, service)
        planner = TradfiTrendPlanner(config)
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
        self.assertGreater(plan.effective_leverage, 1.0)
        self.assertGreater(plan.margin_usd, 0.0)
        self.assertGreater(plan.target_notional_usd, plan.margin_usd)
        self.assertGreater(plan.risk_budget_usd, 0.0)
        self.assertGreater(plan.expected_loss_usd, 0.0)
        self.assertIn("trend_bps", plan.setup_details)
        self.assertIn("cluster_strategy", plan.setup_details)
        self.assertIn("take_profit_bps", plan.setup_details)
        self.assertEqual(plan.setup_details["cluster_strategy"], "index_breakout_long")
        self.assertAlmostEqual(plan.setup_details["stop_bps"], plan.stop_bps, places=4)

    def test_tradfi_planner_applies_cluster_mode_exit_overrides(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_c = replace(
            config.pod_c,
            cluster_aware_v2_enabled=False,
            cluster_modes={
                "index": PodCClusterModeConfig(
                    enabled=True,
                    allowed_setups=["tradfi_continuation_long"],
                    stop_bps_multiplier=1.1,
                    stop_bps_floor=60.0,
                    time_stop_hours=8,
                    take_profit_multiplier=1.2,
                    break_even_multiplier=1.1,
                    trailing_activation_multiplier=1.25,
                    trailing_distance_multiplier=1.15,
                )
            },
        )
        signal = TradfiTrendSignal(
            symbol="SPX",
            side="long",
            setup="tradfi_continuation_long",
            confidence=0.8,
            entry_price=5050.0,
            market_cluster="index",
            cluster_leader="SPX",
            setup_details={
                "market_cluster": "index",
                "cluster_strategy": "index_breakout_long",
            },
        )
        planner = TradfiTrendPlanner(config)
        allocation = PodAllocation(
            pod=PodName.POD_C,
            target_pct=0.3,
            target_usd=300.0,
            symbols=[SymbolAllocation(symbol="SPX", target_pct=0.3, target_usd=300.0)],
        )
        plan = planner.build_trade_plan(signal, allocation)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.time_stop_hours, 8)
        self.assertTrue(plan.setup_details["cluster_mode_active"])
        self.assertEqual(plan.setup_details["cluster_mode_name"], "index")
        self.assertGreaterEqual(plan.stop_bps, 60.0)

    def test_pod_c_day_by_day_recommendations_flags_losing_cluster_pattern(self) -> None:
        recommendations = _recommendations(
            [
                {
                    "date": "2026-04-14",
                    "setup": "tradfi_continuation_long",
                    "side": "long",
                    "pnl_usd": -4.2,
                    "setup_details": {
                        "market_cluster": "oil",
                        "cluster_strategy": "oil_pullback_long",
                        "cluster_regime": "TrendExpansion",
                        "trend_bucket": "medium",
                        "structure_bucket": "strong",
                        "vwap_bucket": "pullback",
                        "activity_bucket": "normal",
                        "flow_bucket": "strong",
                        "flow_alignment": "supportive",
                    },
                },
                {
                    "date": "2026-04-15",
                    "setup": "tradfi_continuation_long",
                    "side": "long",
                    "pnl_usd": -3.1,
                    "setup_details": {
                        "market_cluster": "oil",
                        "cluster_strategy": "oil_pullback_long",
                        "cluster_regime": "TrendExpansion",
                        "trend_bucket": "medium",
                        "structure_bucket": "strong",
                        "vwap_bucket": "pullback",
                        "activity_bucket": "normal",
                        "flow_bucket": "strong",
                        "flow_alignment": "supportive",
                    },
                },
                {
                    "date": "2026-04-16",
                    "setup": "tradfi_continuation_long",
                    "side": "long",
                    "pnl_usd": -2.9,
                    "setup_details": {
                        "market_cluster": "oil",
                        "cluster_strategy": "oil_pullback_long",
                        "cluster_regime": "TrendExpansion",
                        "trend_bucket": "medium",
                        "structure_bucket": "strong",
                        "vwap_bucket": "pullback",
                        "activity_bucket": "normal",
                        "flow_bucket": "strong",
                        "flow_alignment": "supportive",
                    },
                },
            ]
        )
        self.assertGreaterEqual(len(recommendations), 1)
        patterns = {item["pattern"] for item in recommendations}
        self.assertIn("oil|supportive|strong|normal", patterns)
        self.assertIn("oil|oil_pullback_long", patterns)

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

    def test_pod_c_risk_gate_blocks_configured_symbols(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_c.blocked_symbols = ["XYZ:GOLD"]
        gate = PodCRiskGate(config)
        decision = gate.evaluate_many(
            [
                TradePlan(
                    symbol="XYZ:GOLD",
                    side="long",
                    setup="tradfi_continuation_long",
                    confidence=max(config.pod_c.min_confidence, 0.8),
                    target_notional_usd=100.0,
                    stop_bps=45.0,
                    time_stop_hours=config.pod_c.time_stop_hours,
                    margin_usd=25.0,
                    effective_leverage=4.0,
                    risk_budget_usd=7.5,
                    expected_loss_usd=2.0,
                )
            ]
        )[0]
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "symbol_blocked")

    def test_pod_c_risk_gate_blocks_cluster_pattern_veto(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_c.pattern_vetoes = [
            PodAPatternVetoConfig(
                name="oil_supportive_strong_normal",
                setups=["tradfi_continuation_long"],
                sides=["long"],
                market_clusters=["oil"],
                flow_alignments=["supportive"],
                flow_buckets=["strong"],
                activity_buckets=["normal"],
            )
        ]
        gate = PodCRiskGate(config)
        decision = gate.evaluate_many(
            [
                TradePlan(
                    symbol="XYZ:CL",
                    side="long",
                    setup="tradfi_continuation_long",
                    confidence=0.8,
                    target_notional_usd=100.0,
                    stop_bps=45.0,
                    time_stop_hours=config.pod_c.time_stop_hours,
                    margin_usd=25.0,
                    effective_leverage=4.0,
                    risk_budget_usd=7.5,
                    expected_loss_usd=2.0,
                    setup_details={
                        "market_cluster": "oil",
                        "flow_alignment": "supportive",
                        "flow_bucket": "strong",
                        "activity_bucket": "normal",
                    },
                )
            ]
        )[0]
        self.assertFalse(decision.accepted)
        self.assertEqual(
            decision.reason,
            "pattern_veto_oil_supportive_strong_normal",
        )

    def test_default_config_blocks_pod_c_silver_strong_extension_veto(self) -> None:
        config = load_config("config/trident.toml")
        self.assertTrue(
            any(
                rule.name == "silver_strong_extension_veto"
                for rule in config.pod_c.pattern_vetoes
            )
        )
        config.pod_c = replace(config.pod_c, blocked_symbols=[])
        gate = PodCRiskGate(config)
        decision = gate.evaluate_many(
            [
                TradePlan(
                    symbol="XYZ:SILVER",
                    side="long",
                    setup="tradfi_continuation_long",
                    confidence=0.8,
                    target_notional_usd=100.0,
                    stop_bps=45.0,
                    time_stop_hours=config.pod_c.time_stop_hours,
                    margin_usd=25.0,
                    effective_leverage=4.0,
                    risk_budget_usd=7.5,
                    expected_loss_usd=2.0,
                    setup_details={
                        "market_cluster": "silver",
                        "trend_bucket": "strong",
                        "structure_bucket": "strong",
                        "vwap_bucket": "extension",
                    },
                )
            ]
        )[0]
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "pattern_veto_silver_strong_extension_veto")

    def test_pod_c_risk_gate_records_pattern_watch_hits(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_c.pattern_vetoes = []
        config.pod_c.pattern_watchers = [
            PodAPatternVetoConfig(
                name="oil_pullback_watch",
                setups=["tradfi_continuation_long"],
                market_clusters=["oil"],
                cluster_strategies=["oil_pullback_long"],
            )
        ]
        gate = PodCRiskGate(config)
        decision = gate.evaluate_many(
            [
                TradePlan(
                    symbol="XYZ:CL",
                    side="long",
                    setup="tradfi_continuation_long",
                    confidence=0.8,
                    target_notional_usd=100.0,
                    stop_bps=45.0,
                    time_stop_hours=config.pod_c.time_stop_hours,
                    margin_usd=25.0,
                    effective_leverage=4.0,
                    risk_budget_usd=7.5,
                    expected_loss_usd=2.0,
                    setup_details={
                        "market_cluster": "oil",
                        "cluster_strategy": "oil_pullback_long",
                    },
                )
            ]
        )[0]
        self.assertTrue(decision.accepted)
        self.assertEqual(
            decision.trade_plan.setup_details.get("pattern_watch_hits"),
            "oil_pullback_watch",
        )

    def test_pod_c_risk_gate_rejects_trade_plan_when_asset_leverage_limit_is_exceeded(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_c.max_leverage = 10.0
        config.pod_c.max_leverage_by_symbol = {"SPX": 5.0}
        gate = PodCRiskGate(config)

        decision = gate.evaluate_many(
            [
                TradePlan(
                    symbol="SPX",
                    side="long",
                    setup="tradfi_continuation_long",
                    confidence=0.8,
                    target_notional_usd=450.0,
                    stop_bps=80.0,
                    time_stop_hours=24,
                    margin_usd=75.0,
                    effective_leverage=6.0,
                    risk_budget_usd=7.5,
                    expected_loss_usd=3.6,
                )
            ]
        )[0]

        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "leverage_above_asset_limit")

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

    def test_pod_c_keeps_index_position_with_symbol_specific_routing_revoke_grace(self) -> None:
        config = load_config("config/trident.toml")
        config = replace(
            config,
            trident=replace(
                config.trident,
                execution=replace(
                    config.trident.execution,
                    routing_revoke_grace_minutes=0,
                    routing_revoke_grace_minutes_by_symbol={"XYZ:SP500": 540},
                ),
            ),
        )
        executor = DirectionalExecutor(config)
        decision = RiskDecision(
            accepted=True,
            reason="accepted",
            trade_plan=TradePlan(
                symbol="XYZ:SP500",
                side="long",
                setup="tradfi_continuation_long",
                confidence=0.78,
                target_notional_usd=120.0,
                stop_bps=55.0,
                time_stop_hours=24,
                reentry_cooldown_minutes=config.pod_c.reentry_cooldown_minutes,
                margin_usd=24.0,
                effective_leverage=5.0,
                risk_budget_usd=6.6,
                expected_loss_usd=2.2,
                setup_details={"market_cluster": "index"},
            ),
        )

        executor.process_record(
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="XYZ:SP500",
                    price=5100.0,
                    ema_fast=5094.0,
                    ema_slow=5078.0,
                    vwap_distance_bps=-3.0,
                    structure_score=0.28,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                    market_cluster="index",
                    cluster_aligned=True,
                    cluster_leader="XYZ:SP500",
                )
            ],
            risk_decisions=[decision],
            signal_sides_by_symbol={"XYZ:SP500": "long"},
            timestamp="2026-04-05T10:00:00Z",
            allowed_symbols={"XYZ:SP500"},
        )

        within_grace = executor.process_record(
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="XYZ:SP500",
                    price=5102.0,
                    ema_fast=5097.0,
                    ema_slow=5080.0,
                    vwap_distance_bps=-1.0,
                    structure_score=0.24,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                    market_cluster="index",
                    cluster_aligned=True,
                    cluster_leader="XYZ:SP500",
                )
            ],
            risk_decisions=[],
            signal_sides_by_symbol={},
            timestamp="2026-04-05T12:00:00Z",
            allowed_symbols=set(),
        )

        self.assertEqual(len(within_grace.closed_trades), 0)
        self.assertTrue(within_grace.has_open_position_after["XYZ:SP500"])

        after_grace = executor.process_record(
            snapshots=[
                SymbolMarketSnapshot(
                    symbol="XYZ:SP500",
                    price=5101.0,
                    ema_fast=5096.0,
                    ema_slow=5082.0,
                    vwap_distance_bps=-2.0,
                    structure_score=0.2,
                    funding_rate=0.0,
                    spread_bps=1.0,
                    btc_aligned=True,
                    market_cluster="index",
                    cluster_aligned=True,
                    cluster_leader="XYZ:SP500",
                )
            ],
            risk_decisions=[],
            signal_sides_by_symbol={},
            timestamp="2026-04-05T19:01:00Z",
            allowed_symbols=set(),
        )

        self.assertEqual(len(after_grace.closed_trades), 1)
        self.assertEqual(after_grace.closed_trades[0].close_reason, "routing_revoked")
        self.assertFalse(after_grace.has_open_position_after["XYZ:SP500"])

    def test_pod_c_live_runner_processes_records(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_c.enabled = True
        runner = PodCLiveRunner(config, coins=["SPY", "PAXG"])
        runner.collector = _FakeCollector()  # type: ignore[assignment]
        fake_external_reference = _FakeExternalReferenceEnricher()
        runner.external_reference_enricher = fake_external_reference  # type: ignore[assignment]
        status_path = Path("logs/pod_c_live_status.json")
        original_status = status_path.read_text(encoding="utf-8") if status_path.exists() else None
        with tempfile.TemporaryDirectory() as tmpdir:
            journal_path = Path(tmpdir) / "pod_c_live.jsonl"
            try:
                result = asyncio.run(runner.run(max_runtime_seconds=0.1, journal_path=journal_path))
                self.assertEqual(result["records_processed"], 1)
                self.assertGreaterEqual(fake_external_reference.calls, 1)
                runtime_status = json.loads(status_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    runtime_status["external_reference"]["symbols_enriched"],
                    fake_external_reference.symbols_enriched,
                )
                open_positions = runtime_status["open_positions"]
                self.assertIsInstance(open_positions, list)
                if open_positions:
                    spx_position = next(item for item in open_positions if item["symbol"] == "SPY")
                    self.assertEqual(spx_position["current_price"], 5100.0)
                    self.assertIn("margin_usd", spx_position)
                    self.assertIn("take_profit_bps", spx_position)
                    self.assertIn("trailing_activation_bps", spx_position)
                    self.assertIn("best_price_seen", spx_position)
            finally:
                if original_status is None:
                    status_path.unlink(missing_ok=True)
                else:
                    status_path.write_text(original_status, encoding="utf-8")

    def test_pod_c_live_runner_adds_external_reference_shadow_details(self) -> None:
        config = load_config("config/trident.toml")
        runner = PodCLiveRunner(config, coins=["XYZ:GOLD"])
        plan = TradePlan(
            symbol="XYZ:GOLD",
            side="long",
            setup="tradfi_continuation_long",
            confidence=0.78,
            target_notional_usd=100.0,
            stop_bps=40.0,
            time_stop_hours=4.0,
            setup_details={
                "external_reference_source_count": 1,
                "external_reference_age_seconds": 60.0,
                "external_premium_bps": -75.0,
                "external_momentum_300s_bps": 2.0,
            },
        )

        runner._apply_external_reference_shadow_to_plans([plan])
        preview_details = runner._preview_setup_details_with_external_reference_shadow(
            [
                SimpleNamespace(
                    symbol="XYZ:GOLD",
                    side="long",
                    setup_details=plan.setup_details,
                )
            ]
        )
        runner.supervisor.state.pod_c_signal_review = [
            {
                "symbol": "XYZ:GOLD",
                "preferred_side": "long",
                "setup_details": plan.setup_details,
            }
        ]
        runner._apply_external_reference_shadow_to_signal_reviews()

        self.assertEqual(plan.setup_details["external_reference_shadow_mode"], "observation_only")
        self.assertTrue(
            plan.setup_details["would_block_external_reference_abs_premium_gt_50"]
        )
        self.assertTrue(
            preview_details["XYZ:GOLD"]["external_reference_shadow_live_action_unchanged"]
        )
        self.assertTrue(
            runner.supervisor.state.pod_c_signal_review[0]["setup_details"][
                "external_reference_shadow_live_action_unchanged"
            ]
        )

    def test_pod_c_live_runner_adds_p109_oil_shadow_details(self) -> None:
        config = load_config("config/trident.toml")
        runner = PodCLiveRunner(config, coins=["XYZ:CL"])
        snapshot = SymbolMarketSnapshot(
            symbol="XYZ:CL",
            price=80.0,
            ema_fast=80.1,
            ema_slow=80.0,
            vwap_distance_bps=8.0,
            structure_score=0.0,
            funding_rate=0.0,
            spread_bps=2.0,
            btc_aligned=True,
            market_cluster="oil",
            realized_vol_short_bps=10.0,
        )
        shadow_by_symbol = runner._p109_oil_shadow_details_by_symbol(
            snapshots=[snapshot],
            timestamp="2026-06-15T08:00:00Z",
            cluster_regime_snapshots={
                "oil": RegimeSnapshot(
                    ready=True,
                    adx=10.0,
                    atr_ratio=0.2,
                    range_width_bps=10.0,
                    coherence_score=0.1,
                )
            },
        )
        plan = TradePlan(
            symbol="XYZ:CL",
            side="long",
            setup="oil_pullback_long",
            confidence=0.78,
            target_notional_usd=100.0,
            stop_bps=40.0,
            time_stop_hours=4.0,
            setup_details={"market_cluster": "oil"},
        )
        runner._apply_p109_oil_shadow_to_plans([plan], shadow_by_symbol)
        preview_details = runner._preview_setup_details_with_external_reference_shadow(
            [
                SimpleNamespace(
                    symbol="XYZ:CL",
                    side="long",
                    setup_details=plan.setup_details,
                )
            ],
            p109_oil_shadow_by_symbol=shadow_by_symbol,
        )
        runner.supervisor.state.pod_c_signal_review = [
            {
                "symbol": "XYZ:CL",
                "preferred_side": "long",
                "setup_details": {"market_cluster": "oil"},
            }
        ]
        runner._apply_p109_oil_shadow_to_signal_reviews(shadow_by_symbol)

        self.assertTrue(plan.setup_details["would_open_p109_oil_short_shadow"])
        self.assertEqual(
            preview_details["XYZ:CL"]["p109_oil_shadow_mode"],
            "observation_only",
        )
        self.assertTrue(
            runner.supervisor.state.pod_c_signal_review[0]["setup_details"][
                "p109_oil_shadow_live_action_unchanged"
            ]
        )

    def test_pod_c_live_runner_promotes_p109_oil_shadow_to_short_plan(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_c.p109_oil_short_enabled = True
        runner = PodCLiveRunner(config, coins=["XYZ:CL"])
        snapshot = SymbolMarketSnapshot(
            symbol="XYZ:CL",
            price=80.0,
            ema_fast=80.1,
            ema_slow=80.0,
            vwap_distance_bps=8.0,
            structure_score=0.0,
            funding_rate=0.0,
            spread_bps=2.0,
            btc_aligned=True,
            market_cluster="oil",
            realized_vol_short_bps=10.0,
            bucket_volume=1000.0,
            bucket_trade_count=10,
            source="live_test",
        )
        shadow_by_symbol = runner._p109_oil_shadow_details_by_symbol(
            snapshots=[snapshot],
            timestamp="2026-06-15T08:00:00Z",
            cluster_regime_snapshots={
                "oil": RegimeSnapshot(
                    ready=True,
                    adx=10.0,
                    atr_ratio=0.2,
                    range_width_bps=10.0,
                    coherence_score=0.1,
                )
            },
        )
        runner.supervisor.capital_plan.pod_allocations[PodName.POD_C] = PodAllocation(
            pod=PodName.POD_C,
            target_pct=0.06,
            target_usd=60.0,
            symbols=[SymbolAllocation(symbol="XYZ:CL", target_pct=0.06, target_usd=60.0)],
        )

        previews, plans = runner._apply_p109_oil_promotion(
            previews=[],
            trade_plans=[],
            snapshots=[snapshot],
            details_by_symbol=shadow_by_symbol,
        )

        self.assertEqual(len(previews), 1)
        self.assertEqual(len(plans), 1)
        self.assertEqual(previews[0].side, "short")
        self.assertEqual(previews[0].setup, "p109_oil_short_4h_time_gate")
        self.assertEqual(plans[0].side, "short")
        self.assertEqual(plans[0].setup, "p109_oil_short_4h_time_gate")
        self.assertEqual(plans[0].time_stop_hours, 4)
        self.assertTrue(plans[0].setup_details["p109_oil_promoted"])
        self.assertEqual(
            plans[0].setup_details["p109_oil_promoted_live_action"],
            "short_entry_candidate",
        )
        self.assertEqual(
            runner.supervisor.state.pod_c_signal_review[0]["setup"],
            "p109_oil_short_4h_time_gate",
        )

    def test_pod_c_live_runner_defaults_to_observation_universe_filtered_by_cluster(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_c.enabled = True
        config.hyperliquid.observation_universe = [
            "BTC",
            "ETH",
            "XYZ:GOLD",
            "XYZ:SP500",
            "XYZ:XYZ100",
            "XYZ:TSLA",
            "XYZ:JPY",
        ]
        config.pod_c.allowed_market_clusters = ["gold", "index", "fx"]

        runner = PodCLiveRunner(config)

        self.assertEqual(
            runner.coins,
            ["XYZ:GOLD", "XYZ:SP500", "XYZ:XYZ100", "XYZ:JPY"],
        )
        self.assertEqual(
            runner.config.hyperliquid.observation_universe,
            ["XYZ:GOLD", "XYZ:SP500", "XYZ:XYZ100", "XYZ:JPY"],
        )

    def test_pod_c_live_runner_keeps_blocked_gold_in_collection_scope(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_c.enabled = True
        config.hyperliquid.observation_universe = ["XYZ:GOLD", "XYZ:CL", "BTC"]
        config.pod_c.allowed_market_clusters = ["gold", "oil"]
        config.pod_c.blocked_symbols = ["XYZ:GOLD"]

        runner = PodCLiveRunner(config)

        self.assertEqual(runner.coins, ["XYZ:GOLD", "XYZ:CL"])

    def test_pod_c_maintenance_refresh_updates_open_position_market_data_without_new_records(self) -> None:
        config = load_config("config/trident.toml")
        config.pod_c.enabled = True
        runner = PodCLiveRunner(config, coins=["SPY"])
        plan = TradePlan(
            symbol="SPY",
            side="long",
            setup="tradfi_continuation_long",
            confidence=0.78,
            target_notional_usd=150.0,
            stop_bps=40.0,
            time_stop_hours=999999,
            take_profit_bps=500.0,
            break_even_trigger_bps=35.0,
            trailing_activation_bps=70.0,
            trailing_distance_bps=25.0,
        )
        opened = runner.executor.portfolio.open_from_plan(
            plan,
            price=5100.0,
            entry_fee_usd=0.1,
            timestamp="2026-04-12T10:00:00Z",
        )
        self.assertTrue(opened)
        runner._info_client = _FakeInfoClient({"SPY": 5140.0})  # type: ignore[assignment]
        runner._last_record_monotonic = 0.0

        refreshed = runner._refresh_open_positions_without_stream(
            journal=None,
            now=runner.MARKET_DATA_FALLBACK_IDLE_SECONDS + 1.0,
        )

        self.assertTrue(refreshed)
        open_positions = runner._build_open_positions_payload()
        self.assertEqual(len(open_positions), 1)
        self.assertEqual(open_positions[0]["current_price"], 5140.0)
        self.assertGreater(open_positions[0]["unrealized_pnl_usd"], 0.0)
        self.assertEqual(open_positions[0]["break_even_trigger_bps"], 35.0)
        self.assertEqual(open_positions[0]["trailing_activation_bps"], 70.0)
        self.assertEqual(open_positions[0]["trailing_distance_bps"], 25.0)


if __name__ == "__main__":
    unittest.main()
