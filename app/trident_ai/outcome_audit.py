from __future__ import annotations

import bisect
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.backtest.snapshot_loader import open_jsonl_text, resolve_jsonl_files
from app.trident_ai.candidate_scan import CANDIDATE_HINT_FIELD
from app.trident_ai.config import TridentAIConfig, load_trident_ai_config


DEFAULT_OUTCOME_HORIZONS_MINUTES: tuple[int, ...] = (15, 30, 60, 180)


@dataclass(frozen=True, slots=True)
class TridentAICandidateOutcomeAuditResult:
    candidate_input_path: str
    market_input_path: str
    report_json_path: str
    report_md_path: str
    horizons_minutes: tuple[int, ...] = DEFAULT_OUTCOME_HORIZONS_MINUTES
    candidates_seen: int = 0
    candidates_with_any_outcome: int = 0
    missing_outcomes: int = 0
    best_horizon_minutes: int = 0
    best_horizon_avg_net_bps: float = 0.0
    suggested_min_edge_to_cost: float = 1.5
    suggested_min_net_edge_bps: float = 5.0
    symbol_counts: dict[str, int] = field(default_factory=dict)
    side_counts: dict[str, int] = field(default_factory=dict)
    horizon_stats: dict[str, dict[str, object]] = field(default_factory=dict)
    bucket_stats: dict[str, dict[str, dict[str, dict[str, object]]]] = field(default_factory=dict)
    items: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_input_path": self.candidate_input_path,
            "market_input_path": self.market_input_path,
            "report_json_path": self.report_json_path,
            "report_md_path": self.report_md_path,
            "horizons_minutes": list(self.horizons_minutes),
            "candidates_seen": self.candidates_seen,
            "candidates_with_any_outcome": self.candidates_with_any_outcome,
            "missing_outcomes": self.missing_outcomes,
            "best_horizon_minutes": self.best_horizon_minutes,
            "best_horizon_avg_net_bps": round(self.best_horizon_avg_net_bps, 6),
            "suggested_min_edge_to_cost": round(self.suggested_min_edge_to_cost, 4),
            "suggested_min_net_edge_bps": round(self.suggested_min_net_edge_bps, 4),
            "symbol_counts": dict(sorted(self.symbol_counts.items())),
            "side_counts": dict(sorted(self.side_counts.items())),
            "horizon_stats": self.horizon_stats,
            "bucket_stats": self.bucket_stats,
            "items": self.items,
        }


