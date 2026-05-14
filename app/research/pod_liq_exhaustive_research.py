from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from itertools import product
from pathlib import Path

from app.research.pod_liq_features import PodLiqFeatureBuilder, PodLiqFeatureRow


@dataclass(frozen=True, slots=True)
class _CandidateSpec:
    family: str
    variant: str
    use_case: str
    description: str
    score_field: str
    direction_field: str
    objective: str
    method_note: str = ""


@dataclass(slots=True)
class _Metrics:
    sample_count: int
    expectancy_bps: float
    positive_hit_rate: float
    negative_hit_rate: float
    average_score: float
    best_symbol: str | None
    best_regime: str | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class CandidateSelection:
    family: str
    variant: str
    use_case: str
    objective: str
    description: str
    horizon_bars: int
    regime_preset: str
    min_score: float
    max_spread_bps: float
    min_bucket_notional_usd: float
    train: dict[str, object]
    validation: dict[str, object]
    full: dict[str, object]
    decision: str
    recommendation: str
    rationale: str
    method_note: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class FamilySummary:
    family: str
    final_decision: str
    recommendation: str
    best_variant: str | None
    rationale: str
    selected: dict[str, object] | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class PodLiqExhaustiveResearchResult:
    input_path: str
    config_path: str
    start_date: str | None
    end_date: str | None
    train_end_date: str
    validation_start_date: str
    horizons: list[int]
    parameter_grid: dict[str, object]
    notes: list[str]
    family_summaries: list[dict[str, object]]
    selected_candidates: list[dict[str, object]]
    all_sweeps: list[dict[str, object]]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class PodLiqExhaustiveResearchRunner:
    """Runs exhaustive parameter sweeps for the microstructure families from the active plan."""

    HORIZONS = [1, 3, 5]
    MIN_SCORE_VALUES = [0.55, 0.65, 0.75]
    MAX_SPREAD_VALUES = [3.0, 5.0]
    MIN_NOTIONAL_VALUES = [100.0, 250.0]
    REGIME_PRESETS = {
        "all": set(),
        "trend_panic": {"TrendExpansion", "PanicSqueeze"},
        "range_dead": {"RangeAuction", "DeadZone"},
    }
    MIN_TRAIN_SAMPLES = 200
    MIN_VALIDATION_SAMPLES = 120
    CANDIDATES = [
        _CandidateSpec(
            family="liquidity_pull",
            variant="depth10_continuation",
            use_case="watcher_continuation",
            description="Continuation after one-sided withdrawal in 10bps depth.",
            score_field="liquidity_pull_score",
            direction_field="liquidity_pull_direction",
            objective="positive",
        ),
        _CandidateSpec(
            family="liquidity_pull",
            variant="touch_continuation",
            use_case="watcher_continuation",
            description="Continuation after one-sided withdrawal at the touch using best size velocity.",
            score_field="touch_liquidity_pull_score",
            direction_field="touch_liquidity_pull_direction",
            objective="positive",
            method_note="Uses best bid/ask size as the inner-band proxy because true 2/5bps ladders are absent from the replay snapshots.",
        ),
        _CandidateSpec(
            family="depth_refill",
            variant="depth10_continuation",
            use_case="watcher_continuation",
            description="Continuation after supportive 10bps depth refill on the dominant side.",
            score_field="depth_refill_score",
            direction_field="depth_refill_direction",
            objective="positive",
            method_note="Current replay snapshots expose 10bps depth only.",
        ),
        _CandidateSpec(
            family="depth_refill",
            variant="touch_continuation",
            use_case="watcher_continuation",
            description="Continuation after supportive refill at the touch using best size velocity.",
            score_field="touch_refill_score",
            direction_field="touch_refill_direction",
            objective="positive",
            method_note="Uses best bid/ask size as the inner-band proxy because true 2/5bps ladders are absent from the replay snapshots.",
        ),
        _CandidateSpec(
            family="absorption",
            variant="reversal",
            use_case="reversal",
            description="Reversal after aggressive flow prints but price barely moves.",
            score_field="absorption_score",
            direction_field="absorption_direction",
            objective="positive",
        ),
        _CandidateSpec(
            family="absorption",
            variant="flow_veto",
            use_case="shadow_veto",
            description="Shadow veto when strong absorption predicts failure in the original flow direction.",
            score_field="absorption_score",
            direction_field="flow_direction",
            objective="negative",
        ),
        _CandidateSpec(
            family="exhaustion",
            variant="reversal",
            use_case="reversal",
            description="Reversal after impulse fatigue: activity stays high while flow/book decelerate.",
            score_field="exhaustion_score",
            direction_field="exhaustion_direction",
            objective="positive",
        ),
        _CandidateSpec(
            family="exhaustion",
            variant="impulse_veto",
            use_case="shadow_veto",
            description="Shadow veto when exhaustion predicts continuation failure in the original impulse direction.",
            score_field="exhaustion_score",
            direction_field="impulse_direction",
            objective="negative",
        ),
        _CandidateSpec(
            family="cancel_replace_proxy",
            variant="book_churn_micro_veto",
            use_case="shadow_veto",
            description="Shadow veto from two-sided book churn / instability against the microprice direction.",
            score_field="book_churn_score",
            direction_field="micro_direction",
            objective="negative",
            method_note="Proxy only: no explicit cancel/replace feed is present in the replay snapshots.",
        ),
        _CandidateSpec(
            family="cancel_replace_proxy",
            variant="book_churn_flow_veto",
            use_case="shadow_veto",
            description="Shadow veto from two-sided book churn / instability against the trade-flow direction.",
            score_field="book_churn_score",
            direction_field="flow_direction",
            objective="negative",
            method_note="Proxy only: no explicit cancel/replace feed is present in the replay snapshots.",
        ),
        _CandidateSpec(
            family="trigger_liquidity",
            variant="stop_breakout_continuation",
            use_case="watcher_continuation",
            description="Continuation toward visible Hyperliquid stop clusters.",
            score_field="trigger_stop_breakout_score",
            direction_field="trigger_stop_breakout_direction",
            objective="positive",
            method_note="Requires snapshots enriched with node-derived TP/SL trigger liquidity.",
        ),
        _CandidateSpec(
            family="trigger_liquidity",
            variant="stop_sweep_reversal",
            use_case="reversal",
            description="Reversal after price trades into nearby visible stop clusters.",
            score_field="trigger_sweep_reversal_score",
            direction_field="trigger_sweep_reversal_direction",
            objective="positive",
            method_note="Requires snapshots enriched with node-derived TP/SL trigger liquidity.",
        ),
        _CandidateSpec(
            family="trigger_liquidity",
            variant="tp_cluster_exhaustion",
            use_case="reversal",
            description="Exhaustion/reversal near visible take-profit clusters.",
            score_field="trigger_tp_exhaustion_score",
            direction_field="trigger_tp_exhaustion_direction",
            objective="positive",
            method_note="Requires snapshots enriched with node-derived TP/SL trigger liquidity.",
        ),
        _CandidateSpec(
            family="trigger_liquidity",
            variant="cascade_risk_veto",
            use_case="shadow_veto",
            description="Shadow veto when adverse trigger cascade risk is high against the current flow direction.",
            score_field="trigger_cascade_veto_score",
            direction_field="trigger_cascade_veto_direction",
            objective="negative",
            method_note="Requires snapshots enriched with node-derived TP/SL trigger liquidity.",
        ),
    ]

    def __init__(self) -> None:
        self.feature_builder = PodLiqFeatureBuilder()

    def run(
        self,
        *,
        input_path: str | Path,
        config_path: str | Path = "config/trident.toml",
        start_date: str | None = None,
        end_date: str | None = None,
        train_end_date: str = "2026-04-18",
        validation_start_date: str = "2026-04-20",
        horizons: list[int] | None = None,
        output_json: str | Path | None = None,
        output_md: str | Path | None = None,
    ) -> PodLiqExhaustiveResearchResult:
        chosen_horizons = list(horizons or self.HORIZONS)
        selected_candidates: list[CandidateSelection] = []
        all_sweeps: list[dict[str, object]] = []

        for horizon in chosen_horizons:
            rows = self.feature_builder.build_rows(
                input_path=input_path,
                config_path=config_path,
                horizon_bars=horizon,
            )
            rows = self._filter_rows_by_date(
                rows,
                start_date=start_date,
                end_date=end_date,
            )
            for spec in self.CANDIDATES:
                selection, sweeps = self._sweep_spec(
                    rows=rows,
                    spec=spec,
                    horizon_bars=horizon,
                    train_end_date=train_end_date,
                    validation_start_date=validation_start_date,
                )
                selected_candidates.append(selection)
                all_sweeps.extend(sweeps)

        family_summaries = [
            self._summarize_family(family, selected_candidates).to_dict()
            for family in sorted({spec.family for spec in self.CANDIDATES})
        ]
        notes = [
            "Replay coverage uses only snapshot records whose timestamps fall inside the requested date window.",
            "Rows crossing large timestamp gaps are ignored by the feature builder so horizons 1/3/5 remain local.",
            "True 2/5/10bps depth ladders are not present in the replay snapshots; best bid/ask size is used as the inner-band proxy.",
            "Hyperliquid user feeds (`orderUpdates`, `userFills`, `openOrders`, `clearinghouseState`) are not present in this replay input, so those infrastructure ideas cannot be replay-validated here.",
            "Trigger-liquidity candidates only activate when snapshots have been enriched from node-derived TP/SL order statuses.",
        ]
        result = PodLiqExhaustiveResearchResult(
            input_path=str(input_path),
            config_path=str(config_path),
            start_date=start_date,
            end_date=end_date,
            train_end_date=train_end_date,
            validation_start_date=validation_start_date,
            horizons=chosen_horizons,
            parameter_grid={
                "min_score_values": self.MIN_SCORE_VALUES,
                "max_spread_values": self.MAX_SPREAD_VALUES,
                "min_notional_values": self.MIN_NOTIONAL_VALUES,
                "regime_presets": {
                    name: sorted(values)
                    for name, values in self.REGIME_PRESETS.items()
                },
                "min_train_samples": self.MIN_TRAIN_SAMPLES,
                "min_validation_samples": self.MIN_VALIDATION_SAMPLES,
            },
            notes=notes,
            family_summaries=family_summaries,
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

    def _sweep_spec(
        self,
        *,
        rows: list[PodLiqFeatureRow],
        spec: _CandidateSpec,
        horizon_bars: int,
        train_end_date: str,
        validation_start_date: str,
    ) -> tuple[CandidateSelection, list[dict[str, object]]]:
        prefiltered_rows = [
            row
            for row in rows
            if (getattr(row, spec.score_field, 0.0) or 0.0) >= self.MIN_SCORE_VALUES[0]
            and row.spread_bps <= max(self.MAX_SPREAD_VALUES)
            and row.bucket_notional_usd >= min(self.MIN_NOTIONAL_VALUES)
        ]
        train_rows = [
            row for row in prefiltered_rows if self._row_date(row) is not None and self._row_date(row) <= train_end_date
        ]
        validation_rows = [
            row
            for row in prefiltered_rows
            if self._row_date(row) is not None and self._row_date(row) >= validation_start_date
        ]

        sweep_records: list[dict[str, object]] = []
        best_payload: dict[str, object] | None = None
        best_rank: tuple[float, float, int] | None = None

        for min_score, max_spread, min_notional, regime_name in product(
            self.MIN_SCORE_VALUES,
            self.MAX_SPREAD_VALUES,
            self.MIN_NOTIONAL_VALUES,
            self.REGIME_PRESETS.keys(),
        ):
            allowed_regimes = self.REGIME_PRESETS[regime_name]
            train_metrics = self._evaluate_rows(
                rows=train_rows,
                spec=spec,
                min_score=min_score,
                max_spread_bps=max_spread,
                min_bucket_notional_usd=min_notional,
                allowed_regimes=allowed_regimes,
            )
            validation_metrics = self._evaluate_rows(
                rows=validation_rows,
                spec=spec,
                min_score=min_score,
                max_spread_bps=max_spread,
                min_bucket_notional_usd=min_notional,
                allowed_regimes=allowed_regimes,
            )
            full_metrics = self._evaluate_rows(
                rows=prefiltered_rows,
                spec=spec,
                min_score=min_score,
                max_spread_bps=max_spread,
                min_bucket_notional_usd=min_notional,
                allowed_regimes=allowed_regimes,
            )
            payload = {
                "family": spec.family,
                "variant": spec.variant,
                "use_case": spec.use_case,
                "objective": spec.objective,
                "horizon_bars": horizon_bars,
                "regime_preset": regime_name,
                "min_score": round(min_score, 4),
                "max_spread_bps": round(max_spread, 4),
                "min_bucket_notional_usd": round(min_notional, 4),
                "train": train_metrics.to_dict(),
                "validation": validation_metrics.to_dict(),
                "full": full_metrics.to_dict(),
            }
            sweep_records.append(payload)
            rank = self._rank_metrics(train_metrics, objective=spec.objective)
            if (
                train_metrics.sample_count >= self.MIN_TRAIN_SAMPLES
                and self._training_sign_matches(train_metrics, objective=spec.objective)
                and (best_rank is None or rank > best_rank)
            ):
                best_rank = rank
                best_payload = payload

        if best_payload is None:
            fallback_payloads = sorted(
                sweep_records,
                key=lambda item: (
                    item["train"]["sample_count"],
                    self._rank_metrics_from_payload(item["train"], objective=spec.objective),
                ),
                reverse=True,
            )
            best_payload = fallback_payloads[0]

        train_metrics = _Metrics(**best_payload["train"])
        validation_metrics = _Metrics(**best_payload["validation"])
        full_metrics = _Metrics(**best_payload["full"])
        decision, recommendation, rationale = self._decision_for(
            spec=spec,
            train=train_metrics,
            validation=validation_metrics,
            full=full_metrics,
        )
        selection = CandidateSelection(
            family=spec.family,
            variant=spec.variant,
            use_case=spec.use_case,
            objective=spec.objective,
            description=spec.description,
            horizon_bars=horizon_bars,
            regime_preset=str(best_payload["regime_preset"]),
            min_score=float(best_payload["min_score"]),
            max_spread_bps=float(best_payload["max_spread_bps"]),
            min_bucket_notional_usd=float(best_payload["min_bucket_notional_usd"]),
            train=train_metrics.to_dict(),
            validation=validation_metrics.to_dict(),
            full=full_metrics.to_dict(),
            decision=decision,
            recommendation=recommendation,
            rationale=rationale,
            method_note=spec.method_note,
        )
        return selection, sweep_records

    def _evaluate_rows(
        self,
        *,
        rows: list[PodLiqFeatureRow],
        spec: _CandidateSpec,
        min_score: float,
        max_spread_bps: float,
        min_bucket_notional_usd: float,
        allowed_regimes: set[str],
    ) -> _Metrics:
        aligned_returns: list[float] = []
        scores: list[float] = []
        per_symbol_returns: dict[str, list[float]] = {}
        per_regime_returns: dict[str, list[float]] = {}

        for row in rows:
            if row.future_return_bps is None:
                continue
            score = float(getattr(row, spec.score_field, 0.0) or 0.0)
            if score < min_score:
                continue
            if row.spread_bps > max_spread_bps:
                continue
            if row.bucket_notional_usd < min_bucket_notional_usd:
                continue
            if allowed_regimes and row.regime not in allowed_regimes:
                continue
            direction = str(getattr(row, spec.direction_field, "")).lower()
            if direction not in {"long", "short"}:
                continue
            aligned = row.future_return_bps if direction == "long" else -row.future_return_bps
            aligned_returns.append(aligned)
            scores.append(score)
            per_symbol_returns.setdefault(row.symbol, []).append(aligned)
            per_regime_returns.setdefault(row.regime, []).append(aligned)

        sample_count = len(aligned_returns)
        expectancy = round(sum(aligned_returns) / sample_count, 4) if sample_count else 0.0
        positive_hit_rate = (
            round(sum(1 for value in aligned_returns if value > 0) / sample_count, 4)
            if sample_count
            else 0.0
        )
        negative_hit_rate = (
            round(sum(1 for value in aligned_returns if value < 0) / sample_count, 4)
            if sample_count
            else 0.0
        )
        average_score = round(sum(scores) / sample_count, 4) if sample_count else 0.0
        return _Metrics(
            sample_count=sample_count,
            expectancy_bps=expectancy,
            positive_hit_rate=positive_hit_rate,
            negative_hit_rate=negative_hit_rate,
            average_score=average_score,
            best_symbol=self._best_bucket(per_symbol_returns, objective=spec.objective),
            best_regime=self._best_bucket(per_regime_returns, objective=spec.objective),
        )

    def _best_bucket(self, returns_by_key: dict[str, list[float]], *, objective: str) -> str | None:
        best_key = None
        best_value = float("-inf")
        for key, returns in returns_by_key.items():
            if len(returns) < 3:
                continue
            expectancy = sum(returns) / len(returns)
            value = expectancy if objective == "positive" else -expectancy
            if value > best_value:
                best_key = key
                best_value = value
        if best_key is not None:
            return best_key
        for key, returns in returns_by_key.items():
            expectancy = sum(returns) / len(returns)
            value = expectancy if objective == "positive" else -expectancy
            if value > best_value:
                best_key = key
                best_value = value
        return best_key

    def _rank_metrics(self, metrics: _Metrics, *, objective: str) -> tuple[float, float, int]:
        if objective == "positive":
            return (
                metrics.expectancy_bps,
                metrics.positive_hit_rate,
                metrics.sample_count,
            )
        return (
            -metrics.expectancy_bps,
            metrics.negative_hit_rate,
            metrics.sample_count,
        )

    def _rank_metrics_from_payload(
        self,
        payload: dict[str, object],
        *,
        objective: str,
    ) -> tuple[float, float, int]:
        metrics = _Metrics(**payload)
        return self._rank_metrics(metrics, objective=objective)

    def _training_sign_matches(self, metrics: _Metrics, *, objective: str) -> bool:
        if objective == "positive":
            return metrics.expectancy_bps > 0.0
        return metrics.expectancy_bps < 0.0

    def _decision_for(
        self,
        *,
        spec: _CandidateSpec,
        train: _Metrics,
        validation: _Metrics,
        full: _Metrics,
    ) -> tuple[str, str, str]:
        if validation.sample_count < self.MIN_VALIDATION_SAMPLES:
            return (
                "park",
                "research_only",
                "Holdout sample is too small to promote or reject this configuration decisively.",
            )

        if spec.objective == "positive":
            if (
                train.expectancy_bps >= 0.75
                and validation.expectancy_bps >= 0.50
                and validation.positive_hit_rate >= 0.515
                and full.expectancy_bps >= 0.60
            ):
                return (
                    "keep_watch_only",
                    "watch_only",
                    "Positive continuation edge survives both train and holdout, but it still belongs in watcher / shadow mode first.",
                )
            if train.expectancy_bps > 0.0 and validation.expectancy_bps >= 0.0:
                return (
                    "park",
                    "research_only",
                    "Signal keeps the right sign, but the holdout edge is too small for live promotion.",
                )
            return (
                "reject",
                "drop",
                "Continuation edge does not survive the holdout window strongly enough to justify keeping it.",
            )

        if (
            train.expectancy_bps <= -0.75
            and validation.expectancy_bps <= -0.50
            and validation.negative_hit_rate >= 0.515
            and full.expectancy_bps <= -0.60
        ):
            return (
                "keep_veto_only",
                "shadow_veto",
                "The signal consistently predicts failure in the original direction, so it is worth keeping only as a shadow veto candidate.",
            )
        if train.expectancy_bps < 0.0 and validation.expectancy_bps <= 0.0:
            return (
                "park",
                "research_only",
                "The signal leans the right way as a veto, but not strongly enough to justify production gating yet.",
            )
        return (
            "reject",
            "drop",
            "The veto-style edge does not survive the holdout window strongly enough to justify keeping it.",
        )

    def _summarize_family(
        self,
        family: str,
        selections: list[CandidateSelection],
    ) -> FamilySummary:
        family_results = [item for item in selections if item.family == family]
        best = max(family_results, key=self._selection_strength, default=None)
        if best is None:
            return FamilySummary(
                family=family,
                final_decision="reject",
                recommendation="drop",
                best_variant=None,
                rationale="No candidate was evaluated for this family.",
                selected=None,
            )
        return FamilySummary(
            family=family,
            final_decision=best.decision,
            recommendation=best.recommendation,
            best_variant=f"{best.variant}@h{best.horizon_bars}",
            rationale=best.rationale,
            selected=best.to_dict(),
        )

    def _selection_strength(self, selection: CandidateSelection) -> tuple[int, float, float, int]:
        priority = {
            "keep_watch_only": 3,
            "keep_veto_only": 3,
            "park": 2,
            "reject": 1,
        }.get(selection.decision, 0)
        validation = _Metrics(**selection.validation)
        if selection.objective == "positive":
            primary = validation.expectancy_bps
            secondary = validation.positive_hit_rate
        else:
            primary = -validation.expectancy_bps
            secondary = validation.negative_hit_rate
        return (
            priority,
            primary,
            secondary,
            validation.sample_count,
        )

    def _filter_rows_by_date(
        self,
        rows: list[PodLiqFeatureRow],
        *,
        start_date: str | None,
        end_date: str | None,
    ) -> list[PodLiqFeatureRow]:
        filtered: list[PodLiqFeatureRow] = []
        for row in rows:
            row_date = self._row_date(row)
            if row_date is None:
                continue
            if start_date is not None and row_date < start_date:
                continue
            if end_date is not None and row_date > end_date:
                continue
            filtered.append(row)
        return filtered

    def _row_date(self, row: PodLiqFeatureRow) -> str | None:
        if not isinstance(row.timestamp, str) or len(row.timestamp) < 10:
            return None
        return row.timestamp[:10]

    def _render_markdown(self, result: PodLiqExhaustiveResearchResult) -> str:
        lines = [
            "# Pod Liq Exhaustive Microstructure Research",
            "",
            f"- Input path: `{result.input_path}`",
            f"- Config path: `{result.config_path}`",
            f"- Date window: `{result.start_date or '-'} -> {result.end_date or '-'}`",
            f"- Train end date: `{result.train_end_date}`",
            f"- Validation start date: `{result.validation_start_date}`",
            f"- Horizons tested: `{', '.join(str(item) for item in result.horizons)}`",
            "",
            "## Family Verdicts",
            "",
            "| Family | Best use | Horizon | Decision | Holdout exp | Holdout hit | Config |",
            "|--------|----------|---------|----------|-------------|-------------|--------|",
        ]
        for family in result.family_summaries:
            selected = family.get("selected") or {}
            validation = selected.get("validation") or {}
            config = "-"
            if selected:
                config = (
                    f"score>={selected.get('min_score')} | spread<={selected.get('max_spread_bps')} | "
                    f"notional>={selected.get('min_bucket_notional_usd')} | regime={selected.get('regime_preset')}"
                )
            lines.append(
                f"| {family['family']} | {selected.get('variant', '-')} | {selected.get('horizon_bars', '-')} | "
                f"{family['final_decision']} | {validation.get('expectancy_bps', '-')} | "
                f"{max(validation.get('positive_hit_rate', 0.0), validation.get('negative_hit_rate', 0.0)) if validation else '-'} | {config} |"
            )

        lines.extend(
            [
                "",
                "## Notes",
                "",
            ]
        )
        for note in result.notes:
            lines.append(f"- {note}")

        lines.extend(
            [
                "",
                "## Detailed Family Analysis",
                "",
            ]
        )
        for family in result.family_summaries:
            lines.append(f"### {family['family']}")
            lines.append("")
            lines.append(f"- Final decision: `{family['final_decision']}`")
            lines.append(f"- Recommendation: `{family['recommendation']}`")
            lines.append(f"- Why: {family['rationale']}")
            selected = family.get("selected") or {}
            if selected:
                lines.append(f"- Selected variant: `{selected['variant']}`")
                lines.append(f"- Use case: `{selected['use_case']}`")
                lines.append(f"- Objective: `{selected['objective']}`")
                lines.append(f"- Horizon: `{selected['horizon_bars']}`")
                lines.append(
                    f"- Best config: `score>={selected['min_score']}`, `spread<={selected['max_spread_bps']}`, "
                    f"`notional>={selected['min_bucket_notional_usd']}`, `regime={selected['regime_preset']}`"
                )
                train = selected["train"]
                validation = selected["validation"]
                full = selected["full"]
                lines.append(
                    f"- Train: `samples={train['sample_count']}` | `exp={train['expectancy_bps']} bps` | "
                    f"`hit+={train['positive_hit_rate']}` | `hit-={train['negative_hit_rate']}`"
                )
                lines.append(
                    f"- Holdout: `samples={validation['sample_count']}` | `exp={validation['expectancy_bps']} bps` | "
                    f"`hit+={validation['positive_hit_rate']}` | `hit-={validation['negative_hit_rate']}`"
                )
                lines.append(
                    f"- Full window: `samples={full['sample_count']}` | `exp={full['expectancy_bps']} bps` | "
                    f"`hit+={full['positive_hit_rate']}` | `hit-={full['negative_hit_rate']}` | "
                    f"`best_symbol={full['best_symbol'] or '-'}` | `best_regime={full['best_regime'] or '-'}`"
                )
                if selected.get("method_note"):
                    lines.append(f"- Method note: {selected['method_note']}")
            lines.append("")

        return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run exhaustive microstructure sweeps for the active Trident plan")
    parser.add_argument("--input", required=True)
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--train-end-date", default="2026-04-18")
    parser.add_argument("--validation-start-date", default="2026-04-20")
    parser.add_argument("--horizons", help="Comma-separated list like 1,3,5")
    parser.add_argument("--output-json")
    parser.add_argument("--output-md")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    horizons = [
        int(item.strip())
        for item in (args.horizons or "").split(",")
        if item.strip()
    ]
    result = PodLiqExhaustiveResearchRunner().run(
        input_path=args.input,
        config_path=args.config,
        start_date=args.start_date,
        end_date=args.end_date,
        train_end_date=args.train_end_date,
        validation_start_date=args.validation_start_date,
        horizons=horizons or None,
        output_json=args.output_json,
        output_md=args.output_md,
    )
    print(f"family_count={len(result.family_summaries)}")
    print(f"selected_candidate_count={len(result.selected_candidates)}")


if __name__ == "__main__":
    main()
