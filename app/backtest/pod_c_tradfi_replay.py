from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from app.backtest.pod_c_runner import PodCBacktestRunner
from app.live.asset_ctx_enricher import SnapshotAssetCtxEnricher
from app.settings import AppConfig, load_config


@dataclass(slots=True)
class PodCTradfiReplayResult:
    input_path: str
    enriched_input_path: str | None
    symbols: list[str] | None
    backtest: dict[str, object]
    report_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class PodCTradfiReplayRunner:
    """Runs the new Tradfi Pod C offline, optionally enriching snapshots with assetCtx history first."""

    def __init__(
        self,
        *,
        config_loader: Callable[[str | Path], AppConfig] = load_config,
        backtest_runner_factory: Callable[[AppConfig], PodCBacktestRunner] = PodCBacktestRunner,
        enricher_factory: Callable[[], SnapshotAssetCtxEnricher] = SnapshotAssetCtxEnricher,
    ) -> None:
        self._config_loader = config_loader
        self._backtest_runner_factory = backtest_runner_factory
        self._enricher_factory = enricher_factory

    def run(
        self,
        *,
        config_path: str | Path,
        input_path: str | Path,
        output_path: str | Path | None = None,
        report_output: str | Path | None = None,
        funding_history_path: str | Path | None = None,
        symbols: list[str] | None = None,
        funding_max_age_seconds: float = 900.0,
    ) -> PodCTradfiReplayResult:
        config = self._config_loader(config_path)
        selected_symbols = None if symbols is None else [symbol.upper() for symbol in symbols]
        runtime_input = Path(input_path)
        enriched_input_path: str | None = None

        if funding_history_path is not None:
            temp_path = Path(tempfile.mkdtemp(prefix="trident_pod_c_tradfi_")) / "enriched.jsonl"
            self._enricher_factory().enrich(
                input_path=input_path,
                funding_history_path=funding_history_path,
                output_path=temp_path,
                symbols=selected_symbols,
                funding_max_age_seconds=funding_max_age_seconds,
            )
            runtime_input = temp_path
            enriched_input_path = str(temp_path)

        result = self._backtest_runner_factory(config).run_jsonl(
            runtime_input,
            output_path=output_path,
        )
        payload = PodCTradfiReplayResult(
            input_path=str(input_path),
            enriched_input_path=enriched_input_path,
            symbols=selected_symbols,
            backtest=result.backtest,
            report_path=str(report_output) if report_output is not None else None,
        )
        if report_output is not None:
            report_path = Path(report_output)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(payload.to_dict(), indent=2) + "\n",
                encoding="utf-8",
            )
        return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay the Tradfi Pod C offline on TRIDENT snapshots")
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output")
    parser.add_argument("--report-output")
    parser.add_argument("--funding-history")
    parser.add_argument("--symbols", help="Optional comma-separated symbol list")
    parser.add_argument("--funding-max-age-seconds", type=float, default=900.0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    symbols = [item.strip().upper() for item in (args.symbols or "").split(",") if item.strip()]
    result = PodCTradfiReplayRunner().run(
        config_path=args.config,
        input_path=args.input,
        output_path=args.output,
        report_output=args.report_output,
        funding_history_path=args.funding_history,
        symbols=symbols or None,
        funding_max_age_seconds=args.funding_max_age_seconds,
    )
    print(f"input_path={result.input_path}")
    print(f"enriched_input_path={result.enriched_input_path}")
    print(f"records_processed={result.backtest.get('records_processed')}")
    print(f"signal_count={result.backtest.get('signal_count')}")
    print(f"accepted_count={result.backtest.get('accepted_count')}")
    print(f"closed_trade_count={result.backtest.get('closed_trade_count')}")
    print(f"realized_pnl_usd={result.backtest.get('realized_pnl_usd')}")
    if result.report_path is not None:
        print(f"report_path={result.report_path}")


if __name__ == "__main__":
    main()
