from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


OPEN_STATUSES = {"open", "placed", "resting"}
REMOVE_STATUSES = {
    "canceled",
    "filled",
    "triggered",
    "rejected",
    "margincanceled",
    "vaultwithdrawalcanceled",
    "openinterestcapcanceled",
    "selftradecanceled",
    "reduceonlycanceled",
    "siblingfilledcanceled",
    "delistedcanceled",
    "liquidatedcanceled",
    "scheduledcancel",
}


@dataclass(frozen=True, slots=True)
class TriggerLiquidityOrder:
    symbol: str
    oid: str
    user: str = ""
    side: str = ""
    trigger_px: float = 0.0
    limit_px: float = 0.0
    sz: float = 0.0
    orig_sz: float = 0.0
    order_type: str = ""
    trigger_condition: str = ""
    is_position_tpsl: bool = False
    reduce_only: bool = False
    observed_at_ms: int | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.user.lower(), self.oid)

    @property
    def active_size(self) -> float:
        if self.sz > 0:
            return self.sz
        return max(self.orig_sz, 0.0)

    @property
    def trigger_kind(self) -> str:
        text = f"{self.order_type} {self.trigger_condition}".lower()
        if "take" in text or "profit" in text or "tp" in text:
            return "tp"
        if "stop" in text or "sl" in text:
            return "stop"
        return "trigger"

    @property
    def trigger_side(self) -> str:
        side = self.side.strip().lower()
        if side in {"b", "bid", "buy", "long"}:
            return "buy"
        if side in {"a", "ask", "sell", "short"}:
            return "sell"
        return "unknown"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TriggerLiquidityFeatures:
    symbol: str
    trigger_liquidity_available: bool = False
    nearest_stop_cluster_bps: float = 0.0
    nearest_stop_cluster_above_bps: float = 0.0
    nearest_stop_cluster_below_bps: float = 0.0
    nearest_tp_cluster_bps: float = 0.0
    nearest_tp_cluster_above_bps: float = 0.0
    nearest_tp_cluster_below_bps: float = 0.0
    stop_pressure_above: float = 0.0
    stop_pressure_below: float = 0.0
    tp_pressure_above: float = 0.0
    tp_pressure_below: float = 0.0
    trigger_asymmetry: float = 0.0
    cascade_risk_up: float = 0.0
    cascade_risk_down: float = 0.0
    trigger_data_age_seconds: float | None = None
    total_trigger_notional_usd: float = 0.0
    max_trigger_cluster_notional_usd: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class TriggerLiquidityBook:
    """Maintains active public trigger orders and derives compact per-symbol features."""

    def __init__(self) -> None:
        self.orders: dict[tuple[str, str], TriggerLiquidityOrder] = {}
        self.latest_observed_at_ms: int | None = None

    def apply_order_status(self, payload: dict[str, object]) -> TriggerLiquidityOrder | None:
        order_payload = payload.get("order")
        if not isinstance(order_payload, dict):
            return None
        status = str(payload.get("status", "open")).strip().lower()
        observed_at_ms = parse_event_time_ms(payload.get("time")) or _int_or_none(
            order_payload.get("timestamp")
        )
        order = order_from_payload(
            order_payload,
            user=str(payload.get("user", "")),
            observed_at_ms=observed_at_ms,
        )
        if order is None:
            return None
        if status in OPEN_STATUSES:
            self.upsert(order)
            return order
        if status in REMOVE_STATUSES:
            self.remove(order)
            return order
        return order

    def upsert(self, order: TriggerLiquidityOrder) -> None:
        if order.trigger_px <= 0 or order.active_size <= 0:
            return
        self.orders[order.key] = order
        if order.observed_at_ms is not None:
            self.latest_observed_at_ms = max(
                self.latest_observed_at_ms or order.observed_at_ms,
                order.observed_at_ms,
            )

    def remove(self, order: TriggerLiquidityOrder) -> None:
        self.orders.pop(order.key, None)
        if order.observed_at_ms is not None:
            self.latest_observed_at_ms = max(
                self.latest_observed_at_ms or order.observed_at_ms,
                order.observed_at_ms,
            )

    def apply_orders(self, orders: Iterable[TriggerLiquidityOrder]) -> None:
        for order in orders:
            self.upsert(order)

    def features_for_symbol(
        self,
        *,
        symbol: str,
        reference_price: float,
        bucket_bps: float = 5.0,
        lookahead_bps: float = 100.0,
        min_cluster_notional_usd: float = 50_000.0,
        now_ms: int | None = None,
    ) -> TriggerLiquidityFeatures:
        symbol_key = symbol.strip().upper()
        if reference_price <= 0:
            return TriggerLiquidityFeatures(symbol=symbol_key)

        bucket_size = max(float(bucket_bps), 0.0001)
        max_distance = max(float(lookahead_bps), 0.0)
        min_notional = max(float(min_cluster_notional_usd), 1.0)
        clusters: dict[tuple[str, str, str, int], float] = {}
        total_notional = 0.0
        latest_seen = self.latest_observed_at_ms

        for order in self.orders.values():
            if order.symbol.upper() != symbol_key:
                continue
            distance_bps = (order.trigger_px - reference_price) / reference_price * 10_000.0
            if abs(distance_bps) > max_distance:
                continue
            region = "above" if distance_bps >= 0 else "below"
            bucket = int(abs(distance_bps) // bucket_size)
            notional = order.active_size * max(order.trigger_px, reference_price)
            total_notional += notional
            clusters[
                (
                    order.trigger_kind,
                    order.trigger_side,
                    region,
                    bucket,
                )
            ] = clusters.get((order.trigger_kind, order.trigger_side, region, bucket), 0.0) + notional
            if order.observed_at_ms is not None:
                latest_seen = max(latest_seen or order.observed_at_ms, order.observed_at_ms)

        if not clusters:
            return TriggerLiquidityFeatures(
                symbol=symbol_key,
                trigger_data_age_seconds=self._age_seconds(latest_seen, now_ms),
            )

        stop_above_usd = self._sum_clusters(clusters, kind="stop", region="above")
        stop_below_usd = self._sum_clusters(clusters, kind="stop", region="below")
        tp_above_usd = self._sum_clusters(clusters, kind="tp", region="above")
        tp_below_usd = self._sum_clusters(clusters, kind="tp", region="below")
        buy_stop_above_usd = self._sum_clusters(
            clusters,
            kind="stop",
            region="above",
            side="buy",
        )
        sell_stop_below_usd = self._sum_clusters(
            clusters,
            kind="stop",
            region="below",
            side="sell",
        )
        stop_up = self._pressure(buy_stop_above_usd, min_notional)
        stop_down = self._pressure(sell_stop_below_usd, min_notional)
        stop_sum = stop_up + stop_down
        trigger_asymmetry = (stop_up - stop_down) / stop_sum if stop_sum > 0 else 0.0

        return TriggerLiquidityFeatures(
            symbol=symbol_key,
            trigger_liquidity_available=True,
            nearest_stop_cluster_bps=self._nearest_cluster_bps(
                clusters,
                bucket_size=bucket_size,
                min_notional=min_notional,
                kind="stop",
            ),
            nearest_stop_cluster_above_bps=self._nearest_cluster_bps(
                clusters,
                bucket_size=bucket_size,
                min_notional=min_notional,
                kind="stop",
                region="above",
            ),
            nearest_stop_cluster_below_bps=self._nearest_cluster_bps(
                clusters,
                bucket_size=bucket_size,
                min_notional=min_notional,
                kind="stop",
                region="below",
            ),
            nearest_tp_cluster_bps=self._nearest_cluster_bps(
                clusters,
                bucket_size=bucket_size,
                min_notional=min_notional,
                kind="tp",
            ),
            nearest_tp_cluster_above_bps=self._nearest_cluster_bps(
                clusters,
                bucket_size=bucket_size,
                min_notional=min_notional,
                kind="tp",
                region="above",
            ),
            nearest_tp_cluster_below_bps=self._nearest_cluster_bps(
                clusters,
                bucket_size=bucket_size,
                min_notional=min_notional,
                kind="tp",
                region="below",
            ),
            stop_pressure_above=self._pressure(stop_above_usd, min_notional),
            stop_pressure_below=self._pressure(stop_below_usd, min_notional),
            tp_pressure_above=self._pressure(tp_above_usd, min_notional),
            tp_pressure_below=self._pressure(tp_below_usd, min_notional),
            trigger_asymmetry=round(trigger_asymmetry, 4),
            cascade_risk_up=round(min(stop_up, 1.0), 4),
            cascade_risk_down=round(min(stop_down, 1.0), 4),
            trigger_data_age_seconds=self._age_seconds(latest_seen, now_ms),
            total_trigger_notional_usd=round(total_notional, 4),
            max_trigger_cluster_notional_usd=round(max(clusters.values()), 4),
        )

    def _sum_clusters(
        self,
        clusters: dict[tuple[str, str, str, int], float],
        *,
        kind: str,
        region: str,
        side: str | None = None,
    ) -> float:
        return sum(
            notional
            for (cluster_kind, cluster_side, cluster_region, _), notional in clusters.items()
            if cluster_kind == kind
            and cluster_region == region
            and (side is None or cluster_side == side)
        )

    def _nearest_cluster_bps(
        self,
        clusters: dict[tuple[str, str, str, int], float],
        *,
        bucket_size: float,
        min_notional: float,
        kind: str,
        region: str | None = None,
    ) -> float:
        candidates = [
            (bucket + 0.5) * bucket_size
            for (cluster_kind, _, cluster_region, bucket), notional in clusters.items()
            if cluster_kind == kind
            and notional >= min_notional
            and (region is None or cluster_region == region)
        ]
        if not candidates:
            return 0.0
        return round(min(candidates), 4)

    def _pressure(self, notional_usd: float, min_notional: float) -> float:
        return round(min(max(notional_usd / min_notional, 0.0), 10.0), 4)

    def _age_seconds(self, observed_at_ms: int | None, now_ms: int | None) -> float | None:
        if observed_at_ms is None or now_ms is None:
            return None
        return round(max((now_ms - observed_at_ms) / 1000.0, 0.0), 4)


def order_from_payload(
    payload: dict[str, object],
    *,
    user: str = "",
    observed_at_ms: int | None = None,
) -> TriggerLiquidityOrder | None:
    if not bool(payload.get("isTrigger", False)):
        return None
    symbol = str(payload.get("coin", "")).strip().upper()
    oid = str(payload.get("oid", "")).strip()
    if not symbol or not oid:
        return None
    trigger_px = _float_or_zero(payload.get("triggerPx"))
    if trigger_px <= 0:
        return None
    return TriggerLiquidityOrder(
        symbol=symbol,
        oid=oid,
        user=user,
        side=str(payload.get("side", "")),
        trigger_px=trigger_px,
        limit_px=_float_or_zero(payload.get("limitPx")),
        sz=_float_or_zero(payload.get("sz")),
        orig_sz=_float_or_zero(payload.get("origSz")),
        order_type=str(payload.get("orderType", "")),
        trigger_condition=str(payload.get("triggerCondition", "")),
        is_position_tpsl=bool(payload.get("isPositionTpsl", False)),
        reduce_only=bool(payload.get("reduceOnly", False)),
        observed_at_ms=observed_at_ms or _int_or_none(payload.get("timestamp")),
    )


def parse_event_time_ms(raw: object) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    text = str(raw).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def iter_jsonl_files(path: str | Path) -> Iterable[Path]:
    source = Path(path)
    if source.is_file():
        yield source
        return
    if not source.exists():
        return
    yield from sorted(source.glob("*.jsonl"))


def _float_or_zero(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
