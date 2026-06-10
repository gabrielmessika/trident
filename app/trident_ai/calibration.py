from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.trident_ai.candidate_scan import CANDIDATE_HINT_FIELD
from app.trident_ai.config import TridentAIConfig, load_trident_ai_config
from app.trident_ai.paper import (
    PAPER_REPLAY_DECISION_EVENT,
    PAPER_REPLAY_TRADE_CLOSED_EVENT,
)
from app.trident_ai.replay import LLM_REPLAY_DECISION_EVENT


@dataclass(frozen=True, slots=True)
class TridentAICalibrationResult:
    candidate_input_path: str
    llm_journal_path: str
    paper_journal_path: str
    report_json_path: str
    report_md_path: str
    candidates_seen: int = 0
    llm_decisions_seen: int = 0
    paper_decisions_seen: int = 0
    matched_candidates: int = 0
    missing_llm_decisions: int = 0
    matched_paper_decisions: int = 0
    closed_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    ai_cost_usd: float = 0.0
    realized_pnl_usd: float = 0.0
    net_after_ai_cost_usd: float = 0.0
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    llm_action_counts: dict[str, int] = field(default_factory=dict)
    paper_action_counts: dict[str, int] = field(default_factory=dict)
    close_reasons: dict[str, int] = field(default_factory=dict)
    score_buckets: dict[str, dict[str, object]] = field(default_factory=dict)
    items: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_input_path": self.candidate_input_path,
            "llm_journal_path": self.llm_journal_path,
            "paper_journal_path": self.paper_journal_path,
            "report_json_path": self.report_json_path,
            "report_md_path": self.report_md_path,
            "candidates_seen": self.candidates_seen,
            "llm_decisions_seen": self.llm_decisions_seen,
            "paper_decisions_seen": self.paper_decisions_seen,
            "matched_candidates": self.matched_candidates,
            "missing_llm_decisions": self.missing_llm_decisions,
            "matched_paper_decisions": self.matched_paper_decisions,
            "closed_trades": self.closed_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "ai_cost_usd": round(self.ai_cost_usd, 8),
            "realized_pnl_usd": round(self.realized_pnl_usd, 6),
            "net_after_ai_cost_usd": round(self.net_after_ai_cost_usd, 8),
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "llm_action_counts": dict(sorted(self.llm_action_counts.items())),
            "paper_action_counts": dict(sorted(self.paper_action_counts.items())),
            "close_reasons": dict(sorted(self.close_reasons.items())),
            "score_buckets": self.score_buckets,
            "items": self.items,
        }


