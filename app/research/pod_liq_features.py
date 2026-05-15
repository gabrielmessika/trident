from __future__ import annotations

import argparse
import json
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.backtest.snapshot_loader import SnapshotLoader, SnapshotRecord
from app.settings import load_config
from app.trident.regime_allocator import RegimeAllocator
from app.trident.types import Regime, RegimeSnapshot, SymbolMarketSnapshot, symbol_market_snapshot_from_mapping


@dataclass(slots=True)
class PodLiqFeatureRow:
    timestamp: str | None
    source_file: str
    symbol: str
    regime: str
    price: float
    spread_bps: float
    microprice_dislocation_bps: float
    book_imbalance: float
    trade_flow_bias: float
    bucket_range_bps: float
    bucket_trade_count: int
    bucket_notional_usd: float
    structure_score: float
    price_move_bps: float
    delta_spread_bps: float
    delta_book_imbalance: float
    delta_trade_flow_bias: float
    volume_ratio: float
    trade_count_ratio: float
    realized_vol_short_bps: float
    realized_vol_long_bps: float
    compression_score: float
    bid_depth_velocity: float
    ask_depth_velocity: float
    net_depth_velocity: float
    best_bid_size_velocity: float
    best_ask_size_velocity: float
    touch_net_velocity: float
    flow_direction: str
    micro_direction: str
    impulse_direction: str
    direction: str
    event_score: float
    liquidity_pull_score: float
    liquidity_pull_direction: str
    touch_liquidity_pull_score: float
    touch_liquidity_pull_direction: str
    depth_refill_score: float
    depth_refill_direction: str
    touch_refill_score: float
    touch_refill_direction: str
    absorption_score: float
    absorption_direction: str
    exhaustion_score: float
    exhaustion_direction: str
    book_churn_score: float
    future_return_bps: float | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class PodLiqFeatureBuilder:
    """Builds observables-first event features from comparable TRIDENT snapshots."""

    def __init__(self) -> None:
        self.loader = SnapshotLoader()

    def build_rows(
        self,
        *,
        input_path: str | Path,
        config_path: str | Path = "config/trident.toml",
        symbols: list[str] | None = None,
        horizon_bars: int = 1,
        max_bar_gap_seconds: int = 180,
    ) -> list[PodLiqFeatureRow]:
        horizon = max(int(horizon_bars), 1)
        config = load_config(config_path)
        regime_allocator = RegimeAllocator(config)
        current_regime = Regime.CASH
        pending_regime: Regime | None = None
        pending_count = 0
        requested = None if symbols is None else {str(symbol).upper() for symbol in symbols}
        rows: list[PodLiqFeatureRow] = []

        records_iter = iter(self.loader.iter_merged_jsonl(input_path))
        window_records: deque[SnapshotRecord] = deque()
        window_maps: deque[dict[str, SymbolMarketSnapshot]] = deque()

        for _ in range(horizon + 2):
            record = next(records_iter, None)
            if record is None:
                break
            window_records.append(record)
            window_maps.append(self._snapshot_map(record))

        if len(window_records) < 2:
            return rows

        while len(window_records) >= 2:
            previous_record = window_records[0]
            current_record = window_records[1]
            previous_snapshots = window_maps[0]
            current_snapshots = window_maps[1]
            future_snapshots = (
                window_maps[1 + horizon]
                    if len(window_maps) > 1 + horizon
                    else None
                )
            if not self._timestamps_within_gap(
                previous_record.timestamp,
                current_record.timestamp,
                bars=1,
                max_bar_gap_seconds=max_bar_gap_seconds,
            ):
                next_record = next(records_iter, None)
                window_records.popleft()
                window_maps.popleft()
                if next_record is not None:
                    window_records.append(next_record)
                    window_maps.append(self._snapshot_map(next_record))
                continue

            decision = regime_allocator.resolve(
                snapshot=RegimeSnapshot(**current_record.regime_snapshot),
                current_regime=current_regime,
                pending_regime=pending_regime,
                pending_count=pending_count,
            )
            current_regime = decision.effective_regime
            pending_regime = decision.pending_regime
            pending_count = decision.pending_count
            current_regime_name = current_regime.value

            for symbol, current in current_snapshots.items():
                if requested is not None and symbol not in requested:
                    continue
                previous = previous_snapshots.get(symbol)
                if previous is None:
                    continue

                future_return = None
                if future_snapshots is not None:
                    future = future_snapshots.get(symbol)
                    if (
                        future is not None
                        and current.price > 0
                        and len(window_records) > 1 + horizon
                        and self._timestamps_within_gap(
                            current_record.timestamp,
                            window_records[1 + horizon].timestamp,
                            bars=horizon,
                            max_bar_gap_seconds=max_bar_gap_seconds,
                        )
                    ):
                        future_return = round(
                            (future.price - current.price) / current.price * 10_000.0,
                            4,
                        )

                delta_spread = round(current.spread_bps - previous.spread_bps, 4)
                delta_book = round(current.book_imbalance - previous.book_imbalance, 4)
                delta_flow = round(current.trade_flow_bias - previous.trade_flow_bias, 4)
                price_move_bps = self._price_move_bps(previous.price, current.price)
                volume_ratio = self._ratio(
                    current.bucket_volume,
                    previous.bucket_volume,
                )
                trade_count_ratio = self._ratio(
                    float(current.bucket_trade_count),
                    float(previous.bucket_trade_count),
                )
                bid_depth_velocity = self._ratio_change(
                    current.bid_depth_10bps,
                    previous.bid_depth_10bps,
                )
                ask_depth_velocity = self._ratio_change(
                    current.ask_depth_10bps,
                    previous.ask_depth_10bps,
                )
                net_depth_velocity = round(
                    bid_depth_velocity - ask_depth_velocity,
                    4,
                )
                best_bid_size_velocity = self._ratio_change(
                    current.best_bid_size,
                    previous.best_bid_size,
                )
                best_ask_size_velocity = self._ratio_change(
                    current.best_ask_size,
                    previous.best_ask_size,
                )
                touch_net_velocity = round(
                    best_bid_size_velocity - best_ask_size_velocity,
                    4,
                )
                flow_direction = self._flow_direction(current)
                micro_direction = self._micro_direction(
                    current=current,
                    price_move_bps=price_move_bps,
                    fallback=flow_direction,
                )
                impulse_direction = self._impulse_direction(
                    current=current,
                    price_move_bps=price_move_bps,
                    fallback=flow_direction,
                )

                signed_intensity = (
                    current.trade_flow_bias * 0.40
                    + current.book_imbalance * 0.20
                    + delta_flow * 0.25
                    + delta_book * 0.15
                )
                direction = "long" if signed_intensity >= 0 else "short"
                event_score = min(
                    1.0,
                    abs(delta_flow) * 0.35
                    + abs(delta_book) * 0.20
                    + self._clamp(max(delta_spread, 0.0) / 2.0) * 0.15
                    + self._clamp(volume_ratio / 3.0) * 0.15
                    + self._clamp(trade_count_ratio / 3.0) * 0.15,
                )

                liquidity_pull_score, liquidity_pull_direction = self._liquidity_pull_signal(
                    current=current,
                    delta_spread_bps=delta_spread,
                    delta_book_imbalance=delta_book,
                    delta_trade_flow_bias=delta_flow,
                    bid_depth_velocity=bid_depth_velocity,
                    ask_depth_velocity=ask_depth_velocity,
                )
                (
                    touch_liquidity_pull_score,
                    touch_liquidity_pull_direction,
                ) = self._touch_liquidity_pull_signal(
                    current=current,
                    delta_spread_bps=delta_spread,
                    delta_book_imbalance=delta_book,
                    delta_trade_flow_bias=delta_flow,
                    best_bid_size_velocity=best_bid_size_velocity,
                    best_ask_size_velocity=best_ask_size_velocity,
                )
                depth_refill_score, depth_refill_direction = self._depth_refill_signal(
                    current=current,
                    delta_spread_bps=delta_spread,
                    bid_depth_velocity=bid_depth_velocity,
                    ask_depth_velocity=ask_depth_velocity,
                )
                touch_refill_score, touch_refill_direction = self._touch_refill_signal(
                    current=current,
                    delta_spread_bps=delta_spread,
                    best_bid_size_velocity=best_bid_size_velocity,
                    best_ask_size_velocity=best_ask_size_velocity,
                )
                absorption_score, absorption_direction = self._absorption_signal(
                    current=current,
                    price_move_bps=price_move_bps,
                    volume_ratio=volume_ratio,
                    trade_count_ratio=trade_count_ratio,
                )
                exhaustion_score, exhaustion_direction = self._exhaustion_signal(
                    current=current,
                    price_move_bps=price_move_bps,
                    delta_book_imbalance=delta_book,
                    delta_trade_flow_bias=delta_flow,
                    volume_ratio=volume_ratio,
                    trade_count_ratio=trade_count_ratio,
                )
                book_churn_score = self._book_churn_signal(
                    current=current,
                    delta_spread_bps=delta_spread,
                    volume_ratio=volume_ratio,
                    trade_count_ratio=trade_count_ratio,
                    bid_depth_velocity=bid_depth_velocity,
                    ask_depth_velocity=ask_depth_velocity,
                    best_bid_size_velocity=best_bid_size_velocity,
                    best_ask_size_velocity=best_ask_size_velocity,
                )
                rows.append(
                    PodLiqFeatureRow(
                        timestamp=current_record.timestamp,
                        source_file=current_record.source_file,
                        symbol=symbol,
                        regime=current_regime_name,
                        price=round(current.price, 8),
                        spread_bps=round(current.spread_bps, 4),
                        microprice_dislocation_bps=round(current.microprice_dislocation_bps, 4),
                        book_imbalance=round(current.book_imbalance, 4),
                        trade_flow_bias=round(current.trade_flow_bias, 4),
                        bucket_range_bps=round(current.bucket_range_bps, 4),
                        bucket_trade_count=int(current.bucket_trade_count),
                        bucket_notional_usd=round(
                            current.bucket_notional_usd
                            if current.bucket_notional_usd > 0
                            else current.bucket_volume * current.price,
                            4,
                        ),
                        structure_score=round(current.structure_score, 4),
                        price_move_bps=price_move_bps,
                        delta_spread_bps=delta_spread,
                        delta_book_imbalance=delta_book,
                        delta_trade_flow_bias=delta_flow,
                        volume_ratio=volume_ratio,
                        trade_count_ratio=trade_count_ratio,
                        realized_vol_short_bps=round(current.realized_vol_short_bps, 4),
                        realized_vol_long_bps=round(current.realized_vol_long_bps, 4),
                        compression_score=round(current.compression_score, 4),
                        bid_depth_velocity=bid_depth_velocity,
                        ask_depth_velocity=ask_depth_velocity,
                        net_depth_velocity=net_depth_velocity,
                        best_bid_size_velocity=best_bid_size_velocity,
                        best_ask_size_velocity=best_ask_size_velocity,
                        touch_net_velocity=touch_net_velocity,
                        flow_direction=flow_direction,
                        micro_direction=micro_direction,
                        impulse_direction=impulse_direction,
                        direction=direction,
                        event_score=round(event_score, 4),
                        liquidity_pull_score=liquidity_pull_score,
                        liquidity_pull_direction=liquidity_pull_direction,
                        touch_liquidity_pull_score=touch_liquidity_pull_score,
                        touch_liquidity_pull_direction=touch_liquidity_pull_direction,
                        depth_refill_score=depth_refill_score,
                        depth_refill_direction=depth_refill_direction,
                        touch_refill_score=touch_refill_score,
                        touch_refill_direction=touch_refill_direction,
                        absorption_score=absorption_score,
                        absorption_direction=absorption_direction,
                        exhaustion_score=exhaustion_score,
                        exhaustion_direction=exhaustion_direction,
                        book_churn_score=book_churn_score,
                        future_return_bps=future_return,
                    )
                )

            next_record = next(records_iter, None)
            window_records.popleft()
            window_maps.popleft()
            if next_record is not None:
                window_records.append(next_record)
                window_maps.append(self._snapshot_map(next_record))

        return rows

    def build(
        self,
        *,
        input_path: str | Path,
        config_path: str | Path = "config/trident.toml",
        symbols: list[str] | None = None,
        horizon_bars: int = 1,
        output_path: str | Path | None = None,
    ) -> dict[str, object]:
        rows = self.build_rows(
            input_path=input_path,
            config_path=config_path,
            symbols=symbols,
            horizon_bars=horizon_bars,
        )
        if output_path is not None:
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row.to_dict()) + "\n")
        return {
            "input_path": str(input_path),
            "row_count": len(rows),
            "output_path": str(output_path) if output_path is not None else None,
        }

    def _snapshot_map(self, record: SnapshotRecord) -> dict[str, SymbolMarketSnapshot]:
        return {
            item["symbol"].upper(): symbol_market_snapshot_from_mapping(item)
            for item in record.symbols
            if isinstance(item, dict)
        }

    def _price_move_bps(self, previous_price: float, current_price: float) -> float:
        if previous_price <= 0 or current_price <= 0:
            return 0.0
        return round((current_price - previous_price) / previous_price * 10_000.0, 4)

    def _ratio(self, current: float, previous: float) -> float:
        if previous > 0:
            return round(current / previous, 4)
        if current > 0:
            return 2.0
        return 1.0

    def _ratio_change(self, current: float, previous: float) -> float:
        if previous > 0:
            return round((current - previous) / previous, 4)
        if current > 0:
            return 1.0
        return 0.0

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
        return self._clamp(signed / scale)

    def _flow_direction(self, current: SymbolMarketSnapshot) -> str:
        if abs(current.trade_flow_bias) >= 0.02:
            return "long" if current.trade_flow_bias > 0 else "short"
        if abs(current.book_imbalance) >= 0.02:
            return "long" if current.book_imbalance > 0 else "short"
        if abs(current.microprice_dislocation_bps) >= 0.05:
            return "long" if current.microprice_dislocation_bps > 0 else "short"
        return "long"

    def _micro_direction(
        self,
        *,
        current: SymbolMarketSnapshot,
        price_move_bps: float,
        fallback: str,
    ) -> str:
        if abs(current.microprice_dislocation_bps) >= 0.05:
            return "long" if current.microprice_dislocation_bps > 0 else "short"
        if abs(price_move_bps) >= 0.05:
            return "long" if price_move_bps > 0 else "short"
        return fallback

    def _impulse_direction(
        self,
        *,
        current: SymbolMarketSnapshot,
        price_move_bps: float,
        fallback: str,
    ) -> str:
        if abs(price_move_bps) >= 0.6:
            return "long" if price_move_bps > 0 else "short"
        if abs(current.trade_flow_bias) >= 0.08:
            return "long" if current.trade_flow_bias > 0 else "short"
        return self._micro_direction(
            current=current,
            price_move_bps=price_move_bps,
            fallback=fallback,
        )

    def _liquidity_pull_signal(
        self,
        *,
        current: SymbolMarketSnapshot,
        delta_spread_bps: float,
        delta_book_imbalance: float,
        delta_trade_flow_bias: float,
        bid_depth_velocity: float,
        ask_depth_velocity: float,
    ) -> tuple[float, str]:
        bullish_pull = max(-ask_depth_velocity, 0.0) * 0.75 + max(bid_depth_velocity, 0.0) * 0.25
        bearish_pull = max(-bid_depth_velocity, 0.0) * 0.75 + max(ask_depth_velocity, 0.0) * 0.25
        direction = "long" if bullish_pull >= bearish_pull else "short"
        dominant_pull = bullish_pull if direction == "long" else bearish_pull
        flow_support = self._positive_for_direction(
            current.trade_flow_bias * 0.55
            + delta_trade_flow_bias * 0.20
            + current.book_imbalance * 0.15
            + delta_book_imbalance * 0.10,
            direction,
            scale=0.45,
        )
        micro_support = self._positive_for_direction(
            current.microprice_dislocation_bps,
            direction,
            scale=1.5,
        )
        spread_widening = self._clamp(max(delta_spread_bps, 0.0) / 2.0)
        score = (
            self._clamp(dominant_pull / 1.25) * 0.45
            + flow_support * 0.25
            + micro_support * 0.15
            + spread_widening * 0.15
        )
        return round(score, 4), direction

    def _touch_liquidity_pull_signal(
        self,
        *,
        current: SymbolMarketSnapshot,
        delta_spread_bps: float,
        delta_book_imbalance: float,
        delta_trade_flow_bias: float,
        best_bid_size_velocity: float,
        best_ask_size_velocity: float,
    ) -> tuple[float, str]:
        bullish_pull = max(-best_ask_size_velocity, 0.0) * 0.75 + max(best_bid_size_velocity, 0.0) * 0.25
        bearish_pull = max(-best_bid_size_velocity, 0.0) * 0.75 + max(best_ask_size_velocity, 0.0) * 0.25
        direction = "long" if bullish_pull >= bearish_pull else "short"
        dominant_pull = bullish_pull if direction == "long" else bearish_pull
        flow_support = self._positive_for_direction(
            current.trade_flow_bias * 0.50
            + delta_trade_flow_bias * 0.20
            + current.book_imbalance * 0.20
            + delta_book_imbalance * 0.10,
            direction,
            scale=0.45,
        )
        micro_support = self._positive_for_direction(
            current.microprice_dislocation_bps,
            direction,
            scale=1.25,
        )
        spread_widening = self._clamp(max(delta_spread_bps, 0.0) / 1.5)
        score = (
            self._clamp(dominant_pull / 1.10) * 0.45
            + flow_support * 0.25
            + micro_support * 0.15
            + spread_widening * 0.15
        )
        return round(score, 4), direction

    def _depth_refill_signal(
        self,
        *,
        current: SymbolMarketSnapshot,
        delta_spread_bps: float,
        bid_depth_velocity: float,
        ask_depth_velocity: float,
    ) -> tuple[float, str]:
        bullish_refill = max(bid_depth_velocity, 0.0) * 0.75 + max(-ask_depth_velocity, 0.0) * 0.15
        bearish_refill = max(ask_depth_velocity, 0.0) * 0.75 + max(-bid_depth_velocity, 0.0) * 0.15
        direction = "long" if bullish_refill >= bearish_refill else "short"
        dominant_refill = bullish_refill if direction == "long" else bearish_refill
        flow_support = self._positive_for_direction(
            current.book_imbalance * 0.55 + current.trade_flow_bias * 0.45,
            direction,
            scale=0.45,
        )
        micro_support = self._positive_for_direction(
            current.microprice_dislocation_bps,
            direction,
            scale=1.25,
        )
        spread_support = 1.0 - self._clamp(current.spread_bps / 8.0)
        spread_normalization = self._clamp(max(-delta_spread_bps, 0.0) / 2.0)
        score = (
            self._clamp(dominant_refill / 1.25) * 0.35
            + flow_support * 0.25
            + micro_support * 0.20
            + spread_support * 0.10
            + spread_normalization * 0.10
        )
        return round(score, 4), direction

    def _touch_refill_signal(
        self,
        *,
        current: SymbolMarketSnapshot,
        delta_spread_bps: float,
        best_bid_size_velocity: float,
        best_ask_size_velocity: float,
    ) -> tuple[float, str]:
        bullish_refill = max(best_bid_size_velocity, 0.0) * 0.75 + max(-best_ask_size_velocity, 0.0) * 0.15
        bearish_refill = max(best_ask_size_velocity, 0.0) * 0.75 + max(-best_bid_size_velocity, 0.0) * 0.15
        direction = "long" if bullish_refill >= bearish_refill else "short"
        dominant_refill = bullish_refill if direction == "long" else bearish_refill
        flow_support = self._positive_for_direction(
            current.book_imbalance * 0.55 + current.trade_flow_bias * 0.45,
            direction,
            scale=0.45,
        )
        micro_support = self._positive_for_direction(
            current.microprice_dislocation_bps,
            direction,
            scale=1.10,
        )
        spread_support = 1.0 - self._clamp(current.spread_bps / 6.0)
        spread_normalization = self._clamp(max(-delta_spread_bps, 0.0) / 1.5)
        score = (
            self._clamp(dominant_refill / 1.10) * 0.35
            + flow_support * 0.25
            + micro_support * 0.20
            + spread_support * 0.10
            + spread_normalization * 0.10
        )
        return round(score, 4), direction

    def _absorption_signal(
        self,
        *,
        current: SymbolMarketSnapshot,
        price_move_bps: float,
        volume_ratio: float,
        trade_count_ratio: float,
    ) -> tuple[float, str]:
        if current.trade_flow_bias > 0:
            direction = "short"
        elif current.trade_flow_bias < 0:
            direction = "long"
        else:
            direction = "short" if current.microprice_dislocation_bps >= 0 else "long"
        activity = self._clamp(max(volume_ratio, trade_count_ratio) / 3.0)
        flow_pressure = (
            self._clamp(abs(current.trade_flow_bias) / 0.45) * 0.65
            + self._clamp(abs(current.book_imbalance) / 0.45) * 0.35
        )
        small_move = 1.0 - self._clamp(
            abs(price_move_bps) / max(current.bucket_range_bps * 0.35, 3.0)
        )
        micro_support = self._positive_for_direction(
            current.microprice_dislocation_bps,
            direction,
            scale=1.25,
        )
        score = (
            activity * 0.30
            + flow_pressure * 0.25
            + small_move * 0.25
            + micro_support * 0.20
        )
        return round(score, 4), direction

    def _exhaustion_signal(
        self,
        *,
        current: SymbolMarketSnapshot,
        price_move_bps: float,
        delta_book_imbalance: float,
        delta_trade_flow_bias: float,
        volume_ratio: float,
        trade_count_ratio: float,
    ) -> tuple[float, str]:
        if abs(price_move_bps) >= 0.6:
            impulse_direction = "long" if price_move_bps > 0 else "short"
        elif abs(current.trade_flow_bias) >= 0.08:
            impulse_direction = "long" if current.trade_flow_bias > 0 else "short"
        else:
            impulse_direction = "long" if current.microprice_dislocation_bps >= 0 else "short"
        direction = "short" if impulse_direction == "long" else "long"
        activity = self._clamp(max(volume_ratio, trade_count_ratio) / 3.0)
        stretch = self._clamp(
            abs(price_move_bps) / max(current.bucket_range_bps * 0.45, 3.0)
        )
        flow_deceleration = self._positive_for_direction(
            -delta_trade_flow_bias,
            impulse_direction,
            scale=0.18,
        )
        book_deceleration = self._positive_for_direction(
            -delta_book_imbalance,
            impulse_direction,
            scale=0.18,
        )
        micro_support = self._positive_for_direction(
            current.microprice_dislocation_bps,
            direction,
            scale=1.25,
        )
        score = (
            activity * 0.25
            + stretch * 0.20
            + flow_deceleration * 0.25
            + book_deceleration * 0.15
            + micro_support * 0.15
        )
        return round(score, 4), direction

    def _book_churn_signal(
        self,
        *,
        current: SymbolMarketSnapshot,
        delta_spread_bps: float,
        volume_ratio: float,
        trade_count_ratio: float,
        bid_depth_velocity: float,
        ask_depth_velocity: float,
        best_bid_size_velocity: float,
        best_ask_size_velocity: float,
    ) -> float:
        broad_instability = self._clamp(
            (
                abs(bid_depth_velocity)
                + abs(ask_depth_velocity)
                + abs(best_bid_size_velocity)
                + abs(best_ask_size_velocity)
            )
            / 3.0
        )
        two_sided_touch = self._clamp(
            min(abs(best_bid_size_velocity), abs(best_ask_size_velocity)) / 0.35
        )
        two_sided_depth = self._clamp(
            min(abs(bid_depth_velocity), abs(ask_depth_velocity)) / 0.35
        )
        spread_instability = self._clamp(abs(delta_spread_bps) / 2.0)
        micro_noise = self._clamp(abs(current.microprice_dislocation_bps) / 1.25)
        activity = self._clamp(max(volume_ratio, trade_count_ratio) / 3.0)
        score = (
            broad_instability * 0.25
            + two_sided_touch * 0.20
            + two_sided_depth * 0.20
            + spread_instability * 0.15
            + micro_noise * 0.10
            + activity * 0.10
        )
        return round(score, 4)

    def _clamp(self, value: float, lower: float = 0.0, upper: float = 1.0) -> float:
        return max(lower, min(value, upper))

    def _timestamps_within_gap(
        self,
        previous_timestamp: str | None,
        current_timestamp: str | None,
        *,
        bars: int,
        max_bar_gap_seconds: int,
    ) -> bool:
        previous_dt = self._parse_timestamp(previous_timestamp)
        current_dt = self._parse_timestamp(current_timestamp)
        if previous_dt is None or current_dt is None:
            return True
        allowed_gap = max(int(bars), 1) * max(max_bar_gap_seconds, 1)
        return (current_dt - previous_dt).total_seconds() <= allowed_gap

    def _parse_timestamp(self, value: str | None) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            if value.endswith("Z"):
                return datetime.fromisoformat(value[:-1] + "+00:00")
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build observables-first liq/OI features from snapshots")
    parser.add_argument("--input", required=True)
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--symbols", help="Optional comma-separated list")
    parser.add_argument("--horizon-bars", type=int, default=1)
    parser.add_argument("--output")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    symbols = [item.strip().upper() for item in (args.symbols or "").split(",") if item.strip()]
    result = PodLiqFeatureBuilder().build(
        input_path=args.input,
        config_path=args.config,
        symbols=symbols or None,
        horizon_bars=args.horizon_bars,
        output_path=args.output,
    )
    print(f"row_count={result['row_count']}")
    if result["output_path"] is not None:
        print(f"output_path={result['output_path']}")


if __name__ == "__main__":
    main()
