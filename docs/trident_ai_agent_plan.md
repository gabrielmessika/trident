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
- [ ] Etape 7 - Dry-run/paper avec executor simule et cout IA net.
- [ ] Etape 8 - Intel/news-social avec xAI en veto shadow uniquement.
- [ ] Etape 9 - Testnet live sur compte Hyperliquid independant.
- [ ] Etape 10 - Mainnet paper puis canary tiny apres confirmation manuelle.

Derniere mise a jour implementation: `2026-06-07`.

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

Retry recommande pour completer les deux trous de cache v2:

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

### Etape 7 - Dry-run/paper

Ajouter executor dry-run:

- `DryRunExecutionVenue`;
- rapport PnL;
- cout IA net;
- calibration confidence.

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

Les etapes 1 a 6 ajoutent seulement des modules Python, une config, des tests et
un runner local/replay. Elles n'ajoutent aucun service Docker, aucune commande de
deploiement, aucune lecture de secret et aucun chemin d'execution live.

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
