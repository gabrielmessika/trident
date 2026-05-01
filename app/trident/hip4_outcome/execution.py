from __future__ import annotations

import secrets
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any

from app.execution.live import parse_order_result
from app.hyperliquid.private_state import HyperliquidCredentials, sdk_base_url_from_info_url
from app.live.errors import HyperliquidAPIError
from app.trident.hip4_outcome.config import Hip4OutcomeConfig
from app.trident.hip4_outcome.models import (
    OutcomeExecutionResult,
    OutcomeFill,
    OutcomeMarket,
    OutcomeOpportunity,
    OutcomeOrderBook,
    outcome_asset_id,
)


class PaperOutcomeExecutor:
    def __init__(self, config: Hip4OutcomeConfig) -> None:
        self.config = config

    def execute(
        self,
        *,
        opportunity: OutcomeOpportunity,
        market: OutcomeMarket,
        order_book: OutcomeOrderBook,
        approved_size_usdc: float,
    ) -> OutcomeExecutionResult:
        fills = build_paper_fills(
            opportunity=opportunity,
            market=market,
            order_book=order_book,
            approved_size_usdc=approved_size_usdc,
            size_decimals=self.config.outcome_size_decimals,
        )
        return OutcomeExecutionResult(status="paper_filled" if fills else "paper_no_fill", fills=fills)


class TestnetOutcomeExecutor:
    def __init__(self, config: Hip4OutcomeConfig, credentials: HyperliquidCredentials | None = None) -> None:
        self.config = config
        self.credentials = credentials or HyperliquidCredentials.from_env()
        self._exchange_client: Any | None = None
        self.account_address: str | None = None
        self._validate_config()

    def execute(
        self,
        *,
        opportunity: OutcomeOpportunity,
        market: OutcomeMarket,
        order_book: OutcomeOrderBook,
        approved_size_usdc: float,
    ) -> OutcomeExecutionResult:
        exchange = self._exchange()
        self._register_outcome_assets(exchange, market)
        leg_specs = build_order_legs(
            opportunity=opportunity,
            market=market,
            order_book=order_book,
            approved_size_usdc=approved_size_usdc,
            max_order_slippage=self.config.max_order_slippage,
            size_decimals=self.config.outcome_size_decimals,
        )
        if not leg_specs:
            return OutcomeExecutionResult(status="no_order_legs", error="No executable outcome legs")

        fills: list[OutcomeFill] = []
        raw_responses: list[object] = []
        for leg in leg_specs:
            cloid = self._new_cloid()
            try:
                from hyperliquid.utils.types import Cloid
            except Exception as exc:  # pragma: no cover - dependency guard
                raise HyperliquidAPIError("hyperliquid-python-sdk is required for testnet outcome orders") from exc
            raw = exchange.order(
                leg["coin"],
                True,
                float(leg["token_qty"]),
                float(leg["limit_price"]),
                {"limit": {"tif": self.config.order_tif}},
                reduce_only=False,
                cloid=Cloid.from_str(cloid),
            )
            raw_responses.append(raw)
            parsed = parse_order_result(raw, cloid=cloid)
            if parsed.filled:
                fills.append(
                    OutcomeFill(
                        coin=str(leg["coin"]),
                        side_name=str(leg["side_name"]),
                        token_qty=parsed.filled_size,
                        avg_price=parsed.avg_price,
                        cost_usdc=round(float(parsed.filled_size) * parsed.avg_price, 8),
                        status=parsed.status,
                        oid=parsed.oid,
                        cloid=cloid,
                        raw=raw,
                    )
                )
            else:
                fills.append(
                    OutcomeFill(
                        coin=str(leg["coin"]),
                        side_name=str(leg["side_name"]),
                        token_qty=Decimal("0"),
                        avg_price=0.0,
                        cost_usdc=0.0,
                        status=parsed.error or parsed.status,
                        oid=parsed.oid,
                        cloid=cloid,
                        raw=raw,
                    )
                )
        status = "testnet_filled" if any(fill.token_qty > 0 for fill in fills) else "testnet_no_fill"
        return OutcomeExecutionResult(status=status, fills=fills, raw=raw_responses)

    def _validate_config(self) -> None:
        if self.config.require_testnet_url and "hyperliquid-testnet" not in self.config.info_url:
            raise HyperliquidAPIError("HIP-4 outcome testnet executor refuses non-testnet info_url")
        if not self.credentials.secret_key:
            raise HyperliquidAPIError("TRIDENT_SECRET_KEY or HYPERLIQUID_SECRET_KEY is required for testnet orders")
        if not (self.credentials.secret_key.startswith("0x") and len(self.credentials.secret_key) == 66):
            raise HyperliquidAPIError("Testnet secret key must be a 0x-prefixed 32-byte private key")

    def _exchange(self) -> Any:
        if self._exchange_client is not None:
            return self._exchange_client
        try:
            from eth_account import Account
            from hyperliquid.exchange import Exchange
        except Exception as exc:  # pragma: no cover - dependency guard
            raise HyperliquidAPIError("hyperliquid-python-sdk and eth-account are required") from exc
        wallet = Account.from_key(self.credentials.secret_key)
        self.account_address = (
            self.credentials.account_address
            if self.credentials.account_address and self.credentials.account_address.startswith("0x")
            else wallet.address
        )
        self._exchange_client = Exchange(
            wallet,
            sdk_base_url_from_info_url(self.config.info_url),
            vault_address=self.credentials.vault_address,
            account_address=self.account_address,
            timeout=self.config.request_timeout_seconds,
        )
        return self._exchange_client

    def resolve_account_address(self) -> str:
        if self.account_address:
            return self.account_address
        if self.credentials.account_address and self.credentials.account_address.startswith("0x"):
            self.account_address = self.credentials.account_address
            return self.account_address
        try:
            from eth_account import Account
        except Exception as exc:  # pragma: no cover - dependency guard
            raise HyperliquidAPIError("eth-account is required to derive the testnet account") from exc
        wallet = Account.from_key(self.credentials.secret_key)
        self.account_address = wallet.address
        return self.account_address

    def _register_outcome_assets(self, exchange: Any, market: OutcomeMarket) -> None:
        for side, coin in ((0, market.yes_coin), (1, market.no_coin)):
            asset = outcome_asset_id(market.outcome, side)
            exchange.info.name_to_coin[coin] = coin
            exchange.info.coin_to_asset[coin] = asset
            exchange.info.asset_to_sz_decimals[asset] = self.config.outcome_size_decimals

    def _new_cloid(self) -> str:
        millis = int(time.time() * 1000) & ((1 << 48) - 1)
        random_bits = secrets.randbits(80)
        return f"0x{((millis << 80) | random_bits):032x}"


