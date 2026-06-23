#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.backtest.full_bot_replay import FullBotBacktestRunner
from app.backtest.pod_a_executor import PodAExecutor
from app.backtest.pod_report import PodABacktestReport
from app.execution.live_cap import apply_live_notional_cap
from app.risk.pod_a_gate import PodARiskGate
from app.settings import AppConfig, load_config
from app.trident.pod_a.dynamic_symbol_guard import PodADynamicSymbolGuard
from app.trident.pod_a.leverage import LeveragePolicy
from app.trident.pod_a.order_block_shadow import PodAOrderBlockShadowTracker
from app.trident.pod_a.regime_shadow import PodARegimeShadowTracker, regime_snapshot_mapping
from app.trident.supervisor import TridentSupervisor
from app.trident.types import (
    PodName,
    RegimeSnapshot,
    RiskDecision,
    SymbolMarketSnapshot,
    TradePlan,
    symbol_market_snapshot_from_mapping,
)


DEFAULT_BASELINE_INPUT = "server-data/replay_inputs/external_reference_multisource_20260405_20260513_baseline.jsonl"
DEFAULT_LIVE_INPUT = "server-data/live_snapshots"


@dataclass(frozen=True, slots=True)
class WindowSpec:
    name: str
    input_path: Path
    start: datetime | None
    end: datetime | None


@dataclass(frozen=True, slots=True)
class ScenarioSpec:
    name: str
    description: str
    throttle_score: float = 55.0
    quarantine_score: float = 75.0
    action: str = "shadow_only"
    throttle_multiplier: float = 0.50
    quarantine_multiplier: float = 0.50
    recovery_base_multiplier: float = 0.70
    recovery_partial_multiplier: float = 0.85
    recovery_min_closed_trades: int = 4
    recovery_min_profit_factor: float = 1.05
    recovery_min_expectancy_usd: float = 0.0
    loss_probation_multiplier: float = 0.50
    loss_probation_min_closed_trades: int = 2
    loss_probation_max_pnl_usd: float = -2.0
    loss_probation_max_profit_factor: float = 0.80
    loss_probation_rehab_min_profit_factor: float = 1.05
    loss_probation_rehab_min_expectancy_usd: float = 0.0


@dataclass(slots=True)
class ScenarioState:
    spec: ScenarioSpec
    guard: PodADynamicSymbolGuard
    risk_gate: PodARiskGate
    executor: PodAExecutor
    report: PodABacktestReport
    rejections: Counter[str]
    throttle_count: int = 0
    quarantine_count: int = 0
    cap_reduction_count: int = 0


@dataclass(slots=True)
class WindowResult:
    window: str
    scenario: str
    description: str
    records_processed: int
    first_timestamp: str | None
    last_timestamp: str | None
    runtime_seconds: float
    pod_a_pnl_usd: float
    pod_a_trades: int
    pod_a_win_rate: float | None
    pod_a_profit_factor: float | None
    pod_a_max_drawdown_usd: float
    pod_a_accepted_count: int
    pod_a_rejected_count: int
    pod_a_guard_rejections: dict[str, int]
    guard_throttle_count: int
    guard_quarantine_count: int
    guard_cap_reduction_count: int
    pod_c_pnl_usd: float
    pod_c_trades: int
    total_ac_pnl_usd: float
    total_ac_trades: int


