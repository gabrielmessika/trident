import json
from pathlib import Path

from scripts.run_p118_repeated_signal_scale_in_audit import (
    ScaleInScenario,
    add_on_notional_usd,
    build_opportunity_rows,
    load_journal,
    run_scenario,
    summarize_scenario,
)


def test_repeated_signal_scale_in_matches_parent_and_caps_addons(tmp_path: Path) -> None:
    journal = tmp_path / "p117.jsonl"
    _write_jsonl(
        journal,
        [
            _signal("2026-06-22T10:10:00Z", target=300.0),
            _signal("2026-06-22T10:20:00Z", target=500.0),
            _trade("2026-06-22T10:00:00+00:00", "2026-06-22T10:30:00+00:00"),
        ],
    )
    parents, skipped = load_journal(journal)

    opportunities = build_opportunity_rows(skipped, parents)
    rows = run_scenario(
        scenario=ScaleInScenario(
            "first_add50_cap100",
            add_on_fraction=0.5,
            max_add_ons_per_parent=1,
            max_add_on_notional_usd=100.0,
        ),
        skipped=skipped,
        parents=parents,
        taker_fee_bps=3.5,
        dry_run_slippage_bps=0.5,
        dry_run_spread_multiplier=0.5,
    )
    summary = summarize_scenario(
        scenario=ScaleInScenario(
            "first_add50_cap100",
            add_on_fraction=0.5,
            max_add_ons_per_parent=1,
            max_add_on_notional_usd=100.0,
        ),
        rows=rows,
        opportunities=len(skipped),
        matched_opportunities=len([row for row in opportunities if row.parent_trade_id]),
    )

    assert len(parents) == 1
    assert len(skipped) == 2
    assert [row.parent_trade_id for row in opportunities] == ["trade_0001", "trade_0001"]
    assert rows[0].selected is True
    assert rows[0].add_on_notional_usd == 100.0
    assert rows[1].selected is False
    assert rows[1].skip_reason == "max_add_ons_per_parent"
    assert summary.selected_add_ons == 1
    assert summary.parent_trades_touched == 1
    assert summary.pnl_usd > 0.0


def test_add_on_notional_can_run_uncapped() -> None:
    _, skipped = load_journal_from_records([_signal("2026-06-22T10:10:00Z", target=300.0)])

    notional = add_on_notional_usd(
        skipped[0],
        ScaleInScenario(
            "uncapped",
            add_on_fraction=0.25,
            max_add_ons_per_parent=1,
            max_add_on_notional_usd=0.0,
        ),
    )

    assert notional == 75.0


def test_parent_unrealized_filter_skips_weak_parent_mark(tmp_path: Path) -> None:
    journal = tmp_path / "p117.jsonl"
    _write_jsonl(
        journal,
        [
            _signal("2026-06-22T10:10:00Z", target=300.0, price=100.0),
            _signal("2026-06-22T10:20:00Z", target=300.0, price=98.0),
            _trade("2026-06-22T10:00:00+00:00", "2026-06-22T10:30:00+00:00"),
        ],
    )
    parents, skipped = load_journal(journal)

    rows = run_scenario(
        scenario=ScaleInScenario(
            "parent_plus50",
            add_on_fraction=0.25,
            max_add_ons_per_parent=99,
            max_add_on_notional_usd=100.0,
            min_parent_unrealized_return_bps=50.0,
        ),
        skipped=skipped,
        parents=parents,
        taker_fee_bps=3.5,
        dry_run_slippage_bps=0.5,
        dry_run_spread_multiplier=0.5,
    )

    assert rows[0].selected is True
    assert rows[0].parent_unrealized_return_bps and rows[0].parent_unrealized_return_bps > 50.0
    assert rows[1].selected is False
    assert rows[1].skip_reason == "parent_unrealized_below_min"


def load_journal_from_records(records: list[dict[str, object]]):
    path = Path("/tmp/p118_test_journal.jsonl")
    _write_jsonl(path, records)
    try:
        return load_journal(path)
    finally:
        path.unlink(missing_ok=True)


def _signal(timestamp: str, *, target: float, price: float = 100.0) -> dict[str, object]:
    return {
        "event_type": "signal",
        "timestamp": timestamp,
        "signal": {
            "symbol": "ETH",
            "side": "long",
            "setup": "trend_pullback_long",
            "confidence": 0.75,
            "risk": {
                "accepted": True,
                "target_notional_usd": target,
            },
            "execution": {
                "skipped_open": True,
                "skip_reason": "portfolio_open_rejected",
            },
            "setup_details": {
                "microstructure_shadow_bucket": "ok",
                "microstructure_shadow_score": 0.61,
            },
        },
        "symbol_snapshot": {
            "price": price,
            "spread_bps": 2.0,
        },
    }


def _trade(opened_at: str, closed_at: str) -> dict[str, object]:
    return {
        "event_type": "trade_close",
        "trade": {
            "symbol": "ETH",
            "side": "long",
            "entry_price": 99.0,
            "exit_price": 102.0,
            "target_notional_usd": 200.0,
            "pnl_usd": 5.0,
            "close_reason": "trailing_stop",
            "opened_at": opened_at,
            "closed_at": closed_at,
        },
    }


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
