# TRIDENT — Statut

## Tableau de pilotage

| Etape | Avancement | Prochain milestone |
|-------|------------|--------------------|
| 0. Cadrage et branchement | 100% | Rien, etape fermee |
| 1. Superviseur + ownership | 100% | Rien, etape fermee |
| 2. Regime allocator deterministe | 100% | Rien, etape fermee |
| 3. Capital allocator + cash mode | 100% | Rien, etape fermee |
| 4. Pod A minimal | 100% | Rien, etape fermee |
| 4bis. Pod A complet / t-bot+ | 99% | Valider sur une plage dry-run live plus longue |
| 5. Pod B breakout directionnel | 100% | Continuer le tuning durable et valider sur une plage dry-run plus longue |
| 5bis. Routing dynamique symbols / ownership | 100% | Rien, etape fermee |
| 6. Reporting par pod | 100% | Rien, etape fermee |
| 7. Research Pod pour Pod C | 100% | Rien, etape fermee |
| 8. Pod C minimal | 100% | Rien, etape fermee |
| 9. Hardening deployment | 100% | Rien, etape fermee |
| 10. Passage live progressif | 45% | Dry-run serveur long avec Pod A + nouveau Pod B directionnel |
| 11. Pistes futures Hydra revisitees | 35% | Continuer les runs offline funding/liq |
| 12. Pod C v2 Tradfi Trend | 45% | Refondre l'allocation Tradfi par cluster, puis valider en replay |

## Journal condense

### 2026-04-14

- **Fetch local rendu plus robuste**:
  - `scripts/fetch_trident_data.sh` tente maintenant d'installer `rsync` localement si le binaire est absent
  - gestionnaires supportes: `dnf`, `yum`, `apt-get`, `apk`, `pacman`, `zypper`, `brew`
  - les artefacts Hydra research `docs/pod_*_research_latest.*` sont maintenant traites comme optionnels au fetch
- **Review dry-run durcie**:
  - `scripts/trident_dry_run_review.sh` ne valide plus seulement l'absence de crash
  - la review locale tient maintenant compte des erreurs collecteur, de la fraicheur strategique et des economics par pod
  - une incoherence d'observabilite Pod B entre runtime/report et metrics a ete corrigee
- **Pod B filtre strict de continuation integre**:
  - un filtre strict a ete branche dans `app/trident/pod_b/service.py` pour les setups `vol_expansion_long`
  - flag de config: `pod_b.bis_strict_continuation_filter_enabled`
  - config dry-run actuelle: activee
  - replay dedie sur les snapshots fetched:
    - filtre `off`: `93` signaux, `65` trades, `-31.68 USD`, `66.05 USD` de max drawdown
    - filtre `on`: `41` signaux, `36` trades, `+32.53 USD`, `47.79 USD` de max drawdown
  - artefact: `server-data/reviews/pod_b_strict_filter_integration_20260414.json`
- **Pod C garde sa place en dry-run, sans etre coupe**:
  - `Pod C` n'a pas ete desactive
  - la config a seulement ete rendue plus conservative:
    - `min_confidence = 0.66`
    - `blocked_symbols = ["XYZ:GOLD"]`

### 2026-04-12

- **Remplacement complet de Pod B**:
  - l'ancien Pod B maker/range a ete retire des chemins runtime, replay et observabilite
  - `Pod B` designe maintenant officiellement le moteur directionnel breakout/vol-expansion crypto
  - runtime status: `logs/pod_b_live_status.json`
  - live runner: `app/live/pod_b_live_runner.py`
  - replay standalone: `app/backtest/pod_b_runner.py`
  - UI Pod B alignee sur le contrat directionnel de Pod A / Pod C
- **Resultat replay fetched retenu apres integration propre du nouveau Pod B**:
  - fenetre `2026-04-05 -> 2026-04-12`
  - total bot `+221.68 USD`
  - `Pod A = +202.68 USD`
  - `Pod B = +19.00 USD`
  - `Pod C = 0 USD`
  - version retenue: moins agressive que certaines variantes experimentales, mais plus propre sur le routing, l'observabilite et le deploiement
- **Fix de deploiement / fetch Pod B**:
  - le serveur lancait encore l'ancien module supprime `app.trident.pod_b.paper_live_runner`
  - `docker-compose.trident.yml` lance maintenant `app.live.pod_b_live_runner`
  - `scripts/trident_server.sh` ne depend plus du vieux runtime `runtime/passivbot/live.json`
  - `scripts/fetch_trident_data.sh` rapatrie maintenant le vrai runtime Pod B depuis `logs/pod_b_live_status.json`
