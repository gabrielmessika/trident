#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import shutil
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.backtest.full_bot_replay import FullBotBacktestRunner
from app.settings import load_config


DEFAULT_START = "2026-05-24T00:00:00Z"
DEFAULT_END = "2026-06-12T00:00:00Z"

CONFIG_ERAS = (
    {
        "id": "era_1_stop_immediate_bug",
        "label": "24-05 -> 27-05 17:01Z: SL exchange immediat",
        "start": "2026-05-24T00:00:00Z",
        "end": "2026-05-27T17:01:00Z",
    },
    {
        "id": "era_2_stop_grace_165_cat_300",
        "label": "29-05 -> 08-06: grace 165 min + cat stop 300 bps",
        "start": "2026-05-29T00:00:00Z",
        "end": "2026-06-09T00:00:00Z",
    },
    {
        "id": "era_3_quality_sizing_efe",
        "label": "09-06 -> 11-06: grace 60/120, cat stop plafonne, EFE",
        "start": "2026-06-09T00:00:00Z",
        "end": DEFAULT_END,
    },
)


@dataclass(slots=True)
class TradeRow:
    pod: str
    symbol: str
    side: str
    setup: str | None
    close_reason: str | None
    opened_at: str | None
    closed_at: str | None
    target_notional_usd: float
    pnl_usd: float
    gross_pnl_usd: float
    fees_usd: float
    funding_usd: float = 0.0
    source: str = "unknown"


@dataclass(slots=True)
class SlippageStats:
    count: int = 0
    avg_bps: float | None = None
    median_bps: float | None = None
    p75_bps: float | None = None
    max_bps: float | None = None


@dataclass(slots=True)
class P101Artifacts:
    output_dir: Path
    input_dir: Path
    replay_report_path: Path
    replay_summary_path: Path
    report_json_path: Path
    report_md_path: Path
    alignment_csv_path: Path


def parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def utc_stamp(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def latest_child(parent: Path) -> Path:
    candidates = sorted(path for path in parent.iterdir() if path.is_dir())
    if not candidates:
        raise FileNotFoundError(f"no directory found under {parent}")
    return candidates[-1]


def prepare_input_window(
    *,
    snapshots_dir: Path,
    output_dir: Path,
    start: datetime,
    end: datetime,
) -> tuple[Path, list[dict[str, object]]]:
    input_dir = output_dir / "input_window"
    input_dir.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, object]] = []
    for source in sorted(snapshots_dir.glob("*.jsonl")):
        date_text = source.stem
        try:
            file_date = datetime.fromisoformat(date_text).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if not (start.date() <= file_date.date() < end.date()):
            continue
        target = input_dir / source.name
        if not target.exists():
            target.symlink_to(source.resolve())
        files.append(
            {
                "name": source.name,
                "path": str(source),
                "line_count": count_lines(source),
            }
        )
    if not files:
        raise FileNotFoundError(
            f"no snapshot JSONL files in {snapshots_dir} for {start.isoformat()} -> {end.isoformat()}"
        )
    return input_dir, files


def count_lines(path: Path) -> int:
    count = 0
    with path.open("rb") as handle:
        for _ in handle:
            count += 1
    return count


def load_live_trades(path: Path, *, start: datetime, end: datetime) -> list[TradeRow]:
    trades: list[TradeRow] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            closed_at = parse_timestamp(row.get("closed_at") or row.get("event_ts"))
            if closed_at is None or closed_at < start or closed_at >= end:
                continue
            exchange_net = _float(row.get("exchange_net_pnl_usd"))
            trades.append(
                TradeRow(
                    pod=str(row.get("pod") or "").strip(),
                    symbol=str(row.get("symbol") or "").strip(),
                    side=str(row.get("side") or "").strip(),
                    setup=_none_if_blank(row.get("setup")),
                    close_reason=_none_if_blank(row.get("close_reason")),
                    opened_at=_none_if_blank(row.get("opened_at")),
                    closed_at=_none_if_blank(row.get("closed_at") or row.get("event_ts")),
                    target_notional_usd=_float(row.get("target_notional_usd")),
                    pnl_usd=exchange_net,
                    gross_pnl_usd=_float(row.get("exchange_closed_pnl_usd")),
                    fees_usd=_float(row.get("exchange_fee_usd")),
                    funding_usd=_float(row.get("funding_usd")),
                    source="exchange_backfill",
                )
            )
    return trades


