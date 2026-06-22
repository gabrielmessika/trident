#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.trident_ai.market_regime import build_market_micro_regime


DEFAULT_SNAPSHOT_DIR = ROOT / "server-data" / "live_snapshots"
DEFAULT_OUTPUT_PARENT = ROOT / "server-data" / "replay_reports"
DEFAULT_BASELINE_FULL_BOT_JSON = (
    ROOT / "server-data" / "replay_reports" / "official_baseline_current_cli_20260513.json"
)
DEFAULT_RECENT_FULL_BOT_JSON = (
    ROOT
    / "server-data"
    / "replay_reports"
    / "p101_recent_full_bot_livecap_20260612T170415Z"
    / "full_bot_replay_current_config.json"
)
DEFAULT_POD_A_LOG = ROOT / "server-data" / "logs" / "pod_a_live.jsonl"
DEFAULT_POD_C_LOG = ROOT / "server-data" / "logs" / "pod_c_live.jsonl"
DEFAULT_HIP4_DIR = ROOT / "server-data" / "hip4" / "logs" / "hip4_outcome_mainnet_paper"


@dataclass(slots=True)
class SnapshotFeature:
    timestamp: str
    ts: int
    symbol: str
    market_cluster: str
    base_regime: str
    price: float
    bucket_range_bps: float
    realized_vol_short_bps: float
    volume_ratio: float
    vwap_distance_bps: float
    microprice_dislocation_bps: float
    spread_bps: float = 0.0
    trade_flow_bias: float = 0.0
    book_imbalance: float = 0.0
    source_file: str = ""


@dataclass(slots=True)
class TradeRow:
    scope: str
    fold: str
    source: str
    pod: str
    symbol: str
    side: str
    setup: str
    opened_at: str
    closed_at: str
    close_reason: str
    pnl_usd: float
    notional_usd: float
    fees_usd: float = 0.0
    market_cluster: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class P111Result:
    output_dir: Path
    report_json_path: Path
    report_md_path: Path
    enriched_trades_csv_path: Path
    bucket_rows_csv_path: Path
    payload: dict[str, Any]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "P1-11 research-only audit: join TRIDENT A/C and optional HIP4 trades "
            "with entry-time market micro-regime buckets."
        )
    )
    parser.add_argument("--snapshot-dir", default=str(DEFAULT_SNAPSHOT_DIR))
    parser.add_argument("--output-dir", default="")
    parser.add_argument(
        "--full-bot-json",
        action="append",
        default=None,
        help="Replay source as LABEL=PATH. Defaults to official baseline and recent live-cap replay.",
    )
    parser.add_argument("--pod-a-log", default=str(DEFAULT_POD_A_LOG))
    parser.add_argument("--pod-c-log", default=str(DEFAULT_POD_C_LOG))
    parser.add_argument("--skip-live-logs", action="store_true")
    parser.add_argument("--hip4-dir", default=str(DEFAULT_HIP4_DIR))
    parser.add_argument("--skip-hip4", action="store_true")
    parser.add_argument("--bucket-seconds", type=int, default=60)
    parser.add_argument("--join-lookback-seconds", type=int, default=900)
    parser.add_argument("--min-trades", type=int, default=4)
    parser.add_argument("--max-dominant-symbol-ratio", type=float, default=0.75)
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def run_p111_micro_regime_replay(
    *,
    snapshot_dir: str | Path = DEFAULT_SNAPSHOT_DIR,
    output_dir: str | Path | None = None,
    full_bot_json_sources: Sequence[str | Path] | None = None,
    pod_a_log: str | Path = DEFAULT_POD_A_LOG,
    pod_c_log: str | Path = DEFAULT_POD_C_LOG,
    include_live_logs: bool = True,
    hip4_dir: str | Path = DEFAULT_HIP4_DIR,
    include_hip4: bool = True,
    bucket_seconds: int = 60,
    join_lookback_seconds: int = 900,
    min_trades: int = 4,
    max_dominant_symbol_ratio: float = 0.75,
    quiet: bool = False,
) -> P111Result:
    if bucket_seconds <= 0:
        raise ValueError("bucket_seconds_must_be_positive")
    if join_lookback_seconds < 0:
        raise ValueError("join_lookback_seconds_must_be_non_negative")
    if min_trades <= 0:
        raise ValueError("min_trades_must_be_positive")
    if not 0.0 < max_dominant_symbol_ratio <= 1.0:
        raise ValueError("max_dominant_symbol_ratio_must_be_between_zero_and_one")

    sources = list(full_bot_json_sources or _default_full_bot_sources())
    trades: list[TradeRow] = []
    for source in sources:
        label, path = _parse_labeled_path(source)
        if path.exists():
            trades.extend(load_full_bot_json_trades(path, fold=label))
        elif not quiet:
            print(f"skip missing full-bot json: {path}", flush=True)
    if include_live_logs:
        trades.extend(load_live_jsonl_trades(Path(pod_a_log), pod="pod_a", fold="live_actual"))
        trades.extend(load_live_jsonl_trades(Path(pod_c_log), pod="pod_c", fold="live_actual"))
    if include_hip4:
        trades.extend(load_hip4_trades(Path(hip4_dir), fold="hip4_mainnet_paper"))

    snapshots = load_entry_snapshots(
        Path(snapshot_dir),
        trades=trades,
        bucket_seconds=bucket_seconds,
        join_lookback_seconds=join_lookback_seconds,
        quiet=quiet,
    )
    enriched = enrich_trades(
        trades,
        snapshots=snapshots,
        bucket_seconds=bucket_seconds,
        join_lookback_seconds=join_lookback_seconds,
    )
    bucket_rows = build_bucket_rows(
        enriched,
        min_trades=min_trades,
        max_dominant_symbol_ratio=max_dominant_symbol_ratio,
    )
    scope_summary = [
        _group_row(scope, [row for row in enriched if row["scope"] == scope], key_name="scope")
        for scope in sorted({str(row.get("scope", "")) for row in enriched})
    ]
    fold_summary = [
        _group_row(fold, [row for row in enriched if row["fold"] == fold], key_name="fold")
        for fold in sorted({str(row.get("fold", "")) for row in enriched})
    ]
    pod_summary = [
        _group_row(pod, [row for row in enriched if row["pod"] == pod], key_name="pod")
        for pod in sorted({str(row.get("pod", "")) for row in enriched})
    ]
    counterfactuals = build_counterfactual_rows(enriched, bucket_rows)
    coverage = build_join_coverage(enriched)

    run_id = utc_stamp()
    out_dir = Path(output_dir or DEFAULT_OUTPUT_PARENT / f"p111_micro_regime_{run_id}")
    report_json = out_dir / "p111_micro_regime_replay.json"
    report_md = out_dir / "p111_micro_regime_replay.md"
    trades_csv = out_dir / "enriched_trades.csv"
    buckets_csv = out_dir / "bucket_rows.csv"
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "kind": "p111_micro_regime_replay",
        "parameters": {
            "snapshot_dir": str(snapshot_dir),
            "full_bot_json_sources": [str(item) for item in sources],
            "include_live_logs": include_live_logs,
            "pod_a_log": str(pod_a_log),
            "pod_c_log": str(pod_c_log),
            "include_hip4": include_hip4,
            "hip4_dir": str(hip4_dir),
            "bucket_seconds": bucket_seconds,
            "join_lookback_seconds": join_lookback_seconds,
            "min_trades": min_trades,
            "max_dominant_symbol_ratio": round(max_dominant_symbol_ratio, 6),
        },
        "summary": {
            "trades_loaded": len(trades),
            "enriched_trades": len(enriched),
            "scope_summary": scope_summary,
            "fold_summary": fold_summary,
            "pod_summary": pod_summary,
            "join_coverage": coverage,
        },
        "counterfactual_rows": counterfactuals,
        "loss_regime_rows": [
            row
            for row in bucket_rows
            if row["classification"] in {"loss_regime", "symbol_specific_loss_regime"}
        ][:40],
        "support_regime_rows": [
            row
            for row in bucket_rows
            if row["classification"] in {"support_regime", "symbol_specific_support_regime"}
        ][:40],
        "bucket_rows": bucket_rows,
        "enriched_trade_sample": enriched[:40],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_md.write_text(render_markdown(payload), encoding="utf-8")
    write_csv(trades_csv, enriched)
    write_csv(buckets_csv, bucket_rows)
    if not quiet:
        print(f"wrote {report_md}", flush=True)
    return P111Result(
        output_dir=out_dir,
        report_json_path=report_json,
        report_md_path=report_md,
        enriched_trades_csv_path=trades_csv,
        bucket_rows_csv_path=buckets_csv,
        payload=payload,
    )


