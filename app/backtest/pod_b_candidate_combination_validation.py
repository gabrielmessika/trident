from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from app.backtest import full_bot_replay as full_bot_replay_module
from app.backtest.full_bot_replay import FullBotBacktestResult, FullBotBacktestRunner
from app.settings import AllocationConfig, AppConfig, RegimeAllocations, load_config


POD_B_PATTERN_SETUP = {
    "ttm_squeeze_release": "ttm_squeeze_release_long",
    "squeeze_breakout": "compression_breakout_long",
    # Proxy: the research trend_breakout family is not the same rule as Pod B
    # vol_expansion, but this is the closest production Pod B breakout sleeve.
    "trend_breakout": "vol_expansion_long",
}


class _NoopRoutingReplayRunner:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def run_jsonl(self, *, input_path: str | Path, dedupe_by_timestamp: bool = True):
        class _NoopRoutingResult:
            def to_dict(self) -> dict[str, object]:
                return {
                    "skipped": True,
                    "reason": "omitted_by_pod_b_candidate_combination_validation",
                }

        return _NoopRoutingResult()


def _install_fast_routing_replay() -> None:
    full_bot_replay_module.RoutingReplayRunner = _NoopRoutingReplayRunner


@dataclass(frozen=True, slots=True)
class PodBCandidate:
    name: str
    symbol: str
    pattern: str
    setup: str
    mapping: str
    sources: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["sources"] = list(self.sources)
        return payload


@dataclass(slots=True)
class ScenarioResult:
    name: str
    candidate_names: list[str]
    total_pnl_usd: float
    pod_a_pnl_usd: float
    pod_b_pnl_usd: float
    pod_c_pnl_usd: float
    pod_a_trades: int
    pod_b_trades: int
    pod_c_trades: int
    baseline_pod_a_target_stats: dict[str, object]
    scenario_pod_b_target_stats: dict[str, object]
    pod_b_rejections: dict[str, int]
    runtime_seconds: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _safe_name(*parts: str) -> str:
    raw = "_".join(parts)
    return "".join(char.lower() if char.isalnum() else "_" for char in raw).strip("_")


def _source_pattern(item: dict[str, object]) -> dict[str, object]:
    side_breakdown = item.get("side_breakdown", {})
    if not isinstance(side_breakdown, dict):
        side_breakdown = {}
    return {
        "interval": item.get("interval"),
        "hold_bars": item.get("hold_bars"),
        "expectancy_net_bps": item.get("expectancy_net_bps"),
        "sample_count": item.get("sample_count"),
        "side_breakdown": side_breakdown,
    }


def _load_candidates(matrix_path: Path, *, top_n: int) -> list[PodBCandidate]:
    payload = json.loads(matrix_path.read_text(encoding="utf-8"))
    by_key: dict[tuple[str, str, str], list[dict[str, object]]] = {}
    for row in payload.get("symbols", []):
        symbol = str(row.get("symbol", "")).strip().upper()
        if not symbol or ":" in symbol:
            continue
        for item in list(row.get("top_patterns", []))[:top_n]:
            pattern = str(item.get("pattern", "")).strip()
            setup = POD_B_PATTERN_SETUP.get(pattern)
            if setup is None:
                continue
            by_key.setdefault((symbol, pattern, setup), []).append(_source_pattern(item))
    candidates: list[PodBCandidate] = []
    for symbol, pattern, setup in sorted(by_key):
        mapping = "exact" if pattern in {"ttm_squeeze_release", "squeeze_breakout"} else "proxy"
        candidates.append(
            PodBCandidate(
                name=_safe_name(symbol, pattern, setup),
                symbol=symbol,
                pattern=pattern,
                setup=setup,
                mapping=mapping,
                sources=tuple(by_key[(symbol, pattern, setup)]),
            )
        )
    return candidates


def _candidate_by_name(matrix_path: Path, *, top_n: int) -> dict[str, PodBCandidate]:
    return {item.name: item for item in _load_candidates(matrix_path, top_n=top_n)}


def _pod_b_slot_allocations() -> RegimeAllocations:
    return RegimeAllocations(
        trend_expansion=AllocationConfig(pod_a=0.70, pod_b=0.10, pod_c=0.20, cash=0.00),
        range_auction=AllocationConfig(pod_a=0.10, pod_b=0.10, pod_c=0.15, cash=0.65),
        panic_squeeze=AllocationConfig(pod_a=0.10, pod_b=0.10, pod_c=0.05, cash=0.75),
        dead_zone=AllocationConfig(pod_a=0.00, pod_b=0.20, pod_c=0.05, cash=0.75),
    )


