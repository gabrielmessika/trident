from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from app.research.pod_liq_features import PodLiqFeatureBuilder, PodLiqFeatureRow


@dataclass(frozen=True, slots=True)
class _VariantDefinition:
    variant: str
    description: str
    score_field: str
    direction_field: str
    min_score: float
    max_spread_bps: float
    min_bucket_notional_usd: float
    method_note: str = ""


@dataclass(slots=True)
class PodLiqVariantResult:
    variant: str
    description: str
    sample_count: int
    expectancy_bps: float
    hit_rate: float
    average_score: float
    best_symbol: str | None
    best_regime: str | None
    decision: str
    recommendation: str
    rationale: str
    method_note: str = ""

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
    """Research-only evaluator for observables-first microstructure hypotheses."""

    VARIANTS = [
        _VariantDefinition(
            variant="liquidity_pull_continuation",
            description="Continuation after one-sided depth withdrawal and widening spread.",
            score_field="liquidity_pull_score",
            direction_field="liquidity_pull_direction",
            min_score=0.58,
            max_spread_bps=6.0,
            min_bucket_notional_usd=150.0,
        ),
        _VariantDefinition(
            variant="depth_refill_continuation",
            description="Continuation after supportive refill / depth recovery on the dominant side.",
            score_field="depth_refill_score",
            direction_field="depth_refill_direction",
            min_score=0.60,
            max_spread_bps=5.0,
            min_bucket_notional_usd=150.0,
            method_note="Current comparable dataset only supports the 10bps depth proxy, not full 2/5/10bps ladders.",
        ),
        _VariantDefinition(
            variant="absorption_reversal",
            description="Reversal after aggressive flow prints but price barely moves.",
            score_field="absorption_score",
            direction_field="absorption_direction",
            min_score=0.62,
            max_spread_bps=6.0,
            min_bucket_notional_usd=250.0,
        ),
        _VariantDefinition(
            variant="exhaustion_reversal",
            description="Reversal after impulse fatigue: activity stays high while flow/book decelerate.",
            score_field="exhaustion_score",
            direction_field="exhaustion_direction",
            min_score=0.60,
            max_spread_bps=8.0,
            min_bucket_notional_usd=150.0,
        ),
    ]

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
            self._evaluate_variant(rows, definition=definition)
            for definition in self.VARIANTS
        ]
        best = max(variants, key=lambda item: item.expectancy_bps, default=None)
        recommendation = "kill"
        if any(variant.decision == "keep" for variant in variants):
            recommendation = "go"
        elif any(variant.decision == "park" for variant in variants):
            recommendation = "park"
        result = PodLiqResearchResult(
            input_path=str(input_path),
            horizon_bars=horizon_bars,
            best_variant=best.variant if best is not None else None,
            best_symbol=best.best_symbol if best is not None else None,
            recommendation=recommendation,
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
        definition: _VariantDefinition,
    ) -> PodLiqVariantResult:
        aligned_returns: list[float] = []
        scores: list[float] = []
        per_symbol_returns: dict[str, list[float]] = {}
        per_regime_returns: dict[str, list[float]] = {}

        for row in rows:
            if row.future_return_bps is None:
                continue
            score = float(getattr(row, definition.score_field, 0.0) or 0.0)
            if score < definition.min_score:
                continue
            if row.spread_bps > definition.max_spread_bps:
                continue
            if row.bucket_notional_usd < definition.min_bucket_notional_usd:
                continue
            direction = str(getattr(row, definition.direction_field, "")).lower()
            if direction not in {"long", "short"}:
                continue

            aligned = row.future_return_bps if direction == "long" else -row.future_return_bps
            aligned_returns.append(aligned)
            scores.append(score)
            per_symbol_returns.setdefault(row.symbol, []).append(aligned)
            per_regime_returns.setdefault(row.regime, []).append(aligned)

        best_symbol = self._best_bucket(per_symbol_returns)
        best_regime = self._best_bucket(per_regime_returns)
        sample_count = len(aligned_returns)
        expectancy = round(sum(aligned_returns) / sample_count, 4) if sample_count else 0.0
        hit_rate = (
            round(sum(1 for value in aligned_returns if value > 0) / sample_count, 4)
            if sample_count
            else 0.0
        )
        average_score = round(sum(scores) / sample_count, 4) if sample_count else 0.0

        decision = "reject"
        recommendation = "kill"
        rationale = "Negative expectancy on comparable snapshots."
        if sample_count < 8:
            decision = "park"
            recommendation = "park"
            rationale = "Too few comparable events to promote or reject decisively."
        elif expectancy >= 1.0 and hit_rate >= 0.53:
            decision = "keep"
            recommendation = "go"
            rationale = "Positive expectancy and usable hit rate on comparable snapshots."
        elif expectancy >= 0.0:
            decision = "park"
            recommendation = "park"
            rationale = "Signal is plausible, but too weak to promote as a real filter yet."

        return PodLiqVariantResult(
            variant=definition.variant,
            description=definition.description,
            sample_count=sample_count,
            expectancy_bps=expectancy,
            hit_rate=hit_rate,
            average_score=average_score,
            best_symbol=best_symbol,
            best_regime=best_regime,
            decision=decision,
            recommendation=recommendation,
            rationale=rationale,
            method_note=definition.method_note,
        )

    def _best_bucket(self, returns_by_key: dict[str, list[float]]) -> str | None:
        best_key = None
        best_expectancy = float("-inf")
        for key, returns in returns_by_key.items():
            if len(returns) < 3:
                continue
            expectancy = sum(returns) / len(returns)
            if expectancy > best_expectancy:
                best_key = key
                best_expectancy = expectancy
        if best_key is not None:
            return best_key
        for key, returns in returns_by_key.items():
            expectancy = sum(returns) / len(returns)
            if expectancy > best_expectancy:
                best_key = key
                best_expectancy = expectancy
        return best_key

    def _render_markdown(self, result: PodLiqResearchResult) -> str:
        decisions = {"keep": [], "park": [], "reject": []}
        for variant in result.variants:
            decisions[str(variant["decision"])].append(variant["variant"])

        lines = [
            "# Pod Liq Microstructure Candidates",
            "",
            f"- Input path: `{result.input_path}`",
            f"- Horizon bars: `{result.horizon_bars}`",
            f"- Recommendation: `{result.recommendation}`",
            f"- Best variant: `{result.best_variant or '-'}`",
            f"- Best symbol: `{result.best_symbol or '-'}`",
            "- Method: merged comparable snapshots only, evaluated one candidate at a time.",
            "",
            "## Decision Summary",
            "",
            f"- Keep: `{', '.join(decisions['keep']) if decisions['keep'] else '-'}`",
            f"- Park: `{', '.join(decisions['park']) if decisions['park'] else '-'}`",
            f"- Reject: `{', '.join(decisions['reject']) if decisions['reject'] else '-'}`",
            "",
            "## Variant Table",
            "",
            "| Variant | Samples | Avg score | Expectancy (bps) | Hit rate | Best symbol | Best regime | Decision |",
            "|---------|---------|-----------|------------------|----------|-------------|-------------|----------|",
        ]
        for variant in result.variants:
            lines.append(
                f"| {variant['variant']} | {variant['sample_count']} | {variant['average_score']} | "
                f"{variant['expectancy_bps']} | {variant['hit_rate']} | {variant['best_symbol'] or '-'} | "
                f"{variant['best_regime'] or '-'} | {variant['decision']} |"
            )

        lines.extend(
            [
                "",
                "## Why Keep Or Not",
                "",
            ]
        )
        for variant in result.variants:
            lines.append(f"### {variant['variant']}")
            lines.append("")
            lines.append(f"- Description: {variant['description']}")
            lines.append(f"- Decision: `{variant['decision']}`")
            lines.append(f"- Samples: `{variant['sample_count']}`")
            lines.append(f"- Expectancy: `{variant['expectancy_bps']} bps`")
            lines.append(f"- Hit rate: `{variant['hit_rate']}`")
            lines.append(f"- Best symbol: `{variant['best_symbol'] or '-'}`")
            lines.append(f"- Best regime: `{variant['best_regime'] or '-'}`")
            lines.append(f"- Rationale: {variant['rationale']}")
            if variant.get("method_note"):
                lines.append(f"- Note: {variant['method_note']}")
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
