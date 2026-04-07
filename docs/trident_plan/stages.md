# TRIDENT — Etapes detaillees

## Etapes fermees

### Etape 0 — Cadrage et branchement

- statut: completee
- sortie utile:
  - repo bootstrappe
  - plan de travail pose
  - structure Python en place

### Etape 1 — Superviseur vide + ownership

- statut: completee
- sortie utile:
  - superviseur central
  - ownership exclusif
  - premiers tests

### Etape 2 — Regime allocator deterministe

- statut: completee
- sortie utile:
  - `raw_regime`
  - `pending_regime`
  - hysteresis de regime

### Etape 3 — Capital allocator + cash mode

- statut: completee
- sortie utile:
  - allocation deterministe par pod
  - cash mode explicite

### Etape 4 — Pod A minimal

- statut: completee
- sortie utile:
  - backtest et live runner
  - risk gate partage
  - executor directionnel partage

### Etape 5bis — Routing dynamique des symbols / ownership

- statut: completee
- criteres de done atteints:
  - attribution automatique des symbols
  - aucun conflit d'ownership sur replay reel retenu
  - explication claire dans l'UI
  - reattributions pilotables
  - pin manuel disponible sans redeploiement
- validation finale:
  - replay dedie `routing_replay.py`
  - replays reels `2026-04-05 -> 2026-04-07`
  - seuils recalibres

### Etape 6 — Reporting par pod

- statut: completee
- sortie utile:
  - `api/report`
  - reporting multi-pods
  - journalisation et exports

### Etape 7 — Research Pod pour Pod C

- statut: completee
- sortie utile:
  - protocole research
  - suite reproducible
  - cadre go/no-go

### Etape 8 — Pod C minimal

- statut: completee
- sortie utile:
  - runner, planner, service
  - integration superviseur

### Etape 9 — Hardening deployment

- statut: completee
- sortie utile:
  - deployment scripts
  - docker artifacts
  - hardening Hyperliquid

## Etapes ouvertes

### Etape 4bis — Pod A complet / t-bot+

- statut: 99%
- objectif:
  - confirmer le comportement live petit wallet sur une plage plus longue
- reste a faire:
  - validation dry-run live prolongee

### Etape 5 — Pod B range engine natif

- statut: 92%
- objectif:
  - rendre Pod B fiable en 3 pods dry-run 24h
- reste a faire:
  - run 24h 3 pods
  - audit expectancy/churn/toxicite
  - recalibrage de la couche range

### Etape 10 — Passage live progressif

- statut: 30%
- objectif:
  - sortir du mode validations courtes vers de vrais runs operables
- reste a faire:
  - lancer des dry-runs serveur longs
  - review systematique
  - augmenter progressivement le niveau de confiance

### Etape 11 — Pistes futures Hydra revisitees

- statut: 35%
- objectif:
  - garder funding/liq/lead-lag comme axes research, hors run principal
- reste a faire:
  - accumuler les datasets
  - lancer les runs offline utiles
  - decider quelles hypotheses meritent une integration future

## Definition of done transversale

Une etape est fermee quand:

- le code existe
- les tests cibles existent et passent
- l'observabilite est suffisante pour comprendre le comportement
- un critere de validation concret a ete execute
- la prochaine action ne consiste plus a "finir la base", mais a exploiter ou calibrer

## Commandes a garder en tete

- `python -m app.main`
- `python -m app.live.trident_dry_run_launcher`
- `python -m app.backtest.archive_replay`
- `python -m app.backtest.routing_replay`
- `./scripts/trident_dry_run_review.sh`
- `./scripts/fetch_trident_data.sh`

## Risques encore ouverts

### Pod B reste le principal risque strategique

- trop de concentration possible
- calibration encore fragile
- besoin de plus de runs longs

### Le passage live reste surtout un sujet d'operations

- longues sessions
- hygiene review
- verification de la coherence runtime

### Les pistes Hydra doivent rester hors du coeur live

- pas de dette speculative dans la boucle principale
- pas d'activation sans preuve offline claire
