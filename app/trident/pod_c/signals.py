from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class EventRaiderContext:
    symbol: str
    regime: str
    leader_symbol: str
    side: str
    price: float
    spread_bps: float
    structure_score: float
    book_imbalance: float
    trade_flow_bias: float
    btc_aligned: bool
    leader_impulse_bps: float
    follower_move_bps: float
    lag_bps: float


@dataclass(slots=True)
class EventRaiderSignal:
    symbol: str
    side: str
    setup: str
    confidence: float
    entry_price: float
    leader_symbol: str
    confidence_components: dict[str, float] = field(default_factory=dict)
