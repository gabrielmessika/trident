from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from app.backtest.archive_replay import parse_dates
from app.backtest.gbot_converter import GbotL2ToTridentConverter
from app.backtest.pod_a_runner import PodABacktestResult, PodABacktestRunner
from app.hyperliquid.info_client import apply_live_asset_leverage_caps
from app.settings import AppConfig, load_config, override_app_config

DEFAULT_REPLAY_LEVERAGES = "1,2,3,5,10"


@dataclass(slots=True)
class ReplayScenario:
    name: str
    reference_equity_usd: float
    default_leverage: float
    max_leverage: float
    risk_per_trade_pct: float | None = None


@dataclass(slots=True)
class ReplayScenarioResult:
    scenario: ReplayScenario
    backtest: PodABacktestResult

    @property
    def realized_pnl_pct(self) -> float:
        equity = max(self.scenario.reference_equity_usd, 1e-9)
        return round(self.backtest.realized_pnl_usd / equity * 100.0, 4)

    @property
    def max_drawdown_pct(self) -> float:
        equity = max(self.scenario.reference_equity_usd, 1e-9)
        return round(self.backtest.max_drawdown_usd / equity * 100.0, 4)

    @property
    def max_open_expected_loss_pct(self) -> float:
        equity = max(self.scenario.reference_equity_usd, 1e-9)
        return round(self.backtest.max_open_expected_loss_usd / equity * 100.0, 4)

    def to_dict(self) -> dict[str, object]:
        return {
            "scenario": asdict(self.scenario),
            "realized_pnl_pct": self.realized_pnl_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "max_open_expected_loss_pct": self.max_open_expected_loss_pct,
            "backtest": asdict(self.backtest),
        }


@dataclass(slots=True)
class ArchiveReplaySweepResult:
    data_dir: str
    dates: list[str]
    coins: list[str]
    snapshot_dir: str
    snapshot_records_written: int
    snapshot_files_written: int
    scenarios: list[ReplayScenarioResult]
    recommended_scenario: str | None
    report_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "data_dir": self.data_dir,
            "dates": self.dates,
            "coins": self.coins,
            "snapshot_dir": self.snapshot_dir,
            "snapshot_records_written": self.snapshot_records_written,
            "snapshot_files_written": self.snapshot_files_written,
            "recommended_scenario": self.recommended_scenario,
            "scenarios": [scenario.to_dict() for scenario in self.scenarios],
            "report_path": self.report_path,
        }


