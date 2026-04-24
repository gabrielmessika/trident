from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from app.backtest import full_bot_replay as full_bot_replay_module
from app.backtest.full_bot_replay import FullBotBacktestResult, FullBotBacktestRunner
from app.settings import (
    AllocationConfig,
    AppConfig,
    PodAPatternVetoConfig,
    RegimeAllocations,
    load_config,
)


IMPLEMENTABLE_PATTERNS = {
    "ema50_overextension_reversion",
    "ttm_squeeze_release",
    "squeeze_breakout",
}

POD_B_SETUP_BY_PATTERN = {
    "ttm_squeeze_release": "ttm_squeeze_release_long",
    "squeeze_breakout": "compression_breakout_long",
}

NON_IMPLEMENTED_REASONS = {
    "funding_reversion": (
        "needs a dedicated funding-reversion sleeve: replay snapshots expose current "
        "funding, but not the candle-derived funding z-score + BB/stoch/CCI trigger "
        "used by the research pattern"
    ),
    "range_mean_reversion": "needs a mean-reversion pod; current Pod A/Pod B are trend/breakout engines",
    "stoch_cci_reversion": "needs a mean-reversion pod; current Pod A/Pod B are trend/breakout engines",
    "trend_breakout": "not mapped to a production setup; Pod B compression is not the same trigger",
    "vwap_reclaim": "already covered by the Pod A targeted validation, not part of this implementation pass",
    "ichimoku_continuation": "already covered by the Pod A targeted validation, not part of this implementation pass",
    "trend_pullback": "already covered by the Pod A targeted validation, not part of this implementation pass",
}


class _NoopRoutingReplayRunner:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def run_jsonl(self, *, input_path: str | Path, dedupe_by_timestamp: bool = True):
        class _NoopRoutingResult:
            def to_dict(self) -> dict[str, object]:
                return {
                    "skipped": True,
                    "reason": "omitted_by_coin_pattern_implementation_validation",
                }

        return _NoopRoutingResult()


def _install_fast_routing_replay() -> None:
    full_bot_replay_module.RoutingReplayRunner = _NoopRoutingReplayRunner


@dataclass(slots=True)
class TargetSpec:
    symbol: str
    pattern: str
    engine: str
    setup: str
    source_patterns: list[dict[str, object]]

    @property
    def key(self) -> str:
        return f"{self.symbol}:{self.pattern}:{self.engine}:{self.setup}"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class TargetStats:
    trades: int
    pnl_usd: float
    wins: int
    losses: int

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class TargetResult:
    symbol: str
    pattern: str
    engine: str
    setup: str
    verdict: str
    before_total_pnl_usd: float
    after_total_pnl_usd: float
    total_delta_usd: float
    before_pod_a_pnl_usd: float
    after_pod_a_pnl_usd: float
    pod_a_delta_usd: float
    before_pod_b_pnl_usd: float
    after_pod_b_pnl_usd: float
    pod_b_delta_usd: float
    before_target: TargetStats
    after_target: TargetStats
    veto_rejections: int
    note: str
    control_runtime_seconds: float
    runtime_seconds: float

    @property
    def key(self) -> str:
        return f"{self.symbol}:{self.pattern}:{self.engine}:{self.setup}"

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["before_target"] = self.before_target.to_dict()
        payload["after_target"] = self.after_target.to_dict()
        return payload


def _crypto_symbols(config: AppConfig) -> list[str]:
    return [
        symbol.upper()
        for symbol in (config.hyperliquid.observation_universe or [])
        if ":" not in symbol and symbol.upper() == symbol
    ]


def _source_pattern(item: dict[str, object]) -> dict[str, object]:
    return {
        "interval": item.get("interval"),
        "hold_bars": item.get("hold_bars"),
        "expectancy_net_bps": item.get("expectancy_net_bps"),
        "sample_count": item.get("sample_count"),
        "side_breakdown": item.get("side_breakdown", {}),
    }


