# trident

TRIDENT est un orchestrateur de pods de trading pour Hyperliquid.

## Explication très simple

Si on oublie le vocabulaire technique, `Trident` est surtout un **chef d'orchestre**.

Son rôle est simple :

- regarder ce qu'il se passe sur les marchés Hyperliquid en direct ;
- reconnaître le type de marché du moment ;
- répartir l'argent entre plusieurs modules spécialisés ;
- s'assurer qu'un même marché n'est géré que par un seul module à la fois ;
- vérifier que le niveau de risque reste acceptable avant d'agir.

### Qu'est-ce qu'un pod ?

Ici, un `pod`, c'est simplement un **spécialiste**.

Chaque pod a sa propre façon de chercher des opportunités :

- `Pod A` aime les mouvements déjà bien lancés ;
- `Pod B` préfère les impulsions fraîches et les breakouts crypto sélectifs ;
- `Pod C` cherche des tendances Tradfi HL sur un panier builder-dex dédié.

On peut imaginer `Trident` comme une petite équipe :

- un observateur qui regarde le marché ;
- un manager qui décide qui travaille ;
- trois spécialistes (`Pod A`, `Pod B`, `Pod C`) ;
- un gardien du risque qui peut dire : "non, on n'y va pas".

### Comment fonctionne Trident, sans jargon

1. `Trident` reçoit des données en direct sur plusieurs marchés HL.
2. Il essaie de comprendre l'ambiance du marché : est-ce que ça monte franchement, est-ce que ça tourne en rond, ou est-ce que ça devient très agité ?
3. En fonction de cette lecture, il donne plus ou moins de place à chaque pod.
4. Chaque pod ne travaille que sur les marchés qui lui ont été attribués.
5. Le pod peut proposer une action : acheter, vendre, ou ne rien faire.
6. `Trident` vérifie ensuite que cette action n'est pas trop risquée.
7. Si tout est correct, l'action est envoyée au système d'exécution, ou simplement simulée en `dry-run`.
8. Tout est enregistré dans des journaux et visible dans le dashboard.

### Le rôle de chaque pod, très simplement

#### Pod A : le suiveur de tendance

`Pod A` cherche surtout les cryptos qui ont déjà commencé un mouvement clair.

En pratique :

- si un prix monte de manière assez propre, `Pod A` peut essayer de suivre la hausse ;
- si un prix baisse de manière assez propre, `Pod A` peut essayer de suivre la baisse ;
- il préfère les mouvements nets au bruit et aux hésitations.

Dans la config dry-run actuelle, `Pod A` reste le moteur directionnel principal du sleeve crypto, mais avec quelques branches déjà coupées pour rester plus propre :

- `bos_retest_long` et `bos_retest_short` sont désactivés ;
- `trend_pullback_short` est désactivé ;
- `liquidity_sweep_reclaim_short` est maintenant désactivé après replay isolé positif.

Autrement dit, `Pod A` ne cherche pas à deviner un retournement. Il préfère **monter dans un train déjà en marche**.

#### Pod B : le pod breakout directionnel

`Pod B` cherche surtout les expansions naissantes sur le sleeve crypto.

En pratique :

- il attend une compression ou un contexte d'impulsion propre ;
- il ne travaille que sur un sous-ensemble du panier crypto que le superviseur lui attribue ;
- en dry-run courant, il applique un **strict continuation filter** sur les longs `vol_expansion_long` pour ne garder que les expansions deja propres et alignées (structure, VWAP, flow, spread, range) ;
- il peut recevoir des symbols en `DeadZone` pour les surveiller, puis ne rien faire tant qu'aucun setup propre n'apparaît.

Autrement dit, `Pod B` ne fait plus du market making de range. C'est un pod **directionnel**, plus sélectif que `Pod A`, destiné aux breakouts et expansions fraîches.

#### Pod C : le pod Tradfi directionnel

`Pod C` surveille surtout un panier Tradfi builder-dex sur Hyperliquid.

L'idée simple est la suivante :

- un marché comme `XYZ:SP500`, `XYZ:GOLD` ou `XYZ:CL` entre dans une tendance propre ;
- `Pod C` essaie de suivre ce mouvement avec les mêmes briques d'exécution/risk que `Pod A` ;
- son univers est volontairement séparé de `Pod A`/`Pod B`, qui restent focalisés sur `crypto`.

