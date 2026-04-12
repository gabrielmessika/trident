# TRIDENT — Spécification détaillée du nouveau Pod B

Document historique: le remplacement de l'ancien Pod B maker par le Pod B directionnel breakout a maintenant ete implemente. Ce fichier reste utile pour retracer l'intention initiale, mais la source de verite courante est le code du repo et la documentation principale.

## 1. Objectif

Ce document décrit **tout ce qu'il faut pour implémenter un nouveau `Pod B`** dans TRIDENT, en remplacement du `Pod B` actuel basé sur Avellaneda-Stoikov / market making.

Le nouveau `Pod B` doit être un **moteur directionnel opportuniste crypto**, spécialisé dans les **phases de compression suivies de rupture**, les **expansions brutales de volatilité**, et les **mouvements rapides mais encore jeunes**.

Il ne doit **pas** être un doublon de `Pod A`.
Il doit cohabiter avec les autres pods dans le cadre architectural existant de TRIDENT :

- **ownership exclusif par symbole** ;
- **allocation et routing par le supervisor** ;
- distinction entre **`opening_symbols`** et **`managed_symbols`** ;
- exécution via le pipeline partagé **signal → trade plan → risk → executor** ;
- journalisation, reporting, replay et dry-run alignés avec l’existant.

---

## 2. Résumé exécutif

### Positionnement du nouveau Pod B

Le nouveau `Pod B` est un pod **crypto-only**, orienté **breakout / squeeze / volatility expansion**.

Il intervient sur des setups du type :

- période de compression claire ;
- contraction de range et de volatilité ;
- accélération brutale de volume / trades / déséquilibre de carnet ;
- cassure confirmée du range ;
- potentiel de continuation court terme.

### Rôle relatif aux autres pods

- **Pod A** : tendance installée / continuation plus propre / swing-directionnel principal
- **Pod B** : impulsion émergente / breakout précoce / mouvement explosif court terme
- **Pod C** : opportunités opportunistes non-crypto ou spécifiques TradFi / event-driven

### Principe de non-confrontation

Le nouveau `Pod B` ne doit pas être actif sur les mêmes configurations de marché que `Pod A`.

Il doit être sélectionné principalement quand le supervisor détecte :

- un **régime nerveux**, ou
- un **régime de sortie de compression**, ou
- un **régime d’accélération naissante**,

là où `Pod A` sera réservé aux tendances plus établies.

---

## 3. Contraintes d’architecture TRIDENT à respecter

Le design doit respecter les principes déjà en place dans TRIDENT :

1. **Un seul pod par symbole à la fois**
   - Aucun trade ne doit être pris si le symbole n’est pas routé au `Pod B`.
   - `Pod B` ne doit jamais ignorer l’ownership registry.

2. **Séparation ouverture / gestion**
   - `opening_symbols` : symboles sur lesquels le pod peut ouvrir un nouveau trade.
   - `managed_symbols` : symboles que le pod peut encore gérer et déboucler proprement.

3. **Pas de disparition brutale en cas de retrait de routing**
   - Si le supervisor retire le symbole du scope d’ouverture, le pod doit cesser toute nouvelle entrée.
   - Si une position est encore ouverte, le pod garde le droit de la gérer jusqu’à clôture.

4. **Pipeline partagé**
   - Le pod doit idéalement réutiliser les briques communes déjà utilisées par `Pod A` / `Pod C` :
     - modèle de signal ;
     - trade plan ;
     - risk checks ;
     - exécution dry-run / live ;
     - journalisation / reporting.

5. **Univers limité au cluster crypto**
   - `allowed_market_clusters = ["crypto"]`

6. **Compatibilité backtest / replay**
   - Le moteur doit fonctionner avec les snapshots TRIDENT basés sur `l2Book + trades`.
   - Il ne doit pas dépendre uniquement de chandeliers HL.

---

## 4. Mission exacte du Pod B

## 4.1 Ce que le pod doit capturer

Le pod doit chercher les situations suivantes :

- **Compression → cassure**
- **Micro-range → expansion impulsive**
- **Squeeze / release**
- **Continuation précoce après impulsion initiale**
- **Breakout avec confirmation par activité de marché**

L’objectif n’est pas de détecter tous les mouvements de marché.
L’objectif est de détecter **les mouvements asymétriques encore jeunes**, avec un bon ratio :

- invalidation proche,
- potentiel court terme significatif,
- décision rapide,
- durée de vie limitée.

## 4.2 Ce que le pod ne doit pas faire

Le pod ne doit pas :

- faire de market making ;
- maintenir des quotes bilatérales ;
- gérer des inventaires permanents ;
- prendre des trades de tendance déjà trop matures ;
- faire du mean reversion classique ;
- entrer sur des breaks sans filtre de qualité ;
- rester longtemps en position “par défaut”.

