from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.backtest.snapshot_loader import SnapshotLoader, SnapshotRecord
from app.persistence.journal import JsonlJournal
from app.trident_ai.config import TridentAIConfig, TridentAIPaperConfig, load_trident_ai_config
from app.trident_ai.features import AgentMarketContextBuildConfig, TridentAIFeatureBuilder
from app.trident_ai.market_regime import build_market_micro_regime
from app.trident_ai.types import AgentMarketContext


CANDIDATE_SCAN_EVENT = "trident_ai_candidate_scan"
CANDIDATE_HINT_FIELD = "trident_ai_candidate"
CANDIDATE_HINT_SCHEMA_VERSION = "trident_ai_candidate_hint_v7"
DEFAULT_MIN_EDGE_TO_COST_RATIO = 1.5
DEFAULT_MIN_NET_EDGE_BPS = 5.0
DEFAULT_MICROPRICE_CONFLICT_BPS = 0.25
DEFAULT_PATTERN_PROFILE = "none"
RESEARCH_PATTERN_PROFILE = "research_v1"
STABLE_PATTERN_PROFILE = "research_v2_stable"
ALLOWED_PATTERN_PROFILES = {
    DEFAULT_PATTERN_PROFILE,
    RESEARCH_PATTERN_PROFILE,
    STABLE_PATTERN_PROFILE,
}
FOLD_UNSTABLE_PATTERN_KEYS = {
    "microprice=aligned|flow_book=flow_aligned_book_neutral|vwap=aligned|edge=3.0-4.0",
    "microprice=aligned|flow_book=flow_and_book_aligned|vwap=aligned|edge=3.0-4.0",
    "microprice=aligned|flow_book=flow_and_book_aligned|vwap=aligned|edge=>=4.0",
}


@dataclass(frozen=True, slots=True)
class TridentAICandidateScore:
    timestamp: str
    record_index: int
    symbol: str
    side: str
    score: float
    raw_score: float
    directional_score: float
    liquidity_score: float
    activity_score: float
    cost_score: float
    edge_quality_score: float
    pattern_quality_score: float
    pattern_profile: str
    estimated_edge_bps: float
    round_trip_cost_bps: float
    estimated_net_edge_bps: float
    edge_to_cost_ratio: float
    price: float
    spread_bps: float
    context_id: str
    reasons: tuple[str, ...] = ()
    pattern_reasons: tuple[str, ...] = ()
    context: dict[str, object] = field(default_factory=dict)
    source_snapshot: dict[str, object] = field(default_factory=dict)
    replay_record: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "timestamp": self.timestamp,
            "record_index": self.record_index,
            "symbol": self.symbol,
            "side": self.side,
            "score": self.score,
            "raw_score": self.raw_score,
            "directional_score": self.directional_score,
            "liquidity_score": self.liquidity_score,
            "activity_score": self.activity_score,
            "cost_score": self.cost_score,
            "edge_quality_score": self.edge_quality_score,
            "pattern_quality_score": self.pattern_quality_score,
            "pattern_profile": self.pattern_profile,
            "estimated_edge_bps": self.estimated_edge_bps,
            "round_trip_cost_bps": self.round_trip_cost_bps,
            "estimated_net_edge_bps": self.estimated_net_edge_bps,
            "edge_to_cost_ratio": self.edge_to_cost_ratio,
            "price": self.price,
            "spread_bps": self.spread_bps,
            "context_id": self.context_id,
            "reasons": list(self.reasons),
            "pattern_reasons": list(self.pattern_reasons),
            "context": dict(self.context),
        }


@dataclass(frozen=True, slots=True)
class TridentAICandidateScanResult:
    input_path: str
    journal_path: str
    report_json_path: str
    report_md_path: str
    selected_input_path: str
    records_processed: int = 0
    contexts_scored: int = 0
    contexts_rejected: int = 0
    candidate_rejections: int = 0
    candidates_selected: int = 0
    min_score: float = 0.0
    min_edge_to_cost: float = DEFAULT_MIN_EDGE_TO_COST_RATIO
    min_net_edge_bps: float = DEFAULT_MIN_NET_EDGE_BPS
    allow_microprice_conflict: bool = False
    require_microprice_alignment: bool = False
    microprice_conflict_bps: float = DEFAULT_MICROPRICE_CONFLICT_BPS
    pattern_profile: str = DEFAULT_PATTERN_PROFILE
    top_n: int = 0
    max_records: int | None = None
    max_contexts: int | None = None
    start_timestamp: str | None = None
    end_timestamp: str | None = None
    limit_reached: bool = False
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    side_counts: dict[str, int] = field(default_factory=dict)
    symbol_counts: dict[str, int] = field(default_factory=dict)
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    top_candidates: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "input_path": self.input_path,
            "journal_path": self.journal_path,
            "report_json_path": self.report_json_path,
            "report_md_path": self.report_md_path,
            "selected_input_path": self.selected_input_path,
            "records_processed": self.records_processed,
            "contexts_scored": self.contexts_scored,
            "contexts_rejected": self.contexts_rejected,
            "candidate_rejections": self.candidate_rejections,
            "candidates_selected": self.candidates_selected,
            "min_score": self.min_score,
            "min_edge_to_cost": self.min_edge_to_cost,
            "min_net_edge_bps": self.min_net_edge_bps,
            "allow_microprice_conflict": self.allow_microprice_conflict,
            "require_microprice_alignment": self.require_microprice_alignment,
            "microprice_conflict_bps": self.microprice_conflict_bps,
            "pattern_profile": self.pattern_profile,
            "top_n": self.top_n,
            "max_records": self.max_records,
            "max_contexts": self.max_contexts,
            "start_timestamp": self.start_timestamp,
            "end_timestamp": self.end_timestamp,
            "limit_reached": self.limit_reached,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
            "side_counts": dict(sorted(self.side_counts.items())),
            "symbol_counts": dict(sorted(self.symbol_counts.items())),
            "rejection_reasons": dict(sorted(self.rejection_reasons.items())),
            "top_candidates": self.top_candidates,
        }


