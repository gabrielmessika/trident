from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from app.backtest.full_bot_replay import FullBotBacktestResult, FullBotBacktestRunner
from app.settings import (
    AllocationConfig,
    AppConfig,
    RegimeAllocations,
    load_config,
)


@dataclass(slots=True)
class FullBotExperimentScenario:
    name: str
    description: str
    pod_a_enabled: bool = True
    pod_b_enabled: bool = True
    pod_c_enabled: bool = True
    pod_a_disabled_setups: list[str] | None = None
    pod_a_blocked_regimes: list[str] | None = None
    pod_a_allowed_setups_in_blocked_regimes: list[str] | None = None
    pod_b_max_allocation_pct: float | None = None
    pod_c_size_multiplier: float | None = None
    pod_c_blocked_symbols: list[str] | None = None
    routing_reassignment_debounce_seconds_by_symbol: dict[str, int] | None = None
    routing_revoke_grace_minutes_by_symbol: dict[str, int] | None = None
    allocations: dict[str, dict[str, float]] | None = None


@dataclass(slots=True)
class FullBotExperimentScenarioResult:
    scenario: FullBotExperimentScenario
    result: FullBotBacktestResult

    def to_dict(self) -> dict[str, object]:
        return {
            "scenario": asdict(self.scenario),
            "result": self.result.to_dict(),
        }


@dataclass(slots=True)
class FullBotExperimentSweepResult:
    input_path: str
    scenarios: list[FullBotExperimentScenarioResult]
    recommended_scenario: str | None
    report_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "input_path": self.input_path,
            "recommended_scenario": self.recommended_scenario,
            "scenarios": [scenario.to_dict() for scenario in self.scenarios],
            "report_path": self.report_path,
        }


