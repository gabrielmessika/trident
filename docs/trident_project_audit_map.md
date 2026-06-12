# Cartographie d'audit du projet TRIDENT

Date de generation: 2026-06-11

Ce document est une carte detaillee du projet TRIDENT pour permettre a un
outil LLM de realiser un audit fonctionnel, architecturel, trading et
operationnel sans devoir relire toutes les sources. Il condense l'etat courant
du repo, le plan actif, les configs de production/dry-run, les flux d'execution,
les modeles de trading et les points de controle critiques.

## 1. Autorites, limites et regles d'audit

Sources de verite courantes:

- `docs/trident_active_plan.md` est l'autorite pour la roadmap, le statut des
  pods, les decisions live/mainnet, les caps, les guardrails et la separation
  TRIDENT / TRIDENT-HIP4.
- `config/trident.toml` est l'autorite pour l'application TRIDENT A/C
  production/dry-run.
- `config/hip4_outcome_mainnet_paper.toml` est l'autorite pour l'application
  TRIDENT-HIP4 en mainnet paper.
- `docker-compose.trident.yml`, `docker-compose.hip4.yml`, `deploy.sh`,
  `trident-hip4/deploy.sh`, `scripts/fetch_trident_data.sh` et
  `trident-hip4/fetch_data.sh` sont les references de deploiement et de
  rapatriement des donnees.
- Les anciens rapports, anciens plans et docs historiques dans `docs/`,
  `plan_trident.md`, `data/server_archive/` ou `data/gbot_archive/` sont du
  contexte. Ils ne doivent pas contredire le plan actif.

Limites importantes:

- Ne jamais exposer `.env.trident` ni les secrets associes.
- Ne jamais activer de live trading ou envoyer de vrais ordres par un simple
  refactor ou audit. Toute action pouvant envoyer des ordres reels doit etre
  explicitement confirmee.
- Toute modification fonctionnelle doit verifier son impact sur le deploiement,
  les scripts de fetching et la revue des donnees serveur.
- Le nom "Pod B" est ambigu historiquement. Dans l'etat courant, le Pod B
  operationnel est `HIP4OutcomeEdgePod`, dans l'application separee
  TRIDENT-HIP4. Le Pod B directionnel crypto dans TRIDENT A/C est legacy et
  desactive.

## 2. Resume executif

TRIDENT est une stack de trading Hyperliquid composee aujourd'hui de deux
applications deployables separees.

Application TRIDENT:

- Perimetre courant: Pod A + Pod C seulement.
- Chemin serveur attendu: `/opt/trident`.
- Compose: `docker-compose.trident.yml`.
- Deploiement: `./deploy.sh`.
- Gestion serveur: `scripts/trident_server.sh`.
- Fetch data: `scripts/fetch_trident_data.sh`.
- API/UI: port 3000.
- Services principaux: `trident-api`, `pod-a-live`, `pod-c-live`,
  `tradfi-funding-collector`, et optionnellement `funding-collector`.
- Objectif courant: burn-in live/testnet Pod A/C puis mainnet tiny-size sous
  guardrails.
- `TRIDENT_ENABLE_POD_B=false` et `TRIDENT_ENABLE_HIP4_OUTCOME=false` dans le
  compose TRIDENT.

Application TRIDENT-HIP4:

- Perimetre courant: `HIP4OutcomeEdgePod` uniquement.
- Chemin serveur attendu: `/opt/trident-hip4`.
- Compose: `docker-compose.hip4.yml`.
- Deploiement: `trident-hip4/deploy.sh`.
- Gestion serveur: `scripts/trident_hip4_server.sh`.
- Fetch data: `trident-hip4/fetch_data.sh`.
- API/UI: port 3001.
- Services principaux: `hip4-api`, `hip4-outcome-paper`,
  `hip4-mainnet-observer` et sidecars optionnels.
- Mode par defaut: mainnet paper, pas d'execution mainnet reelle.
- Config active: `config/hip4_outcome_mainnet_paper.toml`.

L'architecture generale est volontairement separee:

- TRIDENT A/C traite des strategies directionnelles sur perps Hyperliquid.
- TRIDENT-HIP4 traite des tokens outcome/binary spot-like avec settlement
  binaire et modeles d'edge distincts.
- La separation evite que les contraintes de risque, d'execution et de donnees
  HIP-4 contaminent les pods directionnels.

## 3. Carte du repo

| Chemin | Role | Notes d'audit |
| --- | --- | --- |
| `app/main.py` | Entree CLI/API TRIDENT | Lance le serveur HTTP et le supervisor selon `--mode`, `--profile`, `--config`. |
| `app/settings.py` | Parsing TOML vers dataclasses config | Verifier les defaults, overrides env et la coherence avec `config/trident.toml`. |
| `app/trident/types.py` | Modele de donnees canonique | Contient `SymbolMarketSnapshot`, `RegimeSnapshot`, `TradePlan`, regimes, pods. |
| `app/trident/supervisor.py` | Orchestrateur central A/B/C | Regime, routing, allocation, previews, plans, etat API. |
| `app/trident/regime_allocator.py` | Detection regime global | Mode legacy actif car `crypto_v2_enabled=false`; v2 existe mais inactive. |
| `app/trident/regime_snapshot_v2.py` | Enrichissement regime v2 | Metriques de breadth, dispersion, leader trend, coherence. |
| `app/trident/capital_allocator.py` | Allocation capital par regime/pod/cluster | Redistribue vers cash quand pods disabled ou pas de symbole eligible. |
| `app/trident/symbol_router.py` | Ownership dynamique des symboles | Affinite locale, hysteresis, cooldown, debounce, overrides runtime. |
| `app/trident/market_clusters.py` | Clusters/correlation/leaders | Crypto par defaut, overrides config, leaders cluster, groupes correlation. |
| `app/pods/pod_a/*` | Pod A crypto core | Contextes, strategie anchor trend, risk gate, execution runner. |
| `app/pods/pod_b/*` | Pod B directionnel legacy | Conserve pour replay/backtests, mais disabled dans TRIDENT courant. |
| `app/pods/pod_c/*` | Pod C TradFi builder-dex | Continuation/reclaim directionnels sur clusters index/gold/silver/oil/fx/equity. |
| `app/live/*` | Collecte live et runners | WS Hyperliquid, snapshots, runners live A/C, enrichissement funding. |
| `app/execution/*` | Execution, portfolio, venues | Dry-run/live, state store, reconciliation, protective orders, rate limits. |
| `app/risk/*` | Gates risque | Decisions d'acceptation/rejet par pod et garde-fous de portefeuille. |
| `app/observability/*` | API/UI/status | HTTP stdlib, endpoints et dashboard. |
| `app/hip4_outcome/*` | Pod HIP4 outcome | Edge detector, risk, capital, executor paper/testnet, state/status. |
| `config/trident.toml` | Config active TRIDENT A/C | Source principale des univers, caps, setups, risk gates. |
| `config/hip4_outcome_mainnet_paper.toml` | Config active HIP4 paper | Source principale des edges, exits, risk et budgets HIP4. |
| `docker-compose.trident.yml` | Compose TRIDENT A/C | Ne doit pas lancer HIP4 ni Pod B legacy. |
| `docker-compose.hip4.yml` | Compose TRIDENT-HIP4 | Ne doit pas lancer Pod A/C. |
| `scripts/fetch_trident_data.sh` | Fetch TRIDENT A/C | Rapatrie API/logs/runtime/config Docker A/C dans `server-data/`. |
| `trident-hip4/fetch_data.sh` | Fetch HIP4 | Rapatrie API/logs/runtime/config HIP4 dans `server-data/hip4/`. |
| `docs/server_data_review_agent.md` | Workflow revue data serveur | A suivre pour toute demande de verification donnees serveur. |
| `tests/` | Suite pytest | Commande usuelle: `uv run pytest` ou `make test`. |

## 4. Modes et concepts metier

Modes applicatifs:

- `observation`: collecte, calcule et expose l'etat sans execution.
- `dry-run`: simule les fills avec `DryRunExecutionVenue`, journalise les trades.
- `live`: utilise `LiveExecutionVenue`, reconciliation exchange, state store et
  protective orders. C'est un mode sensible.

Regimes globaux:

- `TrendExpansion`: environnement de tendance exploitable.
- `RangeAuction`: environnement range/auction, capital majoritairement cash.
- `PanicSqueeze`: volatilite ou impulsion forte, entries plus selectives.
- `DeadZone`: activite/volatilite trop faible, capital cash.
- `Cash`: fallback si donnees non pretes ou allocation impossible.

Regimes locaux par symbole:

- `TrendStructure`: symbole en structure directionnelle.
- `RangeStructure`: symbole en range exploitable mais peu directionnel.
- `EventImpulse`: impulsion evenementielle/microstructurelle.
- `Neutral`: pas assez d'evidence.

Pods:

