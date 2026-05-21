# HIP-4 Outcome Run Review

- Status: `collect_more_data`
- Recommendation: continuer la collecte mainnet paper/mainnet observer avant toute promotion
- Blocker: settlements mainnet paper insuffisants: 8/20
- Blocker: samples calibration insuffisants: 8/20
- Blocker: Brier score insuffisant: 0.2585 <= 0.23 attendu

## Profiles

| Profile | Window | Opps | Obs | Approved | Trades | Settlements | PnL | PF | Brier | Fill slip |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| mainnet | 2026-05-03 -> 2026-05-13 | 79484 | 328876 | 0 | 0 | 0 | 0.0000 | n/a | n/a | n/a |
| mainnet_paper | 2026-05-13 -> 2026-05-21 | 57346 | 380101 | 8 | 8 | 8 | 72.6877 | 1.3653 | 0.2585 | 0.0000 |
| paper | 2026-05-01 -> 2026-05-02 | 18076 | 0 | 72 | 72 | 70 | 166.4800 | 4.7974 | 0.1270 | 0.0000 |
| testnet | 2026-05-02 -> 2026-05-13 | 54741 | 557205 | 4372 | 298 | 298 | -541.9302 | 0.8398 | 0.4172 | -0.0172 |

## Cross Profile Opportunities

| Underlying | Edge | Side | mainnet | mainnet_paper | paper | testnet |
|---|---|---|---:|---:|---:|---:|
| BTC | MODEL | BUY_NO | 68552 | 26608 | 319 | 6228 |
| BTC | MODEL | BUY_YES | 10589 | 30630 | 3343 | 4685 |
| HYPE | LATE_EXPIRY | BUY_YES | 0 | 0 | 3166 | 11236 |
| HYPE | MODEL | BUY_YES | 0 | 0 | 3000 | 10610 |
| HYPE | LATE_EXPIRY | BUY_NO | 0 | 0 | 3053 | 9019 |
| HYPE | MODEL | BUY_NO | 0 | 0 | 2999 | 6570 |
| HYPE | SHORT_EXPIRY | BUY_YES | 0 | 0 | 980 | 3498 |
| HYPE | SHORT_EXPIRY | BUY_NO | 0 | 0 | 1013 | 2430 |
| BTC | LATE_EXPIRY | BUY_YES | 174 | 21 | 203 | 103 |
| BTC | LATE_EXPIRY | BUY_NO | 75 | 0 | 0 | 360 |
| BTC | SHORT_EXPIRY | BUY_NO | 62 | 59 | 0 | 0 |
| BTC | SHORT_EXPIRY | BUY_YES | 32 | 26 | 0 | 0 |

## mainnet Details

- Readiness: `ok`

### Market Observations

- Total: `328876`; books logged: `245193`
- namedOutcome: `183894` watch-only

| Class | Support | Count | Books |
|---|---|---:|---:|
| namedOutcome | observe_only | 183894 | 183894 |
| priceBinary | trading_supported | 83683 | 0 |
| fallback | observe_only | 61299 | 61299 |

| Support reason | Count |
|---|---:|
| named_outcome_observation_only | 183894 |
| price_binary_supported | 83683 |
| fallback_outcome_observation_only | 61299 |

### Decision Rejects

| Reason | Count |
|---|---:|
| observer_mode_signal_only | 74882 |
| insufficient_no_depth | 4132 |
| insufficient_yes_depth | 470 |

## mainnet_paper Details

- Readiness: `collect_more_data`
- Reason: settlements mainnet paper insuffisants: 8/20
- Reason: samples calibration insuffisants: 8/20
- Reason: Brier score insuffisant: 0.2585 <= 0.23 attendu

### Market Observations

- Total: `380101`; books logged: `304080`
- namedOutcome: `228059` watch-only

| Class | Support | Count | Books |
|---|---|---:|---:|
| namedOutcome | observe_only | 228059 | 228059 |
| fallback | observe_only | 76021 | 76021 |
| priceBinary | trading_supported | 76021 | 0 |

