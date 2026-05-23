# TRIDENT Active Plan

Date: `2026-05-23`

## Status

- `ACTIVE_SINGLE_SOURCE_OF_TRUTH`
- Ce fichier est la feuille de route courante. Les autres documents sont des archives, des notes de recherche, ou des details d'implementation.
- En cas de contradiction avec un ancien doc, ce fichier gagne.
- Objectif actuel: transformer le canary live testnet `Pod A` + `Pod C` valide
  techniquement en burn-in propre, puis preparer le canary mainnet tiny-size.
  `Pod B HIP-4 Outcome` reste en mainnet paper, comme pod independant, sans
  cannibaliser Pod A.

## Lecture Rapide

- Config prod/dry-run principale: `config/trident.toml`.
- Mode cible live hybride: `Pod A` crypto core et `Pod C` tradfi en vrais
  ordres apres preflight; `Pod B HIP-4 Outcome` reste `paper` mainnet.
- Etat serveur `2026-05-21`: redeploiement propre en `live/testnet` avec
  `Pod A` + `Pod C` en vrais ordres testnet et `Pod B HIP-4 Outcome` force en
  `mainnet paper`. Le baseline de burn-in repart du demarrage conteneur
  `2026-05-21T06:07:35Z`.
- Reconciliation post-redeploiement: `Pod C` a recupere la position SOL
  existante depuis le state store, `Pod A` l'a classee
  `external_known_positions`, et aucun `unknown_exchange_positions`,
  `missing_exchange_positions`, `side_mismatches`, `open_orders` inconnu ou
  `trigger_orders` orphelin n'a ete observe.
- Les valeurs exchange sont maintenant prioritaires pour les positions live
  existantes:
  - `target_notional_usd` local = `abs(size) * entryPx`;
  - `current_notional_usd` = `positionValue`;
  - `margin_usd` = `marginUsed`;
  - `unrealized_pnl_usd` = `unrealizedPnl`;
  - levier/isolation viennent aussi de Hyperliquid quand disponibles.
- Le close live reduce-only utilise la taille exacte de la position exchange,
  au lieu de reconstruire une taille depuis un notionnel local potentiellement
  stale.
- La page `Status > Pods` affiche maintenant `PnL realise` et `PnL latent` dans
  la carte de chaque pod.
- Pods actifs dry-run: `Pod A` crypto core avec `a_grade_enabled`, `Pod B HIP-4 Outcome`, `Pod C` tradfi.
- Pod B historique directionnel: legacy / non demarre par defaut.
- Nouveau Pod B: `HIP4OutcomeEdgePod`, branche HIP-4 outcome en mainnet paper.
  Le testnet HIP-4 a ete arrete: ses donnees n'etaient pas representatives,
  mais il a valide l'architecture, les signatures, les ordres, la reconciliation
  et le format de settlement.
- UI:
  - dashboard principal: `/dashboard`
  - monitoring HIP-4: `/hip4-outcome`
  - API HIP-4: `/api/hip4-outcome`
- Pod B HIP-4 expose maintenant un `operator_brief` et une `short_expiry_watchlist`
  dans son status/API, afin de piloter explicitement les fenêtres proches expiry
  sans transformer le mainnet paper en claim de performance.
- Les blocages HYPE issus du testnet ont ete retires: ils n'ont pas de sens
  comme regle mainnet tant qu'une review mainnet-paper ne prouve pas un
  guardrail entry-time.
- Regle de promotion: aucune logique HIP-4 ne passe en execution mainnet sans
  dataset mainnet complet, calibration, replay comparable, dry-run mainnet
  propre, preflight separe, caps tiny-size et confirmation manuelle.
- Decision live A/C: le burn-in `live/testnet` est relance proprement depuis
  le redeploiement du `2026-05-21T06:07:35Z`, apres correction de la selection
  de fills de close stale et de la conservation des metadonnees d'ordres dans
  le state store. Si aucun incident bloquant n'apparait pendant `72h`,
  reevaluation des criteres de passage mainnet tiny-size le
  `2026-05-24T06:07:35Z`.

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

Rejeu du meme input avec le repo/config courants le `2026-05-19`:

| Total | Pod A | Pod B | Pod C |
|---:|---:|---:|---:|
| `+872.74 USD` | `+793.63` | `0.00` | `+79.11` |

Notes importantes:

- L'input de reference couvre `2026-04-05T19:45:00Z -> 2026-05-13T07:56:49Z`.
- La baseline officielle archivee reste `+859.83 USD`, mais le replay actuel
  du meme JSONL sort `+872.74 USD` (`+12.91`). L'ecart vient uniquement de
  `6` trades `HYPE trend_pullback_long` Pod A reintroduits par le rollback du
  veto HYPE; Pod C reste strictement inchange a `+79.11 USD`.
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

Statut: actif, reference principale crypto. Chemin live/testnet valide
techniquement avec reconciliation exchange stricte.

Point live `2026-05-19`:

- redemarrage serveur en `live/testnet` teste avec une position BTC deja ouverte
  cote Hyperliquid;
- Pod A n'a pas repris BTC car la position etait connue par le state store Pod C;
- rapport attendu observe: `external_known_positions=["BTC"]`, `ready=true`,
  pas de position locale et `live_trading_paused=false`;
- ne pas passer au canary mainnet tant que le burn-in testnet n'a pas plusieurs
  cycles propres de restart, sync, close/reopen et review logs.

Promu dans le profil repo:

- `pod_a.stop_grace_minutes = 165`, scope utile: `trend_pullback_long`.
- `pod_a.opposite_signal_debounce_minutes = 15`.
- `pod_a.a_grade_enabled = true`: boost selectif des entrees A-grade
  `trend_pullback_long` crypto, avec scaling `1.25x` / `1.40x` et exits plus
  larges (`break_even x1.20`, `trailing_activation x1.15`,
  `trailing_distance x1.35`). Backtest `2026-04-05 -> 2026-05-13`:
  `+190.14 USD` vs baseline corrigee.
- Vetoes MTF Pod A valides le `2026-04-27`, non confirmes sur l'OOS
  `2026-04-30 -> 2026-05-05` (`-4.16`, `5` vetoes), mais redevenus
  positifs sur le latest fetch `2026-04-05 -> 2026-05-16` (`+32.97`,
  `94` vetoes). Statut: conserver actifs, pas etendre sans nouveau replay.
- Veto BTC overextension 4h, scope BTC long, et veto XRP overextension 4h,
  scope XRP long: `no_effect` sur l'OOS `2026-05-05`, mais `keep` sur latest
  fetch `2026-04-05 -> 2026-05-16` (`+26.20`, `3` vetoes). Statut:
  conserver actifs, ne pas elargir.
- Veto HYPE `trend_pullback_long`: rollback applique dans `config/trident.toml`
  le `2026-05-17` avec `hype_trend_pullback_long_targeted.enabled = false`.
  Decision prise apres rejet sur l'OOS `2026-04-30 -> 2026-05-05` (`-12.03`,
  `3` trades HYPE vetoes qui auraient ete gagnants) et latest fetch
  `2026-04-05 -> 2026-05-16` (`-14.72`, `13` vetoes). A ne pas confondre avec
  les anciens blocages HYPE HIP-4, eux aussi retires. Le replay de la baseline
  officielle du `2026-05-19` confirme l'impact attendu: `6` trades HYPE
  reintroduits, `+12.91 USD`, Pod C inchange.
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

### Pod C - Tradfi

Statut: actif, quasi stabilise. Canary `live/testnet` serveur en cours.

Point live `2026-05-19`:

- restart reel Pod C avec position BTC deja ouverte cote Hyperliquid: reprise OK;
- state live Pod C mis a jour depuis Hyperliquid:
  `entry_price=77326.0`, `target_notional_usd=94.33772`,
  `margin_usd` et `unrealized_pnl_usd` lus depuis l'exchange;
