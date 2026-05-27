from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from app.trident.hip4_outcome.config import Hip4OutcomeConfig
from app.trident.hip4_outcome.models import BookLevel
from app.trident.hip4_outcome.nautilus_shadow import (
    NautilusBookSnapshot,
    NautilusShadowConfig,
    collect_nautilus_shadow_once,
    nautilus_instrument_id_to_outcome_coin,
    outcome_coin_to_nautilus_instrument_id,
)


class FakeInfoClient:
    def fetch_outcome_meta(self) -> dict[str, object]:
        return {
            "outcomes": [
                {
                    "outcome": 25,
                    "name": "BTC daily",
                    "description": (
                        "class:priceBinary|underlying:BTC|expiry:20260527-0600|"
                        "targetPrice:100000|period:1d"
                    ),
                    "sideSpecs": [{"name": "Yes"}, {"name": "No"}],
                }
            ]
        }

    def fetch_l2_book(self, coin: str) -> dict[str, object]:
        return {
            "coin": coin,
            "time": 1_800_000_000_000,
            "levels": [
                [{"px": "0.48", "sz": "12", "n": 1}],
                [{"px": "0.52", "sz": "10", "n": 1}],
            ],
        }


def fake_nautilus_book_source(markets, config, hip4_config):  # type: ignore[no-untyped-def]
    del config, hip4_config
    snapshots = {}
    for market in markets:
        snapshots[market.yes_coin] = NautilusBookSnapshot(
            instrument_id=outcome_coin_to_nautilus_instrument_id(market.yes_coin),
            coin=market.yes_coin,
            bids=(BookLevel(price=0.4801, size=12),),
            asks=(BookLevel(price=0.5201, size=10),),
            ts_event_ns=1_800_000_000_000_000_000,
            ts_init_ns=1_800_000_000_010_000_000,
            received_ns=1_800_000_000_020_000_000,
            update_count=3,
        )
        snapshots[market.no_coin] = NautilusBookSnapshot(
            instrument_id=outcome_coin_to_nautilus_instrument_id(market.no_coin),
            coin=market.no_coin,
            bids=(BookLevel(price=0.4799, size=12),),
            asks=(BookLevel(price=0.5199, size=10),),
            ts_event_ns=1_800_000_000_000_000_000,
            ts_init_ns=1_800_000_000_010_000_000,
            received_ns=1_800_000_000_020_000_000,
            update_count=4,
        )
    return snapshots, {"source": "fake_nautilus_ws", "snapshot_count": len(snapshots)}


class HIP4NautilusShadowTests(unittest.TestCase):
    def test_coin_to_nautilus_instrument_id(self) -> None:
        self.assertEqual(outcome_coin_to_nautilus_instrument_id("#250"), "+250.HYPERLIQUID")
        self.assertEqual(outcome_coin_to_nautilus_instrument_id("+251"), "+251.HYPERLIQUID")
        self.assertEqual(nautilus_instrument_id_to_outcome_coin("+250.HYPERLIQUID"), "#250")
        with self.assertRaises(ValueError):
            outcome_coin_to_nautilus_instrument_id("#abc")

    def test_shadow_once_writes_read_only_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = NautilusShadowConfig(
                enabled=True,
                logs_dir=str(root / "logs"),
                state_path=str(root / "runtime" / "state.json"),
                include_underlyings=("BTC",),
                max_markets=1,
            )
            payload = collect_nautilus_shadow_once(
                config,
                Hip4OutcomeConfig(),
                info_client=FakeInfoClient(),  # type: ignore[arg-type]
                now_fn=lambda: "2026-05-27T00:00:00Z",
                capabilities={
                    "available": True,
                    "version": "test",
                    "outcome_supported": True,
                    "modules": {},
                    "product_types": ["OUTCOME"],
                    "error": None,
                },
                nautilus_book_source=fake_nautilus_book_source,
            )

            self.assertTrue(payload["shadow_ready"])
            self.assertTrue(payload["read_only"])
            self.assertEqual(payload["instrument_count"], 2)
            self.assertEqual(payload["nautilus_book_source"]["source"], "fake_nautilus_ws")
            instruments = self._read_jsonl(root / "logs" / "instruments.jsonl")
            books = self._read_jsonl(root / "logs" / "book_snapshots.jsonl")
            quality = self._read_csv(root / "logs" / "data_quality.csv")
            parity = self._read_csv(root / "logs" / "parity_compare.csv")
            status = json.loads((root / "logs" / "status.json").read_text())

            self.assertEqual(instruments[0]["instrument_id"], "+250.HYPERLIQUID")
            self.assertEqual(books[0]["source"], "nautilus_hyperliquid_ws")
            self.assertEqual(quality[0]["market_id"], "BTC_GT_100000_20260527_0600")
            self.assertEqual(quality[0]["tradable_window"], "true")
            self.assertEqual(quality[0]["book_update_count_5s"], "7")
            self.assertEqual(parity[0]["nautilus_bid"], "0.4801")
            self.assertEqual(parity[0]["verdict"], "match_lt_10bps")
            self.assertEqual(status["pod"], "hip4_nautilus_shadow")

    def test_shadow_rejects_order_capability(self) -> None:
        config = NautilusShadowConfig(enabled=True, allow_orders=True)
        with self.assertRaises(ValueError):
            collect_nautilus_shadow_once(
                config,
                Hip4OutcomeConfig(),
                info_client=FakeInfoClient(),  # type: ignore[arg-type]
                capabilities={
                    "available": True,
                    "outcome_supported": True,
                    "error": None,
                },
            )

    def _read_csv(self, path: Path) -> list[dict[str, str]]:
        with path.open(newline="", encoding="utf-8") as handle:
            return [dict(row) for row in csv.DictReader(handle)]

    def _read_jsonl(self, path: Path) -> list[dict[str, object]]:
        with path.open(encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]


if __name__ == "__main__":
    unittest.main()