---

## 5. Philosophie de trading

Le nouveau `Pod B` est un moteur :

- **sélectif** ;
- **court terme** ;
- **directionnel** ;
- **faible fréquence** par rapport au market making ;
- **orienté qualité de setup plutôt que quantité de fills**.

### Idée centrale

Le pod doit accepter :

- beaucoup de phases d’attente,
- peu d’entrées,
- un taux de réussite potentiellement moyen,
- mais un profil où les gagnants sont plus gros que les perdants.

On cherche donc un moteur **convexe**, pas un moteur de petites captures répétées.

---

## 6. Régimes de marché cibles

Le supervisor devra favoriser `Pod B` sur certains régimes.

## 6.1 Régimes compatibles Pod B

### Régime 1 — Compression exploitable

Conditions macro du symbole :

- range étroit sur une fenêtre récente ;
- baisse de volatilité réalisée ;
- alternance de micro-mouvements sans direction ;
- peu de follow-through ;
- marché “chargé” mais pas encore parti.

Dans ce régime, `Pod B` ne déclenche pas immédiatement un trade.
Il se met en **mode watchlist active** et attend la rupture.

### Régime 2 — Expansion naissante

Conditions :

- cassure récente d’un range ou d’une zone de compression ;
- augmentation nette du volume / du nombre de trades ;
- déplacement du midprice avec follow-through ;
- déséquilibre de carnet compatible.

C’est le régime prioritaire pour les entrées `Pod B`.

### Régime 3 — Volatilité nerveuse mais encore tradable

Conditions :

- volatilité en hausse ;
- mouvement rapide ;
- spread et slippage encore acceptables ;
- pas de spike chaotique totalement imprenable.

Le pod peut encore agir, mais avec sizing réduit.

## 6.2 Régimes à éviter

- chop sale sans expansion claire ;
- marché déjà trop étendu ;
- liquidité trop faible ;
- slippage excessif ;
- bougie / impulsion déjà trop avancée ;
- contexte où `Pod A` a plus de légitimité ;
- contexte “panic spike” impossible à exécuter proprement.

---

## 7. Signaux d’entrée

Le signal d’entrée doit être composé de plusieurs briques.

## 7.1 Principe général

Un trade `Pod B` n’est autorisé que si :

1. **précondition de compression** validée,
2. **déclencheur de cassure / expansion** validé,
3. **confirmation d’activité** validée,
4. **filtres de qualité de marché** validés,
5. **risk / allocation / ownership** validés.

---

## 7.2 Précondition de compression

L’objectif est d’éviter les faux signaux sur des marchés déjà agités depuis longtemps.

### Mesures recommandées

Sur plusieurs fenêtres glissantes (ex. 5 min, 15 min, 30 min de snapshots) :

- largeur de range normalisée,
- realized vol courte,
- ratio vol courte / vol longue,
- compression du spread de prix,
- réduction de dispersion des returns,
- diminution de l’amplitude des swings intraminute.

### Heuristique de base

Un symbole est considéré en compression si :

- le **range des N dernières minutes** est inférieur à un seuil relatif au prix,
- la **volatilité récente** est inférieure à sa médiane / moyenne sur une fenêtre plus longue,
- le **nombre de cassures suivies d’échec** reste élevé,
- aucun mouvement directeur propre n’est déjà en cours.

### Sortie attendue

Le moteur produit un flag du type :

```python
compression_state = {
    "is_compressed": bool,
    "compression_score": float,
    "range_width_bps": float,
    "realized_vol_short": float,
    "realized_vol_long": float,
}
```

---

## 7.3 Déclencheur de breakout

Une fois la compression validée, le pod surveille la rupture du range.

### Conditions possibles

- franchissement du plus haut / plus bas du range de compression ;
- dépassement d’un seuil de distance au fair price / EMA ;
- déplacement du midprice suffisamment net ;
- confirmation par plusieurs snapshots consécutifs ;
- maintien du prix au-delà de la zone cassée.

### Conditions recommandées

Pour un **long breakout** :

- `midprice_now > compression_high + breakout_buffer`
- suivi sur `k` snapshots ou `t` secondes/minutes
- pas de réintégration immédiate dans le range

Pour un **short breakout** :

- `midprice_now < compression_low - breakout_buffer`
- même logique symétrique

### Paramètres utiles

- `breakout_buffer_bps`
- `breakout_confirmation_snapshots`
- `max_reentry_into_range_bps`
- `minimum_post_break_distance_bps`

---

## 7.4 Confirmation d’activité / énergie de marché

