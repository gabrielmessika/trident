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
    volume_ratio: float | None = None
    net_volume20: float | None = None
    volume_delta20: float | None = None
    atr14_bps: float | None = None
    volume_profile20: str | None = None
    fib50: str | None = None
    supertrend: str | None = None
    ichimoku: str | None = None
    pivot_standard: str | None = None
    adx14: float | None = None
    dmi: str | None = None
    ma_cross20_50: str | None = None
    stoch_rsi14: float | None = None
    parabolic_sar: str | None = None
    obv20: float | None = None
    cci20: float | None = None
    williams_r14: float | None = None
    mfi14: float | None = None
    keltner20: str | None = None
    aroon25: str | None = None
    awesome_oscillator: float | None = None
    accumulation_distribution20: float | None = None
    chaikin_money_flow20: float | None = None
    roc20_bps: float | None = None
    hma16_distance_bps: float | None = None
    wma20_distance_bps: float | None = None
    vwma20_distance_bps: float | None = None
    ma_ribbon: str | None = None
    linear_regression20: str | None = None
    kama10_distance_bps: float | None = None
    alma20_distance_bps: float | None = None
    tema20_distance_bps: float | None = None
    trix_bps: float | None = None
    ultimate_oscillator: float | None = None
    tsi: float | None = None
    rvi10: float | None = None
    vortex14: str | None = None
    klinger: float | None = None
    ease_of_movement14: float | None = None
    pvt20: float | None = None
    anchored_vwap_distance_bps: float | None = None
    technical_rating: str | None = None


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