def _target_specs(
    matrix_path: Path,
    *,
    top_n: int,
) -> tuple[list[TargetSpec], list[dict[str, object]]]:
    payload = json.loads(matrix_path.read_text(encoding="utf-8"))
    by_key: dict[tuple[str, str, str, str], TargetSpec] = {}
    skipped: list[dict[str, object]] = []

    for row in payload.get("symbols", []):
        symbol = str(row.get("symbol", "")).upper()
        if not symbol:
            continue
        for item in list(row.get("top_patterns", []))[:top_n]:
            pattern = str(item.get("pattern", "")).strip()
            source = _source_pattern(item)
            if pattern == "ema50_overextension_reversion":
                engine = "pod_a_veto"
                setup = "trend_pullback_long"
            elif pattern in POD_B_SETUP_BY_PATTERN:
                engine = "pod_b_slot"
                setup = POD_B_SETUP_BY_PATTERN[pattern]
            else:
                skipped.append(
                    {
                        "symbol": symbol,
                        "pattern": pattern,
                        "reason": NON_IMPLEMENTED_REASONS.get(
                            pattern,
                            "no implementation mapping in this pass",
                        ),
                        **source,
                    }
                )
                continue
            key = (symbol, pattern, engine, setup)
            if key not in by_key:
                by_key[key] = TargetSpec(
                    symbol=symbol,
                    pattern=pattern,
                    engine=engine,
                    setup=setup,
                    source_patterns=[source],
                )
            else:
                by_key[key].source_patterns.append(source)

    return list(by_key.values()), skipped


def _pod_b_slot_allocations() -> RegimeAllocations:
    return RegimeAllocations(
        trend_expansion=AllocationConfig(pod_a=0.70, pod_b=0.10, pod_c=0.20, cash=0.00),
        range_auction=AllocationConfig(pod_a=0.10, pod_b=0.10, pod_c=0.15, cash=0.65),
        panic_squeeze=AllocationConfig(pod_a=0.10, pod_b=0.10, pod_c=0.05, cash=0.75),
        dead_zone=AllocationConfig(pod_a=0.00, pod_b=0.20, pod_c=0.05, cash=0.75),
    )


def _overextension_veto_name(target: TargetSpec) -> str:
    return f"{target.symbol.lower()}_overextension_4h_targeted"


def _remove_overextension_veto(config: AppConfig, target: TargetSpec) -> AppConfig:
    veto_name = _overextension_veto_name(target)
    return replace(
        config,
        pod_a=replace(
            config.pod_a,
            pattern_vetoes=[
                rule
                for rule in config.pod_a.pattern_vetoes
                if rule.name != veto_name
            ],
        ),
    )


def _add_overextension_veto(config: AppConfig, target: TargetSpec) -> AppConfig:
    config = _remove_overextension_veto(config, target)
    rule = PodAPatternVetoConfig(
        name=_overextension_veto_name(target),
        enabled=True,
        symbols=[target.symbol],
        sides=["long"],
        setups=["trend_pullback_long"],
        min_rsi21_4h=65.0,
        min_ema50_distance_4h_pct=4.0,
        min_ema50_distance_4h_atr=2.0,
        min_btc_overextension_score=0.70,
    )
    return replace(
        config,
        pod_a=replace(
            config.pod_a,
            pattern_vetoes=list(config.pod_a.pattern_vetoes) + [rule],
        ),
    )


def _enable_targeted_pod_b_slot(config: AppConfig, target: TargetSpec) -> AppConfig:
    blocked = [
        symbol
        for symbol in _crypto_symbols(config)
        if symbol.upper() != target.symbol.upper()
    ]
    routing = replace(
        config.trident.routing,
        symbol_pod_overrides={
            **dict(config.trident.routing.symbol_pod_overrides),
            target.symbol.upper(): "pod_b",
        },
    )
    trident = replace(
        config.trident,
        routing=routing,
        allocations=_pod_b_slot_allocations(),
    )
    return replace(
        config,
        trident=trident,
        pod_b=replace(
            config.pod_b,
            enabled=True,
            bis_blocked_symbols=blocked,
            bis_enable_longs=True,
            bis_enable_shorts=False,
            bis_enabled_setups=[target.setup],
        ),
    )


def _disable_targeted_pod_b_setup(config: AppConfig) -> AppConfig:
    return replace(
        config,
        pod_b=replace(
            config.pod_b,
            # Empty means "all setups enabled" in BreakoutService, so use a
            # sentinel that cannot match a production setup.
            bis_enabled_setups=["__disabled_for_target_control__"],
        ),
    )


