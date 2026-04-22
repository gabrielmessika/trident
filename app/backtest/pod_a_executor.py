from __future__ import annotations

from datetime import datetime

from app.execution.directional_executor import DirectionalExecutor
from app.portfolio.directional_state import DirectionalPortfolioState, OpenPosition, parse_timestamp
from app.settings import AppConfig


class PodAStopGracePortfolioState(DirectionalPortfolioState):
    """Directional state with a Pod A-specific stop grace for crypto pullbacks."""

    def __init__(self, stop_grace_minutes: int) -> None:
        super().__init__()
        self._stop_grace_minutes = max(int(stop_grace_minutes), 0)
        self._current_timestamp: str | None = None

    def _stop_hit(self, position: OpenPosition, price: float) -> bool:
        if self._stop_grace_active(position):
            return False
        return super()._stop_hit(position, price)

    def _stop_grace_active(self, position: OpenPosition) -> bool:
        if self._stop_grace_minutes <= 0 or position.opened_at is None:
            return False
        if self._current_timestamp is None:
            return False
        if str(position.setup or "") != "trend_pullback_long":
            return False
        market_cluster = str(position.setup_details.get("market_cluster", "") or "").lower()
        if market_cluster != "crypto":
            return False
        current = parse_timestamp(self._current_timestamp)
        if current is None:
            return False
        age_seconds = (current - position.opened_at).total_seconds()
        return 0.0 <= age_seconds < self._stop_grace_minutes * 60


class PodAExecutor(DirectionalExecutor):
    """Directional executor with Pod A-specific stop-grace behavior."""

    def __init__(self, config: AppConfig) -> None:
        super().__init__(config)
        self.portfolio = PodAStopGracePortfolioState(config.pod_a.stop_grace_minutes)
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
