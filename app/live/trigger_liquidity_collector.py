from __future__ import annotations

import argparse
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib import error, request

from app.live.runtime_status import write_runtime_status
from app.trident.trigger_liquidity.state import parse_event_time_ms

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TriggerLiquidityCollectorStats:
    polls_completed: int = 0
    records_read: int = 0
    trigger_records_written: int = 0
    source_file_count: int = 0
    source_files_scanned: int = 0
    skipped_invalid: int = 0
    output_paths: set[str] = field(default_factory=set)
    last_event_time: str | None = None
    latest_block_number: int | None = None
    last_block_number: int | None = None
    blocks_requested: int = 0
    blocks_processed: int = 0
    rpc_requests: int = 0
    error_count: int = 0
    last_error: str | None = None
    last_error_at: str | None = None


class TriggerLiquidityNodeDataCollector:
    """Copies public Hyperliquid node trigger-order statuses into replayable JSONL."""

    def __init__(
        self,
        *,
        source_paths: Iterable[str | Path],
        output_dir: str | Path,
        state_path: str | Path,
        status_path: str | Path,
        lookback_hours: float = 6.0,
    ) -> None:
        self.source_paths = [Path(path).expanduser() for path in source_paths]
        self.output_dir = Path(output_dir)
        self.state_path = Path(state_path)
        self.status_path = Path(status_path)
        self.lookback_hours = max(float(lookback_hours), 0.0)

    def collect_once(self) -> TriggerLiquidityCollectorStats:
        stats = TriggerLiquidityCollectorStats()
        state = self._load_state()
        files = list(self._source_files(state))
        stats.source_file_count = len(files)

        for file_path in files:
            stats.source_files_scanned += 1
            self._collect_file(file_path, state, stats)

        self._write_state(state)
        stats.polls_completed = 1
        self._write_status(stats, process_state=self._process_state(stats))
        return stats

    def run(
        self,
        *,
        poll_seconds: float = 30.0,
        iterations: int | None = None,
    ) -> TriggerLiquidityCollectorStats:
        poll_interval = max(float(poll_seconds), 1.0)
        aggregate = TriggerLiquidityCollectorStats()
        self._write_status(aggregate, process_state="starting", poll_seconds=poll_interval)
        remaining = iterations

        while remaining is None or remaining > 0:
            try:
                stats = self.collect_once()
            except Exception as exc:
                aggregate.error_count += 1
                aggregate.last_error = f"{type(exc).__name__}: {exc}"
                aggregate.last_error_at = utc_now_iso()
                logger.warning("Trigger liquidity collector poll failed: %s", aggregate.last_error)
                self._write_status(
                    aggregate,
                    process_state="degraded",
                    poll_seconds=poll_interval,
                )
            else:
                aggregate.polls_completed += 1
                aggregate.records_read += stats.records_read
                aggregate.trigger_records_written += stats.trigger_records_written
                aggregate.source_file_count = stats.source_file_count
                aggregate.source_files_scanned = stats.source_files_scanned
                aggregate.skipped_invalid += stats.skipped_invalid
                aggregate.output_paths.update(stats.output_paths)
                aggregate.last_event_time = stats.last_event_time or aggregate.last_event_time
                aggregate.last_error = None
                self._write_status(
                    aggregate,
                    process_state=self._process_state(stats),
                    poll_seconds=poll_interval,
                )

            if remaining is not None:
                remaining -= 1
                if remaining <= 0:
                    break
            time.sleep(poll_interval)

        return aggregate

    def _collect_file(
        self,
        file_path: Path,
        state: dict[str, dict[str, object]],
        stats: TriggerLiquidityCollectorStats,
    ) -> None:
        file_key = str(file_path)
        file_state = state.get(file_key, {})
        offset = int(file_state.get("offset", 0) or 0)
        try:
            size = file_path.stat().st_size
        except OSError:
            return
        if offset > size:
            offset = 0

        with file_path.open("r", encoding="utf-8") as handle:
            handle.seek(offset)
            last_read_from = offset
            while True:
                line_start = handle.tell()
                raw_line = handle.readline()
                if raw_line == "":
                    break
                last_read_from = line_start
                line = raw_line.strip()
                if not line:
                    continue
                stats.records_read += 1
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    stats.skipped_invalid += 1
                    continue
                normalized = normalize_trigger_order_status(payload)
                if normalized is None:
                    continue
                output_path = self._write_event(normalized)
                stats.trigger_records_written += 1
                stats.output_paths.add(str(output_path))
                event_time = str(normalized.get("time") or "")
                if event_time:
                    stats.last_event_time = event_time
            state[file_key] = {
                "offset": handle.tell(),
                "size": size,
                "mtime": file_path.stat().st_mtime,
                "last_read_from": last_read_from,
            }

    def _write_event(self, payload: dict[str, object]) -> Path:
        output_path = self.output_dir / f"{event_date_key(payload.get('time'))}.jsonl"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
        return output_path

    def _source_files(self, state: dict[str, dict[str, object]]) -> Iterable[Path]:
        cutoff = datetime.now(timezone.utc).timestamp() - self.lookback_hours * 3600.0
        seen: set[Path] = set()
        for source in self.source_paths:
            if source.is_file():
                if source not in seen:
                    seen.add(source)
                    yield source
                continue
            if not source.exists():
                continue
            for file_path in sorted(path for path in source.rglob("*") if path.is_file()):
                if file_path.name.startswith("."):
                    continue
                if file_path in seen:
                    continue
                try:
                    stat = file_path.stat()
                except OSError:
                    continue
                if self.lookback_hours > 0.0 and stat.st_mtime < cutoff:
                    if str(file_path) not in state:
                        continue
                seen.add(file_path)
                yield file_path

    def _load_state(self) -> dict[str, dict[str, object]]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        files = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(files, dict):
            return {}
        return {str(key): value for key, value in files.items() if isinstance(value, dict)}

    def _write_state(self, state: dict[str, dict[str, object]]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": utc_now_iso(),
            "source_paths": [str(path) for path in self.source_paths],
            "files": state,
        }
        tmp_path = self.state_path.with_name(f".{self.state_path.name}.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp_path.replace(self.state_path)

    def _write_status(
        self,
        stats: TriggerLiquidityCollectorStats,
        *,
        process_state: str,
        poll_seconds: float | None = None,
    ) -> None:
        source_meta = source_paths_meta(self.source_paths)
        payload = {
            "service": "trigger_liquidity_collector",
            "label": "Trigger Liquidity Collector",
            "provider": "node_order_statuses",
            "process_state": process_state,
            "healthy": process_state == "running",
            "updated_at": utc_now_iso(),
            "source_paths": [str(path) for path in self.source_paths],
            "source_exists": any(item["exists"] for item in source_meta),
            "source_file_count": stats.source_file_count,
            "source_meta": source_meta,
            "output_dir": str(self.output_dir),
            "output_paths": sorted(stats.output_paths),
            "state_path": str(self.state_path),
            "status_path": str(self.status_path),
            "lookback_hours": self.lookback_hours,
            "poll_seconds": poll_seconds,
            "polls_completed": stats.polls_completed,
            "records_read": stats.records_read,
            "trigger_records_written": stats.trigger_records_written,
            "source_files_scanned": stats.source_files_scanned,
            "skipped_invalid": stats.skipped_invalid,
            "last_event_time": stats.last_event_time,
            "latest_block_number": stats.latest_block_number,
            "last_block_number": stats.last_block_number,
            "blocks_requested": stats.blocks_requested,
            "blocks_processed": stats.blocks_processed,
            "rpc_requests": stats.rpc_requests,
            "error_count": stats.error_count,
            "last_error": stats.last_error,
            "last_error_at": stats.last_error_at,
        }
        write_runtime_status(self.status_path, payload)

    def _process_state(self, stats: TriggerLiquidityCollectorStats) -> str:
        if stats.source_file_count <= 0:
            return "waiting_for_node_data"
        return "running"


class QuickNodeHypercoreClient:
    def __init__(self, *, url: str, timeout_seconds: float = 10.0) -> None:
        self.url = normalize_quicknode_hypercore_url(url)
        self.timeout_seconds = max(float(timeout_seconds), 1.0)
        self.request_count = 0

    def rpc(self, method: str, params: object) -> object:
        self.request_count += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self.request_count, "method": method, "params": params}).encode(
            "utf-8"
        )
        rpc_request = request.Request(
            self.url,
            data=body,
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(rpc_request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"QuickNode HTTP {exc.code}: {detail}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"QuickNode URL error: {exc.reason}") from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"QuickNode returned non-JSON response: {raw[:200]}") from exc
        if isinstance(payload, dict) and payload.get("error") is not None:
            raise RuntimeError(f"QuickNode RPC error: {payload['error']}")
        if not isinstance(payload, dict) or "result" not in payload:
            raise RuntimeError(f"QuickNode response missing result: {payload!r}")
        return payload["result"]

    def latest_block_number(self, stream: str) -> int:
        result = self.rpc("hl_getLatestBlockNumber", [stream])
        return int(result)

    def batch_blocks(self, *, stream: str, from_block: int, to_block: int) -> list[dict[str, object]]:
        result = self.rpc(
            "hl_getBatchBlocks",
            {"stream": stream, "from": int(from_block), "to": int(to_block)},
        )
        if not isinstance(result, dict):
            return []
        blocks = result.get("blocks")
        if not isinstance(blocks, list):
            return []
        return [block for block in blocks if isinstance(block, dict)]


class TriggerLiquidityQuickNodeCollector:
    """Polls QuickNode HyperCore order blocks and writes trigger-order statuses."""

    def __init__(
        self,
        *,
        quicknode_url: str,
        output_dir: str | Path,
        state_path: str | Path,
        status_path: str | Path,
        stream: str = "orders",
        batch_size: int = 200,
        initial_lookback_blocks: int = 2_000,
        max_blocks_per_poll: int = 1_000,
        timeout_seconds: float = 10.0,
        client: QuickNodeHypercoreClient | None = None,
    ) -> None:
        self.client = client or QuickNodeHypercoreClient(
            url=quicknode_url,
            timeout_seconds=timeout_seconds,
        )
        self.output_dir = Path(output_dir)
        self.state_path = Path(state_path)
        self.status_path = Path(status_path)
        self.stream = stream
        self.batch_size = max(1, min(int(batch_size), 200))
        self.initial_lookback_blocks = max(int(initial_lookback_blocks), 1)
        self.max_blocks_per_poll = max(int(max_blocks_per_poll), 1)

    def collect_once(self) -> TriggerLiquidityCollectorStats:
        stats = TriggerLiquidityCollectorStats()
        state = self._load_state()
        latest_block = self.client.latest_block_number(self.stream)
        stats.latest_block_number = latest_block
        stats.rpc_requests += 1

        last_block = as_int(state.get("last_block_number"))
        if last_block is None:
            from_block = max(latest_block - self.initial_lookback_blocks + 1, 0)
        else:
            from_block = last_block + 1

        to_block = min(latest_block, from_block + self.max_blocks_per_poll - 1)
        if from_block <= to_block:
            self._collect_range(from_block, to_block, stats)
            state["last_block_number"] = to_block
            stats.last_block_number = to_block
        else:
            stats.last_block_number = last_block

        self._write_state(state)
        stats.polls_completed = 1
        self._write_status(stats, process_state=self._process_state(stats))
        return stats

    def run(
        self,
        *,
        poll_seconds: float = 30.0,
        iterations: int | None = None,
    ) -> TriggerLiquidityCollectorStats:
        poll_interval = max(float(poll_seconds), 1.0)
        aggregate = TriggerLiquidityCollectorStats()
        self._write_status(aggregate, process_state="starting", poll_seconds=poll_interval)
        remaining = iterations

        while remaining is None or remaining > 0:
            try:
                stats = self.collect_once()
            except Exception as exc:
                aggregate.error_count += 1
                aggregate.last_error = f"{type(exc).__name__}: {exc}"
                aggregate.last_error_at = utc_now_iso()
                logger.warning("QuickNode trigger liquidity collector poll failed: %s", aggregate.last_error)
                self._write_status(
                    aggregate,
                    process_state="degraded",
                    poll_seconds=poll_interval,
                )
            else:
                aggregate.polls_completed += 1
                aggregate.records_read += stats.records_read
                aggregate.trigger_records_written += stats.trigger_records_written
                aggregate.skipped_invalid += stats.skipped_invalid
                aggregate.output_paths.update(stats.output_paths)
                aggregate.last_event_time = stats.last_event_time or aggregate.last_event_time
                aggregate.latest_block_number = stats.latest_block_number
                aggregate.last_block_number = stats.last_block_number
                aggregate.blocks_requested += stats.blocks_requested
                aggregate.blocks_processed += stats.blocks_processed
                aggregate.rpc_requests += stats.rpc_requests
                aggregate.last_error = None
                self._write_status(
                    aggregate,
                    process_state=self._process_state(stats),
                    poll_seconds=poll_interval,
                )

            if remaining is not None:
                remaining -= 1
                if remaining <= 0:
                    break
            time.sleep(poll_interval)

        return aggregate

    def _collect_range(
        self,
        from_block: int,
        to_block: int,
        stats: TriggerLiquidityCollectorStats,
    ) -> None:
        start = from_block
        while start <= to_block:
            end = min(start + self.batch_size - 1, to_block)
            stats.blocks_requested += end - start + 1
            try:
                blocks = self.client.batch_blocks(stream=self.stream, from_block=start, to_block=end)
            except RuntimeError as exc:
                if self.batch_size > 5 and "limited to a 5 range" in str(exc).lower():
                    logger.warning("QuickNode range limit detected; reducing batch size to 5")
                    self.batch_size = 5
                    continue
                raise
            stats.rpc_requests += 1
            for block in blocks:
                stats.blocks_processed += 1
                events = block.get("events")
                if not isinstance(events, list):
                    continue
                for event in events:
                    stats.records_read += 1
                    normalized = normalize_trigger_order_status(event, source="quicknode_orders")
                    if normalized is None:
                        continue
                    normalized["block_number"] = block.get("block_number")
                    output_path = self._write_event(normalized)
                    stats.trigger_records_written += 1
                    stats.output_paths.add(str(output_path))
                    event_time = str(normalized.get("time") or "")
                    if event_time:
                        stats.last_event_time = event_time
            start = end + 1

    def _write_event(self, payload: dict[str, object]) -> Path:
        output_path = self.output_dir / f"{event_date_key(payload.get('time'))}.jsonl"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
        return output_path

    def _load_state(self) -> dict[str, object]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        quicknode = payload.get("quicknode")
        if isinstance(quicknode, dict):
            return dict(quicknode)
        return {}

    def _write_state(self, state: dict[str, object]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": utc_now_iso(),
            "quicknode": {
                **state,
                "stream": self.stream,
                "url": redact_url(self.client.url),
            },
        }
        tmp_path = self.state_path.with_name(f".{self.state_path.name}.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp_path.replace(self.state_path)

    def _write_status(
        self,
        stats: TriggerLiquidityCollectorStats,
        *,
        process_state: str,
        poll_seconds: float | None = None,
    ) -> None:
        payload = {
            "service": "trigger_liquidity_collector",
            "label": "Trigger Liquidity Collector",
            "provider": "quicknode_orders",
            "process_state": process_state,
            "healthy": process_state == "running",
            "updated_at": utc_now_iso(),
            "quicknode_url": redact_url(self.client.url),
            "stream": self.stream,
            "output_dir": str(self.output_dir),
            "output_paths": sorted(stats.output_paths),
            "state_path": str(self.state_path),
            "status_path": str(self.status_path),
            "batch_size": self.batch_size,
            "initial_lookback_blocks": self.initial_lookback_blocks,
            "max_blocks_per_poll": self.max_blocks_per_poll,
            "poll_seconds": poll_seconds,
            "polls_completed": stats.polls_completed,
            "records_read": stats.records_read,
            "trigger_records_written": stats.trigger_records_written,
            "skipped_invalid": stats.skipped_invalid,
            "last_event_time": stats.last_event_time,
            "latest_block_number": stats.latest_block_number,
            "last_block_number": stats.last_block_number,
            "blocks_requested": stats.blocks_requested,
            "blocks_processed": stats.blocks_processed,
            "rpc_requests": stats.rpc_requests,
            "error_count": stats.error_count,
            "last_error": stats.last_error,
            "last_error_at": stats.last_error_at,
        }
        write_runtime_status(self.status_path, payload)

    def _process_state(self, stats: TriggerLiquidityCollectorStats) -> str:
        if stats.latest_block_number is None:
            return "waiting_for_quicknode"
        return "running"


def normalize_trigger_order_status(
    payload: object,
    *,
    source: str = "hyperliquid_node_order_statuses",
) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    order = payload.get("order")
    if not isinstance(order, dict):
        return None
    if not bool(order.get("isTrigger", False)):
        return None
    try:
        trigger_px = float(order.get("triggerPx") or 0.0)
    except (TypeError, ValueError):
        trigger_px = 0.0
    if trigger_px <= 0.0:
        return None

    return {
        "time": payload.get("time"),
        "user": str(payload.get("user", "")),
        "status": str(payload.get("status", "open")),
        "order": order,
        "source": source,
        "collected_at": utc_now_iso(),
    }


def event_date_key(raw_time: object) -> str:
    event_time_ms = parse_event_time_ms(raw_time)
    if event_time_ms is None:
        return datetime.now(timezone.utc).date().isoformat()
    return datetime.fromtimestamp(event_time_ms / 1000.0, tz=timezone.utc).date().isoformat()


def source_paths_meta(paths: Iterable[Path]) -> list[dict[str, object]]:
    meta: list[dict[str, object]] = []
    for path in paths:
        item: dict[str, object] = {"path": str(path), "exists": path.exists()}
        if path.is_file():
            item["file_count"] = 1
            item["latest_mtime"] = mtime_iso(path)
        elif path.is_dir():
            files = [candidate for candidate in path.rglob("*") if candidate.is_file()]
            item["file_count"] = len(files)
            item["latest_mtime"] = max((mtime_iso(file_path) for file_path in files), default=None)
        else:
            item["file_count"] = 0
            item["latest_mtime"] = None
        meta.append(item)
    return meta


def mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat().replace(
        "+00:00",
        "Z",
    )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_quicknode_hypercore_url(raw_url: str) -> str:
    url = raw_url.strip().rstrip("/")
    if not url:
        return ""
    if url.endswith("/hypercore"):
        return url
    if url.endswith("/hypercore/ws"):
        return url[: -len("/ws")]
    for suffix in ("/evm", "/nanoreth", "/info"):
        if url.endswith(suffix):
            return url[: -len(suffix)] + "/hypercore"
    return f"{url}/hypercore"


def redact_url(raw_url: str) -> str:
    url = raw_url.strip()
    if not url:
        return ""
    scheme_sep = "://"
    if scheme_sep not in url:
        return "<configured>"
    scheme, rest = url.split(scheme_sep, 1)
    host = rest.split("/", 1)[0]
    suffix = "/hypercore" if url.rstrip("/").endswith("/hypercore") else ""
    return f"{scheme}{scheme_sep}{host}/...{suffix}"


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "")
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    value = os.environ.get(name, "")
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect public Hyperliquid node_order_statuses trigger orders"
    )
    parser.add_argument(
        "--node-source",
        action="append",
        dest="node_sources",
        help="Hyperliquid node_order_statuses file or directory. Can be repeated.",
    )
    parser.add_argument("--output-dir", default="data/trigger_liquidity")
    parser.add_argument("--state-path", default="runtime/trigger_liquidity_collector_state.json")
    parser.add_argument("--status-output", default="runtime/trigger_liquidity_collector_status.json")
    parser.add_argument("--lookback-hours", type=float, default=6.0)
    parser.add_argument("--poll-seconds", type=float, default=30.0)
    parser.add_argument("--iterations", type=int)
    parser.add_argument(
        "--quicknode-url",
        default=os.environ.get("TRIDENT_TRIGGER_LIQUIDITY_QUICKNODE_URL")
        or os.environ.get("TRIDENT_QUICKNODE_HYPERCORE_URL")
        or os.environ.get("QUICKNODE_HYPERCORE_URL")
        or "",
        help="QuickNode Hyperliquid endpoint URL. Base URL or /hypercore URL accepted.",
    )
    parser.add_argument(
        "--quicknode-stream",
        default=os.environ.get("TRIDENT_TRIGGER_LIQUIDITY_QUICKNODE_STREAM", "orders"),
    )
    parser.add_argument(
        "--quicknode-batch-size",
        type=int,
        default=env_int("TRIDENT_TRIGGER_LIQUIDITY_QUICKNODE_BATCH_SIZE", 200),
    )
    parser.add_argument(
        "--quicknode-initial-lookback-blocks",
        type=int,
        default=env_int("TRIDENT_TRIGGER_LIQUIDITY_QUICKNODE_INITIAL_LOOKBACK_BLOCKS", 2_000),
    )
    parser.add_argument(
        "--quicknode-max-blocks-per-poll",
        type=int,
        default=env_int("TRIDENT_TRIGGER_LIQUIDITY_QUICKNODE_MAX_BLOCKS_PER_POLL", 1_000),
    )
    parser.add_argument(
        "--quicknode-timeout-seconds",
        type=float,
        default=env_float("TRIDENT_TRIGGER_LIQUIDITY_QUICKNODE_TIMEOUT_SECONDS", 10.0),
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    quicknode_url = str(args.quicknode_url or "").strip()
    if quicknode_url:
        stats = TriggerLiquidityQuickNodeCollector(
            quicknode_url=quicknode_url,
            output_dir=args.output_dir,
            state_path=args.state_path,
            status_path=args.status_output,
            stream=args.quicknode_stream,
            batch_size=args.quicknode_batch_size,
            initial_lookback_blocks=args.quicknode_initial_lookback_blocks,
            max_blocks_per_poll=args.quicknode_max_blocks_per_poll,
            timeout_seconds=args.quicknode_timeout_seconds,
        ).run(
            poll_seconds=args.poll_seconds,
            iterations=args.iterations,
        )
        print(f"polls_completed={stats.polls_completed}")
        print(f"trigger_records_written={stats.trigger_records_written}")
        print(f"last_block_number={stats.last_block_number}")
        return

    sources = args.node_sources or ["data/node_order_statuses/hourly"]
    stats = TriggerLiquidityNodeDataCollector(
        source_paths=sources,
        output_dir=args.output_dir,
        state_path=args.state_path,
        status_path=args.status_output,
        lookback_hours=args.lookback_hours,
    ).run(
        poll_seconds=args.poll_seconds,
        iterations=args.iterations,
    )
    print(f"polls_completed={stats.polls_completed}")
    print(f"trigger_records_written={stats.trigger_records_written}")
    print(f"source_file_count={stats.source_file_count}")


if __name__ == "__main__":
    main()