Une cassure sans activité est souvent un faux breakout.

### Signaux recommandés

- augmentation du nombre de trades récents ;
- hausse du volume notionnel ;
- augmentation de la vitesse de déplacement du prix ;
- order flow plus agressif ;
- déséquilibre bid/ask côté cassure ;
- top-of-book qui suit le mouvement.

### Features candidates

- `trade_count_30s / trade_count_baseline`
- `notional_volume_30s / baseline`
- `delta_mid_10s`, `delta_mid_30s`
- `book_imbalance_top_n`
- `microprice_vs_mid`
- `sweep_signature_score`

### Condition minimale

Au moins une partie des métriques d’activité doit dépasser un seuil.

Exemple :

```text
(activity_score >= threshold)
AND
(trade_count_ratio >= min_trade_count_ratio OR volume_ratio >= min_volume_ratio)
```

---

## 7.5 Filtres de qualité d’exécution

Le pod doit refuser un trade si le marché est trop sale.

### Filtres indispensables

- spread max ;
- slippage estimé max ;
- profondeur minimale dans le book ;
- prix pas trop étendu par rapport au point de départ ;
- liquidité minimale ;
- taille minimale d’ordre respectée ;
- funding / contexte défavorable si tu veux l’intégrer plus tard.

### Exemple

Refuser l’entrée si :

- `spread_bps > max_entry_spread_bps`
- `estimated_slippage_bps > max_estimated_slippage_bps`
- `distance_from_break_origin_bps > max_chase_bps`
- `book_depth_usd_top_levels < min_depth_usd`

---

## 7.6 Score global de setup

Le plus propre est de construire un score global.

### Proposition

```text
setup_score =
    w1 * compression_score
  + w2 * breakout_score
  + w3 * activity_score
  + w4 * book_quality_score
  - w5 * chase_penalty
  - w6 * execution_penalty
```

### Utilisation

- si `setup_score < entry_threshold` → pas d’entrée ;
- si `entry_threshold <= setup_score < strong_entry_threshold` → sizing normal ;
- si `setup_score >= strong_entry_threshold` → sizing renforcé dans la limite des caps.

---

## 8. Direction long / short

Le pod doit être symétrique.

### Long

- compression identifiée ;
- cassure haussière ;
- activité en soutien ;
- exécution propre ;
- risk ok.

### Short

- compression identifiée ;
- cassure baissière ;
- activité en soutien ;
- exécution propre ;
- risk ok.

### Point important

Le short ne doit pas être activé si Hyperliquid ou les règles internes du compte / stratégie imposent une contrainte particulière sur certains marchés.

---

## 9. Gestion des entrées

## 9.1 Type d’entrée

Le pod doit être pensé pour une entrée **rapide mais disciplinée**.

### Priorité

1. **ordre limite marketable / limite agressive contrôlée**
2. fallback éventuel sur market selon politique globale de l’executor

### Pourquoi

- éviter les dérapages inutiles ;
- rester cohérent avec les contraintes de microstructure ;
- mieux maîtriser le slippage.

## 9.2 Anti-chase

Il faut éviter d’acheter une impulsion déjà “trop loin”.

### Règle

Interdire l’entrée si :

- le prix s’est déjà trop éloigné du bord du range cassé ;
- le move initial a déjà consommé l’essentiel du potentiel ;
- le ratio risque/récompense devient mauvais.

### Paramètres

- `max_chase_bps`
- `max_move_from_break_origin_bps`
- `min_remaining_room_bps`

---

## 10. Gestion de position

Le `Pod B` doit avoir une logique de gestion stricte, car sa valeur vient du timing plus que de la durée.

## 10.1 Stop loss initial

Le stop doit être proche et logique.

### Références possibles

- retour dans le range de compression ;
- cassure invalidée ;
- distance fixe en bps ;
- ATR / vol récente ;
- plus bas / plus haut structurel du setup.

### Recommandation

Stop initial basé sur :

- `break_level`
- buffer d’invalidation
- éventuellement capé par une vol max tolérée

Exemple long :

```text
stop_price = max(
    compression_high - invalidation_buffer,
    entry_price - max_initial_risk_bps
)
```

Symétrique pour short.

## 10.2 Take profit / sortie

Je recommande une approche hybride.

### Sorties possibles

- TP1 partiel sur premier objectif ;
- trailing stop agressif après extension ;
- exit si perte de momentum ;
- exit timeout ;
- exit si supervisor retire le routing ;
- exit si allocation du pod tombe à zéro ;
- exit si signal opposé fort.

### Hiérarchie

1. sécurité / invalidation
2. `routing_revoked` / `allocation_zero`
3. perte de momentum
4. trailing
5. TP / objectifs
6. timeout maximal

