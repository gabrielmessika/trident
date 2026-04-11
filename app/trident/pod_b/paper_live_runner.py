"""Long-lived Pod B live runner that mirrors the full-bot backtest behavior.

Key principles (matching full_bot_replay._process_pod_b):
1. Uses a TridentSupervisor for routing — Pod B only sees coins assigned to it
2. Allocation (target_usd) comes from the supervisor capital plan, not runtime config
3. Only processes NEW snapshots — skips historical files already on disk at startup
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.backtest.snapshot_loader import merge_snapshot_payloads
from app.persistence.journal import JsonlJournal
from app.reporting.pod_b import PodBReport
from app.settings import AppConfig, load_config
from app.trident.pod_b.models import PassivbotStatus
from app.trident.pod_b.paper_engine import PodBPaperEngine
from app.trident.supervisor import TridentSupervisor
from app.trident.types import PodName, RegimeSnapshot, SymbolMarketSnapshot

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PodBPaperLiveStats:
    records_processed: int = 0
    fills_emitted: int = 0
    idle_loops: int = 0
    skipped_historical: int = 0
    report_path: str | None = None


class PodBPaperLiveRunner:
    """Long-lived Pod B paper wrapper that tails snapshot files, using supervisor routing."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.supervisor = TridentSupervisor(
            config=config,
            profile="trident-live-pod-b",
            mode="dry-run",
        )
        self.engine = PodBPaperEngine(
            managed_symbols=[],
            target_usd=0.0,
            config=config.pod_b,
        )

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
        journal = JsonlJournal(journal_output, truncate=True) if journal_output is not None else None
        report = PodBReport()
        # Derive status_path the same way as PassivbotManager: config.with_suffix(".status.json")
        config_path = Path(self.config.pod_b.passivbot_config_path)
        status_path = config_path.with_suffix(".status.json")
        status_path.parent.mkdir(parents=True, exist_ok=True)
        meta = self._status_meta(status_path)

        logger.info(
            "Pod B live runner starting; input=%s poll_seconds=%.2f",
            input_path, poll_seconds,
        )
        self._write_status(
            self.engine.build_status(
                process_state="running",
                last_sync_reason="paper_live_runner_started",
                status_meta=meta,
            ),
            status_path,
        )

        stats = PodBPaperLiveStats()
        started = time.monotonic()
        merge_wait_seconds = max(poll_seconds * 2.0, 0.25)

        # Track byte offsets per file — skip to end of existing files at startup
        file_byte_offsets: dict[str, int] = {}
        pending_payload_groups: dict[str, list[dict[str, object]]] = {}
        pending_payload_first_seen: dict[str, float] = {}
        files = [input_path] if input_path.is_file() else sorted(input_path.glob("*.jsonl"))
        for file_path in files:
            file_key = str(file_path.resolve())
            size = file_path.stat().st_size
            file_byte_offsets[file_key] = size
            stats.skipped_historical += self._count_lines(file_path)
        if stats.skipped_historical > 0:
            logger.info(
                "Pod B skipping %d historical records from %d files (seek to EOF)",
                stats.skipped_historical, len(file_byte_offsets),
            )

        try:
            while True:
                new_records_processed = 0
                files = [input_path] if input_path.is_file() else sorted(input_path.glob("*.jsonl"))
                for file_path in files:
                    file_key = str(file_path.resolve())
                    byte_offset = file_byte_offsets.get(file_key, 0)

                    # Quick check: skip file if no new bytes
                    try:
                        file_size = file_path.stat().st_size
                    except OSError:
                        continue
                    if file_size <= byte_offset:
                        continue

                    # Read only new bytes from the file
                    with file_path.open("r", encoding="utf-8") as fh:
                        fh.seek(byte_offset)
                        new_data = fh.read()
                        new_byte_offset = fh.tell()

                    file_byte_offsets[file_key] = new_byte_offset

                    for line in new_data.splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            payload = json.loads(line)
                        except json.JSONDecodeError:
                            logger.warning("Pod B skipping malformed line in %s", file_path.name)
                            continue
                        payload_timestamp = str(payload.get("timestamp") or "")
                        pending_group = pending_payload_groups.get(file_key, [])
                        if not pending_group:
                            pending_payload_groups[file_key] = [payload]
                            pending_payload_first_seen[file_key] = time.monotonic()
                            continue
                        pending_timestamp = str(pending_group[0].get("timestamp") or "")
                        if payload_timestamp == pending_timestamp:
                            pending_group.append(payload)
                            continue
                        processed, fill_count = self._process_payload(
                            merge_snapshot_payloads(pending_group),
                            meta=meta,
                            report=report,
                            journal=journal,
                            status_path=status_path,
                        )
                        if processed:
                            new_records_processed += 1
                            stats.records_processed += 1
                            stats.fills_emitted += fill_count
                        pending_payload_groups[file_key] = [payload]
                        pending_payload_first_seen[file_key] = time.monotonic()

                now = time.monotonic()
                flush_before = now - merge_wait_seconds
                for file_key, first_seen in list(pending_payload_first_seen.items()):
                    if first_seen > flush_before:
                        continue
                    group = pending_payload_groups.pop(file_key, [])
                    pending_payload_first_seen.pop(file_key, None)
                    if not group:
                        continue
                    processed, fill_count = self._process_payload(
                        merge_snapshot_payloads(group),
                        meta=meta,
                        report=report,
                        journal=journal,
                        status_path=status_path,
                    )
                    if processed:
                        new_records_processed += 1
                        stats.records_processed += 1
                        stats.fills_emitted += fill_count

                if new_records_processed == 0:
                    stats.idle_loops += 1
                    if stats.idle_loops == 1 or (
                        stats.idle_loops % 30 == 0 and max_idle_loops != 1
                    ):
                        logger.info(
                            "Pod B live runner idle; idle_loops=%s owned=%s files=%s",
                            stats.idle_loops,
                            self.engine.managed_symbols,
                            len(files),
                        )
                else:
                    stats.idle_loops = 0
                    logger.info(
                        "Pod B tick; records=%s cumulative=%s fills=%s owned=%s target_usd=%.2f pnl=%.4f",
                        new_records_processed,
                        stats.records_processed,
                        stats.fills_emitted,
                        self.engine.managed_symbols,
                        self.engine.target_usd,
                        self.engine.realized_pnl_usd,
                    )

                if max_idle_loops is not None and stats.idle_loops >= max_idle_loops:
                    break
                if max_runtime_seconds is not None and time.monotonic() - started >= max_runtime_seconds:
                    break
                time.sleep(poll_seconds)
        finally:
            for group in pending_payload_groups.values():
                processed, fill_count = self._process_payload(
                    merge_snapshot_payloads(group),
                    meta=meta,
                    report=report,
                    journal=journal,
                    status_path=status_path,
                )
                if processed:
                    stats.records_processed += 1
                    stats.fills_emitted += fill_count
            final_status = self.engine.build_status(
                process_state="stopped",
                last_sync_reason="paper_live_runner_completed",
                status_meta=meta,
            )
            self._write_status(final_status, status_path)
            if report_output is not None:
                report_path = Path(report_output)
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(
                    json.dumps(report.to_dict(), indent=2) + "\n",
                    encoding="utf-8",
                )
                stats.report_path = str(report_path)
            logger.info(
                "Pod B live runner completed; records=%s fills=%s skipped_historical=%s pnl=%.4f",
                stats.records_processed,
                stats.fills_emitted,
                stats.skipped_historical,
                final_status.realized_pnl_usd,
            )

        return stats

    def _process_payload(
        self,
        payload: dict[str, object],
        *,
        meta: dict[str, object],
        report: PodBReport,
        journal: JsonlJournal | None,
        status_path: Path,
    ) -> tuple[bool, int]:
        all_snapshots = [
            SymbolMarketSnapshot(**item)
            for item in payload.get("symbols", [])
            if isinstance(item, dict)
        ]
        regime_snapshot = payload.get("regime_snapshot", {})
        if not isinstance(regime_snapshot, dict):
            return False, 0
        cluster_regime_snapshots_raw = payload.get("cluster_regime_snapshots", {})
        cluster_regime_snapshots = {
            cluster: RegimeSnapshot(**snap)
            for cluster, snap in (cluster_regime_snapshots_raw or {}).items()
            if isinstance(snap, dict)
        }

        self.supervisor.apply_regime_snapshot(
            RegimeSnapshot(**regime_snapshot),
            cluster_regime_snapshots=cluster_regime_snapshots,
        )
        self.supervisor.refresh_symbol_routing(all_snapshots)

        pod_b_allocation = self.supervisor.capital_plan.pod_allocations[PodName.POD_B]
        pod_b_owned = self.supervisor.registry.symbols_for(PodName.POD_B)
        self.engine.update_allocation(
            managed_symbols=pod_b_owned,
            target_usd=pod_b_allocation.target_usd,
        )

        pod_b_snapshots = [snapshot for snapshot in all_snapshots if snapshot.symbol in pod_b_owned]
        if not pod_b_snapshots:
            return False, 0

        status, fills = self.engine.process_record(
            timestamp=payload.get("timestamp"),
            snapshots=pod_b_snapshots,
            status_meta=meta,
            regime_snapshot=regime_snapshot,
            last_sync_reason="paper_live_runner_tick",
        )
        self._write_status(status, status_path)
        report.add_tick(
            timestamp=payload.get("timestamp"),
            status=status,
            fills=fills,
        )
        if journal is not None:
            snapshot_by_symbol = {snapshot.symbol: snapshot for snapshot in pod_b_snapshots}
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
        return True, len(fills)

    def _count_lines(self, file_path: Path) -> int:
        """Count non-empty lines in a JSONL file (for stats only, no parsing)."""
        count = 0
        with file_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    count += 1
        return count

    def _status_meta(self, status_path: Path) -> dict[str, object]:
        import os, sys
        payload: dict[str, object] = {}
        if status_path.exists():
            try:
                payload = json.loads(status_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {
            "config_path": str(self.config.pod_b.passivbot_config_path),
            "status_path": str(status_path),
            "leverage": None,
            "pid": payload.get("pid") if payload.get("pid") not in (None, "") else os.getpid(),
            "launch_command": payload.get("launch_command") or sys.argv,
            "stdout_path": payload.get("stdout_path", ""),
            "stderr_path": payload.get("stderr_path", ""),
            "started_at": payload.get("started_at") or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    def _write_status(self, status: PassivbotStatus, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(status.as_dict(), indent=2) + "\n",
            encoding="utf-8",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Pod B paper wrapper with supervisor routing")
    parser.add_argument("--config", default="config/trident.toml", help="Path to trident.toml")
    parser.add_argument("--input", required=True, help="Snapshot JSONL file or directory")
    parser.add_argument("--journal-output", help="Optional JSONL fill journal output")
    parser.add_argument("--report-output", help="Optional JSON summary output")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--max-runtime-seconds", type=float)
    parser.add_argument("--max-idle-loops", type=int)
    return parser


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    args = build_parser().parse_args()
    config = load_config(args.config)
    runner = PodBPaperLiveRunner(config)
    stats = runner.run_live(
        input_path=args.input,
        poll_seconds=args.poll_seconds,
        max_runtime_seconds=args.max_runtime_seconds,
        journal_output=args.journal_output,
        report_output=args.report_output,
        max_idle_loops=args.max_idle_loops,
    )
    print(f"records_processed={stats.records_processed}")
    print(f"fills_emitted={stats.fills_emitted}")
    print(f"skipped_historical={stats.skipped_historical}")
    print(f"idle_loops={stats.idle_loops}")
    if stats.report_path:
        print(f"report_path={stats.report_path}")


if __name__ == "__main__":
    main()
