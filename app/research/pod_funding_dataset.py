from __future__ import annotations

import argparse
from bisect import bisect_right
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.backtest.snapshot_loader import SnapshotLoader
from app.settings import load_config
from app.trident.supervisor import TridentSupervisor
from app.trident.types import RegimeSnapshot, SymbolMarketSnapshot, symbol_market_snapshot_from_mapping


@dataclass(slots=True)
class FundingDatasetRow:
    timestamp: str | None
    source_file: str
    symbol: str
    regime: str
    price: float
    funding_rate: float
    funding_rate_bps: float
    open_interest: float | None
    funding_observation_age_seconds: float | None
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


@dataclass(slots=True)
class FundingHistoryPoint:
    timestamp: datetime
    funding_rate: float
    open_interest: float | None
    mark_px: float | None = None
    oracle_px: float | None = None
    premium: float | None = None
    day_ntl_vlm: float | None = None
    day_base_vlm: float | None = None


@dataclass(slots=True)
class FundingHistorySeries:
    timestamps: list[datetime]
    points: list[FundingHistoryPoint]

    def latest_at(
        self,
        target: datetime,
        *,
        max_age_seconds: float,
    ) -> tuple[FundingHistoryPoint, float] | None:
        index = bisect_right(self.timestamps, target) - 1
        if index < 0:
            return None
        point = self.points[index]
        age_seconds = max((target - point.timestamp).total_seconds(), 0.0)
        if age_seconds > max(max_age_seconds, 0.0):
            return None
        return point, age_seconds


def parse_utc_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_utc_timestamp(value: object) -> datetime | None:
    return parse_utc_timestamp(value)


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
        funding_history_path: str | Path | None = None,
        funding_max_age_seconds: float = 900.0,
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
        funding_history = self._load_funding_history(
            funding_history_path,
            symbols=requested,
        )

        snapshots_by_index: list[dict[str, SymbolMarketSnapshot]] = []
        regimes_by_index: list[str] = []
        record_timestamps: list[datetime | None] = []
        for record in records:
            supervisor.apply_regime_snapshot(
                RegimeSnapshot(**record.regime_snapshot),
                cluster_regime_snapshots={
                    cluster: RegimeSnapshot(**snap)
                    for cluster, snap in (record.cluster_regime_snapshots or {}).items()
                    if isinstance(snap, dict)
                },
            )
            regimes_by_index.append(supervisor.state.regime.value)
            record_timestamps.append(parse_utc_timestamp(record.timestamp))
            snapshots_by_index.append(
                {
                    item["symbol"].upper(): symbol_market_snapshot_from_mapping(item)
                    for item in record.symbols
                    if isinstance(item, dict)
                }
            )

        rows: list[FundingDatasetRow] = []
        for index, record in enumerate(records):
            for symbol, snapshot in snapshots_by_index[index].items():
                if requested is not None and symbol not in requested:
                    continue
                funding_rate = round(snapshot.funding_rate, 10)
                open_interest = None
                funding_age_seconds = None
                record_timestamp = record_timestamps[index]
                series = funding_history.get(symbol)
                if record_timestamp is not None and series is not None:
                    aligned = series.latest_at(
                        record_timestamp,
                        max_age_seconds=funding_max_age_seconds,
                    )
                    if aligned is not None:
                        point, funding_age_seconds = aligned
                        funding_rate = round(point.funding_rate, 10)
                        open_interest = point.open_interest
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
                        funding_rate=funding_rate,
                        funding_rate_bps=round(funding_rate * 10_000.0, 4),
                        open_interest=(
                            round(open_interest, 6)
                            if isinstance(open_interest, (int, float))
                            else None
                        ),
                        funding_observation_age_seconds=(
                            round(funding_age_seconds, 4)
                            if isinstance(funding_age_seconds, (int, float))
                            else None
                        ),
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
        funding_history_path: str | Path | None = None,
        funding_max_age_seconds: float = 900.0,
        output_path: str | Path | None = None,
    ) -> FundingDatasetBuildResult:
        rows = self.build_rows(
            input_path=input_path,
            config_path=config_path,
            symbols=symbols,
            horizons_bars=horizons_bars,
            funding_history_path=funding_history_path,
            funding_max_age_seconds=funding_max_age_seconds,
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

    def _load_funding_history(
        self,
        funding_history_path: str | Path | None,
        *,
        symbols: set[str] | None,
    ) -> dict[str, FundingHistorySeries]:
        if funding_history_path is None:
            return {}
        path = Path(funding_history_path)
        if not path.exists():
            return {}

        rows_by_symbol: dict[str, list[FundingHistoryPoint]] = {}
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                symbol = str(payload.get("symbol", "")).upper()
                if not symbol:
                    continue
                if symbols is not None and symbol not in symbols:
                    continue
                timestamp = parse_utc_timestamp(payload.get("timestamp") or payload.get("captured_at"))
                if timestamp is None:
                    continue
                open_interest = self._float_or_none(payload.get("open_interest"))
                rows_by_symbol.setdefault(symbol, []).append(
                    FundingHistoryPoint(
                        timestamp=timestamp,
                        funding_rate=float(payload.get("funding_rate", 0.0)),
                        open_interest=open_interest,
                        mark_px=self._float_or_none(payload.get("mark_px")),
                        oracle_px=self._float_or_none(payload.get("oracle_px")),
                        premium=self._float_or_none(payload.get("premium")),
                        day_ntl_vlm=self._float_or_none(payload.get("day_ntl_vlm")),
                        day_base_vlm=self._float_or_none(payload.get("day_base_vlm")),
                    )
                )

        series_by_symbol: dict[str, FundingHistorySeries] = {}
        for symbol, points in rows_by_symbol.items():
            ordered = sorted(points, key=lambda item: item.timestamp)
            series_by_symbol[symbol] = FundingHistorySeries(
                timestamps=[item.timestamp for item in ordered],
                points=ordered,
            )
        return series_by_symbol

    def load_funding_history(
        self,
        funding_history_path: str | Path | None,
        *,
        symbols: set[str] | None = None,
    ) -> dict[str, FundingHistorySeries]:
        return self._load_funding_history(
            funding_history_path,
            symbols=symbols,
        )

    @staticmethod
    def _float_or_none(value: object) -> float | None:
        if value in (None, ""):
            return None
        return float(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a funding research dataset from TRIDENT snapshots")
    parser.add_argument("--input", required=True)
    parser.add_argument("--config", default="config/trident.toml")
    parser.add_argument("--symbols", help="Optional comma-separated list")
    parser.add_argument("--horizons-bars", default="1,8,24")
    parser.add_argument("--funding-history")
    parser.add_argument("--funding-max-age-seconds", type=float, default=900.0)
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
        funding_history_path=args.funding_history,
        funding_max_age_seconds=args.funding_max_age_seconds,
        output_path=args.output,
    )
    print(f"row_count={result.row_count}")
    print(f"symbol_count={result.symbol_count}")
    if result.output_path:
        print(f"output_path={result.output_path}")


if __name__ == "__main__":
    main()