@dataclass(slots=True)
class _CandidateScanCounters:
    records_processed: int = 0
    contexts_scored: int = 0
    contexts_rejected: int = 0
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    rejection_reasons: Counter[str] = field(default_factory=Counter)
    limit_reached: bool = False


@dataclass(frozen=True, slots=True)
class _CandidateCostMetrics:
    cost_score: float
    estimated_edge_bps: float
    round_trip_cost_bps: float
    estimated_net_edge_bps: float
    edge_to_cost_ratio: float


@dataclass(frozen=True, slots=True)
class _CandidatePatternQuality:
    score: float
    reasons: tuple[str, ...] = ()


class TridentAICandidateScanner:
    def __init__(
        self,
        *,
        config: TridentAIConfig | None = None,
        loader: SnapshotLoader | None = None,
        feature_builder: TridentAIFeatureBuilder | None = None,
    ) -> None:
        self.config = config or load_trident_ai_config()
        self.loader = loader or SnapshotLoader()
        self.feature_builder = feature_builder or TridentAIFeatureBuilder(
            AgentMarketContextBuildConfig.from_trident_ai_config(self.config)
        )

    def run(
        self,
        input_path: str | Path,
        *,
        journal_path: str | Path | None = None,
        report_json_path: str | Path | None = None,
        report_md_path: str | Path | None = None,
        selected_input_path: str | Path | None = None,
        max_records: int | None = None,
        max_contexts: int | None = None,
        start_timestamp: str | None = None,
        end_timestamp: str | None = None,
        top_n: int = 40,
        min_score: float = 1.25,
        min_edge_to_cost: float = DEFAULT_MIN_EDGE_TO_COST_RATIO,
        min_net_edge_bps: float = DEFAULT_MIN_NET_EDGE_BPS,
        allow_microprice_conflict: bool = False,
        require_microprice_alignment: bool = False,
        microprice_conflict_bps: float = DEFAULT_MICROPRICE_CONFLICT_BPS,
        pattern_profile: str = DEFAULT_PATTERN_PROFILE,
        symbols: Sequence[str] | None = None,
    ) -> TridentAICandidateScanResult:
        if top_n <= 0:
            raise ValueError("top_n_must_be_positive")
        if max_records is not None and max_records <= 0:
            raise ValueError("max_records_must_be_positive")
        if max_contexts is not None and max_contexts <= 0:
            raise ValueError("max_contexts_must_be_positive")
        if min_edge_to_cost < 0.0:
            raise ValueError("min_edge_to_cost_must_be_non_negative")
        if min_net_edge_bps < 0.0:
            raise ValueError("min_net_edge_bps_must_be_non_negative")
        if microprice_conflict_bps < 0.0:
            raise ValueError("microprice_conflict_bps_must_be_non_negative")
        window_start = _parse_optional_window_timestamp(
            start_timestamp,
            field_name="start_timestamp",
        )
        window_end = _parse_optional_window_timestamp(
            end_timestamp,
            field_name="end_timestamp",
        )
        if window_start is not None and window_end is not None and window_start >= window_end:
            raise ValueError("start_timestamp_must_be_before_end_timestamp")
        normalized_pattern_profile = _normalize_pattern_profile(pattern_profile)
        symbols_filter = _symbols_filter(symbols)
        run_id = _timestamp_id(datetime.now(timezone.utc))
        output_dir = Path(self.config.paths.replay_output_dir)
        journal_output = Path(journal_path or output_dir / f"trident_ai_candidate_scan_{run_id}.jsonl")
        report_json_output = Path(
            report_json_path or output_dir / f"trident_ai_candidate_scan_{run_id}.json"
        )
        report_md_output = Path(
            report_md_path or output_dir / f"trident_ai_candidate_scan_{run_id}.md"
        )
        selected_input_output = Path(
            selected_input_path or output_dir / f"trident_ai_candidate_input_{run_id}.jsonl"
        )
        journal = JsonlJournal(journal_output, truncate=True)
        counters = _CandidateScanCounters()
        scored: list[TridentAICandidateScore] = []

        for record in self.loader.iter_merged_jsonl(input_path):
            timestamp = _record_timestamp(record)
            record_time = _parse_timestamp(timestamp)
            if window_start is not None and record_time is not None and record_time < window_start:
                continue
            if window_end is not None and record_time is not None and record_time >= window_end:
                break
            if max_records is not None and counters.records_processed >= max_records:
                break
            counters.records_processed += 1
            counters.first_timestamp = counters.first_timestamp or timestamp
            counters.last_timestamp = timestamp
            regime = _record_regime(record)
            symbol_payloads = _filter_symbol_payloads(record.symbols, symbols_filter)
            for build_result in self.feature_builder.build_contexts_from_mappings(
                symbol_payloads,
                as_of=timestamp,
                regime=regime,
                now=_parse_timestamp(timestamp),
            ):
                if max_contexts is not None and counters.contexts_scored >= max_contexts:
                    counters.limit_reached = True
                    break
                if build_result.context is None:
                    counters.contexts_rejected += 1
                    counters.rejection_reasons[build_result.reason] += 1
                    continue
                payload = _symbol_payload_for_context(symbol_payloads, build_result.context)
                score = score_market_context(
                    build_result.context,
                    record=record,
                    source_snapshot=payload,
                    paper_config=self.config.paper,
                    pattern_profile=normalized_pattern_profile,
                )
                counters.contexts_scored += 1
                scored.append(score)
                journal.append(
                    {
                        "event_type": CANDIDATE_SCAN_EVENT,
                        "source": "trident_ai_candidate_scan",
                        "candidate": score.to_dict(),
                    }
                )
            if counters.limit_reached:
                break

        deduped = _dedupe_candidates(scored)
        eligible: list[TridentAICandidateScore] = []
        candidate_rejections: Counter[str] = Counter()
        for candidate in sorted(deduped, key=_candidate_sort_key):
            rejection_reason = _candidate_gate_rejection_reason(
                candidate,
                min_score=min_score,
                min_edge_to_cost=min_edge_to_cost,
                min_net_edge_bps=min_net_edge_bps,
                allow_microprice_conflict=allow_microprice_conflict,
                require_microprice_alignment=require_microprice_alignment,
                microprice_conflict_bps=microprice_conflict_bps,
            )
            if rejection_reason is not None:
                candidate_rejections[rejection_reason] += 1
                continue
            eligible.append(candidate)
        selected = eligible[:top_n]
        rejection_reasons = Counter(counters.rejection_reasons)
        rejection_reasons.update(candidate_rejections)
        _write_selected_input(selected_input_output, selected)
        result = TridentAICandidateScanResult(
            input_path=str(input_path),
            journal_path=str(journal_output),
            report_json_path=str(report_json_output),
            report_md_path=str(report_md_output),
            selected_input_path=str(selected_input_output),
            records_processed=counters.records_processed,
            contexts_scored=counters.contexts_scored,
            contexts_rejected=counters.contexts_rejected,
            candidate_rejections=sum(candidate_rejections.values()),
            candidates_selected=len(selected),
            min_score=float(min_score),
            min_edge_to_cost=float(min_edge_to_cost),
            min_net_edge_bps=float(min_net_edge_bps),
            allow_microprice_conflict=bool(allow_microprice_conflict),
            require_microprice_alignment=bool(require_microprice_alignment),
            microprice_conflict_bps=float(microprice_conflict_bps),
            pattern_profile=normalized_pattern_profile,
            top_n=int(top_n),
            max_records=max_records,
            max_contexts=max_contexts,
            start_timestamp=_format_timestamp(window_start) if window_start is not None else None,
            end_timestamp=_format_timestamp(window_end) if window_end is not None else None,
            limit_reached=counters.limit_reached,
            first_timestamp=counters.first_timestamp,
            last_timestamp=counters.last_timestamp,
            side_counts=dict(Counter(candidate.side for candidate in selected)),
            symbol_counts=dict(Counter(candidate.symbol for candidate in selected)),
            rejection_reasons=dict(rejection_reasons),
            top_candidates=[candidate.to_dict() for candidate in selected[:20]],
        )
        payload = build_candidate_scan_report_payload(
            result=result,
            generated_at=_format_timestamp(datetime.now(timezone.utc)),
        )
        _write_report_outputs(payload, json_path=report_json_output, md_path=report_md_output)
        return result