Donc `Pod C` n'est plus un pod de lead-lag crypto opportuniste. C'est un pod **directionnel Tradfi HL**, cluster-aware (`index`, `gold`, `silver`, `oil`, `fx`, `equity`).

Dans la config dry-run actuelle, `Pod C` reste **actif**, mais avec un profil plus conservateur :

- `min_confidence = 0.66`
- `blocked_symbols = ["XYZ:GOLD"]`
- `cluster_aware_v2_enabled = true`

La logique `v2` actuellement active en dry-run est volontairement selective :

- `oil` : longs de pullback seulement ;
- `silver` : breakout long seulement ;
- `index` : breakout long seulement ;
- `gold` : toujours observe et collecte, mais bloque en execution via la risk gate ;
- pas de branche short dediee conservee pour l'instant.

Le but n'est pas de le couper, mais de limiter le bruit d'observabilite sur les patterns les moins convaincants.

### Qui décide quel pod gère quelle crypto ?

C'est `Trident` qui décide.

La règle importante est la suivante :

- **un symbole ne doit être géré que par un seul pod à la fois**.

Pourquoi ?

- pour éviter que deux pods prennent des décisions contradictoires sur le même marché ;
- pour garder une répartition claire de l'argent et du risque.

### À quoi servent les "modes de marché" ?

`Trident` essaie de reconnaître l'ambiance générale du marché :

- **marché en tendance** : le prix part clairement dans un sens ;
- **marché en range** : le prix monte et baisse sans vraie direction ;
- **marché très agité** : le prix bouge vite et fort ;
- **marché peu intéressant** : il vaut parfois mieux attendre.

Ensuite, il adapte la répartition :

- plus de place pour `Pod A` si le marché a une vraie direction ;
- plus de place pour `Pod B` si le marché peut offrir un sleeve crypto opportuniste, même avec peu de capital ;
- plus de place pour `Pod C` si le marché devient très nerveux ;
- parfois, une partie de l'argent reste simplement en cash si rien n'est propre.

### Les mots utiles à connaître

- `coin` ou `symbol` : un marché HL comme `BTC`, `ETH` ou `XYZ:SP500` ;
- `acheter` : parier que le prix va monter ;
- `vendre` ou `short` : parier que le prix va baisser ;
- `tendance` : un mouvement assez clair dans une direction ;
- `range` : un marché qui monte et baisse sans partir franchement ;
- `signal` : une idée d'action proposée par un pod ;
- `dry-run` : une simulation, sans argent réel engagé.

### La phrase la plus importante

`Trident` n'est pas "un bot unique qui fait tout". C'est **un coordinateur** qui :

- observe ;
- choisit le bon spécialiste ;
- limite le risque ;
- et évite que plusieurs spécialistes se marchent dessus.

Etat actuel:

- socle Python initialise,
- API HTTP stdlib minimale,
- dashboard HTML TRIDENT A/C sur `/` et `/dashboard` avec version git affichee,
- route JSON `/health` expose la version, le regime et l'etat du kill switch,
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
- runner live Pod B dans `app/live/pod_b_live_runner.py`,
- runner live Pod C dans `app/live/pod_c_live_runner.py`,
- snapshots live ecrits localement dans `data/live_snapshots/`,
- Pod A relie a un pipeline `snapshot -> signal -> trade plan -> risk -> dry-run execution`,
- Pod B relie au meme pipeline directionnel partage, avec une strategie breakout/vol-expansion crypto,
- Pod C relie au meme pipeline d'execution/risk que Pod A via un executor directionnel partage,
- Pod C v2 dry-run active un filtre cluster-aware plus strict, inspire des replays post-execution sur les snapshots fetched,
- convertisseur `gbot` capable de produire des snapshots enrichis a partir de `l2 + trades`,
- research Pod C present dans `app/research/pod_c_leadlag.py` et `app/research/pod_c_research_suite.py`,
- outillage Hydra research/shadow ajoute:
  - collecteur funding autonome dans `app/live/funding_collector.py`,
  - client funding/OI natif dans `app/hyperliquid/funding_client.py`,
  - dataset/research funding dans `app/research/pod_funding_dataset.py` et `app/research/pod_funding_research.py`,
  - features/research liq observables-first dans `app/research/pod_liq_features.py` et `app/research/pod_liq_research.py`,
  - note de faisabilite data dans `docs/pod_liq_data_feasibility.md`,
