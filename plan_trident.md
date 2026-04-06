# TRIDENT — Plan d'implementation

> Version initiale: 2026-04-04
> Objectif produit: construire un nouveau bot Hyperliquid capable de viser une fenetre de performance agressive sur 10 jours, tout en restant developpable, testable et deployable par une equipe sans ambiguite.
>
> Principe cle: ne pas chercher "un signal miracle". Construire une machine composee de pods specialises, avec allocation deterministe, ownership strict des symbols, et validation empirique a chaque etape.

### Statut d'implementation

- 2026-04-04: bootstrap du repo commence dans `/workspaces/trident`
- 2026-04-04: points 1, 2 et 3 avances dans `trident`:
  - convertisseur `gbot -> snapshots TRIDENT` enrichi avec `l2 + trades`
  - execution dry-run explicite ajoutee pour Pod A
  - squelette Pod B branche au superviseur
  - historique de regime et metriques derivees exposes par le superviseur
  - wrapper de process Pod B `start/stop/restart` ajoute
  - dataset historique `gbot/data` copie localement dans `trident/data/gbot_archive`
  - dataset historique `gbot/server-data` copie localement dans `trident/data/server_archive`
  - replay d'archives local ajoute pour Pod A
- 2026-04-05: historique local `trident` complete par sync incrementale:
  - `server_archive` synchronise depuis `/workspaces/gbot/server-data`
  - `24 fichiers` ajoutes, `22 fichiers` mis a jour
  - archive locale portee a `3.0G`, `173 fichiers`
  - nouveaux datasets `2026-04-05` disponibles en local pour `l2`, `trades`, `signals`, `logs`, `api-state`
- 2026-04-05: replay archive etendu ajoute et valide:
  - report Pod A enrichi avec stats par date
  - transitions de regime agregees par date et par type
  - replay reel `2026-04-01 -> 2026-04-05` sur `BTC, ETH, SOL, HYPE`
  - artefacts conserves dans `data/replay_reports/`
  - resultat current:
    - `records_processed = 5151`
    - `signal_count = 25`
    - `accepted_count = 18`
    - `closed_trade_count = 10`
    - `realized_pnl_usd = 19.66`
    - `regime_transition_count = 144`
- 2026-04-05: hysteresis de regime ajoutee et calibree:
  - `raw_regime` distinct du `regime` effectif
  - `pending_regime` et `pending_regime_count` exposes
  - confirmations:
    - `switch_confirmation_bars = 3`
    - `trend_confirmation_bars = 1`
    - `panic_confirmation_bars = 1`
  - sweep de calibration sur replay `2026-04-01 -> 2026-04-05`:
    - `(2,1,1)` -> `111 transitions`, `22.55 USD`, `31 signals`
    - `(2,2,1)` -> `83 transitions`, `-1.36 USD`, `8 signals`
    - `(3,1,1)` -> `93 transitions`, `21.01 USD`, `36 signals`
    - `(3,2,1)` -> `66 transitions`, `-0.23 USD`, `10 signals`
  - configuration retenue: `(3,1,1)` comme meilleur compromis stabilite / activite
  - artefacts finaux conserves dans:
    - `data/replay_reports/archive_replay_hysteresis_2026-04-01_2026-04-05.json`
    - `data/replay_reports/archive_replay_hysteresis_2026-04-01_2026-04-05.jsonl`
- 2026-04-05: Pod B enrichi cote statut runtime:
  - `pod_b_status` expose maintenant:
    - `positions`
    - `open_orders`
    - `inventory`
    - `total_position_count`
    - `total_open_order_count`
    - `total_notional_usd`
    - `total_unrealized_pnl_usd`
  - inventory par symbol derivee automatiquement si le wrapper n'envoie qu'un sous-ensemble des champs
- 2026-04-05: source continue de snapshots online ajoutee dans `trident`:
  - `app/live/collector.py` connecte Hyperliquid en WebSocket
  - souscriptions live:
    - `l2Book`
    - `trades`
  - `app/live/snapshot_builder.py` convertit le flux online en snapshots TRIDENT
  - `app/live/snapshot_writer.py` ecrit les snapshots dans `data/live_snapshots/`
  - smoke online valide:
    - `coins = BTC, ETH`
    - `messages_processed = 45`
    - `snapshots_written = 2`
    - `reconnect_count = 0`
    - fichier ecrit: `data/live_snapshots/2026-04-05.jsonl`
- 2026-04-05: Pod A branche directement au flux live:
  - `app/live/pod_a_live_runner.py` cree
  - pipeline live:
    - `collector -> snapshot builder -> supervisor -> risk gate -> dry-run executor`
  - journal live optionnel supporte
- 2026-04-05: etapes 5 a 9 completees dans le repo:
  - executor directionnel partage ajoute pour garantir les memes regles entre backtest / dry-run / live des pods directionnels
  - gate de risque partage ajoute pour Pod A et Pod C
  - reporting multi-pods complete avec export journalier et reconciliation explicite
  - research Pod C complete avec suite reproductible, protocole et memo `no-go` sur donnees locales recentes
  - Pod C minimal implemente:
    - context service
    - signal service
    - planner
    - gate
    - runner backtest
    - runner live
  - hardening Hyperliquid ajoute:
    - gestion rate-limit / retries HTTP
    - collector WebSocket avec heartbeats, timeouts et backoff exponentiel
    - rate limiter partage cross-process avec circuit breaker
  - artefacts de deployment ajoutes:
    - `Dockerfile.trident`
    - `docker-compose.trident.yml`
    - `prepare_server.sh`
    - `deploy.sh`
    - scripts `start/stop/restart/healthcheck`
    - `scripts/trident_server.sh`
    - `docs/deployment.md`
  - validations recentes:
    - `68 tests` passent
    - smoke live collector `BTC, ETH`: `34 messages`, `1 snapshot`, `0 erreur API`
    - smoke live Pod A: `1 record`, `0 signal`
    - smoke live Pod C: `1 record`, `0 signal`
    - replay Pod B multi-jour `2026-04-01 -> 2026-04-05`: `5151 records`, `199 fills`, `-16.5853 USD`
    - research suite Pod C sur `data/live_snapshots/2026-04-05.jsonl`: `recommendation = no-go`
    - scripts de deploiement valides:
      - `bash -n prepare_server.sh deploy.sh scripts/trident_*.sh` -> OK
      - `./deploy.sh --help` -> OK
      - `./scripts/trident_server.sh --help` -> OK
  - outillage de revue dry-run ajoute:
    - `scripts/trident_dry_run_review.sh` collecte les artefacts depuis le serveur
    - checks deterministes:
      - API /health
      - containers actifs
      - fraicheur du dernier snapshot
      - conflits d'ownership
      - tracebacks recents dans les logs
    - sorties generees:
      - `review_summary.md`
      - `review_summary.json`
      - prompts LLM par etape quand une revue qualitative est necessaire
  - outillage de fetch complet ajoute:
    - `scripts/fetch_trident_data.sh` inspire de `gbot/fetch-data.sh`
    - rapatrie snapshots live, logs runtime, statuses, snapshots API et logs Docker
    - filtres supportes:
      - `--all`
      - `--date YYYY-MM-DD`
      - `--days N`
      - `--logs-only`
      - `--snapshots-only`
      - `--review-only`
    - relance en option la revue locale avec prompts via `trident_dry_run_review.sh`
  - dashboard runtime enrichi:
    - pastilles de statut vert / orange / rouge
    - commentaire synthese "est-ce que ca tourne bien ?"
    - section `Recent trading activity`
    - les trades de Pod A / Pod C y apparaissent depuis les journaux `trade_close`
    - les fills recents de Pod B y apparaissent depuis `pod_b_status.recent_fills`
    - colonne `Leverage` ajoutee dans l'activite recente
    - tooltips visuels CSS sur les colonnes pour expliquer les champs affiches
    - vue dediee `/trades` ajoutee:
      - positions ouvertes
      - evenements de trades recents
      - raison d'ouverture (`setup` / fill maker)
  - correction du reporting runtime directionnel:
    - `Pod A` / `Pod C` publient maintenant le mark courant et le PnL latent de leurs positions ouvertes
    - `runtime_report` compte enfin ces positions dans `active_position_count` et `total_unrealized_pnl_usd`
      - raison de fermeture (`stop_hit`, `time_stop`, `opposite_signal`, etc.) quand connue
      - filtres visuels client-side:
        - `Open`
        - `Closed`
        - `Pod A`
        - `Pod B`
        - `Pod C`
    - auto-refresh du dashboard toutes les 10 secondes avec indication de la derniere mise a jour
  - univers d'observation elargi sans casser la limite WS connue de `gbot`:
    - `hyperliquid.observation_universe` separe de l'univers tradable des pods
    - sharding automatique du collector par groupes de `max_coins_per_connection = 10`
    - pacing de subscription `250ms` entre coins
    - filtrage superviseur pour empecher Pod A de trader un coin seulement observe
  - coherence deployment/runtime amelioree:
    - `--with-pod-b` / `--with-pod-c` activent aussi les pods logiquement dans le superviseur
    - l'UI ne peut plus montrer `container up` mais `enabled = no` pour un pod explicitement demande au lancement
    - `deploy.sh` affiche maintenant les services reellement demandes et des URLs publiques generiques, sans supposer a tort que l'alias SSH est resolvable dans le navigateur
  - API/dashboard relies au runtime reel:
    - `/api/state` resynchronise `pod_b_status` a chaque requete
    - Pod A et Pod C ecrivent un status runtime partage dans `logs/`
    - l'API fusionne ces status runtime pour afficher le regime et l'activite reels, au lieu d'un superviseur fige au demarrage
  - verification UI/runtime durcie:
    - `/api/metrics` et `/api/report` lisent aussi les status runtime de Pod A / Pod C
    - le dashboard utilise maintenant les memes sources runtime que `state/report/metrics`
    - la sante des pods A / C depend de la fraicheur de leur status runtime, pas seulement d'un flag `configured`
    - l'activite de trading agregée compte les trades fermes de Pod A / Pod C et les fills de Pod B
  - coherence finale state/report/runtime verrouillee:
    - `runtime_report` utilise le snapshot runtime fusionne comme source d'autorite quand il est disponible
    - `capital_plan.regime`, `cash_usd` et les allocations du report ne peuvent plus diverger du top-level `regime`
    - les blocs `pod_a_runtime.supervisor` et `pod_c_runtime.supervisor` sont normalises en vue API fusionnee, pour ne plus exposer un `pod_b_status` stale imbrique
    - les fusions runtime n'acceptent plus de fichiers de status anciens: seul un status frais peut override le snapshot du superviseur
  - smoke online valide:
    - `records_processed = 1`
    - `signal_count = 0`
    - `accepted_count = 0`
    - `messages_processed = 38`
    - `reconnect_count = 0`
