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
from app.trident.hip4_outcome import HIP4OutcomeEdgePod, load_hip4_outcome_config


@dataclass(slots=True)
class TridentDryRunResult:
    pod_a: dict[str, object]
    pod_b: dict[str, object]
    pod_c: dict[str, object]
    hip4_outcome: dict[str, object]
    pod_b_status_path: str
    hip4_outcome_status_path: str
    status_path: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class TridentDryRunLauncher:
    """Starts Pod A, Pod C, and the HIP-4 outcome replacement for Pod B."""

    def __init__(
        self,
        config: AppConfig,
        *,
        pod_a_runner_factory: Callable[..., Any] = PodALiveRunner,
        pod_b_runner_factory: Callable[..., Any] | None = None,
        pod_c_runner_factory: Callable[..., Any] = PodCLiveRunner,
        hip4_outcome_runner_factory: Callable[..., Any] | None = None,
        force_enable_all_pods: bool = True,
        enable_hip4_outcome: bool = True,
        hip4_outcome_config_path: str | Path = "config/hip4_outcome_testnet.toml",
    ) -> None:
        self.config = self._runtime_config(
            config,
            force_enable_all_pods=force_enable_all_pods,
        )
        self._pod_a_runner_factory = pod_a_runner_factory
        self._legacy_pod_b_runner_factory = pod_b_runner_factory
        self._pod_c_runner_factory = pod_c_runner_factory
        self._hip4_outcome_runner_factory = (
            hip4_outcome_runner_factory or self._default_hip4_outcome_runner_factory
        )
        self.enable_hip4_outcome = enable_hip4_outcome
        self.hip4_outcome_config_path = str(hip4_outcome_config_path)

    def _runtime_config(self, config: AppConfig, *, force_enable_all_pods: bool) -> AppConfig:
        if not force_enable_all_pods:
            return config
        return replace(
            config,
            pod_a=replace(config.pod_a, enabled=True),
            pod_b=replace(config.pod_b, enabled=False),
            pod_c=replace(config.pod_c, enabled=True),
        )

    async def run(
        self,
        *,
        max_runtime_seconds: float | None = None,
        max_messages: int | None = None,
        journal_dir: str | Path = "logs/dry_run_journals",
        report_dir: str | Path = "logs/dry_run_reports",
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

        self._write_launcher_status(
            status_path,
            process_state="starting",
            pod_b_status_path="logs/pod_b_live_status.json",
            hip4_outcome_status_path="logs/hip4_outcome_status.json",
        )

        pod_a_runner = self._pod_a_runner_factory(pod_a_config)
        pod_c_runner = self._pod_c_runner_factory(pod_c_config)
        hip4_runner = (
            self._hip4_outcome_runner_factory(self.hip4_outcome_config_path)
            if self.enable_hip4_outcome
            else None
        )

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
        hip4_task = (
            asyncio.create_task(
                asyncio.to_thread(
                    self._run_hip4_outcome,
                    hip4_runner,
                    max_runtime_seconds=max_runtime_seconds,
                    max_loops=max_messages,
                )
            )
            if hip4_runner is not None
            else None
        )

        gathered = await asyncio.gather(
            pod_a_task,
            pod_c_task,
            *([hip4_task] if hip4_task is not None else []),
        )
        pod_a_result, pod_c_result = gathered[:2]
        hip4_result = (
            gathered[2]
            if len(gathered) > 2
            else {"enabled": False, "mode": "paper", "reason": "pod_b_disabled"}
        )
        pod_b_result = hip4_result

        result = TridentDryRunResult(
            pod_a=self._to_dict(pod_a_result),
            pod_b=self._to_dict(pod_b_result),
            pod_c=self._to_dict(pod_c_result),
            hip4_outcome=self._to_dict(hip4_result),
            pod_b_status_path="logs/pod_b_live_status.json",
            hip4_outcome_status_path="logs/hip4_outcome_status.json",
            status_path=str(status_path),
        )
        self._write_launcher_status(
            status_path,
            process_state="completed",
            pod_b_status_path=result.pod_b_status_path,
            hip4_outcome_status_path=result.hip4_outcome_status_path,
            result=result.to_dict(),
        )
        return result

    def _default_hip4_outcome_runner_factory(self, config_path: str | Path) -> HIP4OutcomeEdgePod:
        config = load_hip4_outcome_config(config_path).with_mode("paper")
        return HIP4OutcomeEdgePod(config)

    def _run_hip4_outcome(
        self,
        runner: Any,
        *,
        max_runtime_seconds: float | None,
        max_loops: int | None,
    ) -> object:
        return runner.run(
            max_runtime_seconds=max_runtime_seconds,
            max_loops=max_loops,
        )

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
        pod_b_status_path: str,
        hip4_outcome_status_path: str,
        result: dict[str, object] | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "pod": "trident_dry_run",
            "process_state": process_state,
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "pod_b_status_path": pod_b_status_path,
            "hip4_outcome_status_path": hip4_outcome_status_path,
        }
        if result is not None:
            payload["result"] = result
        write_runtime_status(path, payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run Pod A, HIP-4 outcome Pod B, and Pod C together in dry-run"
    )
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--max-runtime-seconds", type=float)
    parser.add_argument("--max-messages", type=int)
    parser.add_argument("--journal-dir", default="logs/dry_run_journals")
    parser.add_argument("--report-dir", default="logs/dry_run_reports")
    parser.add_argument("--hip4-outcome-config", default="config/hip4_outcome_testnet.toml")
    parser.add_argument(
        "--without-hip4-outcome",
        action="store_true",
        help="Do not start the HIP-4 outcome Pod B replacement during the dry-run.",
    )
    parser.add_argument(
        "--respect-config-enabled",
        action="store_true",
        help="Do not force-enable Pod A / Pod C for this dry-run launcher.",
    )
    return parser


async def _run_from_args() -> None:
    args = build_parser().parse_args()
    launcher = TridentDryRunLauncher(
        load_config(args.config),
        force_enable_all_pods=not args.respect_config_enabled,
        enable_hip4_outcome=not args.without_hip4_outcome,
        hip4_outcome_config_path=args.hip4_outcome_config,
    )
    result = await launcher.run(
        max_runtime_seconds=args.max_runtime_seconds,
        max_messages=args.max_messages,
        journal_dir=args.journal_dir,
        report_dir=args.report_dir,
    )
    print(f"status_path={result.status_path}")
    print(f"pod_b_status_path={result.pod_b_status_path}")
    print(f"hip4_outcome_status_path={result.hip4_outcome_status_path}")
    print(f"pod_a_records_processed={result.pod_a.get('records_processed')}")
    print(f"pod_b_hip4_loop_count={result.pod_b.get('loop_count')}")
    print(f"pod_c_records_processed={result.pod_c.get('records_processed')}")
    print(f"hip4_outcome_loop_count={result.hip4_outcome.get('loop_count')}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_run_from_args())


if __name__ == "__main__":
    main()
