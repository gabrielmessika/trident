from __future__ import annotations

from app.settings import AppConfig, PodCConfig, load_config
from app.trident.market_clusters import all_cluster_leaders, enrich_snapshots
from app.trident.pod_c.signals import EventRaiderContext
from app.trident.types import Regime, SymbolMarketSnapshot


class EventContextService:
    """Builds Pod C follower contexts from market snapshots."""

    def __init__(self, config: AppConfig | PodCConfig) -> None:
        if isinstance(config, AppConfig):
            self._app_config = config
            self.config = config.pod_c
        else:
            self._app_config = load_config("config/trident.toml")
            self.config = config

    def build_contexts(
        self,
        regime: Regime,
        snapshots: list[SymbolMarketSnapshot],
    ) -> list[EventRaiderContext]:
        if regime not in {Regime.TREND_EXPANSION, Regime.PANIC_SQUEEZE}:
            return []

        snapshots = enrich_snapshots(self._app_config, snapshots)
        snapshot_by_symbol = {snapshot.symbol: snapshot for snapshot in snapshots}
        leaders_by_cluster = self._best_leaders_by_cluster(snapshot_by_symbol)
        if not leaders_by_cluster:
            return []
        leader_symbols = all_cluster_leaders(self._app_config)

        contexts: list[EventRaiderContext] = []
        for snapshot in snapshots:
            symbol = snapshot.symbol.upper()
            if symbol in leader_symbols:
                continue
            if snapshot.price <= 0:
                continue
            leader = leaders_by_cluster.get(snapshot.market_cluster)
            if leader is None:
                continue
            leader_symbol, leader_snapshot, leader_impulse_bps = leader
            if symbol == leader_symbol:
                continue
            side = "long" if leader_impulse_bps > 0 else "short"
            follower_move_bps = ((snapshot.price - snapshot.ema_slow) / snapshot.price) * 10_000.0
            if side == "short":
                follower_move_bps *= -1.0
            lag_bps = abs(leader_impulse_bps) - max(follower_move_bps, 0.0)
            contexts.append(
                EventRaiderContext(
                    symbol=symbol,
                    regime=regime.value,
                    leader_symbol=leader_symbol,
                    side=side,
                    price=snapshot.price,
                    spread_bps=snapshot.spread_bps,
                    structure_score=snapshot.structure_score,
                    book_imbalance=snapshot.book_imbalance,
                    trade_flow_bias=snapshot.trade_flow_bias,
                    btc_aligned=snapshot.btc_aligned,
                    market_cluster=snapshot.market_cluster,
                    cluster_aligned=snapshot.cluster_aligned,
                    leader_impulse_bps=round(leader_impulse_bps, 4),
                    follower_move_bps=round(follower_move_bps, 4),
                    lag_bps=round(lag_bps, 4),
                )
            )
        return contexts

    def _best_leaders_by_cluster(
        self,
        snapshot_by_symbol: dict[str, SymbolMarketSnapshot],
    ) -> dict[str, tuple[str, SymbolMarketSnapshot, float]]:
        best_by_cluster: dict[str, tuple[str, SymbolMarketSnapshot, float]] = {}
        for snapshot in snapshot_by_symbol.values():
            symbol = snapshot.symbol.upper()
            if symbol not in all_cluster_leaders(self._app_config):
                continue
            if snapshot.price <= 0:
                continue
            impulse_bps = ((snapshot.price - snapshot.ema_slow) / snapshot.price) * 10_000.0
            min_impulse = self._impulse_threshold_bps(snapshot.market_cluster)
            if abs(impulse_bps) < min_impulse:
                continue
            best = best_by_cluster.get(snapshot.market_cluster)
            if best is None or abs(impulse_bps) > abs(best[2]):
                best_by_cluster[snapshot.market_cluster] = (symbol, snapshot, impulse_bps)
        return best_by_cluster

    def _impulse_threshold_bps(self, market_cluster: str) -> float:
        if market_cluster == "index":
            return max(self.config.impulse_threshold_bps * 0.8, 8.0)
        if market_cluster == "gold":
            return max(self.config.impulse_threshold_bps * 0.9, 8.0)
        return self.config.impulse_threshold_bps