- **Observabilite Pod B clarifiee**:
  - le libelle `planned` etait trompeur quand seul le fallback superviseur etait visible
  - l'UI affiche maintenant `Supervisor fallback`
  - dans ce cas, Pod B n'est plus compte comme healthy
  - cela a permis de distinguer un vrai probleme runtime d'un simple etat planifie
- **Régimes par cluster visibles sur la page principale**:
  - le dashboard `Status` affiche maintenant `Crypto` + une carte par cluster Tradfi
  - chaque carte montre le regime courant, le budget cible et le nombre de symbols observes / tradables
  - on voit donc directement un cas du type `crypto DeadZone / index TrendExpansion`
- **Fix de lisibilite du routing**:
  - un symbole laisse a `none` n'affiche plus un faux `capacity_trim:pod_a` quand aucun pod n'atteint en realite le seuil d'assignation
  - le routeur expose maintenant explicitement un motif du type `below_assign_threshold_all_candidates:*`
- **Extension prudente de l'univers crypto observe**:
  - ajout de la vague 1 de coins crypto liquides:
    - `ZEC`
    - `TAO`
    - `ENA`
    - `TON`
    - `BCH`
  - objectif: elargir l'observation sans basculer d'un coup sur des coins plus narratifs / plus bruyants
- **Waves d'extension actuellement retenues**:
  - crypto wave 1 active:
    - `ZEC`
    - `TAO`
    - `ENA`
    - `TON`
    - `BCH`
  - crypto wave 2 candidate:
    - `WLD`
    - `XMR`
    - `CRV`
    - `UNI`
    - `DOT`
  - non-crypto wave 1 deja couverte par le scope builder-dex courant:
    - `XYZ:CL`
    - `XYZ:BRENTOIL`
    - `XYZ:SP500`
    - `XYZ:XYZ100`
    - `XYZ:SILVER`
    - `XYZ:GOLD`
  - non-crypto wave 2 candidate:
    - `XYZ:JPY`
    - `XYZ:CRCL`
    - `XYZ:TSLA`
    - `XYZ:NVDA`
  - non-crypto wave 3 candidate:
    - `XYZ:EWY`
    - `XYZ:EUR`
    - `XYZ:NATGAS`
    - `XYZ:INTC`
    - `XYZ:HOOD`

### 2026-04-11

- **Bug fix: ClosedTrade perdait les champs trailing/break-even**:
  - le dataclass `ClosedTrade` ne contenait pas `trailing_activation_bps`, `trailing_distance_bps`, `break_even_trigger_bps`
  - a la fermeture d'un trade, ces valeurs etaient perdues → l'API affichait "Non configure" meme pour des trades qui avaient un trailing actif
  - fix: ajout des 3 champs a `ClosedTrade` et copie dans `close_position()`
  - fichier: `app/portfolio/directional_state.py`

- **Bug fix: positions ouvertes sans market data (pas de prix courant / PnL)**:
  - le live runner dependait exclusivement du flux WebSocket pour les prix
  - si un shard WS perdait la connexion ou qu'un symbol cessait d'emettre, la position devenait invisible: pas de prix, pas de PnL, et aucun exit check (stop, trailing, time-stop) n'etait evalue
  - fix: ajout d'un fallback REST via l'endpoint `allMids` de Hyperliquid
  - a chaque record, le runner detecte les positions ouvertes sans snapshot WS et injecte des snapshots synthetiques via l'API REST
  - fichiers: `app/hyperliquid/info_client.py` (`fetch_all_mids()`), `app/live/pod_a_live_runner.py` (`_backfill_missing_position_snapshots()`)

### 2026-04-10

- **Correction de cap Pod C / Tradfi**:
  - le premier passage "regime par cluster" a bien ouvert le transport et l'observabilite, mais ne repond pas encore correctement au probleme metier
  - l'agregat unique `tradfi_regime` est juge trop grossier: il ne sait pas representer `gold fort / index faible / oil fort` en meme temps
  - la cible retenue devient une allocation Tradfi par cluster, pas un simple override global de `pod_c`
  - `pod_c.target_pct` devra devenir la somme des budgets Tradfi des clusters actifs
  - `cash` devra etre recalcule comme residuel global apres toutes les allocations reelles
  - les prochains changements doivent aussi unifier le fallback des clusters absents entre allocator, router et supervisor
  - l'Etape 12 est revue a `45%`: le plumbing cluster-aware est utile, mais le modele d'allocation doit etre refait

