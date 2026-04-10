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
| 5. Pod B range engine natif | 92% | Recaler Pod B comme complement du profil actif sur run long |
| 5bis. Routing dynamique symbols / ownership | 100% | Rien, etape fermee |
| 6. Reporting par pod | 100% | Rien, etape fermee |
| 7. Research Pod pour Pod C | 100% | Rien, etape fermee |
| 8. Pod C minimal | 100% | Rien, etape fermee |
| 9. Hardening deployment | 100% | Rien, etape fermee |
| 10. Passage live progressif | 40% | Dry-run serveur long avec profil actif `Pod A + Pod B` |
| 11. Pistes futures Hydra revisitees | 35% | Continuer les runs offline funding/liq |
| 12. Pod C v2 Squeeze Breakout | 80% | Valider offline puis activer |

## Journal condense

### 2026-04-08 (suite)

- Pod B reecrit avec modele Avellaneda-Stoikov:
  - **fair value EMA**: quotes centrees sur un prix estime, pas le spot
  - **inventory skew**: mid-price deplace en fonction de l'inventaire (long → mid baisse pour attirer des sells)
  - **spread volatilite-adaptatif**: plus large quand volatile, plus serre quand calme
  - **trend guard**: arrete de quoter le cote perdant quand prix diverge du fair value
  - **grille multi-niveaux** configurable (1-N niveaux avec espacement geometrique)
  - config: `paper_fair_value_ema_alpha`, `paper_inventory_skew_intensity`, `paper_volatility_spread_multiplier`, `paper_grid_levels`
  - `max_allocation_pct` reduit de 0.70 a 0.40 pour liberer des coins a Pod A
  - `max_inventory_skew_pct` reduit de 1.0 a 0.25 pour empecher l'accumulation directionnelle
- constat sur backtest historique candles HL (annule):
  - l'API HL ne fournit que des candles OHLCV, pas de microstructure L2
  - le bot est calibre pour des snapshots minute avec spread/depth/flow
  - les candles 1h ne sont pas representatives: test sur avril 5-8 donne -32 USD vs +200 USD sur donnees L2
  - conclusion: backtest candles non viable, seules les donnees L2 live sont fiables

### 2026-04-08

- replay "bot complet" ajoute pour rejouer le systeme A/B/C dans des conditions proches du dry-run:
  - [full_bot_replay.py](/workspaces/trident/app/backtest/full_bot_replay.py)
- historique comparatif ajoute:
  - [history.jsonl](/workspaces/trident/data/replay_reports/full_bot/history.jsonl)
- sweep d'experiences radicales ajoute:
  - [full_bot_experiment_sweep.py](/workspaces/trident/app/backtest/full_bot_experiment_sweep.py)
  - [full_bot_experiment_sweep_20260407T214546Z.json](/workspaces/trident/data/replay_reports/full_bot_sweeps/full_bot_experiment_sweep_20260407T214546Z.json)
- constat structurel retenu:
  - `Pod A` porte l'essentiel du PnL
  - `Pod B` ajoute peu mais reste utile en complement
  - `Pod C` detruit de la valeur sur la fenetre validee
- profil principal bascule sur:
  - `Pod A + Pod B`
  - `Pod C off`
- validation replay de la config active:
  - [full_bot_backtest_20260407T214946Z.json](/workspaces/trident/data/replay_reports/full_bot/full_bot_backtest_20260407T214946Z.json)
  - total realise `+27.0668 USD`
  - `467` reattributions
- Hydra explicitement maintenu hors run principal:
  - funding / liq / OI restent en piste research offline

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

- statut: reecrit avec modele Avellaneda-Stoikov
- lecture actuelle:
  - engine refonde: fair value EMA + inventory skew + vol-adaptive spread + trend guard
  - ne detruit plus de valeur de maniere systematique
  - contribution marginale mais stable sur les jours calmes
  - jour directionnel (ADX > 20) reste le principal risque → regime guard coupe le quoting
  - `max_allocation_pct` reduit a 0.40 pour ne pas monopoliser les coins
  - prochaine validation: run long serveur avec profil actif `Pod A + Pod B`

### Profil actif 2026-04-08

- decision retenue:
  - `Pod A` principal
  - `Pod B` complement
  - `Pod C` desactive par defaut
- preuve actuelle:
  - meilleur replay valide sur `2026-04-05 -> 2026-04-07`
  - [full_bot_backtest_20260407T214946Z.json](/workspaces/trident/data/replay_reports/full_bot/full_bot_backtest_20260407T214946Z.json)
  - `+27.0668 USD` realise
- consequence:
  - les prochains dry-runs longs doivent partir de ce profil
  - `Pod C` reste disponible pour la recherche, pas pour le run principal

### Hydra

- statut: research offline uniquement
- regle retenue:
  - funding / liq / OI ne rentrent pas dans le coeur live tant qu'un sweep offline n'a pas produit un verdict `go`

## Prochaines actions

1. lancer un dry-run serveur long avec la config active `Pod A + Pod B, Pod C off`
2. auditer `Pod B` comme couche complementaire avec les outils de review existants
3. continuer la validation de `Pod A` complet sur une plage plus longue
4. lancer un sweep Hydra offline et sortir un memo `go / park / kill`
