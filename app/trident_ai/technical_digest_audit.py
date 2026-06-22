from __future__ import annotations

import bisect
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.trident_ai.candidate_scan import CANDIDATE_HINT_FIELD
from app.trident_ai.config import TridentAIConfig, load_trident_ai_config
from app.trident_ai.features import AgentMarketContextBuildConfig, TridentAIFeatureBuilder
from app.trident_ai.outcome_audit import (
    DEFAULT_OUTCOME_HORIZONS_MINUTES,
    _format_timestamp,
    _gross_move_bps,
    _iter_jsonl,
    _normalize_horizons,
    _number,
    _parse_timestamp,
    _timestamp_id,
)
from app.trident_ai.technical_digest import (
    TECHNICAL_DIGEST_FEATURE_NAME,
    compact_technical_digest,
)


DEFAULT_TECHNICAL_DIGEST_MIN_BUCKET_SAMPLES = 4
DEFAULT_TECHNICAL_DIGEST_MIN_DELTA_BPS = 5.0
DEFAULT_TECHNICAL_DIGEST_MIN_POSITIVE_FOLDS = 2
DEFAULT_TECHNICAL_DIGEST_MAX_NEGATIVE_FOLDS = 0


@dataclass(frozen=True, slots=True)
class TridentAITechnicalDigestAuditResult:
    candidate_input_path: str
    market_input_path: str
    report_json_path: str
    report_md_path: str
    horizons_minutes: tuple[int, ...] = DEFAULT_OUTCOME_HORIZONS_MINUTES
    min_bucket_samples: int = DEFAULT_TECHNICAL_DIGEST_MIN_BUCKET_SAMPLES
    min_delta_bps: float = DEFAULT_TECHNICAL_DIGEST_MIN_DELTA_BPS
    candidates_seen: int = 0
    candidates_with_digest: int = 0
    candidates_with_any_outcome: int = 0
    missing_digest: int = 0
    missing_outcomes: int = 0
    best_horizon_minutes: int = 0
    best_horizon_avg_net_bps: float = 0.0
    recommendation: str = "hold_zero_cost"
    summary: dict[str, object] = field(default_factory=dict)
    horizon_stats: dict[str, dict[str, object]] = field(default_factory=dict)
    bucket_rows: list[dict[str, object]] = field(default_factory=list)
    positive_buckets: list[dict[str, object]] = field(default_factory=list)
    negative_buckets: list[dict[str, object]] = field(default_factory=list)
    veto_or_conflict_buckets: list[dict[str, object]] = field(default_factory=list)
    items: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_input_path": self.candidate_input_path,
            "market_input_path": self.market_input_path,
            "report_json_path": self.report_json_path,
            "report_md_path": self.report_md_path,
            "horizons_minutes": list(self.horizons_minutes),
            "min_bucket_samples": self.min_bucket_samples,
            "min_delta_bps": round(self.min_delta_bps, 6),
            "candidates_seen": self.candidates_seen,
            "candidates_with_digest": self.candidates_with_digest,
            "candidates_with_any_outcome": self.candidates_with_any_outcome,
            "missing_digest": self.missing_digest,
            "missing_outcomes": self.missing_outcomes,
            "best_horizon_minutes": self.best_horizon_minutes,
            "best_horizon_avg_net_bps": round(self.best_horizon_avg_net_bps, 6),
            "recommendation": self.recommendation,
            "summary": self.summary,
            "horizon_stats": self.horizon_stats,
            "bucket_rows": self.bucket_rows,
            "positive_buckets": self.positive_buckets,
            "negative_buckets": self.negative_buckets,
            "veto_or_conflict_buckets": self.veto_or_conflict_buckets,
            "items": self.items,
        }


@dataclass(frozen=True, slots=True)
class TridentAITechnicalDigestFoldValidationResult:
    candidate_input_paths: tuple[str, ...]
    market_input_paths: tuple[str, ...]
    fold_labels: tuple[str, ...]
    report_json_path: str
    report_md_path: str
    horizons_minutes: tuple[int, ...] = DEFAULT_OUTCOME_HORIZONS_MINUTES
    min_bucket_samples: int = DEFAULT_TECHNICAL_DIGEST_MIN_BUCKET_SAMPLES
    min_delta_bps: float = DEFAULT_TECHNICAL_DIGEST_MIN_DELTA_BPS
    min_positive_folds: int = DEFAULT_TECHNICAL_DIGEST_MIN_POSITIVE_FOLDS
    max_negative_folds: int = DEFAULT_TECHNICAL_DIGEST_MAX_NEGATIVE_FOLDS
    recommendation: str = "hold_zero_cost_multifold_not_promising"
    summary: dict[str, object] = field(default_factory=dict)
    folds: list[dict[str, object]] = field(default_factory=list)
    bucket_rows: list[dict[str, object]] = field(default_factory=list)
    stable_positive_buckets: list[dict[str, object]] = field(default_factory=list)
    stable_negative_buckets: list[dict[str, object]] = field(default_factory=list)
    unstable_buckets: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_input_paths": list(self.candidate_input_paths),
            "market_input_paths": list(self.market_input_paths),
            "fold_labels": list(self.fold_labels),
            "report_json_path": self.report_json_path,
            "report_md_path": self.report_md_path,
            "horizons_minutes": list(self.horizons_minutes),
            "min_bucket_samples": self.min_bucket_samples,
            "min_delta_bps": round(self.min_delta_bps, 6),
            "min_positive_folds": self.min_positive_folds,
            "max_negative_folds": self.max_negative_folds,
            "recommendation": self.recommendation,
            "summary": self.summary,
            "folds": self.folds,
            "bucket_rows": self.bucket_rows,
            "stable_positive_buckets": self.stable_positive_buckets,
            "stable_negative_buckets": self.stable_negative_buckets,
            "unstable_buckets": self.unstable_buckets,
        }


@dataclass(frozen=True, slots=True)
class TridentAITechnicalDigestVetoAuditResult:
    candidate_input_paths: tuple[str, ...]
    market_input_paths: tuple[str, ...]
    fold_labels: tuple[str, ...]
    veto_buckets: tuple[str, ...]
    report_json_path: str
    report_md_path: str
    horizons_minutes: tuple[int, ...] = DEFAULT_OUTCOME_HORIZONS_MINUTES
    min_delta_bps: float = DEFAULT_TECHNICAL_DIGEST_MIN_DELTA_BPS
    recommendation: str = "hold_veto_not_promising"
    baseline_summary: dict[str, object] = field(default_factory=dict)
    kept_summary: dict[str, object] = field(default_factory=dict)
    vetoed_summary: dict[str, object] = field(default_factory=dict)
    delta_summary: dict[str, object] = field(default_factory=dict)
    fold_rows: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_input_paths": list(self.candidate_input_paths),
            "market_input_paths": list(self.market_input_paths),
            "fold_labels": list(self.fold_labels),
            "veto_buckets": list(self.veto_buckets),
            "report_json_path": self.report_json_path,
            "report_md_path": self.report_md_path,
            "horizons_minutes": list(self.horizons_minutes),
            "min_delta_bps": round(self.min_delta_bps, 6),
            "recommendation": self.recommendation,
            "baseline_summary": self.baseline_summary,
            "kept_summary": self.kept_summary,
            "vetoed_summary": self.vetoed_summary,
            "delta_summary": self.delta_summary,
            "fold_rows": self.fold_rows,
        }


