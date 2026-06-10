from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.trident_ai.candidate_scan import (
    CANDIDATE_HINT_FIELD,
    DEFAULT_MICROPRICE_CONFLICT_BPS,
)
from app.trident_ai.config import TridentAIConfig, load_trident_ai_config
from app.trident_ai.paper import (
    PAPER_REPLAY_DECISION_EVENT,
    PAPER_REPLAY_TRADE_CLOSED_EVENT,
)
from app.trident_ai.replay import LLM_REPLAY_DECISION_EVENT


@dataclass(frozen=True, slots=True)
class TridentAIPatternCalibrationResult:
    decision_journal_paths: tuple[str, ...]
    paper_journal_paths: tuple[str, ...]
    report_json_path: str
    report_md_path: str
    min_trades_per_pattern: int = 3
    decisions_seen: int = 0
    open_decisions: int = 0
    paper_opens: int = 0
    paper_skips: int = 0
    closed_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    realized_pnl_usd: float = 0.0
    gross_pnl_usd: float = 0.0
    fees_usd: float = 0.0
    avg_realized_net_bps: float = 0.0
    sample_warning: str = ""
    risky_patterns: list[dict[str, object]] = field(default_factory=list)
    promising_patterns: list[dict[str, object]] = field(default_factory=list)
    pattern_buckets: list[dict[str, object]] = field(default_factory=list)
    dimension_buckets: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    symbol_diagnostics: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "decision_journal_paths": list(self.decision_journal_paths),
            "paper_journal_paths": list(self.paper_journal_paths),
            "report_json_path": self.report_json_path,
            "report_md_path": self.report_md_path,
            "min_trades_per_pattern": self.min_trades_per_pattern,
            "decisions_seen": self.decisions_seen,
            "open_decisions": self.open_decisions,
            "paper_opens": self.paper_opens,
            "paper_skips": self.paper_skips,
            "closed_trades": self.closed_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "realized_pnl_usd": round(self.realized_pnl_usd, 6),
            "gross_pnl_usd": round(self.gross_pnl_usd, 6),
            "fees_usd": round(self.fees_usd, 6),
            "avg_realized_net_bps": round(self.avg_realized_net_bps, 6),
            "sample_warning": self.sample_warning,
            "risky_patterns": self.risky_patterns,
            "promising_patterns": self.promising_patterns,
            "pattern_buckets": self.pattern_buckets,
            "dimension_buckets": self.dimension_buckets,
            "symbol_diagnostics": self.symbol_diagnostics,
        }


@dataclass(frozen=True, slots=True)
class TridentAIPatternFoldValidationResult:
    decision_journal_paths: tuple[str, ...]
    paper_journal_paths: tuple[str, ...]
    fold_labels: tuple[str, ...]
    report_json_path: str
    report_md_path: str
    min_trades_per_fold: int = 1
    min_positive_folds: int = 2
    max_catastrophic_net_bps: float = 50.0
    folds: list[dict[str, object]] = field(default_factory=list)
    pattern_folds: list[dict[str, object]] = field(default_factory=list)
    stable_positive_patterns: list[dict[str, object]] = field(default_factory=list)
    unstable_patterns: list[dict[str, object]] = field(default_factory=list)
    no_bonus_patterns: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "decision_journal_paths": list(self.decision_journal_paths),
            "paper_journal_paths": list(self.paper_journal_paths),
            "fold_labels": list(self.fold_labels),
            "report_json_path": self.report_json_path,
            "report_md_path": self.report_md_path,
            "min_trades_per_fold": self.min_trades_per_fold,
            "min_positive_folds": self.min_positive_folds,
            "max_catastrophic_net_bps": round(self.max_catastrophic_net_bps, 6),
            "folds": self.folds,
            "pattern_folds": self.pattern_folds,
            "stable_positive_patterns": self.stable_positive_patterns,
            "unstable_patterns": self.unstable_patterns,
            "no_bonus_patterns": self.no_bonus_patterns,
        }


@dataclass(slots=True)
class _PatternDescriptor:
    pattern_key: str
    side: str
    regime: str
    microprice: str
    flow_book: str
    vwap: str
    edge_bucket: str
    net_edge_bucket: str
    volatility_bucket: str
    reasons: tuple[str, ...] = ()

    def dimensions(self) -> dict[str, str]:
        return {
            "side": self.side,
            "regime": self.regime,
            "microprice": self.microprice,
            "flow_book": self.flow_book,
            "vwap": self.vwap,
            "edge_bucket": self.edge_bucket,
            "net_edge_bucket": self.net_edge_bucket,
            "volatility_bucket": self.volatility_bucket,
        }


@dataclass(slots=True)
class _Observation:
    decision_id: str
    timestamp: str
    symbol: str
    descriptor: _PatternDescriptor
    paper_action: str = ""
    paper_reason: str = ""
    close_reason: str = ""
    pnl_usd: float = 0.0
    gross_pnl_usd: float = 0.0
    fees_usd: float = 0.0
    notional_usd: float = 0.0


