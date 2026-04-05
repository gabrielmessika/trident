from __future__ import annotations

from datetime import datetime, timezone

from app.trident.pod_b.models import PassivbotConfig


class PassivbotConfigRenderer:
    """Renders a minimal Passivbot-compatible live config controlled by TRIDENT."""

    def render(self, config: PassivbotConfig) -> dict[str, object]:
        return {
            "bot": {
                "long": {"enabled": True},
                "short": {"enabled": True},
            },
            "live": {
                "approved_coins": config.approved_coins,
                "execution_delay_seconds": config.execution_delay_seconds,
                "market_orders_allowed": config.market_orders_allowed,
                "time_in_force": config.time_in_force,
                "leverage": config.leverage,
                "empty_means_all_approved": False,
            },
            "trident": {
                "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "managed_symbols": config.approved_coins,
                "target_pct": config.target_pct,
                "target_usd": config.target_usd,
                **config.metadata,
            },
        }
