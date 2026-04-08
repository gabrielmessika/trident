from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class SqueezeContext:
    symbol: str
    regime: str
    price: float
    spread_bps: float
    structure_score: float
    book_imbalance: float
    trade_flow_bias: float
    bucket_range_bps: float
    bucket_trade_count: int
    bucket_volume: float
    squeeze_ratio: float
    volume_ratio: float
    btc_aligned: bool
    market_cluster: str = "crypto"
    cluster_aligned: bool = True


@dataclass(slots=True)
class SqueezeSignal:
    symbol: str
    side: str
    setup: str
    confidence: float
    entry_price: float
    market_cluster: str = "crypto"
    confidence_components: dict[str, float] = field(default_factory=dict)
