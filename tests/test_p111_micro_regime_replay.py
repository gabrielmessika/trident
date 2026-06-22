from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from scripts.run_p111_micro_regime_replay import (
    load_hip4_trades,
    run_p111_micro_regime_replay,
)


class P111MicroRegimeReplayTests(unittest.TestCase):
    def test_micro_regime_replay_joins_entry_snapshots_and_builds_counterfactual(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            snapshots = root / "snapshots"
            snapshots.mkdir()
            replay_json = root / "full_bot.json"
            output_dir = root / "out"

            _write_snapshot(
                snapshots / "2026-06-01.jsonl",
                timestamp="2026-06-01T12:00:00Z",
                symbols=[
                    _symbol_snapshot(
                        "HYPE",
                        price=58.0,
                        bucket_range_bps=58.0,
                        realized_vol_short_bps=24.0,
                        volume_ratio=2.5,
                        vwap_distance_bps=20.0,
                        microprice_dislocation_bps=-0.25,
                    ),
                    _symbol_snapshot(
                        "BTC",
                        price=63000.0,
                        bucket_range_bps=42.0,
                        realized_vol_short_bps=18.0,
                        volume_ratio=1.5,
                        vwap_distance_bps=5.0,
                        microprice_dislocation_bps=0.25,
                    ),
                ],
            )
            replay_json.write_text(
                json.dumps(
                    {
                        "pod_a": {
                            "closed_trade_log": [
                                _closed_trade(
                                    "HYPE",
                                    pnl=-1.25,
                                    opened_at="2026-06-01T12:00:30Z",
                                    close_reason="stop_hit",
                                ),
                                _closed_trade(
                                    "BTC",
                                    pnl=0.75,
                                    opened_at="2026-06-01T12:00:45Z",
                                    close_reason="trailing_stop",
                                ),
                            ]
                        },
                        "pod_c": {"closed_trade_log": []},
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            result = run_p111_micro_regime_replay(
                snapshot_dir=snapshots,
                output_dir=output_dir,
                full_bot_json_sources=[f"fixture={replay_json}"],
                include_live_logs=False,
                include_hip4=False,
                min_trades=1,
                quiet=True,
            )

            payload = result.payload
            self.assertEqual(payload["summary"]["enriched_trades"], 2)
            hype = next(
                row
                for row in payload["enriched_trade_sample"]
                if row["symbol"] == "HYPE"
            )
            self.assertEqual(hype["range_vol_regime"], "range_mid|vol_high")
            self.assertEqual(hype["microprice_bucket"], "micro_adverse")
            loss = next(
                row
                for row in payload["bucket_rows"]
                if row["scope"] == "ac"
                and row["family"] == "range_vol_regime"
                and row["bucket"] == "range_mid|vol_high"
            )
            self.assertEqual(loss["classification"], "symbol_specific_loss_regime")
            veto = next(
                row
                for row in payload["counterfactual_rows"]
                if row["profile"] == "veto_range_mid_vol_high"
            )
            self.assertGreater(veto["delta_vs_baseline_usd"], 0.0)
            self.assertTrue(result.report_md_path.exists())
            self.assertTrue(result.enriched_trades_csv_path.exists())

    def test_load_hip4_trades_maps_buy_no_to_underlying_short(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write_csv(
                root / "trades.csv",
                [
                    {
                        "ts": "2026-06-02T10:00:00Z",
                        "market_id": "HYPE_GT_50_20260603_0600",
                        "outcome": "1",
                        "underlying": "HYPE",
                        "edge_type": "MODEL",
                        "side": "BUY_NO",
                        "coin": "#1",
                        "price": "0.40",
                        "size_usdc": "12",
                        "token_qty": "30",
                        "status": "paper_filled",
                        "oid": "",
                        "cloid": "",
                    }
                ],
            )
            _write_csv(
                root / "settlements.csv",
                [
                    {
                        "ts": "2026-06-03T06:05:00Z",
                        "market_id": "HYPE_GT_50_20260603_0600",
                        "outcome": "1",
                        "underlying": "HYPE",
                        "side": "BUY_NO",
                        "result": "NO",
                        "payout_usdc": "20",
                        "fee_usdc": "0.04",
                        "gross_pnl_usdc": "8",
                        "net_pnl_usdc": "7.96",
                        "pnl_usdc": "7.96",
                        "is_win": "true",
                        "notes": "estimated",
                    }
                ],
            )

            trades = load_hip4_trades(root, fold="fixture_hip4")

            self.assertEqual(len(trades), 1)
            self.assertEqual(trades[0].scope, "hip4")
            self.assertEqual(trades[0].symbol, "HYPE")
            self.assertEqual(trades[0].side, "short")
            self.assertAlmostEqual(trades[0].pnl_usd, 7.96)


def _closed_trade(
    symbol: str,
    *,
    pnl: float,
    opened_at: str,
    close_reason: str,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "side": "long",
        "setup": "trend_pullback_long",
        "opened_at": opened_at,
        "closed_at": "2026-06-01T13:00:00Z",
        "close_reason": close_reason,
        "pnl_usd": pnl,
        "target_notional_usd": 100.0,
        "fees_usd": 0.1,
        "setup_details": {"market_cluster": "crypto"},
    }


def _symbol_snapshot(
    symbol: str,
    *,
    price: float,
    bucket_range_bps: float,
    realized_vol_short_bps: float,
    volume_ratio: float,
    vwap_distance_bps: float,
    microprice_dislocation_bps: float,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "price": price,
        "ema_fast": price,
        "ema_slow": price,
        "vwap_distance_bps": vwap_distance_bps,
        "structure_score": 0.4,
        "funding_rate": 0.0,
        "spread_bps": 1.0,
        "btc_aligned": True,
        "market_cluster": "crypto",
        "bucket_range_bps": bucket_range_bps,
        "realized_vol_short_bps": realized_vol_short_bps,
        "volume_ratio": volume_ratio,
        "microprice_dislocation_bps": microprice_dislocation_bps,
        "trade_flow_bias": 0.1,
        "book_imbalance": 0.1,
    }


def _write_snapshot(path: Path, *, timestamp: str, symbols: list[dict[str, object]]) -> None:
    payload = {
        "timestamp": timestamp,
        "regime_snapshot": {
            "ready": True,
            "adx": 25.0,
            "atr_ratio": 1.0,
            "range_width_bps": 50.0,
            "structure_score": 0.4,
            "btc_impulse": False,
        },
        "symbols": symbols,
    }
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    unittest.main()