- 2026-04-05: Pod B paper runner ajoute:
  - `app/trident/pod_b/paper_engine.py` cree
  - `app/trident/pod_b/paper_runner.py` cree
  - `status.json` enrichi avec:
    - `recent_fills`
    - `total_fill_count`
    - `realized_pnl_usd`
  - le manager et le parser Pod B reprennent maintenant aussi les fills du wrapper
  - validation replay locale sur snapshots reels `BTC, ETH`, date `2026-04-05`:
    - `records_processed = 402`
    - `fills_emitted = 13`
    - `total_fill_count = 13`
    - `total_position_count = 2`
    - `realized_pnl_usd = -0.0643`
    - `total_unrealized_pnl_usd = -1.4186`
- 2026-04-05: cohabitation Pod A / Pod B et wrapper live Pod B ajoutes:
  - `app/backtest/cohabitation_replay.py` cree
  - `app/trident/pod_b/paper_live_runner.py` cree
  - `launch_workdir` ajoute a la config Pod B pour les wrappers runtime reels
  - `PassivbotManager.start(...)` supporte maintenant un `cwd` explicite via config
  - validation cohabitation reelle sur snapshots `BTC, ETH, XRP`, date `2026-04-05`:
    - `records_processed = 402`
    - `ownership_conflict_count = 1`
    - `pod_a_owned_symbols = [BTC, ETH]`
    - `pod_b_owned_symbols = [XRP]`
    - `no_symbol_overlap = true`
    - `pod_a_signal_count = 1`
    - `pod_b_total_fill_count = 4`
  - smoke wrapper live Pod B:
    - `records_processed = 2`
    - `fills_emitted = 0`
    - `idle_loops = 2`
- 2026-04-05: decision d'architecture Pod B:
  - V1 retenue: moteur range natif dans `trident`
  - Passivbot conserve comme reference externe et benchmark, pas comme dependance runtime obligatoire
- 2026-04-05: reporting multi-pods ajoute:
  - `app/reporting/multi_pod.py` cree
  - `/api/report` expose un resume runtime multi-pods
  - `state_payload` inclut maintenant `runtime_report`
  - le dashboard affiche une section `Runtime pod report`
  - les summaries de cohabitation agregeent maintenant le P&L Pod A + Pod B
  - smoke HTTP valide:
    - `GET /api/report` -> OK
    - dashboard contient `Runtime pod report` et `/api/report`
- 2026-04-05: reporting detaille Pod B ajoute:
  - `app/reporting/pod_b.py` cree
  - `PodBPaperRunner` et `PodBPaperLiveRunner` ecrivent maintenant un report detaille
  - champs agreges disponibles:
    - `fills_by_symbol`
    - `fills_by_date`
    - `fill_notional_by_symbol`
    - `fill_notional_by_date`
    - `realized_pnl_by_date`
    - `inventory_skew_by_symbol`
  - replay reel verifie sur `BTC, ETH`, date `2026-04-05`:
    - `fills_by_symbol = {BTC: 6, ETH: 7}`
    - `fills_by_date = {2026-04-05: 13}`
    - `realized_pnl_by_date = {2026-04-05: -0.0643}`
- Etape 0: completee
- Etape 1: completee
- Etape 2: completee
- Etape 3: completee
- Etape 4: completee
- Etape 5: partiellement implementee, avec paper-run reel, cohabitation et wrapper live disponibles
- Fichiers crees: `pyproject.toml`, `Makefile`, `config/trident.toml`, `app/`, `tests/`
- Validation reelle effectuee:
- `python3.12 -m unittest discover -s tests -v` -> OK, `68 tests`
  - `python3.12 -m app.main --help` -> OK
  - `curl http://127.0.0.1:3010/health` -> OK
  - `curl http://127.0.0.1:3010/api/state` -> OK
  - `curl http://127.0.0.1:3010/api/metrics` -> OK
  - `curl http://127.0.0.1:3010/` -> dashboard HTML OK
  - `curl http://127.0.0.1:3010/dashboard` -> dashboard HTML OK
  - `python3.12 -m app.backtest.runner ...` -> OK
  - `python3.12 -m app.backtest.gbot_converter ...` -> OK
  - `python3.12 -m app.trident.pod_b.paper_runner ...` -> OK
  - `python3.12 -m app.trident.pod_b.paper_live_runner ...` -> OK
  - `python3.12 -m app.backtest.cohabitation_replay ...` -> OK
  - miroir local des donnees historiques verifie:
    - source `/workspaces/gbot/data`
    - cible `/workspaces/trident/data/gbot_archive`
    - taille `68M`
    - `29 fichiers` copies
  - miroir local `server-data` verifie:
    - source `/workspaces/gbot/server-data`
    - cible `/workspaces/trident/data/server_archive`
    - taille `3.0G`
    - `173 fichiers`
    - sync incrementale `2026-04-05`:
      - `24 fichiers` ajoutes
      - `22 fichiers` mis a jour
- Limite actuelle:
  - le runtime HTTP reste volontairement en stdlib simple
  - l'execution exchange reelle n'est pas encore activee dans `trident`
  - les artefacts Docker sont prets mais non verifies ici faute de binaire `docker`

### Tableau de pilotage

| Etape | Avancement | Prochain milestone |
|-------|------------|--------------------|
| 0. Cadrage et branchement | 100% | Rien, etape fermee |
| 1. Superviseur + ownership | 100% | Rien, etape fermee |
| 2. Regime allocator deterministe | 100% | Rien, etape fermee |
| 3. Capital allocator + cash mode | 100% | Rien, etape fermee |
| 4. Pod A minimal | 100% | Rien, etape fermee |
| 5. Pod B range engine natif | 100% | Rien, etape fermee |
| 6. Reporting par pod | 100% | Rien, etape fermee |
| 7. Research Pod pour Pod C | 100% | Rien, etape fermee |
| 8. Pod C minimal | 100% | Rien, etape fermee |
| 9. Hardening deployment | 100% | Rien, etape fermee |
| 10. Passage live progressif | 20% | Lancer le premier dry-run 24h sur serveur et auditer avec `trident_dry_run_review.sh` |

Regle de maintenance:

- ce tableau doit etre mis a jour apres chaque modification significative du repo ou du plan.

---

## 1. Resume executif

TRIDENT est un systeme de trading compose de 3 pods live et 1 pod research:

- `Pod A — Anchor Trend`: moteur swing / trend following, base sur les lecons de `t-bot`.
- `Pod B — Range Harvester`: moteur maker/grid natif pour les marches plats.
- `Pod C — Event Raider`: moteur evenementiel, actif uniquement sur des patterns mesurables.
- `Research Pod`: espace d'experimentation offline. Rien ne passe live sans preuve.

TRIDENT n'utilise pas de LLM dans la boucle de decision live.
Le "brain" est un allocateur deterministe de regime et de capital.

Le systeme ne doit pas:

- ouvrir plusieurs strategies sur le meme coin sans regle explicite d'ownership,
- compter sur des projections de P&L additives non realistes,
- faire du microstructure directionnel < 60s tant qu'un edge net n'a pas ete prouve.

---

## 2. Base factuelle issue du projet actuel

### 2.1. Ce qui est retenu

- Le seul alpha documente comme positif est le swing `t-bot` en HTF.
- Le microstructure directionnel sub-minute de `gbot` n'a pas montre d'expectancy positive nette apres frais.
- Les fees Hyperliquid rendent les horizons tres courts fragiles si l'execution n'est pas majoritairement maker.
- Le marche range doit etre traite via quote placement / grid / inventory management, pas via prediction directionnelle naive.
- Le repo actuel fournit une reference utile pour les integrations Hyperliquid, le schema de donnees, le dashboard et les outils de backtest, meme si TRIDENT doit etre ecrit en Python.
- Le repo peut conserver `passivbot/` comme reference de benchmark, mais TRIDENT ne doit pas en dependre pour son runtime V1.

### 2.2. Contraintes non negociables

- Une seule position nette par coin sur Hyperliquid.
- L'allocation par pod doit eviter tout conflit de position.
- Toute etape de dev doit avoir un critere de validation quantifie.
- Aucun passage live sans paper trading / dry-run prealable.
- Le mode "cash" est un etat valide. Le bot n'a pas besoin d'etre expose en permanence.

---

## 3. Architecture cible

### 3.1. Vue d'ensemble

```text
                          +----------------------+
                          |   Regime Allocator   |
                          |  deterministic only  |
                          +----------+-----------+
                                     |
                   +-----------------+-----------------+
                   |                 |                 |
                   v                 v                 v
          +----------------+ +----------------+ +----------------+
          | Pod A          | | Pod B          | | Pod C          |
          | Anchor Trend   | | Range Harv.    | | Event Raider   |
          | Python native  | | Python native  | | Python native  |
          +--------+-------+ +--------+-------+ +--------+-------+
                   |                  |                  |
                   +------------------+------------------+
                                      |
                             +--------v--------+
                             | Risk Supervisor |
                             | ownership, DD,  |
                             | kill switches   |
                             +--------+--------+
                                      |
                             +--------v--------+
                             | Execution Layer |
                             | HL REST / WS    |
                             +--------+--------+
                                      |
                             +--------v--------+
                             | Data / Journal  |
                             | Metrics / UI    |
                             +-----------------+
```

### 3.2. Choix de stack

- `Moteur principal`: Python 3.12+.
- `Pod B`: moteur range natif, pilote par TRIDENT comme les autres pods.
- `Recherche`: Python, notebooks / scripts / DuckDB / Polars.
- `Observabilite`: bootstrap HTTP stdlib en phase 0, puis FastAPI + SSE ou WebSocket + Prometheus a partir de la phase de hardening.
- `Execution Hyperliquid`: Python async (`aiohttp`, `websockets`, signer EIP-712).

### 3.2.1. Pourquoi Python

Python est le langage le plus pertinent pour TRIDENT car:

- les horizons vises ne sont pas du HFT ultra-latence,
- les pods A et C ont besoin de vitesse d'iteration plus que de performance brute,
- le pod B doit partager les memes primitives de config, supervision et observabilite que les autres pods,
- la recherche, le backtest, les notebooks et l'optimisation seront en Python,
- une equipe pourra maintenir plus vite un orchestrateur Python qu'un systeme multi-pods Rust.

### 3.3. Pourquoi cette architecture

- Elle minimise le temps de dev en alignant recherche, backtest et live sur le meme langage.
- Elle permet de separer clairement les responsabilites.
- Elle force une discipline d'allocation et d'ownership.
- Elle rend possible un deploiement progressif: A puis B puis C.
- Elle evite qu'un moteur externe opaque devienne le point de friction principal de l'ops quotidienne.

