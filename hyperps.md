# Recommandations pour un bot dédié aux Hyperps Hyperliquid

## Objectif

Ce document résume les recommandations opérationnelles pour intégrer les **Hyperps** dans un bot de trading Hyperliquid existant.

L’idée principale est qu’un **Hyperp ne doit pas être traité comme un perp classique**.
Le bot doit appliquer une logique spécifique autour du **funding**, de la **sur-extension du prix**, de la **liquidité** et des événements de listing/conversion.

---

## 1. Principe clé

Sur un perp classique, le prix est généralement ancré à un spot, un index ou un oracle externe.

Sur un **Hyperp**, le sous-jacent peut ne pas encore avoir de marché spot fiable ou d’oracle externe robuste. Le funding est donc davantage lié à une référence interne, souvent une moyenne mobile du mark price.

Conséquence :

```text
Sur Hyperps, le funding devient un indicateur central de sur-extension.
```

Un funding extrême indique souvent que le marché est déjà très déséquilibré dans un sens.

---

## 2. Architecture recommandée

Le bot devrait distinguer explicitement les types de marchés.

```text
PERP_STANDARD
PERP_HIP3_XYZ
HYPERP
SPOT
```

Puis appliquer une logique spéciale si :

```text
market_type = HYPERP
```

Cela évite d’appliquer aveuglément aux Hyperps les mêmes règles que sur BTC, ETH, SOL ou les perps classiques.

---

## 3. Règle n°1 : ne pas chase le momentum

C’est la règle la plus importante.

```text
IF market_type = HYPERP
AND funding extrême
AND prix très éloigné de sa moyenne
THEN bloquer les entrées dans le sens du momentum
```

### Exemple côté long

```text
IF funding très positif
AND close > EMA20/EMA50 de manière excessive
THEN ne pas ouvrir de long
```

### Exemple côté short

```text
IF funding très négatif
AND close < EMA20/EMA50 de manière excessive
THEN ne pas ouvrir de short
```

Le but est d’éviter d’acheter une euphorie déjà trop financée ou de shorter une panique déjà trop extrême.

---

## 4. Règle n°2 : privilégier la mean reversion

Les Hyperps sont probablement plus intéressants comme marchés de **mean reversion contrôlée** que comme marchés de trend-following agressif.

Cela ne veut pas dire shorter automatiquement une hausse ou acheter automatiquement une baisse.
Il faut attendre une confirmation.

### Setup long mean reversion

```text
IF funding très négatif
AND prix sous EMA20/EMA50
AND RSI faible
AND bougie de rejet haussière
THEN autoriser un long mean reversion
```

### Setup short mean reversion

```text
IF funding très positif
AND prix au-dessus EMA20/EMA50
AND RSI élevé
AND bougie de rejet baissière
THEN autoriser un short mean reversion
```

---

## 5. Règle n°3 : utiliser des seuils par coin

Il ne faut pas utiliser les mêmes seuils fixes sur tous les Hyperps.

Chaque marché doit avoir ses propres seuils statistiques, calculés sur son historique.

À calculer par coin :

```text
funding_p90
funding_p95
deviation_p80
deviation_p90
ATR moyen
volume moyen
open interest moyen
spread moyen
```

Exemple de filtre robuste :

```text
abs(funding_8h) > funding_p90_coin
AND abs(close / EMA20 - 1) > deviation_p80_coin
```

Cette approche permet de comparer un Hyperp très volatil avec un autre plus calme sans appliquer des seuils arbitraires.

---

## 6. Règle n°4 : filtrer par liquidité

Un Hyperp peut être très manipulable ou trop peu liquide.

Le bot doit bloquer le trade si :

```text
volume trop faible
open interest trop faible
spread trop large
profondeur orderbook insuffisante
slippage estimé trop élevé
```

La liquidité doit être vérifiée avant même de regarder le signal d’entrée.

Un bon signal technique sur un marché illiquide n’est pas exploitable.

---

## 7. Règle n°5 : limiter fortement le levier

Les Hyperps peuvent avoir :

- des funding spikes ;
- des mèches violentes ;
- une liquidité variable ;
- des mouvements brutaux liés aux annonces ;
- des changements de régime rapides.

Recommandations :

```text
levier faible
taille de position réduite
stop obligatoire
take profit partiel rapide
pas de martingale
pas de renforcement automatique contre tendance
```

Le bot doit survivre aux cas où le marché reste irrationnel plus longtemps que prévu.

---

## 8. Règle n°6 : surveiller les événements de conversion/listing

Un Hyperp peut changer de nature quand le sous-jacent devient réellement tradable ou mieux indexé.

Le bot doit avoir un mode sécurité autour de :