def replay_trades_from_report(payload: dict[str, Any]) -> list[TradeRow]:
    trades: list[TradeRow] = []
    for pod in ("pod_a", "pod_b", "pod_c"):
        pod_payload = payload.get(pod)
        if not isinstance(pod_payload, dict):
            continue
        for row in pod_payload.get("closed_trade_log", []) or []:
            if not isinstance(row, dict):
                continue
            trades.append(
                TradeRow(
                    pod=pod,
                    symbol=str(row.get("symbol") or "").strip(),
                    side=str(row.get("side") or "").strip(),
                    setup=_none_if_blank(row.get("setup")),
                    close_reason=_none_if_blank(row.get("close_reason")),
                    opened_at=_none_if_blank(row.get("opened_at")),
                    closed_at=_none_if_blank(row.get("closed_at")),
                    target_notional_usd=_float(row.get("target_notional_usd")),
                    pnl_usd=_float(row.get("pnl_usd")),
                    gross_pnl_usd=_float(row.get("gross_pnl_usd")),
                    fees_usd=_float(row.get("fees_usd")),
                    source="full_bot_replay",
                )
            )
    return trades


def summarize_trades(trades: Iterable[TradeRow]) -> dict[str, Any]:
    rows = list(trades)
    wins = sum(1 for row in rows if row.pnl_usd >= 0)
    losses = sum(1 for row in rows if row.pnl_usd < 0)
    gross_positive = sum(row.pnl_usd for row in rows if row.pnl_usd > 0)
    gross_negative = abs(sum(row.pnl_usd for row in rows if row.pnl_usd < 0))
    return {
        "closed_trade_count": len(rows),
        "win_count": wins,
        "loss_count": losses,
        "win_rate": round(wins / len(rows), 4) if rows else None,
        "pnl_usd": round(sum(row.pnl_usd for row in rows), 6),
        "gross_pnl_usd": round(sum(row.gross_pnl_usd for row in rows), 6),
        "fees_usd": round(sum(row.fees_usd for row in rows), 6),
        "funding_usd": round(sum(row.funding_usd for row in rows), 6),
        "profit_factor": (
            round(gross_positive / gross_negative, 4) if gross_negative > 0 else None
        ),
        "avg_notional_usd": (
            round(sum(row.target_notional_usd for row in rows) / len(rows), 4) if rows else 0.0
        ),
    }


def summarize_by_pod(trades: Iterable[TradeRow]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[TradeRow]] = {}
    for row in trades:
        grouped.setdefault(row.pod, []).append(row)
    return {pod: summarize_trades(rows) for pod, rows in sorted(grouped.items())}


def summarize_by_era(trades: Iterable[TradeRow]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[TradeRow]] = {str(era["id"]): [] for era in CONFIG_ERAS}
    grouped["outside_defined_eras"] = []
    for row in trades:
        closed_at = parse_timestamp(row.closed_at)
        era_id = era_for(closed_at)
        grouped.setdefault(era_id, []).append(row)
    return {
        era_id: {
            **summarize_trades(rows),
            "label": _era_label(era_id),
        }
        for era_id, rows in grouped.items()
        if rows or era_id != "outside_defined_eras"
    }


def era_for(timestamp: datetime | None) -> str:
    if timestamp is None:
        return "outside_defined_eras"
    for era in CONFIG_ERAS:
        start = parse_timestamp(str(era["start"]))
        end = parse_timestamp(str(era["end"]))
        if start is not None and end is not None and start <= timestamp < end:
            return str(era["id"])
    return "outside_defined_eras"


