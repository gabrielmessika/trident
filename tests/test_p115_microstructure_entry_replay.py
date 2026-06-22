from app.trident.types import TradePlan
from scripts.export_trident_audit_pack import compact_setup_details
from scripts.run_p115_microstructure_entry_replay import (
    ScenarioSpec,
    apply_microstructure_cap,
    summarize_microstructure_buckets,
    summarize_microstructure_trades,
)


def test_apply_microstructure_cap_scales_risk_values_without_blocking() -> None:
    plan = _plan(
        setup_details={
            "microstructure_shadow_score": 0.35,
            "microstructure_shadow_bucket": "poor",
        }
    )
    scenario = ScenarioSpec(
        "micro_cap_poor50_lt42",
        "test",
        score_threshold=0.42,
        cap_multiplier=0.50,
    )

    adjusted, capped = apply_microstructure_cap(plan, scenario)

    assert capped
    assert adjusted.target_notional_usd == 100.0
    assert adjusted.margin_usd == 20.0
    assert adjusted.risk_budget_usd == 4.0
    assert adjusted.expected_loss_usd == 2.0
    assert adjusted.setup_details["p115_microstructure_cap_counterfactual"] is True
    assert adjusted.setup_details["p115_microstructure_cap_multiplier"] == 0.5
    assert adjusted.setup_details["p115_microstructure_original_target_notional_usd"] == 200.0


def test_apply_microstructure_cap_leaves_good_scores_unchanged() -> None:
    plan = _plan(
        setup_details={
            "microstructure_shadow_score": 0.62,
            "microstructure_shadow_bucket": "ok",
        }
    )
    scenario = ScenarioSpec(
        "micro_cap_weak50_lt56",
        "test",
        score_threshold=0.56,
        cap_multiplier=0.50,
    )

    adjusted, capped = apply_microstructure_cap(plan, scenario)

    assert not capped
    assert adjusted == plan


def test_summarize_microstructure_trades_counts_buckets_and_worst_bucket() -> None:
    trades = [
        _trade("poor", 0.31, -3.0),
        _trade("poor", 0.39, 1.0),
        _trade("weak", 0.51, -1.5),
        _trade("ok", 0.66, 2.0),
        {"pnl_usd": -0.5, "setup_details": {}},
    ]

    summary = summarize_microstructure_trades(trades)

    assert summary["poor_trades"] == 2
    assert summary["poor_pnl_usd"] == -2.0
    assert summary["weak_trades"] == 1
    assert summary["ok_trades"] == 1
    assert summary["missing_score_trades"] == 1
    assert summary["worst_micro_bucket"] == "poor"


def test_summarize_microstructure_buckets_reports_cap_reductions() -> None:
    rows = summarize_microstructure_buckets(
        window="fixture",
        scenario="micro_cap_poor50_lt42",
        trades=[
            _trade("poor", 0.35, -2.0, capped=True),
            _trade("weak", 0.50, 1.0, capped=False),
        ],
    )

    by_bucket = {row.bucket: row for row in rows}
    assert by_bucket["poor"].closed_trades == 1
    assert by_bucket["poor"].cap_reduced_trades == 1
    assert by_bucket["weak"].cap_reduced_trades == 0


def test_compact_setup_details_exports_p115_fields() -> None:
    compacted = compact_setup_details(
        {
            "microstructure_shadow_active": True,
            "microstructure_shadow_score": 0.41,
            "microstructure_shadow_bucket": "poor",
            "microstructure_shadow_flow_score": 0.2,
            "p115_microstructure_cap_counterfactual": True,
            "p115_microstructure_cap_multiplier": 0.5,
            "p115_microstructure_original_target_notional_usd": 200.0,
            "ignored": "not-exported",
        }
    )

    assert compacted["microstructure_shadow_active"] is True
    assert compacted["microstructure_shadow_score"] == 0.41
    assert compacted["microstructure_shadow_bucket"] == "poor"
    assert compacted["p115_microstructure_cap_counterfactual"] is True
    assert compacted["p115_microstructure_cap_multiplier"] == 0.5
    assert "ignored" not in compacted


def _plan(setup_details: dict[str, object]) -> TradePlan:
    return TradePlan(
        symbol="ETH",
        side="long",
        setup="trend_pullback_long",
        confidence=0.78,
        target_notional_usd=200.0,
        stop_bps=120.0,
        time_stop_hours=6,
        margin_usd=40.0,
        risk_budget_usd=8.0,
        expected_loss_usd=4.0,
        setup_details=setup_details,
    )


def _trade(
    bucket: str,
    score: float,
    pnl_usd: float,
    *,
    capped: bool = False,
) -> dict[str, object]:
    return {
        "pnl_usd": pnl_usd,
        "setup_details": {
            "microstructure_shadow_bucket": bucket,
            "microstructure_shadow_score": score,
            "p115_microstructure_cap_counterfactual": capped,
        },
    }
