import tempfile
import unittest
from pathlib import Path

from app.hyperliquid.rate_limiter import SharedRateLimiter


class SharedRateLimiterTests(unittest.TestCase):
    def test_reserve_blocks_until_window_resets_across_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            now = {"value": 100.0}
            state_path = Path(tmpdir) / "rate_limits.json"

            limiter_a = SharedRateLimiter(state_path, time_fn=lambda: now["value"])
            limiter_b = SharedRateLimiter(state_path, time_fn=lambda: now["value"])

            self.assertEqual(
                limiter_a.reserve("http_info", capacity=1, window_seconds=60.0),
                0.0,
            )
            self.assertGreater(
                limiter_b.reserve("http_info", capacity=1, window_seconds=60.0),
                0.0,
            )

            now["value"] += 60.0
            self.assertEqual(
                limiter_b.reserve("http_info", capacity=1, window_seconds=60.0),
                0.0,
            )

    def test_rate_limit_event_opens_circuit_for_other_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            now = {"value": 100.0}
            state_path = Path(tmpdir) / "rate_limits.json"

            limiter_a = SharedRateLimiter(state_path, time_fn=lambda: now["value"])
            limiter_b = SharedRateLimiter(state_path, time_fn=lambda: now["value"])

            limiter_a.record_rate_limit("ws_connect", threshold=2, breaker_seconds=30.0)
            limiter_a.record_rate_limit("ws_connect", threshold=2, breaker_seconds=30.0)

            wait_seconds = limiter_b.reserve("ws_connect", capacity=10, window_seconds=60.0)
            self.assertGreaterEqual(wait_seconds, 30.0)

            now["value"] += 31.0
            self.assertEqual(
                limiter_b.reserve("ws_connect", capacity=10, window_seconds=60.0),
                0.0,
            )


if __name__ == "__main__":
    unittest.main()
