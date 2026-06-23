from __future__ import annotations

import json
from pathlib import Path

from scripts.run_p103_pod_c_external_reference_validation import (
    EnrichedTrade,
    QuotePoint,
    _enrich_trades,
    _evaluate_cap,
    _evaluate_gate,
    _gate_reason,
    _load_pod_c_trade_close_journal,
    _parse_dt,
    run_validation,
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


def test_fresh_only_gate_ignores_stale_reference_even_with_large_premium() -> None:
    stale_loser = _trade(pnl_usd=-3.0, reference_age_seconds=3600.0, external_premium_bps=200.0)
    fresh_loser = _trade(pnl_usd=-4.0, reference_age_seconds=60.0, external_premium_bps=75.0)

    outcome = _evaluate_cap("fixture", [stale_loser, fresh_loser], "fresh_candidate_default_5m", 0.5)

    assert _gate_reason(stale_loser, "fresh_candidate_default_5m") is None
    assert _gate_reason(fresh_loser, "fresh_candidate_default_5m") == "premium"
    assert outcome.base_pnl_usd == -7.0
    assert outcome.delta_usd == 2.0
    assert outcome.blocked_trades == 1
    assert outcome.stale_blocks == 0


def test_fresh_context_gate_requires_metadata_filter() -> None:
    high_activity = _trade(
        pnl_usd=-3.0,
        reference_age_seconds=60.0,
        external_premium_bps=80.0,
        activity_bucket="high",
    )
    normal_activity = _trade(
        pnl_usd=-4.0,
        reference_age_seconds=60.0,
        external_premium_bps=80.0,
        activity_bucket="normal",
    )

    outcome = _evaluate_cap(
        "fixture",
        [high_activity, normal_activity],
        "fresh_candidate_default_5m_not_high_activity",
        0.5,
    )

    assert _gate_reason(high_activity, "fresh_candidate_default_5m_not_high_activity") is None
    assert _gate_reason(normal_activity, "fresh_candidate_default_5m_not_high_activity") == "premium"
    assert outcome.delta_usd == 2.0
    assert outcome.blocked_trades == 1


def test_enrich_trades_prefers_embedded_setup_reference() -> None:
    trade = {
        "symbol": "XYZ:CL",
        "side": "short",
        "opened_at": "2026-06-23T07:00:00+00:00",
        "entry_price": 72.833,
        "pnl_usd": -1.4,
        "setup_details": {
            "external_reference_available": True,
            "external_reference_price": 73.58999634,
            "external_reference_source_count": 1,
            "external_reference_symbol": "yahoo:CL=F",
            "external_reference_time": "2026-06-23T06:51:02Z",
            "external_reference_age_seconds": 538.0,
            "external_premium_bps": -102.8678,
            "external_momentum_300s_bps": 50.9085,
            "activity_bucket": "high",
        },
    }

    enriched = _enrich_trades({"journal": [trade]}, {}, prefer_embedded_reference=True)

    assert len(enriched) == 1
    assert enriched[0].reference_available is True
    assert enriched[0].reference_data_source == "embedded_setup_details"
    assert enriched[0].external_premium_bps == -102.8678
    assert enriched[0].activity_bucket == "high"
    assert _gate_reason(enriched[0], "candidate_default_5m") == "premium"


def test_enrich_trades_ignores_zeroed_embedded_reference_payload() -> None:
    opened_at = _parse_dt("2026-06-23T07:00:00+00:00")
    assert opened_at is not None
    trade = {
        "symbol": "XYZ:CL",
        "side": "short",
        "opened_at": "2026-06-23T07:00:00+00:00",
        "entry_price": 72.833,
        "pnl_usd": -1.4,
        "setup_details": {
            "external_reference_available": False,
            "external_reference_price": 0.0,
            "external_reference_source_count": 0,
            "external_reference_symbol": "",
            "external_reference_time": "",
            "external_reference_age_seconds": 0.0,
            "external_premium_bps": 0.0,
            "external_momentum_300s_bps": 0.0,
        },
    }

    enriched = _enrich_trades(
        {"report": [trade]},
        {"XYZ:CL": [QuotePoint(timestamp=opened_at, price=73.0)]},
        prefer_embedded_reference=True,
    )

    assert enriched[0].reference_available is True
    assert enriched[0].reference_data_source == "yahoo_chart"
    assert enriched[0].reference_price == 73.0


def test_trade_close_journal_loader_splits_oos_windows(tmp_path: Path) -> None:
    journal = tmp_path / "pod_c_live.jsonl"
    journal.write_text(
        "\n".join(
            [
                json.dumps(trade_close("2026-06-16T10:00:00+00:00", pnl=-1.0)),
                json.dumps(trade_close("2026-06-22T10:00:00+00:00", pnl=2.0)),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    windows = _load_pod_c_trade_close_journal(
        journal,
        start=_parse_dt("2026-06-15T00:00:00Z"),
        end=None,
        splits=[_parse_dt("2026-06-22T00:00:00Z")],  # type: ignore[list-item]
    )

    assert sorted(windows) == ["2026-06-15_to_2026-06-21", "2026-06-22_to_2026-06-22"]
    assert len(windows["2026-06-15_to_2026-06-21"]) == 1
    assert len(windows["2026-06-22_to_2026-06-22"]) == 1


def test_run_validation_from_journal_uses_embedded_references(tmp_path: Path) -> None:
    journal = tmp_path / "pod_c_live.jsonl"
    journal.write_text(
        "\n".join(
            [
                json.dumps(trade_close("2026-06-16T10:00:00+00:00", pnl=2.0, premium=75.0)),
                json.dumps(trade_close("2026-06-22T10:00:00+00:00", pnl=-4.0, premium=75.0)),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    report = run_validation(
        report_paths=[],
        journal_paths=[journal],
        output_dir=tmp_path / "out",
        cache_dir=tmp_path / "cache",
        interval="5m",
        timeout_seconds=0.1,
        journal_start=_parse_dt("2026-06-15T00:00:00Z"),
        journal_splits=[_parse_dt("2026-06-22T00:00:00Z")],  # type: ignore[list-item]
    )

    cap_rows = [row for row in report.gate_outcomes if row.gate == "cap50_abs_premium_gt_50"]

    assert [row.reference_coverage_pct for row in report.window_summaries] == [100.0, 100.0]
    assert len(cap_rows) == 2
    assert cap_rows[0].delta_usd == -1.0
    assert cap_rows[1].delta_usd == 2.0


def trade_close(opened_at: str, *, pnl: float, premium: float = 75.0) -> dict[str, object]:
    return {
        "event_type": "trade_close",
        "timestamp": opened_at,
        "trade": {
            "symbol": "XYZ:GOLD",
            "side": "long",
            "opened_at": opened_at,
            "closed_at": opened_at,
            "entry_price": 4300.0,
            "pnl_usd": pnl,
            "setup_details": {
                "external_reference_available": True,
                "external_reference_price": 4290.0,
                "external_reference_source_count": 1,
                "external_reference_symbol": "yahoo:GC=F",
                "external_reference_time": opened_at,
                "external_reference_age_seconds": 0.0,
                "external_premium_bps": premium,
                "external_momentum_300s_bps": 0.0,
            },
        },
    }