class FullBotExperimentSweepRunner:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def run(
        self,
        *,
        input_path: str | Path,
        scenarios: list[FullBotExperimentScenario],
        dedupe_by_timestamp: bool = True,
        report_output: str | Path | None = None,
        report_dir: str | Path | None = None,
    ) -> FullBotExperimentSweepResult:
        scenario_results: list[FullBotExperimentScenarioResult] = []
        output_root = Path(report_dir) if report_dir is not None else None
        if output_root is not None:
            output_root.mkdir(parents=True, exist_ok=True)

        for scenario in scenarios:
            runtime_config = self._scenario_config(scenario)
            report_path = (
                output_root / f"{scenario.name}.json"
                if output_root is not None
                else None
            )
            summary_path = (
                output_root / f"{scenario.name}.md"
                if output_root is not None
                else None
            )
            result = FullBotBacktestRunner(
                runtime_config,
                force_enable_all_pods=False,
            ).run_jsonl(
                input_path=input_path,
                dedupe_by_timestamp=dedupe_by_timestamp,
                report_output=report_path,
                summary_output=summary_path,
            )
            scenario_results.append(
                FullBotExperimentScenarioResult(
                    scenario=scenario,
                    result=result,
                )
            )

        sweep_result = FullBotExperimentSweepResult(
            input_path=str(input_path),
            scenarios=scenario_results,
            recommended_scenario=self._recommended_scenario_name(scenario_results),
            report_path=str(report_output) if report_output is not None else None,
        )
        if report_output is not None:
            output_path = Path(report_output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(sweep_result.to_dict(), indent=2) + "\n",
                encoding="utf-8",
            )
        return sweep_result

    def _scenario_config(self, scenario: FullBotExperimentScenario) -> AppConfig:
        config = self.config
        pod_a = replace(
            config.pod_a,
            enabled=scenario.pod_a_enabled,
            disabled_setups=(
                list(scenario.pod_a_disabled_setups)
                if scenario.pod_a_disabled_setups is not None
                else list(config.pod_a.disabled_setups)
            ),
            blocked_regimes=(
                list(scenario.pod_a_blocked_regimes)
                if scenario.pod_a_blocked_regimes is not None
                else list(config.pod_a.blocked_regimes)
            ),
            allowed_setups_in_blocked_regimes=(
                list(scenario.pod_a_allowed_setups_in_blocked_regimes)
                if scenario.pod_a_allowed_setups_in_blocked_regimes is not None
                else list(config.pod_a.allowed_setups_in_blocked_regimes)
            ),
        )
        pod_b = replace(
            config.pod_b,
            enabled=scenario.pod_b_enabled,
            max_allocation_pct=(
                float(scenario.pod_b_max_allocation_pct)
                if scenario.pod_b_max_allocation_pct is not None
                else config.pod_b.max_allocation_pct
            ),
        )
        pod_c = replace(
            config.pod_c,
            enabled=scenario.pod_c_enabled,
            size_multiplier=(
                float(scenario.pod_c_size_multiplier)
                if scenario.pod_c_size_multiplier is not None
                else config.pod_c.size_multiplier
            ),
            blocked_symbols=(
                list(scenario.pod_c_blocked_symbols)
                if scenario.pod_c_blocked_symbols is not None
                else list(config.pod_c.blocked_symbols)
            ),
        )
        execution = replace(
            config.trident.execution,
            routing_revoke_grace_minutes_by_symbol=(
                dict(scenario.routing_revoke_grace_minutes_by_symbol)
                if scenario.routing_revoke_grace_minutes_by_symbol is not None
                else dict(config.trident.execution.routing_revoke_grace_minutes_by_symbol)
            ),
        )
        routing = replace(
            config.trident.routing,
            reassignment_debounce_seconds_by_symbol=(
                dict(scenario.routing_reassignment_debounce_seconds_by_symbol)
                if scenario.routing_reassignment_debounce_seconds_by_symbol is not None
                else dict(config.trident.routing.reassignment_debounce_seconds_by_symbol)
            ),
        )
        allocations = (
            self._scenario_allocations(scenario.allocations)
            if scenario.allocations is not None
            else config.trident.allocations
        )
        trident = replace(
            config.trident,
            execution=execution,
            routing=routing,
            allocations=allocations,
        )
        return replace(
            config,
            trident=trident,
            pod_a=pod_a,
            pod_b=pod_b,
            pod_c=pod_c,
        )

    def _scenario_allocations(
        self,
        raw: dict[str, dict[str, float]],
    ) -> RegimeAllocations:
        def alloc(name: str) -> AllocationConfig:
            section = raw.get(name, {})
            return AllocationConfig(
                pod_a=float(section.get("pod_a", 0.0)),
                pod_b=float(section.get("pod_b", 0.0)),
                pod_c=float(section.get("pod_c", 0.0)),
                cash=float(section.get("cash", 0.0)),
            )

        return RegimeAllocations(
            trend_expansion=alloc("trend_expansion"),
            range_auction=alloc("range_auction"),
            panic_squeeze=alloc("panic_squeeze"),
            dead_zone=alloc("dead_zone"),
        )

    def _recommended_scenario_name(
        self,
        scenarios: list[FullBotExperimentScenarioResult],
    ) -> str | None:
        if not scenarios:
            return None
        ordered = sorted(
            scenarios,
            key=lambda item: (
                item.result.total_realized_pnl_usd,
                item.result.pod_a.get("realized_pnl_usd", 0.0),
                item.result.pod_b.get("realized_pnl_usd", 0.0),
                item.result.pod_c.get("realized_pnl_usd", 0.0),
            ),
            reverse=True,
        )
        return ordered[0].scenario.name


