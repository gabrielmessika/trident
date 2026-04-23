# Hyperliquid Meme Coin Bot — Notes de préparation à l’implémentation

## Objectif

Ce document synthétise les idées discutées pour préparer l’implémentation ou l’évaluation d’un bot orienté meme coins sur Hyperliquid (HL), avec :

- les patterns à considérer,
- l’architecture de sélection des coins,
- l’architecture d’exécution sur HL,
- les recommandations concrètes,
- la provenance des idées,
- les limites,
- les points à vérifier avant implémentation.

---

## 1. Constats structurants

### 1.1 Un bon bot meme coin ne doit généralement pas se limiter à une simple liste fixe
Une simple watchlist fixe est plus simple à backtester et plus stable, mais elle rate souvent :
- les rotations rapides de narrative,
- les coins qui “se réveillent” soudainement,
- les nouveaux actifs qui captent brutalement l’attention.

### 1.2 Un scan permanent de tout HL ne doit pas conduire à trader tout HL
Scanner tout l’univers est utile pour détecter :
- nouveaux candidats,
- hausse anormale de volume,
- changement de régime de volatilité,
- changement brutal de profondeur,
- activité inhabituelle.

En revanche, l’exécution doit rester sélective.

### 1.3 Le meilleur compromis est un modèle hybride
Architecture recommandée :
- **Tier 1** : watchlist active permanente, tradable
- **Tier 2** : actifs sous observation, promotion possible
- **Tier 3** : univers complet scanné, sans exécution directe ou avec garde-fous très stricts

---

## 2. Données et capacités HL à utiliser

### 2.1 WebSocket principal
Endpoint principal :
- `wss://api.hyperliquid.xyz/ws`

### 2.2 Subscriptions marché importantes
- `l2Book` : carnet d’ordres
- `trades` : flux des exécutions
- `allMids` : mids sur plusieurs actifs
- `candle` : agrégation OHLC

### 2.3 Subscriptions compte / exécution
- `orderUpdates`
- `userEvents`
- `userFills`
- `openOrders`
- `clearinghouseState`

### 2.4 Info endpoint
À utiliser pour :
- metadata marché,
- univers perp/spot,
- état des actifs,
- historique et compléments d’information.

### 2.5 Contraintes techniques HL à intégrer
Les limites de connexions et de subscriptions doivent être prises en compte dans le design :
- nombre max de connexions websocket,
- nombre max de subscriptions,
- limites sur les messages.

### 2.6 Optimisation latence
Pour une stack très sensible à la latence, HL documente aussi :
- l’usage d’un nœud non validant,
- le désactivage du buffering de sortie,
- une reconstruction locale plus directe de l’état du marché.

---

## 3. Comment un bot meme coin passe ses trades sur HL

### 3.1 Principe général
Le bot :
1. observe le marché via WebSocket,
2. reconstruit l’état local,
3. prend une décision,
4. construit un ordre localement,
5. signe l’ordre,
6. l’envoie à HL,
7. suit les retours de fills et d’updates.

### 3.2 Types d’ordres à prévoir
- **limit**
- **aggressive limit / limit crossing** pour les entrées rapides
- **post-only** quand la logique vise à réduire le coût d’exécution

### 3.3 Boucle d’exécution minimale
- écouter `l2Book`, `trades`, `allMids`
- calculer l’état local
- détecter un signal
- choisir le type d’ordre
- envoyer l’ordre signé
- écouter `orderUpdates` / `userFills`
- gérer timeout, annulation, reprice, sortie

### 3.4 Risque d’exécution
Sur meme coins, la qualité du fill est souvent aussi importante que le signal :
- spread,
- profondeur,
- slippage,
- stabilité du carnet,
- latence interne,
- capacité à annuler/replacer proprement.

---

## 4. Recommandations d’univers tradable

### 4.1 Modèle recommandé
Le bot doit :

1. **scanner large** sur HL,
2. **classer** les actifs,
3. **ne trader qu’un sous-ensemble actif**.

### 4.2 Proposition de structure
#### Tier 1 — Active tradable list
Actifs surveillés et tradables en continu.
Critères possibles :
- bonne liquidité relative,
- spread acceptable,
- profondeur suffisante,
- comportement exploitable,
- bonne qualité de signal historique.

