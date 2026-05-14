# TRIDENT Deployment Guide

## Objectif

Déployer `trident` sur un serveur Hetzner Cloud sans surcouches inutiles :

- préparation initiale du serveur depuis la machine locale
- déploiement via `rsync`
- build Docker directement sur le serveur
- contrôle simple depuis le serveur avec un seul script

Le workflow est inspiré de `gbot`, mais adapté à l'architecture actuelle de `trident` :

- `docker-compose.trident.yml`
- API exposée publiquement sur `0.0.0.0:3000`
- dashboard accessible directement via l'IP ou le DNS du serveur

Par défaut, `deploy.sh` utilise l'alias SSH :

```text
trident-hetzner
```

Tu n'as donc pas besoin de passer `--host` si cet alias existe dans `~/.ssh/config`.

Par défaut, `deploy.sh` utilise aussi :

- l'utilisateur SSH `trident-deploy`
- la clé `~/.ssh/trident_hetzner_ed25519`

---

## 0. Pré-requis

Sur la machine locale :

- une clé SSH dédiée, par exemple `~/.ssh/trident_hetzner_ed25519`
- un alias SSH fonctionnel ou l'IP du serveur
- `rsync` ou un gestionnaire de paquets supporté (`dnf`, `yum`, `apt-get`, `apk`, `pacman`, `zypper`, `brew`)
- `ssh`

Exemple de `~/.ssh/config` :

```sshconfig
Host trident-hetzner
    HostName 46.224.43.198
    User trident-deploy
    IdentityFile ~/.ssh/trident_hetzner_ed25519
```

Test :

```bash
ssh trident-hetzner echo ok
```

---

## 1. Préparer le serveur

À lancer depuis la machine locale :

```bash
cd /workspaces/trident
./prepare_server.sh 46.224.43.198
```

Ou avec un alias SSH :

```bash
./prepare_server.sh trident-hetzner
```

Le script :

- met à jour Ubuntu
- installe Docker + docker compose plugin
- installe `ufw`, `fail2ban`, `unattended-upgrades`
- crée l'utilisateur `trident-deploy`
- prépare `/opt/trident`
- désactive l'authentification SSH par mot de passe
- augmente les limites `nofile`
- ouvre `3000/tcp` dans `ufw`

Répertoire cible sur le serveur :

```text
/opt/trident
```

---

## 2. Déployer le code

Depuis la machine locale :

```bash
cd /workspaces/trident
./deploy.sh
```

Tu peux aussi forcer un host explicite :

```bash
./deploy.sh --host trident-hetzner
./deploy.sh --host 46.224.43.198
```

Ce que fait `deploy.sh` :

1. vérifie les prérequis locaux
2. copie le repo vers `/opt/trident` via `rsync`
3. exclut les gros datasets et les répertoires runtime locaux
4. build l'image Docker sur le serveur
5. laisse les fichiers `runtime/`, `logs/` et `data/` côté serveur intacts

Note opératoire :

- `deploy.sh` continue de requérir `rsync` installé localement.
- En revanche, `scripts/fetch_trident_data.sh` sait désormais tenter une installation automatique de `rsync` côté machine locale si le binaire a disparu après rebuild de l'environnement.

Exclusions principales :

- `.git`
- `.venv`
- `data/gbot_archive`
- `data/server_archive`
- `data/replay_reports`
- `data/live_snapshots`
- `data/funding_history`
- `data/research`
- `docs/pod_funding_research_latest.json`
- `docs/pod_funding_research_latest.md`
- `docs/pod_liq_research_latest.json`
- `docs/pod_liq_research_latest.md`
- `logs`
- `runtime`
- `.env.trident`

---

## 3. Déployer et démarrer

Pour déployer puis démarrer tout le stack par défaut en dry-run préparatoire :

```bash
./deploy.sh --start --mode dry-run
```

`--mode dry-run` est le défaut, mais il est recommandé de le passer
explicitement avant un passage live pour éviter toute ambiguïté.

Pour préparer un build/config live sans démarrer les services :

```bash
./deploy.sh --mode live --config config/trident.toml
```

Le premier démarrage live réel lance Pod A + Pod C et passe par un preflight
exchange/WS obligatoire pour chaque pod. Pod B reste exclu :

```bash
./deploy.sh --start --mode live --without-pod-b --without-funding
```

