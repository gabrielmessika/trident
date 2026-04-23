from __future__ import annotations

from dataclasses import asdict

from app.trident.types import RegimeSnapshot, SymbolMarketSnapshot


def annotate_snapshot_record(
    record: dict[str, object],
    *,
    stream_source: str,
    capture_reason: str = "collector",
) -> dict[str, object]:
    payload = dict(record)
    payload["stream_source"] = str(stream_source)
    payload["capture_reason"] = str(capture_reason)
    return payload


def build_maintenance_snapshot_record(
    *,
    timestamp: str,
    stream_source: str,
    regime_snapshot: RegimeSnapshot,
    cluster_regime_snapshots: dict[str, RegimeSnapshot],
    snapshots: list[SymbolMarketSnapshot],
) -> dict[str, object]:
    return annotate_snapshot_record(
        {
            "timestamp": timestamp,
            "regime_snapshot": asdict(regime_snapshot),
            "cluster_regime_snapshots": {
                cluster: asdict(snapshot)
                for cluster, snapshot in cluster_regime_snapshots.items()
            }
            or None,
            "symbols": [asdict(snapshot) for snapshot in snapshots],
        },
        stream_source=stream_source,
        capture_reason="maintenance_refresh",
    )
