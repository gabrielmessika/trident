#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from app.backtest.snapshot_loader import SnapshotLoader


DEFAULT_BASELINE_INPUT = (
    "server-data/replay_inputs/external_reference_multisource_20260405_20260513_baseline.jsonl"
)
DEFAULT_RECENT_INPUT = "server-data/live_snapshots"
DEFAULT_SYMBOLS = "BTC,ETH,SOL,HYPE"


@dataclass(slots=True)
class MarketPoint:
    timestamp: datetime
    symbol: str
    price: float
    ema_fast: float
    ema_slow: float
    vwap_distance_bps: float
    structure_score: float
    spread_bps: float
    book_imbalance: float
    trade_flow_bias: float
    bucket_notional_usd: float
    bucket_trade_count: int
    regime: dict[str, Any]
    returns_bps: dict[int, float] = field(default_factory=dict)
    future_returns_bps: dict[int, float] = field(default_factory=dict)


@dataclass(slots=True)
class RegimeSnapshot:
    timestamp: str
    bear_score: int
    bull_score: int
    btc_ret_60m_bps: float | None
    btc_ret_240m_bps: float | None
    btc_fwd_360m_bps: float | None
    structure_score: float
    breadth_pct: float | None
    leader_trend_score: float | None


@dataclass(slots=True)
class PatternTrade:
    window: str
    pattern: str
    side: str
    symbol: str
    opened_at: str
    closed_at: str
    close_reason: str
    entry_price: float
    exit_price: float
    bear_score: int
    bull_score: int
    btc_ret_60m_bps: float | None
    btc_ret_240m_bps: float | None
    symbol_ret_60m_bps: float | None
    symbol_ret_240m_bps: float | None
    gross_bps: float
    net_bps: float
    pnl_usd: float
    mfe_bps: float
    mae_bps: float
    round_trip_cost_bps: float


@dataclass(slots=True)
class PatternSummary:
    window: str
    pattern: str
    side: str
    bear_bucket: str
    trade_count: int
    pnl_usd: float
    avg_net_bps: float
    win_rate: float | None
    profit_factor: float | None
    avg_mfe_bps: float
    avg_mae_bps: float
    close_reasons: dict[str, int]


@dataclass(slots=True)
class ClassifierSummary:
    window: str
    threshold: int
    observations: int
    predicted_bear: int
    true_bear: int
    true_positive: int
    precision: float | None
    recall: float | None
    avg_future_btc_bps_when_predicted: float | None


def parse_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def load_market_points(
    input_path: Path,
    *,
    symbols: set[str] | None,
    start: datetime | None,
    end: datetime | None,
) -> dict[str, list[MarketPoint]]:
    loader = SnapshotLoader()
    points_by_symbol: dict[str, list[MarketPoint]] = defaultdict(list)
    for record in loader.iter_merged_jsonl(input_path):
        timestamp = parse_timestamp(record.timestamp)
        if timestamp is None:
            continue
        if start is not None and timestamp < start:
            continue
        if end is not None and timestamp > end:
            continue
        raw_regime = (
            record.cluster_regime_snapshots.get("crypto")
            if isinstance(record.cluster_regime_snapshots, dict)
            else None
        )
        regime = dict(raw_regime or record.regime_snapshot or {})
        for raw_symbol in record.symbols:
            symbol = str(raw_symbol.get("symbol") or "").strip().upper()
            if not symbol or symbol.startswith("XYZ:"):
                continue
            if symbols is not None and symbol not in symbols:
                continue
            price = _float(raw_symbol.get("price"))
            if price <= 0:
                continue
            points_by_symbol[symbol].append(
                MarketPoint(
                    timestamp=timestamp,
                    symbol=symbol,
                    price=price,
                    ema_fast=_float(raw_symbol.get("ema_fast")),
                    ema_slow=_float(raw_symbol.get("ema_slow")),
                    vwap_distance_bps=_float(raw_symbol.get("vwap_distance_bps")),
                    structure_score=_float(raw_symbol.get("structure_score")),
                    spread_bps=max(_float(raw_symbol.get("spread_bps")), 0.0),
                    book_imbalance=_float(raw_symbol.get("book_imbalance")),
                    trade_flow_bias=_float(raw_symbol.get("trade_flow_bias")),
                    bucket_notional_usd=max(_float(raw_symbol.get("bucket_notional_usd")), 0.0),
                    bucket_trade_count=int(max(_float(raw_symbol.get("bucket_trade_count")), 0.0)),
                    regime=regime,
                )
            )
    for symbol_points in points_by_symbol.values():
        symbol_points.sort(key=lambda item: item.timestamp)
        _annotate_returns(symbol_points, horizons_minutes=[60, 240])
        _annotate_future_returns(symbol_points, horizons_minutes=[180, 360])
    return dict(points_by_symbol)