Il échoue fail-closed si les credentials, la reconciliation exchange ou le flux
`orderUpdates` ne sont pas prêts.

Pour un live Pod A seul, ajouter `--without-pod-c`.

Pour démarrer tout sauf Pod C :

```bash
./deploy.sh --start --mode dry-run --without-pod-c
```

Pour démarrer tout sauf le collecteur funding/OI global :

```bash
./deploy.sh --start --mode dry-run --without-funding
```

Pour démarrer tout sauf Pod B :

```bash
./deploy.sh --start --mode dry-run --without-pod-b
```

Important :

- Pod B est maintenant un pod directionnel breakout pilote par le superviseur et expose son runtime via `logs/pod_b_live_status.json`
- le service Docker `pod-b-live` lance maintenant `app.live.pod_b_live_runner` directement
- Pod C utilise maintenant un panier Tradfi builder-dex actif dans la config; le démarrer sans lui reste utile seulement si on veut un run minimal ou si on désactive explicitement son scope
- en dry-run courant, `Pod C` active aussi `cluster_aware_v2_enabled = true`:
  - `oil` en longs de pullback
  - `silver` en breakout long
  - `index` en breakout long
  - `GOLD` reste collecte pour les snapshots/funding/replays, meme s'il est bloque a l'execution par `pod_c.blocked_symbols`
- `./deploy.sh --start` lance maintenant aussi le collecteur funding global `data/funding_history/current.jsonl`
- `Pod C` lance en plus son collecteur Tradfi dédié `data/funding_history/pod_c_tradfi.jsonl`
- le collecteur funding global écrit aussi `logs/funding_collector_status.json`
- le collecteur funding Tradfi écrit `logs/tradfi_funding_collector_status.json`
- les noms `pod-a-live`, `pod-b-live`, `pod-c-live` sont des noms de services Docker historiques
- aujourd'hui, `Pod A`, `Pod B` et `Pod C` tournent encore en dry-run / paper trading par défaut
- le chemin live réel est préparé pour un canary `Pod A` + `Pod C`; `Pod B`
  reste exclu du premier lancement live
- l'UI `System` montre désormais explicitement:
  - `Data collectors` pour la santé des services funding
  - `Pod C scope visibility` pour voir quels symbols Tradfi sont configures, observes, tradables et routes
- l'onglet `Status` montre aussi `Régimes par cluster`:
  - régime crypto global
  - régimes actifs des clusters Tradfi
  - budget cible et couverture observée/tradable par cluster

### Variables live serveur

Pour un canary live, garder ces variables uniquement dans
`/opt/trident/.env.trident` côté serveur :

```bash
TRIDENT_MODE=live
TRIDENT_ACCOUNT_ADDRESS=0x...
TRIDENT_SECRET_KEY=0x...
TRIDENT_VAULT_ADDRESS=
TRIDENT_LIVE_CONFIRM=I_UNDERSTAND_REAL_ORDERS
TRIDENT_LIVE_STATE_PATH_POD_A=
TRIDENT_LIVE_STATE_PATH_POD_C=
```

`TRIDENT_ACCOUNT_ADDRESS` doit être l'adresse réelle du compte/subaccount à
réconcilier. `TRIDENT_SECRET_KEY` doit être une API wallet approuvée, pas une
clé à déplacer dans le repo.

Pour alimenter les observations TP/SL publiques sans node Hyperliquid local,
ajouter aussi l'endpoint QuickNode Hyperliquid dans le même fichier serveur :

```bash
TRIDENT_TRIGGER_LIQUIDITY_QUICKNODE_URL=https://...hype-mainnet.quiknode.pro/...
TRIDENT_TRIGGER_LIQUIDITY_QUICKNODE_STREAM=orders
TRIDENT_TRIGGER_LIQUIDITY_QUICKNODE_BATCH_SIZE=5
TRIDENT_TRIGGER_LIQUIDITY_QUICKNODE_INITIAL_LOOKBACK_BLOCKS=25
TRIDENT_TRIGGER_LIQUIDITY_QUICKNODE_MAX_BLOCKS_PER_POLL=300
TRIDENT_TRIGGER_LIQUIDITY_POLL_SECONDS=1
TRIDENT_TRIGGER_LIQUIDITY_SQL_API_KEY=
```