def _default_full_bot_sources() -> list[str]:
    sources: list[str] = []
    if DEFAULT_BASELINE_FULL_BOT_JSON.exists():
        sources.append(f"official_baseline_20260513={DEFAULT_BASELINE_FULL_BOT_JSON}")
    if DEFAULT_RECENT_FULL_BOT_JSON.exists():
        sources.append(f"recent_livecap_p101={DEFAULT_RECENT_FULL_BOT_JSON}")
    return sources


def _parse_labeled_path(value: str | Path) -> tuple[str, Path]:
    text = str(value)
    if "=" not in text:
        path = Path(text)
        return path.stem, path
    label, path = text.split("=", 1)
    clean_label = label.strip() or Path(path).stem
    return clean_label, Path(path)


def load_full_bot_json_trades(path: Path, *, fold: str) -> list[TradeRow]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows: list[TradeRow] = []
    for pod in ("pod_a", "pod_c"):
        pod_payload = payload.get(pod)
        if not isinstance(pod_payload, Mapping):
            continue
        for trade in pod_payload.get("closed_trade_log", []) or []:
            if not isinstance(trade, Mapping):
                continue
            opened_at = str(trade.get("opened_at") or "")
            symbol = _norm_symbol(trade.get("symbol"))
            if not symbol or not opened_at:
                continue
            details = _mapping(trade.get("setup_details"))
            rows.append(
                TradeRow(
                    scope="ac",
                    fold=fold,
                    source=f"full_bot_json:{path.name}",
                    pod=pod,
                    symbol=symbol,
                    side=_norm_side(trade.get("side")),
                    setup=str(trade.get("setup") or trade.get("open_reason") or ""),
                    opened_at=opened_at,
                    closed_at=str(trade.get("closed_at") or ""),
                    close_reason=str(trade.get("close_reason") or ""),
                    pnl_usd=_float(trade.get("pnl_usd")),
                    notional_usd=abs(_float(trade.get("target_notional_usd"))),
                    fees_usd=abs(_float(trade.get("fees_usd"))),
                    market_cluster=str(details.get("market_cluster") or ""),
                    metadata={"json_path": str(path)},
                )
            )
    return rows


