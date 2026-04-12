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
        candidates = [signal for signal in (compression, expansion) if signal is not None]
        if not candidates:
            return None
        best = max(candidates, key=lambda item: item.confidence)
        if not self._setup_enabled(best):
            return None
        if best.confidence < self._config.pod_b.bis_min_confidence:
            return None
        return best

    def _passes_filters(self, context: BreakoutContext) -> bool:
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
        )
        return BreakoutSignal(
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
                "volume_ratio": round(context.volume_ratio, 4),
                "trade_count_ratio": round(context.trade_count_ratio, 4),
                "regime": context.regime,
            },
            confidence_components=components,
        )

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
        if vol_ratio < 1.15 and abs(context.delta_trade_flow_bias) < 0.22:
            return None
        stop_bps = self._expansion_stop_bps(context)
        components = self._confidence_components(
            context=context,
            activity_score=activity_score,
            breakout_score=breakout_score,
            setup_bonus=0.05,
        )
        components["vol_expansion_quality"] = round(_clamp(vol_ratio / 2.0), 4)
        return BreakoutSignal(
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
                "regime": context.regime,
            },
            confidence_components=components,
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

    def _confidence_components(
        self,
        *,
        context: BreakoutContext,
        activity_score: float,
        breakout_score: float,
        setup_bonus: float,
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
        return components

    def _aggregate_confidence(self, components: dict[str, float]) -> float:
        return (
            components["compression_quality"] * 0.20
            + components["activity_quality"] * 0.22
            + components["breakout_quality"] * 0.28
            + components["spread_quality"] * 0.10
            + components["alignment_quality"] * 0.12
            + components.get("vol_expansion_quality", 0.0) * 0.03
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
