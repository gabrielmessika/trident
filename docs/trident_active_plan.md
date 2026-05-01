# TRIDENT Active Plan

Date: `2026-05-01`

## Status

- `ACTIVE_SINGLE_SOURCE_OF_TRUTH`
- Ce fichier est la feuille de route courante. Les autres documents sont des archives, des notes de recherche, ou des details d'implementation.
- En cas de contradiction avec un ancien doc, ce fichier gagne.
- Objectif actuel: remplacer le Pod B directionnel historique par le Pod B HIP-4 Outcome en dry-run, tout en gardant Pod A / Pod C stables.

## Lecture Rapide

- Prod/dry-run principal: `config/trident.toml`.
- Pods actifs dry-run: `Pod A` crypto core, `Pod B HIP-4 Outcome`, `Pod C` tradfi.
- Pod B historique directionnel: legacy / non demarre par defaut.
- Nouveau Pod B: `HIP4OutcomeEdgePod`, branche HIP-4 outcome sur testnet, paper par defaut, vrais ordres testnet possibles mais verrouilles.
- UI:
  - dashboard principal: `/dashboard`
  - monitoring HIP-4: `/hip4-outcome`
  - API HIP-4: `/api/hip4-outcome`
- Regle de promotion: aucune logique ne passe en mainnet sans replay comparable, dry-run propre, puis testnet taille minimale quand c'est applicable.

## Reference Prod Courante

Config canonique:

- `config/trident.toml`

Backtest officiel de reference encore valide:

- `server-data/replay_reports/official_baseline_current_cli_20260423.md`
- `server-data/replay_reports/official_baseline_current_cli_20260423.json`

Resultat de reference:

| Total | Pod A | Pod B | Pod C |
|---:|---:|---:|---:|
| `+562.48 USD` | `+526.26` | `0.00` | `+36.22` |

Notes importantes:

- L'input officiel couvre `2026-04-05 -> 2026-04-23`.
- L'input courant saute `2026-04-19`.
- Les replays de parite doivent inclure `collector + maintenance_refresh`; le collector-only n'est pas suffisant.
- Les caps de levier crypto live manquants ont ete ajoutes dans `config/trident.toml`.

## Etat Des Pods

### Pod A - Crypto Core

Statut: actif, reference principale crypto.

Promu dans le profil repo:

- `pod_a.stop_grace_minutes = 165`, scope utile: `trend_pullback_long`.
- `pod_a.opposite_signal_debounce_minutes = 15`.
- Vetoes MTF Pod A valides le `2026-04-27`.
- Veto BTC overextension 4h, scope BTC long.
- Veto XRP overextension 4h, scope XRP long.
- Veto HYPE `trend_pullback_long` en observation dry-run.

Principes:

- Ne pas reactiver les shorts Pod A globalement.
- Ne pas relacher globalement `RangeAuction` ou `DeadZone`.
- Ne pas promouvoir `stop_grace_210m` sans validation hors echantillon.
- Toute nouvelle regle Pod A doit battre la baseline full-bot, pas seulement un test isole.

### Pod C - Tradfi

Statut: actif, quasi stabilise.

Promu dans le profil repo:

- `routing_revoke_grace_minutes_by_symbol`:
  - `XYZ:SP500 = 540`
  - `XYZ:XYZ100 = 540`
- Veto `silver_strong_extension_veto`.

Principes:

- Ne pas etendre la grace `routing_revoked` a `silver` ou `gold` sans nouvelle preuve.
- Ne pas relacher globalement les `stop_hit` Pod C.
- Garder `equity` et `fx` en observation, pas en nouvelle branche active.

### Pod B Directionnel Historique

Statut: remplace / legacy.

Conclusion courante:

- Les variantes Pod B testees n'ont pas ete portefeuille-additives sur les snapshots comparables.
- Le silence de Pod B venait en partie d'une incoherence allocation/regime, mais la correction brute cannibalisait Pod A.
- Le service Docker historique reste disponible seulement sous profil `legacy_pod_b`.
- Il ne doit pas redevenir actif sans nouvelle validation full-bot.

## Pod B HIP-4 Outcome

Statut: implemente, integre comme remplacement complet du Pod B en dry-run, paper par defaut.

Fichiers principaux:

- `app/trident/hip4_outcome/`
- `app/live/hip4_outcome_runner.py`
- `app/backtest/hip4_outcome_replay.py`
- `config/hip4_outcome_testnet.toml`
- `tests/test_hip4_outcome_pod.py`

Integration bot complet:

- `app/live/trident_dry_run_launcher.py` lance HIP-4 comme resultat `pod_b`; l'ancien runner directionnel n'est plus lance.
- `scripts/trident_server.sh` mappe le profil `pod_b` vers le service `hip4-outcome-dry-run`.
- `docker-compose.trident.yml` met `hip4-outcome-dry-run` sous profils `pod_b` et `hip4`; l'ancien `pod-b-live` est sous `legacy_pod_b`.
- HIP-4 ecrit aussi `logs/pod_b_live_status.json`, ce qui rend le reporting/UI Pod B compatible avec le nouveau pod.
- Le pod ne modifie pas le routing Pod A/Pod C et n'envoie aucun ordre mainnet.

Modes:

| Mode | Effet |
|---|---|
| `observer` | lit les marches, calcule les signaux, loggue, aucun fill |
| `paper` | simule les fills au visible ask, estime le settlement |
| `testnet` | peut envoyer de vrais ordres testnet IOC si credentials et garde-fous sont actifs |

Mode par defaut actuel:

- `mode = "paper"`
- `allow_testnet_orders = false`
- `require_testnet_url = true`
- `pod_b_budget_usdc = 25`
- `max_position_usdc = 5`
- `max_total_outcome_exposure_usdc = 25`
- `max_per_underlying_outcome_exposure_usdc = 10`
- `max_outcome_markets_open = 3`
- `enforce_testnet_balance_check = true`
- `testnet_balance_buffer_usdc = 1`

Sources de prix:

- Le moteur sait lire Binance, OKX, Bybit, Coinbase, Kraken et Hyperliquid.
- La config testnet active utilise Hyperliquid `allMids` pour toutes les references, afin de rester settlement-alignee.
- `include_underlyings = []` signifie: accepter tous les `priceBinary` renvoyes par `outcomeMeta`.
- Au preflight courant, Hyperliquid testnet renvoie des marches supportes BTC/HYPE; si SOL/ETH/etc. apparaissent dans `outcomeMeta`, le pod les prendra sans changement de code.
- Raison: les prix testnet peuvent diverger fortement des venues externes; utiliser Binance/OKX/etc. sur testnet peut creer de faux edges.

Capital guard:

- Avant tout fill paper/testnet, `OutcomeCapitalGuard` plafonne la taille par budget Pod B et exposition ouverte.
- En `testnet`, il verifie aussi le solde spot `USDC` disponible via `spotClearinghouseState` avant d'envoyer un ordre.
- Le statut expose `capital` dans `logs/hip4_outcome_status.json` et dans l'alias `logs/pod_b_live_status.json`.

Overlap Pod A / Pod B:

- Hyperliquid represente les perps et les outcomes comme des assets differents; techniquement un compte peut donc avoir un perp BTC et un outcome BTC.
- Pour eviter une double exposition TRIDENT, `block_directional_overlap = true`.
- Pod B HIP-4 refuse un market BTC/HYPE/etc. si `logs/pod_a_live_status.json` montre deja une position ouverte sur le meme underlying.
- Pod A lit l'alias `logs/pod_b_live_status.json` et retire de ses nouvelles entrees tout symbole qui a deja une position HIP-4 ouverte.
- Les deux pods utilisent aussi un lock atomique par underlying dans `runtime/hip4_overlap_locks/` pour reduire le risque de double entree simultanee entre deux boucles.
- L'UI HIP-4 affiche maintenant le budget, le solde testnet disponible, et le nombre d'underlyings bloques par overlap.

Edge types implementes:

- `MODEL`: proba lognormal static-vol vs prix YES/NO.
- `LATE_EXPIRY`: sous-jacent deja clairement au-dessus/sous le strike proche expiry.
- `PARITY`: achat YES+NO si le cout combine est sous 1.
- `SHORT_EXPIRY`: chemin OpenClaw-like pour marches tres courts.

Mode `SHORT_EXPIRY`:

- Priorise les marches dans `short_expiry_window_minutes`.
- Maintient un historique prix settlement-aligne dans `runtime/hip4_outcome_paper_state.json`.
- Calcule momentum 30s/60s/180s, distance au strike, vitesse, vol realisee courte.
- Combine:
  - distance au strike
  - momentum court terme
  - probabilite implicite du book YES/NO
  - imbalance du book
  - modele statique
- Loggue tous les snapshots, y compris warming/rejected, dans `short_expiry_features.csv`.

Sorties principales:

