from __future__ import annotations

import json
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from app.trident_ai import (
    AgentMarketContext,
    AgentProposalValidationConfig,
    TRIDENT_AI_INITIAL_SYMBOLS,
    validate_agent_proposal,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "trident_ai" / "initial_proposals.json"


def _load_fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _now() -> datetime:
    return datetime(2026, 6, 7, 12, 0, 0, tzinfo=timezone.utc)


def _proposal(symbol: str = "BTC") -> dict[str, object]:
    fixture = _load_fixture()
    proposals = fixture["proposals"]
    assert isinstance(proposals, list)
    for proposal in proposals:
        assert isinstance(proposal, dict)
        if proposal.get("symbol") == symbol:
            return deepcopy(proposal)
    raise AssertionError(f"missing fixture proposal for {symbol}")


def _market_context(symbol: str = "BTC") -> dict[str, object]:
    fixture = _load_fixture()
    contexts = fixture["market_contexts"]
    assert isinstance(contexts, list)
    for context in contexts:
        assert isinstance(context, dict)
        if context.get("symbol") == symbol:
            return deepcopy(context)
    raise AssertionError(f"missing fixture market context for {symbol}")


def _intel_digest() -> dict[str, object]:
    fixture = _load_fixture()
    digest = fixture["intel_digest"]
    assert isinstance(digest, dict)
    return deepcopy(digest)


class TridentAITypesTests(unittest.TestCase):
    def test_initial_universe_is_btc_eth_sol_hype(self) -> None:
        self.assertEqual(TRIDENT_AI_INITIAL_SYMBOLS, ("BTC", "ETH", "SOL", "HYPE"))

    def test_accepts_fixture_proposals_for_initial_universe(self) -> None:
        for symbol in TRIDENT_AI_INITIAL_SYMBOLS:
            with self.subTest(symbol=symbol):
                result = validate_agent_proposal(
                    _proposal(symbol),
                    market_context=_market_context(symbol),
                    intel_digest=_intel_digest(),
                    now=_now(),
                )

                self.assertTrue(result.accepted)
                self.assertEqual(result.reason, "accepted")
                self.assertIsNotNone(result.proposal)
                self.assertEqual(result.proposal.symbol, symbol)

    def test_rejects_symbol_outside_whitelist(self) -> None:
        proposal = _proposal("BTC")
        proposal["symbol"] = "XRP"

        result = validate_agent_proposal(proposal, now=_now())

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "invalid_symbol")

    def test_rejects_incomplete_json(self) -> None:
        proposal = _proposal("BTC")
        proposal.pop("confidence")

        result = validate_agent_proposal(proposal, now=_now())

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "missing_field:confidence")

    def test_rejects_open_without_stop(self) -> None:
        proposal = _proposal("BTC")
        proposal["stop_bps"] = 0.0

        result = validate_agent_proposal(proposal, now=_now())

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "stop_required")

    def test_rejects_stale_market_context(self) -> None:
        context = _market_context("BTC")
        context["as_of"] = "2026-06-07T11:40:00Z"

        result = validate_agent_proposal(
            _proposal("BTC"),
            market_context=context,
            now=_now(),
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "market_context_stale")

    def test_rejects_notional_above_cap(self) -> None:
        proposal = _proposal("BTC")
        proposal["max_notional_usd"] = 26.0

        result = validate_agent_proposal(proposal, now=_now())

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "notional_above_cap")

    def test_rejects_low_confidence(self) -> None:
        proposal = _proposal("BTC")
        proposal["confidence"] = 0.54

        result = validate_agent_proposal(proposal, now=_now())

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "confidence_below_min")

    def test_accepts_hold_with_zero_notional_and_low_confidence(self) -> None:
        proposal = _proposal("BTC")
        proposal.update(
            {
                "action": "hold",
                "confidence": 0.0,
                "max_notional_usd": 0.0,
                "max_leverage": 0.0,
                "entry_style": "none",
                "invalidation_price": 0.0,
                "stop_bps": 0.0,
                "take_profit_bps": 0.0,
                "time_stop_minutes": 0,
            }
        )

        result = validate_agent_proposal(
            proposal,
            market_context=_market_context("BTC"),
            now=_now(),
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.reason, "accepted")

    def test_rejects_hold_with_negative_notional(self) -> None:
        proposal = _proposal("BTC")
        proposal["action"] = "hold"
        proposal["max_notional_usd"] = -1.0

        result = validate_agent_proposal(proposal, now=_now())

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "invalid_notional")

    def test_rejects_missing_evidence(self) -> None:
        proposal = _proposal("BTC")
        proposal["evidence_ids"] = []

        result = validate_agent_proposal(proposal, now=_now())

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "evidence_required")

    def test_rejects_expired_proposal(self) -> None:
        proposal = _proposal("BTC")
        proposal["valid_until"] = "2026-06-07T11:59:59Z"

        result = validate_agent_proposal(proposal, now=_now())

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "proposal_expired")

    def test_rejects_market_context_symbol_mismatch(self) -> None:
        result = validate_agent_proposal(
            _proposal("BTC"),
            market_context=_market_context("ETH"),
            now=_now(),
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "market_context_symbol_mismatch")

    def test_custom_config_can_reduce_allowed_universe(self) -> None:
        config = AgentProposalValidationConfig(allowed_symbols=("BTC",))

        result = validate_agent_proposal(_proposal("HYPE"), config=config, now=_now())

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "invalid_symbol")

    def test_market_context_accepts_json_feature_digest(self) -> None:
        context = AgentMarketContext.from_mapping(
            {
                "schema_version": "trident_ai_market_context_v1",
                "context_id": "market_BTC_20260607T120000Z",
                "as_of": "2026-06-07T12:00:00Z",
                "symbol": "btc",
                "price": 60000.0,
                "regime": "TrendExpansion",
                "features": {
                    "ema_alignment": "bullish",
                    "technical_digest": {
                        "coverage": {"used_count": 50, "missing_count": 0},
                        "top_signals": [{"id": "ma_stack_bullish", "strength": 0.7}],
                    },
                },
                "source": "unit_test",
            }
        )

        self.assertEqual(context.symbol, "BTC")
        self.assertEqual(context.features["technical_digest"]["coverage"]["used_count"], 50)

    def test_market_context_rejects_non_json_feature_value(self) -> None:
        with self.assertRaises(ValueError):
            AgentMarketContext.from_mapping(
                {
                    "schema_version": "trident_ai_market_context_v1",
                    "context_id": "market_BTC_20260607T120000Z",
                    "as_of": "2026-06-07T12:00:00Z",
                    "symbol": "BTC",
                    "price": 60000.0,
                    "regime": "TrendExpansion",
                    "features": {"technical_digest": object()},
                    "source": "unit_test",
                }
            )


if __name__ == "__main__":
    unittest.main()
