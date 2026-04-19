from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from app.backtest.pod_a_runner import PodABacktestRunner
from app.backtest.pod_c_runner import PodCBacktestRunner
from app.backtest.special_symbols_runner import SpecialSymbolsBacktestRunner
from app.settings import AppConfig, load_config


@dataclass(slots=True)
class SpecialSymbolsReplacementCompareResult:
    input_path: str
    reserved_symbols: list[str]
    pod_a_blocked: dict[str, object]
    special_symbols: dict[str, object]
    pod_c: dict[str, object]
    combined_proxy_realized_pnl_usd: float
    combined_proxy_closed_trade_count: int
    notes: list[str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _build_pod_a_blocked_config(config: AppConfig, reserved_symbols: list[str]) -> AppConfig:
    existing = {str(symbol).strip().upper() for symbol in config.pod_a.blocked_symbols}
    merged = list(existing)
    for symbol in reserved_symbols:
        normalized = str(symbol).strip().upper()
        if normalized and normalized not in existing:
            existing.add(normalized)
            merged.append(normalized)
    return replace(
        config,
        pod_a=replace(
            config.pod_a,
            enabled=True,
            blocked_symbols=merged,
        ),
        pod_b=replace(config.pod_b, enabled=False),
        pod_c=replace(config.pod_c, enabled=False),
    )


class SpecialSymbolsReplacementCompareRunner:
    """Proxy compare: Pod A with reserved symbols removed + special pod + Pod C."""

    def __init__(
        self,
        main_config: AppConfig,
        special_config: AppConfig,
        *,
        reserved_symbols: list[str] | None = None,
    ) -> None:
        self.main_config = main_config
        self.special_config = special_config
        self.reserved_symbols = [
            str(symbol).strip().upper()
            for symbol in (reserved_symbols or ["TAO", "XPL", "BIO", "PENGU"])
            if str(symbol).strip()
        ]

    def run_jsonl(self, input_path: str | Path) -> SpecialSymbolsReplacementCompareResult:
        pod_a_result = PodABacktestRunner(
            _build_pod_a_blocked_config(self.main_config, self.reserved_symbols)
        ).run_jsonl(input_path)
        special_result = SpecialSymbolsBacktestRunner(self.special_config).run_jsonl(input_path)
        pod_c_result = PodCBacktestRunner(self.main_config).run_jsonl(input_path)

        pod_a = asdict(pod_a_result)
        special = special_result.to_dict()
        pod_c = special_result.to_dict()  # placeholder to keep typing clean
        pod_c = {
            "pod": "pod_c",
            **pod_c_result.backtest,
        }

        combined_pnl = round(
            float(pod_a.get("realized_pnl_usd", 0.0) or 0.0)
            + float(special["backtest"].get("realized_pnl_usd", 0.0) or 0.0)
            + float(pod_c.get("realized_pnl_usd", 0.0) or 0.0),
            4,
        )
        combined_trades = (
            int(pod_a.get("closed_trade_count", 0) or 0)
            + int(special["backtest"].get("closed_trade_count", 0) or 0)
            + int(pod_c.get("closed_trade_count", 0) or 0)
        )
        return SpecialSymbolsReplacementCompareResult(
            input_path=str(input_path),
            reserved_symbols=list(self.reserved_symbols),
            pod_a_blocked=pod_a,
            special_symbols=special,
            pod_c=pod_c,
            combined_proxy_realized_pnl_usd=combined_pnl,
            combined_proxy_closed_trade_count=combined_trades,
            notes=[
                "Proxy compare only: Pod A, special pod, and Pod C are backtested separately.",
                "Capital is not shared across the three sleeves in this comparison.",
                "Use this to validate direction before full supervisor integration.",
            ],
        )


def _render_markdown(result: SpecialSymbolsReplacementCompareResult) -> str:
    def _pnl(payload: dict[str, object], *, nested: bool = False) -> float:
        source = payload.get("backtest", {}) if nested else payload
        if not isinstance(source, dict):
            return 0.0
        return float(source.get("realized_pnl_usd", 0.0) or 0.0)

    def _trades(payload: dict[str, object], *, nested: bool = False) -> int:
        source = payload.get("backtest", {}) if nested else payload
        if not isinstance(source, dict):
            return 0
        return int(source.get("closed_trade_count", 0) or 0)

    lines = [
        "# Special Symbols Replacement Compare",
        "",
        f"- Input: `{result.input_path}`",
        f"- Reserved symbols: `{', '.join(result.reserved_symbols)}`",
        f"- Combined proxy realized PnL USD: `{result.combined_proxy_realized_pnl_usd:.2f}`",
        f"- Combined proxy closed trades: `{result.combined_proxy_closed_trade_count}`",
        "",
        "| Sleeve | Realized PnL USD | Closed trades |",
        "|---|---:|---:|",
        f"| Pod A blocked | {_pnl(result.pod_a_blocked):.2f} | {_trades(result.pod_a_blocked)} |",
        f"| Special symbols | {_pnl(result.special_symbols, nested=True):.2f} | {_trades(result.special_symbols, nested=True)} |",
        f"| Pod C | {_pnl(result.pod_c):.2f} | {_trades(result.pod_c)} |",
        "",
        "## Notes",
        "",
    ]
    for note in result.notes:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Proxy compare of replacing Pod B with an isolated special-symbols sleeve."
    )
    parser.add_argument("--main-config", default="config/trident.toml")
    parser.add_argument("--special-config", default="config/trident_special_symbols_core_shadow.toml")
    parser.add_argument("--input", required=True)
    parser.add_argument("--reserved-symbols", default="TAO,XPL,BIO,PENGU")
    parser.add_argument("--json-output")
    parser.add_argument("--md-output")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    reserved_symbols = [
        item.strip().upper() for item in args.reserved_symbols.split(",") if item.strip()
    ]
    runner = SpecialSymbolsReplacementCompareRunner(
        load_config(args.main_config),
        load_config(args.special_config),
        reserved_symbols=reserved_symbols,
    )
    result = runner.run_jsonl(args.input)
    if args.json_output:
        json_path = Path(args.json_output)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
    if args.md_output:
        md_path = Path(args.md_output)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(_render_markdown(result), encoding="utf-8")
    print(f"combined_proxy_realized_pnl_usd={result.combined_proxy_realized_pnl_usd}")
    print(f"combined_proxy_closed_trade_count={result.combined_proxy_closed_trade_count}")


if __name__ == "__main__":
    main()