def _crypto_symbols(config: AppConfig) -> set[str]:
    return {
        symbol.upper()
        for symbol in config.hyperliquid.observation_universe
        if ":" not in symbol and symbol.upper() == symbol
    }


def _scenario_config(config: AppConfig, candidates: list[PodBCandidate]) -> AppConfig:
    selected_symbols = {candidate.symbol.upper() for candidate in candidates}
    selected_setups = list(dict.fromkeys(candidate.setup for candidate in candidates))
    routing = replace(
        config.trident.routing,
        symbol_pod_overrides={
            **dict(config.trident.routing.symbol_pod_overrides),
            **{symbol: "pod_b" for symbol in selected_symbols},
        },
    )
    trident = replace(
        config.trident,
        routing=routing,
        allocations=_pod_b_slot_allocations(),
    )
    pod_b = replace(
        config.pod_b,
        enabled=True,
        bis_blocked_symbols=sorted(_crypto_symbols(config) - selected_symbols),
        bis_enable_longs=True,
        bis_enable_shorts=False,
        bis_enabled_setups=selected_setups,
    )
    return replace(config, trident=trident, pod_b=pod_b)


def _run_full_bot(config: AppConfig, input_path: Path) -> tuple[FullBotBacktestResult, float]:
    started = time.perf_counter()
    result = FullBotBacktestRunner(config, force_enable_all_pods=False).run_jsonl(
        input_path=input_path,
        dedupe_by_timestamp=True,
    )
    return result, time.perf_counter() - started


def _trade_stats(
    result: FullBotBacktestResult,
    *,
    pod: str,
    symbol: str,
    setup: str | None = None,
) -> dict[str, object]:
    payload = result.pod_b if pod == "pod_b" else result.pod_a
    trades = []
    for item in payload.get("closed_trade_log", []) or []:
        if str(item.get("symbol", "")).upper() != symbol.upper():
            continue
        if setup is not None and str(item.get("setup", "")) != setup:
            continue
        trades.append(item)
    pnl = round(sum(float(item.get("pnl_usd", 0.0) or 0.0) for item in trades), 2)
    return {
        "trades": len(trades),
        "pnl_usd": pnl,
        "wins": sum(1 for item in trades if float(item.get("pnl_usd", 0.0) or 0.0) >= 0.0),
        "losses": sum(1 for item in trades if float(item.get("pnl_usd", 0.0) or 0.0) < 0.0),
    }


def _target_stats_by_candidate(
    result: FullBotBacktestResult,
    candidates: list[PodBCandidate],
    *,
    pod: str,
) -> dict[str, object]:
    return {
        candidate.name: _trade_stats(
            result,
            pod=pod,
            symbol=candidate.symbol,
            setup=candidate.setup if pod == "pod_b" else None,
        )
        for candidate in candidates
    }


def _scenario_result(
    *,
    name: str,
    candidate_names: list[str],
    scenario: FullBotBacktestResult,
    candidates: list[PodBCandidate],
    runtime: float,
) -> ScenarioResult:
    return ScenarioResult(
        name=name,
        candidate_names=candidate_names,
        total_pnl_usd=float(scenario.total_realized_pnl_usd),
        pod_a_pnl_usd=float(scenario.pod_a.get("realized_pnl_usd", 0.0) or 0.0),
        pod_b_pnl_usd=float(scenario.pod_b.get("realized_pnl_usd", 0.0) or 0.0),
        pod_c_pnl_usd=float(scenario.pod_c.get("realized_pnl_usd", 0.0) or 0.0),
        pod_a_trades=int(scenario.pod_a.get("closed_trade_count", 0) or 0),
        pod_b_trades=int(scenario.pod_b.get("closed_trade_count", 0) or 0),
        pod_c_trades=int(scenario.pod_c.get("closed_trade_count", 0) or 0),
        baseline_pod_a_target_stats={},
        scenario_pod_b_target_stats=_target_stats_by_candidate(
            scenario,
            candidates,
            pod="pod_b",
        ),
        pod_b_rejections=dict(scenario.pod_b.get("rejections_by_reason", {}) or {}),
        runtime_seconds=round(runtime, 3),
    )


