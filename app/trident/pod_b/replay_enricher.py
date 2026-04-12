from __future__ import annotations

from dataclasses import replace

from app.trident.types import SymbolMarketSnapshot


class ReplayFeatureEnricher:
    """Derives Pod B bis replay features from historical snapshots when fields are absent."""

    def __init__(self) -> None:
        self._state_by_symbol: dict[str, dict[str, float]] = {}

    def enrich_many(self, snapshots: list[SymbolMarketSnapshot]) -> list[SymbolMarketSnapshot]:
        return [self.enrich(snapshot) for snapshot in snapshots]

    def enrich(self, snapshot: SymbolMarketSnapshot) -> SymbolMarketSnapshot:
        state = self._state_by_symbol.setdefault(
            snapshot.symbol.upper(),
            {
                "last_price": snapshot.price,
                "recent_high": snapshot.price,
                "recent_low": snapshot.price,
                "prev_spread_bps": snapshot.spread_bps,
                "prev_book_imbalance": snapshot.book_imbalance,
                "prev_trade_flow_bias": snapshot.trade_flow_bias,
                "avg_bucket_volume": snapshot.bucket_volume,
                "avg_bucket_trade_count": float(snapshot.bucket_trade_count),
                "realized_vol_short_bps": snapshot.realized_vol_short_bps,
                "realized_vol_long_bps": snapshot.realized_vol_long_bps,
            },
        )
        last_price = state["last_price"]
        momentum_bps = (
            ((snapshot.price - last_price) / last_price) * 10_000.0
            if snapshot.price > 0 and last_price > 0
            else 0.0
        )
        recent_high = max(self._ema(state["recent_high"], snapshot.price, 0.1), snapshot.price)
        recent_low = min(self._ema(state["recent_low"], snapshot.price, 0.1), snapshot.price)
        delta_spread_bps = snapshot.spread_bps - state["prev_spread_bps"]
        delta_book_imbalance = snapshot.book_imbalance - state["prev_book_imbalance"]
        delta_trade_flow_bias = snapshot.trade_flow_bias - state["prev_trade_flow_bias"]
        volume_ratio = self._ratio_against_baseline(snapshot.bucket_volume, state["avg_bucket_volume"])
        trade_count_ratio = self._ratio_against_baseline(
            float(snapshot.bucket_trade_count),
            state["avg_bucket_trade_count"],
        )
        realized_vol_short_bps = self._ema(
            state["realized_vol_short_bps"],
            abs(momentum_bps),
            alpha=0.35,
        )
        realized_vol_long_bps = self._ema(
            state["realized_vol_long_bps"],
            abs(momentum_bps),
            alpha=0.08,
        )
        bucket_notional_usd = (
            snapshot.bucket_notional_usd
            if snapshot.bucket_notional_usd > 0
            else snapshot.bucket_volume * snapshot.price
        )
        range_width_bps = self._range_width_bps(recent_low, recent_high, snapshot.price)
        compression_score = self._compression_score(
            range_width_bps=range_width_bps,
            spread_bps=snapshot.spread_bps,
            realized_vol_short_bps=realized_vol_short_bps,
            realized_vol_long_bps=realized_vol_long_bps,
            structure_score=snapshot.structure_score,
        )

        state["last_price"] = snapshot.price
        state["recent_high"] = recent_high
        state["recent_low"] = recent_low
        state["prev_spread_bps"] = snapshot.spread_bps
        state["prev_book_imbalance"] = snapshot.book_imbalance
        state["prev_trade_flow_bias"] = snapshot.trade_flow_bias
        state["avg_bucket_volume"] = self._update_baseline(
            state["avg_bucket_volume"],
            snapshot.bucket_volume,
            alpha=0.20,
        )
        state["avg_bucket_trade_count"] = self._update_baseline(
            state["avg_bucket_trade_count"],
            float(snapshot.bucket_trade_count),
            alpha=0.20,
        )
        state["realized_vol_short_bps"] = realized_vol_short_bps
        state["realized_vol_long_bps"] = realized_vol_long_bps

        return replace(
            snapshot,
            bucket_notional_usd=round(
                snapshot.bucket_notional_usd if snapshot.bucket_notional_usd > 0 else bucket_notional_usd,
                4,
            ),
            delta_spread_bps=round(
                snapshot.delta_spread_bps if snapshot.delta_spread_bps != 0.0 else delta_spread_bps,
                4,
            ),
            delta_book_imbalance=round(
                snapshot.delta_book_imbalance if snapshot.delta_book_imbalance != 0.0 else delta_book_imbalance,
                4,
            ),
            delta_trade_flow_bias=round(
                snapshot.delta_trade_flow_bias if snapshot.delta_trade_flow_bias != 0.0 else delta_trade_flow_bias,
                4,
            ),
            volume_ratio=round(
                snapshot.volume_ratio if snapshot.volume_ratio != 1.0 else volume_ratio,
                4,
            ),
            trade_count_ratio=round(
                snapshot.trade_count_ratio if snapshot.trade_count_ratio != 1.0 else trade_count_ratio,
                4,
            ),
            realized_vol_short_bps=round(
                snapshot.realized_vol_short_bps if snapshot.realized_vol_short_bps > 0 else realized_vol_short_bps,
                4,
            ),
            realized_vol_long_bps=round(
                snapshot.realized_vol_long_bps if snapshot.realized_vol_long_bps > 0 else realized_vol_long_bps,
                4,
            ),
            compression_score=round(
                snapshot.compression_score if snapshot.compression_score > 0 else compression_score,
                4,
            ),
        )

    def _ema(self, prev: float, value: float, alpha: float) -> float:
        return prev * (1 - alpha) + value * alpha

    def _update_baseline(self, baseline: float, current: float, alpha: float) -> float:
        if baseline <= 0:
            return current
        return self._ema(baseline, current, alpha)

    def _ratio_against_baseline(self, current: float, baseline: float) -> float:
        if baseline > 0:
            return current / baseline
        return 2.0 if current > 0 else 1.0

    def _range_width_bps(self, recent_low: float, recent_high: float, price: float) -> float:
        if price <= 0 or recent_low <= 0 or recent_high <= 0:
            return 0.0
        return (recent_high - recent_low) / price * 10_000.0

    def _compression_score(
        self,
        *,
        range_width_bps: float,
        spread_bps: float,
        realized_vol_short_bps: float,
        realized_vol_long_bps: float,
        structure_score: float,
    ) -> float:
        range_component = 1.0 - self._clamp(range_width_bps / 35.0, 0.0, 1.0)
        spread_component = 1.0 - self._clamp(spread_bps / 8.0, 0.0, 1.0)
        vol_component = 1.0 - self._clamp(realized_vol_short_bps / 12.0, 0.0, 1.0)
        relative_vol_component = 1.0
        if realized_vol_long_bps > 0:
            relative_vol_component = 1.0 - self._clamp(
                realized_vol_short_bps / max(realized_vol_long_bps * 1.5, 1e-9),
                0.0,
                1.0,
            )
        structure_component = 1.0 - self._clamp(abs(structure_score), 0.0, 1.0)
        return (
            range_component * 0.30
            + spread_component * 0.20
            + vol_component * 0.20
            + relative_vol_component * 0.15
            + structure_component * 0.15
        )

    def _clamp(self, value: float, lower: float, upper: float) -> float:
        return max(lower, min(value, upper))
