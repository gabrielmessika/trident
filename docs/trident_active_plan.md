# TRIDENT Active Plan

Date: `2026-04-22`

## Status

- `ACTIVE_SINGLE_SOURCE_OF_TRUTH`
- Ce fichier remplace les anciens plans de pilotage pour les evolutions a venir.
- Les documents historiques restent utiles pour le contexte, mais plus comme feuille de route principale.

## References Actives

### Prod de reference

- config: `config/trident.toml`
- backtest de reference propre:
  - [official_baseline_current_cli_20260422.md](/workspaces/trident/server-data/replay_reports/official_baseline_current_cli_20260422.md)
  - [official_baseline_current_cli_20260422.json](/workspaces/trident/server-data/replay_reports/official_baseline_current_cli_20260422.json)
- resultat:
  - total `+545.09 USD`
  - `Pod A +535.37`
  - `Pod B 0.00`
  - `Pod C +9.72`
- note courante:
  - le profil repo courant inclut `pod_a.stop_grace_minutes = 165` et `pod_a.opposite_signal_debounce_minutes = 15`
  - baseline officielle rerun sur le fetch serveur courant `2026-04-05 -> 2026-04-22`
  - delta vs baseline officielle `20260419`: `+99.17 USD`

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
  - candidat principal: `routing_revoke_grace` plus long sur `index` seulement
  - variante secondaire: `silver` en `shadow-only`
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

### 6. Pistes Research Seulement

Restent hors du coeur live:

- `funding / liq / open interest` comme moteurs principaux
- toute these validee seulement sur candles HL sans replay snapshot comparable
- les sleeves ou pods qui n'ont pas encore de preuve sur source serveur comparable

## Regles De Promotion

- toute promotion de config ou de logique doit etre validee par replay complet sur la meme source d'entree
- le benchmark par defaut est:
  - [official_baseline_current_cli_20260422.md](/workspaces/trident/server-data/replay_reports/official_baseline_current_cli_20260422.md)
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
