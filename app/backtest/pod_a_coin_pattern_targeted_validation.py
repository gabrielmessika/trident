from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from app.backtest.full_bot_replay import FullBotBacktestResult, FullBotBacktestRunner
from app.settings import AppConfig, PodAPatternVetoConfig, load_config
from app.trident.pod_a import AnchorTrendService
from app.trident.types import PodName, RegimeSnapshot, RiskDecision, SymbolMarketSnapshot


PATTERN_TO_LONG_SETUP = {
    "trend_pullback": "trend_pullback_long",
    "vwap_reclaim": "vwap_reclaim_long",
    "ichimoku_continuation": "ichimoku_continuation_long",
}

NON_EXECUTABLE_REASONS = {
    "ema50_overextension_reversion": "veto/watch BTC-like, pas une entree Pod A long executable generique",
    "funding_reversion": "pas de moteur Pod A funding/reversion",
    "squeeze_breakout": "famille plutot Pod B",
    "ttm_squeeze_release": "famille plutot Pod B",
    "range_mean_reversion": "pas de setup Pod A mean-reversion",
    "stoch_cci_reversion": "pas de setup Pod A mean-reversion",
    "trend_breakout": "pas de setup Pod A trend_breakout strict",
}


@dataclass(slots=True)
class TargetSpec:
    symbol: str
    pattern: str
    setup: str
    action: str
    source_patterns: list[dict[str, object]]

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
    setup: str
    action: str
    verdict: str
    before_total_pnl_usd: float
    after_total_pnl_usd: float
    total_delta_usd: float
    before_pod_a_pnl_usd: float
    after_pod_a_pnl_usd: float
    pod_a_delta_usd: float
    before_target: TargetStats
    after_target: TargetStats
    note: str
    runtime_seconds: float

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["before_target"] = self.before_target.to_dict()
        payload["after_target"] = self.after_target.to_dict()
        return payload


class TargetedPodAFullBotBacktestRunner(FullBotBacktestRunner):
    def __init__(
        self,
        config: AppConfig,
        *,
        target_symbol: str,
        target_setup: str,
    ) -> None:
        super().__init__(config, force_enable_all_pods=False)
        self._target_symbol = target_symbol.upper()
        self._target_setup = target_setup
        self._target_pod_a_service = AnchorTrendService(config)

    def _process_pod_a(
        self,
        *,
        supervisor,
        report,
        snapshots,
        timestamp,
        source_file,
        previous_regime,
        current_regime,
    ) -> None:
        self._add_regime_record(
            report=report,
            timestamp=timestamp,
            source_file=source_file,
            previous_regime=previous_regime,
            current_regime=current_regime,
        )
        prepared_snapshots = supervisor._prepare_snapshots(snapshots)
        supervisor.refresh_symbol_routing(prepared_snapshots)
        contexts = supervisor.pod_a_context_service.build_contexts(
            supervisor.state.regime,
            supervisor._owned_snapshots(PodName.POD_A, prepared_snapshots),
            timestamp=timestamp,
        )
        signals = []
        for context in contexts:
            service = (
                self._target_pod_a_service
                if context.symbol.upper() == self._target_symbol
                else supervisor.pod_a_service
            )
            signal = service.evaluate(context)
            if signal is not None:
                signals.append(signal)
        signals = sorted(signals, key=lambda item: item.confidence, reverse=True)
        previews = [supervisor._build_signal_preview(signal) for signal in signals]
        supervisor.state.pod_a_signal_preview = previews

        pod_allocation = supervisor._pod_a_planning_allocation(signals)
        trade_plans = [
            plan
            for signal in signals
            if (plan := supervisor.pod_a_planner.build_trade_plan(signal, pod_allocation)) is not None
        ]
        date_key = self._date_key(timestamp, source_file)
        for plan in trade_plans:
            plan.setup_details = {
                **dict(plan.setup_details or {}),
                "current_date_key": date_key,
            }
        risk_decisions = self.pod_a_risk_gate.evaluate_many(trade_plans)
        execution = self.pod_a_executor.process_record(
            snapshots=prepared_snapshots,
            risk_decisions=risk_decisions,
            signal_sides_by_symbol={preview.symbol: preview.side for preview in previews},
            timestamp=timestamp,
            allowed_symbols=supervisor.allowed_symbols_for(PodName.POD_A),
        )
        self._record_directional_tick(
            report=report,
            config=self.config,
            current_regime=supervisor.state.regime.value,
            timestamp=timestamp,
            source_file=source_file,
            previews=previews,
            risk_decisions=risk_decisions,
            execution=execution,
            executor=self.pod_a_executor,
            closed_trade_recorder=self._record_pod_a_closed_trade,
        )


