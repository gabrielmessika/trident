from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import product
from pathlib import Path

from app.trident_ai.candidate_paper import (
    DEFAULT_CANDIDATE_PAPER_CONFIDENCE,
    DEFAULT_CANDIDATE_PAPER_STOP_BPS,
    DEFAULT_CANDIDATE_PAPER_TAKE_PROFIT_BPS,
    DEFAULT_CANDIDATE_PAPER_TIME_STOP_MINUTES,
    TridentAICandidatePaperReplayResult,
    run_trident_ai_candidate_paper_replay,
)
from app.trident_ai.config import TridentAIConfig, load_trident_ai_config
from app.trident_ai.paper import PAPER_REPLAY_TRADE_CLOSED_EVENT, TridentAIPaperReplayRunner
from app.trident_ai.pattern_calibration import _format_timestamp, _mapping, _number, _timestamp_id


DEFAULT_GATE_SWEEP_MIN_EDGE_TO_COST_VALUES: tuple[float, ...] = (2.5, 3.0, 3.5, 4.0)
DEFAULT_GATE_SWEEP_MIN_NET_EDGE_BPS_VALUES: tuple[float, ...] = (15.0, 25.0, 35.0)
DEFAULT_GATE_SWEEP_MIN_LIQUIDITY_SCORE_VALUES: tuple[float, ...] = (1.0, 1.2)
DEFAULT_GATE_SWEEP_MAX_ROUND_TRIP_COST_BPS_VALUES: tuple[float, ...] = (12.0, 16.0)
DEFAULT_GATE_SWEEP_MIN_PATTERN_QUALITY_SCORE_VALUES: tuple[float | None, ...] = (None,)
DEFAULT_GATE_SWEEP_MIN_TOTAL_CLOSED_TRADES = 4
DEFAULT_GATE_SWEEP_MIN_SYMBOLS = 2
DEFAULT_GATE_SWEEP_MAX_NEGATIVE_FOLDS = 0
DEFAULT_GATE_SWEEP_MAX_CATASTROPHIC_NET_BPS = 50.0
DEFAULT_GATE_SWEEP_MAX_DOMINANT_SYMBOL_RATIO = 1.0
DEFAULT_GATE_SWEEP_OOS_NO_TRADE_PENALTY_BPS = 25.0
DEFAULT_GATE_SWEEP_NEGATIVE_FOLD_PENALTY_BPS = 10.0
DEFAULT_GATE_SWEEP_CATASTROPHIC_FOLD_PENALTY_BPS = 50.0
DEFAULT_GATE_SWEEP_MICRO_REGIME_PROFILE_VALUES: tuple[str, ...] = ("none",)
MICRO_REGIME_PROFILE_DEFINITIONS: dict[str, dict[str, tuple[str, ...]]] = {
    "none": {
        "veto_buckets": (),
        "require_buckets": (),
        "size_scales": (),
    },
    "veto_range_mid_vol_high": {
        "veto_buckets": ("range_vol_regime::range_mid|vol_high",),
        "require_buckets": (),
        "size_scales": (),
    },
    "veto_range_mid_vol_high_size_micro_adverse": {
        "veto_buckets": ("range_vol_regime::range_mid|vol_high",),
        "require_buckets": (),
        "size_scales": ("microprice_bucket::micro_adverse=0.5",),
    },
    "low_vol_support": {
        "veto_buckets": (),
        "require_buckets": (
            "range_vol_regime::range_low|vol_controlled",
            "range_vol_regime::range_mid|vol_controlled",
            "symbol_range_vol::HYPE|range_low|vol_controlled",
            "symbol_range_vol::HYPE|range_mid|vol_controlled",
        ),
        "size_scales": (),
    },
    "low_vol_support_size_micro_adverse": {
        "veto_buckets": (),
        "require_buckets": (
            "range_vol_regime::range_low|vol_controlled",
            "range_vol_regime::range_mid|vol_controlled",
            "symbol_range_vol::HYPE|range_low|vol_controlled",
            "symbol_range_vol::HYPE|range_mid|vol_controlled",
        ),
        "size_scales": ("microprice_bucket::micro_adverse=0.5",),
    },
}