def _annotate_returns(points: list[MarketPoint], *, horizons_minutes: list[int]) -> None:
    timestamps = [point.timestamp for point in points]
    for horizon in horizons_minutes:
        cursor = 0
        delta = timedelta(minutes=horizon)
        for point in points:
            cutoff = point.timestamp - delta
            cursor = max(cursor, bisect_right(timestamps, cutoff) - 1)
            if cursor >= 0 and timestamps[cursor] <= cutoff:
                previous = points[cursor]
                if previous.price > 0:
                    point.returns_bps[horizon] = (point.price / previous.price - 1.0) * 10000.0


def _annotate_future_returns(points: list[MarketPoint], *, horizons_minutes: list[int]) -> None:
    timestamps = [point.timestamp for point in points]
    for horizon in horizons_minutes:
        delta = timedelta(minutes=horizon)
        tolerance = timedelta(minutes=5)
        for point_index, point in enumerate(points):
            target = point.timestamp + delta
            index = bisect_right(timestamps, target) - 1
            if (
                index > point_index
                and index < len(points)
                and timestamps[index] >= target - tolerance
            ):
                future = points[index]
                if point.price > 0:
                    point.future_returns_bps[horizon] = (future.price / point.price - 1.0) * 10000.0


def build_regime_snapshots(
    btc_points: list[MarketPoint],
    *,
    sample_step_minutes: int,
) -> list[RegimeSnapshot]:
    snapshots: list[RegimeSnapshot] = []
    last_sample: datetime | None = None
    for point in btc_points:
        if last_sample is not None and point.timestamp - last_sample < timedelta(minutes=sample_step_minutes):
            continue
        last_sample = point.timestamp
        snapshots.append(
            RegimeSnapshot(
                timestamp=_iso(point.timestamp),
                bear_score=bear_score(point, point),
                bull_score=bull_score(point, point),
                btc_ret_60m_bps=_maybe(point.returns_bps.get(60)),
                btc_ret_240m_bps=_maybe(point.returns_bps.get(240)),
                btc_fwd_360m_bps=_maybe(point.future_returns_bps.get(360)),
                structure_score=_float(point.regime.get("structure_score")),
                breadth_pct=_maybe(point.regime.get("breadth_pct")),
                leader_trend_score=_maybe(point.regime.get("leader_trend_score")),
            )
        )
    return snapshots


def bear_score(point: MarketPoint, btc_point: MarketPoint | None) -> int:
    score = 0
    btc_60 = btc_point.returns_bps.get(60) if btc_point is not None else None
    btc_240 = btc_point.returns_bps.get(240) if btc_point is not None else None
    if btc_60 is not None and btc_60 <= -35.0:
        score += 1
    if btc_240 is not None and btc_240 <= -120.0:
        score += 1
    if btc_point is not None and btc_point.ema_slow > 0 and btc_point.price < btc_point.ema_slow:
        score += 1
    if _float(point.regime.get("structure_score")) <= 0.20:
        score += 1
    breadth = _maybe(point.regime.get("breadth_pct"))
    alt_participation = _maybe(point.regime.get("alt_participation_pct"))
    if (breadth is not None and breadth <= 0.45) or (
        alt_participation is not None and alt_participation <= 0.45
    ):
        score += 1
    leader_trend = _maybe(point.regime.get("leader_trend_score"))
    if leader_trend is not None and leader_trend <= -0.05:
        score += 1
    if point.returns_bps.get(240, 0.0) <= -120.0 and point.returns_bps.get(60, 0.0) <= -20.0:
        score += 1
    return score


