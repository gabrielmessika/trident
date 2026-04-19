from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(slots=True)
class PatternDefinition:
    key: str
    title: str
    rule_hint: dict[str, object]


@dataclass(slots=True)
class PatternStats:
    trades: int = 0
    pnl_usd: float = 0.0
    win_count: int = 0
    active_days: int = 0
    negative_days: int = 0
    positive_days: int = 0
    avg_pnl_usd: float = 0.0
    win_rate: float = 0.0
    negative_day_share: float = 0.0


PATTERN_DEFINITIONS = [
    PatternDefinition(
        key="vol_ratio_low",
        title="`vol_ratio < 1.60`",
        rule_hint={"max_vol_ratio": 1.60},
    ),
    PatternDefinition(
        key="vol_ratio_mid_low",
        title="`vol_ratio < 1.85`",
        rule_hint={"max_vol_ratio": 1.85},
    ),
    PatternDefinition(
        key="compression_high",
        title="`compression_score >= 0.26`",
        rule_hint={"min_compression_score": 0.26},
    ),
    PatternDefinition(
        key="reclaim_high",
        title="`vwap_reclaim_quality >= 0.80`",
        rule_hint={"min_vwap_reclaim_quality": 0.80},
    ),
    PatternDefinition(
        key="confidence_high",
        title="`confidence >= 0.685`",
        rule_hint={"min_confidence": 0.685},
    ),
    PatternDefinition(
        key="breakout_low",
        title="`breakout_score < 0.76`",
        rule_hint={"max_breakout_score": 0.76},
    ),
    PatternDefinition(
        key="activity_low",
        title="`activity_score < 0.84`",
        rule_hint={"max_activity_score": 0.84},
    ),
    PatternDefinition(
        key="vol_ratio_mid_low_compression_high",
        title="`vol_ratio < 1.85` et `compression_score >= 0.26`",
        rule_hint={"max_vol_ratio": 1.85, "min_compression_score": 0.26},
    ),
    PatternDefinition(
        key="vol_ratio_mid_low_confidence_high",
        title="`vol_ratio < 1.85` et `confidence >= 0.685`",
        rule_hint={"max_vol_ratio": 1.85, "min_confidence": 0.685},
    ),
    PatternDefinition(
        key="compression_high_reclaim_high",
        title="`compression_score >= 0.26` et `vwap_reclaim_quality >= 0.80`",
        rule_hint={"min_compression_score": 0.26, "min_vwap_reclaim_quality": 0.80},
    ),
]


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _metric(trade: dict[str, object], key: str) -> float:
    if key == "confidence":
        return _safe_float(trade.get("confidence"))
    if key in {"stop_bps", "take_profit_bps"}:
        return _safe_float(trade.get(key))
    details = trade.get("setup_details", {})
    if not isinstance(details, dict):
        return 0.0
    return _safe_float(details.get(key))


def _pattern_matches(key: str, trade: dict[str, object]) -> bool:
    vol_ratio = _metric(trade, "vol_ratio")
    compression_score = _metric(trade, "compression_score")
    reclaim_quality = _metric(trade, "vwap_reclaim_quality")
    confidence = _metric(trade, "confidence")
    breakout_score = _metric(trade, "breakout_score")
    activity_score = _metric(trade, "activity_score")

    if key == "vol_ratio_low":
        return vol_ratio < 1.60
    if key == "vol_ratio_mid_low":
        return vol_ratio < 1.85
    if key == "compression_high":
        return compression_score >= 0.26
    if key == "reclaim_high":
        return reclaim_quality >= 0.80
    if key == "confidence_high":
        return confidence >= 0.685
    if key == "breakout_low":
        return breakout_score < 0.76
    if key == "activity_low":
        return activity_score < 0.84
    if key == "vol_ratio_mid_low_compression_high":
        return vol_ratio < 1.85 and compression_score >= 0.26
    if key == "vol_ratio_mid_low_confidence_high":
        return vol_ratio < 1.85 and confidence >= 0.685
    if key == "compression_high_reclaim_high":
        return compression_score >= 0.26 and reclaim_quality >= 0.80
    return False