---

## 4. Regimes et allocation

### 4.1. Regimes officiels

TRIDENT utilise 4 regimes explicites plus un etat implicite cash:

| Regime | Description | Pods autorises |
|--------|-------------|----------------|
| `TrendExpansion` | marche directionnel propre | A, C |
| `RangeAuction` | range propre, vol moderee | B |
| `PanicSqueeze` | vol forte, acceleration, squeeze possible | C |
| `DeadZone` | faible amplitude, faible edge | B taille reduite ou cash |
| `Cash` | aucune exposition voulue | aucun |

### 4.2. Allocation cible

| Regime | Pod A | Pod B | Pod C | Cash |
|--------|-------|-------|-------|------|
| `TrendExpansion` | 60% | 10% | 30% | 0% |
| `RangeAuction` | 20% | 70% | 10% | 0% |
| `PanicSqueeze` | 10% | 0% | 90% | 0% |
| `DeadZone` | 0% | 20% | 0% | 80% |

### 4.3. Regles de regime

Le regime doit etre calcule sans LLM, via regles simples:

- `TrendExpansion` si ADX >= seuil et ATR normalisee > seuil et structure HTF favorable.
- `RangeAuction` si ADX bas, ATR moderee, prix contenu dans une enveloppe stable.
- `PanicSqueeze` si ATR explose, funding/oi changent vite, impulsions anormales sur BTC/ETH.
- `DeadZone` si vol et amplitudes sont trop faibles pour couvrir le cout reel de trading.

La premiere version doit utiliser uniquement des features presentes dans le repo ou simples a ajouter:

- candles 15m / 1h / 4h,
- ADX,
- ATR ratio,
- distance a EMA50 / EMA200,
- largeur de range recent,
- vol realisee,
- event flags BTC / ETH.

---

## 5. Ownership des coins

### 5.1. Regle absolue

Un coin ne peut etre possede que par un seul pod a la fois.

### 5.2. Mode de fonctionnement

- Le `Regime Allocator` assigne a chaque pod un univers de symbols.
- Si un pod a deja une position ou des ordres resting sur un coin, aucun autre pod ne peut toucher ce coin.
- Si Passivbot tourne sur sous-compte separe, l'ownership est separe par compte.
- Si Passivbot tourne sur le meme compte, il doit recevoir une liste de coins disjoints de A et C.

### 5.3. Ordre de priorite

- Priorite 1: Pod C, car evenementiel et temps sensible.
- Priorite 2: Pod A, car swing a plus forte conviction.
- Priorite 3: Pod B, car moteur d'occupation / revenu de range.

---

## 6. Description detaillee des pods

## 6.1. Pod A — Anchor Trend

### Role

Moteur principal de tendance. Il doit produire la majorite du P&L lors des phases directionnelles.

### Source d'inspiration

- `t-bot` et ses concepts Smart Money / structure / VWAP.
- Jesse / Freqtrade pour la discipline d'entree-sortie et de validation.

### Horizon

- Contexte: 4H et 1H
- Timing: 15m
- Hold: plusieurs heures a 2 jours

### Setups V1

- Trend continuation apres pullback sur EMA / VWAP
- Liquidity sweep + reclaim
- Break of structure avec confirmation BTC
- Deviation VWAP / retour en tendance

### Filtres V1

- BTC regime compatible avec le setup
- Funding pas extremement oppose
- Spread / liquidite minimum
- Pas de trade si regime global = `DeadZone`

### Sorties V1

- Stop structurel et non ultra-serre
- TP partiel optionnel uniquement si backtest le justifie
- Pas de trailing agressif par defaut
- Time stop max configurable

### Implementation precise

Creer dans le repo:

- `app/trident/pod_a/__init__.py`
- `app/trident/pod_a/signals.py`
- `app/trident/pod_a/filters.py`
- `app/trident/pod_a/exits.py`
- `app/trident/pod_a/service.py`

Reutiliser si possible:

- les schemas de donnees existants,
- les conventions de logs et de journaux du repo,
- les scripts de recherche existants comme point de depart,
- le dashboard actuel comme reference fonctionnelle.

Ajouter:

- un `CandleService` pour charger / bufferiser les candles 15m, 1h, 4h,
- un `MarketContextService` pour BTC, ETH, funding, ATR, ADX,
- un `PodIntent` standardise pour integrer A, B et C dans le meme superviseur.
- un `HyperliquidClient` async partage pour REST et WS.

Arborescence cible V1:

```text
app/
  main.py
  trident/
    supervisor.py
    regime_allocator.py
    capital_allocator.py
    symbol_registry.py
    kill_switch.py
    types.py
    pod_a/
    pod_b/
    pod_c/
  exchange/
    hyperliquid_client.py
    ws_client.py
    signer.py
    rate_limiter.py
  risk/
    manager.py
  persistence/
    journal.py
    parquet.py
  observability/
    api.py
    metrics.py
  backtest/
    runner.py
    reports.py
```

### Validation V1

- Backtest 6 mois ou echantillon multi-regimes suffisant.
- Dry-run 7 jours minimum.
- Critere go:
  - profit factor > 1.2,
  - drawdown max < 15%,
  - expectancy nette positive,
  - pas de degradation evidente entre backtest et dry-run.

---

## 6.2. Pod B — Range Harvester

### Role

Capturer les mouvements de va-et-vient et rebates maker pendant les regimes plats.

### Source d'inspiration

- Passivbot pour la logique grid.
- Hummingbot PMM / Avellaneda pour l'inventory skew et la prudence en MM.

### Decision cle

Ne pas reimplementer V1 en Python. Utiliser Passivbot deja present dans `passivbot/`.

### Mode V1

- execution sur sous-ensemble fixe de coins liquides,
- seulement en regime `RangeAuction` ou `DeadZone`,
- pas de martingale profonde,
- pas de DCA infini,
- wallet exposure limitee,
- ordres maker uniquement autant que possible.

### Parametres V1 cibles

- 2 a 5 coins max
- faible exposition par coin
- spacing et wallet exposure calibres via optimizer Passivbot
- auto-pause si ATR / vol / spread sortent du domaine normal

### Integration dans TRIDENT

TRIDENT ne pilote pas chaque ordre de Passivbot.
TRIDENT pilote:

- l'etat `enabled/disabled`,
- la liste de coins autorises,
- le capital autorise,
- les alertes et kill switches.

### Implementation precise

Creer:

- `app/trident/pod_b/__init__.py`
- `app/trident/pod_b/passivbot_manager.py`
- `app/trident/pod_b/config_renderer.py`
- `app/trident/pod_b/status_parser.py`
- `app/trident/pod_b/models.py`

Responsabilites:

- generer les configs Passivbot a partir du regime et du capital alloue,
- lancer / arreter le process ou container Passivbot,
- parser l'etat de sante et les positions,
- remonter les metriques dans le dashboard principal.

Scripts a ajouter:

- `scripts/passivbot_render_config.py`
- `scripts/passivbot_start.sh`
- `scripts/passivbot_stop.sh`

### Validation V1

- Optimizer offline sur donnees historiques pertinentes.
- Paper trading 72h minimum.
- Critere go:
  - P&L net positif,
  - drawdown max < 8% sur la fenetre de test,
  - fill rate et inventory control juges sains,
  - aucun conflit avec les autres pods.

---

## 6.3. Pod C — Event Raider

### Role

Chercher la convexite: peu de trades, forte asymetrie, uniquement lors d'evenements mesurables.

### V1 autorisee

Une seule famille d'evenements doit passer live dans la premiere version.
La meilleure candidate initiale est:

- `BTC/ETH Impulse -> Alt Catch-up`

### Idee

Si BTC ou ETH imprime une impulsion anormale, certaines alts liquides peuvent suivre avec un retard mesurable.
Cette idee doit etre prouvee hors ligne avant toute implementation live.

### Ce qui ne doit pas etre fait en V1

- pas de pseudo-carte de liquidations "reconstruite" sans source fiable,
- pas de funding carry directionnel presente comme arbitrage,
- pas de microstructure < 60s si les tests ne montrent pas d'expectancy nette.

### Pipeline research obligatoire

Mesures offline a produire:

- correlation croisee decalee BTC/ETH -> alt,
- distribution des lags par coin,
- expectancy brute et nette apres fees,
- fill probability maker vs maker+taker defensif,
- robustesse par regime.

### Implementation precise

Creer:

- `app/trident/pod_c/__init__.py`
- `app/trident/pod_c/lead_lag.py`
- `app/trident/pod_c/event_filters.py`
- `app/trident/pod_c/exits.py`
- `app/trident/pod_c/service.py`

Ajouter en recherche:

- `research/trident/lead_lag_study.py`
- `research/trident/event_replay.py`

### Validation V1

Avant code live:

- rapport offline signe,
- dataset multi-jour / multi-regime,
- tests avec fees realistes.

Critere go live:

- expectancy nette > 0,
- signal stable sur plusieurs coins,
- drawdown acceptable,
- nombre de trades non nul mais non excessif.

---

## 6.4. Research Pod

### Role

Zone de test et de preuve.

### Sujets autorises

- lead-lag cross-coin
- funding + OI + premium comme filtre
- event detection BTC / ETH
- breadth de marche
- selection dynamique d'univers

### Regle

Une idee ne passe de research a live que si elle produit:

- une note de design,
- un script reproductible,
- un rapport avec frais inclus,
- une recommandation go/no-go.

---

## 7. Superviseur central

## 7.1. Responsabilites

Le superviseur central coordonne:

- le regime global,
- l'allocation de capital,
- l'ownership des coins,
- les kill switches,
- la remontee d'etat et de metriques.

## 7.2. Modules a creer

- `app/trident/__init__.py`
- `app/trident/supervisor.py`
- `app/trident/regime_allocator.py`
- `app/trident/capital_allocator.py`
- `app/trident/symbol_registry.py`
- `app/trident/kill_switch.py`
- `app/trident/types.py`
- `app/trident/config.py`

## 7.3. Interfaces internes

Chaque pod expose:

- `health() -> PodHealth`
- `desired_symbols() -> list[str]`
- `allocated_capital() -> Decimal`
- `current_positions() -> list[PodPosition]`
- `tick(context) -> list[PodIntent]`
- `on_fill(fill)`
- `on_regime_change(regime)`
- `shutdown()`

## 7.4. Kill switches

Kill switches obligatoires:

- max daily loss global,
- max drawdown global,
- drawdown par pod,
- stale market data,
- boucle d'erreurs exchange,
- desynchronisation etat interne / exchange,
- conflit d'ownership detecte.

---

## 8. Donnees, stockage et backtest

