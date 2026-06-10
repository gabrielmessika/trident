from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.backtest.snapshot_loader import SnapshotLoader, SnapshotRecord
from app.trident_ai.candidate_scan import CANDIDATE_HINT_FIELD
from app.trident_ai.config import TridentAIConfig, load_trident_ai_config
from app.trident_ai.features import AgentMarketContextBuildConfig, TridentAIFeatureBuilder
from app.trident_ai.paper import TridentAIPaperReplayResult, run_trident_ai_paper_replay
from app.trident_ai.replay import LLM_REPLAY_DECISION_EVENT
from app.trident_ai.types import (
    AgentMarketContext,
    TRIDENT_AI_MARKET_CONTEXT_SCHEMA_VERSION,
    TRIDENT_AI_PROPOSAL_SCHEMA_VERSION,
)


CANDIDATE_PAPER_DECISION_SOURCE = "trident_ai_candidate_paper_replay"
DEFAULT_CANDIDATE_PAPER_CONFIDENCE = 0.62
DEFAULT_CANDIDATE_PAPER_STOP_BPS = 120.0
DEFAULT_CANDIDATE_PAPER_TAKE_PROFIT_BPS = 240.0
DEFAULT_CANDIDATE_PAPER_TIME_STOP_MINUTES = 180


@dataclass(frozen=True, slots=True)
class TridentAICandidatePaperReplayResult:
    candidate_input_path: str
    decision_journal_path: str
    paper_journal_path: str
    report_json_path: str
    report_md_path: str
    market_input_path: str
    symbols_filter: tuple[str, ...] = ()
    candidates_seen: int = 0
    decisions_written: int = 0
    skipped_candidates: int = 0
    max_candidates: int | None = None
    requested_notional_usd: float | None = None
    effective_notional_usd: float = 0.0
    confidence: float = DEFAULT_CANDIDATE_PAPER_CONFIDENCE
    stop_bps: float = DEFAULT_CANDIDATE_PAPER_STOP_BPS
    take_profit_bps: float = DEFAULT_CANDIDATE_PAPER_TAKE_PROFIT_BPS
    time_stop_minutes: int = DEFAULT_CANDIDATE_PAPER_TIME_STOP_MINUTES
    min_edge_to_cost: float | None = None
    min_net_edge_bps: float | None = None
    min_liquidity_score: float | None = None
    max_round_trip_cost_bps: float | None = None
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    symbol_counts: dict[str, int] = field(default_factory=dict)
    side_counts: dict[str, int] = field(default_factory=dict)
    skip_reasons: dict[str, int] = field(default_factory=dict)
    paper_result: TridentAIPaperReplayResult | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_input_path": self.candidate_input_path,
            "decision_journal_path": self.decision_journal_path,
            "paper_journal_path": self.paper_journal_path,
            "report_json_path": self.report_json_path,
            "report_md_path": self.report_md_path,
            "market_input_path": self.market_input_path,
            "symbols_filter": list(self.symbols_filter),
            "candidates_seen": self.candidates_seen,
            "decisions_written": self.decisions_written,
            "skipped_candidates": self.skipped_candidates,
            "max_candidates": self.max_candidates,
            "requested_notional_usd": self.requested_notional_usd,
            "effective_notional_usd": round(self.effective_notional_usd, 6),
            "confidence": round(self.confidence, 6),
            "stop_bps": round(self.stop_bps, 6),
            "take_profit_bps": round(self.take_profit_bps, 6),
            "time_stop_minutes": self.time_stop_minutes,
            "min_edge_to_cost": _round_optional(self.min_edge_to_cost),
            "min_net_edge_bps": _round_optional(self.min_net_edge_bps),
            "min_liquidity_score": _round_optional(self.min_liquidity_score),
            "max_round_trip_cost_bps": _round_optional(self.max_round_trip_cost_bps),
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "symbol_counts": dict(sorted(self.symbol_counts.items())),
            "side_counts": dict(sorted(self.side_counts.items())),
            "skip_reasons": dict(sorted(self.skip_reasons.items())),
            "paper_result": self.paper_result.to_dict() if self.paper_result is not None else {},
        }


