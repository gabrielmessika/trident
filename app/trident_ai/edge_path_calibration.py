from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.trident_ai.config import TridentAIConfig, load_trident_ai_config
from app.trident_ai.edge_calibration import (
    _candidate_records,
    _closed_trades_by_decision,
    _llm_decisions_by_context,
    _trade_edge_metrics,
)
from app.trident_ai.exit_audit import (
    DEFAULT_EARLY_ADVERSE_BPS,
    DEFAULT_EXIT_AUDIT_WINDOWS_MINUTES,
    DEFAULT_GIVEBACK_BPS,
    DEFAULT_MIN_FOLLOW_THROUGH_BPS,
    _market_price_index,
    _normalize_windows,
    _trade_item,
)
from app.trident_ai.pattern_calibration import (
    _format_timestamp,
    _mapping,
    _number,
    _string_list,
    _timestamp_id,
)


DEFAULT_FAST_INVALIDATION_MINUTES = 15


@dataclass(frozen=True, slots=True)
class TridentAIEdgePathCalibrationResult:
    candidate_input_paths: tuple[str, ...]
    llm_journal_paths: tuple[str, ...]
    paper_journal_paths: tuple[str, ...]
    market_input_paths: tuple[str, ...]
    fold_labels: tuple[str, ...]
    report_json_path: str
    report_md_path: str
    symbols_filter: tuple[str, ...] = ()
    windows_minutes: tuple[int, ...] = DEFAULT_EXIT_AUDIT_WINDOWS_MINUTES
    early_adverse_bps: float = DEFAULT_EARLY_ADVERSE_BPS
    min_follow_through_bps: float = DEFAULT_MIN_FOLLOW_THROUGH_BPS
    giveback_bps: float = DEFAULT_GIVEBACK_BPS
    fast_invalidation_minutes: int = DEFAULT_FAST_INVALIDATION_MINUTES
    summary: dict[str, object] = field(default_factory=dict)
    threshold_diagnostics: dict[str, object] = field(default_factory=dict)
    fold_rows: list[dict[str, object]] = field(default_factory=list)
    bucket_rows: list[dict[str, object]] = field(default_factory=list)
    worst_trades: list[dict[str, object]] = field(default_factory=list)
    items: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_input_paths": list(self.candidate_input_paths),
            "llm_journal_paths": list(self.llm_journal_paths),
            "paper_journal_paths": list(self.paper_journal_paths),
            "market_input_paths": list(self.market_input_paths),
            "fold_labels": list(self.fold_labels),
            "report_json_path": self.report_json_path,
            "report_md_path": self.report_md_path,
            "symbols_filter": list(self.symbols_filter),
            "windows_minutes": list(self.windows_minutes),
            "early_adverse_bps": round(self.early_adverse_bps, 6),
            "min_follow_through_bps": round(self.min_follow_through_bps, 6),
            "giveback_bps": round(self.giveback_bps, 6),
            "fast_invalidation_minutes": self.fast_invalidation_minutes,
            "summary": self.summary,
            "threshold_diagnostics": self.threshold_diagnostics,
            "fold_rows": self.fold_rows,
            "bucket_rows": self.bucket_rows,
            "worst_trades": self.worst_trades,
            "items": self.items,
        }


