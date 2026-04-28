from dataclasses import replace

from app.settings import load_config
from app.trident.pod_b.hyperps import (
    COOLING_OFF_PHASE,
    RETIRED_PHASE,
    HyperpLifecyclePolicy,
    HyperpReversionContext,
    HyperpReversionPlanner,
    HyperpReversionProfile,
    HyperpReversionService,
    HyperpThresholds,
    HyperpUniverseRegistry,
    HyperpUniverseSnapshot,
    extract_active_hyperp_symbols,
)
from app.trident.types import PodAllocation, PodName, SymbolAllocation, SymbolMarketSnapshot


def _snapshot(**overrides: object) -> SymbolMarketSnapshot:
    data = {
        "symbol": "PENGU",
        "price": 1.04,
        "ema_fast": 1.0,
        "ema_slow": 0.98,
        "vwap_distance_bps": 25.0,
        "structure_score": 0.20,
        "funding_rate": 0.000013,
        "spread_bps": 3.0,
        "btc_aligned": True,
        "book_imbalance": 0.55,
        "trade_flow_bias": 0.70,
        "bucket_trade_count": 40,
        "bucket_notional_usd": 20_000.0,
        "bucket_range_bps": 40.0,
        "delta_book_imbalance": 0.0,
        "delta_trade_flow_bias": 0.0,
        "volume_ratio": 4.0,
        "trade_count_ratio": 2.0,
        "realized_vol_short_bps": 24.0,
        "realized_vol_long_bps": 30.0,
        "compression_score": 0.70,
    }
    data.update(overrides)
    return SymbolMarketSnapshot(**data)


def _thresholds() -> HyperpThresholds:
    return HyperpThresholds(
        symbol="PENGU",
        positive_funding_extreme=0.000013,
        negative_funding_extreme=-0.00003,
        abs_deviation_extreme_bps=75.0,
        event_range_bps=120.0,
        event_volume_ratio=8.0,
    )


def test_flow_exhaustion_fade_produces_short_plan() -> None:
    profile = replace(
        HyperpReversionProfile(),
        trigger_mode="flow_exhaustion_fade",
        short_rsi_min=45.0,
        max_spread_bps=5.0,
        min_interest_score=0.50,
        block_event_spikes=False,
    )
    service = HyperpReversionService(profile)
    signal = service.evaluate(
        HyperpReversionContext(
            snapshot=_snapshot(),
            regime="DeadZone",
            thresholds=_thresholds(),
            rsi14=58.0,
            price_move_bps=30.0,
        )
    )

    assert signal is not None
    assert signal.side == "short"
    assert signal.setup == "hyperp_flow_exhaustion_short"

    planner = HyperpReversionPlanner(load_config("config/trident.toml"), profile)
    plan = planner.build_trade_plan(
        signal,
        PodAllocation(
            pod=PodName.POD_B,
            target_pct=0.20,
            target_usd=200.0,
            symbols=[SymbolAllocation(symbol="PENGU", target_pct=0.20, target_usd=200.0)],
        ),
    )

    assert plan is not None
    assert plan.side == "short"
    assert plan.target_notional_usd > 0.0
    assert plan.take_profit_bps > 0.0


def test_flow_exhaustion_fade_blocks_oversold_short() -> None:
    profile = replace(
        HyperpReversionProfile(),
        trigger_mode="flow_exhaustion_fade",
        short_rsi_min=45.0,
        max_spread_bps=5.0,
        min_interest_score=0.50,
        block_event_spikes=False,
    )
    service = HyperpReversionService(profile)

    signal = service.evaluate(
        HyperpReversionContext(
            snapshot=_snapshot(price=0.96, ema_fast=1.0, funding_rate=-0.000025),
            regime="DeadZone",
            thresholds=_thresholds(),
            rsi14=33.0,
            price_move_bps=25.0,
        )
    )

    assert signal is None


def test_extract_active_hyperps_from_live_meta_shape() -> None:
    payload = [
        {
            "universe": [
                {"name": "BTC", "maxLeverage": 50},
                {"name": "MEGA", "maxLeverage": 3, "marginMode": "strictIsolated", "onlyIsolated": True},
                {"name": "OLD", "maxLeverage": 3, "marginMode": "strictIsolated", "isDelisted": True},
            ]
        },
        [],
    ]

    assert extract_active_hyperp_symbols(payload) == ["MEGA"]


def test_hyperp_lifecycle_keeps_recent_exits_then_retires() -> None:
    registry = HyperpUniverseRegistry(
        [
            HyperpUniverseSnapshot(timestamp="2026-04-01T00:00:00Z", symbols=("MEGA",)),
            HyperpUniverseSnapshot(timestamp="2026-04-10T00:00:00Z", symbols=()),
        ],
        policy=HyperpLifecyclePolicy(
            half_life_days=30,
            cooling_off_days=30,
            retired_after_days=90,
            min_trade_weight=0.10,
        ),
    )

    cooling = registry.state_for("MEGA", "2026-04-20T00:00:00Z")
    retired = registry.state_for("MEGA", "2026-08-01T00:00:00Z")

    assert cooling.phase == COOLING_OFF_PHASE
    assert cooling.tradable
    assert 0.0 < cooling.weight < 1.0
    assert retired.phase == RETIRED_PHASE
    assert not retired.tradable


def test_lifecycle_weight_reduces_plan_size() -> None:
    profile = replace(
        HyperpReversionProfile(),
        trigger_mode="flow_exhaustion_fade",
        short_rsi_min=45.0,
        max_spread_bps=5.0,
        min_interest_score=0.50,
        block_event_spikes=False,
    )
    service = HyperpReversionService(profile)
    active_signal = service.evaluate(
        HyperpReversionContext(
            snapshot=_snapshot(),
            regime="DeadZone",
            thresholds=_thresholds(),
            rsi14=58.0,
            price_move_bps=30.0,
        )
    )
    cooling_signal = service.evaluate(
        HyperpReversionContext(
            snapshot=_snapshot(
                bucket_notional_usd=30_000.0,
                bucket_trade_count=60,
                volume_ratio=5.0,
                trade_count_ratio=3.0,
            ),
            regime="DeadZone",
            thresholds=_thresholds(),
            rsi14=58.0,
            price_move_bps=30.0,
            lifecycle_phase=COOLING_OFF_PHASE,
            lifecycle_weight=0.35,
            lifecycle_days_since_active=20.0,
            lifecycle_strictness=1.10,
        )
    )
    assert active_signal is not None
    assert cooling_signal is not None
    planner = HyperpReversionPlanner(load_config("config/trident.toml"), profile)
    allocation = PodAllocation(
        pod=PodName.POD_B,
        target_pct=0.20,
        target_usd=200.0,
        symbols=[SymbolAllocation(symbol="PENGU", target_pct=0.20, target_usd=200.0)],
    )

    active_plan = planner.build_trade_plan(active_signal, allocation)
    cooling_plan = planner.build_trade_plan(cooling_signal, allocation)

    assert active_plan is not None
    assert cooling_plan is not None
    assert cooling_plan.risk_budget_usd < active_plan.risk_budget_usd
    assert cooling_plan.target_notional_usd < active_plan.target_notional_usd
