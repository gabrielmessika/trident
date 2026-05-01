# Bot Hyperliquid HIP-4 / Outcome Tokens — Handoff technique pour remplacement du Pod B Trident

## 1. Objectif du document

Ce document décrit comment construire un bot dédié aux **outcome tokens HIP-4 Hyperliquid**, puis comment adapter cette idée pour remplacer le **Pod B** d’un bot existant de type **Trident**, composé de :

- un **Supervisor** central ;
- trois pods spécialisés ;
- une contrainte de coordination globale, notamment éviter les conflits de positions entre pods.

L’objectif n’est pas de faire un bot de trading directionnel classique sur BTC/ETH/SOL, mais de détecter des **inefficiences de pricing probabiliste** sur des marchés binaires de type :

```text
BTC > 70 000$ à une date/heure donnée ?
YES / NO
```

Ces marchés doivent être vus comme des marchés de probabilité, pas comme des marchés spot/perp classiques.

---

## 2. Contexte : HIP-4 / Outcome tokens Hyperliquid

Les **outcome tokens** sont des marchés binaires ou conditionnels.

Exemple simplifié :

```text
Question :
BTC sera-t-il au-dessus de 70 000$ à 23:59 UTC ?

Token YES :
vaut 1 si l’événement est vrai
vaut 0 si l’événement est faux

Token NO :
vaut 1 si l’événement est faux
vaut 0 si l’événement est vrai
```

Le prix du token YES représente donc une **probabilité implicite**.

Exemple :

```text
YES = 0.64
=> le marché price environ 64% de probabilité que l’événement arrive
```

Le bot doit donc comparer :

```text
probabilité implicite du marché
vs
probabilité estimée par le modèle
```

et non pas simplement comparer le prix de BTC sur deux exchanges.

---

## 3. Idée principale d’edge

Le bot cherche un edge lorsque :

```text
proba_modèle - prix_market > coûts + spread + slippage + marge de sécurité
```

Pour YES :

```text
edge_yes = probability_model - ask_yes
```

Pour NO :

```text
edge_no = (1 - probability_model) - ask_no
```

Exemple :

```text
Le modèle estime 78% de chance que BTC finisse au-dessus du strike.
YES ask = 0.68

edge_yes = 0.78 - 0.68 = 0.10
=> edge brut de 10 points
```

Le trade n’est autorisé que si l’edge net est suffisamment élevé.

---

## 4. Les trois edges à tester

### 4.1 Edge A — Late-expiry arbitrage

C’est l’edge le plus intuitif.

Il apparaît dans les dernières minutes avant expiration si le prix réel du sous-jacent est déjà très au-dessus ou très en dessous du strike, mais que le token YES/NO n’a pas encore convergé vers 1 ou 0.

Exemple :

```text
Marché :
BTC > 70 000$ à 23:59 UTC

À 23:57 UTC :
BTC = 70 180$
YES ask = 0.82
Temps restant = 2 minutes
```

Si le prix de référence du settlement est fiable et que BTC a une marge suffisante au-dessus du strike, YES devrait valoir beaucoup plus proche de 1.

Signal possible :

```text
IF time_to_expiry < 10 minutes
AND reference_price > strike * 1.001
AND YES_ask < 0.92
AND liquidity_ok
THEN buy YES
```

Version inverse :

```text
IF time_to_expiry < 10 minutes
AND reference_price < strike * 0.999
AND NO_ask < 0.92
AND liquidity_ok
THEN buy NO
```

Ce signal est simple, mais très sensible à la latence, la liquidité, le spread, la source exacte de settlement et le risque de mouvement violent dans les dernières secondes.

---

### 4.2 Edge B — YES/NO parity arbitrage

Sur un marché binaire propre :

```text
YES + NO ≈ 1
```

Il peut exister une inefficience si :

```text
YES ask + NO ask < 1
```

Exemple :

```text
YES ask = 0.47
NO ask = 0.47

Coût total = 0.94
Payout final = 1.00
Edge brut = 0.06
```

Pseudo-règle :

