from __future__ import annotations

import contextlib
import copy
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.backtest.pod_report import PodABacktestReport


TRADE_OUTCOME_REPORT_KEYS = (
    "closed_trade_count",
    "win_count",
    "loss_count",
    "win_rate",
    "realized_pnl_usd",
    "gross_pnl_usd",
    "fees_usd",
    "max_drawdown_usd",
    "average_hold_hours",
    "close_reasons",
    "trades_by_symbol",
    "trades_by_cluster",
    "trades_by_regime",
    "trades_by_setup",
    "pnl_by_symbol",
    "pnl_by_cluster",
    "pnl_by_regime",
    "pnl_by_setup",
    "pnl_by_date",
    "closed_trade_log",
)


@dataclass(slots=True)
class _LiveJournalReportCache:
    report: PodABacktestReport = field(default_factory=PodABacktestReport)
    seen: set[tuple[str, ...]] = field(default_factory=set)
    offset: int = 0
    line_count: int = 0
    device: int | None = None
    inode: int | None = None
    mtime_ns: int = 0


_LIVE_JOURNAL_REPORT_CACHE: dict[tuple[str, bool], _LiveJournalReportCache] = {}


def attach_live_journal_report(
    runtime_payload: dict[str, object] | None,
    journal_path: str | Path,
    *,
    enabled: bool,
    market_cluster_for_symbol: Callable[[str], str | None] | None = None,
) -> dict[str, object] | None:
    if not isinstance(runtime_payload, dict):
        return runtime_payload
    if not enabled and str(runtime_payload.get("mode", "")).strip().lower() != "live":
        return runtime_payload

    journal_report = load_live_journal_report(
        journal_path,
        market_cluster_for_symbol=market_cluster_for_symbol,
    )
    if int(journal_report.get("closed_trade_count", 0) or 0) <= 0:
        return runtime_payload

    payload = dict(runtime_payload)
    runtime_report = payload.get("report", {})
    if not isinstance(runtime_report, dict):
        runtime_report = {}
    payload["report"] = merge_report_with_live_journal(
        runtime_report,
        journal_report,
        journal_path=journal_path,
    )
    return payload


def load_live_journal_report(
    journal_path: str | Path,
    *,
    market_cluster_for_symbol: Callable[[str], str | None] | None = None,
) -> dict[str, object]:
    path = Path(journal_path)
    if not path.exists():
        return PodABacktestReport().to_dict()

    stat = path.stat()
    cache_key = _live_journal_cache_key(path, market_cluster_for_symbol)
    cache = _LIVE_JOURNAL_REPORT_CACHE.get(cache_key)
    if cache is None or _live_journal_cache_invalid(cache, stat):
        cache = _LiveJournalReportCache(
            device=getattr(stat, "st_dev", None),
            inode=getattr(stat, "st_ino", None),
        )
        _LIVE_JOURNAL_REPORT_CACHE[cache_key] = cache

    with path.open("rb") as handle:
        handle.seek(cache.offset)
        for raw_line in handle:
            cache.line_count += 1
            _consume_live_journal_line(
                cache.report,
                cache.seen,
                raw_line.decode("utf-8", errors="replace"),
                line_number=cache.line_count,
                market_cluster_for_symbol=market_cluster_for_symbol,
            )
        cache.offset = handle.tell()
    cache.mtime_ns = int(getattr(stat, "st_mtime_ns", 0) or 0)
    return copy.deepcopy(cache.report.to_dict())


def _live_journal_cache_key(
    path: Path,
    market_cluster_for_symbol: Callable[[str], str | None] | None,
) -> tuple[str, bool]:
    return (str(path.resolve(strict=False)), market_cluster_for_symbol is not None)


def _live_journal_cache_invalid(
    cache: _LiveJournalReportCache,
    stat: os.stat_result,
) -> bool:
    if int(getattr(stat, "st_size", 0) or 0) < cache.offset:
        return True
    device = getattr(stat, "st_dev", None)
    inode = getattr(stat, "st_ino", None)
    if cache.device is not None and device is not None and cache.device != device:
        return True
    if cache.inode is not None and inode is not None and cache.inode != inode:
        return True
    mtime_ns = int(getattr(stat, "st_mtime_ns", 0) or 0)
    size = int(getattr(stat, "st_size", 0) or 0)
    if size == cache.offset and cache.mtime_ns and mtime_ns != cache.mtime_ns:
        return True
    return False


def _consume_live_journal_line(
    report: PodABacktestReport,
    seen: set[tuple[str, ...]],
    line: str,
    *,
    line_number: int,
    market_cluster_for_symbol: Callable[[str], str | None] | None,
) -> None:
    raw_line = line.strip()
    if not raw_line:
        return
    with contextlib.suppress(json.JSONDecodeError):
        record = json.loads(raw_line)
        if not isinstance(record, dict) or record.get("event_type") != "trade_close":
            return
        trade = record.get("trade")
        if not isinstance(trade, dict):
            return
        identity = _trade_identity(trade, record, line_number)
        if identity in seen:
            return
        seen.add(identity)
        _add_trade_record(
            report,
            trade,
            record,
            market_cluster_for_symbol=market_cluster_for_symbol,
        )


def merge_report_with_live_journal(
    runtime_report: dict[str, object],
    journal_report: dict[str, object],
    *,
    journal_path: str | Path,
) -> dict[str, object]:
    if int(journal_report.get("closed_trade_count", 0) or 0) <= 0:
        return dict(runtime_report)

    merged = dict(runtime_report)
    for key in TRADE_OUTCOME_REPORT_KEYS:
        if key in journal_report:
            merged[key] = journal_report[key]
    merged["closed_trade_history_source"] = "live_journal"
    merged["closed_trade_history_path"] = str(journal_path)
    return merged


