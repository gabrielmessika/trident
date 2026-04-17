from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from app.trident.regime_snapshot_v2 import enrich_cluster_regime_snapshots, enrich_regime_snapshot


class SnapshotFormatError(ValueError):
    """Raised when a snapshot JSONL record does not match the expected schema."""


@dataclass(slots=True)
class SnapshotRecord:
    record_index: int
    source_file: str
    timestamp: str | None
    regime_snapshot: dict[str, object]
    symbols: list[dict[str, object]]
    cluster_regime_snapshots: dict[str, dict[str, object]] | None = None


def merge_snapshot_payloads(payloads: list[dict[str, object]]) -> dict[str, object]:
    if not payloads:
        raise ValueError("merge_snapshot_payloads requires at least one payload")
    if len(payloads) == 1:
        return dict(payloads[0])

    primary = max(payloads, key=_snapshot_payload_priority)
    merged_symbols: dict[str, dict[str, object]] = {}
    symbol_order: list[str] = []
    merged_cluster_snapshots: dict[str, dict[str, object]] = {}

    for payload in payloads:
        for item in payload.get("symbols", []):
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol", "")).strip().upper()
            if not symbol:
                continue
            if symbol not in merged_symbols:
                symbol_order.append(symbol)
            merged_symbols[symbol] = dict(item)
        cluster_snapshots = payload.get("cluster_regime_snapshots")
        if not isinstance(cluster_snapshots, dict):
            continue
        for cluster, snapshot in cluster_snapshots.items():
            if isinstance(snapshot, dict):
                merged_cluster_snapshots[str(cluster)] = dict(snapshot)

    merged = dict(primary)
    merged["symbols"] = [merged_symbols[symbol] for symbol in symbol_order]
    merged["cluster_regime_snapshots"] = merged_cluster_snapshots or None
    return merged


def merge_snapshot_records(records: list[SnapshotRecord]) -> SnapshotRecord:
    if not records:
        raise ValueError("merge_snapshot_records requires at least one record")
    if len(records) == 1:
        return records[0]
    merged_payload = merge_snapshot_payloads(
        [
            {
                "timestamp": record.timestamp,
                "regime_snapshot": record.regime_snapshot,
                "symbols": record.symbols,
                "cluster_regime_snapshots": record.cluster_regime_snapshots,
            }
            for record in records
        ]
    )
    primary = max(records, key=_snapshot_record_priority)
    return SnapshotRecord(
        record_index=primary.record_index,
        source_file=primary.source_file,
        timestamp=merged_payload.get("timestamp") if isinstance(merged_payload.get("timestamp"), str) else primary.timestamp,
        regime_snapshot=(
            merged_payload.get("regime_snapshot")
            if isinstance(merged_payload.get("regime_snapshot"), dict)
            else primary.regime_snapshot
        ),
        symbols=[
            item
            for item in merged_payload.get("symbols", [])
            if isinstance(item, dict)
        ],
        cluster_regime_snapshots=(
            merged_payload.get("cluster_regime_snapshots")
            if isinstance(merged_payload.get("cluster_regime_snapshots"), dict)
            else None
        ),
    )


def _snapshot_payload_priority(payload: dict[str, object]) -> tuple[int, int, int]:
    symbols = payload.get("symbols", [])
    cluster_snapshots = payload.get("cluster_regime_snapshots")
    has_btc_or_eth = any(
        isinstance(item, dict) and str(item.get("symbol", "")).strip().upper() in {"BTC", "ETH"}
        for item in (symbols if isinstance(symbols, list) else [])
    )
    has_crypto_cluster = isinstance(cluster_snapshots, dict) and "crypto" in cluster_snapshots
    symbol_count = len(symbols) if isinstance(symbols, list) else 0
    return (
        1 if has_btc_or_eth else 0,
        1 if has_crypto_cluster else 0,
        symbol_count,
    )


def _snapshot_record_priority(record: SnapshotRecord) -> tuple[int, int, int, int]:
    payload_priority = _snapshot_payload_priority(
        {
            "symbols": record.symbols,
            "cluster_regime_snapshots": record.cluster_regime_snapshots,
        }
    )
    return (*payload_priority, record.record_index)