def build_tech_index(
    obs_by_key: dict[tuple[str, int], Obs],
    price_by_symbol: dict[str, dict[int, float]],
) -> dict[tuple[str, int], TechSnapshot]:
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
        closes: deque[float] = deque(maxlen=60)
        highs: deque[float] = deque(maxlen=60)
        lows: deque[float] = deque(maxlen=60)
        typicals: deque[float] = deque(maxlen=60)
        volumes: deque[float] = deque(maxlen=60)
        gains14: deque[float] = deque(maxlen=14)
        losses14: deque[float] = deque(maxlen=14)
        rsi_window: deque[float] = deque(maxlen=14)
        true_range14: deque[float] = deque(maxlen=14)
        plus_dm14: deque[float] = deque(maxlen=14)
        minus_dm14: deque[float] = deque(maxlen=14)
        dx14: deque[float] = deque(maxlen=14)
        money_pos14: deque[float] = deque(maxlen=14)
        money_neg14: deque[float] = deque(maxlen=14)
        bp28: deque[float] = deque(maxlen=28)
        tr28: deque[float] = deque(maxlen=28)
        clv_volume20: deque[float] = deque(maxlen=20)
        signed_volume20: deque[float] = deque(maxlen=20)
        volume_delta20: deque[float] = deque(maxlen=20)
        eom14: deque[float] = deque(maxlen=14)
        vm_plus14: deque[float] = deque(maxlen=14)
        vm_minus14: deque[float] = deque(maxlen=14)
        obv_history: deque[float] = deque(maxlen=21)
        adl_history: deque[float] = deque(maxlen=21)
        pvt_history: deque[float] = deque(maxlen=21)
        klinger_history: deque[float] = deque(maxlen=21)
        ema12 = ema26 = signal9 = ema20 = None
        ema20_1 = ema20_2 = ema20_3 = None
        tsi_mom25 = tsi_abs25 = tsi_mom13 = tsi_abs13 = None
        klinger_fast = klinger_slow = None
        kama10 = None
        sar = sar_ep = None
        sar_af = 0.02
        sar_trend: str | None = None
        obv = adl = pvt = 0.0
        prev_high = prev_low = prev_typical = None
        prev_day_key: int | None = None
        current_day_high = current_day_low = current_day_close = None
        previous_day_hlc: tuple[float, float, float] | None = None
        prev_macd = prev_signal = None
        prev_price = None
        prev_ema20_3 = None
        for ts, price in items:
            obs = obs_by_key.get((symbol, ts))
            volume = max(float(obs.notional if obs else 0.0), 0.0)
            if volume <= 0.0 and obs is not None:
                volume = max(float(obs.volume_ratio), 0.0)
            range_bps = max(float(obs.range_bps if obs else 0.0), 0.0)
            if prev_price and prev_price > 0:
                range_bps = max(range_bps, abs(price / prev_price - 1.0) * 10000.0)
            half_range = price * range_bps / 20000.0
            high = max(price, price + half_range)
            low = max(1e-12, min(price, price - half_range))
            typical = (high + low + price) / 3.0
            day_key = ts // 86400
            if prev_day_key is None:
                prev_day_key = day_key
                current_day_high = high
                current_day_low = low
                current_day_close = price
            elif day_key != prev_day_key:
                if current_day_high is not None and current_day_low is not None and current_day_close is not None:
                    previous_day_hlc = (current_day_high, current_day_low, current_day_close)
                prev_day_key = day_key
                current_day_high = high
                current_day_low = low
                current_day_close = price
            else:
                current_day_high = high if current_day_high is None else max(current_day_high, high)
                current_day_low = low if current_day_low is None else min(current_day_low, low)
                current_day_close = price

            true_range = high - low
            if prev_price is not None:
                delta = price - prev_price
                gains14.append(max(0.0, delta))
                losses14.append(max(0.0, -delta))
                true_range = max(high - low, abs(high - prev_price), abs(low - prev_price))
                if prev_high is not None and prev_low is not None:
                    up_move = high - prev_high
                    down_move = prev_low - low
                    plus_dm14.append(up_move if up_move > down_move and up_move > 0 else 0.0)
                    minus_dm14.append(down_move if down_move > up_move and down_move > 0 else 0.0)
                    vm_plus14.append(abs(high - prev_low))
                    vm_minus14.append(abs(low - prev_high))
                signed_volume = volume if delta > 0 else -volume if delta < 0 else 0.0
                obv += signed_volume
                pvt += (delta / prev_price) * volume if prev_price > 0 else 0.0
                mid = (high + low) / 2.0
                prev_mid = ((prev_high or high) + (prev_low or low)) / 2.0
                mid_move_bps = (mid / prev_mid - 1.0) * 10000.0 if prev_mid > 0 else 0.0
                eom14.append(mid_move_bps / max(volume / 100_000.0, 0.10))
                if prev_typical is not None:
                    raw_money = typical * volume
                    money_pos14.append(raw_money if typical > prev_typical else 0.0)
                    money_neg14.append(raw_money if typical < prev_typical else 0.0)
            else:
                plus_dm14.append(0.0)
                minus_dm14.append(0.0)
                vm_plus14.append(0.0)
                vm_minus14.append(0.0)
                money_pos14.append(0.0)
                money_neg14.append(0.0)
                signed_volume = 0.0

            closes.append(price)
            highs.append(high)
            lows.append(low)
            typicals.append(typical)
            volumes.append(volume)
            true_range14.append(true_range)
            bp28.append(price - min(low, prev_price if prev_price is not None else low))
            tr28.append(true_range)
            clv = ((price - low) - (high - price)) / (high - low) if high > low else 0.0
            clv_volume = clv * volume
            adl += clv_volume
            clv_volume20.append(clv_volume)
            signed_volume20.append(signed_volume)
            volume_delta20.append(float(obs.flow if obs else 0.0) * volume)
            obv_history.append(obv)
            adl_history.append(adl)
            pvt_history.append(pvt)
            ema12 = ema(ema12, price, 12)
            ema26 = ema(ema26, price, 26)
            ema20 = ema(ema20, price, 20)
            ema20_1 = ema(ema20_1, price, 20)
            ema20_2 = ema(ema20_2, ema20_1, 20) if ema20_1 is not None else None
            ema20_3 = ema(ema20_3, ema20_2, 20) if ema20_2 is not None else None
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
            closes_list = list(closes)
            highs_list = list(highs)
            lows_list = list(lows)
            typicals_list = list(typicals)
            volumes_list = list(volumes)
            sma10 = rolling_mean(deque(closes_list[-10:])) if len(closes_list) >= 10 else None
            sma20 = rolling_mean(deque(closes_list[-20:])) if len(closes_list) >= 20 else None
            sma50 = rolling_mean(deque(closes_list[-50:])) if len(closes_list) >= 50 else None
            sd20 = rolling_sd(deque(closes_list[-20:])) if len(closes_list) >= 20 else None
            high9 = max(highs_list[-9:]) if len(highs_list) >= 9 else None
            low9 = min(lows_list[-9:]) if len(lows_list) >= 9 else None
            high14 = max(highs_list[-14:]) if len(highs_list) >= 14 else None
            low14 = min(lows_list[-14:]) if len(lows_list) >= 14 else None
            high20 = max(highs_list[-20:]) if len(highs_list) >= 20 else None
            low20 = min(lows_list[-20:]) if len(lows_list) >= 20 else None
            high25 = max(highs_list[-25:]) if len(highs_list) >= 25 else None
            low25 = min(lows_list[-25:]) if len(lows_list) >= 25 else None
            high26 = max(highs_list[-26:]) if len(highs_list) >= 26 else None
            low26 = min(lows_list[-26:]) if len(lows_list) >= 26 else None
            high50 = max(highs_list[-50:]) if len(highs_list) >= 50 else None
            low50 = min(lows_list[-50:]) if len(lows_list) >= 50 else None
            high52 = max(highs_list[-52:]) if len(highs_list) >= 52 else None
            low52 = min(lows_list[-52:]) if len(lows_list) >= 52 else None
            avg_gain = rolling_mean(gains14) if len(gains14) >= 14 else None
            avg_loss = rolling_mean(losses14) if len(losses14) >= 14 else None
            rsi = None
            if avg_gain is not None and avg_loss is not None:
                rsi = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
                rsi_window.append(rsi)
            stoch_rsi = None
            if len(rsi_window) >= 14 and rsi is not None:
                rsi_low = min(rsi_window)
                rsi_high = max(rsi_window)
                stoch_rsi = (rsi - rsi_low) / (rsi_high - rsi_low) if rsi_high > rsi_low else 0.5
            atr14_bps = None
            if len(true_range14) >= 14 and price > 0:
                atr14_bps = rolling_mean(true_range14) / price * 10000.0
            plus_dm_sum = sum(plus_dm14)
            minus_dm_sum = sum(minus_dm14)
            tr_sum = sum(true_range14)
            plus_di = (plus_dm_sum / tr_sum * 100.0) if tr_sum > 0 else None
            minus_di = (minus_dm_sum / tr_sum * 100.0) if tr_sum > 0 else None
            if plus_di is not None and minus_di is not None and plus_di + minus_di > 0:
                dx14.append(abs(plus_di - minus_di) / (plus_di + minus_di) * 100.0)
            adx14 = rolling_mean(dx14) if len(dx14) >= 14 else None
            dmi = dmi_bucket(plus_di, minus_di, adx14)
            wma20 = weighted_mean(closes_list[-20:]) if len(closes_list) >= 20 else None
            wma8 = weighted_mean(closes_list[-8:]) if len(closes_list) >= 8 else None
            wma16 = weighted_mean(closes_list[-16:]) if len(closes_list) >= 16 else None
            hma16 = 2.0 * wma8 - wma16 if wma8 is not None and wma16 is not None else None
            vwma20 = volume_weighted_mean(closes_list[-20:], volumes_list[-20:]) if len(closes_list) >= 20 else None
            alma20 = alma(closes_list[-20:]) if len(closes_list) >= 20 else None
            if len(closes_list) >= 10:
                recent = closes_list[-10:]
                change = abs(recent[-1] - recent[0])
                volatility = sum(abs(recent[i] - recent[i - 1]) for i in range(1, len(recent)))
                er = change / volatility if volatility > 0 else 0.0
                fast = 2.0 / (2.0 + 1.0)
                slow = 2.0 / (30.0 + 1.0)
                sc = (er * (fast - slow) + slow) ** 2
                kama10 = price if kama10 is None else kama10 + sc * (price - kama10)
            tema20 = (
                3.0 * ema20_1 - 3.0 * ema20_2 + ema20_3
                if ema20_1 is not None and ema20_2 is not None and ema20_3 is not None
                else None
            )
            trix_bps = (ema20_3 / prev_ema20_3 - 1.0) * 10000.0 if ema20_3 and prev_ema20_3 and prev_ema20_3 > 0 else None
            if prev_price is not None:
                mom = price - prev_price
                tsi_mom25 = ema(tsi_mom25, mom, 25)
                tsi_abs25 = ema(tsi_abs25, abs(mom), 25)
                tsi_mom13 = ema(tsi_mom13, tsi_mom25, 13) if tsi_mom25 is not None else None
                tsi_abs13 = ema(tsi_abs13, tsi_abs25, 13) if tsi_abs25 is not None else None
            tsi = 100.0 * tsi_mom13 / tsi_abs13 if tsi_mom13 is not None and tsi_abs13 and tsi_abs13 > 0 else None
            vf = signed_volume * (2.0 if typical >= (prev_typical or typical) else -2.0)
            klinger_fast = ema(klinger_fast, vf, 34)
            klinger_slow = ema(klinger_slow, vf, 55)
            klinger_value = klinger_fast - klinger_slow if klinger_fast is not None and klinger_slow is not None else None
            klinger_history.append(klinger_value or 0.0)
            if sar is None:
                sar_trend = "bull" if prev_price is None or price >= prev_price else "bear"
                sar = low if sar_trend == "bull" else high
                sar_ep = high if sar_trend == "bull" else low
                sar_af = 0.02
            else:
                sar = sar + sar_af * ((sar_ep or price) - sar)
                if sar_trend == "bull":
                    if low < sar:
                        sar_trend = "bear"
                        sar = sar_ep
                        sar_ep = low
                        sar_af = 0.02
                    elif sar_ep is None or high > sar_ep:
                        sar_ep = high
                        sar_af = min(0.20, sar_af + 0.02)
                else:
                    if high > sar:
                        sar_trend = "bull"
                        sar = sar_ep
                        sar_ep = high
                        sar_af = 0.02
                    elif sar_ep is None or low < sar_ep:
                        sar_ep = low
                        sar_af = min(0.20, sar_af + 0.02)
            ret15 = trailing_return(prices, ts, price, 15)
            ret60 = trailing_return(prices, ts, price, 60)
            ret240 = trailing_return(prices, ts, price, 240)
            roc20 = (price / closes_list[-20] - 1.0) * 10000.0 if len(closes_list) >= 20 and closes_list[-20] > 0 else None
            rel60 = relative_return(symbol, ts, ret60, leader_returns, 60)
            rel240 = relative_return(symbol, ts, ret240, leader_returns, 240)
            tenkan = (high9 + low9) / 2.0 if high9 is not None and low9 is not None else None
            kijun = (high26 + low26) / 2.0 if high26 is not None and low26 is not None else None
            span_b = (high52 + low52) / 2.0 if high52 is not None and low52 is not None else None
            ichimoku = ichimoku_bucket(price, tenkan, kijun, span_b)
            supertrend = supertrend_bucket(price, ema20, atr14_bps)
            pivot = pivot_bucket(price, previous_day_hlc)
            ma_cross = ma_cross_bucket(sma20, sma50, price)
            keltner = keltner_bucket(price, ema20, atr14_bps)
            aroon = aroon_bucket(highs_list[-25:], lows_list[-25:]) if len(highs_list) >= 25 else None
            ao = None
            if len(typicals_list) >= 34:
                ao = rolling_mean(deque(typicals_list[-5:])) - rolling_mean(deque(typicals_list[-34:]))
                ao = ao / price * 10000.0 if price > 0 else None
            cci = None
            if len(typicals_list) >= 20:
                mean_typical = sum(typicals_list[-20:]) / 20.0
                mean_dev = sum(abs(value - mean_typical) for value in typicals_list[-20:]) / 20.0
                cci = (typical - mean_typical) / (0.015 * mean_dev) if mean_dev > 0 else 0.0
            williams = None
            if high14 is not None and low14 is not None and high14 > low14:
                williams = -100.0 * (high14 - price) / (high14 - low14)
            money_pos = sum(money_pos14)
            money_neg = sum(money_neg14)
            mfi = None
            if len(money_pos14) >= 14:
                mfi = 100.0 if money_neg <= 0 else 100.0 - 100.0 / (1.0 + money_pos / money_neg)
            cmf = sum(clv_volume20) / sum(volumes_list[-20:]) if len(volumes_list) >= 20 and sum(volumes_list[-20:]) > 0 else None
            uo = ultimate_oscillator(bp28, tr28)
            rvi = None
            if len(closes_list) >= 10 and len(true_range14) >= 10:
                changes = [closes_list[i] - closes_list[i - 1] for i in range(len(closes_list) - 9, len(closes_list))]
                recent_tr = list(true_range14)[-9:]
                denom = sum(recent_tr)
                rvi = 100.0 * sum(changes) / denom if denom > 0 else None
            vortex = vortex_bucket(vm_plus14, vm_minus14, true_range14)
            volume_profile = volume_profile_bucket(price, closes_list[-20:], volumes_list[-20:]) if len(closes_list) >= 20 else None
            fib = fib_bucket(price, high50, low50)
            ribbon = ma_ribbon_bucket(price, sma10, sma20, sma50, ema20)
            linreg = linear_regression_bucket(closes_list[-20:]) if len(closes_list) >= 20 else None
            technical_rating = technical_rating_bucket(
                rsi=rsi,
                macd_cross=macd_cross,
                ma_cross=ma_cross,
                supertrend=supertrend,
                dmi=dmi,
                stoch=stochastic_value(price, high14, low14),
                cci=cci,
                williams=williams,
                mfi=mfi,
                keltner=keltner,
                donchian=donchian_bucket(price, high20, low20),
            )
            tech_by_key[(symbol, ts)] = TechSnapshot(
                rsi14=rsi,
                sma20_distance_bps=(price / sma20 - 1.0) * 10000.0 if sma20 and sma20 > 0 else None,
                sma50_distance_bps=(price / sma50 - 1.0) * 10000.0 if sma50 and sma50 > 0 else None,
                ema20_distance_bps=(price / ema20 - 1.0) * 10000.0 if ema20 and ema20 > 0 else None,
                macd_hist_bps=(macd - signal9) / price * 10000.0 if macd is not None and signal9 is not None and price > 0 else None,
                macd_cross=macd_cross,
                bollinger_z20=(price - sma20) / (2.0 * sd20) if sma20 and sd20 and sd20 > 0 else None,
                bollinger_width_bps=(4.0 * sd20 / sma20 * 10000.0) if sma20 and sd20 and sma20 > 0 else None,
                stoch14=stochastic_value(price, high14, low14),
                donchian20=donchian_bucket(price, high20, low20),
                ret15_bps=ret15,
                ret60_bps=ret60,
                ret240_bps=ret240,
                rel60_bps=rel60,
                rel240_bps=rel240,
                volume_ratio=float(obs.volume_ratio) if obs else None,
                net_volume20=sum(signed_volume20) / sum(volumes_list[-20:]) if len(volumes_list) >= 20 and sum(volumes_list[-20:]) > 0 else None,
                volume_delta20=sum(volume_delta20) / sum(volumes_list[-20:]) if len(volumes_list) >= 20 and sum(volumes_list[-20:]) > 0 else None,
                atr14_bps=atr14_bps,
                volume_profile20=volume_profile,
                fib50=fib,
                supertrend=supertrend,
                ichimoku=ichimoku,
                pivot_standard=pivot,
                adx14=adx14,
                dmi=dmi,
                ma_cross20_50=ma_cross,
                stoch_rsi14=stoch_rsi,
                parabolic_sar=sar_trend,
                obv20=(
                    (obv - obv_history[0]) / sum(volumes_list[-20:])
                    if len(obv_history) >= 21 and sum(volumes_list[-20:]) > 0
                    else None
                ),
                cci20=cci,
                williams_r14=williams,
                mfi14=mfi,
                keltner20=keltner,
                aroon25=aroon,
                awesome_oscillator=ao,
                accumulation_distribution20=(
                    (adl - adl_history[0]) / sum(volumes_list[-20:])
                    if len(adl_history) >= 21 and sum(volumes_list[-20:]) > 0
                    else None
                ),
                chaikin_money_flow20=cmf,
                roc20_bps=roc20,
                hma16_distance_bps=(price / hma16 - 1.0) * 10000.0 if hma16 and hma16 > 0 else None,
                wma20_distance_bps=(price / wma20 - 1.0) * 10000.0 if wma20 and wma20 > 0 else None,
                vwma20_distance_bps=(price / vwma20 - 1.0) * 10000.0 if vwma20 and vwma20 > 0 else None,
                ma_ribbon=ribbon,
                linear_regression20=linreg,
                kama10_distance_bps=(price / kama10 - 1.0) * 10000.0 if kama10 and kama10 > 0 else None,
                alma20_distance_bps=(price / alma20 - 1.0) * 10000.0 if alma20 and alma20 > 0 else None,
                tema20_distance_bps=(price / tema20 - 1.0) * 10000.0 if tema20 and tema20 > 0 else None,
                trix_bps=trix_bps,
                ultimate_oscillator=uo,
                tsi=tsi,
                rvi10=rvi,
                vortex14=vortex,
                klinger=(
                    klinger_value / (sum(volumes_list[-20:]) / 20.0)
                    if klinger_value is not None and len(volumes_list) >= 20 and sum(volumes_list[-20:]) > 0
                    else None
                ),
                ease_of_movement14=rolling_mean(eom14) if len(eom14) >= 14 else None,
                pvt20=(
                    (pvt - pvt_history[0]) / sum(volumes_list[-20:]) * 10000.0
                    if len(pvt_history) >= 21 and sum(volumes_list[-20:]) > 0
                    else None
                ),
                anchored_vwap_distance_bps=anchored_vwap_distance(price, closes_list, volumes_list),
                technical_rating=technical_rating,
            )
            prev_price = price
            prev_high = high
            prev_low = low
            prev_typical = typical
            prev_macd = macd
            prev_signal = signal9
            prev_ema20_3 = ema20_3
    return tech_by_key