def run_trident_ai_edge_path_calibration_report(
    *,
    candidate_input_paths: Sequence[str | Path],
    llm_journal_paths: Sequence[str | Path],
    paper_journal_paths: Sequence[str | Path],
    market_input_paths: Sequence[str | Path],
    fold_labels: Sequence[str] | None = None,
    config: TridentAIConfig | None = None,
    report_json_path: str | Path | None = None,
    report_md_path: str | Path | None = None,
    symbols: Sequence[str] | None = None,
    windows_minutes: tuple[int, ...] = DEFAULT_EXIT_AUDIT_WINDOWS_MINUTES,
    early_adverse_bps: float = DEFAULT_EARLY_ADVERSE_BPS,
    min_follow_through_bps: float = DEFAULT_MIN_FOLLOW_THROUGH_BPS,
    giveback_bps: float = DEFAULT_GIVEBACK_BPS,
    fast_invalidation_minutes: int = DEFAULT_FAST_INVALIDATION_MINUTES,
) -> TridentAIEdgePathCalibrationResult:
    _validate_inputs(
        candidate_input_paths=candidate_input_paths,
        llm_journal_paths=llm_journal_paths,
        paper_journal_paths=paper_journal_paths,
        market_input_paths=market_input_paths,
        fold_labels=fold_labels,
        early_adverse_bps=early_adverse_bps,
        min_follow_through_bps=min_follow_through_bps,
        giveback_bps=giveback_bps,
        fast_invalidation_minutes=fast_invalidation_minutes,
    )
    windows = _normalize_windows(windows_minutes)
    resolved_config = config or load_trident_ai_config()
    run_id = _timestamp_id(datetime.now(timezone.utc))
    output_dir = Path(resolved_config.paths.replay_output_dir)
    json_output = Path(
        report_json_path or output_dir / f"trident_ai_edge_path_calibration_{run_id}.json"
    )
    md_output = Path(
        report_md_path or output_dir / f"trident_ai_edge_path_calibration_{run_id}.md"
    )
    labels = _fold_labels(fold_labels, len(candidate_input_paths))
    symbols_filter = _symbols_filter(symbols)
    allowed = set(symbols_filter)

    all_items: list[dict[str, object]] = []
    fold_rows: list[dict[str, object]] = []
    for label, candidate_path, llm_path, paper_path, market_path in zip(
        labels,
        candidate_input_paths,
        llm_journal_paths,
        paper_journal_paths,
        market_input_paths,
        strict=True,
    ):
        market_index = _market_price_index(market_path)
        llm_by_context = _llm_decisions_by_context(llm_path)
        trades_by_decision = _closed_trades_by_decision(paper_path)
        fold_items: list[dict[str, object]] = []
        for candidate in _candidate_records(candidate_path):
            symbol = str(candidate.get("symbol", "") or "").upper()
            if allowed and symbol not in allowed:
                continue
            item = _candidate_item(
                candidate=candidate,
                llm_decision=llm_by_context.get(str(candidate.get("context_id", "") or "")),
                trades_by_decision=trades_by_decision,
                market_index=market_index,
                fold_label=label,
                candidate_input_path=str(candidate_path),
                llm_journal_path=str(llm_path),
                paper_journal_path=str(paper_path),
                market_input_path=str(market_path),
                windows=windows,
                early_adverse_bps=float(early_adverse_bps),
                min_follow_through_bps=float(min_follow_through_bps),
                giveback_bps=float(giveback_bps),
                fast_invalidation_minutes=int(fast_invalidation_minutes),
            )
            fold_items.append(item)
            all_items.append(item)
        fold_rows.append(_summary_row(label, fold_items, key_name="fold_label"))

    closed_items = [item for item in all_items if item.get("status") == "closed_trade"]
    bucket_rows = _bucket_rows(closed_items)
    result = TridentAIEdgePathCalibrationResult(
        candidate_input_paths=tuple(str(path) for path in candidate_input_paths),
        llm_journal_paths=tuple(str(path) for path in llm_journal_paths),
        paper_journal_paths=tuple(str(path) for path in paper_journal_paths),
        market_input_paths=tuple(str(path) for path in market_input_paths),
        fold_labels=labels,
        report_json_path=str(json_output),
        report_md_path=str(md_output),
        symbols_filter=symbols_filter,
        windows_minutes=windows,
        early_adverse_bps=float(early_adverse_bps),
        min_follow_through_bps=float(min_follow_through_bps),
        giveback_bps=float(giveback_bps),
        fast_invalidation_minutes=int(fast_invalidation_minutes),
        summary=_summary_row("all", all_items, key_name="scope"),
        threshold_diagnostics=_threshold_diagnostics(closed_items),
        fold_rows=fold_rows,
        bucket_rows=bucket_rows[:120],
        worst_trades=_worst_trades(closed_items),
        items=_compact_items(all_items),
    )
    payload = build_edge_path_calibration_report_payload(
        result=result,
        generated_at=_format_timestamp(datetime.now(timezone.utc)),
    )
    _write_report_outputs(payload, json_path=json_output, md_path=md_output)
    return result