def build_paper_fills(
    *,
    opportunity: OutcomeOpportunity,
    market: OutcomeMarket,
    order_book: OutcomeOrderBook,
    approved_size_usdc: float,
    size_decimals: int,
) -> list[OutcomeFill]:
    fills: list[OutcomeFill] = []
    for leg in build_order_legs(
        opportunity=opportunity,
        market=market,
        order_book=order_book,
        approved_size_usdc=approved_size_usdc,
        max_order_slippage=0.0,
        size_decimals=size_decimals,
    ):
        price = float(leg["reference_price"])
        qty = Decimal(str(leg["token_qty"]))
        fills.append(
            OutcomeFill(
                coin=str(leg["coin"]),
                side_name=str(leg["side_name"]),
                token_qty=qty,
                avg_price=price,
                cost_usdc=round(float(qty) * price, 8),
                status="paper_filled",
            )
        )
    return fills


def build_order_legs(
    *,
    opportunity: OutcomeOpportunity,
    market: OutcomeMarket,
    order_book: OutcomeOrderBook,
    approved_size_usdc: float,
    max_order_slippage: float,
    size_decimals: int,
) -> list[dict[str, object]]:
    approved_size_usdc = max(float(approved_size_usdc), 0.0)
    if approved_size_usdc <= 0:
        return []
    if opportunity.side == "BUY_YES":
        return _single_leg(
            coin=market.yes_coin,
            side_name="YES",
            ask=order_book.yes.ask,
            spend_usdc=approved_size_usdc,
            max_order_slippage=max_order_slippage,
            size_decimals=size_decimals,
        )
    if opportunity.side == "BUY_NO":
        return _single_leg(
            coin=market.no_coin,
            side_name="NO",
            ask=order_book.no.ask,
            spend_usdc=approved_size_usdc,
            max_order_slippage=max_order_slippage,
            size_decimals=size_decimals,
        )
    if opportunity.side != "BUY_BOTH":
        return []
    if order_book.yes.ask is None or order_book.no.ask is None:
        return []
    yes_limit = min(order_book.yes.ask * (1.0 + max_order_slippage), 0.99999)
    no_limit = min(order_book.no.ask * (1.0 + max_order_slippage), 0.99999)
    unit_cost = yes_limit + no_limit
    if unit_cost <= 0:
        return []
    qty = _quantize_size(Decimal(str(approved_size_usdc / unit_cost)), size_decimals)
    if qty <= 0:
        return []
    return [
        {
            "coin": market.yes_coin,
            "side_name": "YES",
            "token_qty": qty,
            "reference_price": order_book.yes.ask,
            "limit_price": round(yes_limit, 8),
        },
        {
            "coin": market.no_coin,
            "side_name": "NO",
            "token_qty": qty,
            "reference_price": order_book.no.ask,
            "limit_price": round(no_limit, 8),
        },
    ]


def _single_leg(
    *,
    coin: str,
    side_name: str,
    ask: float | None,
    spend_usdc: float,
    max_order_slippage: float,
    size_decimals: int,
) -> list[dict[str, object]]:
    if ask is None or ask <= 0:
        return []
    limit_price = min(ask * (1.0 + max_order_slippage), 0.99999)
    qty = _quantize_size(Decimal(str(spend_usdc / limit_price)), size_decimals)
    if qty <= 0:
        return []
    return [
        {
            "coin": coin,
            "side_name": side_name,
            "token_qty": qty,
            "reference_price": ask,
            "limit_price": round(limit_price, 8),
        }
    ]


def _quantize_size(value: Decimal, size_decimals: int) -> Decimal:
    decimals = max(int(size_decimals), 0)
    quantum = Decimal("1") if decimals == 0 else Decimal("1").scaleb(-decimals)
    return value.quantize(quantum, rounding=ROUND_DOWN)
