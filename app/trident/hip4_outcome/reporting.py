from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.trident.hip4_outcome.models import OutcomePosition


def build_daily_summary_rows(positions: list[OutcomePosition]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[OutcomePosition]] = defaultdict(list)
    for position in positions:
        date = _date_from_iso(position.opened_at)
        mode = _position_mode(position)
        groups[(date, mode, position.underlying.upper())].append(position)

    rows: list[dict[str, Any]] = []
    for (date, mode, underlying), items in sorted(groups.items()):
        rows.append(
            {
                "date": date,
                "mode": mode,
                "underlying": underlying,
                "positions": len(items),
                "open_positions": len([item for item in items if item.status == "open"]),
                "settled_positions": len([item for item in items if item.status != "open"]),
                "cost_usdc": round(sum(item.cost_usdc for item in items), 8),
                "estimated_payout_usdc": round(
                    sum(item.estimated_payout_usdc for item in items),
                    8,
                ),
                "estimated_pnl_usdc": round(
                    sum(item.estimated_pnl_usdc for item in items),
                    8,
                ),
                "avg_net_edge": round(
                    sum(item.net_edge for item in items) / len(items),
                    8,
                ),
                "avg_confidence": round(
                    sum(item.confidence for item in items) / len(items),
                    8,
                ),
            }
        )
    return rows


def replay_opportunities(path: str | Path) -> list[dict[str, Any]]:
    csv_path = Path(path)
    if not csv_path.exists():
        return []
    groups: dict[tuple[str, str, str, str], list[dict[str, str]]] = defaultdict(list)
    with csv_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            key = (
                _date_from_iso(row.get("ts", "")),
                str(row.get("underlying", "")).upper(),
                str(row.get("edge_type", "")),
                str(row.get("side", "")),
            )
            groups[key].append(row)

    rows: list[dict[str, Any]] = []
    for (date, underlying, edge_type, side), items in sorted(groups.items()):
        net_edges = [_float(row.get("net_edge")) for row in items]
        gross_edges = [_float(row.get("gross_edge")) for row in items]
        confidence = [_float(row.get("confidence")) for row in items]
        rows.append(
            {
                "date": date,
                "underlying": underlying,
                "edge_type": edge_type,
                "side": side,
                "opportunity_count": len(items),
                "avg_gross_edge": round(sum(gross_edges) / len(gross_edges), 8),
                "avg_net_edge": round(sum(net_edges) / len(net_edges), 8),
                "max_net_edge": round(max(net_edges), 8),
                "avg_confidence": round(sum(confidence) / len(confidence), 8),
            }
        )
    return rows


def _date_from_iso(value: str) -> str:
    return str(value or "unknown")[:10] or "unknown"


def _position_mode(position: OutcomePosition) -> str:
    decision = position.metadata.get("decision", {})
    if isinstance(decision, dict):
        mode = str(decision.get("execution_mode", "")).strip().upper()
        if mode:
            return mode
    return "UNKNOWN"


def _float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
