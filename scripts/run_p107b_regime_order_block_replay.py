#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import time
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.backtest import full_bot_replay as full_replay
from app.backtest.full_bot_replay import FullBotBacktestRunner
from app.backtest.pod_a_executor import PodAExecutor
from app.backtest.pod_report import PodABacktestReport
from app.execution.live_cap import apply_live_notional_cap
from app.risk.pod_a_gate import PodARiskGate
from app.settings import AppConfig, load_config
from app.trident.pod_a.leverage import LeveragePolicy
from app.trident.pod_a.regime_shadow import (
    PodARegimeShadowTracker,
    RegimeShadowFeatures,
    regime_snapshot_mapping,
)
from app.trident.supervisor import TridentSupervisor
from app.trident.types import (
    PodName,
    RegimeSnapshot,
    SymbolMarketSnapshot,
    TradePlan,
    symbol_market_snapshot_from_mapping,
)
from scripts.run_p108_chart_pattern_research import ChartPatternEngine


DEFAULT_BASELINE_INPUT = (
    "server-data/replay_inputs/external_reference_multisource_20260405_20260513_baseline.jsonl"
)
DEFAULT_LIVE_INPUT = "server-data/live_snapshots"
ORDER_BLOCK_TIMEFRAMES = {"1h", "4h"}


class _NoopRoutingReplayRunner:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def run_jsonl(self, *_args: object, **_kwargs: object) -> "_NoopRoutingReplayRunner":
        return self

    def to_dict(self) -> dict[str, object]:
        return {"skipped": True, "reason": "p107b_regime_order_block_replay"}


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
    enable_short_setup: bool = False
    allow_longs: bool = True
    allow_shorts: bool = False
    long_veto_bearish_ob_regimes: tuple[str, ...] = ()
    long_require_bullish_ob: bool = False
    long_allowed_regimes: tuple[str, ...] = ()
    short_allowed_regimes: tuple[str, ...] = ()
    short_require_bearish_ob: bool = False


@dataclass(slots=True)
class FilterContext:
    features: RegimeShadowFeatures | None
    bullish_order_blocks: list[str]
    bearish_order_blocks: list[str]


@dataclass(slots=True)
class ScenarioState:
    spec: ScenarioSpec
    config: AppConfig
    risk_gate: PodARiskGate
    executor: PodAExecutor
    report: PodABacktestReport


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
    pod_a_signal_count: int
    pod_a_accepted_count: int
    pod_a_rejected_count: int
    pod_a_pattern_rejections: dict[str, int]
    pod_a_pnl_by_side: dict[str, float]
    pod_c_pnl_usd: float
    pod_c_trades: int
    total_ac_pnl_usd: float
    total_ac_trades: int


