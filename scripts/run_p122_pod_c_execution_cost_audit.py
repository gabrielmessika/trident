#!/usr/bin/env python3
"""P122 / C-PNL-05 Pod C execution-cost audit.

Research-only. Reads full-bot replay reports and tests whether simple
spread/liquidity cost buckets would support a cap-only or repricing candidate.
This is not a maker-fill simulation.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_p105_a_grade_replay import utc_stamp


DEFAULT_REPORTS = [
    Path("server-data/replay_reports/external_reference_multisource_20260405_20260513_baseline.json"),
    Path("server-data/replay_reports/p116_early_failure_post_exit_20260622/full_bot_current_live_nodedupe.json"),
]


@dataclass(slots=True)
class ExecutionTrade:
    window: str
    trade_id: str
    symbol: str
    opened_at: str
    pnl_usd: float
    gross_pnl_usd: float
    fees_usd: float
    target_notional_usd: float
    fee_bps: float | None
    spread_bps: float | None
    entry_cost_bps: float | None
    activity_bucket: str
    trade_count_bucket: str
    bucket_notional_usd: float | None
    bucket_trade_count: float | None


@dataclass(slots=True)
class BucketSummary:
    window: str
    dimension: str
    bucket: str
    trades: int
    pnl_usd: float
    gross_pnl_usd: float
    fees_usd: float
    win_rate: float | None
    profit_factor: float | None
    avg_entry_cost_bps: float | None
    symbols: dict[str, int]


@dataclass(slots=True)
class PolicyOutcome:
    window: str
    policy: str
    action: str
    base_pnl_usd: float
    adjusted_pnl_usd: float
    delta_usd: float
    total_trades: int
    touched_trades: int
    touched_pnl_usd: float
    touched_winners: int
    touched_losers: int
    touched_symbols: dict[str, int]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        action="append",
        dest="reports",
        help="Full-bot replay JSON containing pod_c.closed_trade_log. Can be repeated.",
    )
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--cap-multiplier", type=float, default=0.50)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report_paths = [Path(item) for item in args.reports] if args.reports else DEFAULT_REPORTS
    output_dir = Path(args.output_dir or f"server-data/replay_reports/p122_pod_c_execution_cost_{utc_stamp()}")
    output_dir.mkdir(parents=True, exist_ok=True)

    trades = load_trades(report_paths)
    bucket_summaries = build_bucket_summaries(trades)
    policies = [
        "cap_spread_gte_1",
        "cap_spread_gte_2",
        "cap_entry_cost_gte_8",
        "cap_entry_cost_gte_9",
        "cap_spread_gte_1_not_high_activity",
        "cap_bucket_notional_lt100k",
        "cap_soft_trade_count",
    ]
    outcomes = [
        evaluate_policy(window, rows, policy, float(args.cap_multiplier))
        for window, rows in trades_by_window(trades).items()
        for policy in policies
    ]
    payload = {
        "generated_at": utc_stamp(),
        "decision": "research_only_no_live_change",
        "input_reports": [str(path) for path in report_paths],
        "parameters": {"cap_multiplier": float(args.cap_multiplier)},
        "trade_count": len(trades),
        "bucket_summaries": [asdict(row) for row in bucket_summaries],
        "policy_outcomes": [asdict(row) for row in outcomes],
        "notes": [
            "Fees are inferred from closed trades and are mostly constant around 7 bps in these reports.",
            "No passive/maker fill model is available here; policy rows are cap-only proxies, not live order placement logic.",
            "No live trading flag, deploy script, fetch script, or runtime config is changed by this audit.",
        ],
    }
    write_csv(output_dir / "bucket_summary.csv", bucket_summaries)
    write_csv(output_dir / "policy_outcomes.csv", outcomes)
    write_json(output_dir / "p122_pod_c_execution_cost_audit.json", payload)
    write_markdown(output_dir / "p122_pod_c_execution_cost_audit.md", payload, bucket_summaries, outcomes)
    print(output_dir)


def load_trades(report_paths: list[Path]) -> list[ExecutionTrade]:
    rows: list[ExecutionTrade] = []
    for report_path in report_paths:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        window = report_window(payload, report_path)
        pod_c = payload.get("pod_c") if isinstance(payload, dict) else {}
        trades = (pod_c or {}).get("closed_trade_log") if isinstance(pod_c, dict) else []
        for index, trade in enumerate(trades or [], start=1):
            if not isinstance(trade, dict):
                continue
            setup_details = trade.get("setup_details") if isinstance(trade.get("setup_details"), dict) else {}
            notional = to_float(trade.get("target_notional_usd"))
            fees = to_float(trade.get("fees_usd"))
            fee_bps = fees / notional * 10_000.0 if notional > 0 else None
            spread = optional_float(setup_details.get("spread_bps"))
            entry_cost = (
                (fee_bps or 0.0) + max(spread or 0.0, 0.0)
                if fee_bps is not None or spread is not None
                else None
            )
            rows.append(
                ExecutionTrade(
                    window=window,
                    trade_id=str(trade.get("trade_id") or f"{window}_{index:04d}"),
                    symbol=str(trade.get("symbol") or "").upper(),
                    opened_at=str(trade.get("opened_at") or ""),
                    pnl_usd=round(to_float(trade.get("pnl_usd")), 6),
                    gross_pnl_usd=round(to_float(trade.get("gross_pnl_usd")), 6),
                    fees_usd=round(fees, 6),
                    target_notional_usd=round(notional, 6),
                    fee_bps=round(fee_bps, 6) if fee_bps is not None else None,
                    spread_bps=round(spread, 6) if spread is not None else None,
                    entry_cost_bps=round(entry_cost, 6) if entry_cost is not None else None,
                    activity_bucket=str(setup_details.get("activity_bucket") or "unknown"),
                    trade_count_bucket=str(setup_details.get("trade_count_bucket") or "unknown"),
                    bucket_notional_usd=optional_float(setup_details.get("bucket_notional_usd")),
                    bucket_trade_count=optional_float(setup_details.get("bucket_trade_count")),
                )
            )
    return rows


def report_window(payload: dict[str, Any], path: Path) -> str:
    dates = payload.get("dates_covered") or []
    if isinstance(dates, list) and dates:
        return f"{dates[0]}_to_{dates[-1]}"
    first = str(payload.get("first_timestamp") or "")[:10]
    last = str(payload.get("last_timestamp") or "")[:10]
    return f"{first}_to_{last}".strip("_to_") or path.stem


def build_bucket_summaries(trades: list[ExecutionTrade]) -> list[BucketSummary]:
    dimensions = {
        "spread_bps": lambda row: spread_bucket(row.spread_bps),
        "entry_cost_bps": lambda row: entry_cost_bucket(row.entry_cost_bps),
        "activity_bucket": lambda row: row.activity_bucket,
        "trade_count_bucket": lambda row: row.trade_count_bucket,
        "bucket_notional": lambda row: notional_bucket(row.bucket_notional_usd),
    }
    summaries: list[BucketSummary] = []
    for window, rows in trades_by_window(trades).items():
        for dimension, key_fn in dimensions.items():
            buckets: dict[str, list[ExecutionTrade]] = {}
            for row in rows:
                buckets.setdefault(str(key_fn(row) or "unknown"), []).append(row)
            for bucket, bucket_rows in sorted(buckets.items()):
                summaries.append(summarize_bucket(window, dimension, bucket, bucket_rows))
    return summaries


def summarize_bucket(
    window: str,
    dimension: str,
    bucket: str,
    rows: list[ExecutionTrade],
) -> BucketSummary:
    pnls = [row.pnl_usd for row in rows]
    costs = [row.entry_cost_bps for row in rows if row.entry_cost_bps is not None]
    symbols: dict[str, int] = {}
    for row in rows:
        symbols[row.symbol] = symbols.get(row.symbol, 0) + 1
    return BucketSummary(
        window=window,
        dimension=dimension,
        bucket=bucket,
        trades=len(rows),
        pnl_usd=round(sum(pnls), 6),
        gross_pnl_usd=round(sum(row.gross_pnl_usd for row in rows), 6),
        fees_usd=round(sum(row.fees_usd for row in rows), 6),
        win_rate=round(sum(1 for pnl in pnls if pnl > 0.0) / len(pnls), 6) if pnls else None,
        profit_factor=profit_factor(pnls),
        avg_entry_cost_bps=round(sum(costs) / len(costs), 6) if costs else None,
        symbols=dict(sorted(symbols.items())),
    )


def spread_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 0.5:
        return "lt_0p5"
    if value < 1.0:
        return "0p5_to_1"
    if value < 2.0:
        return "1_to_2"
    if value < 4.0:
        return "2_to_4"
    return "gte_4"


def entry_cost_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 7.5:
        return "lt_7p5"
    if value < 8.0:
        return "7p5_to_8"
    if value < 9.0:
        return "8_to_9"
    return "gte_9"


def notional_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 100_000:
        return "lt_100k"
    if value < 250_000:
        return "100k_to_250k"
    if value < 1_000_000:
        return "250k_to_1m"
    return "gte_1m"


def evaluate_policy(
    window: str,
    trades: list[ExecutionTrade],
    policy: str,
    cap_multiplier: float,
) -> PolicyOutcome:
    base_pnl = sum(row.pnl_usd for row in trades)
    touched = [row for row in trades if policy_touches(row, policy)]
    touched_pnl = sum(row.pnl_usd for row in touched)
    adjusted = base_pnl - touched_pnl * (1.0 - cap_multiplier)
    symbols: dict[str, int] = {}
    for row in touched:
        symbols[row.symbol] = symbols.get(row.symbol, 0) + 1
    return PolicyOutcome(
        window=window,
        policy=policy,
        action=f"cap{int(cap_multiplier * 100)}",
        base_pnl_usd=round(base_pnl, 6),
        adjusted_pnl_usd=round(adjusted, 6),
        delta_usd=round(adjusted - base_pnl, 6),
        total_trades=len(trades),
        touched_trades=len(touched),
        touched_pnl_usd=round(touched_pnl, 6),
        touched_winners=sum(1 for row in touched if row.pnl_usd > 0.0),
        touched_losers=sum(1 for row in touched if row.pnl_usd < 0.0),
        touched_symbols=dict(sorted(symbols.items())),
    )


def policy_touches(row: ExecutionTrade, policy: str) -> bool:
    spread = row.spread_bps
    cost = row.entry_cost_bps
    if policy == "cap_spread_gte_1":
        return spread is not None and spread >= 1.0
    if policy == "cap_spread_gte_2":
        return spread is not None and spread >= 2.0
    if policy == "cap_entry_cost_gte_8":
        return cost is not None and cost >= 8.0
    if policy == "cap_entry_cost_gte_9":
        return cost is not None and cost >= 9.0
    if policy == "cap_spread_gte_1_not_high_activity":
        return spread is not None and spread >= 1.0 and row.activity_bucket not in {"high", "very_high"}
    if policy == "cap_bucket_notional_lt100k":
        return row.bucket_notional_usd is not None and row.bucket_notional_usd < 100_000.0
    if policy == "cap_soft_trade_count":
        return row.trade_count_bucket not in {"high", "very_high"}
    raise ValueError(f"unknown policy: {policy}")


def trades_by_window(trades: Iterable[ExecutionTrade]) -> dict[str, list[ExecutionTrade]]:
    grouped: dict[str, list[ExecutionTrade]] = {}
    for row in trades:
        grouped.setdefault(row.window, []).append(row)
    return grouped


def profit_factor(pnls: list[float]) -> float | None:
    gains = sum(pnl for pnl in pnls if pnl > 0.0)
    losses = -sum(pnl for pnl in pnls if pnl < 0.0)
    if losses <= 0.0:
        return None
    return round(gains / losses, 6)


def optional_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def to_float(value: object) -> float:
    result = optional_float(value)
    return result if result is not None else 0.0


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
    bucket_summaries: list[BucketSummary],
    outcomes: list[PolicyOutcome],
) -> None:
    lines = [
        "# P122 Pod C execution-cost audit",
        "",
        f"- generated_at: `{payload.get('generated_at')}`",
        "- decision: `research_only_no_live_change`",
        f"- input_reports: `{payload.get('input_reports')}`",
        f"- parameters: `{payload.get('parameters')}`",
        f"- trade_count: `{payload.get('trade_count')}`",
        "",
        "## Spread Buckets",
        "",
        "| Window | Spread | Trades | PnL | Gross PnL | Fees | PF | WR | Avg cost bps | Symbols |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in bucket_summaries:
        if row.dimension != "spread_bps":
            continue
        lines.append(
            f"| `{row.window}` | `{row.bucket}` | {row.trades} | {row.pnl_usd:.2f} | "
            f"{row.gross_pnl_usd:.2f} | {row.fees_usd:.2f} | {fmt(row.profit_factor)} | "
            f"{fmt(row.win_rate)} | {fmt(row.avg_entry_cost_bps)} | `{row.symbols}` |"
        )
    lines.extend(
        [
            "",
            "## Policy Counterfactuals",
            "",
            "| Window | Policy | Action | Adjusted PnL | Delta | Touched | Touched PnL | Winners/Losers | Symbols |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for row in outcomes:
        lines.append(
            f"| `{row.window}` | `{row.policy}` | `{row.action}` | {row.adjusted_pnl_usd:.2f} | "
            f"{row.delta_usd:+.2f} | {row.touched_trades}/{row.total_trades} | "
            f"{row.touched_pnl_usd:.2f} | {row.touched_winners}/{row.touched_losers} | "
            f"`{row.touched_symbols}` |"
        )
    lines.extend(
        [
            "",
            "## Lecture",
            "",
            "- `entry_cost_bps = fee_bps + max(spread_bps, 0)`, avec `fee_bps` infere des trades fermes.",
            "- Les variantes sont des proxies cap-only; elles ne modelisent pas le taux de fill maker, la queue position, ni l'occupation portefeuille.",
            "- Aucune modification live/deploy/fetch n'est incluse.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


if __name__ == "__main__":
    main()