def _scenario_config(config_path: Path, target: TargetSpec) -> AppConfig:
    config = load_config(str(config_path))
    if target.engine == "pod_a_veto":
        return _add_overextension_veto(config, target)
    if target.engine == "pod_b_slot":
        return _enable_targeted_pod_b_slot(config, target)
    raise ValueError(f"Unsupported target engine: {target.engine}")


def _control_config(config_path: Path, target: TargetSpec) -> AppConfig:
    config = load_config(str(config_path))
    if target.engine == "pod_a_veto":
        return _remove_overextension_veto(config, target)
    if target.engine == "pod_b_slot":
        return _disable_targeted_pod_b_setup(_enable_targeted_pod_b_slot(config, target))
    return config


def _run_full_bot(config: AppConfig, *, input_path: Path) -> tuple[FullBotBacktestResult, float]:
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
    setup: str,
) -> TargetStats:
    payload = result.pod_b if pod == "pod_b" else result.pod_a
    trades = [
        trade
        for trade in (payload.get("closed_trade_log", []) or [])
        if str(trade.get("symbol", "")).upper() == symbol.upper()
        and str(trade.get("setup", "")) == setup
    ]
    pnl = round(sum(float(trade.get("pnl_usd", 0.0) or 0.0) for trade in trades), 2)
    return TargetStats(
        trades=len(trades),
        pnl_usd=pnl,
        wins=sum(1 for trade in trades if float(trade.get("pnl_usd", 0.0) or 0.0) >= 0.0),
        losses=sum(1 for trade in trades if float(trade.get("pnl_usd", 0.0) or 0.0) < 0.0),
    )


def _target_stats(result: FullBotBacktestResult, target: TargetSpec) -> TargetStats:
    pod = "pod_b" if target.engine == "pod_b_slot" else "pod_a"
    return _trade_stats(result, pod=pod, symbol=target.symbol, setup=target.setup)


def _veto_rejections(result: FullBotBacktestResult, target: TargetSpec) -> int:
    if target.engine != "pod_a_veto":
        return 0
    reason = f"pattern_veto_{target.symbol.lower()}_overextension_4h_targeted"
    return int((result.pod_a.get("rejections_by_reason", {}) or {}).get(reason, 0) or 0)


def _verdict(
    target: TargetSpec,
    *,
    total_delta: float,
    after_stats: TargetStats,
    veto_rejections: int,
) -> tuple[str, str]:
    if target.engine == "pod_a_veto":
        if veto_rejections <= 0:
            return (
                "no_effect",
                "No targeted veto fired on this replay window.",
            )
        if total_delta > 0.0:
            return (
                "keep_candidate",
                "Targeted overextension veto improves full-bot PnL.",
            )
        return (
            "reject",
            "Targeted overextension veto does not improve full-bot PnL.",
        )
    if after_stats.trades <= 0:
        return (
            "no_effect",
            "No targeted Pod B trade closed on this replay window.",
        )
    if total_delta > 0.0 and after_stats.pnl_usd > 0.0:
        return (
            "keep_candidate",
            "Targeted Pod B slot improves full-bot PnL with positive target PnL.",
        )
    return (
        "reject",
        "Targeted Pod B slot is not additive under current routing/allocation constraints.",
    )


def _baseline_payload(result: FullBotBacktestResult, runtime_seconds: float) -> dict[str, object]:
    return {
        "total_pnl_usd": float(result.total_realized_pnl_usd),
        "pod_a_pnl_usd": float(result.pod_a.get("realized_pnl_usd", 0.0) or 0.0),
        "pod_b_pnl_usd": float(result.pod_b.get("realized_pnl_usd", 0.0) or 0.0),
        "pod_c_pnl_usd": float(result.pod_c.get("realized_pnl_usd", 0.0) or 0.0),
        "records_processed": int(result.records_processed),
        "duplicate_timestamps_skipped": int(result.duplicate_timestamps_skipped),
        "runtime_seconds": round(runtime_seconds, 3),
    }