- **Migration Pod C vers builder-dex HL**:
  - `Pod C` ne repose plus sur un panier spot/xStock type `SPY` / `GLD` / `QQQ`
  - le scope runtime courant est maintenant le top builder-dex `xyz` par liquidite validee localement:
    - `XYZ:CL`
    - `XYZ:BRENTOIL`
    - `XYZ:SP500`
    - `XYZ:XYZ100`
    - `XYZ:SILVER`
    - `XYZ:GOLD`
    - `XYZ:JPY`
    - `XYZ:TSLA`
    - `XYZ:NVDA`
    - `XYZ:CRCL`
  - les symbols sont conserves en forme canonique uppercase dans la config, puis resolves dex-aware au runtime:
    - websocket: `XYZ:SP500` -> `xyz:SP500`
    - REST `allMids`: appel par dex
    - REST `metaAndAssetCtxs`: appel par dex
  - les caps de levier live et le funding de `Pod C` sont donc recuperes sur les vrais builder-dex markets, pas sur le perp global

- **Decision produit Pod C**: la trajectoire `Squeeze Breakout` est abandonnee au profit d'un pod directionnel Tradfi HL dans le slot `Pod C`.
  - le systeme reste a 3 pods: pas de `Pod D`
  - `Pod C` devient la place reservee a un moteur inspire de `Pod A`, mais dedie aux instruments Tradfi HL
  - phase 1 cible `indices` / `commodities`; les single stocks restent hors scope initial
  - prerequis de validation: snapshots minute TRIDENT `l2Book + trades`, eventuellement enrichis `assetCtx`
  - les candles HL seules restent exclues de la validation, comme pour les autres pods directionnels
- **Regime par cluster implemente** (25 fichiers, +508/-200 lignes):
  - chaque cluster leader (BTC→crypto, `XYZ:SP500`→index, `XYZ:GOLD`→gold, `XYZ:SILVER`→silver, `XYZ:CL`→oil, `XYZ:JPY`→fx) produit un `RegimeSnapshot` independant
  - `crypto_regime` (issu de BTC) continue de piloter Pod A et Pod B
  - `tradfi_regime` (agregation conservative des regimes non-crypto) pilote Pod C
  - agregation conservative: en cas de divergence entre clusters tradfi, le regime le plus defensif est retenu (dead_zone > panic_squeeze > range_auction > cash > trend_expansion)
  - allocations tradfi dediees dans `config/trident.toml` (section `[trident.allocations_tradfi.*]`)
  - le routeur utilise le regime du cluster du symbol pour le scoring Pod C (`_resolve_tradfi_regime`)
  - le `capital_allocator` derive l'allocation Pod C depuis `allocations_tradfi` au lieu de `allocations`
  - `SupervisorState` expose `cluster_regimes`, `cluster_regime_snapshots`, `cluster_pending_regimes`, `cluster_pending_counts`
  - `SnapshotRecord` transporte les `cluster_regime_snapshots` pour les backtests
  - tous les runners (backtest, live, research, observability) sont branches
  - 142/142 tests passent (4 pre-existants corriges au passage)
- **Transition historique du runtime Pod B**:
  - a cette date, le runtime Pod B quittait deja sa config dediee et passait sous le superviseur partage
  - cette etape a ensuite abouti au remplacement complet du moteur maker par le Pod B directionnel actuel
- **docker-compose.trident.yml**: `--config-path` → `--config` pour pod-b-live
- **deploy.sh**: build avec les profiles Docker (Pod B/C etaient pas rebuild
  quand leurs profiles etaient actives)
- **trident_server.sh**: ajout `--force-recreate` au `start` pour garantir que
  les containers repartent avec la derniere config/image
- **Bug fix Pod C live runner**: le container Pod C creait un superviseur isole
  (`pod_a=False, pod_b=False`) qui lui attribuait tous les coins et ignorait les
  allocations de regime (toutes a 0%). En live il tradait donc sur 14 coins en
  parallele de Pod A/B → conflits d'ownership potentiels et PnL negatif constant
  (-6 USD en 3 jours, 223 trades perdants).
  - fix: le runner utilise desormais la config telle quelle, comme Pod A.
    A cette date, le superviseur voyait les 3 pods mais la config active donnait encore 0% a Pod C.
  - fichier: `app/live/pod_c_live_runner.py` (suppression du `replace()` qui
    desactivait Pod A/B)
