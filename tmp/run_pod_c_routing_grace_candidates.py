from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.backtest.full_bot_replay import FullBotBacktestRunner
from app.settings import AppConfig, load_config


INPUT_PATH = "server-data/replay_inputs/full_bot_latest_fetch.jsonl"
CONFIG_PATH = "config/trident.toml"
OUTPUT_DIR = Path(
    "/workspaces/trident/server-data/replay_reports/pod_c_routing_grace_candidates_20260422"
)


@dataclass(slots=True)
class CandidateSummary:
    scenario: str
    description: str
    total_realized_pnl_usd: float
    pod_a_realized_pnl_usd: float
    pod_c_realized_pnl_usd: float
    pod_c_closed_trade_count: int
    pod_c_loss_count: int
    pod_c_close_reasons: dict[str, int]
    routing_revoked_count: int
    routing_revoked_pnl_usd: float
    routing_revoked_by_cluster: dict[str, dict[str, float | int]]
    stop_hit_count: int
    stop_hit_pnl_usd: float
    pod_c_daily_pnl_by_date: dict[str, float]
    delta_total_vs_baseline: float = 0.0
    delta_pod_c_vs_baseline: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _reason_stats(pod: dict[str, object], reason: str) -> tuple[int, float]:
    trades = pod.get("closed_trade_log", [])
    if not isinstance(trades, list):
        return 0, 0.0
    matches = [trade for trade in trades if str(trade.get("close_reason")) == reason]
    return len(matches), round(sum(float(trade.get("pnl_usd") or 0.0) for trade in matches), 2)


def _routing_revoked_by_cluster(pod: dict[str, object]) -> dict[str, dict[str, float | int]]:
    trades = pod.get("closed_trade_log", [])
    if not isinstance(trades, list):
        return {}
    summary: dict[str, dict[str, float | int]] = {}
    for trade in trades:
        if str(trade.get("close_reason")) != "routing_revoked":
            continue
        details = trade.get("setup_details")
        cluster = "unknown"
        if isinstance(details, dict):
            cluster = str(details.get("market_cluster") or "unknown").lower()
        bucket = summary.setdefault(cluster, {"count": 0, "pnl_usd": 0.0})
        bucket["count"] = int(bucket["count"]) + 1
        bucket["pnl_usd"] = round(float(bucket["pnl_usd"]) + float(trade.get("pnl_usd") or 0.0), 2)
    return dict(sorted(summary.items()))


def _scenario_config(
    config: AppConfig,
    routing_revoke_grace_minutes_by_symbol: dict[str, int],
) -> AppConfig:
    merged_overrides = dict(config.trident.execution.routing_revoke_grace_minutes_by_symbol)
    merged_overrides.update(
        {str(symbol).upper(): max(int(minutes), 0) for symbol, minutes in routing_revoke_grace_minutes_by_symbol.items()}
    )
    return replace(
        config,
        trident=replace(
            config.trident,
            execution=replace(
                config.trident.execution,
                routing_revoke_grace_minutes_by_symbol=merged_overrides,
            ),
        ),
    )


def summarize_result(scenario: str, description: str, result: object) -> CandidateSummary:
    pod_a = result.pod_a
    pod_c = result.pod_c
    routing_revoked_count, routing_revoked_pnl = _reason_stats(pod_c, "routing_revoked")
    stop_hit_count, stop_hit_pnl = _reason_stats(pod_c, "stop_hit")
    return CandidateSummary(
        scenario=scenario,
        description=description,
        total_realized_pnl_usd=round(float(result.total_realized_pnl_usd), 2),
        pod_a_realized_pnl_usd=round(float(pod_a.get("realized_pnl_usd", 0.0)), 2),
        pod_c_realized_pnl_usd=round(float(pod_c.get("realized_pnl_usd", 0.0)), 2),
        pod_c_closed_trade_count=int(pod_c.get("closed_trade_count", 0) or 0),
        pod_c_loss_count=int(pod_c.get("loss_count", 0) or 0),
        pod_c_close_reasons={
            str(key): int(value)
            for key, value in dict(pod_c.get("close_reasons", {})).items()
        },
        routing_revoked_count=routing_revoked_count,
        routing_revoked_pnl_usd=routing_revoked_pnl,
        routing_revoked_by_cluster=_routing_revoked_by_cluster(pod_c),
        stop_hit_count=stop_hit_count,
        stop_hit_pnl_usd=stop_hit_pnl,
        pod_c_daily_pnl_by_date={
            str(key): round(float(value), 2)
            for key, value in dict(pod_c.get("pnl_by_date", {})).items()
        },
    )