- `pod_a`: crypto core, actuellement long-only via `trend_pullback_long`.
- `pod_b`: legacy breakout directionnel crypto, disabled.
- `pod_c`: TradFi directionnel builder-dex, continuation/reclaim par cluster.
- HIP4: pas un pod A/C runtime; app separee pour outcomes.

## 5. Topologie TRIDENT A/C

Flux logique:

```text
Hyperliquid WS / REST
  -> HyperliquidLiveCollector
  -> SnapshotBuilder
  -> SymbolMarketSnapshot / RegimeSnapshot / cluster snapshots
  -> Supervisor
       -> RegimeDetector
       -> CapitalAllocator
       -> SymbolRouter
       -> Pod A service/planner
       -> Pod C service/planner
       -> risk gates
  -> DirectionalExecutor
       -> DryRunExecutionVenue ou LiveExecutionVenue
       -> DirectionalPortfolioState
       -> LiveStateStore / journals / status
  -> Observability API/UI
```

Principes:

- Le collector produit des snapshots de marche normalises.
- Le supervisor est le point d'orchestration unique: il calcule regime, routing,
  allocation, previews et plans.
- Les pods produisent des `TradePlan`; ils ne doivent pas envoyer d'ordres.
- Les risk gates acceptent/rejettent les plans.
- L'executor gere l'ouverture, la fermeture, les stops, les time stops, les
  cooldowns, les revocations de routing et les upgrades.
- La venue dry-run ou live est la seule couche qui simule ou envoie les ordres.

## 6. Topologie TRIDENT-HIP4

Flux logique:

```text
Hyperliquid outcomeMeta / mids / l2Book
  -> decouverte des marches priceBinary
  -> references externes sous-jacents
  -> estimation probabilite / features court terme / shock guard
  -> edge detector
       -> late_expiry
       -> parity
       -> model
       -> price_bucket
       -> named_outcome_basket
       -> short_expiry observe-only
  -> risk manager
  -> capital guard
  -> paper/testnet executor
  -> state store / status / daily reports / API HIP4
```

Principes:

- HIP4 traite des actifs outcome a payoff binaire, pas des perps directionnels.
- Le mode actif est mainnet paper.
- Les sorties actives sont pilotees par `early_exit_policy = "prob_stop_full"`.
- Les sorties EV/TP sont conservees en comparaison shadow, mais inactives sous
  la politique active.
- Aucune execution mainnet HIP4 reelle n'est active dans l'etat courant.

## 7. Donnees canoniques

`SymbolMarketSnapshot` est la ligne de features canonique pour A/C. Elle agrège:

- Prix: `price`, mid derive du book, mark/oracle/premium si enrichi.
- Tendance: EMA fast/slow, distance VWAP, structure score.
- Funding/spread: taux funding, spread bps.
- Alignement: BTC alignment, cluster, cluster leader, cluster alignment.
- Microstructure: book imbalance, trade flow bias, depth bid/ask, velocities,
  microprice, volume ratio, trade count ratio, bucket notional.
- Volatilite: realized vol short/long, compression score, range bucket.
- Open interest et contexte source quand disponibles.

`TradePlan` est le contrat entre pod et execution:

- Identite: pod, symbole, side, setup.
- Conviction: confidence, explications, setup details.
- Sizing: notional cible, marge cible, leverage, mode isolated/cross.
- Risque: stop bps, expected loss, risk budget.
- Exits: take-profit, break-even, trailing stop, time stop.
- Controle: cooldown, invalidation price, tags d'observation et watchers.

`SupervisorState` expose:

- Regime global, historique de regime, regimes clusters et regimes locaux.
- Routing/ownership par symbole.
- Observed symbol status et raisons d'ineligibilite.
- Capital plan par pod/symbole/cluster.
- Signal previews/reviews.
- Statut Pod B legacy et indicateurs shadow le cas echeant.

## 8. Pipeline de donnees marche

Collecte:

- `HyperliquidLiveCollector` s'abonne a `l2Book` et `trades`.
- Les connexions WS sont sharde par `max_coins_per_connection=10`.
- La collecte gere backoff, rate limits, heartbeat, timeouts et reconnexion.
- Les snapshots live sont ecrits en JSONL; les features Pod B legacy peuvent
  aussi etre produites pour replay.

Construction snapshot:

- Bucket par defaut: 60 secondes.
- Pour chaque symbole: prix mid, spread, best bid/ask size, depth 10 bps,
  VWAP du bucket, trade flow, book imbalance, velocities.
- EMA fast alpha: 0.35.
- EMA slow alpha: 0.12.
- Realized vol short alpha: 0.35.
- Realized vol long alpha: 0.08.
- `structure_score` combine tendance, flow et book:
  - trend score: 55%.
  - flow score: 25%.
  - book score: 20%.
- Compression et microprice dislocation sont derivees des ranges, volumes et
  imbalance du bucket.

Enrichissement:

- `asset_ctx_enricher` ajoute funding, OI, mark, oracle, premium et day volume.
- L'alignement funding se fait par timestamp avec age max usuel autour de
  900 secondes.
- Les clusters sont ajoutes via `market_clusters.enrich_snapshots`.

Points d'audit:

- Verifier que les snapshots live contiennent les champs requis par les setups
  actifs avant d'interpreter des rejets comme des problemes trading.
- Distinguer absence de signal, rejet risk gate et absence de donnees.
- Verifier la fraicheur funding/OI pour eviter des decisions sur contexte stale.

## 9. Univers et config active TRIDENT

Mode general:

- `[general] mode = "observation"` dans `config/trident.toml`.
- Host local: `127.0.0.1`.
- Port UI/API: 3000.

Hyperliquid:

- Info/WS mainnet.
- Snapshot dir: `./data/live_snapshots`.
- Bucket general: 60 secondes.
- Bucket Pod B legacy: 10 secondes.
- Observation shardee avec 10 coins maximum par connexion.

Univers observe:

- Crypto: BTC, ETH, SOL, HYPE, DOGE, XRP, SUI, AVAX, LINK, ARB, ADA, BNB, LTC,
  AAVE, NEAR, ZRO, TAO, ZEC, ENA, TON, BCH, STRK, ONDO, BIO, VVV, SAGA, JUP,
  PENGU, INJ, PENDLE, TIA, DYM, ICP, ATOM.
- Builder-dex / TradFi: `XYZ:CL`, `XYZ:BRENTOIL`, `XYZ:SP500`, `XYZ:XYZ100`,
  `XYZ:SILVER`, `XYZ:GOLD`, `XYZ:JPY`, `XYZ:TSLA`, `XYZ:NVDA`, `XYZ:CRCL`.

Symboles bloques globalement:

- TAO, AAVE, ADA, AVAX, HYPE, ICP, NEAR, ONDO, PENDLE, TON, VVV, XRP.

Clusters:

- Crypto par defaut.
- Overrides pour PAXG/SPY/GLD/SLV/QQQ et les symboles `XYZ:*`.
- Leaders: BTC/ETH pour crypto, SP500/XYZ100 pour index, GOLD pour gold,
  SILVER pour silver, CL/BRENTOIL pour oil, JPY pour fx, TSLA/NVDA/CRCL pour
  equity.

Tradability live:

- Spread maximum: 10 bps.
- Bucket notional minimum: 100 USD.
- Trade count minimum: 3.
- Funding absolu maximum: 0.01.

Capital/risk global:

- Reference equity: 1000 USD.
- Allocation max par symbole: 25%.
- Allocation minimale par symbole: 25 USD.
- Confidence minimum globale: 0.50.
- Maximum plans par batch: 2.
- Min notional: 10 USD.
- Max risk par trade: 1.5%.
- Max open risk total: 3%.

Execution live:

- Cap live courant: 200 USD.
- Protective orders requis: true.
- `live_block_stop_grace_setups = false`.
- Slippage live: 8 bps open, 12 bps close.
- Catastrophic stop dynamique active: multiplier 2, buffer 35 bps, maximum
  160 bps.

## 10. Regime global

Mode actif:

- `crypto_v2_enabled=false`, donc le regime legacy est actif.

Seuils legacy:

- ADX trend: 22.
- Structure trend minimum: 0.30.
- ATR panic ratio: 1.8.
- ATR dead zone: 0.45.
- Range width dead zone: 80 bps.
- Confirmation switch: 3 bars.
- Confirmation trend: 1 bar.
- Confirmation panic: 1 bar.

Decision legacy:

- Si les donnees ne sont pas pretes: `Cash`.
- `PanicSqueeze` si `atr_ratio >= 1.8` et impulsion BTC ou structure absolue
  au moins 0.50.
- `TrendExpansion` si `adx >= 22`, `atr_ratio > 0.45` et structure absolue au
  moins 0.30.
- `DeadZone` si `atr_ratio <= 0.45` et range width au plus 80 bps.
- Sinon: `RangeAuction`.

Regime v2 inactif mais present:

- Utilise breadth, dispersion, leader trend, coherence et nombre de symboles
  actifs.
- Sert a eviter les faux regimes quand la tendance manque de breadth, leader
  ou coherence.