- status runtime Pod C utilise les valeurs exchange pour le PnL latent et la
  valeur courante quand elles sont disponibles;
- logs post-restart verifies sans `Traceback`, sans `Decimal is not JSON
  serializable`, et sans echec de reconciliation.

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
- Veto `silver_strong_extension_veto`, historiquement promu mais non confirme
  sur l'OOS `2026-04-30 -> 2026-05-05` (`-2.56`, `1` veto), puis confirme
  positif sur latest fetch `2026-04-05 -> 2026-05-16` (`+18.52`, `13`
  vetoes). Statut: conserver actif, ne pas etendre sans preuve.

Principes:

- Ne pas etendre la grace `routing_revoked` a `silver` ou `gold` sans nouvelle preuve.
- Ne pas relacher globalement les `stop_hit` Pod C.
- Garder `equity` et `fx` en observation, pas en nouvelle branche active.
- Gold: `gold_soft_extension_veto` devient watch tiny-sample (`+6.26`, `1`
  veto) mais ne doit pas etre promu sans repetition; `gold_strong_neutral_veto`
  reste sans effet, `gold_medium_neutral_veto` reste rejete (`-32.78`, `3`
  vetoes).

### Pod B Directionnel Historique

Statut: remplace / legacy.

Conclusion courante:

- Les variantes Pod B testees n'ont pas ete portefeuille-additives sur les snapshots comparables.
- Le silence de Pod B venait en partie d'une incoherence allocation/regime, mais la correction brute cannibalisait Pod A.
- Le service Docker historique reste disponible seulement sous profil `legacy_pod_b`.
- Il ne doit pas redevenir actif sans nouvelle validation full-bot.

## Pod B HIP-4 Outcome

Statut: implemente, integre comme remplacement complet du Pod B, repo safe par
defaut en paper, serveur actuellement oriente mainnet paper. Le testnet reste
un profil historique / preflight, pas une source de donnees exploitable.

Fichiers principaux:

- `app/trident/hip4_outcome/`
- `app/live/hip4_outcome_runner.py`
- `app/backtest/hip4_outcome_replay.py`
- `config/hip4_outcome_mainnet_paper.toml`
- `config/hip4_outcome_testnet.toml`
- `config/hip4_outcome_mainnet_observer.toml`
- `tests/test_hip4_outcome_pod.py`

Integration bot complet:

- `app/live/trident_dry_run_launcher.py` lance HIP-4 comme resultat `pod_b`; l'ancien runner directionnel n'est plus lance.
- `scripts/trident_server.sh` mappe le profil `pod_b` vers le service `hip4-outcome-dry-run`.
- Redéploiement ciblé Pod B sans casser le burn-in Pod A/C:
  `./deploy.sh --start --only-pod-b`. Ce chemin build/recrée uniquement
  `hip4-outcome-dry-run`, sans `stop_unmanaged_services`, sans preflight A/C et
  sans recréer `pod-a-live` ou `pod-c-live`.
- `docker-compose.trident.yml` lance `hip4-outcome-dry-run` avec
  `config/hip4_outcome_mainnet_paper.toml` par defaut; l'ancien
  `hip4-outcome-mainnet-observer` reste defini seulement comme service
  legacy/manual, et l'ancien `pod-b-live` reste sous `legacy_pod_b`.
- HIP-4 ecrit aussi `logs/pod_b_live_status.json`, ce qui rend le reporting/UI Pod B compatible avec le nouveau pod.
- Le pod ne modifie pas le routing Pod A/Pod C.
- Aucun ordre mainnet reel n'est possible dans l'etat courant: le profil actif
  est `paper`, sans mode execution mainnet.

Modes:

| Mode | Effet |
|---|---|
| `observer` | lit les marches, calcule les signaux, loggue, aucun fill |
| `paper` | simule les fills au visible ask, estime le settlement |
| `testnet` | peut envoyer de vrais ordres testnet IOC si credentials et garde-fous sont actifs; le PnL settled vient des fills `Settlement` Hyperliquid |

Defaults repo:

- `mode = "paper"`
- `allow_testnet_orders = false`
- `require_testnet_url = false` dans le profil mainnet paper
- `pod_b_budget_usdc = 500`
- `max_position_usdc = 50`
- `max_total_outcome_exposure_usdc = 500`
- `max_per_underlying_outcome_exposure_usdc = 150`
- `max_outcome_markets_open = 3`
- `enforce_testnet_balance_check = false` dans le profil mainnet paper
- `testnet_balance_coin = "USDH"`
- `testnet_balance_buffer_usdc = 1`
- Les noms historiques `*_usdc` representent le budget notionnel; sur mainnet
  paper ils restent en USDC notionnel simule.

Profil testnet:

- arrete comme source d'analyse, car les prix/settlements etaient trop
  divergents pour conclure sur un edge;
- conserve pour preflight technique et regression d'architecture seulement;
- ne doit pas redevenir un critere de promotion mainnet.

Observation / paper mainnet:

- Config active: `config/hip4_outcome_mainnet_paper.toml`.
- Endpoint: `https://api.hyperliquid.xyz/info`.
- Statut Pod B: `logs/hip4_outcome_status.json` et alias
  `logs/pod_b_live_status.json`.
- Logs: `logs/hip4_outcome_mainnet_paper/`.
- State: `runtime/hip4_outcome_mainnet_paper_state.json`.
- API UI: `/api/hip4-outcome`, et resume integre dans `/dashboard`.
- Profil observer public historique: `config/hip4_outcome_mainnet_observer.toml`,
  `logs/hip4_outcome_mainnet/`, sans alias Pod B.
- Verification initiale mainnet `2026-05-03`: `outcomeMeta` expose un BTC
  `priceBinary` daily (`outcome=1`, `#10/#11`) avec books et mids actifs.

Sources de prix / observation:

- Le moteur sait lire Binance, OKX, Bybit, Coinbase, Kraken et Hyperliquid.
- La config active interroge les venues externes et Hyperliquid mainnet, puis
  ancre la reference a Hyperliquid mainnet pour eviter de prendre une venue
  externe isolee comme verite unique.
- Les sources externes sont conservees comme observation et garde-fou: si elles
  divergent trop de la reference Hyperliquid mainnet, le signal est rejete ou
  degrade au lieu d'etre pris comme edge exploitable.
- `max_source_deviation_bps = 50`, `min_reference_sources = 1`, `anchor_reference_to_hyperliquid = true`.
- `include_underlyings = []` signifie: accepter tous les `priceBinary` renvoyes par `outcomeMeta`.
- Le profil testnet conserve le meme mecanisme uniquement pour preflight
  technique. Ses divergences avec le marche mainnet ne sont plus utilisees pour
  definir des blocages mainnet.
- `market_observations.jsonl` loggue aussi les classes non tradees (`namedOutcome`, fallback, `priceBucket` incomplet, etc.) avec `sideSpecs`, coins, thresholds et un resume book YES/NO quand disponible.
- `priceBucket` est parse en mode paper/observer quand deux thresholds, ou plus de deux thresholds avec `index`, definissent une bande adjacente claire. Le detecteur `PRICE_BUCKET_MODEL` estime `P(lower <= price <= upper)` via le modele lognormal range, mais reste paper/observer seulement.
- `Named Outcome` reste strictement watch-only: pas de modele, pas d'execution, pas d'inference de verite tant que la source de resolution/vote n'est pas replayable.

Capital guard:

- Avant tout fill paper, `OutcomeCapitalGuard` plafonne la taille par budget
  Pod B simule et exposition ouverte.
- En profil testnet preflight seulement, il peut aussi verifier le solde spot
  quote outcome via `spotClearinghouseState` avant un ordre technique.
- Le statut expose `capital` dans `logs/hip4_outcome_status.json` et dans l'alias `logs/pod_b_live_status.json`.
- Le minimum ordre HL est traite comme `10 USDH` de valeur economique effective, avec `min(limit_price, 1 - limit_price)` pour les outcomes. Les rejets explicites sont `below_exchange_min_order_value_yes/no`.

