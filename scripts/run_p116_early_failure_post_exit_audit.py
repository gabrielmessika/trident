#!/usr/bin/env python3
"""P1-16 / A-PNL-04 post-exit audit for Pod A early_failure_exit.

This is research-only. It does not change live exits. The audit takes trades
that actually closed via early_failure_exit, disables only that EFE trigger in a
per-trade simulation, and follows the trade until the next natural protective
exit or replay window end.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.settings import AppConfig, load_config
from app.trident.pod_a.live_risk import (
    catastrophic_stop_bps_for_plan,
    stop_grace_minutes_for_setup,
)
from scripts.run_p102_exit_sensitivity import (
    SnapshotPoint,
    TradeSpec,
    VariantSpec,
    _favorable_bps,
    _fmt_optional,
    load_pod_a_trades,
    load_snapshot_index,
    parse_timestamp,
    simulate_trade,
)


DEFAULT_SNAPSHOT_INPUT = "server-data/live_snapshots"


@dataclass(slots=True)
class EarlyFailureAuditRow:
    trade_id: str
    symbol: str
    setup: str
    opened_at: str
    original_closed_at: str
    natural_closed_at: str | None
    original_close_reason: str
    natural_close_reason: str
    entry_price: float
    target_notional_usd: float
    stop_bps: float
    grace_minutes: int
    catastrophic_stop_bps: float
    original_pnl_usd: float
    natural_pnl_usd: float
    delta_vs_original_usd: float
    classification: str
    missed_recovery_usd: float
    loss_avoided_by_efe_usd: float
    original_hold_minutes: float | None
    natural_hold_minutes: float | None
    extra_hold_minutes: float | None
    total_mfe_bps: float
    total_mae_bps: float
    post_exit_mfe_bps: float
    post_exit_mae_bps: float


@dataclass(slots=True)
class EarlyFailureAuditSummary:
    replay_report: str
    snapshot_input: str
    audited_trades: int
    original_pnl_usd: float
    natural_pnl_usd: float
    delta_natural_vs_original_usd: float
    missed_winner_count: int
    missed_loss_reduction_count: int
    missed_recovery_usd: float
    loss_avoided_count: int
    loss_avoided_by_efe_usd: float
    neutral_count: int
    avg_extra_hold_minutes: float | None
    avg_post_exit_mfe_bps: float | None
    avg_post_exit_mae_bps: float | None
    natural_close_reasons: dict[str, int]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--replay-report", required=True)
    parser.add_argument("--snapshot-input", default=DEFAULT_SNAPSHOT_INPUT)
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--close-reason", default="early_failure_exit")
    parser.add_argument("--taker-fee-bps", type=float, default=3.5)
    parser.add_argument("--dry-run-slippage-bps", type=float, default=0.5)
    parser.add_argument("--dry-run-spread-multiplier", type=float, default=0.5)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = Path(
        args.output_dir
        or f"server-data/replay_reports/p116_early_failure_post_exit_{utc_stamp()}"
    )
    report_path = Path(args.replay_report)
    snapshot_input = Path(args.snapshot_input)
    config = load_config(args.config)
    trades = [
        trade
        for trade in load_pod_a_trades(report_path)
        if trade.original_close_reason == args.close_reason
        and trade.original_closed_at is not None
    ]
    if not trades:
        raise ValueError(f"{report_path}: no trades with close_reason={args.close_reason}")
    start = min(trade.opened_at for trade in trades)
    end = max(_natural_end_candidate(trade) for trade in trades)
    snapshot_index = load_snapshot_index(
        snapshot_input,
        symbols={trade.symbol for trade in trades},
        start=start,
        end=end,
    )
    rows = audit_early_failure_trades(
        trades=trades,
        snapshot_index=snapshot_index,
        config=config,
        taker_fee_bps=float(args.taker_fee_bps),
        dry_run_slippage_bps=float(args.dry_run_slippage_bps),
        dry_run_spread_multiplier=float(args.dry_run_spread_multiplier),
    )
    summary = summarize_rows(
        rows,
        replay_report=str(report_path),
        snapshot_input=str(snapshot_input),
    )
    write_outputs(output_dir=output_dir, summary=summary, rows=rows)
    print(output_dir)


def audit_early_failure_trades(
    *,
    trades: list[TradeSpec],
    snapshot_index: dict[str, list[SnapshotPoint]],
    config: AppConfig,
    taker_fee_bps: float,
    dry_run_slippage_bps: float,
    dry_run_spread_multiplier: float,
) -> list[EarlyFailureAuditRow]:
    rows: list[EarlyFailureAuditRow] = []
    for trade in trades:
        grace_minutes = stop_grace_minutes_for_setup(
            config.pod_a,
            setup=trade.setup,
            confidence=float(trade.confidence or 0.0),
            details=dict(trade.setup_details or {}),
            fallback_minutes=int(config.pod_a.stop_grace_minutes),
        )
        cat_stop_bps = catastrophic_stop_bps_for_plan(
            config.trident.execution,
            stop_bps=float(trade.stop_bps or 0.0),
        )
        variant = VariantSpec(
            grace_minutes=grace_minutes,
            cat_stop_max_bps=cat_stop_bps,
            early_failure_enabled=False,
        )
        points = snapshot_index.get(trade.symbol, [])
        natural = simulate_trade(
            trade,
            points,
            variant,
            taker_fee_bps=taker_fee_bps,
            dry_run_slippage_bps=dry_run_slippage_bps,
            dry_run_spread_multiplier=dry_run_spread_multiplier,
        )
        post_mfe, post_mae = post_exit_mfe_mae_bps(trade, points, natural.closed_at)
        original_hold = _minutes_between(trade.opened_at, trade.original_closed_at)
        natural_closed_at = parse_timestamp(natural.closed_at)
        natural_hold = _minutes_between(trade.opened_at, natural_closed_at)
        extra_hold = (
            round(natural_hold - original_hold, 4)
            if original_hold is not None and natural_hold is not None
            else None
        )
        delta = round(natural.pnl_usd - trade.original_pnl_usd, 6)
        classification = classify_path(trade.original_pnl_usd, natural.pnl_usd)
        rows.append(
            EarlyFailureAuditRow(
                trade_id=trade.trade_id,
                symbol=trade.symbol,
                setup=trade.setup,
                opened_at=trade.opened_at.isoformat(),
                original_closed_at=trade.original_closed_at.isoformat()
                if trade.original_closed_at
                else "",
                natural_closed_at=natural.closed_at,
                original_close_reason=trade.original_close_reason,
                natural_close_reason=natural.close_reason,
                entry_price=round(trade.entry_price, 8),
                target_notional_usd=round(trade.target_notional_usd, 6),
                stop_bps=round(trade.stop_bps, 4),
                grace_minutes=grace_minutes,
                catastrophic_stop_bps=round(cat_stop_bps, 4),
                original_pnl_usd=round(trade.original_pnl_usd, 6),
                natural_pnl_usd=round(natural.pnl_usd, 6),
                delta_vs_original_usd=delta,
                classification=classification,
                missed_recovery_usd=round(max(delta, 0.0), 6),
                loss_avoided_by_efe_usd=round(max(-delta, 0.0), 6),
                original_hold_minutes=original_hold,
                natural_hold_minutes=natural_hold,
                extra_hold_minutes=extra_hold,
                total_mfe_bps=round(natural.mfe_bps, 4),
                total_mae_bps=round(natural.mae_bps, 4),
                post_exit_mfe_bps=round(post_mfe, 4),
                post_exit_mae_bps=round(post_mae, 4),
            )
        )
    return rows


def classify_path(original_pnl: float, natural_pnl: float) -> str:
    delta = natural_pnl - original_pnl
    if delta > 0.005 and natural_pnl >= 0.0:
        return "missed_winner"
    if delta > 0.005:
        return "missed_loss_reduction"
    if delta < -0.005:
        return "loss_avoided_by_efe"
    return "neutral"


def post_exit_mfe_mae_bps(
    trade: TradeSpec,
    points: list[SnapshotPoint],
    natural_closed_at: str | None,
) -> tuple[float, float]:
    if trade.original_closed_at is None:
        return 0.0, 0.0
    end = parse_timestamp(natural_closed_at)
    mfe = 0.0
    mae = 0.0
    for point in points:
        if point.timestamp < trade.original_closed_at:
            continue
        if end is not None and point.timestamp > end:
            continue
        favorable_bps = _favorable_bps(trade.side, trade.entry_price, point.price)
        mfe = max(mfe, favorable_bps)
        mae = max(mae, -favorable_bps)
    return mfe, mae


def summarize_rows(
    rows: list[EarlyFailureAuditRow],
    *,
    replay_report: str,
    snapshot_input: str,
) -> EarlyFailureAuditSummary:
    classifications = Counter(row.classification for row in rows)
    reasons = Counter(row.natural_close_reason for row in rows)
    extra_holds = [
        row.extra_hold_minutes
        for row in rows
        if row.extra_hold_minutes is not None
    ]
    return EarlyFailureAuditSummary(
        replay_report=replay_report,
        snapshot_input=snapshot_input,
        audited_trades=len(rows),
        original_pnl_usd=round(sum(row.original_pnl_usd for row in rows), 6),
        natural_pnl_usd=round(sum(row.natural_pnl_usd for row in rows), 6),
        delta_natural_vs_original_usd=round(
            sum(row.delta_vs_original_usd for row in rows),
            6,
        ),
        missed_winner_count=classifications["missed_winner"],
        missed_loss_reduction_count=classifications["missed_loss_reduction"],
        missed_recovery_usd=round(sum(row.missed_recovery_usd for row in rows), 6),
        loss_avoided_count=classifications["loss_avoided_by_efe"],
        loss_avoided_by_efe_usd=round(
            sum(row.loss_avoided_by_efe_usd for row in rows),
            6,
        ),
        neutral_count=classifications["neutral"],
        avg_extra_hold_minutes=_avg_optional(extra_holds),
        avg_post_exit_mfe_bps=_avg_optional(row.post_exit_mfe_bps for row in rows),
        avg_post_exit_mae_bps=_avg_optional(row.post_exit_mae_bps for row in rows),
        natural_close_reasons=dict(sorted(reasons.items())),
    )


def write_outputs(
    *,
    output_dir: Path,
    summary: EarlyFailureAuditSummary,
    rows: list[EarlyFailureAuditRow],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_csv = output_dir / "early_failure_post_exit_summary.csv"
    rows_csv = output_dir / "early_failure_post_exit_trades.csv"
    report_json = output_dir / "p116_early_failure_post_exit_audit.json"
    report_md = output_dir / "p116_early_failure_post_exit_audit.md"

    with summary_csv.open("w", encoding="utf-8", newline="") as handle:
        payload = asdict(summary)
        payload["natural_close_reasons"] = json.dumps(
            summary.natural_close_reasons,
            sort_keys=True,
        )
        writer = csv.DictWriter(handle, fieldnames=list(payload))
        writer.writeheader()
        writer.writerow(payload)

    with rows_csv.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = list(asdict(rows[0]).keys()) if rows else list(EarlyFailureAuditRow.__slots__)
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))

    payload = {
        "kind": "p116_early_failure_post_exit_audit",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "research_only_no_live_change",
        "summary": asdict(summary),
        "outputs": {
            "summary_csv": str(summary_csv),
            "trades_csv": str(rows_csv),
            "report_json": str(report_json),
            "report_md": str(report_md),
        },
        "limits": [
            "Replay post-exit uniquement: les entrees et le portefeuille ne sont pas rejoues apres une sortie modifiee.",
            "Les fills intra-minute ne sont pas observes; fermeture au prix snapshot minute avec modele dry-run.",
            "La simulation desactive seulement early_failure_exit; stops, trailing, break-even, time-stop et stop catastrophe restent actifs.",
        ],
        "sample_rows": [asdict(row) for row in rows[:20]],
    }
    report_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    report_md.write_text(render_markdown(payload), encoding="utf-8")


def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# P1-16 early_failure_exit post-exit audit",
        "",
        f"- generated_at: `{payload['generated_at']}`",
        "- status: `research_only_no_live_change`",
        f"- replay_report: `{summary['replay_report']}`",
        f"- snapshot_input: `{summary['snapshot_input']}`",
        "",
        "## Summary",
        "",
        "| Trades | Original PnL | Natural no-EFE PnL | Delta | Missed winners | Missed loss cuts | Missed recovery | EFE saves | Loss avoided | Avg extra min | Avg post MFE | Avg post MAE |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        (
            f"| {summary['audited_trades']} | {float(summary['original_pnl_usd']):.2f} | "
            f"{float(summary['natural_pnl_usd']):.2f} | "
            f"{float(summary['delta_natural_vs_original_usd']):+.2f} | "
            f"{summary['missed_winner_count']} | "
            f"{summary['missed_loss_reduction_count']} | "
            f"{float(summary['missed_recovery_usd']):.2f} | "
            f"{summary['loss_avoided_count']} | "
            f"{float(summary['loss_avoided_by_efe_usd']):.2f} | "
            f"{_fmt_optional(summary.get('avg_extra_hold_minutes'))} | "
            f"{_fmt_optional(summary.get('avg_post_exit_mfe_bps'))} | "
            f"{_fmt_optional(summary.get('avg_post_exit_mae_bps'))} |"
        ),
        "",
        "## Natural Close Reasons",
        "",
        f"`{json.dumps(summary['natural_close_reasons'], sort_keys=True)}`",
        "",
        "## Artefacts",
        "",
        f"- Summary CSV: `{payload['outputs']['summary_csv']}`",
        f"- Trades CSV: `{payload['outputs']['trades_csv']}`",
        f"- JSON: `{payload['outputs']['report_json']}`",
        "",
        "## Limits",
        "",
        *[f"- {item}" for item in payload["limits"]],
        "",
    ]
    return "\n".join(lines)


def _natural_end_candidate(trade: TradeSpec) -> datetime:
    return trade.opened_at + timedelta(hours=max(int(trade.time_stop_hours), 1))


def _minutes_between(start: datetime, end: datetime | None) -> float | None:
    if end is None:
        return None
    return round((end - start).total_seconds() / 60.0, 4)


def _avg_optional(values: Iterable[float | None]) -> float | None:
    rows = [float(value) for value in values if value is not None]
    if not rows:
        return None
    return round(sum(rows) / len(rows), 4)


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


if __name__ == "__main__":
    main()
