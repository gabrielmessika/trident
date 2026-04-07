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
    market_cluster: str = "crypto"
    cluster_aligned: bool = True
    cluster_leader: str = ""
    book_imbalance: float = 0.0
    trade_flow_bias: float = 0.0
    bucket_volume: float = 0.0
    bucket_trade_count: int = 0
    bucket_range_bps: float = 0.0
    trend_15m_bps: float = 0.0
    trend_1h_bps: float = 0.0
    trend_4h_bps: float = 0.0
    mtf_bias_score: float = 0.0
    candles_ready: bool = False
    structure_ready: bool = False
    range_high_1h: float = 0.0
    range_low_1h: float = 0.0
    swing_high_1h: float = 0.0
    swing_low_1h: float = 0.0
    bos_long_confirmed: bool = False
    bos_short_confirmed: bool = False


@dataclass(slots=True)
class AnchorTrendSignal:
    symbol: str
    side: str
    setup: str
    confidence: float
    entry_price: float
    market_cluster: str = "crypto"
    cluster_leader: str = ""
    invalidation_price: float | None = None
    setup_details: dict[str, float | str | bool] = field(default_factory=dict)
    confidence_components: dict[str, float] = field(default_factory=dict)
