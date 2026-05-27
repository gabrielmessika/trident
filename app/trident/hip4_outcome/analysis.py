from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_PROFILE_LOGS: dict[str, str] = {
    "mainnet_paper": "logs/hip4_outcome_mainnet_paper",
    "testnet": "logs/hip4_outcome_testnet",
    "mainnet": "logs/hip4_outcome_mainnet",
    "paper": "logs/hip4_outcome_paper",
}
DEFAULT_NAUTILUS_SHADOW_LOGS = "logs/hip4_nautilus_shadow"
DEFAULT_NAUTILUS_DECISION_JOIN_MAX_AGE_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class ReviewThresholds:
    min_testnet_settlements: int = 20
    min_testnet_markets: int = 5
    min_testnet_days: int = 2
    min_mainnet_opportunities: int = 20
    min_calibration_samples: int = 20
    min_guardrail_exclusions: int = 3
    min_profit_factor: float = 1.15
    max_brier_score: float = 0.23
    max_avg_fill_slippage: float = 0.02


def analyze_profiles(
    profiles: dict[str, str | Path],
    *,
    thresholds: ReviewThresholds | None = None,
    nautilus_shadow_dir: str | Path | None = None,
) -> dict[str, Any]:
    limits = thresholds or ReviewThresholds()
    profile_payloads = [
        analyze_profile(
            name,
            Path(logs_dir),
            thresholds=limits,
            nautilus_shadow_dir=nautilus_shadow_dir,
        )
        for name, logs_dir in sorted(profiles.items())
    ]
    payload = {
        "profiles": profile_payloads,
        "cross_profile": _cross_profile_summary(profile_payloads),
        "readiness": _readiness(profile_payloads, limits),
        "thresholds": {
            "min_testnet_settlements": limits.min_testnet_settlements,
            "min_testnet_markets": limits.min_testnet_markets,
            "min_testnet_days": limits.min_testnet_days,
            "min_mainnet_opportunities": limits.min_mainnet_opportunities,
            "min_calibration_samples": limits.min_calibration_samples,
            "min_guardrail_exclusions": limits.min_guardrail_exclusions,
            "min_profit_factor": limits.min_profit_factor,
            "max_brier_score": limits.max_brier_score,
            "max_avg_fill_slippage": limits.max_avg_fill_slippage,
        },
    }
    return payload


def analyze_profile(
    profile: str,
    logs_dir: str | Path,
    *,
    thresholds: ReviewThresholds | None = None,
    nautilus_shadow_dir: str | Path | None = None,
) -> dict[str, Any]:
    limits = thresholds or ReviewThresholds()
    root = Path(logs_dir)
    opportunities = _read_csv(root / "opportunities.csv")
    trades = _read_csv(root / "trades.csv")
    settlements = _read_csv(root / "settlements.csv")
    edge_decay = _read_csv(root / "edge_decay.csv")
    latency = _read_csv(root / "latency_stats.csv")
    short_features = _read_csv(root / "short_expiry_features.csv")
    daily_summary = _read_csv(root / "daily_summary.csv")
    market_observations = _read_jsonl(root / "market_observations.jsonl")
    decisions = _read_jsonl(root / "decisions.jsonl")
    execution_results = _read_jsonl(root / "execution_results.jsonl")

    decision_index = _build_decision_index(decisions)
    opportunity_index = _build_opportunity_index(opportunities)
    edge_decay_index = _build_edge_decay_index(edge_decay)
    trade_quality = _trade_quality(trades, opportunity_index, decisions)
    settlement_rows = _normalize_settlements(
        settlements,
        decision_index=decision_index,
        opportunity_index=opportunity_index,
        edge_decay_index=edge_decay_index,
        trades=trades,
    )
    settlement_summary = _settlement_summary(settlement_rows)
    calibration = _calibration_summary(settlement_rows)
    loss_review = _loss_review(settlement_rows)
    guardrail_candidates = _guardrail_candidate_analysis(settlement_rows, limits)
    nautilus_shadow = _nautilus_shadow_summary(
        _infer_nautilus_shadow_dir(root, nautilus_shadow_dir),
        settlement_rows,
        decisions,
    )

    return {
        "profile": profile,
        "logs_dir": str(root),
        "files": _file_summary(root),
        "row_counts": {
            "opportunities": len(opportunities),
            "decisions": len(decisions),
            "execution_results": len(execution_results),
            "trades": len(trades),
            "settlements": len(settlements),
            "edge_decay": len(edge_decay),
            "latency": len(latency),
            "short_expiry_features": len(short_features),
            "daily_summary": len(daily_summary),
            "market_observations": len(market_observations),
            "nautilus_shadow_data_quality": nautilus_shadow.get("row_count", 0),
        },
        "window": _window(
            opportunities
            + trades
            + settlements
            + edge_decay
            + latency
            + short_features
            + daily_summary
            + market_observations
        ),
        "opportunities": _opportunity_summary(opportunities),
        "market_observations": _market_observation_summary(market_observations),
        "decisions": _decision_summary(decisions),
        "trades": {
            "summary": _trade_summary(trades),
            "fill_quality": trade_quality,
        },
        "settlements": settlement_summary,
        "calibration": calibration,
        "loss_review": loss_review,
        "guardrail_candidates": guardrail_candidates,
        "edge_decay": _edge_decay_summary(edge_decay),
        "latency": _latency_summary(latency),
        "short_expiry": _short_expiry_summary(short_features),
        "nautilus_shadow": nautilus_shadow,
        "readiness": _profile_readiness(
            profile=profile,
            row_counts={
                "opportunities": len(opportunities),
                "settlements": len(settlements),
            },
            settlement_summary=settlement_summary,
            calibration=calibration,
            trade_quality=trade_quality,
            limits=limits,
        ),
    }


