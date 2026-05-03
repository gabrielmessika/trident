# TRIDENT Active Plan

Date: `2026-05-03`

## Status

- `ACTIVE_SINGLE_SOURCE_OF_TRUTH`
- Ce fichier est la feuille de route courante. Les autres documents sont des archives, des notes de recherche, ou des details d'implementation.
- En cas de contradiction avec un ancien doc, ce fichier gagne.
- Objectif actuel: exploiter Pod B HIP-4 Outcome comme remplacement complet du Pod B historique, sur compte Hyperliquid testnet dedie, avec Pod A / Pod C stables et sans exposition mainnet.

## Lecture Rapide

- Prod/dry-run principal: `config/trident.toml`.
- Pods actifs dry-run: `Pod A` crypto core, `Pod B HIP-4 Outcome`, `Pod C` tradfi.
- Pod B historique directionnel: legacy / non demarre par defaut.
- Nouveau Pod B: `HIP4OutcomeEdgePod`, branche HIP-4 outcome sur testnet. Le repo reste safe par defaut en paper; le serveur peut l'activer en vrais ordres testnet via env dedie.
- UI:
  - dashboard principal: `/dashboard`
  - monitoring HIP-4: `/hip4-outcome`
  - API HIP-4: `/api/hip4-outcome`
- Regle de promotion: aucune logique HIP-4 ne passe en mainnet sans dataset complet, calibration, replay comparable, dry-run propre et testnet concluant sur plusieurs expiries.

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

Statut: implemente, integre comme remplacement complet du Pod B, repo safe par defaut en paper, serveur exploitable en testnet avec compte dedie.

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

Defaults repo:

- `mode = "paper"`
- `allow_testnet_orders = false`
- `require_testnet_url = true`
- `pod_b_budget_usdc = 500`
- `max_position_usdc = 50`
- `max_total_outcome_exposure_usdc = 500`
- `max_per_underlying_outcome_exposure_usdc = 150`
- `max_outcome_markets_open = 3`
- `enforce_testnet_balance_check = true`
- `testnet_balance_coin = "USDH"`
- `testnet_balance_buffer_usdc = 1`
- Les noms historiques `*_usdc` representent le budget notionnel; sur HIP-4 testnet le coin quote attendu est `USDH`.

Activation serveur testnet:

- `HIP4_OUTCOME_MODE=testnet`
- `HIP4_OUTCOME_ALLOW_TESTNET_ORDERS=true`
- `HIP4_OUTCOME_ACCOUNT_ADDRESS=<wallet testnet dedie>`
- `HIP4_OUTCOME_SECRET_KEY=<api wallet testnet dedie>`
- `HIP4_OUTCOME_VAULT_ADDRESS=` vide sauf sous-compte explicite.
- Le lancement complet se fait sans `--without-pod-b`; `--without-pod-b` coupe aussi le nouveau Pod B.
- Le Pod B utilise le spot outcome quote `USDH`. Avoir seulement du `USDC` spot ne suffit pas.

Sources de prix / observation:

- Le moteur sait lire Binance, OKX, Bybit, Coinbase, Kraken et Hyperliquid.
- La config active interroge les venues externes et Hyperliquid, mais ancre la reference a Hyperliquid testnet pour eviter les faux edges quand le testnet diverge du marche mainnet.
- Les sources externes sont conservees comme observation et garde-fou: si elles divergent trop de la reference Hyperliquid, le signal est rejete ou degrade au lieu d'etre pris comme edge exploitable.
- `max_source_deviation_bps = 50`, `min_reference_sources = 1`, `anchor_reference_to_hyperliquid = true`.
- `include_underlyings = []` signifie: accepter tous les `priceBinary` renvoyes par `outcomeMeta`.
- Au preflight courant, Hyperliquid testnet renvoie des marches supportes BTC/HYPE; si SOL/ETH/etc. apparaissent dans `outcomeMeta`, le pod les prendra sans changement de code.
- Raison: les prix testnet peuvent diverger fortement des venues externes; utiliser Binance/OKX/etc. comme verite unique sur testnet peut creer de faux edges.

Capital guard:

- Avant tout fill paper/testnet, `OutcomeCapitalGuard` plafonne la taille par budget Pod B et exposition ouverte.
- En `testnet`, il verifie aussi le solde spot quote outcome disponible via `spotClearinghouseState` avant d'envoyer un ordre.
- Sur le testnet courant, les outcomes demandent `USDH`: du `USDC` spot seul ne suffit pas, il faut convertir ou deposer du `USDH`.
- Le statut expose `capital` dans `logs/hip4_outcome_status.json` et dans l'alias `logs/pod_b_live_status.json`.
- Le minimum ordre HL est traite comme `10 USDH` de valeur economique effective, avec `min(limit_price, 1 - limit_price)` pour les outcomes. Les rejets explicites sont `below_exchange_min_order_value_yes/no`.

Frais / PnL:

- Ouverture outcome: `outcome_open_fee_rate = 0.0`.
- Settlement/close outcome: `outcome_settlement_fee_rate = 0.002`.
- Le paper et le testnet estiment le PnL net apres frais de settlement.
- Le statut global et la page HIP-4 doivent lire la meme source d'agregation par coin, pour eviter un PnL Pod B visible sur `/dashboard` mais absent de `/hip4-outcome`.

Isolation Pod A / Pod B:

- Pod B HIP-4 utilise un compte Hyperliquid testnet dedie et un budget USDH dedie.
- Pod A et Pod B ne partagent donc plus ni capital, ni marge, ni budget de risque.
- Un perp Pod A BTC/HYPE/etc. ne bloque plus un outcome HIP-4 sur le meme underlying.
- Les locks atomiques `runtime/hip4_overlap_locks/` ne sont plus utilises par Pod A ou Pod B.
- Les modules d'overlap/lock HIP-4 ont ete supprimes; il ne doit plus rester de cle `directional_overlap`, `hip4_overlap` ou `block_directional_overlap` dans les statuts/UI.
- Les garde-fous conserves sont internes au Pod B: budget, exposition max, `market_already_open`, minimum d'ordre HL, reconciliation/fills/settlement.
- L'UI HIP-4 affiche le budget, le solde testnet disponible, les positions et les executions, sans carte d'overlap Pod A.

Edge types implementes:

- `MODEL`: proba lognormal static-vol vs prix YES/NO.
- `LATE_EXPIRY`: sous-jacent deja clairement au-dessus/sous le strike proche expiry.
- `PARITY`: achat YES+NO si le cout combine est sous 1.
- `SHORT_EXPIRY`: chemin OpenClaw-like pour marches tres courts.

Mode `SHORT_EXPIRY`:

- Priorise les marches dans `short_expiry_window_minutes`.
- Maintient un historique prix settlement-aligne dans le `state_path` configure.
- Calcule momentum 30s/60s/180s, distance au strike, vitesse, vol realisee courte.
- Combine:
  - distance au strike
  - momentum court terme
  - probabilite implicite du book YES/NO
  - imbalance du book
  - modele statique
- Loggue tous les snapshots, y compris warming/rejected, dans `short_expiry_features.csv`.

Sorties principales:

- `logs/hip4_outcome_testnet/opportunities.csv`
- `logs/hip4_outcome_testnet/decisions.jsonl`
- `logs/hip4_outcome_testnet/trades.csv`
- `logs/hip4_outcome_testnet/settlements.csv`
- `logs/hip4_outcome_testnet/latency_stats.csv`
- `logs/hip4_outcome_testnet/edge_decay.csv`
- `logs/hip4_outcome_testnet/short_expiry_features.csv`
- `logs/hip4_outcome_testnet/daily_summary.csv`
- `logs/hip4_outcome_status.json`
- `logs/pod_b_live_status.json` (alias runtime Pod B pour l'UI/reporting)
- `runtime/hip4_outcome_testnet_state.json`

Note: les anciens chemins `logs/hip4_outcome_paper/` et `runtime/hip4_outcome_paper_state.json` peuvent exister dans les archives locales; l'exploitation courante doit privilegier le `logs_dir` et `state_path` de `config/hip4_outcome_testnet.toml`.

Etat d'observation et execution:

- Un signal BTC `MODEL` propre a ete observe en paper autour de `net_edge ~0.36-0.38`.
- Un signal HYPE `SHORT_EXPIRY` a ete observe autour de `best_net_edge ~0.152`.
- Premier ordre testnet valide le `2026-05-03`: `HYPE_GT_58.5_20260503_0800`, `BUY_YES`, `38` tokens a `0.71`, cout `26.98 USDH`, oid `52407686267`.
- Settlement estime: position fermee, PnL net estime autour de `+10.94 USDH`.
- Les premiers gros edges HYPE vus avec reference externe ne doivent pas etre consideres comme edge mainnet fiable: le prix HYPE testnet Hyperliquid divergeait fortement des venues externes.
- Conclusion courante: l'execution testnet, la reconciliation et le PnL net fonctionnent; l'existence d'un edge durable reste a prouver sur plusieurs marches/expiries.

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
uv run python -m app.live.hip4_outcome_runner \
  --config config/hip4_outcome_testnet.toml \
  --mode paper
```

```bash
uv run python -m app.live.hip4_outcome_runner \
  --config config/hip4_outcome_testnet.toml \
  --mode paper \
  --once
```

```bash
uv run python -m app.backtest.hip4_outcome_replay \
  --logs-dir logs/hip4_outcome_testnet \
  --output logs/hip4_outcome_testnet/replay_latest.json
```

Pour de vrais ordres testnet:

- Fournir `HIP4_OUTCOME_SECRET_KEY` pour l'API wallet dedie Pod B.
- Fournir `HIP4_OUTCOME_ACCOUNT_ADDRESS` pour le wallet testnet finance.
- Passer `HIP4_OUTCOME_MODE=testnet`.
- Activer explicitement `HIP4_OUTCOME_ALLOW_TESTNET_ORDERS=true`.
- Lancer d'abord `--preflight`.
- Verifier que le solde `USDH` est visible via `spotClearinghouseState`.
- Garder les caps internes actifs meme si le compte testnet contient plus de capital.

Ce qui manque encore pour se rapprocher d'un bot type OpenClaw performant:

- WebSocket ou streaming plus bas-latence pour mids et order books.
- Statistiques par expiry apres settlement reel/estime.
- Mesure slippage/fill testnet avec tres petites tailles.
- Plus de donnees sur marches 5m/15m avant calibration.
- Une politique de sizing dynamique uniquement apres preuve paper/testnet.
- Une calibration probabiliste serieuse avant tout Kelly ou ML.
- Une base de snapshots complete pour rejouer decisions, books, references, fills, settlements et edge decay.

## Idees A Garder: Bot Prediction Market / Post Crypto_Jargon

Verdict:

- Le post "prediction market trading bot" n'est pas une preuve d'edge exploitable. Il melange des briques reelles de trading systematique avec des promesses non verifiees (`68.4% win rate`, `$300-$1,500/day`).
- La partie "Anthropic dropped" doit etre consideree comme marketing ou interpretation: la doc Anthropic consultee est un guide general de Skills Claude, pas une strategie officielle de trading.
- Les reponses indexees autour du post sont surtout sceptiques: edge qui disparait si tout le monde copie, manque de details sur fees/slippage/fills, dependance API/latence.

Ce que Pod B fait deja dans cet esprit:

- Scan de marches HIP-4 outcome.
- Estimation `p_model - p_market` et edge net frais/slippage.
- Garde-fous de budget, exposition, profondeur, spread, minimum ordre HL, reconciliation/fills/settlement.
- Logs decisionnels et UI pour analyser par coin, type d'edge, PnL, fees et settlements.

Ce que Pod B ne fait pas encore:

- Agent swarm Twitter/Reddit/RSS.
- Modele ML type XGBoost entraine sur historique de settlements.
- Fractional Kelly base sur probabilites calibrees.
- Auto-hedge cross-venue ou execution multi-CLOB.
- Auto-learning qui modifie la strategie tout seul.

Backlog utile, ordre recommande:

1. Dataset complet et replayable: snapshots book, reference prices, decisions, fills, fees, settlements, latence, edge decay.
2. Calibration: Brier score, log-loss, courbes de calibration, walk-forward par date, underlying, expiry horizon et type d'edge.
3. Sizing: fractional Kelly seulement apres calibration; garder hard caps par trade, coin, expiry, jour et drawdown.
4. Modele simple: logistic regression ou XGBoost seulement apres assez de settlements; baseline heuristique Pod B doit rester benchmark.
5. Loss review: classifier les pertes en stale price, spread, fake testnet divergence, late expiry reversal, insufficient depth, model overconfidence.
6. Sentiment/news/LLM: a garder pour plus tard, surtout pour marches narratifs ou macro; priorite faible pour HIP-4 crypto 5m/15m ou la latence et la microstructure dominent.
7. Cross-venue/parity: interessant plus tard si on peut mesurer fills, slippage, inventory risk et settlement mismatch.

Regle:

- Ne pas implementer Kelly/ML/agents tant que Pod B n'a pas accumule un historique testnet/paper propre avec settlements exploitables.

## Validations Recentes

Validation code HIP-4 et integration UI/dry-run:

```bash
uv run python -m py_compile app/trident/hip4_outcome/models.py app/trident/hip4_outcome/state.py app/trident/hip4_outcome/runner.py app/live/pod_a_live_runner.py app/observability/api.py
```

```bash
bash -n deploy.sh scripts/trident_server.sh scripts/trident_dry_run_review.sh scripts/fetch_trident_data.sh
```

```bash
uv run python -m unittest tests.test_hip4_outcome_pod tests.test_pod_a_live_runner
```

Resultat courant `2026-05-03`:

- `uv run python -m unittest tests.test_hip4_outcome_pod`: `35` tests OK.
- `uv run python -m unittest tests.test_pod_a_live_runner`: `3` tests OK.
- `uv run python -m py_compile app/trident/hip4_outcome/models.py app/trident/hip4_outcome/state.py app/trident/hip4_outcome/runner.py app/live/pod_a_live_runner.py app/observability/api.py`: OK.
- Serveur verifie apres redeploiement: `trident-pod-a-live`, `trident-pod-c-live`, `trident-hip4-outcome-dry-run`, `trident-api` et collectors actifs.
- API `/api/hip4-outcome` OK, statut Pod B HIP-4 frais, pas de cle d'overlap fantome.
- Fix important: `OutcomePosition.from_dict` et le state reload HIP-4 sont couverts par test de round-trip.

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

### 1. Operer Pod B HIP-4 En Testnet Dedie

- Lancer le bot complet sans `--without-pod-b`.
- Verifier que le service actif est `hip4-outcome-dry-run`, pas `pod-b-live`.
- Verifier que l'env serveur active bien `HIP4_OUTCOME_MODE=testnet` et `HIP4_OUTCOME_ALLOW_TESTNET_ORDERS=true` seulement pour le compte testnet dedie.
- Verifier que le capital visible est en `USDH`, pas seulement en `USDC`.
- Suivre `/hip4-outcome`:
  - edges par type
  - short-expiry features
  - edge decay
  - settlements estimes
  - PnL paper par underlying
  - PnL testnet net fees par underlying
- Suivre aussi `/dashboard` et `/api/report`: Pod B doit pointer vers `pod_kind = hip4_outcome_edge_pod`.
- Ne pas conclure sur un seul signal; attendre plusieurs expiries.

### 2. Analyser Les Runs Pod B

Prerequis:

- fetch serveur complet apres quelques heures.
- logs `opportunities`, `decisions`, `trades`, `settlements`, `edge_decay`, `short_expiry_features`, `daily_summary`.
- statut API et UI coherents.

Action:

- separer les vrais edges des artefacts testnet: divergence HYPE/BTC, book stale, absence de profondeur, settlement mismatch.
- calculer PnL net fees par coin, cote, type d'edge, horizon d'expiry et heure.
- mesurer win rate, profit factor, drawdown, edge decay et fill quality. Le win rate seul ne suffit pas.
- comparer paper vs testnet quand les deux sources existent.

### 3. Calibration Avant Sizing Dynamique

- Ajouter Brier score, log-loss et calibration buckets par type d'edge.
- Faire du walk-forward par jour/expiry plutot que valider sur une seule fenetre.
- N'autoriser fractional Kelly ou XGBoost qu'apres historique suffisant et stable.
- Garder `max_position_usdc`, `max_total_outcome_exposure_usdc` et `max_per_underlying_outcome_exposure_usdc` comme hard caps meme si Kelly propose plus.

### 4. Ameliorer La Latence HIP-4 Seulement Si Necessaire

Priorite apres plusieurs runs testnet:

- remplacer le polling critique par streaming/WS si l'edge decay montre que les signaux disparaissent trop vite.
- ajouter book cache / allMids cache pour eviter de dependre de REST a chaque boucle.
- mesurer la latence dans `latency_stats.csv` avant d'optimiser.

### 5. Garder Pod A / Pod C Stables

- Pas de nouveau sweep massif tant que le Pod B HIP-4 est en exploration testnet.
- Rejouer la baseline officielle seulement quand le fetch serveur change ou avant une promotion.
- Toute divergence live/replay doit etre analysee avec `collector + maintenance_refresh`.

### 6. Deploiement / Rollback

- S'assurer que le serveur utilise `config/trident.toml`.
- Verifier que le dry-run lance bien HIP-4 comme Pod B, et que les seuls ordres reels possibles sont testnet sur compte dedie.
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