def build_edge_path_calibration_report_payload(
    *,
    result: TridentAIEdgePathCalibrationResult,
    generated_at: str,
) -> dict[str, object]:
    return {
        "generated_at": generated_at,
        "kind": "trident_ai_edge_path_calibration_report",
        "result": result.to_dict(),
    }


def _candidate_item(
    *,
    candidate: dict[str, object],
    llm_decision: Mapping[str, object] | None,
    trades_by_decision: Mapping[str, dict[str, object]],
    market_index: Mapping[str, list[tuple[datetime, str, float]]],
    fold_label: str,
    candidate_input_path: str,
    llm_journal_path: str,
    paper_journal_path: str,
    market_input_path: str,
    windows: tuple[int, ...],
    early_adverse_bps: float,
    min_follow_through_bps: float,
    giveback_bps: float,
    fast_invalidation_minutes: int,
) -> dict[str, object]:
    base = _base_candidate_item(candidate, fold_label=fold_label)
    base.update(
        {
            "candidate_input_path": candidate_input_path,
            "llm_journal_path": llm_journal_path,
            "paper_journal_path": paper_journal_path,
            "market_input_path": market_input_path,
        }
    )
    if llm_decision is None:
        return {**base, "status": "missing_llm_decision"}

    proposal = _mapping(llm_decision.get("proposal"))
    action = str(proposal.get("action", "") or "").lower()
    decision_id = str(proposal.get("decision_id", "") or "")
    base["llm"] = {
        "action": action,
        "decision_id": decision_id,
        "confidence": round(_number(proposal.get("confidence")), 6),
        "rationale_tags": _string_list(proposal.get("rationale_tags")),
    }
    if action != "open":
        return {**base, "status": "llm_hold"}
    trade = trades_by_decision.get(decision_id)
    if trade is None:
        return {**base, "status": "open_without_closed_trade"}

    metrics = _trade_edge_metrics(candidate, trade)
    path = _trade_item(
        trade,
        market_index=dict(market_index),
        fold_label=fold_label,
        paper_journal_path=paper_journal_path,
        market_input_path=market_input_path,
        windows=windows,
        early_adverse_bps=early_adverse_bps,
        min_follow_through_bps=min_follow_through_bps,
        giveback_bps=giveback_bps,
    )
    labels = tuple(
        str(label)
        for label in path.get("classifications", [])
        if isinstance(label, str) and label
    )
    pnl = _number(metrics.get("pnl_usd"))
    estimated_net_edge = _number(metrics.get("estimated_net_edge_bps"))
    realized_net = _number(metrics.get("realized_net_bps"))
    duration = _number(path.get("duration_minutes"))
    close_reason = str(trade.get("close_reason", "") or "")
    fast_invalidation = (
        close_reason == "invalidation_price_hit"
        and 0.0 < duration <= float(fast_invalidation_minutes)
    )
    return {
        **base,
        "status": "closed_trade",
        "trade": {
            "decision_id": decision_id,
            "opened_at": str(trade.get("opened_at", "") or ""),
            "closed_at": str(trade.get("closed_at", "") or ""),
            "close_reason": close_reason,
            "pnl_usd": round(pnl, 6),
            "gross_pnl_usd": round(_number(trade.get("gross_pnl_usd")), 6),
            "fees_usd": round(_number(trade.get("fees_usd")), 6),
            "notional_usd": round(_number(trade.get("notional_usd")), 6),
            "realized_net_bps": round(realized_net, 6),
            "realized_gross_bps": round(_number(metrics.get("realized_gross_bps")), 6),
            "estimated_edge_bps": round(_number(metrics.get("estimated_edge_bps")), 6),
            "estimated_net_edge_bps": round(estimated_net_edge, 6),
            "edge_error_bps": round(_number(metrics.get("edge_error_bps")), 6),
            "net_edge_error_bps": round(realized_net - estimated_net_edge, 6),
        },
        "path": {
            "duration_minutes": round(duration, 6),
            "mfe_bps": round(_number(path.get("mfe_bps")), 6),
            "mae_bps": round(_number(path.get("mae_bps")), 6),
            "giveback_bps": round(_number(path.get("giveback_bps")), 6),
            "time_to_mfe_minutes": round(_number(path.get("time_to_mfe_minutes")), 6),
            "time_to_mae_minutes": round(_number(path.get("time_to_mae_minutes")), 6),
            "classifications": list(labels),
            "fast_invalidation": fast_invalidation,
            "path_available": bool(path.get("path_available", False)),
            "window_outcomes": _mapping(path.get("window_outcomes")),
        },
        "flags": {
            "false_positive": pnl <= 0.0 and estimated_net_edge > 0.0,
            "overestimated": _number(metrics.get("edge_error_bps")) < 0.0,
            "fast_invalidation": fast_invalidation,
            "early_adverse_loss": "early_adverse_loss" in labels,
            "no_follow_through_loss": "no_follow_through_loss" in labels,
            "gave_back_to_loss": "gave_back_to_loss" in labels,
        },
    }