```text
IF YES_ask + NO_ask < 1 - fees - slippage - safety_margin
THEN buy YES and buy NO
```

À tester aussi côté bid si la vente/short est possible :

```text
YES_bid + NO_bid > 1 + fees + slippage + safety_margin
```

Mais cette variante dépend fortement des règles de marge, de collateral, de shorting, et de l’implémentation exacte du marché.

---

### 4.3 Edge C — Model probability mispricing

Le bot estime une probabilité théorique à partir de :

- prix spot/perp externe ;
- distance au strike ;
- temps restant ;
- volatilité réalisée ;
- momentum court terme ;
- spread multi-exchange ;
- funding/basis si pertinent ;
- liquidité et microstructure.

Puis il compare cette probabilité au prix du marché.

Exemple :

```text
Marché :
ETH > 3 500$ demain 00:00 UTC

Prix actuel ETH = 3 430$
Temps restant = 6h
Volatilité réalisée court terme = élevée

Modèle :
P(ETH > 3 500) = 34%

Market :
YES ask = 0.23

Edge brut = 0.34 - 0.23 = 0.11
```

Le modèle peut commencer simple, puis devenir plus sophistiqué.

---

## 5. Phases de développement recommandées

### Phase 1 — Observer-only

Aucun ordre n’est envoyé.

Le bot :

1. découvre les marchés outcome disponibles ;
2. parse les questions, strikes, expiries ;
3. récupère les prix externes ;
4. lit les orderbooks YES/NO ;
5. calcule les edges ;
6. loggue les opportunités ;
7. ne trade pas.

Objectif :

```text
Prouver que la détection fonctionne.
```

### Phase 2 — Paper trading

Le bot simule les entrées/sorties et enregistre signal, prix théorique, profondeur, slippage, expiry, settlement théorique, PnL simulé et raison de sortie.

Objectif :

```text
Prouver que l’edge théorique reste positif après coûts simulés.
```

### Phase 3 — Testnet execution

Le bot passe de vrais ordres testnet afin de valider signature API, ordre, annulation, fill partiel, fill complet, settlement, PnL, erreurs réseau et reconnexion websocket.

Objectif :

```text
Prouver que l’exécution réelle fonctionne sans risque de capital.
```

### Phase 4 — Replay/backtest

Le bot rejoue les opportunités détectées en observer-only ou paper.

À produire :

```text
opportunities.csv
trades_paper.csv
fills_testnet.csv
settlements.csv
daily_summary.csv
edge_decay.csv
latency_stats.csv
```

### Phase 5 — Préparation mainnet

Ne passer au mainnet que si :

```text
- parsing fiable
- settlement compris
- edge net positif en paper
- exécution testnet stable
- risque maximum borné
- logs complets
- kill switch testé
```

---

## 6. Architecture technique proposée

Structure de projet :

```text
/outcome_bot
  config.yaml
  main.py

  discovery/
    markets_discovery.py
    outcome_parser.py

  data/
    external_prices.py
    hyperliquid_orderbook.py
    volatility.py
    funding.py

  model/
    probability_model.py
    late_expiry_model.py
    parity_model.py

  edge/
    edge_detector.py
    edge_ranker.py
    opportunity.py

  execution/
    paper_trader.py
    testnet_executor.py
    order_manager.py
    settlement_tracker.py

  risk/
    risk_manager.py
    liquidity_filter.py
    kill_switch.py

  trident/
    hip4_outcome_edge_pod.py
    supervisor_contract.py

  logs/
    opportunities.csv
    decisions.jsonl
    trades.csv
```

---

## 7. Configuration YAML minimale

