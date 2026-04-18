from __future__ import annotations

from app.settings import (
    AppConfig,
    PodACampaignConfig,
    PodASetupRunnerConfig,
    PodAStructuralTargetConfig,
    load_config,
)
from app.trident.pod_a.exits import (
    initial_stop_bps,
    stop_bps_for_signal,
    time_stop_hours_for_cluster,
    smart_exit_policy,
)
from app.trident.pod_a.sizing import PositionSizer
from app.trident.pod_a.signals import AnchorTrendSignal
from app.trident.pod_a.symbol_mode import active_symbol_mode, scale_exit_policy
from app.trident.types import PodAllocation, TradePlan


class AnchorTrendPlanner:
    """Builds executable trade plans from Pod A signals and capital limits."""

    def __init__(self, config: AppConfig | None = None) -> None:
        self._config = config or load_config("config/trident.toml")
        self._position_sizer = PositionSizer(self._config)

    def build_trade_plan(
        self,
        signal: AnchorTrendSignal,
        pod_allocation: PodAllocation,
    ) -> TradePlan | None:
        symbol_allocation = next(
            (item for item in pod_allocation.symbols if item.symbol == signal.symbol),
            None,
        )
        if symbol_allocation is None or symbol_allocation.target_usd <= 0:
            return None

        stop_bps = stop_bps_for_signal(
            entry_price=signal.entry_price,
            invalidation_price=signal.invalidation_price,
            side=signal.side,
            fallback_bps=initial_stop_bps(signal.confidence),
        )
        symbol_mode = active_symbol_mode(self._config.pod_a, signal.symbol)
        campaign = None if symbol_mode is not None else self._campaign_for_signal(signal)
        if campaign is not None:
            stop_bps = max(
                stop_bps * max(campaign.stop_bps_multiplier, 0.0),
                max(campaign.stop_bps_floor, 0.0),
            )
        if symbol_mode is not None:
            stop_bps = max(
                stop_bps * max(symbol_mode.stop_bps_multiplier, 0.0),
                max(symbol_mode.stop_bps_floor, 0.0),
            )
        sized_trade = self._position_sizer.size_from_stop(
            symbol=signal.symbol,
            margin_cap_usd=symbol_allocation.target_usd,
            stop_bps=stop_bps,
        )
        if sized_trade is None:
            return None
        base_target_notional_usd = sized_trade.target_notional_usd
        base_margin_usd = sized_trade.margin_usd
        base_risk_budget_usd = sized_trade.risk_budget_usd
        base_expected_loss_usd = sized_trade.expected_loss_usd
        exit_policy = smart_exit_policy(
            signal.setup,
            stop_bps,
            signal.confidence,
            signal.market_cluster,
        )
        setup_runner = self._setup_runner_for_signal(signal)
        if setup_runner is not None:
            exit_policy = self._setup_runner_exit_policy(stop_bps, setup_runner)
        if campaign is not None:
            exit_policy = self._scale_exit_policy(
                exit_policy,
                take_profit_multiplier=campaign.take_profit_multiplier,
                break_even_multiplier=campaign.break_even_multiplier,
                trailing_activation_multiplier=campaign.trailing_activation_multiplier,
                trailing_distance_multiplier=campaign.trailing_distance_multiplier,
            )
        if symbol_mode is not None:
            exit_policy = scale_exit_policy(exit_policy, symbol_mode)
        structural_target = self._structural_take_profit(signal)
        structural_take_profit_bps = 0.0
        structural_target_source = ""
        structural_target_level = 0.0
        if structural_target is not None:
            (
                structural_take_profit_bps,
                structural_target_source,
                structural_target_level,
            ) = structural_target
            if exit_policy["take_profit_bps"] <= 0.0:
                exit_policy["take_profit_bps"] = structural_take_profit_bps
            else:
                exit_policy["take_profit_bps"] = min(
                    exit_policy["take_profit_bps"],
                    structural_take_profit_bps,
                )
        time_stop_hours = (
            symbol_mode.time_stop_hours
            if symbol_mode is not None
            else (
                campaign.time_stop_hours
                if campaign is not None
                else time_stop_hours_for_cluster(signal.market_cluster)
            )
        )
        campaign_initial_entry_fraction = 1.0
        campaign_add_on_enabled = False
        campaign_add_on_fraction = 0.0
        campaign_add_on_trigger_bps = 0.0
        campaign_add_on_min_confidence = 0.0
        campaign_max_add_ons = 0
        if campaign is not None and campaign.add_on_enabled:
            initial_fraction = float(campaign.initial_entry_fraction)
            if 0.0 < initial_fraction < 1.0:
                add_on_fraction = min(
                    max(float(campaign.add_on_fraction), 0.0),
                    max(1.0 - initial_fraction, 0.0),
                )
                if add_on_fraction > 0.0 and campaign.max_add_ons_per_position > 0:
                    campaign_initial_entry_fraction = initial_fraction
                    campaign_add_on_enabled = True
                    campaign_add_on_fraction = add_on_fraction
                    campaign_add_on_trigger_bps = max(float(campaign.add_on_trigger_bps), 0.0)
                    campaign_add_on_min_confidence = max(
                        float(campaign.add_on_min_confidence),
                        0.0,
                    )
                    campaign_max_add_ons = max(int(campaign.max_add_ons_per_position), 0)
        target_notional_usd = round(
            base_target_notional_usd * campaign_initial_entry_fraction,
            6,
        )
        margin_usd = round(base_margin_usd * campaign_initial_entry_fraction, 6)
        risk_budget_usd = round(
            base_risk_budget_usd * campaign_initial_entry_fraction,
            6,
        )
        expected_loss_usd = round(
            base_expected_loss_usd * campaign_initial_entry_fraction,
            6,
        )
        return TradePlan(
            symbol=signal.symbol,
            side=signal.side,
            setup=signal.setup,
            confidence=signal.confidence,
            target_notional_usd=target_notional_usd,
            stop_bps=stop_bps,
            time_stop_hours=time_stop_hours,
            take_profit_bps=exit_policy["take_profit_bps"],
            break_even_trigger_bps=exit_policy["break_even_trigger_bps"],
            trailing_activation_bps=exit_policy["trailing_activation_bps"],
            trailing_distance_bps=exit_policy["trailing_distance_bps"],
            reentry_cooldown_minutes=(
                campaign.reentry_cooldown_minutes if campaign is not None else 0
            ),
            confidence_components=signal.confidence_components,
            margin_usd=margin_usd,
            requested_leverage=sized_trade.requested_leverage,
            effective_leverage=sized_trade.effective_leverage,
            risk_budget_usd=risk_budget_usd,
            expected_loss_usd=expected_loss_usd,
            invalidation_price=signal.invalidation_price,
            isolated=self._config.pod_a.prefer_isolated,
            setup_details={
                **signal.setup_details,
                "structural_target_active": structural_target is not None,
                "structural_target_bps": structural_take_profit_bps,
                "structural_target_source": structural_target_source,
                "structural_target_level": structural_target_level,
                "market_cluster": signal.market_cluster,
                "cluster_leader": signal.cluster_leader,
                "campaign_mode_active": campaign is not None,
                "routing_revoke_exempt": campaign is not None,
                "campaign_base_target_notional_usd": round(base_target_notional_usd, 6),
                "campaign_base_margin_usd": round(base_margin_usd, 6),
                "campaign_base_risk_budget_usd": round(base_risk_budget_usd, 6),
                "campaign_base_expected_loss_usd": round(base_expected_loss_usd, 6),
                "campaign_initial_entry_fraction": round(campaign_initial_entry_fraction, 4),
                "campaign_add_on_enabled": campaign_add_on_enabled,
                "campaign_add_on_fraction": round(campaign_add_on_fraction, 4),
                "campaign_add_on_trigger_bps": round(campaign_add_on_trigger_bps, 4),
                "campaign_add_on_min_confidence": round(campaign_add_on_min_confidence, 4),
                "campaign_max_add_ons": campaign_max_add_ons,
                "campaign_add_on_count": 0,
                "setup_runner_active": setup_runner is not None,
                "special_symbol_mode_active": symbol_mode is not None,
            },
        )

    def _campaign_for_signal(
        self,
        signal: AnchorTrendSignal,
    ) -> PodACampaignConfig | None:
        campaign = self._config.pod_a.campaign
        if not campaign.enabled:
            return None
        if signal.market_cluster != "crypto":
            return None
        if campaign.setups and signal.setup not in campaign.setups:
            return None
        if signal.confidence < campaign.min_confidence:
            return None
        signal_regime = str(signal.setup_details.get("regime", "")).strip()
        if campaign.allowed_regimes and signal_regime not in campaign.allowed_regimes:
            return None
        if campaign.symbol_allowlist and signal.symbol.upper() not in {
            symbol.upper() for symbol in campaign.symbol_allowlist
        }:
            return None
        if campaign.only_cluster_leaders and signal.symbol != signal.cluster_leader:
            return None
        candles_ready = bool(signal.setup_details.get("candles_ready"))
        if campaign.require_candles_ready and not candles_ready:
            return None
        structure_score = abs(float(signal.setup_details.get("structure_score", 0.0) or 0.0))
        if structure_score < campaign.min_structure_score:
            return None
        ichimoku_bias_score = float(signal.setup_details.get("ichimoku_bias_score", 0.0) or 0.0)
        if ichimoku_bias_score < campaign.min_ichimoku_bias_score:
            return None
        stoch_rsi_k = float(signal.setup_details.get("stoch_rsi_k", 0.5) or 0.5)
        if stoch_rsi_k > campaign.max_stoch_rsi_k:
            return None
        cci20 = float(signal.setup_details.get("cci20", 0.0) or 0.0)
        if cci20 > campaign.max_cci20:
            return None
        return campaign

    def _setup_runner_for_signal(
        self,
        signal: AnchorTrendSignal,
    ) -> PodASetupRunnerConfig | None:
        runner = self._config.pod_a.setup_runner
        if not runner.enabled:
            return None
        if runner.setups and signal.setup not in runner.setups:
            return None
        if (
            runner.allowed_market_clusters
            and signal.market_cluster not in runner.allowed_market_clusters
        ):
            return None
        if signal.confidence < runner.min_confidence:
            return None
        return runner

    def _setup_runner_exit_policy(
        self,
        stop_bps: float,
        runner: PodASetupRunnerConfig,
    ) -> dict[str, float]:
        return {
            "take_profit_bps": round(
                stop_bps * max(runner.take_profit_multiplier, 0.0),
                4,
            ),
            "break_even_trigger_bps": round(
                stop_bps * max(runner.break_even_multiplier, 0.0),
                4,
            ),
            "trailing_activation_bps": round(
                stop_bps * max(runner.trailing_activation_multiplier, 0.0),
                4,
            ),
            "trailing_distance_bps": round(
                stop_bps * max(runner.trailing_distance_multiplier, 0.0),
                4,
            ),
        }

    def _structural_take_profit(
        self,
        signal: AnchorTrendSignal,
    ) -> tuple[float, str, float] | None:
        config = self._config.pod_a.structural_targets
        if not config.enabled:
            return None
        if config.setups and signal.setup not in config.setups:
            return None
        details = dict(signal.setup_details or {})
        if config.require_structure_ready and not bool(details.get("structure_ready")):
            return None
        candidates: list[tuple[float, str, float]] = []
        if signal.side == "long":
            candidates.extend(
                self._structural_candidates_above_entry(
                    signal.entry_price,
                    details,
                    config,
                    sources=("swing_high_1h", "range_high_1h"),
                )
            )
        elif signal.side == "short":
            candidates.extend(
                self._structural_candidates_below_entry(
                    signal.entry_price,
                    details,
                    config,
                    sources=("swing_low_1h", "range_low_1h"),
                )
            )
        if not candidates:
            return None
        return min(candidates, key=lambda item: item[0])

    def _structural_candidates_above_entry(
        self,
        entry_price: float,
        details: dict[str, float | str | bool],
        config: PodAStructuralTargetConfig,
        *,
        sources: tuple[str, ...],
    ) -> list[tuple[float, str, float]]:
        if entry_price <= 0:
            return []
        candidates: list[tuple[float, str, float]] = []
        for source in sources:
            try:
                level = float(details.get(source, 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            if level <= entry_price:
                continue
            raw_target_bps = (level - entry_price) / entry_price * 10_000.0
            target_bps = raw_target_bps - max(config.target_buffer_bps, 0.0)
            if target_bps < config.min_target_bps or target_bps > config.max_target_bps:
                continue
            candidates.append((round(target_bps, 4), source, round(level, 8)))
        return candidates

    def _structural_candidates_below_entry(
        self,
        entry_price: float,
        details: dict[str, float | str | bool],
        config: PodAStructuralTargetConfig,
        *,
        sources: tuple[str, ...],
    ) -> list[tuple[float, str, float]]:
        if entry_price <= 0:
            return []
        candidates: list[tuple[float, str, float]] = []
        for source in sources:
            try:
                level = float(details.get(source, 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            if level <= 0 or level >= entry_price:
                continue
            raw_target_bps = (entry_price - level) / entry_price * 10_000.0
            target_bps = raw_target_bps - max(config.target_buffer_bps, 0.0)
            if target_bps < config.min_target_bps or target_bps > config.max_target_bps:
                continue
            candidates.append((round(target_bps, 4), source, round(level, 8)))
        return candidates

    def _scale_exit_policy(
        self,
        exit_policy: dict[str, float],
        *,
        take_profit_multiplier: float,
        break_even_multiplier: float,
        trailing_activation_multiplier: float,
        trailing_distance_multiplier: float,
    ) -> dict[str, float]:
        return {
            "take_profit_bps": round(
                exit_policy["take_profit_bps"] * max(take_profit_multiplier, 0.0),
                4,
            ),
            "break_even_trigger_bps": round(
                exit_policy["break_even_trigger_bps"] * max(break_even_multiplier, 0.0),
                4,
            ),
            "trailing_activation_bps": round(
                exit_policy["trailing_activation_bps"]
                * max(trailing_activation_multiplier, 0.0),
                4,
            ),
            "trailing_distance_bps": round(
                exit_policy["trailing_distance_bps"]
                * max(trailing_distance_multiplier, 0.0),
                4,
            ),
        }
