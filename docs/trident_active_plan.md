# TRIDENT Active Plan

Date: `2026-04-24`

## Status

- `ACTIVE_SINGLE_SOURCE_OF_TRUTH`
- Ce fichier remplace les anciens plans de pilotage pour les evolutions a venir.
- Les documents historiques restent utiles pour le contexte, mais plus comme feuille de route principale.

## References Actives

### Prod de reference

- config: `config/trident.toml`
- backtest de reference propre:
  - [official_baseline_current_cli_20260423.md](/workspaces/trident/server-data/replay_reports/official_baseline_current_cli_20260423.md)
  - [official_baseline_current_cli_20260423.json](/workspaces/trident/server-data/replay_reports/official_baseline_current_cli_20260423.json)
- resultat:
  - total `+562.48 USD`
  - `Pod A +526.26`
  - `Pod B 0.00`
  - `Pod C +36.22`
- note courante:
  - le profil repo courant inclut `pod_a.stop_grace_minutes = 165`, `pod_a.opposite_signal_debounce_minutes = 15` et la promo `Pod C index routing grace 540m`
  - le profil repo courant inclut maintenant le veto `Pod A / BTC overextension 4h` en dry-run prod
  - le profil repo courant inclut maintenant les vetoes dry-run `Pod A / XRP overextension 4h` et `Pod C / silver strong extension`
  - baseline officielle rerun sur le fetch serveur courant `2026-04-05 -> 2026-04-23`
  - l'input courant ne contient pas de snapshots pour `2026-04-19`
  - delta vs baseline officielle precedente `20260422_pod_c_index540`: `+4.94 USD`

### Shadow agressif

- config: `config/trident_hybrid_moderate_a_shadow.toml`
- backtest shadow de reference:
  - [official_hybrid_moderate_a_shadow_cli_20260419.md](/workspaces/trident/server-data/replay_reports/official_hybrid_moderate_a_shadow_cli_20260419.md)
  - [official_hybrid_moderate_a_shadow_cli_20260419.json](/workspaces/trident/server-data/replay_reports/official_hybrid_moderate_a_shadow_cli_20260419.json)
- resultat:
  - total `+514.55 USD`
  - `Pod A +468.02`
  - `Pod B 0.00`
  - `Pod C +46.53`
- statut:
  - profitable, mais pas encore promu en prod
  - reste sous le baseline officiel courant sur le fetch serveur complet

### Point d'attention operationnel

- le serveur a ete verifie le `2026-04-19`:
  - il tournait encore sur `config/trident_crypto_launch_fast.toml`
  - ce fichier n'existe plus dans le workspace local
  - il doit donc etre considere comme un profil stale a redeployer ou a retirer

## Travaux Fermés Ou Deja Absorbés

- base TRIDENT historique:
  - superviseur, ownership, reporting, replay, deployment hardening
- `Pod A`:
  - `pattern_vetoes` et `pattern_watchers` en place
  - `campaign` et `setup_runner` en place
  - `routing_revoke_grace` et profils prudents valides
- `Pod C`:
  - transfert `Pod A -> Pod C` valide jusqu'a `Phase 6.9`
  - clusters `index`, `silver`, `gold`, `oil` retravailles et promus
- `Pod B` actuel:
  - pattern vetoes utiles identifies
  - mais le pod reste hors chemin critique prod
- spec detaillee de l'ancien "nouveau Pod B":
  - archivee comme document historique

## Validations Recentes

### 2026-04-27 - Regime BTC range et sleeve crypto breadth

Contexte:

- demande: comprendre pourquoi le bot reste faible alors que le marche crypto semble favorable, et verifier si le blocage quand `BTC` est en range est trop restrictif
- source recente:
  - snapshots serveur `2026-04-24 -> 2026-04-27`, fetch `--days 4`
- validation large:
  - [full_bot_latest_fetch.jsonl](/workspaces/trident/server-data/replay_inputs/full_bot_latest_fetch.jsonl)
  - fenetre couverte: `2026-04-05T19:45:00Z -> 2026-04-27T07:59:00Z`
- mode de lecture:
  - `Pod B` garde son statut desactive
  - les tests de sleeves sont des contre-factuels research, pas des promos config

Constat marche:

- la fenetre recente n'est pas uniformement haussiere:
  - `2026-04-26`: panier crypto moyen `+0.99%`, `17/20` coins positives
  - `2026-04-27` matin: panier crypto moyen `-1.82%`, `1/20` coin positive
- le bot a surtout ouvert tard le `2026-04-26` et tot le `2026-04-27`, juste avant le fade

Tests recents `Pod A`:

| Variante | PnL net | Trades clos | Lecture |
|---|---:|---:|---|
| baseline live | `-40.05` | `13` | faible mais moins mauvais |
| `hybrid_moderate_a` | `-142.98` | `26` | plus d'activite, faux positifs |
| sleeve `RangeAuction` strict | `-69.87` | `19` | plus de bruit et de frais |
| sleeve `RangeAuction + DeadZone` strict | `-213.45` | `33` | rejet |
| sleeve loose | `-213.23` | `34` | rejet |

Validation full replay:

| Variante | Total full bot | Pod A | Pod C | Trades clos | Decision |
|---|---:|---:|---:|---:|---|
| baseline live | `+649.81` | `+613.67` | `+36.14` | `169` | garder reference |
| `hybrid_moderate_a` | `+295.55` | `+250.53` | `+45.02` | `253` | ne pas promouvoir |

Decision:

- ne pas activer `crypto_v2_enabled` / `hybrid_moderate_a` en prod sur cette base
- ne pas relacher globalement `RangeAuction` ou `DeadZone` pour `Pod A`
- garder `BTC range` comme throttle de risque plutot que comme hypothese a supprimer brutalement
- l'idee reste vivante sous une forme plus ciblee:
  - creer un candidat `alt_breadth_continuation`
  - shadow-only
  - sizing minuscule
  - pas un changement de regime global
  - conditions minimales a tester:
    - breadth et alt participation fortes
    - symbole local bullish
    - anti-late-entry / anti-FOMO
    - filtre de frais et liquidite
    - validation obligatoire sur full replay avant toute promotion

### 2026-04-27 - Validation MTF Pod A

Contexte:

- question utilisateur: verifier si `Pod A` exploite vraiment le multi-timeframe et chercher des patterns par coin/timeframe sur les trades recents
- artefacts:
  - recap trades/bougies: [mtf_trade_candle_recap_20260427.csv](/workspaces/trident/server-data/replay_reports/mtf_trade_candle_recap_20260427.csv)
  - recap patterns: [mtf_trade_pattern_recap_20260427.md](/workspaces/trident/server-data/replay_reports/mtf_trade_pattern_recap_20260427.md)
  - validation full replay MTF: [pod_a_mtf_candidate_validation_20260427.md](/workspaces/trident/server-data/replay_reports/pod_a_mtf_candidate_validation_20260427.md)

Constats:

- avant cette validation, `Pod A` faisait deja du MTF intraday (`15m`, `1h`, `4h`) via `CandleService`, mais pas de gate arbitraire type `EMA50 1h + RSI 1D`
- la bougie `1D` reste offline/research seulement sur cette source; pas de promotion daily sans historique plus long
- les meilleurs patterns candidats sont des filtres locaux au coin, pas un relachement global du regime BTC:
  - faiblesse 4h: `prev_rsi14_4h <= 40` ou close 4h precedent sous EMA50
  - chop 1h: EMA20 1h sous EMA50 1h et RSI14 1h entre `40` et `50`
  - chase 1h: RSI14 1h >= `70` et entree deja `+50 bps` au-dessus de l'open 1h courant

Validation full replay comparable, `Pod B` desactive, replay routing final saute pour accelerer:

| Variante | Total | Delta | Pod A | Trades Pod A | Decision |
|---|---:|---:|---:|---:|---|
| baseline comparable | `+645.68` | `+0.00` | `+613.67` | `144` | reference de ce harnais |
| `mtf_4h_weakness_veto` | `+726.21` | `+80.53` | `+694.20` | `133` | favorable |
| `mtf_1h_chop_veto` | `+730.88` | `+85.20` | `+698.87` | `135` | favorable |
| `mtf_1h_overextension_veto` | `+677.76` | `+32.08` | `+645.75` | `139` | favorable mais plus faible |
| `mtf_1h_overextension_throttle_50pct` | `+667.43` | `+21.75` | `+635.42` | `144` | moins bon que veto |
| combo 3 vetoes | `+749.20` | `+103.52` | `+717.19` | `125` | promouvoir |

Decision:

- promouvoir le combo en `pod_a.pattern_vetoes` dans [config/trident.toml](/workspaces/trident/config/trident.toml)
- ne pas promouvoir le throttle overextension: il est positif mais moins bon que le veto dur
- garder `Pod B` desactive comme prevu
- confirmation post-promotion avec la config principale:
  - total `+749.20`
  - `Pod A` `+717.19`, `125` trades clos
  - `Pod C` `+32.01`, `26` trades clos
  - `Pod B` `0.00`

Point important:

- le probleme n'est pas un bug evident de calcul de regime: le legacy est coherent avec ses seuils, mais trop BTC-led pour capturer des derives haussieres lentes
- la solution ne doit pas etre un abaissement global des seuils, qui augmente surtout le churn

### 2026-04-24 - Pod C sweep patterns tradfi

Contexte:

- demande: repasser sur les coins `Pod C` (`GOLD`, `OIL`, `SILVER`, `index`, `equity`, `fx`) avec la meme logique de detection de patterns, tester les candidats un par un, garder uniquement les deltas positifs
- source replay comparable:
  - [full_bot_latest_fetch.jsonl](/workspaces/trident/server-data/replay_inputs/full_bot_latest_fetch.jsonl)
