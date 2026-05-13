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
    ichimoku_bias_score: float = 0.0
    supertrend_direction: int = 0
    stoch_rsi_k: float = 0.5
    cci20: float = 0.0
    vwap_reclaim_score: float = 0.0
    prev_ema50_ready_1h: bool = False
    prev_rsi14_1h: float = 50.0
    prev_ema20_distance_ema50_1h_pct: float = 0.0
    entry_vs_open_1h_bps: float = 0.0
    prev_ema50_ready_4h: bool = False
    prev_rsi14_4h: float = 50.0
    prev_ema50_distance_4h_pct: float = 0.0
    rsi21_4h: float = 50.0
    ema50_distance_4h_pct: float = 0.0
    ema50_distance_4h_atr: float = 0.0
    macd_hist_4h: float = 0.0
    macd_hist_delta_4h: float = 0.0
    upper_wick_ratio_4h: float = 0.0
    lower_wick_ratio_4h: float = 0.0
    bb_position_4h: float = 0.5
    btc_overextension_score: float = 0.0
    external_reference_price: float | None = None
    external_reference_source_count: int = 0
    external_reference_sources: str = ""
    external_reference_symbol: str = ""
    external_reference_time: str = ""
    external_reference_age_seconds: float | None = None
    external_reference_max_deviation_bps: float = 0.0
    external_premium_bps: float = 0.0
    external_momentum_60s_bps: float = 0.0
    external_momentum_300s_bps: float = 0.0
    external_alignment_score: float = 0.0


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