def weighted_mean(values: list[float]) -> float | None:
    if not values:
        return None
    weights = list(range(1, len(values) + 1))
    denom = sum(weights)
    return sum(value * weight for value, weight in zip(values, weights)) / denom if denom else None


def volume_weighted_mean(prices: list[float], volumes: list[float]) -> float | None:
    denom = sum(max(volume, 0.0) for volume in volumes)
    if denom <= 0:
        return None
    return sum(price * max(volume, 0.0) for price, volume in zip(prices, volumes)) / denom


def alma(values: list[float], *, offset: float = 0.85, sigma: float = 6.0) -> float | None:
    if not values:
        return None
    m = offset * (len(values) - 1)
    s = len(values) / sigma
    weights = [math.exp(-((idx - m) ** 2) / (2.0 * s * s)) for idx in range(len(values))]
    denom = sum(weights)
    return sum(value * weight for value, weight in zip(values, weights)) / denom if denom else None


def stochastic_value(price: float, high: float | None, low: float | None) -> float | None:
    if high is None or low is None or high <= low:
        return None
    return (price - low) / (high - low)


def dmi_bucket(plus_di: float | None, minus_di: float | None, adx: float | None) -> str | None:
    if plus_di is None or minus_di is None or adx is None:
        return None
    strength = "strong" if adx >= 25.0 else "weak" if adx >= 15.0 else "flat"
    if plus_di > minus_di * 1.1:
        return f"bull_{strength}"
    if minus_di > plus_di * 1.1:
        return f"bear_{strength}"
    return f"neutral_{strength}"


