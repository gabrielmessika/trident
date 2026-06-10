from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.trident_ai.candidate_scan import CANDIDATE_HINT_FIELD
from app.trident_ai.config import TridentAIConfig, load_trident_ai_config
from app.trident_ai.exit_audit import (
    DEFAULT_EARLY_ADVERSE_BPS,
    DEFAULT_EXIT_AUDIT_WINDOWS_MINUTES,
    DEFAULT_GIVEBACK_BPS,
    DEFAULT_MIN_FOLLOW_THROUGH_BPS,
    _closed_trade_rows,
    _market_price_index,
    _normalize_windows,
    _trade_item,
)
from app.trident_ai.pattern_calibration import (
    _decision_rows,
    _format_timestamp,
    _mapping,
    _number,
    _pattern_descriptor,
    _string_list,
    _timestamp_id,
)


DEFAULT_FAILURE_PATTERN_MIN_TRADES = 2
DEFAULT_FAILURE_PATTERN_MIN_LOSS_TRADES = 2
DEFAULT_FAILURE_PATTERN_MIN_LOSS_FOLDS = 2
DEFAULT_FAILURE_PATTERN_MIN_LOSS_SYMBOLS = 2
DEFAULT_FAILURE_PATTERN_MAX_WIN_RATE = 0.40
DEFAULT_FAILURE_PATTERN_MAX_DOMINANT_LOSS_SYMBOL_RATIO = 0.70


@dataclass(frozen=True, slots=True)
class TridentAIFailurePatternAuditResult:
    decision_journal_paths: tuple[str, ...]
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
    min_trades: int = DEFAULT_FAILURE_PATTERN_MIN_TRADES
    min_loss_trades: int = DEFAULT_FAILURE_PATTERN_MIN_LOSS_TRADES
    min_loss_folds: int = DEFAULT_FAILURE_PATTERN_MIN_LOSS_FOLDS
    min_loss_symbols: int = DEFAULT_FAILURE_PATTERN_MIN_LOSS_SYMBOLS
    max_win_rate: float = DEFAULT_FAILURE_PATTERN_MAX_WIN_RATE
    max_dominant_loss_symbol_ratio: float = (
        DEFAULT_FAILURE_PATTERN_MAX_DOMINANT_LOSS_SYMBOL_RATIO
    )
    summary: dict[str, object] = field(default_factory=dict)
    fold_rows: list[dict[str, object]] = field(default_factory=list)
    bucket_rows: list[dict[str, object]] = field(default_factory=list)
    veto_candidate_buckets: list[dict[str, object]] = field(default_factory=list)
    symbol_concentrated_failure_buckets: list[dict[str, object]] = field(default_factory=list)
    fold_concentrated_failure_buckets: list[dict[str, object]] = field(default_factory=list)
    mixed_failure_buckets: list[dict[str, object]] = field(default_factory=list)
    worst_trades: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "decision_journal_paths": list(self.decision_journal_paths),
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
            "min_trades": self.min_trades,
            "min_loss_trades": self.min_loss_trades,
            "min_loss_folds": self.min_loss_folds,
            "min_loss_symbols": self.min_loss_symbols,
            "max_win_rate": round(self.max_win_rate, 6),
            "max_dominant_loss_symbol_ratio": round(
                self.max_dominant_loss_symbol_ratio,
                6,
            ),
            "summary": self.summary,
            "fold_rows": self.fold_rows,
            "bucket_rows": self.bucket_rows,
            "veto_candidate_buckets": self.veto_candidate_buckets,
            "symbol_concentrated_failure_buckets": (
                self.symbol_concentrated_failure_buckets
            ),
            "fold_concentrated_failure_buckets": self.fold_concentrated_failure_buckets,
            "mixed_failure_buckets": self.mixed_failure_buckets,
            "worst_trades": self.worst_trades,
        }


