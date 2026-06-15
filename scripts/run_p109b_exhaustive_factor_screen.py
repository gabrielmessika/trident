#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.run_p109_factor_research_replay import (
    DEFAULT_SNAPSHOT_DIR,
    ROOT,
    Obs,
    all_in_cost_bps,
    load_snapshots,
    month_label,
)


DEFAULT_HORIZONS = "5,15,30,60,120,240,480"


@dataclass(slots=True)
class TechSnapshot:
    rsi14: float | None = None
    sma20_distance_bps: float | None = None
    sma50_distance_bps: float | None = None
    ema20_distance_bps: float | None = None
    macd_hist_bps: float | None = None
    macd_cross: str | None = None
    bollinger_z20: float | None = None
    bollinger_width_bps: float | None = None
    stoch14: float | None = None
    donchian20: str | None = None
    ret15_bps: float | None = None
    ret60_bps: float | None = None
    ret240_bps: float | None = None
    rel60_bps: float | None = None
    rel240_bps: float | None = None


@dataclass(slots=True)
class EdgeStat:
    n: int = 0
    wins: int = 0
    sum_net_bps: float = 0.0
    sum_gross_bps: float = 0.0
    sum_cost_bps: float = 0.0
    sumsq_net_bps: float = 0.0
    gains_bps: float = 0.0
    losses_bps: float = 0.0

    def add(self, gross_bps: float, cost_bps: float) -> None:
        net_bps = gross_bps - cost_bps
        self.n += 1
        self.wins += 1 if net_bps > 0 else 0
        self.sum_net_bps += net_bps
        self.sum_gross_bps += gross_bps
        self.sum_cost_bps += cost_bps
        self.sumsq_net_bps += net_bps * net_bps
        if net_bps > 0:
            self.gains_bps += net_bps
        elif net_bps < 0:
            self.losses_bps += -net_bps

    def to_dict(self) -> dict[str, Any]:
        mean = self.sum_net_bps / self.n if self.n else 0.0
        variance = max(0.0, self.sumsq_net_bps / self.n - mean * mean) if self.n else 0.0
        sd = math.sqrt(variance)
        stderr = sd / math.sqrt(self.n) if self.n > 1 else 0.0
        pf = self.gains_bps / self.losses_bps if self.losses_bps > 0 else None
        return {
            "n": self.n,
            "mean_net_bps": mean,
            "mean_gross_bps": self.sum_gross_bps / self.n if self.n else 0.0,
            "mean_cost_bps": self.sum_cost_bps / self.n if self.n else 0.0,
            "win_rate": self.wins / self.n if self.n else 0.0,
            "profit_factor": pf,
            "sd_net_bps": sd,
            "t_like": mean / stderr if stderr > 0 else 0.0,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="P1-09b exhaustive factor screener over local snapshots.")
    parser.add_argument("--snapshot-dir", default=str(DEFAULT_SNAPSHOT_DIR))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--horizons-min", default=DEFAULT_HORIZONS)
    parser.add_argument("--fee-slippage-bps", type=float, default=16.0)
    parser.add_argument("--extra-slippage-bps", type=float, default=0.0)
    parser.add_argument("--notional-usd", type=float, default=200.0)
    parser.add_argument("--min-n", type=int, default=120)
    parser.add_argument("--min-mean-net-bps", type=float, default=2.0)
    parser.add_argument("--top-limit", type=int, default=300)
    parser.add_argument("--include-neutral-indicator-buckets", action="store_true")
    parser.add_argument("--include-symbol-indicators", action="store_true")
    parser.add_argument("--include-symbol-regime-hour", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def parse_horizons(value: str) -> list[int]:
    horizons = sorted({int(part.strip()) for part in value.split(",") if part.strip()})
    if any(h <= 0 or h % 5 != 0 for h in horizons):
        raise ValueError("horizons must be positive multiples of 5 minutes")
    return horizons


def rolling_mean(values: deque[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def rolling_sd(values: deque[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = rolling_mean(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / len(values))


def ema(prev: float | None, value: float, length: int) -> float:
    alpha = 2.0 / (length + 1.0)
    return value if prev is None else prev + alpha * (value - prev)


def build_tech_index(price_by_symbol: dict[str, dict[int, float]]) -> dict[tuple[str, int], TechSnapshot]:
    tech_by_key: dict[tuple[str, int], TechSnapshot] = {}
    leader_returns: dict[tuple[str, int, int], float] = {}
    for symbol in ("BTC", "ETH"):
        prices = price_by_symbol.get(symbol, {})
        for ts, price in prices.items():
            for lookback in (60, 240):
                prev = prices.get(ts - lookback * 60)
                if prev and prev > 0:
                    leader_returns[(symbol, ts, lookback)] = (price / prev - 1.0) * 10000.0

    for symbol, prices in price_by_symbol.items():
        items = sorted(prices.items())
        close20: deque[float] = deque(maxlen=20)
        close50: deque[float] = deque(maxlen=50)
        close14: deque[float] = deque(maxlen=14)
        gains14: deque[float] = deque(maxlen=14)
        losses14: deque[float] = deque(maxlen=14)
        ema12 = ema26 = signal9 = ema20 = None
        prev_macd = prev_signal = None
        prev_price = None
        for ts, price in items:
            if prev_price is not None:
                delta = price - prev_price
                gains14.append(max(0.0, delta))
                losses14.append(max(0.0, -delta))
            close20.append(price)
            close50.append(price)
            close14.append(price)
            ema12 = ema(ema12, price, 12)
            ema26 = ema(ema26, price, 26)
            ema20 = ema(ema20, price, 20)
            macd = (ema12 - ema26) if ema12 is not None and ema26 is not None else None
            signal9 = ema(signal9, macd, 9) if macd is not None else None
            macd_cross = None
            if macd is not None and signal9 is not None and prev_macd is not None and prev_signal is not None:
                if prev_macd <= prev_signal and macd > signal9:
                    macd_cross = "cross_up"
                elif prev_macd >= prev_signal and macd < signal9:
                    macd_cross = "cross_down"
                elif macd > signal9:
                    macd_cross = "bull"
                elif macd < signal9:
                    macd_cross = "bear"
            sma20 = rolling_mean(close20) if len(close20) >= 20 else None
            sma50 = rolling_mean(close50) if len(close50) >= 50 else None
            sd20 = rolling_sd(close20) if len(close20) >= 20 else None
            high14 = max(close14) if len(close14) >= 14 else None
            low14 = min(close14) if len(close14) >= 14 else None
            high20 = max(close20) if len(close20) >= 20 else None
            low20 = min(close20) if len(close20) >= 20 else None
            avg_gain = rolling_mean(gains14) if len(gains14) >= 14 else None
            avg_loss = rolling_mean(losses14) if len(losses14) >= 14 else None
            rsi = None
            if avg_gain is not None and avg_loss is not None:
                rsi = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
            ret15 = trailing_return(prices, ts, price, 15)
            ret60 = trailing_return(prices, ts, price, 60)
            ret240 = trailing_return(prices, ts, price, 240)
            rel60 = relative_return(symbol, ts, ret60, leader_returns, 60)
            rel240 = relative_return(symbol, ts, ret240, leader_returns, 240)
            tech_by_key[(symbol, ts)] = TechSnapshot(
                rsi14=rsi,
                sma20_distance_bps=(price / sma20 - 1.0) * 10000.0 if sma20 and sma20 > 0 else None,
                sma50_distance_bps=(price / sma50 - 1.0) * 10000.0 if sma50 and sma50 > 0 else None,
                ema20_distance_bps=(price / ema20 - 1.0) * 10000.0 if ema20 and ema20 > 0 else None,
                macd_hist_bps=(macd - signal9) / price * 10000.0 if macd is not None and signal9 is not None and price > 0 else None,
                macd_cross=macd_cross,
                bollinger_z20=(price - sma20) / (2.0 * sd20) if sma20 and sd20 and sd20 > 0 else None,
                bollinger_width_bps=(4.0 * sd20 / sma20 * 10000.0) if sma20 and sd20 and sma20 > 0 else None,
                stoch14=(price - low14) / (high14 - low14) if high14 and low14 is not None and high14 > low14 else None,
                donchian20=donchian_bucket(price, high20, low20),
                ret15_bps=ret15,
                ret60_bps=ret60,
                ret240_bps=ret240,
                rel60_bps=rel60,
                rel240_bps=rel240,
            )
            prev_price = price
            prev_macd = macd
            prev_signal = signal9
    return tech_by_key


def trailing_return(prices: dict[int, float], ts: int, price: float, lookback_min: int) -> float | None:
    prev = prices.get(ts - lookback_min * 60)
    if not prev or prev <= 0:
        return None
    return (price / prev - 1.0) * 10000.0


def relative_return(
    symbol: str,
    ts: int,
    local_return: float | None,
    leader_returns: dict[tuple[str, int, int], float],
    lookback_min: int,
) -> float | None:
    if local_return is None or symbol in {"BTC", "ETH"}:
        return None
    leaders = [leader_returns[key] for leader in ("BTC", "ETH") if (key := (leader, ts, lookback_min)) in leader_returns]
    if not leaders:
        return None
    return local_return - sum(leaders) / len(leaders)


def donchian_bucket(price: float, high: float | None, low: float | None) -> str | None:
    if high is None or low is None or high <= low:
        return None
    pos = (price - low) / (high - low)
    if price >= high:
        return "breakout_up"
    if price <= low:
        return "breakdown_down"
    if pos >= 0.85:
        return "range_top"
    if pos <= 0.15:
        return "range_bottom"
    return "range_mid"


def bin_signed_bps(value: float | None, *, tight: float = 15.0, wide: float = 60.0) -> str | None:
    if value is None:
        return None
    if value <= -wide:
        return "deep_negative"
    if value <= -tight:
        return "negative"
    if value >= wide:
        return "deep_positive"
    if value >= tight:
        return "positive"
    return "flat"


def bin_rsi(value: float | None) -> str | None:
    if value is None:
        return None
    if value <= 25.0:
        return "extreme_oversold"
    if value <= 35.0:
        return "oversold"
    if value >= 75.0:
        return "extreme_overbought"
    if value >= 65.0:
        return "overbought"
    return "neutral"


def bin_bollinger_z(value: float | None) -> str | None:
    if value is None:
        return None
    if value <= -1.1:
        return "below_lower"
    if value <= -0.6:
        return "lower_band"
    if value >= 1.1:
        return "above_upper"
    if value >= 0.6:
        return "upper_band"
    return "inside"


def bin_width(value: float | None) -> str | None:
    if value is None:
        return None
    if value >= 350.0:
        return "very_wide"
    if value >= 180.0:
        return "wide"
    if value <= 70.0:
        return "compressed"
    return "normal"


def bin_stoch(value: float | None) -> str | None:
    if value is None:
        return None
    if value <= 0.10:
        return "floor"
    if value <= 0.25:
        return "low"
    if value >= 0.90:
        return "ceiling"
    if value >= 0.75:
        return "high"
    return "mid"


def bin_spread(value: float) -> str:
    if value <= 0.5:
        return "tight"
    if value <= 2.0:
        return "ok"
    if value <= 5.0:
        return "wide"
    return "very_wide"


def bin_structure(value: float) -> str:
    if value <= -0.25:
        return "bear"
    if value <= -0.10:
        return "weak_bear"
    if value >= 0.25:
        return "bull"
    if value >= 0.10:
        return "weak_bull"
    return "neutral"


def bin_flow(value: float) -> str:
    if value <= -0.70:
        return "strong_sell"
    if value <= -0.30:
        return "sell"
    if value >= 0.70:
        return "strong_buy"
    if value >= 0.30:
        return "buy"
    return "neutral"


def bin_vwap(value: float) -> str:
    if value <= -15.0:
        return "deep_below_vwap"
    if value <= -5.0:
        return "below_vwap"
    if value >= 15.0:
        return "stretched_above_vwap"
    if value >= 5.0:
        return "above_vwap"
    return "near_vwap"


def base_keys(obs: Obs, args: argparse.Namespace) -> list[tuple[Any, ...]]:
    keys = [
        ("symbol", obs.symbol),
        ("cluster_regime", obs.cluster, obs.regime),
        ("cluster_hour", obs.cluster, f"h{obs.hour:02d}"),
        ("cluster_dow", obs.cluster, f"d{obs.dow}"),
        ("cluster_regime_hour", obs.cluster, obs.regime, f"h{obs.hour:02d}"),
        ("symbol_regime", obs.symbol, obs.regime),
        ("symbol_hour", obs.symbol, f"h{obs.hour:02d}"),
        ("symbol_dow", obs.symbol, f"d{obs.dow}"),
    ]
    if args.include_symbol_regime_hour:
        keys.append(("symbol_regime_hour", obs.symbol, obs.regime, f"h{obs.hour:02d}"))
    return keys


NEUTRAL_BUCKETS = {"neutral", "flat", "inside", "normal", "mid", "range_mid"}


def indicator_keys(obs: Obs, tech: TechSnapshot | None, args: argparse.Namespace) -> list[tuple[Any, ...]]:
    pairs = [
        ("spread", bin_spread(obs.spread_bps)),
        ("structure", bin_structure(obs.structure)),
        ("flow", bin_flow(obs.flow)),
        ("vwap", bin_vwap(obs.vwap)),
        ("range_vol", bin_signed_bps(obs.range_bps, tight=10.0, wide=35.0)),
    ]
    if tech is not None:
        pairs.extend(
            [
                ("rsi14", bin_rsi(tech.rsi14)),
                ("sma20_distance", bin_signed_bps(tech.sma20_distance_bps)),
                ("sma50_distance", bin_signed_bps(tech.sma50_distance_bps)),
                ("ema20_distance", bin_signed_bps(tech.ema20_distance_bps)),
                ("macd", tech.macd_cross),
                ("bollinger_z20", bin_bollinger_z(tech.bollinger_z20)),
                ("bollinger_width20", bin_width(tech.bollinger_width_bps)),
                ("stoch14", bin_stoch(tech.stoch14)),
                ("donchian20", tech.donchian20),
                ("momentum15", bin_signed_bps(tech.ret15_bps)),
                ("momentum60", bin_signed_bps(tech.ret60_bps, tight=25.0, wide=100.0)),
                ("momentum240", bin_signed_bps(tech.ret240_bps, tight=50.0, wide=180.0)),
                ("relative60", bin_signed_bps(tech.rel60_bps, tight=20.0, wide=80.0)),
                ("relative240", bin_signed_bps(tech.rel240_bps, tight=50.0, wide=180.0)),
            ]
        )
    keys = []
    for name, bucket in pairs:
        if bucket is None:
            continue
        if not args.include_neutral_indicator_buckets and bucket in NEUTRAL_BUCKETS:
            continue
        keys.append(("indicator_cluster", obs.cluster, name, bucket))
        if args.include_symbol_indicators:
            keys.append(("indicator_symbol", obs.symbol, name, bucket))
    return keys


def chart_pattern_keys(obs: Obs, tech: TechSnapshot | None) -> dict[str, list[tuple[Any, ...]]]:
    long_keys: list[tuple[Any, ...]] = []
    short_keys: list[tuple[Any, ...]] = []

    def add(side: str, name: str) -> None:
        key_cluster = ("chart_pattern_cluster", obs.cluster, name)
        key_symbol = ("chart_pattern_symbol", obs.symbol, name)
        if side == "long":
            long_keys.extend([key_cluster, key_symbol])
        else:
            short_keys.extend([key_cluster, key_symbol])

    if obs.flow >= 0.70 and obs.structure >= 0.10 and obs.vwap >= -3.0:
        add("long", "flow_structure_continuation")
    if obs.flow <= -0.70 and obs.structure <= -0.10 and obs.vwap <= 3.0:
        add("short", "flow_structure_continuation")
    if obs.structure >= 0.20 and -12.0 <= obs.vwap <= 3.0 and obs.flow >= 0.25:
        add("long", "trend_pullback_reclaim")
    if obs.structure <= -0.20 and -3.0 <= obs.vwap <= 12.0 and obs.flow <= -0.25:
        add("short", "trend_pullback_reclaim")
    if obs.vwap >= 15.0 and obs.flow <= 0.0 and obs.micro <= 0.0:
        add("short", "stretched_vwap_fade")
    if obs.vwap <= -15.0 and obs.flow >= 0.0 and obs.micro >= 0.0:
        add("long", "stretched_vwap_fade")
    if obs.book >= 0.25 and obs.flow <= -0.30 and obs.micro >= 0.15:
        add("long", "absorption_against_flow")
    if obs.book <= -0.25 and obs.flow >= 0.30 and obs.micro <= -0.15:
        add("short", "absorption_against_flow")
    if obs.compression >= 0.70 and obs.flow >= 0.70 and obs.vwap >= 0:
        add("long", "compressed_flow_breakout")
    if obs.compression >= 0.70 and obs.flow <= -0.70 and obs.vwap <= 0:
        add("short", "compressed_flow_breakout")
    if tech is not None:
        if tech.donchian20 == "breakout_up" and obs.volume_ratio >= 1.10:
            add("long", "donchian_volume_breakout")
        if tech.donchian20 == "breakdown_down" and obs.volume_ratio >= 1.10:
            add("short", "donchian_volume_breakdown")
        if tech.rsi14 is not None and tech.rsi14 <= 35.0 and obs.flow >= 0.0:
            add("long", "rsi_oversold_reversal")
        if tech.rsi14 is not None and tech.rsi14 >= 65.0 and obs.flow <= 0.0:
            add("short", "rsi_overbought_fade")
        if tech.macd_cross == "cross_up":
            add("long", "macd_cross_up")
        if tech.macd_cross == "cross_down":
            add("short", "macd_cross_down")
        if tech.bollinger_z20 is not None and tech.bollinger_z20 <= -1.0 and obs.flow >= 0:
            add("long", "bollinger_lower_revert")
        if tech.bollinger_z20 is not None and tech.bollinger_z20 >= 1.0 and obs.flow <= 0:
            add("short", "bollinger_upper_revert")
        if tech.rel60_bps is not None and tech.rel60_bps <= -80.0 and obs.flow <= 0.0:
            add("short", "relative_weakness_short")
        if tech.rel60_bps is not None and tech.rel60_bps >= 80.0 and obs.flow >= 0.0:
            add("long", "relative_strength_long")
    return {"long": long_keys, "short": short_keys}


def scan_edges(
    obs_by_key: dict[tuple[str, int], Obs],
    price_by_symbol: dict[str, dict[int, float]],
    tech_by_key: dict[tuple[str, int], TechSnapshot],
    horizons: list[int],
    args: argparse.Namespace,
) -> tuple[dict[tuple[Any, ...], EdgeStat], dict[tuple[tuple[Any, ...], str], EdgeStat], dict[str, Any]]:
    stats: dict[tuple[Any, ...], EdgeStat] = defaultdict(EdgeStat)
    period_stats: dict[tuple[tuple[Any, ...], str], EdgeStat] = defaultdict(EdgeStat)
    coverage = Counter()
    ordered_obs = sorted(obs_by_key.values(), key=lambda item: (item.ts, item.symbol))
    for idx, obs in enumerate(ordered_obs, start=1):
        prices = price_by_symbol.get(obs.symbol, {})
        tech = tech_by_key.get((obs.symbol, obs.ts))
        neutral_keys = base_keys(obs, args) + indicator_keys(obs, tech, args)
        side_patterns = chart_pattern_keys(obs, tech)
        cost_bps = all_in_cost_bps(obs, args)
        month = month_label(obs.ts)
        for horizon in horizons:
            future = prices.get(obs.ts + horizon * 60)
            if not future or future <= 0:
                continue
            ret_bps = (future / obs.price - 1.0) * 10000.0
            coverage[(horizon, obs.cluster)] += 1
            for side, gross_bps in (("long", ret_bps), ("short", -ret_bps)):
                keys = neutral_keys + side_patterns[side]
                for feature_key in keys:
                    key = (horizon, side, *feature_key)
                    stats[key].add(gross_bps, cost_bps)
                    period_stats[(key, month)].add(gross_bps, cost_bps)
        if not args.quiet and idx % 50_000 == 0:
            print(f"screened {idx}/{len(ordered_obs)} observations stats={len(stats)}", flush=True)
    return stats, period_stats, {"coverage": {f"{h}:{c}": n for (h, c), n in coverage.items()}}


def row_from_key(key: tuple[Any, ...], stat: EdgeStat, periods: list[tuple[str, EdgeStat]]) -> dict[str, Any]:
    d = stat.to_dict()
    month_rows = []
    for month, pstat in periods:
        pd = pstat.to_dict()
        month_rows.append(
            {
                "month": month,
                "n": pd["n"],
                "mean_net_bps": pd["mean_net_bps"],
                "win_rate": pd["win_rate"],
                "profit_factor": pd["profit_factor"],
            }
        )
    month_rows.sort(key=lambda item: item["month"])
    positive_months = sum(1 for row in month_rows if row["n"] >= 20 and row["mean_net_bps"] > 0)
    negative_months = sum(1 for row in month_rows if row["n"] >= 20 and row["mean_net_bps"] < 0)
    return {
        "horizon_min": key[0],
        "side": key[1],
        "group": key[2],
        "factor": "|".join(str(part) for part in key[3:]),
        "n": d["n"],
        "mean_net_bps": round(d["mean_net_bps"], 4),
        "mean_gross_bps": round(d["mean_gross_bps"], 4),
        "mean_cost_bps": round(d["mean_cost_bps"], 4),
        "win_rate": round(d["win_rate"], 4),
        "profit_factor": round(d["profit_factor"], 4) if d["profit_factor"] is not None else None,
        "t_like": round(d["t_like"], 4),
        "positive_months": positive_months,
        "negative_months": negative_months,
        "months": month_rows,
        "score": abs(d["mean_net_bps"]) * math.sqrt(d["n"]) * max(0.5, min(3.0, d["profit_factor"] or 0.5)),
    }


def classify(row: dict[str, Any]) -> str:
    if row["n"] < 120:
        return "too_small"
    if row["mean_net_bps"] <= 0:
        return "negative"
    if row["positive_months"] < 2:
        return "one_period_only"
    pf = row["profit_factor"] or 0.0
    if pf >= 1.15 and row["mean_net_bps"] >= 3.0:
        return "candidate_next_replay"
    return "research_only"


def build_rows(
    stats: dict[tuple[Any, ...], EdgeStat],
    period_stats: dict[tuple[tuple[Any, ...], str], EdgeStat],
    *,
    min_n: int,
    min_mean_net_bps: float,
    top_limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    all_rows = []
    periods_by_key: dict[tuple[Any, ...], list[tuple[str, EdgeStat]]] = defaultdict(list)
    for (key, month), pstat in period_stats.items():
        periods_by_key[key].append((month, pstat))
    for key, stat in stats.items():
        if stat.n < min_n:
            continue
        row = row_from_key(key, stat, periods_by_key.get(key, []))
        row["classification"] = classify(row)
        all_rows.append(row)
    positive = [row for row in all_rows if row["mean_net_bps"] >= min_mean_net_bps]
    positive.sort(key=lambda row: (row["classification"] == "candidate_next_replay", row["score"], row["n"]), reverse=True)
    all_rows.sort(key=lambda row: (row["score"], abs(row["mean_net_bps"]), row["n"]), reverse=True)
    return positive[:top_limit], all_rows[:top_limit]


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "classification",
        "horizon_min",
        "side",
        "group",
        "factor",
        "n",
        "mean_net_bps",
        "mean_gross_bps",
        "mean_cost_bps",
        "win_rate",
        "profit_factor",
        "t_like",
        "positive_months",
        "negative_months",
        "score",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def write_report(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# P1-09b - Screener factoriel exhaustif",
        "",
        f"- Genere le: `{payload['generated_at']}`",
        f"- Statut: `{payload['status']}`",
        f"- Snapshots: `{payload['snapshot_coverage'].get('first_ts')}` -> `{payload['snapshot_coverage'].get('last_ts')}`",
        f"- Horizons testes: `{payload['parameters']['horizons_min']}` minutes",
        f"- Cout: `{payload['parameters']['fee_slippage_bps']}` bps + spread snapshot.",
        "",
        "## Limite importante",
        "",
        "Ce screener ne consomme pas TradingView comme source externe. Il calcule localement des equivalents courants "
        "depuis les snapshots 5m disponibles: RSI, MACD, moyennes, Bollinger, Donchian, momentum, force relative, "
        "VWAP, flow/book, regimes, heures/jours et figures chartistes approximatives. Les resultats restent des "
        "hypotheses de recherche soumises au risque de multiple testing.",
        "",
        "## Meilleurs candidats positifs",
        "",
        "| Classif | Horizon | Side | Groupe | Facteur | N | Net bps | PF | WR | Mois + / - |",
        "|---|---:|---:|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in payload["top_positive_edges"][:40]:
        lines.append(
            f"| `{row['classification']}` | {row['horizon_min']} | `{row['side']}` | `{row['group']}` | "
            f"`{row['factor']}` | {row['n']} | {row['mean_net_bps']:.2f} | "
            f"{format_optional(row['profit_factor'])} | {row['win_rate']:.1%} | "
            f"{row['positive_months']}/{row['negative_months']} |"
        )
    lines.extend(
        [
            "",
            "## Lecture",
            "",
            "- `candidate_next_replay` signifie uniquement: assez positif et present sur au moins deux mois pour meriter un replay integre.",
            "- `one_period_only` et `research_only` ne doivent pas etre promus sans nouvelle validation hors echantillon.",
            "- Les lignes par symbole/heure/regime sont utiles pour generer des hypotheses, mais elles ne remplacent pas un replay full-bot.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def format_optional(value: Any) -> str:
    if value is None:
        return "na"
    return f"{float(value):.2f}"


def main() -> None:
    args = parse_args()
    horizons = parse_horizons(args.horizons_min)
    generated_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(args.output_dir or ROOT / "server-data" / "replay_reports" / f"p109b_exhaustive_factor_screen_{generated_at}")
    output_dir.mkdir(parents=True, exist_ok=True)
    obs_by_key, price_by_symbol, snapshot_coverage = load_snapshots(Path(args.snapshot_dir), quiet=args.quiet)
    tech_by_key = build_tech_index(price_by_symbol)
    stats, period_stats, coverage = scan_edges(obs_by_key, price_by_symbol, tech_by_key, horizons, args)
    top_positive, top_all = build_rows(
        stats,
        period_stats,
        min_n=args.min_n,
        min_mean_net_bps=args.min_mean_net_bps,
        top_limit=args.top_limit,
    )
    classification_counts = Counter(row["classification"] for row in top_positive)
    payload = {
        "generated_at": generated_at,
        "status": "research_screener_no_live_change",
        "parameters": {
            "snapshot_dir": str(Path(args.snapshot_dir)),
            "horizons_min": horizons,
            "fee_slippage_bps": args.fee_slippage_bps,
            "extra_slippage_bps": args.extra_slippage_bps,
            "notional_usd": args.notional_usd,
            "min_n": args.min_n,
            "min_mean_net_bps": args.min_mean_net_bps,
            "include_neutral_indicator_buckets": args.include_neutral_indicator_buckets,
            "include_symbol_indicators": args.include_symbol_indicators,
            "include_symbol_regime_hour": args.include_symbol_regime_hour,
        },
        "snapshot_coverage": snapshot_coverage,
        "forward_coverage": coverage["coverage"],
        "classification_counts_top_positive": dict(classification_counts),
        "top_positive_edges": top_positive,
        "top_all_edges": top_all,
    }
    (output_dir / "p109b_exhaustive_factor_screen.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_csv(output_dir / "top_positive_edges.csv", top_positive)
    write_csv(output_dir / "top_all_edges.csv", top_all)
    write_report(output_dir / "p109b_exhaustive_factor_screen.md", payload)
    print(output_dir)


if __name__ == "__main__":
    main()
