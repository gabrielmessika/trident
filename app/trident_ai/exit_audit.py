from __future__ import annotations

import bisect
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.backtest.snapshot_loader import open_jsonl_text, resolve_jsonl_files
from app.trident_ai.config import TridentAIConfig, load_trident_ai_config
from app.trident_ai.paper import PAPER_REPLAY_TRADE_CLOSED_EVENT


DEFAULT_EXIT_AUDIT_WINDOWS_MINUTES: tuple[int, ...] = (15, 30, 60)
DEFAULT_EARLY_ADVERSE_BPS = 25.0
DEFAULT_MIN_FOLLOW_THROUGH_BPS = 15.0
DEFAULT_GIVEBACK_BPS = 25.0


@dataclass(frozen=True, slots=True)
class TridentAIExitFollowThroughAuditResult:
    paper_journal_paths: tuple[str, ...]
    market_input_paths: tuple[str, ...]
    fold_labels: tuple[str, ...]
    report_json_path: str
    report_md_path: str
    early_windows_minutes: tuple[int, ...] = DEFAULT_EXIT_AUDIT_WINDOWS_MINUTES
    early_adverse_bps: float = DEFAULT_EARLY_ADVERSE_BPS
    min_follow_through_bps: float = DEFAULT_MIN_FOLLOW_THROUGH_BPS
    giveback_bps: float = DEFAULT_GIVEBACK_BPS
    trades_seen: int = 0
    trades_with_path: int = 0
    missing_paths: int = 0
    time_stop_trades: int = 0
    losing_time_stop_trades: int = 0
    realized_pnl_usd: float = 0.0
    gross_pnl_usd: float = 0.0
    fees_usd: float = 0.0
    avg_net_bps: float = 0.0
    avg_gross_bps: float = 0.0
    avg_mfe_bps: float = 0.0
    avg_mae_bps: float = 0.0
    avg_duration_minutes: float = 0.0
    close_reason_counts: dict[str, int] = field(default_factory=dict)
    classification_counts: dict[str, int] = field(default_factory=dict)
    window_stats: dict[str, dict[str, object]] = field(default_factory=dict)
    folds: list[dict[str, object]] = field(default_factory=list)
    trades: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "paper_journal_paths": list(self.paper_journal_paths),
            "market_input_paths": list(self.market_input_paths),
            "fold_labels": list(self.fold_labels),
            "report_json_path": self.report_json_path,
            "report_md_path": self.report_md_path,
            "early_windows_minutes": list(self.early_windows_minutes),
            "early_adverse_bps": round(self.early_adverse_bps, 6),
            "min_follow_through_bps": round(self.min_follow_through_bps, 6),
            "giveback_bps": round(self.giveback_bps, 6),
            "trades_seen": self.trades_seen,
            "trades_with_path": self.trades_with_path,
            "missing_paths": self.missing_paths,
            "time_stop_trades": self.time_stop_trades,
            "losing_time_stop_trades": self.losing_time_stop_trades,
            "realized_pnl_usd": round(self.realized_pnl_usd, 6),
            "gross_pnl_usd": round(self.gross_pnl_usd, 6),
            "fees_usd": round(self.fees_usd, 6),
            "avg_net_bps": round(self.avg_net_bps, 6),
            "avg_gross_bps": round(self.avg_gross_bps, 6),
            "avg_mfe_bps": round(self.avg_mfe_bps, 6),
            "avg_mae_bps": round(self.avg_mae_bps, 6),
            "avg_duration_minutes": round(self.avg_duration_minutes, 6),
            "close_reason_counts": dict(sorted(self.close_reason_counts.items())),
            "classification_counts": dict(sorted(self.classification_counts.items())),
            "window_stats": self.window_stats,
            "folds": self.folds,
            "trades": self.trades,
        }