def run_trident_ai_candidate_scan(
    input_path: str | Path,
    *,
    config: TridentAIConfig | None = None,
    journal_path: str | Path | None = None,
    report_json_path: str | Path | None = None,
    report_md_path: str | Path | None = None,
    selected_input_path: str | Path | None = None,
    max_records: int | None = None,
    max_contexts: int | None = None,
    start_timestamp: str | None = None,
    end_timestamp: str | None = None,
    top_n: int = 40,
    min_score: float = 1.25,
    min_edge_to_cost: float = DEFAULT_MIN_EDGE_TO_COST_RATIO,
    min_net_edge_bps: float = DEFAULT_MIN_NET_EDGE_BPS,
    allow_microprice_conflict: bool = False,
    require_microprice_alignment: bool = False,
    microprice_conflict_bps: float = DEFAULT_MICROPRICE_CONFLICT_BPS,
    pattern_profile: str = DEFAULT_PATTERN_PROFILE,
    symbols: Sequence[str] | None = None,
) -> TridentAICandidateScanResult:
    return TridentAICandidateScanner(config=config).run(
        input_path,
        journal_path=journal_path,
        report_json_path=report_json_path,
        report_md_path=report_md_path,
        selected_input_path=selected_input_path,
        max_records=max_records,
        max_contexts=max_contexts,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        top_n=top_n,
        min_score=min_score,
        min_edge_to_cost=min_edge_to_cost,
        min_net_edge_bps=min_net_edge_bps,
        allow_microprice_conflict=allow_microprice_conflict,
        require_microprice_alignment=require_microprice_alignment,
        microprice_conflict_bps=microprice_conflict_bps,
        pattern_profile=pattern_profile,
        symbols=symbols,
    )


