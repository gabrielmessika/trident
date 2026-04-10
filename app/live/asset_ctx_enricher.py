from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.research.pod_funding_dataset import FundingDatasetBuilder, parse_utc_timestamp


class SnapshotAssetCtxEnricher:
    """Enriches TRIDENT snapshot JSONL files with aligned Hyperliquid assetCtx history."""

    def __init__(self) -> None:
        self._dataset_builder = FundingDatasetBuilder()

    def enrich(
        self,
        *,
        input_path: str | Path,
        funding_history_path: str | Path,
        output_path: str | Path,
        symbols: list[str] | None = None,
        funding_max_age_seconds: float = 900.0,
    ) -> dict[str, object]:
        requested = None if symbols is None else {str(symbol).upper() for symbol in symbols}
        rows = self._dataset_builder.load_funding_history(
            funding_history_path,
            symbols=requested,
        )

        input_file = Path(input_path)
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        records_processed = 0
        symbols_enriched = 0
        with input_file.open("r", encoding="utf-8") as src, output_file.open(
            "w",
            encoding="utf-8",
        ) as dst:
            for raw_line in src:
                line = raw_line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                timestamp = payload.get("timestamp")
                enriched = self._enrich_record(
                    payload=payload,
                    timestamp=timestamp,
                    funding_history=rows,
                    requested=requested,
                    funding_max_age_seconds=funding_max_age_seconds,
                )
                records_processed += 1
                symbols_enriched += enriched
                dst.write(json.dumps(payload) + "\n")
        return {
            "input_path": str(input_path),
            "output_path": str(output_path),
            "records_processed": records_processed,
            "symbols_enriched": symbols_enriched,
        }

    def _enrich_record(
        self,
        *,
        payload: dict[str, object],
        timestamp: object,
        funding_history: dict[str, object],
        requested: set[str] | None,
        funding_max_age_seconds: float,
    ) -> int:
        record_timestamp = parse_utc_timestamp(timestamp)
        if record_timestamp is None:
            return 0
        symbols = payload.get("symbols")
        if not isinstance(symbols, list):
            return 0

        enriched = 0
        for symbol_payload in symbols:
            if not isinstance(symbol_payload, dict):
                continue
            symbol = str(symbol_payload.get("symbol", "")).upper()
            if not symbol:
                continue
            if requested is not None and symbol not in requested:
                continue
            series = funding_history.get(symbol)
            if series is None:
                continue
            aligned = series.latest_at(
                record_timestamp,
                max_age_seconds=funding_max_age_seconds,
            )
            if aligned is None:
                continue
            point, age_seconds = aligned
            symbol_payload["funding_rate"] = round(point.funding_rate, 10)
            symbol_payload["open_interest"] = (
                round(point.open_interest, 6)
                if isinstance(point.open_interest, (int, float))
                else None
            )
            symbol_payload["mark_px"] = point.mark_px
            symbol_payload["oracle_px"] = point.oracle_px
            symbol_payload["premium"] = point.premium
            symbol_payload["day_ntl_vlm"] = point.day_ntl_vlm
            symbol_payload["day_base_vlm"] = point.day_base_vlm
            symbol_payload["asset_ctx_observation_age_seconds"] = round(age_seconds, 4)
            enriched += 1
        return enriched


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Enrich TRIDENT snapshots with aligned HL assetCtx history")
    parser.add_argument("--input", required=True)
    parser.add_argument("--funding-history", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--symbols", help="Optional comma-separated symbol list")
    parser.add_argument("--funding-max-age-seconds", type=float, default=900.0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    symbols = [item.strip().upper() for item in (args.symbols or "").split(",") if item.strip()]
    result = SnapshotAssetCtxEnricher().enrich(
        input_path=args.input,
        funding_history_path=args.funding_history,
        output_path=args.output,
        symbols=symbols or None,
        funding_max_age_seconds=args.funding_max_age_seconds,
    )
    print(f"records_processed={result['records_processed']}")
    print(f"symbols_enriched={result['symbols_enriched']}")
    print(f"output_path={result['output_path']}")


if __name__ == "__main__":
    main()
