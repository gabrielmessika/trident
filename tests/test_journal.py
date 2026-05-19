import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from app.persistence.journal import JsonlJournal


class JournalTests(unittest.TestCase):
    def test_jsonl_journal_serializes_decimal_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "journal.jsonl"
            journal = JsonlJournal(path)

            journal.append({"event_type": "fill", "size": Decimal("0.00122")})
            journal.append_many([{"event_type": "fill", "size": Decimal("2")}])

            rows = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(rows[0]["size"], 0.00122)
            self.assertEqual(rows[1]["size"], 2)


if __name__ == "__main__":
    unittest.main()
