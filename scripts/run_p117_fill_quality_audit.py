#!/usr/bin/env python3
"""P1-17 / A-PNL-07 fill-quality audit for Pod A.

This is research-only. It replays Pod A on local snapshots, writes a backtest
journal, then measures entry cost, book depth, skipped accepted signals, rejected
signals, and directional returns after 1/5/15 minutes.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backtest.pod_a_runner import PodABacktestRunner
from app.backtest.snapshot_loader import SnapshotLoader
from app.settings import AppConfig, load_config
from scripts.run_p105_a_grade_replay import (
    isoformat,
    parse_timestamp,
    prepare_snapshot_window,
    utc_stamp,
)


DEFAULT_LIVE_INPUT = "server-data/live_snapshots"
DEFAULT_LIVE_START = "2026-05-14T00:00:00Z"
DEFAULT_LIVE_END = "2026-06-23T00:00:00Z"
HORIZONS_MINUTES = (1, 5, 15)


@dataclass(frozen=True, slots=True)
class SnapshotPoint:
    timestamp: datetime
    price: float
    spread_bps: float


@dataclass(frozen=True, slots=True)
class SnapshotSeries:
    points: tuple[SnapshotPoint, ...]
    timestamps: tuple[datetime, ...]


@dataclass(slots=True)
class FillQualityRow:
    timestamp: str
    symbol: str
    side: str
    setup: str
    status: str
    reason: str
    risk_accepted: bool
    opened: bool
    skipped_open: bool
    target_notional_usd: float
    entry_mid_price: float
    fill_price: float | None
    spread_bps: float
    expected_entry_cost_bps: float
    expected_round_trip_cost_bps: float
    expected_entry_cost_usd: float
    bucket_notional_usd: float
    touch_notional_usd: float | None
    depth_10bps_usd: float | None
    depth_to_order_ratio: float | None
    touch_to_order_ratio: float | None
    book_imbalance: float
    trade_flow_bias: float
    microprice_dislocation_bps: float
    asset_ctx_observation_age_seconds: float | None
    external_reference_age_seconds: float | None
    future_return_1m_bps: float | None
    future_return_5m_bps: float | None
    future_return_15m_bps: float | None
    adverse_return_1m_bps: float | None
    adverse_return_5m_bps: float | None
    adverse_return_15m_bps: float | None
    mfe_15m_bps: float | None
    mae_15m_bps: float | None
    closed_trade_pnl_usd: float | None
    close_reason: str | None
    hold_hours: float | None


@dataclass(slots=True)
class FillQualityBucketRow:
    bucket_type: str
    bucket: str
    decisions: int
    opened: int
    accepted_skipped: int
    risk_rejected: int
    closed_trades: int
    closed_pnl_usd: float
    win_rate: float | None
    avg_expected_entry_cost_bps: float | None
    avg_spread_bps: float | None
    avg_depth_to_order_ratio: float | None
    avg_future_return_1m_bps: float | None
    avg_future_return_5m_bps: float | None
    avg_future_return_15m_bps: float | None
    avg_adverse_return_15m_bps: float | None
    avg_mae_15m_bps: float | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--live-input", default=DEFAULT_LIVE_INPUT)
    parser.add_argument("--live-start", default=DEFAULT_LIVE_START)
    parser.add_argument("--live-end", default=DEFAULT_LIVE_END)
    parser.add_argument("--output-dir", default="")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    generated_at = utc_stamp()
    output_dir = Path(
        args.output_dir
        or f"server-data/replay_reports/p117_fill_quality_audit_{generated_at}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    config = load_config(args.config)
    live_start = parse_timestamp(args.live_start)
    live_end = parse_timestamp(args.live_end)
    if live_start is None or live_end is None or live_start >= live_end:
        raise ValueError("--live-start and --live-end must define a valid UTC window")

    replay_input, input_files = prepare_snapshot_window(
        snapshots_dir=Path(args.live_input),
        output_dir=output_dir,
        name="live_fill_quality_input",
        start=live_start,
        end=live_end,
    )

    journal_path = output_dir / "pod_a_fill_quality_journal.jsonl"
    if journal_path.exists():
        journal_path.unlink()
    started = time.perf_counter()
    replay_result = PodABacktestRunner(config).run_jsonl(
        replay_input,
        journal_path,
        include_signal_reviews=False,
    )
    runtime_seconds = round(time.perf_counter() - started, 4)

    rows = build_fill_quality_rows(
        journal_path=journal_path,
        snapshot_input=replay_input,
        config=config,
    )
    buckets = summarize_buckets(rows)
    payload = {
        "generated_at": generated_at,
        "window": {
            "start": isoformat(live_start),
            "end": isoformat(live_end),
            "input_files": input_files,
        },
        "runtime_seconds": runtime_seconds,
        "replay": {
            "records_processed": replay_result.records_processed,
            "signal_count": replay_result.signal_count,
            "accepted_count": replay_result.accepted_count,
            "rejected_count": replay_result.rejected_count,
            "opened_count": replay_result.opened_count,
            "skipped_open_count": replay_result.skipped_open_count,
            "closed_trade_count": replay_result.closed_trade_count,
            "realized_pnl_usd": replay_result.realized_pnl_usd,
            "profit_factor": profit_factor_from_rows(rows),
        },
        "status_counts": dict(sorted(Counter(row.status for row in rows).items())),
        "reason_counts": dict(sorted(Counter(row.reason for row in rows).items())),
        "bucket_summary": [asdict(row) for row in buckets],
        "decision": "research_only_no_live_change",
    }
    write_csv(output_dir / "fill_quality_decisions.csv", rows)
    write_csv(output_dir / "fill_quality_bucket_summary.csv", buckets)
    write_json(output_dir / "p117_fill_quality_audit.json", payload)
    write_markdown(output_dir / "p117_fill_quality_audit.md", payload, buckets)
    print(output_dir)


def build_fill_quality_rows(
    *,
    journal_path: Path,
    snapshot_input: Path,
    config: AppConfig,
) -> list[FillQualityRow]:
    signals: list[dict[str, Any]] = []
    trades_by_open: dict[tuple[str, str], dict[str, Any]] = {}
    for record in jsonl_records(journal_path):
        event_type = record.get("event_type")
        if event_type == "signal":
            signal = record.get("signal")
            if isinstance(signal, dict):
                signals.append(record)
        elif event_type == "trade_close":
            trade = record.get("trade")
            if isinstance(trade, dict):
                opened_at = timestamp_key(trade.get("opened_at"))
                symbol = str(trade.get("symbol") or "").upper()
                if opened_at and symbol:
                    trades_by_open[(symbol, opened_at)] = trade

    symbols = {
        str((record.get("signal") or {}).get("symbol") or "").upper()
        for record in signals
    }
    symbols.discard("")
    snapshot_index = load_snapshot_index(snapshot_input, symbols=symbols)

    rows: list[FillQualityRow] = []
    execution_config = config.trident.execution
    for record in signals:
        timestamp = str(record.get("timestamp") or "")
        signal = record.get("signal") or {}
        if not isinstance(signal, dict):
            continue
        risk = signal.get("risk") or {}
        execution = signal.get("execution") or {}
        snapshot = record.get("symbol_snapshot") or {}
        if not isinstance(risk, dict) or not isinstance(execution, dict) or not isinstance(snapshot, dict):
            continue
        symbol = str(signal.get("symbol") or "").upper()
        side = str(signal.get("side") or "")
        if not symbol or side not in {"long", "short"}:
            continue
        accepted = bool(risk.get("accepted"))
        opened = bool(execution.get("opened"))
        skipped = bool(execution.get("skipped_open"))
        if opened:
            status = "opened"
            reason = "opened"
        elif accepted and skipped:
            status = "accepted_skipped"
            reason = str(execution.get("skip_reason") or "execution_skipped")
        elif accepted:
            status = "accepted_not_opened"
            reason = str(execution.get("skip_reason") or "accepted_without_open")
        else:
            status = "risk_rejected"
            reason = str(risk.get("reason") or "risk_rejected")

        open_fills = execution.get("open_fills") or []
        fill = open_fills[0] if isinstance(open_fills, list) and open_fills and isinstance(open_fills[0], dict) else {}
        timestamp_dt = parse_timestamp(timestamp)
        entry_mid = optional_float(snapshot.get("price")) or 0.0
        target_notional = optional_float(risk.get("target_notional_usd")) or 0.0
        fill_price = optional_float(fill.get("price")) if fill else None
        spread_bps = max(optional_float(snapshot.get("spread_bps")) or 0.0, 0.0)
        entry_cost_bps = (
            optional_float(fill.get("slippage_bps"))
            if fill
            else None
        )
        if entry_cost_bps is None:
            entry_cost_bps = expected_entry_cost_bps(
                spread_bps=spread_bps,
                spread_multiplier=float(execution_config.dry_run_spread_multiplier),
                slippage_bps=float(execution_config.dry_run_slippage_bps),
            )
        round_trip_cost_bps = 2.0 * (
            entry_cost_bps + float(execution_config.dry_run_taker_fee_bps)
        )
        touch_notional, depth_notional = liquidity_notional(snapshot, side=side)
        trade = trades_by_open.get((symbol, timestamp_key(timestamp)))
        future = forward_metrics(
            snapshot_index.get(symbol, []),
            timestamp_dt,
            side=side,
            entry_price=entry_mid,
        )
        rows.append(
            FillQualityRow(
                timestamp=timestamp,
                symbol=symbol,
                side=side,
                setup=str(signal.get("setup") or ""),
                status=status,
                reason=reason,
                risk_accepted=accepted,
                opened=opened,
                skipped_open=skipped,
                target_notional_usd=round(target_notional, 6),
                entry_mid_price=round(entry_mid, 8),
                fill_price=round(fill_price, 8) if fill_price is not None else None,
                spread_bps=round(spread_bps, 6),
                expected_entry_cost_bps=round(entry_cost_bps, 6),
                expected_round_trip_cost_bps=round(round_trip_cost_bps, 6),
                expected_entry_cost_usd=round(target_notional * entry_cost_bps / 10_000.0, 6),
                bucket_notional_usd=round(optional_float(snapshot.get("bucket_notional_usd")) or 0.0, 6),
                touch_notional_usd=round(touch_notional, 6) if touch_notional is not None else None,
                depth_10bps_usd=round(depth_notional, 6) if depth_notional is not None else None,
                depth_to_order_ratio=ratio(depth_notional, target_notional),
                touch_to_order_ratio=ratio(touch_notional, target_notional),
                book_imbalance=round(optional_float(snapshot.get("book_imbalance")) or 0.0, 6),
                trade_flow_bias=round(optional_float(snapshot.get("trade_flow_bias")) or 0.0, 6),
                microprice_dislocation_bps=round(
                    optional_float(snapshot.get("microprice_dislocation_bps")) or 0.0,
                    6,
                ),
                asset_ctx_observation_age_seconds=optional_float(
                    snapshot.get("asset_ctx_observation_age_seconds")
                ),
                external_reference_age_seconds=optional_float(
                    snapshot.get("external_reference_age_seconds")
                ),
                future_return_1m_bps=future["future_return_1m_bps"],
                future_return_5m_bps=future["future_return_5m_bps"],
                future_return_15m_bps=future["future_return_15m_bps"],
                adverse_return_1m_bps=future["adverse_return_1m_bps"],
                adverse_return_5m_bps=future["adverse_return_5m_bps"],
                adverse_return_15m_bps=future["adverse_return_15m_bps"],
                mfe_15m_bps=future["mfe_15m_bps"],
                mae_15m_bps=future["mae_15m_bps"],
                closed_trade_pnl_usd=(
                    round(optional_float(trade.get("pnl_usd")) or 0.0, 6)
                    if trade is not None
                    else None
                ),
                close_reason=str(trade.get("close_reason") or "") if trade is not None else None,
                hold_hours=optional_float(trade.get("hold_hours")) if trade is not None else None,
            )
        )
    return rows


def load_snapshot_index(
    snapshot_input: Path,
    *,
    symbols: set[str],
) -> dict[str, SnapshotSeries]:
    index: dict[str, list[SnapshotPoint]] = {symbol: [] for symbol in sorted(symbols)}
    loader = SnapshotLoader()
    for record in loader.iter_jsonl(snapshot_input):
        timestamp = parse_timestamp(record.timestamp)
        if timestamp is None:
            continue
        for item in record.symbols:
            symbol = str(item.get("symbol") or "").upper()
            if symbol not in index:
                continue
            price = optional_float(item.get("price")) or 0.0
            if price <= 0.0:
                continue
            index[symbol].append(
                SnapshotPoint(
                    timestamp=timestamp,
                    price=price,
                    spread_bps=max(optional_float(item.get("spread_bps")) or 0.0, 0.0),
                )
            )
    for points in index.values():
        points.sort(key=lambda point: point.timestamp)
    return {
        symbol: SnapshotSeries(
            points=tuple(points),
            timestamps=tuple(point.timestamp for point in points),
        )
        for symbol, points in index.items()
    }


def forward_metrics(
    points: SnapshotSeries | list[SnapshotPoint],
    timestamp: datetime | None,
    *,
    side: str,
    entry_price: float,
) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    for horizon in HORIZONS_MINUTES:
        value = future_return_bps(
            points,
            timestamp,
            side=side,
            entry_price=entry_price,
            horizon_minutes=horizon,
        )
        result[f"future_return_{horizon}m_bps"] = value
        result[f"adverse_return_{horizon}m_bps"] = (
            round(max(-value, 0.0), 6) if value is not None else None
        )

    mfe, mae = mfe_mae_bps(
        points,
        timestamp,
        side=side,
        entry_price=entry_price,
        horizon_minutes=15,
    )
    result["mfe_15m_bps"] = mfe
    result["mae_15m_bps"] = mae
    return result


def future_return_bps(
    points: SnapshotSeries | list[SnapshotPoint],
    timestamp: datetime | None,
    *,
    side: str,
    entry_price: float,
    horizon_minutes: int,
) -> float | None:
    if timestamp is None or entry_price <= 0:
        return None
    series = ensure_series(points)
    target = timestamp + timedelta(minutes=horizon_minutes)
    index = bisect.bisect_left(series.timestamps, target)
    if index < len(series.points):
        point = series.points[index]
        return round(directional_return_bps(side, entry_price, point.price), 6)
    return None


def mfe_mae_bps(
    points: SnapshotSeries | list[SnapshotPoint],
    timestamp: datetime | None,
    *,
    side: str,
    entry_price: float,
    horizon_minutes: int,
) -> tuple[float | None, float | None]:
    if timestamp is None or entry_price <= 0:
        return None, None
    series = ensure_series(points)
    end = timestamp + timedelta(minutes=horizon_minutes)
    start_index = bisect.bisect_left(series.timestamps, timestamp)
    end_index = bisect.bisect_right(series.timestamps, end)
    returns = [
        directional_return_bps(side, entry_price, point.price)
        for point in series.points[start_index:end_index]
    ]
    if not returns:
        return None, None
    return round(max(returns), 6), round(min(returns), 6)


def ensure_series(points: SnapshotSeries | list[SnapshotPoint]) -> SnapshotSeries:
    if isinstance(points, SnapshotSeries):
        return points
    sorted_points = tuple(sorted(points, key=lambda point: point.timestamp))
    return SnapshotSeries(
        points=sorted_points,
        timestamps=tuple(point.timestamp for point in sorted_points),
    )


def directional_return_bps(side: str, entry_price: float, price: float) -> float:
    if entry_price <= 0.0:
        return 0.0
    if side == "long":
        return (price - entry_price) / entry_price * 10_000.0
    return (entry_price - price) / entry_price * 10_000.0


def liquidity_notional(snapshot: dict[str, Any], *, side: str) -> tuple[float | None, float | None]:
    price = optional_float(snapshot.get("price")) or 0.0
    if side == "long":
        touch_price = optional_float(snapshot.get("best_ask")) or price
        touch_size = optional_float(snapshot.get("best_ask_size"))
        depth_size = optional_float(snapshot.get("ask_depth_10bps"))
    else:
        touch_price = optional_float(snapshot.get("best_bid")) or price
        touch_size = optional_float(snapshot.get("best_bid_size"))
        depth_size = optional_float(snapshot.get("bid_depth_10bps"))
    touch_notional = (
        max(touch_price, 0.0) * max(touch_size, 0.0)
        if touch_size is not None and touch_price > 0.0
        else None
    )
    depth_notional = (
        max(price, 0.0) * max(depth_size, 0.0)
        if depth_size is not None and price > 0.0
        else None
    )
    return touch_notional, depth_notional


def summarize_buckets(rows: list[FillQualityRow]) -> list[FillQualityBucketRow]:
    grouped: dict[tuple[str, str], list[FillQualityRow]] = defaultdict(list)
    for row in rows:
        grouped[("status", row.status)].append(row)
        grouped[("reason", row.reason)].append(row)
        grouped[("spread", spread_bucket(row.spread_bps))].append(row)
        grouped[("entry_cost", cost_bucket(row.expected_entry_cost_bps))].append(row)
        grouped[("depth_ratio", depth_ratio_bucket(row.depth_to_order_ratio))].append(row)
        grouped[("setup", row.setup or "unknown")].append(row)
    return [
        summarize_bucket(bucket_type, bucket, bucket_rows)
        for (bucket_type, bucket), bucket_rows in sorted(grouped.items())
    ]


def summarize_bucket(
    bucket_type: str,
    bucket: str,
    rows: list[FillQualityRow],
) -> FillQualityBucketRow:
    closed_pnls = [
        row.closed_trade_pnl_usd
        for row in rows
        if row.closed_trade_pnl_usd is not None
    ]
    wins = len([pnl for pnl in closed_pnls if pnl > 0.0])
    return FillQualityBucketRow(
        bucket_type=bucket_type,
        bucket=bucket,
        decisions=len(rows),
        opened=len([row for row in rows if row.status == "opened"]),
        accepted_skipped=len([row for row in rows if row.status in {"accepted_skipped", "accepted_not_opened"}]),
        risk_rejected=len([row for row in rows if row.status == "risk_rejected"]),
        closed_trades=len(closed_pnls),
        closed_pnl_usd=round(sum(closed_pnls), 6),
        win_rate=round(wins / len(closed_pnls), 6) if closed_pnls else None,
        avg_expected_entry_cost_bps=avg(row.expected_entry_cost_bps for row in rows),
        avg_spread_bps=avg(row.spread_bps for row in rows),
        avg_depth_to_order_ratio=avg(row.depth_to_order_ratio for row in rows),
        avg_future_return_1m_bps=avg(row.future_return_1m_bps for row in rows),
        avg_future_return_5m_bps=avg(row.future_return_5m_bps for row in rows),
        avg_future_return_15m_bps=avg(row.future_return_15m_bps for row in rows),
        avg_adverse_return_15m_bps=avg(row.adverse_return_15m_bps for row in rows),
        avg_mae_15m_bps=avg(row.mae_15m_bps for row in rows),
    )


def profit_factor_from_rows(rows: list[FillQualityRow]) -> float | None:
    pnls = [row.closed_trade_pnl_usd for row in rows if row.closed_trade_pnl_usd is not None]
    gains = sum(pnl for pnl in pnls if pnl > 0)
    losses = -sum(pnl for pnl in pnls if pnl < 0)
    if losses <= 0:
        return None
    return round(gains / losses, 6)


def spread_bucket(value: float) -> str:
    if value < 1.0:
        return "lt_1bps"
    if value < 2.5:
        return "1_2p5bps"
    if value < 5.0:
        return "2p5_5bps"
    if value < 10.0:
        return "5_10bps"
    return "gte_10bps"


def cost_bucket(value: float) -> str:
    if value < 1.0:
        return "lt_1bps"
    if value < 2.0:
        return "1_2bps"
    if value < 4.0:
        return "2_4bps"
    if value < 8.0:
        return "4_8bps"
    return "gte_8bps"


def depth_ratio_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value < 1.0:
        return "lt_1x"
    if value < 2.0:
        return "1_2x"
    if value < 5.0:
        return "2_5x"
    if value < 10.0:
        return "5_10x"
    return "gte_10x"


def expected_entry_cost_bps(
    *,
    spread_bps: float,
    spread_multiplier: float,
    slippage_bps: float,
) -> float:
    return max(spread_bps, 0.0) * max(spread_multiplier, 0.0) + max(slippage_bps, 0.0)


def ratio(numerator: float | None, denominator: float) -> float | None:
    if numerator is None or denominator <= 0:
        return None
    return round(max(numerator, 0.0) / denominator, 6)


def avg(values: Iterable[float | None]) -> float | None:
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return None
    return round(sum(clean) / len(clean), 6)


def optional_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def timestamp_key(value: object) -> str:
    parsed = parse_timestamp(str(value)) if value not in (None, "") else None
    return isoformat(parsed) if parsed is not None else str(value or "")


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
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def write_markdown(
    path: Path,
    payload: dict[str, Any],
    buckets: list[FillQualityBucketRow],
) -> None:
    status_counts = payload.get("status_counts", {})
    replay = payload.get("replay", {})
    lines = [
        "# P117 fill-quality audit Pod A",
        "",
        f"- generated_at: `{payload.get('generated_at')}`",
        "- decision: `research_only_no_live_change`",
        f"- records_processed: `{replay.get('records_processed')}`",
        f"- signal_count: `{replay.get('signal_count')}`",
        f"- opened_count: `{replay.get('opened_count')}`",
        f"- skipped_open_count: `{replay.get('skipped_open_count')}`",
        f"- realized_pnl_usd: `{replay.get('realized_pnl_usd')}`",
        f"- profit_factor: `{replay.get('profit_factor')}`",
        f"- status_counts: `{status_counts}`",
        "",
        "## Status buckets",
        "",
        "| Bucket | Decisions | Opened | Skipped | Rejected | Closed PnL | Avg cost bps | Avg 15m ret bps | Avg 15m adverse bps |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in buckets:
        if row.bucket_type != "status":
            continue
        lines.append(
            "| "
            f"`{row.bucket}` | {row.decisions} | {row.opened} | "
            f"{row.accepted_skipped} | {row.risk_rejected} | "
            f"{row.closed_pnl_usd:.2f} | {fmt(row.avg_expected_entry_cost_bps)} | "
            f"{fmt(row.avg_future_return_15m_bps)} | {fmt(row.avg_adverse_return_15m_bps)} |"
        )
    lines.extend(
        [
            "",
            "## Depth buckets",
            "",
            "| Bucket | Decisions | Opened | Closed PnL | Avg depth/order | Avg 15m ret bps | Avg 15m MAE bps |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in buckets:
        if row.bucket_type != "depth_ratio":
            continue
        lines.append(
            "| "
            f"`{row.bucket}` | {row.decisions} | {row.opened} | "
            f"{row.closed_pnl_usd:.2f} | {fmt(row.avg_depth_to_order_ratio)} | "
            f"{fmt(row.avg_future_return_15m_bps)} | {fmt(row.avg_mae_15m_bps)} |"
        )
    lines.extend(
        [
            "",
            "## Lecture",
            "",
            "- Ce rapport ne propose aucune activation live.",
            "- Les lignes `accepted_skipped` mesurent les signaux acceptes par le risk gate mais non ouverts par l'execution.",
            "- Les lignes `risk_rejected` servent a verifier si les rejets auraient eu un bon ou mauvais retour directionnel court terme.",
            "- Les buckets depth/spread/cost doivent guider les prochaines variantes cap-only ou repricing, pas un blocage direct.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


if __name__ == "__main__":
    main()