@dataclass(frozen=True, slots=True)
class TridentAICandidateGateSweepResult:
    candidate_input_paths: tuple[str, ...]
    market_input_paths: tuple[str, ...]
    fold_labels: tuple[str, ...]
    oos_fold_labels: tuple[str, ...]
    report_json_path: str
    report_md_path: str
    artifact_dir: str
    symbols_filter: tuple[str, ...] = ()
    profile_count: int = 0
    profiles_evaluated: int = 0
    min_total_closed_trades: int = DEFAULT_GATE_SWEEP_MIN_TOTAL_CLOSED_TRADES
    min_symbols: int = DEFAULT_GATE_SWEEP_MIN_SYMBOLS
    max_negative_folds: int = DEFAULT_GATE_SWEEP_MAX_NEGATIVE_FOLDS
    max_catastrophic_net_bps: float = DEFAULT_GATE_SWEEP_MAX_CATASTROPHIC_NET_BPS
    max_dominant_symbol_ratio: float = DEFAULT_GATE_SWEEP_MAX_DOMINANT_SYMBOL_RATIO
    oos_no_trade_penalty_bps: float = DEFAULT_GATE_SWEEP_OOS_NO_TRADE_PENALTY_BPS
    negative_fold_penalty_bps: float = DEFAULT_GATE_SWEEP_NEGATIVE_FOLD_PENALTY_BPS
    catastrophic_fold_penalty_bps: float = DEFAULT_GATE_SWEEP_CATASTROPHIC_FOLD_PENALTY_BPS
    technical_veto_buckets: tuple[str, ...] = ()
    micro_regime_profile_values: tuple[str, ...] = DEFAULT_GATE_SWEEP_MICRO_REGIME_PROFILE_VALUES
    cache_market_events: bool = False
    allow_guardrail_loss_avoidance: bool = False
    guardrail_fold_labels: tuple[str, ...] = ()
    threshold_values: dict[str, list[float]] = field(default_factory=dict)
    best_profile: dict[str, object] = field(default_factory=dict)
    best_robust_profile: dict[str, object] = field(default_factory=dict)
    best_guardrail_aware_profile: dict[str, object] = field(default_factory=dict)
    classification_counts: dict[str, int] = field(default_factory=dict)
    profile_rows: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_input_paths": list(self.candidate_input_paths),
            "market_input_paths": list(self.market_input_paths),
            "fold_labels": list(self.fold_labels),
            "oos_fold_labels": list(self.oos_fold_labels),
            "report_json_path": self.report_json_path,
            "report_md_path": self.report_md_path,
            "artifact_dir": self.artifact_dir,
            "symbols_filter": list(self.symbols_filter),
            "profile_count": self.profile_count,
            "profiles_evaluated": self.profiles_evaluated,
            "min_total_closed_trades": self.min_total_closed_trades,
            "min_symbols": self.min_symbols,
            "max_negative_folds": self.max_negative_folds,
            "max_catastrophic_net_bps": round(self.max_catastrophic_net_bps, 6),
            "max_dominant_symbol_ratio": round(self.max_dominant_symbol_ratio, 6),
            "oos_no_trade_penalty_bps": round(self.oos_no_trade_penalty_bps, 6),
            "negative_fold_penalty_bps": round(self.negative_fold_penalty_bps, 6),
            "catastrophic_fold_penalty_bps": round(self.catastrophic_fold_penalty_bps, 6),
            "technical_veto_buckets": list(self.technical_veto_buckets),
            "micro_regime_profile_values": list(self.micro_regime_profile_values),
            "cache_market_events": self.cache_market_events,
            "allow_guardrail_loss_avoidance": self.allow_guardrail_loss_avoidance,
            "guardrail_fold_labels": list(self.guardrail_fold_labels),
            "threshold_values": self.threshold_values,
            "best_profile": self.best_profile,
            "best_robust_profile": self.best_robust_profile,
            "best_guardrail_aware_profile": self.best_guardrail_aware_profile,
            "classification_counts": dict(sorted(self.classification_counts.items())),
            "profile_rows": self.profile_rows,
        }


