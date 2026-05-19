# TRIDENT full-bot backtest

- input: `server-data/replay_inputs/external_reference_multisource_20260405_20260513_baseline.jsonl`
- dates: `2026-04-05, 2026-04-06, 2026-04-07, 2026-04-08, 2026-04-09, 2026-04-10, 2026-04-11, 2026-04-12, 2026-04-13, 2026-04-14, 2026-04-15, 2026-04-16, 2026-04-17, 2026-04-18, 2026-04-20, 2026-04-21, 2026-04-22, 2026-04-23, 2026-04-24, 2026-04-25, 2026-04-26, 2026-04-27, 2026-04-30, 2026-05-01, 2026-05-02, 2026-05-03, 2026-05-04, 2026-05-05, 2026-05-06, 2026-05-07, 2026-05-08, 2026-05-12, 2026-05-13`
- records_processed: `40632`
- duplicate_timestamps_skipped: `301`
- total_realized_pnl_usd: `872.74`
- directional_fees_usd: `136.43067`

## PnL par pod

- Pod A realized_pnl_usd: `793.63`
- Pod B realized_pnl_usd: `0.0`
- Pod C realized_pnl_usd: `79.11`

## Activite

- Pod A closed_trade_count: `161`
- Pod B closed_trade_count: `0`
- Pod C closed_trade_count: `41`
- total_activity_count: `202`
- routing reassignment_event_count: `0`
- routing max_ownership_conflict_count: `0`

## Notes

- directional_fees_usd couvre Pod A, Pod B et Pod C.
- total_activity_count additionne les trades clotures des trois pods directionnels.
