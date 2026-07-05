from __future__ import annotations

import gzip
import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.trident_disk_retention import main


class DiskRetentionTests(unittest.TestCase):
    def test_prunes_old_snapshots_and_preserves_recent_state(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "trident"
            snapshot_dir = root / "data" / "live_snapshots"
            runtime_dir = root / "runtime" / "trident"
            snapshot_dir.mkdir(parents=True)
            runtime_dir.mkdir(parents=True)
            old_snapshot = snapshot_dir / "2026-01-01.jsonl"
            recent_snapshot = snapshot_dir / "2026-07-05.jsonl"
            state_file = runtime_dir / "live_state_pod_a.json"
            old_snapshot.write_text("{}\n", encoding="utf-8")
            recent_snapshot.write_text("{}\n", encoding="utf-8")
            state_file.write_text('{"positions": []}\n', encoding="utf-8")
            old_mtime = time.time() - (40 * 86400)
            os.utime(old_snapshot, (old_mtime, old_mtime))

            code = main(
                [
                    "--scope",
                    "trident",
                    "--trident-root",
                    str(root),
                    "--snapshot-days",
                    "21",
                    "--apply",
                    "--print-limit",
                    "0",
                ]
            )

            self.assertEqual(code, 0)
            self.assertFalse(old_snapshot.exists())
            self.assertTrue(recent_snapshot.exists())
            self.assertTrue(state_file.exists())
            manifest = root / "logs" / "retention_runs.jsonl"
            payload = json.loads(manifest.read_text(encoding="utf-8").splitlines()[-1])
            self.assertGreater(payload["bytes_reclaimed"], 0)

    def test_rotates_large_hip4_market_observation_log_to_gzip(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "hip4"
            log_dir = root / "logs" / "hip4_outcome_mainnet"
            log_dir.mkdir(parents=True)
            source = log_dir / "market_observations.jsonl"
            source.write_text('{"ts":"2026-07-05T00:00:00Z"}\n' * 200, encoding="utf-8")

            code = main(
                [
                    "--scope",
                    "hip4",
                    "--hip4-root",
                    str(root),
                    "--hip4-market-max-mb",
                    "0.001",
                    "--apply",
                    "--print-limit",
                    "0",
                ]
            )

            self.assertEqual(code, 0)
            self.assertEqual(source.read_text(encoding="utf-8"), "")
            archives = list((root / "logs" / "retention_archive").rglob("market_observations.jsonl.gz"))
            self.assertEqual(len(archives), 1)
            with gzip.open(archives[0], "rt", encoding="utf-8") as handle:
                archived = handle.read()
            self.assertIn("2026-07-05T00:00:00Z", archived)


if __name__ == "__main__":
    unittest.main()