```yaml
mode: observer  # observer | paper | testnet | mainnet

hyperliquid:
  api_url: "https://api.hyperliquid-testnet.xyz"
  ws_url: "wss://api.hyperliquid-testnet.xyz/ws"
  use_sdk: true

markets:
  include:
    - BTC
    - ETH
    - HYPE
  max_time_to_expiry_minutes: 1440
  min_time_to_expiry_seconds: 30

external_prices:
  sources:
    - binance
    - coinbase
    - bybit
  aggregation: median
  max_source_deviation_bps: 20

edge:
  min_gross_edge: 0.05
  min_net_edge: 0.03
  late_expiry_window_minutes: 10
  safety_margin: 0.02

liquidity:
  min_yes_depth_usdc: 100
  min_no_depth_usdc: 100
  max_spread: 0.05

risk:
  max_position_usdc: 50
  max_total_exposure_usdc: 200
  max_markets_open: 3
  allow_only_one_trade_per_underlying: true
  kill_switch_drawdown_usdc: 50

trident:
  pod_name: "HIP4OutcomeEdgePod"
  replaces: "PodB"
  require_supervisor_approval: true
  publish_signals_only: true
```

---

## 8. Modèles de données

### 8.1 OutcomeMarket

```python
@dataclass
class OutcomeMarket:
    market_id: str
    question: str
    underlying: str
    strike: float | None
    expiry_ts: int
    settlement_source: str | None
    yes_token: str
    no_token: str
    status: str
```

### 8.2 OrderBookSnapshot

```python
@dataclass
class OrderBookSnapshot:
    market_id: str
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    yes_ask_depth: float
    no_ask_depth: float
    ts: int
```

### 8.3 ProbabilityEstimate

```python
@dataclass
class ProbabilityEstimate:
    market_id: str
    probability_yes: float
    model_name: str
    confidence: float
    inputs: dict
    ts: int
```

### 8.4 Opportunity

```python
@dataclass
class Opportunity:
    market_id: str
    underlying: str
    side: str  # BUY_YES | BUY_NO | BUY_BOTH
    edge_type: str  # LATE_EXPIRY | PARITY | MODEL
    gross_edge: float
    estimated_fees: float
    estimated_slippage: float
    net_edge: float
    confidence: float
    max_size_usdc: float
    expiry_ts: int
    reason: str
```

---

## 9. Modèle de probabilité simple

Pour un événement :

```text
BTC > strike à expiry
```

On peut commencer avec un modèle lognormal simplifié.

Inputs :

```text
S = prix actuel
K = strike
T = temps restant en années
sigma = volatilité annualisée
```

Pseudo-code :

```python
def probability_above_strike(spot, strike, time_to_expiry_years, annualized_vol):
    if time_to_expiry_years <= 0:
        return 1.0 if spot > strike else 0.0

    if annualized_vol <= 0:
        return 1.0 if spot > strike else 0.0

    z = (math.log(spot / strike)) / (annualized_vol * math.sqrt(time_to_expiry_years))
    return normal_cdf(z)
```

Ce modèle doit être amélioré avec volatilité multi-window, skew, momentum, microstructure, distance au strike et comportement near-expiry.

---

## 10. Détecteur Late-expiry

```python
def detect_late_expiry(market, ob, ref_price, now_ts, cfg):
    time_left = market.expiry_ts - now_ts

    if time_left <= cfg.min_time_to_expiry_seconds:
        return None

    if time_left > cfg.late_expiry_window_seconds:
        return None

    if market.strike is None:
        return None

    if ref_price > market.strike * (1 + cfg.strike_buffer):
        if ob.yes_ask is not None and ob.yes_ask < cfg.max_late_yes_price:
            edge = 1.0 - ob.yes_ask
            return Opportunity(
                market_id=market.market_id,
                underlying=market.underlying,
                side="BUY_YES",
                edge_type="LATE_EXPIRY",
                gross_edge=edge,
                estimated_fees=cfg.estimated_fees,
                estimated_slippage=cfg.estimated_slippage,
                net_edge=edge - cfg.estimated_fees - cfg.estimated_slippage - cfg.safety_margin,
                confidence=0.7,
                max_size_usdc=cfg.max_position_usdc,
                expiry_ts=market.expiry_ts,
                reason="Underlying safely above strike near expiry"
            )

    if ref_price < market.strike * (1 - cfg.strike_buffer):
        if ob.no_ask is not None and ob.no_ask < cfg.max_late_no_price:
            edge = 1.0 - ob.no_ask
            return Opportunity(
                market_id=market.market_id,
                underlying=market.underlying,
                side="BUY_NO",
                edge_type="LATE_EXPIRY",
                gross_edge=edge,
                estimated_fees=cfg.estimated_fees,
                estimated_slippage=cfg.estimated_slippage,
                net_edge=edge - cfg.estimated_fees - cfg.estimated_slippage - cfg.safety_margin,
                confidence=0.7,
                max_size_usdc=cfg.max_position_usdc,
                expiry_ts=market.expiry_ts,
                reason="Underlying safely below strike near expiry"
            )

    return None
```

