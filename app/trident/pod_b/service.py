from __future__ import annotations

from app.settings import AppConfig
from app.trident.pod_b.signals import BreakoutContext, BreakoutSignal


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(value, upper))


class BreakoutService:
    """Replay-first Pod B bis breakout detector using enriched TRIDENT snapshots."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._enabled_setups = {
            item.strip()
            for item in config.pod_b.bis_enabled_setups
            if item.strip()
        }

    def evaluate_many(self, contexts: list[BreakoutContext]) -> list[BreakoutSignal]:
        signals: list[BreakoutSignal] = []
        for context in contexts:
            signal = self.evaluate(context)
            if signal is not None:
                signals.append(signal)
        return sorted(signals, key=lambda item: item.confidence, reverse=True)

    def evaluate(self, context: BreakoutContext) -> BreakoutSignal | None:
        if not self._passes_filters(context):
            return None
        compression = self._compression_breakout(context)
        expansion = self._vol_expansion(context)
        squeeze_release = self._ttm_squeeze_release(context)
        candidates = [
            signal
            for signal in (compression, expansion, squeeze_release)
            if signal is not None
        ]
        enabled_candidates = [signal for signal in candidates if self._setup_enabled(signal)]
        if not enabled_candidates:
            return None
        best = max(enabled_candidates, key=lambda item: item.confidence)
        if best.confidence < self._config.pod_b.bis_min_confidence:
            return None
        return best

    def review_context(self, context: BreakoutContext) -> dict[str, object]:
        direction = self._direction(context)
        raw_candidates = [
            signal
            for signal in (
                self._compression_breakout(context),
                self._vol_expansion(context),
                self._ttm_squeeze_release(context),
            )
            if signal is not None
        ]
        enabled_candidates = [
            signal for signal in raw_candidates if self._setup_enabled(signal)
        ]
        best = max(enabled_candidates, key=lambda item: item.confidence) if enabled_candidates else None
        failure_reasons = self._failure_reasons(context, direction)
        reason_summary = (
            self._signal_setup_summary(best)
            if best is not None and best.confidence >= self._config.pod_b.bis_min_confidence
            else ", ".join(self._humanize_reason(name) for name in failure_reasons[:3])
        )
        return {
            "symbol": context.symbol,
            "status": (
                "signaled"
                if best is not None and best.confidence >= self._config.pod_b.bis_min_confidence
                else "filtered"
            ),
            "preferred_side": direction or "neutral",
            "candidate_setups": [signal.setup for signal in enabled_candidates],
            "failure_reasons": failure_reasons,
            "reason_summary": reason_summary,
            "context": {
                "regime": context.regime,
                "compression_score": round(context.compression_score, 4),
                "activity_score": round(self._activity_score(context), 4),
                "breakout_score": round(self._breakout_score(context), 4),
                "ttm_squeeze_score": round(self._ttm_squeeze_score(context), 4),
                "volume_ratio": round(context.volume_ratio, 4),
                "trade_count_ratio": round(context.trade_count_ratio, 4),
            },
        }

    def _passes_filters(self, context: BreakoutContext) -> bool:
        blocked_symbols = {
            symbol.strip().upper()
            for symbol in self._config.pod_b.bis_blocked_symbols
            if symbol.strip()
        }
        if context.symbol.upper() in blocked_symbols:
            return False
        allowed_regimes = set(self._config.pod_b.bis_allowed_regimes)
        if allowed_regimes and context.regime not in allowed_regimes:
            return False
        if str(context.market_cluster).strip().lower() != "crypto":
            return False
        if not context.btc_aligned:
            return False
        if context.price <= 0:
            return False
        if abs(context.structure_score) < self._config.pod_b.bis_min_abs_structure_score:
            return False
        if self._trend_quality_bps(context) < self._config.pod_b.bis_min_trend_quality_bps:
            return False
        if context.realized_vol_short_bps < self._config.pod_b.bis_min_realized_vol_short_bps:
            return False
        if context.spread_bps > self._config.pod_b.bis_max_spread_bps:
            return False
        if context.bucket_notional_usd < self._config.pod_b.bis_min_bucket_notional_usd:
            return False
        if context.bucket_trade_count < self._config.pod_b.bis_min_bucket_trade_count:
            return False
        if abs(context.vwap_distance_bps) > self._config.pod_b.bis_max_chase_distance_bps:
            return False
        return True

    def _compression_breakout(self, context: BreakoutContext) -> BreakoutSignal | None:
        direction = self._direction(context)
        if direction is None:
            return None
        if not self._direction_enabled(direction):
            return None
        activity_score = self._activity_score(context)
        breakout_score = self._breakout_score(context)
        if context.compression_score < self._config.pod_b.bis_min_compression_score:
            return None
        if activity_score < self._config.pod_b.bis_min_activity_score:
            return None
        if breakout_score < self._config.pod_b.bis_min_breakout_score:
            return None
        flow_support_quality = self._flow_support_quality(context, direction)
        vwap_reclaim_quality = self._vwap_reclaim_quality(context, direction)
        if flow_support_quality < 0.44 or vwap_reclaim_quality < 0.42:
            return None
        if (
            context.volume_ratio < self._config.pod_b.bis_min_volume_ratio
            and context.trade_count_ratio < self._config.pod_b.bis_min_trade_count_ratio
        ):
            return None
        stop_bps = self._compression_stop_bps(context)
        components = self._confidence_components(
            context=context,
            activity_score=activity_score,
            breakout_score=breakout_score,
            setup_bonus=0.08,
            flow_support_quality=flow_support_quality,
            vwap_reclaim_quality=vwap_reclaim_quality,
        )
        signal = BreakoutSignal(
            symbol=context.symbol,
            side=direction,
            setup=f"compression_breakout_{direction}",
            confidence=round(self._aggregate_confidence(components), 3),
            entry_price=context.price,
            stop_bps_hint=stop_bps,
            market_cluster=context.market_cluster,
            cluster_leader=context.cluster_leader,
            setup_details={
                "compression_score": round(context.compression_score, 4),
                "activity_score": round(activity_score, 4),
                "breakout_score": round(breakout_score, 4),
                "flow_support_quality": round(flow_support_quality, 4),
                "volume_ratio": round(context.volume_ratio, 4),
                "trade_count_ratio": round(context.trade_count_ratio, 4),
                "vwap_reclaim_quality": round(vwap_reclaim_quality, 4),
                "regime": context.regime,
            },
            confidence_components=components,
        )
        return self._with_microstructure_watch_details(signal, context)

    def _vol_expansion(self, context: BreakoutContext) -> BreakoutSignal | None:
        direction = self._direction(context)
        if direction is None:
            return None
        if not self._direction_enabled(direction):
            return None
        if direction == "long":
            if context.structure_score <= 0:
                return None
            if (
                context.vwap_distance_bps
                < self._config.pod_b.bis_min_directional_vwap_distance_bps
            ):
                return None
            if context.trade_flow_bias < 0.05:
                return None
            if context.book_imbalance < -0.05:
                return None
            if context.delta_trade_flow_bias < 0.0 and context.microprice_dislocation_bps < 0.0:
                return None
            if (
                self._config.pod_b.bis_strict_continuation_filter_enabled
                and not self._matches_strict_continuation_pattern(context)
            ):
                return None
        if direction == "short":
            if context.structure_score >= 0:
                return None
            if (
                context.vwap_distance_bps
                > -self._config.pod_b.bis_min_directional_vwap_distance_bps
            ):
                return None
        activity_score = self._activity_score(context)
        breakout_score = self._breakout_score(context)
        vol_ratio = self._vol_ratio(context)
        if activity_score < min(self._config.pod_b.bis_min_activity_score + 0.05, 0.95):
            return None
        if breakout_score < self._config.pod_b.bis_min_breakout_score:
            return None
        vwap_reclaim_quality = self._vwap_reclaim_quality(context, direction)
        money_flow_quality = self._money_flow_quality(context, direction)
        if vwap_reclaim_quality < 0.40 or money_flow_quality < 0.36:
            return None
        if vol_ratio < 1.15 and abs(context.delta_trade_flow_bias) < 0.22:
            return None
        stop_bps = self._expansion_stop_bps(context)
        components = self._confidence_components(
            context=context,
            activity_score=activity_score,
            breakout_score=breakout_score,
            setup_bonus=0.05,
            flow_support_quality=self._flow_support_quality(context, direction),
            vwap_reclaim_quality=vwap_reclaim_quality,
            money_flow_quality=money_flow_quality,
        )
        components["vol_expansion_quality"] = round(_clamp(vol_ratio / 2.0), 4)
        signal = BreakoutSignal(
            symbol=context.symbol,
            side=direction,
            setup=f"vol_expansion_{direction}",
            confidence=round(self._aggregate_confidence(components), 3),
            entry_price=context.price,
            stop_bps_hint=stop_bps,
            market_cluster=context.market_cluster,
            cluster_leader=context.cluster_leader,
            setup_details={
                "compression_score": round(context.compression_score, 4),
                "activity_score": round(activity_score, 4),
                "breakout_score": round(breakout_score, 4),
                "vol_ratio": round(vol_ratio, 4),
                "money_flow_quality": round(money_flow_quality, 4),
                "strict_continuation_filter": bool(
                    direction == "long"
                    and self._config.pod_b.bis_strict_continuation_filter_enabled
                ),
                "vwap_reclaim_quality": round(vwap_reclaim_quality, 4),
                "regime": context.regime,
            },
            confidence_components=components,
        )
        return self._with_microstructure_watch_details(signal, context)

    def _ttm_squeeze_release(self, context: BreakoutContext) -> BreakoutSignal | None:
        direction = self._direction(context)
        if direction is None:
            return None
        if not self._direction_enabled(direction):
            return None
        activity_score = self._activity_score(context)
        breakout_score = self._breakout_score(context)
        squeeze_release_quality = self._ttm_squeeze_score(context)
        flow_support_quality = self._flow_support_quality(context, direction)
        vwap_reclaim_quality = self._vwap_reclaim_quality(context, direction)
        money_flow_quality = self._money_flow_quality(context, direction)
        if context.compression_score < max(self._config.pod_b.bis_min_compression_score - 0.05, 0.48):
            return None
        if squeeze_release_quality < 0.60:
            return None
        if activity_score < max(self._config.pod_b.bis_min_activity_score - 0.08, 0.50):
            return None
        if breakout_score < max(self._config.pod_b.bis_min_breakout_score - 0.02, 0.42):
            return None
        if flow_support_quality < 0.52 or vwap_reclaim_quality < 0.50 or money_flow_quality < 0.48:
            return None
        if self._vol_ratio(context) < 1.05 and abs(context.delta_trade_flow_bias) < 0.18:
            return None
        stop_bps = self._squeeze_release_stop_bps(context)
        components = self._confidence_components(
            context=context,
            activity_score=activity_score,
            breakout_score=breakout_score,
            setup_bonus=0.07,
            squeeze_release_quality=squeeze_release_quality,
            flow_support_quality=flow_support_quality,
            vwap_reclaim_quality=vwap_reclaim_quality,
            money_flow_quality=money_flow_quality,
        )
        signal = BreakoutSignal(
            symbol=context.symbol,
            side=direction,
            setup=f"ttm_squeeze_release_{direction}",
            confidence=round(self._aggregate_confidence(components), 3),
            entry_price=context.price,
            stop_bps_hint=stop_bps,
            market_cluster=context.market_cluster,
            cluster_leader=context.cluster_leader,
            setup_details={
                "compression_score": round(context.compression_score, 4),
                "activity_score": round(activity_score, 4),
                "breakout_score": round(breakout_score, 4),
                "money_flow_quality": round(money_flow_quality, 4),
                "squeeze_release_quality": round(squeeze_release_quality, 4),
                "vwap_reclaim_quality": round(vwap_reclaim_quality, 4),
                "regime": context.regime,
            },
            confidence_components=components,
        )
        return self._with_microstructure_watch_details(signal, context)

    def _with_microstructure_watch_details(
        self,
        signal: BreakoutSignal,
        context: BreakoutContext,
    ) -> BreakoutSignal:
        signal.setup_details = {
            **dict(signal.setup_details or {}),
            **self._microstructure_watch_details(context, signal.side),
        }
        return signal

    def _microstructure_watch_details(
        self,
        context: BreakoutContext,
        side: str,
    ) -> dict[str, float | str | bool]:
        liquidity_pull_score_raw, liquidity_pull_direction = self._liquidity_pull_signal(context)
        depth_refill_score_raw, depth_refill_direction = self._depth_refill_signal(context)
        touch_refill_score_raw, touch_refill_direction = self._touch_refill_signal(context)
        liquidity_pull_score = (
            liquidity_pull_score_raw if liquidity_pull_direction == side else 0.0
        )
        depth_refill_score_depth10 = (
            depth_refill_score_raw if depth_refill_direction == side else 0.0
        )
        depth_refill_score_touch = (
            touch_refill_score_raw if touch_refill_direction == side else 0.0
        )
        return {
            "spread_bps": round(context.spread_bps, 4),
            "bucket_notional_usd": round(context.bucket_notional_usd, 4),
            "liquidity_pull_score": round(liquidity_pull_score, 4),
            "liquidity_pull_score_raw": round(liquidity_pull_score_raw, 4),
            "liquidity_pull_direction": liquidity_pull_direction,
            "depth_refill_score": round(
                max(depth_refill_score_depth10, depth_refill_score_touch),
                4,
            ),
            "depth_refill_score_depth10": round(depth_refill_score_depth10, 4),
            "depth_refill_score_touch": round(depth_refill_score_touch, 4),
            "depth_refill_score_depth10_raw": round(depth_refill_score_raw, 4),
            "depth_refill_score_touch_raw": round(touch_refill_score_raw, 4),
            "depth_refill_direction_depth10": depth_refill_direction,
            "depth_refill_direction_touch": touch_refill_direction,
        }

    def _liquidity_pull_signal(
        self,
        context: BreakoutContext,
    ) -> tuple[float, str]:
        bullish_pull = (
            max(-context.ask_depth_velocity, 0.0) * 0.75
            + max(context.bid_depth_velocity, 0.0) * 0.25
        )
        bearish_pull = (
            max(-context.bid_depth_velocity, 0.0) * 0.75
            + max(context.ask_depth_velocity, 0.0) * 0.25
        )
        direction = "long" if bullish_pull >= bearish_pull else "short"
        dominant_pull = bullish_pull if direction == "long" else bearish_pull
        flow_support = self._positive_for_direction(
            context.trade_flow_bias * 0.55
            + context.delta_trade_flow_bias * 0.20
            + context.book_imbalance * 0.15
            + context.delta_book_imbalance * 0.10,
            direction,
            scale=0.45,
        )
        micro_support = self._positive_for_direction(
            context.microprice_dislocation_bps,
            direction,
            scale=1.5,
        )
        spread_widening = _clamp(max(context.delta_spread_bps, 0.0) / 2.0)
        score = (
            _clamp(dominant_pull / 1.25) * 0.45
            + flow_support * 0.25
            + micro_support * 0.15
            + spread_widening * 0.15
        )
        return round(score, 4), direction

    def _depth_refill_signal(
        self,
        context: BreakoutContext,
    ) -> tuple[float, str]:
        bullish_refill = (
            max(context.bid_depth_velocity, 0.0) * 0.75
            + max(-context.ask_depth_velocity, 0.0) * 0.15
        )
        bearish_refill = (
            max(context.ask_depth_velocity, 0.0) * 0.75
            + max(-context.bid_depth_velocity, 0.0) * 0.15
        )
        direction = "long" if bullish_refill >= bearish_refill else "short"
        dominant_refill = bullish_refill if direction == "long" else bearish_refill
        flow_support = self._positive_for_direction(
            context.book_imbalance * 0.55 + context.trade_flow_bias * 0.45,
            direction,
            scale=0.45,
        )
        micro_support = self._positive_for_direction(
            context.microprice_dislocation_bps,
            direction,
            scale=1.25,
        )
        spread_support = 1.0 - _clamp(context.spread_bps / 8.0)
        spread_normalization = _clamp(max(-context.delta_spread_bps, 0.0) / 2.0)
        score = (
            _clamp(dominant_refill / 1.25) * 0.35
            + flow_support * 0.25
            + micro_support * 0.20
            + spread_support * 0.10
            + spread_normalization * 0.10
        )
        return round(score, 4), direction

    def _touch_refill_signal(
        self,
        context: BreakoutContext,
    ) -> tuple[float, str]:
        bullish_refill = (
            max(context.best_bid_size_velocity, 0.0) * 0.75
            + max(-context.best_ask_size_velocity, 0.0) * 0.15
        )
        bearish_refill = (
            max(context.best_ask_size_velocity, 0.0) * 0.75
            + max(-context.best_bid_size_velocity, 0.0) * 0.15
        )
        direction = "long" if bullish_refill >= bearish_refill else "short"
        dominant_refill = bullish_refill if direction == "long" else bearish_refill
        flow_support = self._positive_for_direction(
            context.book_imbalance * 0.55 + context.trade_flow_bias * 0.45,
            direction,
            scale=0.45,
        )
        micro_support = self._positive_for_direction(
            context.microprice_dislocation_bps,
            direction,
            scale=1.10,
        )
        spread_support = 1.0 - _clamp(context.spread_bps / 6.0)
        spread_normalization = _clamp(max(-context.delta_spread_bps, 0.0) / 1.5)
        score = (
            _clamp(dominant_refill / 1.10) * 0.35
            + flow_support * 0.25
            + micro_support * 0.20
            + spread_support * 0.10
            + spread_normalization * 0.10
        )
        return round(score, 4), direction

    def _positive_for_direction(
        self,
        value: float,
        direction: str,
        *,
        scale: float,
    ) -> float:
        if scale <= 0:
            return 0.0
        signed = value if direction == "long" else -value
        return _clamp(signed / scale)

    def _matches_strict_continuation_pattern(self, context: BreakoutContext) -> bool:
        return (
            context.structure_score >= 0.20
            and context.vwap_distance_bps >= 4.0
            and context.trade_flow_bias >= 0.05
            and context.book_imbalance >= 0.0
            and context.delta_trade_flow_bias >= 0.05
            and context.bucket_range_bps >= 30.0
            and context.realized_vol_short_bps >= 2.2
            and context.spread_bps <= 2.2
        )

    def _direction(self, context: BreakoutContext) -> str | None:
        trend = 1 if context.ema_fast >= context.ema_slow else -1
        structure = 1 if context.structure_score >= 0 else -1
        flow = 1 if (context.trade_flow_bias + context.delta_trade_flow_bias * 0.5) >= 0 else -1
        book = 1 if (
            context.book_imbalance
            + context.delta_book_imbalance * 0.5
            + context.microprice_dislocation_bps / 5.0
        ) >= 0 else -1
        score = trend + structure + flow + book
        if score >= 2:
            return "long"
        if score <= -2:
            return "short"
        return None

    def _activity_score(self, context: BreakoutContext) -> float:
        vol_ratio = self._vol_ratio(context)
        return min(
            1.0,
            min(context.volume_ratio / 3.0, 1.0) * 0.25
            + min(context.trade_count_ratio / 3.0, 1.0) * 0.20
            + min(vol_ratio / 2.0, 1.0) * 0.20
            + min(abs(context.delta_trade_flow_bias) / 0.45, 1.0) * 0.15
            + min(abs(context.microprice_dislocation_bps) / 2.5, 1.0) * 0.10
            + min(
                abs(context.trade_flow_bias) * 0.6 + abs(context.book_imbalance) * 0.4,
                1.0,
            ) * 0.10
        )

    def _breakout_score(self, context: BreakoutContext) -> float:
        directional_pressure = abs(
            context.trade_flow_bias * 0.45
            + context.book_imbalance * 0.20
            + context.delta_trade_flow_bias * 0.20
            + context.delta_book_imbalance * 0.10
            + context.microprice_dislocation_bps / 10.0 * 0.05
        )
        trend_quality = abs((context.ema_fast - context.ema_slow) / max(context.price, 1e-9) * 10_000.0)
        return min(
            1.0,
            min(directional_pressure, 1.0) * 0.45
            + min(abs(context.vwap_distance_bps) / 20.0, 1.0) * 0.20
            + min(trend_quality / 15.0, 1.0) * 0.20
            + min(context.bucket_range_bps / 40.0, 1.0) * 0.15
        )

    def _ttm_squeeze_score(self, context: BreakoutContext) -> float:
        vol_ratio = self._vol_ratio(context)
        squeeze_component = _clamp((context.compression_score - 0.42) / 0.38)
        release_component = _clamp(
            (
                abs(context.vwap_distance_bps)
                - self._config.pod_b.bis_min_directional_vwap_distance_bps
                + 2.0
            )
            / 16.0
        )
        expansion_component = _clamp((vol_ratio - 1.0) / 0.90)
        impulse_component = _clamp((context.bucket_range_bps - 16.0) / 28.0)
        return min(
            1.0,
            squeeze_component * 0.35
            + expansion_component * 0.25
            + release_component * 0.20
            + impulse_component * 0.20,
        )

    def _failure_reasons(self, context: BreakoutContext, direction: str | None) -> list[str]:
        reasons: list[str] = []
        allowed_regimes = set(self._config.pod_b.bis_allowed_regimes)
        if allowed_regimes and context.regime not in allowed_regimes:
            reasons.append("regime_not_allowed")
        if str(context.market_cluster).strip().lower() != "crypto":
            reasons.append("market_cluster_not_crypto")
        if not context.btc_aligned:
            reasons.append("btc_not_aligned")
        if abs(context.structure_score) < self._config.pod_b.bis_min_abs_structure_score:
            reasons.append("structure_too_weak")
        if self._trend_quality_bps(context) < self._config.pod_b.bis_min_trend_quality_bps:
            reasons.append("trend_quality_too_low")
        if context.realized_vol_short_bps < self._config.pod_b.bis_min_realized_vol_short_bps:
            reasons.append("realized_vol_too_low")
        if context.spread_bps > self._config.pod_b.bis_max_spread_bps:
            reasons.append("spread_too_wide")
        if context.bucket_notional_usd < self._config.pod_b.bis_min_bucket_notional_usd:
            reasons.append("bucket_notional_too_low")
        if context.bucket_trade_count < self._config.pod_b.bis_min_bucket_trade_count:
            reasons.append("trade_count_too_low")
        if abs(context.vwap_distance_bps) > self._config.pod_b.bis_max_chase_distance_bps:
            reasons.append("chase_distance_too_large")
        if direction is None:
            reasons.append("direction_unclear")
        if context.compression_score < self._config.pod_b.bis_min_compression_score:
            reasons.append("compression_too_low")
        if self._activity_score(context) < self._config.pod_b.bis_min_activity_score:
            reasons.append("activity_too_low")
        if self._breakout_score(context) < self._config.pod_b.bis_min_breakout_score:
            reasons.append("breakout_too_weak")
        if self._ttm_squeeze_score(context) < 0.60:
            reasons.append("squeeze_not_ready")
        if not reasons:
            reasons.append("no_setup_family_match")
        return reasons

    def _signal_setup_summary(self, signal: BreakoutSignal) -> str:
        return signal.setup.replace("_", " ")

    def _humanize_reason(self, reason: str) -> str:
        return reason.replace("_", " ")

    def _flow_support_quality(self, context: BreakoutContext, direction: str) -> float:
        sign = 1.0 if direction == "long" else -1.0
        directional_pressure = sign * (
            context.trade_flow_bias * 0.45
            + context.book_imbalance * 0.25
            + context.delta_trade_flow_bias * 0.20
            + context.delta_book_imbalance * 0.10
        )
        activity_tail = _clamp((max(context.volume_ratio, context.trade_count_ratio) - 1.0) / 2.0)
        return _clamp(0.5 + directional_pressure * 0.55 + activity_tail * 0.10)

    def _vwap_reclaim_quality(self, context: BreakoutContext, direction: str) -> float:
        sign = 1.0 if direction == "long" else -1.0
        signed_distance = sign * context.vwap_distance_bps
        distance_quality = _clamp((signed_distance + 8.0) / 20.0)
        flow_quality = _clamp(
            0.5 + sign * (context.trade_flow_bias + context.book_imbalance) * 0.5
        )
        return _clamp(distance_quality * 0.60 + flow_quality * 0.40)

    def _money_flow_quality(self, context: BreakoutContext, direction: str) -> float:
        sign = 1.0 if direction == "long" else -1.0
        signed_distance = sign * context.vwap_distance_bps
        signed_pressure = sign * (
            context.trade_flow_bias * 0.50
            + context.book_imbalance * 0.20
        )
        chase_penalty = 0.0
        if (
            signed_distance > self._config.pod_b.bis_max_chase_distance_bps * 0.5
            and context.volume_ratio > 2.0
        ):
            chase_penalty = min(
                (
                    signed_distance
                    - self._config.pod_b.bis_max_chase_distance_bps * 0.5
                )
                / 20.0,
                0.40,
            )
        return _clamp(
            0.55
            + signed_pressure * 0.35
            + max(min(signed_distance / 30.0, 0.15), -0.15)
            + _clamp((context.volume_ratio - 1.0) / 2.0) * 0.10
            - chase_penalty
        )

    def _confidence_components(
        self,
        *,
        context: BreakoutContext,
        activity_score: float,
        breakout_score: float,
        setup_bonus: float,
        squeeze_release_quality: float | None = None,
        flow_support_quality: float | None = None,
        vwap_reclaim_quality: float | None = None,
        money_flow_quality: float | None = None,
    ) -> dict[str, float]:
        components = {
            "compression_quality": round(_clamp(context.compression_score), 4),
            "activity_quality": round(_clamp(activity_score), 4),
            "breakout_quality": round(_clamp(breakout_score), 4),
            "spread_quality": round(
                _clamp(1.0 - context.spread_bps / max(self._config.pod_b.bis_max_spread_bps, 1.0)),
                4,
            ),
            "alignment_quality": round(
                _clamp(abs(context.trade_flow_bias + context.book_imbalance)),
                4,
            ),
            "setup_bonus": round(setup_bonus, 4),
        }
        if squeeze_release_quality is not None:
            components["squeeze_release_quality"] = round(_clamp(squeeze_release_quality), 4)
        if flow_support_quality is not None:
            components["flow_support_quality"] = round(_clamp(flow_support_quality), 4)
        if vwap_reclaim_quality is not None:
            components["vwap_reclaim_quality"] = round(_clamp(vwap_reclaim_quality), 4)
        if money_flow_quality is not None:
            components["money_flow_quality"] = round(_clamp(money_flow_quality), 4)
        return components

    def _aggregate_confidence(self, components: dict[str, float]) -> float:
        return (
            components["compression_quality"] * 0.18
            + components["activity_quality"] * 0.20
            + components["breakout_quality"] * 0.25
            + components["spread_quality"] * 0.09
            + components["alignment_quality"] * 0.10
            + components.get("vol_expansion_quality", 0.0) * 0.03
            + components.get("squeeze_release_quality", 0.0) * 0.05
            + components.get("flow_support_quality", 0.0) * 0.04
            + components.get("vwap_reclaim_quality", 0.0) * 0.03
            + components.get("money_flow_quality", 0.0) * 0.03
            + components.get("setup_bonus", 0.0) * 0.05
        )

    def _direction_enabled(self, direction: str) -> bool:
        if direction == "long":
            return self._config.pod_b.bis_enable_longs
        if direction == "short":
            return self._config.pod_b.bis_enable_shorts
        return False

    def _setup_enabled(self, signal: BreakoutSignal) -> bool:
        if not self._enabled_setups:
            return True
        return signal.setup in self._enabled_setups

    def _vol_ratio(self, context: BreakoutContext) -> float:
        if context.realized_vol_long_bps <= 0:
            return 1.0
        return context.realized_vol_short_bps / max(context.realized_vol_long_bps, 1e-9)

    def _trend_quality_bps(self, context: BreakoutContext) -> float:
        return abs((context.ema_fast - context.ema_slow) / max(context.price, 1e-9) * 10_000.0)

    def _compression_stop_bps(self, context: BreakoutContext) -> float:
        floor = self._config.pod_b.bis_stop_floor_bps
        ceiling = self._config.pod_b.bis_stop_ceiling_bps
        base = max(context.bucket_range_bps * 1.2, floor)
        return round(min(base, ceiling), 4)

    def _expansion_stop_bps(self, context: BreakoutContext) -> float:
        floor = self._config.pod_b.bis_stop_floor_bps
        ceiling = self._config.pod_b.bis_stop_ceiling_bps
        vol_buffer = max(context.realized_vol_short_bps * 1.6, context.bucket_range_bps)
        return round(min(max(vol_buffer, floor), ceiling), 4)

    def _squeeze_release_stop_bps(self, context: BreakoutContext) -> float:
        floor = self._config.pod_b.bis_stop_floor_bps
        ceiling = self._config.pod_b.bis_stop_ceiling_bps
        base = max(
            context.bucket_range_bps * 1.35,
            context.realized_vol_short_bps * 1.8,
            floor,
        )
        return round(min(base, ceiling), 4)
