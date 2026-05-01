from __future__ import annotations

import argparse
import json
import logging
import time

from app.trident.hip4_outcome import HIP4OutcomeEdgePod, load_hip4_outcome_config
from app.trident.hip4_outcome.execution import TestnetOutcomeExecutor
from app.trident.hip4_outcome.models import SupervisorDecision


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Experimental HIP-4 outcome Pod B runner")
    parser.add_argument(
        "--config",
        default="config/hip4_outcome_testnet.toml",
        help="HIP-4 outcome TOML config path",
    )
    parser.add_argument(
        "--mode",
        choices=["observer", "paper", "testnet"],
        default=None,
        help="Override config mode",
    )
    parser.add_argument("--once", action="store_true", help="Run one loop and exit")
    parser.add_argument("--max-loops", type=int, default=None)
    parser.add_argument("--max-runtime-seconds", type=float, default=None)
    parser.add_argument("--loop-interval-seconds", type=float, default=None)
    parser.add_argument("--preflight", action="store_true", help="Check readiness without placing orders")
    parser.add_argument("--log-level", default="INFO")
    return parser


def run_preflight(pod: HIP4OutcomeEdgePod) -> tuple[bool, dict[str, object]]:
    reasons: list[str] = []
    payload: dict[str, object] = {
        "pod": "hip4_outcome_edge_pod",
        "mode": pod.config.mode,
        "info_url": pod.config.info_url,
        "ready": False,
        "capital": pod.capital_guard.local_snapshot(open_positions=[]).to_dict(),
    }
    try:
        mids = pod.info_client.fetch_all_mids()
        markets = pod._discover_markets(now_ts=int(time.time()))  # noqa: SLF001 - CLI readiness probe
        reference_prices = pod.price_aggregator.fetch_many(
            [market.underlying for market in markets],
            hyperliquid_mids=mids,
        )
        market_underlyings = sorted({market.underlying.upper() for market in markets})
        missing_references = [
            underlying for underlying in market_underlyings if underlying not in reference_prices
        ]
        if missing_references:
            reasons.append("missing_reference_prices")
        payload.update(
            {
                "markets_seen": pod._last_markets_seen,  # noqa: SLF001
                "markets_supported": len(markets),
                "market_underlyings": market_underlyings,
                "reference_prices": {
                    underlying: reference.to_metadata()
                    for underlying, reference in reference_prices.items()
                },
                "missing_reference_underlyings": missing_references,
            }
        )
    except Exception as exc:
        reasons.append("public_data_error")
        payload["public_data_error"] = str(exc)

    if pod.config.mode == "testnet":
        try:
            executor = TestnetOutcomeExecutor(pod.config)
            capital_snapshot = pod.capital_guard.apply(
                decision=SupervisorDecision(
                    approved=True,
                    approved_size_usdc=1.0,
                    reason="preflight_capital_probe",
                    execution_mode="TESTNET",
                ),
                open_positions=[],
                testnet_executor=executor,
            )[1]
            payload["testnet_credentials"] = "ok"
            payload["capital"] = capital_snapshot.to_dict()
            if capital_snapshot.reason not in {"capital_ok", "preflight_capital_probe"}:
                reasons.append("testnet_capital_error")
        except Exception as exc:
            reasons.append("testnet_credentials_error")
            payload["testnet_credentials_error"] = str(exc)

    ready = not reasons
    payload["ready"] = ready
    if reasons:
        payload["reasons"] = reasons
    return ready, payload


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = load_hip4_outcome_config(args.config).with_mode(args.mode)
    if args.loop_interval_seconds is not None:
        config.loop_interval_seconds = args.loop_interval_seconds
    pod = HIP4OutcomeEdgePod(config)
    if args.preflight:
        ready, payload = run_preflight(pod)
        print(json.dumps(payload, indent=2, sort_keys=True))
        raise SystemExit(0 if ready else 1)
    if args.once:
        summary = pod.run_once()
    else:
        summary = pod.run(
            max_loops=args.max_loops,
            max_runtime_seconds=args.max_runtime_seconds,
        )
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
