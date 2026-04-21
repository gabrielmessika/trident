from __future__ import annotations

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
        try:
            return super().process_record(
                snapshots=snapshots,
                risk_decisions=risk_decisions,
                signal_sides_by_symbol=signal_sides_by_symbol,
                timestamp=timestamp,
                entry_allowed_symbols=entry_allowed_symbols,
                managed_symbols=managed_symbols,
                allowed_symbols=allowed_symbols,
            )
        finally:
            self.portfolio._current_timestamp = None
