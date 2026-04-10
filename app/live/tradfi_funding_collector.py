from __future__ import annotations

import argparse
from pathlib import Path

from app.live.funding_collector import FundingCollectorStats, FundingHistoryCollector
from app.settings import AppConfig, load_config


class TradfiFundingCollectorRunner:
    """Collects dedicated funding/assetCtx history for the Pod C Tradfi universe."""

    def __init__(
        self,
        config: AppConfig,
        *,
        collector: FundingHistoryCollector | None = None,
    ) -> None:
        self.config = config
        self.collector = collector or FundingHistoryCollector(config)

    def default_symbols(self) -> list[str]:
        return [str(symbol).strip().upper() for symbol in self.config.pod_c.symbols if str(symbol).strip()]

    def run(
        self,
        *,
        output_path: str | Path = "data/funding_history/pod_c_tradfi.jsonl",
        status_path: str | Path | None = "logs/tradfi_funding_collector_status.json",
        poll_seconds: float = 60.0,
        iterations: int | None = None,
        symbols: list[str] | None = None,
        include_delisted: bool = False,
    ) -> FundingCollectorStats:
        selected = [
            str(symbol).strip().upper()
            for symbol in (symbols or self.default_symbols())
            if str(symbol).strip()
        ]
        return self.collector.run(
            output_path=output_path,
            status_path=status_path,
            poll_seconds=poll_seconds,
            iterations=iterations,
            symbols=selected,
            include_delisted=include_delisted,
            collector_name="tradfi_funding_collector",
            collector_label="Tradfi Funding Collector",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect dedicated funding/open-interest snapshots for Pod C Tradfi symbols")
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--output", default="data/funding_history/pod_c_tradfi.jsonl")
    parser.add_argument("--status-output", default="logs/tradfi_funding_collector_status.json")
    parser.add_argument("--symbols", help="Optional comma-separated symbol list")
    parser.add_argument("--poll-seconds", type=float, default=60.0)
    parser.add_argument("--iterations", type=int)
    parser.add_argument("--include-delisted", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = load_config(args.config)
    symbols = [item.strip().upper() for item in (args.symbols or "").split(",") if item.strip()]
    stats = TradfiFundingCollectorRunner(config).run(
        output_path=args.output,
        status_path=args.status_output,
        poll_seconds=args.poll_seconds,
        iterations=args.iterations,
        symbols=symbols or None,
        include_delisted=args.include_delisted,
    )
    print(f"polls_completed={stats.polls_completed}")
    print(f"records_written={stats.records_written}")
    print(f"output_path={stats.output_path}")
    print(f"status_path={stats.status_path}")


if __name__ == "__main__":
    main()
