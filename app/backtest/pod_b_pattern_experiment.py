from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, replace
from pathlib import Path

from app.backtest.pod_b_runner import PodBBacktestRunner
from app.settings import AppConfig, load_config
from app.trident.pod_b import BreakoutContext, BreakoutService
from app.trident.pod_b.signals import BreakoutSignal


@dataclass(slots=True)
class ScenarioResult:
    name: str
    summary: dict[str, object]
    backtest: dict[str, object]


class ExperimentalBreakoutService(BreakoutService):
    """Backtest-only Pod B variants for evaluating continuation patterns."""

    def __init__(self, config: AppConfig, *, scenario: str) -> None:
        super().__init__(config)
        self._scenario = scenario

    def evaluate(self, context: BreakoutContext) -> BreakoutSignal | None:
        base_signal = super().evaluate(context)
        if base_signal is not None and self._scenario in {"continuation_filter", "strict_continuation_filter"}:
            if not self._passes_signal_filter(context):
                base_signal = None
        if base_signal is not None and self._scenario in {"confidence_boost", "combined"}:
            base_signal = self._boost_signal_if_needed(base_signal, context)
        continuation_signal = None
        if self._scenario in {"expansion_continuation", "combined"}:
            continuation_signal = self._expansion_continuation(context)
        candidates = [signal for signal in (base_signal, continuation_signal) if signal is not None]
        if not candidates:
            return None
        best = max(candidates, key=lambda item: item.confidence)
        if best.confidence < self._config.pod_b.bis_min_confidence:
            return None
        return best

    def _boost_signal_if_needed(
        self,
        signal: BreakoutSignal,
        context: BreakoutContext,
    ) -> BreakoutSignal:
        if signal.setup != "vol_expansion_long":
            return signal
        if not self._matches_continuation_pattern(context):
            return signal
        bonus = self._continuation_bonus(context)
        boosted = min(0.99, signal.confidence + bonus)
        return BreakoutSignal(
            symbol=signal.symbol,
            side=signal.side,
            setup=signal.setup,
            confidence=round(boosted, 3),
            entry_price=signal.entry_price,
            stop_bps_hint=signal.stop_bps_hint,
            market_cluster=signal.market_cluster,
            cluster_leader=signal.cluster_leader,
            setup_details={
                **signal.setup_details,
                "continuation_pattern": True,
                "continuation_confidence_bonus": round(bonus, 4),
            },
            confidence_components={
                **signal.confidence_components,
                "continuation_bonus": round(bonus, 4),
            },
        )

    def _passes_signal_filter(self, context: BreakoutContext) -> bool:
        if self._scenario == "continuation_filter":
            return self._matches_continuation_pattern(context)
        if self._scenario == "strict_continuation_filter":
            return self._matches_strict_continuation_pattern(context)
        return True

    def _expansion_continuation(
        self,
        context: BreakoutContext,
    ) -> BreakoutSignal | None:
        if not self._passes_continuation_filters(context):
            return None
        if not self._matches_continuation_pattern(context):
            return None
        activity_score = self._activity_score(context)
        breakout_score = self._breakout_score(context)
        vol_ratio = self._vol_ratio(context)
        if breakout_score < 0.34:
            return None
        if activity_score < 0.42:
            return None
        components = self._confidence_components(
            context=context,
            activity_score=activity_score,
            breakout_score=breakout_score,
            setup_bonus=0.09,
        )
        components["vol_expansion_quality"] = round(_clamp(vol_ratio / 2.0), 4)
        components["continuation_quality"] = round(
            _clamp(
                min(context.bucket_range_bps / 45.0, 1.0) * 0.35
                + min(context.realized_vol_short_bps / 3.0, 1.0) * 0.25
                + min(max(context.trade_flow_bias, 0.0) / 0.10, 1.0) * 0.20
                + min(max(context.vwap_distance_bps, 0.0) / 6.0, 1.0) * 0.20
            ),
            4,
        )
        confidence = min(
            0.99,
            self._aggregate_confidence(components)
            + components["continuation_quality"] * 0.16
            + self._continuation_bonus(context) * 0.5,
        )
        return BreakoutSignal(
            symbol=context.symbol,
            side="long",
            setup="expansion_continuation_long",
            confidence=round(confidence, 3),
            entry_price=context.price,
            stop_bps_hint=self._expansion_stop_bps(context),
            market_cluster=context.market_cluster,
            cluster_leader=context.cluster_leader,
            setup_details={
                "pattern": "expansion_continuation",
                "bucket_range_bps": round(context.bucket_range_bps, 4),
                "realized_vol_short_bps": round(context.realized_vol_short_bps, 4),
                "spread_bps": round(context.spread_bps, 4),
                "structure_score": round(context.structure_score, 4),
                "vwap_distance_bps": round(context.vwap_distance_bps, 4),
                "trade_flow_bias": round(context.trade_flow_bias, 4),
                "book_imbalance": round(context.book_imbalance, 4),
                "regime": context.regime,
            },
            confidence_components=components,
        )

    def _passes_continuation_filters(self, context: BreakoutContext) -> bool:
        allowed_regimes = set(self._config.pod_b.bis_allowed_regimes)
        if allowed_regimes and context.regime not in allowed_regimes:
            return False
        if str(context.market_cluster).strip().lower() != "crypto":
            return False
        if not context.btc_aligned:
            return False
        if context.price <= 0:
            return False
        if context.bucket_notional_usd < self._config.pod_b.bis_min_bucket_notional_usd:
            return False
        if context.bucket_trade_count < self._config.pod_b.bis_min_bucket_trade_count:
            return False
        if abs(context.vwap_distance_bps) > self._config.pod_b.bis_max_chase_distance_bps:
            return False
        if context.spread_bps > min(self._config.pod_b.bis_max_spread_bps, 3.0):
            return False
        return True

    def _matches_continuation_pattern(self, context: BreakoutContext) -> bool:
        return (
            context.structure_score >= 0.10
            and context.vwap_distance_bps >= 1.5
            and context.trade_flow_bias >= 0.02
            and context.book_imbalance >= -0.05
            and context.bucket_range_bps >= 25.0
            and context.realized_vol_short_bps >= 1.8
            and context.spread_bps <= 3.0
        )

    def _matches_strict_continuation_pattern(self, context: BreakoutContext) -> bool:
        return (
            context.structure_score >= 0.20
            and context.vwap_distance_bps >= 4.0
            and context.trade_flow_bias >= 0.05
            and context.book_imbalance >= 0.0
            and context.delta_trade_flow_bias >= 0.05
            and context.bucket_range_bps >= 30.0
            and context.realized_vol_short_bps >= 2.2
            and context.spread_bps <= 2.2
        )

    def _continuation_bonus(self, context: BreakoutContext) -> float:
        return min(
            0.08,
            0.02
            + min(max((context.bucket_range_bps - 25.0) / 25.0, 0.0), 1.0) * 0.025
            + min(max((context.realized_vol_short_bps - 1.8) / 2.2, 0.0), 1.0) * 0.015
            + min(max((context.structure_score - 0.10) / 0.25, 0.0), 1.0) * 0.01
            + min(max((context.trade_flow_bias - 0.02) / 0.08, 0.0), 1.0) * 0.01,
        )


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(value, upper))


