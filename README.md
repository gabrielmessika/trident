# trident

TRIDENT est un orchestrateur de pods de trading pour Hyperliquid.

Etat actuel:

- socle Python initialise,
- API HTTP stdlib minimale,
- dashboard HTML minimal sur `/` et `/dashboard`,
- route JSON `/api/metrics` pour les compteurs de supervision,
- route JSON `/api/report` pour le reporting runtime multi-pods,
- superviseur TRIDENT minimal,
- registry d'ownership des symbols,
- configuration de base chargee depuis `config/trident.toml`,
- donnees historiques `gbot` miroir localement dans `data/gbot_archive/`,
- donnees historiques `gbot/server-data` miroir localement dans `data/server_archive/`,
- collector live Hyperliquid natif dans `app/live/collector.py`,
- gestion durcie des erreurs Hyperliquid:
  - WebSocket avec heartbeats, timeouts, backoff et classification rate-limit
  - client HTTP retriable dans `app/hyperliquid/info_client.py`,
  - rate limiter partage sur disque dans `app/hyperliquid/rate_limiter.py`,
- runner live Pod A dans `app/live/pod_a_live_runner.py`,
- runner live Pod C dans `app/live/pod_c_live_runner.py`,
- snapshots live ecrits localement dans `data/live_snapshots/`,
- Pod A relie a un pipeline `snapshot -> signal -> trade plan -> risk -> dry-run execution`,
- Pod C relie au meme pipeline d'execution/risk que Pod A via un executor directionnel partage,
- replay de cohabitation Pod A / Pod B present dans `app/backtest/cohabitation_replay.py`,
- convertisseur `gbot` capable de produire des snapshots enrichis a partir de `l2 + trades`,
- moteur Pod B natif present dans `app/trident/pod_b/`,
- wrapper de process Pod B `start/stop/restart` present,
- paper runner Pod B present dans `app/trident/pod_b/paper_runner.py`,
- wrapper live Pod B present dans `app/trident/pod_b/paper_live_runner.py`,
- research Pod C present dans `app/research/pod_c_leadlag.py` et `app/research/pod_c_research_suite.py`,
- protocole research Pod C documente dans `docs/pod_c_research_protocol.md`,
- artefacts de deploiement presents:
  - `Dockerfile.trident`
  - `docker-compose.trident.yml`
  - `prepare_server.sh`
  - `deploy.sh`
  - `scripts/trident_start.sh`
  - `scripts/trident_stop.sh`
  - `scripts/trident_restart.sh`
  - `scripts/trident_healthcheck.sh`
  - `scripts/trident_server.sh`
  - `scripts/fetch_trident_data.sh`
  - `docs/deployment.md`

Decision d'architecture actuelle:

- Pod B V1 est natif a `trident`.
- Passivbot reste une reference de benchmark et d'inspiration, pas une dependance runtime obligatoire.
- univers observe et univers trade sont separes:
  - `hyperliquid.observation_universe` = coins observes par le collector
  - `pod_a.symbols`, `pod_b.symbols`, `pod_c.*` = coins reellement tradables par pod
  - le collector est automatiquement sharde si l'univers observe depasse la limite empirique stable de ~10 coins par connexion WS

Comment ca marche, en version simple:

```text
Hyperliquid
   |
   v
Collector de donnees
   |
   v
Snapshots marche
   |
   v
Supervisor TRIDENT
- detecte le regime
- alloue le capital
- attribue chaque coin a un seul pod
   |
   +--> Pod A = trend / swing
   +--> Pod B = range / maker
   +--> Pod C = event / opportunites rares
   |
   v
Risk checks + execution + journal + dashboard
```

Le point cle:

- les pods peuvent tourner en meme temps
- ils ne doivent pas gerer le meme coin en meme temps
- c'est le `supervisor` qui arbitre et evite les conflits

Exemple d'ownership valide:

```text
BTC -> Pod A
ETH -> Pod A
XRP -> Pod B
SOL -> Pod C
```

Exemple invalide:

```text
BTC -> Pod A et Pod B en meme temps
```

Role de chaque pod:

- `Pod A`: moteur directionnel principal, pour les phases de tendance
- `Pod B`: moteur de range, pour les marches plus plats
- `Pod C`: moteur opportuniste, a activer seulement si la recherche le justifie

Pourquoi `deploy.sh --with-pod-b` ou `--with-pod-c` existe:

- `deploy.sh` sert a la fois a copier le code et, avec `--start`, a demarrer les services
- par defaut, le demarrage est prudent: API + `Pod A`
- `--with-pod-b` ajoute `Pod B`
- `--with-pod-c` ajoute `Pod C`

Donc:

- `./deploy.sh` = deploie seulement
- `./deploy.sh --start` = deploie puis demarre API + Pod A
- `./deploy.sh --start --with-pod-b` = ajoute Pod B
- `./deploy.sh --start --with-pod-b --with-pod-c` = demarre tout

En pratique, on a choisi ce mode pour garder un lancement safe:

- `Pod A` est le pod le plus mature
- `Pod B` a sa propre config runtime
- `Pod C` est plus experimental
- il vaut mieux allumer peu de choses au debut, puis elargir

Reporting actuel:

- `state_payload` inclut maintenant `runtime_report`,
- `/api/report` expose un resume multi-pods runtime,
- le dashboard affiche un tableau `Runtime pod report`,
- les replays de cohabitation ecrivent aussi un `summary` multi-pods dans leur JSON de sortie.
- les runners Pod B ecrivent maintenant un report detaille:
  - `fills_by_symbol`
  - `fills_by_date`
  - `fill_notional_by_symbol`
  - `realized_pnl_by_date`
  - `inventory_skew_by_symbol`