def _era_label(era_id: str) -> str:
    for era in CONFIG_ERAS:
        if era["id"] == era_id:
            return str(era["label"])
    return "Hors eres configurees"


def load_slippage_stats(path: Path, *, start: datetime, end: datetime) -> dict[str, Any]:
    values: dict[tuple[str, str, str], list[float]] = {}
    global_values: dict[tuple[str, str], list[float]] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            action = str(row.get("action") or "").strip().lower()
            if action not in {"open", "close"}:
                continue
            ts = parse_timestamp(row.get("fill_ts") or row.get("event_ts"))
            if ts is None or ts < start or ts >= end:
                continue
            bps = _optional_float(row.get("slippage_bps"))
            if bps is None:
                continue
            pod = str(row.get("pod") or "").strip()
            symbol = str(row.get("symbol") or "").strip()
            values.setdefault((pod, symbol, action), []).append(bps)
            global_values.setdefault((pod, action), []).append(bps)
    return {
        "by_pod_symbol_action": {
            "|".join(key): asdict(_stats(items)) for key, items in sorted(values.items())
        },
        "by_pod_action": {
            "|".join(key): asdict(_stats(items)) for key, items in sorted(global_values.items())
        },
    }


def build_cost_matrix(
    trades: list[TradeRow],
    *,
    slippage_stats: dict[str, Any],
) -> dict[str, Any]:
    scenarios = {
        "constant_8bps": {"kind": "constant", "open_bps": 8.0, "close_bps": 8.0},
        "constant_12bps": {"kind": "constant", "open_bps": 12.0, "close_bps": 12.0},
        "live_config_8_12bps": {"kind": "constant", "open_bps": 8.0, "close_bps": 12.0},
        "observed_by_symbol": {"kind": "observed_by_symbol"},
    }
    by_pod_symbol_action = slippage_stats.get("by_pod_symbol_action", {})
    by_pod_action = slippage_stats.get("by_pod_action", {})
    matrix: dict[str, Any] = {}
    for name, scenario in scenarios.items():
        rows: dict[str, dict[str, float]] = {}
        for trade in trades:
            pod_bucket = rows.setdefault(
                trade.pod,
                {
                    "closed_trade_count": 0.0,
                    "base_replay_pnl_usd": 0.0,
                    "slippage_cost_usd": 0.0,
                    "net_after_cost_overlay_usd": 0.0,
                    "notional_usd": 0.0,
                },
            )
            open_bps, close_bps = _scenario_slippage_bps(
                scenario=scenario,
                trade=trade,
                by_pod_symbol_action=by_pod_symbol_action,
                by_pod_action=by_pod_action,
            )
            cost = trade.target_notional_usd * (open_bps + close_bps) / 10_000.0
            pod_bucket["closed_trade_count"] += 1
            pod_bucket["base_replay_pnl_usd"] += trade.pnl_usd
            pod_bucket["slippage_cost_usd"] += cost
            pod_bucket["net_after_cost_overlay_usd"] += trade.pnl_usd - cost
            pod_bucket["notional_usd"] += trade.target_notional_usd
        matrix[name] = {
            pod: {key: round(value, 6) for key, value in sorted(payload.items())}
            for pod, payload in sorted(rows.items())
        }
        totals = {
            "closed_trade_count": sum(row["closed_trade_count"] for row in rows.values()),
            "base_replay_pnl_usd": sum(row["base_replay_pnl_usd"] for row in rows.values()),
            "slippage_cost_usd": sum(row["slippage_cost_usd"] for row in rows.values()),
            "net_after_cost_overlay_usd": sum(
                row["net_after_cost_overlay_usd"] for row in rows.values()
            ),
            "notional_usd": sum(row["notional_usd"] for row in rows.values()),
        }
        matrix[name]["total"] = {key: round(value, 6) for key, value in sorted(totals.items())}
    return matrix