def run_trident_ai_candidate_gate_sweep(
    *,
    candidate_input_paths: Sequence[str | Path],
    market_input_paths: Sequence[str | Path],
    fold_labels: Sequence[str] | None = None,
    oos_fold_labels: Sequence[str] | None = None,
    config: TridentAIConfig | None = None,
    report_json_path: str | Path | None = None,
    report_md_path: str | Path | None = None,
    artifact_dir: str | Path | None = None,
    symbols: Sequence[str] | None = None,
    notional_usd: float | None = None,
    confidence: float = DEFAULT_CANDIDATE_PAPER_CONFIDENCE,
    stop_bps: float = DEFAULT_CANDIDATE_PAPER_STOP_BPS,
    take_profit_bps: float = DEFAULT_CANDIDATE_PAPER_TAKE_PROFIT_BPS,
    time_stop_minutes: int = DEFAULT_CANDIDATE_PAPER_TIME_STOP_MINUTES,
    min_edge_to_cost_values: Sequence[float] = DEFAULT_GATE_SWEEP_MIN_EDGE_TO_COST_VALUES,
    min_net_edge_bps_values: Sequence[float] = DEFAULT_GATE_SWEEP_MIN_NET_EDGE_BPS_VALUES,
    min_liquidity_score_values: Sequence[float] = DEFAULT_GATE_SWEEP_MIN_LIQUIDITY_SCORE_VALUES,
    max_round_trip_cost_bps_values: Sequence[float] = DEFAULT_GATE_SWEEP_MAX_ROUND_TRIP_COST_BPS_VALUES,
    min_pattern_quality_score_values: Sequence[float | None] = (
        DEFAULT_GATE_SWEEP_MIN_PATTERN_QUALITY_SCORE_VALUES
    ),
    max_profiles: int | None = None,
    min_total_closed_trades: int = DEFAULT_GATE_SWEEP_MIN_TOTAL_CLOSED_TRADES,
    min_symbols: int = DEFAULT_GATE_SWEEP_MIN_SYMBOLS,
    max_negative_folds: int = DEFAULT_GATE_SWEEP_MAX_NEGATIVE_FOLDS,
    max_catastrophic_net_bps: float = DEFAULT_GATE_SWEEP_MAX_CATASTROPHIC_NET_BPS,
    max_dominant_symbol_ratio: float = DEFAULT_GATE_SWEEP_MAX_DOMINANT_SYMBOL_RATIO,
    oos_no_trade_penalty_bps: float = DEFAULT_GATE_SWEEP_OOS_NO_TRADE_PENALTY_BPS,
    negative_fold_penalty_bps: float = DEFAULT_GATE_SWEEP_NEGATIVE_FOLD_PENALTY_BPS,
    catastrophic_fold_penalty_bps: float = DEFAULT_GATE_SWEEP_CATASTROPHIC_FOLD_PENALTY_BPS,
    technical_veto_buckets: Sequence[str] | None = None,
    micro_regime_profile_values: Sequence[str] = DEFAULT_GATE_SWEEP_MICRO_REGIME_PROFILE_VALUES,
    cache_market_events: bool = False,
    allow_guardrail_loss_avoidance: bool = False,
    guardrail_fold_labels: Sequence[str] | None = None,
) -> TridentAICandidateGateSweepResult:
    _validate_inputs(
        candidate_input_paths=candidate_input_paths,
        market_input_paths=market_input_paths,
        fold_labels=fold_labels,
        min_edge_to_cost_values=min_edge_to_cost_values,
        min_net_edge_bps_values=min_net_edge_bps_values,
        min_liquidity_score_values=min_liquidity_score_values,
        max_round_trip_cost_bps_values=max_round_trip_cost_bps_values,
        min_pattern_quality_score_values=min_pattern_quality_score_values,
        max_profiles=max_profiles,
        min_total_closed_trades=min_total_closed_trades,
        min_symbols=min_symbols,
        max_negative_folds=max_negative_folds,
        max_catastrophic_net_bps=max_catastrophic_net_bps,
        max_dominant_symbol_ratio=max_dominant_symbol_ratio,
        oos_no_trade_penalty_bps=oos_no_trade_penalty_bps,
        negative_fold_penalty_bps=negative_fold_penalty_bps,
        catastrophic_fold_penalty_bps=catastrophic_fold_penalty_bps,
        micro_regime_profile_values=micro_regime_profile_values,
    )
    technical_vetoes = _technical_veto_buckets(technical_veto_buckets)
    micro_regime_profiles = _micro_regime_profile_values(micro_regime_profile_values)
    guardrail_labels = _guardrail_fold_labels(guardrail_fold_labels)
    active_config = config or load_trident_ai_config()
    run_id = _timestamp_id(datetime.now(timezone.utc))
    output_dir = Path(active_config.paths.replay_output_dir)
    json_output = Path(report_json_path or output_dir / f"trident_ai_candidate_gate_sweep_{run_id}.json")
    md_output = Path(report_md_path or output_dir / f"trident_ai_candidate_gate_sweep_{run_id}.md")
    artifacts = Path(artifact_dir or output_dir / f"{json_output.stem}_artifacts")
    labels = _fold_labels(fold_labels, len(candidate_input_paths))
    oos_labels = _oos_labels(oos_fold_labels, labels)
    symbols_filter = _symbols_filter(symbols)
    market_event_caches = (
        _market_event_caches_by_fold(
            candidate_input_paths=candidate_input_paths,
            market_input_paths=market_input_paths,
            config=active_config,
            symbols_filter=symbols_filter,
        )
        if cache_market_events
        else {}
    )

    profiles = _profile_grid(
        min_edge_to_cost_values=min_edge_to_cost_values,
        min_net_edge_bps_values=min_net_edge_bps_values,
        min_liquidity_score_values=min_liquidity_score_values,
        max_round_trip_cost_bps_values=max_round_trip_cost_bps_values,
        min_pattern_quality_score_values=min_pattern_quality_score_values,
        micro_regime_profile_values=micro_regime_profiles,
    )
    profile_count = len(profiles)
    if max_profiles is not None:
        profiles = profiles[:max_profiles]

    rows: list[dict[str, object]] = []
    for index, profile in enumerate(profiles, start=1):
        fold_rows: list[dict[str, object]] = []
        for fold_index, (label, candidate_path, market_path) in enumerate(
            zip(labels, candidate_input_paths, market_input_paths, strict=True)
        ):
            prefix = artifacts / f"profile_{index:03d}_{profile['profile_id']}_{_safe_name(label)}"
            replay_result = run_trident_ai_candidate_paper_replay(
                candidate_path,
                market_input_path=market_path,
                config=active_config,
                decision_journal_path=prefix.with_name(f"{prefix.name}_decisions.jsonl"),
                journal_path=prefix.with_name(f"{prefix.name}_paper.jsonl"),
                report_json_path=prefix.with_name(f"{prefix.name}.json"),
                report_md_path=prefix.with_name(f"{prefix.name}.md"),
                symbols=symbols_filter,
                notional_usd=notional_usd,
                confidence=confidence,
                stop_bps=stop_bps,
                take_profit_bps=take_profit_bps,
                time_stop_minutes=time_stop_minutes,
                min_edge_to_cost=profile["min_edge_to_cost"],
                min_net_edge_bps=profile["min_net_edge_bps"],
                min_liquidity_score=profile["min_liquidity_score"],
                max_round_trip_cost_bps=profile["max_round_trip_cost_bps"],
                min_pattern_quality_score=profile["min_pattern_quality_score"],
                technical_veto_buckets=technical_vetoes,
                micro_regime_veto_buckets=tuple(profile["micro_regime_veto_buckets"]),
                micro_regime_require_buckets=tuple(profile["micro_regime_require_buckets"]),
                micro_regime_size_scales=tuple(profile["micro_regime_size_scales"]),
                paper_market_event_cache=market_event_caches.get(fold_index),
            )
            fold_row = _fold_row(label, replay_result, is_oos=label in oos_labels)
            if (
                allow_guardrail_loss_avoidance
                and technical_vetoes
                and label in guardrail_labels
                and int(_number(fold_row.get("closed_trades"))) == 0
            ):
                baseline_prefix = artifacts / (
                    f"profile_{index:03d}_{profile['profile_id']}_{_safe_name(label)}_"
                    "guardrail_baseline"
                )
                baseline_result = run_trident_ai_candidate_paper_replay(
                    candidate_path,
                    market_input_path=market_path,
                    config=active_config,
                    decision_journal_path=baseline_prefix.with_name(
                        f"{baseline_prefix.name}_decisions.jsonl"
                    ),
                    journal_path=baseline_prefix.with_name(f"{baseline_prefix.name}_paper.jsonl"),
                    report_json_path=baseline_prefix.with_name(f"{baseline_prefix.name}.json"),
                    report_md_path=baseline_prefix.with_name(f"{baseline_prefix.name}.md"),
                    symbols=symbols_filter,
                    notional_usd=notional_usd,
                    confidence=confidence,
                    stop_bps=stop_bps,
                    take_profit_bps=take_profit_bps,
                    time_stop_minutes=time_stop_minutes,
                    min_edge_to_cost=profile["min_edge_to_cost"],
                    min_net_edge_bps=profile["min_net_edge_bps"],
                    min_liquidity_score=profile["min_liquidity_score"],
                    max_round_trip_cost_bps=profile["max_round_trip_cost_bps"],
                    min_pattern_quality_score=profile["min_pattern_quality_score"],
                    technical_veto_buckets=(),
                    micro_regime_veto_buckets=tuple(profile["micro_regime_veto_buckets"]),
                    micro_regime_require_buckets=tuple(profile["micro_regime_require_buckets"]),
                    micro_regime_size_scales=tuple(profile["micro_regime_size_scales"]),
                    paper_market_event_cache=market_event_caches.get(fold_index),
                )
                baseline_row = _fold_row(label, baseline_result, is_oos=label in oos_labels)
                fold_row["guardrail_baseline"] = baseline_row
                fold_row["guardrail_loss_avoidance"] = _is_guardrail_loss_avoidance(
                    fold_row=fold_row,
                    baseline_row=baseline_row,
                )
            else:
                fold_row["guardrail_baseline"] = {}
                fold_row["guardrail_loss_avoidance"] = False
            fold_rows.append(fold_row)
        rows.append(
            _profile_row(
                profile=profile,
                folds=fold_rows,
                oos_fold_labels=oos_labels,
                min_total_closed_trades=min_total_closed_trades,
                min_symbols=min_symbols,
                max_negative_folds=max_negative_folds,
                max_catastrophic_net_bps=max_catastrophic_net_bps,
                max_dominant_symbol_ratio=max_dominant_symbol_ratio,
                oos_no_trade_penalty_bps=oos_no_trade_penalty_bps,
                negative_fold_penalty_bps=negative_fold_penalty_bps,
                catastrophic_fold_penalty_bps=catastrophic_fold_penalty_bps,
                allow_guardrail_loss_avoidance=allow_guardrail_loss_avoidance,
            )
        )

    rows.sort(key=_profile_sort_key)
    classification_counts = Counter(str(row.get("classification", "")) for row in rows)
    robust_rows = [row for row in rows if row.get("classification") == "robust_candidate"]
    guardrail_rows = [
        row
        for row in rows
        if row.get("classification") == "guardrail_loss_avoidance_candidate"
    ]
    result = TridentAICandidateGateSweepResult(
        candidate_input_paths=tuple(str(path) for path in candidate_input_paths),
        market_input_paths=tuple(str(path) for path in market_input_paths),
        fold_labels=labels,
        oos_fold_labels=oos_labels,
        report_json_path=str(json_output),
        report_md_path=str(md_output),
        artifact_dir=str(artifacts),
        symbols_filter=symbols_filter,
        profile_count=profile_count,
        profiles_evaluated=len(rows),
        min_total_closed_trades=min_total_closed_trades,
        min_symbols=min_symbols,
        max_negative_folds=max_negative_folds,
        max_catastrophic_net_bps=max_catastrophic_net_bps,
        max_dominant_symbol_ratio=max_dominant_symbol_ratio,
        oos_no_trade_penalty_bps=oos_no_trade_penalty_bps,
        negative_fold_penalty_bps=negative_fold_penalty_bps,
        catastrophic_fold_penalty_bps=catastrophic_fold_penalty_bps,
        technical_veto_buckets=technical_vetoes,
        micro_regime_profile_values=micro_regime_profiles,
        cache_market_events=cache_market_events,
        allow_guardrail_loss_avoidance=allow_guardrail_loss_avoidance,
        guardrail_fold_labels=guardrail_labels,
        threshold_values={
            "min_edge_to_cost": [float(value) for value in min_edge_to_cost_values],
            "min_net_edge_bps": [float(value) for value in min_net_edge_bps_values],
            "min_liquidity_score": [float(value) for value in min_liquidity_score_values],
            "max_round_trip_cost_bps": [float(value) for value in max_round_trip_cost_bps_values],
            "min_pattern_quality_score": [
                None if value is None else float(value)
                for value in min_pattern_quality_score_values
            ],
            "micro_regime_profile": list(micro_regime_profiles),
        },
        best_profile=rows[0] if rows else {},
        best_robust_profile=robust_rows[0] if robust_rows else {},
        best_guardrail_aware_profile=guardrail_rows[0] if guardrail_rows else {},
        classification_counts=dict(classification_counts),
        profile_rows=rows,
    )
    payload = build_candidate_gate_sweep_report_payload(
        result=result,
        generated_at=_format_timestamp(datetime.now(timezone.utc)),
    )
    _write_report_outputs(payload, json_path=json_output, md_path=md_output)
    return result


