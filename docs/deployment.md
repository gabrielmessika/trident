# TRIDENT Deployment Guide

## Objectif

Déployer `trident` sur un serveur Hetzner Cloud sans surcouches inutiles :

- préparation initiale du serveur depuis la machine locale
- déploiement via `rsync`
- build Docker directement sur le serveur
- contrôle simple depuis le serveur avec un seul script

Le workflow est inspiré de `gbot`, mais adapté à l'architecture actuelle de `trident` :

- `docker-compose.trident.yml`
- API liée à `127.0.0.1:3000` uniquement
- accès UI via tunnel SSH

Par défaut, `deploy.sh` utilise l'alias SSH :

```text
trident-hetzner
```

Tu n'as donc pas besoin de passer `--host` si cet alias existe dans `~/.ssh/config`.

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
    User root
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

Pour démarrer tous les pods :

```bash
./deploy.sh --start --with-pod-b --with-pod-c
```

Important :

- Pod B nécessite `runtime/passivbot/live.json` sur le serveur
- Pod C n'a de sens que si sa recherche a conclu à un `go`

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

Rebuild + redémarrage :

```bash
./scripts/trident_server.sh update
```

Rebuild + redémarrage avec Pod B :

```bash
./scripts/trident_server.sh update --with-pod-b
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

Scripts raccourcis également disponibles :

```bash
./scripts/trident_start.sh
./scripts/trident_stop.sh
./scripts/trident_restart.sh
./scripts/trident_healthcheck.sh
```

---

## 5. Accéder à l'UI

L'API n'est pas exposée publiquement. Le port `3000` est lié à `127.0.0.1` sur le serveur.

Depuis la machine locale :

```bash
ssh -L 3000:127.0.0.1:3000 trident-hetzner
```

Ou, si ton alias `trident-hetzner` utilise `root`, avec l'utilisateur de déploiement :

```bash
ssh -i ~/.ssh/trident_hetzner_ed25519 -L 3000:127.0.0.1:3000 trident-deploy@46.224.43.198
```

Puis ouvrir :

```text
http://localhost:3000
```

Routes utiles :

- `/`
- `/dashboard`
- `/health`
- `/api/state`
- `/api/metrics`
- `/api/report`

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

## 8. Points d'attention

- ne pas commiter de secrets
- ne pas exposer le port `3000` publiquement
- Pod B ne doit pas être lancé sans `runtime/passivbot/live.json`
- Pod C ne doit pas être activé par réflexe si la recherche est `no-go`
- le rate limiter partagé HL vit sur disque et doit rester persistant entre les runs

---

## 9. Commandes de base

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

Contrôler sur le serveur :

```bash
cd /opt/trident
./scripts/trident_server.sh status
./scripts/trident_server.sh logs trident-api
./scripts/trident_server.sh health
```