def _scenario_slippage_bps(
    *,
    scenario: dict[str, object],
    trade: TradeRow,
    by_pod_symbol_action: dict[str, dict[str, object]],
    by_pod_action: dict[str, dict[str, object]],
) -> tuple[float, float]:
    if scenario.get("kind") == "constant":
        return float(scenario["open_bps"]), float(scenario["close_bps"])
    return (
        _observed_bps(trade, "open", by_pod_symbol_action, by_pod_action),
        _observed_bps(trade, "close", by_pod_symbol_action, by_pod_action),
    )


def _observed_bps(
    trade: TradeRow,
    action: str,
    by_pod_symbol_action: dict[str, dict[str, object]],
    by_pod_action: dict[str, dict[str, object]],
) -> float:
    symbol_key = f"{trade.pod}|{trade.symbol}|{action}"
    pod_key = f"{trade.pod}|{action}"
    stats = by_pod_symbol_action.get(symbol_key) or by_pod_action.get(pod_key) or {}
    value = stats.get("avg_bps")
    if value is None:
        return 0.0
    return float(value)


def trade_alignment(
    *,
    live_trades: list[TradeRow],
    replay_trades: list[TradeRow],
    max_open_delta_minutes: float,
) -> tuple[dict[str, Any], list[dict[str, object]]]:
    live_used: set[int] = set()
    rows: list[dict[str, object]] = []
    matched = 0
    pnl_diffs: list[float] = []
    open_deltas: list[float] = []
    close_deltas: list[float] = []

    for replay_index, replay in enumerate(replay_trades):
        candidate_index, open_delta = _best_live_match(
            replay=replay,
            live_trades=live_trades,
            live_used=live_used,
            max_open_delta_minutes=max_open_delta_minutes,
        )
        if candidate_index is None:
            rows.append(_alignment_row("replay_unmatched", None, replay, None, None))
            continue
        live_used.add(candidate_index)
        live = live_trades[candidate_index]
        close_delta = _minutes_between(live.closed_at, replay.closed_at)
        pnl_diff = replay.pnl_usd - live.pnl_usd
        matched += 1
        pnl_diffs.append(pnl_diff)
        if open_delta is not None:
            open_deltas.append(open_delta)
        if close_delta is not None:
            close_deltas.append(close_delta)
        rows.append(_alignment_row("matched", live, replay, open_delta, close_delta, pnl_diff))

    for index, live in enumerate(live_trades):
        if index in live_used:
            continue
        rows.append(_alignment_row("live_unmatched", live, None, None, None))

    summary = {
        "live_trade_count": len(live_trades),
        "replay_trade_count": len(replay_trades),
        "matched_trade_count": matched,
        "live_unmatched_count": len(live_trades) - len(live_used),
        "replay_unmatched_count": len(replay_trades) - matched,
        "match_rate_vs_live": round(matched / len(live_trades), 4) if live_trades else None,
        "match_rate_vs_replay": round(matched / len(replay_trades), 4) if replay_trades else None,
        "avg_open_delta_minutes": _avg(open_deltas),
        "avg_close_delta_minutes": _avg(close_deltas),
        "avg_pnl_diff_usd": _avg(pnl_diffs),
        "max_abs_pnl_diff_usd": (
            round(max(abs(value) for value in pnl_diffs), 6) if pnl_diffs else None
        ),
        "max_open_delta_minutes": max_open_delta_minutes,
    }
    return summary, rows


def write_alignment_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "status",
        "pod",
        "symbol",
        "side",
        "live_opened_at",
        "replay_opened_at",
        "live_closed_at",
        "replay_closed_at",
        "live_pnl_usd",
        "replay_pnl_usd",
        "pnl_diff_usd",
        "open_delta_minutes",
        "close_delta_minutes",
        "live_close_reason",
        "replay_close_reason",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _best_live_match(
    *,
    replay: TradeRow,
    live_trades: list[TradeRow],
    live_used: set[int],
    max_open_delta_minutes: float,
) -> tuple[int | None, float | None]:
    best_index: int | None = None
    best_delta: float | None = None
    for index, live in enumerate(live_trades):
        if index in live_used:
            continue
        if (live.pod, live.symbol, live.side) != (replay.pod, replay.symbol, replay.side):
            continue
        delta = _minutes_between(live.opened_at, replay.opened_at)
        if delta is None or delta > max_open_delta_minutes:
            continue
        if best_delta is None or delta < best_delta:
            best_index = index
            best_delta = delta
    return best_index, best_delta