def run_trident_ai_candidate_outcome_audit(
    *,
    candidate_input_path: str | Path,
    market_input_path: str | Path,
    config: TridentAIConfig | None = None,
    report_json_path: str | Path | None = None,
    report_md_path: str | Path | None = None,
    horizons_minutes: tuple[int, ...] = DEFAULT_OUTCOME_HORIZONS_MINUTES,
) -> TridentAICandidateOutcomeAuditResult:
    resolved_config = config or load_trident_ai_config()
    horizons = _normalize_horizons(horizons_minutes)
    run_id = _timestamp_id(datetime.now(timezone.utc))
    output_dir = Path(resolved_config.paths.replay_output_dir)
    json_output = Path(report_json_path or output_dir / f"trident_ai_candidate_outcome_audit_{run_id}.json")
    md_output = Path(report_md_path or output_dir / f"trident_ai_candidate_outcome_audit_{run_id}.md")

    candidates = _candidate_records(candidate_input_path)
    market_index = _market_price_index(market_input_path)
    items: list[dict[str, object]] = []
    candidates_with_any_outcome = 0
    missing_outcomes = 0
    horizon_accumulators: dict[int, list[dict[str, float]]] = defaultdict(list)

    for candidate in candidates:
        item = _candidate_item(candidate)
        candidate_outcomes: list[dict[str, object]] = []
        any_outcome = False
        for horizon in horizons:
            outcome = _candidate_horizon_outcome(
                candidate,
                market_index=market_index,
                horizon_minutes=horizon,
            )
            candidate_outcomes.append(outcome)
            if outcome["available"]:
                any_outcome = True
                horizon_accumulators[horizon].append(
                    {
                        "realized_gross_bps": _number(outcome.get("realized_gross_bps")),
                        "realized_net_bps": _number(outcome.get("realized_net_bps")),
                        "edge_error_bps": _number(outcome.get("edge_error_bps")),
                    }
                )
            else:
                missing_outcomes += 1
        if any_outcome:
            candidates_with_any_outcome += 1
        item["outcomes"] = candidate_outcomes
        item["best_outcome"] = _best_outcome(candidate_outcomes)
        items.append(item)

    horizon_stats = _horizon_stats(horizons, horizon_accumulators)
    best_horizon, best_avg_net = _best_horizon(horizon_stats)
    bucket_stats = _bucket_stats(horizons, items)
    result = TridentAICandidateOutcomeAuditResult(
        candidate_input_path=str(candidate_input_path),
        market_input_path=str(market_input_path),
        report_json_path=str(json_output),
        report_md_path=str(md_output),
        horizons_minutes=horizons,
        candidates_seen=len(candidates),
        candidates_with_any_outcome=candidates_with_any_outcome,
        missing_outcomes=missing_outcomes,
        best_horizon_minutes=best_horizon,
        best_horizon_avg_net_bps=best_avg_net,
        suggested_min_edge_to_cost=_suggested_min_edge_to_cost(items, best_horizon=best_horizon),
        suggested_min_net_edge_bps=_suggested_min_net_edge_bps(items, best_horizon=best_horizon),
        symbol_counts=dict(Counter(str(candidate.get("symbol", "") or "") for candidate in candidates)),
        side_counts=dict(Counter(str(candidate.get("side", "") or "") for candidate in candidates)),
        horizon_stats=horizon_stats,
        bucket_stats=bucket_stats,
        items=items,
    )
    payload = build_candidate_outcome_audit_report_payload(
        result=result,
        generated_at=_format_timestamp(datetime.now(timezone.utc)),
    )
    _write_report_outputs(payload, json_path=json_output, md_path=md_output)
    return result


def build_candidate_outcome_audit_report_payload(
    *,
    result: TridentAICandidateOutcomeAuditResult,
    generated_at: str,
) -> dict[str, object]:
    return {
        "generated_at": generated_at,
        "kind": "trident_ai_candidate_outcome_audit",
        "result": result.to_dict(),
    }


def _candidate_records(path: str | Path) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    for row in _iter_jsonl(path):
        symbols = row.get("symbols", [])
        if not isinstance(symbols, list):
            continue
        for symbol_payload in symbols:
            if not isinstance(symbol_payload, dict):
                continue
            hint = symbol_payload.get(CANDIDATE_HINT_FIELD)
            if not isinstance(hint, dict):
                continue
            candidate = dict(hint)
            candidate.setdefault("timestamp", row.get("timestamp", ""))
            candidate.setdefault("symbol", symbol_payload.get("symbol", ""))
            candidate["price"] = _number(symbol_payload.get("price", hint.get("price")))
            candidate["source_features"] = {
                "spread_bps": symbol_payload.get("spread_bps"),
                "microprice_dislocation_bps": symbol_payload.get("microprice_dislocation_bps"),
                "trade_flow_bias": symbol_payload.get("trade_flow_bias"),
                "book_imbalance": symbol_payload.get("book_imbalance"),
            }
            candidates.append(candidate)
    return candidates