### 8.1. Formats

Conserver les JSONL actuels pour la journalisation simple.
Ajouter progressivement Parquet pour les datasets d'analyse.

### 8.2. Jeux de donnees minimum

- candles 15m / 1h / 4h
- funding history
- mids / books / trades si necessaire pour Pod C
- ordres, fills, pnl, regime events

### 8.3. Replay / backtest

Le backtest doit pouvoir rejouer:

- regime,
- allocations,
- decisions par pod,
- conflits de capital,
- ownership des coins.

### 8.4. Ce qu'il faut ajouter

- `app/backtest/trident_runner.py`
- `app/backtest/allocation_sim.py`
- `app/backtest/pod_report.py`
- `app/backtest/slippage.py`

### 8.5. Rapports obligatoires

Produire pour chaque run:

- P&L par pod,
- P&L global,
- drawdown par pod et global,
- temps en cash,
- nombre de conflits d'ownership,
- breakdown par coin,
- breakdown par regime.

---

## 9. UI et observabilite

### 9.1. Dashboard

Le dashboard actuel doit etre etendu, pas remplace.

Nouveaux panneaux:

- regime courant,
- allocation par pod,
- etat de sante de chaque pod,
- ownership des coins,
- P&L par pod,
- statut Passivbot,
- kill switches actifs.

### 9.2. Metriques Prometheus

Ajouter:

- `trident_regime_current`
- `trident_pod_equity{pod=...}`
- `trident_pod_pnl_total{pod=...}`
- `trident_pod_drawdown{pod=...}`
- `trident_symbol_owner{symbol=...,pod=...}`
- `trident_kill_switch_total{reason=...}`
- `trident_passivbot_enabled`

### 9.3. Logs

Tous les logs doivent inclure:

- `pod`
- `symbol`
- `regime`
- `allocation_pct`
- `owner`
- `decision_id`

---

## 10. Configuration

### 10.1. Fichier principal

Ajouter un nouveau fichier:

- `config/trident.toml`
- `.env.trident.example`
- `pyproject.toml`
- `Makefile`

### 10.2. Sections

```toml
[general]
mode = "observation"

[trident]
enabled = true

[trident.regime]
adx_trend_threshold = 25
atr_ratio_panic_threshold = 1.8
dead_zone_atr_threshold = 0.6

[trident.allocations.trend_expansion]
pod_a = 0.60
pod_b = 0.10
pod_c = 0.30

[trident.allocations.range_auction]
pod_a = 0.20
pod_b = 0.70
pod_c = 0.10

[trident.allocations.panic_squeeze]
pod_a = 0.10
pod_b = 0.00
pod_c = 0.90

[trident.allocations.dead_zone]
pod_a = 0.00
pod_b = 0.20
pod_c = 0.00
cash = 0.80

[pod_a]
enabled = true
symbols = ["BTC", "ETH", "SOL", "HYPE"]

[pod_b]
enabled = false
symbols = ["DOGE", "XRP", "SUI"]
passivbot_config_path = "./runtime/passivbot/live.json"

[pod_c]
enabled = false
leader_symbols = ["BTC", "ETH"]
follower_symbols = ["SOL", "HYPE", "SUI"]
```

### 10.3. Regle de config

Chaque pod doit pouvoir etre:

- active ou desactive,
- limite en capital,
- limite en univers de symbols,
- isole du reste pour tests.

### 10.4. Packaging Python recommande

Le projet cible doit utiliser `uv` pour la gestion d'environnement et des dependances.
Le bootstrap actuel reste volontairement executable sans dependances externes, afin de ne pas bloquer les premieres etapes.

Dependances minimales a declarer dans `pyproject.toml`:

- `aiohttp`
- `websockets`
- `orjson`
- `pydantic`
- `pydantic-settings`
- `python-dotenv`
- `fastapi`
- `uvicorn`
- `prometheus-client`
- `structlog`
- `pandas`
- `polars`
- `duckdb`
- `numpy`
- `ta`
- `eth-account`
- `pyarrow`
- `pytest`
- `pytest-asyncio`

Etat actuel:

- `pyproject.toml` minimal en place
- runtime bootstrap sans dependance externe
- tests actuels lances avec `unittest`

Scripts de dev recommandes dans `Makefile`:

```make
install:
	uv sync

run-observation:
	uv run python -m app.main --mode observation --profile trident

run-dry:
	uv run python -m app.main --mode dry-run --profile trident

run-live:
	uv run python -m app.main --mode live --profile trident

backtest:
	uv run python -m app.backtest.runner --profile trident

test:
	uv run pytest
```

En attendant l'installation des dependances de dev, les commandes qui fonctionnent deja sont:

```make
run-observation-stdlib:
	python3.12 -m app.main --mode observation --profile trident

test-stdlib:
	python3.12 -m unittest discover -s tests -v
```

---

## 11. Deploiement cible

## 11.1. Mode local dev

Processus:

- `trident-core` via `uv run python -m app.main`
- `passivbot` lance manuellement ou par script si `pod_b.enabled = true`

Preparation locale:

```bash
uv sync
cp .env.trident.example .env
uv run python -m app.main --mode observation --profile trident
```

Bootstrap actuellement valide:

```bash
python3.12 -m app.main --help
python3.12 -m unittest discover -s tests -v
```

## 11.2. Mode serveur simple

Deploiement recommande V1:

- un serveur Hetzner ou equivalent,
- un container `trident-core`,
- un container `passivbot` optionnel,
- volumes persistants pour `data/` et `runtime/`,
- tunnel SSH pour UI,
- pas d'exposition publique du dashboard.

## 11.3. Docker compose cible

Fichiers a ajouter:

- `docker-compose.trident.yml`
- `.env.trident.example`
- `Dockerfile.trident`

Services:

- `trident-core`
- `passivbot`
- `prometheus` optionnel
- `grafana` optionnel

### 11.3.1. Dockerfile cible

Le container principal doit etre Python-first et minimal.

Exemple de structure:

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml uv.lock* ./
RUN pip install uv && uv sync --frozen --no-dev

COPY app ./app
COPY config ./config
COPY static ./static
COPY scripts ./scripts

RUN mkdir -p /app/data /app/runtime /app/logs

EXPOSE 3000

CMD ["uv", "run", "python", "-m", "app.main", "--mode", "dry-run", "--profile", "trident"]
```

### 11.3.2. docker-compose.trident.yml

Le compose doit:

- monter `data/`, `runtime/`, `logs/`,
- fournir les variables d'environnement de `.env`,
- lancer `passivbot` seulement si `pod_b.enabled = true`,
- definir un healthcheck HTTP sur `/health`.

## 11.4. Volumes

- `/opt/trident/data`
- `/opt/trident/runtime`
- `/opt/trident/logs`

## 11.5. Secrets

Variables a documenter:

- `GBOT__EXCHANGE__WALLET_ADDRESS`
- `GBOT__EXCHANGE__AGENT_PRIVATE_KEY`
- `TRIDENT__PASSIVBOT__API_KEY` si necessaire

## 11.6. Health checks

Le deploiement doit verifier:

- process vivant,
- WS connecte,
- donnees non stale,
- dashboard disponible,
- Passivbot present si active.

### 11.7. Strategie de deploiement

Ordre recommande:

1. `observation` sur serveur
2. `dry-run` avec Pod A seulement
3. `dry-run` avec Pod A + Pod B
4. `live` taille minimale avec Pod A
5. extension progressive

### 11.8. Commandes ops minimales

```bash
# installer
uv sync

# lancer le core
uv run python -m app.main --mode dry-run --profile trident

# lancer le stack docker
docker compose -f docker-compose.trident.yml up -d

# logs
docker compose -f docker-compose.trident.yml logs -f trident-core

# arret
docker compose -f docker-compose.trident.yml down
```

---

## 12. Plan de dev, etape par etape

Chaque etape doit etre terminee, testee et validee avant la suivante.

## Etape 0 — Cadrage et branchement

### Statut

`TERMINEE`

### Objectif

Preparar le repo pour TRIDENT sans casser `gbot`.

### Travail

- creer le package `app/trident/`
- ajouter `config/trident.toml`
- ajouter un point d'entree `app/main.py`
- ajouter des flags de mode pour lancer `legacy` ou `trident`
- creer `plan_trident.md` comme reference

Travail realise:

- package `app/` cree
- package `app/trident/` cree
- `app/main.py` cree
- `app/settings.py` cree
- `app/observability/api.py` cree
- `app/trident/supervisor.py`, `symbol_registry.py`, `types.py`, `capital_allocator.py`, `regime_allocator.py`, `kill_switch.py` crees
- `config/trident.toml` cree
- `tests/test_health.py` et `tests/test_symbol_registry.py` crees
- `README.md`, `.env.trident.example`, `Makefile`, `pyproject.toml`, `.gitignore` ajoutes

### Validation

- l'environnement Python s'installe proprement,
- `gbot` legacy continue de fonctionner,
- un mode `trident` demarre et expose `/health`.

Validation reelle effectuee:

- `python3.12 -m unittest discover -s tests -v` -> OK
- `python3.12 -m app.main --help` -> OK
- `python3.12 -m app.main --mode observation --profile trident --port 3010` -> OK
- `curl http://127.0.0.1:3010/health` -> OK
- `curl http://127.0.0.1:3010/api/state` -> OK

Validation restante:

- installation `uv sync`
- integration mode legacy si necessaire

### Go / no-go

- go si zero regression legacy.

Decision actuelle:

- `GO` pour passer a l'etape 1 en parallele
- `Etape 0` peut etre fermee

---

## Etape 1 — Superviseur vide + ownership

### Statut

`TERMINEE`

### Objectif

Construire l'ossature de coordination sans strategie active.

### Travail

- implementer `supervisor.py`
- implementer `symbol_registry.py`
- ajouter types `PodIntent`, `PodHealth`, `PodPosition`
- brancher les metriques de base

Travail realise:

- `ConfiguredPod` ajoute dans `app/trident/pod_runtime.py`
- le superviseur construit maintenant les pods configures
- synchronisation d'ownership automatique au demarrage
- priorite d'ownership implementee: `pod_c` > `pod_a` > `pod_b`
- conflits d'ownership exposes dans le snapshot
- snapshot par pod ajoute: `desired_symbols`, `owned_symbols`, `enabled`
- historique des transitions de regime expose dans le snapshot superviseur
- compteurs de supervision derives exposes:
  - `enabled_pod_count`
  - `healthy_pod_count`
  - `ownership_conflict_count`
  - `owned_symbol_count`
  - `pod_a_preview_count`
  - `pod_b_managed_symbol_count`
  - `pod_b_process_running`
  - `regime_transition_count`
  - `regime_evaluation_count`
