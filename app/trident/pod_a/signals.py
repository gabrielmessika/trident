from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class AnchorTrendContext:
    symbol: str
    regime: str
    price: float
    ema_fast: float
    ema_slow: float
    vwap_distance_bps: float
    structure_score: float
    funding_rate: float
    spread_bps: float
    btc_aligned: bool


@dataclass(slots=True)
class AnchorTrendSignal:
    symbol: str
    side: str
    setup: str
    confidence: float
    entry_price: float
    confidence_components: dict[str, float] = field(default_factory=dict)
