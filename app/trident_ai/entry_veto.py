from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.trident_ai.candidate_scan import CANDIDATE_HINT_FIELD
from app.trident_ai.config import TridentAIConfig, load_trident_ai_config
from app.trident_ai.paper import (
    PAPER_REPLAY_TRADE_CLOSED_EVENT,
    run_trident_ai_paper_replay,
)
from app.trident_ai.pattern_calibration import (
    _format_timestamp,
    _mapping,
    _number,
    _pattern_descriptor,
    _timestamp_id,
)
from app.trident_ai.replay import LLM_REPLAY_DECISION_EVENT


DEFAULT_ENTRY_VETO_MIN_DELTA_BPS = 0.0


@dataclass(frozen=True, slots=True)
class TridentAIEntryVetoReplayResult:
    decision_journal_paths: tuple[str, ...]
    market_input_paths: tuple[str, ...]
    baseline_paper_journal_paths: tuple[str, ...]
    fold_labels: tuple[str, ...]
    veto_buckets: tuple[str, ...]
    report_json_path: str
    report_md_path: str
    artifact_dir: str
    symbols_filter: tuple[str, ...] = ()
    min_delta_bps: float = DEFAULT_ENTRY_VETO_MIN_DELTA_BPS
    baseline_summary: dict[str, object] = field(default_factory=dict)
    veto_summary: dict[str, object] = field(default_factory=dict)
    delta_summary: dict[str, object] = field(default_factory=dict)
    fold_rows: list[dict[str, object]] = field(default_factory=list)
    verdict: str = "unknown"

    def to_dict(self) -> dict[str, object]:
        return {
            "decision_journal_paths": list(self.decision_journal_paths),
            "market_input_paths": list(self.market_input_paths),
            "baseline_paper_journal_paths": list(self.baseline_paper_journal_paths),
            "fold_labels": list(self.fold_labels),
            "veto_buckets": list(self.veto_buckets),
            "report_json_path": self.report_json_path,
            "report_md_path": self.report_md_path,
            "artifact_dir": self.artifact_dir,
            "symbols_filter": list(self.symbols_filter),
            "min_delta_bps": round(self.min_delta_bps, 6),
            "baseline_summary": self.baseline_summary,
            "veto_summary": self.veto_summary,
            "delta_summary": self.delta_summary,
            "fold_rows": self.fold_rows,
            "verdict": self.verdict,
        }


@dataclass(frozen=True, slots=True)
class TridentAIEntryVetoSweepResult:
    decision_journal_paths: tuple[str, ...]
    market_input_paths: tuple[str, ...]
    baseline_paper_journal_paths: tuple[str, ...]
    fold_labels: tuple[str, ...]
    veto_buckets: tuple[str, ...]
    report_json_path: str
    report_md_path: str
    artifact_dir: str
    symbols_filter: tuple[str, ...] = ()
    min_delta_bps: float = DEFAULT_ENTRY_VETO_MIN_DELTA_BPS
    rows: list[dict[str, object]] = field(default_factory=list)
    best_row: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "decision_journal_paths": list(self.decision_journal_paths),
            "market_input_paths": list(self.market_input_paths),
            "baseline_paper_journal_paths": list(self.baseline_paper_journal_paths),
            "fold_labels": list(self.fold_labels),
            "veto_buckets": list(self.veto_buckets),
            "report_json_path": self.report_json_path,
            "report_md_path": self.report_md_path,
            "artifact_dir": self.artifact_dir,
            "symbols_filter": list(self.symbols_filter),
            "min_delta_bps": round(self.min_delta_bps, 6),
            "rows": self.rows,
            "best_row": self.best_row,
        }


