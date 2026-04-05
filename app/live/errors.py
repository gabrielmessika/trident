from __future__ import annotations


class HyperliquidAPIError(RuntimeError):
    """Base class for classified Hyperliquid API failures."""


class HyperliquidRateLimitError(HyperliquidAPIError):
    """Raised when Hyperliquid signals a rate-limit condition."""


class LiveCollectorRecoverableError(HyperliquidAPIError):
    """Raised when the websocket collector should reconnect and retry."""


def is_rate_limit_message(message: str) -> bool:
    normalized = message.lower()
    return "rate limit" in normalized or "too many requests" in normalized or "429" in normalized


def classify_payload_error(payload: dict[str, object]) -> HyperliquidAPIError | None:
    raw_error = payload.get("error")
    if raw_error is None and payload.get("channel") == "error":
        raw_error = payload.get("data") or payload.get("message")
    if raw_error is None:
        return None
    message = str(raw_error)
    if is_rate_limit_message(message):
        return HyperliquidRateLimitError(message)
    return LiveCollectorRecoverableError(message)
