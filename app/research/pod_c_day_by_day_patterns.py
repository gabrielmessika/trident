from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.backtest.pod_c_runner import PodCBacktestRunner
from app.settings import load_config


@dataclass(slots=True)
class PatternStats:
    trades: int = 0
    pnl_usd: float = 0.0
    win_count: int = 0
    active_days: int = 0
    positive_days: int = 0
    negative_days: int = 0
    avg_pnl_usd: float = 0.0
    win_rate: float = 0.0
    negative_day_share: float = 0.0


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_str(value: object, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _build_pattern_tags(trade: dict[str, object]) -> dict[str, str]:
    details = trade.get("setup_details", {})
    if not isinstance(details, dict):
        details = {}
    cluster = _safe_str(
        details.get("market_cluster"),
        _safe_str(trade.get("market_cluster"), "unknown"),
    )
    setup = _safe_str(trade.get("setup"), "unknown")
    side = _safe_str(trade.get("side"), "unknown")
    cluster_regime = _safe_str(details.get("cluster_regime"), "unknown")
    trend_bucket = _safe_str(details.get("trend_bucket"), "unknown")
    structure_bucket = _safe_str(details.get("structure_bucket"), "unknown")
    vwap_bucket = _safe_str(details.get("vwap_bucket"), "unknown")
    activity_bucket = _safe_str(details.get("activity_bucket"), "unknown")
    flow_bucket = _safe_str(details.get("flow_bucket"), "unknown")
    flow_alignment = _safe_str(details.get("flow_alignment"), "unknown")
    cluster_strategy = _safe_str(details.get("cluster_strategy"), "unknown")
    return {
        "cluster_setup": f"{cluster}|{setup}|{side}",
        "cluster_regime": f"{cluster}|{cluster_regime}",
        "cluster_flow": f"{cluster}|{flow_alignment}|{flow_bucket}|{activity_bucket}",
        "cluster_structure": (
            f"{cluster}|{setup}|{trend_bucket}|{structure_bucket}|{vwap_bucket}"
        ),
        "cluster_strategy": f"{cluster}|{cluster_strategy}",
    }


def _compute_stats(
    trades: list[dict[str, object]],
    *,
    tag_name: str,
) -> dict[str, PatternStats]:
    stats: dict[str, PatternStats] = {}
    daily: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for trade in trades:
        tags = _build_pattern_tags(trade)
        tag = tags.get(tag_name)
        if not tag:
            continue
        pnl = _safe_float(trade.get("pnl_usd"))
        date = _safe_str(trade.get("date"))
        bucket = stats.setdefault(tag, PatternStats())
        bucket.trades += 1
        bucket.pnl_usd += pnl
        if pnl > 0:
            bucket.win_count += 1
        if date:
            daily[tag][date] += pnl
    for tag, bucket in stats.items():
        per_day = daily.get(tag, {})
        bucket.pnl_usd = round(bucket.pnl_usd, 2)
        bucket.active_days = len(per_day)
        bucket.positive_days = sum(1 for pnl in per_day.values() if pnl > 0)
        bucket.negative_days = sum(1 for pnl in per_day.values() if pnl < 0)
        bucket.avg_pnl_usd = round(bucket.pnl_usd / bucket.trades, 2) if bucket.trades else 0.0
        bucket.win_rate = round(bucket.win_count / bucket.trades, 3) if bucket.trades else 0.0
        bucket.negative_day_share = (
            round(bucket.negative_days / bucket.active_days, 3) if bucket.active_days else 0.0
        )
    return stats


def _recommendations(trades: list[dict[str, object]]) -> list[dict[str, object]]:
    recommendations: list[dict[str, object]] = []
    for tag_name in ("cluster_strategy", "cluster_structure", "cluster_flow"):
        stats = _compute_stats(trades, tag_name=tag_name)
        for pattern, item in stats.items():
            if item.trades < 3 or item.active_days < 2:
                continue
            if item.pnl_usd >= 0:
                continue
            if item.negative_day_share < 0.6:
                continue
            recommendations.append(
                {
                    "family": tag_name,
                    "pattern": pattern,
                    "stats": asdict(item),
                }
            )
    return sorted(
        recommendations,
        key=lambda item: (
            item["stats"]["pnl_usd"],
            -item["stats"]["negative_day_share"],
            -item["stats"]["trades"],
        ),
    )


def _daily_breakdown(trades: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    by_day: dict[str, dict[str, object]] = defaultdict(
        lambda: {
            "pnl_usd": 0.0,
            "trades": 0,
            "clusters": defaultdict(float),
            "setups": defaultdict(float),
            "strategies": defaultdict(float),
        }
    )
    for trade in trades:
        date = _safe_str(trade.get("date"))
        if not date:
            continue
        pnl = _safe_float(trade.get("pnl_usd"))
        details = trade.get("setup_details", {})
        if not isinstance(details, dict):
            details = {}
        cluster = _safe_str(
            details.get("market_cluster"),
            _safe_str(trade.get("market_cluster"), "unknown"),
        )
        setup = _safe_str(trade.get("setup"), "unknown")
        strategy = _safe_str(details.get("cluster_strategy"), "unknown")
        bucket = by_day[date]
        bucket["pnl_usd"] += pnl
        bucket["trades"] += 1
        bucket["clusters"][cluster] += pnl
        bucket["setups"][setup] += pnl
        bucket["strategies"][strategy] += pnl
    result: dict[str, dict[str, object]] = {}
    for date, payload in sorted(by_day.items()):
        result[date] = {
            "pnl_usd": round(_safe_float(payload["pnl_usd"]), 2),
            "trades": int(payload["trades"]),
            "clusters": {
                key: round(value, 2)
                for key, value in sorted(payload["clusters"].items())
            },
            "setups": {
                key: round(value, 2)
                for key, value in sorted(payload["setups"].items())
            },
            "strategies": {
                key: round(value, 2)
                for key, value in sorted(payload["strategies"].items())
            },
        }
    return result


def _top_patterns(
    stats: dict[str, PatternStats],
    *,
    limit: int = 8,
    reverse: bool = False,
) -> list[dict[str, object]]:
    ranked = sorted(
        stats.items(),
        key=lambda item: item[1].pnl_usd,
        reverse=reverse,
    )[:limit]
    return [
        {
            "pattern": pattern,
            **asdict(data),
        }
        for pattern, data in ranked
    ]


def _render_markdown(report: dict[str, object]) -> str:
    lines = [
        "# Pod C Day-by-Day Patterns",
        "",
        f"Date: `{report['generated_at']}`",
        "",
        "## Summary",
        "",
        f"- trades closes: `{report['summary']['closed_trade_count']}`",
        f"- pnl realise: `{report['summary']['realized_pnl_usd']}`",
        f"- clusters actifs: `{', '.join(report['summary']['clusters'])}`",
        "",
        "## Top Losing Candidate Patterns",
        "",
    ]
    recommendations = report.get("recommendations", [])
    if recommendations:
        for item in recommendations[:10]:
            stats = item["stats"]
            lines.append(
                f"- `{item['family']}` `{item['pattern']}`: `{stats['pnl_usd']}` sur `{stats['trades']}` trades, `{stats['negative_day_share']}` de jours negatifs"
            )
    else:
        lines.append("- aucun pattern cluster-aware nettement perdant n'a ete detecte avec les seuils courants")
    lines.extend(
        [
            "",
            "## Top Winners By Cluster Strategy",
            "",
        ]
    )
    for item in report["top_winners"]["cluster_strategy"]:
        lines.append(
            f"- `{item['pattern']}`: `{item['pnl_usd']}` sur `{item['trades']}` trades"
        )
    lines.extend(
        [
            "",
            "## Top Losers By Cluster Strategy",
            "",
        ]
    )
    for item in report["top_losers"]["cluster_strategy"]:
        lines.append(
            f"- `{item['pattern']}`: `{item['pnl_usd']}` sur `{item['trades']}` trades"
        )
    lines.extend(
        [
            "",
            "## Daily Breakdown",
            "",
        ]
    )
    for date, payload in report["daily"].items():
        cluster_text = ", ".join(
            f"{cluster} {pnl:+.2f}" for cluster, pnl in payload["clusters"].items()
        )
        lines.append(
            f"- `{date}`: `{payload['pnl_usd']:+.2f}` sur `{payload['trades']}` trades"
            + (f" ({cluster_text})" if cluster_text else "")
        )
    return "\n".join(lines) + "\n"


def build_report(*, config_path: str, input_path: str | Path) -> dict[str, object]:
    config = load_config(config_path)
    config.pod_c.enabled = True
    result = PodCBacktestRunner(config).run_jsonl(input_path)
    backtest = result.backtest
    trades = list(backtest.get("closed_trade_log", []))
    cluster_strategy_stats = _compute_stats(trades, tag_name="cluster_strategy")
    cluster_structure_stats = _compute_stats(trades, tag_name="cluster_structure")
    cluster_flow_stats = _compute_stats(trades, tag_name="cluster_flow")
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00",
            "Z",
        ),
        "input_path": str(input_path),
        "summary": {
            "closed_trade_count": backtest.get("closed_trade_count", 0),
            "realized_pnl_usd": backtest.get("realized_pnl_usd", 0.0),
            "clusters": sorted(backtest.get("pnl_by_cluster", {}).keys()),
            "pnl_by_cluster": backtest.get("pnl_by_cluster", {}),
            "trades_by_cluster": backtest.get("trades_by_cluster", {}),
            "pnl_by_setup": backtest.get("pnl_by_setup", {}),
        },
        "recommendations": _recommendations(trades),
        "top_winners": {
            "cluster_strategy": _top_patterns(cluster_strategy_stats, limit=8, reverse=True),
            "cluster_structure": _top_patterns(cluster_structure_stats, limit=8, reverse=True),
            "cluster_flow": _top_patterns(cluster_flow_stats, limit=8, reverse=True),
        },
        "top_losers": {
            "cluster_strategy": _top_patterns(cluster_strategy_stats, limit=8, reverse=False),
            "cluster_structure": _top_patterns(cluster_structure_stats, limit=8, reverse=False),
            "cluster_flow": _top_patterns(cluster_flow_stats, limit=8, reverse=False),
        },
        "daily": _daily_breakdown(trades),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze Pod C trades day by day")
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--input", default="server-data/live_snapshots")
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-md", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    report = build_report(config_path=args.config, input_path=args.input)
    output_json = Path(args.output_json)
    output_md = Path(args.output_md)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    output_md.write_text(_render_markdown(report), encoding="utf-8")
    print(f"output_json={output_json}")
    print(f"output_md={output_md}")


if __name__ == "__main__":
    main()