def _market_price_index(path: str | Path) -> dict[str, list[tuple[datetime, str, float]]]:
    index: dict[str, list[tuple[datetime, str, float]]] = defaultdict(list)
    for row in _iter_jsonl(path):
        timestamp = _parse_timestamp(str(row.get("timestamp", "") or ""))
        if timestamp is None:
            continue
        timestamp_text = _format_timestamp(timestamp)
        symbols = row.get("symbols", [])
        if not isinstance(symbols, list):
            continue
        for symbol_payload in symbols:
            if not isinstance(symbol_payload, dict):
                continue
            symbol = str(symbol_payload.get("symbol", "") or "").strip().upper()
            price = _number(symbol_payload.get("price"))
            if not symbol or price <= 0:
                continue
            index[symbol].append((timestamp, timestamp_text, price))
    return {symbol: sorted(points, key=lambda point: point[0]) for symbol, points in index.items()}


def _candidate_item(candidate: dict[str, object]) -> dict[str, object]:
    return {
        "timestamp": str(candidate.get("timestamp", "") or ""),
        "symbol": str(candidate.get("symbol", "") or ""),
        "context_id": str(candidate.get("context_id", "") or ""),
        "side": str(candidate.get("side", "") or ""),
        "entry_price": _number(candidate.get("price")),
        "score": _number(candidate.get("score")),
        "estimated_edge_bps": _number(candidate.get("estimated_edge_bps")),
        "round_trip_cost_bps": _number(candidate.get("round_trip_cost_bps")),
        "estimated_net_edge_bps": _number(candidate.get("estimated_net_edge_bps")),
        "edge_to_cost_ratio": _number(candidate.get("edge_to_cost_ratio")),
        "reasons": _string_list(candidate.get("reasons")),
        "source_features": dict(candidate.get("source_features"))
        if isinstance(candidate.get("source_features"), dict)
        else {},
    }


def _candidate_horizon_outcome(
    candidate: dict[str, object],
    *,
    market_index: dict[str, list[tuple[datetime, str, float]]],
    horizon_minutes: int,
) -> dict[str, object]:
    timestamp = _parse_timestamp(str(candidate.get("timestamp", "") or ""))
    symbol = str(candidate.get("symbol", "") or "").strip().upper()
    side = str(candidate.get("side", "") or "").strip().lower()
    entry_price = _number(candidate.get("price"))
    round_trip_cost_bps = _number(candidate.get("round_trip_cost_bps"))
    estimated_edge_bps = _number(candidate.get("estimated_edge_bps"))
    if timestamp is None or entry_price <= 0 or symbol not in market_index:
        return {
            "horizon_minutes": horizon_minutes,
            "available": False,
            "reason": "missing_entry_or_market",
        }
    target = timestamp + timedelta(minutes=horizon_minutes)
    points = market_index[symbol]
    index = bisect.bisect_left([point[0] for point in points], target)
    if index >= len(points):
        return {
            "horizon_minutes": horizon_minutes,
            "available": False,
            "reason": "missing_future_price",
        }
    future_timestamp, future_timestamp_text, future_price = points[index]
    gross_bps = _gross_move_bps(
        side=side,
        entry_price=entry_price,
        future_price=future_price,
    )
    net_bps = gross_bps - round_trip_cost_bps
    return {
        "horizon_minutes": horizon_minutes,
        "available": True,
        "target_timestamp": _format_timestamp(target),
        "future_timestamp": future_timestamp_text,
        "future_lag_seconds": int((future_timestamp - target).total_seconds()),
        "future_price": round(future_price, 8),
        "realized_gross_bps": round(gross_bps, 6),
        "realized_net_bps": round(net_bps, 6),
        "edge_error_bps": round(net_bps - estimated_edge_bps, 6),
    }


def _gross_move_bps(
    *,
    side: str,
    entry_price: float,
    future_price: float,
) -> float:
    if entry_price <= 0 or future_price <= 0:
        return 0.0
    if side == "short":
        return (entry_price - future_price) / entry_price * 10_000.0
    return (future_price - entry_price) / entry_price * 10_000.0


def _best_outcome(outcomes: list[dict[str, object]]) -> dict[str, object]:
    available = [outcome for outcome in outcomes if bool(outcome.get("available", False))]
    if not available:
        return {}
    return max(available, key=lambda item: _number(item.get("realized_net_bps")))