def run_trident_ai_entry_veto_replay(
    *,
    decision_journal_paths: Sequence[str | Path],
    market_input_paths: Sequence[str | Path],
    veto_buckets: Sequence[str],
    baseline_paper_journal_paths: Sequence[str | Path] | None = None,
    fold_labels: Sequence[str] | None = None,
    config: TridentAIConfig | None = None,
    report_json_path: str | Path | None = None,
    report_md_path: str | Path | None = None,
    artifact_dir: str | Path | None = None,
    symbols: Sequence[str] | None = None,
    min_delta_bps: float = DEFAULT_ENTRY_VETO_MIN_DELTA_BPS,
) -> TridentAIEntryVetoReplayResult:
    if not decision_journal_paths:
        raise ValueError("decision_journal_paths_required")
    if len(decision_journal_paths) != len(market_input_paths):
        raise ValueError("decision_and_market_input_counts_must_match")
    if baseline_paper_journal_paths is not None and len(baseline_paper_journal_paths) != len(decision_journal_paths):
        raise ValueError("baseline_paper_journal_count_must_match")
    if fold_labels is not None and len(fold_labels) != len(decision_journal_paths):
        raise ValueError("fold_label_count_must_match_input_count")
    parsed_vetoes = _parse_veto_buckets(veto_buckets)
    if not parsed_vetoes:
        raise ValueError("veto_buckets_required")

    active_config = config or load_trident_ai_config()
    run_id = _timestamp_id(datetime.now(timezone.utc))
    output_dir = Path(active_config.paths.replay_output_dir)
    json_output = Path(report_json_path or output_dir / f"trident_ai_entry_veto_replay_{run_id}.json")
    md_output = Path(report_md_path or output_dir / f"trident_ai_entry_veto_replay_{run_id}.md")
    artifacts = Path(artifact_dir or output_dir / f"{json_output.stem}_artifacts")
    labels = _fold_labels(fold_labels, len(decision_journal_paths))
    symbols_filter = _symbols_filter(symbols)
    baseline_paths = tuple(str(path) for path in baseline_paper_journal_paths or ())

    fold_rows: list[dict[str, object]] = []
    baseline_summaries: list[dict[str, object]] = []
    veto_summaries: list[dict[str, object]] = []
    for index, (label, decision_path, market_path) in enumerate(
        zip(labels, decision_journal_paths, market_input_paths, strict=True),
        start=1,
    ):
        filtered_path = artifacts / f"{index:02d}_{_safe_name(label)}_filtered_decisions.jsonl"
        veto_stats = _write_filtered_decisions(
            decision_path=decision_path,
            output_path=filtered_path,
            vetoes=parsed_vetoes,
            symbols_filter=symbols_filter,
        )
        paper_path = artifacts / f"{index:02d}_{_safe_name(label)}_paper.jsonl"
        paper_report_json = artifacts / f"{index:02d}_{_safe_name(label)}_paper.json"
        paper_report_md = artifacts / f"{index:02d}_{_safe_name(label)}_paper.md"
        paper_result = run_trident_ai_paper_replay(
            filtered_path,
            config=active_config,
            journal_path=paper_path,
            report_json_path=paper_report_json,
            report_md_path=paper_report_md,
            market_input_path=market_path,
            symbols=symbols_filter,
        )
        baseline_path = (
            Path(baseline_paper_journal_paths[index - 1])
            if baseline_paper_journal_paths is not None
            else None
        )
        baseline_summary = _paper_summary(baseline_path) if baseline_path is not None else _empty_paper_summary()
        veto_summary = _paper_summary(paper_path)
        baseline_summaries.append(baseline_summary)
        veto_summaries.append(veto_summary)
        fold_rows.append(
            {
                "fold_label": label,
                "decision_journal_path": str(decision_path),
                "filtered_decision_journal_path": str(filtered_path),
                "market_input_path": str(market_path),
                "baseline_paper_journal_path": str(baseline_path) if baseline_path is not None else "",
                "veto_paper_journal_path": str(paper_path),
                "decisions_seen": veto_stats["decisions_seen"],
                "decisions_written": veto_stats["decisions_written"],
                "decisions_vetoed": veto_stats["decisions_vetoed"],
                "veto_reasons": veto_stats["veto_reasons"],
                "baseline": baseline_summary,
                "veto": veto_summary,
                "delta": _delta_summary(veto_summary, baseline_summary),
                "paper_result": paper_result.to_dict(),
            }
        )

    baseline_total = _combine_summaries(baseline_summaries)
    veto_total = _combine_summaries(veto_summaries)
    delta_total = _delta_summary(veto_total, baseline_total)
    verdict = _verdict(delta_total=delta_total, fold_rows=fold_rows, min_delta_bps=min_delta_bps)
    result = TridentAIEntryVetoReplayResult(
        decision_journal_paths=tuple(str(path) for path in decision_journal_paths),
        market_input_paths=tuple(str(path) for path in market_input_paths),
        baseline_paper_journal_paths=baseline_paths,
        fold_labels=labels,
        veto_buckets=tuple(veto_buckets),
        report_json_path=str(json_output),
        report_md_path=str(md_output),
        artifact_dir=str(artifacts),
        symbols_filter=symbols_filter,
        min_delta_bps=float(min_delta_bps),
        baseline_summary=baseline_total,
        veto_summary=veto_total,
        delta_summary=delta_total,
        fold_rows=fold_rows,
        verdict=verdict,
    )
    payload = build_entry_veto_replay_report_payload(
        result=result,
        generated_at=_format_timestamp(datetime.now(timezone.utc)),
    )
    _write_report_outputs(payload, json_path=json_output, md_path=md_output)
    return result