def _alignment_row(
    status: str,
    live: TradeRow | None,
    replay: TradeRow | None,
    open_delta: float | None,
    close_delta: float | None,
    pnl_diff: float | None = None,
) -> dict[str, object]:
    ref = live or replay
    return {
        "status": status,
        "pod": ref.pod if ref else "",
        "symbol": ref.symbol if ref else "",
        "side": ref.side if ref else "",
        "live_opened_at": live.opened_at if live else "",
        "replay_opened_at": replay.opened_at if replay else "",
        "live_closed_at": live.closed_at if live else "",
        "replay_closed_at": replay.closed_at if replay else "",
        "live_pnl_usd": round(live.pnl_usd, 6) if live else "",
        "replay_pnl_usd": round(replay.pnl_usd, 6) if replay else "",
        "pnl_diff_usd": round(pnl_diff, 6) if pnl_diff is not None else "",
        "open_delta_minutes": round(open_delta, 4) if open_delta is not None else "",
        "close_delta_minutes": round(close_delta, 4) if close_delta is not None else "",
        "live_close_reason": live.close_reason if live else "",
        "replay_close_reason": replay.close_reason if replay else "",
    }


def build_report_payload(
    *,
    start: datetime,
    end: datetime,
    input_files: list[dict[str, object]],
    replay_report_path: Path,
    replay_payload: dict[str, Any],
    live_trades: list[TradeRow],
    replay_trades: list[TradeRow],
    fill_events_path: Path,
    backfill_path: Path,
    max_open_delta_minutes: float,
    apply_live_notional_caps: bool,
) -> tuple[dict[str, Any], list[dict[str, object]]]:
    slippage_stats = load_slippage_stats(fill_events_path, start=start, end=end)
    alignment_summary, alignment_rows = trade_alignment(
        live_trades=live_trades,
        replay_trades=replay_trades,
        max_open_delta_minutes=max_open_delta_minutes,
    )
    live_summary = {
        "total": summarize_trades(live_trades),
        "by_pod": summarize_by_pod(live_trades),
        "by_era": summarize_by_era(live_trades),
    }
    replay_summary = {
        "total": summarize_trades(replay_trades),
        "by_pod": summarize_by_pod(replay_trades),
        "by_era": summarize_by_era(replay_trades),
    }
    live_total = live_summary["total"]["pnl_usd"]
    replay_total = replay_summary["total"]["pnl_usd"]
    return (
        {
            "kind": "p101_recent_full_bot_replay",
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "window": {
                "start": start.isoformat().replace("+00:00", "Z"),
                "end_exclusive": end.isoformat().replace("+00:00", "Z"),
                "label": "2026-05-24 -> 2026-06-11 inclus",
            },
            "inputs": {
                "snapshot_files": input_files,
                "snapshot_line_count": sum(int(item["line_count"]) for item in input_files),
                "replay_report": str(replay_report_path),
                "exchange_backfill_closed_trades": str(backfill_path),
                "fill_events": str(fill_events_path),
                "apply_live_notional_caps": apply_live_notional_caps,
            },
            "raw_replay_metrics": {
                "records_processed": replay_payload.get("records_processed"),
                "duplicate_timestamps_skipped": replay_payload.get(
                    "duplicate_timestamps_skipped"
                ),
                "first_timestamp": replay_payload.get("first_timestamp"),
                "last_timestamp": replay_payload.get("last_timestamp"),
                "dates_covered": replay_payload.get("dates_covered"),
                "total_realized_pnl_usd": replay_payload.get("total_realized_pnl_usd"),
                "directional_fees_usd": replay_payload.get("directional_fees_usd"),
                "total_activity_count": replay_payload.get("total_activity_count"),
            },
            "live_exchange": live_summary,
            "replay_current_config": replay_summary,
            "delta_replay_minus_live_usd": round(replay_total - live_total, 6),
            "trade_alignment": alignment_summary,
            "slippage_observed": slippage_stats,
            "cost_sensitivity_overlay": build_cost_matrix(
                replay_trades,
                slippage_stats=slippage_stats,
            ),
            "known_limits": [
                "Snapshots recents sans external_reference_* Pod C: replay annote no_external_reference.",
                "Le replay P1-01 peut appliquer le cap live A/C; verifier inputs.apply_live_notional_caps.",
                "La matrice de cout est un overlay post-replay, pas un rerun engine par scenario.",
                "La comparaison trade-by-trade matche par pod/symbol/side/open_time; les trades non identiques restent listes dans le CSV.",
            ],
            "decision_note": (
                "Ce rapport est diagnostic/read-only. Aucun changement de sizing, stops, routing "
                "ou ordre live n'est applique par P1-01."
            ),
        },
        alignment_rows,
    )


