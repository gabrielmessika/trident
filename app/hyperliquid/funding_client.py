from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable

from app.hyperliquid.info_client import HyperliquidInfoClient
from app.settings import HyperliquidConfig


@dataclass(slots=True)
class FundingMarketSnapshot:
    symbol: str
    funding_rate: float
    open_interest: float | None
    mark_px: float | None
    oracle_px: float | None
    premium: float | None
    day_ntl_vlm: float | None
    day_base_vlm: float | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _float_or_none(value: object) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def extract_current_funding(
    payload: object,
    *,
    symbols: list[str] | None = None,
    include_delisted: bool = False,
) -> list[FundingMarketSnapshot]:
    if not isinstance(payload, list) or len(payload) < 2:
        return []
    meta = payload[0]
    asset_contexts = payload[1]
    if not isinstance(meta, dict) or not isinstance(asset_contexts, list):
        return []
    universe = meta.get("universe", [])
    if not isinstance(universe, list):
        return []

    requested = None if symbols is None else {str(symbol).upper() for symbol in symbols}
    parsed: list[FundingMarketSnapshot] = []
    for asset, context in zip(universe, asset_contexts):
        if not isinstance(asset, dict) or not isinstance(context, dict):
            continue
        symbol = str(asset.get("name", "")).upper()
        if not symbol:
            continue
        if requested is not None and symbol not in requested:
            continue
        if not include_delisted and bool(asset.get("isDelisted", False)):
            continue
        parsed.append(
            FundingMarketSnapshot(
                symbol=symbol,
                funding_rate=float(context.get("funding", 0.0)),
                open_interest=_float_or_none(context.get("openInterest")),
                mark_px=_float_or_none(context.get("markPx")),
                oracle_px=_float_or_none(context.get("oraclePx")),
                premium=_float_or_none(context.get("premium")),
                day_ntl_vlm=_float_or_none(context.get("dayNtlVlm")),
                day_base_vlm=_float_or_none(context.get("dayBaseVlm")),
            )
        )
    return parsed


class HyperliquidFundingClient:
    """Fetches current market funding and open-interest snapshots from Hyperliquid."""

    def __init__(
        self,
        config: HyperliquidConfig,
        *,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.info_client = HyperliquidInfoClient(config, sleep_fn=sleep_fn)

    def fetch_current_funding(
        self,
        *,
        symbols: list[str] | None = None,
        include_delisted: bool = False,
    ) -> list[FundingMarketSnapshot]:
        payload = self.info_client.post_info({"type": "metaAndAssetCtxs"})
        return extract_current_funding(
            payload,
            symbols=symbols,
            include_delisted=include_delisted,
        )