def run_trident_ai_entry_veto_sweep(
    *,
    decision_journal_paths: Sequence[str | Path],
    market_input_paths: Sequence[str | Path],
    veto_buckets: Sequence[str],
    baseline_paper_journal_paths: Sequence[str | Path] | None = None,
    fold_labels: Sequence[str] | None = None,
    config: TridentAIConfig | None = None,
    report_json_path: str | Path | None = None,
    report_md_path: str | Path | None = None,
    artifact_dir: str | Path | None = None,
    symbols: Sequence[str] | None = None,
    min_delta_bps: float = DEFAULT_ENTRY_VETO_MIN_DELTA_BPS,
) -> TridentAIEntryVetoSweepResult:
    parsed_vetoes = _parse_veto_buckets(veto_buckets)
    if not parsed_vetoes:
        raise ValueError("veto_buckets_required")

    active_config = config or load_trident_ai_config()
    run_id = _timestamp_id(datetime.now(timezone.utc))
    output_dir = Path(active_config.paths.replay_output_dir)
    json_output = Path(report_json_path or output_dir / f"trident_ai_entry_veto_sweep_{run_id}.json")
    md_output = Path(report_md_path or output_dir / f"trident_ai_entry_veto_sweep_{run_id}.md")
    artifacts = Path(artifact_dir or output_dir / f"{json_output.stem}_artifacts")
    labels = _fold_labels(fold_labels, len(decision_journal_paths))
    symbols_filter = _symbols_filter(symbols)
    baseline_paths = tuple(str(path) for path in baseline_paper_journal_paths or ())

    rows: list[dict[str, object]] = []
    for index, (family, bucket) in enumerate(parsed_vetoes, start=1):
        veto = f"{family}::{bucket}"
        safe = f"{index:02d}_{_safe_name(family)}_{_safe_name(bucket)[:120]}"
        replay_result = run_trident_ai_entry_veto_replay(
            decision_journal_paths=decision_journal_paths,
            market_input_paths=market_input_paths,
            baseline_paper_journal_paths=baseline_paper_journal_paths,
            fold_labels=labels,
            config=active_config,
            veto_buckets=(veto,),
            report_json_path=artifacts / f"{safe}.json",
            report_md_path=artifacts / f"{safe}.md",
            artifact_dir=artifacts / safe,
            symbols=symbols_filter,
            min_delta_bps=min_delta_bps,
        )
        rows.append(_sweep_row(replay_result))

    rows.sort(key=_sweep_sort_key)
    result = TridentAIEntryVetoSweepResult(
        decision_journal_paths=tuple(str(path) for path in decision_journal_paths),
        market_input_paths=tuple(str(path) for path in market_input_paths),
        baseline_paper_journal_paths=baseline_paths,
        fold_labels=labels,
        veto_buckets=tuple(veto_buckets),
        report_json_path=str(json_output),
        report_md_path=str(md_output),
        artifact_dir=str(artifacts),
        symbols_filter=symbols_filter,
        min_delta_bps=float(min_delta_bps),
        rows=rows,
        best_row=rows[0] if rows else {},
    )
    payload = build_entry_veto_sweep_report_payload(
        result=result,
        generated_at=_format_timestamp(datetime.now(timezone.utc)),
    )
    _write_sweep_report_outputs(payload, json_path=json_output, md_path=md_output)
    return result


