from __future__ import annotations

from app.settings import AppConfig, load_config
from app.trident.market_clusters import enrich_snapshots
from app.trident.pod_a.candles import CandleService
from app.trident.pod_a.signals import AnchorTrendContext
from app.trident.types import Regime, SymbolMarketSnapshot


def _clamp(value: float, lower: float = -1.0, upper: float = 1.0) -> float:
    return max(lower, min(value, upper))


def _vwap_reclaim_score(
    snapshot: SymbolMarketSnapshot,
    mtf_bias_score: float,
    *,
    candles_ready: bool,
) -> float:
    flow_alignment = (snapshot.trade_flow_bias + snapshot.book_imbalance) / 2.0
    if not candles_ready and abs(flow_alignment) < 0.05:
        return 0.0
    directional_vwap_score = _clamp(snapshot.vwap_distance_bps / 12.0)
    flow_score = _clamp(flow_alignment * 2.0)
    mtf_score = _clamp(mtf_bias_score / 80.0)
    return round(
        directional_vwap_score * 0.45 + flow_score * 0.35 + mtf_score * 0.20,
        4,
    )


class MarketContextService:
    """Builds Pod A evaluation contexts from generic market snapshots."""

    def __init__(self, config: AppConfig | None = None) -> None:
        self._config = config or load_config("config/trident.toml")
        self._candles = CandleService()

    def build_contexts(
        self,
        regime: Regime,
        snapshots: list[SymbolMarketSnapshot],
        timestamp: str | None = None,
    ) -> list[AnchorTrendContext]:
        snapshots = enrich_snapshots(self._config, snapshots)
        self._candles.observe(timestamp=timestamp, snapshots=snapshots)
        return [
            self._build_context(regime=regime, snapshot=snapshot)
            for snapshot in snapshots
        ]

    def _build_context(
        self,
        *,
        regime: Regime,
        snapshot: SymbolMarketSnapshot,
    ) -> AnchorTrendContext:
        features = self._candles.features_for(snapshot.symbol)
        return AnchorTrendContext(
            symbol=snapshot.symbol,
            regime=regime.value,
            price=snapshot.price,
            ema_fast=snapshot.ema_fast,
            ema_slow=snapshot.ema_slow,
            vwap_distance_bps=snapshot.vwap_distance_bps,
            structure_score=snapshot.structure_score,
            funding_rate=snapshot.funding_rate,
            spread_bps=snapshot.spread_bps,
            btc_aligned=snapshot.btc_aligned,
            market_cluster=snapshot.market_cluster,
            cluster_aligned=snapshot.cluster_aligned,
            cluster_leader=snapshot.cluster_leader,
            book_imbalance=snapshot.book_imbalance,
            trade_flow_bias=snapshot.trade_flow_bias,
            bucket_volume=snapshot.bucket_volume,
            bucket_trade_count=snapshot.bucket_trade_count,
            bucket_range_bps=snapshot.bucket_range_bps,
            trend_15m_bps=float(features["trend_15m_bps"]),
            trend_1h_bps=float(features["trend_1h_bps"]),
            trend_4h_bps=float(features["trend_4h_bps"]),
            mtf_bias_score=float(features["mtf_bias_score"]),
            candles_ready=bool(features["candles_ready"]),
            structure_ready=bool(features["structure_ready"]),
            range_high_1h=float(features["range_high_1h"]),
            range_low_1h=float(features["range_low_1h"]),
            swing_high_1h=float(features["swing_high_1h"]),
            swing_low_1h=float(features["swing_low_1h"]),
            bos_long_confirmed=bool(features["bos_long_confirmed"]),
            bos_short_confirmed=bool(features["bos_short_confirmed"]),
            ichimoku_bias_score=float(features["ichimoku_bias_score"]),
            supertrend_direction=int(features["supertrend_direction"]),
            stoch_rsi_k=float(features["stoch_rsi_k"]),
            cci20=float(features["cci20"]),
            vwap_reclaim_score=_vwap_reclaim_score(
                snapshot,
                float(features["mtf_bias_score"]),
                candles_ready=bool(features["candles_ready"]),
            ),
            rsi21_4h=float(features["rsi21_4h"]),
            ema50_distance_4h_pct=float(features["ema50_distance_4h_pct"]),
            ema50_distance_4h_atr=float(features["ema50_distance_4h_atr"]),
            macd_hist_4h=float(features["macd_hist_4h"]),
            macd_hist_delta_4h=float(features["macd_hist_delta_4h"]),
            upper_wick_ratio_4h=float(features["upper_wick_ratio_4h"]),
            lower_wick_ratio_4h=float(features["lower_wick_ratio_4h"]),
            bb_position_4h=float(features["bb_position_4h"]),
            btc_overextension_score=float(features["btc_overextension_score"]),
        )
