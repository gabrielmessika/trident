# Pod C Research Protocol

- Use only TRIDENT snapshots produced by the native converters or live collector.
- Run the lead-lag suite first on archived data, then on a recent out-of-sample window.
- Require at least one candidate with:
  - positive expectancy after the leader impulse
  - stable hit rate above the configured threshold
  - enough samples to avoid a one-day fluke
- Promote to Pod C only after the research memo says `go`.
- Keep the live event pod on the same execution and risk rules as Pod A:
  - same dry-run venue model
  - same trade-plan gate
  - same journal structure