- rapports:
  - detection Pod C isolee:
    - [pod_c_day_by_day_patterns_current_20260424.md](/workspaces/trident/server-data/replay_reports/pod_c_day_by_day_patterns_current_20260424.md)
    - [pod_c_day_by_day_patterns_current_20260424.json](/workspaces/trident/server-data/replay_reports/pod_c_day_by_day_patterns_current_20260424.json)
  - validation full replay un par un:
    - [pod_c_pattern_implementation_validation_20260424.md](/workspaces/trident/server-data/replay_reports/pod_c_pattern_implementation_validation_20260424.md)
    - [pod_c_pattern_implementation_validation_20260424.json](/workspaces/trident/server-data/replay_reports/pod_c_pattern_implementation_validation_20260424.json)
  - probe research-only sans `cluster_aware_v2`:
    - [pod_c_relaxed_cluster_probe_20260424.md](/workspaces/trident/server-data/replay_reports/pod_c_relaxed_cluster_probe_20260424.md)
    - [pod_c_relaxed_cluster_probe_20260424.json](/workspaces/trident/server-data/replay_reports/pod_c_relaxed_cluster_probe_20260424.json)

Baseline full replay avant cette passe:

- total `+603.09 USD`
- `Pod A +566.87`
- `Pod B 0.00`
- `Pod C +36.22`

Resultat apres candidat conserve:

- total `+608.05 USD`
- `Pod A +566.87`
- `Pod B 0.00`
- `Pod C +41.18`
- delta: `+4.96 USD`

Tableau de synthese:

| Coin | Cluster | Pattern le plus interessant | Test full replay | Decision |
|---|---|---|---:|---|
| `XYZ:CL` | `oil` | `oil_pullback_long` | `+8.47 / 5 trades` | garder branche actuelle, rien a ajouter |
| `XYZ:BRENTOIL` | `oil` | `oil_pullback_long` | `+2.04 / 1 trade` | garder branche actuelle, rien a ajouter |
| `XYZ:SP500` | `index` | `index_breakout_long` | `+3.04 / 1 trade` | garder branche actuelle, rien a ajouter |
| `XYZ:XYZ100` | `index` | `index_breakout_long` | `+6.42 / 2 trades` | garder branche actuelle, rien a ajouter |
| `XYZ:SILVER` | `silver` | `silver_breakout_long`, mais veto `strong/strong/extension` | `+19.01 / 13 trades`, veto `+4.96` | promouvoir `pod_c.pattern_vetoes.silver_strong_extension_veto` |
| `XYZ:GOLD` | `gold` | `gold_breakout_long` fragile sur le full replay courant | `-2.76 / 1 trade` | pas de veto promu: les sous-patterns gold detectes en runner isole ne declenchent pas en full replay |
| `XYZ:JPY` | `fx` | aucun pattern actif, aucun trade dans le probe relaxe | `0 trade` | non couvert par `cluster_aware_v2`, pas de promo sans nouvelle branche dediee |
| `XYZ:TSLA` | `equity` | probe relaxe negatif | `-5.43 / 6 trades` | ne pas ajouter de branche equity |
| `XYZ:NVDA` | `equity` | aucun pattern actif, aucun trade dans le probe relaxe | `0 trade` | ne pas ajouter de branche equity |
| `XYZ:CRCL` | `equity` | aucun pattern actif, aucun trade dans le probe relaxe | `0 trade` | ne pas ajouter de branche equity |

Probe complementaire:

- desactivation research-only de `cluster_aware_v2`: `-138.11 USD` sur `158` trades
- lecture:
  - confirme que les branches cluster-aware actuelles sont utiles pour couper le bruit
  - `equity` sort negatif (`TSLA -5.43 / 6 trades`)
  - `fx` ne donne aucun trade exploitable sur la fenetre

Changement promu en dry-run:

- `config/trident.toml`
  - `[[pod_c.pattern_vetoes]] silver_strong_extension_veto`
  - scope: `tradfi_continuation_long`, `silver`, `trend_bucket=strong`, `structure_bucket=strong`, `vwap_bucket=extension`
- couverture test:
  - `tests/test_pod_c.py::PodCTests::test_default_config_blocks_pod_c_silver_strong_extension_veto`

Avant passage live:

- refaire un full replay propre avec la config fichier apres promo si le fetch serveur change
- verifier que le nombre de rejets `pattern_veto_silver_strong_extension_veto` reste faible et cible (`6` sur cette fenetre)
- verifier que `XYZ:SILVER` continue de trader les entrees `medium/strong` rentables et n'est pas coupe globalement
- garder `GOLD`, `equity`, `fx` en observation: aucune branche nouvelle n'est validee par le full replay courant

### 2026-04-21 - Pod A `stop_grace` sur snapshots serveur comparables

Contexte:

- le replay complet aligne serveur / code fixe tourne sur:
  - [stop_grace_solutions_20260421/baseline_current.md](/workspaces/trident/server-data/replay_reports/stop_grace_solutions_20260421/baseline_current.md)
  - [stop_grace_solutions_20260421/baseline_current.json](/workspaces/trident/server-data/replay_reports/stop_grace_solutions_20260421/baseline_current.json)
  - baseline recente sur fetch courant apres fix Pod A:
    - total `+421.10 USD`
    - `Pod A +399.67`
    - `Pod B 0.00`
    - `Pod C +21.43`

Sweep initial:

- artefacts:
  - [stop_grace_solutions_20260421/scenario_summary.md](/workspaces/trident/server-data/replay_reports/stop_grace_solutions_20260421/scenario_summary.md)
- resultat cle:
  - `30m allpods`: `+18.30 USD`
  - `60m allpods`: `+27.49 USD`
  - `90m allpods`: `-0.29 USD`
  - `120m allpods`: `+96.20 USD`
  - `180m allpods`: `+124.62 USD`
  - `120m pod_a_only`: `+95.60 USD`
  - `120m pod_a_trend_only`: `+95.60 USD`
- lecture retenue:
  - le gain ne vient pas d'un effet portefeuille global
  - l'effet utile est quasi entierement concentre sur `Pod A / trend_pullback_long`

Raffinement cible `Pod A / trend_pullback_long`:

- artefacts:
  - [stop_grace_refine_20260421/scenario_summary.md](/workspaces/trident/server-data/replay_reports/stop_grace_refine_20260421/scenario_summary.md)
  - [stop_grace_refine_20260421/daily_delta_vs_baseline.csv](/workspaces/trident/server-data/replay_reports/stop_grace_refine_20260421/daily_delta_vs_baseline.csv)
- candidats testes:
  - `105m`: `-24.99 USD`
  - `120m`: `+95.60 USD`
  - `135m`: `+107.57 USD`
  - `150m`: `+95.66 USD`
  - `165m`: `+125.59 USD`
  - `180m`: `+124.02 USD`
  - `210m`: `+162.99 USD`
- stabilite journaliere:
  - `120m`, `135m`, `165m`, `180m` ont chacun `8` jours meilleurs et `5` jours moins bons que la baseline
  - `210m` monte a `9` jours meilleurs et `4` jours moins bons
  - `105m` echoue (`6` jours meilleurs, `7` jours moins bons), donc le levier n'est pas monotone
- vigilance anti-overfit:
  - `165m` et `180m` gardent un gros poids sur `2026-04-09` et `2026-04-16`
  - `210m` fait le meilleur chiffre brut, mais doit encore etre traite comme candidat agressif tant qu'il n'est pas valide sur plus d'inputs

Check de robustesse par sous-fenetres:

- artefact:
  - [stop_grace_robustness_20260421/robustness_matrix.md](/workspaces/trident/server-data/replay_reports/stop_grace_robustness_20260421/robustness_matrix.md)
- deltas vs baseline:
  - `120m`: full `+95.60`, `2026-04-05 -> 2026-04-12 +18.88`, `2026-04-13 -> 2026-04-17 +84.49`
  - `135m`: full `+107.57`, `2026-04-05 -> 2026-04-12 +24.08`, `2026-04-13 -> 2026-04-17 +88.84`
  - `165m`: full `+125.59`, `2026-04-05 -> 2026-04-12 +52.93`, `2026-04-13 -> 2026-04-17 +90.68`
  - `210m`: full `+162.99`, `2026-04-05 -> 2026-04-12 +39.23`, `2026-04-13 -> 2026-04-17 +98.32`
- conclusion retenue:
  - meilleur compromis perf / robustesse a ce stade: `stop_grace_165m` applique seulement a `Pod A / trend_pullback_long`
  - variante prudente: `135m`
  - `210m` reste un bon candidat `shadow-only`, mais pas encore la recommendation prod

Decision de plan:

- ne pas deployer de `stop_grace` global a tous les pods
- promotion retenue dans le profil repo:
  - scope minimal `Pod A / trend_pullback_long` seulement
  - reference courante `165m`
  - fallback prudent `135m`
- avant confiance large / extension future:
  - valider sur de nouveaux fetchs serveurs hors de cette fenetre
  - verifier que le gain ne vient pas seulement de `2026-04-09` et `2026-04-16`

Validation dynamique finale:

- artefacts:
  - [stop_grace_dynamic_20260421/scenario_summary.md](/workspaces/trident/server-data/replay_reports/stop_grace_dynamic_20260421/scenario_summary.md)
  - [stop_grace_dynamic_v2_20260421/scenario_summary.md](/workspaces/trident/server-data/replay_reports/stop_grace_dynamic_v2_20260421/scenario_summary.md)
- variantes dynamiques testées:
  - `dynamic_extend_135_to_165`: identique au `135m`, donc aucun gain additionnel utile
  - `dynamic_165_cut_on_regime_downgrade`: `498.51 USD`, protege une partie de la queue de pertes mais coupe trop de gagnants
  - `dynamic_extend_165_to_210_on_recovery`: `525.15 USD`, reste sous le `165m` fixe
  - `partial_grace_165m_stop_1p5x`: `439.61 USD`, nettement moins bon que `165m`