def run_trident_ai_exit_follow_through_audit(
    *,
    paper_journal_paths: Sequence[str | Path],
    market_input_paths: Sequence[str | Path],
    fold_labels: Sequence[str] | None = None,
    config: TridentAIConfig | None = None,
    report_json_path: str | Path | None = None,
    report_md_path: str | Path | None = None,
    early_windows_minutes: tuple[int, ...] = DEFAULT_EXIT_AUDIT_WINDOWS_MINUTES,
    early_adverse_bps: float = DEFAULT_EARLY_ADVERSE_BPS,
    min_follow_through_bps: float = DEFAULT_MIN_FOLLOW_THROUGH_BPS,
    giveback_bps: float = DEFAULT_GIVEBACK_BPS,
) -> TridentAIExitFollowThroughAuditResult:
    if not paper_journal_paths:
        raise ValueError("paper_journal_paths_required")
    if len(paper_journal_paths) != len(market_input_paths):
        raise ValueError("paper_and_market_input_counts_must_match")
    windows = _normalize_windows(early_windows_minutes)
    if early_adverse_bps <= 0.0:
        raise ValueError("early_adverse_bps_must_be_positive")
    if min_follow_through_bps < 0.0:
        raise ValueError("min_follow_through_bps_must_be_non_negative")
    if giveback_bps <= 0.0:
        raise ValueError("giveback_bps_must_be_positive")

    resolved_config = config or load_trident_ai_config()
    run_id = _timestamp_id(datetime.now(timezone.utc))
    output_dir = Path(resolved_config.paths.replay_output_dir)
    json_output = Path(
        report_json_path or output_dir / f"trident_ai_exit_follow_through_audit_{run_id}.json"
    )
    md_output = Path(
        report_md_path or output_dir / f"trident_ai_exit_follow_through_audit_{run_id}.md"
    )
    labels = _fold_labels(fold_labels, len(paper_journal_paths))

    all_trades: list[dict[str, object]] = []
    folds: list[dict[str, object]] = []
    for label, paper_path, market_path in zip(labels, paper_journal_paths, market_input_paths, strict=True):
        market_index = _market_price_index(market_path)
        fold_trades = [
            _trade_item(
                row,
                market_index=market_index,
                fold_label=label,
                paper_journal_path=str(paper_path),
                market_input_path=str(market_path),
                windows=windows,
                early_adverse_bps=float(early_adverse_bps),
                min_follow_through_bps=float(min_follow_through_bps),
                giveback_bps=float(giveback_bps),
            )
            for row in _closed_trade_rows(paper_path)
        ]
        all_trades.extend(fold_trades)
        folds.append(
            {
                "fold_label": label,
                "paper_journal_path": str(paper_path),
                "market_input_path": str(market_path),
                **_summary(fold_trades, windows=windows),
            }
        )

    summary = _summary(all_trades, windows=windows)
    result = TridentAIExitFollowThroughAuditResult(
        paper_journal_paths=tuple(str(path) for path in paper_journal_paths),
        market_input_paths=tuple(str(path) for path in market_input_paths),
        fold_labels=labels,
        report_json_path=str(json_output),
        report_md_path=str(md_output),
        early_windows_minutes=windows,
        early_adverse_bps=float(early_adverse_bps),
        min_follow_through_bps=float(min_follow_through_bps),
        giveback_bps=float(giveback_bps),
        folds=folds,
        trades=_sort_trade_items(all_trades),
        **summary,
    )
    payload = build_exit_follow_through_audit_report_payload(
        result=result,
        generated_at=_format_timestamp(datetime.now(timezone.utc)),
    )
    _write_report_outputs(payload, json_path=json_output, md_path=md_output)
    return result


def build_exit_follow_through_audit_report_payload(
    *,
    result: TridentAIExitFollowThroughAuditResult,
    generated_at: str,
) -> dict[str, object]:
    return {
        "generated_at": generated_at,
        "kind": "trident_ai_exit_follow_through_audit",
        "result": result.to_dict(),
    }


