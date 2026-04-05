from __future__ import annotations

from dataclasses import dataclass, field

from app.trident.types import PodHealth, PodName


@dataclass(slots=True)
class ConfiguredPod:
    name: PodName
    enabled: bool
    desired_symbols: list[str] = field(default_factory=list)
    health_message: str = "configured"

    def health(self) -> PodHealth:
        return PodHealth(
            pod=self.name,
            healthy=self.enabled,
            message=self.health_message if self.enabled else "disabled",
        )

