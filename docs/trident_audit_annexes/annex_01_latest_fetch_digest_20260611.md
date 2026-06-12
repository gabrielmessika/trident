# Annexe 01 - Digest du fetch frais 2026-06-11

Date de generation de cette annexe: 2026-06-11

Important:

- Un fetch global a ete lance pendant cette mise a jour.
- L'export compact autonome a ete regenere sous
  `server-data/audit_exports/20260611T135456Z/`.
- Ce digest resume les donnees jointes au pack; l'auditeur externe doit recevoir
  les fichiers exportes eux-memes, pas seulement cette synthese.

## 1. Sources locales utilisees

TRIDENT A/C:

- Source review: `server-data/reviews/20260611T135956Z/review_summary.md`
- `generated_at`: `2026-06-11T13:59:56.577550Z`
- Mode: `live`
- Network: `mainnet`
- Status: `PASS`

TRIDENT-HIP4 run review:

- Source: `server-data/hip4/reviews/20260605T141323Z/hip4_outcome_run_review.md`
- Fenetre reportee: `2026-05-24 -> 2026-06-05`
- Note: le fetch du 2026-06-11 n'a pas regenere cette run review; elle reste
  jointe comme derniere review structuree.

TRIDENT-HIP4 policy market audit:

- Source: `server-data/hip4/replay_reports/hip4_policy_market_audit_latest.md`
- `generated_at`: `2026-06-11T13:51:51.936041Z`

Export compact:

- Source: `server-data/audit_exports/20260611T135456Z/manifest.json`
- `generated_at`: `2026-06-11T14:11:47.397559+00:00`
- `fresh_fetch_run`: `true`
- `contains_secrets`: `false`

## 2. Digest TRIDENT A/C

Statut general:

- Status: `PASS`
- Mode: `live`
- Exchange network: `mainnet`
- Ownership conflict count: `0`

Contexte operateur:

- `live_max_order_notional_usd`: 200.0
- `live_block_stop_grace_setups`: false
- `live_stop_grace_catastrophic_sl_bps`: 300.0
- `pod_a_stop_grace_minutes`: 60
- `pod_c_blocked_symbols`: `['XYZ:SILVER']`
- Pod C silver mode: enabled avec BE 0.9, trailing activation 0.75,
  trailing distance 0.75, mais le symbole silver est bloque.

Checks reportes:

- API `/health` repond OK.
- Pod A healthy dans `/api/report`.
- Pod A `live_trading_paused=false`.
- Pod A reconciliation ready.
- Pod C healthy dans `/api/report`.
- Pod C `live_trading_paused=false`.
- Pod C reconciliation ready.

Performance focus du rapport:

| Pod | runtime_realized_pnl_usd | worst_symbols_runtime | stop actual vs planned |
| --- | ---: | --- | --- |
| Pod A | -5.90 | SOL:-2.14, SUI:-1.99, ZEC:-1.92, DOGE:-1.62, PENGU:-1.14 | count=5, actual=-7.10, planned=-8.06, excess=0.96 |
| Pod C | 0.46 | XYZ:BRENTOIL:-1.41, XYZ:CL:-1.17, XYZ:GOLD:-0.53, XYZ:SP500:1.10, XYZ:XYZ100:2.47 | count=2, actual=-2.58, planned=-2.54, excess=-0.04 |

Worst stop-loss reportes:

| Pod | Symbole | PnL | Planned | Excess | Opened |
| --- | --- | ---: | ---: | ---: | --- |
| Pod A | SOL | -2.51 | -2.6117 | 0.1017 | 2026-06-09T04:37:00+00:00 |
| Pod A | LINK | -0.38 | -0.4853 | 0.1053 | 2026-06-11T04:04:00+00:00 |
| Pod A | BTC | -0.60 | -0.7359 | 0.1359 | 2026-06-11T10:51:00+00:00 |
| Pod C | XYZ:BRENTOIL | -1.41 | -1.3743 | -0.0357 | 2026-06-11T09:09:00+00:00 |
| Pod C | XYZ:CL | -1.17 | -1.1702 | 0.0002 | 2026-06-10T14:59:00+00:00 |

Etat runtime Pod A:

- Process state: `running`
- Position count: 1
- Open order count: 0
- Total fill count: 115
- Realized PnL USD: -134.27
- Total unrealized PnL USD: -0.2166
- Win rate: 0.3565

Etat runtime Pod C:

- Process state: `running`
- Position count: 0
- Open order count: 0
- Total fill count: 23
- Realized PnL USD: -14.21
- Total unrealized PnL USD: 0.0
- Win rate: 0.2609

Interpretation A/C:

- Le statut operationnel A/C est sain au moment du fetch.
- Le focus runtime recent montre Pod A negatif sur la fenetre du log ferme
  courant et Pod C legerement positif.
