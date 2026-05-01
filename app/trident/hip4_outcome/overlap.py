from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.live.runtime_status import load_runtime_status, runtime_status_is_fresh


@dataclass(slots=True)
class OverlapSource:
    pod: str
    status_path: str
    symbols: list[str] = field(default_factory=list)
    fresh: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class DirectionalOverlapSnapshot:
    blocked_underlyings: list[str]
    sources: list[OverlapSource] = field(default_factory=list)

    @property
    def has_overlap(self) -> bool:
        return bool(self.blocked_underlyings)

    def to_dict(self) -> dict[str, object]:
        return {
            "blocked_underlyings": list(self.blocked_underlyings),
            "sources": [source.to_dict() for source in self.sources],
        }


def directional_overlap_snapshot(
    status_paths: list[str],
    *,
    enabled: bool = True,
) -> DirectionalOverlapSnapshot:
    if not enabled:
        return DirectionalOverlapSnapshot(blocked_underlyings=[])
    sources: list[OverlapSource] = []
    blocked: set[str] = set()
    for raw_path in status_paths:
        path = str(raw_path).strip()
        if not path:
            continue
        payload = load_runtime_status(Path(path))
        if not isinstance(payload, dict):
            sources.append(
                OverlapSource(
                    pod="unknown",
                    status_path=path,
                    fresh=False,
                    reason="missing_status",
                )
            )
            continue
        pod = str(payload.get("pod") or "unknown")
        fresh = runtime_status_is_fresh(payload)
        symbols = _open_position_symbols(payload) if fresh else []
        blocked.update(symbols)
        sources.append(
            OverlapSource(
                pod=pod,
                status_path=path,
                symbols=symbols,
                fresh=fresh,
                reason="fresh_runtime" if fresh else "stale_status",
            )
        )
    return DirectionalOverlapSnapshot(
        blocked_underlyings=sorted(blocked),
        sources=sources,
    )


def hip4_open_underlyings(
    status_path: str | Path = "logs/pod_b_live_status.json",
) -> list[str]:
    payload = load_runtime_status(status_path)
    if not isinstance(payload, dict) or not runtime_status_is_fresh(payload):
        return []
    if str(payload.get("pod_kind", "")).strip().lower() != "hip4_outcome_edge_pod":
        return []
    return sorted(_position_underlyings(payload))


def _open_position_symbols(payload: dict[str, Any]) -> list[str]:
    symbols: set[str] = set()
    open_positions = payload.get("open_positions", [])
    if isinstance(open_positions, list):
        for position in open_positions:
            if not isinstance(position, dict):
                continue
            symbol = str(
                position.get("symbol")
                or position.get("underlying")
                or position.get("coin")
                or ""
            ).strip().upper()
            if symbol:
                symbols.add(symbol)
    return sorted(symbols)


def _position_underlyings(payload: dict[str, Any]) -> set[str]:
    underlyings: set[str] = set()
    open_positions = payload.get("open_positions", [])
    if not isinstance(open_positions, list):
        return underlyings
    for position in open_positions:
        if not isinstance(position, dict):
            continue
        underlying = str(
            position.get("underlying")
            or position.get("symbol")
            or ""
        ).strip().upper()
        if underlying:
            underlyings.add(underlying)
    return underlyings
