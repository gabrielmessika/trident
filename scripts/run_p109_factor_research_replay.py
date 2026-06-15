#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SNAPSHOT_DIR = ROOT / "server-data" / "live_snapshots"
DEFAULT_POD_A_LOG = ROOT / "server-data" / "logs" / "pod_a_live.jsonl"
DEFAULT_POD_C_LOG = ROOT / "server-data" / "logs" / "pod_c_live.jsonl"
DEFAULT_HIP4_PAPER_DIR = ROOT / "server-data" / "hip4" / "logs" / "hip4_outcome_mainnet_paper"
DEFAULT_HIP4_QUALITY_FILE = ROOT / "server-data" / "hip4" / "logs" / "hip4_nautilus_shadow" / "data_quality.csv"
HORIZONS_MIN = (60, 240)


@dataclass(frozen=True, slots=True)
class Obs:
    ts: int
    symbol: str
    cluster: str
    price: float
    hour: int
    dow: int
    regime: str
    spread_bps: float
    structure: float
    flow: float
    book: float
    vwap: float
    micro: float
    compression: float
    vol_short: float
    range_bps: float
    volume_ratio: float
    trade_count_ratio: float
    notional: float
    source_file: str


@dataclass(frozen=True, slots=True)
class FactorVariant:
    name: str
    description: str
    side: str
    horizon_min: int
    cluster_cap_key: str
    cooldown_min: int
    selector: Callable[[Obs, dict[str, dict[int, float]], argparse.Namespace], tuple[str, float] | None]


@dataclass(frozen=True, slots=True)
class FactorTrade:
    variant: str
    timestamp: str
    ts: int
    exit_timestamp: str
    symbol: str
    cluster: str
    side: str
    horizon_min: int
    regime: str
    hour_utc: int
    dow_utc: int
    month: str
    entry_price: float
    exit_price: float
    gross_return_bps: float
    spread_bps: float
    fee_slippage_bps: float
    all_in_cost_bps: float
    net_return_bps: float
    notional_usd: float
    gross_pnl_usd: float
    cost_usd: float
    net_pnl_usd: float
    score: float
    reason: str


@dataclass(frozen=True, slots=True)
class ConstraintResult:
    variant: str
    raw_count: int
    kept_count: int
    dropped_symbol_cooldown: int
    dropped_correlation_cap: int