- `app/reporting/export_daily.py` consolide maintenant:
  - realized / unrealized
  - max drawdown par pod
  - equity estimee vs equity reelle fournie
  - gap de reconciliation explicite

Commandes utiles:

```bash
uv sync
uv run python -m app.main --mode observation --profile trident
uv run pytest
python3.12 -m unittest discover -s tests -v
python3.12 -m app.backtest.runner --input /path/to/input.jsonl --output /path/to/output.jsonl
python3.12 -m app.backtest.gbot_converter --data-dir /workspaces/trident/data/gbot_archive --date 2026-04-01 --coins BTC,ETH,SOL --output /tmp/trident_snapshots.jsonl
python3.12 -m app.backtest.archive_replay --data-dir /workspaces/trident/data/server_archive --date-from 2026-04-01 --date-to 2026-04-03 --coins BTC,ETH,SOL --report-output /tmp/trident_report.json --journal-output /tmp/trident_journal.jsonl
uv run python -m app.live.collector --coins BTC,ETH --max-runtime-seconds 8
uv run python -m app.live.pod_a_live_runner --coins BTC,ETH --max-runtime-seconds 8 --journal-output /workspaces/trident/data/live_snapshots/pod_a_live_journal.jsonl
uv run python -m app.live.pod_c_live_runner --coins BTC,ETH,SOL --max-runtime-seconds 8 --journal-output /workspaces/trident/data/live_snapshots/pod_c_live_journal.jsonl
python3.12 -m app.backtest.pod_c_runner --input /workspaces/trident/data/live_snapshots/2026-04-05.jsonl --output /tmp/pod_c_journal.jsonl
python3.12 -m app.research.pod_c_research_suite --input /workspaces/trident/data/live_snapshots/2026-04-05.jsonl --leader-symbols BTC,ETH --follower-symbols SOL,HYPE,SUI --output-json /tmp/pod_c_research.json --output-md /tmp/pod_c_research.md
python3.12 -m app.trident.pod_b.paper_runner --config-path /tmp/trident/runtime/passivbot/live.json --input /workspaces/trident/data/live_snapshots/2026-04-05.jsonl --report-output /tmp/pod_b_report.json --journal-output /tmp/pod_b_fills.jsonl
python3.12 -m app.trident.pod_b.paper_live_runner --config-path /tmp/trident/runtime/passivbot/live.json --input /workspaces/trident/data/live_snapshots --poll-seconds 0.1 --max-idle-loops 5
python3.12 -m app.backtest.cohabitation_replay --config config/trident.toml --input /tmp/trident_snapshots.jsonl --output /tmp/cohabitation_report.json
python3.12 -m app.reporting.export_daily --pod-b-report /tmp/pod_b_report.json --reference-equity-usd 1000 --cash-balance-usd 1001 --output-json /tmp/trident_daily_summary.json --output-md /tmp/trident_daily_summary.md
./scripts/trident_healthcheck.sh
./scripts/fetch_trident_data.sh
./scripts/fetch_trident_data.sh --days 3
./scripts/fetch_trident_data.sh --date 2026-04-05
./prepare_server.sh trident-hetzner
./deploy.sh --start
```

Rapatriement et analyse locale:

- `./scripts/trident_dry_run_review.sh`
  - revue distante legere
  - recupere surtout l'etat courant, les tails de logs et genere les prompts LLM
- `./scripts/fetch_trident_data.sh`
  - rapatrie les snapshots live, logs runtime, statuses, snapshots API et logs Docker
  - peut ensuite relancer automatiquement `trident_dry_run_review.sh`
  - permet une vraie analyse locale plus complete sur plusieurs heures / jours

Exemples:

```bash
./scripts/fetch_trident_data.sh
./scripts/fetch_trident_data.sh --days 2
./scripts/fetch_trident_data.sh --snapshots-only --days 5
./scripts/fetch_trident_data.sh --logs-only
./scripts/fetch_trident_data.sh --review-only
```

Validation recente:

- `68 tests` passent via `python3.12 -m unittest discover -s tests -v`
- smoke live collector `BTC, ETH`:
  - `messages_processed = 34`
  - `snapshots_written = 1`
  - `api_error_count = 0`
  - `throttle_wait_count = 0`
- tests anti-429:
  - retries HTTP sur `429` verifies
  - circuit breaker partage entre instances verifie
  - fenetre de quota partagee entre instances verifiee
- smoke live Pod A:
  - `records_processed = 1`
  - `signal_count = 0`
- smoke live Pod C:
  - `records_processed = 1`
  - `signal_count = 0`
- replay multi-jour Pod B sur `2026-04-01 -> 2026-04-05`:
  - `records_processed = 5151`
  - `fills_emitted = 199`
  - `realized_pnl_usd = -16.5853`
  - `max_drawdown_usd = 16.5989`
- suite research Pod C sur `data/live_snapshots/2026-04-05.jsonl`:
  - `candidate_count = 2`
  - `go_count = 0`
  - `recommendation = no-go`

Limite connue:

- `docker compose` n'a pas pu etre valide dans cet environnement car le binaire `docker` n'est pas installe localement.
- le rate limiter central n'a pas encore ete valide sous charge massive reelle multi-process contre HL, seulement par tests et smokes locaux.
