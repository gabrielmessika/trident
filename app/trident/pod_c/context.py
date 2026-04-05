from __future__ import annotations

from app.settings import PodCConfig
from app.trident.pod_c.signals import EventRaiderContext
from app.trident.types import Regime, SymbolMarketSnapshot


class EventContextService:
    """Builds Pod C follower contexts from market snapshots."""

    def __init__(self, config: PodCConfig) -> None:
        self.config = config

    def build_contexts(
        self,
        regime: Regime,
        snapshots: list[SymbolMarketSnapshot],
    ) -> list[EventRaiderContext]:
        if regime not in {Regime.TREND_EXPANSION, Regime.PANIC_SQUEEZE}:
            return []

        snapshot_by_symbol = {snapshot.symbol: snapshot for snapshot in snapshots}
        leader = self._best_leader(snapshot_by_symbol)
        if leader is None:
            return []
        leader_symbol, leader_snapshot, leader_impulse_bps = leader
        side = "long" if leader_impulse_bps > 0 else "short"

        contexts: list[EventRaiderContext] = []
        for follower_symbol in self.config.follower_symbols:
            symbol = follower_symbol.upper()
            if symbol == leader_symbol:
                continue
            snapshot = snapshot_by_symbol.get(symbol)
            if snapshot is None or snapshot.price <= 0:
                continue
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
                    leader_impulse_bps=round(leader_impulse_bps, 4),
                    follower_move_bps=round(follower_move_bps, 4),
                    lag_bps=round(lag_bps, 4),
                )
            )
        return contexts

    def _best_leader(
        self,
        snapshot_by_symbol: dict[str, SymbolMarketSnapshot],
    ) -> tuple[str, SymbolMarketSnapshot, float] | None:
        best: tuple[str, SymbolMarketSnapshot, float] | None = None
        for leader_symbol in self.config.leader_symbols:
            symbol = leader_symbol.upper()
            snapshot = snapshot_by_symbol.get(symbol)
            if snapshot is None or snapshot.price <= 0:
                continue
            impulse_bps = ((snapshot.price - snapshot.ema_slow) / snapshot.price) * 10_000.0
            if abs(impulse_bps) < self.config.impulse_threshold_bps:
                continue
            if best is None or abs(impulse_bps) > abs(best[2]):
                best = (symbol, snapshot, impulse_bps)
        return best