def default_radical_scenarios() -> list[FullBotExperimentScenario]:
    return [
        FullBotExperimentScenario(
            name="current_tuned",
            description="Configuration tunée actuelle pour référence.",
        ),
        FullBotExperimentScenario(
            name="pod_a_only_trend",
            description="Pod A seul, capital quasi integral en TrendExpansion, cash ailleurs.",
            pod_b_enabled=False,
            pod_c_enabled=False,
            pod_a_disabled_setups=["bos_retest_short"],
            pod_a_blocked_regimes=["DeadZone", "RangeAuction", "PanicSqueeze"],
            pod_a_allowed_setups_in_blocked_regimes=[],
            allocations={
                "trend_expansion": {"pod_a": 1.0, "pod_b": 0.0, "pod_c": 0.0, "cash": 0.0},
                "range_auction": {"pod_a": 0.0, "pod_b": 0.0, "pod_c": 0.0, "cash": 1.0},
                "panic_squeeze": {"pod_a": 0.0, "pod_b": 0.0, "pod_c": 0.0, "cash": 1.0},
                "dead_zone": {"pod_a": 0.0, "pod_b": 0.0, "pod_c": 0.0, "cash": 1.0},
            },
            routing_reassignment_debounce_seconds_by_symbol={},
            routing_revoke_grace_minutes_by_symbol={},
        ),
        FullBotExperimentScenario(
            name="pod_a_plus_pod_b_no_pod_c",
            description="Pod C coupe; Pod A domine les trends, Pod B garde les ranges.",
            pod_c_enabled=False,
            allocations={
                "trend_expansion": {"pod_a": 0.85, "pod_b": 0.15, "pod_c": 0.0, "cash": 0.0},
                "range_auction": {"pod_a": 0.10, "pod_b": 0.90, "pod_c": 0.0, "cash": 0.0},
                "panic_squeeze": {"pod_a": 0.15, "pod_b": 0.0, "pod_c": 0.0, "cash": 0.85},
                "dead_zone": {"pod_a": 0.0, "pod_b": 0.25, "pod_c": 0.0, "cash": 0.75},
            },
        ),
        FullBotExperimentScenario(
            name="pod_a_liquidity_only",
            description="Pod A seul avec seulement les setups de sweep/reclaim; Pod B/C coupes.",
            pod_b_enabled=False,
            pod_c_enabled=False,
            pod_a_disabled_setups=[
                "bos_retest_short",
                "bos_retest_long",
                "trend_pullback_long",
                "trend_pullback_short",
            ],
            pod_a_blocked_regimes=["DeadZone", "RangeAuction", "PanicSqueeze"],
            pod_a_allowed_setups_in_blocked_regimes=["liquidity_sweep_reclaim_long"],
            allocations={
                "trend_expansion": {"pod_a": 1.0, "pod_b": 0.0, "pod_c": 0.0, "cash": 0.0},
                "range_auction": {"pod_a": 0.0, "pod_b": 0.0, "pod_c": 0.0, "cash": 1.0},
                "panic_squeeze": {"pod_a": 0.0, "pod_b": 0.0, "pod_c": 0.0, "cash": 1.0},
                "dead_zone": {"pod_a": 0.0, "pod_b": 0.0, "pod_c": 0.0, "cash": 1.0},
            },
            routing_reassignment_debounce_seconds_by_symbol={},
            routing_revoke_grace_minutes_by_symbol={},
        ),
        FullBotExperimentScenario(
            name="pod_b_only_range",
            description="Test radical market making seul pour mesurer son edge brut sans pods directionnels.",
            pod_a_enabled=False,
            pod_c_enabled=False,
            allocations={
                "trend_expansion": {"pod_a": 0.0, "pod_b": 0.20, "pod_c": 0.0, "cash": 0.80},
                "range_auction": {"pod_a": 0.0, "pod_b": 1.0, "pod_c": 0.0, "cash": 0.0},
                "panic_squeeze": {"pod_a": 0.0, "pod_b": 0.0, "pod_c": 0.0, "cash": 1.0},
                "dead_zone": {"pod_a": 0.0, "pod_b": 0.35, "pod_c": 0.0, "cash": 0.65},
            },
        ),
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run radical full-bot scenarios on one shared snapshot stream",
    )
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--input", required=True)
    parser.add_argument("--report-output")
    parser.add_argument("--report-dir")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = FullBotExperimentSweepRunner(load_config(args.config)).run(
        input_path=args.input,
        scenarios=default_radical_scenarios(),
        report_output=args.report_output,
        report_dir=args.report_dir,
    )
    print(f"recommended_scenario={result.recommended_scenario}")
    for scenario_result in result.scenarios:
        print(
            "scenario="
            f"{scenario_result.scenario.name}"
            f" total_realized_pnl_usd={scenario_result.result.total_realized_pnl_usd}"
            f" pod_a={scenario_result.result.pod_a.get('realized_pnl_usd', 0.0)}"
            f" pod_b={scenario_result.result.pod_b.get('realized_pnl_usd', 0.0)}"
            f" pod_c={scenario_result.result.pod_c.get('realized_pnl_usd', 0.0)}"
        )


if __name__ == "__main__":
    main()
