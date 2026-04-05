from __future__ import annotations


def initial_stop_bps(structure_score: float) -> float:
    base_stop = 90.0
    if abs(structure_score) >= 0.70:
        return 70.0
    if abs(structure_score) >= 0.55:
        return 80.0
    return base_stop


def time_stop_hours() -> int:
    return 24

