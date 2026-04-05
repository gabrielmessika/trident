from __future__ import annotations

from app.trident.pod_a.signals import AnchorTrendContext
from app.trident.types import Regime, SymbolMarketSnapshot


class MarketContextService:
    """Builds Pod A evaluation contexts from generic market snapshots."""

    def build_contexts(
        self,
        regime: Regime,
        snapshots: list[SymbolMarketSnapshot],
    ) -> list[AnchorTrendContext]:
        return [
            AnchorTrendContext(
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
            )
            for snapshot in snapshots
        ]