def build_entry_veto_replay_report_payload(
    *,
    result: TridentAIEntryVetoReplayResult,
    generated_at: str,
) -> dict[str, object]:
    return {
        "generated_at": generated_at,
        "kind": "trident_ai_entry_veto_replay",
        "result": result.to_dict(),
    }


def build_entry_veto_sweep_report_payload(
    *,
    result: TridentAIEntryVetoSweepResult,
    generated_at: str,
) -> dict[str, object]:
    return {
        "generated_at": generated_at,
        "kind": "trident_ai_entry_veto_sweep",
        "result": result.to_dict(),
    }


def _sweep_row(result: TridentAIEntryVetoReplayResult) -> dict[str, object]:
    delta = _mapping(result.delta_summary)
    vetoed = sum(int(_number(row.get("decisions_vetoed"))) for row in result.fold_rows)
    worse_folds = sum(
        1
        for row in result.fold_rows
        if _number(_mapping(row.get("delta")).get("pnl_usd")) < 0.0
    )
    improved_folds = sum(
        1
        for row in result.fold_rows
        if _number(_mapping(row.get("delta")).get("pnl_usd")) > 0.0
    )
    neutral_folds = len(result.fold_rows) - worse_folds - improved_folds
    return {
        "veto_bucket": result.veto_buckets[0] if result.veto_buckets else "",
        "verdict": result.verdict,
        "decisions_vetoed": vetoed,
        "baseline_closed_trades": int(_number(result.baseline_summary.get("closed_trades"))),
        "veto_closed_trades": int(_number(result.veto_summary.get("closed_trades"))),
        "delta_closed_trades": int(_number(delta.get("closed_trades"))),
        "baseline_pnl_usd": round(_number(result.baseline_summary.get("pnl_usd")), 6),
        "veto_pnl_usd": round(_number(result.veto_summary.get("pnl_usd")), 6),
        "delta_pnl_usd": round(_number(delta.get("pnl_usd")), 6),
        "baseline_avg_net_bps": round(_number(result.baseline_summary.get("avg_net_bps")), 6),
        "veto_avg_net_bps": round(_number(result.veto_summary.get("avg_net_bps")), 6),
        "delta_avg_net_bps": round(_number(delta.get("avg_net_bps")), 6),
        "worse_folds": worse_folds,
        "improved_folds": improved_folds,
        "neutral_folds": neutral_folds,
        "report_json_path": result.report_json_path,
        "report_md_path": result.report_md_path,
        "artifact_dir": result.artifact_dir,
    }


def _sweep_sort_key(row: Mapping[str, object]) -> tuple[object, ...]:
    return (
        row.get("verdict") != "promising_no_worse_folds",
        row.get("verdict") != "research_only_improves_total_but_worsens_fold",
        -_number(row.get("delta_pnl_usd")),
        int(_number(row.get("worse_folds"))),
        -int(_number(row.get("decisions_vetoed"))),
        str(row.get("veto_bucket", "")),
    )