def bull_score(point: MarketPoint, btc_point: MarketPoint | None) -> int:
    score = 0
    btc_60 = btc_point.returns_bps.get(60) if btc_point is not None else None
    btc_240 = btc_point.returns_bps.get(240) if btc_point is not None else None
    if btc_60 is not None and btc_60 >= 35.0:
        score += 1
    if btc_240 is not None and btc_240 >= 120.0:
        score += 1
    if btc_point is not None and btc_point.ema_slow > 0 and btc_point.price > btc_point.ema_slow:
        score += 1
    if _float(point.regime.get("structure_score")) >= 0.20:
        score += 1
    breadth = _maybe(point.regime.get("breadth_pct"))
    alt_participation = _maybe(point.regime.get("alt_participation_pct"))
    if (breadth is not None and breadth >= 0.55) or (
        alt_participation is not None and alt_participation >= 0.55
    ):
        score += 1
    leader_trend = _maybe(point.regime.get("leader_trend_score"))
    if leader_trend is not None and leader_trend >= 0.05:
        score += 1
    if point.returns_bps.get(240, 0.0) >= 120.0 and point.returns_bps.get(60, 0.0) >= 20.0:
        score += 1
    return score


def discover_patterns(
    point: MarketPoint,
    *,
    btc_point: MarketPoint | None,
    min_bucket_notional_usd: float,
    min_bucket_trade_count: int,
    max_spread_bps: float,
) -> list[tuple[str, str]]:
    if point.bucket_trade_count < min_bucket_trade_count:
        return []
    if point.bucket_notional_usd < min_bucket_notional_usd:
        return []
    if point.spread_bps > max_spread_bps:
        return []
    if point.ema_slow <= 0 or point.price <= 0:
        return []

    b_score = bear_score(point, btc_point)
    u_score = bull_score(point, btc_point)
    ret60 = point.returns_bps.get(60)
    ret240 = point.returns_bps.get(240)
    patterns: list[tuple[str, str]] = []

    if (
        u_score >= 3
        and b_score <= 2
        and ret60 is not None
        and ret60 >= 10.0
        and point.price >= point.ema_slow
        and point.ema_fast >= point.ema_slow
        and -20.0 <= point.vwap_distance_bps <= 20.0
    ):
        patterns.append(("long_trend_pullback_control", "long"))

    if (
        b_score >= 4
        and ret60 is not None
        and ret60 >= -20.0
        and point.price >= point.ema_slow
        and -15.0 <= point.vwap_distance_bps <= 25.0
    ):
        patterns.append(("long_in_bear_control", "long"))

    if (
        b_score >= 4
        and ret60 is not None
        and ret240 is not None
        and ret60 <= -20.0
        and ret240 <= -100.0
        and point.price <= point.ema_slow
        and point.ema_fast <= point.ema_slow
        and point.vwap_distance_bps <= 2.0
    ):
        patterns.append(("short_downtrend_continuation", "short"))

    if (
        b_score >= 4
        and ret240 is not None
        and ret240 <= -80.0
        and point.vwap_distance_bps >= 2.0
        and point.trade_flow_bias <= 0.25
        and point.price <= point.ema_slow * 1.003
    ):
        patterns.append(("short_vwap_rejection", "short"))

    if (
        b_score >= 3
        and ret60 is not None
        and ret60 <= -15.0
        and point.trade_flow_bias <= -0.25
        and point.book_imbalance <= -0.10
        and point.vwap_distance_bps <= 2.0
    ):
        patterns.append(("short_flow_book_aligned", "short"))

    return patterns