def supertrend_bucket(price: float, ema20: float | None, atr14_bps: float | None) -> str | None:
    if ema20 is None or atr14_bps is None or price <= 0:
        return None
    distance = (price / ema20 - 1.0) * 10000.0
    band = max(20.0, atr14_bps * 1.5)
    if distance >= band:
        return "bull_above_band"
    if distance <= -band:
        return "bear_below_band"
    if distance >= 0:
        return "bull_inside_band"
    return "bear_inside_band"


def ichimoku_bucket(price: float, tenkan: float | None, kijun: float | None, span_b: float | None) -> str | None:
    if tenkan is None or kijun is None or span_b is None:
        return None
    cloud_top = max(kijun, span_b)
    cloud_bottom = min(kijun, span_b)
    if price > cloud_top and tenkan >= kijun:
        return "bull_above_cloud"
    if price < cloud_bottom and tenkan <= kijun:
        return "bear_below_cloud"
    if cloud_bottom <= price <= cloud_top:
        return "inside_cloud"
    if tenkan >= kijun:
        return "bull_cross_cloud_edge"
    return "bear_cross_cloud_edge"


def pivot_bucket(price: float, previous_day_hlc: tuple[float, float, float] | None) -> str | None:
    if previous_day_hlc is None:
        return None
    high, low, close = previous_day_hlc
    if high <= low:
        return None
    pivot = (high + low + close) / 3.0
    r1 = 2.0 * pivot - low
    s1 = 2.0 * pivot - high
    r2 = pivot + (high - low)
    s2 = pivot - (high - low)
    if price >= r2:
        return "above_r2"
    if price >= r1:
        return "r1_r2"
    if price >= pivot:
        return "pivot_r1"
    if price <= s2:
        return "below_s2"
    if price <= s1:
        return "s2_s1"
    return "s1_pivot"


