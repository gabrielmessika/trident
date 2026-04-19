# TRIDENT Active Plan

Date: `2026-04-19`

## Status

- `ACTIVE_SINGLE_SOURCE_OF_TRUTH`
- Ce fichier remplace les anciens plans de pilotage pour les evolutions a venir.
- Les documents historiques restent utiles pour le contexte, mais plus comme feuille de route principale.

## References Actives

### Prod de reference

- config: `config/trident.toml`
- backtest de reference propre:
  - [official_baseline_current_cli_20260419.md](/workspaces/trident/server-data/replay_reports/official_baseline_current_cli_20260419.md)
  - [official_baseline_current_cli_20260419.json](/workspaces/trident/server-data/replay_reports/official_baseline_current_cli_20260419.json)
- resultat:
  - total `+445.92 USD`
  - `Pod A +424.49`
  - `Pod B 0.00`
  - `Pod C +21.43`

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
  - reste moins bon que le baseline sur la fenetre recente `2026-04-13 -> 2026-04-17`

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
  - [official_baseline_current_cli_20260419.md](/workspaces/trident/server-data/replay_reports/official_baseline_current_cli_20260419.md)
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