## 10.3 Perte de momentum

Un breakout sans continuation doit être coupé vite.

### Signaux de perte de momentum

- retour sous le niveau cassé ;
- ralentissement brutal de l’activité ;
- book imbalance qui se retourne ;
- incapacité à prolonger le mouvement après entrée ;
- stagnation trop longue.

### Paramètres

- `momentum_decay_timeout_seconds`
- `max_bars_without_extension`
- `reentry_into_range_exit`

## 10.4 Timeout de position

Le pod n’est pas fait pour porter longtemps.

### Règle

Une position doit être fermée si elle n’a pas atteint ses objectifs dans une durée max.

Exemples :

- 5 min,
- 15 min,
- 30 min,

selon la granularité réelle des snapshots et le style retenu.

---

## 11. Sizing et allocation

## 11.1 Position sizing

Le sizing doit dépendre de :

- allocation accordée au pod par le supervisor ;
- qualité du setup ;
- volatilité du symbole ;
- liquidité ;
- exposition déjà en cours ;
- caps par symbole et par pod.

## 11.2 Proposition de sizing

```text
base_notional = pod_allocated_capital * base_risk_fraction
quality_multiplier = f(setup_score)
volatility_multiplier = inverse_vol_cap
liquidity_multiplier = f(book_depth, spread, slippage)

final_notional = min(
    base_notional * quality_multiplier * volatility_multiplier * liquidity_multiplier,
    max_notional_per_trade,
    max_notional_per_symbol,
    available_capital_for_pod,
)
```

## 11.3 Levier

Le levier doit rester plus conservateur que l’ambition de gain ne le suggère.

Recommandation :

- levier plafonné par symbole ;
- levier plus bas sur altcoins moins liquides ;
- levier réduit quand la volatilité explose.

## 11.4 Limites d’exposition

Caps recommandés :

- `max_open_positions`
- `max_open_positions_per_direction`
- `max_symbol_exposure_usd`
- `max_pod_gross_exposure_usd`
- `max_pod_daily_loss_usd`
- `max_correlated_exposure_group`

---

## 12. Intégration supervisor / routing

## 12.1 Conditions de routing favorables

Le supervisor doit router un symbole vers `Pod B` seulement si :

- cluster = `crypto`
- symbole tradable
- données fraîches
- pas déjà owned par un autre pod
- régime compatible `Pod B`
- allocation `Pod B > 0`
- score de priorisation du symbole suffisant

## 12.2 Priorité entre Pod A et Pod B

Il faut une règle claire d’arbitrage.

### Proposition simple

Si un symbole est candidat pour les deux pods :

- `Pod B` gagne si le setup est une **sortie de compression récente** avec **impulsion encore jeune** ;
- `Pod A` gagne si le mouvement est déjà **plus mature / plus stable / plus propre**.

### Exemple de règle

```text
if breakout_freshness_score >= X and compression_score >= Y:
    route -> Pod B
elif trend_quality_score >= Z:
    route -> Pod A
else:
    route -> none
```

## 12.3 Révocation de routing

Quand le supervisor retire le symbole à `Pod B` :

- aucune nouvelle entrée ;
- annulation des ordres d’entrée en attente ;
- si position ouverte, maintien du symbole dans `managed_symbols` ;
- sortie gérée normalement ou accélérée selon politique choisie.

---

## 13. États internes du Pod B

Le pod gagnera à être implémenté comme une machine à états.

## 13.1 États proposés

```text
IDLE
WATCHING_COMPRESSION
ARMED_LONG
ARMED_SHORT
ENTERING_LONG
ENTERING_SHORT
MANAGING_LONG
MANAGING_SHORT
UNWIND_ONLY
COOLDOWN
DISABLED
```

## 13.2 Description

### `IDLE`
Aucun setup en cours sur le symbole.

### `WATCHING_COMPRESSION`
Le symbole montre une compression exploitable, mais pas encore de cassure valide.

### `ARMED_LONG` / `ARMED_SHORT`
Préconditions validées, cassure imminente ou en confirmation.

### `ENTERING_LONG` / `ENTERING_SHORT`
Ordre en cours d’entrée / attente de fill.

### `MANAGING_LONG` / `MANAGING_SHORT`
Position ouverte et gérée activement.

### `UNWIND_ONLY`
Le symbole n’est plus ouvrable mais reste gérable pour fermeture propre.

### `COOLDOWN`
Le symbole vient d’échouer / sortir ; on évite de re-rentrer trop vite.

### `DISABLED`
Symbole ou pod désactivé par allocation / kill switch / risk gate.

## 13.3 Transitions essentielles

