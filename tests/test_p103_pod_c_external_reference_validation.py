from __future__ import annotations

from scripts.run_p103_pod_c_external_reference_validation import (
    EnrichedTrade,
    _evaluate_cap,
    _evaluate_gate,
    _gate_reason,
)


def _trade(**overrides: object) -> EnrichedTrade:
    payload = {
        "window": "fixture",
        "symbol": "XYZ:GOLD",
        "side": "long",
        "opened_at": "2026-06-01T00:00:00+00:00",
        "pnl_usd": -3.0,
        "entry_price": 4300.0,
        "reference_symbol": "GC=F",
        "reference_price": 4290.0,
        "reference_time": "2026-06-01T00:00:00+00:00",
        "reference_age_seconds": 0.0,
        "external_premium_bps": 23.31,
        "external_momentum_300s_bps": 2.0,
        "reference_available": True,
    }
    payload.update(overrides)
    return EnrichedTrade(**payload)


def test_missing_or_stale_gate_blocks_old_reference() -> None:
    trade = _trade(reference_age_seconds=901.0)

    assert _gate_reason(trade, "missing_or_stale_15m") == "stale"


def test_gate_delta_is_positive_when_blocking_losing_trade() -> None:
    losing_trade = _trade(symbol="XYZ:GOLD", pnl_usd=-3.0, external_premium_bps=75.0)
    winning_trade = _trade(symbol="XYZ:SP500", pnl_usd=2.0, external_premium_bps=10.0)

    outcome = _evaluate_gate("fixture", [losing_trade, winning_trade], "abs_premium_gt_50")

    assert outcome.base_pnl_usd == -1.0
    assert outcome.kept_pnl_usd == 2.0
    assert outcome.delta_usd == 3.0
    assert outcome.blocked_trades == 1
    assert outcome.premium_blocks == 1


def test_cap_outcome_scales_touched_trade_instead_of_removing_it() -> None:
    losing_trade = _trade(symbol="XYZ:GOLD", pnl_usd=-4.0, external_premium_bps=75.0)
    winning_trade = _trade(symbol="XYZ:SP500", pnl_usd=2.0, external_premium_bps=10.0)

    outcome = _evaluate_cap("fixture", [losing_trade, winning_trade], "abs_premium_gt_50", 0.5)

    assert outcome.action == "cap50"
    assert outcome.base_pnl_usd == -2.0
    assert outcome.kept_pnl_usd == 0.0
    assert outcome.delta_usd == 2.0
    assert outcome.blocked_trades == 1
