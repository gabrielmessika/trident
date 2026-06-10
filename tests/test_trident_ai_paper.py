from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.trident_ai import (
    LLM_REPLAY_DECISION_EVENT,
    PAPER_REPLAY_FILL_EVENT,
    PAPER_REPLAY_TRADE_CLOSED_EVENT,
    load_trident_ai_config,
    run_trident_ai_paper_replay,
)


class TridentAIPaperReplayTests(unittest.TestCase):
    def test_paper_replay_opens_and_closes_on_take_profit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            input_path = directory / "llm_replay.jsonl"
            journal_path = directory / "paper.jsonl"
            report_json_path = directory / "paper.json"
            report_md_path = directory / "paper.md"
            records = [
                _decision_record(
                    symbol="BTC",
                    timestamp="2026-06-07T12:00:00Z",
                    price=100.0,
                    action="open",
                    decision_id="btc_open",
                    confidence=0.62,
                ),
                _decision_record(
                    symbol="BTC",
                    timestamp="2026-06-07T12:01:00Z",
                    price=102.0,
                    action="hold",
                    decision_id="btc_hold",
                    confidence=0.60,
                    notional=0.0,
                    leverage=0.0,
                    invalidation=0.0,
                    stop_bps=0.0,
                    take_profit_bps=0.0,
                    time_stop_minutes=0,
                ),
            ]
            _write_jsonl(input_path, records)

            result = run_trident_ai_paper_replay(
                input_path,
                config=load_trident_ai_config("config/trident_ai.toml"),
                journal_path=journal_path,
                report_json_path=report_json_path,
                report_md_path=report_md_path,
            )

            self.assertEqual(result.decisions_seen, 2)
            self.assertEqual(result.proposals_accepted, 2)
            self.assertEqual(result.action_counts["open"], 1)
            self.assertEqual(result.action_counts["hold"], 1)
            self.assertEqual(result.positions_opened, 1)
            self.assertEqual(result.positions_closed, 1)
            self.assertEqual(result.close_reasons["take_profit_hit"], 1)
            self.assertGreater(result.realized_pnl_usd, 0.0)
            self.assertEqual(result.open_positions, 0)
            self.assertEqual(result.ai_cost_usd, 0.002)
            self.assertLess(result.net_after_ai_cost_usd, result.realized_pnl_usd)

            journal = _read_jsonl(journal_path)
            self.assertTrue(any(record["event_type"] == PAPER_REPLAY_FILL_EVENT for record in journal))
            self.assertTrue(
                any(record["event_type"] == PAPER_REPLAY_TRADE_CLOSED_EVENT for record in journal)
            )
            report = json.loads(report_json_path.read_text(encoding="utf-8"))
            self.assertEqual(report["kind"], "trident_ai_paper_replay")
            self.assertIn("TRIDENT-AI Paper Replay", report_md_path.read_text(encoding="utf-8"))

    def test_paper_replay_respects_max_open_positions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            input_path = directory / "llm_replay.jsonl"
            records = [
                _decision_record(
                    symbol="BTC",
                    timestamp="2026-06-07T12:00:00Z",
                    price=100.0,
                    action="open",
                    decision_id="btc_open",
                    confidence=0.62,
                ),
                _decision_record(
                    symbol="ETH",
                    timestamp="2026-06-07T12:00:00Z",
                    price=10.0,
                    action="open",
                    decision_id="eth_open",
                    confidence=0.62,
                ),
            ]
            _write_jsonl(input_path, records)

            result = run_trident_ai_paper_replay(
                input_path,
                config=load_trident_ai_config("config/trident_ai.toml"),
                journal_path=directory / "paper.jsonl",
                report_json_path=directory / "paper.json",
                report_md_path=directory / "paper.md",
            )

            self.assertEqual(result.proposals_accepted, 2)
            self.assertEqual(result.positions_opened, 1)
            self.assertEqual(result.skip_reasons["max_open_positions_reached"], 1)
            self.assertEqual(result.close_reasons["end_of_paper_replay"], 1)
            self.assertEqual(result.open_positions, 0)

    def test_paper_replay_can_filter_decisions_by_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            input_path = directory / "llm_replay.jsonl"
            records = [
                _decision_record(
                    symbol="BTC",
                    timestamp="2026-06-07T12:00:00Z",
                    price=100.0,
                    action="open",
                    decision_id="btc_open",
                    confidence=0.62,
                ),
                _decision_record(
                    symbol="ETH",
                    timestamp="2026-06-07T12:00:00Z",
                    price=10.0,
                    action="open",
                    decision_id="eth_open",
                    confidence=0.62,
                ),
            ]
            _write_jsonl(input_path, records)

            result = run_trident_ai_paper_replay(
                input_path,
                config=load_trident_ai_config("config/trident_ai.toml"),
                journal_path=directory / "paper.jsonl",
                report_json_path=directory / "paper.json",
                report_md_path=directory / "paper.md",
                symbols=("ETH",),
            )

            self.assertEqual(result.symbols_filter, ("ETH",))
            self.assertEqual(result.decisions_seen, 1)
            self.assertEqual(result.positions_opened, 1)
            self.assertEqual(result.skip_reasons, {})

    def test_paper_replay_follows_market_input_after_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            input_path = directory / "llm_replay.jsonl"
            market_input_path = directory / "market.jsonl"
            records = [
                _decision_record(
                    symbol="BTC",
                    timestamp="2026-06-07T12:00:00Z",
                    price=100.0,
                    action="open",
                    decision_id="btc_open",
                    confidence=0.62,
                ),
            ]
            _write_jsonl(input_path, records)
            _write_jsonl(
                market_input_path,
                [
                    _market_snapshot_record(
                        timestamp="2026-06-07T12:01:00Z",
                        symbol="BTC",
                        price=102.0,
                    ),
                ],
            )

            result = run_trident_ai_paper_replay(
                input_path,
                config=load_trident_ai_config("config/trident_ai.toml"),
                journal_path=directory / "paper.jsonl",
                report_json_path=directory / "paper.json",
                report_md_path=directory / "paper.md",
                market_input_path=market_input_path,
            )

            self.assertEqual(result.decisions_seen, 1)
            self.assertEqual(result.market_contexts_seen, 1)
            self.assertEqual(result.market_exit_checks, 1)
            self.assertEqual(result.positions_opened, 1)
            self.assertEqual(result.positions_closed, 1)
            self.assertEqual(result.close_reasons["take_profit_hit"], 1)
            self.assertGreater(result.realized_pnl_usd, 0.0)
            self.assertEqual(result.last_timestamp, "2026-06-07T12:01:00Z")


