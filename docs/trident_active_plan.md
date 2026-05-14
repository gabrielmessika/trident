# TRIDENT Active Plan

Date: `2026-05-13`

## Status

- `ACTIVE_SINGLE_SOURCE_OF_TRUTH`
- Ce fichier est la feuille de route courante. Les autres documents sont des archives, des notes de recherche, ou des details d'implementation.
- En cas de contradiction avec un ancien doc, ce fichier gagne.
- Objectif actuel: exploiter Pod A comme moteur PnL principal en dry-run mainnet,
  garder Pod C stable, et faire tourner Pod B HIP-4 Outcome comme pod independant
  sur compte/budget separe, sans cannibaliser Pod A.

## Lecture Rapide

- Prod/dry-run principal: `config/trident.toml`.
- Pods actifs dry-run: `Pod A` crypto core avec `a_grade_enabled`, `Pod B HIP-4 Outcome`, `Pod C` tradfi.
- Pod B historique directionnel: legacy / non demarre par defaut.
- Nouveau Pod B: `HIP4OutcomeEdgePod`, branche HIP-4 outcome sur testnet. Le repo reste safe par defaut en paper; le serveur peut l'activer en vrais ordres testnet via env dedie.
- UI:
  - dashboard principal: `/dashboard`
  - monitoring HIP-4: `/hip4-outcome`
  - API HIP-4: `/api/hip4-outcome`
- Pod B HIP-4 expose maintenant un `operator_brief` et une `short_expiry_watchlist`
  dans son status/API, afin de piloter explicitement les fenêtres proches expiry
  sans transformer le paper/testnet en claim de performance.
- `/api/hip4-outcome` expose aussi `blocked_opportunity_slices`, afin de verifier
  les guardrails testnet actifs sans ouvrir le fichier TOML serveur.
- Regle de promotion: aucune logique HIP-4 ne passe en mainnet sans dataset complet, calibration, replay comparable, dry-run propre et testnet concluant sur plusieurs expiries.
- Nouvelle piste active en shadow: `TriggerLiquidityOverlay` pour les perps
  Hyperliquid. Elle exploite les TP/SL visibles via node/order-status data
  comme couche de features et de risque pour `Pod A` / `Supervisor`.
  Elle ne concerne pas `Pod B`, qui reste le pod HIP-4 Outcome independant.

## Reference Prod Courante

Config canonique:

- `config/trident.toml`

Backtest officiel de reference courant:

- `server-data/replay_reports/official_baseline_current_cli_20260513.md`
- `server-data/replay_reports/official_baseline_current_cli_20260513.json`
- Statut des references:
  `server-data/replay_reports/BACKTEST_REFERENCE_STATUS_20260513.md`
- Comparaison experimentale ayant servi a promouvoir `evo11`:
  `server-data/replay_reports/pod_a_improvement_levers_20260513/comparison.md`
- Source de validation chemin production avant copie officielle:
  `server-data/replay_reports/pod_a_evo11_promoted_20260513.md`

Resultat de reference avant promotion `evo11`:

| Total | Pod A | Pod B | Pod C |
|---:|---:|---:|---:|
| `+669.69 USD` | `+590.58` | `0.00` | `+79.11` |

Resultat officiel courant avec `evo11_a_grade_boost_wider_exits`:

| Total | Pod A | Pod B | Pod C |
|---:|---:|---:|---:|
| `+859.83 USD` | `+780.72` | `0.00` | `+79.11` |

Notes importantes:

- L'input de reference couvre `2026-04-05T19:45:00Z -> 2026-05-13T07:56:49Z`.
- L'input courant saute plusieurs dates sans collecte locale (`2026-04-19`,
  `2026-04-28`, `2026-04-29`, `2026-05-09 -> 2026-05-11`).
- Les replays de parite doivent inclure `collector + maintenance_refresh`; le collector-only n'est pas suffisant.
- Les caps de levier crypto live manquants ont ete ajoutes dans `config/trident.toml`.
- Le full replay ne force-enable plus Pod B: Pod B HIP-4 est independant et ne
  doit plus retirer de symboles, budget ou marge a Pod A.
- L'univers crypto Pod A a ete elargi avec `STRK`, `ONDO`, `BIO`, `VVV`,
  `SAGA`, `JUP`, `PENGU`, `INJ`, `PENDLE`, `TIA`, `DYM`, `ICP`, `ATOM`.
  `WLFI` reste exclu. Ces nouveaux symbols ne sont pas dans le JSONL full-replay
  historique; leur validation PnL reste donc light/API HL puis dry-run live.
