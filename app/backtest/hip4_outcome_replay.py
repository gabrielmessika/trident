from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.trident.hip4_outcome.reporting import replay_opportunities


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay HIP-4 outcome opportunity logs")
    parser.add_argument(
        "--profile",
        choices=["testnet", "mainnet"],
        default="testnet",
        help="Default logs directory profile when --logs-dir is omitted.",
    )
    parser.add_argument("--logs-dir", default=None)
    parser.add_argument("--output", default=None)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    default_logs_dir = {
        "testnet": "logs/hip4_outcome_testnet",
        "mainnet": "logs/hip4_outcome_mainnet",
    }[args.profile]
    logs_dir = Path(args.logs_dir or default_logs_dir)
    payload = {
        "profile": args.profile,
        "logs_dir": str(logs_dir),
        "opportunities": replay_opportunities(logs_dir / "opportunities.csv"),
    }
    text = json.dumps(payload, indent=2, sort_keys=True)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
