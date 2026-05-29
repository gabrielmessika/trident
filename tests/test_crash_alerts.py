import os
import tempfile
import unittest
from pathlib import Path

from app.live.crash_alerts import notify_crash


class CrashAlertTests(unittest.TestCase):
    def test_crash_alert_is_disabled_without_recipient(self) -> None:
        old_env = dict(os.environ)
        try:
            for key in list(os.environ):
                if key.startswith("TRIDENT_CRASH_ALERT_"):
                    os.environ.pop(key, None)
            with tempfile.TemporaryDirectory() as tmpdir:
                sent = notify_crash(
                    service_name="test-service",
                    exc=RuntimeError("boom"),
                    state_path=Path(tmpdir) / "crash_alert_state.json",
                )
        finally:
            os.environ.clear()
            os.environ.update(old_env)

        self.assertFalse(sent)


if __name__ == "__main__":
    unittest.main()
