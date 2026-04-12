import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.hyperliquid.funding_client import HyperliquidFundingClient, extract_current_funding
from app.live.funding_collector import FundingHistoryCollector
from app.live.runtime_status import load_runtime_status
from app.settings import load_config


class _FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.status = 200

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FundingClientTests(unittest.TestCase):
    def test_extract_current_funding_from_meta_payload(self) -> None:
        payload = [
            {
                "universe": [
                    {"name": "BTC", "maxLeverage": 40},
                    {"name": "ETH", "maxLeverage": 25, "isDelisted": True},
                ]
            },
            [
                {
                    "funding": "-0.00012",
                    "openInterest": "12.5",
                    "markPx": "70000.0",
                    "oraclePx": "70010.0",
                    "premium": "-0.0004",
                    "dayNtlVlm": "12345.0",
                    "dayBaseVlm": "1.23",
                },
                {
                    "funding": "0.00021",
                    "openInterest": "42.0",
                    "markPx": "3500.0",
                    "oraclePx": "3498.0",
                    "premium": "0.0003",
                    "dayNtlVlm": "555.0",
                    "dayBaseVlm": "0.5",
                },
            ],
        ]

        snapshots = extract_current_funding(payload)

        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].symbol, "BTC")
        self.assertAlmostEqual(snapshots[0].funding_rate, -0.00012)
        self.assertAlmostEqual(snapshots[0].open_interest, 12.5)

    def test_funding_collector_writes_jsonl_records(self) -> None:
        config = load_config("config/trident.toml")

        class _FakeClient:
            def fetch_current_funding(self, **_: object):
                return extract_current_funding(
                    [
                        {"universe": [{"name": "BTC"}]},
                        [{"funding": "-0.0001", "openInterest": "10", "markPx": "70000"}],
                    ]
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "funding.jsonl"
            records = FundingHistoryCollector(config, client=_FakeClient()).collect_once(
                output_path=output_path,
                symbols=["BTC"],
                timestamp="2026-04-07T12:00:00Z",
            )

            self.assertEqual(len(records), 1)
            self.assertTrue(output_path.exists())
            payload = json.loads(output_path.read_text(encoding="utf-8").strip())
            self.assertEqual(payload["symbol"], "BTC")
            self.assertEqual(payload["timestamp"], "2026-04-07T12:00:00Z")

    def test_funding_collector_writes_runtime_status(self) -> None:
        config = load_config("config/trident.toml")

        class _FakeClient:
            def fetch_current_funding(self, **_: object):
                return extract_current_funding(
                    [
                        {"universe": [{"name": "BTC"}]},
                        [{"funding": "-0.0001", "openInterest": "10", "markPx": "70000"}],
                    ]
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "funding.jsonl"
            status_path = Path(tmpdir) / "funding_status.json"
            stats = FundingHistoryCollector(config, client=_FakeClient()).run(
                output_path=output_path,
                status_path=status_path,
                poll_seconds=0.0,
                iterations=1,
                symbols=["BTC"],
            )

            self.assertEqual(stats.polls_completed, 1)
            payload = load_runtime_status(status_path)
            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertEqual(payload["service"], "funding_collector")
            self.assertEqual(payload["process_state"], "completed")
            self.assertEqual(payload["symbol_count"], 1)
            self.assertEqual(payload["records_written"], 1)

    def test_fetch_current_funding_merges_builder_dex_symbols(self) -> None:
        config = load_config("config/trident.toml").hyperliquid
        with tempfile.TemporaryDirectory() as tmpdir:
            config.rate_limit_state_path = str(Path(tmpdir) / "rate_limits.json")
            client = HyperliquidFundingClient(config, sleep_fn=lambda _: None)

            def fake_urlopen(req, *args, **kwargs):
                body = json.loads(req.data.decode("utf-8"))
                if body == {"type": "metaAndAssetCtxs"}:
                    return _FakeResponse(
                        [
                            {"universe": [{"name": "ETH", "maxLeverage": 25}]},
                            [{"funding": "-0.0001", "openInterest": "12", "markPx": "3100"}],
                        ]
                    )
                if body == {"type": "metaAndAssetCtxs", "dex": "xyz"}:
                    return _FakeResponse(
                        [
                            {"universe": [{"name": "xyz:SP500", "maxLeverage": 50}]},
                            [{"funding": "0.0002", "openInterest": "7", "markPx": "6700"}],
                        ]
                    )
                raise AssertionError(f"unexpected payload: {body}")

            with patch("app.hyperliquid.info_client.request.urlopen", side_effect=fake_urlopen):
                snapshots = client.fetch_current_funding(symbols=["ETH", "XYZ:SP500"])

            self.assertEqual([item.symbol for item in snapshots], ["ETH", "XYZ:SP500"])
            self.assertAlmostEqual(snapshots[1].funding_rate, 0.0002)


if __name__ == "__main__":
    unittest.main()
