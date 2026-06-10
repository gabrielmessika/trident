from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.trident_ai.candidate_scan import (
    CANDIDATE_HINT_FIELD,
    DEFAULT_MICROPRICE_CONFLICT_BPS,
)
from app.trident_ai.config import TridentAIConfig, load_trident_ai_config
from app.trident_ai.paper import PAPER_REPLAY_TRADE_CLOSED_EVENT
from app.trident_ai.replay import LLM_REPLAY_DECISION_EVENT


@dataclass(frozen=True, slots=True)
class TridentAIEdgeCalibrationResult:
    candidate_input_path: str
    llm_journal_path: str
    paper_journal_path: str
    report_json_path: str
    report_md_path: str
    candidates_seen: int = 0
    matched_llm_decisions: int = 0
    open_decisions: int = 0
    closed_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    false_positive_trades: int = 0
    overestimated_trades: int = 0
    underestimated_trades: int = 0
    avg_estimated_edge_bps: float = 0.0
    avg_estimated_net_edge_bps: float = 0.0
    avg_realized_net_bps: float = 0.0
    avg_edge_error_bps: float = 0.0
    avg_abs_edge_error_bps: float = 0.0
    realized_pnl_usd: float = 0.0
    suggested_min_edge_to_cost: float = 1.5
    suggested_min_net_edge_bps: float = 5.0
    sample_warning: str = ""
    close_reasons: dict[str, int] = field(default_factory=dict)
    edge_buckets: dict[str, dict[str, object]] = field(default_factory=dict)
    items: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_input_path": self.candidate_input_path,
            "llm_journal_path": self.llm_journal_path,
            "paper_journal_path": self.paper_journal_path,
            "report_json_path": self.report_json_path,
            "report_md_path": self.report_md_path,
            "candidates_seen": self.candidates_seen,
            "matched_llm_decisions": self.matched_llm_decisions,
            "open_decisions": self.open_decisions,
            "closed_trades": self.closed_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "false_positive_trades": self.false_positive_trades,
            "overestimated_trades": self.overestimated_trades,
            "underestimated_trades": self.underestimated_trades,
            "avg_estimated_edge_bps": round(self.avg_estimated_edge_bps, 6),
            "avg_estimated_net_edge_bps": round(self.avg_estimated_net_edge_bps, 6),
            "avg_realized_net_bps": round(self.avg_realized_net_bps, 6),
            "avg_edge_error_bps": round(self.avg_edge_error_bps, 6),
            "avg_abs_edge_error_bps": round(self.avg_abs_edge_error_bps, 6),
            "realized_pnl_usd": round(self.realized_pnl_usd, 6),
            "suggested_min_edge_to_cost": round(self.suggested_min_edge_to_cost, 4),
            "suggested_min_net_edge_bps": round(self.suggested_min_net_edge_bps, 4),
            "sample_warning": self.sample_warning,
            "close_reasons": dict(sorted(self.close_reasons.items())),
            "edge_buckets": self.edge_buckets,
            "items": self.items,
        }


