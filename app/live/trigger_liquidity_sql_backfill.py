from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, request

from app.live.runtime_status import write_runtime_status
from app.live.trigger_liquidity_collector import (
    event_date_key,
    utc_now_iso,
)
from app.trident.trigger_liquidity.state import parse_event_time_ms


SQL_ENDPOINT = "https://api.quicknode.com/sql/rest/v1/query"
SQL_CLUSTER_ID = "hyperliquid-core-mainnet"


@dataclass(slots=True)
class TriggerLiquiditySqlBackfillStats:
    pages_completed: int = 0
    rows_read: int = 0
    trigger_records_written: int = 0
    output_paths: set[str] = field(default_factory=set)
    last_status_time: str | None = None
    last_block_number: int | None = None
    last_unique_id: str | None = None
    rows_before_limit_at_least: int | None = None
    sql_requests: int = 0
    credits_used: float = 0.0
    completed: bool = False
    error_count: int = 0
    last_error: str | None = None
    last_error_at: str | None = None


class QuickNodeSqlExplorerClient:
    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str = SQL_ENDPOINT,
        cluster_id: str = SQL_CLUSTER_ID,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.api_key = api_key.strip()
        self.endpoint = endpoint
        self.cluster_id = cluster_id
        self.timeout_seconds = max(float(timeout_seconds), 1.0)

    def query(self, sql: str) -> tuple[dict[str, object], float]:
        body = json.dumps({"query": sql, "clusterId": self.cluster_id}).encode("utf-8")
        sql_request = request.Request(
            self.endpoint,
            data=body,
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "user-agent": "curl/8.5.0 trident-trigger-liquidity-sql-backfill/0.1",
                "x-api-key": self.api_key,
            },
            method="POST",
        )
        try:
            with request.urlopen(sql_request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
                credits = float(response.headers.get("x-credits") or 0.0)
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"QuickNode SQL HTTP {exc.code}: {detail}") from exc
        except error.URLError as exc:
            raise RuntimeError(f"QuickNode SQL URL error: {exc.reason}") from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"QuickNode SQL returned non-JSON response: {raw[:200]}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"QuickNode SQL returned unexpected response: {payload!r}")
        if payload.get("error") is not None:
            raise RuntimeError(f"QuickNode SQL error: {payload['error']}")
        if credits <= 0.0:
            try:
                credits = float(payload.get("credits") or 0.0)
            except (TypeError, ValueError):
                credits = 0.0
        return payload, credits