| Support reason | Count |
|---|---:|
| named_outcome_observation_only | 228059 |
| fallback_outcome_observation_only | 76021 |
| price_binary_supported | 76021 |

### Loss Review

| Category | Count | PnL |
|---|---:|---:|
| unclassified_loss | 4 | -198.9706 |

### Guardrail Candidates

| Candidate | Kind | Verdict | Excluded | Excluded PnL | PnL after | PF after | Brier after | Note |
|---|---|---|---:|---:|---:|---:|---:|---|
| edge_decay_or_stale_book_context | feature | watch | 1 | 67.9503 | 4.7374 | 1.0238 | 0.2529 | sample faible; garder comme signal, pas comme regle |
| loss_category:unclassified_loss | post_trade_loss_category | park | 4 | -198.9706 | 271.6583 | n/a | 0.2813 | trop peu de trades restants apres exclusion |
| slice:BTC:MODEL:BUY_NO | slice | park | 6 | 54.4134 | 18.2743 | 1.3679 | 0.2670 | trop peu de trades restants apres exclusion |
| edge_type:MODEL | slice | park | 8 | 72.6877 | 0.0000 | n/a | n/a | trop peu de trades restants apres exclusion |

### Calibration

| Slice | Count | Avg pred | Win rate | Brier | Log loss | PnL |
|---|---:|---:|---:|---:|---:|---:|
| MODEL | 8 | 0.4777 | 0.5000 | 0.2585 | 0.7102 | 72.6877 |

### Decision Rejects

| Reason | Count |
|---|---:|
| market_already_open | 57336 |
| below_exchange_min_order_value_named_basket | 2 |

## paper Details

- Readiness: `ok`

### Loss Review

| Category | Count | PnL |
|---|---:|---:|
| late_expiry_reversal | 8 | -38.8700 |
| edge_decayed_or_stale_book | 1 | -4.9700 |

### Guardrail Candidates

| Candidate | Kind | Verdict | Excluded | Excluded PnL | PnL after | PF after | Brier after | Note |
|---|---|---|---:|---:|---:|---:|---:|---|
| loss_category:late_expiry_reversal | post_trade_loss_category | watch | 8 | -38.8700 | 205.3500 | 42.3179 | 0.0839 | diagnostic post-trade; deriver un predicat entry-time avant promotion |
| loss_category:edge_decayed_or_stale_book | post_trade_loss_category | watch | 1 | -4.9700 | 171.4500 | 5.4109 | 0.1218 | sample faible; garder comme signal, pas comme regle |
| edge_type:MODEL | slice | watch | 2 | 0.2700 | 166.2100 | 5.2749 | 0.1288 | sample faible; garder comme signal, pas comme regle |
| late_or_short_expiry_context | feature | park | 69 | 161.2500 | 5.2300 | n/a | 0.0078 | trop peu de trades restants apres exclusion |
| edge_type:LATE_EXPIRY | slice | park | 68 | 166.2100 | 0.2700 | 1.0544 | 0.0631 | trop peu de trades restants apres exclusion |
| edge_decay_or_stale_book_context | feature | kill | 41 | 136.4600 | 30.0200 | 1.7723 | 0.1830 | exclusion non additive sur cette fenetre |

### Calibration

| Slice | Count | Avg pred | Win rate | Brier | Log loss | PnL |
|---|---:|---:|---:|---:|---:|---:|
| LATE_EXPIRY | 68 | 0.7194 | 0.8824 | 0.1288 | 0.4358 | 166.2100 |
| MODEL | 2 | 0.6278 | 0.5000 | 0.0631 | 0.2571 | 0.2700 |

### Decision Rejects

| Reason | Count |
|---|---:|
| market_already_open | 18004 |

## testnet Details

- Readiness: `collect_more_data`
- Reason: profit factor testnet insuffisant: 0.8398/1.15
- Reason: Brier score insuffisant: 0.4172 <= 0.23 attendu

