from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.backtest import full_bot_replay as full_bot_replay_module
from app.backtest.full_bot_replay import FullBotBacktestResult, FullBotBacktestRunner
from app.settings import AppConfig, PodAPatternVetoConfig, load_config
from app.trident.market_clusters import cluster_for_symbol


class _NoopRoutingReplayRunner:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def run_jsonl(self, *, input_path: str | Path, dedupe_by_timestamp: bool = True):
        class _NoopRoutingResult:
            def to_dict(self) -> dict[str, object]:
                return {
                    "skipped": True,
                    "reason": "omitted_by_pod_c_pattern_validation",
                }

        return _NoopRoutingResult()


def _install_fast_routing_replay() -> None:
    full_bot_replay_module.RoutingReplayRunner = _NoopRoutingReplayRunner


@dataclass(slots=True)
class Candidate:
    name: str
    cluster: str
    description: str
    source_pattern: str
    rule: PodAPatternVetoConfig

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["rule"] = asdict(self.rule)
        return payload


@dataclass(slots=True)
class PatternStats:
    trades: int = 0
    pnl_usd: float = 0.0
    wins: int = 0
    losses: int = 0

    @property
    def win_rate(self) -> float:
        return round(self.wins / self.trades, 4) if self.trades else 0.0

    def add(self, pnl: float) -> None:
        self.trades += 1
        self.pnl_usd = round(self.pnl_usd + pnl, 2)
        if pnl >= 0.0:
            self.wins += 1
        else:
            self.losses += 1

    def to_dict(self) -> dict[str, object]:
        return {
            "trades": self.trades,
            "pnl_usd": self.pnl_usd,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": self.win_rate,
        }


@dataclass(slots=True)
class CandidateResult:
    name: str
    cluster: str
    verdict: str
    before_total_pnl_usd: float
    after_total_pnl_usd: float
    total_delta_usd: float
    before_pod_c_pnl_usd: float
    after_pod_c_pnl_usd: float
    pod_c_delta_usd: float
    before_target: PatternStats
    after_target: PatternStats
    veto_rejections: int
    note: str
    runtime_seconds: float

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["before_target"] = self.before_target.to_dict()
        payload["after_target"] = self.after_target.to_dict()
        return payload


def _candidates() -> list[Candidate]:
    return [
        Candidate(
            name="silver_strong_extension_veto",
            cluster="silver",
            description=(
                "Veto de la poche silver extension: trend/structure forts avec entree deja en extension."
            ),
            source_pattern="silver|tradfi_continuation_long|strong|strong|extension",
            rule=PodAPatternVetoConfig(
                name="silver_strong_extension_veto",
                enabled=True,
                setups=["tradfi_continuation_long"],
                sides=["long"],
                market_clusters=["silver"],
                trend_buckets=["strong"],
                structure_buckets=["strong"],
                vwap_buckets=["extension"],
            ),
        ),
        Candidate(
            name="gold_soft_extension_veto",
            cluster="gold",
            description=(
                "Veto gold extension fragile: trend encore soft, structure forte, entree en extension."
            ),
            source_pattern="gold|tradfi_continuation_long|soft|strong|extension",
            rule=PodAPatternVetoConfig(
                name="gold_soft_extension_veto",
                enabled=True,
                setups=["tradfi_continuation_long"],
                sides=["long"],
                market_clusters=["gold"],
                trend_buckets=["soft"],
                structure_buckets=["strong"],
                vwap_buckets=["extension"],
            ),
        ),
        Candidate(
            name="gold_strong_neutral_veto",
            cluster="gold",
            description=(
                "Veto gold neutral chase: trend/structure forts mais entree proche VWAP neutre."
            ),
            source_pattern="gold|tradfi_continuation_long|strong|strong|neutral",
            rule=PodAPatternVetoConfig(
                name="gold_strong_neutral_veto",
                enabled=True,
                setups=["tradfi_continuation_long"],
                sides=["long"],
                market_clusters=["gold"],
                trend_buckets=["strong"],
                structure_buckets=["strong"],
                vwap_buckets=["neutral"],
            ),
        ),
        Candidate(
            name="gold_medium_neutral_veto",
            cluster="gold",
            description=(
                "Veto gold neutral mid-trend: trend moyen, structure forte, entree proche VWAP neutre."
            ),
            source_pattern="gold|tradfi_continuation_long|medium|strong|neutral",
            rule=PodAPatternVetoConfig(
                name="gold_medium_neutral_veto",
                enabled=True,
                setups=["tradfi_continuation_long"],
                sides=["long"],
                market_clusters=["gold"],
                trend_buckets=["medium"],
                structure_buckets=["strong"],
                vwap_buckets=["neutral"],
            ),
        ),
    ]


