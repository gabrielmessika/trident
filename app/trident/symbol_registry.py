from __future__ import annotations

from dataclasses import dataclass

from app.trident.types import PodName


@dataclass(slots=True)
class OwnershipView:
    symbol: str
    owner: PodName | None


class SymbolRegistry:
    """Tracks exclusive symbol ownership across pods."""

    def __init__(self) -> None:
        self._owners: dict[str, PodName] = {}

    def owner_of(self, symbol: str) -> PodName | None:
        return self._owners.get(symbol.upper())

    def clear(self) -> None:
        self._owners.clear()

    def claim(self, symbol: str, pod: PodName) -> bool:
        normalized = symbol.upper()
        owner = self._owners.get(normalized)
        if owner is not None and owner != pod:
            return False
        self._owners[normalized] = pod
        return True

    def release(self, symbol: str, pod: PodName) -> bool:
        normalized = symbol.upper()
        owner = self._owners.get(normalized)
        if owner != pod:
            return False
        del self._owners[normalized]
        return True

    def snapshot(self) -> list[OwnershipView]:
        return [
            OwnershipView(symbol=symbol, owner=owner)
            for symbol, owner in sorted(self._owners.items())
        ]

    def symbols_for(self, pod: PodName) -> list[str]:
        return sorted(symbol for symbol, owner in self._owners.items() if owner == pod)
