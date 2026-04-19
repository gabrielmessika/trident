from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from app.backtest.full_bot_replay import FullBotBacktestRunner
from app.backtest.pod_a_runner import PodABacktestRunner
from app.backtest.pod_c_runner import PodCBacktestRunner
from app.backtest.special_symbols_slot_runner import SpecialSymbolsSlotBacktestRunner
from app.settings import AppConfig, load_config


def _merge_blocked_symbols(config: AppConfig, reserved_symbols: list[str]) -> AppConfig:
    existing = {str(symbol).strip().upper() for symbol in config.pod_a.blocked_symbols}
    merged = list(existing)
    for symbol in reserved_symbols:
        normalized = str(symbol).strip().upper()
        if normalized and normalized not in existing:
            existing.add(normalized)
            merged.append(normalized)
    return replace(
        config,
        pod_a=replace(config.pod_a, blocked_symbols=merged),
        pod_b=replace(config.pod_b, enabled=False),
    )


@dataclass(slots=True)
class FullBotOldBVsSpecialCompareResult:
    input_path: str
    compare_config: str
    special_config: str
    reserved_symbols: list[str]
    old_pod_b_full_bot: dict[str, object]
    special_slot_combo: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _sum_by_date(*mappings: dict[str, float]) -> dict[str, float]:
    dates = sorted({date for mapping in mappings for date in mapping})
    return {
        date: round(sum(float(mapping.get(date, 0.0) or 0.0) for mapping in mappings), 2)
        for date in dates
    }


def _build_special_combo(
    *,
    main_config: AppConfig,
    special_config: AppConfig,
    reserved_symbols: list[str],
    input_path: str | Path,
) -> dict[str, object]:
    blocked_config = _merge_blocked_symbols(main_config, reserved_symbols)
    pod_a = asdict(PodABacktestRunner(blocked_config).run_jsonl(input_path))
    pod_c = PodCBacktestRunner(main_config).run_jsonl(input_path).backtest
    special = SpecialSymbolsSlotBacktestRunner(
        main_config,
        special_config,
        tradable_symbols=reserved_symbols,
    ).run_jsonl(input_path).to_dict()
    total_realized = round(
        float(pod_a.get("realized_pnl_usd", 0.0) or 0.0)
        + float(special["backtest"].get("realized_pnl_usd", 0.0) or 0.0)
        + float(pod_c.get("realized_pnl_usd", 0.0) or 0.0),
        2,
    )
    total_fees = round(
        float(pod_a.get("fees_usd", 0.0) or 0.0)
        + float(special["backtest"].get("fees_usd", 0.0) or 0.0)
        + float(pod_c.get("fees_usd", 0.0) or 0.0),
        6,
    )
    total_closed = (
        int(pod_a.get("closed_trade_count", 0) or 0)
        + int(special["backtest"].get("closed_trade_count", 0) or 0)
        + int(pod_c.get("closed_trade_count", 0) or 0)
    )
    pnl_by_date = _sum_by_date(
        {str(k): float(v or 0.0) for k, v in (pod_a.get("pnl_by_date", {}) or {}).items()},
        {
            str(k): float(v or 0.0)
            for k, v in (special["backtest"].get("pnl_by_date", {}) or {}).items()
        },
        {str(k): float(v or 0.0) for k, v in (pod_c.get("pnl_by_date", {}) or {}).items()},
    )
    return {
        "pod_a_blocked": pod_a,
        "special_slot": special,
        "pod_c": pod_c,
        "total_realized_pnl_usd": total_realized,
        "directional_fees_usd": total_fees,
        "total_closed_trade_count": total_closed,
        "pnl_by_date": pnl_by_date,
        "notes": [
            "Pod A is replayed with reserved symbols blocked.",
            "The special pod uses the Pod B capital slot with the compare config allocations.",
            "Pod C is unchanged.",
        ],
    }


