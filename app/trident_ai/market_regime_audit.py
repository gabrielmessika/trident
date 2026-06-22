from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.trident_ai.candidate_scan import CANDIDATE_HINT_FIELD
from app.trident_ai.config import TridentAIConfig, load_trident_ai_config
from app.trident_ai.market_regime import build_market_micro_regime
from app.trident_ai.pattern_calibration import _format_timestamp, _mapping, _number, _timestamp_id


DEFAULT_MARKET_REGIME_MIN_TRADES = 2
DEFAULT_MARKET_REGIME_MAX_DOMINANT_SYMBOL_RATIO = 0.75
PAPER_REPLAY_TRADE_CLOSED_EVENT = "trident_ai_paper_replay_trade_closed"


@dataclass(frozen=True, slots=True)
class TridentAIMarketRegimeAuditResult:
    gate_sweep_report_path: str
    profile_id: str
    report_json_path: str
    report_md_path: str
    min_trades: int = DEFAULT_MARKET_REGIME_MIN_TRADES
    max_dominant_symbol_ratio: float = DEFAULT_MARKET_REGIME_MAX_DOMINANT_SYMBOL_RATIO
    summary: dict[str, object] = field(default_factory=dict)
    fold_rows: list[dict[str, object]] = field(default_factory=list)
    bucket_rows: list[dict[str, object]] = field(default_factory=list)
    loss_regime_rows: list[dict[str, object]] = field(default_factory=list)
    support_regime_rows: list[dict[str, object]] = field(default_factory=list)
    symbol_regime_rows: list[dict[str, object]] = field(default_factory=list)
    worst_trades: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "gate_sweep_report_path": self.gate_sweep_report_path,
            "profile_id": self.profile_id,
            "report_json_path": self.report_json_path,
            "report_md_path": self.report_md_path,
            "min_trades": self.min_trades,
            "max_dominant_symbol_ratio": round(self.max_dominant_symbol_ratio, 6),
            "summary": self.summary,
            "fold_rows": self.fold_rows,
            "bucket_rows": self.bucket_rows,
            "loss_regime_rows": self.loss_regime_rows,
            "support_regime_rows": self.support_regime_rows,
            "symbol_regime_rows": self.symbol_regime_rows,
            "worst_trades": self.worst_trades,
        }


def run_trident_ai_market_regime_audit(
    *,
    gate_sweep_report_path: str | Path,
    profile_id: str | None = None,
    config: TridentAIConfig | None = None,
    report_json_path: str | Path | None = None,
    report_md_path: str | Path | None = None,
    min_trades: int = DEFAULT_MARKET_REGIME_MIN_TRADES,
    max_dominant_symbol_ratio: float = DEFAULT_MARKET_REGIME_MAX_DOMINANT_SYMBOL_RATIO,
) -> TridentAIMarketRegimeAuditResult:
    if min_trades <= 0:
        raise ValueError("min_trades_must_be_positive")
    if not 0.0 < max_dominant_symbol_ratio <= 1.0:
        raise ValueError("max_dominant_symbol_ratio_must_be_between_zero_and_one")

    active_config = config or load_trident_ai_config()
    run_id = _timestamp_id(datetime.now(timezone.utc))
    output_dir = Path(active_config.paths.replay_output_dir)
    json_output = Path(report_json_path or output_dir / f"trident_ai_market_regime_audit_{run_id}.json")
    md_output = Path(report_md_path or output_dir / f"trident_ai_market_regime_audit_{run_id}.md")

    source_path = Path(gate_sweep_report_path)
    profile = _select_gate_sweep_profile(source_path, profile_id=profile_id)
    selected_profile_id = str(profile.get("profile_id", "") or profile_id or "unknown")
    trades = _trades_from_profile(profile)
    buckets: defaultdict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for trade in trades:
        for family, bucket in _bucket_keys(trade):
            buckets[(family, bucket)].append(trade)

    bucket_rows = [
        _bucket_row(
            family=family,
            bucket=bucket,
            trades=rows,
            min_trades=min_trades,
            max_dominant_symbol_ratio=max_dominant_symbol_ratio,
        )
        for (family, bucket), rows in buckets.items()
    ]
    bucket_rows.sort(key=_bucket_sort_key)
    symbol_regime_rows = [
        row for row in bucket_rows if row["family"] == "symbol_micro_regime"
    ]
    loss_regime_rows = [
        row
        for row in bucket_rows
        if row["classification"] in {"loss_regime", "symbol_specific_loss_regime"}
    ]
    support_regime_rows = [
        row
        for row in bucket_rows
        if row["classification"] in {"support_regime", "symbol_specific_support_regime"}
    ]

    result = TridentAIMarketRegimeAuditResult(
        gate_sweep_report_path=str(source_path),
        profile_id=selected_profile_id,
        report_json_path=str(json_output),
        report_md_path=str(md_output),
        min_trades=min_trades,
        max_dominant_symbol_ratio=float(max_dominant_symbol_ratio),
        summary=_group_row("all", trades, key_name="scope"),
        fold_rows=[
            _group_row(fold, [trade for trade in trades if trade["fold_label"] == fold], key_name="fold")
            for fold in sorted({str(trade["fold_label"]) for trade in trades})
        ],
        bucket_rows=bucket_rows,
        loss_regime_rows=loss_regime_rows[:30],
        support_regime_rows=support_regime_rows[:30],
        symbol_regime_rows=symbol_regime_rows[:60],
        worst_trades=sorted(trades, key=lambda item: _number(item.get("pnl_usd")))[:20],
    )
    payload = build_market_regime_audit_report_payload(
        result=result,
        generated_at=_format_timestamp(datetime.now(timezone.utc)),
    )
    _write_report_outputs(payload, json_path=json_output, md_path=md_output)
    return result