def load_live_jsonl_trades(path: Path, *, pod: str, fold: str) -> list[TradeRow]:
    if not path.exists():
        return []
    rows: list[TradeRow] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            if not line.strip() or '"trade_close"' not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, Mapping) or event.get("event_type") != "trade_close":
                continue
            trade = _mapping(event.get("trade"))
            opened_at = str(trade.get("opened_at") or "")
            closed_at = str(trade.get("closed_at") or event.get("timestamp") or "")
            symbol = _norm_symbol(trade.get("symbol"))
            identity = (pod, symbol, opened_at, closed_at, str(trade.get("close_reason") or ""))
            if not symbol or not opened_at or identity in seen:
                continue
            seen.add(identity)
            details = _mapping(trade.get("setup_details"))
            rows.append(
                TradeRow(
                    scope="ac",
                    fold=fold,
                    source=f"live_jsonl:{path.name}",
                    pod=pod,
                    symbol=symbol,
                    side=_norm_side(trade.get("side")),
                    setup=str(trade.get("setup") or trade.get("open_reason") or ""),
                    opened_at=opened_at,
                    closed_at=closed_at,
                    close_reason=str(trade.get("close_reason") or ""),
                    pnl_usd=_float(trade.get("pnl_usd")),
                    notional_usd=abs(_float(trade.get("target_notional_usd"))),
                    fees_usd=abs(_float(trade.get("fees_usd"))),
                    market_cluster=str(details.get("market_cluster") or ""),
                    metadata={"jsonl_path": str(path)},
                )
            )
    return rows


def load_hip4_trades(root: Path, *, fold: str) -> list[TradeRow]:
    trades_path = root / "trades.csv"
    settlements_path = root / "settlements.csv"
    if not trades_path.exists() or not settlements_path.exists():
        return []
    trade_groups: defaultdict[tuple[str, str, str], deque[dict[str, str]]] = defaultdict(deque)
    with trades_path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            key = (
                str(row.get("market_id") or ""),
                str(row.get("outcome") or ""),
                str(row.get("side") or ""),
            )
            trade_groups[key].append(dict(row))

    rows: list[TradeRow] = []
    with settlements_path.open("r", encoding="utf-8", newline="") as handle:
        for settlement in csv.DictReader(handle):
            key = (
                str(settlement.get("market_id") or ""),
                str(settlement.get("outcome") or ""),
                str(settlement.get("side") or ""),
            )
            trade = trade_groups[key].popleft() if trade_groups.get(key) else {}
            opened_at = str(trade.get("ts") or "")
            symbol = _norm_symbol(settlement.get("underlying") or trade.get("underlying"))
            side = _hip4_underlying_side(str(settlement.get("side") or trade.get("side") or ""))
            if not symbol or not opened_at:
                continue
            rows.append(
                TradeRow(
                    scope="hip4",
                    fold=fold,
                    source=f"hip4_csv:{root.name}",
                    pod="hip4",
                    symbol=symbol,
                    side=side,
                    setup=str(trade.get("edge_type") or "outcome"),
                    opened_at=opened_at,
                    closed_at=str(settlement.get("ts") or ""),
                    close_reason=str(settlement.get("result") or settlement.get("notes") or ""),
                    pnl_usd=_float(settlement.get("net_pnl_usdc") or settlement.get("pnl_usdc")),
                    notional_usd=abs(_float(trade.get("size_usdc"))),
                    fees_usd=abs(_float(settlement.get("fee_usdc"))),
                    market_cluster="crypto",
                    metadata={
                        "market_id": str(settlement.get("market_id") or ""),
                        "outcome_side": str(settlement.get("side") or ""),
                    },
                )
            )
    return rows