def run_trident_ai_failure_pattern_audit(
    *,
    decision_journal_paths: Sequence[str | Path],
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
    min_trades: int = DEFAULT_FAILURE_PATTERN_MIN_TRADES,
    min_loss_trades: int = DEFAULT_FAILURE_PATTERN_MIN_LOSS_TRADES,
    min_loss_folds: int = DEFAULT_FAILURE_PATTERN_MIN_LOSS_FOLDS,
    min_loss_symbols: int = DEFAULT_FAILURE_PATTERN_MIN_LOSS_SYMBOLS,
    max_win_rate: float = DEFAULT_FAILURE_PATTERN_MAX_WIN_RATE,
    max_dominant_loss_symbol_ratio: float = (
        DEFAULT_FAILURE_PATTERN_MAX_DOMINANT_LOSS_SYMBOL_RATIO
    ),
) -> TridentAIFailurePatternAuditResult:
    _validate_inputs(
        decision_journal_paths=decision_journal_paths,
        paper_journal_paths=paper_journal_paths,
        market_input_paths=market_input_paths,
        fold_labels=fold_labels,
        early_adverse_bps=early_adverse_bps,
        min_follow_through_bps=min_follow_through_bps,
        giveback_bps=giveback_bps,
        min_trades=min_trades,
        min_loss_trades=min_loss_trades,
        min_loss_folds=min_loss_folds,
        min_loss_symbols=min_loss_symbols,
        max_win_rate=max_win_rate,
        max_dominant_loss_symbol_ratio=max_dominant_loss_symbol_ratio,
    )
    windows = _normalize_windows(windows_minutes)
    active_config = config or load_trident_ai_config()
    run_id = _timestamp_id(datetime.now(timezone.utc))
    output_dir = Path(active_config.paths.replay_output_dir)
    json_output = Path(
        report_json_path or output_dir / f"trident_ai_failure_pattern_audit_{run_id}.json"
    )
    md_output = Path(
        report_md_path or output_dir / f"trident_ai_failure_pattern_audit_{run_id}.md"
    )
    labels = _fold_labels(fold_labels, len(decision_journal_paths))
    symbols_filter = _symbols_filter(symbols)
    allowed = set(symbols_filter)

    observations: list[dict[str, object]] = []
    fold_rows: list[dict[str, object]] = []
    for label, decision_path, paper_path, market_path in zip(
        labels,
        decision_journal_paths,
        paper_journal_paths,
        market_input_paths,
        strict=True,
    ):
        market_index = _market_price_index(market_path)
        decisions = _open_decisions_by_id(decision_path)
        fold_observations: list[dict[str, object]] = []
        for trade in _safe_closed_trade_rows(paper_path):
            symbol = str(trade.get("symbol", "") or "").upper()
            if allowed and symbol not in allowed:
                continue
            path_row = _trade_item(
                trade,
                market_index=market_index,
                fold_label=label,
                paper_journal_path=str(paper_path),
                market_input_path=str(market_path),
                windows=windows,
                early_adverse_bps=float(early_adverse_bps),
                min_follow_through_bps=float(min_follow_through_bps),
                giveback_bps=float(giveback_bps),
            )
            observation = _observation_from_trade(
                trade=trade,
                path_row=path_row,
                decision_row=decisions.get(str(trade.get("decision_id", "") or "")),
                fold_label=label,
            )
            observations.append(observation)
            fold_observations.append(observation)
        fold_rows.append(_group_row(label, fold_observations, key_name="fold_label"))

    buckets: defaultdict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for observation in observations:
        for family, bucket in _bucket_keys(observation):
            buckets[(family, bucket)].append(observation)

    bucket_rows = [
        _bucket_row(
            family=family,
            bucket=bucket,
            observations=rows,
            min_trades=min_trades,
            min_loss_trades=min_loss_trades,
            min_loss_folds=min_loss_folds,
            min_loss_symbols=min_loss_symbols,
            max_win_rate=max_win_rate,
            max_dominant_loss_symbol_ratio=max_dominant_loss_symbol_ratio,
        )
        for (family, bucket), rows in buckets.items()
    ]
    bucket_rows.sort(key=_bucket_sort_key)
    veto_candidates = [row for row in bucket_rows if row["classification"] == "veto_candidate"]
    symbol_concentrated = [
        row for row in bucket_rows if row["classification"] == "symbol_concentrated_failure"
    ]
    fold_concentrated = [
        row for row in bucket_rows if row["classification"] == "fold_concentrated_failure"
    ]
    mixed = [row for row in bucket_rows if row["classification"] == "mixed_or_profitable"]

    result = TridentAIFailurePatternAuditResult(
        decision_journal_paths=tuple(str(path) for path in decision_journal_paths),
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
        min_trades=min_trades,
        min_loss_trades=min_loss_trades,
        min_loss_folds=min_loss_folds,
        min_loss_symbols=min_loss_symbols,
        max_win_rate=float(max_win_rate),
        max_dominant_loss_symbol_ratio=float(max_dominant_loss_symbol_ratio),
        summary=_group_row("all", observations, key_name="scope"),
        fold_rows=fold_rows,
        bucket_rows=bucket_rows[:300],
        veto_candidate_buckets=veto_candidates[:40],
        symbol_concentrated_failure_buckets=symbol_concentrated[:40],
        fold_concentrated_failure_buckets=fold_concentrated[:40],
        mixed_failure_buckets=mixed[:40],
        worst_trades=_worst_trades(observations),
    )
    payload = build_failure_pattern_audit_report_payload(
        result=result,
        generated_at=_format_timestamp(datetime.now(timezone.utc)),
    )
    _write_report_outputs(payload, json_path=json_output, md_path=md_output)
    return result


