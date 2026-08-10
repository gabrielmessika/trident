# Figures chartistes - edge research

Statut: `research_only_no_live_change`.

Sources locales utilisees: bougies OHLCV Hyperliquid natives sous `data/` et
`server-data/`, timeframes par defaut `1h` et `4h`.

## Tasse et anse

Rapports sources:

- `server-data/replay_reports/cup_handle_pattern_scan_20260705T204125Z/`
- `server-data/replay_reports/cup_handle_promising_filter_replay_20260706T000000Z/`

### Scan brut

| Segment | Cas | Target theorique atteinte | Target ever | MFE median | Baisse adverse mediane | Retour final median |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1h | 2447 | 53.53% | 83.78% | 5.28% | -1.99% | -1.83% |
| 4h | 1859 | 57.07% | 84.83% | 7.33% | -2.47% | -1.88% |
| all | 4306 | 55.04% | 84.21% | 6.13% | -2.15% | -1.85% |

Lecture: le pattern brut n'est pas suffisant en execution naive. Les targets
partielles sont beaucoup plus exploitables que la target theorique complete.

### Targets partielles

| Target partielle | Hit rate all | Target mediane | Bars median | Baisse mediane | SL 75% winners | SL 90% winners |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 25% | 84.14% | 1.22% | 3 | -1.31% | 3.31% | 6.65% |
| 33% | 80.38% | 1.61% | 4 | -1.46% | 3.61% | 7.05% |
| 50% | 71.90% | 2.44% | 7 | -1.76% | 4.18% | 7.54% |
| 75% | 62.84% | 3.65% | 12 | -2.05% | 4.68% | 8.13% |
| 100% | 55.04% | 4.87% | 16 | -2.15% | 4.83% | 8.16% |

### Filtre le plus prometteur teste

Filtre: `4h_target_low_q33_cup_shallow_q33_breakout_strong_q66_volume_high_q66`.

Seuils retenus sur `4h`:

- `target_pct_from_breakout <= 4.2908`
- `cup_depth_pct <= 5.6018`
- `breakout_margin_pct >= 1.7644`
- `volume_ratio20 >= 1.1706`

Replay conservateur candle-level, split OOS `2026-01-01T00:00:00Z`, intrabar
defavorable: si TP et SL touchent dans la meme bougie, le SL gagne.

| Split | Trades | TP | Stop | Avg exit | Median exit | PF |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| all | 77 | 98.70% | 1.30% | 0.480% | 0.572% | 5.618 |
| train | 49 | 97.96% | 2.04% | 0.386% | 0.517% | 3.361 |
| test | 28 | 100.00% | 0.00% | 0.645% | 0.596% | n/a |

Parametres replay robustes retenus: target `33%` de la target theorique,
SL `8%`. Conclusion: candidat research interessant, mais sample filtre petit
et SL large; pas une promotion live.

## Replay multi-figures

Rapport source:

- `server-data/replay_reports/chart_pattern_skill_replay_20260706T000000Z/`

Commande research-only lancee via le skill `chart-pattern-edge-analysis`:

```bash
rtk .venv/bin/python scripts/run_chart_pattern_skill_replay.py --output-dir server-data/replay_reports/chart_pattern_skill_replay_20260706T000000Z
```

Figures testees en breakout long uniquement:

- `cup_handle`
- `rectangle_breakout`
- `flag_pennant`
- `triangle_breakout`
- `double_bottom`

Hypotheses importantes:

- Tous les detecteurs lisent uniquement les bougies disponibles avant la
  validation du pattern.
- Les targets theoriques utilisent des measured moves classiques.
- Les replays TP/SL sont candle-level et conservateurs: si TP et SL touchent
  dans la meme bougie, le SL gagne.
- Pas de frais, slippage, sizing, gates TRIDENT, liquidite intrabar, ni replay
  full-bot.
- Les variantes short (`double_top`, H&S short, ranges breakdown) ne sont pas
  incluses dans ce run.

### Couverture

| Metric | Valeur |
| --- | ---: |
| Series scannees | 107 |
| Symbols | 55 |
| Bougies uniques | 325017 |
| Cas valides apres dedupe | 12587 |