- Validation OOS Pod A / Pod C du `2026-05-05`:
  - rapport: `server-data/replay_reports/pod_a_c_shortlist_validation_20260505.md`
  - input: `server-data/replay_inputs/pod_a_c_shortlist_oos_20260430_20260505`
  - baseline OOS: total `+8.67`, Pod A `-11.50`, Pod C `+20.17`.

## Etat Des Pods

### Pod A - Crypto Core

Statut: actif, reference principale crypto.

Promu dans le profil repo:

- `pod_a.stop_grace_minutes = 165`, scope utile: `trend_pullback_long`.
- `pod_a.opposite_signal_debounce_minutes = 15`.
- `pod_a.a_grade_enabled = true`: boost selectif des entrees A-grade
  `trend_pullback_long` crypto, avec scaling `1.25x` / `1.40x` et exits plus
  larges (`break_even x1.20`, `trailing_activation x1.15`,
  `trailing_distance x1.35`). Backtest `2026-04-05 -> 2026-05-13`:
  `+190.14 USD` vs baseline corrigee.
- Vetoes MTF Pod A valides le `2026-04-27`, mais non confirmes sur l'OOS `2026-04-30 -> 2026-05-05` (`-4.16`, `5` vetoes). Statut: garder sous surveillance / candidat rollback, pas nouvelle extension.
- Veto BTC overextension 4h, scope BTC long.
- Veto XRP overextension 4h, scope XRP long.
- Veto HYPE `trend_pullback_long`: actif dans le profil courant, mais rejete sur l'OOS `2026-04-30 -> 2026-05-05` (`-12.03`, `3` trades HYPE vetoes qui auraient ete gagnants). Statut: observation / candidat rollback.
- Leviers testes mais non promus:
  - `evo1_adaptive_exit`: negatif, coupe trop vite la convexite.
  - `evo2_fee_aware_be`: legerement negatif dans la baseline corrigee.
  - `evo3_trend_health_sizing`: negatif, sous-size les winners.
  - `evo4_symbol_health`: negatif, throttle trop brutal.
  - `evo10_context_guardrail`: negatif (`-70.37 USD`), retire surtout des
    re-entries gagnantes BTC/SOL/NEAR.

Principes:

- Ne pas reactiver les shorts Pod A globalement.
- Ne pas relacher globalement `RangeAuction` ou `DeadZone`.
- Ne pas promouvoir `stop_grace_210m` sans validation hors echantillon.
- Toute nouvelle regle Pod A doit battre la baseline full-bot, pas seulement un test isole.
- Surveiller en dry-run l'impact `a_grade` sur `max_open_notional_usd`,
  `max_open_expected_loss_usd`, drawdown et fees; le levier augmente le PnL en
  backtest mais augmente aussi l'exposition brute.
- Les donnees TP/SL Hyperliquid doivent d'abord rester un overlay shadow:
  veto/size/boost seulement apres replay full-bot contre la baseline officielle.
  Aucun signal TP/SL ne doit ouvrir un trade Pod A par lui-meme.

### Pod C - Tradfi

Statut: actif, quasi stabilise.

Backtest `Pod C off` du `2026-05-13`:

- rapport: `server-data/replay_reports/no_pod_c_20260513.md`
- input identique a la baseline officielle `2026-04-05 -> 2026-05-13`.
- resultat: total `+780.72 USD`, Pod A `+780.72`, Pod B `0.00`,
  Pod C `0.00`.
- Pod A est strictement identique a la baseline officielle avec Pod C actif
  (`155` trades, `+780.72 USD`, memes fees/rejets/exposition/drawdown).
- Conclusion courante: Pod C ne bloque pas Pod A dans ce replay. Le couper
  retirerait seulement sa contribution positive `+79.11 USD`; ne pas desactiver
  tant qu'un conflit live explicite de marge/routing n'est pas observe.

Promu dans le profil repo:

- `routing_revoke_grace_minutes_by_symbol`:
  - `XYZ:SP500 = 540`
  - `XYZ:XYZ100 = 540`
- Veto `silver_strong_extension_veto`, historiquement promu mais non confirme sur l'OOS `2026-04-30 -> 2026-05-05` (`-2.56`, `1` veto). Statut: observation / candidat rollback.

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
- `config/hip4_outcome_mainnet_observer.toml`
- `tests/test_hip4_outcome_pod.py`

Integration bot complet:

- `app/live/trident_dry_run_launcher.py` lance HIP-4 comme resultat `pod_b`; l'ancien runner directionnel n'est plus lance.
- `scripts/trident_server.sh` mappe le profil `pod_b` vers le service `hip4-outcome-dry-run`; l'observation mainnet tourne maintenant comme sidecar thread dans ce meme process.
- `docker-compose.trident.yml` lance `hip4-outcome-dry-run` pour Pod B; l'ancien `hip4-outcome-mainnet-observer` reste defini seulement comme service legacy/manual, et l'ancien `pod-b-live` reste sous `legacy_pod_b`.
- HIP-4 ecrit aussi `logs/pod_b_live_status.json`, ce qui rend le reporting/UI Pod B compatible avec le nouveau pod.
- Le pod ne modifie pas le routing Pod A/Pod C.
- Aucun ordre mainnet n'est possible dans l'etat courant: le mainnet ne tourne qu'en `observer`, sans credentials, sans alias Pod B, sans execution.

