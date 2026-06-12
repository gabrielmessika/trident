# Annexe 04 - Baselines et rapports de replay TRIDENT

Date de generation: 2026-06-11

Cette annexe rend explicite la baseline de replay a utiliser pour comparer les
ameliorations TRIDENT A/C. Elle est necessaire parce que l'outil d'audit externe
n'aura pas acces au repo ni au plan actif.

## 1. Verdict court

Le pack d'audit doit traiter la baseline suivante comme reference officielle
prod-compatible courante:

- Rapport Markdown: `baseline_official_current_cli_20260513.md`
- Rapport machine: `baseline_official_current_cli_20260513.json`
- Statut/reference: `baseline_reference_status_20260513.md`

Cette baseline correspond aux fichiers repo originaux:

- `server-data/replay_reports/official_baseline_current_cli_20260513.md`
- `server-data/replay_reports/official_baseline_current_cli_20260513.json`
- `server-data/replay_reports/BACKTEST_REFERENCE_STATUS_20260513.md`

Regle d'audit:

- Toute nouvelle regle Pod A/C doit etre comparee contre cette baseline full-bot
  compatible prod, pas seulement contre un runner isole.
- Un resultat positif standalone ne suffit pas si le full-bot degrade la
  baseline officielle.
- Les rapports de replay doivent rester datees; ne pas ecraser la baseline
  officielle sans demande explicite.

## 2. Baseline officielle courante

Config:

- `config/trident.toml`

Input:

- `server-data/replay_inputs/external_reference_multisource_20260405_20260513_baseline.jsonl`

Fenetre:

- Start: `2026-04-05T19:45:00Z`
- End: `2026-05-13T07:56:49Z`
- Dates couvertes: 2026-04-05 -> 2026-05-13, avec trous de collecte connus.

Trous de collecte notes dans le plan actif:

- `2026-04-19`
- `2026-04-28`
- `2026-04-29`
- `2026-05-09 -> 2026-05-11`

Metriques principales:

| Metric | Valeur |
| --- | ---: |
| Records processed | 40632 |
| Duplicate timestamps skipped | 301 |
| Total realized PnL | +859.83 USD |
| Directional fees | 133.182844 USD |
| Total closed activity | 196 |
| Routing reassignment events | 0 |
| Max ownership conflict count | 0 |

PnL par pod:

| Pod | Realized PnL | Closed trades | Notes |
| --- | ---: | ---: | --- |
| Pod A | +780.72 USD | 155 | Crypto core, `trend_pullback_long`, A-grade actif. |
| Pod B | 0.00 USD | 0 | HIP4 independant, ne contribue pas a TRIDENT A/C. |
| Pod C | +79.11 USD | 41 | TradFi directionnel, logique baseline inchangee. |

## 3. Reference avant promotion evo11

Le plan actif conserve le resultat de reference avant promotion
`evo11_a_grade_boost_wider_exits`:

| Total | Pod A | Pod B | Pod C |
| ---: | ---: | ---: | ---: |
| +669.69 USD | +590.58 | 0.00 | +79.11 |

Comparaison des variantes Pod A:

| Variant | Total PnL | Delta total | Pod A PnL | Delta Pod A | Pod A trades | Fees |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline | 669.69 | 0.00 | 590.58 | 0.00 | 159 | 113.58 |
| evo8_a_grade_boost | 825.48 | 155.79 | 746.37 | 155.79 | 159 | 137.79 |
| evo9_wider_winner_exits | 693.04 | 23.35 | 613.93 | 23.35 | 155 | 109.99 |
| evo10_context_guardrail | 599.32 | -70.37 | 520.21 | -70.37 | 144 | 102.22 |
| evo11_a_grade_boost_wider_exits | 859.83 | 190.14 | 780.72 | 190.14 | 155 | 133.18 |

Rapports a joindre:

- `baseline_pod_a_evo11_comparison_20260513.md`
- `baseline_pod_a_evo11_promoted_20260513.md`

Interpretation:

- `evo11_a_grade_boost_wider_exits` est la variante promue et devient la
  baseline officielle courante.
- Les variantes `evo1/evo2/evo3/evo4/evo10` restent non promues selon le plan
  actif; une nouvelle proposition doit battre la baseline full-bot, pas une
  variante isolee.

## 4. Rejeu avec repo/config courants du 2026-05-19

Le plan actif note un replay du meme input avec le repo/config courants le
2026-05-19:

| Total | Pod A | Pod B | Pod C |
| ---: | ---: | ---: | ---: |
| +872.74 USD | +793.63 | 0.00 | +79.11 |