### Comparatif brut

| Figure | Cas | Target theorique hit | Target ever | Target mediane | MFE median | Baisse adverse mediane | Retour final median |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| double_bottom | 3852 | 63.11% | 84.84% | 4.70% | 7.38% | -2.17% | -1.74% |
| cup_handle | 4306 | 55.06% | 84.23% | 4.87% | 6.14% | -2.15% | -1.85% |
| triangle_breakout | 568 | 48.77% | 77.46% | 10.33% | 9.23% | -2.40% | 0.00% |
| rectangle_breakout | 3609 | 42.78% | 76.48% | 7.52% | 5.88% | -2.92% | -2.28% |
| flag_pennant | 252 | 31.35% | 66.27% | 14.03% | 7.31% | -2.76% | -3.39% |

Lecture: en brut, `double_bottom` est le plus propre. `triangle_breakout` a une
target theorique plus ambitieuse, donc son hit rate brut est plus faible mais
son MFE median est meilleur. `flag_pennant` est trop rare et trop exigeant en
target theorique dans ce detector.

### Targets partielles utiles

| Figure | Target 33% hit | Target 50% hit | Target 100% hit | Target 33% mediane | SL 90% winners sur T33 |
| --- | ---: | ---: | ---: | ---: | ---: |
| double_bottom | 85.64% | 78.95% | 63.11% | 1.55% | 6.72% |
| cup_handle | 80.38% | 71.90% | 55.04% | 1.61% | 7.05% |
| triangle_breakout | 79.58% | 70.95% | 48.77% | 3.41% | 7.59% |
| rectangle_breakout | 72.35% | 63.06% | 42.78% | 2.48% | 8.05% |
| flag_pennant | 64.29% | 54.37% | 31.35% | 4.63% | 8.39% |

Conclusion partielle: pour une logique de trading, la target theorique complete
n'est pas le bon point de depart sauf pour `double_bottom` filtre. Les targets
`33%` a `50%` sont beaucoup plus stables, mais demandent souvent un SL de
`6%` a `8%` pour laisser respirer 90% des winners.

### Replay filtre selectionne par figure

Tous les filtres selectionnes par le harness sont en `4h`. C'est le signal le
plus net du run: le `1h` produit des cas, mais ne gagne pas les selections
robustes train/test.

| Figure | Filtre | Target | SL | Trades | TP | Stop | Avg exit | Train avg | Test trades | Test avg | PF | Verdict |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| triangle_breakout | `4h_target_low_q50_score_high_q50_volume_high_q50` | 75% | 15% | 65 | 83.08% | 6.15% | 3.01% | 2.95% | 26 | 3.10% | 3.38 | Meilleur PnL, sample petit, SL large |
| flag_pennant | `4h_target_low_q33` | 75% | 15% | 41 | 58.54% | 9.76% | 1.94% | 1.31% | 12 | 3.46% | 1.62 | Watch only, sample trop petit |
| rectangle_breakout | `4h_target_low_q33_depth_low_q33_breakout_high_q66_volume_high_q66` | 100% | 15% | 63 | 76.19% | 6.35% | 1.07% | 1.85% | 36 | 0.48% | 1.61 | Positif mais SL trop large |
| double_bottom | `4h_target_low_q50_score_high_q50_volume_high_q50` | 100% | 6% | 319 | 75.55% | 21.94% | 0.62% | 0.74% | 147 | 0.48% | 1.45 | Meilleur compromis sample/SL |
| cup_handle | `4h_target_low_q33_depth_low_q33_breakout_high_q66_volume_high_q66` | 33% | 12% | 79 | 97.47% | 0.00% | 0.45% | 0.44% | 30 | 0.48% | 4.42 | Candidat robuste, preferer retest SL plus bas |

Note tasse/anse: le replay dedie precedent donnait un candidat plus pratique
sur filtre quasi identique avec target `33%`, SL `8%`, `77` trades, avg exit
`0.480%`, test avg `0.645%`, `0%` stop en OOS. Je garde donc `33% / SL 8%`
comme hypothese tasse/anse prioritaire, et le `SL 12%` du harness general comme
borne plus permissive.

