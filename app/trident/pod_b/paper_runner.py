from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.backtest.snapshot_loader import SnapshotLoader
from app.persistence.journal import JsonlJournal
from app.reporting.pod_b import PodBReport
from app.trident.pod_b.models import PassivbotStatus
from app.trident.pod_b.paper_engine import PodBPaperEngine
from app.trident.types import SymbolMarketSnapshot


@dataclass(slots=True)
class PodBPaperRunnerResult:
    input_path: str
    config_path: str
    status_path: str
    records_processed: int
    fills_emitted: int
    total_fill_count: int
    total_position_count: int
    total_open_order_count: int
    realized_pnl_usd: float
    total_notional_usd: float
    total_unrealized_pnl_usd: float
    max_drawdown_usd: float = 0.0
    report: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class PodBPaperRunner:
    """Replays TRIDENT snapshots through a minimal paper market-maker for Pod B."""

    def __init__(self, config_path: str | Path) -> None:
        self.config_path = Path(config_path)
        self.runtime_config: dict[str, object] = {}
        self.managed_symbols: list[str] = []
        self.target_usd: float = 0.0
        self.paper_config = self._build_paper_config({})
        self.engine = PodBPaperEngine(
            managed_symbols=[],
            target_usd=0.0,
            config=self.paper_config,
        )
        self.reload_runtime_config()

    def run(
        self,
        *,
        input_path: str | Path,
        report_output: str | Path | None = None,
        journal_output: str | Path | None = None,
        max_records: int | None = None,
    ) -> PodBPaperRunnerResult:
        loader = SnapshotLoader()
        journal = JsonlJournal(journal_output) if journal_output is not None else None
        report = PodBReport()
        status_path = self.config_path.with_suffix(".status.json")
        meta = self._status_meta(status_path)
        self._write_status(
            self.engine.build_status(
                process_state="running",
                last_sync_reason="paper_runner_started",
                status_meta=meta,
            )
        )

        records_processed = 0
        fills_emitted = 0
        for record in loader.iter_jsonl(input_path):
            snapshots = [
                SymbolMarketSnapshot(**item)
                for item in record.symbols
                if isinstance(item, dict) and str(item.get("symbol", "")).upper() in self.managed_symbols
            ]
            if not snapshots:
                continue
            status, fills = self.engine.process_record(
                timestamp=record.timestamp,
                snapshots=snapshots,
                status_meta=meta,
                regime_snapshot=record.regime_snapshot,
            )
            records_processed += 1
            fills_emitted += len(fills)
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
                            "event_type": "pod_b_fill",
                            "timestamp": fill.timestamp,
                            "source": "pod_b_paper_runner",
                            "fill": fill.as_dict(),
                            "symbol_snapshot": (
                                asdict(snapshot_by_symbol[fill.symbol])
                                if fill.symbol in snapshot_by_symbol
                                else None
                            ),
                        }
                    )
            if max_records is not None and records_processed >= max_records:
                break

        final_status = self.engine.build_status(
            process_state="stopped",
            last_sync_reason="paper_runner_completed",
            status_meta=meta,
        )
        self._write_status(final_status)
        result = PodBPaperRunnerResult(
            input_path=str(input_path),
            config_path=str(self.config_path),
            status_path=str(status_path),
            records_processed=records_processed,
            fills_emitted=fills_emitted,
            total_fill_count=final_status.total_fill_count,
            total_position_count=final_status.total_position_count,
            total_open_order_count=final_status.total_open_order_count,
            realized_pnl_usd=final_status.realized_pnl_usd,
            total_notional_usd=final_status.total_notional_usd,
            total_unrealized_pnl_usd=final_status.total_unrealized_pnl_usd,
            max_drawdown_usd=report.max_drawdown_usd,
            report=report.to_dict(),
        )
        if report_output is not None:
            report_path = Path(report_output)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(result.to_dict(), indent=2) + "\n",
                encoding="utf-8",
            )
        return result

    def _build_paper_config(self, trident: dict[str, object]):
        from app.settings import PodBConfig

        return PodBConfig(
            enabled=True,
            symbols=self.managed_symbols,
            passivbot_config_path=str(self.config_path),
            launch_command=[],
            launch_workdir="",
            max_allocation_pct=1.0,
            paper_quote_width_bps=float(trident.get("paper_quote_width_bps", 6.0)),
            paper_order_size_pct=float(trident.get("paper_order_size_pct", 0.25)),
            paper_max_inventory_skew_pct=float(
                trident.get("paper_max_inventory_skew_pct", 1.0)
            ),
            paper_maker_fee_bps=float(trident.get("paper_maker_fee_bps", 0.0)),
            paper_recent_fills_limit=int(trident.get("paper_recent_fills_limit", 20)),
            paper_pause_outside_range=bool(trident.get("paper_pause_outside_range", True)),
            paper_guard_max_adx=float(trident.get("paper_guard_max_adx", 20.0)),
            paper_guard_max_atr_ratio=float(
                trident.get("paper_guard_max_atr_ratio", 0.9)
            ),
            paper_guard_max_abs_structure_score=float(
                trident.get("paper_guard_max_abs_structure_score", 0.2)
            ),
            paper_guard_max_range_width_bps=float(
                trident.get("paper_guard_max_range_width_bps", 90.0)
            ),
            paper_flow_toxicity_threshold=float(
                trident.get("paper_flow_toxicity_threshold", 0.2)
            ),
            paper_one_sided_inventory_threshold_pct=float(
                trident.get("paper_one_sided_inventory_threshold_pct", 0.6)
            ),
            paper_quote_width_bucket_multiplier=float(
                trident.get("paper_quote_width_bucket_multiplier", 0.35)
            ),
            paper_quote_width_toxicity_multiplier=float(
                trident.get("paper_quote_width_toxicity_multiplier", 1.5)
            ),
            paper_order_size_toxicity_discount=float(
                trident.get("paper_order_size_toxicity_discount", 0.5)
            ),
        )

    def reload_runtime_config(self) -> None:
        self.runtime_config = json.loads(self.config_path.read_text(encoding="utf-8"))
        trident = self.runtime_config.get("trident", {})
        if not isinstance(trident, dict):
            trident = {}
        self.managed_symbols = [
            str(symbol).upper() for symbol in trident.get("managed_symbols", [])
        ]
        self.target_usd = float(trident.get("target_usd", 0.0))
        self.paper_config = self._build_paper_config(trident)
        self.engine.config = self.paper_config
        self.engine.update_allocation(
            managed_symbols=self.managed_symbols,
            target_usd=self.target_usd,
        )

    def _status_meta(self, status_path: Path) -> dict[str, object]:
        payload: dict[str, object] = {}
        if status_path.exists():
            payload = json.loads(status_path.read_text(encoding="utf-8"))
        return {
            "config_path": str(self.config_path),
            "status_path": str(status_path),
            "leverage": self.runtime_config.get("live", {}).get("leverage"),
            "pid": (
                payload.get("pid")
                if payload.get("pid") not in (None, "")
                else os.getpid()
            ),
            "launch_command": payload.get("launch_command") or sys.argv,
            "stdout_path": payload.get("stdout_path", ""),
            "stderr_path": payload.get("stderr_path", ""),
            "started_at": payload.get("started_at") or self._utc_now(),
        }

    def _write_status(self, status: PassivbotStatus) -> None:
        status_path = Path(status.status_path)
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(
            json.dumps(status.as_dict(), indent=2) + "\n",
            encoding="utf-8",
        )

    def _utc_now(self) -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay snapshots through the Pod B paper runner")
    parser.add_argument("--config-path", required=True, help="Path to Pod B runtime config JSON")
    parser.add_argument("--input", required=True, help="Snapshot JSONL file or directory")
    parser.add_argument("--report-output", help="Optional JSON summary output")
    parser.add_argument("--journal-output", help="Optional JSONL fill journal output")
    parser.add_argument("--max-records", type=int, help="Optional cap for local smoke runs")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    runner = PodBPaperRunner(args.config_path)
    result = runner.run(
        input_path=args.input,
        report_output=args.report_output,
        journal_output=args.journal_output,
        max_records=args.max_records,
    )
    print(f"input_path={result.input_path}")
    print(f"config_path={result.config_path}")
    print(f"status_path={result.status_path}")
    print(f"records_processed={result.records_processed}")
    print(f"fills_emitted={result.fills_emitted}")
    print(f"total_fill_count={result.total_fill_count}")
    print(f"total_position_count={result.total_position_count}")
    print(f"total_open_order_count={result.total_open_order_count}")
    print(f"realized_pnl_usd={result.realized_pnl_usd}")
    print(f"total_notional_usd={result.total_notional_usd}")
    print(f"total_unrealized_pnl_usd={result.total_unrealized_pnl_usd}")


if __name__ == "__main__":
    main()
