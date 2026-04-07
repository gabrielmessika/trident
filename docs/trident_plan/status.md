# TRIDENT — Statut

## Tableau de pilotage

| Etape | Avancement | Prochain milestone |
|-------|------------|--------------------|
| 0. Cadrage et branchement | 100% | Rien, etape fermee |
| 1. Superviseur + ownership | 100% | Rien, etape fermee |
| 2. Regime allocator deterministe | 100% | Rien, etape fermee |
| 3. Capital allocator + cash mode | 100% | Rien, etape fermee |
| 4. Pod A minimal | 100% | Rien, etape fermee |
| 4bis. Pod A complet / t-bot+ | 99% | Valider sur une plage dry-run live plus longue |
| 5. Pod B range engine natif | 92% | Premier dry-run 24h 3 pods avec Pod B conservateur |
| 5bis. Routing dynamique symbols / ownership | 100% | Rien, etape fermee |
| 6. Reporting par pod | 100% | Rien, etape fermee |
| 7. Research Pod pour Pod C | 100% | Rien, etape fermee |
| 8. Pod C minimal | 100% | Rien, etape fermee |
| 9. Hardening deployment | 100% | Rien, etape fermee |
| 10. Passage live progressif | 30% | Premier dry-run 24h serveur + audit |
| 11. Pistes futures Hydra revisitees | 35% | Continuer les runs offline funding/liq |

## Journal condense

### 2026-04-07

- coherence superviseur/runtime/API renforcee
- univers observe derive du live avec `tradable_pool` et raisons de rejet
- clusters de marche `crypto/index/gold` branches
- routage dynamique `global + local` finalise:
  - `local_regime_by_symbol`
  - transitions locales
  - cooldown de reattribution
  - overrides statiques/runtime
  - UI System explicable
- validation reelle de `5bis` sur `server-data/live_snapshots/2026-04-05..07`
- outil ajoute:
  - [routing_replay.py](/workspaces/trident/app/backtest/routing_replay.py)
- artefacts generes:
  - [routing_replay_current_2026-04-05_2026-04-07.json](/workspaces/trident/data/replay_reports/routing_replay_current_2026-04-05_2026-04-07.json)
  - [routing_replay_tighter_2026-04-05_2026-04-07.json](/workspaces/trident/data/replay_reports/routing_replay_tighter_2026-04-05_2026-04-07.json)
  - [routing_replay_looser_2026-04-05_2026-04-07.json](/workspaces/trident/data/replay_reports/routing_replay_looser_2026-04-05_2026-04-07.json)

### 2026-04-05

- Pod A live runner branche
- Pod B paper runner et wrapper live branches
- Pod C minimal implemente
- reporting multi-pods et dashboard enrichis
- fetch/review tooling ajoute
- hardening Hyperliquid et artefacts de deploiement ajoutes

### 2026-04-04

- bootstrap repo et premiers socles:
  - convertisseur snapshots
  - superviseur
  - integration Pod B initiale
  - replay archives local

## Decision recentes importantes

### Routing 5bis

- statut: ferme
- principe retenu:
  - `global_regime` borne risque et caps
  - `local_regime` decide l'affinite par coin
  - hysteresis + cooldown limitent le flip-flop
- reglage retenu:
  - `min_assign_score = 0.40`
  - `min_hold_score = 0.30`
  - `hysteresis_margin = 0.10`
  - `reassignment_cooldown_seconds = 600`
- replay retenu:
  - `records_processed = 2911`
  - `duplicate_timestamps_skipped = 1457`
  - `max_ownership_conflict_count = 0`
  - `reassignment_event_count = 649`
  - owner share:
    - `pod_a = 6.88%`
    - `pod_b = 78.42%`
    - `pod_c = 14.70%`

### Pod B

- statut: encore a durcir strategiquement
- lecture actuelle:
  - l'infra est saine
  - la couche range reste trop dominante dans certains replays
  - la prochaine validation utile est un dry-run 24h 3 pods

## Prochaines actions

1. lancer un dry-run 24h 3 pods sur serveur
2. auditer Pod B avec les outils de review existants
3. continuer la validation de Pod A complet sur une plage plus longue
4. accumuler les datasets `funding/liq` hors run principal
