import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.research.memecoin_exhaustive_research import MemecoinExhaustiveResearchRunner
from tests.test_memecoin_concept_research import _record


class MemecoinExhaustiveResearchTests(unittest.TestCase):
    def test_runner_keeps_event_momentum_family_when_holdout_survives(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / "snapshots.jsonl"
            records = []
            current = datetime(2026, 4, 5, 10, 0, tzinfo=timezone.utc)
            price = 10.0
            for _ in range(40):
                records.append(
                    _record(
                        current.isoformat().replace("+00:00", "Z"),
                        price,
                        "baseline",
                    )
                )
                current += timedelta(minutes=1)
                records.append(
                    _record(
                        current.isoformat().replace("+00:00", "Z"),
                        price + 0.30,
                        "event",
                    )
                )
                current += timedelta(minutes=1)
                records.append(
                    _record(
                        current.isoformat().replace("+00:00", "Z"),
                        price + 0.60,
                        "follow",
                    )
                )
                current += timedelta(minutes=1)
                price += 0.70
            input_path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )

            result = MemecoinExhaustiveResearchRunner().run(
                input_path=input_path,
                train_end_date="2026-04-05",
                validation_start_date="2026-04-05",
                horizons=[1],
            )

            summaries = {item["trigger_kind"]: item for item in result.trigger_summaries}
            self.assertEqual(summaries["event_momentum"]["final_decision"], "keep")


if __name__ == "__main__":
    unittest.main()
