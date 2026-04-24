Voici un fichier Markdown structuré, propre et réutilisable pour ton bot 👇

---

```markdown
# Hyperliquid BTC/USDC – Analyse de Patterns & Méthodologie Réplicable

## 🎯 Objectif

Identifier des patterns de prédiction de prix sur BTC/USDC via les APIs Hyperliquid, en utilisant :
- plusieurs timeframes
- un large panel d’indicateurs techniques
- des combinaisons de signaux

L’objectif final est de déterminer s’il existe un pattern **fiable voire "infaillible"** et surtout de construire une **méthodologie reproductible** sur d’autres coins.

---

## ⚠️ Conclusion rapide

❌ Aucun pattern infaillible détecté
✅ Existence de patterns **probabilistes exploitables (60–80%)**
⚠️ Les patterns à 100% sont dus à un **nombre trop faible d’occurrences (sur-apprentissage)**

👉 Le meilleur signal identifié est un pattern de **mean reversion après sur-extension**

---

## 📡 Source des données

### API Hyperliquid

Endpoint utilisé :
```

POST [https://api.hyperliquid.xyz/info](https://api.hyperliquid.xyz/info)

````

Payload :
```json
{
  "type": "candleSnapshot",
  "req": {
    "coin": "BTC",
    "interval": "4h",
    "startTime": <timestamp>,
    "endTime": <timestamp>
  }
}
````

### Timeframes analysés

* 1m
* 5m
* 15m
* 1h
* 4h
* 8h
* 1d

### Période

* 30 derniers jours

---

## 🧠 Indicateurs utilisés

### Trend

* EMA20
* EMA50
* EMA100

### Momentum

* RSI14
* RSI21
* MACD (histogram)

### Volatilité

* ATR
* Bollinger Bands

### Price action

* mèches (wicks)
* engulfing
* rejection candles

---

## 🔬 Méthodologie

### 1. Collecte des données

* récupération OHLCV pour chaque timeframe
* stockage en CSV

### 2. Feature engineering

Pour chaque bougie :

* distance au EMA50 :

  ```
  (close - EMA50) / EMA50
  ```
* RSI
* MACD histogram
* ratio mèche haute / corps
* position dans Bollinger Bands

---

### 3. Génération de règles

Création automatique de règles du type :

```
SI condition(s) ALORS prédiction bougie suivante
```

Exemples :

* RSI > 65
* close > EMA50 + X%
* MACD histogram décroissant
* mèche haute importante

---

### 4. Backtest simple

Pour chaque règle :

* calcul du taux de réussite
* nombre d’occurrences
* gain moyen

---

### 5. Validation (anti overfitting)

Split des données :

* 70% train
* 30% test

On ne conserve que les règles :

* stables entre train/test
* avec suffisamment d’occurrences (> 20 idéalement)

---

## 📊 Résultats clés

### ❌ Faux patterns

Patterns à 100% de réussite :

* uniquement 6 à 10 occurrences
* non reproductibles
* non exploitables

---

### ✅ Pattern principal détecté

#### 📉 Mean Reversion après sur-extension

Condition typique :

```
close > EMA50 + 4%
ET RSI21 > 65
```

Résultat :

* ~77% de réussite
* sur timeframe 4h
* ~18 occurrences

---

### 🧠 Interprétation

Quand le prix est :

* trop éloigné de sa moyenne
* en surachat

👉 le marché corrige ou consolide

---

## 🧩 Pattern combiné recommandé

```
RSI élevé (>65)
+ distance EMA50 > 4%
+ MACD histogram en baisse
+ mèche haute visible
```

👉 signal de :

* prise de profit
* short court terme (scalp)

---

## 🚫 Limites

* 30 jours = dataset trop court
* régime de marché biaisé (bullish récent)
* pas de prise en compte du funding / open interest
* pas de microstructure (orderbook)

---

## 🔁 Méthodologie réplicable (autres coins)

### Étapes

1. Changer le coin dans l’API
2. Récupérer 30–90 jours de données
3. Calculer les mêmes indicateurs
4. Générer les règles automatiquement
5. Backtester
6. Filtrer :

   * suffisamment d’occurrences
   * stabilité train/test

---

## 🤖 Recommandations pour un bot

### ❌ À éviter

* prédire chaque bougie
* utiliser un seul indicateur
* croire à un pattern "magique"

### ✅ À faire

* utiliser les patterns comme **filtres**
* combiner plusieurs signaux
* adapter selon le régime de marché

---

## 🚀 Améliorations possibles

### Données supplémentaires

* funding rate
* open interest
* liquidations
* volume delta

### Techniques avancées

* clustering de marché (trend / range)
* machine learning (random forest / xgboost)
* détection de régime

---

## 🧠 Insight clé

👉 Le marché crypto ne récompense pas la prédiction parfaite
👉 Il récompense :

* la gestion du risque
* la sélection des contextes favorables

---

## 📌 TL;DR

* Aucun pattern infaillible
* Un bon pattern = sur-extension → correction
* Utilisable comme filtre, pas comme stratégie standalone

```

---

Si tu veux, je peux maintenant :
- adapter ce MD directement pour ton **pod B Hyperliquid**
- ou le transformer en **spec technique prête à coder (Python bot + règles)**
```