def build_candidate_gate_sweep_report_payload(
    *,
    result: TridentAICandidateGateSweepResult,
    generated_at: str,
) -> dict[str, object]:
    return {
        "generated_at": generated_at,
        "kind": "trident_ai_candidate_gate_sweep",
        "result": result.to_dict(),
    }


def _validate_inputs(
    *,
    candidate_input_paths: Sequence[str | Path],
    market_input_paths: Sequence[str | Path],
    fold_labels: Sequence[str] | None,
    min_edge_to_cost_values: Sequence[float],
    min_net_edge_bps_values: Sequence[float],
    min_liquidity_score_values: Sequence[float],
    max_round_trip_cost_bps_values: Sequence[float],
    min_pattern_quality_score_values: Sequence[float | None],
    max_profiles: int | None,
    min_total_closed_trades: int,
    min_symbols: int,
    max_negative_folds: int,
    max_catastrophic_net_bps: float,
    max_dominant_symbol_ratio: float,
    oos_no_trade_penalty_bps: float,
    negative_fold_penalty_bps: float,
    catastrophic_fold_penalty_bps: float,
    micro_regime_profile_values: Sequence[str],
) -> None:
    if not candidate_input_paths:
        raise ValueError("candidate_input_paths_required")
    if len(candidate_input_paths) != len(market_input_paths):
        raise ValueError("candidate_and_market_input_counts_must_match")
    if fold_labels is not None and len(fold_labels) != len(candidate_input_paths):
        raise ValueError("fold_label_count_must_match_input_count")
    for name, values in (
        ("min_edge_to_cost_values", min_edge_to_cost_values),
        ("min_net_edge_bps_values", min_net_edge_bps_values),
        ("min_liquidity_score_values", min_liquidity_score_values),
        ("max_round_trip_cost_bps_values", max_round_trip_cost_bps_values),
        ("min_pattern_quality_score_values", min_pattern_quality_score_values),
    ):
        if not values:
            raise ValueError(f"{name}_required")
        if any(value is not None and float(value) < 0.0 for value in values):
            raise ValueError(f"{name}_must_be_non_negative")
    if any(float(value) <= 0.0 for value in max_round_trip_cost_bps_values):
        raise ValueError("max_round_trip_cost_bps_values_must_be_positive")
    if max_profiles is not None and max_profiles <= 0:
        raise ValueError("max_profiles_must_be_positive")
    if min_total_closed_trades <= 0:
        raise ValueError("min_total_closed_trades_must_be_positive")
    if min_symbols <= 0:
        raise ValueError("min_symbols_must_be_positive")
    if max_negative_folds < 0:
        raise ValueError("max_negative_folds_must_be_non_negative")
    if max_catastrophic_net_bps <= 0.0:
        raise ValueError("max_catastrophic_net_bps_must_be_positive")
    if max_dominant_symbol_ratio <= 0.0 or max_dominant_symbol_ratio > 1.0:
        raise ValueError("max_dominant_symbol_ratio_must_be_between_0_and_1")
    if oos_no_trade_penalty_bps < 0.0:
        raise ValueError("oos_no_trade_penalty_bps_must_be_non_negative")
    if negative_fold_penalty_bps < 0.0:
        raise ValueError("negative_fold_penalty_bps_must_be_non_negative")
    if catastrophic_fold_penalty_bps < 0.0:
        raise ValueError("catastrophic_fold_penalty_bps_must_be_non_negative")
    if not micro_regime_profile_values:
        raise ValueError("micro_regime_profile_values_required")
    invalid_profiles = [
        str(value or "").strip()
        for value in micro_regime_profile_values
        if str(value or "").strip() not in MICRO_REGIME_PROFILE_DEFINITIONS
    ]
    if invalid_profiles:
        raise ValueError("unknown_micro_regime_profile")


