from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.trident.hip4_outcome.analysis import (
    DEFAULT_PROFILE_LOGS,
    ReviewThresholds,
    analyze_profiles,
    render_markdown,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Review HIP-4 outcome paper/testnet/mainnet-observer runs."
    )
    parser.add_argument(
        "--logs-dir",
        action="append",
        default=[],
        metavar="PROFILE=PATH",
        help=(
            "Logs directory to review. Can be repeated. "
            "Defaults to testnet/mainnet/paper HIP-4 log directories."
        ),
    )
    parser.add_argument(
        "--output-json",
        default="server-data/replay_reports/hip4_outcome_run_review_latest.json",
    )
    parser.add_argument(
        "--output-md",
        default="server-data/replay_reports/hip4_outcome_run_review_latest.md",
    )
    parser.add_argument("--min-testnet-settlements", type=int, default=20)
    parser.add_argument("--min-testnet-markets", type=int, default=5)
    parser.add_argument("--min-testnet-days", type=int, default=2)
    parser.add_argument("--min-mainnet-opportunities", type=int, default=20)
    parser.add_argument("--min-calibration-samples", type=int, default=20)
    parser.add_argument("--min-guardrail-exclusions", type=int, default=3)
    parser.add_argument("--min-profit-factor", type=float, default=1.15)
    parser.add_argument("--max-brier-score", type=float, default=0.23)
    parser.add_argument("--max-avg-fill-slippage", type=float, default=0.02)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    profiles = _parse_profiles(args.logs_dir)
    thresholds = ReviewThresholds(
        min_testnet_settlements=args.min_testnet_settlements,
        min_testnet_markets=args.min_testnet_markets,
        min_testnet_days=args.min_testnet_days,
        min_mainnet_opportunities=args.min_mainnet_opportunities,
        min_calibration_samples=args.min_calibration_samples,
        min_guardrail_exclusions=args.min_guardrail_exclusions,
        min_profit_factor=args.min_profit_factor,
        max_brier_score=args.max_brier_score,
        max_avg_fill_slippage=args.max_avg_fill_slippage,
    )
    payload = analyze_profiles(profiles, thresholds=thresholds)

    json_text = json.dumps(payload, indent=2, sort_keys=True)
    markdown = render_markdown(payload)
    _write_text(args.output_json, json_text + "\n")
    _write_text(args.output_md, markdown)
    print(json_text)


def _parse_profiles(values: list[str]) -> dict[str, str]:
    if not values:
        return dict(DEFAULT_PROFILE_LOGS)
    profiles: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise SystemExit(f"--logs-dir must be PROFILE=PATH, got: {value}")
        profile, path = value.split("=", 1)
        profile = profile.strip()
        path = path.strip()
        if not profile or not path:
            raise SystemExit(f"--logs-dir must be PROFILE=PATH, got: {value}")
        profiles[profile] = path
    return profiles


def _write_text(path_value: str | None, text: str) -> None:
    if not path_value:
        return
    path = Path(path_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
