from __future__ import annotations

from app.settings import AppConfig
from app.trident.types import Regime, RegimeDecision, RegimeSnapshot


class RegimeAllocator:
    """Deterministic regime classifier for early TRIDENT phases."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def current_regime(self, snapshot: RegimeSnapshot | None = None) -> Regime:
        return self.classify_raw(snapshot or RegimeSnapshot())

    def classify(self, snapshot: RegimeSnapshot) -> Regime:
        return self.classify_raw(snapshot)

    def classify_raw(self, snapshot: RegimeSnapshot) -> Regime:
        if not snapshot.ready:
            return Regime.CASH

        thresholds = self._config.trident.regime
        trend_threshold = thresholds.adx_trend_threshold
        trend_structure_threshold = thresholds.trend_structure_threshold
        panic_threshold = thresholds.atr_ratio_panic_threshold
        dead_zone_threshold = thresholds.dead_zone_atr_threshold
        dead_zone_range_threshold = thresholds.dead_zone_range_threshold

        if snapshot.atr_ratio >= panic_threshold and (
            snapshot.btc_impulse or abs(snapshot.structure_score) >= 0.50
        ):
            return Regime.PANIC_SQUEEZE

        if (
            snapshot.adx >= trend_threshold
            and snapshot.atr_ratio > dead_zone_threshold
            and abs(snapshot.structure_score) >= trend_structure_threshold
        ):
            return Regime.TREND_EXPANSION

        if (
            snapshot.atr_ratio <= dead_zone_threshold
            and snapshot.range_width_bps <= dead_zone_range_threshold
        ):
            return Regime.DEAD_ZONE

        return Regime.RANGE_AUCTION

    def resolve(
        self,
        *,
        snapshot: RegimeSnapshot,
        current_regime: Regime,
        pending_regime: Regime | None,
        pending_count: int,
    ) -> RegimeDecision:
        raw_regime = self.classify_raw(snapshot)
        if current_regime == Regime.CASH and raw_regime != Regime.CASH:
            return RegimeDecision(
                raw_regime=raw_regime,
                effective_regime=raw_regime,
                pending_regime=None,
                pending_count=0,
                switched=True,
            )
        if raw_regime == current_regime:
            return RegimeDecision(
                raw_regime=raw_regime,
                effective_regime=current_regime,
                pending_regime=None,
                pending_count=0,
            )
        next_pending = raw_regime
        next_count = pending_count + 1 if pending_regime == raw_regime else 1
        required = self._required_confirmations(current_regime, raw_regime)
        if next_count >= required:
            return RegimeDecision(
                raw_regime=raw_regime,
                effective_regime=raw_regime,
                pending_regime=None,
                pending_count=0,
                switched=True,
            )
        return RegimeDecision(
            raw_regime=raw_regime,
            effective_regime=current_regime,
            pending_regime=next_pending,
            pending_count=next_count,
        )

    def resolve_cluster(
        self,
        *,
        snapshot: RegimeSnapshot,
        current_regime: Regime,
        pending_regime: Regime | None,
        pending_count: int,
    ) -> RegimeDecision:
        return self.resolve(
            snapshot=snapshot,
            current_regime=current_regime,
            pending_regime=pending_regime,
            pending_count=pending_count,
        )

    def _required_confirmations(
        self,
        current_regime: Regime,
        candidate_regime: Regime,
    ) -> int:
        thresholds = self._config.trident.regime
        if candidate_regime == Regime.PANIC_SQUEEZE:
            return max(1, thresholds.panic_confirmation_bars)
        if candidate_regime == Regime.TREND_EXPANSION:
            return max(1, thresholds.trend_confirmation_bars)
        if current_regime == Regime.CASH:
            return 1
        return max(1, thresholds.switch_confirmation_bars)
