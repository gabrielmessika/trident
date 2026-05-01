from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from app.trident.hip4_outcome.models import OutcomePosition


class OutcomeStateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load_positions(self) -> list[OutcomePosition]:
        payload = self.load()
        raw_positions = payload.get("positions", [])
        if not isinstance(raw_positions, list):
            return []
        positions: list[OutcomePosition] = []
        for item in raw_positions:
            if not isinstance(item, dict):
                continue
            try:
                positions.append(OutcomePosition.from_dict(item))
            except (TypeError, ValueError):
                continue
        return positions

    def save_positions(self, positions: list[OutcomePosition]) -> None:
        payload = self.load()
        payload["positions"] = [position.to_dict() for position in positions]
        self.save(payload)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"positions": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"positions": []}
        return payload if isinstance(payload, dict) else {"positions": []}

    def save(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        tmp_path = self.path.with_name(f".{self.path.name}.tmp")
        tmp_path.write_text(body, encoding="utf-8")
        os.replace(tmp_path, self.path)
