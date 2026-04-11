import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from app.hyperliquid.info_client import (
    HyperliquidInfoClient,
    apply_live_asset_leverage_caps,
    extract_max_leverage_by_symbol,
)
from app.live.errors import HyperliquidRateLimitError
from app.settings import load_config


class _FakeResponse:
    def __init__(self, payload: dict[str, object], status: int = 200) -> None:
        self.payload = payload
        self.status = status

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class HyperliquidHttpTests(unittest.TestCase):
    def test_extracts_max_leverage_by_symbol_from_meta_payload(self) -> None:
        payload = [
            {
                "universe": [
                    {"name": "BTC", "maxLeverage": 40, "marginTableId": 56},
                    {"name": "ETH", "maxLeverage": 25, "marginTableId": 55},
                    {"name": "MATIC", "maxLeverage": 20, "isDelisted": True},
                ]
            },
            [],
        ]

        caps = extract_max_leverage_by_symbol(payload, symbols=["BTC", "ETH", "MATIC"])

        self.assertEqual(caps, {"BTC": 40.0, "ETH": 25.0})

    def test_apply_live_asset_leverage_caps_merges_fetched_limits(self) -> None:
        config = load_config("config/trident.toml")

        with patch.object(
            HyperliquidInfoClient,
            "fetch_max_leverage_by_symbol",
            return_value={"ETH": 17.0},
        ):
            runtime = apply_live_asset_leverage_caps(
                config,
                symbols=["ETH"],
                sleep_fn=lambda _: None,
            )

        self.assertEqual(runtime.pod_a.max_leverage_by_symbol["ETH"], 17.0)
        self.assertEqual(runtime.pod_a.max_leverage_by_symbol["BTC"], 40.0)

    def test_retries_rate_limit_then_succeeds(self) -> None:
        config = load_config("config/trident.toml").hyperliquid
        with tempfile.TemporaryDirectory() as tmpdir:
            config.rate_limit_state_path = str(Path(tmpdir) / "rate_limits.json")
            client = HyperliquidInfoClient(config, sleep_fn=lambda _: None)
            responses = [
                HTTPError(
                    url=config.info_url,
                    code=429,
                    msg="Too Many Requests",
                    hdrs=None,
                    fp=io.BytesIO(b'{"error":"rate limit exceeded"}'),
                ),
                _FakeResponse({"status": "ok"}),
            ]

            def fake_urlopen(*args, **kwargs):
                response = responses.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response

            with patch("app.hyperliquid.info_client.request.urlopen", side_effect=fake_urlopen):
                payload = client.post_info({"type": "allMids"})

            self.assertEqual(payload["status"], "ok")
            self.assertEqual(client.stats.retry_count, 1)
            self.assertEqual(client.stats.rate_limit_count, 1)

    def test_fetch_all_mids_returns_prices(self) -> None:
        config = load_config("config/trident.toml").hyperliquid
        with tempfile.TemporaryDirectory() as tmpdir:
            config.rate_limit_state_path = str(Path(tmpdir) / "rate_limits.json")
            client = HyperliquidInfoClient(config, sleep_fn=lambda _: None)
            fake_payload = {"BTC": "60123.5", "ETH": "3100.0", "BAD": "invalid"}

            with patch(
                "app.hyperliquid.info_client.request.urlopen",
                return_value=_FakeResponse(fake_payload),
            ):
                result = client.fetch_all_mids()

            self.assertEqual(result, {"BTC": 60123.5, "ETH": 3100.0})

    def test_fetch_all_mids_handles_empty_response(self) -> None:
        config = load_config("config/trident.toml").hyperliquid
        with tempfile.TemporaryDirectory() as tmpdir:
            config.rate_limit_state_path = str(Path(tmpdir) / "rate_limits.json")
            client = HyperliquidInfoClient(config, sleep_fn=lambda _: None)

            with patch(
                "app.hyperliquid.info_client.request.urlopen",
                return_value=_FakeResponse("not a dict"),
            ):
                result = client.fetch_all_mids()

            self.assertEqual(result, {})

    def test_raises_rate_limit_error_after_exhaustion(self) -> None:
        config = load_config("config/trident.toml").hyperliquid
        with tempfile.TemporaryDirectory() as tmpdir:
            config.rate_limit_state_path = str(Path(tmpdir) / "rate_limits.json")
            client = HyperliquidInfoClient(config, sleep_fn=lambda _: None)

            def fake_urlopen(*args, **kwargs):
                raise HTTPError(
                    url=config.info_url,
                    code=429,
                    msg="Too Many Requests",
                    hdrs=None,
                    fp=io.BytesIO(b'{"error":"rate limit exceeded"}'),
                )

            with patch("app.hyperliquid.info_client.request.urlopen", side_effect=fake_urlopen):
                with self.assertRaises(HyperliquidRateLimitError):
                    client.post_info({"type": "allMids"}, max_attempts=2)


if __name__ == "__main__":
    unittest.main()