def render_markdown(payload: dict[str, Any]) -> str:
    lines = ["# HIP-4 Outcome Run Review", ""]
    readiness = payload.get("readiness", {})
    lines.extend(
        [
            f"- Status: `{readiness.get('status', 'unknown')}`",
            f"- Recommendation: {readiness.get('recommendation', 'n/a')}",
        ]
    )
    for reason in readiness.get("reasons", []):
        lines.append(f"- Blocker: {reason}")
    lines.append("")

    profiles = list(payload.get("profiles", []))
    if profiles:
        lines.extend(["## Profiles", ""])
        lines.append(
            "| Profile | Window | Opps | Obs | Approved | Trades | Settlements | PnL | PF | Brier | Fill slip |"
        )
        lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for profile in profiles:
            rows = profile.get("row_counts", {})
            decisions = profile.get("decisions", {})
            settlements = profile.get("settlements", {})
            calibration = profile.get("calibration", {})
            fill_quality = profile.get("trades", {}).get("fill_quality", {})
            window = profile.get("window", {})
            lines.append(
                "| "
                f"{profile.get('profile')} | "
                f"{_fmt_window(window)} | "
                f"{rows.get('opportunities', 0)} | "
                f"{rows.get('market_observations', 0)} | "
                f"{decisions.get('approved_count', 0)} | "
                f"{rows.get('trades', 0)} | "
                f"{settlements.get('count', 0)} | "
                f"{_fmt_num(settlements.get('net_pnl_usdc'))} | "
                f"{_fmt_num(settlements.get('profit_factor'))} | "
                f"{_fmt_num(calibration.get('brier_score'))} | "
                f"{_fmt_num(fill_quality.get('avg_slippage_vs_visible_ask'))} |"
            )
        lines.append("")

    cross = payload.get("cross_profile", {})
    if cross.get("opportunity_overlap"):
        lines.extend(["## Cross Profile Opportunities", ""])
        lines.append("| Underlying | Edge | Side | " + " | ".join(cross.get("profiles", [])) + " |")
        lines.append("|---|---|---|" + "---:|" * len(cross.get("profiles", [])))
        for row in cross.get("opportunity_overlap", [])[:12]:
            counts = row.get("counts", {})
            values = [str(counts.get(name, 0)) for name in cross.get("profiles", [])]
            lines.append(
                f"| {row.get('underlying')} | {row.get('edge_type')} | {row.get('side')} | "
                + " | ".join(values)
                + " |"
            )
        lines.append("")

    for profile in profiles:
        lines.extend([f"## {profile.get('profile')} Details", ""])
        profile_readiness = profile.get("readiness", {})
        lines.append(f"- Readiness: `{profile_readiness.get('status', 'unknown')}`")
        for reason in profile_readiness.get("reasons", []):
            lines.append(f"- Reason: {reason}")
        lines.append("")

        market_observations = profile.get("market_observations", {})
        if market_observations.get("count"):
            lines.extend(["### Market Observations", ""])
            lines.append(
                f"- Total: `{market_observations.get('count')}`; "
                f"books logged: `{market_observations.get('books_logged_count', 0)}`"
            )
            price_bucket = market_observations.get("price_bucket", {})
            if price_bucket.get("count"):
                lines.append(
                    f"- priceBucket: `{price_bucket.get('count')}` total, "
                    f"`{price_bucket.get('paper_supported_count', 0)}` paper-supported, "
                    f"`{price_bucket.get('incomplete_count', 0)}` incomplete/observe-only"
                )
            named_outcome = market_observations.get("named_outcome", {})
            if named_outcome.get("count"):
                lines.append(f"- namedOutcome: `{named_outcome.get('count')}` watch-only")
            lines.append("")
            class_support = market_observations.get("by_class_support", [])
            if class_support:
                lines.append("| Class | Support | Count | Books |")
                lines.append("|---|---|---:|---:|")
                for row in class_support[:12]:
                    lines.append(
                        f"| {row.get('class_name')} | {row.get('support_status')} | "
                        f"{row.get('count')} | {row.get('books_logged_count')} |"
                    )
                lines.append("")
            support_reasons = market_observations.get("support_reasons", [])
            if support_reasons:
                lines.append("| Support reason | Count |")
                lines.append("|---|---:|")
                for row in support_reasons[:10]:
                    lines.append(f"| {row.get('support_reason')} | {row.get('count')} |")
                lines.append("")

        nautilus_shadow = profile.get("nautilus_shadow", {})
        lines.extend(["### Nautilus Shadow Data Quality", ""])
        lines.append(f"- Status: `{nautilus_shadow.get('status', 'nautilus_shadow_missing')}`")
        if nautilus_shadow.get("status") != "nautilus_shadow_missing":
            lines.append(
                f"- Rows: `{nautilus_shadow.get('row_count', 0)}`; "
                f"matched settlements: `{nautilus_shadow.get('matched_settlement_count', 0)}`; "
                f"markets: `{nautilus_shadow.get('market_count', 0)}`"
            )
            lines.append(
                f"- Avg quality: `{_fmt_num(nautilus_shadow.get('avg_quality_score'))}`; "
                f"avg max age ms: `{_fmt_num(nautilus_shadow.get('avg_max_book_age_ms'))}`; "
                f"avg skew ms: `{_fmt_num(nautilus_shadow.get('avg_book_pair_skew_ms'))}`"
            )
            row_buckets = nautilus_shadow.get("quality_row_buckets", {})
            if isinstance(row_buckets, dict) and row_buckets.get("by_quality_score"):
                lines.append("")
                lines.append("| Data quality bucket | Count | Tradable rate | Avg max age ms | Avg skew ms |")
                lines.append("|---|---:|---:|---:|---:|")
                for row in row_buckets.get("by_quality_score", []):
                    lines.append(
                        f"| quality {row.get('bucket')} | {row.get('count')} | "
                        f"{_fmt_num(row.get('tradable_rate'))} | "
                        f"{_fmt_num(row.get('avg_max_book_age_ms'))} | "
                        f"{_fmt_num(row.get('avg_book_pair_skew_ms'))} |"
                    )
                for row in row_buckets.get("by_max_book_age_ms", []):
                    lines.append(
                        f"| age {row.get('bucket')} | {row.get('count')} | "
                        f"{_fmt_num(row.get('tradable_rate'))} | "
                        f"{_fmt_num(row.get('avg_max_book_age_ms'))} | "
                        f"{_fmt_num(row.get('avg_book_pair_skew_ms'))} |"
                    )
                for row in row_buckets.get("by_book_pair_skew_ms", []):
                    lines.append(
                        f"| skew {row.get('bucket')} | {row.get('count')} | "
                        f"{_fmt_num(row.get('tradable_rate'))} | "
                        f"{_fmt_num(row.get('avg_max_book_age_ms'))} | "
                        f"{_fmt_num(row.get('avg_book_pair_skew_ms'))} |"
                    )
            decision_time = nautilus_shadow.get("decision_time", {})
            if isinstance(decision_time, dict):
                lines.append("")
                lines.append(
                    f"- Decision-time join: `{decision_time.get('matched_decision_count', 0)}` "
                    f"matched decisions; `{decision_time.get('unmatched_decision_count', 0)}` "
                    f"unmatched; max age `{_fmt_num(decision_time.get('max_match_age_seconds'))}`s"
                )
                lines.append(
                    f"- Decision-time approved: `{decision_time.get('approved_count', 0)}`; "
                    f"rejected: `{decision_time.get('rejected_count', 0)}`; "
                    f"Nautilus would-block approved: `{decision_time.get('would_block_approved_count', 0)}`"
                )
                join_buckets = decision_time.get("buckets", {})
                join_bucket_rows: list[tuple[str, dict[str, Any]]] = []
                if isinstance(join_buckets, dict):
                    for label, key in (
                        ("quality", "by_quality_score"),
                        ("age", "by_max_book_age_ms"),
                        ("skew", "by_book_pair_skew_ms"),
                        ("divergence", "by_reference_divergence_bps"),
                    ):
                        join_bucket_rows.extend(
                            (label, row)
                            for row in join_buckets.get(key, [])
                            if isinstance(row, dict)
                        )
                if join_bucket_rows:
                    lines.append("")
                    lines.append("| Decision bucket | Count | Approved | Rejected | Would block | Avg age s | Avg quality |")
                    lines.append("|---|---:|---:|---:|---:|---:|---:|")
                    for label, row in join_bucket_rows:
                        lines.append(
                            f"| {label} {row.get('bucket')} | {row.get('count')} | "
                            f"{row.get('approved_count')} | {row.get('rejected_count')} | "
                            f"{row.get('would_block_count')} | "
                            f"{_fmt_num(row.get('avg_match_age_seconds'))} | "
                            f"{_fmt_num(row.get('avg_quality_score'))} |"
                        )
            low_quality = nautilus_shadow.get("low_quality_settlements", {})
            if isinstance(low_quality, dict):
                metrics = low_quality.get("metrics", {})
                lines.append(
                    f"- Settled low-quality entries: `{low_quality.get('count', 0)}`; "
                    f"PnL: `{_fmt_num(metrics.get('net_pnl_usdc'))}`; "
                    f"PF: `{_fmt_num(metrics.get('profit_factor'))}`; "
                    f"Brier: `{_fmt_num(metrics.get('brier_score'))}`"
                )
            buckets = nautilus_shadow.get("buckets", {})
            quality_buckets = buckets.get("by_quality_score", []) if isinstance(buckets, dict) else []
            if quality_buckets:
                lines.append("")
                lines.append("| Quality bucket | Count | PnL | PF | Brier |")
                lines.append("|---|---:|---:|---:|---:|")
                for row in quality_buckets:
                    lines.append(
                        f"| {row.get('bucket')} | {row.get('count')} | "
                        f"{_fmt_num(row.get('net_pnl_usdc'))} | "
                        f"{_fmt_num(row.get('profit_factor'))} | "
                        f"{_fmt_num(row.get('brier_score'))} |"
                    )
        lines.append("")

        top_losses = profile.get("loss_review", {}).get("categories", [])
        if top_losses:
            lines.extend(["### Loss Review", ""])
            lines.append("| Category | Count | PnL |")
            lines.append("|---|---:|---:|")
            for row in top_losses[:8]:
                lines.append(
                    f"| {row.get('category')} | {row.get('count')} | {_fmt_num(row.get('pnl_usdc'))} |"
                )
            lines.append("")

        guardrails = profile.get("guardrail_candidates", {}).get("candidates", [])
        if guardrails:
            lines.extend(["### Guardrail Candidates", ""])
            lines.append(
                "| Candidate | Kind | Verdict | Excluded | Excluded PnL | PnL after | PF after | Brier after | Note |"
            )
            lines.append("|---|---|---|---:|---:|---:|---:|---:|---|")
            for row in guardrails[:10]:
                after = row.get("after", {})
                excluded = row.get("excluded", {})
                lines.append(
                    f"| {row.get('name')} | {row.get('kind')} | {row.get('verdict')} | "
                    f"{row.get('excluded_count')} | "
                    f"{_fmt_num(excluded.get('net_pnl_usdc'))} | "
                    f"{_fmt_num(after.get('net_pnl_usdc'))} | "
                    f"{_fmt_num(after.get('profit_factor'))} | "
                    f"{_fmt_num(after.get('brier_score'))} | "
                    f"{row.get('note', '')} |"
                )
            lines.append("")

        calibration = profile.get("calibration", {})
        if calibration.get("count"):
            lines.extend(["### Calibration", ""])
            lines.append("| Slice | Count | Avg pred | Win rate | Brier | Log loss | PnL |")
            lines.append("|---|---:|---:|---:|---:|---:|---:|")
            for row in calibration.get("by_edge_type", [])[:8]:
                lines.append(
                    f"| {row.get('edge_type')} | {row.get('count')} | "
                    f"{_fmt_num(row.get('avg_predicted_probability'))} | "
                    f"{_fmt_num(row.get('actual_win_rate'))} | "
                    f"{_fmt_num(row.get('brier_score'))} | "
                    f"{_fmt_num(row.get('log_loss'))} | "
                    f"{_fmt_num(row.get('pnl_usdc'))} |"
                )
            lines.append("")

        reject_reasons = profile.get("decisions", {}).get("rejected_reasons", [])
        if reject_reasons:
            lines.extend(["### Decision Rejects", ""])
            lines.append("| Reason | Count |")
            lines.append("|---|---:|")
            for row in reject_reasons[:8]:
                lines.append(f"| {row.get('reason')} | {row.get('count')} |")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


def _file_summary(root: Path) -> dict[str, dict[str, Any]]:
    names = [
        "opportunities.csv",
        "decisions.jsonl",
        "execution_results.jsonl",
        "trades.csv",
        "settlements.csv",
        "edge_decay.csv",
        "early_exits.csv",
        "shadow_exit_policies.csv",
        "shadow_sizing.csv",
        "shadow_maker_quotes.csv",
        "latency_stats.csv",
        "short_expiry_features.csv",
        "market_observations.jsonl",
        "daily_summary.csv",
    ]
    files: dict[str, dict[str, Any]] = {}
    for name in names:
        path = root / name
        files[name] = {
            "exists": path.exists(),
            "size_bytes": path.stat().st_size if path.exists() else 0,
        }
    return files


