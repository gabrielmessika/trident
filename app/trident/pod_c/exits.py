from __future__ import annotations


def initial_stop_bps(confidence: float) -> float:
    if confidence >= 0.8:
        return 35.0
    if confidence >= 0.65:
        return 45.0
    return 55.0