def _run_full_bot(config: AppConfig, *, input_path: Path) -> tuple[FullBotBacktestResult, float]:
    started = time.perf_counter()
    result = FullBotBacktestRunner(config, force_enable_all_pods=False).run_jsonl(
        input_path=input_path,
        dedupe_by_timestamp=True,
    )
    return result, time.perf_counter() - started


def _remove_candidate(config: AppConfig, candidate: Candidate) -> AppConfig:
    return replace(
        config,
        pod_c=replace(
            config.pod_c,
            pattern_vetoes=[
                item
                for item in config.pod_c.pattern_vetoes
                if item.name != candidate.rule.name
            ],
        ),
    )


def _has_candidate(config: AppConfig, candidate: Candidate) -> bool:
    return any(item.name == candidate.rule.name for item in config.pod_c.pattern_vetoes)


def _add_candidate(config: AppConfig, candidate: Candidate) -> AppConfig:
    config = _remove_candidate(config, candidate)
    return replace(
        config,
        pod_c=replace(
            config.pod_c,
            pattern_vetoes=list(config.pod_c.pattern_vetoes) + [candidate.rule],
        ),
    )


def _string_set(values: list[str]) -> set[str]:
    return {str(item).strip() for item in values if str(item).strip()}


def _cluster_set(values: list[str]) -> set[str]:
    return {str(item).strip().lower() for item in values if str(item).strip()}


def _trade_matches_rule(trade: dict[str, object], rule: PodAPatternVetoConfig) -> bool:
    details = trade.get("setup_details", {})
    if not isinstance(details, dict):
        details = {}
    if rule.setups and str(trade.get("setup", "")).strip() not in _string_set(rule.setups):
        return False
    if rule.sides and str(trade.get("side", "")).strip() not in _string_set(rule.sides):
        return False
    if rule.symbols and str(trade.get("symbol", "")).strip().upper() not in {
        item.upper() for item in _string_set(rule.symbols)
    }:
        return False
    market_cluster = str(details.get("market_cluster", trade.get("market_cluster", ""))).strip().lower()
    if rule.market_clusters and market_cluster not in _cluster_set(rule.market_clusters):
        return False
    exact_fields = (
        ("cluster_strategies", "cluster_strategy"),
        ("trend_buckets", "trend_bucket"),
        ("structure_buckets", "structure_bucket"),
        ("vwap_buckets", "vwap_bucket"),
        ("activity_buckets", "activity_bucket"),
        ("trade_count_buckets", "trade_count_bucket"),
        ("flow_buckets", "flow_bucket"),
        ("flow_alignments", "flow_alignment"),
    )
    for attr, detail_key in exact_fields:
        expected = _string_set(getattr(rule, attr))
        if expected and str(details.get(detail_key, "")).strip() not in expected:
            return False
    return True


def _target_stats(result: FullBotBacktestResult, candidate: Candidate) -> PatternStats:
    stats = PatternStats()
    for trade in result.pod_c.get("closed_trade_log", []) or []:
        if not isinstance(trade, dict):
            continue
        if _trade_matches_rule(trade, candidate.rule):
            stats.add(float(trade.get("pnl_usd", 0.0) or 0.0))
    return stats