def _run_scenario_worker(args: tuple[str, list[str], str, str, str, int]) -> dict[str, object]:
    name, candidate_names, config_path, input_path, matrix_path, top_n = args
    _install_fast_routing_replay()
    candidate_map = _candidate_by_name(Path(matrix_path), top_n=top_n)
    candidates = [candidate_map[item] for item in candidate_names]
    base_config = load_config(config_path)
    scenario_config = _scenario_config(base_config, candidates)
    scenario, runtime = _run_full_bot(scenario_config, Path(input_path))
    return _scenario_result(
        name=name,
        candidate_names=candidate_names,
        scenario=scenario,
        candidates=candidates,
        runtime=runtime,
    ).to_dict()


def _baseline_payload(result: FullBotBacktestResult, runtime: float) -> dict[str, object]:
    return {
        "total_pnl_usd": float(result.total_realized_pnl_usd),
        "pod_a_pnl_usd": float(result.pod_a.get("realized_pnl_usd", 0.0) or 0.0),
        "pod_b_pnl_usd": float(result.pod_b.get("realized_pnl_usd", 0.0) or 0.0),
        "pod_c_pnl_usd": float(result.pod_c.get("realized_pnl_usd", 0.0) or 0.0),
        "pod_a_trades": int(result.pod_a.get("closed_trade_count", 0) or 0),
        "pod_b_trades": int(result.pod_b.get("closed_trade_count", 0) or 0),
        "pod_c_trades": int(result.pod_c.get("closed_trade_count", 0) or 0),
        "records_processed": int(result.records_processed),
        "duplicate_timestamps_skipped": int(result.duplicate_timestamps_skipped),
        "dates_covered": list(result.dates_covered),
        "runtime_seconds": round(runtime, 3),
    }


def _run_scenarios(
    scenarios: list[tuple[str, list[str]]],
    *,
    config_path: Path,
    input_path: Path,
    matrix_path: Path,
    top_n: int,
    max_workers: int,
) -> list[dict[str, object]]:
    if not scenarios:
        return []
    workers = max(1, min(max_workers, len(scenarios)))
    results: list[dict[str, object]] = []
    with ProcessPoolExecutor(max_workers=workers) as executor:
        future_by_name = {
            executor.submit(
                _run_scenario_worker,
                (name, candidate_names, str(config_path), str(input_path), str(matrix_path), top_n),
            ): name
            for name, candidate_names in scenarios
        }
        for future in as_completed(future_by_name):
            name = future_by_name[future]
            result = future.result()
            results.append(result)
            print(
                f"{name} done total={result['total_pnl_usd']:.2f} "
                f"pod_b={result['pod_b_pnl_usd']:.2f} trades_b={result['pod_b_trades']} "
                f"seconds={result['runtime_seconds']:.1f}",
                flush=True,
            )
    return sorted(results, key=lambda item: str(item["name"]))


def _with_deltas(rows: list[dict[str, object]], baseline_total: float) -> list[dict[str, object]]:
    enriched: list[dict[str, object]] = []
    for row in rows:
        item = dict(row)
        item["delta_vs_baseline_usd"] = round(
            float(item.get("total_pnl_usd", 0.0) or 0.0) - baseline_total,
            4,
        )
        enriched.append(item)
    return enriched


def _with_baseline_target_stats(
    rows: list[dict[str, object]],
    *,
    baseline_result: FullBotBacktestResult,
    candidate_map: dict[str, PodBCandidate],
) -> list[dict[str, object]]:
    enriched: list[dict[str, object]] = []
    for row in rows:
        item = dict(row)
        candidates = [candidate_map[name] for name in item.get("candidate_names", [])]
        item["baseline_pod_a_target_stats"] = _target_stats_by_candidate(
            baseline_result,
            candidates,
            pod="pod_a",
        )
        enriched.append(item)
    return enriched


