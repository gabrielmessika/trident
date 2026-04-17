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
        key="trend1h_negative",
        title="`trend_1h_bps < -5`",
        rule_hint={
            "name": "trend1h_negative",
            "setups": ["trend_pullback_long"],
            "max_trend_1h_bps": -5.0,
        },
    ),
    PatternDefinition(
        key="trend4h_positive_cci_mid",
        title="`trend_4h_bps >= 10` et `-100 <= cci20 <= 100`",
        rule_hint={
            "name": "trend4h_positive_cci_mid",
            "setups": ["trend_pullback_long"],
            "min_trend_4h_bps": 10.0,
            "min_cci20": -100.0,
            "max_cci20": 100.0,
        },
    ),
    PatternDefinition(
        key="vwap_weak",
        title="`vwap_reclaim_score < 0.45`",
        rule_hint={
            "name": "vwap_weak",
            "setups": ["trend_pullback_long"],
            "max_vwap_reclaim_score": 0.45,
        },
    ),
    PatternDefinition(
        key="vwap_weak_trend4h_positive",
        title="`vwap_reclaim_score < 0.45` et `trend_4h_bps >= 10`",
        rule_hint={
            "name": "vwap_weak_trend4h_positive",
            "setups": ["trend_pullback_long"],
            "max_vwap_reclaim_score": 0.45,
            "min_trend_4h_bps": 10.0,
        },
    ),
    PatternDefinition(
        key="trend4h_flat",
        title="`-10 <= trend_4h_bps < 10`",
        rule_hint={
            "name": "trend4h_flat",
            "setups": ["trend_pullback_long"],
            "min_trend_4h_bps": -10.0,
            "max_trend_4h_bps": 10.0,
        },
    ),
    PatternDefinition(
        key="trend1h_positive",
        title="`trend_1h_bps >= 5`",
        rule_hint={},
    ),
    PatternDefinition(
        key="vwap_strong",
        title="`vwap_reclaim_score >= 0.70`",
        rule_hint={},
    ),
    PatternDefinition(
        key="trend1h_positive_cci_high",
        title="`trend_1h_bps >= 5` et `cci20 > 100`",
        rule_hint={},
    ),
]


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pattern_matches(key: str, details: dict[str, object]) -> bool:
    trend_1h_bps = _safe_float(details.get("trend_1h_bps"))
    trend_4h_bps = _safe_float(details.get("trend_4h_bps"))
    cci20 = _safe_float(details.get("cci20"))
    vwap_reclaim_score = _safe_float(details.get("vwap_reclaim_score"))

    if key == "trend1h_negative":
        return trend_1h_bps < -5.0
    if key == "trend4h_positive_cci_mid":
        return trend_4h_bps >= 10.0 and -100.0 <= cci20 <= 100.0
    if key == "vwap_weak":
        return vwap_reclaim_score < 0.45
    if key == "vwap_weak_trend4h_positive":
        return vwap_reclaim_score < 0.45 and trend_4h_bps >= 10.0
    if key == "trend4h_flat":
        return -10.0 <= trend_4h_bps < 10.0
    if key == "trend1h_positive":
        return trend_1h_bps >= 5.0
    if key == "vwap_strong":
        return vwap_reclaim_score >= 0.70
    if key == "trend1h_positive_cci_high":
        return trend_1h_bps >= 5.0 and cci20 > 100.0
    return False


def _compute_pattern_stats(trades: list[dict[str, object]], key: str) -> PatternStats:
    daily_pnl: dict[str, float] = defaultdict(float)
    trades_count = 0
    pnl_usd = 0.0
    win_count = 0
    for trade in trades:
        details = trade.get("setup_details", {})
        if not isinstance(details, dict) or not _pattern_matches(key, details):
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
        details = trade.get("setup_details", {})
        if not date or not isinstance(details, dict):
            continue
        pnl = _safe_float(trade.get("pnl_usd"))
        for definition in PATTERN_DEFINITIONS:
            if _pattern_matches(definition.key, details):
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
    broad_stats: dict[str, PatternStats],
    recent_stats: dict[str, PatternStats],
) -> list[dict[str, object]]:
    by_key = {definition.key: definition for definition in PATTERN_DEFINITIONS}
    recommendations: list[dict[str, object]] = []
    for key, definition in by_key.items():
        broad = broad_stats.get(key, PatternStats())
        recent = recent_stats.get(key, PatternStats())
        confidence = "watch"
        if (
            broad.trades >= 8
            and broad.pnl_usd < 0
            and broad.negative_day_share >= 0.60
            and recent.trades >= 4
            and recent.pnl_usd < 0
        ):
            confidence = "high"
        elif (
            broad.trades >= 15
            and broad.pnl_usd < 0
            and broad.negative_day_share >= 0.55
        ):
            confidence = "medium"
        elif recent.trades >= 8 and recent.pnl_usd < 0:
            confidence = "watch"
        else:
            continue
        recommendations.append(
            {
                "pattern": key,
                "title": definition.title,
                "confidence": confidence,
                "broad": asdict(broad),
                "recent": asdict(recent),
                "rule_hint": definition.rule_hint,
            }
        )
    return sorted(
        recommendations,
        key=lambda item: (
            {"high": 0, "medium": 1, "watch": 2}.get(item["confidence"], 9),
            item["broad"]["pnl_usd"],
            item["recent"]["pnl_usd"],
        ),
    )