def summarize_backtest(payload: dict[str, object]) -> dict[str, object]:
    closed_trade_count = int(payload.get("closed_trade_count", 0) or 0)
    win_count = int(payload.get("win_count", 0) or 0)
    loss_count = int(payload.get("loss_count", 0) or 0)
    return {
        "signal_count": int(payload.get("signal_count", 0) or 0),
        "accepted_count": int(payload.get("accepted_count", 0) or 0),
        "opened_count": int(payload.get("opened_count", 0) or 0),
        "closed_trade_count": closed_trade_count,
        "win_rate": round(win_count / closed_trade_count, 4) if closed_trade_count else 0.0,
        "loss_rate": round(loss_count / closed_trade_count, 4) if closed_trade_count else 0.0,
        "realized_pnl_usd": round(float(payload.get("realized_pnl_usd", 0.0) or 0.0), 4),
        "gross_pnl_usd": round(float(payload.get("gross_pnl_usd", 0.0) or 0.0), 4),
        "fees_usd": round(float(payload.get("fees_usd", 0.0) or 0.0), 4),
        "max_drawdown_usd": round(float(payload.get("max_drawdown_usd", 0.0) or 0.0), 4),
        "average_hold_hours": round(float(payload.get("average_hold_hours", 0.0) or 0.0), 4),
        "average_confidence": round(float(payload.get("average_confidence", 0.0) or 0.0), 4),
        "signals_by_setup": dict(payload.get("signals_by_setup", {}) or {}),
        "trades_by_setup": dict(payload.get("trades_by_setup", {}) or {}),
        "pnl_by_setup": dict(payload.get("pnl_by_setup", {}) or {}),
        "pnl_by_date": dict(payload.get("pnl_by_date", {}) or {}),
    }


def run_scenarios(
    config: AppConfig,
    input_path: str | Path,
) -> list[ScenarioResult]:
    scenarios = [
        ("baseline", None),
        ("continuation_filter", "continuation_filter"),
        ("strict_continuation_filter", "strict_continuation_filter"),
        ("confidence_boost", "confidence_boost"),
        ("expansion_continuation", "expansion_continuation"),
        ("combined", "combined"),
    ]
    results: list[ScenarioResult] = []
    for name, experimental_mode in scenarios:
        scenario_config = replace(
            config,
            pod_b=replace(
                config.pod_b,
                bis_strict_continuation_filter_enabled=False,
            ),
        )
        runner = PodBBacktestRunner(scenario_config)
        if experimental_mode is not None:
            runner.service = ExperimentalBreakoutService(scenario_config, scenario=experimental_mode)
        backtest = runner.run_jsonl(input_path).backtest
        results.append(
            ScenarioResult(
                name=name,
                summary=summarize_backtest(backtest),
                backtest=backtest,
            )
        )
    return results


def build_payload(results: list[ScenarioResult]) -> dict[str, object]:
    scenarios = {item.name: item.summary for item in results}
    ranked = sorted(
        results,
        key=lambda item: (
            float(item.summary["realized_pnl_usd"]),
            -float(item.summary["max_drawdown_usd"]),
            float(item.summary["win_rate"]),
        ),
        reverse=True,
    )
    return {
        "scenario_count": len(results),
        "best_scenario": ranked[0].name if ranked else None,
        "leaderboard": [
            {
                "name": item.name,
                "realized_pnl_usd": item.summary["realized_pnl_usd"],
                "max_drawdown_usd": item.summary["max_drawdown_usd"],
                "signal_count": item.summary["signal_count"],
                "closed_trade_count": item.summary["closed_trade_count"],
                "win_rate": item.summary["win_rate"],
            }
            for item in ranked
        ],
        "scenarios": scenarios,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare Pod B continuation-pattern backtest variants")
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    results = run_scenarios(load_config(args.config), args.input)
    payload = build_payload(results)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