- Ne doit pas etre considere actif sans changement explicite de config.

Points d'audit:

- Verifier que les transitions de regime respectent les confirmations.
- Verifier que les pods ne prennent pas d'entries quand le regime global bloque
  leur setup actif.
- Ne pas attribuer a Pod A/C une absence de trade si le capital allocator a
  envoye la cible vers cash.

## 11. Allocation capital

Allocations globales par regime:

| Regime | Pod A | Pod B legacy | Pod C | Cash |
| --- | ---: | ---: | ---: | ---: |
| TrendExpansion | 0.80 | 0.00 | 0.20 | 0.00 |
| RangeAuction | 0.10 | 0.00 | 0.15 | 0.75 |
| PanicSqueeze | 0.10 | 0.00 | 0.05 | 0.85 |
| DeadZone | 0.00 | 0.20 | 0.05 | 0.75 |

Effets importants:

- Pod B legacy est disabled; son allocation effective est redirigee vers cash.
- Si un pod a une cible positive mais aucun symbole owned/eligible, la cible
  non deployable finit en cash.
- Les allocations par symbole sont uniformes sauf pour Pod C, ou les budgets
  cluster s'appliquent.
- Un minimum de 25 USD par symbole evite de creer des allocations inutilisables.

Allocations Pod C par cluster:

| Cluster | TrendExpansion | RangeAuction | PanicSqueeze | DeadZone |
| --- | ---: | ---: | ---: | ---: |
| index | 0.10 | 0.05 | 0.00 | 0.00 |
| gold | 0.15 | 0.10 | 0.10 | 0.05 |
| silver | 0.05 | 0.03 | 0.02 | 0.00 |
| oil | 0.15 | 0.06 | 0.02 | 0.00 |
| fx | 0.03 | 0.02 | 0.01 | 0.00 |
| equity | 0.07 | 0.05 | 0.01 | 0.00 |

Points d'audit:

- Les budgets cluster ne signifient pas que tous les clusters tradent: il faut
  aussi un symbole tradable, route, setup valide et risk gate accepte.
- `XYZ:SILVER` est bloque cote Pod C, meme si le cluster silver a un budget.
- Pod B legacy ne doit pas recevoir d'allocation live dans TRIDENT A/C.

## 12. Routing et ownership des symboles

Role:

- `SymbolRouter` decide quel pod possede chaque symbole a un instant donne.
- Le supervisor n'envoie a un pod que ses symboles d'ouverture autorises plus
  ses positions deja actives a gerer/fermer.

Inputs:

- Regime global.
- Regime local du symbole.
- Cluster et cluster regime.
- Tradability status.
- Candidats par pod.
- Capital plan.
- Runtime overrides dans `runtime/trident/symbol_routing_overrides.json`.

Mecanismes:

- Minimum assign score: 0.40.
- Minimum hold score: 0.30.
- Hysteresis: 0.10.
- Cooldown: 600 secondes.
- Debounce score minimum: 0.12.
- Tie-break de priorite: Pod C, puis Pod A, puis Pod B.
- Capacity derivee de target pct, reference equity et allocation minimale par
  symbole.

Affinites locales:

| Pod | TrendStructure | RangeStructure | EventImpulse | Neutral |
| --- | ---: | ---: | ---: | ---: |
| Pod A | 1.00 | 0.30 | 0.65 | 0.45 |
| Pod B legacy | 0.45 | 0.05 | 1.00 | 0.35 |
| Pod C | 1.00 | 0.20 | 0.80 | 0.35 |

Scoring Pod A:

- Local regime: 15%.
- Global regime: 25%.
- Trend shape: 25%.
- Structure: 20%.
- Reclaim/VWAP: 10%.
- Cluster: 5%.

Scoring Pod B legacy:

- Local regime: 16%.
- Global regime: 18%.
- Trend: 8%.
- Structure: 12%.
- Flow: 16%.
- Activity: 10%.
- Impulse: 18%.
- Spread: 2%.
- Bonus shadow optionnel.

Scoring Pod C:

- Requiert symbole eligible et budget cluster positif.
- Local regime: 12%.
- Global regime: 22%.
- Trend: 22%.
- Structure: 18%.
- Flow: 12%.
- Activity: 8%.
- Spread: 3%.
- Alignment: 3%.
- Pour non-crypto, utilise le regime cluster.

Raisons et modes de routing:

- `manual_override`.
- `dynamic_cooldown`.
- `dynamic_debounce`.
- `dynamic_hysteresis`.
- `dynamic_affinity`.
- `fallback_priority`.
- `allocation_capacity`.

Points d'audit:

- Un symbole peut etre observe et tradable mais non route si score ou capacity
  insuffisant.
- Une position active doit rester gerable par son pod meme apres revocation
  d'ouverture.
- Les overrides runtime doivent etre audites comme des changements de prod.

## 13. Pod A: crypto core

Statut actif:

- Enabled: true.
- Cluster autorise: crypto.
- Setups autorises actifs: `trend_pullback_long`.
- Setups explicitement disabled: BOS long/short, trend pullback short,
  liquidity sweep reclaim long/short, VWAP reclaim long et autres variantes.
- Regimes bloques: `DeadZone`, `RangeAuction`.
- Shorts non actifs dans la config courante.

Symboles bloques Pod A:

- AAVE, ADA, AVAX, HYPE, ICP, NEAR, ONDO, PENDLE, TON, VVV, XRP.

Parametres de risque/sizing:

- Allocation max Pod A: 0.85.
- Leverage default: 2.
- Leverage max: 30.
- Mode: isolated.
- Sizing: risk-based.
- Risk per trade: 1.25%.
- Min margin: 20 USD.
- Min notional: 10 USD.

Contexte Pod A:

- `MarketContextService` convertit `SymbolMarketSnapshot` et features candles
  en `AnchorTrendContext`.
- `CandleService` maintient des buffers rolling et calcule RSI, StochRSI, CCI,
  EMA, ATR, MACD histogram, Bollinger position, wick ratios, Ichimoku et
  extension BTC.
- Les features multi-timeframe incluent 15m, 1h, 4h, bias MTF, structure ready,
  swing levels, BOS flags, supertrend, Ichimoku, StochRSI, CCI20, distances EMA.

Ordre de detection disponible dans le service:

1. BOS retest long/short.
2. Liquidity sweep reclaim long/short.
3. VWAP reclaim long/short.
4. BOS fallback.
5. Reversal fade short.
6. Ichimoku continuation long/short.
7. Trend pullback long/short.

Dans l'etat courant, la risk gate et la config ne laissent passer que
`trend_pullback_long`.

Filtres anchor globaux:

- Cluster alignment requis.
- Spread maximum par cluster: crypto 8 bps, index 5 bps, gold 6 bps.
- Funding absolu maximum: crypto 0.0005, index 0.0015, gold 0.001.

Setup actif `trend_pullback_long`:

- Regime de setup attendu: principalement `TrendExpansion`.
- Structure score minimum de base: 0.40.
- Mode campaign courant: structure minimum 0.55.
- EMA stack bullish requis.
- VWAP distance minimum: -25 bps.
- Crypto indicator vetoes:
  - supertrend ne doit pas etre negatif.
  - Ichimoku bias doit rester au-dessus de -0.15.
  - VWAP reclaim score doit rester au-dessus de -0.10.
  - Eviter chase si StochRSI tres haut et CCI tres haut.

Confidence Pod A:

- Structure quality: 28%.
- Trend quality: 18%.
- Pullback quality: 14%.
- Spread quality: 8%.
- Funding quality: 5%.
- MTF quality: 8%.
- Structure break quality: 8%.
- Confirmation quality: 7%.
- Extension quality: 4%.
- Setup bonus possible.

Stops et exits:

- Stop initial derive de l'invalidation, souvent min(EMA slow, price) ajuste
  par range buffer.
- Stop fallback et clamp: environ 45 a 160 bps selon confidence/config.
- Campaign active pour `trend_pullback_long`:
  - regimes: TrendExpansion/PanicSqueeze.
  - min confidence: 0.66.
  - stop multiplier: 1.4.
  - stop floor: 160 bps.
  - time stop: 36h.
  - take profit: desactive (`take_profit_multiplier = 0`).
  - break-even: 1.45x.
  - trailing activation: 1.9x.
  - trailing distance: 1.15x.
  - cooldown: 45 minutes.
  - addons: disabled.
- Setup runner actif pour `trend_pullback_long` peut definir BE 1.0x, trailing
  1.4x, distance 0.8x, mais le campaign actif reprofile l'exit policy.

A-grade:

- Applique aux crypto `trend_pullback_long`.
- Score des conditions comme confidence, regime, candles, structure, trends
  1h/4h, VWAP, StochRSI, CCI, BTC ok et faibles watchers.
- Min score A-grade: 6.
- Size scale usuel: 1.25, et jusqu'a 1.40 pour strong.
- Strong A-grade donne une stop grace plus longue.

Pattern vetoes actifs:

- `trend1h_negative`.
- `trend4h_positive_cci_mid`.
- `mtf_4h_rsi14_weakness`.
- `mtf_4h_close_below_ema50`.
- `mtf_1h_chop_ema20_under_ema50_rsi40_50`.
- `mtf_1h_overextension_chase`.
- `btc_overextension_4h`.
- `xrp_overextension_4h_targeted`.

Pattern watchers actifs:

- `vwap_weak_trend4h_positive`.
- `vwap_weak`.
- `trend4h_flat`.

Adaptations live Pod A:

- Stop grace live: 60 minutes.
- Stop grace strong A-grade: 120 minutes.
- `early_failure_exit` actif pendant grace:
  - age entre 10 et 90 minutes.
  - adverse fraction 0.55.
  - adverse minimum 25 bps.
  - structure <= 0.20.
  - VWAP <= -8 bps.
- Live quality sizing actif:
  - low confidence < 0.62: multiplier 0.55.
  - mid confidence < 0.70: 0.75.
  - no A-grade: 0.70.
  - standard: 0.85.
  - watcher: 0.85.
  - multiplier minimum: 0.50.
- Loss tax actif:
  - fenetre 720 minutes.
  - multiplier 0.50 apres stop reasons stop/exchange close.
- Correlation slots:
  - 3 slots pleins.
  - ensuite multiplier 0.50.

Points d'audit Pod A:

- Un signal qui n'est pas `trend_pullback_long` doit etre bloque dans l'etat
  courant.
- Les entries en `RangeAuction` et `DeadZone` doivent etre bloquees.
- La stop grace ne doit pas supprimer la protection: elle remplace le stop
  normal par un catastrophic stop puis restaure le stop normal apres grace.
- Le cap live 200 USD s'applique apres sizing.
- Les symboles bloques doivent rester bloques meme si les features sont bonnes.

## 14. Pod C: TradFi directionnel builder-dex

Statut actif:

- Enabled: true.
- Clusters autorises: index, gold, silver, equity, oil, fx.
- `cluster_aware_v2_enabled=true`.
- Symboles bloques: `XYZ:SILVER`.

Parametres de risque/sizing:

- Allocation max Pod C: 0.90.
- Leverage default: 2.
- Leverage max: 30.
- Spread max: 8 bps.
- Funding absolu max: 0.015.
- Confidence minimum: 0.66.
- Size multiplier: 0.70.
- Risk per trade: 1.25%.
- Min margin: 20 USD.
- Min notional: 10 USD.
- Cooldown: 90 minutes.
- Time stop general: 6h.
- Bucket notional minimum: 100 USD.
- Trade count minimum: 3.
- Min trend: 8 bps.
- Min structure: 0.18.
- Max VWAP distance: 35 bps.
- Min reclaim distance: 6 bps.
- Min activity score: 0.75.

Contexte Pod C:

- `TradfiTrendContext` est construit depuis les snapshots enrichis.
- Le service suit l'activite et le trade count sur une fenetre config de 20.
- Les champs de reference externe sont conserves pour audit.
- Pour non-crypto, le cluster regime est central.

Filtres de base:

- Cluster autorise.
- Cluster aligned.
- Spread <= 8 bps.
- Funding abs <= 0.015.
- Bucket notional >= 100 USD.
- Trade count >= 3.
- Abs VWAP distance <= 35 bps.
- Activity score >= 0.75.

Direction:

- Vote majoritaire entre trend direction, structure sign et flow sign.
- Score >= 1: long.
- Score <= -1: short.
- En pratique, les modes cluster v2 actifs sont orientes long.

Setups generiques:

- `tradfi_continuation_{side}`:
  - abs trend >= 8 bps.
  - abs structure >= 0.18.
  - abs VWAP <= min reclaim distance.
- `tradfi_reclaim_{side}`:
  - structure >= 80% du seuil.
  - abs VWAP >= 6 bps.
  - VWAP oppose a la direction, donc pullback/reclaim.

Confidence Pod C:

- Trend quality: 24%.
- Structure: 22%.
- Flow: 20%.
- Activity: 16%.
- Spread: 10%.
- Reclaim: 8%.
- Setup bonus possible.

Cluster-aware v2:

- Oil:
  - setup: `tradfi_continuation_long`.
  - trend >= 9.
  - structure >= 0.24.
  - trade_flow >= 0.25.
  - VWAP entre -2.6 et -1.0.
  - activity >= 1.7.
  - flow_support entre 0.75 et 1.15.
  - range >= 18.
  - spread <= 3.
- Silver:
  - setup: `tradfi_continuation_long`.
  - trend >= 10.
  - structure >= 0.20.
  - trade_flow >= 0.03.
  - VWAP entre 1 et 6.
  - range >= 18.
  - spread <= 2.
  - Note: `XYZ:SILVER` est bloque, donc cette branche ne doit pas trader en
    production courante.
- Gold:
  - setup: `tradfi_continuation_long`.
  - cluster regime: TrendExpansion.
  - global regime: TrendExpansion ou PanicSqueeze.
  - trend >= 8.
  - structure >= 0.22.
  - trade_flow >= 0.02.
  - VWAP entre 0.5 et 3.5.
  - activity >= 1.1.
  - range >= 14.
  - spread <= 2.
- Index:
  - setup: `tradfi_continuation_long`.
  - trend >= 8.
  - structure >= 0.18.
  - trade_flow >= 0.02.
  - VWAP entre 1 et 6.
  - range >= 16.
  - spread <= 2.5.
- Equity/fx:
  - Pas de strategie v2 active documentee dans le chemin courant; si le cluster
    v2 est strict, ces clusters peuvent ne pas produire de plan.

Exits Pod C:

- Stop initial pour continuation:
  - 55 bps si confidence >= 0.78.
  - 65 bps sinon.
- Stop initial pour reclaim:
  - 70 ou 82 bps selon confidence.
- Ajustements cluster:
  - index: 0.90.
  - gold/silver: 0.95.
  - oil: 1.05.
- Modes cluster peuvent appliquer multiplier/floor specifiques.
- Continuation generic:
  - TP environ 1.8x ou 1.6x.
  - BE environ 0.85x.
  - trailing activation environ 1.15x.
  - trailing distance environ 0.60x.
- Modes actifs:
  - index: TP 1.28, BE 1.08, trailing activation 1.30, distance 1.00,
    time stop 9h.
  - silver: TP 1.08, BE 0.90, trailing activation 0.75, distance 0.75, mais
    symbole bloque.
  - gold: TP 1.08, BE 1.00, trailing activation 1.10, distance 1.10, time stop
    6h.

Pattern controls:

- Watchers:
  - `index_soft_trend_watch`.
  - `index_extension_entry_watch`.
- Veto:
  - `silver_strong_extension_veto`.

Points d'audit Pod C:

- Verifier que `XYZ:SILVER` ne produit pas d'ordre malgre les configs silver.
- Verifier que les branches cluster-aware v2 rejettent clairement les clusters
  sans strategie active.
- Controler la coherence entre cluster budget, routing et plans produits.
- Verifier que les external references ne sont pas stale avant toute conclusion
  sur les signaux TradFi.

## 15. Pod B directionnel legacy

Statut:

- `pod_b.enabled=false` dans `config/trident.toml`.
- Non deploye par TRIDENT A/C.
- Ne doit pas etre confondu avec TRIDENT-HIP4.

Role restant:

- Code conserve pour replay/backtests, comparaisons historiques et eventuelle
  recherche.
- Peut encore apparaitre dans certains etats API comme legacy/shadow.

Strategie legacy:

- Breakout directionnel crypto.
- Filtres: crypto, BTC aligned, regimes TrendExpansion/PanicSqueeze, price > 0,
  structure >= 0.15, trend quality >= 6 bps, realized_vol_short >= 6 bps,
  spread <= 8 bps, bucket notional >= 100, trades >= 3, abs VWAP <= 35.
- Direction par vote EMA, structure, flow, book et microprice.
- Score >= 2: long.
- Score <= -2: short.
- Shorts disabled dans config.

Setups:

- `compression_breakout`.
- `vol_expansion`.
- `ttm_squeeze_release`.
- Config courante autorise seulement `vol_expansion_long` et
  `ttm_squeeze_release_long` si le pod etait active.

Strict continuation pour `vol_expansion_long`:

- structure >= 0.20.
- VWAP >= 4.
- trade_flow >= 0.05.
- book_imbalance >= 0.
- delta_trade_flow >= 0.05.
- range >= 30.
- realized_vol_short >= 2.2.
- spread <= 2.2.

Risk legacy:

- Max concurrent: 3.
- Max total risk: 1.5%.
- Risk per trade: 0.75%.

Points d'audit:

- Tout audit live TRIDENT A/C doit confirmer que Pod B legacy ne tourne pas.
- Si un rapport mentionne Pod B apres 2026-05-24, verifier s'il parle de HIP4
  ou du legacy breakout.

## 16. HIP4OutcomeEdgePod

Statut:

- App separee TRIDENT-HIP4.
- Mode actif: mainnet paper.
- Config: `config/hip4_outcome_mainnet_paper.toml`.
- `allow_testnet_orders=false`.
- `require_testnet_url=false`.
- Pas d'execution mainnet reelle.

Fichiers runtime/logs courants:

- Logs: `logs/hip4_outcome_mainnet_paper`.
- State: `runtime/hip4_outcome_mainnet_paper_state.json`.
- Status: `logs/hip4_outcome_status.json`.
- Alias Pod B: `logs/pod_b_live_status.json`.

Boucle:

- Intervalle: 4 secondes.
- Max markets: 12.
- Max opportunities: 4.
- `include_underlyings` vide signifie accepter tous les priceBinary tradables
  retournes.

Sources reference:

- Binance, OKX, Bybit, Coinbase, Kraken et Hyperliquid.
- Anchor Hyperliquid.
- Deviation max source: 50 bps.
- Sources minimum: 1.

Edge families:

- `late_expiry`.
- `parity`.
- `model`.
- `short_expiry` en observe-only.
- `price_bucket`.
- `named_outcome_basket`.
- Observation de marche.

Modeles d'edge:

- Late expiry:
  - BUY_YES si reference > strike avec buffer et ask YES acceptable.
  - BUY_NO si reference < strike avec buffer et ask NO acceptable.
  - Gross edge approx: payoff 1 moins ask.
  - Net edge retire settlement fee, slippage et safety margin.
- Parity:
  - BUY_BOTH si yes.ask + no.ask < 1 apres couts.
- Model:
  - Probabilite theorique via modele lognormal et volatilite annualisee.
  - BUY_YES si probability_yes - yes.ask est suffisante.
  - BUY_NO si probability_no - no.ask est suffisante.
- Short expiry:
  - Melange probabilite statique, momentum court terme, distance, book
    probability et imbalance.
  - Observe-only dans l'etat courant.
- Price bucket:
  - Compare probabilite d'etre dans ou hors bucket au prix YES/NO.

Shock guard:

- Actif.
- Fenetres: 900, 3600, 14400, 86400, 259200, 604800 secondes.
- Seuils bps: 80, 150, 250, 300, 300, 400.
- Min adverse windows: 2.
- Edge types couverts: MODEL, LATE_EXPIRY, SHORT_EXPIRY.

Couts et seuils:

- Open fee: 0.
- Settlement fee: 0.002.
- Estimated fees: 0.002.
- Slippage: 0.005.
- Safety margin: 0.01.
- Min gross edge: 0.025.
- Min net edge: 0.015.

Exits actifs:

- `early_exit_policy = "prob_stop_full"`.
- Probability stop enabled:
  - stop probability: 0.35.
  - max loss ROI: 0.20.
- Reentry lock jusqu'au settlement: true.
- EV/TP exits: shadow/inactifs sous la policy active.
- Shadow exit, shadow sizing et maker quote logic actives pour comparaison.

Risk/capital:

- max_position_usdc: 12.
- budget_usdc: 500.
- max exposure: 500.
- max exposure par underlying: 150.
- max open markets: 3.
- min depth: 12.
- max spread: 0.60.
- min order value: 10.

Risk manager rejette si:

- Net edge trop faible.
- Taille/depth insuffisante.
- Marche expire, trop proche ou trop loin.
- Settlement source manquante.
- priceBucket/namedBasket interdits dans certains modes testnet.
- Shock guard adverse.
- Slice bloquee.
- Reference divergence.
- Position deja ouverte.
- Kelly shadow trop faible en paper.
- Max open markets/exposure depasses.
- Depth/spread insuffisants.
- Exchange min order value non respecte.
- Observer mode signal-only.

Execution:

- Paper executor consomme les asks visibles, quantize les tailles et respecte
  min order value.
- Testnet executor existe mais requiert URL/secret/flag explicites.
- State store JSON persiste les positions.

Points d'audit HIP4:

- Confirmer que le mode est paper avant toute conclusion sur PnL live.
- Distinguer realized paper, shadow exits, shadow sizing et observations.
- Verifier que `short_expiry` n'est pas active comme source d'ordres.
- Verifier que les exits EV/TP ne sont pas interpretes comme policy active si
  `prob_stop_full` est active.

## 17. Execution directionnelle A/C

Executor:

- `DirectionalExecutor` est partage par Pod A et Pod C.
- Il ferme les positions existantes sur:
  - stop loss.
  - take profit.
  - trailing stop.
  - break-even.
  - time stop.
  - routing revoked, sauf grace/exemption.
  - opposite signal.
- Il ouvre seulement les plans acceptes par risk gate et si le symbole est dans
  `entry_allowed_symbols`.
- Il gere cooldowns, upgrades et campaign scale-in.

Portfolio:

- `DirectionalPortfolioState` suit positions ouvertes, trades fermes, PnL,
  break-even, trailing, take profit et reentry cooldown.
- `PodAStopGracePortfolioState` ajoute la logique de stop grace et early
  failure exit pour Pod A.

Dry-run:

- Fill taker au mid ajuste par spread multiplier et slippage.
- Fee usuelle: 3.5 bps.
- N'envoie aucun ordre reel.

Live:

- `LiveExecutionVenue` utilise le SDK Hyperliquid.
- Orders d'entree IOC par defaut.
- Bloque ouverture si:
  - notional <= 0.
  - price <= 0.
  - notional > live cap.
  - exposition exchange deja presente.
  - stop grace block si active.
- Ecrit `pending_position` durable immediatement apres fill.
- Place ensuite les protective orders reduce-only SL/TP.
- Si SL requis et impossible, emergency close puis erreur.
- Pour stop-grace, place d'abord un catastrophic SL puis remplace par stop
  normal apres grace.
- Close utilise la taille exchange si disponible.
- Rounding respecte les contraintes Hyperliquid, dont la regle liee a
  `6 - szDecimals`.
- Supporte symboles builder-dex wire.

Reconciliation live:

- Refuse l'etat ready si positions exchange inconnues.
- Refuse local missing positions et side mismatches.
- Refuse orders ouverts/triggers inconnus sauf overrides explicites.
- Peut recuperer des positions connues depuis metadata ou `pending_position`.
- Reconnait les state stores externes entre Pod A et Pod C.
- Expose le capital HL, incluant source USDC spot/unified.

Incident ARB 2026-06-07:

- Correction: `LiveExecutionVenue` ecrit `pending_position` durable juste apres
  fill.
- Correction: rounding conforme a Hyperliquid `6 - szDecimals`.
- Guardrail: ne pas contourner via `TRIDENT_LIVE_ALLOW_UNKNOWN_POSITIONS=true`
  sauf intervention explicite et auditee.

Points d'audit execution:

- Toute position live doit avoir state store, metadata orders et protective
  coverage coherents.
- Les local states de Pod A et Pod C ne doivent pas se voler les positions.
- Si user stream live n'est pas sain, les entries doivent etre desactivees.
- Le cap 200 USD doit etre applique avant ordre live.

## 18. Risk gates

Risk gate de base:

- Confidence minimum.
- Notional minimum.
- Margin minimum.
- Leverage maximum.
- Expected loss <= risk budget.
- Total open risk <= limite globale.
- Maximum plans par batch.

Extensions Pod A:

- Symboles bloques.
- Setups autorises/disabled.
- Regimes bloques.
- Guardrails rolling intraday/setup/symbol.
- Pattern vetoes.
- Symbol modes.
- Pattern watchers sur plans acceptes.
- Live quality sizing et loss tax hors risk gate pur mais critiques en prod.

Extensions Pod C:

- Symboles bloques.
- Confidence min 0.66.
- Pattern vetoes/watchers.
- Contraintes cluster-aware.
- Contraintes builder-dex/tradability.

Extensions HIP4:

- Edge net/gross.
- Market expiry window.
- Settlement source.
- Shock guard.
- Depth/spread/min order.
- Budget/exposure.
- Reentry lock.
- Mode paper/testnet/observer.

Points d'audit:

- Toujours separer `no signal`, `signal rejected by risk`, `signal blocked by
  routing` et `signal accepted but executor skipped`.
- Les watchers ne sont pas toujours des rejets; ils peuvent annoter et reduire
  la taille.
- Les vetoes doivent etre deterministes et journalises.

## 19. Observabilite, API et etat

Serveur HTTP:

- Module: `app/observability/api.py`.
- Base TRIDENT: port 3000.
- TRIDENT-HIP4: port 3001 via compose HIP4.
- UI TRIDENT affiche Pod A et Pod C seulement via `TRIDENT_UI_PODS`.
- Routes HIP4 actives si `TRIDENT_APP_KIND=trident-hip4` ou
  `TRIDENT_ENABLE_HIP4_OUTCOME=true`.

Endpoints utiles TRIDENT:

- `/health`.
- `/api/state`.
- `/api/metrics`.
- `/api/report`.

Endpoints utiles HIP4:

- `/health`.
- `/api/hip4-outcome`.
- `/api/hip4-outcome-mainnet`.
- `/api/hip4-nautilus-shadow` si sidecar disponible.
- `/api/state`.
- `/api/report`.

Journals/status typiques:

- `logs/pod_a_live.jsonl`.
- `logs/pod_c_live.jsonl`.
- `logs/pod_a_live_status.json`.
- `logs/pod_c_live_status.json`.
- `logs/hip4_outcome_status.json`.
- `logs/pod_b_live_status.json` comme alias HIP4.
- `runtime/trident/live_state_pod_a.json`.
- `runtime/trident/live_state_pod_c.json`.
- `runtime/hip4_outcome_mainnet_paper_state.json`.

Points d'audit:

- Les endpoints API doivent correspondre au bon app kind.
- Ne pas conclure que HIP4 manque dans TRIDENT A/C: c'est voulu.
- Ne pas conclure que Pod B legacy manque dans l'UI A/C: c'est voulu.

## 20. Deploiement et fetching

TRIDENT A/C:

- Compose: `docker-compose.trident.yml`.
- API: `trident-api`.
- Runners: `pod-a-live`, `pod-c-live`.
- Collectors: `tradfi-funding-collector`, optional `funding-collector`.
- Volumes: config read-only, logs, data, runtime.
- Fetch: `./scripts/fetch_trident_data.sh --days 3`.
- Review locale: `./scripts/fetch_trident_data.sh --review-only`.
- Donnees locales: `server-data/`.
- Reviews: `server-data/reviews/<timestamp>`.

TRIDENT-HIP4:

- Compose: `docker-compose.hip4.yml`.
- API: `hip4-api`.
- Runner paper: `hip4-outcome-paper`.
- Observer mainnet: `hip4-mainnet-observer` via profile.
- Fetch: `./trident-hip4/fetch_data.sh`.
- Donnees locales: `server-data/hip4/`.
- Reviews: `server-data/hip4/reviews/<timestamp>`.

Regle d'audit donnees serveur:

- Pour verifier que tout est OK cote TRIDENT ou analyser les donnees fetch,
  suivre `docs/server_data_review_agent.md`.
- Si le skill `$trident-server-data-review` est disponible, l'utiliser.
- Si un prompt mentionne `/server-data`, verifier d'abord si le chemin absolu
  existe; sinon interpreter comme `server-data/` repo-local.

## 21. Tests, backtests et rapports

Commandes usuelles:

- Installer/synchroniser: `uv sync`.
- Tests: `uv run pytest` ou `make test`.
- Si `uv run pytest` echoue avec `Failed to spawn: pytest`, installer pytest
  localement avec `uv pip install pytest` puis relancer.
- Dry-run: `make run-dry`.
- Healthcheck: `make healthcheck`.

Backtests/rapports:

- Les references officielles de backtest sont listees dans
  `docs/trident_active_plan.md`.
- Les nouveaux rapports experimentaux doivent aller dans
  `server-data/replay_reports/` ou `tmp/` avec nom explicite/date.
- Ne pas ecraser les baselines officielles sans demande explicite.
- Pour une nouvelle regle de trading, comparer contre la baseline full-bot
  pertinente, pas seulement contre un test isole.

Points d'audit:

- Les tests unitaires ne remplacent pas une revue des logs live/dry-run.
- Pour valider un changement strategie, exiger replay/backtest contre baseline.
- Pour valider un changement execution, exiger tests de state/reconciliation et
  dry-run/live-sim avant toute promotion.

## 22. Check-list d'audit LLM

Avant de conclure:

- Confirmer quel perimetre est audite: TRIDENT A/C, TRIDENT-HIP4, ou les deux.
- Lire ou utiliser la version courante de `docs/trident_active_plan.md` comme
  arbitre en cas de contradiction.
- Confirmer la config active (`config/trident.toml` ou config HIP4).
- Confirmer le mode: observation, dry-run, live, paper, testnet, observer.
- Confirmer la date des donnees serveur et la fraicheur des logs.

Architecture:

- Verifier que TRIDENT A/C ne lance pas HIP4 ni Pod B legacy.
- Verifier que TRIDENT-HIP4 ne lance pas Pod A/C.
- Verifier que l'API/UI expose les bons pods selon app kind.
- Verifier que les scripts de fetch rapatrient le bon perimetre.

Donnees:

- Verifier presence, fraicheur et schema des snapshots.
- Verifier funding/OI/mark/oracle/premium si une decision depend de ces champs.
- Verifier cluster alignment et leader mapping.
- Verifier tradability status par symbole et raison de rejet.

Regime/allocation/routing:

- Verifier regime global et confirmations.
- Verifier capital plan, cash residual et allocations cluster.
- Verifier ownership routing et overrides runtime.
- Verifier que les positions existantes restent gerables meme si entries
  revokees.

Pod A:

- Verifier que seul `trend_pullback_long` trade.
- Verifier les regimes autorises.
- Verifier symboles bloques.
- Verifier A-grade, watchers, loss tax et correlation sizing.
- Verifier stop grace, early failure exit et catastrophic stop.

Pod C:

- Verifier que cluster-aware v2 est actif.
- Verifier les contraintes par cluster.
- Verifier que `XYZ:SILVER` reste bloque.
- Verifier external references et builder-dex symbol mapping.

Pod B legacy:

- Verifier disabled.
- Ne pas utiliser ses signaux pour evaluer TRIDENT A/C live.

HIP4:

- Verifier paper/mainnet observer/testnet.
- Verifier `prob_stop_full` comme policy active.
- Verifier shock guard et reentry locks.
- Verifier que `short_expiry` est observe-only.
- Distinguer realised paper, shadow exits et shadow sizing.

Execution:

- Verifier cap live 200 USD.
- Verifier protective orders requis.
- Verifier state store, `pending_position`, metadata orders et reconciliation.
- Verifier user stream live health.
- Verifier rounding prix/taille Hyperliquid.

Securite:

- Verifier aucun secret dans logs/docs.
- Verifier aucun override env dangereux active sans justification.
- Verifier aucune instruction de promotion live/mainnet non confirmee.

## 23. Risques et zones sensibles

Zones sensibles connues:

- Ambiguite Pod B legacy vs HIP4OutcomeEdgePod.
- Stop grace Pod A: doit rester protege par catastrophic stop.
- Reconciliation live: unknown exchange positions ne doivent pas etre ignorees.
- Rounding Hyperliquid: risque de divergence ordre/state si incorrect.
- Builder-dex symbols: mapping wire symbol et references externes.
- HIP4 exits: confusion possible entre policy active et shadow policies.
- Config vs README: README peut contenir du wording historique; le plan actif
  et les configs gagnent.

Patterns de bug probables:

- Signal valide mais non route a cause de capacity ou hysteresis.
- Signal preview present mais risk gate reject.
- Plan accepte mais executor skip car symbole non autorise en entry.
- Capital cluster positif mais aucun symbole eligible.
- Donnees stale interpretees comme absence de signal.
- Pod B legacy analyse comme pod actif par erreur.
- HIP4 paper interprete comme PnL live.

## 24. Modele mental synthetique

TRIDENT A/C est une architecture en couches:

1. Donnees marche normalisees.
2. Detection regime et cluster.
3. Allocation capital.
4. Routing/ownership symbole.
5. Generation de plans par pod.
6. Risk gate.
7. Execution dry-run/live.
8. State/reconciliation.
9. Observability/fetch/review.

Pod A cherche une continuation crypto selective:

- Long-only.
- Pullback dans tendance.
- Qualite MTF et structure.
- Evite sur-extension, faiblesse VWAP, faiblesse 4h et mauvais regime.
- En live, size et stops sont fortement gouvernes par A-grade, watchers, loss
  tax, correlation et stop grace.

Pod C cherche une continuation TradFi selective:

- Builder-dex.
- Cluster-aware.
- Principalement long dans les branches v2 actives.
- Suit index/gold/oil, avec silver configure mais bloque.
- Depend fortement de trend, structure, flow, activity, spread et cluster
  regime.

HIP4 cherche des inefficiences d'outcome tokens:

- Payoff binaire.
- Edge net apres couts.
- Probability model et market microstructure.
- Shock guard et early exit probability stop.
- Paper mainnet par defaut.

Cette separation est le choix d'architecture central: chaque moteur a son
modele de marche, ses exits, son risk gate et son perimetre de deploiement.

## 25. Dossier autonome pour audit externe

Si l'outil d'audit n'a pas acces au repo, ce document seul permet un audit
architecturel et logique, mais pas un audit PnL complet. Pour analyser la
performance et proposer des ameliorations, il faut fournir un dossier autonome
compose de cette carte et d'annexes datees.

Verdict a appliquer par defaut:

- Carte seule: suffisante pour audit architecture/design, insuffisante pour PnL.
- Carte + annexes Markdown actuelles: suffisantes pour cadrer les hypotheses,
  encore insuffisantes pour ameliorations PnL chiffrees definitives.
