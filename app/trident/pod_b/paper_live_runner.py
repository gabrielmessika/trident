from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from app.backtest.snapshot_loader import SnapshotLoader
from app.persistence.journal import JsonlJournal
from app.reporting.pod_b import PodBReport
from app.trident.pod_b.paper_runner import PodBPaperRunner
from app.trident.types import SymbolMarketSnapshot


@dataclass(slots=True)
class PodBPaperLiveStats:
    records_processed: int = 0
    fills_emitted: int = 0
    idle_loops: int = 0
    report_path: str | None = None


class PodBPaperLiveRunner(PodBPaperRunner):
    """Long-lived Pod B paper wrapper that tails snapshot files or directories."""

    def run_live(
        self,
        *,
        input_path: str | Path,
        poll_seconds: float = 1.0,
        max_runtime_seconds: float | None = None,
        journal_output: str | Path | None = None,
        report_output: str | Path | None = None,
        max_idle_loops: int | None = None,
    ) -> PodBPaperLiveStats:
        input_path = Path(input_path)
        journal = JsonlJournal(journal_output) if journal_output is not None else None
        report = PodBReport()
        status_path = self.config_path.with_suffix(".status.json")
        meta = self._status_meta(status_path)
        self._write_status(
            self.engine.build_status(
                process_state="running",
                last_sync_reason="paper_live_runner_started",
                status_meta=meta,
            )
        )

        stats = PodBPaperLiveStats()
        started = time.monotonic()
        processed_offsets: dict[str, int] = {}
        loader = SnapshotLoader()

        try:
            while True:
                self.reload_runtime_config()
                new_records_processed = 0
                files = [input_path] if input_path.is_file() else sorted(input_path.glob("*.jsonl"))
                for file_path in files:
                    file_key = str(file_path.resolve())
                    offset = processed_offsets.get(file_key, 0)
                    for index, record in enumerate(loader.iter_jsonl(file_path), start=1):
                        if index <= offset:
                            continue
                        snapshots = [
                            SymbolMarketSnapshot(**item)
                            for item in record.symbols
                            if isinstance(item, dict)
                            and str(item.get("symbol", "")).upper() in self.managed_symbols
                        ]
                        processed_offsets[file_key] = index
                        if not snapshots:
                            continue
                        status, fills = self.engine.process_record(
                            timestamp=record.timestamp,
                            snapshots=snapshots,
                            status_meta=meta,
                            regime_snapshot=record.regime_snapshot,
                            last_sync_reason="paper_live_runner_tick",
                        )
                        new_records_processed += 1
                        stats.records_processed += 1
                        stats.fills_emitted += len(fills)
                        self._write_status(status)
                        report.add_tick(
                            timestamp=record.timestamp,
                            status=status,
                            fills=fills,
                        )
                        if journal is not None:
                            snapshot_by_symbol = {snapshot.symbol: snapshot for snapshot in snapshots}
                            for fill in fills:
                                journal.append(
                                    {
                                        "event_type": "pod_b_live_fill",
                                        "timestamp": fill.timestamp,
                                        "source": "pod_b_paper_live_runner",
                                        "fill": fill.as_dict(),
                                        "symbol_snapshot": (
                                            asdict(snapshot_by_symbol[fill.symbol])
                                            if fill.symbol in snapshot_by_symbol
                                            else None
                                        ),
                                    }
                                )

                if new_records_processed == 0:
                    stats.idle_loops += 1
                else:
                    stats.idle_loops = 0

                if max_idle_loops is not None and stats.idle_loops >= max_idle_loops:
                    break
                if max_runtime_seconds is not None and time.monotonic() - started >= max_runtime_seconds:
                    break
                time.sleep(poll_seconds)
        finally:
            final_status = self.engine.build_status(
                process_state="stopped",
                last_sync_reason="paper_live_runner_completed",
                status_meta=meta,
            )
            self._write_status(final_status)
            if report_output is not None:
                report_path = Path(report_output)
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(
                    json.dumps(report.to_dict(), indent=2) + "\n",
                    encoding="utf-8",
                )
                stats.report_path = str(report_path)

        return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Pod B paper wrapper in long-lived tail mode")
    parser.add_argument("--config-path", required=True, help="Path to Pod B runtime config JSON")
    parser.add_argument("--input", required=True, help="Snapshot JSONL file or directory")
    parser.add_argument("--journal-output", help="Optional JSONL fill journal output")
    parser.add_argument("--report-output", help="Optional JSON summary output")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--max-runtime-seconds", type=float)
    parser.add_argument("--max-idle-loops", type=int)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    runner = PodBPaperLiveRunner(args.config_path)
    stats = runner.run_live(
        input_path=args.input,
        poll_seconds=args.poll_seconds,
        max_runtime_seconds=args.max_runtime_seconds,
        journal_output=args.journal_output,
        report_output=args.report_output,
        max_idle_loops=args.max_idle_loops,
    )
    print(f"config_path={runner.config_path}")
    print(f"records_processed={stats.records_processed}")
    print(f"fills_emitted={stats.fills_emitted}")
    print(f"idle_loops={stats.idle_loops}")
    if stats.report_path:
        print(f"report_path={stats.report_path}")


if __name__ == "__main__":
    main()