def _compute_pattern_stats(trades: list[dict[str, object]], key: str) -> PatternStats:
    daily_pnl: dict[str, float] = defaultdict(float)
    trades_count = 0
    pnl_usd = 0.0
    win_count = 0
    for trade in trades:
        if not _pattern_matches(key, trade):
            continue
        trades_count += 1
        pnl = _safe_float(trade.get("pnl_usd"))
        pnl_usd += pnl
        if pnl > 0:
            win_count += 1
        date = str(trade.get("date", ""))
        if date:
            daily_pnl[date] += pnl
    active_days = len(daily_pnl)
    negative_days = sum(1 for pnl in daily_pnl.values() if pnl < 0)
    positive_days = sum(1 for pnl in daily_pnl.values() if pnl > 0)
    return PatternStats(
        trades=trades_count,
        pnl_usd=round(pnl_usd, 2),
        win_count=win_count,
        active_days=active_days,
        negative_days=negative_days,
        positive_days=positive_days,
        avg_pnl_usd=round(pnl_usd / trades_count, 2) if trades_count else 0.0,
        win_rate=round(win_count / trades_count, 3) if trades_count else 0.0,
        negative_day_share=round(negative_days / active_days, 3) if active_days else 0.0,
    )


def _daily_pattern_breakdown(
    trades: list[dict[str, object]],
) -> dict[str, dict[str, float]]:
    daily: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for trade in trades:
        date = str(trade.get("date", ""))
        if not date:
            continue
        pnl = _safe_float(trade.get("pnl_usd"))
        for definition in PATTERN_DEFINITIONS:
            if _pattern_matches(definition.key, trade):
                daily[date][definition.key] += pnl
    return {
        date: {pattern: round(pnl, 2) for pattern, pnl in sorted(items.items())}
        for date, items in sorted(daily.items())
    }


def _top_daily_patterns(
    daily_breakdown: dict[str, dict[str, float]],
) -> dict[str, dict[str, list[dict[str, object]]]]:
    result: dict[str, dict[str, list[dict[str, object]]]] = {}
    label_by_key = {definition.key: definition.title for definition in PATTERN_DEFINITIONS}
    for date, pattern_pnl in daily_breakdown.items():
        losers = [
            {
                "pattern": key,
                "title": label_by_key.get(key, key),
                "pnl_usd": pnl,
            }
            for key, pnl in sorted(pattern_pnl.items(), key=lambda item: item[1])[:3]
            if pnl < 0
        ]
        winners = [
            {
                "pattern": key,
                "title": label_by_key.get(key, key),
                "pnl_usd": pnl,
            }
            for key, pnl in sorted(pattern_pnl.items(), key=lambda item: item[1], reverse=True)[:3]
            if pnl > 0
        ]
        result[date] = {
            "losers": losers,
            "winners": winners,
        }
    return result


def _recommendations(
    early_stats: dict[str, PatternStats],
    recent_stats: dict[str, PatternStats],
    full_stats: dict[str, PatternStats],
) -> list[dict[str, object]]:
    by_key = {definition.key: definition for definition in PATTERN_DEFINITIONS}
    recommendations: list[dict[str, object]] = []
    for key, definition in by_key.items():
        early = early_stats.get(key, PatternStats())
        recent = recent_stats.get(key, PatternStats())
        full = full_stats.get(key, PatternStats())
        confidence = "watch"
        if (
            full.trades >= 8
            and recent.trades >= 5
            and full.pnl_usd < 0
            and recent.pnl_usd < 0
            and early.pnl_usd <= 0
        ):
            confidence = "high"
        elif (
            full.trades >= 10
            and recent.trades >= 5
            and full.pnl_usd < 0
            and recent.pnl_usd < 0
            and early.pnl_usd <= 5.0
        ):
            confidence = "medium"
        elif full.pnl_usd < 0 and recent.pnl_usd < 0:
            confidence = "watch"
        else:
            continue
        recommendations.append(
            {
                "pattern": key,
                "title": definition.title,
                "confidence": confidence,
                "rule_hint": definition.rule_hint,
                "early": asdict(early),
                "recent": asdict(recent),
                "full": asdict(full),
            }
        )
    order = {"high": 0, "medium": 1, "watch": 2}
    return sorted(
        recommendations,
        key=lambda item: (
            order.get(item["confidence"], 99),
            item["recent"]["pnl_usd"],
            item["full"]["pnl_usd"],
        ),
    )


