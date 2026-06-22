from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.trident_ai import load_trident_ai_config, run_trident_ai_market_regime_audit
from app.trident_ai.candidate_scan import CANDIDATE_HINT_FIELD


class TridentAIMarketRegimeAuditTests(unittest.TestCase):
    def test_market_regime_audit_buckets_closed_trades_by_symbol_and_regime(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            decision_journal = directory / "decisions.jsonl"
            paper_journal = directory / "paper.jsonl"
            gate_sweep_report = directory / "gate_sweep.json"
            report_json = directory / "regime_audit.json"
            report_md = directory / "regime_audit.md"

            _write_jsonl(
                decision_journal,
                [
                    _decision(
                        decision_id="hype_loss",
                        symbol="HYPE",
                        bucket_range_bps=82.0,
                        realized_vol_short_bps=24.0,
                        volume_ratio=12.0,
                        vwap_distance_bps=22.0,
                    ),
                    _decision(
                        decision_id="btc_win",
                        symbol="BTC",
                        bucket_range_bps=58.0,
                        realized_vol_short_bps=18.0,
                        volume_ratio=4.0,
                        vwap_distance_bps=6.0,
                    ),
                ],
            )
            _write_jsonl(
                paper_journal,
                [
                    _closed_trade("hype_loss", "HYPE", -0.35, "stop_hit"),
                    _closed_trade("btc_win", "BTC", 0.22, "take_profit_hit"),
                ],
            )
            gate_sweep_report.write_text(
                json.dumps(
                    {
                        "result": {
                            "profile_rows": [
                                {
                                    "profile_id": "fixture_profile",
                                    "folds": [
                                        {
                                            "fold_label": "fixture_fold",
                                            "decision_journal_path": str(decision_journal),
                                            "paper_journal_path": str(paper_journal),
                                        }
                                    ],
                                }
                            ]
                        }
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            result = run_trident_ai_market_regime_audit(
                gate_sweep_report_path=gate_sweep_report,
                profile_id="fixture_profile",
                config=load_trident_ai_config("config/trident_ai.toml"),
                report_json_path=report_json,
                report_md_path=report_md,
                min_trades=1,
            )

            self.assertEqual(result.summary["trades"], 2)
            self.assertEqual(result.summary["stop_hits"], 1)
            hype_symbol = next(row for row in result.bucket_rows if row["family"] == "symbol" and row["bucket"] == "HYPE")
            self.assertEqual(hype_symbol["classification"], "symbol_specific_loss_regime")
            self.assertLess(hype_symbol["pnl_usd"], 0.0)
            support = next(
                row
                for row in result.bucket_rows
                if row["family"] == "range_vol_regime"
                and row["bucket"] == "range_mid|vol_controlled"
            )
            self.assertEqual(support["classification"], "symbol_specific_support_regime")
            self.assertTrue(report_json.exists())
            self.assertIn("TRIDENT-AI Market Regime Audit", report_md.read_text(encoding="utf-8"))


def _decision(
    *,
    decision_id: str,
    symbol: str,
    bucket_range_bps: float,
    realized_vol_short_bps: float,
    volume_ratio: float,
    vwap_distance_bps: float,
) -> dict[str, object]:
    return {
        "timestamp": "2026-06-07T12:00:00Z",
        "symbol": symbol,
        "proposal": {
            "decision_id": decision_id,
            "action": "open",
            "symbol": symbol,
            "side": "long",
        },
        "context": {
            "regime": "unknown",
            "features": {
                "bucket_range_bps": bucket_range_bps,
                "realized_vol_short_bps": realized_vol_short_bps,
                "volume_ratio": volume_ratio,
                "vwap_distance_bps": vwap_distance_bps,
                "microprice_dislocation_bps": 0.1,
            },
            CANDIDATE_HINT_FIELD: {
                "edge_to_cost_ratio": 4.5,
                "estimated_net_edge_bps": 30.0,
                "pattern_quality_score": 0.85,
            },
        },
    }


def _closed_trade(
    decision_id: str,
    symbol: str,
    pnl_usd: float,
    close_reason: str,
) -> dict[str, object]:
    return {
        "event_type": "trident_ai_paper_replay_trade_closed",
        "trade": {
            "decision_id": decision_id,
            "symbol": symbol,
            "side": "long",
            "opened_at": "2026-06-07T12:00:00Z",
            "closed_at": "2026-06-07T13:00:00Z",
            "notional_usd": 25.0,
            "pnl_usd": pnl_usd,
            "gross_pnl_usd": pnl_usd,
            "fees_usd": 0.0,
            "close_reason": close_reason,
        },
    }


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