- conclusion retenue:
  - aucune variante dynamique testee ne bat le `165m` fixe
  - le concept `stop_grace` est valide, mais la meilleure implementation a ce stade reste un `165m` fixe et scope a `Pod A / trend_pullback_long`

Promotion retenue dans le repo:

- implementation:
  - `config/trident.toml`: `pod_a.stop_grace_minutes = 165`
  - `app/settings.py`: ajout du champ de config `stop_grace_minutes`
  - `app/backtest/pod_a_executor.py`: application effective du `stop_grace` sur `Pod A`
  - `app/backtest/full_bot_replay.py`: replay aligne sur `PodAExecutor`
  - `tests/test_pod_a_executor.py`: couverture de non-regression
- validation finale sur le meme input serveur:
  - baseline reexecutee avec `stop_grace=0`: `+421.10 USD`
  - profil repo courant avec `stop_grace=165`: `+546.69 USD`
  - delta total: `+125.59 USD`
- statut:
  - `165m` est maintenant la reference repo pour `Pod A`
  - le redeploiement / alignement machine reelle reste suivi dans la roadmap d'alignement prod

### 2026-04-22 - Autopsie des sorties perdantes `Pod A` / `Pod C`

Contexte:

- analyse de suivi sur le profil repo courant avec `Pod A stop_grace_165m`
- objectif:
  - verifier si d'autres fermetures perdantes ont un potentiel comparable au `stop_grace`
  - distinguer les sorties localement trop precoces des sorties globalement utiles au portefeuille

`Pod A`:

- pertes restantes par cause sur le profil courant:
  - `stop_hit`: `29` trades, `-381.17 USD`
  - `opposite_signal`: `6` trades, `-89.11 USD`
  - `break_even_stop`: `15` trades, `-21.36 USD`
  - `end_of_backtest`: `2` trades, `-15.06 USD`
- lecture retenue:
  - pas de nouveau levier du niveau `stop_grace` hors `stop_hit`
  - les `6` `opposite_signal` sont tous des `trend_pullback_long` crypto encore dans la logique du `stop_grace`
  - en contre-factuel cible trade par trade, laisser vivre ces positions au lieu de fermer sur `opposite_signal` ameliore le paquet de `-89.11` a `-32.73 USD`
  - mais la suppression brute de `opposite_signal` n'est pas retenue comme solution:
    - elle desorganise trop le recycle du capital et l'inventaire des trades
    - elle doit rester une piste `shadow-only` tant qu'elle n'est pas validee en replay full-bot comparable
- point d'implementation a verifier:
  - les `preview_pod_a_signals` peuvent encore emettre `trend_pullback_short`
  - ces previews shorts peuvent donc fermer un long via `opposite_signal`
  - alors meme que les shorts `Pod A` restent desactives dans le profil repo
- decision de recherche:
  - ne pas retirer `opposite_signal` globalement
  - privilegier un filtre plus fin:
    - `opposite_signal_debounce` court
    - ou exigence que le signal oppose soit executable et persistant avant fermeture

`Pod C`:

- tres peu de matiere sur le profil courant:
  - `routing_revoked`: `11` trades, net `-2.07 USD`
  - `stop_hit`: `2` trades, `-8.37 USD`
- lecture retenue:
  - le seul levier exploitable a ce stade est `routing_revoked`
  - en contre-factuel cible, laisser vivre les `routing_revoked` au lieu de forcer la sortie passe de `-2.07` a `+8.46 USD`
  - signal tres net sur le cluster `index`
  - signal plus ambigu sur `silver`
  - `gold` ne doit pas recevoir de grace supplementaire a ce stade
  - les `stop_hit` Pod C sont trop peu nombreux et trop mixtes pour justifier un `stop_grace` equivalent
- decision de recherche:
  - ne pas deployer de `grace` global Pod C
  - tester seulement une variante `routing_revoke_grace` plus longue et scopee a `index`
  - garder `silver` en variante secondaire `shadow-only`
  - ne pas etendre a `gold`

### 2026-04-22 - Pod A `opposite_signal_debounce` promu a `15m`

- suite directe de l'autopsie `Pod A` sur les sorties `opposite_signal`
- comparaison replay sur `server-data/replay_inputs/full_bot_latest_fetch.jsonl`
- artefacts:
  - [pod_a_opposite_signal_candidates_20260422/scenario_summary.md](/workspaces/trident/server-data/replay_reports/pod_a_opposite_signal_candidates_20260422/scenario_summary.md)
  - [pod_a_opposite_signal_candidates_20260422/scenario_summary.json](/workspaces/trident/server-data/replay_reports/pod_a_opposite_signal_candidates_20260422/scenario_summary.json)
  - [pod_a_opposite_signal_candidates_20260422/debounce_30m_report.md](/workspaces/trident/server-data/replay_reports/pod_a_opposite_signal_candidates_20260422/debounce_30m_report.md)
- baseline de comparaison:
  - total: `+463.60 USD`
  - `Pod A`: `+442.17 USD`
  - `opposite_signal`: `9` trades, `-133.15 USD`
- resultats retenus:
  - `debounce_15m`: `+107.62 USD` vs baseline
  - `debounce_30m`: strictement identique a `debounce_15m`
  - `opposite_executable_persistent_2snap`: aucun effet sur ce replay
  - `block_opposite_during_stop_grace_trend`: presque equivalent mais legerement sous le `debounce_15m`
- decision:
  - promouvoir `pod_a.opposite_signal_debounce_minutes = 15` dans le profil repo
  - ne pas retenir `30m`, car le gain est identique avec une fenetre plus courte
- implementation repo:
  - [config/trident.toml](/workspaces/trident/config/trident.toml): `pod_a.opposite_signal_debounce_minutes = 15`
  - [app/settings.py](/workspaces/trident/app/settings.py): ajout du champ `opposite_signal_debounce_minutes`
  - [app/backtest/pod_a_executor.py](/workspaces/trident/app/backtest/pod_a_executor.py): debounce applique uniquement au close `opposite_signal` de `Pod A`
  - [tests/test_pod_a_executor.py](/workspaces/trident/tests/test_pod_a_executor.py): couverture du close immediat, de la fenetre de debounce et de l'expiration
- validation implementation:
  - replay full-bot reexecute avec le code de prod et `config/trident.toml`
  - total: `+571.22 USD`
  - `Pod A`: `+549.79 USD`
  - closes `Pod A`: `trailing_stop=76`, `stop_hit=32`, `break_even_stop=11`, `end_of_backtest=2`
  - resultat identique au sweep candidat `debounce_15m`

### 2026-04-22 - Baseline officielle replay remise a jour

- contexte:
  - nouvelles donnees serveur fetchees
  - rerun officiel lance avec la config repo courante `config/trident.toml`
  - commande officielle utilisee avec `--respect-config-enabled`
- nouvelle reference canonique:
  - [official_baseline_current_cli_20260422.md](/workspaces/trident/server-data/replay_reports/official_baseline_current_cli_20260422.md)
  - [official_baseline_current_cli_20260422.json](/workspaces/trident/server-data/replay_reports/official_baseline_current_cli_20260422.json)
  - [BACKTEST_REFERENCE_STATUS_20260422.md](/workspaces/trident/server-data/replay_reports/BACKTEST_REFERENCE_STATUS_20260422.md)
- resultat:
  - dates couvertes: `2026-04-05 -> 2026-04-22`
  - total: `+545.09 USD`
  - `Pod A`: `+535.37 USD`
  - `Pod B`: `0.00 USD`
  - `Pod C`: `+9.72 USD`
  - `records_processed`: `21092`
  - `duplicate_timestamps_skipped`: `292`
- delta vs baseline officielle precedente `20260419`:
  - total: `+99.17 USD`
  - `Pod A`: `+110.88 USD`
  - `Pod C`: `-11.71 USD`
- decision:
  - `official_baseline_current_cli_20260422` devient la baseline replay de reference
  - les artefacts `20260419` restent historiques, mais ne doivent plus etre utilises comme reference prod par defaut

### 2026-04-22 - Sweep `Pod C` sur `routing_revoked` clusterise

- suite des pistes `Pod C` identifiees dans le plan actif
- objectif:
  - tester une extension de `routing_revoke_grace` par symbole Tradfi sur le fetch officiel courant
  - verifier si `index` merite une promo repo et si `silver` doit rester en `shadow-only`
- artefacts:
  - [pod_c_routing_grace_candidates_20260422/scenario_summary.md](/workspaces/trident/server-data/replay_reports/pod_c_routing_grace_candidates_20260422/scenario_summary.md)
  - [pod_c_routing_grace_candidates_20260422/scenario_summary.json](/workspaces/trident/server-data/replay_reports/pod_c_routing_grace_candidates_20260422/scenario_summary.json)
  - [pod_c_routing_grace_candidates_20260422/report.md](/workspaces/trident/server-data/replay_reports/pod_c_routing_grace_candidates_20260422/report.md)
- baseline courante:
  - total: `+545.09 USD`
  - `Pod C`: `+9.72 USD`
  - `routing_revoked`: `14` trades, `-7.32 USD`
  - detail `routing_revoked`: `index -2.99`, `silver -4.49`, `gold -2.76`, `oil +2.92`
- scenarios testes:
  - `index_180m`: `+7.07 USD`
  - `index_540m`: `+12.45 USD`
  - `silver_360m`: `+0.55 USD`
  - `index_540m_plus_silver_360m`: `+13.00 USD`
