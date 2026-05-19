from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable


class JsonlJournal:
    """Small JSONL writer for signals and backtest outputs."""

    def __init__(self, path: str | Path, *, truncate: bool = False) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if truncate and self.path.exists():
            self.path.write_text("", encoding="utf-8")

    def append(self, record: dict[str, object]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=_json_default) + "\n")

    def append_many(self, records: Iterable[dict[str, object]]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, default=_json_default) + "\n")


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        if value.is_finite():
            return int(value) if value == value.to_integral_value() else float(value)
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def build_signal_journal_record(
    *,
    record_index: int,
    regime: str,
    signal: dict[str, object],
    symbol_snapshot: dict[str, object] | None = None,
    regime_snapshot: dict[str, object] | None = None,
    source: str = "pod_a_backtest",
    timestamp: str | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "event_type": "signal",
        "source": source,
        "record_index": record_index,
        "regime": regime,
        "signal": signal,
    }
    if timestamp is not None:
        record["timestamp"] = timestamp
    if symbol_snapshot is not None:
        record["symbol_snapshot"] = symbol_snapshot
    if regime_snapshot is not None:
        record["regime_snapshot"] = regime_snapshot
    return record


def build_trade_journal_record(
    *,
    record_index: int,
    trade: dict[str, object],
    source: str = "pod_a_backtest_trade",
    timestamp: str | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "event_type": "trade_close",
        "source": source,
        "record_index": record_index,
        "trade": trade,
    }
    if timestamp is not None:
        record["timestamp"] = timestamp
    return record


def build_signal_review_journal_record(
    *,
    record_index: int,
    regime: str,
    review: dict[str, object],
    symbol_snapshot: dict[str, object] | None = None,
    regime_snapshot: dict[str, object] | None = None,
    source: str = "pod_a_signal_review",
    timestamp: str | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "event_type": "signal_review",
        "source": source,
        "record_index": record_index,
        "regime": regime,
        "review": review,
    }
    if timestamp is not None:
        record["timestamp"] = timestamp
    if symbol_snapshot is not None:
        record["symbol_snapshot"] = symbol_snapshot
    if regime_snapshot is not None:
        record["regime_snapshot"] = regime_snapshot
    return record
