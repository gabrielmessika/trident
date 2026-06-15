from datetime import timedelta

from scripts.export_trident_audit_pack import compact_setup_details
from scripts.run_p106_regime_shadow_audit import (
    PriceIndex,
    ShadowEvent,
    dedupe_candidates,
    forward_returns,
    parse_timestamp,
)


def _event(symbol: str, timestamp: str) -> ShadowEvent:
    parsed = parse_timestamp(timestamp)
    assert parsed is not None
    return ShadowEvent(
        event_type="signal",
        timestamp=parsed,
        timestamp_text=timestamp,
        symbol=symbol,
        side="short",
        setup="trend_pullback_short",
        status="False",
        source="test",
        price=100.0,
        bull_score=1.0,
        bear_score=3.0,
        regime_gate="defensive",
        would_block_long=False,
        would_open_defensive_short_shadow=True,
        live_action_unchanged=True,
    )


def test_dedupe_candidates_keeps_one_symbol_per_cooldown() -> None:
    rows = [
        _event("BTC", "2026-06-13T00:00:00Z"),
        _event("BTC", "2026-06-13T01:00:00Z"),
        _event("BTC", "2026-06-13T03:01:00Z"),
        _event("ETH", "2026-06-13T01:00:00Z"),
    ]

    deduped = dedupe_candidates(
        rows,
        cooldown=timedelta(0),
    )
    assert len(deduped) == 4

    deduped = dedupe_candidates(rows, cooldown=timedelta(hours=3))
    assert [(row.symbol, row.timestamp_text) for row in deduped] == [
        ("BTC", "2026-06-13T00:00:00Z"),
        ("ETH", "2026-06-13T01:00:00Z"),
        ("BTC", "2026-06-13T03:01:00Z"),
    ]


def test_forward_returns_short_proxy_uses_exit_after_horizon_and_cost() -> None:
    index = PriceIndex()
    t0 = parse_timestamp("2026-06-13T00:00:00Z")
    t1 = parse_timestamp("2026-06-13T03:00:00Z")
    assert t0 is not None and t1 is not None
    index.add("BTC", t0, 100.0)
    index.add("BTC", t1, 99.0)
    index.sort()

    rows = forward_returns(
        [_event("BTC", "2026-06-13T00:00:00Z")],
        price_index=index,
        horizon=t1 - t0,
        cost_bps=16.0,
        notional_usd=200.0,
    )

    assert len(rows) == 1
    assert rows[0].gross_short_return_bps == 100.0
    assert rows[0].net_short_return_bps == 84.0
    assert rows[0].net_pnl_usd == 1.68


def test_compact_setup_details_exports_p106_fields() -> None:
    compacted = compact_setup_details(
        {
            "regime_shadow_mode": "observation_only",
            "bull_regime_score": 1,
            "bear_regime_score": 4,
            "regime_gate_decision": "bearish",
            "would_block_long": True,
            "would_open_defensive_short_shadow": False,
            "live_action_unchanged": True,
            "btc_ret_60m_bps": -25.0,
            "ignored": "not-exported",
        }
    )

    assert compacted["regime_shadow_mode"] == "observation_only"
    assert compacted["bear_regime_score"] == 4
    assert compacted["would_block_long"] is True
    assert compacted["btc_ret_60m_bps"] == -25.0
    assert "ignored" not in compacted
