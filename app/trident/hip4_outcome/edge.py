from __future__ import annotations

from app.trident.hip4_outcome.config import Hip4OutcomeConfig
from app.trident.hip4_outcome.models import (
    OutcomeMarket,
    OutcomeOpportunity,
    OutcomeOrderBook,
    ProbabilityEstimate,
    ShortExpiryAssessment,
    ShortHorizonFeatures,
)


class OutcomeEdgeDetector:
    def __init__(self, config: Hip4OutcomeConfig) -> None:
        self.config = config

    def detect(
        self,
        *,
        market: OutcomeMarket,
        order_book: OutcomeOrderBook,
        reference_price: float,
        probability: ProbabilityEstimate,
        now_ts: int,
        short_features: ShortHorizonFeatures | None = None,
        short_assessment: ShortExpiryAssessment | None = None,
    ) -> list[OutcomeOpportunity]:
        opportunities: list[OutcomeOpportunity] = []
        if self.config.enable_late_expiry:
            opportunities.extend(
                self._detect_late_expiry(
                    market=market,
                    order_book=order_book,
                    reference_price=reference_price,
                    now_ts=now_ts,
                )
            )
        if self.config.enable_parity:
            opportunity = self._detect_parity(market=market, order_book=order_book)
            if opportunity is not None:
                opportunities.append(opportunity)
        if self.config.enable_model:
            opportunities.extend(
                self._detect_model_mispricing(
                    market=market,
                    order_book=order_book,
                    probability=probability,
                    reference_price=reference_price,
                    now_ts=now_ts,
                )
            )
        if self.config.enable_short_expiry:
            opportunities.extend(
                self._detect_short_expiry(
                    market=market,
                    order_book=order_book,
                    probability=probability,
                    reference_price=reference_price,
                    now_ts=now_ts,
                    features=short_features,
                    assessment=short_assessment,
                )
            )
        return [
            item
            for item in opportunities
            if item.gross_edge >= self.config.min_gross_edge
            and item.net_edge >= self.config.min_net_edge
            and item.requested_size_usdc > 0
        ]

    def assess_short_expiry(
        self,
        *,
        market: OutcomeMarket,
        order_book: OutcomeOrderBook,
        probability: ProbabilityEstimate,
        reference_price: float,
        now_ts: int,
        features: ShortHorizonFeatures | None,
    ) -> ShortExpiryAssessment | None:
        if not self.config.enable_short_expiry:
            return None
        time_left = market.expiry_ts - now_ts
        if time_left <= self.config.min_time_to_expiry_seconds:
            return None
        if time_left > self.config.short_expiry_window_seconds:
            return None
        allowed_periods = {period.strip().lower() for period in self.config.short_expiry_periods if period.strip()}
        if allowed_periods and market.period.strip().lower() not in allowed_periods:
            return None

        book_probability = _book_probability_yes(order_book)
        book_imbalance = _book_imbalance_yes(order_book)
        base_metadata = {
            "reference_price": reference_price,
            "strike": market.strike,
            "period": market.period,
            "time_to_expiry_seconds": time_left,
            "model_probability_yes": probability.probability_yes,
            "book_probability_yes": book_probability,
            "book_imbalance_yes": book_imbalance,
            "yes_bid": order_book.yes.bid,
            "yes_ask": order_book.yes.ask,
            "no_bid": order_book.no.bid,
            "no_ask": order_book.no.ask,
            "yes_bid_depth_usdc": order_book.yes.bid_depth_usdc,
            "yes_ask_depth_usdc": order_book.yes.ask_depth_usdc,
            "no_bid_depth_usdc": order_book.no.bid_depth_usdc,
            "no_ask_depth_usdc": order_book.no.ask_depth_usdc,
        }
        if features is None:
            return self._short_assessment(
                market=market,
                probability_yes=probability.probability_yes,
                book_probability_yes=book_probability,
                book_imbalance_yes=book_imbalance,
                best_side="",
                best_gross_edge=0.0,
                best_net_edge=0.0,
                confidence=0.0,
                reason="short_expiry_missing_features",
                metadata=base_metadata,
            )

        feature_metadata = features.to_metadata()
        metadata = {**base_metadata, **feature_metadata}
        primary_momentum = _primary_momentum(features, self.config.short_expiry_primary_momentum_seconds)
        metadata["short_primary_momentum_bps"] = primary_momentum
        if not features.has_min_history:
            return self._short_assessment(
                market=market,
                probability_yes=probability.probability_yes,
                book_probability_yes=book_probability,
                book_imbalance_yes=book_imbalance,
                best_side="",
                best_gross_edge=0.0,
                best_net_edge=0.0,
                confidence=0.0,
                reason="short_expiry_history_warming",
                metadata=metadata,
            )

        momentum_score = _clamp(
            (primary_momentum or 0.0) / max(self.config.short_expiry_momentum_scale_bps, 0.0001),
            -1.0,
            1.0,
        )
        distance_score = _clamp(
            features.distance_to_strike_bps / max(self.config.short_expiry_distance_scale_bps, 0.0001),
            -1.0,
            1.0,
        )
        book_score = _clamp((book_probability - 0.5) * 2.0, -1.0, 1.0)
        raw_probability = _clamp(
            0.5
            + distance_score * self.config.short_expiry_distance_weight
            + momentum_score * self.config.short_expiry_momentum_weight
            + book_score * self.config.short_expiry_microstructure_weight
            + book_imbalance * self.config.short_expiry_book_imbalance_weight,
            0.0,
            1.0,
        )
        static_weight = _clamp(self.config.short_expiry_static_model_weight, 0.0, 1.0)
        short_probability = _clamp(
            probability.probability_yes * static_weight + raw_probability * (1.0 - static_weight),
            0.0,
            1.0,
        )
        confidence = self._short_confidence(
            short_probability=short_probability,
            features=features,
            momentum_score=momentum_score,
            book_score=book_score,
            book_imbalance=book_imbalance,
        )
        metadata.update(
            {
                "short_raw_probability_yes": round(raw_probability, 8),
                "short_probability_yes": round(short_probability, 8),
                "short_momentum_score": round(momentum_score, 8),
                "short_distance_score": round(distance_score, 8),
                "short_book_score": round(book_score, 8),
            }
        )
        yes_gross = _gross_edge(short_probability, order_book.yes.ask, self.config.short_expiry_max_yes_price)
        no_gross = _gross_edge(1.0 - short_probability, order_book.no.ask, self.config.short_expiry_max_no_price)
        if yes_gross >= no_gross:
            best_side = "BUY_YES"
            best_gross = yes_gross
            best_net = self._net_edge(best_gross)
        else:
            best_side = "BUY_NO"
            best_gross = no_gross
            best_net = self._net_edge(best_gross)
        reason = "Short-expiry model probability above visible ask"
        if best_gross < 0:
            reason = "short_expiry_no_executable_ask"
        elif confidence < self.config.short_expiry_min_confidence:
            reason = "short_expiry_confidence_too_low"
        elif not self._short_alignment_ok(
            best_side=best_side,
            distance_bps=features.distance_to_strike_bps,
            primary_momentum=primary_momentum,
        ):
            reason = "short_expiry_alignment_missing"
        return self._short_assessment(
            market=market,
            probability_yes=round(short_probability, 8),
            book_probability_yes=book_probability,
            book_imbalance_yes=book_imbalance,
            best_side=best_side,
            best_gross_edge=best_gross,
            best_net_edge=best_net,
            confidence=confidence,
            reason=reason,
            metadata=metadata,
        )

    def _net_edge(self, gross_edge: float, *, legs: int = 1) -> float:
        return round(
            gross_edge
            - self.config.estimated_fees * legs
            - self.config.estimated_slippage * legs
            - self.config.safety_margin,
            8,
        )

    def _size_for_side(self, order_book: OutcomeOrderBook, side: str) -> float:
        if side == "BUY_YES":
            depth = order_book.yes.ask_depth_usdc
        elif side == "BUY_NO":
            depth = order_book.no.ask_depth_usdc
        else:
            depth = min(order_book.yes.ask_depth_usdc, order_book.no.ask_depth_usdc) * 2.0
        return round(max(min(self.config.max_position_usdc, depth), 0.0), 6)

    def _short_assessment(
        self,
        *,
        market: OutcomeMarket,
        probability_yes: float,
        book_probability_yes: float,
        book_imbalance_yes: float,
        best_side: str,
        best_gross_edge: float,
        best_net_edge: float,
        confidence: float,
        reason: str,
        metadata: dict[str, object],
    ) -> ShortExpiryAssessment:
        return ShortExpiryAssessment(
            market_id=market.market_id,
            underlying=market.underlying,
            period=market.period,
            probability_yes=round(probability_yes, 8),
            book_probability_yes=round(book_probability_yes, 8),
            book_imbalance_yes=round(book_imbalance_yes, 8),
            best_side=best_side,
            best_gross_edge=round(best_gross_edge, 8),
            best_net_edge=round(best_net_edge, 8),
            confidence=round(confidence, 4),
            reason=reason,
            metadata=dict(metadata),
        )

    def _short_confidence(
        self,
        *,
        short_probability: float,
        features: ShortHorizonFeatures,
        momentum_score: float,
        book_score: float,
        book_imbalance: float,
    ) -> float:
        history_component = min(
            features.history_span_seconds / max(self.config.short_expiry_min_history_seconds * 2.0, 1.0),
            1.0,
        )
        probability_component = abs(short_probability - 0.5) * 2.0
        confidence = (
            0.42
            + probability_component * 0.24
            + abs(momentum_score) * 0.12
            + abs(book_score) * 0.08
            + abs(book_imbalance) * 0.06
            + history_component * 0.08
        )
        return round(_clamp(confidence, 0.05, 0.95), 4)

    def _short_alignment_ok(
        self,
        *,
        best_side: str,
        distance_bps: float,
        primary_momentum: float | None,
    ) -> bool:
        if not self.config.short_expiry_require_momentum_alignment:
            return True
        direction = 1.0 if best_side == "BUY_YES" else -1.0
        momentum_ok = (
            primary_momentum is not None
            and abs(primary_momentum) >= self.config.short_expiry_min_abs_momentum_bps
            and primary_momentum * direction > 0
        )
        distance_ok = (
            abs(distance_bps) >= self.config.strike_buffer_bps
            and distance_bps * direction > 0
        )
        return momentum_ok or distance_ok

    def _detect_late_expiry(
        self,
        *,
        market: OutcomeMarket,
        order_book: OutcomeOrderBook,
        reference_price: float,
        now_ts: int,
    ) -> list[OutcomeOpportunity]:
        time_left = market.expiry_ts - now_ts
        if time_left <= self.config.min_time_to_expiry_seconds:
            return []
        if time_left > self.config.late_expiry_window_seconds:
            return []
        buffer = self.config.strike_buffer_bps / 10_000.0
        metadata = {
            "reference_price": reference_price,
            "strike": market.strike,
            "time_to_expiry_seconds": time_left,
            "yes_ask": order_book.yes.ask,
            "no_ask": order_book.no.ask,
            "yes_depth_usdc": order_book.yes.ask_depth_usdc,
            "no_depth_usdc": order_book.no.ask_depth_usdc,
        }
        if reference_price > market.strike * (1.0 + buffer) and order_book.yes.ask is not None:
            if order_book.yes.ask <= self.config.max_late_yes_price:
                gross = round(1.0 - order_book.yes.ask, 8)
                net = self._net_edge(gross)
                confidence = min(0.95, 0.62 + gross * 0.25)
                return [
                    OutcomeOpportunity(
                        market_id=market.market_id,
                        outcome=market.outcome,
                        underlying=market.underlying,
                        side="BUY_YES",
                        edge_type="LATE_EXPIRY",
                        gross_edge=gross,
                        estimated_fees=self.config.estimated_fees,
                        estimated_slippage=self.config.estimated_slippage,
                        net_edge=net,
                        confidence=round(confidence, 4),
                        requested_size_usdc=self._size_for_side(order_book, "BUY_YES"),
                        max_loss_usdc=self._size_for_side(order_book, "BUY_YES"),
                        expiry_ts=market.expiry_ts,
                        reason="Underlying above strike near expiry; YES underpriced",
                        metadata=metadata,
                    )
                ]
        if reference_price < market.strike * (1.0 - buffer) and order_book.no.ask is not None:
            if order_book.no.ask <= self.config.max_late_no_price:
                gross = round(1.0 - order_book.no.ask, 8)
                net = self._net_edge(gross)
                confidence = min(0.95, 0.62 + gross * 0.25)
                return [
                    OutcomeOpportunity(
                        market_id=market.market_id,
                        outcome=market.outcome,
                        underlying=market.underlying,
                        side="BUY_NO",
                        edge_type="LATE_EXPIRY",
                        gross_edge=gross,
                        estimated_fees=self.config.estimated_fees,
                        estimated_slippage=self.config.estimated_slippage,
                        net_edge=net,
                        confidence=round(confidence, 4),
                        requested_size_usdc=self._size_for_side(order_book, "BUY_NO"),
                        max_loss_usdc=self._size_for_side(order_book, "BUY_NO"),
                        expiry_ts=market.expiry_ts,
                        reason="Underlying below strike near expiry; NO underpriced",
                        metadata=metadata,
                    )
                ]
        return []

    def _detect_parity(
        self,
        *,
        market: OutcomeMarket,
        order_book: OutcomeOrderBook,
    ) -> OutcomeOpportunity | None:
        if order_book.yes.ask is None or order_book.no.ask is None:
            return None
        cost = order_book.yes.ask + order_book.no.ask
        gross = round(1.0 - cost, 8)
        net = self._net_edge(gross, legs=2)
        return OutcomeOpportunity(
            market_id=market.market_id,
            outcome=market.outcome,
            underlying=market.underlying,
            side="BUY_BOTH",
            edge_type="PARITY",
            gross_edge=gross,
            estimated_fees=self.config.estimated_fees * 2,
            estimated_slippage=self.config.estimated_slippage * 2,
            net_edge=net,
            confidence=0.9,
            requested_size_usdc=self._size_for_side(order_book, "BUY_BOTH"),
            max_loss_usdc=self._size_for_side(order_book, "BUY_BOTH"),
            expiry_ts=market.expiry_ts,
            reason=f"YES+NO ask below 1: {cost:.6f}",
            metadata={
                "yes_ask": order_book.yes.ask,
                "no_ask": order_book.no.ask,
                "combined_cost": cost,
                "yes_depth_usdc": order_book.yes.ask_depth_usdc,
                "no_depth_usdc": order_book.no.ask_depth_usdc,
                "strike": market.strike,
            },
        )

    def _detect_model_mispricing(
        self,
        *,
        market: OutcomeMarket,
        order_book: OutcomeOrderBook,
        probability: ProbabilityEstimate,
        reference_price: float,
        now_ts: int,
    ) -> list[OutcomeOpportunity]:
        opportunities: list[OutcomeOpportunity] = []
        time_left = market.expiry_ts - now_ts
        base_metadata = {
            "reference_price": reference_price,
            "strike": market.strike,
            "time_to_expiry_seconds": time_left,
            "probability_yes": probability.probability_yes,
            "probability_model": probability.model_name,
            "probability_confidence": probability.confidence,
            **probability.inputs,
        }
        if order_book.yes.ask is not None:
            gross = round(probability.probability_yes - order_book.yes.ask, 8)
            opportunities.append(
                OutcomeOpportunity(
                    market_id=market.market_id,
                    outcome=market.outcome,
                    underlying=market.underlying,
                    side="BUY_YES",
                    edge_type="MODEL",
                    gross_edge=gross,
                    estimated_fees=self.config.estimated_fees,
                    estimated_slippage=self.config.estimated_slippage,
                    net_edge=self._net_edge(gross),
                    confidence=probability.confidence,
                    requested_size_usdc=self._size_for_side(order_book, "BUY_YES"),
                    max_loss_usdc=self._size_for_side(order_book, "BUY_YES"),
                    expiry_ts=market.expiry_ts,
                    reason="Model probability above YES ask",
                    metadata={**base_metadata, "yes_ask": order_book.yes.ask},
                )
            )
        if order_book.no.ask is not None:
            probability_no = 1.0 - probability.probability_yes
            gross = round(probability_no - order_book.no.ask, 8)
            opportunities.append(
                OutcomeOpportunity(
                    market_id=market.market_id,
                    outcome=market.outcome,
                    underlying=market.underlying,
                    side="BUY_NO",
                    edge_type="MODEL",
                    gross_edge=gross,
                    estimated_fees=self.config.estimated_fees,
                    estimated_slippage=self.config.estimated_slippage,
                    net_edge=self._net_edge(gross),
                    confidence=probability.confidence,
                    requested_size_usdc=self._size_for_side(order_book, "BUY_NO"),
                    max_loss_usdc=self._size_for_side(order_book, "BUY_NO"),
                    expiry_ts=market.expiry_ts,
                    reason="Model probability above NO ask",
                    metadata={**base_metadata, "no_ask": order_book.no.ask},
                )
            )
        return opportunities

    def _detect_short_expiry(
        self,
        *,
        market: OutcomeMarket,
        order_book: OutcomeOrderBook,
        probability: ProbabilityEstimate,
        reference_price: float,
        now_ts: int,
        features: ShortHorizonFeatures | None,
        assessment: ShortExpiryAssessment | None,
    ) -> list[OutcomeOpportunity]:
        short_assessment = assessment or self.assess_short_expiry(
            market=market,
            order_book=order_book,
            probability=probability,
            reference_price=reference_price,
            now_ts=now_ts,
            features=features,
        )
        if short_assessment is None:
            return []
        if short_assessment.reason != "Short-expiry model probability above visible ask":
            return []
        if short_assessment.best_side not in {"BUY_YES", "BUY_NO"}:
            return []
        if short_assessment.confidence < self.config.short_expiry_min_confidence:
            return []
        return [
            OutcomeOpportunity(
                market_id=market.market_id,
                outcome=market.outcome,
                underlying=market.underlying,
                side=short_assessment.best_side,
                edge_type="SHORT_EXPIRY",
                gross_edge=short_assessment.best_gross_edge,
                estimated_fees=self.config.estimated_fees,
                estimated_slippage=self.config.estimated_slippage,
                net_edge=short_assessment.best_net_edge,
                confidence=short_assessment.confidence,
                requested_size_usdc=self._size_for_side(order_book, short_assessment.best_side),
                max_loss_usdc=self._size_for_side(order_book, short_assessment.best_side),
                expiry_ts=market.expiry_ts,
                reason=short_assessment.reason,
                metadata=dict(short_assessment.metadata),
            )
        ]


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _gross_edge(probability: float, ask: float | None, max_price: float) -> float:
    if ask is None or ask <= 0 or ask > max_price:
        return -1.0
    return round(probability - ask, 8)