def build_failure_pattern_audit_report_payload(
    *,
    result: TridentAIFailurePatternAuditResult,
    generated_at: str,
) -> dict[str, object]:
    return {
        "generated_at": generated_at,
        "kind": "trident_ai_failure_pattern_audit",
        "result": result.to_dict(),
    }


def _validate_inputs(
    *,
    decision_journal_paths: Sequence[str | Path],
    paper_journal_paths: Sequence[str | Path],
    market_input_paths: Sequence[str | Path],
    fold_labels: Sequence[str] | None,
    early_adverse_bps: float,
    min_follow_through_bps: float,
    giveback_bps: float,
    min_trades: int,
    min_loss_trades: int,
    min_loss_folds: int,
    min_loss_symbols: int,
    max_win_rate: float,
    max_dominant_loss_symbol_ratio: float,
) -> None:
    if not decision_journal_paths:
        raise ValueError("decision_journal_paths_required")
    if len(decision_journal_paths) != len(paper_journal_paths):
        raise ValueError("decision_and_paper_journal_counts_must_match")
    if len(decision_journal_paths) != len(market_input_paths):
        raise ValueError("decision_and_market_input_counts_must_match")
    if fold_labels is not None and len(fold_labels) != len(decision_journal_paths):
        raise ValueError("fold_label_count_must_match_input_count")
    if early_adverse_bps <= 0.0:
        raise ValueError("early_adverse_bps_must_be_positive")
    if min_follow_through_bps < 0.0:
        raise ValueError("min_follow_through_bps_must_be_non_negative")
    if giveback_bps <= 0.0:
        raise ValueError("giveback_bps_must_be_positive")
    if min_trades <= 0:
        raise ValueError("min_trades_must_be_positive")
    if min_loss_trades <= 0:
        raise ValueError("min_loss_trades_must_be_positive")
    if min_loss_folds <= 0:
        raise ValueError("min_loss_folds_must_be_positive")
    if min_loss_symbols <= 0:
        raise ValueError("min_loss_symbols_must_be_positive")
    if not 0.0 <= max_win_rate <= 1.0:
        raise ValueError("max_win_rate_must_be_between_0_and_1")
    if not 0.0 < max_dominant_loss_symbol_ratio <= 1.0:
        raise ValueError("max_dominant_loss_symbol_ratio_must_be_between_zero_and_one")