- lecture retenue:
  - `index_540m` est le meilleur candidat propre:
    - gain net `+12.45 USD`
    - pas de degradation des `stop_hit`
    - meilleure piste repo a ce stade
  - `silver_360m` est trop fragile:
    - gain tres faible
    - `stop_hit` `3 -> 6`
    - pnl `stop_hit` `-14.83 -> -27.80 USD`
  - le combo `index_540m + silver_360m` fait le meilleur chiffre brut, mais le sur-gain vs `index_540m` seul n'est que `+0.55 USD` et herite de la degradation `silver`
  - `gold` reste hors promo
- decision de travail:
  - candidat principal a implementer ensuite:
    - `XYZ:SP500 = 540`
    - `XYZ:XYZ100 = 540`
  - garder `XYZ:SILVER` en `shadow-only`
  - ne rien faire pour `XYZ:GOLD`
  - ne pas relacher globalement les `stop_hit` de `Pod C`

### 2026-04-22 - Promo repo `Pod C index routing grace 540m`

- implementation repo:
  - [config/trident.toml](/workspaces/trident/config/trident.toml): ajout de `routing_revoke_grace_minutes_by_symbol`
    - `XYZ:SP500 = 540`
    - `XYZ:XYZ100 = 540`
  - [tests/test_pod_c.py](/workspaces/trident/tests/test_pod_c.py): test cible sur la grace specifique `index`
- validation:
  - test cible:
    - `python -m unittest tests.test_pod_c.PodCTests.test_pod_c_keeps_index_position_with_symbol_specific_routing_revoke_grace -q`
  - replay officiel:
    - [official_baseline_current_cli_20260422_pod_c_index540.md](/workspaces/trident/server-data/replay_reports/official_baseline_current_cli_20260422_pod_c_index540.md)
    - [official_baseline_current_cli_20260422_pod_c_index540.json](/workspaces/trident/server-data/replay_reports/official_baseline_current_cli_20260422_pod_c_index540.json)
- resultat:
  - total: `+557.54 USD`
  - `Pod A`: `+535.37 USD`
  - `Pod B`: `0.00 USD`
  - `Pod C`: `+22.17 USD`
  - delta vs baseline officielle pre-promo `20260422`: `+12.45 USD`
- decision:
  - la variante `index_540m` est maintenant promue dans le profil repo
  - `silver` reste en `shadow-only`
  - `gold` reste hors promo

### 2026-04-23 - Baseline officielle replay reexecutee sur le fetch courant

- contexte:
  - rerun officiel relance avec le code courant du repo
  - config utilisee: `config/trident.toml`
  - input officiel: `server-data/replay_inputs/full_bot_latest_fetch.jsonl`
  - commande officielle utilisee avec `--respect-config-enabled`
- nouvelle reference canonique:
  - [official_baseline_current_cli_20260423.md](/workspaces/trident/server-data/replay_reports/official_baseline_current_cli_20260423.md)
  - [official_baseline_current_cli_20260423.json](/workspaces/trident/server-data/replay_reports/official_baseline_current_cli_20260423.json)
- resultat:
  - dates couvertes: `2026-04-05 -> 2026-04-23`
  - note de couverture:
    - l'input courant saute `2026-04-19`
  - total: `+562.48 USD`
  - `Pod A`: `+526.26 USD`
  - `Pod B`: `0.00 USD`
  - `Pod C`: `+36.22 USD`
  - `records_processed`: `22102`
  - `duplicate_timestamps_skipped`: `292`
  - `directional_fees_usd`: `120.56224`
  - `total_activity_count`: `167`
  - `routing_reassignment_event_count`: `0`
- delta vs baseline officielle precedente `20260422_pod_c_index540`:
  - total: `+4.94 USD`
  - `Pod A`: `-9.11 USD`
  - `Pod C`: `+14.05 USD`
  - `records_processed`: `+1010`
- decision:
  - `official_baseline_current_cli_20260423` devient la nouvelle baseline replay de reference
  - les artefacts `20260422_pod_c_index540` restent comparables et utiles pour l'historique proche, mais ne doivent plus etre utilises comme benchmark par defaut

## Roadmap Restante

### 1. Alignement Prod Et Nettoyage Des Profils

Objectif:

- faire en sorte que le serveur tourne bien sur le profil decide
- eviter tout redemarrage futur sur une ancienne config non comparable

Reste a faire:

- redeployer la machine sur `config/trident.toml`, ou explicitement sur le shadow choisi
- supprimer ou archiver les profils stale encore presents seulement sur serveur
- nettoyer les references de deploy qui pointent vers des profils obsoletes
- verifier que les replays officiels utilisent toujours `--respect-config-enabled`

### 2. Pod A Crypto Core

Objectif:

- continuer a ameliorer le moteur principal sans reintroduire le churn qui cassait la perf

Reste a faire:

- reduire les `routing_revoked` restants hors `campaign`
- surveiller et revalider hors echantillon le `stop_grace_165m` deja promu dans le profil repo:
  - `Pod A / trend_pullback_long` seulement
  - verifier sur nouveaux fetchs serveur comparables que le gain reste positif
  - confirmer que le gain n'est pas capte par 1-2 journees exceptionnelles
- ne pas relancer de variante dynamique `stop_grace` sans hypothese contextuelle plus solide:
  - les variantes dynamiques testees a ce stade sont toutes sous le `165m` fixe
- garder `stop_grace_210m` en piste research seulement:
  - pas de promotion du profil repo tant qu'il n'est pas plus robuste que `165m` sur plusieurs fenetres
- autopsier `opposite_signal` dans le contexte `stop_grace_165m`:
  - ne pas supprimer brutalement la fermeture sur signal oppose
  - `opposite_signal_debounce` court promu dans le repo: `pod_a.opposite_signal_debounce_minutes = 15`
  - `30m` n'apporte rien de plus que `15m` sur le replay courant
  - le gate `signal oppose executable + persistant` n'a pas montre d'effet utile sur le replay courant
  - revalider hors echantillon le `15m` sur les prochains fetchs comparables avant de le considerer totalement stabilise
  - verifier puis corriger si besoin le point de code ou un `trend_pullback_short` peut encore piloter la fermeture via `opposite_signal` alors que les shorts `Pod A` restent desactives dans le profil repo
- faire evoluer `Phase 2b` vers un vrai modele:
  - rejet confirme
  - reversal fade strict
  - shadow-only tant qu'il n'est pas valide
- poursuivre `Phase 2c`:
  - moteur short separe
  - shadow-only
  - aucune reactivation naive des shorts actuels de `Pod A`

### 3. Crypto Regime V2

Objectif:

- essayer de capturer plus de chiffre sans casser la fenetre recente

Etat:

- `baseline_current` reste la reference prod
- `hybrid_moderate_a` reste la reference shadow la plus rentable

Reste a faire:

- journaliser le candidat shadow a cote du baseline en conditions live
- autopsier jour par jour les faux positifs de `Crypto Regime V2`
- definir un gate clair de promotion:
  - ne pas etre moins bon sur la fenetre recente
  - rester meilleur ou proche sur la fenetre large
  - ne pas augmenter le churn de maniere destructrice

### 4. Pod C Phase 6.10

Objectif:

- finir proprement la boucle `Pod C`

Reste a faire:

- verifier s'il reste un vrai levier sur `equity` et `fx`
- autopsier `routing_revoked` par cluster sur `Pod C`:
  - candidat principal retenu a ce stade: `routing_revoke_grace` plus long sur `index` seulement (`XYZ:SP500`, `XYZ:XYZ100` a `540m`) deja promu dans le repo
  - variante secondaire: `silver` en `shadow-only` seulement
  - ne pas etendre a `gold` sans nouvelle hypothese plus solide
- ne pas relacher globalement les `stop_hit` de `Pod C`:
  - echantillon trop faible
  - contre-factuel non assez robuste pour promouvoir un equivalent `stop_grace`
- garder une lecture par cause de fermeture perdante sur les prochains fetchs:
  - `routing_revoked`
  - `stop_hit`
  - `time_stop` si cette cause emerge davantage
- si non:
  - geler `Pod C` comme stack quasi finalise
  - limiter les evolutions a du tuning mineur et de l'observabilite

### 5. Remplacement De Pod B Par Un Sleeve Special Symbols

Objectif:

- remplacer `Pod B` par un pod dedie a des symbols isoles qui ne doivent pas etre trades par `Pod A`

Etat:

- le coeur research prometteur n'est plus `TAO/XPL/BIO/PENGU`
- le noyau le plus propre a ce stade est `TAO + XPL`
- en pratique, le replay comparable reel ne couvre pas encore assez `XPL`
- le pod dedie reste donc `shadow-only`

Reste a faire:

- elargir le fetch reel / snapshots pour inclure `XPL`
- tester `XPL-only` puis `XPL-first`, avant `TAO + XPL`
- garder `BTC` dans `Pod A`
- reserver les symbols du sleeve a ce futur pod via `pod_a.blocked_symbols`
- comparer objectivement contre:
  - `baseline_current` pur
  - `hybrid_moderate_a` pur
  - `Pod B off`
- ne promouvoir que si le sleeve ajoute du PnL net sans detruire la perf de `Pod A`

### 6. BTC Overextension Et Revue Coin-Par-Coin

Objectif:

- transformer les intuitions de `evos_btc.md` en hypotheses testables dans TRIDENT
- traiter `BTC` comme cas pilote avant toute generalisation aux autres coins crypto
- eviter d'ajouter une strategie mean-reversion directe sans preuve replay comparable

Lecture retenue:

- le signal `BTC` propose est une mean reversion apres sur-extension:
  - prix trop loin de `EMA50`
  - momentum surachete (`RSI21 > 65`)
  - confirmation par essoufflement (`MACD histogram` en baisse, meche haute / rejection)
- le signal brut annonce environ `77%` sur seulement `~18` occurrences en `4h`:
  - trop faible pour une promotion directe
  - utile comme hypothese de filtre / watcher
  - insuffisant pour ouvrir un short live ou creer un pod dedie
