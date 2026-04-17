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
        if not thresholds.crypto_v2_enabled:
            return self._classify_legacy(snapshot)
        trend_threshold = thresholds.adx_trend_threshold
        trend_structure_threshold = thresholds.trend_structure_threshold
        panic_threshold = thresholds.atr_ratio_panic_threshold
        dead_zone_threshold = thresholds.dead_zone_atr_threshold
        dead_zone_range_threshold = thresholds.dead_zone_range_threshold
        has_v2_context = bool(
            snapshot.symbol_count
            or snapshot.active_symbol_count
            or snapshot.leader_symbol
        )

        if not has_v2_context:
            return self._classify_legacy(snapshot)

        legacy_trend = (
            snapshot.adx >= trend_threshold
            and snapshot.atr_ratio > dead_zone_threshold
            and abs(snapshot.structure_score) >= trend_structure_threshold
        )
        legacy_dead_zone = (
            snapshot.atr_ratio <= dead_zone_threshold
            and snapshot.range_width_bps <= dead_zone_range_threshold
        )
        breadth_pct = self._breadth_pct(snapshot)
        dispersion_pct = self._dispersion_pct(snapshot)
        leader_trend_score = self._leader_trend_score(snapshot)
        coherence_score = self._coherence_score(snapshot)
        active_symbol_count = max(
            1,
            snapshot.active_symbol_count or snapshot.symbol_count or 1,
        )
        weak_breadth = breadth_pct < 0.40 and dispersion_pct > 0.60
        weak_coherence = coherence_score < 0.45
        weak_leader = leader_trend_score < 0.32 and not snapshot.btc_impulse

        if snapshot.atr_ratio >= panic_threshold and (
            snapshot.btc_impulse
            or abs(snapshot.structure_score) >= 0.50
            or leader_trend_score >= 0.65
        ):
            if leader_trend_score >= 0.45 and (
                active_symbol_count <= 3 or breadth_pct >= 0.35 or not weak_breadth
            ):
                return Regime.PANIC_SQUEEZE

        if legacy_trend:
            if active_symbol_count <= 3:
                if leader_trend_score >= 0.30 or snapshot.btc_impulse:
                    return Regime.TREND_EXPANSION
            elif not ((weak_breadth and weak_coherence) or weak_leader):
                return Regime.TREND_EXPANSION

        if legacy_dead_zone and leader_trend_score < 0.35 and coherence_score < 0.55:
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
        required = self._required_confirmations(
            current_regime,
            raw_regime,
            snapshot=snapshot,
        )
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
        *,
        snapshot: RegimeSnapshot,
    ) -> int:
        thresholds = self._config.trident.regime
        if not thresholds.crypto_v2_enabled or snapshot.symbol_count == 0:
            return self._required_confirmations_legacy(current_regime, candidate_regime)
        breadth_pct = self._breadth_pct(snapshot)
        coherence_score = self._coherence_score(snapshot)
        leader_trend_score = self._leader_trend_score(snapshot)
        if candidate_regime == Regime.PANIC_SQUEEZE:
            required = max(1, thresholds.panic_confirmation_bars)
            if breadth_pct < 0.35 and leader_trend_score < 0.60:
                required += 1
            return required
        if candidate_regime == Regime.TREND_EXPANSION:
            required = max(1, thresholds.trend_confirmation_bars)
            if snapshot.active_symbol_count >= 4 and breadth_pct < 0.45:
                required += 1
            if snapshot.active_symbol_count >= 4 and coherence_score < 0.50:
                required += 1
            return required
        if current_regime == Regime.CASH:
            return 1
        required = max(1, thresholds.switch_confirmation_bars)
        if (
            current_regime == Regime.TREND_EXPANSION
            and candidate_regime == Regime.RANGE_AUCTION
            and coherence_score >= 0.60
            and breadth_pct >= 0.45
        ):
            required += 1
        if (
            current_regime == Regime.PANIC_SQUEEZE
            and snapshot.atr_ratio >= thresholds.atr_ratio_panic_threshold * 0.85
        ):
            required += 1
        return required

    def _breadth_pct(self, snapshot: RegimeSnapshot) -> float:
        if snapshot.symbol_count > 0:
            return snapshot.breadth_pct
        return self._clamp(abs(snapshot.structure_score) * 1.45, 0.0, 1.0)

    def _dispersion_pct(self, snapshot: RegimeSnapshot) -> float:
        if snapshot.symbol_count > 0:
            return snapshot.dispersion_pct
        return self._clamp(1.0 - self._breadth_pct(snapshot), 0.0, 1.0)

    def _leader_trend_score(self, snapshot: RegimeSnapshot) -> float:
        if snapshot.symbol_count > 0:
            return snapshot.leader_trend_score
        return self._clamp(abs(snapshot.structure_score) * 1.55, 0.0, 1.0)

    def _coherence_score(self, snapshot: RegimeSnapshot) -> float:
        if snapshot.symbol_count > 0:
            return snapshot.coherence_score
        breadth = self._breadth_pct(snapshot)
        leader = self._leader_trend_score(snapshot)
        return self._clamp(breadth * 0.55 + leader * 0.45, 0.0, 1.0)

    def _clamp(self, value: float, lower: float, upper: float) -> float:
        return max(lower, min(value, upper))

    def _classify_legacy(self, snapshot: RegimeSnapshot) -> Regime:
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

    def _required_confirmations_legacy(
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
