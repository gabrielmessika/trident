from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from app.hyperliquid.trigger_liquidity import (
    TriggerLiquidityEvent,
    iter_node_order_status_events,
    parse_node_order_status_event,
)
from app.settings import AppConfig, TriggerLiquidityConfig, load_config
from app.trident.trigger_liquidity.state import (
    TriggerLiquidityBook,
    parse_event_time_ms,
)


class TriggerLiquiditySnapshotEnricher:
    """Adds compact TP/SL trigger-liquidity features to TRIDENT snapshot JSONL files."""

    def __init__(self, config: TriggerLiquidityConfig) -> None:
        self.config = config

    def enrich(
        self,
        *,
        input_path: str | Path,
        trigger_source_path: str | Path,
        output_path: str | Path,
    ) -> dict[str, object]:
        events = sorted(
            iter_node_order_status_events(trigger_source_path),
            key=lambda event: event.event_time_ms or 0,
        )
        event_index = 0
        book = TriggerLiquidityBook()
        records_processed = 0
        symbols_enriched = 0

        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_output = output_file.with_name(f".{output_file.name}.tmp")
        try:
            with Path(input_path).open("r", encoding="utf-8") as src, tmp_output.open(
                "w",
                encoding="utf-8",
            ) as dst:
                for raw_line in src:
                    line = raw_line.strip()
                    if not line:
                        continue
                    payload = json.loads(line)
                    if not isinstance(payload, dict):
                        continue
                    snapshot_time_ms = parse_event_time_ms(payload.get("timestamp"))
                    if snapshot_time_ms is not None:
                        while event_index < len(events):
                            event = events[event_index]
                            if (
                                event.event_time_ms is not None
                                and event.event_time_ms > snapshot_time_ms
                            ):
                                break
                            self._apply_event(book, event)
                            event_index += 1

                    records_processed += 1
                    symbols_enriched += enrich_trigger_liquidity_payload(
                        payload,
                        config=self.config,
                        book=book,
                        now_ms=snapshot_time_ms,
                    )
                    dst.write(json.dumps(payload) + "\n")
            tmp_output.replace(output_file)
        except Exception:
            tmp_output.unlink(missing_ok=True)
            raise

        return {
            "input_path": str(input_path),
            "trigger_source_path": str(trigger_source_path),
            "output_path": str(output_path),
            "records_processed": records_processed,
            "symbols_enriched": symbols_enriched,
            "events_loaded": len(events),
        }

    def _apply_event(self, book: TriggerLiquidityBook, event: TriggerLiquidityEvent) -> None:
        apply_trigger_liquidity_event(book, event)


