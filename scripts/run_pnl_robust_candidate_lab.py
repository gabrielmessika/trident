#!/usr/bin/env python3
"""Robust PnL candidate lab for TRIDENT A/C.

Research-only. This script aggregates existing audit artefacts into a common
candidate scoreboard so that promising ideas are judged across windows, coverage
quality, winner loss, and symbol concentration before any live promotion.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_p105_a_grade_replay import parse_timestamp, utc_stamp


DEFAULT_P117_JOURNAL = (
    "server-data/replay_reports/p117_fill_quality_audit_20260623/"
    "pod_a_fill_quality_journal.jsonl"
)
DEFAULT_P103_REPORT = (
    "server-data/replay_reports/p103_pod_c_external_reference_cap50_20260623/"
    "p103_pod_c_external_reference_validation.json"
)
DEFAULT_P120_REPORT = (
    "server-data/replay_reports/p120_oil_relative_value_20260623/"
    "p120_oil_relative_value_audit.json"
)
DEFAULT_P118_REPORT = (
    "server-data/replay_reports/p118_repeated_signal_scale_in_20260623/"
    "p118_repeated_signal_scale_in_audit.json"
)
DEFAULT_P119_REPORT = (
    "server-data/replay_reports/p119_loss_probation_cap_v2_20260623/"
    "p119_loss_probation_cap_audit.json"
)
DEFAULT_P121_REPORT = (
    "server-data/replay_reports/p121_pod_c_session_liquidity_20260623/"
    "p121_pod_c_session_liquidity_audit.json"
)
DEFAULT_P122_REPORT = (
    "server-data/replay_reports/p122_pod_c_execution_cost_20260623/"
    "p122_pod_c_execution_cost_audit.json"
)


@dataclass(slots=True)
class CandidatePeriod:
    candidate: str
    pod: str
    family: str
    source: str
    period: str
    base_pnl_usd: float
    adjusted_pnl_usd: float
    delta_usd: float
    trades: int
    touched_trades: int
    touched_pnl_usd: float
    touched_winners: int
    touched_losers: int
    capped_winner_pnl_usd: float
    capped_loser_pnl_usd: float
    base_profit_factor: float | None
    adjusted_profit_factor: float | None
    win_rate: float | None
    coverage_pct: float | None
    sufficient_coverage: bool
    top_symbol: str
    top_symbol_count: int
    max_symbol_concentration_pct: float
    symbols: dict[str, int]
    notes: str = ""


@dataclass(slots=True)
class CandidateVerdict:
    candidate: str
    pod: str
    family: str
    classification: str
    total_delta_usd: float
    evaluation_periods: int
    positive_periods: int
    negative_periods: int
    insufficient_coverage_periods: int
    max_symbol_concentration_pct: float
    touched_winner_pnl_usd: float
    touched_loser_pnl_usd: float
    reasons: str


@dataclass(slots=True)
class PodATradeDecision:
    trade_id: str
    opened_at: str
    closed_at: str
    period: str
    symbol: str
    side: str
    close_reason: str
    original_pnl_usd: float
    multiplier: float
    adjusted_pnl_usd: float
    pnl_delta_usd: float
    risk_points: int
    risk_reasons: str
    microstructure_score: float | None
    microstructure_bucket: str
    spread_bps: float | None
    bucket_notional_usd: float | None
    bucket_trade_count: float | None
    dynamic_symbol_guard_state: str
    pattern_watch_count: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--p117-journal", default=DEFAULT_P117_JOURNAL)
    parser.add_argument(
        "--p103-report",
        action="append",
        dest="p103_reports",
        help="P103 external-reference validation report. Can be repeated.",
    )
    parser.add_argument("--p118-report", default=DEFAULT_P118_REPORT)
    parser.add_argument("--p119-report", default=DEFAULT_P119_REPORT)
    parser.add_argument("--p120-report", default=DEFAULT_P120_REPORT)
    parser.add_argument("--p121-report", default=DEFAULT_P121_REPORT)
    parser.add_argument("--p122-report", default=DEFAULT_P122_REPORT)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--split-date", default="2026-06-03")
    parser.add_argument("--min-coverage-pct", type=float, default=80.0)
    parser.add_argument("--max-symbol-concentration-pct", type=float, default=70.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir or f"server-data/replay_reports/pnl_robust_candidate_lab_{utc_stamp()}")
    output_dir.mkdir(parents=True, exist_ok=True)

    periods: list[CandidatePeriod] = []
    pod_a_decisions: list[PodATradeDecision] = []
    p103_reports = list(args.p103_reports or [DEFAULT_P103_REPORT])
    p117_path = Path(args.p117_journal)
    if p117_path.exists():
        pod_a_decisions = build_pod_a_combined_decisions(p117_path, split_date=str(args.split_date))
        periods.extend(summarize_pod_a_combined(pod_a_decisions))

    for path, loader in [
        *[(Path(report), load_p103_candidates) for report in p103_reports],
        (Path(args.p118_report), load_p118_candidates),
        (Path(args.p119_report), load_p119_candidates),
        (Path(args.p120_report), load_p120_candidates),
        (Path(args.p121_report), load_policy_outcome_candidates),
        (Path(args.p122_report), load_policy_outcome_candidates),
    ]:
        if path.exists():
            periods.extend(loader(path, min_coverage_pct=float(args.min_coverage_pct)))

    verdicts = build_verdicts(
        periods,
        max_symbol_concentration_pct=float(args.max_symbol_concentration_pct),
    )
    payload = {
        "generated_at": utc_stamp(),
        "decision": "research_only_no_live_change",
        "inputs": {
            "p117_journal": str(args.p117_journal),
            "p103_reports": p103_reports,
            "p118_report": str(args.p118_report),
            "p119_report": str(args.p119_report),
            "p120_report": str(args.p120_report),
            "p121_report": str(args.p121_report),
            "p122_report": str(args.p122_report),
        },
        "parameters": {
            "split_date": str(args.split_date),
            "min_coverage_pct": float(args.min_coverage_pct),
            "max_symbol_concentration_pct": float(args.max_symbol_concentration_pct),
        },
        "candidate_count": len(verdicts),
        "period_count": len(periods),
        "pod_a_decision_count": len(pod_a_decisions),
        "candidate_summary": [asdict(row) for row in verdicts],
        "period_summary": [asdict(row) for row in periods],
    }
    write_csv(output_dir / "candidate_summary.csv", verdicts)
    write_csv(output_dir / "period_summary.csv", periods)
    write_csv(output_dir / "pod_a_decision_journal.csv", pod_a_decisions)
    write_json(output_dir / "pnl_robust_candidate_lab.json", payload)
    write_markdown(output_dir / "pnl_robust_candidate_lab.md", payload, verdicts, periods)
    print(output_dir)


def build_pod_a_combined_decisions(path: Path, *, split_date: str) -> list[PodATradeDecision]:
    decisions: list[PodATradeDecision] = []
    trade_index = 0
    for record in jsonl_records(path):
        if record.get("event_type") != "trade_close":
            continue
        raw = record.get("trade")
        if not isinstance(raw, dict):
            continue
        closed_at = str(raw.get("closed_at") or "")
        if parse_timestamp(closed_at) is None:
            continue
        trade_index += 1
        details = raw.get("setup_details") if isinstance(raw.get("setup_details"), dict) else {}
        pnl = to_float(raw.get("pnl_usd"))
        risk_points, reasons = pod_a_combined_risk(details, side=str(raw.get("side") or "").lower())
        multiplier = pod_a_multiplier(risk_points)
        adjusted = pnl * multiplier
        period = "pre_split" if closed_at[:10] < split_date else "post_split"
        decisions.append(
            PodATradeDecision(
                trade_id=str(raw.get("trade_id") or f"pod_a_{trade_index:04d}"),
                opened_at=str(raw.get("opened_at") or ""),
                closed_at=closed_at,
                period=period,
                symbol=str(raw.get("symbol") or "").upper(),
                side=str(raw.get("side") or "").lower(),
                close_reason=str(raw.get("close_reason") or ""),
                original_pnl_usd=round(pnl, 6),
                multiplier=round(multiplier, 6),
                adjusted_pnl_usd=round(adjusted, 6),
                pnl_delta_usd=round(adjusted - pnl, 6),
                risk_points=risk_points,
                risk_reasons=",".join(reasons),
                microstructure_score=optional_float(details.get("microstructure_shadow_score")),
                microstructure_bucket=str(details.get("microstructure_shadow_bucket") or ""),
                spread_bps=optional_float(details.get("spread_bps")),
                bucket_notional_usd=optional_float(details.get("bucket_notional_usd")),
                bucket_trade_count=optional_float(details.get("bucket_trade_count")),
                dynamic_symbol_guard_state=str(details.get("symbol_guard_state") or ""),
                pattern_watch_count=int(optional_float(details.get("pattern_watch_count")) or 0),
            )
        )
    return decisions


def pod_a_combined_risk(details: dict[str, Any], *, side: str) -> tuple[int, list[str]]:
    reasons: list[str] = []
    guard_state = str(details.get("symbol_guard_state") or "").lower()
    if guard_state == "quarantine":
        reasons.append("symbol_guard_quarantine")
    elif guard_state == "throttle":
        reasons.append("symbol_guard_throttle")

    micro_bucket = str(details.get("microstructure_shadow_bucket") or "").lower()
    micro_score = optional_float(details.get("microstructure_shadow_score"))
    if micro_bucket in {"poor", "weak"} or (micro_score is not None and micro_score < 0.56):
        reasons.append("weak_microstructure")

    spread = optional_float(details.get("spread_bps"))
    if spread is not None and spread >= 4.0:
        reasons.append("wide_spread")

    notional = optional_float(details.get("bucket_notional_usd"))
    trade_count = optional_float(details.get("bucket_trade_count"))
    if notional is not None and notional < 1_000.0:
        reasons.append("thin_notional")
    if trade_count is not None and trade_count < 10.0:
        reasons.append("thin_trade_count")

    watch_count = int(optional_float(details.get("pattern_watch_count")) or 0)
    if watch_count >= 2:
        reasons.append("many_pattern_watches")

    flow = optional_float(details.get("trade_flow_bias"))
    if side == "long" and flow is not None and flow < 0.20:
        reasons.append("counter_flow")
    if side == "short" and flow is not None and flow > 0.80:
        reasons.append("counter_flow")

    return len(reasons), reasons


def pod_a_multiplier(risk_points: int) -> float:
    if risk_points <= 0:
        return 1.0
    if risk_points == 1:
        return 0.75
    if risk_points == 2:
        return 0.50
    return 0.35


def summarize_pod_a_combined(decisions: list[PodATradeDecision]) -> list[CandidatePeriod]:
    rows: list[CandidatePeriod] = []
    for period, scoped in [
        ("all", decisions),
        ("pre_split", [row for row in decisions if row.period == "pre_split"]),
        ("post_split", [row for row in decisions if row.period == "post_split"]),
    ]:
        rows.append(
            summarize_adjusted_rows(
                candidate="pod_a_combined_sizing_v0",
                pod="pod_a",
                family="combined_sizing",
                source="p117_trade_level",
                period=period,
                rows=[
                    {
                        "symbol": row.symbol,
                        "original_pnl_usd": row.original_pnl_usd,
                        "adjusted_pnl_usd": row.adjusted_pnl_usd,
                        "touched": row.multiplier < 0.999999,
                    }
                    for row in scoped
                ],
                coverage_pct=100.0,
                sufficient_coverage=True,
            )
        )
    return rows


def load_p103_candidates(path: Path, *, min_coverage_pct: float) -> list[CandidatePeriod]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    coverage_by_window = {
        str(row.get("window")): optional_float(row.get("reference_coverage_pct"))
        for row in payload.get("window_summaries", [])
        if isinstance(row, dict)
    }
    periods: list[CandidatePeriod] = []
    for row in payload.get("gate_outcomes", []):
        if not isinstance(row, dict):
            continue
        gate = str(row.get("gate") or "")
        action = str(row.get("action") or "")
        if action != "cap50" or not gate.startswith("cap50_"):
            continue
        window = str(row.get("window") or "")
        coverage = coverage_by_window.get(window)
        periods.append(
            period_from_policy_outcome(
                row,
                candidate=f"pod_c_external_reference::{gate}",
                pod="pod_c",
                family="external_reference",
                source=str(path),
                period=window,
                coverage_pct=coverage,
                sufficient_coverage=coverage is not None and coverage >= min_coverage_pct,
            )
        )
    return periods


def load_p118_candidates(path: Path, *, min_coverage_pct: float) -> list[CandidatePeriod]:
    del min_coverage_pct
    payload = json.loads(path.read_text(encoding="utf-8"))
    periods: list[CandidatePeriod] = []
    for row in payload.get("scenario_summary", []):
        if not isinstance(row, dict):
            continue
        scenario = str(row.get("scenario") or "")
        selected = int(row.get("selected_add_ons") or 0)
        parent_trades = int(row.get("parent_trades_touched") or 0)
        pnl = to_float(row.get("pnl_usd"))
        symbols = {
            str(row.get("best_symbol") or "best_symbol"): 1,
            str(row.get("worst_symbol") or "worst_symbol"): 1,
        }
        top_symbol, top_count, concentration = symbol_concentration(symbols, max(len(symbols), 1))
        periods.append(
            CandidatePeriod(
                candidate=f"pod_a_scale_in::{scenario}",
                pod="pod_a",
                family="scale_in",
                source=str(path),
                period="live_observed",
                base_pnl_usd=0.0,
                adjusted_pnl_usd=round(pnl, 6),
                delta_usd=round(pnl, 6),
                trades=selected,
                touched_trades=selected,
                touched_pnl_usd=round(pnl, 6),
                touched_winners=0,
                touched_losers=0,
                capped_winner_pnl_usd=0.0,
                capped_loser_pnl_usd=0.0,
                base_profit_factor=None,
                adjusted_profit_factor=optional_float(row.get("profit_factor")),
                win_rate=optional_float(row.get("win_rate")),
                coverage_pct=100.0,
                sufficient_coverage=True,
                top_symbol=top_symbol,
                top_symbol_count=top_count,
                max_symbol_concentration_pct=round(concentration, 6),
                symbols=symbols,
                notes=f"proxy_add_on_not_full_bot_delta;parents={parent_trades}",
            )
        )
    return periods


def load_p119_candidates(path: Path, *, min_coverage_pct: float) -> list[CandidatePeriod]:
    del min_coverage_pct
    payload = json.loads(path.read_text(encoding="utf-8"))
    params = payload.get("parameters") if isinstance(payload.get("parameters"), dict) else {}
    candidate = (
        "pod_a_loss_probation::"
        f"cap{int(to_float(params.get('cap_multiplier')) * 100)}"
        f"_lb{int(to_float(params.get('rolling_lookback')))}"
        f"_min{int(to_float(params.get('min_closed_trades')))}"
        f"_pnl{to_float(params.get('max_rolling_pnl_usd')):.0f}"
        f"_pf{str(params.get('max_profit_factor')).replace('.', 'p')}"
    )
    trade_path = path.with_name("trade_adjustments.csv")
    if not trade_path.exists():
        return []
    rows = []
    with trade_path.open("r", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            rows.append(
                {
                    "closed_at": str(raw.get("closed_at") or ""),
                    "symbol": str(raw.get("symbol") or "").upper(),
                    "original_pnl_usd": to_float(raw.get("original_pnl_usd")),
                    "adjusted_pnl_usd": to_float(raw.get("adjusted_pnl_usd")),
                    "touched": to_float(raw.get("cap_multiplier")) < 0.999999,
                }
            )
    split_date = str(params.get("split_date") or "2026-06-03")
    return [
        summarize_adjusted_rows(
            candidate=candidate,
            pod="pod_a",
            family="loss_probation",
            source=str(path),
            period=period,
            rows=scoped,
            coverage_pct=100.0,
            sufficient_coverage=True,
        )
        for period, scoped in [
            ("all", rows),
            ("pre_split", [row for row in rows if str(row.get("closed_at") or "")[:10] < split_date]),
            ("post_split", [row for row in rows if str(row.get("closed_at") or "")[:10] >= split_date]),
        ]
    ]


def load_policy_outcome_candidates(path: Path, *, min_coverage_pct: float) -> list[CandidatePeriod]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    family = "session_liquidity" if "session_liquidity" in path.name else "execution_cost"
    pod = "pod_c"
    periods: list[CandidatePeriod] = []
    for row in payload.get("policy_outcomes", []):
        if not isinstance(row, dict):
            continue
        policy = str(row.get("policy") or "")
        periods.append(
            period_from_policy_outcome(
                row,
                candidate=f"pod_c_{family}::{policy}",
                pod=pod,
                family=family,
                source=str(path),
                period=str(row.get("window") or ""),
                coverage_pct=100.0,
                sufficient_coverage=True,
            )
        )
    return periods


def load_p120_candidates(path: Path, *, min_coverage_pct: float) -> list[CandidatePeriod]:
    del min_coverage_pct
    payload = json.loads(path.read_text(encoding="utf-8"))
    periods: list[CandidatePeriod] = []
    for row in payload.get("summary", []):
        if not isinstance(row, dict):
            continue
        cohort = str(row.get("cohort") or "")
        deduped = bool(row.get("deduped_only"))
        symbols = {str(k): int(v) for k, v in (row.get("by_symbol") or {}).items()}
        candidates = int(row.get("candidates") or 0)
        proxy_pnl = to_float(row.get("proxy_pnl_usd"))
        top_symbol, top_count, concentration = symbol_concentration(symbols, max(candidates, 1))
        periods.append(
            CandidatePeriod(
                candidate=f"pod_c_oil_relative_value::{cohort}_{'deduped' if deduped else 'raw'}",
                pod="pod_c",
                family="oil_relative_value",
                source=str(path),
                period="live_observed",
                base_pnl_usd=0.0,
                adjusted_pnl_usd=round(proxy_pnl, 6),
                delta_usd=round(proxy_pnl, 6),
                trades=candidates,
                touched_trades=candidates,
                touched_pnl_usd=round(proxy_pnl, 6),
                touched_winners=0,
                touched_losers=0,
                capped_winner_pnl_usd=0.0,
                capped_loser_pnl_usd=0.0,
                base_profit_factor=None,
                adjusted_profit_factor=optional_float(row.get("profit_factor")),
                win_rate=optional_float(row.get("win_rate")),
                coverage_pct=100.0,
                sufficient_coverage=True,
                top_symbol=top_symbol,
                top_symbol_count=top_count,
                max_symbol_concentration_pct=round(concentration, 6),
                symbols=symbols,
                notes="proxy_pnl_not_full_bot_delta",
            )
        )
    return periods


def period_from_policy_outcome(
    row: dict[str, Any],
    *,
    candidate: str,
    pod: str,
    family: str,
    source: str,
    period: str,
    coverage_pct: float | None,
    sufficient_coverage: bool,
) -> CandidatePeriod:
    symbols = {str(k): int(v) for k, v in (row.get("blocked_symbols") or row.get("touched_symbols") or {}).items()}
    total_trades = int(row.get("total_trades") or 0)
    touched_trades = int(row.get("blocked_trades") or row.get("touched_trades") or 0)
    touched_pnl = to_float(row.get("blocked_pnl_usd", row.get("touched_pnl_usd")))
    touched_winners = int(row.get("blocked_winners") or row.get("touched_winners") or 0)
    touched_losers = int(row.get("blocked_losers") or row.get("touched_losers") or 0)
    top_symbol, top_count, concentration = symbol_concentration(symbols, max(touched_trades, 1))
    base = to_float(row.get("base_pnl_usd"))
    adjusted = to_float(row.get("kept_pnl_usd", row.get("adjusted_pnl_usd")))
    delta = to_float(row.get("delta_usd"))
    return CandidatePeriod(
        candidate=candidate,
        pod=pod,
        family=family,
        source=source,
        period=period,
        base_pnl_usd=round(base, 6),
        adjusted_pnl_usd=round(adjusted, 6),
        delta_usd=round(delta, 6),
        trades=total_trades,
        touched_trades=touched_trades,
        touched_pnl_usd=round(touched_pnl, 6),
        touched_winners=touched_winners,
        touched_losers=touched_losers,
        capped_winner_pnl_usd=0.0,
        capped_loser_pnl_usd=0.0,
        base_profit_factor=None,
        adjusted_profit_factor=None,
        win_rate=None,
        coverage_pct=coverage_pct,
        sufficient_coverage=sufficient_coverage,
        top_symbol=top_symbol,
        top_symbol_count=top_count,
        max_symbol_concentration_pct=round(concentration, 6),
        symbols=symbols,
    )


def summarize_adjusted_rows(
    *,
    candidate: str,
    pod: str,
    family: str,
    source: str,
    period: str,
    rows: list[dict[str, Any]],
    coverage_pct: float | None,
    sufficient_coverage: bool,
) -> CandidatePeriod:
    base_pnls = [to_float(row.get("original_pnl_usd")) for row in rows]
    adjusted_pnls = [to_float(row.get("adjusted_pnl_usd")) for row in rows]
    touched_rows = [row for row in rows if bool(row.get("touched"))]
    touched_pnls = [to_float(row.get("original_pnl_usd")) for row in touched_rows]
    symbols: dict[str, int] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "")
        if symbol:
            symbols[symbol] = symbols.get(symbol, 0) + 1
    top_symbol, top_count, concentration = symbol_concentration(symbols, max(len(rows), 1))
    return CandidatePeriod(
        candidate=candidate,
        pod=pod,
        family=family,
        source=source,
        period=period,
        base_pnl_usd=round(sum(base_pnls), 6),
        adjusted_pnl_usd=round(sum(adjusted_pnls), 6),
        delta_usd=round(sum(adjusted_pnls) - sum(base_pnls), 6),
        trades=len(rows),
        touched_trades=len(touched_rows),
        touched_pnl_usd=round(sum(touched_pnls), 6),
        touched_winners=sum(1 for value in touched_pnls if value > 0.0),
        touched_losers=sum(1 for value in touched_pnls if value < 0.0),
        capped_winner_pnl_usd=round(sum(value for value in touched_pnls if value > 0.0), 6),
        capped_loser_pnl_usd=round(sum(value for value in touched_pnls if value < 0.0), 6),
        base_profit_factor=profit_factor(base_pnls),
        adjusted_profit_factor=profit_factor(adjusted_pnls),
        win_rate=round(sum(1 for value in adjusted_pnls if value > 0.0) / len(adjusted_pnls), 6)
        if adjusted_pnls
        else None,
        coverage_pct=coverage_pct,
        sufficient_coverage=sufficient_coverage,
        top_symbol=top_symbol,
        top_symbol_count=top_count,
        max_symbol_concentration_pct=round(concentration, 6),
        symbols=dict(sorted(symbols.items())),
    )


def build_verdicts(
    periods: list[CandidatePeriod],
    *,
    max_symbol_concentration_pct: float,
) -> list[CandidateVerdict]:
    grouped: dict[str, list[CandidatePeriod]] = defaultdict(list)
    for row in periods:
        grouped[row.candidate].append(row)
    verdicts = [
        classify_candidate(candidate, rows, max_symbol_concentration_pct=max_symbol_concentration_pct)
        for candidate, rows in sorted(grouped.items())
    ]
    return sorted(
        verdicts,
        key=lambda row: (
            {"promotable_candidate": 0, "shadow_candidate": 1, "reject": 2}.get(row.classification, 3),
            -row.total_delta_usd,
            row.candidate,
        ),
    )


def classify_candidate(
    candidate: str,
    rows: list[CandidatePeriod],
    *,
    max_symbol_concentration_pct: float,
) -> CandidateVerdict:
    eval_rows = [row for row in rows if row.period != "all"]
    covered = [row for row in eval_rows if row.sufficient_coverage]
    insufficient = len(eval_rows) - len(covered)
    positive = sum(1 for row in covered if row.delta_usd > 0.0)
    flat = sum(1 for row in covered if row.delta_usd == 0.0)
    negative = sum(1 for row in covered if row.delta_usd < 0.0)
    total_delta = sum(row.delta_usd for row in covered)
    max_concentration = max((row.max_symbol_concentration_pct for row in rows), default=0.0)
    touched_winners = sum(row.capped_winner_pnl_usd for row in rows)
    touched_losers = sum(row.capped_loser_pnl_usd for row in rows)
    reasons: list[str] = []
    if insufficient:
        reasons.append(f"insufficient_coverage_periods={insufficient}")
    if len(covered) < 2:
        reasons.append("needs_at_least_two_covered_periods")
    if flat:
        reasons.append(f"flat_covered_periods={flat}")
    if negative:
        reasons.append(f"negative_covered_periods={negative}")
    if max_concentration > max_symbol_concentration_pct:
        reasons.append(f"symbol_concentration>{max_symbol_concentration_pct:.1f}%")
    if total_delta <= 0.0:
        reasons.append("non_positive_total_delta")
    if touched_winners > abs(touched_losers) and touched_winners > 0.0:
        reasons.append("caps_more_winner_pnl_than_loser_pnl")

    if total_delta > 0.0 and len(covered) >= 2 and positive == len(covered) and not reasons:
        classification = "promotable_candidate"
    elif total_delta > 0.0 and positive > 0 and "non_positive_total_delta" not in reasons:
        classification = "shadow_candidate"
    else:
        classification = "reject"
    if not reasons:
        reasons.append("passes_lab_filters")
    first = rows[0] if rows else None
    return CandidateVerdict(
        candidate=candidate,
        pod=first.pod if first else "",
        family=first.family if first else "",
        classification=classification,
        total_delta_usd=round(total_delta, 6),
        evaluation_periods=len(covered),
        positive_periods=positive,
        negative_periods=negative,
        insufficient_coverage_periods=insufficient,
        max_symbol_concentration_pct=round(max_concentration, 6),
        touched_winner_pnl_usd=round(touched_winners, 6),
        touched_loser_pnl_usd=round(touched_losers, 6),
        reasons=";".join(reasons),
    )


def profit_factor(pnls: list[float]) -> float | None:
    gains = sum(value for value in pnls if value > 0.0)
    losses = -sum(value for value in pnls if value < 0.0)
    if losses <= 0.0:
        return None
    return round(gains / losses, 6)


def symbol_concentration(symbols: dict[str, int], denominator: int) -> tuple[str, int, float]:
    if not symbols or denominator <= 0:
        return "", 0, 0.0
    top_symbol, top_count = max(symbols.items(), key=lambda item: (item[1], item[0]))
    return top_symbol, top_count, top_count / denominator * 100.0


def jsonl_records(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                yield payload


def optional_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def to_float(value: object) -> float:
    result = optional_float(value)
    return result if result is not None else 0.0


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
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_markdown(
    path: Path,
    payload: dict[str, Any],
    verdicts: list[CandidateVerdict],
    periods: list[CandidatePeriod],
) -> None:
    lines = [
        "# PnL robust candidate lab",
        "",
        f"- generated_at: `{payload.get('generated_at')}`",
        "- decision: `research_only_no_live_change`",
        f"- candidate_count: `{payload.get('candidate_count')}`",
        f"- period_count: `{payload.get('period_count')}`",
        f"- parameters: `{payload.get('parameters')}`",
        "",
        "## Candidate Summary",
        "",
        "| Candidate | Pod | Family | Class | Delta | Periods | +/- | Max symbol | Winner/loser pnl touched | Reasons |",
        "| --- | --- | --- | --- | ---: | ---: | --- | ---: | ---: | --- |",
    ]
    for row in verdicts:
        lines.append(
            f"| `{row.candidate}` | `{row.pod}` | `{row.family}` | `{row.classification}` | "
            f"{row.total_delta_usd:+.2f} | {row.evaluation_periods} | "
            f"{row.positive_periods}/{row.negative_periods} | "
            f"{row.max_symbol_concentration_pct:.2f}% | "
            f"{row.touched_winner_pnl_usd:.2f}/{row.touched_loser_pnl_usd:.2f} | "
            f"`{row.reasons}` |"
        )
    lines.extend(
        [
            "",
            "## Period Details",
            "",
            "| Candidate | Period | Base | Adjusted | Delta | Trades | Touched | Coverage | Top symbol | Notes |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for row in periods:
        coverage = "n/a" if row.coverage_pct is None else f"{row.coverage_pct:.2f}%"
        lines.append(
            f"| `{row.candidate}` | `{row.period}` | {row.base_pnl_usd:.2f} | "
            f"{row.adjusted_pnl_usd:.2f} | {row.delta_usd:+.2f} | {row.trades} | "
            f"{row.touched_trades} | {coverage} | `{row.top_symbol}` "
            f"({row.max_symbol_concentration_pct:.2f}%) | `{row.notes}` |"
        )
    lines.extend(
        [
            "",
            "## Lecture",
            "",
            "- `promotable_candidate` signifie seulement que le candidat passe les filtres du lab; cela ne declenche aucun live.",
            "- `shadow_candidate` signifie positif mais incomplet: OOS, coverage, concentration ou donnees de fill insuffisantes.",
            "- Les candidats Pod C importes depuis P103/P121/P122 restent des proxies cap-only; les candidats oil P120 sont des proxies PnL, pas des deltas full-bot.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