---

## 11. Détecteur YES/NO parity

```python
def detect_parity(market, ob, cfg):
    if ob.yes_ask is None or ob.no_ask is None:
        return None

    cost = ob.yes_ask + ob.no_ask
    gross_edge = 1.0 - cost

    estimated_costs = cfg.estimated_fees + cfg.estimated_slippage + cfg.safety_margin

    if gross_edge > estimated_costs:
        return Opportunity(
            market_id=market.market_id,
            underlying=market.underlying,
            side="BUY_BOTH",
            edge_type="PARITY",
            gross_edge=gross_edge,
            estimated_fees=cfg.estimated_fees,
            estimated_slippage=cfg.estimated_slippage,
            net_edge=gross_edge - estimated_costs,
            confidence=0.9,
            max_size_usdc=cfg.max_position_usdc,
            expiry_ts=market.expiry_ts,
            reason=f"YES+NO ask below 1: {cost}"
        )

    return None
```

---

## 12. Détecteur model mispricing

```python
def detect_model_mispricing(market, ob, prob, cfg):
    opportunities = []

    if ob.yes_ask is not None:
        edge_yes = prob.probability_yes - ob.yes_ask
        net_yes = edge_yes - cfg.estimated_fees - cfg.estimated_slippage - cfg.safety_margin

        if net_yes > cfg.min_net_edge:
            opportunities.append(Opportunity(
                market_id=market.market_id,
                underlying=market.underlying,
                side="BUY_YES",
                edge_type="MODEL",
                gross_edge=edge_yes,
                estimated_fees=cfg.estimated_fees,
                estimated_slippage=cfg.estimated_slippage,
                net_edge=net_yes,
                confidence=prob.confidence,
                max_size_usdc=cfg.max_position_usdc,
                expiry_ts=market.expiry_ts,
                reason="Model probability above YES ask"
            ))

    if ob.no_ask is not None:
        prob_no = 1.0 - prob.probability_yes
        edge_no = prob_no - ob.no_ask
        net_no = edge_no - cfg.estimated_fees - cfg.estimated_slippage - cfg.safety_margin

        if net_no > cfg.min_net_edge:
            opportunities.append(Opportunity(
                market_id=market.market_id,
                underlying=market.underlying,
                side="BUY_NO",
                edge_type="MODEL",
                gross_edge=edge_no,
                estimated_fees=cfg.estimated_fees,
                estimated_slippage=cfg.estimated_slippage,
                net_edge=net_no,
                confidence=prob.confidence,
                max_size_usdc=cfg.max_position_usdc,
                expiry_ts=market.expiry_ts,
                reason="Model probability above NO ask"
            ))

    return opportunities
```

---

## 13. Filtres de risque

Avant toute opportunité, vérifier :

```text
- marché actif
- expiry non dépassée
- temps restant suffisant
- liquidité minimale
- spread acceptable
- source de prix externe cohérente
- settlement source connue
- pas de conflit avec une position Trident existante
- pas de dépassement d’exposition globale
- pas de kill switch actif
```

Pseudo-code :

```python
def risk_filter(opportunity, market, ob, supervisor_state, cfg):
    if opportunity.net_edge < cfg.min_net_edge:
        return False, "Net edge too low"

    if ob.yes_ask_depth < cfg.min_depth and opportunity.side in ["BUY_YES", "BUY_BOTH"]:
        return False, "Insufficient YES depth"

    if ob.no_ask_depth < cfg.min_depth and opportunity.side in ["BUY_NO", "BUY_BOTH"]:
        return False, "Insufficient NO depth"

    if supervisor_state.has_conflicting_position(market.underlying):
        return False, "Conflicting Trident position"

    if supervisor_state.total_exposure_usdc > cfg.max_total_exposure_usdc:
        return False, "Global exposure limit reached"

    if supervisor_state.kill_switch_active:
        return False, "Kill switch active"

    return True, "OK"
```

