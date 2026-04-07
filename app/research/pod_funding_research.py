from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from app.research.pod_funding_dataset import FundingDatasetBuilder, FundingDatasetRow
from app.settings import load_config


@dataclass(slots=True)
class FundingVariantResult:
    variant: str
    sample_count: int
    symbol_count: int
    best_symbol: str | None
    gross_expectancy_bps: float
    net_expectancy_bps: float
    hit_rate: float
    profit_factor: float
    window_expectancy_bps: list[float]
    recommendation: str
    rationale: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class FundingResearchResult:
    input_path: str
    horizon_bars: int
    funding_threshold_bps: float
    round_trip_cost_bps: float
    best_variant: str | None
    best_symbol: str | None
    recommendation: str
    variants: list[dict[str, object]]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class FundingResearchRunner:
    """Evaluates funding mean-reversion variants without wiring them into live pods."""

    def __init__(self) -> None:
        self.dataset_builder = FundingDatasetBuilder()

    def run(
        self,
        *,
        input_path: str | Path,
        config_path: str | Path = "config/trident.toml",
        symbols: list[str] | None = None,
        funding_threshold_bps: float = 4.0,
        horizon_bars: int = 1,
        output_json: str | Path | None = None,
        output_md: str | Path | None = None,
    ) -> FundingResearchResult:
        config = load_config(config_path)
        round_trip_cost_bps = round(
            (config.trident.execution.dry_run_taker_fee_bps + config.trident.execution.dry_run_slippage_bps)
            * 2.0,
            4,
        )
        rows = self.dataset_builder.build_rows(
            input_path=input_path,
            config_path=config_path,
            symbols=symbols,
            horizons_bars=[horizon_bars],
        )
        variants = [
            self._evaluate_variant(
                rows,
                variant="pure_mean_reversion",
                funding_threshold_bps=funding_threshold_bps,
                horizon_bars=horizon_bars,
                round_trip_cost_bps=round_trip_cost_bps,
            ),
            self._evaluate_variant(
                rows,
                variant="funding_plus_regime",
                funding_threshold_bps=funding_threshold_bps,
                horizon_bars=horizon_bars,
                round_trip_cost_bps=round_trip_cost_bps,
                allowed_regimes={"RangeAuction", "DeadZone"},
            ),
            self._evaluate_variant(
                rows,
                variant="funding_plus_microstructure",
                funding_threshold_bps=funding_threshold_bps,
                horizon_bars=horizon_bars,
                round_trip_cost_bps=round_trip_cost_bps,
                max_spread_bps=6.0,
                min_bucket_notional_usd=100.0,
                min_bucket_trade_count=3,
            ),
        ]

        best = None
        for variant in variants:
            if best is None or variant.net_expectancy_bps > best.net_expectancy_bps:
                best = variant

        result = FundingResearchResult(
            input_path=str(input_path),
            horizon_bars=horizon_bars,
            funding_threshold_bps=funding_threshold_bps,
            round_trip_cost_bps=round_trip_cost_bps,
            best_variant=best.variant if best is not None else None,
            best_symbol=best.best_symbol if best is not None else None,
            recommendation=best.recommendation if best is not None else "park",
            variants=[variant.to_dict() for variant in variants],
        )
        if output_json is not None:
            path = Path(output_json)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
        if output_md is not None:
            path = Path(output_md)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self._render_markdown(result), encoding="utf-8")
        return result

    def _evaluate_variant(
        self,
        rows: list[FundingDatasetRow],
        *,
        variant: str,
        funding_threshold_bps: float,
        horizon_bars: int,
        round_trip_cost_bps: float,
        allowed_regimes: set[str] | None = None,
        max_spread_bps: float | None = None,
        min_bucket_notional_usd: float = 0.0,
        min_bucket_trade_count: int = 0,
    ) -> FundingVariantResult:
        net_returns: list[float] = []
        gross_returns: list[float] = []
        per_symbol_net: dict[str, list[float]] = {}
        for row in rows:
            future_return = row.future_returns_bps.get(horizon_bars)
            if future_return is None:
                continue
            if abs(row.funding_rate_bps) < funding_threshold_bps:
                continue
            if allowed_regimes is not None and row.regime not in allowed_regimes:
                continue
            if max_spread_bps is not None and row.spread_bps > max_spread_bps:
                continue
            if row.bucket_notional_usd < min_bucket_notional_usd:
                continue
            if row.bucket_trade_count < min_bucket_trade_count:
                continue
            aligned_gross_return = -future_return if row.funding_rate_bps > 0 else future_return
            aligned_net_return = aligned_gross_return - round_trip_cost_bps
            gross_returns.append(aligned_gross_return)
            net_returns.append(aligned_net_return)
            per_symbol_net.setdefault(row.symbol, []).append(aligned_net_return)

        best_symbol = None
        best_symbol_expectancy = float("-inf")
        for symbol, returns in per_symbol_net.items():
            expectancy = sum(returns) / len(returns)
            if expectancy > best_symbol_expectancy:
                best_symbol = symbol
                best_symbol_expectancy = expectancy

        sample_count = len(net_returns)
        net_expectancy = round(sum(net_returns) / sample_count, 4) if sample_count else 0.0
        gross_expectancy = round(sum(gross_returns) / sample_count, 4) if sample_count else 0.0
        hit_rate = round(sum(1 for value in net_returns if value > 0) / sample_count, 4) if sample_count else 0.0
        positive = sum(value for value in net_returns if value > 0)
        negative = abs(sum(value for value in net_returns if value < 0))
        profit_factor = round(positive / negative, 4) if negative > 0 else (999.0 if positive > 0 else 0.0)
        window_expectancy = self._window_expectancy(net_returns)

        recommendation = "kill"
        rationale = "No stable edge after costs."
        if sample_count < 3:
            recommendation = "park"
            rationale = "Not enough samples yet."
        elif net_expectancy > 0 and profit_factor > 1.1 and all(value > 0 for value in window_expectancy):
            recommendation = "go"
            rationale = "Net expectancy remains positive after costs across both sample halves."
        elif gross_expectancy > 0:
            recommendation = "park"
            rationale = "Gross edge exists, but it is not robust enough after costs."

        return FundingVariantResult(
            variant=variant,
            sample_count=sample_count,
            symbol_count=len(per_symbol_net),
            best_symbol=best_symbol,
            gross_expectancy_bps=gross_expectancy,
            net_expectancy_bps=net_expectancy,
            hit_rate=hit_rate,
            profit_factor=profit_factor,
            window_expectancy_bps=window_expectancy,
            recommendation=recommendation,
            rationale=rationale,
        )

    def _window_expectancy(self, values: list[float]) -> list[float]:
        if not values:
            return [0.0, 0.0]
        midpoint = max(len(values) // 2, 1)
        first = values[:midpoint]
        second = values[midpoint:]
        windows = [first, second if second else first]
        return [
            round(sum(window) / len(window), 4) if window else 0.0
            for window in windows
        ]

    def _render_markdown(self, result: FundingResearchResult) -> str:
        lines = [
            "# Pod Funding Research Memo",
            "",
            f"- Input path: {result.input_path}",
            f"- Horizon bars: {result.horizon_bars}",
            f"- Funding threshold bps: {result.funding_threshold_bps}",
            f"- Round-trip cost bps: {result.round_trip_cost_bps}",
            f"- Recommendation: {result.recommendation}",
            f"- Best variant: {result.best_variant or '-'}",
            f"- Best symbol: {result.best_symbol or '-'}",
            "",
            "| Variant | Samples | Best symbol | Gross exp. | Net exp. | Hit rate | Profit factor | Recommendation |",
            "|---------|---------|-------------|------------|----------|----------|---------------|----------------|",
        ]
        for variant in result.variants:
            lines.append(
                f"| {variant['variant']} | {variant['sample_count']} | {variant['best_symbol'] or '-'} | "
                f"{variant['gross_expectancy_bps']} | {variant['net_expectancy_bps']} | "
                f"{variant['hit_rate']} | {variant['profit_factor']} | {variant['recommendation']} |"
            )
        lines.append("")
        return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run funding Hydra research on TRIDENT snapshots")
    parser.add_argument("--input", required=True)
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--symbols", help="Optional comma-separated list")
    parser.add_argument("--funding-threshold-bps", type=float, default=4.0)
    parser.add_argument("--horizon-bars", type=int, default=1)
    parser.add_argument("--output-json")
    parser.add_argument("--output-md")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    symbols = [item.strip().upper() for item in (args.symbols or "").split(",") if item.strip()]
    result = FundingResearchRunner().run(
        input_path=args.input,
        config_path=args.config,
        symbols=symbols or None,
        funding_threshold_bps=args.funding_threshold_bps,
        horizon_bars=args.horizon_bars,
        output_json=args.output_json,
        output_md=args.output_md,
    )
    print(f"best_variant={result.best_variant}")
    print(f"best_symbol={result.best_symbol}")
    print(f"recommendation={result.recommendation}")


if __name__ == "__main__":
    main()
