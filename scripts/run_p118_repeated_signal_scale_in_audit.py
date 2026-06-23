#!/usr/bin/env python3
"""P1-18 / A-PNL-07b repeated-signal scale-in audit for Pod A.

Research-only. It uses the compact P117 journal, finds accepted signals that
were skipped because a same-symbol position was already open, then simulates
small add-ons closed with the parent trade. No live config or execution path is
changed.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_p105_a_grade_replay import parse_timestamp, utc_stamp


DEFAULT_P117_JOURNAL = (
    "server-data/replay_reports/p117_fill_quality_audit_20260623/"
    "pod_a_fill_quality_journal.jsonl"
)


@dataclass(frozen=True, slots=True)
class ScaleInScenario:
    name: str
    add_on_fraction: float
    max_add_ons_per_parent: int
    max_add_on_notional_usd: float
    min_parent_unrealized_return_bps: float | None = None


@dataclass(slots=True)
class ParentTrade:
    trade_id: str
    symbol: str
    side: str
    setup: str
    opened_at: datetime
    closed_at: datetime
    entry_price: float
    exit_price: float
    target_notional_usd: float
    pnl_usd: float
    close_reason: str


@dataclass(slots=True)
class SkippedSignal:
    signal_id: str
    timestamp: datetime
    symbol: str
    side: str
    setup: str
    reason: str
    target_notional_usd: float
    mid_price: float
    spread_bps: float
    confidence: float
    future_return_15m_bps: float | None
    microstructure_bucket: str
    microstructure_score: float | None


@dataclass(slots=True)
class OpportunityRow:
    signal_id: str
    timestamp: str
    symbol: str
    side: str
    setup: str
    reason: str
    parent_trade_id: str | None
    parent_opened_at: str | None
    parent_closed_at: str | None
    parent_close_reason: str | None
    parent_pnl_usd: float | None
    parent_unrealized_return_bps: float | None
    target_notional_usd: float
    mid_price: float
    spread_bps: float
    confidence: float
    future_return_15m_bps: float | None
    microstructure_bucket: str
    microstructure_score: float | None


@dataclass(slots=True)
class ScenarioSignalRow:
    scenario: str
    signal_id: str
    selected: bool
    skip_reason: str
    timestamp: str
    symbol: str
    parent_trade_id: str | None
    parent_close_reason: str | None
    parent_pnl_usd: float | None
    parent_unrealized_return_bps: float | None
    add_on_notional_usd: float
    entry_price: float | None
    exit_price: float | None
    gross_pnl_usd: float
    fees_usd: float
    pnl_usd: float
    return_bps: float | None


@dataclass(slots=True)
class ScenarioSummaryRow:
    scenario: str
    add_on_fraction: float
    max_add_ons_per_parent: int
    max_add_on_notional_usd: float
    min_parent_unrealized_return_bps: float | None
    opportunities: int
    matched_opportunities: int
    selected_add_ons: int
    parent_trades_touched: int
    total_add_on_notional_usd: float
    gross_pnl_usd: float
    fees_usd: float
    pnl_usd: float
    win_rate: float | None
    profit_factor: float | None
    avg_return_bps: float | None
    avg_parent_unrealized_return_bps: float | None
    avg_add_on_notional_usd: float | None
    worst_symbol: str | None
    worst_symbol_pnl_usd: float | None
    best_symbol: str | None
    best_symbol_pnl_usd: float | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--p117-journal", default=DEFAULT_P117_JOURNAL)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--taker-fee-bps", type=float, default=3.5)
    parser.add_argument("--dry-run-slippage-bps", type=float, default=0.5)
    parser.add_argument("--dry-run-spread-multiplier", type=float, default=0.5)
    parser.add_argument(
        "--max-add-on-notional-usd",
        type=float,
        default=200.0,
        help="Cap each hypothetical add-on. Set <=0 for no cap.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = Path(
        args.output_dir
        or f"server-data/replay_reports/p118_repeated_signal_scale_in_{utc_stamp()}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    journal_path = Path(args.p117_journal)
    parents, skipped = load_journal(journal_path)
    opportunities = build_opportunity_rows(skipped, parents)
    scenarios = default_scenarios(max_add_on_notional_usd=float(args.max_add_on_notional_usd))
    signal_rows: list[ScenarioSignalRow] = []
    summaries: list[ScenarioSummaryRow] = []
    for scenario in scenarios:
        rows = run_scenario(
            scenario=scenario,
            skipped=skipped,
            parents=parents,
            taker_fee_bps=float(args.taker_fee_bps),
            dry_run_slippage_bps=float(args.dry_run_slippage_bps),
            dry_run_spread_multiplier=float(args.dry_run_spread_multiplier),
        )
        signal_rows.extend(rows)
        summaries.append(
            summarize_scenario(
                scenario=scenario,
                rows=rows,
                opportunities=len(skipped),
                matched_opportunities=len([row for row in opportunities if row.parent_trade_id]),
            )
        )

    payload = {
        "generated_at": utc_stamp(),
        "p117_journal": str(journal_path),
        "decision": "research_only_no_live_change",
        "inputs": {
            "parent_trades": len(parents),
            "accepted_skipped_signals": len(skipped),
            "matched_opportunities": len([row for row in opportunities if row.parent_trade_id]),
            "taker_fee_bps": float(args.taker_fee_bps),
            "dry_run_slippage_bps": float(args.dry_run_slippage_bps),
            "dry_run_spread_multiplier": float(args.dry_run_spread_multiplier),
        },
        "scenario_summary": [asdict(row) for row in summaries],
    }
    write_csv(output_dir / "scale_in_opportunities.csv", opportunities)
    write_csv(output_dir / "scale_in_signal_results.csv", signal_rows)
    write_csv(output_dir / "scenario_summary.csv", summaries)
    write_json(output_dir / "p118_repeated_signal_scale_in_audit.json", payload)
    write_markdown(output_dir / "p118_repeated_signal_scale_in_audit.md", payload, summaries)
    print(output_dir)


def default_scenarios(*, max_add_on_notional_usd: float) -> list[ScaleInScenario]:
    return [
        ScaleInScenario(
            name="first_add25_cap",
            add_on_fraction=0.25,
            max_add_ons_per_parent=1,
            max_add_on_notional_usd=max_add_on_notional_usd,
        ),
        ScaleInScenario(
            name="first_add50_cap",
            add_on_fraction=0.50,
            max_add_ons_per_parent=1,
            max_add_on_notional_usd=max_add_on_notional_usd,
        ),
        ScaleInScenario(
            name="all_add25_cap",
            add_on_fraction=0.25,
            max_add_ons_per_parent=99,
            max_add_on_notional_usd=max_add_on_notional_usd,
        ),
        ScaleInScenario(
            name="all_add25_parent_green_cap",
            add_on_fraction=0.25,
            max_add_ons_per_parent=99,
            max_add_on_notional_usd=max_add_on_notional_usd,
            min_parent_unrealized_return_bps=0.0,
        ),
        ScaleInScenario(
            name="all_add25_parent_plus25_cap",
            add_on_fraction=0.25,
            max_add_ons_per_parent=99,
            max_add_on_notional_usd=max_add_on_notional_usd,
            min_parent_unrealized_return_bps=25.0,
        ),
        ScaleInScenario(
            name="all_add25_parent_plus50_cap",
            add_on_fraction=0.25,
            max_add_ons_per_parent=99,
            max_add_on_notional_usd=max_add_on_notional_usd,
            min_parent_unrealized_return_bps=50.0,
        ),
    ]


def load_journal(path: Path) -> tuple[list[ParentTrade], list[SkippedSignal]]:
    parents: list[ParentTrade] = []
    skipped: list[SkippedSignal] = []
    signal_index = 0
    trade_index = 0
    for record in jsonl_records(path):
        event_type = record.get("event_type")
        if event_type == "signal":
            signal = record.get("signal")
            if not isinstance(signal, dict):
                continue
            execution = signal.get("execution") or {}
            risk = signal.get("risk") or {}
            snapshot = record.get("symbol_snapshot") or {}
            if not isinstance(execution, dict) or not isinstance(risk, dict) or not isinstance(snapshot, dict):
                continue
            if not bool(risk.get("accepted")) or not bool(execution.get("skipped_open")):
                continue
            if str(execution.get("skip_reason") or "") != "portfolio_open_rejected":
                continue
            timestamp = parse_timestamp(str(record.get("timestamp") or ""))
            if timestamp is None:
                continue
            signal_index += 1
            details = signal.get("setup_details") or {}
            skipped.append(
                SkippedSignal(
                    signal_id=f"skip_{signal_index:04d}",
                    timestamp=timestamp,
                    symbol=str(signal.get("symbol") or "").upper(),
                    side=str(signal.get("side") or ""),
                    setup=str(signal.get("setup") or ""),
                    reason=str(execution.get("skip_reason") or ""),
                    target_notional_usd=float(risk.get("target_notional_usd") or 0.0),
                    mid_price=float(snapshot.get("price") or 0.0),
                    spread_bps=max(float(snapshot.get("spread_bps") or 0.0), 0.0),
                    confidence=float(signal.get("confidence") or 0.0),
                    future_return_15m_bps=None,
                    microstructure_bucket=str(
                        details.get("microstructure_shadow_bucket") or "missing"
                    ),
                    microstructure_score=optional_float(
                        details.get("microstructure_shadow_score")
                    ),
                )
            )
        elif event_type == "trade_close":
            trade = record.get("trade")
            if not isinstance(trade, dict):
                continue
            opened_at = parse_timestamp(str(trade.get("opened_at") or ""))
            closed_at = parse_timestamp(str(trade.get("closed_at") or ""))
            if opened_at is None or closed_at is None:
                continue
            trade_index += 1
            parents.append(
                ParentTrade(
                    trade_id=f"trade_{trade_index:04d}",
                    symbol=str(trade.get("symbol") or "").upper(),
                    side=str(trade.get("side") or ""),
                    setup=str(trade.get("setup") or ""),
                    opened_at=opened_at,
                    closed_at=closed_at,
                    entry_price=float(trade.get("entry_price") or 0.0),
                    exit_price=float(trade.get("exit_price") or 0.0),
                    target_notional_usd=float(trade.get("target_notional_usd") or 0.0),
                    pnl_usd=float(trade.get("pnl_usd") or 0.0),
                    close_reason=str(trade.get("close_reason") or ""),
                )
            )
    return parents, skipped


def build_opportunity_rows(
    skipped: list[SkippedSignal],
    parents: list[ParentTrade],
) -> list[OpportunityRow]:
    return [
        opportunity_row(signal, match_parent(signal, parents))
        for signal in skipped
    ]


def opportunity_row(
    signal: SkippedSignal,
    parent: ParentTrade | None,
) -> OpportunityRow:
    return OpportunityRow(
        signal_id=signal.signal_id,
        timestamp=isoformat(signal.timestamp),
        symbol=signal.symbol,
        side=signal.side,
        setup=signal.setup,
        reason=signal.reason,
        parent_trade_id=parent.trade_id if parent else None,
        parent_opened_at=isoformat(parent.opened_at) if parent else None,
        parent_closed_at=isoformat(parent.closed_at) if parent else None,
        parent_close_reason=parent.close_reason if parent else None,
        parent_pnl_usd=round(parent.pnl_usd, 6) if parent else None,
        parent_unrealized_return_bps=round(parent_unrealized_return_bps(signal, parent), 6)
        if parent
        else None,
        target_notional_usd=round(signal.target_notional_usd, 6),
        mid_price=round(signal.mid_price, 8),
        spread_bps=round(signal.spread_bps, 6),
        confidence=round(signal.confidence, 6),
        future_return_15m_bps=signal.future_return_15m_bps,
        microstructure_bucket=signal.microstructure_bucket,
        microstructure_score=signal.microstructure_score,
    )


def run_scenario(
    *,
    scenario: ScaleInScenario,
    skipped: list[SkippedSignal],
    parents: list[ParentTrade],
    taker_fee_bps: float,
    dry_run_slippage_bps: float,
    dry_run_spread_multiplier: float,
) -> list[ScenarioSignalRow]:
    selected_by_parent: dict[str, int] = defaultdict(int)
    rows: list[ScenarioSignalRow] = []
    for signal in skipped:
        parent = match_parent(signal, parents)
        if parent is None:
            rows.append(scenario_row(scenario.name, signal, None, selected=False, skip_reason="missing_parent"))
            continue
        parent_mark_return = parent_unrealized_return_bps(signal, parent)
        if (
            scenario.min_parent_unrealized_return_bps is not None
            and parent_mark_return < scenario.min_parent_unrealized_return_bps
        ):
            rows.append(
                scenario_row(
                    scenario.name,
                    signal,
                    parent,
                    selected=False,
                    skip_reason="parent_unrealized_below_min",
                )
            )
            continue
        parent_count = selected_by_parent[parent.trade_id]
        if parent_count >= scenario.max_add_ons_per_parent:
            rows.append(scenario_row(scenario.name, signal, parent, selected=False, skip_reason="max_add_ons_per_parent"))
            continue
        add_on_notional = add_on_notional_usd(signal, scenario)
        if add_on_notional <= 0.0:
            rows.append(scenario_row(scenario.name, signal, parent, selected=False, skip_reason="zero_add_on_notional"))
            continue
        selected_by_parent[parent.trade_id] += 1
        rows.append(
            scenario_row(
                scenario.name,
                signal,
                parent,
                selected=True,
                skip_reason="selected",
                add_on_notional_usd=add_on_notional,
                taker_fee_bps=taker_fee_bps,
                dry_run_slippage_bps=dry_run_slippage_bps,
                dry_run_spread_multiplier=dry_run_spread_multiplier,
            )
        )
    return rows


def match_parent(signal: SkippedSignal, parents: list[ParentTrade]) -> ParentTrade | None:
    matches = [
        parent
        for parent in parents
        if parent.symbol == signal.symbol
        and parent.side == signal.side
        and parent.opened_at < signal.timestamp < parent.closed_at
    ]
    if not matches:
        return None
    return min(matches, key=lambda parent: (signal.timestamp - parent.opened_at).total_seconds())


def add_on_notional_usd(signal: SkippedSignal, scenario: ScaleInScenario) -> float:
    notional = max(signal.target_notional_usd, 0.0) * max(scenario.add_on_fraction, 0.0)
    if scenario.max_add_on_notional_usd > 0:
        notional = min(notional, scenario.max_add_on_notional_usd)
    return round(notional, 6)


def parent_unrealized_return_bps(signal: SkippedSignal, parent: ParentTrade) -> float:
    return directional_return_bps(signal.side, parent.entry_price, signal.mid_price)


def scenario_row(
    scenario_name: str,
    signal: SkippedSignal,
    parent: ParentTrade | None,
    *,
    selected: bool,
    skip_reason: str,
    add_on_notional_usd: float = 0.0,
    taker_fee_bps: float = 3.5,
    dry_run_slippage_bps: float = 0.5,
    dry_run_spread_multiplier: float = 0.5,
) -> ScenarioSignalRow:
    entry_price = None
    exit_price = None
    gross_pnl = 0.0
    fees = 0.0
    pnl = 0.0
    ret_bps = None
    parent_mark_return = parent_unrealized_return_bps(signal, parent) if parent is not None else None
    if selected and parent is not None and add_on_notional_usd > 0.0:
        entry_price = entry_fill_price(
            side=signal.side,
            mid_price=signal.mid_price,
            spread_bps=signal.spread_bps,
            dry_run_slippage_bps=dry_run_slippage_bps,
            dry_run_spread_multiplier=dry_run_spread_multiplier,
        )
        exit_price = parent.exit_price
        ret_bps = directional_return_bps(signal.side, entry_price, exit_price)
        gross_pnl = round(add_on_notional_usd * ret_bps / 10_000.0, 6)
        fees = round(add_on_notional_usd * taker_fee_bps * 2.0 / 10_000.0, 6)
        pnl = round(gross_pnl - fees, 6)
    return ScenarioSignalRow(
        scenario=scenario_name,
        signal_id=signal.signal_id,
        selected=selected,
        skip_reason=skip_reason,
        timestamp=isoformat(signal.timestamp),
        symbol=signal.symbol,
        parent_trade_id=parent.trade_id if parent else None,
        parent_close_reason=parent.close_reason if parent else None,
        parent_pnl_usd=round(parent.pnl_usd, 6) if parent else None,
        parent_unrealized_return_bps=round(parent_mark_return, 6)
        if parent_mark_return is not None
        else None,
        add_on_notional_usd=round(add_on_notional_usd, 6),
        entry_price=round(entry_price, 8) if entry_price is not None else None,
        exit_price=round(exit_price, 8) if exit_price is not None else None,
        gross_pnl_usd=gross_pnl,
        fees_usd=fees,
        pnl_usd=pnl,
        return_bps=round(ret_bps, 6) if ret_bps is not None else None,
    )


def summarize_scenario(
    *,
    scenario: ScaleInScenario,
    rows: list[ScenarioSignalRow],
    opportunities: int,
    matched_opportunities: int,
) -> ScenarioSummaryRow:
    selected = [row for row in rows if row.selected]
    pnls = [row.pnl_usd for row in selected]
    returns = [row.return_bps for row in selected if row.return_bps is not None]
    parent_returns = [
        row.parent_unrealized_return_bps
        for row in selected
        if row.parent_unrealized_return_bps is not None
    ]
    by_symbol: dict[str, float] = defaultdict(float)
    for row in selected:
        by_symbol[row.symbol] += row.pnl_usd
    worst_symbol, worst_pnl = extreme_symbol(by_symbol, reverse=False)
    best_symbol, best_pnl = extreme_symbol(by_symbol, reverse=True)
    return ScenarioSummaryRow(
        scenario=scenario.name,
        add_on_fraction=round(scenario.add_on_fraction, 6),
        max_add_ons_per_parent=scenario.max_add_ons_per_parent,
        max_add_on_notional_usd=round(scenario.max_add_on_notional_usd, 6),
        min_parent_unrealized_return_bps=scenario.min_parent_unrealized_return_bps,
        opportunities=opportunities,
        matched_opportunities=matched_opportunities,
        selected_add_ons=len(selected),
        parent_trades_touched=len({row.parent_trade_id for row in selected if row.parent_trade_id}),
        total_add_on_notional_usd=round(sum(row.add_on_notional_usd for row in selected), 6),
        gross_pnl_usd=round(sum(row.gross_pnl_usd for row in selected), 6),
        fees_usd=round(sum(row.fees_usd for row in selected), 6),
        pnl_usd=round(sum(pnls), 6),
        win_rate=round(len([pnl for pnl in pnls if pnl > 0.0]) / len(pnls), 6)
        if pnls
        else None,
        profit_factor=profit_factor(pnls),
        avg_return_bps=round(sum(returns) / len(returns), 6) if returns else None,
        avg_parent_unrealized_return_bps=round(sum(parent_returns) / len(parent_returns), 6)
        if parent_returns
        else None,
        avg_add_on_notional_usd=round(sum(row.add_on_notional_usd for row in selected) / len(selected), 6)
        if selected
        else None,
        worst_symbol=worst_symbol,
        worst_symbol_pnl_usd=round(worst_pnl, 6) if worst_pnl is not None else None,
        best_symbol=best_symbol,
        best_symbol_pnl_usd=round(best_pnl, 6) if best_pnl is not None else None,
    )


def entry_fill_price(
    *,
    side: str,
    mid_price: float,
    spread_bps: float,
    dry_run_slippage_bps: float,
    dry_run_spread_multiplier: float,
) -> float:
    impact_bps = max(spread_bps, 0.0) * max(dry_run_spread_multiplier, 0.0) + max(
        dry_run_slippage_bps,
        0.0,
    )
    signed = impact_bps if side == "long" else -impact_bps
    return mid_price * (1.0 + signed / 10_000.0)


def directional_return_bps(side: str, entry_price: float, exit_price: float) -> float:
    if entry_price <= 0.0:
        return 0.0
    if side == "long":
        return (exit_price - entry_price) / entry_price * 10_000.0
    return (entry_price - exit_price) / entry_price * 10_000.0


def profit_factor(pnls: list[float]) -> float | None:
    gains = sum(pnl for pnl in pnls if pnl > 0.0)
    losses = -sum(pnl for pnl in pnls if pnl < 0.0)
    if losses <= 0.0:
        return None
    return round(gains / losses, 6)


def extreme_symbol(by_symbol: dict[str, float], *, reverse: bool) -> tuple[str | None, float | None]:
    if not by_symbol:
        return None, None
    symbol, value = sorted(by_symbol.items(), key=lambda item: item[1], reverse=reverse)[0]
    return symbol, value


def optional_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def jsonl_records(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def write_csv(path: Path, rows: list[Any]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(rows[0]).keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def write_markdown(
    path: Path,
    payload: dict[str, Any],
    summaries: list[ScenarioSummaryRow],
) -> None:
    lines = [
        "# P118 repeated-signal scale-in audit Pod A",
        "",
        f"- generated_at: `{payload.get('generated_at')}`",
        "- decision: `research_only_no_live_change`",
        f"- p117_journal: `{payload.get('p117_journal')}`",
        f"- inputs: `{payload.get('inputs')}`",
        "",
        "## Scenario Summary",
        "",
        "| Scenario | Selected | Parents | Add-on notional | PnL | PF | WR | Avg ret bps | Avg parent bps | Worst | Best |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for row in summaries:
        lines.append(
            "| "
            f"`{row.scenario}` | {row.selected_add_ons} | {row.parent_trades_touched} | "
            f"{row.total_add_on_notional_usd:.2f} | {row.pnl_usd:.2f} | "
            f"{fmt(row.profit_factor)} | {fmt(row.win_rate)} | {fmt(row.avg_return_bps)} | "
            f"{fmt(row.avg_parent_unrealized_return_bps)} | "
            f"`{row.worst_symbol}` {fmt(row.worst_symbol_pnl_usd)} | "
            f"`{row.best_symbol}` {fmt(row.best_symbol_pnl_usd)} |"
        )
    lines.extend(
        [
            "",
            "## Lecture",
            "",
            "- Ce rapport ne change pas la logique live et ne propose pas d'activation automatique.",
            "- Un add-on positif ici mesure seulement un delta contrefactuel ferme avec le trade parent.",
            "- Toute promotion demanderait un replay full-bot reel avec caps, risk budget, drawdown et max exposure.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


if __name__ == "__main__":
    main()