- protocole research Pod C documente dans `docs/pod_c_research_protocol.md`,
- artefacts de deploiement presents:
  - `Dockerfile.trident`
  - `docker-compose.trident.yml`
  - `prepare_server.sh`
  - `deploy.sh`
  - `trident-hip4/deploy.sh`
  - `scripts/trident_start.sh`
  - `scripts/trident_stop.sh`
  - `scripts/trident_restart.sh`
  - `scripts/trident_healthcheck.sh`
  - `scripts/trident_server.sh`
  - `scripts/trident_hip4_server.sh`
  - `scripts/fetch_trident_data.sh`
  - `trident-hip4/fetch_data.sh`
  - `docs/deployment.md`

Versioning:

- la version est derivee automatiquement de git: `short_hash (date du commit)`
- affichee dans le dashboard (chip "Version") et dans `/health` (champ `version`)
- le suffixe `-dirty` apparait si le code deploye a des modifications non committees
- module: `app/version.py`

Decision d'architecture actuelle:

- TRIDENT A/C et HIP-4 sont separes operationnellement:
  - TRIDENT (`./deploy.sh`) demarre API + `Pod A` + `Pod C`, UI A/C sur le port `3000`;
  - TRIDENT-HIP4 (`./trident-hip4/deploy.sh`) demarre l'API HIP-4 + `HIP4OutcomeEdgePod`, UI HIP-4 sur le port `3001`.
- L'ancien Pod B directionnel reste legacy; le Pod B courant est HIP-4 mainnet paper dans l'app separee.
- univers observe et univers trade sont separes:
  - `hyperliquid.observation_universe` = coins observes par le collector (crypto + tradfi)
  - le `supervisor` construit le pool tradable dynamiquement a partir des snapshots frais de cet univers observe
  - l'ownership effectif, l'allocation et le `managed_symbols` runtime sont decides par le `supervisor`, pas par la config runtime des pods
  - un pod a maintenant 2 scopes distincts:
    - `opening_symbols` = symbols sur lesquels il a encore le droit d'ouvrir de nouvelles positions / quotes
    - `managed_symbols` = symbols qu'il peut encore gerer et deboucler proprement, meme si le routeur les a remis a `none`
  - donc un `pod_x -> none` ne veut plus dire "abandon immediat du symbole":
    - Pod A / Pod C n'ouvrent plus de nouveau trade dessus, mais peuvent encore gerer la position deja ouverte
    - Pod B n'ouvre plus de nouveau trade dessus, mais garde la gestion de l'existant jusqu'a la sortie
  - il n'y a plus de liste `symbols` par pod a maintenir
  - si un symbol doit etre observe, il doit etre present dans `hyperliquid.observation_universe`
  - `pod_c.leader_symbols` reste utile pour exclure les leaders du pool follower de Pod C
  - le collector est automatiquement sharde si l'univers observe depasse la limite empirique stable de ~10 coins par connexion WS
- isolation par market cluster:
  - chaque symbol a un `market_cluster` (defaut: `crypto`, overrides: `index`, `gold`, `silver`, `oil`, `equity`)
  - Pod A et Pod B ne recoivent que les symbols dont le cluster est dans `pod_a.allowed_market_clusters` et `pod_b.allowed_market_clusters` (par defaut: `crypto`)
  - Pod C ne recoit que les symbols dont le cluster est dans `pod_c.allowed_market_clusters`
  - cette isolation empeche un pod crypto de trader accidentellement un instrument Tradfi et vice versa

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
   +--> Pod A = trend / swing crypto
   +--> Pod B = breakout / vol-expansion crypto
   +--> Pod C = tradfi / directional
   |
   v