---

## 14. Adaptation au bot Trident

### 14.1 Rappel architecture Trident

Hypothèse :

```text
TridentSupervisor
  ├── Pod A : stratégie existante
  ├── Pod B : stratégie actuelle à remplacer
  └── Pod C : stratégie existante
```

Le Supervisor :

- reçoit les signaux des pods ;
- valide ou refuse les trades ;
- empêche les conflits ;
- applique les limites globales ;
- centralise l’exécution ou l’autorisation d’exécution ;
- garantit la contrainte “un seul trade par coin à la fois” sur Hyperliquid.

### 14.2 Nouveau Pod B proposé

Remplacer le Pod B actuel par :

```text
HIP4OutcomeEdgePod
```

Rôle :

```text
Détecter les inefficiences sur outcome tokens HIP-4.
Publier des opportunités probabilistes au Supervisor.
Ne pas ouvrir de position sans accord du Supervisor.
```

Le Pod B ne doit pas être un pod directionnel classique. Il doit être un pod :

```text
event/outcome arbitrage
probability mispricing
late-expiry arbitrage
parity arbitrage
```

---

## 15. Contrat entre Supervisor et Pod B

### 15.1 Message Pod → Supervisor

```python
@dataclass
class PodSignal:
    pod_name: str
    strategy_type: str  # OUTCOME_ARBITRAGE
    market_id: str
    underlying: str
    instrument_type: str  # HIP4_OUTCOME
    side: str  # BUY_YES | BUY_NO | BUY_BOTH
    edge_type: str
    confidence: float
    gross_edge: float
    net_edge: float
    requested_size_usdc: float
    max_loss_usdc: float
    expiry_ts: int
    reason: str
    metadata: dict
```

Exemple :

```json
{
  "pod_name": "HIP4OutcomeEdgePod",
  "strategy_type": "OUTCOME_ARBITRAGE",
  "market_id": "BTC_GT_70000_2026_05_01",
  "underlying": "BTC",
  "instrument_type": "HIP4_OUTCOME",
  "side": "BUY_YES",
  "edge_type": "LATE_EXPIRY",
  "confidence": 0.74,
  "gross_edge": 0.11,
  "net_edge": 0.06,
  "requested_size_usdc": 50,
  "max_loss_usdc": 50,
  "expiry_ts": 1770000000,
  "reason": "BTC above strike near expiry; YES underpriced",
  "metadata": {
    "reference_price": 70180,
    "strike": 70000,
    "yes_ask": 0.89,
    "time_to_expiry_seconds": 240
  }
}
```

### 15.2 Réponse Supervisor → Pod

```python
@dataclass
class SupervisorDecision:
    approved: bool
    approved_size_usdc: float
    reason: str
    execution_mode: str  # PAPER | TESTNET | LIVE
    constraints: dict
```

Exemple :

```json
{
  "approved": true,
  "approved_size_usdc": 25,
  "reason": "No conflicting BTC exposure; risk budget available",
  "execution_mode": "TESTNET",
  "constraints": {
    "post_only": false,
    "max_slippage": 0.02,
    "cancel_after_seconds": 5
  }
}
```

---

## 16. Gestion des conflits avec les autres pods

Le nouveau Pod B doit déclarer ses expositions de deux façons :

```text
1. underlying exposure
2. outcome market exposure
```

Exemple :

```text
BUY YES sur BTC > 70k
=> exposition économique positive à BTC jusqu’à expiry
```

Le Supervisor peut donc refuser si :

```text
Pod A a déjà un long BTC
Pod C a déjà un short BTC
Une règle globale interdit plusieurs trades BTC simultanés
```

Mais il peut aussi autoriser si le Pod B est considéré comme :