def _add_trade_record(
    report: PodABacktestReport,
    trade: dict[str, object],
    record: dict[str, object],
    *,
    market_cluster_for_symbol: Callable[[str], str | None] | None,
) -> None:
    symbol = str(trade.get("symbol") or "").strip()
    if not symbol:
        return
    pnl_usd = _float_or_none(trade.get("pnl_usd"))
    if pnl_usd is None:
        return

    setup_details = _primitive_details(trade.get("setup_details"))
    market_cluster = _str_or_none(
        trade.get("market_cluster")
        or setup_details.get("market_cluster")
    )
    if market_cluster is None and market_cluster_for_symbol is not None:
        with contextlib.suppress(Exception):
            market_cluster = market_cluster_for_symbol(symbol)

    opened_at = _str_or_none(trade.get("opened_at"))
    closed_at = _str_or_none(trade.get("closed_at"))
    hold_hours = _float_or_none(trade.get("hold_hours"))
    if hold_hours is None:
        hold_hours = _hold_hours(opened_at, closed_at)
    gross_pnl_usd = _float_or_none(trade.get("gross_pnl_usd"))
    fees_usd = _float_or_none(trade.get("fees_usd"))

    report.add_closed_trade(
        date_key=_date_key(trade, record),
        symbol=symbol,
        side=str(trade.get("side") or "-"),
        setup=_str_or_none(trade.get("setup") or trade.get("open_reason")),
        confidence=_float_or_none(trade.get("confidence")),
        market_cluster=market_cluster,
        close_regime=_str_or_none(trade.get("close_regime") or record.get("regime")),
        entry_price=_float_or_none(trade.get("entry_price")),
        exit_price=_float_or_none(trade.get("exit_price")),
        target_notional_usd=_float_or_none(trade.get("target_notional_usd")),
        margin_usd=_float_or_none(trade.get("margin_usd")),
        effective_leverage=_float_or_none(
            trade.get("effective_leverage") or trade.get("leverage")
        ),
        risk_budget_usd=_float_or_none(trade.get("risk_budget_usd")),
        expected_loss_usd=_float_or_none(trade.get("expected_loss_usd")),
        invalidation_price=_float_or_none(trade.get("invalidation_price")),
        stop_bps=_float_or_none(trade.get("stop_bps")),
        time_stop_hours=_int_or_none(trade.get("time_stop_hours")),
        take_profit_bps=_float_or_none(trade.get("take_profit_bps")),
        break_even_trigger_bps=_float_or_none(trade.get("break_even_trigger_bps")),
        trailing_activation_bps=_float_or_none(trade.get("trailing_activation_bps")),
        trailing_distance_bps=_float_or_none(trade.get("trailing_distance_bps")),
        pnl_usd=pnl_usd,
        gross_pnl_usd=gross_pnl_usd if gross_pnl_usd is not None else pnl_usd,
        fees_usd=fees_usd if fees_usd is not None else 0.0,
        close_reason=str(trade.get("close_reason") or "unknown"),
        hold_hours=hold_hours,
        opened_at=opened_at,
        closed_at=closed_at,
        best_price_seen=_float_or_none(trade.get("best_price_seen")),
        worst_price_seen=_float_or_none(trade.get("worst_price_seen")),
        mfe_bps=_float_or_none(trade.get("mfe_bps")),
        mae_bps=_float_or_none(trade.get("mae_bps")),
        setup_details=setup_details,
    )


def _trade_identity(
    trade: dict[str, object],
    record: dict[str, object],
    line_number: int,
) -> tuple[str, ...]:
    symbol = str(trade.get("symbol") or "")
    opened_at = str(trade.get("opened_at") or "")
    closed_at = str(trade.get("closed_at") or "")
    if symbol and (opened_at or closed_at):
        return (
            symbol,
            str(trade.get("side") or ""),
            opened_at,
            closed_at,
            str(trade.get("close_reason") or ""),
            str(trade.get("pnl_usd") or ""),
            str(trade.get("entry_price") or ""),
            str(trade.get("exit_price") or ""),
        )
    return (
        str(record.get("source") or ""),
        str(record.get("record_index") or ""),
        str(record.get("timestamp") or ""),
        str(line_number),
    )


def _date_key(trade: dict[str, object], record: dict[str, object]) -> str:
    for value in (
        trade.get("date"),
        trade.get("closed_at"),
        record.get("timestamp"),
        trade.get("opened_at"),
    ):
        if isinstance(value, str) and len(value) >= 10:
            return value[:10]
    return "unknown"


def _hold_hours(opened_at: str | None, closed_at: str | None) -> float | None:
    opened = _parse_timestamp(opened_at)
    closed = _parse_timestamp(closed_at)
    if opened is None or closed is None:
        return None
    return round((closed - opened).total_seconds() / 3600.0, 4)


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    with contextlib.suppress(ValueError):
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _primitive_details(value: object) -> dict[str, float | str | bool]:
    if not isinstance(value, dict):
        return {}
    details: dict[str, float | str | bool] = {}
    for key, item in value.items():
        if isinstance(item, bool):
            details[str(key)] = item
        elif isinstance(item, (int, float)):
            details[str(key)] = float(item)
        elif isinstance(item, str):
            details[str(key)] = item
    return details


def _float_or_none(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _int_or_none(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _str_or_none(value: object) -> str | None:
    if value in (None, ""):
        return None
    return str(value)