def _observation_from_trade(
    *,
    trade: Mapping[str, object],
    path_row: Mapping[str, object],
    decision_row: Mapping[str, object] | None,
    fold_label: str,
) -> dict[str, object]:
    decision = _mapping(decision_row)
    proposal = _mapping(decision.get("proposal"))
    context = _mapping(decision.get("context"))
    descriptor = _pattern_descriptor(context=context, proposal=proposal)
    features = _mapping(context.get("features"))
    hint = _mapping(context.get(CANDIDATE_HINT_FIELD))
    net_bps = _number(path_row.get("net_bps"))
    labels = tuple(
        str(label)
        for label in path_row.get("classifications", [])
        if isinstance(label, str) and label
    )
    window_outcomes = _mapping(path_row.get("window_outcomes"))
    return {
        "fold_label": fold_label,
        "decision_id": str(trade.get("decision_id", "") or ""),
        "timestamp": str(decision.get("timestamp", "") or trade.get("opened_at", "") or ""),
        "opened_at": str(trade.get("opened_at", "") or ""),
        "closed_at": str(trade.get("closed_at", "") or ""),
        "symbol": str(trade.get("symbol", "") or "").upper(),
        "side": str(trade.get("side", "") or "").lower(),
        "pattern_key": descriptor.pattern_key,
        "regime": descriptor.regime,
        "microprice": descriptor.microprice,
        "flow_book": descriptor.flow_book,
        "vwap": descriptor.vwap,
        "edge_bucket": descriptor.edge_bucket,
        "net_edge_bucket": descriptor.net_edge_bucket,
        "volatility_bucket": descriptor.volatility_bucket,
        "market_cluster": str(features.get("market_cluster", "") or "unknown"),
        "liquidity_bucket": _liquidity_bucket(_number(hint.get("liquidity_score"))),
        "activity_bucket": _activity_bucket(_number(hint.get("activity_score"))),
        "cost_bucket": _cost_bucket(_number(hint.get("round_trip_cost_bps"))),
        "edge_to_cost_ratio": round(_number(hint.get("edge_to_cost_ratio")), 6),
        "estimated_net_edge_bps": round(_number(hint.get("estimated_net_edge_bps")), 6),
        "liquidity_score": round(_number(hint.get("liquidity_score")), 6),
        "round_trip_cost_bps": round(_number(hint.get("round_trip_cost_bps")), 6),
        "pattern_reasons": tuple(_string_list(hint.get("pattern_reasons"))),
        "reasons": tuple(_string_list(hint.get("reasons"))),
        "close_reason": str(trade.get("close_reason", "") or ""),
        "notional_usd": round(_number(trade.get("notional_usd")), 6),
        "gross_pnl_usd": round(_number(trade.get("gross_pnl_usd")), 6),
        "fees_usd": round(_number(trade.get("fees_usd")), 6),
        "pnl_usd": round(_number(trade.get("pnl_usd")), 6),
        "net_bps": round(net_bps, 6),
        "is_loss": net_bps < 0.0,
        "duration_minutes": round(_number(path_row.get("duration_minutes")), 6),
        "path_available": bool(path_row.get("path_available", False)),
        "mfe_bps": round(_number(path_row.get("mfe_bps")), 6),
        "mae_bps": round(_number(path_row.get("mae_bps")), 6),
        "giveback_bps": round(_number(path_row.get("giveback_bps")), 6),
        "time_to_mfe_minutes": round(_number(path_row.get("time_to_mfe_minutes")), 6),
        "time_to_mae_minutes": round(_number(path_row.get("time_to_mae_minutes")), 6),
        "window_outcomes": window_outcomes,
        "failure_labels": labels,
        "paper_journal_path": str(path_row.get("paper_journal_path", "") or ""),
        "market_input_path": str(path_row.get("market_input_path", "") or ""),
    }