@dataclass(frozen=True, slots=True)
class Hip4Settlement:
    ts: datetime | None
    open_ts: datetime | None
    market_id: str
    underlying: str
    side: str
    edge_type: str
    result: str
    pnl_usdc: float
    fee_usdc: float
    is_win: bool
    open_seconds_to_expiry: float | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="P1-09 factor research replay from raw server-data.")
    parser.add_argument("--snapshot-dir", default=str(DEFAULT_SNAPSHOT_DIR))
    parser.add_argument("--pod-a-log", default=str(DEFAULT_POD_A_LOG))
    parser.add_argument("--pod-c-log", default=str(DEFAULT_POD_C_LOG))
    parser.add_argument("--hip4-paper-dir", default=str(DEFAULT_HIP4_PAPER_DIR))
    parser.add_argument("--hip4-quality-file", default=str(DEFAULT_HIP4_QUALITY_FILE))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--notional-usd", type=float, default=200.0)
    parser.add_argument("--fee-slippage-bps", type=float, default=16.0)
    parser.add_argument("--extra-slippage-bps", type=float, default=0.0)
    parser.add_argument("--max-all-in-cost-bps", type=float, default=32.0)
    parser.add_argument("--extended-alt-max-spread-bps", type=float, default=15.0)
    parser.add_argument("--crypto-max-spread-bps", type=float, default=12.0)
    parser.add_argument("--max-correlated-positions", type=int, default=3)
    parser.add_argument("--hip4-quality-threshold", type=float, default=0.75)
    parser.add_argument("--hip4-max-book-age-ms", type=float, default=30_000.0)
    parser.add_argument("--hip4-max-reference-divergence-bps", type=float, default=50.0)
    parser.add_argument("--min-trades-shadow", type=int, default=30)
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def parse_market_expiry(market_id: str) -> datetime | None:
    m = re.search(r"_(20\d{6})_(\d{4})$", market_id)
    if not m:
        return None
    try:
        return datetime.strptime("".join(m.groups()), "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def dt_to_bucket(dt: datetime, bucket_seconds: int = 300) -> int:
    return int(dt.timestamp()) // bucket_seconds * bucket_seconds


def iso_from_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def month_label(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m")


def f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def infer_cluster(symbol: str, explicit: Any = None) -> str:
    if explicit:
        return str(explicit)
    if not symbol.startswith("XYZ:"):
        return "crypto"
    tail = symbol.split(":", 1)[1]
    if tail in {"CL", "BRENTOIL"}:
        return "oil"
    if tail == "GOLD":
        return "gold"
    if tail == "SILVER":
        return "silver"
    if tail == "JPY":
        return "fx"
    if tail in {"SP500", "XYZ100"}:
        return "index"
    return "equity"


def regime_mode(regime: dict[str, Any] | None, cluster: str) -> str:
    if not regime or not regime.get("ready", True):
        return "not_ready"
    adx = f(regime.get("adx"))
    atr = f(regime.get("atr_ratio"))
    width = f(regime.get("range_width_bps"))
    structure = f(regime.get("structure_score"))
    breadth = f(regime.get("breadth_pct"))
    coherence = f(regime.get("coherence_score"))
    symbol_count = int(f(regime.get("symbol_count")))
    if atr >= 0.85 or width >= 30.0:
        return "high_vol"
    if cluster == "crypto" and symbol_count >= 5:
        if breadth >= 0.65 and structure >= 0.10 and coherence >= 0.45:
            return "broad_up"
        if breadth <= 0.35 and structure <= -0.10 and coherence >= 0.30:
            return "broad_down"
        if adx < 15.0 or coherence < 0.30:
            return "chop"
        if breadth >= 0.65:
            return "positive_breadth"
        if breadth <= 0.35:
            return "negative_breadth"
        return "mixed"
    if structure >= 0.20 and adx >= 14.0:
        return "uptrend"
    if structure <= -0.20 and adx >= 14.0:
        return "downtrend"
    if adx < 12.0 or coherence < 0.25:
        return "chop"
    return "mixed"


def trailing_return_bps(price_by_symbol: dict[str, dict[int, float]], symbol: str, ts: int, lookback_min: int) -> float | None:
    prices = price_by_symbol.get(symbol, {})
    now = prices.get(ts)
    prev = prices.get(ts - lookback_min * 60)
    if not now or not prev or prev <= 0:
        return None
    return (now / prev - 1.0) * 10000.0


def relative_crypto_return_bps(
    price_by_symbol: dict[str, dict[int, float]],
    symbol: str,
    ts: int,
    lookback_min: int,
) -> float | None:
    local = trailing_return_bps(price_by_symbol, symbol, ts, lookback_min)
    if local is None:
        return None
    leaders = [
        ret
        for leader in ("BTC", "ETH")
        if (ret := trailing_return_bps(price_by_symbol, leader, ts, lookback_min)) is not None
    ]
    if not leaders:
        return None
    return local - sum(leaders) / len(leaders)


def load_snapshots(snapshot_dir: Path, *, quiet: bool = False) -> tuple[dict[tuple[str, int], Obs], dict[str, dict[int, float]], dict[str, Any]]:
    obs_by_key: dict[tuple[str, int], Obs] = {}
    price_by_symbol: dict[str, dict[int, float]] = defaultdict(dict)
    file_rows: list[dict[str, Any]] = []
    files = sorted(snapshot_dir.glob("*.jsonl"))
    started = time.perf_counter()
    for idx, path in enumerate(files, start=1):
        parsed_lines = 0
        accepted_symbols = 0
        min_ts: int | None = None
        max_ts: int | None = None
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if not line.strip():
                    continue
                parsed_lines += 1
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                dt = parse_ts(str(row.get("timestamp") or ""))
                if not dt:
                    continue
                bucket = dt_to_bucket(dt)
                cluster_regs = row.get("cluster_regime_snapshots") or {}
                for sym in row.get("symbols") or []:
                    symbol = str(sym.get("symbol") or "")
                    if not symbol or sym.get("source") != "hyperliquid_live_collector":
                        continue
                    price = f(sym.get("price"))
                    if price <= 0:
                        continue
                    cluster = infer_cluster(symbol, sym.get("market_cluster"))
                    reg = cluster_regs.get(cluster) or row.get("regime_snapshot") or {}
                    key = (symbol, bucket)
                    if key in obs_by_key:
                        continue
                    obs_by_key[key] = Obs(
                        ts=bucket,
                        symbol=symbol,
                        cluster=cluster,
                        price=price,
                        hour=dt.hour,
                        dow=dt.weekday(),
                        regime=regime_mode(reg, cluster),
                        spread_bps=f(sym.get("spread_bps")),
                        structure=f(sym.get("structure_score")),
                        flow=f(sym.get("trade_flow_bias")),
                        book=f(sym.get("book_imbalance")),
                        vwap=f(sym.get("vwap_distance_bps")),
                        micro=f(sym.get("microprice_dislocation_bps")),
                        compression=f(sym.get("compression_score")),
                        vol_short=f(sym.get("realized_vol_short_bps")),
                        range_bps=f(sym.get("bucket_range_bps")),
                        volume_ratio=f(sym.get("volume_ratio"), 1.0),
                        trade_count_ratio=f(sym.get("trade_count_ratio"), 1.0),
                        notional=f(sym.get("bucket_notional_usd")),
                        source_file=path.name,
                    )
                    price_by_symbol[symbol][bucket] = price
                    accepted_symbols += 1
                    min_ts = bucket if min_ts is None else min(min_ts, bucket)
                    max_ts = bucket if max_ts is None else max(max_ts, bucket)
        file_rows.append(
            {
                "file": path.name,
                "lines": parsed_lines,
                "accepted_symbol_buckets": accepted_symbols,
                "min_ts": iso_from_ts(min_ts) if min_ts else None,
                "max_ts": iso_from_ts(max_ts) if max_ts else None,
            }
        )
        if not quiet and (idx == 1 or idx == len(files) or idx % 10 == 0):
            print(f"snapshots {idx}/{len(files)} accepted={len(obs_by_key)} elapsed={time.perf_counter() - started:.1f}s", flush=True)
    for prices in price_by_symbol.values():
        # Plain dict lookups are used for exact 5m horizons; sorting happens only
        # to keep deterministic iteration in tests and reports.
        dict(sorted(prices.items()))
    first_ts = min((obs.ts for obs in obs_by_key.values()), default=0)
    last_ts = max((obs.ts for obs in obs_by_key.values()), default=0)
    coverage = {
        "files": file_rows,
        "first_ts": iso_from_ts(first_ts) if first_ts else None,
        "last_ts": iso_from_ts(last_ts) if last_ts else None,
        "symbol_bucket_count": len(obs_by_key),
        "symbol_count": len({obs.symbol for obs in obs_by_key.values()}),
        "clusters": dict(Counter(obs.cluster for obs in obs_by_key.values())),
        "sampling": "5m collector-only buckets, duplicate symbol/time kept once",
    }
    return obs_by_key, price_by_symbol, coverage


def all_in_cost_bps(obs: Obs, args: argparse.Namespace) -> float:
    return max(0.0, args.fee_slippage_bps + args.extra_slippage_bps + obs.spread_bps)


def select_oil_short_time_gate(
    obs: Obs,
    price_by_symbol: dict[str, dict[int, float]],
    args: argparse.Namespace,
) -> tuple[str, float] | None:
    del price_by_symbol
    if obs.symbol not in {"XYZ:CL", "XYZ:BRENTOIL"}:
        return None
    if obs.regime not in {"chop", "mixed", "high_vol"}:
        return None
    if not (7 <= obs.hour < 10):
        return None
    cost = all_in_cost_bps(obs, args)
    if cost > args.max_all_in_cost_bps:
        return None
    score = max(0.0, 10.0 - obs.spread_bps) + max(0.0, obs.vwap) * 0.10 + max(0.0, obs.vol_short - 6.0) * 0.15
    reason = f"oil short 240m; regime={obs.regime}; hour={obs.hour}; all_in_cost_bps={cost:.2f}"
    return reason, score


def select_crypto_alt_short_weak_basket(
    obs: Obs,
    price_by_symbol: dict[str, dict[int, float]],
    args: argparse.Namespace,
) -> tuple[str, float] | None:
    base = {"PENGU", "TIA", "VVV", "STRK", "ZRO", "ICP"}
    extended = {"SAGA", "DYM"}
    if obs.symbol not in base and obs.symbol not in extended:
        return None
    if obs.regime not in {"mixed", "high_vol", "broad_up", "positive_breadth"}:
        return None
    if obs.spread_bps > args.crypto_max_spread_bps and obs.symbol not in extended:
        return None
    if obs.symbol in extended and obs.spread_bps > args.extended_alt_max_spread_bps:
        return None
    cost = all_in_cost_bps(obs, args)
    if cost > args.max_all_in_cost_bps:
        return None
    rel_60 = relative_crypto_return_bps(price_by_symbol, obs.symbol, obs.ts, 60)
    rel_240 = relative_crypto_return_bps(price_by_symbol, obs.symbol, obs.ts, 240)
    weak_reasons: list[str] = []
    if obs.structure <= -0.10:
        weak_reasons.append("structure_weak")
    if obs.flow <= -0.30:
        weak_reasons.append("flow_sell")
    if rel_60 is not None and rel_60 <= -15.0:
        weak_reasons.append("rel60_weak")
    if rel_240 is not None and rel_240 <= -40.0:
        weak_reasons.append("rel240_weak")
    if obs.vwap >= 5.0 and obs.flow <= 0.0:
        weak_reasons.append("stretched_without_buy_flow")
    if not weak_reasons:
        return None
    score = len(weak_reasons) * 10.0 + max(0.0, -(rel_60 or 0.0)) * 0.10 + max(0.0, obs.vwap) * 0.05 - obs.spread_bps
    reason = (
        f"crypto weak basket short 240m; regime={obs.regime}; reasons={'+'.join(weak_reasons)}; "
        f"rel60_bps={(rel_60 if rel_60 is not None else 0.0):.2f}; all_in_cost_bps={cost:.2f}"
    )
    return reason, score


def select_crypto_high_vol_rebound(
    obs: Obs,
    price_by_symbol: dict[str, dict[int, float]],
    args: argparse.Namespace,
) -> tuple[str, float] | None:
    del price_by_symbol
    if obs.cluster != "crypto":
        return None
    if obs.regime != "high_vol":
        return None
    if obs.spread_bps > args.crypto_max_spread_bps:
        return None
    cost = all_in_cost_bps(obs, args)
    if cost > args.max_all_in_cost_bps:
        return None
    if obs.vwap > -15.0:
        return None
    if not (obs.flow >= 0.0 or obs.micro >= 0.0 or obs.book >= 0.15):
        return None
    score = max(0.0, -obs.vwap) * 0.30 + max(0.0, obs.flow) * 8.0 + max(0.0, obs.micro) * 0.10 - obs.spread_bps
    reason = f"crypto high-vol rebound long 60m; vwap_bps={obs.vwap:.2f}; flow={obs.flow:.2f}; all_in_cost_bps={cost:.2f}"
    return reason, score


def select_gold_short_filter(
    obs: Obs,
    price_by_symbol: dict[str, dict[int, float]],
    args: argparse.Namespace,
) -> tuple[str, float] | None:
    del price_by_symbol
    if obs.symbol != "XYZ:GOLD":
        return None
    if obs.regime not in {"downtrend", "mixed"}:
        return None
    cost = all_in_cost_bps(obs, args)
    if cost > args.max_all_in_cost_bps:
        return None
    if not (obs.structure <= -0.10 or obs.flow <= -0.30 or obs.vwap >= 5.0):
        return None
    score = max(0.0, -obs.structure) * 20.0 + max(0.0, -obs.flow) * 8.0 + max(0.0, obs.vwap) * 0.05 - obs.spread_bps
    reason = f"gold short/filter 240m; regime={obs.regime}; structure={obs.structure:.2f}; all_in_cost_bps={cost:.2f}"
    return reason, score


def default_variants() -> list[FactorVariant]:
    return [
        FactorVariant(
            name="oil_short_4h_time_gate",
            description="XYZ:CL/BRENTOIL short 240m, regimes chop/mixed/high_vol, entries 07:00-10:00 UTC.",
            side="short",
            horizon_min=240,
            cluster_cap_key="oil",
            cooldown_min=240,
            selector=select_oil_short_time_gate,
        ),
        FactorVariant(
            name="crypto_alt_short_4h_weak_basket",
            description="Short 240m weak alts PENGU/TIA/VVV/STRK/ZRO/ICP, plus SAGA/DYM only when costs pass the gate.",
            side="short",
            horizon_min=240,
            cluster_cap_key="crypto",
            cooldown_min=240,
            selector=select_crypto_alt_short_weak_basket,
        ),
        FactorVariant(
            name="crypto_high_vol_rebound_60m",
            description="Long 60m high-vol rebound, no long grace assumption, fixed fast exit.",
            side="long",
            horizon_min=60,
            cluster_cap_key="crypto",
            cooldown_min=60,
            selector=select_crypto_high_vol_rebound,
        ),
        FactorVariant(
            name="gold_short_filter_4h",
            description="XYZ:GOLD short/filter 240m; first use as anti-long filter candidate, not direct live short.",
            side="short",
            horizon_min=240,
            cluster_cap_key="gold",
            cooldown_min=240,
            selector=select_gold_short_filter,
        ),
    ]


def make_factor_trade(
    *,
    variant: FactorVariant,
    obs: Obs,
    future_price: float,
    reason: str,
    score: float,
    args: argparse.Namespace,
) -> FactorTrade:
    raw_ret_bps = (future_price / obs.price - 1.0) * 10000.0
    gross_bps = raw_ret_bps if variant.side == "long" else -raw_ret_bps
    fee_slippage = args.fee_slippage_bps + args.extra_slippage_bps
    cost_bps = fee_slippage + obs.spread_bps
    net_bps = gross_bps - cost_bps
    notional = args.notional_usd
    gross_pnl = gross_bps / 10000.0 * notional
    cost_usd = cost_bps / 10000.0 * notional
    net_pnl = net_bps / 10000.0 * notional
    return FactorTrade(
        variant=variant.name,
        timestamp=iso_from_ts(obs.ts),
        ts=obs.ts,
        exit_timestamp=iso_from_ts(obs.ts + variant.horizon_min * 60),
        symbol=obs.symbol,
        cluster=obs.cluster,
        side=variant.side,
        horizon_min=variant.horizon_min,
        regime=obs.regime,
        hour_utc=obs.hour,
        dow_utc=obs.dow,
        month=month_label(obs.ts),
        entry_price=obs.price,
        exit_price=future_price,
        gross_return_bps=gross_bps,
        spread_bps=obs.spread_bps,
        fee_slippage_bps=fee_slippage,
        all_in_cost_bps=cost_bps,
        net_return_bps=net_bps,
        notional_usd=notional,
        gross_pnl_usd=gross_pnl,
        cost_usd=cost_usd,
        net_pnl_usd=net_pnl,
        score=score,
        reason=reason,
    )


def build_raw_factor_trades(
    obs_by_key: dict[tuple[str, int], Obs],
    price_by_symbol: dict[str, dict[int, float]],
    variants: list[FactorVariant],
    args: argparse.Namespace,
) -> dict[str, list[FactorTrade]]:
    by_variant: dict[str, list[FactorTrade]] = {variant.name: [] for variant in variants}
    ordered_obs = sorted(obs_by_key.values(), key=lambda obs: (obs.ts, obs.symbol))
    for obs in ordered_obs:
        prices = price_by_symbol.get(obs.symbol, {})
        for variant in variants:
            selection = variant.selector(obs, price_by_symbol, args)
            if selection is None:
                continue
            future = prices.get(obs.ts + variant.horizon_min * 60)
            if not future or future <= 0:
                continue
            reason, score = selection
            by_variant[variant.name].append(
                make_factor_trade(variant=variant, obs=obs, future_price=future, reason=reason, score=score, args=args)
            )
    return by_variant


def apply_portfolio_constraints(
    trades: list[FactorTrade],
    *,
    variant: FactorVariant,
    max_correlated_positions: int,
) -> tuple[list[FactorTrade], ConstraintResult]:
    if max_correlated_positions < 1:
        raise ValueError("max_correlated_positions must be >= 1")
    ordered = sorted(trades, key=lambda row: (row.ts, -row.score, row.symbol))
    kept: list[FactorTrade] = []
    next_allowed_by_symbol: dict[str, int] = {}
    active_until_by_cluster: dict[str, list[int]] = defaultdict(list)
    dropped_cooldown = 0
    dropped_correlation = 0
    for trade in ordered:
        if trade.ts < next_allowed_by_symbol.get(trade.symbol, 0):
            dropped_cooldown += 1
            continue
        cap_key = variant.cluster_cap_key or trade.cluster
        active = [end_ts for end_ts in active_until_by_cluster[cap_key] if end_ts > trade.ts]
        active_until_by_cluster[cap_key] = active
        if len(active) >= max_correlated_positions:
            dropped_correlation += 1
            continue
        kept.append(trade)
        next_allowed_by_symbol[trade.symbol] = trade.ts + variant.cooldown_min * 60
        active_until_by_cluster[cap_key].append(trade.ts + trade.horizon_min * 60)
    return kept, ConstraintResult(
        variant=variant.name,
        raw_count=len(trades),
        kept_count=len(kept),
        dropped_symbol_cooldown=dropped_cooldown,
        dropped_correlation_cap=dropped_correlation,
    )


def profit_factor(values: list[float]) -> float | None:
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    if losses <= 0:
        return None
    return gains / losses


def max_drawdown(values: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def max_exposure(rows: list[FactorTrade]) -> tuple[int, float]:
    events: list[tuple[int, int, float]] = []
    for row in rows:
        events.append((row.ts, 1, row.notional_usd))
        events.append((row.ts + row.horizon_min * 60, -1, -row.notional_usd))
    open_count = 0
    exposure = 0.0
    max_count = 0
    max_usd = 0.0
    for _ts, delta_count, delta_usd in sorted(events, key=lambda item: (item[0], item[1])):
        open_count += delta_count
        exposure += delta_usd
        max_count = max(max_count, open_count)
        max_usd = max(max_usd, exposure)
    return max_count, max_usd


def group_summary(rows: list[FactorTrade], key_fn: Callable[[FactorTrade], str]) -> list[dict[str, Any]]:
    grouped: dict[str, list[FactorTrade]] = defaultdict(list)
    for row in rows:
        grouped[key_fn(row)].append(row)
    out = []
    for key, items in grouped.items():
        pnls = [item.net_pnl_usd for item in items]
        out.append(
            {
                "key": key,
                "trade_count": len(items),
                "net_pnl_usd": round(sum(pnls), 4),
                "avg_net_return_bps": round(statistics.mean(item.net_return_bps for item in items), 4),
                "win_rate": round(sum(1 for pnl in pnls if pnl > 0) / len(pnls), 4) if pnls else 0.0,
                "profit_factor": round(pf, 4) if (pf := profit_factor(pnls)) is not None else None,
            }
        )
    out.sort(key=lambda item: (item["net_pnl_usd"], item["trade_count"]), reverse=True)
    return out


def factor_metrics(rows: list[FactorTrade], *, constraints: ConstraintResult | None = None) -> dict[str, Any]:
    pnls = [row.net_pnl_usd for row in rows]
    gross_pnls = [row.gross_pnl_usd for row in rows]
    max_open, max_usd = max_exposure(rows)
    symbol_counts = Counter(row.symbol for row in rows)
    symbol_pnl = defaultdict(float)
    for row in rows:
        symbol_pnl[row.symbol] += row.net_pnl_usd
    trade_count = len(rows)
    positive_months = sum(1 for item in group_summary(rows, lambda row: row.month) if item["net_pnl_usd"] > 0)
    negative_months = sum(1 for item in group_summary(rows, lambda row: row.month) if item["net_pnl_usd"] < 0)
    max_symbol_trade_concentration = max(symbol_counts.values(), default=0) / trade_count if trade_count else 0.0
    max_symbol_pnl_concentration = (
        max((abs(value) for value in symbol_pnl.values()), default=0.0) / abs(sum(pnls))
        if abs(sum(pnls)) > 1e-9
        else 0.0
    )
    metrics = {
        "trade_count": trade_count,
        "gross_pnl_usd": round(sum(gross_pnls), 4),
        "net_pnl_usd": round(sum(pnls), 4),
        "cost_usd": round(sum(row.cost_usd for row in rows), 4),
        "avg_gross_return_bps": round(statistics.mean(row.gross_return_bps for row in rows), 4) if rows else 0.0,
        "avg_net_return_bps": round(statistics.mean(row.net_return_bps for row in rows), 4) if rows else 0.0,
        "median_net_return_bps": round(statistics.median(row.net_return_bps for row in rows), 4) if rows else 0.0,
        "avg_spread_bps": round(statistics.mean(row.spread_bps for row in rows), 4) if rows else 0.0,
        "avg_all_in_cost_bps": round(statistics.mean(row.all_in_cost_bps for row in rows), 4) if rows else 0.0,
        "win_rate": round(sum(1 for pnl in pnls if pnl > 0) / trade_count, 4) if trade_count else 0.0,
        "profit_factor": round(pf, 4) if (pf := profit_factor(pnls)) is not None else None,
        "max_drawdown_usd": round(max_drawdown(pnls), 4),
        "max_open_positions": max_open,
        "max_exposure_usd": round(max_usd, 2),
        "positive_months": positive_months,
        "negative_months": negative_months,
        "max_symbol_trade_concentration": round(max_symbol_trade_concentration, 4),
        "max_symbol_pnl_concentration": round(max_symbol_pnl_concentration, 4),
        "by_month": group_summary(rows, lambda row: row.month),
        "by_regime": group_summary(rows, lambda row: row.regime),
        "by_symbol": group_summary(rows, lambda row: row.symbol),
        "by_hour_utc": group_summary(rows, lambda row: str(row.hour_utc)),
        "by_dow_utc": group_summary(rows, lambda row: str(row.dow_utc)),
    }
    if constraints is not None:
        metrics["constraints"] = asdict(constraints)
    metrics["classification"] = classify_factor_metrics(metrics)
    return metrics


def classify_factor_metrics(metrics: dict[str, Any]) -> dict[str, str]:
    trade_count = int(metrics.get("trade_count") or 0)
    net_pnl = float(metrics.get("net_pnl_usd") or 0.0)
    pf = metrics.get("profit_factor")
    profit_factor_value = float(pf) if pf is not None else (99.0 if net_pnl > 0 else 0.0)
    positive_months = int(metrics.get("positive_months") or 0)
    max_dd = float(metrics.get("max_drawdown_usd") or 0.0)
    symbol_trade_conc = float(metrics.get("max_symbol_trade_concentration") or 0.0)
    if trade_count == 0:
        return {"status": "rejetee", "reason": "aucun candidat apres les gates et horizons disponibles"}
    if net_pnl <= 0:
        return {"status": "rejetee", "reason": "PnL net negatif apres couts"}
    if trade_count < 20:
        return {"status": "research_only", "reason": "PnL positif mais sample trop faible"}
    if positive_months < 2:
        return {"status": "research_only", "reason": "PnL positif mais pas confirme sur au moins deux mois"}
    if symbol_trade_conc > 0.60:
        return {"status": "research_only", "reason": "PnL positif mais concentration symbole trop forte"}
    if profit_factor_value >= 1.15 and trade_count >= 30 and max_dd <= max(25.0, abs(net_pnl) * 0.90):
        return {
            "status": "promouvable_shadow",
            "reason": "criteres research passes; shadow uniquement apres replay full-bot A/C integre",
        }
    return {"status": "research_only", "reason": "edge positif mais PF/drawdown/sample insuffisants pour shadow direct"}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8", errors="ignore") as fh:
        return list(csv.DictReader(fh))


def load_hip4_settlements(hip4_dir: Path) -> list[Hip4Settlement]:
    trades = read_csv(hip4_dir / "trades.csv")
    settlements = read_csv(hip4_dir / "settlements.csv")
    trade_meta: dict[tuple[str, str], dict[str, Any]] = {}
    for row in trades:
        key = (row.get("market_id", ""), row.get("side", ""))
        if key in trade_meta:
            continue
        open_ts = parse_ts(row.get("ts"))
        market_id = row.get("market_id", "")
        expiry = parse_market_expiry(market_id)
        trade_meta[key] = {
            "open_ts": open_ts,
            "edge_type": row.get("edge_type", ""),
            "open_seconds_to_expiry": (expiry - open_ts).total_seconds() if expiry and open_ts else None,
        }
    out = []
    for row in settlements:
        key = (row.get("market_id", ""), row.get("side", ""))
        meta = trade_meta.get(key, {})
        out.append(
            Hip4Settlement(
                ts=parse_ts(row.get("ts")),
                open_ts=meta.get("open_ts"),
                market_id=row.get("market_id", ""),
                underlying=row.get("underlying", ""),
                side=row.get("side", ""),
                edge_type=str(meta.get("edge_type") or ""),
                result=row.get("result", ""),
                pnl_usdc=f(row.get("pnl_usdc") or row.get("net_pnl_usdc")),
                fee_usdc=f(row.get("fee_usdc")),
                is_win=str(row.get("is_win", "")).lower() == "true",
                open_seconds_to_expiry=meta.get("open_seconds_to_expiry"),
            )
        )
    return out


def load_hip4_quality(quality_file: Path) -> dict[str, dict[str, Any]]:
    rows = read_csv(quality_file)
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        market_id = row.get("market_id") or ""
        if market_id:
            grouped[market_id].append(row)
    out: dict[str, dict[str, Any]] = {}
    for market_id, items in grouped.items():
        quality_scores = [f(row.get("quality_score")) for row in items]
        book_ages = [f(row.get("max_book_age_ms")) for row in items]
        divergences = [abs(f(row.get("reference_divergence_bps"))) for row in items]
        reasons = Counter()
        for row in items:
            for reason in (row.get("quality_reasons") or "").split(";"):
                if reason:
                    reasons[reason] += 1
        out[market_id] = {
            "rows": len(items),
            "avg_quality_score": statistics.mean(quality_scores) if quality_scores else 0.0,
            "avg_max_book_age_ms": statistics.mean(book_ages) if book_ages else 0.0,
            "avg_reference_divergence_bps": statistics.mean(divergences) if divergences else 0.0,
            "top_quality_reasons": dict(reasons.most_common(5)),
        }
    return out


def hip4_quality_ok(market_id: str, quality: dict[str, dict[str, Any]], args: argparse.Namespace) -> bool:
    q = quality.get(market_id)
    if not q:
        return False
    return (
        q["avg_quality_score"] >= args.hip4_quality_threshold
        and q["avg_max_book_age_ms"] <= args.hip4_max_book_age_ms
        and q["avg_reference_divergence_bps"] <= args.hip4_max_reference_divergence_bps
    )


def open_to_expiry_bucket(row: Hip4Settlement) -> str:
    seconds = row.open_seconds_to_expiry
    if seconds is None:
        return "unknown"
    if seconds <= 3600:
        return "<=1h"
    if seconds <= 6 * 3600:
        return "1-6h"
    if seconds <= 18 * 3600:
        return "6-18h"
    return ">18h"


def hip4_policy_rows(
    settlements: list[Hip4Settlement],
    quality: dict[str, dict[str, Any]],
    args: argparse.Namespace,
) -> dict[str, list[Hip4Settlement]]:
    return {
        "hip4_current_all": settlements,
        "hip4_buy_yes_only": [row for row in settlements if row.side == "BUY_YES"],
        "hip4_skip_buy_no": [row for row in settlements if row.side != "BUY_NO"],
        "hip4_skip_buy_no_6_18h": [
            row for row in settlements if not (row.side == "BUY_NO" and open_to_expiry_bucket(row) == "6-18h")
        ],
        "hip4_data_quality_gate": [row for row in settlements if hip4_quality_ok(row.market_id, quality, args)],
        "hip4_buy_yes_quality_gate": [
            row for row in settlements if row.side == "BUY_YES" and hip4_quality_ok(row.market_id, quality, args)
        ],
    }


def hip4_group_summary(rows: list[Hip4Settlement], key_fn: Callable[[Hip4Settlement], str]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Hip4Settlement]] = defaultdict(list)
    for row in rows:
        grouped[key_fn(row)].append(row)
    out = []
    for key, items in grouped.items():
        pnls = [item.pnl_usdc for item in items]
        out.append(
            {
                "key": key,
                "count": len(items),
                "net_pnl_usdc": round(sum(pnls), 4),
                "win_rate": round(sum(1 for pnl in pnls if pnl > 0) / len(pnls), 4) if pnls else 0.0,
                "profit_factor": round(pf, 4) if (pf := profit_factor(pnls)) is not None else None,
            }
        )
    out.sort(key=lambda item: (item["net_pnl_usdc"], item["count"]), reverse=True)
    return out


def hip4_metrics(rows: list[Hip4Settlement], baseline_count: int) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: row.ts or datetime.min.replace(tzinfo=timezone.utc))
    pnls = [row.pnl_usdc for row in ordered]
    by_month = hip4_group_summary(ordered, lambda row: (row.ts or datetime.min.replace(tzinfo=timezone.utc)).strftime("%Y-%m"))
    metrics = {
        "settlement_count": len(ordered),
        "skipped_count": baseline_count - len(ordered),
        "net_pnl_usdc": round(sum(pnls), 4),
        "sum_gains_usdc": round(sum(pnl for pnl in pnls if pnl > 0), 4),
        "sum_losses_usdc": round(sum(pnl for pnl in pnls if pnl < 0), 4),
        "win_rate": round(sum(1 for pnl in pnls if pnl > 0) / len(pnls), 4) if pnls else 0.0,
        "profit_factor": round(pf, 4) if (pf := profit_factor(pnls)) is not None else None,
        "max_drawdown_usdc": round(max_drawdown(pnls), 4),
        "positive_months": sum(1 for item in by_month if item["net_pnl_usdc"] > 0),
        "negative_months": sum(1 for item in by_month if item["net_pnl_usdc"] < 0),
        "by_month": by_month,
        "by_side": hip4_group_summary(ordered, lambda row: row.side),
        "by_underlying": hip4_group_summary(ordered, lambda row: row.underlying),
        "by_open_to_expiry": hip4_group_summary(ordered, open_to_expiry_bucket),
        "by_hour_utc": hip4_group_summary(ordered, lambda row: str((row.open_ts or row.ts or datetime.min.replace(tzinfo=timezone.utc)).hour)),
    }
    metrics["classification"] = classify_hip4_metrics(metrics)
    return metrics


def classify_hip4_metrics(metrics: dict[str, Any]) -> dict[str, str]:
    count = int(metrics.get("settlement_count") or 0)
    net = float(metrics.get("net_pnl_usdc") or 0.0)
    pf = metrics.get("profit_factor")
    pf_value = float(pf) if pf is not None else (99.0 if net > 0 else 0.0)
    positive_months = int(metrics.get("positive_months") or 0)
    if count == 0:
        return {"status": "rejetee", "reason": "aucun settlement conserve par la policy"}
    if net <= 0:
        return {"status": "rejetee", "reason": "PnL net negatif en mainnet paper"}
    if count < 20:
        return {"status": "research_only", "reason": "PnL positif mais sample HIP-4 trop faible"}
    if positive_months < 2:
        return {"status": "research_only", "reason": "PnL positif mais pas confirme sur deux sous-periodes"}
    if pf_value >= 1.15:
        return {"status": "promouvable_shadow", "reason": "policy positive en paper; dry-run/mainnet paper prolonge requis"}
    return {"status": "research_only", "reason": "PnL positif mais profit factor insuffisant"}


def analyze_executed_trades(pod_a_log: Path, pod_c_log: Path) -> dict[str, Any]:
    rows = []
    for pod, path in (("pod_a", pod_a_log), ("pod_c", pod_c_log)):
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if not line.strip() or '"event_type": "trade_close"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                trade = event.get("trade") or {}
                symbol = str(trade.get("symbol") or "")
                notional = abs(f(trade.get("target_notional_usd")))
                if not symbol or notional <= 0:
                    continue
                pnl = f(trade.get("pnl_usd"))
                rows.append(
                    {
                        "pod": pod,
                        "symbol": symbol,
                        "cluster": infer_cluster(symbol, (trade.get("setup_details") or {}).get("market_cluster")),
                        "side": trade.get("side", ""),
                        "pnl_usd": pnl,
                        "pnl_bps_notional": pnl / notional * 10000.0,
                        "close_reason": trade.get("close_reason", ""),
                        "setup": trade.get("setup") or trade.get("open_reason") or "",
                    }
                )
    by_pod = defaultdict(float)
    by_cluster = defaultdict(float)
    for row in rows:
        by_pod[row["pod"]] += row["pnl_usd"]
        by_cluster[row["cluster"]] += row["pnl_usd"]
    pnls = [row["pnl_usd"] for row in rows]
    return {
        "trade_count": len(rows),
        "total_pnl_usd": round(sum(pnls), 4),
        "profit_factor": round(pf, 4) if (pf := profit_factor(pnls)) is not None else None,
        "win_rate": round(sum(1 for pnl in pnls if pnl > 0) / len(pnls), 4) if pnls else 0.0,
        "pnl_by_pod": {key: round(value, 4) for key, value in by_pod.items()},
        "pnl_by_cluster": {key: round(value, 4) for key, value in by_cluster.items()},
    }


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    if fieldnames is None:
        keys = []
        seen = set()
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    keys.append(key)
        fieldnames = keys
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def write_report(path: Path, payload: dict[str, Any]) -> None:
    lines = []
    lines.append("# P1-09 - Replay recherche factorielle")
    lines.append("")
    lines.append(f"- Genere le: `{payload['generated_at']}`")
    lines.append(f"- Statut: `{payload['status']}`")
    lines.append(f"- Donnees snapshots: `{payload['snapshot_coverage'].get('first_ts')}` -> `{payload['snapshot_coverage'].get('last_ts')}`")
    lines.append(
        f"- Cout forward: fee/slippage `{payload['parameters']['fee_slippage_bps']}` bps + spread snapshot, "
        f"notionnel `{payload['parameters']['notional_usd']}` USD"
    )
    lines.append("")
    lines.append("## Synthese variantes")
    lines.append("")
    lines.append("| Variante | Statut | Trades | Net | PF | WR | DD | Max expo | Commentaire |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---|")
    for name, metrics in payload["factor_variants"].items():
        cls = metrics["classification"]
        lines.append(
            f"| `{name}` | `{cls['status']}` | {metrics['trade_count']} | "
            f"{metrics['net_pnl_usd']:.2f} USD | {format_optional(metrics['profit_factor'])} | "
            f"{metrics['win_rate']:.1%} | {metrics['max_drawdown_usd']:.2f} | "
            f"{metrics['max_exposure_usd']:.0f} | {cls['reason']} |"
        )
    lines.append("")
    lines.append("## HIP-4 side/data guard")
    lines.append("")
    lines.append("| Policy | Statut | Settlements | Skipped | Net | PF | WR | DD | Commentaire |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---|")
    for name, metrics in payload["hip4_policies"].items():
        cls = metrics["classification"]
        lines.append(
            f"| `{name}` | `{cls['status']}` | {metrics['settlement_count']} | {metrics['skipped_count']} | "
            f"{metrics['net_pnl_usdc']:.2f} USDC | {format_optional(metrics['profit_factor'])} | "
            f"{metrics['win_rate']:.1%} | {metrics['max_drawdown_usdc']:.2f} | {cls['reason']} |"
        )
    lines.append("")
    lines.append("## Details par variante")
    for name, metrics in payload["factor_variants"].items():
        lines.append("")
        lines.append(f"### {name}")
        lines.append("")
        constraints = metrics.get("constraints") or {}
        lines.append(
            f"- Candidats raw `{constraints.get('raw_count', 0)}`, gardes `{constraints.get('kept_count', 0)}`, "
            f"dropped cooldown `{constraints.get('dropped_symbol_cooldown', 0)}`, "
            f"dropped correlation `{constraints.get('dropped_correlation_cap', 0)}`."
        )
        lines.append(
            f"- Moyenne nette `{metrics['avg_net_return_bps']:.2f}` bps, cout moyen "
            f"`{metrics['avg_all_in_cost_bps']:.2f}` bps, spread moyen `{metrics['avg_spread_bps']:.2f}` bps."
        )
        lines.append("- Mois:")
        for row in metrics["by_month"][:8]:
            lines.append(
                f"  - `{row['key']}`: net `{row['net_pnl_usd']:.2f}`, trades `{row['trade_count']}`, "
                f"WR `{row['win_rate']:.1%}`, PF `{format_optional(row['profit_factor'])}`"
            )
        lines.append("- Top symboles:")
        for row in metrics["by_symbol"][:8]:
            lines.append(
                f"  - `{row['key']}`: net `{row['net_pnl_usd']:.2f}`, trades `{row['trade_count']}`, "
                f"WR `{row['win_rate']:.1%}`"
            )
    lines.append("")
    lines.append("## Baseline executee A/C")
    executed = payload["executed_trade_audit"]
    lines.append(
        f"- Journaux live fermes: `{executed['trade_count']}` trades, PnL total `{executed['total_pnl_usd']:.2f}` USD, "
        f"WR `{executed['win_rate']:.1%}`, PF `{format_optional(executed['profit_factor'])}`."
    )
    lines.append(f"- PnL par pod: `{executed['pnl_by_pod']}`.")
    lines.append("")
    lines.append("## Garde-fous")
    lines.append("")
    lines.append("- Ces resultats sont des forwards a horizon fixe depuis snapshots 5m, pas un replay full-bot A/C avec state d'execution.")
    lines.append("- Aucune variante P1-09 ne doit passer live directement; les candidates positives passent d'abord par replay full-bot puis shadow.")
    lines.append("- Les horaires/jours restent des variables de recherche tant qu'une validation hors echantillon n'est pas faite.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def format_optional(value: Any) -> str:
    if value is None:
        return "inf/na"
    return f"{float(value):.2f}"


def main() -> None:
    args = parse_args()
    generated_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(args.output_dir or ROOT / "server-data" / "replay_reports" / f"p109_factor_research_{generated_at}")
    output_dir.mkdir(parents=True, exist_ok=True)
    variants = default_variants()

    obs_by_key, price_by_symbol, snapshot_coverage = load_snapshots(Path(args.snapshot_dir), quiet=args.quiet)
    raw_by_variant = build_raw_factor_trades(obs_by_key, price_by_symbol, variants, args)
    factor_payload: dict[str, Any] = {}
    all_kept_rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    for variant in variants:
        kept, constraints = apply_portfolio_constraints(
            raw_by_variant[variant.name],
            variant=variant,
            max_correlated_positions=args.max_correlated_positions,
        )
        metrics = factor_metrics(kept, constraints=constraints)
        metrics["description"] = variant.description
        factor_payload[variant.name] = metrics
        for row in kept:
            all_kept_rows.append(asdict(row))
        summary_rows.append(
            {
                "variant": variant.name,
                "status": metrics["classification"]["status"],
                "reason": metrics["classification"]["reason"],
                "raw_count": constraints.raw_count,
                "trade_count": metrics["trade_count"],
                "net_pnl_usd": metrics["net_pnl_usd"],
                "profit_factor": metrics["profit_factor"],
                "win_rate": metrics["win_rate"],
                "max_drawdown_usd": metrics["max_drawdown_usd"],
                "max_exposure_usd": metrics["max_exposure_usd"],
                "avg_all_in_cost_bps": metrics["avg_all_in_cost_bps"],
            }
        )
        if not args.quiet:
            print(
                f"variant={variant.name} status={metrics['classification']['status']} "
                f"trades={metrics['trade_count']} net={metrics['net_pnl_usd']:.2f} "
                f"pf={format_optional(metrics['profit_factor'])}",
                flush=True,
            )

    hip4_dir = Path(args.hip4_paper_dir)
    hip4_settlements = load_hip4_settlements(hip4_dir)
    hip4_quality = load_hip4_quality(Path(args.hip4_quality_file))
    hip4_payload = {
        name: hip4_metrics(rows, baseline_count=len(hip4_settlements))
        for name, rows in hip4_policy_rows(hip4_settlements, hip4_quality, args).items()
    }
    hip4_summary_rows = [
        {
            "policy": name,
            "status": metrics["classification"]["status"],
            "reason": metrics["classification"]["reason"],
            "settlement_count": metrics["settlement_count"],
            "skipped_count": metrics["skipped_count"],
            "net_pnl_usdc": metrics["net_pnl_usdc"],
            "profit_factor": metrics["profit_factor"],
            "win_rate": metrics["win_rate"],
            "max_drawdown_usdc": metrics["max_drawdown_usdc"],
        }
        for name, metrics in hip4_payload.items()
    ]

    payload = {
        "generated_at": generated_at,
        "status": "research_only_no_live_change",
        "parameters": {
            "snapshot_dir": str(Path(args.snapshot_dir)),
            "pod_a_log": str(Path(args.pod_a_log)),
            "pod_c_log": str(Path(args.pod_c_log)),
            "hip4_paper_dir": str(hip4_dir),
            "hip4_quality_file": str(Path(args.hip4_quality_file)),
            "notional_usd": args.notional_usd,
            "fee_slippage_bps": args.fee_slippage_bps,
            "extra_slippage_bps": args.extra_slippage_bps,
            "max_all_in_cost_bps": args.max_all_in_cost_bps,
            "max_correlated_positions": args.max_correlated_positions,
        },
        "snapshot_coverage": snapshot_coverage,
        "factor_variants": factor_payload,
        "hip4_policies": hip4_payload,
        "hip4_quality_summary": {
            "market_count": len(hip4_quality),
            "quality_file": str(Path(args.hip4_quality_file)),
            "thresholds": {
                "quality_score": args.hip4_quality_threshold,
                "max_book_age_ms": args.hip4_max_book_age_ms,
                "max_reference_divergence_bps": args.hip4_max_reference_divergence_bps,
            },
        },
        "executed_trade_audit": analyze_executed_trades(Path(args.pod_a_log), Path(args.pod_c_log)),
    }

    (output_dir / "p109_factor_research_replay.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_report(output_dir / "p109_factor_research_replay.md", payload)
    write_csv(output_dir / "factor_variant_summary.csv", summary_rows)
    write_csv(output_dir / "factor_trades.csv", all_kept_rows)
    write_csv(output_dir / "hip4_policy_summary.csv", hip4_summary_rows)
    print(output_dir)


if __name__ == "__main__":
    main()