def _decision_record(
    *,
    symbol: str,
    timestamp: str,
    price: float,
    action: str,
    decision_id: str,
    confidence: float,
    notional: float = 25.0,
    leverage: float = 1.0,
    invalidation: float = 99.0,
    stop_bps: float = 100.0,
    take_profit_bps: float = 100.0,
    time_stop_minutes: int = 60,
) -> dict[str, object]:
    proposal = {
        "schema_version": "trident_ai_proposal_v1",
        "decision_id": decision_id,
        "as_of": timestamp,
        "valid_until": "2026-06-07T12:05:00Z",
        "action": action,
        "symbol": symbol,
        "side": "long",
        "confidence": confidence,
        "time_horizon_minutes": 60,
        "max_notional_usd": notional,
        "max_leverage": leverage,
        "entry_style": "ioc" if action == "open" else "none",
        "invalidation_price": invalidation,
        "stop_bps": stop_bps,
        "take_profit_bps": take_profit_bps,
        "time_stop_minutes": time_stop_minutes,
        "rationale_tags": ["fixture"],
        "evidence_ids": [f"market_{symbol}_{timestamp}"],
        "risk_notes": ["fixture"],
    }
    return {
        "event_type": LLM_REPLAY_DECISION_EVENT,
        "source": "trident_ai_llm_replay",
        "record_index": 0,
        "timestamp": timestamp,
        "symbol": symbol,
        "request": {"request_id": f"request_{decision_id}"},
        "context": {
            "schema_version": "trident_ai_market_context_v1",
            "context_id": f"market_{symbol}_{timestamp}",
            "as_of": timestamp,
            "symbol": symbol,
            "price": price,
            "regime": "fixture",
            "features": {"spread_bps": 2.0},
            "source": "fixture",
        },
        "llm_response": {
            "ok": True,
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 200,
                "total_tokens": 1200,
                "estimated_cost_usd": 0.001,
            },
        },
        "proposal": proposal,
        "validation": {"accepted": True, "reason": "accepted"},
    }


def _market_snapshot_record(
    *,
    timestamp: str,
    symbol: str,
    price: float,
) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "regime_snapshot": {
            "ready": True,
            "adx": 25.0,
            "atr_ratio": 1.0,
            "range_width_bps": 120.0,
            "structure_score": 0.5,
            "btc_impulse": True,
            "regime": "TrendExpansion",
        },
        "symbols": [
            {
                "symbol": symbol,
                "price": price,
                "ema_fast": price,
                "ema_slow": price - 1.0,
                "vwap_distance_bps": 10.0,
                "structure_score": 0.5,
                "funding_rate": 0.0,
                "spread_bps": 2.0,
                "btc_aligned": True,
            }
        ],
    }


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(record, sort_keys=True) for record in records) + "\n",
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


if __name__ == "__main__":
    unittest.main()