def _write_filtered_decisions(
    *,
    decision_path: str | Path,
    output_path: Path,
    vetoes: tuple[tuple[str, str], ...],
    symbols_filter: tuple[str, ...],
) -> dict[str, object]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows = _iter_jsonl(decision_path)
    allowed = set(symbols_filter)
    kept: list[dict[str, object]] = []
    veto_reasons: Counter[str] = Counter()
    decisions_seen = 0
    decisions_vetoed = 0
    for row in rows:
        if row.get("event_type") != LLM_REPLAY_DECISION_EVENT:
            kept.append(row)
            continue
        proposal = _mapping(row.get("proposal"))
        if str(proposal.get("action", "") or "").lower() != "open":
            kept.append(row)
            continue
        symbol = str(row.get("symbol", "") or proposal.get("symbol", "") or "").upper()
        if allowed and symbol not in allowed:
            kept.append(row)
            continue
        decisions_seen += 1
        keys = _entry_bucket_keys(row)
        matched = [f"{family}::{bucket}" for family, bucket in vetoes if (family, bucket) in keys]
        if matched:
            decisions_vetoed += 1
            for item in matched:
                veto_reasons[item] += 1
            continue
        kept.append(row)
    output_path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in kept) + ("\n" if kept else ""),
        encoding="utf-8",
    )
    return {
        "decisions_seen": decisions_seen,
        "decisions_written": sum(
            1
            for row in kept
            if row.get("event_type") == LLM_REPLAY_DECISION_EVENT
            and str(_mapping(row.get("proposal")).get("action", "") or "").lower() == "open"
        ),
        "decisions_vetoed": decisions_vetoed,
        "veto_reasons": dict(sorted(veto_reasons.items())),
    }


def _entry_bucket_keys(row: Mapping[str, object]) -> set[tuple[str, str]]:
    proposal = _mapping(row.get("proposal"))
    context = _mapping(row.get("context"))
    descriptor = _pattern_descriptor(context=context, proposal=proposal)
    features = _mapping(context.get("features"))
    hint = _mapping(context.get(CANDIDATE_HINT_FIELD))
    symbol = str(row.get("symbol", "") or proposal.get("symbol", "") or "").upper()
    side = descriptor.side
    microstructure = (
        f"microprice={descriptor.microprice}|"
        f"flow_book={descriptor.flow_book}|"
        f"vwap={descriptor.vwap}"
    )
    edge_liquidity = (
        f"edge={descriptor.edge_bucket}|"
        f"net_edge={descriptor.net_edge_bucket}|"
        f"liquidity={_liquidity_bucket(_number(hint.get('liquidity_score')))}|"
        f"cost={_cost_bucket(_number(hint.get('round_trip_cost_bps')))}"
    )
    return {
        ("symbol", symbol),
        ("side", f"side={side}"),
        ("pattern", descriptor.pattern_key),
        ("side_pattern", f"side={side}|{descriptor.pattern_key}"),
        ("pattern_regime", f"{descriptor.pattern_key}|regime={descriptor.regime}"),
        ("microstructure", microstructure),
        ("edge_liquidity", edge_liquidity),
        ("cluster_pattern", f"cluster={features.get('market_cluster', 'unknown')}|{descriptor.pattern_key}"),
    }


def _paper_summary(path: str | Path) -> dict[str, object]:
    trades = _closed_trades(path)
    notional = sum(_number(trade.get("notional_usd")) for trade in trades)
    pnl = sum(_number(trade.get("pnl_usd")) for trade in trades)
    gross = sum(_number(trade.get("gross_pnl_usd")) for trade in trades)
    fees = sum(_number(trade.get("fees_usd")) for trade in trades)
    wins = sum(1 for trade in trades if _number(trade.get("pnl_usd")) > 0.0)
    return {
        "closed_trades": len(trades),
        "wins": wins,
        "losses": len(trades) - wins,
        "win_rate": round(wins / len(trades), 6) if trades else 0.0,
        "pnl_usd": round(pnl, 6),
        "gross_pnl_usd": round(gross, 6),
        "fees_usd": round(fees, 6),
        "notional_usd": round(notional, 6),
        "avg_net_bps": round(_bps(pnl, notional), 6),
        "symbols": dict(Counter(str(trade.get("symbol", "") or "unknown") for trade in trades)),
        "close_reasons": dict(Counter(str(trade.get("close_reason", "") or "unknown") for trade in trades)),
    }


def _empty_paper_summary() -> dict[str, object]:
    return {
        "closed_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "pnl_usd": 0.0,
        "gross_pnl_usd": 0.0,
        "fees_usd": 0.0,
        "notional_usd": 0.0,
        "avg_net_bps": 0.0,
        "symbols": {},
        "close_reasons": {},
    }