def _crypto_symbols(config: AppConfig) -> list[str]:
    return [
        symbol.upper()
        for symbol in (config.hyperliquid.observation_universe or [])
        if ":" not in symbol and symbol.upper() == symbol
    ]


def _target_specs(matrix_path: Path, *, top_n: int) -> tuple[list[TargetSpec], list[dict[str, object]]]:
    payload = json.loads(matrix_path.read_text(encoding="utf-8"))
    by_key: dict[tuple[str, str], TargetSpec] = {}
    skipped: list[dict[str, object]] = []
    for row in payload.get("symbols", []):
        symbol = str(row.get("symbol", "")).upper()
        for item in list(row.get("top_patterns", []))[:top_n]:
            pattern = str(item.get("pattern", ""))
            setup = PATTERN_TO_LONG_SETUP.get(pattern)
            if setup is None:
                skipped.append(
                    {
                        "symbol": symbol,
                        "pattern": pattern,
                        "reason": NON_EXECUTABLE_REASONS.get(pattern, "pas de mapping Pod A long-only"),
                        "interval": item.get("interval"),
                        "hold_bars": item.get("hold_bars"),
                        "expectancy_net_bps": item.get("expectancy_net_bps"),
                        "sample_count": item.get("sample_count"),
                    }
                )
                continue
            key = (symbol, pattern)
            source = {
                "interval": item.get("interval"),
                "hold_bars": item.get("hold_bars"),
                "expectancy_net_bps": item.get("expectancy_net_bps"),
                "sample_count": item.get("sample_count"),
                "side_breakdown": item.get("side_breakdown", {}),
            }
            if key not in by_key:
                by_key[key] = TargetSpec(
                    symbol=symbol,
                    pattern=pattern,
                    setup=setup,
                    action="add" if setup != "trend_pullback_long" or symbol == "TAO" else "ablate",
                    source_patterns=[source],
                )
            else:
                by_key[key].source_patterns.append(source)
    return list(by_key.values()), skipped


def _add_pattern_veto(
    config: AppConfig,
    *,
    name: str,
    symbols: list[str],
    setup: str,
) -> AppConfig:
    rule = PodAPatternVetoConfig(
        name=name,
        enabled=True,
        setups=[setup],
        symbols=symbols,
        sides=["long"],
    )
    return replace(
        config,
        pod_a=replace(
            config.pod_a,
            pattern_vetoes=list(config.pod_a.pattern_vetoes) + [rule],
        ),
    )


def _enable_setup_for_target(
    config: AppConfig,
    target: TargetSpec,
) -> AppConfig:
    setups = list(dict.fromkeys(list(config.pod_a.allowed_setups) + [target.setup]))
    disabled = [item for item in config.pod_a.disabled_setups if item != target.setup]
    config = replace(
        config,
        pod_a=replace(
            config.pod_a,
            allowed_setups=setups,
            disabled_setups=disabled,
        ),
    )
    symbols_to_veto = [symbol for symbol in _crypto_symbols(config) if symbol != target.symbol]
    config = _add_pattern_veto(
        config,
        name=f"targeted_{target.symbol.lower()}_{target.setup}_only",
        symbols=symbols_to_veto,
        setup=target.setup,
    )
    if target.symbol == "TAO":
        config = _reactivate_tao_for_pod_a_test(config)
    return config


def _reactivate_tao_for_pod_a_test(config: AppConfig) -> AppConfig:
    hyperliquid = replace(
        config.hyperliquid,
        tradable_blocked_symbols=[
            symbol for symbol in config.hyperliquid.tradable_blocked_symbols if symbol.upper() != "TAO"
        ],
    )
    pod_b = replace(
        config.pod_b,
        bis_blocked_symbols=list(dict.fromkeys(list(config.pod_b.bis_blocked_symbols) + ["TAO"])),
    )
    return replace(config, hyperliquid=hyperliquid, pod_b=pod_b)


