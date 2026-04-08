import json
import tempfile
import unittest
from pathlib import Path

from app.backtest.candle_converter import CandleToSnapshotConverter


class CandleConverterTests(unittest.TestCase):
    def _make_candle_file(self, coin_dir: Path, date: str, candles: list[dict]) -> None:
        path = coin_dir / f"{date}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            for c in candles:
                f.write(json.dumps(c) + "\n")

    def _make_funding_file(self, coin_dir: Path, date: str, records: list[dict]) -> None:
        path = coin_dir / f"{date}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def test_converts_candles_to_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            candle_dir = base / "candles"
            output = base / "snapshots" / "2026-01-01.jsonl"

            btc_candles = [
                {"t": 1_767_225_600_000, "o": "50000", "h": "50100", "l": "49900", "c": "50050", "v": "10.5", "n": 150},
                {"t": 1_767_229_200_000, "o": "50050", "h": "50200", "l": "49950", "c": "50150", "v": "12.3", "n": 180},
            ]
            eth_candles = [
                {"t": 1_767_225_600_000, "o": "2500", "h": "2510", "l": "2490", "c": "2505", "v": "100", "n": 300},
            ]

            self._make_candle_file(candle_dir / "1h" / "BTC", "2026-01-01", btc_candles)
            self._make_candle_file(candle_dir / "1h" / "ETH", "2026-01-01", eth_candles)

            converter = CandleToSnapshotConverter()
            written = converter.convert(
                candle_dir=candle_dir,
                date="2026-01-01",
                coins=["BTC", "ETH"],
                interval="1h",
                output_path=output,
            )

            self.assertEqual(written, 2)
            lines = [json.loads(l) for l in output.read_text().strip().split("\n")]
            self.assertEqual(len(lines), 2)

            # First snapshot has both BTC and ETH
            first = lines[0]
            self.assertIn("timestamp", first)
            self.assertIn("regime_snapshot", first)
            self.assertIn("symbols", first)
            self.assertEqual(len(first["symbols"]), 2)

            # Second snapshot has only BTC (ETH has no candle at that time)
            second = lines[1]
            self.assertEqual(len(second["symbols"]), 1)
            self.assertEqual(second["symbols"][0]["symbol"], "BTC")

            # Check snapshot structure
            btc_sym = first["symbols"][0]
            self.assertEqual(btc_sym["symbol"], "BTC")
            self.assertAlmostEqual(btc_sym["price"], 50050.0)
            self.assertIn("ema_fast", btc_sym)
            self.assertIn("ema_slow", btc_sym)
            self.assertIn("funding_rate", btc_sym)
            self.assertIn("spread_bps", btc_sym)
            self.assertIn("structure_score", btc_sym)
            self.assertEqual(btc_sym["source"], "candle_funding_converter")

            # Regime snapshot
            regime = first["regime_snapshot"]
            self.assertTrue(regime["ready"])
            self.assertIn("adx", regime)
            self.assertIn("atr_ratio", regime)
            self.assertIn("structure_score", regime)
            self.assertIn("btc_impulse", regime)

    def test_integrates_funding_rates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            candle_dir = base / "candles"
            funding_dir = base / "funding"
            output = base / "snapshots" / "2026-01-01.jsonl"

            btc_candles = [
                {"t": 1_767_225_600_000, "o": "50000", "h": "50100", "l": "49900", "c": "50050", "v": "10", "n": 100},
            ]
            btc_funding = [
                {"time": 1_767_225_600_000, "coin": "BTC", "fundingRate": "0.0005", "premium": "0.001"},
            ]

            self._make_candle_file(candle_dir / "1h" / "BTC", "2026-01-01", btc_candles)
            self._make_funding_file(funding_dir / "BTC", "2026-01-01", btc_funding)

            converter = CandleToSnapshotConverter()
            written = converter.convert(
                candle_dir=candle_dir,
                funding_dir=funding_dir,
                date="2026-01-01",
                coins=["BTC"],
                interval="1h",
                output_path=output,
            )

            self.assertEqual(written, 1)
            snap = json.loads(output.read_text().strip())
            btc = snap["symbols"][0]
            self.assertAlmostEqual(btc["funding_rate"], 0.0005)

    def test_empty_candles_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            candle_dir = base / "candles"
            candle_dir.mkdir(parents=True)
            output = base / "snapshots" / "2026-01-01.jsonl"

            converter = CandleToSnapshotConverter()
            written = converter.convert(
                candle_dir=candle_dir,
                date="2026-01-01",
                coins=["BTC"],
                interval="1h",
                output_path=output,
            )

            self.assertEqual(written, 0)

    def test_btc_alignment_calculated(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            candle_dir = base / "candles"
            output = base / "snapshots" / "2026-01-01.jsonl"

            # BTC goes up, ETH goes down → not aligned
            btc_candles = [
                {"t": 1_767_225_600_000, "o": "50000", "h": "50100", "l": "49900", "c": "50050", "v": "10", "n": 100},
                {"t": 1_767_229_200_000, "o": "50050", "h": "50500", "l": "50000", "c": "50400", "v": "10", "n": 100},
            ]
            eth_candles = [
                {"t": 1_767_225_600_000, "o": "2500", "h": "2510", "l": "2490", "c": "2505", "v": "100", "n": 300},
                {"t": 1_767_229_200_000, "o": "2505", "h": "2510", "l": "2450", "c": "2460", "v": "100", "n": 300},
            ]

            self._make_candle_file(candle_dir / "1h" / "BTC", "2026-01-01", btc_candles)
            self._make_candle_file(candle_dir / "1h" / "ETH", "2026-01-01", eth_candles)

            converter = CandleToSnapshotConverter()
            converter.convert(
                candle_dir=candle_dir,
                date="2026-01-01",
                coins=["BTC", "ETH"],
                interval="1h",
                output_path=output,
            )

            lines = [json.loads(l) for l in output.read_text().strip().split("\n")]
            # Second snapshot: BTC up, ETH down
            second = lines[1]
            eth_sym = next(s for s in second["symbols"] if s["symbol"] == "ETH")
            self.assertFalse(eth_sym["btc_aligned"])


if __name__ == "__main__":
    unittest.main()
