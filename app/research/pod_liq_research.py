from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from app.research.pod_liq_features import PodLiqFeatureBuilder, PodLiqFeatureRow


@dataclass(slots=True)
class PodLiqVariantResult:
    variant: str
    sample_count: int
    expectancy_bps: float
    hit_rate: float
    best_symbol: str | None
    recommendation: str
    rationale: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class PodLiqResearchResult:
    input_path: str
    horizon_bars: int
    best_variant: str | None
    best_symbol: str | None
    recommendation: str
    variants: list[dict[str, object]]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class PodLiqResearchRunner:
    """Research-only evaluator for observables-first liquidation/OI hypotheses."""

    def __init__(self) -> None:
        self.feature_builder = PodLiqFeatureBuilder()

    def run(
        self,
        *,
        input_path: str | Path,
        config_path: str | Path = "config/trident.toml",
        symbols: list[str] | None = None,
        horizon_bars: int = 1,
        output_json: str | Path | None = None,
        output_md: str | Path | None = None,
    ) -> PodLiqResearchResult:
        rows = self.feature_builder.build_rows(
            input_path=input_path,
            config_path=config_path,
            symbols=symbols,
            horizon_bars=horizon_bars,
        )
        variants = [
            self._evaluate_variant(
                rows,
                variant="flow_imbalance_burst",
                min_event_score=0.28,
                max_spread_bps=8.0,
                min_bucket_notional_usd=100.0,
            ),
            self._evaluate_variant(
                rows,
                variant="microstructure_squeeze",
                min_event_score=0.35,
                max_spread_bps=12.0,
                min_bucket_notional_usd=250.0,
            ),
        ]
        best = None
        for variant in variants:
            if best is None or variant.expectancy_bps > best.expectancy_bps:
                best = variant
        result = PodLiqResearchResult(
            input_path=str(input_path),
            horizon_bars=horizon_bars,
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
        rows: list[PodLiqFeatureRow],
        *,
        variant: str,
        min_event_score: float,
        max_spread_bps: float,
        min_bucket_notional_usd: float,
    ) -> PodLiqVariantResult:
        aligned_returns: list[float] = []
        per_symbol_returns: dict[str, list[float]] = {}
        for row in rows:
            if row.future_return_bps is None:
                continue
            if row.event_score < min_event_score:
                continue
            if row.spread_bps > max_spread_bps:
                continue
            if row.bucket_notional_usd < min_bucket_notional_usd:
                continue
            aligned = row.future_return_bps if row.direction == "long" else -row.future_return_bps
            aligned_returns.append(aligned)
            per_symbol_returns.setdefault(row.symbol, []).append(aligned)

        best_symbol = None
        best_symbol_expectancy = float("-inf")
        for symbol, returns in per_symbol_returns.items():
            expectancy = sum(returns) / len(returns)
            if expectancy > best_symbol_expectancy:
                best_symbol = symbol
                best_symbol_expectancy = expectancy

        sample_count = len(aligned_returns)
        expectancy = round(sum(aligned_returns) / sample_count, 4) if sample_count else 0.0
        hit_rate = round(sum(1 for value in aligned_returns if value > 0) / sample_count, 4) if sample_count else 0.0
        recommendation = "kill"
        rationale = "No reproducible observables-first edge."
        if sample_count < 3:
            recommendation = "park"
            rationale = "Not enough event samples yet."
        elif expectancy > 0 and hit_rate >= 0.55:
            recommendation = "go"
            rationale = "Event direction keeps positive expectancy on the filtered sample."
        elif expectancy >= 0:
            recommendation = "park"
            rationale = "Signal is plausible but not robust enough yet."
        return PodLiqVariantResult(
            variant=variant,
            sample_count=sample_count,
            expectancy_bps=expectancy,
            hit_rate=hit_rate,
            best_symbol=best_symbol,
            recommendation=recommendation,
            rationale=rationale,
        )

    def _render_markdown(self, result: PodLiqResearchResult) -> str:
        lines = [
            "# Pod Liq Research Memo",
            "",
            f"- Input path: {result.input_path}",
            f"- Horizon bars: {result.horizon_bars}",
            f"- Recommendation: {result.recommendation}",
            f"- Best variant: {result.best_variant or '-'}",
            f"- Best symbol: {result.best_symbol or '-'}",
            "",
            "| Variant | Samples | Expectancy (bps) | Hit rate | Best symbol | Recommendation |",
            "|---------|---------|------------------|----------|-------------|----------------|",
        ]
        for variant in result.variants:
            lines.append(
                f"| {variant['variant']} | {variant['sample_count']} | {variant['expectancy_bps']} | "
                f"{variant['hit_rate']} | {variant['best_symbol'] or '-'} | {variant['recommendation']} |"
            )
        lines.append("")
        return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run observables-first liq/OI Hydra research")
    parser.add_argument("--input", required=True)
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--symbols", help="Optional comma-separated list")
    parser.add_argument("--horizon-bars", type=int, default=1)
    parser.add_argument("--output-json")
    parser.add_argument("--output-md")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    symbols = [item.strip().upper() for item in (args.symbols or "").split(",") if item.strip()]
    result = PodLiqResearchRunner().run(
        input_path=args.input,
        config_path=args.config,
        symbols=symbols or None,
        horizon_bars=args.horizon_bars,
        output_json=args.output_json,
        output_md=args.output_md,
    )
    print(f"best_variant={result.best_variant}")
    print(f"best_symbol={result.best_symbol}")
    print(f"recommendation={result.recommendation}")


if __name__ == "__main__":
    main()