Frais / PnL:

- Ouverture outcome: `outcome_open_fee_rate = 0.0`.
- Settlement/close outcome: `outcome_settlement_fee_rate = 0.002`.
- En `mainnet_paper`, le bot estime le settlement depuis la reference
  Hyperliquid mainnet et applique les frais configures.
- En profil testnet historique/preflight, le bot ne settle plus localement
  depuis la reference: il attend les fills `Settlement` Hyperliquid, lit
  `closedPnl`/`fee`, puis corrige l'etat, les CSV et l'alias Pod B avec cette
  source exchange.
- Les anciens settlements testnet estimes localement doivent etre consideres
  invalides si Hyperliquid renvoie un fill `Settlement` contradictoire.
- Le statut global et la page HIP-4 doivent lire la meme source d'agregation par coin, pour eviter un PnL Pod B visible sur `/dashboard` mais absent de `/hip4-outcome`.

Isolation Pod A / Pod B:

- Pod B HIP-4 mainnet paper utilise un budget simule dedie et ne reserve pas de
  marge directionnelle.
- Pod A et Pod B ne partagent donc plus ni capital live, ni marge, ni budget de
  risque.
- Un perp Pod A BTC/HYPE/etc. ne bloque plus un outcome HIP-4 sur le meme underlying.
- Les locks atomiques `runtime/hip4_overlap_locks/` ne sont plus utilises par Pod A ou Pod B.
- Les modules d'overlap/lock HIP-4 ont ete supprimes; il ne doit plus rester de cle `directional_overlap`, `hip4_overlap` ou `block_directional_overlap` dans les statuts/UI.
- Les garde-fous conserves sont internes au Pod B: budget, exposition max, `market_already_open`, minimum d'ordre HL, reconciliation/fills/settlement.
- L'UI HIP-4 affiche le budget, les positions paper et les executions simulees,
  sans carte d'overlap Pod A. Le solde testnet n'est pertinent que pour un
  preflight technique.

Edge types implementes:

- `MODEL`: proba lognormal static-vol vs prix YES/NO.
- `LATE_EXPIRY`: sous-jacent deja clairement au-dessus/sous le strike proche expiry.
- `PARITY`: achat YES+NO si le cout combine est sous 1.
- `SHORT_EXPIRY`: chemin OpenClaw-like pour marches tres courts.
- `PRICE_BUCKET_MODEL`: paper/observer seulement, proba d'une bande de prix type corridor binary; jamais execute en reel.

Review mainnet paper / calibration:

- La review automatique est en place via
  `app/backtest/hip4_outcome_run_review.py` et
  `app/trident/hip4_outcome/analysis.py`.
- Elle calcule PnL, profit factor, Brier score, log-loss, buckets de
  calibration, loss review et simulations de guardrails.
- Dernier rapport `2026-05-16`:
  `server-data/replay_reports/hip4_outcome_run_review_latest.md`.
- Statut `mainnet_paper`: `collect_more_data`, `27,650` opportunities,
  `4` trades approuves, `3` settlements, PnL `+96.0778`, PF `2.9315`,
  Brier `0.2695`.
- Blockers restants: settlements `3/20`, expiries/marches `3/5`, samples de
  calibration `3/20`, Brier cible non atteint (`0.2695` vs `<= 0.23`).
- Aucun blocage HYPE HIP-4 n'est actif en mainnet paper. Les anciens blocages
  HYPE testnet ont ete retires parce que le testnet n'est plus une source de
  performance representative.
- Decision dry-run `2026-05-23` apres backtest PnL levers:
  `server-data/replay_reports/hip4_outcome_pnl_lever_backtests_20260523T164104Z.md`.
  Pas de blocage statique par coin/cote (`blocked_opportunity_slices = []`).
  Le `shock_guard` reste global sur tous les `priceBinary`, mais il est moins
  agressif: il faut maintenant `2` fenetres adverses avant rejet
  (`shock_guard_min_adverse_windows = 2`). Les seuils restent `15m`, `1h`,
  `4h`, `1d`, `3d`, `7d` a `80`, `150`, `250`, `300`, `300`, `400` bps.
  Si l'historique shock est absent, il est seed depuis `opportunities.csv`.
- Sorties anticipees `2026-05-23`: `bid_over_conservative_hold_ev` reste actif
  et GO dry-run. Le `probability_stop` revient en dry-run paper avec seuil
  PnL-first de compromis: `early_exit_stop_probability = 0.35`,
  `early_exit_stop_max_loss_roi = 0.20`. Le `0.32/0.15` etait trop timide
  sur les candidats observes; le `0.35/0.20` garde le declenchement defensif
  tout en refusant les sorties deja trop abimees.
- GO observe ajoutes/maintenus:
  - `shadow_policy_ev_plus_2pct_full` dans `shadow_exit_policies.csv`;
  - `shadow_sizing_half_kelly` dans `shadow_sizing.csv`;
  - `shock_guard_two_window_confirmation` expose dans le status via
    `summary.pnl_levers`.
- `SHORT_EXPIRY` reste observation-only pendant la fenetre 48h
  (`short_expiry_observe_only = true`): les features/watchlist continuent
  d'etre logguees, mais aucune opportunite `SHORT_EXPIRY` ne doit etre ouverte.
- NOGO a ne pas promouvoir sur cette fenetre: variantes `SHORT_EXPIRY` teste
  hold-to-settlement proxy, durcissement BUY_YES downtrend par edge/rebound,
  `shock_guard` one-hit courant, `shock_guard_scale_2x`, maker quotes sans
  modele de fills.
- Revue prevue apres `48h` de dry-run avec ces reglages: comparer PnL realise,
  PnL si hold-to-settlement par raison de sortie, nombre de stops proba,
  `market_already_open`, et PnL par side/underlying.

Mode `SHORT_EXPIRY`:

- Statut courant `2026-05-23`: observation-only pendant 48h. Les features,
  watchlist et raisons de readiness sont logguees, mais l'edge type
  `SHORT_EXPIRY` ne genere pas d'entree paper.
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

Mainnet paper Pod B actif:

- `logs/hip4_outcome_mainnet_paper/opportunities.csv`
- `logs/hip4_outcome_mainnet_paper/decisions.jsonl`
- `logs/hip4_outcome_mainnet_paper/trades.csv`
- `logs/hip4_outcome_mainnet_paper/settlements.csv`
- `logs/hip4_outcome_mainnet_paper/latency_stats.csv`
- `logs/hip4_outcome_mainnet_paper/edge_decay.csv`
- `logs/hip4_outcome_mainnet_paper/short_expiry_features.csv`
- `logs/hip4_outcome_mainnet_paper/market_observations.jsonl`
- `logs/hip4_outcome_mainnet_paper/daily_summary.csv`
- `logs/hip4_outcome_status.json`
- `logs/pod_b_live_status.json` (alias runtime Pod B pour l'UI/reporting)
- `runtime/hip4_outcome_mainnet_paper_state.json`

Testnet Pod B historique/preflight:

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

Mainnet observer historique:

- `logs/hip4_outcome_mainnet/opportunities.csv`
- `logs/hip4_outcome_mainnet/decisions.jsonl`
- `logs/hip4_outcome_mainnet/latency_stats.csv`
- `logs/hip4_outcome_mainnet/edge_decay.csv`
- `logs/hip4_outcome_mainnet/short_expiry_features.csv`
- `logs/hip4_outcome_mainnet/market_observations.jsonl`
- `logs/hip4_outcome_mainnet/daily_summary.csv`
- `logs/hip4_outcome_mainnet_status.json`
- `runtime/hip4_outcome_mainnet_state.json`

Note: les anciens chemins `logs/hip4_outcome_paper/` et
`runtime/hip4_outcome_paper_state.json` peuvent exister dans les archives
locales; l'exploitation courante doit privilegier le `logs_dir` et
`state_path` de `config/hip4_outcome_mainnet_paper.toml`.

Etat d'observation et execution:

- Le mainnet paper a pris le relais du testnet comme source d'observation et
  de dry-run exploitable.
- La derniere review mainnet paper recalculee le `2026-05-23` reste
  `collect_more_data`: `53` trades, `52` settlements, win rate `24/52`,
  PnL `-34.1787`, PF `0.9106`, Brier `0.2405`. Le volume est maintenant
  suffisant pour tester des leviers paper, mais pas pour promotion mainnet.
- Le testnet a valide les briques techniques: signatures, ordres IOC,
  reconciliation exchange, parsing de `Settlement.closedPnl`/`fee`, alias Pod B
  et UI.
- Les premiers gros edges HYPE vus avec reference externe ne doivent pas etre
  consideres comme edge mainnet fiable: ils venaient d'une divergence testnet /
  venues externes. Les blocages HYPE qui en decoulaient ne pilotent plus le
  mainnet paper.

Commandes utiles:

Deploiement live hybride A/C live + B mainnet paper:

```bash
./deploy.sh --start --mode live --config config/trident.toml --without-funding
```

Deploiement dry-run complet avec le nouveau Pod B:

```bash
./deploy.sh --start --mode dry-run --config config/trident.toml --fresh-start
```

Couper le nouveau Pod B HIP-4:

```bash
./deploy.sh --start --mode dry-run --config config/trident.toml --without-pod-b
```

```bash
uv run python -m app.live.hip4_outcome_runner \
  --config config/hip4_outcome_mainnet_paper.toml \
  --mode paper
```

```bash
uv run python -m app.live.hip4_outcome_runner \
  --config config/hip4_outcome_mainnet_paper.toml \
  --mode paper \
  --once
```

```bash
uv run python -m app.backtest.hip4_outcome_replay \
  --profile mainnet_paper \
  --output logs/hip4_outcome_mainnet_paper/replay_latest.json
```

Replay observer mainnet:

```bash
uv run python -m app.backtest.hip4_outcome_replay \
  --profile mainnet \
  --output logs/hip4_outcome_mainnet/replay_latest.json
```

Review post-fetch mainnet paper / mainnet observer / archives:

```bash
uv run python -m app.backtest.hip4_outcome_run_review \
  --output-json server-data/replay_reports/hip4_outcome_run_review_latest.json \
  --output-md server-data/replay_reports/hip4_outcome_run_review_latest.md
```

Note: `scripts/fetch_trident_data.sh` lance maintenant cette review automatiquement via
`scripts/trident_dry_run_review.sh` quand les logs HIP-4 ont ete rapatries.
Le fetch rapatrie les dossiers HIP-4 entiers (`mainnet_paper`, `mainnet`,
`testnet`, `paper`), et la review inclut `market_observations.jsonl` dans
`hip4_outcome_run_review_latest.{json,md}`:
comptes par classe HIP-4, support status, raisons, underlyings, books observes,
`priceBucket` et `namedOutcome`.
Le rapport inclut une simulation de candidats guardrails: impact PnL/PF/Brier apres
exclusion, verdict `keep/watch/park/kill`, et separation entre slices entry-time
actionnables et categories de pertes post-trade.
Les candidats guardrail testnet HYPE sont archives; en mainnet paper, aucune
slice HYPE n'est bloquee tant qu'une review mainnet-paper ne prouve pas un
predicat entry-time actionnable.

Verification serveur apres deploiement:

```bash
ssh trident-hetzner "cd /opt/trident && curl -fsS http://127.0.0.1:3000/api/hip4-outcome | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get(\"mode\"), d.get(\"blocked_opportunity_slices\"))'"
```

Attendu:

```text
paper []
```

Testnet technique:

- Ne plus l'utiliser comme preuve de performance.
- Le conserver seulement pour regression d'architecture ou preflight ponctuel.
- Les variables `HIP4_OUTCOME_MODE=testnet` et
  `HIP4_OUTCOME_ALLOW_TESTNET_ORDERS=true` ne doivent pas etre actives dans le
  dry-run mainnet paper.

Preparation execution mainnet future:

- Accumuler d'abord les logs mainnet paper via le deploiement standard.
- Rejouer `logs/hip4_outcome_mainnet_paper/opportunities.csv` avec
  `--profile mainnet_paper`.
- Comparer mainnet paper et mainnet observer historique: edge decay, spreads,
  profondeur, fills theoriques, reference prices, horaires d'expiry.
- Aucune execution mainnet ne doit etre ajoutee sans nouveau mode explicite
  `mainnet`, credentials mainnet dedies, preflight separe, caps tiny-size et
  confirmation manuelle.

Ce qui manque encore pour se rapprocher d'un bot type OpenClaw performant:

- WebSocket ou streaming plus bas-latence pour mids et order books.
- Statistiques par expiry apres settlement reel/estime.
- Mesure slippage/fill theorique mainnet paper, puis eventuel preflight tiny
  size separe si une execution mainnet est envisagee.
- Plus de donnees sur marches 5m/15m avant calibration.
- Une politique de sizing dynamique uniquement apres preuve mainnet paper.
- Une calibration probabiliste serieuse avant tout Kelly ou ML.
- Une base de snapshots complete pour rejouer decisions, books, references, fills, settlements et edge decay.
- Un mode mainnet execution explicite et separe de `paper`, seulement apres
  validation mainnet paper.
- Gestion de sortie anticipee HIP-4:
  - les outcomes sont des assets spot-like; sortir avant expiry revient a
    vendre au bid le token YES/NO detenu;
  - implementation cible d'abord `paper` mainnet: mark-to-book au bid,
    comparaison contre une EV hold-to-settlement conservative, exits partiels
    sur profit, exits totaux si le bid surpaie la fair value conservative ou
    si la probabilite se degrade;
  - aucune vente reelle testnet/mainnet sans executor sell dedie,
    reconciliation spot, logs de fills et confirmation operateur.
- Reservation `SHORT_EXPIRY`:
  - le hold-to-settlement produit environ un trade daily par jour et bloque
    souvent le marche via `market_already_open`;
  - les exits anticipes doivent permettre de liberer l'inventaire avant la
    derniere fenetre et de reserver une tranche de budget au moteur
    short-expiry sans augmenter le notional par trade.
- Passive maker / liquidity capture:
  - etudier un mode ALO/GTC autour de la fair probability au lieu de seulement
    traverser le spread;
  - rester en shadow tant que l'adverse selection, les partial fills et le
    risque d'inventaire ne sont pas mesures.
- Extension `priceBucket` / `namedOutcome`:
  - rester observation/paper tant que la resolution n'est pas replayable;
  - prioriser un dataset complet books/references/settlements avant toute
    execution reelle sur ces classes.

### Backlog Data Quality HIP-4 / Dirty Realtime Data

Verdict post "dirty websockets":

- Le diagnostic general est utile: un edge outcome peut etre detruit ou simule
  par des books stale, des references decalees, des snapshots caches ou une
  boucle trop lente.
- La recette Polymarket n'est pas transposable telle quelle a Hyperliquid:
  ne pas lancer `100-300` websockets par feed, ne pas utiliser de seuils fixes
  en cents, et ne pas considerer la latence comme preuve d'edge suffisante.
- Pour TRIDENT, le sujet concerne surtout `Pod B HIP-4 Outcome`. Pod A/Pod C
  utilisent deja un collecteur Hyperliquid shardé et rate-limit; il ne faut pas
  augmenter agressivement les connexions A/C sans preuve et sans respect des
  limites exchange.

Objectif avant toute promotion HIP-4 mainnet:

- Ajouter une couche `data_quality` appelee avant signal/risk/execution.
- Produire un verdict explicite par marche/fenetre:
  `tradable_window=true/false`, `quality_score`, `quality_reasons`.
- D'abord en observation/mainnet paper seulement; aucune execution mainnet
  reelle ne depend de cette couche tant qu'elle n'a pas ete replayee.
- Logger ces champs dans les artifacts HIP-4 et les exposer dans le status/UI.

Metriques minimales a logger:

- `book_age_ms` YES/NO et `max_book_age_ms`.
- `book_pair_skew_ms` entre les books YES et NO d'un meme marche.
- `reference_age_ms` par source quand disponible, et age de la reference
  agregee.
- `loop_total_ms`, `books_ms`, `reference_prices_ms`, `market_observation_ms`.
- `book_update_count_5s/15s` par coin outcome des que le streaming existe.
- `unique_book_count_5s/15s` apres deduplication.
- `price_jump_bps` sur l'underlying et variation absolue de probabilite book
  outcome (`book_probability_delta_abs`).
- `reference_divergence_bps`, sources rejetees, source count, anchor
  Hyperliquid.
- Spread, depth, empty/crossed book, missing bid/ask, et raison de rejet
  `data_quality_*`.

Regles candidates a tester en shadow puis replay:

- Warmup `SHORT_EXPIRY`: commencer la surveillance au moins `15s` avant la
  fenetre tradable, mesurer les `5s` finales, et skip la fenetre si les deux
  legs outcome n'ont pas assez d'updates propres.
- Rejeter une fenetre si un seul jump book/proba/reference depasse un seuil
  calibre en bps ou en probabilite outcome, jamais en cents hard-codes.
- En mode HTTP courant, utiliser l'age `time_ms` des `l2Book` YES/NO, le skew
  YES/NO et la latence de boucle comme proxy de fraicheur.
- En futur mode WebSocket, ignorer le premier tick/snapshot de chaque nouvelle
  connexion tant que la source n'a pas prouve sa fraicheur.
- Marquer une connexion comme stable seulement apres une periode de stabilisation
  d'environ `8s`.
- Stagger les subscriptions/reconnects sur environ `1s` pour eviter de voir le
  meme snapshot cache partout au meme instant.

Redondance prudente:

- Cible initiale: `1` stream primaire + `1` shadow stream pour les outcomes
  critiques ou les fenetres `SHORT_EXPIRY`; monter a `2-3` streams seulement si
  les logs prouvent un gain de fraicheur net.
- Deduplication par `coin`, `time_ms`, best bid/ask et top levels.
- Score `jitter_ema` par connexion: delai inter-message, variance, age max,
  erreurs, reconnects, snapshots identiques repetes.
- Cull les connexions les plus erratiques seulement apres stabilisation, avec
  caps explicites de respawn par minute et par cycle; respecter les limites
  `ws_connects_per_minute`/rate limiter.

Review et criteres de decision:

- Ajouter des buckets de review: PnL, PF, Brier, log-loss, edge decay et fill
  theorique par `quality_score`, `book_age_ms`, `book_pair_skew_ms`,
  `loop_total_ms` et `reference_divergence_bps`.
- Comparer opportunites acceptees vs opportunites qui auraient ete rejetees
  par `data_quality` sur le meme dataset mainnet paper.
- Verifier que la couche ne retire pas seulement quelques winners par hasard:
  exiger un effet stable par jour/expiry/underlying/side avant promotion.
- Mettre a jour `app/backtest/hip4_outcome_run_review.py`,
  `app/trident/hip4_outcome/analysis.py`, `scripts/trident_dry_run_review.sh`
  et `scripts/fetch_trident_data.sh` si de nouveaux fichiers de logs
  `data_quality` sont ajoutes.
- Nouveau pre-requis de promotion mainnet HIP-4: dataset mainnet paper avec
  `data_quality` complet, distributions de latence/fraicheur connues, impact
  replay positif ou neutre sur PF/Brier, et taux de fenetres skip acceptable.

### Backlog LLM Research Sidecar / TradingAgents

Verdict post TradingAgents / multi-agents LLM:

- Le papier et le framework sont interessants comme architecture de recherche:
  plusieurs agents jouent des roles d'analystes, debat bull/bear, trader,
  risk manager et portfolio manager.
- Ce n'est pas une preuve d'edge directement exploitable pour TRIDENT: le cadre
  vise surtout des actions et horizons plus lents, alors que `Pod B HIP-4`
  depend de books outcome, references, expiry proche, latence et calibration.
- Ne pas mettre un LLM dans la boucle d'execution: aucun agent ne doit ouvrir ou
  fermer une position, modifier les caps, changer le mode mainnet, editer une
  config active, ou promouvoir une regle sans replay et confirmation humaine.
- Usage cible: sidecar de review post-fetch et de recherche offline, apres
  collecte mainnet paper, pour accelerer l'analyse sans devenir autorite de
  trading.

Architecture cible:

- `DataQualityAnalyst`: lit `latency_stats.csv`, books, references, age/skew,
  divergences et futurs champs `data_quality`; propose des anomalies testables.
- `LossReviewAnalyst`: classe les pertes par stale book, spread, reference
  divergence, late expiry reversal, insufficient depth, model overconfidence,
  market already open ou sortie anticipee mal calibree.
- `BullResearcher` et `BearResearcher`: debattent une hypothese de guardrail ou
  de sizing, puis formulent un predicat entry-time concret et replayable.
- `RiskReviewer`: verifie caps, drawdown, exposition par underlying, slippage,
  sample size, biais de selection et impact sur Pod A/Pod C.
- `ReplayPlanner`: produit les commandes de replay/review a lancer et les
  slices minimales a comparer; ne change pas le code automatiquement.
- `OperatorReporter`: synthetise le rapport en francais avec verdict
  `go/watch/park/kill`, questions ouvertes et prochaines validations.

Inputs autorises:

- `server-data/logs/hip4_outcome_mainnet_paper/`:
  `opportunities`, `decisions`, `trades`, `settlements`, `latency_stats`,
  `edge_decay`, `short_expiry_features`, `market_observations`,
  `daily_summary`.
- Rapports `server-data/replay_reports/hip4_outcome_run_review_latest.*`.
- Configs HIP-4 et TRIDENT, uniquement en lecture, pour expliquer les seuils.
- Baselines full-bot Pod A/Pod C quand une hypothese pourrait toucher le
  portefeuille global.
- Futurs logs `data_quality` des qu'ils existent.

Outputs attendus:

- Un rapport experimental date dans `server-data/replay_reports/` ou `tmp/`,
  jamais en remplacement d'une baseline officielle sans demande explicite.
- Une liste courte de candidats testables, avec predicat entry-time,
  motivation, risques et commande de replay proposee.
- Des recommandations de collecte ou instrumentation, separees des
  recommandations de strategie.
- Aucun changement live, aucun ordre, aucune promotion et aucun changement de
  caps sans validation humaine.

Regles de promotion:

- Le sidecar LLM peut proposer, jamais decider.
- Tout candidat issu d'un debat LLM doit battre la baseline pertinente via
  replay comparable, avec PnL/PF/Brier/log-loss, sample suffisant et analyse
  par jour/expiry/underlying/side.
- Les conclusions doivent etre deterministes et replayables: si l'agent utilise
  du texte libre, il doit produire aussi des champs structures exploitables par
  les scripts de review.
- Priorite inferieure a `data_quality`: ne pas construire d'agents sophistiques
  tant que les books/references/fills/settlements ne sont pas propres et
  replayables.
- Si ce sidecar devient un service serveur ou ecrit de nouveaux logs a
  rapatrier, mettre a jour les scripts de deploiement, `scripts/fetch_trident_data.sh`
  et `scripts/trident_dry_run_review.sh`.

Cadrage API et couts indicatifs:

- Releve de prix API effectue le `2026-05-23`; a revalider avant tout budget
  engage, car les tarifs et noms de modeles evoluent regulierement.
- Fournisseurs envisageables: OpenAI API, Anthropic Claude API, Gemini API.
  Pas de fine-tuning requis au depart; utiliser une cle API dediee, stockee
  hors repo, et des appels offline/post-fetch uniquement.
- Ne pas envoyer les logs bruts au LLM. Le dossier
  `server-data/logs/hip4_outcome_mainnet_paper/` pese environ `678M` dans le
  workspace courant, principalement `market_observations.jsonl` et
  `decisions.jsonl`; le bon design est de pre-agreger localement puis de donner
  au LLM les rapports, tables agregees et echantillons cibles.
- Prix de reference releves le `2026-05-23`:
  - OpenAI `gpt-5.4-mini`: environ `$0.75/M` tokens input et `$4.50/M` output.
  - OpenAI `gpt-5.4`: environ `$2.50/M` input et `$15/M` output.
  - OpenAI `gpt-5.5`: environ `$5/M` input et `$30/M` output.
  - Anthropic Claude Sonnet 4.6: environ `$3/M` input et `$15/M` output.
  - Gemini 3.1 Flash-Lite: environ `$0.25/M` input et `$1.50/M` output.
- Ordres de grandeur par run quotidien, si les donnees sont agregees avant
  appel LLM:
  - review legere quotidienne (`~50k` input / `~5k` output): environ
    `$0.02/j` sur modele tres cheap, `$0.06/j` sur mini conseille, et
    `$0.20-$0.40/j` sur modele fort.
  - review HIP-4 approfondie (`~200k` input / `~10k` output): environ
    `$0.07/j` sur modele tres cheap, `$0.20/j` sur mini conseille, et
    `$0.65-$1.30/j` sur modele fort.
  - multi-agent `4-6` roles (`~800k-1.2M` input / `~40k-60k` output):
    environ `$0.26-$0.39/j` sur modele tres cheap, `$0.78-$1.17/j` sur mini
    conseille, et `$2.60-$7.80/j` sur modele fort.
  - envoi brut des logs HIP-4 courants (`~170M` tokens input estimes):
    environ `$43/j` sur modele tres cheap, `$128/j` sur mini conseille, et
    `$425-$850+/j` sur modele fort; a eviter.
- Ordres de grandeur mensuels si lance tous les jours:
  - review legere mini: environ `$2/mois`.
  - review approfondie mini: environ `$6/mois`.
  - multi-agent mini quotidien: environ `$25-$35/mois`.
  - multi-agent fort quotidien: environ `$80-$235/mois`.
- Recommandation courante: commencer par un seul rapporteur offline combinant
  `OperatorReporter` et `LossReviewAnalyst` sur un modele mini, puis reserver
  un modele fort uniquement pour relire une courte liste d'hypotheses avant
  replay comparable.

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
5. Loss review: classifier les pertes en stale price, spread, reference divergence,
   late expiry reversal, insufficient depth, model overconfidence.
6. Sentiment/news/LLM: a garder pour plus tard, surtout pour marches narratifs ou macro; priorite faible pour HIP-4 crypto 5m/15m ou la latence et la microstructure dominent.
7. Cross-venue/parity: interessant plus tard si on peut mesurer fills, slippage, inventory risk et settlement mismatch.

Regle:

- Ne pas implementer Kelly/ML/agents tant que Pod B n'a pas accumule un
  historique mainnet paper propre avec settlements exploitables.

## Validations Recentes

Resultat courant `2026-05-19`:

- Historique git relu depuis `2026-05-13`: les commits recents ont surtout
  porte sur le live hybride A/C, le support testnet separe, le remplacement Pod
  B par HIP-4 mainnet paper, la suppression de la piste trigger-liquidity, puis
  le durcissement de la reconciliation live.
- Tests locaux:
  - `python -m py_compile app/live/exchange_position_metrics.py app/live/reconciliation.py app/execution/live.py app/live/pod_a_live_runner.py app/live/pod_c_live_runner.py app/persistence/journal.py`: OK.
  - `.venv/bin/python -m unittest tests.test_live_readiness tests.test_pod_a_live_runner tests.test_journal tests.test_reporting tests.test_health`: `48` tests OK.
- Replay baseline officielle avec repo/config courants:
  - commande: `.venv/bin/python -m app.backtest.full_bot_replay --config config/trident.toml --input server-data/replay_inputs/external_reference_multisource_20260405_20260513_baseline.jsonl --report-output tmp/full_bot_baseline_current_20260519.json --summary-output tmp/full_bot_baseline_current_20260519.md`;
  - `40632` records, `301` timestamps dupliques ignores, memes dates que la
    reference archivee;
  - total `+872.74 USD` vs `+859.83 USD` archive (`+12.91`);
  - Pod A `+793.63` vs `+780.72`, `161` trades vs `155`;
  - Pod B `0.00`, Pod C `+79.11` inchange;
  - delta entierement explique par `6` trades `HYPE trend_pullback_long`
    reintroduits apres rollback du veto HYPE.
- Test serveur reel:
  - rebuild + restart uniquement `pod-a-live` et `pod-c-live` en
    `live/testnet`;
  - position BTC deja ouverte cote Hyperliquid reprise par Pod C;
  - Pod A classe BTC comme position externe connue;
  - `/api/report` remonte Pod C `position_count=1` et
    `total_unrealized_pnl_usd` depuis le status runtime;
  - logs post-restart A/C sans `Traceback`, sans `TypeError`, sans echec de
    reconciliation.
- Fixs valides:
  - journal JSONL compatible avec `Decimal` dans les fills live;
  - payload open positions priorise les valeurs Hyperliquid;
  - close live reduce-only utilise la taille exchange exacte;
  - cartes `Status > Pods` affichent `PnL realise` et `PnL latent`.
- Redeploiement propre `2026-05-21`:
  - ancien journal Pod A archive serveur dans
    `logs/archive/20260521T055645Z_redeploy_base/`;
  - `pod-a-live`, `pod-c-live`, `trident-api`, `hip4-outcome-dry-run` et
    `tradfi-funding-collector` redemarres en `live/testnet --without-funding`,
    `RestartCount=0` au demarrage de verification;
  - `/health`: `status=ok`, `mode=live`, `exchange_network=testnet`,
    `kill_switch_active=false`, version `3f56fd05 (2026-05-19 17:09)`;
  - `/api/state`: Pod A `ready=true`, `live_trading_paused=false`,
    `external_known_positions=["SOL"]`; Pod C `ready=true`,
    `live_trading_paused=false`, `open_positions=["SOL"]`; pas de positions
    inconnues/manquantes, pas d'ordres ouverts inconnus, pas de
    `trigger_orders` orphelins;
  - correction live validee: selection de close fills par timestamp/role
    plausible, conservation des metadonnees d'ordres pour les positions
    ouvertes non presentes dans la sauvegarde courante;
  - logs post-base rapatries: Pod A `negative_holds=0`, dernier close ETH
    `2026-05-21T06:00:18.384Z` apres open `2026-05-21T05:58:00Z`; Pod C
    `negative_holds=0`, dernier close BTC `2026-05-21T06:03:00Z` apres open
    `2026-05-21T05:49:00Z`.

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

```bash
uv run python -m unittest tests.test_risk_gate tests.test_pod_a tests.test_health
```

Resultat courant `2026-05-17`:

- `uv run python -m unittest tests.test_hip4_outcome_pod tests.test_hip4_outcome_analysis tests.test_trident_dry_run_launcher tests.test_health`: `75` tests OK.
- `uv run python -m unittest tests.test_risk_gate`: `27` tests OK; couvre le
  fait que le veto HYPE est charge mais desactive et qu'un
  `HYPE trend_pullback_long` n'est plus rejete par cette regle.
- `uv run python -m unittest tests.test_risk_gate tests.test_pod_a tests.test_health`: `79` tests OK.
- `uv run python -m py_compile app/trident/hip4_outcome/models.py app/trident/hip4_outcome/parser.py app/trident/hip4_outcome/config.py app/trident/hip4_outcome/probability.py app/trident/hip4_outcome/edge.py app/trident/hip4_outcome/runner.py app/trident/hip4_outcome/logging.py app/trident/hip4_outcome/risk.py app/trident/hip4_outcome/analysis.py app/live/hip4_outcome_runner.py app/live/trident_dry_run_launcher.py app/observability/api.py`: OK.
- `bash -n deploy.sh scripts/trident_server.sh scripts/fetch_trident_data.sh scripts/trident_dry_run_review.sh`: OK.
- `config/hip4_outcome_testnet.toml`: testnet conserve `enable_model = false`,
  mais les blocages HYPE et `block_reference_divergence` sont retires
  (`blocked_opportunity_slices = []`, `reference_divergence_underlyings = []`).
- `server-data/replay_reports/hip4_outcome_run_review_latest.md`: review
  mainnet paper en place, statut `collect_more_data`, Brier `0.2695`, samples
  calibration `3/20`.
- `server-data/replay_reports/pod_a_c_shortlist_validation_latest_fetch_20260517.md`:
  rerun latest fetch complet OK, baseline `783.17` (`Pod A 715.59`,
  `Pod C 67.58`) et verdicts watch/rollback mis a jour dans la roadmap.
- `config/trident.toml`: rollback HYPE Pod A applique
  (`hype_trend_pullback_long_targeted.enabled = false`).
- `priceBucket` parse et modele paper/observer couverts; pas d'execution reelle.
- `Named Outcome` et classes inconnues logguees en observation, sans modele ni execution.
- API `/api/hip4-outcome` et alias Pod B conservent le chemin d'integration HIP-4.
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
  - watchers `micro_liquidity_pull_trend_panic` et
    `micro_depth_refill_trend_panic` utiles en research/watch-only.
  - le replay d'integration `2026-04-23` garde la baseline full-bot inchangee;
    meme Pod B explicitement enabled produit `0` signal sur le fetch comparable.
- Squeeze / breakout via Pod B:
  - tests cibles a `0` trade ou non additifs.
- Shorts globaux Pod A:
  - rejetes.
  - `short_49_pivot_low_break_strong_flow` reste shadow seulement:
    standalone positif (`+153.73`, `216` trades), mais le full-bot promu degrade
    la reference (`571.67` total vs `859.83` officiel) et augmente fortement le
    nombre de trades Pod A (`408` vs `155`).
  - toute these short future doit battre le full-bot, pas seulement le standalone.
- Funding / liq / open interest comme pod principal:
  - pas de preuve replay comparable suffisante.
- Mean reversion generaliste:
  - recherche seulement, pas de pod live.
  - les recherches Hyperliquid top30/top50 ont transforme l'idee en verdicts:
    `range_mean_reversion` et `funding_reversion` sont surtout `kill/park`;
    quelques symboles restent candidats research, sans promotion transversale.
- Pod C `silver routing grace`, `gold routing grace`, equity/fx:
  - pas de promotion avec les donnees actuelles.
- Shorts Pod C:
  - rejeter les familles broad/oil/equity/silver/gold testees le `2026-05-17`.
  - seuls `pc_short_21_fx_breakdown_flow` (`+22.34`, `2` trades) et
    `pc_short_06_index_donchian_break_60` (`+0.93`, `4` trades) restent watch
    trop petits pour promotion.

### Garde En Watch / Research Seulement

- Microstructure `depth_refill_continuation` et
  `liquidity_pull_continuation`: watchers/research, pas execution. Le holdout
  `2026-04-23` reste positif (`depth_refill` `+1.3093 bps`, hit `0.5377`;
  `liquidity_pull` `+0.9681 bps`, hit `0.5504`) mais pas encore live gating.
- `funding_reversion`, `range_mean_reversion`, `stoch_cci_reversion`: research
  seulement; top50 ne justifie qu'un upgrade des pods existants, pas un nouveau
  pod mean-reversion.
- LTC/ZRO overextension: ZRO reste interessant en research
  `ema50_overextension_reversion`, LTC reste plutot `vwap_reclaim`/Pod A; aucun
  veto cible valide a promouvoir.
- `absorption` et `book_churn_flow_veto`: park/reformuler avant promotion.

## Roadmap Courante

### 1. Lancer Le Mode Hybride A/C Live + B Paper

- Deployer les changements via `./deploy.sh --start --mode live --without-funding`
  depuis le poste local, apres validation des credentials et du preflight.
- Lancer le bot complet sans `--without-pod-b`: Pod B doit rester HIP-4 paper,
  pas le legacy `pod-b-live`.
- Verifier que les services actifs sont `pod-a-live`, `pod-c-live` et
  `hip4-outcome-dry-run`.
- Verifier que le config runtime est
  `config/hip4_outcome_mainnet_paper.toml`, que le mode expose est `paper`, et
  que `allow_testnet_orders = false`.
- Verifier que `runtime/hyperliquid_rate_limits.json` est persistant entre les
  runs et que les compteurs private info / exchange action ne montrent pas de
  breaker ouvert.
- Verifier que `/api/hip4-outcome.blocked_opportunity_slices` est vide.
- Suivre `/hip4-outcome`:
  - edges par type
  - short-expiry features
  - edge decay
  - settlements estimes
  - PnL paper par underlying
  - bloc mainnet paper: markets, references, opportunities, replay
  - observations de classes HIP-4 non supportees et `priceBucket`
- Suivre aussi `/dashboard` et `/api/report`: Pod B doit pointer vers `pod_kind = hip4_outcome_edge_pod`.
- Ne pas conclure sur un seul signal; attendre plusieurs expiries.

### 2. Analyser Les Runs Pod B

Prerequis:

- fetch serveur complet apres quelques heures.
- logs mainnet paper et mainnet observer historique: `opportunities`,
  `decisions`, `trades`, `settlements`, `edge_decay`,
  `short_expiry_features`, `market_observations`, `daily_summary`.
- statut API et UI coherents.

Action:

- separer les vrais edges des artefacts de donnees: divergence de reference,
  book stale, absence de profondeur, settlement mismatch estime.
- calculer PnL net fees par coin, cote, type d'edge, horizon d'expiry et heure.
- mesurer win rate, profit factor, drawdown, edge decay et fill quality. Le win rate seul ne suffit pas.
- comparer mainnet paper vs mainnet observer: reference price, spread, depth,
  edge decay, et frequence des signaux.
- produire `hip4_outcome_run_review_latest.{json,md}` apres chaque fetch serveur complet.
- utiliser la section `Guardrail Candidates` pour choisir seulement des
  restrictions entry-time prouvees en mainnet paper.
- Ne pas ajouter de slice tant que la fenetre mainnet paper n'a pas confirme un
  Brier `<= 0.23` avec un volume encore exploitable.

### 3. Calibration Avant Sizing Dynamique

- Brier score, log-loss, buckets de calibration, loss review et guardrail
  simulation sont en place.
- Continuer la collecte: la derniere review mainnet paper depasse le minimum
  brut de settlements mais reste bloquee par PF `0.9106/1.15`, Brier
  `0.2405 > 0.23`, et absence de profil mainnet observer comparable.
- Faire du walk-forward par jour/expiry plutot que valider sur une seule fenetre.
- N'autoriser fractional Kelly ou XGBoost qu'apres historique suffisant et stable.
- Garder `max_position_usdc`, `max_total_outcome_exposure_usdc` et `max_per_underlying_outcome_exposure_usdc` comme hard caps meme si Kelly propose plus.

### 3b. Tester Les Sorties Anticipees HIP-4

- Objectif: sortir plus tot que settlement quand le carnet paie deja assez ou
  quand l'edge a disparu, afin de reduire la duree d'inventaire et d'augmenter
  le turnover paper sans augmenter le notional.
- Priorite d'implementation: `mainnet_paper` uniquement, avec journal dedie
  `early_exits.csv`.
- En parallele, garder des experiences shadow paper-only, sans changer les
  positions actives:
  - `shadow_exit_policies.csv`: hold-to-settlement, take-profit partiel
    +25/+35/+50%, sortie EV conservative, sortie defensive, sortie derniere
    fenetre 5/10/15 minutes;
  - `shadow_sizing.csv`: fractional Kelly virtuel/cappe pour estimer le sizing
    avant de toucher au notional actif;
  - `shadow_maker_quotes.csv`: quotes passive/maker virtuelles pour mesurer les
    cas ou le spread pourrait etre capture sans envoyer d'ordre reel.
- Regles initiales:
  - sortie partielle au bid sur ROI positif materialise;
  - sortie totale si le bid est superieur a l'EV conservative de hold;
  - sortie totale defensive si la probabilite de win conservative tombe sous
    seuil et que le bid recupere encore assez de valeur;
  - cooldown de re-entry sur le meme marche apres sortie totale pour eviter le
    churn.
- Comparer apres plusieurs jours: hold-to-settlement historique vs early-exit
  paper, PnL, max drawdown, turnover, Brier/calibration et opportunites
  `SHORT_EXPIRY` debloquees.

### 4. Ameliorer La Latence HIP-4 Seulement Si Necessaire

Priorite apres plusieurs runs mainnet paper:

- remplacer le polling critique par streaming/WS si l'edge decay montre que les signaux disparaissent trop vite.
- ajouter book cache / allMids cache pour eviter de dependre de REST a chaque boucle.
- mesurer la latence dans `latency_stats.csv` avant d'optimiser.

### 5. Garder Pod A / Pod C Stables

- Pas de nouveau sweep massif tant que le Pod B HIP-4 est en exploration
  mainnet paper.
- Rejouer la baseline officielle seulement quand le fetch serveur change ou avant une promotion.
- Toute divergence live/replay doit etre analysee avec `collector + maintenance_refresh`.
- Shorts Pod A `2026-05-16`: `short_49` reste shadow, pas promotion, car le
  standalone positif ne survit pas au full-bot promu.
- Shorts Pod C `2026-05-17`: rejeter les familles larges; seulement deux
  micro-candidats FX/index restent watch trop petits.
- Latest shortlist full fetch `2026-05-17`:
  `server-data/replay_reports/pod_a_c_shortlist_validation_latest_fetch_20260517.md`.
- Resultats shortlist OOS `2026-05-05`:
  - Pod A HYPE veto: `reject`; rollback applique dans `config/trident.toml`
    (`enabled = false`) apres confirmation latest fetch (`-14.72`, `13`
    vetoes).
  - Pod A MTF vetoes: `reject` sur l'OOS recente mais `keep` sur latest fetch
    (`+32.97`, `94` vetoes); conserver actifs, ne pas etendre.
  - Pod A BTC/XRP overextension: `no_effect` sur OOS `2026-05-05`, mais
    `keep` sur latest fetch (`+26.20`, `3` vetoes); conserver actifs.
  - Pod C relaxed cluster-aware off: `reject` sur OOS et encore plus net sur
    latest fetch (`-353.32`, `408` trades); conserver la selectivite.
  - Pod C silver veto: `reject` sur OOS tres petit (`1` veto), mais `keep` sur
    latest fetch (`+18.52`, `13` vetoes); conserver actif.
  - Pod C gold vetoes: `gold_soft_extension_veto` watch tiny-sample
    (`+6.26`, `1` veto), `gold_strong_neutral_veto` sans effet,
    `gold_medium_neutral_veto` rejete (`-32.78`, `3` vetoes).
  - Pod C signal drought recent: `2026-05-02`, `2026-05-03` et `2026-05-05` sans signal; `2026-05-04` a `6` signaux mais `0` acceptes. Pas d'anomalie mecanique prouvee, plutot selectivite/regime.

### 6. Deploiement / Rollback

- S'assurer que le serveur utilise `config/trident.toml`.
- Chemin live hybride attendu: `./deploy.sh --start --mode live --without-funding`
  lance `pod-a-live` et `pod-c-live` en mode `live`, plus
  `hip4-outcome-dry-run` en `paper`.
- Le script serveur force `HIP4_OUTCOME_CONFIG=config/hip4_outcome_mainnet_paper.toml`,
  `HIP4_OUTCOME_MODE=paper` et `HIP4_OUTCOME_ALLOW_TESTNET_ORDERS=false` quand
  `TRIDENT_MODE=live`.
- Verifier que HIP-4 reste le Pod B paper, sans mode execution mainnet et sans
  `HIP4_OUTCOME_ALLOW_TESTNET_ORDERS=true`.

Critères de passage A/C en mainnet tiny-size:

- Fenetre minimale: `72h` propres apres le redeploiement stable du
  `2026-05-21T06:07:35Z`; prochaine reevaluation cible:
  `2026-05-24T06:07:35Z`.
- `pod-a-live`, `pod-c-live` et `trident-api` up en continu, sans crash loop et
  avec `RestartCount=0` depuis le dernier redeploiement, sauf restart manuel
  explicitement documente pour test de recovery.
- Runtime A/C frais en continu dans `/api/state` et `/api/report`:
  `runtime status fresh`, `healthy=true`, `live_trading_paused=false`.
- Reconciliation A/C propre: `ready=true`, `reasons=[]`,
  `unknown_exchange_positions=[]`, `missing_exchange_positions=[]`,
  `side_mismatches=[]`, `open_orders=[]` ou uniquement des orders connus par le
  state store.
- Au moins deux redemarrages/reconciliations propres avec positions exchange
  existantes, dont un cas ou Pod A voit une position connue par Pod C comme
  `external_known_positions`.
- Au moins un cycle reel `open -> close` sur Pod A et un cycle reel
  `open -> close` sur Pod C en testnet apres le fix `triggerPx`, avec state
  local persiste et PnL/fills coherents dans le pod et le superviseur.
- Les erreurs transitoires Hyperliquid testnet (`502`, websocket reconnect,
  timeout) doivent seulement pauser les entrees puis revenir a `ready=true`;
  aucun fill reel ne doit rester sans state local, et aucun ordre protecteur
  requis ne doit echouer sans emergency close.
- Fetch serveur post-burn-in complet: `/health`, `/api/state`, `/api/report`,
  `/api/metrics`, logs Docker, runtime states, snapshots et journals recuperes;
  review sans `Traceback`, sans `Decimal is not JSON serializable`, sans
  divergence A/C live vs state store.
- Rejouer la baseline/replay seulement si le fetch serveur ou la config ont
  change; sinon documenter que la promotion mainnet ne modifie pas la strategie,
  seulement le reseau et les caps.
- Mainnet uniquement tiny-size et manuel: config `config/trident.toml`,
  `--mode live --network mainnet --without-funding`, caps de notional live
  verifies, preflight Pod A et Pod C separes OK, confirmation operateur requise.
- Pod B HIP-4 reste `mainnet paper`; aucune execution HIP-4 mainnet n'est
  incluse dans cette promotion A/C.
- Garde-fous rate limit ajoutes pour le live A/C:
  - lectures privees HL cadencees par `private_info_requests_per_minute`;
  - actions `order/cancel` cadencees par `live_order_actions_per_minute`;
  - breaker partage sur signal 429/rate-limit.
- Garder un rollback simple:
  - couper le Pod B HIP-4 avec `--without-pod-b`
  - `--without-hip4-outcome` reste accepte comme alias historique
  - couper seulement le sidecar observateur mainnet avec `--without-hip4-mainnet-observer`
  - ou laisser `HIP4_OUTCOME_MODE=paper`

## Regles De Promotion

- Une idee validee seulement en candles/research ne passe pas en prod.
- Une idee positive en standalone mais negative en full-bot ne passe pas en prod.
- Une idee HIP-4 mainnet paper ne passe pas en execution mainnet sans dataset
  mainnet replayable, calibration, dry-run propre, preflight separe, caps
  tiny-size et confirmation manuelle.
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
