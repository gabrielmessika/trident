from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.trident_ai.candidate_scan import CANDIDATE_HINT_FIELD
from app.trident_ai.config import TridentAIConfig, load_trident_ai_config
from app.trident_ai.pattern_calibration import (
    _closed_trades_by_decision,
    _decision_rows,
    _format_timestamp,
    _mapping,
    _number,
    _paper_decisions_by_decision,
    _pattern_descriptor,
    _pnl_bps,
    _string_list,
    _timestamp_id,
)


DEFAULT_PATTERN_SUPPORT_MIN_CLOSED_TRADES = 4
DEFAULT_PATTERN_SUPPORT_MIN_FOLDS = 2
DEFAULT_PATTERN_SUPPORT_MIN_POSITIVE_FOLDS = 2
DEFAULT_PATTERN_SUPPORT_MIN_SYMBOLS = 2
DEFAULT_PATTERN_SUPPORT_MAX_NEGATIVE_FOLDS = 0
DEFAULT_PATTERN_SUPPORT_MAX_DOMINANT_SYMBOL_RATIO = 0.70
DEFAULT_PATTERN_SUPPORT_MAX_CATASTROPHIC_NET_BPS = 50.0


@dataclass(frozen=True, slots=True)
class TridentAIPatternSupportAuditResult:
    decision_journal_paths: tuple[str, ...]
    paper_journal_paths: tuple[str, ...]
    fold_labels: tuple[str, ...]
    report_json_path: str
    report_md_path: str
    symbols_filter: tuple[str, ...] = ()
    min_closed_trades: int = DEFAULT_PATTERN_SUPPORT_MIN_CLOSED_TRADES
    min_folds: int = DEFAULT_PATTERN_SUPPORT_MIN_FOLDS
    min_positive_folds: int = DEFAULT_PATTERN_SUPPORT_MIN_POSITIVE_FOLDS
    min_symbols: int = DEFAULT_PATTERN_SUPPORT_MIN_SYMBOLS
    max_negative_folds: int = DEFAULT_PATTERN_SUPPORT_MAX_NEGATIVE_FOLDS
    max_dominant_symbol_ratio: float = DEFAULT_PATTERN_SUPPORT_MAX_DOMINANT_SYMBOL_RATIO
    max_catastrophic_net_bps: float = DEFAULT_PATTERN_SUPPORT_MAX_CATASTROPHIC_NET_BPS
    summary: dict[str, object] = field(default_factory=dict)
    folds: list[dict[str, object]] = field(default_factory=list)
    symbol_diagnostics: list[dict[str, object]] = field(default_factory=list)
    bucket_rows: list[dict[str, object]] = field(default_factory=list)
    symbol_agnostic_positive_buckets: list[dict[str, object]] = field(default_factory=list)
    symbol_concentrated_positive_buckets: list[dict[str, object]] = field(default_factory=list)
    unstable_buckets: list[dict[str, object]] = field(default_factory=list)
    negative_buckets: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "decision_journal_paths": list(self.decision_journal_paths),
            "paper_journal_paths": list(self.paper_journal_paths),
            "fold_labels": list(self.fold_labels),
            "report_json_path": self.report_json_path,
            "report_md_path": self.report_md_path,
            "symbols_filter": list(self.symbols_filter),
            "min_closed_trades": self.min_closed_trades,
            "min_folds": self.min_folds,
            "min_positive_folds": self.min_positive_folds,
            "min_symbols": self.min_symbols,
            "max_negative_folds": self.max_negative_folds,
            "max_dominant_symbol_ratio": round(self.max_dominant_symbol_ratio, 6),
            "max_catastrophic_net_bps": round(self.max_catastrophic_net_bps, 6),
            "summary": self.summary,
            "folds": self.folds,
            "symbol_diagnostics": self.symbol_diagnostics,
            "bucket_rows": self.bucket_rows,
            "symbol_agnostic_positive_buckets": self.symbol_agnostic_positive_buckets,
            "symbol_concentrated_positive_buckets": self.symbol_concentrated_positive_buckets,
            "unstable_buckets": self.unstable_buckets,
            "negative_buckets": self.negative_buckets,
        }