def _horizon_stats(
    horizons: tuple[int, ...],
    accumulators: dict[int, list[dict[str, float]]],
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for horizon in horizons:
        rows = accumulators.get(horizon, [])
        wins = [row for row in rows if row["realized_net_bps"] > 0]
        result[str(horizon)] = {
            "samples": len(rows),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(rows), 6) if rows else 0.0,
            "avg_gross_bps": round(_average(rows, "realized_gross_bps"), 6),
            "avg_net_bps": round(_average(rows, "realized_net_bps"), 6),
            "avg_edge_error_bps": round(_average(rows, "edge_error_bps"), 6),
            "median_net_bps": round(_median([row["realized_net_bps"] for row in rows]), 6),
        }
    return result


def _bucket_stats(
    horizons: tuple[int, ...],
    items: list[dict[str, object]],
) -> dict[str, dict[str, dict[str, dict[str, object]]]]:
    dimensions = ("symbol", "side", "edge_to_cost", "net_edge", "score", "microprice")
    result: dict[str, dict[str, dict[str, dict[str, object]]]] = {}
    for horizon in horizons:
        horizon_key = str(horizon)
        horizon_result: dict[str, dict[str, dict[str, object]]] = {}
        for dimension in dimensions:
            rows_by_bucket: dict[str, list[dict[str, float]]] = defaultdict(list)
            for item in items:
                outcome = _outcome_for_horizon(item, horizon)
                if not outcome:
                    continue
                bucket = _bucket_label(dimension, item)
                rows_by_bucket[bucket].append(
                    {
                        "realized_net_bps": _number(outcome.get("realized_net_bps")),
                        "edge_error_bps": _number(outcome.get("edge_error_bps")),
                    }
                )
            horizon_result[dimension] = {
                bucket: _bucket_summary(rows)
                for bucket, rows in sorted(rows_by_bucket.items(), key=lambda pair: pair[0])
            }
        result[horizon_key] = horizon_result
    return result


def _bucket_summary(rows: list[dict[str, float]]) -> dict[str, object]:
    wins = [row for row in rows if row["realized_net_bps"] > 0]
    return {
        "samples": len(rows),
        "wins": len(wins),
        "win_rate": round(len(wins) / len(rows), 6) if rows else 0.0,
        "avg_net_bps": round(_average(rows, "realized_net_bps"), 6),
        "median_net_bps": round(_median([row["realized_net_bps"] for row in rows]), 6),
        "avg_edge_error_bps": round(_average(rows, "edge_error_bps"), 6),
    }


def _bucket_label(dimension: str, item: dict[str, object]) -> str:
    if dimension == "symbol":
        return str(item.get("symbol", "") or "unknown")
    if dimension == "side":
        return str(item.get("side", "") or "unknown")
    if dimension == "edge_to_cost":
        return _range_bucket(
            _number(item.get("edge_to_cost_ratio")),
            (
                (1.5, "<1.5"),
                (2.0, "1.5-2.0"),
                (2.5, "2.0-2.5"),
                (3.0, "2.5-3.0"),
            ),
            fallback=">=3.0",
        )
    if dimension == "net_edge":
        return _range_bucket(
            _number(item.get("estimated_net_edge_bps")),
            (
                (5.0, "<5"),
                (10.0, "5-10"),
                (15.0, "10-15"),
                (20.0, "15-20"),
            ),
            fallback=">=20",
        )
    if dimension == "score":
        return _range_bucket(
            _number(item.get("score")),
            (
                (1.5, "<1.5"),
                (2.0, "1.5-2.0"),
                (3.0, "2.0-3.0"),
            ),
            fallback=">=3.0",
        )
    if dimension == "microprice":
        return _microprice_bucket(item)
    return "unknown"