def run_trident_ai_technical_digest_audit(
    *,
    candidate_input_path: str | Path,
    market_input_path: str | Path,
    config: TridentAIConfig | None = None,
    report_json_path: str | Path | None = None,
    report_md_path: str | Path | None = None,
    horizons_minutes: tuple[int, ...] = DEFAULT_OUTCOME_HORIZONS_MINUTES,
    min_bucket_samples: int = DEFAULT_TECHNICAL_DIGEST_MIN_BUCKET_SAMPLES,
    min_delta_bps: float = DEFAULT_TECHNICAL_DIGEST_MIN_DELTA_BPS,
) -> TridentAITechnicalDigestAuditResult:
    if min_bucket_samples <= 0:
        raise ValueError("min_bucket_samples_must_be_positive")
    if min_delta_bps < 0.0:
        raise ValueError("min_delta_bps_must_be_non_negative")

    resolved_config = config or load_trident_ai_config()
    horizons = _normalize_horizons(horizons_minutes)
    run_id = _timestamp_id(datetime.now(timezone.utc))
    output_dir = Path(resolved_config.paths.replay_output_dir)
    json_output = Path(
        report_json_path or output_dir / f"trident_ai_technical_digest_audit_{run_id}.json"
    )
    md_output = Path(
        report_md_path or output_dir / f"trident_ai_technical_digest_audit_{run_id}.md"
    )
    feature_builder = TridentAIFeatureBuilder(
        AgentMarketContextBuildConfig.from_trident_ai_config(resolved_config)
    )
    audit = _technical_digest_audit_core(
        candidate_input_path=candidate_input_path,
        market_input_path=market_input_path,
        feature_builder=feature_builder,
        horizons=horizons,
        min_bucket_samples=min_bucket_samples,
        min_delta_bps=min_delta_bps,
    )
    result = TridentAITechnicalDigestAuditResult(
        candidate_input_path=str(candidate_input_path),
        market_input_path=str(market_input_path),
        report_json_path=str(json_output),
        report_md_path=str(md_output),
        horizons_minutes=horizons,
        min_bucket_samples=min_bucket_samples,
        min_delta_bps=min_delta_bps,
        candidates_seen=int(audit["candidates_seen"]),
        candidates_with_digest=int(audit["candidates_with_digest"]),
        candidates_with_any_outcome=int(audit["candidates_with_any_outcome"]),
        missing_digest=int(audit["missing_digest"]),
        missing_outcomes=int(audit["missing_outcomes"]),
        best_horizon_minutes=int(audit["best_horizon_minutes"]),
        best_horizon_avg_net_bps=float(audit["best_horizon_avg_net_bps"]),
        recommendation=str(audit["recommendation"]),
        summary=_mapping(audit["summary"]),
        horizon_stats=_mapping(audit["horizon_stats"]),
        bucket_rows=_mapping_list(audit["bucket_rows"])[:250],
        positive_buckets=_mapping_list(audit["positive_buckets"])[:40],
        negative_buckets=_mapping_list(audit["negative_buckets"])[:40],
        veto_or_conflict_buckets=_mapping_list(audit["veto_or_conflict_buckets"])[:40],
        items=_mapping_list(audit["items"])[:250],
    )
    payload = build_technical_digest_audit_report_payload(
        result=result,
        generated_at=_format_timestamp(datetime.now(timezone.utc)),
    )
    _write_report_outputs(payload, json_path=json_output, md_path=md_output)
    return result


def run_trident_ai_technical_digest_veto_audit(
    *,
    candidate_input_paths: Sequence[str | Path],
    market_input_paths: Sequence[str | Path],
    veto_buckets: Sequence[str],
    fold_labels: Sequence[str] | None = None,
    config: TridentAIConfig | None = None,
    report_json_path: str | Path | None = None,
    report_md_path: str | Path | None = None,
    horizons_minutes: tuple[int, ...] = DEFAULT_OUTCOME_HORIZONS_MINUTES,
    min_delta_bps: float = DEFAULT_TECHNICAL_DIGEST_MIN_DELTA_BPS,
) -> TridentAITechnicalDigestVetoAuditResult:
    if not candidate_input_paths:
        raise ValueError("candidate_input_paths_required")
    if len(candidate_input_paths) != len(market_input_paths):
        raise ValueError("candidate_and_market_input_counts_must_match")
    if fold_labels is not None and len(fold_labels) != len(candidate_input_paths):
        raise ValueError("fold_label_count_must_match_input_count")
    if min_delta_bps < 0.0:
        raise ValueError("min_delta_bps_must_be_non_negative")
    parsed_vetoes = _parse_veto_buckets(veto_buckets)
    if not parsed_vetoes:
        raise ValueError("veto_buckets_required")

    resolved_config = config or load_trident_ai_config()
    horizons = _normalize_horizons(horizons_minutes)
    run_id = _timestamp_id(datetime.now(timezone.utc))
    output_dir = Path(resolved_config.paths.replay_output_dir)
    json_output = Path(
        report_json_path or output_dir / f"trident_ai_technical_digest_veto_audit_{run_id}.json"
    )
    md_output = Path(
        report_md_path or output_dir / f"trident_ai_technical_digest_veto_audit_{run_id}.md"
    )
    labels = tuple(
        str(label or f"fold_{index + 1}") for index, label in enumerate(fold_labels or ())
    )
    if not labels:
        labels = tuple(f"fold_{index + 1}" for index in range(len(candidate_input_paths)))

    feature_builder = TridentAIFeatureBuilder(
        AgentMarketContextBuildConfig.from_trident_ai_config(resolved_config)
    )
    fold_rows: list[dict[str, object]] = []
    for label, candidate_path, market_path in zip(
        labels,
        candidate_input_paths,
        market_input_paths,
        strict=True,
    ):
        audit = _technical_digest_audit_core(
            candidate_input_path=candidate_path,
            market_input_path=market_path,
            feature_builder=feature_builder,
            horizons=horizons,
            min_bucket_samples=1,
            min_delta_bps=min_delta_bps,
        )
        fold_rows.append(
            _technical_veto_fold_row(
                label=label,
                candidate_input_path=candidate_path,
                market_input_path=market_path,
                audit=audit,
                parsed_vetoes=parsed_vetoes,
                min_delta_bps=min_delta_bps,
            )
        )

    baseline_summary = _combine_outcome_summaries(row["baseline"] for row in fold_rows)
    kept_summary = _combine_outcome_summaries(row["kept"] for row in fold_rows)
    vetoed_summary = _combine_outcome_summaries(row["vetoed"] for row in fold_rows)
    delta_summary = _veto_delta_summary(kept_summary, baseline_summary)
    recommendation = _veto_recommendation(
        delta_summary=delta_summary,
        fold_rows=fold_rows,
        min_delta_bps=min_delta_bps,
    )
    result = TridentAITechnicalDigestVetoAuditResult(
        candidate_input_paths=tuple(str(path) for path in candidate_input_paths),
        market_input_paths=tuple(str(path) for path in market_input_paths),
        fold_labels=labels,
        veto_buckets=tuple(_format_veto_bucket(family, bucket) for family, bucket in parsed_vetoes),
        report_json_path=str(json_output),
        report_md_path=str(md_output),
        horizons_minutes=horizons,
        min_delta_bps=min_delta_bps,
        recommendation=recommendation,
        baseline_summary=baseline_summary,
        kept_summary=kept_summary,
        vetoed_summary=vetoed_summary,
        delta_summary=delta_summary,
        fold_rows=fold_rows,
    )
    payload = build_technical_digest_veto_audit_report_payload(
        result=result,
        generated_at=_format_timestamp(datetime.now(timezone.utc)),
    )
    _write_veto_audit_report_outputs(payload, json_path=json_output, md_path=md_output)
    return result


