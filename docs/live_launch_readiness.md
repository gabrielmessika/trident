# TRIDENT - Readiness passage live

Date de revue: 2026-04-29. Mise à jour live hybride: 2026-05-17.

Verdict code: **canary live Pod A + Pod C préparé**, avec **Pod B HIP-4 maintenu en mainnet paper**.

Verdict opérationnel: **go technique conditionnel**. Le démarrage live reste
bloqué tant que le preflight live ne passe pas sur le serveur avec les vrais
credentials, un compte/subaccount propre et `orderUpdates` connecté.

Cette revue consolide les documents TRIDENT, tbot et gbot pour éviter de
répéter les erreurs déjà observées: état exchange non prioritaire au redémarrage,
positions dupliquées, ordres de fermeture mal gérés, erreurs API interprétées
comme zéro position, fill/triggers incomplets, websocket utilisateur absent.

## Sources relues

- TRIDENT: `README.md`, `docs/deployment.md`, `docs/trident_active_plan.md`,
  `plan_trident.md`, scripts de déploiement et runners live.
- tbot: `../tbot-scalp/CHANGELOG.md`.
- gbot: `../gbot/README.md`, `../gbot/plan_old1.md`, `../gbot/src/main.rs`,
  `../gbot/src/exchange/rest_client.rs`, `../gbot/src/exchange/ws_client.rs`.

## Etat actuel

TRIDENT garde son architecture de **dry-run sur données live** par défaut, mais
dispose maintenant d'un chemin live hybride:

- `--mode dry-run` reste le mode par défaut;
- `--mode live` lance Pod A + Pod C en vrais ordres et garde Pod B HIP-4 en
  mainnet paper;
- le démarrage live exécute un preflight bloquant par pod avant de lancer les
  services;
- le serveur force `HIP4_OUTCOME_CONFIG=config/hip4_outcome_mainnet_paper.toml`,
  `HIP4_OUTCOME_MODE=paper` et `HIP4_OUTCOME_ALLOW_TESTNET_ORDERS=false` en
  mode live;
- les runners Pod A/Pod C live ne tronquent pas le journal, ne ferment pas les
  positions à l'arrêt du process, et persistent chacun leur état live:
  `runtime/trident/live_state_pod_a.json` et
  `runtime/trident/live_state_pod_c.json`.

Les scripts de déploiement acceptent maintenant un mode explicite:

- `./deploy.sh --start --mode dry-run` démarre le dry-run préparatoire;
- `./deploy.sh --mode live` prépare un build/config live sans démarrage;
- `./deploy.sh --start --mode live --without-funding` démarre Pod A + Pod C
  en live et Pod B HIP-4 en paper seulement si le preflight live passe.

## Protections P0 implémentées

### 1. Etat exchange autoritaire avant toute décision

Au démarrage, le bot doit reconstruire son état depuis Hyperliquid avant de
calculer un signal ou d'ouvrir un trade:

- positions ouvertes par symbole, sens, taille, prix moyen, levier, margin;
- open orders simples;
- trigger orders TP/SL/trailing via `frontendOpenOrders`;
- fills récents via `userFillsByTime`;
- equity, capital disponible, account value, spot balance/hold si pertinent;
- PnL réalisé et latent si utilisé par le sizing ou les kill-switches.

Règle dure: **une erreur API n'est jamais une position zéro**. Si l'appel échoue,
le bot doit se mettre en pause et conserver l'état précédent.

Implémenté dans:

- `app/hyperliquid/private_state.py`;
- `app/live/reconciliation.py`;
- `app/live/preflight.py`;
- `app/live/pod_a_live_runner.py`;
- `app/live/pod_c_live_runner.py`.

### 2. Websocket utilisateur en source primaire de fills

Les docs gbot imposent `orderUpdates` comme source primaire des fills, avec REST
`userFillsByTime` seulement en fallback après coupure WS. TRIDENT ne doit pas
inverser cette priorité.

Minimum live:

- subscription `orderUpdates` par wallet/subaccount;
- détection de déconnexion et resync REST obligatoire;
- déduplication par `oid`/`cloid`/timestamp;
- journal des transitions `pending -> open -> closing -> closed`;
- blocage des nouvelles entrées si le flux utilisateur est absent.

Implémenté:

- preflight `orderUpdates` obligatoire;
- moniteur `orderUpdates` en tâche de fond;
- nouvelles entrées Pod A/Pod C bloquées si le stream n'est pas sain.

### 3. Pas de double ouverture sur symbole déjà exposé

Avant toute ouverture:

- vérifier position réelle exchange sur le symbole;
- vérifier ordre d'ouverture pending;
- vérifier trigger/close pending;
- vérifier ownership Pod A/B/C;
- vérifier cooldown/reentry;
- vérifier que le symbole est dans `entry_allowed_symbols`.

Si une position existe côté exchange mais pas côté journal, le bot doit la
recover ou la mettre en quarantaine, jamais ouvrir par-dessus.

Implémenté:

- récupération si la position exchange correspond au state store du pod
  propriétaire;
- tolérance stricte des positions connues par l'autre pod actif
  (`external_known_positions`) sans les gérer localement;
- blocage si position inconnue;
- blocage si open order/trigger inconnu.

### 4. Fermeture et triggers réellement sécurisés

Le dry-run ferme localement une position. Le live doit:

- envoyer un ordre reduce-only;
- attendre la confirmation fill;
- gérer partial fills;
- recalculer SL/TP après fill moyen réel;
- placer TP/SL et vérifier les erreurs de retour, notamment `err`;
- cancel+replace correctement les stops break-even/trailing;
- nettoyer les triggers orphelins après fermeture ou recovery.

Implémenté pour le canary:

- ordres IOC via SDK officiel Hyperliquid;
- close reduce-only;
- SL/TP reduce-only trigger après fill d'entrée;
- cancel des triggers connus après close;
- parsing strict des réponses `filled`, `resting`, `error`.

### 5. Capital/equity exchange, pas capital config

Le sizing live ne doit pas partir de `reference_equity_usd` seul. Leçons tbot:

- ne pas faire deux appels `spotClearinghouseState` séparés pour total/hold;
- éviter le double comptage spot hold + accountValue;
- cross-check equity exchange contre dashboard Hyperliquid;
- bloquer si equity varie de façon impossible entre deux lectures;
- distinguer account value, spot balance, available balance et margin utilisée.

Implémenté au preflight/reconciliation:

- lecture `clearinghouseState`;
- lecture `spotClearinghouseState` en un seul appel;
- exposition account value, withdrawable, margin used, USDC total/hold.

### 6. Erreurs API et rate limits propagés

Leçons gbot/tbot:

- 429 ou timeout ne doit pas devenir `[]` positions;
- tous les endpoints privés doivent avoir timeout, retry borné, rate limiter
  pondéré par endpoint et métriques;
- les réponses d'ordre doivent être parsées strictement;
- tout champ critique manquant doit bloquer le trading réel.

Implémenté pour le canary A/C:

- les lectures privées passent par le rate limiter partagé
  `private_info_requests_per_minute`;
- les actions live `order/cancel` passent par
  `live_order_actions_per_minute`;
- un signal 429/rate-limit ouvre le breaker partagé avant nouvelle tentative.

### 7. Deploy live sans ambiguïté

Un live sûr doit avoir:

- mode explicite `dry-run` ou `live`;
- service/journal/status clairement annotés par mode;
- live bloqué si wallet/API secret absent;
- live bloqué si reconciliation startup incomplète;
- live bloqué si user websocket non connecté;
- live bloqué si positions/open orders non reconnus;
- live bloqué si `fresh-start` risque d'effacer un état nécessaire à la
  réconciliation;
- confirmation explicite pour ordres réels, par exemple
  `TRIDENT_LIVE_CONFIRM=I_UNDERSTAND_REAL_ORDERS`.

Implémenté:

- `TRIDENT_ACCOUNT_ADDRESS`, `TRIDENT_SECRET_KEY`, `TRIDENT_VAULT_ADDRESS`;
- confirmation obligatoire `TRIDENT_LIVE_CONFIRM=I_UNDERSTAND_REAL_ORDERS`;
- `scripts/trident_server.sh` autorise Pod B seulement via
  `hip4-outcome-dry-run` force en `paper`;
