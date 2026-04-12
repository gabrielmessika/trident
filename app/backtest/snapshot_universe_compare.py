from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from app.backtest.comparative_analysis import build_backtest_comparative_summary
from app.backtest.pod_a_runner import PodABacktestResult, PodABacktestRunner
from app.settings import AppConfig, load_config, override_app_config


@dataclass(slots=True)
class UniverseScenario:
    name: str
    coins: list[str]
    reference_equity_usd: float | None = None
    default_leverage: float | None = None
    max_leverage: float | None = None
    risk_per_trade_pct: float | None = None


@dataclass(slots=True)
class UniverseScenarioResult:
    scenario: UniverseScenario
    backtest: PodABacktestResult
    comparative_summary: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "scenario": asdict(self.scenario),
            "backtest": asdict(self.backtest),
            "comparative_summary": self.comparative_summary,
        }


@dataclass(slots=True)
class SnapshotUniverseCompareResult:
    input_path: str
    scenarios: list[UniverseScenarioResult]
    recommended_scenario: str | None
    report_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "input_path": self.input_path,
            "recommended_scenario": self.recommended_scenario,
            "scenarios": [scenario.to_dict() for scenario in self.scenarios],
            "report_path": self.report_path,
        }


class SnapshotUniverseCompareRunner:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def run(
        self,
        *,
        input_path: str | Path,
        scenarios: list[UniverseScenario],
        report_output: str | Path | None = None,
    ) -> SnapshotUniverseCompareResult:
        scenario_results: list[UniverseScenarioResult] = []
        for scenario in scenarios:
            runtime_config = override_app_config(
                self.config,
                reference_equity_usd=scenario.reference_equity_usd,
                pod_a_default_leverage=scenario.default_leverage,
                pod_a_max_leverage=scenario.max_leverage,
                pod_a_risk_per_trade_pct=scenario.risk_per_trade_pct,
            )
            filtered_input = self._filtered_input(
                input_path=input_path,
                allowed_symbols={coin.upper() for coin in scenario.coins},
            )
            backtest = PodABacktestRunner(runtime_config).run_jsonl(filtered_input)
            comparative_summary = build_backtest_comparative_summary(backtest)
            scenario_results.append(
                UniverseScenarioResult(
                    scenario=scenario,
                    backtest=backtest,
                    comparative_summary=comparative_summary,
                )
            )

        recommended = self._recommended_scenario_name(scenario_results)
        result = SnapshotUniverseCompareResult(
            input_path=str(input_path),
            scenarios=scenario_results,
            recommended_scenario=recommended,
            report_path=str(report_output) if report_output is not None else None,
        )
        if report_output is not None:
            report_path = Path(report_output)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
        return result

    def _filtered_input(
        self,
        *,
        input_path: str | Path,
        allowed_symbols: set[str],
    ) -> Path:
        source = Path(input_path)
        temp_dir = Path(tempfile.mkdtemp(prefix="trident_universe_compare_"))
        if source.is_file():
            target = temp_dir / source.name
            self._filter_file(source, target, allowed_symbols)
            return target
        for file_path in sorted(source.glob("*.jsonl")):
            target = temp_dir / file_path.name
            self._filter_file(file_path, target, allowed_symbols)
        return temp_dir

    def _filter_file(
        self,
        source: Path,
        target: Path,
        allowed_symbols: set[str],
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        with source.open("r", encoding="utf-8") as src, target.open("w", encoding="utf-8") as dst:
            for line in src:
                line = line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                symbols = payload.get("symbols", [])
                if not isinstance(symbols, list):
                    continue
                filtered_symbols = [
                    item
                    for item in symbols
                    if isinstance(item, dict)
                    and str(item.get("symbol", "")).upper() in allowed_symbols
                ]
                if not filtered_symbols:
                    continue
                payload["symbols"] = filtered_symbols
                dst.write(json.dumps(payload) + "\n")
                written += 1
        if written == 0:
            target.write_text("", encoding="utf-8")

    def _recommended_scenario_name(
        self,
        scenarios: list[UniverseScenarioResult],
    ) -> str | None:
        if not scenarios:
            return None
        ordered = sorted(
            scenarios,
            key=lambda item: (
                item.backtest.realized_pnl_usd,
                item.comparative_summary["summary"]["trade_stats"]["expectancy_usd"],
                -item.backtest.max_drawdown_usd,
            ),
            reverse=True,
        )
        return ordered[0].scenario.name


def parse_universe_scenarios(raw: str) -> list[UniverseScenario]:
    scenarios: list[UniverseScenario] = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError("Universe scenarios must use 'name=COIN1,COIN2' syntax")
        name, symbols = chunk.split("=", 1)
        scenarios.append(
            UniverseScenario(
                name=name.strip(),
                coins=[symbol.strip().upper() for symbol in symbols.split(",") if symbol.strip()],
            )
        )
    return scenarios


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare multiple symbol universes on the same snapshot stream",
    )
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--input", required=True)
    parser.add_argument(
        "--universes",
        required=True,
        help="Semicolon-separated scenarios like majors=BTC,ETH,SOL;mix=BTC,ETH,SOL,XYZ:SP500,XYZ:GOLD",
    )
    parser.add_argument("--report-output")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = SnapshotUniverseCompareRunner(load_config(args.config)).run(
        input_path=args.input,
        scenarios=parse_universe_scenarios(args.universes),
        report_output=args.report_output,
    )
    print(f"input_path={result.input_path}")
    print(f"recommended_scenario={result.recommended_scenario}")
    for scenario in result.scenarios:
        summary = scenario.comparative_summary["summary"]
        trade_stats = summary["trade_stats"]
        print(
            "scenario="
            f"{scenario.scenario.name}"
            f" pnl={summary['realized_pnl_usd']}"
            f" drawdown={summary['max_drawdown_usd']}"
            f" expectancy={trade_stats['expectancy_usd']}"
            f" closed_trades={summary['closed_trade_count']}"
            f" closed_trades_per_day={summary['closed_trades_per_day']}"
        )
    if result.report_path:
        print(f"report_path={result.report_path}")


if __name__ == "__main__":
    main()
