#!/usr/bin/env python3
"""P1-05 replay matrix for Pod A A-grade and live quality sizing.

The script keeps live untouched. It replays the current full-bot A/C stack on
the official April/May baseline and the recent live snapshot window while
changing only Pod A A-grade size scales.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backtest.full_bot_replay import FullBotBacktestResult, FullBotBacktestRunner
from app.backtest.pod_a_executor import PodAExecutor
from app.backtest.pod_report import PodABacktestReport
from app.execution.live_cap import apply_live_notional_cap
from app.risk.pod_a_gate import PodARiskGate
from app.settings import AppConfig, load_config
from app.trident.pod_a.leverage import LeveragePolicy
from app.trident.supervisor import TridentSupervisor
from app.trident.types import (
    PodName,
    RegimeSnapshot,
    RiskDecision,
    SymbolMarketSnapshot,
    symbol_market_snapshot_from_mapping,
)


DEFAULT_BASELINE_INPUT = (
    "server-data/replay_inputs/"
    "external_reference_multisource_20260405_20260513_baseline.jsonl"
)
DEFAULT_LIVE_INPUT = "server-data/live_snapshots"
DEFAULT_LIVE_START = "2026-05-14T00:00:00Z"
DEFAULT_LIVE_END = "2026-06-16T00:00:00Z"


@dataclass(frozen=True, slots=True)
class ScenarioSpec:
    name: str
    description: str
    standard_scale: float
    strong_scale: float
    headroom_cap_enabled: bool = False


@dataclass(frozen=True, slots=True)
class WindowSpec:
    name: str
    input_path: Path
    label: str


@dataclass(slots=True)
class ScenarioState:
    spec: ScenarioSpec
    config: AppConfig
    supervisor: TridentSupervisor
    risk_gate: PodARiskGate
    executor: PodAExecutor
    report: PodABacktestReport


@dataclass(slots=True)
class WindowScenarioResult:
    window: str
    scenario: str
    description: str
    standard_scale: float
    strong_scale: float
    headroom_cap_enabled: bool
    records_processed: int
    duplicate_timestamps_skipped: int
    first_timestamp: str | None
    last_timestamp: str | None
    runtime_seconds: float
    total_ac_pnl_usd: float
    total_ac_trades: int
    directional_fees_usd: float
    pod_a_pnl_usd: float
    pod_a_trades: int
    pod_a_win_rate: float | None
    pod_a_profit_factor: float | None
    pod_a_max_drawdown_usd: float
    pod_c_pnl_usd: float
    pod_c_trades: int
    a_grade_trades: int
    strong_a_grade_trades: int
    standard_a_grade_trades: int
    no_a_grade_trades: int
    strong_a_grade_pnl_usd: float
    standard_a_grade_pnl_usd: float
    no_a_grade_pnl_usd: float
    avg_a_grade_size_scale: float | None
    avg_a_grade_requested_size_scale: float | None
    a_grade_headroom_capped_trades: int
    live_quality_scaled_trades: int
    avg_live_quality_multiplier: float | None
    worst_symbol: str | None
    worst_symbol_pnl_usd: float | None
    worst_date: str | None
    worst_date_pnl_usd: float | None
    report_path: str | None
    summary_path: str | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--baseline-input", default=DEFAULT_BASELINE_INPUT)
    parser.add_argument("--live-input", default=DEFAULT_LIVE_INPUT)
    parser.add_argument("--live-start", default=DEFAULT_LIVE_START)
    parser.add_argument("--live-end", default=DEFAULT_LIVE_END)
    parser.add_argument("--output-dir", default="")
    parser.add_argument(
        "--scenarios",
        default="",
        help="Comma-separated scenario names to run; empty runs all scenarios.",
    )
    parser.add_argument(
        "--windows",
        default="baseline,live",
        help="Comma-separated windows to run: baseline, live.",
    )
    parser.add_argument("--no-live-caps", action="store_true")
    parser.add_argument(
        "--respect-config-enabled",
        action="store_true",
        help="Do not force-enable Pod A / Pod C for this research replay.",
    )
    return parser.parse_args()


def default_scenarios(config: AppConfig) -> list[ScenarioSpec]:
    current_standard = float(config.pod_a.a_grade_boost_scale)
    current_strong = float(config.pod_a.a_grade_strong_boost_scale)
    return [
        ScenarioSpec(
            "current",
            (
                "Config courante: standard "
                f"{current_standard:g}, strong {current_strong:g}."
            ),
            current_standard,
            current_strong,
            bool(config.pod_a.a_grade_size_headroom_cap_enabled),
        ),
        ScenarioSpec(
            "headroom_cap_current",
            (
                "Config courante avec cap headroom dormant: le boost A-grade "
                "ne depasse pas la marge symbole ni le risk budget initial."
            ),
            current_standard,
            current_strong,
            True,
        ),
        ScenarioSpec(
            "flat_scale_1p00",
            "A-grade actif mais boost size neutralisé: standard=strong=1.00.",
            1.0,
            1.0,
        ),
        ScenarioSpec(
            "flat_scale_1p25",
            "A-grade actif avec scale unique 1.25 pour standard et strong.",
            1.25,
            1.25,
        ),
        ScenarioSpec(
            "flat_scale_1p40",
            "A-grade actif avec scale unique 1.40 pour standard et strong.",
            1.40,
            1.40,
        ),
        ScenarioSpec(
            "strong_frozen_1p00",
            (
                "Test opératoire: standard courant conservé, "
                "boost strong gelé à 1.00."
            ),
            current_standard,
            1.0,
        ),
    ]


def scenario_config(config: AppConfig, scenario: ScenarioSpec) -> AppConfig:
    return replace(
        config,
        pod_a=replace(
            config.pod_a,
            a_grade_enabled=True,
            a_grade_boost_scale=float(scenario.standard_scale),
            a_grade_strong_boost_scale=float(scenario.strong_scale),
            a_grade_size_headroom_cap_enabled=bool(scenario.headroom_cap_enabled),
        ),
    )


def selected_names(raw: str, *, available: set[str], label: str) -> list[str]:
    if not str(raw or "").strip():
        return sorted(available)
    names = [item.strip() for item in str(raw).split(",") if item.strip()]
    unknown = [name for name in names if name not in available]
    if unknown:
        raise ValueError(f"unknown {label}(s): {', '.join(unknown)}")
    return names


def selected_scenarios(
    scenarios: list[ScenarioSpec],
    raw: str,
) -> list[ScenarioSpec]:
    if not str(raw or "").strip():
        return scenarios
    by_name = {scenario.name: scenario for scenario in scenarios}
    names = [item.strip() for item in str(raw).split(",") if item.strip()]
    unknown = [name for name in names if name not in by_name]
    if unknown:
        raise ValueError(f"unknown scenario(s): {', '.join(unknown)}")
    return [by_name[name] for name in names]


def main() -> None:
    args = parse_args()
    generated_at = utc_stamp()
    output_dir = Path(
        args.output_dir or f"server-data/replay_reports/p105_a_grade_replay_{generated_at}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    config = load_config(args.config)
    live_start = parse_timestamp(args.live_start)
    live_end = parse_timestamp(args.live_end)
    if live_start is None or live_end is None or live_start >= live_end:
        raise ValueError("--live-start and --live-end must define a valid UTC window")

    requested_windows = selected_names(
        args.windows,
        available={"baseline", "live"},
        label="window",
    )
    windows: list[WindowSpec] = []
    live_input_files: list[dict[str, object]] = []
    if "baseline" in requested_windows:
        windows.append(
            WindowSpec(
                "baseline_apr_may",
                Path(args.baseline_input),
                "Baseline officielle avril/mai.",
            )
        )
    if "live" in requested_windows:
        live_input_dir, live_input_files = prepare_snapshot_window(
            snapshots_dir=Path(args.live_input),
            output_dir=output_dir,
            name="live_post_baseline",
            start=live_start,
            end=live_end,
        )
        windows.append(
            WindowSpec(
                "live_post_baseline",
                live_input_dir,
                f"Snapshots live {isoformat(live_start)} -> {isoformat(live_end)}.",
            )
        )
    scenarios = selected_scenarios(default_scenarios(config), args.scenarios)

    results: list[WindowScenarioResult] = []
    for window in windows:
        print(f"window={window.name} status=running", flush=True)
        window_rows = run_window(
            base_config=config,
            window=window,
            scenarios=scenarios,
            apply_live_caps=not args.no_live_caps,
            force_enable_all_pods=not args.respect_config_enabled,
        )
        results.extend(window_rows)
        current = next(row for row in window_rows if row.scenario == "current")
        for row in window_rows:
            print(
                f"window={window.name} scenario={row.scenario} status=done "
                f"total={row.total_ac_pnl_usd:.2f} "
                f"delta_vs_current={row.total_ac_pnl_usd - current.total_ac_pnl_usd:+.2f} "
                f"pod_a={row.pod_a_pnl_usd:.2f} trades={row.total_ac_trades} "
                f"strong_pnl={row.strong_a_grade_pnl_usd:.2f}",
                flush=True,
            )

    write_results_csv(output_dir / "scenario_summary.csv", results)
    payload = {
        "generated_at": generated_at,
        "status": "research_only_no_live_change",
        "live_caps": not args.no_live_caps,
        "config": args.config,
        "live_input_files": live_input_files,
        "windows": [asdict(window) | {"input_path": str(window.input_path)} for window in windows],
        "scenarios": [asdict(scenario) for scenario in scenarios],
        "results": [asdict(row) for row in results],
    }
    (output_dir / "p105_a_grade_replay.json").write_text(
        json.dumps(payload, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    write_report(output_dir / "p105_a_grade_replay.md", rows=results, generated_at=generated_at)
    print(output_dir)


def run_window(
    *,
    base_config: AppConfig,
    window: WindowSpec,
    scenarios: list[ScenarioSpec],
    apply_live_caps: bool,
    force_enable_all_pods: bool,
) -> list[WindowScenarioResult]:
    helper = FullBotBacktestRunner(
        base_config,
        force_enable_all_pods=force_enable_all_pods,
        apply_live_notional_caps=apply_live_caps,
    )
    pod_c_supervisor = TridentSupervisor(
        config=helper.config,
        profile=f"p105-pod-c-{window.name}",
        mode="dry-run",
    )
    states = [
        scenario_state(
            base_config=base_config,
            scenario=scenario,
            window=window,
            force_enable_all_pods=force_enable_all_pods,
        )
        for scenario in scenarios
    ]
    pod_c_report = PodABacktestReport(
        reference_equity_usd=helper.config.trident.capital.reference_equity_usd,
    )
    latest_snapshots_by_symbol: dict[str, SymbolMarketSnapshot] = {}
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    records_processed = 0
    started = time.perf_counter()

    for record in helper.loader.iter_merged_jsonl(window.input_path):
        timestamp = record.timestamp
        if first_timestamp is None:
            first_timestamp = timestamp
        last_timestamp = timestamp
        snapshots = [symbol_market_snapshot_from_mapping(item) for item in record.symbols]
        latest_snapshots_by_symbol.update({snapshot.symbol: snapshot for snapshot in snapshots})

        if record.capture_reason == "maintenance_refresh":
            for state in states:
                execution = state.executor.process_record(
                    snapshots=snapshots,
                    risk_decisions=[],
                    signal_sides_by_symbol={},
                    timestamp=timestamp,
                    entry_allowed_symbols=state.supervisor.opening_symbols_for(PodName.POD_A),
                    managed_symbols=state.supervisor.managed_symbols_for(
                        PodName.POD_A,
                        active_symbols=state.executor.portfolio.open_positions.keys(),
                    ),
                )
                record_tick(
                    helper=helper,
                    state=state,
                    previews=[],
                    risk_decisions=[],
                    execution=execution,
                    timestamp=timestamp,
                    source_file=record.source_file,
                )
            helper._process_maintenance_record(
                supervisor=pod_c_supervisor,
                pod_a_report=PodABacktestReport(),
                pod_b_report=PodABacktestReport(),
                pod_c_report=pod_c_report,
                snapshots=snapshots,
                timestamp=timestamp,
                source_file=record.source_file,
                stream_source=record.stream_source,
            )
            continue

        cluster_regime_snapshots = {
            cluster: RegimeSnapshot(**snap)
            for cluster, snap in (record.cluster_regime_snapshots or {}).items()
            if isinstance(snap, dict)
        }
        pod_c_previous_regime = pod_c_supervisor.state.regime.value
        pod_c_supervisor.apply_regime_snapshot(
            RegimeSnapshot(**record.regime_snapshot),
            cluster_regime_snapshots=cluster_regime_snapshots,
        )
        pod_c_current_regime = pod_c_supervisor.state.regime.value

        for state in states:
            process_state(
                helper=helper,
                state=state,
                snapshots=snapshots,
                record=record,
                cluster_regime_snapshots=cluster_regime_snapshots,
                timestamp=timestamp,
                apply_live_caps=apply_live_caps,
            )
        helper._process_pod_c(
            supervisor=pod_c_supervisor,
            report=pod_c_report,
            snapshots=snapshots,
            timestamp=timestamp,
            source_file=record.source_file,
            previous_regime=pod_c_previous_regime,
            current_regime=pod_c_current_regime,
        )
        records_processed += 1

    latest_snapshots = list(latest_snapshots_by_symbol.values())
    for state in states:
        helper._finalize_directional_report(
            supervisor=state.supervisor,
            report=state.report,
            executor=state.executor,
            latest_snapshots=latest_snapshots,
            last_timestamp=last_timestamp,
            closed_trade_recorder=lambda trade, risk_gate=state.risk_gate: risk_gate.record_closed_trade(
                symbol=str(getattr(trade, "symbol", "")),
                setup=getattr(trade, "setup", None),
                pnl_usd=getattr(trade, "pnl_usd", None),
                date_key=(trade.closed_at.isoformat()[:10] if trade.closed_at else "unknown"),
            ),
        )
        state.supervisor.flush_compact_logs()
    helper._finalize_directional_report(
        supervisor=pod_c_supervisor,
        report=pod_c_report,
        executor=helper.pod_c_executor,
        latest_snapshots=latest_snapshots,
        last_timestamp=last_timestamp,
    )
    pod_c_supervisor.flush_compact_logs()
    runtime_seconds = round(time.perf_counter() - started, 3)
    return [
        summarize_reports(
            window=window,
            state=state,
            pod_c_report=pod_c_report,
            records_processed=records_processed,
            first_timestamp=first_timestamp,
            last_timestamp=last_timestamp,
            runtime_seconds=runtime_seconds,
        )
        for state in states
    ]


def process_state(
    *,
    helper: FullBotBacktestRunner,
    state: ScenarioState,
    snapshots: list[SymbolMarketSnapshot],
    record: Any,
    cluster_regime_snapshots: dict[str, RegimeSnapshot],
    timestamp: str,
    apply_live_caps: bool,
) -> None:
    previous_regime = state.supervisor.state.regime.value
    state.supervisor.apply_regime_snapshot(
        RegimeSnapshot(**record.regime_snapshot),
        cluster_regime_snapshots=cluster_regime_snapshots,
    )
    current_regime = state.supervisor.state.regime.value
    helper._add_regime_record(
        report=state.report,
        timestamp=timestamp,
        source_file=record.source_file,
        previous_regime=previous_regime,
        current_regime=current_regime,
    )
    previews = state.supervisor.preview_pod_a_signals(snapshots, timestamp=timestamp)
    plans = state.supervisor.build_pod_a_trade_plans(snapshots, timestamp=timestamp)
    date_key = helper._date_key(timestamp, record.source_file)
    plans = [
        replace(
            plan,
            setup_details={
                **dict(plan.setup_details or {}),
                "current_date_key": date_key,
            },
        )
        for plan in plans
    ]
    if apply_live_caps:
        leverage_policy = LeveragePolicy(state.config.pod_a)
        plans = [
            apply_live_notional_cap(
                plan,
                state.config.trident.execution.live_max_order_notional_usd,
                max_leverage=leverage_policy.max_allowed(plan.symbol),
            )
            for plan in plans
        ]
    risk_decisions = state.risk_gate.evaluate_many(plans)
    execution = state.executor.process_record(
        snapshots=snapshots,
        risk_decisions=risk_decisions,
        signal_sides_by_symbol={preview.symbol: preview.side for preview in previews},
        timestamp=timestamp,
        entry_allowed_symbols=state.supervisor.opening_symbols_for(PodName.POD_A),
        managed_symbols=state.supervisor.managed_symbols_for(
            PodName.POD_A,
            active_symbols=state.executor.portfolio.open_positions.keys(),
        ),
    )
    record_tick(
        helper=helper,
        state=state,
        previews=previews,
        risk_decisions=risk_decisions,
        execution=execution,
        timestamp=timestamp,
        source_file=record.source_file,
    )


def record_tick(
    *,
    helper: FullBotBacktestRunner,
    state: ScenarioState,
    previews: list[Any],
    risk_decisions: list[RiskDecision],
    execution: Any,
    timestamp: str,
    source_file: str,
) -> None:
    helper._record_directional_tick(
        report=state.report,
        config=state.config,
        current_regime=state.supervisor.state.regime.value,
        timestamp=timestamp,
        source_file=source_file,
        previews=previews,
        risk_decisions=risk_decisions,
        execution=execution,
        executor=state.executor,
        closed_trade_recorder=lambda trade: state.risk_gate.record_closed_trade(
            symbol=str(getattr(trade, "symbol", "")),
            setup=getattr(trade, "setup", None),
            pnl_usd=getattr(trade, "pnl_usd", None),
            date_key=(trade.closed_at.isoformat()[:10] if trade.closed_at else "unknown"),
        ),
    )


def scenario_state(
    *,
    base_config: AppConfig,
    scenario: ScenarioSpec,
    window: WindowSpec,
    force_enable_all_pods: bool,
) -> ScenarioState:
    config = scenario_config(base_config, scenario)
    helper = FullBotBacktestRunner(
        config,
        force_enable_all_pods=force_enable_all_pods,
        apply_live_notional_caps=False,
    )
    return ScenarioState(
        spec=scenario,
        config=helper.config,
        supervisor=TridentSupervisor(
            config=helper.config,
            profile=f"p105-{safe_name(window.name)}-{safe_name(scenario.name)}",
            mode="dry-run",
        ),
        risk_gate=PodARiskGate(helper.config),
        executor=PodAExecutor(helper.config),
        report=PodABacktestReport(
            reference_equity_usd=helper.config.trident.capital.reference_equity_usd,
        ),
    )


def summarize_reports(
    *,
    window: WindowSpec,
    state: ScenarioState,
    pod_c_report: PodABacktestReport,
    records_processed: int,
    first_timestamp: str | None,
    last_timestamp: str | None,
    runtime_seconds: float,
) -> WindowScenarioResult:
    pod_a = state.report.to_dict()
    pod_c = pod_c_report.to_dict()
    pod_a_trades = trade_rows(pod_a)
    pod_c_trades = trade_rows(pod_c)
    grade = summarize_a_grade_trades(pod_a_trades)
    worst_symbol, worst_symbol_pnl = worst_negative_bucket(pod_a_trades, "symbol")
    worst_date, worst_date_pnl = worst_negative_bucket(pod_a_trades, "date")
    pod_a_pnl = float(pod_a.get("realized_pnl_usd", 0.0) or 0.0)
    pod_c_pnl = float(pod_c.get("realized_pnl_usd", 0.0) or 0.0)
    return WindowScenarioResult(
        window=window.name,
        scenario=state.spec.name,
        description=state.spec.description,
        standard_scale=round(float(state.spec.standard_scale), 4),
        strong_scale=round(float(state.spec.strong_scale), 4),
        headroom_cap_enabled=bool(state.spec.headroom_cap_enabled),
        records_processed=records_processed,
        duplicate_timestamps_skipped=0,
        first_timestamp=first_timestamp,
        last_timestamp=last_timestamp,
        runtime_seconds=runtime_seconds,
        total_ac_pnl_usd=round(pod_a_pnl + pod_c_pnl, 6),
        total_ac_trades=len(pod_a_trades) + len(pod_c_trades),
        directional_fees_usd=round(
            float(pod_a.get("fees_usd", 0.0) or 0.0)
            + float(pod_c.get("fees_usd", 0.0) or 0.0),
            6,
        ),
        pod_a_pnl_usd=round(pod_a_pnl, 6),
        pod_a_trades=len(pod_a_trades),
        pod_a_win_rate=win_rate(pod_a_trades),
        pod_a_profit_factor=profit_factor(pod_a_trades),
        pod_a_max_drawdown_usd=round(float(pod_a.get("max_drawdown_usd", 0.0) or 0.0), 6),
        pod_c_pnl_usd=round(pod_c_pnl, 6),
        pod_c_trades=len(pod_c_trades),
        a_grade_trades=int(grade["a_grade_trades"]),
        strong_a_grade_trades=int(grade["strong_a_grade_trades"]),
        standard_a_grade_trades=int(grade["standard_a_grade_trades"]),
        no_a_grade_trades=int(grade["no_a_grade_trades"]),
        strong_a_grade_pnl_usd=round(float(grade["strong_a_grade_pnl_usd"]), 6),
        standard_a_grade_pnl_usd=round(float(grade["standard_a_grade_pnl_usd"]), 6),
        no_a_grade_pnl_usd=round(float(grade["no_a_grade_pnl_usd"]), 6),
        avg_a_grade_size_scale=optional_round(grade["avg_a_grade_size_scale"]),
        avg_a_grade_requested_size_scale=optional_round(
            grade["avg_a_grade_requested_size_scale"]
        ),
        a_grade_headroom_capped_trades=int(grade["a_grade_headroom_capped_trades"]),
        live_quality_scaled_trades=int(grade["live_quality_scaled_trades"]),
        avg_live_quality_multiplier=optional_round(grade["avg_live_quality_multiplier"]),
        worst_symbol=worst_symbol,
        worst_symbol_pnl_usd=optional_round(worst_symbol_pnl),
        worst_date=worst_date,
        worst_date_pnl_usd=optional_round(worst_date_pnl),
        report_path=None,
        summary_path=None,
    )


def summarize_result(
    *,
    result: FullBotBacktestResult,
    scenario: ScenarioSpec,
    window: WindowSpec,
    runtime_seconds: float,
) -> WindowScenarioResult:
    pod_a = result.pod_a
    pod_c = result.pod_c
    pod_a_trades = trade_rows(pod_a)
    pod_c_trades = trade_rows(pod_c)
    grade = summarize_a_grade_trades(pod_a_trades)
    worst_symbol, worst_symbol_pnl = worst_negative_bucket(pod_a_trades, "symbol")
    worst_date, worst_date_pnl = worst_negative_bucket(pod_a_trades, "date")
    return WindowScenarioResult(
        window=window.name,
        scenario=scenario.name,
        description=scenario.description,
        standard_scale=round(float(scenario.standard_scale), 4),
        strong_scale=round(float(scenario.strong_scale), 4),
        headroom_cap_enabled=bool(scenario.headroom_cap_enabled),
        records_processed=int(result.records_processed),
        duplicate_timestamps_skipped=int(result.duplicate_timestamps_skipped),
        first_timestamp=result.first_timestamp,
        last_timestamp=result.last_timestamp,
        runtime_seconds=runtime_seconds,
        total_ac_pnl_usd=round(float(result.total_realized_pnl_usd), 6),
        total_ac_trades=len(pod_a_trades) + len(pod_c_trades),
        directional_fees_usd=round(float(result.directional_fees_usd), 6),
        pod_a_pnl_usd=round(float(pod_a.get("realized_pnl_usd", 0.0) or 0.0), 6),
        pod_a_trades=len(pod_a_trades),
        pod_a_win_rate=win_rate(pod_a_trades),
        pod_a_profit_factor=profit_factor(pod_a_trades),
        pod_a_max_drawdown_usd=round(float(pod_a.get("max_drawdown_usd", 0.0) or 0.0), 6),
        pod_c_pnl_usd=round(float(pod_c.get("realized_pnl_usd", 0.0) or 0.0), 6),
        pod_c_trades=len(pod_c_trades),
        a_grade_trades=int(grade["a_grade_trades"]),
        strong_a_grade_trades=int(grade["strong_a_grade_trades"]),
        standard_a_grade_trades=int(grade["standard_a_grade_trades"]),
        no_a_grade_trades=int(grade["no_a_grade_trades"]),
        strong_a_grade_pnl_usd=round(float(grade["strong_a_grade_pnl_usd"]), 6),
        standard_a_grade_pnl_usd=round(float(grade["standard_a_grade_pnl_usd"]), 6),
        no_a_grade_pnl_usd=round(float(grade["no_a_grade_pnl_usd"]), 6),
        avg_a_grade_size_scale=optional_round(grade["avg_a_grade_size_scale"]),
        avg_a_grade_requested_size_scale=optional_round(
            grade["avg_a_grade_requested_size_scale"]
        ),
        a_grade_headroom_capped_trades=int(grade["a_grade_headroom_capped_trades"]),
        live_quality_scaled_trades=int(grade["live_quality_scaled_trades"]),
        avg_live_quality_multiplier=optional_round(grade["avg_live_quality_multiplier"]),
        worst_symbol=worst_symbol,
        worst_symbol_pnl_usd=optional_round(worst_symbol_pnl),
        worst_date=worst_date,
        worst_date_pnl_usd=optional_round(worst_date_pnl),
        report_path=result.report_path,
        summary_path=result.summary_path,
    )


def summarize_a_grade_trades(trades: Iterable[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter()
    pnl = defaultdict(float)
    grade_scales: list[float] = []
    requested_grade_scales: list[float] = []
    quality_multipliers: list[float] = []
    for trade in trades:
        details = setup_details(trade)
        trade_pnl = money(trade.get("pnl_usd"))
        if bool(details.get("a_grade_active", False)):
            counts["a_grade_trades"] += 1
            level = str(details.get("a_grade_level") or "unknown")
            if level == "strong":
                counts["strong_a_grade_trades"] += 1
                pnl["strong_a_grade_pnl_usd"] += trade_pnl
            elif level == "standard":
                counts["standard_a_grade_trades"] += 1
                pnl["standard_a_grade_pnl_usd"] += trade_pnl
            else:
                counts["unknown_a_grade_trades"] += 1
            scale = optional_float(details.get("a_grade_size_scale"))
            if scale is not None:
                grade_scales.append(scale)
            requested_scale = optional_float(details.get("a_grade_requested_size_scale"))
            if requested_scale is not None:
                requested_grade_scales.append(requested_scale)
            if bool(details.get("a_grade_size_headroom_cap_active", False)):
                counts["a_grade_headroom_capped_trades"] += 1
        else:
            counts["no_a_grade_trades"] += 1
            pnl["no_a_grade_pnl_usd"] += trade_pnl
        if bool(details.get("live_quality_sizing_active", False)):
            counts["live_quality_scaled_trades"] += 1
            multiplier = optional_float(details.get("live_quality_sizing_multiplier"))
            if multiplier is not None:
                quality_multipliers.append(multiplier)
    return {
        "a_grade_trades": counts["a_grade_trades"],
        "strong_a_grade_trades": counts["strong_a_grade_trades"],
        "standard_a_grade_trades": counts["standard_a_grade_trades"],
        "no_a_grade_trades": counts["no_a_grade_trades"],
        "live_quality_scaled_trades": counts["live_quality_scaled_trades"],
        "strong_a_grade_pnl_usd": pnl["strong_a_grade_pnl_usd"],
        "standard_a_grade_pnl_usd": pnl["standard_a_grade_pnl_usd"],
        "no_a_grade_pnl_usd": pnl["no_a_grade_pnl_usd"],
        "avg_a_grade_size_scale": average(grade_scales),
        "avg_a_grade_requested_size_scale": average(requested_grade_scales),
        "a_grade_headroom_capped_trades": counts["a_grade_headroom_capped_trades"],
        "avg_live_quality_multiplier": average(quality_multipliers),
    }


def trade_rows(report: dict[str, object]) -> list[dict[str, Any]]:
    rows = report.get("closed_trade_log") or []
    return [row for row in rows if isinstance(row, dict)]


def setup_details(trade: dict[str, Any]) -> dict[str, Any]:
    details = trade.get("setup_details") or {}
    return details if isinstance(details, dict) else {}


def win_rate(trades: list[dict[str, Any]]) -> float | None:
    if not trades:
        return None
    wins = sum(1 for trade in trades if money(trade.get("pnl_usd")) >= 0.0)
    return round(wins / len(trades), 4)


def profit_factor(trades: list[dict[str, Any]]) -> float | None:
    gross_profit = sum(max(money(trade.get("pnl_usd")), 0.0) for trade in trades)
    gross_loss = abs(sum(min(money(trade.get("pnl_usd")), 0.0) for trade in trades))
    if gross_loss <= 0.0:
        return None
    return round(gross_profit / gross_loss, 4)


def worst_negative_bucket(trades: Iterable[dict[str, Any]], key: str) -> tuple[str | None, float | None]:
    values: dict[str, float] = defaultdict(float)
    for trade in trades:
        bucket = str(trade.get(key) or "")
        if not bucket and key == "date":
            bucket = str(trade.get("closed_at") or "")[:10]
        if not bucket:
            bucket = "unknown"
        values[bucket] += money(trade.get("pnl_usd"))
    if not values:
        return None, None
    bucket, value = min(values.items(), key=lambda item: item[1])
    return bucket, round(value, 6)


def prepare_snapshot_window(
    *,
    snapshots_dir: Path,
    output_dir: Path,
    name: str,
    start: datetime,
    end: datetime,
) -> tuple[Path, list[dict[str, object]]]:
    input_dir = output_dir / f"input_{safe_name(name)}"
    input_dir.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, object]] = []
    for source in sorted(snapshots_dir.glob("*.jsonl")):
        try:
            file_date = datetime.fromisoformat(source.stem).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if not (start.date() <= file_date.date() < end.date()):
            continue
        target = input_dir / source.name
        if not target.exists():
            target.symlink_to(source.resolve())
        files.append({"name": source.name, "source": str(source), "line_count": count_lines(source)})
    if not files:
        raise FileNotFoundError(
            f"no snapshot JSONL files in {snapshots_dir} for {isoformat(start)} -> {isoformat(end)}"
        )
    return input_dir, files


def write_results_csv(path: Path, rows: list[WindowScenarioResult]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = list(asdict(rows[0]).keys()) if rows else []
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if rows:
            writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def write_report(path: Path, *, rows: list[WindowScenarioResult], generated_at: str) -> None:
    by_window: dict[str, list[WindowScenarioResult]] = {}
    for row in rows:
        by_window.setdefault(row.window, []).append(row)
    lines = [
        "# P1-05 A-grade / quality sizing replay",
        "",
        f"- generated_at: `{generated_at}`",
        "- status: `research_only_no_live_change`",
        "- note: `Les scénarios changent uniquement les scales/caps A-grade Pod A; aucune config live n'est modifiée.`",
        "",
    ]
    for window, window_rows in by_window.items():
        current = next(row for row in window_rows if row.scenario == "current")
        lines.extend(
            [
                f"## {window}",
                "",
                (
                    "| Scenario | Std | Strong | Total A/C | Delta | Pod A | Trades A | "
                    "WR | PF | DD | Strong trades | Strong PnL | Headroom capped | Quality scaled | Worst symbol |"
                ),
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
            ]
        )
        for row in window_rows:
            lines.append(
                f"| `{row.scenario}` | {row.standard_scale:.2f} | {row.strong_scale:.2f} | "
                f"{row.total_ac_pnl_usd:.2f} | {row.total_ac_pnl_usd - current.total_ac_pnl_usd:+.2f} | "
                f"{row.pod_a_pnl_usd:.2f} | {row.pod_a_trades} | {fmt_optional(row.pod_a_win_rate)} | "
                f"{fmt_optional(row.pod_a_profit_factor)} | {row.pod_a_max_drawdown_usd:.2f} | "
                f"{row.strong_a_grade_trades} | {row.strong_a_grade_pnl_usd:.2f} | "
                f"{row.a_grade_headroom_capped_trades} | {row.live_quality_scaled_trades} | "
                f"`{row.worst_symbol or ''}` {fmt_money(row.worst_symbol_pnl_usd)} |"
            )
        lines.extend(["", "### Lecture rapide", ""])
        lines.extend(decision_notes(window_rows))
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def decision_notes(rows: list[WindowScenarioResult]) -> list[str]:
    current = next(row for row in rows if row.scenario == "current")
    notes: list[str] = []
    for scenario_name in (
        "headroom_cap_current",
        "strong_frozen_1p00",
        "flat_scale_1p00",
        "flat_scale_1p25",
        "flat_scale_1p40",
    ):
        row = next((candidate for candidate in rows if candidate.scenario == scenario_name), None)
        if row is None:
            continue
        notes.append(
            f"- `{scenario_name}`: delta total A/C {row.total_ac_pnl_usd - current.total_ac_pnl_usd:+.2f}, "
            f"delta Pod A {row.pod_a_pnl_usd - current.pod_a_pnl_usd:+.2f}, "
            f"DD {row.pod_a_max_drawdown_usd:.2f} vs {current.pod_a_max_drawdown_usd:.2f}, "
            f"headroom capped {row.a_grade_headroom_capped_trades} trades."
        )
    return notes


def parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in value)


def count_lines(path: Path) -> int:
    count = 0
    with path.open("rb") as handle:
        for _ in handle:
            count += 1
    return count


def money(value: object) -> float:
    return float(optional_float(value) or 0.0)


def optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def average(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 6)


def optional_round(value: object) -> float | None:
    numeric = optional_float(value)
    return round(numeric, 6) if numeric is not None else None


def fmt_optional(value: float | None) -> str:
    return "" if value is None else f"{value:.4f}"


def fmt_money(value: float | None) -> str:
    return "" if value is None else f"{value:.2f}"


if __name__ == "__main__":
    main()
