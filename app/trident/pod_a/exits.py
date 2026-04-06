from __future__ import annotations

from app.trident.pod_a.structure import stop_bps_from_invalidation

def initial_stop_bps(structure_score: float) -> float:
    base_stop = 90.0
    if abs(structure_score) >= 0.70:
        return 70.0
    if abs(structure_score) >= 0.55:
        return 80.0
    return base_stop


def time_stop_hours() -> int:
    return 24


def stop_bps_for_signal(
    *,
    entry_price: float,
    invalidation_price: float | None,
    side: str,
    fallback_bps: float,
) -> float:
    return stop_bps_from_invalidation(
        entry_price=entry_price,
        invalidation_price=invalidation_price,
        side=side,
        fallback_bps=fallback_bps,
    )
