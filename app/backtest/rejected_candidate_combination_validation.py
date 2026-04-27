from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.backtest import full_bot_replay as full_bot_replay_module
from app.backtest.full_bot_replay import FullBotBacktestResult, FullBotBacktestRunner
from app.settings import AppConfig, PodAPatternVetoConfig, load_config


class _NoopRoutingReplayRunner:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def run_jsonl(self, *, input_path: str | Path, dedupe_by_timestamp: bool = True):
        class _NoopRoutingResult:
            def to_dict(self) -> dict[str, object]:
                return {
                    "skipped": True,
                    "reason": "omitted_by_rejected_candidate_combination_validation",
                }

        return _NoopRoutingResult()


def _install_fast_routing_replay() -> None:
    full_bot_replay_module.RoutingReplayRunner = _NoopRoutingReplayRunner


@dataclass(frozen=True, slots=True)
class Candidate:
    name: str
    scope: str
    coin: str
    pattern: str
    engine: str
    setup: str
    kind: str
    note: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


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
    target_stats: dict[str, object]
    rejections: dict[str, int]
    runtime_seconds: float

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


EXECUTABLE_CANDIDATES: list[Candidate] = [
    Candidate(
        name="ARB_ichimoku_continuation_long",
        scope="crypto",
        coin="ARB",
        pattern="ichimoku_continuation",
        engine="Pod A",
        setup="ichimoku_continuation_long",
        kind="pod_a_setup",
        note="Ancien refus: ajout cible non additif.",
    ),
    Candidate(
        name="AVAX_vwap_reclaim_long",
        scope="crypto",
        coin="AVAX",
        pattern="vwap_reclaim",
        engine="Pod A",
        setup="vwap_reclaim_long",
        kind="pod_a_setup",
        note="Ancien refus: ajout cible negatif.",
    ),
    Candidate(
        name="BNB_ichimoku_continuation_long",
        scope="crypto",
        coin="BNB",
        pattern="ichimoku_continuation",
        engine="Pod A",
        setup="ichimoku_continuation_long",
        kind="pod_a_setup",
        note="Ancien refus: ajout cible non additif.",
    ),
    Candidate(
        name="BNB_vwap_reclaim_long",
        scope="crypto",
        coin="BNB",
        pattern="vwap_reclaim",
        engine="Pod A",
        setup="vwap_reclaim_long",
        kind="pod_a_setup",
        note="Ancien refus: aucun trade cible.",
    ),
    Candidate(
        name="ETH_vwap_reclaim_long",
        scope="crypto",
        coin="ETH",
        pattern="vwap_reclaim",
        engine="Pod A",
        setup="vwap_reclaim_long",
        kind="pod_a_setup",
        note="Ancien refus: aucun trade cible.",
    ),
    Candidate(
        name="HYPE_trend_pullback_long_unveto",
        scope="crypto",
        coin="HYPE",
        pattern="trend_pullback",
        engine="Pod A",
        setup="trend_pullback_long",
        kind="pod_a_remove_hype_veto",
        note="Teste le pattern HYPE rejete en retirant le veto cible courant.",
    ),
    Candidate(
        name="LTC_ema50_overextension_veto",
        scope="crypto",
        coin="LTC",
        pattern="ema50_overextension_reversion",
        engine="Pod A",
        setup="trend_pullback_long veto",
        kind="pod_a_overextension_veto",
        note="Ancien refus_no_effect: veto non declenche.",
    ),
    Candidate(
        name="LTC_ichimoku_continuation_long",
        scope="crypto",
        coin="LTC",
        pattern="ichimoku_continuation",
        engine="Pod A",
        setup="ichimoku_continuation_long",
        kind="pod_a_setup",
        note="Ancien refus: ajout cible non additif.",
    ),
    Candidate(
        name="NEAR_vwap_reclaim_long",
        scope="crypto",
        coin="NEAR",
        pattern="vwap_reclaim",
        engine="Pod A",
        setup="vwap_reclaim_long",
        kind="pod_a_setup",
        note="Ancien refus: ajout cible negatif.",
    ),
    Candidate(
        name="TAO_trend_pullback_long",
        scope="crypto",
        coin="TAO",
        pattern="trend_pullback",
        engine="Pod A",
        setup="trend_pullback_long",
        kind="pod_a_unblock_tao",
        note="Ancien refus: ajout cible TAO negatif.",
    ),
    Candidate(
        name="XRP_ichimoku_continuation_long",
        scope="crypto",
        coin="XRP",
        pattern="ichimoku_continuation",
        engine="Pod A",
        setup="ichimoku_continuation_long",
        kind="pod_a_setup",
        note="Ancien refus: ajout cible non additif.",
    ),
    Candidate(
        name="ZRO_ema50_overextension_veto",
        scope="crypto",
        coin="ZRO",
        pattern="ema50_overextension_reversion",
        engine="Pod A",
        setup="trend_pullback_long veto",
        kind="pod_a_overextension_veto",
        note="Ancien refus_no_effect: veto non declenche.",
    ),
    Candidate(
        name="gold_soft_extension_veto",
        scope="xyz",
        coin="XYZ:GOLD",
        pattern="gold_soft_extension_veto",
        engine="Pod C",
        setup="pattern veto",
        kind="pod_c_gold_veto",
        note="Ancien refus_no_effect: veto non declenche.",
    ),
    Candidate(
        name="gold_strong_neutral_veto",
        scope="xyz",
        coin="XYZ:GOLD",
        pattern="gold_strong_neutral_veto",
        engine="Pod C",
        setup="pattern veto",
        kind="pod_c_gold_veto",
        note="Ancien refus_no_effect: veto non declenche.",
    ),
    Candidate(
        name="gold_medium_neutral_veto",
        scope="xyz",
        coin="XYZ:GOLD",
        pattern="gold_medium_neutral_veto",
        engine="Pod C",
        setup="pattern veto",
        kind="pod_c_gold_veto",
        note="Ancien refus_no_effect: veto non declenche.",
    ),
]


