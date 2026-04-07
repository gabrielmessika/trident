from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from app.backtest.snapshot_loader import SnapshotLoader
from app.settings import load_config
from app.trident.supervisor import TridentSupervisor
from app.trident.types import RegimeSnapshot, SymbolMarketSnapshot


@dataclass(slots=True)
class FundingDatasetRow:
    timestamp: str | None
    source_file: str
    symbol: str
    regime: str
    price: float
    funding_rate: float
    funding_rate_bps: float
    spread_bps: float
    bucket_notional_usd: float
    bucket_trade_count: int
    structure_score: float
    adx: float
    atr_ratio: float
    range_width_bps: float
    future_returns_bps: dict[int, float | None]

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["future_returns_bps"] = {
            str(key): value for key, value in self.future_returns_bps.items()
        }
        return payload


@dataclass(slots=True)
class FundingDatasetBuildResult:
    input_path: str
    row_count: int
    symbol_count: int
    horizons_bars: list[int]
    output_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class FundingDatasetBuilder:
    """Builds aligned funding research rows from TRIDENT snapshots."""

    def __init__(self) -> None:
        self.loader = SnapshotLoader()

    def build_rows(
        self,
        *,
        input_path: str | Path,
        config_path: str | Path = "config/trident.toml",
        symbols: list[str] | None = None,
        horizons_bars: list[int] | None = None,
    ) -> list[FundingDatasetRow]:
        horizons = sorted({max(int(item), 1) for item in (horizons_bars or [1, 8, 24])})
        records = list(self.loader.iter_jsonl(input_path))
        if not records:
            return []

        config = load_config(config_path)
        supervisor = TridentSupervisor(
            config=config,
            profile="funding-dataset",
            mode="observation",
        )
        requested = None if symbols is None else {str(symbol).upper() for symbol in symbols}

        snapshots_by_index: list[dict[str, SymbolMarketSnapshot]] = []
        regimes_by_index: list[str] = []
        for record in records:
            supervisor.apply_regime_snapshot(RegimeSnapshot(**record.regime_snapshot))
            regimes_by_index.append(supervisor.state.regime.value)
            snapshots_by_index.append(
                {
                    item["symbol"].upper(): SymbolMarketSnapshot(**item)
                    for item in record.symbols
                    if isinstance(item, dict)
                }
            )

        rows: list[FundingDatasetRow] = []
        for index, record in enumerate(records):
            for symbol, snapshot in snapshots_by_index[index].items():
                if requested is not None and symbol not in requested:
                    continue
                future_returns: dict[int, float | None] = {}
                for horizon in horizons:
                    if index + horizon >= len(snapshots_by_index):
                        future_returns[horizon] = None
                        continue
                    future_snapshot = snapshots_by_index[index + horizon].get(symbol)
                    if future_snapshot is None or snapshot.price <= 0:
                        future_returns[horizon] = None
                        continue
                    future_returns[horizon] = round(
                        (future_snapshot.price - snapshot.price) / snapshot.price * 10_000.0,
                        4,
                    )
                rows.append(
                    FundingDatasetRow(
                        timestamp=record.timestamp,
                        source_file=record.source_file,
                        symbol=symbol,
                        regime=regimes_by_index[index],
                        price=round(snapshot.price, 8),
                        funding_rate=round(snapshot.funding_rate, 10),
                        funding_rate_bps=round(snapshot.funding_rate * 10_000.0, 4),
                        spread_bps=round(snapshot.spread_bps, 4),
                        bucket_notional_usd=round(snapshot.bucket_volume * snapshot.price, 4),
                        bucket_trade_count=int(snapshot.bucket_trade_count),
                        structure_score=round(snapshot.structure_score, 4),
                        adx=float(record.regime_snapshot.get("adx", 0.0)),
                        atr_ratio=float(record.regime_snapshot.get("atr_ratio", 0.0)),
                        range_width_bps=float(record.regime_snapshot.get("range_width_bps", 0.0)),
                        future_returns_bps=future_returns,
                    )
                )
        return rows

    def build(
        self,
        *,
        input_path: str | Path,
        config_path: str | Path = "config/trident.toml",
        symbols: list[str] | None = None,
        horizons_bars: list[int] | None = None,
        output_path: str | Path | None = None,
    ) -> FundingDatasetBuildResult:
        rows = self.build_rows(
            input_path=input_path,
            config_path=config_path,
            symbols=symbols,
            horizons_bars=horizons_bars,
        )
        if output_path is not None:
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row.to_dict()) + "\n")
        symbols_in_rows = {row.symbol for row in rows}
        return FundingDatasetBuildResult(
            input_path=str(input_path),
            row_count=len(rows),
            symbol_count=len(symbols_in_rows),
            horizons_bars=sorted({int(item) for item in (horizons_bars or [1, 8, 24])}),
            output_path=str(output_path) if output_path is not None else None,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a funding research dataset from TRIDENT snapshots")
    parser.add_argument("--input", required=True)
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--symbols", help="Optional comma-separated list")
    parser.add_argument("--horizons-bars", default="1,8,24")
    parser.add_argument("--output")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    horizons = [int(item.strip()) for item in args.horizons_bars.split(",") if item.strip()]
    symbols = [item.strip().upper() for item in (args.symbols or "").split(",") if item.strip()]
    result = FundingDatasetBuilder().build(
        input_path=args.input,
        config_path=args.config,
        symbols=symbols or None,
        horizons_bars=horizons,
        output_path=args.output,
    )
    print(f"row_count={result.row_count}")
    print(f"symbol_count={result.symbol_count}")
    if result.output_path:
        print(f"output_path={result.output_path}")


if __name__ == "__main__":
    main()
