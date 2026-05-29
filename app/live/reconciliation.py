from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.hyperliquid.private_state import ExchangeAccountState
from app.live.exchange_position_metrics import (
    apply_exchange_position_to_local,
    exchange_entry_notional_usd,
)
from app.live.state_store import LiveStateStore, open_position_from_metadata
from app.portfolio.directional_state import DirectionalPortfolioState


@dataclass(slots=True)
class ReconciliationReport:
    ready: bool
    recovered_symbols: list[str] = field(default_factory=list)
    external_known_positions: list[str] = field(default_factory=list)
    unknown_exchange_positions: list[str] = field(default_factory=list)
    missing_exchange_positions: list[str] = field(default_factory=list)
    side_mismatches: list[str] = field(default_factory=list)
    open_orders: list[str] = field(default_factory=list)
    trigger_orders: list[str] = field(default_factory=list)
    account_mode: str | None = None
    equity_usd: float = 0.0
    withdrawable_usd: float = 0.0
    total_margin_used_usd: float = 0.0
    spot_usdc_total: float | None = None
    spot_usdc_hold: float | None = None
    spot_usdc_available: float | None = None
    hl_available_usd: float = 0.0
    hl_capital_source: str = "perp_withdrawable"
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "recovered_symbols": self.recovered_symbols,
            "external_known_positions": self.external_known_positions,
            "unknown_exchange_positions": self.unknown_exchange_positions,
            "missing_exchange_positions": self.missing_exchange_positions,
            "side_mismatches": self.side_mismatches,
            "open_orders": self.open_orders,
            "trigger_orders": self.trigger_orders,
            "account_mode": self.account_mode,
            "equity_usd": self.equity_usd,
            "withdrawable_usd": self.withdrawable_usd,
            "total_margin_used_usd": self.total_margin_used_usd,
            "spot_usdc_total": self.spot_usdc_total,
            "spot_usdc_hold": self.spot_usdc_hold,
            "spot_usdc_available": self.spot_usdc_available,
            "hl_available_usd": self.hl_available_usd,
            "hl_capital_source": self.hl_capital_source,
            "perp_account_value_usd": self.equity_usd,
            "perp_withdrawable_usd": self.withdrawable_usd,
            "reasons": self.reasons,
        }


