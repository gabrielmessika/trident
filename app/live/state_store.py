from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.portfolio.directional_state import DirectionalPortfolioState, OpenPosition, parse_timestamp
from app.trident.types import TradePlan


class LiveStateStore:
    """Durable local metadata used to recover live positions after restart."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"positions": {}, "orders": {}, "events": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"positions": {}, "orders": {}, "events": []}
        if not isinstance(payload, dict):
            return {"positions": {}, "orders": {}, "events": []}
        payload.setdefault("positions", {})
        payload.setdefault("orders", {})
        payload.setdefault("events", [])
        return payload

    def save(self, payload: dict[str, Any]) -> None:
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self.path)

    def save_portfolio(
        self,
        portfolio: DirectionalPortfolioState,
        *,
        orders: dict[str, Any] | None = None,
        mode: str = "live",
    ) -> None:
        current = self.load()
        current["mode"] = mode
        current["positions"] = {
            symbol: open_position_to_metadata(position)
            for symbol, position in portfolio.open_positions.items()
        }
        open_symbols = {str(symbol).upper() for symbol in current["positions"]}
        if orders is not None:
            current["orders"] = {
                str(symbol).upper(): metadata
                for symbol, metadata in orders.items()
                if str(symbol).upper() in open_symbols
            }
        else:
            existing_orders = current.get("orders", {})
            current["orders"] = {
                str(symbol).upper(): metadata
                for symbol, metadata in existing_orders.items()
                if str(symbol).upper() in open_symbols
            } if isinstance(existing_orders, dict) else {}
        self.save(current)

    def metadata_for_symbol(self, symbol: str) -> dict[str, Any] | None:
        positions = self.load().get("positions", {})
        if not isinstance(positions, dict):
            return None
        metadata = positions.get(symbol.upper()) or positions.get(symbol)
        return metadata if isinstance(metadata, dict) else None


def default_live_state_path(pod: str) -> str:
    normalized = _normalize_pod_name(pod)
    return f"runtime/trident/live_state_{normalized}.json"


def live_state_env_var(pod: str) -> str:
    normalized = _normalize_pod_name(pod).upper()
    return f"TRIDENT_LIVE_STATE_PATH_{normalized}"


def live_state_path_for_pod(pod: str, *, allow_global: bool = True) -> str:
    specific = os.getenv(live_state_env_var(pod))
    if specific:
        return specific
    if allow_global:
        global_path = os.getenv("TRIDENT_LIVE_STATE_PATH")
        if global_path:
            return global_path
    return default_live_state_path(pod)


def _normalize_pod_name(pod: str) -> str:
    return str(pod).strip().lower().replace("-", "_")


def open_position_to_metadata(position: OpenPosition) -> dict[str, Any]:
    payload = asdict(position)
    opened_at = payload.get("opened_at")
    if opened_at is not None:
        payload["opened_at"] = position.opened_at.isoformat() if position.opened_at else None
    return payload


def open_position_from_metadata(
    metadata: dict[str, Any],
    *,
    symbol: str | None = None,
    side: str | None = None,
    entry_price: float | None = None,
    target_notional_usd: float | None = None,
    margin_usd: float | None = None,
    leverage: float | None = None,
) -> OpenPosition:
    return OpenPosition(
        symbol=str(symbol or metadata.get("symbol", "")).upper(),
        side=str(side or metadata.get("side", "long")),
        setup=str(metadata.get("setup", "exchange_recovered")),
        confidence=float(metadata.get("confidence", 0.0) or 0.0),
        entry_price=float(entry_price if entry_price is not None else metadata.get("entry_price", 0.0) or 0.0),
        entry_fee_usd=float(metadata.get("entry_fee_usd", 0.0) or 0.0),
        target_notional_usd=float(
            target_notional_usd
            if target_notional_usd is not None
            else metadata.get("target_notional_usd", 0.0)
            or 0.0
        ),
        stop_bps=float(metadata.get("stop_bps", 0.0) or 0.0),
        opened_at=parse_timestamp(str(metadata.get("opened_at"))) if metadata.get("opened_at") else None,
        time_stop_hours=int(metadata.get("time_stop_hours", 0) or 0),
        take_profit_bps=float(metadata.get("take_profit_bps", 0.0) or 0.0),
        break_even_trigger_bps=float(metadata.get("break_even_trigger_bps", 0.0) or 0.0),
        trailing_activation_bps=float(metadata.get("trailing_activation_bps", 0.0) or 0.0),
        trailing_distance_bps=float(metadata.get("trailing_distance_bps", 0.0) or 0.0),
        reentry_cooldown_minutes=int(metadata.get("reentry_cooldown_minutes", 0) or 0),
        margin_usd=float(margin_usd if margin_usd is not None else metadata.get("margin_usd", 0.0) or 0.0),
        effective_leverage=float(leverage if leverage is not None else metadata.get("effective_leverage", 1.0) or 1.0),
        risk_budget_usd=float(metadata.get("risk_budget_usd", 0.0) or 0.0),
        expected_loss_usd=float(metadata.get("expected_loss_usd", 0.0) or 0.0),
        invalidation_price=(
            float(metadata["invalidation_price"])
            if metadata.get("invalidation_price") not in (None, "")
            else None
        ),
        isolated=bool(metadata.get("isolated", True)),
        best_price_seen=float(metadata.get("best_price_seen", entry_price or 0.0) or 0.0),
        setup_details=dict(metadata.get("setup_details", {}) or {}),
    )


def metadata_from_trade_plan(plan: TradePlan) -> dict[str, Any]:
    return {
        "symbol": plan.symbol,
        "side": plan.side,
        "setup": plan.setup,
        "confidence": plan.confidence,
        "target_notional_usd": plan.target_notional_usd,
        "stop_bps": plan.stop_bps,
        "time_stop_hours": plan.time_stop_hours,
        "take_profit_bps": plan.take_profit_bps,
        "break_even_trigger_bps": plan.break_even_trigger_bps,
        "trailing_activation_bps": plan.trailing_activation_bps,
        "trailing_distance_bps": plan.trailing_distance_bps,
        "reentry_cooldown_minutes": plan.reentry_cooldown_minutes,
        "margin_usd": plan.margin_usd,
        "effective_leverage": plan.effective_leverage,
        "risk_budget_usd": plan.risk_budget_usd,
        "expected_loss_usd": plan.expected_loss_usd,
        "invalidation_price": plan.invalidation_price,
        "isolated": plan.isolated,
        "setup_details": dict(plan.setup_details),
    }
