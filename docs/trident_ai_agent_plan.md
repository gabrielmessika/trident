# Plan TRIDENT-AI agentique

Date: `2026-06-07`

Statut: `RESEARCH_SHADOW_FIRST`

## Suivi d'implementation

- [x] Etape 1 - Spec, schemas, fixtures et validateur fail-closed.
- [x] Etape 2 - Config dediee `config/trident_ai.toml`.
- [x] Etape 3 - Abstraction LLM OpenAI Responses API, cache et couts.
- [x] Etape 4 - Feature builder depuis `SymbolMarketSnapshot`.
- [x] Etape 5 - Runner shadow local/replay avec agent deterministe, journal et
  runtime status.
- [x] Etape 6 - Replay avec cache LLM obligatoire et rapport comparatif.
- [x] Etape 7 - Dry-run/paper avec executor simule et cout IA net.
- [x] Etape 7b - Audit outcome zero-cout et recalibration scanner locale.
- [x] Etape 7c - Calibration pattern-first et score local `research_v1`.
- [x] Etape 7d - Validation out-of-sample du score pattern-first.
- [x] Etape 7e - Validation multi-fold et profil stable `research_v2_stable`.
- [x] Etape 7f - Audit exit/follow-through zero-cout sur OOS.
- [x] Etape 7g - Sweep overlay d'exit zero-cout sur OOS.
- [x] Etape 7h - Validation overlay multi-fold IS+OOS.
- [x] Etape 7i - Audit pattern-support symbol-agnostic avant nouveau LLM.
- [x] Etape 7j - Sweep gates edge/liquidite/cout avec penalite OOS.
- [x] Etape 7k - Audit failure-pattern multi-fold symbol-agnostic.
- [x] Etape 7l - Replay/sweep de veto entry-time zero-cout.
- [x] Etape 7m - Mini replay LLM payant v8 sous cap strict.
- [x] Etape 7n - Validation payante prompt v9 plus selectif.
- [x] Etape 7o - Premier fold out-of-sample payant v9 sous budget.
- [x] Etape 7p - Recalibration locale edge/exit avant nouvel appel LLM.
- [x] Etape 7q - Validation zero-cout d'un overlay exit robuste multi-fold.
- [ ] Etape 7r - Micro-replay LLM payant v10 uniquement apres validation.
- [ ] Etape 8 - Intel/news-social avec xAI en veto shadow uniquement.
- [ ] Etape 9 - Testnet live sur compte Hyperliquid independant.
- [ ] Etape 10 - Mainnet paper puis canary tiny apres confirmation manuelle.

Derniere mise a jour implementation: `2026-06-11`.

## Decision courte

Un bot `tridentAI` pilote par agent IA est pertinent comme axe de recherche,
mais pas comme remplacement direct de `Pod A` ou `Pod C`.

La meilleure voie n'est pas de repartir a zero. Il faut reutiliser le socle
TRIDENT pour les donnees marche, le backtest, le dry-run, l'execution
Hyperliquid, le journal, le state store, la reconciliation et les guardrails.
En revanche, il faut isoler `tridentAI` comme app/service independant, avec sa
propre config, son propre state, ses propres logs, son propre budget de risque
et un nouveau compte Hyperliquid independant.

Le principe central:

```text
LLM/agent = analyse et propose.
TRIDENT deterministe = valide, dimensionne, execute, journalise et coupe.
```

L'agent ne doit jamais avoir acces direct aux cles, au client d'execution, ni a
un outil capable d'envoyer un ordre. Il produit seulement des propositions
structurees. Un runner deterministe les rejette ou les transforme en
`TradePlan` borne par la config.

## Pourquoi ne pas repartir a zero

TRIDENT contient deja les briques les plus difficiles a rendre fiables:

- `HyperliquidLiveCollector`, `snapshot_builder` et `SymbolMarketSnapshot` pour
  transformer le flux live en features compactes.
- `TradePlan`, `RiskDecision`, `PodARiskGate`, `PodCRiskGate` et les limites
  globales dans `config/trident.toml`.
- `DryRunExecutionVenue` pour simuler fees, spread et slippage.
- `LiveExecutionVenue` pour executer sur Hyperliquid avec cap notionnel,
  rate-limit, verification d'exposition preexistante, ordres protecteurs et
  gestion des tailles/prix.
- `LiveStateStore` et `reconcile_exchange_state` pour eviter les positions
  inconnues, ordres orphelins et divergences exchange/local.
- `JsonlJournal`, `runtime_status`, dashboard/API et scripts de fetch pour
  auditer les decisions apres coup.
- `FullBotBacktestRunner` et les replay reports officiels pour comparer une
  nouvelle logique contre une baseline full-bot, pas seulement contre un test
  isole.

Repartir a zero ferait perdre ces protections et recreerait exactement les
risques deja traites par TRIDENT: crash entre fill et state, positions inconnues,
ordres protecteurs rejetes, conflits entre pods, mauvais rounding Hyperliquid,
open orders orphelins, et divergence entre backtest/dry-run/live.

## Ce qu'il faut isoler

`tridentAI` ne doit pas etre ajoute trop vite comme simple `Pod D` dans le
routage A/C. Le type `PodName` courant ne contient que `pod_a`, `pod_b`,
`pod_c`, et le plan actif separe deja clairement `TRIDENT` A/C et
`TRIDENT-HIP4`.

Architecture recommandee:

- module repo-local d'abord: `app/trident_ai/`;
- app deployable separee ensuite si le shadow est prometteur:
  `TRIDENT-AI`;
- dossier runtime dedie: `runtime/trident_ai/`;
- logs dedies: `logs/trident_ai_*.jsonl`;
- donnees dediees: `data/trident_ai/` ou `server-data/trident_ai/` pour les
  reviews;
- config dediee: `config/trident_ai.toml`;
- compte Hyperliquid independant des comptes A/C des le debut des phases
  d'execution testnet/mainnet.

Le service peut reutiliser les bibliotheques internes de TRIDENT, mais il ne
doit pas partager le meme etat live avec `Pod A` ou `Pod C` au debut.

## Repos publics observes

Recherche effectuee le `2026-06-07` sur GitHub et docs publiques.

| Repo | Ce qui existe deja | Ce qui est utile pour TRIDENT-AI | Prudence |
|---|---|---|---|
| `HammerGPT/Hyper-Alpha-Arena` | Plateforme IA pour Hyperliquid et Binance Futures, testnet paper et mainnet real, facteurs quant, agents specialises. | Confirme que l'idee "LLM + Hyperliquid + execution" existe deja. L'approche multi-agent/facteurs est proche du besoin. | A analyser comme inspiration, pas comme dependance. Les promesses de performance ne remplacent pas une validation TRIDENT. Source: https://github.com/HammerGPT/Hyper-Alpha-Arena |
| `EthanAlgoX/LLM-TradeBot` | Bot futures Binance multi-agent avec `DataSync`, analyse quant, decision core, risk audit, backtest, audit data et LLM optionnel. | Bon pattern: agents semantiques optionnels, risk audit avec veto, logs complets, backtests multi-onglets. | Exchange different, claims marketing a verifier. Source: https://github.com/EthanAlgoX/LLM-TradeBot |
| `TraderAlice/OpenAlice` | Agent trading cross-asset, unified trading account, "Trading-as-Git", guard pipeline, validation humaine avant execution. | Tres bon pattern de securite: staging des ordres, historique versionne, guard pipeline et approbation explicite. | Projet experimental, AGPL, plus terminal/workspace qu'un bot quant live. Source: https://github.com/TraderAlice/OpenAlice |
| `TauricResearch/TradingAgents` | Framework multi-agent finance: analystes fondamentaux, sentiment, news, technique, chercheurs bull/bear, trader, risk management, portfolio manager. | Utile pour l'organisation des roles, la persistence des decisions, le checkpoint resume, la reconnaissance de non-determinisme LLM. | Oriente recherche/analyse; le README signale que les resultats varient selon modele, donnees et periode. Source: https://github.com/TauricResearch/TradingAgents |
| `VibeTradingLabs/vibetrading` | Generation de strategie en langage naturel, validation statique, backtest, analyse LLM, live trading avec adaptateurs Hyperliquid. | Pattern important: `Generate -> Validate -> Backtest -> Analyze -> Deploy`, avec static validator avant runtime. | Plus "generation de code strategie" que decision agentique live; jeune repo. Source: https://github.com/VibeTradingLabs/vibetrading |
| `chain-ml/alphaswarm` | Starter kit agents LLM pour signaux complexes et execution autonome multi-chain. | Utile pour architecture plugin/data source/tooling. | Plus DeFi/on-chain generaliste que Hyperliquid perps. Source: https://github.com/chain-ml/alphaswarm |
| `yubing744/trading-gpt` | Bot base sur `bbgo` et `langchaingo`, strategies en langage naturel, indicateurs, memoire, Twitter/Fear & Greed. | Confirme le pattern LLM manager + memory + exchange abstraction. | Moins aligne avec les contraintes de reconciliation/live de TRIDENT. Source: https://github.com/yubing744/trading-gpt |

Conclusion de cette revue: l'idee existe deja et plusieurs projets
l'implementent partiellement, y compris sur Hyperliquid. Aucun ne justifie de
jeter l'architecture TRIDENT. Les meilleurs patterns a reprendre sont:

- separation entre agents d'analyse et moteur d'execution;
- risk audit/veto deterministe;
- validation statique ou schema stricte avant action;
- backtest et paper avant live;
- journal complet des inputs, prompts, outputs, decisions et rejets;
- human-in-the-loop avant promotion live.

## Architecture cible

Flux recommande:

```text
Hyperliquid WS/API
  -> TRIDENT collectors / snapshot builder
  -> feature store compact
  -> shortlist deterministe des candidats
  -> agents IA sans acces execution
      - Market Analyst
      - News/Social Analyst
      - Risk Critic
      - Decision Arbiter
  -> proposition JSON signee par schema
  -> validateurs deterministes
  -> TridentAIRiskGate
  -> DryRunExecutionVenue ou LiveExecutionVenue
  -> LiveStateStore + JsonlJournal + runtime_status
```

L'agent ne doit pas "voir une courbe" sous forme d'image en boucle live sauf
experience separee. Il doit recevoir des features compactes:

- OHLCV multi-timeframe;
- tendance EMA/VWAP;
- structure score;
- spread, depth, imbalance, trade flow;
- funding, open interest, premium;
- volatilite realisee;
- regime global et cluster;
- positions ouvertes de `tridentAI` uniquement si necessaire, jamais celles des
  autres pods au debut;
- news/social sous forme de digest source, date, symbole, fiabilite et horizon.

L'analyse visuelle de screenshots de chart est possible pour debug ou research,
mais elle est plus couteuse, moins reproductible et plus difficile a backtester.

## Services proposes

### `trident-ai-feature-builder`

Construit un contexte compact par symbole et par regime.

Inputs:

- snapshots live existants;
- maintenance refresh;
- asset context Hyperliquid;
- funding/OI;
- eventuellement candles derivees.

Output:

```json
{
  "as_of": "2026-06-07T12:00:00Z",
  "symbol": "SOL",
  "price": 142.1,
  "regime": "TrendExpansion",
  "features": {
    "ema_alignment": "bullish",
    "vwap_distance_bps": 18.4,
    "spread_bps": 2.1,
    "realized_vol_short_bps": 46.0,
    "funding_rate": 0.00012,
    "book_imbalance": 0.18,
    "trade_flow_bias": 0.22
  }
}
```

### `trident-ai-intel`

Collecte news et social en shadow.

Sources initiales:

- RSS officiels projets/exchanges;
- CryptoPanic ou equivalent;
- CoinMarketCal gratuit/payant si utile;
- X via `xAI X Search` cible, pas via scan massif;
- Reddit/Bluesky seulement en features faibles;
- web search ponctuel pour verifier un evenement.

Sortie:

```json
{
  "as_of": "2026-06-07T12:00:00Z",
  "global_market_impact": "risk_off",
  "items": [
    {
      "symbol": "SOL",
      "direction": "bearish",
      "confidence": 0.72,
      "horizon_minutes": 90,
      "event_type": "regulatory",
      "source_ids": ["news_20260607_001", "x_20260607_014"],
      "reliability": "confirmed"
    }
  ]
}
```

### `trident-ai-agent`

Produit une decision structuree. Il ne decide pas seul de l'execution.

Roles possibles:

- `Market Analyst`: lit uniquement features marche.
- `News/Social Analyst`: lit uniquement digest public et sources.
- `Risk Critic`: cherche les raisons de ne pas trader.
- `Decision Arbiter`: combine les sorties et produit `hold`, `open`, `close`,
  `reduce` ou `close_only_mode`.

Pour le MVP, commencer par un seul agent + un critic deterministe. Ajouter
plusieurs agents seulement si les logs montrent un gain mesurable.

### `trident-ai-runner`

Runner deterministe:

1. charge config;
2. charge state;
3. fait preflight/reconciliation;
4. construit les features;
5. appelle l'agent seulement sur shortlist;
6. valide le JSON;
7. applique `TridentAIRiskGate`;
8. execute en dry-run/paper/live selon mode;
9. journalise tout.

## Schema de proposition obligatoire

L'agent doit sortir un JSON strict, sans texte libre utilise par le runner.

```json
{
  "schema_version": "trident_ai_proposal_v1",
  "decision_id": "20260607T120000Z_SOL_001",
  "as_of": "2026-06-07T12:00:00Z",
  "valid_until": "2026-06-07T12:05:00Z",
  "action": "open",
  "symbol": "SOL",
  "side": "long",
  "confidence": 0.63,
  "time_horizon_minutes": 120,
  "max_notional_usd": 25.0,
  "max_leverage": 1.0,
  "entry_style": "ioc",
  "invalidation_price": 138.4,
  "stop_bps": 85.0,
  "take_profit_bps": 160.0,
  "time_stop_minutes": 180,
  "rationale_tags": [
    "trend_aligned",
    "volume_expansion",
    "news_neutral"
  ],
  "evidence_ids": [
    "market_SOL_20260607T120000Z",
    "intel_digest_20260607T115900Z"
  ],
  "risk_notes": [
    "spread_ok",
    "no_confirmed_negative_event"
  ]
}
```

Rejet automatique si:

- JSON invalide;
- champ requis absent;
- `symbol` hors whitelist;
- `valid_until` expire;
- `as_of` trop vieux;
- `stop_bps <= 0`;
- `take_profit_bps <= stop_bps` sauf strategie explicitement autorisee;
- `max_notional_usd` superieur au cap config;
- `confidence` sous seuil;
- `evidence_ids` absents;
- source news/social non verifiee pour un evenement fort;
- cote propose contredit un veto deterministe.

## Guardrails indispensables

### Guardrails de donnees

- Ne jamais envoyer `.env.trident`, cles, account secret, vault secret, ou
  chemins contenant des secrets.
- Ne pas exposer les positions `Pod A`/`Pod C` a l'agent pendant les phases
  initiales.
- Donner a l'agent des features publiques ou des positions `tridentAI` reduites
  au strict necessaire.
- TTL strict sur features marche et news.
- Deduplication des news/social par URL, source, hash et timestamp.
- Score de fiabilite source: officiel, media etabli, agregateur, social
  verifie, rumeur.
- Protection contre l'injection de prompt dans news/social: le contenu externe
  est traite comme donnee non fiable, jamais comme instruction.

### Guardrails de modele

- Modele et prompt versionnes.
- Temperature basse pour decisions live.
- Structured outputs ou validation JSON schema stricte.
- Timeout court; timeout = `hold`.
- Retry limite; pas de cascade infinie multi-agent.
- Deuxieme modele ou critic uniquement sur propositions `open`/`increase`.
- Modele indisponible = `hold` ou `close_only`, jamais "continue comme avant".
- Budget mensuel hard cap dans config et cote fournisseur.

### Guardrails de risque

Caps initiaux recommandes:

| Phase | Network | Notional max | Levier max | Positions max | Perte journaliere max | Mode |
|---|---|---:|---:|---:|---:|---|
| Shadow | aucun ordre | 0 | 0 | 0 | 0 | observe |
| Dry-run | local | 25 USD simule | 1x | 1 | 5 USD simule | paper |
| Testnet | Hyperliquid testnet | 25 USD | 1x | 1 | 5 USD | vrais ordres testnet |
| Mainnet paper | mainnet sans ordre | 25 USD paper | 1x | 1 | 5 USD paper | observe |
| Mainnet canary | compte Hyperliquid independant | 25 USD | 1x | 1 | 3-5 USD | live tiny |
| Mainnet palier 2 | compte Hyperliquid independant | 50 USD | 1x-2x | 1-2 | 5-8 USD | live tiny |

Autres contraintes:

- max trades par jour;
- cooldown par symbole apres close;
- interdiction d'ouvrir si spread, depth, funding ou volatilite hors borne;
- interdiction d'ouvrir si `reconcile_exchange_state.ready=false`;
- aucune position inconnue toleree;
- aucun ordre ouvert inconnu tolere;
- close reduce-only avec taille exchange exacte;
- SL exchange reduce-only obligatoire immediatement apres fill;
- TP optionnel mais journalise;
- kill-switch operateur;
- close-only automatique apres N rejets execution, N pertes consecutives,
  drawdown intraday ou cout IA anormal.

### Guardrails de concurrence avec Pod A/C

Phase initiale:

- `tridentAI` ne trade pas les symboles actuellement ouverts par A/C.
- `tridentAI` utilise une whitelist plus petite:
  `BTC`, `ETH`, `SOL`, `HYPE` au debut.
- Pas de partage de marge avec A/C.
- Pas de routage inter-pods automatique.
- Le compte Hyperliquid etant independant, aucune position A/C ne doit etre
  visible comme position native `tridentAI`; le runner conserve quand meme un
  veto par symbole si A/C est deja expose sur le meme symbole, afin d'eviter
  de doubler une these de marche.

### Guardrails sociaux/news

- Une rumeur social ne peut pas declencher seule un trade.
- Une news negative confirmee peut seulement:
  - veto une nouvelle entree;
  - reduire une taille;
  - forcer une review;
  - passer en close-only si evenement critique.
- Un evenement positif non officiel ne peut pas augmenter le cap live.
- Les sources doivent etre referencees par ID dans le journal.
- Les resumes LLM ne remplacent pas les donnees brutes conservees.

## Evaluation avant live

### Phase 0 - specs et fixtures

Objectif: prouver que le runner fail-closed.

Tests:

- schemas JSON invalides;
- symbole hallucine;
- stale market context;
- stale news digest;
- stop manquant;
- notional trop grand;
- confidence basse;
- evidence absente;
- provider timeout;
- provider retourne du texte libre;
- position exchange inconnue;
- ordre protecteur rejete.

Sortie attendue: aucun ordre, raison de rejet normalisee, journal complet.

### Phase 1 - replay offline sans LLM live

Objectif: tester la plomberie.

- Lire `server-data/` ou snapshots historiques.
- Remplacer l'agent par fixtures deterministes.
- Verifier conversion `proposal -> TradePlan -> RiskDecision`.
- Verifier dry-run et journal.
- Verifier cout simule et latence simulee.

### Phase 2 - replay avec LLM cache

Objectif: evaluer l'edge sans laisser le modele bouger entre runs.

- Appeler le LLM sur des timestamps historiques.
- Sauver input prompt, model, output brut, output parse, token usage, cout.
- Rejouer ensuite uniquement depuis le cache.
- Comparer contre:
  - baseline full-bot officielle;
  - Pod A seul;
  - Pod C seul;
  - strategy `hold`;
  - random entry controlee par meme frequence.

Metriques:

- PnL net fees/slippage;
- max drawdown;
- profit factor;
- expectancy par trade;
- Sharpe/Sortino si echantillon suffisant;
- win rate par confidence bucket;
- calibration: `confidence 0.60-0.70` doit gagner moins que `0.80+`;
- cout IA par trade;
- cout IA / PnL brut;
- taux de propositions rejetees;
- taux d'hallucination schema/symbole;
- latence p50/p95;
- sensibilite au modele et au prompt.

### Phase 3 - backtest comparatif OOS

Critere minimum avant shadow live:

- dataset out-of-sample non utilise pour prompt tuning;
- aucune fuite temporelle news/social;
- replay reproductible depuis cache;
- PnL net superieur a baseline apres cout IA, ou drawdown nettement meilleur a
  PnL comparable;
- pas d'amelioration concentree sur 1 ou 2 trades chanceux;
- degradation acceptable quand on change de fournisseur cheap vers OpenAI.

### Phase 4 - shadow live

Duree recommandee: minimum 2 semaines, idealement 4.

Mode:

- aucun ordre;
- l'agent produit des propositions;
- le risk gate donne `would_accept` ou `would_reject`;
- le runner calcule le fill theorique avec `DryRunExecutionVenue`;
- toutes les decisions sont visibles dans dashboard/review.

Promotion vers testnet seulement si:

- zero incident schema non gere;
- zero proposition sur symbole non whitelist;
- zero decision stale acceptee;
- taux de timeout acceptable;
- cout mensuel projete sous budget;
- les propositions auraient respecte caps et stops;
- les mauvaises decisions sont explicables et limitees.

### Phase 5 - testnet live

Mode:

- vrais ordres testnet Hyperliquid;
- sous-univers `BTC/ETH/SOL/HYPE`;
- notional 25 USD;
- 1 position max;
- 1x;
- SL reduce-only obligatoire.

Objectif: valider execution, state, reconciliation, restarts, close reduce-only,
rounding, rate-limit, crash recovery.

Ce n'est pas une preuve de PnL mainnet.

### Phase 6 - mainnet paper separe

Mode:

- donnees mainnet;
- paper execution;
- meme latence que production;
- memes caps que live tiny;
- pas de vrais ordres.

Promotion vers canary mainnet uniquement si:

- au moins 72h sans incident technique;
- puis idealement 2 semaines avec PnL/risque acceptable;
- aucune divergence state/exchange simulee;
- aucune inflation de cout;
- review manuelle du rapport.

### Phase 7 - mainnet canary tiny

Conditions:

- compte Hyperliquid independant finance avec une somme limitee;
- confirmation manuelle explicite;
- `live_max_order_notional_usd` dedie `tridentAI <= 25`;
- max daily loss hard;
- alertes crash;
- dashboard montre ouvert/ferme, cout IA, dernier prompt, dernier rejet;
- kill-switch teste avant lancement.

Critere d'arret immediat:

- position inconnue;
- SL absent;
- open order inconnu;
- model output non parse;
- cout journalier > budget;
- perte journaliere atteinte;
- plus de N rejects execution consecutifs;
- divergence entre close attendu et close exchange;
- news/social provider retourne donnees incoherentes.

## Pertinence de l'agent IA en trading live

L'agent IA peut etre utile pour:

- condenser plusieurs horizons de features;
- detecter contradictions dans un setup;
- qualifier des news/social;
- produire des raisons de veto;
- adapter le niveau de prudence a un regime narratif;
- faire de la revue post-trade.

L'agent IA est dangereux pour:

- scalping minute par minute si chaque tick devient une "opinion";
- gestion directe du levier;
- interpretation de rumeurs;
- reaction a des inputs manipules;
- trading sans stop;
- auto-amelioration non bornee;
- changement de prompt en production sans replay.

Recommandation: commencer par `AI risk overlay` et `AI proposal engine`, pas par
"IA autonome qui clique sur l'exchange".

## Fournisseurs IA et couts

Sources officielles consultees le `2026-06-07`:

- OpenAI pricing: https://developers.openai.com/api/docs/pricing
- OpenAI model comparison: https://developers.openai.com/api/docs/models/compare
- Anthropic pricing: https://platform.claude.com/docs/en/about-claude/pricing
- Gemini pricing: https://ai.google.dev/gemini-api/docs/pricing
- xAI pricing: https://docs.x.ai/developers/pricing
- DeepSeek pricing: https://api-docs.deepseek.com/quick_start/pricing/
- Mistral pricing: https://mistral.ai/pricing/

### Prix API texte principaux

Prix par `1M tokens`, hors taxes et hors frais d'outils.

| Fournisseur | Modele | Input | Cached input | Output | Usage recommande |
|---|---:|---:|---:|---:|---|
| OpenAI | `gpt-5.4-nano` | 0.20 USD | 0.02 USD | 1.25 USD | classification/realtime cheap |
| OpenAI | `gpt-5.4-mini` | 0.75 USD | 0.075 USD | 4.50 USD | decision JSON principale si nano insuffisant |
| OpenAI | `gpt-5.4` | 2.50 USD | 0.25 USD | 15.00 USD | critic/verifier sur candidats |
| OpenAI | `gpt-5.5` | 5.00 USD | 0.50 USD | 30.00 USD | offline reviews, pas boucle continue au debut |
| Anthropic | Claude Haiku 4.5 | 1.00 USD | 0.10 USD | 5.00 USD | cheap Claude, classification |
| Anthropic | Claude Sonnet 4.6 | 3.00 USD | 0.30 USD | 15.00 USD | critic/reasoning |
| Anthropic | Claude Opus 4.8 | 5.00 USD | 0.50 USD | 25.00 USD | offline deep review |
| Gemini | `gemini-2.5-flash-lite` | 0.10 USD | 0.01 USD | 0.40 USD | meilleur cout pour shadow |
| Gemini | `gemini-2.5-flash` | 0.30 USD | 0.03 USD | 2.50 USD | bon compromis |
| Gemini | `gemini-2.5-pro` | 1.25 USD | 0.125 USD | 10.00 USD | critic ponctuel |
| Gemini | `gemini-3-flash-preview` | 0.50 USD | 0.05 USD | 3.00 USD | speed + grounding, preview |
| xAI | `grok-4.3` | 1.25 USD | 0.20 USD | 2.50 USD | X/web search integre, si besoin X |
| DeepSeek | `deepseek-v4-flash` | 0.14 USD cache miss | 0.0028 USD | 0.28 USD | cout minimal, verifier contraintes fournisseur |
| DeepSeek | `deepseek-v4-pro` | 0.435 USD cache miss | 0.003625 USD | 0.87 USD | cheap verifier |
| Mistral | Mistral Medium 3.5 | 1.50 USD | n/a | 7.50 USD | option Europe/open-weight |
| Mistral | Mistral Large | 2.00 USD | n/a | 6.00 USD | option generaliste |