@dataclass(frozen=True, slots=True)
class _SupportObservation:
    fold_label: str
    decision_id: str
    timestamp: str
    symbol: str
    side: str
    pattern_key: str
    regime: str
    microprice: str
    flow_book: str
    vwap: str
    edge_bucket: str
    net_edge_bucket: str
    volatility_bucket: str
    liquidity_bucket: str
    activity_bucket: str
    cost_bucket: str
    market_cluster: str
    pattern_reasons: tuple[str, ...]
    paper_action: str = ""
    paper_reason: str = ""
    close_reason: str = ""
    pnl_usd: float = 0.0
    gross_pnl_usd: float = 0.0
    fees_usd: float = 0.0
    notional_usd: float = 0.0


def run_trident_ai_pattern_support_audit(
    *,
    decision_journal_paths: Sequence[str | Path],
    paper_journal_paths: Sequence[str | Path],
    fold_labels: Sequence[str] | None = None,
    config: TridentAIConfig | None = None,
    report_json_path: str | Path | None = None,
    report_md_path: str | Path | None = None,
    symbols: Sequence[str] | None = None,
    min_closed_trades: int = DEFAULT_PATTERN_SUPPORT_MIN_CLOSED_TRADES,
    min_folds: int = DEFAULT_PATTERN_SUPPORT_MIN_FOLDS,
    min_positive_folds: int = DEFAULT_PATTERN_SUPPORT_MIN_POSITIVE_FOLDS,
    min_symbols: int = DEFAULT_PATTERN_SUPPORT_MIN_SYMBOLS,
    max_negative_folds: int = DEFAULT_PATTERN_SUPPORT_MAX_NEGATIVE_FOLDS,
    max_dominant_symbol_ratio: float = DEFAULT_PATTERN_SUPPORT_MAX_DOMINANT_SYMBOL_RATIO,
    max_catastrophic_net_bps: float = DEFAULT_PATTERN_SUPPORT_MAX_CATASTROPHIC_NET_BPS,
) -> TridentAIPatternSupportAuditResult:
    if not decision_journal_paths:
        raise ValueError("decision_journal_paths_required")
    if len(decision_journal_paths) != len(paper_journal_paths):
        raise ValueError("decision_and_paper_journal_counts_must_match")
    if fold_labels is not None and len(fold_labels) != len(decision_journal_paths):
        raise ValueError("fold_label_count_must_match_journal_count")
    if min_closed_trades <= 0:
        raise ValueError("min_closed_trades_must_be_positive")
    if min_folds <= 0:
        raise ValueError("min_folds_must_be_positive")
    if min_positive_folds <= 0:
        raise ValueError("min_positive_folds_must_be_positive")
    if min_symbols <= 0:
        raise ValueError("min_symbols_must_be_positive")
    if max_negative_folds < 0:
        raise ValueError("max_negative_folds_must_be_non_negative")
    if not 0.0 < max_dominant_symbol_ratio <= 1.0:
        raise ValueError("max_dominant_symbol_ratio_must_be_between_zero_and_one")
    if max_catastrophic_net_bps <= 0.0:
        raise ValueError("max_catastrophic_net_bps_must_be_positive")

    resolved_config = config or load_trident_ai_config()
    run_id = _timestamp_id(datetime.now(timezone.utc))
    output_dir = Path(resolved_config.paths.replay_output_dir)
    json_output = Path(report_json_path or output_dir / f"trident_ai_pattern_support_audit_{run_id}.json")
    md_output = Path(report_md_path or output_dir / f"trident_ai_pattern_support_audit_{run_id}.md")
    labels = _fold_labels(fold_labels, len(decision_journal_paths))
    symbols_filter = _symbols_filter(symbols)

    observations: list[_SupportObservation] = []
    decisions_seen = 0
    for label, decision_path, paper_path in zip(labels, decision_journal_paths, paper_journal_paths, strict=True):
        fold_decisions_seen, fold_observations = _support_observations_from_journals(
            decision_path=decision_path,
            paper_path=paper_path,
            fold_label=label,
            symbols_filter=symbols_filter,
        )
        decisions_seen += fold_decisions_seen
        observations.extend(fold_observations)

    bucket_observations: dict[tuple[str, str], list[_SupportObservation]] = defaultdict(list)
    for observation in observations:
        for family, bucket in _bucket_keys(observation):
            bucket_observations[(family, bucket)].append(observation)

    bucket_rows = [
        _bucket_row(
            family=family,
            bucket=bucket,
            observations=rows,
            fold_labels=labels,
            min_closed_trades=min_closed_trades,
            min_folds=min_folds,
            min_positive_folds=min_positive_folds,
            min_symbols=min_symbols,
            max_negative_folds=max_negative_folds,
            max_dominant_symbol_ratio=max_dominant_symbol_ratio,
            max_catastrophic_net_bps=max_catastrophic_net_bps,
        )
        for (family, bucket), rows in bucket_observations.items()
    ]
    bucket_rows.sort(key=_bucket_sort_key)
    symbol_agnostic = [row for row in bucket_rows if row["classification"] == "symbol_agnostic_positive"]
    concentrated = [row for row in bucket_rows if row["classification"] == "symbol_concentrated_positive"]
    unstable = [row for row in bucket_rows if row["classification"] == "fold_unstable"]
    negative = [row for row in bucket_rows if row["classification"] == "negative_or_flat"]

    result = TridentAIPatternSupportAuditResult(
        decision_journal_paths=tuple(str(path) for path in decision_journal_paths),
        paper_journal_paths=tuple(str(path) for path in paper_journal_paths),
        fold_labels=labels,
        report_json_path=str(json_output),
        report_md_path=str(md_output),
        symbols_filter=symbols_filter,
        min_closed_trades=min_closed_trades,
        min_folds=min_folds,
        min_positive_folds=min_positive_folds,
        min_symbols=min_symbols,
        max_negative_folds=max_negative_folds,
        max_dominant_symbol_ratio=max_dominant_symbol_ratio,
        max_catastrophic_net_bps=max_catastrophic_net_bps,
        summary=_summary_row(observations, decisions_seen=decisions_seen),
        folds=[
            _group_row(label, [item for item in observations if item.fold_label == label], key_name="fold")
            for label in labels
        ],
        symbol_diagnostics=sorted(
            (
                _group_row(symbol, [item for item in observations if item.symbol == symbol], key_name="symbol")
                for symbol in sorted({item.symbol for item in observations})
            ),
            key=lambda item: (-int(_number(item.get("closed_trades"))), _number(item.get("pnl_usd")), item.get("symbol", "")),
        ),
        bucket_rows=bucket_rows[:250],
        symbol_agnostic_positive_buckets=symbol_agnostic[:40],
        symbol_concentrated_positive_buckets=concentrated[:40],
        unstable_buckets=unstable[:40],
        negative_buckets=negative[:40],
    )
    payload = build_pattern_support_audit_report_payload(
        result=result,
        generated_at=_format_timestamp(datetime.now(timezone.utc)),
    )
    _write_report_outputs(payload, json_path=json_output, md_path=md_output)
    return result