- route HTTP `GET /api/metrics` ajoutee
- dashboard HTML minimal ajoute:
  - `GET /`
  - `GET /dashboard`
  - sections lisibles pour pods, ownership, conflits, historique de regime, metriques
- test de conflits multi-pods ajoute

### Validation

- tests unitaires sur ownership des coins,
- simulation de conflit entre pods,
- dashboard affiche le pod owner par coin.

Validation reelle effectuee:

- `python3.12 -m unittest discover -s tests -v` -> OK
- test unitaire de cycle claim/release -> OK
- test unitaire de priorite et conflits superviseur -> OK
- `/api/state` expose bien `symbol_ownership`, `ownership_conflicts`, `pods`, `regime_history`, `metrics` -> OK
- `/api/metrics` expose les compteurs derives -> OK
- dashboard HTML verifie:
  - titre `TRIDENT Supervisor Dashboard`
  - sections `Symbol ownership`, `Ownership conflicts`, `Regime history`
  - endpoints `/` et `/dashboard` repondent

Validation restante:

- enrichir encore les metriques par pod avec utilisation de capital / P&L live quand les pods tourneront reellement

### Go / no-go

- go si aucun conflit non detecte en test.

Decision actuelle:

- Etape 1 fermee

---

## Etape 2 — Regime allocator deterministe

### Statut

`TERMINEE`

### Objectif

Avoir un brain simple et testable.

### Travail

- implementer `regime_allocator.py`
- ajouter calcul ADX / ATR / EMA / range width
- produire un `RegimeSnapshot`

Travail realise:

- `RegimeSnapshot` ajoute dans `app/trident/types.py`
- `RegimeAllocator.classify()` implemente
- classification deterministe des 5 etats:
  - `Cash`
  - `DeadZone`
  - `RangeAuction`
  - `TrendExpansion`
  - `PanicSqueeze`
- historique des changements de regime conserve dans le superviseur
- compteurs d'evaluation et de transition exposes par l'API
- replay historique reel branche via `ArchiveReplayRunner`
- hysteresis stateful ajoutee dans `RegimeAllocator.resolve(...)`
- distinction exposee entre:
  - `raw_regime`
  - `regime` effectif
  - `pending_regime`
  - `pending_regime_count`
- confirmations de transition rendues configurables
- report de replay enrichi avec:
  - `records_by_date`
  - `signals_by_date`
  - `accepted_by_date`
  - `rejected_by_date`
  - `regime_transition_count`
  - `regime_transitions`
  - `regime_transitions_by_date`
  - `pnl_by_date`
- tests unitaires synthetiques ajoutes pour chaque regime

Travail restant:

- ajouter les composantes EMA / contexte HTF

### Validation

- tests unitaires sur cas synthetiques,
- replay de quelques journees historiques avec etiquetage visuel,
- verification que le nombre de switches n'est ni nul ni absurde.

Validation reelle effectuee:

- `python3.12 -m unittest discover -s tests -v` -> OK
- classification synthetique verifiee:
  - `cash -> Cash`
  - `dead -> DeadZone`
  - `range -> RangeAuction`
  - `trend -> TrendExpansion`
  - `panic -> PanicSqueeze`
- historique du superviseur verifie:
  - `Cash -> DeadZone`
  - `DeadZone -> TrendExpansion`
  - `regime_transition_count = 2`
  - `regime_evaluation_count = 2`
- replay reel deja branche sur `/workspaces/trident/data/server_archive`
- replay reel etendu verifie:
  - dates `2026-04-01 -> 2026-04-05`
  - coins `BTC, ETH, SOL, HYPE`
  - `records_processed = 5151`
  - `records_by_date = {'2026-04-01': 438, '2026-04-02': 1441, '2026-04-03': 1431, '2026-04-04': 1440, '2026-04-05': 401}`
  - sans hysteresis:
    - `regime_transition_count = 144`
  - avec hysteresis retenue `(3,1,1)`:
    - `regime_transition_count = 93`
    - `signal_count = 36`
    - `accepted_count = 28`
    - `realized_pnl_usd = 21.01`
  - conclusion immediate:
    - le classifieur produit maintenant des regimes plausibles et nettement plus stables
    - la stabilisation ne coupe pas la capacite de Pod A a travailler sur ce replay

Validation restante:

- ajout d'un `RegimeSnapshot` alimente par un vrai pipeline de features

### Go / no-go

- go si les regimes paraissent stables et interpretable par un humain.

Decision actuelle:

- Etape 2 fermee

---

## Etape 3 — Capital allocator + cash mode

### Statut

`TERMINEE`

### Objectif

Allouer le capital sans execution.

### Travail

- implementer `capital_allocator.py`
- lier regime -> allocation
- introduire limites par pod et par coin

Travail realise:

- `CapitalPlan`, `PodAllocation`, `SymbolAllocation` ajoutes
- `trident.capital` ajoute dans `config/trident.toml`
- caps par pod ajoutes dans `pod_a`, `pod_b`, `pod_c`
- `CapitalAllocator.build_plan()` implemente
- retour automatique au cash si:
  - pod desactive
  - pod sans symbols possedes
  - allocation par symbol trop faible
  - cap par symbol depasse
- le superviseur expose maintenant:
  - `capital_plan`
  - `regime_snapshot`
  - `target_pct` / `target_usd` par pod

### Validation

- tests sur transitions de regime,
- rapport d'allocation sur replay,
- aucun depassement de 100% du capital.

Validation reelle effectuee:

- `python3.12 -m unittest discover -s tests -v` -> OK
- test de redistribution au cash pour pods desactives -> OK
- test de cap par symbol -> OK
- verification synthetique:
  - `pod_c_target_pct = 0.5`
  - `cash_pct = 0.5`
  - 2 symbols a `0.25` chacun

Validation restante:

- replay historique avec snapshots reels
- ajout de limites de capital dynamiques selon drawdown / performance pod

### Go / no-go

- go si les allocations restent coherentes et deterministes.

Decision actuelle:

- GO pour demarrer l'etape 4

---

## Etape 4 — Pod A minimal

### Statut

`TERMINEE`

### Objectif

Mettre live la premiere source d'alpha structurelle.

### Travail

- creer `pod_a`
- implementer 1 setup seulement en V1:
  - trend pullback + contexte HTF
- brancher execution, journaling, risk

Travail realise:

- `app/trident/pod_a/signals.py` cree
- `app/trident/pod_a/filters.py` cree
- `app/trident/pod_a/exits.py` cree
- `app/trident/pod_a/context.py` cree
- `app/trident/pod_a/planner.py` cree
- `app/trident/pod_a/service.py` cree
- `app/risk/pod_a_gate.py` cree
- `app/portfolio/pod_a_state.py` cree
- `app/persistence/journal.py` cree
- `app/backtest/snapshot_loader.py` cree
- `app/backtest/pod_a_executor.py` cree
- `app/backtest/gbot_converter.py` cree
- `app/backtest/pod_report.py` cree
- `app/backtest/pod_a_runner.py` cree
- `app/backtest/runner.py` cree
- `app/backtest/archive_replay.py` cree
- `app/execution/dry_run.py` cree
- `app/live/pod_a_live_runner.py` cree
- `AnchorTrendContext` et `AnchorTrendSignal` ajoutes
- `AnchorTrendService.evaluate()` implemente avec:
  - setup long
  - setup short
  - filtres de regime / spread / funding / alignement BTC
- `MarketContextService` implemente pour convertir des snapshots generiques en contextes Pod A
- preview des signaux Pod A branche dans le superviseur
- `AnchorTrendPlanner` implemente pour transformer un signal en `TradePlan`
- pipeline superviseur ajoute:
  - `preview_pod_a_signals(...)`
  - `build_pod_a_trade_plans(...)`
- `PodARiskGate` implemente avec:
  - `min_confidence`
  - `max_trade_plans_per_batch`
  - `min_trade_notional_usd`
- `PodAPortfolioState` implemente:
  - ouverture de position
  - fermeture sur stop
  - fermeture sur signal oppose
  - fermeture sur time stop
  - fermeture de fin de backtest
- `PodAExecutor` implemente pour executer les plans acceptes en dry-run avec:
  - slippage
  - crossing du spread
  - frais taker
  - fills explicites
- loader JSONL pour snapshots Pod A implemente
- validation stricte du schema d'entree JSONL implemente
- convertisseur `gbot L2 + trades -> TRIDENT snapshots` implemente
- report Pod A de backtest implemente:
  - `signals_by_symbol`
  - `signals_by_side`
  - `signals_by_setup`
  - `signals_by_regime`
  - `average_confidence`
  - `accepted_count`
  - `rejected_count`
  - `rejections_by_reason`
  - `closed_trade_count`
  - `win_count`
  - `loss_count`
  - `realized_pnl_usd`
  - `close_reasons`
- journal de signaux enrichi:
  - `timestamp`
  - `source_file`
  - `symbol_snapshot`
  - `regime_snapshot`
  - `risk.accepted`
  - `risk.reason`
  - `risk.target_notional_usd`
  - `risk.stop_bps`
  - `execution.opened`
  - `execution.fills`
- `PodAPortfolioState` enrichi avec:
  - `entry_fee_usd`
  - `gross_pnl_usd`
  - `fees_usd`
  - `pnl_usd` net
- configuration d'execution dry-run ajoutee dans `config/trident.toml`:
  - `dry_run_taker_fee_bps`
  - `dry_run_slippage_bps`
  - `dry_run_spread_multiplier`
- recalibrage regime / setup applique apres replay reel:
  - `adx_trend_threshold = 22`
  - `trend_structure_threshold = 0.30`
  - `dead_zone_atr_threshold = 0.45`
  - `dead_zone_range_threshold = 80`
  - `atr_ratio` du convertisseur rescale via `range_width_bps / 30`
  - setup Pod A assoupli a `structure_score >= 0.40` et pullback VWAP `25 bps`
- scoring de confiance Pod A enrichi:
  - `structure_quality`
  - `trend_quality`
  - `pullback_quality`
  - `spread_quality`
  - `funding_quality`
  - confidence finale = agregat pondere de ces composantes
- le convertisseur `gbot` produit maintenant aussi:
  - `book_imbalance`
  - `trade_flow_bias`
  - `bucket_volume`
  - `bucket_trade_count`
  - `bucket_range_bps`
  - `source`
- `ArchiveReplayRunner` implemente:
  - conversion archive locale `l2 + trades -> snapshots`
  - replay backtest Pod A sur une plage de dates
  - emission d'un report JSON
  - emission optionnelle d'un journal JSONL