Modes:

| Mode | Effet |
|---|---|
| `observer` | lit les marches, calcule les signaux, loggue, aucun fill |
| `paper` | simule les fills au visible ask, estime le settlement |
| `testnet` | peut envoyer de vrais ordres testnet IOC si credentials et garde-fous sont actifs; le PnL settled vient des fills `Settlement` Hyperliquid |

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

Observation mainnet integree au process Pod B:

- Sidecar in-process: `HIP4OutcomeEdgePod` charge `config/hip4_outcome_mainnet_observer.toml` via `embedded_observer_config_paths`.
- Config: `config/hip4_outcome_mainnet_observer.toml`.
- Mode force: `observer`, charge sans overrides d'env testnet (`apply_env = false`).
- Endpoint: `https://api.hyperliquid.xyz/info`.
- Statut: `logs/hip4_outcome_mainnet_status.json`.
- Logs: `logs/hip4_outcome_mainnet/`.
- State: `runtime/hip4_outcome_mainnet_state.json`.
- API UI: `/api/hip4-outcome-mainnet`, et resume integre dans `/hip4-outcome`.
- `write_pod_b_alias_status = false`: l'observateur mainnet ne doit jamais ecraser `logs/pod_b_live_status.json`.
- `allow_testnet_orders = false`, `require_testnet_url = false`, `enforce_testnet_balance_check = false`: c'est strictement de l'observation publique, pas une execution.
- Le start serveur standard lance testnet + sidecar mainnet dans le meme conteneur/process `hip4-outcome-dry-run`; pour retirer uniquement le sidecar: `--without-hip4-mainnet-observer`.
- Verification initiale mainnet `2026-05-03`: `outcomeMeta` expose un BTC `priceBinary` daily (`outcome=1`, `#10/#11`) avec books et mids actifs.

Sources de prix / observation:

- Le moteur sait lire Binance, OKX, Bybit, Coinbase, Kraken et Hyperliquid.
- La config active interroge les venues externes et Hyperliquid, mais ancre la reference a Hyperliquid testnet pour eviter les faux edges quand le testnet diverge du marche mainnet.
- Les sources externes sont conservees comme observation et garde-fou: si elles divergent trop de la reference Hyperliquid, le signal est rejete ou degrade au lieu d'etre pris comme edge exploitable.
- `max_source_deviation_bps = 50`, `min_reference_sources = 1`, `anchor_reference_to_hyperliquid = true`.
- `include_underlyings = []` signifie: accepter tous les `priceBinary` renvoyes par `outcomeMeta`.
- Au preflight courant, Hyperliquid testnet renvoie des marches supportes BTC/HYPE; si SOL/ETH/etc. apparaissent dans `outcomeMeta`, le pod les prendra sans changement de code.
- Raison: les prix testnet peuvent diverger fortement des venues externes; utiliser Binance/OKX/etc. comme verite unique sur testnet peut creer de faux edges.
- `market_observations.jsonl` loggue aussi les classes non tradees (`namedOutcome`, fallback, `priceBucket` incomplet, etc.) avec `sideSpecs`, coins, thresholds et un resume book YES/NO quand disponible.
- `priceBucket` est parse en mode paper/observer quand deux thresholds, ou plus de deux thresholds avec `index`, definissent une bande adjacente claire. Le detecteur `PRICE_BUCKET_MODEL` estime `P(lower <= price <= upper)` via le modele lognormal range, mais le risk gate refuse toute execution testnet avec `price_bucket_paper_only`.
- `Named Outcome` reste strictement watch-only: pas de modele, pas d'execution, pas d'inference de verite tant que la source de resolution/vote n'est pas replayable.

Capital guard:

- Avant tout fill paper/testnet, `OutcomeCapitalGuard` plafonne la taille par budget Pod B et exposition ouverte.
- En `testnet`, il verifie aussi le solde spot quote outcome disponible via `spotClearinghouseState` avant d'envoyer un ordre.
- Sur le testnet courant, les outcomes demandent `USDH`: du `USDC` spot seul ne suffit pas, il faut convertir ou deposer du `USDH`.
- Le statut expose `capital` dans `logs/hip4_outcome_status.json` et dans l'alias `logs/pod_b_live_status.json`.
- Le minimum ordre HL est traite comme `10 USDH` de valeur economique effective, avec `min(limit_price, 1 - limit_price)` pour les outcomes. Les rejets explicites sont `below_exchange_min_order_value_yes/no`.