class TriggerLiquiditySqlBackfiller:
    """Backfills historical TP/SL order statuses from QuickNode SQL Explorer."""

    def __init__(
        self,
        *,
        api_key: str,
        output_dir: str | Path,
        state_path: str | Path,
        status_path: str | Path,
        start_time: str,
        end_time: str,
        page_size: int = 1000,
        sleep_seconds: float = 0.2,
        client: QuickNodeSqlExplorerClient | None = None,
    ) -> None:
        self.client = client or QuickNodeSqlExplorerClient(api_key=api_key)
        self.output_dir = Path(output_dir)
        self.state_path = Path(state_path)
        self.status_path = Path(status_path)
        self.start_time = sql_time_literal(start_time)
        self.end_time = sql_time_literal(end_time)
        self.page_size = max(1, min(int(page_size), 1000))
        self.sleep_seconds = max(float(sleep_seconds), 0.0)

    def run(self, *, max_pages: int | None = None) -> TriggerLiquiditySqlBackfillStats:
        stats = TriggerLiquiditySqlBackfillStats()
        state = self._load_state()
        if state.get("start_time") != self.start_time or state.get("end_time") != self.end_time:
            state = {"start_time": self.start_time, "end_time": self.end_time}

        self._write_status(stats, process_state="starting")
        remaining = max_pages
        while remaining is None or remaining > 0:
            try:
                rows, rows_before_limit, credits = self._query_page(state)
            except Exception as exc:
                stats.error_count += 1
                stats.last_error = f"{type(exc).__name__}: {exc}"
                stats.last_error_at = utc_now_iso()
                self._write_status(stats, process_state="degraded")
                raise

            stats.sql_requests += 1
            stats.credits_used += credits
            stats.rows_before_limit_at_least = rows_before_limit
            if not rows:
                stats.completed = True
                state["completed"] = True
                self._write_state(state)
                self._write_status(stats, process_state="completed")
                break

            for row in rows:
                stats.rows_read += 1
                normalized = normalize_sql_order_row(row)
                if normalized is None:
                    continue
                output_path = self._write_event(normalized)
                stats.trigger_records_written += 1
                stats.output_paths.add(str(output_path))

            last = rows[-1]
            state["last_status_time"] = str(
                last.get("status_time_text") or last.get("status_time") or ""
            )
            state["last_block_number"] = int(last.get("block_number") or 0)
            state["last_unique_id"] = str(last.get("unique_id") or "")
            state["completed"] = False
            self._write_state(state)

            stats.pages_completed += 1
            stats.last_status_time = str(state["last_status_time"])
            stats.last_block_number = int(state["last_block_number"])
            stats.last_unique_id = str(state["last_unique_id"])
            self._write_status(stats, process_state="running")

            if len(rows) < self.page_size:
                stats.completed = True
                state["completed"] = True
                self._write_state(state)
                self._write_status(stats, process_state="completed")
                break
            if remaining is not None:
                remaining -= 1
                if remaining <= 0:
                    break
            if self.sleep_seconds > 0:
                time.sleep(self.sleep_seconds)

        return stats

    def _query_page(self, state: dict[str, object]) -> tuple[list[dict[str, object]], int | None, float]:
        sql = build_trigger_liquidity_sql(
            start_time=self.start_time,
            end_time=self.end_time,
            page_size=self.page_size,
            last_status_time=str(state.get("last_status_time") or ""),
            last_block_number=as_int(state.get("last_block_number")),
            last_unique_id=str(state.get("last_unique_id") or ""),
        )
        payload, credits = self.client.query(sql)
        data = payload.get("data")
        rows = [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []
        rows_before_limit = as_int(payload.get("rows_before_limit_at_least"))
        return rows, rows_before_limit, credits

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
        state = payload.get("sql_backfill") if isinstance(payload, dict) else None
        return dict(state) if isinstance(state, dict) else {}

    def _write_state(self, state: dict[str, object]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"updated_at": utc_now_iso(), "sql_backfill": state}
        tmp_path = self.state_path.with_name(f".{self.state_path.name}.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp_path.replace(self.state_path)

    def _write_status(
        self,
        stats: TriggerLiquiditySqlBackfillStats,
        *,
        process_state: str,
    ) -> None:
        payload = {
            "service": "trigger_liquidity_sql_backfill",
            "label": "Trigger Liquidity SQL Backfill",
            "provider": "quicknode_sql_hyperliquid_orders",
            "process_state": process_state,
            "healthy": process_state in {"running", "completed"},
            "updated_at": utc_now_iso(),
            "start_time": self.start_time,
            "end_time": self.end_time,
            "page_size": self.page_size,
            "output_dir": str(self.output_dir),
            "output_paths": sorted(stats.output_paths),
            "state_path": str(self.state_path),
            "status_path": str(self.status_path),
            "pages_completed": stats.pages_completed,
            "rows_read": stats.rows_read,
            "trigger_records_written": stats.trigger_records_written,
            "last_status_time": stats.last_status_time,
            "last_block_number": stats.last_block_number,
            "last_unique_id": stats.last_unique_id,
            "rows_before_limit_at_least": stats.rows_before_limit_at_least,
            "sql_requests": stats.sql_requests,
            "credits_used": round(stats.credits_used, 4),
            "completed": stats.completed,
            "error_count": stats.error_count,
            "last_error": stats.last_error,
            "last_error_at": stats.last_error_at,
        }
        write_runtime_status(self.status_path, payload)


def build_trigger_liquidity_sql(
    *,
    start_time: str,
    end_time: str,
    page_size: int,
    last_status_time: str = "",
    last_block_number: int | None = None,
    last_unique_id: str = "",
) -> str:
    keyset = ""
    if last_status_time and last_block_number is not None and last_unique_id:
        last_time = sql_quote(last_status_time)
        last_unique = sql_quote(last_unique_id)
        keyset = f"""
  AND (
    status_time > toDateTime64({last_time}, 6, 'UTC')
    OR (
      status_time = toDateTime64({last_time}, 6, 'UTC')
      AND block_number > {int(last_block_number)}
    )
    OR (
      status_time = toDateTime64({last_time}, 6, 'UTC')
      AND block_number = {int(last_block_number)}
      AND unique_id > {last_unique}
    )
  )"""
    return f"""
SELECT
  block_number,
  toString(status_time) AS status_time_text,
  toString(block_time) AS block_time_text,
  user,
  hash,
  status,
  coin,
  side,
  toString(limit_price) AS limit_price,
  toString(size) AS size,
  oid,
  toString(order_timestamp) AS order_timestamp_text,
  trigger_condition,
  toString(trigger_price) AS trigger_price_text,
  is_position_tpsl,
  reduce_only,
  order_type,
  toString(orig_size) AS orig_size,
  tif,
  cloid,
  unique_id
FROM hyperliquid_orders
WHERE status_time >= toDateTime64({sql_quote(start_time)}, 6, 'UTC')
  AND status_time < toDateTime64({sql_quote(end_time)}, 6, 'UTC')
  AND is_trigger = 1
  AND trigger_price > 0{keyset}
ORDER BY status_time ASC, block_number ASC, unique_id ASC
LIMIT {int(page_size)}
""".strip()


def normalize_sql_order_row(row: dict[str, object]) -> dict[str, object] | None:
    trigger_px = decimal_text(row.get("trigger_price_text") or row.get("trigger_price"))
    try:
        if float(trigger_px) <= 0.0:
            return None
    except ValueError:
        return None
    status_time = sql_datetime_to_iso(row.get("status_time_text") or row.get("status_time"))
    order_timestamp = parse_event_time_ms(
        sql_datetime_to_iso(row.get("order_timestamp_text") or row.get("order_timestamp"))
    )
    order = {
        "coin": str(row.get("coin") or ""),
        "side": str(row.get("side") or ""),
        "limitPx": decimal_text(row.get("limit_price")),
        "sz": decimal_text(row.get("size")),
        "oid": as_int(row.get("oid")) or 0,
        "timestamp": order_timestamp,
        "triggerCondition": nullable_text(row.get("trigger_condition")) or "N/A",
        "isTrigger": True,
        "triggerPx": trigger_px,
        "children": [],
        "isPositionTpsl": bool_from_sql(row.get("is_position_tpsl")),
        "reduceOnly": bool_from_sql(row.get("reduce_only")),
        "orderType": str(row.get("order_type") or ""),
        "origSz": decimal_text(row.get("orig_size")),
        "tif": nullable_text(row.get("tif")),
        "cloid": nullable_text(row.get("cloid")),
    }
    return {
        "time": status_time,
        "user": str(row.get("user") or ""),
        "hash": nullable_text(row.get("hash")),
        "status": str(row.get("status") or "open"),
        "order": order,
        "source": "quicknode_sql_hyperliquid_orders",
        "collected_at": utc_now_iso(),
        "block_number": as_int(row.get("block_number")),
        "unique_id": str(row.get("unique_id") or ""),
    }


def sql_time_literal(raw: str) -> str:
    text = raw.strip()
    if not text:
        raise ValueError("empty SQL timestamp")
    normalized = text.replace("Z", "+00:00")
    if "T" not in normalized and len(normalized) == 10:
        normalized = f"{normalized}T00:00:00+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%Y-%m-%d %H:%M:%S.%f")


def sql_datetime_to_iso(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if "T" in text:
        normalized = text.replace("Z", "+00:00")
    else:
        normalized = text.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def decimal_text(value: object) -> str:
    if value in (None, ""):
        return "0.0"
    return str(value)


def nullable_text(value: object) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def bool_from_sql(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "t", "yes", "y"}


def as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill Hyperliquid trigger-order statuses from QuickNode SQL Explorer"
    )
    parser.add_argument("--start", required=True, help="Inclusive UTC start date/time")
    parser.add_argument("--end", required=True, help="Exclusive UTC end date/time")
    parser.add_argument(
        "--api-key-env",
        default="TRIDENT_TRIGGER_LIQUIDITY_SQL_API_KEY",
        help="Environment variable containing the QuickNode SQL Explorer API key",
    )
    parser.add_argument("--output-dir", default="data/trigger_liquidity")
    parser.add_argument(
        "--state-path",
        default="runtime/trigger_liquidity_sql_backfill_state.json",
    )
    parser.add_argument(
        "--status-output",
        default="runtime/trigger_liquidity_sql_backfill_status.json",
    )
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--sleep-seconds", type=float, default=0.2)
    parser.add_argument("--max-pages", type=int)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    api_key = os.environ.get(args.api_key_env, "").strip()
    if not api_key:
        raise SystemExit(f"Missing QuickNode SQL API key env: {args.api_key_env}")
    stats = TriggerLiquiditySqlBackfiller(
        api_key=api_key,
        output_dir=args.output_dir,
        state_path=args.state_path,
        status_path=args.status_output,
        start_time=args.start,
        end_time=args.end,
        page_size=args.page_size,
        sleep_seconds=args.sleep_seconds,
    ).run(max_pages=args.max_pages)
    print(f"pages_completed={stats.pages_completed}")
    print(f"trigger_records_written={stats.trigger_records_written}")
    print(f"completed={stats.completed}")


if __name__ == "__main__":
    main()
