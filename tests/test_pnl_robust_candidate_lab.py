from __future__ import annotations

import json
from pathlib import Path

from scripts.run_pnl_robust_candidate_lab import (
    CandidatePeriod,
    build_pod_a_combined_decisions,
    build_verdicts,
    load_p103_candidates,
    pod_a_combined_risk,
    pod_a_multiplier,
    summarize_adjusted_rows,
)


def test_pod_a_combined_risk_maps_multiple_weak_signals_to_deep_cap() -> None:
    points, reasons = pod_a_combined_risk(
        {
            "symbol_guard_state": "quarantine",
            "microstructure_shadow_bucket": "weak",
            "spread_bps": 5.0,
            "bucket_notional_usd": 500.0,
            "bucket_trade_count": 4.0,
            "pattern_watch_count": 2,
            "trade_flow_bias": -0.1,
        },
        side="long",
    )

    assert points == 7
    assert pod_a_multiplier(points) == 0.35
    assert "symbol_guard_quarantine" in reasons
    assert "weak_microstructure" in reasons
    assert "counter_flow" in reasons


def test_trade_level_summary_and_verdict_promotable_when_two_periods_positive() -> None:
    pre = summarize_adjusted_rows(
        candidate="fixture",
        pod="pod_a",
        family="combined_sizing",
        source="unit",
        period="pre_split",
        rows=[
            {"symbol": "ETH", "original_pnl_usd": -10.0, "adjusted_pnl_usd": -5.0, "touched": True},
            {"symbol": "BTC", "original_pnl_usd": 2.0, "adjusted_pnl_usd": 2.0, "touched": False},
        ],
        coverage_pct=100.0,
        sufficient_coverage=True,
    )
    post = summarize_adjusted_rows(
        candidate="fixture",
        pod="pod_a",
        family="combined_sizing",
        source="unit",
        period="post_split",
        rows=[
            {"symbol": "ETH", "original_pnl_usd": -6.0, "adjusted_pnl_usd": -3.0, "touched": True},
            {"symbol": "BTC", "original_pnl_usd": 1.0, "adjusted_pnl_usd": 1.0, "touched": False},
        ],
        coverage_pct=100.0,
        sufficient_coverage=True,
    )

    verdict = build_verdicts([pre, post], max_symbol_concentration_pct=70.0)[0]

    assert pre.delta_usd == 5.0
    assert post.delta_usd == 3.0
    assert verdict.classification == "promotable_candidate"
    assert verdict.total_delta_usd == 8.0


def test_verdict_shadow_when_positive_but_too_concentrated() -> None:
    rows = [
        period("fixture", "pre_split", delta=3.0, concentration=80.0),
        period("fixture", "post_split", delta=2.0, concentration=80.0),
    ]

    verdict = build_verdicts(rows, max_symbol_concentration_pct=70.0)[0]

    assert verdict.classification == "shadow_candidate"
    assert "symbol_concentration" in verdict.reasons


def test_verdict_shadow_when_one_covered_period_is_flat() -> None:
    rows = [
        period("fixture", "pre_split", delta=3.0, concentration=50.0),
        period("fixture", "post_split", delta=0.0, concentration=50.0),
    ]

    verdict = build_verdicts(rows, max_symbol_concentration_pct=70.0)[0]

    assert verdict.classification == "shadow_candidate"
    assert "flat_covered_periods=1" in verdict.reasons
    assert "passes_lab_filters" not in verdict.reasons


def test_p103_loader_marks_low_coverage_window_as_insufficient(tmp_path: Path) -> None:
    report = tmp_path / "p103.json"
    report.write_text(
        json.dumps(
            {
                "window_summaries": [
                    {"window": "baseline", "reference_coverage_pct": 0.0},
                    {"window": "live", "reference_coverage_pct": 91.0},
                ],
                "gate_outcomes": [
                    cap_outcome("baseline", delta=1.0, touched_pnl=-2.0),
                    cap_outcome("live", delta=4.0, touched_pnl=-8.0),
                ],
            }
        ),
        encoding="utf-8",
    )

    periods = load_p103_candidates(report, min_coverage_pct=80.0)
    verdict = build_verdicts(periods, max_symbol_concentration_pct=70.0)[0]

    assert len(periods) == 2
    assert periods[0].sufficient_coverage is False
    assert periods[1].sufficient_coverage is True
    assert verdict.classification == "shadow_candidate"
    assert "insufficient_coverage" in verdict.reasons


def test_build_pod_a_decisions_reads_trade_close_jsonl(tmp_path: Path) -> None:
    journal = tmp_path / "journal.jsonl"
    journal.write_text(
        json.dumps(
            {
                "event_type": "trade_close",
                "trade": {
                    "symbol": "ETH",
                    "side": "long",
                    "pnl_usd": -10.0,
                    "close_reason": "stop_hit",
                    "opened_at": "2026-06-04T00:00:00+00:00",
                    "closed_at": "2026-06-04T01:00:00+00:00",
                    "setup_details": {
                        "microstructure_shadow_bucket": "poor",
                        "spread_bps": 5.0,
                        "bucket_notional_usd": 500.0,
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    decisions = build_pod_a_combined_decisions(journal, split_date="2026-06-03")

    assert len(decisions) == 1
    assert decisions[0].period == "post_split"
    assert decisions[0].multiplier == 0.35
    assert decisions[0].adjusted_pnl_usd == -3.5


def cap_outcome(window: str, *, delta: float, touched_pnl: float) -> dict[str, object]:
    return {
        "window": window,
        "gate": "cap50_candidate_default_5m",
        "action": "cap50",
        "base_pnl_usd": 0.0,
        "kept_pnl_usd": delta,
        "delta_usd": delta,
        "total_trades": 10,
        "blocked_trades": 2,
        "blocked_pnl_usd": touched_pnl,
        "blocked_winners": 0,
        "blocked_losers": 2,
        "blocked_symbols": {"XYZ:GOLD": 1, "XYZ:CL": 1},
    }


def period(candidate: str, period_name: str, *, delta: float, concentration: float) -> CandidatePeriod:
    return CandidatePeriod(
        candidate=candidate,
        pod="pod_a",
        family="unit",
        source="unit",
        period=period_name,
        base_pnl_usd=0.0,
        adjusted_pnl_usd=delta,
        delta_usd=delta,
        trades=10,
        touched_trades=2,
        touched_pnl_usd=-delta * 2,
        touched_winners=0,
        touched_losers=2,
        capped_winner_pnl_usd=0.0,
        capped_loser_pnl_usd=-delta * 2,
        base_profit_factor=None,
        adjusted_profit_factor=None,
        win_rate=None,
        coverage_pct=100.0,
        sufficient_coverage=True,
        top_symbol="ETH",
        top_symbol_count=8,
        max_symbol_concentration_pct=concentration,
        symbols={"ETH": 8, "BTC": 2},
    )