def _bucket_keys(observation: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    microstructure = (
        f"microprice={observation.get('microprice')}|"
        f"flow_book={observation.get('flow_book')}|"
        f"vwap={observation.get('vwap')}"
    )
    edge_liquidity = (
        f"edge={observation.get('edge_bucket')}|"
        f"net_edge={observation.get('net_edge_bucket')}|"
        f"liquidity={observation.get('liquidity_bucket')}|"
        f"cost={observation.get('cost_bucket')}"
    )
    keys: list[tuple[str, str]] = [
        ("fold", str(observation.get("fold_label", "") or "unknown")),
        ("symbol", str(observation.get("symbol", "") or "unknown")),
        ("side", f"side={observation.get('side', 'unknown')}"),
        ("close_reason", str(observation.get("close_reason", "") or "unknown")),
        ("pattern", str(observation.get("pattern_key", "") or "unknown")),
        ("side_pattern", f"side={observation.get('side')}|{observation.get('pattern_key')}"),
        (
            "pattern_regime",
            f"{observation.get('pattern_key')}|regime={observation.get('regime')}",
        ),
        ("microstructure", microstructure),
        ("edge_liquidity", edge_liquidity),
        (
            "cluster_pattern",
            f"cluster={observation.get('market_cluster')}|{observation.get('pattern_key')}",
        ),
    ]
    for label in observation.get("failure_labels", ()):
        if label in {"loser", "time_stop", "unclassified"}:
            continue
        keys.append(("failure_label", str(label)))
        keys.append(("failure_label_pattern", f"{label}|{observation.get('pattern_key')}"))
    return tuple(keys)


def _bucket_row(
    *,
    family: str,
    bucket: str,
    observations: Sequence[Mapping[str, object]],
    min_trades: int,
    min_loss_trades: int,
    min_loss_folds: int,
    min_loss_symbols: int,
    max_win_rate: float,
    max_dominant_loss_symbol_ratio: float,
) -> dict[str, object]:
    row = _group_row(bucket, observations, key_name="bucket")
    losses = [item for item in observations if bool(item.get("is_loss", False))]
    loss_folds = Counter(str(item.get("fold_label", "") or "unknown") for item in losses)
    loss_symbols = Counter(str(item.get("symbol", "") or "unknown") for item in losses)
    dominant_loss_symbol, dominant_loss_count = _dominant(loss_symbols)
    dominant_loss_ratio = dominant_loss_count / len(losses) if losses else 0.0
    row.update(
        {
            "bucket_family": family,
            "loss_folds": dict(sorted(loss_folds.items())),
            "loss_fold_count": len(loss_folds),
            "loss_symbols": dict(sorted(loss_symbols.items())),
            "loss_symbol_count": len(loss_symbols),
            "dominant_loss_symbol": dominant_loss_symbol,
            "dominant_loss_symbol_ratio": round(dominant_loss_ratio, 6),
        }
    )
    row["classification"] = _bucket_classification(
        row=row,
        min_trades=min_trades,
        min_loss_trades=min_loss_trades,
        min_loss_folds=min_loss_folds,
        min_loss_symbols=min_loss_symbols,
        max_win_rate=max_win_rate,
        max_dominant_loss_symbol_ratio=max_dominant_loss_symbol_ratio,
    )
    return row


def _bucket_classification(
    *,
    row: Mapping[str, object],
    min_trades: int,
    min_loss_trades: int,
    min_loss_folds: int,
    min_loss_symbols: int,
    max_win_rate: float,
    max_dominant_loss_symbol_ratio: float,
) -> str:
    trades = int(_number(row.get("trades")))
    losses = int(_number(row.get("losses")))
    loss_fold_count = int(_number(row.get("loss_fold_count")))
    loss_symbol_count = int(_number(row.get("loss_symbol_count")))
    win_rate = _number(row.get("win_rate"))
    pnl = _number(row.get("pnl_usd"))
    dominant_loss_ratio = _number(row.get("dominant_loss_symbol_ratio"))
    if trades < min_trades or losses < min_loss_trades:
        return "insufficient_loss_support"
    if pnl >= 0.0 or win_rate > max_win_rate:
        return "mixed_or_profitable"
    if loss_fold_count < min_loss_folds:
        return "fold_concentrated_failure"
    if loss_symbol_count < min_loss_symbols or dominant_loss_ratio > max_dominant_loss_symbol_ratio:
        return "symbol_concentrated_failure"
    return "veto_candidate"


def _group_row(
    name: str,
    observations: Sequence[Mapping[str, object]],
    *,
    key_name: str,
) -> dict[str, object]:
    trades = list(observations)
    losses = [item for item in trades if bool(item.get("is_loss", False))]
    wins = [item for item in trades if not bool(item.get("is_loss", False))]
    notional = sum(_number(item.get("notional_usd")) for item in trades)
    pnl = sum(_number(item.get("pnl_usd")) for item in trades)
    gross = sum(_number(item.get("gross_pnl_usd")) for item in trades)
    fees = sum(_number(item.get("fees_usd")) for item in trades)
    failure_labels: Counter[str] = Counter()
    for item in trades:
        labels = item.get("failure_labels", ())
        if isinstance(labels, Sequence) and not isinstance(labels, (str, bytes)):
            failure_labels.update(str(label) for label in labels if str(label))
    return {
        key_name: name,
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades), 6) if trades else 0.0,
        "pnl_usd": round(pnl, 6),
        "gross_pnl_usd": round(gross, 6),
        "fees_usd": round(fees, 6),
        "avg_net_bps": round(_bps(pnl, notional), 6),
        "avg_loss_bps": round(_average([_number(item.get("net_bps")) for item in losses]), 6),
        "avg_win_bps": round(_average([_number(item.get("net_bps")) for item in wins]), 6),
        "avg_mfe_bps": round(_average([_number(item.get("mfe_bps")) for item in trades]), 6),
        "avg_mae_bps": round(_average([_number(item.get("mae_bps")) for item in trades]), 6),
        "avg_duration_minutes": round(
            _average([_number(item.get("duration_minutes")) for item in trades]),
            6,
        ),
        "symbols": dict(Counter(str(item.get("symbol", "") or "unknown") for item in trades)),
        "folds": dict(Counter(str(item.get("fold_label", "") or "unknown") for item in trades)),
        "close_reasons": dict(Counter(str(item.get("close_reason", "") or "unknown") for item in trades)),
        "failure_labels": dict(failure_labels.most_common(12)),
        "window_stats": _window_stats(trades),
    }