### Frais search/outils utiles

| Fournisseur | Outil | Prix | Commentaire |
|---|---|---:|---|
| OpenAI | Web search | 10 USD / 1k calls | plus tokens de contenu au tarif modele |
| Anthropic | Web search | 10 USD / 1k searches | plus tokens standard |
| Gemini | Google Search grounding 2.5 | 1,500 RPD gratuits paid tier puis 35 USD / 1k grounded prompts | interessant si faible frequence |
| Gemini | Gemini 3 Search grounding | 5,000 prompts/mois gratuits puis 14 USD / 1k search queries | utile pour news ciblees |
| xAI | Web Search | 5 USD / 1k calls | moins cher que OpenAI/Anthropic |
| xAI | X Search | 5 USD / 1k calls | meilleur acces X cible sans X API directe |

### Hypotheses de cout mensuel

Scenario shadow raisonnable:

- decision marche toutes les 15 minutes;
- digest news/social toutes les 30 minutes;
- `5k input + 500 output` par cycle marche;
- `2k input + 300 output` par digest news;
- `2,880` cycles marche/mois;
- `1,440` digests news/mois;
- total modele: environ `17.3M input` et `1.87M output` par mois;
- search cible: `2,880` appels/mois si 2 recherches par digest.

Estimation modele seul:

| Modele | Cout mensuel modele seul | Avec xAI X Search 2,880 calls | Avec OpenAI/Anthropic web search 2,880 calls |
|---|---:|---:|---:|
| OpenAI `gpt-5.4-nano` | ~5.8 USD | ~20.2 USD | ~34.6 USD |
| OpenAI `gpt-5.4-mini` | ~21.4 USD | ~35.8 USD | ~50.2 USD |
| OpenAI `gpt-5.4` | ~71.3 USD | ~85.7 USD | ~100.1 USD |
| OpenAI `gpt-5.5` | ~142.6 USD | ~157.0 USD | ~171.4 USD |
| Gemini `2.5 Flash-Lite` | ~2.5 USD | ~16.9 USD | n/a si grounding Gemini reste sous quota |
| Gemini `2.5 Flash` | ~9.9 USD | ~24.3 USD | n/a si grounding Gemini reste sous quota |
| Gemini `2.5 Pro` | ~40.3 USD | ~54.7 USD | n/a si grounding Gemini reste sous quota |
| Claude Haiku 4.5 | ~26.6 USD | ~41.0 USD | ~55.4 USD |
| Claude Sonnet 4.6 | ~79.9 USD | ~94.3 USD | ~108.7 USD |
| xAI `grok-4.3` | ~26.3 USD | ~40.7 USD | n/a |
| DeepSeek `v4-flash` cache miss | ~2.9 USD | ~17.3 USD | n/a |

Scenario verifier ponctuel:

- 200 propositions candidates/mois;
- `20k input + 1.5k output` par verification;
- total `4M input`, `0.3M output`.

| Verifier | Cout mensuel approx |
|---|---:|
| OpenAI `gpt-5.4` | ~14.5 USD |
| OpenAI `gpt-5.5` | ~29.0 USD |
| Claude Sonnet 4.6 | ~16.5 USD |
| Claude Opus 4.8 | ~27.5 USD |
| Gemini `2.5 Pro` | ~8.0 USD |
| DeepSeek `v4-pro` | ~2.0 USD |

Scenario non recommande "LLM chaque minute":

- decision marche toutes les minutes;
- `43,200` cycles/mois;
- `216M input`, `21.6M output`;
- hors news/search.

| Modele | Cout mensuel approx |
|---|---:|
| Gemini `2.5 Flash-Lite` | ~30 USD |
| DeepSeek `v4-flash` cache miss | ~36 USD |
| OpenAI `gpt-5.4-nano` | ~70 USD |
| Gemini `2.5 Flash` | ~119 USD |
| OpenAI `gpt-5.4-mini` | ~259 USD |
| OpenAI `gpt-5.4` | ~864 USD |
| Claude Sonnet 4.6 | ~972 USD |

Le cout n'est donc pas seulement une question de fournisseur. Le vrai levier
est la frequence d'appel et la quantite de contexte. L'agent doit etre appele
sur shortlist, pas sur chaque tick.

### Recommandation fournisseur

MVP priorite OpenAI:

- `gpt-5.4-nano` pour classification/realtime JSON si les evals prouvent que le
  JSON est stable;
- `gpt-5.4-mini` comme modele principal si nano est trop fragile;
- `gpt-5.4` comme verifier ponctuel des propositions d'ouverture;
- eviter `gpt-5.5` en boucle continue tant que le capital et le PnL ne
  justifient pas le cout;
- utiliser web search OpenAI seulement pour verification ponctuelle, pas chaque
  cycle.

Alternative cout minimal:

- Gemini `2.5 Flash-Lite` ou DeepSeek `v4-flash` pour shadow;
- OpenAI `gpt-5.4` seulement comme judge/verifier;
- xAI `X Search` pour social cible si X apporte une vraie information.

Alternative review haut niveau:

- Claude Sonnet/Opus ou OpenAI `gpt-5.5` pour revues offline, autopsies et
  rapports, pas pour execution minute.

## Plan d'implementation

### Etape 1 - Spec et types

Ajouter:

- `docs/trident_ai_agent_plan.md` present document;
- `app/trident_ai/types.py`;
- schemas `AgentMarketContext`, `AgentIntelDigest`, `AgentTradeProposal`,
  `AgentDecisionBundle`;
- tests de validation schema.

Pas d'appel LLM, pas d'ordre.

Livrable minimal de cette premiere etape:

- un package `app/trident_ai/`;
- des dataclasses ou schemas Pydantic pour `AgentMarketContext`,
  `AgentIntelDigest`, `AgentTradeProposal`, `AgentDecisionBundle`;
- un validateur `validate_agent_proposal()` fail-closed;
- des fixtures JSON couvrant `BTC`, `ETH`, `SOL`, `HYPE`;
- des tests unitaires prouvant que les propositions invalides sont rejetees.

Cette etape ne requiert pas encore de cle API OpenAI, ni de compte Hyperliquid
independant, car elle teste seulement le contrat de donnees et les refus.

### Etape 2 - Config

Ajouter `config/trident_ai.toml` et dataclasses:

```toml
[trident_ai]
enabled = false
mode = "shadow"
max_monthly_ai_budget_usd = 30
decision_interval_seconds = 900
max_symbols_per_cycle = 5
tradable_symbols = ["BTC", "ETH", "SOL", "HYPE"]

[trident_ai.paths]
runtime_dir = "./runtime/trident_ai"
llm_cache_dir = "./runtime/trident_ai/llm_cache"
replay_output_dir = "./server-data/replay_reports"

[trident_ai.risk]
live_max_order_notional_usd = 25
max_daily_loss_usd = 5
max_open_positions = 1
max_trades_per_day = 3
max_leverage = 1.0
require_stop = true
require_evidence = true

[trident_ai.llm]
provider = "openai"
model = "gpt-5.4-mini"
verifier_provider = "openai"
verifier_model = "gpt-5.4"
temperature = 0.1
timeout_seconds = 20
```

### Etape 3 - Provider abstraction

Ajouter un client LLM minimal:

- OpenAI Responses API avec structured output;
- fallback Gemini/Claude possible plus tard;
- tracking token/cout;
- cache prompt/output;
- timeout fail-closed;
- aucun outil execution.

### Etape 4 - Feature builder

Construire un contexte compact depuis `SymbolMarketSnapshot`.

Tests:

- valeurs manquantes;
- symboles tradfi ignores au debut;
- stale snapshots;
- features hors bornes;
- serialisation stable.

### Etape 5 - Agent shadow

Runner shadow:

- lit snapshots live;
- construit les contextes `BTC/ETH/SOL/HYPE`;
- rejette les symboles hors univers initial;
- utilise un agent deterministe injectable tant que le LLM n'est pas evalue;
- risk gate `would_accept`;
- ecrit `logs/trident_ai_shadow.jsonl`;
- ecrit `logs/trident_ai_status.json`.

Pas d'execution.

Livrable implemente:

- `app/trident_ai/shadow_runner.py`;
- `DeterministicShadowAgent`;
- `TridentAIShadowRunner`;
- `run_trident_ai_shadow()`;
- tests `tests/test_trident_ai_shadow_runner.py`.

Ce runner valide la plomberie en replay/shadow: lecture de snapshots JSONL,
construction des features, generation de propositions, validation fail-closed,
journalisation des decisions et status runtime. Il n'appelle pas de fournisseur
LLM, ne lit pas de cle Hyperliquid et ne peut pas envoyer d'ordre.

### Etape 6 - Replay/cache LLM

Ajouter runner replay:

- input `server-data/...jsonl`;
- output `server-data/replay_reports/trident_ai_*`;
- cache LLM obligatoire;
- comparaison baseline full-bot.

Livrable implemente:

- `app/trident_ai/replay.py`;
- `TridentAILLMReplayRunner`;
- `run_trident_ai_llm_replay()`;
- `build_trade_proposal_request()`;
- cache obligatoire via `JSONFileLLMCache`;
- mode par defaut `allow_live_llm_calls=false`;
- limites smoke `max_records`, `max_contexts` et `symbols`;
- limites de remplissage cache `max_live_calls` et
  `max_incremental_cost_usd` obligatoires si `allow_live_llm_calls=true`;
- journal JSONL `trident_ai_llm_replay_*.jsonl`;
- rapport JSON/Markdown `trident_ai_llm_replay_*.json|md`;
- tests `tests/test_trident_ai_llm_replay.py`.

Le runner echoue ferme si le cache ne contient pas la reponse attendue et que
les appels live LLM sont interdits. Il journalise le cache key, le modele,
l'usage tokens, le cout estime original, le cout incremental du run, la
proposition parse et le verdict de validation. Le rapport compare deja
`tridentAI` a une baseline `hold` et reference les rapports full-bot officiels.
La comparaison PnL full-bot reste volontairement `reference_only` tant que
l'etape 7 n'a pas branche un executor dry-run/paper reproductible.

#### Recommandation pour lancer un premier replay

On peut lancer un replay technique des maintenant, mais pas encore un replay LLM
large.

Ordre recommande:

1. Lancer d'abord un replay shadow deterministe sur
   `server-data/replay_inputs/full_bot_latest_fetch.jsonl`.
   Objectif: verifier lecture snapshots, filtrage `BTC/ETH/SOL/HYPE`, rejets,
   journal et status, sans cout API.
2. Lancer ensuite le replay LLM en mode cache-only. Si le cache est vide, le
   resultat attendu est un fail-closed propre avec
   `cache_miss_live_calls_disabled`; cela valide que le runner ne part pas en
   appels fournisseur par surprise.
3. Avant tout replay LLM avec `allow_live_llm_calls=true`, ajouter une limite
   stricte dans le runner: `max_records`, `max_contexts`, et idealement
   `symbols`. Premier smoke recommande: `max_records=20`,
   `max_contexts=50`, univers `BTC/ETH/SOL/HYPE`.
4. Remplir le cache sur ce smoke uniquement, puis rejouer le meme input en
   cache-only pour verifier la reproductibilite.
5. N'elargir la fenetre qu'apres avoir controle:
   taux de JSON valides, taux de rejets, cout estime, latence, propositions
   `open`, et raisons de veto.

Ne pas lancer le replay LLM sur un fichier complet de plusieurs dizaines ou
centaines de Mo avec appels live actives tant que ces limites ne sont pas
implementees. Le risque principal n'est pas l'execution d'ordre, impossible a ce
stade, mais le cout API et la generation d'un cache non borne.

#### Premier replay smoke lance

Date: `2026-06-07`.

Input:

- `server-data/replay_inputs/full_bot_latest_fetch.jsonl`

Parametres:

- `max_records=20`;
- `max_contexts=50`;
- `symbols=["BTC", "ETH", "SOL", "HYPE"]`;
- `allow_live_llm_calls=false` pour le replay LLM.

Sorties:

- `server-data/replay_reports/trident_ai_first_shadow_smoke_20260607.jsonl`;
- `server-data/replay_reports/trident_ai_first_shadow_smoke_20260607_status.json`;
- `server-data/replay_reports/trident_ai_first_llm_cache_smoke_20260607.jsonl`;
- `server-data/replay_reports/trident_ai_first_llm_cache_smoke_20260607.json`;
- `server-data/replay_reports/trident_ai_first_llm_cache_smoke_20260607.md`.

Resultats:

| Replay | Records | Contextes | Cache hits | Appels live | Propositions | Verdict |
|---|---:|---:|---:|---:|---:|---|
| Shadow deterministe | 17 | 50 | n/a | 0 | 50 accepted, 0 rejected | OK |
| LLM cache-only | 17 | 50 | 0/50 | 0 | 0 generated | OK fail-closed |

Details shadow:

- symboles traites: `BTC=17`, `ETH=16`, `SOL=17`, `HYPE=0`;
- actions: `hold=50`;
- validations: `accepted=50`.

Details LLM cache-only:

- `llm_failures=50`;
- raison unique: `cache_miss_live_calls_disabled`;
- cout incremental: `0.00 USD`;
- aucun appel OpenAI effectue.

Conclusion: la plomberie replay est validee sur un smoke borne. Le prochain
travail utile est de lancer un remplissage cache OpenAI reel ultra-borne, puis
de rejouer le meme input en cache-only pour verifier la reproductibilite des
sorties.

#### Mode remplissage cache LLM securise

Statut: implemente et lance sur un smoke OpenAI ultra-borne le `2026-06-07`.

Garde-fous:

- `allow_live_llm_calls=true` refuse de demarrer sans `max_live_calls`;
- `allow_live_llm_calls=true` refuse de demarrer sans
  `max_incremental_cost_usd`;
- le runner relit toujours le cache avant un appel live;
- si `max_live_calls` est atteint: refus `live_call_limit_reached`;
- si la reserve de cout estime depasse le budget restant: refus
  `incremental_cost_budget_exhausted`;
- une reponse live OK est ecrite dans le cache avant le rapport;
- les appels bloques avant fournisseur ne sont pas comptes comme appels live;
- le client OpenAI applique `trident_ai.llm.max_retries` uniquement sur erreurs
  transitoires `429/500/502/503/504`.
- CLI disponible: `uv run python -m app.trident_ai.cli`.
- fichier env local supporte: `.env.tridentai`, ignore par git.

Budget operateur OpenAI courant:

- compte credite a `5 USD`;
- ne jamais lancer un replay live LLM sans plafond local explicite;
- plafond recommande par tranche exploratoire: `0.05` a `0.10 USD`;
- plafond maximum avant validation intermediaire: `0.25 USD`;
- budget recherche tridentAI a preserver: garder au moins `4 USD` de reserve
  pendant les etapes 7/8;
- ordre de travail obligatoire avant tout nouveau remplissage payant:
  1. lancer un replay `cache-only` plus large, sans `--allow-live-llm-calls`;
  2. analyser le nombre de cache misses et la fenetre couverte;
  3. remplir le cache par petits lots avec `--max-live-calls` bas et
     `--max-incremental-cost-usd <= 0.10`;
  4. relancer immediatement le meme replay en `cache-only`;
  5. lancer seulement ensuite `paper-replay`.

Fichier env local recommande:

```bash
cp .env.tridentai.example .env.tridentai
chmod 600 .env.tridentai
$EDITOR .env.tridentai
```

Contenu attendu:

```text
OPENAI_API_KEY=...
```

Remplissage reel recommande pour la version active `trident_ai_replay_v2`:

```bash
uv run python -m app.trident_ai.cli llm-replay \
  --input server-data/replay_inputs/full_bot_latest_fetch.jsonl \
  --journal-path server-data/replay_reports/trident_ai_openai_cache_fill_v2_20260607.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_openai_cache_fill_v2_20260607.json \
  --report-md-path server-data/replay_reports/trident_ai_openai_cache_fill_v2_20260607.md \
  --max-records 20 \
  --max-contexts 10 \
  --symbols BTC,ETH,SOL,HYPE \
  --allow-live-llm-calls \
  --max-live-calls 10 \
  --max-incremental-cost-usd 0.05
```

Pour utiliser un autre fichier:

```bash
uv run python -m app.trident_ai.cli llm-replay --env-file /chemin/vers/env ...
```

Apres ce remplissage, relancer immediatement le meme replay avec
`allow_live_llm_calls=false`. Le deuxieme run doit afficher `cache_hits=10/10`,
`live_llm_calls=0` et les memes propositions parsees.

Commande cache-only validee via CLI:

```bash
uv run python -m app.trident_ai.cli llm-replay \
  --input server-data/replay_inputs/full_bot_latest_fetch.jsonl \
  --journal-path server-data/replay_reports/trident_ai_cli_llm_cache_smoke_20260607.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_cli_llm_cache_smoke_20260607.json \
  --report-md-path server-data/replay_reports/trident_ai_cli_llm_cache_smoke_20260607.md \
  --max-records 20 \
  --max-contexts 10 \
  --symbols BTC,ETH,SOL,HYPE
```

Resultat cache-only CLI: `records_processed=4`, `contexts_built=10`,
`llm_requests=10`, `cache_hits=0`, `live_llm_calls=0`,
`rejection_reasons={"cache_miss_live_calls_disabled": 10}`.

Tentative cache-fill sans variable `OPENAI_API_KEY` visible:

- resultat: `rejection_reasons={"missing_api_key": 10}`;
- aucun token facture;
- aucune proposition generee;
- cache non rempli;
- correction implementation: `missing_api_key` et `unsupported_provider` ne
  sont plus comptes comme appels fournisseur reels dans `live_llm_calls`.

Cache-fill OpenAI reel v1:

- input: `server-data/replay_inputs/full_bot_latest_fetch.jsonl`;
- limites: `max_records=20`, `max_contexts=10`, `max_live_calls=10`,
  `max_incremental_cost_usd=0.05`;
- resultat: `llm_requests=10`, `live_llm_calls=10`, `llm_failures=0`;
- tokens: `input_tokens=9940`, `output_tokens=4710`;
- cout estime: `0.02865 USD`;
- actions: `hold=10`;
- propositions: `10 generated`, `6 accepted`, `4 rejected`;
- rejets: `confidence_below_min=2`, `invalid_notional=2`;
- sorties:
  `server-data/replay_reports/trident_ai_openai_cache_fill_20260607.*`;
- securite: la cle OpenAI collee dans le transcript doit etre revoquee et
  remplacee.

Rejeu cache-only apres remplissage:

- sortie:
  `server-data/replay_reports/trident_ai_openai_cache_replay_20260607.*`;
- `cache_hits=10/10`;
- `live_llm_calls=0`;
- `incremental_cost_usd=0.0`;
- propositions identiques au cache-fill.

Conclusion: le cache LLM est reproductible. Le point qualite a corriger avant
un replay plus large est le format des `hold`: certains retours `hold` gardent
des champs numeriques invalides ou une confidence sous le seuil, donc le prompt
ou le schema de sortie doit imposer des valeurs valides meme pour `hold`.

Correction v2 appliquee:

- `TRIDENT_AI_REPLAY_PROMPT_VERSION = "trident_ai_replay_v2"`;
- schema JSON: `max_notional_usd`, `max_leverage` et `invalidation_price`
  acceptent `0.0`;
- validation: `hold` et `close_only_mode` peuvent avoir notional/leverage `0`;
- validation: seuil de confidence et notional positif restent obligatoires pour
  les actions executables `open`, `close`, `reduce`;
- prompt: contrat explicite pour `hold` avec champs zeros ordinaires;
- prompt: interdiction de notation scientifique et de floats subnormaux.

Replay cache-only v2 avant remplissage:

- sortie:
  `server-data/replay_reports/trident_ai_openai_cache_replay_v2_prefill_20260607.*`;
- resultat attendu et observe: `cache_hits=0`, `live_llm_calls=0`,
  `rejection_reasons={"cache_miss_live_calls_disabled": 10}`;
- conclusion: l'ancien cache v1 n'est pas reutilise par le prompt v2.

Remplissage cache OpenAI v2 du `2026-06-07` avec cle renouvelee:

- sortie:
  `server-data/replay_reports/trident_ai_openai_cache_fill_v2_20260607.*`;
- limites: `max_records=20`, `max_contexts=10`, `max_live_calls=10`,
  `max_incremental_cost_usd=0.05`;
- resultat: `llm_requests=10`, `live_llm_calls=10`, `llm_failures=2`;
- cause: `http_error:503=2`, erreurs fournisseur transitoires;
- tokens: `input_tokens=9701`, `output_tokens=2227`;
- cout estime: `0.01729725 USD`;
- actions: `hold=8`;
- propositions: `8 generated`, `8 accepted`, `0 rejected`.

Rejeu cache-only v2 apres remplissage partiel:

- sortie:
  `server-data/replay_reports/trident_ai_openai_cache_replay_v2_after_partial_fill_20260607.*`;
- resultat: `cache_hits=8/10`, `live_llm_calls=0`,
  `rejection_reasons={"cache_miss_live_calls_disabled": 2}`;
- conclusion: le cache v2 contient les 8 reponses valides; les 2 contextes
  manquants doivent etre repris avec un retry borne.

Retry execute pour completer les deux trous de cache v2:

```bash
uv run python -m app.trident_ai.cli llm-replay \
  --input server-data/replay_inputs/full_bot_latest_fetch.jsonl \
  --journal-path server-data/replay_reports/trident_ai_openai_cache_fill_v2_retry_20260607.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_openai_cache_fill_v2_retry_20260607.json \
  --report-md-path server-data/replay_reports/trident_ai_openai_cache_fill_v2_retry_20260607.md \
  --max-records 20 \
  --max-contexts 10 \
  --symbols BTC,ETH,SOL,HYPE \
  --allow-live-llm-calls \
  --max-live-calls 2 \
  --max-incremental-cost-usd 0.02
```

- sortie:
  `server-data/replay_reports/trident_ai_openai_cache_fill_v2_retry_20260607.*`;
- resultat: `cache_hits=8/10`, `live_llm_calls=2`, `llm_failures=0`;
- cout incremental: `0.00415725 USD`;
- cout original estime total v2: `0.0214545 USD`;
- propositions: `10 generated`, `10 accepted`, `0 rejected`;
- actions: `hold=10`.

Rejeu cache-only v2 final:

- sortie:
  `server-data/replay_reports/trident_ai_openai_cache_replay_v2_complete_20260607.*`;
- resultat: `cache_hits=10/10`, `live_llm_calls=0`, `llm_failures=0`;
- cout incremental: `0.0 USD`;
- propositions: `10 generated`, `10 accepted`, `0 rejected`;
- actions: `hold=10`;
- conclusion: le smoke OpenAI v2 est maintenant entierement reproductible par
  cache et ne propose aucune ouverture sur cette fenetre.

### Etape 7 - Dry-run/paper

Statut: implemente et smoke v2 lance le `2026-06-07`.

Livrables:

- `app/trident_ai/paper.py`;
- commande CLI `uv run python -m app.trident_ai.cli paper-replay`;
- config dediee `[trident_ai.paper]`;
- journal paper `trident_ai_paper_*.jsonl`;
- rapport JSON/Markdown avec PnL realise, PnL latent, frais, cout IA net et
  buckets de calibration confidence.

Config paper par defaut:

- `taker_fee_bps=3.5`;
- `slippage_bps=0.5`;
- `spread_multiplier=0.5`;
- `force_close_at_end=true`.

Commande smoke executee:

```bash
uv run python -m app.trident_ai.cli paper-replay \
  --input server-data/replay_reports/trident_ai_openai_cache_replay_v2_complete_20260607.jsonl \
  --journal-path server-data/replay_reports/trident_ai_paper_smoke_v2_20260607.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_paper_smoke_v2_20260607.json \
  --report-md-path server-data/replay_reports/trident_ai_paper_smoke_v2_20260607.md
```

Resultat smoke:

- `decisions_seen=10`;
- `proposals_seen=10`, `proposals_accepted=10`, `proposals_rejected=0`;
- `action_counts={"hold": 10}`;
- `fills=0`;
- `positions_opened=0`, `positions_reduced=0`, `positions_closed=0`;
- `realized_pnl_usd=0.0`, `unrealized_pnl_usd=0.0`;
- `ai_cost_usd=0.0214545`;
- `net_after_ai_cost_usd=-0.0214545`;
- conclusion: la chaine proposition LLM -> paper executor -> rapport PnL/cout
  fonctionne sur un smoke reproductible; cette fenetre ne contient aucun trade.

Prochaine validation paper:

- lancer un replay plus large en cache-only, cout `0 USD`;
- verifier qu'au moins quelques propositions `open/close/reduce` apparaissent
  avant de juger la calibration;
- comparer le PnL paper `tridentAI` a la baseline full-bot officielle, sans
  promotion live.

Commande zero-cout recommandee avant tout nouveau call OpenAI:

```bash
uv run python -m app.trident_ai.cli llm-replay \
  --input server-data/replay_inputs/full_bot_latest_fetch.jsonl \
  --journal-path server-data/replay_reports/trident_ai_cache_only_scan_100_20260608.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_cache_only_scan_100_20260608.json \
  --report-md-path server-data/replay_reports/trident_ai_cache_only_scan_100_20260608.md \
  --max-records 100 \
  --max-contexts 100 \
  --symbols BTC,ETH,SOL,HYPE
```

Resultat du scan zero-cout execute le `2026-06-08`:

- sortie:
  `server-data/replay_reports/trident_ai_cache_only_scan_100_20260608.*`;
- `records_processed=34`;
- `contexts_built=100`;
- `cache_hits=10/100`;
- `live_llm_calls=0`;
- `incremental_cost_usd=0.0`;
- `llm_failures=90`;
- `rejection_reasons={"cache_miss_live_calls_disabled": 90}`;
- fenetre couverte: `2026-05-21T00:00:00Z` ->
  `2026-05-21T00:33:00Z`;
- conclusion: le prochain remplissage payant utile doit viser seulement une
  petite tranche de ces `90` trous de cache, pas tout le replay.

Si ce scan montre assez de contextes utiles, premier remplissage payant
recommande, borne a quelques centimes:

```bash
uv run python -m app.trident_ai.cli llm-replay \
  --input server-data/replay_inputs/full_bot_latest_fetch.jsonl \
  --journal-path server-data/replay_reports/trident_ai_openai_cache_fill_v2_cheap_batch_005_20260608.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_openai_cache_fill_v2_cheap_batch_005_20260608.json \
  --report-md-path server-data/replay_reports/trident_ai_openai_cache_fill_v2_cheap_batch_005_20260608.md \
  --max-records 100 \
  --max-contexts 40 \
  --symbols BTC,ETH,SOL,HYPE \
  --allow-live-llm-calls \
  --max-live-calls 20 \
  --max-incremental-cost-usd 0.05
```

Resultat du mini-batch payant execute le `2026-06-08`:

- sortie:
  `server-data/replay_reports/trident_ai_openai_cache_fill_v2_cheap_batch_005_20260608.*`;
- plafond: `max_incremental_cost_usd=0.05`;
- cout incremental reel: `0.04329675 USD`;
- `contexts_built=40`;
- `cache_hits=10`;
- `live_llm_calls=20`;
- `llm_failures=10`, cause `live_call_limit_reached`;
- propositions: `30 generated`, `30 accepted`, `0 rejected`;
- actions: `hold=30`;
- conclusion budget: le garde-fou de cout a fonctionne et le compte OpenAI
  reste preserve;
- conclusion qualite: le remplissage chronologique continue a produire
  uniquement des `hold`, donc il ne faut pas depenser plus sur cette fenetre
  sans preselection de contextes.

Rejeu cache-only apres mini-batch:

- sortie:
  `server-data/replay_reports/trident_ai_cache_only_scan_100_after_cheap_batch_005_20260608.*`;
- `cache_hits=30/100`;
- `live_llm_calls=0`;
- `incremental_cost_usd=0.0`;
- `rejection_reasons={"cache_miss_live_calls_disabled": 70}`.

Paper replay apres mini-batch:

- sortie:
  `server-data/replay_reports/trident_ai_paper_scan_100_after_cheap_batch_005_20260608.*`;
- `decisions_seen=100`;
- `proposals_seen=30`;
- `action_counts={"hold": 30}`;
- `fills=0`;
- `positions_opened=0`, `positions_closed=0`;
- `ai_cost_usd=0.06475125`;
- `net_after_ai_cost_usd=-0.06475125`.

Decision avant prochain appel OpenAI:

- ne pas continuer les cache-fill chronologiques;
- ajouter un scan zero-cout de scoring deterministe des contextes marche;
- selectionner seulement les contextes avec forte confluence directionnelle ou
  regime exploitable;
- ensuite remplir un nouveau mini-batch payant encore plus cible, plafond
  `0.05 USD`.

Scanner candidat zero-cout implemente le `2026-06-08`:

- `app/trident_ai/candidate_scan.py`;
- commande CLI `uv run python -m app.trident_ai.cli candidate-scan`;
- scoring local sur `AgentMarketContext`;
- selection par score, dedupe `(timestamp, symbol)`;
- generation d'un JSONL d'entree utilisable par `llm-replay`;
- aucun appel LLM, cout API `0 USD`.

Commande scan candidat executee:

```bash
uv run python -m app.trident_ai.cli candidate-scan \
  --input server-data/replay_inputs/full_bot_latest_fetch.jsonl \
  --journal-path server-data/replay_reports/trident_ai_candidate_scan_top40_dedup_20260608.jsonl \
  --selected-input-path server-data/replay_inputs/trident_ai_candidate_top40_dedup_20260608.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_candidate_scan_top40_dedup_20260608.json \
  --report-md-path server-data/replay_reports/trident_ai_candidate_scan_top40_dedup_20260608.md \
  --max-records 100 \
  --max-contexts 100 \
  --symbols BTC,ETH,SOL,HYPE \
  --top-n 40 \
  --min-score 1.25
```

Resultat scan candidat:

- `records_processed=34`;
- `contexts_scored=100`;
- `contexts_rejected=0`;
- `limit_reached=true`;
- `candidates_selected=4`;
- `side_counts={"long": 4}`;
- `symbol_counts={"BTC": 2, "ETH": 1, "SOL": 1}`;
- fenetre: `2026-05-21T00:00:00Z` -> `2026-05-21T00:33:00Z`;
- input cible:
  `server-data/replay_inputs/trident_ai_candidate_top40_dedup_20260608.jsonl`;
- conclusion: le prochain appel OpenAI doit utiliser cet input cible, pas
  `full_bot_latest_fetch.jsonl` en ordre chronologique.

Cache-only sur input candidat:

- sortie:
  `server-data/replay_reports/trident_ai_candidate_cache_only_top4_20260608.*`;
- `contexts_built=4`;
- `cache_hits=0/4`;
- `live_llm_calls=0`;
- `incremental_cost_usd=0.0`;
- `rejection_reasons={"cache_miss_live_calls_disabled": 4}`;
- conclusion: le prochain micro-batch cible ferait au plus `4` appels OpenAI.

Optimisation prompt `v3` executee le `2026-06-08` avant appel payant:

- `TRIDENT_AI_REPLAY_PROMPT_VERSION = "trident_ai_replay_v3"`;
- contexte envoye au LLM remplace par `ctx` compact;
- contexte complet conserve dans le journal pour audit;
- features LLM reduites a `22` signaux utiles:
  EMA/VWAP/structure/funding/spread/BTC-alignement, cluster, flow, book
  imbalance, notional/trades bucket, microprice, deltas, volume ratios,
  volatilite courte, compression et signaux externes;
- champs order-book/depth verbeux retires du prompt LLM;
- actions demandees au replay reduites a `hold/open`;
- sortie texte limitee a `<=3` tags/notes/evidences courts;
- mesure locale fixture:
  - contexte complet: `1644` caracteres;
  - contexte compact: `711` caracteres;
  - ratio: `0.4325`;
  - user prompt complet v3: `1382` caracteres;
  - system prompt v3: `300` caracteres.

Mini-batch payant candidat `v3` execute avec plafond strict:

```bash
uv run python -m app.trident_ai.cli llm-replay \
  --input server-data/replay_inputs/trident_ai_candidate_top40_dedup_20260608.jsonl \
  --journal-path server-data/replay_reports/trident_ai_openai_candidate_fill_v3_002_20260608.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_openai_candidate_fill_v3_002_20260608.json \
  --report-md-path server-data/replay_reports/trident_ai_openai_candidate_fill_v3_002_20260608.md \
  --max-records 10 \
  --max-contexts 4 \
  --symbols BTC,ETH,SOL,HYPE \
  --allow-live-llm-calls \
  --max-live-calls 4 \
  --max-incremental-cost-usd 0.02
```

Resultat:

- `contexts_built=4`;
- `live_llm_calls=4`;
- `llm_failures=0`;
- `prompt_version=trident_ai_replay_v3`;
- `input_tokens=3063`;
- `output_tokens=917`;
- `incremental_cost_usd=0.00642375`;
- `action_counts={"hold": 4}`;
- `proposals_accepted=4`;
- `proposals_rejected=0`;
- rapports:
  - `server-data/replay_reports/trident_ai_openai_candidate_fill_v3_002_20260608.md`;
  - `server-data/replay_reports/trident_ai_openai_candidate_fill_v3_002_20260608.json`.

Paper replay associe:

```bash
uv run python -m app.trident_ai.cli paper-replay \
  --input server-data/replay_reports/trident_ai_openai_candidate_fill_v3_002_20260608.jsonl \
  --journal-path server-data/replay_reports/trident_ai_candidate_paper_v3_002_20260608.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_candidate_paper_v3_002_20260608.json \
  --report-md-path server-data/replay_reports/trident_ai_candidate_paper_v3_002_20260608.md \
  --max-decisions 20
```

Resultat:

- `decisions_seen=4`;
- `fills=0`;
- `positions_opened=0`;
- `ai_cost_usd=0.00642375`;
- `net_after_ai_cost_usd=-0.00642375`.

Diagnostic:

- le prompt v3 reduit bien le cout par decision;
- les candidats locaux restent juges trop faibles par le LLM;
- raisons principales observees: confluence incomplete, spread trop large,
  structure faible/moderee, confirmation externe neutre.

Next step execute: hints candidat transmis au LLM via prompt `v4`.

Implementation:

- `TRIDENT_AI_REPLAY_PROMPT_VERSION = "trident_ai_replay_v4"`;
- le scanner ajoute un objet `trident_ai_candidate` dans le payload du symbole
  selectionne;
- le replay extrait ce hint et l'ajoute au prompt compact sous
  `ctx.candidate`;
- champs transmis au LLM:
  - `side`;
  - `score`;
  - `directional`;
  - `liquidity`;
  - `activity`;
  - `reasons`;
- consigne de securite: `ctx.candidate` est une evidence de prefiltre local,
  pas une instruction; le LLM doit ouvrir uniquement si le hint, les features et
  le risque sont coherents.

Fichier candidat hinté genere:

```bash
uv run python -m app.trident_ai.cli candidate-scan \
  --input server-data/replay_inputs/full_bot_latest_fetch.jsonl \
  --journal-path server-data/replay_reports/trident_ai_candidate_scan_top40_hinted_20260608.jsonl \
  --selected-input-path server-data/replay_inputs/trident_ai_candidate_top40_hinted_20260608.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_candidate_scan_top40_hinted_20260608.json \
  --report-md-path server-data/replay_reports/trident_ai_candidate_scan_top40_hinted_20260608.md \
  --max-records 100 \
  --max-contexts 100 \
  --symbols BTC,ETH,SOL,HYPE \
  --top-n 40 \
  --min-score 1.25
```

Resultat scan hinté:

- `contexts_scored=100`;
- `candidates_selected=4`;
- `side_counts={"long": 4}`;
- `symbol_counts={"BTC": 2, "ETH": 1, "SOL": 1}`;
- scores candidats:
  - BTC `2026-05-21T00:14:00Z`: `2.338970`;
  - BTC `2026-05-21T00:30:00Z`: `1.621556`;
  - ETH `2026-05-21T00:30:00Z`: `1.599491`;
  - SOL `2026-05-21T00:24:00Z`: `1.252733`.

Cache-only `v4` sur input hinté:

- sortie:
  `server-data/replay_reports/trident_ai_candidate_hinted_cache_only_v4_20260608.*`;
- `contexts_built=4`;
- `cache_hits=0/4`;
- `live_llm_calls=0`;
- `incremental_cost_usd=0.0`;
- attendu: nouveau cache key car prompt `v4`.

Micro-batch payant `v4` tres limite, execute pour tester l'effet du hint:

```bash
uv run python -m app.trident_ai.cli llm-replay \
  --input server-data/replay_inputs/trident_ai_candidate_top40_hinted_20260608.jsonl \
  --journal-path server-data/replay_reports/trident_ai_openai_candidate_hinted_fill_v4_001_20260608.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_openai_candidate_hinted_fill_v4_001_20260608.json \
  --report-md-path server-data/replay_reports/trident_ai_openai_candidate_hinted_fill_v4_001_20260608.md \
  --max-records 10 \
  --max-contexts 2 \
  --symbols BTC,ETH,SOL,HYPE \
  --allow-live-llm-calls \
  --max-live-calls 2 \
  --max-incremental-cost-usd 0.01
```

Resultat:

- `live_llm_calls=2`;
- `llm_failures=0`;
- `input_tokens=1721`;
- `output_tokens=515`;
- `incremental_cost_usd=0.00360825`;
- `action_counts={"hold": 1, "open": 1}`;
- premier candidat BTC: `open long`, confidence `0.67`, notional `25`,
  stop `22 bps`, TP `44 bps`, time stop `30m`;
- deuxieme candidat BTC: `hold`.

Paper replay associe:

- sortie:
  `server-data/replay_reports/trident_ai_candidate_hinted_paper_v4_001_20260608.*`;
- `decisions_seen=2`;
- `positions_opened=1`;
- `positions_closed=1`;
- `fees_usd=0.0175`;
- `realized_pnl_usd=-0.007469`;
- `ai_cost_usd=0.00360825`;
- `net_after_ai_cost_usd=-0.01107725`;
- conclusion: le hint debloque bien des `open`, mais l'echantillon est trop
  petit et negatif apres frais/cout AI.

Rapport de calibration local implemente et execute:

- `app/trident_ai/calibration.py`;
- commande CLI `uv run python -m app.trident_ai.cli calibration-report`;
- jointure locale entre:
  - input candidat hinté;
  - journal LLM;
  - journal paper;
- aucun appel OpenAI, cout API `0 USD`;
- rapport par candidat: score local, raisons scanner, action LLM, confidence,
  cout LLM, action paper, trade ferme, PnL et statut.

Commande executee:

```bash
uv run python -m app.trident_ai.cli calibration-report \
  --candidate-input server-data/replay_inputs/trident_ai_candidate_top40_hinted_20260608.jsonl \
  --llm-journal server-data/replay_reports/trident_ai_openai_candidate_hinted_fill_v4_001_20260608.jsonl \
  --paper-journal server-data/replay_reports/trident_ai_candidate_hinted_paper_v4_001_20260608.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_calibration_hinted_v4_001_20260608.json \
  --report-md-path server-data/replay_reports/trident_ai_calibration_hinted_v4_001_20260608.md
```

Resultat:

- `candidates_seen=4`;
- `matched_candidates=2`;
- `missing_llm_decisions=2`;
- `matched_paper_decisions=2`;
- `llm_action_counts={"hold": 1, "open": 1}`;
- `paper_action_counts={"no_op": 1, "open": 1}`;
- `closed_trades=1`;
- `winning_trades=0`;
- `losing_trades=1`;
- `realized_pnl_usd=-0.007469`;
- `ai_cost_usd=0.00360825`;
- `net_after_ai_cost_usd=-0.01107725`.

Lecture par score:

- bucket `>=2.00`: `1` candidat, `1` decision LLM, `1` open,
  `pnl_usd=-0.007469`;
- bucket `1.50-2.00`: `2` candidats, `1` decision LLM, `0` open,
  `pnl_usd=0.0`;
- bucket `<1.50`: `1` candidat, `0` decision LLM.

Conclusion:

- le hint local debloque le passage de `hold` a `open` sur le meilleur score;
- l'unique trade teste reste perdant apres frais/cout AI;
- l'echantillon est trop petit pour juger la strategie;
- les deux candidats non testes sont `ETH` score `1.599491` et `SOL` score
  `1.252733`.

Next step execute: scanner cout-ajuste.

Implementation:

- `TRIDENT_AI_REPLAY_PROMPT_VERSION = "trident_ai_replay_v5"`;
- `CANDIDATE_HINT_SCHEMA_VERSION = "trident_ai_candidate_hint_v2"`;
- le scanner conserve `raw_score`, puis calcule un `score` ajuste par cout;
- cout round-trip estime:
  `2 * taker_fee_bps + 2 * slippage_bps + 2 * spread_bps * spread_multiplier`;
- edge court estime par microprice, vwap distance, structure, trade flow,
  book imbalance et volatilite courte;
- nouveaux champs hintes au LLM:
  - `raw_score`;
  - `cost_score`;
  - `estimated_edge_bps`;
  - `round_trip_cost_bps`;
  - `edge_to_cost_ratio`;
- nouvelles raisons possibles:
  - `round_trip_cost_high`;
  - `cost_edge_ok`;
  - `cost_edge_watchlist`;
  - `cost_edge_marginal`;
  - `cost_edge_thin`.

Commande scan cout-ajuste executee:

```bash
uv run python -m app.trident_ai.cli candidate-scan \
  --input server-data/replay_inputs/full_bot_latest_fetch.jsonl \
  --journal-path server-data/replay_reports/trident_ai_candidate_scan_cost_adjusted_20260608.jsonl \
  --selected-input-path server-data/replay_inputs/trident_ai_candidate_cost_adjusted_20260608.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_candidate_scan_cost_adjusted_20260608.json \
  --report-md-path server-data/replay_reports/trident_ai_candidate_scan_cost_adjusted_20260608.md \
  --max-records 100 \
  --max-contexts 100 \
  --symbols BTC,ETH,SOL,HYPE \
  --top-n 40 \
  --min-score 1.25
```

Resultat scan cout-ajuste:

- `contexts_scored=100`;
- `candidates_selected=5`;
- `side_counts={"long": 3, "short": 2}`;
- `symbol_counts={"BTC": 1, "ETH": 2, "SOL": 2}`;
- le BTC `2026-05-21T00:14:00Z` qui avait ouvert puis perdu apres frais n'est
  plus selectionne;
- candidats selectionnes:
  - SOL long `2026-05-21T00:24:00Z`: score `1.440643`,
    raw `1.252733`, round-trip cost `12.391 bps`,
    edge/cost `1.253910`;
  - ETH short `2026-05-21T00:02:00Z`: score `1.428141`,
    raw `1.241862`, round-trip cost `8.9348 bps`,
    edge/cost `1.319880`;
  - ETH long `2026-05-21T00:17:00Z`: score `1.367886`,
    raw `1.189466`, round-trip cost `13.1372 bps`,
    edge/cost `1.255330`;
  - SOL short `2026-05-21T00:02:00Z`: score `1.290201`,
    raw `1.121914`, round-trip cost `12.6308 bps`,
    edge/cost `1.238420`;
  - BTC long `2026-05-21T00:30:00Z`: score `1.256553`,
    raw `1.621556`, round-trip cost `9.1561 bps`,
    edge/cost `0.880287`, raison `cost_edge_thin`.

Cache-only v5 sur input cout-ajuste:

- sortie:
  `server-data/replay_reports/trident_ai_candidate_cost_adjusted_cache_only_v5_20260608.*`;
- `contexts_built=5`;
- `cache_hits=0/5`;
- `live_llm_calls=0`;
- `incremental_cost_usd=0.0`;
- attendu: nouveau cache key car prompt `v5`.

Calibration historique cout-ajustee contre les journaux v4:

- sortie:
  `server-data/replay_reports/trident_ai_calibration_cost_adjusted_vs_v4_001_20260608.*`;
- `candidates_seen=5`;
- `matched_candidates=1`;
- `missing_llm_decisions=4`;
- seul overlap: BTC `2026-05-21T00:30:00Z`, deja juge `hold` par v4;
- `ai_cost_usd=0.00178425`;
- `realized_pnl_usd=0.0`;
- `net_after_ai_cost_usd=-0.00178425`.

Estimation avant appel payant:

- reserve conservative pour les 5 candidats v5: `0.0200505 USD`;
- reserve par candidat: environ `0.004 USD`.

Micro-batch payante top-2 executee:

- commande:

```bash
uv run python -m app.trident_ai.cli llm-replay \
  --input server-data/replay_inputs/trident_ai_candidate_cost_adjusted_20260608.jsonl \
  --journal-path server-data/replay_reports/trident_ai_openai_candidate_cost_adjusted_fill_v5_001_20260608.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_openai_candidate_cost_adjusted_fill_v5_001_20260608.json \
  --report-md-path server-data/replay_reports/trident_ai_openai_candidate_cost_adjusted_fill_v5_001_20260608.md \
  --max-records 10 \
  --max-contexts 2 \
  --symbols BTC,ETH,SOL,HYPE \
  --allow-live-llm-calls \
  --max-live-calls 2 \
  --max-incremental-cost-usd 0.01
```

- `contexts_built=2`;
- `live_llm_calls=2`;
- `incremental_cost_usd=0.0035535`, sous le cap `0.01 USD`;
- `input_tokens=1822`;
- `output_tokens=486`;
- `llm_failures=0`;
- `action_counts={"hold": 1, "open": 1}`;
- `proposals_accepted=2`;
- `proposals_rejected=0`;
- prompt `trident_ai_replay_v5`.

Paper replay associe:

```bash
uv run python -m app.trident_ai.cli paper-replay \
  --input server-data/replay_reports/trident_ai_openai_candidate_cost_adjusted_fill_v5_001_20260608.jsonl \
  --journal-path server-data/replay_reports/trident_ai_candidate_cost_adjusted_paper_v5_001_20260608.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_candidate_cost_adjusted_paper_v5_001_20260608.json \
  --report-md-path server-data/replay_reports/trident_ai_candidate_cost_adjusted_paper_v5_001_20260608.md \
  --max-decisions 20
```

- `decisions_seen=2`;
- `positions_opened=1`;
- `positions_closed=1`;
- `fees_usd=0.0175`;
- `gross_pnl_usd=-0.004837`;
- `realized_pnl_usd=-0.022337`;
- `ai_cost_usd=0.0035535`;
- `net_after_ai_cost_usd=-0.0258905`;
- l'unique close est forcee par `end_of_paper_replay`.

Calibration top-2 cout-ajustee:

```bash
uv run python -m app.trident_ai.cli calibration-report \
  --candidate-input server-data/replay_inputs/trident_ai_candidate_cost_adjusted_20260608.jsonl \
  --llm-journal server-data/replay_reports/trident_ai_openai_candidate_cost_adjusted_fill_v5_001_20260608.jsonl \
  --paper-journal server-data/replay_reports/trident_ai_candidate_cost_adjusted_paper_v5_001_20260608.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_calibration_cost_adjusted_v5_001_20260608.json \
  --report-md-path server-data/replay_reports/trident_ai_calibration_cost_adjusted_v5_001_20260608.md
```

- `candidates_seen=5`;
- `matched_candidates=2`;
- `missing_llm_decisions=3`;
- `matched_paper_decisions=2`;
- `llm_action_counts={"hold": 1, "open": 1}`;
- `paper_action_counts={"no_op": 1, "open": 1}`;
- `closed_trades=1`;
- `winning_trades=0`;
- `losing_trades=1`;
- `realized_pnl_usd=-0.022337`;
- `net_after_ai_cost_usd=-0.0258905`.

Lecture:

- SOL long `2026-05-21T00:24:00Z`: le LLM garde `hold`, confiance `0.52`.
  Tags principaux: `cost_pressure`, `weak_confluence`,
  `microprice_conflict`;
- ETH short `2026-05-21T00:02:00Z`: le LLM ouvre, confiance `0.67`; le paper
  replay ferme en fin d'echantillon avec `pnl_usd=-0.022337`;
- les 3 candidats restants ne doivent pas etre payes tant que le gate local
  n'est pas durci.

Next step recommande:

- ne pas lancer les 3 candidats restants maintenant;
- durcir le scanner avant tout nouvel appel payant:
  - coherence directionnelle stricte entre side et microprice dislocation;
  - `edge_to_cost_ratio` minimum plus eleve pour un candidat payable;
  - penalite explicite si l'edge estime couvre a peine les frais round-trip;
  - option `--min-edge-to-cost` exposee en CLI pour piloter le budget;
- relancer ensuite un scan cache-only, puis seulement une micro-batch payante
  de `1` ou `2` candidats avec `--max-incremental-cost-usd 0.01`.

Next step execute: gate strict cout/microprice.

Implementation:

- `CANDIDATE_HINT_SCHEMA_VERSION = "trident_ai_candidate_hint_v3"`;
- score local penalise maintenant un conflit microprice/side;
- nouveau gate de selection candidat:
  - `min_edge_to_cost`, defaut `1.5`;
  - `allow_microprice_conflict`, defaut `false`;
  - `microprice_conflict_bps`, defaut `0.25`;