def run_trident_ai_technical_digest_fold_validation(
    *,
    candidate_input_paths: Sequence[str | Path],
    market_input_paths: Sequence[str | Path],
    fold_labels: Sequence[str] | None = None,
    config: TridentAIConfig | None = None,
    report_json_path: str | Path | None = None,
    report_md_path: str | Path | None = None,
    horizons_minutes: tuple[int, ...] = DEFAULT_OUTCOME_HORIZONS_MINUTES,
    min_bucket_samples: int = DEFAULT_TECHNICAL_DIGEST_MIN_BUCKET_SAMPLES,
    min_delta_bps: float = DEFAULT_TECHNICAL_DIGEST_MIN_DELTA_BPS,
    min_positive_folds: int = DEFAULT_TECHNICAL_DIGEST_MIN_POSITIVE_FOLDS,
    max_negative_folds: int = DEFAULT_TECHNICAL_DIGEST_MAX_NEGATIVE_FOLDS,
) -> TridentAITechnicalDigestFoldValidationResult:
    if not candidate_input_paths:
        raise ValueError("candidate_input_paths_required")
    if len(candidate_input_paths) != len(market_input_paths):
        raise ValueError("candidate_and_market_input_counts_must_match")
    if fold_labels is not None and len(fold_labels) != len(candidate_input_paths):
        raise ValueError("fold_label_count_must_match_input_count")
    if min_bucket_samples <= 0:
        raise ValueError("min_bucket_samples_must_be_positive")
    if min_delta_bps < 0.0:
        raise ValueError("min_delta_bps_must_be_non_negative")
    if min_positive_folds <= 0:
        raise ValueError("min_positive_folds_must_be_positive")
    if max_negative_folds < 0:
        raise ValueError("max_negative_folds_must_be_non_negative")

    resolved_config = config or load_trident_ai_config()
    horizons = _normalize_horizons(horizons_minutes)
    run_id = _timestamp_id(datetime.now(timezone.utc))
    output_dir = Path(resolved_config.paths.replay_output_dir)
    json_output = Path(
        report_json_path
        or output_dir / f"trident_ai_technical_digest_fold_validation_{run_id}.json"
    )
    md_output = Path(
        report_md_path
        or output_dir / f"trident_ai_technical_digest_fold_validation_{run_id}.md"
    )
    labels = tuple(
        str(label or f"fold_{index + 1}") for index, label in enumerate(fold_labels or ())
    )
    if not labels:
        labels = tuple(f"fold_{index + 1}" for index in range(len(candidate_input_paths)))

    feature_builder = TridentAIFeatureBuilder(
        AgentMarketContextBuildConfig.from_trident_ai_config(resolved_config)
    )
    folds: list[dict[str, object]] = []
    bucket_rows_by_key: dict[tuple[str, str], dict[str, dict[str, object]]] = defaultdict(dict)
    for label, candidate_path, market_path in zip(
        labels,
        candidate_input_paths,
        market_input_paths,
        strict=True,
    ):
        audit = _technical_digest_audit_core(
            candidate_input_path=candidate_path,
            market_input_path=market_path,
            feature_builder=feature_builder,
            horizons=horizons,
            min_bucket_samples=min_bucket_samples,
            min_delta_bps=min_delta_bps,
        )
        folds.append(
            _technical_digest_fold_summary(
                label=label,
                candidate_input_path=candidate_path,
                market_input_path=market_path,
                audit=audit,
            )
        )
        for row in _mapping_list(audit["bucket_rows"]):
            family = str(row.get("family", "") or "")
            bucket = str(row.get("bucket", "") or "")
            if not family or not bucket:
                continue
            fold_row = dict(row)
            fold_row["fold"] = label
            bucket_rows_by_key[(family, bucket)][label] = fold_row

    bucket_rows = _fold_bucket_rows(
        bucket_rows_by_key,
        labels=labels,
        min_positive_folds=min_positive_folds,
        max_negative_folds=max_negative_folds,
    )
    stable_positive = [
        row for row in bucket_rows if row["classification"] == "stable_positive"
    ][:40]
    stable_negative = [
        row for row in bucket_rows if row["classification"] == "stable_negative"
    ][:40]
    unstable = [
        row
        for row in bucket_rows
        if str(row["classification"]).startswith("unstable")
    ][:40]
    recommendation = _fold_validation_recommendation(
        stable_positive_buckets=stable_positive,
        stable_negative_buckets=stable_negative,
    )
    result = TridentAITechnicalDigestFoldValidationResult(
        candidate_input_paths=tuple(str(path) for path in candidate_input_paths),
        market_input_paths=tuple(str(path) for path in market_input_paths),
        fold_labels=labels,
        report_json_path=str(json_output),
        report_md_path=str(md_output),
        horizons_minutes=horizons,
        min_bucket_samples=min_bucket_samples,
        min_delta_bps=min_delta_bps,
        min_positive_folds=min_positive_folds,
        max_negative_folds=max_negative_folds,
        recommendation=recommendation,
        summary=_fold_validation_summary(folds, bucket_rows),
        folds=folds,
        bucket_rows=bucket_rows[:300],
        stable_positive_buckets=stable_positive,
        stable_negative_buckets=stable_negative,
        unstable_buckets=unstable,
    )
    payload = build_technical_digest_fold_validation_report_payload(
        result=result,
        generated_at=_format_timestamp(datetime.now(timezone.utc)),
    )
    _write_fold_validation_report_outputs(payload, json_path=json_output, md_path=md_output)
    return result


def build_technical_digest_audit_report_payload(
    *,
    result: TridentAITechnicalDigestAuditResult,
    generated_at: str,
) -> dict[str, object]:
    return {
        "generated_at": generated_at,
        "kind": "trident_ai_technical_digest_audit",
        "result": result.to_dict(),
    }


def build_technical_digest_veto_audit_report_payload(
    *,
    result: TridentAITechnicalDigestVetoAuditResult,
    generated_at: str,
) -> dict[str, object]:
    return {
        "generated_at": generated_at,
        "kind": "trident_ai_technical_digest_veto_audit",
        "result": result.to_dict(),
    }


def build_technical_digest_fold_validation_report_payload(
    *,
    result: TridentAITechnicalDigestFoldValidationResult,
    generated_at: str,
) -> dict[str, object]:
    return {
        "generated_at": generated_at,
        "kind": "trident_ai_technical_digest_fold_validation",
        "result": result.to_dict(),
    }


def _technical_digest_audit_core(
    *,
    candidate_input_path: str | Path,
    market_input_path: str | Path,
    feature_builder: TridentAIFeatureBuilder,
    horizons: tuple[int, ...],
    min_bucket_samples: int,
    min_delta_bps: float,
) -> dict[str, object]:
    candidates = _candidate_items(candidate_input_path, feature_builder=feature_builder)
    market_index = _market_price_index(market_input_path)
    missing_outcomes = 0
    candidates_with_any_outcome = 0
    horizon_accumulators: dict[int, list[float]] = defaultdict(list)

    for item in candidates:
        outcomes: list[dict[str, object]] = []
        any_outcome = False
        for horizon in horizons:
            outcome = _candidate_horizon_outcome(
                item,
                market_index=market_index,
                horizon_minutes=horizon,
            )
            outcomes.append(outcome)
            if bool(outcome.get("available", False)):
                any_outcome = True
                horizon_accumulators[horizon].append(_number(outcome.get("realized_net_bps")))
            else:
                missing_outcomes += 1
        if any_outcome:
            candidates_with_any_outcome += 1
        item["outcomes"] = outcomes
        item["best_outcome"] = _best_outcome(outcomes)

    horizon_stats = _horizon_stats(horizons, horizon_accumulators)
    best_horizon, best_avg_net = _best_horizon(horizon_stats)
    bucket_rows = _bucket_rows(
        items=candidates,
        horizon=best_horizon,
        baseline_avg_net_bps=best_avg_net,
        min_bucket_samples=min_bucket_samples,
        min_delta_bps=min_delta_bps,
    )
    positive = [row for row in bucket_rows if row["classification"] == "positive_separator"]
    negative = [row for row in bucket_rows if row["classification"] == "negative_separator"]
    veto_or_conflict = [
        row
        for row in bucket_rows
        if row["family"] in {"veto_signal", "conflict", "has_veto", "has_conflict"}
    ]
    return {
        "candidates_seen": len(candidates),
        "candidates_with_digest": sum(1 for item in candidates if _mapping(item.get("tech"))),
        "candidates_with_any_outcome": candidates_with_any_outcome,
        "missing_digest": sum(1 for item in candidates if not _mapping(item.get("tech"))),
        "missing_outcomes": missing_outcomes,
        "best_horizon_minutes": best_horizon,
        "best_horizon_avg_net_bps": best_avg_net,
        "recommendation": _recommendation(
            positive_buckets=positive,
            negative_buckets=negative,
            veto_or_conflict_buckets=veto_or_conflict,
        ),
        "summary": _summary(candidates, best_horizon=best_horizon),
        "horizon_stats": horizon_stats,
        "bucket_rows": bucket_rows,
        "positive_buckets": positive,
        "negative_buckets": negative,
        "veto_or_conflict_buckets": veto_or_conflict,
        "items": candidates,
    }