@dataclass(slots=True)
class _SyntheticDecisionBuildResult:
    decisions: list[dict[str, object]] = field(default_factory=list)
    candidates_seen: int = 0
    skipped_candidates: int = 0
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    symbol_counts: Counter[str] = field(default_factory=Counter)
    side_counts: Counter[str] = field(default_factory=Counter)
    skip_reasons: Counter[str] = field(default_factory=Counter)


def run_trident_ai_candidate_paper_replay(
    candidate_input_path: str | Path,
    *,
    market_input_path: str | Path,
    config: TridentAIConfig | None = None,
    decision_journal_path: str | Path | None = None,
    journal_path: str | Path | None = None,
    report_json_path: str | Path | None = None,
    report_md_path: str | Path | None = None,
    max_candidates: int | None = None,
    symbols: Sequence[str] | None = None,
    notional_usd: float | None = None,
    confidence: float = DEFAULT_CANDIDATE_PAPER_CONFIDENCE,
    stop_bps: float = DEFAULT_CANDIDATE_PAPER_STOP_BPS,
    take_profit_bps: float = DEFAULT_CANDIDATE_PAPER_TAKE_PROFIT_BPS,
    time_stop_minutes: int = DEFAULT_CANDIDATE_PAPER_TIME_STOP_MINUTES,
    min_edge_to_cost: float | None = None,
    min_net_edge_bps: float | None = None,
    min_liquidity_score: float | None = None,
    max_round_trip_cost_bps: float | None = None,
) -> TridentAICandidatePaperReplayResult:
    if max_candidates is not None and max_candidates <= 0:
        raise ValueError("max_candidates_must_be_positive")
    if confidence < 0.0 or confidence > 1.0:
        raise ValueError("confidence_must_be_between_0_and_1")
    if stop_bps <= 0.0:
        raise ValueError("stop_bps_must_be_positive")
    if take_profit_bps <= stop_bps:
        raise ValueError("take_profit_bps_must_be_above_stop_bps")
    if time_stop_minutes <= 0:
        raise ValueError("time_stop_minutes_must_be_positive")
    if notional_usd is not None and notional_usd <= 0.0:
        raise ValueError("notional_usd_must_be_positive")
    if min_edge_to_cost is not None and min_edge_to_cost < 0.0:
        raise ValueError("min_edge_to_cost_must_be_non_negative")
    if min_net_edge_bps is not None and min_net_edge_bps < 0.0:
        raise ValueError("min_net_edge_bps_must_be_non_negative")
    if min_liquidity_score is not None and min_liquidity_score < 0.0:
        raise ValueError("min_liquidity_score_must_be_non_negative")
    if max_round_trip_cost_bps is not None and max_round_trip_cost_bps <= 0.0:
        raise ValueError("max_round_trip_cost_bps_must_be_positive")

    active_config = config or load_trident_ai_config()
    effective_notional = _effective_notional_usd(
        requested=notional_usd,
        cap=active_config.risk.live_max_order_notional_usd,
    )
    effective_confidence = max(float(confidence), active_config.risk.min_confidence)
    symbols_filter = _symbols_filter(symbols)
    run_id = _timestamp_id(datetime.now(timezone.utc))
    output_dir = Path(active_config.paths.replay_output_dir)
    decision_output = Path(
        decision_journal_path
        or output_dir / f"trident_ai_candidate_paper_decisions_{run_id}.jsonl"
    )
    paper_journal_output = Path(
        journal_path or output_dir / f"trident_ai_candidate_paper_replay_{run_id}.jsonl"
    )
    report_json_output = Path(
        report_json_path or output_dir / f"trident_ai_candidate_paper_replay_{run_id}.json"
    )
    report_md_output = Path(
        report_md_path or output_dir / f"trident_ai_candidate_paper_replay_{run_id}.md"
    )

    build_result = _build_synthetic_decisions(
        candidate_input_path,
        config=active_config,
        max_candidates=max_candidates,
        symbols_filter=symbols_filter,
        notional_usd=effective_notional,
        max_leverage=active_config.risk.max_leverage,
        confidence=effective_confidence,
        stop_bps=float(stop_bps),
        take_profit_bps=float(take_profit_bps),
        time_stop_minutes=int(time_stop_minutes),
        min_edge_to_cost=min_edge_to_cost,
        min_net_edge_bps=min_net_edge_bps,
        min_liquidity_score=min_liquidity_score,
        max_round_trip_cost_bps=max_round_trip_cost_bps,
    )
    _write_decision_journal(decision_output, build_result.decisions)

    paper_report_json = _sidecar_report_path(report_json_output, suffix="_paper_engine")
    paper_report_md = _sidecar_report_path(report_md_output, suffix="_paper_engine")
    paper_result = run_trident_ai_paper_replay(
        decision_output,
        config=active_config,
        journal_path=paper_journal_output,
        report_json_path=paper_report_json,
        report_md_path=paper_report_md,
        market_input_path=market_input_path,
        symbols=symbols_filter,
    )

    result = TridentAICandidatePaperReplayResult(
        candidate_input_path=str(candidate_input_path),
        decision_journal_path=str(decision_output),
        paper_journal_path=str(paper_journal_output),
        report_json_path=str(report_json_output),
        report_md_path=str(report_md_output),
        market_input_path=str(market_input_path),
        symbols_filter=symbols_filter,
        candidates_seen=build_result.candidates_seen,
        decisions_written=len(build_result.decisions),
        skipped_candidates=build_result.skipped_candidates,
        max_candidates=max_candidates,
        requested_notional_usd=notional_usd,
        effective_notional_usd=effective_notional,
        confidence=effective_confidence,
        stop_bps=float(stop_bps),
        take_profit_bps=float(take_profit_bps),
        time_stop_minutes=int(time_stop_minutes),
        min_edge_to_cost=min_edge_to_cost,
        min_net_edge_bps=min_net_edge_bps,
        min_liquidity_score=min_liquidity_score,
        max_round_trip_cost_bps=max_round_trip_cost_bps,
        first_timestamp=build_result.first_timestamp,
        last_timestamp=build_result.last_timestamp,
        symbol_counts=dict(build_result.symbol_counts),
        side_counts=dict(build_result.side_counts),
        skip_reasons=dict(build_result.skip_reasons),
        paper_result=paper_result,
    )
    payload = build_candidate_paper_replay_report_payload(
        result=result,
        generated_at=_format_timestamp(datetime.now(timezone.utc)),
    )
    _write_report_outputs(payload, json_path=report_json_output, md_path=report_md_output)
    return result