def _closed_trade_rows(path: str | Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for row in _iter_jsonl(path):
        if row.get("event_type") != PAPER_REPLAY_TRADE_CLOSED_EVENT:
            continue
        trade = row.get("trade")
        if isinstance(trade, Mapping):
            rows.append(dict(trade))
    return rows


def _trade_item(
    trade: Mapping[str, object],
    *,
    market_index: dict[str, list[tuple[datetime, str, float]]],
    fold_label: str,
    paper_journal_path: str,
    market_input_path: str,
    windows: tuple[int, ...],
    early_adverse_bps: float,
    min_follow_through_bps: float,
    giveback_bps: float,
) -> dict[str, object]:
    symbol = str(trade.get("symbol", "") or "").strip().upper()
    side = str(trade.get("side", "") or "").strip().lower()
    opened_at = _parse_timestamp(str(trade.get("opened_at", "") or ""))
    closed_at = _parse_timestamp(str(trade.get("closed_at", "") or ""))
    entry_price = _number(trade.get("entry_price"))
    exit_price = _number(trade.get("exit_price"))
    notional = _number(trade.get("notional_usd"))
    gross_pnl = _number(trade.get("gross_pnl_usd"))
    fees = _number(trade.get("fees_usd"))
    pnl = _number(trade.get("pnl_usd"))
    gross_bps = _pnl_bps(gross_pnl, notional)
    net_bps = _pnl_bps(pnl, notional)

    base = {
        "fold_label": fold_label,
        "paper_journal_path": paper_journal_path,
        "market_input_path": market_input_path,
        "decision_id": str(trade.get("decision_id", "") or ""),
        "symbol": symbol,
        "side": side,
        "opened_at": str(trade.get("opened_at", "") or ""),
        "closed_at": str(trade.get("closed_at", "") or ""),
        "entry_price": round(entry_price, 8),
        "exit_price": round(exit_price, 8),
        "notional_usd": round(notional, 6),
        "gross_pnl_usd": round(gross_pnl, 6),
        "fees_usd": round(fees, 6),
        "pnl_usd": round(pnl, 6),
        "gross_bps": round(gross_bps, 6),
        "net_bps": round(net_bps, 6),
        "close_reason": str(trade.get("close_reason", "") or ""),
        "confidence": round(_number(trade.get("confidence")), 6),
    }
    if opened_at is None or closed_at is None or closed_at < opened_at:
        return {
            **base,
            "path_available": False,
            "path_reason": "invalid_trade_timestamps",
            "classifications": ["missing_path"],
            "window_outcomes": {},
        }
    if entry_price <= 0.0 or exit_price <= 0.0 or notional <= 0.0:
        return {
            **base,
            "path_available": False,
            "path_reason": "invalid_trade_prices_or_notional",
            "duration_minutes": _duration_minutes(opened_at, closed_at),
            "classifications": ["missing_path"],
            "window_outcomes": {},
        }

    market_points = _market_points_between(
        market_index.get(symbol, []),
        opened_at=opened_at,
        closed_at=closed_at,
    )
    path = [
        (opened_at, _format_timestamp(opened_at), entry_price),
        *market_points,
        (closed_at, _format_timestamp(closed_at), exit_price),
    ]
    gross_path = [
        {
            "timestamp": point[1],
            "minutes_from_open": _duration_minutes(opened_at, point[0]),
            "price": round(point[2], 8),
            "gross_bps": round(_gross_move_bps(side=side, entry_price=entry_price, future_price=point[2]), 6),
        }
        for point in path
    ]
    mfe = max(_number(point["gross_bps"]) for point in gross_path)
    mae = min(_number(point["gross_bps"]) for point in gross_path)
    time_to_mfe = _number(next(point["minutes_from_open"] for point in gross_path if _number(point["gross_bps"]) == mfe))
    time_to_mae = _number(next(point["minutes_from_open"] for point in gross_path if _number(point["gross_bps"]) == mae))
    window_outcomes = _window_outcomes(
        gross_path,
        opened_at=opened_at,
        closed_at=closed_at,
        windows=windows,
    )
    path_available = bool(market_points)
    classifications = _classifications(
        close_reason=str(base["close_reason"]),
        net_bps=net_bps,
        gross_bps=gross_bps,
        mfe_bps=mfe,
        window_outcomes=window_outcomes,
        path_available=path_available,
        early_adverse_bps=early_adverse_bps,
        min_follow_through_bps=min_follow_through_bps,
        giveback_bps=giveback_bps,
    )
    return {
        **base,
        "duration_minutes": round(_duration_minutes(opened_at, closed_at), 6),
        "path_available": path_available,
        "path_reason": "ok" if path_available else "missing_market_points_between_open_close",
        "path_points": len(gross_path),
        "mfe_bps": round(mfe, 6),
        "mae_bps": round(mae, 6),
        "giveback_bps": round(max(0.0, mfe - gross_bps), 6),
        "time_to_mfe_minutes": round(time_to_mfe, 6),
        "time_to_mae_minutes": round(time_to_mae, 6),
        "window_outcomes": window_outcomes,
        "classifications": classifications,
    }


def _window_outcomes(
    gross_path: list[dict[str, object]],
    *,
    opened_at: datetime,
    closed_at: datetime,
    windows: tuple[int, ...],
) -> dict[str, dict[str, object]]:
    outcomes: dict[str, dict[str, object]] = {}
    duration = _duration_minutes(opened_at, closed_at)
    for window in windows:
        target_minutes = float(window)
        points_until = [
            point
            for point in gross_path
            if _number(point.get("minutes_from_open")) <= min(target_minutes, duration)
        ]
        if not points_until:
            outcomes[str(window)] = {"available": False, "reason": "missing_window_points"}
            continue
        point_at_window = _point_at_or_after(gross_path, target_minutes) if target_minutes <= duration else gross_path[-1]
        outcomes[str(window)] = {
            "available": point_at_window is not None,
            "target_minutes": window,
            "actual_minutes": round(_number(point_at_window.get("minutes_from_open")), 6)
            if point_at_window is not None
            else 0.0,
            "gross_at_window_bps": round(_number(point_at_window.get("gross_bps")), 6)
            if point_at_window is not None
            else 0.0,
            "early_mfe_bps": round(max(_number(point.get("gross_bps")) for point in points_until), 6),
            "early_mae_bps": round(min(_number(point.get("gross_bps")) for point in points_until), 6),
        }
    return outcomes


def _classifications(
    *,
    close_reason: str,
    net_bps: float,
    gross_bps: float,
    mfe_bps: float,
    window_outcomes: Mapping[str, Mapping[str, object]],
    path_available: bool,
    early_adverse_bps: float,
    min_follow_through_bps: float,
    giveback_bps: float,
) -> list[str]:
    if not path_available:
        return ["missing_path"]
    labels: list[str] = []
    if close_reason == "time_stop":
        labels.append("time_stop")
    if net_bps < 0.0:
        labels.append("loser")
    if close_reason == "time_stop" and net_bps < 0.0:
        labels.append("losing_time_stop")

    available_windows = [
        outcome
        for outcome in window_outcomes.values()
        if bool(outcome.get("available", False))
    ]
    worst_early_mae = min((_number(item.get("early_mae_bps")) for item in available_windows), default=0.0)
    best_early_mfe = max((_number(item.get("early_mfe_bps")) for item in available_windows), default=0.0)
    early_adverse_loss = net_bps < 0.0 and worst_early_mae <= -abs(early_adverse_bps)
    no_follow_through_loss = net_bps < 0.0 and best_early_mfe < min_follow_through_bps
    giveback = max(0.0, mfe_bps - gross_bps)

    if early_adverse_loss:
        labels.append("early_adverse_loss")
    if no_follow_through_loss:
        labels.append("no_follow_through_loss")
    if mfe_bps >= min_follow_through_bps and giveback >= giveback_bps:
        labels.append("gave_back_mfe")
        if net_bps < 0.0:
            labels.append("gave_back_to_loss")
    if (
        close_reason == "time_stop"
        and net_bps < 0.0
        and not early_adverse_loss
        and not no_follow_through_loss
        and "gave_back_to_loss" not in labels
    ):
        labels.append("late_time_stop_drift")
    if not labels:
        labels.append("unclassified")
    return labels


def _summary(trades: list[dict[str, object]], *, windows: tuple[int, ...]) -> dict[str, object]:
    path_trades = [trade for trade in trades if bool(trade.get("path_available", False))]
    time_stop = [trade for trade in trades if trade.get("close_reason") == "time_stop"]
    losing_time_stop = [
        trade
        for trade in time_stop
        if _number(trade.get("net_bps")) < 0.0
    ]
    return {
        "trades_seen": len(trades),
        "trades_with_path": len(path_trades),
        "missing_paths": len(trades) - len(path_trades),
        "time_stop_trades": len(time_stop),
        "losing_time_stop_trades": len(losing_time_stop),
        "realized_pnl_usd": round(sum(_number(trade.get("pnl_usd")) for trade in trades), 6),
        "gross_pnl_usd": round(sum(_number(trade.get("gross_pnl_usd")) for trade in trades), 6),
        "fees_usd": round(sum(_number(trade.get("fees_usd")) for trade in trades), 6),
        "avg_net_bps": round(_average([_number(trade.get("net_bps")) for trade in trades]), 6),
        "avg_gross_bps": round(_average([_number(trade.get("gross_bps")) for trade in trades]), 6),
        "avg_mfe_bps": round(_average([_number(trade.get("mfe_bps")) for trade in path_trades]), 6),
        "avg_mae_bps": round(_average([_number(trade.get("mae_bps")) for trade in path_trades]), 6),
        "avg_duration_minutes": round(_average([_number(trade.get("duration_minutes")) for trade in trades]), 6),
        "close_reason_counts": dict(Counter(str(trade.get("close_reason", "") or "unknown") for trade in trades)),
        "classification_counts": dict(_classification_counts(trades)),
        "window_stats": _window_stats(trades, windows=windows),
    }


def _window_stats(
    trades: list[dict[str, object]],
    *,
    windows: tuple[int, ...],
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for window in windows:
        rows: list[dict[str, float]] = []
        for trade in trades:
            outcomes = trade.get("window_outcomes", {})
            if not isinstance(outcomes, Mapping):
                continue
            outcome = outcomes.get(str(window))
            if not isinstance(outcome, Mapping) or not bool(outcome.get("available", False)):
                continue
            rows.append(
                {
                    "gross_at_window_bps": _number(outcome.get("gross_at_window_bps")),
                    "early_mfe_bps": _number(outcome.get("early_mfe_bps")),
                    "early_mae_bps": _number(outcome.get("early_mae_bps")),
                }
            )
        positives = [row for row in rows if row["gross_at_window_bps"] > 0.0]
        result[str(window)] = {
            "samples": len(rows),
            "positive_at_window": len(positives),
            "positive_rate": round(len(positives) / len(rows), 6) if rows else 0.0,
            "avg_gross_at_window_bps": round(_average([row["gross_at_window_bps"] for row in rows]), 6),
            "avg_early_mfe_bps": round(_average([row["early_mfe_bps"] for row in rows]), 6),
            "avg_early_mae_bps": round(_average([row["early_mae_bps"] for row in rows]), 6),
        }
    return result


def _classification_counts(trades: list[dict[str, object]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for trade in trades:
        labels = trade.get("classifications", [])
        if not isinstance(labels, list):
            continue
        for label in labels:
            if isinstance(label, str) and label:
                counts[label] += 1
    return counts


def _market_price_index(path: str | Path) -> dict[str, list[tuple[datetime, str, float]]]:
    index: dict[str, list[tuple[datetime, str, float]]] = defaultdict(list)
    for row in _iter_jsonl(path):
        timestamp = _parse_timestamp(str(row.get("timestamp", "") or ""))
        if timestamp is None:
            continue
        symbols = row.get("symbols", [])
        if not isinstance(symbols, list):
            continue
        timestamp_text = _format_timestamp(timestamp)
        for symbol_payload in symbols:
            if not isinstance(symbol_payload, Mapping):
                continue
            symbol = str(symbol_payload.get("symbol", "") or "").strip().upper()
            price = _number(symbol_payload.get("price"))
            if not symbol or price <= 0.0:
                continue
            index[symbol].append((timestamp, timestamp_text, price))
    return {symbol: sorted(points, key=lambda point: point[0]) for symbol, points in index.items()}


def _market_points_between(
    points: list[tuple[datetime, str, float]],
    *,
    opened_at: datetime,
    closed_at: datetime,
) -> list[tuple[datetime, str, float]]:
    if not points:
        return []
    times = [point[0] for point in points]
    start = bisect.bisect_left(times, opened_at)
    stop = bisect.bisect_right(times, closed_at)
    return points[start:stop]


def _point_at_or_after(
    gross_path: list[dict[str, object]],
    target_minutes: float,
) -> dict[str, object] | None:
    for point in gross_path:
        if _number(point.get("minutes_from_open")) >= target_minutes:
            return point
    return gross_path[-1] if gross_path else None


def _sort_trade_items(trades: list[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(
        trades,
        key=lambda item: (
            _number(item.get("net_bps")),
            str(item.get("opened_at", "")),
            str(item.get("symbol", "")),
        ),
    )


def _fold_labels(labels: Sequence[str] | None, expected_count: int) -> tuple[str, ...]:
    if labels is None:
        return tuple(f"fold_{index + 1}" for index in range(expected_count))
    if len(labels) != expected_count:
        raise ValueError("fold_label_count_must_match_inputs")
    normalized: list[str] = []
    for index, label in enumerate(labels):
        value = str(label).strip()
        normalized.append(value or f"fold_{index + 1}")
    return tuple(normalized)


def _normalize_windows(windows: tuple[int, ...]) -> tuple[int, ...]:
    values = sorted({int(value) for value in windows if int(value) > 0})
    if not values:
        raise ValueError("early_windows_minutes_must_be_positive")
    return tuple(values)


def _gross_move_bps(
    *,
    side: str,
    entry_price: float,
    future_price: float,
) -> float:
    if entry_price <= 0.0 or future_price <= 0.0:
        return 0.0
    if side == "short":
        return (entry_price - future_price) / entry_price * 10_000.0
    return (future_price - entry_price) / entry_price * 10_000.0


def _pnl_bps(pnl_usd: float, notional_usd: float) -> float:
    if notional_usd <= 0.0:
        return 0.0
    return pnl_usd / notional_usd * 10_000.0


def _duration_minutes(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 60.0


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _write_report_outputs(
    payload: dict[str, object],
    *,
    json_path: Path,
    md_path: Path,
) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(_render_markdown_report(payload), encoding="utf-8")


def _render_markdown_report(payload: dict[str, object]) -> str:
    result = payload["result"]
    assert isinstance(result, Mapping)
    lines = [
        "# TRIDENT-AI Exit Follow-Through Audit",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Paper journals: `{result['paper_journal_paths']}`",
        f"- Market inputs: `{result['market_input_paths']}`",
        f"- Fold labels: `{result['fold_labels']}`",
        f"- Early windows minutes: `{result['early_windows_minutes']}`",
        f"- Early adverse threshold: `{result['early_adverse_bps']:.2f} bps`",
        f"- Min follow-through threshold: `{result['min_follow_through_bps']:.2f} bps`",
        f"- Giveback threshold: `{result['giveback_bps']:.2f} bps`",
        "",
        "## Summary",
        "",
        f"- Trades seen / with path / missing path: `{result['trades_seen']}` / `{result['trades_with_path']}` / `{result['missing_paths']}`",
        f"- Time-stop trades / losing time-stops: `{result['time_stop_trades']}` / `{result['losing_time_stop_trades']}`",
        f"- Realized PnL: `${result['realized_pnl_usd']:.6f}`",
        f"- Gross PnL / fees: `${result['gross_pnl_usd']:.6f}` / `${result['fees_usd']:.6f}`",
        f"- Avg net / gross: `{result['avg_net_bps']:.2f} bps` / `{result['avg_gross_bps']:.2f} bps`",
        f"- Avg MFE / MAE: `{result['avg_mfe_bps']:.2f} bps` / `{result['avg_mae_bps']:.2f} bps`",
        "",
        "Symbol rows below are diagnostics only; do not turn this into a coin-specific rule without multi-window validation.",
        "",
        "## Classifications",
        "",
        "| Classification | Count |",
        "|---|---:|",
    ]
    classification_counts = result.get("classification_counts", {})
    assert isinstance(classification_counts, Mapping)
    for label, count in classification_counts.items():
        lines.append(f"| {label} | {count} |")
    if not classification_counts:
        lines.append("| none | 0 |")

    lines.extend(["", "## Close Reasons", "", "| Reason | Count |", "|---|---:|"])
    close_reasons = result.get("close_reason_counts", {})
    assert isinstance(close_reasons, Mapping)
    for reason, count in close_reasons.items():
        lines.append(f"| {reason} | {count} |")
    if not close_reasons:
        lines.append("| none | 0 |")

    lines.extend(
        [
            "",
            "## Early Windows",
            "",
            "| Window | Samples | Positive Rate | Avg At Window | Avg Early MFE | Avg Early MAE |",
            "|---:|---:|---:|---:|---:|---:|",
        ]
    )
    window_stats = result.get("window_stats", {})
    assert isinstance(window_stats, Mapping)
    for window, stats in window_stats.items():
        assert isinstance(stats, Mapping)
        lines.append(
            f"| {window}m | {stats['samples']} | {stats['positive_rate']:.2%} | "
            f"{stats['avg_gross_at_window_bps']:.2f} | {stats['avg_early_mfe_bps']:.2f} | "
            f"{stats['avg_early_mae_bps']:.2f} |"
        )

    lines.extend(
        [
            "",
            "## Folds",
            "",
            "| Fold | Trades | PnL | Avg Net | Avg MFE | Avg MAE | Losing Time-Stops |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    folds = result.get("folds", [])
    assert isinstance(folds, list)
    for fold in folds:
        assert isinstance(fold, Mapping)
        lines.append(
            f"| {fold['fold_label']} | {fold['trades_seen']} | ${fold['realized_pnl_usd']:.6f} | "
            f"{fold['avg_net_bps']:.2f} | {fold['avg_mfe_bps']:.2f} | "
            f"{fold['avg_mae_bps']:.2f} | {fold['losing_time_stop_trades']} |"
        )
    if not folds:
        lines.append("| none | 0 | $0.000000 | 0.00 | 0.00 | 0.00 | 0 |")

    lines.extend(
        [
            "",
            "## Worst Trades",
            "",
            "| Fold | Symbol | Side | Opened | Close | Net | MFE | MAE | Giveback | Classifications |",
            "|---|---|---|---|---|---:|---:|---:|---:|---|",
        ]
    )
    trades = result.get("trades", [])
    assert isinstance(trades, list)
    for trade in trades[:40]:
        assert isinstance(trade, Mapping)
        labels = trade.get("classifications", [])
        classifications = ", ".join(str(label) for label in labels) if isinstance(labels, list) else ""
        lines.append(
            f"| {trade.get('fold_label', '')} | {trade.get('symbol', '')} | {trade.get('side', '')} | "
            f"{trade.get('opened_at', '')} | {trade.get('close_reason', '')} | "
            f"{_number(trade.get('net_bps')):.2f} | {_number(trade.get('mfe_bps')):.2f} | "
            f"{_number(trade.get('mae_bps')):.2f} | {_number(trade.get('giveback_bps')):.2f} | "
            f"{classifications} |"
        )
    if not trades:
        lines.append("| none | n/a | n/a | n/a | n/a | 0.00 | 0.00 | 0.00 | 0.00 | n/a |")
    lines.append("")
    return "\n".join(lines)


def _iter_jsonl(path: str | Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for file_path in resolve_jsonl_files(path):
        with open_jsonl_text(file_path) as handle:
            for line in handle:
                if not line.strip():
                    continue
                payload = json.loads(line)
                if isinstance(payload, dict):
                    rows.append(payload)
    return rows


def _number(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _timestamp_id(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