- Carte + export compact `server-data/audit_exports/<timestamp>/`: suffisante
  pour analyser les decisions/signaux et HIP4 paper, mais l'attribution PnL
  TRIDENT A/C reste incomplete sans closed trades/fills de sortie.
- Carte + export compact + closed trades/exchange fills A/C: niveau minimal
  pour proposer des ameliorations PnL chiffrees sur Pod A/C.

Niveaux d'audit possibles selon les annexes disponibles:

| Donnees fournies | Audit possible | Limite |
| --- | --- | --- |
| Cette carte seulement | Architecture, risques de design, coherence des guardrails | Pas de conclusion PnL ni de diagnostic operationnel recent. |
| Carte + digest derniers fetchs | Healthcheck, etat courant, anomalies evidentes, lecture du PnL resume | Pas d'attribution trade-level fiable. |
| Carte + digest + reviews fetch completes | Audit operationnel plus robuste | PnL encore limite si les trades/signaux bruts manquent. |
| Carte + normalized trades/signals/positions | Attribution PnL A/C, causes de pertes, effets routing/risk/execution | Necessite horodatage et joins propres. |
| Carte + HIP4 decisions/trades/settlements/replays | Audit PnL HIP4, calibration, exit policy, churn/reentry | Necessite separer paper, observer et shadow. |
| Carte + baselines/replays | Recommandations d'amelioration testables | Ne pas promouvoir sans out-of-sample ou burn-in. |

Annexes recommandees:

- `annex_00_manifest.md`: inventaire des fichiers fournis, timestamps, source
  et niveau d'audit autorise.
- `annex_01_latest_fetch_digest_YYYYMMDD.md`: synthese des derniers fetchs A/C
  et HIP4, avec verdicts, PnL resume, positions, blockers et next focus.
- `annex_02_pnl_audit_data_contract.md`: schema des tables/JSONL attendus et
  methode de calcul.
- `trident_ac_review_summary.md`: dernier `review_summary.md` A/C colle ou
  exporte comme annexe, pas seulement reference par chemin repo.
- `hip4_outcome_run_review.md`: derniere review HIP4 paper/observer.
- `hip4_policy_market_audit.md`: dernier audit/replay des policies HIP4.
- `trident_ac_trades.csv` ou `.jsonl`: trades/fills normalises Pod A/C.
- `trident_ac_signal_decisions.jsonl`: previews, risk decisions, executor skips.
- `trident_ac_positions_snapshot.json`: positions ouvertes et state store.
- `trident_ac_health_snapshot.json`: `/health`, `/api/state`, `/api/report`,
  `/api/metrics` exportes.
- `hip4_decisions.jsonl`, `hip4_trades.csv`, `hip4_settlements.csv`,
  `hip4_policy_replay.csv`: donnees HIP4 normalisees.

Exporteur local:

- `scripts/export_trident_audit_pack.py` produit un dossier compact sous
  `server-data/audit_exports/<timestamp>/`.
- Il normalise les decisions/signaux A/C, les fills d'ouverture A/C, les closed
  trades A/C issus de `closed_trade_log`, les live states Pod A/C, les
  decisions HIP4, les trades/settlements HIP4, les policy replays et les
  statuses runtime.
- Il ne lance pas le fetch lui-meme. Utiliser `--fresh-fetch-run` seulement
  apres un fetch effectif.
- Il n'exporte pas volontairement de secrets ni les payloads bruts de references
  externes HIP4.
- Limite connue: l'export 2026-06-11 contient les closed trades applicatifs,
  mais les logs fetches ne contiennent toujours pas de `close_fills`. Une
  reconciliation exchange fill-by-fill des sorties A/C requiert donc un export
  de fills exchange ou une instrumentation supplementaire.

Regles pour annexes:

- Les annexes doivent contenir les donnees elles-memes, pas seulement des chemins
  vers `server-data/`, car l'outil externe ne peut pas lire le repo.
- Chaque annexe doit indiquer `generated_at`, `source_window_start`,
  `source_window_end`, `mode`, `network`, `app_kind` et si un fetch frais a ete
  lance.
- Si une annexe vient d'un fichier local deja present sans fetch frais, le dire
  explicitement.
- Les rapports de fetch complets peuvent etre fournis en annexes, mais la carte
  principale doit rester stable et ne pas absorber des donnees temporelles.
- Si les annexes trade-level manquent, l'auditeur doit marquer tout diagnostic
  PnL comme `insufficient_data`. Si les closed trades sont presents mais pas les
  close fills, le diagnostic A/C peut etre fait au niveau applicatif mais les
  conclusions d'execution doivent rester `needs_exchange_reconciliation`.

Decision sur les derniers rapports fetch:

- Oui, ils doivent etre fournis au moins sous forme de digest annexe pour tout
  audit PnL ou operationnel.
- Non, il ne faut pas les coller integralement dans cette carte permanente.
- Pour une revue serieuse, fournir a la fois le digest et les rapports complets
  les plus recents en annexes separees.

## 26. Methode d'analyse PnL TRIDENT A/C

Objectif:

- Expliquer d'ou vient le PnL, pas seulement s'il est positif ou negatif.
- Isoler le modele trading, le sizing, la risk gate, le routing, l'execution et
  les incidents de reconciliation.

Unite d'analyse principale:

- La position fermee, enrichie par ses fills, le signal d'entree, la risk
  decision, l'etat de regime/allocation/routing a l'entree, puis la raison de
  sortie.

Attributions minimales:

- Pod: Pod A ou Pod C.
- Symbole et cluster.
- Setup.
- Side.
- Regime global a l'entree et a la sortie.
- Cluster regime et local regime.
- Confidence.
- A-grade et score si Pod A.
- Watchers et vetoes.
- Allocation target USD.
- Target notional avant et apres live cap.
- Margin, leverage, notional.
- Stop initial bps et risk budget.
- Exit reason.
- Fees, funding, slippage estime ou reel.
- PnL brut, PnL net, PnL en R.
- MFE/MAE si disponible.
- Time in trade.

Formules:

- `gross_pnl_usd = signed_qty * (exit_price - entry_price)`.
- Pour short, inverser le signe du mouvement prix.
- `net_pnl_usd = gross_pnl_usd - fees_usd - funding_usd - slippage_cost_usd`.
- `planned_risk_usd = abs(entry_notional_usd) * initial_stop_bps / 10000`.
- `r_multiple = net_pnl_usd / planned_risk_usd`.
- `expectancy_usd = average(net_pnl_usd)`.
- `expectancy_r = average(r_multiple)`.
- `profit_factor = sum(winning_net_pnl) / abs(sum(losing_net_pnl))`.
- `win_rate = winning_trades / closed_trades`.
- `loss_concentration = abs(worst_trade_pnl) / abs(sum(losing_net_pnl))`.
- `cap_utilization = entry_notional_usd / live_max_order_notional_usd`.
- `execution_shortfall_bps = signed_side * (fill_price - decision_mid_price) /
  decision_mid_price * 10000`.

Etapes d'audit:

1. Reconcilier totals runtime: realized PnL, unrealized PnL, open positions,
   fill count, win rate.
2. Construire la table des positions fermees et verifier que le total net
   reconstruit egale le runtime PnL a tolerance explicite.
3. Grouper PnL par pod, setup, symbole, cluster, regime, confidence bucket,
   watcher/veto, exit reason et periode.
4. Separer pertes de modele et pertes d'execution:
   - modele: mauvais signal, mauvais regime, overextension, mauvais setup.
   - risk: target trop grand, stop trop serre/large, leverage, correlation.
   - execution: slippage, cap, skip, protective order, reconciliation.
5. Comparer PnL realise aux stops planifies:
   - perte reelle <= perte planifiee plus tolerance.
   - toute perte au-dessus du plan doit etre expliquee.
6. Analyser les skips:
   - accepted by risk puis `notional_above_live_cap`.
   - accepted puis symbol not entry-allowed.
   - accepted puis user stream unhealthy.
   - accepted puis reconciliation not ready.
7. Pour Pod A, mesurer l'effet A-grade, watchers, loss tax, correlation sizing
   et stop grace.
8. Pour Pod C, mesurer l'effet cluster-aware v2, symbol block silver, cluster
   budget et external references.
9. Produire une liste d'ameliorations avec preuve, impact estime, risque et test
   de validation.

Buckets recommandes:

- Confidence: `<0.62`, `0.62-0.70`, `0.70-0.78`, `>=0.78`.
- Time in trade: `<15m`, `15-60m`, `1-6h`, `6-24h`, `>24h`.
- Regime: TrendExpansion, PanicSqueeze, RangeAuction, DeadZone, Cash.
- Exit reason: stop, catastrophic stop, early failure, time stop, trailing,
  break-even, take profit, routing revoked, opposite signal, exchange close.
- Stop quality:
  - `within_plan`: loss <= planned loss * 1.10.
  - `mild_excess`: 1.10 a 1.50.
  - `severe_excess`: > 1.50.
