# TRIDENT — Spec et architecture

## Resume executif

TRIDENT est un systeme compose de 3 pods live et 1 pod research:

- `Pod A — Anchor Trend`: swing / trend following
- `Pod B — Breakout Expansion`: breakout / vol-expansion directionnel crypto
- `Pod C — Tradfi Trend`: moteur directionnel dedie aux instruments Tradfi HL
- `Research Pod`: experimentation offline uniquement

Le systeme ne s'appuie pas sur un LLM dans la boucle de decision live.

## Contraintes non negociables

- une seule position nette par coin sur Hyperliquid
- ownership exclusif des symbols
- allocation par pod deterministe
- passage live seulement apres dry-run/paper
- le mode cash est un etat valide

## Architecture cible

### Superviseur

Le superviseur central:

- calcule le `crypto_regime`
- derive le `tradable_pool`
- route chaque coin vers un pod unique
- maintient l'ownership
- publie l'allocation effective vers Pod B
- reste la seule source d'autorite pour le routage et l'ownership

### Regime multi-niveaux

- `crypto_regime` (derive de BTC):
  - borne le risque
  - borne les caps par pod (Pod A, Pod B)
  - decrit la posture macro crypto
- `cluster_regimes` (derive des leaders par cluster):
  - chaque cluster a un leader: BTC→crypto, `XYZ:SP500`→index, `XYZ:GOLD`→gold, `XYZ:SILVER`→silver, `XYZ:CL`→oil, `XYZ:JPY`→fx
  - chaque leader produit un `RegimeSnapshot` independant
  - `crypto_regime` = regime du cluster crypto (BTC)
  - les clusters non-crypto ne doivent pas etre applatis en un seul driver d'allocation
  - chaque cluster Tradfi pilote son propre budget de capital
  - `pod_c.target_pct` = somme des budgets actifs des clusters Tradfi
  - `cash` = residuel global apres recomposition de tous les sleeves
- `local_regime`:
  - decrit le contexte coin par coin
  - alimente le scoring de routing
  - autorise des divergences locales lisibles

### Allocation cible

La reponse cible au probleme des clusters Tradfi divergents n'est pas un simple `tradfi_regime` unique.

Le systeme doit separer:

- un sleeve `crypto`:
  - pilote par `crypto_regime`
  - alloue vers `Pod A` et `Pod B`
- des sleeves Tradfi par cluster:
  - `index`
  - `gold`
  - `silver`
  - `oil`
  - `fx`
  - `equity`
  - autres clusters actives si ajoutees

Principes:

- un cluster Tradfi faible ne doit pas capter du capital parce qu'un autre cluster Tradfi ou BTC est fort
- un cluster Tradfi fort doit pouvoir recevoir du capital meme si BTC est en `panic_squeeze`
- aucune table de config ne doit conserver un `cash` crypto inchange si un budget de cluster Tradfi est substitue
- les invariants comptables sont non negociables:
  - somme finale des allocations `<= 1.0`
  - aucune allocation negative
  - fallback unique et coherent quand un cluster n'a pas de snapshot

### Routing des symbols

Pour chaque symbol observe:

- le superviseur filtre les candidats par `market_cluster`:
  - Pod A: clusters configures dans `pod_a.allowed_market_clusters`
  - Pod B: clusters configures dans `pod_b.allowed_market_clusters`
  - Pod C: clusters configures dans `pod_c.allowed_market_clusters`
- le superviseur calcule un score d'affinite par pod eligible
- choisit un owner unique
- applique un fallback seulement si le signal est trop faible
- applique:
  - seuil minimal
  - hysteresis
  - cooldown de reattribution

Le routing expose:

- `symbol_routing`
- `local_regime_by_symbol`
- `local_regime_transitions`
- `symbol_reassignment_count_by_symbol`
- `routing_overrides`

### Overrides

Le routing peut etre pilote manuellement:

- statiquement via `config/trident.toml`
- dynamiquement via:
  - fichier runtime `runtime/trident/symbol_routing_overrides.json`
  - endpoint `POST /api/routing/override`
  - UI System

## Pods

### Pod A — Anchor Trend

- role: trend/continuation/reclaim structurel
- statut: socle live utilisable, extension `4bis` quasi terminee
- points cle:
  - sizing et risk gate partages
  - fermeture `routing_revoked`
  - adaptation par cluster de marche

### Pod B — Breakout Expansion

- role: breakout / impulsion / expansion naissante sur le sleeve crypto
- statut: moteur directionnel branche sur le pipeline partage, calibration encore ouverte
- points cle:
  - runtime status homogene avec Pod A / Pod C
  - replay et live runner dedies
  - filtres de contexte pour eviter l'overtrading hors regime

### Pod C — Tradfi Trend

- role: directionnel Tradfi / macro trend sur HL
- statut: slot live branche sur un panier builder-dex HL, budget par cluster actif, logique directionnelle partagee avec `Pod A`
- points cle:
  - execution et risk gate partages avec `Pod A`
  - univers restreint aux symbols Tradfi builder-dex valides
  - symbols canoniques en config (`XYZ:SP500`) puis resolution dex-aware pour `allMids`, `metaAndAssetCtxs` et le websocket HL
  - allocations derivees de budgets par cluster Tradfi (pas du regime crypto global)
  - le routeur utilise le regime du cluster du symbol pour scorer Pod C
  - validation offline sur snapshots minute microstructure, pas sur candles HL seules

## Donnees et observabilite

### Donnees

- snapshots live JSONL
- datasets archives locaux
- enrichissements funding / OI / mark / oracle disponibles hors snapshots si besoin
- statuts runtime par pod
- journaux dry-run/live
- regle de validation:
  - un backtest credible pour les pods directionnels repose sur des snapshots minute TRIDENT (`l2Book + trades`) ou des archives equivalentes
  - les candles HL seules ne sont pas considerees suffisantes pour valider un pod live

### Resilience market data

Le live runner Pod A dispose d'un fallback REST pour les positions ouvertes dont le flux WebSocket est absent:

- a chaque record, le runner compare les positions ouvertes aux symbols presents dans le snapshot WS
- si un symbol avec position ouverte est absent, le runner appelle l'endpoint REST `allMids` de Hyperliquid
- un snapshot synthetique est injecte pour que l'executor puisse evaluer les conditions de sortie (stop, trailing, time-stop)
- sans ce fallback, une position sans snapshot WS serait invisible: aucun exit check ne serait evalue

Le `ClosedTrade` preserve desormais les champs d'exit policy complets:
- `trailing_activation_bps`, `trailing_distance_bps`, `break_even_trigger_bps`
- ces champs etaient precedemment perdus a la fermeture du trade

### Dashboard

Le dashboard doit permettre:

- lecture rapide de sante globale
- lecture par pod
- lecture du routing et des overrides
- activite recente
- positions ouvertes

L'onglet `System` est la vue de reference pour:

- ownership
- decisions detaillees de routing
- transitions locales
- overrides actifs

## Config

La config centrale reste `config/trident.toml`.

Sections structurantes:

- `general`
- `hyperliquid`
- `trident.regime`
- `trident.capital`
- `trident.routing`
- `trident.allocations.*` (crypto: Pod A, B)
- `trident.allocations_cluster.*` (budgets Tradfi par cluster et par regime)
- `pod_a`
- `pod_b`
- `pod_c`

## Deploiement

Mode supportes:

- local dev
- serveur simple
- docker compose

La couche runtime HTTP reste volontairement simple en stdlib.