- dans l'architecture actuelle, l'integration la plus saine est:
  - d'abord `research-only`
  - puis `watch-only`
  - puis veto / filtre d'entree `Pod A` si le replay full-bot confirme

Evolutions BTC a implementer:

- ajouter une famille research `btc_overextension_reversion`:
  - timeframe principal `4h`
  - timeframes de controle `1h` et `2h`
  - horizon de test `1`, `2`, `3` bougies
  - variantes:
    - short theorique apres sur-extension
    - no-entry veto pour les longs `Pod A`
    - early-exit watcher pour une position longue deja ouverte
- enrichir les features candles research:
  - `rsi21`
  - distance a `EMA50` en pourcentage
  - distance a `EMA50` normalisee par `ATR14`
  - variation du `MACD histogram`
  - ratio meche haute / corps et meche basse / corps
  - position dans les bandes de Bollinger
- ajouter un rapport BTC dedie:
  - resultat par timeframe
  - train / validation / holdout chronologique
  - nombre d'occurrences
  - hit rate
  - expectancy nette apres frais
  - distribution par regime TRIDENT
  - impact simule comme veto de `Pod A / trend_pullback_long`
- ajouter une option de replay full-bot shadow:
  - baseline officielle courante
  - baseline + watcher BTC sans effet execution
  - baseline + veto BTC sur entrees longues `Pod A`
  - baseline + early-exit BTC uniquement si la version veto est deja positive
- ajouter si necessaire le support de regles `pattern_vetoes / pattern_watchers` scopees par symbole:
  - ex: `symbols = ["BTC"]`
  - ne pas appliquer un veto BTC globalement aux autres coins sans validation separee
- ne pas activer de short BTC live sur cette base:
  - les shorts `Pod A` restent desactives dans le profil repo
  - toute branche short doit passer par un moteur short separe ou une variante shadow explicite

Critere de promotion BTC:

- minimum `40` occurrences cumulees sur la famille testee, ou justification explicite si le pattern reste rare
- expectancy nette positive apres frais sur train et holdout
- pas de degradation de la baseline full-bot officielle
- pas de gain concentre sur 1 ou 2 journees
- meilleur resultat en mode filtre / veto avant toute hypothese de strategie standalone
- promotion maximale initiale:
  - `watcher` si le signal est informatif mais pas encore portefeuille-additif
  - `veto Pod A BTC-only` si le replay complet bat la baseline
  - jamais `nouveau pod` sans preuve transversale sur plusieurs coins

Statut implementation `2026-04-24`:

- implemente dans `Pod A` comme veto BTC-only sur `trend_pullback_long`
- features runtime ajoutees: `rsi21_4h`, distance `EMA50 4h` en `%` et `ATR`, `MACD hist 4h`, ratios de meches, position Bollinger, `btc_overextension_score`
- regle active:
  - `symbols = ["BTC"]`
  - `sides = ["long"]`
  - `min_rsi21_4h = 65.0`
  - `min_ema50_distance_4h_pct = 4.0`
  - `min_btc_overextension_score = 0.70`
- replay strict initial avec `max_macd_hist_delta_4h = 0.0`: aucun veto declenche, perf identique baseline
- replay adapte score-only:
  - rapport: `server-data/replay_reports/full_bot_btc_overextension_poda_score_20260424.json`
  - baseline officielle: total `+562.48`, `Pod A +526.26`
  - variante BTC veto: total `+586.39`, `Pod A +550.17`
  - delta: `+23.91` total / `+23.91` Pod A
  - `4` decisions veto, `3` trades BTC en moins, PnL BTC `+147.55 -> +171.46`
- interpretation: signal utile en filtre Pod A, mais encore trop concentre pour generalisation automatique; prochaine etape = holdout / extension multi-fenetres avant de dupliquer sur d'autres coins

Activation prod dry-run:

- la modification est active dans `config/trident.toml`, qui est la config prod de reference
- `pod-a-live` et `trident_dry_run_launcher` chargent `config/trident.toml` par defaut
- le deploiement Docker peut toutefois surcharger via `TRIDENT_CONFIG_PATH`; verifier que le serveur ne pointe plus vers un ancien profil `config/trident_crypto_launch_fast*`
- comme le systeme tourne actuellement en dry-run, la regle peut etre observee en conditions prod sans ordre reel

Checklist avant passage live:

- verifier sur le serveur que `TRIDENT_CONFIG_PATH=config/trident.toml` pour `trident-api` et `pod-a-live`
- redeployer / restart `pod-a-live` apres synchronisation de la config pour garantir que le veto est charge
- confirmer dans les logs dry-run que les rejets `pattern_veto_btc_overextension_4h` apparaissent seulement sur `BTC` long `trend_pullback_long`
- confirmer dans les logs dry-run que les rejets `pattern_veto_hype_trend_pullback_long_targeted` apparaissent seulement sur `HYPE` long `trend_pullback_long`
- confirmer dans les logs dry-run que les rejets `pattern_veto_xrp_overextension_4h_targeted` apparaissent seulement sur `XRP` long `trend_pullback_long`
- surveiller au moins une fenetre dry-run recente avec:
  - absence d'erreur dans `logs/pod_a_live_status.json`
  - snapshots frais dans `data/live_snapshots`
  - pas de baisse nette de PnL dry-run attribuable au veto BTC
  - pas de reduction excessive d'activite BTC hors zones d'overextension
- relancer un replay full-bot sur les snapshots dry-run les plus recents apres deploiement serveur
- verifier que la config live garde les shorts `Pod A` desactives; ce changement reste un veto long, pas une strategie short
- avant ordre reel:
  - commencer avec Pod A seul ou taille minimale
  - confirmer caps de levier HL live appliques
  - verifier que le dashboard et les journaux affichent les positions / rejets attendus
  - garder un rollback simple: desactiver le veto concerne (`btc_overextension_4h`, `hype_trend_pullback_long_targeted`, `xrp_overextension_4h_targeted`) ou repasser `enabled = false`

Index rapports et artefacts `2026-04-24`:

| Sujet | Fichier | Chemin | Usage |
|-------|---------|--------|-------|
| BTC overextension Pod A, replay score-only | `full_bot_btc_overextension_poda_score_20260424.md` | `server-data/replay_reports/full_bot_btc_overextension_poda_score_20260424.md` | synthese lisible du replay valide |
| BTC overextension Pod A, replay score-only | `full_bot_btc_overextension_poda_score_20260424.json` | `server-data/replay_reports/full_bot_btc_overextension_poda_score_20260424.json` | chiffres machine-readable du replay valide |
| BTC overextension Pod A, replay strict initial | `full_bot_btc_overextension_poda_20260424.md` | `server-data/replay_reports/full_bot_btc_overextension_poda_20260424.md` | tentative stricte, perf identique baseline |
| BTC overextension Pod A, replay strict initial | `full_bot_btc_overextension_poda_20260424.json` | `server-data/replay_reports/full_bot_btc_overextension_poda_20260424.json` | details machine-readable de la tentative stricte |
| Matrice patterns tous coins | `bot_coin_pattern_matrix_20260424.md` | `server-data/replay_reports/bot_coin_pattern_matrix_20260424.md` | tableau lisible par coin avec split `L/S` |
| Matrice patterns tous coins | `bot_coin_pattern_matrix_20260424.json` | `server-data/replay_reports/bot_coin_pattern_matrix_20260424.json` | resultats complets et `side_breakdown` par pattern |
| Validation ciblee coin+pattern Pod A | `pod_a_coin_pattern_targeted_validation_20260424.md` | `server-data/replay_reports/pod_a_coin_pattern_targeted_validation_20260424.md` | reference de decision par coin/pattern, long-only |
| Validation ciblee coin+pattern Pod A | `pod_a_coin_pattern_targeted_validation_20260424.json` | `server-data/replay_reports/pod_a_coin_pattern_targeted_validation_20260424.json` | resultats machine-readable par coin/pattern |
| Validation implementations candidates | `coin_pattern_implementation_validation_20260424.md` | `server-data/replay_reports/coin_pattern_implementation_validation_20260424.md` | test cible des candidats Pod A veto / Pod B slot |
| Validation implementations candidates | `coin_pattern_implementation_validation_20260424.json` | `server-data/replay_reports/coin_pattern_implementation_validation_20260424.json` | resultats machine-readable des candidats implementes |
| Archive garde-fou global shorts | `pod_a_pattern_evolution_validation_20260424.md` | `server-data/replay_reports/pod_a_pattern_evolution_validation_20260424.md` | test global par famille, non utilise pour decision coin-par-coin |
| Archive garde-fou global shorts | `pod_a_pattern_evolution_validation_20260424.json` | `server-data/replay_reports/pod_a_pattern_evolution_validation_20260424.json` | resultats machine-readable du test global |
| Dataset recherche tous coins | `manifest.json` | `data/research/hyperliquid_bot_coins/current/manifest.json` | index de collecte, fenetres, symboles, timeframes |
| Dataset recherche tous coins | `raw/candles/<tf>/<symbol>.json.gz` | `data/research/hyperliquid_bot_coins/current/raw/candles/` | bougies conservees pour recalculs |
| Dataset recherche tous coins | `raw/funding/<symbol>.json.gz` | `data/research/hyperliquid_bot_coins/current/raw/funding/` | funding conserve pour recalculs |

Methodologie pour etudier chaque coin en detail:

- construire une file d'analyse par vagues:
  - vague 1 core: `BTC`, `ETH`, `SOL`, `HYPE`
  - vague 2 majors observees: `DOGE`, `XRP`, `SUI`, `AVAX`, `LINK`, `ARB`, `ADA`, `BNB`, `LTC`, `AAVE`, `NEAR`, `ZRO`, `ZEC`, `ENA`, `TON`, `BCH`
  - vague 3 symbols speciaux / bloques: `TAO`, puis candidats type `XPL`, `BIO`, `PENGU` seulement si la collecte les couvre
  - tradfi `XYZ:*` a traiter separement via la boucle `Pod C`, pas avec les seuils crypto BTC
