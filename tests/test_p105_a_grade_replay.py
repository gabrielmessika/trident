from pathlib import Path

from app.backtest.full_bot_replay import FullBotBacktestResult
from app.settings import load_config
from scripts.export_trident_audit_pack import compact_setup_details
from scripts.run_p105_a_grade_replay import (
    ScenarioSpec,
    WindowSpec,
    default_scenarios,
    scenario_config,
    selected_names,
    selected_scenarios,
    summarize_result,
)


def test_scenario_config_overrides_a_grade_scales_without_mutating_base() -> None:
    config = load_config("config/trident.toml")
    original_standard = config.pod_a.a_grade_boost_scale
    original_strong = config.pod_a.a_grade_strong_boost_scale
    scenario = ScenarioSpec(
        "flat_scale_1p00",
        "test",
        standard_scale=1.0,
        strong_scale=1.0,
    )

    changed = scenario_config(config, scenario)

    assert changed.pod_a.a_grade_enabled
    assert changed.pod_a.a_grade_boost_scale == 1.0
    assert changed.pod_a.a_grade_strong_boost_scale == 1.0
    assert not changed.pod_a.a_grade_size_headroom_cap_enabled
    assert config.pod_a.a_grade_boost_scale == original_standard
    assert config.pod_a.a_grade_strong_boost_scale == original_strong


def test_scenario_config_can_enable_dormant_a_grade_headroom_cap() -> None:
    config = load_config("config/trident.toml")
    scenario = ScenarioSpec(
        "headroom_cap_current",
        "test",
        standard_scale=config.pod_a.a_grade_boost_scale,
        strong_scale=config.pod_a.a_grade_strong_boost_scale,
        headroom_cap_enabled=True,
    )

    changed = scenario_config(config, scenario)

    assert changed.pod_a.a_grade_enabled
    assert changed.pod_a.a_grade_size_headroom_cap_enabled
    assert not config.pod_a.a_grade_size_headroom_cap_enabled
    assert any(item.name == "headroom_cap_current" for item in default_scenarios(config))


def test_scenario_and_window_filters_keep_requested_order() -> None:
    config = load_config("config/trident.toml")
    scenarios = selected_scenarios(
        default_scenarios(config),
        "headroom_cap_current,current",
    )
    windows = selected_names(
        "live,baseline",
        available={"baseline", "live"},
        label="window",
    )

    assert [scenario.name for scenario in scenarios] == ["headroom_cap_current", "current"]
    assert windows == ["live", "baseline"]


def test_summarize_result_counts_a_grade_and_quality_sizing() -> None:
    result = FullBotBacktestResult(
        input_path="input.jsonl",
        dedupe_by_timestamp=True,
        records_processed=2,
        duplicate_timestamps_skipped=0,
        first_timestamp="2026-06-10T00:00:00Z",
        last_timestamp="2026-06-10T00:05:00Z",
        dates_covered=["2026-06-10"],
        pod_a={
            "realized_pnl_usd": -1.0,
            "max_drawdown_usd": 2.0,
            "closed_trade_log": [
                {
                    "date": "2026-06-10",
                    "symbol": "BTC",
                    "pnl_usd": 3.0,
                    "setup_details": {
                        "a_grade_active": True,
                        "a_grade_level": "strong",
                        "a_grade_size_scale": 1.4,
                        "a_grade_requested_size_scale": 1.4,
                        "a_grade_size_headroom_cap_active": True,
                    },
                },
                {
                    "date": "2026-06-10",
                    "symbol": "ETH",
                    "pnl_usd": -4.0,
                    "setup_details": {
                        "live_quality_sizing_active": True,
                        "live_quality_sizing_multiplier": 0.5,
                    },
                },
            ],
        },
        pod_b={"closed_trade_log": []},
        pod_c={"realized_pnl_usd": 0.5, "closed_trade_log": []},
        routing={},
        total_realized_pnl_usd=-0.5,
        directional_fees_usd=0.12,
        total_activity_count=2,
        notes=[],
        report_path="report.json",
        summary_path="summary.md",
    )
    row = summarize_result(
        result=result,
        scenario=ScenarioSpec("current", "test", 1.25, 1.4),
        window=WindowSpec("fixture", input_path=Path("input.jsonl"), label="fixture"),
        runtime_seconds=0.1,
    )

    assert row.pod_a_trades == 2
    assert row.a_grade_trades == 1
    assert row.strong_a_grade_trades == 1
    assert row.no_a_grade_trades == 1
    assert row.strong_a_grade_pnl_usd == 3.0
    assert row.no_a_grade_pnl_usd == -4.0
    assert row.avg_a_grade_requested_size_scale == 1.4
    assert row.a_grade_headroom_capped_trades == 1
    assert row.live_quality_scaled_trades == 1
    assert row.avg_live_quality_multiplier == 0.5
    assert row.worst_symbol == "ETH"
    assert row.worst_date == "2026-06-10"


def test_compact_setup_details_exports_p105_fields() -> None:
    compacted = compact_setup_details(
        {
            "a_grade_active": True,
            "a_grade_level": "strong",
            "a_grade_score": 9,
            "a_grade_size_scale": 1.4,
            "a_grade_requested_size_scale": 1.4,
            "a_grade_size_headroom_cap_active": True,
            "a_grade_size_headroom_cap_reasons": "risk_budget_cap",
            "a_grade_reason": "trend+flow",
            "live_quality_sizing_active": True,
            "live_quality_sizing_multiplier": 0.85,
            "live_quality_sizing_reasons": "standard_a_grade",
            "live_quality_original_target_notional_usd": 200,
            "ignored": "not-exported",
        }
    )

    assert compacted["a_grade_active"] is True
    assert compacted["a_grade_level"] == "strong"
    assert compacted["a_grade_size_scale"] == 1.4
    assert compacted["a_grade_requested_size_scale"] == 1.4
    assert compacted["a_grade_size_headroom_cap_active"] is True
    assert compacted["live_quality_sizing_active"] is True
    assert compacted["live_quality_original_target_notional_usd"] == 200
    assert "ignored" not in compacted