def _combination_scenarios(
    candidates: list[PodBCandidate],
    individual_results: list[dict[str, object]],
) -> list[tuple[str, list[str]]]:
    delta_by_name = {
        str(item["candidate_names"][0]): float(item.get("delta_vs_baseline_usd", 0.0) or 0.0)
        for item in individual_results
        if len(item.get("candidate_names", [])) == 1
    }
    by_name = {item.name: item for item in candidates}
    positive = [name for name, delta in delta_by_name.items() if delta > 0.0]
    non_negative = [name for name, delta in delta_by_name.items() if delta >= 0.0]
    best_by_coin: dict[str, str] = {}
    for candidate in candidates:
        current = best_by_coin.get(candidate.symbol)
        if current is None or delta_by_name.get(candidate.name, -10**9) > delta_by_name.get(current, -10**9):
            best_by_coin[candidate.symbol] = candidate.name

    scenarios: list[tuple[str, list[str]]] = []
    if positive:
        scenarios.append(("combo_positive_individual", positive))
    if non_negative:
        scenarios.append(("combo_non_negative_individual", non_negative))
    scenarios.extend(
        [
            ("combo_best_per_coin", sorted(best_by_coin.values())),
            (
                "combo_all_exact_pod_b_patterns",
                [candidate.name for candidate in candidates if candidate.mapping == "exact"],
            ),
            (
                "combo_all_ttm_squeeze_release",
                [
                    candidate.name
                    for candidate in candidates
                    if candidate.pattern == "ttm_squeeze_release"
                ],
            ),
            (
                "combo_all_squeeze_breakout",
                [candidate.name for candidate in candidates if candidate.pattern == "squeeze_breakout"],
            ),
            (
                "combo_all_trend_breakout_proxy",
                [candidate.name for candidate in candidates if candidate.pattern == "trend_breakout"],
            ),
            ("combo_all_pod_b_candidates", [candidate.name for candidate in candidates]),
        ]
    )
    seen: set[tuple[str, ...]] = set()
    unique: list[tuple[str, list[str]]] = []
    for name, candidate_names in scenarios:
        cleaned = [item for item in candidate_names if item in by_name]
        key = tuple(sorted(cleaned))
        if not cleaned or key in seen:
            continue
        seen.add(key)
        unique.append((name, cleaned))
    return unique


def _render_stats(stats: object) -> str:
    return json.dumps(stats, sort_keys=True)


