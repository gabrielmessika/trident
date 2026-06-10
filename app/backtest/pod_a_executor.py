from __future__ import annotations

from datetime import datetime

from app.execution.directional_executor import DirectionalExecutor
from app.portfolio.directional_state import (
    DirectionalPortfolioState,
    OpenPosition,
    parse_timestamp,
)
from app.settings import AppConfig
from app.trident.pod_a.live_risk import (
    is_crypto_trend_pullback,
    stop_grace_minutes_for_setup,
)
from app.trident.types import SymbolMarketSnapshot


class PodAStopGracePortfolioState(DirectionalPortfolioState):
    """Directional state with a Pod A-specific stop grace for crypto pullbacks."""

    def __init__(self, pod_a_config_or_stop_grace_minutes: object) -> None:
        super().__init__()
        self._pod_a_config = (
            pod_a_config_or_stop_grace_minutes
            if hasattr(pod_a_config_or_stop_grace_minutes, "stop_grace_minutes")
            else None
        )
        self._stop_grace_minutes = max(
            int(
                getattr(
                    pod_a_config_or_stop_grace_minutes,
                    "stop_grace_minutes",
                    pod_a_config_or_stop_grace_minutes,
                )
                or 0
            ),
            0,
        )
        self._current_timestamp: str | None = None
        self._current_snapshots_by_symbol: dict[str, SymbolMarketSnapshot] = {}

    def _stop_hit(self, position: OpenPosition, price: float) -> bool:
        if self._stop_grace_active(position):
            return False
        return super()._stop_hit(position, price)

    def protective_exit_reason(self, position: OpenPosition, price: float) -> str | None:
        if self._early_failure_exit_hit(position, price):
            return "early_failure_exit"
        return super().protective_exit_reason(position, price)

    def set_current_snapshots(self, snapshots: list[SymbolMarketSnapshot]) -> None:
        self._current_snapshots_by_symbol = {
            snapshot.symbol.upper(): snapshot for snapshot in snapshots
        }

    def clear_current_snapshots(self) -> None:
        self._current_snapshots_by_symbol = {}

    def _stop_grace_active(self, position: OpenPosition) -> bool:
        stop_grace_minutes = self._stop_grace_minutes_for_position(position)
        if stop_grace_minutes <= 0 or position.opened_at is None:
            return False
        if self._current_timestamp is None:
            return False
        current = parse_timestamp(self._current_timestamp)
        if current is None:
            return False
        age_seconds = (current - position.opened_at).total_seconds()
        return 0.0 <= age_seconds < stop_grace_minutes * 60

    def _stop_grace_minutes_for_position(self, position: OpenPosition) -> int:
        return stop_grace_minutes_for_setup(
            self._pod_a_config,
            setup=position.setup,
            confidence=float(position.confidence or 0.0),
            details=dict(position.setup_details or {}),
            fallback_minutes=self._stop_grace_minutes,
        )

    def _early_failure_exit_hit(self, position: OpenPosition, price: float) -> bool:
        config = self._pod_a_config
        if config is None or not bool(getattr(config, "early_failure_exit_enabled", False)):
            return False
        if not self._stop_grace_active(position):
            return False
        if not is_crypto_trend_pullback(
            setup=position.setup,
            details=dict(position.setup_details or {}),
        ):
            return False
        if position.opened_at is None or self._current_timestamp is None:
            return False
        current = parse_timestamp(self._current_timestamp)
        if current is None or position.entry_price <= 0:
            return False
        age_minutes = (current - position.opened_at).total_seconds() / 60.0
        min_age = max(float(getattr(config, "early_failure_min_age_minutes", 0) or 0), 0.0)
        max_age = max(float(getattr(config, "early_failure_max_age_minutes", 0) or 0), 0.0)
        if age_minutes < min_age:
            return False
        if max_age > 0 and age_minutes > max_age:
            return False

        if position.side == "long":
            adverse_bps = (
                (position.entry_price - price) / position.entry_price
            ) * 10_000.0
        else:
            adverse_bps = (
                (price - position.entry_price) / position.entry_price
            ) * 10_000.0
        threshold_bps = max(
            float(position.stop_bps or 0.0)
            * max(
                float(
                    getattr(
                        config,
                        "early_failure_adverse_stop_fraction",
                        0.55,
                    )
                    or 0.55
                ),
                0.0,
            ),
            float(getattr(config, "early_failure_min_adverse_bps", 25.0) or 25.0),
        )
        if adverse_bps < threshold_bps:
            return False

        snapshot = self._current_snapshots_by_symbol.get(position.symbol.upper())
        if snapshot is None:
            return True
        if position.side == "long":
            if snapshot.structure_score <= float(
                getattr(config, "early_failure_max_structure_score", 0.20) or 0.20
            ):
                return True
            if snapshot.vwap_distance_bps <= float(
                getattr(config, "early_failure_max_vwap_distance_bps", -8.0) or -8.0
            ):
                return True
            return not bool(snapshot.btc_aligned)
        if snapshot.structure_score >= -float(
            getattr(config, "early_failure_max_structure_score", 0.20) or 0.20
        ):
            return True
        return snapshot.vwap_distance_bps >= abs(
            float(getattr(config, "early_failure_max_vwap_distance_bps", -8.0) or -8.0)
        )