#### Tier 2 — Watch / Promotion candidates
Actifs surveillés mais non tradés systématiquement.
Promotion si :
- accélération de volume,
- hausse de volatilité exploitable,
- amélioration de la profondeur,
- score global en forte hausse.

#### Tier 3 — Scan universe
Tous les actifs HL suivis de façon légère :
- détection d’émergents,
- anomalies,
- changements de régime,
- nouveaux candidats.

### 4.3 Recommandation opérationnelle
Le scanner global ne doit pas être couplé à un trading immédiat sur tous les actifs.
Il doit servir principalement à :
- alimenter le ranking,
- promouvoir/déclasser des actifs,
- ajuster dynamiquement la watchlist active.

---

## 5. Patterns intéressants pour meme coins

## 5.1 Event-driven momentum
Idée :
- détecter un changement de régime très tôt,
- confirmer rapidement,
- exécuter sur mouvement naissant ou sur reprise.

Déclencheurs typiques :
- explosion de volume,
- augmentation brutale de la cadence des trades,
- accélération du mid,
- changement brutal de profondeur,
- changement de narrative ou d’attention.

### 5.2 Breakout narratif confirmé
Combinaison utile :
- hausse du score d’attention / trending / social,
- hausse du volume,
- buy pressure / delta positif,
- cassure technique ou reclaim.

### 5.3 Pullback après emballement initial
Plutôt que d’entrer au premier spike :
- attendre une impulsion claire,
- laisser respirer,
- vérifier maintien du volume / pression,
- entrer sur reprise ou reclaim propre.

### 5.4 Flow-following
Observer moins le prix “brut” que le flux :
- pression acheteuse,
- séquences de trades agressifs,
- déséquilibre carnet,
- présence d’acheteurs persistants,
- comportement de cohortes fortes / whales quand les données sont disponibles.

### 5.5 Creator / wallet / cohort tracking
Idée plus exploratoire :
- ne pas seulement scorer les coins,
- scorer les wallets / clusters / cohortes / créateurs qui déclenchent souvent des séquences d’attention.

### 5.6 Volatility harvesting / fee-first logic
Pour certains univers meme, l’idée n’est pas forcément de mieux prévoir la direction, mais d’exploiter :
- forte volatilité,
- rotation de flux,
- comportement répétitif,
- capture de fees / monétisation de flux,
si la stack et la structure de marché s’y prêtent.

---

## 6. Signaux microstructure à implémenter

### 6.1 Order Book Imbalance
Mesurer :
- volume bid vs ask,
- pondération par distance au mid,
- variation temporelle de l’imbalance.

### 6.2 Trade Flow Imbalance
Mesurer :
- dominance buy/sell,
- rafales de trades agressifs,
- volume exécuté dans un sens,
- persistance du flux.

### 6.3 Spread Dynamics
Mesurer :
- widening / tightening,
- spread relatif à la volatilité locale,
- stabilité ou dégradation avant exécution.

### 6.4 Depth / Liquidity Dynamics
Mesurer :
- profondeur proche du prix,
- apparition/disparition rapide de murs,
- drainage de liquidité,
- réapparition de liquidité.

### 6.5 Cancel / Replace Activity
Mesurer :
- churn du carnet,
- fréquence de mise à jour,
- stabilité réelle des niveaux visibles.

### 6.6 Micro-volatility
Mesurer :
- vitesse du mid,
- accélération,
- clustering de micro-mouvements.

### 6.7 Absorption / Exhaustion
Mesurer :
- exécutions significatives sans déplacement de prix,
- perte de momentum malgré pression persistante.

---

## 7. Architecture bot recommandée

### 7.1 Scanner global
Mission :
- surveiller tout HL,
- mettre à jour un score multi-facteurs,
- détecter émergents et changements de régime.

Sortie :
- shortlist active,
- promotions / dégradations,
- signaux de revue.

### 7.2 Market State Engine
Mission :
- reconstruire localement l’état du marché,
- maintenir best bid/ask, spread, profondeur, mid, historique court,
- produire les features microstructure.

