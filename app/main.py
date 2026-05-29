from __future__ import annotations

import argparse
import logging
import os

from app.live.crash_alerts import notify_crash
from app.observability.api import run_http_server
from app.observability.metrics import MetricsRegistry
from app.settings import load_config
from app.trident.supervisor import TridentSupervisor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TRIDENT orchestrator")
    parser.add_argument(
        "--mode",
        default=os.getenv("TRIDENT_MODE", "observation"),
        choices=["observation", "dry-run", "live"],
    )
    parser.add_argument(
        "--profile",
        default=os.getenv("TRIDENT_PROFILE", "trident"),
    )
    parser.add_argument(
        "--host",
        default=os.getenv("TRIDENT_HOST"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("TRIDENT_PORT", "0") or "0"),
    )
    parser.add_argument(
        "--config",
        default=os.getenv("TRIDENT_CONFIG_PATH", "config/trident.toml"),
    )
    return parser


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        args = build_parser().parse_args()
        config = load_config(args.config)
        host = args.host or config.general.host
        port = args.port or config.general.port

        supervisor = TridentSupervisor(config=config, profile=args.profile, mode=args.mode)
        run_http_server(
            host=host,
            port=port,
            supervisor=supervisor,
            metrics=MetricsRegistry(),
        )
    except Exception as exc:
        notify_crash(service_name="trident-api", exc=exc)
        raise


if __name__ == "__main__":
    main()