def build_market_regime_audit_report_payload(
    *,
    result: TridentAIMarketRegimeAuditResult,
    generated_at: str,
) -> dict[str, object]:
    return {
        "generated_at": generated_at,
        "kind": "trident_ai_market_regime_audit",
        "result": result.to_dict(),
    }


def _select_gate_sweep_profile(path: Path, *, profile_id: str | None) -> Mapping[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    result = _mapping(payload.get("result"))
    rows = [dict(row) for row in result.get("profile_rows", []) if isinstance(row, Mapping)]
    if profile_id:
        for row in rows:
            if str(row.get("profile_id", "") or "") == profile_id:
                return row
        raise ValueError("profile_id_not_found_in_gate_sweep_report")
    if result.get("best_robust_profile"):
        return _mapping(result.get("best_robust_profile"))
    if result.get("best_guardrail_aware_profile"):
        return _mapping(result.get("best_guardrail_aware_profile"))
    if result.get("best_profile"):
        return _mapping(result.get("best_profile"))
    if rows:
        return rows[0]
    raise ValueError("gate_sweep_report_has_no_profiles")


def _trades_from_profile(profile: Mapping[str, object]) -> list[dict[str, object]]:
    trades: list[dict[str, object]] = []
    for fold in profile.get("folds", []):
        if not isinstance(fold, Mapping):
            continue
        fold_label = str(fold.get("fold_label", "") or "unknown")
        decisions = _decisions_by_id(fold.get("decision_journal_path"))
        for trade in _closed_trades(fold.get("paper_journal_path")):
            decision = decisions.get(str(trade.get("decision_id", "") or ""))
            trades.append(
                _trade_row(
                    fold_label=fold_label,
                    trade=trade,
                    decision=decision,
                )
            )
    return trades


def _decisions_by_id(path_value: object) -> dict[str, Mapping[str, object]]:
    path = Path(str(path_value or ""))
    if not path.exists():
        return {}
    rows: dict[str, Mapping[str, object]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, Mapping):
            continue
        proposal = _mapping(row.get("proposal"))
        decision_id = str(proposal.get("decision_id", "") or "")
        if decision_id:
            rows[decision_id] = row
    return rows


def _closed_trades(path_value: object) -> list[Mapping[str, object]]:
    path = Path(str(path_value or ""))
    if not path.exists():
        return []
    trades: list[Mapping[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, Mapping):
            continue
        if row.get("event_type") != PAPER_REPLAY_TRADE_CLOSED_EVENT:
            continue
        trade = _mapping(row.get("trade"))
        if trade:
            trades.append(trade)
    return trades


def _trade_row(
    *,
    fold_label: str,
    trade: Mapping[str, object],
    decision: Mapping[str, object] | None,
) -> dict[str, object]:
    decision_row = _mapping(decision)
    context = _mapping(decision_row.get("context"))
    features = _mapping(context.get("features"))
    hint = _mapping(context.get(CANDIDATE_HINT_FIELD))
    proposal = _mapping(decision_row.get("proposal"))
    symbol = str(trade.get("symbol", "") or decision_row.get("symbol", "") or "").upper()
    side = str(trade.get("side", "") or proposal.get("side", "") or "").lower()
    pnl = _number(trade.get("pnl_usd"))
    notional = _number(trade.get("notional_usd"))
    micro_regime = build_market_micro_regime(features, symbol=symbol, side=side)
    range_value = _number(features.get("bucket_range_bps"))
    vol_value = _number(features.get("realized_vol_short_bps"))
    volume_value = _number(features.get("volume_ratio"))
    vwap_value = _number(features.get("vwap_distance_bps"))
    microprice_value = _number(features.get("microprice_dislocation_bps"))
    base_regime = str(context.get("regime", "") or "unknown")
    row = {
        "fold_label": fold_label,
        "decision_id": str(trade.get("decision_id", "") or ""),
        "opened_at": str(trade.get("opened_at", "") or ""),
        "closed_at": str(trade.get("closed_at", "") or ""),
        "symbol": symbol,
        "side": side,
        "base_regime": base_regime,
        "close_reason": str(trade.get("close_reason", "") or "unknown"),
        "notional_usd": round(notional, 6),
        "pnl_usd": round(pnl, 6),
        "net_bps": round(_bps(pnl, notional), 6),
        "range_bps": round(range_value, 6),
        "short_vol_bps": round(vol_value, 6),
        "volume_ratio": round(volume_value, 6),
        "vwap_distance_bps": round(vwap_value, 6),
        "microprice_dislocation_bps": round(microprice_value, 6),
        "edge_to_cost_ratio": round(_number(hint.get("edge_to_cost_ratio")), 6),
        "estimated_net_edge_bps": round(_number(hint.get("estimated_net_edge_bps")), 6),
        "pattern_quality_score": round(_number(hint.get("pattern_quality_score")), 6),
        "range_bucket": str(micro_regime["range_bucket"]),
        "short_vol_bucket": str(micro_regime["short_vol_bucket"]),
        "volume_ratio_bucket": str(micro_regime["volume_ratio_bucket"]),
        "vwap_bucket": str(micro_regime["vwap_bucket"]),
        "microprice_bucket": str(micro_regime["microprice_bucket"]),
        "range_vol_regime": str(micro_regime["range_vol_regime"]),
        "flow_regime": str(micro_regime["flow_regime"]),
        "micro_regime": str(micro_regime["micro_regime"]),
    }
    row["symbol_micro_regime"] = str(micro_regime["symbol_micro_regime"])
    return row


def _bucket_keys(trade: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    symbol = str(trade.get("symbol", "") or "unknown")
    return (
        ("symbol", symbol),
        ("base_regime", str(trade.get("base_regime", "") or "unknown")),
        ("range_bucket", str(trade.get("range_bucket", "") or "unknown")),
        ("short_vol_bucket", str(trade.get("short_vol_bucket", "") or "unknown")),
        ("volume_ratio_bucket", str(trade.get("volume_ratio_bucket", "") or "unknown")),
        ("vwap_bucket", str(trade.get("vwap_bucket", "") or "unknown")),
        ("microprice_bucket", str(trade.get("microprice_bucket", "") or "unknown")),
        ("range_vol_regime", str(trade.get("range_vol_regime", "") or "unknown")),
        ("flow_regime", str(trade.get("flow_regime", "") or "unknown")),
        ("micro_regime", str(trade.get("micro_regime", "") or "unknown")),
        ("symbol_range_vol", f"{symbol}|{trade.get('range_vol_regime', 'unknown')}"),
        ("symbol_micro_regime", str(trade.get("symbol_micro_regime", "") or "unknown")),
    )


def _bucket_row(
    *,
    family: str,
    bucket: str,
    trades: Sequence[Mapping[str, object]],
    min_trades: int,
    max_dominant_symbol_ratio: float,
) -> dict[str, object]:
    row = _group_row(bucket, trades, key_name="bucket")
    row["family"] = family
    row["classification"] = _classification(
        row,
        min_trades=min_trades,
        max_dominant_symbol_ratio=max_dominant_symbol_ratio,
    )
    return row


def _group_row(label: str, trades: Sequence[Mapping[str, object]], *, key_name: str) -> dict[str, object]:
    closed = list(trades)
    pnl = sum(_number(trade.get("pnl_usd")) for trade in closed)
    notional = sum(_number(trade.get("notional_usd")) for trade in closed)
    wins = sum(1 for trade in closed if _number(trade.get("pnl_usd")) > 0.0)
    losses = len(closed) - wins
    stops = sum(1 for trade in closed if str(trade.get("close_reason", "") or "") == "stop_hit")
    symbols = Counter(str(trade.get("symbol", "") or "unknown") for trade in closed)
    folds = Counter(str(trade.get("fold_label", "") or "unknown") for trade in closed)
    close_reasons = Counter(str(trade.get("close_reason", "") or "unknown") for trade in closed)
    fold_pnl = defaultdict(float)
    symbol_pnl = defaultdict(float)
    for trade in closed:
        fold_pnl[str(trade.get("fold_label", "") or "unknown")] += _number(trade.get("pnl_usd"))
        symbol_pnl[str(trade.get("symbol", "") or "unknown")] += _number(trade.get("pnl_usd"))
    negative_folds = sum(1 for value in fold_pnl.values() if value < 0.0)
    positive_folds = sum(1 for value in fold_pnl.values() if value > 0.0)
    dominant_symbol_ratio = (
        max(symbols.values()) / len(closed) if closed and symbols else 0.0
    )
    return {
        key_name: label,
        "trades": len(closed),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(closed), 6) if closed else 0.0,
        "stop_hits": stops,
        "stop_rate": round(stops / len(closed), 6) if closed else 0.0,
        "pnl_usd": round(pnl, 6),
        "notional_usd": round(notional, 6),
        "avg_net_bps": round(_bps(pnl, notional), 6),
        "folds_with_trades": len(folds),
        "positive_folds": positive_folds,
        "negative_folds": negative_folds,
        "symbols": dict(sorted(symbols.items())),
        "symbol_count": len(symbols),
        "dominant_symbol_ratio": round(dominant_symbol_ratio, 6),
        "close_reasons": dict(sorted(close_reasons.items())),
        "fold_pnl_usd": {key: round(value, 6) for key, value in sorted(fold_pnl.items())},
        "symbol_pnl_usd": {key: round(value, 6) for key, value in sorted(symbol_pnl.items())},
    }


def _classification(
    row: Mapping[str, object],
    *,
    min_trades: int,
    max_dominant_symbol_ratio: float,
) -> str:
    trades = int(_number(row.get("trades")))
    if trades < min_trades:
        return "insufficient_samples"
    pnl = _number(row.get("pnl_usd"))
    negative_folds = int(_number(row.get("negative_folds")))
    positive_folds = int(_number(row.get("positive_folds")))
    dominant = _number(row.get("dominant_symbol_ratio"))
    symbol_specific = dominant > max_dominant_symbol_ratio
    if pnl < 0.0 and negative_folds >= max(1, positive_folds):
        return "symbol_specific_loss_regime" if symbol_specific else "loss_regime"
    if pnl > 0.0 and positive_folds > negative_folds:
        return "symbol_specific_support_regime" if symbol_specific else "support_regime"
    return "mixed_regime"


def _bucket_sort_key(row: Mapping[str, object]) -> tuple[int, float, float, str, str]:
    classification_rank = {
        "loss_regime": 0,
        "symbol_specific_loss_regime": 1,
        "support_regime": 2,
        "symbol_specific_support_regime": 3,
        "mixed_regime": 4,
        "insufficient_samples": 5,
    }.get(str(row.get("classification", "") or ""), 9)
    return (
        classification_rank,
        _number(row.get("pnl_usd")),
        -_number(row.get("trades")),
        str(row.get("family", "") or ""),
        str(row.get("bucket", "") or ""),
    )


def _bps(pnl: float, notional: float) -> float:
    return pnl / notional * 10_000.0 if notional > 0.0 else 0.0


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
        "# TRIDENT-AI Market Regime Audit",
        "",
        f"- Generated at: `{payload.get('generated_at')}`",
        f"- Gate sweep report: `{result.get('gate_sweep_report_path')}`",
        f"- Profile: `{result.get('profile_id')}`",
        f"- Trades: `{summary.get('trades', 0)}`",
        f"- PnL: `${float(summary.get('pnl_usd', 0.0)):.6f}`",
        f"- Avg net: `{float(summary.get('avg_net_bps', 0.0)):.6f}` bps",
        f"- Stop rate: `{float(summary.get('stop_rate', 0.0)):.2%}`",
        "",
        "## Loss Regimes",
        "",
        "| Family | Bucket | Trades | PnL | Avg bps | Stop rate | Neg folds | Symbols |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in result.get("loss_regime_rows", [])[:20]:
        if not isinstance(row, Mapping):
            continue
        lines.append(_bucket_markdown_row(row))
    if not result.get("loss_regime_rows"):
        lines.append("| none | n/a | 0 | $0.000000 | 0.000000 | 0.00% | 0 | {} |")
    lines.extend(
        [
            "",
            "## Support Regimes",
            "",
            "| Family | Bucket | Trades | PnL | Avg bps | Stop rate | Neg folds | Symbols |",
            "|---|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in result.get("support_regime_rows", [])[:20]:
        if not isinstance(row, Mapping):
            continue
        lines.append(_bucket_markdown_row(row))
    if not result.get("support_regime_rows"):
        lines.append("| none | n/a | 0 | $0.000000 | 0.000000 | 0.00% | 0 | {} |")
    lines.append("")
    return "\n".join(lines)


def _bucket_markdown_row(row: Mapping[str, object]) -> str:
    return (
        f"| {row.get('family')} | {row.get('bucket')} | {int(_number(row.get('trades')))} | "
        f"${_number(row.get('pnl_usd')):.6f} | {_number(row.get('avg_net_bps')):.6f} | "
        f"{_number(row.get('stop_rate')):.2%} | {int(_number(row.get('negative_folds')))} | "
        f"`{row.get('symbols', {})}` |"
    )
