#!/usr/bin/env python3
"""P121 / C-PNL-04 Pod C session and liquidity audit.

Research-only. Reads full-bot replay reports, groups Pod C closed trades by
session/liquidity buckets, and tests simple cap-only calendar policies.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
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
class TradeRow:
    window: str
    trade_id: str
    symbol: str
    side: str
    opened_at: str
    hour_utc: float | None
    session: str
    pnl_usd: float
    confidence: float | None
    market_cluster: str
    activity_bucket: str
    trade_count_bucket: str
    close_reason: str


@dataclass(slots=True)
class BucketSummary:
    window: str
    dimension: str
    bucket: str
    trades: int
    pnl_usd: float
    win_rate: float | None
    profit_factor: float | None
    avg_pnl_usd: float | None
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
    output_dir = Path(args.output_dir or f"server-data/replay_reports/p121_pod_c_session_liquidity_{utc_stamp()}")
    output_dir.mkdir(parents=True, exist_ok=True)

    trades = load_trades(report_paths)
    bucket_summaries = build_bucket_summaries(trades)
    policies = [
        "cap_session_us_late",
        "cap_session_asia_overnight",
        "cap_session_non_us_cash",
        "cap_low_conf_outside_us_cash",
        "cap_not_high_activity",
        "cap_not_high_trade_count",
        "cap_late_or_low_activity",
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
            "Cap-only counterfactual scales closed-trade PnL linearly; it does not simulate missed exits or changed portfolio occupancy.",
            "No live trading flag, deploy script, fetch script, or runtime config is changed by this audit.",
        ],
    }
    write_csv(output_dir / "bucket_summary.csv", bucket_summaries)
    write_csv(output_dir / "policy_outcomes.csv", outcomes)
    write_json(output_dir / "p121_pod_c_session_liquidity_audit.json", payload)
    write_markdown(output_dir / "p121_pod_c_session_liquidity_audit.md", payload, bucket_summaries, outcomes)
    print(output_dir)


def load_trades(report_paths: list[Path]) -> list[TradeRow]:
    rows: list[TradeRow] = []
    for report_path in report_paths:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        window = report_window(payload, report_path)
        pod_c = payload.get("pod_c") if isinstance(payload, dict) else {}
        trades = (pod_c or {}).get("closed_trade_log") if isinstance(pod_c, dict) else []
        for index, trade in enumerate(trades or [], start=1):
            if not isinstance(trade, dict):
                continue
            opened = parse_dt(str(trade.get("opened_at") or ""))
            setup_details = trade.get("setup_details") if isinstance(trade.get("setup_details"), dict) else {}
            hour_utc = opened.hour + opened.minute / 60.0 if opened is not None else None
            rows.append(
                TradeRow(
                    window=window,
                    trade_id=str(trade.get("trade_id") or f"{window}_{index:04d}"),
                    symbol=str(trade.get("symbol") or "").upper(),
                    side=str(trade.get("side") or "").lower(),
                    opened_at=opened.isoformat().replace("+00:00", "Z") if opened else str(trade.get("opened_at") or ""),
                    hour_utc=round(hour_utc, 4) if hour_utc is not None else None,
                    session=session_bucket(opened),
                    pnl_usd=round(to_float(trade.get("pnl_usd")), 6),
                    confidence=optional_float(trade.get("confidence")),
                    market_cluster=str(
                        trade.get("market_cluster")
                        or setup_details.get("market_cluster")
                        or setup_details.get("cluster_mode_name")
                        or "unknown"
                    ),
                    activity_bucket=str(setup_details.get("activity_bucket") or "unknown"),
                    trade_count_bucket=str(setup_details.get("trade_count_bucket") or "unknown"),
                    close_reason=str(trade.get("close_reason") or "unknown"),
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


def parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def session_bucket(opened_at: datetime | None) -> str:
    if opened_at is None:
        return "unknown"
    hour = opened_at.hour + opened_at.minute / 60.0
    if hour < 7.0:
        return "asia_overnight"
    if hour < 10.0:
        return "europe_morning"
    if hour < 13.5:
        return "us_premarket"
    if hour < 20.0:
        return "us_cash"
    return "us_late"


def build_bucket_summaries(trades: list[TradeRow]) -> list[BucketSummary]:
    dimensions = {
        "session": lambda row: row.session,
        "market_cluster": lambda row: row.market_cluster,
        "activity_bucket": lambda row: row.activity_bucket,
        "trade_count_bucket": lambda row: row.trade_count_bucket,
        "close_reason": lambda row: row.close_reason,
    }
    summaries: list[BucketSummary] = []
    for window, rows in trades_by_window(trades).items():
        for dimension, key_fn in dimensions.items():
            buckets: dict[str, list[TradeRow]] = {}
            for row in rows:
                buckets.setdefault(str(key_fn(row) or "unknown"), []).append(row)
            for bucket, bucket_rows in sorted(buckets.items()):
                summaries.append(summarize_bucket(window, dimension, bucket, bucket_rows))
    return summaries


def summarize_bucket(window: str, dimension: str, bucket: str, rows: list[TradeRow]) -> BucketSummary:
    pnls = [row.pnl_usd for row in rows]
    symbols: dict[str, int] = {}
    for row in rows:
        symbols[row.symbol] = symbols.get(row.symbol, 0) + 1
    return BucketSummary(
        window=window,
        dimension=dimension,
        bucket=bucket,
        trades=len(rows),
        pnl_usd=round(sum(pnls), 6),
        win_rate=round(sum(1 for pnl in pnls if pnl > 0.0) / len(pnls), 6) if pnls else None,
        profit_factor=profit_factor(pnls),
        avg_pnl_usd=round(sum(pnls) / len(pnls), 6) if pnls else None,
        symbols=dict(sorted(symbols.items())),
    )


def evaluate_policy(
    window: str,
    trades: list[TradeRow],
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


def policy_touches(row: TradeRow, policy: str) -> bool:
    if policy == "cap_session_us_late":
        return row.session == "us_late"
    if policy == "cap_session_asia_overnight":
        return row.session == "asia_overnight"
    if policy == "cap_session_non_us_cash":
        return row.session != "us_cash"
    if policy == "cap_low_conf_outside_us_cash":
        return row.session != "us_cash" and row.confidence is not None and row.confidence < 0.75
    if policy == "cap_not_high_activity":
        return row.activity_bucket not in {"high", "very_high"}
    if policy == "cap_not_high_trade_count":
        return row.trade_count_bucket not in {"high", "very_high"}
    if policy == "cap_late_or_low_activity":
        return row.session == "us_late" or row.activity_bucket not in {"high", "very_high"}
    raise ValueError(f"unknown policy: {policy}")


def trades_by_window(trades: Iterable[TradeRow]) -> dict[str, list[TradeRow]]:
    grouped: dict[str, list[TradeRow]] = {}
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
        "# P121 Pod C session/liquidity audit",
        "",
        f"- generated_at: `{payload.get('generated_at')}`",
        "- decision: `research_only_no_live_change`",
        f"- input_reports: `{payload.get('input_reports')}`",
        f"- parameters: `{payload.get('parameters')}`",
        f"- trade_count: `{payload.get('trade_count')}`",
        "",
        "## Session Buckets",
        "",
        "| Window | Session | Trades | PnL | PF | WR | Avg PnL | Symbols |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in bucket_summaries:
        if row.dimension != "session":
            continue
        lines.append(
            f"| `{row.window}` | `{row.bucket}` | {row.trades} | {row.pnl_usd:.2f} | "
            f"{fmt(row.profit_factor)} | {fmt(row.win_rate)} | {fmt(row.avg_pnl_usd)} | "
            f"`{row.symbols}` |"
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
            "- Les sessions sont buckets UTC: `asia_overnight` 00:00-06:59, `europe_morning` 07:00-09:59, `us_premarket` 10:00-13:29, `us_cash` 13:30-19:59, `us_late` 20:00-23:59.",
            "- Le cap-only applique un multiplicateur lineaire au PnL des trades touches; c'est un proxy de sizing, pas un replay complet de portefeuille.",
            "- Aucune modification live/deploy/fetch n'est incluse.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


if __name__ == "__main__":
    main()