def build_payload(
    *,
    early_report: dict[str, object],
    recent_report: dict[str, object],
    full_report: dict[str, object],
) -> dict[str, object]:
    early_trades = list(early_report.get("closed_trade_log", []) or [])
    recent_trades = list(recent_report.get("closed_trade_log", []) or [])
    full_trades = list(full_report.get("closed_trade_log", []) or [])
    early_stats = {
        definition.key: _compute_pattern_stats(early_trades, definition.key)
        for definition in PATTERN_DEFINITIONS
    }
    recent_stats = {
        definition.key: _compute_pattern_stats(recent_trades, definition.key)
        for definition in PATTERN_DEFINITIONS
    }
    full_stats = {
        definition.key: _compute_pattern_stats(full_trades, definition.key)
        for definition in PATTERN_DEFINITIONS
    }
    early_daily = _daily_pattern_breakdown(early_trades)
    recent_daily = _daily_pattern_breakdown(recent_trades)
    full_daily = _daily_pattern_breakdown(full_trades)
    return {
        "patterns": {
            definition.key: {
                "title": definition.title,
                "rule_hint": definition.rule_hint,
                "early": asdict(early_stats[definition.key]),
                "recent": asdict(recent_stats[definition.key]),
                "full": asdict(full_stats[definition.key]),
            }
            for definition in PATTERN_DEFINITIONS
        },
        "recommendations": _recommendations(early_stats, recent_stats, full_stats),
        "early_daily_top_patterns": _top_daily_patterns(early_daily),
        "recent_daily_top_patterns": _top_daily_patterns(recent_daily),
        "full_daily_top_patterns": _top_daily_patterns(full_daily),
        "report_summaries": {
            "early": {
                "realized_pnl_usd": round(_safe_float(early_report.get("realized_pnl_usd")), 2),
                "closed_trade_count": int(early_report.get("closed_trade_count", 0) or 0),
            },
            "recent": {
                "realized_pnl_usd": round(_safe_float(recent_report.get("realized_pnl_usd")), 2),
                "closed_trade_count": int(recent_report.get("closed_trade_count", 0) or 0),
            },
            "full": {
                "realized_pnl_usd": round(_safe_float(full_report.get("realized_pnl_usd")), 2),
                "closed_trade_count": int(full_report.get("closed_trade_count", 0) or 0),
            },
        },
    }


def render_markdown(payload: dict[str, object]) -> str:
    lines = [
        "# Pod B Day-By-Day Patterns",
        "",
        "## Summary",
        f"- early window: `{payload['report_summaries']['early']['realized_pnl_usd']} USD` on `{payload['report_summaries']['early']['closed_trade_count']}` trades",
        f"- recent window: `{payload['report_summaries']['recent']['realized_pnl_usd']} USD` on `{payload['report_summaries']['recent']['closed_trade_count']}` trades",
        f"- full window: `{payload['report_summaries']['full']['realized_pnl_usd']} USD` on `{payload['report_summaries']['full']['closed_trade_count']}` trades",
        "",
        "## Recommendations",
    ]
    recommendations = payload["recommendations"]
    if recommendations:
        for item in recommendations:
            lines.append(
                f"- `{item['pattern']}` `{item['confidence']}`: early `{item['early']['pnl_usd']}` on `{item['early']['trades']}` trades, "
                f"recent `{item['recent']['pnl_usd']}` on `{item['recent']['trades']}` trades, "
                f"full `{item['full']['pnl_usd']}` on `{item['full']['trades']}` trades"
            )
    else:
        lines.append("- aucun pattern suffisamment stable n'a ete detecte")

    lines.extend(["", "## Early Daily Top Patterns"])
    for date, item in payload["early_daily_top_patterns"].items():
        if item["losers"]:
            lines.append(
                f"- `{date}` losers: "
                + ", ".join(f"{entry['pattern']} {entry['pnl_usd']}" for entry in item["losers"])
            )
        if item["winners"]:
            lines.append(
                f"- `{date}` winners: "
                + ", ".join(f"{entry['pattern']} {entry['pnl_usd']}" for entry in item["winners"])
            )

    lines.extend(["", "## Recent Daily Top Patterns"])
    for date, item in payload["recent_daily_top_patterns"].items():
        if item["losers"]:
            lines.append(
                f"- `{date}` losers: "
                + ", ".join(f"{entry['pattern']} {entry['pnl_usd']}" for entry in item["losers"])
            )
        if item["winners"]:
            lines.append(
                f"- `{date}` winners: "
                + ", ".join(f"{entry['pattern']} {entry['pnl_usd']}" for entry in item["winners"])
            )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze Pod B trades day by day to find robust losing patterns."
    )
    parser.add_argument("--early-report", required=True)
    parser.add_argument("--recent-report", required=True)
    parser.add_argument("--full-report", required=True)
    parser.add_argument("--output-prefix", required=True)
    args = parser.parse_args()

    early_report = json.loads(Path(args.early_report).read_text(encoding="utf-8"))
    recent_report = json.loads(Path(args.recent_report).read_text(encoding="utf-8"))
    full_report = json.loads(Path(args.full_report).read_text(encoding="utf-8"))
    payload = build_payload(
        early_report=early_report,
        recent_report=recent_report,
        full_report=full_report,
    )
    output_prefix = Path(args.output_prefix)
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = output_prefix.with_suffix(".json")
    md_path = output_prefix.with_suffix(".md")
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    print(json_path)
    print(md_path)


if __name__ == "__main__":
    main()
