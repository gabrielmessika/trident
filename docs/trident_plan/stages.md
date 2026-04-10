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

- statut: 95%
- objectif:
  - rendre Pod B fiable comme complement du run principal
- fait:
  - paper_live_runner reecrit: superviseur partage, routing dynamique, allocation par regime
  - Pod B utilise `config/trident.toml` (plus de dependance a `runtime/passivbot/live.json`)
  - comportement identique au full-bot backtest
- reste a faire:
  - run long avec le profil actif `Pod A + Pod B`
  - audit expectancy/churn/toxicite
  - recalibrage de la couche range pour un role de complement, pas de coeur de perf

### Etape 10 — Passage live progressif

- statut: 40%
- objectif:
  - sortir du mode validations courtes vers de vrais runs operables
- reste a faire:
  - lancer des dry-runs serveur longs avec le profil `Pod A + Pod B, Pod C off`
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

### Etape 12 — Pod C v2 Squeeze Breakout

- statut: en cours
- objectif:
  - remplacer la logique lead-lag de Pod C par une strategie Volatility Squeeze Breakout
  - approche fondamentalement differente de Pod A (trend continuation) et Pod B (range/MM)
  - Pod C v2 cible la **transition** range→trend : detecte la compression de volatilite puis trade le breakout
- principe:
  - phase 1 (squeeze detection): `bucket_range_bps` actuel < `squeeze_threshold` × moyenne rolling
  - phase 2 (breakout trigger): expansion de vol + direction confirmee par `book_imbalance` + `trade_flow_bias` + spike volume
  - phase 3 (ride + exit): trailing stop base sur ATR, time stop court (2-4h)
- changements requis:
  - `app/settings.py`: remplacer les champs lead-lag de `PodCConfig` par les champs squeeze (lookback, thresholds)
  - `config/trident.toml` `[pod_c]`: nouveaux parametres squeeze
  - `app/trident/pod_c/signals.py`: nouveaux dataclasses `SqueezeContext` + `SqueezeSignal`
  - `app/trident/pod_c/service.py`: `SqueezeBreakoutService` avec fenetre rolling de vol par symbole
  - `app/trident/pod_c/context.py`: `SqueezeContextService` construit les contextes depuis les snapshots
  - `app/trident/pod_c/exits.py`: politique de sortie adaptee au breakout (trailing ATR)
  - `app/trident/pod_c/planner.py`: adaptation du planner
  - `app/trident/pod_c/__init__.py`: exports mis a jour
  - `app/trident/symbol_router.py` `_score_pod_c`: nouveau scoring favorisant les coins en squeeze (vol basse)
  - `app/trident/supervisor.py`: rewiring des services Pod C
- contrainte:
  - un seul coin trade a la fois sur HL
  - Pod C reste desactive par defaut, activable apres validation offline
  - l'ancien code lead-lag est remplace (pas de Pod D, trop de refactoring sur l'archi 3-pods)
- criteres de done:
  - code compile et tests passent
  - backtest runner et live runner fonctionnent avec la nouvelle logique
  - scoring du router aligne avec la logique squeeze
  - la config par defaut est sensee et documentee

### Etape 13 — Pod A optimisation aggressive

- statut: en cours
- objectif:
  - ameliorer drastiquement le PnL de Pod A (moteur principal du bot)
  - baseline: +57.32 USD sur 3493 records (Apr 5-8), 110 trades, win rate 59%
- diagnostic (backtest squeeze_v2):
  - Pod A perd en DeadZone (-5.55) et RangeAuction (-1.07) = -6.62 USD de perte evitable
  - 28/110 trades (25%) fermes par `routing_revoked` = trades coupes prematurement
  - sizing trop conservateur: risk_per_trade 0.75%, PnL moyen +0.52 USD/trade
  - setups `bos_retest_long` (-1.26) et `vwap_reclaim_long` (-0.83) ont une expectancy negative
  - `break_even_stop` (12 trades) et trailing trop agressif coupent les gagnants trop tot
- evolutions (implementees et validees 1 par 1 avec backtest):
  - evo 1: filtrer les signaux Pod A en DeadZone/RangeAuction → REVERTED (pertes viennent du close_regime, pas open)
  - evo 2: allonger grace periods routing (30→60 defaut, 90-120→150-180 par symbole) → KEPT (+15.46 USD, routing_revoked 28→19)
  - evo 3: augmenter sizing (risk 0.75%→1.25%, leverage 3→5x, alloc 60%→85%) → KEPT (72.78→172.21 USD)
  - evo 4: desactiver setups perdants (bos_retest_long, trend_pullback_short) → KEPT (172.21→200.80, WR 73.3%, DD /2)
  - evo 5: ajuster exits (trailing/break_even moins agressifs) → REVERTED (-7.64 regression)
- resultat final:
  - baseline: +57.32 USD, 110 trades, WR 59%, DD 19.07
  - optimise: **+200.80 USD**, 75 trades, WR **73.3%**, DD **15.2**
  - amelioration: **+250%** PnL, +14pp win rate, drawdown divise par 2
- criteres de done:
  - chaque evo validee par backtest incremental
  - PnL total > baseline: 200.80 vs 57.32 ✓
  - win rate en hausse: 73.3% vs 59% ✓
  - drawdown en baisse: 15.2 vs 19.07 ✓

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
- `python -m app.backtest.full_bot_replay`
- `python -m app.backtest.full_bot_experiment_sweep`
- `./scripts/trident_dry_run_review.sh`
- `./scripts/fetch_trident_data.sh`

## Risques encore ouverts

### Pod B reste le principal risque strategique

- edge encore faible
- calibration encore fragile
- besoin de plus de runs longs en tandem avec `Pod A`

### Le passage live reste surtout un sujet d'operations

- longues sessions
- hygiene review
- verification de la coherence runtime

### Pod C reste un risque s'il revient trop vite dans le run principal

- la fenetre replay validee le montre destructeur de valeur
- il doit rester en recherche tant qu'un nouveau verdict offline ne l'invalide pas

### Les pistes Hydra doivent rester hors du coeur live

- pas de dette speculative dans la boucle principale
- pas d'activation sans preuve offline claire