def ma_cross_bucket(sma20: float | None, sma50: float | None, price: float) -> str | None:
    if sma20 is None or sma50 is None:
        return None
    if sma20 > sma50 and price >= sma20:
        return "bull_price_above_fast"
    if sma20 > sma50:
        return "bull_price_below_fast"
    if sma20 < sma50 and price <= sma20:
        return "bear_price_below_fast"
    if sma20 < sma50:
        return "bear_price_above_fast"
    return "flat"


def keltner_bucket(price: float, ema20: float | None, atr14_bps: float | None) -> str | None:
    if ema20 is None or atr14_bps is None or price <= 0:
        return None
    distance = (price / ema20 - 1.0) * 10000.0
    band = max(10.0, atr14_bps * 2.0)
    if distance >= band:
        return "above_upper"
    if distance <= -band:
        return "below_lower"
    if distance >= band * 0.5:
        return "upper_half"
    if distance <= -band * 0.5:
        return "lower_half"
    return "middle"


def aroon_bucket(highs: list[float], lows: list[float]) -> str | None:
    if not highs or not lows or len(highs) != len(lows):
        return None
    period = len(highs)
    high_idx = max(range(period), key=lambda idx: highs[idx])
    low_idx = min(range(period), key=lambda idx: lows[idx])
    aroon_up = 100.0 * (period - 1 - (period - 1 - high_idx)) / max(period - 1, 1)
    aroon_down = 100.0 * (period - 1 - (period - 1 - low_idx)) / max(period - 1, 1)
    if aroon_up >= 70.0 and aroon_down <= 30.0:
        return "bull"
    if aroon_down >= 70.0 and aroon_up <= 30.0:
        return "bear"
    if aroon_up >= 70.0 and aroon_down >= 70.0:
        return "volatile"
    return "neutral"


def vortex_bucket(vm_plus14: deque[float], vm_minus14: deque[float], true_range14: deque[float]) -> str | None:
    tr = sum(true_range14)
    if len(vm_plus14) < 14 or len(vm_minus14) < 14 or tr <= 0:
        return None
    plus = sum(vm_plus14) / tr
    minus = sum(vm_minus14) / tr
    if plus >= minus * 1.08:
        return "bull"
    if minus >= plus * 1.08:
        return "bear"
    return "neutral"


