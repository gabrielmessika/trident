from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def write_runtime_status(path: str | Path, payload: dict[str, object]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, indent=2) + "\n"
    tmp_path = output.with_name(f".{output.name}.tmp")
    tmp_path.write_text(body, encoding="utf-8")
    os.replace(tmp_path, output)


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
    return sanitize_runtime_status_payload(payload)


def sanitize_runtime_status_payload(
    payload: dict[str, object],
    *,
    include_supervisor: bool = True,
) -> dict[str, object]:
    sanitized = dict(payload)
    if include_supervisor:
        supervisor = sanitized.get("supervisor")
        if isinstance(supervisor, dict):
            sanitized["supervisor"] = sanitize_runtime_supervisor_snapshot(supervisor)
    else:
        sanitized.pop("supervisor", None)
    for key in ("pod_a_runtime", "pod_b_runtime", "pod_c_runtime"):
        nested = sanitized.get(key)
        if isinstance(nested, dict):
            sanitized[key] = sanitize_runtime_status_payload(
                nested,
                include_supervisor=False,
            )
    pod_b_status = sanitized.get("pod_b_status")
    if isinstance(pod_b_status, dict):
        sanitized["pod_b_status"] = sanitize_runtime_status_payload(
            pod_b_status,
            include_supervisor=False,
        )
    return sanitized


def sanitize_runtime_supervisor_snapshot(snapshot: dict[str, object]) -> dict[str, object]:
    sanitized = dict(snapshot)
    for key in ("pod_a_runtime", "pod_b_runtime", "pod_c_runtime", "runtime_report", "metrics"):
        sanitized.pop(key, None)
    pod_b_status = sanitized.get("pod_b_status")
    if isinstance(pod_b_status, dict):
        sanitized["pod_b_status"] = sanitize_runtime_status_payload(
            pod_b_status,
            include_supervisor=False,
        )
    return sanitized


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