def _render_markdown(payload: FullBotOldBVsSpecialCompareResult) -> str:
    old_run = payload.old_pod_b_full_bot
    special = payload.special_slot_combo
    lines = [
        "# Old Pod B Vs Special Slot Compare",
        "",
        f"- Input: `{payload.input_path}`",
        f"- Compare config: `{payload.compare_config}`",
        f"- Special config: `{payload.special_config}`",
        f"- Reserved symbols: `{', '.join(payload.reserved_symbols)}`",
        "",
        "| Scenario | Total PnL USD | Fees USD | Closed trades |",
        "|---|---:|---:|---:|",
        f"| Old Pod B full bot | {float(old_run.get('total_realized_pnl_usd', 0.0) or 0.0):.2f} | {float(old_run.get('directional_fees_usd', 0.0) or 0.0):.2f} | {int(old_run.get('total_activity_count', 0) or 0)} |",
        f"| Special slot combo | {float(special.get('total_realized_pnl_usd', 0.0) or 0.0):.2f} | {float(special.get('directional_fees_usd', 0.0) or 0.0):.2f} | {int(special.get('total_closed_trade_count', 0) or 0)} |",
        "",
        "## Daily PnL",
        "",
        "| Date | Old Pod B Full Bot | Special Slot Combo | Delta |",
        "|---|---:|---:|---:|",
    ]
    old_pnl_by_date = {
        str(k): float(v or 0.0)
        for k, v in (old_run.get("pod_a", {}).get("pnl_by_date", {}) or {}).items()
    }
    old_pod_b_pnl_by_date = {
        str(k): float(v or 0.0)
        for k, v in (old_run.get("pod_b", {}).get("pnl_by_date", {}) or {}).items()
    }
    old_pod_c_pnl_by_date = {
        str(k): float(v or 0.0)
        for k, v in (old_run.get("pod_c", {}).get("pnl_by_date", {}) or {}).items()
    }
    old_total_by_date = _sum_by_date(old_pnl_by_date, old_pod_b_pnl_by_date, old_pod_c_pnl_by_date)
    special_total_by_date = {
        str(k): float(v or 0.0) for k, v in (special.get("pnl_by_date", {}) or {}).items()
    }
    for date in sorted(set(old_total_by_date) | set(special_total_by_date)):
        old_value = float(old_total_by_date.get(date, 0.0) or 0.0)
        special_value = float(special_total_by_date.get(date, 0.0) or 0.0)
        lines.append(f"| {date} | {old_value:.2f} | {special_value:.2f} | {(special_value - old_value):.2f} |")
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare old Pod B full bot vs the new special-symbols pod using the Pod B slot."
    )
    parser.add_argument("--compare-config", default="config/trident_compare_pod_b_slot.toml")
    parser.add_argument("--special-config", default="config/trident_special_symbols_taoxpl_shadow.toml")
    parser.add_argument("--input", required=True)
    parser.add_argument("--reserved-symbols", default="TAO,XPL")
    parser.add_argument("--json-output")
    parser.add_argument("--md-output")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    reserved_symbols = [item.strip().upper() for item in args.reserved_symbols.split(",") if item.strip()]
    compare_config = load_config(args.compare_config)
    special_config = load_config(args.special_config)
    old_pod_b = FullBotBacktestRunner(
        compare_config,
        force_enable_all_pods=False,
    ).run_jsonl(args.input).to_dict()
    special_combo = _build_special_combo(
        main_config=compare_config,
        special_config=special_config,
        reserved_symbols=reserved_symbols,
        input_path=args.input,
    )
    result = FullBotOldBVsSpecialCompareResult(
        input_path=str(args.input),
        compare_config=args.compare_config,
        special_config=args.special_config,
        reserved_symbols=reserved_symbols,
        old_pod_b_full_bot=old_pod_b,
        special_slot_combo=special_combo,
    )
    if args.json_output:
        json_path = Path(args.json_output)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
    if args.md_output:
        md_path = Path(args.md_output)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(_render_markdown(result), encoding="utf-8")
    print(json.dumps(result.to_dict(), indent=2))


if __name__ == "__main__":
    main()
