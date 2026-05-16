from __future__ import annotations

from collections import defaultdict
import math

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
        self._blocked_opportunity_slices: set[str] = set()
        for value in config.blocked_opportunity_slices:
            slice_key = self._normalize_slice_key(value)
            if slice_key is not None:
                self._blocked_opportunity_slices.add(slice_key)

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
        if market.class_name == "priceBucket" and self.config.mode == "testnet":
            return self._reject("price_bucket_paper_only")
        if market.class_name == "namedOutcomeBasket" and self.config.mode == "testnet":
            return self._reject("named_outcome_basket_paper_only")
        blocked_slice = self._opportunity_slice_key(opportunity=opportunity, market=market)
        if blocked_slice in self._blocked_opportunity_slices:
            return self._reject(
                "blocked_outcome_slice",
                constraints={"blocked_slice": blocked_slice},
            )
        reference_guard = self._reference_divergence_guard(
            opportunity=opportunity,
            market=market,
        )
        if reference_guard is not None:
            return self._reject("reference_divergence_guard", constraints=reference_guard)
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
        if self.config.mode != "observer":
            exchange_min_reason = self._exchange_min_order_reject_reason(
                opportunity=opportunity,
                order_book=order_book,
                approved_size_usdc=approved_size,
            )
            if exchange_min_reason:
                return self._reject(exchange_min_reason)

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
        if opportunity.side == "BUY_NAMED_NO_BASKET":
            return self._basket_liquidity_reject_reason(opportunity)
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

    def _exchange_min_order_reject_reason(
        self,
        *,
        opportunity: OutcomeOpportunity,
        order_book: OutcomeOrderBook,
        approved_size_usdc: float,
    ) -> str | None:
        min_order_value = max(float(self.config.min_order_value_usdc), 0.0)
        if min_order_value <= 0:
            return None
        if opportunity.side == "BUY_NAMED_NO_BASKET":
            return self._basket_min_order_reject_reason(
                opportunity=opportunity,
                approved_size_usdc=approved_size_usdc,
                min_order_value=min_order_value,
            )
        checks: list[tuple[str, float | None, float]] = []
        if opportunity.side == "BUY_YES":
            checks.append(("yes", order_book.yes.ask, approved_size_usdc))
        elif opportunity.side == "BUY_NO":
            checks.append(("no", order_book.no.ask, approved_size_usdc))
        elif opportunity.side == "BUY_BOTH":
            if order_book.yes.ask is None or order_book.no.ask is None:
                return None
            yes_limit = min(order_book.yes.ask * (1.0 + self.config.max_order_slippage), 0.99999)
            no_limit = min(order_book.no.ask * (1.0 + self.config.max_order_slippage), 0.99999)
            unit_cost = yes_limit + no_limit
            if unit_cost <= 0:
                return "below_exchange_min_order_value"
            qty = self._floor_size(approved_size_usdc / unit_cost)
            if qty <= 0:
                return "below_exchange_min_order_value"
            for side_name, limit_price in (("yes", yes_limit), ("no", no_limit)):
                if qty * self._min_value_price(limit_price) < min_order_value:
                    return f"below_exchange_min_order_value_{side_name}"
            return None
        else:
            return None

        for side_name, ask, spend_usdc in checks:
            if ask is None or ask <= 0:
                continue
            limit_price = min(ask * (1.0 + self.config.max_order_slippage), 0.99999)
            qty = self._floor_size(spend_usdc / limit_price)
            if qty <= 0 or qty * self._min_value_price(limit_price) < min_order_value:
                return f"below_exchange_min_order_value_{side_name}"
        return None

    def _basket_liquidity_reject_reason(self, opportunity: OutcomeOpportunity) -> str | None:
        legs = _basket_legs(opportunity)
        if not legs:
            return "missing_named_basket_legs"
        for leg in legs:
            ask = _optional_float(leg.get("ask"))
            if ask is None or ask <= 0:
                return "missing_named_basket_no_ask"
            depth = _optional_float(leg.get("ask_depth_usdc")) or 0.0
            if depth < self.config.min_no_depth_usdc:
                return "insufficient_named_basket_no_depth"
            spread = _optional_float(leg.get("spread"))
            if spread is not None and spread > self.config.max_spread:
                return "named_basket_no_spread_too_wide"
        return None

    def _basket_min_order_reject_reason(
        self,
        *,
        opportunity: OutcomeOpportunity,
        approved_size_usdc: float,
        min_order_value: float,
    ) -> str | None:
        legs = _basket_legs(opportunity)
        if not legs:
            return "below_exchange_min_order_value_named_basket"
        limit_prices: list[float] = []
        for leg in legs:
            ask = _optional_float(leg.get("ask"))
            if ask is None or ask <= 0:
                return "below_exchange_min_order_value_named_basket"
            limit_prices.append(min(ask * (1.0 + self.config.max_order_slippage), 0.99999))
        unit_cost = sum(limit_prices)
        if unit_cost <= 0:
            return "below_exchange_min_order_value_named_basket"
        qty = self._floor_size(approved_size_usdc / unit_cost)
        if qty <= 0:
            return "below_exchange_min_order_value_named_basket"
        for limit_price in limit_prices:
            if qty * self._min_value_price(limit_price) < min_order_value:
                return "below_exchange_min_order_value_named_basket"
        return None

    def _floor_size(self, value: float) -> float:
        decimals = max(int(self.config.outcome_size_decimals), 0)
        scale = 10**decimals
        return math.floor(max(value, 0.0) * scale) / scale

    def _reference_divergence_guard(
        self,
        *,
        opportunity: OutcomeOpportunity,
        market: OutcomeMarket,
    ) -> dict[str, object] | None:
        if not self.config.block_reference_divergence:
            return None
        underlying = str(market.underlying or opportunity.underlying).strip().upper()
        allowed_underlyings = {
            item.strip().upper()
            for item in self.config.reference_divergence_underlyings
            if item.strip()
        }
        if allowed_underlyings and underlying not in allowed_underlyings:
            return None

        side = str(opportunity.side).strip().upper()
        allowed_sides = {
            item.strip().upper()
            for item in self.config.reference_divergence_sides
            if item.strip()
        }
        if allowed_sides and side not in allowed_sides and side != "BUY_BOTH":
            return None

        edge_type = str(opportunity.edge_type).strip().upper()
        allowed_edge_types = {
            item.strip().upper()
            for item in self.config.reference_divergence_edge_types
            if item.strip()
        }
        if allowed_edge_types and edge_type not in allowed_edge_types:
            return None

        metadata = opportunity.metadata if isinstance(opportunity.metadata, dict) else {}
        rejected_sources = metadata.get("reference_rejected_sources")
        rejected_count = len(rejected_sources) if isinstance(rejected_sources, list) else 0
        max_deviation = _optional_float(metadata.get("reference_max_deviation_bps"))
        min_rejected = max(int(self.config.reference_divergence_min_rejected_sources), 1)
        max_allowed_bps = max(float(self.config.reference_divergence_max_bps), 0.0)
        rejected_hit = rejected_count >= min_rejected
        deviation_hit = max_deviation is not None and max_deviation > max_allowed_bps
        if not rejected_hit and not deviation_hit:
            return None

        return {
            "underlying": underlying,
            "side": side,
            "edge_type": edge_type,
            "reference_price": metadata.get("reference_price"),
            "reference_max_deviation_bps": max_deviation,
            "reference_rejected_source_count": rejected_count,
            "reference_rejected_sources": _compact_rejected_sources(rejected_sources),
            "reference_divergence_max_bps": max_allowed_bps,
            "reference_divergence_min_rejected_sources": min_rejected,
        }

    @staticmethod
    def _min_value_price(limit_price: float) -> float:
        price = max(min(float(limit_price), 0.99999), 0.00001)
        return max(min(price, 1.0 - price), 0.00001)

    @staticmethod
    def _opportunity_slice_key(
        *,
        opportunity: OutcomeOpportunity,
        market: OutcomeMarket,
    ) -> str:
        return ":".join(
            [
                str(market.underlying or opportunity.underlying).strip().upper(),
                str(opportunity.edge_type).strip().upper(),
                str(opportunity.side).strip().upper(),
            ]
        )

    @staticmethod
    def _normalize_slice_key(value: object) -> str | None:
        parts = [part.strip().upper() for part in str(value).replace("/", ":").split(":")]
        if len(parts) != 3 or not all(parts):
            return None
        return ":".join(parts)

    def _reject(
        self,
        reason: str,
        *,
        constraints: dict[str, object] | None = None,
    ) -> SupervisorDecision:
        return SupervisorDecision(
            approved=False,
            approved_size_usdc=0.0,
            reason=reason,
            execution_mode=self.config.mode.upper(),
            constraints=constraints or {},
        )


def _optional_float(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _basket_legs(opportunity: OutcomeOpportunity) -> list[dict[str, object]]:
    metadata = opportunity.metadata if isinstance(opportunity.metadata, dict) else {}
    raw_legs = metadata.get("basket_legs")
    if not isinstance(raw_legs, list):
        return []
    return [leg for leg in raw_legs if isinstance(leg, dict)]


def _compact_rejected_sources(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    compact: list[dict[str, object]] = []
    for item in value[:6]:
        if not isinstance(item, dict):
            continue
        compact.append(
            {
                "source": item.get("source"),
                "symbol": item.get("symbol"),
                "price": item.get("price"),
            }
        )
    return compact
