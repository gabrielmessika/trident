import json
import tempfile
import unittest
from pathlib import Path

from app.backtest.live_parity_replay import TridentLiveParityReplayRunner
from app.settings import load_config


def _record(timestamp: str, symbols: list[str], *, stream_source: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "timestamp": timestamp,
        "regime_snapshot": {
            "ready": True,
            "adx": 20.0,
            "atr_ratio": 1.0,
            "range_width_bps": 100.0,
            "structure_score": 0.4,
            "btc_impulse": False,
        },
        "symbols": [
            {
                "symbol": symbol,
                "price": 100.0,
                "ema_fast": 100.0,
                "ema_slow": 100.0,
                "vwap_distance_bps": 0.0,
                "structure_score": 0.4,
                "funding_rate": 0.0,
                "spread_bps": 1.0,
                "btc_aligned": True,
            }
            for symbol in symbols
        ],
    }
    if stream_source is not None:
        payload["stream_source"] = stream_source
    return payload


class _FakeReport:
    def __init__(self) -> None:
        self.records_processed = 0

    def to_dict(self) -> dict[str, object]:
        return {"records_processed": self.records_processed}


class _FakeRunner:
    def __init__(self, config, *, coins: list[str], snapshot_stream_source: str) -> None:
        self.config = config
        self.coins = coins
        self.snapshot_stream_source = snapshot_stream_source
        self.report = _FakeReport()
        self.timestamps: list[str] = []

    def _process_record(self, record: dict[str, object], journal=None) -> None:
        self.timestamps.append(str(record.get("timestamp")))
        self.report.records_processed += 1

    def _build_open_positions_payload(self) -> list[dict[str, object]]:
        return []


def _pod_a_factory(config, coins=None):
    return _FakeRunner(
        config,
        coins=["BTC", "ETH", "XYZ:CL"],
        snapshot_stream_source="pod_a_live",
    )


def _pod_b_factory(config, coins=None):
    return _FakeRunner(
        config,
        coins=["BTC", "ETH"],
        snapshot_stream_source="pod_b_live",
    )


def _pod_c_factory(config, coins=None):
    return _FakeRunner(
        config,
        coins=["XYZ:CL"],
        snapshot_stream_source="pod_c_live",
    )


class LiveParityReplayTests(unittest.TestCase):
    def test_routes_records_using_explicit_stream_source(self) -> None:
        config = load_config("config/trident.toml")
        records = [
            _record("2026-04-07T00:00:00Z", ["BTC"], stream_source="pod_a_live"),
            _record("2026-04-07T00:00:00Z", ["XYZ:CL"], stream_source="pod_c_live"),
            _record("2026-04-07T00:01:00Z", ["ETH"], stream_source="pod_a_live"),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "live_parity.jsonl"
            input_path.write_text(
                "".join(json.dumps(item) + "\n" for item in records),
                encoding="utf-8",
            )
            result = TridentLiveParityReplayRunner(
                config,
                pod_a_runner_factory=_pod_a_factory,
                pod_b_runner_factory=_pod_b_factory,
                pod_c_runner_factory=_pod_c_factory,
            ).run_jsonl(input_path)

        self.assertEqual(result.records_processed, 3)
        self.assertEqual(result.unmatched_record_count, 0)
        self.assertEqual(result.records_routed_by_stream["pod_a_live"], 2)
        self.assertEqual(result.records_routed_by_stream["pod_c_live"], 1)
        self.assertEqual(result.pod_a["report"]["records_processed"], 2)
        self.assertEqual(result.pod_c["report"]["records_processed"], 1)

    def test_infers_legacy_streams_from_symbol_sets(self) -> None:
        config = load_config("config/trident.toml")
        records = [
            _record("2026-04-07T00:00:00Z", ["BTC", "ETH", "XYZ:CL"]),
            _record("2026-04-07T00:00:00Z", ["XYZ:CL"]),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "live_parity_legacy.jsonl"
            input_path.write_text(
                "".join(json.dumps(item) + "\n" for item in records),
                encoding="utf-8",
            )
            result = TridentLiveParityReplayRunner(
                config,
                pod_a_runner_factory=_pod_a_factory,
                pod_b_runner_factory=_pod_b_factory,
                pod_c_runner_factory=_pod_c_factory,
            ).run_jsonl(input_path)

        self.assertEqual(result.records_processed, 2)
        self.assertEqual(result.unmatched_record_count, 0)
        self.assertEqual(result.records_routed_by_inference["exact_symbol_set"], 2)
        self.assertEqual(result.records_routed_by_stream["pod_a_live"], 1)
        self.assertEqual(result.records_routed_by_stream["pod_c_live"], 1)


if __name__ == "__main__":
    unittest.main()