class ArchiveReplaySweepRunner:
    """Runs a leverage/equity sweep on one converted snapshot set."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def run(
        self,
        *,
        data_dir: str | Path,
        dates: list[str],
        coins: list[str],
        scenarios: list[ReplayScenario],
        snapshot_dir: str | Path | None = None,
        report_output: str | Path | None = None,
        bucket_ms: int = 60_000,
        use_live_asset_caps: bool = False,
    ) -> ArchiveReplaySweepResult:
        snapshot_dir_path: Path
        if snapshot_dir is None:
            snapshot_dir_path = Path(tempfile.mkdtemp(prefix="trident_sweep_snapshots_"))
        else:
            snapshot_dir_path = Path(snapshot_dir)
            snapshot_dir_path.mkdir(parents=True, exist_ok=True)
        base_runtime_config = self.config
        if use_live_asset_caps:
            base_runtime_config = apply_live_asset_leverage_caps(
                self.config,
                symbols=coins,
                sleep_fn=lambda _: None,
            )

        converter = GbotL2ToTridentConverter(bucket_ms=bucket_ms)
        snapshot_records_written = 0
        snapshot_files_written = 0
        for replay_date in dates:
            output_path = snapshot_dir_path / f"{replay_date}.jsonl"
            written = converter.convert(
                data_dir=data_dir,
                date=replay_date,
                coins=coins,
                output_path=output_path,
            )
            if written <= 0:
                if output_path.exists():
                    output_path.unlink()
                continue
            snapshot_records_written += written
            snapshot_files_written += 1

        scenario_results: list[ReplayScenarioResult] = []
        for scenario in scenarios:
            runtime_config = override_app_config(
                base_runtime_config,
                reference_equity_usd=scenario.reference_equity_usd,
                pod_a_default_leverage=scenario.default_leverage,
                pod_a_max_leverage=scenario.max_leverage,
                pod_a_risk_per_trade_pct=scenario.risk_per_trade_pct,
            )
            backtest = PodABacktestRunner(runtime_config).run_jsonl(snapshot_dir_path)
            scenario_results.append(
                ReplayScenarioResult(
                    scenario=scenario,
                    backtest=backtest,
                )
            )

        recommended = self._recommended_scenario_name(scenario_results)
        result = ArchiveReplaySweepResult(
            data_dir=str(data_dir),
            dates=dates,
            coins=coins,
            snapshot_dir=str(snapshot_dir_path),
            snapshot_records_written=snapshot_records_written,
            snapshot_files_written=snapshot_files_written,
            scenarios=scenario_results,
            recommended_scenario=recommended,
            report_path=str(report_output) if report_output is not None else None,
        )
        if report_output is not None:
            report_path = Path(report_output)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
        return result

    def _recommended_scenario_name(
        self,
        scenarios: list[ReplayScenarioResult],
    ) -> str | None:
        if not scenarios:
            return None
        ordered = sorted(
            scenarios,
            key=lambda item: (
                item.backtest.realized_pnl_usd,
                -item.backtest.max_drawdown_usd,
                -item.backtest.max_open_expected_loss_usd,
            ),
            reverse=True,
        )
        return ordered[0].scenario.name


def default_scenarios(
    *,
    reference_equity_usd: float,
    leverages: list[float],
    risk_per_trade_pct: float | None = None,
) -> list[ReplayScenario]:
    scenarios: list[ReplayScenario] = []
    for leverage in leverages:
        label = f"{int(leverage) if leverage.is_integer() else leverage}x"
        scenarios.append(
            ReplayScenario(
                name=f"{int(reference_equity_usd)}usd_{label}",
                reference_equity_usd=reference_equity_usd,
                default_leverage=leverage,
                max_leverage=leverage,
                risk_per_trade_pct=risk_per_trade_pct,
            )
        )
    return scenarios


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare several Pod A replay scenarios on one archive conversion",
    )
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--data-dir", default="data/server_archive")
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to")
    parser.add_argument("--coins", required=True, help="Comma-separated list, e.g. BTC,ETH,SOL,HYPE")
    parser.add_argument("--snapshot-dir")
    parser.add_argument("--report-output")
    parser.add_argument("--bucket-ms", type=int, default=60_000)
    parser.add_argument("--reference-equity-usd", type=float, default=500.0)
    parser.add_argument("--leverages", default=DEFAULT_REPLAY_LEVERAGES)
    parser.add_argument("--pod-a-risk-per-trade-pct", type=float)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = load_config(args.config)
    leverages = [
        float(chunk.strip())
        for chunk in args.leverages.split(",")
        if chunk.strip()
    ]
    result = ArchiveReplaySweepRunner(config).run(
        data_dir=args.data_dir,
        dates=parse_dates(date_from=args.date_from, date_to=args.date_to),
        coins=[coin.strip().upper() for coin in args.coins.split(",") if coin.strip()],
        scenarios=default_scenarios(
            reference_equity_usd=args.reference_equity_usd,
            leverages=leverages,
            risk_per_trade_pct=args.pod_a_risk_per_trade_pct,
        ),
        snapshot_dir=args.snapshot_dir,
        report_output=args.report_output,
        bucket_ms=args.bucket_ms,
        use_live_asset_caps=True,
    )
    print(f"dates={result.dates}")
    print(f"coins={result.coins}")
    print(f"snapshot_dir={result.snapshot_dir}")
    print(f"snapshot_files_written={result.snapshot_files_written}")
    print(f"snapshot_records_written={result.snapshot_records_written}")
    print(f"recommended_scenario={result.recommended_scenario}")
    for scenario_result in result.scenarios:
        backtest = scenario_result.backtest
        print(
            "scenario="
            f"{scenario_result.scenario.name}"
            f" realized_pnl_usd={backtest.realized_pnl_usd}"
            f" realized_pnl_pct={scenario_result.realized_pnl_pct}"
            f" max_drawdown_usd={backtest.max_drawdown_usd}"
            f" max_drawdown_pct={scenario_result.max_drawdown_pct}"
            f" max_open_expected_loss_usd={backtest.max_open_expected_loss_usd}"
            f" max_open_expected_loss_pct={scenario_result.max_open_expected_loss_pct}"
            f" signal_count={backtest.signal_count}"
            f" accepted_count={backtest.accepted_count}"
            f" closed_trade_count={backtest.closed_trade_count}"
        )
    if result.report_path:
        print(f"report_path={result.report_path}")


if __name__ == "__main__":
    main()