- `IDLE -> WATCHING_COMPRESSION`
- `WATCHING_COMPRESSION -> ARMED_LONG/SHORT`
- `ARMED_* -> ENTERING_*`
- `ENTERING_* -> MANAGING_*`
- `MANAGING_* -> UNWIND_ONLY` si routing retiré
- `MANAGING_* -> COOLDOWN` après clôture
- `UNWIND_ONLY -> COOLDOWN` après clôture
- `* -> DISABLED` si kill switch / risque / données stales

---

## 14. Structures de données recommandées

## 14.1 Features marché par symbole

```python
@dataclass
class PodBMarketFeatures:
    symbol: str
    ts: datetime
    midprice: float
    spread_bps: float
    compression_score: float
    range_width_bps: float
    realized_vol_short: float
    realized_vol_long: float
    breakout_direction: str | None
    breakout_score: float
    activity_score: float
    book_imbalance: float
    microprice_dislocation: float
    trade_count_ratio: float
    volume_ratio: float
    estimated_slippage_bps: float
    chase_distance_bps: float
    setup_score: float
    is_tradable: bool
```

## 14.2 Signal

```python
@dataclass
class PodBSignal:
    symbol: str
    ts: datetime
    side: Literal["long", "short"]
    setup_type: Literal["compression_breakout", "vol_expansion"]
    confidence: float
    entry_reference_price: float
    breakout_level: float
    stop_price: float
    take_profit_price: float | None
    trailing_activation_price: float | None
    timeout_seconds: int
    metadata: dict[str, Any]
```

## 14.3 Position runtime

```python
@dataclass
class PodBRuntimePosition:
    symbol: str
    side: str
    entry_ts: datetime
    entry_price: float
    size: float
    notional_usd: float
    stop_price: float
    initial_break_level: float
    state: str
    setup_score_at_entry: float
    routing_open_allowed: bool
    routing_manage_allowed: bool
    cooldown_until: datetime | None
```

---

## 15. Modules à créer / modifier

## 15.1 Nouveau package cible

Je recommande quelque chose du type :

```text
app/trident/pod_b_v3/
    __init__.py
    config.py
    features.py
    compression.py
    breakout.py
    scoring.py
    signal_engine.py
    position_manager.py
    runner.py
    live_runner.py
    report.py
    state_machine.py
    types.py
```

Si tu préfères conserver `app/trident/pod_b/`, alors refactoriser l’existant pour éviter un mélange avec l’ancien moteur Avellaneda-Stoikov.

## 15.2 Modules détaillés

### `config.py`
- lecture / validation de la config du pod ;
- valeurs par défaut ;
- normalisation des seuils.

### `features.py`
- calcul des features à partir des snapshots `l2Book + trades` ;
- agrégation multi-fenêtres.

### `compression.py`
- détection de compression ;
- calcul du range et scores.

### `breakout.py`
- validation des cassures ;
- long / short ;
- confirmation / réintégration / false breakout guards.

### `scoring.py`
- score global de setup ;
- pénalités ;
- quality tiers.

### `signal_engine.py`
- décision finale `trade / no-trade` ;
- génération du `PodBSignal`.

### `position_manager.py`
- stop, trailing, timeout, momentum decay ;
- gestion `routing_revoked` ;
- bascule en `unwind-only`.

### `runner.py`
- exécution sur input historique / replay ;
- boucle principale du pod en mode backtest/paper.

### `live_runner.py`
- branchement runtime sur snapshots live ;
- intégration au supervisor ;
- journal et status runtime.

### `report.py`
- agrégats par symbole / date ;
- PnL ;
- win rate ;
- expectancy ;
- hold time ;
- distribution des setups.

### `state_machine.py`
- transitions d’état ;
- logique interne symbol-centric.

### `types.py`
- dataclasses / types runtime.

---

## 16. Intégration au pipeline partagé

## 16.1 Recommandation

Réutiliser autant que possible les abstractions déjà utilisées par `Pod A` / `Pod C`.

Le flux cible doit être :

```text
snapshot
 -> feature extraction
 -> setup detection
 -> signal
 -> trade plan
 -> risk checks
 -> executor
 -> journal/report
```

## 16.2 Trade plan

Le `Pod BSignal` doit être converti vers un trade plan standard compatible avec les composants de risk et d’exécution existants.

Le trade plan doit inclure :

- symbole ;
- side ;
- entry type ;
- reference price ;
- stop ;
- TP éventuel ;
- timeout ;
- metadata de setup ;
- sizing proposé ;
- raison d’ouverture lisible.

## 16.3 Exécution

Au début :

- dry-run prioritaire ;
- paper mode avant live ;
- live uniquement après validation suffisante.

---