NON_EXECUTABLE_CANDIDATES: list[dict[str, str]] = [
    {
        "coin": "AAVE",
        "pattern": "funding_reversion",
        "reason": "pas de moteur funding/reversion Pod A.",
    },
    {
        "coin": "ADA",
        "pattern": "funding_reversion",
        "reason": "pas de moteur funding/reversion Pod A.",
    },
    {
        "coin": "AVAX",
        "pattern": "ttm_squeeze_release",
        "reason": "setup disponible seulement dans Pod B, qui reste desactive.",
    },
    {
        "coin": "BCH",
        "pattern": "ttm_squeeze_release",
        "reason": "setup disponible seulement dans Pod B, qui reste desactive.",
    },
    {
        "coin": "BTC",
        "pattern": "funding_reversion",
        "reason": "pas de moteur funding/reversion Pod A.",
    },
    {
        "coin": "DOGE",
        "pattern": "ttm_squeeze_release",
        "reason": "setup disponible seulement dans Pod B, qui reste desactive.",
    },
    {
        "coin": "ENA",
        "pattern": "trend_breakout",
        "reason": "pas de setup Pod A trend_breakout strict.",
    },
    {
        "coin": "HYPE",
        "pattern": "funding_reversion",
        "reason": "pas de moteur funding/reversion Pod A.",
    },
    {
        "coin": "LINK",
        "pattern": "ttm_squeeze_release",
        "reason": "setup disponible seulement dans Pod B, qui reste desactive.",
    },
    {
        "coin": "NEAR",
        "pattern": "squeeze_breakout",
        "reason": "setup disponible seulement dans Pod B, qui reste desactive.",
    },
    {
        "coin": "SOL",
        "pattern": "funding_reversion",
        "reason": "pas de moteur funding/reversion Pod A.",
    },
    {
        "coin": "SUI",
        "pattern": "ttm_squeeze_release",
        "reason": "setup disponible seulement dans Pod B, qui reste desactive.",
    },
    {
        "coin": "TON",
        "pattern": "stoch_cci_reversion",
        "reason": "pas de moteur mean-reversion Pod A.",
    },
    {
        "coin": "TON",
        "pattern": "ttm_squeeze_release",
        "reason": "setup disponible seulement dans Pod B, qui reste desactive.",
    },
    {
        "coin": "ZEC",
        "pattern": "range_mean_reversion",
        "reason": "pas de moteur mean-reversion Pod A.",
    },
    {
        "coin": "ZRO",
        "pattern": "ttm_squeeze_release",
        "reason": "setup disponible seulement dans Pod B, qui reste desactive.",
    },
]


