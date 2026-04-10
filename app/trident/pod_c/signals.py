from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class TradfiTrendContext:
    symbol: str
    regime: str
    price: float
    ema_fast: float
    ema_slow: float
    vwap_distance_bps: float
    spread_bps: float
    funding_rate: float
    structure_score: float
    book_imbalance: float
    trade_flow_bias: float
    bucket_range_bps: float
    bucket_trade_count: int
    bucket_volume: float
    bucket_notional_usd: float
    activity_ratio: float
    trade_count_ratio: float
    trend_bps: float
    btc_aligned: bool
    market_cluster: str = "crypto"
    cluster_aligned: bool = True
    cluster_leader: str = ""


@dataclass(slots=True)
class TradfiTrendSignal:
    symbol: str
    side: str
    setup: str
    confidence: float
    entry_price: float
    market_cluster: str = "crypto"
    cluster_leader: str = ""
    confidence_components: dict[str, float] = field(default_factory=dict)