def volume_profile_bucket(price: float, closes: list[float], volumes: list[float]) -> str | None:
    if len(closes) < 10:
        return None
    low = min(closes)
    high = max(closes)
    if high <= low:
        return "flat_profile"
    buckets = [0.0] * 5
    for close, volume in zip(closes, volumes):
        idx = min(4, max(0, int((close - low) / (high - low) * 5.0)))
        buckets[idx] += max(volume, 0.0)
    current = min(4, max(0, int((price - low) / (high - low) * 5.0)))
    hvn = max(range(5), key=lambda idx: buckets[idx])
    if current == hvn:
        return "at_high_volume_node"
    if current > hvn:
        return "above_high_volume_node"
    return "below_high_volume_node"


def fib_bucket(price: float, high: float | None, low: float | None) -> str | None:
    if high is None or low is None or high <= low:
        return None
    pos = (price - low) / (high - low)
    levels = [(0.236, "fib_236"), (0.382, "fib_382"), (0.5, "fib_500"), (0.618, "fib_618"), (0.786, "fib_786")]
    nearest, name = min(levels, key=lambda item: abs(pos - item[0]))
    if abs(pos - nearest) <= 0.04:
        return name
    if pos >= 0.86:
        return "near_range_high"
    if pos <= 0.14:
        return "near_range_low"
    return "between_fib_levels"


def ma_ribbon_bucket(
    price: float,
    sma10: float | None,
    sma20: float | None,
    sma50: float | None,
    ema20: float | None,
) -> str | None:
    if sma10 is None or sma20 is None or sma50 is None or ema20 is None:
        return None
    if price > sma10 > ema20 > sma20 > sma50:
        return "bull_stacked"
    if price < sma10 < ema20 < sma20 < sma50:
        return "bear_stacked"
    if price >= sma20 and sma10 >= sma50:
        return "bull_mixed"
    if price <= sma20 and sma10 <= sma50:
        return "bear_mixed"
    return "tangled"


def linear_regression_bucket(values: list[float]) -> str | None:
    if len(values) < 3:
        return None
    n = len(values)
    x_mean = (n - 1) / 2.0
    y_mean = sum(values) / n
    denom = sum((idx - x_mean) ** 2 for idx in range(n))
    if denom <= 0 or y_mean <= 0:
        return None
    slope = sum((idx - x_mean) * (value - y_mean) for idx, value in enumerate(values)) / denom
    slope_bps = slope / y_mean * 10000.0
    last_fit = y_mean + slope * ((n - 1) - x_mean)
    residual_bps = (values[-1] / last_fit - 1.0) * 10000.0 if last_fit > 0 else 0.0
    if slope_bps >= 8.0 and residual_bps >= 0:
        return "uptrend_above_line"
    if slope_bps >= 8.0:
        return "uptrend_below_line"
    if slope_bps <= -8.0 and residual_bps <= 0:
        return "downtrend_below_line"
    if slope_bps <= -8.0:
        return "downtrend_above_line"
    return "flat"


def ultimate_oscillator(bp28: deque[float], tr28: deque[float]) -> float | None:
    if len(bp28) < 28 or len(tr28) < 28:
        return None
    bp = list(bp28)
    tr = list(tr28)

    def avg(length: int) -> float | None:
        tr_sum = sum(tr[-length:])
        if tr_sum <= 0:
            return None
        return sum(bp[-length:]) / tr_sum

    avg7 = avg(7)
    avg14 = avg(14)
    avg28 = avg(28)
    if avg7 is None or avg14 is None or avg28 is None:
        return None
    return 100.0 * (4.0 * avg7 + 2.0 * avg14 + avg28) / 7.0


def anchored_vwap_distance(price: float, closes: list[float], volumes: list[float]) -> float | None:
    if len(closes) < 20:
        return None
    window_prices = closes[-288:]
    window_volumes = volumes[-288:]
    avwap = volume_weighted_mean(window_prices, window_volumes)
    if avwap is None or avwap <= 0:
        return None
    return (price / avwap - 1.0) * 10000.0


def technical_rating_bucket(**values: Any) -> str | None:
    votes = 0
    score = 0

    def add(direction: int | None) -> None:
        nonlocal votes, score
        if direction is None:
            return
        votes += 1
        score += direction

    rsi = values.get("rsi")
    if rsi is not None:
        add(1 if 50.0 <= rsi <= 75.0 else -1 if rsi < 35.0 or rsi > 80.0 else 0)
    macd = values.get("macd_cross")
    add(1 if macd in {"bull", "cross_up"} else -1 if macd in {"bear", "cross_down"} else None)
    ma_cross = values.get("ma_cross")
    add(1 if isinstance(ma_cross, str) and ma_cross.startswith("bull") else -1 if isinstance(ma_cross, str) and ma_cross.startswith("bear") else None)
    supertrend = values.get("supertrend")
    add(1 if isinstance(supertrend, str) and supertrend.startswith("bull") else -1 if isinstance(supertrend, str) and supertrend.startswith("bear") else None)
    dmi = values.get("dmi")
    add(1 if isinstance(dmi, str) and dmi.startswith("bull") else -1 if isinstance(dmi, str) and dmi.startswith("bear") else None)
    stoch = values.get("stoch")
    if stoch is not None:
        add(1 if 0.2 <= stoch <= 0.8 else -1 if stoch > 0.9 else 0)
    cci = values.get("cci")
    if cci is not None:
        add(1 if 0.0 <= cci <= 150.0 else -1 if cci < -150.0 or cci > 200.0 else 0)
    williams = values.get("williams")
    if williams is not None:
        add(1 if -80.0 < williams < -20.0 else -1 if williams <= -90.0 or williams >= -10.0 else 0)
    mfi = values.get("mfi")
    if mfi is not None:
        add(1 if 45.0 <= mfi <= 75.0 else -1 if mfi < 20.0 or mfi > 85.0 else 0)
    keltner = values.get("keltner")
    add(1 if keltner == "upper_half" else -1 if keltner in {"above_upper", "below_lower"} else 0 if keltner else None)
    donchian = values.get("donchian")
    add(1 if donchian == "breakout_up" else -1 if donchian == "breakdown_down" else 0 if donchian else None)
    if votes == 0:
        return None
    rating = score / votes
    if rating >= 0.45:
        return "strong_buy"
    if rating >= 0.15:
        return "buy"
    if rating <= -0.45:
        return "strong_sell"
    if rating <= -0.15:
        return "sell"
    return "neutral"


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