### Correlations indicateurs

| Figure | Signal le plus utile | Effet observe |
| --- | --- | --- |
| triangle_breakout | `target_pct_from_entry` corr `-0.321` | Plus la target theorique est proche, meilleur est le hit: bottom tercile `70.37%` vs top `31.75%`. |
| double_bottom | `target_pct_from_entry` corr `-0.245` | Petite structure = meilleure conversion: bottom tercile `75.55%` vs top `49.92%`. |
| rectangle_breakout | `score` corr `0.202`, `breakout_margin_pct` corr `0.178` | Qualite du range et breakout fort ameliorent nettement le hit: score top tercile `54.86%` vs bottom `33.00%`. |
| cup_handle | `target_pct_from_entry` corr `-0.192`, `breakout_margin_pct` corr `0.121` | Petites tasses + breakout fort restent les meilleurs filtres. |
| flag_pennant | `target_pct_from_entry` corr `-0.185` | Le pattern est trop rare; les grands measured moves degradent vite le hit. |

### Classement research

| Rang | Figure | Pourquoi |
| ---: | --- | --- |
| 1 | double_bottom 4h filtre | Meilleur compromis: `319` trades, SL `6%`, OOS encore positif. C'est le candidat le plus testable en full-bot. |
| 2 | triangle_breakout 4h filtre | Meilleur avg exit (`3.01%`) et PF (`3.38`), mais sample `65` et SL `15%`; a retester avec SL `8/10/12%`. |
| 3 | cup_handle 4h filtre | Tres bon taux de TP sur target basse; sample `77-79`, target petite. Bon module additif potentiel si frais/slippage restent faibles. |
| 4 | rectangle_breakout 4h filtre | Positif, mais necessite un SL `15%` dans la selection actuelle. A retravailler sur target partielle avant usage. |
| 5 | flag_pennant 4h filtre | Interessant en OOS mais seulement `41` trades all et `12` OOS; pas assez robuste. |

### Hypotheses de trade a retester

| Figure | Entry | Target initiale | SL initial | Commentaire |
| --- | --- | ---: | ---: | --- |
| double_bottom | Breakout neckline 4h confirme | 100% measured move | 6% | Candidat prioritaire pour replay full-bot/paper. |
| cup_handle | Breakout rim 4h filtre | 33% measured move | 8% | Target modeste mais hit rate tres eleve. |
| triangle_breakout | Breakout trendline 4h filtre | 66-75% measured move | 10-12% a comparer a 15% | Potentiel fort mais SL actuel trop large. |
| rectangle_breakout | Breakout range 4h filtre | 50-100% range height | 8-12% a comparer a 15% | A optimiser avant integration. |
| flag_pennant | Breakout consolidation 4h | 50-75% flagpole | 10-15% | Collect more data / detector a durcir. |

## Conclusion

L'edge brut n'est pas suffisant pour trader toutes les figures telles quelles.
L'edge exploitable apparait surtout en `4h`, avec filtres de qualite:

- petite target theorique relative a l'entree;
- structure pas trop profonde;
- breakout fort;
- volume relatif correct;
- score de pattern au-dessus de la mediane.

Priorite suivante: prendre `double_bottom 4h filtre` et `cup_handle 4h filtre`
comme premiers candidats de replay full-bot/paper, puis refaire un sweep
dedie sur `triangle_breakout` pour chercher un SL moins large que `15%`.

Fichiers generes:

- `chart_pattern_cases.csv`
- `chart_pattern_trade_cases.csv`
- `chart_pattern_target_level_cases.csv`
- `chart_pattern_target_level_summary.csv`
- `chart_pattern_stop_grid_summary.csv`
- `chart_pattern_indicator_correlations.csv`
- `filtered_replay_grid.csv`
- `selected_filter_trades.csv`
- `chart_pattern_report.md`
- `chart_pattern_report.json`

## Replay integre top 3

Rapports sources:

- `server-data/replay_reports/chart_pattern_top3_overlay_20260706T000000Z/`
- `server-data/replay_reports/chart_pattern_top3_overlay_loose_20260706T000000Z/`

Commande principale:

```bash
rtk .venv/bin/python scripts/run_chart_pattern_top3_overlay_replay.py
```

Top 3 integres comme sleeve synthetique:

| Rang | Figure | Filtre | Target | SL |
| ---: | --- | --- | ---: | ---: |
| 1 | `double_bottom` | `4h_target_low_q50_score_high_q50_volume_high_q50` | 100% | 6% |
| 2 | `triangle_breakout` | `4h_target_low_q50_score_high_q50_volume_high_q50` | 75% | 15% |
| 3 | `cup_handle` | `4h_target_low_q33_depth_low_q33_breakout_high_q66_volume_high_q66` | 33% | 8% |

Methode:

- Notional fixe: `200 USD` par trade.
- Replay principal strict: max `4` positions ouvertes overlay, max `2`
  nouvelles positions par bougie 4h, pas de chevauchement sur le meme symbole.
- Sensibilite loose: max `20` positions ouvertes, max `10` nouvelles positions
  par bougie 4h.
- TP/SL conservateur: si TP et SL touchent dans la meme bougie, le SL gagne.
- Comparaison full-bot uniquement sur la fenetre officielle disponible:
  `2026-04-05T19:45:00Z -> 2026-05-13T07:56:49Z`.
- Baseline courante rejouee sur cette fenetre: `+872.74 USD`; baseline
  officielle archivee: `+859.83 USD`.

### Replay strict

| Segment | Candidates | Trades | Skips | TP | Stop | Timeout | Win | Avg ret | PnL | PF | Max DD | Baseline current | Total + overlay | Delta |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Tout historique OHLCV | 463 | 309 | 154 | 237 | 63 | 9 | 77.67% | 0.76% | 470.11 | 1.55 | 78.82 | n/a | n/a | n/a |
| Fenetre baseline | 122 | 61 | 61 | 45 | 13 | 3 | 73.77% | 0.63% | 76.77 | 1.45 | 52.00 | 872.74 | 949.51 | +76.77 |

Breakdown strict:

| Segment | Figure | Trades | PnL | Avg ret | Win |
| --- | --- | ---: | ---: | ---: | ---: |
| Tout historique | `double_bottom` | 237 | 214.70 | 0.45% | 73.42% |
| Tout historique | `triangle_breakout` | 36 | 249.83 | 3.47% | 88.89% |
| Tout historique | `cup_handle` | 36 | 5.57 | 0.08% | 94.44% |
| Fenetre baseline | `double_bottom` | 53 | 58.73 | 0.55% | 71.70% |
| Fenetre baseline | `triangle_breakout` | 4 | 31.09 | 3.89% | 100.00% |
| Fenetre baseline | `cup_handle` | 4 | -13.06 | -1.63% | 75.00% |

### Sensibilite loose

| Segment | Candidates | Trades | Skips | TP | Stop | Timeout | Win | Avg ret | PnL | PF | Max DD | Baseline current | Total + overlay | Delta |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Tout historique OHLCV | 463 | 402 | 61 | 318 | 73 | 11 | 80.10% | 0.87% | 699.15 | 1.69 | 83.96 | n/a | n/a | n/a |
| Fenetre baseline | 122 | 100 | 22 | 76 | 20 | 4 | 76.00% | 0.72% | 143.67 | 1.55 | 49.84 | 872.74 | 1016.41 | +143.67 |

Breakdown loose:

| Segment | Figure | Trades | PnL | Avg ret | Win |
| --- | --- | ---: | ---: | ---: | ---: |
| Tout historique | `double_bottom` | 294 | 343.94 | 0.58% | 75.51% |
| Tout historique | `triangle_breakout` | 53 | 322.46 | 3.04% | 88.68% |
| Tout historique | `cup_handle` | 55 | 32.75 | 0.30% | 96.36% |
| Fenetre baseline | `double_bottom` | 79 | 49.07 | 0.31% | 70.89% |
| Fenetre baseline | `triangle_breakout` | 11 | 97.82 | 4.45% | 100.00% |
| Fenetre baseline | `cup_handle` | 10 | -3.22 | -0.16% | 90.00% |

