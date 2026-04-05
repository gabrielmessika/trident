from __future__ import annotations

import json
from pathlib import Path


class LiveSnapshotWriter:
    """Appends live snapshots to daily JSONL files."""

    def __init__(self, output_dir: str | Path) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def append_many(self, records: list[dict[str, object]]) -> list[Path]:
        written_paths: list[Path] = []
        for record in records:
            timestamp = str(record.get("timestamp", "unknown"))
            date_key = timestamp[:10] if len(timestamp) >= 10 else "unknown"
            path = self.output_dir / f"{date_key}.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
            written_paths.append(path)
        return written_paths
