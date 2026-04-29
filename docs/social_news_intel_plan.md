# TRIDENT - Plan social/news intelligence

Date: `2026-04-29`

Statut: `RESEARCH_SHADOW_FIRST`

Ce document resume le plan d'ajout d'un overlay d'analyse social/news pour TRIDENT. L'objectif n'est pas de creer un nouveau pod de prediction par LLM, mais d'ajouter une couche d'information publique capable de produire des vetoes, reductions de taille ou alertes de regime.

## Contrainte ROI

Reference utilisateur: le bot a genere environ `+700 USD` sur le mois d'avril avec `1000 USD` de capital initial.

Conclusion budget:

| Niveau | Budget mensuel | Lecture ROI |
|---|---:|---|
| Shadow initial | `0-30 USD` | acceptable sans preuve forte |
| Petit live risk overlay | `30-75 USD` | acceptable si le signal evite quelques mauvais trades |
| Version serieuse | `75-150 USD` | seulement apres replay + dry-run positifs |
| Au-dessus | `>150 USD` | a eviter tant que le capital reste proche de `1000 USD` |
| Cher | `~500 USD` | non justifie sauf gain net supplementaire massif et stable |

Un cout de `500 USD/mois` demande au minimum `+500 USD` de PnL additionnel juste pour etre neutre, soit environ `+71%` du PnL mensuel observe. Ce n'est pas un bon point de depart.

## Principe d'architecture

Le LLM ne doit recevoir que des donnees publiques:

- posts/news publics;
- URL, source, date;
- symbole concerne;
- source officielle ou non;
- metriques publiques simples si disponibles;
- resume court de contexte marche public.

Il ne doit pas recevoir:

- positions ouvertes;
- capital;
- PnL interne;
- cles API;
- seuils exacts de strategie;
- routage runtime des pods;
- journaux d'execution.

Sortie attendue:

```json
{
  "global_market_impact": "risk_on|risk_off|neutral",
  "symbol_impacts": [
    {
      "symbol": "SOL",
      "direction": "bullish|bearish|neutral",
      "confidence": 0.0,
      "horizon_minutes": 60,
      "event_type": "official|hack|listing|regulatory|macro|rumor|social_momentum",
      "recommended_action": "observe|veto_new_longs|veto_new_shorts|reduce_size"
    }
  ]
}
```

Au debut, cette sortie doit rester en `shadow`. Ensuite, si les replays sont convaincants, elle peut alimenter uniquement:

- veto d'ouverture;
- reduction de taille;
- augmentation temporaire du seuil de confiance;
- tag d'observabilite dans les journaux.

Pas de trade direct sur signal LLM.

## Stack recommandee

### Phase 1 - cout minimal

Budget cible: `0-20 USD/mois`.

- RSS officiels projets/exchanges.
- CryptoPanic gratuit ou Pro si l'API et les limites suffisent.
- CoinMarketCal gratuit si les evenements a 7 jours suffisent.
- Bluesky/Farcaster publics pour signal faible.
- LLM cheap: `Gemini Flash-Lite`, `DeepSeek V4 Flash`, ou `OpenAI nano/mini`.

Objectif: produire un dataset horodate, sans effet trading.

### Phase 2 - X cible, sans X API direct

Budget cible: `30-80 USD/mois`.

- Ajouter `xAI X Search` sur requetes ciblees.
- Ne pas scanner tout X.
- Requetes par panier: `BTC`, `ETH`, `SOL`, `HYPE`, `DOGE`, `XRP`, `SUI`, plus macro.
- Une requete toutes les `5-15 min`, avec cache et deduplication.

Objectif: detecter les evenements qui n'apparaissent pas encore dans les flux RSS/news.

### Phase 3 - provider crypto specialise

Budget cible: `75-150 USD/mois`.

- CoinMarketCal Standard/Professional si les catalysts planifies se montrent utiles.
- LunarCrush/Santiment seulement si le capital et le PnL augmentent, ou si un test court prouve un edge clair.

Objectif: enrichir les features, pas remplacer la logique prix/microstructure.

## Comparatif fournisseurs