```text
position événementielle à perte bornée
```

Dans ce cas, le Supervisor doit appliquer une limite dédiée :

```text
max_outcome_exposure_per_underlying
```

---

## 17. Mapping du risque outcome

Pour un achat YES ou NO :

```text
max_loss = montant investi
max_gain ≈ montant * (1 / prix - 1)
```

Exemple :

```text
Acheter 100 USDC de YES à 0.80

Nombre de tokens = 100 / 0.80 = 125
Payout si gagné = 125
Profit brut = 25
Perte max = 100
```

Donc le Pod B doit toujours envoyer au Supervisor :

```text
max_loss_usdc
expected_payout_usdc
edge_net
expiry
```

---

## 18. Risk management spécifique Pod B

Règles recommandées :

```text
- taille faible en phase testnet/mainnet initiale
- aucune martingale
- aucun renforcement automatique
- pas d’ordre si settlement source inconnue
- pas d’ordre si parsing ambigu
- pas d’ordre si time_to_expiry trop faible
- pas d’ordre si spread énorme
- pas d’ordre si profondeur insuffisante
- annulation rapide si non fill
- mode observer si erreur API répétée
```

Seuils initiaux :

```yaml
risk:
  max_position_usdc: 25
  max_total_outcome_exposure_usdc: 100
  max_outcome_markets_open: 3
  max_per_underlying_outcome_exposure_usdc: 50
  min_net_edge: 0.03
  safety_margin: 0.02
```

---

## 19. Exécution recommandée

En testnet :

```text
Pod B peut exécuter directement après validation Supervisor.
```

En mainnet :

```text
Le Supervisor devrait garder le contrôle final de l’exécution.
```

Architecture préférée :

```text
Pod B détecte
→ Pod B envoie signal
→ Supervisor valide
→ ExecutionEngine centralisé passe l’ordre
→ Pod B suit le settlement
```

Cela évite conflits entre pods, double position, dépassement de risque, signatures API dispersées et logs incohérents.

---

## 20. Logs indispensables

### opportunities.csv

```csv
ts,market_id,underlying,edge_type,side,gross_edge,net_edge,confidence,yes_ask,no_ask,ref_price,strike,time_to_expiry
```

### decisions.jsonl

```json
{
  "ts": 1770000000,
  "pod": "HIP4OutcomeEdgePod",
  "signal": {},
  "supervisor_decision": {},
  "reason": "approved"
}
```

### trades.csv

```csv
ts,market_id,side,price,size_usdc,status,fill_qty,fill_price,order_id
```

### settlements.csv

```csv
market_id,expiry_ts,result,payout,pnl,notes
```

---

## 21. Critères de passage de phase

### Observer → Paper

Passer en paper si :

```text
- marchés correctement découverts
- parsing fiable sur >95% des marchés ciblés
- logs complets
- prix externes stables
- edge détecté sans erreurs critiques
```

### Paper → Testnet execution

Passer en testnet si :

```text
- PnL paper cohérent
- pas d’erreurs de settlement simulé
- gestion fill partiel prête
- risk manager fonctionnel
- Supervisor contract validé
```

### Testnet → Mainnet

Passer en mainnet seulement si :

```text
- exécution testnet stable
- settlement réel compris
- frais correctement modélisés
- aucune erreur critique sur plusieurs jours
- kill switch testé
- taille initiale très faible
```

---

## 22. Intégration concrète comme Pod B

Classe cible :

```python
class HIP4OutcomeEdgePod:
    def __init__(self, config, supervisor_client):
        self.config = config
        self.supervisor = supervisor_client

    async def run_once(self):
        markets = await self.discover_markets()

        for market in markets:
            ob = await self.get_orderbook(market)
            ref_price = await self.get_reference_price(market.underlying)
            prob = await self.estimate_probability(market, ref_price)

            opportunities = []
            opportunities += self.detect_late_expiry(market, ob, ref_price)
            opportunities += self.detect_parity(market, ob)
            opportunities += self.detect_model_mispricing(market, ob, prob)

            for opp in opportunities:
                if self.local_risk_ok(opp):
                    signal = self.to_pod_signal(opp)
                    decision = await self.supervisor.request_approval(signal)

                    if decision.approved:
                        await self.execute_or_publish(opp, decision)
```

