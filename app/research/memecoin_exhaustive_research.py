from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from itertools import product
from pathlib import Path

from app.research.memecoin_concept_research import MemecoinConceptResearchRunner
from app.research.pod_liq_features import PodLiqFeatureRow


@dataclass(frozen=True, slots=True)
class _TriggerSpec:
    trigger_kind: str
    description: str


@dataclass(slots=True)
class _Metrics:
    sample_count: int
    expectancy_bps: float
    hit_rate: float
    average_interest_score: float
    best_symbol: str | None
    best_regime: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class CandidateSelection:
    trigger_kind: str
    description: str
    horizon_bars: int
    regime_preset: str
    top_n: int
    min_interest_score: float
    max_spread_bps: float
    min_bucket_notional_usd: float
    train: dict[str, object]
    validation: dict[str, object]
    full: dict[str, object]
    decision: str
    recommendation: str
    rationale: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class TriggerSummary:
    trigger_kind: str
    final_decision: str
    recommendation: str
    rationale: str
    selected: dict[str, object] | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class MemecoinExhaustiveResearchResult:
    input_path: str
    config_path: str
    train_end_date: str
    validation_start_date: str
    horizon_bars: list[int]
    max_bar_gap_seconds: int
    parameter_grid: dict[str, object]
    notes: list[str]
    trigger_summaries: list[dict[str, object]]
    selected_candidates: list[dict[str, object]]
    all_sweeps: list[dict[str, object]]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class MemecoinExhaustiveResearchRunner:
    """Exhaustive sweep for the memecoin ranking + trigger concept from toto.md."""

    HORIZONS = [1, 3, 5]
    TOP_N_VALUES = [3, 5, 10]
    MIN_INTEREST_VALUES = [0.45, 0.55, 0.65]
    MAX_SPREAD_VALUES = [3.0, 5.0]
    MIN_NOTIONAL_VALUES = [50.0, 100.0, 250.0]
    REGIME_PRESETS = {
        "all": set(),
        "trend_panic": {"TrendExpansion", "PanicSqueeze"},
        "range_dead": {"RangeAuction", "DeadZone"},
        "dead_only": {"DeadZone"},
        "panic_only": {"PanicSqueeze"},
    }
    MIN_TRAIN_SAMPLES = 40
    MIN_VALIDATION_SAMPLES = 20
    TRIGGERS = [
        _TriggerSpec(
            trigger_kind="event_momentum",
            description="Acceleration on ranked emergents after event-like flow and depth expansion.",
        ),
        _TriggerSpec(
            trigger_kind="flow_following",
            description="Persistent buy flow and aligned book pressure inside the ranked shortlist.",
        ),
        _TriggerSpec(
            trigger_kind="pullback_reclaim",
            description="Continuation after the first spike cools down but ranked demand stays supportive.",
        ),
    ]

    def __init__(self) -> None:
        self.concept_runner = MemecoinConceptResearchRunner()

    def run(
        self,
        *,
        input_path: str | Path,
        config_path: str | Path = "config/trident.toml",
        symbols: list[str] | None = None,
        train_end_date: str,
        validation_start_date: str,
        horizons: list[int] | None = None,
        max_bar_gap_seconds: int = 180,
        output_json: str | Path | None = None,
        output_md: str | Path | None = None,
    ) -> MemecoinExhaustiveResearchResult:
        chosen_horizons = list(horizons or self.HORIZONS)
        selected_candidates: list[CandidateSelection] = []
        all_sweeps: list[dict[str, object]] = []

        for horizon in chosen_horizons:
            rows = self.concept_runner.feature_builder.build_rows(
                input_path=input_path,
                config_path=config_path,
                symbols=symbols,
                horizon_bars=horizon,
                max_bar_gap_seconds=max_bar_gap_seconds,
            )
            crypto_rows = [row for row in rows if ":" not in row.symbol]
            ranked_rows = self.concept_runner._rank_rows_by_timestamp(crypto_rows)
            train_rows = self._filter_ranked_rows(ranked_rows, end_date=train_end_date)
            validation_rows = self._filter_ranked_rows(ranked_rows, start_date=validation_start_date)

            for trigger in self.TRIGGERS:
                best_candidate: CandidateSelection | None = None
                best_train_key: tuple[float, float, int] | None = None

                for regime_name, top_n, min_interest, max_spread, min_notional in product(
                    self.REGIME_PRESETS,
                    self.TOP_N_VALUES,
                    self.MIN_INTEREST_VALUES,
                    self.MAX_SPREAD_VALUES,
                    self.MIN_NOTIONAL_VALUES,
                ):
                    regime_scope = self.REGIME_PRESETS[regime_name]
                    train_metrics = self._evaluate_subset(
                        train_rows,
                        trigger_kind=trigger.trigger_kind,
                        regime_scope=regime_scope,
                        top_n=top_n,
                        min_interest_score=min_interest,
                        max_spread_bps=max_spread,
                        min_bucket_notional_usd=min_notional,
                    )
                    validation_metrics = self._evaluate_subset(
                        validation_rows,
                        trigger_kind=trigger.trigger_kind,
                        regime_scope=regime_scope,
                        top_n=top_n,
                        min_interest_score=min_interest,
                        max_spread_bps=max_spread,
                        min_bucket_notional_usd=min_notional,
                    )
                    full_metrics = self._evaluate_subset(
                        ranked_rows,
                        trigger_kind=trigger.trigger_kind,
                        regime_scope=regime_scope,
                        top_n=top_n,
                        min_interest_score=min_interest,
                        max_spread_bps=max_spread,
                        min_bucket_notional_usd=min_notional,
                    )
                    decision, recommendation, rationale = self._decide_candidate(
                        train_metrics=train_metrics,
                        validation_metrics=validation_metrics,
                    )
                    selection = CandidateSelection(
                        trigger_kind=trigger.trigger_kind,
                        description=trigger.description,
                        horizon_bars=horizon,
                        regime_preset=regime_name,
                        top_n=top_n,
                        min_interest_score=min_interest,
                        max_spread_bps=max_spread,
                        min_bucket_notional_usd=min_notional,
                        train=train_metrics.to_dict(),
                        validation=validation_metrics.to_dict(),
                        full=full_metrics.to_dict(),
                        decision=decision,
                        recommendation=recommendation,
                        rationale=rationale,
                    )
                    selection_dict = selection.to_dict()
                    all_sweeps.append(selection_dict)

                    if train_metrics.sample_count < self.MIN_TRAIN_SAMPLES:
                        continue
                    train_key = (
                        train_metrics.expectancy_bps,
                        train_metrics.hit_rate,
                        train_metrics.sample_count,
                    )
                    if best_train_key is None or train_key > best_train_key:
                        best_train_key = train_key
                        best_candidate = selection

                if best_candidate is None:
                    best_candidate = CandidateSelection(
                        trigger_kind=trigger.trigger_kind,
                        description=trigger.description,
                        horizon_bars=chosen_horizons[0],
                        regime_preset="all",
                        top_n=self.TOP_N_VALUES[0],
                        min_interest_score=self.MIN_INTEREST_VALUES[0],
                        max_spread_bps=self.MAX_SPREAD_VALUES[0],
                        min_bucket_notional_usd=self.MIN_NOTIONAL_VALUES[0],
                        train=_Metrics(0, 0.0, 0.0, 0.0, None, None).to_dict(),
                        validation=_Metrics(0, 0.0, 0.0, 0.0, None, None).to_dict(),
                        full=_Metrics(0, 0.0, 0.0, 0.0, None, None).to_dict(),
                        decision="park",
                        recommendation="park",
                        rationale="No sweep produced enough train samples for a fair assessment.",
                    )
                selected_candidates.append(best_candidate)

        trigger_summaries = [
            self._build_trigger_summary(trigger_kind=trigger.trigger_kind, candidates=selected_candidates)
            for trigger in self.TRIGGERS
        ]
        result = MemecoinExhaustiveResearchResult(
            input_path=str(input_path),
            config_path=str(config_path),
            train_end_date=train_end_date,
            validation_start_date=validation_start_date,
            horizon_bars=chosen_horizons,
            max_bar_gap_seconds=max_bar_gap_seconds,
            parameter_grid={
                "top_n_values": self.TOP_N_VALUES,
                "min_interest_values": self.MIN_INTEREST_VALUES,
                "max_spread_values": self.MAX_SPREAD_VALUES,
                "min_notional_values": self.MIN_NOTIONAL_VALUES,
                "regime_presets": sorted(self.REGIME_PRESETS),
            },
            notes=[
                "This sweep remains research-only: it scores signal quality, not fill-aware execution.",
                "Ranking stays separated from the trigger, mirroring the scanner-versus-trade split recommended in toto.md.",
                "If the replay universe lacks actual memecoins, the result should be interpreted as a generic ranked-crypto proxy.",
            ],
            trigger_summaries=[item.to_dict() for item in trigger_summaries],
            selected_candidates=[item.to_dict() for item in selected_candidates],
            all_sweeps=all_sweeps,
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

    def _filter_ranked_rows(
        self,
        ranked_rows: list[object],
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[object]:
        filtered: list[object] = []
        for ranked in ranked_rows:
            timestamp = ranked.row.timestamp or ""
            day = timestamp[:10]
            if start_date is not None and day < start_date:
                continue
            if end_date is not None and day > end_date:
                continue
            filtered.append(ranked)
        return filtered

    def _evaluate_subset(
        self,
        ranked_rows: list[object],
        *,
        trigger_kind: str,
        regime_scope: set[str],
        top_n: int,
        min_interest_score: float,
        max_spread_bps: float,
        min_bucket_notional_usd: float,
    ) -> _Metrics:
        returns: list[float] = []
        scores: list[float] = []
        returns_by_symbol: dict[str, list[float]] = {}
        returns_by_regime: dict[str, list[float]] = {}
        for ranked in ranked_rows:
            row: PodLiqFeatureRow = ranked.row
            if row.future_return_bps is None:
                continue
            if ranked.rank > top_n:
                continue
            if regime_scope and row.regime not in regime_scope:
                continue
            if ranked.interest_score < min_interest_score:
                continue
            if row.spread_bps > max_spread_bps:
                continue
            if row.bucket_notional_usd < min_bucket_notional_usd:
                continue
            if not self.concept_runner._matches_trigger(row, trigger_kind):
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
        return _Metrics(
            sample_count=sample_count,
            expectancy_bps=expectancy,
            hit_rate=hit_rate,
            average_interest_score=average_score,
            best_symbol=self.concept_runner._best_bucket(returns_by_symbol),
            best_regime=self.concept_runner._best_bucket(returns_by_regime),
        )

    def _decide_candidate(
        self,
        *,
        train_metrics: _Metrics,
        validation_metrics: _Metrics,
    ) -> tuple[str, str, str]:
        if train_metrics.sample_count < self.MIN_TRAIN_SAMPLES:
            return (
                "park",
                "park",
                "Too few train samples for a meaningful parameter selection.",
            )
        if validation_metrics.sample_count < self.MIN_VALIDATION_SAMPLES:
            return (
                "park",
                "park",
                "Train edge exists, but the holdout window is too small to decide.",
            )
        if validation_metrics.expectancy_bps >= 1.0 and validation_metrics.hit_rate >= 0.52:
            return (
                "keep",
                "go",
                "The holdout edge survives with positive expectancy and enough sample depth.",
            )
        if validation_metrics.expectancy_bps >= 0.0:
            return (
                "park",
                "park",
                "The signal stays plausible on holdout, but not strong enough for a Pod B replacement decision.",
            )
        return (
            "reject",
            "kill",
            "The train winner fails to keep a positive edge on holdout.",
        )

    def _build_trigger_summary(
        self,
        *,
        trigger_kind: str,
        candidates: list[CandidateSelection],
    ) -> TriggerSummary:
        matching = [item for item in candidates if item.trigger_kind == trigger_kind]
        if not matching:
            return TriggerSummary(
                trigger_kind=trigger_kind,
                final_decision="park",
                recommendation="park",
                rationale="No candidate selected for this trigger family.",
                selected=None,
            )
        decision_rank = {"keep": 2, "park": 1, "reject": 0}
        best = max(
            matching,
            key=lambda item: (
                decision_rank.get(item.decision, 0),
                item.validation["sample_count"] >= self.MIN_VALIDATION_SAMPLES,
                item.validation["sample_count"],
                item.validation["expectancy_bps"],
                item.validation["hit_rate"],
            ),
        )
        return TriggerSummary(
            trigger_kind=trigger_kind,
            final_decision=best.decision,
            recommendation=best.recommendation,
            rationale=best.rationale,
            selected=best.to_dict(),
        )

    def _render_markdown(self, result: MemecoinExhaustiveResearchResult) -> str:
        lines = [
            "# Memecoin Exhaustive Research",
            "",
            f"- Input path: `{result.input_path}`",
            f"- Config path: `{result.config_path}`",
            f"- Train end date: `{result.train_end_date}`",
            f"- Validation start date: `{result.validation_start_date}`",
            f"- Horizons: `{','.join(str(item) for item in result.horizon_bars)}`",
            f"- Max bar gap seconds: `{result.max_bar_gap_seconds}`",
            "",
            "## Trigger Summaries",
            "",
            "| Trigger | Decision | Validation expectancy (bps) | Validation hit rate | Validation samples | Horizon | Top N | Regime | Best symbol |",
            "|---------|----------|----------------------------:|--------------------:|-------------------:|--------:|------:|--------|-------------|",
        ]
        for summary in result.trigger_summaries:
            selected = summary["selected"] or {}
            validation = selected.get("validation", {})
            lines.append(
                "| "
                f"{summary['trigger_kind']} | "
                f"{summary['final_decision']} | "
                f"{float(validation.get('expectancy_bps', 0.0)):.4f} | "
                f"{float(validation.get('hit_rate', 0.0)):.4f} | "
                f"{int(validation.get('sample_count', 0))} | "
                f"{int(selected.get('horizon_bars', 0))} | "
                f"{int(selected.get('top_n', 0))} | "
                f"{selected.get('regime_preset', '-')} | "
                f"{validation.get('best_symbol') or '-'} |"
            )
        lines.extend(
            [
                "",
                "## Selected Candidates",
                "",
                "| Trigger | Horizon | Regime | Top N | Min interest | Max spread | Min notional | Train exp (bps) | Validation exp (bps) | Validation hit rate | Decision |",
                "|---------|--------:|--------|------:|-------------:|-----------:|-------------:|----------------:|---------------------:|--------------------:|----------|",
            ]
        )
        for item in result.selected_candidates:
            lines.append(
                "| "
                f"{item['trigger_kind']} | "
                f"{item['horizon_bars']} | "
                f"{item['regime_preset']} | "
                f"{item['top_n']} | "
                f"{item['min_interest_score']:.2f} | "
                f"{item['max_spread_bps']:.1f} | "
                f"{item['min_bucket_notional_usd']:.0f} | "
                f"{item['train']['expectancy_bps']:.4f} | "
                f"{item['validation']['expectancy_bps']:.4f} | "
                f"{item['validation']['hit_rate']:.4f} | "
                f"{item['decision']} |"
            )
        lines.extend(["", "## Notes", ""])
        for note in result.notes:
            lines.append(f"- {note}")
        return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Exhaustive sweep for the memecoin concept described in toto.md.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--symbols", help="Optional comma-separated symbol list")
    parser.add_argument("--train-end-date", required=True)
    parser.add_argument("--validation-start-date", required=True)
    parser.add_argument("--horizons", default="1,3,5")
    parser.add_argument("--max-bar-gap-seconds", type=int, default=180)
    parser.add_argument("--output-json")
    parser.add_argument("--output-md")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    symbols = None
    if args.symbols:
        symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    horizons = [
        int(item.strip())
        for item in args.horizons.split(",")
        if item.strip()
    ]
    result = MemecoinExhaustiveResearchRunner().run(
        input_path=args.input,
        config_path=args.config,
        symbols=symbols,
        train_end_date=args.train_end_date,
        validation_start_date=args.validation_start_date,
        horizons=horizons,
        max_bar_gap_seconds=args.max_bar_gap_seconds,
        output_json=args.output_json,
        output_md=args.output_md,
    )
    best = max(
        result.selected_candidates,
        key=lambda item: (
            item["validation"]["expectancy_bps"],
            item["validation"]["hit_rate"],
            item["validation"]["sample_count"],
        ),
        default=None,
    )
    print(f"best_trigger={best['trigger_kind'] if best else '-'}")
    print(f"best_decision={best['decision'] if best else 'park'}")


if __name__ == "__main__":
    main()