@dataclass(slots=True)
class _BucketStats:
    decisions: int = 0
    paper_opens: int = 0
    paper_skips: int = 0
    closed_trades: int = 0
    wins: int = 0
    losses: int = 0
    pnl_usd: float = 0.0
    gross_pnl_usd: float = 0.0
    fees_usd: float = 0.0
    notional_usd: float = 0.0
    close_reasons: Counter[str] = field(default_factory=Counter)
    skip_reasons: Counter[str] = field(default_factory=Counter)
    symbols: Counter[str] = field(default_factory=Counter)

    def add(self, observation: _Observation) -> None:
        self.decisions += 1
        if observation.symbol:
            self.symbols[observation.symbol] += 1
        if observation.paper_action == "open":
            self.paper_opens += 1
        elif observation.paper_action == "skip":
            self.paper_skips += 1
            if observation.paper_reason:
                self.skip_reasons[observation.paper_reason] += 1
        if observation.close_reason:
            self.closed_trades += 1
            self.pnl_usd += observation.pnl_usd
            self.gross_pnl_usd += observation.gross_pnl_usd
            self.fees_usd += observation.fees_usd
            self.notional_usd += observation.notional_usd
            self.close_reasons[observation.close_reason] += 1
            if observation.pnl_usd > 0.0:
                self.wins += 1
            elif observation.pnl_usd < 0.0:
                self.losses += 1


def run_trident_ai_pattern_calibration_report(
    *,
    decision_journal_paths: Sequence[str | Path],
    paper_journal_paths: Sequence[str | Path],
    config: TridentAIConfig | None = None,
    report_json_path: str | Path | None = None,
    report_md_path: str | Path | None = None,
    min_trades_per_pattern: int = 3,
) -> TridentAIPatternCalibrationResult:
    if not decision_journal_paths:
        raise ValueError("decision_journal_paths_required")
    if len(decision_journal_paths) != len(paper_journal_paths):
        raise ValueError("decision_and_paper_journal_counts_must_match")
    if min_trades_per_pattern <= 0:
        raise ValueError("min_trades_per_pattern_must_be_positive")

    resolved_config = config or load_trident_ai_config()
    run_id = _timestamp_id(datetime.now(timezone.utc))
    output_dir = Path(resolved_config.paths.replay_output_dir)
    json_output = Path(
        report_json_path or output_dir / f"trident_ai_pattern_calibration_{run_id}.json"
    )
    md_output = Path(
        report_md_path or output_dir / f"trident_ai_pattern_calibration_{run_id}.md"
    )

    observations: list[_Observation] = []
    decisions_seen = 0
    for decision_path, paper_path in zip(decision_journal_paths, paper_journal_paths, strict=True):
        decisions = _open_decisions(decision_path)
        decisions_seen += len(_decision_rows(decision_path))
        paper_decisions = _paper_decisions_by_decision(paper_path)
        trades = _closed_trades_by_decision(paper_path)
        for decision in decisions:
            observations.append(
                _observation_from_records(
                    decision=decision,
                    paper_decision=paper_decisions.get(decision.decision_id),
                    trade=trades.get(decision.decision_id),
                )
            )

    pattern_stats: dict[str, _BucketStats] = defaultdict(_BucketStats)
    dimension_stats: dict[str, dict[str, _BucketStats]] = defaultdict(lambda: defaultdict(_BucketStats))
    symbol_stats: dict[str, _BucketStats] = defaultdict(_BucketStats)
    for observation in observations:
        pattern_stats[observation.descriptor.pattern_key].add(observation)
        symbol_stats[observation.symbol].add(observation)
        for dimension_name, dimension_value in observation.descriptor.dimensions().items():
            dimension_stats[dimension_name][dimension_value].add(observation)

    pattern_buckets = _finalize_pattern_buckets(pattern_stats, min_trades_per_pattern)
    risky_patterns = [
        bucket
        for bucket in pattern_buckets
        if bucket["closed_trades"] >= min_trades_per_pattern and bucket["pnl_usd"] < 0.0
    ]
    promising_patterns = [
        bucket
        for bucket in pattern_buckets
        if bucket["closed_trades"] >= min_trades_per_pattern and bucket["pnl_usd"] > 0.0
    ]
    risky_patterns = sorted(risky_patterns, key=lambda item: (item["pnl_usd"], -item["closed_trades"]))[:10]
    promising_patterns = sorted(
        promising_patterns,
        key=lambda item: (-item["pnl_usd"], -item["closed_trades"]),
    )[:10]

    closed_observations = [item for item in observations if item.close_reason]
    realized_pnl = sum(item.pnl_usd for item in closed_observations)
    gross_pnl = sum(item.gross_pnl_usd for item in closed_observations)
    fees = sum(item.fees_usd for item in closed_observations)
    total_notional = sum(item.notional_usd for item in closed_observations)
    result = TridentAIPatternCalibrationResult(
        decision_journal_paths=tuple(str(path) for path in decision_journal_paths),
        paper_journal_paths=tuple(str(path) for path in paper_journal_paths),
        report_json_path=str(json_output),
        report_md_path=str(md_output),
        min_trades_per_pattern=min_trades_per_pattern,
        decisions_seen=decisions_seen,
        open_decisions=len(observations),
        paper_opens=sum(1 for item in observations if item.paper_action == "open"),
        paper_skips=sum(1 for item in observations if item.paper_action == "skip"),
        closed_trades=len(closed_observations),
        winning_trades=sum(1 for item in closed_observations if item.pnl_usd > 0.0),
        losing_trades=sum(1 for item in closed_observations if item.pnl_usd < 0.0),
        realized_pnl_usd=realized_pnl,
        gross_pnl_usd=gross_pnl,
        fees_usd=fees,
        avg_realized_net_bps=_pnl_bps(realized_pnl, total_notional),
        sample_warning=_sample_warning(len(closed_observations), min_trades_per_pattern),
        risky_patterns=risky_patterns,
        promising_patterns=promising_patterns,
        pattern_buckets=pattern_buckets,
        dimension_buckets=_finalize_dimension_buckets(dimension_stats),
        symbol_diagnostics=_finalize_symbol_diagnostics(symbol_stats),
    )
    payload = build_pattern_calibration_report_payload(
        result=result,
        generated_at=_format_timestamp(datetime.now(timezone.utc)),
    )
    _write_report_outputs(payload, json_path=json_output, md_path=md_output)
    return result