def load_entry_snapshots(
    snapshot_dir: Path,
    *,
    trades: Sequence[TradeRow],
    bucket_seconds: int,
    join_lookback_seconds: int,
    quiet: bool,
) -> dict[tuple[str, int], SnapshotFeature]:
    needed_symbols = {trade.symbol for trade in trades if trade.symbol}
    needed_dates = _needed_snapshot_dates(trades, lookback_seconds=join_lookback_seconds)
    if not needed_symbols or not needed_dates:
        return {}
    min_ts, max_ts = _trade_ts_bounds(trades, lookback_seconds=join_lookback_seconds)
    snapshots: dict[tuple[str, int], SnapshotFeature] = {}
    files = [snapshot_dir / f"{date_key}.jsonl" for date_key in sorted(needed_dates)]
    for index, path in enumerate(files, start=1):
        if not path.exists():
            continue
        accepted = 0
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                timestamp = str(payload.get("timestamp") or "")
                dt = parse_ts(timestamp)
                if dt is None:
                    continue
                bucket = bucket_ts(dt, bucket_seconds)
                if bucket < min_ts or bucket > max_ts:
                    continue
                cluster_regs = _mapping(payload.get("cluster_regime_snapshots"))
                fallback_regime = _mapping(payload.get("regime_snapshot"))
                for item in payload.get("symbols") or []:
                    if not isinstance(item, Mapping):
                        continue
                    symbol = _norm_symbol(item.get("symbol"))
                    if symbol not in needed_symbols:
                        continue
                    key = (symbol, bucket)
                    if key in snapshots:
                        continue
                    cluster = str(item.get("market_cluster") or _infer_cluster(symbol))
                    regime_payload = _mapping(cluster_regs.get(cluster)) or fallback_regime
                    snapshots[key] = SnapshotFeature(
                        timestamp=timestamp,
                        ts=bucket,
                        symbol=symbol,
                        market_cluster=cluster,
                        base_regime=_classify_snapshot_regime(regime_payload),
                        price=_float(item.get("price")),
                        bucket_range_bps=_float(item.get("bucket_range_bps")),
                        realized_vol_short_bps=_float(item.get("realized_vol_short_bps")),
                        volume_ratio=_float(item.get("volume_ratio"), 1.0),
                        vwap_distance_bps=_float(item.get("vwap_distance_bps")),
                        microprice_dislocation_bps=_float(item.get("microprice_dislocation_bps")),
                        spread_bps=_float(item.get("spread_bps")),
                        trade_flow_bias=_float(item.get("trade_flow_bias")),
                        book_imbalance=_float(item.get("book_imbalance")),
                        source_file=path.name,
                    )
                    accepted += 1
        if not quiet and (index == 1 or index == len(files) or index % 10 == 0):
            print(
                f"snapshots {index}/{len(files)} accepted={accepted} total={len(snapshots)}",
                flush=True,
            )
    return snapshots