### Market Observations

- Total: `557205`; books logged: `475087`
- namedOutcome: `214229` watch-only

| Class | Support | Count | Books |
|---|---|---:|---:|
| namedOutcome | observe_only | 214229 | 214218 |
| unknown | observe_only | 189458 | 189458 |
| priceBinary | trading_supported | 82107 | 0 |
| fallback | observe_only | 71411 | 71411 |

| Support reason | Count |
|---|---:|
| named_outcome_observation_only | 214229 |
| unsupported_outcome_class | 189458 |
| price_binary_supported | 82107 |
| fallback_outcome_observation_only | 71411 |

### Loss Review

| Category | Count | PnL |
|---|---:|---:|
| reference_divergence | 185 | -3333.2700 |
| unclassified_loss | 2 | -26.7400 |
| edge_decayed_or_stale_book | 1 | -23.1800 |

### Guardrail Candidates

| Candidate | Kind | Verdict | Excluded | Excluded PnL | PnL after | PF after | Brier after | Note |
|---|---|---|---:|---:|---:|---:|---:|---|
| loss_category:reference_divergence | post_trade_loss_category | watch | 185 | -3333.2700 | 2791.3398 | 56.9163 | 0.1492 | diagnostic post-trade; deriver un predicat entry-time avant promotion |
| slice:HYPE:MODEL:BUY_YES | slice | watch | 171 | -2062.8383 | 1520.9081 | 2.6921 | 0.2420 | ameliore une partie des metriques; revalider sur prochaine fenetre |
| edge_type:MODEL | slice | watch | 217 | -784.4627 | 242.5326 | 1.2927 | 0.2381 | ameliore une partie des metriques; revalider sur prochaine fenetre |
| slice:HYPE:LATE_EXPIRY:BUY_YES | slice | watch | 36 | -619.2454 | 77.3153 | 1.0296 | 0.4168 | ameliore une partie des metriques; revalider sur prochaine fenetre |
| edge_decay_or_stale_book_context | feature | watch | 116 | -399.0674 | -142.8628 | 0.9265 | 0.3769 | ameliore une partie des metriques; revalider sur prochaine fenetre |
| loss_category:unclassified_loss | post_trade_loss_category | watch | 2 | -26.7400 | -515.1902 | 0.8465 | 0.4181 | sample faible; garder comme signal, pas comme regle |
| loss_category:edge_decayed_or_stale_book | post_trade_loss_category | watch | 1 | -23.1800 | -518.7502 | 0.8456 | 0.4169 | sample faible; garder comme signal, pas comme regle |
| reference_divergence_context | feature | park | 290 | -655.8122 | 113.8820 | 3.2813 | 0.2532 | trop peu de trades restants apres exclusion |
| late_or_short_expiry_context | feature | park | 292 | -651.9122 | 109.9820 | 5.1130 | 0.2410 | trop peu de trades restants apres exclusion |
| slice:BTC:MODEL:BUY_NO | slice | kill | 4 | 36.4758 | -578.4059 | 0.8277 | 0.4194 | exclusion non additive sur cette fenetre |

### Calibration

| Slice | Count | Avg pred | Win rate | Brier | Log loss | PnL |
|---|---:|---:|---:|---:|---:|---:|
| MODEL | 217 | 0.6861 | 0.2673 | 0.4841 | 2.6580 | -784.4627 |
| LATE_EXPIRY | 81 | 0.7296 | 0.6420 | 0.2381 | 0.6736 | 242.5326 |

### Decision Rejects

| Reason | Count |
|---|---:|
| market_already_open | 26232 |
| blocked_outcome_slice | 8259 |
| reference_divergence_guard | 6797 |
| insufficient_yes_depth | 3542 |
| insufficient_no_depth | 2648 |
| below_exchange_min_order_value_no | 1200 |
| below_exchange_min_order_value_yes | 981 |
| insufficient_testnet_usdc | 266 |