def _technical_digest_fold_summary(
    *,
    label: str,
    candidate_input_path: str | Path,
    market_input_path: str | Path,
    audit: Mapping[str, object],
) -> dict[str, object]:
    summary = _mapping(audit.get("summary"))
    return {
        "fold": label,
        "candidate_input_path": str(candidate_input_path),
        "market_input_path": str(market_input_path),
        "candidates_seen": int(_number(audit.get("candidates_seen"))),
        "candidates_with_digest": int(_number(audit.get("candidates_with_digest"))),
        "candidates_with_any_outcome": int(_number(audit.get("candidates_with_any_outcome"))),
        "missing_digest": int(_number(audit.get("missing_digest"))),
        "missing_outcomes": int(_number(audit.get("missing_outcomes"))),
        "best_horizon_minutes": int(_number(audit.get("best_horizon_minutes"))),
        "best_horizon_avg_net_bps": round(_number(audit.get("best_horizon_avg_net_bps")), 6),
        "summary_avg_net_bps": round(_number(summary.get("avg_net_bps")), 6),
        "summary_win_rate": round(_number(summary.get("win_rate")), 6),
        "positive_buckets": len(_mapping_list(audit.get("positive_buckets"))),
        "negative_buckets": len(_mapping_list(audit.get("negative_buckets"))),
        "recommendation": str(audit.get("recommendation", "") or ""),
    }