- Live cap:
  - `below_50pct_cap`.
  - `50_90pct_cap`.
  - `near_cap`.
  - `capped`.

Questions PnL a trancher:

- Le PnL negatif vient-il d'une mauvaise selection de trades ou d'un mismatch
  execution/sizing?
- Les trades acceptes par risk gate sont-ils ensuite bloques par cap live?
- Les pertes sont-elles concentrees sur un symbole, un setup, un regime ou une
  periode?
- Les watchers annoncent-ils vraiment une degradation de PnL?
- Les A-grade ameliorent-ils le PnL ou seulement la taille?
- Les stops reels respectent-ils les stops planifies?
- Les time stops coupent-ils des trades qui auraient recupere ou limitent-ils
  vraiment les drawdowns?
- Les revocations routing ferment-elles trop tot?
- Pod C perd-il sur un cluster specifique ou sur un probleme de liquidite?

Sortie attendue d'un audit PnL A/C:

- Verdict: `OK`, `WARN`, `KO` ou `insufficient_data`.
- PnL total et par pod.
- Top 5 contributions positives et negatives.
- Breakdown par setup/regime/symbole/exit reason.
- Incidents execution/reconciliation.
- Hypotheses d'amelioration classees par evidence et risque.
- Donnees manquantes qui empechent de conclure.

## 27. Methode d'analyse PnL HIP4

Objectif:

- Evaluer la qualite des edges outcome, la calibration, les exits et le churn,
  sans melanger paper, observer et shadow.

Unite d'analyse principale:

- Marche outcome + position paper + settlement, enrichis par decision,
  probabilite, edge type, side, underlying, expiry, books, reference price,
  shock guard et exit policy.

Attributions minimales:

- Profile: mainnet paper, mainnet observer, testnet ou shadow.
- Market id, underlying, expiry, strike/bucket.
- Edge type.
- Side.
- Probability estimate a l'entree.
- YES/NO ask/bid a l'entree.
- Net edge apres couts.
- Approved/rejected reason.
- Size approved, size filled.
- Exit policy active.
- Exit reason.
- Settlement outcome.
- PnL net.
- Reentry lock status.
- Shock guard status.
- Nautilus quality bucket si disponible.

Formules et metrics:

- `edge_net = expected_payoff - entry_cost - fees - slippage - safety_margin`.
- `brier = average((predicted_probability - actual_outcome)^2)`.
- `calibration_gap = win_rate_slice - avg_predicted_probability_slice`.
- `profit_factor = wins / abs(losses)`.
- `worst_loss_share = abs(worst_loss) / abs(total_losses)`.
- `churn_count = positions_same_market_before_settlement`.
- `reentry_loss_usd = pnl of trades reopened after early exit on same market`.

Etapes d'audit:

1. Confirmer le mode actif et la policy active (`prob_stop_full` dans l'etat
   courant).
2. Separer paper execute, mainnet observer, testnet et shadow.
3. Reconciler trades, positions ouvertes, settlements et PnL net.
4. Grouper par edge type, side, market id, expiry bucket et underlying.
5. Mesurer calibration par slice: count, avg pred, win rate, Brier, PnL.
6. Identifier les pertes dominantes et leur part du PnL total.
7. Comparer active policy vs shadow policies avec les memes settlements.
8. Verifier si une sortie early a permis une re-entry perdante avant settlement.
9. Verifier les rejets dominants: `market_already_open`, shock guard, depth,
   min order value, shadow Kelly.
10. Pour Nautilus, separer data quality utile, would-block decisions et
    trade-time joins.

Questions PnL HIP4 a trancher:

- Le modele est-il mal calibre ou l'edge est-il mange par couts/liquidite?
- Les pertes viennent-elles d'un edge type ou d'un side specifique?
- Les exits actifs reduisent-ils la perte ou creent-ils du churn?
- La policy shadow gagne-t-elle sur un nombre suffisant de settlements?
- Les reentries apres early exit degradent-elles le PnL?
- Les observations non-BTC sont-elles reellement tradables ou observe-only?
- Nautilus aurait-il bloque des trades perdants sans bloquer les gagnants?

Sortie attendue d'un audit HIP4:

- Verdict separe `mainnet_paper`, `mainnet_observer`, `shadow`.
- Readiness et blockers.
- PnL, PF, win rate, Brier, settlements count.
- Contribution des pires trades.
- Policy active vs policies shadow.
- Reentry/churn.
- Recommandations de collecte, rollback, guardrail ou promotion.

## 28. Protocole d'amelioration

Toute recommandation doit etre classee:

- `bugfix`: incoherence, skip non voulu, mismatch state, mauvaise reconciliation.
- `guardrail`: reduction de risque sans changer l'edge principal.
- `sizing`: changement de taille/cap/leverage/loss tax/correlation.
- `entry_filter`: filtre supplementaire avant entree.
- `exit_policy`: stop, BE, trailing, TP, time stop, early exit.
- `data_quality`: collecte, freshness, schema, references externes.
- `research_only`: idee insuffisamment prouvee.

Barre de preuve:

- Ne pas recommander une promotion live/mainnet sur moins de 20 trades ou
  settlements pertinents, sauf bugfix de securite.
- Ne pas recommander un filtre qui retire surtout des perdants si le nombre de
  trades restants devient trop faible.
- Toujours comparer au full-bot baseline pertinent.
- Toujours separer in-sample et out-of-sample quand des replays existent.
- Ne pas optimiser sur un seul worst trade sans verifier le cout en opportunite.
- Un changement execution/protective order demande tests de state,
  reconciliation et dry-run/live-sim.

Format d'une proposition:

- Probleme observe.
- Preuve: fichiers/annexes, metriques, exemples.
- Changement propose.
- Impact attendu.
- Risque introduit.
- Test minimal avant merge.
- Critere de rollback.
- Impact deploiement/fetching.

Exemples de conclusions valides:

- `WARN sizing`: Pod A accepte des plans mais l'executor les skip car target
  notional depasse le cap live. Corriger le chemin cap-aware avant de juger le
  modele.
- `WARN data`: HIP4 observer voit beaucoup d'opportunites, mais paper n'a pas
  assez de settlements pour promotion. Continuer collecte.
- `KO execution`: perte reelle stop > 1.5x perte planifiee sans explication,
  investiguer protective order/reconciliation.
- `research_only`: une policy shadow bat l'active sur 20 settlements mais le
  cutoff recent est faible; garder en observation.

## 29. Annexes creees avec cette carte

Les annexes suivantes sont ajoutees sous `docs/trident_audit_annexes/` pour
faciliter un export vers un outil externe:

- `annex_00_manifest.md`: manifest et niveaux d'audit.
- `annex_01_latest_fetch_digest_20260611.md`: digest des derniers rapports
  fetches au 2026-06-11, apres fetch global et inspection serveur.
- `annex_02_pnl_audit_data_contract.md`: schemas attendus pour trades, signals,
  positions, HIP4 decisions et settlements.
- `annex_03_gap_and_improvement_register_20260611.md`: verdict corrige,
  inventaire de l'export genere, gaps restants et axes d'amelioration
  detectables.
- `annex_04_baseline_replays_20260611.md`: baseline full-bot officielle,
  resultats replay, rapports de promotion Pod A et comparaisons Pod C.

Ces annexes ne remplacent pas les donnees brutes. Elles disent a l'outil ce qui
est connu, ce qui est manquant, et comment classer la confiance de ses
conclusions.

Export compact genere localement:

- Chemin: `server-data/audit_exports/20260611T135456Z/`.
- Statut: donnees ignorees par git, a fournir separement a l'outil externe si
  audit PnL demande.
- Contenu cle: `manifest.json`, `trident_ac_signal_decisions.jsonl`,
  `trident_ac_fill_events.csv`, `trident_ac_closed_trades.csv`,
  `trident_ac_runtime_summary.json`, `trident_ac_open_positions.json`,
  `trident_ac_live_state_pod_a.json`, `trident_ac_live_state_pod_c.json`,
  `baseline_official_current_cli_20260513.md`,
  `baseline_official_current_cli_20260513.json`,
  `baseline_reference_status_20260513.md`, `hip4_decisions.jsonl`,
  `hip4_trades.csv`, `hip4_settlements.csv`, `hip4_policy_replay.csv`.
- Statut data: 193697 decisions/reviews A/C, 141 fills A/C vus dans les logs,
  31 closed trades A/C, 93610 decisions HIP4, 27 trades HIP4 paper et 25
  settlements HIP4 paper.
- Baseline replay officielle jointe: full-bot `2026-04-05T19:45:00Z ->
  2026-05-13T07:56:49Z`, total `+859.83 USD`, Pod A `+780.72`, Pod B `0.00`,
  Pod C `+79.11`.
- Limite cle: A/C contient 0 close fill brut dans les logs exportes; l'audit
  PnL A/C est possible au niveau closed trade applicatif, mais la reconciliation
  exchange complete requiert encore les fills exchange de sortie.
