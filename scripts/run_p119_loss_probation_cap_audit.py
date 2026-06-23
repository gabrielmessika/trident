#!/usr/bin/env python3
"""P119 / A-PNL-08 loss-probation cap-only audit for Pod A.

Research-only. It uses the P117 compact journal and applies a counterfactual
notional cap to already-opened trades after negative rolling symbol/setup
history. This is a fast first-pass audit; it does not replace a full-bot replay
if a variant looks promotable.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_p105_a_grade_replay import parse_timestamp, utc_stamp


DEFAULT_P117_JOURNAL = (
    "server-data/replay_reports/p117_fill_quality_audit_20260623/"
    "pod_a_fill_quality_journal.jsonl"
)


@dataclass(slots=True)
class TradeRow:
    trade_id: str
    opened_at: str
    closed_at: str
    symbol: str
    setup: str
    close_reason: str
    target_notional_usd: float
    original_pnl_usd: float


@dataclass(slots=True)
class AdjustedTradeRow:
    trade_id: str
    closed_at: str
    symbol: str
    setup: str
    close_reason: str
    rolling_trades_before: int
    rolling_pnl_before_usd: float
    rolling_expectancy_before_usd: float
    rolling_profit_factor_before: float
    cap_multiplier: float
    cap_reason: str
    original_pnl_usd: float
    adjusted_pnl_usd: float
    pnl_delta_usd: float


@dataclass(slots=True)
class SummaryRow:
    period: str
    trades: int
    capped_trades: int
    original_pnl_usd: float
    adjusted_pnl_usd: float
    delta_usd: float
    original_profit_factor: float | None
    adjusted_profit_factor: float | None
    capped_original_pnl_usd: float
    capped_winner_pnl_usd: float
    capped_loser_pnl_usd: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--p117-journal", default=DEFAULT_P117_JOURNAL)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--rolling-lookback", type=int, default=4)
    parser.add_argument("--min-closed-trades", type=int, default=2)
    parser.add_argument("--max-rolling-pnl-usd", type=float, default=-2.0)
    parser.add_argument("--max-profit-factor", type=float, default=0.80)
    parser.add_argument("--rehab-min-profit-factor", type=float, default=1.05)
    parser.add_argument("--rehab-min-expectancy-usd", type=float, default=0.0)
    parser.add_argument("--cap-multiplier", type=float, default=0.50)
    parser.add_argument("--split-date", default="2026-06-03")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir or f"server-data/replay_reports/p119_loss_probation_cap_{utc_stamp()}")
    output_dir.mkdir(parents=True, exist_ok=True)

    trades = load_trades(Path(args.p117_journal))
    adjusted = apply_loss_probation(
        trades,
        rolling_lookback=int(args.rolling_lookback),
        min_closed_trades=int(args.min_closed_trades),
        max_rolling_pnl_usd=float(args.max_rolling_pnl_usd),
        max_profit_factor=float(args.max_profit_factor),
        rehab_min_profit_factor=float(args.rehab_min_profit_factor),
        rehab_min_expectancy_usd=float(args.rehab_min_expectancy_usd),
        cap_multiplier=float(args.cap_multiplier),
    )
    summaries = summarize_periods(adjusted, split_date=str(args.split_date))
    payload = {
        "generated_at": utc_stamp(),
        "decision": "research_only_no_live_change",
        "p117_journal": str(args.p117_journal),
        "parameters": {
            "rolling_lookback": int(args.rolling_lookback),
            "min_closed_trades": int(args.min_closed_trades),
            "max_rolling_pnl_usd": float(args.max_rolling_pnl_usd),
            "max_profit_factor": float(args.max_profit_factor),
            "rehab_min_profit_factor": float(args.rehab_min_profit_factor),
            "rehab_min_expectancy_usd": float(args.rehab_min_expectancy_usd),
            "cap_multiplier": float(args.cap_multiplier),
            "split_date": str(args.split_date),
        },
        "summary": [asdict(row) for row in summaries],
    }
    write_csv(output_dir / "trade_adjustments.csv", adjusted)
    write_csv(output_dir / "scenario_summary.csv", summaries)
    write_json(output_dir / "p119_loss_probation_cap_audit.json", payload)
    write_markdown(output_dir / "p119_loss_probation_cap_audit.md", payload, summaries, adjusted)
    print(output_dir)


def load_trades(path: Path) -> list[TradeRow]:
    trades: list[TradeRow] = []
    trade_index = 0
    for record in jsonl_records(path):
        if record.get("event_type") != "trade_close":
            continue
        raw = record.get("trade")
        if not isinstance(raw, dict):
            continue
        closed_at = str(raw.get("closed_at") or "")
        if parse_timestamp(closed_at) is None:
            continue
        trade_index += 1
        trades.append(
            TradeRow(
                trade_id=f"trade_{trade_index:04d}",
                opened_at=str(raw.get("opened_at") or ""),
                closed_at=closed_at,
                symbol=str(raw.get("symbol") or "").upper(),
                setup=str(raw.get("setup") or ""),
                close_reason=str(raw.get("close_reason") or ""),
                target_notional_usd=float(raw.get("target_notional_usd") or 0.0),
                original_pnl_usd=float(raw.get("pnl_usd") or 0.0),
            )
        )
    return trades


def apply_loss_probation(
    trades: list[TradeRow],
    *,
    rolling_lookback: int,
    min_closed_trades: int,
    max_rolling_pnl_usd: float,
    max_profit_factor: float,
    rehab_min_profit_factor: float,
    rehab_min_expectancy_usd: float,
    cap_multiplier: float,
) -> list[AdjustedTradeRow]:
    histories: dict[tuple[str, str], deque[float]] = defaultdict(
        lambda: deque(maxlen=max(rolling_lookback, 1))
    )
    adjusted: list[AdjustedTradeRow] = []
    for trade in trades:
        key = (trade.symbol, trade.setup)
        history = list(histories[key])
        stats = rolling_stats(history)
        multiplier, reason = probation_multiplier(
            stats,
            min_closed_trades=max(min_closed_trades, 1),
            max_rolling_pnl_usd=max_rolling_pnl_usd,
            max_profit_factor=max_profit_factor,
            rehab_min_profit_factor=rehab_min_profit_factor,
            rehab_min_expectancy_usd=rehab_min_expectancy_usd,
            cap_multiplier=cap_multiplier,
        )
        adjusted_pnl = trade.original_pnl_usd * multiplier
        adjusted.append(
            AdjustedTradeRow(
                trade_id=trade.trade_id,
                closed_at=trade.closed_at,
                symbol=trade.symbol,
                setup=trade.setup,
                close_reason=trade.close_reason,
                rolling_trades_before=int(stats["trades"]),
                rolling_pnl_before_usd=round(float(stats["pnl_usd"]), 6),
                rolling_expectancy_before_usd=round(float(stats["expectancy_usd"]), 6),
                rolling_profit_factor_before=round(float(stats["profit_factor"]), 6),
                cap_multiplier=round(multiplier, 6),
                cap_reason=reason,
                original_pnl_usd=round(trade.original_pnl_usd, 6),
                adjusted_pnl_usd=round(adjusted_pnl, 6),
                pnl_delta_usd=round(adjusted_pnl - trade.original_pnl_usd, 6),
            )
        )
        histories[key].append(trade.original_pnl_usd)
    return adjusted


def rolling_stats(history: list[float]) -> dict[str, float | int]:
    if not history:
        return {"trades": 0, "pnl_usd": 0.0, "expectancy_usd": 0.0, "profit_factor": 0.0}
    gains = sum(value for value in history if value > 0.0)
    losses = -sum(value for value in history if value < 0.0)
    if losses > 0.0:
        profit_factor = gains / losses
    elif gains > 0.0:
        profit_factor = 999.0
    else:
        profit_factor = 0.0
    total = sum(history)
    return {
        "trades": len(history),
        "pnl_usd": total,
        "expectancy_usd": total / len(history),
        "profit_factor": profit_factor,
    }


def probation_multiplier(
    stats: dict[str, float | int],
    *,
    min_closed_trades: int,
    max_rolling_pnl_usd: float,
    max_profit_factor: float,
    rehab_min_profit_factor: float,
    rehab_min_expectancy_usd: float,
    cap_multiplier: float,
) -> tuple[float, str]:
    trades = int(stats.get("trades") or 0)
    if trades < min_closed_trades:
        return 1.0, "insufficient_history"
    expectancy = float(stats.get("expectancy_usd") or 0.0)
    profit_factor = float(stats.get("profit_factor") or 0.0)
    pnl = float(stats.get("pnl_usd") or 0.0)
    if expectancy > rehab_min_expectancy_usd and profit_factor >= rehab_min_profit_factor:
        return 1.0, "rehabilitated"
    if pnl <= max_rolling_pnl_usd or (expectancy < 0.0 and profit_factor <= max_profit_factor):
        return max(min(cap_multiplier, 1.0), 0.0), "loss_probation"
    return 1.0, "not_degraded"


def summarize_periods(adjusted: list[AdjustedTradeRow], *, split_date: str) -> list[SummaryRow]:
    return [
        summarize("all", adjusted),
        summarize("pre_split", [row for row in adjusted if row.closed_at[:10] < split_date]),
        summarize("post_split", [row for row in adjusted if row.closed_at[:10] >= split_date]),
    ]


def summarize(period: str, rows: list[AdjustedTradeRow]) -> SummaryRow:
    capped = [row for row in rows if row.cap_multiplier < 0.9999]
    original_pnls = [row.original_pnl_usd for row in rows]
    adjusted_pnls = [row.adjusted_pnl_usd for row in rows]
    capped_pnls = [row.original_pnl_usd for row in capped]
    return SummaryRow(
        period=period,
        trades=len(rows),
        capped_trades=len(capped),
        original_pnl_usd=round(sum(original_pnls), 6),
        adjusted_pnl_usd=round(sum(adjusted_pnls), 6),
        delta_usd=round(sum(adjusted_pnls) - sum(original_pnls), 6),
        original_profit_factor=profit_factor(original_pnls),
        adjusted_profit_factor=profit_factor(adjusted_pnls),
        capped_original_pnl_usd=round(sum(capped_pnls), 6),
        capped_winner_pnl_usd=round(sum(value for value in capped_pnls if value > 0.0), 6),
        capped_loser_pnl_usd=round(sum(value for value in capped_pnls if value < 0.0), 6),
    )


def profit_factor(pnls: list[float]) -> float | None:
    gains = sum(pnl for pnl in pnls if pnl > 0.0)
    losses = -sum(pnl for pnl in pnls if pnl < 0.0)
    if losses <= 0.0:
        return None
    return round(gains / losses, 6)


def jsonl_records(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def write_csv(path: Path, rows: list[Any]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(asdict(rows[0]).keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_markdown(
    path: Path,
    payload: dict[str, Any],
    summaries: list[SummaryRow],
    adjusted: list[AdjustedTradeRow],
) -> None:
    by_symbol: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {"trades": 0, "capped": 0, "delta": 0.0, "capped_original_pnl": 0.0}
    )
    for row in adjusted:
        bucket = by_symbol[row.symbol]
        bucket["trades"] = int(bucket["trades"]) + 1
        if row.cap_multiplier < 0.9999:
            bucket["capped"] = int(bucket["capped"]) + 1
            bucket["capped_original_pnl"] = float(bucket["capped_original_pnl"]) + row.original_pnl_usd
        bucket["delta"] = float(bucket["delta"]) + row.pnl_delta_usd
    lines = [
        "# P119 loss-probation cap audit Pod A",
        "",
        f"- generated_at: `{payload.get('generated_at')}`",
        "- decision: `research_only_no_live_change`",
        f"- p117_journal: `{payload.get('p117_journal')}`",
        f"- parameters: `{payload.get('parameters')}`",
        "",
        "## Summary",
        "",
        "| Period | Trades | Capped | Original PnL | Adjusted PnL | Delta | Original PF | Adjusted PF | Capped original PnL | Capped winners | Capped losers |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in summaries:
        lines.append(
            f"| `{row.period}` | {row.trades} | {row.capped_trades} | "
            f"{row.original_pnl_usd:.2f} | {row.adjusted_pnl_usd:.2f} | {row.delta_usd:+.2f} | "
            f"{fmt(row.original_profit_factor)} | {fmt(row.adjusted_profit_factor)} | "
            f"{row.capped_original_pnl_usd:.2f} | {row.capped_winner_pnl_usd:.2f} | "
            f"{row.capped_loser_pnl_usd:.2f} |"
        )
    lines.extend(["", "## By Symbol", ""])
    lines.append("| Symbol | Trades | Capped | Delta | Capped original PnL |")
    lines.append("| --- | ---: | ---: | ---: | ---: |")
    for symbol, row in sorted(by_symbol.items(), key=lambda item: float(item[1]["delta"])):
        lines.append(
            f"| `{symbol}` | {int(row['trades'])} | {int(row['capped'])} | "
            f"{float(row['delta']):+.2f} | {float(row['capped_original_pnl']):.2f} |"
        )
    lines.extend(
        [
            "",
            "## Lecture",
            "",
            "- Audit cap-only rapide sur trades effectivement ouverts; il ne modifie aucun chemin live.",
            "- Une variante positive ici doit encore passer un replay full-bot si elle devient candidate.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


if __name__ == "__main__":
    main()