def enrich_trades(
    trades: Sequence[TradeRow],
    *,
    snapshots: Mapping[tuple[str, int], SnapshotFeature],
    bucket_seconds: int,
    join_lookback_seconds: int,
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    max_steps = join_lookback_seconds // bucket_seconds if bucket_seconds > 0 else 0
    for trade in trades:
        opened_dt = parse_ts(trade.opened_at)
        if opened_dt is None:
            continue
        entry_bucket = bucket_ts(opened_dt, bucket_seconds)
        snapshot = None
        join_lag_seconds = None
        for step in range(max_steps + 1):
            candidate_bucket = entry_bucket - step * bucket_seconds
            candidate = snapshots.get((trade.symbol, candidate_bucket))
            if candidate is not None:
                snapshot = candidate
                join_lag_seconds = entry_bucket - candidate_bucket
                break
        row = asdict(trade)
        row.pop("metadata", None)
        row["opened_ts"] = entry_bucket
        row["opened_date"] = opened_dt.date().isoformat()
        row["notional_usd"] = round(float(trade.notional_usd or 0.0), 6)
        row["pnl_usd"] = round(float(trade.pnl_usd or 0.0), 6)
        row["fees_usd"] = round(float(trade.fees_usd or 0.0), 6)
        row["net_bps"] = round(_bps(trade.pnl_usd, trade.notional_usd), 6)
        row["is_win"] = trade.pnl_usd > 0.0
        row["snapshot_joined"] = snapshot is not None
        row["snapshot_join_lag_seconds"] = join_lag_seconds
        if snapshot is None:
            row.update(_missing_micro_regime(trade))
            enriched.append(row)
            continue
        features = {
            "bucket_range_bps": snapshot.bucket_range_bps,
            "realized_vol_short_bps": snapshot.realized_vol_short_bps,
            "volume_ratio": snapshot.volume_ratio,
            "vwap_distance_bps": snapshot.vwap_distance_bps,
            "microprice_dislocation_bps": snapshot.microprice_dislocation_bps,
        }
        micro = build_market_micro_regime(features, symbol=trade.symbol, side=trade.side)
        row.update(
            {
                "snapshot_timestamp": snapshot.timestamp,
                "snapshot_source_file": snapshot.source_file,
                "entry_price_snapshot": round(snapshot.price, 8),
                "market_cluster": trade.market_cluster or snapshot.market_cluster,
                "base_regime": snapshot.base_regime,
                "bucket_range_bps": round(snapshot.bucket_range_bps, 6),
                "realized_vol_short_bps": round(snapshot.realized_vol_short_bps, 6),
                "volume_ratio": round(snapshot.volume_ratio, 6),
                "vwap_distance_bps": round(snapshot.vwap_distance_bps, 6),
                "microprice_dislocation_bps": round(snapshot.microprice_dislocation_bps, 6),
                "spread_bps": round(snapshot.spread_bps, 6),
                "trade_flow_bias": round(snapshot.trade_flow_bias, 6),
                "book_imbalance": round(snapshot.book_imbalance, 6),
                "range_bucket": micro["range_bucket"],
                "short_vol_bucket": micro["short_vol_bucket"],
                "volume_ratio_bucket": micro["volume_ratio_bucket"],
                "vwap_bucket": micro["vwap_bucket"],
                "microprice_bucket": micro["microprice_bucket"],
                "range_vol_regime": micro["range_vol_regime"],
                "flow_regime": micro["flow_regime"],
                "micro_regime": micro["micro_regime"],
                "symbol_range_vol": micro["symbol_range_vol"],
                "symbol_micro_regime": micro["symbol_micro_regime"],
            }
        )
        enriched.append(row)
    return enriched


def _missing_micro_regime(trade: TradeRow) -> dict[str, Any]:
    symbol = trade.symbol or "unknown"
    return {
        "snapshot_timestamp": "",
        "snapshot_source_file": "",
        "entry_price_snapshot": 0.0,
        "base_regime": "missing_snapshot",
        "range_bucket": "missing_snapshot",
        "short_vol_bucket": "missing_snapshot",
        "volume_ratio_bucket": "missing_snapshot",
        "vwap_bucket": "missing_snapshot",
        "microprice_bucket": "missing_snapshot",
        "range_vol_regime": "missing_snapshot",
        "flow_regime": "missing_snapshot",
        "micro_regime": "missing_snapshot",
        "symbol_range_vol": f"{symbol}|missing_snapshot",
        "symbol_micro_regime": f"{symbol}|missing_snapshot",
    }


def build_bucket_rows(
    trades: Sequence[Mapping[str, Any]],
    *,
    min_trades: int,
    max_dominant_symbol_ratio: float,
) -> list[dict[str, Any]]:
    buckets: defaultdict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for trade in trades:
        scope = str(trade.get("scope") or "unknown")
        for family, bucket in _bucket_keys(trade):
            buckets[(scope, family, bucket)].append(trade)
    rows = [
        _bucket_row(
            scope=scope,
            family=family,
            bucket=bucket,
            trades=items,
            min_trades=min_trades,
            max_dominant_symbol_ratio=max_dominant_symbol_ratio,
        )
        for (scope, family, bucket), items in buckets.items()
    ]
    rows.sort(key=_bucket_sort_key)
    return rows


def _bucket_keys(trade: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    symbol = str(trade.get("symbol") or "unknown")
    pod = str(trade.get("pod") or "unknown")
    return (
        ("pod", pod),
        ("symbol", symbol),
        ("pod_symbol", f"{pod}|{symbol}"),
        ("side", str(trade.get("side") or "unknown")),
        ("market_cluster", str(trade.get("market_cluster") or "unknown")),
        ("base_regime", str(trade.get("base_regime") or "unknown")),
        ("range_bucket", str(trade.get("range_bucket") or "unknown")),
        ("short_vol_bucket", str(trade.get("short_vol_bucket") or "unknown")),
        ("volume_ratio_bucket", str(trade.get("volume_ratio_bucket") or "unknown")),
        ("vwap_bucket", str(trade.get("vwap_bucket") or "unknown")),
        ("microprice_bucket", str(trade.get("microprice_bucket") or "unknown")),
        ("range_vol_regime", str(trade.get("range_vol_regime") or "unknown")),
        ("flow_regime", str(trade.get("flow_regime") or "unknown")),
        ("micro_regime", str(trade.get("micro_regime") or "unknown")),
        ("symbol_range_vol", str(trade.get("symbol_range_vol") or f"{symbol}|unknown")),
        ("symbol_micro_regime", str(trade.get("symbol_micro_regime") or f"{symbol}|unknown")),
    )


def _bucket_row(
    *,
    scope: str,
    family: str,
    bucket: str,
    trades: Sequence[Mapping[str, Any]],
    min_trades: int,
    max_dominant_symbol_ratio: float,
) -> dict[str, Any]:
    row = _group_row(bucket, trades, key_name="bucket")
    row["scope"] = scope
    row["family"] = family
    row["classification"] = _classification(
        row,
        min_trades=min_trades,
        max_dominant_symbol_ratio=max_dominant_symbol_ratio,
    )
    return row


def _group_row(label: str, trades: Sequence[Mapping[str, Any]], *, key_name: str) -> dict[str, Any]:
    rows = list(trades)
    pnl = sum(_float(row.get("pnl_usd")) for row in rows)
    notional = sum(abs(_float(row.get("notional_usd"))) for row in rows)
    fees = sum(abs(_float(row.get("fees_usd"))) for row in rows)
    wins = sum(1 for row in rows if _float(row.get("pnl_usd")) > 0.0)
    losses = len(rows) - wins
    gross_pos = sum(max(_float(row.get("pnl_usd")), 0.0) for row in rows)
    gross_neg = abs(sum(min(_float(row.get("pnl_usd")), 0.0) for row in rows))
    symbols = Counter(str(row.get("symbol") or "unknown") for row in rows)
    folds = Counter(str(row.get("fold") or "unknown") for row in rows)
    pods = Counter(str(row.get("pod") or "unknown") for row in rows)
    close_reasons = Counter(str(row.get("close_reason") or "unknown") for row in rows)
    fold_pnl: defaultdict[str, float] = defaultdict(float)
    symbol_pnl: defaultdict[str, float] = defaultdict(float)
    for row in rows:
        fold_pnl[str(row.get("fold") or "unknown")] += _float(row.get("pnl_usd"))
        symbol_pnl[str(row.get("symbol") or "unknown")] += _float(row.get("pnl_usd"))
    negative_folds = sum(1 for value in fold_pnl.values() if value < 0.0)
    positive_folds = sum(1 for value in fold_pnl.values() if value > 0.0)
    dominant_symbol_ratio = max(symbols.values()) / len(rows) if rows and symbols else 0.0
    stop_hits = sum(1 for row in rows if _is_stop_close(row.get("close_reason")))
    missing_snapshots = sum(1 for row in rows if not bool(row.get("snapshot_joined")))
    return {
        key_name: label,
        "trades": len(rows),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(rows), 6) if rows else 0.0,
        "pnl_usd": round(pnl, 6),
        "notional_usd": round(notional, 6),
        "fees_usd": round(fees, 6),
        "avg_net_bps": round(_bps(pnl, notional), 6),
        "profit_factor": round(gross_pos / gross_neg, 6) if gross_neg > 0 else None,
        "stop_hits": stop_hits,
        "stop_rate": round(stop_hits / len(rows), 6) if rows else 0.0,
        "missing_snapshots": missing_snapshots,
        "missing_snapshot_rate": round(missing_snapshots / len(rows), 6) if rows else 0.0,
        "folds_with_trades": len(folds),
        "positive_folds": positive_folds,
        "negative_folds": negative_folds,
        "symbols": dict(sorted(symbols.items())),
        "symbol_count": len(symbols),
        "pods": dict(sorted(pods.items())),
        "dominant_symbol_ratio": round(dominant_symbol_ratio, 6),
        "close_reasons": dict(sorted(close_reasons.items())),
        "fold_pnl_usd": {key: round(value, 6) for key, value in sorted(fold_pnl.items())},
        "symbol_pnl_usd": {key: round(value, 6) for key, value in sorted(symbol_pnl.items())},
    }


def _classification(
    row: Mapping[str, Any],
    *,
    min_trades: int,
    max_dominant_symbol_ratio: float,
) -> str:
    trades = int(_float(row.get("trades")))
    if trades < min_trades:
        return "insufficient_samples"
    if _float(row.get("missing_snapshot_rate")) >= 0.5:
        return "insufficient_snapshot_coverage"
    pnl = _float(row.get("pnl_usd"))
    negative_folds = int(_float(row.get("negative_folds")))
    positive_folds = int(_float(row.get("positive_folds")))
    dominant = _float(row.get("dominant_symbol_ratio"))
    symbol_specific = dominant > max_dominant_symbol_ratio
    if pnl < 0.0 and negative_folds >= max(1, positive_folds):
        return "symbol_specific_loss_regime" if symbol_specific else "loss_regime"
    if pnl > 0.0 and positive_folds > negative_folds:
        return "symbol_specific_support_regime" if symbol_specific else "support_regime"
    return "mixed_or_fold_unstable"


def _bucket_sort_key(row: Mapping[str, Any]) -> tuple[int, str, float, int, str, str]:
    classification_rank = {
        "loss_regime": 0,
        "symbol_specific_loss_regime": 1,
        "support_regime": 2,
        "symbol_specific_support_regime": 3,
        "mixed_or_fold_unstable": 4,
        "insufficient_snapshot_coverage": 5,
        "insufficient_samples": 6,
    }.get(str(row.get("classification") or ""), 9)
    return (
        classification_rank,
        str(row.get("scope") or ""),
        _float(row.get("pnl_usd")),
        -int(_float(row.get("trades"))),
        str(row.get("family") or ""),
        str(row.get("bucket") or ""),
    )


def build_counterfactual_rows(
    trades: Sequence[Mapping[str, Any]],
    bucket_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    ac_trades = [row for row in trades if row.get("scope") == "ac"]
    loss_range_vol = {
        str(row.get("bucket"))
        for row in bucket_rows
        if row.get("scope") == "ac"
        and row.get("family") == "range_vol_regime"
        and row.get("classification") == "loss_regime"
    }
    loss_symbol_range_vol = {
        str(row.get("bucket"))
        for row in bucket_rows
        if row.get("scope") == "ac"
        and row.get("family") == "symbol_range_vol"
        and row.get("classification") in {"loss_regime", "symbol_specific_loss_regime"}
    }
    profiles = [
        (
            "baseline_no_filter",
            "All A/C trades loaded.",
            lambda row: 1.0,
        ),
        (
            "veto_range_mid_vol_high",
            "Drop A/C trades whose entry range_vol_regime is range_mid|vol_high.",
            lambda row: 0.0 if row.get("range_vol_regime") == "range_mid|vol_high" else 1.0,
        ),
        (
            "half_size_micro_adverse",
            "Scale A/C trades by 0.5 when entry microprice_bucket is micro_adverse.",
            lambda row: 0.5 if row.get("microprice_bucket") == "micro_adverse" else 1.0,
        ),
        (
            "veto_data_mined_loss_range_vol",
            "Research-only: drop range_vol buckets classified as A/C loss regimes in this sample.",
            lambda row: 0.0 if row.get("range_vol_regime") in loss_range_vol else 1.0,
        ),
        (
            "veto_data_mined_loss_symbol_range_vol",
            "Research-only: drop symbol_range_vol buckets classified as A/C loss regimes in this sample.",
            lambda row: 0.0 if row.get("symbol_range_vol") in loss_symbol_range_vol else 1.0,
        ),
    ]
    baseline = _scaled_group(ac_trades, lambda _row: 1.0)
    rows: list[dict[str, Any]] = []
    for name, description, scale_func in profiles:
        stats = _scaled_group(ac_trades, scale_func)
        stats.update(
            {
                "profile": name,
                "description": description,
                "delta_vs_baseline_usd": round(stats["pnl_usd"] - baseline["pnl_usd"], 6),
                "status": _counterfactual_status(name, stats, baseline),
            }
        )
        rows.append(stats)
    return rows


def _scaled_group(
    rows: Sequence[Mapping[str, Any]],
    scale_func: Any,
) -> dict[str, Any]:
    scaled: list[dict[str, Any]] = []
    vetoed = 0
    reduced = 0
    for row in rows:
        scale = float(scale_func(row))
        if scale <= 0.0:
            vetoed += 1
            continue
        if scale < 1.0:
            reduced += 1
        item = dict(row)
        item["pnl_usd"] = _float(row.get("pnl_usd")) * scale
        item["notional_usd"] = _float(row.get("notional_usd")) * scale
        item["fees_usd"] = _float(row.get("fees_usd")) * scale
        scaled.append(item)
    stats = _group_row("scaled", scaled, key_name="profile_scope")
    stats["kept_trades"] = stats.pop("trades")
    stats["input_trades"] = len(rows)
    stats["vetoed_trades"] = vetoed
    stats["reduced_trades"] = reduced
    return stats


def _counterfactual_status(name: str, stats: Mapping[str, Any], baseline: Mapping[str, Any]) -> str:
    if name == "baseline_no_filter":
        return "reference"
    input_trades = int(_float(stats.get("input_trades")))
    vetoed = int(_float(stats.get("vetoed_trades")))
    if input_trades and vetoed / input_trades > 0.35:
        return "research_only_overfilters"
    if _float(stats.get("pnl_usd")) > _float(baseline.get("pnl_usd")):
        return "research_candidate_requires_replay"
    return "no_improvement"


def build_join_coverage(trades: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_scope: dict[str, dict[str, Any]] = {}
    for scope in sorted({str(row.get("scope") or "unknown") for row in trades}):
        rows = [row for row in trades if str(row.get("scope") or "unknown") == scope]
        joined = sum(1 for row in rows if row.get("snapshot_joined"))
        by_scope[scope] = {
            "trades": len(rows),
            "joined": joined,
            "missing": len(rows) - joined,
            "join_rate": round(joined / len(rows), 6) if rows else 0.0,
        }
    return by_scope


def render_markdown(payload: Mapping[str, Any]) -> str:
    summary = _mapping(payload.get("summary"))
    lines = [
        "# P1-11 Micro-Regime Replay",
        "",
        f"- Generated at: `{payload.get('generated_at')}`",
        "- Mode: `research_only_no_live_change`",
        "- Scope: TRIDENT A/C primary; HIP4 optional exploratory join by underlying.",
        "",
        "## Summary",
        "",
        f"- Trades loaded: `{summary.get('trades_loaded', 0)}`",
        f"- Enriched trades: `{summary.get('enriched_trades', 0)}`",
        f"- Join coverage: `{summary.get('join_coverage', {})}`",
        "",
        "### By Fold",
        "",
        "| Fold | Trades | PnL | Avg bps | WR | PF | Stops | Missing snapshots |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary.get("fold_summary", []):
        if isinstance(row, Mapping):
            lines.append(_summary_markdown_row(row, key="fold"))
    lines.extend(
        [
            "",
            "## Counterfactuals A/C",
            "",
            "| Profile | Status | Input | Kept | Vetoed | Reduced | PnL | Delta | PF |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in payload.get("counterfactual_rows", []):
        if not isinstance(row, Mapping):
            continue
        lines.append(
            "| "
            f"`{row.get('profile')}` | `{row.get('status')}` | "
            f"{int(_float(row.get('input_trades')))} | {int(_float(row.get('kept_trades')))} | "
            f"{int(_float(row.get('vetoed_trades')))} | {int(_float(row.get('reduced_trades')))} | "
            f"{_fmt_money(row.get('pnl_usd'))} | {_fmt_money(row.get('delta_vs_baseline_usd'))} | "
            f"{_fmt_optional(row.get('profit_factor'))} |"
        )
    lines.extend(
        [
            "",
            "## Loss Regimes",
            "",
            "| Scope | Family | Bucket | Class | Trades | PnL | Avg bps | Neg folds | Dominant | Symbols |",
            "|---|---|---|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in payload.get("loss_regime_rows", [])[:25]:
        if isinstance(row, Mapping):
            lines.append(_bucket_markdown_row(row))
    if not payload.get("loss_regime_rows"):
        lines.append("| none | n/a | n/a | n/a | 0 | $0.00 | 0.00 | 0 | 0.00 | {} |")
    lines.extend(
        [
            "",
            "## Support Regimes",
            "",
            "| Scope | Family | Bucket | Class | Trades | PnL | Avg bps | Neg folds | Dominant | Symbols |",
            "|---|---|---|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in payload.get("support_regime_rows", [])[:25]:
        if isinstance(row, Mapping):
            lines.append(_bucket_markdown_row(row))
    if not payload.get("support_regime_rows"):
        lines.append("| none | n/a | n/a | n/a | 0 | $0.00 | 0.00 | 0 | 0.00 | {} |")
    lines.extend(
        [
            "",
            "## Interpretation Guardrails",
            "",
            "- This report is post-trade research. Buckets marked as candidates are not live rules.",
            "- Data-mined veto profiles must be replayed on baseline, recent and walk-forward windows before any promotion.",
            "- HIP4 rows are exploratory because outcome positions depend on expiry/strike/probability, not only the underlying micro-regime.",
            "",
        ]
    )
    return "\n".join(lines)


def _summary_markdown_row(row: Mapping[str, Any], *, key: str) -> str:
    return (
        f"| `{row.get(key)}` | {int(_float(row.get('trades')))} | {_fmt_money(row.get('pnl_usd'))} | "
        f"{_float(row.get('avg_net_bps')):.2f} | {_float(row.get('win_rate')):.2%} | "
        f"{_fmt_optional(row.get('profit_factor'))} | {int(_float(row.get('stop_hits')))} | "
        f"{int(_float(row.get('missing_snapshots')))} |"
    )


def _bucket_markdown_row(row: Mapping[str, Any]) -> str:
    return (
        f"| `{row.get('scope')}` | `{row.get('family')}` | `{row.get('bucket')}` | "
        f"`{row.get('classification')}` | {int(_float(row.get('trades')))} | "
        f"{_fmt_money(row.get('pnl_usd'))} | {_float(row.get('avg_net_bps')):.2f} | "
        f"{int(_float(row.get('negative_folds')))} | {_float(row.get('dominant_symbol_ratio')):.2f} | "
        f"`{row.get('symbols', {})}` |"
    )


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_cell(row.get(key)) for key in fieldnames})


def _needed_snapshot_dates(trades: Sequence[TradeRow], *, lookback_seconds: int) -> set[str]:
    dates: set[str] = set()
    for trade in trades:
        dt = parse_ts(trade.opened_at)
        if dt is None:
            continue
        dates.add(dt.date().isoformat())
        if lookback_seconds > 0:
            dates.add((dt - timedelta(seconds=lookback_seconds)).date().isoformat())
    return dates


def _trade_ts_bounds(trades: Sequence[TradeRow], *, lookback_seconds: int) -> tuple[int, int]:
    timestamps = [int(dt.timestamp()) for trade in trades if (dt := parse_ts(trade.opened_at))]
    if not timestamps:
        return 0, 0
    return min(timestamps) - lookback_seconds, max(timestamps)


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def bucket_ts(dt: datetime, bucket_seconds: int) -> int:
    return int(dt.timestamp()) // bucket_seconds * bucket_seconds


def utc_stamp(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    return current.strftime("%Y%m%dT%H%M%SZ")


def _classify_snapshot_regime(snapshot: Mapping[str, Any]) -> str:
    ready = bool(snapshot.get("ready", True))
    if not ready:
        return "Cash"
    adx = _float(snapshot.get("adx"))
    atr_ratio = _float(snapshot.get("atr_ratio"))
    range_width_bps = _float(snapshot.get("range_width_bps"))
    structure = abs(_float(snapshot.get("structure_score")))
    impulse = bool(snapshot.get("btc_impulse"))
    if atr_ratio >= 1.8 or impulse:
        return "PanicSqueeze"
    if adx >= 22.0 and structure >= 0.30:
        return "TrendExpansion"
    if atr_ratio <= 0.45 or range_width_bps <= 80.0:
        return "DeadZone"
    return "RangeAuction"


def _infer_cluster(symbol: str) -> str:
    if symbol.startswith("XYZ:CL") or symbol.startswith("XYZ:BRENTOIL"):
        return "oil"
    if symbol == "XYZ:GOLD":
        return "gold"
    if symbol == "XYZ:SILVER":
        return "silver"
    if symbol in {"XYZ:SP500", "XYZ:XYZ100"}:
        return "index"
    if symbol == "XYZ:JPY":
        return "fx"
    if symbol.startswith("XYZ:"):
        return "equity"
    return "crypto"


def _hip4_underlying_side(side: str) -> str:
    normalized = side.strip().upper()
    if normalized == "BUY_NO":
        return "short"
    return "long"


def _norm_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def _norm_side(value: Any) -> str:
    side = str(value or "").strip().lower()
    if side in {"buy_no", "short"}:
        return "short"
    if side in {"buy_yes", "long"}:
        return "long"
    return side or "unknown"


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        result = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(result) or math.isinf(result):
        return default
    return result


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _bps(pnl: float, notional: float) -> float:
    return pnl / notional * 10_000.0 if notional > 0.0 else 0.0


def _is_stop_close(value: Any) -> bool:
    return "stop" in str(value or "").lower()


def _fmt_money(value: Any) -> str:
    return f"${_float(value):.2f}"


def _fmt_optional(value: Any) -> str:
    if value is None or value == "":
        return "n/a"
    return f"{_float(value):.2f}"


def _csv_cell(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True)
    return value


def main() -> None:
    args = parse_args()
    run_p111_micro_regime_replay(
        snapshot_dir=args.snapshot_dir,
        output_dir=args.output_dir or None,
        full_bot_json_sources=args.full_bot_json,
        pod_a_log=args.pod_a_log,
        pod_c_log=args.pod_c_log,
        include_live_logs=not args.skip_live_logs,
        hip4_dir=args.hip4_dir,
        include_hip4=not args.skip_hip4,
        bucket_seconds=args.bucket_seconds,
        join_lookback_seconds=args.join_lookback_seconds,
        min_trades=args.min_trades,
        max_dominant_symbol_ratio=args.max_dominant_symbol_ratio,
        quiet=args.quiet,
    )


if __name__ == "__main__":
    main()