def build_pattern_calibration_report_payload(
    *,
    result: TridentAIPatternCalibrationResult,
    generated_at: str,
) -> dict[str, object]:
    return {
        "generated_at": generated_at,
        "kind": "trident_ai_pattern_calibration_report",
        "result": result.to_dict(),
    }


def run_trident_ai_pattern_fold_validation_report(
    *,
    decision_journal_paths: Sequence[str | Path],
    paper_journal_paths: Sequence[str | Path],
    fold_labels: Sequence[str] | None = None,
    config: TridentAIConfig | None = None,
    report_json_path: str | Path | None = None,
    report_md_path: str | Path | None = None,
    min_trades_per_fold: int = 1,
    min_positive_folds: int = 2,
    max_catastrophic_net_bps: float = 50.0,
) -> TridentAIPatternFoldValidationResult:
    if not decision_journal_paths:
        raise ValueError("decision_journal_paths_required")
    if len(decision_journal_paths) != len(paper_journal_paths):
        raise ValueError("decision_and_paper_journal_counts_must_match")
    if fold_labels is not None and len(fold_labels) != len(decision_journal_paths):
        raise ValueError("fold_label_count_must_match_journal_count")
    if min_trades_per_fold <= 0:
        raise ValueError("min_trades_per_fold_must_be_positive")
    if min_positive_folds <= 0:
        raise ValueError("min_positive_folds_must_be_positive")
    if max_catastrophic_net_bps <= 0.0:
        raise ValueError("max_catastrophic_net_bps_must_be_positive")

    resolved_config = config or load_trident_ai_config()
    run_id = _timestamp_id(datetime.now(timezone.utc))
    output_dir = Path(resolved_config.paths.replay_output_dir)
    json_output = Path(
        report_json_path or output_dir / f"trident_ai_pattern_fold_validation_{run_id}.json"
    )
    md_output = Path(
        report_md_path or output_dir / f"trident_ai_pattern_fold_validation_{run_id}.md"
    )
    labels = tuple(
        str(label or f"fold_{index + 1}")
        for index, label in enumerate(fold_labels or ())
    )
    if not labels:
        labels = tuple(f"fold_{index + 1}" for index in range(len(decision_journal_paths)))

    fold_summaries: list[dict[str, object]] = []
    pattern_by_fold: dict[str, dict[str, dict[str, object]]] = defaultdict(dict)
    for label, decision_path, paper_path in zip(
        labels,
        decision_journal_paths,
        paper_journal_paths,
        strict=True,
    ):
        decisions_seen, observations = _observations_from_journals(decision_path, paper_path)
        fold_summaries.append(_fold_summary(label, decisions_seen, observations))
        pattern_stats: dict[str, _BucketStats] = defaultdict(_BucketStats)
        for observation in observations:
            pattern_stats[observation.descriptor.pattern_key].add(observation)
        for pattern, stats in pattern_stats.items():
            row = _bucket_row(pattern, stats)
            row["fold"] = label
            pattern_by_fold[pattern][label] = row

    pattern_rows = _fold_pattern_rows(
        pattern_by_fold,
        labels=labels,
        min_trades_per_fold=min_trades_per_fold,
        min_positive_folds=min_positive_folds,
        max_catastrophic_net_bps=max_catastrophic_net_bps,
    )
    stable_positive = [
        row for row in pattern_rows if row["classification"] == "stable_positive"
    ][:20]
    unstable = [
        row for row in pattern_rows if row["classification"] == "unstable_negative"
    ][:20]
    no_bonus = [
        row for row in pattern_rows if row["classification"] != "stable_positive"
    ][:50]

    result = TridentAIPatternFoldValidationResult(
        decision_journal_paths=tuple(str(path) for path in decision_journal_paths),
        paper_journal_paths=tuple(str(path) for path in paper_journal_paths),
        fold_labels=labels,
        report_json_path=str(json_output),
        report_md_path=str(md_output),
        min_trades_per_fold=min_trades_per_fold,
        min_positive_folds=min_positive_folds,
        max_catastrophic_net_bps=max_catastrophic_net_bps,
        folds=fold_summaries,
        pattern_folds=pattern_rows,
        stable_positive_patterns=stable_positive,
        unstable_patterns=unstable,
        no_bonus_patterns=no_bonus,
    )
    payload = build_pattern_fold_validation_report_payload(
        result=result,
        generated_at=_format_timestamp(datetime.now(timezone.utc)),
    )
    _write_fold_report_outputs(payload, json_path=json_output, md_path=md_output)
    return result


