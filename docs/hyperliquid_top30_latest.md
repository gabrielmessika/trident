# Hyperliquid Top 30 Research

- Dataset: `data/research/hyperliquid_top30/current`
- Requested window: `2025-10-19T09:56:42.744658Z` -> `2026-04-17T09:56:42.744658Z`
- Final recommendation: **park_research_only**
- Rationale: Aucun pattern transversal n'est assez robuste pour justifier un remplacement immédiat. Le meilleur chemin est de conserver la collecte, renforcer les pods existants si besoin, et exiger un replay/backtest dédié avant tout nouveau sleeve live.

## Data Coverage

| Interval | Full requested window | Median coverage ratio | Notes |
|----------|------------------------|------------------------|-------|
| 15m | 0/30 | 0.2898 | Official HL candle API limit hits this interval. |
| 30m | 0/30 | 0.5792 | Official HL candle API limit hits this interval. |
| 1h | 29/30 | 1.0 | Official HL candle API limit hits this interval. |
| 2h | 29/30 | 1.0 | Official HL candle API limit hits this interval. |

## Strongest Pattern Families

| Interval | Pattern | Archetype | Samples | Positive symbols | Hit rate | Net expectancy (bps) | Recommendation |
|----------|---------|-----------|---------|------------------|----------|----------------------|----------------|
| 15m | ttm_squeeze_release | breakout | 480 | 16/30 | 0.4396 | 1.4815 | park |
| 15m | ichimoku_continuation | trend | 6441 | 6/30 | 0.4305 | -5.9395 | kill |
| 15m | vwap_reclaim | trend | 1731 | 7/30 | 0.4131 | -9.1359 | kill |
| 15m | funding_reversion | mean_reversion | 208 | 8/24 | 0.476 | -9.4708 | kill |
| 15m | squeeze_breakout | breakout | 126 | 6/18 | 0.2936 | -14.5292 | kill |
| 15m | trend_breakout | trend | 56 | 3/8 | 0.3214 | -16.0282 | kill |
| 15m | trend_pullback | trend | 699 | 6/30 | 0.4192 | -16.7535 | kill |
| 15m | stoch_cci_reversion | mean_reversion | 358 | 7/30 | 0.4581 | -19.5763 | kill |
| 15m | range_mean_reversion | mean_reversion | 368 | 6/30 | 0.4158 | -22.3329 | kill |
| 30m | trend_breakout | trend | 105 | 6/13 | 0.4381 | 3.591 | park |
| 30m | squeeze_breakout | breakout | 219 | 15/26 | 0.4794 | -1.022 | kill |
| 30m | ttm_squeeze_release | breakout | 662 | 16/30 | 0.4441 | -4.3635 | kill |

## Symbol Recommendations

