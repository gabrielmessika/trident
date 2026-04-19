from __future__ import annotations

import argparse
import copy
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[2]))

from app.backtest.full_bot_replay import FullBotBacktestRunner
from app.backtest.snapshot_loader import SnapshotLoader
from app.settings import AppConfig, load_config
from app.trident.supervisor import TridentSupervisor
from app.trident.types import RegimeSnapshot


def _normalize_timestamp(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text.replace("+00:00", "Z")


def _round2(value: float) -> float:
    return round(float(value), 2)


def _variant_config(base: AppConfig, variant: str) -> AppConfig:
    if variant == "hybrid_moderate_a":
        cfg = copy.deepcopy(base)
        cfg.trident.regime.crypto_v2_enabled = True
        cfg.trident.regime.crypto_v2_mode = "hybrid_upgrade_only"
        cfg.trident.regime.adx_trend_threshold = 20.0
        cfg.trident.regime.trend_structure_threshold = 0.27
        cfg.trident.regime.dead_zone_atr_threshold = 0.38
        cfg.trident.regime.dead_zone_range_threshold = 65.0
        cfg.trident.regime.switch_confirmation_bars = 4
        cfg.trident.regime.trend_confirmation_bars = 1
        cfg.trident.regime.panic_confirmation_bars = 1
        return cfg
    if variant == "hybrid_separate_thresholds":
        cfg = copy.deepcopy(base)
        cfg.trident.regime.crypto_v2_enabled = True
        cfg.trident.regime.crypto_v2_mode = "hybrid_upgrade_only"
        cfg.trident.regime.crypto_v2_adx_trend_threshold = 20.0
        cfg.trident.regime.crypto_v2_trend_structure_threshold = 0.27
        cfg.trident.regime.crypto_v2_dead_zone_atr_threshold = 0.38
        cfg.trident.regime.crypto_v2_dead_zone_range_threshold = 65.0
        cfg.trident.regime.crypto_v2_switch_confirmation_bars = 4
        cfg.trident.regime.crypto_v2_trend_confirmation_bars = 1
        cfg.trident.regime.crypto_v2_panic_confirmation_bars = 1
        return cfg
    raise ValueError(f"Unsupported variant: {variant}")


def _run_report(config: AppConfig, input_path: str, output_path: Path) -> dict[str, object]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result = FullBotBacktestRunner(config, force_enable_all_pods=False).run_jsonl(
        input_path,
        dedupe_by_timestamp=True,
        report_output=output_path,
    )
    return result.to_dict()


def _build_regime_timeline(config: AppConfig, input_path: str) -> dict[str, dict[str, object]]:
    supervisor = TridentSupervisor(
        config=config,
        profile="crypto-regime-v2-autopsy",
        mode="dry-run",
    )
    loader = SnapshotLoader()
    timeline: dict[str, dict[str, object]] = {}
    for record in loader.iter_merged_jsonl(input_path):
        timestamp = _normalize_timestamp(record.timestamp)
        snapshot = RegimeSnapshot(**record.regime_snapshot)
        cluster_regime_snapshots = {
            cluster: RegimeSnapshot(**snap)
            for cluster, snap in (record.cluster_regime_snapshots or {}).items()
            if isinstance(snap, dict)
        }
        supervisor.apply_regime_snapshot(
            snapshot,
            cluster_regime_snapshots=cluster_regime_snapshots,
        )
        timeline[timestamp] = {
            "date": timestamp[:10],
            "effective_regime": supervisor.state.regime.value,
            "raw_regime": supervisor.state.raw_regime.value,
            "adx": float(record.regime_snapshot.get("adx", 0.0) or 0.0),
            "atr_ratio": float(record.regime_snapshot.get("atr_ratio", 0.0) or 0.0),
            "range_width_bps": float(
                record.regime_snapshot.get("range_width_bps", 0.0) or 0.0
            ),
            "structure_score": float(
                record.regime_snapshot.get("structure_score", 0.0) or 0.0
            ),
            "breadth_pct": float(record.regime_snapshot.get("breadth_pct", 0.0) or 0.0),
            "dispersion_pct": float(
                record.regime_snapshot.get("dispersion_pct", 0.0) or 0.0
            ),
            "leader_trend_score": float(
                record.regime_snapshot.get("leader_trend_score", 0.0) or 0.0
            ),
            "coherence_score": float(
                record.regime_snapshot.get("coherence_score", 0.0) or 0.0
            ),
            "active_symbol_count": int(
                record.regime_snapshot.get("active_symbol_count", 0) or 0
            ),
            "btc_impulse": bool(record.regime_snapshot.get("btc_impulse", False)),
        }
    return timeline


def _trade_key(trade: dict[str, object]) -> tuple[str, str, str, str]:
    return (
        str(trade.get("symbol", "")),
        str(trade.get("side", "")),
        str(trade.get("setup", "")),
        _normalize_timestamp(trade.get("opened_at")),
    )


def _trade_view(
    trade: dict[str, object],
    *,
    baseline_timeline: dict[str, dict[str, object]],
    variant_timeline: dict[str, dict[str, object]],
) -> dict[str, object]:
    opened_at = _normalize_timestamp(trade.get("opened_at"))
    baseline = baseline_timeline.get(opened_at, {})
    variant = variant_timeline.get(opened_at, {})
    return {
        "date": str(trade.get("date", "")),
        "symbol": str(trade.get("symbol", "")),
        "setup": str(trade.get("setup", "")),
        "side": str(trade.get("side", "")),
        "pnl_usd": _round2(float(trade.get("pnl_usd", 0.0) or 0.0)),
        "close_reason": str(trade.get("close_reason", "")),
        "opened_at": opened_at,
        "closed_at": _normalize_timestamp(trade.get("closed_at")),
        "baseline_entry_regime": str(baseline.get("effective_regime", "")),
        "variant_entry_regime": str(variant.get("effective_regime", "")),
        "baseline_entry_raw_regime": str(baseline.get("raw_regime", "")),
        "variant_entry_raw_regime": str(variant.get("raw_regime", "")),
        "breadth_pct": float(variant.get("breadth_pct", 0.0) or 0.0),
        "coherence_score": float(variant.get("coherence_score", 0.0) or 0.0),
        "leader_trend_score": float(variant.get("leader_trend_score", 0.0) or 0.0),
        "atr_ratio": float(variant.get("atr_ratio", 0.0) or 0.0),
        "structure_score": float(variant.get("structure_score", 0.0) or 0.0),
        "active_symbol_count": int(variant.get("active_symbol_count", 0) or 0),
        "btc_impulse": bool(variant.get("btc_impulse", False)),
    }


def _metric_summary(trades: list[dict[str, object]]) -> dict[str, float]:
    if not trades:
        return {
            "trades": 0,
            "pnl_usd": 0.0,
            "avg_breadth_pct": 0.0,
            "avg_coherence_score": 0.0,
            "avg_leader_trend_score": 0.0,
            "avg_atr_ratio": 0.0,
            "avg_structure_score": 0.0,
        }
    count = len(trades)
    return {
        "trades": count,
        "pnl_usd": _round2(sum(float(item.get("pnl_usd", 0.0) or 0.0) for item in trades)),
        "avg_breadth_pct": round(
            sum(float(item.get("breadth_pct", 0.0) or 0.0) for item in trades) / count,
            4,
        ),
        "avg_coherence_score": round(
            sum(float(item.get("coherence_score", 0.0) or 0.0) for item in trades) / count,
            4,
        ),
        "avg_leader_trend_score": round(
            sum(float(item.get("leader_trend_score", 0.0) or 0.0) for item in trades) / count,
            4,
        ),
        "avg_atr_ratio": round(
            sum(float(item.get("atr_ratio", 0.0) or 0.0) for item in trades) / count,
            4,
        ),
        "avg_structure_score": round(
            sum(float(item.get("structure_score", 0.0) or 0.0) for item in trades) / count,
            4,
        ),
    }


def _ranked_stats(
    trades: list[dict[str, object]],
    *,
    key_name: str,
) -> list[dict[str, object]]:
    buckets: dict[str, list[dict[str, object]]] = defaultdict(list)
    for trade in trades:
        key = str(trade.get(key_name, ""))
        buckets[key].append(trade)
    ranked: list[dict[str, object]] = []
    for key, items in buckets.items():
        pnl = sum(float(item.get("pnl_usd", 0.0) or 0.0) for item in items)
        ranked.append(
            {
                key_name: key,
                "trades": len(items),
                "pnl_usd": _round2(pnl),
                "avg_pnl_usd": _round2(pnl / len(items)),
            }
        )
    return sorted(ranked, key=lambda item: (item["pnl_usd"], item["trades"]))


def _entry_pair_stats(trades: list[dict[str, object]]) -> list[dict[str, object]]:
    buckets: dict[str, list[dict[str, object]]] = defaultdict(list)
    for trade in trades:
        pair = (
            f"{trade.get('baseline_entry_regime', '')}"
            f"->{trade.get('variant_entry_regime', '')}"
        )
        buckets[pair].append(trade)
    ranked: list[dict[str, object]] = []
    for pair, items in buckets.items():
        pnl = sum(float(item.get("pnl_usd", 0.0) or 0.0) for item in items)
        ranked.append(
            {
                "entry_pair": pair,
                "trades": len(items),
                "pnl_usd": _round2(pnl),
                "avg_pnl_usd": _round2(pnl / len(items)),
            }
        )
    return sorted(ranked, key=lambda item: (item["pnl_usd"], item["trades"]))


def _daily_summary(
    baseline_report: dict[str, object],
    variant_report: dict[str, object],
) -> dict[str, dict[str, object]]:
    all_dates = sorted(
        set(baseline_report["pod_a"].get("pnl_by_date", {}))
        | set(variant_report["pod_a"].get("pnl_by_date", {}))
        | set(baseline_report["pod_c"].get("pnl_by_date", {}))
        | set(variant_report["pod_c"].get("pnl_by_date", {}))
    )
    result: dict[str, dict[str, object]] = {}
    for date in all_dates:
        base_a = float(baseline_report["pod_a"].get("pnl_by_date", {}).get(date, 0.0) or 0.0)
        base_c = float(baseline_report["pod_c"].get("pnl_by_date", {}).get(date, 0.0) or 0.0)
        var_a = float(variant_report["pod_a"].get("pnl_by_date", {}).get(date, 0.0) or 0.0)
        var_c = float(variant_report["pod_c"].get("pnl_by_date", {}).get(date, 0.0) or 0.0)
        result[date] = {
            "baseline_total_pnl_usd": _round2(base_a + base_c),
            "variant_total_pnl_usd": _round2(var_a + var_c),
            "delta_total_pnl_usd": _round2((var_a + var_c) - (base_a + base_c)),
            "baseline_pod_a_pnl_usd": _round2(base_a),
            "variant_pod_a_pnl_usd": _round2(var_a),
            "delta_pod_a_pnl_usd": _round2(var_a - base_a),
            "baseline_pod_c_pnl_usd": _round2(base_c),
            "variant_pod_c_pnl_usd": _round2(var_c),
            "delta_pod_c_pnl_usd": _round2(var_c - base_c),
            "baseline_pod_a_trades": int(
                sum(
                    1
                    for trade in baseline_report["pod_a"].get("closed_trade_log", [])
                    if trade.get("date") == date
                )
            ),
            "variant_pod_a_trades": int(
                sum(
                    1
                    for trade in variant_report["pod_a"].get("closed_trade_log", [])
                    if trade.get("date") == date
                )
            ),
            "baseline_pod_c_trades": int(
                sum(
                    1
                    for trade in baseline_report["pod_c"].get("closed_trade_log", [])
                    if trade.get("date") == date
                )
            ),
            "variant_pod_c_trades": int(
                sum(
                    1
                    for trade in variant_report["pod_c"].get("closed_trade_log", [])
                    if trade.get("date") == date
                )
            ),
        }
    return result


def _timeline_diff_summary(
    baseline_timeline: dict[str, dict[str, object]],
    variant_timeline: dict[str, dict[str, object]],
) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    total = Counter()
    by_date: dict[str, Counter[str]] = defaultdict(Counter)
    for timestamp, baseline in baseline_timeline.items():
        variant = variant_timeline.get(timestamp)
        if variant is None:
            continue
        baseline_regime = str(baseline.get("effective_regime", ""))
        variant_regime = str(variant.get("effective_regime", ""))
        if baseline_regime == variant_regime:
            continue
        key = f"{baseline_regime}->{variant_regime}"
        total[key] += 1
        by_date[str(baseline.get("date", ""))][key] += 1
    return dict(total), {date: dict(counter) for date, counter in sorted(by_date.items())}


def _build_payload(
    *,
    baseline_report: dict[str, object],
    variant_report: dict[str, object],
    baseline_timeline: dict[str, dict[str, object]],
    variant_timeline: dict[str, dict[str, object]],
    variant_name: str,
) -> dict[str, object]:
    baseline_pod_a_trades = baseline_report["pod_a"].get("closed_trade_log", [])
    variant_pod_a_trades = variant_report["pod_a"].get("closed_trade_log", [])
    baseline_keys = {_trade_key(trade) for trade in baseline_pod_a_trades}
    variant_keys = {_trade_key(trade) for trade in variant_pod_a_trades}

    variant_trade_views = [
        _trade_view(
            trade,
            baseline_timeline=baseline_timeline,
            variant_timeline=variant_timeline,
        )
        for trade in variant_pod_a_trades
    ]
    hybrid_only_trade_views = [
        trade
        for trade, key in zip(variant_trade_views, map(_trade_key, variant_pod_a_trades))
        if key not in baseline_keys
    ]
    baseline_only_trade_views = [
        _trade_view(
            trade,
            baseline_timeline=baseline_timeline,
            variant_timeline=variant_timeline,
        )
        for trade in baseline_pod_a_trades
        if _trade_key(trade) not in variant_keys
    ]
    upgraded_hybrid_only = [
        trade
        for trade in hybrid_only_trade_views
        if trade.get("baseline_entry_regime") != trade.get("variant_entry_regime")
    ]
    upgraded_hybrid_only_losers = [
        trade for trade in upgraded_hybrid_only if float(trade.get("pnl_usd", 0.0) or 0.0) < 0.0
    ]
    upgraded_hybrid_only_winners = [
        trade for trade in upgraded_hybrid_only if float(trade.get("pnl_usd", 0.0) or 0.0) > 0.0
    ]
    diff_total, diff_by_date = _timeline_diff_summary(baseline_timeline, variant_timeline)

    daily = _daily_summary(baseline_report, variant_report)
    return {
        "variant_name": variant_name,
        "summary": {
            "baseline_total_realized_pnl_usd": _round2(
                float(baseline_report.get("total_realized_pnl_usd", 0.0) or 0.0)
            ),
            "variant_total_realized_pnl_usd": _round2(
                float(variant_report.get("total_realized_pnl_usd", 0.0) or 0.0)
            ),
            "delta_total_realized_pnl_usd": _round2(
                float(variant_report.get("total_realized_pnl_usd", 0.0) or 0.0)
                - float(baseline_report.get("total_realized_pnl_usd", 0.0) or 0.0)
            ),
            "baseline_pod_a_realized_pnl_usd": _round2(
                float(baseline_report["pod_a"].get("realized_pnl_usd", 0.0) or 0.0)
            ),
            "variant_pod_a_realized_pnl_usd": _round2(
                float(variant_report["pod_a"].get("realized_pnl_usd", 0.0) or 0.0)
            ),
            "baseline_pod_c_realized_pnl_usd": _round2(
                float(baseline_report["pod_c"].get("realized_pnl_usd", 0.0) or 0.0)
            ),
            "variant_pod_c_realized_pnl_usd": _round2(
                float(variant_report["pod_c"].get("realized_pnl_usd", 0.0) or 0.0)
            ),
            "baseline_regime_transition_count": int(
                baseline_report["pod_a"].get("regime_transition_count", 0) or 0
            ),
            "variant_regime_transition_count": int(
                variant_report["pod_a"].get("regime_transition_count", 0) or 0
            ),
            "baseline_pod_a_closed_trade_count": int(
                baseline_report["pod_a"].get("closed_trade_count", 0) or 0
            ),
            "variant_pod_a_closed_trade_count": int(
                variant_report["pod_a"].get("closed_trade_count", 0) or 0
            ),
        },
        "daily": daily,
        "timeline_diff_counts_total": diff_total,
        "timeline_diff_counts_by_date": diff_by_date,
        "pod_a": {
            "variant_entry_pairs": _entry_pair_stats(variant_trade_views),
            "hybrid_only_entry_pairs": _entry_pair_stats(hybrid_only_trade_views),
            "baseline_only_entry_pairs": _entry_pair_stats(baseline_only_trade_views),
            "hybrid_only_by_symbol": _ranked_stats(hybrid_only_trade_views, key_name="symbol"),
            "hybrid_only_by_date": _ranked_stats(hybrid_only_trade_views, key_name="date"),
            "upgraded_hybrid_only_entry_pairs": _entry_pair_stats(upgraded_hybrid_only),
            "upgraded_hybrid_only_loser_metrics": _metric_summary(upgraded_hybrid_only_losers),
            "upgraded_hybrid_only_winner_metrics": _metric_summary(upgraded_hybrid_only_winners),
            "worst_hybrid_only_trades": sorted(
                hybrid_only_trade_views,
                key=lambda item: (float(item.get("pnl_usd", 0.0) or 0.0), item["opened_at"]),
            )[:15],
            "best_hybrid_only_trades": sorted(
                hybrid_only_trade_views,
                key=lambda item: (float(item.get("pnl_usd", 0.0) or 0.0), item["opened_at"]),
                reverse=True,
            )[:10],
        },
    }


def _render_markdown(payload: dict[str, object]) -> str:
    lines = [
        "# Crypto Regime V2 Autopsy",
        "",
        f"Variant studied: `{payload['variant_name']}`",
        "",
        "## Summary",
    ]
    summary = payload["summary"]
    lines.extend(
        [
            f"- baseline total: `{summary['baseline_total_realized_pnl_usd']} USD`",
            f"- variant total: `{summary['variant_total_realized_pnl_usd']} USD`",
            f"- delta total: `{summary['delta_total_realized_pnl_usd']} USD`",
            f"- Pod A delta: `{_round2(summary['variant_pod_a_realized_pnl_usd'] - summary['baseline_pod_a_realized_pnl_usd'])} USD`",
            f"- Pod C delta: `{_round2(summary['variant_pod_c_realized_pnl_usd'] - summary['baseline_pod_c_realized_pnl_usd'])} USD`",
            f"- regime transitions: `{summary['baseline_regime_transition_count']} -> {summary['variant_regime_transition_count']}`",
            f"- Pod A closed trades: `{summary['baseline_pod_a_closed_trade_count']} -> {summary['variant_pod_a_closed_trade_count']}`",
            "",
            "## Daily Delta",
        ]
    )
    for date, item in payload["daily"].items():
        lines.append(
            f"- `{date}`: total `{item['baseline_total_pnl_usd']} -> {item['variant_total_pnl_usd']}`"
            f" (`{item['delta_total_pnl_usd']:+.2f}`), Pod A `{item['baseline_pod_a_pnl_usd']} -> {item['variant_pod_a_pnl_usd']}`,"
            f" Pod C `{item['baseline_pod_c_pnl_usd']} -> {item['variant_pod_c_pnl_usd']}`"
        )
    lines.extend(["", "## Regime Diffs"])
    for key, count in sorted(
        payload["timeline_diff_counts_total"].items(),
        key=lambda item: item[1],
        reverse=True,
    ):
        lines.append(f"- `{key}`: `{count}` bars")
    lines.extend(["", "## Regime Diffs By Date"])
    for date, items in payload["timeline_diff_counts_by_date"].items():
        rendered = ", ".join(
            f"{key} {count}"
            for key, count in sorted(items.items(), key=lambda item: item[1], reverse=True)
        )
        lines.append(f"- `{date}`: {rendered}")
    lines.extend(["", "## Hybrid-Only Entry Pairs"])
    for item in payload["pod_a"]["hybrid_only_entry_pairs"][:8]:
        lines.append(
            f"- `{item['entry_pair']}`: `{item['pnl_usd']} USD` on `{item['trades']}` trades"
        )
    lines.extend(["", "## Upgraded Hybrid-Only Entry Pairs"])
    for item in payload["pod_a"]["upgraded_hybrid_only_entry_pairs"][:8]:
        lines.append(
            f"- `{item['entry_pair']}`: `{item['pnl_usd']} USD` on `{item['trades']}` trades"
        )
    lines.extend(["", "## Upgraded Entry Metrics"])
    loser_metrics = payload["pod_a"]["upgraded_hybrid_only_loser_metrics"]
    winner_metrics = payload["pod_a"]["upgraded_hybrid_only_winner_metrics"]
    lines.append(
        f"- losers: `{loser_metrics['trades']}` trades, `{loser_metrics['pnl_usd']} USD`,"
        f" breadth `{loser_metrics['avg_breadth_pct']}`, coherence `{loser_metrics['avg_coherence_score']}`,"
        f" leader `{loser_metrics['avg_leader_trend_score']}`, atr `{loser_metrics['avg_atr_ratio']}`"
    )
    lines.append(
        f"- winners: `{winner_metrics['trades']}` trades, `{winner_metrics['pnl_usd']} USD`,"
        f" breadth `{winner_metrics['avg_breadth_pct']}`, coherence `{winner_metrics['avg_coherence_score']}`,"
        f" leader `{winner_metrics['avg_leader_trend_score']}`, atr `{winner_metrics['avg_atr_ratio']}`"
    )
    lines.extend(["", "## Worst Hybrid-Only Trades"])
    for item in payload["pod_a"]["worst_hybrid_only_trades"][:10]:
        lines.append(
            f"- `{item['date']}` `{item['symbol']}` `{item['baseline_entry_regime']}->{item['variant_entry_regime']}`"
            f" `{item['pnl_usd']} USD` `{item['close_reason']}`"
            f" breadth `{round(float(item['breadth_pct']),4)}` coherence `{round(float(item['coherence_score']),4)}`"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Autopsy a Crypto Regime V2 variant.")
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument(
        "--input",
        default="server-data/replay_inputs/full_bot_latest_fetch_2026-04-13_2026-04-17.jsonl",
    )
    parser.add_argument("--variant", default="hybrid_moderate_a")
    parser.add_argument(
        "--output-prefix",
        default="server-data/replay_reports/crypto_regime_v2_autopsy_20260418",
    )
    args = parser.parse_args()

    output_prefix = Path(args.output_prefix)
    output_prefix.mkdir(parents=True, exist_ok=True)

    baseline_config = load_config(args.config)
    variant_config = _variant_config(baseline_config, args.variant)

    baseline_report_path = output_prefix / "baseline_report.json"
    variant_report_path = output_prefix / f"{args.variant}_report.json"
    baseline_report = _run_report(baseline_config, args.input, baseline_report_path)
    variant_report = _run_report(variant_config, args.input, variant_report_path)
    baseline_timeline = _build_regime_timeline(baseline_config, args.input)
    variant_timeline = _build_regime_timeline(variant_config, args.input)

    payload = _build_payload(
        baseline_report=baseline_report,
        variant_report=variant_report,
        baseline_timeline=baseline_timeline,
        variant_timeline=variant_timeline,
        variant_name=args.variant,
    )
    json_path = output_prefix / f"{args.variant}_autopsy.json"
    md_path = output_prefix / f"{args.variant}_autopsy.md"
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(_render_markdown(payload), encoding="utf-8")
    print(json_path)
    print(md_path)


if __name__ == "__main__":
    main()