Risk checks + execution + journal + dashboard
```

Le point cle:

- les pods peuvent tourner en meme temps
- ils ne doivent pas gerer le meme symbol en meme temps
- c'est le `supervisor` qui arbitre et evite les conflits
- chaque live runner (Pod A, Pod B, Pod C) utilise la config globale pour le routing
  (le supervisor voit les 3 pods et alloue les symbols en fonction du regime)
- quand un symbole sort du scope d'ouverture sans etre repris par un autre pod:
  - le pod garde un scope de gestion temporaire pour fermer proprement l'existant
  - il n'y a plus d'obligation de faire `pod_x -> pod_y` pour eviter un `routing_revoked` brutal
- Pod C ne recoit des symbols que si ses allocations de regime sont > 0%
  (dans la config actuelle, Pod C a des allocations globales et des budgets cluster actifs)
- la config Pod B est entierement centralisee dans `config/trident.toml`, comme les autres pods

Exemple d'ownership valide:

```text
BTC -> Pod A
ETH -> Pod A
XRP -> Pod B
XYZ:SP500 -> Pod C
```

Exemple invalide:

```text
BTC -> Pod A et Pod B en meme temps
```

Role de chaque pod:

- `Pod A`: moteur directionnel principal, pour les phases de tendance crypto (filtre: cluster `crypto` uniquement)
- `Pod B`: moteur breakout/vol-expansion crypto, reserve aux phases impulsives et breakouts propres, avec allocation possible meme en `DeadZone` pour surveiller le sleeve crypto (filtre: cluster `crypto` uniquement)
- `Pod C`: moteur directionnel Tradfi HL, scope actuel derive de `hyperliquid.observation_universe` et filtre par clusters `index`, `gold`, `silver`, `equity`, `oil`, `fx`; dans la config courante: `XYZ:CL`, `XYZ:BRENTOIL`, `XYZ:SP500`, `XYZ:XYZ100`, `XYZ:SILVER`, `XYZ:GOLD`, `XYZ:JPY`, `XYZ:TSLA`, `XYZ:NVDA`, `XYZ:CRCL`
- univers crypto courant: `BTC`, `ETH`, `SOL`, `HYPE`, `DOGE`, `XRP`, `SUI`, `AVAX`, `LINK`, `ARB`, `ADA`, `BNB`, `LTC`, `AAVE`, `NEAR`, `ZRO`, `ZEC`, `TAO`, `ENA`, `TON`, `BCH`
- waves d'extension actuellement retenues:
  - crypto wave 1 active: `ZEC`, `TAO`, `ENA`, `TON`, `BCH`
  - crypto wave 2 candidate: `WLD`, `XMR`, `CRV`, `UNI`, `DOT`
  - non-crypto wave 1 deja couverte par le scope builder-dex actuel: `XYZ:CL`, `XYZ:BRENTOIL`, `XYZ:SP500`, `XYZ:XYZ100`, `XYZ:SILVER`, `XYZ:GOLD`
  - non-crypto wave 2 candidate: `XYZ:JPY`, `XYZ:CRCL`, `XYZ:TSLA`, `XYZ:NVDA`
  - non-crypto wave 3 candidate: `XYZ:EWY`, `XYZ:EUR`, `XYZ:NATGAS`, `XYZ:INTC`, `XYZ:HOOD`
- les symbols builder-dex sont stockes en forme canonique `XYZ:SP500` / `XYZ:GOLD` / etc.
- au runtime, le collector WS traduit automatiquement vers le format HL attendu (`xyz:SP500`, `xyz:GOLD`, ...)
- les caps live et `allMids` sont aussi resolus par dex, donc `Pod C` recupere bien les `maxLeverage` / mids de ces marches et pas ceux du perp global
- sur les 3 pods, il faut maintenant distinguer:
  - "a le droit d'ouvrir" = scope d'entree courant decide par le routeur et le capital allocator
  - "a encore le droit de gerer" = scope elargi pour sortir proprement d'une position deja ouverte

Pourquoi `deploy.sh` a maintenant des flags `--without-...`:

- `deploy.sh` sert a la fois a copier le code et, avec `--start`, a demarrer les services
- par defaut, le demarrage lance le stack TRIDENT A/C: API + `Pod A` + `Pod C` + funding
- `--without-pod-c` retire `Pod C` et son collecteur Tradfi dedie
- `--without-funding` retire le collecteur funding global
- HIP-4 se deploie avec `./trident-hip4/deploy.sh`

Donc:

- `./deploy.sh` = deploie seulement
- `./deploy.sh --start` = deploie puis demarre TRIDENT A/C
- `./deploy.sh --start --without-pod-c` = demarre tout sauf `Pod C`
- `./deploy.sh --start --without-funding` = demarre tout sauf le funding global
- `./trident-hip4/deploy.sh --start` = deploie puis demarre HIP-4 mainnet paper

En pratique:

- `Pod A` est le pod le plus mature
- `Pod B`/HIP-4 vit dans `TRIDENT-HIP4`, separe de TRIDENT A/C et absent de l'UI TRIDENT
- `Pod C` est maintenant un pod Tradfi builder-dex actif dans la config
- il reste borne par ses budgets cluster (`index`, `gold`, `silver`, `oil`, `fx`, `equity`) et par ses caps de levier live par marche
- si on veut un lancement minimal, on coupe explicitement les briques non voulues avec `--without-...`

Reporting actuel:

- `state_payload` inclut maintenant `runtime_report`,
- `/api/report` expose un resume multi-pods runtime,
- `/health`, `/api/state`, `/api/metrics` et `/api/report` se recalent d'abord sur le dernier snapshot live frais pour eviter un regime stale,
- le dashboard TRIDENT affiche seulement Pod A/Pod C; l'UI HIP-4 dediee est servie par `TRIDENT-HIP4` sur `:3001`,
- le dashboard affiche aussi l'etat des `Data collectors`,
- l'onglet `System` expose maintenant `Pod C scope visibility` pour distinguer:
  - les symbols Tradfi configures pour `Pod C`
  - ceux qui sont effectivement observes par la vue live
  - ceux qui passent les gates de tradabilite
  - ceux qui sont vraiment routes a `Pod C`
- l'onglet `Status` affiche maintenant aussi `Régimes par cluster`:
  - `Crypto` pour le régime global BTC/crypto
  - une carte par cluster Tradfi actif (`index`, `gold`, `silver`, `oil`, `fx`, `equity`, ...)
  - avec pour chaque cluster le régime courant, le budget cible et le nombre de symbols observés/tradables
- si `Pod B` n'a pas de runtime frais, l'UI affiche explicitement `Supervisor fallback`:
  - cela signifie que le superviseur a un plan pour `Pod B`
  - mais qu'aucun runtime `logs/pod_b_live_status.json` frais n'a été vu
  - ce n'est plus présenté comme un pod healthy
- les replays de cohabitation ecrivent aussi un `summary` multi-pods dans leur JSON de sortie.
- `Pod A` et `Pod C` ferment maintenant une position si le `supervisor` retire le symbol ou remet son allocation a zero (`routing_revoked`),
- `Pod A` et `Pod C` n'utilisent plus exactement le meme scope pour ouvrir et pour fermer:
  - ils n'ouvrent plus si le symbole n'est plus dans le scope d'entree
  - mais ils peuvent encore gerer une position deja ouverte tant que le symbole reste dans leur scope de gestion
- `Pod B` a maintenant aussi cette separation:
  - `opening_symbols` = scope sur lequel le pod peut encore ouvrir un nouveau trade
  - `managed_symbols` = scope elargi qui permet de gerer et fermer proprement une position deja ouverte
  - quand un symbole sort du scope d'entree sans etre reassigne, Pod B garde l'existant mais n'ouvre plus
- les tables de trades `Pod A` / `Pod B` / `Pod C` sont maintenant homogenes et operatoires:
  - trades ouverts: `prix courant`, `valeur courante USD`, `marge utilisee`, `prix TP`, `prix SL`, `unrealized PnL`, `trailing TP`
  - trades fermes: raisons d'ouverture et de fermeture traduites en libelles lisibles
- les collectors funding ecrivent maintenant leurs runtime status:
  - `logs/funding_collector_status.json`
  - `logs/tradfi_funding_collector_status.json`
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
# Pas de `historical_replay` supporte:
# les candles HL seules ont ete invalidees pour TRIDENT, qui depend de snapshots minute `l2Book + trades`.
uv run python -m app.live.collector --coins BTC,ETH --max-runtime-seconds 8
uv run python -m app.live.pod_a_live_runner --coins BTC,ETH --max-runtime-seconds 8 --journal-output /workspaces/trident/data/live_snapshots/pod_a_live_journal.jsonl
uv run python -m app.live.pod_b_live_runner --coins BTC,ETH --max-runtime-seconds 8 --journal-output /workspaces/trident/data/live_snapshots/pod_b_live_journal.jsonl
uv run python -m app.live.pod_c_live_runner --coins XYZ:CL,XYZ:BRENTOIL,XYZ:SP500,XYZ:XYZ100,XYZ:SILVER,XYZ:GOLD,XYZ:JPY,XYZ:TSLA,XYZ:NVDA,XYZ:CRCL --max-runtime-seconds 8 --journal-output /workspaces/trident/data/live_snapshots/pod_c_live_journal.jsonl
uv run python -m app.live.tradfi_funding_collector --poll-seconds 60 --output /workspaces/trident/data/funding_history/pod_c_tradfi.jsonl
uv run python -m app.backtest.pod_b_runner --config config/trident.toml --input /workspaces/trident/server-data/live_snapshots --output /tmp/pod_b_report.json
python3.12 -m app.backtest.pod_c_runner --input /workspaces/trident/data/live_snapshots/2026-04-05.jsonl --output /tmp/pod_c_journal.jsonl
python3.12 -m app.research.pod_c_research_suite --input /workspaces/trident/data/live_snapshots/2026-04-05.jsonl --leader-symbols BTC,ETH --follower-symbols SOL,HYPE,SUI --output-json /tmp/pod_c_research.json --output-md /tmp/pod_c_research.md
python3.12 -m app.reporting.export_daily --pod-b-report /tmp/pod_b_report.json --reference-equity-usd 1000 --cash-balance-usd 1001 --output-json /tmp/trident_daily_summary.json --output-md /tmp/trident_daily_summary.md
./scripts/trident_healthcheck.sh
./scripts/fetch_trident_data.sh
./scripts/fetch_trident_data.sh --days 3
./scripts/fetch_trident_data.sh --date 2026-04-05
./trident-hip4/fetch_data.sh
./prepare_server.sh trident-hetzner
./deploy.sh --start
./trident-hip4/deploy.sh --start
```