class SnapshotLoader:
    """Loads JSONL snapshot records from a file or a directory."""

    REQUIRED_REGIME_FIELDS = {
        "ready",
        "adx",
        "atr_ratio",
        "range_width_bps",
        "structure_score",
        "btc_impulse",
    }
    REQUIRED_SYMBOL_FIELDS = {
        "symbol",
        "price",
        "ema_fast",
        "ema_slow",
        "vwap_distance_bps",
        "structure_score",
        "funding_rate",
        "spread_bps",
        "btc_aligned",
    }

    def iter_jsonl(self, input_path: str | Path) -> Iterator[SnapshotRecord]:
        path = Path(input_path)
        files = [path] if path.is_file() else sorted(path.glob("*.jsonl"))
        record_index = 0

        for file_path in files:
            with file_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    payload = json.loads(line)
                    self._validate_payload(payload, file_path=file_path)
                    enriched_payload = self._enrich_payload(payload)
                    record_index += 1
                    cluster_raw = enriched_payload.get("cluster_regime_snapshots")
                    yield SnapshotRecord(
                        record_index=record_index,
                        source_file=file_path.name,
                        timestamp=enriched_payload.get("timestamp"),
                        regime_snapshot=enriched_payload["regime_snapshot"],
                        symbols=enriched_payload.get("symbols", []),
                        cluster_regime_snapshots=(
                            cluster_raw if isinstance(cluster_raw, dict) else None
                        ),
                    )

    def iter_merged_jsonl(self, input_path: str | Path) -> Iterator[SnapshotRecord]:
        pending: list[SnapshotRecord] = []
        pending_key: tuple[str, str | None] | None = None

        for record in self.iter_jsonl(input_path):
            record_key = (record.source_file, record.timestamp)
            if pending_key is None or record_key == pending_key:
                pending.append(record)
                pending_key = record_key
                continue
            yield merge_snapshot_records(pending)
            pending = [record]
            pending_key = record_key

        if pending:
            yield merge_snapshot_records(pending)

    def _validate_payload(self, payload: dict[str, object], file_path: Path) -> None:
        if "regime_snapshot" not in payload:
            raise SnapshotFormatError(
                f"{file_path.name}: missing required field 'regime_snapshot'"
            )
        if "symbols" not in payload:
            raise SnapshotFormatError(f"{file_path.name}: missing required field 'symbols'")

        regime_snapshot = payload["regime_snapshot"]
        if not isinstance(regime_snapshot, dict):
            raise SnapshotFormatError(f"{file_path.name}: 'regime_snapshot' must be an object")
        missing_regime = self.REQUIRED_REGIME_FIELDS - set(regime_snapshot.keys())
        if missing_regime:
            raise SnapshotFormatError(
                f"{file_path.name}: regime_snapshot missing fields {sorted(missing_regime)}"
            )

        symbols = payload["symbols"]
        if not isinstance(symbols, list):
            raise SnapshotFormatError(f"{file_path.name}: 'symbols' must be a list")
        for index, symbol in enumerate(symbols):
            if not isinstance(symbol, dict):
                raise SnapshotFormatError(
                    f"{file_path.name}: symbols[{index}] must be an object"
                )
            missing_symbol = self.REQUIRED_SYMBOL_FIELDS - set(symbol.keys())
            if missing_symbol:
                raise SnapshotFormatError(
                    f"{file_path.name}: symbols[{index}] missing fields {sorted(missing_symbol)}"
                )

    def _enrich_payload(self, payload: dict[str, object]) -> dict[str, object]:
        enriched = dict(payload)
        symbols = payload.get("symbols", [])
        regime_snapshot = payload.get("regime_snapshot")
        if isinstance(regime_snapshot, dict) and isinstance(symbols, list):
            enriched["regime_snapshot"] = enrich_regime_snapshot(regime_snapshot, symbols)
        cluster_snapshots = payload.get("cluster_regime_snapshots")
        if isinstance(cluster_snapshots, dict) and isinstance(symbols, list):
            enriched["cluster_regime_snapshots"] = enrich_cluster_regime_snapshots(
                cluster_snapshots,
                symbols,
            )
        return enriched
