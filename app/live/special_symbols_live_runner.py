from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from app.live.pod_a_live_runner import PodALiveRunner
from app.settings import AppConfig, load_config
from app.special_symbols_runtime import (
    SpecialSymbolsSelection,
    build_special_symbols_runtime_config,
)

logger = logging.getLogger(__name__)


class SpecialSymbolsLiveRunner:
    """Runs the isolated TAO/XPL/BIO sleeve as a dedicated shadow pod."""

    def __init__(
        self,
        config: AppConfig,
        *,
        tradable_symbols: list[str] | None = None,
        observe_only_symbols: list[str] | None = None,
        use_live_asset_caps: bool = False,
    ) -> None:
        runtime_config, selection = build_special_symbols_runtime_config(
            config,
            tradable_symbols=tradable_symbols,
            observe_only_symbols=observe_only_symbols,
        )
        self.selection: SpecialSymbolsSelection = selection
        self.runner = PodALiveRunner(
            runtime_config,
            coins=list(selection.observation_universe),
            use_live_asset_caps=use_live_asset_caps,
            runtime_name="special_symbols",
            status_path=Path("logs/special_symbols_live_status.json"),
            supervisor_profile="trident-live-special-symbols",
            signal_source="special_symbols_live_signal",
            filtered_source="special_symbols_live_filtered",
            trade_source="special_symbols_live_trade",
            review_label="Special Symbols",
        )

    async def run(
        self,
        *,
        max_runtime_seconds: float | None = None,
        max_messages: int | None = None,
        journal_path: str | Path | None = None,
    ) -> dict[str, object]:
        result = await self.runner.run(
            max_runtime_seconds=max_runtime_seconds,
            max_messages=max_messages,
            journal_path=journal_path,
        )
        result["pod"] = "special_symbols"
        result["tradable_symbols"] = list(self.selection.tradable_symbols)
        result["observe_only_symbols"] = list(self.selection.observe_only_symbols)
        result["observation_universe"] = list(self.selection.observation_universe)
        return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the isolated special-symbols sleeve intended to replace Pod B."
    )
    parser.add_argument("--config", default="config/trident_special_symbols_core_shadow.toml")
    parser.add_argument("--max-runtime-seconds", type=float)
    parser.add_argument("--max-messages", type=int)
    parser.add_argument("--journal-output")
    parser.add_argument("--tradable-symbols")
    parser.add_argument("--observe-only-symbols")
    return parser


def _parse_symbol_list(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip().upper() for item in value.split(",") if item.strip()]


async def _run_from_args() -> None:
    args = build_parser().parse_args()
    runner = SpecialSymbolsLiveRunner(
        load_config(args.config),
        tradable_symbols=_parse_symbol_list(args.tradable_symbols),
        observe_only_symbols=_parse_symbol_list(args.observe_only_symbols),
        use_live_asset_caps=True,
    )
    result = await runner.run(
        max_runtime_seconds=args.max_runtime_seconds,
        max_messages=args.max_messages,
        journal_path=args.journal_output,
    )
    for key in (
        "records_processed",
        "signal_count",
        "accepted_count",
        "rejected_count",
        "opened_count",
        "closed_trade_count",
        "realized_pnl_usd",
    ):
        print(f"{key}={result[key]}")
    print(f"tradable_symbols={','.join(result['tradable_symbols'])}")
    print(f"observe_only_symbols={','.join(result['observe_only_symbols'])}")
    if result.get("journal_path"):
        print(f"journal_path={result['journal_path']}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_run_from_args())


if __name__ == "__main__":
    main()