def run_window_analysis(
    *,
    window_label: str,
    input_path: Path,
    symbols: set[str] | None,
    start: datetime | None,
    end: datetime | None,
    sample_step_minutes: int,
    horizon_minutes: int,
    notional_usd: float,
    stop_bps: float,
    take_profit_bps: float,
    round_trip_cost_bps: float,
    min_bucket_notional_usd: float,
    min_bucket_trade_count: int,
    max_spread_bps: float,
) -> tuple[list[PatternTrade], list[PatternSummary], list[ClassifierSummary], list[RegimeSnapshot], dict[str, Any]]:
    points_by_symbol = load_market_points(input_path, symbols=symbols, start=start, end=end)
    btc_points = points_by_symbol.get("BTC", [])
    if not btc_points:
        raise ValueError(f"{input_path}: no BTC points found for {window_label}")
    btc_index = _PointIndex(btc_points)
    regime_snapshots = build_regime_snapshots(btc_points, sample_step_minutes=sample_step_minutes)
    trades: list[PatternTrade] = []
    for symbol, points in sorted(points_by_symbol.items()):
        last_sample: datetime | None = None
        for index, point in enumerate(points):
            if last_sample is not None and point.timestamp - last_sample < timedelta(minutes=sample_step_minutes):
                continue
            last_sample = point.timestamp
            btc_point = btc_index.at_or_before(point.timestamp)
            for pattern, side in discover_patterns(
                point,
                btc_point=btc_point,
                min_bucket_notional_usd=min_bucket_notional_usd,
                min_bucket_trade_count=min_bucket_trade_count,
                max_spread_bps=max_spread_bps,
            ):
                trade = simulate_pattern_trade(
                    window_label=window_label,
                    pattern=pattern,
                    side=side,
                    point=point,
                    future_points=points[index + 1 :],
                    btc_point=btc_point,
                    horizon_minutes=horizon_minutes,
                    notional_usd=notional_usd,
                    stop_bps=stop_bps,
                    take_profit_bps=take_profit_bps,
                    round_trip_cost_bps=round_trip_cost_bps,
                )
                if trade is not None:
                    trades.append(trade)
    summaries = summarize_pattern_trades(trades)
    classifier = summarize_classifier(window_label, regime_snapshots, bear_outcome_bps=-100.0)
    meta = {
        "input_path": str(input_path),
        "window_label": window_label,
        "first_timestamp": _iso(min((p.timestamp for points in points_by_symbol.values() for p in points), default=None)),
        "last_timestamp": _iso(max((p.timestamp for points in points_by_symbol.values() for p in points), default=None)),
        "symbols": sorted(points_by_symbol),
        "point_count": sum(len(points) for points in points_by_symbol.values()),
        "sample_step_minutes": sample_step_minutes,
        "horizon_minutes": horizon_minutes,
        "notional_usd": notional_usd,
        "stop_bps": stop_bps,
        "take_profit_bps": take_profit_bps,
        "round_trip_cost_bps": round_trip_cost_bps,
    }
    return trades, summaries, classifier, regime_snapshots, meta