def render_markdown(payload: dict[str, Any], *, alignment_csv_path: Path) -> str:
    live_total = payload["live_exchange"]["total"]
    replay_total = payload["replay_current_config"]["total"]
    alignment = payload["trade_alignment"]
    cost = payload["cost_sensitivity_overlay"]
    by_pod_live = payload["live_exchange"]["by_pod"]
    by_pod_replay = payload["replay_current_config"]["by_pod"]
    lines = [
        "# P1-01 - Replay full-bot fenetre live recente\n",
        f"- Genere le: `{payload['generated_at']}`",
        f"- Fenetre: `{payload['window']['label']}`",
        f"- Snapshots: `{payload['inputs']['snapshot_line_count']}` lignes / `{len(payload['inputs']['snapshot_files'])}` fichiers",
        f"- Replay brut: `{payload['inputs']['replay_report']}`",
        f"- Alignement trade-by-trade: `{alignment_csv_path}`",
        "",
        "## Synthese",
        "",
        "| Source | Trades | PnL net USD | Fees USD | Funding USD | WR | PF |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        _summary_row("Live exchange P0-03", live_total),
        _summary_row("Replay config courante", replay_total),
        "",
        f"- Delta replay - live: `{payload['delta_replay_minus_live_usd']}` USD",
        f"- Match trade-by-trade: `{alignment['matched_trade_count']}` matches, "
        f"`{alignment['live_unmatched_count']}` live non matches, "
        f"`{alignment['replay_unmatched_count']}` replay non matches.",
        "",
        "## Par pod",
        "",
        "| Pod | Live trades | Live PnL | Replay trades | Replay PnL | Delta |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    pods = sorted(set(by_pod_live) | set(by_pod_replay))
    for pod in pods:
        live = by_pod_live.get(pod, {})
        replay = by_pod_replay.get(pod, {})
        live_pnl = float(live.get("pnl_usd", 0.0) or 0.0)
        replay_pnl = float(replay.get("pnl_usd", 0.0) or 0.0)
        lines.append(
            "| {pod} | {live_count} | {live_pnl:.2f} | {replay_count} | {replay_pnl:.2f} | {delta:.2f} |".format(
                pod=pod,
                live_count=int(live.get("closed_trade_count", 0) or 0),
                live_pnl=live_pnl,
                replay_count=int(replay.get("closed_trade_count", 0) or 0),
                replay_pnl=replay_pnl,
                delta=replay_pnl - live_pnl,
            )
        )
    lines.extend(
        [
            "",
            "## Eres de config",
            "",
            "| Ere | Live trades | Live PnL | Replay trades | Replay PnL |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for era_id, live_era in payload["live_exchange"]["by_era"].items():
        replay_era = payload["replay_current_config"]["by_era"].get(era_id, {})
        lines.append(
            "| {label} | {live_count} | {live_pnl:.2f} | {replay_count} | {replay_pnl:.2f} |".format(
                label=live_era.get("label", era_id),
                live_count=int(live_era.get("closed_trade_count", 0) or 0),
                live_pnl=float(live_era.get("pnl_usd", 0.0) or 0.0),
                replay_count=int(replay_era.get("closed_trade_count", 0) or 0),
                replay_pnl=float(replay_era.get("pnl_usd", 0.0) or 0.0),
            )
        )
    lines.extend(
        [
            "",
            "## Matrice couts",
            "",
            "| Scenario | Base replay PnL | Cout slippage | Net apres overlay |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for scenario, scenario_payload in cost.items():
        total = scenario_payload.get("total", {})
        lines.append(
            "| {scenario} | {base:.2f} | {cost:.2f} | {net:.2f} |".format(
                scenario=scenario,
                base=float(total.get("base_replay_pnl_usd", 0.0) or 0.0),
                cost=float(total.get("slippage_cost_usd", 0.0) or 0.0),
                net=float(total.get("net_after_cost_overlay_usd", 0.0) or 0.0),
            )
        )
    lines.extend(
        [
            "",
            "## Limites",
            "",
            *[f"- {item}" for item in payload["known_limits"]],
            "",
            payload["decision_note"],
            "",
        ]
    )
    return "\n".join(lines)


def _summary_row(label: str, summary: dict[str, object]) -> str:
    win_rate = summary.get("win_rate")
    profit_factor = summary.get("profit_factor")
    return "| {label} | {trades} | {pnl:.2f} | {fees:.2f} | {funding:.4f} | {wr} | {pf} |".format(
        label=label,
        trades=int(summary.get("closed_trade_count", 0) or 0),
        pnl=float(summary.get("pnl_usd", 0.0) or 0.0),
        fees=float(summary.get("fees_usd", 0.0) or 0.0),
        funding=float(summary.get("funding_usd", 0.0) or 0.0),
        wr=_fmt_optional(win_rate),
        pf=_fmt_optional(profit_factor),
    )


def run_p101(args: argparse.Namespace) -> P101Artifacts:
    source_root = Path(args.source_root)
    output_dir = Path(args.output_dir) if args.output_dir else (
        source_root / "replay_reports" / f"p101_recent_full_bot_{utc_stamp()}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    start = parse_timestamp(args.start)
    end = parse_timestamp(args.end)
    if start is None or end is None or start >= end:
        raise ValueError("--start and --end must define a valid UTC window")

    input_dir, input_files = prepare_input_window(
        snapshots_dir=Path(args.snapshots_dir),
        output_dir=output_dir,
        start=start,
        end=end,
    )

    replay_report_path = output_dir / "full_bot_replay_current_config.json"
    replay_summary_path = output_dir / "full_bot_replay_current_config.md"
    if args.existing_replay_report:
        existing_report = Path(args.existing_replay_report)
        replay_payload = json.loads(existing_report.read_text(encoding="utf-8"))
        if existing_report.resolve() != replay_report_path.resolve():
            shutil.copy2(existing_report, replay_report_path)
        if args.existing_replay_summary:
            existing_summary = Path(args.existing_replay_summary)
            if existing_summary.exists() and existing_summary.resolve() != replay_summary_path.resolve():
                shutil.copy2(existing_summary, replay_summary_path)
    else:
        result = FullBotBacktestRunner(
            load_config(args.config),
            force_enable_all_pods=not args.respect_config_enabled,
            apply_live_notional_caps=args.apply_live_notional_caps,
        ).run_jsonl(
            input_path=input_dir,
            report_output=replay_report_path,
            summary_output=replay_summary_path,
        )
        replay_payload = result.to_dict()

    backfill_dir = Path(args.backfill_dir) if args.backfill_dir else latest_child(
        source_root / "audit_backfills"
    )
    backfill_path = backfill_dir / "trident_ac_closed_trades_full.csv"
    if not backfill_path.exists():
        raise FileNotFoundError(backfill_path)
    fill_events_path = Path(args.fill_events)
    if not fill_events_path.exists():
        raise FileNotFoundError(fill_events_path)

    live_trades = load_live_trades(backfill_path, start=start, end=end)
    replay_trades = replay_trades_from_report(replay_payload)
    payload, alignment_rows = build_report_payload(
        start=start,
        end=end,
        input_files=input_files,
        replay_report_path=replay_report_path,
        replay_payload=replay_payload,
        live_trades=live_trades,
        replay_trades=replay_trades,
        fill_events_path=fill_events_path,
        backfill_path=backfill_path,
        max_open_delta_minutes=float(args.match_open_delta_minutes),
        apply_live_notional_caps=bool(args.apply_live_notional_caps),
    )

    report_json_path = output_dir / "p101_recent_replay_report.json"
    report_md_path = output_dir / "p101_recent_replay_report.md"
    alignment_csv_path = output_dir / "trade_alignment.csv"
    write_alignment_csv(alignment_csv_path, alignment_rows)
    report_json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    report_md_path.write_text(
        render_markdown(payload, alignment_csv_path=alignment_csv_path),
        encoding="utf-8",
    )
    return P101Artifacts(
        output_dir=output_dir,
        input_dir=input_dir,
        replay_report_path=replay_report_path,
        replay_summary_path=replay_summary_path,
        report_json_path=report_json_path,
        report_md_path=report_md_path,
        alignment_csv_path=alignment_csv_path,
    )


def _stats(values: list[float]) -> SlippageStats:
    if not values:
        return SlippageStats()
    ordered = sorted(values)
    p75_index = min(int(len(ordered) * 0.75), len(ordered) - 1)
    return SlippageStats(
        count=len(values),
        avg_bps=round(sum(values) / len(values), 4),
        median_bps=round(statistics.median(values), 4),
        p75_bps=round(ordered[p75_index], 4),
        max_bps=round(max(values), 4),
    )


def _minutes_between(left: str | None, right: str | None) -> float | None:
    left_ts = parse_timestamp(left)
    right_ts = parse_timestamp(right)
    if left_ts is None or right_ts is None:
        return None
    return abs((left_ts - right_ts).total_seconds()) / 60.0


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 6)


def _float(value: object) -> float:
    parsed = _optional_float(value)
    return parsed if parsed is not None else 0.0


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _none_if_blank(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _fmt_optional(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run P1-01 recent full-bot replay and compare it to P0-03 exchange PnL.",
    )
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--source-root", default="server-data")
    parser.add_argument("--snapshots-dir", default="server-data/live_snapshots")
    parser.add_argument("--backfill-dir")
    parser.add_argument(
        "--fill-events",
        default="server-data/audit_exports/20260612T163311Z_p003_final/trident_ac_fill_events.csv",
    )
    parser.add_argument("--output-dir")
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--existing-replay-report")
    parser.add_argument("--existing-replay-summary")
    parser.add_argument(
        "--apply-live-notional-caps",
        action="store_true",
        help="Apply live_max_order_notional_usd to Pod A/C plans before replay execution.",
    )
    parser.add_argument("--match-open-delta-minutes", type=float, default=90.0)
    parser.add_argument(
        "--respect-config-enabled",
        action="store_true",
        help="Do not force-enable Pod A / Pod C for the backtest runner.",
    )
    return parser


def main() -> None:
    artifacts = run_p101(build_parser().parse_args())
    print(f"output_dir={artifacts.output_dir}")
    print(f"input_dir={artifacts.input_dir}")
    print(f"replay_report={artifacts.replay_report_path}")
    print(f"report_json={artifacts.report_json_path}")
    print(f"report_md={artifacts.report_md_path}")
    print(f"alignment_csv={artifacts.alignment_csv_path}")


if __name__ == "__main__":
    main()