class RegimeOrderBlockRunner(FullBotBacktestRunner):
    def __init__(
        self,
        config: AppConfig,
        *,
        scenario: ScenarioSpec,
        window_name: str,
        apply_live_notional_caps: bool,
    ) -> None:
        super().__init__(
            config,
            force_enable_all_pods=True,
            apply_live_notional_caps=apply_live_notional_caps,
        )
        self._scenario = scenario
        self._window_name = window_name
        self._regime_shadow = PodARegimeShadowTracker()
        self._chart_engine = ChartPatternEngine(
            timeframes={
                "1h": _timedelta(hours=1),
                "4h": _timedelta(hours=4),
            }
        )
        self._filter_rejections: Counter[str] = Counter()

    def run_window(self, window: WindowSpec) -> WindowResult:
        supervisor = TridentSupervisor(
            config=self.config,
            profile=f"p107b-{window.name}-{self._scenario.name}",
            mode="dry-run",
        )
        pod_a_report = PodABacktestReport(
            reference_equity_usd=self.config.trident.capital.reference_equity_usd,
        )
        pod_c_report = PodABacktestReport(
            reference_equity_usd=self.config.trident.capital.reference_equity_usd,
        )
        latest_snapshots_by_symbol: dict[str, SymbolMarketSnapshot] = {}
        first_timestamp: str | None = None
        last_timestamp: str | None = None
        records_processed = 0
        started = time.perf_counter()

        for record in self.loader.iter_merged_jsonl(window.input_path):
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
                self._process_maintenance_record(
                    supervisor=supervisor,
                    pod_a_report=pod_a_report,
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

            self._process_pod_a(
                supervisor=supervisor,
                report=pod_a_report,
                snapshots=snapshots,
                timestamp=timestamp_text,
                source_file=record.source_file,
                previous_regime=previous_regime,
                current_regime=current_regime,
            )
            self._process_pod_c(
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
        self._finalize_directional_report(
            supervisor=supervisor,
            report=pod_a_report,
            executor=self.pod_a_executor,
            latest_snapshots=latest_snapshots,
            last_timestamp=last_timestamp,
            closed_trade_recorder=self._record_pod_a_closed_trade,
        )
        self._finalize_directional_report(
            supervisor=supervisor,
            report=pod_c_report,
            executor=self.pod_c_executor,
            latest_snapshots=latest_snapshots,
            last_timestamp=last_timestamp,
        )
        supervisor.flush_compact_logs()

        pod_a = pod_a_report.to_dict()
        pod_c = pod_c_report.to_dict()
        pod_a_pnl = float(pod_a.get("realized_pnl_usd", 0.0) or 0.0)
        pod_c_pnl = float(pod_c.get("realized_pnl_usd", 0.0) or 0.0)
        pod_a_trades = int(pod_a.get("closed_trade_count", 0) or 0)
        pod_c_trades = int(pod_c.get("closed_trade_count", 0) or 0)
        pod_a_rejections = {
            str(key): int(value)
            for key, value in (pod_a.get("rejections_by_reason", {}) or {}).items()
            if str(key).startswith("p107b_")
        }
        return WindowResult(
            window=window.name,
            scenario=self._scenario.name,
            description=self._scenario.description,
            records_processed=records_processed,
            first_timestamp=first_timestamp,
            last_timestamp=last_timestamp,
            runtime_seconds=round(time.perf_counter() - started, 3),
            pod_a_pnl_usd=round(pod_a_pnl, 6),
            pod_a_trades=pod_a_trades,
            pod_a_win_rate=_win_rate(pod_a),
            pod_a_profit_factor=_profit_factor(pod_a),
            pod_a_max_drawdown_usd=float(pod_a.get("max_drawdown_usd", 0.0) or 0.0),
            pod_a_signal_count=int(pod_a.get("signal_count", 0) or 0),
            pod_a_accepted_count=int(pod_a.get("accepted_count", 0) or 0),
            pod_a_rejected_count=int(pod_a.get("rejected_count", 0) or 0),
            pod_a_pattern_rejections=pod_a_rejections,
            pod_a_pnl_by_side=_pnl_by_side(pod_a),
            pod_c_pnl_usd=round(pod_c_pnl, 6),
            pod_c_trades=pod_c_trades,
            total_ac_pnl_usd=round(pod_a_pnl + pod_c_pnl, 6),
            total_ac_trades=pod_a_trades + pod_c_trades,
        )

    def _process_pod_a(
        self,
        *,
        supervisor: TridentSupervisor,
        report: PodABacktestReport,
        snapshots: list[SymbolMarketSnapshot],
        timestamp: str | None,
        source_file: str,
        previous_regime: str,
        current_regime: str,
    ) -> None:
        self._add_regime_record(
            report=report,
            timestamp=timestamp,
            source_file=source_file,
            previous_regime=previous_regime,
            current_regime=current_regime,
        )
        parsed_timestamp = parse_timestamp(timestamp)
        features_by_symbol: dict[str, RegimeShadowFeatures] = {}
        if parsed_timestamp is not None:
            self._chart_engine.observe(
                timestamp=parsed_timestamp,
                window=self._window_name,
                snapshots=snapshots,
            )
            regime_snapshot = supervisor.state.cluster_regime_snapshots.get(
                "crypto",
                supervisor.state.regime_snapshot,
            )
            features_by_symbol = self._regime_shadow.evaluate(
                timestamp=parsed_timestamp,
                snapshots=snapshots,
                regime_snapshot=regime_snapshot_mapping(regime_snapshot),
            )

        previews = supervisor.preview_pod_a_signals(snapshots, timestamp=timestamp)
        trade_plans = supervisor.build_pod_a_trade_plans(snapshots, timestamp=timestamp)
        date_key = self._date_key(timestamp, source_file)
        enriched_plans: list[TradePlan] = []
        for plan in trade_plans:
            details = {
                **dict(plan.setup_details or {}),
                "current_date_key": date_key,
            }
            context = self._filter_context(plan.symbol, features_by_symbol)
            details.update(_context_details(context))
            plan = replace(plan, setup_details=details)
            enriched_plans.append(plan)

        filtered_plans: list[TradePlan] = []
        for plan in enriched_plans:
            context = self._filter_context(plan.symbol, features_by_symbol)
            reason = self._filter_reason(plan, context)
            if reason is None:
                filtered_plans.append(plan)
                continue
            self._filter_rejections[reason] += 1
            report.add_decision(
                date_key=date_key,
                setup=plan.setup,
                accepted=False,
                reason=reason,
            )

        filtered_plans = self._apply_live_notional_caps(PodName.POD_A, filtered_plans)
        risk_decisions = self.pod_a_risk_gate.evaluate_many(filtered_plans)
        opening_symbols = supervisor.opening_symbols_for(PodName.POD_A)
        managed_symbols = supervisor.managed_symbols_for(
            PodName.POD_A,
            active_symbols=self.pod_a_executor.portfolio.open_positions.keys(),
        )
        execution = self.pod_a_executor.process_record(
            snapshots=snapshots,
            risk_decisions=risk_decisions,
            signal_sides_by_symbol={preview.symbol: preview.side for preview in previews},
            timestamp=timestamp,
            entry_allowed_symbols=opening_symbols,
            managed_symbols=managed_symbols,
        )
        self._record_directional_tick(
            report=report,
            config=self.config,
            current_regime=supervisor.state.regime.value,
            timestamp=timestamp,
            source_file=source_file,
            previews=previews,
            risk_decisions=risk_decisions,
            execution=execution,
            executor=self.pod_a_executor,
            closed_trade_recorder=self._record_pod_a_closed_trade,
        )
        if parsed_timestamp is not None:
            self._regime_shadow.observe(timestamp=parsed_timestamp, snapshots=snapshots)

    def _apply_live_notional_caps(
        self,
        pod_name: PodName,
        trade_plans: list[object],
    ) -> list[object]:
        if not self.apply_live_notional_caps:
            return trade_plans
        if pod_name == PodName.POD_A:
            leverage_policy = LeveragePolicy(self.config.pod_a)
        elif pod_name == PodName.POD_C:
            leverage_policy = LeveragePolicy(self.config.pod_c)
        else:
            return trade_plans
        return [
            apply_live_notional_cap(
                plan,  # type: ignore[arg-type]
                self.config.trident.execution.live_max_order_notional_usd,
                max_leverage=leverage_policy.max_allowed(getattr(plan, "symbol", None)),
            )
            for plan in trade_plans
        ]

    def _filter_context(
        self,
        symbol: str,
        features_by_symbol: dict[str, RegimeShadowFeatures],
    ) -> FilterContext:
        bullish = [
            f"{event.pattern}:{event.timeframe}"
            for event in self._chart_engine.active_events(symbol, side="long")
            if event.pattern == "order_block_bull_retest"
            and event.timeframe in ORDER_BLOCK_TIMEFRAMES
        ]
        bearish = [
            f"{event.pattern}:{event.timeframe}"
            for event in self._chart_engine.active_events(symbol, side="short")
            if event.pattern == "order_block_bear_retest"
            and event.timeframe in ORDER_BLOCK_TIMEFRAMES
        ]
        return FilterContext(
            features=features_by_symbol.get(symbol.upper()),
            bullish_order_blocks=sorted(set(bullish)),
            bearish_order_blocks=sorted(set(bearish)),
        )

    def _filter_reason(self, plan: TradePlan, context: FilterContext) -> str | None:
        scenario = self._scenario
        features = context.features
        regime_gate = features.regime_gate_decision if features is not None else "missing_features"
        has_bullish_ob = bool(context.bullish_order_blocks)
        has_bearish_ob = bool(context.bearish_order_blocks)
        if plan.side == "long":
            if not scenario.allow_longs:
                return "p107b_long_disabled"
            if scenario.long_allowed_regimes and regime_gate not in set(scenario.long_allowed_regimes):
                return f"p107b_long_regime_{regime_gate}_filtered"
            if scenario.long_require_bullish_ob and not has_bullish_ob:
                return "p107b_long_missing_bullish_order_block_1h4h"
            if (
                scenario.long_veto_bearish_ob_regimes
                and regime_gate in set(scenario.long_veto_bearish_ob_regimes)
                and has_bearish_ob
            ):
                return f"p107b_long_bearish_order_block_{regime_gate}_veto"
            return None
        if plan.side == "short":
            if not scenario.allow_shorts:
                return "p107b_short_disabled"
            if features is None:
                return "p107b_short_missing_regime_features"
            if scenario.short_allowed_regimes and regime_gate not in set(scenario.short_allowed_regimes):
                return f"p107b_short_regime_{regime_gate}_filtered"
            if scenario.short_require_bearish_ob and not has_bearish_ob:
                return "p107b_short_missing_bearish_order_block_1h4h"
            return None
        return "p107b_side_not_supported"


def default_scenarios() -> list[ScenarioSpec]:
    return [
        ScenarioSpec(
            name="current_ac",
            description="Current A/C config: Pod A long-only, no order-block gate.",
        ),
        ScenarioSpec(
            name="long_veto_defensive_bearish_ob_1h4h",
            description="Block Pod A longs when P1-06 gate is defensive/bearish and bearish order block 1h/4h is active.",
            long_veto_bearish_ob_regimes=("defensive", "bearish"),
        ),
        ScenarioSpec(
            name="long_veto_defensive_ob_1h4h",
            description="Block Pod A longs only in defensive regime when bearish order block 1h/4h is active.",
            long_veto_bearish_ob_regimes=("defensive",),
        ),
        ScenarioSpec(
            name="long_constructive_bullish_ob_only_1h4h",
            description="Keep Pod A longs only when regime is constructive/bullish and bullish order block 1h/4h is active.",
            long_allowed_regimes=("constructive", "bullish"),
            long_require_bullish_ob=True,
        ),
        ScenarioSpec(
            name="defensive_short_bearish_ob_only_1h4h",
            description="Shadow short candidate: trend_pullback_short only in defensive regime with bearish order block 1h/4h.",
            enable_short_setup=True,
            allow_longs=False,
            allow_shorts=True,
            short_allowed_regimes=("defensive",),
            short_require_bearish_ob=True,
        ),
        ScenarioSpec(
            name="long_veto_plus_defensive_short_ob_1h4h",
            description="Combo: current longs with defensive/bearish bearish-OB veto plus defensive short only with bearish OB 1h/4h.",
            enable_short_setup=True,
            allow_longs=True,
            allow_shorts=True,
            long_veto_bearish_ob_regimes=("defensive", "bearish"),
            short_allowed_regimes=("defensive",),
            short_require_bearish_ob=True,
        ),
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--baseline-input", default=DEFAULT_BASELINE_INPUT)
    parser.add_argument("--live-input", default=DEFAULT_LIVE_INPUT)
    parser.add_argument("--baseline-start", default="2026-04-05T00:00:00Z")
    parser.add_argument("--baseline-end", default="2026-05-13T23:59:59Z")
    parser.add_argument("--live-start", default="2026-05-14T00:00:00Z")
    parser.add_argument("--live-end", default="2026-06-12T23:59:59Z")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--no-live-caps", action="store_true")
    parser.add_argument("--include-routing-replay", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.include_routing_replay:
        full_replay.RoutingReplayRunner = _NoopRoutingReplayRunner
    generated_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(args.output_dir or f"server-data/replay_reports/p107b_regime_order_block_{generated_at}")
    output_dir.mkdir(parents=True, exist_ok=True)
    base_config = _base_config(args.config)
    windows = [
        WindowSpec(
            name="baseline_apr_may",
            input_path=Path(args.baseline_input),
            start=parse_timestamp(args.baseline_start),
            end=parse_timestamp(args.baseline_end),
        ),
        WindowSpec(
            name="live_post_baseline",
            input_path=Path(args.live_input),
            start=parse_timestamp(args.live_start),
            end=parse_timestamp(args.live_end),
        ),
    ]
    rows: list[WindowResult] = []
    for window in windows:
        print(f"window={window.name} status=running", flush=True)
        window_rows = run_window_fast(
            base_config=base_config,
            window=window,
            scenarios=default_scenarios(),
            apply_live_caps=not args.no_live_caps,
        )
        rows.extend(window_rows)
        baseline = next(row for row in window_rows if row.scenario == "current_ac")
        for result in window_rows:
            delta = result.total_ac_pnl_usd - baseline.total_ac_pnl_usd
            print(
                f"window={window.name} scenario={result.scenario} status=done "
                f"total={result.total_ac_pnl_usd:.2f} delta={delta:+.2f} "
                f"pod_a={result.pod_a_pnl_usd:.2f} trades={result.total_ac_trades}",
                flush=True,
            )

    write_csv(output_dir / "scenario_summary.csv", rows)
    payload = {
        "generated_at": generated_at,
        "status": "research_only_no_live_change",
        "windows": [asdict(window) | {"input_path": str(window.input_path)} for window in windows],
        "scenarios": [asdict(scenario) for scenario in default_scenarios()],
        "results": [asdict(row) for row in rows],
        "live_caps": not args.no_live_caps,
        "routing_replay_included": bool(args.include_routing_replay),
    }
    (output_dir / "p107b_regime_order_block_replay.json").write_text(
        json.dumps(payload, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    write_report(
        output_dir / "p107b_regime_order_block_replay.md",
        generated_at=generated_at,
        rows=rows,
        live_caps=not args.no_live_caps,
    )
    print(output_dir)


def run_window_fast(
    *,
    base_config: AppConfig,
    window: WindowSpec,
    scenarios: list[ScenarioSpec],
    apply_live_caps: bool,
) -> list[WindowResult]:
    short_config = _short_enabled_config(base_config)
    helper = FullBotBacktestRunner(
        base_config,
        force_enable_all_pods=True,
        apply_live_notional_caps=apply_live_caps,
    )
    base_supervisor = TridentSupervisor(
        config=base_config,
        profile=f"p107b-{window.name}-base",
        mode="dry-run",
    )
    short_supervisor = TridentSupervisor(
        config=short_config,
        profile=f"p107b-{window.name}-short",
        mode="dry-run",
    )
    states = [
        ScenarioState(
            spec=scenario,
            config=(short_config if scenario.enable_short_setup else base_config),
            risk_gate=PodARiskGate(short_config if scenario.enable_short_setup else base_config),
            executor=PodAExecutor(short_config if scenario.enable_short_setup else base_config),
            report=PodABacktestReport(
                reference_equity_usd=base_config.trident.capital.reference_equity_usd,
            ),
        )
        for scenario in scenarios
    ]
    regime_shadow = PodARegimeShadowTracker()
    chart_engine = ChartPatternEngine(
        timeframes={
            "1h": _timedelta(hours=1),
            "4h": _timedelta(hours=4),
        }
    )
    pod_c_report = PodABacktestReport(
        reference_equity_usd=base_config.trident.capital.reference_equity_usd,
    )
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
                    entry_allowed_symbols=base_supervisor.opening_symbols_for(PodName.POD_A),
                    managed_symbols=base_supervisor.managed_symbols_for(
                        PodName.POD_A,
                        active_symbols=state.executor.portfolio.open_positions.keys(),
                    ),
                )
                helper._record_directional_tick(
                    report=state.report,
                    config=state.config,
                    current_regime=base_supervisor.state.regime.value,
                    timestamp=timestamp_text,
                    source_file=record.source_file,
                    previews=[],
                    risk_decisions=[],
                    execution=execution,
                    executor=state.executor,
                    closed_trade_recorder=lambda trade, risk_gate=state.risk_gate: _record_pod_a_closed_trade(
                        risk_gate,
                        trade,
                    ),
                )
            helper._process_maintenance_record(
                supervisor=base_supervisor,
                pod_a_report=PodABacktestReport(),
                pod_b_report=PodABacktestReport(),
                pod_c_report=pod_c_report,
                snapshots=snapshots,
                timestamp=timestamp_text,
                source_file=record.source_file,
                stream_source=record.stream_source,
            )
            continue

        previous_base_regime = base_supervisor.state.regime.value
        previous_short_regime = short_supervisor.state.regime.value
        cluster_regime_snapshots = {
            cluster: RegimeSnapshot(**snap)
            for cluster, snap in (record.cluster_regime_snapshots or {}).items()
            if isinstance(snap, dict)
        }
        regime_snapshot = RegimeSnapshot(**record.regime_snapshot)
        base_supervisor.apply_regime_snapshot(
            regime_snapshot,
            cluster_regime_snapshots=cluster_regime_snapshots,
        )
        short_supervisor.apply_regime_snapshot(
            RegimeSnapshot(**record.regime_snapshot),
            cluster_regime_snapshots=cluster_regime_snapshots,
        )
        current_base_regime = base_supervisor.state.regime.value
        current_short_regime = short_supervisor.state.regime.value

        chart_engine.observe(timestamp=timestamp, window=window.name, snapshots=snapshots)
        crypto_regime_snapshot = base_supervisor.state.cluster_regime_snapshots.get(
            "crypto",
            base_supervisor.state.regime_snapshot,
        )
        features_by_symbol = regime_shadow.evaluate(
            timestamp=timestamp,
            snapshots=snapshots,
            regime_snapshot=regime_snapshot_mapping(crypto_regime_snapshot),
        )

        base_previews = base_supervisor.preview_pod_a_signals(snapshots, timestamp=timestamp_text)
        base_plans = base_supervisor.build_pod_a_trade_plans(snapshots, timestamp=timestamp_text)
        short_previews = short_supervisor.preview_pod_a_signals(snapshots, timestamp=timestamp_text)
        short_plans = short_supervisor.build_pod_a_trade_plans(snapshots, timestamp=timestamp_text)

        for state in states:
            if state.spec.enable_short_setup:
                supervisor = short_supervisor
                previous_regime = previous_short_regime
                current_regime = current_short_regime
                previews = short_previews
                plans = short_plans
            else:
                supervisor = base_supervisor
                previous_regime = previous_base_regime
                current_regime = current_base_regime
                previews = base_previews
                plans = base_plans
            process_pod_a_state(
                helper=helper,
                state=state,
                supervisor=supervisor,
                chart_engine=chart_engine,
                features_by_symbol=features_by_symbol,
                snapshots=snapshots,
                previews=previews,
                plans=plans,
                timestamp=timestamp_text,
                source_file=record.source_file,
                previous_regime=previous_regime,
                current_regime=current_regime,
                apply_live_caps=apply_live_caps,
            )

        helper._process_pod_c(
            supervisor=base_supervisor,
            report=pod_c_report,
            snapshots=snapshots,
            timestamp=timestamp_text,
            source_file=record.source_file,
            previous_regime=previous_base_regime,
            current_regime=current_base_regime,
        )
        regime_shadow.observe(timestamp=timestamp, snapshots=snapshots)
        records_processed += 1

    latest_snapshots = list(latest_snapshots_by_symbol.values())
    for state in states:
        helper._finalize_directional_report(
            supervisor=base_supervisor,
            report=state.report,
            executor=state.executor,
            latest_snapshots=latest_snapshots,
            last_timestamp=last_timestamp,
            closed_trade_recorder=lambda trade, risk_gate=state.risk_gate: _record_pod_a_closed_trade(
                risk_gate,
                trade,
            ),
        )
    helper._finalize_directional_report(
        supervisor=base_supervisor,
        report=pod_c_report,
        executor=helper.pod_c_executor,
        latest_snapshots=latest_snapshots,
        last_timestamp=last_timestamp,
    )
    base_supervisor.flush_compact_logs()
    short_supervisor.flush_compact_logs()

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


def process_pod_a_state(
    *,
    helper: FullBotBacktestRunner,
    state: ScenarioState,
    supervisor: TridentSupervisor,
    chart_engine: ChartPatternEngine,
    features_by_symbol: dict[str, RegimeShadowFeatures],
    snapshots: list[SymbolMarketSnapshot],
    previews: list[Any],
    plans: list[TradePlan],
    timestamp: str | None,
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
    date_key = helper._date_key(timestamp, source_file)
    enriched_plans: list[TradePlan] = []
    for plan in plans:
        context = filter_context(chart_engine, plan.symbol, features_by_symbol)
        details = {
            **dict(plan.setup_details or {}),
            "current_date_key": date_key,
            **_context_details(context),
        }
        enriched_plans.append(replace(plan, setup_details=details))

    filtered_plans: list[TradePlan] = []
    for plan in enriched_plans:
        reason = filter_reason(state.spec, plan, filter_context(chart_engine, plan.symbol, features_by_symbol))
        if reason is None:
            filtered_plans.append(plan)
            continue
        state.report.add_decision(
            date_key=date_key,
            setup=plan.setup,
            accepted=False,
            reason=reason,
        )

    if apply_live_caps:
        leverage_policy = LeveragePolicy(state.config.pod_a)
        filtered_plans = [
            apply_live_notional_cap(
                plan,
                state.config.trident.execution.live_max_order_notional_usd,
                max_leverage=leverage_policy.max_allowed(plan.symbol),
            )
            for plan in filtered_plans
        ]
    risk_decisions = state.risk_gate.evaluate_many(filtered_plans)
    opening_symbols = supervisor.opening_symbols_for(PodName.POD_A)
    managed_symbols = supervisor.managed_symbols_for(
        PodName.POD_A,
        active_symbols=state.executor.portfolio.open_positions.keys(),
    )
    execution = state.executor.process_record(
        snapshots=snapshots,
        risk_decisions=risk_decisions,
        signal_sides_by_symbol={preview.symbol: preview.side for preview in previews},
        timestamp=timestamp,
        entry_allowed_symbols=opening_symbols,
        managed_symbols=managed_symbols,
    )
    helper._record_directional_tick(
        report=state.report,
        config=state.config,
        current_regime=supervisor.state.regime.value,
        timestamp=timestamp,
        source_file=source_file,
        previews=previews,
        risk_decisions=risk_decisions,
        execution=execution,
        executor=state.executor,
        closed_trade_recorder=lambda trade: _record_pod_a_closed_trade(state.risk_gate, trade),
    )


def filter_context(
    chart_engine: ChartPatternEngine,
    symbol: str,
    features_by_symbol: dict[str, RegimeShadowFeatures],
) -> FilterContext:
    bullish = [
        f"{event.pattern}:{event.timeframe}"
        for event in chart_engine.active_events(symbol, side="long")
        if event.pattern == "order_block_bull_retest"
        and event.timeframe in ORDER_BLOCK_TIMEFRAMES
    ]
    bearish = [
        f"{event.pattern}:{event.timeframe}"
        for event in chart_engine.active_events(symbol, side="short")
        if event.pattern == "order_block_bear_retest"
        and event.timeframe in ORDER_BLOCK_TIMEFRAMES
    ]
    return FilterContext(
        features=features_by_symbol.get(symbol.upper()),
        bullish_order_blocks=sorted(set(bullish)),
        bearish_order_blocks=sorted(set(bearish)),
    )


def filter_reason(
    scenario: ScenarioSpec,
    plan: TradePlan,
    context: FilterContext,
) -> str | None:
    features = context.features
    regime_gate = features.regime_gate_decision if features is not None else "missing_features"
    has_bullish_ob = bool(context.bullish_order_blocks)
    has_bearish_ob = bool(context.bearish_order_blocks)
    if plan.side == "long":
        if not scenario.allow_longs:
            return "p107b_long_disabled"
        if scenario.long_allowed_regimes and regime_gate not in set(scenario.long_allowed_regimes):
            return f"p107b_long_regime_{regime_gate}_filtered"
        if scenario.long_require_bullish_ob and not has_bullish_ob:
            return "p107b_long_missing_bullish_order_block_1h4h"
        if (
            scenario.long_veto_bearish_ob_regimes
            and regime_gate in set(scenario.long_veto_bearish_ob_regimes)
            and has_bearish_ob
        ):
            return f"p107b_long_bearish_order_block_{regime_gate}_veto"
        return None
    if plan.side == "short":
        if not scenario.allow_shorts:
            return "p107b_short_disabled"
        if features is None:
            return "p107b_short_missing_regime_features"
        if scenario.short_allowed_regimes and regime_gate not in set(scenario.short_allowed_regimes):
            return f"p107b_short_regime_{regime_gate}_filtered"
        if scenario.short_require_bearish_ob and not has_bearish_ob:
            return "p107b_short_missing_bearish_order_block_1h4h"
        return None
    return "p107b_side_not_supported"


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
    rejections = {
        str(key): int(value)
        for key, value in (pod_a.get("rejections_by_reason", {}) or {}).items()
        if str(key).startswith("p107b_")
    }
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
        pod_a_signal_count=int(pod_a.get("signal_count", 0) or 0),
        pod_a_accepted_count=int(pod_a.get("accepted_count", 0) or 0),
        pod_a_rejected_count=int(pod_a.get("rejected_count", 0) or 0),
        pod_a_pattern_rejections=rejections,
        pod_a_pnl_by_side=_pnl_by_side(pod_a),
        pod_c_pnl_usd=round(pod_c_pnl, 6),
        pod_c_trades=pod_c_trades,
        total_ac_pnl_usd=round(pod_a_pnl + pod_c_pnl, 6),
        total_ac_trades=pod_a_trades + pod_c_trades,
    )


def _record_pod_a_closed_trade(risk_gate: PodARiskGate, trade: object) -> None:
    closed_at = getattr(trade, "closed_at", None)
    risk_gate.record_closed_trade(
        symbol=str(getattr(trade, "symbol", "")),
        setup=getattr(trade, "setup", None),
        pnl_usd=getattr(trade, "pnl_usd", None),
        date_key=(closed_at.isoformat()[:10] if closed_at is not None else None),
    )


def _base_config(path: str | Path) -> AppConfig:
    config = load_config(path)
    return replace(config, pod_b=replace(config.pod_b, enabled=False))


def _short_enabled_config(config: AppConfig) -> AppConfig:
    allowed = list(dict.fromkeys([*config.pod_a.allowed_setups, "trend_pullback_short"]))
    disabled = [item for item in config.pod_a.disabled_setups if item != "trend_pullback_short"]
    return replace(
        config,
        pod_a=replace(config.pod_a, allowed_setups=allowed, disabled_setups=disabled),
    )


def _context_details(context: FilterContext) -> dict[str, float | str | bool]:
    features = context.features
    details: dict[str, float | str | bool] = {
        "p107b_has_bullish_order_block_1h4h": bool(context.bullish_order_blocks),
        "p107b_has_bearish_order_block_1h4h": bool(context.bearish_order_blocks),
        "p107b_bullish_order_blocks_1h4h": ",".join(context.bullish_order_blocks),
        "p107b_bearish_order_blocks_1h4h": ",".join(context.bearish_order_blocks),
    }
    if features is None:
        details.update(
            {
                "bull_regime_score": 0.0,
                "bear_regime_score": 0.0,
                "regime_gate_decision": "missing_features",
            }
        )
        return details
    details.update(
        {
            "bull_regime_score": float(features.bull_regime_score),
            "bear_regime_score": float(features.bear_regime_score),
            "regime_gate_decision": features.regime_gate_decision,
            "btc_ret_60m_bps": features.btc_ret_60m_bps or 0.0,
            "btc_ret_240m_bps": features.btc_ret_240m_bps or 0.0,
            "btc_ret_1440m_bps": features.btc_ret_1440m_bps or 0.0,
            "symbol_ret_60m_bps": features.symbol_ret_60m_bps or 0.0,
            "symbol_ret_240m_bps": features.symbol_ret_240m_bps or 0.0,
            "breadth_pct": features.breadth_pct or 0.0,
            "leader_trend_score": features.leader_trend_score or 0.0,
        }
    )
    return details


def _pnl_by_side(report: dict[str, object]) -> dict[str, float]:
    rows = report.get("closed_trade_log", [])
    pnl: dict[str, float] = {}
    if not isinstance(rows, list):
        return pnl
    for row in rows:
        if not isinstance(row, dict):
            continue
        side = str(row.get("side") or "unknown")
        pnl[side] = round(pnl.get(side, 0.0) + float(row.get("pnl_usd", 0.0) or 0.0), 6)
    return pnl


def _win_rate(report: dict[str, object]) -> float | None:
    wins = int(report.get("win_count", 0) or 0)
    losses = int(report.get("loss_count", 0) or 0)
    total = wins + losses
    return round(wins / total, 6) if total else None


def _profit_factor(report: dict[str, object]) -> float | None:
    rows = report.get("closed_trade_log", [])
    if not isinstance(rows, list):
        return None
    wins = 0.0
    losses = 0.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        pnl = float(row.get("pnl_usd", 0.0) or 0.0)
        if pnl > 0:
            wins += pnl
        elif pnl < 0:
            losses += abs(pnl)
    if losses <= 0:
        return None
    return round(wins / losses, 6)


def write_csv(path: Path, rows: Iterable[WindowResult]) -> None:
    row_dicts = [asdict(row) for row in rows]
    if not row_dicts:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row_dicts[0]))
        writer.writeheader()
        writer.writerows(row_dicts)


def write_report(
    path: Path,
    *,
    generated_at: str,
    rows: list[WindowResult],
    live_caps: bool,
) -> None:
    lines = [
        "# P1-07b regime + order block replay",
        "",
        f"- Generated at: `{generated_at}`",
        "- Status: `research_only_no_live_change`",
        f"- Live caps applied: `{live_caps}`",
        "- Scope: A/C directional replay; Pod B disabled; routing replay skipped unless requested.",
        "- Tested patterns: `order_block_bull_retest` / `order_block_bear_retest` on `1h` and `4h` only.",
        "",
        "## Scenario Summary",
        "",
        (
            "| window | scenario | total A/C pnl | delta | trades | Pod A pnl | Pod A trades | "
            "Pod A PF | Pod A DD | Pod C pnl | P1-07b rejections |"
        ),
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        baseline = next(
            (
                item
                for item in rows
                if item.window == row.window and item.scenario == "current_ac"
            ),
            None,
        )
        delta = row.total_ac_pnl_usd - baseline.total_ac_pnl_usd if baseline else 0.0
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{row.window}`",
                    f"`{row.scenario}`",
                    f"{row.total_ac_pnl_usd:.2f}",
                    f"{delta:+.2f}",
                    str(row.total_ac_trades),
                    f"{row.pod_a_pnl_usd:.2f}",
                    str(row.pod_a_trades),
                    format_optional(row.pod_a_profit_factor),
                    f"{row.pod_a_max_drawdown_usd:.2f}",
                    f"{row.pod_c_pnl_usd:.2f}",
                    format_dict(row.pod_a_pattern_rejections),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Lecture",
            "",
            "- Un scenario est promouvable seulement s'il ameliore `live_post_baseline` sans degrader `baseline_apr_may` et sans echantillon trop faible.",
            "- Les shorts restent recherche/shadow seulement; ce replay ne modifie pas la config live.",
            "- Si aucun scenario ne passe ce filtre, P1-07b doit etre abandonne ou garde en observation seulement.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_timestamp(value: object) -> datetime | None:
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


def isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _timedelta(*, hours: int = 0) -> Any:
    from datetime import timedelta

    return timedelta(hours=hours)


def format_optional(value: float | None) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value:.2f}"


def format_dict(values: dict[str, int]) -> str:
    if not values:
        return "-"
    return ", ".join(f"{key}={value}" for key, value in sorted(values.items()))


if __name__ == "__main__":
    main()