def bin_volume_ratio(value: float | None) -> str | None:
    if value is None:
        return None
    if value >= 5.0:
        return "extreme_high"
    if value >= 2.0:
        return "high"
    if value >= 1.1:
        return "above_normal"
    if value <= 0.4:
        return "dry"
    if value <= 0.8:
        return "below_normal"
    return "normal"


def bin_atr(value: float | None) -> str | None:
    if value is None:
        return None
    if value >= 120.0:
        return "extreme"
    if value >= 60.0:
        return "high"
    if value >= 25.0:
        return "normal"
    return "low"


def bin_adx(value: float | None) -> str | None:
    if value is None:
        return None
    if value >= 35.0:
        return "very_strong"
    if value >= 25.0:
        return "strong"
    if value >= 15.0:
        return "developing"
    return "weak"


def bin_osc_100(value: float | None) -> str | None:
    if value is None:
        return None
    if value <= 20.0:
        return "oversold"
    if value >= 80.0:
        return "overbought"
    if value >= 60.0:
        return "positive"
    if value <= 40.0:
        return "negative"
    return "neutral"


def bin_signed_unit(value: float | None, *, tight: float = 0.10, wide: float = 0.35) -> str | None:
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


def bin_cci(value: float | None) -> str | None:
    if value is None:
        return None
    if value <= -200.0:
        return "extreme_low"
    if value <= -100.0:
        return "low"
    if value >= 200.0:
        return "extreme_high"
    if value >= 100.0:
        return "high"
    return "neutral"


def bin_williams(value: float | None) -> str | None:
    if value is None:
        return None
    if value <= -90.0:
        return "floor"
    if value <= -80.0:
        return "oversold"
    if value >= -10.0:
        return "ceiling"
    if value >= -20.0:
        return "overbought"
    return "neutral"


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


