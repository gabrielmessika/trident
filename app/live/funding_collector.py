from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.hyperliquid.funding_client import HyperliquidFundingClient, FundingMarketSnapshot
from app.settings import AppConfig, load_config


@dataclass(slots=True)
class FundingCollectorStats:
    polls_completed: int = 0
    records_written: int = 0
    output_path: str | None = None


class FundingHistoryCollector:
    """Standalone funding/open-interest collector for Hydra research workflows."""

    def __init__(
        self,
        config: AppConfig,
        *,
        client: HyperliquidFundingClient | None = None,
    ) -> None:
        self.config = config
        self.client = client or HyperliquidFundingClient(config.hyperliquid)

    def collect_once(
        self,
        *,
        output_path: str | Path | None = None,
        symbols: list[str] | None = None,
        include_delisted: bool = False,
        timestamp: str | None = None,
    ) -> list[dict[str, object]]:
        observed = self.client.fetch_current_funding(
            symbols=symbols or self.config.hyperliquid.observation_universe,
            include_delisted=include_delisted,
        )
        collected_at = timestamp or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        records = [
            {
                "timestamp": collected_at,
                "symbol": item.symbol,
                "funding_rate": item.funding_rate,
                "open_interest": item.open_interest,
                "mark_px": item.mark_px,
                "oracle_px": item.oracle_px,
                "premium": item.premium,
                "day_ntl_vlm": item.day_ntl_vlm,
                "day_base_vlm": item.day_base_vlm,
                "source": "hyperliquid_funding_collector",
            }
            for item in observed
        ]
        if output_path is not None:
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record) + "\n")
        return records

    def run(
        self,
        *,
        output_path: str | Path,
        poll_seconds: float = 60.0,
        iterations: int | None = None,
        symbols: list[str] | None = None,
        include_delisted: bool = False,
    ) -> FundingCollectorStats:
        stats = FundingCollectorStats(output_path=str(output_path))
        remaining = iterations
        while remaining is None or remaining > 0:
            records = self.collect_once(
                output_path=output_path,
                symbols=symbols,
                include_delisted=include_delisted,
            )
            stats.polls_completed += 1
            stats.records_written += len(records)
            if remaining is not None:
                remaining -= 1
                if remaining <= 0:
                    break
            time.sleep(max(poll_seconds, 0.0))
        return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect standalone funding/open-interest snapshots")
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--output", default="data/funding_history/current.jsonl")
    parser.add_argument("--symbols", help="Optional comma-separated symbol list")
    parser.add_argument("--poll-seconds", type=float, default=60.0)
    parser.add_argument("--iterations", type=int)
    parser.add_argument("--include-delisted", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = load_config(args.config)
    collector = FundingHistoryCollector(config)
    symbols = [item.strip().upper() for item in (args.symbols or "").split(",") if item.strip()]
    stats = collector.run(
        output_path=args.output,
        poll_seconds=args.poll_seconds,
        iterations=args.iterations,
        symbols=symbols or None,
        include_delisted=args.include_delisted,
    )
    print(f"polls_completed={stats.polls_completed}")
    print(f"records_written={stats.records_written}")
    print(f"output_path={stats.output_path}")


if __name__ == "__main__":
    main()