def _technical_veto_buckets(value: Sequence[str] | None) -> tuple[str, ...]:
    if not value:
        return ()
    buckets: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        if "::" not in text:
            raise ValueError("technical_veto_buckets_must_use_family_bucket_format")
        family, bucket = text.split("::", 1)
        family = family.strip()
        bucket = bucket.strip()
        if not family or not bucket:
            raise ValueError("technical_veto_buckets_must_use_family_bucket_format")
        normalized = f"{family}::{bucket}"
        if normalized not in buckets:
            buckets.append(normalized)
    return tuple(buckets)


def _guardrail_fold_labels(value: Sequence[str] | None) -> tuple[str, ...]:
    if not value:
        return ()
    labels: list[str] = []
    for item in value:
        label = str(item or "").strip()
        if label and label not in labels:
            labels.append(label)
    return tuple(labels)


def _micro_regime_profile_values(value: Sequence[str]) -> tuple[str, ...]:
    profiles: list[str] = []
    for item in value:
        profile = str(item or "").strip()
        if profile and profile not in profiles:
            profiles.append(profile)
    return tuple(profiles)


def _profile_grid(
    *,
    min_edge_to_cost_values: Sequence[float],
    min_net_edge_bps_values: Sequence[float],
    min_liquidity_score_values: Sequence[float],
    max_round_trip_cost_bps_values: Sequence[float],
    min_pattern_quality_score_values: Sequence[float | None],
    micro_regime_profile_values: Sequence[str],
) -> list[dict[str, object]]:
    profiles: list[dict[str, object]] = []
    for edge, net_edge, liquidity, cost, pattern_quality, micro_profile in product(
        min_edge_to_cost_values,
        min_net_edge_bps_values,
        min_liquidity_score_values,
        max_round_trip_cost_bps_values,
        min_pattern_quality_score_values,
        micro_regime_profile_values,
    ):
        micro_definition = MICRO_REGIME_PROFILE_DEFINITIONS[str(micro_profile)]
        profile_id = (
            f"edge{_compact_float(edge)}_net{_compact_float(net_edge)}_"
            f"liq{_compact_float(liquidity)}_cost{_compact_float(cost)}_"
            f"pq{_compact_optional_float(pattern_quality)}"
        )
        if micro_profile != "none":
            profile_id = f"{profile_id}_mr{_safe_name(str(micro_profile))}"
        profiles.append(
            {
                "profile_id": profile_id,
                "min_edge_to_cost": float(edge),
                "min_net_edge_bps": float(net_edge),
                "min_liquidity_score": float(liquidity),
                "max_round_trip_cost_bps": float(cost),
                "min_pattern_quality_score": (
                    None if pattern_quality is None else float(pattern_quality)
                ),
                "micro_regime_profile": micro_profile,
                "micro_regime_veto_buckets": micro_definition["veto_buckets"],
                "micro_regime_require_buckets": micro_definition["require_buckets"],
                "micro_regime_size_scales": micro_definition["size_scales"],
            }
        )
    return profiles


def _fold_row(
    label: str,
    result: TridentAICandidatePaperReplayResult,
    *,
    is_oos: bool,
) -> dict[str, object]:
    trades = _closed_trades(result.paper_journal_path)
    total_notional = sum(float(trade.get("notional_usd", 0.0) or 0.0) for trade in trades)
    pnl = sum(float(trade.get("pnl_usd", 0.0) or 0.0) for trade in trades)
    gross = sum(float(trade.get("gross_pnl_usd", 0.0) or 0.0) for trade in trades)
    fees = sum(float(trade.get("fees_usd", 0.0) or 0.0) for trade in trades)
    wins = sum(1 for trade in trades if float(trade.get("pnl_usd", 0.0) or 0.0) > 0.0)
    symbols = Counter(str(trade.get("symbol", "") or "").upper() for trade in trades)
    symbols.pop("", None)
    close_reasons = Counter(str(trade.get("close_reason", "") or "unknown") for trade in trades)
    paper = result.paper_result
    return {
        "fold_label": label,
        "is_oos": is_oos,
        "candidate_input_path": result.candidate_input_path,
        "market_input_path": result.market_input_path,
        "decision_journal_path": result.decision_journal_path,
        "paper_journal_path": result.paper_journal_path,
        "report_json_path": result.report_json_path,
        "candidates_seen": result.candidates_seen,
        "decisions_written": result.decisions_written,
        "skipped_candidates": result.skipped_candidates,
        "candidate_skip_reasons": dict(sorted(result.skip_reasons.items())),
        "paper_opens": paper.positions_opened if paper is not None else 0,
        "paper_closed": paper.positions_closed if paper is not None else len(trades),
        "paper_skips": paper.proposals_rejected if paper is not None else 0,
        "closed_trades": len(trades),
        "wins": wins,
        "losses": len(trades) - wins,
        "win_rate": round(wins / len(trades), 6) if trades else 0.0,
        "realized_pnl_usd": round(pnl, 6),
        "gross_pnl_usd": round(gross, 6),
        "fees_usd": round(fees, 6),
        "closed_notional_usd": round(total_notional, 6),
        "avg_net_bps": round(_bps(pnl, total_notional), 6),
        "symbols": dict(sorted(symbols.items())),
        "symbols_with_closed": len(symbols),
        "close_reasons": dict(sorted(close_reasons.items())),
    }