def run_scenario(
    scenario: str,
    description: str,
    routing_revoke_grace_minutes_by_symbol: dict[str, int],
) -> CandidateSummary:
    config = load_config(CONFIG_PATH)
    runtime_config = _scenario_config(config, routing_revoke_grace_minutes_by_symbol)
    runner = FullBotBacktestRunner(runtime_config, force_enable_all_pods=False)
    result = runner.run_jsonl(INPUT_PATH)
    return summarize_result(scenario, description, result)


def write_outputs(summaries: list[CandidateSummary]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    baseline = next(item for item in summaries if item.scenario == "baseline_current")
    for summary in summaries:
        summary.delta_total_vs_baseline = round(
            summary.total_realized_pnl_usd - baseline.total_realized_pnl_usd,
            2,
        )
        summary.delta_pod_c_vs_baseline = round(
            summary.pod_c_realized_pnl_usd - baseline.pod_c_realized_pnl_usd,
            2,
        )

    (OUTPUT_DIR / "scenario_summary.json").write_text(
        json.dumps([item.to_dict() for item in summaries], indent=2) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# Pod C Routing Grace Candidate Sweep",
        "",
        f"- input: `{INPUT_PATH}`",
        f"- config: `{CONFIG_PATH}`",
        "- note: le global `routing_revoke_grace_minutes` reste a `60m`; les scenarios ci-dessous testent seulement des overrides par symbole Tradfi.",
        "",
        "| Scenario | Delta total | Delta Pod C | Total | Pod C | Pod C trades | Pod C losses | routing_revoked | routing_revoked pnl | stop_hit | stop_hit pnl |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summaries:
        lines.append(
            "| `{scenario}` | {delta_total:+.2f} | {delta_pod_c:+.2f} | {total:.2f} | {pod_c:.2f} | {trades} | {losses} | {routing_count} | {routing_pnl:.2f} | {stop_count} | {stop_pnl:.2f} |".format(
                scenario=item.scenario,
                delta_total=item.delta_total_vs_baseline,
                delta_pod_c=item.delta_pod_c_vs_baseline,
                total=item.total_realized_pnl_usd,
                pod_c=item.pod_c_realized_pnl_usd,
                trades=item.pod_c_closed_trade_count,
                losses=item.pod_c_loss_count,
                routing_count=item.routing_revoked_count,
                routing_pnl=item.routing_revoked_pnl_usd,
                stop_count=item.stop_hit_count,
                stop_pnl=item.stop_hit_pnl_usd,
            )
        )
    lines.append("")
    for item in summaries:
        lines.append(f"## {item.scenario}")
        lines.append("")
        lines.append(f"- description: {item.description}")
        lines.append(f"- routing_revoked_by_cluster: `{json.dumps(item.routing_revoked_by_cluster, sort_keys=True)}`")
        lines.append(f"- pod_c_daily_pnl_by_date: `{json.dumps(item.pod_c_daily_pnl_by_date, sort_keys=True)}`")
        lines.append("")

    (OUTPUT_DIR / "scenario_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    scenarios = [
        (
            "baseline_current",
            "Config repo courante avec grace globale 60m seulement.",
            {},
        ),
        (
            "index_180m",
            "Override a 180m pour XYZ:SP500 et XYZ:XYZ100.",
            {"XYZ:SP500": 180, "XYZ:XYZ100": 180},
        ),
        (
            "index_540m",
            "Override a 540m pour XYZ:SP500 et XYZ:XYZ100.",
            {"XYZ:SP500": 540, "XYZ:XYZ100": 540},
        ),
        (
            "silver_360m",
            "Override a 360m pour XYZ:SILVER seulement.",
            {"XYZ:SILVER": 360},
        ),
        (
            "index_540m_plus_silver_360m",
            "Index a 540m et silver a 360m, sans extension gold.",
            {"XYZ:SP500": 540, "XYZ:XYZ100": 540, "XYZ:SILVER": 360},
        ),
    ]
    summaries: list[CandidateSummary] = []
    for scenario, description, overrides in scenarios:
        summary = run_scenario(scenario, description, overrides)
        summaries.append(summary)
        print(json.dumps(summary.to_dict()))
    write_outputs(summaries)


if __name__ == "__main__":
    main()