Frais / PnL:

- Ouverture outcome: `outcome_open_fee_rate = 0.0`.
- Settlement/close outcome: `outcome_settlement_fee_rate = 0.002`.
- En `paper`, le bot estime le settlement depuis la reference locale et applique les frais configures.
- En `testnet`, le bot ne settle plus localement depuis la reference: il attend les fills `Settlement` Hyperliquid, lit `closedPnl`/`fee`, puis corrige l'etat, les CSV et l'alias Pod B avec cette source exchange.
- Les anciens settlements testnet estimes localement doivent etre consideres invalides si Hyperliquid renvoie un fill `Settlement` contradictoire.
- Le statut global et la page HIP-4 doivent lire la meme source d'agregation par coin, pour eviter un PnL Pod B visible sur `/dashboard` mais absent de `/hip4-outcome`.

Isolation Pod A / Pod B:

- Pod B HIP-4 utilise un compte Hyperliquid testnet dedie et un budget USDH dedie.
- Pod A et Pod B ne partagent donc plus ni capital, ni marge, ni budget de risque.
- Un perp Pod A BTC/HYPE/etc. ne bloque plus un outcome HIP-4 sur le meme underlying.
- Les locks atomiques `runtime/hip4_overlap_locks/` ne sont plus utilises par Pod A ou Pod B.
- Les modules d'overlap/lock HIP-4 ont ete supprimes; il ne doit plus rester de cle `directional_overlap`, `hip4_overlap` ou `block_directional_overlap` dans les statuts/UI.
- Les garde-fous conserves sont internes au Pod B: budget, exposition max, `market_already_open`, minimum d'ordre HL, reconciliation/fills/settlement.
- L'UI HIP-4 affiche le budget, le solde testnet disponible, les positions et les executions, sans carte d'overlap Pod A.
- Toute nouvelle logique perps TP/SL, stop clusters, liquidation pressure ou
  microstructure directionnelle doit rester hors Pod B. Le chemin autorise est
  `TriggerLiquidityOverlay -> Pod A / Supervisor`, pas `Pod B`.

## TriggerLiquidityOverlay Perps

Statut: actif en collecte shadow serveur, sans veto/size/boost effectif.

Objectif:

- Lire les TP/SL publics Hyperliquid depuis une source node/order-status ou
  QuickNode HyperCore `orders`.
- Construire une carte compacte de trigger liquidity par symbole.
- Enrichir les snapshots Trident avec des features replayables.
- Produire des decisions `allow/watch/reduce_size/veto_entry/boost_confidence`
  pour `Pod A` et le `Supervisor`, sans execution autonome.

Fichiers principaux:

- `app/hyperliquid/trigger_liquidity.py`
- `app/trident/trigger_liquidity/`
- `app/live/trigger_liquidity_enricher.py`
- `app/live/trigger_liquidity_sql_backfill.py`
- `app/research/pod_liq_features.py`
- `app/research/pod_liq_research.py`
- `app/research/pod_liq_exhaustive_research.py`

Config canonique:

- `[trigger_liquidity]` dans `config/trident.toml`
- Profil serveur courant:
  - `enabled = true`
  - `shadow_only = true`
  - `veto_enabled = false`
  - `sizing_enabled = false`
  - `confidence_boost_enabled = false`
  - service Docker: `trigger-liquidity-collector`
  - service Docker: `trigger-liquidity-enricher`

Features snapshot ajoutees:

- `trigger_liquidity_available`
- `nearest_stop_cluster_bps`
- `nearest_stop_cluster_above_bps`
- `nearest_stop_cluster_below_bps`
- `nearest_tp_cluster_bps`
- `nearest_tp_cluster_above_bps`
- `nearest_tp_cluster_below_bps`
- `stop_pressure_above`
- `stop_pressure_below`
- `tp_pressure_above`
- `tp_pressure_below`
- `trigger_asymmetry`
- `cascade_risk_up`
- `cascade_risk_down`
- `trigger_data_age_seconds`
- `total_trigger_notional_usd`
- `max_trigger_cluster_notional_usd`

Usage concret autorise:

1. `TriggerLiquiditySnapshotEnricher` enrichit un JSONL snapshot depuis les
   `node_order_statuses` TP/SL.
   En live, `trigger-liquidity-collector` lit les donnees node Hyperliquid
   `node_order_statuses/hourly` ou les blocs QuickNode HyperCore `orders`
   quand `TRIDENT_TRIGGER_LIQUIDITY_QUICKNODE_URL` est configure. Il filtre les
   ordres `isTrigger` et ecrit `data/trigger_liquidity/*.jsonl`.
   Ensuite `trigger-liquidity-enricher`
   reecrit en continu `data/live_snapshots_trigger_liquidity/*.jsonl` et publie
   `runtime/trigger_liquidity_enricher_status.json`.
   Le status collecteur est `runtime/trigger_liquidity_collector_status.json`.
   Pour les replays historiques, `trigger_liquidity_sql_backfill` peut remplir
   la meme source depuis QuickNode SQL Explorer `hyperliquid_orders`.