def _closed_trades(path: str | Path) -> list[dict[str, object]]:
    paper_path = Path(path)
    if not paper_path.exists():
        return []
    trades: list[dict[str, object]] = []
    for row in _iter_jsonl(paper_path):
        if row.get("event_type") != PAPER_REPLAY_TRADE_CLOSED_EVENT:
            continue
        trade = _mapping(row.get("trade"))
        if trade:
            trades.append(trade)
    return trades


def _combine_summaries(summaries: Sequence[Mapping[str, object]]) -> dict[str, object]:
    trades = sum(int(_number(item.get("closed_trades"))) for item in summaries)
    wins = sum(int(_number(item.get("wins"))) for item in summaries)
    pnl = sum(_number(item.get("pnl_usd")) for item in summaries)
    gross = sum(_number(item.get("gross_pnl_usd")) for item in summaries)
    fees = sum(_number(item.get("fees_usd")) for item in summaries)
    notional = sum(_number(item.get("notional_usd")) for item in summaries)
    symbols: Counter[str] = Counter()
    close_reasons: Counter[str] = Counter()
    for item in summaries:
        symbols.update({key: int(_number(value)) for key, value in _mapping(item.get("symbols")).items()})
        close_reasons.update({key: int(_number(value)) for key, value in _mapping(item.get("close_reasons")).items()})
    return {
        "closed_trades": trades,
        "wins": wins,
        "losses": trades - wins,
        "win_rate": round(wins / trades, 6) if trades else 0.0,
        "pnl_usd": round(pnl, 6),
        "gross_pnl_usd": round(gross, 6),
        "fees_usd": round(fees, 6),
        "notional_usd": round(notional, 6),
        "avg_net_bps": round(_bps(pnl, notional), 6),
        "symbols": dict(sorted(symbols.items())),
        "close_reasons": dict(sorted(close_reasons.items())),
    }


def _delta_summary(veto: Mapping[str, object], baseline: Mapping[str, object]) -> dict[str, object]:
    return {
        "closed_trades": int(_number(veto.get("closed_trades"))) - int(_number(baseline.get("closed_trades"))),
        "wins": int(_number(veto.get("wins"))) - int(_number(baseline.get("wins"))),
        "losses": int(_number(veto.get("losses"))) - int(_number(baseline.get("losses"))),
        "pnl_usd": round(_number(veto.get("pnl_usd")) - _number(baseline.get("pnl_usd")), 6),
        "avg_net_bps": round(_number(veto.get("avg_net_bps")) - _number(baseline.get("avg_net_bps")), 6),
    }


def _verdict(
    *,
    delta_total: Mapping[str, object],
    fold_rows: Sequence[Mapping[str, object]],
    min_delta_bps: float,
) -> str:
    worse_folds = sum(1 for row in fold_rows if _number(_mapping(row.get("delta")).get("pnl_usd")) < 0.0)
    if _number(delta_total.get("pnl_usd")) <= 0.0:
        return "rejected_delta_pnl_non_positive"
    if _number(delta_total.get("avg_net_bps")) < min_delta_bps:
        return "rejected_delta_bps_below_min"
    if worse_folds > 0:
        return "research_only_improves_total_but_worsens_fold"
    return "promising_no_worse_folds"


def _parse_veto_buckets(values: Sequence[str]) -> tuple[tuple[str, str], ...]:
    parsed: list[tuple[str, str]] = []
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        if "::" not in text:
            raise ValueError("veto_bucket_must_use_family_double_colon_bucket")
        family, bucket = text.split("::", 1)
        family = family.strip()
        bucket = bucket.strip()
        if family and bucket:
            parsed.append((family, bucket))
    return tuple(parsed)


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


def _write_sweep_report_outputs(
    payload: dict[str, object],
    *,
    json_path: Path,
    md_path: Path,
) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(_render_sweep_markdown_report(payload), encoding="utf-8")