- **Journals reinitialises au demarrage**: `JsonlJournal` accepte `truncate=True`
  active dans les 3 live runners (Pod A, Pod B, Pod C). Avant, les journaux
  JSONL s'accumulaient entre les redeploys, donnant un PnL cumule trompeur
  pour Pod B et Pod C.
  - fichier: `app/persistence/journal.py`
- **Pod A max_leverage passe de 5x a 10x**: 131/132 trades etaient deja au
  plafond de 5x. Le sizing risk-based demande typiquement 15-17x mais etait
  bride. A 10x le PnL double (+429 USD vs +215 USD sur 6 jours) pour un
  drawdown proportionnel (43 vs 21 USD).
  - sweep valide: 5x/10x/15x/20x — le profil risque/rendement est lineaire
  - 10x reste bien en-dessous des limites HL (min 10x pour les alts)
- full bot replay sur 6 jours (5-10 avril) avec config actuelle:
  - Pod A: +429 USD, 130 trades, 62.3% win, drawdown 43 USD
  - Pod B: -0.61 USD, 1349 fills (Avellaneda-Stoikov engine)
  - Pod C: 0 USD, 0 trades (historique, avant la migration builder-dex et l'activation de son scope actuel)
  - total: +428.46 USD
- **Versioning automatique**: version git (hash + date) affichee dans le dashboard
  et exposee dans `/health`. Module `app/version.py`, suffixe `-dirty` si non committe.

### 2026-04-08 (suite)

- Pod B reecrit avec modele Avellaneda-Stoikov:
  - **fair value EMA**: quotes centrees sur un prix estime, pas le spot
  - **inventory skew**: mid-price deplace en fonction de l'inventaire (long → mid baisse pour attirer des sells)
  - **spread volatilite-adaptatif**: plus large quand volatile, plus serre quand calme
  - **trend guard**: arrete de quoter le cote perdant quand prix diverge du fair value
  - **grille multi-niveaux** configurable (1-N niveaux avec espacement geometrique)
  - config: `paper_fair_value_ema_alpha`, `paper_inventory_skew_intensity`, `paper_volatility_spread_multiplier`, `paper_grid_levels`
  - `max_allocation_pct` reduit de 0.70 a 0.40 pour liberer des coins a Pod A
  - `max_inventory_skew_pct` reduit de 1.0 a 0.25 pour empecher l'accumulation directionnelle
- constat sur backtest historique candles HL (annule):
  - l'API HL ne fournit que des candles OHLCV, pas de microstructure L2
  - le bot est calibre pour des snapshots minute avec spread/depth/flow
  - les candles 1h ne sont pas representatives: test sur avril 5-8 donne -32 USD vs +200 USD sur donnees L2
  - conclusion: backtest candles non viable, seules les donnees L2 live sont fiables

### 2026-04-08

- replay "bot complet" ajoute pour rejouer le systeme A/B/C dans des conditions proches du dry-run:
  - [full_bot_replay.py](/workspaces/trident/app/backtest/full_bot_replay.py)
- historique comparatif ajoute:
  - [history.jsonl](/workspaces/trident/data/replay_reports/full_bot/history.jsonl)
- sweep d'experiences radicales ajoute:
  - [full_bot_experiment_sweep.py](/workspaces/trident/app/backtest/full_bot_experiment_sweep.py)
  - [full_bot_experiment_sweep_20260407T214546Z.json](/workspaces/trident/data/replay_reports/full_bot_sweeps/full_bot_experiment_sweep_20260407T214546Z.json)
- constat structurel retenu:
  - `Pod A` porte l'essentiel du PnL
  - `Pod B` ajoute peu mais reste utile en complement
  - `Pod C` detruit de la valeur sur la fenetre validee
- profil principal bascule sur:
  - `Pod A + Pod B`
  - `Pod C` coupe a cette date
- validation replay de la config active:
  - [full_bot_backtest_20260407T214946Z.json](/workspaces/trident/data/replay_reports/full_bot/full_bot_backtest_20260407T214946Z.json)
  - total realise `+27.0668 USD`
  - `467` reattributions
- Hydra explicitement maintenu hors run principal:
  - funding / liq / OI restent en piste research offline

### 2026-04-07

- coherence superviseur/runtime/API renforcee
- univers observe derive du live avec `tradable_pool` et raisons de rejet
- clusters de marche `crypto/index/gold` branches
- routage dynamique `global + local` finalise:
  - `local_regime_by_symbol`
  - transitions locales
  - cooldown de reattribution
  - overrides statiques/runtime
  - UI System explicable
- validation reelle de `5bis` sur `server-data/live_snapshots/2026-04-05..07`
- outil ajoute:
  - [routing_replay.py](/workspaces/trident/app/backtest/routing_replay.py)
- artefacts generes:
  - [routing_replay_current_2026-04-05_2026-04-07.json](/workspaces/trident/data/replay_reports/routing_replay_current_2026-04-05_2026-04-07.json)
  - [routing_replay_tighter_2026-04-05_2026-04-07.json](/workspaces/trident/data/replay_reports/routing_replay_tighter_2026-04-05_2026-04-07.json)
  - [routing_replay_looser_2026-04-05_2026-04-07.json](/workspaces/trident/data/replay_reports/routing_replay_looser_2026-04-05_2026-04-07.json)

### 2026-04-05

- Pod A live runner branche
- Pod B paper runner et wrapper live branches
- Pod C minimal implemente
- reporting multi-pods et dashboard enrichis
- fetch/review tooling ajoute
- hardening Hyperliquid et artefacts de deploiement ajoutes

### 2026-04-04

- bootstrap repo et premiers socles:
  - convertisseur snapshots
  - superviseur
  - integration Pod B initiale
  - replay archives local

## Decision recentes importantes

### Routing 5bis

- statut: ferme
- principe retenu:
  - `global_regime` borne risque et caps
  - `local_regime` decide l'affinite par coin
  - hysteresis + cooldown limitent le flip-flop
- reglage retenu:
  - `min_assign_score = 0.40`
  - `min_hold_score = 0.30`
  - `hysteresis_margin = 0.10`
  - `reassignment_cooldown_seconds = 600`
- replay retenu:
  - `records_processed = 2911`
  - `duplicate_timestamps_skipped = 1457`
  - `max_ownership_conflict_count = 0`
  - `reassignment_event_count = 649`
  - owner share:
    - `pod_a = 6.88%`
    - `pod_b = 78.42%`
    - `pod_c = 14.70%`

### Pod B

- statut: remplace completement par le Pod B breakout directionnel actuel
- lecture actuelle:
  - moteur directionnel crypto, complementaire de Pod A
  - runtime / replay / UI / fetch sont maintenant homogenes avec Pod A et Pod C
  - peut recevoir des symbols en `DeadZone` pour surveillance, sans forcement ouvrir de trade
  - contribution retenue sur le replay fetched integre: `+19.00 USD`
  - le point de vigilance restant est surtout le churn de routing sur un univers crypto plus large
  - prochaine validation: run long serveur avec profil actif `Pod A + Pod B`

### Profil actif 2026-04-10

- decision retenue a cette date:
  - `Pod A` principal, max_leverage=10x (au lieu de 5x)
  - `Pod B` complement (engine Avellaneda-Stoikov), max_allocation_pct=0.40
  - `Pod C` desactive dans la config serveur de ce moment-la
- preuve actuelle:
  - replay valide sur `2026-04-05 -> 2026-04-10` (6 jours)
  - Pod A: `+429.07 USD`, 130 trades, 62.3% win rate
  - Pod B: `-0.61 USD`, 1349 fills
  - Pod C: `0 USD`, 0 trades
- bug corrige:
  - le live runner Pod C ignorait les allocations et tradait en parallele de Pod A/B
  - desormais il respecte le meme routing que le backtest full-bot
- consequence:
  - ce constat est historique et ne decrit plus la config actuelle
  - la version actuelle utilise un scope builder-dex Pod C actif et dex-aware
  - tout changement futur de Pod C doit etre valide en replay sur son univers builder-dex avant redeploy

### Hydra

- statut: research offline uniquement
- regle retenue:
  - funding / liq / OI ne rentrent pas dans le coeur live tant qu'un sweep offline n'a pas produit un verdict `go`

## Prochaines actions

1. constituer un dataset Tradfi exploitable pour replay via snapshots minute `l2Book + trades`
2. valider le regime par cluster en replay sur donnees Tradfi reelles
3. implementer la logique directionnelle Pod C (setups, filtres, exits adaptes Tradfi)
4. lancer un dry-run serveur long avec la config active incluant le scope builder-dex de Pod C
5. auditer `Pod B` comme couche complementaire avec les outils de review existants
6. continuer la validation de `Pod A` complet sur une plage plus longue
7. lancer un sweep Hydra offline et sortir un memo `go / park / kill`