def _window_stats(observations: Sequence[Mapping[str, object]]) -> dict[str, dict[str, object]]:
    windows = sorted(
        {
            str(window)
            for item in observations
            for window in _mapping(item.get("window_outcomes")).keys()
        },
        key=lambda value: int(value) if value.isdigit() else 0,
    )
    result: dict[str, dict[str, object]] = {}
    for window in windows:
        rows = []
        for item in observations:
            outcome = _mapping(_mapping(item.get("window_outcomes")).get(window))
            if bool(outcome.get("available", False)):
                rows.append(outcome)
        result[window] = {
            "samples": len(rows),
            "positive_rate": round(
                sum(1 for row in rows if _number(row.get("gross_at_window_bps")) > 0.0)
                / len(rows),
                6,
            )
            if rows
            else 0.0,
            "avg_gross_at_window_bps": round(
                _average([_number(row.get("gross_at_window_bps")) for row in rows]),
                6,
            ),
            "avg_early_mfe_bps": round(
                _average([_number(row.get("early_mfe_bps")) for row in rows]),
                6,
            ),
            "avg_early_mae_bps": round(
                _average([_number(row.get("early_mae_bps")) for row in rows]),
                6,
            ),
        }
    return result


def _open_decisions_by_id(path: str | Path) -> dict[str, dict[str, object]]:
    rows: dict[str, dict[str, object]] = {}
    for row in _safe_decision_rows(path):
        proposal = _mapping(row.get("proposal"))
        if str(proposal.get("action", "") or "").lower() != "open":
            continue
        decision_id = str(proposal.get("decision_id", "") or "")
        if decision_id:
            rows[decision_id] = row
    return rows


def _safe_decision_rows(path: str | Path) -> list[dict[str, object]]:
    return _decision_rows(path) if Path(path).exists() else []


def _safe_closed_trade_rows(path: str | Path) -> list[dict[str, object]]:
    return _closed_trade_rows(path) if Path(path).exists() else []


