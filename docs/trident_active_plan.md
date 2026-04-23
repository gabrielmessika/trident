# TRIDENT Active Plan

Date: `2026-04-23`

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

### 6. Iteration Microstructure Observables-First

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

### 7. Pistes Research Seulement

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