| Fournisseur | Role | Cout typique | Compte/API necessaire | Recommendation |
|---|---|---:|---|---|
| RSS officiels | annonces fiables | `0 USD` | aucun | priorite 1 |
| CryptoPanic | news crypto agregees | `0 USD`, Pro peu cher selon compte | compte + API token | priorite 1 |
| CoinMarketCal | evenements planifies | gratuit, puis environ `50-112 USD/mois` selon plan | compte + API | utile phase 2/3 |
| xAI X Search | recherche ciblee sur X | `5 USD / 1000 appels` + tokens Grok | compte xAI + credits API | meilleur acces X low-cost |
| X API direct | posts X bruts | `0.005 USD/post lu` | compte developpeur X + credits | a eviter au debut |
| Gemini API | LLM + Google Search grounding | tres faible a modere | Google AI Studio / GCP billing | excellent ROI |
| DeepSeek API | LLM cheap JSON | tres faible | compte DeepSeek + top-up | excellent classifier cheap |
| OpenAI API | LLM stable JSON | faible a modere | compte API + billing | bon fallback |
| Tavily | web search API | `1000` credits gratuits, puis `30 USD+` | compte Tavily | bon web search simple |
| Perplexity API | search + answer citee | environ `5-12 USD / 1000 requetes` + tokens | compte Perplexity API | bon pour news/web |
| Exa | search semantique | `5 USD / 1000` recherches simples | compte Exa | bon fallback search |
| Reddit API | sentiment retail | gratuit bas volume, commercial opaque | app Reddit dev | shadow uniquement |
| Bluesky API | social ouvert | gratuit avec limites | compte/app selon usage | complement gratuit |
| Telegram/Discord | annonces de communautes | souvent gratuit | bot/app + acces canaux | utile si sources autorisees |
| LunarCrush | social crypto specialise | souvent `70 USD+` | abonnement/API | plus tard |
| Santiment | social/on-chain/dev | payant, pricing variable | SanAPI | plus tard |
| NewsAPI | news generaliste | `449 USD/mois` prod | compte NewsAPI | trop cher pour maintenant |

## Combinaisons utiles

| Combinaison | Cout estime | Usage |
|---|---:|---|
| RSS + CryptoPanic + Gemini/DeepSeek | `0-20 USD/mois` | MVP shadow |
| RSS + CryptoPanic + CoinMarketCal + Gemini | `50-75 USD/mois` | catalysts crypto serieux |
| RSS + xAI X Search cible + DeepSeek/Gemini | `30-80 USD/mois` | X sans cout massif |
| Tavily ou Perplexity + Gemini | `20-80 USD/mois` | web/news generaliste |
| X API direct + LLM | `150-900+ USD/mois` | seulement apres preuve |

La meilleure combinaison initiale est:

1. RSS officiels;
2. CryptoPanic;
3. CoinMarketCal gratuit;
4. Gemini Flash-Lite ou DeepSeek V4 Flash;
5. xAI X Search cible uniquement si la phase 1 montre un signal utile.

## Parametrage commun TRIDENT

Variables d'environnement proposees:

```bash
TRIDENT_SOCIAL_INTEL_ENABLED=false
TRIDENT_SOCIAL_INTEL_MODE=shadow
TRIDENT_SOCIAL_INTEL_OUTPUT_DIR=./data/social_intel

OPENAI_API_KEY=
GEMINI_API_KEY=
DEEPSEEK_API_KEY=
XAI_API_KEY=
TAVILY_API_KEY=
PERPLEXITY_API_KEY=
EXA_API_KEY=
CRYPTOPANIC_API_KEY=
COINMARKETCAL_API_KEY=
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
TELEGRAM_BOT_TOKEN=
DISCORD_BOT_TOKEN=
```

Section config future possible:

```toml
[social_intel]
enabled = false
mode = "shadow"
interval_seconds = 900
ttl_minutes = 90
max_monthly_budget_usd = 75
provider_timeout_seconds = 15
output_dir = "./data/social_intel"

[social_intel.sources]
rss_enabled = true
cryptopanic_enabled = true
coinmarketcal_enabled = true
xai_x_search_enabled = false
x_api_direct_enabled = false
reddit_enabled = false
bluesky_enabled = true
telegram_enabled = false
discord_enabled = false

[social_intel.llm]
provider = "gemini"
model = "gemini-2.5-flash-lite"
fallback_provider = "deepseek"
fallback_model = "deepseek-v4-flash"
```

## Runbook fournisseurs

### OpenAI API

Role recommande: classifier fiable ou fallback JSON.

Creation compte:

1. Aller sur `https://platform.openai.com/`.
2. Creer ou utiliser un compte OpenAI.
3. Ouvrir `API keys`.
4. Creer une cle projet.
5. Activer la facturation et definir une limite mensuelle.

Parametrage:

- variable: `OPENAI_API_KEY`;
- budget initial: `10-25 USD/mois`;
- modele recommande: modele mini/nano disponible le moins cher respectant bien le JSON;
- ne pas utiliser ChatGPT Pro comme API: l'abonnement ChatGPT et l'API sont separes.

