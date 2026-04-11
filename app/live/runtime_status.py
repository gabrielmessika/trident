from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def write_runtime_status(path: str | Path, payload: dict[str, object]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_runtime_status(path: str | Path) -> dict[str, object] | None:
    source = Path(path)
    if not source.exists():
        return None
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def runtime_status_age_seconds(payload: dict[str, object] | None) -> float | None:
    if not isinstance(payload, dict):
        return None
    updated_at = payload.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at:
        return None
    normalized = updated_at.replace("Z", "+00:00")
    try:
        updated_dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if updated_dt.tzinfo is None:
        updated_dt = updated_dt.replace(tzinfo=timezone.utc)
    return max((datetime.now(timezone.utc) - updated_dt).total_seconds(), 0.0)


def runtime_status_max_age_seconds(
    payload: dict[str, object] | None,
    *,
    default_max_age_seconds: float = 180.0,
) -> float:
    if not isinstance(payload, dict):
        return float(default_max_age_seconds)
    poll_seconds = payload.get("poll_seconds")
    try:
        poll_seconds_value = float(poll_seconds)
    except (TypeError, ValueError):
        return float(default_max_age_seconds)
    if poll_seconds_value <= 0.0:
        return float(default_max_age_seconds)
    return max(float(default_max_age_seconds), poll_seconds_value * 1.25)


def runtime_status_is_fresh(
    payload: dict[str, object] | None,
    *,
    max_age_seconds: float | None = None,
) -> bool:
    age_seconds = runtime_status_age_seconds(payload)
    if age_seconds is None:
        return False
    allowed_age_seconds = (
        float(max_age_seconds)
        if max_age_seconds is not None
        else runtime_status_max_age_seconds(payload)
    )
    return age_seconds <= allowed_age_seconds
