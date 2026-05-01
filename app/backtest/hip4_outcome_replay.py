from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.trident.hip4_outcome.reporting import replay_opportunities


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay HIP-4 outcome opportunity logs")
    parser.add_argument("--logs-dir", default="logs/hip4_outcome_paper")
    parser.add_argument("--output", default=None)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    logs_dir = Path(args.logs_dir)
    payload = {
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
