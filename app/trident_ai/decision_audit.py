from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.trident_ai.candidate_scan import (
    CANDIDATE_HINT_FIELD,
    DEFAULT_MICROPRICE_CONFLICT_BPS,
    DEFAULT_MIN_EDGE_TO_COST_RATIO,
    DEFAULT_MIN_NET_EDGE_BPS,
)
from app.trident_ai.config import TridentAIConfig, load_trident_ai_config
from app.trident_ai.replay import LLM_REPLAY_DECISION_EVENT


@dataclass(frozen=True, slots=True)
class TridentAIDecisionAuditResult:
    candidate_input_path: str
    llm_journal_path: str
    report_json_path: str
    report_md_path: str
    min_edge_to_cost: float = DEFAULT_MIN_EDGE_TO_COST_RATIO
    min_net_edge_bps: float = DEFAULT_MIN_NET_EDGE_BPS
    candidates_seen: int = 0
    matched_llm_decisions: int = 0
    hold_decisions: int = 0
    open_decisions: int = 0
    eligible_candidates: int = 0
    eligible_holds: int = 0
    contradictory_decisions: int = 0
    contradiction_counts: dict[str, int] = field(default_factory=dict)
    action_counts: dict[str, int] = field(default_factory=dict)
    items: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_input_path": self.candidate_input_path,
            "llm_journal_path": self.llm_journal_path,
            "report_json_path": self.report_json_path,
            "report_md_path": self.report_md_path,
            "min_edge_to_cost": round(self.min_edge_to_cost, 6),
            "min_net_edge_bps": round(self.min_net_edge_bps, 6),
            "candidates_seen": self.candidates_seen,
            "matched_llm_decisions": self.matched_llm_decisions,
            "hold_decisions": self.hold_decisions,
            "open_decisions": self.open_decisions,
            "eligible_candidates": self.eligible_candidates,
            "eligible_holds": self.eligible_holds,
            "contradictory_decisions": self.contradictory_decisions,
            "contradiction_counts": dict(sorted(self.contradiction_counts.items())),
            "action_counts": dict(sorted(self.action_counts.items())),
            "items": self.items,
        }


def run_trident_ai_llm_decision_audit(
    *,
    candidate_input_path: str | Path,
    llm_journal_path: str | Path,
    config: TridentAIConfig | None = None,
    report_json_path: str | Path | None = None,
    report_md_path: str | Path | None = None,
    min_edge_to_cost: float = DEFAULT_MIN_EDGE_TO_COST_RATIO,
    min_net_edge_bps: float = DEFAULT_MIN_NET_EDGE_BPS,
) -> TridentAIDecisionAuditResult:
    resolved_config = config or load_trident_ai_config()
    run_id = _timestamp_id(datetime.now(timezone.utc))
    output_dir = Path(resolved_config.paths.replay_output_dir)
    json_output = Path(report_json_path or output_dir / f"trident_ai_llm_decision_audit_{run_id}.json")
    md_output = Path(report_md_path or output_dir / f"trident_ai_llm_decision_audit_{run_id}.md")

    candidates = _candidate_records(candidate_input_path)
    llm_by_context = _llm_decisions_by_context(llm_journal_path)
    action_counts: Counter[str] = Counter()
    contradiction_counts: Counter[str] = Counter()
    items: list[dict[str, object]] = []
    matched_llm_decisions = 0
    hold_decisions = 0
    open_decisions = 0
    eligible_candidates = 0
    eligible_holds = 0
    contradictory_decisions = 0

    for candidate in candidates:
        item = _base_item(candidate)
        eligible = _candidate_is_eligible(
            item,
            min_edge_to_cost=min_edge_to_cost,
            min_net_edge_bps=min_net_edge_bps,
        )
        item["eligible_candidate"] = eligible
        if eligible:
            eligible_candidates += 1
        context_id = str(candidate.get("context_id", "") or "")
        llm_record = llm_by_context.get(context_id)
        if llm_record is None:
            item["status"] = "missing_llm_decision"
            items.append(item)
            continue

        matched_llm_decisions += 1
        proposal = _mapping(llm_record.get("proposal"))
        action = str(proposal.get("action", "") or "")
        action_counts[action] += 1
        if action == "hold":
            hold_decisions += 1
        elif action == "open":
            open_decisions += 1
        item["llm"] = {
            "action": action,
            "confidence": _number(proposal.get("confidence")),
            "decision_id": str(proposal.get("decision_id", "") or ""),
            "rationale_tags": _string_list(proposal.get("rationale_tags")),
            "evidence_ids": _string_list(proposal.get("evidence_ids")),
            "risk_notes": _string_list(proposal.get("risk_notes")),
        }
        if action == "hold" and eligible:
            eligible_holds += 1
            item.setdefault("findings", []).append("eligible_candidate_held")

        findings = _decision_findings(
            candidate_item=item,
            llm_record=llm_record,
            proposal=proposal,
            min_edge_to_cost=min_edge_to_cost,
            min_net_edge_bps=min_net_edge_bps,
        )
        for finding in findings:
            contradiction_counts[finding] += 1
        if findings:
            contradictory_decisions += 1
            item.setdefault("findings", []).extend(findings)
            item["status"] = "contradiction"
        elif action == "hold" and eligible:
            item["status"] = "eligible_hold"
        else:
            item["status"] = "ok"
        items.append(item)

    result = TridentAIDecisionAuditResult(
        candidate_input_path=str(candidate_input_path),
        llm_journal_path=str(llm_journal_path),
        report_json_path=str(json_output),
        report_md_path=str(md_output),
        min_edge_to_cost=min_edge_to_cost,
        min_net_edge_bps=min_net_edge_bps,
        candidates_seen=len(candidates),
        matched_llm_decisions=matched_llm_decisions,
        hold_decisions=hold_decisions,
        open_decisions=open_decisions,
        eligible_candidates=eligible_candidates,
        eligible_holds=eligible_holds,
        contradictory_decisions=contradictory_decisions,
        contradiction_counts=dict(contradiction_counts),
        action_counts=dict(action_counts),
        items=items,
    )
    payload = build_llm_decision_audit_report_payload(
        result=result,
        generated_at=_format_timestamp(datetime.now(timezone.utc)),
    )
    _write_report_outputs(payload, json_path=json_output, md_path=md_output)
    return result