## 17. Configuration TOML recommandée

Exemple de structure :

```toml
[pod_b]
enabled = true
strategy = "breakout_v3"
allowed_market_clusters = ["crypto"]

[pod_b.routing]
min_supervisor_allocation_pct = 0.05
prefer_over_pod_a_when_breakout_freshness_above = 0.75

[pod_b.features]
compression_window_minutes = 15
vol_short_window_minutes = 5
vol_long_window_minutes = 30
activity_window_seconds = 30
breakout_confirmation_snapshots = 2

[pod_b.entry]
entry_threshold = 0.72
strong_entry_threshold = 0.85
breakout_buffer_bps = 6.0
max_entry_spread_bps = 8.0
max_estimated_slippage_bps = 12.0
max_chase_bps = 20.0
min_trade_count_ratio = 1.5
min_volume_ratio = 1.8
min_depth_usd = 20000
cooldown_seconds = 300

[pod_b.risk]
base_risk_fraction = 0.03
max_notional_per_trade_usd = 150
max_notional_per_symbol_usd = 200
max_open_positions = 3
max_symbol_leverage = 5
max_pod_daily_loss_usd = 40
max_initial_risk_bps = 35

[pod_b.exit]
invalidation_buffer_bps = 8.0
momentum_decay_timeout_seconds = 90
max_bars_without_extension = 3
position_timeout_seconds = 900
use_trailing_stop = true
trailing_activation_rr = 1.0
trailing_distance_bps = 18.0

[pod_b.reporting]
write_runtime_status = true
write_trade_journal = true
```

Les valeurs ci-dessus sont **indicatives**, pas finales.

---

## 18. Features détaillées à calculer

## 18.1 À partir du `l2Book`

- bid / ask top ;
- spread absolu et en bps ;
- profondeur top `n` niveaux ;
- book imbalance ;
- microprice ;
- pente de profondeur ;
- évolution de la meilleure liquidité ;
- disparition / apparition de liquidité proche.

## 18.2 À partir des trades

- nombre de trades par fenêtre ;
- volume notionnel ;
- taille moyenne ;
- burstiness ;
- intensité d’activité ;
- agressivité des prints si reconstructible ;
- rolling delta si faisable.

## 18.3 À partir du prix / mid

- returns glissants ;
- realized vol ;
- range width ;
- breakout distance ;
- extension distance ;
- vitesse du move ;
- retests / reentries.

## 18.4 Features composites

- compression score ;
- breakout freshness score ;
- setup score ;
- execution quality score ;
- momentum persistence score ;
- fake-break risk score.

---

## 19. Logique de sortie détaillée

## 19.1 Invalidation structurelle

Sortie immédiate si :

- retour clair dans le range ;
- cassure invalidée ;
- stop touché ;
- contexte d’exécution dégradé.

## 19.2 Time-based exit

Sortie si le trade reste vivant mais sans extension suffisante.

## 19.3 Trailing

Une fois un certain RR atteint :

- remonter le stop ;
- protéger rapidement ;
- ne pas redonner trop de gains.

## 19.4 Supervisor-driven exit

Si :

- allocation à zéro ;
- routing retiré ;
- kill switch ;
- symbole non tradable,

alors le pod doit fermer ou au minimum passer en mode de sortie accélérée.

---

## 20. Gestion des faux breakouts

C’est le principal risque de la stratégie.

## 20.1 Parades

- exiger une compression préalable ;
- buffer de cassure ;
- confirmation sur plusieurs snapshots ;
- exiger une hausse d’activité ;
- refuser les cassures avec spread/slippage trop élevés ;
- anti-chase ;
- cooldown après échec ;
- stopper vite les trades sans follow-through.

## 20.2 Cooldown

Après un faux breakout :

- interdiction de ré-entrer immédiatement ;
- éventuellement cooldown plus long après deux échecs rapprochés sur le même symbole.

---

## 21. Backtest et replay

## 21.1 Données requises

Le pod doit fonctionner sur les snapshots minute / multi-snapshots basés sur :

- `l2Book`
- `trades`

Il ne doit pas dépendre de candles HL seules.

## 21.2 Moteur de replay

Créer un runner dédié, par exemple :

```bash
python3.12 -m app.trident.pod_b_v3.runner \
  --config config/trident.toml \
  --input /path/to/trident_snapshots.jsonl \
  --report-output /tmp/pod_b_v3_report.json \
  --journal-output /tmp/pod_b_v3_journal.jsonl
```

## 21.3 Sorties attendues du replay

Le replay doit produire :