def run_trident_ai_edge_calibration_report(
    *,
    candidate_input_path: str | Path,
    llm_journal_path: str | Path,
    paper_journal_path: str | Path,
    config: TridentAIConfig | None = None,
    report_json_path: str | Path | None = None,
    report_md_path: str | Path | None = None,
) -> TridentAIEdgeCalibrationResult:
    resolved_config = config or load_trident_ai_config()
    run_id = _timestamp_id(datetime.now(timezone.utc))
    output_dir = Path(resolved_config.paths.replay_output_dir)
    json_output = Path(report_json_path or output_dir / f"trident_ai_edge_calibration_{run_id}.json")
    md_output = Path(report_md_path or output_dir / f"trident_ai_edge_calibration_{run_id}.md")

    candidates = _candidate_records(candidate_input_path)
    llm_by_context = _llm_decisions_by_context(llm_journal_path)
    trades_by_decision = _closed_trades_by_decision(paper_journal_path)

    items: list[dict[str, object]] = []
    close_reasons: Counter[str] = Counter()
    bucket_stats: dict[str, dict[str, float | int]] = defaultdict(_empty_edge_bucket)
    trade_metrics: list[dict[str, float]] = []
    matched_llm_decisions = 0
    open_decisions = 0
    winning_trades = 0
    losing_trades = 0
    false_positive_trades = 0
    overestimated_trades = 0
    underestimated_trades = 0
    realized_pnl_usd = 0.0
    losing_edge_to_cost: list[float] = []
    losing_estimated_net_edges: list[float] = []

    for candidate in candidates:
        context_id = str(candidate.get("context_id", "") or "")
        item = _base_item(candidate)
        bucket = _edge_bucket(_number(candidate.get("edge_to_cost_ratio")))
        bucket_stats[bucket]["candidates"] += 1
        llm_record = llm_by_context.get(context_id)
        if llm_record is None:
            item["status"] = "missing_llm_decision"
            items.append(item)
            continue

        matched_llm_decisions += 1
        proposal = _mapping(llm_record.get("proposal"))
        action = str(proposal.get("action", "") or "")
        decision_id = str(proposal.get("decision_id", "") or "")
        item["llm"] = {
            "action": action,
            "confidence": _number(proposal.get("confidence")),
            "decision_id": decision_id,
        }
        if action != "open":
            item["status"] = "llm_hold"
            items.append(item)
            continue

        open_decisions += 1
        bucket_stats[bucket]["opens"] += 1
        trade = trades_by_decision.get(decision_id)
        if trade is None:
            item["status"] = "open_without_closed_trade"
            items.append(item)
            continue

        metrics = _trade_edge_metrics(candidate, trade)
        trade_metrics.append(metrics)
        pnl = metrics["pnl_usd"]
        realized_pnl_usd += pnl
        bucket_stats[bucket]["closed_trades"] += 1
        bucket_stats[bucket]["pnl_usd"] += pnl
        close_reason = str(trade.get("close_reason", "") or "")
        close_reasons[close_reason] += 1
        if pnl > 0:
            winning_trades += 1
        elif pnl < 0:
            losing_trades += 1
        if pnl <= 0 and metrics["estimated_net_edge_bps"] > 0:
            false_positive_trades += 1
            losing_edge_to_cost.append(metrics["edge_to_cost_ratio"])
            losing_estimated_net_edges.append(metrics["estimated_net_edge_bps"])
        if metrics["edge_error_bps"] < 0:
            overestimated_trades += 1
        elif metrics["edge_error_bps"] > 0:
            underestimated_trades += 1
        item["status"] = "closed_trade"
        item["trade"] = {
            "close_reason": close_reason,
            "pnl_usd": round(pnl, 6),
            "notional_usd": round(metrics["notional_usd"], 6),
            "realized_net_bps": round(metrics["realized_net_bps"], 6),
            "realized_gross_bps": round(metrics["realized_gross_bps"], 6),
            "fees_bps": round(metrics["fees_bps"], 6),
            "edge_error_bps": round(metrics["edge_error_bps"], 6),
            "abs_edge_error_bps": round(abs(metrics["edge_error_bps"]), 6),
        }
        items.append(item)

    result = TridentAIEdgeCalibrationResult(
        candidate_input_path=str(candidate_input_path),
        llm_journal_path=str(llm_journal_path),
        paper_journal_path=str(paper_journal_path),
        report_json_path=str(json_output),
        report_md_path=str(md_output),
        candidates_seen=len(candidates),
        matched_llm_decisions=matched_llm_decisions,
        open_decisions=open_decisions,
        closed_trades=len(trade_metrics),
        winning_trades=winning_trades,
        losing_trades=losing_trades,
        false_positive_trades=false_positive_trades,
        overestimated_trades=overestimated_trades,
        underestimated_trades=underestimated_trades,
        avg_estimated_edge_bps=_average_metric(trade_metrics, "estimated_edge_bps"),
        avg_estimated_net_edge_bps=_average_metric(trade_metrics, "estimated_net_edge_bps"),
        avg_realized_net_bps=_average_metric(trade_metrics, "realized_net_bps"),
        avg_edge_error_bps=_average_metric(trade_metrics, "edge_error_bps"),
        avg_abs_edge_error_bps=_average_abs_metric(trade_metrics, "edge_error_bps"),
        realized_pnl_usd=realized_pnl_usd,
        suggested_min_edge_to_cost=_suggested_min_edge_to_cost(losing_edge_to_cost),
        suggested_min_net_edge_bps=_suggested_min_net_edge_bps(losing_estimated_net_edges),
        sample_warning=_sample_warning(len(trade_metrics)),
        close_reasons=dict(close_reasons),
        edge_buckets=_finalize_edge_buckets(bucket_stats),
        items=items,
    )
    payload = build_edge_calibration_report_payload(
        result=result,
        generated_at=_format_timestamp(datetime.now(timezone.utc)),
    )
    _write_report_outputs(payload, json_path=json_output, md_path=md_output)
    return result