class PodAExecutor(DirectionalExecutor):
    """Directional executor with Pod A-specific stop-grace behavior."""

    def __init__(self, config: AppConfig) -> None:
        super().__init__(config)
        self.portfolio = PodAStopGracePortfolioState(config.pod_a)
        self._opposite_signal_debounce_minutes = max(
            int(config.pod_a.opposite_signal_debounce_minutes),
            0,
        )
        self._opposite_signal_since_by_symbol: dict[str, datetime] = {}

    def process_record(
        self,
        *,
        snapshots,
        risk_decisions,
        signal_sides_by_symbol,
        timestamp,
        entry_allowed_symbols=None,
        managed_symbols=None,
        allowed_symbols=None,
    ):
        self.portfolio._current_timestamp = timestamp
        self.portfolio.set_current_snapshots(snapshots)
        raw_signal_sides = dict(signal_sides_by_symbol)
        filtered_signal_sides = self._filter_opposite_signal_sides(
            signal_sides_by_symbol=raw_signal_sides,
            timestamp=timestamp,
        )
        try:
            return super().process_record(
                snapshots=snapshots,
                risk_decisions=risk_decisions,
                signal_sides_by_symbol=filtered_signal_sides,
                timestamp=timestamp,
                entry_allowed_symbols=entry_allowed_symbols,
                managed_symbols=managed_symbols,
                allowed_symbols=allowed_symbols,
            )
        finally:
            self.portfolio._current_timestamp = None
            self.portfolio.clear_current_snapshots()
            self._cleanup_opposite_signal_tracking(raw_signal_sides)

    def _filter_opposite_signal_sides(
        self,
        *,
        signal_sides_by_symbol: dict[str, str],
        timestamp: str | None,
    ) -> dict[str, str]:
        if self._opposite_signal_debounce_minutes <= 0:
            self._opposite_signal_since_by_symbol.clear()
            return dict(signal_sides_by_symbol)

        filtered = dict(signal_sides_by_symbol)
        current = parse_timestamp(timestamp)
        for symbol, position in self.portfolio.open_positions.items():
            preview_side = signal_sides_by_symbol.get(symbol)
            if preview_side is None or preview_side == position.side:
                self._opposite_signal_since_by_symbol.pop(symbol, None)
                continue
            if current is None:
                filtered.pop(symbol, None)
                continue
            first_seen = self._opposite_signal_since_by_symbol.get(symbol)
            if first_seen is None:
                self._opposite_signal_since_by_symbol[symbol] = current
                filtered.pop(symbol, None)
                continue
            age_seconds = (current - first_seen).total_seconds()
            if age_seconds < self._opposite_signal_debounce_minutes * 60:
                filtered.pop(symbol, None)
        return filtered

    def _cleanup_opposite_signal_tracking(
        self,
        signal_sides_by_symbol: dict[str, str],
    ) -> None:
        if self._opposite_signal_debounce_minutes <= 0:
            self._opposite_signal_since_by_symbol.clear()
            return
        for symbol in list(self._opposite_signal_since_by_symbol):
            position = self.portfolio.open_positions.get(symbol)
            preview_side = signal_sides_by_symbol.get(symbol)
            if position is None or preview_side is None or preview_side == position.side:
                self._opposite_signal_since_by_symbol.pop(symbol, None)