def _candidate_by_name() -> dict[str, Candidate]:
    return {item.name: item for item in EXECUTABLE_CANDIDATES}


def _crypto_symbols(config: AppConfig) -> list[str]:
    return [
        symbol.upper()
        for symbol in config.hyperliquid.observation_universe
        if ":" not in symbol and symbol.upper() == symbol
    ]


def _remove_pod_a_vetoes(config: AppConfig, names: set[str]) -> AppConfig:
    return replace(
        config,
        pod_a=replace(
            config.pod_a,
            pattern_vetoes=[
                item for item in config.pod_a.pattern_vetoes if item.name not in names
            ],
        ),
    )


def _remove_pod_c_vetoes(config: AppConfig, names: set[str]) -> AppConfig:
    return replace(
        config,
        pod_c=replace(
            config.pod_c,
            pattern_vetoes=[
                item for item in config.pod_c.pattern_vetoes if item.name not in names
            ],
        ),
    )


def _enable_pod_a_setups_for_symbols(
    config: AppConfig,
    setup_symbols: dict[str, set[str]],
) -> AppConfig:
    if not setup_symbols:
        return config
    allowed_setups = list(config.pod_a.allowed_setups)
    disabled_setups = list(config.pod_a.disabled_setups)
    pattern_vetoes = list(config.pod_a.pattern_vetoes)
    crypto_symbols = set(_crypto_symbols(config))

    for setup, symbols in sorted(setup_symbols.items()):
        if setup not in allowed_setups:
            allowed_setups.append(setup)
        disabled_setups = [item for item in disabled_setups if item != setup]
        allowed_symbols = {symbol.upper() for symbol in symbols}
        veto_name = f"rejected_targeted_{setup}_only"
        pattern_vetoes = [item for item in pattern_vetoes if item.name != veto_name]
        blocked_symbols = sorted(crypto_symbols - allowed_symbols)
        if blocked_symbols:
            pattern_vetoes.append(
                PodAPatternVetoConfig(
                    name=veto_name,
                    enabled=True,
                    symbols=blocked_symbols,
                    sides=["long"],
                    setups=[setup],
                )
            )

    return replace(
        config,
        pod_a=replace(
            config.pod_a,
            allowed_setups=allowed_setups,
            disabled_setups=disabled_setups,
            pattern_vetoes=pattern_vetoes,
        ),
    )


def _add_overextension_veto(config: AppConfig, symbol: str) -> AppConfig:
    name = f"{symbol.lower()}_overextension_4h_targeted"
    config = _remove_pod_a_vetoes(config, {name})
    rule = PodAPatternVetoConfig(
        name=name,
        enabled=True,
        symbols=[symbol.upper()],
        sides=["long"],
        setups=["trend_pullback_long"],
        min_rsi21_4h=65.0,
        min_ema50_distance_4h_pct=4.0,
        min_ema50_distance_4h_atr=2.0,
        min_btc_overextension_score=0.70,
    )
    return replace(
        config,
        pod_a=replace(config.pod_a, pattern_vetoes=list(config.pod_a.pattern_vetoes) + [rule]),
    )