def _range_bucket(
    value: float,
    thresholds: tuple[tuple[float, str], ...],
    *,
    fallback: str,
) -> str:
    for threshold, label in thresholds:
        if value < threshold:
            return label
    return fallback


def _microprice_bucket(item: dict[str, object]) -> str:
    features = item.get("source_features", {})
    if not isinstance(features, dict):
        return "unknown"
    dislocation = _number(features.get("microprice_dislocation_bps"))
    if abs(dislocation) < 0.25:
        return "neutral"
    side = str(item.get("side", "") or "").strip().lower()
    if side == "long":
        return "aligned" if dislocation > 0 else "conflict"
    if side == "short":
        return "aligned" if dislocation < 0 else "conflict"
    return "unknown"


def _best_horizon(horizon_stats: dict[str, dict[str, object]]) -> tuple[int, float]:
    best_horizon = 0
    best_avg_net = 0.0
    for horizon, stats in horizon_stats.items():
        avg_net = _number(stats.get("avg_net_bps"))
        samples = int(_number(stats.get("samples")))
        if samples <= 0:
            continue
        if best_horizon == 0 or avg_net > best_avg_net:
            best_horizon = int(horizon)
            best_avg_net = avg_net
    return best_horizon, best_avg_net


def _suggested_min_edge_to_cost(items: list[dict[str, object]], *, best_horizon: int) -> float:
    if best_horizon <= 0:
        return 1.5
    losers: list[float] = []
    for item in items:
        outcome = _outcome_for_horizon(item, best_horizon)
        if outcome and _number(outcome.get("realized_net_bps")) <= 0:
            losers.append(_number(item.get("edge_to_cost_ratio")))
    if not losers:
        return 1.5
    return max(1.5, round(_percentile(losers, 0.75) + 0.1, 4))


def _suggested_min_net_edge_bps(items: list[dict[str, object]], *, best_horizon: int) -> float:
    if best_horizon <= 0:
        return 5.0
    losers: list[float] = []
    for item in items:
        outcome = _outcome_for_horizon(item, best_horizon)
        if outcome and _number(outcome.get("realized_net_bps")) <= 0:
            losers.append(_number(item.get("estimated_net_edge_bps")))
    if not losers:
        return 5.0
    return max(5.0, round(_percentile(losers, 0.75) + 2.0, 4))


def _outcome_for_horizon(item: dict[str, object], horizon: int) -> dict[str, object]:
    outcomes = item.get("outcomes", [])
    if not isinstance(outcomes, list):
        return {}
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            continue
        if int(_number(outcome.get("horizon_minutes"))) == horizon and bool(outcome.get("available")):
            return outcome
    return {}


def _average(rows: list[dict[str, float]], key: str) -> float:
    if not rows:
        return 0.0
    return sum(row[key] for row in rows) / len(rows)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percentile))))
    return ordered[index]


def _write_report_outputs(
    payload: dict[str, object],
    *,
    json_path: Path,
    md_path: Path,
) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(_render_markdown_report(payload), encoding="utf-8")