def _profile_row(
    *,
    profile: Mapping[str, object],
    folds: Sequence[Mapping[str, object]],
    oos_fold_labels: tuple[str, ...],
    min_total_closed_trades: int,
    min_symbols: int,
    max_negative_folds: int,
    max_catastrophic_net_bps: float,
    max_dominant_symbol_ratio: float,
    oos_no_trade_penalty_bps: float,
    negative_fold_penalty_bps: float,
    catastrophic_fold_penalty_bps: float,
    allow_guardrail_loss_avoidance: bool,
) -> dict[str, object]:
    closed_trades = sum(int(_number(fold.get("closed_trades"))) for fold in folds)
    decisions = sum(int(_number(fold.get("decisions_written"))) for fold in folds)
    candidates = sum(int(_number(fold.get("candidates_seen"))) for fold in folds)
    skipped = sum(int(_number(fold.get("skipped_candidates"))) for fold in folds)
    total_notional = sum(_number(fold.get("closed_notional_usd")) for fold in folds)
    pnl = sum(_number(fold.get("realized_pnl_usd")) for fold in folds)
    gross = sum(_number(fold.get("gross_pnl_usd")) for fold in folds)
    fees = sum(_number(fold.get("fees_usd")) for fold in folds)
    wins = sum(int(_number(fold.get("wins"))) for fold in folds)
    symbol_counts: Counter[str] = Counter()
    skip_reasons: Counter[str] = Counter()
    close_reasons: Counter[str] = Counter()
    for fold in folds:
        symbol_counts.update(_int_mapping(fold.get("symbols")))
        skip_reasons.update(_int_mapping(fold.get("candidate_skip_reasons")))
        close_reasons.update(_int_mapping(fold.get("close_reasons")))
    negative_folds = sum(
        1
        for fold in folds
        if int(_number(fold.get("closed_trades"))) > 0 and _number(fold.get("realized_pnl_usd")) < 0.0
    )
    catastrophic_folds = sum(
        1
        for fold in folds
        if int(_number(fold.get("closed_trades"))) > 0
        and _number(fold.get("avg_net_bps")) <= -abs(max_catastrophic_net_bps)
    )
    oos_no_trade_folds = sum(
        1
        for fold in folds
        if str(fold.get("fold_label", "") or "") in oos_fold_labels
        and int(_number(fold.get("closed_trades"))) == 0
    )
    dominant_symbol_ratio = (
        max(symbol_counts.values()) / closed_trades
        if closed_trades > 0 and symbol_counts
        else 0.0
    )
    guardrail_loss_avoidance_folds = (
        sum(
            1
            for fold in folds
            if str(fold.get("fold_label", "") or "") in oos_fold_labels
            and int(_number(fold.get("closed_trades"))) == 0
            and bool(fold.get("guardrail_loss_avoidance", False))
        )
        if allow_guardrail_loss_avoidance
        else 0
    )
    effective_oos_no_trade_folds = max(0, oos_no_trade_folds - guardrail_loss_avoidance_folds)
    avg_net_bps = _bps(pnl, total_notional)
    penalized_avg_net_bps = (
        avg_net_bps
        - effective_oos_no_trade_folds * oos_no_trade_penalty_bps
        - negative_folds * negative_fold_penalty_bps
        - catastrophic_folds * catastrophic_fold_penalty_bps
    )
    classification = _classification(
        realized_pnl_usd=pnl,
        closed_trades=closed_trades,
        symbols_with_closed=len(symbol_counts),
        effective_oos_no_trade_folds=effective_oos_no_trade_folds,
        guardrail_loss_avoidance_folds=guardrail_loss_avoidance_folds,
        negative_folds=negative_folds,
        catastrophic_folds=catastrophic_folds,
        dominant_symbol_ratio=dominant_symbol_ratio,
        min_total_closed_trades=min_total_closed_trades,
        min_symbols=min_symbols,
        max_negative_folds=max_negative_folds,
        max_dominant_symbol_ratio=max_dominant_symbol_ratio,
    )
    return {
        "profile_id": profile["profile_id"],
        "classification": classification,
        "min_edge_to_cost": profile["min_edge_to_cost"],
        "min_net_edge_bps": profile["min_net_edge_bps"],
        "min_liquidity_score": profile["min_liquidity_score"],
        "max_round_trip_cost_bps": profile["max_round_trip_cost_bps"],
        "min_pattern_quality_score": profile["min_pattern_quality_score"],
        "micro_regime_profile": profile.get("micro_regime_profile", "none"),
        "micro_regime_veto_buckets": list(profile.get("micro_regime_veto_buckets", ())),
        "micro_regime_require_buckets": list(profile.get("micro_regime_require_buckets", ())),
        "micro_regime_size_scales": list(profile.get("micro_regime_size_scales", ())),
        "candidates_seen": candidates,
        "decisions_written": decisions,
        "skipped_candidates": skipped,
        "closed_trades": closed_trades,
        "wins": wins,
        "losses": closed_trades - wins,
        "win_rate": round(wins / closed_trades, 6) if closed_trades else 0.0,
        "realized_pnl_usd": round(pnl, 6),
        "gross_pnl_usd": round(gross, 6),
        "fees_usd": round(fees, 6),
        "closed_notional_usd": round(total_notional, 6),
        "avg_net_bps": round(avg_net_bps, 6),
        "penalized_avg_net_bps": round(penalized_avg_net_bps, 6),
        "negative_folds": negative_folds,
        "catastrophic_folds": catastrophic_folds,
        "oos_no_trade_folds": oos_no_trade_folds,
        "guardrail_loss_avoidance_folds": guardrail_loss_avoidance_folds,
        "effective_oos_no_trade_folds": effective_oos_no_trade_folds,
        "oos_fold_labels": list(oos_fold_labels),
        "symbols": dict(sorted(symbol_counts.items())),
        "symbols_with_closed": len(symbol_counts),
        "dominant_symbol_ratio": round(dominant_symbol_ratio, 6),
        "candidate_skip_reasons": dict(sorted(skip_reasons.items())),
        "close_reasons": dict(sorted(close_reasons.items())),
        "folds": list(folds),
    }