def build_candidate_paper_replay_report_payload(
    *,
    result: TridentAICandidatePaperReplayResult,
    generated_at: str,
) -> dict[str, object]:
    return {
        "generated_at": generated_at,
        "kind": "trident_ai_candidate_paper_replay",
        "result": result.to_dict(),
    }


def _build_synthetic_decisions(
    input_path: str | Path,
    *,
    config: TridentAIConfig,
    max_candidates: int | None,
    symbols_filter: tuple[str, ...],
    notional_usd: float,
    max_leverage: float,
    confidence: float,
    stop_bps: float,
    take_profit_bps: float,
    time_stop_minutes: int,
    min_edge_to_cost: float | None,
    min_net_edge_bps: float | None,
    min_liquidity_score: float | None,
    max_round_trip_cost_bps: float | None,
) -> _SyntheticDecisionBuildResult:
    loader = SnapshotLoader()
    feature_builder = TridentAIFeatureBuilder(
        AgentMarketContextBuildConfig.from_trident_ai_config(config)
    )
    allowed = set(symbols_filter)
    result = _SyntheticDecisionBuildResult()

    for record in loader.iter_merged_jsonl(input_path):
        timestamp = _record_timestamp(record)
        regime = _record_regime(record)
        for symbol_payload in record.symbols:
            if max_candidates is not None and len(result.decisions) >= max_candidates:
                return _sort_synthetic_build_result(result)
            symbol = _payload_symbol(symbol_payload)
            if allowed and symbol not in allowed:
                continue
            hint = symbol_payload.get(CANDIDATE_HINT_FIELD)
            if not isinstance(hint, Mapping):
                result.skipped_candidates += 1
                result.skip_reasons["missing_candidate_hint"] += 1
                continue
            result.candidates_seen += 1
            gate_rejection = _candidate_gate_rejection(
                hint,
                min_edge_to_cost=min_edge_to_cost,
                min_net_edge_bps=min_net_edge_bps,
                min_liquidity_score=min_liquidity_score,
                max_round_trip_cost_bps=max_round_trip_cost_bps,
            )
            if gate_rejection:
                result.skipped_candidates += 1
                result.skip_reasons[gate_rejection] += 1
                continue
            context_result = feature_builder.build_context_from_mapping(
                symbol_payload,
                as_of=timestamp,
                regime=regime,
                now=_parse_timestamp(timestamp),
            )
            if context_result.context is None:
                result.skipped_candidates += 1
                result.skip_reasons[context_result.reason] += 1
                continue
            side = _hint_side(hint)
            if side not in {"long", "short"}:
                result.skipped_candidates += 1
                result.skip_reasons["invalid_candidate_side"] += 1
                continue
            decision = _synthetic_decision_record(
                context=context_result.context,
                hint=hint,
                record_index=record.record_index,
                side=side,
                notional_usd=notional_usd,
                max_leverage=max_leverage,
                confidence=confidence,
                stop_bps=stop_bps,
                take_profit_bps=take_profit_bps,
                time_stop_minutes=time_stop_minutes,
            )
            result.decisions.append(decision)
            result.first_timestamp = result.first_timestamp or context_result.context.as_of
            result.last_timestamp = context_result.context.as_of
            result.symbol_counts[context_result.context.symbol] += 1
            result.side_counts[side] += 1

    return _sort_synthetic_build_result(result)


