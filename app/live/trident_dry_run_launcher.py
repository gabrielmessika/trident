from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.live.pod_a_live_runner import PodALiveRunner
from app.live.pod_c_live_runner import PodCLiveRunner
from app.live.runtime_status import write_runtime_status
from app.settings import AppConfig, load_config
from app.trident.pod_b import PassivbotManager
from app.trident.pod_b.paper_live_runner import PodBPaperLiveRunner
from app.trident.supervisor import TridentSupervisor
from app.trident.types import PodName


@dataclass(slots=True)
class TridentDryRunResult:
    pod_a: dict[str, object]
    pod_b: dict[str, object]
    pod_c: dict[str, object]
    pod_b_config_path: str
    pod_b_status_path: str
    status_path: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class TridentDryRunLauncher:
    """Starts Pod A, Pod B paper, and Pod C together for a fast dry-run."""

    def __init__(
        self,
        config: AppConfig,
        *,
        pod_a_runner_factory: Callable[..., Any] = PodALiveRunner,
        pod_b_runner_factory: Callable[..., Any] = PodBPaperLiveRunner,
        pod_c_runner_factory: Callable[..., Any] = PodCLiveRunner,
        force_enable_all_pods: bool = True,
    ) -> None:
        self.config = self._runtime_config(
            config,
            force_enable_all_pods=force_enable_all_pods,
        )
        self._pod_a_runner_factory = pod_a_runner_factory
        self._pod_b_runner_factory = pod_b_runner_factory
        self._pod_c_runner_factory = pod_c_runner_factory

    def _runtime_config(self, config: AppConfig, *, force_enable_all_pods: bool) -> AppConfig:
        if not force_enable_all_pods:
            return config
        return replace(
            config,
            pod_a=replace(config.pod_a, enabled=True),
            pod_b=replace(config.pod_b, enabled=True),
            pod_c=replace(config.pod_c, enabled=True),
        )

    async def run(
        self,
        *,
        max_runtime_seconds: float | None = None,
        max_messages: int | None = None,
        journal_dir: str | Path = "logs/dry_run_journals",
        report_dir: str | Path = "logs/dry_run_reports",
        pod_b_poll_seconds: float = 1.0,
        pod_b_max_idle_loops: int | None = None,
    ) -> TridentDryRunResult:
        journal_dir = Path(journal_dir)
        report_dir = Path(report_dir)
        journal_dir.mkdir(parents=True, exist_ok=True)
        report_dir.mkdir(parents=True, exist_ok=True)
        status_path = Path("logs/trident_dry_run_status.json")
        snapshot_root = Path(self.config.hyperliquid.snapshot_output_dir)
        pod_a_snapshot_dir = snapshot_root / "pod_a"
        pod_c_snapshot_dir = snapshot_root / "pod_c"
        pod_a_snapshot_dir.mkdir(parents=True, exist_ok=True)
        pod_c_snapshot_dir.mkdir(parents=True, exist_ok=True)

        pod_a_config = replace(
            self.config,
            hyperliquid=replace(
                self.config.hyperliquid,
                snapshot_output_dir=str(pod_a_snapshot_dir),
            ),
        )
        pod_c_config = replace(
            self.config,
            hyperliquid=replace(
                self.config.hyperliquid,
                snapshot_output_dir=str(pod_c_snapshot_dir),
            ),
        )

        supervisor = TridentSupervisor(
            config=self.config,
            profile="trident-dry-run-3pods",
            mode="dry-run",
        )
        manager = PassivbotManager(self.config)
        pod_b_allocation = supervisor.capital_plan.pod_allocations[PodName.POD_B]
        pod_b_owned_symbols = supervisor.registry.symbols_for(PodName.POD_B)
        pod_b_status = manager.sync(
            allocation=pod_b_allocation,
            owned_symbols=pod_b_owned_symbols,
        )

        self._write_launcher_status(
            status_path,
            process_state="starting",
            pod_b_config_path=self.config.pod_b.passivbot_config_path,
            pod_b_status_path=str(manager.status_path()),
        )

        pod_a_runner = self._pod_a_runner_factory(pod_a_config)
        pod_b_runner = self._pod_b_runner_factory(self.config.pod_b.passivbot_config_path)
        pod_c_runner = self._pod_c_runner_factory(pod_c_config)

        pod_a_task = asyncio.create_task(
            pod_a_runner.run(
                max_runtime_seconds=max_runtime_seconds,
                max_messages=max_messages,
                journal_path=journal_dir / "pod_a_live.jsonl",
            )
        )
        pod_c_task = asyncio.create_task(
            pod_c_runner.run(
                max_runtime_seconds=max_runtime_seconds,
                max_messages=max_messages,
                journal_path=journal_dir / "pod_c_live.jsonl",
            )
        )
        pod_b_task = asyncio.to_thread(
            pod_b_runner.run_live,
            input_path=pod_a_snapshot_dir,
            poll_seconds=pod_b_poll_seconds,
            max_runtime_seconds=max_runtime_seconds,
            journal_output=journal_dir / "pod_b_live.jsonl",
            report_output=report_dir / "pod_b_live_report.json",
            max_idle_loops=pod_b_max_idle_loops,
        )

        pod_a_result, pod_b_result, pod_c_result = await asyncio.gather(
            pod_a_task,
            pod_b_task,
            pod_c_task,
        )

        result = TridentDryRunResult(
            pod_a=self._to_dict(pod_a_result),
            pod_b=self._to_dict(pod_b_result),
            pod_c=self._to_dict(pod_c_result),
            pod_b_config_path=self.config.pod_b.passivbot_config_path,
            pod_b_status_path=str(pod_b_status.status_path),
            status_path=str(status_path),
        )
        self._write_launcher_status(
            status_path,
            process_state="completed",
            pod_b_config_path=result.pod_b_config_path,
            pod_b_status_path=result.pod_b_status_path,
            result=result.to_dict(),
        )
        return result

    def _to_dict(self, value: object) -> dict[str, object]:
        if isinstance(value, dict):
            return dict(value)
        if is_dataclass(value):
            return asdict(value)
        if hasattr(value, "__dict__"):
            return {
                key: field_value
                for key, field_value in vars(value).items()
                if not key.startswith("_")
            }
        return {"value": value}

    def _write_launcher_status(
        self,
        path: str | Path,
        *,
        process_state: str,
        pod_b_config_path: str,
        pod_b_status_path: str,
        result: dict[str, object] | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "pod": "trident_dry_run",
            "process_state": process_state,
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "pod_b_config_path": pod_b_config_path,
            "pod_b_status_path": pod_b_status_path,
        }
        if result is not None:
            payload["result"] = result
        write_runtime_status(path, payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Pod A, Pod B, and Pod C together in dry-run")
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--max-runtime-seconds", type=float)
    parser.add_argument("--max-messages", type=int)
    parser.add_argument("--journal-dir", default="logs/dry_run_journals")
    parser.add_argument("--report-dir", default="logs/dry_run_reports")
    parser.add_argument("--pod-b-poll-seconds", type=float, default=1.0)
    parser.add_argument("--pod-b-max-idle-loops", type=int)
    parser.add_argument(
        "--respect-config-enabled",
        action="store_true",
        help="Do not force-enable Pod A / Pod B / Pod C for this dry-run launcher.",
    )
    return parser


async def _run_from_args() -> None:
    args = build_parser().parse_args()
    launcher = TridentDryRunLauncher(
        load_config(args.config),
        force_enable_all_pods=not args.respect_config_enabled,
    )
    result = await launcher.run(
        max_runtime_seconds=args.max_runtime_seconds,
        max_messages=args.max_messages,
        journal_dir=args.journal_dir,
        report_dir=args.report_dir,
        pod_b_poll_seconds=args.pod_b_poll_seconds,
        pod_b_max_idle_loops=args.pod_b_max_idle_loops,
    )
    print(f"status_path={result.status_path}")
    print(f"pod_b_config_path={result.pod_b_config_path}")
    print(f"pod_b_status_path={result.pod_b_status_path}")
    print(f"pod_a_records_processed={result.pod_a.get('records_processed')}")
    print(f"pod_b_records_processed={result.pod_b.get('records_processed')}")
    print(f"pod_c_records_processed={result.pod_c.get('records_processed')}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_run_from_args())


if __name__ == "__main__":
    main()
