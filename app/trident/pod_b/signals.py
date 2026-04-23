from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class BreakoutContext:
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
    price_move_bps: float = 0.0
    market_cluster: str = "crypto"
    cluster_leader: str = ""
    book_imbalance: float = 0.0
    trade_flow_bias: float = 0.0
    bucket_trade_count: int = 0
    bucket_notional_usd: float = 0.0
    bucket_range_bps: float = 0.0
    delta_spread_bps: float = 0.0
    delta_book_imbalance: float = 0.0
    delta_trade_flow_bias: float = 0.0
    volume_ratio: float = 1.0
    trade_count_ratio: float = 1.0
    realized_vol_short_bps: float = 0.0
    realized_vol_long_bps: float = 0.0
    compression_score: float = 0.0
    best_bid_size: float = 0.0
    best_ask_size: float = 0.0
    bid_depth_10bps: float = 0.0
    ask_depth_10bps: float = 0.0
    bid_depth_velocity: float = 0.0
    ask_depth_velocity: float = 0.0
    best_bid_size_velocity: float = 0.0
    best_ask_size_velocity: float = 0.0
    microprice_dislocation_bps: float = 0.0


@dataclass(slots=True)
class BreakoutSignal:
    symbol: str
    side: str
    setup: str
    confidence: float
    entry_price: float
    stop_bps_hint: float
    market_cluster: str = "crypto"
    cluster_leader: str = ""
    setup_details: dict[str, float | str | bool] = field(default_factory=dict)
    confidence_components: dict[str, float] = field(default_factory=dict)