Captures a ajouter si besoin:

- page `API keys`;
- page `Billing / limits`;
- page `Usage`.

### Gemini API

Role recommande: meilleur ROI pour classification + web grounding.

Creation compte:

1. Aller sur `https://aistudio.google.com/`.
2. Creer une cle API dans Google AI Studio.
3. Si usage production, passer le projet en plan paid.
4. Ajouter une limite de budget dans Google Cloud Billing.

Parametrage:

- variable: `GEMINI_API_KEY`;
- modele recommande: `gemini-2.5-flash-lite` ou equivalent Flash-Lite courant;
- activer Google Search grounding uniquement quand une recherche web est necessaire;
- eviter de grounder chaque cycle si RSS/news suffisent.

Captures a ajouter:

- generation de cle API;
- budget alert GCP;
- dashboard usage Gemini.

### DeepSeek API

Role recommande: classifier tres cheap en sortie JSON.

Creation compte:

1. Aller sur `https://platform.deepseek.com/`.
2. Creer un compte.
3. Ajouter du credit/top-up.
4. Creer une API key.

Parametrage:

- variable: `DEEPSEEK_API_KEY`;
- modele recommande: `deepseek-v4-flash`;
- utiliser le mode non-thinking pour classification simple;
- verifier strictement le JSON cote client, avec fallback si schema invalide.

Captures a ajouter:

- API keys;
- balance/top-up;
- usage.

### xAI API et X Search

Role recommande: recherche ciblee sur X sans payer la X API post par post.

Creation compte:

1. Aller sur `https://console.x.ai/`.
2. Creer un compte xAI.
3. Creer une team si necessaire.
4. Ajouter des credits prepayes.
5. Creer une cle API.

Parametrage:

- variable: `XAI_API_KEY`;
- outil recommande: `x_search`;
- cout outil verifie: `5 USD / 1000 appels`;
- limiter les requetes a des paniers precis;
- dedupliquer les resultats par URL/post id.

Exemple de requetes:

- `("SOL" OR "Solana") (hack OR exploit OR outage OR listing)`;
- `("HYPE" OR "Hyperliquid") (incident OR listing OR exploit OR outage)`;
- `("BTC" OR "Bitcoin") (ETF OR Fed OR regulation OR liquidation)`.

Captures a ajouter:

- API key;
- billing credits;
- usage explorer.

### X API direct

Role recommande: uniquement si besoin de posts bruts backtestables.

Creation compte:

1. Aller sur `https://developer.x.com/`.
2. Creer un compte developpeur.
3. Creer un projet/app.
4. Acheter des credits X API.
5. Generer bearer token / OAuth selon endpoint.

Parametrage:

- variable: `X_BEARER_TOKEN`;
- cout actuel: `0.005 USD` par post lu;
- definir un plafond strict de posts par cycle;
- ne pas activer par defaut dans TRIDENT.

Exemple budget:

| Posts lus/cycle | Cycle 15 min | Cycle 5 min |
|---:|---:|---:|
| 5 | `~72 USD/mois` | `~216 USD/mois` |
| 10 | `~144 USD/mois` | `~432 USD/mois` |
| 20 | `~288 USD/mois` | `~864 USD/mois` |

Conclusion: trop cher au depart.

Captures a ajouter:

- app developer;
- page credits;
- usage posts.

### Tavily

Role recommande: web search simple et controle.

Creation compte:

1. Aller sur `https://app.tavily.com/`.
2. Creer un compte.
3. Recuperer une API key.
4. Rester en free tier au debut si possible.
5. Passer au plan Project seulement si le free tier est insuffisant.

Parametrage:

- variable: `TAVILY_API_KEY`;
- commencer en `basic search`;
- utiliser `advanced` seulement pour incidents importants;
- budget initial: `0-30 USD/mois`.

Captures a ajouter:

- API key;
- credits usage;
- plan billing.

### Perplexity API

Role recommande: search + synthese citee.

Creation compte:

1. Aller sur `https://docs.perplexity.ai/`.
2. Creer un compte API.
3. Creer une API key.
4. Ajouter le billing / credits selon console.

Parametrage:

- variable: `PERPLEXITY_API_KEY`;
- preferer Search API ou Sonar low context;
- eviter Sonar Deep Research en boucle automatique;
- budget initial: `15-50 USD/mois`.

Captures a ajouter:

- API key;
- usage;
- modele/search context choisi.

### Exa

Role recommande: recherche semantique web, alternative Tavily/Perplexity.

