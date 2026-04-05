from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from app.research.pod_c_leadlag import LeadLagResearchRunner, LeadLagStudyResult


@dataclass(slots=True)
class PodCResearchSuiteResult:
    input_path: str
    candidate_count: int
    go_count: int
    best_candidate: dict[str, object] | None
    recommendation: str
    studies: list[dict[str, object]]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class PodCResearchSuite:
    """Runs a reproducible Pod C study across multiple leader/follower pairs."""

    def __init__(self) -> None:
        self.runner = LeadLagResearchRunner()

    def run(
        self,
        *,
        input_path: str | Path,
        leader_symbols: list[str],
        follower_symbols: list[str],
        impulse_threshold_bps: float = 8.0,
        horizon_bars: int = 2,
        output_json: str | Path | None = None,
        output_md: str | Path | None = None,
    ) -> PodCResearchSuiteResult:
        studies: list[LeadLagStudyResult] = []
        for leader in leader_symbols:
            followers = [symbol for symbol in follower_symbols if symbol.upper() != leader.upper()]
            if not followers:
                continue
            studies.append(
                self.runner.run(
                    input_path=input_path,
                    leader_symbol=leader,
                    follower_symbols=followers,
                    impulse_threshold_bps=impulse_threshold_bps,
                    horizon_bars=horizon_bars,
                )
            )

        go_studies = [study for study in studies if study.recommendation == "go"]
        best_candidate = None
        if go_studies:
            best = max(go_studies, key=lambda item: item.best_expectancy_bps)
            best_candidate = {
                "leader_symbol": best.leader_symbol,
                "best_symbol": best.best_symbol,
                "best_expectancy_bps": best.best_expectancy_bps,
                "recommendation": best.recommendation,
                "rationale": best.rationale,
            }

        result = PodCResearchSuiteResult(
            input_path=str(input_path),
            candidate_count=len(studies),
            go_count=len(go_studies),
            best_candidate=best_candidate,
            recommendation="go" if best_candidate is not None else "no-go",
            studies=[study.to_dict() for study in studies],
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

    def _render_markdown(self, result: PodCResearchSuiteResult) -> str:
        lines = [
            "# Pod C Research Suite",
            "",
            f"- Input path: {result.input_path}",
            f"- Candidate count: {result.candidate_count}",
            f"- Go count: {result.go_count}",
            f"- Recommendation: {result.recommendation}",
        ]
        if result.best_candidate is not None:
            lines.extend(
                [
                    f"- Best leader: {result.best_candidate['leader_symbol']}",
                    f"- Best follower: {result.best_candidate['best_symbol']}",
                    f"- Best expectancy bps: {result.best_candidate['best_expectancy_bps']}",
                    f"- Rationale: {result.best_candidate['rationale']}",
                ]
            )
        lines.extend(
            [
                "",
                "| Leader | Best follower | Expectancy bps | Recommendation |",
                "|--------|---------------|----------------|----------------|",
            ]
        )
        for study in result.studies:
            lines.append(
                f"| {study['leader_symbol']} | {study['best_symbol'] or '-'} | "
                f"{study['best_expectancy_bps']} | {study['recommendation']} |"
            )
        lines.append("")
        return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Pod C research suite")
    parser.add_argument("--input", required=True)
    parser.add_argument("--leader-symbols", required=True, help="Comma-separated list")
    parser.add_argument("--follower-symbols", required=True, help="Comma-separated list")
    parser.add_argument("--impulse-threshold-bps", type=float, default=8.0)
    parser.add_argument("--horizon-bars", type=int, default=2)
    parser.add_argument("--output-json")
    parser.add_argument("--output-md")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    result = PodCResearchSuite().run(
        input_path=args.input,
        leader_symbols=[symbol.strip().upper() for symbol in args.leader_symbols.split(",") if symbol.strip()],
        follower_symbols=[symbol.strip().upper() for symbol in args.follower_symbols.split(",") if symbol.strip()],
        impulse_threshold_bps=args.impulse_threshold_bps,
        horizon_bars=args.horizon_bars,
        output_json=args.output_json,
        output_md=args.output_md,
    )
    print(f"candidate_count={result.candidate_count}")
    print(f"go_count={result.go_count}")
    print(f"recommendation={result.recommendation}")


if __name__ == "__main__":
    main()