2. Les recherches `pod_liq_*` testent:
   - `trigger_stop_breakout_continuation`
   - `trigger_sweep_reversal`
   - `trigger_tp_exhaustion`
   - `cascade_risk_veto`
3. Le `Supervisor` ajoute les details overlay aux previews Pod A quand
   `trigger_liquidity.enabled = true`.
4. Le `Supervisor` peut appliquer veto/size/boost aux trade plans Pod A
   uniquement si `shadow_only = false` et si le flag correspondant est active.

Regles de securite:

- Pas de nouveau pod capitalise pour cette idee tant que les replays ne battent
  pas la baseline officielle full-bot.
- Pas de trade ouvert uniquement parce qu'un cluster TP/SL existe.
- Donnees stale: ignorer si `trigger_data_age_seconds` depasse
  `max_data_age_seconds`.
- Ne pas assimiler TP/SL a liquidation map: les liquidations restent une
  inference separee, a valider ulterieurement.
- Promotion graduelle seulement dans cet ordre:
  1. logging / dashboard / review;
  2. veto shadow;
  3. veto effectif Pod A;
  4. reduction de taille;
  5. boost de confiance, seulement si la baseline full-bot s'ameliore.

Edge types implementes:

- `MODEL`: proba lognormal static-vol vs prix YES/NO.
- `LATE_EXPIRY`: sous-jacent deja clairement au-dessus/sous le strike proche expiry.
- `PARITY`: achat YES+NO si le cout combine est sous 1.
- `SHORT_EXPIRY`: chemin OpenClaw-like pour marches tres courts.
- `PRICE_BUCKET_MODEL`: paper/observer seulement, proba d'une bande de prix type corridor binary; jamais execute en testnet.

Guardrail testnet actif:

- `enable_model = false` dans `config/hip4_outcome_testnet.toml` depuis la
  review du `2026-05-08`: `MODEL` testnet etait a `-754.5327` PnL, win rate
  `26.98%`, Brier `0.4826`.
- `blocked_opportunity_slices = ["HYPE:LATE_EXPIRY:BUY_YES",
  "HYPE:MODEL:BUY_YES", "HYPE:SHORT_EXPIRY:BUY_YES"]` dans
  `config/hip4_outcome_testnet.toml`.
- `block_reference_divergence = true` cible `HYPE` quand au moins `2` sources
  sont rejetees ou que `reference_max_deviation_bps > 250`. La review du
  `2026-05-08` classait `184` pertes / `-3315.58` PnL sous
  `reference_divergence`; ce guardrail doit rester entry-time avant de remettre
  de la taille testnet.
- Le rejet runtime est explicite: `reason = "blocked_outcome_slice"` avec
  `constraints.blocked_slice = "..."`, ou `reason = "reference_divergence_guard"`
  avec le nombre de sources rejetees et le max deviation bps.
- Le status HIP-4 et l'alias Pod B exposent `blocked_opportunity_slices` et
  `reference_divergence_guard`; l'API `/api/hip4-outcome` relaie ces champs.

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

Testnet Pod B:

- `logs/hip4_outcome_testnet/opportunities.csv`
- `logs/hip4_outcome_testnet/decisions.jsonl`
- `logs/hip4_outcome_testnet/trades.csv`
- `logs/hip4_outcome_testnet/settlements.csv`
- `logs/hip4_outcome_testnet/latency_stats.csv`
- `logs/hip4_outcome_testnet/edge_decay.csv`
- `logs/hip4_outcome_testnet/short_expiry_features.csv`
- `logs/hip4_outcome_testnet/market_observations.jsonl`
- `logs/hip4_outcome_testnet/daily_summary.csv`
- `logs/hip4_outcome_status.json`
- `logs/pod_b_live_status.json` (alias runtime Pod B pour l'UI/reporting)
- `runtime/hip4_outcome_testnet_state.json`

Mainnet observer:

- `logs/hip4_outcome_mainnet/opportunities.csv`
- `logs/hip4_outcome_mainnet/decisions.jsonl`
- `logs/hip4_outcome_mainnet/latency_stats.csv`
- `logs/hip4_outcome_mainnet/edge_decay.csv`
- `logs/hip4_outcome_mainnet/short_expiry_features.csv`
- `logs/hip4_outcome_mainnet/market_observations.jsonl`
- `logs/hip4_outcome_mainnet/daily_summary.csv`
- `logs/hip4_outcome_mainnet_status.json`
- `runtime/hip4_outcome_mainnet_state.json`

Note: les anciens chemins `logs/hip4_outcome_paper/` et `runtime/hip4_outcome_paper_state.json` peuvent exister dans les archives locales; l'exploitation courante doit privilegier le `logs_dir` et `state_path` de `config/hip4_outcome_testnet.toml`.

Etat d'observation et execution:

- Un signal BTC `MODEL` propre a ete observe en paper autour de `net_edge ~0.36-0.38`.
- Un signal HYPE `SHORT_EXPIRY` a ete observe autour de `best_net_edge ~0.152`.
- Premier ordre testnet valide le `2026-05-03`: `HYPE_GT_58.5_20260503_0800`, `BUY_YES`, `38` tokens a `0.71`, cout `26.98 USDH`, oid `52407686267`.
- Correction settlement testnet `2026-05-03`: les premiers HYPE qui semblaient gagnants localement ont ete settles perdants par Hyperliquid (`Settlement.closedPnl`), donc le PnL testnet doit suivre l'exchange et non la reference locale.
- Les premiers gros edges HYPE vus avec reference externe ne doivent pas etre consideres comme edge mainnet fiable: le prix HYPE testnet Hyperliquid divergeait fortement des venues externes.
- Conclusion courante: l'execution testnet, la reconciliation exchange et le PnL net refletent maintenant la realite HL; l'existence d'un edge durable reste a prouver sur plusieurs marches/expiries.

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
  --profile testnet \
  --output logs/hip4_outcome_testnet/replay_latest.json
```

Replay observer mainnet:

```bash
uv run python -m app.backtest.hip4_outcome_replay \
  --profile mainnet \
  --output logs/hip4_outcome_mainnet/replay_latest.json
```

Review post-fetch paper / testnet / mainnet observer:

```bash
uv run python -m app.backtest.hip4_outcome_run_review \
  --output-json server-data/replay_reports/hip4_outcome_run_review_latest.json \
  --output-md server-data/replay_reports/hip4_outcome_run_review_latest.md
```

Note: `scripts/fetch_trident_data.sh` lance maintenant cette review automatiquement via
`scripts/trident_dry_run_review.sh` quand les logs HIP-4 ont ete rapatries.
Le fetch rapatrie les dossiers HIP-4 entiers (`testnet`, `mainnet`, `paper`), et la review
inclut `market_observations.jsonl` dans `hip4_outcome_run_review_latest.{json,md}`:
comptes par classe HIP-4, support status, raisons, underlyings, books observes,
`priceBucket` et `namedOutcome`.
Le rapport inclut une simulation de candidats guardrails: impact PnL/PF/Brier apres
exclusion, verdict `keep/watch/park/kill`, et separation entre slices entry-time
actionnables et categories de pertes post-trade.
Le candidat garde en config testnet est `HYPE:LATE_EXPIRY:BUY_YES`; revalider
apres la prochaine fenetre de collecte avant d'ajouter d'autres slices.

Verification serveur apres deploiement:

```bash
ssh trident-hetzner "cd /opt/trident && curl -fsS http://127.0.0.1:3000/api/hip4-outcome | python3 -c 'import json,sys; print(json.load(sys.stdin).get(\"blocked_opportunity_slices\"))'"
```

Attendu:

```text
['HYPE:LATE_EXPIRY:BUY_YES']
```

Pour de vrais ordres testnet:

- Fournir `HIP4_OUTCOME_SECRET_KEY` pour l'API wallet dedie Pod B.
- Fournir `HIP4_OUTCOME_ACCOUNT_ADDRESS` pour le wallet testnet finance.
- Passer `HIP4_OUTCOME_MODE=testnet`.
- Activer explicitement `HIP4_OUTCOME_ALLOW_TESTNET_ORDERS=true`.
- Lancer d'abord `--preflight`.
- Verifier que le solde `USDH` est visible via `spotClearinghouseState`.
- Garder les caps internes actifs meme si le compte testnet contient plus de capital.

Preparation mainnet:

- Accumuler d'abord les logs mainnet observer via le deploiement standard.
- Rejouer `logs/hip4_outcome_mainnet/opportunities.csv` avec `--profile mainnet`.
- Comparer mainnet observer vs testnet execution: edge decay, spreads, profondeur, fills theoriques, reference prices, horaires d'expiry.
- Aucune execution mainnet ne doit etre ajoutee sans nouveau mode explicite `mainnet`, credentials mainnet dedies, preflight mainnet, caps tiny-size et confirmation manuelle.

Ce qui manque encore pour se rapprocher d'un bot type OpenClaw performant:

- WebSocket ou streaming plus bas-latence pour mids et order books.
- Statistiques par expiry apres settlement reel/estime.
- Mesure slippage/fill testnet avec tres petites tailles.
- Plus de donnees sur marches 5m/15m avant calibration.
- Une politique de sizing dynamique uniquement apres preuve paper/testnet.
- Une calibration probabiliste serieuse avant tout Kelly ou ML.
- Une base de snapshots complete pour rejouer decisions, books, references, fills, settlements et edge decay.
- Un mode mainnet execution explicite et separe de `testnet`, seulement apres validation de l'observateur mainnet.

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

Validation code HIP-4, observation embedded et integration UI/dry-run:

```bash
uv run python -m py_compile app/trident/hip4_outcome/models.py app/trident/hip4_outcome/parser.py app/trident/hip4_outcome/config.py app/trident/hip4_outcome/probability.py app/trident/hip4_outcome/edge.py app/trident/hip4_outcome/runner.py app/trident/hip4_outcome/logging.py app/trident/hip4_outcome/risk.py app/trident/hip4_outcome/analysis.py app/live/hip4_outcome_runner.py app/live/trident_dry_run_launcher.py app/observability/api.py
```

```bash
bash -n deploy.sh scripts/trident_server.sh scripts/trident_dry_run_review.sh scripts/fetch_trident_data.sh
```

```bash
uv run python -m unittest tests.test_hip4_outcome_pod tests.test_hip4_outcome_analysis tests.test_trident_dry_run_launcher tests.test_health
```

Resultat courant `2026-05-05`:

- `uv run python -m unittest tests.test_hip4_outcome_pod`: `48` tests OK.
- `uv run python -m unittest tests.test_hip4_outcome_analysis`: `2` tests OK; couvre la synthese `market_observations` dans la review automatique.
- `uv run python -m unittest tests.test_trident_dry_run_launcher tests.test_health`: `18` tests OK.
- `uv run python -m py_compile app/trident/hip4_outcome/models.py app/trident/hip4_outcome/parser.py app/trident/hip4_outcome/config.py app/trident/hip4_outcome/probability.py app/trident/hip4_outcome/edge.py app/trident/hip4_outcome/runner.py app/trident/hip4_outcome/logging.py app/trident/hip4_outcome/risk.py app/trident/hip4_outcome/analysis.py app/live/hip4_outcome_runner.py app/live/trident_dry_run_launcher.py app/observability/api.py`: OK.
- `bash -n deploy.sh scripts/trident_server.sh scripts/fetch_trident_data.sh scripts/trident_dry_run_review.sh`: OK.
- `uv run python -m app.live.hip4_outcome_runner --config config/hip4_outcome_testnet.toml --mode observer --once`: OK, testnet `markets_seen=14`, `markets_supported=2`, `market_observation.total=14`, classes observees `fallback=1`, `namedOutcome=3`, `priceBinary=3`, `unknown=7`, `books_logged=11`; sidecar mainnet embedded OK avec `markets_seen=1`, `markets_supported=1`, `opportunities=1`, aucune execution.
- `priceBucket` parse et modele paper/observer couverts; execution testnet refusee par risk gate `price_bucket_paper_only`.
- `Named Outcome` et classes inconnues logguees en observation, sans modele ni execution.
- `uv run python -m app.backtest.hip4_outcome_replay --profile mainnet`: OK.
- Derniere verification serveur avant ajout de l'observateur mainnet: `trident-pod-a-live`, `trident-pod-c-live`, `trident-hip4-outcome-dry-run`, `trident-api` et collectors actifs.
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

- Deployer les changements via `./deploy.sh --start --mode dry-run` depuis le poste local.
- Lancer le bot complet sans `--without-pod-b`.
- Verifier que le service actif est `hip4-outcome-dry-run`, pas `pod-b-live`.
- Verifier que le sidecar mainnet apparait dans `embedded_observers` de `logs/hip4_outcome_status.json`, sans process `hip4-outcome-mainnet-observer` separe.
- Verifier que l'env serveur active bien `HIP4_OUTCOME_MODE=testnet` et `HIP4_OUTCOME_ALLOW_TESTNET_ORDERS=true` seulement pour le compte testnet dedie.
- Verifier que le capital visible est en `USDH`, pas seulement en `USDC`.
- Verifier que `/api/hip4-outcome.blocked_opportunity_slices` contient
  `HYPE:LATE_EXPIRY:BUY_YES`.
- Suivre `/hip4-outcome`:
  - edges par type
  - short-expiry features
  - edge decay
  - settlements estimes
  - PnL paper par underlying
  - PnL testnet net fees par underlying
  - bloc mainnet observer: markets, references, opportunities, replay mainnet
  - observations de classes HIP-4 non supportees et `priceBucket`
- Suivre aussi `/dashboard` et `/api/report`: Pod B doit pointer vers `pod_kind = hip4_outcome_edge_pod`.
- Ne pas conclure sur un seul signal; attendre plusieurs expiries.

### 2. Analyser Les Runs Pod B

Prerequis:

- fetch serveur complet apres quelques heures.
- logs testnet et mainnet observer: `opportunities`, `decisions`, `trades`, `settlements`, `edge_decay`, `short_expiry_features`, `market_observations`, `daily_summary`.
- statut API et UI coherents.

Action:

- separer les vrais edges des artefacts testnet: divergence HYPE/BTC, book stale, absence de profondeur, settlement mismatch.
- calculer PnL net fees par coin, cote, type d'edge, horizon d'expiry et heure.
- mesurer win rate, profit factor, drawdown, edge decay et fill quality. Le win rate seul ne suffit pas.
- comparer paper vs testnet quand les deux sources existent.
- comparer mainnet observer vs testnet: reference price, spread, depth, edge decay, et frequence des signaux.
- produire `hip4_outcome_run_review_latest.{json,md}` apres chaque fetch serveur complet.
- utiliser la section `Guardrail Candidates` pour choisir les prochaines restrictions testnet.
- Ne pas ajouter d'autre slice tant que la fenetre post-guardrail n'a pas confirme
  un Brier testnet `<= 0.23` avec un volume encore exploitable.

### 3. Calibration Avant Sizing Dynamique

- Ajouter Brier score, log-loss et calibration buckets par type d'edge.
- Faire du walk-forward par jour/expiry plutot que valider sur une seule fenetre.
- N'autoriser fractional Kelly ou XGBoost qu'apres historique suffisant et stable.
- Garder `max_position_usdc`, `max_total_outcome_exposure_usdc` et `max_per_underlying_outcome_exposure_usdc` comme hard caps meme si Kelly propose plus.

### 4. Ameliorer La Latence HIP-4 Seulement Si Necessaire

Priorite apres plusieurs runs testnet/mainnet observer:

- remplacer le polling critique par streaming/WS si l'edge decay montre que les signaux disparaissent trop vite.
- ajouter book cache / allMids cache pour eviter de dependre de REST a chaque boucle.
- mesurer la latence dans `latency_stats.csv` avant d'optimiser.

### 5. Garder Pod A / Pod C Stables

- Pas de nouveau sweep massif tant que le Pod B HIP-4 est en exploration testnet.
- Rejouer la baseline officielle seulement quand le fetch serveur change ou avant une promotion.
- Toute divergence live/replay doit etre analysee avec `collector + maintenance_refresh`.
- Resultats shortlist OOS `2026-05-05`:
  - Pod A HYPE veto: `reject`, ne pas promouvoir; envisager rollback apres confirmation sur fenetre plus large.
  - Pod A MTF vetoes: `reject` sur l'OOS recente malgre validation historique; ne pas etendre, surveiller.
  - Pod A BTC/XRP overextension: `no_effect`, aucun declenchement sur cette fenetre.
  - Pod C relaxed cluster-aware off: `reject` (`56` trades, Pod C `+1.05` vs `5` trades, Pod C `+20.17` courant); conserver la selectivite.
  - Pod C silver veto: `reject` sur `1` veto; candidat rollback/watch.
  - Pod C gold vetoes: `gold_soft_extension_veto` et `gold_strong_neutral_veto` sans effet; `gold_medium_neutral_veto` rejete (`-19.62`).
  - Pod C signal drought recent: `2026-05-02`, `2026-05-03` et `2026-05-05` sans signal; `2026-05-04` a `6` signaux mais `0` acceptes. Pas d'anomalie mecanique prouvee, plutot selectivite/regime.

### 6. Deploiement / Rollback

- S'assurer que le serveur utilise `config/trident.toml`.
- Verifier que le dry-run lance bien HIP-4 comme Pod B, avec l'observateur mainnet integre au meme process, et que les seuls ordres reels possibles sont testnet sur compte dedie.
- Garder un rollback simple:
  - couper le Pod B HIP-4 avec `--without-pod-b`
  - `--without-hip4-outcome` reste accepte comme alias historique
  - couper seulement le sidecar observateur mainnet avec `--without-hip4-mainnet-observer`
  - ou laisser `allow_testnet_orders = false`

## Regles De Promotion

- Une idee validee seulement en candles/research ne passe pas en prod.
- Une idee positive en standalone mais negative en full-bot ne passe pas en prod.
- Une idee HIP-4 paper/testnet ne passe pas en mainnet sans observation mainnet replayable, testnet tiny-order, puis preflight mainnet separe.
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
