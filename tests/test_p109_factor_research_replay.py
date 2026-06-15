from argparse import Namespace
from datetime import datetime, timezone

import pytest

from scripts.run_p109_factor_research_replay import (
    FactorTrade,
    FactorVariant,
    Hip4Settlement,
    Obs,
    apply_portfolio_constraints,
    classify_factor_metrics,
    default_variants,
    hip4_metrics,
    hip4_policy_rows,
    make_factor_trade,
)


def _args() -> Namespace:
    return Namespace(
        notional_usd=200.0,
        fee_slippage_bps=16.0,
        extra_slippage_bps=0.0,
        max_all_in_cost_bps=32.0,
        extended_alt_max_spread_bps=15.0,
        crypto_max_spread_bps=12.0,
        hip4_quality_threshold=0.75,
        hip4_max_book_age_ms=30_000.0,
        hip4_max_reference_divergence_bps=50.0,
    )


def _obs(symbol: str = "XYZ:CL", ts: int = 1_800_000_000, price: float = 100.0) -> Obs:
    return Obs(
        ts=ts,
        symbol=symbol,
        cluster="oil",
        price=price,
        hour=8,
        dow=1,
        regime="chop",
        spread_bps=2.0,
        structure=0.0,
        flow=0.0,
        book=0.0,
        vwap=0.0,
        micro=0.0,
        compression=0.0,
        vol_short=10.0,
        range_bps=10.0,
        volume_ratio=1.0,
        trade_count_ratio=1.0,
        notional=100_000.0,
        source_file="test.jsonl",
    )


def test_factor_trade_short_direction_subtracts_all_in_cost() -> None:
    variant = default_variants()[0]
    trade = make_factor_trade(
        variant=variant,
        obs=_obs(),
        future_price=99.0,
        reason="test",
        score=1.0,
        args=_args(),
    )

    assert trade.gross_return_bps == pytest.approx(100.0)
    assert trade.all_in_cost_bps == 18.0
    assert trade.net_return_bps == pytest.approx(82.0)
    assert trade.net_pnl_usd == pytest.approx(1.64)


def _trade(symbol: str, ts: int, *, net_pnl: float = 1.0) -> FactorTrade:
    return FactorTrade(
        variant="crypto_alt_short_4h_weak_basket",
        timestamp="2026-01-01T00:00:00Z",
        ts=ts,
        exit_timestamp="2026-01-01T04:00:00Z",
        symbol=symbol,
        cluster="crypto",
        side="short",
        horizon_min=240,
        regime="mixed",
        hour_utc=0,
        dow_utc=0,
        month="2026-01",
        entry_price=100.0,
        exit_price=99.0,
        gross_return_bps=100.0,
        spread_bps=2.0,
        fee_slippage_bps=16.0,
        all_in_cost_bps=18.0,
        net_return_bps=82.0,
        notional_usd=200.0,
        gross_pnl_usd=2.0,
        cost_usd=0.36,
        net_pnl_usd=net_pnl,
        score=1.0,
        reason="test",
    )


def test_portfolio_constraints_apply_symbol_cooldown_and_correlation_cap() -> None:
    variant = FactorVariant(
        name="crypto_alt_short_4h_weak_basket",
        description="test",
        side="short",
        horizon_min=240,
        cluster_cap_key="crypto",
        cooldown_min=240,
        selector=lambda _obs, _prices, _args: None,
    )
    rows = [
        _trade("PENGU", 1_800_000_000),
        _trade("TIA", 1_800_000_000),
        _trade("VVV", 1_800_000_000),
        _trade("PENGU", 1_800_000_300),
    ]

    kept, constraints = apply_portfolio_constraints(rows, variant=variant, max_correlated_positions=2)

    assert [row.symbol for row in kept] == ["PENGU", "TIA"]
    assert constraints.dropped_correlation_cap == 1
    assert constraints.dropped_symbol_cooldown == 1


def test_classification_promotes_only_strong_research_to_shadow_candidate() -> None:
    assert classify_factor_metrics(
        {
            "trade_count": 40,
            "net_pnl_usd": 80.0,
            "profit_factor": 1.35,
            "positive_months": 2,
            "max_drawdown_usd": 20.0,
            "max_symbol_trade_concentration": 0.4,
        }
    )["status"] == "promouvable_shadow"

    assert classify_factor_metrics(
        {
            "trade_count": 40,
            "net_pnl_usd": -1.0,
            "profit_factor": 0.9,
            "positive_months": 2,
            "max_drawdown_usd": 20.0,
            "max_symbol_trade_concentration": 0.4,
        }
    )["status"] == "rejetee"


def test_hip4_skip_buy_no_6_18h_removes_only_that_bucket() -> None:
    base_ts = datetime(2026, 6, 1, tzinfo=timezone.utc)
    settlements = [
        Hip4Settlement(
            ts=base_ts,
            open_ts=base_ts,
            market_id="BTC_GT_1_20260602_0600",
            underlying="BTC",
            side="BUY_NO",
            edge_type="MODEL",
            result="YES",
            pnl_usdc=-10.0,
            fee_usdc=0.0,
            is_win=False,
            open_seconds_to_expiry=8 * 3600,
        ),
        Hip4Settlement(
            ts=base_ts,
            open_ts=base_ts,
            market_id="BTC_GT_2_20260602_0600",
            underlying="BTC",
            side="BUY_NO",
            edge_type="MODEL",
            result="NO",
            pnl_usdc=5.0,
            fee_usdc=0.0,
            is_win=True,
            open_seconds_to_expiry=20 * 3600,
        ),
        Hip4Settlement(
            ts=base_ts,
            open_ts=base_ts,
            market_id="BTC_GT_3_20260602_0600",
            underlying="BTC",
            side="BUY_YES",
            edge_type="MODEL",
            result="YES",
            pnl_usdc=4.0,
            fee_usdc=0.0,
            is_win=True,
            open_seconds_to_expiry=8 * 3600,
        ),
    ]

    policies = hip4_policy_rows(settlements, quality={}, args=_args())
    metrics = hip4_metrics(policies["hip4_skip_buy_no_6_18h"], baseline_count=len(settlements))

    assert len(policies["hip4_skip_buy_no_6_18h"]) == 2
    assert metrics["net_pnl_usdc"] == 9.0
