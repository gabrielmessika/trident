from __future__ import annotations

from app.settings import PodAPatternVetoConfig
from app.risk.plan_gate import TradePlanRiskGate
from app.settings import AppConfig
from app.trident.pod_a.leverage import LeveragePolicy


class PodCRiskGate(TradePlanRiskGate):
    """Pod C reuses the same deterministic trade-plan rules as Pod A."""

    def __init__(self, config: AppConfig) -> None:
        super().__init__(config)
        self._leverage_policy = LeveragePolicy(config.pod_c)
        self._pod_c_min_confidence = config.pod_c.min_confidence
        self._pattern_vetoes = list(config.pod_c.pattern_vetoes)
        self._pattern_watchers = list(config.pod_c.pattern_watchers)
        self._blocked_symbols = {
            symbol.strip().upper() for symbol in config.pod_c.blocked_symbols if symbol.strip()
        }

    def _decision_reason(
        self,
        *,
        plan,
        accepted_count: int,
        seen_symbols: set[str],
    ) -> str:
        if str(plan.symbol).upper() in self._blocked_symbols:
            return "symbol_blocked"
        if plan.confidence < self._pod_c_min_confidence:
            return "confidence_below_min"
        reason = super()._decision_reason(
            plan=plan,
            accepted_count=accepted_count,
            seen_symbols=seen_symbols,
        )
        if reason != "accepted":
            return reason
        pattern_veto = self._pattern_veto_reason(plan)
        if pattern_veto is not None:
            return pattern_veto
        limits = self._config.trident.risk
        min_notional = max(
            limits.min_trade_notional_usd,
            self._config.pod_c.min_notional_usd,
        )
        if plan.target_notional_usd < min_notional:
            return "notional_below_min"
        if plan.margin_usd < self._config.pod_c.min_margin_usd:
            return "margin_below_min"
        global_limit = self._leverage_policy.max_allowed()
        symbol_limit = self._leverage_policy.max_allowed(plan.symbol)
        if plan.effective_leverage > symbol_limit:
            if symbol_limit < global_limit:
                return "leverage_above_asset_limit"
            return "leverage_above_limit"
        if plan.expected_loss_usd > max(plan.risk_budget_usd, 0.0):
            return "risk_budget_exceeded"
        self._apply_pattern_watch_hits(plan)
        return "accepted"

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
        rules: list[PodAPatternVetoConfig],
        plan,
    ) -> list[str]:
        return [rule.name for rule in rules if self._matches_pattern_rule(rule, plan)]

    def _matches_pattern_rule(
        self,
        rule: PodAPatternVetoConfig,
        plan,
    ) -> bool:
        if not rule.enabled:
            return False
        details = dict(plan.setup_details or {})
        if rule.setups and plan.setup not in {item.strip() for item in rule.setups if item.strip()}:
            return False
        if rule.sides and str(plan.side).strip() not in {item.strip() for item in rule.sides if item.strip()}:
            return False
        market_cluster = str(details.get("market_cluster", "")).strip().lower()
        if rule.market_clusters and market_cluster not in {
            item.strip().lower() for item in rule.market_clusters if item.strip()
        }:
            return False
        regime = str(details.get("global_regime", details.get("regime", ""))).strip()
        if rule.regimes and regime not in {item.strip() for item in rule.regimes if item.strip()}:
            return False
        cluster_regime = str(details.get("cluster_regime", "")).strip()
        if rule.cluster_regimes and cluster_regime not in {
            item.strip() for item in rule.cluster_regimes if item.strip()
        }:
            return False
        cluster_strategy = str(details.get("cluster_strategy", "")).strip()
        if rule.cluster_strategies and cluster_strategy not in {
            item.strip() for item in rule.cluster_strategies if item.strip()
        }:
            return False
        if rule.trend_buckets and str(details.get("trend_bucket", "")).strip() not in {
            item.strip() for item in rule.trend_buckets if item.strip()
        }:
            return False
        if rule.structure_buckets and str(details.get("structure_bucket", "")).strip() not in {
            item.strip() for item in rule.structure_buckets if item.strip()
        }:
            return False
        if rule.vwap_buckets and str(details.get("vwap_bucket", "")).strip() not in {
            item.strip() for item in rule.vwap_buckets if item.strip()
        }:
            return False
        if rule.activity_buckets and str(details.get("activity_bucket", "")).strip() not in {
            item.strip() for item in rule.activity_buckets if item.strip()
        }:
            return False
        if rule.trade_count_buckets and str(details.get("trade_count_bucket", "")).strip() not in {
            item.strip() for item in rule.trade_count_buckets if item.strip()
        }:
            return False
        if rule.flow_buckets and str(details.get("flow_bucket", "")).strip() not in {
            item.strip() for item in rule.flow_buckets if item.strip()
        }:
            return False
        if rule.flow_alignments and str(details.get("flow_alignment", "")).strip() not in {
            item.strip() for item in rule.flow_alignments if item.strip()
        }:
            return False
        if not self._matches_float(details.get("trend_bps"), rule.min_trend_bps, rule.max_trend_bps):
            return False
        if not self._matches_float(
            details.get("structure_score"),
            rule.min_structure_score,
            rule.max_structure_score,
        ):
            return False
        if not self._matches_float(
            details.get("vwap_distance_bps"),
            rule.min_vwap_distance_bps,
            rule.max_vwap_distance_bps,
        ):
            return False
        if not self._matches_float(
            details.get("activity_ratio"),
            rule.min_activity_ratio,
            rule.max_activity_ratio,
        ):
            return False
        if not self._matches_float(
            details.get("trade_count_ratio"),
            rule.min_trade_count_ratio,
            rule.max_trade_count_ratio,
        ):
            return False
        if not self._matches_float(
            details.get("flow_support_score"),
            rule.min_flow_support_score,
            rule.max_flow_support_score,
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