def _render_markdown_report(payload: dict[str, object]) -> str:
    result = payload["result"]
    assert isinstance(result, dict)
    lines = [
        "# TRIDENT-AI Candidate Outcome Audit",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Candidate input: `{result['candidate_input_path']}`",
        f"- Market input: `{result['market_input_path']}`",
        f"- Horizons minutes: `{result['horizons_minutes']}`",
        f"- Candidates seen: `{result['candidates_seen']}`",
        f"- Candidates with any outcome: `{result['candidates_with_any_outcome']}`",
        f"- Missing outcomes: `{result['missing_outcomes']}`",
        f"- Best horizon: `{result['best_horizon_minutes']}m`",
        f"- Best horizon avg net: `{result['best_horizon_avg_net_bps']:.4f} bps`",
        f"- Suggested min edge/cost: `{result['suggested_min_edge_to_cost']:.4f}`",
        f"- Suggested min net edge: `{result['suggested_min_net_edge_bps']:.4f} bps`",
        "",
        "## Horizon Stats",
        "",
        "| Horizon | Samples | Win Rate | Avg Gross | Avg Net | Median Net | Avg Error |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    horizon_stats = result["horizon_stats"]
    assert isinstance(horizon_stats, dict)
    for horizon, stats in horizon_stats.items():
        assert isinstance(stats, dict)
        lines.append(
            f"| {horizon}m | {stats['samples']} | {stats['win_rate']:.2%} | "
            f"{stats['avg_gross_bps']:.2f} | {stats['avg_net_bps']:.2f} | "
            f"{stats['median_net_bps']:.2f} | {stats['avg_edge_error_bps']:.2f} |"
        )

    bucket_stats = result.get("bucket_stats", {})
    best_horizon_key = str(result.get("best_horizon_minutes", ""))
    if isinstance(bucket_stats, dict) and best_horizon_key in bucket_stats:
        best_horizon_buckets = bucket_stats[best_horizon_key]
        assert isinstance(best_horizon_buckets, dict)
        lines.extend(
            [
                "",
                f"## Bucket Stats Best Horizon {best_horizon_key}m",
                "",
            ]
        )
        for dimension in ("symbol", "side", "edge_to_cost", "net_edge", "score", "microprice"):
            dimension_rows = best_horizon_buckets.get(dimension, {})
            if not isinstance(dimension_rows, dict):
                continue
            lines.extend(
                [
                    f"### {dimension}",
                    "",
                    "| Bucket | Samples | Win Rate | Avg Net | Median Net | Avg Error |",
                    "|---|---:|---:|---:|---:|---:|",
                ]
            )
            for bucket, stats in dimension_rows.items():
                assert isinstance(stats, dict)
                lines.append(
                    f"| {bucket} | {stats['samples']} | {stats['win_rate']:.2%} | "
                    f"{stats['avg_net_bps']:.2f} | {stats['median_net_bps']:.2f} | "
                    f"{stats['avg_edge_error_bps']:.2f} |"
                )
            if not dimension_rows:
                lines.append("| none | 0 | 0.00% | 0.00 | 0.00 | 0.00 |")
            lines.append("")

    lines.extend(
        [
            "",
            "## Candidates",
            "",
            "| Symbol | Time | Side | Score | Est Net | Edge/Cost | Best Horizon | Best Net |",
            "|---|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    items = result["items"]
    assert isinstance(items, list)
    for item in items[:80]:
        assert isinstance(item, dict)
        best = item.get("best_outcome")
        best_outcome = best if isinstance(best, dict) else {}
        lines.append(
            f"| {item.get('symbol', '')} | {item.get('timestamp', '')} | "
            f"{item.get('side', '')} | {_number(item.get('score')):.4f} | "
            f"{_number(item.get('estimated_net_edge_bps')):.2f} | "
            f"{_number(item.get('edge_to_cost_ratio')):.2f} | "
            f"{int(_number(best_outcome.get('horizon_minutes')))}m | "
            f"{_number(best_outcome.get('realized_net_bps')):.2f} |"
        )
    if not items:
        lines.append("| none | n/a | n/a | 0.0000 | 0.00 | 0.00 | 0m | 0.00 |")
    lines.append("")
    return "\n".join(lines)


def _iter_jsonl(path: str | Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for file_path in resolve_jsonl_files(path):
        with open_jsonl_text(file_path) as handle:
            for line in handle:
                if not line.strip():
                    continue
                payload = json.loads(line)
                if isinstance(payload, dict):
                    rows.append(payload)
    return rows


def _normalize_horizons(horizons: tuple[int, ...]) -> tuple[int, ...]:
    values = sorted({int(value) for value in horizons if int(value) > 0})
    if not values:
        raise ValueError("horizons_minutes_must_be_positive")
    return tuple(values)


def _parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _timestamp_id(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)