def build_pattern_support_audit_report_payload(
    *,
    result: TridentAIPatternSupportAuditResult,
    generated_at: str,
) -> dict[str, object]:
    return {
        "generated_at": generated_at,
        "kind": "trident_ai_pattern_support_audit",
        "result": result.to_dict(),
    }


def _support_observations_from_journals(
    *,
    decision_path: str | Path,
    paper_path: str | Path,
    fold_label: str,
    symbols_filter: tuple[str, ...],
) -> tuple[int, list[_SupportObservation]]:
    rows = _decision_rows(decision_path)
    if not rows:
        return 0, []
    paper_decisions = _paper_decisions_by_decision(paper_path)
    closed_trades = _closed_trades_by_decision(paper_path)
    allowed = set(symbols_filter)
    observations: list[_SupportObservation] = []
    for row in rows:
        proposal = _mapping(row.get("proposal"))
        if str(proposal.get("action", "") or "").lower() != "open":
            continue
        decision_id = str(proposal.get("decision_id", "") or "")
        if not decision_id:
            continue
        symbol = str(row.get("symbol", "") or proposal.get("symbol", "") or "").upper()
        if allowed and symbol not in allowed:
            continue
        context = _mapping(row.get("context"))
        observations.append(
            _support_observation(
                fold_label=fold_label,
                row=row,
                context=context,
                proposal=proposal,
                paper_decision=paper_decisions.get(decision_id),
                trade=closed_trades.get(decision_id),
            )
        )
    return len(rows), observations


