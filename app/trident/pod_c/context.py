from __future__ import annotations

from app.settings import AppConfig, PodCConfig, load_config
from app.trident.pod_c.service import SqueezeBreakoutService
from app.trident.pod_c.signals import SqueezeContext
from app.trident.types import Regime, SymbolMarketSnapshot


class SqueezeContextService:
    """Builds Pod C squeeze contexts from market snapshots and rolling history."""

    def __init__(self, config: AppConfig | PodCConfig, service: SqueezeBreakoutService) -> None:
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
    ) -> list[SqueezeContext]:
        for snapshot in snapshots:
            self._service.update_history(
                snapshot.symbol.upper(),
                snapshot.bucket_range_bps,
                snapshot.bucket_trade_count,
            )

        contexts: list[SqueezeContext] = []
        for snapshot in snapshots:
            symbol = snapshot.symbol.upper()
            if owned_symbols is not None and symbol not in owned_symbols:
                continue
            if snapshot.price <= 0:
                continue
            squeeze_ratio = self._service.squeeze_ratio(symbol, snapshot.bucket_range_bps)
            volume_ratio = self._service.volume_ratio(symbol, snapshot.bucket_trade_count)
            is_candidate = (
                squeeze_ratio >= self.config.breakout_multiplier * 0.8
                and volume_ratio >= self.config.min_volume_spike * 0.5
            )
            if not is_candidate:
                continue
            contexts.append(
                SqueezeContext(
                    symbol=symbol,
                    regime=regime.value,
                    price=snapshot.price,
                    spread_bps=snapshot.spread_bps,
                    structure_score=snapshot.structure_score,
                    book_imbalance=snapshot.book_imbalance,
                    trade_flow_bias=snapshot.trade_flow_bias,
                    bucket_range_bps=snapshot.bucket_range_bps,
                    bucket_trade_count=snapshot.bucket_trade_count,
                    bucket_volume=snapshot.bucket_volume,
                    squeeze_ratio=round(squeeze_ratio, 4),
                    volume_ratio=round(volume_ratio, 4),
                    btc_aligned=snapshot.btc_aligned,
                    market_cluster=snapshot.market_cluster,
                    cluster_aligned=snapshot.cluster_aligned,
                )
            )
        return contexts
