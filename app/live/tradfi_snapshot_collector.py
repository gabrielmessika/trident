from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
from dataclasses import replace
from pathlib import Path
from typing import Callable

from app.live.collector import HyperliquidLiveCollector
from app.live.funding_collector import FundingHistoryCollector
from app.settings import AppConfig, load_config


@dataclass(slots=True)
class TradfiCollectionResult:
    symbols: list[str]
    snapshot_output_dir: str
    snapshot_records_written: int
    collector: dict[str, object]
    funding_output_path: str | None = None
    funding_polls_completed: int = 0
    funding_records_written: int = 0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class TradfiSnapshotCollectionRunner:
    """Collects dedicated Tradfi TRIDENT snapshots, with optional parallel assetCtx polling."""

    def __init__(
        self,
        config: AppConfig,
        *,
        collector_factory: Callable[[AppConfig, list[str]], HyperliquidLiveCollector] | None = None,
        funding_collector_factory: Callable[[AppConfig], FundingHistoryCollector] | None = None,
    ) -> None:
        self.config = config
        self._collector_factory = collector_factory or (
            lambda runtime_config, selected: HyperliquidLiveCollector(runtime_config, coins=selected)
        )
        self._funding_collector_factory = funding_collector_factory or FundingHistoryCollector

    def default_symbols(self) -> list[str]:
        return list(self.config.pod_c.symbols)

    async def run(
        self,
        *,
        symbols: list[str] | None = None,
        snapshot_output_dir: str | Path | None = None,
        max_runtime_seconds: float | None = None,
        max_messages: int | None = None,
        funding_output_path: str | Path | None = None,
        funding_poll_seconds: float = 60.0,
    ) -> TradfiCollectionResult:
        selected = [
            str(symbol).strip().upper()
            for symbol in (symbols or self.default_symbols())
            if str(symbol).strip()
        ]
        runtime_config = self.config
        if snapshot_output_dir is not None:
            runtime_config = replace(
                runtime_config,
                hyperliquid=replace(
                    runtime_config.hyperliquid,
                    snapshot_output_dir=str(snapshot_output_dir),
                ),
            )

        collector = self._collector_factory(runtime_config, selected)
        funding_collector = (
            self._funding_collector_factory(runtime_config)
            if funding_output_path is not None
            else None
        )

        funding_polls_completed = 0
        funding_records_written = 0
        stop_event = asyncio.Event()

        async def poll_funding_history() -> None:
            nonlocal funding_polls_completed, funding_records_written
            if funding_collector is None:
                return
            while not stop_event.is_set():
                records = funding_collector.collect_once(
                    output_path=funding_output_path,
                    symbols=selected,
                )
                funding_polls_completed += 1
                funding_records_written += len(records)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=max(funding_poll_seconds, 0.1))
                except asyncio.TimeoutError:
                    continue

        funding_task = (
            asyncio.create_task(poll_funding_history())
            if funding_collector is not None
            else None
        )
        try:
            stats = await collector.run(
                max_runtime_seconds=max_runtime_seconds,
                max_messages=max_messages,
            )
        finally:
            stop_event.set()
            if funding_task is not None:
                await asyncio.gather(funding_task, return_exceptions=True)

        return TradfiCollectionResult(
            symbols=selected,
            snapshot_output_dir=runtime_config.hyperliquid.snapshot_output_dir,
            snapshot_records_written=stats.snapshots_written,
            collector={
                "messages_processed": stats.messages_processed,
                "snapshots_written": stats.snapshots_written,
                "reconnect_count": stats.reconnect_count,
                "heartbeat_count": stats.heartbeat_count,
                "pong_count": stats.pong_count,
                "timeout_count": stats.timeout_count,
                "api_error_count": stats.api_error_count,
                "rate_limit_error_count": stats.rate_limit_error_count,
                "last_error": stats.last_error,
            },
            funding_output_path=str(funding_output_path) if funding_output_path is not None else None,
            funding_polls_completed=funding_polls_completed,
            funding_records_written=funding_records_written,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect dedicated Tradfi snapshots for Pod C validation")
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--symbols", help="Optional comma-separated symbol list")
    parser.add_argument("--snapshot-output-dir")
    parser.add_argument("--max-runtime-seconds", type=float)
    parser.add_argument("--max-messages", type=int)
    parser.add_argument("--funding-output")
    parser.add_argument("--funding-poll-seconds", type=float, default=60.0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = load_config(args.config)
    symbols = [item.strip().upper() for item in (args.symbols or "").split(",") if item.strip()]
    result = asyncio.run(
        TradfiSnapshotCollectionRunner(config).run(
            symbols=symbols or None,
            snapshot_output_dir=args.snapshot_output_dir,
            max_runtime_seconds=args.max_runtime_seconds,
            max_messages=args.max_messages,
            funding_output_path=args.funding_output,
            funding_poll_seconds=args.funding_poll_seconds,
        )
    )
    print(f"symbols={result.symbols}")
    print(f"snapshot_output_dir={result.snapshot_output_dir}")
    print(f"snapshot_records_written={result.snapshot_records_written}")
    print(f"messages_processed={result.collector['messages_processed']}")
    print(f"funding_polls_completed={result.funding_polls_completed}")
    print(f"funding_records_written={result.funding_records_written}")
    if result.funding_output_path is not None:
        print(f"funding_output_path={result.funding_output_path}")


if __name__ == "__main__":
    main()