def _gold_veto_rule(name: str) -> PodAPatternVetoConfig:
    if name == "gold_soft_extension_veto":
        return PodAPatternVetoConfig(
            name=name,
            enabled=True,
            setups=["tradfi_continuation_long"],
            sides=["long"],
            market_clusters=["gold"],
            trend_buckets=["soft"],
            structure_buckets=["strong"],
            vwap_buckets=["extension"],
        )
    if name == "gold_strong_neutral_veto":
        return PodAPatternVetoConfig(
            name=name,
            enabled=True,
            setups=["tradfi_continuation_long"],
            sides=["long"],
            market_clusters=["gold"],
            trend_buckets=["strong"],
            structure_buckets=["strong"],
            vwap_buckets=["neutral"],
        )
    if name == "gold_medium_neutral_veto":
        return PodAPatternVetoConfig(
            name=name,
            enabled=True,
            setups=["tradfi_continuation_long"],
            sides=["long"],
            market_clusters=["gold"],
            trend_buckets=["medium"],
            structure_buckets=["strong"],
            vwap_buckets=["neutral"],
        )
    raise ValueError(f"unknown gold veto {name}")


def _add_gold_veto(config: AppConfig, name: str) -> AppConfig:
    config = _remove_pod_c_vetoes(config, {name})
    return replace(
        config,
        pod_c=replace(
            config.pod_c,
            pattern_vetoes=list(config.pod_c.pattern_vetoes) + [_gold_veto_rule(name)],
        ),
    )


def _unblock_tao(config: AppConfig) -> AppConfig:
    return replace(
        config,
        hyperliquid=replace(
            config.hyperliquid,
            tradable_blocked_symbols=[
                symbol
                for symbol in config.hyperliquid.tradable_blocked_symbols
                if symbol.upper() != "TAO"
            ],
        ),
    )


def _apply_candidates(config: AppConfig, candidates: list[Candidate]) -> AppConfig:
    setup_symbols: dict[str, set[str]] = {}
    for candidate in candidates:
        if candidate.kind == "pod_a_setup":
            setup_symbols.setdefault(candidate.setup, set()).add(candidate.coin.upper())

    config = _enable_pod_a_setups_for_symbols(config, setup_symbols)

    for candidate in candidates:
        if candidate.kind == "pod_a_remove_hype_veto":
            config = _remove_pod_a_vetoes(config, {"hype_trend_pullback_long_targeted"})
        elif candidate.kind == "pod_a_unblock_tao":
            config = _unblock_tao(config)
        elif candidate.kind == "pod_a_overextension_veto":
            config = _add_overextension_veto(config, candidate.coin)
        elif candidate.kind == "pod_c_gold_veto":
            config = _add_gold_veto(config, candidate.name)
        elif candidate.kind == "pod_a_setup":
            continue
        else:
            raise ValueError(f"unsupported candidate kind {candidate.kind}")
    return config


def _run_full_bot(config: AppConfig, input_path: Path) -> tuple[FullBotBacktestResult, float]:
    started = time.perf_counter()
    result = FullBotBacktestRunner(config, force_enable_all_pods=False).run_jsonl(
        input_path=input_path,
        dedupe_by_timestamp=True,
    )
    return result, time.perf_counter() - started


def _stats_for_setup(
    result: FullBotBacktestResult,
    *,
    pod: str,
    symbol: str,
    setup: str,
) -> dict[str, object]:
    payload = result.pod_c if pod == "pod_c" else result.pod_a
    trades = [
        item
        for item in (payload.get("closed_trade_log", []) or [])
        if str(item.get("symbol", "")).upper() == symbol.upper()
        and str(item.get("setup", "")) == setup
    ]
    pnl = round(sum(float(item.get("pnl_usd", 0.0) or 0.0) for item in trades), 2)
    return {
        "trades": len(trades),
        "pnl_usd": pnl,
        "wins": sum(1 for item in trades if float(item.get("pnl_usd", 0.0) or 0.0) >= 0.0),
        "losses": sum(1 for item in trades if float(item.get("pnl_usd", 0.0) or 0.0) < 0.0),
    }