def analyze_reports(
    *,
    broad_report_path: str | Path,
    recent_report_path: str | Path,
) -> dict[str, object]:
    broad_payload = json.loads(Path(broad_report_path).read_text(encoding="utf-8"))
    recent_payload = json.loads(Path(recent_report_path).read_text(encoding="utf-8"))
    broad_trades = list(broad_payload["pod_a"]["closed_trade_log"])
    recent_trades = list(recent_payload["pod_a"]["closed_trade_log"])

    broad_stats = {
        definition.key: _compute_pattern_stats(broad_trades, definition.key)
        for definition in PATTERN_DEFINITIONS
    }
    recent_stats = {
        definition.key: _compute_pattern_stats(recent_trades, definition.key)
        for definition in PATTERN_DEFINITIONS
    }
    broad_daily = _daily_pattern_breakdown(broad_trades)
    recent_daily = _daily_pattern_breakdown(recent_trades)
    return {
        "broad_report_path": str(broad_report_path),
        "recent_report_path": str(recent_report_path),
        "broad_total_realized_pnl_usd": broad_payload["total_realized_pnl_usd"],
        "recent_total_realized_pnl_usd": recent_payload["total_realized_pnl_usd"],
        "broad_pnl_by_date": broad_payload["pod_a"].get("pnl_by_date", {}),
        "recent_pnl_by_date": recent_payload["pod_a"].get("pnl_by_date", {}),
        "patterns": {
            definition.key: {
                "title": definition.title,
                "broad": asdict(broad_stats[definition.key]),
                "recent": asdict(recent_stats[definition.key]),
                "rule_hint": definition.rule_hint,
            }
            for definition in PATTERN_DEFINITIONS
        },
        "broad_daily_top_patterns": _top_daily_patterns(broad_daily),
        "recent_daily_top_patterns": _top_daily_patterns(recent_daily),
        "recommended_vetoes": _recommendations(broad_stats, recent_stats),
    }


def render_markdown(payload: dict[str, object]) -> str:
    patterns = payload["patterns"]
    recommendations = payload["recommended_vetoes"]
    lines = [
        "# Pod A Day-By-Day Pattern Analysis",
        "",
        f"- broad report: `{payload['broad_report_path']}`",
        f"- recent report: `{payload['recent_report_path']}`",
        f"- broad total_realized_pnl_usd: `{payload['broad_total_realized_pnl_usd']}`",
        f"- recent total_realized_pnl_usd: `{payload['recent_total_realized_pnl_usd']}`",
        "",
        "## Recommended Vetoes",
        "",
    ]
    if not recommendations:
        lines.append("- none")
    for item in recommendations:
        lines.append(
            f"- `{item['pattern']}` `{item['confidence']}`: broad `{item['broad']['pnl_usd']}` on `{item['broad']['trades']}` trades, recent `{item['recent']['pnl_usd']}` on `{item['recent']['trades']}` trades"
        )
        if item["rule_hint"]:
            lines.append(f"  rule_hint: `{json.dumps(item['rule_hint'], ensure_ascii=True)}`")
    lines.extend(
        [
            "",
            "## Pattern Scorecard",
            "",
        ]
    )
    for key, item in patterns.items():
        broad = item["broad"]
        recent = item["recent"]
        lines.append(
            f"- `{key}` {item['title']}: broad `{broad['pnl_usd']}` / `{broad['trades']}` trades / neg_days `{broad['negative_days']}/{broad['active_days']}`, recent `{recent['pnl_usd']}` / `{recent['trades']}` trades / neg_days `{recent['negative_days']}/{recent['active_days']}`"
        )
    lines.extend(["", "## Broad Daily Highlights", ""])
    for date, item in payload["broad_daily_top_patterns"].items():
        lines.append(f"- `{date}`")
        if item["losers"]:
            loser_text = ", ".join(
                f"{entry['pattern']} {entry['pnl_usd']}" for entry in item["losers"]
            )
            lines.append(
                f"  losers: {loser_text}"
            )
        if item["winners"]:
            winner_text = ", ".join(
                f"{entry['pattern']} {entry['pnl_usd']}" for entry in item["winners"]
            )
            lines.append(
                f"  winners: {winner_text}"
            )
    lines.extend(["", "## Recent Daily Highlights", ""])
    for date, item in payload["recent_daily_top_patterns"].items():
        lines.append(f"- `{date}`")
        if item["losers"]:
            loser_text = ", ".join(
                f"{entry['pattern']} {entry['pnl_usd']}" for entry in item["losers"]
            )
            lines.append(
                f"  losers: {loser_text}"
            )
        if item["winners"]:
            winner_text = ", ".join(
                f"{entry['pattern']} {entry['pnl_usd']}" for entry in item["winners"]
            )
            lines.append(
                f"  winners: {winner_text}"
            )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze Pod A trades day by day to find robust losing patterns."
    )
    parser.add_argument("--broad-report", required=True)
    parser.add_argument("--recent-report", required=True)
    parser.add_argument("--output-json")
    parser.add_argument("--output-md")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    payload = analyze_reports(
        broad_report_path=args.broad_report,
        recent_report_path=args.recent_report,
    )
    if args.output_json:
        output_json = Path(args.output_json)
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    markdown = render_markdown(payload)
    if args.output_md:
        output_md = Path(args.output_md)
        output_md.parent.mkdir(parents=True, exist_ok=True)
        output_md.write_text(markdown, encoding="utf-8")
    print(markdown)


if __name__ == "__main__":
    main()