def _support_observation(
    *,
    fold_label: str,
    row: Mapping[str, object],
    context: Mapping[str, object],
    proposal: Mapping[str, object],
    paper_decision: Mapping[str, object] | None,
    trade: Mapping[str, object] | None,
) -> _SupportObservation:
    descriptor = _pattern_descriptor(context=context, proposal=proposal)
    features = _mapping(context.get("features"))
    hint = _mapping(context.get(CANDIDATE_HINT_FIELD))
    trade_payload = _mapping(trade)
    paper_payload = _mapping(paper_decision)
    return _SupportObservation(
        fold_label=fold_label,
        decision_id=str(proposal.get("decision_id", "") or ""),
        timestamp=str(row.get("timestamp", "") or ""),
        symbol=str(row.get("symbol", "") or proposal.get("symbol", "") or "").upper(),
        side=descriptor.side,
        pattern_key=descriptor.pattern_key,
        regime=descriptor.regime,
        microprice=descriptor.microprice,
        flow_book=descriptor.flow_book,
        vwap=descriptor.vwap,
        edge_bucket=descriptor.edge_bucket,
        net_edge_bucket=descriptor.net_edge_bucket,
        volatility_bucket=descriptor.volatility_bucket,
        liquidity_bucket=_liquidity_bucket(_number(hint.get("liquidity_score"))),
        activity_bucket=_activity_bucket(_number(hint.get("activity_score"))),
        cost_bucket=_cost_bucket(_number(hint.get("round_trip_cost_bps"))),
        market_cluster=str(features.get("market_cluster", "") or "unknown"),
        pattern_reasons=tuple(_string_list(hint.get("pattern_reasons"))),
        paper_action=str(paper_payload.get("paper_action", "") or ""),
        paper_reason=str(paper_payload.get("reason", "") or ""),
        close_reason=str(trade_payload.get("close_reason", "") or ""),
        pnl_usd=_number(trade_payload.get("pnl_usd")),
        gross_pnl_usd=_number(trade_payload.get("gross_pnl_usd")),
        fees_usd=_number(trade_payload.get("fees_usd")),
        notional_usd=_number(trade_payload.get("notional_usd")),
    )


def _bucket_keys(observation: _SupportObservation) -> tuple[tuple[str, str], ...]:
    microstructure = (
        f"microprice={observation.microprice}|"
        f"flow_book={observation.flow_book}|"
        f"vwap={observation.vwap}"
    )
    edge_liquidity = (
        f"edge={observation.edge_bucket}|"
        f"net_edge={observation.net_edge_bucket}|"
        f"liquidity={observation.liquidity_bucket}|"
        f"cost={observation.cost_bucket}"
    )
    return (
        ("pattern", observation.pattern_key),
        ("side_pattern", f"side={observation.side}|{observation.pattern_key}"),
        ("pattern_regime", f"{observation.pattern_key}|regime={observation.regime}"),
        ("microstructure", microstructure),
        ("edge_liquidity", edge_liquidity),
        ("cluster_pattern", f"cluster={observation.market_cluster}|{observation.pattern_key}"),
    )