def _base_candidate_item(candidate: Mapping[str, object], *, fold_label: str) -> dict[str, object]:
    features = _mapping(candidate.get("market_features"))
    estimated_edge = _number(candidate.get("estimated_edge_bps"))
    round_trip_cost = _number(candidate.get("round_trip_cost_bps"))
    return {
        "fold_label": fold_label,
        "timestamp": str(candidate.get("timestamp", "") or ""),
        "context_id": str(candidate.get("context_id", "") or ""),
        "symbol": str(candidate.get("symbol", "") or "").upper(),
        "side": str(candidate.get("side", "") or "").lower(),
        "candidate": {
            "score": round(_number(candidate.get("score")), 6),
            "raw_score": round(_number(candidate.get("raw_score")), 6),
            "estimated_edge_bps": round(estimated_edge, 6),
            "round_trip_cost_bps": round(round_trip_cost, 6),
            "estimated_net_edge_bps": round(estimated_edge - round_trip_cost, 6),
            "edge_to_cost_ratio": round(_number(candidate.get("edge_to_cost_ratio")), 6),
            "liquidity_score": round(_number(candidate.get("liquidity_score")), 6),
            "activity_score": round(_number(candidate.get("activity_score")), 6),
            "reasons": _string_list(candidate.get("reasons")),
            "pattern_reasons": _string_list(candidate.get("pattern_reasons")),
            "market_features": {
                "microprice_dislocation_bps": round(_number(features.get("microprice_dislocation_bps")), 6),
                "vwap_distance_bps": round(_number(features.get("vwap_distance_bps")), 6),
                "structure_score": round(_number(features.get("structure_score")), 6),
                "trade_flow_bias": round(_number(features.get("trade_flow_bias")), 6),
                "book_imbalance": round(_number(features.get("book_imbalance")), 6),
                "spread_bps": round(_number(features.get("spread_bps")), 6),
            },
        },
    }


def _summary_row(name: str, items: Sequence[Mapping[str, object]], *, key_name: str) -> dict[str, object]:
    rows = list(items)
    closed = [row for row in rows if row.get("status") == "closed_trade"]
    opens = [
        row
        for row in rows
        if _mapping(row.get("llm")).get("action") == "open"
    ]
    holds = [
        row
        for row in rows
        if _mapping(row.get("llm")).get("action") == "hold"
    ]
    pnl_values = [_number(_mapping(row.get("trade")).get("pnl_usd")) for row in closed]
    notional = sum(_number(_mapping(row.get("trade")).get("notional_usd")) for row in closed)
    flags = [_mapping(row.get("flags")) for row in closed]
    path_rows = [_mapping(row.get("path")) for row in closed]
    trade_rows = [_mapping(row.get("trade")) for row in closed]
    return {
        key_name: name,
        "candidates_seen": len(rows),
        "matched_llm_decisions": sum(1 for row in rows if "llm" in row),
        "open_decisions": len(opens),
        "hold_decisions": len(holds),
        "closed_trades": len(closed),
        "winning_trades": sum(1 for value in pnl_values if value > 0.0),
        "losing_trades": sum(1 for value in pnl_values if value < 0.0),
        "false_positive_trades": sum(1 for flag in flags if bool(flag.get("false_positive", False))),
        "fast_invalidations": sum(1 for flag in flags if bool(flag.get("fast_invalidation", False))),
        "early_adverse_losses": sum(1 for flag in flags if bool(flag.get("early_adverse_loss", False))),
        "no_follow_through_losses": sum(1 for flag in flags if bool(flag.get("no_follow_through_loss", False))),
        "gave_back_to_loss": sum(1 for flag in flags if bool(flag.get("gave_back_to_loss", False))),
        "realized_pnl_usd": round(sum(pnl_values), 6),
        "avg_realized_net_bps": round(_bps(sum(pnl_values), notional), 6),
        "avg_estimated_net_edge_bps": round(
            _average([_number(_mapping(row.get("candidate")).get("estimated_net_edge_bps")) for row in closed]),
            6,
        ),
        "avg_net_edge_error_bps": round(
            _average([_number(trade.get("net_edge_error_bps")) for trade in trade_rows]),
            6,
        ),
        "avg_mfe_bps": round(_average([_number(path.get("mfe_bps")) for path in path_rows]), 6),
        "avg_mae_bps": round(_average([_number(path.get("mae_bps")) for path in path_rows]), 6),
        "avg_duration_minutes": round(
            _average([_number(path.get("duration_minutes")) for path in path_rows]),
            6,
        ),
        "status_counts": dict(Counter(str(row.get("status", "") or "unknown") for row in rows)),
        "symbol_counts": dict(Counter(str(row.get("symbol", "") or "unknown") for row in closed)),
        "close_reason_counts": dict(Counter(str(_mapping(row.get("trade")).get("close_reason", "") or "unknown") for row in closed)),
    }


