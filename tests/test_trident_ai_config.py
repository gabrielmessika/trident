from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.trident_ai import (
    TRIDENT_AI_INITIAL_SYMBOLS,
    TridentAIConfigError,
    load_trident_ai_config,
)


class TridentAIConfigTests(unittest.TestCase):
    def test_default_config_loads_shadow_first_defaults(self) -> None:
        config = load_trident_ai_config("config/trident_ai.toml")

        self.assertFalse(config.enabled)
        self.assertEqual(config.mode, "shadow")
        self.assertEqual(config.tradable_symbols, ("BTC", "ETH", "SOL", "HYPE"))
        self.assertEqual(config.tradable_symbols, TRIDENT_AI_INITIAL_SYMBOLS)
        self.assertTrue(config.require_independent_hyperliquid_account)
        self.assertEqual(config.max_monthly_ai_budget_usd, 30.0)
        self.assertEqual(config.decision_interval_seconds, 900)
        self.assertEqual(config.max_symbols_per_cycle, 5)
        self.assertEqual(config.paths.runtime_dir, "./runtime/trident_ai")
        self.assertEqual(config.paths.llm_cache_dir, "./runtime/trident_ai/llm_cache")
        self.assertEqual(config.paths.shadow_journal_path, "./logs/trident_ai_shadow.jsonl")
        self.assertEqual(config.risk.live_max_order_notional_usd, 25.0)
        self.assertEqual(config.risk.max_daily_loss_usd, 5.0)
        self.assertEqual(config.risk.max_open_positions, 1)
        self.assertEqual(config.risk.max_trades_per_day, 3)
        self.assertEqual(config.risk.max_leverage, 1.0)
        self.assertTrue(config.risk.require_stop)
        self.assertTrue(config.risk.require_evidence)
        self.assertEqual(config.llm.provider, "openai")
        self.assertEqual(config.llm.model, "gpt-5.4-mini")
        self.assertEqual(config.llm.verifier_provider, "openai")
        self.assertEqual(config.llm.verifier_model, "gpt-5.4")
        self.assertTrue(config.llm.cache_enabled)

    def test_builds_proposal_validation_config_from_risk_caps(self) -> None:
        config = load_trident_ai_config("config/trident_ai.toml")

        validation_config = config.proposal_validation_config()

        self.assertEqual(validation_config.allowed_symbols, config.tradable_symbols)
        self.assertEqual(validation_config.min_confidence, config.risk.min_confidence)
        self.assertEqual(
            validation_config.max_notional_usd,
            config.risk.live_max_order_notional_usd,
        )
        self.assertEqual(validation_config.max_leverage, config.risk.max_leverage)
        self.assertTrue(validation_config.require_stop)
        self.assertTrue(validation_config.require_evidence)

    def test_rejects_symbol_outside_initial_universe(self) -> None:
        path = self._write_config(
            """
            [trident_ai]
            tradable_symbols = ["BTC", "XRP"]
            """
        )

        with self.assertRaises(TridentAIConfigError):
            load_trident_ai_config(path)

    def test_rejects_execution_mode_without_independent_account(self) -> None:
        path = self._write_config(
            """
            [trident_ai]
            mode = "live"
            require_independent_hyperliquid_account = false
            """
        )

        with self.assertRaises(TridentAIConfigError):
            load_trident_ai_config(path)

    def test_allows_reduced_initial_universe(self) -> None:
        path = self._write_config(
            """
            [trident_ai]
            tradable_symbols = ["BTC", "ETH"]
            """
        )

        config = load_trident_ai_config(path)

        self.assertEqual(config.tradable_symbols, ("BTC", "ETH"))
        self.assertEqual(config.proposal_validation_config().allowed_symbols, ("BTC", "ETH"))

    def _write_config(self, body: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "trident_ai.toml"
        path.write_text(body, encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main()
