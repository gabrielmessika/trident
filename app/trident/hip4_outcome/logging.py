from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


class OutcomeEventLogger:
    def __init__(self, logs_dir: str | Path) -> None:
        self.logs_dir = Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.opportunities_path = self.logs_dir / "opportunities.csv"
        self.decisions_path = self.logs_dir / "decisions.jsonl"
        self.trades_path = self.logs_dir / "trades.csv"
        self.settlements_path = self.logs_dir / "settlements.csv"
        self.latency_path = self.logs_dir / "latency_stats.csv"
        self.edge_decay_path = self.logs_dir / "edge_decay.csv"
        self.short_expiry_features_path = self.logs_dir / "short_expiry_features.csv"
        self.reconciliation_path = self.logs_dir / "reconciliation.jsonl"
        self.daily_summary_path = self.logs_dir / "daily_summary.csv"

    def log_opportunity(self, row: dict[str, Any]) -> None:
        self._append_csv(
            self.opportunities_path,
            [
                "ts",
                "market_id",
                "outcome",
                "underlying",
                "edge_type",
                "side",
                "gross_edge",
                "net_edge",
                "confidence",
                "requested_size_usdc",
                "yes_ask",
                "no_ask",
                "ref_price",
                "strike",
                "time_to_expiry",
                "reason",
            ],
            row,
        )

    def log_decision(self, payload: dict[str, Any]) -> None:
        self._append_jsonl(self.decisions_path, payload)

    def log_trade(self, row: dict[str, Any]) -> None:
        self._append_csv(
            self.trades_path,
            [
                "ts",
                "market_id",
                "outcome",
                "underlying",
                "edge_type",
                "side",
                "coin",
                "price",
                "size_usdc",
                "token_qty",
                "status",
                "oid",
                "cloid",
            ],
            row,
        )

    def log_settlement(self, row: dict[str, Any]) -> None:
        self._append_csv(
            self.settlements_path,
            [
                "ts",
                "market_id",
                "outcome",
                "underlying",
                "side",
                "result",
                "payout_usdc",
                "pnl_usdc",
                "notes",
            ],
            row,
        )

    def log_latency(self, row: dict[str, Any]) -> None:
        self._append_csv(
            self.latency_path,
            [
                "ts",
                "loop_count",
                "mode",
                "markets_seen",
                "markets_supported",
                "opportunities",
                "executed",
                "total_ms",
                "fetch_mids_ms",
                "discover_markets_ms",
                "reference_prices_ms",
                "books_ms",
                "edge_detection_ms",
                "execution_ms",
                "settlement_ms",
                "reconciliation_ms",
                "status_ms",
                "error",
                "short_features_ms",
            ],
            row,
        )

    def log_edge_decay(self, row: dict[str, Any]) -> None:
        self._append_csv(
            self.edge_decay_path,
            [
                "ts",
                "market_id",
                "underlying",
                "edge_type",
                "side",
                "first_seen_at",
                "first_net_edge",
                "current_net_edge",
                "delta_net_edge",
                "elapsed_seconds",
                "ref_price",
                "yes_ask",
                "no_ask",
                "source_count",
            ],
            row,
        )

    def log_short_expiry_features(self, row: dict[str, Any]) -> None:
        self._append_csv(
            self.short_expiry_features_path,
            [
                "ts",
                "market_id",
                "outcome",
                "underlying",
                "period",
                "seconds_left",
                "reference_price",
                "strike",
                "distance_bps",
                "history_span_seconds",
                "sample_count",
                "momentum_bps_30s",
                "momentum_bps_60s",
                "momentum_bps_180s",
                "velocity_bps_per_minute",
                "realized_vol_bps_60s",
                "book_probability_yes",
                "book_imbalance_yes",
                "model_probability_yes",
                "short_probability_yes",
                "yes_bid",
                "yes_ask",
                "no_bid",
                "no_ask",
                "best_side",
                "best_gross_edge",
                "best_net_edge",
                "confidence",
                "reason",
            ],
            row,
        )

    def log_reconciliation(self, payload: dict[str, Any]) -> None:
        self._append_jsonl(self.reconciliation_path, payload)

    def write_daily_summary(self, rows: list[dict[str, Any]]) -> None:
        self._write_csv(
            self.daily_summary_path,
            [
                "date",
                "mode",
                "underlying",
                "positions",
                "open_positions",
                "settled_positions",
                "cost_usdc",
                "estimated_payout_usdc",
                "estimated_pnl_usdc",
                "avg_net_edge",
                "avg_confidence",
            ],
            rows,
        )

    def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")

    def _append_csv(self, path: Path, fieldnames: list[str], row: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        exists = path.exists()
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            if not exists:
                writer.writeheader()
            writer.writerow(row)

    def _write_csv(self, path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