def build_pattern_fold_validation_report_payload(
    *,
    result: TridentAIPatternFoldValidationResult,
    generated_at: str,
) -> dict[str, object]:
    return {
        "generated_at": generated_at,
        "kind": "trident_ai_pattern_fold_validation_report",
        "result": result.to_dict(),
    }


@dataclass(frozen=True, slots=True)
class _DecisionRecord:
    decision_id: str
    timestamp: str
    symbol: str
    descriptor: _PatternDescriptor


def _open_decisions(path: str | Path) -> list[_DecisionRecord]:
    result: list[_DecisionRecord] = []
    for row in _decision_rows(path):
        proposal = _mapping(row.get("proposal"))
        if str(proposal.get("action", "") or "").lower() != "open":
            continue
        decision_id = str(proposal.get("decision_id", "") or "")
        if not decision_id:
            continue
        context = _mapping(row.get("context"))
        result.append(
            _DecisionRecord(
                decision_id=decision_id,
                timestamp=str(row.get("timestamp", "") or ""),
                symbol=str(row.get("symbol", "") or "").upper(),
                descriptor=_pattern_descriptor(context=context, proposal=proposal),
            )
        )
    return result


def _decision_rows(path: str | Path) -> list[dict[str, object]]:
    return [
        row
        for row in _iter_jsonl(path)
        if row.get("event_type") == LLM_REPLAY_DECISION_EVENT
    ]


def _paper_decisions_by_decision(path: str | Path) -> dict[str, dict[str, object]]:
    records: dict[str, dict[str, object]] = {}
    for row in _iter_jsonl(path):
        if row.get("event_type") != PAPER_REPLAY_DECISION_EVENT:
            continue
        decision_id = str(row.get("decision_id", "") or "")
        if decision_id:
            records[decision_id] = row
    return records


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


def _observation_from_records(
    *,
    decision: _DecisionRecord,
    paper_decision: Mapping[str, object] | None,
    trade: Mapping[str, object] | None,
) -> _Observation:
    paper_action = ""
    paper_reason = ""
    if paper_decision is not None:
        paper_action = str(paper_decision.get("paper_action", "") or "")
        paper_reason = str(paper_decision.get("reason", "") or "")
    trade_payload = _mapping(trade)
    return _Observation(
        decision_id=decision.decision_id,
        timestamp=decision.timestamp,
        symbol=decision.symbol,
        descriptor=decision.descriptor,
        paper_action=paper_action,
        paper_reason=paper_reason,
        close_reason=str(trade_payload.get("close_reason", "") or ""),
        pnl_usd=_number(trade_payload.get("pnl_usd")),
        gross_pnl_usd=_number(trade_payload.get("gross_pnl_usd")),
        fees_usd=_number(trade_payload.get("fees_usd")),
        notional_usd=_number(trade_payload.get("notional_usd")),
    )


def _observations_from_journals(
    decision_path: str | Path,
    paper_path: str | Path,
) -> tuple[int, list[_Observation]]:
    decisions = _open_decisions(decision_path)
    paper_decisions = _paper_decisions_by_decision(paper_path)
    trades = _closed_trades_by_decision(paper_path)
    return (
        len(_decision_rows(decision_path)),
        [
            _observation_from_records(
                decision=decision,
                paper_decision=paper_decisions.get(decision.decision_id),
                trade=trades.get(decision.decision_id),
            )
            for decision in decisions
        ],
    )


