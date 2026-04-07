from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def load_runtime_symbol_pod_override_payload(
    path: str | Path,
) -> dict[str, object]:
    source = Path(path)
    if not source.exists():
        return {
            "updated_at": None,
            "symbol_pod_overrides": {},
        }
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "updated_at": None,
            "symbol_pod_overrides": {},
        }
    if not isinstance(payload, dict):
        return {
            "updated_at": None,
            "symbol_pod_overrides": {},
        }
    raw_overrides = payload.get("symbol_pod_overrides", payload)
    normalized_overrides: dict[str, str] = {}
    if isinstance(raw_overrides, dict):
        for symbol, owner in raw_overrides.items():
            normalized_symbol = str(symbol).strip().upper()
            normalized_owner = str(owner).strip().lower()
            if not normalized_symbol or not normalized_owner:
                continue
            normalized_overrides[normalized_symbol] = normalized_owner
    updated_at = payload.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at.strip():
        updated_at = None
    return {
        "updated_at": updated_at,
        "symbol_pod_overrides": normalized_overrides,
    }


def write_runtime_symbol_pod_override_payload(
    path: str | Path,
    symbol_pod_overrides: dict[str, str],
) -> dict[str, object]:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "symbol_pod_overrides": {
            str(symbol).strip().upper(): str(owner).strip().lower()
            for symbol, owner in sorted(symbol_pod_overrides.items())
            if str(symbol).strip() and str(owner).strip()
        },
    }
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload
