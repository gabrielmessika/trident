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
from app.trident_ai.paper import PAPER_REPLAY_TRADE_CLOSED_EVENT
from app.trident_ai.pattern_calibration import _format_timestamp, _mapping, _number, _timestamp_id


DEFAULT_GATE_SWEEP_MIN_EDGE_TO_COST_VALUES: tuple[float, ...] = (2.5, 3.0, 3.5, 4.0)
DEFAULT_GATE_SWEEP_MIN_NET_EDGE_BPS_VALUES: tuple[float, ...] = (15.0, 25.0, 35.0)
DEFAULT_GATE_SWEEP_MIN_LIQUIDITY_SCORE_VALUES: tuple[float, ...] = (1.0, 1.2)
DEFAULT_GATE_SWEEP_MAX_ROUND_TRIP_COST_BPS_VALUES: tuple[float, ...] = (12.0, 16.0)
DEFAULT_GATE_SWEEP_MIN_TOTAL_CLOSED_TRADES = 4
DEFAULT_GATE_SWEEP_MIN_SYMBOLS = 2
DEFAULT_GATE_SWEEP_MAX_NEGATIVE_FOLDS = 0
DEFAULT_GATE_SWEEP_MAX_CATASTROPHIC_NET_BPS = 50.0
DEFAULT_GATE_SWEEP_OOS_NO_TRADE_PENALTY_BPS = 25.0
DEFAULT_GATE_SWEEP_NEGATIVE_FOLD_PENALTY_BPS = 10.0
DEFAULT_GATE_SWEEP_CATASTROPHIC_FOLD_PENALTY_BPS = 50.0


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
    oos_no_trade_penalty_bps: float = DEFAULT_GATE_SWEEP_OOS_NO_TRADE_PENALTY_BPS
    negative_fold_penalty_bps: float = DEFAULT_GATE_SWEEP_NEGATIVE_FOLD_PENALTY_BPS
    catastrophic_fold_penalty_bps: float = DEFAULT_GATE_SWEEP_CATASTROPHIC_FOLD_PENALTY_BPS
    threshold_values: dict[str, list[float]] = field(default_factory=dict)
    best_profile: dict[str, object] = field(default_factory=dict)
    best_robust_profile: dict[str, object] = field(default_factory=dict)
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
            "oos_no_trade_penalty_bps": round(self.oos_no_trade_penalty_bps, 6),
            "negative_fold_penalty_bps": round(self.negative_fold_penalty_bps, 6),
            "catastrophic_fold_penalty_bps": round(self.catastrophic_fold_penalty_bps, 6),
            "threshold_values": self.threshold_values,
            "best_profile": self.best_profile,
            "best_robust_profile": self.best_robust_profile,
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
    max_profiles: int | None = None,
    min_total_closed_trades: int = DEFAULT_GATE_SWEEP_MIN_TOTAL_CLOSED_TRADES,
    min_symbols: int = DEFAULT_GATE_SWEEP_MIN_SYMBOLS,
    max_negative_folds: int = DEFAULT_GATE_SWEEP_MAX_NEGATIVE_FOLDS,
    max_catastrophic_net_bps: float = DEFAULT_GATE_SWEEP_MAX_CATASTROPHIC_NET_BPS,
    oos_no_trade_penalty_bps: float = DEFAULT_GATE_SWEEP_OOS_NO_TRADE_PENALTY_BPS,
    negative_fold_penalty_bps: float = DEFAULT_GATE_SWEEP_NEGATIVE_FOLD_PENALTY_BPS,
    catastrophic_fold_penalty_bps: float = DEFAULT_GATE_SWEEP_CATASTROPHIC_FOLD_PENALTY_BPS,
) -> TridentAICandidateGateSweepResult:
    _validate_inputs(
        candidate_input_paths=candidate_input_paths,
        market_input_paths=market_input_paths,
        fold_labels=fold_labels,
        min_edge_to_cost_values=min_edge_to_cost_values,
        min_net_edge_bps_values=min_net_edge_bps_values,
        min_liquidity_score_values=min_liquidity_score_values,
        max_round_trip_cost_bps_values=max_round_trip_cost_bps_values,
        max_profiles=max_profiles,
        min_total_closed_trades=min_total_closed_trades,
        min_symbols=min_symbols,
        max_negative_folds=max_negative_folds,
        max_catastrophic_net_bps=max_catastrophic_net_bps,
        oos_no_trade_penalty_bps=oos_no_trade_penalty_bps,
        negative_fold_penalty_bps=negative_fold_penalty_bps,
        catastrophic_fold_penalty_bps=catastrophic_fold_penalty_bps,
    )
    active_config = config or load_trident_ai_config()
    run_id = _timestamp_id(datetime.now(timezone.utc))
    output_dir = Path(active_config.paths.replay_output_dir)
    json_output = Path(report_json_path or output_dir / f"trident_ai_candidate_gate_sweep_{run_id}.json")
    md_output = Path(report_md_path or output_dir / f"trident_ai_candidate_gate_sweep_{run_id}.md")
    artifacts = Path(artifact_dir or output_dir / f"{json_output.stem}_artifacts")
    labels = _fold_labels(fold_labels, len(candidate_input_paths))
    oos_labels = _oos_labels(oos_fold_labels, labels)
    symbols_filter = _symbols_filter(symbols)

    profiles = _profile_grid(
        min_edge_to_cost_values=min_edge_to_cost_values,
        min_net_edge_bps_values=min_net_edge_bps_values,
        min_liquidity_score_values=min_liquidity_score_values,
        max_round_trip_cost_bps_values=max_round_trip_cost_bps_values,
    )
    profile_count = len(profiles)
    if max_profiles is not None:
        profiles = profiles[:max_profiles]

    rows: list[dict[str, object]] = []
    for index, profile in enumerate(profiles, start=1):
        fold_rows: list[dict[str, object]] = []
        for label, candidate_path, market_path in zip(labels, candidate_input_paths, market_input_paths, strict=True):
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
            )
            fold_rows.append(_fold_row(label, replay_result, is_oos=label in oos_labels))
        rows.append(
            _profile_row(
                profile=profile,
                folds=fold_rows,
                oos_fold_labels=oos_labels,
                min_total_closed_trades=min_total_closed_trades,
                min_symbols=min_symbols,
                max_negative_folds=max_negative_folds,
                max_catastrophic_net_bps=max_catastrophic_net_bps,
                oos_no_trade_penalty_bps=oos_no_trade_penalty_bps,
                negative_fold_penalty_bps=negative_fold_penalty_bps,
                catastrophic_fold_penalty_bps=catastrophic_fold_penalty_bps,
            )
        )

    rows.sort(key=_profile_sort_key)
    classification_counts = Counter(str(row.get("classification", "")) for row in rows)
    robust_rows = [row for row in rows if row.get("classification") == "robust_candidate"]
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
        oos_no_trade_penalty_bps=oos_no_trade_penalty_bps,
        negative_fold_penalty_bps=negative_fold_penalty_bps,
        catastrophic_fold_penalty_bps=catastrophic_fold_penalty_bps,
        threshold_values={
            "min_edge_to_cost": [float(value) for value in min_edge_to_cost_values],
            "min_net_edge_bps": [float(value) for value in min_net_edge_bps_values],
            "min_liquidity_score": [float(value) for value in min_liquidity_score_values],
            "max_round_trip_cost_bps": [float(value) for value in max_round_trip_cost_bps_values],
        },
        best_profile=rows[0] if rows else {},
        best_robust_profile=robust_rows[0] if robust_rows else {},
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
    max_profiles: int | None,
    min_total_closed_trades: int,
    min_symbols: int,
    max_negative_folds: int,
    max_catastrophic_net_bps: float,
    oos_no_trade_penalty_bps: float,
    negative_fold_penalty_bps: float,
    catastrophic_fold_penalty_bps: float,
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
    ):
        if not values:
            raise ValueError(f"{name}_required")
        if any(float(value) < 0.0 for value in values):
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
    if oos_no_trade_penalty_bps < 0.0:
        raise ValueError("oos_no_trade_penalty_bps_must_be_non_negative")
    if negative_fold_penalty_bps < 0.0:
        raise ValueError("negative_fold_penalty_bps_must_be_non_negative")
    if catastrophic_fold_penalty_bps < 0.0:
        raise ValueError("catastrophic_fold_penalty_bps_must_be_non_negative")