### Lecture du replay integre

- Le top 3 combine reste positif sur tout l'historique OHLCV et sur la fenetre
  comparable a la baseline full-bot.
- Sur la fenetre officielle, le sleeve strict ajoute `+76.77 USD`, soit total
  `949.51 USD` vs baseline courante `872.74 USD`. La sensibilite loose ajoute
  `+143.67 USD`, soit total `1016.41 USD`.
- `double_bottom` est le coeur robuste du panier: beaucoup de trades, SL `6%`,
  PnL positif en strict et en loose.
- `triangle_breakout` apporte le plus de convexite mais reste sample-limited et
  depend d'un SL `15%`.
- `cup_handle` est tres bon en standalone mais devient faible dans le panier:
  la priorite/capacite le reduit beaucoup et il est negatif sur la fenetre
  baseline. Il faut le garder comme module separe ou le retester avec une
  regle de priorite differente.

Verdict: `double_bottom 4h` merite le premier replay full-bot/paper dedie.
Le panier top 3 est positif en overlay, mais il reste un sleeve synthetique:
il ne modelise pas encore fees, slippage, liquidite, marge, correlation avec
les positions Pod A/C, ni routing TRIDENT.

### Blocage live actuel

Ne pas promouvoir tel quel. Le replay top 3 utilise l'univers OHLCV complet,
alors que la config live bloque plusieurs symboles crypto via
`hyperliquid.tradable_blocked_symbols`. Dans le replay strict, `92/309` trades
acceptes sont sur des symboles actuellement bloques (`TON`, `NEAR`, `ONDO`,
`PENDLE`, `AAVE`, `HYPE`, `VVV`, etc.) et representent `+115.75 USD` de PnL.
Un passage live fidele demanderait donc soit de filtrer ces symboles et
rejouer, soit de les reautoriser explicitement apres review dediee. A ce stade,
le chemin acceptable est: replay top 3 hors symboles bloques, puis paper-only
ou full-bot comparable avant toute activation.

Replay strict relance en excluant la blocklist live courante
(`TAO`, `AAVE`, `ADA`, `AVAX`, `HYPE`, `ICP`, `NEAR`, `ONDO`, `PENDLE`, `TON`,
`VVV`, `XRP`):

| Segment | Candidates | Trades | Skips | TP | Stop | Timeout | Win | Avg ret | PnL | PF | Max DD | Baseline current | Total + overlay | Delta |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Tout historique OHLCV tradable | 315 | 222 | 93 | 177 | 38 | 7 | 80.63% | 1.01% | 448.10 | 1.86 | 74.15 | n/a | n/a | n/a |
| Fenetre baseline tradable | 86 | 48 | 38 | 40 | 5 | 3 | 83.33% | 1.54% | 147.60 | 3.08 | 13.26 | 872.74 | 1020.34 | +147.60 |

Ce resultat renforce `double_bottom 4h` comme candidat prioritaire, mais il
reste un overlay OHLCV additif, pas un vrai chemin d'execution TRIDENT.

## Replay full-bot comparable, evos une par une

Rapport principal:

- `server-data/replay_reports/chart_pattern_fullbot_comparable_official_20260706T000000Z/`

Commandes:

```bash
rtk .venv/bin/python scripts/run_chart_pattern_fullbot_comparable_replay.py --no-live-caps --output-dir server-data/replay_reports/chart_pattern_fullbot_comparable_official_20260706T000000Z
rtk .venv/bin/python app/backtest/full_bot_replay.py --input server-data/replay_inputs/external_reference_multisource_20260405_20260513_baseline.jsonl --report-output tmp/full_bot_baseline_current_code_check_20260706.json --summary-output tmp/full_bot_baseline_current_code_check_20260706.md
```

Methode:

- Etape `paper-only/dormant` zappee comme demande: replay direct full-bot
  comparable, research-only, sans deploy et sans config live.
