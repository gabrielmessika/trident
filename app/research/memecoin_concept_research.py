from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from app.research.pod_liq_features import PodLiqFeatureBuilder, PodLiqFeatureRow


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(value, upper))


@dataclass(frozen=True, slots=True)
class _VariantDefinition:
    variant: str
    description: str
    trigger_kind: str
    top_n: int
    min_interest_score: float
    max_spread_bps: float
    min_bucket_notional_usd: float


@dataclass(slots=True)
class _RankedRow:
    row: PodLiqFeatureRow
    interest_score: float
    rank: int


@dataclass(slots=True)
class UniverseSliceResult:
    top_n: int
    sample_count: int
    expectancy_bps: float
    hit_rate: float
    average_interest_score: float
    best_symbol: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class MemecoinVariantResult:
    variant: str
    description: str
    trigger_kind: str
    top_n: int
    sample_count: int
    expectancy_bps: float
    hit_rate: float
    average_interest_score: float
    best_symbol: str | None
    best_regime: str | None
    decision: str
    recommendation: str
    rationale: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class MemecoinConceptResearchResult:
    input_path: str
    horizon_bars: int
    recommendation: str
    best_variant: str | None
    universe_slices: list[dict[str, object]]
    variants: list[dict[str, object]]
    notes: list[str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class MemecoinConceptResearchRunner:
    """Research-only tester for a memecoin-style scanner + ranking + top-N concept."""

    UNIVERSE_TOP_N = [5, 10, 15]
    VARIANTS = [
        _VariantDefinition(
            variant="event_momentum_top_5",
            description="Top-5 ranked emerging names with event acceleration and supportive microstructure.",
            trigger_kind="event_momentum",
            top_n=5,
            min_interest_score=0.55,
            max_spread_bps=5.0,
            min_bucket_notional_usd=100.0,
        ),
        _VariantDefinition(
            variant="event_momentum_top_10",
            description="Top-10 ranked emerging names with event acceleration and supportive microstructure.",
            trigger_kind="event_momentum",
            top_n=10,
            min_interest_score=0.52,
            max_spread_bps=5.0,
            min_bucket_notional_usd=100.0,
        ),
        _VariantDefinition(
            variant="flow_following_top_5",
            description="Top-5 ranked names with persistent buy flow and aligned order book pressure.",
            trigger_kind="flow_following",
            top_n=5,
            min_interest_score=0.53,
            max_spread_bps=4.5,
            min_bucket_notional_usd=125.0,
        ),
        _VariantDefinition(
            variant="flow_following_top_10",
            description="Top-10 ranked names with persistent buy flow and aligned order book pressure.",
            trigger_kind="flow_following",
            top_n=10,
            min_interest_score=0.50,
            max_spread_bps=4.5,
            min_bucket_notional_usd=125.0,
        ),
        _VariantDefinition(
            variant="pullback_reclaim_top_5",
            description="Top-5 ranked names that keep event pressure while pulling back cleanly before continuation.",
            trigger_kind="pullback_reclaim",
            top_n=5,
            min_interest_score=0.50,
            max_spread_bps=4.0,
            min_bucket_notional_usd=100.0,
        ),
        _VariantDefinition(
            variant="pullback_reclaim_top_10",
            description="Top-10 ranked names that keep event pressure while pulling back cleanly before continuation.",
            trigger_kind="pullback_reclaim",
            top_n=10,
            min_interest_score=0.48,
            max_spread_bps=4.0,
            min_bucket_notional_usd=100.0,
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
        horizon_bars: int = 3,
        max_bar_gap_seconds: int = 180,
        output_json: str | Path | None = None,
        output_md: str | Path | None = None,
    ) -> MemecoinConceptResearchResult:
        rows = self.feature_builder.build_rows(
            input_path=input_path,
            config_path=config_path,
            symbols=symbols,
            horizon_bars=horizon_bars,
            max_bar_gap_seconds=max_bar_gap_seconds,
        )
        crypto_rows = [
            row
            for row in rows
            if ":" not in row.symbol
        ]
        ranked_rows = self._rank_rows_by_timestamp(crypto_rows)
        universe_slices = [
            self._evaluate_universe_slice(ranked_rows, top_n=top_n)
            for top_n in self.UNIVERSE_TOP_N
        ]
        variants = [
            self._evaluate_variant(ranked_rows, definition=definition)
            for definition in self.VARIANTS
        ]
        best_variant = max(
            variants,
            key=lambda item: (item.expectancy_bps, item.hit_rate, item.sample_count),
            default=None,
        )
        recommendation = "kill"
        if any(item.decision == "keep" for item in variants):
            recommendation = "go"
        elif any(item.decision == "park" for item in variants):
            recommendation = "park"
        result = MemecoinConceptResearchResult(
            input_path=str(input_path),
            horizon_bars=horizon_bars,
            recommendation=recommendation,
            best_variant=best_variant.variant if best_variant is not None else None,
            universe_slices=[item.to_dict() for item in universe_slices],
            variants=[item.to_dict() for item in variants],
            notes=[
                "Research-only: this measures direction quality on snapshot replay, not full fill-aware execution.",
                "Crypto-only approximation: symbols containing ':' are excluded so tradfi overlays do not pollute the memecoin scan.",
                "The ranking is dynamic per timestamp and separate from the trade trigger, matching the scanner-versus-signal split described in toto.md.",
                f"Feature continuity uses max_bar_gap_seconds={max_bar_gap_seconds}, which can be raised for slower snapshot cadences.",
            ],
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

    def _rank_rows_by_timestamp(
        self,
        rows: list[PodLiqFeatureRow],
    ) -> list[_RankedRow]:
        rows_by_timestamp: dict[str | None, list[PodLiqFeatureRow]] = defaultdict(list)
        for row in rows:
            rows_by_timestamp[row.timestamp].append(row)
        ranked_rows: list[_RankedRow] = []
        for timestamp in sorted(rows_by_timestamp, key=lambda value: value or ""):
            ranked_group = sorted(
                rows_by_timestamp[timestamp],
                key=lambda item: (
                    self._interest_score(item),
                    item.bucket_notional_usd,
                    item.symbol,
                ),
                reverse=True,
            )
            for rank, row in enumerate(ranked_group, start=1):
                ranked_rows.append(
                    _RankedRow(
                        row=row,
                        interest_score=round(self._interest_score(row), 4),
                        rank=rank,
                    )
                )
        return ranked_rows

    def _evaluate_universe_slice(
        self,
        ranked_rows: list[_RankedRow],
        *,
        top_n: int,
    ) -> UniverseSliceResult:
        returns: list[float] = []
        scores: list[float] = []
        returns_by_symbol: dict[str, list[float]] = {}
        for ranked in ranked_rows:
            row = ranked.row
            if ranked.rank > top_n or row.future_return_bps is None:
                continue
            aligned = row.future_return_bps if row.flow_direction == "long" else -row.future_return_bps
            returns.append(aligned)
            scores.append(ranked.interest_score)
            returns_by_symbol.setdefault(row.symbol, []).append(aligned)
        sample_count = len(returns)
        expectancy = round(sum(returns) / sample_count, 4) if sample_count else 0.0
        hit_rate = (
            round(sum(1 for value in returns if value > 0) / sample_count, 4)
            if sample_count
            else 0.0
        )
        average_score = round(sum(scores) / sample_count, 4) if sample_count else 0.0
        return UniverseSliceResult(
            top_n=top_n,
            sample_count=sample_count,
            expectancy_bps=expectancy,
            hit_rate=hit_rate,
            average_interest_score=average_score,
            best_symbol=self._best_bucket(returns_by_symbol),
        )

    def _evaluate_variant(
        self,
        ranked_rows: list[_RankedRow],
        *,
        definition: _VariantDefinition,
    ) -> MemecoinVariantResult:
        returns: list[float] = []
        scores: list[float] = []
        returns_by_symbol: dict[str, list[float]] = {}
        returns_by_regime: dict[str, list[float]] = {}

        for ranked in ranked_rows:
            row = ranked.row
            if row.future_return_bps is None:
                continue
            if ranked.rank > definition.top_n:
                continue
            if ranked.interest_score < definition.min_interest_score:
                continue
            if row.spread_bps > definition.max_spread_bps:
                continue
            if row.bucket_notional_usd < definition.min_bucket_notional_usd:
                continue
            if not self._matches_trigger(row, definition.trigger_kind):
                continue
            returns.append(row.future_return_bps)
            scores.append(ranked.interest_score)
            returns_by_symbol.setdefault(row.symbol, []).append(row.future_return_bps)
            returns_by_regime.setdefault(row.regime, []).append(row.future_return_bps)

        sample_count = len(returns)
        expectancy = round(sum(returns) / sample_count, 4) if sample_count else 0.0
        hit_rate = (
            round(sum(1 for value in returns if value > 0) / sample_count, 4)
            if sample_count
            else 0.0
        )
        average_score = round(sum(scores) / sample_count, 4) if sample_count else 0.0

        decision = "reject"
        recommendation = "kill"
        rationale = "No reliable edge for this memecoin-style trigger on the comparable snapshot stream."
        if sample_count < 10:
            decision = "park"
            recommendation = "park"
            rationale = "Too few comparable events to judge the concept decisively."
        elif expectancy >= 1.0 and hit_rate >= 0.53 and sample_count >= 25:
            decision = "keep"
            recommendation = "go"
            rationale = "Dynamic ranking plus trigger selection keeps a positive edge with enough comparable samples."
        elif expectancy >= 0.0:
            decision = "park"
            recommendation = "park"
            rationale = "Signal is plausible, but not yet strong enough to justify replacing or promoting Pod B."

        return MemecoinVariantResult(
            variant=definition.variant,
            description=definition.description,
            trigger_kind=definition.trigger_kind,
            top_n=definition.top_n,
            sample_count=sample_count,
            expectancy_bps=expectancy,
            hit_rate=hit_rate,
            average_interest_score=average_score,
            best_symbol=self._best_bucket(returns_by_symbol),
            best_regime=self._best_bucket(returns_by_regime),
            decision=decision,
            recommendation=recommendation,
            rationale=rationale,
        )

    def _interest_score(self, row: PodLiqFeatureRow) -> float:
        volume_accel = _clamp((row.volume_ratio - 1.0) / 2.5)
        trade_accel = _clamp((row.trade_count_ratio - 1.0) / 2.5)
        notional_presence = _clamp(math.log10(max(row.bucket_notional_usd, 1.0)) / 4.0)
        volatility = _clamp(row.realized_vol_short_bps / 35.0)
        spread_quality = 1.0 - _clamp(row.spread_bps / 8.0)
        stability = 1.0 - _clamp(row.book_churn_score / 1.0)
        directional_pressure = _clamp(
            abs(row.trade_flow_bias) * 0.60
            + abs(row.book_imbalance) * 0.40
        )
        score = (
            row.event_score * 0.24
            + volume_accel * 0.16
            + trade_accel * 0.12
            + notional_presence * 0.12
            + volatility * 0.10
            + spread_quality * 0.12
            + stability * 0.06
            + directional_pressure * 0.08
        )
        return _clamp(score)

    def _matches_trigger(
        self,
        row: PodLiqFeatureRow,
        trigger_kind: str,
    ) -> bool:
        liquidity_score = max(row.liquidity_pull_score, row.touch_liquidity_pull_score)
        refill_score = max(row.depth_refill_score, row.touch_refill_score)
        if trigger_kind == "event_momentum":
            return (
                row.event_score >= 0.55
                and row.volume_ratio >= 1.40
                and row.trade_count_ratio >= 1.15
                and row.price_move_bps >= 0.25
                and max(liquidity_score, refill_score) >= 0.60
                and row.trade_flow_bias >= 0.05
                and row.book_imbalance >= -0.05
                and row.microprice_dislocation_bps >= 0.0
            )
        if trigger_kind == "flow_following":
            return (
                row.trade_flow_bias >= 0.12
                and row.delta_trade_flow_bias >= 0.05
                and row.book_imbalance >= 0.05
                and row.delta_book_imbalance >= -0.05
                and row.microprice_dislocation_bps >= 0.0
                and max(liquidity_score, row.event_score) >= 0.50
            )
        if trigger_kind == "pullback_reclaim":
            return (
                row.event_score >= 0.42
                and row.volume_ratio >= 1.20
                and row.trade_count_ratio >= 1.05
                and -0.75 <= row.price_move_bps <= 2.50
                and row.trade_flow_bias >= 0.06
                and row.book_imbalance >= -0.02
                and refill_score >= 0.60
                and row.microprice_dislocation_bps >= 0.0
            )
        raise ValueError(f"Unsupported trigger kind: {trigger_kind}")

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

    def _render_markdown(self, result: MemecoinConceptResearchResult) -> str:
        lines = [
            "# Memecoin Concept Research",
            "",
            f"- Input path: `{result.input_path}`",
            f"- Horizon bars: `{result.horizon_bars}`",
            f"- Recommendation: `{result.recommendation}`",
            f"- Best variant: `{result.best_variant or '-'}`",
            "- Method: dynamic per-timestamp ranking, crypto-only rows, then trigger evaluation inside top-N slices.",
            "",
            "## Universe Slices",
            "",
            "| Top N | Samples | Expectancy (bps) | Hit rate | Avg interest score | Best symbol |",
            "|------:|--------:|-----------------:|---------:|-------------------:|-------------|",
        ]
        for item in result.universe_slices:
            lines.append(
                "| "
                f"{item['top_n']} | "
                f"{item['sample_count']} | "
                f"{item['expectancy_bps']:.4f} | "
                f"{item['hit_rate']:.4f} | "
                f"{item['average_interest_score']:.4f} | "
                f"{item['best_symbol'] or '-'} |"
            )
        lines.extend(
            [
                "",
                "## Trigger Variants",
                "",
                "| Variant | Trigger | Top N | Samples | Expectancy (bps) | Hit rate | Avg interest score | Best symbol | Best regime | Decision |",
                "|---------|---------|------:|--------:|-----------------:|---------:|-------------------:|-------------|-------------|----------|",
            ]
        )
        for item in result.variants:
            lines.append(
                "| "
                f"{item['variant']} | "
                f"{item['trigger_kind']} | "
                f"{item['top_n']} | "
                f"{item['sample_count']} | "
                f"{item['expectancy_bps']:.4f} | "
                f"{item['hit_rate']:.4f} | "
                f"{item['average_interest_score']:.4f} | "
                f"{item['best_symbol'] or '-'} | "
                f"{item['best_regime'] or '-'} | "
                f"{item['decision']} |"
            )
        lines.extend(["", "## Notes", ""])
        for note in result.notes:
            lines.append(f"- {note}")
        return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Research-only validation of a memecoin ranking + trigger concept.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--symbols", help="Optional comma-separated symbol list")
    parser.add_argument("--horizon-bars", type=int, default=3)
    parser.add_argument("--max-bar-gap-seconds", type=int, default=180)
    parser.add_argument("--output-json")
    parser.add_argument("--output-md")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    symbols = None
    if args.symbols:
        symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    result = MemecoinConceptResearchRunner().run(
        input_path=args.input,
        config_path=args.config,
        symbols=symbols,
        horizon_bars=args.horizon_bars,
        max_bar_gap_seconds=args.max_bar_gap_seconds,
        output_json=args.output_json,
        output_md=args.output_md,
    )
    print(f"recommendation={result.recommendation}")
    print(f"best_variant={result.best_variant}")


if __name__ == "__main__":
    main()
