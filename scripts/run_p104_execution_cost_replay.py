#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import statistics
import time
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.backtest.full_bot_replay import FullBotBacktestRunner
from app.backtest.pod_a_executor import PodAExecutor
from app.backtest.pod_report import PodABacktestReport
from app.execution.dry_run import DryRunExecutionVenue, DryRunFill
from app.execution.live_cap import apply_live_notional_cap
from app.risk.pod_a_gate import PodARiskGate
from app.settings import AppConfig, ExecutionConfig, load_config
from app.trident.pod_a.leverage import LeveragePolicy
from app.trident.supervisor import TridentSupervisor
from app.trident.types import (
    PodName,
    RegimeSnapshot,
    RiskDecision,
    SymbolMarketSnapshot,
    symbol_market_snapshot_from_mapping,
)


DEFAULT_BASELINE_INPUT = "server-data/replay_inputs/external_reference_multisource_20260405_20260513_baseline.jsonl"
DEFAULT_LIVE_INPUT = "server-data/live_snapshots"
DEFAULT_FILL_EVENTS = "server-data/audit_exports/20260612T163311Z_p003_final/trident_ac_fill_events.csv"

CONFIG_ERAS = (
    ("era_1_stop_immediate_bug", "2026-05-24T00:00:00Z", "2026-05-27T17:01:00Z"),
    ("era_2_stop_grace_165_cat_300", "2026-05-29T00:00:00Z", "2026-06-09T00:00:00Z"),
    ("era_3_quality_sizing_efe", "2026-06-09T00:00:00Z", "2026-06-12T00:00:00Z"),
)


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
    max_spread_bps: float | None = None
    max_expected_entry_cost_bps: float | None = None


@dataclass(slots=True)
class ScenarioState:
    spec: ScenarioSpec
    risk_gate: PodARiskGate
    executor: PodAExecutor
    report: PodABacktestReport
    skipped_by_cost: Counter[str]


@dataclass(slots=True)
class SlippageRow:
    pod: str
    symbol: str
    setup: str
    action: str
    era: str
    count: int
    avg_bps: float | None
    median_bps: float | None
    p75_bps: float | None
    max_bps: float | None
    fee_usd: float
    notional_usd: float


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
    pod_a_cost_skips: dict[str, int]
    pod_c_pnl_usd: float
    pod_c_trades: int
    total_ac_pnl_usd: float
    total_ac_trades: int


class SpreadAwareDryRunVenue(DryRunExecutionVenue):
    def __init__(
        self,
        config: ExecutionConfig,
        *,
        max_spread_bps: float | None,
        max_expected_entry_cost_bps: float | None,
    ) -> None:
        super().__init__(config)
        self.max_spread_bps = max_spread_bps
        self.max_expected_entry_cost_bps = max_expected_entry_cost_bps
        self.last_block_reason_by_symbol: dict[str, str] = {}

    def open_fill(
        self,
        *,
        symbol: str,
        side: str,
        mid_price: float,
        spread_bps: float,
        notional_usd: float,
        timestamp: str | None,
        plan: object | None = None,
    ) -> DryRunFill | None:
        expected_cost = expected_entry_cost_bps(
            spread_bps=spread_bps,
            dry_run_spread_multiplier=self.config.dry_run_spread_multiplier,
            dry_run_slippage_bps=self.config.dry_run_slippage_bps,
        )
        if self.max_spread_bps is not None and spread_bps > self.max_spread_bps:
            self.last_block_reason_by_symbol[str(symbol)] = (
                f"p104_spread_above_{self.max_spread_bps:g}bps"
            )
            return None
        if (
            self.max_expected_entry_cost_bps is not None
            and expected_cost > self.max_expected_entry_cost_bps
        ):
            self.last_block_reason_by_symbol[str(symbol)] = (
                f"p104_expected_entry_cost_above_{self.max_expected_entry_cost_bps:g}bps"
            )
            return None
        return super().open_fill(
            symbol=symbol,
            side=side,
            mid_price=mid_price,
            spread_bps=spread_bps,
            notional_usd=notional_usd,
            timestamp=timestamp,
            plan=plan,
        )