- le reporting Pod A expose maintenant aussi:
  - `records_by_regime`
  - `records_by_date`
  - `opened_count`
  - `skipped_open_count`
  - `gross_pnl_usd`
  - `fees_usd`
  - `average_hold_hours`
  - `signals_by_date`
  - `accepted_by_date`
  - `rejected_by_date`
  - `regime_transition_count`
  - `regime_transitions`
  - `regime_transitions_by_date`
  - `trades_by_symbol`
  - `pnl_by_symbol`
  - `pnl_by_date`
  - `closed_trade_log`
- le journal Pod A expose maintenant aussi:
  - `signal.confidence_components`
  - evenements `trade` pour les fermetures
  - `event_type = signal | trade_close`
  - `execution.had_open_position_before`
  - `execution.has_open_position_after`
  - `execution.skipped_open`
  - `execution.close_reason`
  - `execution.open_fills`
  - `execution.close_fills`
- CLI ajoutee:
  - `python3.12 -m app.backtest.archive_replay ...`
  - `uv run python -m app.live.collector ...`
  - `uv run python -m app.live.pod_a_live_runner ...`

Validation reelle effectuee:

- `python3.12 -m unittest discover -s tests -v` -> OK, `34 tests`
- cas synthetiques verifiés:
  - long -> signal long
  - short -> signal short
  - regime non trending -> rejet
- preview superviseur verifie:
  - `preview_count = 1`
  - premier signal `ETH long`
- trade planner verifie:
  - `target_notional_usd = 150.0`
  - `stop_bps = 80.0`
  - `time_stop_hours = 24`
- risk gate verifie:
  - trade valide -> `accepted`
  - confiance trop faible -> `confidence_below_min`
- executor verifie:
  - ouverture de position -> OK
  - fermeture fin de backtest -> OK
  - fermeture sur stop -> OK
  - slippage/frais appliques -> OK
- backtest runner verifie:
  - `records_processed = 2`
  - `signal_count = 2`
  - `accepted_count = 2`
  - `rejected_count = 0`
  - `closed_trade_count = 2`
  - `realized_pnl_usd < 0.0` sur l'exemple synthétique flat, ce qui confirme que les frais/slippage sont maintenant prices
  - `signals_by_setup = {'trend_pullback_long': 1, 'trend_pullback_short': 1}`
  - `signals_by_regime = {'TrendExpansion': 2}`
  - `average_confidence = 0.6`
  - output JSONL enrichi ecrit avec 2 signaux
- validateur de snapshots verifie:
  - input valide -> charge
  - input invalide -> `SnapshotFormatError` explicite
- convertisseur `gbot` verifie:
  - `records_written = 2` sur un exemple minimal
  - output JSONL conforme au schema snapshot TRIDENT
  - features enrichies presentes dans la sortie
- CLI backtest verifiee sur exemple minimal:
  - `records_processed = 1`
  - `accepted_count = 1`
  - `closed_trade_count = 1`
  - `realized_pnl_usd = -0.14`
- replay archive reel verifie sur donnees locales:
  - source `/workspaces/trident/data/server_archive`
  - dates `2026-04-01 -> 2026-04-03`
  - coins `BTC, ETH, SOL`
  - `snapshot_files_written = 3`
  - `snapshot_records_written = 3309`
  - `records_processed = 3309`
  - premier replay avant recalibrage:
    - `signal_count = 0`
    - `records_by_regime = {'DeadZone': 3303, 'RangeAuction': 6}`
  - replay apres recalibrage:
    - `signal_count = 20`
    - `accepted_count = 4`
    - `rejected_count = 16`
    - `opened_count = 2`
    - `skipped_open_count = 2`
    - `closed_trade_count = 2`
    - `realized_pnl_usd = 2.56`
    - `records_by_regime = {'DeadZone': 3055, 'TrendExpansion': 38, 'RangeAuction': 212, 'PanicSqueeze': 4}`
    - `rejections_by_reason = {'confidence_below_min': 16}`
  - replay apres scoring de confiance enrichi:
    - `signal_count = 20`
    - `accepted_count = 14`
    - `rejected_count = 6`
    - `opened_count = 6`
    - `skipped_open_count = 8`
    - `closed_trade_count = 6`
    - `win_count = 5`
    - `loss_count = 1`
    - `realized_pnl_usd = 12.52`
    - `gross_pnl_usd = 13.14`
    - `fees_usd = 0.63`
    - `average_hold_hours = 16.7667`
    - `average_confidence = 0.6417`
    - `rejections_by_reason = {'confidence_below_min': 5, 'batch_limit_reached': 1}`
    - `close_reasons = {'opposite_signal': 3, 'stop_hit': 1, 'time_stop': 2}`
    - `trades_by_symbol = {'ETH': 2, 'BTC': 2, 'SOL': 2}`
    - `pnl_by_symbol = {'ETH': 1.29, 'BTC': -0.57, 'SOL': 11.8}`
    - journal clarifie:
      - les refus sans position restent lisibles
      - les signaux recus pendant une fermeture montrent maintenant explicitement `had_open_position_before`, `has_open_position_after` et `close_fills`
      - les fermetures sont journalisees a part avec `event_type = trade_close`
  - replay etendu apres sync archive locale:
    - dates `2026-04-01 -> 2026-04-05`
    - coins `BTC, ETH, SOL, HYPE`
    - `records_processed = 5151`
    - `signal_count = 25`
    - `accepted_count = 18`
    - `rejected_count = 7`
    - `opened_count = 10`
    - `skipped_open_count = 8`
    - `closed_trade_count = 10`
    - `realized_pnl_usd = 19.66`
    - `gross_pnl_usd = 20.69`
    - `fees_usd = 1.05`
    - `average_hold_hours = 13.835`
    - `signals_by_date = {'2026-04-01': 2, '2026-04-02': 20, '2026-04-03': 1, '2026-04-05': 2}`
    - `accepted_by_date = {'2026-04-01': 1, '2026-04-02': 15, '2026-04-05': 2}`
    - `rejected_by_date = {'2026-04-01': 1, '2026-04-02': 5, '2026-04-03': 1}`
    - `pnl_by_date = {'2026-04-02': 12.11, '2026-04-03': 7.13, '2026-04-05': 0.42}`
    - `pnl_by_symbol = {'ETH': 1.29, 'BTC': -0.57, 'HYPE': 6.72, 'SOL': 12.22}`
  - smoke online live collector:
    - `messages_processed = 45`
    - `snapshots_written = 2`
    - `reconnect_count = 0`
    - output `data/live_snapshots/2026-04-05.jsonl`
  - smoke online Pod A live runner:
    - `records_processed = 1`
    - `signal_count = 0`
    - `accepted_count = 0`
    - `messages_processed = 38`
    - `reconnect_count = 0`
  - conclusion immediate: le pipeline produit maintenant des setups reelles, une partie significative passe le risk gate, et le nouveau scoring permet d'expliquer clairement les rejets et les ouvertures

Validation restante:

- backtest historique sur vraies donnees
- enrichir les exits / fills avec un modele plus proche de l'execution exchange
- affiner encore le scoring de confiance, mais a partir du journal detaille plutot qu'a l'aveugle
- ajouter un vrai snapshot d'etat de position par symbole pour chaque record si necessaire
- scheduler ou routine de replay/dry-run multi-jour
- reporting de performance plus riche

### Validation

- backtest multi-mois,
- dry-run 7 jours,
- comparaison backtest / dry-run.

### Go / no-go

- go si expectancy nette positive et DD acceptable.

Decision actuelle:

- Etape 4 fermee

---

## Etape 5 — Pod B range engine natif

### Statut

`EN COURS — moteur natif, wrapper de process, paper-run, wrapper live et cohabitation en place`

### Objectif

Ajouter le moteur range natif de TRIDENT.

### Travail

- ecrire le renderer de config runtime Pod B
- lancer / stopper le wrapper Pod B depuis TRIDENT
- remonter statut et positions
- remonter les fills du wrapper
- imposer liste de coins dediee
- garder Passivbot comme benchmark optionnel, pas comme obligation runtime

Travail realise:

- `app/trident/pod_b/models.py` cree
- `app/trident/pod_b/config_renderer.py` cree
- `app/trident/pod_b/status_parser.py` cree
- `app/trident/pod_b/passivbot_manager.py` cree
- `app/trident/pod_b/__init__.py` expose maintenant l'API Pod B
- `PassivbotConfigRenderer` genere une config runtime minimale pilotee par TRIDENT
- `PassivbotStatusParser` lit un `status.json` local s'il existe
- `PassivbotManager.sync(...)`:
  - ecrit le runtime config
  - publie un etat initial
  - prepare la commande du wrapper Pod B
- `PassivbotManager.start(...)` implemente:
  - lance un process externe
  - ecrit `pid`, `launch_command`, `stdout_path`, `stderr_path`, `started_at`
- `PassivbotManager.stop()` implemente:
  - termine le process courant via `SIGTERM`, puis `SIGKILL` si necessaire
  - remet le status local en `stopped`
- `PassivbotManager.restart(...)` implemente
- detection de status stale implementee:
  - un `status.json` indiquant `running` avec un PID mort est corrige en `process_exited`
- `PassivbotStatus` enrichi avec:
  - `positions`
  - `open_orders`
  - `inventory`
  - `total_position_count`
  - `total_open_order_count`
  - `total_notional_usd`
  - `total_unrealized_pnl_usd`
- `PassivbotStatusParser` parse maintenant les champs trading si presents dans le `status.json`
- fallback d'inventory implemente:
  - cible par symbol = `target_usd / nombre de symbols geres`
  - notional courant deduit des positions
  - `open_order_count` deduit des ordres ouverts
- `PassivbotStatus` enrichi en plus avec:
  - `recent_fills`
  - `total_fill_count`
  - `realized_pnl_usd`
- `PodBPaperEngine` implemente:
  - quotes maker symetriques autour du mid
  - skew d'inventory simple
  - fills papier sur croisement du prix
  - positions / inventory / unrealized / realized P&L
- choix d'architecture acte:
  - ce moteur natif est la V1 retenue
  - Passivbot reste une reference externe pour benchmark et calibration
- `PodBPaperRunner` implemente:
  - lit des snapshots TRIDENT JSONL
  - ecrit un `status.json` vivant a chaque tick
  - ecrit un report JSON final
  - ecrit un journal JSONL optionnel des fills
- `PodBPaperLiveRunner` implemente:
  - suit un fichier ou un repertoire de snapshots
  - met a jour le `status.json` en continu
  - supporte `poll_seconds`, `max_runtime_seconds`, `max_idle_loops`
- `launch_workdir` ajoute a la config Pod B
- `PassivbotManager.start(...)` supporte maintenant un vrai wrapper runtime Python via `launch_command`
- `CohabitationReplayRunner` implemente:
  - rejoue Pod A et Pod B sur le meme flux de snapshots
  - verifie les conflits d'ownership et l'absence de recouvrement de symbols
  - rapporte les signaux Pod A et les fills Pod B