def _render_markdown(payload: dict[str, object]) -> str:
    baseline = payload["baseline"]
    baseline_total = float(baseline["total_pnl_usd"])
    candidates = {
        str(item["name"]): item for item in payload.get("candidates", []) if isinstance(item, dict)
    }
    lines = [
        "# Pod B Candidate Combination Validation",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Input: `{payload['input_path']}`",
        f"- Config baseline: `{payload['config_path']}`",
        f"- Matrix: `{payload['matrix_path']}`, top N: `{payload['top_n']}`",
        "- Candidate = coin + pattern + Pod B setup. No global pattern enablement.",
        "- Scenarios route only selected candidate coins to Pod B; non-selected crypto remains Pod A.",
        "- Pod B is tested long-only, matching current config directionality.",
        f"- Baseline total PnL: `{baseline_total:.2f}` "
        f"(Pod A `{baseline['pod_a_pnl_usd']:.2f}`, Pod B `{baseline['pod_b_pnl_usd']:.2f}`, "
        f"Pod C `{baseline['pod_c_pnl_usd']:.2f}`)",
        "",
        "## Individual Candidates",
        "",
        "| Candidate | Coin | Pattern | Mapping | Setup | Total | Delta | Pod B PnL | Pod B trades | Baseline Pod A target | Scenario Pod B target |",
        "|---|---|---|---|---|---:|---:|---:|---:|---|---|",
    ]
    for row in sorted(
        payload["individual_results"],
        key=lambda item: float(item.get("delta_vs_baseline_usd", 0.0) or 0.0),
        reverse=True,
    ):
        name = str(row["candidate_names"][0])
        candidate = candidates[name]
        baseline_stats = row.get("baseline_pod_a_target_stats", {}).get(name, {})
        pod_b_stats = row.get("scenario_pod_b_target_stats", {}).get(name, {})
        lines.append(
            f"| {name} | {candidate['symbol']} | {candidate['pattern']} | "
            f"{candidate['mapping']} | {candidate['setup']} | "
            f"{float(row['total_pnl_usd']):.2f} | "
            f"{float(row['delta_vs_baseline_usd']):+.2f} | "
            f"{float(row['pod_b_pnl_usd']):.2f} | {int(row['pod_b_trades'])} | "
            f"`{_render_stats(baseline_stats)}` | `{_render_stats(pod_b_stats)}` |"
        )
    lines.extend(
        [
            "",
            "## Combination Replays",
            "",
            "| Scenario | Candidates | Total | Delta | Pod A | Pod B | Pod C | Pod B trades |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in sorted(
        payload["combination_results"],
        key=lambda item: float(item.get("delta_vs_baseline_usd", 0.0) or 0.0),
        reverse=True,
    ):
        lines.append(
            f"| {row['name']} | {len(row['candidate_names'])} | "
            f"{float(row['total_pnl_usd']):.2f} | "
            f"{float(row['delta_vs_baseline_usd']):+.2f} | "
            f"{float(row['pod_a_pnl_usd']):.2f} | "
            f"{float(row['pod_b_pnl_usd']):.2f} | "
            f"{float(row['pod_c_pnl_usd']):.2f} | {int(row['pod_b_trades'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def _write_outputs(payload: dict[str, object], output_json: Path, output_md: Path) -> None:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    output_md.write_text(_render_markdown(payload), encoding="utf-8")


def run_validation(
    *,
    config_path: Path,
    input_path: Path,
    matrix_path: Path,
    output_json: Path,
    output_md: Path,
    top_n: int,
    max_workers: int,
) -> dict[str, object]:
    candidates = _load_candidates(matrix_path, top_n=top_n)
    candidate_map = {candidate.name: candidate for candidate in candidates}
    print(f"pod_b_candidates={len(candidates)} top_n={top_n}", flush=True)
    print("baseline status=running", flush=True)
    _install_fast_routing_replay()
    baseline_result, baseline_runtime = _run_full_bot(load_config(str(config_path)), input_path)
    baseline = _baseline_payload(baseline_result, baseline_runtime)
    baseline_total = float(baseline["total_pnl_usd"])
    print(
        f"baseline done total={baseline_total:.2f} "
        f"pod_a={baseline['pod_a_pnl_usd']:.2f} pod_b={baseline['pod_b_pnl_usd']:.2f} "
        f"pod_c={baseline['pod_c_pnl_usd']:.2f} seconds={baseline_runtime:.1f}",
        flush=True,
    )

    individual_scenarios = [(candidate.name, [candidate.name]) for candidate in candidates]
    print(f"individual scenarios={len(individual_scenarios)} workers={max_workers}", flush=True)
    individual_results = _with_baseline_target_stats(
        _with_deltas(
            _run_scenarios(
                individual_scenarios,
                config_path=config_path,
                input_path=input_path,
                matrix_path=matrix_path,
                top_n=top_n,
                max_workers=max_workers,
            ),
            baseline_total,
        ),
        baseline_result=baseline_result,
        candidate_map=candidate_map,
    )
    combo_scenarios = _combination_scenarios(candidates, individual_results)
    print(f"combination scenarios={len(combo_scenarios)} workers={max_workers}", flush=True)
    combination_results = _with_baseline_target_stats(
        _with_deltas(
            _run_scenarios(
                combo_scenarios,
                config_path=config_path,
                input_path=input_path,
                matrix_path=matrix_path,
                top_n=top_n,
                max_workers=max_workers,
            ),
            baseline_total,
        ),
        baseline_result=baseline_result,
        candidate_map=candidate_map,
    )
    payload: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00",
            "Z",
        ),
        "config_path": str(config_path),
        "input_path": str(input_path),
        "matrix_path": str(matrix_path),
        "top_n": top_n,
        "baseline": baseline,
        "candidates": [candidate.to_dict() for candidate in candidates],
        "individual_results": individual_results,
        "combination_results": combination_results,
    }
    _write_outputs(payload, output_json, output_md)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Test coin-scoped Pod B matching candidates individually and in combinations.",
    )
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--input", default="server-data/replay_inputs/full_bot_latest_fetch.jsonl")
    parser.add_argument("--matrix", default="server-data/replay_reports/bot_coin_pattern_matrix_20260424.json")
    parser.add_argument("--top-n", type=int, default=3)
    parser.add_argument(
        "--output-json",
        default="server-data/replay_reports/pod_b_candidate_combination_validation_20260425.json",
    )
    parser.add_argument(
        "--output-md",
        default="server-data/replay_reports/pod_b_candidate_combination_validation_20260425.md",
    )
    parser.add_argument("--max-workers", type=int, default=4)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    payload = run_validation(
        config_path=Path(args.config),
        input_path=Path(args.input),
        matrix_path=Path(args.matrix),
        output_json=Path(args.output_json),
        output_md=Path(args.output_md),
        top_n=args.top_n,
        max_workers=max(args.max_workers, 1),
    )
    print(f"output_json={args.output_json}")
    print(f"output_md={args.output_md}")
    best_combo = max(
        payload["combination_results"],
        key=lambda item: float(item.get("delta_vs_baseline_usd", 0.0) or 0.0),
        default=None,
    )
    if best_combo is not None:
        print(
            f"best_combo={best_combo['name']} total={best_combo['total_pnl_usd']:.2f} "
            f"delta={best_combo['delta_vs_baseline_usd']:+.2f}",
            flush=True,
        )


if __name__ == "__main__":
    main()