def run_trident_ai_calibration_report(
    *,
    candidate_input_path: str | Path,
    llm_journal_path: str | Path,
    paper_journal_path: str | Path,
    config: TridentAIConfig | None = None,
    report_json_path: str | Path | None = None,
    report_md_path: str | Path | None = None,
) -> TridentAICalibrationResult:
    resolved_config = config or load_trident_ai_config()
    run_id = _timestamp_id(datetime.now(timezone.utc))
    output_dir = Path(resolved_config.paths.replay_output_dir)
    json_output = Path(report_json_path or output_dir / f"trident_ai_calibration_{run_id}.json")
    md_output = Path(report_md_path or output_dir / f"trident_ai_calibration_{run_id}.md")

    candidates = _candidate_records(candidate_input_path)
    llm_by_context, llm_decisions_seen = _llm_decisions_by_context(llm_journal_path)
    paper_by_request, paper_decisions_seen = _paper_decisions_by_request(paper_journal_path)
    trades_by_decision = _closed_trades_by_decision(paper_journal_path)

    items: list[dict[str, object]] = []
    llm_action_counts: Counter[str] = Counter()
    paper_action_counts: Counter[str] = Counter()
    close_reasons: Counter[str] = Counter()
    score_bucket_stats: dict[str, dict[str, float | int]] = defaultdict(_empty_bucket)
    matched_candidates = 0
    missing_llm_decisions = 0
    matched_paper_decisions = 0
    ai_cost_usd = 0.0
    realized_pnl_usd = 0.0
    closed_trades = 0
    winning_trades = 0
    losing_trades = 0
    timestamps: list[str] = []

    for candidate in candidates:
        context_id = str(candidate.get("context_id", "") or "")
        timestamp = str(candidate.get("timestamp", "") or "")
        if timestamp:
            timestamps.append(timestamp)
        llm_record = llm_by_context.get(context_id)
        item = _candidate_item(candidate)
        bucket = _score_bucket(_number(candidate.get("score")))
        score_bucket_stats[bucket]["candidates"] += 1

        if llm_record is None:
            missing_llm_decisions += 1
            item["status"] = "missing_llm_decision"
            items.append(item)
            continue

        matched_candidates += 1
        item["status"] = "matched"
        request_id = _request_id(llm_record)
        proposal = _mapping(llm_record.get("proposal"))
        action = str(proposal.get("action", "") or "")
        confidence = _number(proposal.get("confidence"))
        cost = _llm_cost_usd(llm_record)
        ai_cost_usd += cost
        llm_action_counts[action] += 1
        score_bucket_stats[bucket]["llm_decisions"] += 1
        if action == "open":
            score_bucket_stats[bucket]["opens"] += 1
        item["llm"] = {
            "request_id": request_id,
            "action": action,
            "side": str(proposal.get("side", "") or ""),
            "confidence": confidence,
            "cost_usd": round(cost, 8),
            "validation": _mapping(llm_record.get("validation")),
            "rationale_tags": _string_list(proposal.get("rationale_tags")),
            "risk_notes": _string_list(proposal.get("risk_notes")),
        }

        paper_record = paper_by_request.get(request_id)
        if paper_record is not None:
            matched_paper_decisions += 1
            paper_action = str(paper_record.get("paper_action", "") or "")
            paper_action_counts[paper_action] += 1
            item["paper"] = {
                "paper_action": paper_action,
                "reason": str(paper_record.get("reason", "") or ""),
                "price": _number(paper_record.get("price")),
            }

        decision_id = str(proposal.get("decision_id", "") or "")
        trade = trades_by_decision.get(decision_id)
        if trade is not None:
            pnl = _number(trade.get("pnl_usd"))
            closed_trades += 1
            realized_pnl_usd += pnl
            score_bucket_stats[bucket]["closed_trades"] += 1
            score_bucket_stats[bucket]["pnl_usd"] += pnl
            if pnl > 0:
                winning_trades += 1
            elif pnl < 0:
                losing_trades += 1
            close_reason = str(trade.get("close_reason", "") or "")
            close_reasons[close_reason] += 1
            item["trade"] = {
                "close_reason": close_reason,
                "pnl_usd": round(pnl, 6),
                "gross_pnl_usd": round(_number(trade.get("gross_pnl_usd")), 6),
                "fees_usd": round(_number(trade.get("fees_usd")), 6),
                "entry_price": _number(trade.get("entry_price")),
                "exit_price": _number(trade.get("exit_price")),
            }
        items.append(item)

    score_buckets = _finalize_score_buckets(score_bucket_stats)
    result = TridentAICalibrationResult(
        candidate_input_path=str(candidate_input_path),
        llm_journal_path=str(llm_journal_path),
        paper_journal_path=str(paper_journal_path),
        report_json_path=str(json_output),
        report_md_path=str(md_output),
        candidates_seen=len(candidates),
        llm_decisions_seen=llm_decisions_seen,
        paper_decisions_seen=paper_decisions_seen,
        matched_candidates=matched_candidates,
        missing_llm_decisions=missing_llm_decisions,
        matched_paper_decisions=matched_paper_decisions,
        closed_trades=closed_trades,
        winning_trades=winning_trades,
        losing_trades=losing_trades,
        ai_cost_usd=ai_cost_usd,
        realized_pnl_usd=realized_pnl_usd,
        net_after_ai_cost_usd=realized_pnl_usd - ai_cost_usd,
        first_timestamp=min(timestamps) if timestamps else None,
        last_timestamp=max(timestamps) if timestamps else None,
        llm_action_counts=dict(llm_action_counts),
        paper_action_counts=dict(paper_action_counts),
        close_reasons=dict(close_reasons),
        score_buckets=score_buckets,
        items=items,
    )
    payload = build_calibration_report_payload(
        result=result,
        generated_at=_format_timestamp(datetime.now(timezone.utc)),
    )
    _write_report_outputs(payload, json_path=json_output, md_path=md_output)
    return result


