from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from app.backtest.pod_a_runner import PodABacktestRunner
from app.settings import AppConfig, load_config
from app.special_symbols_runtime import (
    SpecialSymbolsSelection,
    build_special_symbols_runtime_config,
)


@dataclass(slots=True)
class SpecialSymbolsBacktestResult:
    pod: str
    tradable_symbols: list[str]
    observe_only_symbols: list[str]
    observation_universe: list[str]
    backtest: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class SpecialSymbolsBacktestRunner:
    """Replays the isolated TAO/XPL/BIO sleeve with Pod A's validated engine."""

    def __init__(
        self,
        config: AppConfig,
        *,
        tradable_symbols: list[str] | None = None,
        observe_only_symbols: list[str] | None = None,
    ) -> None:
        runtime_config, selection = build_special_symbols_runtime_config(
            config,
            tradable_symbols=tradable_symbols,
            observe_only_symbols=observe_only_symbols,
        )
        self.config = runtime_config
        self.selection = selection
        self.runner = PodABacktestRunner(runtime_config)

    def run_jsonl(
        self,
        input_path: str | Path,
        *,
        journal_output: str | Path | None = None,
    ) -> SpecialSymbolsBacktestResult:
        backtest = self.runner.run_jsonl(input_path, output_path=journal_output)
        return SpecialSymbolsBacktestResult(
            pod="special_symbols",
            tradable_symbols=list(self.selection.tradable_symbols),
            observe_only_symbols=list(self.selection.observe_only_symbols),
            observation_universe=list(self.selection.observation_universe),
            backtest=asdict(backtest),
        )


def _render_markdown(result: SpecialSymbolsBacktestResult) -> str:
    backtest = result.backtest
    lines = [
        "# Special Symbols Backtest",
        "",
        f"- Pod: `{result.pod}`",
        f"- Tradable symbols: `{', '.join(result.tradable_symbols) or '-'}`",
        f"- Observe-only symbols: `{', '.join(result.observe_only_symbols) or '-'}`",
        f"- Observation universe: `{', '.join(result.observation_universe) or '-'}`",
        f"- Records processed: `{backtest.get('records_processed', 0)}`",
        f"- Closed trades: `{backtest.get('closed_trade_count', 0)}`",
        f"- Realized PnL USD: `{float(backtest.get('realized_pnl_usd', 0.0) or 0.0):.2f}`",
        f"- Fees USD: `{float(backtest.get('fees_usd', 0.0) or 0.0):.2f}`",
        f"- Max drawdown USD: `{float(backtest.get('max_drawdown_usd', 0.0) or 0.0):.2f}`",
        "",
        "## PnL By Symbol",
        "",
    ]
    pnl_by_symbol = backtest.get("pnl_by_symbol", {})
    if isinstance(pnl_by_symbol, dict) and pnl_by_symbol:
        for symbol, pnl in sorted(
            ((str(k), float(v or 0.0)) for k, v in pnl_by_symbol.items()),
            key=lambda item: item[1],
            reverse=True,
        ):
            lines.append(f"- `{symbol}`: `{pnl:.2f} USD`")
    else:
        lines.append("- No closed trades.")
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backtest the isolated special-symbols sleeve intended to replace Pod B."
    )
    parser.add_argument("--config", default="config/trident_special_symbols_core_shadow.toml")
    parser.add_argument("--input", required=True)
    parser.add_argument("--journal-output")
    parser.add_argument("--json-output")
    parser.add_argument("--md-output")
    parser.add_argument("--tradable-symbols")
    parser.add_argument("--observe-only-symbols")
    return parser


def _parse_symbol_list(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def main() -> None:
    args = build_parser().parse_args()
    runner = SpecialSymbolsBacktestRunner(
        load_config(args.config),
        tradable_symbols=_parse_symbol_list(args.tradable_symbols),
        observe_only_symbols=_parse_symbol_list(args.observe_only_symbols),
    )
    result = runner.run_jsonl(
        args.input,
        journal_output=args.journal_output,
    )
    if args.json_output:
        json_path = Path(args.json_output)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
    if args.md_output:
        md_path = Path(args.md_output)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(_render_markdown(result), encoding="utf-8")
    print(f"pod={result.pod}")
    print(f"tradable_symbols={','.join(result.tradable_symbols)}")
    print(f"observe_only_symbols={','.join(result.observe_only_symbols)}")
    print(f"records_processed={result.backtest.get('records_processed')}")
    print(f"closed_trade_count={result.backtest.get('closed_trade_count')}")
    print(f"realized_pnl_usd={result.backtest.get('realized_pnl_usd')}")


if __name__ == "__main__":
    main()