### 7.3 Signal Engine
Mission :
- calculer les patterns d’entrée/sortie,
- combiner microstructure + momentum + ranking univers.

### 7.4 Execution Engine
Mission :
- choisir type d’ordre,
- contrôler le slippage,
- gérer les timeouts,
- gérer l’annulation/replacement,
- tenir compte de la qualité du carnet.

### 7.5 Portfolio / Risk Layer
Mission :
- limiter le nombre de positions,
- imposer une taille max par coin,
- gérer conflits entre pods ou stratégies,
- empêcher plusieurs trades simultanés incohérents sur le même coin.

---

## 8. Recommandations concrètes

### 8.1 Commencer avec un univers réduit mais dynamique
Préconisation :
- scanner tout,
- mais ne trader que le top N.

Exemple :
- top 5 à top 15 selon capacité de traitement et qualité d’exécution.

### 8.2 Séparer “détection d’intérêt” et “décision de trade”
Le scanner n’est pas le moteur de trade.
Il doit produire :
- un score,
- une shortlist,
- des drapeaux de contexte.

### 8.3 Ne pas baser la logique uniquement sur des indicateurs classiques
Sur meme coins, les patterns les plus intéressants semblent venir davantage de :
- l’événementiel,
- le flux,
- la rapidité de détection,
- les filtres de qualité,
plutôt que d’un simple RSI/MACD isolé.

### 8.4 Noter la qualité d’exécution par coin
Chaque coin doit avoir un profil d’exécution :
- spread moyen,
- profondeur utile,
- slippage moyen,
- fiabilité des fills,
- fréquence de faux mouvements.

### 8.5 Utiliser un ranking dynamique
Exemple de facteurs de score :
- volume récent,
- accélération du volume,
- spread,
- profondeur,
- stabilité du carnet,
- volatilité exploitable,
- qualité historique des signaux,
- coût implicite d’exécution.

### 8.6 Traiter différemment leaders et émergents
#### Leaders établis
Exemples de logique :
- microstructure,
- breakout/pullback,
- flow-following.

#### Émergents / réveils
Exemples de logique :
- filtre plus strict,
- taille plus faible,
- nombre d’essais limité,
- promotion si confirmation durable.

---

## 9. Points importants à vérifier avant implémentation

### 9.1 Couverture fonctionnelle HL
Vérifier :
- support correct des subscriptions utilisées,
- gestion correcte des snapshots,
- mapping perp/spot,
- gestion robuste des erreurs,
- limites WS bien intégrées,
- logique d’auth/signature testée.

### 9.2 Robustesse data
Vérifier :
- absence de trous de flux,
- désynchronisation carnet,
- ordre de traitement des messages,
- horodatage cohérent,
- replay possible.

### 9.3 Coûts et qualité d’exécution
Vérifier :
- impact réel du spread,
- slippage réel sur tailles visées,
- comportement pendant spikes,
- taux de fills partiels,
- taux d’annulation/replacement.

### 9.4 Contraintes de portefeuille
Vérifier :
- exposition max par coin,
- exposition max par bucket meme,
- interaction avec autres pods,
- règle “1 trade par coin à la fois” si elle existe dans l’architecture cible.

### 9.5 Validation quantitative
Vérifier :
- performance par régime de marché,
- performance hors échantillon,
- taux de faux signaux,
- dégradation en conditions de forte agitation,
- robustesse après coûts complets.

### 9.6 Logging et post-mortem
Vérifier :
- logs de décision,
- logs d’exécution,
- stockage des features,
- capacité à reconstruire un trade de bout en bout.

---

## 10. Limites

### 10.1 Les repos publics ne donnent pas une recette miracle
Ils sont utiles pour :
- repérer les patterns récurrents,
- identifier les architectures intéressantes,
- voir quelles idées reviennent souvent.

Ils ne prouvent pas à eux seuls la robustesse d’une stratégie.

### 10.2 Beaucoup d’idées publiques sont déjà “connues”
Les idées comme :
- sniping,
- whale tracking,
- social tracking,
- momentum événementiel,
sont souvent déjà exploitées.