Le collector normalise automatiquement l'URL vers `/hypercore` et ne logge que
la partie non secrète. Les comptes QuickNode Discover/free limitent
`hl_getBatchBlocks` à des ranges de 5 blocs; ces valeurs permettent de démarrer
la collecte JSON-RPC sans WebSocket.

Pour reconstruire l'historique TP/SL depuis SQL Explorer, lancer ensuite côté
serveur :

```bash
cd /opt/trident
docker compose --env-file .env.trident -f docker-compose.trident.yml run --rm --no-deps trident-api \
  python -m app.live.trigger_liquidity_sql_backfill \
    --start 2026-04-01 \
    --end 2026-05-15 \
    --output-dir data/trigger_liquidity
```

---

## 4. Contrôler le bot depuis le serveur

Une fois connecté :

```bash
ssh trident-hetzner
cd /opt/trident
```

Si ton alias SSH pointe encore sur `root`, tu peux aussi te connecter explicitement avec l'utilisateur de déploiement :

```bash
ssh -i ~/.ssh/trident_hetzner_ed25519 trident-deploy@46.224.43.198
```

Le script principal est :

```bash
./scripts/trident_server.sh
```

### Commandes utiles

Démarrer tout en dry-run :

```bash
./scripts/trident_server.sh start --mode dry-run
```

Démarrer tout sauf Pod B :

```bash
./scripts/trident_server.sh start --mode dry-run --without-pod-b
```

Démarrer tout sauf Pod C :

```bash
./scripts/trident_server.sh start --mode dry-run --without-pod-c
```

Démarrer tout sauf funding/OI global :

```bash
./scripts/trident_server.sh start --mode dry-run --without-funding
```

Rebuild + redémarrage :

```bash
./scripts/trident_server.sh update --mode dry-run
```

Rebuild + redémarrage sans Pod C :

```bash
./scripts/trident_server.sh update --mode dry-run --without-pod-c
```

Rebuild + redémarrage sans funding/OI global :

```bash
./scripts/trident_server.sh update --mode dry-run --without-funding
```

Arrêter :

```bash
./scripts/trident_server.sh stop
```

Redémarrer :

```bash
./scripts/trident_server.sh restart
```

Statut :

```bash
./scripts/trident_server.sh status
```

Logs :

```bash
./scripts/trident_server.sh logs
./scripts/trident_server.sh logs trident-api
./scripts/trident_server.sh logs pod-a-live
```

Health check :

```bash
./scripts/trident_server.sh health
```

Point pratique :

- `status`, `restart`, `stop` et `logs` prennent par defaut tout le stack; utilise les memes flags `--without-...` si tu veux piloter exactement le meme sous-ensemble de services que celui lance
- exemple :

```bash
./scripts/trident_server.sh status --without-pod-c
./scripts/trident_server.sh logs --without-pod-c pod-b-live
```

Scripts raccourcis également disponibles :

```bash
./scripts/trident_start.sh
./scripts/trident_stop.sh
./scripts/trident_restart.sh
./scripts/trident_healthcheck.sh
./scripts/fetch_trident_data.sh
```

---

## 5. Accéder à l'UI

L'API est exposée publiquement sur le port `3000`.

Depuis n'importe quelle machine, ouvrir :

```text
http://46.224.43.198:3000
```

Ou, si tu as un DNS devant le serveur :

```text
http://ton-domaine:3000
```

Si le serveur a ete prepare avant cette modification, ouvre aussi le firewall une fois :

```bash
ssh trident-hetzner
sudo ufw allow 3000/tcp
sudo ufw status
```

Routes utiles :

- `/`
- `/dashboard`
- `/health`
- `/api/state`
- `/api/metrics`
- `/api/report`

Important :

- l'alias SSH `trident-hetzner` est pratique pour `ssh` et `deploy.sh`
- dans le navigateur, utilise l'IP publique ou un vrai DNS, pas forcément l'alias SSH local

---

## 6. Mise à jour standard

Workflow recommandé :

1. modifier le code localement
2. valider localement
3. déployer
4. redémarrer proprement

Exemple :

```bash
cd /workspaces/trident
python3.12 -m unittest discover -s tests -v
./deploy.sh --start --mode dry-run
```

Si tu es déjà sur le serveur et que le code est déjà synchronisé :

```bash
cd /opt/trident
./scripts/trident_server.sh update
```

---

## 7. Fichiers importants côté serveur

Application :

```text
/opt/trident
```