- Baseline full-bot rejouee dans la meme passe que les overlays.
- Chaque evolution est testee seule avec son executor overlay independant.
- Signal 4h injecte au premier snapshot apres cloture de bougie 4h.
- Symboles bloques live exclus par defaut:
  `TAO`, `AAVE`, `ADA`, `AVAX`, `HYPE`, `ICP`, `NEAR`, `ONDO`, `PENDLE`,
  `TON`, `VVV`, `XRP`.
- Les entrees overlay evitent les symboles deja detenus par le full-bot.

Point de baseline important:

- Le plan actif cite encore le replay `2026-05-19` a `+872.74 USD`, mais la
  commande officielle relancee avec le repo/config courants au `2026-07-06`
  sort `+345.64 USD` (`Pod A +257.25`, `Pod B 0.00`, `Pod C +88.39`,
  `115` trades).
- Le replay chartiste comparable retombe sur cette meme baseline actuelle
  `+345.64 USD`; les deltas ci-dessous sont donc compares au code courant,
  pas a l'artefact historique `+872.74`.

| Evolution | Filtre | Target | SL | Signals | Trades | TP | Autres sorties | PnL overlay | Total full-bot + overlay | Delta | Win | PF | Max DD | Avg MFE | Avg MAE |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `double_bottom 4h` | `target_low_q50 + score_high_q50 + volume_high_q50` | 100% | 6% | 17 | 15 | 9 | 3 routing, 1 stop, 1 time, 1 EOB | +11.59 | 357.23 | +11.59 | 60.00% | 1.36 | 27.77 | 201 bps | -329 bps |
| `triangle_breakout 4h` | `target_low_q50 + score_high_q50 + volume_high_q50` | 75% | 15% | 9 | 7 | 4 | 3 routing | +36.33 | 381.97 | +36.33 | 57.14% | 16.14 | 1.47 | 337 bps | -457 bps |

Lecture:

- `triangle_breakout 4h` est le plus prometteur dans ce replay full-bot:
  faible drawdown, PF eleve, delta `+36.33 USD`. Le sample est toutefois tres
  petit (`7` trades), et le SL `15%` reste trop large pour une promotion live
  sans sweep dedie `8/10/12/15%`.
- `double_bottom 4h` reste positif et plus fourni (`15` trades), mais l'edge
  se comprime fortement dans le chemin full-bot (`+11.59 USD`, PF `1.36`) avec
  un drawdown overlay `27.77 USD`.
- Les deux evos battent la baseline actuelle en additif, mais l'ampleur est
  insuffisante pour justifier un passage live immediat: la prochaine etape
  utile est un replay full-bot sweepant target/SL sur ces deux evos, en gardant
  l'exclusion des symboles bloques live.

## Etapes 1-3 avant live: baseline, sweep, gate

Statut: `research_only_no_live_change`. Aucun deploy, aucune config live, aucun
ordre.

### 1. Reconciliation baseline

Les trois controles utilisent le meme input:
`server-data/replay_inputs/external_reference_multisource_20260405_20260513_baseline.jsonl`
avec `40632` records et `301` timestamps dupliques ignores.

| Baseline | Source | Caps live | Total | Pod A | Pod B | Pod C | Trades |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |
| Historique plan actif `2026-05-19` | `tmp/full_bot_baseline_current_20260519.json` | Non | 872.74 | 793.63 | 0.00 | 79.11 | 202 |
| Runner officiel repo courant `2026-07-06` | `tmp/full_bot_baseline_current_code_check_20260706.json` | Non | 345.64 | 257.25 | 0.00 | 88.39 | 115 |
| Runner comparable pre-live | `chart_pattern_fullbot_sweep_livecaps_v2_20260706T000000Z` | Oui | 77.08 | 56.72 | 0.00 | 20.36 | 133 |

Conclusion baseline:

- L'artefact historique `+872.74` ne doit pas etre utilise comme baseline de
  promotion avec le repo courant: la commande officielle relancee au
  `2026-07-06` sort `+345.64`.
- Pour une decision live, le controle le plus pertinent est le sweep cap-aware
  (`+77.08` baseline), car il applique `live_max_order_notional_usd`.