def _classification(
    *,
    realized_pnl_usd: float,
    closed_trades: int,
    symbols_with_closed: int,
    effective_oos_no_trade_folds: int,
    guardrail_loss_avoidance_folds: int,
    negative_folds: int,
    catastrophic_folds: int,
    dominant_symbol_ratio: float,
    min_total_closed_trades: int,
    min_symbols: int,
    max_negative_folds: int,
    max_dominant_symbol_ratio: float,
) -> str:
    if closed_trades < min_total_closed_trades:
        return "insufficient_trades"
    if symbols_with_closed < min_symbols:
        return "insufficient_symbol_support"
    if dominant_symbol_ratio > max_dominant_symbol_ratio:
        return "symbol_concentrated"
    if effective_oos_no_trade_folds > 0:
        return "oos_no_trade"
    if catastrophic_folds > 0:
        return "catastrophic_fold"
    if negative_folds > max_negative_folds:
        return "fold_unstable"
    if realized_pnl_usd <= 0.0:
        return "negative_or_flat"
    if guardrail_loss_avoidance_folds > 0:
        return "guardrail_loss_avoidance_candidate"
    return "robust_candidate"


def _is_guardrail_loss_avoidance(
    *,
    fold_row: Mapping[str, object],
    baseline_row: Mapping[str, object],
) -> bool:
    if int(_number(fold_row.get("closed_trades"))) != 0:
        return False
    skip_reasons = _mapping(fold_row.get("candidate_skip_reasons"))
    technical_veto_skips = sum(
        int(_number(count))
        for reason, count in skip_reasons.items()
        if str(reason).startswith("technical_digest_veto_")
    )
    if technical_veto_skips <= 0:
        return False
    return (
        int(_number(baseline_row.get("closed_trades"))) > 0
        and _number(baseline_row.get("realized_pnl_usd")) < 0.0
    )


def _closed_trades(path: str | Path) -> list[dict[str, object]]:
    paper_path = Path(path)
    if not paper_path.exists():
        return []
    trades: list[dict[str, object]] = []
    for line in paper_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("event_type") != PAPER_REPLAY_TRADE_CLOSED_EVENT:
            continue
        trade = _mapping(row.get("trade"))
        if trade:
            trades.append(dict(trade))
    return trades


def _market_event_caches_by_fold(
    *,
    candidate_input_paths: Sequence[str | Path],
    market_input_paths: Sequence[str | Path],
    config: TridentAIConfig,
    symbols_filter: tuple[str, ...],
) -> dict[int, tuple[object, ...]]:
    runner = TridentAIPaperReplayRunner(config=config)
    caches: dict[int, tuple[object, ...]] = {}
    for index, (candidate_path, market_path) in enumerate(
        zip(candidate_input_paths, market_input_paths, strict=True)
    ):
        min_timestamp = _candidate_min_timestamp(candidate_path, symbols_filter=symbols_filter)
        if min_timestamp is None:
            min_timestamp = datetime.min.replace(tzinfo=timezone.utc)
        caches[index] = runner.build_market_event_cache(
            market_path,
            min_timestamp=min_timestamp,
            symbols=symbols_filter,
        )
    return caches


def _candidate_min_timestamp(
    input_path: str | Path,
    *,
    symbols_filter: tuple[str, ...],
) -> datetime | None:
    allowed = set(symbols_filter)
    earliest: datetime | None = None
    path = Path(input_path)
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, Mapping):
                continue
            if allowed and not _record_has_allowed_symbol(payload, allowed):
                continue
            timestamp = _parse_timestamp(str(payload.get("timestamp", "") or ""))
            if timestamp is None:
                continue
            if earliest is None or timestamp < earliest:
                earliest = timestamp
    return earliest