def build_llm_decision_audit_report_payload(
    *,
    result: TridentAIDecisionAuditResult,
    generated_at: str,
) -> dict[str, object]:
    return {
        "generated_at": generated_at,
        "kind": "trident_ai_llm_decision_audit",
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
        "microprice_dislocation_bps",
        "spread_bps",
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


def _base_item(candidate: dict[str, object]) -> dict[str, object]:
    features = _mapping(candidate.get("market_features"))
    side = str(candidate.get("side", "") or "")
    estimated_edge = _number(candidate.get("estimated_edge_bps"))
    round_trip_cost = _number(candidate.get("round_trip_cost_bps"))
    estimated_net_edge = _number(candidate.get("estimated_net_edge_bps"))
    if estimated_net_edge == 0 and estimated_edge > 0:
        estimated_net_edge = estimated_edge - round_trip_cost
    return {
        "timestamp": str(candidate.get("timestamp", "") or ""),
        "symbol": str(candidate.get("symbol", "") or ""),
        "context_id": str(candidate.get("context_id", "") or ""),
        "candidate": {
            "side": side,
            "score": _number(candidate.get("score")),
            "estimated_edge_bps": estimated_edge,
            "round_trip_cost_bps": round_trip_cost,
            "estimated_net_edge_bps": round(estimated_net_edge, 6),
            "edge_to_cost_ratio": _number(candidate.get("edge_to_cost_ratio")),
            "microprice_dislocation_bps": _number(features.get("microprice_dislocation_bps")),
            "microprice_conflict": _microprice_conflict(features, side),
            "reasons": _string_list(candidate.get("reasons")),
        },
        "findings": [],
    }


def _candidate_is_eligible(
    item: dict[str, object],
    *,
    min_edge_to_cost: float,
    min_net_edge_bps: float,
) -> bool:
    candidate = _mapping(item.get("candidate"))
    return (
        _number(candidate.get("edge_to_cost_ratio")) >= min_edge_to_cost
        and _number(candidate.get("estimated_net_edge_bps")) >= min_net_edge_bps
        and not bool(candidate.get("microprice_conflict", False))
    )


def _decision_findings(
    *,
    candidate_item: dict[str, object],
    llm_record: dict[str, object],
    proposal: dict[str, object],
    min_edge_to_cost: float,
    min_net_edge_bps: float,
) -> list[str]:
    candidate = _mapping(candidate_item.get("candidate"))
    edge_to_cost = _number(candidate.get("edge_to_cost_ratio"))
    net_edge = _number(candidate.get("estimated_net_edge_bps"))
    text = _decision_text(llm_record=llm_record, proposal=proposal)
    findings: list[str] = []
    if edge_to_cost >= min_edge_to_cost and _claims_edge_to_cost_below_threshold(text):
        findings.append("false_edge_to_cost_below_threshold")
    if net_edge >= min_net_edge_bps and _claims_net_edge_below_threshold(text):
        findings.append("false_net_edge_below_threshold")
    if not bool(candidate.get("microprice_conflict", False)) and _claims_microprice_conflict(text):
        findings.append("false_microprice_conflict")
    return findings


def _decision_text(
    *,
    llm_record: dict[str, object],
    proposal: dict[str, object],
) -> str:
    parts: list[str] = []
    for field_name in ("rationale_tags", "evidence_ids", "risk_notes"):
        parts.extend(_string_list(proposal.get(field_name)))
    response = _mapping(llm_record.get("llm_response"))
    raw_text = response.get("raw_text")
    if isinstance(raw_text, str):
        parts.append(raw_text)
    return " ".join(parts).lower().replace("-", "_")


def _claims_edge_to_cost_below_threshold(text: str) -> bool:
    patterns = (
        "edge_to_cost_below_threshold",
        "edge_to_cost below",
        "edge/cost below",
        "edge cost below",
        "edge_to_cost<",
    )
    return any(pattern in text for pattern in patterns)


def _claims_net_edge_below_threshold(text: str) -> bool:
    patterns = (
        "net_edge_bps_below_threshold",
        "net_edge below",
        "net edge below",
        "net_edge_bps below",
        "weak_net_edge",
        "net edge is below",
    )
    return any(pattern in text for pattern in patterns)


def _claims_microprice_conflict(text: str) -> bool:
    patterns = (
        "microprice_conflict",
        "microprice adverse",
        "microprice against",
    )
    return any(pattern in text for pattern in patterns)


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
        "# TRIDENT-AI LLM Decision Audit",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Candidate input: `{result['candidate_input_path']}`",
        f"- LLM journal: `{result['llm_journal_path']}`",
        f"- Min edge/cost: `{result['min_edge_to_cost']:.4f}`",
        f"- Min net edge: `{result['min_net_edge_bps']:.4f} bps`",
        f"- Candidates seen: `{result['candidates_seen']}`",
        f"- Matched LLM decisions: `{result['matched_llm_decisions']}`",
        f"- Eligible candidates: `{result['eligible_candidates']}`",
        f"- Eligible holds: `{result['eligible_holds']}`",
        f"- Contradictory decisions: `{result['contradictory_decisions']}`",
        f"- Contradiction counts: `{result['contradiction_counts']}`",
        "",
        "## Decisions",
        "",
        "| Symbol | Time | Action | Score | Net Edge | Edge/Cost | Eligible | Status | Findings |",
        "|---|---|---|---:|---:|---:|---|---|---|",
    ]
    items = result["items"]
    assert isinstance(items, list)
    for item in items:
        assert isinstance(item, dict)
        candidate = _mapping(item.get("candidate"))
        llm = _mapping(item.get("llm"))
        findings = item.get("findings", [])
        lines.append(
            f"| {item.get('symbol', '')} | {item.get('timestamp', '')} | "
            f"{llm.get('action', 'n/a')} | "
            f"{_number(candidate.get('score')):.4f} | "
            f"{_number(candidate.get('estimated_net_edge_bps')):.2f} | "
            f"{_number(candidate.get('edge_to_cost_ratio')):.2f} | "
            f"{bool(item.get('eligible_candidate', False))} | "
            f"{item.get('status', '')} | {_markdown_cell(', '.join(_string_list(findings)))} |"
        )
    if not items:
        lines.append("| none | n/a | n/a | 0.0000 | 0.00 | 0.00 | False | none | none |")
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


def _markdown_cell(value: str) -> str:
    return value.replace("|", "/").replace("\n", " ").strip()


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _timestamp_id(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
