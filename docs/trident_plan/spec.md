# TRIDENT — Spec et architecture

## Resume executif

TRIDENT est un systeme compose de 3 pods live et 1 pod research:

- `Pod A — Anchor Trend`: swing / trend following
- `Pod B — Range Harvester`: maker/range/inventory management
- `Pod C — Event Raider`: moteur evenementiel
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

- calcule le `global_regime`
- derive le `tradable_pool`
- route chaque coin vers un pod unique
- maintient l'ownership
- publie l'allocation effective vers Pod B
- reste la seule source d'autorite pour le routage et l'ownership

### Regime multi-niveaux

- `global_regime`:
  - borne le risque
  - borne les caps par pod
  - decrit la posture macro
- `local_regime`:
  - decrit le contexte coin par coin
  - alimente le scoring de routing
  - autorise des divergences locales lisibles

### Routing des symbols

Pour chaque symbol observe:

- le superviseur calcule un score d'affinite par pod
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

### Pod B — Range Harvester

- role: range/maker/inventory
- statut: moteur natif present, mais calibration encore ouverte
- points cle:
  - manager + status runtime coherents
  - drift trimming et logs renforces
  - paper/live runner disponibles

### Pod C — Event Raider

- role: impulsion/event/lead-lag
- statut: minimal implemente, research deja cadree
- points cle:
  - followers dynamiques par cluster
  - exclusions structurelles sur les leaders

## Donnees et observabilite

### Donnees

- snapshots live JSONL
- datasets archives locaux
- statuts runtime par pod
- journaux dry-run/live

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
- `pod_a`
- `pod_b`
- `pod_c`

## Deploiement

Mode supportes:

- local dev
- serveur simple
- docker compose

La couche runtime HTTP reste volontairement simple en stdlib.