def _synthetic_decision_record(
    *,
    context: AgentMarketContext,
    hint: Mapping[str, object],
    record_index: int,
    side: str,
    notional_usd: float,
    max_leverage: float,
    confidence: float,
    stop_bps: float,
    take_profit_bps: float,
    time_stop_minutes: int,
) -> dict[str, object]:
    timestamp = _parse_timestamp(context.as_of) or datetime.now(timezone.utc)
    decision_suffix = f"{context.symbol}_{_timestamp_id(timestamp)}"
    decision_id = f"candidate_open_{decision_suffix}"
    valid_until = _format_timestamp(timestamp + timedelta(minutes=5))
    invalidation_price = _invalidation_price(
        price=context.price,
        side=side,
        stop_bps=stop_bps,
    )
    proposal = {
        "schema_version": TRIDENT_AI_PROPOSAL_SCHEMA_VERSION,
        "decision_id": decision_id,
        "as_of": context.as_of,
        "valid_until": valid_until,
        "action": "open",
        "symbol": context.symbol,
        "side": side,
        "confidence": round(confidence, 6),
        "time_horizon_minutes": int(time_stop_minutes),
        "max_notional_usd": round(notional_usd, 6),
        "max_leverage": round(max_leverage, 6),
        "entry_style": "ioc",
        "invalidation_price": invalidation_price,
        "stop_bps": round(stop_bps, 6),
        "take_profit_bps": round(take_profit_bps, 6),
        "time_stop_minutes": int(time_stop_minutes),
        "rationale_tags": _rationale_tags(hint),
        "evidence_ids": _evidence_ids(context, hint),
        "risk_notes": _risk_notes(hint),
    }
    return {
        "event_type": LLM_REPLAY_DECISION_EVENT,
        "source": CANDIDATE_PAPER_DECISION_SOURCE,
        "record_index": record_index,
        "timestamp": context.as_of,
        "symbol": context.symbol,
        "request": {
            "request_id": f"candidate_request_{decision_suffix}",
            "prompt_version": "candidate_paper_replay_v1",
        },
        "context": _context_with_hint(context, hint),
        "llm_response": {
            "ok": True,
            "provider": "local_candidate_replay",
            "model": "deterministic_candidate_open",
            "usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "estimated_cost_usd": 0.0,
            },
        },
        "proposal": proposal,
        "validation": {"accepted": True, "reason": "synthetic_candidate_open"},
    }


