from __future__ import annotations


class KillSwitch:
    """Global kill switch placeholder for early bootstrap."""

    def __init__(self) -> None:
        self._active_reason: str | None = None

    @property
    def active_reason(self) -> str | None:
        return self._active_reason

    @property
    def is_active(self) -> bool:
        return self._active_reason is not None

    def activate(self, reason: str) -> None:
        self._active_reason = reason

    def reset(self) -> None:
        self._active_reason = None