```text
listing spot réel
conversion
changement de specs du marché
annonce Hyperliquid
explosion de volume
explosion d’open interest
modification brutale du funding
```

Dans ces cas, le bot devrait :

```text
désactiver temporairement le trading
ou réduire la taille maximale
ou passer en mode observation
```

L’objectif est d’éviter de trader un marché dont les règles implicites viennent de changer.

---

## 9. Indicateurs prioritaires

| Priorité | Indicateur | Usage |
|---:|---|---|
| 1 | Funding actuel + historique | Détecter les excès de positionnement |
| 2 | Distance à EMA20 / EMA50 | Mesurer la sur-extension du prix |
| 3 | RSI14 / RSI21 | Confirmer surachat ou survente |
| 4 | ATR | Adapter stop-loss et take-profit |
| 5 | Volume + Open Interest | Filtrer la liquidité |
| 6 | Orderbook spread/depth | Éviter slippage et manipulation |
| 7 | Wick / rejection candle | Déclencher l’entrée après confirmation |

---

## 10. Logique bot recommandée

```text
1. Identifier si le marché est un Hyperp
2. Vérifier liquidité + spread + open interest
3. Calculer les percentiles de funding par coin
4. Calculer l’écart prix / EMA20 ou EMA50
5. Bloquer les trades momentum si funding extrême
6. Chercher uniquement un setup mean reversion confirmé
7. Entrer avec une taille réduite
8. Prendre un take profit partiel rapidement
9. Utiliser un stop obligatoire
10. Désactiver ou réduire le risque autour des événements majeurs
```

---

## 11. À éviter absolument

```text
Acheter parce que ça monte
Shorter parce que ça baisse
Utiliser le même signal que BTC/ETH
Ignorer le funding
Ignorer la liquidité
Utiliser un levier élevé
Laisser courir sans stop
Backtester sur seulement quelques jours
Renforcer automatiquement une position perdante
```

---

## 12. Règle synthétique à intégrer

### Hyperp risk filter

```text
HYPERP_RISK_FILTER

IF abs(funding_8h) > funding_p90_coin
AND abs(close / EMA20 - 1) > deviation_p80_coin
THEN
    block_momentum_entries = true
```

### Hyperp mean reversion

```text
HYPERP_MEAN_REVERSION

IF block_momentum_entries = true
AND rejection_candle = true
AND RSI confirms exhaustion
AND liquidity_ok = true
THEN
    allow_contrarian_trade_with_reduced_size
```

---

## 13. Position sizing recommandé

Pour les Hyperps, utiliser une taille réduite par rapport aux perps standards.

Exemple de logique :

```text
base_position_size = position_size_standard_perp * 0.25
```

Puis ajuster selon :

```text
liquidity_score
funding_extremeness
ATR
spread
confidence_score
```

Exemple :

```text
IF funding_extreme = true
AND liquidity_ok = true
AND rejection_confirmed = true
THEN position_size = 25% à 50% de la taille normale
```

---

## 14. Stop-loss et take-profit

### Stop-loss

Utiliser un stop basé sur l’ATR plutôt qu’un pourcentage fixe.

```text
stop_loss = entry_price -/+ 1.5 * ATR
```

Selon la direction :

```text
Long  : stop sous l’entrée
Short : stop au-dessus de l’entrée
```

### Take-profit

Privilégier un take-profit partiel rapide.

Exemple :

```text
TP1 = retour vers EMA20
TP2 = retour vers EMA50 ou niveau de consolidation
```

Sur Hyperps, il vaut mieux sécuriser rapidement une partie du trade plutôt que chercher un mouvement parfait.

---

## 15. Scoring recommandé

Le bot peut construire un score Hyperp dédié.

```text
hyperp_score = 0

+1 si funding extrême
+1 si déviation EMA extrême
+1 si RSI confirme l’excès
+1 si bougie de rejet confirmée
+1 si liquidité suffisante
-1 si spread trop large
-1 si événement marché proche
```

Exemple de décision :

```text
score >= 4 => trade autorisé avec taille réduite
score = 3  => observation seulement
score < 3  => pas de trade
```

---

## 16. Conclusion opérationnelle

Les Hyperps ne doivent pas être vus comme des perps classiques sur lesquels appliquer un trend-following naïf.

La meilleure approche est :

```text
Hyperps = marchés opportunistes de mean reversion contrôlée
```

Le bot doit surtout :

```text
- surveiller le funding ;
- bloquer les entrées momentum quand le funding est extrême ;
- attendre un signal de rejet ;
- filtrer sévèrement la liquidité ;
- réduire la taille des positions ;
- sécuriser rapidement les profits ;
- désactiver ou réduire le risque autour des événements majeurs.
```

La règle finale à retenir :

```text
Ne jamais chase le momentum sur un Hyperp si le funding est extrême.
```
