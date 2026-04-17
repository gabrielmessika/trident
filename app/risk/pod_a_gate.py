from __future__ import annotations

from collections import deque

from app.settings import PodAPatternVetoConfig
from app.risk.plan_gate import TradePlanRiskGate
from app.trident.pod_a.leverage import LeveragePolicy
from app.trident.pod_a.symbol_mode import active_symbol_mode
from app.trident.types import RiskDecision, TradePlan


class PodARiskGate(TradePlanRiskGate):
    """Pod A extends the shared gate with risk-budget and leverage checks."""

    def __init__(self, config) -> None:
        super().__init__(config)
        self._leverage_policy = LeveragePolicy(config.pod_a)
        self._allowed_setups = {
            item.strip() for item in config.pod_a.allowed_setups if item.strip()
        }
        self._disabled_setups = {item.strip() for item in config.pod_a.disabled_setups if item.strip()}
        self._blocked_regimes = {
            item.strip().lower() for item in config.pod_a.blocked_regimes if item.strip()
        }
        self._allowed_setups_in_blocked_regimes = {
            item.strip()
            for item in config.pod_a.allowed_setups_in_blocked_regimes
            if item.strip()
        }
        self._pattern_vetoes = list(config.pod_a.pattern_vetoes)
        self._pattern_watchers = list(config.pod_a.pattern_watchers)
        self._guardrail_lookback = max(config.pod_a.guardrail_lookback_trades, 1)
        self._setup_guardrail_lookback = max(config.pod_a.setup_guardrail_lookback_trades, 1)
        self._intraday_setup_guardrail_lookback = max(
            config.pod_a.intraday_setup_guardrail_lookback_trades,
            1,
        )
        self._closed_trade_pnl_by_key: dict[tuple[str, str], deque[float]] = {}
        self._closed_trade_pnl_by_setup: dict[str, deque[float]] = {}
        self._closed_trade_pnl_by_intraday_setup: dict[tuple[str, str], deque[float]] = {}

    def record_closed_trade(
        self,
        *,
        symbol: str,
        setup: str | None,
        pnl_usd: float | None,
        date_key: str | None = None,
    ) -> None:
        key = self._guardrail_key(symbol, setup)
        if key is None or pnl_usd is None:
            return
        history = self._closed_trade_pnl_by_key.get(key)
        if history is None:
            history = deque(maxlen=self._guardrail_lookback)
            self._closed_trade_pnl_by_key[key] = history
        history.append(float(pnl_usd))
        setup_key = self._setup_guardrail_key(setup)
        if setup_key is None:
            return
        setup_history = self._closed_trade_pnl_by_setup.get(setup_key)
        if setup_history is None:
            setup_history = deque(maxlen=self._setup_guardrail_lookback)
            self._closed_trade_pnl_by_setup[setup_key] = setup_history
        setup_history.append(float(pnl_usd))
        intraday_key = self._intraday_setup_guardrail_key(date_key, setup)
        if intraday_key is None:
            return
        intraday_history = self._closed_trade_pnl_by_intraday_setup.get(intraday_key)
        if intraday_history is None:
            intraday_history = deque(maxlen=self._intraday_setup_guardrail_lookback)
            self._closed_trade_pnl_by_intraday_setup[intraday_key] = intraday_history
        intraday_history.append(float(pnl_usd))

    def evaluate_many(self, plans: list[TradePlan]) -> list[RiskDecision]:
        decisions: list[RiskDecision] = []
        seen_symbols: set[str] = set()
        accepted_count = 0
        accepted_expected_loss_usd = 0.0

        for plan in plans:
            reason = self._decision_reason(
                plan=plan,
                accepted_count=accepted_count,
                seen_symbols=seen_symbols,
                accepted_expected_loss_usd=accepted_expected_loss_usd,
            )
            accepted = reason == "accepted"
            decisions.append(RiskDecision(accepted=accepted, reason=reason, trade_plan=plan))
            if accepted:
                accepted_count += 1
                seen_symbols.add(plan.symbol)
                accepted_expected_loss_usd += max(plan.expected_loss_usd, 0.0)
        return decisions

    def _decision_reason(
        self,
        *,
        plan: TradePlan,
        accepted_count: int,
        seen_symbols: set[str],
        accepted_expected_loss_usd: float = 0.0,
    ) -> str:
        reason = super()._decision_reason(
            plan=plan,
            accepted_count=accepted_count,
            seen_symbols=seen_symbols,
        )
        if reason != "accepted":
            return reason
        symbol_mode = active_symbol_mode(self._config.pod_a, plan.symbol)
        symbol_mode_allowed_setups = (
            {item.strip() for item in symbol_mode.allowed_setups if item.strip()}
            if symbol_mode is not None
            else set()
        )
        if (
            self._allowed_setups
            and plan.setup not in self._allowed_setups
            and plan.setup not in symbol_mode_allowed_setups
        ):
            return "setup_not_allowed"
        if plan.setup in self._disabled_setups and plan.setup not in symbol_mode_allowed_setups:
            return "setup_disabled"
        if self._rolling_intraday_setup_guardrail_triggered(plan):
            return "rolling_guardrail_intraday_setup"
        if self._rolling_setup_guardrail_triggered(plan.setup):
            return "rolling_guardrail_setup"
        if self._rolling_guardrail_triggered(plan.symbol, plan.setup):
            return "rolling_guardrail_symbol_setup"

        current_regime = str(plan.setup_details.get("regime", "")).strip().lower()
        if (
            current_regime in self._blocked_regimes
            and plan.setup not in self._allowed_setups_in_blocked_regimes
        ):
            return "regime_filtered"
        pattern_veto = self._pattern_veto_reason(plan)
        if pattern_veto is not None:
            return pattern_veto
        if symbol_mode is not None:
            if symbol_mode_allowed_setups and plan.setup not in symbol_mode_allowed_setups:
                return "symbol_mode_setup_filtered"
            allowed_regimes = {
                item.strip().lower() for item in symbol_mode.allowed_regimes if item.strip()
            }
            if allowed_regimes and current_regime not in allowed_regimes:
                return "symbol_mode_regime_filtered"
            if plan.confidence < max(symbol_mode.min_confidence, 0.0):
                return "symbol_mode_confidence_below_min"

        limits = self._config.trident.risk
        min_notional = max(
            limits.min_trade_notional_usd,
            self._config.pod_a.min_notional_usd,
        )
        if plan.target_notional_usd < min_notional:
            return "notional_below_min"
        if plan.margin_usd < self._config.pod_a.min_margin_usd:
            return "margin_below_min"
        global_limit = self._leverage_policy.max_allowed()
        symbol_limit = self._leverage_policy.max_allowed(plan.symbol)
        if plan.effective_leverage > symbol_limit:
            if symbol_limit < global_limit:
                return "leverage_above_asset_limit"
            return "leverage_above_limit"
        if plan.expected_loss_usd > max(plan.risk_budget_usd, 0.0):
            return "risk_budget_exceeded"

        max_total_open_risk_usd = (
            self._config.trident.capital.reference_equity_usd
            * max(limits.max_total_open_risk_pct, 0.0)
        )
        if accepted_expected_loss_usd + max(plan.expected_loss_usd, 0.0) > max_total_open_risk_usd:
            return "total_open_risk_exceeded"
        self._apply_pattern_watch_hits(plan)
        return "accepted"

    def _rolling_guardrail_triggered(self, symbol: str, setup: str | None) -> bool:
        if not self._config.pod_a.guardrail_enabled:
            return False
        key = self._guardrail_key(symbol, setup)
        if key is None:
            return False
        history = self._closed_trade_pnl_by_key.get(key)
        if history is None:
            return False
        if len(history) < max(self._config.pod_a.guardrail_min_closed_trades, 1):
            return False
        return sum(history) <= self._config.pod_a.guardrail_max_cumulative_loss_usd

    def _rolling_setup_guardrail_triggered(self, setup: str | None) -> bool:
        if not self._config.pod_a.setup_guardrail_enabled:
            return False
        setup_key = self._setup_guardrail_key(setup)
        if setup_key is None:
            return False
        history = self._closed_trade_pnl_by_setup.get(setup_key)
        if history is None:
            return False
        if len(history) < max(self._config.pod_a.setup_guardrail_min_closed_trades, 1):
            return False
        return sum(history) <= self._config.pod_a.setup_guardrail_max_cumulative_loss_usd

    def _rolling_intraday_setup_guardrail_triggered(self, plan: TradePlan) -> bool:
        if not self._config.pod_a.intraday_setup_guardrail_enabled:
            return False
        current_date_key = str(plan.setup_details.get("current_date_key", "")).strip()
        intraday_key = self._intraday_setup_guardrail_key(current_date_key, plan.setup)
        if intraday_key is None:
            return False
        history = self._closed_trade_pnl_by_intraday_setup.get(intraday_key)
        if history is None:
            return False
        if len(history) < max(self._config.pod_a.intraday_setup_guardrail_min_closed_trades, 1):
            return False
        cumulative_pnl = sum(history)
        average_pnl = cumulative_pnl / max(len(history), 1)
        return (
            cumulative_pnl <= self._config.pod_a.intraday_setup_guardrail_max_cumulative_loss_usd
            or average_pnl <= self._config.pod_a.intraday_setup_guardrail_max_average_pnl_usd
        )

    def _pattern_veto_reason(self, plan: TradePlan) -> str | None:
        for name in self._matching_pattern_rule_names(self._pattern_vetoes, plan):
            return f"pattern_veto_{self._normalize_rule_name(name)}"
        return None

    def _apply_pattern_watch_hits(self, plan: TradePlan) -> None:
        hits = self._matching_pattern_rule_names(self._pattern_watchers, plan)
        details = dict(plan.setup_details or {})
        if hits:
            details["pattern_watch_hits"] = ",".join(hits)
            details["pattern_watch_count"] = len(hits)
        else:
            details.pop("pattern_watch_hits", None)
            details.pop("pattern_watch_count", None)
        plan.setup_details = details

    def _matching_pattern_rule_names(
        self,
        rules: list[PodAPatternVetoConfig],
        plan: TradePlan,
    ) -> list[str]:
        return [rule.name for rule in rules if self._matches_pattern_veto(rule, plan)]

    def _matches_pattern_veto(
        self,
        rule: PodAPatternVetoConfig,
        plan: TradePlan,
    ) -> bool:
        if not rule.enabled:
            return False
        if rule.setups and plan.setup not in {item.strip() for item in rule.setups if item.strip()}:
            return False
        current_regime = str(plan.setup_details.get("regime", "")).strip()
        if rule.regimes and current_regime not in {item.strip() for item in rule.regimes if item.strip()}:
            return False
        if rule.require_candles_ready is not None:
            if bool(plan.setup_details.get("candles_ready")) != rule.require_candles_ready:
                return False
        if rule.require_supertrend_direction is not None:
            if int(plan.setup_details.get("supertrend_direction", 0) or 0) != rule.require_supertrend_direction:
                return False
        if not self._within_range(
            plan.setup_details.get("trend_1h_bps"),
            minimum=rule.min_trend_1h_bps,
            maximum=rule.max_trend_1h_bps,
        ):
            return False
        if not self._within_range(
            plan.setup_details.get("trend_4h_bps"),
            minimum=rule.min_trend_4h_bps,
            maximum=rule.max_trend_4h_bps,
        ):
            return False
        if not self._within_range(
            plan.setup_details.get("ichimoku_bias_score"),
            minimum=rule.min_ichimoku_bias_score,
            maximum=rule.max_ichimoku_bias_score,
        ):
            return False
        if not self._within_range(
            plan.setup_details.get("stoch_rsi_k"),
            minimum=rule.min_stoch_rsi_k,
            maximum=rule.max_stoch_rsi_k,
        ):
            return False
        if not self._within_range(
            plan.setup_details.get("cci20"),
            minimum=rule.min_cci20,
            maximum=rule.max_cci20,
        ):
            return False
        if not self._within_range(
            plan.setup_details.get("vwap_reclaim_score"),
            minimum=rule.min_vwap_reclaim_score,
            maximum=rule.max_vwap_reclaim_score,
        ):
            return False
        if not self._within_range(
            plan.setup_details.get("structure_score"),
            minimum=rule.min_structure_score,
            maximum=rule.max_structure_score,
        ):
            return False
        return True

    def _within_range(
        self,
        raw_value: object,
        *,
        minimum: float | None,
        maximum: float | None,
    ) -> bool:
        if minimum is None and maximum is None:
            return True
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            return False
        if minimum is not None and value < minimum:
            return False
        if maximum is not None and value > maximum:
            return False
        return True

    def _normalize_rule_name(self, name: str) -> str:
        normalized = "".join(
            character.lower() if character.isalnum() else "_"
            for character in name.strip()
        )
        while "__" in normalized:
            normalized = normalized.replace("__", "_")
        return normalized.strip("_") or "unnamed"

    def _guardrail_key(self, symbol: str, setup: str | None) -> tuple[str, str] | None:
        normalized_symbol = str(symbol).strip().upper()
        normalized_setup = self._setup_guardrail_key(setup)
        if not normalized_symbol or not normalized_setup:
            return None
        return normalized_symbol, normalized_setup

    def _setup_guardrail_key(self, setup: str | None) -> str | None:
        normalized_setup = str(setup or "").strip().lower()
        if not normalized_setup:
            return None
        return normalized_setup

    def _intraday_setup_guardrail_key(
        self,
        date_key: str | None,
        setup: str | None,
    ) -> tuple[str, str] | None:
        normalized_date = str(date_key or "").strip()
        normalized_setup = self._setup_guardrail_key(setup)
        if not normalized_date or not normalized_setup:
            return None
        return normalized_date, normalized_setup
