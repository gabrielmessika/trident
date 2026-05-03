from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from typing import Any

from app.trident.hip4_outcome.client import HIP4OutcomeInfoClient
from app.trident.hip4_outcome.config import Hip4OutcomeConfig
from app.trident.hip4_outcome.models import OutcomePosition, SupervisorDecision
from app.trident.hip4_outcome.reconciliation import parse_spot_balances


@dataclass(slots=True)
class OutcomeCapitalSnapshot:
    mode: str
    budget_usdc: float
    open_exposure_usdc: float
    remaining_budget_usdc: float
    approved_size_before_usdc: float = 0.0
    approved_size_after_usdc: float = 0.0
    testnet_balance_coin: str = "USDC"
    testnet_available_usdc: float | None = None
    testnet_balance_source: str | None = None
    testnet_perp_withdrawable_usdc: float | None = None
    testnet_balance_buffer_usdc: float = 0.0
    testnet_spot_transfer_usdc: float = 0.0
    testnet_spot_transfer_status: str | None = None
    account_address: str | None = None
    reason: str = "capital_ok"
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class OutcomeCapitalGuard:
    """Caps HIP-4 Pod B order size by budget and testnet cash availability."""

    def __init__(
        self,
        config: Hip4OutcomeConfig,
        info_client: HIP4OutcomeInfoClient,
    ) -> None:
        self.config = config
        self.info_client = info_client

    def local_snapshot(
        self,
        *,
        open_positions: list[OutcomePosition],
    ) -> OutcomeCapitalSnapshot:
        open_exposure = _open_exposure_usdc(open_positions)
        budget = self._budget_usdc()
        return OutcomeCapitalSnapshot(
            mode=self.config.mode,
            budget_usdc=budget,
            open_exposure_usdc=open_exposure,
            remaining_budget_usdc=round(max(budget - open_exposure, 0.0), 8),
            testnet_balance_coin=self.config.testnet_balance_coin,
            testnet_balance_buffer_usdc=float(self.config.testnet_balance_buffer_usdc),
        )

    def apply(
        self,
        *,
        decision: SupervisorDecision,
        open_positions: list[OutcomePosition],
        testnet_executor: Any | None = None,
    ) -> tuple[SupervisorDecision, OutcomeCapitalSnapshot]:
        snapshot = self.local_snapshot(open_positions=open_positions)
        if not decision.approved:
            return decision, replace(snapshot, reason=decision.reason)

        approved_before = max(float(decision.approved_size_usdc), 0.0)
        approved_after = min(approved_before, snapshot.remaining_budget_usdc)
        reason = "capital_ok"
        error: str | None = None

        if approved_after <= 0:
            return (
                self._reject(decision, "capital_budget_exhausted", snapshot=snapshot),
                replace(
                    snapshot,
                    approved_size_before_usdc=approved_before,
                    approved_size_after_usdc=0.0,
                    reason="capital_budget_exhausted",
                ),
            )

        if self.config.mode == "testnet" and self.config.enforce_testnet_balance_check:
            balance_snapshot = self._testnet_balance_snapshot(
                snapshot=snapshot,
                testnet_executor=testnet_executor,
            )
            snapshot = balance_snapshot
            if balance_snapshot.error:
                return (
                    self._reject(decision, "testnet_balance_check_failed", snapshot=balance_snapshot),
                    replace(
                        balance_snapshot,
                        approved_size_before_usdc=approved_before,
                        approved_size_after_usdc=0.0,
                        reason="testnet_balance_check_failed",
                    ),
                )
            available = max(float(balance_snapshot.testnet_available_usdc or 0.0), 0.0)
            spendable = max(available - float(self.config.testnet_balance_buffer_usdc), 0.0)
            approved_after = min(approved_after, spendable)
            if approved_after <= 0:
                reason = "insufficient_testnet_usdc"

        if approved_after <= 0:
            return (
                self._reject(decision, reason, snapshot=snapshot),
                replace(
                    snapshot,
                    approved_size_before_usdc=approved_before,
                    approved_size_after_usdc=0.0,
                    reason=reason,
                    error=error,
                ),
            )

        adjusted = replace(
            decision,
            approved_size_usdc=round(approved_after, 6),
            constraints={
                **decision.constraints,
                "capital": replace(
                    snapshot,
                    approved_size_before_usdc=approved_before,
                    approved_size_after_usdc=round(approved_after, 8),
                    reason=reason,
                    error=error,
                ).to_dict(),
            },
        )
        return (
            adjusted,
            replace(
                snapshot,
                approved_size_before_usdc=approved_before,
                approved_size_after_usdc=round(approved_after, 8),
                reason=reason,
                error=error,
            ),
        )

    def testnet_balance_snapshot(
        self,
        *,
        open_positions: list[OutcomePosition],
        testnet_executor: Any | None,
    ) -> OutcomeCapitalSnapshot:
        return self._testnet_balance_snapshot(
            snapshot=self.local_snapshot(open_positions=open_positions),
            testnet_executor=testnet_executor,
        )

    def _testnet_balance_snapshot(
        self,
        *,
        snapshot: OutcomeCapitalSnapshot,
        testnet_executor: Any | None,
    ) -> OutcomeCapitalSnapshot:
        if testnet_executor is None:
            return replace(snapshot, error="testnet_executor_unavailable")
        try:
            account_address = str(testnet_executor.resolve_account_address())
            balances = parse_spot_balances(self.info_client.fetch_spot_state(account_address))
            available = _available_balance_usdc(
                balances,
                coin=self.config.testnet_balance_coin,
            )
            source = "spotClearinghouseState"
            transfer_usdc = Decimal("0")
            transfer_status: str | None = None
            clearinghouse_available = self._fetch_clearinghouse_withdrawable(account_address)
            if self.config.auto_transfer_testnet_spot_usdc:
                transfer_target = Decimal(str(self._spot_transfer_target_usdc(snapshot)))
                if available < transfer_target and clearinghouse_available is not None:
                    transfer_usdc = min(
                        max(transfer_target - available, Decimal("0")),
                        max(
                            clearinghouse_available - Decimal(str(self.config.testnet_balance_buffer_usdc)),
                            Decimal("0"),
                        ),
                    )
                    if transfer_usdc > 0:
                        transferer = getattr(testnet_executor, "transfer_usd_to_spot", None)
                        if transferer is None:
                            transfer_status = "transfer_method_unavailable"
                        else:
                            raw = transferer(float(transfer_usdc))
                            transfer_status = _transfer_status(raw)
                            source = "spotClearinghouseState_after_usdClassTransfer"
                            balances = parse_spot_balances(
                                self.info_client.fetch_spot_state(account_address)
                            )
                            available = _available_balance_usdc(
                                balances,
                                coin=self.config.testnet_balance_coin,
                            )
            elif available <= 0 and clearinghouse_available is not None and clearinghouse_available > 0:
                transfer_status = "manual_usd_class_transfer_required"
            return replace(
                snapshot,
                account_address=account_address,
                testnet_available_usdc=round(float(available), 8),
                testnet_balance_source=source,
                testnet_perp_withdrawable_usdc=(
                    None
                    if clearinghouse_available is None
                    else round(float(clearinghouse_available), 8)
                ),
                testnet_spot_transfer_usdc=round(float(transfer_usdc), 8),
                testnet_spot_transfer_status=transfer_status,
            )
        except Exception as exc:
            return replace(snapshot, error=str(exc))

    def _spot_transfer_target_usdc(self, snapshot: OutcomeCapitalSnapshot) -> float:
        configured = float(self.config.testnet_spot_transfer_target_usdc)
        if configured > 0:
            return round(configured, 8)
        budget_target = min(
            float(snapshot.remaining_budget_usdc),
            self._budget_usdc(),
        )
        return round(
            max(
                budget_target + float(self.config.testnet_balance_buffer_usdc),
                float(self.config.min_order_value_usdc) + float(self.config.testnet_balance_buffer_usdc),
            ),
            8,
        )

    def _fetch_clearinghouse_withdrawable(self, account_address: str) -> Decimal | None:
        fetcher = getattr(self.info_client, "fetch_clearinghouse_state", None)
        if fetcher is None:
            return None
        payload = fetcher(account_address)
        if not isinstance(payload, dict):
            return None
        withdrawable = _decimal_or_none(payload.get("withdrawable"))
        if withdrawable is not None:
            return max(withdrawable, Decimal("0"))
        margin_summary = payload.get("marginSummary", {})
        if isinstance(margin_summary, dict):
            account_value = _decimal_or_none(margin_summary.get("accountValue"))
            if account_value is not None:
                return max(account_value, Decimal("0"))
        return None

    def _budget_usdc(self) -> float:
        configured = float(self.config.pod_b_budget_usdc)
        if configured <= 0:
            configured = float(self.config.max_total_outcome_exposure_usdc)
        return round(max(configured, 0.0), 8)

    def _reject(
        self,
        decision: SupervisorDecision,
        reason: str,
        *,
        snapshot: OutcomeCapitalSnapshot,
    ) -> SupervisorDecision:
        return replace(
            decision,
            approved=False,
            approved_size_usdc=0.0,
            reason=reason,
            constraints={
                **decision.constraints,
                "capital": replace(snapshot, reason=reason).to_dict(),
            },
        )


def _open_exposure_usdc(open_positions: list[OutcomePosition]) -> float:
    return round(
        sum(max(float(position.max_loss_usdc), 0.0) for position in open_positions),
        8,
    )


def _available_balance_usdc(
    balances: dict[str, Any],
    *,
    coin: str,
) -> Decimal:
    target = coin.strip().upper()
    for balance_coin, balance in balances.items():
        normalized = str(balance_coin).strip().upper()
        if normalized == target:
            return Decimal(balance.available)
    return Decimal("0")


def _decimal_or_none(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _transfer_status(raw: object) -> str:
    if not isinstance(raw, dict):
        return str(raw)
    status = str(raw.get("status", "unknown"))
    response = raw.get("response")
    if status.lower() != "ok":
        return f"{status}:{response}"
    if isinstance(response, dict) and response.get("type"):
        return f"ok:{response.get('type')}"
    return status