def _target_stats(result: FullBotBacktestResult, candidates: list[Candidate]) -> dict[str, object]:
    stats: dict[str, object] = {}
    for candidate in candidates:
        if candidate.kind in {"pod_a_setup", "pod_a_remove_hype_veto", "pod_a_unblock_tao"}:
            stats[candidate.name] = _stats_for_setup(
                result,
                pod="pod_a",
                symbol=candidate.coin,
                setup="trend_pullback_long"
                if candidate.kind in {"pod_a_remove_hype_veto", "pod_a_unblock_tao"}
                else candidate.setup,
            )
        elif candidate.kind == "pod_a_overextension_veto":
            reason = f"pattern_veto_{candidate.coin.lower()}_overextension_4h_targeted"
            stats[candidate.name] = {
                "veto_rejections": int(
                    (result.pod_a.get("rejections_by_reason", {}) or {}).get(reason, 0) or 0
                )
            }
        elif candidate.kind == "pod_c_gold_veto":
            reason = f"pattern_veto_{candidate.name}"
            stats[candidate.name] = {
                "veto_rejections": int(
                    (result.pod_c.get("rejections_by_reason", {}) or {}).get(reason, 0) or 0
                )
            }
    return stats


def _scenario_result(
    *,
    name: str,
    candidate_names: list[str],
    result: FullBotBacktestResult,
    runtime: float,
) -> ScenarioResult:
    by_name = _candidate_by_name()
    candidates = [by_name[item] for item in candidate_names]
    return ScenarioResult(
        name=name,
        candidate_names=candidate_names,
        total_pnl_usd=float(result.total_realized_pnl_usd),
        pod_a_pnl_usd=float(result.pod_a.get("realized_pnl_usd", 0.0) or 0.0),
        pod_b_pnl_usd=float(result.pod_b.get("realized_pnl_usd", 0.0) or 0.0),
        pod_c_pnl_usd=float(result.pod_c.get("realized_pnl_usd", 0.0) or 0.0),
        pod_a_trades=int(result.pod_a.get("closed_trade_count", 0) or 0),
        pod_b_trades=int(result.pod_b.get("closed_trade_count", 0) or 0),
        pod_c_trades=int(result.pod_c.get("closed_trade_count", 0) or 0),
        target_stats=_target_stats(result, candidates),
        rejections={
            "pod_a": dict(result.pod_a.get("rejections_by_reason", {}) or {}),
            "pod_c": dict(result.pod_c.get("rejections_by_reason", {}) or {}),
        },
        runtime_seconds=round(runtime, 3),
    )


def _run_scenario_worker(args: tuple[str, list[str], str, str]) -> dict[str, object]:
    name, candidate_names, config_path, input_path = args
    _install_fast_routing_replay()
    by_name = _candidate_by_name()
    candidates = [by_name[item] for item in candidate_names]
    config = _apply_candidates(load_config(config_path), candidates)
    result, runtime = _run_full_bot(config, Path(input_path))
    return _scenario_result(
        name=name,
        candidate_names=candidate_names,
        result=result,
        runtime=runtime,
    ).to_dict()


def _run_baseline(config_path: Path, input_path: Path) -> dict[str, object]:
    _install_fast_routing_replay()
    result, runtime = _run_full_bot(load_config(str(config_path)), input_path)
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


def _write_outputs(payload: dict[str, object], output_json: Path, output_md: Path) -> None:
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    output_md.write_text(_render_markdown(payload), encoding="utf-8")


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


