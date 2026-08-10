#!/usr/bin/env python3
"""Comparable full-bot replay for selected chart-pattern evolutions.

Research-only: replays the current TRIDENT full-bot baseline on the official snapshot
input, then injects one chart-pattern evolution at a time as a synthetic sleeve
in the same pass. This is stricter than the OHLCV overlay because entries occur
on the first replay snapshot after the 4h candle close, use snapshot prices,
include dry-run fees/slippage, and avoid symbols already owned by the baseline.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backtest.full_bot_replay import FullBotBacktestRunner
from app.backtest.pod_report import PodABacktestReport
from app.execution.directional_executor import DirectionalExecutor
from app.execution.live_cap import apply_live_notional_cap
from app.settings import AppConfig, load_config
from app.trident.pod_a.leverage import LeveragePolicy
from app.trident.supervisor import TridentSupervisor
from app.trident.types import (
    PodName,
    RegimeSnapshot,
    RiskDecision,
    SignalPreview,
    TradePlan,
    symbol_market_snapshot_from_mapping,
)
from scripts import run_cup_handle_pattern_scan as cup


DEFAULT_BASELINE_INPUT = (
    "server-data/replay_inputs/"
    "external_reference_multisource_20260405_20260513_baseline.jsonl"
)
DEFAULT_CASES_CSV = (
    "server-data/replay_reports/chart_pattern_skill_replay_20260706T000000Z/"
    "chart_pattern_cases.csv"
)
DEFAULT_OUTPUT_DIR = (
    "server-data/replay_reports/chart_pattern_fullbot_comparable_20260706T000000Z"
)


@dataclass(frozen=True, slots=True)
class EvoSpec:
    name: str
    pattern: str
    filter_name: str
    target_fraction_pct: float
    stop_loss_pct: float
    description: str


@dataclass(frozen=True, slots=True)
class PatternCase:
    pattern: str
    symbol: str
    timeframe: str
    validation_time: str
    signal_time: str
    target_pct_from_entry: float
    target_bps: float
    stop_bps: float
    horizon_bars: int
    score: float
    structure_depth_pct: float
    breakout_margin_pct: float
    volume_ratio20: float | None
    atr14_pct: float | None


@dataclass(slots=True)
class EvoState:
    spec: EvoSpec
    executor: DirectionalExecutor
    cases: list[PatternCase]
    pending: deque[PatternCase]
    next_index: int = 0
    signal_count: int = 0
    stale_signal_count: int = 0
    accepted_signal_count: int = 0
    opened_count: int = 0
    skipped_open_count: int = 0
    skipped_baseline_overlap_count: int = 0
    skipped_overlay_overlap_count: int = 0
    skipped_capacity_count: int = 0
    skipped_per_bar_count: int = 0
    skipped_spread_count: int = 0
    closed_trades: list[dict[str, Any]] | None = None
    pattern_pnl: Counter[str] | None = None

    def __post_init__(self) -> None:
        self.closed_trades = []
        self.pattern_pnl = Counter()


@dataclass(slots=True)
class ScenarioResult:
    scenario: str
    description: str
    records_processed: int
    duplicate_timestamps_skipped: int
    first_timestamp: str | None
    last_timestamp: str | None
    runtime_seconds: float
    baseline_pod_a_pnl_usd: float
    baseline_pod_a_trades: int
    baseline_pod_b_pnl_usd: float
    baseline_pod_b_trades: int
    baseline_pod_c_pnl_usd: float
    baseline_pod_c_trades: int
    baseline_ac_pnl_usd: float
    baseline_ac_trades: int
    overlay_pnl_usd: float
    overlay_trades: int
    overlay_win_rate_pct: float | None
    overlay_profit_factor: float | None
    overlay_max_drawdown_usd: float
    total_ac_plus_overlay_pnl_usd: float
    delta_vs_current_ac_usd: float
    signal_count: int
    accepted_signal_count: int
    opened_count: int
    skipped_open_count: int
    skipped_baseline_overlap_count: int
    skipped_overlay_overlap_count: int
    skipped_capacity_count: int
    skipped_per_bar_count: int
    skipped_spread_count: int
    stale_signal_count: int
    close_reasons: dict[str, int]
    pnl_by_symbol: dict[str, float]
    trades_by_symbol: dict[str, int]
    avg_mfe_bps: float | None
    avg_mae_bps: float | None


def evo_specs() -> list[EvoSpec]:
    return [
        EvoSpec(
            name="evo_double_bottom_only",
            pattern="double_bottom",
            filter_name="4h_target_low_q50_score_high_q50_volume_high_q50",
            target_fraction_pct=100.0,
            stop_loss_pct=6.0,
            description="Double bottom 4h filtre, target 100% measured move, SL 6%.",
        ),
        EvoSpec(
            name="evo_triangle_breakout_only",
            pattern="triangle_breakout",
            filter_name="4h_target_low_q50_score_high_q50_volume_high_q50",
            target_fraction_pct=75.0,
            stop_loss_pct=15.0,
            description="Triangle breakout 4h filtre, target 75% measured move, SL 15%.",
        ),
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--baseline-input", default=DEFAULT_BASELINE_INPUT)
    parser.add_argument("--cases-csv", default=DEFAULT_CASES_CSV)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--notional-usd", type=float, default=200.0)
    parser.add_argument("--max-open-positions", type=int, default=4)
    parser.add_argument("--max-new-positions-per-bar", type=int, default=2)
    parser.add_argument("--max-spread-bps", type=float, default=10.0)
    parser.add_argument("--max-signal-lag-hours", type=float, default=12.0)
    parser.add_argument("--include-blocked-symbols", action="store_true")
    cap_group = parser.add_mutually_exclusive_group()
    cap_group.add_argument("--apply-live-caps", action="store_true")
    cap_group.add_argument("--no-live-caps", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    config = load_config(args.config)
    blocked_symbols = set() if args.include_blocked_symbols else {
        str(symbol).upper()
        for symbol in getattr(config.hyperliquid, "tradable_blocked_symbols", [])
    }
    cases = load_cases(Path(args.cases_csv), specs=evo_specs(), blocked_symbols=blocked_symbols)
    states = [
        EvoState(
            spec=spec,
            executor=DirectionalExecutor(config),
            cases=sorted(cases.get(spec.name, []), key=lambda item: (cup.iso_ms(item.signal_time), item.symbol)),
            pending=deque(),
        )
        for spec in evo_specs()
    ]
    results = run_fullbot_comparable(
        config=config,
        input_path=Path(args.baseline_input),
        states=states,
        notional_usd=float(args.notional_usd),
        max_open_positions=int(args.max_open_positions),
        max_new_positions_per_bar=int(args.max_new_positions_per_bar),
        max_spread_bps=float(args.max_spread_bps),
        max_signal_lag_hours=float(args.max_signal_lag_hours),
        apply_live_caps=bool(args.apply_live_caps),
    )
    payload = {
        "kind": "chart_pattern_fullbot_comparable_replay",
        "generated_at": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "status": "research_only_no_live_change",
        "inputs": {
            "config": str(args.config),
            "baseline_input": str(args.baseline_input),
            "cases_csv": str(args.cases_csv),
        },
        "method": {
            "evolutions": [asdict(spec) for spec in evo_specs()],
            "blocked_symbols_excluded": sorted(blocked_symbols),
            "signal_timing": "4h pattern validation is shifted to candle close; entry occurs on first replay snapshot at or after that time.",
        "baseline": "Current full-bot baseline is replayed in the same pass; each evolution has an independent overlay executor.",
            "notional_usd": float(args.notional_usd),
            "max_open_positions": int(args.max_open_positions),
            "max_new_positions_per_bar": int(args.max_new_positions_per_bar),
            "max_spread_bps": float(args.max_spread_bps),
            "live_caps": bool(args.apply_live_caps),
            "limits": [
                "Overlay is still a synthetic research sleeve, not live code.",
                "Uses replay snapshots/mid-price path, not full 4h candle high/low intrabar ordering.",
                "Avoids symbols owned by the baseline at entry time.",
            ],
        },
        "results": [asdict(row) for row in results],
        "closed_trades": {
            state.spec.name: state.closed_trades or []
            for state in states
        },
    }
    write_csv(output_dir / "scenario_summary.csv", results)
    write_closed_trade_csv(output_dir / "closed_trades.csv", states)
    (output_dir / "fullbot_comparable_replay.json").write_text(
        json.dumps(payload, indent=2, default=str, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_markdown(output_dir / "fullbot_comparable_replay.md", payload)
    print(output_dir)


def load_cases(
    path: Path,
    *,
    specs: list[EvoSpec],
    blocked_symbols: set[str],
) -> dict[str, list[PatternCase]]:
    raw_rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        for raw in csv.DictReader(handle):
            if raw.get("timeframe") == "4h":
                raw_rows.append(raw)
    thresholds = compute_thresholds(raw_rows, blocked_symbols=blocked_symbols)
    result: dict[str, list[PatternCase]] = {spec.name: [] for spec in specs}
    for raw in raw_rows:
        symbol = str(raw.get("symbol", "")).upper()
        if symbol in blocked_symbols:
            continue
        pattern = str(raw.get("pattern", ""))
        matching_specs = [item for item in specs if item.pattern == pattern]
        if not matching_specs:
            continue
        for spec in matching_specs:
            if not matches_filter(raw, spec, thresholds):
                continue
            validation_time = str(raw.get("validation_time", ""))
            signal_time = cup.ms_iso(cup.iso_ms(validation_time) + cup.INTERVAL_MS["4h"])
            target_pct = parse_float(raw.get("target_pct_from_entry")) * spec.target_fraction_pct / 100.0
            result[spec.name].append(
                PatternCase(
                    pattern=pattern,
                    symbol=symbol,
                    timeframe="4h",
                    validation_time=validation_time,
                    signal_time=signal_time,
                    target_pct_from_entry=parse_float(raw.get("target_pct_from_entry")),
                    target_bps=target_pct * 100.0,
                    stop_bps=spec.stop_loss_pct * 100.0,
                    horizon_bars=parse_int(raw.get("horizon_bars")),
                    score=parse_float(raw.get("score")),
                    structure_depth_pct=parse_float(raw.get("structure_depth_pct")),
                    breakout_margin_pct=parse_float(raw.get("breakout_margin_pct")),
                    volume_ratio20=parse_optional_float(raw.get("volume_ratio20")),
                    atr14_pct=parse_optional_float(raw.get("atr14_pct")),
                )
            )
    return result


def compute_thresholds(
    raw_rows: list[dict[str, Any]],
    *,
    blocked_symbols: set[str],
) -> dict[tuple[str, str, int], float]:
    thresholds: dict[tuple[str, str, int], float] = {}
    fields = [
        "target_pct_from_entry",
        "structure_depth_pct",
        "breakout_margin_pct",
        "volume_ratio20",
        "score",
    ]
    patterns = sorted({str(row.get("pattern", "")) for row in raw_rows})
    for pattern in patterns:
        scoped = [
            row for row in raw_rows
            if str(row.get("pattern", "")) == pattern
            and str(row.get("symbol", "")).upper() not in blocked_symbols
        ]
        for field in fields:
            values = [parse_optional_float(row.get(field)) for row in scoped]
            for pct_value in (33, 50, 66):
                value = cup.percentile(values, float(pct_value))
                if value is not None:
                    thresholds[(pattern, field, pct_value)] = value
    return thresholds


def matches_filter(
    raw: dict[str, Any],
    spec: EvoSpec,
    thresholds: dict[tuple[str, str, int], float],
) -> bool:
    if spec.pattern in {"double_bottom", "triangle_breakout"}:
        return (
            le(raw, thresholds, spec.pattern, "target_pct_from_entry", 50)
            and ge(raw, thresholds, spec.pattern, "score", 50)
            and ge(raw, thresholds, spec.pattern, "volume_ratio20", 50)
        )
    return False


def run_fullbot_comparable(
    *,
    config: AppConfig,
    input_path: Path,
    states: list[EvoState],
    notional_usd: float,
    max_open_positions: int,
    max_new_positions_per_bar: int,
    max_spread_bps: float,
    max_signal_lag_hours: float,
    apply_live_caps: bool,
) -> list[ScenarioResult]:
    helper = FullBotBacktestRunner(config, force_enable_all_pods=True, apply_live_notional_caps=apply_live_caps)
    supervisor = TridentSupervisor(config=helper.config, profile="chart-pattern-fullbot-comparable", mode="dry-run")
    pod_a_report = PodABacktestReport(reference_equity_usd=helper.config.trident.capital.reference_equity_usd)
    pod_b_report = PodABacktestReport(reference_equity_usd=helper.config.trident.capital.reference_equity_usd)
    pod_c_report = PodABacktestReport(reference_equity_usd=helper.config.trident.capital.reference_equity_usd)
    latest_snapshots_by_symbol: dict[str, Any] = {}
    seen_timestamps: set[str] = set()
    records_processed = 0
    duplicate_timestamps_skipped = 0
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    started = time.perf_counter()

    for record in helper.loader.iter_merged_jsonl(input_path):
        timestamp = record.timestamp
        if timestamp and timestamp in seen_timestamps:
            duplicate_timestamps_skipped += 1
            continue
        if timestamp:
            seen_timestamps.add(timestamp)
            first_timestamp = first_timestamp or timestamp
            last_timestamp = timestamp
        snapshots = [symbol_market_snapshot_from_mapping(item) for item in record.symbols]
        previous_snapshots_by_symbol = dict(latest_snapshots_by_symbol)
        latest_snapshots_by_symbol.update({snapshot.symbol: snapshot for snapshot in snapshots})

        if record.capture_reason == "maintenance_refresh":
            helper._process_maintenance_record(
                supervisor=supervisor,
                pod_a_report=pod_a_report,
                pod_b_report=pod_b_report,
                pod_c_report=pod_c_report,
                snapshots=snapshots,
                timestamp=timestamp,
                source_file=record.source_file,
                stream_source=record.stream_source,
            )
            baseline_open_symbols = baseline_open(helper)
            for state in states:
                process_overlay(
                    state=state,
                    config=helper.config,
                    snapshots=snapshots,
                    timestamp=timestamp,
                    baseline_open_symbols=baseline_open_symbols,
                    notional_usd=notional_usd,
                    max_open_positions=max_open_positions,
                    max_new_positions_per_bar=max_new_positions_per_bar,
                    max_spread_bps=max_spread_bps,
                    max_signal_lag_hours=max_signal_lag_hours,
                    apply_live_caps=apply_live_caps,
                    allow_entries=False,
                )
            continue

        previous_regime = supervisor.state.regime.value
        cluster_regime_snapshots = {
            cluster: RegimeSnapshot(**snap)
            for cluster, snap in (record.cluster_regime_snapshots or {}).items()
            if isinstance(snap, dict)
        }
        supervisor.apply_regime_snapshot(
            RegimeSnapshot(**record.regime_snapshot),
            cluster_regime_snapshots=cluster_regime_snapshots,
        )
        current_regime = supervisor.state.regime.value
        helper._process_pod_a(
            supervisor=supervisor,
            report=pod_a_report,
            snapshots=snapshots,
            timestamp=timestamp,
            source_file=record.source_file,
            previous_regime=previous_regime,
            current_regime=current_regime,
        )
        helper._process_pod_c(
            supervisor=supervisor,
            report=pod_c_report,
            snapshots=snapshots,
            timestamp=timestamp,
            source_file=record.source_file,
            previous_regime=previous_regime,
            current_regime=current_regime,
        )
        helper._process_pod_b(
            supervisor=supervisor,
            report=pod_b_report,
            snapshots=snapshots,
            previous_snapshots_by_symbol=previous_snapshots_by_symbol,
            timestamp=timestamp,
            source_file=record.source_file,
            previous_regime=previous_regime,
            current_regime=current_regime,
        )
        baseline_open_symbols = baseline_open(helper)
        for state in states:
            process_overlay(
                state=state,
                config=helper.config,
                snapshots=snapshots,
                timestamp=timestamp,
                baseline_open_symbols=baseline_open_symbols,
                notional_usd=notional_usd,
                max_open_positions=max_open_positions,
                max_new_positions_per_bar=max_new_positions_per_bar,
                max_spread_bps=max_spread_bps,
                max_signal_lag_hours=max_signal_lag_hours,
                apply_live_caps=apply_live_caps,
                allow_entries=True,
            )
        records_processed += 1

    latest_snapshots = list(latest_snapshots_by_symbol.values())
    helper._finalize_directional_report(
        supervisor=supervisor,
        report=pod_a_report,
        executor=helper.pod_a_executor,
        latest_snapshots=latest_snapshots,
        last_timestamp=last_timestamp,
        closed_trade_recorder=helper._record_pod_a_closed_trade,
    )
    helper._finalize_directional_report(
        supervisor=supervisor,
        report=pod_c_report,
        executor=helper.pod_c_executor,
        latest_snapshots=latest_snapshots,
        last_timestamp=last_timestamp,
    )
    helper._finalize_directional_report(
        supervisor=supervisor,
        report=pod_b_report,
        executor=helper.pod_b_executor,
        latest_snapshots=latest_snapshots,
        last_timestamp=last_timestamp,
        closed_trade_recorder=helper._record_pod_b_closed_trade,
    )
    for state in states:
        closed, _fills = state.executor.finalize(snapshots=latest_snapshots, timestamp=last_timestamp)
        for trade in closed:
            record_overlay_trade(state, trade)

    pod_a = pod_a_report.to_dict()
    pod_b = pod_b_report.to_dict()
    pod_c = pod_c_report.to_dict()
    baseline_a = float(pod_a.get("realized_pnl_usd", 0.0) or 0.0)
    baseline_b = float(pod_b.get("realized_pnl_usd", 0.0) or 0.0)
    baseline_c = float(pod_c.get("realized_pnl_usd", 0.0) or 0.0)
    baseline_a_trades = int(pod_a.get("closed_trade_count", 0) or 0)
    baseline_b_trades = int(pod_b.get("closed_trade_count", 0) or 0)
    baseline_c_trades = int(pod_c.get("closed_trade_count", 0) or 0)
    baseline_ac = round(baseline_a + baseline_b + baseline_c, 6)
    runtime = round(time.perf_counter() - started, 3)
    return [
        ScenarioResult(
            scenario="current_ac",
            description="Current full-bot baseline, no chart-pattern overlay.",
            records_processed=records_processed,
            duplicate_timestamps_skipped=duplicate_timestamps_skipped,
            first_timestamp=first_timestamp,
            last_timestamp=last_timestamp,
            runtime_seconds=runtime,
            baseline_pod_a_pnl_usd=round(baseline_a, 6),
            baseline_pod_a_trades=baseline_a_trades,
            baseline_pod_b_pnl_usd=round(baseline_b, 6),
            baseline_pod_b_trades=baseline_b_trades,
            baseline_pod_c_pnl_usd=round(baseline_c, 6),
            baseline_pod_c_trades=baseline_c_trades,
            baseline_ac_pnl_usd=baseline_ac,
            baseline_ac_trades=baseline_a_trades + baseline_b_trades + baseline_c_trades,
            overlay_pnl_usd=0.0,
            overlay_trades=0,
            overlay_win_rate_pct=None,
            overlay_profit_factor=None,
            overlay_max_drawdown_usd=0.0,
            total_ac_plus_overlay_pnl_usd=baseline_ac,
            delta_vs_current_ac_usd=0.0,
            signal_count=0,
            accepted_signal_count=0,
            opened_count=0,
            skipped_open_count=0,
            skipped_baseline_overlap_count=0,
            skipped_overlay_overlap_count=0,
            skipped_capacity_count=0,
            skipped_per_bar_count=0,
            skipped_spread_count=0,
            stale_signal_count=0,
            close_reasons={},
            pnl_by_symbol={},
            trades_by_symbol={},
            avg_mfe_bps=None,
            avg_mae_bps=None,
        )
    ] + [
        build_result(
            state=state,
            records_processed=records_processed,
            duplicate_timestamps_skipped=duplicate_timestamps_skipped,
            first_timestamp=first_timestamp,
            last_timestamp=last_timestamp,
            runtime=runtime,
            baseline_a=baseline_a,
            baseline_a_trades=baseline_a_trades,
            baseline_b=baseline_b,
            baseline_b_trades=baseline_b_trades,
            baseline_c=baseline_c,
            baseline_c_trades=baseline_c_trades,
            baseline_ac=baseline_ac,
        )
        for state in states
    ]


def process_overlay(
    *,
    state: EvoState,
    config: AppConfig,
    snapshots: list[Any],
    timestamp: str | None,
    baseline_open_symbols: set[str],
    notional_usd: float,
    max_open_positions: int,
    max_new_positions_per_bar: int,
    max_spread_bps: float,
    max_signal_lag_hours: float,
    apply_live_caps: bool,
    allow_entries: bool,
) -> None:
    snapshot_by_symbol = {snapshot.symbol: snapshot for snapshot in snapshots}
    current_ms = cup.iso_ms(timestamp) if timestamp else 0
    while state.next_index < len(state.cases) and cup.iso_ms(state.cases[state.next_index].signal_time) <= current_ms:
        state.pending.append(state.cases[state.next_index])
        state.next_index += 1

    plans: list[TradePlan] = []
    if allow_entries:
        opened_this_bar = 0
        kept_pending: deque[PatternCase] = deque()
        candidates = sorted(state.pending, key=lambda item: (item.score, -cup.iso_ms(item.signal_time)), reverse=True)
        state.pending.clear()
        for case in candidates:
            lag_hours = (current_ms - cup.iso_ms(case.signal_time)) / 3_600_000.0
            if lag_hours > max_signal_lag_hours:
                state.stale_signal_count += 1
                continue
            snapshot = snapshot_by_symbol.get(case.symbol)
            if snapshot is None:
                kept_pending.append(case)
                continue
            state.signal_count += 1
            if snapshot.spread_bps > max_spread_bps:
                state.skipped_spread_count += 1
                continue
            if case.symbol in baseline_open_symbols:
                state.skipped_baseline_overlap_count += 1
                continue
            if state.executor.portfolio.has_open_position(case.symbol):
                state.skipped_overlay_overlap_count += 1
                continue
            if len(state.executor.portfolio.open_positions) >= max_open_positions:
                state.skipped_capacity_count += 1
                continue
            if opened_this_bar >= max_new_positions_per_bar:
                state.skipped_per_bar_count += 1
                continue
            plans.append(build_plan(case=case, state=state, snapshot=snapshot, notional_usd=notional_usd))
            opened_this_bar += 1
            state.accepted_signal_count += 1
        state.pending.extend(kept_pending)

    if apply_live_caps:
        leverage = LeveragePolicy(config.pod_a)
        plans = [
            apply_live_notional_cap(
                plan,
                config.trident.execution.live_max_order_notional_usd,
                max_leverage=leverage.max_allowed(plan.symbol),
            )
            for plan in plans
        ]
    decisions = [RiskDecision(accepted=True, reason="accepted", trade_plan=plan) for plan in plans]
    previews = [
        SignalPreview(
            symbol=plan.symbol,
            side=plan.side,
            setup=plan.setup,
            confidence=plan.confidence,
            setup_details=dict(plan.setup_details),
        )
        for plan in plans
    ]
    managed_symbols = set(snapshot_by_symbol) - baseline_open_symbols
    execution = state.executor.process_record(
        snapshots=snapshots,
        risk_decisions=decisions,
        signal_sides_by_symbol={preview.symbol: preview.side for preview in previews},
        timestamp=timestamp,
        entry_allowed_symbols=managed_symbols,
        managed_symbols=managed_symbols,
    )
    state.opened_count += len(execution.opened_symbols)
    state.skipped_open_count += len(execution.skipped_open_symbols)
    for trade in execution.closed_trades:
        record_overlay_trade(state, trade)


def build_plan(*, case: PatternCase, state: EvoState, snapshot: Any, notional_usd: float) -> TradePlan:
    horizon_hours = max(1, case.horizon_bars * 4)
    expected_loss = round(notional_usd * case.stop_bps / 10_000.0, 6)
    return TradePlan(
        symbol=case.symbol,
        side="long",
        setup=f"chart_{state.spec.pattern}",
        confidence=0.74,
        target_notional_usd=notional_usd,
        stop_bps=case.stop_bps,
        time_stop_hours=horizon_hours,
        take_profit_bps=case.target_bps,
        break_even_trigger_bps=0.0,
        trailing_activation_bps=0.0,
        trailing_distance_bps=0.0,
        reentry_cooldown_minutes=0,
        margin_usd=round(notional_usd / 2.0, 6),
        requested_leverage=2.0,
        effective_leverage=2.0,
        risk_budget_usd=expected_loss,
        expected_loss_usd=expected_loss,
        setup_details={
            "chart_pattern_evo": state.spec.name,
            "chart_pattern": case.pattern,
            "chart_filter": state.spec.filter_name,
            "chart_validation_time": case.validation_time,
            "chart_signal_time": case.signal_time,
            "chart_replay_entry_time_policy": "first_snapshot_after_4h_candle_close",
            "chart_target_fraction_pct": state.spec.target_fraction_pct,
            "chart_target_bps": round(case.target_bps, 4),
            "chart_stop_bps": round(case.stop_bps, 4),
            "chart_score": round(case.score, 6),
            "chart_structure_depth_pct": round(case.structure_depth_pct, 6),
            "chart_breakout_margin_pct": round(case.breakout_margin_pct, 6),
            "chart_volume_ratio20": round(case.volume_ratio20, 6) if case.volume_ratio20 is not None else "",
            "spread_bps": round(float(getattr(snapshot, "spread_bps", 0.0)), 4),
        },
    )


def record_overlay_trade(state: EvoState, trade: Any) -> None:
    row = asdict(trade)
    assert state.closed_trades is not None
    assert state.pattern_pnl is not None
    state.closed_trades.append(row)
    state.pattern_pnl[state.spec.pattern] += float(row.get("pnl_usd", 0.0) or 0.0)


def build_result(
    *,
    state: EvoState,
    records_processed: int,
    duplicate_timestamps_skipped: int,
    first_timestamp: str | None,
    last_timestamp: str | None,
    runtime: float,
    baseline_a: float,
    baseline_a_trades: int,
    baseline_b: float,
    baseline_b_trades: int,
    baseline_c: float,
    baseline_c_trades: int,
    baseline_ac: float,
) -> ScenarioResult:
    rows = state.closed_trades or []
    pnls = [float(row.get("pnl_usd", 0.0) or 0.0) for row in rows]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]
    by_symbol_pnl = defaultdict(float)
    by_symbol_trades = Counter()
    close_reasons = Counter()
    mfe_values: list[float] = []
    mae_values: list[float] = []
    for row in rows:
        symbol = str(row.get("symbol", ""))
        by_symbol_pnl[symbol] += float(row.get("pnl_usd", 0.0) or 0.0)
        by_symbol_trades[symbol] += 1
        close_reasons[str(row.get("close_reason", ""))] += 1
        mfe_values.append(float(row.get("mfe_bps", 0.0) or 0.0))
        mae_values.append(float(row.get("mae_bps", 0.0) or 0.0))
    overlay_pnl = round(sum(pnls), 6)
    return ScenarioResult(
        scenario=state.spec.name,
        description=state.spec.description,
        records_processed=records_processed,
        duplicate_timestamps_skipped=duplicate_timestamps_skipped,
        first_timestamp=first_timestamp,
        last_timestamp=last_timestamp,
        runtime_seconds=runtime,
        baseline_pod_a_pnl_usd=round(baseline_a, 6),
        baseline_pod_a_trades=baseline_a_trades,
        baseline_pod_b_pnl_usd=round(baseline_b, 6),
        baseline_pod_b_trades=baseline_b_trades,
        baseline_pod_c_pnl_usd=round(baseline_c, 6),
        baseline_pod_c_trades=baseline_c_trades,
        baseline_ac_pnl_usd=round(baseline_ac, 6),
        baseline_ac_trades=baseline_a_trades + baseline_b_trades + baseline_c_trades,
        overlay_pnl_usd=overlay_pnl,
        overlay_trades=len(rows),
        overlay_win_rate_pct=round(100.0 * len(wins) / len(rows), 6) if rows else None,
        overlay_profit_factor=round(sum(wins) / abs(sum(losses)), 6) if losses else None,
        overlay_max_drawdown_usd=round(max_drawdown(pnls), 6),
        total_ac_plus_overlay_pnl_usd=round(baseline_ac + overlay_pnl, 6),
        delta_vs_current_ac_usd=overlay_pnl,
        signal_count=state.signal_count,
        accepted_signal_count=state.accepted_signal_count,
        opened_count=state.opened_count,
        skipped_open_count=state.skipped_open_count,
        skipped_baseline_overlap_count=state.skipped_baseline_overlap_count,
        skipped_overlay_overlap_count=state.skipped_overlay_overlap_count,
        skipped_capacity_count=state.skipped_capacity_count,
        skipped_per_bar_count=state.skipped_per_bar_count,
        skipped_spread_count=state.skipped_spread_count,
        stale_signal_count=state.stale_signal_count,
        close_reasons=dict(sorted(close_reasons.items())),
        pnl_by_symbol={key: round(value, 6) for key, value in sorted(by_symbol_pnl.items())},
        trades_by_symbol=dict(sorted(by_symbol_trades.items())),
        avg_mfe_bps=round(cup.mean(mfe_values), 6) if mfe_values else None,
        avg_mae_bps=round(cup.mean(mae_values), 6) if mae_values else None,
    )


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    rows = payload["results"]
    lines = [
        "# Chart pattern full-bot comparable replay",
        "",
        "Statut: `research_only_no_live_change`.",
        "",
        "## Methode",
        "",
        "- Baseline full-bot rejouee dans la meme passe que les overlays.",
        "- Deux evolutions separees, chacune avec son propre executor overlay.",
        "- Signal 4h injecte au premier snapshot apres cloture de bougie 4h.",
        "- Symboles bloques live exclus par defaut.",
        "- Pas de deploy, pas de config live, pas d'ordre reel.",
        "",
        "## Resultats",
        "",
        "| Scenario | Trades overlay | PnL overlay | Baseline full-bot | Total | Delta | Win | PF | Max DD | Signals | Opened | Overlap baseline | Capacity skips | Close reasons |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        lines.append(
            "| {scenario} | {trades} | {overlay:.2f} | {baseline:.2f} | {total:.2f} | {delta:+.2f} | {win} | {pf} | {dd:.2f} | {signals} | {opened} | {overlap} | {capacity} | `{reasons}` |".format(
                scenario=row["scenario"],
                trades=row["overlay_trades"],
                overlay=row["overlay_pnl_usd"],
                baseline=row["baseline_ac_pnl_usd"],
                total=row["total_ac_plus_overlay_pnl_usd"],
                delta=row["delta_vs_current_ac_usd"],
                win=fmt_pct(row["overlay_win_rate_pct"]),
                pf=fmt_num(row["overlay_profit_factor"]),
                dd=row["overlay_max_drawdown_usd"],
                signals=row["signal_count"],
                opened=row["opened_count"],
                overlap=row["skipped_baseline_overlap_count"],
                capacity=row["skipped_capacity_count"],
                reasons=row["close_reasons"],
            )
        )
    lines.extend(["", "## Breakdown symboles", ""])
    for row in rows:
        if row["scenario"] == "current_ac":
            continue
        lines.extend([
            f"### {row['scenario']}",
            "",
            "| Symbol | Trades | PnL |",
            "| --- | ---: | ---: |",
        ])
        for symbol, pnl in sorted(row["pnl_by_symbol"].items(), key=lambda item: item[1]):
            lines.append(f"| {symbol} | {row['trades_by_symbol'].get(symbol, 0)} | {pnl:.2f} |")
        lines.append("")
    lines.extend([
        "## Fichiers",
        "",
        "- `scenario_summary.csv`",
        "- `closed_trades.csv`",
        "- `fullbot_comparable_replay.json`",
        "- `fullbot_comparable_replay.md`",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[ScenarioResult]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(rows[0]).keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def write_closed_trade_csv(path: Path, states: list[EvoState]) -> None:
    rows: list[dict[str, Any]] = []
    for state in states:
        for row in state.closed_trades or []:
            rows.append({"scenario": state.spec.name, **row})
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def baseline_open(helper: FullBotBacktestRunner) -> set[str]:
    return {
        *helper.pod_a_executor.portfolio.open_positions.keys(),
        *helper.pod_b_executor.portfolio.open_positions.keys(),
        *helper.pod_c_executor.portfolio.open_positions.keys(),
    }


def max_drawdown(pnls: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def le(raw: dict[str, Any], thresholds: dict[tuple[str, str, int], float], pattern: str, field: str, pct_value: int) -> bool:
    value = parse_optional_float(raw.get(field))
    threshold = thresholds.get((pattern, field, pct_value))
    return value is not None and threshold is not None and value <= threshold


def ge(raw: dict[str, Any], thresholds: dict[tuple[str, str, int], float], pattern: str, field: str, pct_value: int) -> bool:
    value = parse_optional_float(raw.get(field))
    threshold = thresholds.get((pattern, field, pct_value))
    return value is not None and threshold is not None and value >= threshold


def parse_float(value: Any) -> float:
    parsed = cup.safe_float(value)
    return 0.0 if parsed is None else parsed


def parse_optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return cup.safe_float(value)


def parse_int(value: Any) -> int:
    parsed = cup.safe_int(value)
    return 0 if parsed is None else parsed


def fmt_pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2f}%"


def fmt_num(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.3f}"


if __name__ == "__main__":
    main()