Interpretation:

- La baseline officielle archivee reste `+859.83 USD`.
- Le replay courant du meme JSONL sort `+872.74 USD`, soit `+12.91 USD`.
- L'ecart vient uniquement de `6` trades HYPE `trend_pullback_long` Pod A
  reintroduits par rollback du veto HYPE.
- Pod C reste strictement inchange a `+79.11 USD`.

Regle:

- Utiliser `+859.83 USD` comme reference officielle sauf si la question porte
  explicitement sur le replay post-rollback HYPE du 2026-05-19.

## 5. Pod C off

Rapport joint:

- `baseline_no_pod_c_20260513.md`

Resultat:

| Total | Pod A | Pod B | Pod C | Closed trades |
| ---: | ---: | ---: | ---: | ---: |
| +780.72 USD | +780.72 | 0.00 | 0.00 | 155 |

Interpretation:

- Pod A est strictement identique a la baseline officielle avec Pod C actif:
  `155` trades, `+780.72 USD`.
- Couper Pod C retirerait simplement sa contribution positive `+79.11 USD`.
- Le replay ne montre pas de conflit full-bot Pod C -> Pod A sur cette fenetre.

## 6. Pod C multiplier replays

Rapports joints:

- `baseline_pod_c_cluster_multiplier_global_20260526.md`
- `baseline_pod_c_cluster_multiplier_global_20260526.json`
- `baseline_pod_c_cluster_multiplier_recent_20260526.md`
- `baseline_pod_c_cluster_multiplier_recent_20260526.json`

Fenetre globale:

| Scenario | Realized PnL | Trades | Fees | Note |
| --- | ---: | ---: | ---: | --- |
| baseline_055 | +79.11 | 41 | 22.662696 | Baseline Pod C officielle. |
| global_065 | +94.92 | 42 | 27.253842 | Plus de PnL, activite proche. |
| global_070 | +105.56 | 68 | 36.559414 | Plus de PnL mais forte hausse activite/fees. |
| silver_070 | +67.90 | 61 | 31.193508 | Rejete: degrade global, silver plus actif. |
| gold_070 | +86.07 | 41 | 23.604784 | Levier le plus propre observe. |
| metals_070 | +74.86 | 61 | 32.135596 | Rejete: degrade global. |

Fenetre recente `2026-05-24 -> 2026-05-26`:

| Scenario | Realized PnL | Trades | Fees | Note |
| --- | ---: | ---: | ---: | --- |
| baseline_055 | -14.78 | 4 | 2.021250 | Baseline recente faible sample. |
| global_065 | -17.47 | 4 | 2.388750 | Degrade la fenetre recente. |
| global_070 | -11.12 | 6 | 3.234000 | Moins mauvais recent, plus actif. |
| silver_070 | -10.75 | 5 | 2.625000 | Mieux recent, mais rejet global. |
| gold_070 | -14.78 | 4 | 2.021250 | Aucun changement recent. |
| metals_070 | -10.75 | 5 | 2.625000 | Mieux recent, mais rejet global. |

Interpretation:

- `global_070` a ete utilise comme canary live explicite selon le plan actif,
  mais l'auditeur doit retenir que cela augmente fortement l'activite.
- `silver_070` et `metals_070` sont rejetes malgre un mieux recent, car ils
  degradent la fenetre globale.
- `gold_070` est le levier le plus propre observe, mais ne doit pas etre promu
  sans nouveau contexte si le plan actif ne l'a pas fait.

## 7. Limites pour l'auditeur externe

Ce pack joint les rapports et resultats de baseline, mais pas l'input complet de
replay:

- `server-data/replay_inputs/external_reference_multisource_20260405_20260513_baseline.jsonl`

Consequences:

- L'auditeur peut comparer une proposition aux resultats officiels.
- L'auditeur ne peut pas rerun la baseline sans recevoir l'input JSONL et le
  code.
- Toute conclusion de surperformance doit indiquer si elle compare contre:
  - la baseline officielle `+859.83 USD`;
  - le replay repo/config courant du 2026-05-19 `+872.74 USD`;
  - une fenetre recente differente;
  - un runner standalone non comparable.

Verdict d'audit attendu:

- Si une proposition ne bat pas la baseline full-bot pertinente, elle reste
  `research_only` ou `rejected`.
- Si elle bat seulement un replay standalone, elle reste `needs_full_bot_replay`.
- Si elle bat le full-bot mais augmente fortement activite, fees ou drawdown,
  elle doit etre classee `needs_risk_review`.