def build_edge_calibration_report_payload(
    *,
    result: TridentAIEdgeCalibrationResult,
    generated_at: str,
) -> dict[str, object]:
    return {
        "generated_at": generated_at,
        "kind": "trident_ai_edge_calibration_report",
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
            candidate["market_features"] = _candidate_market_features(symbol_payload)
            candidates.append(candidate)
    return candidates


def _candidate_market_features(symbol_payload: dict[str, object]) -> dict[str, object]:
    keys = (
        "price",
        "spread_bps",
        "microprice_dislocation_bps",
        "vwap_distance_bps",
        "structure_score",
        "trade_flow_bias",
        "book_imbalance",
    )
    return {key: symbol_payload.get(key) for key in keys if key in symbol_payload}


def _llm_decisions_by_context(path: str | Path) -> dict[str, dict[str, object]]:
    decisions: dict[str, dict[str, object]] = {}
    for row in _iter_jsonl(path):
        if row.get("event_type") != LLM_REPLAY_DECISION_EVENT:
            continue
        context = _mapping(row.get("context"))
        context_id = str(context.get("context_id", "") or "")
        if context_id:
            decisions[context_id] = row
    return decisions


def _closed_trades_by_decision(path: str | Path) -> dict[str, dict[str, object]]:
    trades: dict[str, dict[str, object]] = {}
    for row in _iter_jsonl(path):
        if row.get("event_type") != PAPER_REPLAY_TRADE_CLOSED_EVENT:
            continue
        trade = _mapping(row.get("trade"))
        decision_id = str(trade.get("decision_id", "") or "")
        if decision_id:
            trades[decision_id] = trade
    return trades


def _base_item(candidate: dict[str, object]) -> dict[str, object]:
    features = _mapping(candidate.get("market_features"))
    side = str(candidate.get("side", "") or "")
    estimated_edge = _number(candidate.get("estimated_edge_bps"))
    round_trip_cost = _number(candidate.get("round_trip_cost_bps"))
    return {
        "timestamp": str(candidate.get("timestamp", "") or ""),
        "symbol": str(candidate.get("symbol", "") or ""),
        "context_id": str(candidate.get("context_id", "") or ""),
        "candidate": {
            "side": side,
            "score": _number(candidate.get("score")),
            "raw_score": _number(candidate.get("raw_score")),
            "estimated_edge_bps": estimated_edge,
            "round_trip_cost_bps": round_trip_cost,
            "estimated_net_edge_bps": round(estimated_edge - round_trip_cost, 6),
            "edge_to_cost_ratio": _number(candidate.get("edge_to_cost_ratio")),
            "microprice_dislocation_bps": _number(features.get("microprice_dislocation_bps")),
            "microprice_conflict": _microprice_conflict(features, side),
            "reasons": _string_list(candidate.get("reasons")),
        },
    }


def _trade_edge_metrics(
    candidate: dict[str, object],
    trade: dict[str, object],
) -> dict[str, float]:
    estimated_edge = _number(candidate.get("estimated_edge_bps"))
    round_trip_cost = _number(candidate.get("round_trip_cost_bps"))
    notional = _number(trade.get("notional_usd"))
    pnl = _number(trade.get("pnl_usd"))
    gross = _number(trade.get("gross_pnl_usd"))
    fees = _number(trade.get("fees_usd"))
    realized_net_bps = _pnl_bps(pnl, notional)
    edge_error = realized_net_bps - estimated_edge
    return {
        "estimated_edge_bps": estimated_edge,
        "round_trip_cost_bps": round_trip_cost,
        "estimated_net_edge_bps": estimated_edge - round_trip_cost,
        "edge_to_cost_ratio": _number(candidate.get("edge_to_cost_ratio")),
        "notional_usd": notional,
        "pnl_usd": pnl,
        "realized_net_bps": realized_net_bps,
        "realized_gross_bps": _pnl_bps(gross, notional),
        "fees_bps": _pnl_bps(fees, notional),
        "edge_error_bps": edge_error,
    }


def _pnl_bps(value: float, notional: float) -> float:
    if notional <= 0:
        return 0.0
    return round(value / notional * 10_000.0, 6)


def _suggested_min_edge_to_cost(losing_edge_to_cost: list[float]) -> float:
    if not losing_edge_to_cost:
        return 1.5
    return max(1.5, round(max(losing_edge_to_cost) + 0.1, 4))


def _suggested_min_net_edge_bps(losing_estimated_net_edges: list[float]) -> float:
    if not losing_estimated_net_edges:
        return 5.0
    return max(5.0, round(max(losing_estimated_net_edges) + 2.0, 4))


def _sample_warning(closed_trades: int) -> str:
    if closed_trades < 10:
        return "sample_too_small_keep_conservative_gates"
    return ""


def _edge_bucket(edge_to_cost: float) -> str:
    if edge_to_cost >= 1.5:
        return ">=1.50"
    if edge_to_cost >= 1.25:
        return "1.25-1.50"
    if edge_to_cost >= 1.0:
        return "1.00-1.25"
    return "<1.00"


def _empty_edge_bucket() -> dict[str, float | int]:
    return {
        "candidates": 0,
        "opens": 0,
        "closed_trades": 0,
        "pnl_usd": 0.0,
    }


def _finalize_edge_buckets(
    buckets: dict[str, dict[str, float | int]],
) -> dict[str, dict[str, object]]:
    ordered: dict[str, dict[str, object]] = {}
    for bucket in (">=1.50", "1.25-1.50", "1.00-1.25", "<1.00"):
        if bucket not in buckets:
            continue
        stats = buckets[bucket]
        ordered[bucket] = {
            "candidates": int(stats["candidates"]),
            "opens": int(stats["opens"]),
            "closed_trades": int(stats["closed_trades"]),
            "pnl_usd": round(float(stats["pnl_usd"]), 6),
        }
    return ordered


def _average_metric(items: list[dict[str, float]], key: str) -> float:
    if not items:
        return 0.0
    return sum(item[key] for item in items) / len(items)


def _average_abs_metric(items: list[dict[str, float]], key: str) -> float:
    if not items:
        return 0.0
    return sum(abs(item[key]) for item in items) / len(items)


def _microprice_conflict(features: dict[str, object], side: str) -> bool:
    dislocation = _number(features.get("microprice_dislocation_bps"))
    if abs(dislocation) < DEFAULT_MICROPRICE_CONFLICT_BPS:
        return False
    normalized_side = side.strip().lower()
    if normalized_side == "long":
        return dislocation < 0
    if normalized_side == "short":
        return dislocation > 0
    return False


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
        "# TRIDENT-AI Edge Calibration Report",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Candidate input: `{result['candidate_input_path']}`",
        f"- LLM journal: `{result['llm_journal_path']}`",
        f"- Paper journal: `{result['paper_journal_path']}`",
        f"- Candidates seen: `{result['candidates_seen']}`",
        f"- Matched LLM decisions: `{result['matched_llm_decisions']}`",
        f"- Open decisions: `{result['open_decisions']}`",
        f"- Closed trades: `{result['closed_trades']}`",
        f"- False positive trades: `{result['false_positive_trades']}`",
        f"- Realized PnL: `${result['realized_pnl_usd']:.6f}`",
        f"- Avg estimated edge: `{result['avg_estimated_edge_bps']:.4f} bps`",
        f"- Avg estimated net edge: `{result['avg_estimated_net_edge_bps']:.4f} bps`",
        f"- Avg realized net: `{result['avg_realized_net_bps']:.4f} bps`",
        f"- Avg edge error: `{result['avg_edge_error_bps']:.4f} bps`",
        f"- Suggested min edge/cost: `{result['suggested_min_edge_to_cost']:.4f}`",
        f"- Suggested min net edge: `{result['suggested_min_net_edge_bps']:.4f} bps`",
        f"- Sample warning: `{result['sample_warning']}`",
        "",
        "## Edge Buckets",
        "",
        "| Edge/Cost | Candidates | Opens | Trades | PnL |",
        "|---|---:|---:|---:|---:|",
    ]
    buckets = result["edge_buckets"]
    assert isinstance(buckets, dict)
    for bucket, stats in buckets.items():
        assert isinstance(stats, dict)
        lines.append(
            f"| {bucket} | {stats['candidates']} | {stats['opens']} | "
            f"{stats['closed_trades']} | ${stats['pnl_usd']:.6f} |"
        )
    if not buckets:
        lines.append("| none | 0 | 0 | 0 | $0.000000 |")

    lines.extend(
        [
            "",
            "## Trades",
            "",
            "| Symbol | Time | Score | Edge | Cost | Net Est | Edge/Cost | Realized | Error | Close | Status |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|",
        ]
    )
    items = result["items"]
    assert isinstance(items, list)
    for item in items:
        assert isinstance(item, dict)
        candidate = _mapping(item.get("candidate"))
        trade = _mapping(item.get("trade"))
        lines.append(
            f"| {item.get('symbol', '')} | {item.get('timestamp', '')} | "
            f"{_number(candidate.get('score')):.4f} | "
            f"{_number(candidate.get('estimated_edge_bps')):.2f} | "
            f"{_number(candidate.get('round_trip_cost_bps')):.2f} | "
            f"{_number(candidate.get('estimated_net_edge_bps')):.2f} | "
            f"{_number(candidate.get('edge_to_cost_ratio')):.2f} | "
            f"{_number(trade.get('realized_net_bps')):.2f} | "
            f"{_number(trade.get('edge_error_bps')):.2f} | "
            f"{trade.get('close_reason', 'n/a')} | {item.get('status', '')} |"
        )
    if not items:
        lines.append("| none | n/a | 0.0000 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | n/a | none |")
    lines.append("")
    return "\n".join(lines)


def _iter_jsonl(path: str | Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _timestamp_id(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