def _fold_summary(
    label: str,
    decisions_seen: int,
    observations: Sequence[_Observation],
) -> dict[str, object]:
    closed = [item for item in observations if item.close_reason]
    pnl = sum(item.pnl_usd for item in closed)
    gross = sum(item.gross_pnl_usd for item in closed)
    fees = sum(item.fees_usd for item in closed)
    notional = sum(item.notional_usd for item in closed)
    return {
        "fold": label,
        "decisions_seen": decisions_seen,
        "open_decisions": len(observations),
        "paper_opens": sum(1 for item in observations if item.paper_action == "open"),
        "paper_skips": sum(1 for item in observations if item.paper_action == "skip"),
        "closed_trades": len(closed),
        "winning_trades": sum(1 for item in closed if item.pnl_usd > 0.0),
        "losing_trades": sum(1 for item in closed if item.pnl_usd < 0.0),
        "realized_pnl_usd": round(pnl, 6),
        "gross_pnl_usd": round(gross, 6),
        "fees_usd": round(fees, 6),
        "avg_realized_net_bps": _pnl_bps(pnl, notional),
    }


def _fold_pattern_rows(
    pattern_by_fold: Mapping[str, Mapping[str, Mapping[str, object]]],
    *,
    labels: Sequence[str],
    min_trades_per_fold: int,
    min_positive_folds: int,
    max_catastrophic_net_bps: float,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for pattern, fold_rows_by_label in pattern_by_fold.items():
        fold_rows = [_mapping(fold_rows_by_label.get(label)) for label in labels]
        folds_with_decisions = sum(1 for row in fold_rows if _number(row.get("decisions")) > 0)
        folds_with_closed = sum(1 for row in fold_rows if _number(row.get("closed_trades")) > 0)
        positive_folds = sum(
            1
            for row in fold_rows
            if _number(row.get("closed_trades")) >= min_trades_per_fold
            and _number(row.get("pnl_usd")) > 0.0
        )
        negative_folds = sum(
            1
            for row in fold_rows
            if _number(row.get("closed_trades")) >= min_trades_per_fold
            and _number(row.get("pnl_usd")) < 0.0
        )
        catastrophic_folds = sum(
            1
            for row in fold_rows
            if _number(row.get("closed_trades")) > 0
            and _number(row.get("avg_realized_net_bps")) <= -max_catastrophic_net_bps
        )
        decisions = sum(int(_number(row.get("decisions"))) for row in fold_rows)
        paper_opens = sum(int(_number(row.get("paper_opens"))) for row in fold_rows)
        paper_skips = sum(int(_number(row.get("paper_skips"))) for row in fold_rows)
        closed_trades = sum(int(_number(row.get("closed_trades"))) for row in fold_rows)
        pnl = sum(_number(row.get("pnl_usd")) for row in fold_rows)
        gross = sum(_number(row.get("gross_pnl_usd")) for row in fold_rows)
        fees = sum(_number(row.get("fees_usd")) for row in fold_rows)
        notional = _notional_from_bucket_rows(fold_rows)
        classification = _fold_pattern_classification(
            positive_folds=positive_folds,
            negative_folds=negative_folds,
            catastrophic_folds=catastrophic_folds,
            total_pnl=pnl,
            min_positive_folds=min_positive_folds,
        )
        rows.append(
            {
                "pattern": pattern,
                "classification": classification,
                "folds_with_decisions": folds_with_decisions,
                "folds_with_closed": folds_with_closed,
                "positive_folds": positive_folds,
                "negative_folds": negative_folds,
                "catastrophic_folds": catastrophic_folds,
                "decisions": decisions,
                "paper_opens": paper_opens,
                "paper_skips": paper_skips,
                "closed_trades": closed_trades,
                "pnl_usd": round(pnl, 6),
                "gross_pnl_usd": round(gross, 6),
                "fees_usd": round(fees, 6),
                "avg_realized_net_bps": _pnl_bps(pnl, notional),
                "fold_results": [
                    _fold_pattern_cell(label, row)
                    for label, row in zip(labels, fold_rows, strict=True)
                ],
            }
        )
    return sorted(
        rows,
        key=lambda item: (
            item["classification"] != "unstable_negative",
            item["classification"] != "stable_positive",
            -item["closed_trades"],
            item["pnl_usd"],
            item["pattern"],
        ),
    )


def _fold_pattern_classification(
    *,
    positive_folds: int,
    negative_folds: int,
    catastrophic_folds: int,
    total_pnl: float,
    min_positive_folds: int,
) -> str:
    if positive_folds >= min_positive_folds and catastrophic_folds == 0 and total_pnl > 0.0:
        return "stable_positive"
    if catastrophic_folds > 0 or (negative_folds > 0 and total_pnl < 0.0):
        return "unstable_negative"
    return "insufficient_fold_support"


def _notional_from_bucket_rows(rows: Sequence[Mapping[str, object]]) -> float:
    total = 0.0
    for row in rows:
        avg_bps = _number(row.get("avg_realized_net_bps"))
        pnl = _number(row.get("pnl_usd"))
        if avg_bps:
            total += pnl / avg_bps * 10_000.0
    return total


def _fold_pattern_cell(label: str, row: Mapping[str, object]) -> dict[str, object]:
    if not row:
        return {
            "fold": label,
            "decisions": 0,
            "paper_opens": 0,
            "closed_trades": 0,
            "pnl_usd": 0.0,
            "avg_realized_net_bps": 0.0,
        }
    return {
        "fold": label,
        "decisions": int(_number(row.get("decisions"))),
        "paper_opens": int(_number(row.get("paper_opens"))),
        "closed_trades": int(_number(row.get("closed_trades"))),
        "pnl_usd": round(_number(row.get("pnl_usd")), 6),
        "avg_realized_net_bps": round(_number(row.get("avg_realized_net_bps")), 6),
    }


def _pattern_descriptor(
    *,
    context: Mapping[str, object],
    proposal: Mapping[str, object],
) -> _PatternDescriptor:
    side = str(proposal.get("side", "") or "").lower()
    regime = _regime_bucket(str(context.get("regime", "") or "unknown"))
    features = _mapping(context.get("features"))
    hint = _mapping(context.get(CANDIDATE_HINT_FIELD))
    reasons = tuple(_string_list(hint.get("reasons")))
    microprice = _microprice_alignment(features, side)
    flow_book = _flow_book_alignment(features, side)
    vwap = _signed_feature_alignment(features, side, "vwap_distance_bps", threshold_bps=2.0)
    edge_bucket = _edge_bucket(_number(hint.get("edge_to_cost_ratio")))
    net_edge_bucket = _net_edge_bucket(_number(hint.get("estimated_net_edge_bps")))
    volatility_bucket = _volatility_bucket(_number(features.get("realized_vol_short_bps")))
    parts = (
        f"microprice={microprice}",
        f"flow_book={flow_book}",
        f"vwap={vwap}",
        f"edge={edge_bucket}",
    )
    return _PatternDescriptor(
        pattern_key="|".join(parts),
        side=side or "unknown",
        regime=regime,
        microprice=microprice,
        flow_book=flow_book,
        vwap=vwap,
        edge_bucket=edge_bucket,
        net_edge_bucket=net_edge_bucket,
        volatility_bucket=volatility_bucket,
        reasons=reasons,
    )


def _microprice_alignment(features: Mapping[str, object], side: str) -> str:
    return _signed_feature_alignment(
        features,
        side,
        "microprice_dislocation_bps",
        threshold_bps=DEFAULT_MICROPRICE_CONFLICT_BPS,
    )


def _flow_book_alignment(features: Mapping[str, object], side: str) -> str:
    flow = _signed_feature_alignment(features, side, "trade_flow_bias", threshold_bps=0.25)
    book = _signed_feature_alignment(features, side, "book_imbalance", threshold_bps=0.25)
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
    threshold_bps: float,
) -> str:
    value = features.get(field_name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "missing"
    numeric = float(value)
    if abs(numeric) < threshold_bps:
        return "neutral"
    normalized_side = side.strip().lower()
    if normalized_side == "long":
        return "aligned" if numeric > 0.0 else "conflict"
    if normalized_side == "short":
        return "aligned" if numeric < 0.0 else "conflict"
    return "unknown"


def _regime_bucket(value: str) -> str:
    normalized = value.strip() or "unknown"
    if normalized.lower() in {"", "unknown", "none"}:
        return "unknown"
    return normalized


def _edge_bucket(edge_to_cost: float) -> str:
    if edge_to_cost >= 4.0:
        return ">=4.0"
    if edge_to_cost >= 3.0:
        return "3.0-4.0"
    if edge_to_cost >= 2.0:
        return "2.0-3.0"
    if edge_to_cost >= 1.5:
        return "1.5-2.0"
    return "<1.5"


def _net_edge_bucket(net_edge_bps: float) -> str:
    if net_edge_bps >= 35.0:
        return ">=35"
    if net_edge_bps >= 25.0:
        return "25-35"
    if net_edge_bps >= 15.0:
        return "15-25"
    if net_edge_bps >= 5.0:
        return "5-15"
    return "<5"


def _volatility_bucket(realized_vol_short_bps: float) -> str:
    if realized_vol_short_bps >= 25.0:
        return "high"
    if realized_vol_short_bps >= 12.0:
        return "medium"
    return "low"


def _finalize_pattern_buckets(
    buckets: Mapping[str, _BucketStats],
    min_trades_per_pattern: int,
) -> list[dict[str, object]]:
    rows = []
    for pattern, stats in buckets.items():
        row = _bucket_row(pattern, stats)
        if row["closed_trades"] < min_trades_per_pattern:
            row["reliability"] = "sample_too_small"
        elif row["pnl_usd"] < 0.0:
            row["reliability"] = "negative_pattern_watchlist"
        else:
            row["reliability"] = "positive_pattern_watchlist"
        rows.append(row)
    return sorted(rows, key=lambda item: (-item["closed_trades"], item["pnl_usd"], item["pattern"]))[:100]


def _finalize_dimension_buckets(
    dimensions: Mapping[str, Mapping[str, _BucketStats]],
) -> dict[str, list[dict[str, object]]]:
    result: dict[str, list[dict[str, object]]] = {}
    for dimension_name, buckets in sorted(dimensions.items()):
        result[dimension_name] = sorted(
            (_bucket_row(bucket_name, stats, key_name="bucket") for bucket_name, stats in buckets.items()),
            key=lambda item: (-item["closed_trades"], item["pnl_usd"], item["bucket"]),
        )
    return result


def _finalize_symbol_diagnostics(
    buckets: Mapping[str, _BucketStats],
) -> list[dict[str, object]]:
    return sorted(
        (_bucket_row(symbol, stats, key_name="symbol") for symbol, stats in buckets.items()),
        key=lambda item: (-item["closed_trades"], item["pnl_usd"], item["symbol"]),
    )


def _bucket_row(
    name: str,
    stats: _BucketStats,
    *,
    key_name: str = "pattern",
) -> dict[str, object]:
    return {
        key_name: name,
        "decisions": stats.decisions,
        "paper_opens": stats.paper_opens,
        "paper_skips": stats.paper_skips,
        "closed_trades": stats.closed_trades,
        "wins": stats.wins,
        "losses": stats.losses,
        "win_rate": round(stats.wins / stats.closed_trades, 6) if stats.closed_trades else 0.0,
        "pnl_usd": round(stats.pnl_usd, 6),
        "gross_pnl_usd": round(stats.gross_pnl_usd, 6),
        "fees_usd": round(stats.fees_usd, 6),
        "avg_realized_net_bps": _pnl_bps(stats.pnl_usd, stats.notional_usd),
        "close_reasons": dict(sorted(stats.close_reasons.items())),
        "skip_reasons": dict(sorted(stats.skip_reasons.items())),
        "symbols": dict(sorted(stats.symbols.items())),
    }


def _sample_warning(closed_trades: int, min_trades_per_pattern: int) -> str:
    if closed_trades < max(20, min_trades_per_pattern * 5):
        return "sample_small_pattern_findings_are_research_only"
    return ""


def _pnl_bps(value: float, notional: float) -> float:
    if notional <= 0.0:
        return 0.0
    return round(value / notional * 10_000.0, 6)


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


def _write_fold_report_outputs(
    payload: dict[str, object],
    *,
    json_path: Path,
    md_path: Path,
) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(_render_fold_markdown_report(payload), encoding="utf-8")


def _render_markdown_report(payload: Mapping[str, object]) -> str:
    result = _mapping(payload.get("result"))
    lines = [
        "# TRIDENT-AI Pattern Calibration Report",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Decision journals: `{result['decision_journal_paths']}`",
        f"- Paper journals: `{result['paper_journal_paths']}`",
        f"- Open decisions: `{result['open_decisions']}`",
        f"- Paper opens/skips: `{result['paper_opens']}` / `{result['paper_skips']}`",
        f"- Closed trades: `{result['closed_trades']}`",
        f"- Realized PnL: `${result['realized_pnl_usd']:.6f}`",
        f"- Avg realized net: `{result['avg_realized_net_bps']:.4f} bps`",
        f"- Sample warning: `{result['sample_warning']}`",
        "",
        "## Risky Patterns",
        "",
        "| Pattern | Trades | Win rate | PnL | Avg net | Close reasons | Symbols |",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    _append_bucket_rows(lines, result.get("risky_patterns"), key_name="pattern")
    lines.extend(
        [
            "",
            "## Promising Patterns",
            "",
            "| Pattern | Trades | Win rate | PnL | Avg net | Close reasons | Symbols |",
            "|---|---:|---:|---:|---:|---|---|",
        ]
    )
    _append_bucket_rows(lines, result.get("promising_patterns"), key_name="pattern")
    lines.extend(
        [
            "",
            "## Pattern Buckets",
            "",
            "| Pattern | Decisions | Opens | Trades | Win rate | PnL | Avg net | Reliability |",
            "|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    buckets = result.get("pattern_buckets", [])
    if isinstance(buckets, list) and buckets:
        for item in buckets[:40]:
            bucket = _mapping(item)
            lines.append(
                f"| {bucket.get('pattern', '')} | {bucket.get('decisions', 0)} | "
                f"{bucket.get('paper_opens', 0)} | {bucket.get('closed_trades', 0)} | "
                f"{_number(bucket.get('win_rate')):.2%} | ${_number(bucket.get('pnl_usd')):.6f} | "
                f"{_number(bucket.get('avg_realized_net_bps')):.2f} | "
                f"{bucket.get('reliability', '')} |"
            )
    else:
        lines.append("| none | 0 | 0 | 0 | 0.00% | $0.000000 | 0.00 | n/a |")
    lines.extend(
        [
            "",
            "## Symbol Diagnostics",
            "",
            "Secondary diagnostic only: do not use this table as a coin-specific rule.",
            "",
            "| Symbol | Decisions | Opens | Trades | Win rate | PnL | Avg net |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    symbols = result.get("symbol_diagnostics", [])
    if isinstance(symbols, list) and symbols:
        for item in symbols:
            bucket = _mapping(item)
            lines.append(
                f"| {bucket.get('symbol', '')} | {bucket.get('decisions', 0)} | "
                f"{bucket.get('paper_opens', 0)} | {bucket.get('closed_trades', 0)} | "
                f"{_number(bucket.get('win_rate')):.2%} | ${_number(bucket.get('pnl_usd')):.6f} | "
                f"{_number(bucket.get('avg_realized_net_bps')):.2f} |"
            )
    else:
        lines.append("| none | 0 | 0 | 0 | 0.00% | $0.000000 | 0.00 |")
    lines.append("")
    return "\n".join(lines)


def _render_fold_markdown_report(payload: Mapping[str, object]) -> str:
    result = _mapping(payload.get("result"))
    lines = [
        "# TRIDENT-AI Pattern Fold Validation Report",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Folds: `{result.get('fold_labels', [])}`",
        f"- Min trades per fold: `{result.get('min_trades_per_fold')}`",
        f"- Min positive folds: `{result.get('min_positive_folds')}`",
        f"- Max catastrophic net bps: `{result.get('max_catastrophic_net_bps')}`",
        "",
        "## Fold Summaries",
        "",
        "| Fold | Decisions | Opens | Trades | PnL | Avg net |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    folds = result.get("folds", [])
    if isinstance(folds, list) and folds:
        for item in folds:
            fold = _mapping(item)
            lines.append(
                f"| {fold.get('fold', '')} | {fold.get('open_decisions', 0)} | "
                f"{fold.get('paper_opens', 0)} | {fold.get('closed_trades', 0)} | "
                f"${_number(fold.get('realized_pnl_usd')):.6f} | "
                f"{_number(fold.get('avg_realized_net_bps')):.2f} |"
            )
    else:
        lines.append("| none | 0 | 0 | 0 | $0.000000 | 0.00 |")
    lines.extend(
        [
            "",
            "## Stable Positive Patterns",
            "",
            "| Pattern | Closed | Positive folds | Negative folds | Catastrophic folds | PnL | Avg net |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    _append_fold_rows(lines, result.get("stable_positive_patterns"))
    lines.extend(
        [
            "",
            "## Unstable Patterns",
            "",
            "| Pattern | Closed | Positive folds | Negative folds | Catastrophic folds | PnL | Avg net |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    _append_fold_rows(lines, result.get("unstable_patterns"))
    lines.extend(
        [
            "",
            "## All Pattern Classifications",
            "",
            "| Pattern | Classification | Folds closed | Closed | PnL | Avg net |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    pattern_rows = result.get("pattern_folds", [])
    if isinstance(pattern_rows, list) and pattern_rows:
        for item in pattern_rows[:60]:
            row = _mapping(item)
            lines.append(
                f"| {row.get('pattern', '')} | {row.get('classification', '')} | "
                f"{row.get('folds_with_closed', 0)} | {row.get('closed_trades', 0)} | "
                f"${_number(row.get('pnl_usd')):.6f} | "
                f"{_number(row.get('avg_realized_net_bps')):.2f} |"
            )
    else:
        lines.append("| none | n/a | 0 | 0 | $0.000000 | 0.00 |")
    lines.append("")
    return "\n".join(lines)


def _append_bucket_rows(
    lines: list[str],
    value: object,
    *,
    key_name: str,
) -> None:
    if not isinstance(value, list) or not value:
        lines.append("| none | 0 | 0.00% | $0.000000 | 0.00 | n/a | n/a |")
        return
    for item in value:
        bucket = _mapping(item)
        lines.append(
            f"| {bucket.get(key_name, '')} | {bucket.get('closed_trades', 0)} | "
            f"{_number(bucket.get('win_rate')):.2%} | ${_number(bucket.get('pnl_usd')):.6f} | "
            f"{_number(bucket.get('avg_realized_net_bps')):.2f} | "
            f"{bucket.get('close_reasons', {})} | {bucket.get('symbols', {})} |"
        )


def _append_fold_rows(lines: list[str], value: object) -> None:
    if not isinstance(value, list) or not value:
        lines.append("| none | 0 | 0 | 0 | 0 | $0.000000 | 0.00 |")
        return
    for item in value:
        bucket = _mapping(item)
        lines.append(
            f"| {bucket.get('pattern', '')} | {bucket.get('closed_trades', 0)} | "
            f"{bucket.get('positive_folds', 0)} | {bucket.get('negative_folds', 0)} | "
            f"{bucket.get('catastrophic_folds', 0)} | "
            f"${_number(bucket.get('pnl_usd')):.6f} | "
            f"{_number(bucket.get('avg_realized_net_bps')):.2f} |"
        )


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
    return dict(value) if isinstance(value, Mapping) else {}


def _string_list(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
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