def _threshold_diagnostics(items: Sequence[Mapping[str, object]]) -> dict[str, object]:
    closed = [row for row in items if row.get("status") == "closed_trade"]
    winners = [_metric_row(row) for row in closed if _number(_mapping(row.get("trade")).get("pnl_usd")) > 0.0]
    false_positives = [
        _metric_row(row)
        for row in closed
        if bool(_mapping(row.get("flags")).get("false_positive", False))
    ]
    max_false_positive_net = max((row["estimated_net_edge_bps"] for row in false_positives), default=0.0)
    min_winner_net = min((row["estimated_net_edge_bps"] for row in winners), default=0.0)
    max_false_positive_edge_to_cost = max((row["edge_to_cost_ratio"] for row in false_positives), default=0.0)
    min_winner_edge_to_cost = min((row["edge_to_cost_ratio"] for row in winners), default=0.0)
    net_threshold = round(max_false_positive_net + 2.0, 4) if false_positives else 0.0
    edge_threshold = round(max_false_positive_edge_to_cost + 0.1, 4) if false_positives else 0.0
    net_separates = bool(false_positives and winners and max_false_positive_net < min_winner_net)
    edge_separates = bool(false_positives and winners and max_false_positive_edge_to_cost < min_winner_edge_to_cost)
    avg_estimated_net = _average([row["estimated_net_edge_bps"] for row in _metric_rows(closed)])
    avg_realized_net = _average([row["realized_net_bps"] for row in _metric_rows(closed)])
    return {
        "winner_count": len(winners),
        "false_positive_count": len(false_positives),
        "max_false_positive_net_edge_bps": round(max_false_positive_net, 6),
        "min_winner_net_edge_bps": round(min_winner_net, 6),
        "net_edge_threshold_separates": net_separates,
        "suggested_min_net_edge_bps": net_threshold,
        "suggested_min_net_edge_would_block_winner": bool(winners and net_threshold >= min_winner_net),
        "max_false_positive_edge_to_cost": round(max_false_positive_edge_to_cost, 6),
        "min_winner_edge_to_cost": round(min_winner_edge_to_cost, 6),
        "edge_to_cost_threshold_separates": edge_separates,
        "suggested_min_edge_to_cost": edge_threshold,
        "suggested_min_edge_to_cost_would_block_winner": bool(winners and edge_threshold >= min_winner_edge_to_cost),
        "avg_estimated_net_edge_bps": round(avg_estimated_net, 6),
        "avg_realized_net_bps": round(avg_realized_net, 6),
        "suggested_edge_penalty_bps": round(max(0.0, avg_estimated_net - avg_realized_net), 6),
        "sample_warning": "sample_too_small_keep_conservative_gates" if len(closed) < 10 else "",
        "verdict": _threshold_verdict(
            winners=winners,
            false_positives=false_positives,
            net_separates=net_separates,
            edge_separates=edge_separates,
        ),
    }