- nouvelles options CLI:
  - `--min-edge-to-cost`;
  - `--allow-microprice-conflict`;
  - `--microprice-conflict-bps`;
- le rapport expose `candidate_rejections`, `min_edge_to_cost`,
  `allow_microprice_conflict` et `microprice_conflict_bps`.

Scan strict zero-cout execute:

```bash
uv run python -m app.trident_ai.cli candidate-scan \
  --input server-data/replay_inputs/full_bot_latest_fetch.jsonl \
  --journal-path server-data/replay_reports/trident_ai_candidate_scan_strict_gate_20260608.jsonl \
  --selected-input-path server-data/replay_inputs/trident_ai_candidate_strict_gate_20260608.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_candidate_scan_strict_gate_20260608.json \
  --report-md-path server-data/replay_reports/trident_ai_candidate_scan_strict_gate_20260608.md \
  --max-records 100 \
  --max-contexts 100 \
  --symbols BTC,ETH,SOL,HYPE \
  --top-n 40 \
  --min-score 1.25
```

Resultat:

- `contexts_scored=100`;
- `candidate_rejections=100`;
- `candidates_selected=0`;
- cout API: `0.0 USD`;
- `rejection_reasons`:
  - `edge_to_cost_below_min=61`;
  - `microprice_direction_conflict=35`;
  - `score_below_min=4`;
- input selectionne vide:
  `server-data/replay_inputs/trident_ai_candidate_strict_gate_20260608.jsonl`.

Scan watchlist zero-cout execute avec score relache:

```bash
uv run python -m app.trident_ai.cli candidate-scan \
  --input server-data/replay_inputs/full_bot_latest_fetch.jsonl \
  --journal-path server-data/replay_reports/trident_ai_candidate_scan_strict_edge_watchlist_20260608.jsonl \
  --selected-input-path server-data/replay_inputs/trident_ai_candidate_strict_edge_watchlist_20260608.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_candidate_scan_strict_edge_watchlist_20260608.json \
  --report-md-path server-data/replay_reports/trident_ai_candidate_scan_strict_edge_watchlist_20260608.md \
  --max-records 100 \
  --max-contexts 100 \
  --symbols BTC,ETH,SOL,HYPE \
  --top-n 40 \
  --min-score 0.75 \
  --min-edge-to-cost 1.5
```

Resultat:

- `candidate_rejections=100`;
- `candidates_selected=0`;
- conclusion: la fenetre locale ne contient aucun candidat payable selon le
  gate strict.

Lecture:

- le meilleur candidat non-conflictuel reste ETH short
  `2026-05-21T00:02:00Z`, mais son `edge_to_cost_ratio=1.319880`, sous le
  nouveau minimum `1.5`, et c'est le trade qui a perdu dans le paper v5;
- plusieurs candidats ont un bon edge/cost theorique mais un score de
  confluence trop faible;
- aucun nouvel appel OpenAI n'est justifie sur cette fenetre.

Next step recommande:

- separer le moment ou le LLM decide du suivi complet de la position;
- faire vivre les trades paper sur les snapshots marche suivants sans rappeler
  le LLM;
- relancer ensuite une calibration contre cette trajectoire plus realiste;
- ne lancer OpenAI que si au moins un candidat passe `score >= 1.25`,
  `edge_to_cost >= 1.5` et sans conflit microprice.

Next step execute: paper replay avec suivi marche complet.

Implementation:

- `paper-replay` accepte maintenant `--market-input`;
- le journal LLM reste la source des decisions agent;
- les snapshots de `--market-input` sont rejoues chronologiquement apres la
  premiere decision LLM;
- les positions ouvertes sont suivies sur les contextes marche suivants:
  - invalidation price;
  - stop;
  - take-profit;
  - time-stop;
  - force close de fin de replay si encore ouvert;
- le replay trie les decisions LLM et les snapshots marche par timestamp,
  afin d'eviter un biais lie a un input selectionne par score plutot que par
  ordre chronologique;
- le rapport expose:
  - `market_input_path`;
  - `market_contexts_seen`;
  - `market_exit_checks`.

Commande executee sans nouvel appel OpenAI:

```bash
uv run python -m app.trident_ai.cli paper-replay \
  --input server-data/replay_reports/trident_ai_openai_candidate_cost_adjusted_fill_v5_001_20260608.jsonl \
  --market-input server-data/replay_inputs/full_bot_latest_fetch.jsonl \
  --journal-path server-data/replay_reports/trident_ai_candidate_cost_adjusted_market_follow_paper_v5_001_20260608.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_candidate_cost_adjusted_market_follow_paper_v5_001_20260608.json \
  --report-md-path server-data/replay_reports/trident_ai_candidate_cost_adjusted_market_follow_paper_v5_001_20260608.md \
  --max-decisions 20
```

Resultat:

- cout OpenAI incremental: `0.0 USD`;
- `decisions_seen=2`;
- `market_contexts_seen=21218`;
- `market_exit_checks=31`;
- `positions_opened=1`;
- `positions_closed=1`;
- close reason: `time_stop=1`;
- `fees_usd=0.0175`;
- `gross_pnl_usd=-0.03756`;
- `realized_pnl_usd=-0.05506`;
- `ai_cost_usd=0.0035535`;
- `net_after_ai_cost_usd=-0.0586135`.

Calibration associee:

```bash
uv run python -m app.trident_ai.cli calibration-report \
  --candidate-input server-data/replay_inputs/trident_ai_candidate_cost_adjusted_20260608.jsonl \
  --llm-journal server-data/replay_reports/trident_ai_openai_candidate_cost_adjusted_fill_v5_001_20260608.jsonl \
  --paper-journal server-data/replay_reports/trident_ai_candidate_cost_adjusted_market_follow_paper_v5_001_20260608.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_calibration_cost_adjusted_market_follow_v5_001_20260608.json \
  --report-md-path server-data/replay_reports/trident_ai_calibration_cost_adjusted_market_follow_v5_001_20260608.md
```

Resultat calibration:

- `candidates_seen=5`;
- `matched_candidates=2`;
- `missing_llm_decisions=3`;
- `closed_trades=1`;
- `winning_trades=0`;
- `losing_trades=1`;
- `realized_pnl_usd=-0.05506`;
- `net_after_ai_cost_usd=-0.0586135`.

Lecture:

- l'ancien paper v5 fermait l'ETH short presque immediatement en fin
  d'echantillon avec `pnl_usd=-0.022337`;
- le replay market-follow laisse vivre la position sur `31` checks marche et
  sort au `time_stop`, avec `pnl_usd=-0.05506`;
- le gate strict etait donc justifie: ce candidat avait
  `edge_to_cost_ratio=1.319880`, sous le minimum payable `1.5`.

Next step recommande:

- calibrer l'estimateur local `estimated_edge_bps` contre les trades paper
  market-follow;
- ajouter un rapport `edge-calibration` pour comparer score, edge/cost, edge net,
  conflit microprice, close reason et PnL realise;
- recalibrer le scanner local avant tout nouvel appel OpenAI;
- relancer seulement ensuite un scan zero-cout sur une fenetre plus large.

Next step execute: edge-calibration et recalibration du scanner.

Implementation:

- nouvelle commande CLI `edge-calibration`;
- nouveau rapport `TRIDENT-AI Edge Calibration Report`;
- comparaison par candidat:
  - `estimated_edge_bps`;
  - `round_trip_cost_bps`;
  - `estimated_net_edge_bps`;
  - `edge_to_cost_ratio`;
  - conflit microprice/side;
  - close reason;
  - PnL realise en USD et en bps;
  - erreur d'edge en bps;
- `candidate-scan` ajoute maintenant:
  - `CANDIDATE_HINT_SCHEMA_VERSION = "trident_ai_candidate_hint_v4"`;
  - `estimated_net_edge_bps`;
  - gate `min_net_edge_bps`, defaut `5.0`;
  - option CLI `--min-net-edge-bps`;
- `llm-replay` passe en prompt `trident_ai_replay_v6`;
- le prompt compact inclut `net_edge_bps`;
- la regle prompt dit explicitement de traiter comme `hold` tout candidat avec
  `edge_to_cost < 1.5`, `net_edge_bps < 5` ou conflit microprice.

Rapport edge-calibration execute:

```bash
uv run python -m app.trident_ai.cli edge-calibration \
  --candidate-input server-data/replay_inputs/trident_ai_candidate_cost_adjusted_20260608.jsonl \
  --llm-journal server-data/replay_reports/trident_ai_openai_candidate_cost_adjusted_fill_v5_001_20260608.jsonl \
  --paper-journal server-data/replay_reports/trident_ai_candidate_cost_adjusted_market_follow_paper_v5_001_20260608.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_edge_calibration_market_follow_v5_001_20260608.json \
  --report-md-path server-data/replay_reports/trident_ai_edge_calibration_market_follow_v5_001_20260608.md
```

Resultat:

- `candidates_seen=5`;
- `matched_llm_decisions=2`;
- `open_decisions=1`;
- `closed_trades=1`;
- `false_positive_trades=1`;
- `avg_estimated_edge_bps=11.79286`;
- `avg_estimated_net_edge_bps=2.85806`;
- `avg_realized_net_bps=-22.024`;
- `avg_edge_error_bps=-33.81686`;
- `realized_pnl_usd=-0.05506`;
- `close_reasons={"time_stop": 1}`;
- `suggested_min_edge_to_cost=1.5`;
- `suggested_min_net_edge_bps=5.0`;
- warning: `sample_too_small_keep_conservative_gates`.

Lecture:

- l'unique open perdant avait un edge net estime de seulement `2.85806 bps`;
- le seuil `min_net_edge_bps=5.0` bloque ce type de candidat avant appel LLM;
- l'echantillon est trop petit pour optimiser finement les coefficients, donc
  on garde des gates conservateurs plutot qu'un fit statistique fragile.

Scan strict recalibre sur les 100 premiers contextes:

- sortie:
  `server-data/replay_reports/trident_ai_candidate_scan_net_edge_gate_20260608.*`;
- `contexts_scored=100`;
- `candidate_rejections=100`;
- `candidates_selected=0`;
- `rejection_reasons`:
  - `microprice_direction_conflict=35`;
  - `net_edge_below_min=60`;
  - `edge_to_cost_below_min=2`;
  - `score_below_min=3`;
- cout OpenAI: `0.0 USD`.

Scan zero-cout elargi:

```bash
uv run python -m app.trident_ai.cli candidate-scan \
  --input server-data/replay_inputs/full_bot_latest_fetch.jsonl \
  --journal-path server-data/replay_reports/trident_ai_candidate_scan_net_edge_gate_8000_20260608.jsonl \
  --selected-input-path server-data/replay_inputs/trident_ai_candidate_net_edge_gate_8000_20260608.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_candidate_scan_net_edge_gate_8000_20260608.json \
  --report-md-path server-data/replay_reports/trident_ai_candidate_scan_net_edge_gate_8000_20260608.md \
  --max-records 2500 \
  --max-contexts 8000 \
  --symbols BTC,ETH,SOL,HYPE \
  --top-n 40 \
  --min-score 1.25
```

Resultat:

- `records_processed=2500`;
- `contexts_scored=5333`;
- `candidate_rejections=4157`;
- `candidates_selected=40`;
- `side_counts={"long": 22, "short": 18}`;
- `symbol_counts={"BTC": 16, "ETH": 12, "SOL": 12}`;
- meilleurs candidats:
  - ETH long `2026-05-21T15:13:00Z`: score `5.072335`,
    edge/cost `3.176817`, net edge `18.43176 bps`;
  - BTC long `2026-05-21T15:13:00Z`: score `3.526116`,
    edge/cost `1.873794`, net edge `11.624 bps`.

Replay cache-only v6 sur les 40 candidats:

- sortie:
  `server-data/replay_reports/trident_ai_net_edge_gate_cache_only_v6_20260608.*`;
- `contexts_built=40`;
- `cache_hits=0`;
- `live_llm_calls=0`;
- `llm_failures=40`, cause `cache_miss_live_calls_disabled`;
- cout OpenAI: `0.0 USD`.

Input top-2 prepare:

- sortie:
  `server-data/replay_inputs/trident_ai_candidate_net_edge_gate_top2_20260608.jsonl`;
- top-2: ETH long et BTC long `2026-05-21T15:13:00Z`;
- replay cache-only top-2:
  `server-data/replay_reports/trident_ai_net_edge_gate_top2_cache_only_v6_20260608.*`;
- `contexts_built=2`;
- `cache_hits=0`;
- cout OpenAI: `0.0 USD`.

Next step recommande:

- ne pas lancer les 40 candidats;
- si validation operateur, remplir seulement le top-2 v6 avec:
  - `--max-live-calls 2`;
  - `--max-incremental-cost-usd 0.01`;
- puis lancer immediatement:
  - `paper-replay --market-input server-data/replay_inputs/full_bot_latest_fetch.jsonl`;
  - `edge-calibration`;
- si le top-2 v6 ne produit pas au moins un trade non perdant en market-follow,
  ne pas continuer les appels OpenAI et passer a l'etape `xAI/news-social`
  en veto shadow.

Next step execute: top-2 v6 payant sous cap strict.

Commande:

```bash
uv run python -m app.trident_ai.cli llm-replay \
  --input server-data/replay_inputs/trident_ai_candidate_net_edge_gate_top2_20260608.jsonl \
  --journal-path server-data/replay_reports/trident_ai_net_edge_gate_top2_live_v6_20260608.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_net_edge_gate_top2_live_v6_20260608.json \
  --report-md-path server-data/replay_reports/trident_ai_net_edge_gate_top2_live_v6_20260608.md \
  --max-records 2 \
  --max-contexts 2 \
  --symbols BTC,ETH,SOL,HYPE \
  --allow-live-llm-calls \
  --max-live-calls 2 \
  --max-incremental-cost-usd 0.01
```

Resultat:

- `live_llm_calls=2`;
- `incremental_cost_usd=0.0035415`;
- `prompt_version=trident_ai_replay_v6`;
- `llm_failures=0`;
- `action_counts={"hold": 2}`;
- `open_decisions=0` au paper-replay;
- paper market-follow:
  `server-data/replay_reports/trident_ai_net_edge_gate_top2_live_v6_paper_20260608.*`;
- `market_contexts_seen=16891`;
- `positions_opened=0`, `positions_closed=0`;
- `net_after_ai_cost_usd=-0.0035415`.

Calibration top-2 v6:

- rapport:
  `server-data/replay_reports/trident_ai_edge_calibration_top2_live_v6_20260608.*`;
- `candidates_seen=2`;
- `matched_llm_decisions=2`;
- `open_decisions=0`;
- les deux meilleurs candidats locaux (`ETH` long edge net `18.43176 bps`,
  `BTC` long edge net `11.624 bps`) ont ete conserves en `hold` par le LLM;
- aucune conclusion PnL possible sur ce micro-batch, mais le resultat confirme
  qu'il ne faut pas depenser sur les 40 candidats v6 sans ajustement ou couche
  veto/diagnostic supplementaire.

### Etape 8 - Intel/news-social xAI

Ajouter une couche `trident-ai-intel` en shadow, optionnelle et separee du
decision engine OpenAI.

Objectif:

- collecter un digest news/social cible;
- utiliser xAI `X Search` et eventuellement `Web Search`;
- produire un `AgentIntelDigest` structure;
- ajouter des vetoes, reductions de prudence ou `close_only_mode`;
- ne jamais ouvrir un trade ni augmenter un cap a partir du social seul.

Sources ciblees initiales:

- comptes officiels Hyperliquid;
- comptes officiels des projets `BTC/ETH/SOL/HYPE` quand pertinents;
- status pages/exchanges;
- sources securite crypto reconnues;
- sources macro/news uniquement si evenement fort.

Garde-fous specifiques:

- cle separee `XAI_API_KEY`;
- `enabled=false` par defaut;
- budget mensuel xAI dedie;
- `max_x_search_calls_per_day`;
- `max_web_search_calls_per_day`;
- allowlist de handles X, maximum 20 par requete;
- pas de scan massif X;
- TTL strict des digests;
- conservation des `source_ids`;
- deduplication URL/post/thread;
- protection prompt-injection: tout contenu social est une donnee non fiable,
  jamais une instruction;
- rumeur = veto potentiel ou review, pas signal d'achat;
- evenement positif non officiel = jamais hausse de taille ou de levier;
- evenement negatif confirme = peut veto une entree, reduire une taille, ou
  passer `close_only_mode`.

Livrables:

- `app/trident_ai/intel.py`;
- schemas/tests `AgentIntelDigest` enrichis si besoin;
- client xAI injectable, cacheable et testable sans reseau;
- fixtures de digest pour `BTC/ETH/SOL/HYPE`;
- journal `trident_ai_intel_*.jsonl`;
- integration replay: decision LLM recoit un digest optionnel;
- rapport: cout xAI, nombre de recherches, sources, vetoes.

Timing recommande:

- apres le premier cache-fill OpenAI et avant tout testnet live;
- avant le dry-run large si on veut mesurer l'effet des vetoes sociaux;
- ne pas bloquer l'etape 7 si l'objectif est d'abord de tester PnL paper sans
  news/social.

Next step execute: scaffold intel/news-social shadow.

Implementation:

- nouvelle config `[trident_ai.intel]` dans `config/trident_ai.toml`;
- `enabled=false` par defaut;
- provider cible `xai`, modele `grok-4.3`;
- caps explicites:
  - `max_live_calls_per_digest=2`;
  - `max_incremental_cost_usd=0.02`;
  - `max_x_search_calls_per_day=24`;
  - `max_web_search_calls_per_day=12`;
- couts outils configurables:
  - `x_search_cost_per_1000_calls_usd=5.0`;
  - `web_search_cost_per_1000_calls_usd=5.0`;
- allowlist X limitee a `20` handles maximum;
- nouveau module `app/trident_ai/intel.py`;
- nouvelle commande CLI `intel-digest`;
- cache local `runtime/trident_ai/intel_cache`;
- schemas/rapports:
  - `trident_ai_intel_digest`;
  - `INTEL_DIGEST_EVENT`;
  - journal JSONL + rapport JSON/Markdown.

Garde-fous confirmes:

- aucun appel xAI sans `--allow-live-intel-calls`;
- aucun appel xAI si `trident_ai.intel.enabled=false`;
- digest neutre si live disabled;
- digest fixture possible pour tester les vetoes sans reseau;
- une source social/news ne peut produire qu'un veto, un `close_only_mode` ou
  un signal de prudence shadow, jamais un signal d'ouverture.

Runs zero-cout:

```bash
uv run python -m app.trident_ai.cli intel-digest \
  --journal-path server-data/replay_reports/trident_ai_intel_disabled_top2_20260608.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_intel_disabled_top2_20260608.json \
  --report-md-path server-data/replay_reports/trident_ai_intel_disabled_top2_20260608.md \
  --symbols BTC,ETH,SOL,HYPE \
  --as-of 2026-05-21T15:13:00Z
```

Resultat:

- `provider=xai`, `model=grok-4.3`;
- `live_intel_calls=0`;
- `estimated_incremental_cost_usd=0.0`;
- `skip_reasons={"live_intel_calls_disabled": 1}`;
- digest neutre, aucun veto.

Smoke fixture veto:

- sortie:
  `server-data/replay_reports/trident_ai_intel_fixture_veto_smoke_20260608.*`;
- `provider=fixture`;
- `items_seen=2`;
- `veto_symbols=["HYPE"]`;
- cout `0.0 USD`.

Next step recommande:

- ne pas activer xAI live tant que la couche intel n'est pas branchee au replay
  LLM/paper comme veto optionnel;
- ajouter l'injection d'un digest intel au prompt v7 et au journal LLM;
- ajouter un replay comparatif `without_intel` vs `with_intel_fixture` pour
  verifier que le digest peut bloquer un candidat sans jamais creer une entree;
- ensuite seulement tester un digest xAI live avec `--max-live-calls 1` et
  `--max-incremental-cost-usd 0.01`.

Next step execute: integration replay LLM prompt v7 avec veto intel local.

Implementation:

- `TRIDENT_AI_REPLAY_PROMPT_VERSION = "trident_ai_replay_v7"`;
- `llm-replay` accepte `--intel-digest-input`;
- le digest peut venir:
  - d'un rapport JSON `trident_ai_intel_digest`;
  - d'un journal JSONL `INTEL_DIGEST_EVENT`;
  - d'un fixture JSON local;
- le prompt compact ajoute `ctx.intel`:
  - `digest_id`;
  - `as_of`;
  - `global_market_impact`;
  - items symboliques limites;
  - `veto_entry`;
  - `close_only_mode`;
- le runner applique un veto local avant cache/appel LLM:
  - si un item intel matche le symbole avec `veto_entry=true` ou
    `close_only_mode=true`, le contexte est rejete avec `intel_veto`;
  - aucun appel OpenAI n'est effectue;
  - le journal garde les `intel_veto_reasons`;
- le validateur LLM recoit aussi le digest pour les controles de fraicheur.

Replay zero-cout de verification veto:

```bash
uv run python -m app.trident_ai.cli llm-replay \
  --input server-data/replay_inputs/trident_ai_fixture_hype_snapshot_20260608.jsonl \
  --journal-path server-data/replay_reports/trident_ai_intel_fixture_hype_veto_llm_v7_20260608.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_intel_fixture_hype_veto_llm_v7_20260608.json \
  --report-md-path server-data/replay_reports/trident_ai_intel_fixture_hype_veto_llm_v7_20260608.md \
  --max-records 1 \
  --max-contexts 1 \
  --symbols HYPE \
  --intel-digest-input tests/fixtures/trident_ai/intel_digest.json
```

Resultat:

- `prompt_version=trident_ai_replay_v7`;
- `contexts_built=1`;
- `context_rejections=1`;
- `rejection_reasons={"intel_veto": 1}`;
- `llm_requests=0`;
- `live_llm_calls=0`;
- cout OpenAI `0.0 USD`;
- rapport:
  `server-data/replay_reports/trident_ai_intel_fixture_hype_veto_llm_v7_20260608.*`.

Next step recommande:

- ne pas lancer de nouvel appel OpenAI v7 tant qu'un digest intel reel n'est pas
  disponible ou tant que le prompt d'ouverture n'est pas recalibre;
- ajouter un rapport de diagnostic `llm-decision-audit` pour detecter les
  contradictions du type "edge sous seuil" alors que le scanner donne
  `edge_to_cost>=1.5` et `net_edge_bps>=5`;
- ensuite seulement tester un digest xAI live unique, avec `enabled=true` dans
  une config locale non committee, `--max-live-calls 1` et
  `--max-incremental-cost-usd 0.01`.

Next step execute: audit decisions LLM et prompt v8.

Implementation audit:

- nouveau module `app/trident_ai/decision_audit.py`;
- nouvelle commande CLI `llm-decision-audit`;
- le rapport joint:
  - input candidats;
  - journal LLM;
  - action LLM;
  - score candidat;
  - `edge_to_cost_ratio`;
  - `estimated_net_edge_bps`;
  - conflit microprice;
  - tags/evidence/risk_notes LLM;
- detection:
  - `eligible_candidate_held`;
  - `false_edge_to_cost_below_threshold`;
  - `false_net_edge_below_threshold`;
  - `false_microprice_conflict`.

Audit top-2 v6:

```bash
uv run python -m app.trident_ai.cli llm-decision-audit \
  --candidate-input server-data/replay_inputs/trident_ai_candidate_net_edge_gate_top2_20260608.jsonl \
  --llm-journal server-data/replay_reports/trident_ai_net_edge_gate_top2_live_v6_20260608.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_llm_decision_audit_top2_v6_20260608.json \
  --report-md-path server-data/replay_reports/trident_ai_llm_decision_audit_top2_v6_20260608.md
```

Resultat:

- `eligible_candidates=2`;
- `eligible_holds=2`;
- `contradictory_decisions=1`;
- `contradiction_counts`:
  - `false_edge_to_cost_below_threshold=1`;
  - `false_net_edge_below_threshold=1`;
- lecture:
  - ETH = hold prudent mais pas contradiction factuelle;
  - BTC = contradiction claire, le LLM affirme que les seuils edge/net edge sont
    sous minimum alors que le scanner les valide.

Prompt v8:

- `TRIDENT_AI_REPLAY_PROMPT_VERSION = "trident_ai_replay_v8"`;
- le prompt ajoute `ctx.candidate.passes`:
  - `edge_to_cost`;
  - `net_edge`;
  - `microprice`;
  - `local_gate`;
- instruction explicite:
  - ne jamais contredire les chiffres ou flags `passes`;
  - si un candidat eligible est conserve en `hold`, citer une raison autre que
    les seuils deja valides.

Micro-batch top-2 v8 execute sous cap:

```bash
uv run python -m app.trident_ai.cli llm-replay \
  --input server-data/replay_inputs/trident_ai_candidate_net_edge_gate_top2_20260608.jsonl \
  --journal-path server-data/replay_reports/trident_ai_net_edge_gate_top2_live_v8_20260608.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_net_edge_gate_top2_live_v8_20260608.json \
  --report-md-path server-data/replay_reports/trident_ai_net_edge_gate_top2_live_v8_20260608.md \
  --max-records 2 \
  --max-contexts 2 \
  --symbols BTC,ETH,SOL,HYPE \
  --allow-live-llm-calls \
  --max-live-calls 2 \
  --max-incremental-cost-usd 0.01
```