def build_calibration_report_payload(
    *,
    result: TridentAICalibrationResult,
    generated_at: str,
) -> dict[str, object]:
    return {
        "generated_at": generated_at,
        "kind": "trident_ai_calibration_report",
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
            candidates.append(candidate)
    return candidates


def _llm_decisions_by_context(path: str | Path) -> tuple[dict[str, dict[str, object]], int]:
    decisions: dict[str, dict[str, object]] = {}
    count = 0
    for row in _iter_jsonl(path):
        if row.get("event_type") != LLM_REPLAY_DECISION_EVENT:
            continue
        count += 1
        context = _mapping(row.get("context"))
        context_id = str(context.get("context_id", "") or "")
        if context_id:
            decisions[context_id] = row
    return decisions, count


def _paper_decisions_by_request(path: str | Path) -> tuple[dict[str, dict[str, object]], int]:
    decisions: dict[str, dict[str, object]] = {}
    count = 0
    for row in _iter_jsonl(path):
        if row.get("event_type") != PAPER_REPLAY_DECISION_EVENT:
            continue
        count += 1
        request_id = str(row.get("request_id", "") or "")
        if request_id:
            decisions[request_id] = row
    return decisions, count


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


def _candidate_item(candidate: dict[str, object]) -> dict[str, object]:
    return {
        "timestamp": str(candidate.get("timestamp", "") or ""),
        "symbol": str(candidate.get("symbol", "") or ""),
        "context_id": str(candidate.get("context_id", "") or ""),
        "candidate": {
            "side": str(candidate.get("side", "") or ""),
            "score": _number(candidate.get("score")),
            "raw_score": _number(candidate.get("raw_score")),
            "directional": _number(candidate.get("directional_score")),
            "liquidity": _number(candidate.get("liquidity_score")),
            "activity": _number(candidate.get("activity_score")),
            "cost_score": _number(candidate.get("cost_score")),
            "estimated_edge_bps": _number(candidate.get("estimated_edge_bps")),
            "round_trip_cost_bps": _number(candidate.get("round_trip_cost_bps")),
            "edge_to_cost_ratio": _number(candidate.get("edge_to_cost_ratio")),
            "reasons": _string_list(candidate.get("reasons")),
        },
    }


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
        "# TRIDENT-AI Calibration Report",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Candidate input: `{result['candidate_input_path']}`",
        f"- LLM journal: `{result['llm_journal_path']}`",
        f"- Paper journal: `{result['paper_journal_path']}`",
        f"- Candidates seen: `{result['candidates_seen']}`",
        f"- Matched LLM decisions: `{result['matched_candidates']}`",
        f"- Missing LLM decisions: `{result['missing_llm_decisions']}`",
        f"- Matched paper decisions: `{result['matched_paper_decisions']}`",
        f"- AI cost: `${result['ai_cost_usd']:.8f}`",
        f"- Realized PnL: `${result['realized_pnl_usd']:.6f}`",
        f"- Net after AI cost: `${result['net_after_ai_cost_usd']:.8f}`",
        "",
        "## Actions",
        "",
        "| LLM action | Count |",
        "|---|---:|",
    ]
    action_counts = result["llm_action_counts"]
    assert isinstance(action_counts, dict)
    for action, count in action_counts.items():
        lines.append(f"| {action} | {count} |")
    if not action_counts:
        lines.append("| none | 0 |")

    lines.extend(["", "## Score Buckets", "", "| Bucket | Candidates | LLM | Opens | Trades | PnL |", "|---|---:|---:|---:|---:|---:|"])
    buckets = result["score_buckets"]
    assert isinstance(buckets, dict)
    for bucket, stats in buckets.items():
        assert isinstance(stats, dict)
        lines.append(
            f"| {bucket} | {stats['candidates']} | {stats['llm_decisions']} | "
            f"{stats['opens']} | {stats['closed_trades']} | ${stats['pnl_usd']:.6f} |"
        )
    if not buckets:
        lines.append("| none | 0 | 0 | 0 | 0 | $0.000000 |")

    lines.extend(
        [
            "",
            "## Candidates",
            "",
            "| Symbol | Time | Score | Raw | Cost | Edge/Cost | LLM | Confidence | Paper | PnL | Status |",
            "|---|---|---:|---:|---:|---:|---|---:|---|---:|---|",
        ]
    )
    items = result["items"]
    assert isinstance(items, list)
    for item in items:
        assert isinstance(item, dict)
        candidate = _mapping(item.get("candidate"))
        llm = _mapping(item.get("llm"))
        paper = _mapping(item.get("paper"))
        trade = _mapping(item.get("trade"))
        lines.append(
            f"| {item.get('symbol', '')} | {item.get('timestamp', '')} | "
            f"{_number(candidate.get('score')):.4f} | "
            f"{_number(candidate.get('raw_score')):.4f} | "
            f"{_number(candidate.get('round_trip_cost_bps')):.2f} | "
            f"{_number(candidate.get('edge_to_cost_ratio')):.2f} | "
            f"{llm.get('action', 'n/a')} | "
            f"{_number(llm.get('confidence')):.2f} | {paper.get('paper_action', 'n/a')} | "
            f"${_number(trade.get('pnl_usd')):.6f} | {item.get('status', '')} |"
        )
    if not items:
        lines.append(
            "| none | n/a | 0.0000 | 0.0000 | 0.00 | 0.00 | n/a | 0.00 | n/a | "
            "$0.000000 | none |"
        )
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


def _request_id(record: dict[str, object]) -> str:
    request = _mapping(record.get("request"))
    return str(request.get("request_id", "") or "")


def _llm_cost_usd(record: dict[str, object]) -> float:
    response = _mapping(record.get("llm_response"))
    usage = _mapping(response.get("usage"))
    return _number(usage.get("estimated_cost_usd"))


def _score_bucket(score: float) -> str:
    if score >= 2.0:
        return ">=2.00"
    if score >= 1.5:
        return "1.50-2.00"
    return "<1.50"


def _empty_bucket() -> dict[str, float | int]:
    return {
        "candidates": 0,
        "llm_decisions": 0,
        "opens": 0,
        "closed_trades": 0,
        "pnl_usd": 0.0,
    }


def _finalize_score_buckets(
    buckets: dict[str, dict[str, float | int]],
) -> dict[str, dict[str, object]]:
    ordered: dict[str, dict[str, object]] = {}
    for bucket in (">=2.00", "1.50-2.00", "<1.50"):
        if bucket not in buckets:
            continue
        stats = buckets[bucket]
        ordered[bucket] = {
            "candidates": int(stats["candidates"]),
            "llm_decisions": int(stats["llm_decisions"]),
            "opens": int(stats["opens"]),
            "closed_trades": int(stats["closed_trades"]),
            "pnl_usd": round(float(stats["pnl_usd"]), 6),
        }
    return ordered


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _timestamp_id(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