def _bucket_row(
    *,
    family: str,
    bucket: str,
    observations: Sequence[_SupportObservation],
    fold_labels: tuple[str, ...],
    min_closed_trades: int,
    min_folds: int,
    min_positive_folds: int,
    min_symbols: int,
    max_negative_folds: int,
    max_dominant_symbol_ratio: float,
    max_catastrophic_net_bps: float,
) -> dict[str, object]:
    row = _group_row(bucket, observations, key_name="bucket")
    fold_results = [
        _group_row(label, [item for item in observations if item.fold_label == label], key_name="fold")
        for label in fold_labels
    ]
    symbol_results = [
        _group_row(symbol, [item for item in observations if item.symbol == symbol], key_name="symbol")
        for symbol in sorted({item.symbol for item in observations})
    ]
    closed = [item for item in observations if item.close_reason]
    folds_with_closed = sum(1 for fold in fold_results if _number(fold.get("closed_trades")) > 0.0)
    positive_folds = sum(1 for fold in fold_results if _number(fold.get("closed_trades")) > 0.0 and _number(fold.get("pnl_usd")) > 0.0)
    negative_folds = sum(1 for fold in fold_results if _number(fold.get("closed_trades")) > 0.0 and _number(fold.get("pnl_usd")) < 0.0)
    catastrophic_folds = sum(
        1
        for fold in fold_results
        if _number(fold.get("closed_trades")) > 0.0
        and _number(fold.get("avg_net_bps")) <= -abs(max_catastrophic_net_bps)
    )
    symbols_with_closed = sum(1 for symbol in symbol_results if _number(symbol.get("closed_trades")) > 0.0)
    positive_symbols = sum(1 for symbol in symbol_results if _number(symbol.get("closed_trades")) > 0.0 and _number(symbol.get("pnl_usd")) > 0.0)
    negative_symbols = sum(1 for symbol in symbol_results if _number(symbol.get("closed_trades")) > 0.0 and _number(symbol.get("pnl_usd")) < 0.0)
    dominant_symbol, dominant_count = _dominant_symbol(closed)
    dominant_trade_ratio = dominant_count / len(closed) if closed else 0.0
    dominant_abs_symbol, dominant_abs_ratio = _dominant_abs_pnl_symbol(closed)
    row.update(
        {
            "bucket_family": family,
            "classification": _classification(
                row=row,
                folds_with_closed=folds_with_closed,
                positive_folds=positive_folds,
                negative_folds=negative_folds,
                catastrophic_folds=catastrophic_folds,
                symbols_with_closed=symbols_with_closed,
                positive_symbols=positive_symbols,
                min_closed_trades=min_closed_trades,
                min_folds=min_folds,
                min_positive_folds=min_positive_folds,
                min_symbols=min_symbols,
                max_negative_folds=max_negative_folds,
                max_dominant_symbol_ratio=max_dominant_symbol_ratio,
                dominant_symbol_trade_ratio=dominant_trade_ratio,
            ),
            "folds_with_closed": folds_with_closed,
            "positive_folds": positive_folds,
            "negative_folds": negative_folds,
            "catastrophic_folds": catastrophic_folds,
            "symbols_with_closed": symbols_with_closed,
            "positive_symbols": positive_symbols,
            "negative_symbols": negative_symbols,
            "dominant_symbol": dominant_symbol,
            "dominant_symbol_trade_ratio": round(dominant_trade_ratio, 6),
            "dominant_abs_pnl_symbol": dominant_abs_symbol,
            "dominant_abs_pnl_ratio": round(dominant_abs_ratio, 6),
            "fold_results": fold_results,
            "symbol_results": sorted(
                symbol_results,
                key=lambda item: (-int(_number(item.get("closed_trades"))), _number(item.get("pnl_usd")), item.get("symbol", "")),
            ),
        }
    )
    return row


def _classification(
    *,
    row: Mapping[str, object],
    folds_with_closed: int,
    positive_folds: int,
    negative_folds: int,
    catastrophic_folds: int,
    symbols_with_closed: int,
    positive_symbols: int,
    min_closed_trades: int,
    min_folds: int,
    min_positive_folds: int,
    min_symbols: int,
    max_negative_folds: int,
    max_dominant_symbol_ratio: float,
    dominant_symbol_trade_ratio: float,
) -> str:
    closed_trades = int(_number(row.get("closed_trades")))
    pnl = _number(row.get("pnl_usd"))
    if closed_trades < min_closed_trades or folds_with_closed < min_folds:
        return "insufficient_support"
    if catastrophic_folds > 0 or negative_folds > max_negative_folds:
        return "fold_unstable"
    if pnl <= 0.0:
        return "negative_or_flat"
    if symbols_with_closed < min_symbols or dominant_symbol_trade_ratio > max_dominant_symbol_ratio:
        return "symbol_concentrated_positive"
    if positive_folds < min_positive_folds or positive_symbols < min_symbols:
        return "insufficient_symbol_or_fold_quality"
    return "symbol_agnostic_positive"


