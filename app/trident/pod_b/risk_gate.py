from __future__ import annotations

from collections import deque

from app.risk.plan_gate import TradePlanRiskGate
from app.settings import AppConfig, PodBPatternRuleConfig


class PodBRiskGate(TradePlanRiskGate):
    """Directional risk gate for Pod B breakout."""

    def __init__(self, config: AppConfig) -> None:
        super().__init__(config)
        self._config = config
        self._guardrail_lookback = max(config.pod_b.bis_guardrail_lookback_trades, 1)
        self._closed_trade_pnl_by_key: dict[tuple[str, str], deque[float]] = {}
        self._pattern_vetoes = list(config.pod_b.pattern_vetoes)
        self._pattern_watchers = list(config.pod_b.pattern_watchers)

    def record_closed_trade(
        self,
        *,
        symbol: str,
        setup: str | None,
        pnl_usd: float | None,
    ) -> None:
        key = self._guardrail_key(symbol, setup)
        if key is None or pnl_usd is None:
            return
        history = self._closed_trade_pnl_by_key.get(key)
        if history is None:
            history = deque(maxlen=self._guardrail_lookback)
            self._closed_trade_pnl_by_key[key] = history
        history.append(float(pnl_usd))

    def evaluate_many(
        self,
        plans: list,
        *,
        current_open_expected_loss_usd: float = 0.0,
        current_open_position_count: int = 0,
    ) -> list:
        decisions: list = []
        seen_symbols: set[str] = set()
        accepted_count = 0
        accepted_expected_loss_usd = 0.0

        for plan in plans:
            reason = self._decision_reason(
                plan=plan,
                accepted_count=accepted_count,
                seen_symbols=seen_symbols,
                current_open_expected_loss_usd=current_open_expected_loss_usd,
                current_open_position_count=current_open_position_count,
                accepted_expected_loss_usd=accepted_expected_loss_usd,
            )
            accepted = reason == "accepted"
            from app.trident.types import RiskDecision

            decisions.append(RiskDecision(accepted=accepted, reason=reason, trade_plan=plan))
            if accepted:
                accepted_count += 1
                seen_symbols.add(plan.symbol)
                accepted_expected_loss_usd += max(plan.expected_loss_usd, 0.0)
        return decisions

    def _decision_reason(
        self,
        *,
        plan,
        accepted_count: int,
        seen_symbols: set[str],
        current_open_expected_loss_usd: float = 0.0,
        current_open_position_count: int = 0,
        accepted_expected_loss_usd: float = 0.0,
    ) -> str:
        if plan.confidence < self._config.pod_b.bis_min_confidence:
            return "confidence_below_min"
        if accepted_count + current_open_position_count >= self._config.pod_b.bis_max_concurrent_positions:
            return "max_open_positions_reached"
        reason = super()._decision_reason(
            plan=plan,
            accepted_count=accepted_count,
            seen_symbols=seen_symbols,
        )
        if reason != "accepted":
            return reason
        if self._rolling_guardrail_triggered(plan.symbol, plan.setup):
            return "rolling_guardrail_symbol_setup"
        pattern_veto = self._pattern_veto_reason(plan)
        if pattern_veto is not None:
            return pattern_veto
        min_notional = max(
            self._config.trident.risk.min_trade_notional_usd,
            self._config.pod_b.bis_min_notional_usd,
        )
        if plan.target_notional_usd < min_notional:
            return "notional_below_min"
        if plan.margin_usd < self._config.pod_b.bis_min_margin_usd:
            return "margin_below_min"
        symbol_limit = self._config.pod_b.bis_max_leverage_by_symbol.get(
            plan.symbol.upper(),
            self._config.pod_b.bis_max_leverage,
        )
        if plan.effective_leverage > min(symbol_limit, self._config.pod_b.bis_max_leverage):
            return "leverage_above_asset_limit"
        if plan.expected_loss_usd > max(plan.risk_budget_usd, 0.0):
            return "risk_budget_exceeded"
        max_total_open_risk_usd = (
            self._config.trident.capital.reference_equity_usd
            * max(self._config.pod_b.bis_max_total_open_risk_pct, 0.0)
        )
        if (
            current_open_expected_loss_usd
            + accepted_expected_loss_usd
            + max(plan.expected_loss_usd, 0.0)
            > max_total_open_risk_usd
        ):
            return "total_open_risk_exceeded"
        self._apply_pattern_watch_hits(plan)
        return "accepted"

    def _rolling_guardrail_triggered(self, symbol: str, setup: str | None) -> bool:
        if not self._config.pod_b.bis_guardrail_enabled:
            return False
        key = self._guardrail_key(symbol, setup)
        if key is None:
            return False
        history = self._closed_trade_pnl_by_key.get(key)
        if history is None:
            return False
        if len(history) < max(self._config.pod_b.bis_guardrail_min_closed_trades, 1):
            return False
        cumulative_pnl = sum(history)
        return cumulative_pnl <= self._config.pod_b.bis_guardrail_max_cumulative_loss_usd

    def _guardrail_key(self, symbol: str, setup: str | None) -> tuple[str, str] | None:
        normalized_symbol = str(symbol).strip().upper()
        normalized_setup = str(setup or "").strip().lower()
        if not normalized_symbol or not normalized_setup:
            return None
        return normalized_symbol, normalized_setup

    def _pattern_veto_reason(self, plan) -> str | None:
        for name in self._matching_pattern_rule_names(self._pattern_vetoes, plan):
            return f"pattern_veto_{self._normalize_rule_name(name)}"
        return None

    def _apply_pattern_watch_hits(self, plan) -> None:
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
        rules: list[PodBPatternRuleConfig],
        plan,
    ) -> list[str]:
        return [rule.name for rule in rules if self._matches_pattern_rule(rule, plan)]

    def _matches_pattern_rule(
        self,
        rule: PodBPatternRuleConfig,
        plan,
    ) -> bool:
        if not rule.enabled:
            return False
        details = dict(plan.setup_details or {})
        if rule.setups and plan.setup not in {item.strip() for item in rule.setups if item.strip()}:
            return False
        if rule.sides and str(plan.side).strip() not in {item.strip() for item in rule.sides if item.strip()}:
            return False
        regime = str(details.get("regime", "")).strip()
        if rule.regimes and regime not in {item.strip() for item in rule.regimes if item.strip()}:
            return False
        if rule.require_strict_continuation_filter is not None:
            if bool(details.get("strict_continuation_filter")) != rule.require_strict_continuation_filter:
                return False
        if not self._matches_float(plan.confidence, rule.min_confidence, rule.max_confidence):
            return False
        if not self._matches_float(
            details.get("compression_score"),
            rule.min_compression_score,
            rule.max_compression_score,
        ):
            return False
        if not self._matches_float(
            details.get("activity_score"),
            rule.min_activity_score,
            rule.max_activity_score,
        ):
            return False
        if not self._matches_float(
            details.get("breakout_score"),
            rule.min_breakout_score,
            rule.max_breakout_score,
        ):
            return False
        volume_ratio = details.get("volume_ratio", details.get("vol_ratio"))
        if not self._matches_float(volume_ratio, rule.min_volume_ratio, rule.max_volume_ratio):
            return False
        if not self._matches_float(
            details.get("bucket_notional_usd"),
            rule.min_bucket_notional_usd,
            rule.max_bucket_notional_usd,
        ):
            return False
        if not self._matches_float(
            details.get("spread_bps"),
            rule.min_spread_bps,
            rule.max_spread_bps,
        ):
            return False
        if not self._matches_float(
            details.get("trade_count_ratio"),
            rule.min_trade_count_ratio,
            rule.max_trade_count_ratio,
        ):
            return False
        if not self._matches_float(
            details.get("liquidity_pull_score"),
            rule.min_liquidity_pull_score,
            rule.max_liquidity_pull_score,
        ):
            return False
        if not self._matches_float(
            details.get("depth_refill_score"),
            rule.min_depth_refill_score,
            rule.max_depth_refill_score,
        ):
            return False
        if not self._matches_float(
            details.get("flow_support_quality"),
            rule.min_flow_support_quality,
            rule.max_flow_support_quality,
        ):
            return False
        if not self._matches_float(
            details.get("vwap_reclaim_quality"),
            rule.min_vwap_reclaim_quality,
            rule.max_vwap_reclaim_quality,
        ):
            return False
        if not self._matches_float(
            details.get("money_flow_quality"),
            rule.min_money_flow_quality,
            rule.max_money_flow_quality,
        ):
            return False
        if not self._matches_float(
            details.get("squeeze_release_quality"),
            rule.min_squeeze_release_quality,
            rule.max_squeeze_release_quality,
        ):
            return False
        return True

    def _matches_float(
        self,
        value: object,
        minimum: float | None,
        maximum: float | None,
    ) -> bool:
        if minimum is None and maximum is None:
            return True
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return False
        if minimum is not None and numeric < minimum:
            return False
        if maximum is not None and numeric > maximum:
            return False
        return True

    def _normalize_rule_name(self, value: str) -> str:
        return "_".join(part for part in str(value).strip().lower().replace("-", "_").split())
