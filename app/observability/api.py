from __future__ import annotations

import base64
import contextlib
import copy
import csv
import io
import json
import math
import os
import secrets
from collections import Counter
from datetime import datetime, timedelta, timezone
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from app.observability.metrics import MetricsRegistry
from app.version import VERSION
from app.reporting.multi_pod import (
    build_runtime_report,
    _is_supervisor_fallback_runtime,
    is_hip4_pod_b_replacement_runtime,
)
from app.reporting.live_journal import attach_live_journal_report
from app.trident.hip4_outcome.reporting import replay_opportunities
from app.backtest.snapshot_loader import (
    SnapshotFormatError,
    SnapshotLoader,
    SnapshotRecord,
    merge_snapshot_records,
)
from app.live.runtime_status import (
    load_runtime_status,
    sanitize_runtime_status_payload,
    runtime_status_age_seconds,
    runtime_status_is_fresh,
)
from app.observability.runtime_merge import merge_runtime_supervisor_snapshot
from app.trident.market_clusters import (
    cluster_for_symbol,
    normalize_cluster_names,
    observation_universe_symbols,
    symbols_in_allowed_clusters,
)
from app.trident.supervisor import TridentSupervisor
from app.trident.types import PodName, RegimeSnapshot, SymbolMarketSnapshot, symbol_market_snapshot_from_mapping


TRIDENT_UI_PODS = ("pod_a", "pod_c")
HIP4_APP_KINDS = {"trident-hip4", "hip4", "hip4-outcome"}
TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}


def _app_kind() -> str:
    return os.getenv("TRIDENT_APP_KIND", "trident").strip().lower()


def _is_hip4_app() -> bool:
    return _app_kind() in HIP4_APP_KINDS


def _env_truthy(name: str) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value == "":
        return False
    return raw_value.strip().lower() in TRUTHY_ENV_VALUES


def _hip4_routes_enabled() -> bool:
    if _is_hip4_app():
        return True
    return _env_truthy("TRIDENT_ENABLE_HIP4_OUTCOME")


def _routing_override_enabled() -> bool:
    return _env_truthy("TRIDENT_ROUTING_OVERRIDE_ENABLED")


def _ui_basic_auth_credentials() -> tuple[str, str] | None:
    username = os.getenv("TRIDENT_UI_AUTH_USERNAME", "").strip()
    password = os.getenv("TRIDENT_UI_AUTH_PASSWORD", "")
    if not username or not password:
        return None
    return username, password


def _parse_basic_auth_header(header_value: str) -> tuple[str, str] | None:
    scheme, separator, token = header_value.partition(" ")
    if separator != " " or scheme.lower() != "basic" or not token.strip():
        return None
    try:
        decoded = base64.b64decode(token.strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    username, separator, password = decoded.partition(":")
    if separator != ":":
        return None
    return username, password


def _basic_auth_matches(
    header_value: str,
    expected_credentials: tuple[str, str],
) -> bool:
    parsed = _parse_basic_auth_header(header_value)
    if parsed is None:
        return False
    expected_username, expected_password = expected_credentials
    username, password = parsed
    username_ok = secrets.compare_digest(username, expected_username)
    password_ok = secrets.compare_digest(password, expected_password)
    return username_ok and password_ok


def _request_path(path: str) -> str:
    return path.split("?", 1)[0]


def _exchange_network_from_url(info_url: object) -> str:
    normalized = str(info_url or "").strip().lower()
    if "hyperliquid-testnet" in normalized or "testnet" in normalized:
        return "testnet"
    return "mainnet"


def _latest_snapshot_status(snapshot_dir: Path = Path("data/live_snapshots")) -> dict[str, object]:
    files = sorted(snapshot_dir.glob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not files:
        return {
            "status": "bad",
            "label": "No snapshots",
            "comment": "Aucun snapshot live trouvé.",
            "age_minutes": None,
            "path": None,
        }
    latest = files[0]
    age_minutes = (datetime.now(timezone.utc).timestamp() - latest.stat().st_mtime) / 60.0
    if age_minutes <= 2.0:
        status = "good"
        label = "Fresh snapshots"
    elif age_minutes <= 10.0:
        status = "warn"
        label = "Snapshots aging"
    else:
        status = "bad"
        label = "Snapshots stale"
    return {
        "status": status,
        "label": label,
        "comment": f"Dernier snapshot il y a {age_minutes:.1f} min dans {latest.name}.",
        "age_minutes": round(age_minutes, 2),
        "path": str(latest),
    }


def _latest_snapshot_status_for_payload(snapshot: dict[str, object]) -> dict[str, object]:
    exchange = snapshot.get("exchange", {})
    if isinstance(exchange, dict) and exchange.get("snapshot_output_dir"):
        return _latest_snapshot_status(Path(str(exchange["snapshot_output_dir"])))
    return _latest_snapshot_status()


def _tail_jsonl_records(
    path: Path,
    *,
    event_type: str | None = None,
    limit: int = 5,
    scan_lines: int = 250,
) -> list[dict[str, object]]:
    if not path.exists():
        return []
    records: list[dict[str, object]] = []
    for raw in reversed(_tail_text_lines(path, max_lines=scan_lines)):
        with contextlib.suppress(json.JSONDecodeError):
            record = json.loads(raw)
            if not isinstance(record, dict):
                continue
            if event_type is not None and record.get("event_type") != event_type:
                continue
            records.append(record)
            if len(records) >= limit:
                break
    return records


def _tail_csv_records(path: Path, *, limit: int = 20) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            header = handle.readline().rstrip("\r\n")
    except (OSError, csv.Error):
        return []
    if not header:
        return []
    tail_lines = _tail_text_lines(path, max_lines=max(limit + 1, 2))
    data_lines = [line for line in tail_lines if line.strip()]
    if data_lines and data_lines[0] == header:
        csv_lines = data_lines
    else:
        csv_lines = [header, *[line for line in data_lines if line != header][-limit:]]
    try:
        rows = list(csv.DictReader(io.StringIO("\n".join(csv_lines) + "\n")))
    except csv.Error:
        return []
    return rows[-limit:]


def _tail_text_lines(
    path: Path,
    *,
    max_lines: int,
    chunk_size: int = 64 * 1024,
) -> list[str]:
    if max_lines <= 0 or not path.exists():
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            position = handle.tell()
            chunks: list[bytes] = []
            newline_count = 0
            while position > 0 and newline_count <= max_lines:
                read_size = min(chunk_size, position)
                position -= read_size
                handle.seek(position)
                chunk = handle.read(read_size)
                chunks.append(chunk)
                newline_count += chunk.count(b"\n")
    except OSError:
        return []
    if not chunks:
        return []
    data = b"".join(reversed(chunks))
    return data.decode("utf-8", errors="replace").splitlines()[-max_lines:]


def _float_or_none(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _execution_fill_statuses(row: dict[str, object]) -> str:
    fills = row.get("fills")
    if not isinstance(fills, list) or not fills:
        return "-"
    statuses: list[str] = []
    for fill in fills:
        if not isinstance(fill, dict):
            continue
        side = str(fill.get("side_name") or fill.get("coin") or "leg")
        status = str(fill.get("status") or "-")
        qty = fill.get("token_qty")
        if qty not in (None, ""):
            statuses.append(f"{side}:{status}:{qty}")
        else:
            statuses.append(f"{side}:{status}")
    return " | ".join(statuses) if statuses else "-"


def _first_float(row: dict[str, object], *keys: str) -> float | None:
    for key in keys:
        value = _float_or_none(row.get(key))
        if value is not None:
            return value
    return None


def _win_counts_from_pnl(value: object) -> tuple[int, int]:
    pnl = _float_or_none(value)
    if pnl is None:
        return 0, 0
    return (1, 0) if pnl >= 0 else (0, 1)


def _win_rate_from_counts(win_count: int, loss_count: int) -> float | None:
    closed_count = int(win_count) + int(loss_count)
    if closed_count <= 0:
        return None
    return round(int(win_count) / closed_count, 4)


def _settlement_fee(row: dict[str, object]) -> float:
    return _first_float(row, "fee_usdc", "fees_usdc") or 0.0


def _settlement_net_pnl(row: dict[str, object]) -> float | None:
    return _first_float(row, "net_pnl_usdc", "pnl_usdc", "estimated_pnl_usdc")


def _settlement_gross_pnl(row: dict[str, object]) -> float | None:
    gross = _first_float(row, "gross_pnl_usdc", "estimated_gross_pnl_usdc")
    if gross is not None:
        return gross
    net = _settlement_net_pnl(row)
    if net is None:
        return None
    return net + _settlement_fee(row)


def _hip4_outcome_monitor_payload(
    *,
    status_path: Path = Path("logs/hip4_outcome_status.json"),
    include_pod_b_alias_report: bool = True,
    default_logs_dir: Path = Path("logs/hip4_outcome_mainnet_paper"),
    inactive_when_status_missing: bool = False,
) -> dict[str, object]:
    status = load_runtime_status(status_path)
    summary = status.get("summary", {}) if isinstance(status, dict) else {}
    if not isinstance(summary, dict):
        summary = {}
    logs_dir = default_logs_dir
    if isinstance(status, dict):
        raw_logs_dir = status.get("logs_dir")
        if isinstance(raw_logs_dir, str) and raw_logs_dir:
            logs_dir = Path(raw_logs_dir)

    opportunities = _tail_csv_records(logs_dir / "opportunities.csv", limit=24)
    edge_decay = _tail_csv_records(logs_dir / "edge_decay.csv", limit=24)
    short_expiry_features = _tail_csv_records(logs_dir / "short_expiry_features.csv", limit=36)
    latency = _tail_csv_records(logs_dir / "latency_stats.csv", limit=12)
    execution_results = _tail_jsonl_records(logs_dir / "execution_results.jsonl", limit=24, scan_lines=1000)
    daily_summary = _tail_csv_records(logs_dir / "daily_summary.csv", limit=24)
    settlements = _tail_csv_records(logs_dir / "settlements.csv", limit=24)
    fills = _tail_csv_records(logs_dir / "trades.csv", limit=24)
    market_observations = _tail_jsonl_records(
        logs_dir / "market_observations.jsonl",
        limit=80,
        scan_lines=4000,
    )
    replay_rows = replay_opportunities(logs_dir / "opportunities.csv")
    all_opportunity_rows = _tail_csv_records(logs_dir / "opportunities.csv", limit=5000)
    all_short_rows = _tail_csv_records(logs_dir / "short_expiry_features.csv", limit=5000)
    all_settlement_rows = _tail_csv_records(logs_dir / "settlements.csv", limit=5000)
    all_fill_rows = _tail_csv_records(logs_dir / "trades.csv", limit=5000)
    pod_b_alias_status = (
        load_runtime_status(Path("logs/pod_b_live_status.json"))
        if include_pod_b_alias_report
        else None
    )
    pod_b_alias_report = {}
    if (
        isinstance(pod_b_alias_status, dict)
        and str(pod_b_alias_status.get("pod_kind", "")).strip().lower() == "hip4_outcome_edge_pod"
        and isinstance(pod_b_alias_status.get("report"), dict)
    ):
        pod_b_alias_report = pod_b_alias_status["report"]
    best_net_edge = None
    latest_net_edge = None
    best_short_net_edge = None
    latest_short_net_edge = None
    if all_opportunity_rows:
        best_net_edge = max(
            (
                value
                for value in (_float_or_none(row.get("net_edge")) for row in all_opportunity_rows)
                if value is not None
            ),
            default=None,
        )
    if opportunities:
        latest_net_edge = _float_or_none(opportunities[-1].get("net_edge"))
    if all_short_rows:
        best_short_net_edge = max(
            (
                value
                for value in (_float_or_none(row.get("best_net_edge")) for row in all_short_rows)
                if value is not None
            ),
            default=None,
        )
    if short_expiry_features:
        latest_short_net_edge = _float_or_none(short_expiry_features[-1].get("best_net_edge"))
    net_pnl_usd = sum(
        value
        for value in (_settlement_net_pnl(row) for row in all_settlement_rows)
        if value is not None
    )
    gross_pnl_usd = sum(
        value
        for value in (_settlement_gross_pnl(row) for row in all_settlement_rows)
        if value is not None
    )
    fees_usd = sum(_settlement_fee(row) for row in all_settlement_rows)
    payout_usdc = sum(
        value
        for value in (_float_or_none(row.get("payout_usdc")) for row in all_settlement_rows)
        if value is not None
    )
    settlement_win_count = 0
    settlement_loss_count = 0
    for row in all_settlement_rows:
        pnl = _settlement_net_pnl(row)
        win_count, loss_count = _win_counts_from_pnl(pnl)
        settlement_win_count += win_count
        settlement_loss_count += loss_count
    effective_report = dict(pod_b_alias_report)
    effective_win_count = int(effective_report.get("win_count", settlement_win_count) or 0)
    effective_loss_count = int(effective_report.get("loss_count", settlement_loss_count) or 0)
    effective_report.setdefault("win_count", effective_win_count)
    effective_report.setdefault("loss_count", effective_loss_count)
    effective_report.setdefault(
        "win_rate",
        _win_rate_from_counts(effective_win_count, effective_loss_count),
    )

    reference_prices = summary.get("reference_prices", {})
    if not isinstance(reference_prices, dict):
        reference_prices = {}
    capital = {}
    if isinstance(status, dict) and isinstance(status.get("capital"), dict):
        capital = status["capital"]
    elif isinstance(summary.get("capital"), dict):
        capital = summary["capital"]
    operator_brief = {}
    if isinstance(status, dict) and isinstance(status.get("operator_brief"), dict):
        operator_brief = status["operator_brief"]
    elif isinstance(summary.get("operator_brief"), dict):
        operator_brief = summary["operator_brief"]
    short_expiry_watchlist: list[object] = []
    if isinstance(status, dict) and isinstance(status.get("short_expiry_watchlist"), list):
        short_expiry_watchlist = status["short_expiry_watchlist"]
    elif isinstance(summary.get("short_expiry_watchlist"), list):
        short_expiry_watchlist = summary["short_expiry_watchlist"]
    decision_reasons = summary.get("decision_reasons", {})
    if not isinstance(decision_reasons, dict):
        decision_reasons = {}
    opportunity_mix = summary.get("opportunity_mix", {})
    if not isinstance(opportunity_mix, dict):
        opportunity_mix = {}
    blocked_opportunity_slices: list[object] = []
    if (
        isinstance(pod_b_alias_status, dict)
        and isinstance(pod_b_alias_status.get("blocked_opportunity_slices"), list)
    ):
        blocked_opportunity_slices = pod_b_alias_status["blocked_opportunity_slices"]
    elif (
        isinstance(status, dict)
        and isinstance(status.get("blocked_opportunity_slices"), list)
    ):
        blocked_opportunity_slices = status["blocked_opportunity_slices"]
    reference_divergence_guard: dict[str, object] = {}
    if (
        isinstance(pod_b_alias_status, dict)
        and isinstance(pod_b_alias_status.get("reference_divergence_guard"), dict)
    ):
        reference_divergence_guard = pod_b_alias_status["reference_divergence_guard"]
    elif (
        isinstance(status, dict)
        and isinstance(status.get("reference_divergence_guard"), dict)
    ):
        reference_divergence_guard = status["reference_divergence_guard"]
    reference_rows: list[dict[str, object]] = []
    for underlying, reference in sorted(reference_prices.items()):
        if not isinstance(reference, dict):
            continue
        rejected = reference.get("reference_rejected_sources", [])
        reference_rows.append(
            {
                "underlying": str(underlying),
                "price": reference.get("reference_price"),
                "source_count": reference.get("reference_source_count", 0),
                "rejected_count": len(rejected) if isinstance(rejected, list) else 0,
                "max_deviation_bps": reference.get("reference_max_deviation_bps"),
            }
        )

    status_age = runtime_status_age_seconds(status)
    fresh = runtime_status_is_fresh(status)
    status_missing = status is None
    market_observation_health = _hip4_observation_health(
        market_observations,
        loop_summary=summary.get("market_observation"),
        fresh=fresh,
        inactive=inactive_when_status_missing and status_missing,
    )
    return {
        "pod": "hip4_outcome_edge_pod",
        "status_path": str(status_path),
        "logs_dir": str(logs_dir),
        "status": status,
        "summary": summary,
        "fresh": fresh,
        "status_age_seconds": status_age,
        "process_state": (
            str(status.get("process_state"))
            if isinstance(status, dict) and status.get("process_state") is not None
            else "off"
            if inactive_when_status_missing and status_missing
            else "running"
            if fresh
            else "missing"
            if status_missing
            else "stale"
        ),
        "mode": str(summary.get("mode") or (status.get("mode") if isinstance(status, dict) else "observer")),
        "markets_seen": int(summary.get("markets_seen", 0) or 0),
        "markets_supported": int(summary.get("markets_supported", 0) or 0),
        "open_positions": int(summary.get("open_positions", 0) or 0),
        "opportunities_this_loop": int(summary.get("opportunities", 0) or 0),
        "approved_this_loop": int(summary.get("approved", 0) or 0),
        "executed_this_loop": int(summary.get("executed", 0) or 0),
        "report": effective_report,
        "settled_position_count": int(
            effective_report.get("closed_trade_count", len(all_settlement_rows)) or 0
        ),
        "fill_count": int(effective_report.get("total_fill_count", len(all_fill_rows)) or 0),
        "realized_pnl_usd": float(
            effective_report.get("realized_pnl_usd", round(net_pnl_usd, 8)) or 0.0
        ),
        "gross_pnl_usd": float(
            effective_report.get("gross_pnl_usd", round(gross_pnl_usd, 8)) or 0.0
        ),
        "fees_usd": float(effective_report.get("fees_usd", round(fees_usd, 8)) or 0.0),
        "settlement_payout_usdc": float(
            effective_report.get("settlement_payout_usdc", round(payout_usdc, 8)) or 0.0
        ),
        "fee_model": (
            pod_b_alias_status.get("fee_model")
            if isinstance(pod_b_alias_status, dict) and isinstance(pod_b_alias_status.get("fee_model"), dict)
            else status.get("fee_model")
            if isinstance(status, dict) and isinstance(status.get("fee_model"), dict)
            else {}
        ),
        "blocked_opportunity_slices": blocked_opportunity_slices,
        "reference_divergence_guard": reference_divergence_guard,
        "best_net_edge": best_net_edge,
        "latest_net_edge": latest_net_edge,
        "best_short_net_edge": best_short_net_edge,
        "latest_short_net_edge": latest_short_net_edge,
        "short_expiry_brief": operator_brief,
        "short_expiry_watchlist": short_expiry_watchlist,
        "decision_reasons": decision_reasons,
        "opportunity_mix": opportunity_mix,
        "reference_prices": reference_rows,
        "capital": capital,
        "opportunities": opportunities,
        "short_expiry_features": short_expiry_features,
        "edge_decay": edge_decay,
        "latency": latency,
        "execution_results": execution_results,
        "last_execution_results": (
            status.get("last_execution_results")
            if isinstance(status, dict) and isinstance(status.get("last_execution_results"), list)
            else []
        ),
        "daily_summary": daily_summary,
        "settlements": settlements,
        "fills": fills,
        "replay": replay_rows,
        "market_observations": market_observations,
        "market_observation_health": market_observation_health,
    }


def _hip4_observation_health(
    rows: list[dict[str, object]],
    *,
    loop_summary: object = None,
    fresh: bool = True,
    inactive: bool = False,
) -> dict[str, object]:
    tones = Counter(_hip4_observation_row_tone(row)["tone"] for row in rows)
    classes = Counter(str(row.get("class_name") or "unknown") for row in rows)
    supports = Counter(str(row.get("support_status") or "unknown") for row in rows)
    reasons = Counter(str(row.get("support_reason") or "unspecified") for row in rows)
    book_errors = sum(1 for row in rows if _hip4_observation_has_book_error(row))
    books_logged = sum(1 for row in rows if _hip4_observation_has_books(row))
    total = len(rows)
    unknown_count = classes.get("unknown", 0)
    named_count = classes.get("namedOutcome", 0)
    price_bucket_count = classes.get("priceBucket", 0)
    price_binary_count = classes.get("priceBinary", 0)
    latest_ts = max((str(row.get("ts", "")) for row in rows if row.get("ts")), default="")
    loop_total = None
    if isinstance(loop_summary, dict):
        loop_total = loop_summary.get("total")
    if inactive:
        tone = "neutral"
        label = "off"
        reason = "observer mainnet optionnel désactivé ou status absent"
    elif not fresh:
        tone = "bad"
        label = "stale"
        reason = "status runtime trop ancien"
    elif total <= 0 and not loop_total:
        tone = "bad"
        label = "aucune observation"
        reason = "market_observations.jsonl vide ou absent"
    elif book_errors > 0:
        tone = "bad"
        label = "à investiguer"
        reason = "erreurs book observées"
    elif unknown_count > 0:
        tone = "warn"
        label = "watch-only"
        reason = "marchés non supportés observés"
    elif tones.get("warn", 0) > 0:
        tone = "warn"
        label = "watch-only"
        reason = "observations connues mais pas toutes exploitables"
    else:
        tone = "good"
        label = "bonne"
        reason = "classes reconnues et observations lisibles"
    return {
        "tone": tone,
        "label": label,
        "reason": reason,
        "count": total,
        "latest_ts": latest_ts,
        "books_logged_count": books_logged,
        "book_error_count": book_errors,
        "unknown_count": unknown_count,
        "named_outcome_count": named_count,
        "price_bucket_count": price_bucket_count,
        "price_binary_count": price_binary_count,
        "by_tone": _counter_dict(tones),
        "by_class": _counter_dict(classes),
        "by_support_status": _counter_dict(supports),
        "support_reasons": _counter_dict(reasons),
    }


def _hip4_observation_row_tone(row: dict[str, object]) -> dict[str, str]:
    class_name = str(row.get("class_name") or "unknown")
    support = str(row.get("support_status") or "unknown")
    reason = str(row.get("support_reason") or "")
    if _hip4_observation_has_book_error(row):
        return {"tone": "bad", "label": "book error"}
    if class_name == "unknown" or reason == "unsupported_outcome_class":
        return {"tone": "warn", "label": "unsupported"}
    if support == "trading_supported":
        return {"tone": "good", "label": "supported"}
    if class_name == "priceBucket":
        if (
            support == "paper_supported"
            and _float_or_none(row.get("bucket_lower")) is not None
            and _float_or_none(row.get("bucket_upper")) is not None
        ):
            return {"tone": "good", "label": "paper ok"}
        return {"tone": "warn", "label": "bucket incomplet"}
    if class_name == "namedOutcome":
        return {"tone": "warn", "label": "vote/watch"}
    if class_name == "fallback":
        return {"tone": "warn", "label": "fallback"}
    if support == "observe_only":
        return {"tone": "warn", "label": "observe"}
    return {"tone": "warn", "label": support or "watch"}


def _hip4_observation_has_books(row: dict[str, object]) -> bool:
    books = row.get("books")
    if not isinstance(books, dict) or not books:
        return False
    return any(isinstance(books.get(side), dict) for side in ("yes", "no"))


def _hip4_observation_has_book_error(row: dict[str, object]) -> bool:
    books = row.get("books")
    if not isinstance(books, dict):
        return False
    for side in ("yes", "no"):
        book = books.get(side)
        if isinstance(book, dict) and book.get("error"):
            return True
    return False


def _counter_dict(counter: Counter[str]) -> dict[str, int]:
    return {key: int(value) for key, value in sorted(counter.items())}


def _format_count_map(value: object) -> str:
    if not isinstance(value, dict) or not value:
        return "-"
    return " · ".join(
        f"{key}:{value[key]}"
        for key in sorted(value)
    )


def _format_observation_list(value: object, *, limit: int = 4) -> str:
    if not isinstance(value, list) or not value:
        return "-"
    items = [str(item) for item in value[:limit]]
    suffix = "" if len(value) <= limit else f" +{len(value) - limit}"
    return ", ".join(items) + suffix


def _format_observation_bucket(row: dict[str, object]) -> str:
    lower = _float_or_none(row.get("bucket_lower"))
    upper = _float_or_none(row.get("bucket_upper"))
    if lower is not None and upper is not None:
        return f"{lower:g} - {upper:g}"
    thresholds = row.get("thresholds")
    if isinstance(thresholds, list) and thresholds:
        return _format_observation_list(thresholds, limit=6)
    return "-"


def _format_observation_books(row: dict[str, object]) -> str:
    books = row.get("books")
    if not isinstance(books, dict) or not books:
        return "-"
    chunks: list[str] = []
    for side in ("yes", "no"):
        book = books.get(side)
        if not isinstance(book, dict):
            continue
        error = book.get("error")
        if error:
            chunks.append(f"{side}:error")
            continue
        bid = _float_or_none(book.get("bid"))
        ask = _float_or_none(book.get("ask"))
        if bid is None and ask is None:
            chunks.append(f"{side}:empty")
        else:
            chunks.append(f"{side}:{'-' if bid is None else f'{bid:.4f}'}/{ '-' if ask is None else f'{ask:.4f}'}")
    return " · ".join(chunks) if chunks else "-"


def _recent_activity_rows(snapshot: dict[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for pod_name, log_name in (
        ("pod_a", "pod_a_live.jsonl"),
        ("pod_b", "pod_b_live.jsonl"),
        ("pod_c", "pod_c_live.jsonl"),
    ):
        for record in _tail_jsonl_records(Path("logs") / log_name, event_type="trade_close", limit=4):
            trade = record.get("trade", {})
            if not isinstance(trade, dict):
                continue
            rows.append(
                {
                    "timestamp": str(record.get("timestamp") or trade.get("closed_at") or "-"),
                    "pod": pod_name,
                    "symbol": str(trade.get("symbol", "-")),
                    "side": str(trade.get("side", "-")),
                    "event": "trade_close",
                    "price": trade.get("exit_price"),
                    "notional_usd": trade.get("target_notional_usd"),
                    "leverage": trade.get("leverage"),
                    "pnl_usd": trade.get("pnl_usd"),
                    "comment": str(trade.get("close_reason", "-")),
                }
            )

    rows.sort(key=lambda item: str(item.get("timestamp", "")), reverse=True)
    return rows[:10]


def _status_badge(status: str, label: str) -> str:
    return f'<span class="badge badge-{escape(status)}">{escape(label)}</span>'


def _display_process_state(value: object) -> str:
    process_state = str(value or "-")
    if process_state == "supervisor_fallback":
        return "Supervisor fallback"
    return process_state


def _cluster_display_name(cluster: str) -> str:
    normalized = str(cluster).strip().lower()
    labels = {
        "crypto": "Crypto",
        "index": "Index",
        "gold": "Gold",
        "silver": "Silver",
        "equity": "Equity",
        "oil": "Oil",
        "fx": "FX",
    }
    return labels.get(normalized, normalized.replace("_", " ").title() or "-")


def _table_header(label: str, tooltip: str) -> str:
    return (
        "<th>"
        "<span class='th-with-tooltip'>"
        f"<span>{escape(label)}</span>"
        "<button class='tooltip-trigger' type='button' aria-label='Afficher l’aide'>i</button>"
        f"<span class='tooltip-bubble'>{escape(tooltip)}</span>"
        "</span>"
        "</th>"
    )


def _format_leverage(value: object) -> str:
    if value in (None, "", 0, 0.0):
        return "-"
    try:
        return f"{float(value):.1f}x"
    except (TypeError, ValueError):
        return escape(str(value))


def _directional_price_from_bps(entry_price: object, side: object, bps: object) -> float | None:
    try:
        entry = float(entry_price)
        distance_bps = float(bps)
    except (TypeError, ValueError):
        return None
    if entry <= 0 or distance_bps <= 0:
        return None
    ratio = distance_bps / 10_000.0
    if str(side) == "short":
        return round(entry * (1 - ratio), 8)
    return round(entry * (1 + ratio), 8)


def _directional_stop_price(item: dict[str, object]) -> float | None:
    invalidation = item.get("invalidation_price")
    if invalidation not in (None, ""):
        try:
            return round(float(invalidation), 8)
        except (TypeError, ValueError):
            pass
    return _directional_price_from_bps(
        item.get("entry_price"),
        "short" if str(item.get("side")) == "long" else "long",
        item.get("stop_bps"),
    )


def _directional_take_profit_price(item: dict[str, object]) -> float | None:
    return _directional_price_from_bps(
        item.get("entry_price"),
        item.get("side"),
        item.get("take_profit_bps"),
    )


def _directional_favorable_move_bps(entry_price: object, side: object, price: object) -> float | None:
    try:
        entry = float(entry_price)
        current = float(price)
    except (TypeError, ValueError):
        return None
    if entry <= 0:
        return None
    if str(side) == "short":
        return ((entry - current) / entry) * 10_000.0
    return ((current - entry) / entry) * 10_000.0


def _directional_trailing_stop_price(item: dict[str, object]) -> float | None:
    best_price_seen = item.get("best_price_seen")
    trailing_distance_bps = item.get("trailing_distance_bps")
    try:
        reference = float(best_price_seen)
        distance_bps = float(trailing_distance_bps)
    except (TypeError, ValueError):
        return None
    if reference <= 0 or distance_bps <= 0:
        return None
    ratio = distance_bps / 10_000.0
    if str(item.get("side")) == "short":
        return round(reference * (1 + ratio), 8)
    return round(reference * (1 - ratio), 8)


def _directional_trailing_status(item: dict[str, object]) -> str:
    activation_bps = item.get("trailing_activation_bps")
    trailing_distance_bps = item.get("trailing_distance_bps")
    if activation_bps in (None, "", 0, 0.0) or trailing_distance_bps in (None, "", 0, 0.0):
        return "Non configure"
    best_favorable_bps = _directional_favorable_move_bps(
        item.get("entry_price"),
        item.get("side"),
        item.get("best_price_seen"),
    )
    try:
        activation = float(activation_bps)
        distance = float(trailing_distance_bps)
    except (TypeError, ValueError):
        return "Non configure"
    if best_favorable_bps is None or best_favorable_bps < activation:
        return f"En attente, activation apres +{activation:.1f} bps"
    trailing_price = _directional_trailing_stop_price(item)
    if trailing_price is None:
        return f"Actif, distance {distance:.1f} bps"
    return f"Actif, stop suiveur {trailing_price:.6f}"


def _humanize_setup_reason(value: object) -> str:
    reason = str(value or "").strip()
    mapping = {
        "bos_retest_long": "BOS retest long: reprise haussiere apres retest du breakout",
        "bos_retest_short": "BOS retest short: reprise baissiere apres retest du breakout",
        "liquidity_sweep_reclaim_long": "Liquidity sweep reclaim long: reprise haussiere apres chasse de liquidite",
        "liquidity_sweep_reclaim_short": "Liquidity sweep reclaim short: reprise baissiere apres chasse de liquidite",
        "vwap_reclaim_long": "VWAP reclaim long: reprise haussiere apres recuperation de la VWAP",
        "vwap_reclaim_short": "VWAP reclaim short: reprise baissiere apres rejet sous la VWAP",
        "trend_pullback_long": "Trend pullback long: achat de repli dans une tendance haussiere",
        "trend_pullback_short": "Trend pullback short: vente de rebond dans une tendance baissiere",
        "tradfi_continuation_long": "Tradfi continuation long: continuation haussiere sur instrument Tradfi",
        "tradfi_continuation_short": "Tradfi continuation short: continuation baissiere sur instrument Tradfi",
        "tradfi_reclaim_long": "Tradfi reclaim long: reprise haussiere apres recapture du niveau cle",
        "tradfi_reclaim_short": "Tradfi reclaim short: reprise baissiere apres rejet sous le niveau cle",
    }
    return mapping.get(reason, reason or "-")


def _humanize_close_reason(value: object) -> str:
    reason = str(value or "").strip()
    mapping = {
        "take_profit_hit": "Take profit atteint: la cible de gain fixe a ete touchee",
        "trailing_stop": "Trailing stop declenche: le trade avait avance puis a retrace",
        "break_even_stop": "Sortie break-even: la protection est remontee a zero apres avance favorable",
        "stop_hit": "Stop loss touche: l'invalidation du trade a ete atteinte",
        "time_stop": "Time stop atteint: duree maximale de detention depassee",
        "routing_revoked": "Routing revoke: le symbole n'etait plus autorise pour ce pod",
        "opposite_signal": "Signal oppose: un signal inverse a force la fermeture",
        "upgrade_setup": "Upgrade setup: fermeture pour reouvrir sur un setup juge meilleur",
        "exchange_closed": "Cloture exchange: position fermee cote exchange, raison non classee",
        "exchange_closed_stop_loss": "Stop loss exchange declenche: ordre protecteur SL execute",
        "exchange_closed_take_profit": "Take profit exchange declenche: ordre protecteur TP execute",
        "exchange_closed_liquidation": "Liquidation exchange detectee: cloture cote exchange",
        "end_of_backtest": "Fin de replay: cloture technique de fin de session",
    }
    return mapping.get(reason, reason or "-")


def _reason_code(value: object) -> str:
    return str(value or "").strip().split(":", 1)[0]


def _reason_detail(value: object) -> str:
    raw = str(value or "").strip()
    if ":" not in raw:
        return ""
    return raw.split(":", 1)[1].strip()


def _humanize_opportunity_reason(value: object) -> str:
    code = _reason_code(value)
    mapping = {
        "opened": "Ordre ouvert",
        "accepted": "Accepté risk",
        "accepted_no_open": "Accepté, pas encore ouvert",
        "filtered": "Filtre stratégie",
        "shadow_blocked_by_routing": "Bloqué routing shadow",
        "missing_trade_plan": "Plan de trade absent",
        "confidence_below_min": "Confiance trop basse",
        "notional_below_min": "Notional trop petit",
        "batch_limit_reached": "Limite batch atteinte",
        "duplicate_symbol": "Symbole déjà candidat",
        "symbol_blocked": "Symbole bloqué",
        "setup_not_allowed": "Setup non autorisé",
        "setup_disabled": "Setup désactivé",
        "rolling_guardrail_intraday_setup": "Guardrail intraday setup",
        "rolling_guardrail_setup": "Guardrail setup",
        "rolling_guardrail_symbol_setup": "Guardrail symbole/setup",
        "regime_filtered": "Régime filtré",
        "symbol_mode_setup_filtered": "Filtre mode symbole/setup",
        "symbol_mode_regime_filtered": "Filtre mode symbole/régime",
        "symbol_mode_confidence_below_min": "Confiance trop basse pour ce symbole",
        "margin_below_min": "Marge trop petite",
        "leverage_above_asset_limit": "Levier au-dessus limite actif",
        "leverage_above_limit": "Levier au-dessus limite",
        "risk_budget_exceeded": "Budget risque dépassé",
        "total_open_risk_exceeded": "Risque ouvert total dépassé",
        "entry_not_allowed_by_routing": "Entrée non autorisée par routing",
        "reentry_cooldown_active": "Cooldown de réentrée actif",
        "scale_in_open_fill_rejected": "Scale-in refusé à l'ouverture",
        "portfolio_scale_in_rejected": "Scale-in refusé portefeuille",
        "scale_in_not_allowed": "Scale-in non autorisé",
        "upgrade_close_fill_rejected": "Fermeture upgrade refusée",
        "upgrade_close_partial_fill": "Fermeture upgrade partielle",
        "open_fill_rejected": "Ouverture refusée par venue",
        "portfolio_open_rejected": "Ouverture refusée portefeuille",
        "invalid_notional_or_price": "Notional ou prix invalide",
        "notional_above_live_cap": "Notional au-dessus cap live",
        "stop_grace_exchange_sl_mismatch": "Blocage SL grace live",
        "exchange_position_or_order_exists": "Exposition exchange déjà présente",
        "size_rounds_to_zero": "Taille arrondie à zéro",
        "post_only_size_rounds_to_zero": "Post-only arrondi à zéro",
        "entry_order_resting_post_only": "Ordre post-only en attente",
        "asset_not_resolved": "Actif exchange non résolu",
        "execution_skipped": "Accepté puis non ouvert",
    }
    if code.startswith("pattern_veto_"):
        return f"Pattern veto {code.removeprefix('pattern_veto_')}"
    return mapping.get(code, str(value or "-"))


def _opportunity_reason_tooltip(value: object) -> str:
    code = _reason_code(value)
    detail = _reason_detail(value)
    mapping = {
        "opened": "Le signal a passé le risk gate et une ouverture a été enregistrée dans les fills du pod.",
        "accepted": "Le risk gate a accepté le plan: confiance, notional, marge, levier et budget risque passent les limites.",
        "accepted_no_open": "Le plan est accepté par le risk gate, mais aucun fill d'ouverture n'est visible dans ce record de journal.",
        "filtered": "Le superviseur ou la stratégie a filtré le candidat avant la création d'un plan de trade exécutable.",
        "shadow_blocked_by_routing": "Le signal existe en shadow, mais le routing courant ne donne pas le symbole à ce pod pour l'ouverture.",
        "missing_trade_plan": "Une preview de signal existe, mais aucun TradePlan correspondant n'a été construit pour le risk gate.",
        "confidence_below_min": "La confiance du setup est sous le minimum configuré, donc le risk gate refuse le trade avant sizing.",
        "notional_below_min": "Le notional calculé est sous le minimum autorisé; augmenter la taille peut parfois faire passer ce filtre.",
        "batch_limit_reached": "Le nombre maximal de plans acceptés dans cette boucle est déjà atteint.",
        "duplicate_symbol": "Un autre plan sur le même symbole est déjà présent dans cette boucle.",
        "symbol_blocked": "Le symbole figure dans la liste de blocage du pod.",
        "setup_not_allowed": "Le setup n'est pas dans la liste des setups autorisés pour ce pod ou ce mode de symbole.",
        "setup_disabled": "Le setup est explicitement désactivé dans la config active.",
        "rolling_guardrail_intraday_setup": "Le setup est coupé pour la journée car son historique intraday récent a dépassé la perte tolérée.",
        "rolling_guardrail_setup": "Le setup est coupé car ses derniers trades visibles déclenchent le guardrail de performance.",
        "rolling_guardrail_symbol_setup": "La paire symbole/setup est coupée car ses derniers trades visibles déclenchent le guardrail.",
        "regime_filtered": "Le régime de marché courant est bloqué pour ce setup.",
        "symbol_mode_setup_filtered": "Le mode spécifique du symbole autorise seulement certains setups, et celui-ci n'en fait pas partie.",
        "symbol_mode_regime_filtered": "Le mode spécifique du symbole n'autorise pas le régime courant.",
        "symbol_mode_confidence_below_min": "Le symbole a un seuil de confiance local plus élevé que le seuil global.",
        "margin_below_min": "La marge calculée est sous la marge minimale du pod.",
        "leverage_above_asset_limit": "Le levier effectif dépasse la limite propre à cet actif.",
        "leverage_above_limit": "Le levier effectif dépasse la limite globale configurée.",
        "risk_budget_exceeded": "La perte attendue du plan dépasse le budget risque attribué à ce trade.",
        "total_open_risk_exceeded": "Ajouter ce trade ferait dépasser le risque ouvert total autorisé.",
        "entry_not_allowed_by_routing": "Le superviseur ne permet pas à ce pod d'ouvrir ce symbole dans la boucle courante, même si le signal existe.",
        "reentry_cooldown_active": "Le symbole vient d'être tradé et la fenêtre de cooldown interdit encore une réentrée.",
        "scale_in_open_fill_rejected": "Le pod voulait ajouter à une position existante, mais l'ouverture additionnelle n'a pas été fillée.",
        "portfolio_scale_in_rejected": "Le fill existe, mais l'état portefeuille local a refusé de l'ajouter à la position.",
        "scale_in_not_allowed": "Une position existe déjà et les conditions de scale-in ne sont pas réunies.",
        "upgrade_close_fill_rejected": "Le pod voulait fermer pour rouvrir un meilleur setup, mais la fermeture d'abord n'a pas été fillée.",
        "upgrade_close_partial_fill": "La fermeture d'upgrade n'a rempli qu'une partie de la position, donc la réouverture est bloquée.",
        "open_fill_rejected": "Le plan a passé le risk gate, mais la venue d'exécution n'a pas retourné de fill d'ouverture.",
        "portfolio_open_rejected": "Le fill existe, mais l'état portefeuille local a refusé d'enregistrer la position.",
        "invalid_notional_or_price": "Le prix ou le notional envoyé à la venue est nul ou invalide.",
        "notional_above_live_cap": "Le notional demandé dépasse live_max_order_notional_usd; le live bloque l'ordre pour éviter une taille excessive.",
        "stop_grace_exchange_sl_mismatch": "Ancien guardrail live: le setup était bloqué car le backtest ignore le SL pendant la grace, alors qu'un SL exchange immédiat pouvait sortir trop tôt. Avec le correctif, le live place un SL catastrophe puis rafraîchit le SL normal après la grace.",
        "exchange_position_or_order_exists": "Hyperliquid montre déjà une position ou un ordre ouvert sur ce symbole, donc le pod évite une double exposition.",
        "size_rounds_to_zero": "Après conversion notional/prix et arrondi aux décimales Hyperliquid, la taille d'ordre devient zéro.",
        "post_only_size_rounds_to_zero": "Le retry post-only calcule une taille trop petite après arrondi.",
        "entry_order_resting_post_only": "L'ordre post-only est resté dans le carnet sans fill immédiat; le pod le suit comme entrée pending.",
        "asset_not_resolved": "Le symbole n'a pas pu être converti vers l'identifiant exchange attendu par Hyperliquid.",
        "execution_skipped": "Le risk gate a accepté le plan, mais l'exécuteur l'a marqué skipped sans cause plus précise dans cet ancien journal.",
    }
    tooltip = mapping.get(code)
    if tooltip is None:
        if code.startswith("pattern_veto_"):
            tooltip = "Une règle pattern_veto configurée a reconnu un contexte de marché à éviter pour ce plan."
        else:
            tooltip = "Cause brute remontée par le pod; elle n'a pas encore de description dédiée dans le dashboard."
    if detail:
        tooltip = f"{tooltip} Détail runtime: {detail}."
    return tooltip


def _tooltip_value(label: object, tooltip: object) -> str:
    return (
        "<span class='th-with-tooltip'>"
        f"<span>{escape(str(label or '-'))}</span>"
        "<button class='tooltip-trigger' type='button' aria-label='Afficher le détail'>i</button>"
        f"<span class='tooltip-bubble'>{escape(str(tooltip or '-'))}</span>"
        "</span>"
    )


def _opportunity_status_and_reason(signal: dict[str, object]) -> tuple[str, str]:
    risk = signal.get("risk", {})
    if not isinstance(risk, dict):
        risk = {}
    execution = signal.get("execution", {})
    if not isinstance(execution, dict):
        execution = {}
    if bool(execution.get("opened")):
        return "opened", "opened"
    if bool(execution.get("skipped_open")):
        return "skipped", str(execution.get("skip_reason") or "execution_skipped")
    if bool(risk.get("accepted")):
        return "accepted", str(risk.get("reason") or "accepted_no_open")
    return "rejected", str(risk.get("reason") or "missing_trade_plan")


def _opportunity_status_label(status: str) -> str:
    return {
        "opened": "Acceptée + ouverte",
        "accepted": "Acceptée risk",
        "skipped": "Acceptée, bloquée",
        "rejected": "Refusée risk",
        "filtered": "Filtrée",
        "shadow": "Bloquée shadow",
        "preview": "Preview",
    }.get(status, status or "-")


def _opportunity_status_tone(status: str) -> str:
    return {
        "opened": "good",
        "accepted": "good",
        "skipped": "warn",
        "rejected": "bad",
        "filtered": "warn",
        "shadow": "warn",
        "preview": "neutral",
    }.get(status, "neutral")


def _opportunity_detail(
    *,
    signal: dict[str, object] | None = None,
    review: dict[str, object] | None = None,
    risk: dict[str, object] | None = None,
    execution: dict[str, object] | None = None,
    row: dict[str, object] | None = None,
) -> str:
    parts: list[str] = []
    if signal is not None:
        summary = str(signal.get("reason_summary") or "").strip()
        if summary:
            parts.append(summary)
    if review is not None:
        summary = str(review.get("reason_summary") or review.get("reason") or "").strip()
        if summary:
            parts.append(summary)
    if risk is not None:
        target = _float_or_none(risk.get("target_notional_usd"))
        margin = _float_or_none(risk.get("margin_usd"))
        leverage = _float_or_none(risk.get("effective_leverage"))
        expected_loss = _float_or_none(risk.get("expected_loss_usd"))
        if target is not None:
            parts.append(f"notional {target:.2f} USD")
        if margin is not None:
            parts.append(f"marge {margin:.2f} USD")
        if leverage is not None:
            parts.append(f"levier {leverage:.1f}x")
        if expected_loss is not None:
            parts.append(f"perte attendue {expected_loss:.2f} USD")
    if row is not None:
        stop_price = _float_or_none(row.get("stop_price"))
        take_profit = _float_or_none(row.get("take_profit_price"))
        if stop_price is not None:
            parts.append(f"SL {stop_price:.6f}")
        if take_profit is not None:
            parts.append(f"TP {take_profit:.6f}")
    if execution is not None:
        open_fills = execution.get("open_fills")
        close_fills = execution.get("close_fills")
        if isinstance(open_fills, list) and open_fills:
            parts.append(f"{len(open_fills)} fill(s) open")
        if isinstance(close_fills, list) and close_fills:
            parts.append(f"{len(close_fills)} fill(s) close")
    return " · ".join(parts) if parts else "-"


def _recent_directional_opportunity_rows(
    snapshot: dict[str, object],
    *,
    pod: str,
    limit: int = 24,
) -> list[dict[str, object]]:
    journal_name = {
        "pod_a": "pod_a_live.jsonl",
        "pod_c": "pod_c_live.jsonl",
    }.get(pod)
    if journal_name is None:
        return []
    rows: list[dict[str, object]] = []
    seen: set[tuple[str, str, str, str]] = set()
    records = _tail_jsonl_records(
        Path("logs") / journal_name,
        limit=max(limit * 6, 60),
        scan_lines=5000,
    )
    for record in records:
        event_type = str(record.get("event_type") or "")
        if event_type == "signal":
            signal = record.get("signal")
            if not isinstance(signal, dict):
                continue
            risk = signal.get("risk", {})
            if not isinstance(risk, dict):
                risk = {}
            execution = signal.get("execution", {})
            if not isinstance(execution, dict):
                execution = {}
            symbol_snapshot = record.get("symbol_snapshot", {})
            if not isinstance(symbol_snapshot, dict):
                symbol_snapshot = {}
            status, reason = _opportunity_status_and_reason(signal)
            row: dict[str, object] = {
                "timestamp": str(record.get("timestamp") or "-"),
                "pod": pod,
                "symbol": str(signal.get("symbol") or symbol_snapshot.get("symbol") or "-"),
                "side": str(signal.get("side") or "-"),
                "setup": str(signal.get("setup") or "-"),
                "confidence": signal.get("confidence"),
                "status": status,
                "status_label": _opportunity_status_label(status),
                "tone": _opportunity_status_tone(status),
                "reason": reason,
                "cause_label": _humanize_opportunity_reason(reason),
                "cause_tooltip": _opportunity_reason_tooltip(reason),
                "entry_price": symbol_snapshot.get("price") or risk.get("entry_price"),
                "current_price": symbol_snapshot.get("price"),
                "target_notional_usd": risk.get("target_notional_usd"),
                "margin_usd": risk.get("margin_usd"),
                "effective_leverage": risk.get("effective_leverage"),
                "invalidation_price": risk.get("invalidation_price"),
                "stop_bps": risk.get("stop_bps"),
                "take_profit_bps": risk.get("take_profit_bps"),
            }
            row["stop_price"] = _directional_stop_price(row)
            row["take_profit_price"] = _directional_take_profit_price(row)
            row["detail"] = _opportunity_detail(
                signal=signal,
                risk=risk,
                execution=execution,
                row=row,
            )
        elif event_type == "signal_review":
            review = record.get("review")
            if not isinstance(review, dict):
                continue
            status_raw = str(review.get("status") or "filtered")
            status = "shadow" if status_raw == "shadow_blocked_by_routing" else "filtered"
            symbol_snapshot = record.get("symbol_snapshot", {})
            if not isinstance(symbol_snapshot, dict):
                symbol_snapshot = {}
            reason = str(review.get("reason") or review.get("reason_summary") or status_raw)
            row = {
                "timestamp": str(record.get("timestamp") or "-"),
                "pod": pod,
                "symbol": str(review.get("symbol") or symbol_snapshot.get("symbol") or "-"),
                "side": str(review.get("preferred_side") or review.get("side") or "-"),
                "setup": str(review.get("setup") or "-"),
                "confidence": review.get("confidence"),
                "status": status,
                "status_label": _opportunity_status_label(status),
                "tone": _opportunity_status_tone(status),
                "reason": reason,
                "cause_label": _humanize_opportunity_reason(status_raw),
                "cause_tooltip": _opportunity_reason_tooltip(status_raw),
                "entry_price": symbol_snapshot.get("price"),
                "current_price": symbol_snapshot.get("price"),
                "target_notional_usd": None,
                "margin_usd": None,
                "effective_leverage": None,
                "invalidation_price": None,
                "stop_bps": None,
                "take_profit_bps": None,
                "stop_price": None,
                "take_profit_price": None,
                "detail": _opportunity_detail(review=review),
            }
        else:
            continue
        key = (
            str(row.get("timestamp")),
            str(row.get("symbol")),
            str(row.get("status")),
            str(row.get("reason")),
        )
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
        if len(rows) >= limit:
            break

    if rows:
        return rows

    preview_key = f"{pod}_signal_preview"
    review_key = f"{pod}_signal_review"
    review_items = snapshot.get(review_key, [])
    if not isinstance(review_items, list):
        review_items = []
    for item in review_items:
        if not isinstance(item, dict):
            continue
        status_raw = str(item.get("status") or "filtered")
        if status_raw not in {"filtered", "shadow_blocked_by_routing"}:
            continue
        status = "shadow" if status_raw == "shadow_blocked_by_routing" else "filtered"
        rows.append(
            {
                "timestamp": "-",
                "pod": pod,
                "symbol": str(item.get("symbol") or "-"),
                "side": str(item.get("preferred_side") or item.get("side") or "-"),
                "setup": str(item.get("setup") or "-"),
                "confidence": item.get("confidence"),
                "status": status,
                "status_label": _opportunity_status_label(status),
                "tone": _opportunity_status_tone(status),
                "reason": str(item.get("reason_summary") or status_raw),
                "cause_label": _humanize_opportunity_reason(status_raw),
                "cause_tooltip": _opportunity_reason_tooltip(status_raw),
                "entry_price": None,
                "current_price": None,
                "target_notional_usd": None,
                "margin_usd": None,
                "effective_leverage": None,
                "stop_price": None,
                "take_profit_price": None,
                "detail": _opportunity_detail(review=item),
            }
        )
    preview_items = snapshot.get(preview_key, [])
    if not isinstance(preview_items, list):
        preview_items = []
    for item in preview_items:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "timestamp": "-",
                "pod": pod,
                "symbol": str(item.get("symbol") or "-"),
                "side": str(item.get("side") or "-"),
                "setup": str(item.get("setup") or "-"),
                "confidence": item.get("confidence"),
                "status": "preview",
                "status_label": _opportunity_status_label("preview"),
                "tone": _opportunity_status_tone("preview"),
                "reason": "preview",
                "cause_label": "Preview stratégie",
                "cause_tooltip": "Signal proposé par la stratégie dans le snapshot runtime courant; il n'a pas encore de verdict risk/exécution dans les journaux live visibles.",
                "entry_price": None,
                "current_price": None,
                "target_notional_usd": None,
                "margin_usd": None,
                "effective_leverage": None,
                "stop_price": None,
                "take_profit_price": None,
                "detail": _opportunity_detail(signal=item),
            }
        )
    return rows[:limit]


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    with contextlib.suppress(ValueError):
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _format_duration_compact(total_seconds: float) -> str:
    seconds = max(int(total_seconds), 0)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days > 0:
        return f"{days}j {hours}h {minutes}m"
    if hours > 0:
        return f"{hours}h {minutes:02d}m"
    if minutes > 0:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _panel_tone(tone: object) -> str:
    value = str(tone or "neutral")
    if value in {"good", "warn", "bad", "neutral"}:
        return value
    return "neutral"


def _recent_directional_trade_rows(runtime_payload: dict[str, object] | None, *, pod: str) -> list[dict[str, object]]:
    if not isinstance(runtime_payload, dict):
        return []
    report = runtime_payload.get("report", {})
    if not isinstance(report, dict):
        return []
    closed_trade_log = report.get("closed_trade_log", [])
    if not isinstance(closed_trade_log, list):
        return []
    rows: list[dict[str, object]] = []
    for item in reversed(closed_trade_log[-12:]):
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "timestamp": str(item.get("closed_at") or item.get("date") or "-"),
                "pod": pod,
                "symbol": str(item.get("symbol", "-")),
                "side": str(item.get("side", "-")),
                "status": "closed",
                "open_reason": str(item.get("setup") or item.get("open_reason") or "-"),
                "close_reason": str(item.get("close_reason") or "-"),
                "entry_price": item.get("entry_price"),
                "exit_price": item.get("exit_price"),
                "current_price": item.get("current_price") or item.get("exit_price"),
                "notional_usd": item.get("target_notional_usd"),
                "leverage": item.get("leverage"),
                "pnl_usd": item.get("pnl_usd"),
                "opened_at": item.get("opened_at"),
                "closed_at": item.get("closed_at"),
                "invalidation_price": item.get("invalidation_price"),
                "stop_bps": item.get("stop_bps"),
                "take_profit_bps": item.get("take_profit_bps"),
            }
        )
    return rows


def _normalized_trade_side(value: object) -> str:
    raw = str(value or "").strip()
    normalized = raw.lower()
    if normalized in {"long", "buy", "buy_yes", "yes"} or normalized.endswith("_long"):
        return "long"
    if normalized in {"short", "sell", "buy_no", "no"} or normalized.endswith("_short"):
        return "short"
    if "long" in normalized and "short" not in normalized:
        return "long"
    if "short" in normalized:
        return "short"
    return normalized or "-"


def _trade_summary_key(pod: str, symbol: object, side: object) -> tuple[str, str, str]:
    normalized_symbol = str(symbol or "-").strip().upper() or "-"
    return (str(pod), normalized_symbol, _normalized_trade_side(side))


def _add_trade_summary_row(
    rows: dict[tuple[str, str, str], dict[str, object]],
    *,
    pod: str,
    symbol: object,
    side: object,
    closed_trades: int = 0,
    open_positions: int = 0,
    win_count: int = 0,
    loss_count: int = 0,
    realized_pnl_usd: object = None,
    unrealized_pnl_usd: object = None,
) -> None:
    key = _trade_summary_key(pod, symbol, side)
    if key not in rows:
        rows[key] = {
            "pod": key[0],
            "symbol": key[1],
            "side": key[2],
            "closed_trade_count": 0,
            "open_position_count": 0,
            "win_count": 0,
            "loss_count": 0,
            "win_rate": None,
            "realized_pnl_usd": 0.0,
            "unrealized_pnl_usd": 0.0,
            "total_pnl_usd": 0.0,
        }
    row = rows[key]
    row["closed_trade_count"] = int(row["closed_trade_count"]) + int(closed_trades)
    row["open_position_count"] = int(row["open_position_count"]) + int(open_positions)
    row["win_count"] = int(row["win_count"]) + int(win_count)
    row["loss_count"] = int(row["loss_count"]) + int(loss_count)
    row["win_rate"] = _win_rate_from_counts(
        int(row["win_count"]),
        int(row["loss_count"]),
    )
    realized = _float_or_none(realized_pnl_usd)
    if realized is not None:
        row["realized_pnl_usd"] = round(float(row["realized_pnl_usd"]) + realized, 8)
    unrealized = _float_or_none(unrealized_pnl_usd)
    if unrealized is not None:
        row["unrealized_pnl_usd"] = round(float(row["unrealized_pnl_usd"]) + unrealized, 8)
    row["total_pnl_usd"] = round(
        float(row["realized_pnl_usd"]) + float(row["unrealized_pnl_usd"]),
        8,
    )


def _sorted_trade_summary(rows: dict[tuple[str, str, str], dict[str, object]]) -> list[dict[str, object]]:
    return sorted(
        rows.values(),
        key=lambda item: (
            str(item.get("pod", "")),
            str(item.get("symbol", "")),
            str(item.get("side", "")),
        ),
    )


def _directional_pod_trade_summary(
    runtime_payload: dict[str, object] | None,
    *,
    pod: str,
) -> list[dict[str, object]]:
    if not isinstance(runtime_payload, dict):
        return []
    rows: dict[tuple[str, str, str], dict[str, object]] = {}
    report = runtime_payload.get("report", {})
    if not isinstance(report, dict):
        report = {}
    closed_trade_log = report.get("closed_trade_log", [])
    if isinstance(closed_trade_log, list) and closed_trade_log:
        for item in closed_trade_log:
            if not isinstance(item, dict):
                continue
            win_count, loss_count = _win_counts_from_pnl(item.get("pnl_usd"))
            _add_trade_summary_row(
                rows,
                pod=pod,
                symbol=item.get("symbol"),
                side=item.get("side"),
                closed_trades=1,
                win_count=win_count,
                loss_count=loss_count,
                realized_pnl_usd=item.get("pnl_usd"),
            )
    else:
        trades_by_symbol = report.get("trades_by_symbol", {})
        pnl_by_symbol = report.get("pnl_by_symbol", {})
        if isinstance(trades_by_symbol, dict):
            for symbol, count in trades_by_symbol.items():
                pnl = pnl_by_symbol.get(symbol) if isinstance(pnl_by_symbol, dict) else None
                _add_trade_summary_row(
                    rows,
                    pod=pod,
                    symbol=symbol,
                    side="mixed",
                    closed_trades=int(count or 0),
                    realized_pnl_usd=pnl,
                )
    open_positions = runtime_payload.get("open_positions", [])
    if isinstance(open_positions, list):
        for item in open_positions:
            if not isinstance(item, dict):
                continue
            _add_trade_summary_row(
                rows,
                pod=pod,
                symbol=item.get("symbol"),
                side=item.get("side"),
                open_positions=1,
                unrealized_pnl_usd=item.get("unrealized_pnl_usd"),
            )
    return _sorted_trade_summary(rows)


def _hip4_pod_trade_summary(runtime_payload: dict[str, object] | None) -> list[dict[str, object]]:
    if not isinstance(runtime_payload, dict):
        return []
    rows: dict[tuple[str, str, str], dict[str, object]] = {}
    settled_positions = runtime_payload.get("settled_positions", [])
    if isinstance(settled_positions, list):
        for item in settled_positions:
            if not isinstance(item, dict):
                continue
            pnl = _first_float(
                item,
                "estimated_pnl_usdc",
                "net_pnl_usdc",
                "pnl_usdc",
            )
            win_count, loss_count = _win_counts_from_pnl(pnl)
            _add_trade_summary_row(
                rows,
                pod="pod_b",
                symbol=item.get("underlying") or item.get("symbol"),
                side=item.get("side"),
                closed_trades=1,
                win_count=win_count,
                loss_count=loss_count,
                realized_pnl_usd=pnl,
            )
    open_positions = runtime_payload.get("open_positions", [])
    if isinstance(open_positions, list):
        for item in open_positions:
            if not isinstance(item, dict):
                continue
            _add_trade_summary_row(
                rows,
                pod="pod_b",
                symbol=item.get("underlying") or item.get("symbol"),
                side=item.get("side"),
                open_positions=1,
                unrealized_pnl_usd=_first_float(
                    item,
                    "estimated_pnl_usdc",
                    "unrealized_pnl_usd",
                    "estimated_gross_pnl_usdc",
                ),
            )
    return _sorted_trade_summary(rows)


def _pod_trade_summary(
    runtime_payload: dict[str, object] | None,
    *,
    pod: str,
) -> list[dict[str, object]]:
    if pod == "pod_b" and is_hip4_pod_b_replacement_runtime(runtime_payload):
        return _hip4_pod_trade_summary(runtime_payload)
    return _directional_pod_trade_summary(runtime_payload, pod=pod)


def _global_trade_summary(
    pod_rows: dict[str, list[dict[str, object]]],
) -> list[dict[str, object]]:
    rows: dict[tuple[str, str, str], dict[str, object]] = {}
    pods_by_key: dict[tuple[str, str, str], set[str]] = {}
    for pod, summary_rows in pod_rows.items():
        for item in summary_rows:
            key = _trade_summary_key("global", item.get("symbol"), item.get("side"))
            _add_trade_summary_row(
                rows,
                pod="global",
                symbol=item.get("symbol"),
                side=item.get("side"),
                closed_trades=int(item.get("closed_trade_count", 0) or 0),
                open_positions=int(item.get("open_position_count", 0) or 0),
                win_count=int(item.get("win_count", 0) or 0),
                loss_count=int(item.get("loss_count", 0) or 0),
                realized_pnl_usd=item.get("realized_pnl_usd"),
                unrealized_pnl_usd=item.get("unrealized_pnl_usd"),
            )
            pods_by_key.setdefault(key, set()).add(pod)
    merged = _sorted_trade_summary(rows)
    for item in merged:
        key = _trade_summary_key("global", item.get("symbol"), item.get("side"))
        item["pods"] = sorted(pods_by_key.get(key, set()))
    return merged


def _stats_windows() -> list[tuple[str, timedelta]]:
    return [
        ("24h", timedelta(hours=24)),
        ("3 jours", timedelta(days=3)),
        ("1 semaine", timedelta(days=7)),
    ]


def _pod_capital_map(
    snapshot: dict[str, object],
    runtime_report: dict[str, object],
) -> dict[str, float]:
    capital_by_pod: dict[str, float] = {}
    for item in runtime_report.get("pods", []):
        if not isinstance(item, dict):
            continue
        pod = str(item.get("pod") or "")
        if not pod:
            continue
        capital_by_pod[pod] = float(item.get("target_usd", 0.0) or 0.0)
    pod_b_runtime = snapshot.get("pod_b_runtime")
    if isinstance(pod_b_runtime, dict):
        capital = pod_b_runtime.get("capital", {})
        if isinstance(capital, dict):
            budget = _float_or_none(capital.get("budget_usdc"))
            if budget is not None:
                capital_by_pod["pod_b"] = budget
    return capital_by_pod


def _new_stats_row(
    *,
    window: str,
    pod: str,
    symbol: str,
    side: str,
    capital_usd: float,
) -> dict[str, object]:
    return {
        "window": window,
        "pod": pod,
        "symbol": symbol,
        "side": side,
        "closed_trade_count": 0,
        "open_position_count": 0,
        "win_count": 0,
        "loss_count": 0,
        "win_rate": None,
        "realized_pnl_usd": 0.0,
        "unrealized_pnl_usd": 0.0,
        "total_pnl_usd": 0.0,
        "capital_usd": round(capital_usd, 8),
    }


def _add_stats_row(
    rows: dict[tuple[str, str, str, str], dict[str, object]],
    *,
    window: str,
    pod: str,
    symbol: object,
    side: object,
    capital_usd: float,
    closed_trades: int = 0,
    open_positions: int = 0,
    realized_pnl_usd: object = None,
    unrealized_pnl_usd: object = None,
    win_count: int = 0,
    loss_count: int = 0,
) -> None:
    normalized_symbol = str(symbol or "-").strip().upper() or "-"
    normalized_side = _normalized_trade_side(side)
    key = (window, pod, normalized_symbol, normalized_side)
    if key not in rows:
        rows[key] = _new_stats_row(
            window=window,
            pod=pod,
            symbol=normalized_symbol,
            side=normalized_side,
            capital_usd=capital_usd,
        )
    row = rows[key]
    row["closed_trade_count"] = int(row["closed_trade_count"]) + int(closed_trades)
    row["open_position_count"] = int(row["open_position_count"]) + int(open_positions)
    row["win_count"] = int(row["win_count"]) + int(win_count)
    row["loss_count"] = int(row["loss_count"]) + int(loss_count)
    row["win_rate"] = _win_rate_from_counts(
        int(row["win_count"]),
        int(row["loss_count"]),
    )
    realized = _float_or_none(realized_pnl_usd)
    if realized is not None:
        row["realized_pnl_usd"] = round(float(row["realized_pnl_usd"]) + realized, 8)
    unrealized = _float_or_none(unrealized_pnl_usd)
    if unrealized is not None:
        row["unrealized_pnl_usd"] = round(float(row["unrealized_pnl_usd"]) + unrealized, 8)
    row["total_pnl_usd"] = round(
        float(row["realized_pnl_usd"]) + float(row["unrealized_pnl_usd"]),
        8,
    )


def _temporal_stats_payload(
    snapshot: dict[str, object],
    runtime_report: dict[str, object],
) -> dict[str, object]:
    now = datetime.now(timezone.utc)
    capital_by_pod = _pod_capital_map(snapshot, runtime_report)
    pod_rows: dict[tuple[str, str, str, str], dict[str, object]] = {}
    coin_rows: dict[tuple[str, str, str, str], dict[str, object]] = {}
    window_order = {label: index for index, (label, _delta) in enumerate(_stats_windows())}

    def ensure_pod_row(window: str, pod: str) -> None:
        key = (window, pod, "ALL", "mixed")
        if key not in pod_rows:
            pod_rows[key] = _new_stats_row(
                window=window,
                pod=pod,
                symbol="ALL",
                side="mixed",
                capital_usd=capital_by_pod.get(pod, 0.0),
            )

    for window, delta in _stats_windows():
        window_start = now - delta
        for pod in ("pod_a", "pod_b", "pod_c"):
            ensure_pod_row(window, pod)
            runtime_payload = snapshot.get(f"{pod}_runtime")
            if not isinstance(runtime_payload, dict):
                continue
            capital_usd = capital_by_pod.get(pod, 0.0)
            if pod == "pod_b" and is_hip4_pod_b_replacement_runtime(runtime_payload):
                settled_positions = runtime_payload.get("settled_positions", [])
                if isinstance(settled_positions, list):
                    for item in settled_positions:
                        if not isinstance(item, dict):
                            continue
                        settled_at = _parse_timestamp(
                            item.get("settled_at")
                            or item.get("closed_at")
                            or item.get("opened_at")
                        )
                        if settled_at is None or settled_at < window_start:
                            continue
                        pnl = _first_float(
                            item,
                            "estimated_pnl_usdc",
                            "net_pnl_usdc",
                            "pnl_usdc",
                        )
                        win_count, loss_count = _win_counts_from_pnl(pnl)
                        for rows, symbol, side in (
                            (pod_rows, "ALL", "mixed"),
                            (
                                coin_rows,
                                item.get("underlying") or item.get("symbol"),
                                item.get("side"),
                            ),
                        ):
                            _add_stats_row(
                                rows,
                                window=window,
                                pod=pod,
                                symbol=symbol,
                                side=side,
                                capital_usd=capital_usd,
                                closed_trades=1,
                                realized_pnl_usd=pnl,
                                win_count=win_count,
                                loss_count=loss_count,
                            )
                open_positions = runtime_payload.get("open_positions", [])
                if isinstance(open_positions, list):
                    for item in open_positions:
                        if not isinstance(item, dict):
                            continue
                        unrealized = _first_float(
                            item,
                            "estimated_pnl_usdc",
                            "estimated_pnl_usd",
                            "unrealized_pnl_usd",
                            "estimated_gross_pnl_usdc",
                        )
                        for rows, symbol, side in (
                            (pod_rows, "ALL", "mixed"),
                            (
                                coin_rows,
                                item.get("underlying") or item.get("symbol"),
                                item.get("side"),
                            ),
                        ):
                            _add_stats_row(
                                rows,
                                window=window,
                                pod=pod,
                                symbol=symbol,
                                side=side,
                                capital_usd=capital_usd,
                                open_positions=1,
                                unrealized_pnl_usd=unrealized,
                            )
                continue

            report = runtime_payload.get("report", {})
            closed_trade_log = report.get("closed_trade_log", []) if isinstance(report, dict) else []
            if isinstance(closed_trade_log, list):
                for item in closed_trade_log:
                    if not isinstance(item, dict):
                        continue
                    closed_at = _parse_timestamp(
                        item.get("closed_at")
                        or item.get("timestamp")
                        or item.get("date")
                    )
                    if closed_at is None or closed_at < window_start:
                        continue
                    pnl = item.get("pnl_usd")
                    win_count, loss_count = _win_counts_from_pnl(pnl)
                    for rows, symbol, side in (
                        (pod_rows, "ALL", "mixed"),
                        (coin_rows, item.get("symbol"), item.get("side")),
                    ):
                        _add_stats_row(
                            rows,
                            window=window,
                            pod=pod,
                            symbol=symbol,
                            side=side,
                            capital_usd=capital_usd,
                            closed_trades=1,
                            realized_pnl_usd=pnl,
                            win_count=win_count,
                            loss_count=loss_count,
                        )

            open_positions = runtime_payload.get("open_positions", [])
            if isinstance(open_positions, list):
                for item in open_positions:
                    if not isinstance(item, dict):
                        continue
                    for rows, symbol, side in (
                        (pod_rows, "ALL", "mixed"),
                        (coin_rows, item.get("symbol"), item.get("side")),
                    ):
                        _add_stats_row(
                            rows,
                            window=window,
                            pod=pod,
                            symbol=symbol,
                            side=side,
                            capital_usd=capital_usd,
                            open_positions=1,
                            unrealized_pnl_usd=item.get("unrealized_pnl_usd"),
                        )

    def sort_rows(rows: dict[tuple[str, str, str, str], dict[str, object]]) -> list[dict[str, object]]:
        return sorted(
            rows.values(),
            key=lambda item: (
                window_order.get(str(item.get("window")), 999),
                str(item.get("pod")),
                str(item.get("symbol")),
                str(item.get("side")),
            ),
        )

    return {
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "windows": [label for label, _delta in _stats_windows()],
        "by_pod": sort_rows(pod_rows),
        "by_coin": sort_rows(coin_rows),
    }


def _open_position_rows(snapshot: dict[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for pod_name in ("pod_a", "pod_b", "pod_c"):
        runtime_payload = snapshot.get(f"{pod_name}_runtime")
        if not isinstance(runtime_payload, dict):
            continue
        positions = runtime_payload.get("open_positions", [])
        if not isinstance(positions, list):
            continue
        for item in positions:
            if not isinstance(item, dict):
                continue
            if (
                pod_name == "pod_b"
                and str(runtime_payload.get("pod_kind", "")).lower() == "hip4_outcome_edge_pod"
            ):
                metadata = item.get("metadata", {})
                signal = metadata.get("signal", {}) if isinstance(metadata, dict) else {}
                if not isinstance(signal, dict):
                    signal = {}
                signal_metadata = signal.get("metadata", {})
                if not isinstance(signal_metadata, dict):
                    signal_metadata = {}
                fills = item.get("fills", [])
                first_fill = fills[0] if isinstance(fills, list) and fills and isinstance(fills[0], dict) else {}
                rows.append(
                    {
                        "pod": pod_name,
                        "symbol": str(item.get("underlying") or item.get("market_id") or "-"),
                        "side": str(item.get("side") or signal.get("side") or "-"),
                        "status": "open",
                        "open_reason": str(
                            signal.get("reason")
                            or item.get("edge_type")
                            or item.get("market_id")
                            or "-"
                        ),
                        "close_reason": "-",
                        "entry_price": first_fill.get("avg_price") or signal_metadata.get("yes_ask"),
                        "current_price": None,
                        "exit_price": None,
                        "notional_usd": item.get("cost_usdc"),
                        "current_notional_usd": item.get("cost_usdc"),
                        "leverage": None,
                        "pnl_usd": item.get("estimated_pnl_usd"),
                        "unrealized_pnl_usd": item.get("estimated_pnl_usd"),
                        "opened_at": item.get("opened_at"),
                        "closed_at": None,
                        "margin_usd": item.get("max_loss_usdc") or item.get("cost_usdc"),
                        "risk_budget_usd": item.get("max_loss_usdc"),
                        "expected_loss_usd": item.get("max_loss_usdc"),
                        "invalidation_price": None,
                        "stop_bps": None,
                        "time_stop_hours": None,
                        "take_profit_bps": None,
                        "break_even_trigger_bps": None,
                        "trailing_activation_bps": None,
                        "trailing_distance_bps": None,
                        "best_price_seen": None,
                        "confidence": signal.get("confidence"),
                        "campaign_mode_active": False,
                        "routing_revoke_exempt": True,
                    }
                )
                continue
            rows.append(
                {
                    "pod": pod_name,
                    "symbol": str(item.get("symbol", "-")),
                    "side": str(item.get("side", "-")),
                    "status": "open",
                    "open_reason": str(item.get("open_reason") or item.get("setup") or "-"),
                    "close_reason": "-",
                    "entry_price": item.get("entry_price"),
                    "current_price": item.get("current_price"),
                    "exit_price": None,
                    "notional_usd": item.get("target_notional_usd"),
                    "current_notional_usd": item.get("current_notional_usd"),
                    "leverage": item.get("leverage"),
                    "pnl_usd": None,
                    "unrealized_pnl_usd": item.get("unrealized_pnl_usd"),
                    "opened_at": item.get("opened_at"),
                    "closed_at": None,
                    "margin_usd": item.get("margin_usd"),
                    "risk_budget_usd": item.get("risk_budget_usd"),
                    "expected_loss_usd": item.get("expected_loss_usd"),
                    "invalidation_price": item.get("invalidation_price"),
                    "stop_bps": item.get("stop_bps"),
                    "time_stop_hours": item.get("time_stop_hours"),
                    "take_profit_bps": item.get("take_profit_bps"),
                    "break_even_trigger_bps": item.get("break_even_trigger_bps"),
                    "trailing_activation_bps": item.get("trailing_activation_bps"),
                    "trailing_distance_bps": item.get("trailing_distance_bps"),
                    "best_price_seen": item.get("best_price_seen"),
                    "confidence": item.get("confidence"),
                    "campaign_mode_active": item.get("campaign_mode_active"),
                    "routing_revoke_exempt": item.get("routing_revoke_exempt"),
                }
            )

    rows.sort(key=lambda item: (str(item.get("pod")), str(item.get("symbol"))))
    return rows


def _trade_event_rows(snapshot: dict[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    rows.extend(_recent_directional_trade_rows(snapshot.get("pod_a_runtime"), pod="pod_a"))
    rows.extend(_recent_directional_trade_rows(snapshot.get("pod_b_runtime"), pod="pod_b"))
    rows.extend(_recent_directional_trade_rows(snapshot.get("pod_c_runtime"), pod="pod_c"))
    rows.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
    return rows[:30]


def _dashboard_status_items(
    snapshot: dict[str, object],
    runtime_report: dict[str, object],
    *,
    pod_names: tuple[str, ...] = ("pod_a", "pod_b", "pod_c"),
) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    snapshot_status = _latest_snapshot_status_for_payload(snapshot)
    items.append(snapshot_status)

    pod_set = set(pod_names)
    pod_reports = [
        item
        for item in runtime_report.get("pods", [])
        if isinstance(item, dict)
        and str(item.get("pod")) in pod_set
    ]
    if pod_reports:
        enabled_count = sum(1 for item in pod_reports if bool(item.get("enabled", False)))
        healthy_count = sum(
            1
            for item in pod_reports
            if bool(item.get("enabled", False)) and bool(item.get("healthy", False))
        )
    else:
        healthy_count = int(runtime_report.get("healthy_pod_count", 0))
        enabled_count = int(runtime_report.get("enabled_pod_count", 0))
    if healthy_count == enabled_count:
        items.append(
            {
                "status": "good",
                "label": "Pods OK",
                "comment": f"{healthy_count}/{enabled_count} pod(s) OK.",
            }
        )
    elif healthy_count > 0:
        items.append(
            {
                "status": "warn",
                "label": "Pods à surveiller",
                "comment": f"{healthy_count}/{enabled_count} pod(s) OK.",
            }
        )
    else:
        items.append(
            {
                "status": "bad",
                "label": "Pods KO",
                "comment": f"0/{enabled_count} pod(s) OK.",
            }
        )

    healthy_collectors = int(runtime_report.get("healthy_service_count", 0))
    enabled_collectors = int(runtime_report.get("enabled_service_count", 0))
    if enabled_collectors > 0:
        if healthy_collectors == enabled_collectors:
            items.append(
                {
                    "status": "good",
                    "label": "Collectors OK",
                    "comment": f"{healthy_collectors}/{enabled_collectors} collector(s) OK.",
                }
            )
        elif healthy_collectors > 0:
            items.append(
                {
                    "status": "warn",
                    "label": "Collectors à surveiller",
                    "comment": f"{healthy_collectors}/{enabled_collectors} collector(s) OK.",
                }
            )
        else:
            items.append(
                {
                    "status": "bad",
                    "label": "Collectors KO",
                    "comment": f"0/{enabled_collectors} collector(s) OK.",
                }
            )

    conflicts = int(snapshot["metrics"]["ownership_conflict_count"])
    items.append(
        {
            "status": "good" if conflicts == 0 else "bad",
            "label": "Aucun conflit" if conflicts == 0 else "Conflit ownership",
            "comment": "Ownership propre." if conflicts == 0 else f"{conflicts} conflit(s) détecté(s).",
        }
    )

    fill_count = sum(
        int(item.get("total_fill_count", 0) or 0)
        for item in pod_reports
    )
    if fill_count > 0:
        items.append(
            {
                "status": "good",
                "label": "Activité visible",
                "comment": f"A/C {fill_count} exécution(s) visible(s).",
            }
        )
    else:
        items.append(
            {
                "status": "warn",
                "label": "Pas d'exécution",
                "comment": "Runtime actif, aucune exécution visible.",
            }
        )

    return items


def _dashboard_commentary(
    snapshot: dict[str, object],
    runtime_report: dict[str, object],
    *,
    pod_names: tuple[str, ...] = ("pod_a", "pod_b", "pod_c"),
) -> str:
    conflicts = int(snapshot["metrics"]["ownership_conflict_count"])
    pod_set = set(pod_names)
    pod_reports = [
        item
        for item in runtime_report.get("pods", [])
        if isinstance(item, dict)
        and str(item.get("pod")) in pod_set
    ]
    if pod_reports:
        enabled = sum(1 for item in pod_reports if bool(item.get("enabled", False)))
        healthy = sum(
            1
            for item in pod_reports
            if bool(item.get("enabled", False)) and bool(item.get("healthy", False))
        )
        fill_count = sum(int(item.get("total_fill_count", 0) or 0) for item in pod_reports)
    else:
        enabled = int(snapshot["metrics"]["enabled_pod_count"])
        healthy = int(runtime_report.get("healthy_pod_count", 0))
        fill_count = int(runtime_report.get("total_fill_count", 0))
    collector_enabled = int(runtime_report.get("enabled_service_count", 0))
    collector_healthy = int(runtime_report.get("healthy_service_count", 0))
    latest_snapshot = _latest_snapshot_status_for_payload(snapshot)
    if latest_snapshot["status"] == "bad":
        return "Collector en retard. Vérifie la collecte live et les logs API."
    if healthy < enabled:
        return "Un pod actif est à surveiller."
    if collector_enabled > collector_healthy:
        return "Un collector de données est à surveiller."
    if conflicts > 0:
        return "Conflit d'ownership détecté. Corriger avant d'augmenter le risque."
    if fill_count == 0:
        return "Runtime OK. Pas encore d'exécution visible."
    return "Runtime OK. Activité récente visible."


def health_payload(supervisor: TridentSupervisor) -> dict[str, object]:
    refreshed_from_snapshots = _refresh_supervisor_from_latest_snapshot(supervisor)
    regime = supervisor.state.regime.value
    if not refreshed_from_snapshots:
        runtime_supervisor = merge_runtime_supervisor_snapshot(
            _normalized_runtime_payload(load_runtime_status("logs/pod_a_live_status.json")),
            _normalized_runtime_payload(load_runtime_status("logs/pod_c_live_status.json")),
        )
        if isinstance(runtime_supervisor, dict) and runtime_supervisor.get("regime") is not None:
            regime = str(runtime_supervisor["regime"])
    return {
        "status": "ok",
        "version": VERSION,
        "profile": supervisor.profile,
        "mode": supervisor.mode,
        "exchange_network": _exchange_network_from_url(supervisor.config.hyperliquid.info_url),
        "regime": regime,
        "kill_switch_active": supervisor.kill_switch.is_active,
    }


def state_payload(
    supervisor: TridentSupervisor,
    metrics: MetricsRegistry,
    *,
    include_stats: bool = True,
) -> dict[str, object]:
    refreshed_from_snapshots = _refresh_supervisor_from_latest_snapshot(supervisor)
    supervisor.sync_pod_b()
    metrics.refresh_from_supervisor(supervisor)
    snapshot = supervisor.snapshot()
    snapshot = _merge_runtime_snapshot(
        snapshot,
        allow_runtime_authority_override=not refreshed_from_snapshots,
        market_cluster_for_symbol=lambda symbol: cluster_for_symbol(
            supervisor.config,
            symbol,
        ),
    )
    snapshot["exchange"] = {
        "network": _exchange_network_from_url(supervisor.config.hyperliquid.info_url),
        "info_url": supervisor.config.hyperliquid.info_url,
        "ws_url": supervisor.config.hyperliquid.ws_url,
        "snapshot_output_dir": supervisor.config.hyperliquid.snapshot_output_dir,
    }
    snapshot["metrics"] = metrics.snapshot()
    runtime_report = build_runtime_report(
        supervisor,
        metrics,
        runtime_snapshot=snapshot,
    ).to_dict()
    snapshot["exchange"]["account"] = _exchange_account_payload(runtime_report)
    snapshot["runtime_report"] = runtime_report
    if include_stats:
        snapshot["stats"] = _temporal_stats_payload(snapshot, runtime_report)
    return snapshot


def _exchange_account_payload(runtime_report: dict[str, object]) -> dict[str, object]:
    keys = (
        "account_mode",
        "hl_available_usd",
        "hl_capital_source",
        "spot_usdc_total",
        "spot_usdc_hold",
        "spot_usdc_available",
        "perp_account_value_usd",
        "perp_withdrawable_usd",
        "total_margin_used_usd",
    )
    payload = {key: runtime_report.get(key) for key in keys if runtime_report.get(key) is not None}
    if not payload:
        return {}
    return payload


def metrics_payload(
    supervisor: TridentSupervisor,
    metrics: MetricsRegistry,
) -> dict[str, int | float]:
    _refresh_supervisor_from_latest_snapshot(supervisor)
    supervisor.sync_pod_b()
    metrics.refresh_from_supervisor(supervisor)
    return metrics.snapshot()


def report_payload(
    supervisor: TridentSupervisor,
    metrics: MetricsRegistry,
) -> dict[str, object]:
    refreshed_from_snapshots = _refresh_supervisor_from_latest_snapshot(supervisor)
    supervisor.sync_pod_b()
    snapshot = _merge_runtime_snapshot(
        supervisor.snapshot(),
        allow_runtime_authority_override=not refreshed_from_snapshots,
        market_cluster_for_symbol=lambda symbol: cluster_for_symbol(
            supervisor.config,
            symbol,
        ),
    )
    return build_runtime_report(supervisor, metrics, runtime_snapshot=snapshot).to_dict()


def hip4_outcome_payload() -> dict[str, object]:
    payload = _hip4_outcome_monitor_payload()
    payload["mainnet_observer"] = hip4_outcome_mainnet_payload()
    return payload


def hip4_outcome_mainnet_payload() -> dict[str, object]:
    return _hip4_outcome_monitor_payload(
        status_path=Path("logs/hip4_outcome_mainnet_status.json"),
        include_pod_b_alias_report=False,
        default_logs_dir=Path("logs/hip4_outcome_mainnet"),
        inactive_when_status_missing=True,
    )


def hip4_nautilus_shadow_payload(
    status_path: Path = Path("logs/hip4_nautilus_shadow/status.json"),
    logs_dir: Path = Path("logs/hip4_nautilus_shadow"),
) -> dict[str, object]:
    status = load_runtime_status(str(status_path))
    if not status:
        return {
            "pod": "hip4_nautilus_shadow",
            "mode": "shadow",
            "read_only": True,
            "active": False,
            "shadow_ready": False,
            "status": "missing",
            "status_path": str(status_path),
            "logs_dir": str(logs_dir),
            "data_quality_tail": [],
        }
    payload = sanitize_runtime_status_payload(status)
    payload.update(
        {
            "pod": "hip4_nautilus_shadow",
            "mode": str(payload.get("mode") or "shadow"),
            "read_only": True,
            "active": True,
            "status_path": str(status_path),
            "logs_dir": str(logs_dir),
            "data_quality_tail": _tail_csv_records(logs_dir / "data_quality.csv", limit=12),
        }
    )
    return payload


def _merge_runtime_snapshot(
    snapshot: dict[str, object],
    *,
    allow_runtime_authority_override: bool = True,
    market_cluster_for_symbol: Callable[[str], str | None] | None = None,
) -> dict[str, object]:
    hip4_enabled = _hip4_routes_enabled()
    live_journals_enabled = str(snapshot.get("mode", "")).strip().lower() == "live"
    pod_a_runtime = attach_live_journal_report(
        _normalized_runtime_payload(load_runtime_status("logs/pod_a_live_status.json")),
        "logs/pod_a_live.jsonl",
        enabled=live_journals_enabled,
        market_cluster_for_symbol=market_cluster_for_symbol,
    )
    pod_b_runtime = (
        _normalized_runtime_payload(load_runtime_status("logs/pod_b_live_status.json"))
        if hip4_enabled
        else None
    )
    pod_c_runtime = attach_live_journal_report(
        _normalized_runtime_payload(load_runtime_status("logs/pod_c_live_status.json")),
        "logs/pod_c_live.jsonl",
        enabled=live_journals_enabled,
        market_cluster_for_symbol=market_cluster_for_symbol,
    )
    snapshot["pod_a_runtime"] = pod_a_runtime
    snapshot["pod_b_runtime"] = pod_b_runtime
    snapshot["pod_c_runtime"] = pod_c_runtime
    if hip4_enabled and isinstance(pod_b_runtime, dict):
        snapshot["pod_b_status"] = sanitize_runtime_status_payload(
            pod_b_runtime,
            include_supervisor=False,
        )
        if is_hip4_pod_b_replacement_runtime(pod_b_runtime) and runtime_status_is_fresh(
            pod_b_runtime
        ):
            _apply_hip4_pod_b_replacement(snapshot, pod_b_runtime)
    if isinstance(snapshot.get("pod_health"), list):
        merged_health: list[dict[str, object]] = []
        for health in snapshot["pod_health"]:
            if not isinstance(health, dict):
                merged_health.append(health)
                continue
            merged = dict(health)
            pod_name = merged.get("pod")
            if pod_name == "pod_a" and snapshot["pods"]["pod_a"]["enabled"]:
                merged["healthy"] = runtime_status_is_fresh(pod_a_runtime)
                merged["message"] = (
                    "runtime status fresh"
                    if merged["healthy"]
                    else "runtime status missing or stale"
                )
            elif pod_name == "pod_b" and snapshot["pods"]["pod_b"]["enabled"]:
                pod_b_status = snapshot.get("pod_b_status", {})
                merged["healthy"] = (
                    runtime_status_is_fresh(
                        pod_b_status if isinstance(pod_b_status, dict) else None
                    )
                    and not _is_supervisor_fallback_runtime(
                        pod_b_status if isinstance(pod_b_status, dict) else None
                    )
                )
                merged["message"] = (
                    "runtime status fresh"
                    if merged["healthy"]
                    else "runtime status missing, stale, or replaced by supervisor fallback"
                )
            elif pod_name == "pod_c" and snapshot["pods"]["pod_c"]["enabled"]:
                merged["healthy"] = runtime_status_is_fresh(pod_c_runtime)
                merged["message"] = (
                    "runtime status fresh"
                    if merged["healthy"]
                    else "runtime status missing or stale"
                )
            merged_health.append(merged)
        snapshot["pod_health"] = merged_health

    runtime_supervisor = merge_runtime_supervisor_snapshot(
        pod_a_runtime,
        pod_b_runtime,
        pod_c_runtime,
        base_snapshot=snapshot,
    )
    if not allow_runtime_authority_override or not isinstance(runtime_supervisor, dict):
        return snapshot

    for key in (
        "enabled_pods",
        "regime",
        "raw_regime",
        "symbol_ownership",
        "ownership_conflicts",
        "symbol_routing",
        "local_regime_by_symbol",
        "local_regime_transitions",
        "symbol_reassignment_count_by_symbol",
        "routing_overrides",
        "pods",
        "allocations",
        "capital_plan",
        "regime_snapshot",
        "pending_regime",
        "pending_regime_count",
        "regime_transition_count",
        "regime_evaluation_count",
        "regime_history",
        "pod_a_signal_preview",
        "pod_b_signal_preview",
        "pod_c_signal_preview",
        "pod_a_signal_review",
        "pod_b_signal_review",
        "pod_c_signal_review",
    ):
        if key in runtime_supervisor:
            snapshot[key] = runtime_supervisor[key]

    if isinstance(pod_b_runtime, dict) and is_hip4_pod_b_replacement_runtime(
        pod_b_runtime
    ) and runtime_status_is_fresh(pod_b_runtime):
        _apply_hip4_pod_b_replacement(snapshot, pod_b_runtime)

    embedded_supervisor = _embedded_supervisor_snapshot(snapshot)
    if isinstance(pod_a_runtime, dict):
        pod_a_runtime["supervisor"] = copy.deepcopy(embedded_supervisor)
    if isinstance(pod_b_runtime, dict):
        pod_b_runtime["supervisor"] = copy.deepcopy(embedded_supervisor)
    if isinstance(pod_c_runtime, dict):
        pod_c_runtime["supervisor"] = copy.deepcopy(embedded_supervisor)
    return snapshot


def _apply_hip4_pod_b_replacement(
    snapshot: dict[str, object],
    pod_b_runtime: dict[str, object],
) -> None:
    enabled_pods = snapshot.get("enabled_pods")
    if not isinstance(enabled_pods, list):
        enabled_pods = []
    if "pod_b" not in [str(item) for item in enabled_pods]:
        enabled_pods = [*enabled_pods, "pod_b"]
    snapshot["enabled_pods"] = enabled_pods

    managed_symbols = pod_b_runtime.get("managed_symbols", [])
    if not isinstance(managed_symbols, list):
        managed_symbols = []
    pods = snapshot.get("pods")
    if not isinstance(pods, dict):
        pods = {}
    pod_b = dict(pods.get("pod_b", {}) if isinstance(pods.get("pod_b"), dict) else {})
    pod_b.update(
        {
            "enabled": True,
            "candidate_symbols": [str(symbol) for symbol in managed_symbols],
            "desired_symbols": [str(symbol) for symbol in managed_symbols],
            "owned_symbols": [str(symbol) for symbol in managed_symbols],
            "runtime_strategy": "HIP4OutcomeEdgePod",
            "runtime_mode": str(pod_b_runtime.get("mode", "")),
        }
    )
    pods["pod_b"] = pod_b
    snapshot["pods"] = pods

    health_rows = snapshot.get("pod_health")
    if not isinstance(health_rows, list):
        return
    updated_health_rows: list[object] = []
    for row in health_rows:
        if isinstance(row, dict) and row.get("pod") == "pod_b":
            merged = dict(row)
            merged["healthy"] = True
            merged["message"] = "HIP-4 outcome replacement runtime fresh"
            updated_health_rows.append(merged)
        else:
            updated_health_rows.append(row)
    snapshot["pod_health"] = updated_health_rows


def _pod_b_status_path_from_snapshot(snapshot: dict[str, object]) -> str:
    pod_b_status = snapshot.get("pod_b_status", {})
    if isinstance(pod_b_status, dict):
        status_path = pod_b_status.get("status_path")
        if isinstance(status_path, str) and status_path:
            return status_path
    return "logs/pod_b_live_status.json"


def _normalized_runtime_payload(payload: dict[str, object] | None) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    return sanitize_runtime_status_payload(payload)

def _embedded_supervisor_snapshot(snapshot: dict[str, object]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key in (
        "profile",
        "mode",
        "regime",
        "raw_regime",
        "kill_switch",
        "enabled_pods",
        "pod_health",
        "symbol_ownership",
        "ownership_conflicts",
        "symbol_routing",
        "local_regime_by_symbol",
        "local_regime_transitions",
        "symbol_reassignment_count_by_symbol",
        "routing_overrides",
        "pods",
        "allocations",
        "capital_plan",
        "regime_snapshot",
        "pending_regime",
        "pending_regime_count",
        "regime_transition_count",
        "regime_evaluation_count",
        "regime_history",
        "pod_a_signal_preview",
        "pod_b_signal_preview",
        "pod_c_signal_preview",
        "pod_a_signal_review",
        "pod_b_signal_review",
        "pod_c_signal_review",
    ):
        if key in snapshot:
            payload[key] = copy.deepcopy(snapshot[key])
    payload["source"] = "api_merged_runtime_view"
    return payload


def _refresh_supervisor_from_latest_snapshot(
    supervisor: TridentSupervisor,
    *,
    max_snapshot_age_seconds: float = 180.0,
) -> bool:
    record = _latest_snapshot_record(
        snapshot_dir=Path(supervisor.config.hyperliquid.snapshot_output_dir),
        max_snapshot_age_seconds=max_snapshot_age_seconds,
    )
    if record is None:
        return False
    supervisor.apply_regime_snapshot(
        RegimeSnapshot(**record.regime_snapshot),
        cluster_regime_snapshots={
            cluster: RegimeSnapshot(**snap)
            for cluster, snap in (record.cluster_regime_snapshots or {}).items()
            if isinstance(snap, dict)
        },
    )
    supervisor.refresh_symbol_routing(
        [
            symbol_market_snapshot_from_mapping(item)
            for item in record.symbols
            if isinstance(item, dict)
        ]
    )
    return True


def _latest_snapshot_record(
    *,
    snapshot_dir: Path,
    max_snapshot_age_seconds: float,
) -> SnapshotRecord | None:
    files = sorted(snapshot_dir.glob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not files:
        return None
    latest_file = files[0]
    age_seconds = max(
        datetime.now(timezone.utc).timestamp() - latest_file.stat().st_mtime,
        0.0,
    )
    if age_seconds > max_snapshot_age_seconds:
        return None
    loader = SnapshotLoader()
    records: list[SnapshotRecord] = []
    for record_index, line in enumerate(_tail_text_lines(latest_file, max_lines=200), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            if not isinstance(payload, dict):
                continue
            loader._validate_payload(payload, file_path=latest_file)
            enriched_payload = loader._enrich_payload(payload)
        except (json.JSONDecodeError, SnapshotFormatError, KeyError, TypeError):
            continue
        cluster_raw = enriched_payload.get("cluster_regime_snapshots")
        records.append(
            SnapshotRecord(
                record_index=record_index,
                source_file=latest_file.name,
                timestamp=enriched_payload.get("timestamp"),
                regime_snapshot=enriched_payload["regime_snapshot"],
                symbols=enriched_payload.get("symbols", []),
                cluster_regime_snapshots=(
                    cluster_raw if isinstance(cluster_raw, dict) else None
                ),
                capture_reason=(
                    enriched_payload.get("capture_reason")
                    if isinstance(enriched_payload.get("capture_reason"), str)
                    else None
                ),
                stream_source=(
                    enriched_payload.get("stream_source")
                    if isinstance(enriched_payload.get("stream_source"), str)
                    else None
                ),
            )
        )
    if not records:
        return None
    latest_key = (records[-1].source_file, records[-1].timestamp)
    latest_group: list[SnapshotRecord] = []
    for record in reversed(records):
        if (record.source_file, record.timestamp) != latest_key:
            break
        latest_group.append(record)
    latest_group.reverse()
    return merge_snapshot_records(latest_group)


def _pod_label(pod_name: str) -> str:
    return {
        "pod_a": "Pod A",
        "pod_b": "Pod B",
        "pod_c": "Pod C",
    }.get(pod_name, pod_name.replace("_", " ").title())


def _control_center_html(
    supervisor: TridentSupervisor,
    metrics: MetricsRegistry,
    *,
    active_tab: str,
    title: str,
    subtitle: str,
) -> str:
    snapshot = state_payload(supervisor, metrics)
    runtime_report = report_payload(supervisor, metrics)
    refreshed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    started_at = _parse_timestamp(snapshot.get("started_at"))
    uptime_label = (
        _format_duration_compact((datetime.now(timezone.utc) - started_at).total_seconds())
        if started_at is not None
        else "-"
    )
    refresh_seconds = 10
    status_items = _dashboard_status_items(snapshot, runtime_report)
    commentary = _dashboard_commentary(snapshot, runtime_report)
    recent_activity = _recent_activity_rows(snapshot)
    open_rows = _open_position_rows(snapshot)
    event_rows = _trade_event_rows(snapshot)
    pod_trade_summary_rows = {
        pod_name: _pod_trade_summary(
            snapshot.get(f"{pod_name}_runtime"),
            pod=pod_name,
        )
        for pod_name in ("pod_a", "pod_b", "pod_c")
    }
    global_trade_summary_rows = _global_trade_summary(pod_trade_summary_rows)
    stats_payload = snapshot.get("stats", {})
    if not isinstance(stats_payload, dict):
        stats_payload = _temporal_stats_payload(snapshot, runtime_report)
    stats_by_pod_rows = (
        stats_payload.get("by_pod", [])
        if isinstance(stats_payload.get("by_pod", []), list)
        else []
    )
    stats_by_coin_rows = (
        stats_payload.get("by_coin", [])
        if isinstance(stats_payload.get("by_coin", []), list)
        else []
    )
    pod_b_status = snapshot.get("pod_b_status", {})
    health_map = {
        str(item.get("pod")): item
        for item in snapshot.get("pod_health", [])
        if isinstance(item, dict) and item.get("pod") is not None
    }
    runtime_pod_map = {
        str(item.get("pod")): item
        for item in runtime_report.get("pods", [])
        if isinstance(item, dict) and item.get("pod") is not None
    }
    runtime_service_rows = [
        item
        for item in runtime_report.get("services", [])
        if isinstance(item, dict) and item.get("service") is not None
    ]
    pod_c_allowed_clusters = sorted(
        normalize_cluster_names(supervisor.config.pod_c.allowed_market_clusters)
    )
    pod_c_scope_symbols = symbols_in_allowed_clusters(
        supervisor.config,
        observation_universe_symbols(supervisor.config),
        supervisor.config.pod_c.allowed_market_clusters,
    )
    pod_c_scope_symbol_set = set(pod_c_scope_symbols)
    cluster_regimes = {
        str(cluster).strip().lower(): str(regime)
        for cluster, regime in snapshot.get("cluster_regimes", {}).items()
        if str(cluster).strip()
    }
    cluster_target_allocations = {
        str(cluster).strip().lower(): float(target_pct)
        for cluster, target_pct in snapshot.get("cluster_target_allocations", {}).items()
        if str(cluster).strip()
    }
    observed_status_rows = [
        item
        for item in snapshot.get("observed_symbol_status", [])
        if isinstance(item, dict) and item.get("symbol") is not None
    ]
    observed_symbols = {
        str(item.get("symbol")).upper()
        for item in observed_status_rows
    }
    tradable_symbols = {
        str(item.get("symbol")).upper()
        for item in observed_status_rows
        if bool(item.get("tradable"))
    }
    observed_count_by_cluster: dict[str, int] = {}
    tradable_count_by_cluster: dict[str, int] = {}
    for item in observed_status_rows:
        symbol = str(item.get("symbol", "")).upper()
        if not symbol:
            continue
        cluster = cluster_for_symbol(supervisor.config, symbol)
        observed_count_by_cluster[cluster] = observed_count_by_cluster.get(cluster, 0) + 1
        if bool(item.get("tradable")):
            tradable_count_by_cluster[cluster] = tradable_count_by_cluster.get(cluster, 0) + 1
    scope_count_by_cluster: dict[str, int] = {}
    for symbol in pod_c_scope_symbols:
        cluster = cluster_for_symbol(supervisor.config, symbol)
        scope_count_by_cluster[cluster] = scope_count_by_cluster.get(cluster, 0) + 1
    routing_rows = [
        item
        for item in snapshot.get("symbol_routing", [])
        if isinstance(item, dict) and item.get("symbol") is not None
    ]
    pod_c_routing_rows = [
        item
        for item in routing_rows
        if str(item.get("symbol")).upper() in pod_c_scope_symbol_set
    ]
    pod_c_routed_symbols = [
        str(item.get("symbol")).upper()
        for item in pod_c_routing_rows
        if item.get("owner") == "pod_c"
    ]
    pod_c_seen_not_observed = [
        symbol for symbol in pod_c_scope_symbols if symbol not in observed_symbols
    ]
    pod_c_observed_not_tradable = [
        symbol for symbol in pod_c_scope_symbols if symbol in observed_symbols and symbol not in tradable_symbols
    ]
    def fmt_number(value: object, digits: int = 2, *, fallback: str = "-") -> str:
        if value in (None, ""):
            return fallback
        try:
            return f"{float(value):.{digits}f}"
        except (TypeError, ValueError):
            return escape(str(value))

    def fmt_signed_usd(value: object, digits: int = 4) -> str:
        if value in (None, ""):
            return "-"
        try:
            return f"{float(value):+.{digits}f}"
        except (TypeError, ValueError):
            return escape(str(value))

    def fmt_pct(value: object, digits: int = 1) -> str:
        parsed = _float_or_none(value)
        if parsed is None:
            return "-"
        return f"{parsed * 100:.{digits}f}%"

    def hl_capital_note() -> str:
        mode = str(runtime_report.get("account_mode") or "mode inconnu")
        source = str(runtime_report.get("hl_capital_source") or "source inconnue")
        spot_total = fmt_number(runtime_report.get("spot_usdc_total"), 2)
        spot_hold = fmt_number(runtime_report.get("spot_usdc_hold"), 2)
        if source == "unified_spot_usdc":
            return f"{mode} · spot {spot_total} · hold {spot_hold}"
        if source == "perp_withdrawable":
            return f"{mode} · marge perp disponible"
        return f"{mode} · {source}"

    def render_stat_cards(cards: list[dict[str, str]]) -> str:
        return "".join(
            (
                "<article class='metric-card'>"
                f"<span>{escape(str(card['label']))}</span>"
                f"<strong>{escape(str(card['value']))}</strong>"
                f"<small>{escape(str(card['note']))}</small>"
                "</article>"
            )
            for card in cards
        )

    def render_coin_trade_summary_rows(
        rows: list[dict[str, object]],
        *,
        include_pods: bool = False,
    ) -> str:
        if not rows:
            colspan = 9 if include_pods else 8
            return f"<tr><td colspan='{colspan}'>Aucun trade par coin visible pour le moment.</td></tr>"
        rendered_rows: list[str] = []
        for item in rows:
            pods = item.get("pods", [])
            pods_label = (
                ", ".join(_pod_label(str(pod)) for pod in pods)
                if isinstance(pods, list) and pods
                else "-"
            )
            pod_cell = f"<td>{escape(pods_label)}</td>" if include_pods else ""
            rendered_rows.append(
                "<tr>"
                f"<td>{escape(str(item.get('symbol', '-')))}</td>"
                f"<td>{escape(str(item.get('side', '-')))}</td>"
                f"{pod_cell}"
                f"<td>{int(item.get('closed_trade_count', 0) or 0)}</td>"
                f"<td>{int(item.get('open_position_count', 0) or 0)}</td>"
                f"<td>{fmt_pct(item.get('win_rate'))}</td>"
                f"<td>{fmt_signed_usd(item.get('realized_pnl_usd'))}</td>"
                f"<td>{fmt_signed_usd(item.get('unrealized_pnl_usd'))}</td>"
                f"<td>{fmt_signed_usd(item.get('total_pnl_usd'))}</td>"
                "</tr>"
            )
        return "".join(rendered_rows)

    def render_coin_trade_summary_panel(
        *,
        title: str,
        description: str,
        rows: list[dict[str, object]],
        include_pods: bool = False,
        tone: object = "neutral",
    ) -> str:
        pod_header = _table_header("Pods", "Pods qui ont eu une activité visible sur ce coin et ce sens.") if include_pods else ""
        return (
            f"<div class='panel panel-{escape(_panel_tone(tone))}'>"
            "<div class='panel-header'>"
            f"<h3>{escape(title)}</h3>"
            f"<p>{escape(description)}</p>"
            "</div>"
            "<div class='table-wrap'>"
            "<table>"
            "<thead><tr>"
            f"{_table_header('Coin', 'Sous-jacent ou symbole tradé.')}"
            f"{_table_header('Sens', 'Lecture normalisée: long, short ou mixed quand les vieux agrégats ne donnent pas le sens.')}"
            f"{pod_header}"
            f"{_table_header('Trades', 'Nombre de trades fermés visibles pour ce coin et ce sens.')}"
            f"{_table_header('Ouvert', 'Nombre de positions encore ouvertes pour ce coin et ce sens.')}"
            f"{_table_header('Win rate', 'Pourcentage de trades fermés gagnants dans la vue visible.')}"
            f"{_table_header('PnL réalisé', 'PnL des trades fermés, net de fees quand le pod les modélise.')}"
            f"{_table_header('PnL latent', 'PnL non réalisé des positions encore ouvertes, quand disponible.')}"
            f"{_table_header('PnL visible', 'Somme du PnL réalisé et du PnL latent visible dans le runtime.')}"
            "</tr></thead>"
            f"<tbody>{render_coin_trade_summary_rows(rows, include_pods=include_pods)}</tbody>"
            "</table>"
            "</div>"
            "</div>"
        )

    def render_stats_rows(rows: list[dict[str, object]], *, include_coin: bool) -> str:
        if not rows:
            colspan = 13 if include_coin else 11
            return f"<tr><td colspan='{colspan}'>Aucune statistique visible pour le moment.</td></tr>"
        rendered_rows: list[str] = []
        for item in rows:
            coin_cell = (
                f"<td>{escape(str(item.get('symbol', '-')))}</td>"
                f"<td>{escape(str(item.get('side', '-')))}</td>"
                if include_coin
                else ""
            )
            rendered_rows.append(
                "<tr>"
                f"<td>{escape(str(item.get('window', '-')))}</td>"
                f"<td>{escape(_pod_label(str(item.get('pod', '-'))))}</td>"
                f"{coin_cell}"
                f"<td>{fmt_number(item.get('capital_usd'), 2)}</td>"
                f"<td>{int(item.get('closed_trade_count', 0) or 0)}</td>"
                f"<td>{int(item.get('open_position_count', 0) or 0)}</td>"
                f"<td>{int(item.get('win_count', 0) or 0)}</td>"
                f"<td>{int(item.get('loss_count', 0) or 0)}</td>"
                f"<td>{fmt_pct(item.get('win_rate'))}</td>"
                f"<td>{fmt_signed_usd(item.get('realized_pnl_usd'))}</td>"
                f"<td>{fmt_signed_usd(item.get('unrealized_pnl_usd'))}</td>"
                f"<td>{fmt_signed_usd(item.get('total_pnl_usd'))}</td>"
                "</tr>"
            )
        return "".join(rendered_rows)

    def render_preview_list(items: object) -> str:
        if not isinstance(items, list) or not items:
            return "<p class='soft-note'>Aucun signal en attente pour le moment.</p>"
        rows = []
        for item in items[:8]:
            if not isinstance(item, dict):
                continue
            symbol = escape(str(item.get("symbol", "-")))
            side = escape(str(item.get("side", ""))).upper()
            setup = escape(str(item.get("setup", "")))
            confidence = (
                f"{float(item['confidence']):.2f}"
                if item.get("confidence") not in (None, "")
                else "-"
            )
            parts = [symbol]
            if side:
                parts.append(side)
            if setup:
                parts.append(setup)
            if confidence != "-":
                parts.append(f"conf {confidence}")
            reason_summary = str(item.get("reason_summary", "")).strip()
            if reason_summary:
                rows.append(
                    f"<li>{' · '.join(parts)}<br><span class='soft-note'>{escape(reason_summary)}</span></li>"
                )
            else:
                rows.append(f"<li>{' · '.join(parts)}</li>")
        if not rows:
            return "<p class='soft-note'>Aucun signal en attente pour le moment.</p>"
        return f"<ul class='simple-list'>{''.join(rows)}</ul>"

    def render_review_list(items: object) -> str:
        if not isinstance(items, list):
            return "<p class='soft-note'>Aucun filtre notable pour le moment.</p>"
        filtered = [
            item for item in items
            if isinstance(item, dict)
            and str(item.get("status", "")) in {"filtered", "shadow_blocked_by_routing"}
        ]
        if not filtered:
            return "<p class='soft-note'>Aucun filtre notable pour le moment.</p>"
        rows = []
        for item in filtered[:6]:
            symbol = escape(str(item.get("symbol", "-")))
            status = str(item.get("status", "")).strip()
            side_value = str(item.get("preferred_side", "")).strip()
            if not side_value:
                side_value = str(item.get("side", "")).strip()
            side = escape(side_value).upper()
            summary = escape(str(item.get("reason_summary", "")))
            parts = [symbol]
            if side and side != "NEUTRAL":
                parts.append(side)
            if status == "shadow_blocked_by_routing":
                setup = escape(str(item.get("setup", "")))
                if setup:
                    parts.append(setup)
                confidence = item.get("confidence")
                if confidence not in (None, ""):
                    parts.append(f"conf {float(confidence):.2f}")
                parts.append("shadow")
            rows.append(
                f"<li>{' · '.join(parts)}<br><span class='soft-note'>{summary}</span></li>"
            )
        return f"<ul class='simple-list'>{''.join(rows)}</ul>"

    def pod_summary(pod_name: str) -> dict[str, object]:
        pod_cfg = snapshot["pods"].get(pod_name, {})
        pod_report = runtime_pod_map.get(pod_name, {})
        pod_health = health_map.get(pod_name, {})
        enabled = bool(pod_cfg.get("enabled", pod_report.get("enabled", False)))
        healthy = bool(pod_report.get("healthy", pod_health.get("healthy", False)))
        if not enabled:
            tone = "neutral"
            badge = "Off"
            comment = "Pod coupé."
        elif healthy:
            tone = "good"
            badge = "OK"
            comment = "Pod OK."
        elif str(pod_report.get("process_state", "")) in {"running", "completed"}:
            tone = "warn"
            badge = "Check"
            comment = str(
                pod_health.get("message")
                or "Pod à vérifier."
            )
        elif str(pod_report.get("process_state", "")) == "supervisor_fallback":
            tone = "warn"
            badge = "Check"
            comment = "Pas de runtime Pod B frais; fallback superviseur affiché."
        else:
            tone = "bad"
            badge = "KO"
            comment = str(
                pod_health.get("message")
                or "Statut runtime absent ou obsolète."
            )
        if enabled and pod_name in {"pod_a", "pod_b", "pod_c"}:
            if int(pod_report.get("position_count", 0)) > 0:
                comment = f"{int(pod_report.get('position_count', 0))} position(s) ouverte(s)."
            elif int(pod_report.get("preview_count", 0)) > 0:
                comment = (
                    f"{int(pod_report.get('preview_count', 0))} signal(aux), "
                    "0 position."
                )
        return {
            "label": _pod_label(pod_name),
            "tone": tone,
            "badge": badge,
            "comment": comment,
            "enabled": enabled,
            "healthy": healthy,
            "owned_symbols": pod_report.get("owned_symbols", pod_cfg.get("owned_symbols", [])),
            "target_pct": pod_report.get("target_pct", pod_cfg.get("target_pct", 0.0)),
            "target_usd": pod_report.get("target_usd", pod_cfg.get("target_usd", 0.0)),
            "preview_count": int(pod_report.get("preview_count", 0)),
            "process_state": _display_process_state(pod_report.get("process_state")),
            "position_count": int(pod_report.get("position_count", 0)),
            "open_order_count": int(pod_report.get("open_order_count", 0)),
            "total_fill_count": int(pod_report.get("total_fill_count", 0)),
            "realized_pnl_usd": float(pod_report.get("realized_pnl_usd", 0.0)),
            "total_unrealized_pnl_usd": float(pod_report.get("total_unrealized_pnl_usd", 0.0)),
            "win_count": int(pod_report.get("win_count", 0) or 0),
            "loss_count": int(pod_report.get("loss_count", 0) or 0),
            "win_rate": pod_report.get("win_rate"),
        }

    pod_a_summary = pod_summary("pod_a")
    pod_b_summary = pod_summary("pod_b")
    pod_c_summary = pod_summary("pod_c")
    pod_summaries = (pod_a_summary, pod_b_summary, pod_c_summary)

    latest_snapshot = _latest_snapshot_status_for_payload(snapshot)
    enabled_count = int(runtime_report.get("enabled_pod_count", 0))
    healthy_count = int(runtime_report.get("healthy_pod_count", 0))
    conflict_count = int(snapshot["metrics"]["ownership_conflict_count"])
    active_positions = int(runtime_report.get("active_position_count", 0))
    active_orders = int(runtime_report.get("active_open_order_count", 0))
    total_fills = int(runtime_report.get("total_fill_count", 0))
    enabled_collectors = int(runtime_report.get("enabled_service_count", 0))
    healthy_collectors = int(runtime_report.get("healthy_service_count", 0))
    if latest_snapshot["status"] == "bad" or conflict_count > 0:
        global_tone = "bad"
        global_label = "Agir"
    elif healthy_count < enabled_count:
        global_tone = "warn"
        global_label = "Surveiller"
    else:
        global_tone = "good"
        global_label = "OK"

    focus_items: list[dict[str, str]] = []
    if latest_snapshot["status"] == "bad":
        focus_items.append(
                {
                    "tone": "bad",
                    "label": "Maintenant",
                    "title": "Collector en retard",
                    "comment": str(latest_snapshot["comment"]),
            }
        )
    if conflict_count > 0:
        focus_items.append(
                {
                    "tone": "bad",
                    "label": "Maintenant",
                    "title": "Corriger l'ownership",
                    "comment": f"{conflict_count} conflit(s) détecté(s) entre pods.",
                }
            )
    for service in runtime_service_rows:
        if bool(service.get("enabled", True)) and not bool(service.get("healthy")):
            focus_items.append(
                {
                    "tone": "warn",
                    "label": "Vérifier",
                    "title": f"{service.get('label', service.get('service', 'collector'))} à vérifier",
                    "comment": str(service.get("comment") or "Runtime status collector absent ou obsolète."),
                }
            )
    for pod in pod_summaries:
        if bool(pod["enabled"]) and str(pod["tone"]) in {"warn", "bad"}:
            focus_items.append(
                {
                    "tone": str(pod["tone"]),
                    "label": "Vérifier",
                    "title": f"{pod['label']} à vérifier",
                    "comment": str(pod["comment"]),
                }
            )
    if not focus_items and total_fills == 0:
        focus_items.append(
            {
                "tone": "good",
                "label": "RAS",
                "title": "Aucune urgence",
                "comment": "Pas d'exécution visible, mais aucun incident runtime.",
            }
        )
    if not focus_items:
        focus_items.append(
            {
                "tone": "good",
                "label": "RAS",
                "title": "Runtime stable",
                "comment": "Statuts frais et activité visible.",
            }
        )
    focus_tone = "good"
    if any(str(item["tone"]) == "bad" for item in focus_items):
        focus_tone = "bad"
    elif any(str(item["tone"]) == "warn" for item in focus_items):
        focus_tone = "warn"

    status_rows = "".join(
        (
            f"<article class='status-card status-card-{escape(str(item['status']))}'>"
            f"<div class='status-head'>{_status_badge(str(item['status']), str(item['label']))}</div>"
            f"<p>{escape(str(item['comment']))}</p>"
            "</article>"
        )
        for item in status_items
    )
    focus_rows = "".join(
        (
            "<article class='focus-item'>"
            f"<div class='focus-item-top'><span class='dot dot-{escape(str(item['tone']))}'></span>"
            f"<div><strong>{escape(str(item['title']))}</strong>"
            f"<small>{escape(str(item['comment']))}</small></div></div>"
            f"<span class='focus-tag focus-tag-{escape(str(item['tone']))}'>{escape(str(item['label']))}</span>"
            "</article>"
        )
        for item in focus_items[:4]
    )
    summary_cards = render_stat_cards(
        [
            {
                "label": "Régime",
                "value": str(snapshot["regime"]),
                "note": "État de marché courant",
            },
            {
                "label": "Pods",
                "value": f"{int(runtime_report['healthy_pod_count'])}/{int(runtime_report['enabled_pod_count'])}",
                "note": "Pods sains sur pods actifs",
            },
            {
                "label": "Risque live",
                "value": f"{active_positions} pos · {active_orders} ordres",
                "note": "Exposition visible maintenant",
            },
            {
                "label": "Collectors",
                "value": f"{healthy_collectors}/{enabled_collectors}",
                "note": "Services funding visibles",
            },
            {
                "label": "Exécutions",
                "value": str(total_fills),
                "note": "Fills / trades observés",
            },
            {
                "label": "Realized PnL",
                "value": f"{float(runtime_report['realized_pnl_usd']):.4f} USD",
                "note": "Cumul runtime visible",
            },
            {
                "label": "Cash",
                "value": f"{float(snapshot['capital_plan']['cash_usd']):.2f} USD",
                "note": "Capital non déployé",
            },
        ]
    )
    cluster_order: list[str] = ["crypto"]
    for cluster in sorted(
        set(pod_c_allowed_clusters)
        | set(cluster_regimes)
        | set(cluster_target_allocations)
        | set(scope_count_by_cluster)
    ):
        if cluster != "crypto":
            cluster_order.append(cluster)
    cluster_cards = []
    crypto_budget_pct = float(snapshot.get("allocations", {}).get("pod_a", 0.0)) + float(
        snapshot.get("allocations", {}).get("pod_b", 0.0)
    )
    for cluster in cluster_order:
        observed_count = int(observed_count_by_cluster.get(cluster, 0))
        tradable_count = int(tradable_count_by_cluster.get(cluster, 0))
        scope_count = int(scope_count_by_cluster.get(cluster, 0))
        regime_value = str(snapshot["regime"]) if cluster == "crypto" else cluster_regimes.get(cluster, "No data")
        if cluster == "crypto":
            note = (
                f"{tradable_count}/{max(observed_count, 1)} tradable"
                f" · budget pods A/B {crypto_budget_pct:.0%}"
            )
        else:
            population = observed_count if observed_count > 0 else scope_count
            if population > 0:
                note = (
                    f"{tradable_count}/{population} tradable"
                    f" · budget {cluster_target_allocations.get(cluster, 0.0):.0%}"
                )
            else:
                note = (
                    f"Pas de snapshot visible"
                    f" · budget {cluster_target_allocations.get(cluster, 0.0):.0%}"
                )
        cluster_cards.append(
            {
                "label": _cluster_display_name(cluster),
                "value": regime_value,
                "note": note,
            }
        )
    cluster_regime_cards = render_stat_cards(cluster_cards)

    pod_cards = "".join(
        (
            f"<article class='pod-card pod-card-{escape(str(pod['tone']))}'>"
            f"<div class='pod-card-head'><span class='dot dot-{pod['tone']}'></span>"
            f"<div><h3>{escape(str(pod['label']))}</h3><p>{escape(str(pod['comment']))}</p></div></div>"
            f"<div class='pod-card-meta'>{_status_badge(str(pod['tone']), str(pod['badge']))}"
            f"<span class='meta-chip'>Process {escape(str(pod['process_state']))}</span></div>"
            "<dl class='pod-facts'>"
            f"<div><dt>Symbols</dt><dd>{', '.join(escape(str(symbol)) for symbol in pod['owned_symbols']) or '-'}</dd></div>"
            f"<div><dt>Allocation</dt><dd>{float(pod['target_pct']):.2f} · {float(pod['target_usd']):.2f} USD</dd></div>"
            f"<div><dt>Ouvert</dt><dd>{int(pod['position_count'])} pos · {int(pod['open_order_count'])} ordres</dd></div>"
            f"<div><dt>Exécution</dt><dd>{int(pod['total_fill_count'])} exec</dd></div>"
            f"<div><dt>PnL réalisé</dt><dd>{fmt_signed_usd(pod.get('realized_pnl_usd'))} USD</dd></div>"
            f"<div><dt>PnL latent</dt><dd>{fmt_signed_usd(pod.get('total_unrealized_pnl_usd'))} USD</dd></div>"
            f"<div><dt>Win rate</dt><dd>{fmt_pct(pod.get('win_rate'))} · {int(pod['win_count'])}/{int(pod['win_count']) + int(pod['loss_count'])}</dd></div>"
            "</dl>"
            f"<button class='tab-link' type='button' data-jump-tab='{escape(str(pod['label']).lower().replace(' ', '_'))}'>Voir l'onglet {escape(str(pod['label']))}</button>"
            "</article>"
        )
        for pod in (pod_a_summary, pod_b_summary, pod_c_summary)
    )

    def render_directional_open_rows(pod_name: str) -> str:
        rows = [
            item
            for item in open_rows
            if str(item.get("pod")) == pod_name and str(item.get("status")) == "open"
        ]
        if not rows:
            return "<tr><td colspan='12'>Aucune position ouverte visible pour le moment.</td></tr>"
        return "".join(
            (
                "<tr>"
                f"<td>{escape(str(item.get('symbol', '-')))}</td>"
                f"<td>{escape(str(item.get('side', '-')))}</td>"
                f"<td>{escape(_humanize_setup_reason(item.get('open_reason')))}</td>"
                f"<td>{fmt_number(item.get('entry_price'), 6)}</td>"
                f"<td>{fmt_number(item.get('current_price'), 6)}</td>"
                f"<td>{'-' if item.get('current_notional_usd') is None else format(float(item.get('current_notional_usd', 0.0)), '.2f')}</td>"
                f"<td>{'-' if item.get('margin_usd') is None else format(float(item.get('margin_usd', 0.0)), '.2f')}</td>"
                f"<td>{fmt_number(_directional_take_profit_price(item), 6)}</td>"
                f"<td>{fmt_number(_directional_stop_price(item), 6)}</td>"
                f"<td>{fmt_signed_usd(item.get('unrealized_pnl_usd'))}</td>"
                f"<td>{escape(_directional_trailing_status(item))}</td>"
                f"<td>{escape(str(item.get('opened_at') or '-'))}</td>"
                "</tr>"
            )
            for item in rows
        )

    def render_directional_closed_rows(pod_name: str) -> str:
        rows = [
            item
            for item in event_rows
            if str(item.get("pod")) == pod_name and str(item.get("status")) == "closed"
        ]
        if not rows:
            return "<tr><td colspan='13'>Aucun trade fermé visible pour le moment.</td></tr>"
        return "".join(
            (
                "<tr>"
                f"<td>{escape(str(item.get('closed_at') or item.get('timestamp') or '-'))}</td>"
                f"<td>{escape(str(item.get('symbol', '-')))}</td>"
                f"<td>{escape(str(item.get('side', '-')))}</td>"
                f"<td>{escape(_humanize_setup_reason(item.get('open_reason')))}</td>"
                f"<td>{escape(_humanize_close_reason(item.get('close_reason')))}</td>"
                f"<td>{fmt_number(item.get('entry_price'), 6)}</td>"
                f"<td>{fmt_number(item.get('exit_price'), 6)}</td>"
                f"<td>{'-' if item.get('notional_usd') is None else format(float(item.get('notional_usd', 0.0)), '.2f')}</td>"
                f"<td>{_format_leverage(item.get('leverage'))}</td>"
                f"<td>{fmt_signed_usd(item.get('pnl_usd'))}</td>"
                "</tr>"
            )
            for item in rows
        )

    def render_activity_open_rows() -> str:
        if not open_rows:
            return "<tr><td colspan='13'>Aucune position ouverte visible pour le moment.</td></tr>"
        return "".join(
            (
                "<tr data-filter-status='open' "
                f"data-filter-pod='{escape(str(item['pod']))}'>"
                f"<td>{escape(str(item['pod']))}</td>"
                f"<td>{escape(str(item['symbol']))}</td>"
                f"<td>{escape(str(item['side']))}</td>"
                f"<td>{escape(str(item['open_reason']))}</td>"
                f"<td>{fmt_number(item.get('entry_price'), 6)}</td>"
                f"<td>{'-' if item.get('notional_usd') is None else format(float(item.get('notional_usd', 0.0)), '.2f')}</td>"
                f"<td>{_format_leverage(item.get('leverage'))}</td>"
                f"<td>{fmt_number(item.get('confidence'), 2)}</td>"
                f"<td>{fmt_number(item.get('stop_bps'), 1)}</td>"
                f"<td>{escape(str(item.get('time_stop_hours') or '-'))}</td>"
                f"<td>{escape(str(item.get('opened_at') or '-'))}</td>"
                "</tr>"
            )
            for item in open_rows
        )

    def render_activity_event_rows() -> str:
        if not event_rows:
            return "<tr><td colspan='12'>Aucun évènement de trade récent visible pour le moment.</td></tr>"
        return "".join(
            (
                "<tr "
                f"data-filter-status=\"{'closed' if str(item.get('status')) == 'closed' else 'open'}\" "
                f"data-filter-pod='{escape(str(item['pod']))}'>"
                f"<td>{escape(str(item.get('timestamp') or '-'))}</td>"
                f"<td>{escape(str(item['pod']))}</td>"
                f"<td>{escape(str(item['symbol']))}</td>"
                f"<td>{escape(str(item['side']))}</td>"
                f"<td>{escape(str(item['status']))}</td>"
                f"<td>{escape(str(item.get('open_reason') or '-'))}</td>"
                f"<td>{escape(str(item.get('close_reason') or '-'))}</td>"
                f"<td>{fmt_number(item.get('entry_price'), 6)}</td>"
                f"<td>{fmt_number(item.get('exit_price'), 6)}</td>"
                f"<td>{'-' if item.get('notional_usd') is None else format(float(item.get('notional_usd', 0.0)), '.2f')}</td>"
                f"<td>{_format_leverage(item.get('leverage'))}</td>"
                f"<td>{fmt_signed_usd(item.get('pnl_usd'))}</td>"
                "</tr>"
            )
            for item in event_rows
        )

    recent_activity_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(item['timestamp']))}</td>"
            f"<td>{escape(str(item['pod']))}</td>"
            f"<td>{escape(str(item['symbol']))}</td>"
            f"<td>{escape(str(item['side']))}</td>"
            f"<td>{escape(str(item['event']))}</td>"
            f"<td>{fmt_number(item.get('price'), 4)}</td>"
            f"<td>{'-' if item['notional_usd'] is None else format(float(item.get('notional_usd', 0.0)), '.2f')}</td>"
            f"<td>{_format_leverage(item.get('leverage'))}</td>"
            f"<td>{fmt_signed_usd(item.get('pnl_usd'))}</td>"
            f"<td>{escape(str(item['comment']))}</td>"
            "</tr>"
        )
        for item in recent_activity
    )
    if not recent_activity_rows:
        recent_activity_rows = (
            "<tr><td colspan='10'>Aucune exécution récente visible. "
            "Les trades apparaîtront ici dès qu'un pod écrira un trade close ou un fill récent.</td></tr>"
        )

    def render_hip4_pod_b_tab() -> str:
        payload = _hip4_outcome_monitor_payload()
        status = payload.get("status", {})
        if not isinstance(status, dict):
            status = {}
        summary = payload.get("summary", {})
        if not isinstance(summary, dict):
            summary = {}
        capital = payload.get("capital", {})
        if not isinstance(capital, dict):
            capital = {}
        balance_coin = str(capital.get("testnet_balance_coin") or "USDH")
        status_age = payload.get("status_age_seconds")
        age_label = "-" if status_age is None else _format_duration_compact(float(status_age))
        settled_position_count = int(payload.get("settled_position_count", 0) or 0)
        fill_count = int(payload.get("fill_count", 0) or 0)
        realized_pnl = payload.get("realized_pnl_usd")
        gross_pnl = payload.get("gross_pnl_usd")
        fees_usd = payload.get("fees_usd")
        report = payload.get("report", {})
        if not isinstance(report, dict):
            report = {}
        win_count = int(report.get("win_count", 0) or 0)
        loss_count = int(report.get("loss_count", 0) or 0)
        win_rate = report.get("win_rate")
        latest_edge = payload.get("latest_net_edge")
        best_edge = payload.get("best_net_edge")
        latest_short_edge = payload.get("latest_short_net_edge")
        best_short_edge = payload.get("best_short_net_edge")
        tone = "good" if bool(payload.get("fresh")) else "bad"
        open_positions = status.get("open_positions", [])
        if not isinstance(open_positions, list):
            open_positions = []
        short_brief = payload.get("short_expiry_brief", {})
        if not isinstance(short_brief, dict):
            short_brief = {}
        short_watchlist = payload.get("short_expiry_watchlist", [])
        if not isinstance(short_watchlist, list):
            short_watchlist = []

        def fmt_hip4_number(value: object, digits: int = 4, *, fallback: str = "-") -> str:
            parsed = _float_or_none(value)
            if parsed is None:
                return fallback
            return f"{parsed:.{digits}f}"

        def render_hip4_cards(cards: list[dict[str, str]]) -> str:
            return "".join(
                (
                    "<article class='metric-card'>"
                    f"<span>{escape(card['label'])}</span>"
                    f"<strong>{escape(card['value'])}</strong>"
                    f"<small>{escape(card['note'])}</small>"
                    "</article>"
                )
                for card in cards
            )

        def render_reference_rows() -> str:
            rows = payload.get("reference_prices", [])
            if not isinstance(rows, list) or not rows:
                return "<tr><td colspan='5'>Aucune référence prix visible.</td></tr>"
            return "".join(
                (
                    "<tr>"
                    f"<td>{escape(str(row.get('underlying', '-')))}</td>"
                    f"<td>{fmt_hip4_number(row.get('price'), 6)}</td>"
                    f"<td>{escape(str(row.get('source_count', 0)))}</td>"
                    f"<td>{escape(str(row.get('rejected_count', 0)))}</td>"
                    f"<td>{fmt_hip4_number(row.get('max_deviation_bps'), 2)}</td>"
                    "</tr>"
                )
                for row in rows
                if isinstance(row, dict)
            )

        def render_open_position_rows() -> str:
            if not open_positions:
                return "<tr><td colspan='9'>Aucune position HIP-4 ouverte visible.</td></tr>"
            rows: list[str] = []
            for item in open_positions:
                if not isinstance(item, dict):
                    continue
                fills = item.get("fills", [])
                first_fill = fills[0] if isinstance(fills, list) and fills and isinstance(fills[0], dict) else {}
                rows.append(
                    "<tr>"
                    f"<td>{escape(str(item.get('underlying', '-')))}</td>"
                    f"<td>{escape(str(item.get('market_id', '-')))}</td>"
                    f"<td>{escape(str(item.get('side', '-')))}</td>"
                    f"<td>{escape(str(first_fill.get('side_name') or '-'))}</td>"
                    f"<td>{fmt_hip4_number(first_fill.get('avg_price'), 4)}</td>"
                    f"<td>{fmt_hip4_number(item.get('cost_usdc'), 2)}</td>"
                    f"<td>{fmt_hip4_number(item.get('net_edge'), 4)}</td>"
                    f"<td>{fmt_hip4_number(item.get('confidence'), 4)}</td>"
                    f"<td>{escape(str(item.get('opened_at') or '-'))}</td>"
                    "</tr>"
                )
            return "".join(rows) or "<tr><td colspan='9'>Aucune position HIP-4 ouverte visible.</td></tr>"

        def render_settlement_rows() -> str:
            rows = payload.get("settlements", [])
            if not isinstance(rows, list) or not rows:
                return "<tr><td colspan='10'>Aucun settlement HIP-4 visible.</td></tr>"
            return "".join(
                (
                    "<tr>"
                    f"<td>{escape(str(row.get('ts', '-')))}</td>"
                    f"<td>{escape(str(row.get('underlying', '-')))}</td>"
                    f"<td>{escape(str(row.get('market_id', '-')))}</td>"
                    f"<td>{escape(str(row.get('side', '-')))}</td>"
                    f"<td>{escape(str(row.get('result', '-')))}</td>"
                    f"<td>{fmt_hip4_number(row.get('payout_usdc'), 2)}</td>"
                    f"<td>{fmt_hip4_number(_settlement_fee(row), 4)}</td>"
                    f"<td>{fmt_hip4_number(_settlement_gross_pnl(row), 2)}</td>"
                    f"<td>{fmt_hip4_number(_settlement_net_pnl(row), 2)}</td>"
                    f"<td>{escape(str(row.get('notes', '-')))}</td>"
                    "</tr>"
                )
                for row in reversed(rows[-12:])
                if isinstance(row, dict)
            )

        def render_opportunity_rows() -> str:
            rows = payload.get("opportunities", [])
            if not isinstance(rows, list) or not rows:
                return "<tr><td colspan='10'>Aucune opportunité loggée.</td></tr>"
            return "".join(
                (
                    "<tr>"
                    f"<td>{escape(str(row.get('ts', '-')))}</td>"
                    f"<td>{escape(str(row.get('underlying', '-')))}</td>"
                    f"<td>{escape(str(row.get('edge_type', '-')))}</td>"
                    f"<td>{escape(str(row.get('side', '-')))}</td>"
                    f"<td>{fmt_hip4_number(row.get('net_edge'), 6)}</td>"
                    f"<td>{fmt_hip4_number(row.get('gross_edge'), 6)}</td>"
                    f"<td>{fmt_hip4_number(row.get('confidence'), 4)}</td>"
                    f"<td>{fmt_hip4_number(row.get('ref_price'), 6)}</td>"
                    f"<td>{fmt_hip4_number(row.get('yes_ask'), 6)}</td>"
                    f"<td>{escape(str(row.get('reason', '-')))}</td>"
                    "</tr>"
                )
                for row in reversed(rows[-12:])
                if isinstance(row, dict)
            )

        def render_short_expiry_rows() -> str:
            rows = payload.get("short_expiry_features", [])
            if not isinstance(rows, list) or not rows:
                return "<tr><td colspan='12'>Aucun snapshot short-expiry loggé.</td></tr>"
            return "".join(
                (
                    "<tr>"
                    f"<td>{escape(str(row.get('ts', '-')))}</td>"
                    f"<td>{escape(str(row.get('underlying', '-')))}</td>"
                    f"<td>{escape(str(row.get('period', '-')))}</td>"
                    f"<td>{fmt_hip4_number(row.get('seconds_left'), 0)}</td>"
                    f"<td>{fmt_hip4_number(row.get('distance_bps'), 2)}</td>"
                    f"<td>{fmt_hip4_number(row.get('momentum_bps_60s'), 2)}</td>"
                    f"<td>{fmt_hip4_number(row.get('book_probability_yes'), 4)}</td>"
                    f"<td>{fmt_hip4_number(row.get('short_probability_yes'), 4)}</td>"
                    f"<td>{escape(str(row.get('best_side', '-')))}</td>"
                    f"<td>{fmt_hip4_number(row.get('best_net_edge'), 6)}</td>"
                    f"<td>{fmt_hip4_number(row.get('confidence'), 4)}</td>"
                    f"<td>{escape(str(row.get('reason', '-')))}</td>"
                    "</tr>"
                )
                for row in reversed(rows[-12:])
                if isinstance(row, dict)
            )

        def render_short_watchlist_rows() -> str:
            if not short_watchlist:
                return "<tr><td colspan='11'>Aucune fenêtre short-expiry surveillée dans la dernière boucle.</td></tr>"
            return "".join(
                (
                    "<tr>"
                    f"<td>{escape(str(row.get('readiness', '-')))}</td>"
                    f"<td>{escape(str(row.get('underlying', '-')))}</td>"
                    f"<td>{escape(str(row.get('period', '-')))}</td>"
                    f"<td>{fmt_hip4_number(row.get('seconds_left'), 0)}</td>"
                    f"<td>{fmt_hip4_number(row.get('reference_price'), 6)}</td>"
                    f"<td>{fmt_hip4_number(row.get('strike'), 6)}</td>"
                    f"<td>{fmt_hip4_number(row.get('distance_bps'), 2)}</td>"
                    f"<td>{fmt_hip4_number(row.get('momentum_bps_60s'), 2)}</td>"
                    f"<td>{escape(str(row.get('best_side') or '-'))}</td>"
                    f"<td>{fmt_hip4_number(row.get('best_net_edge'), 6)}</td>"
                    f"<td>{escape(str(row.get('reason', '-')))}</td>"
                    "</tr>"
                )
                for row in short_watchlist
                if isinstance(row, dict)
            )

        def render_latency_rows() -> str:
            rows = payload.get("latency", [])
            if not isinstance(rows, list) or not rows:
                return "<tr><td colspan='7'>Aucune latence loggée.</td></tr>"
            return "".join(
                (
                    "<tr>"
                    f"<td>{escape(str(row.get('ts', '-')))}</td>"
                    f"<td>{escape(str(row.get('loop_count', '-')))}</td>"
                    f"<td>{fmt_hip4_number(row.get('total_ms'), 1)}</td>"
                    f"<td>{fmt_hip4_number(row.get('reference_prices_ms'), 1)}</td>"
                    f"<td>{fmt_hip4_number(row.get('books_ms'), 1)}</td>"
                    f"<td>{fmt_hip4_number(row.get('opportunities'), 0)}</td>"
                    f"<td>{escape(str(row.get('error', '')))}</td>"
                    "</tr>"
                )
                for row in reversed(rows[-8:])
                if isinstance(row, dict)
            )

        cards = render_hip4_cards(
            [
                {
                    "label": "Runtime",
                    "value": str(payload.get("process_state", "-")),
                    "note": f"status {age_label}",
                },
                {
                    "label": "Mode",
                    "value": str(payload.get("mode", "paper")),
                    "note": "alias Pod B HIP-4",
                },
                {
                    "label": "Markets",
                    "value": f"{payload.get('markets_supported', 0)}/{payload.get('markets_seen', 0)}",
                    "note": "supportés / vus",
                },
                {
                    "label": "Positions",
                    "value": str(len(open_positions)),
                    "note": "positions HIP-4 ouvertes",
                },
                {
                    "label": "Realized PnL",
                    "value": f"{fmt_hip4_number(realized_pnl, 2)} USD",
                    "note": f"net fees · {settled_position_count} settlement(s)",
                },
                {
                    "label": "Gross/Fees",
                    "value": f"{fmt_hip4_number(gross_pnl, 2)} USD",
                    "note": f"fees {fmt_hip4_number(fees_usd, 4)} USD",
                },
                {
                    "label": "Win rate",
                    "value": fmt_pct(win_rate),
                    "note": f"{win_count} win · {loss_count} loss",
                },
                {
                    "label": "Fills",
                    "value": str(fill_count),
                    "note": "fills cumulés",
                },
                {
                    "label": "Exposure",
                    "value": f"{fmt_hip4_number(capital.get('open_exposure_usdc'), 2)} USD",
                    "note": f"budget {fmt_hip4_number(capital.get('budget_usdc'), 2)} USD",
                },
                {
                    "label": "Remaining",
                    "value": f"{fmt_hip4_number(capital.get('remaining_budget_usdc'), 2)} USD",
                    "note": str(capital.get("reason", "capital")),
                },
                {
                    "label": "Loop edge",
                    "value": fmt_hip4_number(latest_edge, 4),
                    "note": f"best {fmt_hip4_number(best_edge, 4)}",
                },
                {
                    "label": "Best short",
                    "value": fmt_hip4_number(best_short_edge, 4),
                    "note": f"latest {fmt_hip4_number(latest_short_edge, 4)}",
                },
                {
                    "label": "Short focus",
                    "value": str(short_brief.get("label", "-")),
                    "note": (
                        f"ready {short_brief.get('ready_count', 0)} · "
                        f"watch {short_brief.get('candidate_count', 0)}"
                    ),
                },
                {
                    "label": "Next window",
                    "value": (
                        _format_duration_compact(float(short_brief.get("next_window_seconds")))
                        if _float_or_none(short_brief.get("next_window_seconds")) is not None
                        else "-"
                    ),
                    "note": "short-expiry prioritaire",
                },
                {
                    "label": "Loop signals",
                    "value": str(payload.get("opportunities_this_loop", 0)),
                    "note": (
                        f"approved {payload.get('approved_this_loop', 0)} · "
                        f"exec {payload.get('executed_this_loop', 0)}"
                    ),
                },
                {
                    "label": f"Quote {balance_coin}",
                    "value": f"{fmt_hip4_number(capital.get('testnet_available_usdc'), 2)} {balance_coin}",
                    "note": "non requis en paper" if capital.get("testnet_available_usdc") is None else "balance runtime",
                },
                {
                    "label": "Last error",
                    "value": "none" if not status.get("last_error") else "error",
                    "note": str(status.get("last_error") or "runtime propre"),
                },
            ]
        )

        return f"""
      <section class="tab-panel{' is-active' if active_tab == 'pod_b' else ''}" data-tab-panel="pod_b">
        <div class="panel panel-{escape(_panel_tone(tone))}">
          <div class="panel-header">
            <h2>Pod B HIP-4 Outcome</h2>
            <p>Vue native du nouveau Pod B expérimental: marchés outcome mainnet paper, positions simulées, budget, edge court terme, latence et exécutions.</p>
          </div>
          <div class="metric-grid">
            {cards}
          </div>
        </div>

        {render_coin_trade_summary_panel(
            title="Performance par coin",
            description="Settlements exchange en testnet, estimations locales en paper, et positions encore ouvertes regroupées par underlying.",
            rows=pod_trade_summary_rows["pod_b"],
            tone=tone,
        )}

        <div class="panel panel-{escape(_panel_tone(tone))}">
          <div class="panel-header">
            <h3>Positions HIP-4 ouvertes</h3>
            <p>{escape(str(payload.get('status_path', 'logs/hip4_outcome_status.json')))}</p>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr>{_table_header("Underlying", "Sous-jacent du marché outcome.")}{_table_header("Market", "Identifiant interne du marché HIP-4 outcome.")}{_table_header("Signal", "Sens décidé par le pod.")}{_table_header("Token", "Token outcome acheté.")}{_table_header("Avg px", "Prix moyen du fill testnet ou paper.")}{_table_header("Cost quote", "Coût engagé dans la devise quote outcome.")}{_table_header("Net edge", "Edge net estimé à l'entrée.")}{_table_header("Conf", "Confiance du signal.")}{_table_header("Opened", "Horodatage d'ouverture.")}</tr></thead>
              <tbody>{render_open_position_rows()}</tbody>
            </table>
          </div>
        </div>

        <div class="panel panel-neutral">
          <div class="panel-header">
            <h3>Settlements HIP-4</h3>
            <p>En testnet, PnL repris depuis les fills Hyperliquid Settlement; en paper, estimation locale.</p>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr>{_table_header("Ts", "Horodatage du settlement.")}{_table_header("Underlying", "Sous-jacent du marché outcome.")}{_table_header("Market", "Identifiant du marché HIP-4 outcome.")}{_table_header("Side", "Sens acheté par le pod.")}{_table_header("Result", "Résultat outcome.")}{_table_header("Payout", "Payout dans la devise quote outcome.")}{_table_header("Fees", "Fees repris ou estimés au settlement.")}{_table_header("Gross PnL", "Payout moins coût, avant fees.")}{_table_header("Net PnL", "PnL net.")}{_table_header("Notes", "Source, méthode ou contexte du settlement.")}</tr></thead>
              <tbody>{render_settlement_rows()}</tbody>
            </table>
          </div>
        </div>

        <div class="pod-detail-grid">
          <div class="panel panel-{escape(_panel_tone(short_brief.get('tone')))}">
            <div class="panel-header"><h3>Short-expiry watchlist</h3><p>Fenêtres proches expiry priorisées par la dernière boucle du pod.</p></div>
            <div class="table-wrap">
              <table>
                <thead><tr><th>Status</th><th>Underlying</th><th>Period</th><th>T-exp s</th><th>Ref</th><th>Strike</th><th>Dist bps</th><th>Mom 60s</th><th>Best side</th><th>Net</th><th>Reason</th></tr></thead>
                <tbody>{render_short_watchlist_rows()}</tbody>
              </table>
            </div>
          </div>
          <div class="panel panel-neutral">
            <div class="panel-header"><h3>Signal court terme</h3><p>Snapshots du modèle court terme YES/NO.</p></div>
            <div class="table-wrap">
              <table>
                <thead><tr><th>Ts</th><th>Underlying</th><th>Period</th><th>T-exp s</th><th>Dist bps</th><th>Mom 60s</th><th>Book pY</th><th>Short pY</th><th>Best side</th><th>Net</th><th>Conf</th><th>Reason</th></tr></thead>
                <tbody>{render_short_expiry_rows()}</tbody>
              </table>
            </div>
          </div>
          <div class="panel panel-neutral">
            <div class="panel-header"><h3>Sources prix</h3><p>Références utilisées par la dernière boucle.</p></div>
            <div class="table-wrap">
              <table>
                <thead><tr><th>Underlying</th><th>Price</th><th>Sources</th><th>Rejected</th><th>Max dev bps</th></tr></thead>
                <tbody>{render_reference_rows()}</tbody>
              </table>
            </div>
          </div>
        </div>

        <div class="panel panel-neutral">
          <div class="panel-header"><h3>Opportunités récentes</h3><p>{escape(str(payload.get('logs_dir')))}</p></div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Ts</th><th>Underlying</th><th>Edge</th><th>Side</th><th>Net</th><th>Gross</th><th>Conf</th><th>Ref</th><th>Yes ask</th><th>Reason</th></tr></thead>
              <tbody>{render_opportunity_rows()}</tbody>
            </table>
          </div>
        </div>

        <div class="panel panel-neutral">
          <div class="panel-header"><h3>Latence</h3><p>Dernières boucles du collecteur HIP-4.</p></div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Ts</th><th>Loop</th><th>Total ms</th><th>Refs ms</th><th>Books ms</th><th>Opps</th><th>Error</th></tr></thead>
              <tbody>{render_latency_rows()}</tbody>
            </table>
          </div>
        </div>
      </section>"""

    pod_b_tab_panel = render_hip4_pod_b_tab()

    def render_observation_tab() -> str:
        testnet_payload = _hip4_outcome_monitor_payload()
        mainnet_payload = hip4_outcome_mainnet_payload()
        testnet_health = testnet_payload.get("market_observation_health", {})
        if not isinstance(testnet_health, dict):
            testnet_health = {}
        mainnet_health = mainnet_payload.get("market_observation_health", {})
        if not isinstance(mainnet_health, dict):
            mainnet_health = {}
        testnet_status = testnet_payload.get("status", {})
        if not isinstance(testnet_status, dict):
            testnet_status = {}
        embedded = testnet_status.get("embedded_observers", {})
        if not isinstance(embedded, dict):
            embedded = {}
        embedded_enabled = bool(embedded.get("enabled"))
        embedded_threads = int(embedded.get("running_threads", 0) or 0)
        embedded_tone = "good" if embedded_enabled and embedded_threads > 0 else "warn"

        def observation_pill(tone_value: object, label: object) -> str:
            tone_name = _panel_tone(tone_value)
            return (
                f"<span class='health-pill health-pill-{escape(tone_name)}'>"
                f"<span class='status-dot status-dot-{escape(tone_name)}'></span>"
                f"{escape(str(label or tone_name))}</span>"
            )

        def observation_cards() -> str:
            cards_html: list[str] = []
            for label, health in (
                ("Pod B mainnet paper", testnet_health),
                ("Mainnet observation", mainnet_health),
            ):
                tone_name = _panel_tone(health.get("tone"))
                cards_html.append(
                    f"<article class='observation-card observation-card-{escape(tone_name)}'>"
                    "<div class='observation-card-head'>"
                    f"<span>{escape(label)}</span>"
                    f"{observation_pill(tone_name, health.get('label', tone_name))}"
                    "</div>"
                    f"<strong>{int(health.get('count', 0) or 0)}</strong>"
                    f"<small>{escape(str(health.get('reason', '-')))}</small>"
                    f"<small>unsupported/unknown {int(health.get('unknown_count', 0) or 0)} · "
                    f"books {int(health.get('books_logged_count', 0) or 0)} · "
                    f"named {int(health.get('named_outcome_count', 0) or 0)} · "
                    f"bucket {int(health.get('price_bucket_count', 0) or 0)}</small>"
                    "</article>"
                )
            cards_html.append(
                f"<article class='observation-card observation-card-{escape(embedded_tone)}'>"
                "<div class='observation-card-head'>"
                "<span>Sidecar mainnet</span>"
                f"{observation_pill(embedded_tone, 'embedded' if embedded_enabled else 'off')}"
                "</div>"
                f"<strong>{embedded_threads}</strong>"
                "<small>thread(s) dans le process Pod B</small>"
                f"<small>{escape(', '.join(str(item) for item in embedded.get('config_paths', []) if item) or '-')}</small>"
                "</article>"
            )
            return "".join(cards_html)

        def observation_summary_rows() -> str:
            rendered_rows: list[str] = []
            for profile_name, health in (
                ("pod_b_mainnet_paper", testnet_health),
                ("mainnet", mainnet_health),
            ):
                class_counts = health.get("by_class")
                if not isinstance(class_counts, dict):
                    continue
                support_counts = health.get("by_support_status")
                if not isinstance(support_counts, dict):
                    support_counts = {}
                for class_name, count in sorted(
                    class_counts.items(),
                    key=lambda item: (-int(item[1]), str(item[0])),
                ):
                    rendered_rows.append(
                        "<tr>"
                        f"<td>{escape(profile_name)}</td>"
                        f"<td>{observation_pill(health.get('tone'), health.get('label'))}</td>"
                        f"<td>{escape(str(class_name))}</td>"
                        f"<td>{int(count or 0)}</td>"
                        f"<td>{escape(_format_count_map(support_counts))}</td>"
                        f"<td>{escape(str(health.get('latest_ts') or '-'))}</td>"
                        "</tr>"
                    )
            if not rendered_rows:
                return "<tr><td colspan='6'>Aucune synthèse d'observation disponible.</td></tr>"
            return "".join(rendered_rows)

        def observation_detail_rows() -> str:
            rendered_rows: list[str] = []
            for profile_name, source in (("pod_b_mainnet_paper", testnet_payload), ("mainnet", mainnet_payload)):
                rows = source.get("market_observations", [])
                if not isinstance(rows, list):
                    continue
                for row in rows[:50]:
                    if not isinstance(row, dict):
                        continue
                    tone_payload = _hip4_observation_row_tone(row)
                    rendered_rows.append(
                        "<tr>"
                        f"<td>{escape(profile_name)}</td>"
                        f"<td>{observation_pill(tone_payload.get('tone'), tone_payload.get('label'))}</td>"
                        f"<td>{escape(str(row.get('ts', '-')))}</td>"
                        f"<td>{escape(str(row.get('class_name', '-')))}</td>"
                        f"<td>{escape(str(row.get('support_status', '-')))}</td>"
                        f"<td>{escape(str(row.get('name', '-')))}</td>"
                        f"<td>{escape(str(row.get('underlying') or '-'))}</td>"
                        f"<td>{escape(_format_observation_bucket(row))}</td>"
                        f"<td>{escape(_format_observation_list(row.get('coins')))}</td>"
                        f"<td>{escape(_format_observation_books(row))}</td>"
                        f"<td>{escape(str(row.get('support_reason') or '-'))}</td>"
                        "</tr>"
                    )
            if not rendered_rows:
                return "<tr><td colspan='11'>Aucune observation HIP-4 loggée pour le moment.</td></tr>"
            return "".join(rendered_rows)

        return f"""
      <section class="tab-panel{' is-active' if active_tab == 'observation' else ''}" data-tab-panel="observation">
        <div class="panel panel-neutral">
          <div class="panel-header">
            <h2>Observation</h2>
            <p>Vue directe des marchés HIP-4 vus par Pod B et par le sidecar mainnet. Les pastilles indiquent si l'observation est exploitable, watch-only, ou en erreur book.</p>
          </div>
          <div class="observation-grid">
            {observation_cards()}
          </div>
        </div>

        <div class="panel panel-neutral">
          <div class="panel-header">
            <h3>Synthèse observation HIP-4</h3>
            <p>Vert: classe reconnue et exploitable en observation. Jaune: watch-only ou marché non supporté. Rouge: erreur book.</p>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>{_table_header("Profile", "Source de l'observation: Pod B mainnet paper ou mainnet observer.")}{_table_header("État", "Pastille de santé synthétique.")}{_table_header("Classe", "Classe HIP-4 observée.")}{_table_header("Count", "Nombre de lignes récentes dans market_observations.jsonl.")}{_table_header("Support", "Répartition des statuts supportés / watch-only.")}{_table_header("Latest", "Dernier timestamp observé dans ce profil.")}</tr>
              </thead>
              <tbody>{observation_summary_rows()}</tbody>
            </table>
          </div>
        </div>

        <div class="panel panel-neutral">
          <div class="panel-header">
            <h3>Marchés observés</h3>
            <p>Dernières lignes `market_observations.jsonl`, incluant namedOutcome, priceBucket, classes inconnues, coins et books visibles.</p>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>{_table_header("Profile", "Source de la ligne.")}{_table_header("État", "État de l'observation.")}{_table_header("Ts", "Horodatage de l'observation.")}{_table_header("Classe", "Classe HIP-4 parsée.")}{_table_header("Support", "Statut supporté par le bot.")}{_table_header("Nom", "Nom brut du marché.")}{_table_header("Underlying", "Sous-jacent si connu.")}{_table_header("Bucket", "Bande ou thresholds si priceBucket.")}{_table_header("Coins", "Outcome coins observés.")}{_table_header("Books", "Bid/ask résumé sur YES/NO.")}{_table_header("Reason", "Raison du statut support/watch.")}</tr>
              </thead>
              <tbody>{observation_detail_rows()}</tbody>
            </table>
          </div>
        </div>
      </section>"""

    observation_tab_panel = render_observation_tab()

    runtime_report_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(item['pod']))}</td>"
            f"<td>{_status_badge('good' if bool(item['healthy']) else 'bad', 'healthy' if bool(item['healthy']) else 'degraded')}</td>"
            f"<td>{escape(str(item['process_state'] or '-'))}</td>"
            f"<td>{int(item['position_count'])}</td>"
            f"<td>{int(item['open_order_count'])}</td>"
            f"<td>{int(item['total_fill_count'])}</td>"
            f"<td>{fmt_pct(item.get('win_rate'))}</td>"
            f"<td>{float(item['realized_pnl_usd']):.4f}</td>"
            f"<td>{float(item['total_unrealized_pnl_usd']):.4f}</td>"
            "</tr>"
        )
        for item in runtime_report["pods"]
    )
    runtime_service_report_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(item['label']))}</td>"
            f"<td>{_status_badge('good' if bool(item['healthy']) else 'bad', 'healthy' if bool(item['healthy']) else 'degraded')}</td>"
            f"<td>{escape(str(item.get('process_state') or '-'))}</td>"
            f"<td>{int(item.get('symbol_count', 0))}</td>"
            f"<td>{int(item.get('polls_completed', 0))}</td>"
            f"<td>{int(item.get('records_written', 0))}</td>"
            f"<td>{escape(str(item.get('last_collected_at') or '-'))}</td>"
            f"<td>{escape(str(item.get('output_path') or '-'))}</td>"
            "</tr>"
        )
        for item in runtime_service_rows
        if bool(item.get("enabled", True))
    ) or "<tr><td colspan='8'>Aucun collector runtime actif visible.</td></tr>"
    pod_c_scope_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(symbol)}</td>"
            f"<td>{'oui' if symbol in observed_symbols else 'non'}</td>"
            f"<td>{'oui' if symbol in tradable_symbols else 'non'}</td>"
            f"<td>{escape(str(next((item.get('owner') for item in pod_c_routing_rows if str(item.get('symbol')).upper() == symbol), '-')))}</td>"
            f"<td>{escape(str(next((item.get('reason') for item in pod_c_routing_rows if str(item.get('symbol')).upper() == symbol), '-')))}</td>"
            "</tr>"
        )
        for symbol in pod_c_scope_symbols
    ) or "<tr><td colspan='5'>Aucun symbole Pod C derive de l'univers observe.</td></tr>"
    ownership_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(item['symbol']))}</td>"
            f"<td>{escape(str(item['owner'] or 'unassigned'))}</td>"
            f"<td>{escape(str(item.get('override_owner') or '-'))}</td>"
            f"<td>{escape(str(item.get('routing_mode') or '-'))}</td>"
            f"<td>{escape(str(item.get('routing_reason') or '-'))}</td>"
            "</tr>"
        )
        for item in snapshot["symbol_ownership"]
    ) or "<tr><td colspan='5'>Aucune ownership visible.</td></tr>"
    local_regime_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(item.get('symbol') or '-'))}</td>"
            f"<td>{escape(str(item.get('local_regime') or '-'))}</td>"
            f"<td>{escape(str(item.get('global_alignment') or '-'))}</td>"
            f"<td>{escape(str(item.get('owner') or 'unassigned'))}</td>"
            f"<td>{escape(str(item.get('override_owner') or '-'))}</td>"
            f"<td>{escape(str(item.get('reassignment_count') or 0))}</td>"
            f"<td>{escape(str(item.get('reason') or '-'))}</td>"
            "</tr>"
        )
        for item in snapshot.get("local_regime_by_symbol", [])
        if isinstance(item, dict)
    ) or "<tr><td colspan='7'>Aucun état local visible.</td></tr>"
    routing_override_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(symbol))}</td>"
            f"<td>{escape(str(owner))}</td>"
            f"<td>config</td>"
            "</tr>"
        )
        for symbol, owner in sorted(
            (
                snapshot.get("routing_overrides", {}).get("config", {})
                if isinstance(snapshot.get("routing_overrides"), dict)
                else {}
            ).items()
        )
    )
    runtime_override_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(symbol))}</td>"
            f"<td>{escape(str(owner))}</td>"
            f"<td>runtime</td>"
            "</tr>"
        )
        for symbol, owner in sorted(
            (
                snapshot.get("routing_overrides", {}).get("runtime", {})
                if isinstance(snapshot.get("routing_overrides"), dict)
                else {}
            ).items()
        )
    )
    routing_override_rows = (
        routing_override_rows + runtime_override_rows
    ) or "<tr><td colspan='3'>Aucun override de routing actif.</td></tr>"
    routing_override_meta = (
        snapshot.get("routing_overrides", {})
        if isinstance(snapshot.get("routing_overrides"), dict)
        else {}
    )
    routing_override_runtime_updated_at = escape(
        str(routing_override_meta.get("runtime_updated_at") or "-")
    )
    routing_override_runtime_path = escape(
        str(routing_override_meta.get("runtime_path") or "-")
    )
    routing_override_mutation_enabled = _routing_override_enabled()
    routing_override_auth_configured = _ui_basic_auth_credentials() is not None
    if routing_override_mutation_enabled and routing_override_auth_configured:
        routing_override_controls = """
            <form class="inline-form" data-routing-override-form>
              <div class="field-stack">
                <label for="routing-override-symbol">Symbol</label>
                <input id="routing-override-symbol" name="symbol" type="text" placeholder="SOL" autocomplete="off">
              </div>
              <div class="field-stack">
                <label for="routing-override-owner">Owner</label>
                <select id="routing-override-owner" name="owner">
                  <option value="pod_a">pod_a</option>
                  <option value="pod_b">pod_b</option>
                  <option value="pod_c">pod_c</option>
                </select>
              </div>
              <button class="action-button" type="submit">Set runtime pin</button>
              <button class="action-button secondary" type="button" data-routing-override-clear>Clear pin</button>
            </form>
            <div class="inline-status" data-routing-override-status>
              Utilisez ce panneau pour forcer un symbole en live sans redémarrer le supervisor.
            </div>
        """
    elif routing_override_mutation_enabled:
        routing_override_controls = """
            <div class="inline-status">
              Les modifications runtime exigent TRIDENT_UI_AUTH_USERNAME et TRIDENT_UI_AUTH_PASSWORD avant d'afficher le panneau d'action.
            </div>
        """
    else:
        routing_override_controls = """
            <div class="inline-status">
              Les modifications runtime sont désactivées sur cette UI. Définir TRIDENT_ROUTING_OVERRIDE_ENABLED=true pour réactiver ce panneau.
            </div>
        """
    routing_decision_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(item.get('symbol') or '-'))}</td>"
            f"<td>{escape(str(item.get('owner') or 'unassigned'))}</td>"
            f"<td>{escape(str(item.get('previous_owner') or '-'))}</td>"
            f"<td>{escape(str(item.get('mode') or '-'))}</td>"
            f"<td>{escape(', '.join(item.get('candidate_pods', [])) if isinstance(item.get('candidate_pods'), list) else '-')}</td>"
            f"<td>{'<br>'.join(f'{escape(str(pod))}: {float(score):.2f}' for pod, score in sorted((item.get('pod_scores') or {}).items())) if isinstance(item.get('pod_scores'), dict) and item.get('pod_scores') else '-'}</td>"
            f"<td>{escape(str(item.get('local_regime') or '-'))}</td>"
            f"<td>{'on' if bool(item.get('reassignment_cooldown_active')) else 'off'}</td>"
            f"<td>{escape(str(item.get('override_owner') or '-'))}</td>"
            f"<td>{'<br>'.join(f'{escape(str(pod))}: {escape(str(reason))}' for pod, reason in sorted((item.get('pod_reasoning') or {}).items())) if isinstance(item.get('pod_reasoning'), dict) and item.get('pod_reasoning') else '-'}</td>"
            f"<td>{escape(str(item.get('reason') or '-'))}</td>"
            "</tr>"
        )
        for item in snapshot.get("symbol_routing", [])
        if isinstance(item, dict)
    ) or "<tr><td colspan='11'>Aucune décision de routing visible.</td></tr>"
    conflict_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(conflict['symbol']))}</td>"
            f"<td>{escape(str(conflict['requested_by']))}</td>"
            f"<td>{escape(str(conflict['owner']))}</td>"
            "</tr>"
        )
        for conflict in snapshot["ownership_conflicts"]
    ) or "<tr><td colspan='3'>Aucun conflit d'ownership.</td></tr>"
    regime_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(item['recorded_at']))}</td>"
            f"<td>{escape(str(item['previous_regime']))}</td>"
            f"<td>{escape(str(item['new_regime']))}</td>"
            f"<td>{fmt_number(item['snapshot'].get('adx'), 2)}</td>"
            f"<td>{fmt_number(item['snapshot'].get('atr_ratio'), 2)}</td>"
            "</tr>"
        )
        for item in snapshot["regime_history"]
        if isinstance(item, dict)
    ) or "<tr><td colspan='5'>Aucune transition de régime enregistrée.</td></tr>"
    metric_rows = "".join(
        (
            "<tr>"
            f"<td>{escape(str(name))}</td>"
            f"<td>{escape(str(value))}</td>"
            "</tr>"
        )
        for name, value in snapshot["metrics"].items()
    )

    tabs = [
        ("status", "Status"),
        ("stats", "Stats"),
        ("pod_a", "Pod A"),
        ("pod_b", "Pod B HIP-4"),
        ("observation", "Observation"),
        ("pod_c", "Pod C"),
        ("activity", "Activity"),
        ("system", "System"),
    ]
    tab_nav = "".join(
        (
            f"<button class='tab-button{' is-active' if key == active_tab else ''}' "
            f"type='button' data-tab-button='{key}' aria-selected='{'true' if key == active_tab else 'false'}'>"
            f"{escape(label)}</button>"
        )
        for key, label in tabs
    )

    return f"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{
      --bg: #efe6d8;
      --panel: rgba(255, 251, 244, 0.94);
      --panel-strong: #fffdf9;
      --text: #1f2a33;
      --muted: #66727c;
      --line: #d8ccbb;
      --accent: #145b57;
      --accent-soft: #d8eeeb;
      --good: #176b3a;
      --good-soft: #ddf5e5;
      --warn: #9a6700;
      --warn-soft: #fff0cc;
      --bad: #a12d2f;
      --bad-soft: #ffe1e1;
      --neutral: #6a7680;
      --neutral-soft: #edf0f2;
      --shadow: 0 18px 40px rgba(31, 42, 51, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--text);
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top left, #fff4dc, transparent 28%),
        radial-gradient(circle at top right, #d8eeeb, transparent 22%),
        linear-gradient(180deg, #f4ecdf 0%, var(--bg) 100%);
    }}
    main {{
      max-width: 1380px;
      margin: 0 auto;
      padding: 28px 18px 48px;
    }}
    h1, h2, h3 {{ margin: 0; }}
    p {{ margin: 0; color: var(--muted); }}
    .hero {{
      background: linear-gradient(135deg, rgba(255,253,249,0.95), rgba(248,241,230,0.92));
      border: 1px solid var(--line);
      border-radius: 28px;
      box-shadow: var(--shadow);
      padding: 24px;
      display: grid;
      gap: 16px;
    }}
    .eyebrow {{
      font-family: "Fraunces", "Iowan Old Style", Georgia, serif;
      font-size: 0.95rem;
      color: var(--accent);
      letter-spacing: 0.03em;
    }}
    .chip-row, .hero-links, .tab-nav, .filter-row {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
    }}
    .chip, .meta-chip {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 7px 12px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent);
      font-size: 0.9rem;
      font-weight: 600;
    }}
    .hero h1 {{
      font-family: "Fraunces", "Iowan Old Style", Georgia, serif;
      font-size: clamp(2rem, 4vw, 3.4rem);
      line-height: 1.02;
      letter-spacing: -0.03em;
    }}
    .hero-copy {{
      max-width: 760px;
      line-height: 1.55;
    }}
    .hero-links a {{
      color: var(--accent);
      text-decoration: none;
      font-weight: 700;
    }}
    .hero-links a:hover {{
      text-decoration: underline;
    }}
    .tab-shell {{
      margin-top: 20px;
      display: grid;
      gap: 16px;
    }}
    .tab-nav {{
      background: rgba(255, 253, 249, 0.84);
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 8px;
      box-shadow: 0 10px 24px rgba(31, 42, 51, 0.05);
      position: sticky;
      top: 12px;
      z-index: 10;
      backdrop-filter: blur(12px);
    }}
    .tab-button {{
      border: 0;
      background: transparent;
      color: var(--muted);
      border-radius: 999px;
      padding: 10px 14px;
      font-weight: 700;
      font-size: 0.95rem;
      cursor: pointer;
      transition: background 120ms ease, color 120ms ease, transform 120ms ease;
    }}
    .tab-button:hover {{
      color: var(--text);
      transform: translateY(-1px);
    }}
    .tab-button.is-active {{
      background: var(--accent);
      color: #fff;
      box-shadow: 0 10px 20px rgba(20, 91, 87, 0.18);
    }}
    .tab-panel {{
      display: none;
      gap: 16px;
    }}
      .tab-panel.is-active {{
      display: grid;
    }}
    .panel, .status-card, .metric-card, .pod-card, .observation-card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 22px;
      box-shadow: var(--shadow);
    }}
    .panel {{
      padding: 20px;
      overflow: hidden;
    }}
    .panel-header {{
      display: grid;
      gap: 6px;
      margin-bottom: 16px;
    }}
    .panel-header p {{
      max-width: 820px;
      line-height: 1.5;
    }}
    .status-grid, .metric-grid, .pod-grid, .pod-detail-grid {{
      display: grid;
      gap: 14px;
    }}
    .focus-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
      gap: 14px;
    }}
    .status-grid {{
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    }}
    .metric-grid {{
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    }}
    .pod-grid {{
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    }}
    .pod-detail-grid {{
      grid-template-columns: 1.1fr 0.9fr;
      align-items: start;
    }}
    .status-card, .metric-card, .pod-card {{
      padding: 18px;
    }}
    .observation-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 14px;
    }}
    .observation-card {{
      border-radius: 8px;
      padding: 18px;
      display: grid;
      gap: 8px;
    }}
    .observation-card-good {{
      background: linear-gradient(180deg, rgba(248, 255, 250, 0.97), rgba(255, 251, 244, 0.97));
      border-color: rgba(23, 107, 58, 0.18);
    }}
    .observation-card-warn {{
      background: linear-gradient(180deg, rgba(255, 251, 240, 0.97), rgba(255, 251, 244, 0.97));
      border-color: rgba(154, 103, 0, 0.20);
    }}
    .observation-card-bad {{
      background: linear-gradient(180deg, rgba(255, 245, 245, 0.97), rgba(255, 251, 244, 0.97));
      border-color: rgba(161, 45, 47, 0.20);
    }}
    .observation-card-neutral {{
      background: linear-gradient(180deg, rgba(246, 248, 249, 0.97), rgba(255, 251, 244, 0.97));
      border-color: rgba(106, 118, 128, 0.18);
    }}
    .observation-card-head {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
    }}
    .observation-card-head span:first-child {{
      color: var(--muted);
      font-weight: 800;
    }}
    .observation-card strong {{
      font-size: 1.7rem;
      line-height: 1.1;
      font-family: "Fraunces", "Iowan Old Style", Georgia, serif;
    }}
    .observation-card small {{
      color: var(--muted);
      line-height: 1.4;
    }}
    .health-pill {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
      border-radius: 999px;
      padding: 6px 10px;
      font-size: 0.84rem;
      font-weight: 800;
      line-height: 1;
    }}
    .health-pill-good {{ background: var(--good-soft); color: var(--good); }}
    .health-pill-warn {{ background: var(--warn-soft); color: var(--warn); }}
    .health-pill-bad {{ background: var(--bad-soft); color: var(--bad); }}
    .health-pill-neutral {{ background: var(--neutral-soft); color: var(--neutral); }}
    .status-dot {{
      width: 9px;
      height: 9px;
      border-radius: 999px;
      display: inline-block;
      flex: 0 0 auto;
    }}
    .status-dot-good {{ background: var(--good); }}
    .status-dot-warn {{ background: var(--warn); }}
    .status-dot-bad {{ background: var(--bad); }}
    .status-dot-neutral {{ background: var(--neutral); }}
    .panel-good {{
      background: linear-gradient(180deg, rgba(248, 255, 250, 0.98), rgba(255, 251, 244, 0.97));
      border-color: rgba(23, 107, 58, 0.20);
    }}
    .panel-warn {{
      background: linear-gradient(180deg, rgba(255, 251, 240, 0.98), rgba(255, 251, 244, 0.97));
      border-color: rgba(154, 103, 0, 0.22);
    }}
    .panel-bad {{
      background: linear-gradient(180deg, rgba(255, 245, 245, 0.98), rgba(255, 251, 244, 0.97));
      border-color: rgba(161, 45, 47, 0.22);
    }}
    .panel-neutral {{
      background: linear-gradient(180deg, rgba(255, 253, 249, 0.98), rgba(252, 246, 238, 0.96));
      border-color: rgba(158, 144, 118, 0.16);
    }}
    .global-banner {{
      display: grid;
      gap: 12px;
      padding: 22px;
      background: linear-gradient(135deg, rgba(255,253,249,0.96), rgba(240,247,246,0.92));
    }}
    .global-banner-good {{
      background: linear-gradient(135deg, rgba(248, 255, 250, 0.98), rgba(231, 247, 236, 0.94));
      border-color: rgba(23, 107, 58, 0.22);
    }}
    .global-banner-warn {{
      background: linear-gradient(135deg, rgba(255, 251, 237, 0.98), rgba(255, 243, 209, 0.94));
      border-color: rgba(154, 103, 0, 0.24);
    }}
    .global-banner-bad {{
      background: linear-gradient(135deg, rgba(255, 247, 247, 0.98), rgba(255, 229, 229, 0.94));
      border-color: rgba(161, 45, 47, 0.24);
    }}
    .global-banner h2 {{
      font-family: "Fraunces", "Iowan Old Style", Georgia, serif;
      font-size: clamp(1.6rem, 3vw, 2.4rem);
      line-height: 1.05;
    }}
    .global-banner p {{
      max-width: 780px;
      line-height: 1.55;
    }}
    .status-head {{
      margin-bottom: 12px;
    }}
    .status-card-good {{
      background: linear-gradient(180deg, rgba(248, 255, 250, 0.96), rgba(255, 251, 244, 0.96));
      border-color: rgba(23, 107, 58, 0.18);
    }}
    .status-card-warn {{
      background: linear-gradient(180deg, rgba(255, 251, 240, 0.96), rgba(255, 251, 244, 0.96));
      border-color: rgba(154, 103, 0, 0.20);
    }}
    .status-card-bad {{
      background: linear-gradient(180deg, rgba(255, 245, 245, 0.96), rgba(255, 251, 244, 0.96));
      border-color: rgba(161, 45, 47, 0.20);
    }}
    .metric-card strong {{
      display: block;
      margin-top: 6px;
      font-size: 1.5rem;
      line-height: 1.1;
      font-family: "Fraunces", "Iowan Old Style", Georgia, serif;
    }}
    .metric-card small {{
      display: block;
      margin-top: 8px;
      color: var(--muted);
      line-height: 1.4;
    }}
    .pod-card-head {{
      display: flex;
      align-items: flex-start;
      gap: 12px;
      margin-bottom: 14px;
    }}
    .pod-card-head p {{
      margin-top: 6px;
      line-height: 1.45;
    }}
    .pod-card-good {{
      background: linear-gradient(180deg, rgba(248, 255, 250, 0.97), rgba(255, 251, 244, 0.97));
      border-color: rgba(23, 107, 58, 0.18);
    }}
    .pod-card-warn {{
      background: linear-gradient(180deg, rgba(255, 251, 240, 0.97), rgba(255, 251, 244, 0.97));
      border-color: rgba(154, 103, 0, 0.20);
    }}
    .pod-card-bad {{
      background: linear-gradient(180deg, rgba(255, 245, 245, 0.97), rgba(255, 251, 244, 0.97));
      border-color: rgba(161, 45, 47, 0.20);
    }}
    .pod-card-neutral {{
      background: linear-gradient(180deg, rgba(246, 248, 249, 0.97), rgba(255, 251, 244, 0.97));
      border-color: rgba(106, 118, 128, 0.18);
    }}
    .dot {{
      width: 12px;
      height: 12px;
      border-radius: 999px;
      margin-top: 6px;
      flex: none;
    }}
    .dot-good {{ background: var(--good); box-shadow: 0 0 0 6px var(--good-soft); }}
    .dot-warn {{ background: var(--warn); box-shadow: 0 0 0 6px var(--warn-soft); }}
    .dot-bad {{ background: var(--bad); box-shadow: 0 0 0 6px var(--bad-soft); }}
    .dot-neutral {{ background: var(--neutral); box-shadow: 0 0 0 6px var(--neutral-soft); }}
    .pod-card-meta {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 14px;
    }}
    .pod-facts {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px 14px;
      margin: 0 0 14px;
    }}
    .pod-facts div {{
      border-top: 1px solid var(--line);
      padding-top: 10px;
    }}
    .pod-facts dt {{
      font-size: 0.8rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--muted);
      margin-bottom: 6px;
    }}
    .pod-facts dd {{
      margin: 0;
      font-weight: 600;
      line-height: 1.45;
    }}
    .tab-link {{
      border: 0;
      background: transparent;
      color: var(--accent);
      padding: 0;
      font-weight: 700;
      cursor: pointer;
    }}
    .tab-link:hover {{
      text-decoration: underline;
    }}
    .badge {{
      display: inline-block;
      padding: 5px 10px;
      border-radius: 999px;
      font-size: 0.86rem;
      font-weight: 700;
    }}
    .badge-good {{ background: var(--good-soft); color: var(--good); }}
    .badge-warn {{ background: var(--warn-soft); color: var(--warn); }}
    .badge-bad {{ background: var(--bad-soft); color: var(--bad); }}
    .badge-neutral {{ background: var(--neutral-soft); color: var(--neutral); }}
    .soft-note {{
      color: var(--muted);
      line-height: 1.55;
    }}
    .inline-form {{
      display: grid;
      grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr) auto auto;
      gap: 10px;
      align-items: end;
    }}
    .field-stack {{
      display: grid;
      gap: 6px;
    }}
    .field-stack label {{
      font-size: 0.82rem;
      font-weight: 700;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .field-stack input,
    .field-stack select {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 12px;
      background: #fff;
      padding: 11px 12px;
      color: var(--text);
      font: inherit;
    }}
    .action-button {{
      border: 0;
      border-radius: 12px;
      padding: 11px 14px;
      font-weight: 700;
      cursor: pointer;
      background: var(--accent);
      color: #fff;
    }}
    .action-button.secondary {{
      background: rgba(143, 103, 71, 0.12);
      color: var(--accent);
      border: 1px solid rgba(143, 103, 71, 0.22);
    }}
    .inline-status {{
      margin-top: 12px;
      padding: 10px 12px;
      border-radius: 12px;
      background: rgba(143, 103, 71, 0.08);
      color: var(--muted);
      min-height: 22px;
      line-height: 1.45;
    }}
    .focus-item {{
      display: grid;
      gap: 12px;
      padding: 18px;
      border-radius: 18px;
      background: var(--panel-strong);
      border: 1px solid rgba(216, 204, 187, 0.75);
    }}
    .focus-item-top {{
      display: flex;
      gap: 12px;
      align-items: flex-start;
    }}
    .focus-item-top strong {{
      display: block;
      line-height: 1.35;
    }}
    .focus-item-top small {{
      display: block;
      margin-top: 6px;
      color: var(--muted);
      line-height: 1.45;
    }}
    .focus-tag {{
      justify-self: start;
      padding: 5px 10px;
      border-radius: 999px;
      font-size: 0.82rem;
      font-weight: 700;
    }}
    .focus-tag-good {{
      background: var(--good-soft);
      color: var(--good);
    }}
    .focus-tag-warn {{
      background: var(--warn-soft);
      color: var(--warn);
    }}
    .focus-tag-bad {{
      background: var(--bad-soft);
      color: var(--bad);
    }}
    .simple-list {{
      margin: 0;
      padding-left: 18px;
      color: var(--muted);
      line-height: 1.55;
    }}
    .table-wrap {{
      overflow-x: auto;
      border-radius: 16px;
      border: 1px solid rgba(216, 204, 187, 0.55);
      background: var(--panel-strong);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.95rem;
      min-width: 780px;
    }}
    th, td {{
      text-align: left;
      padding: 12px 10px;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
    }}
    th {{
      color: var(--muted);
      font-weight: 700;
      background: rgba(244, 236, 223, 0.45);
    }}
    .th-with-tooltip {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      position: relative;
    }}
    .tooltip-trigger {{
      width: 18px;
      height: 18px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: #fff;
      color: var(--accent);
      font-size: 0.72rem;
      font-weight: 700;
      line-height: 1;
      padding: 0;
      cursor: help;
    }}
    .tooltip-bubble {{
      position: absolute;
      left: 0;
      top: calc(100% + 8px);
      width: min(260px, 42vw);
      padding: 10px 12px;
      border-radius: 12px;
      background: #1f2a33;
      color: #fff;
      font-size: 0.84rem;
      line-height: 1.4;
      box-shadow: 0 12px 24px rgba(31, 42, 51, 0.18);
      opacity: 0;
      pointer-events: none;
      transform: translateY(-4px);
      transition: opacity 120ms ease, transform 120ms ease;
      z-index: 20;
    }}
    .tooltip-bubble::before {{
      content: "";
      position: absolute;
      left: 12px;
      top: -6px;
      width: 12px;
      height: 12px;
      background: #1f2a33;
      transform: rotate(45deg);
    }}
    .th-with-tooltip:hover .tooltip-bubble,
    .th-with-tooltip:focus-within .tooltip-bubble {{
      opacity: 1;
      transform: translateY(0);
    }}
    .filter-row {{
      margin-bottom: 12px;
    }}
    .filter-chip {{
      border: 1px solid var(--line);
      background: #fffaf0;
      color: var(--text);
      border-radius: 999px;
      padding: 7px 12px;
      font-size: 0.92rem;
      font-weight: 600;
      cursor: pointer;
      transition: transform 120ms ease, background 120ms ease, color 120ms ease, border-color 120ms ease;
    }}
    .filter-chip:hover {{
      transform: translateY(-1px);
      border-color: var(--accent);
    }}
    .filter-chip.is-active {{
      background: var(--accent);
      color: #fff;
      border-color: var(--accent);
    }}
    .is-hidden {{
      display: none;
    }}
    @media (max-width: 980px) {{
      .inline-form {{
        grid-template-columns: 1fr;
      }}
      .pod-detail-grid {{
        grid-template-columns: 1fr;
      }}
      .pod-facts {{
        grid-template-columns: 1fr;
      }}
      .tab-nav {{
        position: static;
      }}
    }}
  </style>
</head>
<body data-default-tab="{escape(active_tab)}" data-refresh-seconds="{refresh_seconds}">
  <main>
    <header class="hero">
      <div class="chip-row">
        <span class="chip">Version {escape(VERSION)}</span>
        <span class="chip">Profile {escape(str(snapshot['profile']))}</span>
        <span class="chip">Mode {escape(str(snapshot['mode']))}</span>
        <span class="chip">Régime {escape(str(snapshot['regime']))}</span>
        <span class="chip">Actif depuis {escape(uptime_label)}</span>
        <span class="chip">Auto-refresh {refresh_seconds}s</span>
      </div>
      <div class="eyebrow">TRIDENT Supervisor Dashboard</div>
      <h1>{escape(title)}</h1>
      <p class="hero-copy">{escape(subtitle)}</p>
      <div class="hero-links">
        <span>Last updated: {escape(refreshed_at)}</span>
        <a href="/dashboard">/dashboard</a>
        <a href="/trades">/trades</a>
        <a href="/hip4-outcome">/hip4-outcome</a>
        <a href="/api/state">/api/state</a>
        <a href="/api/report">/api/report</a>
        <a href="/api/metrics">/api/metrics</a>
      </div>
    </header>

    <div class="tab-shell">
      <nav class="tab-nav" aria-label="Navigation principale">
        {tab_nav}
      </nav>

      <section class="tab-panel{' is-active' if active_tab == 'status' else ''}" data-tab-panel="status">
        <div class="panel global-banner global-banner-{escape(global_tone)}">
          <div class="panel-header">
            <h2>{escape(global_label)}</h2>
            <p>{escape(commentary)}</p>
          </div>
          <div>
            {_status_badge(global_tone, global_label)}
            <span class="meta-chip">Régime {escape(str(snapshot['regime']))}</span>
            <span class="meta-chip">{healthy_count}/{enabled_count} pod(s) sain(s)</span>
            <span class="meta-chip">{active_positions} position(s)</span>
            <span class="meta-chip">{active_orders} ordre(s)</span>
          </div>
        </div>

        <div class="panel panel-{escape(_panel_tone(focus_tone))}">
          <div class="panel-header">
            <h2>À faire maintenant</h2>
            <p>Liste courte et concrète. Si tu ne dois lire qu'un bloc sur cette page, c'est celui-ci.</p>
          </div>
          <div class="focus-grid">
            {focus_rows}
          </div>
        </div>

        <div class="panel panel-{escape(_panel_tone(global_tone))}">
          <div class="panel-header">
            <h2>Status</h2>
            <p>État général très compact : collecte, santé des pods, ownership et activité récente. Le détail est volontairement renvoyé vers les onglets de pod et système.</p>
          </div>
          <div class="status-grid">
            {status_rows}
          </div>
        </div>

        <div class="panel panel-neutral">
          <div class="panel-header">
            <h2>En un coup d’œil</h2>
            <p>Quelques chiffres seulement : régime, santé, exposition, exécutions et cash disponible.</p>
          </div>
          <div class="metric-grid">
            {summary_cards}
          </div>
        </div>

        <div class="panel panel-neutral">
          <div class="panel-header">
            <h2>Régimes par cluster</h2>
            <p>Vue compacte du régime crypto global et des régimes actifs par cluster non-crypto. C'est ici qu'on voit rapidement si un cluster Tradfi peut activer Pod C même quand le crypto reste en DeadZone.</p>
          </div>
          <div class="metric-grid">
            {cluster_regime_cards}
          </div>
        </div>

        <div class="panel panel-neutral">
          <div class="panel-header">
            <h2>Pods</h2>
            <p>Chaque carte dit simplement si le pod est OK, à surveiller, ou s'il mérite qu'on ouvre son onglet détail.</p>
          </div>
          <div class="pod-grid">
            {pod_cards}
          </div>
        </div>

        {render_coin_trade_summary_panel(
            title="Performance globale par coin",
            description="Synthèse tous pods confondus, agrégée par coin et par sens. Le Pod B HIP-4 est normalisé en long pour BUY_YES et short pour BUY_NO.",
            rows=global_trade_summary_rows,
            include_pods=True,
            tone=global_tone,
        )}
      </section>

      <section class="tab-panel{' is-active' if active_tab == 'stats' else ''}" data-tab-panel="stats">
        <div class="panel panel-neutral">
          <div class="panel-header">
            <h2>Stats</h2>
            <p>Vue temporelle par pod et par coin sur les fenêtres utiles pour décider si un edge se dessine.</p>
          </div>
          <div class="metric-grid">
            {render_stat_cards([
                {"label": "Fenêtres", "value": "24h · 3j · 1w", "note": "Stats calculées depuis le runtime visible"},
                {"label": "Pods", "value": str(len([pod for pod in pod_summaries if bool(pod['enabled'])])), "note": "Pods actifs inclus dans les stats"},
                {"label": "Trades", "value": str(total_fills), "note": "Trades fermés visibles tous pods"},
                {"label": "Win rate global", "value": fmt_pct(_win_rate_from_counts(sum(int(pod['win_count']) for pod in pod_summaries), sum(int(pod['loss_count']) for pod in pod_summaries))), "note": "Cumul runtime visible"},
                {"label": "Capital ciblé", "value": f"{float(runtime_report.get('total_target_usd', 0.0)):.2f} USD", "note": "Somme des budgets par pod"},
                {"label": "PnL visible", "value": f"{float(runtime_report.get('realized_pnl_usd', 0.0)) + float(runtime_report.get('total_unrealized_pnl_usd', 0.0)):.4f} USD", "note": "Réalisé + latent visible"},
            ])}
          </div>
        </div>

        <div class="panel panel-neutral">
          <div class="panel-header">
            <h3>Stats par pod</h3>
            <p>Capital, trades, win rate et PnL visibles par pod sur 24h, 3 jours et 1 semaine.</p>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>{_table_header("Fenêtre", "Période glissante calculée à partir des timestamps visibles.")}{_table_header("Pod", "Pod concerné.")}{_table_header("Capital", "Budget cible du pod, ou budget HIP-4 pour Pod B.")}{_table_header("Trades", "Trades fermés dans la fenêtre.")}{_table_header("Ouvert", "Positions encore ouvertes maintenant.")}{_table_header("Win", "Trades gagnants fermés dans la fenêtre.")}{_table_header("Loss", "Trades perdants fermés dans la fenêtre.")}{_table_header("Win rate", "Win / trades fermés avec PnL connu.")}{_table_header("PnL réalisé", "PnL net des trades fermés dans la fenêtre.")}{_table_header("PnL latent", "PnL latent courant des positions ouvertes.")}{_table_header("PnL visible", "PnL réalisé de la fenêtre + latent courant visible.")}</tr>
              </thead>
              <tbody>{render_stats_rows(stats_by_pod_rows, include_coin=False)}</tbody>
            </table>
          </div>
        </div>

        <div class="panel panel-neutral">
          <div class="panel-header">
            <h3>Stats par coin</h3>
            <p>Lecture fine par sous-jacent et par sens pour vérifier quels coins contribuent vraiment au résultat.</p>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>{_table_header("Fenêtre", "Période glissante calculée à partir des timestamps visibles.")}{_table_header("Pod", "Pod concerné.")}{_table_header("Coin", "Sous-jacent ou symbole tradé.")}{_table_header("Sens", "Long, short ou mixed selon les données disponibles.")}{_table_header("Capital", "Budget du pod, répété pour contextualiser le coin.")}{_table_header("Trades", "Trades fermés dans la fenêtre.")}{_table_header("Ouvert", "Positions encore ouvertes maintenant.")}{_table_header("Win", "Trades gagnants fermés dans la fenêtre.")}{_table_header("Loss", "Trades perdants fermés dans la fenêtre.")}{_table_header("Win rate", "Win / trades fermés avec PnL connu.")}{_table_header("PnL réalisé", "PnL net des trades fermés dans la fenêtre.")}{_table_header("PnL latent", "PnL latent courant des positions ouvertes.")}{_table_header("PnL visible", "PnL réalisé de la fenêtre + latent courant visible.")}</tr>
              </thead>
              <tbody>{render_stats_rows(stats_by_coin_rows, include_coin=True)}</tbody>
            </table>
          </div>
        </div>
      </section>

      <section class="tab-panel{' is-active' if active_tab == 'pod_a' else ''}" data-tab-panel="pod_a">
        <div class="panel panel-{escape(_panel_tone(pod_a_summary['tone']))}">
          <div class="panel-header">
            <h2>Pod A</h2>
            <p>Pod directionnel trend / structure. On suit ici les positions ouvertes, les trades fermés, le levier, le stop et la raison exacte d'ouverture / fermeture.</p>
          </div>
          <div class="metric-grid">
            {render_stat_cards([
                {"label": "Status", "value": str(pod_a_summary["badge"]), "note": str(pod_a_summary["comment"])},
                {"label": "Target", "value": f"{float(pod_a_summary['target_usd']):.2f} USD", "note": f"{float(pod_a_summary['target_pct']):.2f} du capital"},
                {"label": "Open positions", "value": str(pod_a_summary['position_count']), "note": "Positions directionnelles ouvertes"},
                {"label": "Signals", "value": str(pod_a_summary['preview_count']), "note": "Previews actuellement visibles"},
                {"label": "Exec", "value": str(pod_a_summary['total_fill_count']), "note": "Trades/fills observés"},
                {"label": "Win rate", "value": fmt_pct(pod_a_summary.get("win_rate")), "note": f"{pod_a_summary['win_count']} win · {pod_a_summary['loss_count']} loss"},
                {"label": "Realized PnL", "value": f"{float(pod_a_summary['realized_pnl_usd']):.4f} USD", "note": "Cumul runtime"},
            ])}
          </div>
        </div>

        <div class="pod-detail-grid">
          {render_coin_trade_summary_panel(
            title="Performance par coin",
            description="Trades Pod A fermés et positions encore ouvertes, regroupés par coin et par sens.",
            rows=pod_trade_summary_rows["pod_a"],
            tone=pod_a_summary["tone"],
        )}
          <div class="panel panel-neutral">
            <div class="panel-header">
              <h3>Signal preview</h3>
              <p>Signaux vus par le superviseur mais pas encore forcément convertis en position.</p>
            </div>
            {render_preview_list(snapshot.get("pod_a_signal_preview"))}
            <div class="panel-header" style="margin-top:16px">
              <h3>Pourquoi filtré</h3>
              <p>Derniers symboles vus mais bloqués avant émission de signal.</p>
            </div>
            {render_review_list(snapshot.get("pod_a_signal_review"))}
          </div>
        </div>

        <div class="pod-detail-grid">
          <div class="panel panel-{escape(_panel_tone(pod_a_summary['tone']))}">
            <div class="panel-header">
              <h3>Trades ouverts</h3>
              <p>Ce tableau sert a lire la position telle qu'elle vit maintenant: prix courant, valeur actuelle, marge immobilisee, niveaux TP/SL et etat du trailing.</p>
            </div>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>{_table_header("Symbol", "Marché actuellement détenu par le pod.")}{_table_header("Side", "Sens de la position: long si le pod gagne sur une hausse, short s'il gagne sur une baisse.")}{_table_header("Raison ouverture", "Nom lisible du setup qui a ouvert le trade. C'est la logique d'entree initiale, pas l'etat actuel du trade.")}{_table_header("Prix entree", "Prix moyen d'entree retenu par le moteur dry-run au moment de l'ouverture.")}{_table_header("Prix courant", "Dernier prix vu dans le snapshot live pour ce symbole. C'est la reference utilisee pour valoriser le trade maintenant.")}{_table_header("Valeur courante USD", "Valeur notionnelle actuelle de la position au prix courant. Elle peut bouger meme si la taille unitaire ne change pas.")}{_table_header("Marge utilisee", "Capital immobilise pour porter la position. C'est plus utile operatoirement que la notionnelle brute.")}{_table_header("Prix TP", "Prix theorique auquel le take profit fixe sortirait le trade s'il etait touche maintenant.")}{_table_header("Prix SL", "Prix de stop loss actuel. Quand une invalidation structurelle existe, on l'affiche directement; sinon on reconstruit le stop a partir des bps.")}{_table_header("Unrealized PnL", "PnL latent marque au dernier prix courant. Ce n'est pas realise tant que le trade n'est pas ferme.")}{_table_header("Trailing TP", "Etat du trailing: non configure, en attente d'activation, ou actif avec le niveau actuel du stop suiveur.")}{_table_header("Ouvert le", "Horodatage d'ouverture du trade pour juger son age reel.")}</tr>
                </thead>
                <tbody>{render_directional_open_rows("pod_a")}</tbody>
              </table>
            </div>
          </div>
        </div>

        <div class="panel panel-neutral">
          <div class="panel-header">
            <h3>Trades fermés récents</h3>
            <p>Les raisons d'ouverture et de fermeture sont maintenant formulees en langage lisible, pour comprendre rapidement ce qui a declenche l'entree puis la sortie.</p>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>{_table_header("Ferme le", "Horodatage reel de la sortie du trade.")}{_table_header("Symbol", "Marche concerne par le trade ferme.")}{_table_header("Side", "Sens du trade qui a ete porte: long ou short.")}{_table_header("Raison ouverture", "Setup lisible qui avait justifie l'entree du trade au depart.")}{_table_header("Raison fermeture", "Explication lisible de la sortie: TP touche, trailing stop, stop loss, time stop, signal oppose, etc.")}{_table_header("Prix entree", "Prix moyen d'entree du trade au moment de l'ouverture.")}{_table_header("Prix sortie", "Prix de sortie effectivement retenu lors de la cloture.")}{_table_header("Notional USD", "Notionnelle cible du trade au moment ou il a ete ouvert.")}{_table_header("Leverage", "Levier effectif configure pour ce trade si l'information est disponible.")}{_table_header("PnL USD", "Resultat net du trade, frais inclus.")}</tr>
              </thead>
              <tbody>{render_directional_closed_rows("pod_a")}</tbody>
            </table>
          </div>
        </div>
      </section>

      {pod_b_tab_panel}
      {observation_tab_panel}

      <section class="tab-panel{' is-active' if active_tab == 'pod_c' else ''}" data-tab-panel="pod_c">
        <div class="panel panel-{escape(_panel_tone(pod_c_summary['tone']))}">
          <div class="panel-header">
            <h2>Pod C</h2>
            <p>Pod opportuniste event / lead-lag. On suit les positions, la logique d'ouverture, puis la raison de fermeture comme sur Pod A.</p>
          </div>
          <div class="metric-grid">
            {render_stat_cards([
                {"label": "Status", "value": str(pod_c_summary["badge"]), "note": str(pod_c_summary["comment"])},
                {"label": "Target", "value": f"{float(pod_c_summary['target_usd']):.2f} USD", "note": f"{float(pod_c_summary['target_pct']):.2f} du capital"},
                {"label": "Open positions", "value": str(pod_c_summary['position_count']), "note": "Positions event-driven ouvertes"},
                {"label": "Signals", "value": str(pod_c_summary['preview_count']), "note": "Previews actuellement visibles"},
                {"label": "Exec", "value": str(pod_c_summary['total_fill_count']), "note": "Trades/fills observés"},
                {"label": "Win rate", "value": fmt_pct(pod_c_summary.get("win_rate")), "note": f"{pod_c_summary['win_count']} win · {pod_c_summary['loss_count']} loss"},
                {"label": "Realized PnL", "value": f"{float(pod_c_summary['realized_pnl_usd']):.4f} USD", "note": "Cumul runtime"},
            ])}
          </div>
        </div>

        <div class="pod-detail-grid">
          {render_coin_trade_summary_panel(
            title="Performance par coin",
            description="Trades Pod C fermés et positions event encore ouvertes, regroupés par coin et par sens.",
            rows=pod_trade_summary_rows["pod_c"],
            tone=pod_c_summary["tone"],
        )}
          <div class="panel panel-neutral">
            <div class="panel-header">
              <h3>Signal preview</h3>
              <p>Signaux event / lead-lag vus mais pas encore transformés en position.</p>
            </div>
            {render_preview_list(snapshot.get("pod_c_signal_preview"))}
            <div class="panel-header" style="margin-top:16px">
              <h3>Pourquoi filtré</h3>
              <p>Derniers symboles Pod C vus mais bloqués avant émission de signal.</p>
            </div>
            {render_review_list(snapshot.get("pod_c_signal_review"))}
          </div>
        </div>

        <div class="pod-detail-grid">
          <div class="panel panel-{escape(_panel_tone(pod_c_summary['tone']))}">
            <div class="panel-header">
              <h3>Trades ouverts</h3>
              <p>On lit ici les positions Tradfi vivantes avec les niveaux operatoires vraiment utiles: prix live, valeur, marge, TP, SL et trailing.</p>
            </div>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>{_table_header("Symbol", "Marche Tradfi actuellement porte par Pod C.")}{_table_header("Side", "Sens de la position: long a la hausse, short a la baisse.")}{_table_header("Raison ouverture", "Setup lisible qui a motive l'ouverture initiale du trade.")}{_table_header("Prix entree", "Prix moyen d'entree retenu au moment de l'ouverture.")}{_table_header("Prix courant", "Dernier prix live vu par le runner pour ce symbole.")}{_table_header("Valeur courante USD", "Valorisation actuelle de la position au dernier prix courant.")}{_table_header("Marge utilisee", "Capital immobilise pour porter ce trade. C'est la mesure pratique de l'exposition engagee.")}{_table_header("Prix TP", "Prix theorique du take profit fixe si la cible est atteinte.")}{_table_header("Prix SL", "Prix du stop de protection actuellement applicable au trade.")}{_table_header("Unrealized PnL", "PnL latent calcule au dernier prix courant. Il deviendra realise seulement a la sortie.")}{_table_header("Trailing TP", "Indique si le trailing est deja arme, et si oui a quel niveau se situe le stop suiveur actuel.")}{_table_header("Ouvert le", "Horodatage d'ouverture pour estimer l'anciennete du trade.")}</tr>
                </thead>
                <tbody>{render_directional_open_rows("pod_c")}</tbody>
              </table>
            </div>
          </div>
        </div>

        <div class="panel panel-neutral">
          <div class="panel-header">
            <h3>Trades fermés récents</h3>
            <p>Les codes internes de setup et de sortie sont traduits en formulations lisibles pour faciliter la review operatoire.</p>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>{_table_header("Ferme le", "Horodatage reel de sortie du trade.")}{_table_header("Symbol", "Marche Tradfi concerne.")}{_table_header("Side", "Sens du trade qui a ete porte.")}{_table_header("Raison ouverture", "Setup lisible qui avait motive l'ouverture du trade.")}{_table_header("Raison fermeture", "Explication lisible de la sortie: TP, trailing stop, stop, time stop, signal oppose, etc.")}{_table_header("Prix entree", "Prix moyen d'entree du trade.")}{_table_header("Prix sortie", "Prix retenu a la fermeture.")}{_table_header("Notional USD", "Notionnelle cible du trade au moment de l'ouverture.")}{_table_header("Leverage", "Levier effectif configure si disponible.")}{_table_header("PnL USD", "Resultat net final du trade, frais inclus.")}</tr>
              </thead>
              <tbody>{render_directional_closed_rows("pod_c")}</tbody>
            </table>
          </div>
        </div>
      </section>

      <section class="tab-panel{' is-active' if active_tab == 'activity' else ''}" data-tab-panel="activity">
        <div class="panel">
          <div class="panel-header">
            <h2>Activity</h2>
            <p>Onglet transversal pour les positions ouvertes et les évènements récents. Il remplace l'ancienne page de trades, mais reste organisé par filtres pour ne pas noyer la vue principale.</p>
          </div>
          <div class="filter-row" aria-label="Filtres activity">
            <button class="filter-chip is-active" type="button" data-filter-group="status" data-filter-value="open">Open</button>
            <button class="filter-chip is-active" type="button" data-filter-group="status" data-filter-value="closed">Closed</button>
            <button class="filter-chip is-active" type="button" data-filter-group="pod" data-filter-value="pod_a">Pod A</button>
            <button class="filter-chip is-active" type="button" data-filter-group="pod" data-filter-value="pod_c">Pod C</button>
          </div>
          <p class="soft-note" style="margin-bottom:16px;">Les trois pods sont maintenant lus de manière homogène: positions ouvertes, previews et trades fermés.</p>

          <div class="panel" style="box-shadow:none;">
            <div class="panel-header">
              <h3>Open positions</h3>
              <p>Ce qui est en risque maintenant, tous pods confondus.</p>
            </div>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>{_table_header("Pod", "Pod qui porte actuellement la position.")}{_table_header("Symbol", "Marché concerné.")}{_table_header("Side", "Sens actuel de la position.")}{_table_header("Open reason", "Pourquoi la position a été ouverte.")}{_table_header("Entry", "Prix d'entrée connu ou prix moyen.")}{_table_header("Notional USD", "Valeur notionnelle actuelle ou cible.")}{_table_header("Leverage", "Levier configuré quand disponible.")}{_table_header("Confidence", "Confiance du signal directionnel quand elle existe.")}{_table_header("Stop bps", "Distance du stop pour les pods directionnels.")}{_table_header("Time stop h", "Durée maximale de détention prévue.")}{_table_header("Opened at", "Horodatage d'ouverture si connu.")}</tr>
                </thead>
                <tbody>{render_activity_open_rows()}</tbody>
              </table>
            </div>
          </div>

          <div class="panel" style="box-shadow:none;">
            <div class="panel-header">
              <h3>Recent trade events</h3>
              <p>Historique récent des sorties directionnelles sur les trois pods.</p>
            </div>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>{_table_header("Timestamp", "Horodatage de l'évènement.")}{_table_header("Pod", "Pod responsable.")}{_table_header("Symbol", "Marché concerné.")}{_table_header("Side", "Sens long/short porté par le trade.")}{_table_header("Status", "closed pour un trade fermé.")}{_table_header("Open reason", "Pourquoi la position a été initiée.")}{_table_header("Close reason", "Pourquoi le trade s'est fermé.")}{_table_header("Entry", "Prix d'entrée si connu.")}{_table_header("Exit", "Prix de sortie si connu.")}{_table_header("Notional USD", "Valeur notionnelle concernée.")}{_table_header("Leverage", "Levier configuré quand il est disponible.")}{_table_header("PnL USD", "PnL net quand disponible.")}</tr>
                </thead>
                <tbody>{render_activity_event_rows()}</tbody>
              </table>
            </div>
          </div>

          <div class="panel" style="box-shadow:none;">
            <div class="panel-header">
              <h3>Recent trading activity</h3>
              <p>Résumé mixte des derniers journaux live visibles.</p>
            </div>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>{_table_header("Timestamp", "Heure de l'évènement.")}{_table_header("Pod", "Pod responsable.")}{_table_header("Symbol", "Marché concerné.")}{_table_header("Side", "Sens de l'ordre ou de la position.")}{_table_header("Event", "Type d'évènement.")}{_table_header("Price", "Prix d'exécution ou de sortie.")}{_table_header("Notional USD", "Valeur notionnelle approx.")}{_table_header("Leverage", "Levier configuré quand il est modélisé.")}{_table_header("PnL USD", "PnL net si connu.")}{_table_header("Comment", "Contexte utile : fee, raison de sortie, etc.")}</tr>
                </thead>
                <tbody>{recent_activity_rows}</tbody>
              </table>
            </div>
          </div>
        </div>
      </section>

      <section class="tab-panel{' is-active' if active_tab == 'system' else ''}" data-tab-panel="system">
        <div class="panel">
          <div class="panel-header">
            <h2>System</h2>
            <p>Onglet réservé aux détails opératoires : ownership, conflits, transitions de régime et métriques brutes. On le sort de la vue principale pour garder l'écran Status vraiment lisible.</p>
          </div>
          <div class="pod-detail-grid">
            <div class="panel" style="box-shadow:none;">
              <div class="panel-header">
                <h3>Runtime pod report</h3>
                <p>Résumé structurel par pod.</p>
              </div>
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr>{_table_header("Pod", "Nom logique du pod.")}{_table_header("Healthy", "État runtime selon la fraîcheur du status.")}{_table_header("Process", "État du process ou runner associé.")}{_table_header("Positions", "Nombre de positions ouvertes.")}{_table_header("Open orders", "Nombre d'ordres suivis.")}{_table_header("Fills", "Nombre cumulé d'exécutions.")}{_table_header("Win rate", "Pourcentage de trades fermés gagnants du pod.")}{_table_header("Realized PnL", "PnL réalisé cumulé.")}{_table_header("Unrealized PnL", "PnL latent courant.")}</tr>
                  </thead>
                  <tbody>{runtime_report_rows}</tbody>
                </table>
              </div>
            </div>
            <div class="panel" style="box-shadow:none;">
              <div class="panel-header">
                <h3>Ownership conflicts</h3>
                <p>Conflits d'ownership à régler avant d'augmenter le risque.</p>
              </div>
              <div class="table-wrap">
                <table>
                  <thead><tr><th>Symbol</th><th>Requested by</th><th>Owner</th></tr></thead>
                  <tbody>{conflict_rows}</tbody>
                </table>
              </div>
            </div>
            <div class="panel" style="box-shadow:none;">
              <div class="panel-header">
                <h3>Data collectors</h3>
                <p>État runtime des services de collecte funding et asset context.</p>
              </div>
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr>{_table_header("Service", "Nom logique du collector.")}{_table_header("Healthy", "Fraîcheur du runtime status du collector.")}{_table_header("Process", "État du process de collecte.")}{_table_header("Symbols", "Nombre de symbols suivis.")}{_table_header("Polls", "Nombre de polls terminés.")}{_table_header("Records", "Records JSONL écrits.")}{_table_header("Last collected", "Horodatage du dernier lot collecté.")}{_table_header("Output", "Fichier de sortie courant.")}</tr>
                  </thead>
                  <tbody>{runtime_service_report_rows}</tbody>
                </table>
              </div>
            </div>
            <div class="panel" style="box-shadow:none;">
              <div class="panel-header">
                <h3>Pod C scope visibility</h3>
                <p>Cette vue derive le scope de Pod C depuis `hyperliquid.observation_universe`, puis le filtre avec `pod_c.allowed_market_clusters`. Si un symbol n'apparait pas dans Routing decisions, c'est souvent qu'il n'est pas observe ou pas tradable, pas qu'il manque dans une liste secondaire.</p>
              </div>
              <div class="metric-grid" style="margin-bottom:16px;">
                {render_stat_cards([
                    {"label": "Clusters", "value": str(len(pod_c_allowed_clusters)), "note": ", ".join(pod_c_allowed_clusters) or "-"},
                    {"label": "In Scope", "value": str(len(pod_c_scope_symbols)), "note": ", ".join(pod_c_scope_symbols) or "-"},
                    {"label": "Observed", "value": str(len([symbol for symbol in pod_c_scope_symbols if symbol in observed_symbols])), "note": "Symbols Pod C presents dans les snapshots visibles"},
                    {"label": "Tradable", "value": str(len([symbol for symbol in pod_c_scope_symbols if symbol in tradable_symbols])), "note": "Symbols Pod C qui passent les gates live"},
                    {"label": "Routed To Pod C", "value": str(len(pod_c_routed_symbols)), "note": ", ".join(pod_c_routed_symbols) or "-"},
                ])}
              </div>
              <p class="soft-note" style="margin-bottom:12px;">Non observes: {escape(", ".join(pod_c_seen_not_observed) or "-")} | Observes mais non tradables: {escape(", ".join(pod_c_observed_not_tradable) or "-")}</p>
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr>{_table_header("Symbol", "Symbole configure dans le scope Pod C.")}{_table_header("Observed", "Oui si le symbole apparait dans la vue snapshot/superviseur utilisee par l'UI au moment du rendu.")}{_table_header("Tradable", "Oui si le symbole est vu et passe les gates de tradabilite live: spread, activite, funding, etc.")}{_table_header("Current owner", "Pod actuellement assigne par le routeur, ou '-' si aucune decision visible.")}{_table_header("Routing note", "Raison de routing actuellement visible pour ce symbole. Si la ligne est vide, l'UI n'a pas de decision runtime pour ce symbol.")}</tr>
                  </thead>
                  <tbody>{pod_c_scope_rows}</tbody>
                </table>
              </div>
            </div>
          </div>

          <div class="pod-detail-grid">
            <div class="panel" style="box-shadow:none;">
              <div class="panel-header">
                <h3>Symbol ownership</h3>
                <p>Qui possède quoi en ce moment.</p>
              </div>
              <div class="table-wrap">
                <table>
                  <thead><tr><th>Symbol</th><th>Owner</th><th>Override</th><th>Routing</th><th>Reason</th></tr></thead>
                  <tbody>{ownership_rows}</tbody>
                </table>
              </div>
            </div>
            <div class="panel" style="box-shadow:none;">
              <div class="panel-header">
                <h3>Local symbol states</h3>
                <p>Lecture locale par coin, alignement global/local et compteur de réattributions.</p>
              </div>
              <div class="table-wrap">
                <table>
                  <thead><tr><th>Symbol</th><th>Local regime</th><th>Alignment</th><th>Owner</th><th>Override</th><th>Reassignments</th><th>Reason</th></tr></thead>
                  <tbody>{local_regime_rows}</tbody>
                </table>
              </div>
            </div>
          </div>

          <div class="pod-detail-grid">
            <div class="panel" style="box-shadow:none;">
              <div class="panel-header">
                <h3>Regime history</h3>
                <p>Transitions récentes du régime de marché.</p>
              </div>
              <div class="table-wrap">
                <table>
                  <thead><tr><th>Recorded at</th><th>Previous</th><th>New</th><th>ADX</th><th>ATR ratio</th></tr></thead>
                  <tbody>{regime_rows}</tbody>
                </table>
              </div>
            </div>
            <div class="panel" style="box-shadow:none;">
              <div class="panel-header">
                <h3>Local transitions</h3>
                <p>Transitions locales récentes détectées coin par coin.</p>
              </div>
              <div class="table-wrap">
                <table>
                  <thead><tr><th>Recorded at</th><th>Symbol</th><th>Previous</th><th>New</th><th>Reason</th></tr></thead>
                  <tbody>{
                    "".join(
                        (
                            "<tr>"
                            f"<td>{escape(str(item.get('recorded_at') or '-'))}</td>"
                            f"<td>{escape(str(item.get('symbol') or '-'))}</td>"
                            f"<td>{escape(str(item.get('previous_local_regime') or '-'))}</td>"
                            f"<td>{escape(str(item.get('new_local_regime') or '-'))}</td>"
                            f"<td>{escape(str(item.get('reason') or '-'))}</td>"
                            "</tr>"
                        )
                        for item in snapshot.get("local_regime_transitions", [])[-20:]
                        if isinstance(item, dict)
                    ) or "<tr><td colspan='5'>Aucune transition locale enregistrée.</td></tr>"
                  }</tbody>
                </table>
              </div>
            </div>
          </div>

          <div class="panel" style="box-shadow:none;">
            <div class="panel-header">
              <h3>Routing overrides</h3>
              <p>Overrides statiques et runtime actuellement pris en compte par le routeur. Runtime file: <code>{routing_override_runtime_path}</code>. Last runtime update: <code>{routing_override_runtime_updated_at}</code>.</p>
            </div>
            {routing_override_controls}
            <div class="table-wrap">
              <table>
                <thead><tr><th>Symbol</th><th>Owner</th><th>Source</th></tr></thead>
                <tbody>{routing_override_rows}</tbody>
              </table>
            </div>
          </div>

          <div class="panel" style="box-shadow:none;">
            <div class="panel-header">
              <h3>Routing decisions</h3>
              <p>Vue détaillée des candidats, scores et raisons par symbole pour analyser le choix effectif.</p>
            </div>
            <div class="table-wrap">
              <table>
                <thead><tr><th>Symbol</th><th>Owner</th><th>Previous</th><th>Mode</th><th>Candidates</th><th>Scores</th><th>Local regime</th><th>Cooldown</th><th>Override</th><th>Reasoning</th><th>Decision</th></tr></thead>
                <tbody>{routing_decision_rows}</tbody>
              </table>
            </div>
          </div>

          <div class="panel" style="box-shadow:none;">
            <div class="panel-header">
              <h3>Metrics</h3>
              <p>Métriques brutes exposées par l'API d'observabilité.</p>
            </div>
            <div class="table-wrap">
              <table>
                <thead><tr><th>Name</th><th>Value</th></tr></thead>
                <tbody>{metric_rows}</tbody>
              </table>
            </div>
          </div>
        </div>
      </section>
    </div>
  </main>
  <script>
    (() => {{
      const body = document.body;
      const refreshSeconds = Number(body.dataset.refreshSeconds || "0");
      const buttons = Array.from(document.querySelectorAll("[data-tab-button]"));
      const panels = Array.from(document.querySelectorAll("[data-tab-panel]"));
      const validTabs = new Set(
        buttons.map((button) => button.dataset.tabButton).filter(Boolean)
      );
      const jumpButtons = Array.from(document.querySelectorAll("[data-jump-tab]"));
      const activeFilters = {{
        status: new Set(["open", "closed"]),
        pod: new Set(["pod_a", "pod_b", "pod_c"]),
      }};
      const filterButtons = Array.from(document.querySelectorAll("[data-filter-group]"));
      const filterRows = Array.from(document.querySelectorAll("tr[data-filter-status][data-filter-pod]"));
      const routingOverrideForm = document.querySelector("[data-routing-override-form]");
      const routingOverrideStatus = document.querySelector("[data-routing-override-status]");
      const routingOverrideClearButton = document.querySelector("[data-routing-override-clear]");

      function normalizedTab(tabName) {{
        return validTabs.has(tabName) ? tabName : body.dataset.defaultTab || "status";
      }}

      function activeTabName() {{
        return normalizedTab((window.location.hash || "").replace("#", ""));
      }}

      function scrollStorageKey(tabName) {{
        return `trident:scroll:${{window.location.pathname}}:${{normalizedTab(tabName)}}`;
      }}

      function saveScrollPosition(tabName = activeTabName()) {{
        try {{
          window.sessionStorage.setItem(scrollStorageKey(tabName), String(window.scrollY || 0));
        }} catch (_error) {{
          // Ignore storage failures and keep refresh behavior intact.
        }}
      }}

      function restoreScrollPosition(tabName = activeTabName()) {{
        try {{
          const raw = window.sessionStorage.getItem(scrollStorageKey(tabName));
          if (raw === null) return;
          const next = Number(raw);
          if (!Number.isFinite(next) || next < 0) return;
          window.requestAnimationFrame(() => {{
            window.scrollTo(0, next);
          }});
        }} catch (_error) {{
          // Ignore storage failures and keep refresh behavior intact.
        }}
      }}

      function setTab(tabName, updateHash = true) {{
        const next = normalizedTab(tabName);
        buttons.forEach((button) => {{
          const active = button.dataset.tabButton === next;
          button.classList.toggle("is-active", active);
          button.setAttribute("aria-selected", active ? "true" : "false");
        }});
        panels.forEach((panel) => {{
          panel.classList.toggle("is-active", panel.dataset.tabPanel === next);
        }});
        if (updateHash) {{
          history.replaceState(null, "", `#${{next}}`);
        }}
      }}

      function refreshFilterRows() {{
        filterRows.forEach((row) => {{
          const status = row.dataset.filterStatus;
          const pod = row.dataset.filterPod;
          const visible = activeFilters.status.has(status) && activeFilters.pod.has(pod);
          row.classList.toggle("is-hidden", !visible);
        }});
      }}

      buttons.forEach((button) => {{
        button.addEventListener("click", () => {{
          setTab(button.dataset.tabButton);
        }});
      }});

      jumpButtons.forEach((button) => {{
        button.addEventListener("click", () => {{
          const key = button.dataset.jumpTab || "";
          const mapping = {{
            "pod_a": "pod_a",
            "pod_b": "pod_b",
            "pod_c": "pod_c",
          }};
          setTab(mapping[key] || key);
        }});
      }});

      filterButtons.forEach((button) => {{
        button.addEventListener("click", () => {{
          const group = button.dataset.filterGroup;
          const value = button.dataset.filterValue;
          const set = activeFilters[group];
          if (!set) return;
          if (set.has(value)) {{
            if (set.size === 1) return;
            set.delete(value);
            button.classList.remove("is-active");
          }} else {{
            set.add(value);
            button.classList.add("is-active");
          }}
          refreshFilterRows();
        }});
      }});

      async function submitRoutingOverride(ownerValue) {{
        if (!routingOverrideForm || !routingOverrideStatus) return;
        const formData = new window.FormData(routingOverrideForm);
        const symbol = String(formData.get("symbol") || "").trim().toUpperCase();
        if (!symbol) {{
          routingOverrideStatus.textContent = "Symbol requis pour modifier un pin runtime.";
          return;
        }}
        const owner =
          ownerValue === null
            ? null
            : String(ownerValue || formData.get("owner") || "").trim().toLowerCase();
        routingOverrideStatus.textContent = owner === null
          ? `Suppression du pin runtime pour ${{symbol}}...`
          : `Application du pin runtime ${{symbol}} -> ${{owner}}...`;
        try {{
          const response = await window.fetch("/api/routing/override", {{
            method: "POST",
            headers: {{
              "Content-Type": "application/json",
            }},
            body: JSON.stringify({{ symbol, owner }}),
          }});
          const payload = await response.json();
          if (!response.ok || !payload.ok) {{
            routingOverrideStatus.textContent = `Échec update runtime pin: ${{payload.error || response.status}}`;
            return;
          }}
          routingOverrideStatus.textContent = owner === null
            ? `Pin runtime supprimé pour ${{symbol}}. Rafraîchissement en cours...`
            : `Pin runtime appliqué: ${{symbol}} -> ${{payload.owner || owner}}. Rafraîchissement en cours...`;
          saveScrollPosition("system");
          window.setTimeout(() => {{
            window.location.hash = "#system";
            window.location.reload();
          }}, 450);
        }} catch (_error) {{
          routingOverrideStatus.textContent = "Erreur réseau pendant la mise à jour du pin runtime.";
        }}
      }}

      if (routingOverrideForm) {{
        routingOverrideForm.addEventListener("submit", (event) => {{
          event.preventDefault();
          void submitRoutingOverride(undefined);
        }});
      }}

      if (routingOverrideClearButton) {{
        routingOverrideClearButton.addEventListener("click", () => {{
          void submitRoutingOverride(null);
        }});
      }}

      const hashTab = (window.location.hash || "").replace("#", "");
      setTab(hashTab || body.dataset.defaultTab || "status", false);
      refreshFilterRows();
      restoreScrollPosition(hashTab || body.dataset.defaultTab || "status");
      window.addEventListener("beforeunload", () => {{
        saveScrollPosition();
      }});
      window.addEventListener("hashchange", () => {{
        const next = (window.location.hash || "").replace("#", "");
        if (validTabs.has(next)) {{
          setTab(next, false);
        }}
      }});

      if (Number.isFinite(refreshSeconds) && refreshSeconds > 0) {{
        window.setTimeout(() => {{
          saveScrollPosition();
          const target = `${{window.location.pathname}}${{window.location.search}}${{window.location.hash}}`;
          window.location.replace(target);
        }}, refreshSeconds * 1000);
      }}
    }})();
  </script>
</body>
</html>"""


_legacy_control_center_html = _control_center_html


def _control_center_html(
    supervisor: TridentSupervisor,
    metrics: MetricsRegistry,
    *,
    active_tab: str,
    title: str,
    subtitle: str,
) -> str:
    snapshot = state_payload(supervisor, metrics, include_stats=False)
    runtime_report = snapshot.get("runtime_report", {})
    if not isinstance(runtime_report, dict):
        runtime_report = {}
    refreshed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    refresh_seconds = 10
    display_pods = TRIDENT_UI_PODS
    status_items = _dashboard_status_items(snapshot, runtime_report, pod_names=display_pods)
    commentary = _dashboard_commentary(snapshot, runtime_report, pod_names=display_pods)
    exchange_payload = snapshot.get("exchange", {})
    if not isinstance(exchange_payload, dict):
        exchange_payload = {}
    exchange_network = str(exchange_payload.get("network") or "-")
    visible_pod_set = set(display_pods)
    open_rows = [
        row
        for row in _open_position_rows(snapshot)
        if str(row.get("pod")) in visible_pod_set
    ]
    event_rows = [
        row
        for row in _trade_event_rows(snapshot)
        if str(row.get("pod")) in visible_pod_set
    ]
    opportunity_rows = {
        pod_name: _recent_directional_opportunity_rows(snapshot, pod=pod_name)
        for pod_name in display_pods
    }
    pod_trade_summary_rows = {
        pod_name: _pod_trade_summary(snapshot.get(f"{pod_name}_runtime"), pod=pod_name)
        for pod_name in display_pods
    }
    health_map = {
        str(item.get("pod")): item
        for item in snapshot.get("pod_health", [])
        if isinstance(item, dict) and item.get("pod") is not None
    }
    runtime_pod_map = {
        str(item.get("pod")): item
        for item in runtime_report.get("pods", [])
        if isinstance(item, dict) and item.get("pod") is not None
    }
    pod_c_allowed_clusters = sorted(
        normalize_cluster_names(supervisor.config.pod_c.allowed_market_clusters)
    )
    pod_c_scope_symbols = symbols_in_allowed_clusters(
        supervisor.config,
        observation_universe_symbols(supervisor.config),
        supervisor.config.pod_c.allowed_market_clusters,
    )
    cluster_regimes = {
        str(cluster).strip().lower(): str(regime)
        for cluster, regime in snapshot.get("cluster_regimes", {}).items()
        if str(cluster).strip()
    }
    cluster_target_allocations = {
        str(cluster).strip().lower(): float(target_pct)
        for cluster, target_pct in snapshot.get("cluster_target_allocations", {}).items()
        if str(cluster).strip()
    }
    observed_status_rows = [
        item
        for item in snapshot.get("observed_symbol_status", [])
        if isinstance(item, dict) and item.get("symbol") is not None
    ]
    observed_count_by_cluster: dict[str, int] = {}
    tradable_count_by_cluster: dict[str, int] = {}
    for item in observed_status_rows:
        symbol = str(item.get("symbol", "")).upper()
        if not symbol:
            continue
        cluster = cluster_for_symbol(supervisor.config, symbol)
        observed_count_by_cluster[cluster] = observed_count_by_cluster.get(cluster, 0) + 1
        if bool(item.get("tradable")):
            tradable_count_by_cluster[cluster] = tradable_count_by_cluster.get(cluster, 0) + 1
    scope_count_by_cluster: dict[str, int] = {}
    for symbol in pod_c_scope_symbols:
        cluster = cluster_for_symbol(supervisor.config, symbol)
        scope_count_by_cluster[cluster] = scope_count_by_cluster.get(cluster, 0) + 1

    def fmt_number(value: object, digits: int = 2, *, fallback: str = "-") -> str:
        parsed = _float_or_none(value)
        if parsed is None:
            return fallback
        return f"{parsed:.{digits}f}"

    def fmt_signed_usd(value: object, digits: int = 4) -> str:
        parsed = _float_or_none(value)
        if parsed is None:
            return "-"
        return f"{parsed:+.{digits}f}"

    def fmt_pct(value: object, digits: int = 1) -> str:
        parsed = _float_or_none(value)
        if parsed is None:
            return "-"
        return f"{parsed * 100:.{digits}f}%"

    def hl_capital_note() -> str:
        mode = str(runtime_report.get("account_mode") or "mode inconnu")
        source = str(runtime_report.get("hl_capital_source") or "source inconnue")
        spot_total = fmt_number(runtime_report.get("spot_usdc_total"), 2)
        spot_hold = fmt_number(runtime_report.get("spot_usdc_hold"), 2)
        if source == "unified_spot_usdc":
            return f"{mode} · spot {spot_total} · hold {spot_hold}"
        if source == "perp_withdrawable":
            return f"{mode} · marge perp disponible"
        return f"{mode} · {source}"

    def runtime_mode(pod_name: str) -> str:
        runtime_payload = snapshot.get(f"{pod_name}_runtime")
        if isinstance(runtime_payload, dict) and runtime_payload.get("mode") not in (None, ""):
            return str(runtime_payload["mode"])
        pods = snapshot.get("pods", {})
        pod_cfg = pods.get(pod_name, {}) if isinstance(pods, dict) else {}
        if isinstance(pod_cfg, dict) and pod_cfg.get("runtime_mode") not in (None, ""):
            return str(pod_cfg["runtime_mode"])
        return str(snapshot.get("mode") or "-")

    def normalized_network(value: object) -> str:
        normalized = str(value or "").strip().lower()
        if "testnet" in normalized:
            return "testnet"
        if "mainnet" in normalized:
            return "mainnet"
        return normalized if normalized in {"mainnet", "testnet"} else "-"

    def pod_network(pod_name: str) -> str:
        if pod_name in {"pod_a", "pod_c"}:
            return normalized_network(exchange_network)
        payloads = [
            snapshot.get(f"{pod_name}_runtime"),
            snapshot.get(f"{pod_name}_status"),
            snapshot.get("pod_b_status") if pod_name == "pod_b" else None,
        ]
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            if pod_name == "pod_b" and not runtime_status_is_fresh(payload):
                continue
            for key in ("network", "exchange_network", "info_url", "logs_dir", "status_path"):
                candidate = normalized_network(payload.get(key))
                if candidate in {"mainnet", "testnet"}:
                    return candidate
            if str(payload.get("mode") or "").strip().lower() == "testnet":
                return "testnet"
        return normalized_network(exchange_network)

    def network_label(network: object) -> str:
        normalized = normalized_network(network)
        if normalized == "testnet":
            return "Testnet"
        if normalized == "mainnet":
            return "Mainnet"
        return "-"

    def mode_tone(mode: object) -> str:
        normalized = str(mode or "").lower()
        if normalized == "live":
            return "good"
        if normalized in {"dry-run", "paper", "observer", "observation"}:
            return "neutral"
        return "warn"

    def render_metric_cards(cards: list[dict[str, object]]) -> str:
        return "".join(
            (
                "<article class='metric-card'>"
                f"<span>{escape(str(card.get('label', '-')))}</span>"
                f"<strong>{escape(str(card.get('value', '-')))}</strong>"
                f"<small>{escape(str(card.get('note', '-')))}</small>"
                "</article>"
            )
            for card in cards
        )

    def pod_summary(pod_name: str) -> dict[str, object]:
        pods_cfg = snapshot.get("pods", {})
        pod_cfg = pods_cfg.get(pod_name, {}) if isinstance(pods_cfg, dict) else {}
        pod_report = runtime_pod_map.get(pod_name, {})
        pod_health = health_map.get(pod_name, {})
        enabled = bool(
            pod_cfg.get("enabled", pod_report.get("enabled", False))
            if isinstance(pod_cfg, dict)
            else pod_report.get("enabled", False)
        )
        healthy = bool(pod_report.get("healthy", pod_health.get("healthy", False)))
        process_state = _display_process_state(pod_report.get("process_state"))
        if not enabled:
            tone = "neutral"
            badge = "Off"
            comment = "Pod coupé."
        elif healthy:
            tone = "good"
            badge = "OK"
            comment = "Runtime frais."
        elif process_state in {"running", "completed"}:
            tone = "warn"
            badge = "Check"
            comment = str(pod_health.get("message") or "Pod à vérifier.")
        else:
            tone = "bad"
            badge = "KO"
            comment = str(pod_health.get("message") or "Statut runtime absent ou obsolète.")
        position_count = int(pod_report.get("position_count", 0) or 0)
        preview_count = int(pod_report.get("preview_count", 0) or 0)
        if enabled and position_count > 0:
            comment = f"{position_count} position(s) ouverte(s)."
        elif enabled and preview_count > 0:
            comment = f"{preview_count} signal(aux), 0 position."
        owned_symbols = pod_report.get(
            "owned_symbols",
            pod_cfg.get("owned_symbols", []) if isinstance(pod_cfg, dict) else [],
        )
        if not isinstance(owned_symbols, list):
            owned_symbols = []
        return {
            "pod": pod_name,
            "label": "Pod B HIP-4" if pod_name == "pod_b" else _pod_label(pod_name),
            "enabled": enabled,
            "tone": tone,
            "badge": badge,
            "comment": comment,
            "mode": runtime_mode(pod_name),
            "network": pod_network(pod_name),
            "process_state": process_state,
            "owned_symbols": [str(symbol) for symbol in owned_symbols],
            "target_pct": float(
                pod_report.get(
                    "target_pct",
                    pod_cfg.get("target_pct", 0.0) if isinstance(pod_cfg, dict) else 0.0,
                )
                or 0.0
            ),
            "target_usd": float(
                pod_report.get(
                    "target_usd",
                    pod_cfg.get("target_usd", 0.0) if isinstance(pod_cfg, dict) else 0.0,
                )
                or 0.0
            ),
            "preview_count": preview_count,
            "position_count": position_count,
            "open_order_count": int(pod_report.get("open_order_count", 0) or 0),
            "total_fill_count": int(pod_report.get("total_fill_count", 0) or 0),
            "realized_pnl_usd": float(pod_report.get("realized_pnl_usd", 0.0) or 0.0),
            "total_unrealized_pnl_usd": float(
                pod_report.get("total_unrealized_pnl_usd", 0.0) or 0.0
            ),
            "win_count": int(pod_report.get("win_count", 0) or 0),
            "loss_count": int(pod_report.get("loss_count", 0) or 0),
            "win_rate": pod_report.get("win_rate"),
        }

    summaries = {pod: pod_summary(pod) for pod in display_pods}
    pod_a_summary = summaries["pod_a"]
    pod_c_summary = summaries["pod_c"]
    ac_realized = pod_a_summary["realized_pnl_usd"] + pod_c_summary["realized_pnl_usd"]
    ac_unrealized = (
        pod_a_summary["total_unrealized_pnl_usd"]
        + pod_c_summary["total_unrealized_pnl_usd"]
    )
    ac_visible_pnl = ac_realized + ac_unrealized
    ac_win_count = int(pod_a_summary["win_count"]) + int(pod_c_summary["win_count"])
    ac_loss_count = int(pod_a_summary["loss_count"]) + int(pod_c_summary["loss_count"])
    ac_closed_count = ac_win_count + ac_loss_count
    ac_win_rate = ac_win_count / ac_closed_count if ac_closed_count > 0 else None
    capital_used_usd = sum(
        _float_or_none(item.get("margin_usd")) or 0.0
        for item in open_rows
    )
    if capital_used_usd <= 0.0:
        capital_used_usd = _float_or_none(runtime_report.get("total_margin_used_usd")) or 0.0
    open_position_count = len(open_rows)
    closed_event_count = sum(1 for item in event_rows if str(item.get("status")) == "closed")
    enabled_count = sum(1 for item in summaries.values() if bool(item.get("enabled", False)))
    healthy_count = sum(
        1
        for item in summaries.values()
        if bool(item.get("enabled", False)) and str(item.get("tone")) == "good"
    )
    conflict_count = (
        int(snapshot.get("metrics", {}).get("ownership_conflict_count", 0))
        if isinstance(snapshot.get("metrics"), dict)
        else 0
    )
    latest_snapshot = _latest_snapshot_status_for_payload(snapshot)
    if latest_snapshot["status"] == "bad" or conflict_count > 0:
        global_tone = "bad"
        global_label = "Agir"
    elif healthy_count < enabled_count:
        global_tone = "warn"
        global_label = "Surveiller"
    else:
        global_tone = "good"
        global_label = "OK"

    summary_cards = render_metric_cards(
        [
            {
                "label": "Capital HL dispo",
                "value": f"{fmt_number(runtime_report.get('hl_available_usd'), 2)} USDC",
                "note": hl_capital_note(),
            },
            {
                "label": "Capital utilisé",
                "value": f"{capital_used_usd:.2f} USD",
                "note": f"{open_position_count} position(s) ouverte(s)",
            },
            {
                "label": "PnL visible A/C",
                "value": f"{ac_visible_pnl:+.4f} USD",
                "note": f"réalisé {ac_realized:+.4f} · latent {ac_unrealized:+.4f}",
            },
            {
                "label": "Win rate A/C",
                "value": fmt_pct(ac_win_rate),
                "note": f"{ac_win_count} win · {ac_loss_count} loss",
            },
            {
                "label": "Runtime",
                "value": f"{healthy_count}/{enabled_count}",
                "note": "pods A/C sains sur actifs",
            },
            {
                "label": "Mode A/C",
                "value": str(snapshot.get("mode", "-")),
                "note": f"réseau {exchange_network}",
            },
            {
                "label": "Régime crypto",
                "value": str(snapshot.get("regime", "-")),
                "note": "marchés crypto / Pod A",
            },
            {
                "label": "Trades fermés",
                "value": str(closed_event_count),
                "note": "events récents visibles A/C",
            },
        ]
    )

    cluster_order: list[str] = ["crypto"]
    for cluster in sorted(
        set(pod_c_allowed_clusters)
        | set(cluster_regimes)
        | set(cluster_target_allocations)
        | set(scope_count_by_cluster)
    ):
        if cluster != "crypto":
            cluster_order.append(cluster)
    cluster_cards: list[dict[str, object]] = []
    crypto_budget_pct = float(snapshot.get("allocations", {}).get("pod_a", 0.0) or 0.0)
    for cluster in cluster_order:
        observed_count = int(observed_count_by_cluster.get(cluster, 0))
        tradable_count = int(tradable_count_by_cluster.get(cluster, 0))
        scope_count = int(scope_count_by_cluster.get(cluster, 0))
        regime_value = (
            str(snapshot.get("regime", "-"))
            if cluster == "crypto"
            else cluster_regimes.get(cluster, "No data")
        )
        if cluster == "crypto":
            label = "Crypto (Pod A)"
            note = (
                f"{tradable_count}/{max(observed_count, 1)} tradable"
                f" · budget Pod A {crypto_budget_pct:.0%}"
            )
        else:
            label = f"{_cluster_display_name(cluster)} (Pod C)"
            population = observed_count if observed_count > 0 else scope_count
            if population > 0:
                note = (
                    f"{tradable_count}/{population} tradable"
                    f" · budget {cluster_target_allocations.get(cluster, 0.0):.0%}"
                )
            else:
                note = (
                    f"Pas de snapshot visible"
                    f" · budget {cluster_target_allocations.get(cluster, 0.0):.0%}"
                )
        cluster_cards.append({"label": label, "value": regime_value, "note": note})
    market_state_cards = render_metric_cards(cluster_cards)

    focus_items: list[dict[str, str]] = []
    if latest_snapshot["status"] == "bad":
        focus_items.append(
            {
                "tone": "bad",
                "label": "Maintenant",
                "title": "Collector en retard",
                "comment": str(latest_snapshot["comment"]),
            }
        )
    if conflict_count > 0:
        focus_items.append(
            {
                "tone": "bad",
                "label": "Maintenant",
                "title": "Corriger l'ownership",
                "comment": f"{conflict_count} conflit(s) détecté(s) entre pods.",
            }
        )
    for pod in summaries.values():
        if str(pod["tone"]) in {"warn", "bad"}:
            focus_items.append(
                {
                    "tone": str(pod["tone"]),
                    "label": "Vérifier",
                    "title": f"{pod['label']} à vérifier",
                    "comment": str(pod["comment"]),
                }
            )
    if not focus_items:
        focus_items.append(
            {
                "tone": "good",
                "label": "RAS",
                "title": "Runtime stable",
                "comment": "Statuts frais pour Pod A et Pod C.",
            }
        )
    focus_tone = (
        "bad"
        if any(item["tone"] == "bad" for item in focus_items)
        else "warn"
        if any(item["tone"] == "warn" for item in focus_items)
        else "good"
    )
    focus_rows = "".join(
        (
            "<article class='focus-item'>"
            f"<div><span class='dot dot-{escape(str(item['tone']))}'></span>"
            f"<strong>{escape(str(item['title']))}</strong>"
            f"<small>{escape(str(item['comment']))}</small></div>"
            f"<span class='focus-tag focus-tag-{escape(str(item['tone']))}'>{escape(str(item['label']))}</span>"
            "</article>"
        )
        for item in focus_items[:4]
    )
    status_rows = "".join(
        (
            f"<article class='status-card status-card-{escape(str(item['status']))}'>"
            f"<div>{_status_badge(str(item['status']), str(item['label']))}</div>"
            f"<p>{escape(str(item['comment']))}</p>"
            "</article>"
        )
        for item in status_items
    )

    def render_coin_summary_rows(rows: list[dict[str, object]]) -> str:
        if not rows:
            return "<tr><td colspan='8'>Aucun trade par coin visible pour le moment.</td></tr>"
        return "".join(
            (
                "<tr>"
                f"<td>{escape(str(item.get('symbol', '-')))}</td>"
                f"<td>{escape(str(item.get('side', '-')))}</td>"
                f"<td>{int(item.get('closed_trade_count', 0) or 0)}</td>"
                f"<td>{int(item.get('open_position_count', 0) or 0)}</td>"
                f"<td>{fmt_pct(item.get('win_rate'))}</td>"
                f"<td>{fmt_signed_usd(item.get('realized_pnl_usd'))}</td>"
                f"<td>{fmt_signed_usd(item.get('unrealized_pnl_usd'))}</td>"
                f"<td>{fmt_signed_usd(item.get('total_pnl_usd'))}</td>"
                "</tr>"
            )
            for item in rows
        )

    def render_preview_list(items: object) -> str:
        if not isinstance(items, list) or not items:
            return "<p class='soft-note'>Aucun signal en attente pour le moment.</p>"
        rows = []
        for item in items[:8]:
            if not isinstance(item, dict):
                continue
            parts = [escape(str(item.get("symbol", "-")))]
            side = str(item.get("side", "")).strip().upper()
            setup = str(item.get("setup", "")).strip()
            if side:
                parts.append(escape(side))
            if setup:
                parts.append(escape(setup))
            confidence = item.get("confidence")
            if confidence not in (None, ""):
                parts.append(f"conf {float(confidence):.2f}")
            summary = str(item.get("reason_summary", "")).strip()
            if summary:
                rows.append(f"<li>{' · '.join(parts)}<br><span>{escape(summary)}</span></li>")
            else:
                rows.append(f"<li>{' · '.join(parts)}</li>")
        return (
            f"<ul class='simple-list'>{''.join(rows)}</ul>"
            if rows
            else "<p class='soft-note'>Aucun signal en attente pour le moment.</p>"
        )

    def render_review_list(items: object) -> str:
        if not isinstance(items, list):
            return "<p class='soft-note'>Aucun filtre notable pour le moment.</p>"
        filtered = [
            item
            for item in items
            if isinstance(item, dict)
            and str(item.get("status", "")) in {"filtered", "shadow_blocked_by_routing"}
        ]
        if not filtered:
            return "<p class='soft-note'>Aucun filtre notable pour le moment.</p>"
        rows = []
        for item in filtered[:6]:
            parts = [escape(str(item.get("symbol", "-")))]
            side = str(item.get("preferred_side") or item.get("side") or "").strip().upper()
            if side and side != "NEUTRAL":
                parts.append(escape(side))
            if item.get("setup"):
                parts.append(escape(str(item.get("setup"))))
            rows.append(
                f"<li>{' · '.join(parts)}<br><span>{escape(str(item.get('reason_summary', '-')))}</span></li>"
            )
        return f"<ul class='simple-list'>{''.join(rows)}</ul>"

    def render_directional_open_rows(pod_name: str) -> str:
        rows = [
            item
            for item in open_rows
            if str(item.get("pod")) == pod_name and str(item.get("status")) == "open"
        ]
        if not rows:
            return "<tr><td colspan='12'>Aucune position ouverte visible pour le moment.</td></tr>"
        return "".join(
            (
                "<tr>"
                f"<td>{escape(str(item.get('symbol', '-')))}</td>"
                f"<td>{escape(str(item.get('side', '-')))}</td>"
                f"<td>{escape(_humanize_setup_reason(item.get('open_reason')))}</td>"
                f"<td>{fmt_number(item.get('entry_price'), 6)}</td>"
                f"<td>{fmt_number(item.get('current_price'), 6)}</td>"
                f"<td>{fmt_number(item.get('current_notional_usd'), 2)}</td>"
                f"<td>{fmt_number(item.get('margin_usd'), 2)}</td>"
                f"<td>{fmt_number(_directional_take_profit_price(item), 6)}</td>"
                f"<td>{fmt_number(_directional_stop_price(item), 6)}</td>"
                f"<td>{fmt_signed_usd(item.get('unrealized_pnl_usd'))}</td>"
                f"<td>{escape(_directional_trailing_status(item))}</td>"
                f"<td>{escape(str(item.get('opened_at') or '-'))}</td>"
                "</tr>"
            )
            for item in rows
        )

    def render_directional_closed_rows(pod_name: str) -> str:
        rows = [
            item
            for item in event_rows
            if str(item.get("pod")) == pod_name and str(item.get("status")) == "closed"
        ]
        if not rows:
            return "<tr><td colspan='13'>Aucun trade fermé visible pour le moment.</td></tr>"
        return "".join(
            (
                "<tr>"
                f"<td>{escape(str(item.get('closed_at') or item.get('timestamp') or '-'))}</td>"
                f"<td>{escape(str(item.get('symbol', '-')))}</td>"
                f"<td>{escape(str(item.get('side', '-')))}</td>"
                f"<td>{escape(_humanize_setup_reason(item.get('open_reason')))}</td>"
                f"<td>{escape(_humanize_close_reason(item.get('close_reason')))}</td>"
                f"<td>{fmt_number(item.get('entry_price'), 6)}</td>"
                f"<td>{fmt_number(item.get('exit_price'), 6)}</td>"
                f"<td>{fmt_number(item.get('current_price') or item.get('exit_price'), 6)}</td>"
                f"<td>{fmt_number(_directional_take_profit_price(item), 6)}</td>"
                f"<td>{fmt_number(_directional_stop_price(item), 6)}</td>"
                f"<td>{fmt_number(item.get('notional_usd'), 2)}</td>"
                f"<td>{_format_leverage(item.get('leverage'))}</td>"
                f"<td>{fmt_signed_usd(item.get('pnl_usd'))}</td>"
                "</tr>"
            )
            for item in rows
        )

    def render_hip4_open_rows() -> str:
        rows = [
            item
            for item in open_rows
            if str(item.get("pod")) == "pod_b" and str(item.get("status")) == "open"
        ]
        if not rows:
            return "<tr><td colspan='9'>Aucune position Pod B ouverte visible pour le moment.</td></tr>"
        return "".join(
            (
                "<tr>"
                f"<td>{escape(str(item.get('symbol', '-')))}</td>"
                f"<td>{escape(str(item.get('side', '-')))}</td>"
                f"<td>{escape(str(item.get('open_reason', '-')))}</td>"
                f"<td>{fmt_number(item.get('entry_price'), 6)}</td>"
                f"<td>{fmt_number(item.get('notional_usd'), 2)}</td>"
                f"<td>{fmt_number(item.get('margin_usd'), 2)}</td>"
                f"<td>{fmt_signed_usd(item.get('unrealized_pnl_usd'))}</td>"
                f"<td>{fmt_number(item.get('confidence'), 4)}</td>"
                f"<td>{escape(str(item.get('opened_at') or '-'))}</td>"
                "</tr>"
            )
            for item in rows
        )

    def render_hip4_closed_rows() -> str:
        runtime_payload = snapshot.get("pod_b_runtime")
        settled_rows = []
        if isinstance(runtime_payload, dict) and isinstance(runtime_payload.get("settled_positions"), list):
            settled_rows = [item for item in runtime_payload["settled_positions"] if isinstance(item, dict)]
        if not settled_rows:
            return "<tr><td colspan='9'>Aucun settlement ou trade fermé Pod B visible pour le moment.</td></tr>"
        rows = []
        for item in reversed(settled_rows[-16:]):
            pnl = _first_float(item, "estimated_pnl_usdc", "net_pnl_usdc", "pnl_usdc", "pnl_usd")
            rows.append(
                "<tr>"
                f"<td>{escape(str(item.get('settled_at') or item.get('closed_at') or item.get('ts') or '-'))}</td>"
                f"<td>{escape(str(item.get('underlying') or item.get('symbol') or '-'))}</td>"
                f"<td>{escape(str(item.get('market_id') or '-'))}</td>"
                f"<td>{escape(str(item.get('side') or '-'))}</td>"
                f"<td>{escape(str(item.get('result') or item.get('status') or '-'))}</td>"
                f"<td>{fmt_number(item.get('cost_usdc') or item.get('cost_usd'), 2)}</td>"
                f"<td>{fmt_number(item.get('payout_usdc'), 2)}</td>"
                f"<td>{fmt_signed_usd(pnl)}</td>"
                f"<td>{escape(str(item.get('notes') or item.get('reason') or '-'))}</td>"
                "</tr>"
            )
        return "".join(rows)

    def render_directional_opportunity_rows(pod_name: str) -> str:
        rows = opportunity_rows.get(pod_name, [])
        if not rows:
            return "<tr><td colspan='12'>Aucune opportunité récente visible dans le journal live ou le snapshot runtime.</td></tr>"
        return "".join(
            (
                "<tr>"
                f"<td>{escape(str(item.get('timestamp') or '-'))}</td>"
                f"<td>{escape(str(item.get('symbol') or '-'))}</td>"
                f"<td>{escape(str(item.get('side') or '-'))}</td>"
                f"<td>{escape(_humanize_setup_reason(item.get('setup')))}</td>"
                f"<td>{_status_badge(str(item.get('tone') or 'neutral'), str(item.get('status_label') or '-'))}</td>"
                f"<td>{_tooltip_value(item.get('cause_label'), item.get('cause_tooltip'))}</td>"
                f"<td>{fmt_number(item.get('current_price') or item.get('entry_price'), 6)}</td>"
                f"<td>{fmt_number(item.get('target_notional_usd'), 2)}</td>"
                f"<td>{fmt_number(item.get('margin_usd'), 2)}</td>"
                f"<td>{fmt_number(item.get('stop_price'), 6)}</td>"
                f"<td>{fmt_number(item.get('take_profit_price'), 6)}</td>"
                f"<td>{escape(str(item.get('detail') or '-'))}</td>"
                "</tr>"
            )
            for item in rows
        )

    def pod_header_metrics(summary: dict[str, object]) -> str:
        return render_metric_cards(
            [
                {"label": "Mode", "value": str(summary["mode"]), "note": "Runtime du pod"},
                {"label": "Status", "value": str(summary["badge"]), "note": str(summary["comment"])},
                {
                    "label": "Target",
                    "value": f"{float(summary['target_usd']):.2f} USD",
                    "note": f"{float(summary['target_pct']):.2f} du capital",
                },
                {"label": "Open", "value": str(summary["position_count"]), "note": "Positions ouvertes"},
                {"label": "Signals", "value": str(summary["preview_count"]), "note": "Previews visibles"},
                {"label": "Exec", "value": str(summary["total_fill_count"]), "note": "Trades/fills observés"},
                {
                    "label": "Win rate",
                    "value": fmt_pct(summary.get("win_rate")),
                    "note": f"{summary['win_count']} win · {summary['loss_count']} loss",
                },
                {
                    "label": "Realized PnL",
                    "value": f"{float(summary['realized_pnl_usd']):.4f} USD",
                    "note": f"Mode {summary['mode']} uniquement",
                },
            ]
        )

    def render_pod_card(summary: dict[str, object]) -> str:
        symbols = ", ".join(escape(str(symbol)) for symbol in summary["owned_symbols"]) or "-"
        deep_link = (
            f"<button class='text-link' type='button' data-jump-tab='{escape(str(summary['pod']))}'>"
            "Voir l'onglet</button>"
        )
        return (
            f"<article class='pod-card pod-card-{escape(str(summary['tone']))}'>"
            "<div class='pod-card-head'>"
            f"<span class='dot dot-{escape(str(summary['tone']))}'></span>"
            f"<div><h3>{escape(str(summary['label']))}</h3><p>{escape(str(summary['comment']))}</p></div>"
            "</div>"
            f"<div class='pod-card-meta'>{_status_badge(str(summary['tone']), str(summary['badge']))}"
            f"<span class='network-chip network-chip-{escape(normalized_network(summary['network']))}'>{escape(network_label(summary['network']))}</span>"
            f"<span class='mode-chip mode-chip-{escape(mode_tone(summary['mode']))}'>Mode {escape(str(summary['mode']))}</span></div>"
            "<dl class='pod-facts'>"
            f"<div><dt>Symbols</dt><dd>{symbols}</dd></div>"
            f"<div><dt>Allocation</dt><dd>{float(summary['target_pct']):.2f} · {float(summary['target_usd']):.2f} USD</dd></div>"
            f"<div><dt>Ouvert</dt><dd>{int(summary['position_count'])} pos · {int(summary['open_order_count'])} ordres</dd></div>"
            f"<div><dt>Exécution</dt><dd>{int(summary['total_fill_count'])} exec</dd></div>"
            f"<div><dt>PnL réalisé</dt><dd>{fmt_signed_usd(summary.get('realized_pnl_usd'))} USD</dd></div>"
            f"<div><dt>PnL latent</dt><dd>{fmt_signed_usd(summary.get('total_unrealized_pnl_usd'))} USD</dd></div>"
            "</dl>"
            f"{deep_link}"
            "</article>"
        )

    def render_dashboard_open_rows() -> str:
        if not open_rows:
            return "<tr><td colspan='12'>Aucune position ouverte A/C.</td></tr>"
        return "".join(
            (
                "<tr>"
                f"<td>{escape(_pod_label(str(item.get('pod', '-'))))}</td>"
                f"<td>{escape(str(item.get('symbol', '-')))}</td>"
                f"<td>{escape(str(item.get('side', '-')))}</td>"
                f"<td>{escape(_humanize_setup_reason(item.get('open_reason')))}</td>"
                f"<td>{fmt_number(item.get('entry_price'), 6)}</td>"
                f"<td>{fmt_number(item.get('current_price'), 6)}</td>"
                f"<td>{fmt_number(_directional_take_profit_price(item), 6)}</td>"
                f"<td>{fmt_number(_directional_stop_price(item), 6)}</td>"
                f"<td>{fmt_number(item.get('margin_usd'), 2)}</td>"
                f"<td>{fmt_number(item.get('current_notional_usd'), 2)}</td>"
                f"<td>{fmt_signed_usd(item.get('unrealized_pnl_usd'))}</td>"
                f"<td>{escape(str(item.get('opened_at') or '-'))}</td>"
                "</tr>"
            )
            for item in open_rows[:10]
        )

    def render_dashboard_closed_rows() -> str:
        rows = [item for item in event_rows if str(item.get("status")) == "closed"]
        if not rows:
            return "<tr><td colspan='12'>Aucun trade fermé A/C récent.</td></tr>"
        return "".join(
            (
                "<tr>"
                f"<td>{escape(_pod_label(str(item.get('pod', '-'))))}</td>"
                f"<td>{escape(str(item.get('symbol', '-')))}</td>"
                f"<td>{escape(str(item.get('side', '-')))}</td>"
                f"<td>{escape(_humanize_setup_reason(item.get('open_reason')))}</td>"
                f"<td>{escape(_humanize_close_reason(item.get('close_reason')))}</td>"
                f"<td>{fmt_number(item.get('entry_price'), 6)}</td>"
                f"<td>{fmt_number(item.get('current_price') or item.get('exit_price'), 6)}</td>"
                f"<td>{fmt_number(_directional_take_profit_price(item), 6)}</td>"
                f"<td>{fmt_number(_directional_stop_price(item), 6)}</td>"
                f"<td>{fmt_number(item.get('notional_usd'), 2)}</td>"
                f"<td>{fmt_signed_usd(item.get('pnl_usd'))}</td>"
                f"<td>{escape(str(item.get('closed_at') or item.get('timestamp') or '-'))}</td>"
                "</tr>"
            )
            for item in rows[:10]
        )

    pod_cards = "".join(render_pod_card(summaries[pod]) for pod in display_pods)
    status_panel_html = f"""
      <section class="tab-panel{' is-active' if active_tab == 'status' else ''}" data-tab-panel="status">
        <div class="band band-{escape(global_tone)}">
          <div class="section-head"><div><h2>{escape(global_label)}</h2><p>{escape(commentary)}</p></div>{_status_badge(global_tone, global_label)}</div>
          <div class="metric-grid">{summary_cards}</div>
        </div>
        <section class="panel panel-{escape(focus_tone)}">
          <div class="section-head"><h2>À faire maintenant</h2><p>Liste courte. Si tu ne lis qu'un bloc, c'est celui-ci.</p></div>
          <div class="focus-grid">{focus_rows}</div>
        </section>
        <section class="two-col">
          <section class="panel">
            <div class="section-head"><h2>Positions ouvertes A/C</h2><p>Capital actuellement engagé par Pod A et Pod C.</p></div>
            <div class="table-wrap"><table><thead><tr>{_table_header("Pod", "Pod qui porte la position.")}{_table_header("Symbol", "Marché concerné.")}{_table_header("Side", "Sens de la position.")}{_table_header("Raison", "Setup qui a ouvert la position.")}{_table_header("Prix entrée", "Prix moyen d'entrée.")}{_table_header("Prix courant", "Dernier prix live vu par le pod.")}{_table_header("Prix TP", "Take profit théorique ou configuré.")}{_table_header("Prix SL", "Stop loss courant, invalidation si disponible.")}{_table_header("Marge", "Capital immobilisé.")}{_table_header("Valeur USD", "Valeur notionnelle actuelle.")}{_table_header("PnL latent", "PnL non réalisé.")}{_table_header("Ouvert", "Horodatage d'ouverture.")}</tr></thead><tbody>{render_dashboard_open_rows()}</tbody></table></div>
          </section>
          <section class="panel">
            <div class="section-head"><h2>Trades fermés A/C</h2><p>Dernières sorties visibles, sans détails de pod trop bruyants.</p></div>
            <div class="table-wrap"><table><thead><tr>{_table_header("Pod", "Pod responsable.")}{_table_header("Symbol", "Marché concerné.")}{_table_header("Side", "Sens porté.")}{_table_header("Entrée", "Setup d'entrée.")}{_table_header("Sortie", "Raison de fermeture.")}{_table_header("Prix entrée", "Prix moyen d'entrée du trade.")}{_table_header("Prix courant/sortie", "Dernier prix connu; pour un trade fermé, on affiche le prix de sortie si aucun prix courant n'est conservé.")}{_table_header("Prix TP", "Take profit théorique enregistré avec le trade.")}{_table_header("Prix SL", "Stop loss ou invalidation enregistré avec le trade.")}{_table_header("Notional", "Notionnelle cible.")}{_table_header("PnL", "Résultat du trade.")}{_table_header("Fermé", "Horodatage de sortie.")}</tr></thead><tbody>{render_dashboard_closed_rows()}</tbody></table></div>
          </section>
        </section>
        <section class="panel">
          <div class="section-head"><h2>États des marchés</h2><p>Crypto explicite pour Pod A, puis clusters hors crypto qui pilotent Pod C.</p></div>
          <div class="metric-grid">{market_state_cards}</div>
        </section>
        <section class="panel">
          <div class="section-head"><h2>Status</h2><p>Collecte, santé des pods, ownership et activité récente.</p></div>
          <div class="status-grid">{status_rows}</div>
        </section>
        <section class="panel">
          <div class="section-head"><h2>Pods</h2><p>Vue TRIDENT limitée à Pod A et Pod C.</p></div>
          <div class="pod-grid">{pod_cards}</div>
        </section>
      </section>"""

    def render_directional_pod_panel(pod_name: str, summary: dict[str, object]) -> str:
        preview_key = f"{pod_name}_signal_preview"
        review_key = f"{pod_name}_signal_review"
        return f"""
      <section class="tab-panel{' is-active' if active_tab == pod_name else ''}" data-tab-panel="{escape(pod_name)}">
        <div class="band band-{escape(str(summary['tone']))}">
          <div class="section-head">
            <div><h2>{escape(str(summary['label']))}</h2><p>Positions ouvertes, trades fermés, signal preview et filtres. PnL lu uniquement dans le ledger de ce pod.</p></div>
            <span class="mode-chip mode-chip-{escape(mode_tone(summary['mode']))}">Mode {escape(str(summary['mode']))}</span>
          </div>
          <div class="metric-grid">{pod_header_metrics(summary)}</div>
        </div>
        <div class="two-col">
          <section class="panel">
            <div class="section-head"><h3>Performance par coin</h3><p>Trades fermés et positions ouvertes du pod, regroupés par coin et par sens.</p></div>
            <div class="table-wrap"><table><thead><tr>{_table_header("Coin", "Sous-jacent ou symbole tradé.")}{_table_header("Sens", "Long, short ou mixed selon les données disponibles.")}{_table_header("Trades", "Trades fermés visibles.")}{_table_header("Ouvert", "Positions encore ouvertes.")}{_table_header("Win rate", "Win / trades fermés avec PnL connu.")}{_table_header("PnL réalisé", "PnL du pod uniquement.")}{_table_header("PnL latent", "PnL latent du pod uniquement.")}{_table_header("PnL visible", "Réalisé + latent du pod uniquement.")}</tr></thead><tbody>{render_coin_summary_rows(pod_trade_summary_rows[pod_name])}</tbody></table></div>
          </section>
          <section class="panel">
            <div class="section-head"><h3>Signaux et filtres</h3><p>Ce qui est proposé ou bloqué avant exécution.</p></div>
            <h4>Signal preview</h4>{render_preview_list(snapshot.get(preview_key))}
            <h4>Pourquoi filtré</h4>{render_review_list(snapshot.get(review_key))}
          </section>
        </div>
        <section class="panel">
          <div class="section-head"><h3>Opportunités récentes</h3><p>Chaque candidat vu par le pod avec son verdict risk/exécution et la cause précise du refus ou de l'acceptation.</p></div>
          <div class="table-wrap"><table><thead><tr>{_table_header("Timestamp", "Horodatage du signal ou du filtre.")}{_table_header("Symbol", "Marché concerné.")}{_table_header("Side", "Sens proposé.")}{_table_header("Setup", "Setup stratégique qui a généré l'opportunité.")}{_table_header("Statut", "Verdict: ouvert, accepté risk, bloqué à l'exécution, refusé par le risk gate ou filtré avant risk.")}{_table_header("Cause", "Cause normalisée. Survole le i pour la signification concrète et le détail runtime.")}{_table_header("Prix ref", "Prix du snapshot utilisé pour décider, quand disponible.")}{_table_header("Notional", "Notionnelle cible du plan après caps live.")}{_table_header("Marge", "Marge calculée pour le plan.")}{_table_header("Prix SL", "Prix de stop ou invalidation calculé pour ce plan.")}{_table_header("Prix TP", "Prix de take profit calculé pour ce plan.")}{_table_header("Détail", "Résumé du signal, sizing et fills éventuels.")}</tr></thead><tbody>{render_directional_opportunity_rows(pod_name)}</tbody></table></div>
        </section>
        <section class="panel">
          <div class="section-head"><h3>Trades ouverts</h3><p>Prix courant, marge, TP/SL, PnL latent et trailing.</p></div>
          <div class="table-wrap"><table><thead><tr>{_table_header("Symbol", "Marché actuellement détenu par le pod.")}{_table_header("Side", "Sens de la position.")}{_table_header("Raison ouverture", "Setup qui a ouvert le trade.")}{_table_header("Prix entrée", "Prix moyen d'entrée.")}{_table_header("Prix courant", "Dernier prix live vu.")}{_table_header("Valeur USD", "Valeur notionnelle actuelle.")}{_table_header("Marge", "Capital immobilisé.")}{_table_header("Prix TP", "Take profit théorique.")}{_table_header("Prix SL", "Stop loss actuel.")}{_table_header("Unrealized PnL", "PnL latent au dernier prix.")}{_table_header("Trailing TP", "État du trailing.")}{_table_header("Ouvert le", "Horodatage d'ouverture.")}</tr></thead><tbody>{render_directional_open_rows(pod_name)}</tbody></table></div>
        </section>
        <section class="panel">
          <div class="section-head"><h3>Trades fermés récents</h3><p>Raisons d'ouverture et de fermeture en clair.</p></div>
          <div class="table-wrap"><table><thead><tr>{_table_header("Fermé le", "Horodatage de sortie.")}{_table_header("Symbol", "Marché concerné.")}{_table_header("Side", "Sens porté.")}{_table_header("Raison ouverture", "Setup d'entrée.")}{_table_header("Raison fermeture", "Cause de sortie.")}{_table_header("Prix entrée", "Prix d'entrée.")}{_table_header("Prix sortie", "Prix de sortie.")}{_table_header("Prix courant/sortie", "Dernier prix connu; pour un trade fermé, tombe sur le prix de sortie si le runtime n'a pas gardé de prix courant.")}{_table_header("Prix TP", "Take profit théorique enregistré avec le trade.")}{_table_header("Prix SL", "Stop loss ou invalidation enregistré avec le trade.")}{_table_header("Notional USD", "Notionnelle cible.")}{_table_header("Leverage", "Levier configuré.")}{_table_header("PnL USD", "Résultat net du trade.")}</tr></thead><tbody>{render_directional_closed_rows(pod_name)}</tbody></table></div>
        </section>
      </section>"""

    def render_activity_open_rows() -> str:
        if not open_rows:
            return "<tr><td colspan='13'>Aucune position ouverte visible pour le moment.</td></tr>"
        return "".join(
            (
                "<tr data-filter-status='open' "
                f"data-filter-pod='{escape(str(item['pod']))}'>"
                f"<td>{escape(str(item['pod']))}</td>"
                f"<td>{escape(str(item['symbol']))}</td>"
                f"<td>{escape(str(item['side']))}</td>"
                f"<td>{escape(str(item['open_reason']))}</td>"
                f"<td>{fmt_number(item.get('entry_price'), 6)}</td>"
                f"<td>{fmt_number(item.get('current_price'), 6)}</td>"
                f"<td>{fmt_number(item.get('notional_usd'), 2)}</td>"
                f"<td>{_format_leverage(item.get('leverage'))}</td>"
                f"<td>{fmt_number(item.get('confidence'), 2)}</td>"
                f"<td>{fmt_number(_directional_stop_price(item), 6)}</td>"
                f"<td>{fmt_number(_directional_take_profit_price(item), 6)}</td>"
                f"<td>{escape(str(item.get('time_stop_hours') or '-'))}</td>"
                f"<td>{escape(str(item.get('opened_at') or '-'))}</td>"
                "</tr>"
            )
            for item in open_rows
        )

    def render_activity_event_rows() -> str:
        if not event_rows:
            return "<tr><td colspan='15'>Aucun évènement de trade récent visible pour le moment.</td></tr>"
        return "".join(
            (
                "<tr "
                f"data-filter-status=\"{'closed' if str(item.get('status')) == 'closed' else 'open'}\" "
                f"data-filter-pod='{escape(str(item['pod']))}'>"
                f"<td>{escape(str(item.get('timestamp') or '-'))}</td>"
                f"<td>{escape(str(item['pod']))}</td>"
                f"<td>{escape(str(item['symbol']))}</td>"
                f"<td>{escape(str(item['side']))}</td>"
                f"<td>{escape(str(item['status']))}</td>"
                f"<td>{escape(str(item.get('open_reason') or '-'))}</td>"
                f"<td>{escape(str(item.get('close_reason') or '-'))}</td>"
                f"<td>{fmt_number(item.get('entry_price'), 6)}</td>"
                f"<td>{fmt_number(item.get('current_price') or item.get('exit_price'), 6)}</td>"
                f"<td>{fmt_number(item.get('exit_price'), 6)}</td>"
                f"<td>{fmt_number(_directional_stop_price(item), 6)}</td>"
                f"<td>{fmt_number(_directional_take_profit_price(item), 6)}</td>"
                f"<td>{fmt_number(item.get('notional_usd'), 2)}</td>"
                f"<td>{_format_leverage(item.get('leverage'))}</td>"
                f"<td>{fmt_signed_usd(item.get('pnl_usd'))}</td>"
                "</tr>"
            )
            for item in event_rows
        )

    activity_panel_html = f"""
      <section class="tab-panel{' is-active' if active_tab == 'activity' else ''}" data-tab-panel="activity">
        <section class="panel">
          <div class="section-head"><h2>Activity</h2><p>Positions ouvertes et évènements récents, filtrables par pod.</p></div>
          <div class="filter-row" aria-label="Filtres activity">
            <button class="filter-chip is-active" type="button" data-filter-group="status" data-filter-value="open">Open</button>
            <button class="filter-chip is-active" type="button" data-filter-group="status" data-filter-value="closed">Closed</button>
            <button class="filter-chip is-active" type="button" data-filter-group="pod" data-filter-value="pod_a">Pod A</button>
            <button class="filter-chip is-active" type="button" data-filter-group="pod" data-filter-value="pod_c">Pod C</button>
          </div>
        </section>
        <section class="panel">
          <div class="section-head"><h3>Open positions</h3><p>Ce qui est en risque maintenant, tous pods confondus, sans PnL agrégé.</p></div>
          <div class="table-wrap"><table><thead><tr>{_table_header("Pod", "Pod qui porte la position.")}{_table_header("Symbol", "Marché concerné.")}{_table_header("Side", "Sens.")}{_table_header("Open reason", "Pourquoi la position a été ouverte.")}{_table_header("Entry", "Prix d'entrée.")}{_table_header("Current", "Prix courant vu par le pod.")}{_table_header("Notional USD", "Valeur notionnelle.")}{_table_header("Leverage", "Levier configuré.")}{_table_header("Confidence", "Confiance.")}{_table_header("Prix SL", "Prix de stop ou invalidation.")}{_table_header("Prix TP", "Prix de take profit.")}{_table_header("Time stop h", "Durée max.")}{_table_header("Opened at", "Horodatage.")}</tr></thead><tbody>{render_activity_open_rows()}</tbody></table></div>
        </section>
        <section class="panel">
          <div class="section-head"><h3>Recent trade events</h3><p>Sorties directionnelles récentes.</p></div>
          <div class="table-wrap"><table><thead><tr>{_table_header("Timestamp", "Horodatage.")}{_table_header("Pod", "Pod responsable.")}{_table_header("Symbol", "Marché.")}{_table_header("Side", "Sens.")}{_table_header("Status", "Statut.")}{_table_header("Open reason", "Raison d'ouverture.")}{_table_header("Close reason", "Raison de fermeture.")}{_table_header("Entry", "Prix d'entrée.")}{_table_header("Current/Exit", "Dernier prix connu; pour les fermés, fallback sur le prix de sortie.")}{_table_header("Exit", "Prix de sortie.")}{_table_header("Prix SL", "Stop loss ou invalidation enregistré.")}{_table_header("Prix TP", "Take profit enregistré.")}{_table_header("Notional USD", "Valeur notionnelle.")}{_table_header("Leverage", "Levier.")}{_table_header("PnL USD", "PnL du trade.")}</tr></thead><tbody>{render_activity_event_rows()}</tbody></table></div>
        </section>
      </section>"""

    primary_tabs = [
        ("status", "Status"),
        ("pod_a", "Pod A"),
        ("pod_c", "Pod C"),
        ("activity", "Activity"),
    ]
    tab_nav = "".join(
        (
            f"<button class='tab-button{' is-active' if key == active_tab else ''}' "
            f"type='button' data-tab-button='{key}' aria-selected='{'true' if key == active_tab else 'false'}'>"
            f"{escape(label)}</button>"
        )
        for key, label in primary_tabs
    )
    main_panels = (
        status_panel_html
        + render_directional_pod_panel("pod_a", pod_a_summary)
        + render_directional_pod_panel("pod_c", pod_c_summary)
        + activity_panel_html
    )

    return f"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{
      --bg: #f5f7fa;
      --surface: #ffffff;
      --surface-soft: #f0f4f7;
      --text: #16212b;
      --muted: #5f6f7d;
      --line: #d7e0e7;
      --accent: #0f766e;
      --good: #16743f;
      --good-soft: #e1f5e8;
      --warn: #9a6700;
      --warn-soft: #fff1cb;
      --bad: #a12d2f;
      --bad-soft: #ffe3e3;
      --neutral: #64748b;
      --neutral-soft: #e8edf3;
      --shadow: 0 12px 28px rgba(22, 33, 43, 0.07);
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; color: var(--text); font-family: "IBM Plex Sans", "Segoe UI", sans-serif; background: var(--bg); }}
    main {{ max-width: 1380px; margin: 0 auto; padding: 22px 18px 48px; }}
    h1, h2, h3, h4 {{ margin: 0; letter-spacing: 0; }}
    p {{ margin: 0; color: var(--muted); line-height: 1.5; }}
    a {{ color: var(--accent); font-weight: 800; text-decoration: none; }}
    a:hover, .text-link:hover {{ text-decoration: underline; }}
    .hero, .band, .panel, .metric-card, .status-card, .pod-card, .focus-item {{ background: var(--surface); border: 1px solid var(--line); border-radius: 8px; box-shadow: var(--shadow); }}
    .hero {{ padding: 18px; display: grid; gap: 14px; }}
    .hero h1 {{ font-size: 2rem; line-height: 1.1; }}
    .chip-row, .hero-links, .link-row, .filter-row, .tab-nav {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
    .chip, .mode-chip, .network-chip {{ display: inline-flex; align-items: center; padding: 6px 10px; border-radius: 999px; background: var(--surface-soft); color: var(--muted); font-size: 0.9rem; font-weight: 800; }}
    .mode-chip-good {{ background: var(--good-soft); color: var(--good); }}
    .mode-chip-warn {{ background: var(--warn-soft); color: var(--warn); }}
    .mode-chip-neutral {{ background: var(--neutral-soft); color: var(--neutral); }}
    .network-chip-mainnet {{ background: #e0f2fe; color: #075985; }}
    .network-chip-testnet {{ background: #fef3c7; color: #92400e; }}
    .network-chip-- {{ background: var(--neutral-soft); color: var(--neutral); }}
    .badge {{ display: inline-block; padding: 5px 9px; border-radius: 999px; font-size: 0.86rem; font-weight: 800; }}
    .badge-good {{ background: var(--good-soft); color: var(--good); }}
    .badge-warn {{ background: var(--warn-soft); color: var(--warn); }}
    .badge-bad {{ background: var(--bad-soft); color: var(--bad); }}
    .badge-neutral {{ background: var(--neutral-soft); color: var(--neutral); }}
    .tab-shell {{ margin-top: 16px; display: grid; gap: 14px; }}
    .tab-nav {{ background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 6px; position: sticky; top: 10px; z-index: 10; }}
    .tab-button, .filter-chip, .text-link {{ border: 0; background: transparent; color: var(--muted); border-radius: 6px; padding: 9px 12px; font: inherit; font-weight: 800; cursor: pointer; }}
    .tab-button.is-active, .filter-chip.is-active {{ background: var(--accent); color: #fff; }}
    .text-link {{ color: var(--accent); padding: 0; }}
    .tab-panel {{ display: none; gap: 14px; }}
    .tab-panel.is-active {{ display: grid; }}
    .band, .panel {{ padding: 18px; overflow: hidden; }}
    .band-good, .panel-good {{ border-color: rgba(22, 116, 63, 0.26); }}
    .band-warn, .panel-warn {{ border-color: rgba(154, 103, 0, 0.28); }}
    .band-bad, .panel-bad {{ border-color: rgba(161, 45, 47, 0.28); }}
    .section-head {{ display: flex; justify-content: space-between; gap: 14px; align-items: flex-start; flex-wrap: wrap; margin-bottom: 14px; }}
    .metric-grid, .status-grid, .pod-grid, .focus-grid, .two-col {{ display: grid; gap: 12px; }}
    .metric-grid {{ grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); }}
    .status-grid {{ grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }}
    .pod-grid {{ grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }}
    .focus-grid {{ grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }}
    .two-col {{ grid-template-columns: minmax(0, 1fr) minmax(320px, 0.85fr); align-items: start; }}
    .metric-card, .status-card, .pod-card, .focus-item {{ padding: 14px; }}
    .metric-card span, .metric-card small, .focus-item small {{ color: var(--muted); line-height: 1.4; }}
    .metric-card strong {{ display: block; margin-top: 6px; font-size: 1.35rem; line-height: 1.15; }}
    .focus-item {{ display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }}
    .focus-item strong {{ display: block; margin-left: 20px; }}
    .focus-item small {{ display: block; margin: 5px 0 0 20px; }}
    .focus-tag {{ padding: 5px 9px; border-radius: 999px; font-size: 0.82rem; font-weight: 800; }}
    .focus-tag-good {{ background: var(--good-soft); color: var(--good); }}
    .focus-tag-warn {{ background: var(--warn-soft); color: var(--warn); }}
    .focus-tag-bad {{ background: var(--bad-soft); color: var(--bad); }}
    .pod-card-head {{ display: flex; gap: 10px; align-items: flex-start; margin-bottom: 12px; }}
    .pod-card-head p {{ margin-top: 5px; }}
    .pod-card-meta {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }}
    .pod-facts {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin: 0 0 12px; }}
    .pod-facts div {{ border-top: 1px solid var(--line); padding-top: 8px; }}
    .pod-facts dt {{ font-size: 0.78rem; text-transform: uppercase; color: var(--muted); margin-bottom: 4px; font-weight: 800; }}
    .pod-facts dd {{ margin: 0; font-weight: 700; line-height: 1.35; }}
    .dot {{ width: 10px; height: 10px; border-radius: 999px; margin-top: 6px; flex: none; display: inline-block; }}
    .dot-good {{ background: var(--good); }}
    .dot-warn {{ background: var(--warn); }}
    .dot-bad {{ background: var(--bad); }}
    .dot-neutral {{ background: var(--neutral); }}
    .soft-note, .simple-list {{ color: var(--muted); line-height: 1.55; }}
    .simple-list {{ margin: 0 0 12px; padding-left: 18px; }}
    .simple-list span {{ color: var(--muted); }}
    .table-wrap {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.92rem; min-width: 780px; background: var(--surface); }}
    th, td {{ text-align: left; padding: 10px 9px; border-bottom: 1px solid var(--line); vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 800; background: var(--surface-soft); }}
    .th-with-tooltip {{ display: inline-flex; align-items: center; gap: 6px; position: relative; }}
    .tooltip-trigger {{ width: 18px; height: 18px; border-radius: 999px; border: 1px solid var(--line); background: #fff; color: var(--accent); font-size: 0.72rem; font-weight: 800; line-height: 1; padding: 0; cursor: help; }}
    .tooltip-bubble {{ position: absolute; left: 0; top: calc(100% + 8px); width: min(260px, 42vw); padding: 10px 12px; border-radius: 8px; background: #16212b; color: #fff; font-size: 0.84rem; line-height: 1.4; opacity: 0; pointer-events: none; transform: translateY(-4px); transition: opacity 120ms ease, transform 120ms ease; z-index: 20; }}
    .th-with-tooltip:hover .tooltip-bubble, .th-with-tooltip:focus-within .tooltip-bubble {{ opacity: 1; transform: translateY(0); }}
    .is-hidden {{ display: none; }}
    @media (max-width: 920px) {{ main {{ padding: 14px 10px 36px; }} .tab-nav {{ position: static; }} .two-col, .pod-facts {{ grid-template-columns: 1fr; }} .hero h1 {{ font-size: 1.6rem; }} }}
  </style>
</head>
<body data-default-tab="{escape(active_tab)}" data-refresh-seconds="{refresh_seconds}">
  <main>
    <header class="hero">
      <div class="chip-row">
        <span class="chip">Version {escape(VERSION)}</span>
        <span class="chip">Profile {escape(str(snapshot.get('profile', '-')))}</span>
        <span class="chip">Mode supervisor {escape(str(snapshot.get('mode', '-')))}</span>
        <span class="chip">Réseau A/C {escape(exchange_network)}</span>
        <span class="chip">Régime {escape(str(snapshot.get('regime', '-')))}</span>
        <span class="chip">Auto-refresh {refresh_seconds}s</span>
      </div>
      <h1>{escape(title)}</h1>
      <p>{escape(subtitle)}</p>
      <div class="hero-links">
        <span>Last updated: {escape(refreshed_at)}</span>
        <a href="/dashboard">Dashboard</a>
        <a href="/trades">Activity</a>
        <a href="/stats">Stats</a>
        <a href="/system">System</a>
        <a href="/api/state">API state</a>
        <a href="/api/report">API report</a>
      </div>
    </header>
    <div class="tab-shell">
      <nav class="tab-nav" aria-label="Navigation principale">{tab_nav}</nav>
      {main_panels}
    </div>
  </main>
  <script>
    (() => {{
      const body = document.body;
      const refreshSeconds = Number(body.dataset.refreshSeconds || "0");
      const buttons = Array.from(document.querySelectorAll("[data-tab-button]"));
      const panels = Array.from(document.querySelectorAll("[data-tab-panel]"));
      const validTabs = new Set(buttons.map((button) => button.dataset.tabButton).filter(Boolean));
      const jumpButtons = Array.from(document.querySelectorAll("[data-jump-tab]"));
      const activeFilters = {{
        status: new Set(["open", "closed"]),
        pod: new Set(["pod_a", "pod_c"]),
      }};
      const filterButtons = Array.from(document.querySelectorAll("[data-filter-group]"));
      const filterRows = Array.from(document.querySelectorAll("tr[data-filter-status][data-filter-pod]"));

      function normalizedTab(tabName) {{
        return validTabs.has(tabName) ? tabName : body.dataset.defaultTab || "status";
      }}

      function activeTabName() {{
        const activeButton = buttons.find((button) => button.classList.contains("is-active"));
        return normalizedTab(activeButton ? activeButton.dataset.tabButton : (window.location.hash || "").replace("#", ""));
      }}

      function scrollStorageKey(tabName) {{
        return `trident:scroll:${{window.location.pathname}}:${{normalizedTab(tabName)}}`;
      }}

      function lastTabStorageKey() {{
        return `trident:tab:${{window.location.pathname}}`;
      }}

      function focusStorageKey() {{
        return `trident:focus:${{window.location.pathname}}`;
      }}

      function cssEscape(value) {{
        if (window.CSS && typeof window.CSS.escape === "function") {{
          return window.CSS.escape(String(value));
        }}
        return String(value).replace(/[^a-zA-Z0-9_-]/g, "");
      }}

      function focusSelector(element) {{
        if (!element || element === document.body) return "";
        if (element.dataset.tabButton) {{
          return `[data-tab-button="${{cssEscape(element.dataset.tabButton)}}"]`;
        }}
        if (element.dataset.filterGroup && element.dataset.filterValue) {{
          return `[data-filter-group="${{cssEscape(element.dataset.filterGroup)}}"][data-filter-value="${{cssEscape(element.dataset.filterValue)}}"]`;
        }}
        if (element.dataset.jumpTab) {{
          return `[data-jump-tab="${{cssEscape(element.dataset.jumpTab)}}"]`;
        }}
        if (element.id) {{
          return `#${{cssEscape(element.id)}}`;
        }}
        if (element.name) {{
          return `${{element.tagName.toLowerCase()}}[name="${{cssEscape(element.name)}}"]`;
        }}
        return "";
      }}

      function isEditable(element) {{
        return element instanceof HTMLInputElement
          || element instanceof HTMLTextAreaElement
          || element instanceof HTMLSelectElement;
      }}

      function saveScrollPosition(tabName = activeTabName()) {{
        try {{
          window.sessionStorage.setItem(scrollStorageKey(tabName), String(window.scrollY || 0));
        }} catch (_error) {{
          // Keep refresh behavior intact if sessionStorage is unavailable.
        }}
      }}

      function saveActiveTab(tabName) {{
        try {{
          window.sessionStorage.setItem(lastTabStorageKey(), normalizedTab(tabName));
        }} catch (_error) {{
          // Keep refresh behavior intact if sessionStorage is unavailable.
        }}
      }}

      function savedTabName() {{
        try {{
          return normalizedTab(window.sessionStorage.getItem(lastTabStorageKey()) || "");
        }} catch (_error) {{
          return body.dataset.defaultTab || "status";
        }}
      }}

      function saveFocusedElement() {{
        const element = document.activeElement;
        const selector = focusSelector(element);
        if (!selector) return;
        const payload = {{ selector }};
        if (isEditable(element) && element.type !== "password") {{
          payload.value = element.value;
          if (typeof element.selectionStart === "number") {{
            payload.selectionStart = element.selectionStart;
            payload.selectionEnd = element.selectionEnd;
          }}
        }}
        try {{
          window.sessionStorage.setItem(focusStorageKey(), JSON.stringify(payload));
        }} catch (_error) {{
          // Keep refresh behavior intact if sessionStorage is unavailable.
        }}
      }}

      function restoreFocusedElement() {{
        try {{
          const raw = window.sessionStorage.getItem(focusStorageKey());
          if (!raw) return;
          const payload = JSON.parse(raw);
          const element = document.querySelector(payload.selector || "");
          if (!element) return;
          if (isEditable(element) && element.type !== "password" && payload.value !== undefined) {{
            element.value = payload.value;
          }}
          window.requestAnimationFrame(() => {{
            element.focus({{ preventScroll: true }});
            if (
              isEditable(element)
              && typeof payload.selectionStart === "number"
              && typeof element.setSelectionRange === "function"
            ) {{
              element.setSelectionRange(payload.selectionStart, payload.selectionEnd ?? payload.selectionStart);
            }}
          }});
        }} catch (_error) {{
          // Keep refresh behavior intact if sessionStorage is unavailable.
        }}
      }}

      function restoreScrollPosition(tabName = activeTabName()) {{
        try {{
          const raw = window.sessionStorage.getItem(scrollStorageKey(tabName));
          if (raw === null) return;
          const next = Number(raw);
          if (!Number.isFinite(next) || next < 0) return;
          window.requestAnimationFrame(() => {{
            window.scrollTo(0, next);
          }});
        }} catch (_error) {{
          // Keep refresh behavior intact if sessionStorage is unavailable.
        }}
      }}

      function savePageState(tabName = activeTabName()) {{
        saveScrollPosition(tabName);
        saveActiveTab(tabName);
        saveFocusedElement();
      }}

      function setTab(tabName, updateHash = true, remember = true) {{
        if (remember) {{
          saveScrollPosition();
          saveFocusedElement();
        }}
        const next = normalizedTab(tabName);
        buttons.forEach((button) => {{
          const active = button.dataset.tabButton === next;
          button.classList.toggle("is-active", active);
          button.setAttribute("aria-selected", active ? "true" : "false");
        }});
        panels.forEach((panel) => {{
          panel.classList.toggle("is-active", panel.dataset.tabPanel === next);
        }});
        if (updateHash) {{
          history.replaceState(null, "", `#${{next}}`);
        }}
        if (remember) {{
          saveActiveTab(next);
        }}
      }}

      function refreshFilterRows() {{
        filterRows.forEach((row) => {{
          const visible = activeFilters.status.has(row.dataset.filterStatus) && activeFilters.pod.has(row.dataset.filterPod);
          row.classList.toggle("is-hidden", !visible);
        }});
      }}

      buttons.forEach((button) => button.addEventListener("click", () => setTab(button.dataset.tabButton)));
      jumpButtons.forEach((button) => button.addEventListener("click", () => setTab(button.dataset.jumpTab || "status")));
      filterButtons.forEach((button) => {{
        button.addEventListener("click", () => {{
          const set = activeFilters[button.dataset.filterGroup];
          const value = button.dataset.filterValue;
          if (!set || !value) return;
          if (set.has(value)) {{
            if (set.size === 1) return;
            set.delete(value);
            button.classList.remove("is-active");
          }} else {{
            set.add(value);
            button.classList.add("is-active");
          }}
          refreshFilterRows();
        }});
      }});
      const hashTab = (window.location.hash || "").replace("#", "");
      const initialTab = hashTab || savedTabName() || body.dataset.defaultTab || "status";
      setTab(initialTab, false, false);
      refreshFilterRows();
      restoreScrollPosition(initialTab);
      restoreFocusedElement();
      window.addEventListener("beforeunload", () => savePageState());
      window.addEventListener("hashchange", () => {{
        const next = (window.location.hash || "").replace("#", "");
        if (validTabs.has(next)) {{
          setTab(next, false);
          restoreScrollPosition(next);
          restoreFocusedElement();
        }}
      }});
      if (Number.isFinite(refreshSeconds) && refreshSeconds > 0) {{
        window.setTimeout(() => {{
          savePageState();
          const target = `${{window.location.pathname}}${{window.location.search}}${{window.location.hash}}`;
          window.location.replace(target);
        }}, refreshSeconds * 1000);
      }}
    }})();
  </script>
</body>
</html>"""


def dashboard_html(
    supervisor: TridentSupervisor,
    metrics: MetricsRegistry,
) -> str:
    return _control_center_html(
        supervisor,
        metrics,
        active_tab="status",
        title="TRIDENT Control Center",
        subtitle=(
            "Cockpit TRIDENT resserré sur Pod A et Pod C : status, détails par pod et activité récente."
        ),
    )


def trades_html(
    supervisor: TridentSupervisor,
    metrics: MetricsRegistry,
) -> str:
    return _control_center_html(
        supervisor,
        metrics,
        active_tab="activity",
        title="TRIDENT Trades",
        subtitle=(
            "Vue activity ouverte directement sur l'onglet exécution. Les détails par pod restent accessibles "
            "dans les autres onglets sans changer d'interface."
        ),
    )


def stats_html(
    supervisor: TridentSupervisor,
    metrics: MetricsRegistry,
) -> str:
    return _control_center_html(
        supervisor,
        metrics,
        active_tab="status",
        title="TRIDENT Stats",
        subtitle=(
            "Vue synthétique A/C. Les statistiques HIP4 vivent dans l'app TRIDENT-HIP4."
        ),
    )


def system_html(
    supervisor: TridentSupervisor,
    metrics: MetricsRegistry,
) -> str:
    return _control_center_html(
        supervisor,
        metrics,
        active_tab="status",
        title="TRIDENT System",
        subtitle=(
            "Vue système TRIDENT limitée aux services Pod A, Pod C et collectors A/C."
        ),
    )


def hip4_outcome_html(
    supervisor: TridentSupervisor,
    metrics: MetricsRegistry,
) -> str:
    payload = _hip4_outcome_monitor_payload()
    mainnet_payload = hip4_outcome_mainnet_payload()
    shadow_payload = hip4_nautilus_shadow_payload()
    status = payload.get("status", {})
    if not isinstance(status, dict):
        status = {}
    coin_summary_rows = _hip4_pod_trade_summary(status)
    refreshed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    refresh_seconds = 10
    status_age = payload.get("status_age_seconds")
    age_label = "-" if status_age is None else _format_duration_compact(float(status_age))
    tone = "good" if payload.get("fresh") else "bad"
    mainnet_status_age = mainnet_payload.get("status_age_seconds")
    mainnet_age_label = (
        "-"
        if mainnet_status_age is None
        else _format_duration_compact(float(mainnet_status_age))
    )
    shadow_label = (
        "ready"
        if shadow_payload.get("shadow_ready")
        else str(shadow_payload.get("status") or shadow_payload.get("reason") or "missing")
    )
    settled_position_count = int(payload.get("settled_position_count", 0) or 0)
    fill_count = int(payload.get("fill_count", 0) or 0)
    realized_pnl = payload.get("realized_pnl_usd")
    gross_pnl = payload.get("gross_pnl_usd")
    fees_usd = payload.get("fees_usd")
    report = payload.get("report", {})
    if not isinstance(report, dict):
        report = {}
    win_count = int(report.get("win_count", 0) or 0)
    loss_count = int(report.get("loss_count", 0) or 0)
    win_rate = report.get("win_rate")
    latest_edge = payload.get("latest_net_edge")
    best_edge = payload.get("best_net_edge")
    latest_short_edge = payload.get("latest_short_net_edge")
    best_short_edge = payload.get("best_short_net_edge")
    capital = payload.get("capital", {})
    if not isinstance(capital, dict):
        capital = {}
    balance_coin = str(capital.get("testnet_balance_coin") or "USDH")
    testnet_observation_health = payload.get("market_observation_health", {})
    if not isinstance(testnet_observation_health, dict):
        testnet_observation_health = {}
    mainnet_observation_health = mainnet_payload.get("market_observation_health", {})
    if not isinstance(mainnet_observation_health, dict):
        mainnet_observation_health = {}
    short_brief = payload.get("short_expiry_brief", {})
    if not isinstance(short_brief, dict):
        short_brief = {}
    short_watchlist = payload.get("short_expiry_watchlist", [])
    if not isinstance(short_watchlist, list):
        short_watchlist = []
    open_positions = status.get("open_positions", [])
    if not isinstance(open_positions, list):
        open_positions = []
    settled_positions = status.get("settled_positions", [])
    if not isinstance(settled_positions, list):
        settled_positions = []

    mode_label = str(payload.get("mode") or status.get("mode") or "-")
    logs_dir_label = str(payload.get("logs_dir") or status.get("logs_dir") or "")
    network_label = str(
        status.get("network")
        or status.get("exchange_network")
        or payload.get("network")
        or payload.get("exchange_network")
        or ("testnet" if mode_label.lower() == "testnet" else "mainnet")
    )
    if "testnet" in logs_dir_label.lower():
        network_label = "testnet"
    elif "mainnet" in logs_dir_label.lower():
        network_label = "mainnet"

    open_pnl = round(
        sum(
            float(item.get("estimated_pnl_usdc", 0.0) or 0.0)
            for item in open_positions
            if isinstance(item, dict)
        ),
        6,
    )
    visible_pnl = round(float(realized_pnl or 0.0) + open_pnl, 6)
    available_capital = capital.get("remaining_budget_usdc")
    if available_capital is None:
        available_capital = capital.get("testnet_available_usdc")
    used_capital = capital.get("open_exposure_usdc")
    if used_capital is None:
        used_capital = sum(
            float(item.get("max_loss_usdc", item.get("cost_usdc", 0.0)) or 0.0)
            for item in open_positions
            if isinstance(item, dict)
        )

    def fmt_number(value: object, digits: int = 4, *, fallback: str = "-") -> str:
        parsed = _float_or_none(value)
        if parsed is None:
            return fallback
        return f"{parsed:.{digits}f}"

    def fmt_signed_number(value: object, digits: int = 4) -> str:
        parsed = _float_or_none(value)
        if parsed is None:
            return "-"
        return f"{parsed:+.{digits}f}"

    def fmt_pct(value: object, digits: int = 1) -> str:
        parsed = _float_or_none(value)
        if parsed is None:
            return "-"
        return f"{parsed * 100:.{digits}f}%"

    def first_present(*values: object) -> object:
        for value in values:
            if value is not None:
                return value
        return None

    def render_stat_cards(cards: list[dict[str, str]]) -> str:
        return "".join(
            (
                "<article class='metric-card'>"
                f"<span>{escape(card['label'])}</span>"
                f"<strong>{escape(card['value'])}</strong>"
                f"<small>{escape(card['note'])}</small>"
                "</article>"
            )
            for card in cards
        )

    def render_health_pill(tone_value: object, label: object) -> str:
        tone_name = str(tone_value or "neutral")
        if tone_name not in {"good", "warn", "bad", "neutral"}:
            tone_name = "neutral"
        return (
            f"<span class='health-pill health-pill-{escape(tone_name)}'>"
            f"<span class='status-dot status-dot-{escape(tone_name)}'></span>"
            f"{escape(str(label or tone_name))}</span>"
        )

    def render_observation_cards() -> str:
        embedded = status.get("embedded_observers") if isinstance(status, dict) else {}
        if not isinstance(embedded, dict):
            embedded = {}
        embedded_enabled = bool(embedded.get("enabled"))
        embedded_threads = int(embedded.get("running_threads", 0) or 0)
        standalone_observer_running = (
            str(mainnet_payload.get("process_state") or "") == "running"
            and bool(mainnet_payload.get("fresh"))
        )
        observer_count = embedded_threads + (1 if standalone_observer_running else 0)
        observer_tone = "good" if observer_count > 0 else "neutral"
        observer_label = (
            "standalone"
            if standalone_observer_running
            else "embedded"
            if embedded_enabled and embedded_threads > 0
            else "off"
        )
        observer_sources = []
        if standalone_observer_running:
            observer_sources.append(str(mainnet_payload.get("logs_dir") or "logs/hip4_outcome_mainnet"))
        observer_sources.extend(str(item) for item in embedded.get("config_paths", []) if item)
        specs = [
            ("HIP4 paper runner", testnet_observation_health),
            ("Mainnet observation", mainnet_observation_health),
        ]
        cards_html = []
        for label, health in specs:
            tone_name = str(health.get("tone", "neutral"))
            cards_html.append(
                "<article class='observation-card'>"
                f"<div class='observation-card-head'><span>{escape(label)}</span>"
                f"{render_health_pill(tone_name, health.get('label', tone_name))}</div>"
                f"<strong>{int(health.get('count', 0) or 0)}</strong>"
                f"<small>{escape(str(health.get('reason', '-')))}</small>"
                f"<small>unsupported/unknown {int(health.get('unknown_count', 0) or 0)} · "
                f"books {int(health.get('books_logged_count', 0) or 0)} · "
                f"named {int(health.get('named_outcome_count', 0) or 0)} · "
                f"bucket {int(health.get('price_bucket_count', 0) or 0)}</small>"
                "</article>"
            )
        cards_html.append(
            "<article class='observation-card'>"
            f"<div class='observation-card-head'><span>Observer mainnet</span>"
            f"{render_health_pill(observer_tone, observer_label)}</div>"
            f"<strong>{observer_count}</strong>"
            "<small>service standalone + thread(s) embarqué(s)</small>"
            f"<small>{escape(', '.join(observer_sources) or '-')}</small>"
            "</article>"
        )
        return "".join(cards_html)

    def render_observation_summary_rows() -> str:
        rows_html: list[str] = []
        for profile_name, health in (
            ("hip4_paper", testnet_observation_health),
            ("mainnet", mainnet_observation_health),
        ):
            class_counts = health.get("by_class")
            if not isinstance(class_counts, dict):
                continue
            support_counts = health.get("by_support_status")
            if not isinstance(support_counts, dict):
                support_counts = {}
            for class_name, count in sorted(class_counts.items(), key=lambda item: (-int(item[1]), str(item[0]))):
                rows_html.append(
                    "<tr>"
                    f"<td>{escape(profile_name)}</td>"
                    f"<td>{render_health_pill(health.get('tone'), health.get('label'))}</td>"
                    f"<td>{escape(str(class_name))}</td>"
                    f"<td>{int(count or 0)}</td>"
                    f"<td>{escape(_format_count_map(support_counts))}</td>"
                    f"<td>{escape(str(health.get('latest_ts') or '-'))}</td>"
                    "</tr>"
                )
        if not rows_html:
            return "<tr><td colspan='6'>Aucune synthèse d'observation disponible.</td></tr>"
        return "".join(rows_html)

    def render_observation_rows() -> str:
        rows_html: list[str] = []
        for profile_name, source in (("hip4_paper", payload), ("mainnet", mainnet_payload)):
            rows = source.get("market_observations", [])
            if not isinstance(rows, list):
                continue
            for row in rows[:40]:
                if not isinstance(row, dict):
                    continue
                tone_payload = _hip4_observation_row_tone(row)
                rows_html.append(
                    "<tr>"
                    f"<td>{escape(profile_name)}</td>"
                    f"<td>{render_health_pill(tone_payload.get('tone'), tone_payload.get('label'))}</td>"
                    f"<td>{escape(str(row.get('ts', '-')))}</td>"
                    f"<td>{escape(str(row.get('class_name', '-')))}</td>"
                    f"<td>{escape(str(row.get('support_status', '-')))}</td>"
                    f"<td>{escape(str(row.get('name', '-')))}</td>"
                    f"<td>{escape(str(row.get('underlying') or '-'))}</td>"
                    f"<td>{escape(_format_observation_bucket(row))}</td>"
                    f"<td>{escape(_format_observation_list(row.get('coins')))}</td>"
                    f"<td>{escape(_format_observation_books(row))}</td>"
                    f"<td>{escape(str(row.get('support_reason') or '-'))}</td>"
                    f"<td>{escape(str(row.get('description') or '-'))}</td>"
                    "</tr>"
                )
        if not rows_html:
            return "<tr><td colspan='12'>Aucune observation HIP-4 loggée pour le moment.</td></tr>"
        return "".join(rows_html)

    def render_coin_summary_rows() -> str:
        if not coin_summary_rows:
            return "<tr><td colspan='8'>Aucun trade par coin visible pour le moment.</td></tr>"
        return "".join(
            (
                "<tr>"
                f"<td>{escape(str(row.get('symbol', '-')))}</td>"
                f"<td>{escape(str(row.get('side', '-')))}</td>"
                f"<td>{int(row.get('closed_trade_count', 0) or 0)}</td>"
                f"<td>{int(row.get('open_position_count', 0) or 0)}</td>"
                f"<td>{fmt_pct(row.get('win_rate'))}</td>"
                f"<td>{fmt_signed_number(row.get('realized_pnl_usd'))}</td>"
                f"<td>{fmt_signed_number(row.get('unrealized_pnl_usd'))}</td>"
                f"<td>{fmt_signed_number(row.get('total_pnl_usd'))}</td>"
                "</tr>"
            )
            for row in coin_summary_rows
        )

    def render_reference_rows() -> str:
        rows = payload.get("reference_prices", [])
        if not isinstance(rows, list) or not rows:
            return "<tr><td colspan='5'>Aucune référence prix visible.</td></tr>"
        return "".join(
            (
                "<tr>"
                f"<td>{escape(str(row.get('underlying', '-')))}</td>"
                f"<td>{fmt_number(row.get('price'), 6)}</td>"
                f"<td>{escape(str(row.get('source_count', 0)))}</td>"
                f"<td>{escape(str(row.get('rejected_count', 0)))}</td>"
                f"<td>{fmt_number(row.get('max_deviation_bps'), 2)}</td>"
                "</tr>"
            )
            for row in rows
            if isinstance(row, dict)
        )

    def render_opportunity_rows() -> str:
        rows = payload.get("opportunities", [])
        if not isinstance(rows, list) or not rows:
            return "<tr><td colspan='10'>Aucune opportunité loggée.</td></tr>"
        return "".join(
            (
                "<tr>"
                f"<td>{escape(str(row.get('ts', '-')))}</td>"
                f"<td>{escape(str(row.get('underlying', '-')))}</td>"
                f"<td>{escape(str(row.get('edge_type', '-')))}</td>"
                f"<td>{escape(str(row.get('side', '-')))}</td>"
                f"<td>{fmt_number(row.get('net_edge'), 6)}</td>"
                f"<td>{fmt_number(row.get('gross_edge'), 6)}</td>"
                f"<td>{fmt_number(row.get('confidence'), 4)}</td>"
                f"<td>{fmt_number(row.get('ref_price'), 6)}</td>"
                f"<td>{fmt_number(row.get('yes_ask'), 6)}</td>"
                f"<td>{escape(str(row.get('reason', '-')))}</td>"
                "</tr>"
            )
            for row in reversed(rows[-16:])
            if isinstance(row, dict)
        )

    def render_settlement_rows() -> str:
        rows = payload.get("settlements", [])
        if not isinstance(rows, list) or not rows:
            return "<tr><td colspan='10'>Aucun settlement HIP-4 visible.</td></tr>"
        return "".join(
            (
                "<tr>"
                f"<td>{escape(str(row.get('ts', '-')))}</td>"
                f"<td>{escape(str(row.get('underlying', '-')))}</td>"
                f"<td>{escape(str(row.get('market_id', '-')))}</td>"
                f"<td>{escape(str(row.get('side', '-')))}</td>"
                f"<td>{escape(str(row.get('result', '-')))}</td>"
                f"<td>{fmt_number(row.get('payout_usdc'), 2)}</td>"
                f"<td>{fmt_number(_settlement_fee(row), 4)}</td>"
                f"<td>{fmt_number(_settlement_gross_pnl(row), 2)}</td>"
                f"<td>{fmt_number(_settlement_net_pnl(row), 2)}</td>"
                f"<td>{escape(str(row.get('notes', '-')))}</td>"
                "</tr>"
            )
            for row in reversed(rows[-12:])
            if isinstance(row, dict)
        )

    def render_short_expiry_rows() -> str:
        rows = payload.get("short_expiry_features", [])
        if not isinstance(rows, list) or not rows:
            return "<tr><td colspan='12'>Aucun snapshot short-expiry loggé.</td></tr>"
        return "".join(
            (
                "<tr>"
                f"<td>{escape(str(row.get('ts', '-')))}</td>"
                f"<td>{escape(str(row.get('underlying', '-')))}</td>"
                f"<td>{escape(str(row.get('period', '-')))}</td>"
                f"<td>{fmt_number(row.get('seconds_left'), 0)}</td>"
                f"<td>{fmt_number(row.get('distance_bps'), 2)}</td>"
                f"<td>{fmt_number(row.get('momentum_bps_60s'), 2)}</td>"
                f"<td>{fmt_number(row.get('book_probability_yes'), 4)}</td>"
                f"<td>{fmt_number(row.get('short_probability_yes'), 4)}</td>"
                f"<td>{escape(str(row.get('best_side', '-')))}</td>"
                f"<td>{fmt_number(row.get('best_net_edge'), 6)}</td>"
                f"<td>{fmt_number(row.get('confidence'), 4)}</td>"
                f"<td>{escape(str(row.get('reason', '-')))}</td>"
                "</tr>"
            )
            for row in reversed(rows[-16:])
            if isinstance(row, dict)
        )

    def render_short_watchlist_rows() -> str:
        if not short_watchlist:
            return "<tr><td colspan='11'>Aucune fenêtre short-expiry surveillée dans la dernière boucle.</td></tr>"
        return "".join(
            (
                "<tr>"
                f"<td>{escape(str(row.get('readiness', '-')))}</td>"
                f"<td>{escape(str(row.get('underlying', '-')))}</td>"
                f"<td>{escape(str(row.get('period', '-')))}</td>"
                f"<td>{fmt_number(row.get('seconds_left'), 0)}</td>"
                f"<td>{fmt_number(row.get('reference_price'), 6)}</td>"
                f"<td>{fmt_number(row.get('strike'), 6)}</td>"
                f"<td>{fmt_number(row.get('distance_bps'), 2)}</td>"
                f"<td>{fmt_number(row.get('momentum_bps_60s'), 2)}</td>"
                f"<td>{escape(str(row.get('best_side') or '-'))}</td>"
                f"<td>{fmt_number(row.get('best_net_edge'), 6)}</td>"
                f"<td>{escape(str(row.get('reason', '-')))}</td>"
                "</tr>"
            )
            for row in short_watchlist
            if isinstance(row, dict)
        )

    def render_execution_rows() -> str:
        rows = payload.get("execution_results", [])
        if not isinstance(rows, list) or not rows:
            return "<tr><td colspan='10'>Aucune tentative d'exécution persistée pour le moment.</td></tr>"
        return "".join(
            (
                "<tr>"
                f"<td>{escape(str(row.get('ts', '-')))}</td>"
                f"<td>{escape(str(row.get('underlying', '-')))}</td>"
                f"<td>{escape(str(row.get('edge_type', '-')))}</td>"
                f"<td>{escape(str(row.get('side', '-')))}</td>"
                f"<td>{fmt_number(row.get('approved_size_usdc'), 2)}</td>"
                f"<td>{escape(str(row.get('status', '-')))}</td>"
                f"<td>{'yes' if row.get('filled') else 'no'}</td>"
                f"<td>{fmt_number(row.get('total_cost_usdc'), 4)}</td>"
                f"<td>{escape(_execution_fill_statuses(row))}</td>"
                f"<td>{escape(str(row.get('error') or '-'))}</td>"
                "</tr>"
            )
            for row in reversed(rows[-16:])
            if isinstance(row, dict)
        )

    def render_replay_rows() -> str:
        rows = payload.get("replay", [])
        if not isinstance(rows, list) or not rows:
            return "<tr><td colspan='8'>Replay vide pour le moment.</td></tr>"
        return "".join(
            (
                "<tr>"
                f"<td>{escape(str(row.get('date', '-')))}</td>"
                f"<td>{escape(str(row.get('underlying', '-')))}</td>"
                f"<td>{escape(str(row.get('edge_type', '-')))}</td>"
                f"<td>{escape(str(row.get('side', '-')))}</td>"
                f"<td>{escape(str(row.get('opportunity_count', 0)))}</td>"
                f"<td>{fmt_number(row.get('avg_net_edge'), 6)}</td>"
                f"<td>{fmt_number(row.get('max_net_edge'), 6)}</td>"
                f"<td>{fmt_number(row.get('avg_confidence'), 4)}</td>"
                "</tr>"
            )
            for row in rows
            if isinstance(row, dict)
        )

    def render_edge_decay_rows() -> str:
        rows = payload.get("edge_decay", [])
        if not isinstance(rows, list) or not rows:
            return "<tr><td colspan='8'>Aucune dérive d'edge visible.</td></tr>"
        return "".join(
            (
                "<tr>"
                f"<td>{escape(str(row.get('ts', '-')))}</td>"
                f"<td>{escape(str(row.get('underlying', '-')))}</td>"
                f"<td>{escape(str(row.get('edge_type', '-')))}</td>"
                f"<td>{escape(str(row.get('side', '-')))}</td>"
                f"<td>{fmt_number(row.get('first_net_edge'), 6)}</td>"
                f"<td>{fmt_number(row.get('current_net_edge'), 6)}</td>"
                f"<td>{fmt_number(row.get('delta_net_edge'), 6)}</td>"
                f"<td>{escape(str(row.get('elapsed_seconds', '-')))}</td>"
                "</tr>"
            )
            for row in reversed(rows[-12:])
            if isinstance(row, dict)
        )

    def render_latency_rows() -> str:
        rows = payload.get("latency", [])
        if not isinstance(rows, list) or not rows:
            return "<tr><td colspan='7'>Aucune latence loggée.</td></tr>"
        return "".join(
            (
                "<tr>"
                f"<td>{escape(str(row.get('ts', '-')))}</td>"
                f"<td>{escape(str(row.get('loop_count', '-')))}</td>"
                f"<td>{fmt_number(row.get('total_ms'), 1)}</td>"
                f"<td>{fmt_number(row.get('reference_prices_ms'), 1)}</td>"
                f"<td>{fmt_number(row.get('books_ms'), 1)}</td>"
                f"<td>{fmt_number(row.get('opportunities'), 0)}</td>"
                f"<td>{escape(str(row.get('error', '')))}</td>"
                "</tr>"
            )
            for row in reversed(rows[-8:])
            if isinstance(row, dict)
        )

    def render_mainnet_reference_rows() -> str:
        rows = mainnet_payload.get("reference_prices", [])
        if not isinstance(rows, list) or not rows:
            return "<tr><td colspan='5'>Aucune référence mainnet visible.</td></tr>"
        return "".join(
            (
                "<tr>"
                f"<td>{escape(str(row.get('underlying', '-')))}</td>"
                f"<td>{fmt_number(row.get('price'), 6)}</td>"
                f"<td>{escape(str(row.get('source_count', 0)))}</td>"
                f"<td>{escape(str(row.get('rejected_count', 0)))}</td>"
                f"<td>{fmt_number(row.get('max_deviation_bps'), 2)}</td>"
                "</tr>"
            )
            for row in rows
            if isinstance(row, dict)
        )

    def render_mainnet_opportunity_rows() -> str:
        rows = mainnet_payload.get("opportunities", [])
        if not isinstance(rows, list) or not rows:
            return "<tr><td colspan='10'>Aucune opportunité mainnet loggée.</td></tr>"
        return "".join(
            (
                "<tr>"
                f"<td>{escape(str(row.get('ts', '-')))}</td>"
                f"<td>{escape(str(row.get('underlying', '-')))}</td>"
                f"<td>{escape(str(row.get('edge_type', '-')))}</td>"
                f"<td>{escape(str(row.get('side', '-')))}</td>"
                f"<td>{fmt_number(row.get('net_edge'), 6)}</td>"
                f"<td>{fmt_number(row.get('gross_edge'), 6)}</td>"
                f"<td>{fmt_number(row.get('confidence'), 4)}</td>"
                f"<td>{fmt_number(row.get('ref_price'), 6)}</td>"
                f"<td>{fmt_number(row.get('yes_ask'), 6)}</td>"
                f"<td>{escape(str(row.get('reason', '-')))}</td>"
                "</tr>"
            )
            for row in reversed(rows[-16:])
            if isinstance(row, dict)
        )

    def render_mainnet_replay_rows() -> str:
        rows = mainnet_payload.get("replay", [])
        if not isinstance(rows, list) or not rows:
            return "<tr><td colspan='8'>Replay mainnet vide pour le moment.</td></tr>"
        return "".join(
            (
                "<tr>"
                f"<td>{escape(str(row.get('date', '-')))}</td>"
                f"<td>{escape(str(row.get('underlying', '-')))}</td>"
                f"<td>{escape(str(row.get('edge_type', '-')))}</td>"
                f"<td>{escape(str(row.get('side', '-')))}</td>"
                f"<td>{escape(str(row.get('opportunity_count', 0)))}</td>"
                f"<td>{fmt_number(row.get('avg_net_edge'), 6)}</td>"
                f"<td>{fmt_number(row.get('max_net_edge'), 6)}</td>"
                f"<td>{fmt_number(row.get('avg_confidence'), 4)}</td>"
                "</tr>"
            )
            for row in rows
            if isinstance(row, dict)
        )

    def render_mainnet_latency_rows() -> str:
        rows = mainnet_payload.get("latency", [])
        if not isinstance(rows, list) or not rows:
            return "<tr><td colspan='7'>Aucune latence mainnet loggée.</td></tr>"
        return "".join(
            (
                "<tr>"
                f"<td>{escape(str(row.get('ts', '-')))}</td>"
                f"<td>{escape(str(row.get('loop_count', '-')))}</td>"
                f"<td>{fmt_number(row.get('total_ms'), 1)}</td>"
                f"<td>{fmt_number(row.get('reference_prices_ms'), 1)}</td>"
                f"<td>{fmt_number(row.get('books_ms'), 1)}</td>"
                f"<td>{fmt_number(row.get('opportunities'), 0)}</td>"
                f"<td>{escape(str(row.get('error', '')))}</td>"
                "</tr>"
            )
            for row in reversed(rows[-8:])
            if isinstance(row, dict)
        )

    def render_dashboard_open_position_rows() -> str:
        rows = [row for row in open_positions if isinstance(row, dict)]
        if not rows:
            return "<tr><td colspan='9'>Aucune position ouverte.</td></tr>"
        return "".join(
            (
                "<tr>"
                f"<td>{escape(str(row.get('underlying', '-')))}</td>"
                f"<td>{escape(str(row.get('market_id', '-')))}</td>"
                f"<td>{escape(str(row.get('side', '-')))}</td>"
                f"<td>{escape(str(row.get('status', '-')))}</td>"
                f"<td>{fmt_number(row.get('cost_usdc'), 2)}</td>"
                f"<td>{fmt_number(row.get('max_loss_usdc'), 2)}</td>"
                f"<td>{fmt_signed_number(row.get('estimated_pnl_usdc'), 2)}</td>"
                f"<td>{fmt_number(row.get('net_edge'), 5)}</td>"
                f"<td>{escape(str(row.get('opened_at') or '-'))}</td>"
                "</tr>"
            )
            for row in rows[:12]
        )

    def render_dashboard_closed_position_rows() -> str:
        rows = [row for row in settled_positions if isinstance(row, dict)]
        if rows:
            return "".join(
                (
                    "<tr>"
                    f"<td>{escape(str(row.get('underlying', '-')))}</td>"
                    f"<td>{escape(str(row.get('market_id', '-')))}</td>"
                    f"<td>{escape(str(row.get('side', '-')))}</td>"
                    f"<td>{escape(str(row.get('status') or row.get('result') or '-'))}</td>"
                    f"<td>{fmt_number(row.get('cost_usdc'), 2)}</td>"
                    f"<td>{fmt_number(first_present(row.get('estimated_payout_usdc'), row.get('payout_usdc')), 2)}</td>"
                    f"<td>{fmt_signed_number(first_present(row.get('estimated_pnl_usdc'), row.get('net_pnl_usdc')), 2)}</td>"
                    f"<td>{escape(str(row.get('settled_at') or row.get('closed_at') or '-'))}</td>"
                    "</tr>"
                )
                for row in reversed(rows[-12:])
            )
        settlement_rows = [
            row for row in payload.get("settlements", []) if isinstance(row, dict)
        ]
        if not settlement_rows:
            return "<tr><td colspan='8'>Aucune position fermée.</td></tr>"
        return "".join(
            (
                "<tr>"
                f"<td>{escape(str(row.get('underlying', '-')))}</td>"
                f"<td>{escape(str(row.get('market_id', '-')))}</td>"
                f"<td>{escape(str(row.get('side', '-')))}</td>"
                f"<td>{escape(str(row.get('result', '-')))}</td>"
                f"<td>{fmt_number(row.get('cost_usdc'), 2)}</td>"
                f"<td>{fmt_number(row.get('payout_usdc'), 2)}</td>"
                f"<td>{fmt_signed_number(_settlement_net_pnl(row), 2)}</td>"
                f"<td>{escape(str(row.get('ts', '-')))}</td>"
                "</tr>"
            )
            for row in reversed(settlement_rows[-12:])
        )

    def render_dashboard_runtime_rows() -> str:
        rows = [
            ("Process", payload.get("process_state", "-")),
            ("Freshness", "fresh" if payload.get("fresh") else "stale"),
            ("Age status", age_label),
            ("Mode", mode_label),
            ("Network", network_label),
            ("Capital status", capital.get("reason", "-")),
            ("Logs", logs_dir_label or "-"),
            (
                "Dernière boucle",
                (
                    f"signals {payload.get('opportunities_this_loop', 0)} · "
                    f"approved {payload.get('approved_this_loop', 0)} · "
                    f"executed {payload.get('executed_this_loop', 0)}"
                ),
            ),
        ]
        return "".join(
            f"<tr><th>{escape(label)}</th><td>{escape(str(value))}</td></tr>"
            for label, value in rows
        )

    dashboard_cards = render_stat_cards(
        [
            {
                "label": "Capital dispo",
                "value": f"{fmt_number(available_capital, 2)} USD",
                "note": f"budget total {fmt_number(capital.get('budget_usdc'), 2)}",
            },
            {
                "label": "Capital utilisé",
                "value": f"{fmt_number(used_capital, 2)} USD",
                "note": f"{len(open_positions)} position(s) ouverte(s)",
            },
            {
                "label": "PnL visible",
                "value": f"{fmt_signed_number(visible_pnl, 2)} USD",
                "note": f"réalisé {fmt_signed_number(realized_pnl, 2)} · latent {fmt_signed_number(open_pnl, 2)}",
            },
            {
                "label": "Win rate",
                "value": fmt_pct(win_rate),
                "note": f"{win_count} win · {loss_count} loss",
            },
            {
                "label": "Runtime",
                "value": str(payload.get("process_state", "-")),
                "note": f"{'fresh' if payload.get('fresh') else 'stale'} · age {age_label}",
            },
            {
                "label": "Mode",
                "value": mode_label,
                "note": str(capital.get("mode") or "execution HIP4"),
            },
            {
                "label": "Network",
                "value": network_label,
                "note": "books et markets observés",
            },
            {
                "label": "Markets",
                "value": f"{payload.get('markets_supported', 0)}/{payload.get('markets_seen', 0)}",
                "note": "supportés / vus",
            },
            {
                "label": "Fermées",
                "value": str(settled_position_count),
                "note": f"{fill_count} fill(s)",
            },
            {
                "label": "Dernière boucle",
                "value": str(payload.get("opportunities_this_loop", 0)),
                "note": f"approved {payload.get('approved_this_loop', 0)} · executed {payload.get('executed_this_loop', 0)}",
            },
        ]
    )

    cards = render_stat_cards(
        [
            {
                "label": "Runtime",
                "value": str(payload.get("process_state", "-")),
                "note": f"âge status {age_label}",
            },
            {
                "label": "Mode",
                "value": str(payload.get("mode", "observer")),
                "note": (
                    "ordres testnet actifs"
                    if str(payload.get("mode", "")).lower() == "testnet"
                    else "ordre désactivé"
                ),
            },
            {
                "label": "Markets",
                "value": f"{payload.get('markets_supported', 0)}/{payload.get('markets_seen', 0)}",
                "note": "supportés / vus",
            },
            {
                "label": "Realized PnL",
                "value": f"{fmt_number(realized_pnl, 2)} USD",
                "note": f"net fees · {settled_position_count} settlement(s)",
            },
            {
                "label": "Gross/Fees",
                "value": f"{fmt_number(gross_pnl, 2)} USD",
                "note": f"fees {fmt_number(fees_usd, 4)} USD",
            },
            {
                "label": "Win rate",
                "value": fmt_pct(win_rate),
                "note": f"{win_count} win · {loss_count} loss",
            },
            {
                "label": "Fills",
                "value": str(fill_count),
                "note": "fills cumulés",
            },
            {
                "label": "Exec attempts",
                "value": str(len(payload.get("execution_results", []) or [])),
                "note": "dernières tentatives persistées",
            },
            {
                "label": "Loop edge",
                "value": fmt_number(latest_edge, 4),
                "note": "dernier net edge loggé",
            },
            {
                "label": "Best short",
                "value": fmt_number(best_short_edge, 4),
                "note": f"latest {fmt_number(latest_short_edge, 4)}",
            },
            {
                "label": "Short focus",
                "value": str(short_brief.get("label", "-")),
                "note": (
                    f"ready {short_brief.get('ready_count', 0)} · "
                    f"watch {short_brief.get('candidate_count', 0)}"
                ),
            },
            {
                "label": "Next window",
                "value": (
                    _format_duration_compact(float(short_brief.get("next_window_seconds")))
                    if _float_or_none(short_brief.get("next_window_seconds")) is not None
                    else "-"
                ),
                "note": "short-expiry prioritaire",
            },
            {
                "label": "Best edge",
                "value": fmt_number(best_edge, 4),
                "note": "meilleur net edge récent",
            },
            {
                "label": "Loop signals",
                "value": str(payload.get("opportunities_this_loop", 0)),
                "note": "opportunités sur la dernière boucle",
            },
            {
                "label": "Budget",
                "value": f"{fmt_number(capital.get('remaining_budget_usdc'), 2)} USD",
                "note": f"sur {fmt_number(capital.get('budget_usdc'), 2)} USD",
            },
            {
                "label": f"Quote {balance_coin}",
                "value": f"{fmt_number(capital.get('testnet_available_usdc'), 2)} {balance_coin}",
                "note": str(
                    capital.get("testnet_spot_transfer_status")
                    or capital.get("testnet_balance_source")
                    or capital.get("reason", "capital")
                ),
            },
        ]
    )
    mainnet_cards = render_stat_cards(
        [
            {
                "label": "Mainnet observer",
                "value": str(mainnet_payload.get("process_state", "-")),
                "note": f"âge status {mainnet_age_label}",
            },
            {
                "label": "Mode",
                "value": str(mainnet_payload.get("mode", "observer")),
                "note": "ordre impossible",
            },
            {
                "label": "Markets",
                "value": f"{mainnet_payload.get('markets_supported', 0)}/{mainnet_payload.get('markets_seen', 0)}",
                "note": "supportés / vus",
            },
            {
                "label": "Loop edge",
                "value": fmt_number(mainnet_payload.get("latest_net_edge"), 4),
                "note": "dernier net edge mainnet",
            },
            {
                "label": "Best edge",
                "value": fmt_number(mainnet_payload.get("best_net_edge"), 4),
                "note": "meilleur net edge mainnet",
            },
            {
                "label": "Signals",
                "value": str(mainnet_payload.get("opportunities_this_loop", 0)),
                "note": "opportunités dernière boucle",
            },
            {
                "label": "Logs",
                "value": "mainnet",
                "note": str(mainnet_payload.get("logs_dir", "logs/hip4_outcome_mainnet")),
            },
        ]
    )

    return f"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TRIDENT HIP-4 Outcome</title>
  <style>
    :root {{
      --bg: #f4f7f8;
      --panel: #ffffff;
      --text: #1f2a33;
      --muted: #66727c;
      --line: #d5e0e5;
      --accent: #145b57;
      --accent-soft: #d8eeeb;
      --good: #176b3a;
      --bad: #a12d2f;
      --warn: #9a6700;
      --shadow: 0 18px 40px rgba(31, 42, 51, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--text);
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
      background: var(--bg);
    }}
    main {{ max-width: 1380px; margin: 0 auto; padding: 28px 18px 48px; }}
    h1, h2, h3 {{ margin: 0; }}
    p {{ margin: 0; color: var(--muted); }}
    a {{ color: var(--accent); font-weight: 700; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .hero, .panel, .metric-card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 14px;
      box-shadow: var(--shadow);
    }}
    .hero {{ padding: 22px; display: grid; gap: 12px; }}
    .hero h1 {{
      font-family: "Fraunces", "Iowan Old Style", Georgia, serif;
      font-size: clamp(2rem, 4vw, 3.1rem);
      line-height: 1.05;
    }}
    .chip-row, .hero-links {{ display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }}
    .chip {{
      display: inline-flex;
      padding: 7px 12px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent);
      font-weight: 700;
      font-size: 0.9rem;
    }}
    .badge-good {{ background: #ddf5e5; color: var(--good); }}
    .badge-warn {{ background: #fff2c2; color: var(--warn); }}
    .badge-bad {{ background: #ffe1e1; color: var(--bad); }}
    .badge-neutral {{ background: #e9edf0; color: var(--muted); }}
    .badge {{
      display: inline-flex;
      padding: 7px 12px;
      border-radius: 999px;
      font-weight: 800;
    }}
    .grid {{ display: grid; gap: 16px; margin-top: 18px; }}
    .metric-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }}
    .metric-card {{ padding: 16px; display: grid; gap: 6px; }}
    .metric-card span, .metric-card small {{ color: var(--muted); }}
    .metric-card strong {{ font-size: 1.55rem; }}
    .tab-bar {{
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      border-bottom: 1px solid var(--line);
      padding-top: 4px;
    }}
    .tab-button {{
      appearance: none;
      border: 1px solid var(--line);
      border-bottom: 0;
      border-radius: 8px 8px 0 0;
      background: #e9f0f3;
      color: var(--muted);
      cursor: pointer;
      font: inherit;
      font-weight: 800;
      padding: 10px 14px;
    }}
    .tab-button.is-active {{
      background: var(--panel);
      color: var(--accent);
      box-shadow: 0 -8px 22px rgba(31, 42, 51, 0.06);
    }}
    .tab-panel {{ display: none; }}
    .tab-panel.is-active {{ display: grid; gap: 16px; }}
    .panel {{ padding: 18px; display: grid; gap: 14px; }}
    .panel-header {{ display: flex; justify-content: space-between; gap: 12px; align-items: start; flex-wrap: wrap; }}
    .observation-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }}
    .observation-card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      padding: 16px;
      display: grid;
      gap: 7px;
    }}
    .observation-card-head {{ display: flex; justify-content: space-between; gap: 10px; align-items: center; }}
    .observation-card-head span:first-child {{ color: var(--muted); font-weight: 800; }}
    .observation-card strong {{ font-size: 1.7rem; }}
    .observation-card small {{ color: var(--muted); }}
    .health-pill {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
      border-radius: 999px;
      padding: 6px 10px;
      font-weight: 800;
      line-height: 1;
    }}
    .health-pill-good {{ background: #ddf5e5; color: var(--good); }}
    .health-pill-warn {{ background: #fff2c2; color: var(--warn); }}
    .health-pill-bad {{ background: #ffe1e1; color: var(--bad); }}
    .health-pill-neutral {{ background: #e9edf0; color: var(--muted); }}
    .status-dot {{
      width: 9px;
      height: 9px;
      border-radius: 999px;
      display: inline-block;
      flex: 0 0 auto;
    }}
    .status-dot-good {{ background: var(--good); }}
    .status-dot-warn {{ background: var(--warn); }}
    .status-dot-bad {{ background: var(--bad); }}
    .status-dot-neutral {{ background: var(--muted); }}
    .table-wrap {{ overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 0.92rem; }}
    th, td {{ padding: 10px 9px; border-bottom: 1px solid var(--line); text-align: left; white-space: nowrap; }}
    th {{ color: var(--muted); font-size: 0.78rem; text-transform: uppercase; }}
    td:last-child {{ white-space: normal; min-width: 220px; }}
    .two-col {{ display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(320px, 0.9fr); gap: 16px; }}
    .dashboard-stack {{ display: grid; gap: 16px; }}
    .dashboard-focus {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(320px, 0.8fr); gap: 16px; align-items: start; }}
    @media (max-width: 980px) {{ .two-col {{ grid-template-columns: 1fr; }} }}
    @media (max-width: 980px) {{ .dashboard-focus {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body data-default-tab="dashboard" data-refresh-seconds="{refresh_seconds}">
  <main>
    <header class="hero">
      <div class="chip-row">
        <span class="chip">Version {escape(VERSION)}</span>
        <span class="chip">Profile {escape(str(supervisor.profile))}</span>
        <span class="chip">HIP4 {escape(mode_label)}</span>
        <span class="chip">Network {escape(network_label)}</span>
        <span class="chip">Nautilus shadow {escape(shadow_label)}</span>
        <span class="chip">Auto-refresh {refresh_seconds}s</span>
        <span class="badge badge-{tone}">{'fresh' if payload.get('fresh') else 'stale'}</span>
      </div>
      <h1>HIP-4 Outcome Experimental</h1>
      <p>Mode {escape(mode_label)} · réseau {escape(network_label)} · runtime HIP4 isolé.</p>
      <div class="hero-links">
        <span>Last updated: {escape(refreshed_at)}</span>
        <a href="/">HIP4 UI</a>
        <a href="/hip4-outcome#observation">Observation</a>
        <a href="/api/hip4-outcome">/api/hip4-outcome</a>
        <a href="/api/hip4-outcome-mainnet">/api/hip4-outcome-mainnet</a>
        <a href="/api/hip4-nautilus-shadow">/api/hip4-nautilus-shadow</a>
        <a href="/health">/health</a>
      </div>
    </header>

    <div class="grid">
      <nav class="tab-bar" aria-label="HIP-4 sections">
        <button class="tab-button is-active" type="button" data-hip4-tab="dashboard" aria-selected="true">Dashboard</button>
        <button class="tab-button" type="button" data-hip4-tab="details" aria-selected="false">Détails</button>
        <button class="tab-button" type="button" data-hip4-tab="observation" aria-selected="false">Observation</button>
      </nav>

      <section class="tab-panel is-active" data-hip4-panel="dashboard">
        <section class="metric-grid">{dashboard_cards}</section>

        <section class="two-col">
          <section class="panel">
            <div class="panel-header"><h2>Positions ouvertes</h2><p>Capital actuellement engagé par HIP4.</p></div>
            <div class="table-wrap">
              <table>
                <thead><tr><th>Underlying</th><th>Market</th><th>Side</th><th>Status</th><th>Cost</th><th>Max loss</th><th>PnL est.</th><th>Net edge</th><th>Opened</th></tr></thead>
                <tbody>{render_dashboard_open_position_rows()}</tbody>
              </table>
            </div>
          </section>

          <section class="panel">
            <div class="panel-header"><h2>Positions fermées</h2><p>Derniers settlements visibles.</p></div>
            <div class="table-wrap">
              <table>
                <thead><tr><th>Underlying</th><th>Market</th><th>Side</th><th>Result</th><th>Cost</th><th>Payout</th><th>PnL</th><th>Closed</th></tr></thead>
                <tbody>{render_dashboard_closed_position_rows()}</tbody>
              </table>
            </div>
          </section>
        </section>

        <section class="two-col">
          <section class="panel">
            <div class="panel-header"><h2>Runtime HIP4</h2><p>Etat opérateur minimal du runner.</p></div>
            <div class="table-wrap">
              <table>
                <tbody>{render_dashboard_runtime_rows()}</tbody>
              </table>
            </div>
          </section>

          <section class="panel">
            <div class="panel-header"><h2>Observation runtime</h2><p>Santé des flux et de l'observateur mainnet.</p></div>
            <section class="observation-grid">{render_observation_cards()}</section>
          </section>
        </section>
      </section>

      <section class="tab-panel" data-hip4-panel="details" hidden>
      <section class="metric-grid">{cards}</section>

      <section class="panel">
        <div class="panel-header">
          <h2>Mainnet observer</h2>
          <p>{escape(str(mainnet_payload.get('status_path', 'logs/hip4_outcome_mainnet_status.json')))} · {escape(str(mainnet_payload.get('logs_dir', 'logs/hip4_outcome_mainnet')))}</p>
        </div>
      </section>
      <section class="metric-grid">{mainnet_cards}</section>

      <section class="two-col">
        <div class="panel">
          <div class="panel-header"><h2>Opportunités mainnet</h2><p>Observation pure: décisions rejetées en mode observer, aucune exécution.</p></div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Ts</th><th>Underlying</th><th>Edge</th><th>Side</th><th>Net</th><th>Gross</th><th>Conf</th><th>Ref</th><th>Yes ask</th><th>Reason</th></tr></thead>
              <tbody>{render_mainnet_opportunity_rows()}</tbody>
            </table>
          </div>
        </div>
        <div class="panel">
          <div class="panel-header"><h2>Sources prix mainnet</h2><p>Références de la dernière boucle mainnet.</p></div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Underlying</th><th>Price</th><th>Sources</th><th>Rejected</th><th>Max dev bps</th></tr></thead>
              <tbody>{render_mainnet_reference_rows()}</tbody>
            </table>
          </div>
        </div>
      </section>

      <section class="two-col">
        <div class="panel">
          <div class="panel-header"><h2>Replay mainnet</h2><p>Agrégation des opportunités mainnet déjà collectées.</p></div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Date</th><th>Underlying</th><th>Edge</th><th>Side</th><th>Count</th><th>Avg net</th><th>Max net</th><th>Avg conf</th></tr></thead>
              <tbody>{render_mainnet_replay_rows()}</tbody>
            </table>
          </div>
        </div>
        <div class="panel">
          <div class="panel-header"><h2>Latence mainnet</h2><p>Dernières boucles de l'observateur mainnet.</p></div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Ts</th><th>Loop</th><th>Total ms</th><th>Refs ms</th><th>Books ms</th><th>Opps</th><th>Error</th></tr></thead>
              <tbody>{render_mainnet_latency_rows()}</tbody>
            </table>
          </div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-header"><h2>Exécutions paper</h2><p>Réponses persistées après les décisions approuvées en dry-run.</p></div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Ts</th><th>Underlying</th><th>Edge</th><th>Side</th><th>Approved</th><th>Status</th><th>Filled</th><th>Cost</th><th>Fill statuses</th><th>Error</th></tr></thead>
            <tbody>{render_execution_rows()}</tbody>
          </table>
        </div>
      </section>

      <section class="panel">
        <div class="panel-header"><h2>Performance par coin</h2><p>Settlements exchange en testnet, estimations locales en paper, et positions encore ouvertes regroupées par underlying.</p></div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Coin</th><th>Sens</th><th>Trades</th><th>Ouvert</th><th>Win rate</th><th>PnL réalisé</th><th>PnL latent</th><th>PnL visible</th></tr></thead>
            <tbody>{render_coin_summary_rows()}</tbody>
          </table>
        </div>
      </section>

      <section class="panel">
        <div class="panel-header"><h2>Settlements paper / testnet HIP-4</h2><p>En testnet, PnL repris depuis les fills Hyperliquid Settlement; en paper, estimation locale.</p></div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Ts</th><th>Underlying</th><th>Market</th><th>Side</th><th>Result</th><th>Payout</th><th>Fees</th><th>Gross PnL</th><th>Net PnL</th><th>Notes</th></tr></thead>
            <tbody>{render_settlement_rows()}</tbody>
          </table>
        </div>
      </section>

      <section class="panel">
        <div class="panel-header"><h2>Signal court terme</h2><p>Snapshots du modèle court terme YES/NO.</p></div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Ts</th><th>Underlying</th><th>Period</th><th>T-exp s</th><th>Dist bps</th><th>Mom 60s</th><th>Book pY</th><th>Short pY</th><th>Best side</th><th>Net</th><th>Conf</th><th>Reason</th></tr></thead>
            <tbody>{render_short_expiry_rows()}</tbody>
          </table>
        </div>
      </section>

      <section class="panel">
        <div class="panel-header"><h2>Short-expiry watchlist</h2><p>Fenêtres proches expiry priorisées par la dernière boucle du pod.</p></div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Status</th><th>Underlying</th><th>Period</th><th>T-exp s</th><th>Ref</th><th>Strike</th><th>Dist bps</th><th>Mom 60s</th><th>Best side</th><th>Net</th><th>Reason</th></tr></thead>
            <tbody>{render_short_watchlist_rows()}</tbody>
          </table>
        </div>
      </section>

      <section class="two-col">
        <div class="panel">
          <div class="panel-header"><h2>Opportunités récentes</h2><p>{escape(str(payload.get('logs_dir')))}</p></div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Ts</th><th>Underlying</th><th>Edge</th><th>Side</th><th>Net</th><th>Gross</th><th>Conf</th><th>Ref</th><th>Yes ask</th><th>Reason</th></tr></thead>
              <tbody>{render_opportunity_rows()}</tbody>
            </table>
          </div>
        </div>
        <div class="panel">
          <div class="panel-header"><h2>Sources prix</h2><p>Références utilisées par la dernière boucle.</p></div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Underlying</th><th>Price</th><th>Sources</th><th>Rejected</th><th>Max dev bps</th></tr></thead>
              <tbody>{render_reference_rows()}</tbody>
            </table>
          </div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-header"><h2>Replay signal</h2><p>Agrégation des opportunités déjà collectées.</p></div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Date</th><th>Underlying</th><th>Edge</th><th>Side</th><th>Count</th><th>Avg net</th><th>Max net</th><th>Avg conf</th></tr></thead>
            <tbody>{render_replay_rows()}</tbody>
          </table>
        </div>
      </section>

      <section class="two-col">
        <div class="panel">
          <div class="panel-header"><h2>Edge decay</h2><p>Dérive des signaux réobservés.</p></div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Ts</th><th>Underlying</th><th>Edge</th><th>Side</th><th>First</th><th>Current</th><th>Delta</th><th>Elapsed s</th></tr></thead>
              <tbody>{render_edge_decay_rows()}</tbody>
            </table>
          </div>
        </div>
        <div class="panel">
          <div class="panel-header"><h2>Latence</h2><p>Dernières boucles du collecteur.</p></div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Ts</th><th>Loop</th><th>Total ms</th><th>Refs ms</th><th>Books ms</th><th>Opps</th><th>Error</th></tr></thead>
              <tbody>{render_latency_rows()}</tbody>
            </table>
          </div>
        </div>
      </section>
      </section>

      <section class="tab-panel" data-hip4-panel="observation" hidden>
        <section class="observation-grid">
          {render_observation_cards()}
        </section>

        <section class="panel">
          <div class="panel-header">
            <h2>Observation HIP-4</h2>
            <p>Pastilles: vert = classe reconnue et exploitable en observation, jaune = watch-only ou marché non supporté, rouge = erreur book.</p>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Profile</th><th>État</th><th>Classe</th><th>Count</th><th>Support</th><th>Latest</th></tr></thead>
              <tbody>{render_observation_summary_rows()}</tbody>
            </table>
          </div>
        </section>

        <section class="panel">
          <div class="panel-header">
            <h2>Marchés observés</h2>
            <p>Dernières lignes `market_observations.jsonl` mainnet paper et mainnet observer.</p>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Profile</th><th>État</th><th>Ts</th><th>Classe</th><th>Support</th><th>Nom</th><th>Underlying</th><th>Bucket</th><th>Coins</th><th>Books</th><th>Reason</th><th>Description</th></tr></thead>
              <tbody>{render_observation_rows()}</tbody>
            </table>
          </div>
        </section>
      </section>
    </div>
  </main>
  <script>
    (() => {{
      const body = document.body;
      const refreshSeconds = Number(body.dataset.refreshSeconds || "0");
      const tabs = Array.from(document.querySelectorAll("[data-hip4-tab]"));
      const panels = Array.from(document.querySelectorAll("[data-hip4-panel]"));
      const valid = new Set(tabs.map((button) => button.dataset.hip4Tab));
      function normalizedTab(name) {{
        return valid.has(name) ? name : body.dataset.defaultTab || "dashboard";
      }}
      function activeTabName() {{
        const active = tabs.find((button) => button.classList.contains("is-active"));
        return normalizedTab(active ? active.dataset.hip4Tab : (window.location.hash || "").replace("#", ""));
      }}
      function scrollStorageKey(tabName) {{
        return `trident-hip4:scroll:${{window.location.pathname}}:${{normalizedTab(tabName)}}`;
      }}
      function lastTabStorageKey() {{
        return `trident-hip4:tab:${{window.location.pathname}}`;
      }}
      function focusStorageKey() {{
        return `trident-hip4:focus:${{window.location.pathname}}`;
      }}
      function cssEscape(value) {{
        if (window.CSS && typeof window.CSS.escape === "function") {{
          return window.CSS.escape(String(value));
        }}
        return String(value).replace(/[^a-zA-Z0-9_-]/g, "");
      }}
      function focusSelector(element) {{
        if (!element || element === document.body) return "";
        if (element.dataset.hip4Tab) {{
          return `[data-hip4-tab="${{cssEscape(element.dataset.hip4Tab)}}"]`;
        }}
        if (element.id) {{
          return `#${{cssEscape(element.id)}}`;
        }}
        if (element.name) {{
          return `${{element.tagName.toLowerCase()}}[name="${{cssEscape(element.name)}}"]`;
        }}
        return "";
      }}
      function isEditable(element) {{
        return element instanceof HTMLInputElement
          || element instanceof HTMLTextAreaElement
          || element instanceof HTMLSelectElement;
      }}
      function saveScrollPosition(tabName = activeTabName()) {{
        try {{
          window.sessionStorage.setItem(scrollStorageKey(tabName), String(window.scrollY || 0));
        }} catch (_error) {{
          // Keep refresh behavior intact if sessionStorage is unavailable.
        }}
      }}
      function restoreScrollPosition(tabName = activeTabName()) {{
        try {{
          const raw = window.sessionStorage.getItem(scrollStorageKey(tabName));
          if (raw === null) return;
          const next = Number(raw);
          if (!Number.isFinite(next) || next < 0) return;
          window.requestAnimationFrame(() => window.scrollTo(0, next));
        }} catch (_error) {{
          // Keep refresh behavior intact if sessionStorage is unavailable.
        }}
      }}
      function saveActiveTab(tabName) {{
        try {{
          window.sessionStorage.setItem(lastTabStorageKey(), normalizedTab(tabName));
        }} catch (_error) {{
          // Keep refresh behavior intact if sessionStorage is unavailable.
        }}
      }}
      function savedTabName() {{
        try {{
          return normalizedTab(window.sessionStorage.getItem(lastTabStorageKey()) || "");
        }} catch (_error) {{
          return body.dataset.defaultTab || "dashboard";
        }}
      }}
      function saveFocusedElement() {{
        const element = document.activeElement;
        const selector = focusSelector(element);
        if (!selector) return;
        const payload = {{ selector }};
        if (isEditable(element) && element.type !== "password") {{
          payload.value = element.value;
          if (typeof element.selectionStart === "number") {{
            payload.selectionStart = element.selectionStart;
            payload.selectionEnd = element.selectionEnd;
          }}
        }}
        try {{
          window.sessionStorage.setItem(focusStorageKey(), JSON.stringify(payload));
        }} catch (_error) {{
          // Keep refresh behavior intact if sessionStorage is unavailable.
        }}
      }}
      function restoreFocusedElement() {{
        try {{
          const raw = window.sessionStorage.getItem(focusStorageKey());
          if (!raw) return;
          const payload = JSON.parse(raw);
          const element = document.querySelector(payload.selector || "");
          if (!element) return;
          if (isEditable(element) && element.type !== "password" && payload.value !== undefined) {{
            element.value = payload.value;
          }}
          window.requestAnimationFrame(() => {{
            element.focus({{ preventScroll: true }});
            if (
              isEditable(element)
              && typeof payload.selectionStart === "number"
              && typeof element.setSelectionRange === "function"
            ) {{
              element.setSelectionRange(payload.selectionStart, payload.selectionEnd ?? payload.selectionStart);
            }}
          }});
        }} catch (_error) {{
          // Keep refresh behavior intact if sessionStorage is unavailable.
        }}
      }}
      function savePageState(tabName = activeTabName()) {{
        saveScrollPosition(tabName);
        saveActiveTab(tabName);
        saveFocusedElement();
      }}
      function setTab(name, updateHash = true, remember = true) {{
        if (remember) {{
          saveScrollPosition();
          saveFocusedElement();
        }}
        const next = normalizedTab(name);
        tabs.forEach((button) => {{
          const active = button.dataset.hip4Tab === next;
          button.classList.toggle("is-active", active);
          button.setAttribute("aria-selected", active ? "true" : "false");
        }});
        panels.forEach((panel) => {{
          const active = panel.dataset.hip4Panel === next;
          panel.classList.toggle("is-active", active);
          panel.hidden = !active;
        }});
        if (updateHash) {{
          history.replaceState(null, "", `#${{next}}`);
        }}
        if (remember) {{
          saveActiveTab(next);
        }}
      }}
      tabs.forEach((button) => {{
        button.addEventListener("click", () => setTab(button.dataset.hip4Tab || "dashboard"));
      }});
      const hashTab = (window.location.hash || "").replace("#", "");
      const initialTab = hashTab || savedTabName() || body.dataset.defaultTab || "dashboard";
      setTab(initialTab, false, false);
      restoreScrollPosition(initialTab);
      restoreFocusedElement();
      window.addEventListener("beforeunload", () => savePageState());
      window.addEventListener("hashchange", () => {{
        const next = (window.location.hash || "").replace("#", "");
        if (valid.has(next)) {{
          setTab(next, false);
          restoreScrollPosition(next);
          restoreFocusedElement();
        }}
      }});
      if (Number.isFinite(refreshSeconds) && refreshSeconds > 0) {{
        window.setTimeout(() => {{
          savePageState();
          const target = `${{window.location.pathname}}${{window.location.search}}${{window.location.hash}}`;
          window.location.replace(target);
        }}, refreshSeconds * 1000);
      }}
    }})();
  </script>
</body>
</html>"""


def build_handler(
    supervisor: TridentSupervisor,
    metrics: MetricsRegistry,
) -> type[BaseHTTPRequestHandler]:
    auth_credentials = _ui_basic_auth_credentials()
    routing_override_enabled = _routing_override_enabled()

    class TridentHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = _request_path(self.path)
            routes: dict[str, Callable[[], dict[str, object]]] = {
                "/health": lambda: health_payload(supervisor),
                "/api/state": lambda: state_payload(supervisor, metrics),
                "/api/metrics": lambda: metrics_payload(supervisor, metrics),
                "/api/report": lambda: report_payload(supervisor, metrics),
            }
            if _is_hip4_app():
                html_routes: dict[str, Callable[[], str]] = {
                    "/": lambda: hip4_outcome_html(supervisor, metrics),
                    "/dashboard": lambda: hip4_outcome_html(supervisor, metrics),
                    "/hip4-outcome": lambda: hip4_outcome_html(supervisor, metrics),
                    "/trades": lambda: hip4_outcome_html(supervisor, metrics),
                    "/stats": lambda: hip4_outcome_html(supervisor, metrics),
                    "/system": lambda: hip4_outcome_html(supervisor, metrics),
                }
            else:
                html_routes = {
                    "/": lambda: dashboard_html(supervisor, metrics),
                    "/dashboard": lambda: dashboard_html(supervisor, metrics),
                    "/trades": lambda: trades_html(supervisor, metrics),
                    "/stats": lambda: stats_html(supervisor, metrics),
                    "/system": lambda: system_html(supervisor, metrics),
                }
            if _hip4_routes_enabled():
                routes.update(
                    {
                        "/api/hip4-outcome": lambda: hip4_outcome_payload(),
                        "/api/hip4-outcome-mainnet": lambda: hip4_outcome_mainnet_payload(),
                        "/api/hip4-nautilus-shadow": lambda: hip4_nautilus_shadow_payload(),
                    }
                )
                if not _is_hip4_app():
                    html_routes["/hip4-outcome"] = lambda: hip4_outcome_html(
                        supervisor,
                        metrics,
                    )
            if path != "/health" and not self._require_auth():
                return
            if path in html_routes:
                self._send_html(HTTPStatus.OK, html_routes[path]())
                return
            if path not in routes:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            self._send_json(HTTPStatus.OK, routes[path]())

        def do_POST(self) -> None:  # noqa: N802
            path = _request_path(self.path)
            if path != "/api/routing/override":
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                return
            if not routing_override_enabled:
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "routing_override_disabled"})
                return
            if not self._require_auth(require_configured=True):
                return
            payload = self._read_json_body()
            if not isinstance(payload, dict):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
                return
            symbol = str(payload.get("symbol", "")).strip().upper()
            if not symbol:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing_symbol"})
                return
            owner_raw = payload.get("owner")
            if owner_raw in (None, ""):
                supervisor.clear_runtime_symbol_override(symbol)
                response_owner = None
                action = "cleared"
            else:
                try:
                    owner = PodName(str(owner_raw).strip().lower())
                except ValueError:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_owner", "valid_owners": [pod.value for pod in PodName]},
                    )
                    return
                supervisor.set_runtime_symbol_override(symbol, owner)
                response_owner = owner.value
                action = "set"
            metrics.refresh_from_supervisor(supervisor)
            snapshot = supervisor.snapshot()
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "action": action,
                    "symbol": symbol,
                    "owner": response_owner,
                    "routing_overrides": snapshot.get("routing_overrides", {}),
                    "symbol_routing": next(
                        (
                            item
                            for item in snapshot.get("symbol_routing", [])
                            if isinstance(item, dict) and item.get("symbol") == symbol
                        ),
                        None,
                    ),
                },
            )

        def log_message(self, format: str, *args: object) -> None:
            return

        def _require_auth(self, *, require_configured: bool = False) -> bool:
            if auth_credentials is None:
                if require_configured:
                    self._send_json(
                        HTTPStatus.FORBIDDEN,
                        {"error": "authentication_not_configured"},
                    )
                    return False
                return True
            if _basic_auth_matches(self.headers.get("Authorization", ""), auth_credentials):
                return True
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {"error": "authentication_required"},
                headers={"WWW-Authenticate": 'Basic realm="TRIDENT UI", charset="UTF-8"'},
            )
            return False

        def _read_json_body(self) -> dict[str, object] | None:
            raw_length = self.headers.get("Content-Length", "0")
            try:
                content_length = int(raw_length)
            except ValueError:
                return None
            if content_length <= 0:
                return None
            body = self.rfile.read(content_length)
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            return payload if isinstance(payload, dict) else None

        def _send_json(
            self,
            status: HTTPStatus,
            payload: dict[str, object],
            *,
            headers: dict[str, str] | None = None,
        ) -> None:
            body = json.dumps(payload).encode("utf-8")
            self._write_response(
                status,
                body,
                content_type="application/json",
                headers=headers,
            )

        def _send_html(self, status: HTTPStatus, payload: str) -> None:
            body = payload.encode("utf-8")
            self._write_response(
                status,
                body,
                content_type="text/html; charset=utf-8",
            )

        def _write_response(
            self,
            status: HTTPStatus,
            body: bytes,
            *,
            content_type: str,
            headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for header_name, header_value in (headers or {}).items():
                self.send_header(header_name, header_value)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

    return TridentHandler


def run_http_server(
    host: str,
    port: int,
    supervisor: TridentSupervisor,
    metrics: MetricsRegistry,
) -> None:
    server = ThreadingHTTPServer((host, port), build_handler(supervisor, metrics))
    try:
        server.serve_forever()
    finally:
        server.server_close()
