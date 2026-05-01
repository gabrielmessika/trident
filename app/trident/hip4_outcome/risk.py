from __future__ import annotations

from collections import defaultdict

from app.trident.hip4_outcome.config import Hip4OutcomeConfig
from app.trident.hip4_outcome.models import (
    OutcomeMarket,
    OutcomeOpportunity,
    OutcomeOrderBook,
    OutcomePosition,
    SupervisorDecision,
)


class OutcomeRiskManager:
    def __init__(self, config: Hip4OutcomeConfig) -> None:
        self.config = config

    def evaluate(
        self,
        *,
        opportunity: OutcomeOpportunity,
        market: OutcomeMarket,
        order_book: OutcomeOrderBook,
        open_positions: list[OutcomePosition],
        now_ts: int,
    ) -> SupervisorDecision:
        if opportunity.net_edge < self.config.min_net_edge:
            return self._reject("net_edge_too_low")
        if opportunity.requested_size_usdc <= 0:
            return self._reject("no_available_depth_or_size")
        if market.expiry_ts <= now_ts:
            return self._reject("market_expired")
        time_left = market.expiry_ts - now_ts
        if time_left < self.config.min_time_to_expiry_seconds:
            return self._reject("too_close_to_expiry")
        if time_left > self.config.max_time_to_expiry_seconds:
            return self._reject("expiry_too_far")
        if not market.settlement_source:
            return self._reject("missing_settlement_source")
        if any(position.market_id == market.market_id and position.status == "open" for position in open_positions):
            return self._reject("market_already_open")

        active_positions = [position for position in open_positions if position.status == "open"]
        if len(active_positions) >= self.config.max_outcome_markets_open:
            return self._reject("max_open_markets_reached")

        total_exposure = sum(max(position.max_loss_usdc, 0.0) for position in active_positions)
        if total_exposure + opportunity.max_loss_usdc > self.config.max_total_outcome_exposure_usdc:
            return self._reject("total_outcome_exposure_limit")

        exposure_by_underlying: dict[str, float] = defaultdict(float)
        for position in active_positions:
            exposure_by_underlying[position.underlying.upper()] += max(position.max_loss_usdc, 0.0)
        if (
            exposure_by_underlying[market.underlying.upper()] + opportunity.max_loss_usdc
            > self.config.max_per_underlying_outcome_exposure_usdc
        ):
            return self._reject("underlying_outcome_exposure_limit")

        liquidity_reason = self._liquidity_reject_reason(opportunity, order_book)
        if liquidity_reason:
            return self._reject(liquidity_reason)

        approved_size = min(
            opportunity.requested_size_usdc,
            self.config.max_position_usdc,
            self.config.max_total_outcome_exposure_usdc - total_exposure,
            self.config.max_per_underlying_outcome_exposure_usdc
            - exposure_by_underlying[market.underlying.upper()],
        )
        if approved_size <= 0:
            return self._reject("approved_size_zero")

        if self.config.mode == "observer":
            return SupervisorDecision(
                approved=False,
                approved_size_usdc=0.0,
                reason="observer_mode_signal_only",
                execution_mode="OBSERVER",
                constraints={},
            )
        if self.config.mode == "testnet" and not self.config.allow_testnet_orders:
            return self._reject("testnet_orders_not_enabled")

        return SupervisorDecision(
            approved=True,
            approved_size_usdc=round(approved_size, 6),
            reason="local_outcome_risk_ok",
            execution_mode=self.config.mode.upper(),
            constraints={
                "max_slippage": self.config.max_order_slippage,
                "order_tif": self.config.order_tif,
                "max_loss_usdc": round(approved_size, 6),
            },
        )

    def _liquidity_reject_reason(
        self,
        opportunity: OutcomeOpportunity,
        order_book: OutcomeOrderBook,
    ) -> str | None:
        if opportunity.side in {"BUY_YES", "BUY_BOTH"}:
            if order_book.yes.ask is None:
                return "missing_yes_ask"
            if order_book.yes.ask_depth_usdc < self.config.min_yes_depth_usdc:
                return "insufficient_yes_depth"
            if order_book.yes.spread is not None and order_book.yes.spread > self.config.max_spread:
                return "yes_spread_too_wide"
        if opportunity.side in {"BUY_NO", "BUY_BOTH"}:
            if order_book.no.ask is None:
                return "missing_no_ask"
            if order_book.no.ask_depth_usdc < self.config.min_no_depth_usdc:
                return "insufficient_no_depth"
            if order_book.no.spread is not None and order_book.no.spread > self.config.max_spread:
                return "no_spread_too_wide"
        return None

    def _reject(self, reason: str) -> SupervisorDecision:
        return SupervisorDecision(
            approved=False,
            approved_size_usdc=0.0,
            reason=reason,
            execution_mode=self.config.mode.upper(),
            constraints={},
        )