- `PassivbotStatusParser` et `PassivbotManager` reprennent maintenant aussi:
  - `recent_fills`
  - `total_fill_count`
  - `realized_pnl_usd`
- le superviseur appelle maintenant `sync_pod_b()`:
  - a l'initialisation
  - apres sync ownership
  - apres changement de regime
- `/api/state` expose maintenant `pod_b_status`

Validation reelle effectuee:

- renderer verifie:
  - coins autorises rendus correctement
  - `time_in_force = post_only`
  - `target_usd` expose dans le bloc `trident`
- manager verifie:
  - runtime config ecrit sur disque
  - status coherent sans wrapper externe
- parser verifie:
  - reprise correcte d'un `status.json` externe
- parser/runtime trading verifie:
  - reprise correcte des `positions`
  - reprise correcte des `open_orders`
  - inventory derivee correctement a partir du `target_usd`, des positions et des ordres
- wrapper de process verifie:
  - lancement d'un process test -> `process_state = running`
  - arret du process -> `process_state = stopped`
  - fichiers `stdout/stderr` crees
- detection stale verifiee:
  - `pid` inexistant dans un status `running` -> corrige en `stopped` avec raison `process_exited`
- superviseur verifie:
  - `pod_b_status.managed_symbols` visible
  - runtime config Pod B ecrit a l'initialisation
  - `pod_b_status.inventory` expose
  - `pod_b_status.total_position_count` expose
- paper runner verifie:
  - ecrit un `status.json` complet avec positions / ordres / inventory / fills
  - ecrit un journal JSONL de fills
  - `manager.sync(...)` relit correctement le `status.json` genere par le runner
- wrapper live verifie:
  - lancement reel via `launch_command`
  - `manager.sync(...)` relit un `status.json` `running` mis a jour par le process
- replay de cohabitation verifie:
  - conflit `ETH` correctement attribue a Pod A
  - Pod B limite a `XRP`
  - aucun overlap de symbols entre Pod A et Pod B
- replay reel local verifie:
  - conversion snapshots `BTC, ETH` du `2026-04-05`
  - `records_processed = 402`
  - `fills_emitted = 13`
  - `total_fill_count = 13`
  - `realized_pnl_usd = -0.0643`
  - `total_unrealized_pnl_usd = -1.4186`

Validation reelle effectuee supplementaire:

- replay multi-jour Pod B sur snapshots locaux `2026-04-01 -> 2026-04-05`:
  - `records_processed = 5151`
  - `fills_emitted = 199`
  - `total_fill_count = 199`
  - `realized_pnl_usd = -16.5853`
  - `total_unrealized_pnl_usd = -1.8381`
  - `max_drawdown_usd = 16.5989`

Validation restante:

- dette de nommage interne `passivbot_*` optionnelle seulement, pas bloquante pour l'etape

### Validation

- benchmark offline contre Passivbot,
- paper 72h,
- test de cohabitation avec Pod A desactive puis active.

### Go / no-go

- go si P&L net positif, pas de conflit, pas de runaway inventory.

Decision actuelle:

- GO ferme pour considerer le moteur Pod B natif comme la V1 de reference dans `trident`

---

## Etape 6 — Reporting par pod

### Statut

`TERMINEE — reporting runtime, replay et export journalier consolides`

### Objectif

Rendre le systeme lisible avant d'ajouter plus de complexite.

### Travail

- ajouter P&L par pod
- ajouter DD par pod
- afficher capital alloue vs utilise
- ajouter vues UI dediees

Travail realise:

- `app/reporting/multi_pod.py` cree
- `build_runtime_report(...)` implemente:
  - resume runtime par pod
  - agregats globaux multi-pods
  - positions / ordres / fills / P&L Pod B
- `build_cohabitation_summary(...)` implemente:
  - P&L total Pod A + Pod B
  - resume compact des ownerships et activites par pod
- `state_payload(...)` expose maintenant `runtime_report`
- `/api/report` expose le report runtime multi-pods
- le dashboard affiche maintenant:
  - open positions
  - open orders
  - total fills
  - realized PnL
  - tableau `Runtime pod report`
- `app/reporting/pod_b.py` cree
- `PodBPaperRunner` et `PodBPaperLiveRunner` ecrivent maintenant un report detaille avec:
  - `fills_by_symbol`
  - `fills_by_date`
  - `fill_notional_by_symbol`
  - `fill_notional_by_date`
  - `realized_pnl_by_date`
  - `inventory_skew_by_symbol`
- `app/reporting/export_daily.py` enrichi:
  - `actual_total_equity_usd`
  - `cash_balance_usd`
  - `runtime.cash_usd`
  - `runtime.total_target_usd`
  - `reconciliation_gap_usd` explicite
- `PodABacktestReport` et `PodBReport` exposent maintenant `max_drawdown_usd`
- `build_runtime_report(...)` remonte aussi le preview count Pod C
- `state_payload(...)` et le dashboard restent alignes avec le meme report runtime

Validation reelle effectuee:

- `python3.12 -m unittest discover -s tests -v` -> OK, `68 tests`
- smoke HTTP:
  - `GET /api/report` -> OK
  - dashboard contient `Runtime pod report` -> OK
- replay reel Pod B:
  - `records_processed = 402`
  - `fills_by_symbol = {BTC: 6, ETH: 7}`
  - `fills_by_date = {2026-04-05: 13}`
  - `realized_pnl_by_date = {2026-04-05: -0.0643}`
- export journalier verifie:
  - `python3.12 -m app.reporting.export_daily ...` -> OK
  - `reconciliation_gap_usd` expose explicitement
  - sortie JSON + markdown ecrites

Validation restante:

- aucune bloquante pour l'etape

### Validation

- rapport journalier lisible,
- metriques Prometheus coherentes,
- reconciliation P&L globale = somme des pods + cash.

### Go / no-go

- go si un humain peut expliquer la journee en 5 minutes.

---

## Etape 7 — Research Pod pour Pod C

### Statut

`TERMINEE — suite de recherche reproductible, protocole et memo produits`

### Objectif

Prouver ou tuer vite l'idee lead-lag.

### Travail

- ecrire les scripts d'etude cross-correlation
- mesurer lags et expectancy nette
- produire un memo technique

Travail realise:

- `app/research/pod_c_leadlag.py` ajoute
- `app/research/pod_c_research_suite.py` ajoute
- protocole documente dans `docs/pod_c_research_protocol.md`
- memo de run local genere:
  - `docs/pod_c_research_latest.json`
  - `docs/pod_c_research_latest.md`

Validation reelle effectuee:

- `python3.12 -m unittest discover -s tests -v` -> OK, `68 tests`
- suite locale sur `data/live_snapshots/2026-04-05.jsonl`:
  - `candidate_count = 2`
  - `go_count = 0`
  - `recommendation = no-go`

Decision actuelle:

- NO-GO research pour activer Pod C sur ce petit echantillon live local

### Validation

- notebook / script reproductible,
- rapport markdown dans `docs/`,
- recommandation go/no-go explicite.

### Go / no-go

- go uniquement si edge net positif robuste.

---

## Etape 8 — Pod C minimal

### Statut

`TERMINEE — moteur minimal implemente derriere le meme pipeline d'execution/risk`

### Objectif

Ajouter un moteur de convexite tres borne.

### Travail

- implementer uniquement le setup event retenu,
- ajout de filtres severes,
- exits defensifs,
- tailles bornees.

Travail realise:

- `app/trident/pod_c/context.py` cree
- `app/trident/pod_c/service.py` cree
- `app/trident/pod_c/planner.py` cree
- `app/trident/pod_c/exits.py` cree
- `app/risk/pod_c_gate.py` cree
- `app/backtest/pod_c_runner.py` cree
- `app/live/pod_c_live_runner.py` cree
- `TridentSupervisor` expose maintenant:
  - `preview_pod_c_signals(...)`
  - `build_pod_c_trade_plans(...)`
  - `pod_c_signal_preview`
- Pod C reutilise les memes briques que Pod A pour garantir la coherence:
  - `app/execution/directional_executor.py`
  - `app/risk/plan_gate.py`
  - `app/execution/dry_run.py`

Validation reelle effectuee:

- `python3.12 -m unittest discover -s tests -v` -> OK, `68 tests`
- replay Pod C sur `data/live_snapshots/2026-04-05.jsonl`:
  - `records_processed = 2`
  - `signal_count = 0`
  - `accepted_count = 0`
- smoke live Pod C:
  - `records_processed = 1`
  - `signal_count = 0`
  - `api_error_count = 0`

Challenge applique:

- le pod est implemente mais garde des filtres severes
- absence de signal sur l'echantillon live local est consideree comme un resultat sain, pas comme un bug, car elle reste coherente avec le `no-go` research courant

### Validation

- replay offline,
- dry-run 5 a 7 jours,
- mesure du slippage reel et du fill rate.

### Go / no-go

- go si le pod ajoute de la convexite sans exploser le DD global.

---

## Etape 9 — Hardening deployment

### Statut

`TERMINEE — couche live HL durcie et artefacts de deploiement ajoutes`

### Objectif

Rendre le deploiement repetable.

### Travail

- ajouter `docker-compose.trident.yml`
- ajouter scripts start/stop/restart
- health checks
- rotation logs
- relance process automatique

Travail realise:

- `app/live/errors.py` cree pour classifier:
  - erreurs recoverable
  - rate limits
- `app/hyperliquid/rate_limiter.py` cree:
  - quota partage cross-process
  - circuit breaker partage
  - jitter
- `app/live/collector.py` durci:
  - heartbeats explicites
  - gestion `pong`
  - timeouts de message
  - invalid JSON defenses
  - backoff exponentiel cappe
  - compteurs d'erreurs API et rate-limit
- `app/hyperliquid/info_client.py` cree:
  - retries HTTP
  - classification des `429`
  - backoff cappe
  - integration au rate limiter partage
- `Dockerfile.trident` ajoute
- `docker-compose.trident.yml` ajoute
- `.dockerignore` ajoute
- scripts ajoutes:
  - `scripts/trident_start.sh`
  - `scripts/trident_stop.sh`
  - `scripts/trident_restart.sh`
  - `scripts/trident_healthcheck.sh`
- `Makefile` enrichi avec cibles Docker / healthcheck

Validation reelle effectuee:

- `python3.12 -m unittest discover -s tests -v` -> OK, `68 tests`
- `python3.12 -m app.live.collector --help` -> OK
- `python3.12 -m app.live.pod_c_live_runner --help` -> OK
- `python3.12 -m app.backtest.pod_c_runner --help` -> OK
- `bash -n scripts/trident_*.sh` -> OK
- smoke live collector:
  - `messages_processed = 34`
  - `snapshots_written = 1`
  - `api_error_count = 0`
  - `rate_limit_error_count = 0`
  - `throttle_wait_count = 0`
- tests anti-429:
  - retry HTTP sur `429` -> OK
  - partage de fenetre de quota entre 2 instances -> OK
  - ouverture de circuit breaker partage apres rafale de `429` -> OK
- limite d'environnement:
  - `docker compose -f docker-compose.trident.yml config` non verifiable ici car `docker` absent localement

### Validation

- redeploiement a chaud,
- reboot serveur,
- coupure reseau simulee,
- reconnexion propre.

Challenge applique:

- un vrai piege a ete corrige dans `docker-compose.trident.yml`:
  - `pod-b-live` ne doit pas utiliser `--max-idle-loops 0`, sinon il s'arrete immediatement
- un warning `python -m app.live.collector` a ete elimine en rendant `app/live/__init__.py` lazy
- la gestion `429` n'est plus seulement locale a un process:
  - un client HTTP ou un collector WebSocket peut maintenant ouvrir un frein partage visible des autres process `trident`

### Go / no-go

- go si le systeme survit a 72h sans intervention manuelle.

---

## Etape 10 — Passage live progressif

### Objectif

Monter le risque en paliers.

### Travail

- live avec Pod A seulement
- live A + B ensuite
- live A + B + C en dernier
- revue de chaque palier avec `scripts/trident_dry_run_review.sh`
- statut auto quand le verdict est mecanique
- prompt LLM genere quand la decision demande un jugement qualitatif

### Validation

- capital minimal,
- revue quotidienne,
- comparaison dry-run / live,
- aucune hausse anormale du fee drag ou des conflits.
- artefacts de revue conserves a chaque palier:
  - `review_summary.md`
  - `review_summary.json`
  - prompts LLM eventuels

### Go / no-go

- stop immediat si divergence majeure entre comportement observe et attendu.

---

## 13. Definition of done par etape

Une etape n'est terminee que si:

- le code s'execute dans un environnement propre,
- les tests passent,
- les logs sont lisibles,
- les metriques existent,
- la doc de la fonctionnalite est ajoutee,
- le critere quantifie de validation est atteint,
- la commande de lancement est documentee.

---

## 14. Commandes cibles a supporter

Le projet doit converger vers ces commandes simples:

```bash
# legacy
./run.sh dry-run

# trident observation
uv run python -m app.main --mode observation --profile trident

# trident dry-run
uv run python -m app.main --mode dry-run --profile trident

# trident backtest
uv run python -m app.backtest.runner --profile trident --date 2026-04-01

# passivbot managed
docker compose -f docker-compose.trident.yml up -d
```

---

## 15. Risques majeurs

### Risque 1

Le port de `t-bot` vers Python peut prendre plus de temps que prevu.

Mitigation:

- commencer par un setup unique de Pod A,
- ne pas attendre un port complet du scoring original.

### Risque 2

Passivbot peut etre rentable seulement sur certains coins / regimes.

Mitigation:

- coins dedies,
- optimizer,
- pause automatique hors domaine.

### Risque 3

Le lead-lag peut etre une fausse bonne idee apres fees.

Mitigation:

- research d'abord,
- V1 event minimale,
- no-go explicite si l'edge n'est pas la.

### Risque 4

Le systeme devient trop complexe trop vite.

Mitigation:

- A puis B puis C,
- aucun LLM live,
- reporting par pod obligatoire avant tout ajout.

---

## 16. Pistes futures Hydra revisitees

Ces pistes ne doivent pas etre branchees directement dans le run principal.
Elles suivent obligatoirement la sequence:

- research note
- prototype offline
- backtest/replay
- dry-run shadow
- decision `go / park / kill`

### Piste A - Funding Mean Reversion revisitee

Statut:

- a implementer plus tard
- candidate prioritaire parmi les idees Hydra non reprises

Pourquoi elle reste interessante:

- hypothese testable proprement
- mecanique claire
- potentiellement structurelle si les episodes de funding extreme sont exploitables apres fees

Ce qu'il faut implementer avant tout test live:

- collecte native du funding history dans `trident`
- dataset local dedie funding par coin / timestamp
- strategie research explicite:
  - seuils d'entree
  - duree de hold
  - regles de sortie
  - filtres spread / liquidite / regime
- backtest offline sur historique suffisant
- replay sur snapshots/reports TRIDENT

Conditions minimales pour promotion en dry-run:

- expectancy positive apres frais
- nombre d'occurrences suffisant
- drawdown borne
- comportement stable sur plusieurs coins et plusieurs jours

Etat actuel:

- aucun pod funding dedie
- `funding_rate` n'est aujourd'hui qu'un champ de snapshot et un filtre mineur dans Pod A

Mini roadmap technique:

1. Collecte de donnees
   - ajouter un collecteur funding natif HL
   - persister `coin / timestamp / funding_rate / mark / open_interest si disponible`
   - fichiers cibles:
     - `app/hyperliquid/funding_client.py`
     - `app/live/funding_collector.py`
     - `data/funding_history/`

2. Dataset research
   - construire un dataset aligne:
     - funding extremum
     - rendement futur 1h / 8h / 24h
     - spread moyen
     - regime courant
   - fichiers cibles:
     - `app/research/pod_funding_dataset.py`
     - `app/research/pod_funding_research.py`

3. Prototype offline
   - tester au minimum:
     - version pure mean reversion
     - version funding + filtre regime
     - version funding + filtre spread/liquidite

4. Backtest/replay
   - convertir la logique research en runner deterministe
   - fichiers cibles:
     - `app/trident/pod_funding/`
     - `app/backtest/pod_funding_runner.py`

5. Dry-run shadow
   - tourner separement du run principal
   - report dedie obligatoire:
     - expectancy nette
     - PnL par coin
     - taux de faux signaux
     - drawdown max

Critere `kill`:

- edge positif seulement avant frais
- trop peu d'occurrences
- performance concentree sur 1 seul coin ou 1 seul jour

Critere `go`:

- expectancy nette > 0 apres frais
- profit factor > 1.1
- comportement stable sur au moins 2 sous-fenetres out-of-sample

### Piste B - Liquidation / OI event engine revisite

Statut:

- a implementer plus tard
- candidate secondaire

Pourquoi elle n'est pas reprise telle quelle depuis Hydra:

- la version Hydra reposait sur une hypothese de donnees trop fragile
- on ne veut pas construire un pod sur une reconstruction de heatmap de liquidation non fiable

Nouvelle approche demandee:

- repartir des donnees vraiment observables
- ne pas supposer l'existence d'une carte fiable des liquidation clusters

Directions de recherche possibles:

- variations rapides d'open interest si source exploitable
- bursts de flow agressif
- acceleration de spread / imbalance / trade flow avant moves violents
- patterns post-liquidation visibles dans le tape et le book
- dislocations prix / flow / microstructure autour d'evenements de squeeze

Pipeline exige:

- note de faisabilite data d'abord
- seulement ensuite prototype research
- seulement ensuite replay et dry-run shadow

Conditions de promotion:

- signal observable en temps reel
- logique d'entree/sortie reproductible
- edge non explique uniquement par quelques outliers

Decision par defaut:

- `park` tant que la source de donnees et la definition du signal ne sont pas propres

Mini roadmap technique:

1. Note de faisabilite data
   - verifier exactement quelles donnees HL sont accessibles en historique et en live
   - lister ce qui est:
     - observable
     - approximable
     - non exploitable
   - livrable:
     - `docs/pod_liq_data_feasibility.md`

2. Prototype observables-first
   - ne pas coder de "liquidation map" au debut
   - commencer par des features simples:
     - acceleration de trade flow
     - dislocation book imbalance / spread
     - changement brutal d'intensite de prints
     - eventuellement open interest delta si la source est propre
   - fichiers cibles:
     - `app/research/pod_liq_features.py`
     - `app/research/pod_liq_research.py`

3. Definition du signal
   - un signal n'existe que s'il a:
     - une entree objective
     - une invalidation objective
     - un horizon borne
   - tant que ces 3 points ne sont pas fixes, on ne cree pas de pod

4. Replay puis dry-run shadow
   - seulement si une feature montre un edge reproductible
   - fichiers cibles:
     - `app/trident/pod_liq/`
     - `app/backtest/pod_liq_runner.py`

Critere `kill`:

- impossibilite d'obtenir une source de donnees fiable
- edge dependant d'une interpretation discretionary
- perf expliquee par quelques evenements extrêmes non repetables

Critere `go`:

- signal data-driven clairement definissable
- nombre d'occurrences suffisant
- avantage net conserve hors echantillon

### Piste C - Lead-lag inter-coins a garder en veille

Statut:

- deja implemente en research / Pod C
- non promu car dernier memo = `no-go`

Decision:

- ne pas supprimer
- ne pas activer par reflexe
- rerun seulement quand:
  - plus d'historique live a ete accumule
  - ou une variante de l'hypothese est redefinie proprement

Mini roadmap technique:

1. Accumuler plus d'historique live natif
   - objectif minimal:
     - plusieurs jours multi-regimes
     - davantage de leaders / followers

2. Retester plusieurs variantes
   - lead BTC seul
   - lead ETH seul
   - filtre regime explicite
   - filtre spread / flow / book imbalance
   - version taker defensive comparee a maker stricte

3. Produire un memo comparatif
   - livrables:
     - `docs/pod_c_research_<date>.md`
     - `docs/pod_c_research_<date>.json`
   - conclusion obligatoire:
     - `go`
     - `park`
     - `kill`

4. Si `go`
   - promouvoir seulement la variante gagnante
   - conserver les memes regles d'execution et de risk que Pod A

Critere `kill`:

- `no-go` repete sur plusieurs fenetres independantes
- signal qui disparait apres frais ou apres contraintes de fill realistes

---

## 17. Decision finale de conception

TRIDENT n'est pas:

- un monolithe "multi-alpha" deploye d'un coup,
- un bot AI auto-magique,
- une reedition de `gbot` avec plus de filtres.

TRIDENT est:

- un superviseur simple,
- un moteur trend comme ancre,
- un moteur range natif dans `trident`,
- un moteur event live seulement si la recherche le justifie,
- une machine orientee mesure, isolation, et discipline de deploiement.

Si l'equipe suit ce plan, elle pourra:

- livrer une V1 utile rapidement,
- comprendre clairement ce que chaque pod apporte,
- couper vite ce qui ne marche pas,
- monter en risque sans se perdre dans un chantier trop theorique.