def _group_row(
    name: str,
    observations: Sequence[_SupportObservation],
    *,
    key_name: str,
) -> dict[str, object]:
    closed = [item for item in observations if item.close_reason]
    pnl = sum(item.pnl_usd for item in closed)
    gross = sum(item.gross_pnl_usd for item in closed)
    fees = sum(item.fees_usd for item in closed)
    notional = sum(item.notional_usd for item in closed)
    close_reasons: Counter[str] = Counter(item.close_reason for item in closed)
    paper_reasons: Counter[str] = Counter(item.paper_reason for item in observations if item.paper_reason)
    pattern_reasons: Counter[str] = Counter(reason for item in observations for reason in item.pattern_reasons)
    symbols: Counter[str] = Counter(item.symbol for item in observations if item.symbol)
    return {
        key_name: name,
        "decisions": len(observations),
        "paper_opens": sum(1 for item in observations if item.paper_action == "open"),
        "paper_skips": sum(1 for item in observations if item.paper_action == "skip"),
        "closed_trades": len(closed),
        "wins": sum(1 for item in closed if item.pnl_usd > 0.0),
        "losses": sum(1 for item in closed if item.pnl_usd < 0.0),
        "win_rate": round(sum(1 for item in closed if item.pnl_usd > 0.0) / len(closed), 6) if closed else 0.0,
        "pnl_usd": round(pnl, 6),
        "gross_pnl_usd": round(gross, 6),
        "fees_usd": round(fees, 6),
        "avg_net_bps": _pnl_bps(pnl, notional),
        "close_reasons": dict(sorted(close_reasons.items())),
        "paper_reasons": dict(sorted(paper_reasons.items())),
        "pattern_reasons": dict(pattern_reasons.most_common(8)),
        "symbols": dict(sorted(symbols.items())),
    }


def _summary_row(observations: Sequence[_SupportObservation], *, decisions_seen: int) -> dict[str, object]:
    row = _group_row("all", observations, key_name="scope")
    row["decisions_seen"] = decisions_seen
    row["symbols_seen"] = sorted({item.symbol for item in observations})
    row["folds_seen"] = sorted({item.fold_label for item in observations})
    return row


def _dominant_symbol(observations: Sequence[_SupportObservation]) -> tuple[str, int]:
    counts: Counter[str] = Counter(item.symbol for item in observations if item.symbol)
    if not counts:
        return "", 0
    return counts.most_common(1)[0]


def _dominant_abs_pnl_symbol(observations: Sequence[_SupportObservation]) -> tuple[str, float]:
    totals: defaultdict[str, float] = defaultdict(float)
    for observation in observations:
        if observation.symbol:
            totals[observation.symbol] += abs(observation.pnl_usd)
    total_abs = sum(totals.values())
    if total_abs <= 0.0:
        return "", 0.0
    symbol, value = max(totals.items(), key=lambda item: item[1])
    return symbol, value / total_abs


def _bucket_sort_key(row: Mapping[str, object]) -> tuple[int, float, float, float, str, str]:
    classification_rank = {
        "symbol_agnostic_positive": 0,
        "fold_unstable": 1,
        "symbol_concentrated_positive": 2,
        "negative_or_flat": 3,
        "insufficient_symbol_or_fold_quality": 4,
        "insufficient_support": 5,
    }
    return (
        classification_rank.get(str(row.get("classification", "")), 9),
        -_number(row.get("closed_trades")),
        -_number(row.get("pnl_usd")),
        _number(row.get("dominant_symbol_trade_ratio")),
        str(row.get("bucket_family", "")),
        str(row.get("bucket", "")),
    )


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


def _cost_bucket(round_trip_cost_bps: float) -> str:
    if round_trip_cost_bps <= 0.0:
        return "missing"
    if round_trip_cost_bps <= 8.0:
        return "low"
    if round_trip_cost_bps <= 12.0:
        return "normal"
    return "high"