- `records_processed`
- `candidate_setups`
- `signals_emitted`
- `orders_emitted`
- `fills_emitted`
- `realized_pnl_usd`
- `unrealized_pnl_usd`
- `max_drawdown_usd`
- `win_rate`
- `profit_factor`
- `expectancy_per_trade`
- `avg_holding_seconds`
- `median_holding_seconds`
- `false_break_rate`
- `exit_reason_breakdown`
- `setup_type_breakdown`
- `per_symbol_metrics`

## 21.4 Important

Le replay doit intégrer autant que possible :

- slippage simulé ;
- rejet d’ordres impossibles ;
- tailles mini ;
- caps de liquidité ;
- latence raisonnable simulée si faisable.

---

## 22. Tests à écrire

## 22.1 Unit tests

### Compression
- détecte une vraie compression ;
- ne détecte pas une fausse compression sur marché déjà nerveux.

### Breakout
- long breakout valide ;
- short breakout valide ;
- faux breakout rejeté ;
- breakout rejeté si déjà trop étendu.

### Activity confirmation
- activité suffisante valide ;
- cassure sans activité rejetée.

### Scoring
- score croissant avec qualité ;
- pénalité anti-chase appliquée.

### Position manager
- stop initial correct ;
- timeout ;
- trailing ;
- reentry into range → exit ;
- routing revoked → unwind-only.

## 22.2 Integration tests

- symbol routed to Pod B only ;
- pas d’entrée hors `opening_symbols` ;
- gestion toujours possible en `managed_symbols` ;
- annulation des entrées à la révocation ;
- coexistence correcte avec Pod A ;
- respect des caps de risque ;
- reporting généré.

## 22.3 Replay regression tests

Créer quelques datasets de référence :

- journée avec compression + breakout haussier ;
- journée avec faux breakout ;
- journée sans setup ;
- journée de volatilité sale ;
- journée avec cohabitation Pod A / Pod B.

---

## 23. Reporting et observabilité

Le nouveau pod doit exposer un reporting lisible au même niveau que les autres pods.

## 23.1 Runtime status

Fichier JSON de status recommandé :

```text
logs/pod_b_v3_status.json
```

Contenu recommandé :

- état global ;
- derniers timestamps traités ;
- symboles surveillés ;
- symboles armés ;
- positions ouvertes ;
- cooldowns ;
- nombre de setups détectés ;
- nombre de signaux ;
- raisons des derniers refus.

## 23.2 Reporting agrégé

Inclure :

- `signals_by_symbol`
- `signals_by_setup_type`
- `rejections_by_reason`
- `fills_by_symbol`
- `realized_pnl_by_date`
- `avg_slippage_bps`
- `avg_setup_score`
- `avg_hold_time`
- `routing_revoked_exits`
- `momentum_decay_exits`
- `timeout_exits`

## 23.3 Dashboard

Ajouter de quoi voir :

- symboles en `WATCHING_COMPRESSION`
- symboles `ARMED_LONG/SHORT`
- setup score par symbole ;
- derniers signaux ;
- raisons de rejet fréquentes ;
- positions ouvertes ;
- état `unwind-only` ;
- métriques replay/live.

---

## 24. Plan de déploiement recommandé

## 24.1 Phase 1 — Research offline

Objectif : valider les features et seuils.

Livrables :

- extraction des features ;
- runner de replay ;
- rapport markdown/json ;
- premières métriques de qualité.

## 24.2 Phase 2 — Paper runner

Objectif : faire tourner le pod sur snapshots live sans exécution réelle.

Livrables :

- journal des setups ;
- journal des signaux ;
- faux positifs / faux négatifs ;
- comportement sous routing réel.

## 24.3 Phase 3 — Dry-run cohabitation avec Pod A

Objectif : vérifier qu’il ne se marche pas dessus avec Pod A.

Vérifications :

- exclusivité par symbole ;
- pas de prise de symboles là où Pod A devrait dominer ;
- cohérence des arbitrages du supervisor.

## 24.4 Phase 4 — Live très limité

Objectif : validation prudente.

Recommandations :

- taille minimale ;
- peu de symboles ;
- caps stricts ;
- kill switch facile ;
- suivi quotidien.

## 24.5 Phase 5 — Généralisation

- tuning des seuils ;
- amélioration des règles Pod A vs Pod B ;
- extension prudente du périmètre.

---

## 25. Critères de réussite

Le nouveau `Pod B` est considéré comme réussi si :

1. il s’intègre proprement à TRIDENT ;
2. il ne casse pas la règle d’exclusivité par symbole ;
3. il ne duplique pas `Pod A` ;
4. il produit un journal et un reporting exploitables ;
5. il est backtestable sur les snapshots existants ;
6. il montre une utilité marginale claire par rapport à la situation actuelle ;
7. il améliore le profil global du bot en complément de `Pod A`, pas en concurrence frontale.

