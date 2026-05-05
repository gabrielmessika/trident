from __future__ import annotations

import math

from app.trident.hip4_outcome.config import Hip4OutcomeConfig
from app.trident.hip4_outcome.models import OutcomeMarket, ProbabilityEstimate


def normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def probability_above_strike(
    *,
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    annualized_vol: float,
) -> float:
    if spot <= 0 or strike <= 0:
        return 0.5
    if time_to_expiry_years <= 0:
        return 1.0 if spot > strike else 0.0
    if annualized_vol <= 0:
        return 1.0 if spot > strike else 0.0
    z_score = math.log(spot / strike) / (annualized_vol * math.sqrt(time_to_expiry_years))
    return max(0.0, min(1.0, normal_cdf(z_score)))


def probability_between_prices(
    *,
    spot: float,
    lower: float,
    upper: float,
    time_to_expiry_years: float,
    annualized_vol: float,
) -> float:
    if lower <= 0 or upper <= 0 or lower >= upper:
        return 0.0
    if spot <= 0:
        return 0.5
    if time_to_expiry_years <= 0 or annualized_vol <= 0:
        return 1.0 if lower <= spot <= upper else 0.0
    above_lower = probability_above_strike(
        spot=spot,
        strike=lower,
        time_to_expiry_years=time_to_expiry_years,
        annualized_vol=annualized_vol,
    )
    above_upper = probability_above_strike(
        spot=spot,
        strike=upper,
        time_to_expiry_years=time_to_expiry_years,
        annualized_vol=annualized_vol,
    )
    return max(0.0, min(1.0, above_lower - above_upper))


class ProbabilityModel:
    def __init__(self, config: Hip4OutcomeConfig) -> None:
        self.config = config

    def estimate(self, market: OutcomeMarket, *, reference_price: float, now_ts: int) -> ProbabilityEstimate:
        seconds_left = max(market.expiry_ts - now_ts, 0)
        years_left = seconds_left / (365.0 * 24.0 * 3600.0)
        vol = self.config.annualized_vol_by_underlying.get(
            market.underlying.upper(),
            self.config.default_annualized_vol,
        )
        if (
            market.class_name == "priceBucket"
            and market.bucket_lower is not None
            and market.bucket_upper is not None
        ):
            probability = probability_between_prices(
                spot=reference_price,
                lower=market.bucket_lower,
                upper=market.bucket_upper,
                time_to_expiry_years=years_left,
                annualized_vol=vol,
            )
            confidence = self._bucket_confidence(
                probability=probability,
                seconds_left=seconds_left,
                reference_price=reference_price,
                lower=market.bucket_lower,
                upper=market.bucket_upper,
            )
            return ProbabilityEstimate(
                market_id=market.market_id,
                probability_yes=probability,
                model_name="lognormal_static_vol_range_v1",
                confidence=confidence,
                inputs={
                    "reference_price": reference_price,
                    "bucket_lower": market.bucket_lower,
                    "bucket_upper": market.bucket_upper,
                    "bucket_mid": market.strike,
                    "seconds_left": float(seconds_left),
                    "annualized_vol": vol,
                    "years_left": years_left,
                },
            )
        probability = probability_above_strike(
            spot=reference_price,
            strike=market.strike,
            time_to_expiry_years=years_left,
            annualized_vol=vol,
        )
        confidence = self._confidence(
            probability=probability,
            seconds_left=seconds_left,
            reference_price=reference_price,
            strike=market.strike,
        )
        return ProbabilityEstimate(
            market_id=market.market_id,
            probability_yes=probability,
            model_name="lognormal_static_vol_v1",
            confidence=confidence,
            inputs={
                "reference_price": reference_price,
                "strike": market.strike,
                "seconds_left": float(seconds_left),
                "annualized_vol": vol,
                "years_left": years_left,
            },
        )

    def _confidence(
        self,
        *,
        probability: float,
        seconds_left: int,
        reference_price: float,
        strike: float,
    ) -> float:
        distance = abs(reference_price / strike - 1.0) if strike > 0 else 0.0
        probability_distance = abs(probability - 0.5) * 2.0
        time_component = 0.15 if seconds_left <= self.config.late_expiry_window_seconds else 0.0
        confidence = 0.45 + probability_distance * 0.25 + min(distance * 20.0, 0.15) + time_component
        return round(max(0.05, min(confidence, 0.95)), 4)

    def _bucket_confidence(
        self,
        *,
        probability: float,
        seconds_left: int,
        reference_price: float,
        lower: float,
        upper: float,
    ) -> float:
        if reference_price <= 0 or lower <= 0 or upper <= lower:
            return 0.05
        nearest_boundary = min(abs(reference_price / lower - 1.0), abs(reference_price / upper - 1.0))
        probability_distance = abs(probability - 0.5) * 2.0
        time_component = 0.12 if seconds_left <= self.config.late_expiry_window_seconds else 0.0
        confidence = 0.40 + probability_distance * 0.24 + min(nearest_boundary * 18.0, 0.14) + time_component
        return round(max(0.05, min(confidence, 0.90)), 4)
