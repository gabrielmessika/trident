#!/usr/bin/env python3
"""P1-15 / A-PNL-05 microstructure entry score replay for Pod A.

The script keeps live untouched. It replays the current full-bot A/C stack and
tests only counterfactual Pod A notional reductions on weak microstructure
entry-score buckets.
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

from app.backtest.full_bot_replay import FullBotBacktestRunner
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
    SignalPreview,
    SymbolMarketSnapshot,
    TradePlan,
    symbol_market_snapshot_from_mapping,
)
from scripts.run_p105_a_grade_replay import (
    isoformat,
    parse_timestamp,
    prepare_snapshot_window,
    safe_name,
    selected_names,
)


DEFAULT_BASELINE_INPUT = (
    "server-data/replay_inputs/"
    "external_reference_multisource_20260405_20260513_baseline.jsonl"
)
DEFAULT_LIVE_INPUT = "server-data/live_snapshots"
DEFAULT_LIVE_START = "2026-05-14T00:00:00Z"
DEFAULT_LIVE_END = "2026-06-23T00:00:00Z"


@dataclass(frozen=True, slots=True)
class ScenarioSpec:
    name: str
    description: str
    score_threshold: float = 0.0
    cap_multiplier: float = 1.0


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
    micro_cap_reduction_count: int = 0


@dataclass(slots=True)
class WindowScenarioResult:
    window: str
    scenario: str
    description: str
    score_threshold: float
    cap_multiplier: float
    records_processed: int
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
    micro_cap_reduction_count: int
    micro_cap_closed_trades: int
    avg_microstructure_score: float | None
    poor_trades: int
    poor_pnl_usd: float
    weak_trades: int
    weak_pnl_usd: float
    ok_trades: int
    ok_pnl_usd: float
    strong_trades: int
    strong_pnl_usd: float
    missing_score_trades: int
    worst_micro_bucket: str | None
    worst_micro_bucket_pnl_usd: float | None


@dataclass(slots=True)
class MicroBucketResult:
    window: str
    scenario: str
    bucket: str
    closed_trades: int
    pnl_usd: float
    win_rate: float | None
    profit_factor: float | None
    avg_score: float | None
    cap_reduced_trades: int


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


def default_scenarios() -> list[ScenarioSpec]:
    return [
        ScenarioSpec(
            "current",
            "Config courante A/C avec score microstructure en shadow seulement.",
        ),
        ScenarioSpec(
            "micro_cap_poor50_lt42",
            "Counterfactual: score microstructure <0.42 réduit le notional Pod A de 50%, aucun blocage.",
            score_threshold=0.42,
            cap_multiplier=0.50,
        ),
        ScenarioSpec(
            "micro_cap_weak50_lt56",
            "Counterfactual: score microstructure <0.56 réduit le notional Pod A de 50%, aucun blocage.",
            score_threshold=0.56,
            cap_multiplier=0.50,
        ),
    ]


def main() -> None:
    args = parse_args()
    generated_at = utc_stamp()
    output_dir = Path(
        args.output_dir
        or f"server-data/replay_reports/p115_microstructure_entry_{generated_at}"
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

    scenarios = selected_scenarios(default_scenarios(), args.scenarios)
    scenario_rows: list[WindowScenarioResult] = []
    bucket_rows: list[MicroBucketResult] = []
    for window in windows:
        print(f"window={window.name} status=running", flush=True)
        rows, buckets = run_window(
            base_config=config,
            window=window,
            scenarios=scenarios,
            apply_live_caps=not args.no_live_caps,
            force_enable_all_pods=not args.respect_config_enabled,
        )
        scenario_rows.extend(rows)
        bucket_rows.extend(buckets)
        current = next(row for row in rows if row.scenario == "current")
        for row in rows:
            print(
                f"window={window.name} scenario={row.scenario} status=done "
                f"total={row.total_ac_pnl_usd:.2f} "
                f"delta_vs_current={row.total_ac_pnl_usd - current.total_ac_pnl_usd:+.2f} "
                f"pod_a={row.pod_a_pnl_usd:.2f} trades={row.total_ac_trades} "
                f"micro_plan_caps={row.micro_cap_reduction_count} "
                f"micro_closed_caps={row.micro_cap_closed_trades}",
                flush=True,
            )

    write_csv(output_dir / "scenario_summary.csv", scenario_rows)
    write_csv(output_dir / "microstructure_bucket_summary.csv", bucket_rows)
    payload = {
        "generated_at": generated_at,
        "status": "research_only_no_live_change",
        "live_caps": not args.no_live_caps,
        "config": args.config,
        "live_input_files": live_input_files,
        "windows": [asdict(window) | {"input_path": str(window.input_path)} for window in windows],
        "scenarios": [asdict(scenario) for scenario in scenarios],
        "results": [asdict(row) for row in scenario_rows],
        "bucket_results": [asdict(row) for row in bucket_rows],
    }
    (output_dir / "p115_microstructure_entry_replay.json").write_text(
        json.dumps(payload, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    write_report(
        output_dir / "p115_microstructure_entry_replay.md",
        rows=scenario_rows,
        bucket_rows=bucket_rows,
        generated_at=generated_at,
    )
    print(output_dir)


def selected_scenarios(scenarios: list[ScenarioSpec], raw: str) -> list[ScenarioSpec]:
    if not str(raw or "").strip():
        return scenarios
    by_name = {scenario.name: scenario for scenario in scenarios}
    names = [item.strip() for item in str(raw).split(",") if item.strip()]
    unknown = [name for name in names if name not in by_name]
    if unknown:
        raise ValueError(f"unknown scenario(s): {', '.join(unknown)}")
    return [by_name[name] for name in names]


def run_window(
    *,
    base_config: AppConfig,
    window: WindowSpec,
    scenarios: list[ScenarioSpec],
    apply_live_caps: bool,
    force_enable_all_pods: bool,
) -> tuple[list[WindowScenarioResult], list[MicroBucketResult]]:
    helper = FullBotBacktestRunner(
        base_config,
        force_enable_all_pods=force_enable_all_pods,
        apply_live_notional_caps=apply_live_caps,
    )
    pod_c_supervisor = TridentSupervisor(
        config=helper.config,
        profile=f"p115-pod-c-{window.name}",
        mode="dry-run",
    )
    pod_a_supervisor = TridentSupervisor(
        config=helper.config,
        profile=f"p115-pod-a-shared-{window.name}",
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
                    entry_allowed_symbols=pod_a_supervisor.opening_symbols_for(PodName.POD_A),
                    managed_symbols=pod_a_supervisor.managed_symbols_for(
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
                    current_regime=pod_a_supervisor.state.regime.value,
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
        pod_a_previous_regime = pod_a_supervisor.state.regime.value
        pod_a_supervisor.apply_regime_snapshot(
            RegimeSnapshot(**record.regime_snapshot),
            cluster_regime_snapshots=cluster_regime_snapshots,
        )
        pod_a_current_regime = pod_a_supervisor.state.regime.value
        previews, plans = build_pod_a_previews_and_plans(
            pod_a_supervisor,
            snapshots,
            timestamp=timestamp,
        )

        for state in states:
            process_state(
                helper=helper,
                state=state,
                supervisor=pod_a_supervisor,
                snapshots=snapshots,
                previews=previews,
                plans=plans,
                record=record,
                previous_regime=pod_a_previous_regime,
                current_regime=pod_a_current_regime,
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
            supervisor=pod_a_supervisor,
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
    pod_a_supervisor.flush_compact_logs()
    helper._finalize_directional_report(
        supervisor=pod_c_supervisor,
        report=pod_c_report,
        executor=helper.pod_c_executor,
        latest_snapshots=latest_snapshots,
        last_timestamp=last_timestamp,
    )
    pod_c_supervisor.flush_compact_logs()
    runtime_seconds = round(time.perf_counter() - started, 3)
    scenario_rows = [
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
    bucket_rows = [
        row
        for state in states
        for row in summarize_microstructure_buckets(
            window=window.name,
            scenario=state.spec.name,
            trades=trade_rows(state.report.to_dict()),
        )
    ]
    return scenario_rows, bucket_rows


def build_pod_a_previews_and_plans(
    supervisor: TridentSupervisor,
    snapshots: list[SymbolMarketSnapshot],
    *,
    timestamp: str | None,
) -> tuple[list[SignalPreview], list[TradePlan]]:
    prepared = supervisor._prepare_snapshots(snapshots)
    supervisor.refresh_symbol_routing(prepared)
    contexts = supervisor.pod_a_context_service.build_contexts(
        supervisor.state.regime,
        supervisor._owned_snapshots(PodName.POD_A, prepared),
        timestamp=timestamp,
    )
    signals = supervisor.pod_a_service.evaluate_many(contexts)
    previews = [supervisor._build_signal_preview(signal) for signal in signals]
    signal_by_symbol = {signal.symbol: signal for signal in signals}
    supervisor.state.pod_a_signal_review = [
        supervisor._build_signal_review(
            supervisor._build_signal_preview(signal_by_symbol[context.symbol])
            if context.symbol in signal_by_symbol
            else supervisor.pod_a_service.review_context(context)
        )
        for context in contexts
    ]
    supervisor.state.pod_a_signal_preview = previews
    pod_allocation = supervisor._pod_a_planning_allocation(signals)
    plans: list[TradePlan] = []
    for signal in signals:
        plan = supervisor.pod_a_planner.build_trade_plan(signal, pod_allocation)
        if plan is not None:
            plans.append(plan)
    return previews, plans


def process_state(
    *,
    helper: FullBotBacktestRunner,
    state: ScenarioState,
    supervisor: TridentSupervisor,
    snapshots: list[SymbolMarketSnapshot],
    previews: list[Any],
    plans: list[TradePlan],
    record: Any,
    previous_regime: str,
    current_regime: str,
    timestamp: str,
    apply_live_caps: bool,
) -> None:
    helper._add_regime_record(
        report=state.report,
        timestamp=timestamp,
        source_file=record.source_file,
        previous_regime=previous_regime,
        current_regime=current_regime,
    )
    date_key = helper._date_key(timestamp, record.source_file)
    adjusted_plans: list[TradePlan] = []
    for plan in plans:
        plan = replace(
            plan,
            setup_details={
                **dict(plan.setup_details or {}),
                "current_date_key": date_key,
            },
        )
        plan, capped = apply_microstructure_cap(plan, state.spec)
        if capped:
            state.micro_cap_reduction_count += 1
        adjusted_plans.append(plan)
    if apply_live_caps:
        leverage_policy = LeveragePolicy(state.config.pod_a)
        adjusted_plans = [
            apply_live_notional_cap(
                plan,
                state.config.trident.execution.live_max_order_notional_usd,
                max_leverage=leverage_policy.max_allowed(plan.symbol),
            )
            for plan in adjusted_plans
        ]
    risk_decisions = state.risk_gate.evaluate_many(adjusted_plans)
    execution = state.executor.process_record(
        snapshots=snapshots,
        risk_decisions=risk_decisions,
        signal_sides_by_symbol={preview.symbol: preview.side for preview in previews},
        timestamp=timestamp,
        entry_allowed_symbols=supervisor.opening_symbols_for(PodName.POD_A),
        managed_symbols=supervisor.managed_symbols_for(
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
        current_regime=current_regime,
    )


def apply_microstructure_cap(
    plan: TradePlan,
    spec: ScenarioSpec,
) -> tuple[TradePlan, bool]:
    if spec.cap_multiplier >= 1.0 or spec.score_threshold <= 0.0:
        return plan, False
    details = dict(plan.setup_details or {})
    score = optional_float(details.get("microstructure_shadow_score"))
    if score is None or score >= spec.score_threshold:
        return plan, False
    multiplier = max(0.0, min(float(spec.cap_multiplier), 1.0))
    return (
        replace(
            plan,
            target_notional_usd=round(float(plan.target_notional_usd) * multiplier, 6),
            margin_usd=round(float(plan.margin_usd) * multiplier, 6),
            risk_budget_usd=round(float(plan.risk_budget_usd) * multiplier, 6),
            expected_loss_usd=round(float(plan.expected_loss_usd) * multiplier, 6),
            setup_details={
                **details,
                "p115_microstructure_cap_counterfactual": True,
                "p115_microstructure_cap_multiplier": round(multiplier, 4),
                "p115_microstructure_score_threshold": round(float(spec.score_threshold), 4),
                "p115_microstructure_cap_reason": (
                    f"score_lt_{str(round(float(spec.score_threshold), 4)).replace('.', 'p')}"
                ),
                "p115_microstructure_original_target_notional_usd": round(
                    float(plan.target_notional_usd),
                    6,
                ),
                "p115_microstructure_original_margin_usd": round(float(plan.margin_usd), 6),
                "p115_microstructure_original_risk_budget_usd": round(
                    float(plan.risk_budget_usd),
                    6,
                ),
                "p115_microstructure_original_expected_loss_usd": round(
                    float(plan.expected_loss_usd),
                    6,
                ),
            },
        ),
        True,
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
    current_regime: str,
) -> None:
    helper._record_directional_tick(
        report=state.report,
        config=state.config,
        current_regime=current_regime,
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
    helper = FullBotBacktestRunner(
        base_config,
        force_enable_all_pods=force_enable_all_pods,
        apply_live_notional_caps=False,
    )
    return ScenarioState(
        spec=scenario,
        config=helper.config,
        supervisor=TridentSupervisor(
            config=helper.config,
            profile=f"p115-{safe_name(window.name)}-{safe_name(scenario.name)}",
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
    micro = summarize_microstructure_trades(pod_a_trades)
    pod_a_pnl = money(pod_a.get("realized_pnl_usd"))
    pod_c_pnl = money(pod_c.get("realized_pnl_usd"))
    return WindowScenarioResult(
        window=window.name,
        scenario=state.spec.name,
        description=state.spec.description,
        score_threshold=round(float(state.spec.score_threshold), 4),
        cap_multiplier=round(float(state.spec.cap_multiplier), 4),
        records_processed=records_processed,
        first_timestamp=first_timestamp,
        last_timestamp=last_timestamp,
        runtime_seconds=runtime_seconds,
        total_ac_pnl_usd=round(pod_a_pnl + pod_c_pnl, 6),
        total_ac_trades=len(pod_a_trades) + len(pod_c_trades),
        directional_fees_usd=round(
            money(pod_a.get("fees_usd")) + money(pod_c.get("fees_usd")),
            6,
        ),
        pod_a_pnl_usd=round(pod_a_pnl, 6),
        pod_a_trades=len(pod_a_trades),
        pod_a_win_rate=win_rate(pod_a_trades),
        pod_a_profit_factor=profit_factor(pod_a_trades),
        pod_a_max_drawdown_usd=round(money(pod_a.get("max_drawdown_usd")), 6),
        pod_c_pnl_usd=round(pod_c_pnl, 6),
        pod_c_trades=len(pod_c_trades),
        micro_cap_reduction_count=state.micro_cap_reduction_count,
        micro_cap_closed_trades=int(micro["cap_reduced_closed_trades"]),
        avg_microstructure_score=optional_round(micro["avg_score"]),
        poor_trades=int(micro["poor_trades"]),
        poor_pnl_usd=round(float(micro["poor_pnl_usd"]), 6),
        weak_trades=int(micro["weak_trades"]),
        weak_pnl_usd=round(float(micro["weak_pnl_usd"]), 6),
        ok_trades=int(micro["ok_trades"]),
        ok_pnl_usd=round(float(micro["ok_pnl_usd"]), 6),
        strong_trades=int(micro["strong_trades"]),
        strong_pnl_usd=round(float(micro["strong_pnl_usd"]), 6),
        missing_score_trades=int(micro["missing_score_trades"]),
        worst_micro_bucket=micro["worst_micro_bucket"],
        worst_micro_bucket_pnl_usd=optional_round(micro["worst_micro_bucket_pnl_usd"]),
    )


def summarize_microstructure_trades(trades: Iterable[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter()
    pnl = defaultdict(float)
    scores: list[float] = []
    bucket_totals: dict[str, float] = defaultdict(float)
    for trade in trades:
        details = setup_details(trade)
        if bool(details.get("p115_microstructure_cap_counterfactual", False)):
            counts["cap_reduced_closed_trades"] += 1
        score = optional_float(details.get("microstructure_shadow_score"))
        bucket = str(details.get("microstructure_shadow_bucket") or "")
        trade_pnl = money(trade.get("pnl_usd"))
        if score is None or not bucket:
            counts["missing_score_trades"] += 1
            continue
        scores.append(score)
        counts[f"{bucket}_trades"] += 1
        pnl[f"{bucket}_pnl_usd"] += trade_pnl
        bucket_totals[bucket] += trade_pnl
    worst_bucket: str | None = None
    worst_pnl: float | None = None
    if bucket_totals:
        worst_bucket, worst_pnl = min(bucket_totals.items(), key=lambda item: item[1])
    return {
        "avg_score": average(scores),
        "poor_trades": counts["poor_trades"],
        "poor_pnl_usd": pnl["poor_pnl_usd"],
        "weak_trades": counts["weak_trades"],
        "weak_pnl_usd": pnl["weak_pnl_usd"],
        "ok_trades": counts["ok_trades"],
        "ok_pnl_usd": pnl["ok_pnl_usd"],
        "strong_trades": counts["strong_trades"],
        "strong_pnl_usd": pnl["strong_pnl_usd"],
        "missing_score_trades": counts["missing_score_trades"],
        "cap_reduced_closed_trades": counts["cap_reduced_closed_trades"],
        "worst_micro_bucket": worst_bucket,
        "worst_micro_bucket_pnl_usd": worst_pnl,
    }


def summarize_microstructure_buckets(
    *,
    window: str,
    scenario: str,
    trades: Iterable[dict[str, Any]],
) -> list[MicroBucketResult]:
    by_bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        details = setup_details(trade)
        bucket = str(details.get("microstructure_shadow_bucket") or "missing")
        by_bucket[bucket].append(trade)
    rows: list[MicroBucketResult] = []
    for bucket in sorted(by_bucket, key=bucket_sort_key):
        bucket_trades = by_bucket[bucket]
        scores = [
            score
            for score in (
                optional_float(setup_details(trade).get("microstructure_shadow_score"))
                for trade in bucket_trades
            )
            if score is not None
        ]
        rows.append(
            MicroBucketResult(
                window=window,
                scenario=scenario,
                bucket=bucket,
                closed_trades=len(bucket_trades),
                pnl_usd=round(sum(money(trade.get("pnl_usd")) for trade in bucket_trades), 6),
                win_rate=win_rate(bucket_trades),
                profit_factor=profit_factor(bucket_trades),
                avg_score=optional_round(average(scores)),
                cap_reduced_trades=sum(
                    1
                    for trade in bucket_trades
                    if bool(
                        setup_details(trade).get(
                            "p115_microstructure_cap_counterfactual",
                            False,
                        )
                    )
                ),
            )
        )
    return rows


def trade_rows(report: dict[str, object]) -> list[dict[str, Any]]:
    rows = report.get("closed_trade_log") or []
    return [row for row in rows if isinstance(row, dict)]


def setup_details(trade: dict[str, Any]) -> dict[str, Any]:
    details = trade.get("setup_details") or {}
    return details if isinstance(details, dict) else {}


def write_csv(path: Path, rows: list[Any]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = list(asdict(rows[0]).keys()) if rows else []
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if rows:
            writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def write_report(
    path: Path,
    *,
    rows: list[WindowScenarioResult],
    bucket_rows: list[MicroBucketResult],
    generated_at: str,
) -> None:
    by_window: dict[str, list[WindowScenarioResult]] = {}
    for row in rows:
        by_window.setdefault(row.window, []).append(row)
    lines = [
        "# P1-15 microstructure entry replay",
        "",
        f"- generated_at: `{generated_at}`",
        "- status: `research_only_no_live_change`",
        "- note: `Les scénarios cap-only sont counterfactual; aucun blocage ni activation live.`",
        "",
    ]
    for window, window_rows in by_window.items():
        current = next(row for row in window_rows if row.scenario == "current")
        lines.extend(
            [
                f"## {window}",
                "",
                (
                    "| Scenario | Threshold | Cap | Total A/C | Delta | Pod A | Trades A | "
                    "WR | PF | DD | Avg score | Poor PnL | Weak PnL | Plan caps | Closed caps | Worst bucket |"
                ),
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
            ]
        )
        for row in window_rows:
            lines.append(
                f"| `{row.scenario}` | {row.score_threshold:.2f} | {row.cap_multiplier:.2f} | "
                f"{row.total_ac_pnl_usd:.2f} | {row.total_ac_pnl_usd - current.total_ac_pnl_usd:+.2f} | "
                f"{row.pod_a_pnl_usd:.2f} | {row.pod_a_trades} | {fmt_optional(row.pod_a_win_rate)} | "
                f"{fmt_optional(row.pod_a_profit_factor)} | {row.pod_a_max_drawdown_usd:.2f} | "
                f"{fmt_optional(row.avg_microstructure_score)} | {row.poor_pnl_usd:.2f} | "
                f"{row.weak_pnl_usd:.2f} | {row.micro_cap_reduction_count} | "
                f"{row.micro_cap_closed_trades} | "
                f"`{row.worst_micro_bucket or ''}` {fmt_money(row.worst_micro_bucket_pnl_usd)} |"
            )
        window_buckets = [row for row in bucket_rows if row.window == window]
        if window_buckets:
            lines.extend(["", "### Buckets microstructure", ""])
            lines.extend(
                [
                    "| Scenario | Bucket | Trades | PnL | WR | PF | Avg score | Cap reduced |",
                    "|---|---|---:|---:|---:|---:|---:|---:|",
                ]
            )
            for bucket_row in window_buckets:
                lines.append(
                    f"| `{bucket_row.scenario}` | `{bucket_row.bucket}` | {bucket_row.closed_trades} | "
                    f"{bucket_row.pnl_usd:.2f} | {fmt_optional(bucket_row.win_rate)} | "
                    f"{fmt_optional(bucket_row.profit_factor)} | {fmt_optional(bucket_row.avg_score)} | "
                    f"{bucket_row.cap_reduced_trades} |"
                )
        lines.extend(["", "### Lecture rapide", ""])
        for row in window_rows:
            if row.scenario == "current":
                continue
            lines.append(
                f"- `{row.scenario}`: delta total A/C {row.total_ac_pnl_usd - current.total_ac_pnl_usd:+.2f}, "
                f"delta Pod A {row.pod_a_pnl_usd - current.pod_a_pnl_usd:+.2f}, "
                f"DD {row.pod_a_max_drawdown_usd:.2f} vs {current.pod_a_max_drawdown_usd:.2f}, "
                f"plan caps {row.micro_cap_reduction_count}, closed caps {row.micro_cap_closed_trades}."
            )
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def bucket_sort_key(bucket: str) -> tuple[int, str]:
    order = {"poor": 0, "weak": 1, "ok": 2, "strong": 3, "missing": 4}
    return order.get(bucket, 99), bucket


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


def average(values: Iterable[float]) -> float | None:
    data = list(values)
    if not data:
        return None
    return sum(data) / len(data)


def optional_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed


def optional_round(value: Any, digits: int = 6) -> float | None:
    parsed = optional_float(value)
    return round(parsed, digits) if parsed is not None else None


def money(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def fmt_optional(value: float | None) -> str:
    return "" if value is None else f"{value:.4f}"


def fmt_money(value: float | None) -> str:
    return "" if value is None else f"{value:.2f}"


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


if __name__ == "__main__":
    main()