def default_scenarios() -> list[ScenarioSpec]:
    return [
        ScenarioSpec(
            name="current_ac",
            description="Config courante A/C, guard P1-08 en shadow seulement.",
        ),
        ScenarioSpec(
            name="throttle_only_55_cap50",
            description="Counterfactual: score >=55 réduit le notional Pod A de 50%, aucun blocage.",
            action="throttle_only",
        ),
        ScenarioSpec(
            name="live_sizing_55_75_cap50_cap50",
            description=(
                "Counterfactual policy live candidate: throttle/quarantine "
                "reduisent le notional de 50%, aucun blocage."
            ),
            action="live_sizing_policy",
        ),
        ScenarioSpec(
            name="live_sizing_55_75_cap50_cap10_rejected",
            description=(
                "Counterfactual policy live rejetee: throttle reduit le notional "
                "de 50%, quarantine reduit le notional a 10%, aucun blocage."
            ),
            action="live_sizing_policy",
            quarantine_multiplier=0.10,
        ),
        ScenarioSpec(
            name="live_sizing_recovery_55_75_base70_partial85",
            description=(
                "Counterfactual A-PNL-02: throttle/quarantine restent a 50%, "
                "les symboles normaux non prouves restent a 70%, recovery "
                "partielle a 85%, plein sizing seulement apres PF/expectancy "
                "rolling positifs."
            ),
            action="recovery_sizing_policy",
        ),
        ScenarioSpec(
            name="loss_probation_symbol_setup_cap50",
            description=(
                "Counterfactual A-PNL-08: cap-only 50% apres pertes rolling "
                "sur le meme couple symbole/setup; rehabilitation au plein "
                "sizing apres PF et expectancy rolling positifs."
            ),
            action="loss_probation_sizing_policy",
        ),
        ScenarioSpec(
            name="quarantine_only_75",
            description="Counterfactual: score >=75 bloque les nouvelles entrées Pod A du symbole.",
            action="quarantine_only",
        ),
        ScenarioSpec(
            name="throttle_then_quarantine_55_75",
            description="Counterfactual: throttle >=55 et blocage >=75.",
            action="throttle_then_quarantine",
        ),
        ScenarioSpec(
            name="throttle_then_quarantine_60_80",
            description="Counterfactual plus strict: throttle >=60 et blocage >=80.",
            throttle_score=60.0,
            quarantine_score=80.0,
            action="throttle_then_quarantine",
        ),
    ]


