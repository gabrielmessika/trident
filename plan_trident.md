# TRIDENT — Plan vivant

> Derniere refonte: 2026-04-10
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
- `Etape 10`: 40%
- `Etape 11`: 35%
- `Etape 12`: 45% (transport des regimes par cluster en place; modele d'allocation Tradfi a refondre)
- `Etape 13`: en cours (Pod A optimisation aggressive)

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
- un replay "bot complet" dans des conditions proches du dry-run existe:
  - `app.backtest.full_bot_replay`
  - rapports historises sous `data/replay_reports/full_bot/`
- un sweep d'experiences radicales existe:
  - `app.backtest.full_bot_experiment_sweep`
  - comparaison historisee sous `data/replay_reports/full_bot_sweeps/`
- le profil recommande et configure par defaut est maintenant:
  - `Pod A` moteur principal
  - `Pod B` complement defensif/range
  - `Pod C` desactive dans le run principal
- le slot `Pod C` est maintenant reserve a un futur pod directionnel Tradfi HL:
  - inspire de `Pod A`
  - pas de `Pod D`
  - sous-univers initial limite aux indices / commodities
- le transport du regime par cluster est en place:
  - chaque cluster leader (BTC, SPY, GLD, SLV) peut produire un `RegimeSnapshot` independant
  - `crypto_regime` (BTC) continue de piloter Pod A et Pod B
  - le routeur peut scorer Pod C avec le regime du cluster du symbol
  - l'API expose les `cluster_regimes` et `cluster_regime_snapshots`
- mais la vraie resolution du probleme Tradfi n'est pas encore terminee:
  - l'agregat unique `tradfi_regime` est juge trop grossier
  - la cible devient une allocation Tradfi par cluster (`index`, `gold`, `silver`, `equity`, etc.)
  - `pod_c.target_pct` doit devenir la somme des budgets de clusters actifs
  - `cash` doit etre recalcule comme residuel global, jamais garde depuis une table crypto
- la meilleure validation replay retenue a ce stade sur `2026-04-05 -> 2026-04-07` est:
  - profil `Pod A + Pod B, Pod C off`
  - `+27.0668 USD` realise
  - `467` reattributions
- Hydra reste une piste de recherche offline:
  - funding / liq / OI hors coeur live
  - pas d'activation sans preuve offline nette
- les backtests credibles restent fondes sur des snapshots minute `l2Book + trades`:
  - les candles HL seules ont deja ete invalidees pour les pods directionnels
  - pas de relance de cette piste sans nouvelle preuve de precision

Reglage routing retenu:

- `min_assign_score = 0.40`
- `min_hold_score = 0.30`
- `hysteresis_margin = 0.10`
- `reassignment_cooldown_seconds = 600`

## Priorites suivantes

1. `Etape 12`: remplacer le faux pilotage Tradfi par un budget par cluster, avec invariants de somme (`total <= 1.0`) et fallback coherent.
2. `Etape 12`: brancher Pod C sur des budgets de clusters, pas sur un agregat `tradfi_regime`.
3. `Etape 12`: constituer un dataset Tradfi exploitable pour replay via snapshots minute `l2Book + trades`, eventuellement enrichis par `assetCtx`.
4. `Etape 10`: lancer un dry-run serveur long avec le profil actif `Pod A + Pod B, Pod C off`, puis auditer avec `trident_dry_run_review.sh`.
5. `Etape 4bis`: confirmer sur une plage plus longue le comportement `Pod A` en dry-run live petit wallet.
6. `Etape 5`: recalibrer Pod B comme complement du run principal, pas comme coeur de perf, via expectancy/toxicite sur runs longs.

## Regles de maintenance

- mettre a jour ce fichier seulement pour:
  - l'etat global
  - les priorites
  - les decisions structurantes
- mettre a jour [status.md](/workspaces/trident/docs/trident_plan/status.md) apres chaque milestone concret
- mettre a jour [spec.md](/workspaces/trident/docs/trident_plan/spec.md) seulement si l'architecture cible change
- mettre a jour [stages.md](/workspaces/trident/docs/trident_plan/stages.md) quand une etape change de definition, de statut ou de critere de done