Rapatriement et analyse locale:

- `./scripts/trident_dry_run_review.sh`
  - revue historique combinee, gardee pour compatibilite
  - preferer les fetchs separes TRIDENT A/C et TRIDENT-HIP4 pour les nouveaux checks
- `./scripts/fetch_trident_data.sh`
  - rapatrie les snapshots live, logs runtime, statuses, snapshots API et logs Docker
  - ne rapatrie plus Pod B/HIP-4
  - permet une vraie analyse locale plus complete sur plusieurs heures / jours
- `./trident-hip4/fetch_data.sh`
  - rapatrie les logs/runtime/configs HIP-4 depuis `/opt/trident-hip4`
  - interroge l'API HIP-4 sur le port `3001` par defaut
  - genere la review HIP-4 outcome depuis les logs `paper/testnet/mainnet`
- les reviews separees gardent chacune leur périmètre: santé/réconciliation A/C
  côté TRIDENT, readiness/calibration/logs côté HIP-4

Exemples:

```bash
./scripts/fetch_trident_data.sh
./scripts/fetch_trident_data.sh --days 2
./scripts/fetch_trident_data.sh --snapshots-only --days 5
./scripts/fetch_trident_data.sh --logs-only
./scripts/fetch_trident_data.sh --review-only
./trident-hip4/fetch_data.sh
./trident-hip4/fetch_data.sh --review-only
```