def _profile_grid(
    *,
    min_edge_to_cost_values: Sequence[float],
    min_net_edge_bps_values: Sequence[float],
    min_liquidity_score_values: Sequence[float],
    max_round_trip_cost_bps_values: Sequence[float],
) -> list[dict[str, object]]:
    profiles: list[dict[str, object]] = []
    for edge, net_edge, liquidity, cost in product(
        min_edge_to_cost_values,
        min_net_edge_bps_values,
        min_liquidity_score_values,
        max_round_trip_cost_bps_values,
    ):
        profile_id = (
            f"edge{_compact_float(edge)}_net{_compact_float(net_edge)}_"
            f"liq{_compact_float(liquidity)}_cost{_compact_float(cost)}"
        )
        profiles.append(
            {
                "profile_id": profile_id,
                "min_edge_to_cost": float(edge),
                "min_net_edge_bps": float(net_edge),
                "min_liquidity_score": float(liquidity),
                "max_round_trip_cost_bps": float(cost),
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
    oos_no_trade_penalty_bps: float,
    negative_fold_penalty_bps: float,
    catastrophic_fold_penalty_bps: float,
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
    avg_net_bps = _bps(pnl, total_notional)
    penalized_avg_net_bps = (
        avg_net_bps
        - oos_no_trade_folds * oos_no_trade_penalty_bps
        - negative_folds * negative_fold_penalty_bps
        - catastrophic_folds * catastrophic_fold_penalty_bps
    )
    classification = _classification(
        realized_pnl_usd=pnl,
        closed_trades=closed_trades,
        symbols_with_closed=len(symbol_counts),
        oos_no_trade_folds=oos_no_trade_folds,
        negative_folds=negative_folds,
        catastrophic_folds=catastrophic_folds,
        min_total_closed_trades=min_total_closed_trades,
        min_symbols=min_symbols,
        max_negative_folds=max_negative_folds,
    )
    return {
        "profile_id": profile["profile_id"],
        "classification": classification,
        "min_edge_to_cost": profile["min_edge_to_cost"],
        "min_net_edge_bps": profile["min_net_edge_bps"],
        "min_liquidity_score": profile["min_liquidity_score"],
        "max_round_trip_cost_bps": profile["max_round_trip_cost_bps"],
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
        "oos_fold_labels": list(oos_fold_labels),
        "symbols": dict(sorted(symbol_counts.items())),
        "symbols_with_closed": len(symbol_counts),
        "candidate_skip_reasons": dict(sorted(skip_reasons.items())),
        "close_reasons": dict(sorted(close_reasons.items())),
        "folds": list(folds),
    }


def _classification(
    *,
    realized_pnl_usd: float,
    closed_trades: int,
    symbols_with_closed: int,
    oos_no_trade_folds: int,
    negative_folds: int,
    catastrophic_folds: int,
    min_total_closed_trades: int,
    min_symbols: int,
    max_negative_folds: int,
) -> str:
    if closed_trades < min_total_closed_trades:
        return "insufficient_trades"
    if symbols_with_closed < min_symbols:
        return "insufficient_symbol_support"
    if oos_no_trade_folds > 0:
        return "oos_no_trade"
    if catastrophic_folds > 0:
        return "catastrophic_fold"
    if negative_folds > max_negative_folds:
        return "fold_unstable"
    if realized_pnl_usd <= 0.0:
        return "negative_or_flat"
    return "robust_candidate"


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
    rows = [row for row in result.get("profile_rows", []) if isinstance(row, Mapping)]
    lines = [
        "# TRIDENT-AI Candidate Gate Sweep",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Profiles evaluated: `{result.get('profiles_evaluated', 0)}` / `{result.get('profile_count', 0)}`",
        f"- Artifact dir: `{result.get('artifact_dir', '')}`",
        f"- Symbols filter: `{result.get('symbols_filter', [])}`",
        f"- OOS fold labels: `{result.get('oos_fold_labels', [])}`",
        f"- OOS no-trade penalty: `{result.get('oos_no_trade_penalty_bps', 0)}` bps",
        "",
        "## Best Profiles",
        "",
        _profile_line("Best penalized", best),
        _profile_line("Best robust", best_robust),
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
            "| Profile | Class | Trades | Symbols | OOS no-trade | Neg folds | PnL | Avg bps | Penalized bps |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in rows[:20]:
        lines.append(_profile_table_row(row))
    if not rows:
        lines.append("| none | n/a | 0 | 0 | 0 | 0 | `$0.000000` | `0.00` | `0.00` |")
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
        f"- {label}: `{row.get('profile_id')}` / `{row.get('classification')}` / "
        f"trades `{row.get('closed_trades')}` / PnL "
        f"`${float(row.get('realized_pnl_usd', 0.0)):.6f}` / penalized "
        f"`{float(row.get('penalized_avg_net_bps', 0.0)):.2f} bps`"
    )


def _profile_table_row(row: Mapping[str, object]) -> str:
    return (
        f"| `{row.get('profile_id')}` | `{row.get('classification')}` | "
        f"{int(_number(row.get('closed_trades')))} | {int(_number(row.get('symbols_with_closed')))} | "
        f"{int(_number(row.get('oos_no_trade_folds')))} | {int(_number(row.get('negative_folds')))} | "
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
        int(_number(row.get("oos_no_trade_folds"))),
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


def _safe_name(value: str) -> str:
    normalized = []
    for char in value.lower():
        if char.isalnum():
            normalized.append(char)
        else:
            normalized.append("_")
    return "".join(normalized).strip("_") or "fold"
