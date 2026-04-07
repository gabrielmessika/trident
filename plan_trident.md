# TRIDENT — Plan vivant

> Derniere refonte: 2026-04-07
> Objectif: garder un point d'entree court, fiable et maintenable pour piloter les evolutions du projet.

## Lecture rapide

Lire dans cet ordre:

1. [docs/trident_plan/status.md](/workspaces/trident/docs/trident_plan/status.md)
2. [docs/trident_plan/spec.md](/workspaces/trident/docs/trident_plan/spec.md)
3. [docs/trident_plan/stages.md](/workspaces/trident/docs/trident_plan/stages.md)

Ce fichier reste volontairement court. Le detail historique supprime ici reste recuperable via `git history` si besoin.

## Etat actuel

- `Etape 0` a `Etape 4`: completees
- `Etape 4bis`: 99%
- `Etape 5`: 92%
- `Etape 5bis`: 100%, fermee
- `Etape 6` a `Etape 9`: completees
- `Etape 10`: 30%
- `Etape 11`: 35%

## Ce qui est vrai aujourd'hui

- le superviseur est la seule source d'autorite pour:
  - l'ownership effectif
  - le routage des symbols
  - les caps/allocation par pod
- le routage dynamique `global + local` est en place:
  - `local_regime` par coin
  - hysteresis
  - cooldown de reattribution
  - overrides statiques et runtime
  - UI System explicable
- un pin runtime peut etre applique sans redeploiement:
  - fichier runtime
  - endpoint `POST /api/routing/override`
  - panneau direct dans l'UI
- `5bis` a ete valide sur snapshots reels `2026-04-05 -> 2026-04-07`

Reglage routing retenu:

- `min_assign_score = 0.40`
- `min_hold_score = 0.30`
- `hysteresis_margin = 0.10`
- `reassignment_cooldown_seconds = 600`

## Priorites suivantes

1. `Etape 5`: pousser Pod B vers un dry-run 24h 3 pods exploitable, puis recalibrer la couche range/toxicite.
2. `Etape 10`: utiliser le lanceur dry-run 3 pods sur serveur et auditer les runs avec `trident_dry_run_review.sh`.
3. `Etape 4bis`: confirmer sur une plage plus longue le comportement `Pod A` en dry-run live petit wallet.
4. `Etape 11`: continuer les runs offline funding/liq et garder ces hypotheses hors du run principal.

## Regles de maintenance

- mettre a jour ce fichier seulement pour:
  - l'etat global
  - les priorites
  - les decisions structurantes
- mettre a jour [status.md](/workspaces/trident/docs/trident_plan/status.md) apres chaque milestone concret
- mettre a jour [spec.md](/workspaces/trident/docs/trident_plan/spec.md) seulement si l'architecture cible change
- mettre a jour [stages.md](/workspaces/trident/docs/trident_plan/stages.md) quand une etape change de definition, de statut ou de critere de done