Resultat:

- `live_llm_calls=2`;
- `incremental_cost_usd=0.00381`;
- `action_counts={"open": 2}`;
- `llm_failures=0`;
- audit v8:
  - `eligible_candidates=2`;
  - `eligible_holds=0`;
  - `contradictory_decisions=0`;
  - rapport:
    `server-data/replay_reports/trident_ai_llm_decision_audit_top2_v8_20260608.*`.

Paper market-follow v8:

- portefeuille normal `max_open_positions=1`:
  - rapport:
    `server-data/replay_reports/trident_ai_net_edge_gate_top2_live_v8_paper_20260608.*`;
  - `positions_opened=1`;
  - `skip_reasons={"max_open_positions_reached": 1}`;
  - trade ETH ferme par `time_stop`;
  - `realized_pnl_usd=-0.008318`;
  - `net_after_ai_cost_usd=-0.012128`.

Le paper-replay accepte maintenant `--symbols`, afin de tester chaque decision
independamment sans changer les caps globaux.

Paper independant:

- ETH only:
  - rapport:
    `server-data/replay_reports/trident_ai_net_edge_gate_top2_live_v8_paper_eth_only_20260608.*`;
  - `realized_pnl_usd=-0.008318`;
  - `gross_pnl_usd=0.009182`;
  - `fees_usd=0.0175`;
  - calibration:
    `server-data/replay_reports/trident_ai_edge_calibration_top2_live_v8_eth_only_20260608.*`;
  - `realized_net_bps=-3.3272`;
  - `edge_error_bps=-30.22626`.
- BTC only:
  - rapport:
    `server-data/replay_reports/trident_ai_net_edge_gate_top2_live_v8_paper_btc_only_20260608.*`;
  - `realized_pnl_usd=-0.019676`;
  - `gross_pnl_usd=-0.002176`;
  - `fees_usd=0.0175`;
  - calibration:
    `server-data/replay_reports/trident_ai_edge_calibration_top2_live_v8_btc_only_20260608.*`;
  - `realized_net_bps=-7.8704`;
  - `edge_error_bps=-32.7973`.

Lecture:

- v8 repare le prompt: le LLM ne contredit plus les seuils locaux;
- le probleme restant est economique: l'edge local est surestime par rapport au
  mouvement tradable apres frais;
- sur `n=2`, ne pas durcir definitivement les gates a `net_edge > 20 bps`
  malgre les suggestions automatiques; il faut d'abord calibrer sur un ensemble
  plus large, sans nouvel appel OpenAI.

Next step recommande:

- ajouter un `candidate-outcome-audit` zero-cout sur les 40 candidats locaux:
  mesurer le mouvement realise a horizons fixes (`15/30/60/180m`) net de frais,
  comparer a `estimated_edge_bps`, `estimated_net_edge_bps`, `edge_to_cost`,
  microprice et raisons locales;
- recalibrer le scanner local avec ce rapport avant tout nouveau batch OpenAI;
- ne pas lancer les 40 candidats v8 tant que l'audit outcome ne montre pas que
  les meilleurs candidats ont un edge net robuste apres frais.

Next step execute: audit outcome zero-cout et gate outcome strict.

Implementation:

- nouveau module `app/trident_ai/outcome_audit.py`;
- nouvelle commande CLI `candidate-outcome-audit`;
- le rapport mesure, pour chaque candidat local, le resultat mark-to-mid aux
  horizons fixes `15/30/60/180m`;
- le rapport ajoute des buckets par meilleur horizon pour comparer `symbol`,
  `side`, `edge_to_cost`, `net_edge`, `score` et alignement microprice;
- calculs:
  - `realized_gross_bps` side-aware;
  - `realized_net_bps = realized_gross_bps - round_trip_cost_bps`;
  - `edge_error_bps = realized_net_bps - estimated_edge_bps`;
  - agregats par horizon: samples, win rate, moyenne nette, mediane nette,
    erreur moyenne d'edge;
- le rapport propose des gates indicatifs bases sur le percentile des candidats
  perdants, mais ces seuils restent experimentaux tant que l'echantillon est
  petit.

Audit des 40 candidats `net_edge_gate`:

```bash
uv run python -m app.trident_ai.cli candidate-outcome-audit \
  --candidate-input server-data/replay_inputs/trident_ai_candidate_net_edge_gate_8000_20260608.jsonl \
  --market-input server-data/replay_inputs/full_bot_latest_fetch.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_candidate_outcome_audit_40_20260608.json \
  --report-md-path server-data/replay_reports/trident_ai_candidate_outcome_audit_40_20260608.md \
  --horizons-minutes 15,30,60,180
```

Resultat:

- `candidates_seen=40`, `candidates_with_any_outcome=40`;
- meilleur horizon moyen: `15m`;
- `15m`: win rate `50.00%`, avg net `-0.148265 bps`, median net
  `0.43387 bps`;
- `30m`: win rate `32.50%`, avg net `-5.269801 bps`;
- `60m`: win rate `27.50%`, avg net `-23.348152 bps`;
- `180m`: win rate `37.50%`, avg net `-10.334613 bps`;
- erreur moyenne d'edge tres negative, par exemple `-22.591673 bps` a `15m`;
- gates suggerees par l'audit:
  - `suggested_min_edge_to_cost=2.4597`;
  - `suggested_min_net_edge_bps=15.2579`;
- rapport:
  `server-data/replay_reports/trident_ai_candidate_outcome_audit_40_20260608.*`.

Lecture:

- le scanner v8 corrige le probleme de prompt, mais le panier de 40 candidats
  reste trop faible apres frais;
- l'edge theorique local surestime le mouvement exploitable;
- aucun nouveau batch OpenAI large n'est justifie sur ces 40 candidats.

Scan recalibre avec les gates outcome:

```bash
uv run python -m app.trident_ai.cli candidate-scan \
  --input server-data/replay_inputs/full_bot_latest_fetch.jsonl \
  --journal-path server-data/replay_reports/trident_ai_candidate_scan_outcome_recalibrated_8000_20260608.jsonl \
  --selected-input-path server-data/replay_inputs/trident_ai_candidate_outcome_recalibrated_8000_20260608.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_candidate_scan_outcome_recalibrated_8000_20260608.json \
  --report-md-path server-data/replay_reports/trident_ai_candidate_scan_outcome_recalibrated_8000_20260608.md \
  --max-records 2500 \
  --max-contexts 8000 \
  --symbols BTC,ETH,SOL,HYPE \
  --top-n 40 \
  --min-score 1.25 \
  --min-edge-to-cost 2.4597 \
  --min-net-edge-bps 15.2579
```

Resultat:

- `contexts_scored=5333`;
- `candidates_selected=7`;
- symboles: `BTC=2`, `ETH=3`, `SOL=2`;
- rejets:
  - `net_edge_below_min=2875`;
  - `microprice_direction_conflict=1302`;
  - `edge_to_cost_below_min=14`;
  - `score_below_min=9`;
- rapport:
  `server-data/replay_reports/trident_ai_candidate_scan_outcome_recalibrated_8000_20260608.*`;
- input selectionne:
  `server-data/replay_inputs/trident_ai_candidate_outcome_recalibrated_8000_20260608.jsonl`.

Audit outcome des 7 candidats recalibres:

```bash
uv run python -m app.trident_ai.cli candidate-outcome-audit \
  --candidate-input server-data/replay_inputs/trident_ai_candidate_outcome_recalibrated_8000_20260608.jsonl \
  --market-input server-data/replay_inputs/full_bot_latest_fetch.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_candidate_outcome_audit_recalibrated_7_20260608.json \
  --report-md-path server-data/replay_reports/trident_ai_candidate_outcome_audit_recalibrated_7_20260608.md \
  --horizons-minutes 15,30,60,180
```

Resultat:

- `candidates_seen=7`, `candidates_with_any_outcome=7`;
- meilleur horizon moyen: `30m`;
- `15m`: win rate `57.14%`, avg net `10.227078 bps`, median net
  `11.628506 bps`;
- `30m`: win rate `42.86%`, avg net `14.302912 bps`, median net
  `-1.690807 bps`;
- `60m`: win rate `42.86%`, avg net `-18.59452 bps`;
- `180m`: win rate `57.14%`, avg net `-6.988485 bps`;
- gates suggerees par ce sous-ensemble:
  - `suggested_min_edge_to_cost=3.2768`;
  - `suggested_min_net_edge_bps=20.4318`;
- rapport:
  `server-data/replay_reports/trident_ai_candidate_outcome_audit_recalibrated_7_20260608.*`.

Lecture:

- le gate outcome strict ameliore nettement le panier local, et valide qu'un
  prefiltrage zero-cout peut proteger le budget OpenAI;
- l'echantillon `n=7` reste trop petit pour considerer le scanner comme calibre;
- la mediane negative a `30m` indique que le resultat moyen depend encore de
  quelques gros gagnants;
- les horizons `60m/180m` ne doivent pas etre privilegies pour le prochain test
  local.

Next step execute: score edge-quality et gate microprice aligne.

Implementation:

- `CANDIDATE_HINT_SCHEMA_VERSION = "trident_ai_candidate_hint_v5"`;
- le score candidat ajoute `edge_quality_score`, multiplicateur borne de `0.85`
  a `1.10`, base sur `edge_to_cost_ratio` et `estimated_net_edge_bps`;
- `candidate-scan` expose `--require-microprice-alignment`;
- rejet normalise: `microprice_not_aligned`;
- l'option reste desactivee par defaut pour permettre les comparaisons.

Scan outcome-quality sans microprice aligne requis:

```bash
uv run python -m app.trident_ai.cli candidate-scan \
  --input server-data/replay_inputs/full_bot_latest_fetch.jsonl \
  --journal-path server-data/replay_reports/trident_ai_candidate_scan_outcome_quality_v5_8000_20260608.jsonl \
  --selected-input-path server-data/replay_inputs/trident_ai_candidate_outcome_quality_v5_8000_20260608.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_candidate_scan_outcome_quality_v5_8000_20260608.json \
  --report-md-path server-data/replay_reports/trident_ai_candidate_scan_outcome_quality_v5_8000_20260608.md \
  --max-records 2500 \
  --max-contexts 8000 \
  --symbols BTC,ETH,SOL,HYPE \
  --top-n 40 \
  --min-score 1.25 \
  --min-edge-to-cost 2.4597 \
  --min-net-edge-bps 15.2579
```

Resultat:

- `candidates_selected=9`;
- audit outcome:
  `server-data/replay_reports/trident_ai_candidate_outcome_audit_outcome_quality_v5_9_20260608.*`;
- meilleur horizon `30m`;
- `30m`: win rate `33.33%`, avg net `7.31364 bps`, median net
  `-4.642731 bps`;
- `15m`: win rate `44.44%`, avg net `0.173813 bps`, median net
  `-1.28551 bps`;
- lecture: le score edge-quality seul ajoute des candidats faibles; il ne suffit
  pas a justifier un batch OpenAI.

Scan outcome-quality avec microprice aligne requis:

```bash
uv run python -m app.trident_ai.cli candidate-scan \
  --input server-data/replay_inputs/full_bot_latest_fetch.jsonl \
  --journal-path server-data/replay_reports/trident_ai_candidate_scan_microprice_aligned_v5_8000_20260608.jsonl \
  --selected-input-path server-data/replay_inputs/trident_ai_candidate_microprice_aligned_v5_8000_20260608.jsonl \
  --report-json-path server-data/replay_reports/trident_ai_candidate_scan_microprice_aligned_v5_8000_20260608.json \
  --report-md-path server-data/replay_reports/trident_ai_candidate_scan_microprice_aligned_v5_8000_20260608.md \
  --max-records 2500 \
  --max-contexts 8000 \
  --symbols BTC,ETH,SOL,HYPE \
  --top-n 40 \
  --min-score 1.25 \
  --min-edge-to-cost 2.4597 \
  --min-net-edge-bps 15.2579 \
  --require-microprice-alignment
```

Resultat:

- `candidates_selected=3`;
- rejets:
  - `microprice_direction_conflict=1302`;
  - `microprice_not_aligned=1277`;
  - `net_edge_below_min=1609`;
  - `edge_to_cost_below_min=14`;
  - `score_below_min=2`;
- audit outcome:
  `server-data/replay_reports/trident_ai_candidate_outcome_audit_microprice_aligned_v5_3_20260608.*`;
- meilleur horizon `30m`;
- `30m`: win rate `66.67%`, avg net `28.915557 bps`, median net
  `45.648462 bps`;
- `15m`: win rate `66.67%`, avg net `25.226559 bps`, median net
  `39.014856 bps`;
- lecture: microprice aligne est le meilleur signal observe, mais `n=3` est trop
  faible pour payer un replay LLM.

Next step recommande:

- etendre l'audit zero-cout sur une fenetre historique plus longue ou plusieurs
  fichiers de replay pour obtenir au moins `20` candidats microprice-aligned;
- garder la comparaison suivante comme baseline:
  1. gates outcome sans alignement microprice;
  2. gates outcome avec `--require-microprice-alignment`;
  3. meme univers `BTC/ETH/SOL/HYPE`;
- ne refaire un micro-batch OpenAI v8/v5 que si le panier microprice-aligned
  garde un avg net positif et une mediane non negative sur au moins `20`
  candidats.

Next step execute: extension multi-fenetres microprice-aligned.

Objectif: verifier si le bon resultat `n=3` venait d'un vrai signal ou d'un
echantillon trop chanceux. Aucun appel OpenAI.

Fenetres auditees:

| Fenetre | Input marche | Candidats microprice-aligned | Rapport outcome |
|---|---|---:|---|
| `2026-04-05 -> 2026-04-12` | `server-data/replay_inputs/full_bot_latest_fetch_2026-04-05_2026-04-12.jsonl` | 1 | `server-data/replay_reports/trident_ai_candidate_outcome_audit_microprice_aligned_v5_20260405_20260412_20260608.*` |
| `2026-04-13 -> 2026-04-17` | `server-data/replay_inputs/full_bot_latest_fetch_2026-04-13_2026-04-17.jsonl` | 55 | `server-data/replay_reports/trident_ai_candidate_outcome_audit_microprice_aligned_v5_20260413_20260417_20260608.*` |
| `2026-04-24 -> 2026-04-27` | `server-data/replay_inputs/full_bot_latest_fetch_live_window_20260424T1820_20260427T1813.jsonl` | 6 | `server-data/replay_reports/trident_ai_candidate_outcome_audit_microprice_aligned_v5_20260424_20260427_20260608.*` |
| `2026-05-21 -> 2026-05-24` | `server-data/replay_inputs/full_bot_latest_fetch.jsonl` | 24 | `server-data/replay_reports/trident_ai_candidate_outcome_audit_microprice_aligned_v5_full_latest_24_20260608.*` |

Detail important: le scan complet du dernier fichier doit passer explicitement
`--max-records 20000`, car la CLI herite d'un default smoke `20` pour les
replays bornes.

Resultats par fenetre:

| Fenetre | Horizon best | Samples best | Avg net best | Median net best | Lecture |
|---|---:|---:|---:|---:|---|
| `2026-04-05 -> 2026-04-12` | 15m | 1 | `-24.70 bps` | `-24.70 bps` | non exploitable, `n=1` |
| `2026-04-13 -> 2026-04-17` | 180m | 45 | `14.65 bps` | `25.23 bps` | positif a 180m, surtout via HYPE/long |
| `2026-04-24 -> 2026-04-27` | 180m | 5 | `52.39 bps` | `31.92 bps` | positif mais `n=6` candidats seulement |
| `2026-05-21 -> 2026-05-24` | 15m | 24 | `-12.19 bps` | `-17.24 bps` | negatif malgre `n>=20` |

Agregat multi-fenetres, tous symboles `BTC/ETH/SOL/HYPE`:

| Horizon | Samples | Win rate | Avg net | Median net |
|---:|---:|---:|---:|---:|
| 15m | 86 | `41.86%` | `-10.76 bps` | `-13.73 bps` |
| 30m | 86 | `40.70%` | `-17.69 bps` | `-14.44 bps` |
| 60m | 82 | `41.46%` | `-14.35 bps` | `-11.08 bps` |
| 180m | 75 | `54.67%` | `0.77 bps` | `12.27 bps` |

Agregat hors HYPE:

| Horizon | Samples | Win rate | Avg net | Median net |
|---:|---:|---:|---:|---:|
| 15m | 52 | `44.23%` | `-10.22 bps` | `-12.64 bps` |
| 30m | 52 | `40.38%` | `-20.83 bps` | `-11.33 bps` |
| 60m | 50 | `34.00%` | `-22.42 bps` | `-14.46 bps` |
| 180m | 44 | `45.45%` | `-23.66 bps` | `-16.70 bps` |

Lecture:

- le gate `microprice_aligned` seul n'est pas suffisant;
- les horizons `15/30/60m` restent negatifs en agregat;
- le seul horizon non rejete est `180m`, mais l'avg net total `0.77 bps` est
  trop faible pour payer du LLM;
- l'effet positif `180m` depend fortement de HYPE et d'une fenetre d'avril; hors
  HYPE, l'agregat redevient negatif;
- ne pas lancer de micro-batch OpenAI sur ce signal;
- le prochain test doit simuler le portefeuille, pas seulement le mark-to-mid:
  une position max, cooldown par symbole, fees/slippage, time stop `180m`, et
  selection chronologique des candidats.

Next step recommande:

- ajouter un replay paper deterministe de candidats locaux, sans LLM:
  `candidate-paper-replay`;
- input: JSONL produit par `candidate-scan`;
- execution: ouvrir les candidats acceptes par le scanner comme propositions
  locales, avec `time_stop_minutes=180`, `notional=25`, `max_open_positions=1`,
  cooldown symbole et force close de fin;
- objectif: verifier si le signal `180m` survit aux contraintes de portefeuille
  avant tout nouvel appel OpenAI.

Next step execute: replay paper deterministe de candidats locaux.

Implementation:

- ajout de `app/trident_ai/candidate_paper.py`;
- nouvelle commande CLI `candidate-paper-replay`;
- conversion des candidats locaux en decisions synthetiques `open`, source
  `trident_ai_candidate_paper_replay`;
- reutilisation du moteur existant `paper-replay` pour garder les memes frais,
  slippage, `max_open_positions`, `max_trades_per_day`, stops, take-profit,
  time-stop et journaux;
- cout LLM force a `0.0`: aucun appel OpenAI, aucun cache LLM consomme;
- rapports wrapper ecrits dans `server-data/replay_reports/`, avec rapport
  moteur paper sidecar suffixe `_paper_engine`.

Parametres executes:

- `notional=25 USDC`, borne par `trident_ai.risk.live_max_order_notional_usd`;
- `confidence=0.62`;
- `stop_bps=120`;
- `take_profit_bps=240`;
- `time_stop_minutes=180`;
- univers `BTC/ETH/SOL/HYPE`;
- contraintes config: `max_open_positions=1`, `max_trades_per_day=3`.

Resultats paper par fenetre:

| Fenetre | Decisions candidates | Trades ouverts | Trades fermes | PnL realise | Win rate trades | Close reasons | Rapport |
|---|---:|---:|---:|---:|---:|---|---|
| `2026-04-05 -> 2026-04-12` | 1 | 1 | 1 | `$-0.349404` | `0.00%` | `stop_hit=1` | `server-data/replay_reports/trident_ai_candidate_paper_microprice_aligned_v5_20260405_20260412_20260608.*` |
| `2026-04-13 -> 2026-04-17` | 55 | 13 | 13 | `$-1.292104` | `38.46%` | `stop_hit=7`, `take_profit_hit=1`, `time_stop=4`, `end_of_paper_replay=1` | `server-data/replay_reports/trident_ai_candidate_paper_microprice_aligned_v5_20260413_20260417_20260608.*` |
| `2026-04-24 -> 2026-04-27` | 6 | 3 | 3 | `$0.864418` | `100.00%` | `take_profit_hit=1`, `time_stop=2` | `server-data/replay_reports/trident_ai_candidate_paper_microprice_aligned_v5_20260424_20260427_20260608.*` |
| `2026-05-21 -> 2026-05-24` | 24 | 7 | 7 | `$-0.003285` | `42.86%` | `time_stop=7` | `server-data/replay_reports/trident_ai_candidate_paper_microprice_aligned_v5_full_latest_24_20260608.*` |

Agregat paper:

- `86` decisions candidates;
- `24` trades ouverts et fermes;
- win rate trades: `45.83%`;
- gross PnL: `$-0.360375`;
- fees: `$0.420000`;
- realized/net after AI cost: `$-0.780375`;
- AI cost: `$0.00000000`.

Lecture:

- le signal `microprice_aligned + edge_quality` ne survit pas encore aux stops
  et contraintes de portefeuille;
- la fenetre positive `2026-04-24 -> 2026-04-27` est trop petite (`3` trades)
  pour compenser les autres regimes;
- la fenetre `2026-04-13 -> 2026-04-17`, pourtant positive en horizon fixe
  `180m`, devient negative en paper a cause des stop-outs;
- ne pas lancer de batch OpenAI sur ce panier: le probleme est local au scanner
  et au profil stop/TP, pas au raisonnement LLM.

Next step recommande:

- ajouter un sweep zero-cout des parametres paper candidats:
  `stop_bps`, `take_profit_bps`, `time_stop_minutes`;
- comparer au minimum les profils `120/240/180`, `180/360/180`,
  `240/480/180`, `180/360/360`;
- objectif: verifier si les stop-outs sont un probleme de timing/largeur de
  stop ou si le signal d'entree lui-meme est insuffisant;
- ne reactiver OpenAI que si un profil local produit un PnL positif hors frais
  et net frais sur plusieurs fenetres, sans dependance excessive a une seule
  fenetre ou a HYPE.

Next step execute: sweep zero-cout stop/TP/time-stop.

Artefacts:

- rapports temporaires:
  `tmp/trident_ai_candidate_paper_sweep_20260608/report_*.json`;
- journaux temporaires:
  `tmp/trident_ai_candidate_paper_sweep_20260608/paper_*.jsonl`;
- aucun appel OpenAI, cout AI `0.0`.

Profils testes sur les quatre fenetres:

| Profil | Decisions | Trades | PnL realise | Gross PnL | Fees | Win rate | Close reasons | PnL par fenetre |
|---|---:|---:|---:|---:|---:|---:|---|---|
| `stop=120,tp=240,time=180` | 86 | 24 | `$-0.780375` | `$-0.360375` | `$0.420000` | `45.83%` | `stop_hit=8`, `take_profit_hit=2`, `time_stop=13`, `end=1` | `[-0.349404, -1.292104, 0.864418, -0.003285]` |
| `stop=180,tp=360,time=180` | 86 | 23 | `$-0.233606` | `$0.168894` | `$0.402500` | `47.83%` | `stop_hit=3`, `time_stop=19`, `end=1` | `[-0.348276, -0.690737, 0.808692, -0.003285]` |
| `stop=240,tp=480,time=180` | 86 | 23 | `$0.463011` | `$0.865511` | `$0.402500` | `52.17%` | `time_stop=22`, `end=1` | `[-0.348276, 0.005880, 0.808692, -0.003285]` |
| `stop=180,tp=360,time=360` | 86 | 20 | `$-0.723083` | `$-0.373083` | `$0.350000` | `45.00%` | `stop_hit=5`, `time_stop=12`, `end=3` | `[-0.308793, -1.419276, 0.773289, 0.231697]` |

Lecture sweep:

- elargir le stop reduit fortement les stop-outs;
- le meilleur brut est `stop=240,tp=480,time=180`, mais il reste fragile:
  l'essentiel du positif vient de la petite fenetre `2026-04-24 -> 2026-04-27`;
- par symbole sur `stop=240,tp=480,time=180`:
  - `BTC`: `3` trades, `$0.456201`, win rate `100.00%`;
  - `ETH`: `7` trades, `$-0.587450`, win rate `28.57%`;
  - `HYPE`: `10` trades, `$0.754196`, win rate `60.00%`;
  - `SOL`: `3` trades, `$-0.159936`, win rate `33.33%`;
- meme profil sans HYPE (`BTC/ETH/SOL`) sur les quatre fenetres:
  - `52` decisions;
  - `18` trades;
  - gross PnL `$0.241593`;
  - fees `$0.315000`;
  - realized PnL `$-0.073407`;
  - win rate `50.00%`.

Decision:

- garder `stop=240,tp=480,time=180` comme meilleur profil de recherche local,
  pas comme profil deployable;
- ne pas appeler OpenAI sur ce panier: le net positif tous symboles depend trop
  de HYPE et d'une petite fenetre;
- prochaine amelioration locale: ajouter une calibration de fiabilite
  pattern-first. Les symboles servent de diagnostic anti-surapprentissage, pas
  de regle primaire.

Next step execute: calibration pattern-first.

Motivation:

- ne pas construire de regle `coin-specific` du type "HYPE oui / ETH non";
- evaluer les familles de signaux: microprice, flow/book, vwap, edge bucket,
  volatilite, side et regime;
- garder le symbole comme diagnostic secondaire pour verifier qu'un pattern
  prometteur n'est pas porte par un seul actif.

Implementation:

- ajout de `app/trident_ai/pattern_calibration.py`;
- nouvelle commande CLI `pattern-calibration`;
- inputs: une ou plusieurs paires `--decision-journal` / `--paper-journal`;
- jointure par `decision_id`;
- groupement principal par pattern general:
  `microprice + flow_book + vwap + edge_bucket`;
- dimensions separees:
  `side`, `regime`, `microprice`, `flow_book`, `vwap`, `edge_bucket`,
  `net_edge_bucket`, `volatility_bucket`;
- table `symbol_diagnostics` explicitement marquee comme diagnostic secondaire.

Artefacts:

- profil tous symboles:
  `server-data/replay_reports/trident_ai_pattern_calibration_s240_tp480_t180_20260609.*`;
- diagnostic no-HYPE:
  `server-data/replay_reports/trident_ai_pattern_calibration_s240_tp480_t180_nohype_20260609.*`;
- aucun appel OpenAI.

Resultats pattern, profil `stop=240,tp=480,time=180`, tous symboles:

- `86` decisions candidates;
- `23` opens paper;
- `23` trades fermes;
- PnL realise `$0.463011`;
- avg net `8.05 bps`;
- le seul pattern avec `>=3` trades et PnL positif:
  `microprice=aligned | flow_book=flow_aligned_book_neutral | vwap=aligned | edge=2.0-3.0`;
  - `3` trades;
  - PnL `$0.041039`;
  - avg net `5.47 bps`;
  - win rate `33.33%`;
  - symboles observes: `ETH=2`, `HYPE=4`, `SOL=1` decisions.

Dimensions les plus informatives, tous symboles:

| Dimension | Bucket | Trades | PnL | Avg net | Lecture |
|---|---|---:|---:|---:|---|
| `flow_book` | `flow_aligned_book_neutral` | 7 | `$0.900274` | `51.44 bps` | pattern positif mais porte en partie par HYPE |
| `flow_book` | `flow_and_book_aligned` | 7 | `$-0.330025` | `-18.86 bps` | confluence trop evidente ou momentum deja consomme |
| `flow_book` | `mixed_conflict` | 5 | `$-0.341183` | `-27.29 bps` | conflit local a penaliser |
| `edge_bucket` | `3.0-4.0` | 8 | `$0.636691` | `31.83 bps` | meilleur bucket edge |
| `edge_bucket` | `2.0-3.0` | 12 | `$-0.144239` | `-4.81 bps` | edge insuffisant malgre selection |
| `edge_bucket` | `>=4.0` | 3 | `$-0.029441` | `-3.93 bps` | tres haut edge estime pas fiable seul |
| `vwap` | `aligned` | 16 | `$0.799491` | `19.99 bps` | utile |
| `vwap` | `neutral` | 2 | `$-0.513957` | `-102.79 bps` | a eviter ou fortement penaliser, sample faible |
| `volatility` | `high` | 4 | `$-0.045048` | `-4.50 bps` | vol haute pas favorable avec ce profil |
| `volatility` | `low` | 3 | `$0.328969` | `43.86 bps` | positif mais sample faible |

Diagnostic no-HYPE, meme profil:

- `52` decisions;
- `18` opens paper;
- `18` trades fermes;
- PnL realise `$-0.073407`;
- avg net `-1.63 bps`;
- aucune regle coin-specific a tirer;
- confirmation utile: les patterns/dimensions restent plus explicatifs que le
  nom du coin.

Decision pattern-first:

- ne pas debrayer sur une blacklist/whitelist coin;
- ne pas appeler OpenAI: le meilleur pattern fiable est trop petit et trop peu
  rentable;
- prochain step local: transformer ces diagnostics en penalites de score
  pattern-first dans `candidate-scan`, par exemple:
  - penaliser `flow_book=mixed_conflict`;
  - penaliser `flow_book=flow_and_book_aligned` tant que le replay reste
    negatif;
  - penaliser `vwap=neutral`;
  - relever le seuil utile vers `edge_bucket=3.0-4.0`, sans faire confiance
    aveuglement a `>=4.0`;
  - penaliser `volatility=high` pour le profil `240/480/180`.

Next step execute: score pattern-first `research_v1`.

Implementation:

- `candidate-scan` accepte maintenant `--pattern-profile`;
- profil par defaut: `none`, comportement historique conserve;
- profil de recherche: `research_v1`;
- le rapport candidat expose `pattern_quality_score`, `pattern_profile` et
  `pattern_reasons`;
- le score local reste symbol-agnostic: aucune whitelist/blacklist de coin,
  seulement des penalites/bonus de patterns;
- version de hint candidat: `trident_ai_candidate_hint_v6`;
- aucun appel OpenAI.

Regles du profil `research_v1`:

- bonus leger pour `flow_book=flow_aligned_book_neutral`;
- bonus leger pour `vwap=aligned`;
- bonus leger pour `edge_bucket=3.0-4.0`;
- bonus leger pour `volatility=low`;
- penalite pour `flow_book=mixed_conflict`;
- penalite pour `flow_book=flow_and_book_aligned`, qui peut correspondre a une
  confluence deja consommee;
- penalite pour `vwap=neutral`;
- penalite pour `edge_bucket=2.0-3.0`;
- penalite prudente pour `edge_bucket=>=4.0`, car le tres haut edge estime
  n'etait pas fiable seul;
- penalite pour `volatility=high`.

Scan pattern-first sur les quatre fenetres, memes gates que le panier
`microprice_aligned_v5`:

| Fenetre | Candidats v5 | Candidats `research_v1` | Lecture |
|---|---:|---:|---|
| `2026-04-05 -> 2026-04-12` | 1 | 1 | inchange |
| `2026-04-13 -> 2026-04-17` | 55 | 50 | reduction des patterns faibles |
| `2026-04-24 -> 2026-04-27` | 6 | 6 | inchange |
| `2026-05-21 -> 2026-05-24` | 24 | 18 | reduction nette |

Replay paper du panier `research_v1`, profil
`stop=240,tp=480,time=180`:

| Fenetre | Candidats | Opens | Trades fermes | PnL realise | Avg net | Close reasons |
|---|---:|---:|---:|---:|---:|---|
| `2026-04-05 -> 2026-04-12` | 1 | 1 | 1 | `$-0.348276` | `-139.31 bps` | `time_stop=1` |
| `2026-04-13 -> 2026-04-17` | 50 | 12 | 12 | `$0.807948` | `26.93 bps` | `time_stop=11`, `end=1` |
| `2026-04-24 -> 2026-04-27` | 6 | 3 | 3 | `$0.808692` | `107.83 bps` | `time_stop=3` |
| `2026-05-21 -> 2026-05-24` | 18 | 6 | 6 | `$0.212462` | `14.16 bps` | `time_stop=6` |

Comparaison agregee:

| Profil | Decisions | Trades fermes | PnL realise | Gross PnL | Fees | Avg net |
|---|---:|---:|---:|---:|---:|---:|
| Baseline sweep `s240/tp480/t180` | 86 | 23 | `$0.463011` | `$0.865511` | `$0.402500` | `8.05 bps` |
| Pattern-first `research_v1` | 75 | 22 | `$1.480826` | `$1.865826` | `$0.385000` | `26.92 bps` |
| Delta | `-11` | `-1` | `+$1.017815` | `+$1.000315` | `$-0.017500` | `+18.87 bps` |

Artefacts:

- scans:
  `server-data/replay_reports/trident_ai_candidate_scan_pattern_v1_*_20260609.*`;
- inputs candidats:
  `server-data/replay_inputs/trident_ai_candidate_pattern_v1_*_20260609.jsonl`;
- replays paper:
  `server-data/replay_reports/trident_ai_candidate_paper_pattern_v1_*_20260609.*`;
- calibration pattern du nouveau panier:
  `server-data/replay_reports/trident_ai_pattern_calibration_pattern_v1_s240_tp480_t180_20260609.*`.

Lecture pattern-first:

- la progression vient d'une meilleure selection de familles de signaux, pas
  d'une regle primaire par symbole;
- dimensions positives observees:
  - `edge_bucket=3.0-4.0`: `7` trades, `$1.113287`,
    `63.62 bps`;
  - `flow_book=flow_aligned_book_neutral`: `8` trades,
    `$1.248067`, `62.40 bps`;
  - `net_edge_bucket=25-35`: `3` trades, `$0.610812`,
    `81.44 bps`;
  - `volatility=medium`: `15` trades, `$1.093969`,
    `29.17 bps`;
  - `volatility=low`: `3` trades, `$0.328969`, `43.86 bps`;
- dimensions prudentes:
  - `edge_bucket=2.0-3.0`: `11` trades, seulement `$0.049187`,
    `1.79 bps`;
  - `vwap=neutral`: `1` trade, `$-0.059682`, sample trop faible mais
    toujours a penaliser;
  - `flow_book=neutral`: `2` trades, `$-0.114372`, sample faible.

Diagnostics symboles, secondaires uniquement:

- `HYPE`: `10` trades, `$1.556264`, `62.25 bps`;
- `BTC`: `3` trades, `$0.483818`, `64.51 bps`;
- `SOL`: `2` trades, `$0.028194`, `5.64 bps`;
- `ETH`: `7` trades, `$-0.587450`, `-33.57 bps`.

Decision:

- continuer avec une approche pattern-first;
- ne pas exclure/inclure un coin sur ce sample: les symboles indiquent un risque
  de concentration, pas une regle de trading;
- ne pas appeler OpenAI sur ce panier tant que le replay deterministe n'a pas
  ete valide sur une fenetre supplementaire hors echantillon;
- prochaine etape recommandee: rejouer `research_v1` sur une nouvelle fenetre
  serveur plus fraiche ou plus longue, puis produire un rapport comparatif
  baseline vs pattern-first avant tout nouveau batch LLM.

Next step execute: validation out-of-sample pattern-first.

Objectif:

- verifier si le profil `research_v1` generalise hors des quatre fenetres ayant
  servi a calibrer les patterns;
- garder les memes gates locaux:
  `edge_to_cost >= 2.4597`, `net_edge >= 15.2579 bps`,
  `--require-microprice-alignment`;
- ne pas appeler OpenAI.

Fenetres OOS testees:

| Fenetre | Source | Raison |
|---|---|---|
| `2026-05-12 -> 2026-05-13` | `external_reference_guardrail_20260512_20260513_baseline.jsonl` | fenetre guardrail non utilisee dans les quatre replays precedents |
| `2026-04-18 -> 2026-04-23T07:49Z` | slice locale de `pod_liq_rich_20260413_20260423.jsonl` | fenetre plus longue, entre les fenetres deja testees |

Artefacts:

- input slice long:
  `server-data/replay_inputs/trident_ai_oos_pod_liq_rich_20260418_20260423_20260609.jsonl`;
- scans guardrail:
  `server-data/replay_reports/trident_ai_candidate_scan_oos_guardrail_*_20260512_20260513_20260609.*`;
- replays guardrail:
  `server-data/replay_reports/trident_ai_candidate_paper_oos_guardrail_*_20260512_20260513_20260609.*`;
- scans fenetre longue:
  `server-data/replay_reports/trident_ai_candidate_scan_oos_liq_*_20260418_20260423_20260609.*`;
- replays fenetre longue:
  `server-data/replay_reports/trident_ai_candidate_paper_oos_liq_*_20260418_20260423_20260609.*`;
- calibration OOS:
  `server-data/replay_reports/trident_ai_pattern_calibration_oos_pattern_v1_20260609.*`.

Resultats OOS:

| Fenetre | Profil | Candidats | Trades fermes | PnL realise | Avg net | Lecture |
|---|---|---:|---:|---:|---:|---|
| `2026-05-12 -> 2026-05-13` | baseline | 1 | 1 | `$-0.075351` | `-30.14 bps` | echantillon trop petit |
| `2026-05-12 -> 2026-05-13` | `research_v1` | 1 | 1 | `$-0.075351` | `-30.14 bps` | meme trade, aucun effet |
| `2026-04-18 -> 2026-04-23T07:49Z` | baseline | 28 | 9 | `$-1.741631` | `-77.41 bps` | echec net |
| `2026-04-18 -> 2026-04-23T07:49Z` | `research_v1` | 27 | 9 | `$-1.741631` | `-77.41 bps` | retire un candidat non execute, aucun gain |

Agrege OOS:

| Profil | Candidats | Trades fermes | PnL realise | Gross PnL | Fees | Avg net |
|---|---:|---:|---:|---:|---:|---:|
| Baseline | 29 | 10 | `$-1.816982` | `$-1.641982` | `$0.175000` | `-72.68 bps` |
| `research_v1` | 28 | 10 | `$-1.816982` | `$-1.641982` | `$0.175000` | `-72.68 bps` |
| Delta | `-1` | `0` | `$0.000000` | `$0.000000` | `$0.000000` | `0.00 bps` |

Calibration OOS:

- `10` trades fermes, `1` gagnant, `9` perdants;
- tous les closes sont des `time_stop`;
- `microprice=aligned` seul est insuffisant: `10` trades, `$-1.816982`,
  `-72.68 bps`;
- les patterns qui semblaient encourageants in-sample deviennent negatifs OOS:
  - `microprice=aligned | flow_book=flow_aligned_book_neutral | vwap=aligned | edge=3.0-4.0`:
    `2` trades, `$-0.603798`, `-120.76 bps`;
  - `microprice=aligned | flow_book=flow_and_book_aligned | vwap=aligned | edge=3.0-4.0`:
    `2` trades, `$-0.284545`, `-56.91 bps`;
  - `microprice=aligned | flow_book=flow_and_book_aligned | vwap=aligned | edge=>=4.0`:
    `3` trades, `$-0.191151`, `-25.49 bps`;
- diagnostic symbole secondaire:
  - `HYPE`: `9` trades, `$-1.548112`;
  - `SOL`: `1` trade, `$-0.268870`;
  - `ETH`: aucune ouverture fermee.

Decision OOS:

- ne pas promouvoir `research_v1` comme gate de decision;
- garder `research_v1` comme outil d'audit/tri exploratoire seulement;
- ne pas appeler OpenAI sur ces paniers;
- ne pas creer de blacklist coin: le probleme est un regime/pattern instable,
  pas seulement un symbole;
- prochaine etape recommandee: ajouter une notion de stabilite par fold avant
  tout score candidat. Un pattern ne doit etre bonus que s'il est positif dans
  au moins deux fenetres independantes et non catastrophique OOS; sinon il doit
  rester neutre ou etre penalise.

Next step execute: validation multi-fold et profil stable conservateur.

Objectif:

- verifier les patterns sur plusieurs folds independants, pas symbole par
  symbole;
- classer les patterns en `stable_positive`, `unstable_negative` ou
  `insufficient_fold_support`;
- interdire les bonus de score si la stabilite multi-fold n'est pas prouvee;
- garder `BTC/ETH/SOL/HYPE` comme univers, avec les symboles seulement comme
  diagnostic secondaire anti-surapprentissage.

Implementation:

- nouvelle commande CLI `pattern-fold-validation`;
- nouveau rapport JSON/Markdown de validation fold dans
  `app/trident_ai/pattern_calibration.py`;
- nouveau profil `candidate-scan --pattern-profile research_v2_stable`;
- le profil `research_v2_stable` ne donne aucun bonus positif par defaut:
  il garde les patterns non prouves neutres et penalise les patterns
  catastrophiques ou instables;
- aucun appel OpenAI.

Artefacts:

- validation fold stricte:
  `server-data/replay_reports/trident_ai_pattern_fold_validation_strict_v1_plus_oos_20260609.*`;
- scans OOS `research_v2_stable`:
  `server-data/replay_reports/trident_ai_candidate_scan_oos_*_research_v2_stable_*_20260609.*`;
- replays paper OOS `research_v2_stable`:
  `server-data/replay_reports/trident_ai_candidate_paper_oos_*_research_v2_stable_*_20260609.*`.

Resultats validation fold stricte:

| Classe | Nombre | Lecture |
|---|---:|---|
| `stable_positive` | 0 | aucun pattern n'a assez de support multi-fold pour meriter un bonus |
| `unstable_negative` | 8 | patterns a penaliser ou a surveiller |
| `insufficient_fold_support` / no-bonus | 30 | patterns a laisser neutres |

Comparaison OOS agregee, profil `stop=240,tp=480,time=180`:

| Profil | Candidats | Trades fermes | PnL realise | Gross PnL | Fees | Avg net |
|---|---:|---:|---:|---:|---:|---:|
| Baseline | 29 | 10 | `$-1.816982` | `$-1.641982` | `$0.175000` | `-72.68 bps` |
| `research_v1` | 28 | 10 | `$-1.816982` | `$-1.641982` | `$0.175000` | `-72.68 bps` |
| `research_v2_stable` | 26 | 10 | `$-1.516501` | `$-1.341501` | `$0.175000` | `-60.66 bps` |
| Delta `v2` vs baseline | `-3` | `0` | `+$0.300481` | `+$0.300481` | `$0.000000` | `+12.02 bps` |

Lecture:

- le profil stable reduit la perte OOS mais reste negatif;
- il valide la direction "patterns d'abord, symboles en diagnostic", sans
  prouver un edge tradable;
- tous les trades OOS ferment encore par `time_stop`, ce qui pointe davantage
  vers un probleme de follow-through/exit que vers un simple tuning de prompt;
- ne pas reactiver OpenAI tant que le scanner local et le replay paper restent
  negatifs OOS.

Decision:

- ne pas promouvoir `research_v2_stable`;
- le garder comme profil d'audit conservateur;
- ne pas creer de regle coin-specific;
- prochaine etape recommandee: auditer la qualite d'execution/exit sur les
  trades OOS, notamment les drifts `time_stop`, les mouvements adverses
  initiaux et le manque de follow-through apres entree.

Next step execute: audit exit/follow-through OOS.

Objectif:

- analyser les trades effectivement ouverts, pas seulement les candidats;
- reconstruire le chemin de prix entre `opened_at` et `closed_at`;
- mesurer MFE/MAE, follow-through precoce et give-back avant `time_stop`;
- garder le diagnostic symboles secondaire uniquement.

Implementation:

- ajout de `app/trident_ai/exit_audit.py`;
- nouvelle commande CLI `exit-follow-through-audit`;
- inputs: une ou plusieurs paires `--paper-journal` / `--market-input`;
- sorties JSON/Markdown;
- aucun appel OpenAI.

Artefacts OOS `research_v2_stable`:

- rapport JSON:
  `server-data/replay_reports/trident_ai_exit_follow_through_oos_research_v2_stable_20260609.json`;
- rapport Markdown:
  `server-data/replay_reports/trident_ai_exit_follow_through_oos_research_v2_stable_20260609.md`.

Resultats agreges OOS:

| Metrique | Valeur |
|---|---:|
| Trades vus | 10 |
| Trades avec chemin reconstruit | 10 |
| Close reason `time_stop` | 10 |
| Losing time-stop | 9 |
| PnL realise | `$-1.516501` |
| Avg net | `-60.66 bps` |
| Avg gross | `-53.66 bps` |
| Avg MFE | `58.80 bps` |
| Avg MAE | `-127.74 bps` |

Classifications:

| Classification | Count | Lecture |
|---|---:|---|
| `early_adverse_loss` | 9 | presque tous les perdants ont un mouvement adverse significatif tot |
| `gave_back_mfe` | 9 | presque tous les trades rendent une excursion favorable |
| `gave_back_to_loss` | 8 | la plupart des give-backs finissent en perte nette |
| `no_follow_through_loss` | 1 | le probleme principal n'est pas seulement l'absence de follow-through |

Stats precoces:

| Fenetre | Positive rate | Avg gross at window | Avg early MFE | Avg early MAE |
|---:|---:|---:|---:|---:|
| 15m | `60.00%` | `-6.65 bps` | `22.28 bps` | `-28.93 bps` |
| 30m | `50.00%` | `-2.88 bps` | `40.82 bps` | `-37.70 bps` |
| 60m | `60.00%` | `-24.89 bps` | `50.77 bps` | `-71.44 bps` |

Lecture:

- les entrees ont souvent une excursion favorable exploitable;
- le time-stop fixe a `180m` laisse trop de trades transformer du MFE en perte;
- l'adverse move precoce est un bon candidat de guardrail, mais pas suffisant
  seul car beaucoup de trades alternent MFE positif puis drawdown;
- prochaine etape recommandee: simuler zero-cout un overlay d'exit local:
  early adverse exit + protection du MFE/give-back, avant de retoucher le
  prompt LLM ou les seuils d'entree.

Next step execute: sweep overlay d'exit zero-cout OOS.

Objectif:

- comparer la sortie originale `time_stop` a des overlays locaux sans LLM;
- tester un exit adverse precoce et une protection MFE/give-back;
- conserver les memes fees aller-retour que le paper original;
- verifier si le gain tient sur les deux folds OOS.

Implementation:

- ajout de `app/trident_ai/exit_overlay.py`;
- nouvelle commande CLI `exit-overlay-sweep`;
- grille testee:
  - adverse early: `0/25/35/50 bps`;
  - fenetres adverse: `15/30/60m`;
  - activation MFE: `0/25/40/60 bps`;
  - give-back MFE: `0/20/30/45 bps`;
- aucun appel OpenAI.

Artefacts OOS `research_v2_stable`:

- rapport JSON:
  `server-data/replay_reports/trident_ai_exit_overlay_sweep_oos_research_v2_stable_20260609.json`;
- rapport Markdown:
  `server-data/replay_reports/trident_ai_exit_overlay_sweep_oos_research_v2_stable_20260609.md`.

Resultats:

| Profil | PnL | Avg net | Trades | Overlay exits | Win rate |
|---|---:|---:|---:|---:|---:|
| Baseline `time_stop` | `$-1.516501` | `-60.66 bps` | 10 | 0 | `10.00%` |
| Best sweep `ea50@15m+mfe25_gb20` | `$0.062880` | `2.52 bps` | 10 | 9 | `60.00%` |

Best sweep detail:

- delta PnL vs baseline: `+$1.579381`;
- exits: `early_adverse_exit=1`, `mfe_giveback_exit=8`,
  `original_time_stop=1`;
- fold long `2026-04-18 -> 2026-04-23`: `9` trades, PnL `$-0.082238`,
  avg net `-3.66 bps`;
- fold guardrail `2026-05-12 -> 2026-05-13`: `1` trade, PnL `$0.145118`,
  avg net `58.05 bps`;
- aucun profil du sweep n'est positif sur les deux folds.

Lecture:

- la protection MFE/give-back est la piste la plus prometteuse: elle explique
  l'essentiel de l'amelioration;
- l'early adverse seul reduit les pertes mais ne suffit pas;
- le meilleur profil agrege devient legerement positif, mais il n'est pas
  valide car le grand fold reste legerement negatif et le fold positif ne
  contient qu'un trade;
- ne pas promouvoir l'overlay et ne pas reactiver OpenAI sur cette base.

Next step recommande:

- valider l'overlay sur les quatre folds in-sample precedents plus les deux
  OOS, avec contrainte explicite: le profil candidat doit ameliorer l'agrege
  sans creer de fold catastrophique et idealement sans dependre du fold
  guardrail a un seul trade;
- si aucun profil n'est robuste, passer a un overlay plus simple: protection
  MFE uniquement, sans early adverse, pour reduire le risque de surfit.

Next step execute: validation overlay multi-fold IS+OOS.

Objectif:

- verifier si le gain de l'overlay OOS tient aussi sur les folds in-sample;
- distinguer le meilleur PnL agrege du meilleur profil robuste par fold;
- controler la concentration symbole pour eviter une conclusion "coin first";
- rester zero-cout: aucun appel OpenAI, xAI, Claude ou Gemini.

Implementation:

- `app/trident_ai/exit_overlay.py` enrichi avec metriques de robustesse:
  `improved_fold_count`, `worse_fold_count`, `worst_fold_delta_pnl_usd`,
  concentration symbole et `robust_profiles`;
- le rapport Markdown affiche maintenant:
  - baseline vs best vs best robust;
  - top profils agreges;
  - profils robustes;
  - deltas par fold.

Artefacts multi-fold:

- rapport JSON:
  `server-data/replay_reports/trident_ai_exit_overlay_sweep_multifold_research_v2_stable_20260609.json`;
- rapport Markdown:
  `server-data/replay_reports/trident_ai_exit_overlay_sweep_multifold_research_v2_stable_20260609.md`.

Folds utilises:

| Fold | Trades baseline | Type |
|---|---:|---|
| `2026-04-05 -> 2026-04-12` | 1 | IS |
| `2026-04-13 -> 2026-04-17` | 12 | IS |
| `2026-04-24 -> 2026-04-27` | 3 | IS |
| `2026-05-21 -> 2026-05-24` | 6 | IS |
| `2026-04-18 -> 2026-04-23` | 9 | OOS |
| `2026-05-12 -> 2026-05-13` | 1 | OOS guardrail |

Resultats:

| Profil | PnL | Delta PnL | Avg net | Folds ameliores | Folds degrades | Robust |
|---|---:|---:|---:|---:|---:|---|
| Baseline `time_stop` | `$-0.035675` | `$0.000000` | `-0.45 bps` | 0 | 0 | n/a |
| Best agrege `mfe60_gb20` | `$0.287814` | `+$0.323489` | `3.60 bps` | 3 | 2 | non |
| Best robust | n/a | n/a | n/a | n/a | n/a | aucun profil |

Detail du meilleur profil agrege `mfe60_gb20`:

- overlay exits: `18/32`;
- win rate: `62.50%`;
- fold `2026-04-13 -> 2026-04-17`: delta PnL `-$0.862191`;
- fold `2026-04-24 -> 2026-04-27`: delta PnL `-$0.281547`;
- fold OOS long `2026-04-18 -> 2026-04-23`: delta PnL `+$0.996808`;
- fold OOS guardrail `2026-05-12 -> 2026-05-13`: delta PnL
  `+$0.220469`, mais un seul trade;
- concentration trades: `HYPE=59.38%`; concentration PnL absolu:
  `HYPE=67.54%`.

Lecture:

- l'overlay MFE/give-back reste informatif: il montre que beaucoup de trades
  ont une excursion favorable puis rendent le gain;
- ce n'est pas encore une regle executable: le meilleur profil agrege degrade
  deux folds in-sample et aucun profil ne passe le filtre robuste
  `delta global > 0` + `aucun fold degrade`;
- la concentration HYPE vient de l'echantillon et ne doit pas devenir une these
  specifique HYPE; le travail utile est bien sur les patterns transverses;
- ne pas promouvoir l'overlay, ne pas relancer OpenAI, ne pas passer testnet.

Next step recommande:

- ajouter un audit `pattern-support` symbol-agnostic: grouper les trades par
  buckets de pattern/regime/liquidite/microstructure, puis mesurer support,
  PnL et stabilite par fold et par symbole;
- n'accorder un bonus d'entree qu'aux patterns qui ont du support multi-fold et
  multi-symboles dans l'univers initial `BTC/ETH/SOL/HYPE`;
- garder l'overlay MFE comme diagnostic de qualite des exits, pas comme regle
  de trading tant qu'il degrade des folds.

Next step execute: audit `pattern-support` symbol-agnostic.

Objectif:

- verifier les buckets de pattern sans conclure par coin;
- exiger support multi-fold et multi-symboles avant tout nouveau bonus;
- filtrer l'univers initial `BTC/ETH/SOL/HYPE`;
- rester zero-cout: aucun appel OpenAI, xAI, Claude ou Gemini.

Implementation:

- ajout de `app/trident_ai/pattern_support.py`;
- nouvelle commande CLI `pattern-support-audit`;
- rapport par familles de buckets:
  - `pattern`;
  - `side_pattern`;
  - `pattern_regime`;
  - `microstructure`;
  - `edge_liquidity`;
  - `cluster_pattern`;
- classification:
  - `symbol_agnostic_positive`;
  - `symbol_concentrated_positive`;
  - `fold_unstable`;
  - `negative_or_flat`;
  - `insufficient_support`;
  - `insufficient_symbol_or_fold_quality`.

Seuils utilises:

- closed trades minimum: `4`;
- folds minimum: `2`;
- positive folds minimum: `2`;
- symboles minimum: `2`;
- negative folds max: `0`;
- dominance symbole max: `70%`;
- catastrophic fold: avg net `<= -50 bps`.

Artefacts:

- rapport JSON:
  `server-data/replay_reports/trident_ai_pattern_support_audit_multifold_btc_eth_sol_hype_20260610.json`;
- rapport Markdown:
  `server-data/replay_reports/trident_ai_pattern_support_audit_multifold_btc_eth_sol_hype_20260610.md`.

Resultats globaux:

| Scope | Decisions | Opens | Trades | Wins | Losses | PnL | Avg net |
|---|---:|---:|---:|---:|---:|---:|---:|
| `BTC/ETH/SOL/HYPE` | 101 | 32 | 32 | 14 | 18 | `$-0.035675` | `-0.45 bps` |

Par fold:

| Fold | Trades | PnL | Avg net |
|---|---:|---:|---:|
| `2026-04-05 -> 2026-04-12` | 1 | `$-0.348276` | `-139.31 bps` |
| `2026-04-13 -> 2026-04-17` | 12 | `$0.807948` | `26.93 bps` |
| `2026-04-24 -> 2026-04-27` | 3 | `$0.808692` | `107.83 bps` |
| `2026-05-21 -> 2026-05-24` | 6 | `$0.212462` | `14.16 bps` |
| `2026-04-18 -> 2026-04-23` | 9 | `$-1.441150` | `-64.05 bps` |
| `2026-05-12 -> 2026-05-13` | 1 | `$-0.075351` | `-30.14 bps` |

Par symbole, diagnostic secondaire seulement:

| Symbole | Decisions | Trades | PnL | Avg net |
|---|---:|---:|---:|---:|
| `HYPE` | 52 | 19 | `$0.308633` | `6.50 bps` |
| `ETH` | 14 | 7 | `$-0.587450` | `-33.57 bps` |
| `SOL` | 21 | 3 | `$-0.240676` | `-32.09 bps` |
| `BTC` | 14 | 3 | `$0.483818` | `64.51 bps` |

Verdict:

- aucun bucket ne passe `symbol_agnostic_positive`;
- aucun bucket positif concentre ne doit etre promu;
- `16` buckets sont classes `fold_unstable`;
- le meilleur signal proche est `edge>=4.0`, `net_edge>=35`,
  `liquidity=high`, `cost=normal`: `4` trades, `3` folds, `3` symboles,
  PnL `$0.318352`, avg net `31.84 bps`, dominance max `ETH=50%`, mais `1`
  fold negatif;
- donc on ne doit toujours pas donner de bonus pattern, ni relancer OpenAI pour
  optimiser le prompt.

Lecture:

- les patterns comptent bien plus que le coin, mais le dataset actuel ne prouve
  pas encore un pattern executable;
- HYPE domine l'echantillon, ce qui explique beaucoup de bruit, mais BTC/ETH/SOL
  contredisent assez le signal pour interdire une promotion;
- le near-miss `edge/liquidite` est une piste de gate conservateur, pas une
  regle validee.

Next step recommande:

- ajouter puis rejouer un gate bucket strict `edge_to_cost>=4`,
  `estimated_net_edge_bps>=35`, liquidite haute et cout normal, en gardant la
  contrainte `BTC/ETH/SOL/HYPE`;
- comparer ce gate a la baseline `research_v2_stable` sur les memes folds;
- si ce gate reste negatif sur un fold OOS ou concentre sur un symbole, revenir
  au scanner neutre et accumuler plus de donnees avant tout nouvel appel LLM.

Next step execute: replay du gate strict edge/liquidite/cout.

Objectif:

- tester le near-miss detecte par `pattern-support` sans focaliser sur un coin;
- filtrer seulement par caracteristiques transverses:
  `edge_to_cost>=4`, `estimated_net_edge_bps>=35`,
  `liquidity_score>=1.2`, `round_trip_cost_bps<=12`;
- garder le meme univers initial `BTC/ETH/SOL/HYPE`;
- rester zero-cout: aucun appel OpenAI, xAI, Claude ou Gemini.

Implementation:

- ajout de gates optionnels dans `candidate-paper-replay`;
- nouveaux arguments CLI:
  - `--min-edge-to-cost`;
  - `--min-net-edge-bps`;
  - `--min-liquidity-score`;
  - `--max-round-trip-cost-bps`;
- reporting JSON/Markdown des seuils appliques;
- `pattern-support-audit` accepte maintenant les folds a zero decision meme si
  le paper journal vide n'existe pas, afin de ne pas exclure les "no trade"
  de la validation multi-fold.

Artefacts principaux:

- rapports paper strict gate:
  `server-data/replay_reports/trident_ai_candidate_paper_edge_liq_gate_*_20260610.{json,md}`;
- decisions strict gate:
  `server-data/replay_reports/trident_ai_candidate_paper_decisions_edge_liq_gate_*_20260610.jsonl`;
- audit pattern-support:
  `server-data/replay_reports/trident_ai_pattern_support_audit_edge_liq_gate_multifold_btc_eth_sol_hype_20260610.json`;
- audit Markdown:
  `server-data/replay_reports/trident_ai_pattern_support_audit_edge_liq_gate_multifold_btc_eth_sol_hype_20260610.md`.

Resultats du gate strict:

| Fold | Candidates | Decisions | Opened/Closed | PnL | Avg net |
|---|---:|---:|---:|---:|---:|
| `2026-04-05 -> 2026-04-12` | 1 | 0 | 0/0 | `$0.000000` | `0.00 bps` |
| `2026-04-13 -> 2026-04-17` | 50 | 5 | 4/4 | `$-0.066250` | `-6.62 bps` |
| `2026-04-24 -> 2026-04-27` | 6 | 1 | 1/1 | `$0.173712` | `69.48 bps` |
| `2026-05-21 -> 2026-05-24` | 18 | 2 | 1/1 | `$-0.018827` | `-7.53 bps` |
| `2026-04-18 -> 2026-04-23` | 25 | 0 | 0/0 | `$0.000000` | `0.00 bps` |
| `2026-05-12 -> 2026-05-13` | 1 | 0 | 0/0 | `$0.000000` | `0.00 bps` |
| **Total** | **101** | **8** | **6/6** | **`$0.088635`** | **`5.91 bps`** |

Comparaison a la reference multi-fold precedente:

| Profil | Decisions | Trades | PnL | Avg net | Buckets symbol-agnostic positifs |
|---|---:|---:|---:|---:|---:|
| Reference avant gate | 101 | 32 | `$-0.035675` | `-0.45 bps` | 0 |
| Gate strict edge/liquidite/cout | 8 | 6 | `$0.088635` | `5.91 bps` | 0 |

Lecture:

- le gate strict ameliore le PnL agrege et reduit fortement le turnover;
- il n'est pas robuste: deux folds trades sont negatifs, les deux folds OOS ne
  produisent aucun trade, et le meilleur bucket reste classe `fold_unstable`;
- l'audit ne trouve toujours aucun bucket `symbol_agnostic_positive`;
- le signal est donc utile comme diagnostic de selectivite, pas comme regle
  executable;
- ne pas promouvoir ce gate, ne pas relancer OpenAI, ne pas passer testnet.

Next step recommande:

- ajouter un rapport de sweep des gates edge/liquidite/cout sur les memes
  folds, avec penalite explicite pour "no trade OOS";
- chercher un compromis moins brittle que le gate strict, en exigeant support
  multi-fold et multi-symboles;
- comparer les profils par pattern transversal (`microstructure`,
  `edge_liquidity`, `cluster_pattern`) plutot que par coin;
- si aucun profil ne passe `OOS trade presence + PnL positif + absence de fold
  catastrophique`, arreter l'optimisation locale et accumuler plus de donnees
  avant tout nouvel appel LLM.

Next step execute: sweep des gates edge/liquidite/cout avec penalite OOS.

Objectif:

- verifier si un seuil moins strict que `edge>=4/net>=35/liquidity>=1.2`
  peut produire un compromis plus stable;
- penaliser explicitement les profils qui ne tradent pas les folds OOS;
- garder une evaluation par patterns transverses, pas par coin;
- rester zero-cout: aucun appel OpenAI, xAI, Claude ou Gemini.

Implementation:

- ajout de `app/trident_ai/candidate_gate_sweep.py`;
- nouvelle commande CLI `candidate-gate-sweep`;
- la commande rejoue `candidate-paper-replay` pour chaque profil et chaque
  fold, puis agrege:
  - PnL net;
  - avg net bps;
  - presence/absence de trades OOS;
  - folds negatifs;
  - folds catastrophiques;
  - support multi-symboles;
  - ranking `penalized_avg_net_bps`;
- classification par profil:
  - `robust_candidate`;
  - `oos_no_trade`;
  - `fold_unstable`;
  - `catastrophic_fold`;
  - `insufficient_trades`;
  - `insufficient_symbol_support`;
  - `negative_or_flat`.

Artefacts:

- sweep initial:
  `server-data/replay_reports/trident_ai_candidate_gate_sweep_edge_liq_btc_eth_sol_hype_20260610.json`;
- sweep initial Markdown:
  `server-data/replay_reports/trident_ai_candidate_gate_sweep_edge_liq_btc_eth_sol_hype_20260610.md`;
- sweep refine:
  `server-data/replay_reports/trident_ai_candidate_gate_sweep_edge_liq_refine_btc_eth_sol_hype_20260610.json`;
- sweep refine Markdown:
  `server-data/replay_reports/trident_ai_candidate_gate_sweep_edge_liq_refine_btc_eth_sol_hype_20260610.md`;
- artefacts par profil/fold:
  `server-data/replay_reports/trident_ai_candidate_gate_sweep_edge_liq*_artifacts/`.

Sweep initial:

- profils testes: `8`;
- grille:
  - `edge_to_cost`: `2.5`, `3.0`, `3.5`, `4.0`;
  - `estimated_net_edge_bps`: `15`, `35`;
  - `liquidity_score`: `1.0`;
  - `round_trip_cost_bps<=12`;
- penalite no-trade OOS: `25 bps`;
- profils robustes: `0`.

Top profils du sweep initial:

| Rank | Profile | Class | Trades | Symbols | OOS no-trade | Neg folds | PnL | Avg bps | Penalized bps |
|---:|---|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | `edge3p5_net15_liq1_cost12` | `fold_unstable` | 13 | 3 | 0 | 3 | `$0.151042` | `4.65` | `-25.35` |
| 2 | `edge4_net15_liq1_cost12` | `fold_unstable` | 12 | 4 | 0 | 4 | `$-0.044408` | `-1.48` | `-41.48` |
| 3 | `edge2p5_net35_liq1_cost12` | `oos_no_trade` | 6 | 4 | 2 | 2 | `$0.088635` | `5.91` | `-64.09` |
| 7 | `edge3_net15_liq1_cost12` | `catastrophic_fold` | 21 | 4 | 0 | 4 | `$0.371644` | `7.08` | `-132.92` |

Sweep refine:

- profils testes: `12`;
- grille:
  - `edge_to_cost`: `3.0`, `3.5`, `4.0`;
  - `estimated_net_edge_bps`: `10`, `15`, `20`, `25`;
  - `liquidity_score`: `1.0`;
  - `round_trip_cost_bps<=12`;
- profils robustes: `0`.

Top profils du sweep refine:

| Rank | Profile | Class | Trades | Symbols | OOS no-trade | Neg folds | PnL | Avg bps | Penalized bps |
|---:|---|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | `edge3p5_net10_liq1_cost12` | `fold_unstable` | 13 | 3 | 0 | 3 | `$0.151042` | `4.65` | `-25.35` |
| 2 | `edge3p5_net15_liq1_cost12` | `fold_unstable` | 13 | 3 | 0 | 3 | `$0.151042` | `4.65` | `-25.35` |
| 3 | `edge3p5_net20_liq1_cost12` | `fold_unstable` | 13 | 3 | 0 | 3 | `$0.151042` | `4.65` | `-25.35` |
| 8 | `edge3_net20_liq1_cost12` | `catastrophic_fold` | 19 | 4 | 0 | 3 | `$1.684588` | `35.47` | `-44.53` |

Detail important:

- `edge3_net20_liq1_cost12` semble attractif en PnL brut (`+$1.684588`), mais
  il inclut un fold catastrophique `2026-04-05 -> 2026-04-12` a `-139.31 bps`;
- le meilleur profil penalise trade bien l'OOS, mais les deux folds OOS sont
  perdants:
  - `2026-04-18 -> 2026-04-23`: `-$0.154675`, `-15.47 bps`;
  - `2026-05-12 -> 2026-05-13`: `-$0.075351`, `-30.14 bps`;
- les profils stricts `net>=35` redeviennent `oos_no_trade`;
- les profils plus larges ouvrent l'OOS mais deviennent `fold_unstable` ou
  `catastrophic_fold`.

Verdict:

- aucun profil edge/liquidite/cout ne passe le filtre robuste;
- continuer a ajuster ces seuils risque de faire du curve-fitting;
- ne pas promouvoir de gate, ne pas relancer OpenAI, ne pas passer testnet;
- le probleme n'est plus seulement la selectivite, mais le contexte d'entree et
  le chemin de risque des folds perdants.

Next step recommande:

- ajouter un audit `failure-pattern` centre sur les folds perdants:
  comparer les trades gagnants/perdants par regime, microstructure, side,
  close reason, adverse move initial, concentration symbole et horizon;
- chercher des vetoes transverses de contexte, pas des seuils edge plus fins;
- inclure explicitement les folds OOS et le fold catastrophique
  `2026-04-05 -> 2026-04-12`;
- si aucun pattern d'echec multi-fold/multi-symbole n'apparait, stopper
  l'optimisation locale et accumuler plus de donnees avant nouvel appel LLM.

Next step execute: audit `failure-pattern` multi-fold.

Objectif:

- identifier des patterns d'echec transverses, pas des exclusions par coin;
- relier chaque trade paper ferme a son contexte d'entree, son chemin de prix
  local (`MFE/MAE`, adverse move initial, follow-through) et son close reason;
- produire des buckets candidats a veto uniquement s'ils sont multi-fold,
  multi-symboles et suffisamment perdants.

Implementation:

- ajout de `app/trident_ai/failure_pattern.py`;
- nouvelle commande CLI `failure-pattern-audit`;
- tests `tests/test_trident_ai_failure_pattern.py` et smoke CLI;
- aucun impact deploy/fetch: outil local de recherche dans
  `server-data/replay_reports/`.

Artefacts:

- profil penalise `edge3p5_net10_liq1_cost12`:
  `server-data/replay_reports/trident_ai_failure_pattern_edge3p5_net10_liq1_cost12_btc_eth_sol_hype_20260610.json`;
- Markdown:
  `server-data/replay_reports/trident_ai_failure_pattern_edge3p5_net10_liq1_cost12_btc_eth_sol_hype_20260610.md`;
- profil PnL brut attractif mais fold catastrophique `edge3_net20_liq1_cost12`:
  `server-data/replay_reports/trident_ai_failure_pattern_edge3_net20_liq1_cost12_btc_eth_sol_hype_20260610.json`;
- Markdown:
  `server-data/replay_reports/trident_ai_failure_pattern_edge3_net20_liq1_cost12_btc_eth_sol_hype_20260610.md`.

Resultats:

| Profil | Trades | Wins | Losses | PnL | Avg bps | Buckets veto candidats |
|---|---:|---:|---:|---:|---:|---:|
| `edge3p5_net10_liq1_cost12` | 13 | 7 | 6 | `$0.151042` | `4.65` | 11 |
| `edge3_net20_liq1_cost12` | 19 | 12 | 7 | `$1.684588` | `35.47` | 10 |

Patterns observes:

- les labels `gave_back_to_loss`, `losing_time_stop`, `early_adverse_loss` sont
  des diagnostics post-trade utiles, mais pas des vetoes d'entree utilisables;
- le bucket entry-time recurrent le plus visible est
  `side_pattern::side=short|microprice=aligned|flow_book=flow_and_book_aligned|vwap=aligned|edge=>=4.0`;
- ce bucket apparait sur plusieurs folds et symboles, mais doit etre teste en
  counterfactual, car retirer une decision peut changer la sequence des trades
  paper suivants.

Next step execute: replay/sweep de veto entry-time.

Objectif:

- tester les buckets entry-time candidats avant de modifier le scanner ou le
  prompt;
- mesurer le counterfactual complet avec le meme paper engine, pas seulement
  retirer ex-post les trades perdants;
- rester zero-cout: aucun appel OpenAI/xAI/Claude/Gemini.

Implementation:

- ajout de `app/trident_ai/entry_veto.py`;
- nouvelles commandes CLI:
  - `entry-veto-replay`;
  - `entry-veto-sweep`;
- tests `tests/test_trident_ai_entry_veto.py` et smoke CLI;
- gestion robuste d'un baseline paper manquant quand un fold n'avait aucun
  trade ferme;
- aucun impact deploy/fetch: outil local de recherche dans
  `server-data/replay_reports/`.

Artefacts replay single-bucket:

- profil `edge3p5_net10_liq1_cost12`:
  `server-data/replay_reports/trident_ai_entry_veto_edge3p5_net10_short_aligned_edge4_btc_eth_sol_hype_20260610.json`;
- profil `edge3_net20_liq1_cost12`:
  `server-data/replay_reports/trident_ai_entry_veto_edge3_net20_short_aligned_edge4_btc_eth_sol_hype_20260610.json`.

Verdict single-bucket
`side_pattern::side=short|microprice=aligned|flow_book=flow_and_book_aligned|vwap=aligned|edge=>=4.0`:

| Profil | Baseline PnL | Veto PnL | Delta | Verdict |
|---|---:|---:|---:|---|
| `edge3p5_net10_liq1_cost12` | `$0.151042` | `$-0.243415` | `$-0.394457` | `rejected_delta_pnl_non_positive` |
| `edge3_net20_liq1_cost12` | `$1.684588` | `$1.290131` | `$-0.394457` | `rejected_delta_pnl_non_positive` |

Artefacts sweep multi-bucket:

- profil `edge3p5_net10_liq1_cost12`:
  `server-data/replay_reports/trident_ai_entry_veto_sweep_edge3p5_net10_btc_eth_sol_hype_20260610.json`;
- profil `edge3_net20_liq1_cost12`:
  `server-data/replay_reports/trident_ai_entry_veto_sweep_edge3_net20_btc_eth_sol_hype_20260610.json`;
- artefacts par bucket:
  `server-data/replay_reports/trident_ai_entry_veto_sweep_*_artifacts/`.

Buckets entry-time testes:

- `side::side=short`;
- `side_pattern::side=short|microprice=aligned|flow_book=flow_and_book_aligned|vwap=aligned|edge=>=4.0`;
- `cluster_pattern::cluster=crypto|microprice=aligned|flow_book=flow_and_book_aligned|vwap=aligned|edge=>=4.0`;
- `pattern::microprice=aligned|flow_book=flow_and_book_aligned|vwap=aligned|edge=>=4.0`;
- `pattern_regime::microprice=aligned|flow_book=flow_and_book_aligned|vwap=aligned|edge=>=4.0|regime=unknown`;
- `microstructure::microprice=aligned|flow_book=mixed_conflict|vwap=aligned`.

Verdict sweep:

- aucun bucket entry-time teste ne produit un delta PnL positif;
- meilleur bucket profil `edge3p5_net10_liq1_cost12`: `side::side=short`,
  delta `$-0.346508`, `worse_folds=3`;
- meilleur bucket profil `edge3_net20_liq1_cost12`: le `side_pattern` ci-dessus,
  delta `$-0.394457`, `worse_folds=2`;
- ne pas promouvoir de veto entry-time;
- ne pas durcir le scanner sur ces buckets: ils confondent "trade perdant
  observe" et "decision utile a retirer dans une sequence paper complete".

Blocage actuel avant next step:

- les etapes locales zero-cout disponibles ont ete epuisees sur ce dataset:
  gate sweep, failure-pattern, entry-veto counterfactual;
- le prochain travail utile est un recalibrage du prompt LLM ou un petit replay
  LLM payant sur un panier tres borne;
- ne pas lancer ce replay sans validation explicite du budget, car il consomme
  l'API OpenAI;
- recommandation si validation: batch tres petit, `max_live_calls<=4`,
  `max_incremental_cost_usd<=0.02`, puis cache-only immediat et paper replay.

Next step execute: mini replay LLM payant v8 sous cap strict.

Validation operateur recue. Test lance sur le fold
`2026-04-13 -> 2026-04-17`, fichier:

`server-data/replay_inputs/trident_ai_candidate_pattern_v1_20260413_20260417_20260609.jsonl`.

Commande bornee:

- `max_records=4`;
- `max_contexts=4`;
- `max_live_calls=4`;
- `max_incremental_cost_usd=0.02`;
- univers `BTC,ETH,SOL,HYPE`;
- aucun ordre exchange, replay LLM/paper uniquement.

Artefacts:

- replay LLM payant:
  `server-data/replay_reports/trident_ai_prompt_recalibration_paid_v8_004_20260610.json`;
- replay LLM cache-only de verification:
  `server-data/replay_reports/trident_ai_prompt_recalibration_cache_replay_v8_004_20260610.json`;
- paper replay:
  `server-data/replay_reports/trident_ai_prompt_recalibration_paper_v8_004_20260610.json`;
- calibration:
  `server-data/replay_reports/trident_ai_prompt_recalibration_calibration_v8_004_20260610.json`;
- edge calibration:
  `server-data/replay_reports/trident_ai_prompt_recalibration_edge_calibration_v8_004_20260610.json`.

Resultats LLM:

- `live_llm_calls=4`;
- `llm_failures=0`;
- `proposals_generated=4`;
- `proposals_accepted=4`;
- `action_counts={"open": 4}`;
- cout incremental estime: `$0.00770625`;
- cache-only immediat: `cache_hits=4`, `live_llm_calls=0`, cout incremental
  `$0.0`.

Resultats paper:

| Trade | Side | Close | PnL |
|---|---|---|---:|
| `HYPE 2026-04-13T17:50Z` | short | `invalidation_price_hit` | `$-0.299164` |
| `HYPE 2026-04-13T22:06Z` | long | `take_profit_hit` | `$0.298411` |
| `BTC 2026-04-13T22:53Z` | long | `invalidation_price_hit` | `$-0.048506` |
| `BTC 2026-04-16T16:33Z` | short | `time_stop` | `$0.035548` |

Synthese paper:

- PnL brut: `$0.056289`;
- fees: `$0.070000`;
- PnL realise: `$-0.013711`;
- net apres cout IA: `$-0.02141725`;
- win rate: `2/4`;
- close reasons: `invalidation_price_hit=2`, `take_profit_hit=1`,
  `time_stop=1`.

Diagnostic:

- le prompt v8 ne filtre pas assez: il ouvre les `4/4` premiers candidats;
- la selectivite du LLM n'apporte pas de valeur sur ce batch si elle se limite
  a confirmer le scanner local;
- les deux pertes auraient ete bloquees par un seuil plus strict
  `edge_to_cost>=3.25` et `net_edge_bps>=25`;
- le petit gagnant BTC short aurait aussi ete bloque, donc le seuil doit etre
  teste comme hypothese prudente, pas promu directement.

Next step execute local: prompt v9 strict.

Implementation:

- `TRIDENT_AI_REPLAY_PROMPT_VERSION = "trident_ai_replay_v9"`;
- ajout de flags `ctx.candidate.passes.research_*`;
- nouveau `research_gate`:
  - `edge_to_cost>=3.25`;
  - `net_edge_bps>=25`;
  - `round_trip_cost_bps<=12`;
  - pas de conflit microprice;
- regle prompt: `open` seulement si `ctx.candidate.passes.research_gate=true`;
- tests cibles OK:
  `uv run pytest tests/test_trident_ai_llm_replay.py tests/test_trident_ai_llm.py tests/test_trident_ai_cli.py`
  -> `37 passed`;
- probe cache-only v9:
  `server-data/replay_reports/trident_ai_prompt_v9_cache_only_probe_004_20260610.json`;
- resultat probe: `cache_hits=0`, `live_llm_calls=0`,
  `rejection_reasons={"cache_miss_live_calls_disabled": 4}`.

Next step execute: mini replay LLM payant v9 sous cap strict.

Validation operateur recue. Test lance sur le meme mini-batch que v8 pour
mesurer uniquement l'effet du prompt/gate v9.

Commande bornee:

- `max_records=4`;
- `max_contexts=4`;
- `max_live_calls=4`;
- `max_incremental_cost_usd=0.02`;
- univers `BTC,ETH,SOL,HYPE`;
- aucun ordre exchange, replay LLM/paper uniquement.

Artefacts:

- replay LLM payant:
  `server-data/replay_reports/trident_ai_prompt_recalibration_paid_v9_004_20260610.json`;
- replay LLM cache-only de verification:
  `server-data/replay_reports/trident_ai_prompt_recalibration_cache_replay_v9_004_20260610.json`;
- paper replay:
  `server-data/replay_reports/trident_ai_prompt_recalibration_paper_v9_004_20260610.json`;
- calibration:
  `server-data/replay_reports/trident_ai_prompt_recalibration_calibration_v9_004_20260610.json`;
- edge calibration:
  `server-data/replay_reports/trident_ai_prompt_recalibration_edge_calibration_v9_004_20260610.json`.

Resultats LLM v9:

- `live_llm_calls=4`;
- `llm_failures=0`;
- `proposals_generated=4`;
- `proposals_accepted=4`;
- `action_counts={"hold": 3, "open": 1}`;
- cout incremental estime: `$0.00799875`;
- cache-only immediat: `cache_hits=4`, `live_llm_calls=0`, cout incremental
  `$0.0`.

Decisions v9:

| Candidat | Decision | Tags principaux |
|---|---|---|
| `HYPE 2026-04-13T22:06Z long` | `open` | `research_gate_pass`, `bullish_ema_flow`, `book_microprice_aligned` |
| `BTC 2026-04-16T16:33Z short` | `hold` | `research_gate_failed`, `net_edge_below_threshold`, `edge_to_cost_below_threshold` |
| `HYPE 2026-04-13T17:50Z short` | `hold` | `research_gate_failed`, `microprice_ok`, `bearish_bias` |
| `BTC 2026-04-13T22:53Z long` | `hold` | `research_gate_failed`, `edge_to_cost_below_requirement`, `net_edge_below_requirement` |

Resultats paper v9:

| Trade | Side | Close | PnL |
|---|---|---|---:|
| `HYPE 2026-04-13T22:06Z` | long | `time_stop` | `$0.252190` |

Synthese paper:

- PnL brut: `$0.269690`;
- fees: `$0.017500`;
- PnL realise: `$0.252190`;
- net apres cout IA: `$0.24419125`;
- win rate: `1/1`;
- close reasons: `time_stop=1`.

Comparaison directe v8 -> v9 sur ce mini-batch:

| Version | Opens | Holds | PnL realise | Net apres cout IA |
|---|---:|---:|---:|---:|
| `v8` | 4 | 0 | `$-0.013711` | `$-0.02141725` |
| `v9` | 1 | 3 | `$0.252190` | `$0.24419125` |

Diagnostic:

- v9 corrige le probleme principal de v8: le LLM ne se contente plus de
  confirmer tous les candidats du scanner local;
- sur ce batch, v9 evite les deux pertes et le petit gagnant fragile de v8, et
  conserve uniquement le HYPE long gagnant;
- l'edge calibration v9 reste trop petite pour promouvoir quoi que ce soit:
  `closed_trades=1`, `false_positive_trades=0`,
  `avg_realized_net_bps=100.876`, `avg_estimated_net_edge_bps=26.88216`;
- le seuil suggere par ce seul winner (`min_edge_to_cost=1.5`,
  `min_net_edge_bps=5.0`) ne doit pas etre applique: sample trop petit et deja
  influence par le tuning du batch.

Verdict:

- prompt v9 valide comme hypothese de recherche, pas comme policy promue;
- conserver les gates stricts actuels pour eviter de reouvrir trop large;
- ne pas lancer de batch plus grand sans validation budgetaire explicite, car
  chaque nouveau contexte v9 vide le cache et consomme l'API OpenAI.

Next step execute: premier fold out-of-sample v9 sous budget.

Validation operateur recue via `continue`. Budget applique:

- `max_live_calls=4`;
- `max_incremental_cost_usd=0.02`;
- aucun ordre exchange, replay LLM/paper uniquement.

Micro-batch OOS diversifie construit depuis le fold liquide
`2026-04-18 -> 2026-04-23` pour eviter un test trop mono-symbole:

`server-data/replay_inputs/trident_ai_candidate_oos_liq_diverse_v9_004_20260610.jsonl`

Composition:

| Candidat | Side | Edge/cost | Net edge | Lecture v9 |
|---|---|---:|---:|---|
| `HYPE 2026-04-20T15:47Z` | short | `4.1121` | `28.6892` | passe gate v9 |
| `HYPE 2026-04-21T17:40Z` | long | `3.9880` | `26.9044` | passe gate v9 |
| `SOL 2026-04-20T07:22Z` | long | `3.8483` | `26.1206` | passe gate v9 |
| `ETH 2026-04-20T17:02Z` | short | `2.7551` | `15.5652` | doit rester hold |

Artefacts:

- replay LLM payant:
  `server-data/replay_reports/trident_ai_oos_liq_diverse_paid_v9_004_20260610.json`;
- replay LLM cache-only:
  `server-data/replay_reports/trident_ai_oos_liq_diverse_cache_replay_v9_004_20260610.json`;
- paper replay:
  `server-data/replay_reports/trident_ai_oos_liq_diverse_paper_v9_004_20260610.json`;
- calibration:
  `server-data/replay_reports/trident_ai_oos_liq_diverse_calibration_v9_004_20260610.json`;
- edge calibration:
  `server-data/replay_reports/trident_ai_oos_liq_diverse_edge_calibration_v9_004_20260610.json`.

Resultats LLM:

- `live_llm_calls=4`;
- `llm_failures=0`;
- `action_counts={"hold": 1, "open": 3}`;
- cout incremental estime: `$0.007983`;
- cache-only immediat: `cache_hits=4`, `live_llm_calls=0`, cout incremental
  `$0.0`.

Resultats paper OOS:

| Trade | Side | Close | PnL |
|---|---|---|---:|
| `SOL 2026-04-20T07:22Z` | long | `invalidation_price_hit` | `$-0.195548` |
| `HYPE 2026-04-20T15:47Z` | short | `time_stop` | `$-0.046206` |
| `HYPE 2026-04-21T17:40Z` | long | `invalidation_price_hit` | `$-0.199432` |
| `ETH 2026-04-20T17:02Z` | short | `hold/no_op` | `$0.0` |

Synthese:

- PnL brut: `$-0.388686`;
- fees: `$0.052500`;
- PnL realise: `$-0.441186`;
- net apres cout IA: `$-0.449169`;
- win rate: `0/3`;
- close reasons: `invalidation_price_hit=2`, `time_stop=1`.

Edge calibration OOS:

- `closed_trades=3`;
- `false_positive_trades=3`;
- `avg_estimated_net_edge_bps=27.23806`;
- `avg_realized_net_bps=-58.8248`;
- `avg_abs_edge_error_bps=95.193927`;
- `suggested_min_edge_to_cost=4.2121`;
- `suggested_min_net_edge_bps=30.6892`;
- warning: `sample_too_small_keep_conservative_gates`.

Diagnostic:

- le prompt v9 fait bien respecter le gate: l'ETH sous seuil reste `hold`;
- le probleme n'est donc pas seulement le prompt, mais l'estimateur local
  `estimated_edge_bps` / `estimated_net_edge_bps`, qui surestime fortement les
  setups passants sur ce fold OOS;
- les trois faux positifs avaient microprice aligne et edge local positif:
  durcir aveuglement le prompt ne suffit pas;
- le seuil suggere par ce seul OOS (`edge_to_cost>4.21`, `net_edge>30.69`)
  bloquerait aussi le seul gagnant observe du mini-batch v9 precedent
  (`HYPE 2026-04-13T22:06Z`, edge/cost `3.6142`, net edge `26.8822`), donc il
  ne doit pas etre promu tel quel.

Next step execute local: sweep zero-cout des gates stricts v9/OOS.

Un sweep large multi-fold a ete lance puis interrompu car trop lent sur les
gros fichiers; les artefacts partiels ont ete supprimes. Un sweep cible et
complet a ensuite ete lance sur:

- fold de recalibration v9 `2026-04-13 -> 2026-04-17`;
- fold OOS liquide `2026-04-18 -> 2026-04-23`;
- fold OOS guardrail `2026-05-12 -> 2026-05-13`.

Artefacts:

- rapport:
  `server-data/replay_reports/trident_ai_candidate_gate_sweep_v9_oos_focused_20260610.json`;
- Markdown:
  `server-data/replay_reports/trident_ai_candidate_gate_sweep_v9_oos_focused_20260610.md`;
- artefacts par profil:
  `server-data/replay_reports/trident_ai_candidate_gate_sweep_v9_oos_focused_20260610_artifacts/`.

Grille:

- `edge_to_cost`: `3.75`, `4.0`, `4.25`;
- `estimated_net_edge_bps`: `25`, `30`;
- `liquidity_score>=1.0`;
- `round_trip_cost_bps<=12`;
- `min_total_closed_trades=2`;
- `min_symbols=2`;
- penalites OOS/no-trade et folds negatifs conservees.

Resultats sweep cible:

- profils testes: `6`;
- profil robuste: `0`;
- classifications:
  - `catastrophic_fold=1`;
  - `fold_unstable=1`;
  - `oos_no_trade=4`.

Top profils:

| Profile | Class | Trades | Symbols | PnL | Avg bps | Penalized bps | Neg folds |
|---|---|---:|---:|---:|---:|---:|---:|
| `edge4_net25_liq1_cost12` | `fold_unstable` | 12 | 3 | `$-1.129354` | `-37.6451` | `-67.6451` | 3 |
| `edge3p75_net30_liq1_cost12` | `oos_no_trade` | 10 | 3 | `$-1.071166` | `-42.8466` | `-87.8466` | 2 |
| `edge4_net30_liq1_cost12` | `oos_no_trade` | 10 | 3 | `$-1.071166` | `-42.8466` | `-87.8466` | 2 |
| `edge4p25_net25_liq1_cost12` | `oos_no_trade` | 10 | 3 | `$-1.071166` | `-42.8466` | `-87.8466` | 2 |
| `edge3p75_net25_liq1_cost12` | `catastrophic_fold` | 14 | 3 | `$-1.873587` | `-53.5311` | `-133.5311` | 3 |

Verdict:

- ne pas faire de nouvel appel LLM payant maintenant;
- ne pas promouvoir v9;
- ne pas chercher a "sauver" la qualite des trades par prompt seul;
- priorite: recalibrer localement l'estimateur d'edge et les sorties paper sur
  les faux positifs OOS, puis seulement relancer un micro-batch LLM si un gate
  local robuste apparait.

Blocage actuel avant next step payant:

- tout nouvel appel OpenAI doit attendre une nouvelle validation budgetaire;
- le prochain travail utile est zero-cout: audit des faux positifs OOS par
  trajectoire post-entree, MFE/MAE, vitesse d'invalidation, regime et
  concentration HYPE/SOL, afin de corriger l'estimateur local avant un prompt
  v10.

Next step execute local: recalibration edge/path et instrumentation de replay.

Objectif:

- ne pas refaire d'appel OpenAI tant qu'un diagnostic local n'explique pas les
  faux positifs v9;
- verifier si le probleme vient du prompt, du score d'edge, ou de la trajectoire
  post-entree;
- garder une lecture symbol-agnostic: les patterns de chemin importent plus que
  le coin isole.

Modifications code:

- les journaux `llm-replay` conservent maintenant le `trident_ai_candidate`
  dans `context` quand un hint candidat est present. Cela evite de perdre les
  features qui ont justifie l'appel LLM;
- nouvelle commande CLI `edge-path-calibration`;
- nouveau module `app/trident_ai/edge_path_calibration.py`;
- le rapport joint quatre couches:
  - input candidat local;
  - decision LLM;
  - replay paper;
  - trajectoire marche apres entree avec MFE/MAE et windows `5/15/30/60m`;
- tests ajoutes pour le rapport et pour l'enrichissement des journaux LLM.

Artefacts generes:

- audit trajectoire:
  `server-data/replay_reports/trident_ai_v9_path_audit_is_oos_20260611.md`;
- sweep overlay exit:
  `server-data/replay_reports/trident_ai_v9_exit_overlay_is_oos_20260611.md`;
- calibration edge/path jointe:
  `server-data/replay_reports/trident_ai_v9_edge_path_calibration_is_oos_20260611.md`;
- miroir exact du mini-batch IS v9:
  `server-data/replay_inputs/trident_ai_candidate_recalibration_v9_is_004_20260611.jsonl`.

Resultats audit trajectoire IS+OOS v9:

- trades fermes: `4`;
- PnL realise total: `$-0.188996`;
- fold IS: `1` gagnant HYPE long, `$+0.252190`, `+100.88 bps`;
- fold OOS: `3` perdants, `$-0.441186`, `-58.82 bps`;
- faux positifs OOS: `3/3`;
- pertes avec mouvement adverse rapide: `3/3`;
- invalidations rapides: `2/3`, a environ `12m` et `14m`;
- un short HYPE avait une MFE favorable puis a rendu le gain et fini perdant.

Lecture:

- l'unique gagnant a une trajectoire constructive malgre une MAE initiale;
- les perdants OOS ne sont pas un probleme de coin unique: ils partagent surtout
  un pattern `early_adverse_loss`, `no_follow_through_loss` ou
  `gave_back_to_loss`;
- HYPE est mixte dans l'echantillon: un gagnant IS, un short OOS qui donne puis
  rend, un long OOS invalide. Il ne faut donc pas ajouter une regle
  coin-specifique.

Resultats sweep overlay exit:

- baseline IS+OOS: `$-0.188996`;
- meilleur profil brut: `ea35@10m+mfe75_gb20`, PnL `$+0.058391`, mais il degrade
  le gagnant IS;
- meilleur profil robuste sans degradation de fold: `ea35@10m`;
- `ea35@10m` conserve le gagnant IS et ameliore l'OOS de `$-0.441186` a
  `$-0.322820`, mais reste perdant.

Verdict overlay:

- l'exit overlay reduit une partie des pertes;
- il ne suffit pas encore a rendre v9 promouvable;
- il doit etre valide sur plus de folds et combine a une logique de
  follow-through/giveback avant tout nouveau paiement LLM.

Resultats `edge-path-calibration`:

- candidats vus: `8`;
- decisions LLM matchees: `8`;
- opens/holds: `4/4`;
- trades fermes: `4`;
- faux positifs: `3`;
- PnL realise: `$-0.188996`;
- moyenne edge net estime: `27.15 bps`;
- moyenne PnL net realise: `-18.90 bps`;
- verdict: `edge_thresholds_do_not_separate_winners_from_false_positives`.

Diagnostics de seuil:

- max faux positif `estimated_net_edge_bps`: `28.6892`;
- min winner `estimated_net_edge_bps`: `26.8822`;
- max faux positif `edge_to_cost`: `4.1121`;
- min winner `edge_to_cost`: `3.6142`;
- seuil net suggere `30.6892 bps`: bloquerait aussi le winner;
- seuil edge/cost suggere `4.2121`: bloquerait aussi le winner;
- warning maintenu: `sample_too_small_keep_conservative_gates`.

Conclusion:

- le prompt v9 n'est pas le point faible principal sur ce batch: il respecte le
  gate et garde les candidats sous seuil en `hold`;
- l'estimateur local d'edge confond encore edge theorique et edge reellement
  tradable apres frais, invalidation et giveback;
- un nouveau prompt v10 sans meilleure policy locale aurait de bonnes chances
  de consommer du budget pour confirmer le meme diagnostic;
- aucun nouvel appel LLM payant n'est justifie avant validation zero-cout d'un
  overlay robuste multi-fold.

Next step zero-cout:

- valider `ea35@10m` et des variantes follow-through/giveback sur plusieurs
  folds, avec penalite si un fold gagnant est degrade;
- si aucun profil ne reste robuste, revenir au scanner local au lieu de relancer
  OpenAI;
- si un profil robuste apparait, demander validation explicite avant un
  micro-replay LLM payant sous cap strict.

Impact deploy/fetch:

- aucun impact `deploy.sh`, `docker-compose.trident.yml`,
  `scripts/trident_server.sh` ou `scripts/fetch_trident_data.sh`;
- changements limites a la recherche locale, aux rapports et aux tests;
- aucun ordre exchange, aucun service live, aucun secret expose.

Next step execute local: overlay `no-follow-through`.

Motivation:

- les overlays `early_adverse` coupent des faux positifs, mais ils coupent aussi
  des winners qui commencent par respirer avant de suivre;
- le pattern le plus exploitable est plutot l'absence de follow-through:
  apres une fenetre donnee, si la position n'a jamais imprime une MFE minimale
  et reste faible, elle peut etre coupee plus tot;
- cette logique est testable sans LLM, car elle depend seulement du chemin
  marche apres entree.

Implementation:

- `exit-overlay-sweep` accepte maintenant:
  - `--follow-through-window-minutes-values`;
  - `--min-follow-through-bps-values`;
  - `--max-follow-through-gross-bps-values`;
- le comportement historique reste inchange par defaut: le nouvel overlay est
  desactive si ces valeurs restent a `0`;
- nouveau motif de sortie simule: `no_follow_through_exit`;
- test dedie ajoute: le no-follow coupe un perdant sans toucher un winner qui a
  deja imprime assez de MFE.

Artefacts:

- overlay pattern_v1 multi-fold:
  `server-data/replay_reports/trident_ai_pattern_v1_nofollow_overlay_multifold_20260611.md`;
- overlay v9 IS/OOS:
  `server-data/replay_reports/trident_ai_v9_nofollow_overlay_is_oos_20260611.md`;
- failure-pattern multi-fold pattern_v1:
  `server-data/replay_reports/trident_ai_pattern_v1_failure_pattern_multifold_20260611.md`.

Resultats pattern_v1 multi-fold:

- baseline: `26` trades, PnL `$-0.548618`, avg net `-8.44 bps`;
- meilleur profil brut: `nft40@15m_max10`;
- PnL meilleur brut: `$0.635667`, delta `$+1.184285`, avg net `9.78 bps`;
- exits overlay: `17`;
- profil robuste: `0`;
- degradation observee: le fold `pattern_v1_is_20260424_20260427` passe de
  `$0.808692` a `$0.703646`, soit `$-0.105046`.

Resultats v9 IS/OOS:

- baseline: `4` trades, PnL `$-0.188996`, avg net `-18.90 bps`;
- meilleur profil brut: `nft15@5m_max10`;
- PnL meilleur brut: `$-0.181415`, delta seulement `$+0.007581`;
- fold OOS ameliore de `$+0.259689`;
- fold IS degrade de `$-0.252108`, car le seul winner HYPE est ferme trop tot;
- profil robuste: `0`.

Failure-pattern pattern_v1:

- trades: `26`;
- wins/losses: `11/15`;
- PnL: `$-0.548618`;
- bucket `no_follow_through_loss`: `5` pertes sur `5`, `4` folds, PnL
  `$-1.211575`;
- mais les buckets pre-entry restent trop mixtes ou trop petits pour une regle
  de veto agressive;
- ne pas transformer ces observations en regle par coin: les patterns de chemin
  expliquent mieux les resultats que `HYPE`, `SOL`, `ETH` ou `BTC` seuls.

Verdict 7q:

- le no-follow-through est une bonne brique d'analyse et doit rester dans le
  tooling;
- il n'est pas assez robuste pour etre promu en policy;
- aucun nouvel appel OpenAI n'a ete fait pendant cette etape;
- le prochain test utile cote LLM serait un micro-replay payant v10, limite par
  budget, pour verifier si un prompt/critic plus selectif evite ces faux
  positifs sans couper les winners;
- ne pas lancer ce micro-replay sans validation operateur explicite.

Blocage actuel avant next step payant:

- le prochain step qui change l'information disponible est payant: nouveau
  batch LLM hors cache;
- recommandation de cap si validation: `max_live_calls<=4` et
  `max_incremental_cost_usd<=0.02`;
- tant que ce n'est pas valide, continuer uniquement les audits/reports locaux
  et ne pas activer de live/testnet.

### Etape 9 - Testnet live

Ajouter service Docker optionnel, disabled par defaut:

- `trident-ai-testnet`;
- config testnet;
- state dedie;
- preflight/reconciliation;
- alertes crash.

A ce stade seulement, mettre a jour:

- `docker-compose.trident.yml`;
- `deploy.sh`;
- `scripts/trident_server.sh` si UI/API expose `tridentAI`;
- `scripts/fetch_trident_data.sh` pour rapatrier logs/status/reviews
  `tridentAI`;
- docs deployment.

### Etape 10 - Mainnet paper puis canary

Ne pas passer live sans:

- rapport replay;
- rapport shadow;
- rapport testnet;
- preflight mainnet paper;
- caps tiny;
- confirmation manuelle.

## Impact deploy/fetch actuel

Les etapes research/shadow actuelles ajoutent seulement des modules Python, une
config, des tests et des runners locaux/replay. Elles n'ajoutent aucun service
Docker, aucune commande de deploiement, aucune lecture de secret et aucun chemin
d'execution live.

Aucun changement de `deploy.sh`, `docker-compose.trident.yml`,
`scripts/trident_server.sh` ou `scripts/fetch_trident_data.sh` n'est requis a ce
stade.

Ces scripts devront etre mis a jour au moment ou `tridentAI` devient un service
deploye, ou si ses logs/reports doivent etre rapatries automatiquement depuis le
serveur.

## Questions ouvertes

- Le premier usage IA doit-il etre `proposal engine` ou simplement `risk
  overlay` sur les plans de Pod A/C?
- Quel budget mensuel maximum est acceptable tant que le capital reste proche
  de `1000-2000 USD`?
- Veut-on une approbation humaine obligatoire pour les 20 premiers vrais ordres?

## Recommandation finale

Construire `tridentAI` comme une experience independante dans le repo TRIDENT,
pas comme un bot from scratch et pas comme un pod live immediat.

Ordre recommande:

1. `AI risk overlay` en shadow sur les signaux A/C existants.
2. `AI proposal engine` shadow sur `BTC/ETH/SOL/HYPE`.
3. Replay avec cache LLM et comparaison baseline.
4. Dry-run/paper.
5. Intel/news-social xAI en shadow, optionnel, comme veto seulement.
6. Testnet live tiny.
7. Mainnet paper.
8. Mainnet canary sur compte Hyperliquid independant.

Le premier objectif n'est pas "laisser l'IA trader", mais mesurer si elle
ameliore les vetoes, reduit les mauvaises entrees, ou identifie des regimes que
les features deterministes ne capturent pas encore. Si elle ne bat pas une
baseline nette apres couts, elle reste un outil d'analyse, pas un trader.