def _threshold_verdict(
    *,
    winners: Sequence[Mapping[str, float]],
    false_positives: Sequence[Mapping[str, float]],
    net_separates: bool,
    edge_separates: bool,
) -> str:
    if not false_positives:
        return "no_false_positive_in_sample"
    if not winners:
        return "no_winner_in_sample_keep_or_tighten"
    if net_separates or edge_separates:
        return "threshold_candidate_requires_multifold_validation"
    return "edge_thresholds_do_not_separate_winners_from_false_positives"


def _bucket_rows(items: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    buckets: defaultdict[tuple[str, str], list[Mapping[str, object]]] = defaultdict(list)
    for item in items:
        for family, bucket in _bucket_keys(item):
            buckets[(family, bucket)].append(item)
    rows = [
        _bucket_row(family=family, bucket=bucket, items=rows)
        for (family, bucket), rows in buckets.items()
    ]
    return sorted(rows, key=_bucket_sort_key)


def _bucket_keys(item: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    candidate = _mapping(item.get("candidate"))
    trade = _mapping(item.get("trade"))
    path = _mapping(item.get("path"))
    labels = [
        str(label)
        for label in path.get("classifications", [])
        if isinstance(label, str) and label not in {"loser", "time_stop", "unclassified"}
    ]
    keys = [
        ("symbol", str(item.get("symbol", "") or "unknown")),
        ("side", f"side={item.get('side', 'unknown')}"),
        ("close_reason", str(trade.get("close_reason", "") or "unknown")),
        ("edge_to_cost", _edge_to_cost_bucket(_number(candidate.get("edge_to_cost_ratio")))),
        ("net_edge", _net_edge_bucket(_number(candidate.get("estimated_net_edge_bps")))),
        (
            "score_edge",
            f"score={_score_bucket(_number(candidate.get('score')))}|"
            f"net={_net_edge_bucket(_number(candidate.get('estimated_net_edge_bps')))}",
        ),
    ]
    for label in labels:
        keys.append(("path_label", label))
    if bool(path.get("fast_invalidation", False)):
        keys.append(("path_label", "fast_invalidation"))
    return tuple(keys)


def _bucket_row(
    *,
    family: str,
    bucket: str,
    items: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    row = _summary_row(bucket, items, key_name="bucket")
    row["bucket_family"] = family
    row["classification"] = _bucket_classification(row)
    return row


def _bucket_classification(row: Mapping[str, object]) -> str:
    trades = int(_number(row.get("closed_trades")))
    losses = int(_number(row.get("losing_trades")))
    pnl = _number(row.get("realized_pnl_usd"))
    win_rate = _number(row.get("winning_trades")) / trades if trades else 0.0
    if trades < 2 or losses < 2:
        return "insufficient_support"
    if pnl >= 0.0 or win_rate > 0.4:
        return "mixed_or_profitable"
    return "candidate_failure_cluster"


def _bucket_sort_key(row: Mapping[str, object]) -> tuple[float, float, int, str]:
    classification_rank = 0 if row.get("classification") == "candidate_failure_cluster" else 1
    return (
        classification_rank,
        _number(row.get("realized_pnl_usd")),
        -int(_number(row.get("closed_trades"))),
        str(row.get("bucket_family", "")),
    )


def _worst_trades(items: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    rows = sorted(
        items,
        key=lambda row: (
            _number(_mapping(row.get("trade")).get("pnl_usd")),
            str(row.get("timestamp", "")),
        ),
    )
    return [_compact_item(row) for row in rows[:20]]


def _compact_items(items: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    return [_compact_item(item) for item in items[:500]]


def _compact_item(item: Mapping[str, object]) -> dict[str, object]:
    candidate = _mapping(item.get("candidate"))
    trade = _mapping(item.get("trade"))
    path = _mapping(item.get("path"))
    flags = _mapping(item.get("flags"))
    llm = _mapping(item.get("llm"))
    return {
        "fold_label": item.get("fold_label", ""),
        "timestamp": item.get("timestamp", ""),
        "context_id": item.get("context_id", ""),
        "symbol": item.get("symbol", ""),
        "side": item.get("side", ""),
        "status": item.get("status", ""),
        "llm_action": llm.get("action", ""),
        "decision_id": trade.get("decision_id", llm.get("decision_id", "")),
        "pnl_usd": trade.get("pnl_usd", 0.0),
        "realized_net_bps": trade.get("realized_net_bps", 0.0),
        "estimated_net_edge_bps": candidate.get("estimated_net_edge_bps", 0.0),
        "edge_to_cost_ratio": candidate.get("edge_to_cost_ratio", 0.0),
        "score": candidate.get("score", 0.0),
        "close_reason": trade.get("close_reason", ""),
        "duration_minutes": path.get("duration_minutes", 0.0),
        "mfe_bps": path.get("mfe_bps", 0.0),
        "mae_bps": path.get("mae_bps", 0.0),
        "time_to_mae_minutes": path.get("time_to_mae_minutes", 0.0),
        "path_labels": path.get("classifications", []),
        "flags": flags,
    }


def _metric_row(item: Mapping[str, object]) -> dict[str, float]:
    candidate = _mapping(item.get("candidate"))
    trade = _mapping(item.get("trade"))
    return {
        "estimated_net_edge_bps": _number(candidate.get("estimated_net_edge_bps")),
        "edge_to_cost_ratio": _number(candidate.get("edge_to_cost_ratio")),
        "realized_net_bps": _number(trade.get("realized_net_bps")),
    }


def _metric_rows(items: Sequence[Mapping[str, object]]) -> list[dict[str, float]]:
    return [_metric_row(item) for item in items]


def _edge_to_cost_bucket(value: float) -> str:
    if value >= 4.25:
        return ">=4.25"
    if value >= 4.0:
        return "4.00-4.25"
    if value >= 3.75:
        return "3.75-4.00"
    if value >= 3.25:
        return "3.25-3.75"
    return "<3.25"


def _net_edge_bucket(value: float) -> str:
    if value >= 35.0:
        return ">=35"
    if value >= 30.0:
        return "30-35"
    if value >= 25.0:
        return "25-30"
    return "<25"


def _score_bucket(value: float) -> str:
    if value >= 5.0:
        return ">=5"
    if value >= 4.0:
        return "4-5"
    if value >= 3.0:
        return "3-4"
    return "<3"


def _validate_inputs(
    *,
    candidate_input_paths: Sequence[str | Path],
    llm_journal_paths: Sequence[str | Path],
    paper_journal_paths: Sequence[str | Path],
    market_input_paths: Sequence[str | Path],
    fold_labels: Sequence[str] | None,
    early_adverse_bps: float,
    min_follow_through_bps: float,
    giveback_bps: float,
    fast_invalidation_minutes: int,
) -> None:
    if not candidate_input_paths:
        raise ValueError("candidate_input_paths_required")
    counts = {
        len(candidate_input_paths),
        len(llm_journal_paths),
        len(paper_journal_paths),
        len(market_input_paths),
    }
    if len(counts) != 1:
        raise ValueError("input_counts_must_match")
    if fold_labels is not None and len(fold_labels) != len(candidate_input_paths):
        raise ValueError("fold_label_count_must_match_inputs")
    if early_adverse_bps <= 0.0:
        raise ValueError("early_adverse_bps_must_be_positive")
    if min_follow_through_bps < 0.0:
        raise ValueError("min_follow_through_bps_must_be_non_negative")
    if giveback_bps <= 0.0:
        raise ValueError("giveback_bps_must_be_positive")
    if fast_invalidation_minutes <= 0:
        raise ValueError("fast_invalidation_minutes_must_be_positive")


def _fold_labels(labels: Sequence[str] | None, expected_count: int) -> tuple[str, ...]:
    if labels is None:
        return tuple(f"fold_{index + 1}" for index in range(expected_count))
    return tuple(str(label).strip() or f"fold_{index + 1}" for index, label in enumerate(labels))


def _symbols_filter(symbols: Sequence[str] | None) -> tuple[str, ...]:
    if not symbols:
        return ()
    return tuple(symbol.strip().upper() for symbol in symbols if symbol.strip())


def _bps(pnl: float, notional: float) -> float:
    if notional <= 0.0:
        return 0.0
    return pnl / notional * 10_000.0


def _average(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


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
    summary = _mapping(result.get("summary"))
    thresholds = _mapping(result.get("threshold_diagnostics"))
    lines = [
        "# TRIDENT-AI Edge Path Calibration",
        "",
        f"- Generated at: `{payload.get('generated_at', '')}`",
        f"- Candidate inputs: `{result.get('candidate_input_paths', [])}`",
        f"- LLM journals: `{result.get('llm_journal_paths', [])}`",
        f"- Paper journals: `{result.get('paper_journal_paths', [])}`",
        f"- Market inputs: `{result.get('market_input_paths', [])}`",
        f"- Fold labels: `{result.get('fold_labels', [])}`",
        "",
        "## Summary",
        "",
        f"- Candidates / opens / holds: `{summary.get('candidates_seen', 0)}` / `{summary.get('open_decisions', 0)}` / `{summary.get('hold_decisions', 0)}`",
        f"- Closed trades / false positives: `{summary.get('closed_trades', 0)}` / `{summary.get('false_positive_trades', 0)}`",
        f"- Realized PnL: `${_number(summary.get('realized_pnl_usd')):.6f}`",
        f"- Avg realized net / estimated net: `{_number(summary.get('avg_realized_net_bps')):.2f} bps` / `{_number(summary.get('avg_estimated_net_edge_bps')):.2f} bps`",
        f"- Avg MFE / MAE: `{_number(summary.get('avg_mfe_bps')):.2f} bps` / `{_number(summary.get('avg_mae_bps')):.2f} bps`",
        f"- Fast invalidations / early adverse / no follow-through / gave-back-to-loss: `{summary.get('fast_invalidations', 0)}` / `{summary.get('early_adverse_losses', 0)}` / `{summary.get('no_follow_through_losses', 0)}` / `{summary.get('gave_back_to_loss', 0)}`",
        "",
        "## Threshold Diagnostics",
        "",
        f"- Verdict: `{thresholds.get('verdict', '')}`",
        f"- Suggested net-edge threshold: `{_number(thresholds.get('suggested_min_net_edge_bps')):.4f}` bps; would block winner: `{thresholds.get('suggested_min_net_edge_would_block_winner', False)}`",
        f"- Suggested edge/cost threshold: `{_number(thresholds.get('suggested_min_edge_to_cost')):.4f}`; would block winner: `{thresholds.get('suggested_min_edge_to_cost_would_block_winner', False)}`",
        f"- Suggested edge penalty: `{_number(thresholds.get('suggested_edge_penalty_bps')):.2f} bps`",
        f"- Warning: `{thresholds.get('sample_warning', '')}`",
        "",
        "## Folds",
        "",
        "| Fold | Closed | PnL | Avg net | False positives | Fast invalidations |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in result.get("fold_rows", []):
        if isinstance(row, Mapping):
            lines.append(
                f"| `{row.get('fold_label')}` | {int(_number(row.get('closed_trades')))} | "
                f"`${_number(row.get('realized_pnl_usd')):.6f}` | "
                f"`{_number(row.get('avg_realized_net_bps')):.2f}` | "
                f"{int(_number(row.get('false_positive_trades')))} | "
                f"{int(_number(row.get('fast_invalidations')))} |"
            )
    lines.extend(
        [
            "",
            "## Top Buckets",
            "",
            "| Family | Bucket | Class | Trades | PnL | Avg net | False positives |",
            "|---|---|---|---:|---:|---:|---:|",
        ]
    )
    for row in result.get("bucket_rows", [])[:20]:
        if isinstance(row, Mapping):
            lines.append(
                f"| `{row.get('bucket_family')}` | `{row.get('bucket')}` | "
                f"`{row.get('classification')}` | {int(_number(row.get('closed_trades')))} | "
                f"`${_number(row.get('realized_pnl_usd')):.6f}` | "
                f"`{_number(row.get('avg_realized_net_bps')):.2f}` | "
                f"{int(_number(row.get('false_positive_trades')))} |"
            )
    return "\n".join(lines) + "\n"