def _combo_scenarios(individual_results: list[dict[str, object]]) -> list[tuple[str, list[str]]]:
    delta_by_name = {
        str(item["candidate_names"][0]): float(item.get("delta_vs_baseline_usd", 0.0) or 0.0)
        for item in individual_results
        if len(item.get("candidate_names", [])) == 1
    }

    def names_where(predicate) -> list[str]:
        return [candidate.name for candidate in EXECUTABLE_CANDIDATES if predicate(candidate)]

    scenarios: list[tuple[str, list[str]]] = []
    positive = [name for name, delta in delta_by_name.items() if delta > 0.0]
    non_negative = [name for name, delta in delta_by_name.items() if delta >= 0.0]
    small_loss_or_better = [
        name for name, delta in delta_by_name.items() if delta >= -20.0
    ]
    if positive:
        scenarios.append(("combo_positive_individual", positive))
    if non_negative and set(non_negative) != set(positive):
        scenarios.append(("combo_non_negative_individual", non_negative))
    if small_loss_or_better and set(small_loss_or_better) != set(non_negative):
        scenarios.append(("combo_small_loss_or_better", small_loss_or_better))
    scenarios.extend(
        [
            (
                "combo_all_rejected_executable",
                [candidate.name for candidate in EXECUTABLE_CANDIDATES],
            ),
            (
                "combo_all_vwap_reclaim",
                names_where(lambda item: item.pattern == "vwap_reclaim"),
            ),
            (
                "combo_all_ichimoku",
                names_where(lambda item: item.pattern == "ichimoku_continuation"),
            ),
            (
                "combo_all_overextension_no_effect_vetoes",
                names_where(lambda item: item.kind == "pod_a_overextension_veto"),
            ),
            (
                "combo_all_gold_vetoes",
                names_where(lambda item: item.kind == "pod_c_gold_veto"),
            ),
        ]
    )
    seen: set[tuple[str, ...]] = set()
    unique: list[tuple[str, list[str]]] = []
    for name, candidate_names in scenarios:
        cleaned = [item for item in candidate_names if item]
        key = tuple(sorted(cleaned))
        if not cleaned or key in seen:
            continue
        seen.add(key)
        unique.append((name, cleaned))
    return unique