L’edge réel vient souvent de :
- la qualité du filtre,
- la rapidité d’exécution,
- le ranking,
- le risk control,
- la discipline de sélection.

### 10.3 La direction est parfois moins stable que la volatilité
Sur meme coins, certains environnements se prêtent mieux à des logiques de capture de flux / volatilité qu’à une vraie prédiction directionnelle robuste.

### 10.4 Le bot peut être limité par l’exécution plus que par le signal
Un bon signal sur un coin mal exécutable peut devenir un mauvais trade.

---

## 11. Sources et provenance des idées

### 11.1 Documentation officielle Hyperliquid
Utilisée pour :
- WebSocket subscriptions,
- endpoint WS,
- snapshots,
- info endpoint,
- limites,
- optimisations de latence,
- exchange endpoint.

Sources :
- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions
- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits
- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/optimizing-latency
- https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint

### 11.2 Repos publics utilisés comme inspiration / signal faible
#### Chainstack pump.fun / letsbonk bot
Apporte des idées sur :
- architecture listener,
- détection événementielle,
- nouveaux tokens / migrations,
- trading/snipe orienté vitesse.

Source :
- https://github.com/chainstacklabs/pumpfun-bonkfun-bot

#### Hyperliquid Data Layer API
Apporte des idées sur :
- whale positions,
- buyer tracking,
- smart money ranking,
- cumulative delta / order flow,
- vues orientées flux et positionnement.

Sources :
- https://github.com/moondevonyt/Hyperliquid-Data-Layer-API
- https://github.com/moondevonyt/Hyperliquid-Data-Layer-API/blob/main/examples/README.md

#### Meteora Meme Pool Fee Harvest Strategy
Apporte des idées sur :
- logique non purement directionnelle,
- token screening,
- matching volatilité / frais,
- contrôles de risque,
- shortlist dynamique.

Source :
- https://github.com/createMonster/meteora_meme/blob/main/README.md

### 11.3 Nature des recommandations
Les préconisations de ce document viennent de trois niveaux :
1. **capacités officielles HL**,
2. **motifs récurrents vus dans des repos publics**,
3. **synthèse d’architecture et de priorisation** pour un bot meme coin sur HL.

---

## 12. Préconisations finales

### Recommandation A
Mettre en place un **scanner universel léger** sur tout HL.

### Recommandation B
Maintenir une **watchlist active dynamique** plutôt qu’une liste fixe figée.

### Recommandation C
Construire un **ranking multi-facteurs** avant toute décision d’exécution.

### Recommandation D
Séparer clairement :
- détection,
- qualification,
- décision,
- exécution,
- suivi.

### Recommandation E
Prioriser l’implémentation de :
1. scanner univers,
2. market state engine,
3. signaux microstructure de base,
4. exécution robuste,
5. logging et replay,
6. promotion/déclassement dynamique des coins.

### Recommandation F
Avant toute mise en production, valider séparément :
- le scan univers,
- la logique de ranking,
- la qualité d’exécution,
- la robustesse aux trous de données,
- la compatibilité avec les autres stratégies/pods.

---

## 13. Checklist courte d’implémentation

### Univers
- [ ] scanner global HL
- [ ] ranking multi-facteurs
- [ ] tiers 1 / 2 / 3
- [ ] promotion/déclassement dynamique

### Data
- [ ] `l2Book`
- [ ] `trades`
- [ ] `allMids`
- [ ] snapshots gérés
- [ ] re-sync possible

### Features
- [ ] spread
- [ ] profondeur
- [ ] order book imbalance
- [ ] trade flow imbalance
- [ ] micro-volatility
- [ ] churn du carnet
- [ ] absorption / exhaustion

### Exécution
- [ ] ordres signés HL
- [ ] suivi fills et updates
- [ ] timeout / cancel / replace
- [ ] slippage control
- [ ] profil d’exécution par coin

### Risque
- [ ] taille max par coin
- [ ] limite par bucket
- [ ] compatibilité multi-pods
- [ ] garde-fou sur actifs émergents

### Validation
- [ ] backtest
- [ ] replay
- [ ] paper trade
- [ ] logs complets
- [ ] revue post-trade