---

## 23. Loop principale

```python
async def main_loop():
    while True:
        try:
            await pod.run_once()
        except Exception as e:
            logger.exception("Pod B error")
            await pod.notify_supervisor_error(e)

        await asyncio.sleep(config.loop_interval_seconds)
```

---

## 24. Priorité d’implémentation

Ordre recommandé :

```text
1. Discovery outcomeMeta
2. Parser market question/strike/expiry
3. External price aggregator
4. Orderbook reader
5. Late-expiry detector
6. Parity detector
7. Probability model simple
8. Paper trader
9. Supervisor contract
10. Testnet executor
11. Settlement tracker
12. Replay/backtest
```

Commencer par le **late-expiry detector** car c’est le plus simple à comprendre et à debugger.

---

## 25. Ce qu’il ne faut pas faire

```text
- Traiter HIP-4 comme un simple perp
- Comparer directement prix BTC HL vs Binance sans tenir compte de la probabilité
- Trader si la définition de settlement est ambiguë
- Trader si le parsing de la question est incertain
- Trader avec une taille élevée dès le départ
- Ignorer les frais de settlement
- Ignorer le spread et le slippage
- Laisser le pod exécuter sans validation Supervisor
- Mélanger l’exposition outcome avec une exposition perp classique sans mapping
```

---

## 26. Résumé pour le LLM chargé de l’implémentation

Construire un nouveau Pod B nommé :

```text
HIP4OutcomeEdgePod
```

Mission :

```text
Détecter et exploiter en testnet les inefficiences de pricing sur les outcome tokens HIP-4 Hyperliquid.
```

Edges à implémenter :

```text
1. Late-expiry arbitrage
2. YES/NO parity arbitrage
3. Model probability mispricing
```

Contraintes :

```text
- observer-only d’abord
- paper trading ensuite
- testnet execution ensuite
- pas de mainnet sans validation
- Supervisor approval obligatoire
- risk limits stricts
- logs complets
```

Intégration Trident :

```text
Le pod remplace l’ancien Pod B.
Il ne doit pas entrer en conflit avec les Pods A/C.
Il publie un signal structuré au Supervisor.
Le Supervisor valide ou refuse selon exposition globale, underlying, risque et mode d’exécution.
```

Règle conceptuelle :

```text
On ne trade pas un prix.
On trade un écart entre probabilité implicite et probabilité estimée.
```

---

## 27. Mini-roadmap

### Sprint 1

```text
- Créer squelette HIP4OutcomeEdgePod
- Charger config
- Lister outcome markets testnet
- Logger markets découverts
```

### Sprint 2

```text
- Parser strike/expiry/underlying
- Récupérer prix externes
- Lire orderbook YES/NO
- Calculer late-expiry edge
```

### Sprint 3

```text
- Ajouter paper trader
- Ajouter parity detector
- Ajouter probability model simple
- Générer opportunities.csv
```

### Sprint 4

```text
- Ajouter contrat Supervisor
- Ajouter risk manager
- Ajouter testnet executor
- Gérer fills/annulations
```

### Sprint 5

```text
- Ajouter settlement tracker
- Ajouter replay/backtest
- Mesurer edge réel
- Préparer critères mainnet
```

---

## 28. Conclusion

Cette idée est plus intéressante qu’un simple bot d’indicateurs techniques car elle cherche une inefficience structurelle :

```text
prix de marché d’une probabilité
vs
probabilité estimée à partir de données externes et du temps restant
```

Le testnet Hyperliquid est le bon environnement pour démarrer.

Le nouveau Pod B doit être conçu comme un pod spécialisé :

```text
probabilistic event arbitrage
```

et non comme un pod de momentum ou de trend following.

La priorité absolue est la robustesse :

```text
parsing fiable
settlement compris
risque borné
supervisor approval
logs exhaustifs
```
