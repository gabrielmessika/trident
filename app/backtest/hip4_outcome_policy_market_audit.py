from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_PAPER_LOGS = Path("server-data/hip4/logs/hip4_outcome_mainnet_paper")
DEFAULT_OBSERVER_LOGS = Path("server-data/hip4/logs/hip4_outcome_mainnet")
DEFAULT_OUTPUT_DIR = Path("server-data/hip4/replay_reports")
DEFAULT_FOCUS_POLICIES = (
    "prob_stop_full",
    "ev_plus_2pct_partial_runner",
    "hold_to_settlement",
)
DEFAULT_ENTRY_CUTOFFS = (
    "2026-06-02T00:00:00Z",
    "2026-06-05T00:00:00Z",
    "2026-06-10T00:00:00Z",
)
DEFAULT_TARGET_UNDERLYINGS = (
    "ETH",
    "SOL",
    "HYPE",
    "DOGE",
    "XRP",
    "SUI",
    "AVAX",
    "LINK",
    "ARB",
    "ADA",
    "BNB",
    "LTC",
    "AAVE",
    "NEAR",
    "ZRO",
)


def analyze_logs(
    *,
    paper_logs_dir: str | Path = DEFAULT_PAPER_LOGS,
    observer_logs_dir: str | Path = DEFAULT_OBSERVER_LOGS,
    entry_cutoffs: Iterable[str] = DEFAULT_ENTRY_CUTOFFS,
    target_underlyings: Iterable[str] = DEFAULT_TARGET_UNDERLYINGS,
) -> dict[str, Any]:
    paper_logs = Path(paper_logs_dir)
    observer_logs = Path(observer_logs_dir)
    targets = tuple(sorted({item.upper() for item in target_underlyings}))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "paper_logs_dir": str(paper_logs),
        "observer_logs_dir": str(observer_logs),
        "exit_policy_replay": analyze_exit_policy_replay(
            paper_logs,
            entry_cutoffs=tuple(entry_cutoffs),
        ),
        "market_universe_audit": {
            "target_underlyings": list(targets),
            "profiles": [
                audit_market_universe("mainnet_paper", paper_logs, targets),
                audit_market_universe("mainnet_observer", observer_logs, targets),
            ],
        },
    }


def analyze_exit_policy_replay(
    logs_dir: str | Path,
    *,
    entry_cutoffs: Iterable[str] = DEFAULT_ENTRY_CUTOFFS,
) -> dict[str, Any]:
    root = Path(logs_dir)
    trade_entries = _trade_entry_queues(root / "trades.csv")
    active_records = _active_settlement_records(root / "settlements.csv", deepcopy(trade_entries))
    active_exit_counts, active_exit_pnl = _active_exit_summary(root / "early_exits.csv")
    shadow_records, shadow_exit_counts = _shadow_policy_records(
        root / "shadow_exit_policies.csv",
        trade_entries,
    )

    all_records = active_records + shadow_records
    policies = sorted({str(row["policy"]) for row in all_records})
    active_summary = _policy_metric_row(
        "active_paper",
        [row for row in active_records if row["policy"] == "active_paper"],
        exit_count=sum(active_exit_counts.values()),
        source="active",
    )
    policy_summaries = [active_summary]
    for policy in policies:
        if policy == "active_paper":
            continue
        policy_summaries.append(
            _policy_metric_row(
                policy,
                [row for row in shadow_records if row["policy"] == policy],
                exit_count=shadow_exit_counts.get(policy, 0),
                source="shadow",
            )
        )

    baseline_pnl = _float(active_summary.get("net_pnl_usdc"))
    for row in policy_summaries:
        row["delta_vs_active_pnl_usdc"] = round(_float(row.get("net_pnl_usdc")) - baseline_pnl, 8)

    cutoff_summaries = []
    for cutoff in entry_cutoffs:
        cutoff_row = {"entry_cutoff": cutoff, "policies": []}
        for policy in ["active_paper", *DEFAULT_FOCUS_POLICIES]:
            records = [
                row
                for row in all_records
                if row["policy"] == policy
                and str(row.get("entry_ts") or row.get("ts") or "") >= cutoff
            ]
            exit_count = (
                sum(active_exit_counts.values())
                if policy == "active_paper"
                else shadow_exit_counts.get(policy, 0)
            )
            cutoff_row["policies"].append(
                _policy_metric_row(
                    policy,
                    records,
                    exit_count=exit_count,
                    source="active" if policy == "active_paper" else "shadow",
                )
            )
        cutoff_summaries.append(cutoff_row)

    return {
        "logs_dir": str(root),
        "settlement_count": len(active_records),
        "policy_summaries": sorted(
            policy_summaries,
            key=lambda row: (
                0 if row["policy"] in ("active_paper", *DEFAULT_FOCUS_POLICIES) else 1,
                -_float(row.get("net_pnl_usdc")),
                str(row["policy"]),
            ),
        ),
        "focus_policies": [
            row
            for row in policy_summaries
            if row["policy"] in ("active_paper", *DEFAULT_FOCUS_POLICIES)
        ],
        "entry_cutoff_summaries": cutoff_summaries,
        "active_exit_reasons": [
            {
                "reason": reason,
                "count": count,
                "realized_pnl_usdc": round(active_exit_pnl.get(reason, 0.0), 8),
            }
            for reason, count in active_exit_counts.most_common()
        ],
        "notes": _exit_policy_notes(policy_summaries),
    }


