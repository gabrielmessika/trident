from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(slots=True)
class SharedRateLimiterStats:
    wait_count: int = 0
    wait_seconds: float = 0.0
    reservation_count: int = 0
    rate_limit_events: int = 0
    circuit_open_count: int = 0


class SharedRateLimiter:
    """Small cross-process rate limiter backed by a JSON state file."""

    def __init__(
        self,
        state_path: str | Path,
        *,
        time_fn: Callable[[], float] | None = None,
        jitter_fn: Callable[[float], float] | None = None,
    ) -> None:
        self.state_path = Path(state_path)
        self.lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
        self.time_fn = time_fn or time.time
        self.jitter_fn = jitter_fn or (lambda seconds: seconds)
        self.stats = SharedRateLimiterStats()

    def acquire(
        self,
        key: str,
        *,
        capacity: int,
        window_seconds: float,
        sleep_fn: Callable[[float], None],
        cost: float = 1.0,
    ) -> float:
        total_wait = 0.0
        while True:
            wait_seconds = self.reserve(
                key,
                capacity=capacity,
                window_seconds=window_seconds,
                cost=cost,
            )
            if wait_seconds <= 0:
                return round(total_wait, 4)
            self.stats.wait_count += 1
            self.stats.wait_seconds = round(self.stats.wait_seconds + wait_seconds, 4)
            total_wait += wait_seconds
            sleep_fn(wait_seconds)

    def reserve(
        self,
        key: str,
        *,
        capacity: int,
        window_seconds: float,
        cost: float = 1.0,
    ) -> float:
        now = self.time_fn()
        with self._locked_state() as state:
            entry = self._entry(state, key, now)
            open_until = float(entry.get("open_until", 0.0))
            if open_until > now:
                self.stats.circuit_open_count += 1
                return round(self.jitter_fn(open_until - now), 4)

            window_started_at = float(entry.get("window_started_at", now))
            used = float(entry.get("used", 0.0))
            if now - window_started_at >= window_seconds:
                window_started_at = now
                used = 0.0

            if used + cost <= capacity:
                entry["window_started_at"] = now if used == 0 else window_started_at
                entry["used"] = round(used + cost, 6)
                self.stats.reservation_count += 1
                return 0.0

            retry_after = max(window_seconds - (now - window_started_at), 0.0)
            return round(self.jitter_fn(retry_after), 4)

    def record_rate_limit(
        self,
        key: str,
        *,
        threshold: int,
        breaker_seconds: float,
    ) -> None:
        now = self.time_fn()
        with self._locked_state() as state:
            entry = self._entry(state, key, now)
            consecutive = int(entry.get("consecutive_rate_limits", 0)) + 1
            entry["consecutive_rate_limits"] = consecutive
            entry["last_rate_limit_at"] = now
            self.stats.rate_limit_events += 1
            if consecutive >= threshold:
                entry["open_until"] = round(now + breaker_seconds, 6)

    def record_success(self, key: str) -> None:
        now = self.time_fn()
        with self._locked_state() as state:
            entry = self._entry(state, key, now)
            entry["consecutive_rate_limits"] = 0
            entry["open_until"] = 0.0

    def _entry(self, state: dict[str, object], key: str, now: float) -> dict[str, object]:
        buckets = state.setdefault("buckets", {})
        assert isinstance(buckets, dict)
        return buckets.setdefault(
            key,
            {
                "window_started_at": now,
                "used": 0.0,
                "open_until": 0.0,
                "consecutive_rate_limits": 0,
                "last_rate_limit_at": 0.0,
            },
        )

    def _load_state(self) -> dict[str, object]:
        if not self.state_path.exists():
            return {"buckets": {}}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"buckets": {}}

    def _save_state(self, state: dict[str, object]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    def _locked_state(self):
        import fcntl

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+", encoding="utf-8")

        class _StateContext:
            def __enter__(inner_self):
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                inner_self.state = self._load_state()
                return inner_self.state

            def __exit__(inner_self, exc_type, exc, tb):
                if exc_type is None:
                    self._save_state(inner_self.state)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
                return False

        return _StateContext()


def jitter_seconds(base_seconds: float, max_jitter_seconds: float) -> float:
    if base_seconds <= 0 or max_jitter_seconds <= 0:
        return max(base_seconds, 0.0)
    return round(max(base_seconds + random.uniform(0.0, max_jitter_seconds), 0.0), 4)