def _worst_trades(observations: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    keys = (
        "fold_label",
        "decision_id",
        "symbol",
        "side",
        "opened_at",
        "closed_at",
        "close_reason",
        "pnl_usd",
        "net_bps",
        "mfe_bps",
        "mae_bps",
        "giveback_bps",
        "duration_minutes",
        "pattern_key",
        "regime",
        "microprice",
        "flow_book",
        "vwap",
        "edge_bucket",
        "net_edge_bucket",
        "liquidity_bucket",
        "cost_bucket",
        "failure_labels",
    )
    rows = sorted(observations, key=lambda item: (_number(item.get("net_bps")), str(item.get("opened_at", ""))))
    return [{key: row.get(key) for key in keys} for row in rows[:60]]


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
    lines = [
        "# TRIDENT-AI Failure Pattern Audit",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Decision journals: `{result.get('decision_journal_paths', [])}`",
        f"- Paper journals: `{result.get('paper_journal_paths', [])}`",
        f"- Market inputs: `{result.get('market_input_paths', [])}`",
        f"- Fold labels: `{result.get('fold_labels', [])}`",
        f"- Symbols filter: `{result.get('symbols_filter', [])}`",
        f"- Windows minutes: `{result.get('windows_minutes', [])}`",
        "",
        "## Summary",
        "",
        f"- Trades: `{summary.get('trades', 0)}`",
        f"- Wins / losses: `{summary.get('wins', 0)}` / `{summary.get('losses', 0)}`",
        f"- Win rate: `{_number(summary.get('win_rate')):.2%}`",
        f"- Realized PnL: `${_number(summary.get('pnl_usd')):.6f}`",
        f"- Avg net: `{_number(summary.get('avg_net_bps')):.2f} bps`",
        f"- Avg MFE / MAE: `{_number(summary.get('avg_mfe_bps')):.2f}` / `{_number(summary.get('avg_mae_bps')):.2f} bps`",
        "",
        "## Bucket Classifications",
        "",
        "| Classification | Count |",
        "|---|---:|",
    ]
    bucket_rows = [row for row in result.get("bucket_rows", []) if isinstance(row, Mapping)]
    class_counts = Counter(str(row.get("classification", "") or "unknown") for row in bucket_rows)
    if class_counts:
        for name, count in sorted(class_counts.items()):
            lines.append(f"| {name} | {count} |")
    else:
        lines.append("| none | 0 |")

    _append_bucket_section(
        lines,
        title="Veto Candidates",
        rows=result.get("veto_candidate_buckets", []),
    )
    _append_bucket_section(
        lines,
        title="Symbol-Concentrated Failures",
        rows=result.get("symbol_concentrated_failure_buckets", []),
    )
    _append_bucket_section(
        lines,
        title="Fold-Concentrated Failures",
        rows=result.get("fold_concentrated_failure_buckets", []),
    )
    _append_bucket_section(lines, title="Top Failure Buckets", rows=bucket_rows[:30])

    lines.extend(
        [
            "",
            "## Folds",
            "",
            "| Fold | Trades | Wins | Losses | PnL | Avg net | Failure labels |",
            "|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for fold in result.get("fold_rows", []):
        if isinstance(fold, Mapping):
            lines.append(_fold_row_markdown(fold))
    if not result.get("fold_rows"):
        lines.append("| none | 0 | 0 | 0 | `$0.000000` | `0.00` | none |")

    lines.extend(
        [
            "",
            "## Worst Trades",
            "",
            "| Fold | Symbol | Side | Opened | Close | Net | MFE | MAE | Pattern | Labels |",
            "|---|---|---|---|---|---:|---:|---:|---|---|",
        ]
    )
    for trade in result.get("worst_trades", [])[:40]:
        if isinstance(trade, Mapping):
            lines.append(_trade_row_markdown(trade))
    if not result.get("worst_trades"):
        lines.append("| none | n/a | n/a | n/a | n/a | 0.00 | 0.00 | 0.00 | n/a | n/a |")
    lines.append("")
    return "\n".join(lines)


def _append_bucket_section(lines: list[str], *, title: str, rows: object) -> None:
    lines.extend(
        [
            "",
            f"## {title}",
            "",
            "| Family | Bucket | Class | Trades | Losses | Loss folds | Loss symbols | PnL | Avg net | Labels |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    if not isinstance(rows, list) or not rows:
        lines.append("| none | n/a | n/a | 0 | 0 | 0 | 0 | `$0.000000` | `0.00` | none |")
        return
    for row in rows[:40]:
        if isinstance(row, Mapping):
            lines.append(_bucket_row_markdown(row))


def _bucket_row_markdown(row: Mapping[str, object]) -> str:
    labels = _mapping(row.get("failure_labels"))
    label_text = ", ".join(f"{label}:{count}" for label, count in labels.items()) or "none"
    return (
        f"| `{row.get('bucket_family', '')}` | `{row.get('bucket', '')}` | "
        f"`{row.get('classification', '')}` | {int(_number(row.get('trades')))} | "
        f"{int(_number(row.get('losses')))} | {int(_number(row.get('loss_fold_count')))} | "
        f"{int(_number(row.get('loss_symbol_count')))} | "
        f"`${_number(row.get('pnl_usd')):.6f}` | `{_number(row.get('avg_net_bps')):.2f}` | "
        f"{label_text} |"
    )


def _fold_row_markdown(row: Mapping[str, object]) -> str:
    labels = _mapping(row.get("failure_labels"))
    label_text = ", ".join(f"{label}:{count}" for label, count in labels.items()) or "none"
    return (
        f"| `{row.get('fold_label', '')}` | {int(_number(row.get('trades')))} | "
        f"{int(_number(row.get('wins')))} | {int(_number(row.get('losses')))} | "
        f"`${_number(row.get('pnl_usd')):.6f}` | `{_number(row.get('avg_net_bps')):.2f}` | "
        f"{label_text} |"
    )


def _trade_row_markdown(row: Mapping[str, object]) -> str:
    labels = row.get("failure_labels", ())
    label_text = ", ".join(str(label) for label in labels) if isinstance(labels, Sequence) else ""
    return (
        f"| `{row.get('fold_label', '')}` | `{row.get('symbol', '')}` | `{row.get('side', '')}` | "
        f"`{row.get('opened_at', '')}` | `{row.get('close_reason', '')}` | "
        f"`{_number(row.get('net_bps')):.2f}` | `{_number(row.get('mfe_bps')):.2f}` | "
        f"`{_number(row.get('mae_bps')):.2f}` | `{row.get('pattern_key', '')}` | {label_text} |"
    )


def _bucket_sort_key(row: Mapping[str, object]) -> tuple[int, float, float, float, str, str]:
    rank = {
        "veto_candidate": 0,
        "symbol_concentrated_failure": 1,
        "fold_concentrated_failure": 2,
        "mixed_or_profitable": 3,
        "insufficient_loss_support": 4,
    }
    return (
        rank.get(str(row.get("classification", "")), 9),
        -_number(row.get("losses")),
        _number(row.get("pnl_usd")),
        _number(row.get("avg_net_bps")),
        str(row.get("bucket_family", "")),
        str(row.get("bucket", "")),
    )


def _fold_labels(labels: Sequence[str] | None, expected_count: int) -> tuple[str, ...]:
    if labels is None:
        return tuple(f"fold_{index + 1}" for index in range(expected_count))
    return tuple(str(label).strip() or f"fold_{index + 1}" for index, label in enumerate(labels))


def _symbols_filter(symbols: Sequence[str] | None) -> tuple[str, ...]:
    if not symbols:
        return ()
    normalized: list[str] = []
    for symbol in symbols:
        value = str(symbol).strip().upper()
        if value and value not in normalized:
            normalized.append(value)
    return tuple(normalized)


def _dominant(counter: Counter[str]) -> tuple[str, int]:
    if not counter:
        return "", 0
    return counter.most_common(1)[0]


def _liquidity_bucket(value: float) -> str:
    if value >= 1.2:
        return "high"
    if value >= 1.0:
        return "normal"
    if value > 0.0:
        return "low"
    return "missing"


def _activity_bucket(value: float) -> str:
    if value >= 1.1:
        return "high"
    if value >= 0.8:
        return "normal"
    if value > 0.0:
        return "low"
    return "missing"


def _cost_bucket(value: float) -> str:
    if value <= 0.0:
        return "missing"
    if value <= 8.0:
        return "low"
    if value <= 12.0:
        return "normal"
    return "high"


def _bps(pnl: float, notional: float) -> float:
    return pnl / notional * 10_000.0 if notional > 0.0 else 0.0


def _average(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0