def filter_scenarios(scenarios: list[ScenarioSpec], raw_names: str) -> list[ScenarioSpec]:
    requested = [item.strip() for item in str(raw_names or "").split(",") if item.strip()]
    if not requested:
        return scenarios
    by_name = {scenario.name: scenario for scenario in scenarios}
    missing = [name for name in requested if name not in by_name]
    if missing:
        available = ", ".join(sorted(by_name))
        raise SystemExit(f"Unknown --scenarios values: {', '.join(missing)}. Available: {available}")
    return [by_name[name] for name in requested]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--baseline-input", default=DEFAULT_BASELINE_INPUT)
    parser.add_argument("--live-input", default=DEFAULT_LIVE_INPUT)
    parser.add_argument("--baseline-start", default="2026-04-05T00:00:00Z")
    parser.add_argument("--baseline-end", default="2026-05-13T23:59:59Z")
    parser.add_argument("--live-start", default="2026-05-14T00:00:00Z")
    parser.add_argument("--live-end", default="2026-06-15T23:59:59Z")
    parser.add_argument(
        "--scenarios",
        default="",
        help="Liste optionnelle de scenarios separes par virgule.",
    )
    parser.add_argument(
        "--window",
        choices=("both", "baseline", "live"),
        default="both",
        help="Fenetre a rejouer; utile pour relancer rapidement la tranche live.",
    )
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--no-live-caps", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    generated_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(args.output_dir or f"server-data/replay_reports/p108_dynamic_symbol_guard_{generated_at}")
    output_dir.mkdir(parents=True, exist_ok=True)
    config = load_config(args.config)
    all_windows = [
        WindowSpec("baseline_apr_may", Path(args.baseline_input), parse_timestamp(args.baseline_start), parse_timestamp(args.baseline_end)),
        WindowSpec("live_post_baseline", Path(args.live_input), parse_timestamp(args.live_start), parse_timestamp(args.live_end)),
    ]
    windows = [
        window
        for window in all_windows
        if args.window == "both"
        or (args.window == "baseline" and window.name == "baseline_apr_may")
        or (args.window == "live" and window.name == "live_post_baseline")
    ]
    scenarios = filter_scenarios(default_scenarios(), args.scenarios)
    rows: list[WindowResult] = []
    for window in windows:
        print(f"window={window.name} status=running", flush=True)
        window_rows = run_window(
            config=config,
            window=window,
            scenarios=scenarios,
            apply_live_caps=not args.no_live_caps,
        )
        rows.extend(window_rows)
        baseline = next(row for row in window_rows if row.scenario == "current_ac")
        for row in window_rows:
            print(
                f"window={window.name} scenario={row.scenario} status=done "
                f"total={row.total_ac_pnl_usd:.2f} delta={row.total_ac_pnl_usd - baseline.total_ac_pnl_usd:+.2f} "
                f"pod_a={row.pod_a_pnl_usd:.2f} trades={row.total_ac_trades} "
                f"guard_blocks={sum(row.pod_a_guard_rejections.values())} throttle={row.guard_throttle_count}",
                flush=True,
            )
    write_csv(output_dir / "scenario_summary.csv", rows)
    payload = {
        "generated_at": generated_at,
        "status": "research_only_no_live_change",
        "live_caps": not args.no_live_caps,
        "windows": [asdict(window) | {"input_path": str(window.input_path)} for window in windows],
        "scenarios": [asdict(scenario) for scenario in scenarios],
        "results": [asdict(row) for row in rows],
    }
    (output_dir / "p108_dynamic_symbol_guard_replay.json").write_text(
        json.dumps(payload, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    write_report(output_dir / "p108_dynamic_symbol_guard_replay.md", rows=rows, generated_at=generated_at)
    print(output_dir)


def run_window(
    *,
    config: AppConfig,
    window: WindowSpec,
    scenarios: list[ScenarioSpec],
    apply_live_caps: bool,
) -> list[WindowResult]:
    helper = FullBotBacktestRunner(config, force_enable_all_pods=True, apply_live_notional_caps=apply_live_caps)
    supervisor = TridentSupervisor(config=config, profile=f"p108-{window.name}", mode="dry-run")
    states = [
        ScenarioState(
            spec=scenario,
            guard=_guard_for_scenario(scenario),
            risk_gate=PodARiskGate(config),
            executor=PodAExecutor(config),
            report=PodABacktestReport(reference_equity_usd=config.trident.capital.reference_equity_usd),
            rejections=Counter(),
        )
        for scenario in scenarios
    ]
    regime_shadow = PodARegimeShadowTracker()
    order_block_shadow = PodAOrderBlockShadowTracker()
    pod_c_report = PodABacktestReport(reference_equity_usd=config.trident.capital.reference_equity_usd)
    latest_snapshots_by_symbol: dict[str, SymbolMarketSnapshot] = {}
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    records_processed = 0
    started = time.perf_counter()

    for record in helper.loader.iter_merged_jsonl(window.input_path):
        timestamp = parse_timestamp(record.timestamp)
        if timestamp is None:
            continue
        if window.start is not None and timestamp < window.start:
            continue
        if window.end is not None and timestamp > window.end:
            continue
        timestamp_text = isoformat(timestamp)
        first_timestamp = first_timestamp or timestamp_text
        last_timestamp = timestamp_text
        snapshots = [symbol_market_snapshot_from_mapping(item) for item in record.symbols]
        latest_snapshots_by_symbol.update({snapshot.symbol: snapshot for snapshot in snapshots})

        if record.capture_reason == "maintenance_refresh":
            for state in states:
                execution = state.executor.process_record(
                    snapshots=snapshots,
                    risk_decisions=[],
                    signal_sides_by_symbol={},
                    timestamp=timestamp_text,
                    entry_allowed_symbols=supervisor.opening_symbols_for(PodName.POD_A),
                    managed_symbols=supervisor.managed_symbols_for(
                        PodName.POD_A,
                        active_symbols=state.executor.portfolio.open_positions.keys(),
                    ),
                )
                record_tick(helper, state, config, snapshots, [], [], execution, timestamp_text, record.source_file, supervisor.state.regime.value)
            helper._process_maintenance_record(
                supervisor=supervisor,
                pod_a_report=PodABacktestReport(),
                pod_b_report=PodABacktestReport(),
                pod_c_report=pod_c_report,
                snapshots=snapshots,
                timestamp=timestamp_text,
                source_file=record.source_file,
                stream_source=record.stream_source,
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
        crypto_regime_snapshot = supervisor.state.cluster_regime_snapshots.get(
            "crypto",
            supervisor.state.regime_snapshot,
        )
        regime_features = regime_shadow.evaluate(
            timestamp=timestamp,
            snapshots=snapshots,
            regime_snapshot=regime_snapshot_mapping(crypto_regime_snapshot),
        )
        order_block_features = order_block_shadow.observe(timestamp=timestamp, snapshots=snapshots)
        previews = supervisor.preview_pod_a_signals(snapshots, timestamp=timestamp_text)
        plans = supervisor.build_pod_a_trade_plans(snapshots, timestamp=timestamp_text)
        for state in states:
            process_state(
                helper=helper,
                state=state,
                config=config,
                supervisor=supervisor,
                snapshots=snapshots,
                previews=previews,
                plans=plans,
                regime_features=regime_features,
                order_block_features=order_block_features,
                timestamp=timestamp_text,
                source_file=record.source_file,
                previous_regime=previous_regime,
                current_regime=current_regime,
                apply_live_caps=apply_live_caps,
            )
        helper._process_pod_c(
            supervisor=supervisor,
            report=pod_c_report,
            snapshots=snapshots,
            timestamp=timestamp_text,
            source_file=record.source_file,
            previous_regime=previous_regime,
            current_regime=current_regime,
        )
        regime_shadow.observe(timestamp=timestamp, snapshots=snapshots)
        records_processed += 1

    latest_snapshots = list(latest_snapshots_by_symbol.values())
    for state in states:
        helper._finalize_directional_report(
            supervisor=supervisor,
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
    helper._finalize_directional_report(
        supervisor=supervisor,
        report=pod_c_report,
        executor=helper.pod_c_executor,
        latest_snapshots=latest_snapshots,
        last_timestamp=last_timestamp,
    )
    supervisor.flush_compact_logs()
    pod_c = pod_c_report.to_dict()
    pod_c_pnl = float(pod_c.get("realized_pnl_usd", 0.0) or 0.0)
    pod_c_trades = int(pod_c.get("closed_trade_count", 0) or 0)
    runtime_seconds = round(time.perf_counter() - started, 3)
    return [
        summarize_state(
            window=window,
            state=state,
            pod_c_pnl=pod_c_pnl,
            pod_c_trades=pod_c_trades,
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
    config: AppConfig,
    supervisor: TridentSupervisor,
    snapshots: list[SymbolMarketSnapshot],
    previews: list[Any],
    plans: list[TradePlan],
    regime_features: dict[str, Any],
    order_block_features: dict[str, Any],
    timestamp: str,
    source_file: str,
    previous_regime: str,
    current_regime: str,
    apply_live_caps: bool,
) -> None:
    helper._add_regime_record(
        report=state.report,
        timestamp=timestamp,
        source_file=source_file,
        previous_regime=previous_regime,
        current_regime=current_regime,
    )
    guard_features = state.guard.evaluate(
        timestamp=timestamp,
        snapshots=snapshots,
        regime_features=regime_features,
        order_block_features=order_block_features,
    )
    date_key = helper._date_key(timestamp, source_file)
    filtered: list[TradePlan] = []
    for plan in plans:
        feature = guard_features.get(plan.symbol.upper())
        rolling_stats = state.risk_gate.rolling_symbol_setup_stats(plan.symbol, plan.setup)
        plan = TradePlan(
            **{
                **asdict(plan),
                "setup_details": {
                    **dict(plan.setup_details or {}),
                    "current_date_key": date_key,
                    **rolling_stats,
                },
            }
        )
        reason = guard_filter_reason(state.spec, feature)
        if reason is not None:
            state.rejections[reason] += 1
            state.report.add_decision(date_key=date_key, setup=plan.setup, accepted=False, reason=reason)
            continue
        if feature is not None and feature.would_throttle:
            state.throttle_count += 1
        if feature is not None and feature.would_block:
            state.quarantine_count += 1
        cap_multiplier = p108_cap_multiplier(state.spec, feature, plan.setup_details)
        if cap_multiplier < 1.0:
            state.cap_reduction_count += 1
            plan = TradePlan(
                **{
                    **asdict(plan),
                    "target_notional_usd": float(plan.target_notional_usd) * cap_multiplier,
                    "margin_usd": float(plan.margin_usd) * cap_multiplier,
                    "risk_budget_usd": float(plan.risk_budget_usd) * cap_multiplier,
                    "expected_loss_usd": float(plan.expected_loss_usd) * cap_multiplier,
                    "setup_details": {
                        **dict(plan.setup_details or {}),
                        "p108_shadow_cap_multiplier": cap_multiplier,
                        "p108_live_sizing_policy_counterfactual": (
                            state.spec.action == "live_sizing_policy"
                        ),
                        "p108_recovery_sizing_policy_counterfactual": (
                            state.spec.action == "recovery_sizing_policy"
                        ),
                        "p108_loss_probation_policy_counterfactual": (
                            state.spec.action == "loss_probation_sizing_policy"
                        ),
                        "symbol_guard_live_action_unchanged": False,
                    },
                }
            )
        filtered.append(plan)
    if apply_live_caps:
        leverage_policy = LeveragePolicy(config.pod_a)
        filtered = [
            apply_live_notional_cap(
                plan,
                config.trident.execution.live_max_order_notional_usd,
                max_leverage=leverage_policy.max_allowed(plan.symbol),
            )
            for plan in filtered
        ]
    risk_decisions = state.risk_gate.evaluate_many(filtered)
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
    record_tick(helper, state, config, snapshots, previews, risk_decisions, execution, timestamp, source_file, supervisor.state.regime.value)


def guard_filter_reason(spec: ScenarioSpec, feature: Any) -> str | None:
    if spec.action == "shadow_only" or feature is None:
        return None
    score = float(getattr(feature, "falling_knife_score", 0.0) or 0.0)
    if spec.action in {"quarantine_only", "throttle_then_quarantine"} and score >= spec.quarantine_score:
        return f"p108_dynamic_symbol_guard_quarantine_ge_{int(spec.quarantine_score)}"
    return None


def p108_cap_multiplier(
    spec: ScenarioSpec,
    feature: Any,
    details: dict[str, Any] | None = None,
) -> float:
    if feature is None:
        if spec.action == "loss_probation_sizing_policy":
            return p108_loss_probation_multiplier(spec, details or {})
        if spec.action != "recovery_sizing_policy":
            return 1.0
        return p108_recovery_multiplier(spec, details or {})
    if spec.action == "loss_probation_sizing_policy":
        return p108_loss_probation_multiplier(spec, details or {})
    if spec.action in {"live_sizing_policy", "recovery_sizing_policy"}:
        state = str(getattr(feature, "state", "") or "").lower()
        if state == "quarantine" or bool(getattr(feature, "would_block", False)):
            return spec.quarantine_multiplier
        if state == "throttle" or bool(getattr(feature, "would_reduce_cap", False)):
            return spec.throttle_multiplier
        if spec.action == "recovery_sizing_policy":
            return p108_recovery_multiplier(spec, details or {})
    if spec.action in {"throttle_only", "throttle_then_quarantine"}:
        if bool(getattr(feature, "would_throttle", False)):
            return spec.throttle_multiplier
    return 1.0


def p108_recovery_multiplier(spec: ScenarioSpec, details: dict[str, Any]) -> float:
    trades = int(float(details.get("symbol_setup_rolling_trades", 0) or 0))
    expectancy = float(details.get("symbol_setup_rolling_expectancy_usd", 0.0) or 0.0)
    profit_factor = float(details.get("symbol_setup_rolling_profit_factor", 0.0) or 0.0)
    if trades < max(int(spec.recovery_min_closed_trades), 0):
        return spec.recovery_base_multiplier
    expectancy_ok = expectancy > spec.recovery_min_expectancy_usd
    profit_factor_ok = profit_factor >= spec.recovery_min_profit_factor
    if expectancy_ok and profit_factor_ok:
        return 1.0
    if expectancy_ok or profit_factor_ok:
        return spec.recovery_partial_multiplier
    return spec.recovery_base_multiplier


def p108_loss_probation_multiplier(spec: ScenarioSpec, details: dict[str, Any]) -> float:
    trades = int(float(details.get("symbol_setup_rolling_trades", 0) or 0))
    if trades < max(int(spec.loss_probation_min_closed_trades), 1):
        return 1.0
    pnl = float(details.get("symbol_setup_rolling_pnl_usd", 0.0) or 0.0)
    expectancy = float(details.get("symbol_setup_rolling_expectancy_usd", 0.0) or 0.0)
    profit_factor = float(details.get("symbol_setup_rolling_profit_factor", 0.0) or 0.0)
    rehabilitated = (
        expectancy > spec.loss_probation_rehab_min_expectancy_usd
        and profit_factor >= spec.loss_probation_rehab_min_profit_factor
    )
    if rehabilitated:
        return 1.0
    probation = (
        pnl <= spec.loss_probation_max_pnl_usd
        or (
            expectancy < 0.0
            and profit_factor <= spec.loss_probation_max_profit_factor
        )
    )
    if probation:
        return spec.loss_probation_multiplier
    return 1.0


def record_tick(
    helper: FullBotBacktestRunner,
    state: ScenarioState,
    config: AppConfig,
    snapshots: list[SymbolMarketSnapshot],
    previews: list[Any],
    risk_decisions: list[RiskDecision],
    execution: Any,
    timestamp: str,
    source_file: str,
    current_regime: str,
) -> None:
    helper._record_directional_tick(
        report=state.report,
        config=config,
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


def summarize_state(
    *,
    window: WindowSpec,
    state: ScenarioState,
    pod_c_pnl: float,
    pod_c_trades: int,
    records_processed: int,
    first_timestamp: str | None,
    last_timestamp: str | None,
    runtime_seconds: float,
) -> WindowResult:
    pod_a = state.report.to_dict()
    pod_a_pnl = float(pod_a.get("realized_pnl_usd", 0.0) or 0.0)
    pod_a_trades = int(pod_a.get("closed_trade_count", 0) or 0)
    return WindowResult(
        window=window.name,
        scenario=state.spec.name,
        description=state.spec.description,
        records_processed=records_processed,
        first_timestamp=first_timestamp,
        last_timestamp=last_timestamp,
        runtime_seconds=runtime_seconds,
        pod_a_pnl_usd=round(pod_a_pnl, 6),
        pod_a_trades=pod_a_trades,
        pod_a_win_rate=_win_rate(pod_a),
        pod_a_profit_factor=_profit_factor(pod_a),
        pod_a_max_drawdown_usd=float(pod_a.get("max_drawdown_usd", 0.0) or 0.0),
        pod_a_accepted_count=int(pod_a.get("accepted_count", 0) or 0),
        pod_a_rejected_count=int(pod_a.get("rejected_count", 0) or 0),
        pod_a_guard_rejections=dict(state.rejections),
        guard_throttle_count=state.throttle_count,
        guard_quarantine_count=state.quarantine_count,
        guard_cap_reduction_count=state.cap_reduction_count,
        pod_c_pnl_usd=round(pod_c_pnl, 6),
        pod_c_trades=pod_c_trades,
        total_ac_pnl_usd=round(pod_a_pnl + pod_c_pnl, 6),
        total_ac_trades=pod_a_trades + pod_c_trades,
    )


def _guard_for_scenario(spec: ScenarioSpec) -> PodADynamicSymbolGuard:
    guard = PodADynamicSymbolGuard()
    guard.THROTTLE_SCORE = spec.throttle_score
    guard.QUARANTINE_SCORE = spec.quarantine_score
    return guard


def write_csv(path: Path, rows: list[WindowResult]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(rows[0]).keys()) if rows else [])
        if rows:
            writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def write_report(path: Path, *, rows: list[WindowResult], generated_at: str) -> None:
    by_window: dict[str, list[WindowResult]] = {}
    for row in rows:
        by_window.setdefault(row.window, []).append(row)
    lines = [
        "# P1-08 dynamic symbol guard replay",
        "",
        f"- generated_at: `{generated_at}`",
        "- status: `research_only_no_live_change`",
        "- note: `Les scénarios throttle/quarantine sont counterfactual; aucune règle live n'est activée.`",
        "",
    ]
    for window, window_rows in by_window.items():
        baseline = next(row for row in window_rows if row.scenario == "current_ac")
        lines.extend([f"## {window}", "", "| Scenario | Total A/C | Delta | Pod A | Pod A trades | Blocks | Throttles | Cap reductions |", "|---|---:|---:|---:|---:|---:|---:|---:|"])
        for row in window_rows:
            blocks = sum(row.pod_a_guard_rejections.values())
            lines.append(
                f"| `{row.scenario}` | {row.total_ac_pnl_usd:.2f} | {row.total_ac_pnl_usd - baseline.total_ac_pnl_usd:+.2f} | "
                f"{row.pod_a_pnl_usd:.2f} | {row.pod_a_trades} | {blocks} | {row.guard_throttle_count} | {row.guard_cap_reduction_count} |"
            )
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _win_rate(report: dict[str, object]) -> float | None:
    reported = report.get("win_rate")
    if reported is not None:
        return round(float(reported), 4)
    trades = int(report.get("closed_trade_count", 0) or 0)
    wins = int(report.get("win_count", 0) or 0)
    return round(wins / trades, 4) if trades else None


def _profit_factor(report: dict[str, object]) -> float | None:
    gross_profit = float(report.get("gross_profit_usd", 0.0) or 0.0)
    gross_loss = abs(float(report.get("gross_loss_usd", 0.0) or 0.0))
    if gross_profit <= 0.0 and gross_loss <= 0.0:
        closed = report.get("closed_trade_log") or []
        if isinstance(closed, list):
            pnls = [
                float(item.get("pnl_usd") or 0.0)
                for item in closed
                if isinstance(item, dict)
            ]
            gross_profit = sum(value for value in pnls if value > 0.0)
            gross_loss = abs(sum(value for value in pnls if value < 0.0))
    if gross_loss <= 0:
        return None
    return round(gross_profit / gross_loss, 4)


if __name__ == "__main__":
    main()