def _symbol_rows(config: AppConfig, result: FullBotBacktestResult) -> list[dict[str, object]]:
    stats_by_symbol: dict[str, PatternStats] = {}
    strategies_by_symbol: dict[str, dict[str, PatternStats]] = {}
    reasons_by_symbol: dict[str, dict[str, int]] = {}
    for trade in result.pod_c.get("closed_trade_log", []) or []:
        if not isinstance(trade, dict):
            continue
        symbol = str(trade.get("symbol", "")).upper()
        if not symbol:
            continue
        pnl = float(trade.get("pnl_usd", 0.0) or 0.0)
        stats_by_symbol.setdefault(symbol, PatternStats()).add(pnl)
        details = trade.get("setup_details", {})
        if not isinstance(details, dict):
            details = {}
        strategy = str(details.get("cluster_strategy", trade.get("setup", "unknown")) or "unknown")
        strategies_by_symbol.setdefault(symbol, {}).setdefault(strategy, PatternStats()).add(pnl)
        reason = str(trade.get("close_reason", "unknown") or "unknown")
        reasons = reasons_by_symbol.setdefault(symbol, {})
        reasons[reason] = reasons.get(reason, 0) + 1

    rows: list[dict[str, object]] = []
    for symbol in config.hyperliquid.observation_universe:
        if ":" not in symbol:
            continue
        cluster = cluster_for_symbol(config, symbol)
        symbol_stats = stats_by_symbol.get(symbol.upper(), PatternStats())
        strategies = strategies_by_symbol.get(symbol.upper(), {})
        best_strategy = "-"
        if strategies:
            best_strategy, best_stats = max(
                strategies.items(),
                key=lambda item: (item[1].pnl_usd, item[1].trades),
            )
            best_strategy = f"{best_strategy} ({best_stats.pnl_usd:+.2f}/{best_stats.trades}t)"
        note = "branche active positive" if symbol_stats.pnl_usd > 0 else "aucun trade pod C courant"
        if cluster in {"equity", "fx"}:
            note = "non couvert par cluster_aware_v2 actuellement"
        elif symbol_stats.trades > 0 and symbol_stats.pnl_usd <= 0:
            note = "poche a surveiller / filtrer"
        rows.append(
            {
                "symbol": symbol,
                "cluster": cluster,
                "trades": symbol_stats.trades,
                "pnl_usd": symbol_stats.pnl_usd,
                "win_rate": symbol_stats.win_rate,
                "best_pattern": best_strategy,
                "close_reasons": reasons_by_symbol.get(symbol.upper(), {}),
                "note": note,
            }
        )
    return rows


def _cluster_strategy_rows(result: FullBotBacktestResult) -> list[dict[str, object]]:
    stats: dict[str, PatternStats] = {}
    for trade in result.pod_c.get("closed_trade_log", []) or []:
        if not isinstance(trade, dict):
            continue
        details = trade.get("setup_details", {})
        if not isinstance(details, dict):
            details = {}
        cluster = str(details.get("market_cluster", trade.get("market_cluster", "unknown")))
        strategy = str(details.get("cluster_strategy", trade.get("setup", "unknown")))
        key = f"{cluster}|{strategy}"
        stats.setdefault(key, PatternStats()).add(float(trade.get("pnl_usd", 0.0) or 0.0))
    return [
        {"pattern": key, **item.to_dict()}
        for key, item in sorted(
            stats.items(),
            key=lambda pair: pair[1].pnl_usd,
            reverse=True,
        )
    ]


def _baseline_payload(result: FullBotBacktestResult, runtime: float) -> dict[str, object]:
    return {
        "total_pnl_usd": float(result.total_realized_pnl_usd),
        "pod_a_pnl_usd": float(result.pod_a.get("realized_pnl_usd", 0.0) or 0.0),
        "pod_b_pnl_usd": float(result.pod_b.get("realized_pnl_usd", 0.0) or 0.0),
        "pod_c_pnl_usd": float(result.pod_c.get("realized_pnl_usd", 0.0) or 0.0),
        "pod_c_trades": int(result.pod_c.get("closed_trade_count", 0) or 0),
        "records_processed": int(result.records_processed),
        "duplicate_timestamps_skipped": int(result.duplicate_timestamps_skipped),
        "dates_covered": list(result.dates_covered),
        "runtime_seconds": round(runtime, 3),
    }


def _verdict(
    *,
    before: FullBotBacktestResult,
    after: FullBotBacktestResult,
    before_target: PatternStats,
    veto_rejections: int,
) -> tuple[str, str]:
    total_delta = round(after.total_realized_pnl_usd - before.total_realized_pnl_usd, 4)
    pod_c_delta = round(
        float(after.pod_c.get("realized_pnl_usd", 0.0) or 0.0)
        - float(before.pod_c.get("realized_pnl_usd", 0.0) or 0.0),
        4,
    )
    if before_target.trades <= 0 and veto_rejections <= 0:
        return "no_effect", "Aucun trade cible et aucun veto declenche sur cette fenetre."
    if veto_rejections <= 0:
        return "no_effect", "La regle n'a pas declenche de rejet sur cette fenetre."
    if total_delta > 0.0 and pod_c_delta > 0.0:
        return "keep", "Le veto ameliore le full replay et le PnL Pod C."
    return "reject", "Le veto ne cree pas de gain net comparable."


