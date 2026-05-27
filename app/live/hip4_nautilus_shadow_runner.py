from __future__ import annotations

import argparse
import json
import time

from app.trident.hip4_outcome.config import load_hip4_outcome_config
from app.trident.hip4_outcome.nautilus_shadow import (
    collect_nautilus_shadow_once,
    load_nautilus_shadow_config,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the HIP-4 Nautilus shadow/read-only adapter."
    )
    parser.add_argument(
        "--config",
        default="config/hip4_nautilus_shadow.toml",
        help="Nautilus shadow config path.",
    )
    parser.add_argument(
        "--hip4-config",
        default="config/hip4_outcome_mainnet_paper.toml",
        help="HIP-4 config used for read-only Hyperliquid info URLs/rate limits.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single shadow collection loop and exit.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even if hip4_nautilus_shadow.enabled=false.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    shadow_config = load_nautilus_shadow_config(args.config)
    hip4_config = load_hip4_outcome_config(args.hip4_config, apply_env=False)

    while True:
        started = time.monotonic()
        payload = collect_nautilus_shadow_once(
            shadow_config,
            hip4_config,
            force=args.force,
        )
        print(json.dumps(payload, sort_keys=True))
        if args.once:
            return
        min_spacing = 60.0 / max(float(shadow_config.max_ws_connects_per_minute), 1.0)
        elapsed = time.monotonic() - started
        sleep_for = max(float(shadow_config.loop_interval_seconds), min_spacing - elapsed, 0.1)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