Creation compte:

1. Aller sur `https://dashboard.exa.ai/`.
2. Creer un compte.
3. Recuperer une API key.
4. Commencer avec les credits gratuits.

Parametrage:

- variable: `EXA_API_KEY`;
- utiliser search simple `1-25` resultats;
- eviter `26-100` resultats en boucle car plus cher;
- budget initial: `0-30 USD/mois`.

Captures a ajouter:

- API key;
- credit balance;
- usage.

### CryptoPanic

Role recommande: flux news crypto et sentiment communautaire.

Creation compte:

1. Aller sur `https://cryptopanic.com/`.
2. Creer un compte.
3. Ouvrir la section API/developer si disponible sur le compte.
4. Generer ou recuperer l'auth token.
5. Evaluer Pro seulement si les limites gratuites bloquent le shadow.

Parametrage:

- variable: `CRYPTOPANIC_API_KEY`;
- filtrer par currencies de l'univers observe;
- stocker `published_at`, `domain`, `title`, `url`, votes/sentiment si disponibles;
- dedupliquer par URL.

Captures a ajouter:

- page token/API;
- filtres currencies;
- feed avec votes/sentiment.

### CoinMarketCal

Role recommande: catalysts planifies.

Creation compte:

1. Aller sur `https://coinmarketcal.com/api`.
2. Creer un compte.
3. Choisir Personal pour test ou Standard/Professional si besoin commercial.
4. Recuperer la cle API.

Parametrage:

- variable: `COINMARKETCAL_API_KEY`;
- commencer par les events `upcoming`;
- mapper les coins aux symbols Hyperliquid;
- tagger les events par categorie: listing, unlock, vote, fork, conference, release.

Captures a ajouter:

- page plan;
- API key;
- exemple d'event avec source originale.

### Reddit

Role recommande: sentiment retail bas volume, shadow uniquement.

Creation compte:

1. Aller sur `https://www.reddit.com/prefs/apps`.
2. Creer une app `script` ou `web app` selon client.
3. Recuperer `client_id` et `client_secret`.
4. Utiliser OAuth officiel.

Parametrage:

- variables: `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT`;
- limiter les subreddits;
- ne pas scraper agressivement;
- respecter les conditions Reddit pour usage commercial.

Subreddits possibles:

- `r/CryptoCurrency`;
- `r/Bitcoin`;
- `r/ethereum`;
- subreddits projet uniquement si pertinents.

Captures a ajouter:

- app Reddit;
- rate limit headers dans logs;
- liste des subreddits suivis.

### Bluesky

Role recommande: social public gratuit en complement.

Creation compte:

1. Creer un compte Bluesky si besoin d'auth.
2. Utiliser les endpoints publics AppView quand possible.
3. Lire les rate limits exposes par headers.

Parametrage:

- pas forcement de variable si endpoint public;
- variable optionnelle: `BLUESKY_APP_PASSWORD`;
- requetes ciblees par mots-cles et handles officiels;
- faible priorite par rapport a X/CryptoPanic.

Captures a ajouter:

- app password si utilisee;
- exemple de recherche publique;
- headers rate limit.

### Telegram

Role recommande: annonces officielles de projets ou canaux autorises.

Creation compte:

1. Creer un bot via `@BotFather`.
2. Recuperer le token.
3. Ajouter le bot uniquement dans des canaux/groupes autorises.
4. Verifier que les conditions du canal permettent l'usage.

Parametrage:

- variable: `TELEGRAM_BOT_TOKEN`;
- collecter seulement les messages de canaux suivis;
- conserver source, date, lien message si disponible;
- eviter les groupes bruyants non officiels.

Captures a ajouter:

- BotFather token masque;
- canal autorise;
- exemple de message collecte.

### Discord

Role recommande: annonces officielles de projets si le serveur l'autorise.

Creation compte:

1. Aller sur `https://discord.com/developers/applications`.
2. Creer une application.
3. Creer un bot.
4. Ajouter le bot au serveur/canal autorise.
5. Activer les intents necessaires seulement si indispensables.

Parametrage:

- variable: `DISCORD_BOT_TOKEN`;
- limiter aux canaux announcement;
- ne pas lire les salons prives/non autorises;
- dedupliquer par message id.

Captures a ajouter:

- application Discord;
- bot permissions;
- canal surveille.

### LunarCrush

Role recommande: social crypto specialise, plus tard.

Creation compte:

1. Aller sur `https://lunarcrush.com/`.
2. Creer un compte.
3. Choisir un plan donnant acces API si necessaire.
4. Creer une cle API.

Parametrage:

- variable: `LUNARCRUSH_API_KEY`;
- ne pas activer avant d'avoir prouve la valeur d'un overlay social moins cher;
- utiliser les scores agreges plutot que les posts bruts.

Decision: a eviter tant que le budget social total doit rester sous `75 USD/mois`.

### Santiment

Role recommande: donnees social/on-chain/dev avancees, plus tard.

Creation compte:

1. Aller sur `https://santiment.net/` ou `https://api.santiment.net/`.
2. Creer un compte.
3. Demander ou activer SanAPI.
4. Recuperer la cle API.

Parametrage:

- variable: `SANTIMENT_API_KEY`;
- tester d'abord manuellement sur quelques assets;
- ne pas brancher en live tant que les signaux gratuits n'ont pas prouve leur valeur.

Decision: plutot phase capital superieur.

### NewsAPI

Role recommande: non recommande pour TRIDENT au capital actuel.

Creation compte:

1. Aller sur `https://newsapi.org/`.
2. Creer un compte si test.
3. Utiliser le plan Developer uniquement en developpement/test.
4. Production: plan Business requis.

Parametrage:

- variable: `NEWSAPI_KEY`;
- plan Business autour de `449 USD/mois`;
- trop cher pour le ROI actuel.

Decision: ne pas utiliser.

### LLM local

Role recommande: fallback classification sans cout API.

Creation:

1. Installer Ollama, llama.cpp ou vLLM sur une machine locale.
2. Choisir un modele instruct capable de respecter un schema JSON.
3. Exposer une API locale compatible OpenAI si possible.

Parametrage:

- variable: `LOCAL_LLM_BASE_URL`;
- modele possible: petit modele instruct quantifie;
- utiliser seulement pour classifier du texte deja collecte;
- ne resout pas l'acces aux donnees X/news.

Decision: bon fallback, mais pas prioritaire si Gemini/DeepSeek coutent quelques dollars.

## Captures d'ecran

Les captures d'ecran des consoles ne sont pas incluses dans ce commit car elles demandent une session authentifiee et des secrets visibles. Quand les comptes seront crees, ajouter les images masquees dans:

```text
docs/assets/social_news_intel/
```

Convention proposee:

```text
docs/assets/social_news_intel/openai_api_key.png
docs/assets/social_news_intel/gemini_billing_budget.png
docs/assets/social_news_intel/xai_usage_explorer.png
docs/assets/social_news_intel/tavily_credits.png
docs/assets/social_news_intel/cryptopanic_token.png
docs/assets/social_news_intel/coinmarketcal_api_key.png
```

Regle: masquer toutes les cles, emails, noms de compte, IDs de projet sensibles et infos de paiement.

## Validation avant activation live

Checklist:

- collecter au moins `2-4 semaines` en shadow;
- enregistrer chaque item source + score LLM + action recommandee;
- rejouer avril avec une simulation d'overlay sans lookahead;
- mesurer:
  - PnL net;
  - max drawdown;
  - nombre de trades vetoes;
  - trades gagnants vetoes a tort;
  - trades perdants evites;
  - latence entre news et mouvement prix;
- promouvoir uniquement si l'overlay ameliore le PnL net ou reduit fortement le risque.

Seuils de promotion proposes:

| Cout mensuel | Amelioration minimale attendue |
|---:|---:|
| `25 USD` | `+50 USD/mois` ou drawdown reduit clairement |
| `75 USD` | `+150 USD/mois` |
| `150 USD` | `+300 USD/mois` |
| `500 USD` | `+1000 USD/mois` minimum |

## Sources de prix et docs

- X API pricing: `https://docs.x.com/x-api/getting-started/pricing`
- xAI models/tools pricing: `https://docs.x.ai/developers/models`
- xAI billing: `https://docs.x.ai/console/billing`
- Gemini pricing: `https://ai.google.dev/gemini-api/docs/pricing`
- DeepSeek pricing: `https://api-docs.deepseek.com/quick_start/pricing`
- OpenAI pricing: `https://developers.openai.com/api/docs/pricing`
- Perplexity pricing: `https://docs.perplexity.ai/docs/getting-started/pricing`
- Tavily credits: `https://docs.tavily.com/documentation/api-credits`
- Exa pricing: `https://exa.sh/pricing`
- NewsAPI pricing: `https://newsapi.org/pricing`
- CoinMarketCal API: `https://coinmarketcal.com/api`
- CryptoPanic: `https://cryptopanic.com/`
- Bluesky rate limits: `https://docs.bsky.app/docs/advanced-guides/rate-limits`