- Pour une decision recherche/backtest, le controle no-caps (`+345.64`) reste
  conserve comme reference repo courant.

### 2. Sweep full-bot target/SL

Script:

- `scripts/run_chart_pattern_fullbot_sweep.py`

Rapports retenus:

- Pre-live cap-aware:
  `server-data/replay_reports/chart_pattern_fullbot_sweep_livecaps_v2_20260706T000000Z/`
- Controle no-caps:
  `server-data/replay_reports/chart_pattern_fullbot_sweep_nocaps_20260706T000000Z/`

Le premier sweep livecaps `chart_pattern_fullbot_sweep_livecaps_20260706T000000Z`
est rejete comme artefact de harness: les signaux n'etaient pas dupliques sur
tous les profils target/SL d'un meme pattern. Le loader a ete corrige et le
sweep `v2` est la source de decision.

Parametres communs:

- Patterns: `double_bottom 4h`, `triangle_breakout 4h`.
- Filtres: `target_low_q50 + score_high_q50 + volume_high_q50`.
- Grid `double_bottom`: targets `50/75/100%`, SL `4/5/6/8%`.
- Grid `triangle_breakout`: targets `50/66/75%`, SL `8/10/12/15%`.
- Blocklist live exclue:
  `TAO`, `AAVE`, `ADA`, `AVAX`, `HYPE`, `ICP`, `NEAR`, `ONDO`, `PENDLE`,
  `TON`, `VVV`, `XRP`.
- Max `1` position overlay ouverte, max `1` nouvelle position par bougie.

Top profils pre-live cap-aware:

| Rang | Profil | Trades | PnL overlay | Total | Win | PF | Max DD | Verdict |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | `double_bottom` target `100%`, SL `6%` | 8 | 19.14 | 96.22 | 62.50% | 3.69 | 5.93 | Echec sample `<10` |
| 2 | `double_bottom` target `100%`, SL `8%` | 8 | 19.14 | 96.22 | 62.50% | 3.69 | 5.93 | Echec sample `<10` |
| 3 | `triangle_breakout` target `75%`, SL `8/10/12/15%` | 4 | 16.31 | 93.39 | 50.00% | 13.08 | 0.93 | Echec sample `<10` |
| 4 | `triangle_breakout` target `66%`, SL `8/10/12/15%` | 4 | 14.31 | 91.39 | 50.00% | 11.60 | 0.93 | Echec sample `<10` |
| 5 | `double_bottom` target `75%`, SL `6/8%` | 8 | 13.36 | 90.44 | 62.50% | 2.88 | 5.93 | Echec sample `<10` |

Top profils no-caps:

| Rang | Profil | Trades | PnL overlay | Total | Win | PF | Max DD | Verdict |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | `double_bottom` target `100%`, SL `8%` | 7 | 16.47 | 362.11 | 71.43% | 2.68 | 8.60 | Echec sample `<10` |
| 2 | `triangle_breakout` target `75%`, SL `8/10/12/15%` | 4 | 16.31 | 361.95 | 50.00% | 13.08 | 0.93 | Echec sample `<10` |
| 3 | `triangle_breakout` target `66%`, SL `8/10/12/15%` | 4 | 14.31 | 359.95 | 50.00% | 11.60 | 0.93 | Echec sample `<10` |
| 4 | `double_bottom` target `100%`, SL `5%` | 8 | 11.30 | 356.94 | 62.50% | 1.62 | 11.10 | Echec sample `<10` |
| 5 | `double_bottom` target `75%`, SL `4%` | 10 | 1.30 | 346.94 | 60.00% | 1.05 | 18.37 | Echec PF `<1.5` |

### 3. Gate de promotion

Critere applique:

- PnL overlay net positif.
- Au moins `10` trades.
- Profit factor `>= 1.5`.
- Max drawdown overlay `<= 25 USD`.
- Pas de dependance extreme a un seul symbole (`<60%` du PnL positif ou des
  trades).

Resultat:

- Pre-live cap-aware: `0/24` profils passent.
- Controle no-caps: `0/24` profils passent.

Verdict:

- Pas de deploiement live sur ces figures a ce stade.
- Le meilleur candidat a conserver en recherche est `double_bottom 4h`, target
  `100%`, SL `6-8%`, mais son sample tombe a `7-8` trades avec les contraintes
  pre-live.
- `triangle_breakout 4h` garde une convexite interessante, mais seulement
  `4` trades dans le chemin strict; c'est trop fragile pour une promotion.
- Prochaine action utile avant de reparler live: collecte OOS/fetch plus frais
  ou replay sur une fenetre elargie, puis re-run du meme gate. Sans sample
  supplementaire, l'edge reste `research_only_candidate`.

## Override risque accepte et promotion locale

Statut: `risk_accepted_local_config_promoted_no_server_deploy`.

Decision operateur `2026-07-06`: le risque de sample insuffisant est accepte
pour promouvoir les deux meilleurs profils en config locale Pod A. La promotion
active uniquement des signaux `long` sur figures chartistes `4h`, avec
garde-fous conservateurs:

- `pod_a.chart_patterns.enabled=true`.
- `require_first_snapshot_after_4h_close=true`.
- `max_new_signals_per_batch=1`.
- `max_open_positions=1`.
- Setups autorises: `chart_double_bottom_long`,
  `chart_triangle_breakout_long`.
- Profils promus:
  - `double_bottom 4h`: target `100%` du measured move, SL `6%`,
    filtre score/volume/target issu du sweep livecaps.
  - `triangle_breakout 4h`: target `75%` du measured move, SL `8%`,
    filtre score/volume/target issu du sweep livecaps.

Replay integre full-bot cap-aware apres promotion:

- Commande:
  `rtk .venv/bin/python app/backtest/full_bot_replay.py --input server-data/replay_inputs/external_reference_multisource_20260405_20260513_baseline.jsonl --apply-live-notional-caps --report-output server-data/replay_reports/chart_pattern_promoted_livecaps_20260706T000000Z/full_bot_replay.json --summary-output server-data/replay_reports/chart_pattern_promoted_livecaps_20260706T000000Z/full_bot_replay.md`
- Rapport:
  `server-data/replay_reports/chart_pattern_promoted_livecaps_20260706T000000Z/`

Comparaison contre baseline cap-aware:

| Scenario | Total | Pod A | Pod C | Trades A | Trades C | Delta total |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Baseline cap-aware | 77.08 | 56.72 | 20.36 | 109 | 24 | - |
| Promotion chart patterns | 83.99 | 63.63 | 20.36 | 111 | 24 | +6.91 |

Details des trades chartistes fermes:

| Setup | Trades | PnL net | TP hits | Autres clotures | Lecture |
| --- | ---: | ---: | ---: | --- | --- |
| `chart_double_bottom_long` | 2 | +6.91 | 1 | 1 `upgrade_setup` | Effet positif, mais sample tres faible |
| `chart_triangle_breakout_long` | 0 | 0.00 | 0 | - | Configure, mais aucun trade accepte sur cette fenetre |

Trades individuels:

| Open | Close | Symbole | Setup | PnL | Close reason | TP bps | SL bps |
| --- | --- | --- | --- | ---: | --- | ---: | ---: |
| `2026-04-26T20:34:00Z` | `2026-04-27T00:59:00Z` | `ETH` | `chart_double_bottom_long` | +1.36 | `upgrade_setup` | 140.27 | 600 |
| `2026-05-06T00:08:00Z` | `2026-05-06T04:41:20Z` | `BCH` | `chart_double_bottom_long` | +5.55 | `take_profit_hit` | 287.17 | 600 |

Lecture promotion:

- L'override risque est code/config localement, mais le serveur n'est pas
  redeploye dans cette passe.
- Le replay integre officiel est meilleur que la baseline cap-aware, mais
  l'edge observe reste fragile: `+6.91 USD` vient de `2` trades seulement.
- Le profil triangle reste disponible en config comme option convexite, mais il
  n'a pas contribue sur l'historique integre.
- Avant effet live serveur: preflight, deploy explicite, puis fetch/review
  post-deploy avec verification des setups `chart_*` dans les logs.