def audit_market_universe(
    profile: str,
    logs_dir: str | Path,
    target_underlyings: Iterable[str] = DEFAULT_TARGET_UNDERLYINGS,
) -> dict[str, Any]:
    root = Path(logs_dir)
    targets = {item.upper() for item in target_underlyings}
    opportunities = _opportunity_universe(root / "opportunities.csv", targets)
    decisions = _decision_universe(root / "decisions.jsonl", targets)
    observations = _price_binary_observation_universe(
        root / "market_observations.jsonl",
        targets,
    )
    non_btc_underlyings = sorted(
        set(opportunities["non_btc_underlyings"])
        | set(decisions["non_btc_underlyings"])
        | set(observations["non_btc_underlyings"])
    )
    tradable_non_btc = sorted(
        set(observations["trading_supported_non_btc_underlyings"])
        | set(opportunities["non_btc_underlyings"])
    )
    return {
        "profile": profile,
        "logs_dir": str(root),
        "opportunities": opportunities,
        "decisions": decisions,
        "price_binary_observations": observations,
        "non_btc_underlyings": non_btc_underlyings,
        "tradable_non_btc_underlyings": tradable_non_btc,
        "target_coverage": [
            {
                "underlying": target,
                "opportunities": opportunities["by_underlying"].get(target, {}).get("count", 0),
                "approved_decisions": decisions["approved_by_underlying"].get(target, 0),
                "price_binary_observations": observations["by_underlying"].get(target, {}).get(
                    "count", 0
                ),
                "trading_supported_observations": observations["by_underlying"]
                .get(target, {})
                .get("trading_supported_count", 0),
            }
            for target in sorted(targets)
        ],
        "conclusion": _market_universe_conclusion(
            non_btc_underlyings,
            tradable_non_btc,
            observations["non_btc_examples"],
        ),
    }


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# HIP-4 Exit Policy And Market Audit",
        "",
        f"- generated_at: `{payload.get('generated_at')}`",
        f"- paper_logs_dir: `{payload.get('paper_logs_dir')}`",
        f"- observer_logs_dir: `{payload.get('observer_logs_dir')}`",
        "",
    ]
    _render_exit_policy_section(lines, payload.get("exit_policy_replay", {}))
    _render_market_audit_section(lines, payload.get("market_universe_audit", {}))
    return "\n".join(lines).rstrip() + "\n"