- `logs/hip4_outcome_paper/opportunities.csv`
- `logs/hip4_outcome_paper/decisions.jsonl`
- `logs/hip4_outcome_paper/trades.csv`
- `logs/hip4_outcome_paper/settlements.csv`
- `logs/hip4_outcome_paper/latency_stats.csv`
- `logs/hip4_outcome_paper/edge_decay.csv`
- `logs/hip4_outcome_paper/short_expiry_features.csv`
- `logs/hip4_outcome_paper/daily_summary.csv`
- `logs/hip4_outcome_status.json`
- `logs/pod_b_live_status.json` (alias runtime Pod B pour l'UI/reporting)
- `runtime/hip4_outcome_paper_state.json`

Etat d'observation initial `2026-05-01`:

- Un signal BTC `MODEL` propre a ete observe en paper autour de `net_edge ~0.36-0.38`.
- Un signal HYPE `SHORT_EXPIRY` a ete observe autour de `best_net_edge ~0.152`.
- Les premiers gros edges HYPE vus avec reference externe ne doivent pas etre consideres comme fiables; la reference a ete corrigee vers Hyperliquid testnet.
- Ces observations ne prouvent pas encore un edge exploitable: il faut plusieurs expiries, settlements estimes, et un passage testnet taille minimale avant toute conclusion mainnet.

Commandes utiles:

Deploiement dry-run complet avec le nouveau Pod B:

```bash
./deploy.sh --start --config config/trident.toml --fresh-start
```

Couper le nouveau Pod B HIP-4:

```bash
./deploy.sh --start --config config/trident.toml --without-pod-b
```

```bash
.venv/bin/python -m app.live.hip4_outcome_runner \
  --config config/hip4_outcome_testnet.toml \
  --mode paper
```

```bash
.venv/bin/python -m app.live.hip4_outcome_runner \
  --config config/hip4_outcome_testnet.toml \
  --mode paper \
  --once
```

```bash
.venv/bin/python -m app.backtest.hip4_outcome_replay \
  --logs-dir logs/hip4_outcome_paper \
  --output logs/hip4_outcome_paper/replay_latest.json
```

Pour de vrais ordres testnet:

- Fournir `TRIDENT_SECRET_KEY` ou `HYPERLIQUID_SECRET_KEY`.
- Passer `mode = "testnet"`.
- Activer explicitement `allow_testnet_orders = true`.
- Lancer d'abord `--preflight`.
- Garder les caps actuels minuscules.

Ce qui manque encore pour se rapprocher d'un bot type OpenClaw performant:

- WebSocket ou streaming plus bas-latence pour mids et order books.
- Statistiques par expiry apres settlement reel/estime.
- Mesure slippage/fill testnet avec tres petites tailles.
- Plus de donnees sur marches 5m/15m avant calibration.
- Une politique de sizing dynamique uniquement apres preuve paper/testnet.

## Validations Recentes

Validation code HIP-4 et integration UI/dry-run:

```bash
.venv/bin/python -m compileall -q app/trident/hip4_outcome app/live/hip4_outcome_runner.py app/live/trident_dry_run_launcher.py app/observability/api.py app/backtest/hip4_outcome_replay.py
```

```bash
bash -n deploy.sh scripts/trident_server.sh scripts/trident_dry_run_review.sh scripts/fetch_trident_data.sh
```

```bash
.venv/bin/python -m unittest tests.test_hip4_outcome_pod tests.test_trident_dry_run_launcher tests.test_health tests.test_reporting -v
```

Resultat courant:

- `47` tests OK sur le paquet cible.
- Preflight paper OK contre l'API Hyperliquid testnet, avec marches BTC/HYPE et references disponibles.
- API `/api/hip4-outcome` OK avec status frais.

Limite d'environnement:

- Docker n'est pas disponible dans ce workspace; `docker compose config` n'a pas ete valide localement.

## Decisions Nettoyees

Ces pistes ne doivent plus apparaitre comme roadmap active. Elles restent seulement historiques si on relit les anciens rapports.

### Rejete / Non Promu

- Pod B Hyperps dynamique:
  - infra utile, mais pas de promotion.
  - raison: univers live courant insuffisant et TAO ne doit pas remplacer un Hyperp actif.
- Sleeve special symbols `TAO/XPL/BIO/PENGU`:
  - pas assez de couverture comparable, pas portefeuille-additif.
  - TAO reste bloque tradable.
- Crypto Regime V2 / `hybrid_moderate_a`:
  - interessant en shadow, pas promu.
  - trop de churn et faux positifs sur la fenetre recente.
- Pod B microstructure directionnel:
  - watchers utiles en research/watch-only.
  - aucune activation Pod B actuelle ne bat la baseline full-bot.
- Squeeze / breakout via Pod B:
  - tests cibles a `0` trade ou non additifs.
- Shorts globaux Pod A:
  - rejetes.
  - toute these short future doit etre redefinie coin par coin et separee du moteur long actuel.
- Funding / liq / open interest comme pod principal:
  - pas de preuve replay comparable suffisante.
- Mean reversion generaliste:
  - recherche seulement, pas de pod live.
- Pod C `silver routing grace`, `gold routing grace`, equity/fx:
  - pas de promotion avec les donnees actuelles.

### Garde En Watch / Research Seulement

- Microstructure `depth_refill_continuation` et `liquidity_pull_continuation`: watchers/research, pas execution.
- `funding_reversion`, `range_mean_reversion`, `stoch_cci_reversion`: hypotheses research.
- LTC/ZRO overextension: aucun veto cible declenche en replay courant.
- `absorption` et `book_churn_flow_veto`: a reformuler avant promotion.

## Roadmap Courante

### 1. Lancer Pod B HIP-4 En Dry-Run

- Lancer le bot complet sans `--without-pod-b`.
- Verifier que le service actif est `hip4-outcome-dry-run`, pas `pod-b-live`.
- Garder le mode par defaut `paper` tant que les premiers settlements ne sont pas coherents.
- Suivre `/hip4-outcome`:
  - edges par type
  - short-expiry features
  - edge decay
  - settlements estimes
  - PnL paper par underlying
- Suivre aussi `/dashboard` et `/api/report`: Pod B doit pointer vers `pod_kind = hip4_outcome_edge_pod`.
- Ne pas conclure sur un seul signal; attendre plusieurs expiries.

### 2. Passer HIP-4 En Testnet Taille Minimale Si Le Paper Tient

Prerequis:

- paper positif ou au moins coherent sur plusieurs expiries.
- pas de faux edge lie a la reference settlement.
- preflight testnet OK.
- credentials testnet disponibles.

Action:

- activer `mode = "testnet"` et `allow_testnet_orders = true` explicitement.
- garder `max_position_usdc = 5` ou moins.
- verifier reconciliation `spotClearinghouseState` + `userFillsByTime`.

### 3. Ameliorer La Latence HIP-4 Seulement Si Necessaire

Priorite apres paper/testnet:

- remplacer le polling critique par streaming/WS si l'edge decay montre que les signaux disparaissent trop vite.
- ajouter book cache / allMids cache pour eviter de dependre de REST a chaque boucle.
- mesurer la latence dans `latency_stats.csv` avant d'optimiser.

### 4. Garder Pod A / Pod C Stables

- Pas de nouveau sweep massif tant que le Pod B HIP-4 est en dry-run exploratoire.
- Rejouer la baseline officielle seulement quand le fetch serveur change ou avant une promotion.
- Toute divergence live/replay doit etre analysee avec `collector + maintenance_refresh`.

### 5. Deploiement / Rollback

- S'assurer que le serveur utilise `config/trident.toml`.
- Verifier que le dry-run lance bien HIP-4 paper comme Pod B, et que le live ne le lance pas en ordre reel.
- Garder un rollback simple:
  - couper le Pod B HIP-4 avec `--without-pod-b`
  - `--without-hip4-outcome` reste accepte comme alias historique
  - ou laisser `allow_testnet_orders = false`

## Regles De Promotion

- Une idee validee seulement en candles/research ne passe pas en prod.
- Une idee positive en standalone mais negative en full-bot ne passe pas en prod.
- Une idee HIP-4 paper ne passe pas en mainnet sans testnet tiny-order.
- Une nouvelle logique doit etre lisible dans l'UI et dans les logs avant toute activation durable.
- Les documents historiques peuvent expliquer une decision, mais ne rouvrent pas automatiquement une piste rejetee.

## Documents Historiques / Non Canoniques

Ces documents peuvent etre consultes pour le contexte, mais ne sont plus la roadmap active:

- `hip4.md`
- `docs/hip4_outcome_testnet.md`
- `docs/new_podB.md`
- `docs/crypto_refonte_plan_20260417.md`
- `docs/pod_c_vs_pod_a_transfer_20260418.md`
- `docs/pod_c_research_protocol.md`
- `docs/pod_liq_data_feasibility.md`
- `docs/trident_plan/spec.md`
- `docs/trident_plan/status.md`
- `docs/trident_plan/stages.md`
