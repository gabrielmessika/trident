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
- `rsync`
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

Pour déployer puis démarrer l'API et Pod A :

```bash
./deploy.sh --start
```

Pour démarrer aussi Pod B :

```bash
./deploy.sh --start --with-pod-b
```

Pour démarrer aussi Pod C :

```bash
./deploy.sh --start --with-pod-c
```

Pour démarrer aussi le collecteur funding/OI autonome :

```bash
./deploy.sh --start --with-funding
```

Pour démarrer tous les pods :

```bash
./deploy.sh --start --with-pod-b --with-pod-c --with-funding
```

Important :

- Pod B utilise `config/trident.toml` (plus besoin de `runtime/passivbot/live.json`)
- Pod C n'a de sens que si sa recherche a conclu à un `go`
- `--with-funding` lance un collecteur séparé qui écrit `data/funding_history/current.jsonl`
- `--with-pod-b` et `--with-pod-c` activent maintenant aussi logiquement les pods côté superviseur/UI, pas seulement leurs containers Docker
- les noms `pod-a-live`, `pod-b-live`, `pod-c-live` sont des noms de services Docker historiques
- aujourd'hui, `Pod A`, `Pod B` et `Pod C` tournent encore en dry-run / paper trading, pas en exécution réelle exchange

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

Démarrer API + Pod A :

```bash
./scripts/trident_server.sh start
```

Démarrer API + Pod A + Pod B :

```bash
./scripts/trident_server.sh start --with-pod-b
```

Démarrer API + Pod A + Pod C :

```bash
./scripts/trident_server.sh start --with-pod-c
```

Démarrer API + Pod A + collecteur funding/OI :

```bash
./scripts/trident_server.sh start --with-funding
```

Rebuild + redémarrage :

```bash
./scripts/trident_server.sh update
```

Rebuild + redémarrage avec Pod B :

```bash
./scripts/trident_server.sh update --with-pod-b
```

Rebuild + redémarrage avec funding/OI :

```bash
./scripts/trident_server.sh update --with-funding
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

- `status`, `restart`, `stop` et `logs` doivent recevoir les mêmes flags `--with-pod-b` / `--with-pod-c` si tu veux voir ou piloter exactement le même sous-ensemble de services que celui lancé
- exemple :

```bash
./scripts/trident_server.sh status --with-pod-b
./scripts/trident_server.sh logs --with-pod-b pod-b-live
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
./deploy.sh --start
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
- génère :
  - `review_summary.md`
  - `review_summary.json`
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
- `logs/pod_b_live_report.json`
- `logs/pod_a_live_status.json`
- `logs/pod_c_live_status.json`
- `runtime/passivbot/live.status.json`
- snapshots API courants
- tails de logs Docker

Puis il peut relancer automatiquement la revue locale avec suggestions de prompts.

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
- Pod B utilise désormais `config/trident.toml` et le superviseur partagé (plus de dépendance à `runtime/passivbot/live.json`)
- Pod C ne doit pas être activé par réflexe si la recherche est `no-go`
- le rate limiter partagé HL vit sur disque et doit rester persistant entre les runs

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

Déployer + démarrer :

```bash
./deploy.sh --start
```

Déployer + démarrer Pod A et Pod B :

```bash
./deploy.sh --start --with-pod-b
```

Contrôler sur le serveur :

```bash
cd /opt/trident
./scripts/trident_server.sh status
./scripts/trident_server.sh logs trident-api
./scripts/trident_server.sh health
```