- pour chaque coin, produire un dossier standard:
  - qualite data: couverture, trous, nombre de bougies par timeframe
  - liquidite: volume, notional, spread, trade count, open interest
  - relation BTC/ETH: correlation, beta, alignement / divergence
  - regime: perf des patterns par `TrendExpansion`, `RangeAuction`, `PanicSqueeze`, `DeadZone`
  - archetypes: trend, breakout, mean reversion, funding reversion
  - compatibilite pod:
    - `Pod A` si trend / pullback robuste
    - `Pod B` si breakout / squeeze robuste
    - sleeve special si comportement isolé et non transferable
    - `observe_only` si edge faible ou non stable
    - `new_pod_candidate` seulement si mean reversion robuste sur plusieurs coins
- utiliser des seuils normalises, pas un copier-coller BTC:
  - distance EMA en `ATR`
  - percentiles historiques par symbole
  - z-scores de funding / volume
  - buckets de volatilite et de liquidite
- separer trois niveaux de preuve:
  - candles HL: utile pour trouver des hypotheses
  - snapshots serveur comparables: necessaires pour valider un filtre compatible TRIDENT
  - replay full-bot: obligatoire avant toute promotion
- standardiser les sorties:
  - un JSON machine-readable par coin
  - un MD de synthese par coin
  - une matrice finale `symbol -> owner recommande -> action`
- planifier les decisions possibles:
  - `promote_watch`
  - `promote_veto_shadow`
  - `promote_config_candidate`
  - `keep_research_only`
  - `observe_only`
  - `remove_from_tradable_pool`

Matrice coin-par-coin `2026-04-24`:

- dataset stocke: `data/research/hyperliquid_bot_coins/current`
- rapport complet:
  - `server-data/replay_reports/bot_coin_pattern_matrix_20260424.md`
  - `server-data/replay_reports/bot_coin_pattern_matrix_20260424.json`
- demande initiale `30j`, elargie automatiquement a `180j`:
  - `1h`, `2h`, `4h`, `1d`: fenetre complete
  - `15m`, `30m`: limite officielle HL `5000` bougies, completee avec l'ancien dataset local + tails recents
- horizons testes: `1`, `2`, `3` bougies
- le rapport `2026-04-24` inclut maintenant un split `long-only` / `short-only` par pattern (`side_breakdown` dans le JSON, labels `L/S` dans le MD)
- lecture: ce tableau est `research candles`; toute promotion config doit passer par replay full-bot comparable

Validation ciblee full-bot coin+pattern `2026-04-24`:

- rapport de reference:
  - `server-data/replay_reports/pod_a_coin_pattern_targeted_validation_20260424.md`
  - `server-data/replay_reports/pod_a_coin_pattern_targeted_validation_20260424.json`
- baseline actuelle avec veto BTC: total `+586.39`, `Pod A +550.17`
- shorts: non testes dans cette passe; tous les scenarios sont `long-only`
- `TAO`: debloque uniquement dans le scenario `TAO trend_pullback`, Pod B gardant TAO bloque
- verdicts `keep`:
  - `BTC trend_pullback`: ablation delta `-171.46`, PnL cible baseline `+171.46` sur `28` trades
  - `LINK trend_pullback`: ablation delta `-13.49`, PnL cible baseline `+13.49` sur `5` trades
  - `ENA trend_pullback`: ablation delta `-106.98`, PnL cible baseline `+106.98` sur `9` trades
- verdicts `reject`:
  - `HYPE trend_pullback`: ablation delta `+10.25`; retirer HYPE ameliore le full-bot
  - `XRP ichimoku_continuation`: ajout delta `-242.56`, PnL cible `-10.77` sur `23` trades
  - `AVAX vwap_reclaim`: ajout delta `-2.95`, PnL cible `-2.95` sur `1` trade
  - `ARB ichimoku_continuation`: ajout delta `-252.08`, PnL cible `-20.29` sur `23` trades
  - `BNB ichimoku_continuation`: ajout delta `-235.62`, PnL cible `-3.83` sur `10` trades
  - `LTC ichimoku_continuation`: ajout delta `-203.88`, PnL cible `-9.27` sur `13` trades
  - `NEAR vwap_reclaim`: ajout delta `-18.05`, PnL cible `-25.88` sur `2` trades
  - `TAO trend_pullback`: ajout delta `-596.77`, PnL cible `-46.60` sur `8` trades; garder TAO bloque
- verdicts `no_effect`:
  - `ETH vwap_reclaim`: aucun trade cible sur cette fenetre
  - `BNB vwap_reclaim`: aucun trade cible sur cette fenetre
- conclusion operationnelle:
  - garder `BTC`, `LINK`, `ENA` dans `trend_pullback_long`
  - veto cible `HYPE / trend_pullback_long` ajoute a `config/trident.toml` pour observation dry-run:
    - `[[pod_a.pattern_vetoes]].name = "hype_trend_pullback_long_targeted"`
    - `symbols = ["HYPE"]`
    - `sides = ["long"]`
    - `setups = ["trend_pullback_long"]`
    - raison replay: retirer HYPE ameliorait le full-bot de `+10.25`
    - avant passage live: confirmer sur une seconde fenetre replay / dry-run
  - ne pas promouvoir les ajouts `vwap_reclaim_long` ou `ichimoku_continuation_long` testes ici
  - garder `TAO` bloque tradable

Validation implementation candidats non couverts Pod A `2026-04-24`:

- rapport:
  - `server-data/replay_reports/coin_pattern_implementation_validation_20260424.md`
  - `server-data/replay_reports/coin_pattern_implementation_validation_20260424.json`
- baseline config de depart: veto BTC + HYPE actif, total `+596.64`, `Pod A +560.42`, `Pod C +36.22`, `Pod B 0.00`
- methodologie:
  - `ema50_overextension_reversion`: transforme en veto Pod A cible `trend_pullback_long` par symbole
  - `ttm_squeeze_release`: teste via slot Pod B cible `ttm_squeeze_release_long`
  - `squeeze_breakout`: teste via slot Pod B cible `compression_breakout_long`
  - pour Pod B, chaque delta est mesure contre un replay controle avec meme routage/allocation mais setup Pod B desactive
- verdict `keep_candidate`:
  - `XRP ema50_overextension_reversion`: delta `+6.45`, 1 veto, PnL cible `-3.07/3t` -> `+3.38/2t`
  - action: veto ajoute a `config/trident.toml` pour observation dry-run:
    - `[[pod_a.pattern_vetoes]].name = "xrp_overextension_4h_targeted"`
    - `symbols = ["XRP"]`
    - `sides = ["long"]`
    - `setups = ["trend_pullback_long"]`
    - seuils: `rsi21_4h >= 65`, `ema50_distance_4h_pct >= 4`, `ema50_distance_4h_atr >= 2`, `btc_overextension_score >= 0.70`
- verdicts `no_effect`:
  - `LTC ema50_overextension_reversion`: aucun veto declenche, delta `0.00`
  - `ZRO ema50_overextension_reversion`: aucun veto declenche, delta `0.00`
  - `DOGE`, `SUI`, `AVAX`, `LINK`, `ZRO`, `TON`, `BCH` en `ttm_squeeze_release_long`: aucun trade Pod B cible
  - `NEAR squeeze_breakout` mappe en `compression_breakout_long`: aucun trade Pod B cible
- non implementes dans cette passe:
  - `funding_reversion`: necessite un sleeve dedie; les snapshots replay exposent le funding courant mais pas le z-score funding + trigger BB/stoch/CCI de la recherche candles
  - `range_mean_reversion` et `stoch_cci_reversion`: necessitent un pod mean-reversion, pas adapte a Pod A/Pod B actuels
  - `trend_breakout`: pas de mapping production strict; ne pas assimiler automatiquement a Pod B compression
- conclusion operationnelle:
  - promouvoir seulement `XRP` en veto dry-run
  - ne pas activer Pod B pour les candidats squeeze tant que le moteur live ne reproduit pas de signaux sur snapshots serveur
  - conserver les edges Pod B candles comme recherche, pas comme config executable

Archive garde-fou global shorts `2026-04-24`:

- rapport:
  - `server-data/replay_reports/pod_a_pattern_evolution_validation_20260424.md`
  - `server-data/replay_reports/pod_a_pattern_evolution_validation_20260424.json`
- baseline actuelle avec veto BTC: total `+586.39`, `Pod A +550.17`
- `trend_pullback`:
  - no-short: delta `0.00`
  - shorts-on: delta `-335.20`, `trend_pullback_short -335.20`
  - verdict: `reject` pour la reactivation globale du short
- `vwap_reclaim`:
  - no-short: delta `-87.26`
  - shorts-on: delta `-130.30`, `vwap_reclaim_short -43.04`
  - verdict: `reject`
- `ichimoku_continuation`:
  - no-short: delta `-355.17`
  - shorts-on: delta `-927.37`, `ichimoku_continuation_short -572.20`
  - verdict: `reject`
- conclusion operationnelle:
  - ce test etait global par famille, pas cible coin-par-coin
  - il sert seulement de garde-fou: ne pas reactiver les shorts Pod A globalement
  - les decisions coin-par-coin doivent se baser sur `pod_a_coin_pattern_targeted_validation_20260424.*`