def _fold_bucket_rows(
    bucket_rows_by_key: Mapping[tuple[str, str], Mapping[str, Mapping[str, object]]],
    *,
    labels: Sequence[str],
    min_positive_folds: int,
    max_negative_folds: int,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for (family, bucket), rows_by_label in bucket_rows_by_key.items():
        fold_rows = [_mapping(rows_by_label.get(label)) for label in labels]
        folds_seen = sum(1 for row in fold_rows if _number(row.get("samples")) > 0)
        positive_folds = sum(
            1 for row in fold_rows if row.get("classification") == "positive_separator"
        )
        negative_folds = sum(
            1 for row in fold_rows if row.get("classification") == "negative_separator"
        )
        neutral_folds = sum(1 for row in fold_rows if row.get("classification") == "neutral")
        thin_folds = sum(1 for row in fold_rows if row.get("classification") == "thin")
        total_samples = sum(int(_number(row.get("samples"))) for row in fold_rows)
        wins = sum(int(_number(row.get("wins"))) for row in fold_rows)
        total_net = sum(_number(row.get("total_net_bps")) for row in fold_rows)
        weighted_delta = sum(
            _number(row.get("delta_vs_all_avg_net_bps")) * int(_number(row.get("samples")))
            for row in fold_rows
        )
        avg_net = total_net / total_samples if total_samples else 0.0
        avg_delta = weighted_delta / total_samples if total_samples else 0.0
        classification = _fold_bucket_classification(
            positive_folds=positive_folds,
            negative_folds=negative_folds,
            min_positive_folds=min_positive_folds,
            max_negative_folds=max_negative_folds,
        )
        rows.append(
            {
                "family": family,
                "bucket": bucket,
                "classification": classification,
                "folds_seen": folds_seen,
                "positive_folds": positive_folds,
                "negative_folds": negative_folds,
                "neutral_folds": neutral_folds,
                "thin_folds": thin_folds,
                "total_samples": total_samples,
                "wins": wins,
                "win_rate": round(wins / total_samples, 6) if total_samples else 0.0,
                "avg_net_bps": round(avg_net, 6),
                "total_net_bps": round(total_net, 6),
                "avg_delta_vs_all_avg_net_bps": round(avg_delta, 6),
                "positive_fold_labels": [
                    label
                    for label, row in zip(labels, fold_rows, strict=True)
                    if row.get("classification") == "positive_separator"
                ],
                "negative_fold_labels": [
                    label
                    for label, row in zip(labels, fold_rows, strict=True)
                    if row.get("classification") == "negative_separator"
                ],
                "fold_results": [
                    _fold_bucket_cell(label, row)
                    for label, row in zip(labels, fold_rows, strict=True)
                ],
            }
        )
    return sorted(rows, key=_fold_bucket_sort_key)


def _fold_bucket_classification(
    *,
    positive_folds: int,
    negative_folds: int,
    min_positive_folds: int,
    max_negative_folds: int,
) -> str:
    if positive_folds >= min_positive_folds and negative_folds <= max_negative_folds:
        return "stable_positive"
    if negative_folds >= min_positive_folds and positive_folds <= max_negative_folds:
        return "stable_negative"
    if positive_folds > 0 and negative_folds > 0:
        return "unstable_mixed"
    if negative_folds > max_negative_folds:
        return "unstable_negative"
    return "insufficient_fold_support"


def _fold_bucket_cell(label: str, row: Mapping[str, object]) -> dict[str, object]:
    if not row:
        return {
            "fold": label,
            "samples": 0,
            "win_rate": 0.0,
            "avg_net_bps": 0.0,
            "delta_vs_all_avg_net_bps": 0.0,
            "classification": "missing",
        }
    return {
        "fold": label,
        "samples": int(_number(row.get("samples"))),
        "win_rate": round(_number(row.get("win_rate")), 6),
        "avg_net_bps": round(_number(row.get("avg_net_bps")), 6),
        "delta_vs_all_avg_net_bps": round(_number(row.get("delta_vs_all_avg_net_bps")), 6),
        "classification": str(row.get("classification", "") or ""),
    }


def _fold_bucket_sort_key(row: Mapping[str, object]) -> tuple[int, float, float, float, str, str]:
    class_rank = {
        "stable_positive": 0,
        "stable_negative": 1,
        "unstable_mixed": 2,
        "unstable_negative": 3,
        "insufficient_fold_support": 4,
    }.get(str(row.get("classification", "")), 5)
    return (
        class_rank,
        -abs(_number(row.get("avg_delta_vs_all_avg_net_bps"))),
        -_number(row.get("total_samples")),
        -_number(row.get("folds_seen")),
        str(row.get("family", "")),
        str(row.get("bucket", "")),
    )


def _fold_validation_recommendation(
    *,
    stable_positive_buckets: Sequence[Mapping[str, object]],
    stable_negative_buckets: Sequence[Mapping[str, object]],
) -> str:
    useful_risk_buckets = [
        row
        for row in stable_negative_buckets
        if row.get("family") in {"veto_signal", "conflict", "has_veto", "has_conflict"}
    ]
    if stable_positive_buckets and useful_risk_buckets:
        return "v10_candidate_multifold_promising_with_vetoes"
    if useful_risk_buckets:
        return "v10_candidate_multifold_guardrails_only"
    if stable_positive_buckets:
        return "v10_candidate_multifold_signal_only"
    if stable_negative_buckets:
        return "hold_zero_cost_multifold_digest_not_promising"
    return "hold_zero_cost_multifold_not_promising"


def _fold_validation_summary(
    folds: Sequence[Mapping[str, object]],
    bucket_rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    candidates_seen = sum(int(_number(fold.get("candidates_seen"))) for fold in folds)
    candidates_with_digest = sum(
        int(_number(fold.get("candidates_with_digest"))) for fold in folds
    )
    candidates_with_any_outcome = sum(
        int(_number(fold.get("candidates_with_any_outcome"))) for fold in folds
    )
    return {
        "folds": len(folds),
        "candidates_seen": candidates_seen,
        "candidates_with_digest": candidates_with_digest,
        "candidates_with_any_outcome": candidates_with_any_outcome,
        "digest_coverage_rate": round(candidates_with_digest / candidates_seen, 6)
        if candidates_seen
        else 0.0,
        "stable_positive_buckets": sum(
            1 for row in bucket_rows if row.get("classification") == "stable_positive"
        ),
        "stable_negative_buckets": sum(
            1 for row in bucket_rows if row.get("classification") == "stable_negative"
        ),
        "unstable_buckets": sum(
            1 for row in bucket_rows if str(row.get("classification", "")).startswith("unstable")
        ),
    }


def _technical_veto_fold_row(
    *,
    label: str,
    candidate_input_path: str | Path,
    market_input_path: str | Path,
    audit: Mapping[str, object],
    parsed_vetoes: Sequence[tuple[str, str]],
    min_delta_bps: float,
) -> dict[str, object]:
    horizon = int(_number(audit.get("best_horizon_minutes")))
    items = _mapping_list(audit.get("items"))
    matched: list[dict[str, object]] = []
    kept: list[dict[str, object]] = []
    match_counts: Counter[str] = Counter()
    for item in items:
        matches = _item_veto_matches(item, parsed_vetoes)
        if matches:
            item_with_matches = dict(item)
            item_with_matches["veto_matches"] = matches
            matched.append(item_with_matches)
            match_counts.update(matches)
        else:
            kept.append(item)
    baseline_summary = _outcome_summary(items, horizon=horizon)
    kept_summary = _outcome_summary(kept, horizon=horizon)
    vetoed_summary = _outcome_summary(matched, horizon=horizon)
    delta = _veto_delta_summary(kept_summary, baseline_summary)
    return {
        "fold": label,
        "candidate_input_path": str(candidate_input_path),
        "market_input_path": str(market_input_path),
        "best_horizon_minutes": horizon,
        "candidates_seen": int(_number(audit.get("candidates_seen"))),
        "candidates_vetoed": len(matched),
        "candidates_kept": len(kept),
        "veto_match_counts": dict(sorted(match_counts.items())),
        "baseline": baseline_summary,
        "kept": kept_summary,
        "vetoed": vetoed_summary,
        "delta": delta,
        "classification": _veto_fold_classification(
            candidates_vetoed=len(matched),
            delta_total_net_bps=_number(delta.get("total_net_bps")),
            min_delta_bps=min_delta_bps,
        ),
        "examples_vetoed": _veto_examples(matched),
    }


def _parse_veto_buckets(value: Sequence[str]) -> tuple[tuple[str, str], ...]:
    parsed: list[tuple[str, str]] = []
    for item in value:
        text = str(item or "").strip()
        if not text or "::" not in text:
            continue
        family, bucket = text.split("::", 1)
        family = family.strip()
        bucket = bucket.strip()
        if family and bucket and (family, bucket) not in parsed:
            parsed.append((family, bucket))
    return tuple(parsed)


def _format_veto_bucket(family: str, bucket: str) -> str:
    return f"{family}::{bucket}"


def _item_veto_matches(
    item: Mapping[str, object],
    parsed_vetoes: Sequence[tuple[str, str]],
) -> list[str]:
    wanted = set(parsed_vetoes)
    matches: list[str] = []
    for bucket in _technical_buckets(item):
        key = (bucket["family"], bucket["bucket"])
        if key in wanted:
            matches.append(_format_veto_bucket(*key))
    return matches


def _outcome_summary(items: Sequence[Mapping[str, object]], *, horizon: int) -> dict[str, object]:
    values: list[float] = []
    symbols: Counter[str] = Counter()
    sides: Counter[str] = Counter()
    for item in items:
        outcome = _outcome_for_horizon(item, horizon)
        if not outcome:
            continue
        values.append(_number(outcome.get("realized_net_bps")))
        symbol = str(item.get("symbol", "") or "")
        side = str(item.get("side", "") or "")
        if symbol:
            symbols[symbol] += 1
        if side:
            sides[side] += 1
    wins = [value for value in values if value > 0.0]
    return {
        "samples": len(values),
        "wins": len(wins),
        "win_rate": round(len(wins) / len(values), 6) if values else 0.0,
        "avg_net_bps": round(_average(values), 6),
        "median_net_bps": round(_median(values), 6),
        "total_net_bps": round(sum(values), 6),
        "symbols": dict(sorted(symbols.items())),
        "sides": dict(sorted(sides.items())),
    }


def _combine_outcome_summaries(summaries: Sequence[Mapping[str, object]]) -> dict[str, object]:
    summary_list = list(summaries)
    samples = sum(int(_number(summary.get("samples"))) for summary in summary_list)
    wins = sum(int(_number(summary.get("wins"))) for summary in summary_list)
    total_net = sum(_number(summary.get("total_net_bps")) for summary in summary_list)
    symbols: Counter[str] = Counter()
    sides: Counter[str] = Counter()
    for summary in summary_list:
        symbols.update(
            {
                str(symbol): int(_number(count))
                for symbol, count in _mapping(summary.get("symbols")).items()
            }
        )
        sides.update(
            {
                str(side): int(_number(count))
                for side, count in _mapping(summary.get("sides")).items()
            }
        )
    return {
        "samples": samples,
        "wins": wins,
        "win_rate": round(wins / samples, 6) if samples else 0.0,
        "avg_net_bps": round(total_net / samples, 6) if samples else 0.0,
        "total_net_bps": round(total_net, 6),
        "symbols": dict(sorted(symbols.items())),
        "sides": dict(sorted(sides.items())),
    }


def _veto_delta_summary(
    kept_summary: Mapping[str, object],
    baseline_summary: Mapping[str, object],
) -> dict[str, object]:
    return {
        "samples": int(_number(kept_summary.get("samples")))
        - int(_number(baseline_summary.get("samples"))),
        "wins": int(_number(kept_summary.get("wins")))
        - int(_number(baseline_summary.get("wins"))),
        "win_rate": round(
            _number(kept_summary.get("win_rate")) - _number(baseline_summary.get("win_rate")),
            6,
        ),
        "avg_net_bps": round(
            _number(kept_summary.get("avg_net_bps"))
            - _number(baseline_summary.get("avg_net_bps")),
            6,
        ),
        "total_net_bps": round(
            _number(kept_summary.get("total_net_bps"))
            - _number(baseline_summary.get("total_net_bps")),
            6,
        ),
    }


def _veto_fold_classification(
    *,
    candidates_vetoed: int,
    delta_total_net_bps: float,
    min_delta_bps: float,
) -> str:
    if candidates_vetoed <= 0:
        return "no_veto_hits"
    if delta_total_net_bps >= min_delta_bps:
        return "improved"
    if delta_total_net_bps <= -min_delta_bps:
        return "degraded"
    return "neutral"


def _veto_recommendation(
    *,
    delta_summary: Mapping[str, object],
    fold_rows: Sequence[Mapping[str, object]],
    min_delta_bps: float,
) -> str:
    candidates_vetoed = sum(int(_number(row.get("candidates_vetoed"))) for row in fold_rows)
    improved_folds = sum(1 for row in fold_rows if row.get("classification") == "improved")
    degraded_folds = sum(1 for row in fold_rows if row.get("classification") == "degraded")
    delta_total = _number(delta_summary.get("total_net_bps"))
    if candidates_vetoed <= 0:
        return "hold_veto_no_hits"
    if delta_total >= min_delta_bps and degraded_folds == 0 and improved_folds > 0:
        return "promote_candidate_veto_research"
    if delta_total >= min_delta_bps:
        return "candidate_veto_promising_but_fold_risk"
    if degraded_folds > 0:
        return "hold_veto_degrades"
    return "hold_veto_not_promising"


def _veto_examples(items: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    examples: list[dict[str, object]] = []
    for item in items[:10]:
        examples.append(
            {
                "timestamp": str(item.get("timestamp", "") or ""),
                "symbol": str(item.get("symbol", "") or ""),
                "side": str(item.get("side", "") or ""),
                "context_id": str(item.get("context_id", "") or ""),
                "veto_matches": _string_list(item.get("veto_matches")),
                "best_outcome": _mapping(item.get("best_outcome")),
            }
        )
    return examples


def _candidate_items(
    path: str | Path,
    *,
    feature_builder: TridentAIFeatureBuilder,
) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for row in _iter_jsonl(path):
        timestamp = str(row.get("timestamp", "") or "")
        regime = _record_regime(row)
        symbols = row.get("symbols", [])
        if not isinstance(symbols, list):
            continue
        for symbol_payload in symbols:
            if not isinstance(symbol_payload, Mapping):
                continue
            hint = _mapping(symbol_payload.get(CANDIDATE_HINT_FIELD))
            if not hint:
                continue
            symbol = str(hint.get("symbol", symbol_payload.get("symbol", "")) or "").strip().upper()
            item_timestamp = str(hint.get("timestamp", timestamp) or timestamp)
            item = {
                "timestamp": item_timestamp,
                "symbol": symbol,
                "context_id": str(hint.get("context_id", "") or ""),
                "side": str(hint.get("side", "") or "").strip().lower(),
                "price": _number(symbol_payload.get("price", hint.get("price"))),
                "score": _number(hint.get("score")),
                "estimated_edge_bps": _number(hint.get("estimated_edge_bps")),
                "round_trip_cost_bps": _number(hint.get("round_trip_cost_bps")),
                "estimated_net_edge_bps": _estimated_net_edge(hint),
                "edge_to_cost_ratio": _number(hint.get("edge_to_cost_ratio")),
                "candidate_reasons": _string_list(hint.get("reasons")),
                "tech": {},
                "tech_rejection_reason": "",
            }
            context_result = feature_builder.build_context_from_mapping(
                symbol_payload,
                as_of=item_timestamp,
                regime=regime,
                now=_parse_timestamp(item_timestamp),
            )
            if context_result.context is None:
                item["tech_rejection_reason"] = context_result.reason
            else:
                item["tech"] = compact_technical_digest(
                    context_result.context.features.get(TECHNICAL_DIGEST_FEATURE_NAME)
                )
            items.append(item)
    return items


def _estimated_net_edge(hint: Mapping[str, object]) -> float:
    value = _number(hint.get("estimated_net_edge_bps"))
    if value != 0.0:
        return value
    edge = _number(hint.get("estimated_edge_bps"))
    cost = _number(hint.get("round_trip_cost_bps"))
    return edge - cost if edge > 0.0 or cost > 0.0 else 0.0


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
            if not isinstance(symbol_payload, Mapping):
                continue
            symbol = str(symbol_payload.get("symbol", "") or "").strip().upper()
            price = _number(symbol_payload.get("price"))
            if symbol and price > 0.0:
                index[symbol].append((timestamp, timestamp_text, price))
    return {symbol: sorted(points, key=lambda point: point[0]) for symbol, points in index.items()}


def _candidate_horizon_outcome(
    item: Mapping[str, object],
    *,
    market_index: dict[str, list[tuple[datetime, str, float]]],
    horizon_minutes: int,
) -> dict[str, object]:
    timestamp = _parse_timestamp(str(item.get("timestamp", "") or ""))
    symbol = str(item.get("symbol", "") or "").strip().upper()
    side = str(item.get("side", "") or "").strip().lower()
    entry_price = _number(item.get("price"))
    round_trip_cost_bps = _number(item.get("round_trip_cost_bps"))
    if timestamp is None or entry_price <= 0.0 or symbol not in market_index:
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
    gross_bps = _gross_move_bps(side=side, entry_price=entry_price, future_price=future_price)
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
    }


def _horizon_stats(
    horizons: tuple[int, ...],
    accumulators: dict[int, list[float]],
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for horizon in horizons:
        values = accumulators.get(horizon, [])
        wins = [value for value in values if value > 0.0]
        result[str(horizon)] = {
            "samples": len(values),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(values), 6) if values else 0.0,
            "avg_net_bps": round(_average(values), 6),
            "median_net_bps": round(_median(values), 6),
            "total_net_bps": round(sum(values), 6),
        }
    return result


def _bucket_rows(
    *,
    items: list[dict[str, object]],
    horizon: int,
    baseline_avg_net_bps: float,
    min_bucket_samples: int,
    min_delta_bps: float,
) -> list[dict[str, object]]:
    if horizon <= 0:
        return []
    buckets: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for item in items:
        outcome = _outcome_for_horizon(item, horizon)
        if not outcome:
            continue
        for bucket in _technical_buckets(item):
            buckets[(bucket["family"], bucket["bucket"])].append(
                {
                    "net_bps": _number(outcome.get("realized_net_bps")),
                    "symbol": str(item.get("symbol", "") or ""),
                    "side": str(item.get("side", "") or ""),
                    "context_id": str(item.get("context_id", "") or ""),
                }
            )
    rows = [
        _bucket_row(
            family=family,
            bucket=bucket,
            samples=sample_rows,
            baseline_avg_net_bps=baseline_avg_net_bps,
            min_bucket_samples=min_bucket_samples,
            min_delta_bps=min_delta_bps,
        )
        for (family, bucket), sample_rows in buckets.items()
    ]
    return sorted(rows, key=_bucket_sort_key)


def _technical_buckets(item: Mapping[str, object]) -> list[dict[str, str]]:
    tech = _mapping(item.get("tech"))
    if not tech:
        return [{"family": "digest", "bucket": "missing"}]
    buckets: list[dict[str, str]] = []
    bias = _mapping(tech.get("bias"))
    bias_side = str(bias.get("side", "mixed") or "mixed")
    bias_quality = str(bias.get("quality", "unknown") or "unknown")
    candidate_side = str(item.get("side", "") or "").strip().lower()
    buckets.append({"family": "bias_side", "bucket": bias_side})
    buckets.append({"family": "bias_quality", "bucket": bias_quality})
    buckets.append(
        {
            "family": "candidate_vs_bias",
            "bucket": _candidate_vs_bias(candidate_side=candidate_side, bias_side=bias_side),
        }
    )
    families = _mapping(tech.get("families"))
    for family, state in families.items():
        buckets.append({"family": "family", "bucket": f"{family}={state}"})
    top_signals = _mapping_list(tech.get("top_signals"))
    veto_signals = _mapping_list(tech.get("veto_signals"))
    conflicts = _mapping_list(tech.get("conflicts"))
    for signal in top_signals:
        signal_id = str(signal.get("id", "") or "")
        if signal_id:
            buckets.append({"family": "top_signal", "bucket": signal_id})
    for signal in veto_signals:
        signal_id = str(signal.get("id", "") or "")
        if signal_id:
            buckets.append({"family": "veto_signal", "bucket": signal_id})
    for signal in conflicts:
        signal_id = str(signal.get("id", "") or "")
        if signal_id:
            buckets.append({"family": "conflict", "bucket": signal_id})
    buckets.append({"family": "has_veto", "bucket": str(bool(veto_signals)).lower()})
    buckets.append({"family": "has_conflict", "bucket": str(bool(conflicts)).lower()})
    return buckets


def _bucket_row(
    *,
    family: str,
    bucket: str,
    samples: list[dict[str, object]],
    baseline_avg_net_bps: float,
    min_bucket_samples: int,
    min_delta_bps: float,
) -> dict[str, object]:
    values = [_number(item.get("net_bps")) for item in samples]
    wins = [value for value in values if value > 0.0]
    avg_net = _average(values)
    delta = avg_net - baseline_avg_net_bps
    symbols = Counter(str(item.get("symbol", "") or "") for item in samples)
    sides = Counter(str(item.get("side", "") or "") for item in samples)
    return {
        "family": family,
        "bucket": bucket,
        "samples": len(samples),
        "wins": len(wins),
        "win_rate": round(len(wins) / len(samples), 6) if samples else 0.0,
        "avg_net_bps": round(avg_net, 6),
        "median_net_bps": round(_median(values), 6),
        "total_net_bps": round(sum(values), 6),
        "delta_vs_all_avg_net_bps": round(delta, 6),
        "classification": _classification(
            samples=len(samples),
            delta=delta,
            avg_net=avg_net,
            min_bucket_samples=min_bucket_samples,
            min_delta_bps=min_delta_bps,
        ),
        "symbols": dict(sorted(symbols.items())),
        "sides": dict(sorted(sides.items())),
        "example_context_ids": [
            str(item.get("context_id", "") or "")
            for item in samples[:5]
            if str(item.get("context_id", "") or "")
        ],
    }


def _classification(
    *,
    samples: int,
    delta: float,
    avg_net: float,
    min_bucket_samples: int,
    min_delta_bps: float,
) -> str:
    if samples < min_bucket_samples:
        return "thin"
    if delta >= min_delta_bps and avg_net > 0.0:
        return "positive_separator"
    if delta <= -min_delta_bps and avg_net <= 0.0:
        return "negative_separator"
    return "neutral"


def _recommendation(
    *,
    positive_buckets: Sequence[Mapping[str, object]],
    negative_buckets: Sequence[Mapping[str, object]],
    veto_or_conflict_buckets: Sequence[Mapping[str, object]],
) -> str:
    useful_risk_buckets = [
        row
        for row in veto_or_conflict_buckets
        if row.get("classification") == "negative_separator"
    ]
    if positive_buckets and useful_risk_buckets:
        return "v10_candidate_digest_promising_with_vetoes"
    if useful_risk_buckets:
        return "v10_candidate_guardrails_only"
    if positive_buckets:
        return "v10_candidate_signal_only"
    if negative_buckets:
        return "hold_zero_cost_digest_not_promising"
    return "hold_zero_cost"


def _summary(items: list[dict[str, object]], *, best_horizon: int) -> dict[str, object]:
    values: list[float] = []
    by_symbol: Counter[str] = Counter()
    by_side: Counter[str] = Counter()
    for item in items:
        by_symbol[str(item.get("symbol", "") or "")] += 1
        by_side[str(item.get("side", "") or "")] += 1
        outcome = _outcome_for_horizon(item, best_horizon)
        if outcome:
            values.append(_number(outcome.get("realized_net_bps")))
    wins = [value for value in values if value > 0.0]
    return {
        "samples": len(values),
        "wins": len(wins),
        "win_rate": round(len(wins) / len(values), 6) if values else 0.0,
        "avg_net_bps": round(_average(values), 6),
        "median_net_bps": round(_median(values), 6),
        "total_net_bps": round(sum(values), 6),
        "symbol_counts": dict(sorted(by_symbol.items())),
        "side_counts": dict(sorted(by_side.items())),
    }


def _best_horizon(horizon_stats: Mapping[str, Mapping[str, object]]) -> tuple[int, float]:
    best_horizon = 0
    best_avg_net = 0.0
    for horizon, stats in horizon_stats.items():
        samples = int(_number(stats.get("samples")))
        avg_net = _number(stats.get("avg_net_bps"))
        if samples <= 0:
            continue
        if best_horizon == 0 or avg_net > best_avg_net:
            best_horizon = int(horizon)
            best_avg_net = avg_net
    return best_horizon, best_avg_net


def _best_outcome(outcomes: Sequence[Mapping[str, object]]) -> dict[str, object]:
    available = [outcome for outcome in outcomes if bool(outcome.get("available", False))]
    if not available:
        return {}
    return dict(max(available, key=lambda item: _number(item.get("realized_net_bps"))))


def _outcome_for_horizon(item: Mapping[str, object], horizon: int) -> dict[str, object]:
    outcomes = item.get("outcomes", [])
    if not isinstance(outcomes, Sequence) or isinstance(outcomes, (str, bytes, bytearray)):
        return {}
    for outcome in outcomes:
        if not isinstance(outcome, Mapping):
            continue
        if int(_number(outcome.get("horizon_minutes"))) == horizon and bool(outcome.get("available")):
            return dict(outcome)
    return {}


def _candidate_vs_bias(*, candidate_side: str, bias_side: str) -> str:
    if bias_side in {"mixed", "neutral", "unknown", ""}:
        return "mixed"
    if not candidate_side:
        return "unknown"
    return "aligned" if candidate_side == bias_side else "conflict"


def _record_regime(row: Mapping[str, object]) -> str:
    regime_snapshot = _mapping(row.get("regime_snapshot"))
    for field_name in ("regime", "effective_regime", "regime_label"):
        value = regime_snapshot.get(field_name, row.get(field_name))
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def _bucket_sort_key(row: Mapping[str, object]) -> tuple[int, float, float, str, str]:
    class_rank = {
        "positive_separator": 0,
        "negative_separator": 1,
        "neutral": 2,
        "thin": 3,
    }.get(str(row.get("classification", "")), 4)
    return (
        class_rank,
        -abs(_number(row.get("delta_vs_all_avg_net_bps"))),
        -_number(row.get("samples")),
        str(row.get("family", "")),
        str(row.get("bucket", "")),
    )


def _average(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _mapping_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _string_list(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [str(item) for item in value if str(item)]


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


def _write_fold_validation_report_outputs(
    payload: dict[str, object],
    *,
    json_path: Path,
    md_path: Path,
) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(_render_fold_validation_markdown_report(payload), encoding="utf-8")


def _write_veto_audit_report_outputs(
    payload: dict[str, object],
    *,
    json_path: Path,
    md_path: Path,
) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(_render_veto_audit_markdown_report(payload), encoding="utf-8")


def _render_markdown_report(payload: dict[str, object]) -> str:
    result = payload["result"]
    assert isinstance(result, dict)
    lines = [
        "# TRIDENT-AI Technical Digest Audit",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Candidate input: `{result['candidate_input_path']}`",
        f"- Market input: `{result['market_input_path']}`",
        f"- Horizons minutes: `{result['horizons_minutes']}`",
        f"- Candidates seen: `{result['candidates_seen']}`",
        f"- Candidates with digest: `{result['candidates_with_digest']}`",
        f"- Candidates with outcome: `{result['candidates_with_any_outcome']}`",
        f"- Missing digest: `{result['missing_digest']}`",
        f"- Missing outcomes: `{result['missing_outcomes']}`",
        f"- Best horizon: `{result['best_horizon_minutes']}m`",
        f"- Best horizon avg net: `{result['best_horizon_avg_net_bps']:.4f} bps`",
        f"- Recommendation: `{result['recommendation']}`",
        "",
        "## Horizon Stats",
        "",
        "| Horizon | Samples | Win Rate | Avg Net | Median Net | Total Net |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for horizon, stats in _mapping(result.get("horizon_stats")).items():
        assert isinstance(stats, Mapping)
        lines.append(
            f"| {horizon}m | {stats['samples']} | {stats['win_rate']:.2%} | "
            f"{stats['avg_net_bps']:.2f} | {stats['median_net_bps']:.2f} | "
            f"{stats['total_net_bps']:.2f} |"
        )
    lines.extend(["", "## Separating Buckets", ""])
    _append_bucket_table(lines, "Positive", result.get("positive_buckets", []))
    _append_bucket_table(lines, "Negative", result.get("negative_buckets", []))
    _append_bucket_table(lines, "Veto Or Conflict", result.get("veto_or_conflict_buckets", []))
    lines.append("")
    return "\n".join(lines)


def _render_veto_audit_markdown_report(payload: dict[str, object]) -> str:
    result = payload["result"]
    assert isinstance(result, dict)
    baseline = _mapping(result.get("baseline_summary"))
    kept = _mapping(result.get("kept_summary"))
    vetoed = _mapping(result.get("vetoed_summary"))
    delta = _mapping(result.get("delta_summary"))
    lines = [
        "# TRIDENT-AI Technical Digest Veto Audit",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Candidate inputs: `{result['candidate_input_paths']}`",
        f"- Market inputs: `{result['market_input_paths']}`",
        f"- Fold labels: `{result['fold_labels']}`",
        f"- Veto buckets: `{result['veto_buckets']}`",
        f"- Horizons minutes: `{result['horizons_minutes']}`",
        f"- Min delta: `{result['min_delta_bps']:.4f} bps`",
        f"- Recommendation: `{result['recommendation']}`",
        "",
        "## Aggregate",
        "",
        "| Slice | Samples | Wins | Win Rate | Avg Net | Total Net |",
        "|---|---:|---:|---:|---:|---:|",
        _summary_md_row("Baseline", baseline),
        _summary_md_row("Kept", kept),
        _summary_md_row("Vetoed", vetoed),
        (
            f"| Delta kept-baseline | {int(_number(delta.get('samples')))} | "
            f"{int(_number(delta.get('wins')))} | {_number(delta.get('win_rate')):.2%} | "
            f"{_number(delta.get('avg_net_bps')):.2f} | "
            f"{_number(delta.get('total_net_bps')):.2f} |"
        ),
        "",
        "## Folds",
        "",
        "| Fold | Vetoed | Horizon | Baseline Total | Kept Total | Delta Total | Class | Matches |",
        "|---|---:|---:|---:|---:|---:|---|---|",
    ]
    for row in _mapping_list(result.get("fold_rows")):
        match_counts = _mapping(row.get("veto_match_counts"))
        lines.append(
            f"| {row.get('fold', '')} | {int(_number(row.get('candidates_vetoed')))} | "
            f"{int(_number(row.get('best_horizon_minutes')))}m | "
            f"{_number(_mapping(row.get('baseline')).get('total_net_bps')):.2f} | "
            f"{_number(_mapping(row.get('kept')).get('total_net_bps')):.2f} | "
            f"{_number(_mapping(row.get('delta')).get('total_net_bps')):.2f} | "
            f"{row.get('classification', '')} | {dict(sorted(match_counts.items()))} |"
        )
    if not _mapping_list(result.get("fold_rows")):
        lines.append("| none | 0 | 0m | 0.00 | 0.00 | 0.00 | n/a | {} |")
    lines.append("")
    return "\n".join(lines)


def _summary_md_row(label: str, summary: Mapping[str, object]) -> str:
    return (
        f"| {label} | {int(_number(summary.get('samples')))} | "
        f"{int(_number(summary.get('wins')))} | {_number(summary.get('win_rate')):.2%} | "
        f"{_number(summary.get('avg_net_bps')):.2f} | "
        f"{_number(summary.get('total_net_bps')):.2f} |"
    )


def _render_fold_validation_markdown_report(payload: dict[str, object]) -> str:
    result = payload["result"]
    assert isinstance(result, dict)
    summary = _mapping(result.get("summary"))
    lines = [
        "# TRIDENT-AI Technical Digest Fold Validation",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Candidate inputs: `{result['candidate_input_paths']}`",
        f"- Market inputs: `{result['market_input_paths']}`",
        f"- Fold labels: `{result['fold_labels']}`",
        f"- Horizons minutes: `{result['horizons_minutes']}`",
        f"- Min bucket samples: `{result['min_bucket_samples']}`",
        f"- Min delta: `{result['min_delta_bps']:.4f} bps`",
        f"- Min positive folds: `{result['min_positive_folds']}`",
        f"- Max negative folds: `{result['max_negative_folds']}`",
        f"- Recommendation: `{result['recommendation']}`",
        f"- Candidates with digest: `{summary.get('candidates_with_digest', 0)}` / "
        f"`{summary.get('candidates_seen', 0)}`",
        f"- Stable positive buckets: `{summary.get('stable_positive_buckets', 0)}`",
        f"- Stable negative buckets: `{summary.get('stable_negative_buckets', 0)}`",
        f"- Unstable buckets: `{summary.get('unstable_buckets', 0)}`",
        "",
        "## Folds",
        "",
        "| Fold | Candidates | Digest | Outcome | Best Horizon | Avg Net | Pos Buckets | Neg Buckets | Recommendation |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    folds = _mapping_list(result.get("folds"))
    for fold in folds:
        lines.append(
            f"| {fold.get('fold', '')} | {int(_number(fold.get('candidates_seen')))} | "
            f"{int(_number(fold.get('candidates_with_digest')))} | "
            f"{int(_number(fold.get('candidates_with_any_outcome')))} | "
            f"{int(_number(fold.get('best_horizon_minutes')))}m | "
            f"{_number(fold.get('best_horizon_avg_net_bps')):.2f} | "
            f"{int(_number(fold.get('positive_buckets')))} | "
            f"{int(_number(fold.get('negative_buckets')))} | "
            f"{fold.get('recommendation', '')} |"
        )
    if not folds:
        lines.append("| none | 0 | 0 | 0 | 0m | 0.00 | 0 | 0 | n/a |")
    lines.extend(["", "## Stable Buckets", ""])
    _append_fold_bucket_table(lines, "Stable Positive", result.get("stable_positive_buckets", []))
    _append_fold_bucket_table(lines, "Stable Negative", result.get("stable_negative_buckets", []))
    _append_fold_bucket_table(lines, "Unstable", result.get("unstable_buckets", []))
    lines.append("")
    return "\n".join(lines)


def _append_bucket_table(lines: list[str], title: str, rows_value: object) -> None:
    rows = _mapping_list(rows_value)
    lines.extend(
        [
            f"### {title}",
            "",
            "| Family | Bucket | Samples | Win Rate | Avg Net | Delta | Class |",
            "|---|---|---:|---:|---:|---:|---|",
        ]
    )
    for row in rows[:30]:
        lines.append(
            f"| {row.get('family', '')} | {row.get('bucket', '')} | "
            f"{int(_number(row.get('samples')))} | {_number(row.get('win_rate')):.2%} | "
            f"{_number(row.get('avg_net_bps')):.2f} | "
            f"{_number(row.get('delta_vs_all_avg_net_bps')):.2f} | "
            f"{row.get('classification', '')} |"
        )
    if not rows:
        lines.append("| none | n/a | 0 | 0.00% | 0.00 | 0.00 | n/a |")
    lines.append("")


def _append_fold_bucket_table(lines: list[str], title: str, rows_value: object) -> None:
    rows = _mapping_list(rows_value)
    lines.extend(
        [
            f"### {title}",
            "",
            "| Family | Bucket | Folds | +/- | Samples | Win Rate | Avg Net | Avg Delta | Class |",
            "|---|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in rows[:30]:
        lines.append(
            f"| {row.get('family', '')} | {row.get('bucket', '')} | "
            f"{int(_number(row.get('folds_seen')))} | "
            f"{int(_number(row.get('positive_folds')))}/"
            f"{int(_number(row.get('negative_folds')))} | "
            f"{int(_number(row.get('total_samples')))} | "
            f"{_number(row.get('win_rate')):.2%} | "
            f"{_number(row.get('avg_net_bps')):.2f} | "
            f"{_number(row.get('avg_delta_vs_all_avg_net_bps')):.2f} | "
            f"{row.get('classification', '')} |"
        )
    if not rows:
        lines.append("| none | n/a | 0 | 0/0 | 0 | 0.00% | 0.00 | 0.00 | n/a |")
    lines.append("")