Logs :

```text
/opt/trident/logs
```

Runtime :

```text
/opt/trident/runtime
```

Snapshots live :

```text
/opt/trident/data/live_snapshots
```

Rate limiter partagé HL :

```text
/opt/trident/runtime/hyperliquid_rate_limits.json
```

---

## 8. Rapatrier les données pour analyse locale

Deux niveaux existent :

1. revue légère distante
2. fetch complet pour analyse locale

### Revue légère

```bash
cd /workspaces/trident
./scripts/trident_dry_run_review.sh
```

Ce script :

- récupère l'état courant (`/health`, `/api/state`, `/api/metrics`, `/api/report`)
- récupère les tails de logs Docker
- vérifie la fraîcheur des snapshots
- si un cache local est fourni, génère aussi la review HIP-4 outcome complète
- génère :
  - `review_summary.md`
- `review_summary.json`
- `hip4_outcome_run_review.md`
- `hip4_outcome_run_review.json`

La review HIP-4 contient aussi une section `Guardrail Candidates` pour simuler
l'effet des exclusions avant de les transformer en regles live.
  - des prompts LLM quand un jugement qualitatif est utile

### Fetch complet

```bash
cd /workspaces/trident
./scripts/fetch_trident_data.sh
```

Ce script est inspiré de `gbot/fetch-data.sh`, mais garde les capacités de revue simple de `trident_dry_run_review.sh`.

Il rapatrie localement :

- `data/live_snapshots/*.jsonl`
- `logs/pod_a_live.jsonl`
- `logs/pod_b_live.jsonl`
- `logs/pod_c_live.jsonl`
- `logs/pod_a_live_status.json`
- `logs/pod_b_live_status.json`
- `logs/pod_c_live_status.json`
- snapshots API courants
- tails de logs Docker
- logs HIP-4 `paper`, `testnet`, `mainnet observer`

Point utile :

- le fetch ne lit plus l'ancien runtime `runtime/passivbot/*`
- si le dashboard affiche `Supervisor fallback` pour Pod B, le fetch te permet maintenant de verifier directement si `logs/pod_b_live_status.json` est frais ou non

Puis il peut relancer automatiquement la revue locale avec suggestions de prompts.
La review HIP-4 est aussi copiée en alias latest sous `server-data/replay_reports/hip4_outcome_run_review_latest.{md,json}`.

Exemples :

```bash
./scripts/fetch_trident_data.sh
./scripts/fetch_trident_data.sh --days 3
./scripts/fetch_trident_data.sh --date 2026-04-05
./scripts/fetch_trident_data.sh --snapshots-only --days 5
./scripts/fetch_trident_data.sh --logs-only
./scripts/fetch_trident_data.sh --review-only
./scripts/fetch_trident_data.sh --skip-review
```

Dossier local par défaut :

```text
/workspaces/trident/server-data
```

La revue générée par défaut est écrite dans :

```text
/workspaces/trident/server-data/reviews/<timestamp>
```

---

## 9. Points d'attention

- ne pas commiter de secrets
- si le dashboard est expose publiquement, considerer qu'il revele l'etat runtime et le PnL
- Pod B est maintenant un pod directionnel breakout, avec runtime status dans `logs/pod_b_live_status.json`
- Pod C ne doit pas être modifié par réflexe sans revérifier son univers builder-dex, ses caps live et son replay associé
- le rate limiter partagé HL vit sur disque et doit rester persistant entre les runs
- l'univers observe est plus large qu'au bootstrap, mais le coût live augmente vraiment avec chaque coin ajoute:
  - plus de shards WS
  - plus de messages
  - plus de churn possible dans le superviseur
  - elargir par petites vagues reste recommande

---

## 10. Commandes de base

Préparer :

```bash
./prepare_server.sh trident-hetzner
```

Déployer :

```bash
./deploy.sh
```

Démarrer le dry-run préparatoire :

```bash
./deploy.sh --start --mode dry-run
```

Déployer + démarrer :

```bash
./deploy.sh --start --mode dry-run
```

Déployer + démarrer sans Pod C :

```bash
./deploy.sh --start --mode dry-run --without-pod-c
```

Contrôler sur le serveur :

```bash
cd /opt/trident
./scripts/trident_server.sh status
./scripts/trident_server.sh logs trident-api
./scripts/trident_server.sh health
```