def _render_markdown_report(payload: Mapping[str, object]) -> str:
    result = _mapping(payload.get("result"))
    baseline = _mapping(result.get("baseline_summary"))
    veto = _mapping(result.get("veto_summary"))
    delta = _mapping(result.get("delta_summary"))
    lines = [
        "# TRIDENT-AI Entry Veto Replay",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Veto buckets: `{result.get('veto_buckets', [])}`",
        f"- Verdict: `{result.get('verdict', '')}`",
        f"- Artifact dir: `{result.get('artifact_dir', '')}`",
        "",
        "## Summary",
        "",
        "| Case | Trades | PnL | Avg net | Win rate |",
        "|---|---:|---:|---:|---:|",
        _summary_row("Baseline", baseline),
        _summary_row("Veto", veto),
        _delta_row(delta),
        "",
        "## Folds",
        "",
        "| Fold | Vetoed | Baseline PnL | Veto PnL | Delta | Baseline bps | Veto bps |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for fold in result.get("fold_rows", []):
        if isinstance(fold, Mapping):
            lines.append(_fold_row(fold))
    if not result.get("fold_rows"):
        lines.append("| none | 0 | `$0.000000` | `$0.000000` | `$0.000000` | `0.00` | `0.00` |")
    lines.append("")
    return "\n".join(lines)


def _render_sweep_markdown_report(payload: Mapping[str, object]) -> str:
    result = _mapping(payload.get("result"))
    lines = [
        "# TRIDENT-AI Entry Veto Sweep",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Buckets tested: `{len(result.get('rows', []))}`",
        f"- Artifact dir: `{result.get('artifact_dir', '')}`",
        "",
        "## Results",
        "",
        "| Veto bucket | Verdict | Vetoed | Delta PnL | Delta bps | Worse folds | Improved folds |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in result.get("rows", []):
        if isinstance(row, Mapping):
            lines.append(_sweep_markdown_row(row))
    if not result.get("rows"):
        lines.append("| none | n/a | 0 | `$0.000000` | `0.00` | 0 | 0 |")
    lines.append("")
    return "\n".join(lines)


def _sweep_markdown_row(row: Mapping[str, object]) -> str:
    return (
        f"| `{row.get('veto_bucket', '')}` | `{row.get('verdict', '')}` | "
        f"{int(_number(row.get('decisions_vetoed')))} | "
        f"`${_number(row.get('delta_pnl_usd')):.6f}` | "
        f"`{_number(row.get('delta_avg_net_bps')):.2f}` | "
        f"{int(_number(row.get('worse_folds')))} | "
        f"{int(_number(row.get('improved_folds')))} |"
    )


def _summary_row(label: str, row: Mapping[str, object]) -> str:
    return (
        f"| {label} | {int(_number(row.get('closed_trades')))} | "
        f"`${_number(row.get('pnl_usd')):.6f}` | `{_number(row.get('avg_net_bps')):.2f}` | "
        f"{_number(row.get('win_rate')):.2%} |"
    )


def _delta_row(row: Mapping[str, object]) -> str:
    return (
        f"| Delta | {int(_number(row.get('closed_trades')))} | "
        f"`${_number(row.get('pnl_usd')):.6f}` | `{_number(row.get('avg_net_bps')):.2f}` | n/a |"
    )


def _fold_row(row: Mapping[str, object]) -> str:
    baseline = _mapping(row.get("baseline"))
    veto = _mapping(row.get("veto"))
    delta = _mapping(row.get("delta"))
    return (
        f"| `{row.get('fold_label', '')}` | {int(_number(row.get('decisions_vetoed')))} | "
        f"`${_number(baseline.get('pnl_usd')):.6f}` | `${_number(veto.get('pnl_usd')):.6f}` | "
        f"`${_number(delta.get('pnl_usd')):.6f}` | `{_number(baseline.get('avg_net_bps')):.2f}` | "
        f"`{_number(veto.get('avg_net_bps')):.2f}` |"
    )


def _iter_jsonl(path: str | Path) -> list[dict[str, object]]:
    file_path = Path(path)
    if not file_path.exists():
        return []
    rows: list[dict[str, object]] = []
    for line in file_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


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


def _liquidity_bucket(value: float) -> str:
    if value >= 1.2:
        return "high"
    if value >= 1.0:
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


def _safe_name(value: str) -> str:
    normalized = []
    for char in value.lower():
        normalized.append(char if char.isalnum() else "_")
    return "".join(normalized).strip("_") or "fold"