def _context_with_hint(
    context: AgentMarketContext,
    hint: Mapping[str, object],
) -> dict[str, object]:
    payload = context.to_dict()
    payload["schema_version"] = TRIDENT_AI_MARKET_CONTEXT_SCHEMA_VERSION
    payload[CANDIDATE_HINT_FIELD] = dict(hint)
    payload["source"] = CANDIDATE_PAPER_DECISION_SOURCE
    return payload


def _rationale_tags(hint: Mapping[str, object]) -> list[str]:
    tags = ["candidate_paper_replay"]
    for item in hint.get("reasons", []):
        if isinstance(item, str) and item.strip():
            tags.append(item.strip())
        if len(tags) >= 8:
            break
    return tags


def _evidence_ids(context: AgentMarketContext, hint: Mapping[str, object]) -> list[str]:
    evidence = [context.context_id]
    hint_context_id = hint.get("context_id")
    if isinstance(hint_context_id, str) and hint_context_id.strip() and hint_context_id not in evidence:
        evidence.append(hint_context_id.strip())
    return evidence


def _risk_notes(hint: Mapping[str, object]) -> list[str]:
    notes = ["synthetic_open_from_local_candidate_scan"]
    for field_name in ("score", "estimated_net_edge_bps", "edge_to_cost_ratio"):
        value = hint.get(field_name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        notes.append(f"{field_name}={float(value):.4f}")
    return notes


def _candidate_gate_rejection(
    hint: Mapping[str, object],
    *,
    min_edge_to_cost: float | None,
    min_net_edge_bps: float | None,
    min_liquidity_score: float | None,
    max_round_trip_cost_bps: float | None,
) -> str:
    if min_edge_to_cost is not None:
        value = _number(hint.get("edge_to_cost_ratio"))
        if value is None or value < min_edge_to_cost:
            return "edge_to_cost_below_gate"
    if min_net_edge_bps is not None:
        value = _number(hint.get("estimated_net_edge_bps"))
        if value is None or value < min_net_edge_bps:
            return "net_edge_bps_below_gate"
    if min_liquidity_score is not None:
        value = _number(hint.get("liquidity_score"))
        if value is None or value < min_liquidity_score:
            return "liquidity_score_below_gate"
    if max_round_trip_cost_bps is not None:
        value = _number(hint.get("round_trip_cost_bps"))
        if value is None or value > max_round_trip_cost_bps:
            return "round_trip_cost_above_gate"
    return ""


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _round_optional(value: float | None) -> float | None:
    return round(float(value), 6) if value is not None else None


def _invalidation_price(
    *,
    price: float,
    side: str,
    stop_bps: float,
) -> float:
    catastrophic_bps = stop_bps * 1.5
    if side == "long":
        return round(price * (1.0 - catastrophic_bps / 10_000.0), 8)
    return round(price * (1.0 + catastrophic_bps / 10_000.0), 8)


def _effective_notional_usd(*, requested: float | None, cap: float) -> float:
    if cap <= 0.0:
        raise ValueError("config_live_max_order_notional_usd_must_be_positive")
    if requested is None:
        return float(cap)
    return min(float(requested), float(cap))


def _write_decision_journal(path: Path, decisions: Sequence[Mapping[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(decision, sort_keys=True) for decision in decisions]
    path.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")


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


def _render_markdown_report(payload: Mapping[str, object]) -> str:
    result = payload["result"]
    assert isinstance(result, Mapping)
    paper = result.get("paper_result", {})
    assert isinstance(paper, Mapping)
    lines = [
        "# TRIDENT-AI Candidate Paper Replay",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Candidate input: `{result['candidate_input_path']}`",
        f"- Market input: `{result['market_input_path']}`",
        f"- Decision journal: `{result['decision_journal_path']}`",
        f"- Paper journal: `{result['paper_journal_path']}`",
        f"- Symbols filter: `{result['symbols_filter']}`",
        f"- Candidates seen: `{result['candidates_seen']}`",
        f"- Synthetic decisions written: `{result['decisions_written']}`",
        f"- Skipped candidates: `{result['skipped_candidates']}`",
        f"- Effective notional: `${result['effective_notional_usd']:.6f}`",
        f"- Stop / TP / time stop: `{result['stop_bps']}` bps / `{result['take_profit_bps']}` bps / `{result['time_stop_minutes']}` min",
        "- Candidate gates: "
        f"edge/cost `>={result.get('min_edge_to_cost')}`, "
        f"net edge `>={result.get('min_net_edge_bps')}` bps, "
        f"liquidity `>={result.get('min_liquidity_score')}`, "
        f"round-trip cost `<={result.get('max_round_trip_cost_bps')}` bps",
        "",
        "## Paper Result",
        "",
        f"- Positions opened/closed: `{paper.get('positions_opened', 0)}` / `{paper.get('positions_closed', 0)}`",
        f"- Open positions after replay: `{paper.get('open_positions', 0)}`",
        f"- Realized PnL: `${float(paper.get('realized_pnl_usd', 0.0)):.6f}`",
        f"- Fees: `${float(paper.get('fees_usd', 0.0)):.6f}`",
        f"- AI cost estimate: `${float(paper.get('ai_cost_usd', 0.0)):.8f}`",
        f"- Net after AI cost: `${float(paper.get('net_after_ai_cost_usd', 0.0)):.8f}`",
        "",
        "## Close Reasons",
        "",
        "| Reason | Count |",
        "|---|---:|",
    ]
    close_reasons = paper.get("close_reasons", {})
    if isinstance(close_reasons, Mapping) and close_reasons:
        for reason, count in close_reasons.items():
            lines.append(f"| {reason} | {count} |")
    else:
        lines.append("| none | 0 |")
    lines.extend(["", "## Candidate Skips", "", "| Reason | Count |", "|---|---:|"])
    skip_reasons = result.get("skip_reasons", {})
    if isinstance(skip_reasons, Mapping) and skip_reasons:
        for reason, count in skip_reasons.items():
            lines.append(f"| {reason} | {count} |")
    else:
        lines.append("| none | 0 |")
    lines.append("")
    return "\n".join(lines)


def _sort_synthetic_build_result(
    result: _SyntheticDecisionBuildResult,
) -> _SyntheticDecisionBuildResult:
    result.decisions.sort(key=_decision_sort_key)
    if result.decisions:
        result.first_timestamp = str(result.decisions[0].get("timestamp", "") or "")
        result.last_timestamp = str(result.decisions[-1].get("timestamp", "") or "")
    return result


def _decision_sort_key(record: Mapping[str, object]) -> tuple[str, str]:
    return (
        str(record.get("timestamp", "") or ""),
        str(record.get("symbol", "") or ""),
    )


def _sidecar_report_path(path: Path, *, suffix: str) -> Path:
    return path.with_name(f"{path.stem}{suffix}{path.suffix}")


def _symbols_filter(symbols: Sequence[str] | None) -> tuple[str, ...]:
    if not symbols:
        return ()
    normalized: list[str] = []
    for symbol in symbols:
        value = str(symbol).strip().upper()
        if value and value not in normalized:
            normalized.append(value)
    return tuple(normalized)


def _payload_symbol(payload: Mapping[str, object]) -> str:
    value = payload.get("symbol", "")
    return str(value).strip().upper() if isinstance(value, str) else ""


def _hint_side(hint: Mapping[str, object]) -> str:
    value = hint.get("side", "")
    return str(value).strip().lower() if isinstance(value, str) else ""


def _record_regime(record: SnapshotRecord) -> str:
    for field_name in ("regime", "effective_regime", "regime_label"):
        value = record.regime_snapshot.get(field_name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def _record_timestamp(record: SnapshotRecord) -> str:
    if record.timestamp:
        return _format_timestamp(_parse_timestamp(record.timestamp) or datetime.now(timezone.utc))
    return _format_timestamp(datetime.now(timezone.utc))


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
