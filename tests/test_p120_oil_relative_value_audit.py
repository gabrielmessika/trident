from __future__ import annotations

from datetime import datetime, timedelta, timezone

from scripts.run_p120_oil_relative_value_audit import (
    OilObservation,
    build_candidate_rows,
    short_return_bps,
    summarize_candidates,
)


def test_short_return_bps_rewards_price_drop() -> None:
    assert short_return_bps(100.0, 99.0) == 100.0
    assert short_return_bps(100.0, 101.0) == -100.0
    assert short_return_bps(0.0, 99.0) is None
    assert short_return_bps(100.0, None) is None


def test_pair_confirmation_and_dedupe_by_symbol_horizon() -> None:
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    observations = [
        observation("cl_1", t0, "XYZ:CL", 100.0, True),
        observation("brent_1", t0, "XYZ:BRENTOIL", 200.0, True),
        observation("cl_repeat", t0 + timedelta(minutes=60), "XYZ:CL", 100.0, True),
        observation("cl_future", t0 + timedelta(minutes=240), "XYZ:CL", 99.0, False),
        observation("brent_future", t0 + timedelta(minutes=240), "XYZ:BRENTOIL", 198.0, False),
        observation("cl_repeat_future", t0 + timedelta(minutes=300), "XYZ:CL", 98.0, False),
    ]

    rows = build_candidate_rows(
        observations,
        horizon=timedelta(minutes=240),
        notional_usd=200.0,
        roundtrip_fee_bps=7.0,
    )
    summaries = summarize_candidates(rows)

    assert len(rows) == 3
    assert [row.cohort for row in rows] == ["pair_confirmed", "pair_confirmed", "solo_confirmed"]
    assert [row.deduped_240m for row in rows] == [True, True, False]
    assert rows[0].short_return_240m_bps == 100.0
    assert rows[0].proxy_pnl_usd == 1.86
    assert rows[1].pair_symbols_present == 2
    assert rows[1].pair_would_open_count == 2
    assert rows[2].exit_price_240m == 98.0

    deduped_pair = next(
        row for row in summaries if row.cohort == "pair_confirmed" and row.deduped_only
    )
    assert deduped_pair.candidates == 2
    assert deduped_pair.proxy_pnl_usd == 3.72


def test_single_oil_leg_is_solo_confirmed_when_pair_disagrees() -> None:
    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    observations = [
        observation("cl_1", t0, "XYZ:CL", 100.0, True),
        observation("brent_1", t0, "XYZ:BRENTOIL", 200.0, False),
        observation("cl_future", t0 + timedelta(minutes=240), "XYZ:CL", 99.0, False),
    ]

    rows = build_candidate_rows(
        observations,
        horizon=timedelta(minutes=240),
        notional_usd=200.0,
        roundtrip_fee_bps=7.0,
    )

    assert len(rows) == 1
    assert rows[0].cohort == "solo_confirmed"
    assert rows[0].pair_confirmed is False
    assert rows[0].pair_symbols_present == 2
    assert rows[0].pair_would_open_count == 1


def observation(
    observation_id: str,
    timestamp: datetime,
    symbol: str,
    price: float,
    would_open: bool,
) -> OilObservation:
    return OilObservation(
        observation_id=observation_id,
        timestamp=timestamp,
        symbol=symbol,
        price=price,
        would_open=would_open,
        research_regime="oil_short_4h_time_gate",
        hour_utc=timestamp.hour,
        score=1.0,
        reason="fixture",
        external_premium_bps=10.0,
        external_momentum_300s_bps=-2.0,
    )
