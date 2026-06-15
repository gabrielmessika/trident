import unittest
from datetime import datetime, timezone

from app.live.pod_c_external_reference import (
    PodCExternalReferenceEnricher,
    ReferenceQuote,
    ReferenceSpec,
)
from app.settings import PodCExternalReferenceConfig


def _ms(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


class PodCExternalReferenceTests(unittest.TestCase):
    def test_enrich_record_adds_reference_fields_for_configured_xyz_symbol(self) -> None:
        calls: list[tuple[ReferenceSpec, float]] = []

        def fetcher(spec: ReferenceSpec, timeout_seconds: float) -> ReferenceQuote | None:
            calls.append((spec, timeout_seconds))
            return ReferenceQuote(
                source=spec.source,
                source_symbol=spec.symbol,
                price=2500.0,
                time_ms=_ms("2026-06-14T11:59:00Z"),
                momentum_60s_bps=4.0,
                momentum_300s_bps=10.0,
            )

        enricher = PodCExternalReferenceEnricher(
            PodCExternalReferenceConfig(
                timeout_seconds=1.25,
                symbols={"XYZ:GOLD": ["yahoo:GC=F"]},
            ),
            fetcher=fetcher,
            monotonic=lambda: 10.0,
        )
        record = {
            "timestamp": "2026-06-14T12:00:00Z",
            "symbols": [
                {"symbol": "XYZ:GOLD", "price": 2510.0},
                {"symbol": "BTC", "price": 65000.0},
            ],
        }

        enriched = enricher.enrich_record(record)
        gold = enriched["symbols"][0]
        btc = enriched["symbols"][1]

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], ReferenceSpec(source="yahoo", symbol="GC=F"))
        self.assertEqual(calls[0][1], 1.25)
        self.assertEqual(gold["external_reference_price"], 2500.0)
        self.assertEqual(gold["external_reference_source_count"], 1)
        self.assertEqual(gold["external_reference_sources"], "yahoo")
        self.assertEqual(gold["external_reference_symbol"], "yahoo:GC=F")
        self.assertEqual(gold["external_reference_age_seconds"], 60.0)
        self.assertEqual(gold["external_premium_bps"], 40.0)
        self.assertEqual(gold["external_momentum_60s_bps"], 4.0)
        self.assertEqual(gold["external_momentum_300s_bps"], 10.0)
        self.assertNotIn("external_reference_price", btc)
        self.assertEqual(enricher.stats_payload()["symbols_enriched"], 1)

    def test_cache_reuses_quote_but_recomputes_hyperliquid_premium(self) -> None:
        calls = 0

        def fetcher(spec: ReferenceSpec, timeout_seconds: float) -> ReferenceQuote | None:
            nonlocal calls
            calls += 1
            return ReferenceQuote(
                source=spec.source,
                source_symbol=spec.symbol,
                price=100.0,
                time_ms=_ms("2026-06-14T12:00:00Z"),
            )

        enricher = PodCExternalReferenceEnricher(
            PodCExternalReferenceConfig(
                cache_ttl_seconds=60.0,
                symbols={"XYZ:SP500": ["yahoo:ES=F"]},
            ),
            fetcher=fetcher,
            monotonic=lambda: 20.0,
        )

        first = enricher.enrich_record(
            {
                "timestamp": "2026-06-14T12:00:10Z",
                "symbols": [{"symbol": "XYZ:SP500", "price": 101.0}],
            }
        )
        second = enricher.enrich_record(
            {
                "timestamp": "2026-06-14T12:00:20Z",
                "symbols": [{"symbol": "XYZ:SP500", "price": 99.0}],
            }
        )

        self.assertEqual(calls, 1)
        self.assertEqual(first["symbols"][0]["external_premium_bps"], 100.0)
        self.assertEqual(second["symbols"][0]["external_premium_bps"], -100.0)

    def test_disabled_config_leaves_record_unchanged(self) -> None:
        enricher = PodCExternalReferenceEnricher(
            PodCExternalReferenceConfig(enabled=False),
            fetcher=lambda spec, timeout: None,
        )
        record = {"timestamp": "2026-06-14T12:00:00Z", "symbols": []}

        self.assertIs(enricher.enrich_record(record), record)


if __name__ == "__main__":
    unittest.main()