def score_market_context(
    context: AgentMarketContext,
    *,
    record: SnapshotRecord | None = None,
    source_snapshot: Mapping[str, object] | None = None,
    paper_config: TridentAIPaperConfig | None = None,
    pattern_profile: str = DEFAULT_PATTERN_PROFILE,
) -> TridentAICandidateScore:
    features = context.features
    directional = _directional_score(features)
    side = "long" if directional >= 0 else "short"
    liquidity = _liquidity_score(features)
    activity = _activity_score(features)
    raw_score = round(abs(directional) * liquidity * activity, 6)
    cost_metrics = _cost_metrics(features, paper_config=paper_config or TridentAIPaperConfig())
    microprice_score = _microprice_direction_multiplier(features, side)
    edge_quality_score = _edge_quality_score(cost_metrics)
    normalized_pattern_profile = _normalize_pattern_profile(pattern_profile)
    pattern_quality = _pattern_quality_score(
        features,
        side,
        edge_to_cost_ratio=cost_metrics.edge_to_cost_ratio,
        profile=normalized_pattern_profile,
    )
    score = round(
        raw_score
        * cost_metrics.cost_score
        * microprice_score
        * edge_quality_score
        * pattern_quality.score,
        6,
    )
    reasons = _score_reasons(
        features=features,
        side=side,
        directional=directional,
        cost_metrics=cost_metrics,
    )
    candidate_hint = _candidate_hint(
        context=context,
        side=side,
        score=score,
        raw_score=raw_score,
        directional_score=round(directional, 6),
        liquidity_score=round(liquidity, 6),
        activity_score=round(activity, 6),
        cost_score=cost_metrics.cost_score,
        edge_quality_score=edge_quality_score,
        pattern_quality_score=pattern_quality.score,
        pattern_profile=normalized_pattern_profile,
        estimated_edge_bps=cost_metrics.estimated_edge_bps,
        round_trip_cost_bps=cost_metrics.round_trip_cost_bps,
        estimated_net_edge_bps=cost_metrics.estimated_net_edge_bps,
        edge_to_cost_ratio=cost_metrics.edge_to_cost_ratio,
        reasons=reasons,
        pattern_reasons=pattern_quality.reasons,
    )
    replay_record = {}
    if record is not None and source_snapshot is not None:
        replay_record = _selected_replay_record(
            record,
            source_snapshot,
            candidate_hint=candidate_hint,
        )
    return TridentAICandidateScore(
        timestamp=context.as_of,
        record_index=record.record_index if record is not None else 0,
        symbol=context.symbol,
        side=side,
        score=score,
        raw_score=raw_score,
        directional_score=round(directional, 6),
        liquidity_score=round(liquidity, 6),
        activity_score=round(activity, 6),
        cost_score=cost_metrics.cost_score,
        edge_quality_score=edge_quality_score,
        pattern_quality_score=pattern_quality.score,
        pattern_profile=normalized_pattern_profile,
        estimated_edge_bps=cost_metrics.estimated_edge_bps,
        round_trip_cost_bps=cost_metrics.round_trip_cost_bps,
        estimated_net_edge_bps=cost_metrics.estimated_net_edge_bps,
        edge_to_cost_ratio=cost_metrics.edge_to_cost_ratio,
        price=context.price,
        spread_bps=_float_feature(features, "spread_bps"),
        context_id=context.context_id,
        reasons=tuple(reasons),
        pattern_reasons=pattern_quality.reasons,
        context=context.to_dict(),
        source_snapshot=dict(source_snapshot or {}),
        replay_record=replay_record,
    )


def _candidate_hint(
    *,
    context: AgentMarketContext,
    side: str,
    score: float,
    raw_score: float,
    directional_score: float,
    liquidity_score: float,
    activity_score: float,
    cost_score: float,
    edge_quality_score: float,
    pattern_quality_score: float,
    pattern_profile: str,
    estimated_edge_bps: float,
    round_trip_cost_bps: float,
    estimated_net_edge_bps: float,
    edge_to_cost_ratio: float,
    reasons: Sequence[str],
    pattern_reasons: Sequence[str],
) -> dict[str, object]:
    micro_regime = build_market_micro_regime(
        context.features,
        symbol=context.symbol,
        side=side,
    )
    return {
        "schema_version": CANDIDATE_HINT_SCHEMA_VERSION,
        "context_id": context.context_id,
        "timestamp": context.as_of,
        "symbol": context.symbol,
        "side": side,
        "score": score,
        "raw_score": raw_score,
        "directional_score": directional_score,
        "liquidity_score": liquidity_score,
        "activity_score": activity_score,
        "cost_score": cost_score,
        "edge_quality_score": edge_quality_score,
        "pattern_quality_score": pattern_quality_score,
        "pattern_profile": pattern_profile,
        "estimated_edge_bps": estimated_edge_bps,
        "round_trip_cost_bps": round_trip_cost_bps,
        "estimated_net_edge_bps": estimated_net_edge_bps,
        "edge_to_cost_ratio": edge_to_cost_ratio,
        "market_micro_regime": micro_regime,
        "range_vol_regime": micro_regime["range_vol_regime"],
        "flow_regime": micro_regime["flow_regime"],
        "micro_regime": micro_regime["micro_regime"],
        "symbol_range_vol": micro_regime["symbol_range_vol"],
        "symbol_micro_regime": micro_regime["symbol_micro_regime"],
        "reasons": list(reasons[:8]),
        "pattern_reasons": list(pattern_reasons[:8]),
    }