| Rank | Symbol | 24h volume ($M) | OI ($M) | Best pattern | Best TF | Net expectancy (bps) | Suggested owner | Corr BTC 1h |
|------|--------|-----------------|---------|--------------|---------|----------------------|-----------------|-------------|
| 1 | BTC | 2789.26 | 2065.09 | trend_pullback | 2h | 43.9097 | pod_a | - |
| 2 | ETH | 971.71 | 974.9 | trend_pullback | 2h | 46.1952 | pod_a | 0.8943 |
| 3 | SOL | 419.27 | 337.3 | funding_reversion | 2h | 114.5081 | new_pod_candidate | 0.8462 |
| 4 | HYPE | 275.98 | 953.83 | stoch_cci_reversion | 30m | 85.2438 | new_pod_candidate | 0.5664 |
| 5 | XRP | 54.18 | 91.6 | range_mean_reversion | 1h | 39.4144 | new_pod_candidate | 0.7937 |
| 6 | ZEC | 49.99 | 104.29 | range_mean_reversion | 15m | 101.757 | new_pod_candidate | 0.4404 |
| 7 | ORDI | 47.68 | 4.96 | ttm_squeeze_release | 2h | 164.1306 | pod_b | 0.5248 |
| 8 | DOGE | 33.58 | 33.64 | stoch_cci_reversion | 30m | 77.9577 | new_pod_candidate | 0.799 |
| 9 | kPEPE | 33.33 | 32.81 | ttm_squeeze_release | 2h | 185.1678 | pod_b | 0.7106 |
| 10 | FARTCOIN | 32.93 | 48.77 | funding_reversion | 1h | 97.6227 | new_pod_candidate | 0.5833 |
| 11 | SUI | 24.2 | 19.83 | ttm_squeeze_release | 2h | 125.386 | pod_b | 0.7916 |
| 12 | AAVE | 24.0 | 46.88 | funding_reversion | 2h | 179.7855 | new_pod_candidate | 0.7493 |
| 13 | TAO | 23.4 | 47.2 | trend_pullback | 2h | 157.9707 | pod_a | 0.6115 |
| 14 | LIT | 13.74 | 56.32 | stoch_cci_reversion | 30m | 159.8086 | new_pod_candidate | 0.3762 |
| 15 | MON | 13.41 | 56.77 | funding_reversion | 2h | 226.8642 | new_pod_candidate | 0.3719 |
| 16 | PUMP | 13.4 | 37.03 | funding_reversion | 2h | 94.5261 | new_pod_candidate | 0.6296 |
| 17 | ENA | 11.87 | 32.3 | squeeze_breakout | 30m | 104.4243 | pod_b | 0.7008 |
| 18 | XPL | 9.9 | 30.72 | trend_pullback | 2h | 204.8759 | pod_a | 0.5211 |
| 19 | NEAR | 9.71 | 38.23 | funding_reversion | 15m | 47.2388 | new_pod_candidate | 0.6556 |
| 20 | AVAX | 9.31 | 44.36 | ttm_squeeze_release | 30m | 63.5146 | pod_b | 0.7884 |
| 21 | PENGU | 9.17 | 5.78 | trend_pullback | 1h | 86.1535 | pod_a | 0.7613 |
| 22 | BIO | 9.03 | 4.24 | trend_pullback | 2h | 212.7502 | pod_a | 0.4772 |
| 23 | ARB | 8.69 | 9.36 | ttm_squeeze_release | 2h | 131.7296 | pod_b | 0.7476 |
| 24 | WLD | 8.5 | 22.09 | trend_pullback | 1h | 70.3339 | pod_a | 0.6453 |
| 25 | PNUT | 7.66 | 0.87 | stoch_cci_reversion | 1h | 161.5176 | new_pod_candidate | 0.6477 |
| 26 | BNB | 7.3 | 32.36 | trend_pullback | 1h | 13.4126 | pod_a | 0.8268 |
| 27 | LINK | 7.15 | 29.43 | squeeze_breakout | 30m | 60.4266 | pod_b | 0.8447 |
| 28 | VVV | 6.64 | 14.64 | stoch_cci_reversion | 15m | 116.173 | new_pod_candidate | 0.4262 |
| 29 | kBONK | 6.48 | 6.53 | ttm_squeeze_release | 1h | 101.2693 | pod_b | 0.7248 |
| 30 | ADA | 6.31 | 13.17 | stoch_cci_reversion | 1h | 110.0315 | new_pod_candidate | 0.8088 |

## Strongest Correlations

| Interval | Left | Right | Samples | Corr |
|----------|------|-------|---------|------|
| 30m | ETH | LINK | 5003 | 0.9162 |
| 2h | ETH | LINK | 2160 | 0.9083 |
| 2h | BTC | ETH | 2160 | 0.9048 |
| 2h | DOGE | ADA | 2160 | 0.8997 |
| 30m | BTC | ETH | 5004 | 0.8965 |
| 1h | ETH | LINK | 4320 | 0.8963 |
| 30m | AVAX | LINK | 5003 | 0.896 |
| 1h | BTC | ETH | 4320 | 0.8943 |
| 30m | LINK | ADA | 4999 | 0.8936 |
| 2h | LINK | ADA | 2160 | 0.8925 |

## Lead-Lag Candidates

| Interval | Leader | Follower | Lag bars | Lagged corr | Same-time corr |
|----------|--------|----------|----------|-------------|----------------|

## Correlation Clusters

- AAVE, ADA, ARB, AVAX, BNB, BTC, DOGE, ENA, ETH, FARTCOIN, LINK, NEAR, ORDI, PENGU, PNUT, PUMP, SOL, SUI, TAO, WLD, XRP, kBONK, kPEPE