def bin_vwap(value: float | None) -> str | None:
    if value is None:
        return None
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
        ("volume", bin_volume_ratio(obs.volume_ratio)),
        ("net_volume", bin_signed_unit(obs.flow)),
        ("volume_delta", bin_signed_unit(obs.flow)),
        ("range_vol", bin_signed_bps(obs.range_bps, tight=10.0, wide=35.0)),
    ]
    if tech is not None:
        pairs.extend(
            [
                ("rsi14", bin_rsi(tech.rsi14)),
                ("sma20_distance", bin_signed_bps(tech.sma20_distance_bps)),
                ("sma50_distance", bin_signed_bps(tech.sma50_distance_bps)),
                ("ema20_distance", bin_signed_bps(tech.ema20_distance_bps)),
                ("wma20_distance", bin_signed_bps(tech.wma20_distance_bps)),
                ("vwma20_distance", bin_signed_bps(tech.vwma20_distance_bps)),
                ("hma16_distance", bin_signed_bps(tech.hma16_distance_bps)),
                ("kama10_distance", bin_signed_bps(tech.kama10_distance_bps)),
                ("alma20_distance", bin_signed_bps(tech.alma20_distance_bps)),
                ("tema20_distance", bin_signed_bps(tech.tema20_distance_bps)),
                ("ma_cross20_50", tech.ma_cross20_50),
                ("ma_ribbon", tech.ma_ribbon),
                ("macd", tech.macd_cross),
                ("trix", bin_signed_bps(tech.trix_bps, tight=4.0, wide=16.0)),
                ("bollinger_z20", bin_bollinger_z(tech.bollinger_z20)),
                ("bollinger_width20", bin_width(tech.bollinger_width_bps)),
                ("keltner20", tech.keltner20),
                ("stoch14", bin_stoch(tech.stoch14)),
                ("stoch_rsi14", bin_stoch(tech.stoch_rsi14)),
                ("donchian20", tech.donchian20),
                ("atr14", bin_atr(tech.atr14_bps)),
                ("volume_profile20", tech.volume_profile20),
                ("fib50", tech.fib50),
                ("supertrend", tech.supertrend),
                ("ichimoku", tech.ichimoku),
                ("pivot_standard", tech.pivot_standard),
                ("adx14", bin_adx(tech.adx14)),
                ("dmi", tech.dmi),
                ("parabolic_sar", tech.parabolic_sar),
                ("net_volume20", bin_signed_unit(tech.net_volume20)),
                ("volume_delta20", bin_signed_unit(tech.volume_delta20)),
                ("obv20", bin_signed_unit(tech.obv20)),
                ("accumulation_distribution20", bin_signed_unit(tech.accumulation_distribution20)),
                ("chaikin_money_flow20", bin_signed_unit(tech.chaikin_money_flow20)),
                ("mfi14", bin_osc_100(tech.mfi14)),
                ("cci20", bin_cci(tech.cci20)),
                ("williams_r14", bin_williams(tech.williams_r14)),
                ("aroon25", tech.aroon25),
                ("awesome_oscillator", bin_signed_bps(tech.awesome_oscillator, tight=8.0, wide=35.0)),
                ("ultimate_oscillator", bin_osc_100(tech.ultimate_oscillator)),
                ("tsi", bin_signed_bps(tech.tsi, tight=8.0, wide=25.0)),
                ("rvi10", bin_signed_bps(tech.rvi10, tight=15.0, wide=50.0)),
                ("vortex14", tech.vortex14),
                ("klinger", bin_signed_unit(tech.klinger)),
                ("ease_of_movement14", bin_signed_bps(tech.ease_of_movement14, tight=5.0, wide=20.0)),
                ("pvt20", bin_signed_bps(tech.pvt20, tight=15.0, wide=60.0)),
                ("anchored_vwap", bin_vwap(tech.anchored_vwap_distance_bps)),
                ("linear_regression20", tech.linear_regression20),
                ("momentum15", bin_signed_bps(tech.ret15_bps)),
                ("momentum60", bin_signed_bps(tech.ret60_bps, tight=25.0, wide=100.0)),
                ("momentum240", bin_signed_bps(tech.ret240_bps, tight=50.0, wide=180.0)),
                ("roc20", bin_signed_bps(tech.roc20_bps, tight=25.0, wide=100.0)),
                ("relative60", bin_signed_bps(tech.rel60_bps, tight=20.0, wide=80.0)),
                ("relative240", bin_signed_bps(tech.rel240_bps, tight=50.0, wide=180.0)),
                ("technical_rating", tech.technical_rating),
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


def tradingview_top50_coverage() -> list[dict[str, Any]]:
    rows = [
        (1, "Moving Average / SMA", "sma20_distance, sma50_distance, ma_ribbon"),
        (2, "Exponential Moving Average / EMA", "ema20_distance"),
        (3, "Volume", "volume bucket from volume_ratio / bucket_notional_usd"),
        (4, "RSI", "rsi14"),
        (5, "MACD", "macd cross / histogram proxy"),
        (6, "Bollinger Bands", "bollinger_z20, bollinger_width20"),
        (7, "VWAP", "vwap_distance_bps"),
        (8, "Stochastic Oscillator", "stoch14"),
        (9, "ATR", "atr14_bps from synthetic 5m true range"),
        (10, "Volume Profile", "volume_profile20 rolling price-volume node proxy"),
        (11, "Fibonacci Retracement / Auto Fib Retracement", "fib50 rolling high-low retracement bucket"),
        (12, "Supertrend", "supertrend bucket from EMA20 +/- ATR band"),
        (13, "Ichimoku Cloud", "tenkan/kijun/span-b cloud proxy"),
        (14, "Pivot Points Standard", "previous UTC-day HLC pivot bucket"),
        (15, "ADX / DMI", "adx14, dmi"),
        (16, "Moving Average Cross", "ma_cross20_50"),
        (17, "Stochastic RSI", "stoch_rsi14"),
        (18, "Parabolic SAR", "parabolic_sar trend proxy"),
        (19, "OBV", "obv20 normalized slope"),
        (20, "CCI", "cci20"),
        (21, "Williams %R", "williams_r14"),
        (22, "Money Flow Index / MFI", "mfi14 with notional-volume proxy"),
        (23, "Keltner Channels", "keltner20"),
        (24, "Donchian Channels", "donchian20"),
        (25, "Aroon", "aroon25"),
        (26, "Awesome Oscillator", "awesome_oscillator"),
        (27, "Accumulation / Distribution", "accumulation_distribution20 normalized slope"),
        (28, "Chaikin Money Flow", "chaikin_money_flow20"),
        (29, "Rate of Change / ROC", "roc20"),
        (30, "Momentum", "momentum15/60/240"),
        (31, "Hull Moving Average / HMA", "hma16_distance"),
        (32, "Weighted Moving Average / WMA", "wma20_distance"),
        (33, "VWMA", "vwma20_distance"),
        (34, "Moving Average Ribbon", "ma_ribbon"),
        (35, "Linear Regression", "linear_regression20"),
        (36, "KAMA", "kama10_distance"),
        (37, "ALMA", "alma20_distance"),
        (38, "TEMA / Triple EMA", "tema20_distance"),
        (39, "TRIX", "trix"),
        (40, "Ultimate Oscillator", "ultimate_oscillator"),
        (41, "True Strength Index / TSI", "tsi"),
        (42, "Relative Vigor Index / RVI", "rvi10 synthetic close/range proxy"),
        (43, "Vortex Indicator", "vortex14"),
        (44, "Klinger Oscillator", "klinger volume-force proxy"),
        (45, "Ease of Movement", "ease_of_movement14"),
        (46, "Price Volume Trend / PVT", "pvt20 normalized slope"),
        (47, "Net Volume", "net_volume20 / trade_flow_bias proxy"),
        (48, "Volume Delta", "volume_delta20 / trade_flow_bias * notional proxy"),
        (49, "Anchored VWAP / VWAP Auto Anchored", "anchored_vwap rolling session proxy"),
        (50, "Technical Ratings", "technical_rating composite vote"),
    ]
    return [
        {
            "rank": rank,
            "indicator": indicator,
            "used": True,
            "proxy": proxy,
            "data_source": "local 5m TRIDENT snapshots; no TradingView external feed",
        }
        for rank, indicator, proxy in rows
    ]


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
        "depuis les snapshots 5m disponibles. Cette version couvre les 50 indicateurs TradingView fournis via "
        "`tradingview_top50_coverage` dans le JSON; les indicateurs volume/L2 utilisent des proxies locaux "
        "(`bucket_notional_usd`, `volume_ratio`, `trade_flow_bias`, ranges 5m). Les resultats restent des hypotheses "
        "de recherche soumises au risque de multiple testing.",
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
    tech_by_key = build_tech_index(obs_by_key, price_by_symbol)
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
        "tradingview_top50_coverage": tradingview_top50_coverage(),
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