- Les PnL cumules runtime restent negatifs: Pod A -134.27 USD, Pod C -14.21
  USD.
- L'audit PnL A/C peut maintenant travailler au niveau trade ferme via
  `trident_ac_closed_trades.csv`, mais il ne peut pas encore reconciler chaque
  fermeture avec des fills exchange bruts.

## 3. Donnees A/C exportees

Inventaire export A/C:

| Fichier | Rows / contenu | Role |
| --- | ---: | --- |
| `trident_ac_signal_decisions.jsonl` | 193697 lignes | Decisions, signaux, signal reviews, risk/execution summaries. |
| `trident_ac_fill_events.csv` | 141 lignes data | Fills vus dans les logs; ouvertures seulement. |
| `trident_ac_closed_trades.csv` | 31 lignes data | Trades fermes issus de `closed_trade_log`. |
| `trident_ac_runtime_summary.json` | snapshot | Status Pod A/C et report runtime. |
| `trident_ac_open_positions.json` | snapshot | Positions ouvertes au moment du fetch. |
| `trident_ac_live_state_pod_a.json` | snapshot | State store live Pod A: positions, ordres, evenements. |
| `trident_ac_live_state_pod_c.json` | snapshot | State store live Pod C: positions, ordres, evenements. |

Stats manifest A/C:

- Opened count exporte: Pod A 118, Pod C 23.
- Close fill count exporte: 0.
- Closed trades: Pod A 25, Pod C 6.
- Closed-trade PnL exporte: Pod A -5.90 USD, Pod C +0.46 USD.
- `trident_ac_fill_events.csv` contient les open fills; les close fills ne sont
  pas presents dans les JSONL disponibles.

Closed trades Pod A:

- Count: 25
- PnL ferme exporte: -5.90 USD
- Exit reasons:
  - `early_failure_exit`: 10
  - `trailing_stop`: 7
  - `exchange_closed_stop_loss`: 5
  - `break_even_stop`: 2
  - `stop_hit`: 1
- Symboles les plus frequents:
  - SOL: 4
  - BTC: 3
  - ZEC, BIO, PENGU, LINK, ZRO: 2 chacun

Closed trades Pod C:

- Count: 6
- PnL ferme exporte: +0.46 USD
- Exit reasons:
  - `time_stop`: 3
  - `exchange_closed_stop_loss`: 2
  - `trailing_stop`: 1
- Symboles:
  - XYZ:XYZ100: 2
  - XYZ:CL, XYZ:SP500, XYZ:BRENTOIL, XYZ:GOLD: 1 chacun

Rejets / reviews dominants A/C dans l'export:

- Risk rejects dominants:
  - `setup_not_allowed`: 3409
  - `missing_trade_plan`: 1457
  - `pattern_veto_trend4h_positive_cci_mid`: 242
  - `rolling_guardrail_intraday_setup`: 195
  - `confidence_below_min`: 183
- Signal reviews dominants:
  - `no_continuation_or_reclaim_setup`: 29125
  - `structure too weak for short`: 10521
  - `structure too weak for long`: 7402
  - `structure too weak for long, ema stack not bullish, supertrend against`: 6363
  - `cluster_strategy_not_matched`: 5810

Inspection serveur concernant les infos manquantes:

- Les fichiers distants inspectes sous `/opt/trident/logs`,
  `/opt/trident/runtime` et `/opt/trident/data` contiennent des logs/status et
  live states, mais pas de fichier brut separe de `close_fills`.
- `logs/pod_a_live.jsonl`: signaux 6041, open_fills 118, close_fills 0.
- `logs/pod_c_live.jsonl`: signaux 113, open_fills 23, close_fills 0.
- `runtime/trident/live_state_pod_a.json`: cles `events`, `mode`, `orders`,
  `positions`; 1 position, 17 orders, 2 events.
- `runtime/trident/live_state_pod_c.json`: cles `events`, `mode`, `orders`,
  `positions`; 0 position, 5 orders, 0 event.
- Conclusion: le serveur donne le `closed_trade_log` via les status runtime,
  mais pas les fills de fermeture exchange bruts dans les donnees fetchables
  actuelles.

## 4. Digest TRIDENT-HIP4 run review

Verdict global de la derniere run review structuree:

- Status: `collect_more_data`
- Recommendation: continuer la collecte mainnet paper/mainnet observer avant
  toute promotion.

Blockers reportes:

- Settlements mainnet paper insuffisants: 17/20.
- Profit factor mainnet paper insuffisant: 0.7718/1.15.
- Samples calibration insuffisants: 17/20.
- Brier score insuffisant: 0.2611, seuil attendu <= 0.23.

Profils de la run review 2026-06-05:

| Profile | Window | Opps | Obs | Approved | Trades | Settlements | PnL | PF | Brier | Fill slip |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| mainnet | 2026-05-24 -> 2026-06-05 | 49570 | 903365 | 0 | 0 | 0 | 0.0000 | n/a | n/a | n/a |
| mainnet_paper | 2026-05-24 -> 2026-06-05 | 28827 | 843265 | 18 | 18 | 17 | -31.2696 | 0.7718 | 0.2611 | 0.0001 |
| paper | n/a | 0 | 0 | 0 | 0 | 0 | 0.0000 | n/a | n/a | n/a |
| testnet | n/a | 0 | 0 | 0 | 0 | 0 | 0.0000 | n/a | n/a | n/a |

Interpretation:

- HIP4 mainnet paper n'est pas pret a promotion selon cette run review.
- Le PnL est negatif et la calibration insuffisante.
- Cette review est utile comme reference structurante, mais les donnees
  policy/market plus fraiches du 2026-06-11 doivent etre utilisees pour les
  questions d'exit policy et d'univers.

## 5. Digest HIP4 policy market audit

Policy replay frais:

| Policy | Source | Settlements | Exits | PnL | Delta active | PF | Win rate | Worst | Best |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| prob_stop_full | shadow | 23 | 7 | 189.5755 | 237.4141 | 1.5479 | 0.3913 | -49.8884 | 74.0342 |
| ev_plus_2pct_partial_runner | shadow | 23 | 9 | 160.0827 | 207.9213 | 1.5558 | 0.3913 | -49.7975 | 74.0342 |
| hold_to_settlement | shadow | 23 | 0 | 105.4094 | 153.2479 | 1.2450 | 0.3913 | -49.8884 | 74.0342 |
| active_paper | active | 25 | 23 | -47.8386 | 0.0000 | 0.7083 | 0.4400 | -49.7638 | 31.3643 |
| last_5m_full | shadow | 23 | 0 | 105.4094 | 153.2479 | 1.2450 | 0.3913 | -49.8884 | 74.0342 |
| last_15m_full | shadow | 23 | 1 | 89.2915 | 137.1300 | 1.2076 | 0.3913 | -49.8884 | 74.0342 |
| last_10m_full | shadow | 23 | 1 | 85.7587 | 133.5973 | 1.1994 | 0.3913 | -49.8884 | 74.0342 |
| ev_plus_2pct_full | shadow | 23 | 23 | 17.3830 | 65.2216 | 1.2822 | 0.9130 | -49.7638 | 11.8455 |
| tp_25_partial | shadow | 23 | 12 | -0.2800 | 47.5586 | 0.9991 | 0.3913 | -49.7975 | 43.2515 |
| tp_35_partial | shadow | 23 | 11 | -5.2999 | 42.5386 | 0.9850 | 0.3913 | -49.7975 | 46.6640 |
| tp_50_partial | shadow | 23 | 10 | -7.0675 | 40.7710 | 0.9815 | 0.3913 | -49.7975 | 49.8326 |

Entry cutoff replay:

| Cutoff | Policy | Settlements | PnL | PF | Win rate | Worst | Best |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2026-06-02T00:00:00Z | active_paper | 10 | -29.7507 | 0.2596 | 0.1000 | -11.8060 | 10.4322 |
| 2026-06-02T00:00:00Z | prob_stop_full | 8 | -27.9203 | 0.2720 | 0.1250 | -11.8060 | 10.4322 |
| 2026-06-02T00:00:00Z | ev_plus_2pct_partial_runner | 8 | -60.1173 | 0.1479 | 0.1250 | -11.8262 | 10.4322 |
| 2026-06-02T00:00:00Z | hold_to_settlement | 8 | -71.7213 | 0.1270 | 0.1250 | -11.8899 | 10.4322 |
| 2026-06-05T00:00:00Z | active_paper | 8 | -16.5690 | 0.3864 | 0.1250 | -11.8060 | 10.4322 |
| 2026-06-05T00:00:00Z | prob_stop_full | 6 | -14.7386 | 0.4145 | 0.1667 | -11.8060 | 10.4322 |
| 2026-06-05T00:00:00Z | ev_plus_2pct_partial_runner | 6 | -36.7500 | 0.2211 | 0.1667 | -11.8262 | 10.4322 |
| 2026-06-05T00:00:00Z | hold_to_settlement | 6 | -48.3540 | 0.1775 | 0.1667 | -11.8899 | 10.4322 |

Active exit reasons:

| Reason | Count | Realized PnL |
| --- | ---: | ---: |
| bid_over_conservative_hold_ev | 14 | 40.8992 |
| probability_stop | 7 | -9.8395 |
| full_take_profit | 2 | 40.3604 |

Policy notes:

- `prob_stop_full_delta_vs_active`: 237.4141 USDC.
- `partial_runner_delta_vs_hold`: 54.6734 USDC.
- Les policies shadow sont des contrefactuels paper; ne pas promouvoir sans
  plus de settlements et sans verification post-changement.