def _render_markdown(payload: dict[str, object]) -> str:
    baseline = payload.get("baseline") or {}
    lines = [
        "# Coin Pattern Implementation Validation",
        "",
        f"- Input: `{payload['input_path']}`",
        f"- Config baseline: `{payload['config_path']}`",
        f"- Pattern matrix: `{payload['matrix_path']}`",
        f"- Matrix top N per coin: `{payload['top_n']}`",
        f"- Baseline total PnL: `{float(baseline.get('total_pnl_usd', 0.0)):.2f}`",
        f"- Baseline Pod A PnL: `{float(baseline.get('pod_a_pnl_usd', 0.0)):.2f}`",
        f"- Baseline Pod B PnL: `{float(baseline.get('pod_b_pnl_usd', 0.0)):.2f}`",
        "- Shorts: `disabled`",
        "- Pod B slot test: targeted symbol is routed exclusively to Pod B with the compare slot allocation "
        "`trend 70/10/20`, `range 10/10/15`, `panic 10/10/5`, `dead 0/20/5`.",
        "- Pod B deltas are measured versus a target-specific control replay with the same routing/allocation "
        "but with the tested Pod B setup disabled.",
        "- Pod A veto deltas are measured versus a target-specific control replay with that veto removed, "
        "so reruns stay comparable after a candidate is promoted to config.",
        "- Full-bot routing still runs during execution; the extra routing summary replay is omitted for speed.",
        "",
        "## Results",
        "",
        "| Coin | Pattern | Engine | Setup | Verdict | Before | After | Delta | Target after | Pod A delta | Pod B delta | Vetoes | Note |",
        "|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for item in payload.get("results", []):
        after = item["after_target"]
        lines.append(
            f"| {item['symbol']} | {item['pattern']} | {item['engine']} | {item['setup']} | "
            f"{item['verdict']} | {item['before_total_pnl_usd']:.2f} | "
            f"{item['after_total_pnl_usd']:.2f} | {item['total_delta_usd']:.2f} | "
            f"{after['pnl_usd']:.2f}/{after['trades']}t | "
            f"{item['pod_a_delta_usd']:.2f} | {item['pod_b_delta_usd']:.2f} | "
            f"{item['veto_rejections']} | {item['note']} |"
        )
    lines.extend(
        [
            "",
            "## Not Implemented In This Pass",
            "",
            "| Coin | Pattern | Reason | Matrix edge |",
            "|---|---|---|---|",
        ]
    )
    for item in payload.get("skipped", []):
        lines.append(
            f"| {item['symbol']} | {item['pattern']} | {item['reason']} | "
            f"{item.get('expectancy_net_bps')}bps/{item.get('sample_count')}n |"
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


def _load_resume_results(output_json: Path) -> list[dict[str, object]]:
    if not output_json.exists():
        return []
    try:
        payload = json.loads(output_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    results = payload.get("results", [])
    return list(results) if isinstance(results, list) else []


def run_validation(
    *,
    config_path: Path,
    input_path: Path,
    matrix_path: Path,
    output_json: Path,
    output_md: Path,
    top_n: int,
    resume: bool,
) -> dict[str, object]:
    targets, skipped = _target_specs(matrix_path, top_n=top_n)
    print(f"targets={len(targets)} skipped={len(skipped)} top_n={top_n}", flush=True)
    _install_fast_routing_replay()
    baseline, baseline_runtime = _run_full_bot(load_config(str(config_path)), input_path=input_path)
    print(
        f"baseline total={baseline.total_realized_pnl_usd} "
        f"pod_a={baseline.pod_a.get('realized_pnl_usd', 0.0)} "
        f"pod_b={baseline.pod_b.get('realized_pnl_usd', 0.0)} "
        f"seconds={baseline_runtime:.1f}",
        flush=True,
    )

    resumed_results = _load_resume_results(output_json) if resume else []
    done_keys = {
        f"{item.get('symbol')}:{item.get('pattern')}:{item.get('engine')}:{item.get('setup')}"
        for item in resumed_results
        if isinstance(item, dict)
    }
    results: list[dict[str, object]] = list(resumed_results)
    payload: dict[str, object] = {
        "input_path": str(input_path),
        "config_path": str(config_path),
        "matrix_path": str(matrix_path),
        "top_n": top_n,
        "baseline": _baseline_payload(baseline, baseline_runtime),
        "targets": [item.to_dict() for item in targets],
        "results": results,
        "skipped": skipped,
    }
    _write_outputs(payload, output_json=output_json, output_md=output_md)

    for index, target in enumerate(targets, start=1):
        if target.key in done_keys:
            print(f"[{index}/{len(targets)}] {target.key} status=resume_skip", flush=True)
            continue
        control_runtime = 0.0
        comparison = baseline
        if target.engine in {"pod_a_veto", "pod_b_slot"}:
            print(f"[{index}/{len(targets)}] {target.key} status=control", flush=True)
            comparison, control_runtime = _run_full_bot(
                _control_config(config_path, target),
                input_path=input_path,
            )
            print(
                f"[{index}/{len(targets)}] {target.key} control={comparison.total_realized_pnl_usd} "
                f"seconds={control_runtime:.1f}",
                flush=True,
            )
        print(f"[{index}/{len(targets)}] {target.key} status=running", flush=True)
        scenario, runtime = _run_full_bot(
            _scenario_config(config_path, target),
            input_path=input_path,
        )
        before_stats = _target_stats(comparison, target)
        after_stats = _target_stats(scenario, target)
        total_delta = round(scenario.total_realized_pnl_usd - comparison.total_realized_pnl_usd, 4)
        pod_a_delta = round(
            float(scenario.pod_a.get("realized_pnl_usd", 0.0) or 0.0)
            - float(comparison.pod_a.get("realized_pnl_usd", 0.0) or 0.0),
            4,
        )
        pod_b_delta = round(
            float(scenario.pod_b.get("realized_pnl_usd", 0.0) or 0.0)
            - float(comparison.pod_b.get("realized_pnl_usd", 0.0) or 0.0),
            4,
        )
        veto_rejections = _veto_rejections(scenario, target)
        verdict, note = _verdict(
            target,
            total_delta=total_delta,
            after_stats=after_stats,
            veto_rejections=veto_rejections,
        )
        result = TargetResult(
            symbol=target.symbol,
            pattern=target.pattern,
            engine=target.engine,
            setup=target.setup,
            verdict=verdict,
            before_total_pnl_usd=float(comparison.total_realized_pnl_usd),
            after_total_pnl_usd=float(scenario.total_realized_pnl_usd),
            total_delta_usd=total_delta,
            before_pod_a_pnl_usd=float(comparison.pod_a.get("realized_pnl_usd", 0.0) or 0.0),
            after_pod_a_pnl_usd=float(scenario.pod_a.get("realized_pnl_usd", 0.0) or 0.0),
            pod_a_delta_usd=pod_a_delta,
            before_pod_b_pnl_usd=float(comparison.pod_b.get("realized_pnl_usd", 0.0) or 0.0),
            after_pod_b_pnl_usd=float(scenario.pod_b.get("realized_pnl_usd", 0.0) or 0.0),
            pod_b_delta_usd=pod_b_delta,
            before_target=before_stats,
            after_target=after_stats,
            veto_rejections=veto_rejections,
            note=note,
            control_runtime_seconds=round(control_runtime, 3),
            runtime_seconds=round(runtime, 3),
        )
        results.append(result.to_dict())
        payload["results"] = results
        _write_outputs(payload, output_json=output_json, output_md=output_md)
        print(
            f"[{index}/{len(targets)}] {target.key} after={result.after_total_pnl_usd} "
            f"delta={result.total_delta_usd} target_pnl={after_stats.pnl_usd} "
            f"target_trades={after_stats.trades} verdict={verdict} seconds={runtime:.1f}",
            flush=True,
        )

    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run targeted implementation validations for non-Pod-A research candidates.",
    )
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--input", default="server-data/replay_inputs/full_bot_latest_fetch.jsonl")
    parser.add_argument("--matrix", default="server-data/replay_reports/bot_coin_pattern_matrix_20260424.json")
    parser.add_argument("--top-n", type=int, default=2)
    parser.add_argument(
        "--output-json",
        default="server-data/replay_reports/coin_pattern_implementation_validation_20260424.json",
    )
    parser.add_argument(
        "--output-md",
        default="server-data/replay_reports/coin_pattern_implementation_validation_20260424.md",
    )
    parser.add_argument("--resume", action="store_true")
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
        resume=bool(args.resume),
    )
    print(f"output_json={args.output_json}")
    print(f"output_md={args.output_md}")
    print(json.dumps(payload["results"], indent=2))


if __name__ == "__main__":
    main()