def _scenario_config(config_path: Path, target: TargetSpec) -> AppConfig:
    config = load_config(str(config_path))
    if target.action == "ablate":
        return _add_pattern_veto(
            config,
            name=f"ablate_{target.symbol.lower()}_{target.setup}",
            symbols=[target.symbol],
            setup=target.setup,
        )
    return _enable_setup_for_target(config, target)


def _run_scenario(
    config: AppConfig,
    *,
    input_path: Path,
    target: TargetSpec | None = None,
) -> tuple[FullBotBacktestResult, float]:
    started = time.perf_counter()
    if target is not None and target.action == "add" and target.setup != "trend_pullback_long":
        runner = TargetedPodAFullBotBacktestRunner(
            config,
            target_symbol=target.symbol,
            target_setup=target.setup,
        )
    else:
        runner = FullBotBacktestRunner(config, force_enable_all_pods=False)
    result = runner.run_jsonl(input_path=input_path, dedupe_by_timestamp=True)
    return result, time.perf_counter() - started


def _target_stats(result: FullBotBacktestResult, *, symbol: str, setup: str) -> TargetStats:
    trades = [
        trade
        for trade in (result.pod_a.get("closed_trade_log", []) or [])
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


def _verdict(target: TargetSpec, delta: float, after_stats: TargetStats) -> tuple[str, str]:
    if target.action == "ablate":
        if delta < 0.0:
            return ("keep", "Le retrait cible degrade le full-bot; garder ce coin/pattern.")
        if delta > 0.0:
            return ("reject", "Le retrait cible ameliore le full-bot; exclure ce coin/pattern.")
        return ("neutral", "Le retrait cible n'a pas d'effet mesurable.")
    if delta > 0.0 and after_stats.pnl_usd > 0.0 and after_stats.trades > 0:
        return ("keep_candidate", "Ajout cible positif en full-bot et PnL cible positif.")
    if after_stats.trades == 0:
        return ("no_effect", "Aucun trade cible cloture sur cette fenetre.")
    return ("reject", "Ajout cible non additif en full-bot.")


def _render_markdown(payload: dict[str, object]) -> str:
    baseline = payload["baseline"]
    lines = [
        "# Pod A Coin Pattern Targeted Validation",
        "",
        f"- Input: `{payload['input_path']}`",
        f"- Config baseline: `{payload['config_path']}`",
        f"- Pattern matrix: `{payload['matrix_path']}`",
        f"- Matrix top N per coin: `{payload['top_n']}`",
        f"- Baseline total PnL: `{baseline['total_pnl_usd']:.2f}`",
        f"- Baseline Pod A PnL: `{baseline['pod_a_pnl_usd']:.2f}`",
        "- Shorts: `disabled`",
        "- TAO: debloque uniquement dans les scenarios TAO Pod A; Pod B garde TAO bloque.",
        "",
        "## Decisions",
        "",
        "| Coin | Pattern | Action | Verdict | Before total | After total | Delta | Before target | After target | Note |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for item in payload["results"]:
        before = item["before_target"]
        after = item["after_target"]
        lines.append(
            f"| {item['symbol']} | {item['pattern']} | {item['action']} | {item['verdict']} | "
            f"{item['before_total_pnl_usd']:.2f} | {item['after_total_pnl_usd']:.2f} | "
            f"{item['total_delta_usd']:.2f} | "
            f"{before['pnl_usd']:.2f}/{before['trades']}t | "
            f"{after['pnl_usd']:.2f}/{after['trades']}t | {item['note']} |"
        )
    lines.extend(
        [
            "",
            "## Non Executable / Skipped",
            "",
            "| Coin | Pattern | Reason | Matrix edge |",
            "|---|---|---|---|",
        ]
    )
    for item in payload["skipped"]:
        lines.append(
            f"| {item['symbol']} | {item['pattern']} | {item['reason']} | "
            f"{item.get('expectancy_net_bps')}bps/{item.get('sample_count')}n |"
        )
    lines.append("")
    return "\n".join(lines)


def run_validation(
    *,
    config_path: Path,
    input_path: Path,
    matrix_path: Path,
    output_json: Path,
    output_md: Path,
    top_n: int,
) -> dict[str, object]:
    targets, skipped = _target_specs(matrix_path, top_n=top_n)
    print(f"targets={len(targets)} skipped={len(skipped)} top_n={top_n}", flush=True)
    baseline, baseline_runtime = _run_scenario(load_config(str(config_path)), input_path=input_path)
    print(
        f"baseline total={baseline.total_realized_pnl_usd} "
        f"pod_a={baseline.pod_a.get('realized_pnl_usd', 0.0)} "
        f"seconds={baseline_runtime:.1f}",
        flush=True,
    )
    results: list[TargetResult] = []
    for index, target in enumerate(targets, start=1):
        print(
            f"[{index}/{len(targets)}] {target.symbol} {target.pattern} "
            f"action={target.action} status=running",
            flush=True,
        )
        scenario_config = _scenario_config(config_path, target)
        scenario, runtime = _run_scenario(
            scenario_config,
            input_path=input_path,
            target=target,
        )
        before_stats = _target_stats(baseline, symbol=target.symbol, setup=target.setup)
        after_stats = _target_stats(scenario, symbol=target.symbol, setup=target.setup)
        total_delta = round(scenario.total_realized_pnl_usd - baseline.total_realized_pnl_usd, 4)
        pod_a_delta = round(
            float(scenario.pod_a.get("realized_pnl_usd", 0.0) or 0.0)
            - float(baseline.pod_a.get("realized_pnl_usd", 0.0) or 0.0),
            4,
        )
        verdict, note = _verdict(target, total_delta, after_stats)
        result = TargetResult(
            symbol=target.symbol,
            pattern=target.pattern,
            setup=target.setup,
            action=target.action,
            verdict=verdict,
            before_total_pnl_usd=float(baseline.total_realized_pnl_usd),
            after_total_pnl_usd=float(scenario.total_realized_pnl_usd),
            total_delta_usd=total_delta,
            before_pod_a_pnl_usd=float(baseline.pod_a.get("realized_pnl_usd", 0.0) or 0.0),
            after_pod_a_pnl_usd=float(scenario.pod_a.get("realized_pnl_usd", 0.0) or 0.0),
            pod_a_delta_usd=pod_a_delta,
            before_target=before_stats,
            after_target=after_stats,
            note=note,
            runtime_seconds=round(runtime, 3),
        )
        results.append(result)
        print(
            f"[{index}/{len(targets)}] {target.symbol} {target.pattern} "
            f"after={result.after_total_pnl_usd} delta={result.total_delta_usd} "
            f"target_pnl={after_stats.pnl_usd} target_trades={after_stats.trades} "
            f"verdict={verdict} seconds={runtime:.1f}",
            flush=True,
        )
    payload = {
        "input_path": str(input_path),
        "config_path": str(config_path),
        "matrix_path": str(matrix_path),
        "top_n": top_n,
        "baseline": {
            "total_pnl_usd": float(baseline.total_realized_pnl_usd),
            "pod_a_pnl_usd": float(baseline.pod_a.get("realized_pnl_usd", 0.0) or 0.0),
            "pod_b_pnl_usd": float(baseline.pod_b.get("realized_pnl_usd", 0.0) or 0.0),
            "pod_c_pnl_usd": float(baseline.pod_c.get("realized_pnl_usd", 0.0) or 0.0),
            "records_processed": int(baseline.records_processed),
            "duplicate_timestamps_skipped": int(baseline.duplicate_timestamps_skipped),
            "runtime_seconds": round(baseline_runtime, 3),
        },
        "targets": [item.to_dict() for item in targets],
        "results": [item.to_dict() for item in results],
        "skipped": skipped,
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_md.write_text(_render_markdown(payload), encoding="utf-8")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run targeted coin+pattern Pod A long-only validations.",
    )
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--input", default="server-data/replay_inputs/full_bot_latest_fetch.jsonl")
    parser.add_argument("--matrix", default="server-data/replay_reports/bot_coin_pattern_matrix_20260424.json")
    parser.add_argument("--top-n", type=int, default=2)
    parser.add_argument(
        "--output-json",
        default="server-data/replay_reports/pod_a_coin_pattern_targeted_validation_20260424.json",
    )
    parser.add_argument(
        "--output-md",
        default="server-data/replay_reports/pod_a_coin_pattern_targeted_validation_20260424.md",
    )
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
    )
    print(f"output_json={args.output_json}")
    print(f"output_md={args.output_md}")
    print(json.dumps(payload["results"], indent=2))


if __name__ == "__main__":
    main()