- preflight Docker obligatoire avant `start/update/restart --mode live`.

## Préflight live requis

### Infra

- version serveur égale au commit attendu;
- `TRIDENT_CONFIG_PATH` attendu;
- `TRIDENT_MODE=live` uniquement au moment du vrai canary;
- Pod B HIP-4 actif seulement en mainnet paper;
- Pod C activé comme Pod A si la commande ne passe pas `--without-pod-c`;
- snapshots frais;
- funding/OI frais si utilisés par les gates;
- pas de 429 sur 24h;
- reconnect WS explicables et resync effectués.

### Exchange privé

- `clearinghouseState` OK;
- `spotClearinghouseState` OK si equity/spot utilisé;
- `openOrders` OK;
- `frontendOpenOrders` OK;
- `userFillsByTime` OK;
- `orderUpdates` WS connecté;
- test parse des retours order/cancel/trigger;
- nonce/signature/subaccount validés en environnement contrôlé.

### Reconciliation startup

Le démarrage doit produire un rapport bloquant:

- positions exchange;
- positions journal;
- écarts positions exchange vs journal;
- open orders reconnus/non reconnus;
- triggers reconnus/non reconnus;
- fills non ingérés depuis dernier journal;
- equity/capital/PnL retenus;
- décisions: recover, quarantine, close-only ou ready.

Tant que ce rapport n'est pas `ready`, le moteur de signaux ne doit pas pouvoir
envoyer d'ordre d'ouverture.

### Execution

- market/limit open testés en dry-run signé ou environnement minimal;
- close reduce-only testé;
- partial fill testé;
- cancel testé;
- cancel+replace SL testé;
- trigger TP/SL testé;
- erreur `err` volontaire testée;
- ordre rejeté ne modifie pas l'état portefeuille;
- ordre accepté mais fill absent reste `pending` et bloque les doublons.

### Observabilité

- dashboard affiche mode réel;
- dashboard distingue dry-run PnL et exchange PnL;
- alertes pour WS down, API down, 429, fill drift, orphan orders, equity drift,
  stale snapshots, kill-switch actif;
- journal parseable après restart;
- kill-switch testé;
- rollback documenté.

Commande preflight seule:

```bash
python -m app.live.preflight --config config/trident.toml --pod pod_a
python -m app.live.preflight --config config/trident.toml --pod pod_c
```

Commande canary live, après validation du preflight:

```bash
./deploy.sh --start --mode live --without-funding
```

## Plan de préparation recommandé

1. Relire le dernier fetch dry-run et confirmer qu'il n'y a pas de 429/breaker
   ouvert.
2. Déployer la version live-ready en dry-run si une validation finale est
   souhaitée.
3. Configurer `.env.trident` côté serveur avec l'API wallet.
4. Exécuter le preflight live sans démarrer les pods live.
5. Confirmer que le compte/subaccount ne contient aucune position inconnue.
6. Confirmer que `orderUpdates` est sain.
7. Revoir les journaux dry-run après redeploy.
8. Canary live: Pod A + Pod C en vrais ordres, Pod B HIP-4 en paper, capital
   isolé/minimal, symboles limités, max notional très bas, close-only/kill-switch validés.
9. Revoir les fills réels et les écarts exchange/journal.
10. Elargir seulement après plusieurs sessions propres.

## Critère go/no-go

Go live uniquement si:

- reconciliation startup exchange passe sans inconnue;
- websocket utilisateur est connecté et testé;
- aucun ordre ouvert/trigger inconnu n'existe sur le compte/subaccount;
- les positions exchange et journal sont cohérentes;
- equity exchange est cohérente avec le dashboard;
- ordre open, close reduce-only, cancel, trigger, cancel+replace et partial fill
  sont testés;
- `deploy.sh --start --mode live --without-funding` passe le
  preflight sans override dangereux.

Avant ça, le mode opérable reste le **dry-run préparatoire**.
