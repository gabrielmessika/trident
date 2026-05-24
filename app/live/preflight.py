from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from app.hyperliquid.private_state import HyperliquidCredentials, HyperliquidPrivateInfoClient
from app.live.errors import HyperliquidAPIError
from app.live.reconciliation import reconcile_exchange_state
from app.live.state_store import LiveStateStore, live_state_path_for_pod
from app.live.user_stream import check_order_updates_subscription
from app.portfolio.directional_state import DirectionalPortfolioState
from app.settings import load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TRIDENT live readiness preflight")
    parser.add_argument("--config", default=os.getenv("TRIDENT_CONFIG_PATH", "config/trident.toml"))
    parser.add_argument("--pod", default="pod_a")
    parser.add_argument("--state-path", default=None)
    parser.add_argument("--external-state-path", action="append", default=[])
    parser.add_argument("--fills-lookback-hours", type=float, default=24.0)
    parser.add_argument("--allow-unknown-exchange-positions", action="store_true")
    parser.add_argument("--allow-open-orders", action="store_true")
    parser.add_argument("--skip-user-ws-check", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def default_state_path(pod: str) -> str:
    normalized = str(pod).strip().lower().replace("-", "_")
    return live_state_path_for_pod(pod, allow_global=(normalized == "pod_a"))


async def run_preflight(args: argparse.Namespace) -> tuple[bool, dict[str, object]]:
    config = load_config(args.config)
    credentials = HyperliquidCredentials.from_env()
    credential_errors = credentials.validate_for_trading()
    payload: dict[str, object] = {
        "pod": args.pod,
        "config": args.config,
        "credential_errors": credential_errors,
        "ready": False,
    }
    if credential_errors:
        payload["reasons"] = ["credential_error"]
        return False, payload

    state_path = args.state_path or default_state_path(args.pod)
    store = LiveStateStore(state_path)
    external_stores = [
        LiveStateStore(path)
        for path in (args.external_state_path or [])
        if str(path).strip()
    ]
    portfolio = DirectionalPortfolioState()
    private_client = HyperliquidPrivateInfoClient(config.hyperliquid, credentials)
    account_state = private_client.fetch_account_state(
        fills_lookback_hours=args.fills_lookback_hours,
        include_account_mode=True,
    )
    report = reconcile_exchange_state(
        account_state=account_state,
        portfolio=portfolio,
        state_store=store,
        allow_unknown_exchange_positions=args.allow_unknown_exchange_positions,
        allow_open_orders=args.allow_open_orders,
        external_state_stores=external_stores,
    )
    payload["state_path"] = state_path
    payload["external_state_paths"] = [str(store.path) for store in external_stores]
    payload["account"] = {
        "address": account_state.account_address,
        "account_mode": account_state.account_mode,
        "account_value_usd": account_state.account_value_usd,
        "withdrawable_usd": account_state.withdrawable_usd,
        "total_margin_used_usd": account_state.total_margin_used_usd,
        "spot_usdc_total": account_state.spot_usdc_total,
        "spot_usdc_hold": account_state.spot_usdc_hold,
        "spot_usdc_available": account_state.spot_usdc_available,
        "hl_available_usd": account_state.hl_available_usd,
        "hl_capital_source": account_state.hl_capital_source,
        "perp_account_value_usd": account_state.account_value_usd,
        "perp_withdrawable_usd": account_state.withdrawable_usd,
        "position_symbols": sorted(account_state.positions),
        "open_order_count": len(account_state.open_orders),
        "frontend_open_order_count": len(account_state.frontend_open_orders),
        "recent_fill_count": len(account_state.recent_fills),
        "fetched_at": account_state.fetched_at,
    }
    payload["reconciliation"] = report.to_dict()

    ws_ready = True
    if not args.skip_user_ws_check:
        ws_check = await check_order_updates_subscription(
            config.hyperliquid,
            account_address=credentials.account_address,
            timeout_seconds=min(config.hyperliquid.connect_timeout_seconds, 10.0),
        )
        payload["user_stream"] = ws_check.to_dict()
        ws_ready = ws_check.ok
    else:
        payload["user_stream"] = {"ok": True, "skipped": True}

    ready = report.ready and ws_ready
    payload["ready"] = ready
    if not ready:
        reasons = list(report.reasons)
        if not ws_ready:
            reasons.append("order_updates_ws_not_ready")
        payload["reasons"] = reasons
    return ready, payload


def main() -> None:
    args = build_parser().parse_args()
    try:
        ready, payload = asyncio.run(run_preflight(args))
    except HyperliquidAPIError as exc:
        ready = False
        payload = {"ready": False, "reasons": ["api_error"], "error": str(exc)}
    text = json.dumps(payload, indent=2, sort_keys=True)
    print(text)
    raise SystemExit(0 if ready else 1)


if __name__ == "__main__":
    main()