def simulate_pattern_trade(
    *,
    window_label: str,
    pattern: str,
    side: str,
    point: MarketPoint,
    future_points: list[MarketPoint],
    btc_point: MarketPoint | None,
    horizon_minutes: int,
    notional_usd: float,
    stop_bps: float,
    take_profit_bps: float,
    round_trip_cost_bps: float,
) -> PatternTrade | None:
    deadline = point.timestamp + timedelta(minutes=horizon_minutes)
    close_point: MarketPoint | None = None
    close_reason = "time_stop"
    mfe_bps = -math.inf
    mae_bps = math.inf
    gross_bps = 0.0
    for future in future_points:
        if future.timestamp > deadline:
            break
        gross_bps = side_return_bps(point.price, future.price, side)
        mfe_bps = max(mfe_bps, gross_bps)
        mae_bps = min(mae_bps, gross_bps)
        close_point = future
        if gross_bps <= -stop_bps:
            close_reason = "stop"
            break
        if gross_bps >= take_profit_bps:
            close_reason = "take_profit"
            break
    if close_point is None:
        return None
    if mfe_bps == -math.inf:
        mfe_bps = gross_bps
    if mae_bps == math.inf:
        mae_bps = gross_bps
    net_bps = gross_bps - round_trip_cost_bps
    return PatternTrade(
        window=window_label,
        pattern=pattern,
        side=side,
        symbol=point.symbol,
        opened_at=_iso(point.timestamp),
        closed_at=_iso(close_point.timestamp),
        close_reason=close_reason,
        entry_price=round(point.price, 10),
        exit_price=round(close_point.price, 10),
        bear_score=bear_score(point, btc_point),
        bull_score=bull_score(point, btc_point),
        btc_ret_60m_bps=_maybe(btc_point.returns_bps.get(60) if btc_point else None),
        btc_ret_240m_bps=_maybe(btc_point.returns_bps.get(240) if btc_point else None),
        symbol_ret_60m_bps=_maybe(point.returns_bps.get(60)),
        symbol_ret_240m_bps=_maybe(point.returns_bps.get(240)),
        gross_bps=round(gross_bps, 6),
        net_bps=round(net_bps, 6),
        pnl_usd=round(notional_usd * net_bps / 10000.0, 6),
        mfe_bps=round(mfe_bps, 6),
        mae_bps=round(mae_bps, 6),
        round_trip_cost_bps=round_trip_cost_bps,
    )


def summarize_pattern_trades(trades: list[PatternTrade]) -> list[PatternSummary]:
    grouped: dict[tuple[str, str, str, str], list[PatternTrade]] = defaultdict(list)
    for trade in trades:
        grouped[(trade.window, trade.pattern, trade.side, bear_bucket(trade.bear_score))].append(trade)
    summaries: list[PatternSummary] = []
    for (window, pattern, side, bucket), rows in sorted(grouped.items()):
        pnl = sum(row.pnl_usd for row in rows)
        gains = sum(row.pnl_usd for row in rows if row.pnl_usd > 0)
        losses = -sum(row.pnl_usd for row in rows if row.pnl_usd < 0)
        summaries.append(
            PatternSummary(
                window=window,
                pattern=pattern,
                side=side,
                bear_bucket=bucket,
                trade_count=len(rows),
                pnl_usd=round(pnl, 6),
                avg_net_bps=round(sum(row.net_bps for row in rows) / len(rows), 6),
                win_rate=sum(1 for row in rows if row.pnl_usd > 0) / len(rows),
                profit_factor=(gains / losses if losses > 0 else None),
                avg_mfe_bps=round(sum(row.mfe_bps for row in rows) / len(rows), 6),
                avg_mae_bps=round(sum(row.mae_bps for row in rows) / len(rows), 6),
                close_reasons=dict(Counter(row.close_reason for row in rows)),
            )
        )
    summaries.sort(key=lambda item: (item.window, -item.pnl_usd, item.pattern, item.bear_bucket))
    return summaries


def summarize_classifier(
    window: str,
    snapshots: list[RegimeSnapshot],
    *,
    bear_outcome_bps: float,
) -> list[ClassifierSummary]:
    rows = [row for row in snapshots if row.btc_fwd_360m_bps is not None]
    true_bear = [row for row in rows if row.btc_fwd_360m_bps is not None and row.btc_fwd_360m_bps <= bear_outcome_bps]
    summaries: list[ClassifierSummary] = []
    for threshold in range(1, 8):
        predicted = [row for row in rows if row.bear_score >= threshold]
        true_positive = [
            row
            for row in predicted
            if row.btc_fwd_360m_bps is not None and row.btc_fwd_360m_bps <= bear_outcome_bps
        ]
        summaries.append(
            ClassifierSummary(
                window=window,
                threshold=threshold,
                observations=len(rows),
                predicted_bear=len(predicted),
                true_bear=len(true_bear),
                true_positive=len(true_positive),
                precision=(len(true_positive) / len(predicted) if predicted else None),
                recall=(len(true_positive) / len(true_bear) if true_bear else None),
                avg_future_btc_bps_when_predicted=(
                    sum(row.btc_fwd_360m_bps or 0.0 for row in predicted) / len(predicted)
                    if predicted
                    else None
                ),
            )
        )
    return summaries