def reconcile_exchange_state(
    *,
    account_state: ExchangeAccountState,
    portfolio: DirectionalPortfolioState,
    state_store: LiveStateStore,
    allow_unknown_exchange_positions: bool = False,
    allow_open_orders: bool = False,
    recover_known_positions: bool = True,
    external_state_stores: Sequence[LiveStateStore] | None = None,
) -> ReconciliationReport:
    report = ReconciliationReport(
        ready=True,
        account_mode=account_state.account_mode,
        equity_usd=account_state.account_value_usd,
        withdrawable_usd=account_state.withdrawable_usd,
        total_margin_used_usd=account_state.total_margin_used_usd,
        spot_usdc_total=account_state.spot_usdc_total,
        spot_usdc_hold=account_state.spot_usdc_hold,
        spot_usdc_available=account_state.spot_usdc_available,
        hl_available_usd=account_state.hl_available_usd,
        hl_capital_source=account_state.hl_capital_source,
    )

    external_state_stores = list(external_state_stores or [])
    known_order_ids = _known_order_ids([state_store, *external_state_stores])
    for order in account_state.all_orders:
        if order.oid is not None and int(order.oid) in known_order_ids:
            continue
        label = f"{order.symbol}:{order.oid or order.cloid or 'unknown'}"
        if order.is_trigger:
            report.trigger_orders.append(label)
        else:
            report.open_orders.append(label)

    if (report.open_orders or report.trigger_orders) and not allow_open_orders:
        report.ready = False
        report.reasons.append("exchange_open_orders_present")

    for symbol, exchange_position in account_state.positions.items():
        local = portfolio.open_positions.get(symbol)
        if local is not None:
            if local.side != exchange_position.side:
                report.side_mismatches.append(symbol)
                report.ready = False
            else:
                apply_exchange_position_to_local(local, exchange_position)
            continue
        metadata = state_store.metadata_for_symbol(symbol)
        if metadata is not None and recover_known_positions:
            portfolio.open_positions[symbol] = open_position_from_metadata(
                metadata,
                symbol=symbol,
                side=exchange_position.side,
                entry_price=exchange_position.entry_price,
                target_notional_usd=exchange_entry_notional_usd(exchange_position),
                margin_usd=exchange_position.margin_used_usd,
                leverage=exchange_position.leverage,
            )
            report.recovered_symbols.append(symbol)
            continue
        pending_metadata = state_store.pending_metadata_for_symbol(symbol)
        if pending_metadata is not None and recover_known_positions:
            metadata_side = str(pending_metadata.get("side", "")).lower()
            if metadata_side in {"long", "short"} and metadata_side != exchange_position.side:
                report.side_mismatches.append(symbol)
                report.ready = False
                continue
            portfolio.open_positions[symbol] = open_position_from_metadata(
                pending_metadata,
                symbol=symbol,
                side=exchange_position.side,
                entry_price=exchange_position.entry_price,
                target_notional_usd=exchange_entry_notional_usd(exchange_position),
                margin_usd=exchange_position.margin_used_usd,
                leverage=exchange_position.leverage,
            )
            report.recovered_symbols.append(symbol)
            continue
        external_metadata = _metadata_for_symbol(external_state_stores, symbol)
        if external_metadata is not None:
            metadata_side = str(external_metadata.get("side", "")).lower()
            if metadata_side in {"long", "short"} and metadata_side != exchange_position.side:
                report.side_mismatches.append(symbol)
                report.ready = False
            else:
                report.external_known_positions.append(symbol)
            continue
        report.unknown_exchange_positions.append(symbol)
        if not allow_unknown_exchange_positions:
            report.ready = False

    for symbol in portfolio.open_positions:
        if symbol not in account_state.positions:
            report.missing_exchange_positions.append(symbol)
            report.ready = False

    if report.unknown_exchange_positions:
        report.reasons.append("unknown_exchange_positions")
    if report.missing_exchange_positions:
        report.reasons.append("local_positions_missing_on_exchange")
    if report.side_mismatches:
        report.reasons.append("exchange_local_side_mismatch")

    return report


def _known_order_ids(state_stores: Sequence[LiveStateStore]) -> set[int]:
    known: set[int] = set()
    for state_store in state_stores:
        payload = state_store.load()
        orders = payload.get("orders", {})
        if not isinstance(orders, dict):
            continue
        for metadata in orders.values():
            if not isinstance(metadata, dict):
                continue
            for key in ("entry_oid", "tp_oid", "sl_oid"):
                value = metadata.get(key)
                if value is None:
                    continue
                try:
                    known.add(int(value))
                except (TypeError, ValueError):
                    pass
            protective = metadata.get("protective_oids", {})
            if isinstance(protective, dict):
                for value in protective.values():
                    if value is None:
                        continue
                    try:
                        known.add(int(value))
                    except (TypeError, ValueError):
                        pass
            stop_grace = metadata.get("stop_grace", {})
            if isinstance(stop_grace, dict):
                for key in ("catastrophic_sl_oid", "normal_sl_oid"):
                    value = stop_grace.get(key)
                    if value is None:
                        continue
                    try:
                        known.add(int(value))
                    except (TypeError, ValueError):
                        pass
    return known


def _metadata_for_symbol(
    state_stores: Sequence[LiveStateStore],
    symbol: str,
) -> dict[str, object] | None:
    for state_store in state_stores:
        metadata = state_store.metadata_for_symbol(symbol)
        if metadata is not None:
            return metadata
    return None