def _render_markdown(payload: dict[str, object]) -> str:
    baseline = payload["baseline"]
    final = payload["final"]
    lines = [
        "# Pod C Pattern Implementation Validation",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Input: `{payload['input_path']}`",
        f"- Config baseline: `{payload['config_path']}`",
        "- Full-bot routing execution is active; only the extra routing summary replay is omitted for speed.",
        f"- Baseline total PnL: `{baseline['total_pnl_usd']:.2f}` "
        f"(Pod C `{baseline['pod_c_pnl_usd']:.2f}`)",
        f"- Final kept total PnL: `{final['total_pnl_usd']:.2f}` "
        f"(Pod C `{final['pod_c_pnl_usd']:.2f}`)",
        "",
        "## Patterns By Coin",
        "",
        "| Coin | Cluster | Pattern principal courant | Trades | PnL | Win rate | Decision |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    result_by_cluster = {
        str(item["cluster"]): item
        for item in payload.get("results", [])
        if item.get("verdict") == "keep"
    }
    for row in payload["symbol_rows"]:
        decision = row["note"]
        cluster_result = result_by_cluster.get(str(row["cluster"]))
        if cluster_result is not None:
            decision = f"garder + ajouter `{cluster_result['name']}`"
        lines.append(
            f"| {row['symbol']} | {row['cluster']} | {row['best_pattern']} | "
            f"{row['trades']} | {row['pnl_usd']:.2f} | {row['win_rate']:.2f} | {decision} |"
        )
    lines.extend(
        [
            "",
            "## Strategy Summary",
            "",
            "| Pattern | Trades | PnL | Win rate |",
            "|---|---:|---:|---:|",
        ]
    )
    for row in payload["cluster_strategy_rows"]:
        lines.append(
            f"| {row['pattern']} | {row['trades']} | {row['pnl_usd']:.2f} | {row['win_rate']:.2f} |"
        )
    lines.extend(
        [
            "",
            "## Candidate Replays",
            "",
            "| Candidat | Source pattern | Verdict | Before | After | Delta | Pod C delta | Target before | Vetoes | Note |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    candidate_by_name = {item["name"]: item for item in payload["candidates"]}
    for item in payload["results"]:
        candidate = candidate_by_name.get(str(item["name"]), {})
        before_target = item["before_target"]
        lines.append(
            f"| {item['name']} | {candidate.get('source_pattern', '-')} | {item['verdict']} | "
            f"{item['before_total_pnl_usd']:.2f} | {item['after_total_pnl_usd']:.2f} | "
            f"{item['total_delta_usd']:.2f} | {item['pod_c_delta_usd']:.2f} | "
            f"{before_target['pnl_usd']:.2f}/{before_target['trades']}t | "
            f"{item['veto_rejections']} | {item['note']} |"
        )
    lines.append("")
    return "\n".join(lines)


def _write_outputs(
    payload: dict[str, object],
    *,
    output_json: Path,
    output_md: Path,
) -> None:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_md.write_text(_render_markdown(payload), encoding="utf-8")


def run_validation(
    *,
    config_path: Path,
    input_path: Path,
    output_json: Path,
    output_md: Path,
) -> dict[str, object]:
    _install_fast_routing_replay()
    base_config = load_config(str(config_path))
    print("baseline status=running", flush=True)
    baseline, baseline_runtime = _run_full_bot(base_config, input_path=input_path)
    print(
        f"baseline total={baseline.total_realized_pnl_usd} "
        f"pod_c={baseline.pod_c.get('realized_pnl_usd', 0.0)} "
        f"seconds={baseline_runtime:.1f}",
        flush=True,
    )

    candidates = _candidates()
    working_config = base_config
    working_result = baseline
    results: list[dict[str, object]] = []
    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00",
            "Z",
        ),
        "input_path": str(input_path),
        "config_path": str(config_path),
        "baseline": _baseline_payload(baseline, baseline_runtime),
        "final": _baseline_payload(baseline, baseline_runtime),
        "candidates": [item.to_dict() for item in candidates],
        "symbol_rows": _symbol_rows(base_config, baseline),
        "cluster_strategy_rows": _cluster_strategy_rows(baseline),
        "results": results,
        "kept_candidates": [],
    }
    _write_outputs(payload, output_json=output_json, output_md=output_md)

    for index, candidate in enumerate(candidates, start=1):
        candidate_already_active = _has_candidate(working_config, candidate)
        if candidate_already_active:
            comparison_config = _remove_candidate(working_config, candidate)
            print(
                f"[{index}/{len(candidates)}] {candidate.name} status=control_without_promoted",
                flush=True,
            )
            comparison, runtime = _run_full_bot(comparison_config, input_path=input_path)
            scenario_config = working_config
            scenario = working_result
        else:
            comparison_config = working_config
            comparison = working_result
            scenario_config = _add_candidate(working_config, candidate)
            print(f"[{index}/{len(candidates)}] {candidate.name} status=running", flush=True)
            scenario, runtime = _run_full_bot(scenario_config, input_path=input_path)
        before_target = _target_stats(comparison, candidate)
        after_target = _target_stats(scenario, candidate)
        reason = f"pattern_veto_{candidate.rule.name}"
        veto_rejections = int(
            (scenario.pod_c.get("rejections_by_reason", {}) or {}).get(reason, 0) or 0
        )
        verdict, note = _verdict(
            before=comparison,
            after=scenario,
            before_target=before_target,
            veto_rejections=veto_rejections,
        )
        result = CandidateResult(
            name=candidate.name,
            cluster=candidate.cluster,
            verdict=verdict,
            before_total_pnl_usd=float(comparison.total_realized_pnl_usd),
            after_total_pnl_usd=float(scenario.total_realized_pnl_usd),
            total_delta_usd=round(
                scenario.total_realized_pnl_usd - comparison.total_realized_pnl_usd,
                4,
            ),
            before_pod_c_pnl_usd=float(
                comparison.pod_c.get("realized_pnl_usd", 0.0) or 0.0
            ),
            after_pod_c_pnl_usd=float(scenario.pod_c.get("realized_pnl_usd", 0.0) or 0.0),
            pod_c_delta_usd=round(
                float(scenario.pod_c.get("realized_pnl_usd", 0.0) or 0.0)
                - float(comparison.pod_c.get("realized_pnl_usd", 0.0) or 0.0),
                4,
            ),
            before_target=before_target,
            after_target=after_target,
            veto_rejections=veto_rejections,
            note=note,
            runtime_seconds=round(runtime, 3),
        )
        results.append(result.to_dict())
        if verdict == "keep":
            working_config = scenario_config
            working_result = scenario
            payload["kept_candidates"].append(candidate.name)
            payload["final"] = _baseline_payload(scenario, runtime)
        elif candidate_already_active:
            working_config = comparison_config
            working_result = comparison
            payload["final"] = _baseline_payload(comparison, runtime)
        payload["results"] = results
        _write_outputs(payload, output_json=output_json, output_md=output_md)
        print(
            f"[{index}/{len(candidates)}] {candidate.name} verdict={verdict} "
            f"after={result.after_total_pnl_usd} delta={result.total_delta_usd} "
            f"pod_c_delta={result.pod_c_delta_usd} vetoes={veto_rejections} "
            f"seconds={runtime:.1f}",
            flush=True,
        )

    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate Pod C pattern candidates one by one on a full replay.",
    )
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--input", default="server-data/replay_inputs/full_bot_latest_fetch.jsonl")
    parser.add_argument(
        "--output-json",
        default="server-data/replay_reports/pod_c_pattern_implementation_validation_20260424.json",
    )
    parser.add_argument(
        "--output-md",
        default="server-data/replay_reports/pod_c_pattern_implementation_validation_20260424.md",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    run_validation(
        config_path=Path(args.config),
        input_path=Path(args.input),
        output_json=Path(args.output_json),
        output_md=Path(args.output_md),
    )


if __name__ == "__main__":
    main()
