from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


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
                    record_index += 1
                    cluster_raw = payload.get("cluster_regime_snapshots")
                    yield SnapshotRecord(
                        record_index=record_index,
                        source_file=file_path.name,
                        timestamp=payload.get("timestamp"),
                        regime_snapshot=payload["regime_snapshot"],
                        symbols=payload.get("symbols", []),
                        cluster_regime_snapshots=(
                            cluster_raw if isinstance(cluster_raw, dict) else None
                        ),
                    )

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