def build_candidate_scan_report_payload(
    *,
    result: TridentAICandidateScanResult,
    generated_at: str,
) -> dict[str, object]:
    return {
        "generated_at": generated_at,
        "kind": "trident_ai_candidate_scan",
        "result": result.to_dict(),
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
        "# TRIDENT-AI Candidate Scan",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Input: `{result['input_path']}`",
        f"- Selected input: `{result['selected_input_path']}`",
        f"- Records processed: `{result['records_processed']}`",
        f"- Contexts scored: `{result['contexts_scored']}`",
        f"- Candidate rejections: `{result['candidate_rejections']}`",
        f"- Candidates selected: `{result['candidates_selected']}`",
        f"- Limit reached: `{result['limit_reached']}`",
        f"- Min score: `{result['min_score']}`",
        f"- Min edge/cost: `{result['min_edge_to_cost']}`",
        f"- Min net edge bps: `{result['min_net_edge_bps']}`",
        f"- Allow microprice conflict: `{result['allow_microprice_conflict']}`",
        f"- Require microprice alignment: `{result['require_microprice_alignment']}`",
        f"- Microprice conflict bps: `{result['microprice_conflict_bps']}`",
        f"- Pattern profile: `{result['pattern_profile']}`",
        f"- Top N: `{result['top_n']}`",
        f"- Window: `{result.get('start_timestamp')}` -> `{result.get('end_timestamp')}`",
        "",
        "## Selection",
        "",
        "| Symbol | Side | Score | Raw | EdgeQ | PatternQ | Cost | Net Edge | Edge/Cost | Timestamp | Reasons |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for candidate in result["top_candidates"]:
        assert isinstance(candidate, dict)
        reasons = ", ".join(str(item) for item in candidate.get("reasons", []))
        lines.append(
            f"| {candidate['symbol']} | {candidate['side']} | {candidate['score']:.4f} | "
            f"{candidate['raw_score']:.4f} | {candidate['edge_quality_score']:.3f} | "
            f"{candidate['pattern_quality_score']:.3f} | "
            f"{candidate['round_trip_cost_bps']:.2f} | "
            f"{candidate['estimated_net_edge_bps']:.2f} | {candidate['edge_to_cost_ratio']:.2f} | "
            f"{candidate['timestamp']} | {reasons} |"
        )
    if not result["top_candidates"]:
        lines.append("| none | n/a | 0.0000 | 0.0000 | 0.000 | 0.000 | 0.00 | 0.00 | 0.00 | n/a | n/a |")
    lines.append("")
    return "\n".join(lines)


def _write_selected_input(path: Path, selected: list[TridentAICandidateScore]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(candidate.replay_record, sort_keys=True)
        for candidate in selected
        if candidate.replay_record
    ]
    path.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")


def _candidate_sort_key(candidate: TridentAICandidateScore) -> tuple[float, float, str]:
    return (-candidate.score, -abs(candidate.directional_score), candidate.context_id)


def _dedupe_candidates(
    candidates: list[TridentAICandidateScore],
) -> list[TridentAICandidateScore]:
    best_by_key: dict[tuple[str, str], TridentAICandidateScore] = {}
    for candidate in candidates:
        key = (candidate.timestamp, candidate.symbol)
        current = best_by_key.get(key)
        if current is None or _candidate_sort_key(candidate) < _candidate_sort_key(current):
            best_by_key[key] = candidate
    return list(best_by_key.values())


def _candidate_gate_rejection_reason(
    candidate: TridentAICandidateScore,
    *,
    min_score: float,
    min_edge_to_cost: float,
    min_net_edge_bps: float,
    allow_microprice_conflict: bool,
    require_microprice_alignment: bool,
    microprice_conflict_bps: float,
) -> str | None:
    features = _candidate_features(candidate)
    if (
        not allow_microprice_conflict
        and _microprice_direction_conflicts(
            features,
            candidate.side,
            threshold_bps=microprice_conflict_bps,
        )
    ):
        return "microprice_direction_conflict"
    if require_microprice_alignment and not _microprice_direction_aligned(
        features,
        candidate.side,
        threshold_bps=microprice_conflict_bps,
    ):
        return "microprice_not_aligned"
    estimated_net_edge = candidate.estimated_edge_bps - candidate.round_trip_cost_bps
    if estimated_net_edge < min_net_edge_bps:
        return "net_edge_below_min"
    if candidate.edge_to_cost_ratio < min_edge_to_cost:
        return "edge_to_cost_below_min"
    if candidate.score < min_score:
        return "score_below_min"
    return None


def _candidate_features(candidate: TridentAICandidateScore) -> Mapping[str, object]:
    features = candidate.context.get("features", {})
    if isinstance(features, Mapping):
        return features
    return {}


def _directional_score(features: Mapping[str, object]) -> float:
    score = 0.0
    ema_alignment = str(features.get("ema_alignment", "")).lower()
    if ema_alignment == "bullish":
        score += 1.0
    elif ema_alignment == "bearish":
        score -= 1.0
    score += 0.8 * _clamp(_float_feature(features, "structure_score"), -1.0, 1.0)
    score += 0.7 * _clamp(_float_feature(features, "trade_flow_bias"), -1.0, 1.0)
    score += 0.6 * _clamp(_float_feature(features, "book_imbalance"), -1.0, 1.0)
    score += 0.5 * _clamp(_float_feature(features, "microprice_dislocation_bps") / 5.0, -1.0, 1.0)
    score += 0.4 * _clamp(_float_feature(features, "vwap_distance_bps") / 20.0, -1.0, 1.0)
    score += 0.4 * _clamp(_float_feature(features, "external_alignment_score"), -1.0, 1.0)
    if bool(features.get("btc_aligned", False)):
        score += 0.2 if score >= 0 else -0.2
    return score


def _liquidity_score(features: Mapping[str, object]) -> float:
    spread = max(_float_feature(features, "spread_bps"), 0.0)
    spread_score = _clamp(1.35 - spread / 10.0, 0.25, 1.35)
    notional = max(_float_feature(features, "bucket_notional_usd"), 0.0)
    notional_score = _clamp(notional / 5_000.0, 0.35, 1.35)
    trades = max(_float_feature(features, "bucket_trade_count"), 0.0)
    trade_score = _clamp(trades / 10.0, 0.35, 1.25)
    return (spread_score + notional_score + trade_score) / 3.0


def _activity_score(features: Mapping[str, object]) -> float:
    volume_ratio = max(_float_feature(features, "volume_ratio"), 0.0)
    trade_count_ratio = max(_float_feature(features, "trade_count_ratio"), 0.0)
    compression = _clamp(_float_feature(features, "compression_score"), 0.0, 1.0)
    volatility = max(_float_feature(features, "realized_vol_short_bps"), 0.0)
    volume_score = _clamp(volume_ratio / 4.0, 0.5, 1.35)
    count_score = _clamp(trade_count_ratio / 2.0, 0.5, 1.25)
    compression_score = 0.8 + 0.4 * compression
    volatility_score = _clamp(volatility / 8.0, 0.6, 1.25)
    return (volume_score + count_score + compression_score + volatility_score) / 4.0


def _score_reasons(
    *,
    features: Mapping[str, object],
    side: str,
    directional: float,
    cost_metrics: _CandidateCostMetrics,
) -> list[str]:
    reasons: list[str] = [f"{side}_directional_score"]
    ema_alignment = str(features.get("ema_alignment", "")).lower()
    if ema_alignment in {"bullish", "bearish"}:
        reasons.append(f"ema_{ema_alignment}")
    if abs(_float_feature(features, "trade_flow_bias")) >= 0.25:
        reasons.append("trade_flow_bias")
    if abs(_float_feature(features, "book_imbalance")) >= 0.35:
        reasons.append("book_imbalance")
    if abs(_float_feature(features, "microprice_dislocation_bps")) >= 1.0:
        reasons.append("microprice_dislocation")
    if _microprice_direction_conflicts(
        features,
        side,
        threshold_bps=DEFAULT_MICROPRICE_CONFLICT_BPS,
    ):
        reasons.append("microprice_conflict")
    elif abs(_float_feature(features, "microprice_dislocation_bps")) >= DEFAULT_MICROPRICE_CONFLICT_BPS:
        reasons.append("microprice_aligned")
    if _float_feature(features, "spread_bps") <= 3.0:
        reasons.append("spread_ok")
    if _float_feature(features, "bucket_notional_usd") >= 1_000.0:
        reasons.append("liquidity_ok")
    if cost_metrics.round_trip_cost_bps >= 12.0:
        reasons.append("round_trip_cost_high")
    if cost_metrics.edge_to_cost_ratio >= DEFAULT_MIN_EDGE_TO_COST_RATIO:
        reasons.append("cost_edge_ok")
    elif cost_metrics.edge_to_cost_ratio >= 1.25:
        reasons.append("cost_edge_watchlist")
    elif cost_metrics.edge_to_cost_ratio >= 1.0:
        reasons.append("cost_edge_marginal")
    else:
        reasons.append("cost_edge_thin")
    if abs(directional) < 1.0:
        reasons.append("weak_confluence")
    return reasons[:8]


def _cost_metrics(
    features: Mapping[str, object],
    *,
    paper_config: TridentAIPaperConfig,
) -> _CandidateCostMetrics:
    round_trip_cost = _round_trip_cost_bps(features, paper_config=paper_config)
    estimated_edge = _estimated_edge_bps(features)
    estimated_net_edge = estimated_edge - round_trip_cost
    edge_to_cost = estimated_edge / round_trip_cost if round_trip_cost > 0 else 0.0
    cost_score = _cost_efficiency_score(edge_to_cost)
    return _CandidateCostMetrics(
        cost_score=round(cost_score, 6),
        estimated_edge_bps=round(estimated_edge, 6),
        round_trip_cost_bps=round(round_trip_cost, 6),
        estimated_net_edge_bps=round(estimated_net_edge, 6),
        edge_to_cost_ratio=round(edge_to_cost, 6),
    )


def _round_trip_cost_bps(
    features: Mapping[str, object],
    *,
    paper_config: TridentAIPaperConfig,
) -> float:
    spread = max(_float_feature(features, "spread_bps"), 0.0)
    spread_impact = 2.0 * spread * paper_config.spread_multiplier
    slippage = 2.0 * paper_config.slippage_bps
    fees = 2.0 * paper_config.taker_fee_bps
    return spread_impact + slippage + fees


def _estimated_edge_bps(features: Mapping[str, object]) -> float:
    return max(
        0.0,
        1.2 * abs(_float_feature(features, "microprice_dislocation_bps"))
        + 0.4 * abs(_float_feature(features, "vwap_distance_bps"))
        + 6.0 * abs(_float_feature(features, "structure_score"))
        + 4.0 * abs(_float_feature(features, "trade_flow_bias"))
        + 3.0 * abs(_float_feature(features, "book_imbalance"))
        + 1.2 * max(_float_feature(features, "realized_vol_short_bps"), 0.0),
    )


def _cost_efficiency_score(edge_to_cost_ratio: float) -> float:
    if edge_to_cost_ratio <= 0.0:
        return 0.25
    if edge_to_cost_ratio < 1.0:
        return _clamp(edge_to_cost_ratio * edge_to_cost_ratio, 0.25, 1.0)
    return _clamp(edge_to_cost_ratio, 1.0, 1.15)


def _edge_quality_score(cost_metrics: _CandidateCostMetrics) -> float:
    edge_to_cost_component = _clamp(
        (cost_metrics.edge_to_cost_ratio - DEFAULT_MIN_EDGE_TO_COST_RATIO) / 1.5,
        0.0,
        1.0,
    )
    net_edge_component = _clamp(
        (cost_metrics.estimated_net_edge_bps - DEFAULT_MIN_NET_EDGE_BPS) / 20.0,
        0.0,
        1.0,
    )
    quality = 0.85 + 0.25 * (0.45 * edge_to_cost_component + 0.55 * net_edge_component)
    return round(_clamp(quality, 0.85, 1.1), 6)


def _pattern_quality_score(
    features: Mapping[str, object],
    side: str,
    *,
    edge_to_cost_ratio: float,
    profile: str,
) -> _CandidatePatternQuality:
    if profile == DEFAULT_PATTERN_PROFILE:
        return _CandidatePatternQuality(score=1.0, reasons=())
    if profile == STABLE_PATTERN_PROFILE:
        return _stable_pattern_quality_score(
            features,
            side,
            edge_to_cost_ratio=edge_to_cost_ratio,
            profile=profile,
        )
    multiplier = 1.0
    reasons: list[str] = [profile]

    flow_book = _flow_book_alignment(features, side)
    if flow_book == "mixed_conflict":
        multiplier *= 0.75
        reasons.append("penalty_flow_book_mixed_conflict")
    elif flow_book == "flow_and_book_aligned":
        multiplier *= 0.9
        reasons.append("penalty_flow_and_book_aligned_overcrowded")
    elif flow_book == "flow_aligned_book_neutral":
        multiplier *= 1.03
        reasons.append("watchlist_flow_aligned_book_neutral")

    vwap = _signed_feature_alignment(
        features,
        side,
        "vwap_distance_bps",
        threshold=2.0,
    )
    if vwap == "neutral":
        multiplier *= 0.75
        reasons.append("penalty_vwap_neutral")
    elif vwap == "aligned":
        multiplier *= 1.02
        reasons.append("watchlist_vwap_aligned")

    edge_bucket = _edge_bucket(edge_to_cost_ratio)
    if edge_bucket == "3.0-4.0":
        multiplier *= 1.04
        reasons.append("watchlist_edge_bucket_3_4")
    elif edge_bucket == "2.0-3.0":
        multiplier *= 0.9
        reasons.append("penalty_edge_bucket_2_3")
    elif edge_bucket == ">=4.0":
        multiplier *= 0.95
        reasons.append("penalty_edge_bucket_overconfident")

    volatility_bucket = _volatility_bucket(_float_feature(features, "realized_vol_short_bps"))
    if volatility_bucket == "high":
        multiplier *= 0.9
        reasons.append("penalty_high_short_volatility")
    elif volatility_bucket == "low":
        multiplier *= 1.02
        reasons.append("watchlist_low_short_volatility")

    return _CandidatePatternQuality(
        score=round(_clamp(multiplier, 0.45, 1.08), 6),
        reasons=tuple(reasons),
    )


def _stable_pattern_quality_score(
    features: Mapping[str, object],
    side: str,
    *,
    edge_to_cost_ratio: float,
    profile: str,
) -> _CandidatePatternQuality:
    multiplier = 1.0
    reasons: list[str] = [profile, "no_bonus_without_fold_stability"]

    microprice = _signed_feature_alignment(
        features,
        side,
        "microprice_dislocation_bps",
        threshold=DEFAULT_MICROPRICE_CONFLICT_BPS,
    )
    flow_book = _flow_book_alignment(features, side)
    vwap = _signed_feature_alignment(
        features,
        side,
        "vwap_distance_bps",
        threshold=2.0,
    )
    edge_bucket = _edge_bucket(edge_to_cost_ratio)
    pattern_key = "|".join(
        (
            f"microprice={microprice}",
            f"flow_book={flow_book}",
            f"vwap={vwap}",
            f"edge={edge_bucket}",
        )
    )
    if pattern_key in FOLD_UNSTABLE_PATTERN_KEYS:
        multiplier *= 0.7
        reasons.append("penalty_fold_unstable_pattern")

    if flow_book == "mixed_conflict":
        multiplier *= 0.75
        reasons.append("penalty_flow_book_mixed_conflict")
    elif flow_book == "flow_and_book_aligned":
        multiplier *= 0.85
        reasons.append("penalty_flow_and_book_aligned_oos_fragile")

    if vwap == "neutral":
        multiplier *= 0.75
        reasons.append("penalty_vwap_neutral")
    elif vwap == "conflict":
        multiplier *= 0.9
        reasons.append("penalty_vwap_conflict")

    if edge_bucket == "2.0-3.0":
        multiplier *= 0.9
        reasons.append("penalty_edge_bucket_2_3")
    elif edge_bucket == ">=4.0":
        multiplier *= 0.85
        reasons.append("penalty_edge_bucket_overconfident_oos")

    volatility_bucket = _volatility_bucket(_float_feature(features, "realized_vol_short_bps"))
    if volatility_bucket == "high":
        multiplier *= 0.9
        reasons.append("penalty_high_short_volatility")

    return _CandidatePatternQuality(
        score=round(_clamp(multiplier, 0.35, 1.0), 6),
        reasons=tuple(reasons),
    )


def _normalize_pattern_profile(value: str) -> str:
    profile = str(value or DEFAULT_PATTERN_PROFILE).strip().lower()
    if profile not in ALLOWED_PATTERN_PROFILES:
        raise ValueError("invalid_pattern_profile")
    return profile


def _flow_book_alignment(features: Mapping[str, object], side: str) -> str:
    flow = _signed_feature_alignment(features, side, "trade_flow_bias", threshold=0.25)
    book = _signed_feature_alignment(features, side, "book_imbalance", threshold=0.25)
    if flow == "aligned" and book == "aligned":
        return "flow_and_book_aligned"
    if flow == "conflict" and book == "conflict":
        return "flow_and_book_conflict"
    if flow == "aligned" and book in {"neutral", "missing"}:
        return "flow_aligned_book_neutral"
    if book == "aligned" and flow in {"neutral", "missing"}:
        return "book_aligned_flow_neutral"
    if flow == "conflict" or book == "conflict":
        return "mixed_conflict"
    return "neutral"


def _signed_feature_alignment(
    features: Mapping[str, object],
    side: str,
    field_name: str,
    *,
    threshold: float,
) -> str:
    value = features.get(field_name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "missing"
    numeric = float(value)
    if abs(numeric) < threshold:
        return "neutral"
    normalized_side = side.strip().lower()
    if normalized_side == "long":
        return "aligned" if numeric > 0.0 else "conflict"
    if normalized_side == "short":
        return "aligned" if numeric < 0.0 else "conflict"
    return "unknown"


def _edge_bucket(edge_to_cost_ratio: float) -> str:
    if edge_to_cost_ratio >= 4.0:
        return ">=4.0"
    if edge_to_cost_ratio >= 3.0:
        return "3.0-4.0"
    if edge_to_cost_ratio >= 2.0:
        return "2.0-3.0"
    if edge_to_cost_ratio >= 1.5:
        return "1.5-2.0"
    return "<1.5"


def _volatility_bucket(realized_vol_short_bps: float) -> str:
    if realized_vol_short_bps >= 25.0:
        return "high"
    if realized_vol_short_bps >= 12.0:
        return "medium"
    return "low"


def _microprice_direction_multiplier(features: Mapping[str, object], side: str) -> float:
    if not _microprice_direction_conflicts(
        features,
        side,
        threshold_bps=DEFAULT_MICROPRICE_CONFLICT_BPS,
    ):
        return 1.0
    conflict_bps = abs(_float_feature(features, "microprice_dislocation_bps"))
    conflict_strength = _clamp(conflict_bps / 2.0, 0.0, 1.0)
    return round(_clamp(1.0 - 0.55 * conflict_strength, 0.35, 1.0), 6)


def _microprice_direction_conflicts(
    features: Mapping[str, object],
    side: str,
    *,
    threshold_bps: float,
) -> bool:
    dislocation = _float_feature(features, "microprice_dislocation_bps")
    if abs(dislocation) < threshold_bps:
        return False
    normalized_side = str(side).strip().lower()
    if normalized_side == "long":
        return dislocation < 0.0
    if normalized_side == "short":
        return dislocation > 0.0
    return False


def _microprice_direction_aligned(
    features: Mapping[str, object],
    side: str,
    *,
    threshold_bps: float,
) -> bool:
    dislocation = _float_feature(features, "microprice_dislocation_bps")
    if abs(dislocation) < threshold_bps:
        return False
    normalized_side = str(side).strip().lower()
    if normalized_side == "long":
        return dislocation > 0.0
    if normalized_side == "short":
        return dislocation < 0.0
    return False


def _selected_replay_record(
    record: SnapshotRecord,
    symbol_payload: Mapping[str, object],
    *,
    candidate_hint: Mapping[str, object],
) -> dict[str, object]:
    selected_symbol = dict(symbol_payload)
    selected_symbol[CANDIDATE_HINT_FIELD] = dict(candidate_hint)
    payload: dict[str, object] = {
        "timestamp": record.timestamp,
        "regime_snapshot": dict(record.regime_snapshot),
        "symbols": [selected_symbol],
    }
    if record.cluster_regime_snapshots is not None:
        payload["cluster_regime_snapshots"] = dict(record.cluster_regime_snapshots)
    if record.capture_reason is not None:
        payload["capture_reason"] = record.capture_reason
    if record.stream_source is not None:
        payload["stream_source"] = record.stream_source
    return payload


def _symbol_payload_for_context(
    payloads: Sequence[Mapping[str, object]],
    context: AgentMarketContext,
) -> dict[str, object]:
    for payload in payloads:
        if str(payload.get("symbol", "")).strip().upper() == context.symbol:
            return dict(payload)
    return {}


def _filter_symbol_payloads(
    payloads: Sequence[dict[str, object]],
    symbols_filter: tuple[str, ...],
) -> list[dict[str, object]]:
    if not symbols_filter:
        return list(payloads)
    allowed = set(symbols_filter)
    return [
        payload
        for payload in payloads
        if str(payload.get("symbol", "")).strip().upper() in allowed
    ]


def _symbols_filter(symbols: Sequence[str] | None) -> tuple[str, ...]:
    if symbols is None:
        return ()
    normalized: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        value = str(symbol).strip().upper()
        if not value or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    return tuple(normalized)


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


def _float_feature(features: Mapping[str, object], key: str) -> float:
    value = features.get(key, 0.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_optional_window_timestamp(value: str | None, *, field_name: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = _parse_timestamp(text)
    if parsed is None:
        raise ValueError(f"{field_name}_must_be_iso8601")
    return parsed


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _timestamp_id(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