def default_scenarios() -> list[ScenarioSpec]:
    return [
        ScenarioSpec("current_ac", "Config courante A/C, sans filtre coût additionnel."),
        ScenarioSpec(
            "spread_lte_6bps",
            "Skip Pod A open si spread snapshot > 6 bps.",
            max_spread_bps=6.0,
        ),
        ScenarioSpec(
            "spread_lte_8bps",
            "Skip Pod A open si spread snapshot > 8 bps.",
            max_spread_bps=8.0,
        ),
        ScenarioSpec(
            "expected_entry_cost_lte_8bps",
            "Skip Pod A open si coût attendu dry-run > 8 bps.",
            max_expected_entry_cost_bps=8.0,
        ),
        ScenarioSpec(
            "expected_entry_cost_lte_10bps",
            "Skip Pod A open si coût attendu dry-run > 10 bps.",
            max_expected_entry_cost_bps=10.0,
        ),
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--baseline-input", default=DEFAULT_BASELINE_INPUT)
    parser.add_argument("--live-input", default=DEFAULT_LIVE_INPUT)
    parser.add_argument("--fill-events", default=DEFAULT_FILL_EVENTS)
    parser.add_argument("--baseline-start", default="2026-04-05T00:00:00Z")
    parser.add_argument("--baseline-end", default="2026-05-13T23:59:59Z")
    parser.add_argument("--live-start", default="2026-05-14T00:00:00Z")
    parser.add_argument("--live-end", default="2026-06-15T23:59:59Z")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--no-live-caps", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    generated_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(args.output_dir or f"server-data/replay_reports/p104_execution_cost_{generated_at}")
    output_dir.mkdir(parents=True, exist_ok=True)
    config = load_config(args.config)
    windows = [
        WindowSpec("baseline_apr_may", Path(args.baseline_input), parse_timestamp(args.baseline_start), parse_timestamp(args.baseline_end)),
        WindowSpec("live_post_baseline", Path(args.live_input), parse_timestamp(args.live_start), parse_timestamp(args.live_end)),
    ]
    slippage_rows = load_slippage_rows(Path(args.fill_events))
    write_slippage_csv(output_dir / "slippage_by_pod_symbol_setup_era.csv", slippage_rows)
    results: list[WindowResult] = []
    for window in windows:
        print(f"window={window.name} status=running", flush=True)
        rows = run_window(
            config=config,
            window=window,
            scenarios=default_scenarios(),
            apply_live_caps=not args.no_live_caps,
        )
        results.extend(rows)
        baseline = next(row for row in rows if row.scenario == "current_ac")
        for row in rows:
            skips = sum(row.pod_a_cost_skips.values())
            print(
                f"window={window.name} scenario={row.scenario} status=done "
                f"total={row.total_ac_pnl_usd:.2f} delta={row.total_ac_pnl_usd - baseline.total_ac_pnl_usd:+.2f} "
                f"pod_a={row.pod_a_pnl_usd:.2f} trades={row.total_ac_trades} skips={skips}",
                flush=True,
            )
    write_results_csv(output_dir / "scenario_summary.csv", results)
    payload = {
        "generated_at": generated_at,
        "status": "research_only_no_live_change",
        "live_caps": not args.no_live_caps,
        "fill_events": str(args.fill_events),
        "slippage_summary": [asdict(row) for row in slippage_rows],
        "windows": [asdict(window) | {"input_path": str(window.input_path)} for window in windows],
        "scenarios": [asdict(scenario) for scenario in default_scenarios()],
        "results": [asdict(row) for row in results],
    }
    (output_dir / "p104_execution_cost_replay.json").write_text(
        json.dumps(payload, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    write_report(output_dir / "p104_execution_cost_replay.md", rows=results, slippage_rows=slippage_rows, generated_at=generated_at)
    print(output_dir)


def run_window(
    *,
    config: AppConfig,
    window: WindowSpec,
    scenarios: list[ScenarioSpec],
    apply_live_caps: bool,
) -> list[WindowResult]:
    helper = FullBotBacktestRunner(config, force_enable_all_pods=True, apply_live_notional_caps=apply_live_caps)
    supervisor = TridentSupervisor(config=config, profile=f"p104-{window.name}", mode="dry-run")
    states = [
        _scenario_state(config=config, spec=scenario)
        for scenario in scenarios
    ]
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
                record_tick(helper, state, config, [], [], execution, timestamp_text, record.source_file, supervisor.state.regime.value)
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
        previews = supervisor.preview_pod_a_signals(snapshots, timestamp=timestamp_text)
        plans = supervisor.build_pod_a_trade_plans(snapshots, timestamp=timestamp_text)
        date_key = helper._date_key(timestamp_text, record.source_file)
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
            leverage_policy = LeveragePolicy(config.pod_a)
            plans = [
                apply_live_notional_cap(
                    plan,
                    config.trident.execution.live_max_order_notional_usd,
                    max_leverage=leverage_policy.max_allowed(plan.symbol),
                )
                for plan in plans
            ]
        for state in states:
            process_state(
                helper=helper,
                state=state,
                config=config,
                supervisor=supervisor,
                snapshots=snapshots,
                previews=previews,
                plans=plans,
                timestamp=timestamp_text,
                source_file=record.source_file,
                previous_regime=previous_regime,
                current_regime=current_regime,
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
    plans: list[Any],
    timestamp: str,
    source_file: str,
    previous_regime: str,
    current_regime: str,
) -> None:
    helper._add_regime_record(
        report=state.report,
        timestamp=timestamp,
        source_file=source_file,
        previous_regime=previous_regime,
        current_regime=current_regime,
    )
    risk_decisions = state.risk_gate.evaluate_many(plans)
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
    for reason in execution.skip_reasons_by_symbol.values():
        if str(reason).startswith("p104_"):
            state.skipped_by_cost[str(reason)] += 1
    record_tick(helper, state, config, previews, risk_decisions, execution, timestamp, source_file, supervisor.state.regime.value)


def record_tick(
    helper: FullBotBacktestRunner,
    state: ScenarioState,
    config: AppConfig,
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


def _scenario_state(*, config: AppConfig, spec: ScenarioSpec) -> ScenarioState:
    executor = PodAExecutor(config)
    executor.venue = SpreadAwareDryRunVenue(
        config.trident.execution,
        max_spread_bps=spec.max_spread_bps,
        max_expected_entry_cost_bps=spec.max_expected_entry_cost_bps,
    )
    return ScenarioState(
        spec=spec,
        risk_gate=PodARiskGate(config),
        executor=executor,
        report=PodABacktestReport(reference_equity_usd=config.trident.capital.reference_equity_usd),
        skipped_by_cost=Counter(),
    )


def load_slippage_rows(path: Path) -> list[SlippageRow]:
    buckets: dict[tuple[str, str, str, str, str], list[dict[str, float]]] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            action = str(row.get("action") or "").strip().lower()
            if action not in {"open", "close"}:
                continue
            slippage = optional_float(row.get("slippage_bps"))
            if slippage is None:
                continue
            ts = parse_timestamp(row.get("fill_ts") or row.get("event_ts"))
            pod = str(row.get("pod") or "").strip() or "unknown"
            symbol = str(row.get("symbol") or "").strip() or "unknown"
            setup = str(row.get("setup") or "").strip() or "unknown"
            era = era_for(ts)
            buckets.setdefault((pod, symbol, setup, action, era), []).append(
                {
                    "slippage_bps": slippage,
                    "fee_usd": float(optional_float(row.get("fee_usd")) or optional_float(row.get("exchange_fee_usd")) or 0.0),
                    "notional_usd": float(optional_float(row.get("notional_usd")) or 0.0),
                }
            )
    rows: list[SlippageRow] = []
    for (pod, symbol, setup, action, era), items in sorted(buckets.items()):
        values = [item["slippage_bps"] for item in items]
        rows.append(
            SlippageRow(
                pod=pod,
                symbol=symbol,
                setup=setup,
                action=action,
                era=era,
                count=len(values),
                avg_bps=round(sum(values) / len(values), 4) if values else None,
                median_bps=round(statistics.median(values), 4) if values else None,
                p75_bps=percentile(values, 0.75),
                max_bps=round(max(values), 4) if values else None,
                fee_usd=round(sum(item["fee_usd"] for item in items), 6),
                notional_usd=round(sum(item["notional_usd"] for item in items), 6),
            )
        )
    return rows


def era_for(timestamp: datetime | None) -> str:
    if timestamp is None:
        return "unknown"
    for era_id, start_text, end_text in CONFIG_ERAS:
        start = parse_timestamp(start_text)
        end = parse_timestamp(end_text)
        if start is not None and end is not None and start <= timestamp < end:
            return era_id
    return "outside_defined_eras"


def expected_entry_cost_bps(
    *,
    spread_bps: float,
    dry_run_spread_multiplier: float,
    dry_run_slippage_bps: float,
) -> float:
    return max(float(spread_bps), 0.0) * float(dry_run_spread_multiplier) + float(dry_run_slippage_bps)


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
        pod_a_win_rate=win_rate(pod_a),
        pod_a_profit_factor=profit_factor(pod_a),
        pod_a_max_drawdown_usd=float(pod_a.get("max_drawdown_usd", 0.0) or 0.0),
        pod_a_accepted_count=int(pod_a.get("accepted_count", 0) or 0),
        pod_a_rejected_count=int(pod_a.get("rejected_count", 0) or 0),
        pod_a_cost_skips=dict(state.skipped_by_cost),
        pod_c_pnl_usd=round(pod_c_pnl, 6),
        pod_c_trades=pod_c_trades,
        total_ac_pnl_usd=round(pod_a_pnl + pod_c_pnl, 6),
        total_ac_trades=pod_a_trades + pod_c_trades,
    )


def write_slippage_csv(path: Path, rows: list[SlippageRow]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(rows[0]).keys()) if rows else [])
        if rows:
            writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def write_results_csv(path: Path, rows: list[WindowResult]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(rows[0]).keys()) if rows else [])
        if rows:
            writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def write_report(path: Path, *, rows: list[WindowResult], slippage_rows: list[SlippageRow], generated_at: str) -> None:
    by_window: dict[str, list[WindowResult]] = {}
    for row in rows:
        by_window.setdefault(row.window, []).append(row)
    lines = [
        "# P1-04 execution cost replay",
        "",
        f"- generated_at: `{generated_at}`",
        "- status: `research_only_no_live_change`",
        "- note: `Les scénarios de skip spread/cost sont counterfactual; aucune règle live n'est activée.`",
        "",
        "## Replay scenarios",
    ]
    for window, window_rows in by_window.items():
        baseline = next(row for row in window_rows if row.scenario == "current_ac")
        lines.extend(["", f"### {window}", "", "| Scenario | Total A/C | Delta | Pod A | Pod A trades | Cost skips |", "|---|---:|---:|---:|---:|---:|"])
        for row in window_rows:
            lines.append(
                f"| `{row.scenario}` | {row.total_ac_pnl_usd:.2f} | {row.total_ac_pnl_usd - baseline.total_ac_pnl_usd:+.2f} | "
                f"{row.pod_a_pnl_usd:.2f} | {row.pod_a_trades} | {sum(row.pod_a_cost_skips.values())} |"
            )
    lines.extend(["", "## Slippage observed, worst Pod A open buckets", "", "| Era | Symbol | Setup | Count | Avg bps | P75 bps | Max bps | Fees |", "|---|---|---|---:|---:|---:|---:|---:|"])
    worst = sorted(
        [row for row in slippage_rows if row.pod == "pod_a" and row.action == "open"],
        key=lambda row: (float(row.avg_bps or 0.0), row.count),
        reverse=True,
    )[:20]
    for row in worst:
        lines.append(
            f"| `{row.era}` | `{row.symbol}` | `{row.setup}` | {row.count} | "
            f"{fmt(row.avg_bps)} | {fmt(row.p75_bps)} | {fmt(row.max_bps)} | {row.fee_usd:.4f} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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


def optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(int(len(ordered) * quantile), len(ordered) - 1)
    return round(ordered[index], 4)


def win_rate(report: dict[str, object]) -> float | None:
    trades = int(report.get("closed_trade_count", 0) or 0)
    wins = int(report.get("winning_trade_count", 0) or 0)
    return round(wins / trades, 4) if trades else None


def profit_factor(report: dict[str, object]) -> float | None:
    gross_profit = float(report.get("gross_profit_usd", 0.0) or 0.0)
    gross_loss = abs(float(report.get("gross_loss_usd", 0.0) or 0.0))
    return round(gross_profit / gross_loss, 4) if gross_loss > 0 else None


def fmt(value: float | None) -> str:
    return "" if value is None else f"{value:.2f}"


if __name__ == "__main__":
    main()
