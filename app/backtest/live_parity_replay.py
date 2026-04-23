from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from app.live.pod_a_live_runner import PodALiveRunner
from app.live.pod_b_live_runner import PodBLiveRunner
from app.live.pod_c_live_runner import PodCLiveRunner
from app.backtest.snapshot_loader import SnapshotLoader
from app.settings import AppConfig, load_config


@dataclass(slots=True)
class LiveParityReplayResult:
    input_path: str
    records_processed: int
    records_routed_by_stream: dict[str, int]
    records_routed_by_inference: dict[str, int]
    unmatched_record_count: int
    first_timestamp: str | None
    last_timestamp: str | None
    pod_a: dict[str, object]
    pod_b: dict[str, object]
    pod_c: dict[str, object]
    report_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class TridentLiveParityReplayRunner:
    """Replays recorded live snapshot streams through the live runners themselves."""

    def __init__(
        self,
        config: AppConfig,
        *,
        pod_a_runner_factory: Callable[..., Any] = PodALiveRunner,
        pod_b_runner_factory: Callable[..., Any] = PodBLiveRunner,
        pod_c_runner_factory: Callable[..., Any] = PodCLiveRunner,
    ) -> None:
        self.config = config
        self.loader = SnapshotLoader()
        self._pod_a_runner_factory = pod_a_runner_factory
        self._pod_b_runner_factory = pod_b_runner_factory
        self._pod_c_runner_factory = pod_c_runner_factory

    def run_jsonl(
        self,
        input_path: str | Path,
        *,
        report_output: str | Path | None = None,
    ) -> LiveParityReplayResult:
        pod_a_runner = self._pod_a_runner_factory(self.config)
        pod_b_runner = self._pod_b_runner_factory(self.config)
        pod_c_runner = self._pod_c_runner_factory(self.config)
        runners = {
            self._stream_source_for(pod_a_runner, default="pod_a_live"): pod_a_runner,
            self._stream_source_for(pod_b_runner, default="pod_b_live"): pod_b_runner,
            self._stream_source_for(pod_c_runner, default="pod_c_live"): pod_c_runner,
        }
        records_routed_by_stream = {name: 0 for name in runners}
        records_routed_by_inference: dict[str, int] = {}
        records_processed = 0
        unmatched_record_count = 0
        first_timestamp: str | None = None
        last_timestamp: str | None = None

        for file_path in self._input_files(input_path):
            with file_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    payload = json.loads(line)
                    self.loader._validate_payload(payload, file_path=file_path)
                    record = self.loader._enrich_payload(payload)
                    timestamp = record.get("timestamp")
                    if isinstance(timestamp, str):
                        if first_timestamp is None:
                            first_timestamp = timestamp
                        last_timestamp = timestamp
                    stream_source, inferred_by = self._resolve_stream_source(
                        payload=payload,
                        runners=runners,
                    )
                    if stream_source is None:
                        unmatched_record_count += 1
                        continue
                    runner = runners[stream_source]
                    runner._process_record(record, journal=None)
                    records_processed += 1
                    records_routed_by_stream[stream_source] = (
                        records_routed_by_stream.get(stream_source, 0) + 1
                    )
                    if inferred_by is not None:
                        records_routed_by_inference[inferred_by] = (
                            records_routed_by_inference.get(inferred_by, 0) + 1
                        )

        result = LiveParityReplayResult(
            input_path=str(input_path),
            records_processed=records_processed,
            records_routed_by_stream=records_routed_by_stream,
            records_routed_by_inference=dict(sorted(records_routed_by_inference.items())),
            unmatched_record_count=unmatched_record_count,
            first_timestamp=first_timestamp,
            last_timestamp=last_timestamp,
            pod_a=self._runner_payload(pod_a_runner, records_routed_by_stream, "pod_a_live"),
            pod_b=self._runner_payload(pod_b_runner, records_routed_by_stream, "pod_b_live"),
            pod_c=self._runner_payload(pod_c_runner, records_routed_by_stream, "pod_c_live"),
            report_path=str(report_output) if report_output is not None else None,
        )
        if report_output is not None:
            report_path = Path(report_output)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(result.to_dict(), indent=2) + "\n",
                encoding="utf-8",
            )
        return result

    def _input_files(self, input_path: str | Path) -> list[Path]:
        path = Path(input_path)
        if path.is_file():
            return [path]
        return sorted(path.glob("*.jsonl"))

    def _resolve_stream_source(
        self,
        *,
        payload: dict[str, object],
        runners: dict[str, object],
    ) -> tuple[str | None, str | None]:
        raw_stream = payload.get("stream_source")
        if isinstance(raw_stream, str) and raw_stream in runners:
            return raw_stream, None

        symbols = {
            str(item.get("symbol", "")).strip().upper()
            for item in payload.get("symbols", [])
            if isinstance(item, dict) and str(item.get("symbol", "")).strip()
        }
        if not symbols:
            return None, None

        runner_symbols = {
            name: {
                str(symbol).strip().upper()
                for symbol in getattr(runner, "coins", [])
                if str(symbol).strip()
            }
            for name, runner in runners.items()
        }
        exact_matches = [
            name
            for name, available_symbols in runner_symbols.items()
            if available_symbols and symbols == available_symbols
        ]
        if len(exact_matches) == 1:
            return exact_matches[0], "exact_symbol_set"

        subset_matches = [
            name
            for name, available_symbols in runner_symbols.items()
            if available_symbols and symbols.issubset(available_symbols)
        ]
        if len(subset_matches) == 1:
            return subset_matches[0], "subset_symbol_set"

        # Historic server-data often has one full Pod A stream plus one TradFi-only Pod C stream.
        if "pod_c_live" in runners and symbols.issubset(runner_symbols.get("pod_c_live", set())):
            return "pod_c_live", "legacy_pod_c_subset"
        if "pod_a_live" in runners and symbols.issubset(runner_symbols.get("pod_a_live", set())):
            return "pod_a_live", "legacy_pod_a_subset"
        return None, None

    def _runner_payload(
        self,
        runner: object,
        records_routed_by_stream: dict[str, int],
        default_stream_source: str,
    ) -> dict[str, object]:
        stream_source = self._stream_source_for(runner, default=default_stream_source)
        report = runner.report.to_dict() if hasattr(runner, "report") else {}
        open_positions: list[dict[str, object]] = []
        if hasattr(runner, "_build_open_positions_payload"):
            open_positions = list(runner._build_open_positions_payload())  # type: ignore[misc]
        return {
            "stream_source": stream_source,
            "records_processed": records_routed_by_stream.get(stream_source, 0),
            "report": report,
            "open_positions": open_positions,
        }

    def _stream_source_for(self, runner: object, *, default: str) -> str:
        value = getattr(runner, "snapshot_stream_source", None)
        if isinstance(value, str) and value.strip():
            return value
        return default


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay recorded live snapshot streams through the live runners."
    )
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--input", required=True)
    parser.add_argument("--report-output")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = TridentLiveParityReplayRunner(load_config(args.config)).run_jsonl(
        input_path=args.input,
        report_output=args.report_output,
    )
    print(f"records_processed={result.records_processed}")
    print(f"unmatched_record_count={result.unmatched_record_count}")
    print(f"pod_a_closed_trade_count={result.pod_a['report'].get('closed_trade_count', 0)}")
    print(f"pod_b_closed_trade_count={result.pod_b['report'].get('closed_trade_count', 0)}")
    print(f"pod_c_closed_trade_count={result.pod_c['report'].get('closed_trade_count', 0)}")


if __name__ == "__main__":
    main()
