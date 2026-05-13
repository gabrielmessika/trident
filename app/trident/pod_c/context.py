from __future__ import annotations

from app.settings import AppConfig, PodCConfig
from app.trident.pod_c.service import TradfiTrendService
from app.trident.pod_c.signals import TradfiTrendContext
from app.trident.types import Regime, SymbolMarketSnapshot


class TradfiTrendContextService:
    """Builds Pod C Tradfi contexts from market snapshots and short activity history."""

    def __init__(self, config: AppConfig | PodCConfig, service: TradfiTrendService) -> None:
        if isinstance(config, AppConfig):
            self.config = config.pod_c
        else:
            self.config = config
        self._service = service

    def build_contexts(
        self,
        regime: Regime,
        snapshots: list[SymbolMarketSnapshot],
        *,
        owned_symbols: set[str] | None = None,
        cluster_regimes: dict[str, Regime] | None = None,
    ) -> list[TradfiTrendContext]:
        for snapshot in snapshots:
            self._service.update_history(
                snapshot.symbol.upper(),
                snapshot.bucket_volume * snapshot.price,
                snapshot.bucket_trade_count,
            )

        contexts: list[TradfiTrendContext] = []
        for snapshot in snapshots:
            symbol = snapshot.symbol.upper()
            if owned_symbols is not None and symbol not in owned_symbols:
                continue
            if snapshot.price <= 0:
                continue
            if not self._service.is_eligible_symbol(symbol, snapshot.market_cluster):
                continue
            bucket_notional_usd = snapshot.bucket_volume * snapshot.price
            context_regime = regime
            cluster = str(snapshot.market_cluster).strip().lower()
            if cluster and cluster != "crypto":
                context_regime = (cluster_regimes or {}).get(cluster, Regime.CASH)
            contexts.append(
                TradfiTrendContext(
                    symbol=symbol,
                    regime=context_regime.value,
                    price=snapshot.price,
                    ema_fast=snapshot.ema_fast,
                    ema_slow=snapshot.ema_slow,
                    vwap_distance_bps=snapshot.vwap_distance_bps,
                    spread_bps=snapshot.spread_bps,
                    funding_rate=snapshot.funding_rate,
                    structure_score=snapshot.structure_score,
                    book_imbalance=snapshot.book_imbalance,
                    trade_flow_bias=snapshot.trade_flow_bias,
                    bucket_range_bps=snapshot.bucket_range_bps,
                    bucket_trade_count=snapshot.bucket_trade_count,
                    bucket_volume=snapshot.bucket_volume,
                    bucket_notional_usd=round(bucket_notional_usd, 4),
                    activity_ratio=round(
                        self._service.activity_ratio(symbol, bucket_notional_usd),
                        4,
                    ),
                    trade_count_ratio=round(
                        self._service.trade_count_ratio(symbol, snapshot.bucket_trade_count),
                        4,
                    ),
                    trend_bps=round(
                        (
                            (snapshot.ema_fast - snapshot.ema_slow)
                            / max(snapshot.price, 1e-9)
                            * 10_000.0
                        ),
                        4,
                    ),
                    btc_aligned=snapshot.btc_aligned,
                    market_cluster=snapshot.market_cluster,
                    cluster_aligned=snapshot.cluster_aligned,
                    cluster_leader=snapshot.cluster_leader,
                    global_regime=regime.value,
                    cluster_regime=context_regime.value,
                    external_reference_price=snapshot.external_reference_price,
                    external_reference_source_count=snapshot.external_reference_source_count,
                    external_reference_sources=snapshot.external_reference_sources,
                    external_reference_symbol=snapshot.external_reference_symbol,
                    external_reference_time=snapshot.external_reference_time,
                    external_reference_age_seconds=snapshot.external_reference_age_seconds,
                    external_reference_max_deviation_bps=snapshot.external_reference_max_deviation_bps,
                    external_premium_bps=snapshot.external_premium_bps,
                    external_momentum_60s_bps=snapshot.external_momentum_60s_bps,
                    external_momentum_300s_bps=snapshot.external_momentum_300s_bps,
                    external_alignment_score=snapshot.external_alignment_score,
                )
            )
        return contexts