def _record_has_allowed_symbol(payload: Mapping[str, object], allowed: set[str]) -> bool:
    symbols = payload.get("symbols")
    if not isinstance(symbols, Sequence) or isinstance(symbols, (str, bytes, bytearray)):
        return False
    for item in symbols:
        if not isinstance(item, Mapping):
            continue
        symbol = str(item.get("symbol", "") or "").strip().upper()
        if symbol in allowed:
            return True
    return False


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
    result = _mapping(payload.get("result"))
    best = _mapping(result.get("best_profile"))
    best_robust = _mapping(result.get("best_robust_profile"))
    best_guardrail = _mapping(result.get("best_guardrail_aware_profile"))
    rows = [row for row in result.get("profile_rows", []) if isinstance(row, Mapping)]
    lines = [
        "# TRIDENT-AI Candidate Gate Sweep",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Profiles evaluated: `{result.get('profiles_evaluated', 0)}` / `{result.get('profile_count', 0)}`",
        f"- Artifact dir: `{result.get('artifact_dir', '')}`",
        f"- Symbols filter: `{result.get('symbols_filter', [])}`",
        f"- OOS fold labels: `{result.get('oos_fold_labels', [])}`",
        f"- Technical veto buckets: `{result.get('technical_veto_buckets', [])}`",
        f"- Micro-regime profiles: `{result.get('micro_regime_profile_values', [])}`",
        f"- Market event cache: `{result.get('cache_market_events', False)}`",
        f"- Guardrail-aware loss avoidance: `{result.get('allow_guardrail_loss_avoidance', False)}`",
        f"- Guardrail fold labels: `{result.get('guardrail_fold_labels', [])}`",
        f"- Max dominant symbol ratio: `{result.get('max_dominant_symbol_ratio', 1.0)}`",
        f"- OOS no-trade penalty: `{result.get('oos_no_trade_penalty_bps', 0)}` bps",
        "",
        "## Best Profiles",
        "",
        _profile_line("Best penalized", best),
        _profile_line("Best robust", best_robust),
        _profile_line("Best guardrail-aware", best_guardrail),
        "",
        "## Classification Counts",
        "",
        "| Classification | Count |",
        "|---|---:|",
    ]
    classifications = _mapping(result.get("classification_counts"))
    if classifications:
        for name, count in sorted(classifications.items()):
            lines.append(f"| {name} | {count} |")
    else:
        lines.append("| none | 0 |")
    lines.extend(
        [
            "",
            "## Top Profiles",
            "",
            "| Profile | Micro | Class | Trades | Symbols | Dom symbol | OOS no-trade | Guardrail avoid | Effective OOS no-trade | Neg folds | PnL | Avg bps | Penalized bps |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in rows[:20]:
        lines.append(_profile_table_row(row))
    if not rows:
        lines.append("| none | none | n/a | 0 | 0 | `0.00` | 0 | 0 | 0 | 0 | `$0.000000` | `0.00` | `0.00` |")
    lines.extend(["", "## Best Profile Folds", "", "| Fold | OOS | Trades | PnL | Avg bps | Symbols |", "|---|---:|---:|---:|---:|---|"])
    for fold in best.get("folds", []):
        if isinstance(fold, Mapping):
            lines.append(_fold_table_row(fold))
    if not best:
        lines.append("| none | 0 | 0 | `$0.000000` | `0.00` | none |")
    lines.append("")
    return "\n".join(lines)


def _profile_line(label: str, row: Mapping[str, object]) -> str:
    if not row:
        return f"- {label}: `none`"
    return (
        f"- {label}: `{row.get('profile_id')}` / "
        f"micro `{row.get('micro_regime_profile', 'none')}` / "
        f"`{row.get('classification')}` / "
        f"trades `{row.get('closed_trades')}` / PnL "
        f"`${float(row.get('realized_pnl_usd', 0.0)):.6f}` / penalized "
        f"`{float(row.get('penalized_avg_net_bps', 0.0)):.2f} bps`"
    )


def _profile_table_row(row: Mapping[str, object]) -> str:
    return (
        f"| `{row.get('profile_id')}` | `{row.get('micro_regime_profile', 'none')}` | "
        f"`{row.get('classification')}` | "
        f"{int(_number(row.get('closed_trades')))} | {int(_number(row.get('symbols_with_closed')))} | "
        f"`{_number(row.get('dominant_symbol_ratio')):.2f}` | "
        f"{int(_number(row.get('oos_no_trade_folds')))} | "
        f"{int(_number(row.get('guardrail_loss_avoidance_folds')))} | "
        f"{int(_number(row.get('effective_oos_no_trade_folds')))} | "
        f"{int(_number(row.get('negative_folds')))} | "
        f"`${_number(row.get('realized_pnl_usd')):.6f}` | "
        f"`{_number(row.get('avg_net_bps')):.2f}` | "
        f"`{_number(row.get('penalized_avg_net_bps')):.2f}` |"
    )


def _fold_table_row(row: Mapping[str, object]) -> str:
    symbols = _mapping(row.get("symbols"))
    symbol_text = ", ".join(f"{symbol}:{count}" for symbol, count in sorted(symbols.items())) or "none"
    return (
        f"| `{row.get('fold_label')}` | {1 if row.get('is_oos') else 0} | "
        f"{int(_number(row.get('closed_trades')))} | "
        f"`${_number(row.get('realized_pnl_usd')):.6f}` | "
        f"`{_number(row.get('avg_net_bps')):.2f}` | {symbol_text} |"
    )


def _profile_sort_key(row: Mapping[str, object]) -> tuple[float, float, int, int, str]:
    return (
        -_number(row.get("penalized_avg_net_bps")),
        -_number(row.get("realized_pnl_usd")),
        int(_number(row.get("effective_oos_no_trade_folds", row.get("oos_no_trade_folds")))),
        int(_number(row.get("negative_folds"))),
        str(row.get("profile_id", "") or ""),
    )


def _fold_labels(labels: Sequence[str] | None, count: int) -> tuple[str, ...]:
    if labels is None:
        return tuple(f"fold_{index + 1}" for index in range(count))
    return tuple(str(label).strip() or f"fold_{index + 1}" for index, label in enumerate(labels))


def _oos_labels(oos_fold_labels: Sequence[str] | None, labels: tuple[str, ...]) -> tuple[str, ...]:
    if oos_fold_labels is not None:
        return tuple(str(label).strip() for label in oos_fold_labels if str(label).strip())
    return tuple(label for label in labels if "oos" in label.lower())


def _symbols_filter(symbols: Sequence[str] | None) -> tuple[str, ...]:
    if not symbols:
        return ()
    normalized: list[str] = []
    for symbol in symbols:
        value = str(symbol).strip().upper()
        if value and value not in normalized:
            normalized.append(value)
    return tuple(normalized)


def _int_mapping(value: object) -> dict[str, int]:
    payload = _mapping(value)
    return {
        str(key): int(_number(count))
        for key, count in payload.items()
        if str(key) and int(_number(count)) != 0
    }


def _bps(pnl: float, notional: float) -> float:
    return pnl / notional * 10_000.0 if notional > 0.0 else 0.0


def _compact_float(value: object) -> str:
    number = float(value)
    text = f"{number:g}"
    return text.replace("-", "m").replace(".", "p")


def _compact_optional_float(value: object) -> str:
    if value is None:
        return "none"
    return _compact_float(value)


def _safe_name(value: str) -> str:
    normalized = []
    for char in value.lower():
        if char.isalnum():
            normalized.append(char)
        else:
            normalized.append("_")
    return "".join(normalized).strip("_") or "fold"