class TriggerLiquidityLiveRecordEnricher:
    """Incrementally enriches live records from the trigger-liquidity JSONL feed."""

    def __init__(
        self,
        config: TriggerLiquidityConfig,
        *,
        source_path: str | Path | None = None,
    ) -> None:
        self.config = config
        self.source_path = Path(source_path or config.source_path)
        self.book = TriggerLiquidityBook()
        self._file_offsets: dict[str, int] = {}
        self._pending_events: list[tuple[int, int, TriggerLiquidityEvent]] = []
        self._sequence = 0
        self.records_processed = 0
        self.records_enriched = 0
        self.symbols_seen = 0
        self.symbols_enriched = 0
        self.events_loaded = 0
        self.events_applied = 0
        self.error_count = 0
        self.last_error: str | None = None
        self.last_event_time_ms: int | None = None
        self.last_enriched_at: str | None = None

    def enrich_record(self, payload: dict[str, object]) -> dict[str, object]:
        if not self.config.enabled:
            return payload
        self.records_processed += 1
        try:
            self._load_new_events()
            snapshot_time_ms = parse_event_time_ms(payload.get("timestamp"))
            self.events_applied += self._apply_pending_events(snapshot_time_ms)
            symbols = payload.get("symbols")
            if isinstance(symbols, list):
                self.symbols_seen += sum(1 for item in symbols if isinstance(item, dict))
            enriched = enrich_trigger_liquidity_payload(
                payload,
                config=self.config,
                book=self.book,
                now_ms=snapshot_time_ms,
            )
            self.symbols_enriched += enriched
            if enriched > 0:
                self.records_enriched += 1
            self.last_error = None
            self.last_enriched_at = utc_now_iso()
        except Exception as exc:
            self.error_count += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
        return payload

    def status(self) -> dict[str, object]:
        return {
            "enabled": bool(self.config.enabled),
            "source_path": str(self.source_path),
            "records_processed": self.records_processed,
            "records_enriched": self.records_enriched,
            "symbols_seen": self.symbols_seen,
            "symbols_enriched": self.symbols_enriched,
            "events_loaded": self.events_loaded,
            "events_applied": self.events_applied,
            "pending_events": len(self._pending_events),
            "active_order_count": len(self.book.orders),
            "last_event_time": (
                datetime.fromtimestamp(self.last_event_time_ms / 1000, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
                if self.last_event_time_ms is not None
                else None
            ),
            "last_enriched_at": self.last_enriched_at,
            "error_count": self.error_count,
            "last_error": self.last_error,
        }

    def _load_new_events(self) -> None:
        for file_path in iter_trigger_source_files(self.source_path):
            key = str(file_path)
            try:
                size = file_path.stat().st_size
            except FileNotFoundError:
                continue
            offset = self._file_offsets.get(key, 0)
            if size < offset:
                offset = 0
            if size == offset:
                continue
            with file_path.open("rb") as handle:
                handle.seek(offset)
                while True:
                    line_start = handle.tell()
                    raw_line = handle.readline()
                    if not raw_line:
                        break
                    if not raw_line.endswith(b"\n"):
                        handle.seek(line_start)
                        break
                    event = self._parse_raw_event(raw_line)
                    if event is None:
                        continue
                    self._sequence += 1
                    self.events_loaded += 1
                    event_time_ms = event.event_time_ms or 0
                    self.last_event_time_ms = max(
                        self.last_event_time_ms or event_time_ms,
                        event_time_ms,
                    )
                    self._pending_events.append((event_time_ms, self._sequence, event))
                self._file_offsets[key] = handle.tell()
        if self._pending_events:
            self._pending_events.sort(key=lambda item: (item[0], item[1]))

    def _parse_raw_event(self, raw_line: bytes) -> TriggerLiquidityEvent | None:
        try:
            payload = json.loads(raw_line.decode("utf-8").strip())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        return parse_node_order_status_event(payload)

    def _apply_pending_events(self, snapshot_time_ms: int | None) -> int:
        if not self._pending_events:
            return 0
        cutoff = snapshot_time_ms if snapshot_time_ms is not None else float("inf")
        applied = 0
        for event_time_ms, _, event in self._pending_events:
            if event_time_ms > cutoff:
                break
            apply_trigger_liquidity_event(self.book, event)
            applied += 1
        if applied:
            del self._pending_events[:applied]
        return applied


def apply_trigger_liquidity_event(
    book: TriggerLiquidityBook,
    event: TriggerLiquidityEvent,
) -> None:
    order = event.order
    book.apply_order_status(
        {
            "time": event.event_time_ms,
            "status": event.status,
            "user": order.user,
            "order": {
                "coin": order.symbol,
                "side": order.side,
                "limitPx": order.limit_px,
                "sz": order.sz,
                "oid": order.oid,
                "timestamp": order.observed_at_ms,
                "triggerCondition": order.trigger_condition,
                "isTrigger": True,
                "triggerPx": order.trigger_px,
                "isPositionTpsl": order.is_position_tpsl,
                "reduceOnly": order.reduce_only,
                "orderType": order.order_type,
                "origSz": order.orig_sz,
            },
        }
    )


def enrich_trigger_liquidity_payload(
    payload: dict[str, object],
    *,
    config: TriggerLiquidityConfig,
    book: TriggerLiquidityBook,
    now_ms: int | None,
) -> int:
    symbols = payload.get("symbols")
    if not isinstance(symbols, list):
        return 0
    enriched = 0
    for item in symbols:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        reference_price = trigger_reference_price(item)
        features = book.features_for_symbol(
            symbol=symbol,
            reference_price=reference_price,
            bucket_bps=config.bucket_bps,
            lookahead_bps=config.lookahead_bps,
            min_cluster_notional_usd=config.min_cluster_notional_usd,
            now_ms=now_ms,
        )
        item.update(features.to_dict())
        if features.trigger_liquidity_available:
            enriched += 1
    return enriched


def trigger_reference_price(item: dict[str, object]) -> float:
    for key in ("mark_px", "price"):
        value = item.get(key)
        if value in (None, ""):
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return 0.0


def iter_trigger_source_files(path: str | Path):
    root = Path(path)
    if root.is_file():
        yield root
        return
    if not root.exists():
        return
    yield from sorted(
        item
        for item in root.glob("*.jsonl")
        if item.is_file() and not item.name.startswith(".")
    )

class TriggerLiquidityEnricherRunner:
    """Continuously mirrors live snapshots with trigger-liquidity shadow fields."""

    def __init__(
        self,
        app_config: AppConfig,
        *,
        poll_seconds: float = 60.0,
        status_path: str | Path = "runtime/trigger_liquidity_enricher_status.json",
    ) -> None:
        self.app_config = app_config
        self.poll_seconds = max(float(poll_seconds), 1.0)
        self.status_path = Path(status_path)
        self.enricher = TriggerLiquiditySnapshotEnricher(app_config.trigger_liquidity)
        self.error_count = 0
        self.last_success_at: str | None = None

    def run_once(self) -> dict[str, object]:
        source_path = Path(self.app_config.trigger_liquidity.source_path)
        self._ensure_source_path(source_path)

        input_path = latest_jsonl_file(Path(self.app_config.hyperliquid.snapshot_output_dir))
        if input_path is None:
            status = self._status(
                healthy=False,
                last_error="no_live_snapshot_input",
                input_path=None,
                output_path=None,
                trigger_source_path=source_path,
                result={},
            )
            self._write_status(status)
            return status

        output_path = Path(self.app_config.trigger_liquidity.snapshot_output_dir) / input_path.name
        result = self.enricher.enrich(
            input_path=input_path,
            trigger_source_path=source_path,
            output_path=output_path,
        )
        self.last_success_at = utc_now_iso()
        status = self._status(
            healthy=True,
            last_error=None,
            input_path=input_path,
            output_path=output_path,
            trigger_source_path=source_path,
            result=result,
        )
        self._write_status(status)
        return status

    def run_forever(self) -> None:
        while True:
            try:
                self.run_once()
            except Exception as exc:  # pragma: no cover - exercised by container runtime
                self.error_count += 1
                source_path = Path(self.app_config.trigger_liquidity.source_path)
                self._write_status(
                    self._status(
                        healthy=False,
                        last_error=f"{type(exc).__name__}: {exc}",
                        input_path=latest_jsonl_file(
                            Path(self.app_config.hyperliquid.snapshot_output_dir)
                        ),
                        output_path=None,
                        trigger_source_path=source_path,
                        result={},
                    )
                )
            time.sleep(self.poll_seconds)

    def _status(
        self,
        *,
        healthy: bool,
        last_error: str | None,
        input_path: Path | None,
        output_path: Path | None,
        trigger_source_path: Path,
        result: dict[str, object],
    ) -> dict[str, object]:
        source_meta = path_jsonl_meta(trigger_source_path)
        output_meta = path_jsonl_meta(output_path) if output_path is not None else {}
        return {
            "service": "trigger_liquidity_enricher",
            "process_state": "running",
            "healthy": healthy,
            "timestamp": utc_now_iso(),
            "last_success_at": self.last_success_at,
            "error_count": self.error_count,
            "last_error": last_error,
            "poll_seconds": self.poll_seconds,
            "config_enabled": bool(self.app_config.trigger_liquidity.enabled),
            "shadow_only": bool(self.app_config.trigger_liquidity.shadow_only),
            "input_path": str(input_path) if input_path is not None else None,
            "output_path": str(output_path) if output_path is not None else None,
            "trigger_source_path": str(trigger_source_path),
            "records_processed": int(result.get("records_processed", 0) or 0),
            "symbols_enriched": int(result.get("symbols_enriched", 0) or 0),
            "events_loaded": int(result.get("events_loaded", 0) or 0),
            "source_exists": trigger_source_path.exists(),
            "source_file_count": int(source_meta.get("file_count", 0) or 0),
            "source_latest_mtime": source_meta.get("latest_mtime"),
            "output_file_count": int(output_meta.get("file_count", 0) or 0),
            "output_latest_mtime": output_meta.get("latest_mtime"),
        }

    def _write_status(self, status: dict[str, object]) -> None:
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_status = self.status_path.with_name(f".{self.status_path.name}.tmp")
        tmp_status.write_text(
            json.dumps(status, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp_status.replace(self.status_path)

    def _ensure_source_path(self, source_path: Path) -> None:
        if source_path.suffix == ".jsonl":
            source_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            source_path.mkdir(parents=True, exist_ok=True)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def latest_jsonl_file(snapshot_dir: str | Path) -> Path | None:
    root = Path(snapshot_dir)
    if root.is_file():
        return root
    if not root.exists():
        return None
    files = sorted(
        (path for path in root.glob("*.jsonl") if path.is_file()),
        key=lambda path: (path.stat().st_mtime, path.name),
    )
    if not files:
        return None
    return files[-1]


def path_jsonl_meta(path: str | Path | None) -> dict[str, object]:
    if path is None:
        return {"file_count": 0, "latest_mtime": None}
    root = Path(path)
    if root.is_file():
        return {
            "file_count": 1,
            "latest_mtime": datetime.fromtimestamp(
                root.stat().st_mtime,
                tz=timezone.utc,
            )
            .isoformat()
            .replace("+00:00", "Z"),
        }
    if not root.exists():
        return {"file_count": 0, "latest_mtime": None}
    files = [item for item in root.glob("*.jsonl") if item.is_file()]
    if not files:
        return {"file_count": 0, "latest_mtime": None}
    latest_mtime = max(item.stat().st_mtime for item in files)
    return {
        "file_count": len(files),
        "latest_mtime": datetime.fromtimestamp(latest_mtime, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Enrich TRIDENT snapshots with HL TP/SL trigger liquidity"
    )
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--input")
    parser.add_argument("--trigger-source", help="Node order status JSONL file or directory")
    parser.add_argument("--output")
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Continuously enrich the latest live snapshot",
    )
    parser.add_argument("--poll-seconds", type=float, default=60.0)
    parser.add_argument(
        "--status-output",
        default="runtime/trigger_liquidity_enricher_status.json",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    app_config = load_config(args.config)
    if args.watch:
        TriggerLiquidityEnricherRunner(
            app_config,
            poll_seconds=args.poll_seconds,
            status_path=args.status_output,
        ).run_forever()
        return
    if not args.input or not args.output:
        parser.error("--input and --output are required unless --watch is set")
    trigger_source = args.trigger_source or app_config.trigger_liquidity.source_path
    result = TriggerLiquiditySnapshotEnricher(app_config.trigger_liquidity).enrich(
        input_path=args.input,
        trigger_source_path=trigger_source,
        output_path=args.output,
    )
    print(f"records_processed={result['records_processed']}")
    print(f"symbols_enriched={result['symbols_enriched']}")
    print(f"events_loaded={result['events_loaded']}")
    print(f"output_path={result['output_path']}")


if __name__ == "__main__":
    main()