| Coin | Patterns les plus interessants | Lecture / suite |
|------|--------------------------------|-----------------|
| `BTC` | `trend_pullback` `2h h3`: `57.2004 bps`, n=`14`<br>`funding_reversion` `4h h3`: `53.3059 bps`, n=`8` | `Pod A` |
| `ETH` | `vwap_reclaim` `4h h1`: `108.9252 bps`, n=`14`<br>`vwap_reclaim` `2h h2`: `53.9404 bps`, n=`38` | `Pod A`, corr BTC 1h `0.8945` |
| `SOL` | `funding_reversion` `2h h2`: `82.7706 bps`, n=`16`<br>`funding_reversion` `2h h3`: `77.6316 bps`, n=`15` | `research_only`, corr BTC 1h `0.8462` |
| `HYPE` | `funding_reversion` `15m h3`: `59.7231 bps`, n=`11`<br>`trend_pullback` `2h h2`: `105.8477 bps`, n=`9` | `research_only`, corr BTC 1h `0.5658` |
| `DOGE` | `ttm_squeeze_release` `2h h1`: `75.8638 bps`, n=`10`<br>`ttm_squeeze_release` `2h h2`: `50.7612 bps`, n=`10` | `Pod B`, corr BTC 1h `0.7974` |
| `XRP` | `ema50_overextension_reversion` `15m h3`: `46.4922 bps`, n=`11`<br>`ichimoku_continuation` `4h h3`: `60.4905 bps`, n=`53` | `watch/veto`, corr BTC 1h `0.7966` |
| `SUI` | `ttm_squeeze_release` `2h h2`: `126.6209 bps`, n=`14`<br>`ttm_squeeze_release` `2h h3`: `123.7374 bps`, n=`14` | `Pod B`, corr BTC 1h `0.7913` |
| `AVAX` | `vwap_reclaim` `2h h1`: `56.054 bps`, n=`23`<br>`ttm_squeeze_release` `30m h3`: `31.347 bps`, n=`19` | `Pod A`, corr BTC 1h `0.7879` |
| `LINK` | `ttm_squeeze_release` `2h h2`: `71.0509 bps`, n=`9`<br>`trend_pullback` `1h h3`: `35.4078 bps`, n=`20` | `Pod B`, corr BTC 1h `0.8482` |
| `ARB` | `ichimoku_continuation` `4h h3`: `59.0739 bps`, n=`51`<br>`ichimoku_continuation` `4h h2`: `49.4271 bps`, n=`65` | `Pod A`, corr BTC 1h `0.7382` |
| `ADA` | `funding_reversion` `2h h2`: `72.4633 bps`, n=`16`<br>`funding_reversion` `2h h1`: `48.6946 bps`, n=`17` | `research_only`, corr BTC 1h `0.8076` |
| `BNB` | `vwap_reclaim` `4h h1`: `36.5701 bps`, n=`18`<br>`ichimoku_continuation` `4h h3`: `28.9087 bps`, n=`47` | `Pod A`, corr BTC 1h `0.8352` |
| `LTC` | `ema50_overextension_reversion` `2h h3`: `40.9118 bps`, n=`55`<br>`ichimoku_continuation` `4h h2`: `41.6604 bps`, n=`42` | `watch/veto`, corr BTC 1h `0.7109` |
| `AAVE` | `funding_reversion` `2h h2`: `82.8807 bps`, n=`9`<br>`funding_reversion` `2h h3`: `90.4577 bps`, n=`9` | `research_only`, corr BTC 1h `0.7273` |
| `NEAR` | `squeeze_breakout` `1h h2`: `51.8767 bps`, n=`11`<br>`vwap_reclaim` `1h h2`: `31.0235 bps`, n=`55` | `Pod B`, corr BTC 1h `0.6523` |
| `ZRO` | `ema50_overextension_reversion` `4h h3`: `95.1641 bps`, n=`36`<br>`ttm_squeeze_release` `2h h3`: `78.5522 bps`, n=`10` | `watch/veto`, corr BTC 1h `0.4578` |
| `TAO` | `trend_pullback` `2h h3`: `177.6836 bps`, n=`11`<br>`trend_pullback` `2h h2`: `149.0897 bps`, n=`11` | `Pod A`, corr BTC 1h `0.6102`; reste bloque tradable |
| `ZEC` | `range_mean_reversion` `15m h3`: `97.0182 bps`, n=`8`<br>`range_mean_reversion` `15m h2`: `60.7868 bps`, n=`8` | `research_only`, corr BTC 1h `0.4422` |
| `ENA` | `trend_breakout` `30m h3`: `85.6124 bps`, n=`8`<br>`trend_pullback` `2h h2`: `79.8661 bps`, n=`8` | `Pod A`, corr BTC 1h `0.7057` |
| `TON` | `stoch_cci_reversion` `1h h3`: `57.55 bps`, n=`14`<br>`ttm_squeeze_release` `2h h3`: `91.5703 bps`, n=`9` | `research_only`, corr BTC 1h `0.5944` |
| `BCH` | `ttm_squeeze_release` `2h h2`: `73.5455 bps`, n=`8`<br>`ttm_squeeze_release` `2h h3`: `82.5992 bps`, n=`8` | `Pod B`, corr BTC 1h `0.5849` |

Reste a faire:

- confirmer le veto candidat `HYPE / trend_pullback_long` sur une deuxieme fenetre replay avant toute promotion config
- confirmer le veto candidat `XRP / xrp_overextension_4h_targeted` sur dry-run et sur une deuxieme fenetre replay avant toute promotion live
- ne pas reprendre les tests shorts tant qu'une hypothese short ciblee n'est pas redefinie coin par coin
- implementer en shadow/watch seulement les familles non executables avant replay PnL:
  - `funding_reversion`
  - `range_mean_reversion`
  - `stoch_cci_reversion`
- ne pas promouvoir `LTC` / `ZRO` overextension sur la fenetre actuelle: aucun veto cible n'a declenche en replay
- ne pas promouvoir les familles squeeze / breakout cote `Pod B` dans la config actuelle: tous les tests cibles ont fait `0` trade
- transformer les meilleurs candidats `watch/veto` restants en hypotheses de replay full-bot, une par symbole
- prioriser les candidats a sample plus robuste hors deja testes: `ARB`, `BNB`, puis nouvelles donnees `LTC` / `ZRO`
- analyser separement les familles `funding_reversion` avant toute promotion, car elles ne correspondent pas encore a un pod live dedie
- garder `TAO` bloque tant que le choix tradable n'est pas revalide en replay full-bot et en dry-run
- ne pas etendre le veto BTC aux autres coins sans replay full-bot comparable par symbole

### 7. Iteration Microstructure Observables-First

Objectif:

- exploiter davantage le socle `l2Book + trades` deja collecte sans lancer un nouveau pod non prouve
- transformer les intuitions utiles de microstructure en hypotheses testables, puis en filtres `watchers / vetoes` si elles tiennent en replay comparable

Etat:

- le socle live existe deja:
  - collector HL natif `l2Book + trades`
  - snapshots enrichis avec `spread`, `depth`, `book_imbalance`, `trade_flow_bias`, `microprice_dislocation`, ratios d'activite et deltas
  - sidecar intraminute `Pod B` en `10s / 30s`
  - outillage `pod_liq_features` / `pod_liq_research`
- les flux user HL (`userFills`, `openOrders`, `orderUpdates`, `clearinghouseState`) ne sont pas encore dans le chemin critique
- il n'y a pas encore de signal first-class `cancel / replace`, `absorption` ou `exhaustion`; ces idees doivent donc d'abord etre testees via proxies observables sur snapshots comparables

Validation `2026-04-23`:

- rapports produits:
  - [pod_liq_microstructure_iteration_report_20260423.md](/workspaces/trident/server-data/replay_reports/pod_liq_microstructure_iteration_report_20260423.md)
  - [pod_liq_microstructure_candidates_20260423_h1.md](/workspaces/trident/server-data/replay_reports/pod_liq_microstructure_candidates_20260423_h1.md)
  - [pod_liq_microstructure_candidates_20260423_h3.md](/workspaces/trident/server-data/replay_reports/pod_liq_microstructure_candidates_20260423_h3.md)
- validation exhaustive supplementaire:
  - [pod_liq_exhaustive_research_20260423.md](/workspaces/trident/server-data/replay_reports/pod_liq_exhaustive_research_20260423.md)
  - [pod_liq_exhaustive_research_20260423.json](/workspaces/trident/server-data/replay_reports/pod_liq_exhaustive_research_20260423.json)
- integration watch-only et validation replay:
  - [full_bot_micro_watch_baseline_20260423.md](/workspaces/trident/server-data/replay_reports/full_bot_micro_watch_baseline_20260423.md)
  - [full_bot_micro_watch_baseline_20260423.json](/workspaces/trident/server-data/replay_reports/full_bot_micro_watch_baseline_20260423.json)
  - [full_bot_pod_b_enabled_20260423.md](/workspaces/trident/server-data/replay_reports/full_bot_pod_b_enabled_20260423.md)
  - [full_bot_pod_b_enabled_20260423.json](/workspaces/trident/server-data/replay_reports/full_bot_pod_b_enabled_20260423.json)
  - [pod_b_allocation_diagnosis_20260423.md](/workspaces/trident/server-data/replay_reports/pod_b_allocation_diagnosis_20260423.md)
  - [pod_b_sleeve_sweep_20260423.md](/workspaces/trident/server-data/replay_reports/pod_b_sleeve_sweep_20260423.md)
  - [pod_b_micro_watch_replay_20260423.json](/workspaces/trident/server-data/replay_reports/pod_b_micro_watch_replay_20260423.json)
  - rapport de synthese:
    - [pod_b_micro_watch_integration_20260423.md](/workspaces/trident/server-data/replay_reports/pod_b_micro_watch_integration_20260423.md)
- verdict actuel sur snapshots serveur comparables:
  - garder en `watch-only` / `shadow filter`:
    - `depth_refill_continuation`
    - `liquidity_pull_continuation`
  - garder en `research-only`:
    - `absorption_reversal`
    - `cancel_replace_proxy` via `book_churn_flow_veto`
  - rejeter dans leur forme actuelle:
    - `exhaustion_reversal`
