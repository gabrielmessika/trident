from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path


DEFAULT_OVERLAP_LOCK_DIR = Path("runtime/hip4_overlap_locks")
DEFAULT_OVERLAP_LOCK_TTL_SECONDS = 300.0


@dataclass(slots=True)
class UnderlyingOverlapLock:
    underlying: str
    owner: str
    lock_dir: Path = DEFAULT_OVERLAP_LOCK_DIR
    ttl_seconds: float = DEFAULT_OVERLAP_LOCK_TTL_SECONDS
    acquired: bool = False

    @property
    def path(self) -> Path:
        safe_underlying = "".join(
            char if char.isalnum() or char in {"_", "-"} else "_"
            for char in self.underlying.upper()
        )
        return self.lock_dir / f"{safe_underlying}.lock"

    def acquire(self) -> bool:
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self._remove_stale_lock()
        payload = {
            "underlying": self.underlying.upper(),
            "owner": self.owner,
            "pid": os.getpid(),
            "created_at": time.time(),
        }
        try:
            fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
            handle.write("\n")
        self.acquired = True
        return True

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        if not isinstance(payload, dict) or str(payload.get("owner")) == self.owner:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self.acquired = False

    def _remove_stale_lock(self) -> None:
        path = self.path
        if not path.exists():
            return
        payload = self._read_payload()
        if self._is_dead_owner_lock(payload):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return
        if age <= max(self.ttl_seconds, 1.0):
            return
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _read_payload(self) -> dict[str, object]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _is_dead_owner_lock(self, payload: dict[str, object]) -> bool:
        if str(payload.get("owner")) != self.owner:
            return False
        try:
            pid = int(payload.get("pid", 0))
        except (TypeError, ValueError):
            return False
        if pid <= 0 or pid == os.getpid():
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False