def _run_scenarios(
    scenarios: list[tuple[str, list[str]]],
    *,
    config_path: Path,
    input_path: Path,
    max_workers: int,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    if not scenarios:
        return results
    workers = max(1, min(max_workers, len(scenarios)))
    with ProcessPoolExecutor(max_workers=workers) as executor:
        future_by_name = {
            executor.submit(
                _run_scenario_worker,
                (name, candidate_names, str(config_path), str(input_path)),
            ): name
            for name, candidate_names in scenarios
        }
        for future in as_completed(future_by_name):
            name = future_by_name[future]
            result = future.result()
            results.append(result)
            print(
                f"{name} done total={result['total_pnl_usd']:.2f} "
                f"pod_a={result['pod_a_pnl_usd']:.2f} pod_c={result['pod_c_pnl_usd']:.2f} "
                f"seconds={result['runtime_seconds']:.1f}",
                flush=True,
            )
    return sorted(results, key=lambda item: str(item["name"]))


def _render_markdown(payload: dict[str, object]) -> str:
    baseline = payload["baseline"]
    baseline_total = float(baseline["total_pnl_usd"])
    lines = [
        "# Rejected Candidate Combination Validation",
        "",
        f"- Generated at: `{payload['generated_at']}`",
        f"- Input: `{payload['input_path']}`",
        f"- Config baseline: `{payload['config_path']}`",
        "- Baseline = accepted compatible set already active, Pod B disabled.",
        "- Full-bot execution is active; the extra routing summary replay is omitted for speed.",
        f"- Baseline total PnL: `{baseline_total:.2f}` "
        f"(Pod A `{baseline['pod_a_pnl_usd']:.2f}`, Pod C `{baseline['pod_c_pnl_usd']:.2f}`)",
        "",
        "## Individual Rejected Candidates",
        "",
        "| Candidate | Coin | Pattern | Setup | Total | Delta | Pod A | Pod C | Target / veto stats |",
        "|---|---|---|---|---:|---:|---:|---:|---|",
    ]
    candidates = _candidate_by_name()
    for row in sorted(
        payload["individual_results"],
        key=lambda item: float(item.get("delta_vs_baseline_usd", 0.0) or 0.0),
        reverse=True,
    ):
        candidate = candidates[str(row["candidate_names"][0])]
        stats = row.get("target_stats", {}).get(candidate.name, {})
        lines.append(
            f"| {candidate.name} | {candidate.coin} | {candidate.pattern} | {candidate.setup} | "
            f"{float(row['total_pnl_usd']):.2f} | "
            f"{float(row['delta_vs_baseline_usd']):+.2f} | "
            f"{float(row['pod_a_pnl_usd']):.2f} | {float(row['pod_c_pnl_usd']):.2f} | "
            f"`{json.dumps(stats, sort_keys=True)}` |"
        )
    lines.extend(
        [
            "",
            "## Combination Replays",
            "",
            "| Scenario | Candidates | Total | Delta | Pod A | Pod C |",
            "|---|---:|---:|---:|---:|---:|",
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
            f"{float(row['pod_a_pnl_usd']):.2f} | {float(row['pod_c_pnl_usd']):.2f} |"
        )
    lines.extend(
        [
            "",
            "## Non Executable Without New Engine",
            "",
            "| Coin | Pattern | Reason |",
            "|---|---|---|",
        ]
    )
    for item in payload["non_executable_candidates"]:
        lines.append(f"| {item['coin']} | {item['pattern']} | {item['reason']} |")
    lines.append("")
    return "\n".join(lines)


def run_validation(
    *,
    config_path: Path,
    input_path: Path,
    output_json: Path,
    output_md: Path,
    max_workers: int,
) -> dict[str, object]:
    print("baseline status=running", flush=True)
    baseline = _run_baseline(config_path, input_path)
    baseline_total = float(baseline["total_pnl_usd"])
    print(
        f"baseline done total={baseline_total:.2f} "
        f"pod_a={baseline['pod_a_pnl_usd']:.2f} pod_c={baseline['pod_c_pnl_usd']:.2f} "
        f"seconds={baseline['runtime_seconds']:.1f}",
        flush=True,
    )

    individual_scenarios = [
        (candidate.name, [candidate.name]) for candidate in EXECUTABLE_CANDIDATES
    ]
    print(f"individual scenarios={len(individual_scenarios)} workers={max_workers}", flush=True)
    individual_results = _with_deltas(
        _run_scenarios(
            individual_scenarios,
            config_path=config_path,
            input_path=input_path,
            max_workers=max_workers,
        ),
        baseline_total,
    )
    combo_scenarios = _combo_scenarios(individual_results)
    print(f"combination scenarios={len(combo_scenarios)} workers={max_workers}", flush=True)
    combination_results = _with_deltas(
        _run_scenarios(
            combo_scenarios,
            config_path=config_path,
            input_path=input_path,
            max_workers=max_workers,
        ),
        baseline_total,
    )
    payload: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00",
            "Z",
        ),
        "config_path": str(config_path),
        "input_path": str(input_path),
        "baseline": baseline,
        "executable_candidates": [item.to_dict() for item in EXECUTABLE_CANDIDATES],
        "non_executable_candidates": NON_EXECUTABLE_CANDIDATES,
        "individual_results": individual_results,
        "combination_results": combination_results,
    }
    _write_outputs(payload, output_json, output_md)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay rejected executable candidates one by one and in combinations.",
    )
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--input", default="server-data/replay_inputs/full_bot_latest_fetch.jsonl")
    parser.add_argument(
        "--output-json",
        default="server-data/replay_reports/rejected_candidate_combination_validation_20260425.json",
    )
    parser.add_argument(
        "--output-md",
        default="server-data/replay_reports/rejected_candidate_combination_validation_20260425.md",
    )
    parser.add_argument("--max-workers", type=int, default=3)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    payload = run_validation(
        config_path=Path(args.config),
        input_path=Path(args.input),
        output_json=Path(args.output_json),
        output_md=Path(args.output_md),
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
            "best_combo="
            f"{best_combo['name']} total={best_combo['total_pnl_usd']:.2f} "
            f"delta={best_combo['delta_vs_baseline_usd']:+.2f}",
            flush=True,
        )


if __name__ == "__main__":
    main()