- lecture de ces resultats:
  - `depth_refill_continuation` est confirme comme meilleur candidat en replay exhaustif:
    - meilleur mode retenu: `horizon=3`, `score>=0.75`, `spread<=3.0`, `notional>=250`, `regime=range_dead`
    - holdout `2026-04-20 -> 2026-04-23`: `+1.3093 bps`, hit rate `0.5377`
  - `liquidity_pull_continuation` est finalement assez propre pour rester aussi en `watch-only`:
    - meilleur mode retenu: `horizon=3`, `score>=0.65`, `spread<=3.0`, `notional>=100`, `regime=trend_panic`
    - holdout `2026-04-20 -> 2026-04-23`: `+0.9681 bps`, hit rate `0.5504`
  - integration `watch-only` dans `Pod B`:
    - watchers configures:
      - `micro_liquidity_pull_trend_panic`
      - `micro_depth_refill_trend_panic`
    - replay complet `respect-config-enabled`:
      - baseline strictement inchangee vs benchmark officiel `20260423`
      - `total_realized_pnl_usd=562.48`
      - `Pod A=526.26`, `Pod B=0.0`, `Pod C=36.22`
    - replay `Pod B` dedie sur l'input officiel courant:
      - `signal_count=0`, `accepted_count=0`, `closed_trade_count=0`
      - conclusion: les watchers sont bien armes, mais `Pod B` reste actuellement dormant sur le fetch officiel comparable courant
    - replay complet correctif avec `Pod B` explicitement active:
      - `total_realized_pnl_usd=586.31`
      - `Pod A=550.09`, `Pod B=0.0`, `Pod C=36.22`
      - `routing_reassignment_event_count=2845`
      - conclusion: le `0` de `Pod B` ne venait pas seulement de `enabled=false`; meme active, la strategie ne declenche aucun signal sur ce fetch officiel
    - diagnostic racine confirme:
      - la config courante alloue `Pod B` uniquement en `DeadZone`, alors que `Pod B` ne trade que `TrendExpansion / PanicSqueeze`
      - sur le fetch officiel courant avec `Pod B` active et allocations par defaut:
        - `opening scope` seulement en `DeadZone`
        - `103599` contextes `Pod B`, tous rejetes d'abord par `regime_not_allowed`
    - test correctif d'allocations alignees:
      - avec un sleeve `Pod B` en `TrendExpansion / PanicSqueeze` et `0` en `DeadZone`, `Pod B` redevient actif
      - replay `Pod B` dedie:
        - `signal_count=107`
        - `accepted_count=45`
        - `closed_trade_count=43`
        - `realized_pnl_usd=67.46`
      - replay full bot:
        - `total_realized_pnl_usd=438.93`
        - `Pod A=324.42`, `Pod B=78.29`, `Pod C=36.22`
      - conclusion: l'incoherence de config explique bien le silence de `Pod B`, mais une correction brute des allocations degrade le portefeuille global en cannibalisant `Pod A`
    - mini-sweep de sleeves `Pod B`:
      - `panic_only_5`:
        - total `526.20`
        - `Pod B -11.76`
      - `trend_2_panic_5`:
        - total `518.10`
        - `Pod B -11.76`
        - pas de signaux `TrendExpansion`; le sleeve `2%` est vraisemblablement trop petit pour devenir deployable avec le floor de sizing courant
      - `trend_3_panic_5`:
        - total `396.30`
        - `Pod B +3.64`
        - premiers signaux `TrendExpansion`, mais cannibalisation trop forte de `Pod A`
      - conclusion:
        - aucun sleeve teste ne bat la baseline officielle
        - `Pod B` peut etre positif standalone, mais n'est pas encore portefeuille-additif sur le fetch officiel courant
    - verification research sur le meme flux comparable:
      - evenements bruts `liquidity_pull` compatibles watcher: `369`, expectancy moyenne `+2.3082 bps`, hit rate `0.5718`
      - evenements bruts `depth_refill` compatibles watcher: `1169`, expectancy moyenne `+0.6139 bps`, hit rate `0.5252`
      - conclusion pratique: les patterns existent bien dans le flux, mais ils ne croisent pas encore les conditions d'entree `Pod B` actuelles
  - `absorption_reversal` n'est pas assez bon pour une promo, mais pas totalement mort:
    - meilleur mode retenu: `horizon=3`, `score>=0.75`, `spread<=5.0`, `notional>=250`, `regime=trend_panic`
    - holdout: `+0.1268 bps`; garder seulement en `research-only`
  - `cancel_replace_proxy` via churn de carnet montre un veto plausible mais pas encore assez fort:
    - meilleur mode retenu: `book_churn_flow_veto`, `horizon=5`, `score>=0.75`, `spread<=5.0`, `notional>=250`, `regime=trend_panic`
    - holdout: `-0.8179 bps` dans le sens du flow, mais hit rate veto encore trop juste (`0.5103`)
  - `exhaustion_reversal` reste a laisser tomber:
    - meilleur mode trouve en train, mais le holdout repasse negatif (`-0.1073 bps`)
  - limite replay importante:
    - les snapshots comparables ne contiennent pas de vraies bandes `2 / 5 / 10 bps`
    - on teste donc `best_bid_size / best_ask_size` comme proxy inner-band et `depth_10bps` comme proxy outer-band
    - les feeds HL `orderUpdates / userFills / openOrders / clearinghouseState` ne sont pas dans l'input replay, donc non validables ici

Reste a faire:

- etendre le sidecar microstructure et les snapshots research avec des proxies testables:
  - `liquidity_pull_score`: retrait rapide de profondeur, widening du spread, degradation d'imbalance
  - `depth_velocity` et `refill_velocity` sur plusieurs bandes de profondeur (`2 / 5 / 10 bps`)
  - `absorption_score`: gros notional et flow agressif, mais faible deplacement du mid
  - `exhaustion_score`: intensite qui monte, mais `delta_mid` qui plafonne et alignement `flow / book` qui se degrade
- prochaine iteration prioritaire:
  - promouvoir au plus en `watcher` / veto shadow `Pod B` le proxy `depth_refill_continuation`
  - promouvoir au plus en `watcher` / veto shadow `Pod B` le proxy `liquidity_pull_continuation`
  - garder `absorption` et `book_churn_flow_veto` dans la boucle `research-only` tant qu'ils ne passent pas un holdout plus propre
  - ne pas pousser `exhaustion` plus loin sans nouvelle formulation ou nouvelles donnees
- ne pas attendre un vrai feed `cancel / replace` pour commencer:
  - utiliser d'abord des proxies bases sur `depth pull`, `refill`, `spread` et `microprice_dislocation`
- garder la premiere boucle en `research-only`:
  - enrichir `app/research/pod_liq_features.py`
  - enrichir `app/research/pod_liq_research.py`
  - produire des rapports sur snapshots serveur comparables, pas sur candles HL seules
  - mesurer l'expectancy par symbole, regime, cluster, setup et bucket de feature
- brancher ces signaux d'abord comme filtres legers:
  - priorite `Pod B` pour filtrer les faux breakouts, breakouts crowded et impulsions deja epuisees
  - priorite secondaire `Pod A` seulement si un pattern perdant robuste emerge sur les phases impulsives
  - priorite `Pod C` seulement si une poche perdante clusterisee reapparait nettement sur un fetch comparable
- privilegier les tests de filtrage microstructure plutot qu'un nouveau retuning global des exits:
  - plus prometteur a ce stade que relancer des `runner` globaux ou un nouveau sweep de `min_confidence`
- ne pas creer de nouveau pod principal `funding / liq / open interest` sur cette base sans preuve replay comparable
- si et seulement si l'execution HL reelle devient prioritaire:
  - evaluer le branchement des flux `userFills / openOrders / orderUpdates / clearinghouseState`
  - evaluer si `allMids` WS apporte un benefice reel par rapport au fallback REST actuel

### 8. Pistes Research Seulement

Restent hors du coeur live:

- `funding / liq / open interest` comme moteurs principaux
- toute these validee seulement sur candles HL sans replay snapshot comparable
- les sleeves ou pods qui n'ont pas encore de preuve sur source serveur comparable

## Regles De Promotion

- toute promotion de config ou de logique doit etre validee par replay complet sur la meme source d'entree
- le benchmark par defaut est:
  - [official_baseline_current_cli_20260423.md](/workspaces/trident/server-data/replay_reports/official_baseline_current_cli_20260423.md)
- les comparaisons non comparables ou experimentales doivent rester clairement etiquetees comme telles
- un gain sur source synthetique seule ne suffit pas pour la prod

## Documents Remplaces Ou Rabaisses En Historique

- [plan_trident.md](/workspaces/trident/plan_trident.md)
- [docs/crypto_refonte_plan_20260417.md](/workspaces/trident/docs/crypto_refonte_plan_20260417.md)
- [docs/pod_c_vs_pod_a_transfer_20260418.md](/workspaces/trident/docs/pod_c_vs_pod_a_transfer_20260418.md)
- [docs/trident_plan/status.md](/workspaces/trident/docs/trident_plan/status.md)
- [docs/trident_plan/stages.md](/workspaces/trident/docs/trident_plan/stages.md)

## Documents Historiques Gardes Comme Reference

- [docs/new_podB.md](/workspaces/trident/docs/new_podB.md)
- [docs/trident_plan/spec.md](/workspaces/trident/docs/trident_plan/spec.md)
- [docs/pod_c_research_protocol.md](/workspaces/trident/docs/pod_c_research_protocol.md)
- [docs/pod_liq_data_feasibility.md](/workspaces/trident/docs/pod_liq_data_feasibility.md)