---

## 26. Critères d’échec

Le projet doit être reconsidéré si :

- les entrées se superposent massivement à Pod A ;
- le pod ne capte que du bruit ;
- les faux breakouts dominent ;
- le slippage tue le signal ;
- le pod finit par se comporter comme un mauvais trend follower ;
- l’amélioration du full-bot est nulle ou négative sur plusieurs replays significatifs.

---

## 27. Pseudocode d’ensemble

```python
def on_snapshot(symbol, snapshot, routing_state, allocation_state):
    if not routing_state.manage_allowed(symbol):
        cancel_pending_entries(symbol)
        disable_symbol(symbol)
        return

    features = compute_features(symbol, snapshot)
    current_state = state_machine.get(symbol)

    if has_open_position(symbol):
        manage_existing_position(symbol, snapshot, features, routing_state)
        return

    if not routing_state.open_allowed(symbol):
        state_machine.set(symbol, "IDLE")
        return

    if not allocation_state.pod_b_enabled_for(symbol):
        state_machine.set(symbol, "DISABLED")
        return

    if in_cooldown(symbol):
        state_machine.set(symbol, "COOLDOWN")
        return

    compression = detect_compression(features)
    if not compression.is_compressed:
        state_machine.set(symbol, "IDLE")
        return

    breakout = detect_breakout(features, compression)
    if not breakout.is_valid:
        state_machine.set(symbol, "WATCHING_COMPRESSION")
        return

    if not passes_activity_filters(features):
        state_machine.set(symbol, "WATCHING_COMPRESSION")
        return

    if not passes_execution_filters(features):
        reject(symbol, reason="execution_filters")
        state_machine.set(symbol, "COOLDOWN")
        return

    setup_score = score_setup(features, compression, breakout)
    if setup_score < config.entry_threshold:
        reject(symbol, reason="setup_score")
        state_machine.set(symbol, "WATCHING_COMPRESSION")
        return

    signal = build_signal(symbol, features, compression, breakout, setup_score)
    trade_plan = build_trade_plan(signal, allocation_state)

    risk_result = risk_engine.validate(trade_plan)
    if not risk_result.allowed:
        reject(symbol, reason=risk_result.reason)
        state_machine.set(symbol, "COOLDOWN")
        return

    execution_result = executor.submit(trade_plan)
    if execution_result.accepted:
        state_machine.set(symbol, "MANAGING_" + signal.side.upper())
    else:
        reject(symbol, reason=execution_result.reason)
        state_machine.set(symbol, "COOLDOWN")
```

---

## 28. Roadmap d’implémentation concrète

## Étape 1 — Freeze de l’ancien Pod B

- conserver l’ancien moteur comme benchmark ;
- ne pas le supprimer immédiatement ;
- isoler le nouveau développement sous un namespace clair.

## Étape 2 — Features + replay

- implémenter extraction de features ;
- implémenter compression + breakout + scoring ;
- runner historique ;
- rapports de recherche.

## Étape 3 — Signal engine

- signal ;
- stop / TP / timeout ;
- intégration au trade plan standard.

## Étape 4 — Position manager

- trailing ;
- momentum decay ;
- unwind-only ;
- routing revoked.

## Étape 5 — Paper live runner

- fonctionnement sur snapshots live ;
- runtime status ;
- dashboard minimal.

## Étape 6 — Cohabitation supervisor

- règles Pod A vs Pod B ;
- tuning de l’arbitrage ;
- replays full-bot.

## Étape 7 — Live limité

- petites tailles ;
- contrôles renforcés ;
- revue quotidienne.

---

## 29. Arbitrages de design recommandés

### À privilégier

- simplicité du premier signal engine ;
- règles explicites avant ML ;
- qualité d’exécution ;
- observabilité forte ;
- peu de paramètres mais lisibles.

### À éviter au départ

- overfitting ;
- trop de setups différents ;
- microstructure trop complexe si non robuste ;
- sizing agressif prématuré ;
- logique Pod A / Pod B ambiguë.

---

## 30. Décision recommandée

La meilleure façon d’implémenter le nouveau `Pod B` dans TRIDENT est de le construire comme :

> **un pod crypto directionnel spécialisé dans les cassures de compression et les expansions de volatilité, court terme, sélectif, fortement filtré, et clairement différencié de Pod A par sa fenêtre d’intervention et ses règles de routing.**

C’est l’option la plus cohérente avec :

- l’architecture actuelle de TRIDENT,
- la contrainte d’un seul trade par coin à la fois,
- la faiblesse actuelle du Pod B existant,
- et le besoin d’un complément à `Pod A`, pas d’un concurrent direct.
