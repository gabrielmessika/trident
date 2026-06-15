from datetime import timedelta

from scripts.export_trident_audit_pack import combine_setup_details, compact_setup_details
from scripts.run_p107_order_block_shadow_audit import (
    OrderBlockEvent,
    PriceIndex,
    dedupe_candidates,
    forward_proxies,
    parse_timestamp,
)


def _event(
    symbol: str,
    timestamp: str,
    *,
    would_block_long: bool = False,
    would_open_defensive_short: bool = False,
) -> OrderBlockEvent:
    parsed = parse_timestamp(timestamp)
    assert parsed is not None
    return OrderBlockEvent(
        event_type="signal",
        timestamp=parsed,
        timestamp_text=timestamp,
        symbol=symbol,
        side="long",
        setup="trend_pullback_long",
        status="False",
        source="test",
        price=100.0,
        regime_gate="defensive",
        bullish_order_blocks="",
        bearish_order_blocks="1h",
        has_bullish_order_block=False,
        has_bearish_order_block=True,
        would_block_long=would_block_long,
        would_open_defensive_short=would_open_defensive_short,
        live_action_unchanged=True,
    )


def test_forward_proxy_long_veto_values_blocking_help() -> None:
    index = PriceIndex()
    t0 = parse_timestamp("2026-06-14T21:15:00Z")
    t1 = parse_timestamp("2026-06-15T00:15:00Z")
    assert t0 is not None and t1 is not None
    index.add("BTC", t0, 100.0)
    index.add("BTC", t1, 99.0)
    index.sort()

    rows = forward_proxies(
        [_event("BTC", "2026-06-14T21:15:00Z", would_block_long=True)],
        kind="long_veto",
        price_index=index,
        horizon=t1 - t0,
        cost_bps=16.0,
        notional_usd=200.0,
    )

    assert len(rows) == 1
    assert rows[0].gross_return_bps == -100.0
    assert rows[0].net_return_bps == -116.0
    assert rows[0].net_pnl_usd == -2.32
    assert rows[0].decision_value_usd == 2.32


def test_forward_proxy_defensive_short_uses_short_direction() -> None:
    index = PriceIndex()
    t0 = parse_timestamp("2026-06-14T21:15:00Z")
    t1 = parse_timestamp("2026-06-15T00:15:00Z")
    assert t0 is not None and t1 is not None
    index.add("ETH", t0, 100.0)
    index.add("ETH", t1, 99.0)
    index.sort()

    rows = forward_proxies(
        [
            _event(
                "ETH",
                "2026-06-14T21:15:00Z",
                would_open_defensive_short=True,
            )
        ],
        kind="defensive_short",
        price_index=index,
        horizon=t1 - t0,
        cost_bps=16.0,
        notional_usd=200.0,
    )

    assert len(rows) == 1
    assert rows[0].gross_return_bps == 100.0
    assert rows[0].net_return_bps == 84.0
    assert rows[0].net_pnl_usd == 1.68
    assert rows[0].decision_value_usd == 1.68


def test_dedupe_candidates_keeps_one_symbol_per_kind_per_cooldown() -> None:
    rows = [
        _event("BTC", "2026-06-14T21:15:00Z", would_block_long=True),
        _event("BTC", "2026-06-14T22:15:00Z", would_block_long=True),
        _event("BTC", "2026-06-15T00:16:00Z", would_block_long=True),
        _event("BTC", "2026-06-14T22:15:00Z", would_open_defensive_short=True),
        _event("ETH", "2026-06-14T22:15:00Z", would_block_long=True),
    ]

    deduped = dedupe_candidates(rows, cooldown=timedelta(hours=3))

    assert [(row.symbol, row.timestamp_text, row.would_open_defensive_short) for row in deduped] == [
        ("BTC", "2026-06-14T21:15:00Z", False),
        ("BTC", "2026-06-14T22:15:00Z", True),
        ("ETH", "2026-06-14T22:15:00Z", False),
        ("BTC", "2026-06-15T00:16:00Z", False),
    ]


def test_compact_setup_details_exports_p107_fields() -> None:
    compacted = compact_setup_details(
        {
            "order_block_shadow_mode": "observation_only",
            "bullish_order_blocks_1h4h": "4h",
            "bearish_order_blocks_1h4h": "1h",
            "has_bullish_order_block_1h4h": True,
            "has_bearish_order_block_1h4h": True,
            "would_block_long_order_block_shadow": True,
            "would_open_defensive_short_order_block_shadow": False,
            "ignored": "not-exported",
        }
    )

    assert compacted["order_block_shadow_mode"] == "observation_only"
    assert compacted["bullish_order_blocks_1h4h"] == "4h"
    assert compacted["bearish_order_blocks_1h4h"] == "1h"
    assert compacted["has_bullish_order_block_1h4h"] is True
    assert compacted["would_block_long_order_block_shadow"] is True
    assert "ignored" not in compacted


def test_review_shadow_details_can_be_combined_for_compact_export() -> None:
    details = combine_setup_details(
        {},
        {"regime_gate_decision": "defensive"},
        {
            "order_block_shadow_mode": "observation_only",
            "has_bearish_order_block_1h4h": True,
            "would_block_long_order_block_shadow": True,
            "would_open_defensive_short_order_block_shadow": False,
        },
    )
    compacted = compact_setup_details(details)

    assert compacted["regime_gate_decision"] == "defensive"
    assert compacted["order_block_shadow_mode"] == "observation_only"
    assert compacted["has_bearish_order_block_1h4h"] is True
    assert compacted["would_block_long_order_block_shadow"] is True
