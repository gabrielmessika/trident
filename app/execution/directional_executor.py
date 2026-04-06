from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass

from app.execution.dry_run import DryRunExecutionVenue
from app.portfolio.directional_state import ClosedTrade, DirectionalPortfolioState
from app.settings import AppConfig
from app.trident.types import RiskDecision, SymbolMarketSnapshot


@dataclass(slots=True)
class ExecutionBatch:
    opened_symbols: list[str]
    skipped_open_symbols: list[str]
    closed_trades: list[ClosedTrade]
    fills: list[dict[str, object]]
    had_open_position_before: dict[str, bool]
    has_open_position_after: dict[str, bool]
    close_reasons_by_symbol: dict[str, str]


class DirectionalExecutor:
    """Shared dry-run execution rules for directional pods."""

    def __init__(self, config: AppConfig) -> None:
        self.portfolio = DirectionalPortfolioState()
        self.venue = DryRunExecutionVenue(config.trident.execution)

    def process_record(
        self,
        *,
        snapshots: list[SymbolMarketSnapshot],
        risk_decisions: list[RiskDecision],
        signal_sides_by_symbol: dict[str, str],
        timestamp: str | None,
    ) -> ExecutionBatch:
        snapshot_by_symbol = {snapshot.symbol: snapshot for snapshot in snapshots}
        closed_trades: list[ClosedTrade] = []
        opened_symbols: list[str] = []
        skipped_open_symbols: list[str] = []
        fills: list[dict[str, object]] = []
        tracked_symbols = {
            *snapshot_by_symbol.keys(),
            *(decision.trade_plan.symbol for decision in risk_decisions),
        }
        had_open_position_before = {
            symbol: self.portfolio.has_open_position(symbol) for symbol in tracked_symbols
        }
        close_reasons_by_symbol: dict[str, str] = {}

        for snapshot in snapshots:
            existing = self.portfolio.open_positions.get(snapshot.symbol)
            if existing is None:
                continue
            close_reason: str | None = None
            if self.portfolio._stop_hit(existing, snapshot.price):
                close_reason = "stop_hit"
            elif (
                signal_sides_by_symbol.get(snapshot.symbol) is not None
                and signal_sides_by_symbol.get(snapshot.symbol) != existing.side
            ):
                close_reason = "opposite_signal"
            elif self.portfolio._time_stop_hit(existing, timestamp):
                close_reason = "time_stop"

            if close_reason is None:
                continue
            fill = self.venue.close_fill(
                symbol=snapshot.symbol,
                side=existing.side,
                mid_price=snapshot.price,
                spread_bps=snapshot.spread_bps,
                notional_usd=existing.target_notional_usd,
                timestamp=timestamp,
            )
            trade = self.portfolio.close_position(
                snapshot.symbol,
                fill.price,
                fill.fee_usd,
                timestamp,
                close_reason,
            )
            if trade is not None:
                closed_trades.append(trade)
                fills.append(asdict(fill))
                close_reasons_by_symbol[trade.symbol] = trade.close_reason

        for decision in risk_decisions:
            if not decision.accepted:
                continue
            snapshot = snapshot_by_symbol.get(decision.trade_plan.symbol)
            if snapshot is None:
                continue
            existing = self.portfolio.open_positions.get(decision.trade_plan.symbol)
            if existing is not None and self._should_upgrade(existing, decision.trade_plan):
                close_fill = self.venue.close_fill(
                    symbol=decision.trade_plan.symbol,
                    side=existing.side,
                    mid_price=snapshot.price,
                    spread_bps=snapshot.spread_bps,
                    notional_usd=existing.target_notional_usd,
                    timestamp=timestamp,
                )
                trade = self.portfolio.close_position(
                    decision.trade_plan.symbol,
                    close_fill.price,
                    close_fill.fee_usd,
                    timestamp,
                    "upgrade_setup",
                )
                if trade is not None:
                    closed_trades.append(trade)
                    fills.append(asdict(close_fill))
                    close_reasons_by_symbol[trade.symbol] = trade.close_reason
            fill = self.venue.open_fill(
                symbol=decision.trade_plan.symbol,
                side=decision.trade_plan.side,
                mid_price=snapshot.price,
                spread_bps=snapshot.spread_bps,
                notional_usd=decision.trade_plan.target_notional_usd,
                timestamp=timestamp,
            )
            if self.portfolio.open_from_plan(
                decision.trade_plan,
                fill.price,
                fill.fee_usd,
                timestamp,
            ):
                opened_symbols.append(decision.trade_plan.symbol)
                fills.append(asdict(fill))
            else:
                skipped_open_symbols.append(decision.trade_plan.symbol)

        return ExecutionBatch(
            opened_symbols=opened_symbols,
            skipped_open_symbols=skipped_open_symbols,
            closed_trades=closed_trades,
            fills=fills,
            had_open_position_before=had_open_position_before,
            has_open_position_after={
                symbol: self.portfolio.has_open_position(symbol) for symbol in tracked_symbols
            },
            close_reasons_by_symbol=close_reasons_by_symbol,
        )

    def _should_upgrade(self, existing: object, plan: object) -> bool:
        existing_side = getattr(existing, "side", "")
        existing_setup = getattr(existing, "setup", "")
        existing_confidence = float(getattr(existing, "confidence", 0.0))
        if existing_side != getattr(plan, "side", ""):
            return False
        return self._setup_rank(getattr(plan, "setup", "")) > self._setup_rank(existing_setup) and (
            float(getattr(plan, "confidence", 0.0)) >= existing_confidence - 0.05
        )

    def _setup_rank(self, setup: str) -> int:
        if setup.startswith("liquidity_sweep_reclaim"):
            return 4
        if setup.startswith("bos_retest"):
            return 3
        if setup.startswith("vwap_reclaim"):
            return 2
        if setup.startswith("trend_pullback"):
            return 1
        return 0

    def finalize(
        self,
        *,
        snapshots: list[SymbolMarketSnapshot],
        timestamp: str | None,
    ) -> tuple[list[ClosedTrade], list[dict[str, object]]]:
        snapshot_by_symbol = {snapshot.symbol: snapshot for snapshot in snapshots}
        closed_trades: list[ClosedTrade] = []
        fills: list[dict[str, object]] = []
        for symbol, existing in list(self.portfolio.open_positions.items()):
            snapshot = snapshot_by_symbol.get(symbol)
            if snapshot is None:
                continue
            fill = self.venue.close_fill(
                symbol=symbol,
                side=existing.side,
                mid_price=snapshot.price,
                spread_bps=snapshot.spread_bps,
                notional_usd=existing.target_notional_usd,
                timestamp=timestamp,
            )
            trade = self.portfolio.close_position(
                symbol,
                fill.price,
                fill.fee_usd,
                timestamp,
                "end_of_backtest",
            )
            if trade is not None:
                closed_trades.append(trade)
                fills.append(asdict(fill))
        return closed_trades, fills