def side_return_bps(entry_price: float, exit_price: float, side: str) -> float:
    if entry_price <= 0 or exit_price <= 0:
        return 0.0
    if side == "short":
        return (entry_price - exit_price) / entry_price * 10000.0
    return (exit_price / entry_price - 1.0) * 10000.0


def bear_bucket(score: int) -> str:
    if score >= 5:
        return "bear_score>=5"
    if score >= 4:
        return "bear_score=4"
    if score >= 3:
        return "bear_score=3"
    return "bear_score<=2"


class _PointIndex:
    def __init__(self, points: list[MarketPoint]) -> None:
        self.points = points
        self.timestamps = [point.timestamp for point in points]

    def at_or_before(self, timestamp: datetime) -> MarketPoint | None:
        index = bisect_right(self.timestamps, timestamp) - 1
        if index < 0:
            return None
        return self.points[index]


def write_csv(path: Path, rows: Iterable[Any]) -> None:
    row_dicts = [asdict(row) for row in rows]
    if not row_dicts:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row_dicts[0]))
        writer.writeheader()
        writer.writerows(row_dicts)


def write_report(
    path: Path,
    *,
    generated_at: str,
    metas: list[dict[str, Any]],
    summaries: list[PatternSummary],
    classifiers: list[ClassifierSummary],
) -> None:
    lines = [
        "# P1-06 Bear Regime / Shorts Research",
        "",
        f"- Generated at: `{generated_at}`",
        "- Status: `research_only_no_live_change`",
        "- Goal: detect adverse crypto regimes before entry and compare long/short candidate patterns.",
        "",
        "## Inputs",
        "",
    ]
    for meta in metas:
        lines.extend(
            [
                f"- `{meta['window_label']}`: `{meta['input_path']}`",
                f"  - Window: `{meta['first_timestamp']}` -> `{meta['last_timestamp']}`",
                f"  - Symbols: `{', '.join(meta['symbols'])}`",
                f"  - Points: `{meta['point_count']}`, sample step `{meta['sample_step_minutes']}m`, horizon `{meta['horizon_minutes']}m`, notional `${meta['notional_usd']}`, round-trip cost `{meta['round_trip_cost_bps']}` bps",
            ]
        )
    lines.extend(["", "## Pattern Summary", ""])
    lines.append("| Window | Pattern | Side | Bear bucket | Trades | PnL | Avg net bps | WR | PF | MFE | MAE |")
    lines.append("|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for row in summaries:
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{row.window}`",
                    f"`{row.pattern}`",
                    f"`{row.side}`",
                    f"`{row.bear_bucket}`",
                    str(row.trade_count),
                    f"${row.pnl_usd:.2f}",
                    f"{row.avg_net_bps:.2f}",
                    _fmt_pct(row.win_rate),
                    _fmt_float(row.profit_factor),
                    f"{row.avg_mfe_bps:.2f}",
                    f"{row.avg_mae_bps:.2f}",
                ]
            )
            + " |"
        )
    lines.extend(["", "## Bear Classifier", ""])
    lines.append("| Window | Threshold | Obs | Pred bear | True bear | TP | Precision | Recall | Avg future BTC bps |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for row in classifiers:
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{row.window}`",
                    str(row.threshold),
                    str(row.observations),
                    str(row.predicted_bear),
                    str(row.true_bear),
                    str(row.true_positive),
                    _fmt_pct(row.precision),
                    _fmt_pct(row.recall),
                    _fmt_float(row.avg_future_btc_bps_when_predicted),
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Reading Notes",
            "",
            "- This is an opportunity scan, not a portfolio replay: overlapping candidates are intentionally not suppressed.",
            "- The bear score is computed from information available at the candidate timestamp: BTC 1h/4h returns, BTC EMA state, crypto breadth/structure/leader trend when available, and local symbol 1h/4h weakness.",
            "- No live setting, allowed setup, order side, cap, stop, or sizing is changed by this research run.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-input", default=DEFAULT_BASELINE_INPUT)
    parser.add_argument("--recent-input", default=DEFAULT_RECENT_INPUT)
    parser.add_argument("--baseline-start", default="2026-04-05T00:00:00Z")
    parser.add_argument("--baseline-end", default="2026-05-13T23:59:59Z")
    parser.add_argument("--recent-start", default="2026-05-24T00:00:00Z")
    parser.add_argument("--recent-end", default="2026-06-11T23:59:59Z")
    parser.add_argument("--symbols", default=DEFAULT_SYMBOLS, help="'all' or comma-separated symbols")
    parser.add_argument("--sample-step-minutes", type=int, default=15)
    parser.add_argument("--horizon-minutes", type=int, default=180)
    parser.add_argument("--notional-usd", type=float, default=200.0)
    parser.add_argument("--stop-bps", type=float, default=120.0)
    parser.add_argument("--take-profit-bps", type=float, default=240.0)
    parser.add_argument("--round-trip-cost-bps", type=float, default=16.0)
    parser.add_argument("--min-bucket-notional-usd", type=float, default=100.0)
    parser.add_argument("--min-bucket-trade-count", type=int, default=3)
    parser.add_argument("--max-spread-bps", type=float, default=10.0)
    parser.add_argument("--output-dir", default="")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    generated_at = utc_stamp()
    output_dir = Path(args.output_dir) if args.output_dir else Path(
        f"server-data/replay_reports/p106_bear_regime_short_research_{generated_at}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    symbols = None if args.symbols.strip().lower() == "all" else {
        item.strip().upper() for item in args.symbols.split(",") if item.strip()
    }
    all_trades: list[PatternTrade] = []
    all_summaries: list[PatternSummary] = []
    all_classifiers: list[ClassifierSummary] = []
    all_regime_rows: list[RegimeSnapshot] = []
    metas: list[dict[str, Any]] = []
    for label, input_path, start, end in [
        ("baseline_apr_may", Path(args.baseline_input), args.baseline_start, args.baseline_end),
        ("recent_may_jun", Path(args.recent_input), args.recent_start, args.recent_end),
    ]:
        trades, summaries, classifiers, regime_rows, meta = run_window_analysis(
            window_label=label,
            input_path=input_path,
            symbols=symbols,
            start=parse_timestamp(start),
            end=parse_timestamp(end),
            sample_step_minutes=args.sample_step_minutes,
            horizon_minutes=args.horizon_minutes,
            notional_usd=args.notional_usd,
            stop_bps=args.stop_bps,
            take_profit_bps=args.take_profit_bps,
            round_trip_cost_bps=args.round_trip_cost_bps,
            min_bucket_notional_usd=args.min_bucket_notional_usd,
            min_bucket_trade_count=args.min_bucket_trade_count,
            max_spread_bps=args.max_spread_bps,
        )
        all_trades.extend(trades)
        all_summaries.extend(summaries)
        all_classifiers.extend(classifiers)
        all_regime_rows.extend(regime_rows)
        metas.append(meta)

    write_csv(output_dir / "pattern_trades.csv", all_trades)
    write_csv(output_dir / "pattern_summary.csv", all_summaries)
    write_csv(output_dir / "regime_classifier.csv", all_classifiers)
    write_csv(output_dir / "regime_samples.csv", all_regime_rows)
    payload = {
        "generated_at": generated_at,
        "status": "research_only_no_live_change",
        "meta": metas,
        "pattern_summary": [asdict(row) for row in all_summaries],
        "regime_classifier": [asdict(row) for row in all_classifiers],
    }
    (output_dir / "p106_bear_regime_report.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_report(
        output_dir / "p106_bear_regime_report.md",
        generated_at=generated_at,
        metas=metas,
        summaries=all_summaries,
        classifiers=all_classifiers,
    )
    print(output_dir)


def _float(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return parsed


def _maybe(value: object) -> float | None:
    parsed = _float(value, default=math.nan)
    if not math.isfinite(parsed):
        return None
    return parsed


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1%}"


def _fmt_float(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}"


if __name__ == "__main__":
    main()