def _fold_labels(labels: Sequence[str] | None, count: int) -> tuple[str, ...]:
    if labels is None:
        return tuple(f"fold_{index + 1}" for index in range(count))
    return tuple(str(label or f"fold_{index + 1}") for index, label in enumerate(labels))


def _symbols_filter(symbols: Sequence[str] | None) -> tuple[str, ...]:
    if symbols is None:
        return ()
    return tuple(sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()}))


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
        "# TRIDENT-AI Pattern Support Audit",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Folds: `{result.get('fold_labels', [])}`",
        f"- Symbols filter: `{result.get('symbols_filter', [])}`",
        f"- Closed trades: `{summary.get('closed_trades', 0)}`",
        f"- Realized PnL: `${_number(summary.get('pnl_usd')):.6f}`",
        f"- Avg net: `{_number(summary.get('avg_net_bps')):.2f} bps`",
        f"- Symbol-agnostic positives: `{len(result.get('symbol_agnostic_positive_buckets', []))}`",
        "",
        "## Fold Summaries",
        "",
        "| Fold | Decisions | Opens | Trades | Win rate | PnL | Avg net |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    _append_group_rows(lines, result.get("folds"), key_name="fold")
    lines.extend(
        [
            "",
            "## Symbol-Agnostic Positive Buckets",
            "",
            "| Family | Bucket | Trades | Folds | Symbols | Win rate | PnL | Avg net | Dominant |",
            "|---|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    _append_bucket_rows(lines, result.get("symbol_agnostic_positive_buckets"))
    lines.extend(
        [
            "",
            "## Concentrated Positive Buckets",
            "",
            "| Family | Bucket | Trades | Folds | Symbols | Win rate | PnL | Avg net | Dominant |",
            "|---|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    _append_bucket_rows(lines, result.get("symbol_concentrated_positive_buckets"))
    lines.extend(
        [
            "",
            "## Unstable Buckets",
            "",
            "| Family | Bucket | Trades | Folds | Symbols | Win rate | PnL | Avg net | Dominant |",
            "|---|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    _append_bucket_rows(lines, result.get("unstable_buckets"))
    lines.extend(
        [
            "",
            "## Symbol Diagnostics",
            "",
            "Secondary diagnostic only: do not promote coin-specific rules from this table.",
            "",
            "| Symbol | Decisions | Opens | Trades | Win rate | PnL | Avg net |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    _append_group_rows(lines, result.get("symbol_diagnostics"), key_name="symbol")
    lines.append("")
    return "\n".join(lines)


def _append_bucket_rows(lines: list[str], rows: object) -> None:
    if not isinstance(rows, list) or not rows:
        lines.append("| none | none | 0 | 0 | 0 | 0.00% | $0.000000 | 0.00 | n/a |")
        return
    for item in rows[:30]:
        row = _mapping(item)
        dominant = str(row.get("dominant_symbol", "") or "n/a")
        dominant_ratio = _number(row.get("dominant_symbol_trade_ratio"))
        lines.append(
            f"| {row.get('bucket_family', '')} | {row.get('bucket', '')} | "
            f"{int(_number(row.get('closed_trades')))} | {int(_number(row.get('folds_with_closed')))} | "
            f"{int(_number(row.get('symbols_with_closed')))} | {_number(row.get('win_rate')):.2%} | "
            f"${_number(row.get('pnl_usd')):.6f} | {_number(row.get('avg_net_bps')):.2f} | "
            f"{dominant} {dominant_ratio:.2%} |"
        )


def _append_group_rows(lines: list[str], rows: object, *, key_name: str) -> None:
    if not isinstance(rows, list) or not rows:
        lines.append("| none | 0 | 0 | 0 | 0.00% | $0.000000 | 0.00 |")
        return
    for item in rows:
        row = _mapping(item)
        lines.append(
            f"| {row.get(key_name, '')} | {int(_number(row.get('decisions')))} | "
            f"{int(_number(row.get('paper_opens')))} | {int(_number(row.get('closed_trades')))} | "
            f"{_number(row.get('win_rate')):.2%} | ${_number(row.get('pnl_usd')):.6f} | "
            f"{_number(row.get('avg_net_bps')):.2f} |"
        )