La review HIP-4 est ecrite dans le dossier HIP-4 separe et en alias latest:

```text
server-data/hip4/replay_reports/hip4_outcome_run_review_latest.md
server-data/hip4/replay_reports/hip4_outcome_run_review_latest.json
```

Elle inclut aussi une simulation de candidats guardrails: slices a exclure,
impact PnL/PF/Brier apres exclusion, et distinction entre regles entry-time
actionnables et diagnostics post-trade.
Le guardrail testnet actif se configure via `blocked_opportunity_slices` dans
`config/hip4_outcome_testnet.toml`.

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
- replay fetched complet avec le Pod B directionnel actuel:
  - `2026-04-05 -> 2026-04-12`
  - total bot `+221.68 USD`
  - `Pod A = +202.68 USD`
  - `Pod B = +19.00 USD`
  - `Pod C = 0 USD`
  - version retenue: runtime/UI/deploiement/fetch alignes sur le nouveau Pod B officiel
- ancienne suite research Pod C sur `data/live_snapshots/2026-04-05.jsonl`:
  - ce bloc correspond a l'ancien Pod C research/lead-lag
  - il ne decrit pas le Pod C builder-dex actuel

Replay full-bot 2026-04-05 -> 2026-04-10 (historique, avant le remplacement du Pod B et avant la migration builder-dex de Pod C):

- Pod A: `+429.07 USD`, 130 trades, 62.3% win rate, max_leverage=10x
- Pod B historique: `-0.61 USD`, 1349 trades de l'ancien moteur maker
- Pod C: `0 USD`, 0 trades (ancien scope/config, avant l'activation du panier builder-dex actuel)
- total: `+428.46 USD`

Limite connue:

- `docker compose` n'a pas pu etre valide dans cet environnement car le binaire `docker` n'est pas installe localement.
- le rate limiter central n'a pas encore ete valide sous charge massive reelle multi-process contre HL, seulement par tests et smokes locaux.