def _build_decision_index(decisions: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for payload in decisions:
        signal = payload.get("signal")
        if not isinstance(signal, dict):
            continue
        key = _key(signal.get("market_id"), signal.get("side"))
        if key[0] == "":
            continue
        decision = payload.get("supervisor_decision")
        approved = isinstance(decision, dict) and bool(decision.get("approved"))
        current = index.get(key)
        if current is None or (approved and not bool(current.get("_approved"))):
            index[key] = {**payload, "_approved": approved}
    return index


def _build_opportunity_index(rows: list[dict[str, str]]) -> dict[tuple[str, str], dict[str, str]]:
    index: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        key = _key(row.get("market_id"), row.get("side"))
        if key[0] and key not in index:
            index[key] = row
    return index


def _build_edge_decay_index(rows: list[dict[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        key = _key(row.get("market_id"), row.get("side"))
        if key[0]:
            groups[key].append(row)

    index: dict[tuple[str, str], dict[str, Any]] = {}
    for key, items in groups.items():
        ordered = sorted(items, key=lambda item: str(item.get("ts", "")))
        deltas = [_float(item.get("delta_net_edge")) for item in ordered]
        currents = [_float(item.get("current_net_edge")) for item in ordered]
        index[key] = {
            "count": len(ordered),
            "first_net_edge": _float(ordered[0].get("first_net_edge")),
            "last_net_edge": currents[-1] if currents else None,
            "min_current_net_edge": min(currents) if currents else None,
            "min_delta_net_edge": min(deltas) if deltas else None,
            "max_elapsed_seconds": max(_float(item.get("elapsed_seconds")) for item in ordered),
        }
    return index


def _decision_summary(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    approved = 0
    rejected = 0
    reject_reasons: Counter[str] = Counter()
    modes: Counter[str] = Counter()
    edge_types: Counter[str] = Counter()
    for payload in decisions:
        signal = payload.get("signal")
        decision = payload.get("supervisor_decision")
        if isinstance(signal, dict):
            edge_types[str(signal.get("edge_type", "unknown"))] += 1
        if not isinstance(decision, dict):
            continue
        modes[str(decision.get("execution_mode", "unknown"))] += 1
        if decision.get("approved"):
            approved += 1
        else:
            rejected += 1
            reject_reasons[str(decision.get("reason", "unknown"))] += 1
    return {
        "count": len(decisions),
        "approved_count": approved,
        "rejected_count": rejected,
        "approval_rate": _safe_div(approved, approved + rejected),
        "rejected_reasons": _counter_rows(reject_reasons, "reason"),
        "execution_modes": _counter_rows(modes, "mode"),
        "edge_types": _counter_rows(edge_types, "edge_type"),
    }


def _opportunity_summary(rows: list[dict[str, str]]) -> dict[str, Any]:
    net_edges = [_float(row.get("net_edge")) for row in rows]
    confidences = [_float(row.get("confidence")) for row in rows]
    return {
        "count": len(rows),
        "unique_markets": len({str(row.get("market_id", "")) for row in rows if row.get("market_id")}),
        "avg_net_edge": _avg(net_edges),
        "max_net_edge": max(net_edges) if net_edges else None,
        "avg_confidence": _avg(confidences),
        "by_date": _aggregate_opportunities(rows, ["date"]),
        "by_underlying_edge_side": _aggregate_opportunities(
            rows,
            ["underlying", "edge_type", "side"],
        ),
    }


def _aggregate_opportunities(rows: list[dict[str, str]], keys: list[str]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[_group_key(row, keys)].append(row)

    output: list[dict[str, Any]] = []
    for key, items in groups.items():
        net_edges = [_float(row.get("net_edge")) for row in items]
        confidences = [_float(row.get("confidence")) for row in items]
        payload = {name: value for name, value in zip(keys, key)}
        payload.update(
            {
                "count": len(items),
                "avg_net_edge": _avg(net_edges),
                "max_net_edge": max(net_edges) if net_edges else None,
                "avg_confidence": _avg(confidences),
            }
        )
        output.append(payload)
    return sorted(output, key=lambda item: (-int(item["count"]), str(item)))


def _trade_summary(rows: list[dict[str, str]]) -> dict[str, Any]:
    sizes = [_float(row.get("size_usdc")) for row in rows]
    status = Counter(str(row.get("status", "unknown")) for row in rows)
    return {
        "count": len(rows),
        "total_size_usdc": round(sum(sizes), 8),
        "avg_size_usdc": _avg(sizes),
        "statuses": _counter_rows(status, "status"),
        "by_underlying_edge_side": _aggregate_trades(rows, ["underlying", "edge_type", "side"]),
    }


def _aggregate_trades(rows: list[dict[str, str]], keys: list[str]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[_group_key(row, keys)].append(row)
    output: list[dict[str, Any]] = []
    for key, items in groups.items():
        sizes = [_float(row.get("size_usdc")) for row in items]
        prices = [_float(row.get("price")) for row in items]
        payload = {name: value for name, value in zip(keys, key)}
        payload.update(
            {
                "count": len(items),
                "total_size_usdc": round(sum(sizes), 8),
                "avg_price": _avg(prices),
            }
        )
        output.append(payload)
    return sorted(output, key=lambda item: (-int(item["count"]), str(item)))


def _trade_quality(
    trades: list[dict[str, str]],
    opportunity_index: dict[tuple[str, str], dict[str, str]],
    decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    approved = 0
    for payload in decisions:
        decision = payload.get("supervisor_decision")
        if isinstance(decision, dict) and decision.get("approved"):
            approved += 1
    filled = len([row for row in trades if _float(row.get("token_qty")) > 0])
    slippages: list[float] = []
    missing_ask = 0
    for trade in trades:
        key = _key(trade.get("market_id"), trade.get("side"))
        opportunity = opportunity_index.get(key, {})
        side = str(trade.get("side", "")).upper()
        ask = None
        if side == "BUY_YES":
            ask = _optional_float(opportunity.get("yes_ask"))
        elif side == "BUY_NO":
            ask = _optional_float(opportunity.get("no_ask"))
        if ask is None:
            missing_ask += 1
            continue
        slippages.append(round(_float(trade.get("price")) - ask, 8))

    return {
        "approved_decisions": approved,
        "filled_trades": filled,
        "fill_rate_vs_approved": _safe_div(filled, approved),
        "avg_slippage_vs_visible_ask": _avg(slippages),
        "max_slippage_vs_visible_ask": max(slippages) if slippages else None,
        "min_slippage_vs_visible_ask": min(slippages) if slippages else None,
        "missing_visible_ask_count": missing_ask,
    }


def _normalize_settlements(
    rows: list[dict[str, str]],
    *,
    decision_index: dict[tuple[str, str], dict[str, Any]],
    opportunity_index: dict[tuple[str, str], dict[str, str]],
    edge_decay_index: dict[tuple[str, str], dict[str, Any]],
    trades: list[dict[str, str]],
) -> list[dict[str, Any]]:
    trade_index = _trade_index(trades)
    normalized: list[dict[str, Any]] = []
    for row in rows:
        key = _key(row.get("market_id"), row.get("side"))
        decision = decision_index.get(key, {})
        signal = decision.get("signal") if isinstance(decision.get("signal"), dict) else {}
        opportunity = opportunity_index.get(key, {})
        trade = trade_index.get(key, {})
        edge_decay = edge_decay_index.get(key, {})
        prediction = _predicted_win_probability(signal, opportunity)
        pnl = _settlement_pnl(row)
        gross_pnl = _optional_float(row.get("gross_pnl_usdc"))
        fee = _float(row.get("fee_usdc"))
        if gross_pnl is None:
            gross_pnl = pnl + fee if fee else pnl
        is_win = _settlement_is_win(row, pnl)
        normalized.append(
            {
                "ts": str(row.get("ts", "")),
                "date": _date(row.get("ts")),
                "market_id": str(row.get("market_id", "")),
                "outcome": str(row.get("outcome", "")),
                "underlying": str(row.get("underlying") or signal.get("underlying", "")).upper(),
                "edge_type": str(signal.get("edge_type") or opportunity.get("edge_type", "unknown")),
                "side": str(row.get("side") or signal.get("side", "")),
                "result": str(row.get("result", "")),
                "payout_usdc": _float(row.get("payout_usdc")),
                "fee_usdc": fee,
                "gross_pnl_usdc": round(gross_pnl, 8),
                "net_pnl_usdc": round(pnl, 8),
                "is_win": is_win,
                "notes": str(row.get("notes", "")),
                "predicted_win_probability": prediction.get("probability"),
                "prediction_source": prediction.get("source"),
                "signal": signal,
                "opportunity": opportunity,
                "edge_decay": edge_decay,
                "trade": trade,
                "loss_category": None if is_win else _classify_loss(
                    row=row,
                    pnl=pnl,
                    signal=signal,
                    opportunity=opportunity,
                    edge_decay=edge_decay,
                    trade=trade,
                    prediction=prediction,
                ),
            }
        )
    return sorted(normalized, key=lambda item: str(item.get("ts", "")))


def _trade_index(rows: list[dict[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        key = _key(row.get("market_id"), row.get("side"))
        if key[0]:
            grouped[key].append(row)
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for key, items in grouped.items():
        sizes = [_float(row.get("size_usdc")) for row in items]
        prices = [_float(row.get("price")) for row in items]
        index[key] = {
            "count": len(items),
            "total_size_usdc": round(sum(sizes), 8),
            "avg_price": _avg(prices),
        }
    return index


def _settlement_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [float(row["net_pnl_usdc"]) for row in rows]
    wins = [row for row in rows if bool(row.get("is_win"))]
    losses = [row for row in rows if not bool(row.get("is_win"))]
    gross_profit = sum(value for value in pnls if value > 0)
    gross_loss = sum(value for value in pnls if value < 0)
    return {
        "count": len(rows),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": _safe_div(len(wins), len(rows)),
        "net_pnl_usdc": round(sum(pnls), 8),
        "gross_profit_usdc": round(gross_profit, 8),
        "gross_loss_usdc": round(gross_loss, 8),
        "profit_factor": _safe_div(gross_profit, abs(gross_loss)),
        "avg_pnl_usdc": _avg(pnls),
        "fees_usdc": round(sum(float(row.get("fee_usdc", 0.0)) for row in rows), 8),
        "max_drawdown_usdc": _max_drawdown(pnls),
        "unique_markets": len({str(row.get("market_id", "")) for row in rows if row.get("market_id")}),
        "active_days": len({str(row.get("date", "")) for row in rows if row.get("date")}),
        "by_date": _aggregate_settlements(rows, ["date"]),
        "by_underlying": _aggregate_settlements(rows, ["underlying"]),
        "by_edge_type": _aggregate_settlements(rows, ["edge_type"]),
        "by_underlying_edge_side": _aggregate_settlements(
            rows,
            ["underlying", "edge_type", "side"],
        ),
    }


def _aggregate_settlements(rows: list[dict[str, Any]], keys: list[str]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[tuple(str(row.get(key, "unknown") or "unknown") for key in keys)].append(row)
    output: list[dict[str, Any]] = []
    for key, items in groups.items():
        pnls = [float(row.get("net_pnl_usdc", 0.0)) for row in items]
        wins = len([row for row in items if bool(row.get("is_win"))])
        payload = {name: value for name, value in zip(keys, key)}
        payload.update(
            {
                "count": len(items),
                "win_count": wins,
                "loss_count": len(items) - wins,
                "win_rate": _safe_div(wins, len(items)),
                "net_pnl_usdc": round(sum(pnls), 8),
                "avg_pnl_usdc": _avg(pnls),
            }
        )
        output.append(payload)
    return sorted(output, key=lambda item: (float(item["net_pnl_usdc"]), str(item)))


def _calibration_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    samples = [
        row
        for row in rows
        if row.get("predicted_win_probability") is not None and row.get("side") != "BUY_BOTH"
    ]
    base = _calibration_metrics(samples)
    base.update(
        {
            "by_bucket": _calibration_buckets(samples),
            "by_date": _aggregate_calibration(samples, ["date"]),
            "by_underlying": _aggregate_calibration(samples, ["underlying"]),
            "by_edge_type": _aggregate_calibration(samples, ["edge_type"]),
            "prediction_sources": _counter_rows(
                Counter(str(row.get("prediction_source", "unknown")) for row in samples),
                "source",
            ),
        }
    )
    return base


def _calibration_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {
            "count": 0,
            "avg_predicted_probability": None,
            "actual_win_rate": None,
            "brier_score": None,
            "log_loss": None,
            "pnl_usdc": 0.0,
        }
    probs = [_clamp(float(row["predicted_win_probability"]), 0.001, 0.999) for row in rows]
    outcomes = [1.0 if row.get("is_win") else 0.0 for row in rows]
    brier = sum((prob - actual) ** 2 for prob, actual in zip(probs, outcomes)) / len(rows)
    log_loss = -sum(
        actual * math.log(prob) + (1.0 - actual) * math.log(1.0 - prob)
        for prob, actual in zip(probs, outcomes)
    ) / len(rows)
    return {
        "count": len(rows),
        "avg_predicted_probability": round(sum(probs) / len(probs), 8),
        "actual_win_rate": round(sum(outcomes) / len(outcomes), 8),
        "brier_score": round(brier, 8),
        "log_loss": round(log_loss, 8),
        "pnl_usdc": round(sum(float(row.get("net_pnl_usdc", 0.0)) for row in rows), 8),
    }


def _calibration_buckets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        prob = float(row["predicted_win_probability"])
        low = min(int(prob * 10) / 10, 0.9)
        high = low + 0.1
        buckets[f"{low:.1f}-{high:.1f}"].append(row)
    output: list[dict[str, Any]] = []
    for bucket, items in sorted(buckets.items()):
        output.append({"bucket": bucket, **_calibration_metrics(items)})
    return output


def _aggregate_calibration(rows: list[dict[str, Any]], keys: list[str]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[tuple(str(row.get(key, "unknown") or "unknown") for key in keys)].append(row)
    output: list[dict[str, Any]] = []
    for key, items in groups.items():
        payload = {name: value for name, value in zip(keys, key)}
        payload.update(_calibration_metrics(items))
        output.append(payload)
    return sorted(output, key=lambda item: (-int(item["count"]), str(item)))


def _loss_review(rows: list[dict[str, Any]]) -> dict[str, Any]:
    losses = [row for row in rows if not bool(row.get("is_win"))]
    category_pnl: dict[str, float] = defaultdict(float)
    category_count: Counter[str] = Counter()
    examples: list[dict[str, Any]] = []
    for row in sorted(losses, key=lambda item: float(item.get("net_pnl_usdc", 0.0))):
        category = str(row.get("loss_category") or "unclassified_loss")
        category_count[category] += 1
        category_pnl[category] += float(row.get("net_pnl_usdc", 0.0))
        if len(examples) < 20:
            examples.append(
                {
                    "ts": row.get("ts"),
                    "market_id": row.get("market_id"),
                    "underlying": row.get("underlying"),
                    "edge_type": row.get("edge_type"),
                    "side": row.get("side"),
                    "result": row.get("result"),
                    "net_pnl_usdc": row.get("net_pnl_usdc"),
                    "predicted_win_probability": row.get("predicted_win_probability"),
                    "loss_category": category,
                }
            )
    categories = [
        {
            "category": category,
            "count": count,
            "pnl_usdc": round(category_pnl[category], 8),
        }
        for category, count in category_count.most_common()
    ]
    categories.sort(key=lambda item: (float(item["pnl_usdc"]), -int(item["count"])))
    return {
        "loss_count": len(losses),
        "categories": categories,
        "worst_examples": examples,
    }


def _guardrail_candidate_analysis(
    rows: list[dict[str, Any]],
    limits: ReviewThresholds,
) -> dict[str, Any]:
    baseline = _guardrail_metrics(rows)
    candidates: list[dict[str, Any]] = []
    if not rows:
        return {"baseline": baseline, "candidates": candidates}

    candidate_specs: list[tuple[str, str, str, Any]] = [
        (
            "reference_divergence_context",
            "feature",
            "Exclude rows whose entry context already shows rejected/divergent reference sources.",
            _row_has_reference_divergence_context,
        ),
        (
            "edge_decay_or_stale_book_context",
            "feature",
            "Exclude rows whose logged edge later decayed materially or crossed non-positive.",
            _row_has_edge_decay_context,
        ),
        (
            "late_or_short_expiry_context",
            "feature",
            "Exclude late-expiry and short-expiry rows as a broad timing stress test.",
            _row_is_late_or_short_expiry_context,
        ),
    ]

    for category in sorted(
        {
            str(row.get("loss_category"))
            for row in rows
            if row.get("loss_category")
        }
    ):
        candidate_specs.append(
            (
                f"loss_category:{category}",
                "post_trade_loss_category",
                f"Exclude rows matching loss category `{category}`.",
                lambda row, category=category: row.get("loss_category") == category,
            )
        )

    for edge_type in sorted({str(row.get("edge_type", "unknown")) for row in rows}):
        candidate_specs.append(
            (
                f"edge_type:{edge_type}",
                "slice",
                f"Exclude all `{edge_type}` rows.",
                lambda row, edge_type=edge_type: str(row.get("edge_type", "unknown")) == edge_type,
            )
        )

    for key, items in _rows_by_slice(rows, ["underlying", "edge_type", "side"]).items():
        if len(items) < limits.min_guardrail_exclusions:
            continue
        metrics = _guardrail_metrics(items)
        if (
            float(metrics.get("net_pnl_usdc", 0.0)) >= 0.0
            and not _metric_exceeds(metrics.get("brier_score"), limits.max_brier_score)
        ):
            continue
        underlying, edge_type, side = key
        candidate_specs.append(
            (
                f"slice:{underlying}:{edge_type}:{side}",
                "slice",
                f"Exclude `{underlying}/{edge_type}/{side}`.",
                lambda row, key=key: (
                    str(row.get("underlying", "unknown")),
                    str(row.get("edge_type", "unknown")),
                    str(row.get("side", "unknown")),
                )
                == key,
            )
        )

    seen: set[str] = set()
    for name, kind, description, predicate in candidate_specs:
        if name in seen:
            continue
        seen.add(name)
        candidate = _simulate_guardrail_candidate(
            rows=rows,
            baseline=baseline,
            name=name,
            kind=kind,
            description=description,
            predicate=predicate,
            limits=limits,
        )
        if candidate is not None:
            candidates.append(candidate)

    candidates.sort(
        key=lambda item: (
            _guardrail_verdict_rank(str(item.get("verdict", ""))),
            -float(item.get("pnl_delta_usdc", 0.0)),
            -int(item.get("excluded_count", 0)),
            str(item.get("name", "")),
        )
    )
    return {
        "baseline": baseline,
        "candidates": candidates,
    }


def _simulate_guardrail_candidate(
    *,
    rows: list[dict[str, Any]],
    baseline: dict[str, Any],
    name: str,
    kind: str,
    description: str,
    predicate: Any,
    limits: ReviewThresholds,
) -> dict[str, Any] | None:
    excluded = [row for row in rows if predicate(row)]
    if not excluded:
        return None
    kept = [row for row in rows if not predicate(row)]
    after = _guardrail_metrics(kept)
    excluded_metrics = _guardrail_metrics(excluded)
    pnl_delta = round(
        float(after.get("net_pnl_usdc", 0.0)) - float(baseline.get("net_pnl_usdc", 0.0)),
        8,
    )
    brier_delta = _optional_delta(after.get("brier_score"), baseline.get("brier_score"))
    profit_factor_delta = _optional_delta(
        after.get("profit_factor"),
        baseline.get("profit_factor"),
    )
    verdict, note = _guardrail_verdict(
        excluded_count=len(excluded),
        pnl_delta=pnl_delta,
        brier_delta=brier_delta,
        after=after,
        baseline=baseline,
        limits=limits,
    )
    if kind == "post_trade_loss_category" and verdict == "keep":
        verdict = "watch"
        note = "diagnostic post-trade; deriver un predicat entry-time avant promotion"
    return {
        "name": name,
        "kind": kind,
        "description": description,
        "verdict": verdict,
        "note": note,
        "excluded_count": len(excluded),
        "kept_count": len(kept),
        "pnl_delta_usdc": pnl_delta,
        "brier_delta": brier_delta,
        "profit_factor_delta": profit_factor_delta,
        "after": after,
        "excluded": excluded_metrics,
    }


def _guardrail_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnls = [float(row.get("net_pnl_usdc", 0.0)) for row in rows]
    wins = len([row for row in rows if bool(row.get("is_win"))])
    gross_profit = sum(value for value in pnls if value > 0)
    gross_loss = sum(value for value in pnls if value < 0)
    samples = [
        row
        for row in rows
        if row.get("predicted_win_probability") is not None and row.get("side") != "BUY_BOTH"
    ]
    calibration = _calibration_metrics(samples)
    return {
        "count": len(rows),
        "win_count": wins,
        "loss_count": len(rows) - wins,
        "win_rate": _safe_div(wins, len(rows)),
        "net_pnl_usdc": round(sum(pnls), 8),
        "gross_profit_usdc": round(gross_profit, 8),
        "gross_loss_usdc": round(gross_loss, 8),
        "profit_factor": _safe_div(gross_profit, abs(gross_loss)),
        "max_drawdown_usdc": _max_drawdown(pnls),
        "calibration_count": calibration.get("count", 0),
        "brier_score": calibration.get("brier_score"),
        "log_loss": calibration.get("log_loss"),
    }


def _guardrail_verdict(
    *,
    excluded_count: int,
    pnl_delta: float,
    brier_delta: float | None,
    after: dict[str, Any],
    baseline: dict[str, Any],
    limits: ReviewThresholds,
) -> tuple[str, str]:
    if excluded_count < limits.min_guardrail_exclusions:
        return "watch", "sample faible; garder comme signal, pas comme regle"

    after_count = int(after.get("count", 0))
    if after_count < limits.min_calibration_samples:
        return "park", "trop peu de trades restants apres exclusion"

    after_brier = _optional_float(after.get("brier_score"))
    baseline_brier = _optional_float(baseline.get("brier_score"))
    after_pf = _optional_float(after.get("profit_factor"))
    baseline_pf = _optional_float(baseline.get("profit_factor"))
    brier_improves = (
        after_brier is not None
        and baseline_brier is not None
        and after_brier < baseline_brier
    )
    pf_improves = (
        after_pf is not None
        and baseline_pf is not None
        and after_pf >= baseline_pf
    )
    pnl_improves = pnl_delta > 0.0
    passes_absolute_gates = (
        after_pf is not None
        and after_pf >= limits.min_profit_factor
        and after_brier is not None
        and after_brier <= limits.max_brier_score
    )
    if pnl_improves and brier_improves and pf_improves and passes_absolute_gates:
        return "keep", "ameliore PnL, PF et Brier; candidat guardrail prioritaire"
    if pnl_improves and (brier_improves or pf_improves):
        return "watch", "ameliore une partie des metriques; revalider sur prochaine fenetre"
    if pnl_improves:
        return "park", "ameliore le PnL mais degrade calibration ou PF"
    if brier_improves and (brier_delta is not None and brier_delta < -0.03):
        return "watch", "ameliore nettement la calibration mais coute du PnL"
    return "kill", "exclusion non additive sur cette fenetre"


def _rows_by_slice(
    rows: list[dict[str, Any]],
    keys: list[str],
) -> dict[tuple[str, ...], list[dict[str, Any]]]:
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[
            tuple(str(row.get(key, "unknown") or "unknown") for key in keys)
        ].append(row)
    return grouped


def _row_has_reference_divergence_context(row: dict[str, Any]) -> bool:
    signal = row.get("signal") if isinstance(row.get("signal"), dict) else {}
    metadata = signal.get("metadata") if isinstance(signal.get("metadata"), dict) else {}
    rejected_sources = metadata.get("reference_rejected_sources")
    max_deviation = _optional_float(metadata.get("reference_max_deviation_bps"))
    return bool(isinstance(rejected_sources, list) and rejected_sources) or (
        max_deviation is not None and max_deviation > 50.0
    )


def _row_has_edge_decay_context(row: dict[str, Any]) -> bool:
    edge_decay = row.get("edge_decay") if isinstance(row.get("edge_decay"), dict) else {}
    min_delta = _optional_float(edge_decay.get("min_delta_net_edge"))
    min_current = _optional_float(edge_decay.get("min_current_net_edge"))
    return (min_delta is not None and min_delta <= -0.05) or (
        min_current is not None and min_current <= 0.0
    )


def _row_is_late_or_short_expiry_context(row: dict[str, Any]) -> bool:
    signal = row.get("signal") if isinstance(row.get("signal"), dict) else {}
    metadata = signal.get("metadata") if isinstance(signal.get("metadata"), dict) else {}
    opportunity = row.get("opportunity") if isinstance(row.get("opportunity"), dict) else {}
    edge_type = str(row.get("edge_type", "")).upper()
    seconds_left = _optional_float(
        metadata.get("time_to_expiry_seconds")
        or metadata.get("seconds_left")
        or opportunity.get("time_to_expiry")
    )
    return edge_type in {"LATE_EXPIRY", "SHORT_EXPIRY"} or (
        seconds_left is not None and seconds_left <= 900.0
    )


def _metric_exceeds(value: object, limit: float) -> bool:
    parsed = _optional_float(value)
    return parsed is not None and parsed > limit


def _optional_delta(value: object, baseline: object) -> float | None:
    parsed = _optional_float(value)
    base = _optional_float(baseline)
    if parsed is None or base is None:
        return None
    return round(parsed - base, 8)


def _guardrail_verdict_rank(verdict: str) -> int:
    return {
        "keep": 0,
        "watch": 1,
        "park": 2,
        "kill": 3,
    }.get(verdict, 4)


def _classify_loss(
    *,
    row: dict[str, str],
    pnl: float,
    signal: dict[str, Any],
    opportunity: dict[str, str],
    edge_decay: dict[str, Any],
    trade: dict[str, Any],
    prediction: dict[str, Any],
) -> str:
    metadata = signal.get("metadata") if isinstance(signal.get("metadata"), dict) else {}
    if not signal and not opportunity:
        return "missing_signal_context"
    rejected_sources = metadata.get("reference_rejected_sources")
    max_deviation = _optional_float(metadata.get("reference_max_deviation_bps"))
    if (isinstance(rejected_sources, list) and rejected_sources) or (
        max_deviation is not None and max_deviation > 50.0
    ):
        return "reference_divergence"
    min_delta = _optional_float(edge_decay.get("min_delta_net_edge"))
    min_current = _optional_float(edge_decay.get("min_current_net_edge"))
    if (min_delta is not None and min_delta <= -0.05) or (
        min_current is not None and min_current <= 0.0
    ):
        return "edge_decayed_or_stale_book"
    side = str(row.get("side") or signal.get("side", "")).upper()
    visible_ask = None
    if side == "BUY_YES":
        visible_ask = _optional_float(opportunity.get("yes_ask") or metadata.get("yes_ask"))
    elif side == "BUY_NO":
        visible_ask = _optional_float(opportunity.get("no_ask") or metadata.get("no_ask"))
    avg_price = _optional_float(trade.get("avg_price"))
    if visible_ask is not None and avg_price is not None and avg_price - visible_ask > 0.02:
        return "adverse_fill_slippage"
    seconds_left = _optional_float(
        metadata.get("time_to_expiry_seconds")
        or metadata.get("seconds_left")
        or opportunity.get("time_to_expiry")
    )
    edge_type = str(signal.get("edge_type") or opportunity.get("edge_type", "")).upper()
    if edge_type in {"LATE_EXPIRY", "SHORT_EXPIRY"} or (
        seconds_left is not None and seconds_left <= 900
    ):
        return "late_expiry_reversal"
    probability = _optional_float(prediction.get("probability"))
    net_edge = _optional_float(signal.get("net_edge") or opportunity.get("net_edge"))
    if probability is not None and probability >= 0.75:
        return "model_overconfidence"
    if net_edge is not None and net_edge >= 0.15 and pnl < 0:
        return "high_edge_loss"
    return "unclassified_loss"


def _edge_decay_summary(rows: list[dict[str, str]]) -> dict[str, Any]:
    deltas = [_float(row.get("delta_net_edge")) for row in rows]
    current_edges = [_float(row.get("current_net_edge")) for row in rows]
    return {
        "count": len(rows),
        "avg_delta_net_edge": _avg(deltas),
        "min_delta_net_edge": min(deltas) if deltas else None,
        "avg_current_net_edge": _avg(current_edges),
        "current_edge_non_positive_count": len([value for value in current_edges if value <= 0]),
        "by_underlying_edge_side": _aggregate_edge_decay(rows),
    }


def _aggregate_edge_decay(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[
            (
                str(row.get("underlying", "")).upper(),
                str(row.get("edge_type", "")),
                str(row.get("side", "")),
            )
        ].append(row)
    output: list[dict[str, Any]] = []
    for (underlying, edge_type, side), items in groups.items():
        deltas = [_float(row.get("delta_net_edge")) for row in items]
        currents = [_float(row.get("current_net_edge")) for row in items]
        output.append(
            {
                "underlying": underlying,
                "edge_type": edge_type,
                "side": side,
                "count": len(items),
                "avg_delta_net_edge": _avg(deltas),
                "min_delta_net_edge": min(deltas) if deltas else None,
                "avg_current_net_edge": _avg(currents),
            }
        )
    return sorted(output, key=lambda item: (float(item.get("avg_delta_net_edge") or 0.0), str(item)))


def _latency_summary(rows: list[dict[str, str]]) -> dict[str, Any]:
    totals = [_float(row.get("total_ms")) for row in rows]
    execution = [_float(row.get("execution_ms")) for row in rows]
    errors = [row for row in rows if str(row.get("error", "")).strip()]
    return {
        "count": len(rows),
        "avg_total_ms": _avg(totals),
        "p95_total_ms": _percentile(totals, 0.95),
        "avg_execution_ms": _avg(execution),
        "error_count": len(errors),
        "modes": _counter_rows(Counter(str(row.get("mode", "unknown")) for row in rows), "mode"),
    }


def _short_expiry_summary(rows: list[dict[str, str]]) -> dict[str, Any]:
    best_edges = [_float(row.get("best_net_edge")) for row in rows]
    reasons = Counter(str(row.get("reason", "unknown")) for row in rows)
    actionable = [
        row
        for row in rows
        if str(row.get("best_side", "")).strip()
        and _float(row.get("best_net_edge")) > 0.0
        and "warming" not in str(row.get("reason", "")).lower()
    ]
    return {
        "count": len(rows),
        "actionable_count": len(actionable),
        "avg_best_net_edge": _avg(best_edges),
        "max_best_net_edge": max(best_edges) if best_edges else None,
        "reasons": _counter_rows(reasons, "reason"),
    }


def _infer_nautilus_shadow_dir(
    profile_logs_dir: Path,
    explicit: str | Path | None,
) -> Path:
    if explicit is not None:
        return Path(explicit)
    sibling = profile_logs_dir.parent / "hip4_nautilus_shadow"
    if sibling.exists():
        return sibling
    return Path(DEFAULT_NAUTILUS_SHADOW_LOGS)


def _nautilus_shadow_summary(
    shadow_dir: Path,
    settlement_rows: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    data_quality_path = shadow_dir / "data_quality.csv"
    status_path = shadow_dir / "status.json"
    rows = _read_csv(data_quality_path)
    if not data_quality_path.exists():
        return {
            "status": "nautilus_shadow_missing",
            "logs_dir": str(shadow_dir),
            "data_quality_path": str(data_quality_path),
            "status_path": str(status_path),
            "row_count": 0,
            "matched_settlement_count": 0,
            "market_count": 0,
            "decision_time": _decision_time_quality_summary(decisions, []),
        }

    quality_index = _latest_quality_by_market(rows)
    matched = _settlements_with_quality(settlement_rows, quality_index)
    low_quality = [row for row in matched if _quality_reject_reason(row)]
    high_quality = [row for row in matched if not _quality_reject_reason(row)]
    quality_scores = [_float(row.get("quality_score")) for row in rows if row.get("quality_score")]
    max_ages = [_float(row.get("max_book_age_ms")) for row in rows if row.get("max_book_age_ms")]
    skews = [_float(row.get("book_pair_skew_ms")) for row in rows if row.get("book_pair_skew_ms")]
    status = "ok" if rows else "partial"
    if not matched and settlement_rows:
        status = "partial"

    return {
        "status": status,
        "logs_dir": str(shadow_dir),
        "data_quality_path": str(data_quality_path),
        "status_path": str(status_path),
        "row_count": len(rows),
        "matched_settlement_count": len(matched),
        "market_count": len(quality_index),
        "avg_quality_score": _avg(quality_scores),
        "avg_max_book_age_ms": _avg(max_ages),
        "avg_book_pair_skew_ms": _avg(skews),
        "low_quality_settlements": {
            "count": len(low_quality),
            "metrics": _guardrail_metrics(low_quality),
            "reasons": _counter_rows(
                Counter(
                    reason
                    for row in low_quality
                    for reason in [_quality_reject_reason(row)]
                    if reason
                ),
                "reason",
            ),
        },
        "high_quality_settlements": {
            "count": len(high_quality),
            "metrics": _guardrail_metrics(high_quality),
        },
        "buckets": {
            "by_quality_score": _aggregate_quality_bucket(
                matched,
                lambda row: _quality_score_bucket(row.get("_nautilus_quality", {})),
            ),
            "by_max_book_age_ms": _aggregate_quality_bucket(
                matched,
                lambda row: _age_bucket(
                    _optional_float(
                        _quality_payload(row).get("max_book_age_ms")
                    )
                ),
            ),
            "by_book_pair_skew_ms": _aggregate_quality_bucket(
                matched,
                lambda row: _skew_bucket(
                    _optional_float(
                        _quality_payload(row).get("book_pair_skew_ms")
                    )
                ),
            ),
        },
        "quality_row_buckets": {
            "by_quality_score": _aggregate_quality_rows(
                rows,
                lambda row: _quality_score_bucket(row),
            ),
            "by_max_book_age_ms": _aggregate_quality_rows(
                rows,
                lambda row: _age_bucket(_optional_float(row.get("max_book_age_ms"))),
            ),
            "by_book_pair_skew_ms": _aggregate_quality_rows(
                rows,
                lambda row: _skew_bucket(_optional_float(row.get("book_pair_skew_ms"))),
            ),
        },
        "decision_time": _decision_time_quality_summary(decisions, rows),
    }


def _latest_quality_by_market(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    latest: dict[str, dict[str, str]] = {}
    for row in sorted(rows, key=lambda item: str(item.get("ts", ""))):
        market_id = str(row.get("market_id", "")).strip()
        if market_id:
            latest[market_id] = row
    return latest


def _settlements_with_quality(
    settlement_rows: list[dict[str, Any]],
    quality_index: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in settlement_rows:
        quality = quality_index.get(str(row.get("market_id", "")).strip())
        if not quality:
            continue
        enriched = dict(row)
        enriched["_nautilus_quality"] = quality
        output.append(enriched)
    return output


def _decision_time_quality_summary(
    decisions: list[dict[str, Any]],
    quality_rows: list[dict[str, str]],
    *,
    max_age_seconds: float = DEFAULT_NAUTILUS_DECISION_JOIN_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    decision_rows = [_normalize_decision_for_quality_join(row) for row in decisions]
    decision_rows = [row for row in decision_rows if row is not None]
    quality_index = _quality_rows_by_market(quality_rows)
    matched: list[dict[str, Any]] = []
    unmatched_reasons: Counter[str] = Counter()

    for decision in decision_rows:
        decision_ts = decision.get("_ts")
        market_id = str(decision.get("market_id", ""))
        if not isinstance(decision_ts, datetime):
            unmatched_reasons["decision_ts_missing"] += 1
            continue
        candidates = quality_index.get(market_id, [])
        if not candidates:
            unmatched_reasons["no_market_quality"] += 1
            continue
        match = _latest_quality_before_decision(candidates, decision_ts)
        if match is None:
            unmatched_reasons["no_prior_quality"] += 1
            continue
        quality_ts, quality = match
        age_seconds = (decision_ts - quality_ts).total_seconds()
        if age_seconds < 0:
            unmatched_reasons["future_quality_only"] += 1
            continue
        if age_seconds > max_age_seconds:
            unmatched_reasons["quality_too_old"] += 1
            continue
        quality_reject = _quality_reject_reason({"_nautilus_quality": quality})
        matched.append(
            {
                **decision,
                "_nautilus_quality": quality,
                "match_age_seconds": round(age_seconds, 3),
                "quality_reject_reason": quality_reject,
            }
        )

    approved = [row for row in matched if bool(row.get("approved"))]
    rejected = [row for row in matched if not bool(row.get("approved"))]
    would_block = [row for row in matched if row.get("quality_reject_reason")]
    would_block_approved = [row for row in approved if row.get("quality_reject_reason")]
    return {
        "max_match_age_seconds": max_age_seconds,
        "decision_count": len(decision_rows),
        "matched_decision_count": len(matched),
        "unmatched_decision_count": max(len(decision_rows) - len(matched), 0),
        "unmatched_reasons": _counter_rows(unmatched_reasons, "reason"),
        "approved_count": len(approved),
        "rejected_count": len(rejected),
        "would_block_count": len(would_block),
        "would_block_approved_count": len(would_block_approved),
        "avg_match_age_seconds": _avg([float(row["match_age_seconds"]) for row in matched]),
        "avg_quality_score": _avg(
            [
                _float(_quality_payload(row).get("quality_score"))
                for row in matched
                if _quality_payload(row).get("quality_score")
            ]
        ),
        "by_approval": [
            {"approved": True, **_decision_quality_join_metrics(approved)},
            {"approved": False, **_decision_quality_join_metrics(rejected)},
        ],
        "would_block_reasons": _counter_rows(
            Counter(str(row.get("quality_reject_reason")) for row in would_block),
            "reason",
        ),
        "rejected_reasons": _counter_rows(
            Counter(str(row.get("decision_reason") or "unknown") for row in rejected),
            "reason",
        ),
        "buckets": {
            "by_quality_score": _aggregate_decision_quality_bucket(
                matched,
                lambda row: _quality_score_bucket(row.get("_nautilus_quality", {})),
            ),
            "by_max_book_age_ms": _aggregate_decision_quality_bucket(
                matched,
                lambda row: _age_bucket(
                    _optional_float(_quality_payload(row).get("max_book_age_ms"))
                ),
            ),
            "by_book_pair_skew_ms": _aggregate_decision_quality_bucket(
                matched,
                lambda row: _skew_bucket(
                    _optional_float(_quality_payload(row).get("book_pair_skew_ms"))
                ),
            ),
            "by_reference_divergence_bps": _aggregate_decision_quality_bucket(
                matched,
                lambda row: _divergence_bucket(
                    _optional_float(_quality_payload(row).get("reference_divergence_bps"))
                ),
            ),
        },
    }


def _normalize_decision_for_quality_join(payload: dict[str, Any]) -> dict[str, Any] | None:
    signal = payload.get("signal")
    if not isinstance(signal, dict):
        return None
    market_id = str(signal.get("market_id") or "").strip()
    if not market_id:
        return None
    decision = payload.get("supervisor_decision")
    decision_payload = decision if isinstance(decision, dict) else {}
    metadata = signal.get("metadata") if isinstance(signal.get("metadata"), dict) else {}
    return {
        "_ts": _parse_ts(payload.get("ts")),
        "ts": payload.get("ts"),
        "market_id": market_id,
        "underlying": str(signal.get("underlying") or "").upper(),
        "edge_type": str(signal.get("edge_type") or "unknown"),
        "side": str(signal.get("side") or "unknown"),
        "approved": bool(decision_payload.get("approved")),
        "decision_reason": str(decision_payload.get("reason") or "unknown"),
        "execution_mode": str(decision_payload.get("execution_mode") or "unknown"),
        "net_edge": _first_optional_float(signal.get("net_edge"), metadata.get("net_edge")),
        "confidence": _first_optional_float(signal.get("confidence"), metadata.get("probability_confidence")),
    }


def _quality_rows_by_market(
    rows: list[dict[str, str]],
) -> dict[str, list[tuple[datetime, dict[str, str]]]]:
    grouped: dict[str, list[tuple[datetime, dict[str, str]]]] = defaultdict(list)
    for row in rows:
        market_id = str(row.get("market_id") or "").strip()
        ts = _parse_ts(row.get("ts"))
        if market_id and ts is not None:
            grouped[market_id].append((ts, row))
    return {
        market_id: sorted(items, key=lambda item: item[0])
        for market_id, items in grouped.items()
    }


def _latest_quality_before_decision(
    candidates: list[tuple[datetime, dict[str, str]]],
    decision_ts: datetime,
) -> tuple[datetime, dict[str, str]] | None:
    latest: tuple[datetime, dict[str, str]] | None = None
    for item in candidates:
        if item[0] <= decision_ts:
            latest = item
        else:
            break
    return latest


def _aggregate_decision_quality_bucket(
    rows: list[dict[str, Any]],
    bucket_fn: Any,
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(bucket_fn(row))].append(row)
    output: list[dict[str, Any]] = []
    for bucket, items in groups.items():
        output.append({"bucket": bucket, **_decision_quality_join_metrics(items)})
    return sorted(output, key=lambda item: (str(item.get("bucket")), -int(item.get("count", 0))))


def _decision_quality_join_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    approved = [row for row in rows if bool(row.get("approved"))]
    rejected = [row for row in rows if not bool(row.get("approved"))]
    would_block = [row for row in rows if row.get("quality_reject_reason")]
    qualities = [_quality_payload(row) for row in rows]
    return {
        "count": len(rows),
        "approved_count": len(approved),
        "rejected_count": len(rejected),
        "approval_rate": _safe_div(len(approved), len(rows)),
        "would_block_count": len(would_block),
        "would_block_approved_count": len(
            [row for row in approved if row.get("quality_reject_reason")]
        ),
        "tradable_rate": _avg(
            [1.0 if _boolish(quality.get("tradable_window")) else 0.0 for quality in qualities]
        ),
        "avg_match_age_seconds": _avg(
            [float(row["match_age_seconds"]) for row in rows if row.get("match_age_seconds") is not None]
        ),
        "avg_quality_score": _avg(
            [_float(quality.get("quality_score")) for quality in qualities if quality.get("quality_score")]
        ),
        "avg_max_book_age_ms": _avg(
            [_float(quality.get("max_book_age_ms")) for quality in qualities if quality.get("max_book_age_ms")]
        ),
        "avg_book_pair_skew_ms": _avg(
            [
                _float(quality.get("book_pair_skew_ms"))
                for quality in qualities
                if quality.get("book_pair_skew_ms")
            ]
        ),
        "avg_reference_divergence_bps": _avg(
            [
                _float(quality.get("reference_divergence_bps"))
                for quality in qualities
                if quality.get("reference_divergence_bps")
            ]
        ),
    }


def _aggregate_quality_bucket(
    rows: list[dict[str, Any]],
    bucket_fn: Any,
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(bucket_fn(row))].append(row)
    output: list[dict[str, Any]] = []
    for bucket, items in groups.items():
        output.append({"bucket": bucket, **_guardrail_metrics(items)})
    return sorted(output, key=lambda item: (str(item.get("bucket")), -int(item.get("count", 0))))


def _aggregate_quality_rows(
    rows: list[dict[str, str]],
    bucket_fn: Any,
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[str(bucket_fn(row))].append(row)
    output: list[dict[str, Any]] = []
    for bucket, items in groups.items():
        output.append(
            {
                "bucket": bucket,
                "count": len(items),
                "tradable_rate": _avg([1.0 if _boolish(item.get("tradable_window")) else 0.0 for item in items]),
                "avg_quality_score": _avg(
                    [_float(item.get("quality_score")) for item in items if item.get("quality_score")]
                ),
                "avg_max_book_age_ms": _avg(
                    [_float(item.get("max_book_age_ms")) for item in items if item.get("max_book_age_ms")]
                ),
                "avg_book_pair_skew_ms": _avg(
                    [_float(item.get("book_pair_skew_ms")) for item in items if item.get("book_pair_skew_ms")]
                ),
            }
        )
    return sorted(output, key=lambda item: (str(item.get("bucket")), -int(item.get("count", 0))))


def _quality_score_bucket(quality: object) -> str:
    payload = quality if isinstance(quality, dict) else {}
    score = _optional_float(payload.get("quality_score"))
    if score is None:
        return "missing"
    if score < 0.4:
        return "0.0-0.4"
    if score < 0.6:
        return "0.4-0.6"
    if score < 0.8:
        return "0.6-0.8"
    return "0.8-1.0"


def _age_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value <= 250:
        return "<=250ms"
    if value <= 1000:
        return "250-1000ms"
    if value <= 3000:
        return "1000-3000ms"
    return ">3000ms"


def _skew_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value <= 100:
        return "<=100ms"
    if value <= 250:
        return "100-250ms"
    if value <= 1000:
        return "250-1000ms"
    return ">1000ms"


def _divergence_bucket(value: float | None) -> str:
    if value is None:
        return "missing"
    if value <= 1:
        return "<=1bp"
    if value <= 10:
        return "1-10bps"
    if value <= 50:
        return "10-50bps"
    return ">50bps"


def _quality_reject_reason(row: dict[str, Any]) -> str | None:
    quality = _quality_payload(row)
    if _boolish(quality.get("empty_book")):
        return "empty_book"
    if _boolish(quality.get("crossed_book")):
        return "crossed_book"
    if not _boolish(quality.get("tradable_window"), default=True):
        return "not_tradable_window"
    score = _optional_float(quality.get("quality_score"))
    max_age = _optional_float(quality.get("max_book_age_ms"))
    skew = _optional_float(quality.get("book_pair_skew_ms"))
    divergence = _optional_float(quality.get("reference_divergence_bps"))
    if score is not None and score < 0.60:
        return "quality_score_lt_0_60"
    if max_age is not None and max_age > 3000:
        return "max_book_age_gt_3000ms"
    if skew is not None and skew > 1000:
        return "book_pair_skew_gt_1000ms"
    if divergence is not None and divergence > 50:
        return "reference_divergence_gt_50bps"
    return None


def _quality_payload(row: dict[str, Any]) -> dict[str, Any]:
    quality = row.get("_nautilus_quality")
    return quality if isinstance(quality, dict) else {}


def _boolish(value: object, *, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _market_observation_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_class: Counter[str] = Counter()
    by_support: Counter[str] = Counter()
    by_reason: Counter[str] = Counter()
    by_underlying: Counter[str] = Counter()
    by_class_support: Counter[tuple[str, str]] = Counter()
    books_by_class_support: Counter[tuple[str, str]] = Counter()
    book_spreads: list[float] = []
    examples: list[dict[str, Any]] = []

    for row in rows:
        class_name = str(row.get("class_name") or "unknown")
        support = str(row.get("support_status") or "unknown")
        reason = str(row.get("support_reason") or "unspecified")
        underlying = str(row.get("underlying") or "").upper()
        key = (class_name, support)
        by_class[class_name] += 1
        by_support[support] += 1
        by_reason[reason] += 1
        by_class_support[key] += 1
        if underlying:
            by_underlying[underlying] += 1
        if _observation_has_book(row):
            books_by_class_support[key] += 1
            book_spreads.extend(_observation_book_spreads(row))
        if len(examples) < 20:
            examples.append(_market_observation_example(row))

    price_bucket_rows = [
        row for row in rows if str(row.get("class_name") or "") == "priceBucket"
    ]
    named_rows = [
        row for row in rows if str(row.get("class_name") or "") == "namedOutcome"
    ]
    return {
        "count": len(rows),
        "books_logged_count": len([row for row in rows if _observation_has_book(row)]),
        "avg_book_spread": _avg(book_spreads),
        "by_class": _counter_rows(by_class, "class_name"),
        "by_support_status": _counter_rows(by_support, "support_status"),
        "support_reasons": _counter_rows(by_reason, "support_reason"),
        "by_underlying": _counter_rows(by_underlying, "underlying"),
        "by_class_support": [
            {
                "class_name": class_name,
                "support_status": support,
                "count": count,
                "books_logged_count": books_by_class_support.get((class_name, support), 0),
            }
            for (class_name, support), count in sorted(
                by_class_support.items(),
                key=lambda item: (-item[1], item[0][0], item[0][1]),
            )
        ],
        "price_bucket": _price_bucket_observation_summary(price_bucket_rows),
        "named_outcome": _named_outcome_observation_summary(named_rows),
        "examples": examples,
    }


def _price_bucket_observation_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    paper_supported = [
        row for row in rows if str(row.get("support_status") or "") == "paper_supported"
    ]
    complete = [
        row
        for row in rows
        if _optional_float(row.get("bucket_lower")) is not None
        and _optional_float(row.get("bucket_upper")) is not None
    ]
    widths = [
        float(row.get("bucket_upper")) - float(row.get("bucket_lower"))
        for row in complete
        if _optional_float(row.get("bucket_lower")) is not None
        and _optional_float(row.get("bucket_upper")) is not None
    ]
    by_underlying = Counter(
        str(row.get("underlying") or "unknown").upper() for row in rows
    )
    return {
        "count": len(rows),
        "paper_supported_count": len(paper_supported),
        "complete_bucket_count": len(complete),
        "incomplete_count": max(len(rows) - len(complete), 0),
        "avg_bucket_width": _avg(widths),
        "by_underlying": _counter_rows(by_underlying, "underlying"),
        "examples": [_market_observation_example(row) for row in rows[:10]],
    }


def _named_outcome_observation_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    names = Counter(str(row.get("name") or "unnamed") for row in rows)
    return {
        "count": len(rows),
        "names": _counter_rows(names, "name"),
        "examples": [_market_observation_example(row) for row in rows[:10]],
    }


def _market_observation_example(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": row.get("ts"),
        "class_name": row.get("class_name"),
        "support_status": row.get("support_status"),
        "support_reason": row.get("support_reason"),
        "market_id": row.get("market_id"),
        "name": row.get("name"),
        "underlying": row.get("underlying"),
        "expiry_iso": row.get("expiry_iso"),
        "coins": row.get("coins"),
        "thresholds": row.get("thresholds"),
        "bucket_lower": row.get("bucket_lower"),
        "bucket_upper": row.get("bucket_upper"),
        "bucket_index": row.get("bucket_index"),
        "book": _market_observation_book_example(row),
    }


def _market_observation_book_example(row: dict[str, Any]) -> dict[str, Any]:
    books = row.get("books")
    if not isinstance(books, dict):
        return {}
    output: dict[str, Any] = {}
    for side in ("yes", "no"):
        book = books.get(side)
        if not isinstance(book, dict):
            continue
        output[side] = {
            "coin": book.get("coin"),
            "bid": book.get("bid"),
            "ask": book.get("ask"),
            "bid_depth_usdc": book.get("bid_depth_usdc"),
            "ask_depth_usdc": book.get("ask_depth_usdc"),
            "error": book.get("error"),
        }
    return output


def _observation_has_book(row: dict[str, Any]) -> bool:
    books = row.get("books")
    if not isinstance(books, dict) or not books:
        return False
    return any(isinstance(books.get(side), dict) for side in ("yes", "no"))


def _observation_book_spreads(row: dict[str, Any]) -> list[float]:
    books = row.get("books")
    if not isinstance(books, dict):
        return []
    spreads: list[float] = []
    for side in ("yes", "no"):
        book = books.get(side)
        if not isinstance(book, dict):
            continue
        bid = _optional_float(book.get("bid"))
        ask = _optional_float(book.get("ask"))
        if bid is not None and ask is not None and ask >= bid:
            spreads.append(round(ask - bid, 8))
    return spreads


def _cross_profile_summary(profiles: list[dict[str, Any]]) -> dict[str, Any]:
    names = [str(profile.get("profile")) for profile in profiles]
    rows_by_profile: dict[str, list[dict[str, Any]]] = {}
    all_keys: set[tuple[str, str, str]] = set()
    for profile in profiles:
        name = str(profile.get("profile"))
        rows = profile.get("opportunities", {}).get("by_underlying_edge_side", [])
        rows_by_profile[name] = rows
        for row in rows:
            all_keys.add(
                (
                    str(row.get("underlying", "")),
                    str(row.get("edge_type", "")),
                    str(row.get("side", "")),
                )
            )
    overlap: list[dict[str, Any]] = []
    for underlying, edge_type, side in sorted(all_keys):
        counts: dict[str, int] = {}
        avg_edges: dict[str, float | None] = {}
        for name in names:
            match = next(
                (
                    row
                    for row in rows_by_profile.get(name, [])
                    if str(row.get("underlying")) == underlying
                    and str(row.get("edge_type")) == edge_type
                    and str(row.get("side")) == side
                ),
                None,
            )
            counts[name] = int(match.get("count", 0)) if match else 0
            avg_edges[name] = match.get("avg_net_edge") if match else None
        overlap.append(
            {
                "underlying": underlying,
                "edge_type": edge_type,
                "side": side,
                "counts": counts,
                "avg_net_edges": avg_edges,
                "total_count": sum(counts.values()),
            }
        )
    return {
        "profiles": names,
        "opportunity_overlap": sorted(
            overlap,
            key=lambda item: (-int(item["total_count"]), str(item)),
        ),
    }


def _readiness(profiles: list[dict[str, Any]], limits: ReviewThresholds) -> dict[str, Any]:
    by_name = {str(profile.get("profile")): profile for profile in profiles}
    reasons: list[str] = []
    testnet = by_name.get("testnet")
    mainnet_paper = by_name.get("mainnet_paper")
    mainnet = by_name.get("mainnet")
    execution_profile = None
    for candidate in (mainnet_paper, testnet):
        if candidate is not None and _profile_has_execution_data(candidate):
            execution_profile = candidate
            break
    if execution_profile is None:
        execution_profile = mainnet_paper or testnet

    if execution_profile is None:
        reasons.append("profil mainnet paper/testnet absent de la review")
    else:
        reasons.extend(execution_profile.get("readiness", {}).get("reasons", []))
    if mainnet is None:
        reasons.append("profil mainnet observer absent de la review")
    else:
        opportunities = int(mainnet.get("row_counts", {}).get("opportunities", 0))
        if opportunities < limits.min_mainnet_opportunities:
            reasons.append(
                "mainnet observer insuffisant: "
                f"{opportunities}/{limits.min_mainnet_opportunities} opportunites"
            )

    status = "candidate_for_next_review" if not reasons else "collect_more_data"
    recommendation = (
        "continuer mainnet paper et preparer une calibration detaillee"
        if status == "candidate_for_next_review"
        else "continuer la collecte mainnet paper/mainnet observer avant toute promotion"
    )
    return {
        "status": status,
        "recommendation": recommendation,
        "reasons": reasons,
    }


def _profile_has_execution_data(profile: dict[str, Any]) -> bool:
    row_counts = profile.get("row_counts", {})
    if not isinstance(row_counts, dict):
        return False
    return any(
        int(row_counts.get(key, 0) or 0) > 0
        for key in ("opportunities", "trades", "settlements", "execution_results")
    )


def _profile_readiness(
    *,
    profile: str,
    row_counts: dict[str, int],
    settlement_summary: dict[str, Any],
    calibration: dict[str, Any],
    trade_quality: dict[str, Any],
    limits: ReviewThresholds,
) -> dict[str, Any]:
    reasons: list[str] = []
    if profile in {"testnet", "mainnet_paper"}:
        label = "mainnet paper" if profile == "mainnet_paper" else "testnet"
        settlements = int(settlement_summary.get("count", 0))
        unique_markets = int(settlement_summary.get("unique_markets", 0))
        active_days = int(settlement_summary.get("active_days", 0))
        if settlements < limits.min_testnet_settlements:
            reasons.append(f"settlements {label} insuffisants: {settlements}/{limits.min_testnet_settlements}")
        if unique_markets < limits.min_testnet_markets:
            reasons.append(f"expiries/marches {label} insuffisants: {unique_markets}/{limits.min_testnet_markets}")
        if active_days < limits.min_testnet_days:
            reasons.append(f"jours {label} insuffisants: {active_days}/{limits.min_testnet_days}")
        profit_factor = _optional_float(settlement_summary.get("profit_factor"))
        if profit_factor is None or profit_factor < limits.min_profit_factor:
            reasons.append(
                f"profit factor {label} insuffisant: "
                f"{_fmt_num(profit_factor)}/{limits.min_profit_factor:.2f}"
            )
        brier = _optional_float(calibration.get("brier_score"))
        samples = int(calibration.get("count", 0))
        if samples < limits.min_calibration_samples:
            reasons.append(f"samples calibration insuffisants: {samples}/{limits.min_calibration_samples}")
        if brier is None or brier > limits.max_brier_score:
            reasons.append(f"Brier score insuffisant: {_fmt_num(brier)} <= {limits.max_brier_score:.2f} attendu")
        avg_slippage = _optional_float(trade_quality.get("avg_slippage_vs_visible_ask"))
        if avg_slippage is not None and avg_slippage > limits.max_avg_fill_slippage:
            reasons.append(
                "slippage moyen trop eleve: "
                f"{avg_slippage:.4f} > {limits.max_avg_fill_slippage:.4f}"
            )
    elif profile == "mainnet":
        opportunities = int(row_counts.get("opportunities", 0))
        if opportunities < limits.min_mainnet_opportunities:
            reasons.append(
                f"opportunites mainnet observer insuffisantes: {opportunities}/{limits.min_mainnet_opportunities}"
            )
    elif row_counts.get("opportunities", 0) == 0 and row_counts.get("settlements", 0) == 0:
        reasons.append("aucune donnee exploitable")

    return {
        "status": "ok" if not reasons else "collect_more_data",
        "reasons": reasons,
    }


def _predicted_win_probability(
    signal: dict[str, Any],
    opportunity: dict[str, str],
) -> dict[str, Any]:
    metadata = signal.get("metadata") if isinstance(signal.get("metadata"), dict) else {}
    side = str(signal.get("side") or opportunity.get("side", "")).upper()
    probability_yes = _first_optional_float(
        metadata.get("probability_yes"),
        metadata.get("short_probability_yes"),
        metadata.get("model_probability_yes"),
    )
    if probability_yes is not None:
        if side == "BUY_YES":
            return {"probability": round(_clamp(probability_yes, 0.001, 0.999), 8), "source": "probability_yes"}
        if side == "BUY_NO":
            return {
                "probability": round(_clamp(1.0 - probability_yes, 0.001, 0.999), 8),
                "source": "probability_yes",
            }
    if side == "BUY_BOTH":
        return {"probability": None, "source": "not_applicable_buy_both"}
    confidence = _first_optional_float(signal.get("confidence"), opportunity.get("confidence"))
    if confidence is not None:
        return {"probability": round(_clamp(confidence, 0.001, 0.999), 8), "source": "confidence_proxy"}
    return {"probability": None, "source": "missing"}


def _settlement_pnl(row: dict[str, str]) -> float:
    return _first_optional_float(
        row.get("net_pnl_usdc"),
        row.get("pnl_usdc"),
        row.get("estimated_pnl_usdc"),
        row.get("gross_pnl_usdc"),
    ) or 0.0


def _settlement_is_win(row: dict[str, str], pnl: float) -> bool:
    raw = str(row.get("is_win", "")).strip().lower()
    if raw in {"1", "true", "yes", "y"}:
        return True
    if raw in {"0", "false", "no", "n"}:
        return False
    return pnl >= 0.0


def _max_drawdown(pnls: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return round(abs(max_dd), 8)


def _window(rows: list[dict[str, Any]]) -> dict[str, str | None]:
    timestamps = sorted(str(row.get("ts", "")) for row in rows if str(row.get("ts", "")).strip())
    return {
        "start": timestamps[0] if timestamps else None,
        "end": timestamps[-1] if timestamps else None,
    }


def _group_key(row: dict[str, str], keys: list[str]) -> tuple[str, ...]:
    values: list[str] = []
    for key in keys:
        if key == "date":
            values.append(_date(row.get("ts")))
        elif key == "underlying":
            values.append(str(row.get(key, "")).upper())
        else:
            values.append(str(row.get(key, "unknown") or "unknown"))
    return tuple(values)


def _key(market_id: object, side: object) -> tuple[str, str]:
    return str(market_id or ""), str(side or "")


def _date(value: object) -> str:
    text = str(value or "")
    return text[:10] if len(text) >= 10 else "unknown"


def _parse_ts(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _counter_rows(counter: Counter[str], label: str) -> list[dict[str, Any]]:
    return [
        {label: key, "count": count}
        for key, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    ]


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 8) if values else None


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(max(math.ceil(len(ordered) * percentile) - 1, 0), len(ordered) - 1)
    return round(ordered[idx], 8)


def _safe_div(numerator: float, denominator: float) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 8)


def _float(value: object) -> float:
    parsed = _optional_float(value)
    return parsed if parsed is not None else 0.0


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(parsed) or math.isinf(parsed):
        return None
    return parsed


def _first_optional_float(*values: object) -> float | None:
    for value in values:
        parsed = _optional_float(value)
        if parsed is not None:
            return parsed
    return None


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _fmt_num(value: object) -> str:
    parsed = _optional_float(value)
    if parsed is None:
        return "n/a"
    return f"{parsed:.4f}"


def _fmt_window(window: dict[str, Any]) -> str:
    start = window.get("start")
    end = window.get("end")
    if not start and not end:
        return "n/a"
    return f"{str(start)[:10]} -> {str(end)[:10]}"
