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
class PodLiqFeatureRow:
    timestamp: str | None
    source_file: str
    symbol: str
    regime: str
    price: float
    spread_bps: float
    book_imbalance: float
    trade_flow_bias: float
    bucket_range_bps: float
    bucket_trade_count: int
    bucket_notional_usd: float
    structure_score: float
    delta_spread_bps: float
    delta_book_imbalance: float
    delta_trade_flow_bias: float
    volume_ratio: float
    trade_count_ratio: float
    direction: str
    event_score: float
    future_return_bps: float | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class PodLiqFeatureBuilder:
    """Builds observables-first event features from TRIDENT snapshots."""

    def __init__(self) -> None:
        self.loader = SnapshotLoader()

    def build_rows(
        self,
        *,
        input_path: str | Path,
        config_path: str | Path = "config/trident.toml",
        symbols: list[str] | None = None,
        horizon_bars: int = 1,
    ) -> list[PodLiqFeatureRow]:
        records = list(self.loader.iter_jsonl(input_path))
        if not records:
            return []
        config = load_config(config_path)
        supervisor = TridentSupervisor(
            config=config,
            profile="pod-liq-features",
            mode="observation",
        )
        requested = None if symbols is None else {str(symbol).upper() for symbol in symbols}
        snapshot_matrix: list[dict[str, SymbolMarketSnapshot]] = []
        regimes_by_index: list[str] = []
        for record in records:
            supervisor.apply_regime_snapshot(RegimeSnapshot(**record.regime_snapshot))
            regimes_by_index.append(supervisor.state.regime.value)
            snapshot_matrix.append(
                {
                    item["symbol"].upper(): SymbolMarketSnapshot(**item)
                    for item in record.symbols
                    if isinstance(item, dict)
                }
            )

        rows: list[PodLiqFeatureRow] = []
        for index in range(1, len(records)):
            previous_snapshots = snapshot_matrix[index - 1]
            current_snapshots = snapshot_matrix[index]
            for symbol, current in current_snapshots.items():
                if requested is not None and symbol not in requested:
                    continue
                previous = previous_snapshots.get(symbol)
                if previous is None:
                    continue
                future_return = None
                if index + horizon_bars < len(snapshot_matrix):
                    future = snapshot_matrix[index + horizon_bars].get(symbol)
                    if future is not None and current.price > 0:
                        future_return = round(
                            (future.price - current.price) / current.price * 10_000.0,
                            4,
                        )
                delta_spread = round(current.spread_bps - previous.spread_bps, 4)
                delta_book = round(current.book_imbalance - previous.book_imbalance, 4)
                delta_flow = round(current.trade_flow_bias - previous.trade_flow_bias, 4)
                volume_ratio = round(
                    current.bucket_volume / previous.bucket_volume,
                    4,
                ) if previous.bucket_volume > 0 else (2.0 if current.bucket_volume > 0 else 1.0)
                trade_count_ratio = round(
                    current.bucket_trade_count / previous.bucket_trade_count,
                    4,
                ) if previous.bucket_trade_count > 0 else (2.0 if current.bucket_trade_count > 0 else 1.0)
                signed_intensity = (
                    current.trade_flow_bias * 0.4
                    + current.book_imbalance * 0.2
                    + delta_flow * 0.25
                    + delta_book * 0.15
                )
                direction = "long" if signed_intensity >= 0 else "short"
                event_score = min(
                    1.0,
                    abs(delta_flow) * 0.35
                    + abs(delta_book) * 0.20
                    + max(delta_spread, 0.0) / 10.0 * 0.15
                    + min(volume_ratio / 3.0, 1.0) * 0.15
                    + min(trade_count_ratio / 3.0, 1.0) * 0.15,
                )
                rows.append(
                    PodLiqFeatureRow(
                        timestamp=records[index].timestamp,
                        source_file=records[index].source_file,
                        symbol=symbol,
                        regime=regimes_by_index[index],
                        price=round(current.price, 8),
                        spread_bps=round(current.spread_bps, 4),
                        book_imbalance=round(current.book_imbalance, 4),
                        trade_flow_bias=round(current.trade_flow_bias, 4),
                        bucket_range_bps=round(current.bucket_range_bps, 4),
                        bucket_trade_count=int(current.bucket_trade_count),
                        bucket_notional_usd=round(current.bucket_volume * current.price, 4),
                        structure_score=round(current.structure_score, 4),
                        delta_spread_bps=delta_spread,
                        delta_book_imbalance=delta_book,
                        delta_trade_flow_bias=delta_flow,
                        volume_ratio=volume_ratio,
                        trade_count_ratio=trade_count_ratio,
                        direction=direction,
                        event_score=round(event_score, 4),
                        future_return_bps=future_return,
                    )
                )
        return rows

    def build(
        self,
        *,
        input_path: str | Path,
        config_path: str | Path = "config/trident.toml",
        symbols: list[str] | None = None,
        horizon_bars: int = 1,
        output_path: str | Path | None = None,
    ) -> dict[str, object]:
        rows = self.build_rows(
            input_path=input_path,
            config_path=config_path,
            symbols=symbols,
            horizon_bars=horizon_bars,
        )
        if output_path is not None:
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row.to_dict()) + "\n")
        return {
            "input_path": str(input_path),
            "row_count": len(rows),
            "output_path": str(output_path) if output_path is not None else None,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build observables-first liq/OI features from snapshots")
    parser.add_argument("--input", required=True)
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--symbols", help="Optional comma-separated list")
    parser.add_argument("--horizon-bars", type=int, default=1)
    parser.add_argument("--output")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    symbols = [item.strip().upper() for item in (args.symbols or "").split(",") if item.strip()]
    result = PodLiqFeatureBuilder().build(
        input_path=args.input,
        config_path=args.config,
        symbols=symbols or None,
        horizon_bars=args.horizon_bars,
        output_path=args.output,
    )
    print(f"row_count={result['row_count']}")
    if result["output_path"] is not None:
        print(f"output_path={result['output_path']}")


if __name__ == "__main__":
    main()