Non-BTC priceBinary audit:

| Profile | Opps | Opp underlyings | priceBinary obs | priceBinary underlyings | non-BTC priceBinary | tradable non-BTC | Conclusion |
| --- | ---: | --- | ---: | --- | --- | --- | --- |
| mainnet_paper | 34034 | BTC, ETH, HYPE, SOL | 103877 | BTC, ETH, HYPE, SOL | ETH, HYPE, SOL | ETH, HYPE, SOL | non_btc_price_binary_tradable_candidates_present |
| mainnet_observer | 59588 | BTC, ETH, HYPE, SOL | 109929 | BTC, ETH, HYPE, SOL | ETH, HYPE, SOL | ETH, HYPE, SOL | non_btc_price_binary_tradable_candidates_present |

Target coverage non-BTC:

| Profile | Underlying | Opps | Approved | priceBinary obs | Trading-supported obs |
| --- | --- | ---: | ---: | ---: | ---: |
| mainnet_paper | ETH | 662 | 1 | 883 | 883 |
| mainnet_paper | HYPE | 358 | 1 | 828 | 828 |
| mainnet_paper | SOL | 515 | 1 | 883 | 883 |
| mainnet_observer | ETH | 692 | 0 | 926 | 926 |
| mainnet_observer | HYPE | 437 | 0 | 869 | 869 |
| mainnet_observer | SOL | 543 | 0 | 926 | 926 |

Stats manifest HIP4:

- `hip4_decisions.jsonl`: 93610 lignes.
- Approved mainnet paper: 27.
- Top decision reasons:
  - `mainnet_observer:observer_mode_signal_only`: 40143
  - `mainnet_paper:market_already_open`: 29095
  - `mainnet_observer:shock_guard_adverse_momentum`: 16089
  - `mainnet_paper:shock_guard_adverse_momentum`: 4661
  - `mainnet_observer:insufficient_yes_depth`: 1922
  - `mainnet_observer:insufficient_no_depth`: 1426
  - `mainnet_paper:max_open_markets_reached`: 178
  - `mainnet_paper:local_outcome_risk_ok`: 27

Interpretation HIP4:

- L'ambiguite policy reste l'axe PnL le plus tangible: la config courante
  documentee indique `prob_stop_full`, alors que le replay actif historique
  `active_paper` reste negatif et le contrefactuel `prob_stop_full` est
  fortement positif sur la fenetre.
- Les cutoffs recents restent faibles et negatifs; l'auditeur ne doit pas
  transformer le delta shadow en promotion automatique.
- L'audit non-BTC a change de statut: il existe maintenant des candidats
  priceBinary tradables ETH, HYPE et SOL, avec 1 approbation paper chacun sur la
  fenetre observee.

## 6. Axes detectables immediatement

Axes exploitables avec l'export frais:

- `A/C closed-trade attribution`: analyser `trident_ac_closed_trades.csv` par
  pod, symbole, confidence, exit reason, stop planned vs actual, A-grade et
  external references.
- `A/C close-fill gap`: instrumenter ou exporter les fills exchange de
  fermeture pour transformer l'audit applicatif en reconciliation complete.
- `Pod A early_failure_exit`: 10/25 closed trades Pod A; mesurer contribution
  PnL et verifier si la regle coupe des pertes utiles ou trop de recoveries.
- `Pod A exchange_closed_stop_loss`: 5/25 closed trades Pod A; comparer perte
  reelle vs perte planifiee.
- `Pod C time_stop`: 3/6 closed trades Pod C; verifier si les time stops
  ameliorent ou degradent l'expectancy.
- `HIP4 policy ambiguity`: verifier timestamps de changement, trades
  post-change et status runtime avant toute conclusion.
- `HIP4 market_already_open`: 29095 rejets mainnet paper; auditer comme bruit
  pipeline potentiel ou protection anti-churn.
- `HIP4 non-BTC`: ETH/HYPE/SOL sont maintenant presents comme candidats
  tradables; traiter comme axe de collecte/couverture, pas encore comme edge
  prouve.

## 7. Donnees encore manquantes pour PnL complet

Manquent encore pour un audit PnL exhaustif:

- fills exchange bruts de fermeture A/C;
- reconciliation fill-by-fill entre exchange, state store et closed trades;
- MFE/MAE par trade;
- funding reel par trade;
- slippage decision mid vs fill price de sortie;
- snapshots marche d'entree/sortie si l'auditeur veut tester les exits;
- review HIP4 run regeneree apres le fetch du 2026-06-11;
- replays HIP4 post-changement avec assez de settlements.

Conclusion:

- Le pack est maintenant autonome pour un audit architecture/trading et pour un
  premier audit PnL A/C au niveau trade ferme.
- Il n'est pas encore autonome pour une reconciliation exchange complete des
  sorties A/C.