def write_outputs(
    payload: dict[str, Any],
    *,
    output_json: str | Path,
    output_md: str | Path,
) -> None:
    json_path = Path(output_json)
    md_path = Path(output_md)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare HIP-4 paper exit policies and audit non-BTC priceBinary markets."
    )
    parser.add_argument("--paper-logs-dir", default=str(DEFAULT_PAPER_LOGS))
    parser.add_argument("--observer-logs-dir", default=str(DEFAULT_OBSERVER_LOGS))
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory used when --output-json/--output-md are omitted.",
    )
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--output-md", default=None)
    parser.add_argument(
        "--entry-cutoff",
        action="append",
        default=[],
        help="Entry timestamp cutoff for extra policy summaries. Can be repeated.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    cutoffs = tuple(args.entry_cutoff) if args.entry_cutoff else DEFAULT_ENTRY_CUTOFFS
    payload = analyze_logs(
        paper_logs_dir=args.paper_logs_dir,
        observer_logs_dir=args.observer_logs_dir,
        entry_cutoffs=cutoffs,
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(args.output_dir)
    output_json = args.output_json or output_dir / f"hip4_policy_market_audit_{stamp}.json"
    output_md = args.output_md or output_dir / f"hip4_policy_market_audit_{stamp}.md"
    write_outputs(payload, output_json=output_json, output_md=output_md)
    print(json.dumps({"output_json": str(output_json), "output_md": str(output_md)}, indent=2))


def _active_settlement_records(
    path: Path,
    trade_entries: dict[tuple[str, str], list[dict[str, str]]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in _iter_csv(path):
        key = _market_side_key(row)
        entry = _pop_entry(trade_entries, key)
        records.append(
            {
                "policy": "active_paper",
                "source": "active",
                "ts": row.get("ts", ""),
                "entry_ts": entry.get("ts") or row.get("ts", ""),
                "market_id": row.get("market_id", ""),
                "underlying": str(row.get("underlying") or entry.get("underlying") or "").upper(),
                "side": row.get("side", ""),
                "result": row.get("result", ""),
                "net_pnl_usdc": _settlement_pnl(row),
            }
        )
    return records


def _shadow_policy_records(
    path: Path,
    trade_entries: dict[tuple[str, str], list[dict[str, str]]],
) -> tuple[list[dict[str, Any]], Counter[str]]:
    records: list[dict[str, Any]] = []
    exit_counts: Counter[str] = Counter()
    policy_trade_entries: dict[str, dict[tuple[str, str], list[dict[str, str]]]] = {}
    for row in _iter_csv(path):
        policy = str(row.get("policy") or "unknown")
        event_type = str(row.get("event_type") or "")
        if event_type == "exit":
            exit_counts[policy] += 1
            continue
        if event_type != "settlement":
            continue
        if policy not in policy_trade_entries:
            policy_trade_entries[policy] = deepcopy(trade_entries)
        key = _market_side_key(row)
        entry = _pop_entry(policy_trade_entries[policy], key)
        records.append(
            {
                "policy": policy,
                "source": "shadow",
                "ts": row.get("ts", ""),
                "entry_ts": entry.get("ts") or row.get("ts", ""),
                "market_id": row.get("market_id", ""),
                "underlying": str(row.get("underlying") or entry.get("underlying") or "").upper(),
                "side": row.get("side", ""),
                "result": row.get("result", ""),
                "net_pnl_usdc": _first_float(
                    row.get("net_pnl_usdc"),
                    row.get("realized_pnl_usdc"),
                    row.get("gross_pnl_usdc"),
                ),
            }
        )
    return records, exit_counts


def _active_exit_summary(path: Path) -> tuple[Counter[str], Counter[str]]:
    counts: Counter[str] = Counter()
    pnl: Counter[str] = Counter()
    for row in _iter_csv(path):
        if str(row.get("action") or "") == "hold":
            continue
        if _float(row.get("exit_fraction")) <= 0.0:
            continue
        reason = str(row.get("reason") or "unknown")
        counts[reason] += 1
        pnl[reason] += _float(row.get("realized_pnl_usdc"))
    return counts, pnl


def _trade_entry_queues(path: Path) -> dict[tuple[str, str], list[dict[str, str]]]:
    queues: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in sorted(_iter_csv(path), key=lambda item: str(item.get("ts", ""))):
        key = _market_side_key(row)
        if key[0]:
            queues[key].append(row)
    return dict(queues)


def _policy_metric_row(
    policy: str,
    records: list[dict[str, Any]],
    *,
    exit_count: int,
    source: str,
) -> dict[str, Any]:
    pnls = [_float(row.get("net_pnl_usdc")) for row in records]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value <= 0]
    gross_profit = sum(value for value in pnls if value > 0)
    gross_loss = sum(value for value in pnls if value < 0)
    return {
        "policy": policy,
        "source": source,
        "settlement_count": len(records),
        "exit_event_count": exit_count,
        "unique_markets": len(
            {str(row.get("market_id") or "") for row in records if row.get("market_id")}
        ),
        "net_pnl_usdc": round(sum(pnls), 8),
        "gross_profit_usdc": round(gross_profit, 8),
        "gross_loss_usdc": round(gross_loss, 8),
        "profit_factor": _safe_div(gross_profit, abs(gross_loss)),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": _safe_div(len(wins), len(records)),
        "avg_pnl_usdc": _safe_div(sum(pnls), len(pnls)),
        "worst_pnl_usdc": min(pnls) if pnls else None,
        "best_pnl_usdc": max(pnls) if pnls else None,
    }


def _exit_policy_notes(policy_summaries: list[dict[str, Any]]) -> list[str]:
    by_policy = {str(row["policy"]): row for row in policy_summaries}
    notes: list[str] = []
    active = by_policy.get("active_paper")
    prob_stop = by_policy.get("prob_stop_full")
    partial = by_policy.get("ev_plus_2pct_partial_runner")
    hold = by_policy.get("hold_to_settlement")
    if active and prob_stop:
        delta = _float(prob_stop.get("net_pnl_usdc")) - _float(active.get("net_pnl_usdc"))
        notes.append(
            "prob_stop_full_delta_vs_active="
            f"{_fmt_num(delta)} USDC"
        )
    if partial and hold:
        delta = _float(partial.get("net_pnl_usdc")) - _float(hold.get("net_pnl_usdc"))
        notes.append(
            "partial_runner_delta_vs_hold="
            f"{_fmt_num(delta)} USDC"
        )
    notes.append(
        "shadow policies are paper counterfactuals; do not promote without more settlements"
    )
    return notes


def _opportunity_universe(path: Path, target_underlyings: set[str]) -> dict[str, Any]:
    by_underlying: dict[str, dict[str, Any]] = {}
    total = 0
    non_btc_examples: list[dict[str, Any]] = []
    for row in _iter_csv(path):
        total += 1
        underlying = str(row.get("underlying") or "").upper()
        if not underlying:
            continue
        bucket = by_underlying.setdefault(
            underlying,
            {
                "count": 0,
                "markets": set(),
                "edges": Counter(),
                "sides": Counter(),
                "max_net_edge": None,
            },
        )
        bucket["count"] += 1
        if row.get("market_id"):
            bucket["markets"].add(str(row["market_id"]))
        bucket["edges"][str(row.get("edge_type") or "unknown")] += 1
        bucket["sides"][str(row.get("side") or "unknown")] += 1
        net_edge = _optional_float(row.get("net_edge"))
        if net_edge is not None:
            current = bucket.get("max_net_edge")
            bucket["max_net_edge"] = net_edge if current is None else max(float(current), net_edge)
        if underlying != "BTC" and len(non_btc_examples) < 20:
            non_btc_examples.append(_opportunity_example(row))

    return {
        "count": total,
        "by_underlying": _finalize_universe_buckets(by_underlying),
        "underlyings": sorted(by_underlying),
        "non_btc_underlyings": sorted([key for key in by_underlying if key != "BTC"]),
        "target_underlyings_seen": sorted(set(by_underlying) & target_underlyings),
        "non_btc_examples": non_btc_examples,
    }


def _decision_universe(path: Path, target_underlyings: set[str]) -> dict[str, Any]:
    by_underlying: Counter[str] = Counter()
    approved_by_underlying: Counter[str] = Counter()
    rejected_reasons: Counter[str] = Counter()
    total = 0
    approved = 0
    for payload in _iter_jsonl(path):
        total += 1
        signal = payload.get("signal")
        decision = payload.get("supervisor_decision")
        if not isinstance(signal, dict):
            continue
        underlying = str(signal.get("underlying") or "").upper()
        if underlying:
            by_underlying[underlying] += 1
        if isinstance(decision, dict) and decision.get("approved"):
            approved += 1
            if underlying:
                approved_by_underlying[underlying] += 1
        elif isinstance(decision, dict):
            rejected_reasons[str(decision.get("reason") or "unknown")] += 1
    return {
        "count": total,
        "approved_count": approved,
        "by_underlying": dict(sorted(by_underlying.items())),
        "approved_by_underlying": dict(sorted(approved_by_underlying.items())),
        "non_btc_underlyings": sorted([key for key in by_underlying if key != "BTC"]),
        "target_underlyings_seen": sorted(set(by_underlying) & target_underlyings),
        "rejected_reasons": _counter_rows(rejected_reasons, "reason"),
    }


def _price_binary_observation_universe(path: Path, target_underlyings: set[str]) -> dict[str, Any]:
    total_jsonl = 0
    price_binary_count = 0
    by_underlying: dict[str, dict[str, Any]] = {}
    support_status: Counter[str] = Counter()
    support_reasons: Counter[str] = Counter()
    non_btc_examples: list[dict[str, Any]] = []
    for row in _iter_jsonl(path):
        total_jsonl += 1
        if str(row.get("class_name") or "") != "priceBinary":
            continue
        price_binary_count += 1
        underlying = str(row.get("underlying") or "").upper()
        support = str(row.get("support_status") or "unknown")
        reason = str(row.get("support_reason") or "unknown")
        support_status[support] += 1
        support_reasons[reason] += 1
        if not underlying:
            underlying = "UNKNOWN"
        bucket = by_underlying.setdefault(
            underlying,
            {
                "count": 0,
                "trading_supported_count": 0,
                "markets": set(),
                "periods": Counter(),
                "support_status": Counter(),
                "support_reasons": Counter(),
                "first_ts": None,
                "last_ts": None,
                "examples": [],
            },
        )
        bucket["count"] += 1
        if support == "trading_supported":
            bucket["trading_supported_count"] += 1
        if row.get("market_id"):
            bucket["markets"].add(str(row["market_id"]))
        if row.get("period"):
            bucket["periods"][str(row["period"])] += 1
        bucket["support_status"][support] += 1
        bucket["support_reasons"][reason] += 1
        ts = str(row.get("ts") or "")
        bucket["first_ts"] = ts if not bucket["first_ts"] else min(str(bucket["first_ts"]), ts)
        bucket["last_ts"] = ts if not bucket["last_ts"] else max(str(bucket["last_ts"]), ts)
        if len(bucket["examples"]) < 5:
            bucket["examples"].append(_observation_example(row))
        if underlying != "BTC" and len(non_btc_examples) < 20:
            non_btc_examples.append(_observation_example(row))

    finalized = _finalize_observation_buckets(by_underlying)
    non_btc = sorted([key for key in finalized if key not in {"BTC", "UNKNOWN"}])
    trading_supported_non_btc = sorted(
        [
            key
            for key, value in finalized.items()
            if key not in {"BTC", "UNKNOWN"} and int(value.get("trading_supported_count", 0)) > 0
        ]
    )
    return {
        "jsonl_rows": total_jsonl,
        "count": price_binary_count,
        "by_underlying": finalized,
        "underlyings": sorted(finalized),
        "non_btc_underlyings": non_btc,
        "trading_supported_non_btc_underlyings": trading_supported_non_btc,
        "target_underlyings_seen": sorted(set(finalized) & target_underlyings),
        "support_status": _counter_rows(support_status, "support_status"),
        "support_reasons": _counter_rows(support_reasons, "support_reason"),
        "non_btc_examples": non_btc_examples,
    }


def _finalize_universe_buckets(payload: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for underlying, row in sorted(payload.items()):
        output[underlying] = {
            "count": row["count"],
            "market_count": len(row["markets"]),
            "edges": _counter_rows(row["edges"], "edge_type"),
            "sides": _counter_rows(row["sides"], "side"),
            "max_net_edge": row.get("max_net_edge"),
        }
    return output


def _finalize_observation_buckets(payload: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for underlying, row in sorted(payload.items()):
        output[underlying] = {
            "count": row["count"],
            "trading_supported_count": row["trading_supported_count"],
            "market_count": len(row["markets"]),
            "markets": sorted(row["markets"])[:20],
            "periods": _counter_rows(row["periods"], "period"),
            "support_status": _counter_rows(row["support_status"], "support_status"),
            "support_reasons": _counter_rows(row["support_reasons"], "support_reason"),
            "first_ts": row["first_ts"],
            "last_ts": row["last_ts"],
            "examples": row["examples"],
        }
    return output


def _market_universe_conclusion(
    non_btc_underlyings: list[str],
    tradable_non_btc: list[str],
    examples: list[dict[str, Any]],
) -> str:
    if tradable_non_btc:
        return "non_btc_price_binary_tradable_candidates_present"
    if non_btc_underlyings:
        return "non_btc_price_binary_seen_but_not_tradable"
    if examples:
        return "non_btc_examples_seen_without_underlying_summary"
    return "btc_only_price_binary_universe"


def _render_exit_policy_section(lines: list[str], replay: dict[str, Any]) -> None:
    lines.extend(["## Exit Policy Replay", ""])
    lines.append(
        "| Policy | Source | Settlements | Exits | PnL | Delta active | "
        "PF | Win rate | Worst | Best |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for row in replay.get("policy_summaries", []):
        lines.append(
            f"| {row.get('policy')} | {row.get('source')} | "
            f"{row.get('settlement_count')} | {row.get('exit_event_count')} | "
            f"{_fmt_num(row.get('net_pnl_usdc'))} | "
            f"{_fmt_num(row.get('delta_vs_active_pnl_usdc'))} | "
            f"{_fmt_num(row.get('profit_factor'))} | "
            f"{_fmt_num(row.get('win_rate'))} | "
            f"{_fmt_num(row.get('worst_pnl_usdc'))} | "
            f"{_fmt_num(row.get('best_pnl_usdc'))} |"
        )
    lines.append("")
    if replay.get("entry_cutoff_summaries"):
        lines.extend(["### Entry Cutoffs", ""])
        lines.append("| Cutoff | Policy | Settlements | PnL | PF | Win rate | Worst | Best |")
        lines.append("|---|---|---:|---:|---:|---:|---:|---:|")
        for cutoff in replay.get("entry_cutoff_summaries", []):
            for row in cutoff.get("policies", []):
                lines.append(
                    f"| {cutoff.get('entry_cutoff')} | {row.get('policy')} | "
                    f"{row.get('settlement_count')} | {_fmt_num(row.get('net_pnl_usdc'))} | "
                    f"{_fmt_num(row.get('profit_factor'))} | {_fmt_num(row.get('win_rate'))} | "
                    f"{_fmt_num(row.get('worst_pnl_usdc'))} | "
                    f"{_fmt_num(row.get('best_pnl_usdc'))} |"
                )
        lines.append("")
    if replay.get("active_exit_reasons"):
        lines.extend(["### Active Exit Reasons", ""])
        lines.append("| Reason | Count | Realized PnL |")
        lines.append("|---|---:|---:|")
        for row in replay.get("active_exit_reasons", []):
            lines.append(
                f"| {row.get('reason')} | {row.get('count')} | "
                f"{_fmt_num(row.get('realized_pnl_usdc'))} |"
            )
        lines.append("")
    for note in replay.get("notes", []):
        lines.append(f"- {note}")
    lines.append("")


def _render_market_audit_section(lines: list[str], audit: dict[str, Any]) -> None:
    lines.extend(["## Non-BTC priceBinary Audit", ""])
    lines.append(
        "| Profile | Opps | Opp underlyings | priceBinary obs | priceBinary underlyings | "
        "non-BTC priceBinary | tradable non-BTC | Conclusion |"
    )
    lines.append("|---|---:|---|---:|---|---|---|---|")
    for profile in audit.get("profiles", []):
        opps = profile.get("opportunities", {})
        observations = profile.get("price_binary_observations", {})
        lines.append(
            f"| {profile.get('profile')} | {opps.get('count', 0)} | "
            f"{', '.join(opps.get('underlyings', [])) or 'none'} | "
            f"{observations.get('count', 0)} | "
            f"{', '.join(observations.get('underlyings', [])) or 'none'} | "
            f"{', '.join(profile.get('non_btc_underlyings', [])) or 'none'} | "
            f"{', '.join(profile.get('tradable_non_btc_underlyings', [])) or 'none'} | "
            f"{profile.get('conclusion')} |"
        )
    lines.append("")
    lines.extend(["### Target Coverage", ""])
    lines.append(
        "| Profile | Underlying | Opps | Approved | priceBinary obs | Trading-supported obs |"
    )
    lines.append("|---|---|---:|---:|---:|---:|")
    for profile in audit.get("profiles", []):
        for row in profile.get("target_coverage", []):
            if not any(
                int(row.get(key, 0))
                for key in (
                    "opportunities",
                    "approved_decisions",
                    "price_binary_observations",
                    "trading_supported_observations",
                )
            ):
                continue
            lines.append(
                f"| {profile.get('profile')} | {row.get('underlying')} | "
                f"{row.get('opportunities')} | {row.get('approved_decisions')} | "
                f"{row.get('price_binary_observations')} | "
                f"{row.get('trading_supported_observations')} |"
            )
    lines.append("")
    for profile in audit.get("profiles", []):
        examples = profile.get("price_binary_observations", {}).get("non_btc_examples", [])
        if not examples:
            continue
        lines.extend([f"### {profile.get('profile')} Non-BTC Examples", ""])
        lines.append("| ts | underlying | market | support | reason | description |")
        lines.append("|---|---|---|---|---|---|")
        for row in examples[:10]:
            description = str(row.get("description") or "").replace("|", "/")[:120]
            lines.append(
                f"| {row.get('ts')} | {row.get('underlying')} | {row.get('market_id')} | "
                f"{row.get('support_status')} | {row.get('support_reason')} | {description} |"
            )
        lines.append("")


def _iter_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
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
                yield payload


def _market_side_key(row: dict[str, Any]) -> tuple[str, str]:
    return str(row.get("market_id") or ""), str(row.get("side") or "")


def _pop_entry(
    queues: dict[tuple[str, str], list[dict[str, str]]],
    key: tuple[str, str],
) -> dict[str, str]:
    queue = queues.get(key)
    if not queue:
        return {}
    return queue.pop(0)


def _settlement_pnl(row: dict[str, str]) -> float:
    return _first_float(row.get("pnl_usdc"), row.get("net_pnl_usdc"), row.get("gross_pnl_usdc"))


def _first_float(*values: object) -> float:
    for value in values:
        parsed = _optional_float(value)
        if parsed is not None:
            return parsed
    return 0.0


def _float(value: object) -> float:
    parsed = _optional_float(value)
    return parsed if parsed is not None else 0.0


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_div(numerator: float, denominator: float) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 8)


def _counter_rows(counter: Counter[str], label: str) -> list[dict[str, Any]]:
    return [
        {label: key, "count": value}
        for key, value in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    ]


def _fmt_num(value: object) -> str:
    parsed = _optional_float(value)
    if parsed is None:
        return "n/a"
    return f"{parsed:.4f}"


def _opportunity_example(row: dict[str, str]) -> dict[str, Any]:
    return {
        "ts": row.get("ts"),
        "market_id": row.get("market_id"),
        "underlying": row.get("underlying"),
        "edge_type": row.get("edge_type"),
        "side": row.get("side"),
        "net_edge": _optional_float(row.get("net_edge")),
        "reason": row.get("reason"),
    }


def _observation_example(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": row.get("ts"),
        "market_id": row.get("market_id"),
        "underlying": str(row.get("underlying") or "").upper(),
        "period": row.get("period"),
        "expiry_iso": row.get("expiry_iso"),
        "support_status": row.get("support_status"),
        "support_reason": row.get("support_reason"),
        "description": row.get("description"),
    }


if __name__ == "__main__":
    main()
