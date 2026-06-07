from __future__ import annotations

import json
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from app.trident_ai import (
    AgentMarketContext,
    AgentMarketContextBuildConfig,
    TridentAIFeatureBuilder,
    load_trident_ai_config,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "trident_ai" / "market_snapshots.json"


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _symbols() -> list[dict[str, object]]:
    symbols = _fixture()["symbols"]
    assert isinstance(symbols, list)
    return [deepcopy(item) for item in symbols if isinstance(item, dict)]


def _symbol_payload(symbol: str) -> dict[str, object]:
    for item in _symbols():
        if item.get("symbol") == symbol:
            return item
    raise AssertionError(f"missing fixture symbol {symbol}")


def _now() -> datetime:
    return datetime(2026, 6, 7, 12, 1, 0, tzinfo=timezone.utc)


class TridentAIFeatureBuilderTests(unittest.TestCase):
    def test_builds_market_context_for_allowed_symbol(self) -> None:
        builder = TridentAIFeatureBuilder()

        result = builder.build_context_from_mapping(
            _symbol_payload("BTC"),
            as_of="2026-06-07T12:00:00Z",
            regime="TrendExpansion",
            now=_now(),
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.reason, "accepted")
        self.assertIsNotNone(result.context)
        assert result.context is not None
        self.assertEqual(result.context.context_id, "market_BTC_20260607T120000Z")
        self.assertEqual(result.context.as_of, "2026-06-07T12:00:00Z")
        self.assertEqual(result.context.symbol, "BTC")
        self.assertEqual(result.context.regime, "TrendExpansion")
        self.assertEqual(result.context.features["ema_alignment"], "bullish")
        self.assertEqual(result.context.features["spread_bps"], 1.2)
        self.assertEqual(result.context.features["market_cluster"], "crypto")
        self.assertEqual(result.context.features["open_interest"], 123456789.0)

    def test_builds_only_initial_universe_from_snapshot_batch(self) -> None:
        builder = TridentAIFeatureBuilder()

        results = builder.build_contexts_from_mappings(
            _symbols(),
            as_of="2026-06-07T12:00:00Z",
            regime="TrendExpansion",
            now=_now(),
        )

        accepted = [result.context for result in results if result.accepted]
        rejected = [result for result in results if not result.accepted]
        self.assertEqual([context.symbol for context in accepted if context], ["BTC", "ETH", "SOL", "HYPE"])
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0].symbol, "XRP")
        self.assertEqual(rejected[0].reason, "symbol_not_allowed")

    def test_rejects_missing_required_snapshot_value(self) -> None:
        payload = _symbol_payload("BTC")
        payload.pop("price")
        builder = TridentAIFeatureBuilder()

        result = builder.build_context_from_mapping(
            payload,
            as_of="2026-06-07T12:00:00Z",
            now=_now(),
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "missing_field:price")

    def test_rejects_stale_snapshot_timestamp(self) -> None:
        builder = TridentAIFeatureBuilder(
            AgentMarketContextBuildConfig(max_snapshot_age_seconds=300.0)
        )

        result = builder.build_context_from_mapping(
            _symbol_payload("BTC"),
            as_of="2026-06-07T11:50:00Z",
            now=_now(),
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "snapshot_stale")

    def test_rejects_negative_spread(self) -> None:
        payload = _symbol_payload("BTC")
        payload["spread_bps"] = -1.0
        builder = TridentAIFeatureBuilder()

        result = builder.build_context_from_mapping(
            payload,
            as_of="2026-06-07T12:00:00Z",
            now=_now(),
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "invalid_spread")

    def test_context_serialization_is_stable(self) -> None:
        builder = TridentAIFeatureBuilder()

        result = builder.build_context_from_mapping(
            _symbol_payload("HYPE"),
            as_of="2026-06-07T12:00:00Z",
            regime="TrendExpansion",
            now=_now(),
        )

        self.assertTrue(result.accepted)
        assert result.context is not None
        payload = result.context.to_dict()
        restored = AgentMarketContext.from_mapping(payload)
        self.assertEqual(restored.to_dict(), payload)
        self.assertEqual(payload["context_id"], "market_HYPE_20260607T120000Z")
        self.assertEqual(payload["features"]["ema_alignment"], "bearish")

    def test_build_config_can_derive_from_trident_ai_config(self) -> None:
        config = load_trident_ai_config("config/trident_ai.toml")

        build_config = AgentMarketContextBuildConfig.from_trident_ai_config(config)

        self.assertEqual(build_config.allowed_symbols, ("BTC", "ETH", "SOL", "HYPE"))
        self.assertEqual(
            build_config.max_snapshot_age_seconds,
            config.risk.max_market_context_age_seconds,
        )


if __name__ == "__main__":
    unittest.main()