def _book_probability_yes(order_book: OutcomeOrderBook) -> float:
    estimates: list[float] = []
    yes_mid = _midpoint(order_book.yes.bid, order_book.yes.ask)
    if yes_mid is not None:
        estimates.append(yes_mid)
    no_mid = _midpoint(order_book.no.bid, order_book.no.ask)
    if no_mid is not None:
        estimates.append(1.0 - no_mid)
    if not estimates:
        return 0.5
    return round(_clamp(sum(estimates) / len(estimates), 0.0, 1.0), 8)


def _midpoint(bid: float | None, ask: float | None) -> float | None:
    if bid is not None and ask is not None:
        return _clamp((bid + ask) / 2.0, 0.0, 1.0)
    if bid is not None:
        return _clamp(bid, 0.0, 1.0)
    if ask is not None:
        return _clamp(ask, 0.0, 1.0)
    return None


def _book_imbalance_yes(order_book: OutcomeOrderBook) -> float:
    yes_pressure = _side_pressure(order_book.yes.bid_depth_usdc, order_book.yes.ask_depth_usdc)
    no_pressure = _side_pressure(order_book.no.bid_depth_usdc, order_book.no.ask_depth_usdc)
    return round(_clamp(yes_pressure - no_pressure, -1.0, 1.0), 8)


def _side_pressure(bid_depth: float, ask_depth: float) -> float:
    total = max(bid_depth, 0.0) + max(ask_depth, 0.0)
    if total <= 0:
        return 0.5
    return max(bid_depth, 0.0) / total


def _primary_momentum(features: ShortHorizonFeatures, primary_window: int) -> float | None:
    direct = features.momentum_bps(primary_window)
    if direct is not None:
        return direct
    for _, value in sorted(features.momentum_bps_by_window.items()):
        if value is not None:
            return value
    return None
